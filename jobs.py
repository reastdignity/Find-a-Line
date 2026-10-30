"""
The job queue of the web application and its worker.

- Jobs: the queue and the events of the jobs (jobs.sqlite in the data folder), and the
  leases of the workers.
- Scheduler: the worker. `findaline.py worker` runs it as its own process, `findaline.py
  web` in a thread of its own unless it gets --no-worker. It starts one process per job
  and phase (run_phase): one downloading and one computing job at a time. It holds a
  lease in the table `workers` and renews it every HEARTBEAT seconds; the running jobs
  of a worker whose lease expired are failed by the next worker that looks (so a
  restart of the web server does not touch the jobs of a separate worker).
- Cancelling sets jobs.cancel_requested; the worker running the job kills its whole
  process tree (psutil) and marks it cancelled. A waiting job is cancelled at once.
- run_phase: a phase in its own process; its stdout becomes "info" events
  (_EventWriter), the progress goes as "progress" events.
- The public site (`worker --public`, Jobs(public=True)): events, errors and answers pass
  Redact, which removes the data folder and other absolute server paths; an unexpected
  exception shows "internal error, ref <id>" and its traceback goes to the log (stderr).
- The quick lane: estimates and profiles of the page (short, but they may call Nominatim
  or Overpass, read sheet indexes and build grids) are rows of the table `quick`, which
  one long-lived process of the worker (run_quick) serves one after another; the web
  server only adds a row and waits for its answer. A task that takes longer than the
  time limit gets its process killed and a new one started.
"""

import contextlib
import json
import math
import multiprocessing
import os
import re
import shutil
import signal
import socket
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Set

import cache
import params as pm
from settings import Limits, PublicSettings

TICK = 0.5                # seconds between looks at the queue
PROGRESS_INTERVAL = 0.3   # seconds between progress events of a job
HEARTBEAT = 5.0           # seconds between renewals of a worker's lease
HOUSEKEEPING = 6 * 3600   # seconds between clean-ups of the data folder (and at the start)
QUICK_POLL = 0.05         # seconds between looks of the quick lane at its table
KEEP_WORK_DAYS = 7        # days the work folder of a job that failed unexpectedly is kept (to look at)
KEEP_QUICK_SECONDS = 3600  # an answer of the quick lane nobody took (the page was closed) is deleted after it
# A waiting task of the quick lane is deleted by the web process that waits for it when it gives up
# (web.quick); one left by a web process that stopped is deleted after this (longer than any wait)
KEEP_WAITING_QUICK_SECONDS = 86400
JOB_EVENTS_MAX = 1000  # messages of a job its page gets at once (the newest; and the last progress)
LEASE = 30.0              # seconds a lease lasts without a renewal
# What a job is doing; "ready" means its data is downloaded and it waits to compute
ACTIVE = ("downloading", "computing")
WAITING = ("queued", "ready")
FINISHED = ("done", "failed", "cancelled")
PHASE_STATUS = {"download": "downloading", "compute": "computing"}  # the status each phase works in
VISIBILITIES = ("private", "demo")  # runs.visibility: the owner's (and administrators'), or everyone's
INTERRUPTED = ("interrupted: the app was closed (a tiled area resumes where it stopped when it is "
               "started again)")
# The schema of jobs.sqlite, one step per version (cache.migrate; never change a released step).
# Version 1 is the schema from before the versions: such a database (user_version 0)
# already has these tables, so the step only marks it, and its jobs stay.
MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS jobs (
           id TEXT PRIMARY KEY, created TEXT, status TEXT, request TEXT, title TEXT,
           run_id TEXT, error TEXT, finished TEXT);
       CREATE TABLE IF NOT EXISTS events (
           seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, kind TEXT, data TEXT);""",
    # 2: the events of one job (the log of the page) and the next job of a status (claim)
    """CREATE INDEX IF NOT EXISTS events_job ON events (job, seq);
       CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, created);""",
    # 3: the worker running a job, cancelling through the database, the leases of the workers
    """ALTER TABLE jobs ADD COLUMN worker_id TEXT;
       ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
       CREATE TABLE workers (id TEXT PRIMARY KEY, pid INTEGER, host TEXT, started TEXT, expires REAL);""",
    # 4: the tasks of the quick lane (estimates and profiles); `answer` is {"code": HTTP status, "body": ...}
    """CREATE TABLE quick (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, request TEXT, status TEXT,
                           answer TEXT, worker_id TEXT, created REAL, started REAL);
       CREATE INDEX quick_status ON quick (status, id);""",
    # 5: the owner of a job (accounts.sqlite users.id; NULL: the local app's, or from before the accounts)
    """ALTER TABLE jobs ADD COLUMN user_id INTEGER;
       CREATE INDEX jobs_user ON jobs (user_id, created);""",
    # 6: the owners of the runs (web.py serves a run's files to them only), who sees them (private;
    # later demo), the sha256 of a run's share link and its size on disk
    """CREATE TABLE runs (run_id TEXT PRIMARY KEY, user_id INTEGER, visibility TEXT NOT NULL DEFAULT 'private',
                          share_hash TEXT UNIQUE, bytes INTEGER NOT NULL DEFAULT 0, created TEXT);
       CREATE INDEX runs_user ON runs (user_id, created);""",
    # 7: quotas: whether a job or a quick task is a user's without limits (the worker applies its
    # Limits otherwise), the owner of a quick task, and what users used (km², Nominatim requests)
    """ALTER TABLE jobs ADD COLUMN unlimited INTEGER NOT NULL DEFAULT 0;
       ALTER TABLE quick ADD COLUMN user_id INTEGER;
       ALTER TABLE quick ADD COLUMN unlimited INTEGER NOT NULL DEFAULT 0;
       CREATE TABLE usage (user_id INTEGER NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL, amount REAL NOT NULL);
       CREATE INDEX usage_user ON usage (user_id, kind, at);""",
    # 8: a user's favourite lines (NULL: the local app's user): the run, the line ("lat1,lon1,lat2,lon2"
    # as the run's data.js holds it) and its values as the map sent them (JSON, for the list of all of them)
    """CREATE TABLE favourites (user_id INTEGER, run_id TEXT NOT NULL, line TEXT NOT NULL, data TEXT NOT NULL,
                                created TEXT);
       CREATE UNIQUE INDEX favourites_key ON favourites (IFNULL(user_id, 0), run_id, line);
       CREATE INDEX favourites_run ON favourites (run_id);""",
    # 9: folders of favourite lines (as a browser's bookmarks: nested, each favourite in one folder or at
    # the top level, NULL); the favourites of before stay at the top level
    """CREATE TABLE favourite_folders (id INTEGER PRIMARY KEY, user_id INTEGER, parent_id INTEGER,
                                       name TEXT NOT NULL, created TEXT);
       CREATE INDEX favourite_folders_user ON favourite_folders (IFNULL(user_id, 0));
       ALTER TABLE favourites ADD COLUMN folder_id INTEGER;""",
]
FAVOURITES_MAX = 10000  # favourite lines of a user (a safeguard of the database, the same for everyone)
FOLDERS_MAX = 1000      # folders of favourites of a user
FOLDER_DEPTH_MAX = 8    # folders inside one another (a top-level folder is at depth 1)
FOLDER_NAME_MAX = 100   # characters of a folder's name


# An absolute path on the server: C:\... (or C:/...), or /dir/... not in a URL (http://host/...: the
# slash after "host" follows a letter, the first of "//" another slash); after a colon too (path:/x)
ABSOLUTE_PATH = re.compile(r"""(?:\b[A-Za-z]:[\\/]|(?<![\w/.~-])/(?=[\w.-]))[^\s"'<>|]*""")
APP_DIR = os.path.dirname(os.path.abspath(__file__))


