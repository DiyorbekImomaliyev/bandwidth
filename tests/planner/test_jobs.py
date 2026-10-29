from datetime import UTC, datetime, timedelta

import httpx
from tests.planner.conftest import doha, fixture_text

from bandwidth.planner import jobs
from bandwidth.planner.ics import Deadline


def lab(ctx, due, uid="u1", title="lab 7 - stack"):
    ctx.store.sync_d2l([Deadline(uid, "comp2201", title, "lab", due.astimezone(UTC))], due - timedelta(days=5))


def test_daily_sent_once_after_send_at(ctx, tg_server):  # MSG-1, MSG-6
    assert not jobs.send_daily(ctx, doha(2026, 10, 25, 4, 59))
    assert jobs.send_daily(ctx, doha(2026, 10, 25, 5, 0))
    assert not jobs.send_daily(ctx, doha(2026, 10, 25, 9, 0))
    assert tg_server.methods() == ["sendPhoto"]
    assert ctx.store.daily_msg(doha(2026, 10, 25).date()) == 101
    assert ctx.store.lc_day(doha(2026, 10, 25).date()) == ([0, 1], None)  # problems fixed for the day


def test_daily_not_sent_in_quiet_hours(ctx, tg_server):
    assert not jobs.send_daily(ctx, doha(2026, 10, 25, 22, 30))
    assert tg_server.calls == []


def test_daily_falls_back_to_text_when_chrome_fails(ctx, tg_server, monkeypatch):  # RUN-4
    from bandwidth.planner import render

    def broken(*a, **k):
        raise render.RenderError("no chrome")

    monkeypatch.setattr(render, "html_to_png", broken)
    jobs.send_daily(ctx, doha(2026, 10, 25, 6))
    assert tg_server.methods() == ["sendMessage"]
    assert "<b>musts</b>" in tg_server.texts()[0]


def test_yesterday_line(ctx, tg_server):  # MSG-5
    sat = doha(2026, 10, 24).date()
    ctx.store.set_must(sat, "learn", True)
    ctx.store.set_must(sat, "uni", True)
    text, _ = jobs.daily_caption(ctx, sat + timedelta(days=1), doha(2026, 10, 25, 5),
                                 jobs.daily_notes(ctx, sat + timedelta(days=1), doha(2026, 10, 25, 5)))
    assert "- yesterday - 2/4 done" in text


def test_toggle_leetcode_solves_and_undoes(ctx):
    day = doha(2026, 10, 25).date()
    jobs.send_daily(ctx, doha(2026, 10, 25, 5))
    assert jobs.toggle_must(ctx, day, "leetcode") is True
    assert set(ctx.store.solved()) == {0, 1}
    assert jobs.toggle_must(ctx, day, "leetcode") is False
    assert ctx.store.solved() == {}


def test_alerts_three_slots_once_each(ctx, tg_server):  # ALR-1, ALR-3
    due = doha(2026, 10, 29, 18, 0)
    lab(ctx, due)
    assert jobs.send_alerts(ctx, due - timedelta(days=3, minutes=1)) == 0
    assert jobs.send_alerts(ctx, due - timedelta(days=3)) == 1
    assert jobs.send_alerts(ctx, due - timedelta(days=2)) == 0
    assert jobs.send_alerts(ctx, due - timedelta(days=1)) == 1
    assert jobs.send_alerts(ctx, due - timedelta(hours=3)) == 1
    assert jobs.send_alerts(ctx, due - timedelta(hours=1)) == 0
    texts = tg_server.texts()
    assert texts[0].startswith("<b>due in 3 days</b>") and "comp2201 lab 7 - stack" in texts[0]
    assert texts[2].startswith("<b>due in 3 hours</b>")


def test_late_item_gets_only_latest_slot(ctx, tg_server):
    due = doha(2026, 10, 26, 23, 59)
    lab(ctx, due)
    assert jobs.send_alerts(ctx, due - timedelta(hours=10)) == 1
    assert jobs.send_alerts(ctx, due - timedelta(hours=9)) == 0
    assert "due in 10 hours" in tg_server.texts()[0]


def test_done_item_gets_no_alerts(ctx, tg_server):
    due = doha(2026, 10, 29, 23, 59)
    lab(ctx, due)
    ctx.store.set_done("u1")
    assert jobs.send_alerts(ctx, due - timedelta(hours=3)) == 0


