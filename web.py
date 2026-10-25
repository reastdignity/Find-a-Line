"""
The local web application: `python findaline.py web` starts it on 127.0.0.1 and
opens the browser.

- The page (viewer/src/app) picks a point or draws an area, fills in the parameters
  (the form comes from params.SearchParams), shows the estimate and queues the search.
- Jobs wait in a queue (jobs.sqlite in the data folder). Each job runs in its own
  process, in two phases: downloading its data (search.download_data) and computing
  (the search itself). One job computes at a time, as it uses all cores and much
  memory; the next job may download its data meanwhile. Jobs can be cancelled.
- OpenStreetMap extracts (osmextract.py) are downloaded and updated as jobs too, so the
  coastline and cliffs of a region need no Overpass.
- Messages and progress of the jobs are stored as events and sent to the page as
  Server-Sent Events. Results are ordinary runs: the page opens runs/<id>/map.html.

Security (docs/roadmap.md, "Security"): the server listens on 127.0.0.1 only, but any
website the user visits could send requests to it, so it accepts only its own Host
names, and every request that changes something needs the session token of the page
(a header a foreign website cannot set). File paths never come from the web: the
cli_only fields are refused, and a drawn or uploaded area is saved by the server.
"""

import asyncio
import contextlib
import json
import multiprocessing
import os
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from typing import Dict, List, Optional

import cache
import params as pm

PORT = 8765
TICK = 0.5                # seconds between looks at the queue
PROGRESS_INTERVAL = 0.3   # seconds between progress events of a job
MAX_UPLOAD = 5_000_000    # bytes of an uploaded area file
APP_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer", "dist")
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


def search_params(request: Dict, folder: str) -> pm.SearchParams:
    """The parameters of a request; a drawn or uploaded area is saved in the job folder."""
    values = dict(request["params"])
    if request.get("geometry"):
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "area.geojson")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(request["geometry"], f)
        values["area"] = path
    return pm.SearchParams(**values)


def run_phase(data_dir: str, job_id: str, phase: str):
    """A worker process: download the data of a job, or compute it."""
    import requests

    import progress as pg
    import runs as rn
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
        params = search_params(request, jobs.folder(job_id))
        if phase == "compute":
            params = params.model_copy(update={"update": None})  # updated while downloading
        plan = search.plan_search(params, paths, on_event)
        if phase == "download":
            search.download_data(plan)
            jobs.set(job_id, "ready")
        else:
            if request.get("geometry") and not params.name:
                name = rn.place_name(*plan.area.center, paths.cache) or "drawn area"
                plan.params = params.model_copy(update={"name": name})
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


# --- The HTTP side ----------------------------------------------------------------------

def schema() -> List[Dict]:
    """The fields of SearchParams the page may set (cli_only fields are left out)."""
    import typing
    fields = []
    for name, info in pm.SearchParams.model_fields.items():
        extra = info.json_schema_extra or {}
        if extra.get("cli_only"):
            continue
        kind, choices = info.annotation, None
        if typing.get_origin(kind) is typing.Union:
            kind = next(a for a in typing.get_args(kind) if a is not type(None))
        if typing.get_origin(kind) is typing.Literal:
            choices, kind = list(typing.get_args(kind)), "choice"
        elif name == "source":
            choices, kind = pm.SOURCE_CHOICES[:1] + pm.SOURCE_CHOICES[2:], "choice"  # no local files
        else:
            kind = {float: "number", int: "integer", bool: "boolean", str: "text"}.get(kind, "text")
        default = None if info.default_factory else info.default
        fields.append({"name": name, "type": kind, "choices": choices, "default": default,
                       "help": info.description, "group": extra.get("group"),
                       "optional": not info.is_required() and default is None})
    return fields


def check_request(request: Dict) -> Dict:
    """Refuse what the web must not set; returns the request with known keys only."""
    params = request.get("params")
    if not isinstance(params, dict):
        raise ValueError("params missing")
    allowed = {f["name"] for f in schema()}
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"not allowed: {', '.join(sorted(unknown))}")
    area = params.get("area")
    if area is not None and area not in ("coast", "cliffs") and not str(area).startswith("osm:"):
        raise ValueError("area: coast, cliffs, osm:<name> or a drawn/uploaded geometry")
    geometry = request.get("geometry")
    if geometry is not None and (not isinstance(geometry, dict) or "type" not in geometry):
        raise ValueError("geometry: GeoJSON expected")
    if geometry is not None and area is not None:
        raise ValueError("either area or geometry")
    return {"params": params, "geometry": geometry}


def title(request: Dict) -> str:
    p = request["params"]
    if request.get("geometry"):
        where = "drawn area"
    elif p.get("area"):
        where = p["area"]
    elif p.get("lat") is not None:
        where = f"{p['lat']:.5f}, {p['lon']:.5f}"
    else:
        where = "?"
    return (p.get("name") or where) + (f" (r {p['radius']:g} m)" if p.get("radius") and not p.get("area")
                                       and not request.get("geometry") else "")