class Redact:
    """Removes server paths from texts for the public site: the data folder becomes relative
    ("runs/<id>"), the folder of the app and other absolute paths "<path>"."""

    def __init__(self, data_dir: str):
        folders = {os.path.abspath(data_dir), os.path.realpath(data_dir)}
        flags = re.IGNORECASE if os.name == "nt" else 0
        # Only at a path boundary (a separator, a quote, whitespace or the end), so a sibling such as
        # "<data>-old" is not taken for the data folder (it is still an absolute path: ABSOLUTE_PATH).
        # Best effort: a path with spaces after the folder is cut at the first space.
        self.data = [re.compile(re.escape(f) + r"""(?:[\\/]|(?=[\s"']|$))""", flags)
                     for f in self._variants(folders)]
        self.app = [re.compile(re.escape(f) + r"""(?=[\\/\s"']|$)[^\s"'<>|]*""", flags)
                    for f in self._variants({APP_DIR})]

    @staticmethod
    def _variants(folders) -> List[str]:
        found = {v for f in folders for v in (f, f.replace("\\", "/"), f.replace("/", "\\"))}
        return sorted(found, key=len, reverse=True)  # the longest first

    def __call__(self, value: Any) -> Any:
        """The value (a text, or texts in lists and dicts) without server paths."""
        if isinstance(value, str):
            for pattern in self.data:
                value = pattern.sub(lambda m: "" if m.group(0)[-1] in "\\/" else "<data folder>", value)
            for pattern in self.app:
                value = pattern.sub("<path>", value)
            return ABSOLUTE_PATH.sub("<path>", value)
        if isinstance(value, list):
            return [self(v) for v in value]
        if isinstance(value, dict):
            return {k: self(v) for k, v in value.items()}
        return value


class QuotaExceeded(ValueError):
    """A quota or a safeguard refuses a job or a request (`status`: the HTTP status, 429 for a user's
    quota, 503 for the server's safeguards). A ValueError, so code that goes on without an optional
    lookup (a run's place name, the country of --source auto) goes on without it."""

    def __init__(self, message: str, status: int = 429):
        super().__init__(message)
        self.status = status


DAY = 86400.0  # seconds of the daily quotas (a moving 24 hours)
USAGE = {"km2": "km² of search areas", "nominatim": "uncached Nominatim requests (place names, osm: areas)",
         "download_mb": "MB of downloads (the estimates of your searches)"}


def _in(values: Iterable) -> str:
    """Placeholders for an SQL IN (...) of these values."""
    return "(" + ", ".join("?" * len(tuple(values))) + ")"


