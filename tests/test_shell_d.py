"""Prototype D of the new interface (?ui=d, viewer/src/app/d) in headless Edge, offline: C's home and new search
(test_shell_c's fixtures and page helpers) with B's result (result/view/ResultWorkspace): the panel on the right,
the profile docked at the bottom of the page, "‹ Home", the line in the hash and in copied links, Back and
Forward, and phones (C's bottom navigation, B's sheet); no CSP violations anywhere."""

import json
import os
import urllib.parse

import pytest

from test_shell_c import BIG, SMALL, _edge, big_clip, data, stub_page  # noqa: F401 (fixtures)
from test_web import EDGE, _server

DESKTOP_SCRIPT = """
const BIG = "%(big)s", SMALL = "%(small)s", POINT = {lat: 50.1559508, lng: 19.7562722};
const model = () => window.FINDALINE_RESULT && window.FINDALINE_RESULT.model;
const near = (a, b) => Math.abs(a - b) <= 16;
let job = "";
run({
  home(next) { wait(() => card("run", SMALL) && card("run", BIG) && $(".c-account-btn"), () => {
    result.home = [$$(".c-card").map((c) => c.dataset.run || c.dataset.job), !!$(".ui.u-app-d.c-app"), !!$(".c-top")];
    $(".c-account-btn").click();
    later(() => { result.account = text(".c-account-links"); $(".c-account-btn").click(); next(); });
  }); },
  newSearch(next) {
    $(".c-new").click();
    wait(() => window.FINDALINE_PLACE && $(".s-panel") && $(".c-bar .s-start"), () => {
      result.newPage = [location.hash, text(".s-card.u-on b")];
      window.FINDALINE_PLACE.map.fire("click", {latlng: POINT});
      wait(() => text(".c-bar .s-bar-line").startsWith("Ready"), () => {
        $(".c-bar .s-start").click();
        wait(() => location.hash === "#home" && $(".c-card-new"), () => {
          job = $(".c-card-new").dataset.job;
          result.started = [job, $(".c-card-new").dataset.status];
          next();
        });
      });
    });
  },
  progress(next) {
    pushEvent({seq: 1000001, job, kind: "status", data: {status: "computing"}});
    wait(() => card("job", job).dataset.status === "running", () => {
      pushEvent({seq: 1000002, job, kind: "status", data: {status: "done", run_id: SMALL}});
      wait(() => card("job", job).dataset.status === "done" && card("job", job).dataset.run === SMALL, () => {
        result.done = $$(`.c-card[data-run="${SMALL}"]`).length;
        next();
      });
    });
  },
  open(next) {
    card("job", job).querySelector("a.c-card-open").click();
    wait(() => $(".b-results .r-rows .r-row") && $(".d-map.leaflet-container") && model(), () => {
      const panel = $(".b-results").getBoundingClientRect(), dock = $(".b-dock").getBoundingClientRect();
      result.layout = {hash: location.hash, header: !!$(".c-top"), panelRight: near(panel.right, innerWidth - 12),
                       panelTall: panel.height > innerHeight - 40, dockBottom: near(dock.bottom, innerHeight - 12),
                       dockLeftOfPanel: dock.right <= panel.left, home: !!button("Home"), edit: !!button("Edit & run again"),
                       actions: $$(".b-run-actions .r-actions button").map((b) => b.getAttribute("aria-label") || b.textContent.trim()),
                       name: text(".b-pill-name"), sort: text(".r-col.r-on")};
      result.sorted = model().rows.slice(0, 5).every((r, k, all) => k === 0 || all[k - 1].line.clr >= r.line.clr);
      // The map's controls follow the theme: in the dark one the zoom buttons are the panel's colour, also
      // focused (after a click) and disabled (at the last zoom), where Leaflet's own are light grey
      const root = $(".ui"), map = window.FINDALINE_MAP, colour = (q) => getComputedStyle($(q)).backgroundColor;
      root.dataset.theme = "dark";
      map.setZoom(map.getMaxZoom(), {animate: false});
      $(".leaflet-control-zoom-out").focus();
      later(() => {
        result.dark = {panel: colour(".b-results"), zoom: colour(".leaflet-control-zoom-in"), layers: colour(".leaflet-control-layers"),
                       disabled: $(".leaflet-control-zoom-in").classList.contains("leaflet-disabled"),
                       focused: colour(".leaflet-control-zoom-out")};
        delete root.dataset.theme;
        $(".leaflet-control-zoom-out").blur();
        next();
      });
    });
  },
  select(next) {
    $$(".b-results .r-rows > .r-spot > .r-row")[0].click();
    wait(() => $(".b-results .r-dtitle") && $(".b-dock .fal-profile-title"), () => {
      const m = model(), dock = $(".b-dock .r-dock").getBoundingClientRect();
      result.selected = [text(".b-results .r-eyebrow"), text(".b-dock .fal-profile-title"),
                         decodeURIComponent(location.hash) === `#run=${SMALL}&line=${m.lineLink(m.selected.line)}`,
                         near(dock.bottom, innerHeight - 12)];
      button("Next spot").click();
      later(() => {
        result.stepped = [text(".b-results .r-eyebrow"), m.position,
                          decodeURIComponent(location.hash) === `#run=${SMALL}&line=${m.lineLink(m.selected.line)}`];
        next();
      });
    });
  },
  filter(next) {
    const m = model(), before = m.shown.spots;
    button("Clearance").click();
    wait(() => $(".r-editor input[type=number]"), () => {
      const input = $(".r-editor input[type=number]");
      input.value = "1000";
      input.dispatchEvent(new Event("change", {bubbles: true}));
      later(() => {
        result.filtered = [before, m.shown.spots, $$(".u-chip.u-on").map((c) => c.textContent.trim())];
        button("Remove the clearance filter").click();
        later(() => {
          result.cleared = m.shown.spots;
          if (!m.selected) $$(".b-results .r-rows > .r-spot > .r-row")[1].click();
          next();
        });
      });
    });
  },
  copy(next) {
    wait(() => $(".b-results .r-copy"), () => {
      $(".b-results .r-copy").click();
      later(() => { result.copied = COPIED[0] || ""; result.line = model().lineLink(model().selected.line); next(); });
    });
  },
  goHome(next) {
    button("Home").click();
    wait(() => location.hash === "#home" && $(".c-cards") && !$(".d-map"), () => { result.home2 = location.hash; next(); });
  },
  back(next) {
    history.back();
    wait(() => location.hash.startsWith("#run=") && model() && model().selected && $(".b-results .r-dtitle"), () => {
      result.back = [decodeURIComponent(location.hash), model().lineLink(model().selected.line)];
      history.forward();
      wait(() => location.hash === "#home" && $(".c-cards"), () => { result.forward = location.hash; next(); });
    });
  },
  link(next) {
    location.href = COPIED[0];
    wait(() => $(".b-results .r-dtitle") && model() && model().selected, () => {
      result.linked = [model().lineLink(model().selected.line), !!$(".u-app-d")];
      next();
    });
  },
});
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_shell_d_desktop(data, tmp_path):
    """C's home with the runs as cards and its account menu (Old interface, Try B, Try C); New search returns home
    with the new card, which runs and turns done; its card opens B's result on a full-screen map: the panel on
    the right, the profile docked at the bottom left of it, "‹ Home", Edit & run again and the run's actions,
    sorted by clearance, the map's controls dark in the dark theme (focused and disabled too); a row selects its line (details under the row, the profile in the dock, the hash),
    ‹ › and a filter chip; Copy link gives D's address; Home returns; Back and Forward; the copied link opens
    the line; no CSP violations."""
    page = stub_page(data, "dtest", DESKTOP_SCRIPT % {"big": BIG, "small": SMALL})
    with _server(data) as port:
        r = _edge(port, page + "?ui=d", tmp_path / "edge")
    assert "timeout" not in r and "error" not in r, json.dumps(r)
    assert set(r["home"][0]) == {SMALL, BIG} and r["home"][1] and r["home"][2], r
    assert all(t in r["account"] for t in ("Old interface", "Try B", "Try C")) and "Try D" not in r["account"], r
    assert r["newPage"] == ["#new", "Typical highline"] and r["started"][1] == "waiting" and r["done"] == 1, r
    layout = r["layout"]
    assert layout["hash"] == f"#run={SMALL}" and not layout["header"] and layout["name"] == "Small run", r
    assert layout["panelRight"] and layout["panelTall"] and layout["dockBottom"] and layout["dockLeftOfPanel"], r
    assert layout["home"] and layout["edit"] and {"Compare", "Download"} <= set(layout["actions"])
    assert any(a.startswith("Measure") for a in layout["actions"]), r
    assert layout["sort"].startswith("Clear.") and r["sorted"], r
    dark = r["dark"]
    assert dark["disabled"] and dark["zoom"] == dark["layers"] == dark["panel"] != "rgb(255, 255, 255)", r
    assert dark["focused"] not in (dark["panel"], "rgb(244, 244, 244)"), r
    assert r["selected"][0].startswith("Spot 1 of ") and r["selected"][1].startswith("Profile"), r
    assert r["selected"][2] and r["selected"][3], r
    assert r["stepped"][0].startswith("Spot 2 of ") and r["stepped"][1] == 1 and r["stepped"][2], r
    before, after, chips = r["filtered"]
    assert after < before and any(c.startswith("Clearance ≥") for c in chips) and r["cleared"] == before, r
    assert "?ui=d#run=" in r["copied"], r
    assert r["copied"].endswith(f"#run={SMALL}&line=" + urllib.parse.quote(r["line"], safe="")), r
    assert r["home2"] == "#home", r
    assert r["back"] == [f"#run={SMALL}&line={r['line']}", r["line"]], r
    assert r["forward"] == "#home" and r["linked"] == [r["line"], True], r
    assert r["csp"] == []


PHONE_SCRIPT = """
const SMALL = "%(small)s";
run({
  home(next) { wait(() => card("run", SMALL) && $(".u-bnav"), () => {
    result.home = {width: innerWidth, nav: $$(".u-bnav-item").map((b) => b.textContent.trim()), overflow: overflow()};
    next();
  }); },
  result(next) {
    card("run", SMALL).querySelector("a.c-card-open").click();
    wait(() => $(".u-sheet .r-rows .r-row") && window.FINDALINE_RESULT, () => {
      result.sheet = {sheet: !!$(".d-layer .u-sheet"), panel: !!$(".b-results"), nav: !!$(".u-bnav"), top: !!$(".b-top-run .b-pill"),
                      peek: text(".u-sheet-head"), overflow: overflow()};
      $$(".u-sheet .r-rows > .r-spot > .r-row")[0].click();
      wait(() => $(".u-sheet-head .r-eyebrow"), () => { result.peek = text(".u-sheet-head .r-eyebrow"); next(); });
    });
  },
  heights(next) {
    // From the sheet's style (headless Edge does not run the transition): half the map, full under the top bar
    const sheet = () => ({snap: $(".u-sheet").className, height: parseFloat($(".u-sheet").style.height), inner: innerHeight,
                          bar: $(".b-top").getBoundingClientRect().bottom});
    result.half = sheet();
    $(".u-sheet-handle").click();
    later(() => { result.full = sheet(); $(".u-sheet-handle").click(); later(next); });
  },
  step(next) {
    button("Next spot").click();
    later(() => {
      result.stepped = text(".u-sheet-head .r-eyebrow");
      result.after = overflow();
      button("Home").click();
      wait(() => location.hash === "#home" && $(".u-bnav"), () => { result.home2 = location.hash; next(); });
    });
  },
});
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_shell_d_phone(data, tmp_path):
    """Below 768 px: C's home with the bottom navigation; the result is B's sheet over the map ("Spot k of N" with
    ‹ ›, no panel, no bottom navigation) under a top bar whose Home returns; a selected line leaves the sheet at
    half the map, and at full it stays below the top bar; nothing wider than the window; no CSP violations."""
    page = stub_page(data, "dphone", PHONE_SCRIPT % {"small": SMALL})
    with _server(data) as port:
        r = _edge(port, page + "?ui=d", tmp_path / "edge-phone", size="500,900")
    assert "timeout" not in r and "error" not in r, json.dumps(r)
    assert r["home"]["width"] < 768 and r["home"]["nav"] == ["Home", "Favourites", "Account"] and not r["home"]["overflow"], r
    sheet = r["sheet"]
    assert sheet["sheet"] and not sheet["panel"] and not sheet["nav"] and sheet["top"] and not sheet["overflow"], r
    assert r["peek"].startswith("Spot 1 of ") and r["stepped"].startswith("Spot 2 of ") and not r["after"], r
    half, full = r["half"], r["full"]
    assert "u-half" in half["snap"] and half["height"] <= half["inner"] / 2 + 1, r  # a selected line keeps it half
    assert "u-full" in full["snap"] and full["inner"] - full["height"] >= full["bar"], r
    assert r["home2"] == "#home", r
    assert r["csp"] == []