def create_app(data_dir: str, token: Optional[str] = None, port: int = PORT, start_scheduler: bool = True):
    """The FastAPI application (tests create it without the scheduler)."""
    from fastapi import FastAPI, HTTPException, Request, UploadFile
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    import runs as rn
    import search

    token = token or secrets.token_urlsafe(24)
    jobs = Jobs(data_dir)
    scheduler = Scheduler(jobs)

    @contextlib.asynccontextmanager
    async def lifespan(_):
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="Find-a-Line", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.jobs, app.state.scheduler, app.state.token = jobs, scheduler, token
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "testserver"}
    cache_dir = os.path.join(data_dir, "cache")
    runs_dir = os.path.join(data_dir, "runs")
    os.makedirs(runs_dir, exist_ok=True)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Only our own host names (DNS rebinding), and the token for anything that changes state (CSRF)
        if request.headers.get("host") not in hosts:
            return JSONResponse({"detail": "unknown host"}, status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-findaline-token") != token:
            return JSONResponse({"detail": "missing or wrong token"}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        # The viewer and the app change with the code, run data with every run: always revalidate
        # (otherwise a browser may keep an old viewer.js next to a new data.js)
        response.headers.setdefault("Cache-Control", "no-cache")
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/", response_class=HTMLResponse)
    def page():
        return APP_HTML.format(token=token)

    @app.get("/api/schema")
    def get_schema():
        return {"fields": schema(), "groups": pm.GROUPS}

    def parse(body: Dict) -> Dict:
        try:
            return check_request(body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/estimate")
    async def estimate(body: Dict):
        request = parse(body)

        def work():
            messages = []
            folder = os.path.join(data_dir, "work", "estimates", uuid.uuid4().hex)
            try:
                params = search_params(request, folder)
                paths = search.Paths(data_dir, os.path.join(folder, "work"))
                plan = search.plan_search(params, paths, lambda kind, text: messages.append([kind, text]))
                result = search.estimate_search(plan)
            finally:
                shutil.rmtree(folder, ignore_errors=True)
            from area import Polygon, size_km2
            return {"messages": messages, "reasons": result.reasons, "download_mb": round(result.download_mb),
                    "work_gb": round(result.work_gb, 2), "source": plan.source.name,
                    "tiles": len(plan.tiles) if plan.tiles else 1, "resolution": plan.resolution,
                    "km2": size_km2(plan.area.geometry) if isinstance(plan.area, Polygon) else None,
                    "area": plan.area.shape_wgs84().__geo_interface__}  # drawn on the map before the start
        try:
            return await run_in_threadpool(work)
        except (ValueError, search.SearchError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/jobs")
    async def add_job(body: Dict):
        request = parse(body)
        try:
            await run_in_threadpool(search_params, request, os.path.join(data_dir, "work", "check"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        finally:
            shutil.rmtree(os.path.join(data_dir, "work", "check"), ignore_errors=True)
        return {"id": jobs.add(request, title(request))}

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": jobs.all(), "seq": jobs.last_seq()}

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str):
        return {"events": jobs.events(0, job_id)}

    @app.delete("/api/jobs/{job_id}")
    def cancel(job_id: str):
        if not scheduler.cancel(job_id):
            raise HTTPException(409, "the job is not waiting or running")
        return {"ok": True}

    @app.get("/api/events")
    async def events(request: Request, since: int = 0):
        """All job events after `since`, as Server-Sent Events."""
        # A reconnecting EventSource sends the last id it got
        last = request.headers.get("last-event-id", "")
        start = max(since, int(last) if last.isdigit() else 0)

        async def stream():
            seq = start
            while not await request.is_disconnected():
                for event in await run_in_threadpool(jobs.events, seq):
                    seq = event["seq"]
                    yield f"id: {seq}\ndata: {json.dumps(event)}\n\n"
                await asyncio.sleep(TICK)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/runs")
    def list_runs():
        return {"runs": [{k: r.get(k) for k in ("id", "name", "created", "summary", "has_map")}
                         | {"description": rn.params_summary(r)} for r in rn.list_runs(runs_dir)]}

    @app.get("/api/osm")
    async def osm_regions(lat: float, lon: float):
        """The Geofabrik regions at a point and the downloaded extracts."""
        import osmextract

        def work():
            return {"regions": osmextract.regions_at(cache_dir, lat, lon),
                    "extracts": osmextract.downloaded(cache_dir)}
        try:
            return await run_in_threadpool(work)
        except Exception as e:  # the Geofabrik index could not be fetched
            raise HTTPException(502, f"Geofabrik index: {e}")

    @app.post("/api/osm")
    async def osm_job(body: Dict):
        """Queue the download or the update of an extract: {"action": "download"|"update", "region": id}."""
        import osmextract
        action, region = body.get("action"), str(body.get("region", ""))
        if action == "download":
            if region not in await run_in_threadpool(osmextract._index, cache_dir):
                raise HTTPException(400, "unknown Geofabrik region")
        elif action == "update":
            if region not in dict(osmextract.extracts(cache_dir)):
                raise HTTPException(400, "no such extract downloaded")
        else:
            raise HTTPException(400, "action: download or update")
        request = {"osm": {"action": action, "region": region}}
        return {"id": jobs.add(request, f"OSM {action}: {region}")}

    profile_lock = threading.Lock()

    @app.get("/api/profile")
    async def line_profile(request: Request, run: str, line: str):
        """The profile of a line of a saved run, computed from the cached data (search.profile_of_line).

        A GET that costs computing time: it needs the X-Findaline-Profile header, which a
        foreign website cannot send without a CORS preflight this server does not allow.
        """
        if not request.headers.get("x-findaline-profile"):
            raise HTTPException(403, "header X-Findaline-Profile missing")
        meta_path = os.path.join(runs_dir, run, "run.json")
        if os.path.basename(run) != run or run in ("", ".", "..") or not os.path.isfile(meta_path):
            raise HTTPException(404, "no such run")
        try:
            lat1, lon1, lat2, lon2 = (float(v) for v in line.split(","))
        except ValueError:
            raise HTTPException(400, "line: lat1,lon1,lat2,lon2")
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)

        def work():
            with profile_lock:  # one at a time: each builds grids around the line
                paths = search.Paths(data_dir, cache.new_work(data_dir))
                try:
                    return search.profile_of_line(meta, paths, (lat1, lon1), (lat2, lon2))
                finally:
                    cache.remove_work(paths.work)
        try:
            return await run_in_threadpool(work)
        except (ValueError, search.SearchError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/area-file")
    async def area_file(file: UploadFile):
        """An uploaded GeoJSON/KML/GPX file as GeoJSON (shown on the map, then sent as the geometry)."""
        content = await file.read(MAX_UPLOAD + 1)
        if len(content) > MAX_UPLOAD:
            raise HTTPException(413, f"the file is larger than {MAX_UPLOAD // 1_000_000} MB")
        suffix = os.path.splitext(file.filename or "")[1].lower()
        if suffix not in (".geojson", ".json", ".kml", ".gpx"):
            raise HTTPException(400, "GeoJSON, KML or GPX expected")
        folder = os.path.join(data_dir, "work", "uploads", uuid.uuid4().hex)
        os.makedirs(folder)
        path = os.path.join(folder, "area" + suffix)
        with open(path, "wb") as f:
            f.write(content)

        def read():
            from area import _read_file
            from shapely.geometry import mapping
            return [mapping(g) for g in _read_file(path)]
        try:
            geometries = await run_in_threadpool(read)
        except Exception as e:  # GDAL errors of every kind
            raise HTTPException(400, f"could not read the file: {e}")
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        if not geometries:
            raise HTTPException(400, "no geometries in the file")
        return {"type": "FeatureCollection",
                "features": [{"type": "Feature", "properties": {}, "geometry": g} for g in geometries]}

    app.mount("/app", StaticFiles(directory=APP_DIST), name="app")
    app.mount("/runs", StaticFiles(directory=runs_dir), name="runs")

    # Cached orthophoto tiles only (maps reach them as ../../cache/<folder>/wm256/...)
    @app.get("/cache/{path:path}")
    def ortho_tile(path: str):
        from fastapi.responses import FileResponse
        parts = path.split("/")
        full = os.path.normpath(os.path.join(cache_dir, *parts))
        if ("wm256" not in parts or not path.endswith(".jpg") or ".." in parts
                or not full.startswith(os.path.normpath(cache_dir) + os.sep) or not os.path.isfile(full)):
            raise HTTPException(404)
        return FileResponse(full, media_type="image/jpeg")

    return app


APP_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="findaline-token" content="{token}">
<title>Find-a-Line</title>
<link rel="stylesheet" href="/app/app.css">
</head><body><div id="app"></div>
<script src="/app/app.js"></script>
</body></html>
"""


def main(argv: List[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="findaline.py web", description="Find-a-Line in the browser.")
    parser.add_argument("--port", type=int, default=PORT, help=f"port on 127.0.0.1 (default {PORT})")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--data-dir", help="data folder (default: FINDALINE_DATA or the folder of the scripts)")
    args = parser.parse_args(argv)
    import uvicorn
    data = cache.data_dir(args.data_dir)
    cache.check_layout(os.path.join(data, "cache"))
    app = create_app(data, port=args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Find-a-Line is running at {url} (close this window or press Ctrl+C to stop it)")
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0