class Jobs:
    """The queue and the events of the jobs (jobs.sqlite in the data folder)."""

    def __init__(self, data_dir: str, public: bool = False):
        self.data_dir = data_dir
        self.public = public
        # The public site: no server paths in events, errors and answers
        self.redact = Redact(data_dir) if public else (lambda value: value)
        self.path = os.path.join(data_dir, "jobs.sqlite")
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

    def folder(self, job_id: str) -> str:
        return os.path.join(self.data_dir, "work", "jobs", job_id)

    def add(self, request: Dict, title: str, user_id: Optional[int] = None,
            check: Optional[Callable[[sqlite3.Connection], None]] = None, unlimited: bool = False) -> str:
        """Queue a job of a user (None: the local app's); `check(db)` runs in the same write
        transaction before the job is added (a quota: it raises to refuse it); `unlimited`: a job
        of a user without limits (the worker's Limits do not apply to it, its time limits do)."""
        job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        created = datetime.now().isoformat(timespec="seconds")
        with self._write() as db:
            if check:
                check(db)
            db.execute("INSERT INTO jobs (id, created, status, request, title, user_id, unlimited) "
                       "VALUES (?, ?, 'queued', ?, ?, ?, ?)",
                       (job_id, created, json.dumps(request), title, user_id, int(unlimited)))
        self.event(job_id, "status", {"status": "queued", "title": title, "created": created})
        return job_id

    def admission(self, user_id: Optional[int], limits: Optional[Limits], limited: bool) -> Callable:
        """The check of a new job (for add(check=...), so it runs in the insert's transaction and two
        requests at once cannot both pass): the safeguards of the server for everyone (free disk,
        queue length), the quotas of `limits` for a limited user (jobs at once and per day, size of
        their runs, km² of the last 24 hours). Raises QuotaExceeded."""
        def check(db: sqlite3.Connection):
            if limits is None:
                return
            if limits.free_disk_gb_min is not None:
                free = shutil.disk_usage(self.data_dir).free / 1e9
                if free < limits.free_disk_gb_min:
                    raise QuotaExceeded("the server is short of disk space: no new searches for now", 503)
            active = WAITING + ACTIVE
            if limits.queue_max is not None:
                count = db.execute(f"SELECT COUNT(*) FROM jobs WHERE status IN {_in(active)}", active).fetchone()[0]
                if count >= limits.queue_max:
                    raise QuotaExceeded(f"the queue is full ({count} searches): try again later", 503)
            if not limited or user_id is None:
                return
            if limits.jobs_active_max is not None:
                count = db.execute(f"SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status IN {_in(active)}",
                                   (user_id, *active)).fetchone()[0]
                if count >= limits.jobs_active_max:
                    raise QuotaExceeded(f"at most {limits.jobs_active_max} of your searches may wait or run at once")
            if limits.jobs_day_max is not None:
                since = (datetime.now() - timedelta(seconds=DAY)).isoformat(timespec="seconds")
                count = db.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ? AND created >= ?",
                                   (user_id, since)).fetchone()[0]
                if count >= limits.jobs_day_max:
                    raise QuotaExceeded(f"at most {limits.jobs_day_max} searches in 24 hours: try again later")
            if limits.runs_gb_max is not None:
                size = db.execute("SELECT COALESCE(SUM(bytes), 0) FROM runs WHERE user_id = ?",
                                  (user_id,)).fetchone()[0]
                if size >= limits.runs_gb_max * 1e9:
                    raise QuotaExceeded(f"your runs take {size / 1e9:.1f} GB, at most {limits.runs_gb_max:g} GB: "
                                        f"delete some in Runs")
            if limits.km2_day_max is not None:
                used = self._used(db, user_id, "km2")
                if used >= limits.km2_day_max:
                    raise QuotaExceeded(f"your searches covered {used:.0f} km² in 24 hours, at most "
                                        f"{limits.km2_day_max:g} km²: try again later")
        return check

    def precheck(self, check: Callable[[sqlite3.Connection], None]):
        """Run an admission check (admission(...)) without adding a job: the web refuses a request
        over a quota before it validates it; add() checks again in its transaction."""
        db = self._db()
        try:
            check(db)
        finally:
            db.close()

    @staticmethod
    def _used(db: sqlite3.Connection, user_id: int, kind: str) -> float:
        return db.execute("SELECT COALESCE(SUM(amount), 0) FROM usage WHERE user_id = ? AND kind = ? AND at > ?",
                          (user_id, kind, time.time() - DAY)).fetchone()[0]

    def use(self, user_id: int, kind: str, amount: float, limit: Optional[float]):
        """Count what a user uses (USAGE: "km2", "nominatim") against its quota of 24 hours; over it,
        QuotaExceeded and nothing is counted (checked and counted in one write transaction)."""
        with self._write() as db:
            used = self._used(db, user_id, kind)
            if limit is not None and used + amount > limit:
                refused = QuotaExceeded(f"{USAGE[kind]}: {used:.0f} of at most {limit:g} in 24 hours used, "
                                        f"{amount:.1f} more needed: try again later")
            else:
                refused = None
                db.execute("INSERT INTO usage (user_id, kind, at, amount) VALUES (?, ?, ?, ?)",
                           (user_id, kind, time.time(), amount))
        if refused is not None:
            if self.public:  # the audit log of the public site (the worker's refusals)
                import audit
                audit.Audit(self.data_dir).log("refused", user_id, None, {"reason": str(refused), "status": 429})
            raise refused

    def usage(self, user_id: Optional[int], limits: Optional[Limits]) -> Dict:
        """A user's use of the quotas, as admission() and use() count it (the last 24 hours, the jobs
        waiting or running now, the size of the runs), each with its quota of `limits` (None: no
        quotas; then the counts of USAGE, kept only against a quota, are None as well). user_id None:
        the local app's jobs and runs (those without an owner)."""
        owner = "user_id IS ?" if user_id is None else "user_id = ?"
        active = WAITING + ACTIVE
        since = (datetime.now() - timedelta(seconds=DAY)).isoformat(timespec="seconds")
        db = self._db()
        try:
            counted = limits is not None and user_id is not None
            used = {kind: self._used(db, user_id, kind) if counted else None for kind in USAGE}
            jobs_active = db.execute(f"SELECT COUNT(*) FROM jobs WHERE {owner} AND status IN {_in(active)}",
                                     (user_id, *active)).fetchone()[0]
            jobs_day = db.execute(f"SELECT COUNT(*) FROM jobs WHERE {owner} AND created >= ?",
                                  (user_id, since)).fetchone()[0]
            runs_bytes = db.execute(f"SELECT COALESCE(SUM(bytes), 0) FROM runs WHERE {owner}",
                                    (user_id,)).fetchone()[0]
        finally:
            db.close()
        quota = (lambda name: getattr(limits, name)) if limits is not None else (lambda name: None)
        return {"limited": limits is not None, "hours": DAY / 3600, "usage": {
            "km2": {"used": used["km2"], "limit": quota("km2_day_max")},
            "download_mb": {"used": used["download_mb"], "limit": quota("download_mb_day_max")},
            "nominatim": {"used": used["nominatim"], "limit": quota("nominatim_day_max")},
            "jobs_day": {"used": jobs_day, "limit": quota("jobs_day_max")},
            "jobs_active": {"used": jobs_active, "limit": quota("jobs_active_max")},
            "runs_gb": {"used": runs_bytes / 1e9, "limit": quota("runs_gb_max")},
        }}

    def forget_usage(self) -> int:
        """Delete what users used before the last two days (the quotas look at 24 hours)."""
        with self._db() as db:
            return db.execute("DELETE FROM usage WHERE at < ?", (time.time() - 2 * DAY,)).rowcount

    def get(self, job_id: str) -> Optional[Dict]:
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def all(self, user_id: Optional[int] = None) -> List[Dict]:
        """The newest jobs, of one user (None: of everyone)."""
        mine = "" if user_id is None else "WHERE user_id = ? "
        with self._db() as db:
            rows = db.execute(f"SELECT id, created, status, title, run_id, error, finished, user_id FROM jobs {mine}"
                              "ORDER BY created DESC LIMIT 200", () if user_id is None else (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def waiting_or_running(self) -> List[Dict]:
        """The jobs not finished yet, with their requests."""
        with self._db() as db:
            rows = db.execute(f"SELECT * FROM jobs WHERE status IN {_in(WAITING + ACTIVE)}",
                              WAITING + ACTIVE).fetchall()
        return [dict(r) for r in rows]

    @contextlib.contextmanager
    def _write(self):
        """A connection in one BEGIN IMMEDIATE transaction: committed at the end, rolled back on an error."""
        db = self._db()
        db.isolation_level = None  # our own BEGIN / COMMIT
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    def claim(self, waiting: str, status: str, worker_id: Optional[str] = None) -> Optional[Dict]:
        """Take the oldest job of the status `waiting` and set it to `status` (None if there is none).

        One statement in one write transaction, so two workers never take the same job.
        """
        with self._write() as db:
            rows = db.execute("UPDATE jobs SET status = ?, worker_id = ? WHERE id = (SELECT id FROM jobs "
                              "WHERE status = ? AND cancel_requested = 0 ORDER BY created, rowid LIMIT 1) "
                              "AND status = ? RETURNING *", (status, worker_id, waiting, waiting)).fetchall()
        if not rows:
            return None
        self.event(rows[0]["id"], "status", {"status": status})
        return dict(rows[0])

    def set(self, job_id: str, status: str, when: Optional[Iterable[str]] = None, **fields) -> bool:
        """Set the status (and other columns) of a job; with `when` only if its status is one of
        those (e.g. a phase that ends must not overwrite a job cancelled meanwhile). Whether it changed."""
        if status in FINISHED:
            fields["finished"] = datetime.now().isoformat(timespec="seconds")
        if fields.get("error"):
            fields["error"] = self.redact(fields["error"])
        names = "".join(f", {k} = ?" for k in fields)
        condition, values = "", ()
        if when is not None:
            values = tuple(when)
            condition = f" AND status IN {_in(values)}"
        with self._db() as db:
            changed = db.execute(f"UPDATE jobs SET status = ?{names} WHERE id = ?{condition}",
                                 (status, *fields.values(), job_id, *values)).rowcount
        if changed:
            self.event(job_id, "status", {"status": status, **fields})
        return bool(changed)

    def event(self, job_id: str, kind: str, data):
        with self._db() as db:
            db.execute("INSERT INTO events (job, kind, data) VALUES (?, ?, ?)",
                       (job_id, kind, json.dumps(self.redact(data))))

    def job_events(self, job_id: str, limit: int = JOB_EVENTS_MAX) -> Dict:
        """The history of one job for its page: its newest `limit` events other than progress, and
        its last progress event, oldest first; "omitted": how many older messages were left out."""
        with self._db() as db:
            rows = db.execute("SELECT * FROM (SELECT * FROM events WHERE job = ? AND kind != 'progress' ORDER BY "
                              "seq DESC LIMIT ?) UNION ALL SELECT * FROM (SELECT * FROM events WHERE job = ? AND "
                              "kind = 'progress' ORDER BY seq DESC LIMIT 1) ORDER BY seq",
                              (job_id, limit, job_id)).fetchall()
            count = db.execute("SELECT COUNT(*) FROM events WHERE job = ? AND kind != 'progress'",
                               (job_id,)).fetchone()[0]
        return {"events": [{"seq": r["seq"], "job": r["job"], "kind": r["kind"], "data": json.loads(r["data"])}
                           for r in rows], "omitted": max(0, count - limit)}

    def events(self, since: int, job_id: Optional[str] = None, user_id: Optional[int] = None) -> List[Dict]:
        """The events after `since`: of one job, or of all jobs (of one user's jobs with user_id)."""
        with self._db() as db:
            if job_id:
                rows = db.execute("SELECT * FROM events WHERE seq > ? AND job = ? ORDER BY seq", (since, job_id))
            elif user_id is not None:
                rows = db.execute("SELECT e.* FROM events e JOIN jobs j ON j.id = e.job WHERE e.seq > ? AND "
                                  "j.user_id = ? ORDER BY e.seq LIMIT 1000", (since, user_id))
            else:
                rows = db.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT 1000", (since,))
            return [{"seq": r["seq"], "job": r["job"], "kind": r["kind"], "data": json.loads(r["data"])}
                    for r in rows.fetchall()]

    def last_seq(self, user_id: Optional[int] = None) -> int:
        """The id of the last event (of one user's jobs with user_id)."""
        with self._db() as db:
            if user_id is not None:
                return db.execute("SELECT COALESCE(MAX(e.seq), 0) FROM events e JOIN jobs j ON j.id = e.job "
                                  "WHERE j.user_id = ?", (user_id,)).fetchone()[0]
            return db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]

    # --- The runs: their owners and share links (web.py serves their files by these) ---

    def add_run(self, run_id: str, user_id: Optional[int], size: int):
        """A run saved by a job of a user (None: the local app's); the job's owner wins over an owner
        given by adopt_runs meanwhile."""
        with self._db() as db:
            db.execute("INSERT INTO runs (run_id, user_id, bytes, created) VALUES (?, ?, ?, ?) ON CONFLICT (run_id) "
                       "DO UPDATE SET user_id = excluded.user_id, bytes = excluded.bytes",
                       (run_id, user_id, size, datetime.now().isoformat(timespec="seconds")))

    def run(self, run_id: str) -> Optional[Dict]:
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def runs_of(self, user_id: int) -> Set[str]:
        """The ids of a user's runs."""
        with self._db() as db:
            return {r[0] for r in db.execute("SELECT run_id FROM runs WHERE user_id = ?", (user_id,))}

    def adopt_runs(self, runs_dir: str, user_id: Optional[int]) -> int:
        """Give the runs without an owner to `user_id` (the first administrator), as private runs:
        those of the folder without a row (saved before the public mode, or on the command line) and
        rows without a user (the local app's), with the local app's favourite lines. Runs in progress
        (no run.json yet) are left to their job. Nothing is deleted. Returns how many runs were given."""
        import runs as rn
        found = []
        if os.path.isdir(runs_dir):
            for entry in os.scandir(runs_dir):
                if entry.is_dir() and not entry.name.startswith((".", "_")) \
                        and os.path.isfile(os.path.join(entry.path, "run.json")):
                    found.append(entry.name)
        with self._write() as db:
            known = {r[0] for r in db.execute("SELECT run_id FROM runs")}
            now = datetime.now().isoformat(timespec="seconds")
            new = [run_id for run_id in found if run_id not in known]
            for run_id in new:
                db.execute("INSERT INTO runs (run_id, user_id, bytes, created) VALUES (?, ?, ?, ?)",
                           (run_id, user_id, rn.folder_bytes(os.path.join(runs_dir, run_id)), now))
            given = db.execute("UPDATE runs SET user_id = ? WHERE user_id IS NULL", (user_id,)).rowcount \
                if user_id is not None else 0
            if user_id is not None:  # the local app's favourites and their folders go with its runs
                db.execute("UPDATE OR IGNORE favourites SET user_id = ? WHERE user_id IS NULL", (user_id,))
                db.execute("UPDATE favourite_folders SET user_id = ? WHERE user_id IS NULL", (user_id,))
        return len(new) + given

    def set_share(self, run_id: str, share_hash: Optional[str]):
        """The sha256 of a run's share link (None: not shared); a new one replaces the old link."""
        with self._db() as db:
            db.execute("UPDATE runs SET share_hash = ? WHERE run_id = ?", (share_hash, run_id))

    def shared_runs(self, user_id: int) -> Set[str]:
        """The ids of a user's runs that have a share link."""
        with self._db() as db:
            return {r[0] for r in db.execute("SELECT run_id FROM runs WHERE user_id = ? AND share_hash IS NOT NULL",
                                             (user_id,))}

    def set_visibility(self, run_id: str, visibility: str) -> bool:
        """Who sees a run: "private" (its owner, administrators) or "demo" (everyone, also without
        signing in); whether the run has a row."""
        if visibility not in VISIBILITIES:
            raise ValueError(visibility)
        with self._db() as db:
            return bool(db.execute("UPDATE runs SET visibility = ? WHERE run_id = ?", (visibility, run_id)).rowcount)

    def delete_run(self, run_id: str, runs_dir: str):
        """Delete a run: its folder, then its row (its share link and demo flag go with it) and the
        favourite lines of everyone in it. A folder that cannot be deleted raises OSError and keeps its
        row (or adopt_runs would give it away)."""
        folder = os.path.join(runs_dir, run_id)
        if os.path.isdir(folder):
            shutil.rmtree(folder)
        with self._db() as db:
            db.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            db.execute("DELETE FROM favourites WHERE run_id = ?", (run_id,))

    # --- Favourite lines (the maps mark them, the app lists them) ---

    def favourites(self, user_id: Optional[int], run_id: Optional[str] = None) -> List[Dict]:
        """A user's favourite lines (of one run), newest first: run_id, line, data (a dict), created and
        folder_id (None: the top level)."""
        query, args = "SELECT run_id, line, data, created, folder_id FROM favourites WHERE user_id IS ?", [user_id]
        if run_id is not None:
            query += " AND run_id = ?"
            args.append(run_id)
        with self._db() as db:
            rows = db.execute(query + " ORDER BY created DESC, rowid DESC", args).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def set_favourite(self, user_id: Optional[int], run_id: str, line: str, data: Optional[Dict],
                      folder_id: Optional[int] = None):
        """Mark a line of a run as a favourite of a user (with its values `data`, in the user's folder
        `folder_id`, None: the top level), or unmark it (data None). Marking one more than FAVOURITES_MAX
        raises QuotaExceeded (409), a folder not the user's KeyError; marking a line again keeps the first
        values and its folder (move_favourite moves it)."""
        with self._write() as db:
            if data is None:
                db.execute("DELETE FROM favourites WHERE user_id IS ? AND run_id = ? AND line = ?",
                           (user_id, run_id, line))
                return
            self._own_folder(db, user_id, folder_id)
            count = db.execute("SELECT COUNT(*) FROM favourites WHERE user_id IS ?", (user_id,)).fetchone()[0]
            known = db.execute("SELECT 1 FROM favourites WHERE user_id IS ? AND run_id = ? AND line = ?",
                               (user_id, run_id, line)).fetchone()
            if not known and count >= FAVOURITES_MAX:
                raise QuotaExceeded(f"at most {FAVOURITES_MAX} favourite lines: remove some first", 409)
            db.execute("INSERT INTO favourites (user_id, run_id, line, data, created, folder_id) "
                       "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                       (user_id, run_id, line, json.dumps(data), datetime.now().isoformat(timespec="seconds"),
                        folder_id))

    def move_favourite(self, user_id: Optional[int], run_id: str, line: str, folder_id: Optional[int]):
        """Move a favourite of a user into the user's folder `folder_id` (None: the top level); KeyError
        when the favourite or the folder is not the user's."""
        with self._write() as db:
            self._own_folder(db, user_id, folder_id)
            if not db.execute("UPDATE favourites SET folder_id = ? WHERE user_id IS ? AND run_id = ? AND line = ?",
                              (folder_id, user_id, run_id, line)).rowcount:
                raise KeyError(line)

    # --- Folders of favourites (nested; deleting one deletes what is in it) ---

    def folders(self, user_id: Optional[int]) -> List[Dict]:
        """A user's folders of favourites: id, parent_id (None: the top level), name, created; by name."""
        with self._db() as db:
            rows = db.execute("SELECT id, parent_id, name, created FROM favourite_folders WHERE user_id IS ? "
                              "ORDER BY name COLLATE NOCASE, id", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _tree(db, user_id: Optional[int]) -> Dict[int, Optional[int]]:
        """The parent of each folder of a user."""
        return {r[0]: r[1] for r in db.execute("SELECT id, parent_id FROM favourite_folders WHERE user_id IS ?",
                                               (user_id,))}

    @classmethod
    def _own_folder(cls, db, user_id: Optional[int], folder_id: Optional[int]):
        """KeyError unless `folder_id` is None (the top level) or a folder of the user."""
        if folder_id is not None and not db.execute("SELECT 1 FROM favourite_folders WHERE id = ? AND user_id IS ?",
                                                    (folder_id, user_id)).fetchone():
            raise KeyError(folder_id)

    @staticmethod
    def _depth(parents: Dict[int, Optional[int]], folder_id: Optional[int]) -> int:
        """How deep a folder is (the top level 0, a folder in it 1)."""
        depth = 0
        while folder_id is not None:
            depth += 1
            folder_id = parents.get(folder_id)
        return depth

    @staticmethod
    def _subtree(parents: Dict[int, Optional[int]], folder_id: int) -> List[int]:
        """A folder and every folder inside it, at any depth."""
        found, todo = [], [folder_id]
        while todo:
            f = todo.pop()
            found.append(f)
            todo += [c for c, p in parents.items() if p == f]
        return found

    def add_folder(self, user_id: Optional[int], name: str, parent_id: Optional[int] = None) -> int:
        """A new folder of a user in `parent_id` (None: the top level); its id. KeyError for a parent not
        the user's, ValueError beyond FOLDER_DEPTH_MAX, QuotaExceeded (409) beyond FOLDERS_MAX."""
        with self._write() as db:
            self._own_folder(db, user_id, parent_id)
            parents = self._tree(db, user_id)
            if len(parents) >= FOLDERS_MAX:
                raise QuotaExceeded(f"at most {FOLDERS_MAX} folders: delete some first", 409)
            if self._depth(parents, parent_id) + 1 > FOLDER_DEPTH_MAX:
                raise ValueError(f"folders go at most {FOLDER_DEPTH_MAX} deep")
            return db.execute("INSERT INTO favourite_folders (user_id, parent_id, name, created) VALUES (?, ?, ?, ?)",
                              (user_id, parent_id, name, datetime.now().isoformat(timespec="seconds"))).lastrowid

    def change_folder(self, user_id: Optional[int], folder_id: int, name: Optional[str] = None,
                      parent_id: Optional[int] = None, move: bool = False):
        """Rename a user's folder (`name`) and/or move it into `parent_id` (`move`; None: the top level).
        KeyError for a folder not the user's; ValueError for a move into itself or its own folders, or
        beyond FOLDER_DEPTH_MAX."""
        with self._write() as db:
            self._own_folder(db, user_id, folder_id)
            if move:
                self._own_folder(db, user_id, parent_id)
                parents = self._tree(db, user_id)
                inside = self._subtree(parents, folder_id)
                if parent_id in inside:
                    raise ValueError("a folder cannot go into itself or one of its folders")
                height = max(self._depth(parents, f) for f in inside) - self._depth(parents, folder_id) + 1
                if self._depth(parents, parent_id) + height > FOLDER_DEPTH_MAX:
                    raise ValueError(f"folders go at most {FOLDER_DEPTH_MAX} deep")
                db.execute("UPDATE favourite_folders SET parent_id = ? WHERE id = ?", (parent_id, folder_id))
            if name is not None:
                db.execute("UPDATE favourite_folders SET name = ? WHERE id = ?", (name, folder_id))

    def delete_folder(self, user_id: Optional[int], folder_id: int) -> Dict[str, int]:
        """Delete a user's folder with the folders and favourite lines in it (as a browser's bookmarks);
        how many of each went. KeyError for a folder not the user's."""
        with self._write() as db:
            self._own_folder(db, user_id, folder_id)
            inside = self._subtree(self._tree(db, user_id), folder_id)
            marks = ",".join("?" * len(inside))
            lines = db.execute(f"DELETE FROM favourites WHERE user_id IS ? AND folder_id IN ({marks})",
                               (user_id, *inside)).rowcount
            db.execute(f"DELETE FROM favourite_folders WHERE id IN ({marks})", inside)
        return {"folders": len(inside), "favourites": lines}

    def all_runs(self) -> List[Dict]:
        """Every run's row (the admin tab), newest first."""
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT run_id, user_id, visibility, bytes, created, "
                                                "share_hash IS NOT NULL AS shared FROM runs ORDER BY created DESC")]

    def workers(self) -> List[Dict]:
        """The workers holding a lease (the admin tab)."""
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT id, pid, host, started FROM workers WHERE expires > ?",
                                                (time.time(),))]

    def demo_runs(self) -> Set[str]:
        """The ids of the demo runs."""
        with self._db() as db:
            return {r[0] for r in db.execute("SELECT run_id FROM runs WHERE visibility = 'demo'")}

    def shared_run(self, share_hash: str) -> Optional[str]:
        """The run of a share link's sha256 (None if no run has it)."""
        with self._db() as db:
            row = db.execute("SELECT run_id FROM runs WHERE share_hash = ?", (share_hash,)).fetchone()
        return row[0] if row else None

    # --- Workers and their leases ---

    def register(self, worker_id: str):
        """A worker starts: its lease."""
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO workers (id, pid, host, started, expires) VALUES (?, ?, ?, ?, ?)",
                       (worker_id, os.getpid(), socket.gethostname(), datetime.now().isoformat(timespec="seconds"),
                        time.time() + LEASE))

    def heartbeat(self, worker_id: str) -> bool:
        """Renew a worker's lease; False if it had expired already (its jobs may have been failed)."""
        now = time.time()
        with self._db() as db:
            return bool(db.execute("UPDATE workers SET expires = ? WHERE id = ? AND expires > ?",
                                   (now + LEASE, worker_id, now)).rowcount)

    def unregister(self, worker_id: str):
        with self._db() as db:
            db.execute("DELETE FROM workers WHERE id = ?", (worker_id,))

    def worker_alive(self) -> bool:
        """Whether any worker holds a lease (the page says so when none does)."""
        with self._db() as db:
            return db.execute("SELECT 1 FROM workers WHERE expires > ? LIMIT 1", (time.time(),)).fetchone() is not None

    def interrupted(self, worker_id: Optional[str] = None) -> List[str]:
        """Fail the running jobs of workers that are gone: whose lease expired (or of no worker,
        from before the leases), or those of `worker_id` when it stops. Returns their ids."""
        now = time.time()
        with self._write() as db:
            if worker_id:
                rows = db.execute(f"SELECT id FROM jobs WHERE status IN {_in(ACTIVE)} AND worker_id = ?",
                                  (*ACTIVE, worker_id))
            else:
                rows = db.execute(f"SELECT id FROM jobs WHERE status IN {_in(ACTIVE)} AND (worker_id IS NULL "
                                  "OR worker_id NOT IN (SELECT id FROM workers WHERE expires > ?))", (*ACTIVE, now))
            ids = [r[0] for r in rows.fetchall()]
            gone = json.dumps({"code": 503, "body": "the worker stopped"})
            if worker_id:
                db.execute("UPDATE quick SET status = 'done', answer = ? WHERE status = 'running' AND worker_id = ?",
                           (gone, worker_id))
            else:
                db.execute("UPDATE quick SET status = 'done', answer = ? WHERE status = 'running' AND worker_id NOT IN "
                           "(SELECT id FROM workers WHERE expires > ?)", (gone, now))
            db.execute("DELETE FROM workers WHERE expires <= ?", (now - LEASE,))  # long gone
        return [job_id for job_id in ids if self.set(job_id, "failed", when=ACTIVE, error=INTERRUPTED)]

    # --- The quick lane ---

    def quick_add(self, kind: str, request: Dict, user_id: Optional[int] = None, unlimited: bool = False,
                  one_waiting: bool = False) -> int:
        """A task for the quick lane (a kind of QUICK) of a user (`unlimited`: without the
        worker's Limits); its id. With `one_waiting` (the public site), QuotaExceeded while another
        task of the same user waits (checked in the insert's transaction)."""
        with self._write() as db:
            if one_waiting and user_id is not None and db.execute(
                    "SELECT 1 FROM quick WHERE user_id = ? AND status = 'waiting' LIMIT 1", (user_id,)).fetchone():
                raise QuotaExceeded("another estimate, profile or place lookup of yours is still waiting: try again when it "
                                    "is answered")
            return db.execute("INSERT INTO quick (kind, request, status, created, user_id, unlimited) "
                              "VALUES (?, ?, 'waiting', ?, ?, ?)",
                              (kind, json.dumps(request), time.time(), user_id, int(unlimited))).lastrowid

    def quick_forget_user(self, user_id: int) -> int:
        """Delete the waiting tasks of a user (blocked); how many."""
        with self._db() as db:
            return db.execute("DELETE FROM quick WHERE user_id = ? AND status = 'waiting'", (user_id,)).rowcount

    def quick_ahead(self, task_id: int) -> int:
        """Tasks of the quick lane waiting or running before this one."""
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM quick WHERE id < ? AND status IN ('waiting', 'running')",
                              (task_id,)).fetchone()[0]

    def quick_take(self, task_id: int) -> Optional[Dict]:
        """The answer of a task once it is there ({"code", "body"}), removing the task; else None."""
        with self._write() as db:
            row = db.execute("SELECT answer FROM quick WHERE id = ? AND status = 'done'", (task_id,)).fetchone()
            if row:
                db.execute("DELETE FROM quick WHERE id = ?", (task_id,))
        return json.loads(row["answer"]) if row else None

    def quick_forget(self, task_id: int):
        """Delete a task nobody waits for any more; a running one stays until it is answered (the
        lane's time limit applies to it) and is deleted with the other answers nobody took."""
        with self._db() as db:
            db.execute("DELETE FROM quick WHERE id = ? AND status != 'running'", (task_id,))

    def quick_claim(self, worker_id: str) -> Optional[Dict]:
        with self._write() as db:
            rows = db.execute("UPDATE quick SET status = 'running', worker_id = ?, started = ? WHERE id = "
                              "(SELECT id FROM quick WHERE status = 'waiting' ORDER BY id LIMIT 1) "
                              "AND status = 'waiting' RETURNING *", (worker_id, time.time())).fetchall()
        return dict(rows[0]) if rows else None

    def quick_running(self, worker_id: str) -> Optional[Dict]:
        with self._db() as db:
            row = db.execute("SELECT * FROM quick WHERE status = 'running' AND worker_id = ?", (worker_id,)).fetchone()
        return dict(row) if row else None

    def forget_quick(self, seconds: float = KEEP_QUICK_SECONDS) -> int:
        """Delete tasks of the quick lane nobody waits for any more: answers older than `seconds`
        nobody took, waiting tasks of a web process that stopped (KEEP_WAITING_QUICK_SECONDS; a web
        process deletes its own when it gives up). Waiting tasks someone awaits stay."""
        now = time.time()
        with self._db() as db:
            return db.execute("DELETE FROM quick WHERE (status = 'done' AND created < ?) OR (status = 'waiting' AND "
                              "created < ?)", (now - seconds, now - KEEP_WAITING_QUICK_SECONDS)).rowcount

    def forget_events(self, days: float) -> int:
        """Delete the events (messages, progress) of jobs finished more than `days` ago; the jobs stay."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._db() as db:
            return db.execute(f"DELETE FROM events WHERE job IN (SELECT id FROM jobs WHERE status IN {_in(FINISHED)} "
                              "AND finished < ?)", (*FINISHED, cutoff)).rowcount

    def quick_answer(self, task_id: int, code: int, body):
        """The answer of a running task."""
        with self._db() as db:
            db.execute("UPDATE quick SET status = 'done', answer = ? WHERE id = ? AND status = 'running'",
                       (json.dumps({"code": code, "body": self.redact(body)}), task_id))

    # --- Cancelling ---

    def request_cancel(self, job_id: str) -> bool:
        """Cancel a job: a waiting one (or one whose worker is gone) at once, a running one through
        its worker (cancel_requested). False if it is not waiting or running."""
        with self._write() as db:
            row = db.execute("SELECT status, worker_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row or row["status"] not in WAITING + ACTIVE:
                return False
            alive = db.execute("SELECT 1 FROM workers WHERE id = ? AND expires > ?",
                               (row["worker_id"], time.time())).fetchone()
            now = row["status"] in WAITING or not alive
            finished = datetime.now().isoformat(timespec="seconds")
            if now:
                db.execute("UPDATE jobs SET status = 'cancelled', finished = ? WHERE id = ?", (finished, job_id))
            else:
                db.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
        if now:
            self.event(job_id, "status", {"status": "cancelled", "finished": finished})
        return True

    def cancel_requested(self, job_ids: Iterable[str]) -> Set[str]:
        """Which of these jobs are to be cancelled; jobs that wait with the request are cancelled
        here (a download that ended just after it)."""
        job_ids = tuple(job_ids)
        with self._db() as db:
            waiting = [r[0] for r in db.execute(f"SELECT id FROM jobs WHERE cancel_requested = 1 AND status IN "
                                                f"{_in(WAITING)}", WAITING)]
            found = {r[0] for r in db.execute(f"SELECT id FROM jobs WHERE cancel_requested = 1 AND id IN "
                                              f"{_in(job_ids)}", job_ids)} if job_ids else set()
        for job_id in waiting:
            self.set(job_id, "cancelled", when=WAITING)
        return found


# --- The worker: one process per job and phase ------------------------------------------

class _EventWriter:
    """stdout of a worker: every line becomes an "info" event of the job."""

    def __init__(self, jobs: Jobs, job_id: str):
        self.jobs, self.job_id, self.buffer = jobs, job_id, ""

    def write(self, text: str):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                self.jobs.event(self.job_id, "info", line)
        return len(text)

    def flush(self):
        pass


def search_params(request: Dict) -> pm.SearchParams:
    """The parameters of a request, a drawn or uploaded area as their geometry (validated, no file)."""
    values = dict(request["params"])
    if request.get("geometry") is not None:
        values["geometry"] = request["geometry"]
    return pm.SearchParams(**values)


def ortho_confirm(limits: Limits, on_event):
    """search.Confirm of the worker: yes, except to orthophoto over Limits.ortho_minutes (the map
    loads it online then); sets search.CONFIRM_MINUTES to that limit."""
    import search
    if limits.ortho_minutes is None:
        return lambda kind, text: True
    search.CONFIRM_MINUTES = limits.ortho_minutes

    def confirm(kind: str, text: str) -> bool:
        if kind != "ortho":
            return True
        on_event("info", f"The orthophoto would take more than {limits.ortho_minutes:g} min to download: the map "
                         f"loads it online")
        return False
    return confirm


def nominatim_quota(jobs: Jobs, user_id: Optional[int], limits: Optional[Limits]):
    """Count the uncached Nominatim requests of this process's work (a job's phase, a quick task) for
    its user, against Limits.nominatim_day_max: osm.BEFORE_REQUEST, which refuses a request over the
    quota before it is sent (QuotaExceeded). None (no limits, no user): nothing is counted."""
    import osm
    osm.BEFORE_REQUEST = None
    if limits is not None and limits.nominatim_day_max is not None and user_id is not None:
        osm.BEFORE_REQUEST = lambda: jobs.use(user_id, "nominatim", 1, limits.nominatim_day_max)


def check_disk(data_dir: str, limits: Optional[Limits], phase: str):
    """Fail a phase while the disk of the data folder has less free space than Limits.free_disk_gb_min
    (a safeguard for everyone: also for users without limits)."""
    if limits is None or limits.free_disk_gb_min is None:
        return
    free = shutil.disk_usage(data_dir).free / 1e9
    if free < limits.free_disk_gb_min:
        raise QuotaExceeded(f"the server is short of disk space ({free:.0f} GB free): the search stopped before "
                            f"its {phase}; try again later", 503)


def charge_area(jobs: Jobs, user_id: Optional[int], plan, limits: Optional[Limits]):
    """Count the km² of a search's area for its user against Limits.km2_day_max (QuotaExceeded over
    it); known only here for osm:<name> and coast/cliffs areas. A circle counts as π r²."""
    from area import Polygon, size_km2
    if limits is None or limits.km2_day_max is None or user_id is None:
        return
    area = plan.area
    km2 = size_km2(area.geometry) if isinstance(area, Polygon) else math.pi * area.radius ** 2 / 1e6
    jobs.use(user_id, "km2", km2, limits.km2_day_max)


def run_phase(data_dir: str, job_id: str, phase: str, limits: Optional[Limits] = None, public: bool = False):
    """A worker process: download the data of a job, or compute it. It ends the phase only if the
    job is still in it (not cancelled or failed by the worker meanwhile). With limits, a search
    over them fails before its costly work (search.plan_search, check_estimate); a job of a user
    without limits gets none, and the user's quotas (km², Nominatim) are counted here."""
    import requests

    import progress as pg
    import search
    from progress import progress

    jobs = Jobs(data_dir, public)
    job = jobs.get(job_id)
    request = json.loads(job["request"])
    safeguards = limits  # of the server: for everyone
    if job["unlimited"]:
        limits = None  # a user without limits: the time limits of the phase (Scheduler) still apply
    nominatim_quota(jobs, job["user_id"], limits)
    sys.stdout = _EventWriter(jobs, job_id)
    pg.TEXT_INTERVAL = float("inf")  # the progress goes to the page as events, not as text
    last = [0.0]
    working = (PHASE_STATUS[phase],)

    def on_progress(state: Dict):
        if state["done"] or time.time() - last[0] >= PROGRESS_INTERVAL:
            last[0] = time.time()
            jobs.event(job_id, "progress", state)

    def on_event(kind: str, text: str):
        jobs.event(job_id, kind, text)

    progress.subscribe(on_progress)
    paths = search.Paths(data_dir, os.path.join(jobs.folder(job_id), phase))  # removed with the job when killed
    os.makedirs(paths.work, exist_ok=True)
    failed = True
    try:
        check_disk(data_dir, safeguards, "download" if phase == "download" else "computing")
        if "osm" in request:  # an OpenStreetMap extract: one phase only
            import osmextract
            region = request["osm"]["region"]
            if request["osm"]["action"] == "download":
                osmextract.download(paths.cache, region)
            else:
                osmextract.update(paths.cache, region, dict(osmextract.extracts(paths.cache))[region])
            jobs.set(job_id, "done", when=working, error=None)
            failed = False
            return
        params = search_params(request)
        # Tiles are sized from the free memory: the computing phase uses the size the
        # download phase announced, not one from the memory free by then
        tile_file = os.path.join(jobs.folder(job_id), "tile_core.json")
        if phase == "compute":
            params = params.model_copy(update={"update": None})  # updated while downloading
            if params.tile_size is None and os.path.exists(tile_file):
                with open(tile_file, encoding="utf-8") as f:
                    params = params.model_copy(update={"tile_size": json.load(f)})
        plan = search.plan_search(params, paths, on_event, limits=limits)
        plan.owner = job["user_id"]  # a tiled search resumes only for the same user
        if phase == "download":
            charge_area(jobs, job["user_id"], plan, limits)
            if limits and (limits.download_mb_max or limits.download_mb_day_max):  # the estimate asks the indexes
                estimate = search.estimate_search(plan)
                search.check_estimate(plan, estimate)
                if limits.download_mb_day_max and job["user_id"] is not None:  # the estimate, before the download
                    jobs.use(job["user_id"], "download_mb", estimate.download_mb, limits.download_mb_day_max)
            search.download_data(plan)
            if plan.tile_core:
                os.makedirs(jobs.folder(job_id), exist_ok=True)
                with open(tile_file, "w", encoding="utf-8") as f:
                    json.dump(plan.tile_core, f)
            jobs.set(job_id, "ready", when=working)
        else:
            lines, run = search.search_lines(plan)
            result = search.save_run(plan, lines, ortho_confirm(limits or Limits(), on_event), run)
            import runs as rn
            jobs.add_run(result.run["id"], job["user_id"], rn.folder_bytes(result.run["path"]))  # its owner
            jobs.set(job_id, "done", when=working, run_id=result.run["id"], error=None)
        failed = False
    except (search.SearchError, ValueError, RuntimeError, requests.RequestException) as e:  # ValueError: parameters
        jobs.set(job_id, "failed", when=working, error=cache.clean_error(e))
        failed = False
    except Exception as e:  # anything else: keep the work folder to look at (not on the public site)
        if public:  # the traceback (raised below) goes to the log, the page gets a reference
            print(f"internal error, ref {job_id} (its working grids are removed):", file=sys.stderr)
            jobs.set(job_id, "failed", when=working, error=f"internal error, ref {job_id}")
            failed = False  # the public site keeps no work folders: its disk is shared
        else:
            jobs.set(job_id, "failed", when=working,
                     error=f"{type(e).__name__}: {cache.clean_error(e)} (working grids kept in {paths.work})")
        raise
    finally:
        if not failed:
            import gc
            gc.collect()  # close the memory-mapped grids before deleting them (Windows)
            cache.remove_work(paths.work)
            if (jobs.get(job_id) or {}).get("status") in FINISHED:  # the job ended: tile_core.json is not needed
                shutil.rmtree(jobs.folder(job_id), ignore_errors=True)


