"""
The local web application: `python findaline.py web` starts it on 127.0.0.1 and
opens the browser.

- The page (viewer/src/app) picks a point or draws an area, fills in the parameters
  (the form comes from params.SearchParams), shows the estimate and queues the search.
- Jobs wait in a queue (jobs.py; jobs.sqlite in the data folder). The worker (jobs.Scheduler,
  in this process unless --no-worker, else `findaline.py worker`) runs each job in its own
  process, in two phases: downloading its data (search.download_data) and computing
  (the search itself). One job computes at a time, as it uses all cores and much
  memory; the next job may download its data meanwhile. Jobs can be cancelled.
- OpenStreetMap extracts (osmextract.py) are downloaded and updated as jobs too, so the
  coastline and cliffs of a region need no Overpass.
- Messages and progress of the jobs are stored as events and sent to the page as
  Server-Sent Events. Results are ordinary runs: the page opens runs/<id>/map.html.

Security (docs/roadmap.md, "Security"): the server listens on 127.0.0.1 only, but any
website the user visits could send requests to it, so it accepts only its own Host
names (TrustedHostMiddleware), and a request that changes something only from its own
Origin and with the session token of the page (a header a foreign website cannot set).
File paths never come from the web: the cli_only fields are refused, and a drawn area or
an area file (read in the browser) is a GeoJSON geometry checked without GDAL
(SearchParams.geometry, area.check_geometry), never written to a file. Every answer
carries the headers of SECURITY_HEADERS.

The public mode (`--public`, limits.PublicSettings) serves the site behind a reverse
proxy: only the host of FINDALINE_PUBLIC_URL, an Origin required for changes, X-Forwarded-*
trusted from FINDALINE_TRUSTED_PROXY only, no worker in this process. Until accounts
exist, everything but the page and its assets (is_open) answers 401.
"""

import asyncio
import contextlib
import json
import math
import os
import re
import secrets
import threading
import time
import webbrowser
from typing import Dict, List, Optional

import cache
import params as pm
from jobs import TICK, Jobs, Scheduler, search_params
from limits import Limits, PublicSettings

PORT = 8765
PING = 15           # seconds between the keep-alive comments of the event stream
SEND_TIMEOUT = 30   # seconds a client may take to accept a message of the event stream
QUICK_POLL = 0.1    # seconds between looks for the answer of an estimate or a profile
NO_WORKER = "the worker is not running (findaline.py worker, or findaline.py web without --no-worker)"
APP_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer", "dist")
LOCAL_HOSTS = ["127.0.0.1", "localhost", "testserver"]  # testserver: FastAPI's TestClient
GRACEFUL_SECONDS = 5    # open event streams are closed this long after a stop
KEEP_ALIVE_SECONDS = 5  # an idle connection is closed after it
# Headers of every answer, in both modes. The viewer and the app need no inline scripts or
# styles. The Referer carries the origin only: the tile servers' policies ask for one, and no
# path (later a share token) leaves the site.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: "
                               "https:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
                          "microphone=(), payment=(), usb=()",
}


def schema(limits: Optional[Limits] = None) -> List[Dict]:
    """The fields of SearchParams the page may set (cli_only and web_only fields are left out),
    with the bounds of `limits` (min/max; fields and choices they do not allow are left out)."""
    import typing

    import sources as src
    fields = []
    for name, info in pm.SearchParams.model_fields.items():
        extra = info.json_schema_extra or {}
        if extra.get("cli_only") or extra.get("web_only") or (limits and not limits.allows(name)):
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
        if choices and limits:
            choices = [c for c in choices if limits.allows(name, c)]
        default = None if info.default_factory else info.default
        low, high = limits.bounds(name, src.MIN_RESOLUTION) if limits else (None, None)
        text = info.description
        if name == "group_distance" and low is not None:
            text += f" (on this site at least {limits.group_distance_min_res:g} × the resolution)"
        elif low is not None or high is not None:
            said = ([] if low is None else [f"at least {low:g}"]) + ([] if high is None else [f"at most {high:g}"])
            text += f" (on this site {' and '.join(said)})"
        fields.append({"name": name, "type": kind, "choices": choices, "default": default,
                       "help": text, "group": extra.get("group"), "min": low, "max": high,
                       "optional": not info.is_required() and default is None})
    return fields


