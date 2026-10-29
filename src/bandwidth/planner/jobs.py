"""Scheduled work, run every minute by the bot and every 30 minutes by launchd: fetch the feed,
send the daily message, deadline alerts and the weekly summary (SPEC SRC-1, SRC-5, MSG-*, ALR-*, WK-1)."""

from __future__ import annotations

import fcntl
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import httpx

from . import render
from .config import Settings, redact, secret
from .ics import Deadline, deadlines_from_feed
from .plan import MUSTS, REVIEW_DAYS, build_day, day_number, finals_countdown, week_start, week_view, weekday
from .store import Store
from .telegram import Telegram, TelegramError, keyboard

log = logging.getLogger(__name__)

FEED_EVERY = timedelta(hours=6)
ALERT_SLOTS = (("3d", timedelta(days=3)), ("1d", timedelta(days=1)), ("3h", timedelta(hours=3)))  # ALR-1


@contextmanager
def single_instance(path: Path) -> Iterator[bool]:
    """Yield True when this process holds the lock; False when another one does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@dataclass
class Ctx:
    s: Settings
    store: Store
    tg: Telegram | None  # None = dry run
    data_dir: Path
    http: httpx.Client | None = None

    @property
    def out_dir(self) -> Path:
        return self.data_dir / "out"  # images and dry-run output

    def local(self, now: datetime) -> datetime:
        return now.astimezone(self.s.tz)


# ---- feed -------------------------------------------------------------------------------------

def refresh_feed(ctx: Ctx, now: datetime, force: bool = False) -> bool:
    """Fetch the D2L feed at most every 6 hours. A failure keeps the last good data (SRC-5)."""
    url = secret("D2L_ICS_URL")
    if not url:
        return False
    last = ctx.store.kv_get("feed_tried")
    if not force and last and now - datetime.fromisoformat(last) < FEED_EVERY:
        return False
    ctx.store.kv_set("feed_tried", now.isoformat())
    try:
        client = ctx.http or httpx.Client(timeout=30, follow_redirects=True)
        resp = client.get(url)
        resp.raise_for_status()
        if "BEGIN:VCALENDAR" not in resp.text:
            raise ValueError("not a calendar (the link may have been reset)")
        items = deadlines_from_feed(resp.text, ctx.s.tz, ctx.s.school_location)
    except (httpx.HTTPError, ValueError) as e:
        ctx.store.kv_set("feed_error", redact(str(e))[:200])
        log.warning("d2l feed failed: %s", redact(str(e)))
        return False
    ctx.store.sync_d2l([d for d in items if d.due > now - timedelta(days=1)], now)  # SRC-4
    ctx.store.kv_set("feed_ok", now.isoformat())
    ctx.store.kv_set("feed_error", "")
    log.info("d2l feed: %d items", len(items))
    return True


def feed_note(ctx: Ctx, now: datetime) -> str | None:
    if not ctx.store.kv_get("feed_error"):
        return None
    ok = ctx.store.kv_get("feed_ok")
    when = render.now_label(datetime.fromisoformat(ok), ctx.s) if ok else "never"
    return f"- d2l feed failed - using data from {when}"


# ---- daily message ----------------------------------------------------------------------------

def due_soon(ctx: Ctx, day: date) -> list[Deadline]:
    start = datetime.combine(day, time(0), ctx.s.tz)
    return ctx.store.upcoming(start.astimezone(UTC), (start + timedelta(days=8)).astimezone(UTC), include_done=False)


def done_on(ctx: Ctx, day: date) -> set[str]:
    return ctx.store.musts_between(day, day).get(day.isoformat(), set())


def daily_caption(ctx: Ctx, day: date, now: datetime, notes: list[str] | None = None,
                  save: bool = False) -> tuple[str, dict]:
    plan = build_day(ctx.s, ctx.store, day, save=save)
    done = done_on(ctx, day)
    text = render.caption(ctx.s, plan, due_soon(ctx, day), done, day_number(ctx.s, day), notes)
    return text, render.must_buttons(day, done)


def daily_notes(ctx: Ctx, day: date, now: datetime) -> list[str]:
    notes = []
    y = day - timedelta(days=1)
    if y >= ctx.s.start_date:  # MSG-5
        notes.append(f"- yesterday - {len(done_on(ctx, y) & set(MUSTS))}/4 done")
    if n := feed_note(ctx, now):
        notes.append(n)
    return notes


def daily_image(ctx: Ctx, day: date) -> bytes | None:
    plan = build_day(ctx.s, ctx.store, day)
    page, height = render.image_html(ctx.s, plan, week_view(ctx.s, ctx.store, day), day_number(ctx.s, day))
    out = ctx.out_dir / f"{day.isoformat()}.png"
    try:
        return render.html_to_png(page, height, out).read_bytes()
    except render.RenderError as e:  # RUN-4: send the text alone
        log.warning("image not rendered: %s", e)
        return None


def send_daily(ctx: Ctx, now: datetime, force: bool = False) -> bool:
    """Once a day after send_at and before quiet hours (MSG-1, MSG-6). `force` resends now."""
    local = ctx.local(now)
    day = local.date()
    if not force:
        if ctx.store.kv_get("daily_sent") == day.isoformat():
            return False
        if not (ctx.s.send_at <= local.time() < ctx.s.quiet_from):
            return False
    notes = daily_notes(ctx, day, now)
    text, markup = daily_caption(ctx, day, now, notes, save=ctx.tg is not None)
    png = daily_image(ctx, day)
    if ctx.tg is None:
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        (ctx.out_dir / f"{day.isoformat()}.txt").write_text(text)
        log.info("dry run: daily message for %s written to %s", day, ctx.out_dir)
        return True
    msg_id = ctx.tg.send_photo(png, text, markup) if png else ctx.tg.send(text, markup)
    ctx.store.set_daily_msg(day, msg_id)
    ctx.store.kv_set(f"notes:{day.isoformat()}", "\n".join(notes))
    ctx.store.kv_set("daily_sent", day.isoformat())
    return True


def refresh_daily(ctx: Ctx, day: date, now: datetime, message_id: int, is_photo: bool) -> None:
    """Edit the day's message after a tap so the buttons and the count stay true."""
    notes = (ctx.store.kv_get(f"notes:{day.isoformat()}") or "").split("\n")
    text, markup = daily_caption(ctx, day, now, [n for n in notes if n])
    if ctx.tg is None:
        return
    if is_photo:
        ctx.tg.edit_caption(message_id, text, markup)
    else:
        ctx.tg.edit_text(message_id, text, markup)


