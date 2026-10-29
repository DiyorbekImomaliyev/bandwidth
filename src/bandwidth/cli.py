"""Command line: bw bot | doctor | ai-stats | migrate | radar … | planner … (SPEC §7)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sqlite3
import stat
import sys
import time
from datetime import UTC, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any

from .paths import AGENT_DB, CONFIG_DIR, DATA_DIR, ROOT

log = logging.getLogger("bandwidth")


# ---------------------------------------------------------------- logging (RUN-5)


class _Json(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        from bandwidth.radar.config import redact

        out = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "level": record.levelname,
               "logger": record.name, "msg": redact(record.getMessage())}
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False)


def setup_logging(verbose: bool = False) -> None:
    logs = DATA_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    file = logging.handlers.RotatingFileHandler(logs / "bw.log", maxBytes=1_000_000, backupCount=5)
    file.setFormatter(_Json())
    err = logging.StreamHandler()
    err.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [file, err]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for noisy in ("httpx", "httpcore"):  # their request lines contain the bot token
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------- agent kv (polling offset)


class AgentKV:
    def __init__(self, path: Path = AGENT_DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.db.commit()


# ---------------------------------------------------------------- bot (BOT-1..7)


async def radar_digest(router) -> None:
    """Radar's once-a-day digest (R-SCHED-1), on its own database connection like the old process."""
    from bandwidth.radar import config as rconfig
    from bandwidth.radar.cli import online, should_run, single_instance
    from bandwidth.radar.config import load_profile, load_sources
    from bandwidth.radar.llm import Refiner
    from bandwidth.radar.pipeline import owner_examples, run
    from bandwidth.radar.store import Store

    with single_instance(rconfig.DATA_DIR / "run.lock") as got:
        if not got:
            return
        is_online = await asyncio.to_thread(online)  # a blocking probe: keep it off the event loop
        store = Store(rconfig.DATA_DIR / "radar.db")
        try:
            due, why = should_run(store, datetime.now(UTC), dtime(7, 0), is_online=lambda: is_online)
            if not due:
                log.info("radar digest not due: %s", why)
                return
            profile = load_profile()
            result = await run(sources=load_sources(), profile=profile, store=store, scheduled=True,
                               refiner=Refiner(profile, router=router, examples=owner_examples(store)))
            log.info("radar digest: %d items, delivered=%s", result.digest.item_count, result.delivered)
        finally:
            store.close()


def setup_applying(radar: Any, gw: Any, tg: Any, chat_id: str, router: Any) -> Any:
    """Wire Partial help into the bot once the vault exists (JOB-4, SPEC §10). Returns the browser
    (Chrome starts on the first tap) or None when applying is not set up."""
    from .apply import applyable_platforms
    from .apply.applications import Applications
    from .apply.browser import Browser
    from .apply.service import ApplyService
    from .apply.vault import VAULT_DIR

    platforms = applyable_platforms()
    if not platforms or router is None:
        log.info("applying off: %s", "no vault/" if not platforms else "no config/ai.yaml")
        return None
    from bandwidth.radar.telegram import Telegram

    from .apply.settings import load_settings

    browser = Browser(DATA_DIR / "browser")
    atg = Telegram(tg.fx)
    atg.callback_prefix = "ap:"  # apply's own buttons: the gateway hands them to the service (BOT-2)
    service = ApplyService(vault_dir=VAULT_DIR, router=router, apps=Applications(AGENT_DB), browser=browser,
                           send=lambda text, markup=None: atg.send(chat_id, text, markup),
                           audit_dir=DATA_DIR / "applications", settings=load_settings())
    busy = asyncio.Lock()
    tasks: set[asyncio.Task] = set()

    filling: list[str] = []

    async def one_at_a_time(url: str, mode: str) -> None:
        async with busy:
            filling.append(url)
            try:
                await service.help(url, mode)
            except Exception:  # noqa: BLE001 - logged; the owner sees it in /applications
                log.exception("applying to %s failed", url)
            finally:
                filling.clear()

    async def on_apply(url: str, mode: str) -> str:
        if busy.locked():
            return "⏳ Still filling the last form. Tap again in a minute."
        task = asyncio.create_task(one_at_a_time(url, mode))  # filling takes a minute: never block the polling
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return ("⚡ Filling the form on your Mac. The preview follows in a minute or two." if mode == "fast"
                else "🖐 Filling everything on your Mac. Your checklist and fit report follow in a minute or two.")

    radar.on_apply, radar.apply_platforms = on_apply, platforms
    gw.apply_button = service.on_button
    gw.apply_command = lambda cmd, arg: service.command(cmd, arg, filling[0] if filling else None)
    log.info("applying on for %s", ", ".join(platforms))
    return browser


