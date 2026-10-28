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
from typing import Callable, Dict, List, Optional

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

import accounts as ac
import cache
import params as pm
from jobs import TICK, Jobs, Scheduler, search_params
from settings import AccountSettings, Limits, PublicSettings

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


# The routes anyone may request in the public mode (signing in and out, the page, which shows a
# sign-in page to visitors, and its assets); every other route depends on current_user,
# require_admin or run_access (tests/test_public.py checks it)
PUBLIC_ROUTES = {"/", "/login/{provider}", "/auth/{provider}", "/logout", "/app", "/runs/index.html",
                 "/s/runs.js", "/s/index.html", "/cache/{path:path}"}
SHARE_BYTES = 32  # the random part of a share link
SSE_PER_USER = 3  # open event streams of one user (the public mode)
# The files of a run the public site serves (what mapping.save_map and runs.save_results write, without
# run.json: its parameters and data files are the owner's); never progress.json, tiles or other files
RUN_FILES = ("map.html", "data.js", "lines.js", "lines.csv", "lines.geojson", "ATTRIBUTION.txt")
VIEWER = "_viewer"  # mapping.VIEWER_FOLDER: the viewer bundle, the same for everyone
SAFE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")  # a run id, a token, a file name: no "..", no "/"


class RunFiles(StaticFiles):
    """The run folders of the public site, with access control (never a plain StaticFiles):
    <first>/<file> serves a file of RUN_FILES of the run `resolve(request, first)` gives (None:
    404, whether the run exists or not), _viewer/<asset> anyone. Starlette's StaticFiles does the
    rest: the path within the folder (symbolic links out of it are refused), ETag, Range."""

    def __init__(self, directory: str, resolve: Callable):
        super().__init__(directory=directory)
        self.resolve = resolve  # (request, first part of the path) -> the run's id, or None

    async def get_response(self, path: str, scope):
        from starlette.concurrency import run_in_threadpool
        from starlette.requests import Request
        root = scope.get("root_path", "")
        route = scope["path"][len(root):] if scope["path"].startswith(root) else scope["path"]
        parts = route.lstrip("/").split("/")
        if len(parts) != 2 or not all(SAFE_PART.fullmatch(part) or part == VIEWER for part in parts):
            raise StarletteHTTPException(404)
        first, name = parts
        if first != VIEWER:
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

    @contextlib.asynccontextmanager
    async def lifespan(_):
        if start_scheduler:
            scheduler.start()
        yield
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
        return bool(expected) and given is not None and secrets.compare_digest(given, expected)

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
        # The viewer and the app change with the code, run data with every run: always revalidate
        # (otherwise a browser may keep an old viewer.js next to a new data.js)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    if public:
        from starlette.middleware.sessions import SessionMiddleware

        from limits import parse_many, storage, strategies
        # A sign-in in progress (Authlib's state, nonce and PKCE verifier): a signed cookie of ten minutes
        app.add_middleware(SessionMiddleware, secret_key=accounts.session_secret.get_secret_value(),
                           session_cookie=ac.OAUTH_COOKIE, max_age=ac.OAUTH_SECONDS, path="/", same_site="lax",
                           https_only=True)
        oauth = ac.oauth_clients(accounts, oauth_transport)
        login_limiter = strategies.MovingWindowRateLimiter(storage.MemoryStorage())
        login_rates = parse_many(ac.LOGIN_RATE)

    # Only our own host names (DNS rebinding; behind the proxy the public host), before the guard
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts, www_redirect=False)

    def client_of(request: Request, provider: str):
        """Authlib's client of a provider, after the limit of sign-in requests per IP address."""
        client = oauth.create_client(provider) if public and provider in accounts.providers else None
        if client is None:
            raise HTTPException(404, "no such sign-in")
        address = request.client.host if request.client else "?"
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
        return signin_page(accounts.providers, *(found or (None, None)))

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
            user = await run_in_threadpool(users.sign_in, provider, subject, name)
        except ac.SignupClosed:
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
        if signed_in(request) and not csrf_ok(request, given):
            raise HTTPException(403, "missing or wrong token")
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

    def may_open(user: ac.User, run_id: str) -> bool:
        """Whether a user may open a run: on the public site its owner (an administrator: every run
        with an owner's row); locally every saved run."""
        if not SAFE_PART.fullmatch(run_id) or not os.path.isfile(os.path.join(runs_dir, run_id, "run.json")):
            return False
        if not public:
            return True
        row = jobs.run(run_id)
        return row is not None and (row["user_id"] == user.id or user.admin)

    def run_access(run: str, user: ac.User = Depends(current_user)) -> str:
        """A run its user may open (404 otherwise, also for runs of others)."""
        if not may_open(user, run):
            raise HTTPException(404, "no such run")
        return run

    def own_run(run: str = Depends(run_access), user: ac.User = Depends(current_user)) -> str:
        """A run its user owns (sharing it): run_access, and the owner only."""
        if public and (jobs.run(run) or {}).get("user_id") != user.id:
            raise HTTPException(404, "no such run")
        return run

    def run_of_session(request: Request, run_id: str) -> Optional[str]:
        """RunFiles of /runs: the run, if the request's user may open it."""
        found = signed_in(request)
        return run_id if found and found[0].approved and may_open(found[0], run_id) else None

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
        jobs.adopt_runs(runs_dir, users.first_admin())
        import mapping
        mapping.install_viewer(runs_dir)  # the viewer of share links, also before the first run

    streams: Dict[Optional[int], int] = {}  # open event streams per user

    @app.get("/api/schema")
    def get_schema(user: ac.User = Depends(current_user)):
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
    async def estimate(body: Dict, user: ac.User = Depends(current_user)):
        return await quick("estimate", parse(body))

    @app.post("/api/jobs")
    async def add_job(body: Dict, user: ac.User = Depends(current_user)):
        request = parse(body)
        try:
            params = await run_in_threadpool(search_params, request)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": await run_in_threadpool(jobs.add, request, title(params), user.id)}

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
            parse(request)  # the limits may have changed since
        return {"id": jobs.add(request, title, user.id)}

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job: Dict = Depends(visible_job)):
        return {"events": jobs.events(0, job["id"])}

    @app.delete("/api/jobs/{job_id}")
    def cancel(job: Dict = Depends(visible_job)):
        """A waiting job is cancelled at once, a running one by its worker (which kills its processes)."""
        if not jobs.request_cancel(job["id"]):
            raise HTTPException(409, "the job is not waiting or running")
        return {"ok": True}

    @app.get("/api/events")
    async def events(request: Request, since: int = 0, user: ac.User = Depends(current_user)):
        """The events of the user's jobs (an administrator's: of all) after `since`, as Server-Sent Events;
        at most SSE_PER_USER streams per user on the public site."""
        # A reconnecting EventSource sends the last id it got
        last = request.headers.get("last-event-id", "")
        start = max(since, int(last) if last.isdigit() else 0)
        if public and streams.get(user.id, 0) >= SSE_PER_USER:
            raise HTTPException(429, f"at most {SSE_PER_USER} open pages per user")
        whose = seen_by(user)

        def look(seq: int):
            return jobs.events(seq, user_id=whose), jobs.worker_alive()

        async def stream():
            # sse-starlette ends the generator when the client goes away (or the server stops)
            seq, worker = start, None
            streams[user.id] = streams.get(user.id, 0) + 1  # counted while the generator runs
            try:
                while True:
                    found, alive = await run_in_threadpool(look, seq)
                    for event in found:
                        seq = event["seq"]
                        yield {"id": str(seq), "data": json.dumps(event)}
                    if alive != worker:  # whether a worker runs: no id, it is not a stored event
                        worker = alive
                        yield {"data": json.dumps({"kind": "worker", "data": {"alive": alive}})}
                    await asyncio.sleep(TICK)
            finally:
                streams[user.id] -= 1
        # A comment every PING seconds keeps proxies from closing a quiet stream and finds dead
        # connections; a client that takes no data for SEND_TIMEOUT seconds is dropped
        return EventSourceResponse(stream(), ping=PING, send_timeout=SEND_TIMEOUT)

    def runs_of(user: ac.User) -> List[Dict]:
        """The runs a user's lists show: their own on the public site (an administrator's too), all locally."""
        return rn.list_runs(runs_dir, jobs.runs_of(user.id) if public else None)

    @app.get("/api/runs")
    def list_runs(user: ac.User = Depends(current_user)):
        """The user's runs (locally all)."""
        shared = jobs.shared_runs(user.id) if public else set()
        return {"public": bool(public),  # whether runs can be shared (by a link)
                "runs": [{k: r.get(k) for k in ("id", "name", "created", "summary", "has_map")}
                         | {"description": rn.params_summary(r), "shared": r["id"] in shared}
                         for r in runs_of(user)]}

    @app.post("/api/runs/{run}/share")
    def share_run(run: str = Depends(own_run)):
        """A share link of an own run (the map for anyone who has the link); a new one replaces the old.
        Only its sha256 is kept, so the link is shown this once."""
        if not public:
            raise HTTPException(404, "share links are for the public site")
        token = secrets.token_urlsafe(SHARE_BYTES)
        jobs.set_share(run, ac.digest(token))
        return {"url": f"{public.public_url}/s/{token}/map.html"}

    @app.delete("/api/runs/{run}/share")
    def unshare_run(run: str = Depends(own_run)):
        """Revoke the share link of an own run."""
        jobs.set_share(run, None)
        return {"ok": True}

    @app.get("/api/osm")
    async def osm_regions(lat: float, lon: float, user: ac.User = Depends(current_user)):
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
        return {"id": await run_in_threadpool(jobs.add, request, f"OSM {action}: {region}", user.id)}

    @app.get("/api/profile")
    async def line_profile(request: Request, line: str, run: str = Depends(run_access)):
        """The profile of a line of a saved run, computed from the cached data (search.profile_of_line,
        in the worker's quick lane).

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
        return await quick("profile", {"run": run, "line": points})  # one at a time in the quick lane

    if public:
        @app.get("/runs/runs.js")
        def runs_js(user: ac.User = Depends(current_user)):
            """The list of runs of the maps (their "Other runs"): the user's own, and whether the map may
            compute profiles and measure (FINDALINE_APP)."""
            listed = [rn._listed(r) for r in runs_of(user)]
            return Response(f"window.FINDALINE_RUNS = {json.dumps(listed, ensure_ascii=False)};\n"
                            f"window.FINDALINE_APP = {json.dumps({'profiles': True})};\n",
                            media_type="text/javascript")

        @app.get("/runs/index.html")
        def runs_index():
            return RedirectResponse("/", 303)  # the app lists the runs

        # Share links: /s/<token>/map.html and its files; the map's ../runs.js and ../_viewer/ land here
        @app.get("/s/runs.js")
        def share_runs_js():
            """No list of runs (FINDALINE_RUNS stays undefined: the map shows its own run only) and no
            profiles or measuring."""
            return Response(f"window.FINDALINE_APP = {json.dumps({'profiles': False})};\n",
                            media_type="text/javascript")

        @app.get("/s/index.html")
        def share_index():
            return RedirectResponse("/", 303)

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


SIGNIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Find-a-Line</title>
<link rel="stylesheet" href="/app/app.css">
</head><body><div class="fa-signin">
<h1>Find-a-Line</h1>
<p>Finds possible highlines in elevation data.</p>
{body}
</div></body></html>
"""


