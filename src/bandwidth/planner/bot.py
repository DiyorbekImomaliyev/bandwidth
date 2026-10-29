"""The Telegram bot: buttons, messages from its owner, and the polling loop (SPEC MSG-4, ALR-2..3, IN-1..4)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta


from . import jobs, render
from .ics import Deadline, to_ics
from .jobs import Ctx
from .parse import parse_item, parse_log
from .plan import MUSTS
from .telegram import TelegramError, keyboard

log = logging.getLogger(__name__)

HELP = """<b>study planner</b>

- tap the 4 buttons under the daily message when a must is done
- add a date: <code>history essay due nov 2</code>, <code>math midterm nov 5 10am</code>
- log extra work: <code>side project 2h</code>, <code>football 1.5h</code>

/today - send today's message again
/list - deadlines in the next 14 days
/calendar - calendar file for your phone
/help - this message"""


class Bot:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.tg = ctx.tg
        assert self.tg is not None

    def handle(self, update: dict, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if q := update.get("callback_query"):
            chat = ((q.get("message") or {}).get("chat") or {}).get("id")
            if str(q.get("from", {}).get("id")) != self.tg.chat_id or str(chat) != self.tg.chat_id:
                return  # IN-4: owner only
            self.on_button(q, now)
        elif msg := update.get("message"):
            if str((msg.get("chat") or {}).get("id")) != self.tg.chat_id:
                return
            if text := (msg.get("text") or "").strip():
                self.on_text(text, now)

    # ---- buttons ----
    def on_button(self, q: dict, now: datetime) -> None:
        data, msg = q.get("data", ""), q.get("message") or {}
        kind, _, rest = data.partition(":")
        note = ""
        try:
            if kind == "m":
                day_s, _, task = rest.partition(":")
                day = date.fromisoformat(day_s)
                if task in MUSTS:
                    done = jobs.toggle_must(self.ctx, day, task)
                    jobs.refresh_daily(self.ctx, day, now, msg["message_id"], "photo" in msg)
                    note = f"{task} - {'done' if done else 'not done'}"
            elif kind == "a":
                key, _, choice = rest.partition(":")
                note = self.on_alert(int(key), choice, msg["message_id"], now)
            elif kind == "p":
                note = self.on_pending(rest, msg["message_id"], now)
            elif kind == "r":
                uid = self.ctx.store.uid_for(int(rest))
                if uid and (d := self.ctx.store.deadline(uid)):
                    self.ctx.store.remove(uid)
                    note = f"removed - {d.title}"
        finally:
            try:
                self.tg.answer(q["id"], note)
            except TelegramError as e:
                log.warning("answer failed: %s", e)

    def on_alert(self, key: int, choice: str, message_id: int, now: datetime) -> str:
        uid = self.ctx.store.uid_for(key)
        d = self.ctx.store.deadline(uid) if uid else None
        if d is None:
            return "not found"
        name = d.title if d.course in ("school", "other") else f"{d.course} {d.title}"
        if choice == "done":
            self.ctx.store.set_done(uid)
            text = f"- {render.e(name)} - done - no more alerts"
        else:
            until = jobs.snooze_target(self.ctx, choice, now)
            self.ctx.store.snooze(uid, until)
            text = f"- {render.e(name)} - snoozed to {render.now_label(until, self.ctx.s)}"
        self.tg.edit_text(message_id, text)
        return text.removeprefix("- ")

    def on_pending(self, choice: str, message_id: int, now: datetime) -> str:
        item = self.ctx.store.pop_pending(message_id)
        if not item:
            return "expired"
        if choice == "save":
            due = datetime.fromisoformat(item["due"])
            d = Deadline(f"manual-{uuid.uuid4().hex[:12]}", item["course"], item["title"], item["kind"],
                         due.astimezone(UTC), "manual")
            self.ctx.store.add_manual(d)
            self.tg.edit_text(message_id, f"saved - {render.e(self.describe(d))}")
            return "saved"
        self.tg.edit_text(message_id, "cancelled")
        return "cancelled"

    def describe(self, d: Deadline) -> str:
        local = d.due.astimezone(self.ctx.s.tz)
        name = d.title if d.course in ("school", "other") else f"{d.course} {d.title}"
        return f"{name} - {render.day_title(local.date())} - {local:%H:%M}"

    # ---- text ----
    def on_text(self, text: str, now: datetime) -> None:
        cmd = text.split()[0].lower().split("@")[0]
        today = now.astimezone(self.ctx.s.tz).date()
        if cmd in ("/start", "/help"):
            self.tg.send(HELP)
        elif cmd == "/today":
            jobs.send_daily(self.ctx, now, force=True)
        elif cmd == "/list":
            self.send_list(now)
        elif cmd == "/calendar":
            self.send_calendar(now)
        elif got := parse_log(text):
            name, hours = got
            self.ctx.store.log(today, name, hours)
            self.tg.send(f"logged - {render.e(name)} {hours:g}h - counts as focus")
        elif item := parse_item(text, self.ctx.s, now):
            d = Deadline("", item.course, item.title, item.kind, item.due)
            msg_id = self.tg.send(f"add this?\n- {render.e(self.describe(d))}\n- alerts 3 days, 1 day, 3 hours before",
                                  keyboard([("save", "p:save"), ("cancel", "p:cancel")]))
            self.ctx.store.put_pending(msg_id, item.as_dict())
        else:
            self.tg.send("not understood. try <code>history essay due nov 2</code> or <code>side project 2h</code> - /help")

    def send_list(self, now: datetime) -> None:
        items = self.ctx.store.upcoming(now, now + timedelta(days=14), include_done=False)
        if not items:
            self.tg.send("nothing due in the next 14 days")
            return
        lines = ["<b>next 14 days</b>"] + [f"- {render.e(self.describe(d))}" for d in items]
        rows = [[(f"remove {d.title[:24]}", f"r:{self.ctx.store.key(d.uid)}")] for d in items if d.source == "manual"]
        self.tg.send("\n".join(lines), keyboard(*rows) if rows else None)

    def send_calendar(self, now: datetime) -> None:
        items = self.ctx.store.upcoming(now - timedelta(days=30), now + timedelta(days=365))
        exams = [(e.course, datetime.combine(e.day, e.start, self.ctx.s.tz), datetime.combine(e.day, e.end, self.ctx.s.tz),
                  e.room) for e in self.ctx.s.exams]
        self.tg.send_file("study-planner.ics", to_ics(items, exams, now).encode(),
                          "open this file to add your deadlines and exams to the calendar")
