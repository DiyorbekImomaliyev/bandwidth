"""Read the D2L calendar feed and write the planner's own .ics (SPEC SRC-2..4, SRC-7, ICS-1)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ALL_DAY_TIME = time(23, 59)  # an all-day item is due at the end of that day


@dataclass(frozen=True)
class Deadline:
    uid: str
    course: str  # "comp2201", or "school" for university-wide dates
    title: str  # cleaned, e.g. "lab 6 - ipc"
    kind: str  # lab | quiz | assignment | exam | other
    due: datetime  # aware, UTC
    source: str = "d2l"  # d2l | manual


def unfold(text: str) -> list[str]:
    """Join folded lines: a line that starts with a space or tab continues the previous one."""
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def unescape(value: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m[1] in "nN" else m[1], value)


def html_to_text(value: str) -> str:
    value = re.sub(r"<br\s*/?>|</p>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"[ \t]+", " ", html.unescape(value)).strip()


def parse_events(text: str) -> list[dict[str, str]]:
    """Each VEVENT as {NAME: value, NAME_PARAMS: ";TZID=..."}; values are unescaped."""
    events: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, _, value = line.partition(":")
            name, _, params = head.partition(";")
            cur[name.upper()] = unescape(value)
            cur[name.upper() + "_PARAMS"] = params
    return events


def parse_dt(value: str, params: str, local: ZoneInfo) -> tuple[datetime, bool]:
    """(aware UTC datetime, all_day). Handles UTC 'Z', TZID=, floating and VALUE=DATE."""
    value = value.strip()
    if "VALUE=DATE" in params.upper() and "VALUE=DATE-TIME" not in params.upper() or len(value) == 8:
        d = datetime.strptime(value[:8], "%Y%m%d").date()
        return datetime.combine(d, ALL_DAY_TIME, local).astimezone(UTC), True
    naive = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return naive.replace(tzinfo=UTC), False
    m = re.search(r"TZID=([^;:]+)", params)
    tz = local
    if m:
        try:
            tz = ZoneInfo(m[1].strip('"'))
        except (ZoneInfoNotFoundError, ValueError):
            tz = local
    return naive.replace(tzinfo=tz).astimezone(UTC), False


COURSE_RE = re.compile(r"\b([A-Z]{4}\s?\d{4})\b")
SUFFIX_RE = re.compile(r"\s*-\s*(due|availability ends|available)\s*$", re.I)
NOISE_RE = re.compile(r"-?\s*requires respondus lockdown browser\s*", re.I)


def course_of(summary: str, location: str, school: str = "") -> str:
    if school and location.strip().lower() == school.lower():
        return "school"
    for text in (location, summary):
        if m := COURSE_RE.search(text):
            return m[1].replace(" ", "").lower()
    return "other"


def kind_of(title: str) -> str:
    t = title.lower()
    if re.search(r"\bmid-?term\b|\bfinal\b|\bexam\b", t):
        return "exam"
    if re.search(r"\bquiz", t):
        return "quiz"
    if re.search(r"\blab\b", t):
        return "lab"
    if re.search(r"assignment|essay|project|report|homework|exercise|presentation", t):
        return "assignment"
    return "other"


def clean_title(summary: str, course: str) -> str:
    t = SUFFIX_RE.sub("", summary)
    t = NOISE_RE.sub(" ", t)
    if course not in ("school", "other"):  # "DATA3201 Fall 2026 Quiz 1" -> "Quiz 1"
        t = re.sub(rf"^\s*{course[:4]}\s?{course[4:]}\s*((fall|winter|spring|summer)\s*\d{{4}})?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*-\s*", " - ", t)  # "Lab 6 -  IPC" and "Quiz 1-" -> one style
    t = re.sub(r"\s+", " ", t).strip(" -")
    return t.lower()


def deadlines_from_feed(text: str, local: ZoneInfo, school: str = "") -> list[Deadline]:
    """Deadlines and university-wide dates (events whose LOCATION is `school`, e.g. "CAMPUS").
    Items that only say when something opens are skipped."""
    out: list[Deadline] = []
    for ev in parse_events(text):
        summary, location = ev.get("SUMMARY", "").strip(), ev.get("LOCATION", "").strip()
        if not summary or "DTSTART" not in ev:
            continue
        m = SUFFIX_RE.search(summary)
        suffix = m[1].lower() if m else ""
        course = course_of(summary, location, school)
        if suffix == "available":
            continue  # an opening time, not a deadline
        if not suffix and course != "school":
            continue  # course events without "- Due" are not deadlines
        due, _ = parse_dt(ev["DTSTART"], ev.get("DTSTART_PARAMS", ""), local)
        title = clean_title(summary, course)
        out.append(Deadline(ev.get("UID", f"{summary}-{ev['DTSTART']}"), course, title,
                            kind_of(title) if course != "school" else "other", due))
    return out


# ---- export ----------------------------------------------------------------------------------

def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Fold to 75 octets per line (RFC 5545), never splitting a UTF-8 character."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode()) > (75 if not out else 74):
            out.append(cur)
            cur = ""
        cur += ch
    out.append(cur)
    return "\r\n ".join(out)


def _utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def to_ics(deadlines: list[Deadline], exams: list[tuple[str, datetime, datetime, str]], now: datetime) -> str:
    """Deadlines as 15-minute events ending at the due time; exams as real events."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//study-planner//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Study Planner"]
    for d in deadlines:
        lines += ["BEGIN:VEVENT", f"UID:{_esc(d.uid)}", f"DTSTAMP:{_utc(now)}",
                  f"DTSTART:{_utc(d.due - timedelta(minutes=15))}", f"DTEND:{_utc(d.due)}",
                  f"SUMMARY:{_esc(f'{d.course} - {d.title} - due')}", "END:VEVENT"]
    for course, start, end, room in exams:
        lines += ["BEGIN:VEVENT", f"UID:exam-{course}-{start.date().isoformat()}@study-planner",
                  f"DTSTAMP:{_utc(now)}", f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end)}",
                  f"SUMMARY:{_esc(f'{course} final exam')}", f"LOCATION:{_esc(room)}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"


def local_date(dt: datetime, tz: ZoneInfo) -> date:
    return dt.astimezone(tz).date()
