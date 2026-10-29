from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_CONFIG = Path(__file__).parents[2] / "config" / "planner.example.toml"
QATAR = ZoneInfo("Asia/Qatar")

# Tests run "as of" Sun 25 Oct 2026, 04:30 Doha (01:30 UTC), before the daily send.
NOW = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def doha(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=QATAR)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Tests never see the owner's secrets, config or data."""
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "D2L_ICS_URL", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    from bandwidth.planner import config

    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copy(EXAMPLE_CONFIG, cfg / "planner.toml")
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "load_dotenv", lambda path=None: None)


@pytest.fixture
def settings():
    from bandwidth.planner.config import load_settings

    return load_settings(EXAMPLE_CONFIG)


class FakeTelegramServer:
    """Records Bot API calls and answers like Telegram. Use .client with planner.telegram.Telegram."""

    def __init__(self):
        import httpx

        self.calls: list[tuple[str, dict]] = []
        self.next_id = 100
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request):
        import json

        import httpx

        method = request.url.path.rsplit("/", 1)[-1]
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content or b"{}")
        else:
            body = {"_multipart": True, "raw": request.content}
        self.calls.append((method, body))
        self.next_id += 1
        result = {"message_id": self.next_id} if method.startswith(("send", "edit")) else True
        if method == "getUpdates":
            result = []
        return httpx.Response(200, json={"ok": True, "result": result})

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def texts(self) -> list[str]:
        return [b.get("text", "") for m, b in self.calls if m in ("sendMessage", "editMessageText")]


@pytest.fixture
def tg_server():
    return FakeTelegramServer()


@pytest.fixture
def ctx(settings, tg_server, tmp_path):
    from bandwidth.planner.jobs import Ctx
    from bandwidth.planner.store import Store
    from bandwidth.planner.telegram import Telegram

    c = Ctx(settings, Store(":memory:"), Telegram("123:SECRET", "424242", tg_server.client), tmp_path / "data")
    yield c
    c.store.close()


@pytest.fixture(autouse=True)
def no_chrome(monkeypatch):
    """Tests never start Chrome: rendering returns a tiny fake PNG."""
    from bandwidth.planner import render

    def fake(page, height, out, width=760, scale=2):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG" + b"0" * 2000)
        return out

    monkeypatch.setattr(render, "html_to_png", fake)
