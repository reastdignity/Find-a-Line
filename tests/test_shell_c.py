"""Prototype C of the new interface (?ui=c, viewer/src/app/c) in headless Edge, offline: home with the searches
as cards and their mini maps, the focused new search, the three-column result, favourites, the hash's pages
with Back and Forward, phones, and the public site signed in; no CSP violations anywhere."""

import json
import os
import shutil
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runs as rn  # noqa: E402
import web  # noqa: E402

pytest.importorskip("httpx2")
from fastapi.testclient import TestClient  # noqa: E402

from test_public import ACCOUNTS, FakeProviders, RUN_A, clip_run, copy_run, session_of  # noqa: E402,F401
from test_web import CSP_STUB, EDGE, LOCAL, TOKEN, _result_of, _server, _violations  # noqa: E402

BIG = "2026-10-27_100000_Big_00000000000000b1"     # the clip's run: about 27,000 lines, its mini map skips them
SMALL = "2026-10-28_120000_Small_00000000000000c1"  # its first lines only: the mini map draws them
SMALL_LINES = 300

# Before the app: an event stream the test feeds (pushEvent), a fixed estimate, a clipboard that records
C_STUBS = CSP_STUB + """
window.EVENT_SOURCES = [];
window.EventSource = class { constructor(url) { this.url = url; EVENT_SOURCES.push(this); } close() {} };
window.pushEvent = (event) => EVENT_SOURCES.forEach((s) => s.onmessage && s.onmessage({data: JSON.stringify(event)}));
window.ESTIMATES = [];
window.COPIED = [];
Object.defineProperty(navigator, "clipboard", {value: {writeText: (text) => { COPIED.push(text); return Promise.resolve(); }}});
const realFetch = window.fetch;
window.PLACES = [];
window.fetch = (url, init) => {
  if (String(url).startsWith("/api/place")) {  // the place box: two places, the second a point
    PLACES.push(JSON.parse(init.body).q);
    return Promise.resolve(new Response(JSON.stringify({places: [
      {name: "Kraków, Polska", lat: 50.06, lon: 19.94, bbox: [49.97, 19.79, 50.13, 20.22], type: "city"},
      {name: "Giewont, Polska", lat: 49.2508, lon: 19.9339, bbox: null, type: "peak"}]}),
      {headers: {"content-type": "application/json"}}));
  }
  if (!String(url).startsWith("/api/estimate")) return realFetch(url, init);
  ESTIMATES.push(JSON.parse(init.body));
  return new Promise((resolve) => setTimeout(() => resolve(new Response(JSON.stringify({source: "test", resolution: 1,
    km2: null, tiles: 1, download_mb: 3, work_gb: 0, reasons: [], messages: [], area: null}),
    {headers: {"content-type": "application/json"}})), 50));
};
"""

# Helpers of the scripts (before each): steps run one after another, the result goes into <pre id="result">
HELPERS = """
const result = {};
let finished = false;
const done = () => { if (finished) return; finished = true; const out = document.createElement("pre"); out.id = "result"; out.textContent = JSON.stringify(result);
                     document.body.appendChild(out); };
const $ = (s) => document.querySelector(s), $$ = (s) => [...document.querySelectorAll(s)];
const text = (s) => ($(s) || {}).textContent || "";
const button = (label) => $$("button, a").find((b) => b.textContent.trim() === label || b.getAttribute("aria-label") === label);
const later = (then, ms = 300) => setTimeout(then, ms);
let step = "";
window.addEventListener("error", (e) => { result.error = step + ": " + e.message + " " + (e.error && e.error.stack || "").slice(0, 600); done(); });
const wait = (ok, then, tries = 150) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 200)
                                                  : (result.timeout = step + " @ " + location.hash + " | " + $$("[data-thumb]").map((t) => t.dataset.thumb).join()
                                                     + " | " + text(".c-error"), done());
const card = (attr, id) => $(`.c-card[data-${attr}="${id}"]`);
const run = (steps) => {
  const names = Object.keys(steps);
  const go = (k) => { if (k >= names.length) return done(); step = names[k]; steps[step](() => go(k + 1)); };
  go(0);
};
const overflow = () => document.documentElement.scrollWidth > innerWidth
  || $$(".c-main, .c-main *").some((e) => e.getBoundingClientRect().right > innerWidth + 1 && getComputedStyle(e).position !== "fixed"
                                       && !e.closest(".leaflet-container, .u-sheet .r-table, .r-pop"));
"""


