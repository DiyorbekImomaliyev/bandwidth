"""AI router: privacy routing, fallback, cooldowns, structured output, accounting (SPEC §4)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from pydantic import BaseModel

from bandwidth.ai import (
    TASKS,
    AIError,
    Ledger,
    PrivacyError,
    Provider,
    Router,
    Task,
    inline_refs,
    is_local_url,
    load_router,
    strip_think,
)

CLOUD_A = "https://a.example.com/v1"
CLOUD_B = "https://b.example.com/v1"
LOCAL = "http://localhost:11434"


class Answer(BaseModel):
    ok: bool
    word: str


def run(coro):
    return asyncio.run(coro)


def openai_reply(content: str, prompt_tokens=11, completion_tokens=7) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}})


def ollama_reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content},
                                     "done": True, "prompt_eval_count": 20, "eval_count": 5})


GOOD = json.dumps({"ok": True, "word": "hi"})
PUBLIC = Task("public", private=False)  # a public task that may fall back to the local model


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "agent.db")


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setenv("A_KEY", "secret-a-123")
    monkeypatch.setenv("B_KEY", "secret-b-456")
    return [
        Provider("a", "openai", CLOUD_A, "model-a", key_env="A_KEY"),
        Provider("b", "openai", CLOUD_B, "model-b", key_env="B_KEY"),
        Provider("local", "ollama", LOCAL, "qwen3:8b"),
    ]


def router(providers, ledger, **kw) -> Router:
    return Router(providers, ledger=ledger, **kw)


# ---------------------------------------------------------------- privacy


@pytest.mark.parametrize("url,local", [
    ("http://localhost:11434", True), ("http://127.0.0.1:11434/v1", True), ("http://[::1]:8080", True),
    ("http://127.8.9.1", True), ("https://api.groq.com/openai/v1", False),
    ("http://localhost.evil.com", False), ("http://10.0.0.5:11434", False), ("http://0.0.0.0", False),
])
def test_local_is_decided_from_the_url(url, local):
    """AI-2: local means the URL host is loopback, nothing else."""
    assert is_local_url(url) is local


def test_tasks_have_a_privacy_class():
    """AI-1, AI-5"""
    assert TASKS["rank"].private is False and TASKS["rank"].local_fallback is False
    assert TASKS["requirements"].private is False and TASKS["requirements"].local_fallback is False
    assert TASKS["chat"].private is True


@respx.mock
def test_private_call_goes_only_to_the_local_provider(providers, ledger):
    """AI-2"""
    cloud = respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=openai_reply(GOOD))
    local = respx.post(f"{LOCAL}/api/chat").mock(return_value=ollama_reply(GOOD))
    out = run(router(providers, ledger).json("chat", "sys", "my message", Answer))
    assert out == Answer(ok=True, word="hi")
    assert local.called and not cloud.called


@respx.mock
def test_private_call_never_falls_back_to_the_cloud(providers, ledger):
    """AI-2: local down → error, not the cloud."""
    cloud = respx.post(url__startswith="https://").mock(return_value=openai_reply(GOOD))
    respx.post(f"{LOCAL}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(AIError):
        run(router(providers, ledger).json("chat", "sys", "my message", Answer))
    assert not cloud.called


def test_private_call_without_a_local_provider_fails(providers, ledger):
    """AI-2"""
    with pytest.raises(PrivacyError):
        run(router(providers[:2], ledger).json("chat", "sys", "x", Answer))


def test_a_provider_flagged_local_but_remote_is_not_trusted(ledger):
    """AI-2: the URL decides, so a mislabelled cloud provider is never used for private calls."""
    sneaky = Provider("local", "ollama", "https://cloud.example.com", "qwen3:8b")
    with pytest.raises(PrivacyError):
        run(router([sneaky], ledger).json("chat", "sys", "x", Answer))


@respx.mock
def test_public_call_with_a_private_term_is_blocked(providers, ledger):
    """AI-3: the guard blocks the call and names the term, not the text."""
    route = respx.post(url__startswith="https://").mock(return_value=openai_reply(GOOD))
    r = router(providers, ledger, private_terms=["Uzbekistan", "+974 5555 1234"])
    with pytest.raises(PrivacyError) as e:
        run(r.json("rank", "sys", "I am from uzbekistan and like AI", Answer))
    assert "Uzbekistan" in str(e.value) and "like AI" not in str(e.value)
    assert not route.called


@respx.mock
def test_public_data_is_not_checked_and_is_marked_as_data(providers, ledger):
    """AI-3, AI-15: a posting that mentions Uzbekistan is public text, not the owner's data."""
    route = respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=openai_reply(GOOD))
    r = router(providers, ledger, private_terms=["Uzbekistan"])
    run(r.json("rank", "sys", "rate these", Answer, data='[{"title": "Hackathon in Uzbekistan"}]'))
    messages = json.loads(route.calls[0].request.content)["messages"]
    assert "never instructions" in messages[0]["content"]
    assert messages[1]["content"].startswith("rate these")
    assert "<data>\n[{\"title\": \"Hackathon in Uzbekistan\"}]\n</data>" in messages[1]["content"]