def signin_page(providers: List[str], user=None, csrf: Optional[str] = None, note: str = "") -> str:
    """The page of the public site for a visitor not signed in, or whose account is not approved
    (pending or blocked): sign-in links, or the account's state and signing out. No script."""
    e = html.escape
    parts = [f"<p class='fa-error'>{e(note)}</p>"] if note else []
    if user is None:
        parts.append("<p>Sign in to search:</p><p class='fa-signin-links'>" + " ".join(
            f"<a class='fa-signin-link' href='/login/{p}'>Sign in with {ac.NAMES[p]}</a>" for p in providers) + "</p>")
        parts.append("<p class='fa-hint'>A new account waits for an administrator's approval. The site keeps your "
                     "provider's user id and display name, no e-mail address.</p>")
    else:
        state = ("is waiting for an administrator's approval" if user.status == "pending"
                 else "is blocked")
        parts.append(f"<p>Your account <b>{e(user.name)}</b> ({e(ac.NAMES.get(user.provider, user.provider))}) "
                     f"{state}.</p>")
        parts.append(f"<form method='post' action='/logout'><input type='hidden' name='csrf' value='{e(csrf or '')}'>"
                     "<button type='submit'>Sign out</button></form>")
    return SIGNIN_HTML.format(body="\n".join(parts))


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