def quick_estimate(data_dir: str, request: Dict, limits: Optional[Limits] = None) -> Dict:
    """The estimate of a search request (/api/estimate); a request over the limits is refused."""
    import search
    from area import Polygon, size_km2
    messages = []
    params = search_params(request)
    folder = os.path.join(data_dir, "work", "estimates", uuid.uuid4().hex)  # one per request
    try:
        paths = search.Paths(data_dir, folder)
        plan = search.plan_search(params, paths, lambda kind, text: messages.append([kind, text]), limits=limits)
        result = search.estimate_search(plan)
        search.check_estimate(plan, result)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return {"messages": messages, "reasons": result.reasons, "download_mb": round(result.download_mb),
            "work_gb": round(result.work_gb, 2), "source": plan.source.name,
            "tiles": len(plan.tiles) if plan.tiles else 1, "resolution": plan.resolution,
            "km2": size_km2(plan.area.geometry) if isinstance(plan.area, Polygon) else None,
            "area": plan.area.shape_wgs84().__geo_interface__}  # drawn on the map before the start


def quick_profile(data_dir: str, request: Dict, limits: Optional[Limits] = None) -> Dict:
    """The profile of a line of a saved run (/api/profile): {"run": id, "line": [lat1, lon1, lat2, lon2]}."""
    import search
    with open(os.path.join(data_dir, "runs", request["run"], "run.json"), encoding="utf-8") as f:
        meta = json.load(f)
    lat1, lon1, lat2, lon2 = request["line"]
    paths = search.Paths(data_dir, os.path.join(data_dir, "work", "profiles", uuid.uuid4().hex))
    os.makedirs(paths.work)
    try:
        return search.profile_of_line(meta, paths, (lat1, lon1), (lat2, lon2))
    finally:
        cache.remove_work(paths.work)


