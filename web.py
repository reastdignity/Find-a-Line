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

The public mode (`--public`, settings.PublicSettings) serves the site behind a reverse
proxy: only the host of FINDALINE_PUBLIC_URL, an Origin required for changes, X-Forwarded-*
trusted from FINDALINE_TRUSTED_PROXY only, no worker in this process. Users sign in with
GitHub or Google (accounts.py, settings.AccountSettings); the token a change needs is then
the CSRF token of the user's session, and sign-in requests are limited per IP address
(the `limits` library, in memory).
"""

import asyncio
import contextlib
import html
import json
import math
import os
import re
import secrets
import threading
import time
import webbrowser
from typing import Callable, Dict, List, Optional, Tuple

import pydantic
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

import accounts as ac
import cache
from audit import KEEP_DAYS as AUDIT_DAYS, Audit
import params as pm
from jobs import TICK, Jobs, QuotaExceeded, Scheduler, search_params
from settings import AccountSettings, Limits, PublicSettings

PORT = 8765
PING = 15           # seconds between the keep-alive comments of the event stream
SEND_TIMEOUT = 30   # seconds a client may take to accept a message of the event stream
QUICK_POLL = 0.1    # seconds between looks for the answer of an estimate or a profile
# How long the server waits for an answer of the quick lane: the lane's time limit for each task ahead
# and this one, plus QUICK_SLACK, at most QUICK_WAIT_MAX; without a time limit (locally) QUICK_WAIT_LOCAL
QUICK_SLACK = 30
QUICK_WAIT_MAX = 600
QUICK_WAIT_LOCAL = 3600
NO_WORKER = "the worker is not running (findaline.py worker, or findaline.py web without --no-worker)"
APP_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer", "dist")
LOCAL_HOSTS = ["127.0.0.1", "localhost"]  # tests use them too (TestClient's base_url)
GRACEFUL_SECONDS = 5    # open event streams are closed this long after a stop
KEEP_ALIVE_SECONDS = 5  # an idle connection is closed after it
# Headers of every answer, in both modes. The viewer and the app need no inline scripts or
# styles. The Referer carries the origin only, to other sites and to our own (strict-origin): the
# tile servers' policies ask for one, and no path leaves the page, not even to us (a share page's
# own requests, its favicon or cached tiles, would carry /s/<token>/ into the logs).
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: "
                               "https:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
                          "microphone=(), payment=(), usb=()",
}


def schema(limits: Optional[Limits] = None, hidden_sources=()) -> List[Dict]:
    """The fields of SearchParams the page may set (cli_only and web_only fields are left out),
    with the bounds of `limits` (min/max; fields and choices they do not allow are left out) and
    without the sources of `hidden_sources` (ADMIN_SOURCES for all but administrators)."""
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
            choices, kind = [c for c in pm.SOURCE_CHOICES[:1] + pm.SOURCE_CHOICES[2:]  # no local files
                             if c not in hidden_sources], "choice"
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
        label, unit = pm.LABELS.get(name, (name.replace("_", " "), ""))
        names = {c: ("Automatic" if c == "auto" else src.SOURCES[c].name) for c in choices or []}             if name == "source" else None
        fields.append({"name": name, "type": kind, "choices": choices, "default": default,
                       "help": text, "group": extra.get("group"), "min": low, "max": high,
                       "optional": not info.is_required() and default is None,
                       "label": label, "unit": unit, "topic": pm.TOPIC_OF.get(name), "choice_labels": names})
    return fields


def presets(limits: Optional[Limits] = None) -> List[Dict]:
    """The presets of params.PRESETS for the form: each with its values for every field of
    PRESET_FIELDS (None: "custom", the values stay as they are) and whether it is available under
    `limits`. A value outside a user's bounds, or a field the site does not allow, makes the preset
    unavailable (with the reason): clamping it would give another kind of lines under the same name."""
    import sources as src
    answer = []
    for preset, (label, _) in pm.PRESETS.items():
        values = pm.preset_values(preset)
        problems = []
        for name, value in (values or {}).items():
            if limits and not limits.allows(name):
                problems.append(f"{name}: not allowed on this site")
                continue
            low, high = limits.bounds(name, src.MIN_RESOLUTION) if limits else (None, None)
            if low is not None and value < low:
                problems.append(f"{name}: at least {low:g} on this site")
            if high is not None and value > high:
                problems.append(f"{name}: at most {high:g} on this site")
        answer.append({"id": preset, "label": label, "values": values, "available": not problems,
                       "reason": "; ".join(problems) or None})
    return answer


def check_request(request: Dict, limits: Optional[Limits] = None, hidden_sources=()) -> Dict:
    """Refuse what the web must not set, values over `limits` (None: no limits) and the sources of
    `hidden_sources`; returns the request with known keys only."""
    return checked_request(request, limits, hidden_sources)[0]


def checked_request(request: Dict, limits: Optional[Limits] = None, hidden_sources=()) -> Tuple[Dict, pm.SearchParams]:
    """check_request, and the validated parameters: the bounds of `limits` are checked on the values
    pydantic read (a number spelled "5_e3" is 5000 there), a value it cannot read is refused."""
    import sources as src
    params = request.get("params")
    if not isinstance(params, dict):
        raise ValueError("params missing")
    if limits:
        limits.check_request(params)
    fields = {f["name"]: f for f in schema(limits, hidden_sources)}
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
    request = {"params": params, "geometry": geometry}
    p = search_params(request)  # ValueError (pydantic's) for a value it cannot read
    if limits:
        limits.check_params(p, min_resolution=src.MIN_RESOLUTION)
    return request, p


def profile_problem(folder: str, points: List[float], admin: bool = False) -> Optional[str]:
    """Why a profile of a run may not be computed on the public site (None: it may): both points in
    the box of the run's area grown by its --max-length, and at most --max-length apart (1% and
    2 m of slack for the projection). A run from a source of ADMIN_SOURCES (a demo run any user may
    open) only for administrators: its profile may download a tile with the server's key."""
    import runs as rn
    try:
        with open(os.path.join(folder, "run.json"), encoding="utf-8") as f:
            meta = json.load(f)
        max_length = float(meta["params"]["max_length"])
        box = rn._bbox(meta)
        source = meta.get("source", {}).get("id")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        box = None
    if box is None:
        return "this run has no profiles"
    if source in ADMIN_SOURCES and not admin:
        return f"profiles of runs from {source} are computed for administrators only"
    lat1, lon1, lat2, lon2 = points
    west, south, east, north = box
    dlat = max_length / 111320
    dlon = dlat / max(math.cos(math.radians((south + north) / 2)), 0.01)
    if not all(south - dlat <= lat <= north + dlat and west - dlon <= lon <= east + dlon
               for lat, lon in ((lat1, lon1), (lat2, lon2))):
        return "the points must lie in the run's area (with a margin of its --max-length)"
    mean = math.radians((lat1 + lat2) / 2)
    length = 6371008.8 * math.hypot(math.radians(lat2 - lat1), math.radians(lon2 - lon1) * math.cos(mean))
    if length > max_length * 1.01 + 2:
        return f"the points are {length:.0f} m apart, at most {max_length:g} m (the run's --max-length)"
    return None


# A favourite line: its anchors as the run's data.js holds them and the map joins them
# (viewer/src/keys.ts lineCoords: JavaScript's numbers, so 1e-7 may be spelled so)
_NUMBER = r"-?\d{1,3}(?:\.\d{1,15})?(?:e-\d{1,2})?"
LINE_KEY = re.compile(rf"{_NUMBER}(?:,{_NUMBER}){{3}}")


class FavouriteLine(pydantic.BaseModel):
    """The values of a favourite line as the map sends them (the fields of viewer/src/types.ts Line
    without its anchors, grid coordinates and profile; null: not known), kept for the list of all of
    a user's favourites (one's own data, shown to oneself only)."""
    model_config = pydantic.ConfigDict(extra="forbid")
    len: pydantic.FiniteFloat
    clr: pydantic.FiniteFloat
    clrN: Optional[pydantic.FiniteFloat]
    e1: pydantic.FiniteFloat
    e2: pydantic.FiniteFloat
    over: pydantic.StrictBool
    water: pydantic.FiniteFloat
    filled: pydantic.FiniteFloat
    hmax: Optional[pydantic.FiniteFloat]
    hmean: Optional[pydantic.FiniteFloat]
    highPct: Optional[pydantic.FiniteFloat]
    angle1: Optional[pydantic.FiniteFloat]
    angle2: Optional[pydantic.FiniteFloat]
    prom1: Optional[pydantic.FiniteFloat]
    prom2: Optional[pydantic.FiniteFloat]
    drop1: Optional[pydantic.FiniteFloat]
    drop2: Optional[pydantic.FiniteFloat]