def small_copy(data_dir, run_id: str, lines: int = SMALL_LINES) -> str:
    """A copy of a run with its first `lines` lines only (SMALL); runs.js written again."""
    runs = os.path.join(str(data_dir), "runs")
    src, dst = os.path.join(runs, run_id), os.path.join(runs, SMALL)
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("data.js", "lines.js"))
    with open(os.path.join(src, "data.js"), encoding="utf-8") as f:
        data = json.loads(f.read().partition(" = ")[2].rstrip().rstrip(";"))
    data["lines"] = data["lines"][:lines]
    data["run"].update(id=SMALL, name="Small run", created="2026-10-28T12:00:00")
    with open(os.path.join(dst, "data.js"), "w", encoding="utf-8") as f:
        f.write("window.FINDALINE_RUN = " + json.dumps(data) + ";\n")
    with open(os.path.join(src, "lines.js"), encoding="utf-8") as f:
        body = json.loads(f.read().partition("] = ")[2].rstrip().rstrip(";"))
    body["lines"] = body["lines"][:lines]
    with open(os.path.join(dst, "lines.js"), "w", encoding="utf-8") as f:
        f.write(f'(window.FINDALINE_LINES = window.FINDALINE_LINES || {{}})["{SMALL}"] = {json.dumps(body)};\n')
    with open(os.path.join(dst, "run.json"), encoding="utf-8") as f:
        meta = json.load(f)
    meta.update(id=SMALL, name="Small run", created="2026-10-28T12:00:00")
    meta["summary"] = {**meta.get("summary", {}), "lines": lines, "spots": 12}
    with open(os.path.join(dst, "run.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    rn.write_index(runs)
    return SMALL


def stub_page(data_dir, folder: str, script: str = "", stubs: str = C_STUBS) -> str:
    """The app's page in runs/<folder>/index.html with the stubs before and the script after the app."""
    path = os.path.join(str(data_dir), "runs", folder)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "stubs.js"), "w", encoding="utf-8") as f:
        f.write(stubs)
    with open(os.path.join(path, "test.js"), "w", encoding="utf-8") as f:
        f.write(HELPERS + script if script else "")
    app = web.create_app(str(data_dir), token=TOKEN, start_scheduler=False)
    with TestClient(app, base_url=LOCAL) as client:
        page = client.get("/").text
    with open(os.path.join(path, "index.html"), "w", encoding="utf-8") as f:
        f.write(page.replace('<script src="/app/app.js"></script>',
                             '<script src="stubs.js"></script><script src="/app/app.js"></script>'
                             '<script src="test.js"></script>'))
    return f"runs/{folder}/index.html"


def _edge(port: int, page: str, profile, size: str = "1280,800") -> dict:
    import subprocess
    dom = subprocess.run(
        [EDGE, "--headless", "--disable-gpu", f"--user-data-dir={profile}", "--no-first-run", f"--window-size={size}",
         "--virtual-time-budget=90000", "--dump-dom", f"http://127.0.0.1:{port}/{page}"],
        capture_output=True, text=True, encoding="utf-8", timeout=240).stdout
    result = _result_of(dom)
    result["csp"] = _violations(dom)
    return result


