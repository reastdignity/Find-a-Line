"""Prototype B of the new interface (?ui=b, viewer/src/app/b) in headless Edge: the search on the map,
the search followed after Start until its result opens on the same map, the results (details, profile,
‹ ›, filters), favourites, the routes in the hash with back and forward, phones, and the public site
(the Share action, the Admin item). Offline: the Żabi Koń clip, a fixed estimate, and the job's events
played by the test page through a stand-in for the event stream (headless Edge would wait for a real one)."""

import json
import os
import re
import urllib.parse

import pytest

from test_public import account_settings, clip_run  # noqa: F401 (fixtures)
from test_web import CSP_STUB, EDGE, TOKEN, LOCAL, _clip_run, _edge_page, _lines_of, _result_of, _violations, web

pytest.importorskip("httpx2")
from fastapi.testclient import TestClient  # noqa: E402

# Before the app: the event stream is a stand-in the script plays events through (window.emit), the
# estimate is fixed, the clipboard records what it gets
B_STUBS = CSP_STUB + """
window.SOURCES = [];
window.EventSource = class { constructor() { SOURCES.push(this); } close() {} };
window.emit = (event) => SOURCES.forEach((s) => s.onmessage && s.onmessage({data: JSON.stringify(event)}));
window.COPIED = [];
Object.defineProperty(navigator, "clipboard", {value: {writeText: (text) => { COPIED.push(text); return Promise.resolve(); }}});
const realFetch = window.fetch;
window.fetch = (url, init) => String(url).startsWith("/api/estimate")
  ? Promise.resolve(new Response(JSON.stringify({source: "test", resolution: 1, km2: null, tiles: 1, download_mb: 3,
      work_gb: 0, reasons: [], messages: [], area: null}), {headers: {"content-type": "application/json"}}))
  : realFetch(url, init);
"""

# Helpers of the scripts: a result written at the end, waits, steps run one after another
HELPERS = """
const out = document.createElement("pre");
out.id = "result";
const result = {};
const done = () => { out.textContent = JSON.stringify(result); document.body.appendChild(out); };
const $ = (s) => document.querySelector(s), $$ = (s) => [...document.querySelectorAll(s)];
const text = (s) => ($(s) || {}).textContent || "";
const button = (label, root = document) => [...root.querySelectorAll("button")].find((b) =>
  b.textContent.trim() === label || b.getAttribute("aria-label") === label);
const rows = () => $$(".r-rows > .r-spot > .r-row");
const later = (then, ms = 300) => setTimeout(then, ms);
let step = "";
const wait = (ok, then, tries = 150) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 200)
                                                  : (result.timeout = step, done());
const play = (steps) => {
  const names = Object.keys(steps);
  const go = (k) => { if (k >= names.length) return done(); step = names[k]; steps[step](() => go(k + 1)); };
  go(0);
};
"""

