"""
Screenshots of the app for a visual review of a UI change (CLAUDE.md, "Verifying changes"): every page of
PAGES on a desktop (Chromium, 1400 × 900) and on two phones (Playwright's device profiles: iPhone 13 on
WebKit, Safari's engine; Pixel 7 on Chromium), each with its viewport, pixel ratio, touch and user agent,
in the light theme and optionally the dark one.

The site is the public one (signed in as an administrator, so Admin and the OSM extracts are there;
the "-limited" pages as an approved user with the site's limits), run
from a scratch data folder with a run of the Żabi Koń clip (tests/data, computed offline), a problem
report and a worker of this process (the estimates of a new search need its quick lane). It is served over
HTTPS with a certificate made for the run: WebKit, unlike Chromium, sends no Secure cookie (the session's
`__Host-` cookie) to http://localhost.

    uv sync --group ui
    uv run playwright install chromium webkit
    uv run tools/ui_shots.py --out <folder> [--only d-] [--dark]

The "map" pages are a run's own map.html (from the server and by its share link). The files are
<page>-<device>-<theme>.png; a page whose click finds nothing on a device is left out.
Phone screenshots of the viewport only (the shells scroll inside, not the page).
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Dummy settings of the public site: nobody signs in through a provider, the session is made here
ACCOUNTS = {"FINDALINE_SESSION_SECRET": "ui-shots-" * 5, "FINDALINE_GITHUB_CLIENT_ID": "id",
            "FINDALINE_GITHUB_CLIENT_SECRET": "secret", "FINDALINE_ADMINS": "github:1"}

# name -> (interface ("map": the run's map.html, "shared": it by its share link), hash, actions after loading or None: a CSS selector to click, "scroll:<selector>" to
# scroll that element (or the page) to its end, "low:<selector>" to click the last one scrolled to the
# bottom of its panel (a "?" near the bottom: its bubble opens upwards), or a list of such steps[, the user:
# "admin" (default), "walker", an approved user with the site's limits, or "visitor", not signed in]);
# "{run}" is the clip's run; an interface starting with "/" is a path of web.py's server-rendered pages
MORE = [".s-more-head", ".s-topic:nth-of-type(2) > summary"]  # More settings, Anchors opened (Lines is)
PAGES = {
    "d-home": ("d", "", None),
    "d-menu": ("d", "", ".c-account-btn"),        # the account menu (desktop: a popover)
    "d-account": ("d", "#account", None),         # the account page (phones)
    "d-account-end": ("d", "#account", "scroll:.c-main"),
    "d-new": ("d", "#new", None),
    "d-new-hint": ("d", "#new", ".u-hint"),       # the first "?" of the search panel, its bubble open
    "d-new-intro": ("d", "#new", ".s-intro .n-intro-link"),  # "How a search works" (ui/Intro.tsx)
    "d-new-more": ("d", "#new", MORE),
    "d-new-more-hint": ("d", "#new", MORE + [".s-topics details[open] .u-hint"]),  # a setting's "?" (no bounds: admin)
    "d-new-low-hint": ("d", "#new", MORE + ["low:.s-topics details[open] .u-hint"]),  # the last "?" at the bottom: its bubble above it
    "d-new-limited": ("d", "#new", ".s-intro .n-intro-link", "walker"),  # the intro with the limits
    "d-new-limited-hint": ("d", "#new", MORE + [".s-topics details[open] .u-hint"], "walker"),  # a "?" with the bounds
    "d-result": ("d", "#run={run}", None),
    "d-result-intro": ("d", "#run={run}", ".r-filters .n-intro-link"),  # "How to read the result"
    "d-favourites": ("d", "#favourites", None),
    "d-admin": ("d", "#admin", None),
    "d-osm": ("d", "#osm", None),
    "b-search": ("b", "#search", None),
    "b-search-hint": ("b", "#search", ".u-hint"),
    "b-search-low-hint": ("b", "#search", "low:.u-hint"),  # the last "?" at the bottom: its bubble above it
    "b-searches": ("b", "#searches", None),
    "b-result": ("b", "#run={run}", None),
    "b-admin": ("b", "#admin", None),
    "b-osm": ("b", "#osm", None),
    "c-home": ("c", "", None),
    "c-result": ("c", "#run={run}", None),
    "map": ("map", "", None),
    "map-line": ("map", "", ".r-rows .r-row"),  # a spot selected: its details and profile
    "map-shared": ("shared", "", None),
    "page-signin": ("/", "", None, "visitor"),
    "page-privacy": ("/privacy", "", None),
    "page-about": ("/about", "", None),
}

# The "low:" step: the last matching element is scrolled to just above the bottom of the visible part of
# its scrolling panel (or above a bar that stays there, data-hint-floor), then marked for the click
LOWEST = """(sel) => {
  const all = [...document.querySelectorAll(sel)].filter((e) => e.checkVisibility({visibilityProperty: true}));
  const e = all[all.length - 1];
  if (!e) return;
  let box = e.parentElement;
  while (box && !/auto|scroll/.test(getComputedStyle(box).overflowY)) box = box.parentElement;
  box = box || document.scrollingElement;
  let bottom = Math.min(box.getBoundingClientRect().bottom || innerHeight, innerHeight);
  for (const f of box.querySelectorAll("[data-hint-floor]")) {
    if (getComputedStyle(f).position === "sticky") bottom = Math.min(bottom, f.getBoundingClientRect().top);
  }
  box.scrollTop += e.getBoundingClientRect().bottom - (bottom - 24);
  e.setAttribute("data-shot-low", "");
}"""

INTROS = ("search", "result")  # the intros of ui/Intro.tsx (localStorage "findaline.intro.<name>")

DESKTOP = {"viewport": {"width": 1400, "height": 900}, "device_scale_factor": 1}
DEVICES = {"desktop": ("chromium", None), "iphone": ("webkit", "iPhone 13"), "pixel": ("chromium", "Pixel 7")}


def clip_run(data: str) -> str:
    """A run of the Żabi Koń clip in this data folder (offline, as tests/test_public.py's fixture)."""
    env = {**os.environ, "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
           "PYTHONIOENCODING": "utf-8"}
    clip = os.path.join(REPO, "tests", "data", "zabi_kon_nmt.tif")
    result = subprocess.run(
        [sys.executable, "findaline.py", "--elevation-file", clip, "--northing", "254608", "--easting", "553966",
         "--radius", "150", "--water", "off", "--fill-gaps", "off", "--no-plot", "--yes", "--ortho", "never",
         "--nmpt", "off", "--name", "Żabi Koń clip", "--data-dir", data],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=600, env=env)
    if result.returncode:
        sys.exit(result.stdout + result.stderr)
    runs = os.path.join(data, "runs")
    return next(d for d in os.listdir(runs) if os.path.isfile(os.path.join(runs, d, "run.json")))


def certificate(folder: str):
    """A self-signed certificate of localhost for this run: (certificate file, key file)."""
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = os.path.join(folder, "localhost.pem"), os.path.join(folder, "localhost-key.pem")
    with open(cert_file, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_file, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cert_file, key_file


def serve(data: str):
    """The public site on a free port of 127.0.0.1, with a worker; (address, session cookies by user, run,
    the run's share token, stop)."""
    import uvicorn

    import accounts as ac
    import web
    from jobs import Jobs, Scheduler
    from settings import AccountSettings, PublicSettings

    os.environ.update(ACCOUNTS)
    settings = AccountSettings.load()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    site = f"https://localhost:{port}"
    users = ac.Accounts(data, settings)
    admin = users.sign_in("github", "1", "admin")
    cookie, _ = users.new_session(admin.id)
    climber = users.sign_in("github", "11", "a climber")  # a user waiting for approval, in Admin's list
    walker = users.sign_in("github", "12", "a walker")  # approved, with the limits of the site
    users.set_status(walker.id, "approved")
    cookies = {"admin": cookie, "walker": users.new_session(walker.id)[0]}
    jobs = Jobs(data, public=True)
    run = clip_run(data)
    jobs.add_run(run, admin.id, 1)
    share = "ui-shots-share-" + "x" * 30
    jobs.set_share(run, ac.digest(share))
    jobs.add_report(climber.id, "water", "The lake in the profile is 3 m too high near the second anchor.",
                    {"run": run, "page": f"#run={run}", "ui": "d", "viewport": "390x844",
                     "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"})
    scheduler = Scheduler(jobs, public=True)
    scheduler.start()
    public = PublicSettings(public_url=site, trusted_proxy="127.0.0.1", contact="admin@example.org")
    app = web.create_app(data, public=public, accounts=settings)
    cert_file, key_file = certificate(data)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           ssl_certfile=cert_file, ssl_keyfile=key_file))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)

    def stop():
        server.should_exit = True
        scheduler.stop()
    return site, cookies, run, share, stop


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="folder of the screenshots")
    parser.add_argument("--only", default="", help="pages whose name contains this (comma-separated)")
    parser.add_argument("--devices", default="desktop,iphone,pixel", help="of: " + ", ".join(DEVICES))
    parser.add_argument("--dark", action="store_true", help="the dark theme too")
    parser.add_argument("--data-dir", help="a scratch data folder (default: a new temporary one)")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    os.makedirs(args.out, exist_ok=True)
    data = args.data_dir or tempfile.mkdtemp(prefix="ui-shots-")
    site, cookies, run, share, stop = serve(data)
    wanted = [w for w in args.only.split(",") if w]
    pages = {n: p for n, p in PAGES.items() if not wanted or any(w in n for w in wanted)}
    themes = ["light", "dark"] if args.dark else ["light"]
    try:
        with sync_playwright() as pw:
            for device in args.devices.split(","):
                engine, profile = DEVICES[device]
                browser = getattr(pw, engine).launch()
                options = dict(pw.devices[profile]) if profile else dict(DESKTOP)
                options.pop("default_browser_type", None)
                for theme in themes:
                    context = browser.new_context(**options, color_scheme=theme, ignore_https_errors=True)
                    # the theme, and the intros put away (ui/Intro.tsx): the "-intro" pages open them
                    context.add_init_script(f"try {{ localStorage.setItem('findaline-theme', {json.dumps(theme)}); "
                                            + "".join(f"localStorage.setItem('findaline.intro.{intro}', 'seen'); "
                                                      for intro in INTROS) + "} catch (e) {}")
                    for name, (ui, hash_, steps, *who) in pages.items():
                        context.clear_cookies()
                        user = who[0] if who else "admin"
                        if user != "visitor":
                            context.add_cookies([{"name": "__Host-findaline", "value": cookies[user],
                                                  "domain": "localhost", "path": "/", "secure": True,
                                                  "httpOnly": True, "sameSite": "Lax"}])
                        page = context.new_page()  # a fresh page each: no menu or route of the one before
                        start = {"map": f"/runs/{run}/map.html", "shared": f"/s/{share}/map.html"}.get(
                            ui, ui if ui.startswith("/") else f"/?ui={ui}")
                        page.goto(f"{site}{start}{hash_.format(run=run)}")
                        page.wait_for_timeout(2500)  # the event stream stays open: no "network idle"
                        try:  # the maps' tiles (OpenTopoMap can be slow): an empty map is no finding
                            page.wait_for_function("!document.querySelector('.leaflet-tile:not(.leaflet-tile-loaded)')",
                                                   timeout=10000)
                        except Exception:
                            print(f"{name} ({device}): map tiles still loading")
                        missing = False
                        for step in ([steps] if isinstance(steps, str) else steps or []):
                            if step.startswith("scroll:"):
                                page.evaluate("(sel) => { const e = document.querySelector(sel) || "
                                              "document.scrollingElement; e.scrollTop = e.scrollHeight; }", step[7:])
                                page.wait_for_timeout(400)
                                continue
                            if step.startswith("low:"):  # the last one, at the bottom of its panel
                                page.evaluate(LOWEST, step[4:])
                                step = "[data-shot-low]"
                            target = page.query_selector(step)
                            if not (target and target.is_visible()):
                                missing = True
                                break
                            if step != "[data-shot-low]":
                                target.scroll_into_view_if_needed()
                            target.click()
                            page.wait_for_timeout(600)
                        if missing:  # this device does not show it
                            page.close()
                            continue
                        path = os.path.join(args.out, f"{name}-{device}-{theme}.png")
                        page.screenshot(path=path)
                        page.close()
                        print(path)
                    context.close()
                browser.close()
    finally:
        stop()


if __name__ == "__main__":
    main()