@pytest.fixture(scope="module")
def big_clip(tmp_path_factory):
    """The folder of a run of the Żabi Koń clip with its surface model (the sag model: about 27,000 lines), computed once, offline."""
    import subprocess
    folder = tmp_path_factory.mktemp("c-clip")
    env = {**os.environ, "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
           "PYTHONIOENCODING": "utf-8"}
    data = os.path.join(REPO, "tests", "data")
    result = subprocess.run(
        [sys.executable, "findaline.py", "--elevation-file", os.path.join(data, "zabi_kon_nmt.tif"),
         "--surface-file", os.path.join(data, "zabi_kon_nmpt.tif"), "--northing", "254608", "--easting", "553966",
         "--radius", "150", "--model", "sag", "--water", "off", "--fill-gaps", "off", "--no-plot", "--yes",
         "--ortho", "never",
         "--data-dir", str(folder)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=600, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    runs = folder / "runs"
    return next(runs / d for d in os.listdir(runs) if (runs / d / "run.json").is_file())


def copy_clip(clip, data_dir, run_id: str, name: str):
    """copy_run, and its lines.js under the new id too."""
    folder = copy_run(clip, data_dir, run_id, name)
    lines = (folder / "lines.js").read_text("utf-8").replace(clip.name, run_id)
    (folder / "lines.js").write_text(lines, "utf-8")
    rn.write_index(str(data_dir / "runs"))
    return folder


@pytest.fixture
def data(tmp_path, big_clip):
    """A data folder with the clip's run (BIG, more lines than a mini map draws) and its small copy (SMALL)."""
    folder = tmp_path / "data"
    copy_clip(big_clip, folder, BIG, "Big run")
    small_copy(folder, BIG)
    return folder


DESKTOP_SCRIPT = """
const BIG = "%(big)s", SMALL = "%(small)s", POINT = {lat: 50.1559508, lng: 19.7562722};
let job = "";
run({
  home(next) { wait(() => card("run", SMALL) && card("run", BIG) && $(".c-trip, .c-trip-empty"), () => {
    result.cards = $$(".c-card").map((c) => c.dataset.run || c.dataset.job);
    result.noWorker = !!$(".c-no-worker");
    result.example = ($(".c-example") || {dataset: {}}).dataset.run;
    next();
  }); },
  thumbs(next) { wait(() => card("run", SMALL).querySelector("[data-thumb=lines]")
                       && card("run", BIG).querySelector("[data-thumb=outline]"), () => {
    const small = card("run", SMALL).querySelector(".c-thumb"), big = card("run", BIG).querySelector(".c-thumb");
    result.smallThumb = [small.dataset.thumb, Number(small.dataset.lines), small.querySelectorAll("path.c-thumb-line").length];
    result.bigThumb = [big.dataset.thumb, Number(big.dataset.lines), text(`.c-card[data-run="${BIG}"] .c-thumb-label`)];
    result.bigLinesLoaded = !!(window.FINDALINE_LINES || {})[BIG];
    next();
  }); },
  newSearch(next) {
    $(".c-new").click();
    wait(() => window.FINDALINE_PLACE && $(".s-panel") && $(".c-bar .s-start"), () => {
      result.newPage = [location.hash, text(".s-card.u-on b"), $(".c-bar .s-start").disabled];
      window.FINDALINE_PLACE.map.fire("click", {latlng: POINT});
      wait(() => text(".c-bar .s-bar-line").startsWith("Ready"), () => {
        result.ready = [text(".c-bar .s-bar-line"), $(".c-bar .s-start").disabled, ESTIMATES.length];
        $(".c-bar .s-start").click();
        wait(() => location.hash === "#home" && $(".c-card-new"), () => {
          job = $(".c-card-new").dataset.job;
          result.started = [job, $(".c-card-new").dataset.status, history.length];
          next();
        });
      });
    });
  },
  progress(next) {
    pushEvent({seq: 1000001, job, kind: "status", data: {status: "computing"}});
    pushEvent({seq: 1000002, job, kind: "progress", data: {bar: "Compute", stage: "", count: 5, total: 10, unit: "",
      extra: {}, done: false, step: "Step 3/5: evaluating anchor pairs"}});
    wait(() => card("job", job).dataset.status === "running", () => {
      result.running = [text(`.c-card[data-job="${job}"] .u-st`), !!card("job", job).querySelector(".u-prog")];
      pushEvent({seq: 1000003, job, kind: "status", data: {status: "done", run_id: SMALL}});
      wait(() => card("job", job).dataset.status === "done" && card("job", job).dataset.run === SMALL, () => {
        result.done = [text(`.c-card[data-job="${job}"] .c-card-meta`), $$(`.c-card[data-run="${SMALL}"]`).length];
        next();
      });
    });
  },
  open(next) {
    card("job", job).querySelector("a.c-card-open").click();
    wait(() => $(".c-col-list .r-rows .r-row") && $(".c-col-map .leaflet-container") && window.FINDALINE_RESULT, () => {
      result.columns = [!!$(".c-col-list .r-summary"), !!$(".c-col-list .r-filters"), text(".c-col-details .c-pick"),
                        location.hash, text(".r-col.r-on"), !!button("Edit & run again"), text(".c-run-name")];
      const model = window.FINDALINE_RESULT.model;
      result.sorted = model.rows.slice(0, 5).every((r, k, all) => k === 0 || all[k - 1].line.clr >= r.line.clr);
      next();
    });
  },
  select(next) {
    $$(".c-col-list .r-rows > .r-spot > .r-row")[0].click();
    wait(() => $(".c-col-details .r-dtitle") && $(".c-col-details .fal-profile-title"), () => {
      const model = window.FINDALINE_RESULT.model;
      result.selected = [text(".c-col-details .r-eyebrow"), text(".c-col-details .fal-profile-title"),
                         decodeURIComponent(location.hash) === `#run=${SMALL}&line=${model.lineLink(model.selected.line)}`];
      button("Next spot").click();
      later(() => {
        result.stepped = [text(".c-col-details .r-eyebrow"), model.position];
        result.secondLine = model.lineLink(model.selected.line);
        next();
      });
    });
  },
  filter(next) {
    const model = window.FINDALINE_RESULT.model, before = model.shown.spots;
    button("Clearance").click();
    wait(() => $(".r-editor input[type=number]"), () => {
      const input = $(".r-editor input[type=number]");
      input.value = "1000";
      input.dispatchEvent(new Event("change", {bubbles: true}));
      later(() => {
        result.filtered = [before, model.shown.spots, $$(".u-chip.u-on").map((c) => c.textContent.trim())];
        button("Remove the clearance filter").click();
        later(() => {
          result.cleared = model.shown.spots;
          if (!model.selected) { $$(".c-col-list .r-rows > .r-spot > .r-row")[1].click(); }
          next();
        });
      });
    });
  },
  favourite(next) {
    const model = window.FINDALINE_RESULT.model;
    wait(() => $(".c-col-details .r-fav"), () => {
    $(".c-col-details .r-fav").click();  // its folders: the top level
    later(() => $(".r-pop .r-folder").click());
    const saved = () => fetch("/api/favourites").then((r) => r.json());
    const until = (tries) => saved().then((body) => body.favourites.length || !tries ? body : new Promise((r) =>
      later(() => r(until(tries - 1)))));
    until(30).then((body) => {
      result.favourite = [body.favourites.length, body.favourites[0] && body.favourites[0].line === model.lineLink(model.selected.line)];
      result.favLine = model.lineLink(model.selected.line);
      next();
    });
    });
  },
  copy(next) {
    $(".c-col-details .r-copy").click();
    later(() => { result.copied = COPIED[0] || ""; next(); });
  },
  goHome(next) {
    button("Home").click();
    wait(() => location.hash === "#home" && $(".c-fav"), () => {
      result.aside = [$$(".c-fav").length, $(".c-fav-open").getAttribute("href")];
      next();
    });
  },
  back(next) {
    history.back();
    wait(() => location.hash.startsWith("#run=") && $(".c-col-details .r-dtitle"), () => {
      const model = window.FINDALINE_RESULT.model;
      result.back = [decodeURIComponent(location.hash), model.lineLink(model.selected.line)];
      history.forward();
      wait(() => location.hash === "#home" && $(".c-cards"), () => {
        result.forward = location.hash;
        next();
      });
    });
  },
  link(next) {
    location.href = COPIED[0];
    wait(() => $(".c-col-details .r-dtitle") && window.FINDALINE_RESULT, () => {
      const model = window.FINDALINE_RESULT.model;
      result.linked = model.lineLink(model.selected.line);
      next();
    });
  },
  edit(next) {
    window.FINDALINE_SEARCH = null;  // the first new search's
    button("Edit & run again").click();
    wait(() => location.hash.startsWith("#new&edit=") && $(".c-subtitle") && window.FINDALINE_SEARCH && window.FINDALINE_SEARCH.point, () => {
      result.edit = [location.hash, window.FINDALINE_SEARCH.point, $(".c-subtitle").textContent];
      next();
    });
  },
});
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_shell_c_desktop(data, tmp_path):
    """Home: the runs as cards, the small run's mini map with its lines, the big one's box only (its lines.js not
    loaded); New search starts with "Typical highline", Start is enabled after the estimate and returns home
    with the new card highlighted, which runs (progress) and turns done with its run; the card opens the
    result in three columns sorted by clearance, a row selects its line (details, profile, the hash),
    ‹ › and a filter chip; a favourite shows in home's aside; Back and Forward; the copied link opens the
    line; Edit & run again opens the form with the job's request; no CSP violations."""
    import urllib.parse
    page = stub_page(data, "ctest", DESKTOP_SCRIPT % {"big": BIG, "small": SMALL})
    with _server(data) as port:
        r = _edge(port, page + "?ui=c", tmp_path / "edge")
    assert "timeout" not in r and "error" not in r, json.dumps(r)
    assert set(r["cards"]) == {SMALL, BIG} and r["noWorker"] and r["example"] in (SMALL, BIG), r
    assert r["smallThumb"] == ["lines", SMALL_LINES, r["smallThumb"][2]] and r["smallThumb"][2] >= 1, r
    assert r["bigThumb"][:2] == ["outline", 0] and int(r["bigThumb"][2].split()[0].replace(",", "")) > 3000, r
    assert not r["bigLinesLoaded"], r
    assert r["newPage"] == ["#new", "Typical highline", True], r
    assert r["ready"][0] == "Ready to search" and r["ready"][1] is False and r["ready"][2] >= 1, r
    assert r["started"][0] and r["started"][1] == "waiting", r
    assert r["running"][0].upper().startswith("RUNNING · ") and r["running"][1], r
    assert r["done"][1] == 1 and "spots" in r["done"][0], r
    columns = r["columns"]
    assert columns[0] and columns[1] and columns[2].startswith("Select a line"), r
    assert columns[3] == f"#run={SMALL}" and columns[4].startswith("Clear.") and columns[5], r
    assert columns[6] == "Small run" and r["sorted"], r
    assert r["selected"][0].startswith("Spot 1 of ") and r["selected"][1].startswith("Profile") and r["selected"][2], r
    assert r["stepped"][0].startswith("Spot 2 of ") and r["stepped"][1] == 1, r
    before, after, chips = r["filtered"]
    assert after < before and any(c.startswith("Clearance ≥") for c in chips) and r["cleared"] == before, r
    assert r["favourite"] == [1, True], r
    assert r["copied"].endswith(f"#run={SMALL}&line=" + urllib.parse.quote(r["favLine"], safe="")), r
    assert r["aside"][0] == 1 and r["aside"][1].startswith(f"#run={SMALL}&line="), r
    assert r["back"][0] == f"#run={SMALL}&line={r['favLine']}" and r["back"][1] == r["favLine"], r
    assert r["forward"] == "#home" and r["linked"] == r["favLine"], r
    assert r["edit"][0].startswith("#new&edit=") and r["edit"][2] == "Edit & run again", r
    assert [round(v, 5) for v in r["edit"][1]] == [50.15595, 19.75627], r
    assert r["csp"] == []


PHONE_SCRIPT = """
const SMALL = "%(small)s";
run({
  home(next) { wait(() => card("run", SMALL) && $(".u-bnav") && $(".c-card [data-thumb=lines]"), () => {
    const cards = $$(".c-card").map((c) => c.getBoundingClientRect());
    result.home = {width: innerWidth, nav: $$(".u-bnav-item").map((b) => b.textContent.trim()),
                   newWidth: Math.round($(".c-new").getBoundingClientRect().width),
                   main: Math.round($(".c-home-main").getBoundingClientRect().width),
                   stacked: cards.every((c, k) => k === 0 || c.top >= cards[k - 1].bottom - 1),
                   thumb: Math.round($(".c-card-thumb").getBoundingClientRect().width), overflow: overflow()};
    next();
  }); },
  favourites(next) {
    $$(".u-bnav-item")[1].click();
    wait(() => location.hash === "#favourites" && $(".c-favs-page"), () => {
      result.favourites = [$(".u-bnav-item.u-on").textContent.trim(), overflow()];
      $$(".u-bnav-item")[2].click();
      wait(() => location.hash === "#account" && $(".c-account"), () => {
        result.account = [text(".c-account-links"), overflow()];
        next();
      });
    });
  },
  newSearch(next) {
    location.hash = "#home";
    wait(() => $(".c-new"), () => {
      $(".c-new").click();
      wait(() => $(".c-bar .s-start") && $(".s-panel"), () => {
        const bar = $(".c-bar").getBoundingClientRect(), form = $(".c-form");
        result.newSearch = {bar: Math.round(bar.bottom) === innerHeight, scrolls: form.scrollHeight > form.clientHeight,
                            nav: !!$(".u-bnav"), overflow: overflow()};
        next();
      });
    });
  },
  result(next) {
    location.hash = "#run=" + SMALL;
    wait(() => $(".u-sheet .r-rows .r-row") && window.FINDALINE_RESULT, () => {
      result.sheet = [!!$(".u-sheet"), !!$(".c-col-list"), !!$(".u-bnav"), overflow()];
      $$(".u-sheet .r-rows > .r-spot > .r-row")[0].click();
      wait(() => $(".u-sheet-head .r-eyebrow"), () => {
        result.peek = text(".u-sheet-head .r-eyebrow");
        button("Next spot").click();
        later(() => {
          result.stepped = text(".u-sheet-head .r-eyebrow");
          result.after = overflow();
          next();
        });
      });
    });
  },
});
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_shell_c_phone(data, tmp_path):
    """Below 768 px: home's cards stack with small mini maps, "New search" is full width, the bottom navigation
    leads to Favourites and the account; the new search's bar stays at the bottom under the scrolling form;
    the result is a map with a sheet ("Spot k of N", ‹ ›); nothing wider than the window; no CSP violations."""
    page = stub_page(data, "cphone", PHONE_SCRIPT % {"small": SMALL})
    with _server(data) as port:
        r = _edge(port, page + "?ui=c", tmp_path / "edge-phone", size="500,900")
    assert "timeout" not in r and "error" not in r, json.dumps(r)
    home = r["home"]
    assert home["width"] < 768 and home["nav"] == ["Home", "Favourites", "Account"], r
    assert home["newWidth"] == home["main"] and home["stacked"] and home["thumb"] < 140 and not home["overflow"], r
    assert r["favourites"] == ["Favourites", False], r
    assert "Old interface" not in r["account"][0] and "Try interface B" in r["account"][0] and "Try interface D" in r["account"][0] and not r["account"][1], r
    assert r["newSearch"] == {"bar": True, "scrolls": True, "nav": False, "overflow": False}, r
    assert r["sheet"] == [True, False, False, False], r
    assert r["peek"].startswith("Spot 1 of ") and r["stepped"].startswith("Spot 2 of ") and not r["after"], r
    assert r["csp"] == []


PUBLIC_SCRIPT = """
const RUN = "%(run)s", ADMIN = %(admin)s;
const typeIn = (el, value) => { el.value = value; el.dispatchEvent(new Event("input", {bubbles: true})); };
/** Send a report from the dialog that is open: its kind, its text; then the dialog closed. */
const report = (kind, words, then) => wait(() => $("dialog.n-report[open] .u-seg") && !$("dialog.n-report textarea").value
                                           && text("dialog.n-report .n-attached").includes("the page"), () => {
  const sent = {attached: text("dialog.n-report .n-attached")};
  $$("dialog.n-report .u-seg button").find((b) => b.textContent === kind).click();
  later(() => {
    typeIn($("dialog.n-report textarea"), words);
    later(() => {
      $("dialog.n-report button[type=submit]").click();
      wait(() => text("dialog.n-report h2") === "Thank you", () => {
        button("Close").click();
        later(() => { sent.closed = !$("dialog.n-report[open]"); then(sent); });
      });
    });
  });
});
run({
  notice(next) {
    // The beta notice after the sign-in, once: the cookie of the sign-in is gone after it
    wait(() => $("dialog.n-beta[open]"), () => {
      result.notice = {title: text("dialog.n-beta h2"), main: text("dialog.n-beta .n-warn").includes("break of the main"),
                       links: $$("dialog.n-beta .n-ways a").map((a) => a.getAttribute("href"))};
      button("Understood").click();
      later(() => {
        result.notice.closed = !$("dialog.n-beta[open]");
        result.notice.cookie = document.cookie.includes("findaline-welcome");
        next();
      });
    });
  },
  home(next) { wait(() => (ADMIN || card("run", RUN)) && $(".c-account-btn") && $(".c-cards, .c-empty"), () => {
    result.cards = $$(".c-card").map((c) => c.dataset.run);
    $(".c-account-btn").click();
    later(() => {
      result.account = text(".c-account-links");
      result.name = text(".c-account-who b");
      $(".c-account-btn").click();
      next();
    });
  }); },
  admin(next) {
    if (!ADMIN) return next();
    location.hash = "#admin";
    wait(() => $$(".c-admin .u-panel-head h2").length > 3, () => {
      result.admin = $$(".c-admin .u-panel-head h2").map((h) => h.textContent);
      result.reports = $$(".c-report").map((r) => [r.querySelector(".u-badge").textContent,
                                                    r.querySelector(".c-report-text").textContent,
                                                    !!r.querySelector(".c-report-context a"),
                                                    !!r.querySelector(".c-report-agent")]);
      const done = (fn) => wait(() => !$(".r-spin"), fn);
      if (result.reports.length) {
        $(".c-report .c-admin-tools button").click();  // Done
        wait(() => $(".c-report-done"), () => {
          $$(".c-report")[0].querySelectorAll(".c-admin-tools button")[1].click();  // Delete…
          later(() => {
            $(".c-report-delete").click();
            wait(() => $$(".c-report").length === result.reports.length - 1, () => {
              result.reportsAfter = $$(".c-report").length;
              location.hash = "#osm";
              wait(() => $$(".c-osm > p, .c-osm .c-admin-row").length > 1, () => { location.hash = "#home"; next(); });
            });
          });
        });
        return;
      }
      location.hash = "#osm";  // the extracts' own page, beside its map
      wait(() => $$(".c-osm > p, .c-osm .c-admin-row").length > 1, () => {  // the intro and the regions
        result.osm = $$(".c-osm h2").map((h) => h.textContent);
        location.hash = "#home";
        wait(() => $(".c-cards, .c-empty"), next);
      });
    });
  },
  reports(next) {
    if (ADMIN) return next();
    // Report a problem: from the account menu, then from a line of the run (its run and line sent with it)
    later(() => $(".c-account-btn").click());  // (after the menu closed by the step before was drawn)
    wait(() => button("Report a problem"), () => {
      button("Report a problem").click();
      report("Interface", "The map does not move.", (fromMenu) => {
        result.fromMenu = fromMenu;
        location.hash = "#run=" + RUN;
        wait(() => $(".r-rows .r-row"), () => {
          $(".r-rows .r-row").click();
          wait(() => button("Report this line"), () => {
            button("Report this line").click();
            report("Water level", "The river is 2 m too high here.", (fromLine) => {
              result.fromLine = fromLine;
              location.hash = "#home";
              wait(() => card("run", RUN), next);
            });
          });
        });
      });
    });
  },
  share(next) {
    if (ADMIN) return next();
    card("run", RUN).querySelector(".c-card-menu").click();
    later(() => {
      result.menu = $$(`.c-card[data-run="${RUN}"] .c-menu-item`).map((m) => m.textContent.trim());
      button("Share…").click();
      later(() => {
        button("Make a link").click();
        wait(() => $(".c-card-panel .r-linkfield input"), () => {
          result.link = $(".c-card-panel .r-linkfield input").value;
          result.shared = card("run", RUN).dataset.status;
          next();
        });
      });
    });
  },
  remove(next) {
    if (ADMIN) return next();
    card("run", RUN).querySelector(".c-card-menu").click();
    later(() => {
      button("Delete…").click();
      wait(() => $(".c-confirm"), () => {
        result.confirm = text(".c-confirm span");
        $(".c-confirm-delete").click();
        wait(() => !card("run", RUN), () => { result.deleted = true; next(); });
      });
    });
  },
});
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_shell_c_public(tmp_path, clip_run, monkeypatch):
    """The public site, signed in through a fake GitHub: a user's own run as a card whose menu shares it (a link
    shown once) and deletes it after an in-page confirmation; the account menu has the user, privacy and about,
    no Admin; an administrator's has Admin, whose page embeds the old admin panel; no CSP violations."""
    import re
    import socket
    import threading

    import uvicorn
    from fastapi import Request
    from fastapi.responses import RedirectResponse
    from starlette.staticfiles import StaticFiles

    import accounts as ac
    from jobs import Jobs
    from settings import AccountSettings, PublicSettings
    for key, value in ACCOUNTS.items():
        monkeypatch.setenv(key, value)
    account_settings = AccountSettings.load()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    site = f"http://localhost:{port}"
    providers = FakeProviders(site)
    monkeypatch.setitem(ac.GITHUB, "authorize_url", f"{site}/test-provider/authorize")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "stubs.js").write_text(C_STUBS, encoding="utf-8")
    script = tmp_path / "script"  # the check depends on who signs in: written before each sign-in
    script.mkdir()
    monkeypatch.setattr(web, "APP_HTML", web.APP_HTML.replace(
        '<script src="/app/app.js"></script>',
        '<script src="/test/stubs.js"></script><script src="/app/app.js"></script><script src="/check/check.js"></script>'))
    data = tmp_path / "data"
    data.mkdir()
    (data / "cache" / "osm").mkdir(parents=True)  # the admin panel's OSM part: no Geofabrik regions
    (data / "cache" / "osm" / "geofabrik-index.json").write_text(json.dumps({"features": []}))
    copy_clip(clip_run, data, RUN_A, "A's run")
    user_id, _, _ = session_of(data, account_settings, "a")
    Jobs(str(data)).add_run(RUN_A, user_id, 1)

    settings = PublicSettings(public_url=site, trusted_proxy="127.0.0.1", contact="admin@example.org")
    app = web.create_app(str(data), public=settings, accounts=account_settings, oauth_transport=providers.transport)
    signing_in = [{"id": 11, "login": "a"}]

    @app.get("/test-provider/authorize")
    def authorize(request: Request):  # the provider's page: the user agrees at once
        return RedirectResponse(providers.authorize(str(request.url), signing_in[0]), 302)
    app.mount("/test", StaticFiles(directory=str(stubs)))
    app.mount("/check", StaticFiles(directory=str(script)))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    def signed_in(who: dict, admin: bool, profile: str) -> dict:
        import subprocess
        signing_in[0] = who
        (script / "check.js").write_text(HELPERS + PUBLIC_SCRIPT % {"run": RUN_A, "admin": json.dumps(admin)},
                                         encoding="utf-8")
        dom = subprocess.run(
            [EDGE, "--headless", "--disable-gpu", f"--user-data-dir={tmp_path / profile}", "--no-first-run",
             "--window-size=1280,800", "--virtual-time-budget=60000", "--dump-dom",
             f"{site}/login/github?next=" + "/%3Fui%3Dc"],
            capture_output=True, text=True, encoding="utf-8", timeout=240).stdout
        found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
        assert found, dom[-3000:]
        r = _result_of(dom)
        r["csp"] = _violations(dom)
        return r

    try:
        while not server.started:
            time.sleep(0.1)
        r = signed_in({"id": 11, "login": "a"}, False, "edge-a")
        assert "timeout" not in r and "error" not in r, json.dumps(r)
        assert r["notice"] == {"title": "An early beta", "main": True, "closed": True, "cookie": False,
                               "links": ["https://github.com/reastdignity/Find-a-Line/issues/new/choose",
                                         "mailto:admin@example.org"]}, r
        assert r["cards"] == [RUN_A] and r["name"] == "a", r
        assert "Admin" not in r["account"] and "Privacy" in r["account"] and "About" in r["account"], r
        assert "Report a problem" in r["account"], r
        assert r["fromMenu"]["closed"] and "the window's size" in r["fromMenu"]["attached"], r
        assert "the run and the line" in r["fromLine"]["attached"] and r["fromLine"]["closed"], r
        sent = Jobs(str(data)).reports()
        assert [(s["category"], s["context"].get("line") is not None, bool(s["context"]["user_agent"])) for s in sent] ==             [("water", True, True), ("interface", False, True)], sent
        assert r["menu"] == ["Open the result", "Share…", "Report a problem…", "Delete…"], r
        assert r["link"].startswith(f"{site}/s/") and r["shared"] == "shared", r
        assert r["confirm"].startswith("Delete “A's run”") and r["deleted"], r
        assert not (data / "runs" / RUN_A).exists()
        assert r["csp"] == []
        r = signed_in({"id": 1, "login": "admin"}, True, "edge-admin")
        assert "timeout" not in r and "error" not in r, json.dumps(r)
        assert "Admin" in r["account"] and r["name"] == "admin", r
        assert {"Server", "Users", "All searches", "Runs", "OpenStreetMap extracts"} <= set(r["admin"]), r
        assert "Problem reports (2 open)" in r["admin"], r
        assert r["reports"] == [["water level", "The river is 2 m too high here.", False, True],
                                ["interface", "The map does not move.", False, True]], r  # the run is deleted
        assert r["reportsAfter"] == 1, r
        assert r["csp"] == []
    finally:
        server.should_exit = True
        thread.join(10)
