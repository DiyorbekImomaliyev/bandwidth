"""Settings, secrets and the timetable (SPEC SRC-6, SAF-1)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..paths import CONFIG_DIR, ROOT

DATA_DIR = Path(os.environ.get("PLANNER_DATA_DIR") or ROOT / "data" / "planner")

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # index = date.weekday()
FOCUS_TASKS = ("leetcode", "learn", "uni", "push")


class ConfigError(Exception):
    pass


def hm(text: str) -> time:
    try:
        h, m = text.strip().split(":")
        return time(int(h), int(m))
    except ValueError as e:
        raise ConfigError(f"bad time {text!r} (use HH:MM)") from e


def span(text: str) -> tuple[time, time]:
    a, sep, b = text.partition("-")
    if not sep:
        raise ConfigError(f"bad time range {text!r} (use HH:MM-HH:MM)")
    start, end = hm(a), hm(b)
    if end <= start:
        raise ConfigError(f"time range ends before it starts: {text!r}")
    return start, end


@dataclass(frozen=True)
class ClassSlot:
    course: str
    kind: str
    day: str
    start: time
    end: time
    room: str


@dataclass(frozen=True)
class Exam:
    course: str
    day: date
    start: time
    end: time
    room: str


@dataclass(frozen=True)
class RoutineBlock:
    start: time
    end: time
    kind: str  # focus | break
    tasks: tuple[str, ...]  # focus: subset of FOCUS_TASKS; break: ()
    label: str  # break text, e.g. "family dinner"


@dataclass
class Settings:
    timezone: str = "Asia/Qatar"
    school_location: str = ""  # feed LOCATION of university-wide dates, e.g. "Main Campus"
    send_at: time = time(5, 0)
    quiet_from: time = time(22, 0)
    quiet_until: time = time(5, 0)
    weekly_day: str = "sat"
    weekly_at: time = time(20, 0)
    start_date: date = date(2026, 10, 24)
    classes_end: date = date(2026, 12, 3)
    exam_mode: tuple[date, date] | None = None
    aliases: dict[str, str] = field(default_factory=dict)
    classes: list[ClassSlot] = field(default_factory=list)
    exams: list[Exam] = field(default_factory=list)
    routine: dict[str, list[RoutineBlock]] = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def courses(self) -> set[str]:
        return {c.course for c in self.classes} | {e.course for e in self.exams}


def parse_block(line: str) -> RoutineBlock:
    parts = line.split(maxsplit=2)
    if len(parts) < 3:
        raise ConfigError(f"routine line needs 'HH:MM-HH:MM kind task': {line!r}")
    start, end = span(parts[0])
    kind, rest = parts[1], parts[2].strip()
    if kind == "focus":
        tasks = tuple(rest.split())
        bad = [t for t in tasks if t not in FOCUS_TASKS]
        if bad or not tasks:
            raise ConfigError(f"unknown focus task {bad or rest!r} in {line!r} (use {', '.join(FOCUS_TASKS)})")
        return RoutineBlock(start, end, "focus", tasks, "")
    if kind == "break":
        return RoutineBlock(start, end, "break", (), rest)
    raise ConfigError(f"routine kind must be focus or break: {line!r}")


def load_settings(path: Path | None = None) -> Settings:
    path = path or CONFIG_DIR / "planner.toml"
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"config not found: {path} (copy planner.example.toml to planner.toml)") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    s = Settings()
    s.timezone = raw.get("timezone", s.timezone)
    try:
        ZoneInfo(s.timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f"unknown timezone {s.timezone!r}") from e
    s.school_location = raw.get("school_location", "")
    s.send_at = hm(raw.get("send_at", "05:00"))
    s.quiet_from = hm(raw.get("quiet_from", "22:00"))
    s.quiet_until = hm(raw.get("quiet_until", "05:00"))
    wd, _, wt = raw.get("weekly_summary", "sat 20:00").partition(" ")
    if wd not in DAYS:
        raise ConfigError(f"weekly_summary day must be one of {DAYS}")
    s.weekly_day, s.weekly_at = wd, hm(wt)
    s.start_date = raw.get("start_date", s.start_date)
    s.classes_end = raw.get("classes_end", s.classes_end)
    if em := raw.get("exam_mode"):
        s.exam_mode = (em[0], em[1])
    s.aliases = {k.lower(): v.lower() for k, v in raw.get("aliases", {}).items()}
    for c in raw.get("class", []):
        if c.get("day") not in DAYS:
            raise ConfigError(f"class {c.get('course')!r}: day must be one of {DAYS}")
        a, b = span(c["time"])
        s.classes.append(ClassSlot(c["course"].lower(), c.get("kind", "lecture"), c["day"], a, b, c.get("room", "")))
    for e in raw.get("exam", []):
        a, b = span(e["time"])
        s.exams.append(Exam(e["course"].lower(), e["date"], a, b, e.get("room", "")))
    for key, lines in raw.get("routine", {}).items():
        if key not in (*DAYS, "exam"):
            raise ConfigError(f"routine.{key}: use a day name or 'exam'")
        s.routine[key] = sorted((parse_block(x) for x in lines), key=lambda b: b.start)
    return s


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader: KEY=VALUE lines; the real environment wins."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def secret(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


SECRET_NAMES = ("TELEGRAM_BOT_TOKEN", "D2L_ICS_URL", "GEMINI_API_KEY")


def redact(text: str) -> str:
    """Replace every secret value in text. The feed URL's token is redacted on its own too."""
    for name in SECRET_NAMES:
        value = secret(name)
        if value and len(value) >= 4:
            text = text.replace(value, "***")
            if "token=" in value:
                text = text.replace(value.split("token=", 1)[1], "***")
    return text