async def serve_bot() -> int:
    from bandwidth.radar import config as rconfig
    from bandwidth.radar.bot import Bot as RadarBot
    from bandwidth.radar.config import load_profile, secret
    from bandwidth.radar.http import Fetcher
    from bandwidth.radar.pipeline import default_router
    from bandwidth.radar.store import Store
    from bandwidth.radar.telegram import Telegram
    from bandwidth.radar.textutil import set_local_tz
    from bandwidth.planner.cli import make_ctx

    from .gateway import Gateway, PlannerApp, PlannerWorker

    chat_id = secret("TELEGRAM_CHAT_ID")
    if not secret("TELEGRAM_BOT_TOKEN") or not chat_id:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env (see `bw doctor`)", file=sys.stderr)
        return 2
    profile = load_profile()
    set_local_tz(profile.timezone)
    router = default_router()
    store = Store(rconfig.DATA_DIR / "radar.db")
    kv = AgentKV()
    planner = PlannerWorker(lambda: PlannerApp(make_ctx(dry_run=False)))
    stop = asyncio.Event()
    browser = None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        async with Fetcher() as fx:
            tg = Telegram(fx)
            radar = RadarBot(store, profile, tg, chat_id, fx, router=router)
            gw = Gateway(tg, chat_id, radar, planner)
            gw.radar_digest = lambda: radar_digest(router)
            browser = setup_applying(radar, gw, tg, chat_id, router)
            log.info("bot started; AI router %s", "on" if router else "off (no config/ai.yaml)")

            async def updates(offset, timeout):
                poll = asyncio.ensure_future(tg.updates(offset, timeout))
                stopped = asyncio.ensure_future(stop.wait())
                done, _ = await asyncio.wait({poll, stopped}, return_when=asyncio.FIRST_COMPLETED)
                if poll in done:
                    stopped.cancel()
                    return poll.result()
                poll.cancel()  # shutting down: don't wait 50 s for the long poll
                return []

            async def pause(seconds: float) -> None:
                """Wait before retrying, but wake up at once on shutdown."""
                try:
                    await asyncio.wait_for(stop.wait(), seconds)
                except TimeoutError:
                    pass

            await gw.serve(updates=updates, load_offset=lambda: int(kv.get("tg_offset") or 0) or None,
                           save_offset=lambda o: kv.set("tg_offset", str(o)), clock=time.monotonic,
                           pause=pause, stop=stop)
    finally:
        if browser:
            await browser.close()
        planner.close()
        store.close()
        log.info("bot stopped")
    return 0


def cmd_bot(_args: argparse.Namespace) -> int:
    from bandwidth.radar.cli import single_instance

    setup_logging(_args.verbose)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with single_instance(DATA_DIR / "bot.lock") as got:
        if not got:
            print("the bot is already running", file=sys.stderr)
            return 0
        return asyncio.run(serve_bot())


# ---------------------------------------------------------------- doctor (RUN-3)


def cmd_doctor(_args: argparse.Namespace) -> int:
    from bandwidth.radar.config import secret

    problems = 0

    def report(ok: bool | None, what: str, detail: str = "") -> None:
        nonlocal problems
        mark = {True: "ok  ", False: "FAIL", None: "skip"}[ok]
        problems += ok is False
        print(f"[{mark}] {what}{' - ' + detail if detail else ''}")

    env = ROOT / ".env"
    if env.exists():
        mode = stat.S_IMODE(env.stat().st_mode)
        report(mode & 0o077 == 0, ".env is private", f"mode {mode:o}" + ("" if mode & 0o077 == 0 else "; run chmod 600 .env"))
    else:
        report(False, ".env", "missing; copy .env.example")

    # configs
    try:
        from bandwidth.radar.config import load_profile, load_sources
        profile, sources = load_profile(), load_sources()
        report(True, "radar config", f"{sum(s.enabled for s in sources)} sources enabled")
    except Exception as e:  # noqa: BLE001
        profile = None
        report(False, "radar config", str(e))
    try:
        from bandwidth.planner.config import load_settings
        load_settings()
        report(True, "planner config")
    except Exception as e:  # noqa: BLE001
        report(False, "planner config", str(e))

    # telegram
    token, chat = secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID")
    if not token:
        report(False, "telegram bot token", "TELEGRAM_BOT_TOKEN is empty; create a bot with @BotFather")
    else:
        import httpx
        try:
            me = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
            report(bool(me.get("ok")), "telegram bot", f"@{me['result']['username']}" if me.get("ok") else str(me.get("description")))
        except Exception as e:  # noqa: BLE001
            report(False, "telegram bot", type(e).__name__)
    report(bool(chat), "telegram owner chat id", "" if chat else "TELEGRAM_CHAT_ID is empty")

    # AI providers, one ping each, and the privacy split
    asyncio.run(_doctor_ai(report, profile))

    # vault: only once applying is set up (VLT-3)
    from .apply.vault import VAULT_DIR, check
    if VAULT_DIR.exists():
        vault_problems = check(VAULT_DIR)
        report(not vault_problems, "vault (applying)", "; ".join(vault_problems))
    else:
        report(None, "vault (applying)", "not set up; copy vault.example/ to vault/ to use ⚡ Apply")

    # chrome for the planner's image
    from bandwidth.planner.render import chrome_path
    chrome = chrome_path()
    report(bool(chrome), "chrome (plan image)", chrome or "not found; the morning message will be text only")
    print("all good" if not problems else f"{problems} problem(s)")
    return 0 if not problems else 1


