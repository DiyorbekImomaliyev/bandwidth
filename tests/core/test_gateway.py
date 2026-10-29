"""One bot for both tools: routing, namespaced buttons, owner-only, schedules (SPEC §6)."""

from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest
import respx

from bandwidth.gateway import HELP, Gateway, PlannerWorker, prefix_markup

OWNER = "4242"


def run(coro):
    return asyncio.run(coro)


class FakeTG:
    def __init__(self):
        self.sent: list[str] = []
        self.answers: list[tuple[str, str]] = []

    async def send(self, chat_id, text, markup=None, silent=False):
        self.sent.append(text)
        return len(self.sent)

    async def answer(self, callback_id, text=""):
        self.answers.append((callback_id, text))


class FakeRadar:
    def __init__(self):
        self.buttons: list[str] = []
        self.messages: list[str] = []
        self.reminders = 0

    async def on_button(self, cb):
        self.buttons.append(cb["data"])

    async def on_message(self, msg):
        self.messages.append(msg["text"])

    async def send_due_reminders(self):
        self.reminders += 1
        return 0


class FakePlanner:
    """Records calls and the thread they ran on, like the real (thread-bound) planner."""

    def __init__(self):
        self.buttons: list[str] = []
        self.texts: list[str] = []
        self.threads: set[str] = set()
        self.ticks = 0

    def _seen(self):
        self.threads.add(threading.current_thread().name)

    def on_button(self, q, now):
        self._seen()
        self.buttons.append(q["data"])

    def on_text(self, text, now):
        self._seen()
        self.texts.append(text)

    def understands(self, text, now):
        self._seen()
        return text.startswith("hist") or text.endswith("2h")

    def tick(self):
        self._seen()
        self.ticks += 1


@pytest.fixture
def gw():
    tg, radar, planner = FakeTG(), FakeRadar(), FakePlanner()
    worker = PlannerWorker(lambda: planner)
    g = Gateway(tg, OWNER, radar, worker)
    yield g, tg, radar, planner
    worker.close()


def msg(text, chat=OWNER):
    return {"update_id": 1, "message": {"message_id": 5, "chat": {"id": int(chat)}, "from": {"id": int(chat)},
                                        "text": text}}


def tap(data, chat=OWNER, user=OWNER):
    return {"update_id": 2, "callback_query": {"id": "cb1", "data": data, "from": {"id": int(user)},
                                               "message": {"message_id": 9, "chat": {"id": int(chat)}}}}


# ---------------------------------------------------------------- owner only (BOT-1)


def test_strangers_are_ignored(gw):
    """BOT-1"""
    g, tg, radar, planner = gw
    run(g.handle(msg("/today", chat="999")))
    run(g.handle(tap("rd:m:r:abc", chat="999", user="999")))
    run(g.handle(tap("rd:m:r:abc", chat=OWNER, user="999")))  # someone else tapping in a shared chat
    assert not (tg.sent or radar.buttons or planner.texts or radar.messages)


# ---------------------------------------------------------------- buttons (BOT-2)


def test_taps_go_to_the_tool_that_sent_the_button_without_its_prefix(gw):
    """BOT-2"""
    g, tg, radar, planner = gw
    run(g.handle(tap("rd:m:r:abc")))
    run(g.handle(tap("pl:m:2026-10-28:leetcode")))
    assert radar.buttons == ["m:r:abc"] and planner.buttons == ["m:2026-10-28:leetcode"]


def test_apply_taps_go_to_the_apply_module(gw):
    """BOT-2, FA-5"""
    g, tg, radar, planner = gw
    run(g.handle(tap("ap:s:1:abc")))
    assert "not set up" in tg.answers[-1][1]
    got = []

    async def on_button(data):
        got.append(data)
        return "📨 Checking"

    g.apply_button = on_button
    run(g.handle(tap("ap:s:1:abc")))
    assert got == ["s:1:abc"] and tg.answers[-1] == ("cb1", "📨 Checking") and not radar.buttons