FLOW_SCRIPT = HELPERS + """
const RUN = window.TEST_RUN;
const model = () => window.FINDALINE_RESULT && window.FINDALINE_RESULT.model;
const saved = () => fetch("/api/runs/" + encodeURIComponent(RUN) + "/favourites").then((r) => r.json());
let job = "", favourite = "";
play({
  search(next) { wait(() => window.FINDALINE_PLACE && $(".s-panel") && $(".u-rail"), () => {
    result.rail = $$(".u-rail-item").map((b) => b.textContent.trim());
    result.preset = text(".s-preset.u-on");
    result.estimateFirst = text(".s-est-line");
    window.FINDALINE_PLACE.map.fire("click", {latlng: {lat: 50.1559508, lng: 19.7562722}});
    wait(() => text(".s-est-line").startsWith("Ready"), () => { result.estimate = text(".s-est-row"); next(); });
  }); },
  start(next) {
    button("Find lines").click();
    wait(() => $(".b-following") && $(".b-following .b-card"), () => {
      job = $(".b-following").dataset.started;
      result.following = {job, card: $(".b-following .b-card").dataset.job, status: text(".b-following .u-st"),
                          findLines: !!button("Find lines"), searchVisible: window.FINDALINE_PLACE.searchVisible};
      next();
    });
  },
  progress(next) {
    let seq = 1e6;
    emit({seq: ++seq, job, kind: "status", data: {status: "downloading"}});
    emit({seq: ++seq, job, kind: "progress", data: {bar: "Download", stage: "tiles", count: 1, total: 4, unit: "MB",
                                                    extra: {}, done: false}});
    later(() => {
      result.downloading = [text(".b-following .u-st"), text(".b-following .b-step.b-now")];
      emit({seq: ++seq, job, kind: "status", data: {status: "computing"}});
      emit({seq: ++seq, job, kind: "progress", data: {bar: "Compute", stage: "pairs", count: 1, total: 2, unit: "",
                                                      extra: {}, done: false, step: "Step 3/5: evaluating anchor pairs"}});
      later(() => {
        result.computing = [text(".b-following .b-step.b-now"),
                            +$(".b-following [role=progressbar]").getAttribute("aria-valuenow")];
        emit({seq: ++seq, job, kind: "status", data: {status: "done", run_id: RUN, finished: new Date().toISOString()}});
        wait(() => button("Show results", $(".b-following")) && text(".b-following .b-counts"), () => {
          result.counts = text(".b-following .b-counts");
          later(() => { result.toast = !!$(".b-toast"); next(); }, 500);  // the panel shows it: no toast
        });
      });
    });
  },
  results(next) {
    button("Show results", $(".b-following")).click();
    wait(() => rows().length && model(), () => {
      result.hash = location.hash;
      result.spots = model().shown.spots;
      result.sort = [model().list.sort, model().list.desc];
      result.searchHidden = !window.FINDALINE_PLACE.searchVisible;
      result.pill = text(".b-pill-name");
      result.sameMap = window.FINDALINE_MAP === window.FINDALINE_PLACE.map;
      // A run's orthophoto joins the base layers of the one map and leaves with the result (the clip's
      // run has none: given by hand), the default layer shown again when it was the one shown
      const place = window.FINDALINE_PLACE, labels = () => $$(".leaflet-control-layers-base label").map((l) => l.textContent.trim());
      const shown = () => labels().filter((_, k) => $$(".leaflet-control-layers-base input")[k].checked);
      const before = labels();
      place.setRunOrtho({name: "Test ortho", url: null, layer: "", attribution: "", base: {url: "/cache/none/{z}/{x}/{y}.jpg",
                         max_zoom: 18}, cache: null}, 21);
      const added = labels();
      $$(".leaflet-control-layers-base label").find((l) => l.textContent.trim() === "Test ortho").querySelector("input").click();
      const chosen = shown();
      place.setRunOrtho(null);
      result.ortho = [before.includes("Test ortho"), added.includes("Test ortho"), chosen, labels().includes("Test ortho"), shown()];
      next();
    });
  },
  select(next) {
    rows()[0].click();
    wait(() => $(".r-details .r-dtitle") && $(".b-dock .fal-profile-title"), () => {
      const l = model().selected.line;
      result.title = text(".r-details .r-dtitle");
      result.expected = l.len.toFixed(0) + " m · " + l.clr.toFixed(1) + " m clear";
      result.eyebrow = text(".r-details .r-eyebrow");
      result.profile = text(".b-dock .fal-profile-title");
      result.lineHash = location.hash === "#run=" + encodeURIComponent(RUN) + "&line=" + encodeURIComponent(model().lineLink(l));
      button("Next spot").click();
      later(() => { result.stepped = [text(".r-details .r-eyebrow"), model().position]; next(); });
    });
  },
  filter(next) {
    const before = model().shown.spots;
    button("Clearance").click();
    wait(() => $(".r-editor input[type=number]"), () => {
      const input = $(".r-editor input[type=number]");
      input.value = "1";
      input.dispatchEvent(new Event("change", {bubbles: true}));
      later(() => {
        result.filtered = [before, model().shown.spots, text(".r-summary-count")];
        button("Remove the clearance filter").click();
        later(() => { result.cleared = model().shown.spots; next(); });
      });
    });
  },
  copy(next) {
    if (!model().selected) rows()[0].click();
    $(".r-details .r-copy").click();
    later(() => { result.copied = COPIED[0] || ""; next(); });
  },
  favourite(next) {
    if (!model().selected) rows()[0].click();
    favourite = model().lineLink(model().selected.line);
    $(".r-details .r-fav").click();
    const until = (tries) => saved().then((body) => body.lines.length || !tries ? next() : later(() => until(tries - 1)));
    until(30);
  },
  favourites(next) {
    button("Favourites").click();
    wait(() => $(".b-fav"), () => {
      result.favs = $$(".b-fav").length;
      result.favHash = location.hash;
      $(".b-fav-open").click();
      wait(() => model() && model().selected && location.hash.startsWith("#run="), () => {
        result.favOpened = model().lineLink(model().selected.line) === favourite;
        next();
      });
    });
  },
  back(next) {
    history.back();
    wait(() => $(".b-fav") && !$(".b-results"), () => {
      result.back = location.hash;
      history.forward();
      wait(() => $(".b-results") && model() && model().selected, () => {
        result.forward = model().lineLink(model().selected.line) === favourite;
        button("Back").click();
        wait(() => $(".b-fav"), () => { result.pillBack = location.hash; next(); });
      });
    });
  },
  newSearch(next) {
    button("Search").click();
    wait(() => $(".b-following") && window.FINDALINE_PLACE.searchVisible, () => {
      button("New search").click();
      wait(() => $(".s-panel"), () => {
        result.newSearch = [text(".s-est-line"), text(".s-preset.u-on")];
        button("My searches").click();
        wait(() => $(`.b-card[data-run="${RUN}"]`) && button("Edit & run again"), () => {
          button("Edit & run again").click();
          wait(() => location.hash === "#search" && text(".s-point").includes("50.15595, 19.75627"), () => {
            result.edited = [text(".s-point"), text(".s-preset.u-on"), !!button("Find lines")];
            next();
          });
        });
      });
    });
  },
});
"""