async def _doctor_ai(report, profile) -> None:
    from .ai import AIError, Ledger, Router, load_router

    path = CONFIG_DIR / "ai.yaml"
    if not path.exists():
        report(False, "AI config", "config/ai.yaml missing; copy config/ai.example.yaml")
        return
    try:
        main = load_router(path)
    except ValueError as e:
        report(False, "AI config", str(e))
        return
    report(any(p.local for p in main.providers), "local AI provider configured", "private tasks need one")
    if profile is not None:
        leaks = [t for t in main.private_terms if t in profile.about.lower()]
        if not profile.about.strip():
            report(False, "public profile", "`about` is empty in config/profile.yaml; ranking needs it")
        else:
            report(not leaks, "public profile has no private terms", f"found: {', '.join(leaks)}" if leaks else "")
        report(profile.private_facts is not None, "private facts for eligibility",
               "" if profile.private_facts else "add private_facts to config/profile.yaml")
    ledger = Ledger(AGENT_DB)
    for p in main.providers:
        if not p.usable:
            report(None, f"AI {p.name}", f"no {p.key_env} in .env")
            continue
        r = Router([p], ledger=ledger)
        start = time.monotonic()
        try:
            await r.text("ping_private" if p.local else "ping", "Reply with one word.", "Say ok.")
            report(True, f"AI {p.name}", f"{p.model}, {time.monotonic() - start:.1f}s{' (local)' if p.local else ''}")
        except AIError as e:
            report(False, f"AI {p.name}", str(e).split(": ", 1)[-1])


def cmd_vault(_args: argparse.Namespace) -> int:
    from .apply.vault import VAULT_DIR, check
    problems = check(VAULT_DIR)
    for p in problems:
        print(f"[FAIL] {p}")
    print(f"vault {VAULT_DIR}: " + ("all good" if not problems else f"{len(problems)} problem(s)"))
    return 0 if not problems else 1


# ---------------------------------------------------------------- ai-stats (AI-13)


def cmd_ai_stats(args: argparse.Namespace) -> int:
    from .ai import Ledger

    ledger = Ledger(AGENT_DB)
    for days in (1, args.days):
        s = ledger.stats(days=days)
        print(f"\nlast {days} day(s)")
        for title, key, col in (("by provider", "by_provider", "provider"), ("by task", "by_task", "task")):
            print(f"  {title}:")
            for row in s[key]:
                print(f"    {row[col]:12} calls {row['calls']:4}  failed {row['failures']:3}  "
                      f"tokens {row['tokens_in']:7}/{row['tokens_out']:<6}  avg {row['avg_ms']} ms")
        print(f"  private calls that left this machine: {s['private_calls_off_machine']}")
    return 0


# ---------------------------------------------------------------- migrate (RUN-2)


def backup_db(src: Path, dst: Path, force: bool = False) -> str:
    """Copy a live SQLite database safely (the online backup API, not a file copy)."""
    if not src.exists():
        return f"skip {src}: not found"
    if dst.exists() and not force:
        return f"skip {dst}: already exists (use --force to replace it)"
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    return f"copied {src} -> {dst}"


def cmd_migrate(args: argparse.Namespace) -> int:
    from bandwidth.radar import config as rconfig
    from bandwidth.planner import config as pconfig

    radar_src = Path(args.radar).expanduser() / "data" / "radar.db"
    planner_src = Path(args.planner).expanduser() / "data" / "planner.db"
    print(backup_db(radar_src, rconfig.DATA_DIR / "radar.db", args.force))
    print(backup_db(planner_src, pconfig.DATA_DIR / "planner.db", args.force))
    return 0


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["radar"]:
        from bandwidth.radar.cli import main as radar_main
        return radar_main(argv[1:])
    if argv[:1] == ["planner"]:
        from bandwidth.planner.cli import main as planner_main
        return planner_main(argv[1:])

    from bandwidth.radar.config import load_dotenv
    load_dotenv()
    os.umask(0o077)  # new data files are private

    ap = argparse.ArgumentParser(prog="bw", description="Bandwidth: your private study and opportunity assistant.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bot", help="run the Telegram bot and all schedules (launchd keeps it alive)")
    b.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("doctor", help="check keys, Telegram, AI providers and configs")
    s = sub.add_parser("ai-stats", help="AI calls, failures and tokens per provider and task")
    s.add_argument("--days", type=int, default=7)
    m = sub.add_parser("migrate", help="copy the databases from the old project folders (never moves them)")
    m.add_argument("--radar", default="~/Desktop/event-parser")
    m.add_argument("--planner", default="~/Desktop/study-planner")
    m.add_argument("--force", action="store_true")
    sub.add_parser("vault", help="check the vault that applications are filled from")
    sub.add_parser("radar", help="Opportunity Radar commands (bw radar --help)")
    sub.add_parser("planner", help="Study Planner commands (bw planner --help)")
    args = ap.parse_args(argv)
    return {"bot": cmd_bot, "doctor": cmd_doctor, "ai-stats": cmd_ai_stats, "migrate": cmd_migrate,
            "vault": cmd_vault}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
