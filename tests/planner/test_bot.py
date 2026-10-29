from datetime import UTC, timedelta

from tests.planner.conftest import doha

from bandwidth.planner import jobs
from bandwidth.planner.bot import Bot
from bandwidth.planner.ics import Deadline

OWNER = 424242
NOW = doha(2026, 10, 25, 9)


def button(data, message_id=101, photo=True, user=OWNER):
    msg = {"message_id": message_id, "chat": {"id": user}}
    if photo:
        msg["photo"] = [{}]
    return {"update_id": 1, "callback_query": {"id": "cb", "from": {"id": user}, "data": data, "message": msg}}


def text(t, user=OWNER):
    return {"update_id": 1, "message": {"chat": {"id": user}, "text": t}}


def test_strangers_are_ignored(ctx, tg_server):  # IN-4
    bot = Bot(ctx)
    bot.handle(text("/help", user=1), NOW)
    bot.handle(button("m:2026-10-25:learn", user=1), NOW)
    assert tg_server.calls == []


def test_must_button_toggles_and_edits_caption(ctx, tg_server):  # MSG-4
    jobs.send_daily(ctx, doha(2026, 10, 25, 5))
    Bot(ctx).handle(button("m:2026-10-25:learn"), NOW)
    method, body = tg_server.calls[-2]
    assert method == "editMessageCaption"
    assert "1/4 done" in body["caption"]
    assert body["reply_markup"]["inline_keyboard"][0][1]["text"] == "✓ learn"
    assert tg_server.calls[-1][0] == "answerCallbackQuery"


def test_alert_done_and_snooze(ctx, tg_server):  # ALR-2, ALR-3
    due = doha(2026, 10, 26, 23, 59)
    ctx.store.sync_d2l([Deadline("u1", "comp2201", "lab 7", "lab", due.astimezone(UTC))], NOW)
    key = ctx.store.key("u1")
    Bot(ctx).handle(button(f"a:{key}:s3", photo=False), NOW)
    assert ctx.store.snoozed_until("u1") == NOW + timedelta(hours=3)
    Bot(ctx).handle(button(f"a:{key}:done", photo=False), NOW)
    assert ctx.store.is_done("u1") and ctx.store.snoozed_until("u1") is None
    assert "done - no more alerts" in tg_server.texts()[-1]


def test_add_item_needs_save(ctx, tg_server):  # IN-1
    bot = Bot(ctx)
    bot.handle(text("hist essay due nov 2"), NOW)
    assert "add this?\n- hist1001 essay - mon 2 nov - 23:59" in tg_server.texts()[0]
    assert ctx.store.upcoming(NOW, NOW + timedelta(days=30)) == []
    bot.handle(button("p:save", message_id=101, photo=False), NOW)
    items = ctx.store.upcoming(NOW, NOW + timedelta(days=30))
    assert [(d.course, d.title, d.source) for d in items] == [("hist1001", "essay", "manual")]


def test_cancel_item(ctx, tg_server):
    bot = Bot(ctx)
    bot.handle(text("os midterm nov 5 10am"), NOW)
    bot.handle(button("p:cancel", message_id=101, photo=False), NOW)
    assert ctx.store.upcoming(NOW, NOW + timedelta(days=30)) == []


def test_log_counts_as_focus(ctx, tg_server):  # IN-2
    Bot(ctx).handle(text("capstone 2h"), NOW)
    assert ctx.store.logged(NOW.date(), NOW.date()) == 2
    assert tg_server.texts() == ["logged - capstone 2h - counts as focus"]


def test_list_and_remove_manual(ctx, tg_server):  # IN-3
    bot = Bot(ctx)
    bot.handle(text("hist essay due nov 2"), NOW)
    bot.handle(button("p:save", message_id=101, photo=False), NOW)
    bot.handle(text("/list"), NOW)
    body = [b for m, b in tg_server.calls if m == "sendMessage"][-1]
    data = body["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert data.startswith("pl:")  # the shared bot strips this before the planner sees the tap (BOT-2)
    bot.handle(button(data.removeprefix("pl:"), photo=False), NOW)
    assert ctx.store.upcoming(NOW, NOW + timedelta(days=30)) == []


def test_calendar_file(ctx, tg_server):  # ICS-2
    Bot(ctx).handle(text("/calendar"), NOW)
    method, body = tg_server.calls[-1]
    assert method == "sendDocument" and b"comp3104 final exam" in body["raw"]


def test_unknown_text_gets_help_hint(ctx, tg_server):
    Bot(ctx).handle(text("hello"), NOW)
    assert "not understood" in tg_server.texts()[0]