def test_23_59_deadline_alerts_come_the_evening_before(ctx, tg_server):  # ALR-4
    due = doha(2026, 10, 29, 23, 59)  # thu
    lab(ctx, due)
    assert jobs.send_alerts(ctx, doha(2026, 10, 26, 20, 59)) == 0
    assert jobs.send_alerts(ctx, doha(2026, 10, 26, 21, 0)) == 1  # mon 21:00: due in 3 days
    assert jobs.send_alerts(ctx, doha(2026, 10, 28, 21, 0)) == 1  # wed 21:00: due in 1 day
    assert jobs.send_alerts(ctx, doha(2026, 10, 29, 20, 59)) == 1  # thu 20:59: due in 3 hours
    assert [t.splitlines()[0] for t in tg_server.texts()] == [
        "<b>due in 3 days</b>", "<b>due in 1 day</b>", "<b>due in 3 hours</b>"]


def test_nothing_is_sent_in_quiet_hours(ctx):  # ALR-4
    due = doha(2026, 10, 26, 8, 0)
    lab(ctx, due)
    ctx.store.mark_alert("u1", "3d", due)
    ctx.store.mark_alert("u1", "1d", due)
    assert jobs.send_alerts(ctx, doha(2026, 10, 25, 23, 0)) == 0
    assert jobs.send_alerts(ctx, doha(2026, 10, 26, 5, 0)) == 1  # 3h slot is 05:00


def test_snooze_returns_later(ctx, tg_server):  # ALR-2
    due = doha(2026, 10, 29, 18, 0)
    lab(ctx, due)
    now = due - timedelta(days=1)
    jobs.send_alerts(ctx, now)
    ctx.store.snooze("u1", jobs.snooze_target(ctx, "s3", now))
    assert jobs.send_alerts(ctx, now + timedelta(hours=2)) == 0
    assert jobs.send_alerts(ctx, now + timedelta(hours=3)) == 1
    assert ctx.store.snoozed_until("u1") is None


def test_snooze_3h_skips_quiet_hours(ctx):
    target = jobs.snooze_target(ctx, "s3", doha(2026, 10, 25, 20, 30))
    assert target.astimezone(ctx.s.tz) == doha(2026, 10, 26, 5, 0)
    assert jobs.snooze_target(ctx, "tm", doha(2026, 10, 25, 20, 30)) == doha(2026, 10, 26, 8, 0)


def test_feed_refresh_and_failure_note(ctx, monkeypatch):  # SRC-1, SRC-5
    monkeypatch.setenv("D2L_ICS_URL", "https://d2l.example/feed.ics?token=TOPSECRET")
    feed = fixture_text("feed.ics")
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=feed)))
    ctx.http = ok
    now = datetime(2026, 10, 18, tzinfo=UTC)
    assert jobs.refresh_feed(ctx, now)
    assert not jobs.refresh_feed(ctx, now + timedelta(hours=1))  # at most every 6 hours
    assert len(ctx.store.upcoming(now, now + timedelta(days=30))) == 4
    ctx.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="down")))
    assert not jobs.refresh_feed(ctx, now + timedelta(hours=7))
    note = jobs.feed_note(ctx, now)
    assert note.startswith("- d2l feed failed - using data from 18 oct")
    assert "TOPSECRET" not in (ctx.store.kv_get("feed_error") or "")
    assert len(ctx.store.upcoming(now, now + timedelta(days=30))) == 4  # last good data kept


def test_feed_page_that_is_not_a_calendar_fails(ctx, monkeypatch):
    monkeypatch.setenv("D2L_ICS_URL", "https://d2l.example/feed.ics?token=x")
    ctx.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>login</html>")))
    assert not jobs.refresh_feed(ctx, datetime(2026, 10, 18, tzinfo=UTC))
    assert "not a calendar" in ctx.store.kv_get("feed_error")


def test_weekly_summary_once_on_saturday_evening(ctx, tg_server):  # WK-1
    sun = doha(2026, 10, 25).date()
    for i, task in enumerate(["leetcode", "learn", "uni", "push"]):
        ctx.store.set_must(sun + timedelta(days=i), task, True)
    ctx.store.log(sun, "capstone", 2)
    assert not jobs.send_weekly(ctx, doha(2026, 10, 31, 19, 59))
    assert jobs.send_weekly(ctx, doha(2026, 10, 31, 20, 0))
    assert not jobs.send_weekly(ctx, doha(2026, 10, 31, 21, 0))
    text = tg_server.texts()[0]
    assert text.startswith("<b>week 25 - 31 oct</b>")
    assert "- leetcode - 1/7 days" in text and "- focus - " in text and "of 62.5h" in text


def test_tick_lock_stops_double_send(ctx, tg_server):
    with jobs.single_instance(ctx.data_dir / "tick.lock") as got:
        assert got
        jobs.tick(ctx, doha(2026, 10, 25, 6))
    assert tg_server.calls == []
    jobs.tick(ctx, doha(2026, 10, 25, 6))
    assert tg_server.methods() == ["sendPhoto"]