def checked_line(line) -> str:
    """A line of a run as its map knows it ("lat1,lon1,lat2,lon2"); ValueError for anything else."""
    if not isinstance(line, str) or len(line) > 100 or not LINE_KEY.fullmatch(line):
        raise ValueError("line: lat1,lon1,lat2,lon2")
    lat1, lon1, lat2, lon2 = (float(v) for v in line.split(","))
    if not (abs(lat1) <= 90 and abs(lat2) <= 90 and abs(lon1) <= 180 and abs(lon2) <= 180):
        raise ValueError("line: lat1,lon1,lat2,lon2")
    return line


def checked_folder(value, name: str = "folder") -> Optional[int]:
    """A folder of favourites given by its id, or None (the top level); ValueError for anything else."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2 ** 62:
        raise ValueError(f"{name}: a folder's id or null")
    return value


def checked_folder_name(value) -> str:
    """A folder's name: 1 to jobs.FOLDER_NAME_MAX characters (stripped), no control characters."""
    import jobs as jb
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > jb.FOLDER_NAME_MAX \
            or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value):
        raise ValueError(f"name: 1 to {jb.FOLDER_NAME_MAX} characters")
    return value.strip()


def checked_favourite(body) -> Tuple[str, Optional[Dict]]:
    """(line, its values; None: unmark it) of a request {"line": "lat1,lon1,lat2,lon2", "favourite":
    true|false, "data": FavouriteLine (when true)}; ValueError for anything else."""
    if not isinstance(body, dict) or not isinstance(body.get("favourite"), bool):
        raise ValueError("favourite: true or false")
    line = checked_line(body.get("line"))
    if not body["favourite"]:
        return line, None
    try:
        return line, FavouriteLine.model_validate(body.get("data")).model_dump()
    except pydantic.ValidationError:
        raise ValueError("data: the values of the line") from None


MAX_TITLE = 120  # characters of a job's title (the queue lists them, each event of the job repeats none)


def title(p: pm.SearchParams) -> str:
    """The title of a job in the queue, from its validated parameters (at most MAX_TITLE characters)."""
    circle = p.geometry is None and p.area is None
    if p.geometry is not None:
        where = "drawn area"
    elif p.area:
        where = p.area
    elif p.lat is not None:
        where = f"{p.lat:.5f}, {p.lon:.5f}"
    else:
        where = f"N {p.northing:.0f}, E {p.easting:.0f}"
    text = (p.name or where) + (f" (r {p.radius:g} m)" if circle else "")
    return text if len(text) <= MAX_TITLE else text[:MAX_TITLE - 1] + "…"


# The routes anyone may request in the public mode (signing in and out, the page, which shows a
# sign-in page with the demo runs to visitors, its assets, the list of runs of the maps, which
# gives a visitor the demo runs only, the privacy and about pages); every other route depends on current_user,
# require_admin or run_access (tests/test_public.py checks it)
PUBLIC_ROUTES = {"/", "/login/{provider}", "/auth/{provider}", "/logout", "/app", "/runs/index.html",
                 "/runs/runs.js", "/s/runs.js", "/s/index.html", "/cache/{path:path}", "/privacy", "/about"}
SESSION_SWEEP = 6 * 3600  # seconds between deletions of expired sessions (the web process, public site)
SHARE_BYTES = 32  # the random part of a share link
# Sources for administrators only on the public site: nasadem needs the server's OpenTopography key,
# malta2012's licence is not settled (sources.MALTA_CREDITS)
ADMIN_SOURCES = ("nasadem", "malta2012")
SSE_PER_USER = 3  # open event streams of one user (the public mode)
SESSION_CHECK = 30  # seconds between checks of the session inside an open event stream (the public mode)
# A refusal by a rate or a quota is one row of the audit log per user and kind in this many seconds;
# the others are counted, and the next row says how many there were ("repeated")
REFUSAL_WINDOW = 600
PLACE_CHARS_MAX = 200  # of a name the place box looks up
RATE_NAMES = {"estimate": "estimates", "profile": "profiles", "place": "place lookups",  # Limits.<kind>_rate
              "job": "searches started"}
# The files of a run the public site serves (what mapping.save_map and runs.save_results write, without
# run.json: its parameters and data files are the owner's); never progress.json, tiles or other files
RUN_FILES = ("map.html", "data.js", "lines.js", "lines.csv", "lines.geojson", "ATTRIBUTION.txt")
VIEWER = "_viewer"  # mapping.VIEWER_FOLDER: the viewer bundle, the same for everyone
# A run id, a share token (token_urlsafe: may start with - or _), a file name: no leading dot (no ".."), no "/"
SAFE_PART = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,199}")


class RunFiles(StaticFiles):
    """The run folders of the public site, with access control (never a plain StaticFiles):
    <first>/<file> serves a file of RUN_FILES of the run `resolve(request, first)` gives (None:
    404, whether the run exists or not), _viewer/<asset> anyone, from the code's viewer/dist (not
    runs/_viewer, which the worker writes: it cannot change the viewer of everyone). Starlette's
    StaticFiles does the rest: the path within the folder (symbolic links out of it are refused),
    ETag, Range."""

    def __init__(self, directory: str, resolve: Callable):
        super().__init__(directory=directory)
        self.resolve = resolve  # (request, first part of the path) -> the run's id, or None
        self.viewer = StaticFiles(directory=APP_DIST)

    async def get_response(self, path: str, scope):
        from starlette.concurrency import run_in_threadpool
        from starlette.requests import Request
        root = scope.get("root_path", "")
        route = scope["path"][len(root):] if scope["path"].startswith(root) else scope["path"]
        parts = route.split("/")[1:]  # "/<first>/<file>": an empty part ("//") is no path of ours
        if not route.startswith("/") or len(parts) != 2 or not all(SAFE_PART.fullmatch(part) or part == VIEWER for part in parts):
            raise StarletteHTTPException(404)
        first, name = parts
        if first == VIEWER:
            return await self.viewer.get_response(name, scope)
        if name not in RUN_FILES:
            raise StarletteHTTPException(404)
        first = await run_in_threadpool(self.resolve, Request(scope), first)
        if first is None:
            raise StarletteHTTPException(404)
        return await super().get_response(os.path.join(first, name), scope)