def _b_page(tmp_path, script: str, folder: str = "shellb", extra: str = "") -> str:
    """A copy of the app's page in runs/<folder>/ with the stubs before and the script after the app."""
    path = tmp_path / "runs" / folder
    path.mkdir(parents=True)
    (path / "stubs.js").write_text(B_STUBS + extra, encoding="utf-8")
    (path / "test.js").write_text(script, encoding="utf-8")
    app = web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)
    with TestClient(app, base_url=LOCAL) as client:
        page = client.get("/").text
    (path / "index.html").write_text(page.replace(
        '<script src="/app/app.js"></script>',
        '<script src="stubs.js"></script><script src="/app/app.js"></script><script src="test.js"></script>'),
        encoding="utf-8")
    return f"runs/{folder}/index.html"


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_search_follows_into_results(tmp_path, monkeypatch):
    """W2, W4, W6 and the routes on a desktop: a click on the map gives the estimate (Typical highline
    preset), Find lines queues the job and the panel follows it (no second start); its events move the
    steps and the bar, and when it is done a toast offers Show results, which opens the run on the same
    map (search layers hidden, sorted by clearance); a row shows its details and profile, ‹ › steps, a
    filter chip changes the counts; the line is in the hash and its copied link; a favourite is in
    Favourites without a reload and opens its line; back and forward; New search clears the place."""
    run_id = _clip_run(tmp_path, monkeypatch)
    page = _b_page(tmp_path, FLOW_SCRIPT, extra=f"window.TEST_RUN = {json.dumps(run_id)};\n")
    dom = _edge_page(tmp_path, page + "?ui=b&theme=light", size="1280,800")
    r = _result_of(dom)
    assert "timeout" not in r, r
    assert r["rail"][:3] == ["Search", "My searches", "Favourites"] and "OSM" in r["rail"], r
    assert r["preset"] == "Typical highline" and r["estimateFirst"].startswith("Click the map"), r
    f = r["following"]
    assert f["job"] and f["card"] == f["job"] and f["status"] == "Waiting" and not f["findLines"], r
    assert f["searchVisible"], r
    assert r["downloading"][0] == "Running" and "Elevation data · 1 of 4 MB" in r["downloading"][1], r
    assert "Finding lines · Step 3/5" in r["computing"][0] and r["computing"][1] > 40, r
    assert re.search(r"^\d+ lines in \d+ spots$", r["counts"]) and not r["toast"], r
    assert r["hash"] == "#run=" + urllib.parse.quote(run_id, safe=""), r
    assert r["sort"] == ["clr", True] and r["searchHidden"] and r["sameMap"], r
    assert r["ortho"] == [False, True, ["Test ortho"], False, ["OpenTopoMap"]], r
    assert r["title"] == r["expected"] and r["eyebrow"].startswith(f"Spot 1 of {r['spots']}"), r
    assert r["profile"].startswith("Profile: ") and r["lineHash"], r
    assert r["stepped"][0].startswith(f"Spot 2 of {r['spots']}") and r["stepped"][1] == 1, r
    before, after, summary = r["filtered"]
    assert after < before and summary.startswith(f"{after} spots") and r["cleared"] == before, r
    assert "?ui=b&theme=light#run=" in r["copied"] and "&line=" in r["copied"], r
    assert r["favs"] == 1 and r["favHash"] == "#favourites" and r["favOpened"], r
    assert r["back"] == "#favourites" and r["forward"] and r["pillBack"] == "#favourites", r
    assert r["newSearch"][0].startswith("Click the map") and r["newSearch"][1] == "Typical highline", r
    assert r["edited"][1] == "Typical highline" and r["edited"][2], r  # the job's values, a new start
    assert _violations(dom) == []


