"""AI refinement through the router (R-REL-4, RAD-1..4) and configuration (SPEC §13)."""

import json
import os

import httpx
import pytest
import respx

from bandwidth.radar.config import ConfigError, load_dotenv, load_profile, load_sources
from bandwidth.radar.llm import BATCH_SIZE, Refiner
from tests.radar.ai_helpers import CLOUD, LOCAL, data_items, make_router, messages, rank_answer, rules_from_local
from tests.radar.conftest import CONFIG_EXAMPLES, NOW, item, run


def refiner(profile, **kw):
    return Refiner(profile, router=make_router(**kw))


@respx.mock
def test_ranking_is_a_public_call_with_the_public_profile_only(profile):
    """RAD-1, AI-4: about goes to the cloud; private facts never do; items travel as data."""
    route = respx.post(CLOUD).mock(side_effect=rank_answer(7))
    local = respx.post(LOCAL).mock(side_effect=rules_from_local())
    ref = refiner(profile)
    out = run(ref.refine({"u1": item(title="AI Night")}, NOW))
    assert out["u1"].about == "AI: AI Night" and out["u1"].score == 7
    assert ref.label == "cloud" and not local.called
    sent = json.dumps(messages(route.calls[0].request))
    assert "Istanbul-based" in sent and "Freedonia" not in sent
    assert data_items(route.calls[0].request)[0]["title"] == "AI Night"


@respx.mock
def test_items_are_ranked_in_batches(profile):
    route = respx.post(CLOUD).mock(side_effect=rank_answer())
    n = 2 * BATCH_SIZE + 5
    items = {f"u{i}": item(url=f"https://x.y/{i}", title=f"AI thing {i}") for i in range(n)}
    out = run(refiner(profile, local=False).refine(items, NOW))
    assert len(out) == n and route.call_count == 3


@respx.mock
def test_all_providers_failing_returns_empty_and_records(profile):
    """R-REL-4: any AI failure → {} (rule scores are kept)."""
    respx.post(CLOUD).respond(403, json={"error": {"message": "denied"}})
    respx.post(LOCAL).mock(side_effect=httpx.ConnectError("refused"))
    ref = refiner(profile)
    assert run(ref.refine({"u1": item()}, NOW)) == {}
    assert len(ref.failures) == 1 and "http_403" in ref.failures[0]


@respx.mock
def test_unknown_ids_from_model_are_ignored(profile):
    respx.post(CLOUD).respond(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
        {"verdicts": [{"id": "nope", "relevant": True, "score": 9, "about": "x", "deadline": None}]})}}]})
    assert run(refiner(profile, local=False).refine({"u": item()}, NOW)) == {}


def test_disabled_without_router(profile):
    assert not Refiner(profile).enabled
    assert run(Refiner(profile).refine({"u": item()}, NOW)) == {}


@respx.mock
def test_time_budget_stops_further_batches(profile):
    respx.post(CLOUD).mock(side_effect=rank_answer())
    ref = Refiner(profile, router=make_router(local=False), time_budget=0)
    items = {f"u{i}": item(url=f"https://x.y/{i}") for i in range(30)}
    assert run(ref.refine(items, NOW)) == {}
    assert "time budget" in ref.failures[0] and "30 items" in ref.failures[0]


@respx.mock
def test_stops_early_when_every_provider_fails(profile):
    """When all providers fail on two batches in a row (quota/outage), later batches aren't attempted."""
    busy = respx.post(CLOUD).respond(503)
    items = {f"u{i}": item(url=f"https://x.y/{i}") for i in range(4 * BATCH_SIZE)}  # 4 batches
    ref = refiner(profile, local=False)
    assert run(ref.refine(items, NOW)) == {}
    assert busy.call_count == 2
    assert f"stopped early, {2 * BATCH_SIZE} items" in ref.failures[-1]


@respx.mock
def test_one_failed_batch_does_not_stop_the_run(profile):
    """A single overloaded moment (503) often clears within seconds (seen live)."""
    route = respx.post(CLOUD)
    route.side_effect = [httpx.Response(503), rank_answer(), rank_answer()]
    items = {f"u{i}": item(url=f"https://x.y/{i}") for i in range(3 * BATCH_SIZE)}  # 3 batches
    assert len(run(refiner(profile, local=False).refine(items, NOW))) == 2 * BATCH_SIZE


# ------------------------------------------------------------------ eligibility rules (RAD-2)


def _kawader(p):
    return {"open_to_nationalities": ["Qatar"], "evidence": "Qatari nationals only"} if "Nationals" in p["title"] else {}


@respx.mock
def test_rules_are_extracted_by_a_public_call_without_private_facts(profile):
    """RAD-2, RAD-4: the model only sees postings; the owner's facts never leave the machine."""
    assert profile.private_facts and profile.private_facts.citizenship
    route = respx.post(CLOUD).mock(side_effect=rank_answer(rules=_kawader))
    items = {"a": item(url="https://x.y/a", title="AI internship"),
             "b": item(url="https://x.y/b", title="Nationals only AI program")}
    out = run(refiner(profile).extract_rules(items))
    assert out["b"].open_to_nationalities == ["Qatar"] and not out["a"].restricts
    sent = json.dumps(messages(route.calls[0].request))
    assert not any(fact in sent for fact in profile.private_facts.citizenship + profile.private_facts.lives_in)


