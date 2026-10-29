"""A real AI router pointed at mocked endpoints: a cloud provider and the local model."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from bandwidth.ai import Provider, Router

CLOUD = "https://cloud.test/v1/chat/completions"
LOCAL = "http://localhost:11434/api/chat"


def make_router(*, local: bool = True, private_terms: list[str] | None = None) -> Router:
    providers = [Provider("cloud", "openai", "https://cloud.test/v1", "cloud-model")]
    if local:
        providers.append(Provider("local", "ollama", "http://localhost:11434", "qwen3:8b"))
    return Router(providers, private_terms=private_terms or [])


def messages(request: httpx.Request) -> list[dict]:
    return json.loads(request.content)["messages"]


def data_items(request: httpx.Request) -> list[dict]:
    """The items the router sent in the <data> block of the last user message."""
    user = messages(request)[-1]["content"]
    return json.loads(user.split("<data>\n", 1)[1].rsplit("\n</data>", 1)[0])


def _openai(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)},
                                                  "finish_reason": "stop"}]})


def _ollama(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"message": {"content": json.dumps(payload)}, "done": True})


def is_rules_request(request: httpx.Request) -> bool:
    return "report who is allowed to apply" in messages(request)[0]["content"]


def no_rules(p: dict) -> dict:
    return {}


def rank_answer(score: float = 7, relevant: bool = True,
                rules: Callable[[dict], dict] = no_rules) -> Callable[[httpx.Request], httpx.Response]:
    """A cloud reply: verdicts for ranking requests, `rules(item)` for rule-extraction requests."""
    def answer(request: httpx.Request) -> httpx.Response:
        items = data_items(request)
        if is_rules_request(request):
            return _openai({"results": [{"id": p["id"], **rules(p)} for p in items]})
        return _openai({"verdicts": [{"id": p["id"], "relevant": relevant, "score": score,
                                      "about": f"AI: {p['title']}", "deadline": None} for p in items]})
    return answer


def rules_from_local(rules: Callable[[dict], dict] = no_rules) -> Callable[[httpx.Request], httpx.Response]:
    """A local-model reply to rule-extraction requests (the fallback when the cloud is down)."""
    def answer(request: httpx.Request) -> httpx.Response:
        return _ollama({"results": [{"id": p["id"], **rules(p)} for p in data_items(request)]})
    return answer


def chat_answer(**action) -> httpx.Response:
    return _ollama({"value": None, "number": None, "reply": "", **action})