PHONE_SCRIPT = HELPERS + """
const model = () => window.FINDALINE_RESULT && window.FINDALINE_RESULT.model;
const wider = () => document.documentElement.scrollWidth > innerWidth;
play({
  linked(next) { wait(() => model() && model().selected && $(".u-sheet-head .r-eyebrow"), () => {
    result.width = innerWidth;
    result.rail = !!$(".u-rail");
    result.sheet = !!$(".u-sheet") && !$(".b-results");
    result.peek = text(".u-sheet-head .r-eyebrow");
    result.linked = model().lineLink(model().selected.line) === window.TEST_LINE;
    result.hint = text(".u-sheet .r-dock .r-hint");
    result.wider = [wider()];
    button("Next spot").click();
    later(() => { result.stepped = text(".u-sheet-head .r-eyebrow"); next(); });
  }); },
  menu(next) {
    button("Menu").click();
    wait(() => $(".b-menu"), () => {
      result.menu = $$(".b-menu button").map((b) => b.textContent.trim());
      button("Search", $(".b-menu")).click();
      wait(() => $(".u-sheet .s-panel") && !$(".b-menu"), () => {
        window.FINDALINE_PLACE.map.fire("click", {latlng: {lat: 50.1559508, lng: 19.7562722}});
        wait(() => text(".s-est-line").startsWith("Ready"), () => {
          const start = button("Find lines").getBoundingClientRect();
          result.start = start.bottom <= innerHeight && start.top >= 0;
          result.wider.push(wider());
          next();
        });
      });
    });
  },
  searches(next) {
    button("Menu").click();
    wait(() => $(".b-menu"), () => {
      button("My searches", $(".b-menu")).click();
      wait(() => $(".u-sheet .b-searches .b-card"), () => {
        result.cards = $$(".b-card").length;
        result.wider.push(wider());
        emit({seq: 1e6, job: "job-x", kind: "status", data: {status: "computing", title: "Elsewhere", created: ""}});
        later(() => {
          emit({seq: 1e6 + 1, job: "job-x", kind: "status", data: {status: "done", run_id: window.TEST_RUN}});
          wait(() => $(".b-toast"), () => {
            result.toast = text(".b-toast .u-toast-text");
            button("Show results", $(".b-toast")).click();
            wait(() => model() && $(".u-sheet-head") && !$(".b-toast"), () => { result.toastShown = location.hash; next(); });
          });
        });
      });
    });
  },
});
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_on_a_phone(tmp_path, monkeypatch):
    """Below 768 px: no rail but a menu, every panel a sheet; a line link (?ui=b#run=…&line=…) opened
    directly shows its spot in the sheet's peek with ‹ ›; the search's Find lines stays in view; no
    sideways scrolling anywhere."""
    run_id = _clip_run(tmp_path, monkeypatch)
    row = _lines_of(tmp_path, run_id)[0]
    line = ",".join(str(v) for v in row[:4])
    page = _b_page(tmp_path, PHONE_SCRIPT, extra=f"window.TEST_LINE = {json.dumps(line)}; "
                                                f"window.TEST_RUN = {json.dumps(run_id)};\n")
    link = f"#run={urllib.parse.quote(run_id, safe='')}&line={urllib.parse.quote(line, safe='')}"
    dom = _edge_page(tmp_path, page + "?ui=b" + link, size="500,900")
    r = _result_of(dom)
    assert "timeout" not in r, r
    assert r["width"] < 768 and not r["rail"] and r["sheet"] and r["linked"], r
    assert r["peek"].startswith("Spot ") and r["stepped"] != r["peek"], r
    assert r["hint"] in ("move the mouse along the chart", "tap or drag along the chart"), r
    assert r["menu"][:3] == ["Search", "My searches", "Favourites"], r
    assert r["start"] and r["cards"] >= 1 and r["wider"] == [False, False, False], r
    assert re.search(r"is ready: \d+ lines in \d+ spots$", r["toast"]), r
    assert r["toastShown"] == "#run=" + urllib.parse.quote(run_id, safe=""), r
    assert _violations(dom) == []


PUBLIC_SCRIPT = HELPERS + """
const RUN = window.TEST_RUN;
const model = () => window.FINDALINE_RESULT && window.FINDALINE_RESULT.model;
play({
  rail(next) { wait(() => window.FINDALINE_PLACE && $(".u-rail") && $(".s-panel"), () => {
    result.rail = $$(".u-rail-item").map((b) => b.textContent.trim());
    $(".b-account-button").click();
    wait(() => $(".b-account"), () => {
      result.account = [text(".b-account-user"), !!button("Sign out"),
                        $$(".b-account-links a").map((a) => a.getAttribute("href").split("?")[0])];
      $(".b-account-button").click();
      next();
    });
  }); },
  card(next) {
    if (window.TEST_ADMIN) return next();
    location.hash = "#searches";
    wait(() => $(`.b-card[data-run="${RUN}"]`), () => {
      const card = $(`.b-card[data-run="${RUN}"]`);
      result.cardActions = $$(`.b-card[data-run="${RUN}"] .b-card-acts button`).map((b) => b.textContent.trim());
      button("Delete", card).click();
      wait(() => card.querySelector(".b-confirm"), () => {
        result.confirm = text(".b-confirm span");
        button("Keep", card).click();
        later(() => { result.kept = !card.querySelector(".b-confirm"); next(); });
      });
    });
  },
  share(next) {
    if (window.TEST_ADMIN) return next();
    location.hash = "#run=" + encodeURIComponent(RUN);
    wait(() => rows().length && model() && button("Share", $(".b-run-actions")), () => {
      result.actions = $$(".b-run-actions .r-actions > * button").map((b) => b.textContent.trim()).filter(Boolean);
      button("Share", $(".b-run-actions")).click();
      wait(() => button("Make a link"), () => {
        button("Make a link").click();
        wait(() => $(".r-share .r-linkfield input"), () => {
          result.link = $(".r-share .r-linkfield input").value;
          next();
        });
      });
    });
  },
  admin(next) {
    const item = button("Admin");
    result.adminItem = !!item;
    if (!item) return next();
    item.click();
    wait(() => $$(".b-legacy h3").length, () => {
      result.sections = $$(".b-legacy h3").map((h) => h.textContent);
      next();
    });
  },
});
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_on_the_public_site(tmp_path, clip_run, account_settings, monkeypatch):
    """The public site, signed in through a fake GitHub: a user's rail has no Admin and no OSM, the account
    shows the user, privacy and about and signs out; the card of an own run shares and asks before
    deleting (in the page); the result's Share makes a link; an administrator gets the Admin item with the
    old admin panel. No CSP violations."""
    import socket
    import threading
    import time

    import uvicorn
    from fastapi import Request
    from fastapi.responses import RedirectResponse
    from starlette.staticfiles import StaticFiles

    import accounts as ac
    from jobs import Jobs
    from settings import PublicSettings
    from test_public import RUN_A, FakeProviders, copy_run, session_of

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    site = f"http://localhost:{port}"
    providers = FakeProviders(site)
    monkeypatch.setitem(ac.GITHUB, "authorize_url", f"{site}/test-provider/authorize")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "stubs.js").write_text(B_STUBS + f"window.TEST_RUN = {json.dumps(RUN_A)};\n", encoding="utf-8")
    (stubs / "check.js").write_text(PUBLIC_SCRIPT, encoding="utf-8")
    monkeypatch.setattr(web, "APP_HTML", web.APP_HTML.replace(
        '<script src="/app/app.js"></script>',
        '<script src="/test/stubs.js"></script><script src="/app/app.js"></script><script src="/test/check.js"></script>'))
    data = tmp_path / "data"
    data.mkdir()
    (data / "cache" / "osm").mkdir(parents=True)  # the admin panel's OSM part: no Geofabrik regions
    (data / "cache" / "osm" / "geofabrik-index.json").write_text(json.dumps({"features": []}))
    copy_run(clip_run, data, RUN_A, "A's run")
    user_id, _, _ = session_of(data, account_settings, "a")
    Jobs(str(data)).add_run(RUN_A, user_id, 1)
    settings = PublicSettings(public_url=site, trusted_proxy="127.0.0.1", contact="admin@example.org")
    app = web.create_app(str(data), public=settings, accounts=account_settings, oauth_transport=providers.transport)
    signing_in = [{"id": 11, "login": "a"}]  # who agrees at the fake provider

    @app.get("/test-provider/authorize")
    def authorize(request: Request):  # the provider's page: the user agrees at once
        return RedirectResponse(providers.authorize(str(request.url), signing_in[0]), 302)
    app.mount("/test", StaticFiles(directory=str(stubs)))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    def page(profile):
        import subprocess
        dom = subprocess.run(
            [EDGE, "--headless", "--disable-gpu", f"--user-data-dir={tmp_path / profile}", "--no-first-run",
             "--window-size=1280,800", "--virtual-time-budget=60000", "--dump-dom",
             f"{site}/login/github?next=" + urllib.parse.quote("/?ui=b", safe="")],
            capture_output=True, text=True, encoding="utf-8", timeout=180).stdout
        return dom, _result_of(dom)

    try:
        while not server.started:
            time.sleep(0.1)
        dom, r = page("edge-a")
        assert "timeout" not in r, r
        assert r["rail"] == ["Search", "My searches", "Favourites", "Account"], r
        assert r["account"][0].startswith("a") and r["account"][1], r
        assert r["account"][2][:2] == ["/privacy", "/about"], r
        assert "Show results" in r["cardActions"] and "Share" in r["cardActions"] and "Delete" in r["cardActions"], r
        assert r["confirm"].startswith("Delete “A's run”") and r["kept"], r
        assert r["actions"][:4] == ["Measure", "Compare", "Share", "Download"], r
        assert r["link"].startswith(f"{site}/s/"), r
        assert not r["adminItem"] and _violations(dom) == []
        signing_in[0] = {"id": 1, "login": "admin"}  # FINDALINE_ADMINS: the Admin item
        (stubs / "stubs.js").write_text(B_STUBS + "window.TEST_ADMIN = true;\n", encoding="utf-8")
        dom, r = page("edge-admin")
        assert r["rail"] == ["Search", "My searches", "Favourites", "Admin", "Account"], r
        assert r["adminItem"] and {"Users", "Runs"} <= set(r["sections"]), r
        assert _violations(dom) == []
    finally:
        server.should_exit = True
        thread.join(10)