def toggle_must(ctx: Ctx, day: date, task: str) -> bool:
    """Flip one must for that day; leetcode also marks that day's problems solved (PLAN-5)."""
    done = not ctx.store.must_done(day, task)
    ctx.store.set_must(day, task, done)
    if task == "leetcode":
        assigned = ctx.store.lc_day(day)
        if assigned:
            new, review = assigned
            for idx in new:
                if done:
                    ctx.store.solve(idx, day, REVIEW_DAYS)
                else:
                    ctx.store.unsolve(idx, day)
            if review is not None:
                ctx.store.review_done(review, day, done)
    return done


# ---- alerts -----------------------------------------------------------------------------------

def in_quiet(ctx: Ctx, now: datetime) -> bool:
    t = ctx.local(now).time()
    a, b = ctx.s.quiet_from, ctx.s.quiet_until
    return t >= a or t < b if a > b else a <= t < b


def left_label(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours >= 36:
        return f"{round(hours / 24)} days"
    if hours >= 20:
        return "1 day"
    return f"{max(1, round(hours))} hours" if hours >= 1.5 else f"{max(1, round(hours * 60))} min"


def alert_text(ctx: Ctx, d: Deadline, now: datetime) -> str:
    local = d.due.astimezone(ctx.s.tz)
    name = d.title if d.course in ("school", "other") else f"{d.course} {d.title}"
    return (f"<b>due in {left_label(d.due - now)}</b>\n- {render.e(name)}\n"
            f"- {weekday(local.date())} {local.day} {local:%b}".lower() + f" - {local:%H:%M}")


def alert_buttons(key: int) -> dict:
    return keyboard([("done", f"a:{key}:done"), ("snooze 3h", f"a:{key}:s3"), ("tomorrow 08:00", f"a:{key}:tm")])


def slot_time(ctx: Ctx, due: datetime, before: timedelta) -> datetime:
    """When an alert slot fires. A slot inside quiet hours moves earlier, to 1 hour before quiet
    hours start that evening, so "due in 1 day" for a 23:59 deadline arrives the evening before (ALR-4)."""
    t = due - before
    if not in_quiet(ctx, t):
        return t
    local = ctx.local(t)
    day = local.date() if local.time() >= ctx.s.quiet_from else local.date() - timedelta(days=1)
    return datetime.combine(day, ctx.s.quiet_from, ctx.s.tz) - timedelta(hours=1)


def send_alerts(ctx: Ctx, now: datetime) -> int:
    """3 days, 1 day and 3 hours before each open deadline; only the latest passed slot is sent,
    never twice, never in quiet hours (ALR-1..4). Snoozed items come back when the snooze ends."""
    if in_quiet(ctx, now):
        return 0
    sent = 0
    for d, until in ctx.store.snoozed():
        if until <= now and d.due > now:
            ctx.store.clear_snooze(d.uid)
            sent += _send_alert(ctx, d, now, f"snooze:{until.isoformat()}")
    for d in ctx.store.upcoming(now, now + ALERT_SLOTS[0][1] + timedelta(hours=12), include_done=False):
        if ctx.store.snoozed_until(d.uid):
            continue
        passed = [slot for slot, before in ALERT_SLOTS if slot_time(ctx, d.due, before) <= now]
        if not passed or ctx.store.alert_sent(d.uid, passed[-1]):
            continue
        sent += _send_alert(ctx, d, now, passed[-1])
        for slot in passed:  # earlier slots are now pointless
            ctx.store.mark_alert(d.uid, slot, now)
    return sent


def _send_alert(ctx: Ctx, d: Deadline, now: datetime, slot: str) -> int:
    if ctx.store.alert_sent(d.uid, slot):
        return 0
    if ctx.tg is not None:
        ctx.tg.send(alert_text(ctx, d, now), alert_buttons(ctx.store.key(d.uid)))
    ctx.store.mark_alert(d.uid, slot, now)
    return 1


def snooze_target(ctx: Ctx, choice: str, now: datetime) -> datetime:
    if choice == "s3":
        target = now + timedelta(hours=3)
        if in_quiet(ctx, target):  # land on the end of quiet hours instead
            local = ctx.local(target)
            day = local.date() + timedelta(days=local.time() >= ctx.s.quiet_from)
            target = datetime.combine(day, ctx.s.quiet_until, ctx.s.tz)
        return target
    local = ctx.local(now)
    return datetime.combine(local.date() + timedelta(days=1), time(8, 0), ctx.s.tz)


# ---- weekly summary ---------------------------------------------------------------------------

def weekly_stats(ctx: Ctx, today: date) -> str:
    start = max(week_start(today), ctx.s.start_date)
    end = week_start(today) + timedelta(days=6)
    days = [start + timedelta(days=i) for i in range((min(today, end) - start).days + 1)]
    musts = ctx.store.musts_between(start, end)
    plan_h = done_h = 0.0
    for d in days:
        p = build_day(ctx.s, ctx.store, d, detail=False)
        plan_h += p.focus_hours
        got = musts.get(d.isoformat(), set())
        done_h += sum(p.hours(t) for t in ("leetcode", "learn", "uni") if t in got)
    done_h += ctx.store.logged(start, end)
    a = datetime.combine(week_start(today), time(0), ctx.s.tz).astimezone(UTC)
    week_items = [d for d in ctx.store.upcoming(a, a + timedelta(days=7)) if d.course != "school"]
    return render.weekly_text(
        week_start(today), end, musts, len(days), ctx.store.solved_between(start, end), len(ctx.store.solved()),
        round(done_h, 1), round(plan_h, 1), sum(ctx.store.is_done(d.uid) for d in week_items), len(week_items),
        finals_countdown(ctx.s, today))


def send_weekly(ctx: Ctx, now: datetime) -> bool:
    local = ctx.local(now)
    today = local.date()
    if weekday(today) != ctx.s.weekly_day or local.time() < ctx.s.weekly_at or local.time() >= ctx.s.quiet_from:
        return False
    key = week_start(today).isoformat()
    if ctx.store.kv_get("weekly_sent") == key or today < ctx.s.start_date:
        return False
    text = weekly_stats(ctx, today)
    if ctx.tg is not None:
        ctx.tg.send(text)
    ctx.store.kv_set("weekly_sent", key)
    return True


# ---- one tick ---------------------------------------------------------------------------------

def tick(ctx: Ctx, now: datetime | None = None) -> None:
    """Everything that is due right now, under a lock so the bot and launchd never both send.
    Each part fails on its own without stopping the others."""
    now = now or datetime.now(UTC)
    with single_instance(ctx.data_dir / "tick.lock") as got:
        if not got:
            return
        for name, job in (("feed", lambda: refresh_feed(ctx, now)), ("daily", lambda: send_daily(ctx, now)),
                          ("alerts", lambda: send_alerts(ctx, now)), ("weekly", lambda: send_weekly(ctx, now))):
            try:
                job()
            except (TelegramError, httpx.HTTPError, OSError) as e:
                log.warning("%s failed: %s", name, redact(str(e)))
            except Exception:  # noqa: BLE001 - a bug in one job must not stop the bot
                log.exception("%s crashed", name)