def test_unknown_prefix_is_answered_not_dropped(gw):
    """BOT-2: old buttons from before the merge get a short answer instead of a spinner."""
    g, tg, radar, planner = gw
    run(g.handle(tap("m:r:abc")))
    assert tg.answers == [("cb1", "This button is from an older version.")]
    assert not radar.buttons and not planner.buttons


def test_prefix_markup_adds_the_prefix_once():
    """BOT-2"""
    markup = {"inline_keyboard": [[{"text": "a", "callback_data": "m:x"}, {"text": "b", "url": "https://x"}]]}
    once = prefix_markup(markup, "rd:")
    assert once["inline_keyboard"][0][0]["callback_data"] == "rd:m:x"
    assert once["inline_keyboard"][0][1] == {"text": "b", "url": "https://x"}
    assert prefix_markup(once, "rd:") == once
    assert markup["inline_keyboard"][0][0]["callback_data"] == "m:x"  # input untouched


@respx.mock
def test_radar_client_prefixes_its_buttons():
    """BOT-2: done in the tool's own Telegram client, so its handlers don't change."""
    from bandwidth.radar.http import Fetcher
    from bandwidth.radar.telegram import Telegram, keyboard

    route = respx.post(url__regex=r"https://api\.telegram\.org/bot.+/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 1}})

    async def go():
        async with Fetcher() as fx:
            await Telegram(fx, token="T0K3N").send("1", "hi", keyboard([("ok", "m:r:abc")]))

    run(go())
    body = json.loads(route.calls[0].request.content)
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "rd:m:r:abc"


def test_planner_client_prefixes_its_buttons():
    """BOT-2"""
    from bandwidth.planner.telegram import Telegram, keyboard

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["content-type"].startswith("multipart"):
            seen.append(request.content.decode(errors="replace"))
        else:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    tg = Telegram("T0K3N", "1", client=httpx.Client(transport=httpx.MockTransport(handler)))
    tg.send("hi", keyboard([("leetcode", "m:2026-10-28:leetcode")]))
    tg.send_photo(b"png", "caption", keyboard([("push", "m:2026-10-28:push")]))
    assert seen[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "pl:m:2026-10-28:leetcode"
    assert "pl:m:2026-10-28:push" in seen[1]


def test_keyboards_leave_room_for_the_prefix():
    """BOT-2: Telegram allows 64 bytes; the prefix takes 3."""
    from bandwidth.radar.telegram import keyboard as rk
    from bandwidth.planner.telegram import keyboard as pk

    for kb in (rk, pk):
        kb([("x", "a" * 61)])
        with pytest.raises(ValueError):
            kb([("x", "a" * 62)])


# ---------------------------------------------------------------- commands and text (BOT-3, BOT-4)


@pytest.mark.parametrize("text,tool", [
    ("/today", "planner"), ("/list", "planner"), ("/calendar", "planner"), ("/today@my_bot", "planner"),
    ("/saved", "radar"), ("/upcoming", "radar"), ("/review", "radar"), ("/exclude crypto", "radar"),
    ("/follow https://linkedin.com/company/x", "radar"), ("/quota jobs 3", "radar"),
])
def test_commands_go_to_their_tool(gw, text, tool):
    """BOT-3"""
    g, tg, radar, planner = gw
    run(g.handle(msg(text)))
    assert (planner.texts if tool == "planner" else radar.messages) == [text]


@pytest.mark.parametrize("text", ["/start", "/help", "/nonsense"])
def test_help_is_merged(gw, text):
    """BOT-3"""
    g, tg, radar, planner = gw
    run(g.handle(msg(text)))
    assert tg.sent == [HELP] and not radar.messages and not planner.texts
    assert "/today" in HELP and "/saved" in HELP


def test_answer_goes_to_applying(gw):
    """VLT-4"""
    g, tg, radar, planner = gw
    run(g.handle(msg("/answer 3 No")))
    assert "not set up" in tg.sent[-1]
    got = []

    async def command(cmd, arg):
        got.append((cmd, arg))
        return "Saved"

    g.apply_command = command
    run(g.handle(msg("/answer 3 No")))
    assert got == [("/answer", "3 No")] and tg.sent[-1] == "Saved" and not radar.messages and not planner.texts
    run(g.handle(msg("/applications")))
    run(g.handle(msg("/learn")))
    assert got[1:] == [("/applications", ""), ("/learn", "")]


def test_text_the_planner_understands_goes_to_the_planner(gw):
    """BOT-4: deadlines and work logs first."""
    g, tg, radar, planner = gw
    run(g.handle(msg("hist essay due nov 2")))
    run(g.handle(msg("capstone 2h")))
    assert planner.texts == ["hist essay due nov 2", "capstone 2h"] and not radar.messages


def test_links_and_other_text_go_to_radar(gw):
    """BOT-4"""
    g, tg, radar, planner = gw
    run(g.handle(msg("look https://example.com/event")))
    run(g.handle(msg("no more real estate events")))
    assert radar.messages == ["look https://example.com/event", "no more real estate events"]
    assert not planner.texts


def test_planner_work_always_runs_on_one_dedicated_thread(gw):
    """BOT-6: the planner's SQLite connection belongs to the thread that opened it."""
    g, tg, radar, planner = gw
    run(g.handle(msg("/today")))
    run(g.handle(tap("pl:m:2026-10-28:learn")))
    run(g.run_jobs(0.0))
    assert len(planner.threads) == 1
    assert planner.threads != {threading.main_thread().name}


def test_a_failing_handler_does_not_stop_the_bot(gw):
    """BOT-7"""
    g, tg, radar, planner = gw

    async def boom(cb):
        raise RuntimeError("bug")

    radar.on_button = boom
    run(g.handle(tap("rd:m:r:abc")))  # logged, not raised
    run(g.handle(msg("/saved")))
    assert radar.messages == ["/saved"]


# ---------------------------------------------------------------- schedules (BOT-5)


def test_jobs_run_on_their_own_intervals(gw):
    """BOT-5: planner tick every 60 s, reminders every pass, digest check every 30 min."""
    g, tg, radar, planner = gw
    checks = []

    async def digest():
        checks.append(1)

    g.radar_digest = digest
    run(g.run_jobs(0.0))
    run(g.run_jobs(30.0))
    run(g.run_jobs(61.0))
    run(g.run_jobs(1801.0))
    assert planner.ticks == 3 and radar.reminders == 4 and len(checks) == 2


def test_one_failing_job_does_not_stop_the_others(gw):
    """BOT-5"""
    g, tg, radar, planner = gw

    def bad_tick():
        raise RuntimeError("render crashed")

    planner.tick = bad_tick
    run(g.run_jobs(0.0))
    assert radar.reminders == 1


# ---------------------------------------------------------------- polling (BOT-7)


def test_serve_handles_updates_remembers_the_offset_and_backs_off(gw):
    """BOT-7: offline → wait (5 s, then 10 s…); every handled update moves the saved offset."""
    g, tg, radar, planner = gw
    stop = asyncio.Event()
    saved, pauses = [], []
    batches = [ConnectionError("offline"), ConnectionError("offline"),
               [msg("/saved") | {"update_id": 7}, msg("/upcoming") | {"update_id": 8}]]

    async def updates(offset, timeout):
        if not batches:
            stop.set()
            return []
        b = batches.pop(0)
        if isinstance(b, Exception):
            raise b
        return b

    async def pause(s):
        pauses.append(s)

    run(g.serve(updates, lambda: 3, saved.append, clock=lambda: 0.0, pause=pause, stop=stop))
    assert pauses == [5.0, 10.0]
    assert saved == [8, 9] and radar.messages == ["/saved", "/upcoming"]