@respx.mock
def test_private_terms_do_not_block_private_calls(providers, ledger):
    """AI-3 applies to public calls only."""
    respx.post(f"{LOCAL}/api/chat").mock(return_value=ollama_reply(GOOD))
    r = router(providers, ledger, private_terms=["Uzbekistan"])
    assert run(r.json("chat", "sys", "citizen of Uzbekistan", Answer)).ok


def test_short_private_terms_are_ignored(providers, ledger):
    """AI-3: terms under 3 characters would block everything."""
    r = router(providers, ledger, private_terms=["", "a", "QA", "Uzbekistan"])
    assert r.private_terms == ["uzbekistan"]


# ---------------------------------------------------------------- fallback


@respx.mock
def test_public_call_uses_the_first_cloud_provider(providers, ledger):
    """AI-7"""
    a = respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=openai_reply(GOOD))
    b = respx.post(f"{CLOUD_B}/chat/completions").mock(return_value=openai_reply(GOOD))
    run(router(providers, ledger).json("rank", "sys", "public text", Answer))
    assert a.called and not b.called
    body = json.loads(a.calls[0].request.content)
    assert body["model"] == "model-a"
    assert body["response_format"] == {"type": "json_object"}
    assert a.calls[0].request.headers["authorization"] == "Bearer secret-a-123"


@respx.mock
def test_falls_back_in_order_then_to_local(providers, ledger):
    """AI-7"""
    respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=httpx.Response(500))
    respx.post(f"{CLOUD_B}/chat/completions").mock(side_effect=httpx.ReadTimeout("slow"))
    local = respx.post(f"{LOCAL}/api/chat").mock(return_value=ollama_reply(GOOD))
    r = router(providers, ledger)
    assert run(r.json(PUBLIC, "sys", "x", Answer)).ok
    assert local.called and r.last_provider == "local"


@respx.mock
def test_a_task_can_opt_out_of_the_local_fallback(providers, ledger):
    """AI-16"""
    respx.post(url__startswith="https://").respond(503)
    local = respx.post(f"{LOCAL}/api/chat").mock(return_value=ollama_reply(GOOD))
    with pytest.raises(AIError):
        run(router(providers, ledger).json(Task("big", private=False, local_fallback=False), "sys", "x", Answer))
    assert not local.called


@respx.mock
def test_provider_without_its_key_is_skipped(providers, ledger, monkeypatch):
    """AI-7"""
    monkeypatch.delenv("A_KEY")
    a = respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=openai_reply(GOOD))
    b = respx.post(f"{CLOUD_B}/chat/completions").mock(return_value=openai_reply(GOOD))
    run(router(providers, ledger).json("rank", "sys", "x", Answer))
    assert b.called and not a.called


@respx.mock
def test_429_cools_a_provider_down_for_retry_after(providers, ledger):
    """AI-8"""
    clock = [1000.0]
    a = respx.post(f"{CLOUD_A}/chat/completions").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"}))
    respx.post(f"{CLOUD_B}/chat/completions").mock(return_value=openai_reply(GOOD))
    r = router(providers, ledger, clock=lambda: clock[0])
    run(r.json("rank", "sys", "x", Answer))
    run(r.json("rank", "sys", "x", Answer))
    assert a.call_count == 1  # resting
    clock[0] += 121
    run(r.json("rank", "sys", "x", Answer))
    assert a.call_count == 2