def create_app(data_dir: str, token: Optional[str] = None, port: int = PORT, start_scheduler: bool = True,
               public: Optional[PublicSettings] = None, limits: Optional[Limits] = None,
               accounts: Optional[AccountSettings] = None, oauth_transport=None):
    """The FastAPI application (tests create it without the scheduler); with `public`, the public
    mode, which never runs the worker in this process and needs `accounts` (the sign-in;
    `oauth_transport`: an httpx2 transport to the providers, for tests). `limits` of the request
    values: by default from FINDALINE_LIMIT_* (and the public site's defaults in the public mode)."""
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
    from sse_starlette import EventSourceResponse
    from limits import parse_many, storage, strategies
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    import runs as rn

    if public and accounts is None:
        raise ValueError("the public mode needs the account settings")
    token = token or secrets.token_urlsafe(24)
    limits = limits if limits is not None else Limits.load(public is not None)
    start_scheduler = start_scheduler and public is None
    jobs = Jobs(data_dir)
    scheduler = Scheduler(jobs, limits=limits)
    users = ac.Accounts(data_dir, accounts) if public else None
    audit = Audit(data_dir) if public else None  # the local app keeps no audit log

    def record(action: str, user: Optional[ac.User] = None, target=None, detail: Optional[Dict] = None):
        """An event of the audit log (the public site only; audit.py). It writes to a database: from
        an async endpoint call it in the thread pool."""
        if audit is not None:
            audit.log(action, user.id if user else None, None if target is None else str(target), detail)

    refusals: Dict[tuple, list] = {}  # (user id, kind) -> [start of its window, refusals not recorded]
    refusals_lock = threading.Lock()

    def refused(user: ac.User, kind: str, reason: str, status: int):
        """A refusal by a rate, a quota or a safeguard in the audit log: the first of a user and kind
        in REFUSAL_WINDOW seconds; the others only counted, their number goes with the next row."""
        if audit is None:
            return
        now = time.monotonic()
        with refusals_lock:
            seen = refusals.get((user.id, kind))
            if seen is not None and now - seen[0] < REFUSAL_WINDOW:
                seen[1] += 1
                return
            refusals[(user.id, kind)] = [now, 0]
            if len(refusals) > 10000:  # forget the windows that ended (their counts are lost)
                for key in [k for k, (start, _) in refusals.items() if now - start >= REFUSAL_WINDOW]:
                    del refusals[key]
        record("refused", user, None, {"reason": reason, "status": status,
                                       **({"repeated": seen[1]} if seen and seen[1] else {})})

    @contextlib.asynccontextmanager
    async def lifespan(_):
        if start_scheduler:
            scheduler.start()
        sweep = None
        if public:
            async def forget_sessions():  # expired and idle sessions (the worker cleans the rest up)
                while True:
                    await run_in_threadpool(users.forget_sessions)
                    await asyncio.sleep(SESSION_SWEEP)
            sweep = asyncio.create_task(forget_sessions())
        yield
        if sweep:
            sweep.cancel()
        scheduler.stop()

    app = FastAPI(title="Find-a-Line", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.jobs, app.state.scheduler, app.state.token, app.state.limits = jobs, scheduler, token, limits
    app.state.accounts, app.state.account_settings = users, accounts
    if public:
        hosts, origins = [public.host], {public.origin}
    else:
        hosts, origins = LOCAL_HOSTS, {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    cache_dir = os.path.join(data_dir, "cache")
    runs_dir = os.path.join(data_dir, "runs")
    os.makedirs(runs_dir, exist_ok=True)

    def signed_in(request: Request) -> Optional[tuple]:
        """(user, CSRF token) of the request's session in the public mode (looked up once per request)."""
        if not public:
            return None
        if not hasattr(request.state, "signed_in"):
            request.state.signed_in = users.session(request.cookies.get(ac.SESSION_COOKIE))
        return request.state.signed_in

    def csrf_ok(request: Request, given: Optional[str]) -> bool:
        """Whether a token sent with a change is the page's: the session's (public) or the app's (local)."""
        expected = (signed_in(request) or (None, None))[1] if public else token
        # compare_digest takes ASCII text only: anything else is not our token (and no error)
        return bool(expected) and given is not None and given.isascii() and secrets.compare_digest(given, expected)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Anything that changes state only from our own page (CSRF): its Origin (required in the
        # public mode; locally a client without one, e.g. curl, is no browser) and its token
        # (/logout takes it from its form too: the sign-in page has no script)
        changes = request.method not in ("GET", "HEAD")
        origin = request.headers.get("origin")
        path = request.url.path
        if changes and (public or origin is not None) and origin not in origins:
            response = JSONResponse({"detail": "foreign or missing Origin"}, status_code=403)
        elif changes and path != "/logout" and public and not signed_in(request):
            response = JSONResponse({"detail": "sign-in required"}, status_code=401)  # no session, no token
        elif changes and path != "/logout" and not csrf_ok(request, request.headers.get("x-findaline-token")):
            response = JSONResponse({"detail": "missing or wrong token"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        response.headers["Cache-Control"] = cache_control(request, path)
        return response

    def cache_control(request: Request, path: str) -> str:
        """Answers with a user's data are not stored: the API, the list of runs and the page with its
        token (locally always; on the public site when signed in). The files of runs and share links
        are private (no shared cache) and revalidated. The rest (the app, the viewer, the public
        pages) is revalidated too: the viewer and the app change with the code, run data with every
        run (a browser must not keep an old viewer.js next to a new data.js)."""
        if path.startswith("/api/") or path == "/runs/runs.js" or (
                path == "/" and (not public or getattr(request.state, "signed_in", None))):
            return "no-store"
        if path.startswith(("/runs/", "/s/")) and not path.startswith((f"/runs/{VIEWER}/", f"/s/{VIEWER}/")) \
                and path != "/s/runs.js":
            return "private, no-cache"
        return "no-cache"

    if public:
        from starlette.middleware.sessions import SessionMiddleware

        # A sign-in in progress (Authlib's state, nonce and PKCE verifier): a signed cookie of ten minutes
        app.add_middleware(SessionMiddleware, secret_key=accounts.session_secret.get_secret_value(),
                           session_cookie=ac.OAUTH_COOKIE, max_age=ac.OAUTH_SECONDS, path="/", same_site="lax",
                           https_only=True)
        oauth = ac.oauth_clients(accounts, oauth_transport)
        # A sliding window counter: its keys expire (MemoryStorage keeps every key of a moving window)
        login_limiter = strategies.SlidingWindowCounterRateLimiter(storage.MemoryStorage())
        login_rates = parse_many(ac.LOGIN_RATE)
        app.state.limiters = {"login": login_limiter}

    # Only our own host names (DNS rebinding; behind the proxy the public host), before the guard
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts, www_redirect=False)

    def client_of(request: Request, provider: str):
        """Authlib's client of a provider, after the limit of sign-in requests per IP address."""
        client = oauth.create_client(provider) if public and provider in accounts.providers else None
        if client is None:
            raise HTTPException(404, "no such sign-in")
        address = client_key(request.client.host if request.client else "?")
        if not all([login_limiter.hit(rate, "login", address) for rate in login_rates]):
            raise HTTPException(429, "too many sign-in attempts: try again later")
        return client

    @app.get("/", response_class=HTMLResponse)
    def page(request: Request):
        if not public:
            return APP_HTML.format(token=token, user=html.escape(json.dumps({"local": True})))
        found = signed_in(request)
        if found and found[0].approved:
            return APP_HTML.format(token=found[1], user=html.escape(json.dumps(found[0].public())))
        return signin_page(accounts.providers, *(found or (None, None)), demos=runs_of(None))

    @app.get("/login/{provider}")
    async def login(request: Request, provider: str, next: str = "/"):
        """Start signing in: to the provider, which sends the browser back to /auth/<provider>."""
        client = client_of(request, provider)
        request.session.clear()
        request.session["next"] = ac.local_path(next)
        return await client.authorize_redirect(request, f"{public.public_url}/auth/{provider}")

    @app.get("/auth/{provider}")
    async def auth(request: Request, provider: str):
        """The provider's answer: a new session of the user (a new account waits for approval)."""
        import httpx2
        from authlib.integrations.starlette_client import OAuthError
        from joserfc.errors import JoseError
        client = client_of(request, provider)
        try:
            # Checks the state against the cookie of this browser (login CSRF), sends the PKCE verifier,
            # and for Google checks the ID token
            oauth_token = await client.authorize_access_token(request)
            subject, name = await ac.identity(client, provider, oauth_token)
            new = not await run_in_threadpool(users.known, provider, subject)
            user = await run_in_threadpool(users.sign_in, provider, subject, name)
            await run_in_threadpool(record, "sign_up" if new else "sign_in", user, None,
                                    {"provider": provider, "status": user.status})
        except ac.SignupClosed:
            await run_in_threadpool(record, "sign_up_refused", None, None, {"provider": provider})
            return HTMLResponse(signin_page(accounts.providers, note="New accounts are not accepted."), 403)
        except (OAuthError, JoseError, ValueError, KeyError, httpx2.HTTPError) as e:  # the ID token's checks: JoseError
            request.session.clear()
            return HTMLResponse(signin_page(accounts.providers, note=f"Signing in failed ({type(e).__name__})."),
                                400)
        next_path = ac.local_path(request.session.get("next"))
        request.session.clear()
        await run_in_threadpool(users.end_session, request.cookies.get(ac.SESSION_COOKIE))
        session, _ = await run_in_threadpool(users.new_session, user.id)  # a new id at every sign-in
        response = RedirectResponse(next_path, 303)
        response.set_cookie(ac.SESSION_COOKIE, session, max_age=ac.SESSION_DAYS * 86400, path="/", secure=True,
                            httponly=True, samesite="lax")
        return response

    @app.post("/logout")
    async def logout(request: Request):
        """Sign out (a POST with the CSRF token, as the header or the `csrf` field of the form)."""
        from urllib.parse import parse_qs
        if not public:
            raise HTTPException(404)
        given = request.headers.get("x-findaline-token")
        if given is None and int(request.headers.get("content-length") or 0) <= 4096:
            given = (parse_qs((await request.body())[:4096].decode("utf-8", "replace")).get("csrf") or [None])[0]
        found = signed_in(request)
        if found and not csrf_ok(request, given):
            raise HTTPException(403, "missing or wrong token")
        if found:
            await run_in_threadpool(record, "sign_out", found[0])
        await run_in_threadpool(users.end_session, request.cookies.get(ac.SESSION_COOKIE))
        response = RedirectResponse("/", 303)
        response.delete_cookie(ac.SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    # --- Who may do what: dependencies of the routes (never checks inside an endpoint) ---

    def current_user(request: Request) -> ac.User:
        """The signed-in, approved user (401 / 403 otherwise); locally the one local user."""
        if not public:
            return ac.LOCAL
        found = signed_in(request)
        if not found:
            raise HTTPException(401, "sign-in required")
        if not found[0].approved:
            raise HTTPException(403, "your account is not approved")
        return found[0]

    def require_admin(user: ac.User = Depends(current_user)) -> ac.User:
        if not user.admin:
            raise HTTPException(403, "administrators only")
        return user

    def visible_job(job_id: str, user: ac.User = Depends(current_user)) -> Dict:
        """A job its user may see and cancel: their own; an administrator's: every job (404 otherwise,
        so other users' jobs are not even known to exist)."""
        job = jobs.get(job_id)
        if not job or not (user.admin or job["user_id"] == user.id):
            raise HTTPException(404, "no such job")
        return job

    def own_job(job_id: str, user: ac.User = Depends(current_user)) -> Dict:
        """A job its user may edit or queue again: their own (an administrator's: also those from
        before the accounts)."""
        job = jobs.get(job_id)
        if not job or not (job["user_id"] == user.id or (user.admin and job["user_id"] is None)):
            raise HTTPException(404, "no such job")
        return job

    def may_open(user: Optional[ac.User], run_id: str) -> bool:
        """Whether a user (None: a visitor not signed in, or not approved) may open a run: on the
        public site its owner (an administrator: every run with an owner's row), and anyone a demo
        run; locally every saved run."""
        if not SAFE_PART.fullmatch(run_id) or not os.path.isfile(os.path.join(runs_dir, run_id, "run.json")):
            return False
        if not public:
            return True
        row = jobs.run(run_id)
        return row is not None and (row["visibility"] == "demo"
                                    or (user is not None and user.approved and (row["user_id"] == user.id or user.admin)))

    def run_access(run: str, user: ac.User = Depends(current_user)) -> str:
        """A run its user may open (404 otherwise, also for runs of others)."""
        if not may_open(user, run):
            raise HTTPException(404, "no such run")
        return run

    def admin_run(run: str, user: ac.User = Depends(require_admin)) -> str:
        """A run of the public site with an owner's row, for an administrator's action (the demo flag)."""
        if not public or jobs.run(run) is None or not may_open(user, run):
            raise HTTPException(404, "no such run")
        return run

    def visitor(request: Request) -> Optional[ac.User]:
        """The approved user of a request, or None (not signed in, pending, blocked): for what anyone
        may see, demo runs (public routes only)."""
        found = signed_in(request)
        return found[0] if found and found[0].approved else None

    def own_run(run: str = Depends(run_access), user: ac.User = Depends(current_user)) -> str:
        """A run its user owns (sharing it): run_access, and the owner only."""
        if public and (jobs.run(run) or {}).get("user_id") != user.id:
            raise HTTPException(404, "no such run")
        return run

    def run_of_session(request: Request, run_id: str) -> Optional[str]:
        """RunFiles of /runs: the run, if the request's user (or anyone: a demo run) may open it."""
        return run_id if may_open(visitor(request), run_id) else None

    def run_of_link(request: Request, share: str) -> Optional[str]:
        """RunFiles of /s: the run of a share link, found by the sha256 of its token (so the lookup
        tells nothing about the token itself); none once its owner is blocked."""
        run_id = jobs.shared_run(ac.digest(share)) if len(share) <= 100 else None
        row = jobs.run(run_id) if run_id else None
        owner = users.user(row["user_id"]) if row and row["user_id"] is not None else None
        if owner is None or owner.status == "blocked" or not os.path.isdir(os.path.join(runs_dir, run_id)):
            return None
        return run_id

    if public:
        # Runs from before the public mode (no row, or no owner) belong to the first administrator,
        # as private runs; nothing is deleted. Made before anyone signs in, so the id stays.
        jobs.adopt_runs(runs_dir, users.first_admin())  # (the viewer comes from the code: RunFiles)

    streams: Dict[Optional[int], int] = {}  # open event streams per user

    # --- The tiers of the accounts: limits of the site for users, none for users without limits
    # and administrators; the safeguards of the server (Jobs.admission) for everyone ---

    def unlimited(user: ac.User) -> bool:
        """Whether a user of the public site is without limits (locally FINDALINE_LIMIT_* apply as before)."""
        return public is not None and not user.limited

    def user_limits(user: ac.User) -> Optional[Limits]:
        """The limits of a user's requests, searches, quotas and rates (None: without limits)."""
        return None if unlimited(user) else limits

    def hidden_sources(user: ac.User) -> tuple:
        return ADMIN_SOURCES if public and not user.admin else ()

    rates = {}  # kind -> the parsed rates of Limits.<kind>_rate (requests of a user)
    rate_limiter = strategies.SlidingWindowCounterRateLimiter(storage.MemoryStorage())
    app.state.limiters = {**getattr(app.state, "limiters", {}), "rate": rate_limiter}
    if public:
        for kind in RATE_NAMES:
            if getattr(limits, f"{kind}_rate"):
                rates[kind] = parse_many(getattr(limits, f"{kind}_rate"))

    def within_rate(kind: str, user: ac.User):
        """429 when a limited user asks for more estimates, profiles or searches than Limits allows (the
        limits library, in the memory of this process)."""
        if user.limited and kind in rates and not all([rate_limiter.hit(r, kind, str(user.id)) for r in rates[kind]]):
            refused(user, f"{kind} rate", f"{kind} rate", 429)
            raise HTTPException(429, f"too many {RATE_NAMES[kind]}: try again in a minute")

    def admitted(user: ac.User):
        """The quotas and safeguards of a new job, before its request is validated (a drawn geometry
        costs time); queue() checks them again in the insert's transaction."""
        try:
            jobs.precheck(jobs.admission(user.id, limits, user.limited))
        except QuotaExceeded as e:
            refused(user, "quota", str(e), e.status)
            raise HTTPException(e.status, str(e))

    def queue(request: Dict, title: str, user: ac.User, again: Optional[str] = None) -> str:
        """Add a job after the quotas and safeguards (Jobs.admission, in the insert's transaction)."""
        try:
            job_id = jobs.add(request, title, user.id, check=jobs.admission(user.id, limits, user.limited),
                              unlimited=unlimited(user))
        except QuotaExceeded as e:
            refused(user, "quota", str(e), e.status)
            raise HTTPException(e.status, str(e))
        record("job_add", user, job_id, {"title": title, **({"again": again} if again else {})})
        return job_id

    @app.get("/api/schema")
    def get_schema(user: ac.User = Depends(current_user)):
        return {"fields": schema(user_limits(user), hidden_sources(user)), "groups": pm.GROUPS,
                "topics": {topic: title for topic, (title, _) in pm.TOPICS.items()},
                "presets": presets(user_limits(user))}

    @app.get("/api/usage")
    def get_usage(user: ac.User = Depends(current_user)):
        """What the user used in the last 24 hours and holds now, with the quotas that apply (None:
        none; for users without limits, administrators and the local app, whose one user has no id:
        Jobs.admission and Jobs.use count no quotas for it). Read-only: nothing is recorded."""
        quotas = limits if user.id is not None and user.limited else None
        return jobs.usage(user.id, quotas)

    def parse(body: Dict, user: ac.User) -> Tuple[Dict, pm.SearchParams]:
        """The checked request and its validated parameters (400 otherwise); in a thread (a drawn
        geometry takes a moment to validate)."""
        try:
            return checked_request(body, user_limits(user), hidden_sources(user))
        except ValueError as e:
            raise HTTPException(400, str(e))

    async def quick(kind: str, request: Dict, user: ac.User, client: Request):
        """The answer of the worker's quick lane (jobs.QUICK) to a task: added to the table `quick`
        and awaited without holding a thread, so the server does no search work itself. On the
        public site a user has one waiting task at most (429). The wait ends (504) after the lane's
        time limit for each task ahead and this one (QUICK_WAIT_MAX at most), or when the client
        goes away; the task is deleted then, unless the lane works on it already."""
        if not await run_in_threadpool(jobs.worker_alive):
            raise HTTPException(503, NO_WORKER)
        try:
            task = await run_in_threadpool(jobs.quick_add, kind, request, user.id, unlimited(user), public is not None)
        except QuotaExceeded as e:
            raise HTTPException(e.status, str(e))
        gone = None
        try:
            if limits.quick_seconds:
                ahead = await run_in_threadpool(jobs.quick_ahead, task)
                wait = min(QUICK_WAIT_MAX, limits.quick_seconds * (ahead + 1) + QUICK_SLACK)
            else:
                wait = QUICK_WAIT_LOCAL
            started = looked = time.monotonic()
            # Request.is_disconnected never sees the disconnect behind the guard (a BaseHTTPMiddleware
            # cancels its receive): a task waits for that message instead
            gone = asyncio.ensure_future(_disconnect(client.receive))
            while (answer := await run_in_threadpool(jobs.quick_take, task)) is None:
                await asyncio.sleep(QUICK_POLL)
                if gone.done():
                    raise HTTPException(503, "the client went away")  # nobody reads it
                if time.monotonic() - started > wait:
                    raise HTTPException(504, f"no answer within {wait:g} s: the worker is busy, try again later")
                if time.monotonic() - looked > 5:
                    looked = time.monotonic()
                    if not await run_in_threadpool(jobs.worker_alive):
                        raise HTTPException(503, NO_WORKER)
        except BaseException:  # also a cancelled request (the server stops): nobody waits for the task
            jobs.quick_forget(task)
            raise
        finally:
            if gone is not None:
                gone.cancel()
        if answer["code"] != 200:
            raise HTTPException(answer["code"], answer["body"])
        return answer["body"]

    @app.post("/api/estimate")
    async def estimate(body: Dict, client: Request, user: ac.User = Depends(current_user)):
        request, _ = await run_in_threadpool(parse, body, user)
        await run_in_threadpool(within_rate, "estimate", user)
        return await quick("estimate", request, user, client)

    @app.post("/api/place")
    async def place(body: Dict, client: Request, user: ac.User = Depends(current_user)):
        """The places of a name ({"q"}) for the place box: Nominatim through the quick lane (the
        worker's network, the shared rate and the user's quota of uncached requests)."""
        q = body.get("q") if isinstance(body, dict) else None
        if not isinstance(q, str) or not q.strip() or len(q) > PLACE_CHARS_MAX:
            raise HTTPException(400, f"a place name of 1 to {PLACE_CHARS_MAX} characters")
        await run_in_threadpool(within_rate, "place", user)
        return await quick("place", {"q": q.strip()}, user, client)

    @app.post("/api/jobs")
    async def add_job(body: Dict, user: ac.User = Depends(current_user)):
        """Queue a search: the rate of the user's requests and the quotas first, then the request."""
        await run_in_threadpool(within_rate, "job", user)
        await run_in_threadpool(admitted, user)
        request, params = await run_in_threadpool(parse, body, user)
        return {"id": await run_in_threadpool(queue, request, title(params), user)}

    def seen_by(user: ac.User) -> Optional[int]:
        """The user whose jobs a user sees (None: everyone's, for an administrator)."""
        return None if user.admin else user.id

    @app.get("/api/jobs")
    def list_jobs(user: ac.User = Depends(current_user)):
        return {"jobs": jobs.all(seen_by(user)), "seq": jobs.last_seq(seen_by(user)), "worker": jobs.worker_alive()}

    @app.get("/api/jobs/{job_id}")
    def get_job(job: Dict = Depends(own_job)):
        """A job with its request (parameters and drawn or uploaded area), for "edit"."""
        return {**{k: job[k] for k in ("id", "created", "status", "title", "run_id", "error")},
                "request": json.loads(job["request"])}

    @app.post("/api/jobs/{job_id}/again")
    def run_again(job: Dict = Depends(own_job), user: ac.User = Depends(current_user)):
        """Queue a finished job again with the same request (a tiled area resumes where it stopped)."""
        if job["status"] not in ("done", "failed", "cancelled"):
            raise HTTPException(409, "the job is still waiting or running")
        title = re.sub(r"( \(again\))+$", "", job["title"]) + " (again)"
        request = json.loads(job["request"])
        if "osm" not in request:
            parse(request, user)  # the limits (or the user's) may have changed since
        return {"id": queue(request, title, user, again=job["id"])}

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job: Dict = Depends(visible_job)):
        """The history of a job: its newest JOB_EVENTS_MAX messages and its last progress."""
        return jobs.job_events(job["id"])

    @app.delete("/api/jobs/{job_id}")
    def cancel(job: Dict = Depends(visible_job), user: ac.User = Depends(current_user)):
        """A waiting job is cancelled at once, a running one by its worker (which kills its processes)."""
        if not jobs.request_cancel(job["id"]):
            raise HTTPException(409, "the job is not waiting or running")
        record("job_cancel", user, job["id"], {"owner": job["user_id"]} if job["user_id"] != user.id else None)
        return {"ok": True}

    @app.get("/api/events")
    async def events(request: Request, since: int = 0, user: ac.User = Depends(current_user)):
        """The events of the user's jobs (an administrator's: of all) after `since`, as Server-Sent Events;
        at most SSE_PER_USER streams per user on the public site."""
        from starlette.background import BackgroundTask
        # A reconnecting EventSource sends the last id it got (digits only: "²" is a digit to isdigit())
        last = request.headers.get("last-event-id", "")
        start = max(since, int(last) if last.isascii() and last.isdecimal() else 0)
        released = []

        def release():  # from the generator's end and from the response's background task: once
            if public and not released:
                released.append(True)
                streams[user.id] -= 1
                if not streams[user.id]:
                    del streams[user.id]
        if public:  # checked and counted with no await in between, so streams opened at once count
            if streams.get(user.id, 0) >= SSE_PER_USER:
                raise HTTPException(429, f"at most {SSE_PER_USER} open pages per user")
            streams[user.id] = streams.get(user.id, 0) + 1
        whose = seen_by(user)
        cookie = request.cookies.get(ac.SESSION_COOKIE)

        def look(seq: int):
            return jobs.events(seq, user_id=whose), jobs.worker_alive()

        def still_signed_in() -> bool:
            found = users.session(cookie)
            return found is not None and found[0].approved and found[0].id == user.id

        async def stream():
            # sse-starlette ends the generator when the client goes away (or the server stops); the
            # public site ends it when the session ends (signed out, revoked, blocked)
            seq, worker, checked = start, None, time.monotonic()
            try:
                while True:
                    if public and time.monotonic() - checked > SESSION_CHECK:
                        checked = time.monotonic()
                        if not await run_in_threadpool(still_signed_in):
                            return
                    found, alive = await run_in_threadpool(look, seq)
                    for event in found:
                        seq = event["seq"]
                        yield {"id": str(seq), "data": json.dumps(event)}
                    if alive != worker:  # whether a worker runs: no id, it is not a stored event
                        worker = alive
                        yield {"data": json.dumps({"kind": "worker", "data": {"alive": alive}})}
                    await asyncio.sleep(TICK)
            finally:
                release()
        # A comment every PING seconds keeps proxies from closing a quiet stream and finds dead
        # connections; a client that takes no data for SEND_TIMEOUT seconds is dropped
        return EventSourceResponse(stream(), ping=PING, send_timeout=SEND_TIMEOUT, background=BackgroundTask(release))

    def runs_of(user: Optional[ac.User]) -> List[Dict]:
        """The runs a user's lists show: on the public site their own (an administrator's too) and the
        demo runs (a visitor: those only); locally all."""
        if not public:
            return rn.list_runs(runs_dir)
        return rn.list_runs(runs_dir, (jobs.runs_of(user.id) if user else set()) | jobs.demo_runs())

    @app.get("/api/runs")
    def list_runs(user: ac.User = Depends(current_user)):
        """The user's runs and the demo runs (locally all)."""
        shared = jobs.shared_runs(user.id) if public else set()
        mine = jobs.runs_of(user.id) if public else None
        demo = jobs.demo_runs() if public else set()
        return {"public": bool(public),  # whether runs can be shared (by a link)
                "runs": [{k: r.get(k) for k in ("id", "name", "created", "summary", "has_map")}
                         | {"description": rn.params_summary(r), "shared": r["id"] in shared,
                            "own": mine is None or r["id"] in mine, "demo": r["id"] in demo}
                         for r in runs_of(user)]}

    @app.post("/api/runs/{run}/demo")
    def set_demo(body: Dict, run: str = Depends(admin_run), user: ac.User = Depends(require_admin)):
        """Mark a run as a demo (its map for everyone, also without signing in) or private again:
        {"demo": true|false}; administrators only."""
        if not isinstance(body.get("demo"), bool):
            raise HTTPException(400, "demo: true or false")
        jobs.set_visibility(run, "demo" if body["demo"] else "private")
        record("run_demo", user, run, {"demo": body["demo"]})
        return {"ok": True}

    @app.post("/api/runs/{run}/share")
    def share_run(run: str = Depends(own_run), user: ac.User = Depends(current_user)):
        """A share link of an own run (the map for anyone who has the link); a new one replaces the old.
        Only its sha256 is kept, so the link is shown this once."""
        if not public:
            raise HTTPException(404, "share links are for the public site")
        renewed = (jobs.run(run) or {}).get("share_hash") is not None
        token = secrets.token_urlsafe(SHARE_BYTES)
        jobs.set_share(run, ac.digest(token))
        record("share_renew" if renewed else "share_create", user, run)
        return {"url": f"{public.public_url}/s/{token}/map.html"}

    @app.delete("/api/runs/{run}")
    def delete_run(run: str = Depends(own_run), user: ac.User = Depends(current_user)):
        """Delete an own run (its folder and row) on the public site; it no longer counts in the quota."""
        if not public:
            raise HTTPException(404, "runs are deleted in the runs folder")
        try:
            jobs.delete_run(run, runs_dir)
        except OSError:
            raise HTTPException(409, "the run is in use: try again in a moment")
        record("run_delete", user, run)
        return {"ok": True}

    @app.delete("/api/runs/{run}/share")
    def unshare_run(run: str = Depends(own_run), user: ac.User = Depends(current_user)):
        """Revoke the share link of an own run."""
        jobs.set_share(run, None)
        record("share_revoke", user, run)
        return {"ok": True}

    # --- Favourite lines: marked on a run's map, listed in the app ---

    @app.get("/api/runs/{run}/favourites")
    def run_favourites(request: Request, run: str = Depends(run_access), user: ac.User = Depends(current_user)):
        """The user's favourite lines of a run (lat1,lon1,lat2,lon2), and the token the map's changes need:
        a map is a static page without one (an answer in JSON, which only a page of this site can read)."""
        marked = jobs.favourites(user.id, run)
        return {"lines": [f["line"] for f in marked], "in": {f["line"]: f["folder_id"] for f in marked},
                "folders": jobs.folders(user.id), "token": signed_in(request)[1] if public else token}

    @app.post("/api/runs/{run}/favourites")
    def set_favourite(body: Dict, run: str = Depends(run_access), user: ac.User = Depends(current_user)):
        """Mark a line of a run as a favourite or unmark it: {"line": "lat1,lon1,lat2,lon2", "favourite":
        true|false, "data": its values (FavouriteLine) when true, "folder": the user's folder (null or none:
        the top level)}; at most jobs.FAVOURITES_MAX per user."""
        try:
            line, data = checked_favourite(body)
            folder = checked_folder(body.get("folder"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            jobs.set_favourite(user.id, run, line, data, folder)
        except KeyError:
            raise HTTPException(404, "no such folder")
        except QuotaExceeded as e:
            refused(user, "favourites", str(e), e.status)
            raise HTTPException(e.status, str(e))
        record("favourite", user, run, {"line": line, "favourite": data is not None,
                                        **({"folder": folder} if data is not None and folder else {})})
        return {"ok": True}

    @app.post("/api/runs/{run}/favourites/folder")
    def move_favourite(body: Dict, run: str = Depends(run_access), user: ac.User = Depends(current_user)):
        """Move a favourite line of a run into a folder of the user: {"line", "folder": id or null}."""
        try:
            line = checked_line(body.get("line") if isinstance(body, dict) else None)
            folder = checked_folder(body.get("folder"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            jobs.move_favourite(user.id, run, line, folder)
        except KeyError:
            raise HTTPException(404, "no such favourite or folder")
        record("favourite", user, run, {"line": line, "folder": folder})
        return {"ok": True}

    @app.post("/api/favourites/folders")
    def add_folder(body: Dict, user: ac.User = Depends(current_user)):
        """A new folder of favourites: {"name", "parent": id or null (the top level)}; its id."""
        try:
            name = checked_folder_name(body.get("name") if isinstance(body, dict) else None)
            parent = checked_folder(body.get("parent"), "parent")
            folder = jobs.add_folder(user.id, name, parent)
        except KeyError:
            raise HTTPException(404, "no such folder")
        except QuotaExceeded as e:  # a ValueError too
            refused(user, "folders", str(e), e.status)
            raise HTTPException(e.status, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        record("favourite_folder", user, str(folder), {"do": "add", "parent": parent})
        return {"id": folder}

    @app.post("/api/favourites/folders/{folder}")
    def change_folder(folder: int, body: Dict, user: ac.User = Depends(current_user)):
        """Rename a folder ({"name"}) and/or move it ({"parent": id or null, the top level})."""
        if not isinstance(body, dict) or not ({"name", "parent"} & body.keys()):
            raise HTTPException(400, "name and/or parent")
        try:
            name = checked_folder_name(body["name"]) if "name" in body else None
            parent = checked_folder(body.get("parent"), "parent")
            jobs.change_folder(user.id, folder, name, parent, move="parent" in body)
        except KeyError:
            raise HTTPException(404, "no such folder")
        except ValueError as e:
            raise HTTPException(400, str(e))
        record("favourite_folder", user, str(folder), {"do": "change", **({"rename": True} if name else {}),
                                                       **({"parent": parent} if "parent" in body else {})})
        return {"ok": True}

    @app.delete("/api/favourites/folders/{folder}")
    def delete_folder(folder: int, user: ac.User = Depends(current_user)):
        """Delete a folder with the folders and favourite lines in it; how many of each went."""
        try:
            gone = jobs.delete_folder(user.id, folder)
        except KeyError:
            raise HTTPException(404, "no such folder")
        record("favourite_folder", user, str(folder), {"do": "delete", **gone})
        return gone

    @app.get("/api/favourites")
    def all_favourites(user: ac.User = Depends(current_user)):
        """The user's favourite lines in every run they may still open (a run that is no longer a demo
        drops out), with the run's name and date, newest first, and the user's folders."""
        found = [f for f in jobs.favourites(user.id) if may_open(user, f["run_id"])]
        meta = {r["id"]: r for r in rn.list_runs(runs_dir, {f["run_id"] for f in found})}
        return {"favourites": [{**f, "run_name": meta[f["run_id"]].get("name", f["run_id"]),
                                "run_created": meta[f["run_id"]].get("created", "")}
                               for f in found if f["run_id"] in meta],
                "folders": jobs.folders(user.id)}

    # --- The admin tab (the public site; every route Depends(require_admin)) ---

    def admin_only():
        if not public:
            raise HTTPException(404, "the admin tab is for the public site")

    def other_user(user_id: int, admin: ac.User = Depends(require_admin)) -> ac.User:
        """A user an administrator may change: not an administrator (FINDALINE_ADMINS decides those)."""
        admin_only()
        target = users.user(user_id)
        if target is None:
            raise HTTPException(404, "no such user")
        if target.admin:
            raise HTTPException(400, "administrators are set by FINDALINE_ADMINS")
        return target

    @app.get("/api/admin/status")
    def admin_status(user: ac.User = Depends(require_admin)):
        """The workers, the free disk of the data folder and the queue."""
        admin_only()
        import shutil
        waiting = jobs.waiting_or_running()
        return {"workers": jobs.workers(), "free_gb": round(shutil.disk_usage(data_dir).free / 1e9, 1),
                "free_gb_min": limits.free_disk_gb_min, "queue": len(waiting), "queue_max": limits.queue_max}

    @app.get("/api/admin/users")
    def admin_users(user: ac.User = Depends(require_admin)):
        admin_only()
        return {"users": users.listing()}

    @app.post("/api/admin/users/{user_id}")
    def admin_set_user(body: Dict, target: ac.User = Depends(other_user), admin: ac.User = Depends(require_admin)):
        """Approve, block or unblock a user ({"status": "approved"|"blocked"}; blocking ends its sessions,
        cancels its waiting and running jobs and deletes its waiting estimates and profiles) or lift its
        limits ({"unlimited": true|false})."""
        if body.get("status") in ("approved", "blocked"):
            users.set_status(target.id, body["status"])
            detail = {"status": body["status"], "was": target.status}
            if body["status"] == "blocked":
                detail["cancelled"] = [job["id"] for job in jobs.waiting_or_running()
                                       if job["user_id"] == target.id and jobs.request_cancel(job["id"])]
                detail["quick_deleted"] = jobs.quick_forget_user(target.id)
            record("user_status", admin, target.id, detail)
        elif isinstance(body.get("unlimited"), bool):
            users.set_unlimited(target.id, body["unlimited"])
            record("user_unlimited", admin, target.id, {"unlimited": body["unlimited"]})
        else:
            raise HTTPException(400, "status (approved, blocked) or unlimited (true, false)")
        return {"ok": True}

    @app.post("/api/admin/users/{user_id}/revoke")
    def admin_revoke(target: ac.User = Depends(other_user), admin: ac.User = Depends(require_admin)):
        """End every session of a user (they sign in again)."""
        users.revoke(target.id)
        record("user_revoke", admin, target.id)
        return {"ok": True}

    @app.get("/api/admin/runs")
    def admin_runs(user: ac.User = Depends(require_admin)):
        """Every run with its owner and visibility (the demo flag: POST /api/runs/<id>/demo)."""
        admin_only()
        meta = {r["id"]: r for r in rn.list_runs(runs_dir)}
        return {"runs": [{**row, "shared": bool(row["shared"]), "name": meta[row["run_id"]].get("name"),
                          "has_map": meta[row["run_id"]]["has_map"]}
                         for row in jobs.all_runs() if row["run_id"] in meta]}

    @app.get("/api/osm")
    async def osm_regions(lat: float, lon: float, user: ac.User = Depends(require_admin)):
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
    async def osm_job(body: Dict, user: ac.User = Depends(require_admin)):
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
        return {"id": await run_in_threadpool(queue, request, f"OSM {action}: {region}", user)}

    @app.get("/api/profile")
    async def line_profile(request: Request, line: str, run: str = Depends(run_access),
                           user: ac.User = Depends(current_user)):
        """The profile of a line of a saved run, computed from the cached data (search.profile_of_line,
        in the worker's quick lane). On the public site both points lie in the run's box with a margin
        of its --max-length, at most --max-length apart.

        A GET that costs computing time: it needs the X-Findaline-Profile header, which a
        foreign website cannot send without a CORS preflight this server does not allow.
        """
        if not request.headers.get("x-findaline-profile"):
            raise HTTPException(403, "header X-Findaline-Profile missing")
        try:
            points = [float(v) for v in line.split(",")]
        except ValueError:
            points = []
        if len(points) != 4 or not all(math.isfinite(v) for v in points):
            raise HTTPException(400, "line: lat1,lon1,lat2,lon2")
        if public:
            problem = await run_in_threadpool(profile_problem, os.path.join(runs_dir, run), points, user.admin)
            if problem:
                raise HTTPException(400, problem)
        await run_in_threadpool(within_rate, "profile", user)
        return await quick("profile", {"run": run, "line": points}, user, request)  # one at a time in the quick lane

    if public:
        @app.get("/runs/runs.js")
        def runs_js(request: Request):
            """The list of runs of the maps (their "Other runs"): the user's own and the demo runs (a
            visitor not signed in or not approved: the demo runs), and whether the map may compute
            profiles and measure, and mark favourite lines (FINDALINE_APP: signed-in, approved users only)."""
            user = visitor(request)
            listed = [rn._listed(r) for r in runs_of(user)]
            flags = {"profiles": user is not None, "favourites": user is not None}
            return Response(f"window.FINDALINE_RUNS = {json.dumps(listed, ensure_ascii=False)};\n"
                            f"window.FINDALINE_APP = {json.dumps(flags)};\n", media_type="text/javascript")

        @app.get("/runs/index.html")
        def runs_index():
            return RedirectResponse("/", 303)  # the app lists the runs

        # Share links: /s/<token>/map.html and its files; the map's ../runs.js and ../_viewer/ land here
        @app.get("/s/runs.js")
        def share_runs_js():
            """No list of runs (FINDALINE_RUNS stays undefined: the map shows its own run only), no
            profiles or measuring, no favourites."""
            return Response(f"window.FINDALINE_APP = {json.dumps({'profiles': False, 'favourites': False})};\n",
                            media_type="text/javascript")

        @app.get("/s/index.html")
        def share_index():
            return RedirectResponse("/", 303)

        @app.get("/privacy", response_class=HTMLResponse)
        def privacy():
            """What the site stores, why and for how long (no script)."""
            return privacy_page(public.contact, limits.keep_events_days)

        @app.get("/about", response_class=HTMLResponse)
        def about():
            """What the site is, and the data it uses with their licences and notices (no script)."""
            return about_page(public.contact)

        app.mount("/s", RunFiles(runs_dir, run_of_link), name="shared")

    app.mount("/app", StaticFiles(directory=APP_DIST), name="app")
    # Locally the runs folder as before; on the public site only the files of RUN_FILES of one's own runs
    app.mount("/runs", RunFiles(runs_dir, run_of_session) if public else StaticFiles(directory=runs_dir),
              name="runs")

    # Cached orthophoto tiles only (maps reach them as ../../cache/<folder>/wm256/..., also from share
    # links: /s/<token>/map.html). Open to anyone: copies of the tiles of public services, no user data
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
<meta name="findaline-user" content="{user}">
<title>Find-a-Line</title>
<link rel="stylesheet" href="/app/app.css">
</head><body><div id="app"></div>
<script src="/app/app.js"></script>
</body></html>
"""


# The logo's mark (as LogoMark in viewer/src/ui/icons.tsx) and the providers' marks of the sign-in buttons
LOGO_SVG = ('<svg viewBox="0 0 32 20" aria-hidden="true"><path class="u-lg" d="M3 5 Q16 15 29 5"/>'
            '<circle cx="3" cy="5" r="2.6"/><circle cx="29" cy="5" r="2.6"/></svg>')
PROVIDER_SVG = {
    "github": '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 '
              '7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 '
              '0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 '
              '.27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 '
              '3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9'
              '.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 '
              '3.58-8 8-8Z"/></svg>',
    "google": '<svg viewBox="0 0 48 48" aria-hidden="true"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 '
              '3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 '
              '9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 '
              '5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 '
              '28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 '
              '2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 '
              '1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>',
}
PAGE_FOOTER = ('<footer class="p-footer"><a href="/">Find-a-Line</a><a href="/privacy">Privacy</a>'
               '<a href="/about">About / data licences</a></footer>')
# The sign-in, privacy and about pages of the public site: server-rendered, no script, no inline styles;
# the kit's look (viewer/src/ui/pages.css, in /app/app.css), the theme of the system
SIGNIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Find-a-Line</title>
<link rel="stylesheet" href="/app/app.css">
</head><body class="ui p-body"><main class="p-page p-signin">
<header class="p-brand"><h1 class="u-logo">{logo}Find-a-Line</h1>
<p class="p-lead">Finds possible highlines in elevation data.</p></header>
{body}
{footer}
</main></body></html>
"""

PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Find-a-Line</title>
<link rel="stylesheet" href="/app/app.css">
</head><body class="ui p-body"><main class="p-page p-doc">
<header class="p-brand"><a class="u-logo" href="/">{logo}Find-a-Line</a></header>
<article class="p-card p-prose"><h1 class="u-h u-xl">{title}</h1>
{body}
</article>
{footer}
</main></body></html>
"""


def privacy_page(contact: str, keep_events_days: Optional[float]) -> str:
    """What the public site stores, why and for how long (facts of accounts.py, jobs.py, audit.py)."""
    import jobs as jb
    e = html.escape
    events = f"after {keep_events_days:g} days" if keep_events_days else "never automatically"
    items = [
        ("Your account", "The sign-in provider (GitHub or Google), its user id for you and your display name there, "
         "whether an administrator approved or blocked the account or lifted its limits, when it was made and when "
         "you last signed in. No e-mail address, no password. Why: to sign you in and to give you your searches. "
         "Kept as long as the account exists."),
        ("Sessions", f"A random token in a cookie (__Host-findaline); the site stores only its SHA-256 hash, with a "
         f"token against cross-site requests and when it was made and last used. A session ends when you sign out, "
         f"after {ac.SESSION_DAYS} days, or after {ac.IDLE_DAYS} days without a visit, and is then deleted. "
         f"Signing in uses a second, signed cookie for at most {ac.OAUTH_SECONDS // 60} minutes."),
        ("Searches", f"What you search (the place, the drawn or loaded area, the options), its title and status, "
         f"and its messages and progress. Why: to run the search and show it to you. The messages are deleted "
         f"{events} after the search ended; the search itself stays in your queue."),
        ("Runs", "The results of your searches (lines, maps, parameters, the place's name from OpenStreetMap) and "
         "their size. Kept until you delete them in Runs. A share link is stored as a SHA-256 hash only; it can be "
         "renewed or revoked."),
        ("Favourite lines", f"The lines you mark as favourites on a map: the run, the line's anchors and its values "
         f"(length, clearances, heights, angles, prominence), when you marked it and its folder; at most "
         f"{jb.FAVOURITES_MAX}. Why: to list them for you. Kept until you unmark them, delete their folder or the "
         f"run is deleted."),
        ("Folders of favourites", f"The names you give them, which folder each is in and when you made it; at most "
         f"{jb.FOLDERS_MAX}. Kept until you delete them (with what is in them)."),
        ("Quotas", f"How many km² your searches covered and how many uncached Nominatim requests they made, with the "
         f"time; deleted after {2 * jb.DAY / 86400:g} days. Request rates are counted in memory only."),
        ("Audit log", f"Sign-ins and sign-ups, approvals and blocks, searches started and cancelled, share links "
         f"made, renewed and revoked, deleted runs, the demo flag, favourite lines marked, unmarked and moved, "
         f"folders of favourites made, changed and deleted (not their names), other "
         f"administrators' actions and refusals by a "
         f"limit: the time, the action, the user id and what it concerned. Why: to find abuse and mistakes. "
         f"Deleted after {AUDIT_DAYS} days."),
        ("IP addresses", "The application stores none. The reverse proxy in front of it may log requests with "
         "their addresses to protect the site (its operator's configuration); sign-in requests are counted per "
         "address in memory only."),
    ]
    facts = [
        "Administrators can see every search and open every run, also private ones (to run the site and to "
        "help).",
        "A demo run, marked by an administrator, is open to everyone. The cached orthophoto tiles (copies of "
        "public services' images, no user data) are open to everyone.",
        "Searches ask public services from the server: Nominatim and Overpass (OpenStreetMap) and the services "
        "of the elevation data, with the places of the search, not who you are. Maps in your browser load "
        "background tiles (OpenStreetMap, OpenTopoMap, Esri, orthophoto services) directly: those services see "
        "your IP address.",
        "No analytics, no advertising, no tracking cookies.",
    ]
    body = ["<p>What this site stores about you, why, and for how long.</p><dl class='p-facts'>"]
    body += [f"<dt>{e(name)}</dt><dd>{e(text)}</dd>" for name, text in items]
    body.append("</dl><ul>" + "".join(f"<li>{e(text)}</li>" for text in facts) + "</ul>")
    body.append(f"<p>Questions, or to have your account and data deleted: {e(contact)}.</p>")
    return PAGE_HTML.format(title="Privacy", body="\n".join(body), logo=LOGO_SVG, footer=PAGE_FOOTER)


def about_page(contact: str) -> str:
    """What the site is, and the credits and licences of its data (sources.SOURCES, plus OpenStreetMap)."""
    import sources as src
    e = html.escape

    def link(text: str, url: str) -> str:
        return f"<a href='{e(url)}'>{e(text)}</a>" if url.startswith("https://") else e(text)
    credits = {}
    for source in src.SOURCES.values():
        for credit in source.credits + tuple(layer.credit for layer in (source.nmt, source.nmpt)
                                             if layer is not None and layer.credit is not None):
            credits.setdefault(credit.data, credit)
    credits.setdefault(src.WATER_MASK_CREDIT.data, src.WATER_MASK_CREDIT)
    rows = [f"<li><b>{e(c.data)}</b>: {e(c.attribution)}. Licence: {link(c.licence, c.url)}."
            + (f" <span class='u-small'>{e(c.notice)}</span>" if c.notice else "") + "</li>" for c in credits.values()]
    orthos = {s.ortho.name: s.ortho.attribution for s in src.SOURCES.values() if s.ortho is not None}
    body = [
        "<p>Find-a-Line finds possible highlines in elevation data: anchors on steep ground, every pair checked for "
        "clearance under the webbing. Its results are suggestions to check on the spot, not safety advice.</p>",
        "<h2>Elevation data</h2><ul>" + "".join(rows) + "</ul>",
        "<h2>OpenStreetMap</h2><p>Coastlines, cliffs, named areas and place names: © "
        "<a href='https://www.openstreetmap.org/copyright'>OpenStreetMap contributors</a>, under the Open Database "
        "Licence (ODbL 1.0); searched with Nominatim and the Overpass API, extracts from Geofabrik.</p>",
        "<h2>Orthophotos and background maps</h2><ul>"
        + "".join(f"<li>{e(name)}: {e(attribution)}</li>" for name, attribution in orthos.items())
        + "<li>OpenStreetMap tiles: © OpenStreetMap contributors</li><li>OpenTopoMap: © OpenTopoMap (CC-BY-SA)"
          "</li><li>Esri World Imagery: © Esri and its data providers</li></ul>",
        "<p>Each run names the data it used in its map and in its ATTRIBUTION.txt; results made from FABDEM may be "
        "shared only for non-commercial use, under the same licence.</p>",
        f"<p>Contact: {e(contact)}.</p>",
    ]
    return PAGE_HTML.format(title="About / data licences", body="\n".join(body), logo=LOGO_SVG,
                            footer=PAGE_FOOTER)


def demo_list(demos: List[Dict]) -> str:
    """The demo runs of the anonymous page: links to their maps (read-only)."""
    import runs as rn
    from urllib.parse import quote
    e = html.escape
    if not demos:
        return ""
    items = []
    for run in demos:
        summary = run.get("summary") or {}
        inner = (f"<b>{e(run.get('name') or run['id'])}</b><span class='u-small'>{summary.get('spots', 0)} spots · "
                 f"{summary.get('lines', 0)} lines</span><span class='u-small'>{e(rn.params_summary(run))}</span>")
        items.append(f"<li><a class='p-ex' href='/runs/{e(quote(run['id'], safe=''))}/map.html'>{inner}"
                     f"<span class='p-open'>Open map ›</span></a></li>" if run.get("has_map")
                     else f"<li><div class='p-ex'>{inner}</div></li>")
    return ("<section class='p-examples'><h2 class='u-h'>Examples</h2><ul class='p-cards'>" + "".join(items)
            + "</ul></section>")


def signin_page(providers: List[str], user=None, csrf: Optional[str] = None, note: str = "",
                demos: Optional[List[Dict]] = None) -> str:
    """The page of the public site for a visitor not signed in, or whose account is not approved
    (pending or blocked): sign-in links, or the account's state and signing out; the demo runs
    (their maps, read-only). No script, and nothing that changes state but signing out."""
    e = html.escape
    card = [f"<p class='p-error' role='alert'>{e(note)}</p>"] if note else []
    if user is None:
        card.append("<h2 class='u-h'>Sign in to search</h2><div class='p-providers'>" + "".join(
            f"<a class='p-provider p-{p}' href='/login/{p}'>{PROVIDER_SVG.get(p, '')}Sign in with {ac.NAMES[p]}</a>"
            for p in providers) + "</div>")
        card.append("<p class='u-small'>A new account waits for an administrator's approval. The site keeps your "
                    "provider's user id and display name, no e-mail address.</p>")
    else:
        state = ("is waiting for an administrator's approval" if user.status == "pending"
                 else "is blocked")
        card.append(f"<p>Your account <b>{e(user.name)}</b> ({e(ac.NAMES.get(user.provider, user.provider))}) "
                    f"{state}.</p>")
        card.append(f"<form method='post' action='/logout'><input type='hidden' name='csrf' value='{e(csrf or '')}'>"
                    "<button class='u-btn' type='submit'>Sign out</button></form>")
    parts = ["<section class='p-card'>" + "\n".join(card) + "</section>", demo_list(demos or [])]
    return SIGNIN_HTML.format(body="\n".join(parts), logo=LOGO_SVG, footer=PAGE_FOOTER)


async def _disconnect(receive):
    """Wait until the client of a request goes away (its body, if any, is read already)."""
    while (await receive())["type"] != "http.disconnect":
        pass


def client_key(address: str) -> str:
    """The key of a client's address for the rates of sign-ins: an IPv6 address by its /64 (one
    connection usually has a whole /64), anything else as it is."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 6:
        return str(ip.ipv4_mapped) if ip.ipv4_mapped else str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


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
                             "FINDALINE_TRUSTED_PROXY, FINDALINE_CONTACT, FINDALINE_SESSION_SECRET and a sign-in "
                             "provider (FINDALINE_GITHUB_CLIENT_ID/_SECRET, FINDALINE_GOOGLE_CLIENT_ID/_SECRET); implies --no-worker and --no-browser "
                             "(the worker runs as `findaline.py worker --public`)")
    args = parser.parse_args(argv)
    if not args.public and not is_loopback(args.host):
        parser.error("--host other than a loopback address needs --public (the local app has no sign-in)")
    import uvicorn
    public = PublicSettings.load() if args.public else None
    accounts = AccountSettings.load() if args.public else None
    data = cache.data_dir(args.data_dir)
    cache.check_layout(os.path.join(data, "cache"))
    app = create_app(data, port=args.port, start_scheduler=not args.no_worker, public=public, accounts=accounts)
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