def quick_place(data_dir: str, request: Dict, limits: Optional[Limits] = None) -> Dict:
    """The places of a name (/api/place: {"q": name}) for the app's place box; Nominatim through
    osm.places (cached, counted by nominatim_quota)."""
    import osm
    return {"places": osm.places(request["q"], os.path.join(data_dir, "cache"))}


QUICK = {"estimate": quick_estimate, "profile": quick_profile, "place": quick_place}


def run_quick(data_dir: str, worker_id: str, parent: int, limits: Optional[Limits] = None, public: bool = False):
    """The process of the quick lane: serves the tasks of the table `quick` one after another, until
    its worker (`parent`) is gone. One process and one thread, so the state of search and sources
    (caches of sheet indexes, sag tables) is never shared between threads."""
    import traceback

    import psutil
    import requests

    import search
    jobs = Jobs(data_dir, public)
    looked = time.time()
    while True:
        task = jobs.quick_claim(worker_id)
        if task is None:
            if time.time() - looked > 2:
                looked = time.time()
                if not psutil.pid_exists(parent):
                    return
            time.sleep(QUICK_POLL)
            continue
        task_limits = None if task["unlimited"] else limits  # a user without limits: none
        nominatim_quota(jobs, task["user_id"], task_limits)
        try:
            jobs.quick_answer(task["id"], 200, QUICK[task["kind"]](data_dir, json.loads(task["request"]), task_limits))
        except QuotaExceeded as e:
            jobs.quick_answer(task["id"], e.status, str(e))
        except (ValueError, search.SearchError, requests.RequestException) as e:  # ValueError: parameters
            jobs.quick_answer(task["id"], 400, cache.clean_error(e))
        except Exception as e:
            ref = f"quick-{task['id']}"
            print(f"internal error, ref {ref}:", file=sys.stderr)
            traceback.print_exc()
            jobs.quick_answer(task["id"], 500, f"internal error, ref {ref}" if public else
                              f"internal error: {type(e).__name__}: {cache.clean_error(e)}")


