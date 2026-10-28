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
- The quick lane: estimates and profiles of the page (short, but they may call Nominatim
  or Overpass, read sheet indexes and build grids) are rows of the table `quick`, which
  one long-lived process of the worker (run_quick) serves one after another; the web
  server only adds a row and waits for its answer. A task that takes longer than the
  time limit gets its process killed and a new one started.
"""

import contextlib
import json
import multiprocessing
import os
import shutil
import signal
import socket
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, NamedTuple, Optional, Set

import cache
import params as pm
from limits import Limits, PublicSettings

TICK = 0.5                # seconds between looks at the queue
PROGRESS_INTERVAL = 0.3   # seconds between progress events of a job
HEARTBEAT = 5.0           # seconds between renewals of a worker's lease
HOUSEKEEPING = 6 * 3600   # seconds between clean-ups of the data folder (and at the start)
QUICK_POLL = 0.05         # seconds between looks of the quick lane at its table
KEEP_WORK_DAYS = 7        # days the work folder of a job that failed unexpectedly is kept (to look at)
KEEP_QUICK_SECONDS = 3600  # an answer of the quick lane nobody took (the page was closed) is deleted after it
LEASE = 30.0              # seconds a lease lasts without a renewal
# What a job is doing; "ready" means its data is downloaded and it waits to compute
ACTIVE = ("downloading", "computing")
WAITING = ("queued", "ready")
FINISHED = ("done", "failed", "cancelled")
PHASE_STATUS = {"download": "downloading", "compute": "computing"}  # the status each phase works in
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
]


def _in(values: Iterable) -> str:
    """Placeholders for an SQL IN (...) of these values."""
    return "(" + ", ".join("?" * len(tuple(values))) + ")"


class Jobs:
    """The queue and the events of the jobs (jobs.sqlite in the data folder)."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
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

    def add(self, request: Dict, title: str) -> str:
        job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        created = datetime.now().isoformat(timespec="seconds")
        with self._db() as db:
            db.execute("INSERT INTO jobs (id, created, status, request, title) VALUES (?, ?, 'queued', ?, ?)",
                       (job_id, created, json.dumps(request), title))
        self.event(job_id, "status", {"status": "queued", "title": title, "created": created})
        return job_id

    def get(self, job_id: str) -> Optional[Dict]:
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def all(self) -> List[Dict]:
        with self._db() as db:
            rows = db.execute("SELECT id, created, status, title, run_id, error, finished FROM jobs "
                              "ORDER BY created DESC LIMIT 200").fetchall()
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
            db.execute("INSERT INTO events (job, kind, data) VALUES (?, ?, ?)", (job_id, kind, json.dumps(data)))

    def events(self, since: int, job_id: Optional[str] = None) -> List[Dict]:
        with self._db() as db:
            if job_id:
                rows = db.execute("SELECT * FROM events WHERE seq > ? AND job = ? ORDER BY seq", (since, job_id))
            else:
                rows = db.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT 1000", (since,))
            return [{"seq": r["seq"], "job": r["job"], "kind": r["kind"], "data": json.loads(r["data"])}
                    for r in rows.fetchall()]

    def last_seq(self) -> int:
        with self._db() as db:
            return db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]

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

    def quick_add(self, kind: str, request: Dict) -> int:
        """A task for the quick lane ("estimate" or "profile"); its id."""
        with self._db() as db:
            return db.execute("INSERT INTO quick (kind, request, status, created) VALUES (?, ?, 'waiting', ?)",
                              (kind, json.dumps(request), time.time())).lastrowid

    def quick_take(self, task_id: int) -> Optional[Dict]:
        """The answer of a task once it is there ({"code", "body"}), removing the task; else None."""
        with self._write() as db:
            row = db.execute("SELECT answer FROM quick WHERE id = ? AND status = 'done'", (task_id,)).fetchone()
            if row:
                db.execute("DELETE FROM quick WHERE id = ?", (task_id,))
        return json.loads(row["answer"]) if row else None

    def quick_forget(self, task_id: int):
        with self._db() as db:
            db.execute("DELETE FROM quick WHERE id = ?", (task_id,))

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
        """Delete tasks of the quick lane nobody waits for any more (older than `seconds`, not running)."""
        with self._db() as db:
            return db.execute("DELETE FROM quick WHERE created < ? AND status != 'running'",
                              (time.time() - seconds,)).rowcount

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
                       (json.dumps({"code": code, "body": body}), task_id))

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