def check_request(request: Dict, limits: Optional[Limits] = None) -> Dict:
    """Refuse what the web must not set, and values over `limits` (None: no limits); returns the
    request with known keys only."""
    import sources as src
    params = request.get("params")
    if not isinstance(params, dict):
        raise ValueError("params missing")
    if limits:
        limits.check_request(params, src.MIN_RESOLUTION)
    fields = {f["name"]: f for f in schema(limits)}
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


def is_open(path: str) -> bool:
    """Whether the public mode serves a path without signing in: the page and its assets."""
    return path == "/" or path.startswith("/app/")


def create_app(data_dir: str, token: Optional[str] = None, port: int = PORT, start_scheduler: bool = True,
               public: Optional[PublicSettings] = None, limits: Optional[Limits] = None):
    """The FastAPI application (tests create it without the scheduler); with `public`, the public
    mode, which never runs the worker in this process. `limits` of the request values: by default
    from FINDALINE_LIMIT_* (and the public site's defaults in the public mode)."""
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from sse_starlette import EventSourceResponse
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    import runs as rn

    token = token or secrets.token_urlsafe(24)
    limits = limits if limits is not None else Limits.load(public is not None)
    start_scheduler = start_scheduler and public is None
    jobs = Jobs(data_dir)
    scheduler = Scheduler(jobs)

    @contextlib.asynccontextmanager
    async def lifespan(_):
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="Find-a-Line", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.jobs, app.state.scheduler, app.state.token, app.state.limits = jobs, scheduler, token, limits
    if public:
        hosts, origins = [public.host], {public.origin}
    else:
        hosts, origins = LOCAL_HOSTS, {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    cache_dir = os.path.join(data_dir, "cache")
    runs_dir = os.path.join(data_dir, "runs")
    os.makedirs(runs_dir, exist_ok=True)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Anything that changes state only from our own page (CSRF): its Origin (required in the
        # public mode; locally a client without one, e.g. curl, is no browser) and its token
        changes = request.method not in ("GET", "HEAD")
        origin = request.headers.get("origin")
        if public and not is_open(request.url.path):
            response = JSONResponse({"detail": "sign-in required"}, status_code=401)
        elif changes and (public or origin is not None) and origin not in origins:
            response = JSONResponse({"detail": "foreign or missing Origin"}, status_code=403)
        elif changes and request.headers.get("x-findaline-token") != token:
            response = JSONResponse({"detail": "missing or wrong token"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        # The viewer and the app change with the code, run data with every run: always revalidate
        # (otherwise a browser may keep an old viewer.js next to a new data.js)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # Only our own host names (DNS rebinding; behind the proxy the public host), before the guard
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts, www_redirect=False)

    @app.get("/", response_class=HTMLResponse)
    def page():
        return APP_HTML.format(token=token)

    @app.get("/api/schema")
    def get_schema():
        return {"fields": schema(limits), "groups": pm.GROUPS}

    def parse(body: Dict) -> Dict:
        try:
            return check_request(body, limits)
        except ValueError as e:
            raise HTTPException(400, str(e))

    async def quick(kind: str, request: Dict):
        """The answer of the worker's quick lane (jobs.QUICK) to a task: added to the table `quick`
        and awaited without holding a thread, so the server does no search work itself."""
        if not await run_in_threadpool(jobs.worker_alive):
            raise HTTPException(503, NO_WORKER)
        task = await run_in_threadpool(jobs.quick_add, kind, request)
        looked = time.monotonic()
        while (answer := await run_in_threadpool(jobs.quick_take, task)) is None:
            await asyncio.sleep(QUICK_POLL)
            if time.monotonic() - looked > 5:
                looked = time.monotonic()
                if not await run_in_threadpool(jobs.worker_alive):
                    await run_in_threadpool(jobs.quick_forget, task)
                    raise HTTPException(503, NO_WORKER)
        if answer["code"] != 200:
            raise HTTPException(answer["code"], answer["body"])
        return answer["body"]

    @app.post("/api/estimate")
    async def estimate(body: Dict):
        return await quick("estimate", parse(body))

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
        return {"jobs": jobs.all(), "seq": jobs.last_seq(), "worker": jobs.worker_alive()}

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
        request = json.loads(job["request"])
        if "osm" not in request:
            parse(request)  # the limits may have changed since
        return {"id": jobs.add(request, title)}

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str):
        return {"events": jobs.events(0, job_id)}

    @app.delete("/api/jobs/{job_id}")
    def cancel(job_id: str):
        """A waiting job is cancelled at once, a running one by its worker (which kills its processes)."""
        if not jobs.request_cancel(job_id):
            raise HTTPException(409, "the job is not waiting or running")
        return {"ok": True}

    @app.get("/api/events")
    async def events(request: Request, since: int = 0):
        """All job events after `since`, as Server-Sent Events."""
        # A reconnecting EventSource sends the last id it got
        last = request.headers.get("last-event-id", "")
        start = max(since, int(last) if last.isdigit() else 0)

        def look(seq: int):
            return jobs.events(seq), jobs.worker_alive()

        async def stream():
            # sse-starlette ends the generator when the client goes away (or the server stops)
            seq, worker = start, None
            while True:
                found, alive = await run_in_threadpool(look, seq)
                for event in found:
                    seq = event["seq"]
                    yield {"id": str(seq), "data": json.dumps(event)}
                if alive != worker:  # whether a worker runs: no id, it is not a stored event
                    worker = alive
                    yield {"data": json.dumps({"kind": "worker", "data": {"alive": alive}})}
                await asyncio.sleep(TICK)
        # A comment every PING seconds keeps proxies from closing a quiet stream and finds dead
        # connections; a client that takes no data for SEND_TIMEOUT seconds is dropped
        return EventSourceResponse(stream(), ping=PING, send_timeout=SEND_TIMEOUT)

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

    @app.get("/api/profile")
    async def line_profile(request: Request, run: str, line: str):
        """The profile of a line of a saved run, computed from the cached data (search.profile_of_line,
        in the worker's quick lane).

        A GET that costs computing time: it needs the X-Findaline-Profile header, which a
        foreign website cannot send without a CORS preflight this server does not allow.
        """
        if not request.headers.get("x-findaline-profile"):
            raise HTTPException(403, "header X-Findaline-Profile missing")
        meta_path = os.path.join(runs_dir, run, "run.json")
        if os.path.basename(run) != run or run in ("", ".", "..") or not os.path.isfile(meta_path):
            raise HTTPException(404, "no such run")
        try:
            points = [float(v) for v in line.split(",")]
        except ValueError:
            points = []
        if len(points) != 4 or not all(math.isfinite(v) for v in points):
            raise HTTPException(400, "line: lat1,lon1,lat2,lon2")
        return await quick("profile", {"run": run, "line": points})  # one at a time in the quick lane

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


def is_loopback(host: str) -> bool:
    import ipaddress
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: List[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="findaline.py web", description="Find-a-Line in the browser.")
    parser.add_argument("--port", type=int, default=PORT, help=f"port (default {PORT})")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default 127.0.0.1); other than a loopback address only "
                             "with --public")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--data-dir", help="data folder (default: FINDALINE_DATA or the folder of the scripts)")
    parser.add_argument("--no-worker", action="store_true",
                        help="do not run the worker in this process; `findaline.py worker` runs the searches")
    parser.add_argument("--public", action="store_true",
                        help="the public site behind a reverse proxy: needs FINDALINE_PUBLIC_URL, "
                             "FINDALINE_TRUSTED_PROXY and FINDALINE_CONTACT; implies --no-worker and --no-browser "
                             "(the worker runs as `findaline.py worker --public`)")
    args = parser.parse_args(argv)
    if not args.public and not is_loopback(args.host):
        parser.error("--host other than a loopback address needs --public (the local app has no sign-in)")
    import uvicorn
    public = PublicSettings.load() if args.public else None
    data = cache.data_dir(args.data_dir)
    cache.check_layout(os.path.join(data, "cache"))
    app = create_app(data, port=args.port, start_scheduler=not args.no_worker, public=public)
    server = {"host": args.host, "port": args.port, "log_level": "warning", "server_header": False,
              "timeout_graceful_shutdown": GRACEFUL_SECONDS, "timeout_keep_alive": KEEP_ALIVE_SECONDS}
    if public:
        # The client's address and scheme from the X-Forwarded-* headers of the proxy only; a cap
        # on connections (each open page holds one for its event stream)
        server.update(proxy_headers=True, forwarded_allow_ips=public.trusted_proxy,
                      limit_concurrency=public.concurrency)
        print(f"Find-a-Line (public) serves {public.public_url} on {args.host}:{args.port}; the worker runs "
              f"separately (findaline.py worker --public)")
    else:
        server["proxy_headers"] = False  # no proxy in front of the local app
        url = f"http://127.0.0.1:{args.port}/"
        print(f"Find-a-Line is running at {url} (close this window or press Ctrl+C to stop it)")
        if not args.no_browser:
            threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, **server)
    return 0