class Running(NamedTuple):
    """A phase of a job in its process."""
    job_id: str
    process: object
    started: float
    limit: Optional[float] = None  # seconds (Limits.phase_seconds), None: no limit
    what: str = "phase"            # for the error over the limit: "the download phase", "the OSM extract job"


def _remove_old(folder: str, days: float, keep: Iterable[str] = ()):
    """Delete the entries of a folder not changed for `days`, except those named in `keep`."""
    limit, keep = time.time() - days * 86400, set(keep)
    if not os.path.isdir(folder):
        return
    for entry in os.scandir(folder):
        with contextlib.suppress(OSError):
            if entry.name not in keep and entry.stat().st_mtime < limit:
                shutil.rmtree(entry.path) if entry.is_dir() else os.remove(entry.path)


def kill_tree(pid: int):
    """Kill a process and every process it started (download pools, numba, GDAL helpers)."""
    import psutil
    try:
        parent = psutil.Process(pid)
        processes = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return
    for process in processes:
        with contextlib.suppress(psutil.NoSuchProcess):
            process.kill()
    psutil.wait_procs(processes, timeout=30)


class Scheduler:
    """The worker: starts the processes of the jobs, one downloading and one computing job at a time."""

    def __init__(self, jobs: Jobs, worker_id: Optional[str] = None, limits: Optional[Limits] = None,
                 public: bool = False):
        self.jobs = jobs
        self.public = public  # the worker of the public site
        self.limits = limits or Limits.load(public)  # FINDALINE_LIMIT_* (none locally), for its processes too
        self.quick = None  # the process of the quick lane
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.running: Dict[str, Running] = {}  # phase -> the job running in it
        self.lock = threading.Lock()
        self.context = multiprocessing.get_context("spawn")  # the same on every OS
        self.stopped = threading.Event()
        self.beat = 0.0  # time of the last heartbeat

    def start(self):
        """Run the worker in a thread of this process (findaline.py web)."""
        self._begin()
        threading.Thread(target=self._loop, daemon=True).start()

    def run(self):
        """Run the worker in this thread until stop() (findaline.py worker)."""
        self._begin()
        try:
            self._loop()
        finally:
            self.stop()

    def _begin(self):
        self.jobs.register(self.worker_id)
        self.beat = time.time()
        self.jobs.interrupted()  # jobs of workers that are gone

    def housekeeping(self):
        """Clean the data folder up: temporary files of downloads killed while writing; events of
        jobs finished more than Limits.keep_events_days ago; answers of the quick lane nobody took;
        work folders of ended jobs (kept after an unexpected error) after KEEP_WORK_DAYS, and those
        of estimates and profiles (left by a killed quick lane) after a day."""
        data = self.jobs.data_dir
        cache.remove_stale_parts(os.path.join(data, "cache"))
        import audit
        audit.purge(data)  # events of the audit log older than audit.KEEP_DAYS
        if self.limits.keep_events_days:
            self.jobs.forget_events(self.limits.keep_events_days)
        self.jobs.forget_quick()
        self.jobs.forget_usage()
        unfinished = {job["id"] for job in self.jobs.waiting_or_running()}
        _remove_old(os.path.join(data, "work", "jobs"), KEEP_WORK_DAYS, keep=unfinished)
        for name in ("estimates", "profiles"):
            _remove_old(os.path.join(data, "work", name), 1)

    def _remove_folder(self, job_id: str):
        """The work folder of a job whose processes were killed (their memory-mapped grids are closed)."""
        shutil.rmtree(self.jobs.folder(job_id), ignore_errors=True)

    def stop(self):
        """Stop gracefully: kill the running phases, mark their jobs interrupted, give up the lease."""
        self.stopped.set()
        with self.lock:
            self._kill_all()
            self.jobs.unregister(self.worker_id)

    def _kill_all(self):
        for phase, running in list(self.running.items()):
            kill_tree(running.process.pid)
            running.process.join()
            del self.running[phase]
            self._remove_folder(running.job_id)
        if self.quick is not None:
            kill_tree(self.quick.pid)
            self.quick.join()
            self.quick = None
        self.jobs.interrupted(self.worker_id)

    def _loop(self):
        cleaned = 0.0
        while not self.stopped.wait(TICK):
            if time.time() - cleaned >= HOUSEKEEPING:  # in a thread: it walks the whole cache
                cleaned = time.time()
                threading.Thread(target=self.housekeeping, daemon=True).start()
            with self.lock:
                if self.stopped.is_set():
                    break
                self._heartbeat()
                self._check_quick()
                self._check()

    def _heartbeat(self):
        if time.time() - self.beat < HEARTBEAT:
            return
        if not self.jobs.heartbeat(self.worker_id):
            # The lease expired (the computer slept, the database was locked): another worker may
            # have failed our jobs already; stop them and start over
            self._kill_all()
            self.jobs.register(self.worker_id)
        self.beat = time.time()
        self.jobs.interrupted()  # jobs of other workers that are gone

    def _check_quick(self):
        """Keep the quick lane running: a new process after a crash or a task over the time limit."""
        if self.quick is not None:
            task = self.jobs.quick_running(self.worker_id)
            if not self.quick.is_alive():
                self.quick.join()
                self.quick = None
                if task:
                    self.jobs.quick_answer(task["id"], 500, "the worker of estimates and profiles stopped")
            elif task and self.limits.quick_seconds and time.time() - task["started"] > self.limits.quick_seconds:
                kill_tree(self.quick.pid)
                self.quick.join()
                self.quick = None
                self.jobs.quick_answer(task["id"], 504, f"time limit: stopped after {self.limits.quick_seconds:g} s")
        if self.quick is None:
            self.quick = self.context.Process(target=run_quick, daemon=True,
                                              args=(self.jobs.data_dir, self.worker_id, os.getpid(), self.limits,
                                                    self.public))
            self.quick.start()

    def _kill(self, phase: str):
        running = self.running.pop(phase)
        kill_tree(running.process.pid)
        running.process.join()
        self._remove_folder(running.job_id)

    def _check(self):
        for job_id in self.jobs.cancel_requested(running.job_id for running in self.running.values()):
            for phase, running in list(self.running.items()):
                if running.job_id == job_id:
                    self._kill(phase)
            self._remove_folder(job_id)
            self.jobs.set(job_id, "cancelled", when=ACTIVE + WAITING)
        for phase, (job_id, process, started, limit, what) in list(self.running.items()):
            if limit and process.is_alive() and time.time() - started > limit:
                self._kill(phase)
                self.jobs.set(job_id, "failed", when=(PHASE_STATUS[phase],),
                              error=f"time limit: {what} was stopped after {limit:g} s")
                continue
            if process.is_alive():
                continue
            process.join()
            del self.running[phase]
            # Ended without a word. Only the job's status of this phase tells that: a download
            # process sets "ready" and may take a moment to exit, while the job already computes
            self.jobs.set(job_id, "failed", when=(PHASE_STATUS[phase],),
                          error=f"the worker stopped (exit code {process.exitcode})")
        for phase, waiting, status in (("download", "queued", "downloading"), ("compute", "ready", "computing")):
            if phase not in self.running:
                job = self.jobs.claim(waiting, status, self.worker_id)
                if job:
                    process = self.context.Process(target=run_phase, daemon=True,
                                                   args=(self.jobs.data_dir, job["id"], phase, self.limits,
                                                         self.public))
                    process.start()
                    osm = "osm" in json.loads(job["request"])  # an OSM extract: its own limit
                    self.running[phase] = Running(job["id"], process, time.time(),
                                                  self.limits.phase_seconds(phase, osm),
                                                  "the OSM extract job" if osm else f"the {phase} phase")