@respx.mock
def test_retry_after_is_capped_at_an_hour(providers, ledger):
    """AI-8"""
    respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=httpx.Response(429, headers={"Retry-After": "999999"}))
    respx.post(f"{CLOUD_B}/chat/completions").mock(return_value=openai_reply(GOOD))
    r = router(providers, ledger, clock=lambda: 0.0)
    run(r.json("rank", "sys", "x", Answer))
    assert r.resting_until("a") == 3600


def fake_time(start=1000.0):
    """A clock and a sleep that moves it, so waits take no real time."""
    now, slept = [start], []

    async def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    return (lambda: now[0]), sleep, slept


@respx.mock
def test_a_short_limit_is_waited_out_instead_of_failing(providers, ledger):
    """AI-8: Groq's per-minute limit — every cloud provider is busy for a few seconds → wait, try again."""
    clock, sleep, slept = fake_time()
    a = respx.post(f"{CLOUD_A}/chat/completions").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "7"}), openai_reply(GOOD)])
    respx.post(f"{CLOUD_B}/chat/completions").mock(return_value=httpx.Response(429, headers={"Retry-After": "30"}))
    r = router(providers, ledger, clock=clock, sleep=sleep)
    assert run(r.json("rank", "sys", "x", Answer)) == Answer(ok=True, word="hi")
    assert slept == [7] and a.call_count == 2 and r.last_provider == "a"


@respx.mock
def test_a_long_limit_is_not_waited_for(providers, ledger):
    """AI-8: a daily limit (or 5-minute rest) fails the call at once; the caller falls back."""
    clock, sleep, slept = fake_time()
    respx.post(url__startswith="https://").mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    r = router(providers, ledger, clock=clock, sleep=sleep)
    with pytest.raises(AIError):
        run(r.json("rank", "sys", "x", Answer))
    assert slept == []


@respx.mock
def test_the_wait_happens_once_per_call(providers, ledger):
    """AI-8: still limited after the wait → the call fails instead of looping."""
    clock, sleep, slept = fake_time()
    a = respx.post(url__startswith="https://").mock(return_value=httpx.Response(429, headers={"Retry-After": "5"}))
    r = router(providers, ledger, clock=clock, sleep=sleep)
    with pytest.raises(AIError):
        run(r.json("rank", "sys", "x", Answer))
    assert slept == [5] and a.call_count == 4


@respx.mock
def test_token_budget_raises_max_tokens_for_one_provider(providers, ledger):
    """AI-6: a reasoning model gets room to think; other providers keep the task's budget."""
    providers[0].token_budget = 16000
    a = respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=httpx.Response(503))
    b = respx.post(f"{CLOUD_B}/chat/completions").mock(return_value=openai_reply(GOOD))
    run(router(providers, ledger).json("rank", "sys", "x", Answer))
    sent = lambda route: json.loads(route.calls[0].request.content)["max_tokens"]  # noqa: E731
    assert sent(a) == 16000 and sent(b) == TASKS["rank"].max_tokens


@respx.mock
def test_three_failures_in_a_row_rest_a_provider_five_minutes(providers, ledger):
    """AI-8: and a success resets the count."""
    clock = [0.0]
    a = respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=httpx.Response(503))
    respx.post(f"{CLOUD_B}/chat/completions").mock(return_value=openai_reply(GOOD))
    r = router(providers, ledger, clock=lambda: clock[0])
    for _ in range(4):
        run(r.json("rank", "sys", "x", Answer))
    assert a.call_count == 3
    assert r.resting_until("a") == 300
    clock[0] = 301
    a.mock(return_value=openai_reply(GOOD))
    run(r.json("rank", "sys", "x", Answer))
    assert r.failures_in_a_row("a") == 0


