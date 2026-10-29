"""Understand messages from the bot's owner: new deadlines and work logs (SPEC IN-1, IN-2)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import DAYS, Settings
from .ics import kind_of

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
FILLER = {"due", "on", "at", "by", "is", "the"}

LOG_RE = re.compile(r"^(?:(?P<name>[a-z][\w .+-]*?)\s+)?(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>h|hr|hrs|hours?|m|min|mins)"
                    r"(?:\s*(?P<min>\d+)\s*m(?:in)?)?(?:\s+(?P<name2>[a-z][\w .+-]*))?$")


def parse_log(text: str) -> tuple[str, float] | None:
    """'capstone 2h', '2h capstone', 'football 1.5h', 'capstone 90m', 'capstone 1h 30m' -> (name, hours)."""
    t = " ".join(text.lower().split())
    m = LOG_RE.match(t)
    if not m or not (m["name"] or m["name2"]):
        return None
    num = float(m["num"])
    hours = num / 60 if m["unit"].startswith("m") else num + (int(m["min"]) / 60 if m["min"] else 0)
    if not 0 < hours <= 16:
        return None
    return (m["name"] or m["name2"]).strip(), round(hours, 2)


@dataclass(frozen=True)
class NewItem:
    course: str
    title: str
    kind: str
    due: datetime  # aware, local tz

    def as_dict(self) -> dict:
        return {"course": self.course, "title": self.title, "kind": self.kind, "due": self.due.isoformat()}


TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def _time(tokens: list[str]) -> tuple[time | None, list[str]]:
    """A time at the end: '10am', '10:30', '14:00', '10 pm', 'at 9'."""
    for n in (2, 1):
        if len(tokens) >= n:
            cand = "".join(tokens[-n:])
            m = TIME_RE.match(cand)
            if m and (m[2] or m[3] or (len(tokens) > n and tokens[-n - 1] == "at")):
                h, mi = int(m[1]), int(m[2] or 0)
                if m[3] == "pm" and h < 12:
                    h += 12
                if m[3] == "am" and h == 12:
                    h = 0
                if h < 24 and mi < 60:
                    rest = tokens[:-n]
                    return time(h, mi), rest[:-1] if rest and rest[-1] == "at" else rest
    return None, tokens


def _date(tokens: list[str], today: date) -> tuple[date | None, list[str]]:
    """'nov 2', '2 nov', 'november 2nd', 'tomorrow', 'today', 'fri' (next one)."""
    for i, tok in enumerate(tokens):
        if tok in ("today", "tomorrow"):
            return today + timedelta(days=tok == "tomorrow"), tokens[:i] + tokens[i + 1:]
        if tok[:3] in DAYS and (len(tok) == 3 or tok.endswith("day")) and i > 0:
            ahead = (DAYS.index(tok[:3]) - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead), tokens[:i] + tokens[i + 1:]
        mon = MONTHS.get(tok[:3]) if tok.isalpha() and len(tok) >= 3 else None
        if mon is None:
            continue
        for j in (i + 1, i - 1):
            if 0 <= j < len(tokens) and (m := re.fullmatch(r"(\d{1,2})(st|nd|rd|th)?", tokens[j])):
                try:
                    d = date(today.year, mon, int(m[1]))
                except ValueError:
                    return None, tokens
                if d < today - timedelta(days=30):
                    d = d.replace(year=today.year + 1)
                return d, [t for k, t in enumerate(tokens) if k not in (i, j)]
    return None, tokens


def parse_item(text: str, s: Settings, now: datetime) -> NewItem | None:
    """'hist essay due nov 2', 'os midterm nov 5 10am', 'comp2201 quiz 4 fri 23:59'."""
    tokens = re.sub(r"[,]", " ", text.lower()).split()
    today = now.astimezone(s.tz).date()
    when, tokens = _time(tokens)
    day, tokens = _date(tokens, today)
    if day is None:
        return None
    if when is None:
        when, tokens = _time(tokens)
    words = [t for t in tokens if t not in FILLER]
    course = "other"
    for k, w in enumerate(words):
        if w in s.aliases:
            course = s.aliases[w]
        elif w in s.courses or re.fullmatch(r"[a-z]{4}\d{4}", w):
            course = w
        else:
            continue
        words = words[:k] + words[k + 1:]
        break
    title = " ".join(words).strip(" -")
    if not title:
        return None
    due = datetime.combine(day, when or time(23, 59), s.tz)
    return NewItem(course, title, kind_of(title), due)