def warn_without_cgroup(root: Optional[str] = None) -> bool:
    """The public worker sizes tiles and threads from the limits of its container (search.cgroup_limits,
    cgroup v2 only): a warning on stderr when it finds none (cgroup v1, or no mem_limit / cpus).
    Whether it warned."""
    import search
    found = search.cgroup_limits(root)
    if found.memory is not None and found.cpus is not None:
        return False
    v2 = os.path.exists(os.path.join(root or search.CGROUP_ROOT, "cgroup.controllers"))
    print("WARNING: " + ("no memory or CPU limit of the container found (cgroup v2: memory.max, cpu.max)" if v2 else
                         "no cgroup v2 found (cgroup v1 is not read)") + ": tiles and threads are sized from the "
          "whole machine; run the worker in a container with mem_limit and cpus on a host with cgroup v2",
          file=sys.stderr, flush=True)
    return True


def main(argv: List[str]) -> int:
    """findaline.py worker: the worker of the web app as its own process."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="findaline.py worker",
        description="The worker of the web app: runs the queued searches (start the server with "
                    "`findaline.py web --no-worker`).")
    parser.add_argument("--data-dir", help="data folder (default: FINDALINE_DATA or the folder of the scripts)")
    parser.add_argument("--public", action="store_true",
                        help="the worker of the public site (`findaline.py web --public`): needs the same settings "
                             "(FINDALINE_PUBLIC_URL, FINDALINE_TRUSTED_PROXY, FINDALINE_CONTACT)")
    args = parser.parse_args(argv)
    if args.public:
        # The site's address and FINDALINE_CONTACT go into the User-Agent of the worker's requests
        # (cache.http_headers reads the environment, which the worker's processes inherit; a
        # Docker secret is put there too)
        public = PublicSettings.load()
        os.environ["FINDALINE_PUBLIC_URL"], os.environ["FINDALINE_CONTACT"] = public.public_url, public.contact
    data = cache.data_dir(args.data_dir)
    cache.check_layout(os.path.join(data, "cache"))
    if args.public:
        warn_without_cgroup()
    scheduler = Scheduler(Jobs(data, public=args.public), public=args.public)
    # SIGTERM (docker stop, systemd) stops it like Ctrl+C: the running phases are killed and
    # their jobs marked interrupted (a tiled area resumes when it is started again)
    for name in ("SIGTERM", "SIGBREAK"):  # SIGBREAK: Ctrl+Break, or a service stopped on Windows
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: scheduler.stopped.set())
    print(f"Find-a-Line worker {scheduler.worker_id} is running on {data} (Ctrl+C to stop it)", flush=True)
    try:
        scheduler.run()
    except KeyboardInterrupt:
        pass
    return 0
