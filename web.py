"""
The local web application: `python findaline.py web` starts it on 127.0.0.1 and
opens the browser.

- The page (viewer/src/app) picks a point or draws an area, fills in the parameters
  (the form comes from params.SearchParams), shows the estimate and queues the search.
- Jobs wait in a queue (jobs.py; jobs.sqlite in the data folder). Each job runs in its own
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
cli_only fields are refused, and a drawn area or an area file (read in the browser) is a
GeoJSON geometry checked without GDAL (SearchParams.geometry, area.check_geometry), never
written to a file.
"""

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import threading
import uuid
import webbrowser
from typing import Dict, List, Optional

import cache
import params as pm
from jobs import TICK, Jobs, Scheduler, search_params

PORT = 8765
APP_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer", "dist")


def schema() -> List[Dict]:
    """The fields of SearchParams the page may set (cli_only and web_only fields are left out)."""
    import typing
    fields = []
    for name, info in pm.SearchParams.model_fields.items():
        extra = info.json_schema_extra or {}
        if extra.get("cli_only") or extra.get("web_only"):
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
    fields = {f["name"]: f for f in schema()}
    unknown = set(params) - set(fields)
    if unknown:
        raise ValueError(f"not allowed: {', '.join(sorted(unknown))}")
    for name, value in params.items():  # e.g. no local files as the source
        choices = fields[name]["choices"]
        if choices and value is not None and value not in choices:
            raise ValueError(f"{name}: one of {', '.join(map(str, choices))}")
    area = params.get("area")
    if area is not None and area not in ("coast", "cliffs") and not str(area).startswith("osm:"):
        raise ValueError("area: coast, cliffs, osm:<name> or a drawn/uploaded geometry")
    geometry = request.get("geometry")
    if geometry is not None and (not isinstance(geometry, dict) or "type" not in geometry):
        raise ValueError("geometry: GeoJSON expected")
    if geometry is not None and area is not None:
        raise ValueError("either area or geometry")
    return {"params": params, "geometry": geometry}


def title(p: pm.SearchParams) -> str:
    """The title of a job in the queue, from its validated parameters."""
    circle = p.geometry is None and p.area is None
    if p.geometry is not None:
        where = "drawn area"
    elif p.area:
        where = p.area
    elif p.lat is not None:
        where = f"{p.lat:.5f}, {p.lon:.5f}"
    else:
        where = f"N {p.northing:.0f}, E {p.easting:.0f}"
    return (p.name or where) + (f" (r {p.radius:g} m)" if circle else "")


def create_app(data_dir: str, token: Optional[str] = None, port: int = PORT, start_scheduler: bool = True):
    """The FastAPI application (tests create it without the scheduler)."""
    from fastapi import FastAPI, HTTPException, Request
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
            "default-src 'self'; script-src 'self'; style-src 'self'; "
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
            params = search_params(request)
            folder = os.path.join(data_dir, "work", "estimates", uuid.uuid4().hex)  # one per request
            try:
                paths = search.Paths(data_dir, folder)
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
            params = await run_in_threadpool(search_params, request)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": jobs.add(request, title(params))}

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": jobs.all(), "seq": jobs.last_seq()}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        """A job with its request (parameters and drawn or uploaded area), for "edit"."""
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        return {**{k: job[k] for k in ("id", "created", "status", "title", "run_id", "error")},
                "request": json.loads(job["request"])}

    @app.post("/api/jobs/{job_id}/again")
    def run_again(job_id: str):
        """Queue a finished job again with the same request (a tiled area resumes where it stopped)."""
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        if job["status"] not in ("done", "failed", "cancelled"):
            raise HTTPException(409, "the job is still waiting or running")
        title = re.sub(r"( \(again\))+$", "", job["title"]) + " (again)"
        return {"id": jobs.add(json.loads(job["request"]), title)}

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
            raise HTTPException(502, f"Geofabrik index: {cache.clean_error(e)}")

    @app.post("/api/osm")
    async def osm_job(body: Dict):
        """Queue the download or the update of an extract: {"action": "download"|"update", "region": id}."""
        import osmextract
        action, region = body.get("action"), str(body.get("region", ""))
        if action == "download":
            regions = await run_in_threadpool(osmextract._index, cache_dir)
            if region not in regions:
                raise HTTPException(400, "unknown Geofabrik region")
            if not osmextract.offered(regions[region]):
                raise HTTPException(400, "a continent: choose a country or a smaller region")
        elif action == "update":
            if region not in dict(osmextract.extracts(cache_dir)):
                raise HTTPException(400, "no such extract downloaded")
        else:
            raise HTTPException(400, "action: download or update")
        if any(json.loads(j["request"]).get("osm", {}).get("region") == region for j in jobs.waiting_or_running()):
            raise HTTPException(409, f"{region} is already waiting or being downloaded in the queue")
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
