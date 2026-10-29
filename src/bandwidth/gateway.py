"""One Telegram bot for both tools (SPEC §6).

The gateway owns the polling loop and the owner check, routes each update to Radar or the planner,
and runs both tools' schedules. Each tool keeps its own handlers; buttons are namespaced by the
tools' Telegram clients (`rd:` / `pl:`) and the gateway strips the prefix before handing a tap on.

Radar is async and runs on the event loop. The planner is synchronous and its SQLite connection is
bound to the thread that opened it, so all planner work runs on one dedicated worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Protocol

from .buttons import prefix_markup

__all__ = ["HELP", "Gateway", "PlannerApp", "PlannerWorker", "prefix_markup"]

log = logging.getLogger(__name__)

URL = re.compile(r"https?://\S+")
PLANNER_COMMANDS = {"/today", "/list", "/calendar"}
RADAR_COMMANDS = {"/saved", "/upcoming", "/review", "/exclude", "/follow", "/unfollow", "/quota"}
APPLY_COMMANDS = {"/answer", "/applications", "/learn", "/signin", "/retry"}
OLD_BUTTON = "This button is from an older version."

TICK_EVERY = 60.0          # planner: daily message, alerts, feed, weekly summary (BOT-5)
DIGEST_CHECK_EVERY = 1800.0  # Radar: the once-a-day digest (R-SCHED-1)

HELP = """<b>What I do</b>
Every morning: your study plan and what is due. Through the day: new opportunities that fit you.

<b>Study</b>
- tap the buttons under the morning message when a must is done
- add a date: <code>history essay due nov 2</code>, <code>math midterm nov 5 10am</code>
- log extra work: <code>side project 2h</code>
/today - today's plan again
/list - deadlines in the next 14 days
/calendar - calendar file for your phone

<b>Opportunities</b>
- tap the buttons under each one: registered, remind me, not for me, can't apply
- send a link to keep it and get reminders
- write in plain words: <i>no more real estate events</i>, <i>what is saved?</i>
/saved - what you saved or registered for
/upcoming - what is coming up this week
/review - opportunities you haven't marked yet
/exclude &lt;word&gt; · /follow &lt;url&gt; · /unfollow &lt;url&gt; · /quota &lt;kind&gt; &lt;n&gt;

<b>Applying</b>
- ⚡ Fast apply: I fill the form on your Mac, write the essays and cover letter for that job, and
  send a preview. Nothing is sent until you tap Submit
- 🖐 Help me apply: I fill everything, never submit, and tell you how to raise your chances
- send any application link (not LinkedIn) to get both buttons
- the first 5 forms per site are dry runs: tell me if the values are right
/answer &lt;n&gt; &lt;answer&gt; - save an answer from the last checklist; I fill it next time
/applications - what each application is doing
/learn - after you fill questions in the window: I save your answers for next time
/signin &lt;url&gt; - sign in to a site (Devpost, Luma…) once in the agent's Chrome

/help - this message"""


class Sender(Protocol):
    async def send(self, chat_id: str, text: str, markup: dict | None = None, silent: bool = False) -> int: ...
    async def answer(self, callback_id: str, text: str = "") -> None: ...


class RadarHandlers(Protocol):
    async def on_button(self, cb: dict) -> None: ...
    async def on_message(self, msg: dict) -> None: ...
    async def send_due_reminders(self) -> int: ...


class PlannerApp:
    """The planner's bot and jobs, seen from the gateway. Lives on the planner's thread."""

    def __init__(self, ctx: Any):
        from bandwidth.planner.bot import Bot

        self.ctx = ctx
        self.bot = Bot(ctx)

    def on_button(self, q: dict, now: datetime) -> None:
        self.bot.on_button(q, now)

    def on_text(self, text: str, now: datetime) -> None:
        self.bot.on_text(text, now)

    def understands(self, text: str, now: datetime) -> bool:
        """True when the planner's own rules read this as a deadline or a work log (IN-1, IN-2)."""
        from bandwidth.planner.parse import parse_item, parse_log

        return bool(parse_log(text) or parse_item(text, self.ctx.s, now))

    def tick(self) -> None:
        from bandwidth.planner import jobs

        jobs.tick(self.ctx)


class PlannerWorker:
    """Runs every planner call on one thread; the app is created on that thread too (BOT-6)."""

    def __init__(self, factory: Callable[[], Any]):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="planner")
        self.app = self._pool.submit(factory).result()

    async def call(self, method: str, *args: Any) -> Any:
        fn = getattr(self.app, method)
        return await asyncio.get_running_loop().run_in_executor(self._pool, lambda: fn(*args))

    def close(self) -> None:
        self._pool.shutdown(wait=True)