@respx.mock
def test_rules_run_in_batches_of_ten(profile):
    route = respx.post(CLOUD).mock(side_effect=rank_answer())
    items = {f"u{i}": item(url=f"https://x.y/{i}") for i in range(25)}
    assert len(run(refiner(profile).extract_rules(items))) == 25 and route.call_count == 3


@respx.mock
def test_rules_never_come_from_the_local_model(profile):
    """AI-16, RAD-2: a wrong exclusion hides an opportunity, so the weak local model is not used."""
    respx.post(CLOUD).respond(503)
    local = respx.post(LOCAL).mock(side_effect=rules_from_local(_kawader))
    assert run(refiner(profile).extract_rules({"b": item(title="Nationals only AI program")})) is None
    assert not local.called


@respx.mock
def test_rules_unavailable_returns_none(profile):
    """RAD-2: unavailable → None, so the caller keeps rank scores and checks again next run."""
    respx.post(CLOUD).respond(503)
    ref = refiner(profile, local=False)
    assert run(ref.extract_rules({"u": item()})) is None and ref.eligibility_failures


@respx.mock
def test_ranking_does_not_fall_back_to_the_local_model(profile):
    """AI-16: 20-item ranking batches are too slow locally; rule scores are kept instead."""
    respx.post(CLOUD).respond(503)
    local = respx.post(LOCAL).mock(side_effect=rules_from_local())
    assert run(refiner(profile).refine({"u": item()}, NOW)) == {} and not local.called


# ------------------------------------------------------------------ config

def write(tmp_path, text):
    p = tmp_path / "sources.yaml"
    p.write_text(text)
    return p


def test_example_configs_are_valid():
    """R-CFG-1: the example configs shipped in the repo load and validate."""
    sources = load_sources(CONFIG_EXAMPLES / "sources.example.yaml")
    assert len(sources) >= 5 and len({s.name for s in sources}) == len(sources)
    assert load_profile(CONFIG_EXAMPLES / "profile.example.yaml").min_score > 0


def test_private_configs_are_valid_if_present():
    """Your own (git-ignored) config/sources.yaml and profile.yaml, when they exist."""
    if not (CONFIG_EXAMPLES / "sources.yaml").exists():
        pytest.skip("no private config")
    assert load_sources() and load_profile()


def test_missing_config_points_to_example(tmp_path):
    (tmp_path / "sources.example.yaml").write_text("sources: []\n")
    with pytest.raises(ConfigError, match="copy sources.example.yaml to sources.yaml"):
        load_sources(tmp_path / "sources.yaml")


def test_bad_timezone_rejected(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text("timezone: Mars/Olympus\n")
    with pytest.raises(ConfigError, match="unknown timezone"):
        load_profile(p)


@pytest.mark.parametrize("yaml_text, message", [
    ("sources:\n  - {name: a, type: nope}\n", "unknown type 'nope'"),
    ("sources:\n  - {name: a, type: bevy}\n", "chapter_id"),
    ("sources:\n  - {name: a, type: bevy, chapter_id: 1, bogus: 2}\n", "bogus"),
    ("sources:\n  - {name: a, type: bevy, chapter_id: 1}\n  - {name: a, type: bevy, chapter_id: 2}\n", "duplicate"),
    ("nothing: here\n", "sources:"),
    ("sources: [\n", "invalid YAML"),
])
def test_config_errors_name_the_problem(tmp_path, yaml_text, message):
    """R-CFG-1"""
    with pytest.raises(ConfigError, match=message):
        load_sources(write(tmp_path, yaml_text))


def test_disabled_flag_parsed(tmp_path):
    """R-CFG-3"""
    (s,) = load_sources(write(tmp_path, "sources:\n  - {name: a, type: bevy, chapter_id: 1, enabled: false}\n"))
    assert s.enabled is False and s.params == {"chapter_id": 1}


def test_dotenv(tmp_path, monkeypatch):
    """R-CFG-2: .env is read; real environment wins."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "from-env")
    env = tmp_path / ".env"
    env.write_text("# comment\nTELEGRAM_BOT_TOKEN='abc'\nTELEGRAM_CHAT_ID=from-file\nEMPTY=\n")
    load_dotenv(env)
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "abc" and os.environ["TELEGRAM_CHAT_ID"] == "from-env"
    assert "EMPTY" not in os.environ


def test_quotas_must_fit_in_max_items(tmp_path):
    """R-REL-6: quotas larger than the digest starve the kinds ranked last (seen live: 19 > 15)."""
    p = tmp_path / "profile.yaml"
    p.write_text("max_items: 15\nquotas: {in_person: 8, jobs: 4, posts: 3, online: 4}\n")
    with pytest.raises(ConfigError, match="more than max_items"):
        load_profile(p)