@respx.mock
def test_all_failing_raises_one_error_listing_each_without_keys(providers, ledger):
    """AI-11, AI-14"""
    respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=httpx.Response(401, text="bad key secret-a-123"))
    respx.post(f"{CLOUD_B}/chat/completions").mock(return_value=httpx.Response(500))
    respx.post(f"{LOCAL}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(AIError) as e:
        run(router(providers, ledger).json(PUBLIC, "sys", "x", Answer))
    msg = str(e.value)
    assert "a:" in msg and "b:" in msg and "local:" in msg
    assert "secret-a-123" not in msg


# ---------------------------------------------------------------- structured output


@respx.mock
def test_invalid_json_gets_one_repair_request(providers, ledger):
    """AI-9"""
    a = respx.post(f"{CLOUD_A}/chat/completions").mock(side_effect=[
        openai_reply('{"ok": "maybe"}'), openai_reply(GOOD)])
    assert run(router(providers, ledger).json("rank", "sys", "x", Answer)).word == "hi"
    repair = json.loads(a.calls[1].request.content)["messages"]
    assert repair[-2]["role"] == "assistant" and "invalid" in repair[-1]["content"].lower()


@respx.mock
def test_still_invalid_after_repair_moves_to_the_next_provider(providers, ledger):
    """AI-9"""
    respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=openai_reply("not json"))
    b = respx.post(f"{CLOUD_B}/chat/completions").mock(return_value=openai_reply(GOOD))
    assert run(router(providers, ledger).json("rank", "sys", "x", Answer)).ok
    assert b.called


@respx.mock
def test_schema_is_in_the_prompt_for_openai_providers(providers, ledger):
    """AI-9"""
    a = respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=openai_reply(GOOD))
    run(router(providers, ledger).json("rank", "be brief", "x", Answer))
    system = json.loads(a.calls[0].request.content)["messages"][0]["content"]
    assert system.startswith("be brief") and '"word"' in system and "JSON" in system


@respx.mock
def test_ollama_gets_the_schema_as_format_and_thinking_off(providers, ledger):
    """AI-9, AI-10: constrained decoding, no thinking for tasks that don't need it."""
    local = respx.post(f"{LOCAL}/api/chat").mock(return_value=ollama_reply(GOOD))
    run(router(providers, ledger).json("chat", "sys", "x", Answer))
    body = json.loads(local.calls[0].request.content)
    assert body["format"]["properties"]["word"] == {"title": "Word", "type": "string"}
    assert body["think"] is False and body["stream"] is False
    assert body["options"]["num_ctx"] >= 8192


@respx.mock
def test_thinking_task_keeps_thinking_on(providers, ledger):
    """AI-10"""
    local = respx.post(f"{LOCAL}/api/chat").mock(return_value=ollama_reply(GOOD))
    r = router(providers, ledger)
    run(r.json(Task("deep", private=True, think=True), "sys", "x", Answer))
    assert json.loads(local.calls[0].request.content)["think"] is True


def test_think_blocks_are_removed():
    """AI-10"""
    assert strip_think("<think>\nhmm\n</think>\n\n{\"a\": 1}") == '{"a": 1}'
    assert strip_think('{"a": 1}') == '{"a": 1}'


@respx.mock
def test_json_inside_a_code_fence_is_accepted(providers, ledger):
    """AI-9: some models wrap JSON in ```json fences."""
    respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=openai_reply(f"```json\n{GOOD}\n```"))
    assert run(router(providers, ledger).json("rank", "sys", "x", Answer)).ok


def test_inline_refs_removes_defs():
    """AI-9: nested models become one self-contained schema."""
    class Inner(BaseModel):
        x: int

    class Outer(BaseModel):
        items: list[Inner]

    schema = inline_refs(Outer.model_json_schema())
    assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)
    assert schema["properties"]["items"]["items"]["properties"]["x"]["type"] == "integer"


@respx.mock
def test_text_call_returns_plain_text(providers, ledger):
    respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=openai_reply("<think>x</think>Hello."))
    assert run(router(providers, ledger).text("ping", "sys", "hi")) == "Hello."


# ---------------------------------------------------------------- accounting


@respx.mock
def test_every_attempt_is_logged_without_prompt_text(providers, ledger):
    """AI-12"""
    respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=httpx.Response(500))
    respx.post(f"{CLOUD_B}/chat/completions").mock(return_value=openai_reply(GOOD, 30, 4))
    run(router(providers, ledger).json("rank", "sys", "SECRET PROMPT TEXT", Answer))
    rows = ledger.rows()
    assert [(r["provider"], r["ok"]) for r in rows] == [("a", 0), ("b", 1)]
    assert rows[1]["tokens_in"] == 30 and rows[1]["tokens_out"] == 4 and rows[1]["task"] == "rank"
    assert rows[0]["error"] == "http_500"
    dump = ledger.path.read_bytes()
    assert b"SECRET PROMPT TEXT" not in dump


