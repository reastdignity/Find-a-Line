"""
The job queue of the web application and its worker processes.

- Jobs: the queue and the events of the jobs (jobs.sqlite in the data folder).
- Scheduler: starts one process per job and phase (run_phase): one downloading and one
  computing job at a time.
- run_phase: a worker process; its stdout becomes "info" events (_EventWriter), the
  progress goes as "progress" events.
"""

import json
import multiprocessing
import os
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import cache
import params as pm

TICK = 0.5                # seconds between looks at the queue
PROGRESS_INTERVAL = 0.3   # seconds between progress events of a job
# What a job is doing; "ready" means its data is downloaded and it waits to compute
ACTIVE = ("downloading", "computing")
PHASE_STATUS = {"download": "downloading", "compute": "computing"}  # the status each phase works in
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, created TEXT, status TEXT, request TEXT, title TEXT,
    run_id TEXT, error TEXT, finished TEXT);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, kind TEXT, data TEXT);
"""


class Jobs:
    """The queue and the events of the jobs (jobs.sqlite in the data folder)."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "jobs.sqlite")
        with self._db() as db:
            db.executescript(SCHEMA)

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

    def next(self, status: str) -> Optional[Dict]:
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE status = ? ORDER BY created LIMIT 1", (status,)).fetchone()
        return dict(row) if row else None

    def set(self, job_id: str, status: str, **fields):
        if status in ("done", "failed", "cancelled"):
            fields["finished"] = datetime.now().isoformat(timespec="seconds")
        names = ", ".join(f"{k} = ?" for k in fields)
        with self._db() as db:
            db.execute(f"UPDATE jobs SET status = ?{', ' + names if names else ''} WHERE id = ?",
                       (status, *fields.values(), job_id))
        self.event(job_id, "status", {"status": status, **fields})

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

    def interrupted(self):
        """Jobs that were running when the app was closed."""
        with self._db() as db:
            ids = [r[0] for r in db.execute(f"SELECT id FROM jobs WHERE status IN {ACTIVE}")]
        for job_id in ids:
            self.set(job_id, "failed", error="interrupted: the app was closed (a tiled area resumes "
                                             "where it stopped when it is started again)")


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
    """A worker process: download the data of a job, or compute it."""
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

    def on_progress(state: Dict):
        if state["done"] or time.time() - last[0] >= PROGRESS_INTERVAL:
            last[0] = time.time()
            jobs.event(job_id, "progress", state)

    def on_event(kind: str, text: str):
        jobs.event(job_id, kind, text)

    progress.subscribe(on_progress)
    paths = search.Paths(data_dir, cache.new_work(data_dir))
    failed = True
    try:
        if "osm" in request:  # an OpenStreetMap extract: one phase only
            import osmextract
            region = request["osm"]["region"]
            if request["osm"]["action"] == "download":
                osmextract.download(paths.cache, region)
            else:
                osmextract.update(paths.cache, region, dict(osmextract.extracts(paths.cache))[region])
            jobs.set(job_id, "done", error=None)
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
            jobs.set(job_id, "ready")
        else:
            lines, run = search.search_lines(plan)
            result = search.save_run(plan, lines, lambda kind, text: True, run)
            jobs.set(job_id, "done", run_id=result.run["id"], error=None)
        failed = False
    except (search.SearchError, ValueError, RuntimeError, requests.RequestException) as e:  # ValueError: parameters
        jobs.set(job_id, "failed", error=str(e))
        failed = False
    except Exception as e:  # anything else: keep the work folder to look at
        jobs.set(job_id, "failed", error=f"{type(e).__name__}: {e} (working grids kept in {paths.work})")
        raise
    finally:
        if not failed:
            import gc
            gc.collect()  # close the memory-mapped grids before deleting them (Windows)
            cache.remove_work(paths.work)


class Scheduler:
    """Starts the worker processes: one downloading and one computing job at a time."""

    def __init__(self, jobs: Jobs):
        self.jobs = jobs
        self.running: Dict[str, tuple] = {}  # phase -> (job id, process)
        self.lock = threading.Lock()
        self.context = multiprocessing.get_context("spawn")  # the same on every OS
        self.stopped = threading.Event()

    def start(self):
        self.jobs.interrupted()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.stopped.set()
        with self.lock:
            for _, process in self.running.values():
                process.kill()

    def _loop(self):
        while not self.stopped.wait(TICK):
            with self.lock:
                self._check()

    def _check(self):
        for phase, (job_id, process) in list(self.running.items()):
            if process.is_alive():
                continue
            process.join()
            del self.running[phase]
            job = self.jobs.get(job_id)
            # Ended without a word. Only the job's status of this phase tells that: a download
            # process sets "ready" and may take a moment to exit, while the job already computes
            if job and job["status"] == PHASE_STATUS[phase]:
                self.jobs.set(job_id, "failed", error=f"the worker stopped (exit code {process.exitcode})")
        for phase, waiting, status in (("download", "queued", "downloading"), ("compute", "ready", "computing")):
            if phase not in self.running:
                job = self.jobs.next(waiting)
                if job:
                    self.jobs.set(job["id"], status)
                    process = self.context.Process(target=run_phase, args=(self.jobs.data_dir, job["id"], phase),
                                                   daemon=True)
                    process.start()
                    self.running[phase] = (job["id"], process)

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["status"] in ("done", "failed", "cancelled"):
                return False
            for phase, (running_id, process) in list(self.running.items()):
                if running_id == job_id:
                    process.kill()
                    process.join()
                    del self.running[phase]
            self.jobs.set(job_id, "cancelled")
            return True