class Gateway:
    def __init__(self, tg: Sender, owner_chat_id: str, radar: RadarHandlers, planner: PlannerWorker,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.tg, self.owner, self.radar, self.planner, self.now = tg, str(owner_chat_id), radar, planner, now
        self.radar_digest: Callable[[], Awaitable[None]] | None = None  # set when Radar is configured
        self.apply_command: Callable[[str, str], Awaitable[str]] | None = None  # /answer, /applications, /learn
        self.apply_button: Callable[[str], Awaitable[str]] | None = None  # taps on `ap:` buttons (FA-4, FA-5)
        self._last_tick: float | None = None
        self._last_digest_check: float | None = None
        self._digest_task: asyncio.Task | None = None
        self._polling = False  # while serving, the digest runs in the background

    # ---------------------------------------------------------------- updates

    async def handle(self, update: dict) -> None:
        """Route one update. A bug in a handler is logged and never stops the bot (BOT-7)."""
        try:
            if cb := update.get("callback_query"):
                chat = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
                if chat != self.owner or str((cb.get("from") or {}).get("id", "")) != self.owner:
                    log.warning("ignored a button tap from chat %s", chat)
                    return
                await self._on_button(cb)
            elif msg := update.get("message"):
                if str((msg.get("chat") or {}).get("id", "")) != self.owner:
                    log.warning("ignored a message from chat %s", (msg.get("chat") or {}).get("id"))
                    return
                await self._on_message(msg)
        except Exception:  # noqa: BLE001 - one bad update must not stop the bot
            log.exception("update %s failed", update.get("update_id"))

    async def _on_button(self, cb: dict) -> None:
        ns, sep, rest = (cb.get("data") or "").partition(":")
        if sep and ns == "rd":
            await self.radar.on_button({**cb, "data": rest})
        elif sep and ns == "pl":
            await self.planner.call("on_button", {**cb, "data": rest}, self.now())
        elif sep and ns == "ap":
            toast = ""
            try:
                toast = await self.apply_button(rest) if self.apply_button else "Applying is not set up on this Mac."
            finally:
                await self.tg.answer(cb["id"], toast[:190])
        else:
            await self.tg.answer(cb["id"], OLD_BUTTON)

    async def _on_message(self, msg: dict) -> None:
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text:
            return
        if text.startswith("/"):
            cmd = text.split()[0].lower().split("@")[0]
            if cmd in PLANNER_COMMANDS:
                await self.planner.call("on_text", text, self.now())
            elif cmd in RADAR_COMMANDS:
                await self.radar.on_message(msg)
            elif cmd in APPLY_COMMANDS:
                reply = (await self.apply_command(cmd, text.partition(" ")[2].strip()) if self.apply_command
                         else "Applying is not set up on this Mac.")
                await self.tg.send(self.owner, reply)
            else:
                await self.tg.send(self.owner, HELP)
            return
        if not URL.search(text) and await self.planner.call("understands", text, self.now()):
            await self.planner.call("on_text", text, self.now())
        else:
            await self.radar.on_message(msg)

    # ---------------------------------------------------------------- schedules (BOT-5)

    async def run_jobs(self, t: float) -> None:
        """Run whatever is due at monotonic time `t`. Each job fails on its own."""
        if self._last_tick is None or t - self._last_tick >= TICK_EVERY:
            self._last_tick = t
            await self._job("planner tick", self.planner.call("tick"))
        await self._job("radar reminders", self.radar.send_due_reminders())
        if self.radar_digest and (self._last_digest_check is None or t - self._last_digest_check >= DIGEST_CHECK_EVERY):
            self._last_digest_check = t
            if self._digest_task is None or self._digest_task.done():
                # A digest run takes minutes (it fetches every source): never block the polling for it.
                self._digest_task = asyncio.create_task(self._job("radar digest", self.radar_digest()))
                if not self._polling:
                    await self._digest_task

    async def _job(self, name: str, coro: Awaitable[Any]) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001 - a bug in one job must not stop the others
            log.exception("%s failed", name)

    # ---------------------------------------------------------------- polling

    async def serve(self, updates: Callable[[int | None, int], Awaitable[list[dict]]],
                    load_offset: Callable[[], int | None], save_offset: Callable[[int], None],
                    clock: Callable[[], float], pause: Callable[[float], Awaitable[None]],
                    stop: asyncio.Event | None = None) -> None:
        """Long-poll Telegram and run the schedules between polls, until `stop` is set."""
        self._polling = True
        offset, backoff = load_offset(), 5.0
        stop = stop or asyncio.Event()
        while not stop.is_set():
            await self.run_jobs(clock())
            try:
                batch = await updates(offset, 50)
                backoff = 5.0
            except Exception as e:  # noqa: BLE001 - offline or Telegram down: wait, then try again
                log.warning("polling failed: %s", e)
                await pause(backoff)
                backoff = min(backoff * 2, 300.0)
                continue
            for update in batch:
                offset = update["update_id"] + 1
                save_offset(offset)
                await self.handle(update)
        if self._digest_task and not self._digest_task.done():
            self._digest_task.cancel()
