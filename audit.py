"""
The audit log of the public site: audit.sqlite in the data folder, one row per event that
matters for security or abuse (ACTIONS): sign-ins and sign-ups, approvals and blocks, searches
started and cancelled, share links, deletions, the demo flag, favourite lines marked and
unmarked, other administrators' actions, and refusals by a quota, a rate or a safeguard.

- Its own database, not jobs.sqlite or accounts.sqlite: both the web server and the worker
  write it (the worker never opens accounts.sqlite), it is kept, backed up and purged on
  its own, and reading it (`sqlite3 audit.sqlite`) never touches the busy queue.
- A row is when (UTC), the action, who (users.id of accounts.sqlite; none for a visitor),
  what it was done to (a job, run or user id) and a short JSON detail. No IP addresses, no
  names or e-mail addresses (the user id leads to the account while it exists).
- Rows older than KEEP_DAYS are deleted by the worker's housekeeping (jobs.Scheduler).
- The local app keeps no audit log.

There is no viewer in the app; administrators read it with sqlite3, e.g.
`sqlite3 audit.sqlite "SELECT * FROM audit ORDER BY id DESC LIMIT 50"`.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import cache

KEEP_DAYS = 90
ACTIONS = ("sign_in", "sign_up", "sign_up_refused", "sign_out", "user_status", "user_unlimited", "user_revoke",
           "job_add", "job_cancel", "share_create", "share_renew", "share_revoke", "run_delete", "run_demo",
           "favourite", "refused")
# The schema of audit.sqlite, one step per version (cache.migrate; never change a released step)
MIGRATIONS = [
    """CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, action TEXT NOT NULL,
                           user_id INTEGER, target TEXT, detail TEXT);
       CREATE INDEX audit_at ON audit (at);
       CREATE INDEX audit_user ON audit (user_id, at);""",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Audit:
    """The audit log of a data folder (audit.sqlite)."""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "audit.sqlite")
        db = self._db()
        try:
            cache.migrate(db, MIGRATIONS)
        finally:
            db.close()

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=60)
        db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = sqlite3.Row
        return db

    def log(self, action: str, user_id: Optional[int] = None, target: Optional[str] = None,
            detail: Optional[Dict] = None):
        """Record an event (one of ACTIONS)."""
        if action not in ACTIONS:
            raise ValueError(action)
        with self._db() as db:
            db.execute("INSERT INTO audit (at, action, user_id, target, detail) VALUES (?, ?, ?, ?, ?)",
                       (_now(), action, user_id, target, json.dumps(detail, ensure_ascii=False) if detail else None))
        db.close()

    def entries(self, user_id: Optional[int] = None) -> List[Dict]:
        """The events, oldest first (of one user with user_id); for tests and scripts."""
        with self._db() as db:
            rows = db.execute("SELECT * FROM audit" + (" WHERE user_id = ?" if user_id is not None else "")
                              + " ORDER BY id", () if user_id is None else (user_id,)).fetchall()
        db.close()
        return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]

    def purge(self, days: float = KEEP_DAYS) -> int:
        """Delete the events older than `days`."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._db() as db:
            count = db.execute("DELETE FROM audit WHERE at < ?", (cutoff,)).rowcount
        db.close()
        return count


def purge(data_dir: str, days: float = KEEP_DAYS) -> int:
    """Housekeeping: delete old events of a data folder's audit log, if it has one."""
    if not os.path.exists(os.path.join(data_dir, "audit.sqlite")):
        return 0
    return Audit(data_dir).purge(days)
