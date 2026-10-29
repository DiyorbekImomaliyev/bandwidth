"""Command line: planner run | bot | preview | check (SPEC RUN-1..3)."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from datetime import UTC, date, datetime

from . import config, jobs
from .config import ConfigError, load_dotenv, load_settings, redact, secret
from .store import Store
from .telegram import Telegram, TelegramError


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(verbose: bool) -> None:
    logs = config.DATA_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file = logging.handlers.RotatingFileHandler(logs / "planner.log", maxBytes=1_000_000, backupCount=3)
    file.setFormatter(fmt)
    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    err.setLevel(logging.INFO if verbose else logging.WARNING)
    logging.basicConfig(level=logging.INFO, handlers=[file, err], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines contain the bot token


def make_ctx(dry_run: bool) -> jobs.Ctx:
    s = load_settings()
    tg = None if dry_run else Telegram(secret("TELEGRAM_BOT_TOKEN") or "", secret("TELEGRAM_CHAT_ID") or "")
    return jobs.Ctx(s, Store(config.DATA_DIR / "planner.db"), tg, config.DATA_DIR)


def cmd_run(args: argparse.Namespace) -> int:
    ctx = make_ctx(args.dry_run)
    try:
        if args.dry_run or args.force:
            now = datetime.now(UTC)
            jobs.refresh_feed(ctx, now, force=True)
            jobs.send_daily(ctx, now, force=True)
            if args.dry_run:
                print(f"wrote {ctx.out_dir}/{now.astimezone(ctx.s.tz).date().isoformat()}.png and .txt")
        else:
            jobs.tick(ctx)
    finally:
        ctx.store.close()
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    """Write the message for any day to data/out without sending it."""
    ctx = make_ctx(True)
    try:
        day = date.fromisoformat(args.day) if args.day else datetime.now(UTC).astimezone(ctx.s.tz).date()
        text, _ = jobs.daily_caption(ctx, day, datetime.now(UTC))
        png = jobs.daily_image(ctx, day)
        print(text)
        print(f"\nimage: {ctx.out_dir / (day.isoformat() + '.png')}" if png else "\nimage: not rendered")
    finally:
        ctx.store.close()
    return 0


def cmd_bot(_args: argparse.Namespace) -> int:
    """The bot is shared with Opportunity Radar now: one process serves both (SPEC BOT-1)."""
    print("The planner's bot now runs inside the shared bot: use `bw bot`.", file=sys.stderr)
    return 2


def cmd_check(_args: argparse.Namespace) -> int:
    """Check config, secrets, Chrome and the bot token without sending anything."""
    ok = True
    try:
        s = load_settings()
        print(f"config ok - {len(s.classes)} classes, {len(s.exams)} exams, routine for {len(s.routine)} days")
    except ConfigError as e:
        print(f"config: {e}")
        ok = False
    for name in ("D2L_ICS_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        print(f"{name}: {'set' if secret(name) else 'MISSING'}")
        ok &= bool(secret(name))
    from .render import chrome_path

    print(f"chrome: {chrome_path() or 'MISSING (images will not be sent)'}")
    if secret("TELEGRAM_BOT_TOKEN"):
        try:
            me = Telegram(secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID") or "-").call("getMe")
            print(f"bot: @{me['username']}")
        except TelegramError as e:
            print(f"bot: {e}")
            ok = False
    return 0 if ok else 1


def cmd_chat_id(_args: argparse.Namespace) -> int:
    """Print the chats that messaged the bot, to find TELEGRAM_CHAT_ID. Run before `planner bot` is running."""
    tg = Telegram(secret("TELEGRAM_BOT_TOKEN") or "", "-")
    seen = {}
    for u in tg.call("getUpdates") or []:
        chat = (u.get("message") or {}).get("chat") or {}
        if "id" in chat:
            seen[chat["id"]] = chat.get("username") or chat.get("first_name") or "?"
    if not seen:
        print("no messages yet - open your bot in Telegram, send it /start, then run this again")
    for cid, name in seen.items():
        print(f"{cid}  {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="planner", description="Daily study plan and D2L deadlines on Telegram.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="do what is due now (the schedule runs this every 30 minutes)")
    r.add_argument("--dry-run", action="store_true", help="write today's message to data/out, send nothing")
    r.add_argument("--force", action="store_true", help="send today's message now, even if already sent")
    pv = sub.add_parser("preview", help="write the message for a day to data/out")
    pv.add_argument("day", nargs="?", help="YYYY-MM-DD (default today)")
    sub.add_parser("bot", help="run the Telegram bot (buttons, messages, schedule)")
    sub.add_parser("check", help="check config, secrets, chrome and the bot")
    sub.add_parser("chat-id", help="show your chat id (send the bot /start first)")
    args = p.parse_args(argv)
    load_dotenv()
    setup_logging(args.verbose)
    try:
        commands = {"run": cmd_run, "preview": cmd_preview, "bot": cmd_bot, "check": cmd_check, "chat-id": cmd_chat_id}
        return commands[args.cmd](args)
    except (ConfigError, TelegramError) as e:
        print(redact(str(e)), file=sys.stderr)
        return 1