def run_phase(data_dir: str, job_id: str, phase: str):
    """A worker process: download the data of a job, or compute it. It ends the phase only if the
    job is still in it (not cancelled or failed by the worker meanwhile)."""
    import requests

    import progress as pg
    import search
    from progress import progress

    jobs = Jobs(data_dir)
    job = jobs.get(job_id)
    request = json.loads(job["request"])
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
        plan = search.plan_search(params, paths, on_event)
        if phase == "download":
            search.download_data(plan)
            if plan.tile_core:
                os.makedirs(jobs.folder(job_id), exist_ok=True)
                with open(tile_file, "w", encoding="utf-8") as f:
                    json.dump(plan.tile_core, f)
            jobs.set(job_id, "ready", when=working)
        else:
            lines, run = search.search_lines(plan)
            result = search.save_run(plan, lines, lambda kind, text: True, run)
            jobs.set(job_id, "done", when=working, run_id=result.run["id"], error=None)
        failed = False
    except (search.SearchError, ValueError, RuntimeError, requests.RequestException) as e:  # ValueError: parameters
        jobs.set(job_id, "failed", when=working, error=cache.clean_error(e))
        failed = False
    except Exception as e:  # anything else: keep the work folder to look at
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


def quick_estimate(data_dir: str, request: Dict) -> Dict:
    """The estimate of a search request (/api/estimate)."""
    import search
    from area import Polygon, size_km2
    messages = []
    params = search_params(request)
    folder = os.path.join(data_dir, "work", "estimates", uuid.uuid4().hex)  # one per request
    try:
        paths = search.Paths(data_dir, folder)
        plan = search.plan_search(params, paths, lambda kind, text: messages.append([kind, text]))
        result = search.estimate_search(plan)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return {"messages": messages, "reasons": result.reasons, "download_mb": round(result.download_mb),
            "work_gb": round(result.work_gb, 2), "source": plan.source.name,
            "tiles": len(plan.tiles) if plan.tiles else 1, "resolution": plan.resolution,
            "km2": size_km2(plan.area.geometry) if isinstance(plan.area, Polygon) else None,
            "area": plan.area.shape_wgs84().__geo_interface__}  # drawn on the map before the start


def quick_profile(data_dir: str, request: Dict) -> Dict:
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


QUICK = {"estimate": quick_estimate, "profile": quick_profile}


def run_quick(data_dir: str, worker_id: str, parent: int):
    """The process of the quick lane: serves the tasks of the table `quick` one after another, until
    its worker (`parent`) is gone. One process and one thread, so the state of search and sources
    (caches of sheet indexes, sag tables) is never shared between threads."""
    import traceback

    import psutil
    import requests

    import search
    jobs = Jobs(data_dir)
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
        try:
            jobs.quick_answer(task["id"], 200, QUICK[task["kind"]](data_dir, json.loads(task["request"])))
        except (ValueError, search.SearchError, requests.RequestException) as e:  # ValueError: parameters
            jobs.quick_answer(task["id"], 400, cache.clean_error(e))
        except Exception as e:
            traceback.print_exc()
            jobs.quick_answer(task["id"], 500, f"internal error: {type(e).__name__}: {cache.clean_error(e)}")


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
        self.limits = limits or Limits()  # time limits (FINDALINE_LIMIT_*; none locally)
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
        if self.limits.keep_events_days:
            self.jobs.forget_events(self.limits.keep_events_days)
        self.jobs.forget_quick()
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
            self.quick = self.context.Process(target=run_quick, args=(self.jobs.data_dir, self.worker_id, os.getpid()),
                                              daemon=True)
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
                    process = self.context.Process(target=run_phase, args=(self.jobs.data_dir, job["id"], phase),
                                                   daemon=True)
                    process.start()
                    osm = "osm" in json.loads(job["request"])  # an OSM extract: its own limit
                    self.running[phase] = Running(job["id"], process, time.time(),
                                                  self.limits.phase_seconds(phase, osm),
                                                  "the OSM extract job" if osm else f"the {phase} phase")


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
        # FINDALINE_CONTACT goes into the User-Agent of the worker's requests (cache.py reads the
        # environment, which the worker's processes inherit; a Docker secret is put there too)
        os.environ["FINDALINE_CONTACT"] = PublicSettings.load().contact
    data = cache.data_dir(args.data_dir)
    cache.check_layout(os.path.join(data, "cache"))
    scheduler = Scheduler(Jobs(data), public=args.public)
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
