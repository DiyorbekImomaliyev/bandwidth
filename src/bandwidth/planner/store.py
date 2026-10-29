"""SQLite storage: deadlines, alerts, daily musts, leetcode progress, work logs (SPEC DB-1)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from .ics import Deadline

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deadlines (
    uid TEXT PRIMARY KEY, course TEXT NOT NULL, title TEXT NOT NULL, kind TEXT NOT NULL,
    due TEXT NOT NULL, source TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
    snoozed_until TEXT, removed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alerts (uid TEXT NOT NULL, slot TEXT NOT NULL, sent_at TEXT NOT NULL,
    PRIMARY KEY (uid, slot));
CREATE TABLE IF NOT EXISTS musts (day TEXT NOT NULL, task TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, task));
CREATE TABLE IF NOT EXISTS lc_day (day TEXT PRIMARY KEY, new TEXT NOT NULL, review INTEGER);
CREATE TABLE IF NOT EXISTS lc_solved (idx INTEGER PRIMARY KEY, solved_on TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lc_review (idx INTEGER NOT NULL, due TEXT NOT NULL, done_on TEXT,
    PRIMARY KEY (idx, due));
CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
    name TEXT NOT NULL, hours REAL NOT NULL);
CREATE TABLE IF NOT EXISTS daily_msg (day TEXT PRIMARY KEY, message_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pending (message_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if self.kv_get("schema") is None:
            self.kv_set("schema", str(SCHEMA_VERSION))

    def close(self) -> None:
        self.db.close()

    # ---- kv ----
    def kv_get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, value))

    # ---- deadlines ----
    def sync_d2l(self, items: list[Deadline], now: datetime) -> None:
        """Upsert by UID (keeps done/snooze). Future D2L items missing from the feed are removed."""
        with self.db:
            for d in items:
                self.db.execute(
                    "INSERT INTO deadlines (uid, course, title, kind, due, source) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(uid) DO UPDATE SET course=excluded.course, title=excluded.title, "
                    "kind=excluded.kind, due=excluded.due, removed=0",
                    (d.uid, d.course, d.title, d.kind, _iso(d.due), d.source))
            seen = {d.uid for d in items}
            for row in self.db.execute("SELECT uid FROM deadlines WHERE source='d2l' AND removed=0 AND due>?",
                                       (_iso(now),)).fetchall():
                if row[0] not in seen:
                    self.db.execute("UPDATE deadlines SET removed=1 WHERE uid=?", (row[0],))

    def add_manual(self, d: Deadline) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO deadlines (uid, course, title, kind, due, source) "
                            "VALUES (?,?,?,?,?,'manual')", (d.uid, d.course, d.title, d.kind, _iso(d.due)))

    def remove(self, uid: str) -> None:
        with self.db:
            self.db.execute("UPDATE deadlines SET removed=1 WHERE uid=?", (uid,))

    def deadline(self, uid: str) -> Deadline | None:
        row = self.db.execute("SELECT * FROM deadlines WHERE uid=?", (uid,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(r: sqlite3.Row) -> Deadline:
        return Deadline(r["uid"], r["course"], r["title"], r["kind"], _dt(r["due"]), r["source"])

    def upcoming(self, start: datetime, end: datetime, include_done: bool = True) -> list[Deadline]:
        q = "SELECT * FROM deadlines WHERE removed=0 AND due>=? AND due<?" + ("" if include_done else " AND done=0")
        return [self._row(r) for r in self.db.execute(q + " ORDER BY due, course", (_iso(start), _iso(end)))]

    def key(self, uid: str) -> int | None:
        """Short numeric id for buttons (callback data is limited to 64 bytes)."""
        row = self.db.execute("SELECT rowid FROM deadlines WHERE uid=?", (uid,)).fetchone()
        return row[0] if row else None

    def uid_for(self, key: int) -> str | None:
        row = self.db.execute("SELECT uid FROM deadlines WHERE rowid=?", (key,)).fetchone()
        return row[0] if row else None

    def is_done(self, uid: str) -> bool:
        row = self.db.execute("SELECT done FROM deadlines WHERE uid=?", (uid,)).fetchone()
        return bool(row and row[0])

    def set_done(self, uid: str, done: bool = True) -> None:
        with self.db:
            self.db.execute("UPDATE deadlines SET done=?, snoozed_until=NULL WHERE uid=?", (int(done), uid))

    def snooze(self, uid: str, until: datetime) -> None:
        with self.db:
            self.db.execute("UPDATE deadlines SET snoozed_until=? WHERE uid=?", (_iso(until), uid))

    def snoozed_until(self, uid: str) -> datetime | None:
        row = self.db.execute("SELECT snoozed_until FROM deadlines WHERE uid=?", (uid,)).fetchone()
        return _dt(row[0]) if row and row[0] else None

    def clear_snooze(self, uid: str) -> None:
        with self.db:
            self.db.execute("UPDATE deadlines SET snoozed_until=NULL WHERE uid=?", (uid,))

    def snoozed(self) -> list[tuple[Deadline, datetime]]:
        rows = self.db.execute("SELECT * FROM deadlines WHERE snoozed_until IS NOT NULL AND done=0 AND removed=0")
        return [(self._row(r), _dt(r["snoozed_until"])) for r in rows]

    # ---- alerts ----
    def alert_sent(self, uid: str, slot: str) -> bool:
        return self.db.execute("SELECT 1 FROM alerts WHERE uid=? AND slot=?", (uid, slot)).fetchone() is not None

    def mark_alert(self, uid: str, slot: str, now: datetime) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO alerts VALUES (?,?,?)", (uid, slot, _iso(now)))

    # ---- daily musts ----
    def must_done(self, day: date, task: str) -> bool:
        row = self.db.execute("SELECT done FROM musts WHERE day=? AND task=?", (day.isoformat(), task)).fetchone()
        return bool(row and row[0])

    def set_must(self, day: date, task: str, done: bool) -> None:
        with self.db:
            self.db.execute("INSERT INTO musts VALUES (?,?,?) ON CONFLICT(day, task) DO UPDATE SET done=excluded.done",
                            (day.isoformat(), task, int(done)))

    def musts_between(self, start: date, end: date) -> dict[str, set[str]]:
        """{day iso: {done tasks}} for start <= day <= end."""
        out: dict[str, set[str]] = {}
        for r in self.db.execute("SELECT day, task FROM musts WHERE done=1 AND day>=? AND day<=?",
                                 (start.isoformat(), end.isoformat())):
            out.setdefault(r["day"], set()).add(r["task"])
        return out

    # ---- leetcode ----
    def lc_day(self, day: date) -> tuple[list[int], int | None] | None:
        row = self.db.execute("SELECT new, review FROM lc_day WHERE day=?", (day.isoformat(),)).fetchone()
        return (json.loads(row["new"]), row["review"]) if row else None

    def set_lc_day(self, day: date, new: list[int], review: int | None) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO lc_day VALUES (?,?,?)", (day.isoformat(), json.dumps(new), review))

    def solved(self) -> dict[int, str]:
        return {r["idx"]: r["solved_on"] for r in self.db.execute("SELECT idx, solved_on FROM lc_solved")}

    def solve(self, idx: int, day: date, review_days: tuple[int, ...]) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO lc_solved VALUES (?,?)", (idx, day.isoformat()))
            for n in review_days:
                due = date.fromordinal(day.toordinal() + n).isoformat()
                self.db.execute("INSERT OR IGNORE INTO lc_review (idx, due) VALUES (?,?)", (idx, due))

    def unsolve(self, idx: int, day: date) -> None:
        """Undo a solve made on `day` (a second tap on the button)."""
        with self.db:
            if self.db.execute("DELETE FROM lc_solved WHERE idx=? AND solved_on=?", (idx, day.isoformat())).rowcount:
                self.db.execute("DELETE FROM lc_review WHERE idx=? AND done_on IS NULL", (idx,))

    def next_review(self, day: date) -> int | None:
        """The oldest review due on or before `day` that is not done."""
        row = self.db.execute("SELECT idx FROM lc_review WHERE done_on IS NULL AND due<=? ORDER BY due, idx LIMIT 1",
                              (day.isoformat(),)).fetchone()
        return row[0] if row else None

    def review_done(self, idx: int, day: date, done: bool) -> None:
        with self.db:
            if done:
                self.db.execute("UPDATE lc_review SET done_on=? WHERE rowid=(SELECT rowid FROM lc_review "
                                "WHERE idx=? AND done_on IS NULL AND due<=? ORDER BY due LIMIT 1)",
                                (day.isoformat(), idx, day.isoformat()))
            else:
                self.db.execute("UPDATE lc_review SET done_on=NULL WHERE rowid=(SELECT rowid FROM lc_review "
                                "WHERE idx=? AND done_on=? ORDER BY due DESC LIMIT 1)", (idx, day.isoformat()))

    def solved_between(self, start: date, end: date) -> int:
        return self.db.execute("SELECT COUNT(*) FROM lc_solved WHERE solved_on>=? AND solved_on<=?",
                               (start.isoformat(), end.isoformat())).fetchone()[0]

    # ---- work logs ----
    def log(self, day: date, name: str, hours: float) -> None:
        with self.db:
            self.db.execute("INSERT INTO logs (day, name, hours) VALUES (?,?,?)", (day.isoformat(), name, hours))

    def logged(self, start: date, end: date) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(hours),0) FROM logs WHERE day>=? AND day<=?",
                              (start.isoformat(), end.isoformat())).fetchone()
        return float(row[0])

    # ---- telegram message ids ----
    def daily_msg(self, day: date) -> int | None:
        row = self.db.execute("SELECT message_id FROM daily_msg WHERE day=?", (day.isoformat(),)).fetchone()
        return row[0] if row else None

    def set_daily_msg(self, day: date, message_id: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO daily_msg VALUES (?,?)", (day.isoformat(), message_id))

    def put_pending(self, message_id: int, data: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO pending VALUES (?,?)", (message_id, json.dumps(data)))

    def pop_pending(self, message_id: int) -> dict | None:
        row = self.db.execute("SELECT data FROM pending WHERE message_id=?", (message_id,)).fetchone()
        if not row:
            return None
        with self.db:
            self.db.execute("DELETE FROM pending WHERE message_id=?", (message_id,))
        return json.loads(row[0])
