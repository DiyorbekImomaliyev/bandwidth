"""Every application is a row with a state that moves only by compare-and-set (SPEC APP-7).

    planned → filling → handed_over                                   Partial help
    planned → filling → ready → submitting → submitted | needs_check  Fast apply
    any → failed
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .forms import posting_ref

STATES = {"planned", "filling", "handed_over", "ready", "submitting", "submitted", "needs_check", "failed"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id           INTEGER PRIMARY KEY,
    url          TEXT NOT NULL,
    ref          TEXT NOT NULL,      -- platform:board:job, the same posting under any of its URLs
    platform     TEXT NOT NULL,
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    mode         TEXT NOT NULL,      -- partial | dry | fast
    state        TEXT NOT NULL,
    plan         TEXT,
    preview_hash TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    submitted_at TEXT
);
CREATE INDEX IF NOT EXISTS applications_ref ON applications(ref);
CREATE TABLE IF NOT EXISTS apply_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _ref(url: str) -> str:
    r = posting_ref(url)
    return f"{r.platform}:{r.board}:{r.job_id}" if r else url


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Applications:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def create(self, url: str, platform: str, company: str, title: str, mode: str) -> int:
        now = _now()
        with self._db() as db:
            cur = db.execute("INSERT INTO applications (url, ref, platform, company, title, mode, state, created_at, "
                             "updated_at) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?)",
                             (url, _ref(url), platform, company, title, mode, now, now))
            return int(cur.lastrowid or 0)

    def move(self, app_id: int, frm: set[str], to: str) -> bool:
        """Compare-and-set: moves only from one of `frm`; a second, late move does nothing."""
        if to not in STATES or not frm <= STATES:
            raise ValueError(f"unknown state: {to} or {frm}")
        now = _now()
        marks = ",".join("?" * len(frm))
        with self._db() as db:
            cur = db.execute(f"UPDATE applications SET state = ?, updated_at = ?, "
                             f"submitted_at = CASE WHEN ? = 'submitted' THEN ? ELSE submitted_at END "
                             f"WHERE id = ? AND state IN ({marks})", (to, now, to, now, app_id, *frm))
            return cur.rowcount == 1

    def update(self, app_id: int, **fields: Any) -> None:
        allowed = {"plan", "preview_hash", "error", "company", "title", "mode"}
        if not set(fields) <= allowed:
            raise ValueError(f"cannot update {set(fields) - allowed}")
        with self._db() as db:
            sets = ", ".join(f"{k} = ?" for k in fields)
            db.execute(f"UPDATE applications SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), _now(), app_id))

    def get(self, app_id: int) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
            return dict(row) if row else None

    def submitted(self, url: str) -> str | None:
        """When this posting was submitted, under any of its URLs; None if never."""
        with self._db() as db:
            row = db.execute("SELECT submitted_at FROM applications WHERE ref = ? AND state = 'submitted' "
                             "ORDER BY submitted_at LIMIT 1", (_ref(url),)).fetchone()
            return row[0] if row else None

    def recent(self, n: int = 8) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM applications ORDER BY id DESC LIMIT ?", (n,))]

    def submitted_since(self, iso: str) -> int:
        """How many applications were submitted at or after `iso` (FA-6)."""
        with self._db() as db:
            return int(db.execute("SELECT COUNT(*) FROM applications WHERE state = 'submitted' AND submitted_at >= ?",
                                  (iso,)).fetchone()[0])

    def kv_set(self, key: str, value: Any) -> None:
        with self._db() as db:
            db.execute("INSERT INTO apply_kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                       (key, json.dumps(value)))

    def kv_get(self, key: str) -> Any:
        with self._db() as db:
            row = db.execute("SELECT value FROM apply_kv WHERE key = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else None