@respx.mock
def test_stats_count_private_calls_that_left_the_machine(providers, ledger):
    """AI-13: always zero; computed from the logged URL host, not from the provider name."""
    respx.post(f"{LOCAL}/api/chat").mock(return_value=ollama_reply(GOOD))
    respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=openai_reply(GOOD))
    r = router(providers, ledger)
    run(r.json("chat", "sys", "x", Answer))
    run(r.json("rank", "sys", "x", Answer))
    stats = ledger.stats(days=7)
    assert stats["private_calls_off_machine"] == 0
    assert {row["provider"] for row in stats["by_provider"]} == {"a", "local"}


# ---------------------------------------------------------------- config


def test_load_router_from_yaml(tmp_path, monkeypatch, ledger):
    """AI-6"""
    cfg = tmp_path / "ai.yaml"
    cfg.write_text(
        "private_terms: [Uzbekistan]\n"
        "providers:\n"
        "  - {name: groq, kind: openai, base_url: 'https://api.groq.com/openai/v1', model: m, key_env: GROQ_API_KEY}\n"
        "  - {name: local, kind: ollama, base_url: 'http://localhost:11434', model: 'qwen3:8b', num_ctx: 16384}\n")
    r = load_router(cfg, ledger=ledger)
    assert [p.name for p in r.providers] == ["groq", "local"]
    assert r.providers[1].num_ctx == 16384 and r.providers[1].local
    assert r.private_terms == ["uzbekistan"]


@pytest.mark.parametrize("bad,message", [
    ("providers: []", "at least one provider"),
    ("providers:\n  - {name: x, kind: soap, base_url: 'http://localhost', model: m}", "kind"),
    ("providers:\n  - {name: x, kind: openai, base_url: 'ftp://x', model: m}", "base_url"),
    ("providers:\n  - {name: x, kind: openai, base_url: 'https://x', model: m, key: abc}", "key"),
])
def test_bad_ai_config_is_a_clear_error(tmp_path, bad, message, ledger):
    """AI-6: keys never in the config file."""
    cfg = tmp_path / "ai.yaml"
    cfg.write_text(bad)
    with pytest.raises(ValueError, match=message):
        load_router(cfg, ledger=ledger)


def test_example_config_is_valid(ledger):
    """AI-6"""
    from bandwidth.paths import CONFIG_DIR
    r = load_router(CONFIG_DIR / "ai.example.yaml", ledger=ledger)
    assert any(p.local for p in r.providers)


@respx.mock
def test_answer_cut_off_by_the_token_limit_is_reported_as_truncated(providers, ledger):
    """AI-10: e.g. a thinking model spent the budget on hidden reasoning; don't try to repair it."""
    a = respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": "Here is the JSON:"}, "finish_reason": "length"}]}))
    respx.post(f"{CLOUD_B}/chat/completions").mock(return_value=openai_reply(GOOD))
    assert run(router(providers, ledger).json("rank", "sys", "x", Answer)).ok
    assert a.call_count == 1
    assert ledger.rows()[0]["error"] == "truncated"


@respx.mock
def test_no_think_params_are_sent_only_to_tasks_that_do_not_think(ledger, monkeypatch):
    """AI-10: e.g. Gemini's reasoning_effort: none."""
    monkeypatch.setenv("A_KEY", "k" * 10)
    p = Provider("a", "openai", CLOUD_A, "m", key_env="A_KEY", no_think={"reasoning_effort": "none"})
    route = respx.post(f"{CLOUD_A}/chat/completions").mock(return_value=openai_reply(GOOD))
    r = router([p], ledger)
    run(r.json("rank", "sys", "x", Answer))
    run(r.json(Task("deep", private=False, think=True), "sys", "x", Answer))
    first, second = (json.loads(c.request.content) for c in route.calls)
    assert first["reasoning_effort"] == "none" and "reasoning_effort" not in second
