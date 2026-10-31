"""The viewer (viewer/src, TypeScript) against Python: the sag estimator and the
grouping must give the same results, and viewer/dist must be built from the sources.

Needs Node and `npm install` in viewer/; skipped without them.
"""

import json
import os
import random
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWER = os.path.join(REPO, "viewer")
sys.path.insert(0, REPO)
import mapping  # noqa: E402
import sag  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not os.path.isdir(os.path.join(VIEWER, "node_modules", "esbuild")),
    reason="needs Node and `npm install` in viewer/")


def run_ts(tmp_path, module: str, script: str):
    """Bundle viewer/src/<module>.ts for Node and run script with it as `m`; returns its JSON output."""
    bundle = tmp_path / f"{module}.cjs"
    subprocess.run(["node", "-e", f"require('esbuild').buildSync({{entryPoints: ['src/{module}.ts'], bundle: true, "
                                  f"format: 'cjs', platform: 'node', outfile: {json.dumps(str(bundle))}}})"],
                   cwd=VIEWER, check=True)
    path = tmp_path / f"{module}-test.cjs"
    path.write_text(f"const m = require({json.dumps(str(bundle))});\n{script}", encoding="utf-8")
    result = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


CASES = [(30, 0), (100, 0), (300, -40), (1000, 0), (100, 100)]


def test_sag_matches_python(tmp_path):
    rig = sag.Rigging()
    rigging = {"pretension": rig.pretension, "stretch": rig.stretch, "webbing_weight": rig.webbing_weight,
               "athlete_mass": rig.athlete_mass, "fall_zone": rig.fall_zone}
    found = run_ts(tmp_path, "sag", f"""
const out = {json.dumps(CASES)}.map(([span, dh]) => {{
  const e = m.estimate(span, dh, {json.dumps(rigging)});
  return {{L0: e.L0, sag: e.sag, unloaded: e.unloaded, athlete: e.athlete(span * 0.3).sag}};
}});
console.log(JSON.stringify(out));""")
    for (span, dh), ts in zip(CASES, found):
        L0, _ = sag.unstretched(span, dh, rig)
        _, envelope = sag.envelope(span, dh, rig)
        athlete = sag.loaded(span, dh, rig, span * 0.3, L0)["sag"]
        assert ts["L0"] == pytest.approx(L0, abs=1e-6)
        assert ts["sag"] == pytest.approx(envelope.tolist(), abs=1e-6)
        unloaded = [v for p in sag.unloaded_curve(span, dh, rig) for v in p]
        assert [v for p in ts["unloaded"] for v in p] == pytest.approx(unloaded, abs=1e-6)
        assert ts["athlete"] == pytest.approx(athlete, abs=1e-6)


def test_grouping_matches_python(tmp_path):
    random.seed(1)
    results = []
    for _ in range(2000):  # clusters of similar lines, some over trees
        x, y = random.choice([(0, 0), (30, 5), (500, 500), (-40, 70)])
        x1, y1 = x + random.uniform(-25, 25), y + random.uniform(-25, 25)
        results.append({"a1_xy": (x1, y1), "a2_xy": (x1 + 100 + random.uniform(-25, 25), y1 + random.uniform(-25, 25)),
                        "length": round(random.uniform(80, 130), 1), "clearance": round(random.uniform(0, 20), 2),
                        "over_obstacles": random.random() < 0.3,
                        "height_max": random.choice([10.0, 20.0, round(random.uniform(5, 60), 2)])})
    lines = [{"x1": r["a1_xy"][0], "y1": r["a1_xy"][1], "x2": r["a2_xy"][0], "y2": r["a2_xy"][1],
              "len": r["length"], "clr": r["clearance"], "over": r["over_obstacles"], "hmax": r["height_max"], "k": k}
             for k, r in enumerate(results)]
    for distance, by in ((20, "clearance"), (7, "longest"), (15, "height")):
        groups = mapping.group_lines(results, distance, by)
        python = [[results.index(g["rep"])] + [results.index(m) for m in g["members"]] for g in groups]
        ts = run_ts(tmp_path, "grouping", f"""
const lines = {json.dumps(lines)};
console.log(JSON.stringify(m.groupLines(lines, {distance}, {json.dumps(by)})
  .map((g) => [g.rep.k].concat(g.members.map((x) => x.k)))));""")
        assert ts == python


def test_list_sorting(tmp_path):
    """The list of lines: both directions, unknown values (null, NaN) last in both, ties in their order."""
    found = run_ts(tmp_path, "list", """
const rows = [[5, 1], [null, 2], [7, 3], [NaN, 4], [5, 5], [1, 6]]
  .map(([clrN, k]) => ({line: {clrN, k}, n: 1}));
const key = m.sortKey("clrN");
console.log(JSON.stringify([true, false].map((desc) => m.sortRows(rows, key, desc).map((r) => r.line.k))));""")
    assert found == [[3, 1, 5, 6, 2, 4], [6, 1, 5, 3, 2, 4]]


def test_favourites_export(tmp_path):
    """The export of favourite lines: lines.csv's column names, text quoted and kept from being read as
    a formula, unknown values empty (CSV) or null (GeoJSON), the anchors as a LineString."""
    found = run_ts(tmp_path, "favourites", """
const f = {run_id: "r1", run_name: "=cmd \\"x\\", y", run_created: "", created: "", line: "50.1,19.7,50.2,19.8",
           data: {len: 120.5, clr: 1.2, clrN: null, e1: 330, e2: 328.5, over: true, water: 0, filled: 0, hmax: 15,
                  hmean: null, highPct: 0, angle1: 12, angle2: null, prom1: 3, prom2: null, drop1: 30, drop2: null}};
const line = m.favouriteLine(f), lines = [{line, runId: f.run_id, runName: f.run_name}];
console.log(JSON.stringify({csv: m.favouritesCsv(lines), geojson: JSON.parse(m.favouritesGeoJSON(lines)),
                            data: m.favouriteData(line)}));""")
    head, row = found["csv"].split("\r\n")[:2]
    assert head.split(",")[:5] == ["run_id", "run_name", "folder", "length_m", "clearance_m"]
    assert row.startswith('"r1","\'=cmd ""x"", y","",120.5,1.2,,true,0,0,15,,0,50.1,19.7,330,12,3,30,50.2,19.8,328.5,,,')
    feature = found["geojson"]["features"][0]
    assert feature["geometry"] == {"type": "LineString", "coordinates": [[19.7, 50.1], [19.8, 50.2]]}
    assert feature["properties"]["height_mean_m"] is None and feature["properties"]["over_trees_buildings"] is True
    assert found["data"]["hmean"] is None and found["data"]["len"] == 120.5  # back as the server keeps it


def test_run_being_saved(tmp_path):
    """The new shells hide a listed run that its unfinished job is still saving (run.json is written before
    the map, and the job gets its run_id only when done): no map yet and made after the job was queued."""
    found = run_ts(tmp_path, "app/jobStatus", """
const job = (status, created, run_id = null) => ({status, created, run_id});
const run = (has_map, created) => ({has_map, created});
const running = [job("computing", "2026-10-29T07:50:00")];
console.log(JSON.stringify([
  m.beingSaved(run(false, "2026-10-29T07:58:00"), running),                     // being saved: hidden
  m.beingSaved(run(true, "2026-10-29T07:58:00"), running),                      // its map is written
  m.beingSaved(run(false, "2026-10-29T07:40:00"), running),                     // older than the job
  m.beingSaved(run(false, "2026-10-29T07:58:00"), [job("done", "2026-10-29T07:50:00", "r")]),
  m.beingSaved(run(false, "2026-10-29T07:58:00"), [job("failed", "2026-10-29T07:50:00")]),
]));""")
    assert found == [True, False, False, False, False]


def test_bundle_is_built_from_sources(tmp_path):
    """viewer/dist is committed: rebuild it (cd viewer; npm run build) after changing viewer/src."""
    subprocess.run(["node", "build.mjs", "--out", str(tmp_path)], cwd=VIEWER, check=True)
    for name in os.listdir(os.path.join(VIEWER, "dist")):
        with open(os.path.join(VIEWER, "dist", name), "rb") as a, open(tmp_path / name, "rb") as b:
            assert a.read() == b.read(), f"viewer/dist/{name} is out of date: run `npm run build` in viewer/"


@pytest.fixture(scope="module")
def clip_data(tmp_path_factory):
    """data.js of a run of the Żabi Koń clip with the surface model (clear lines and lines over trees),
    computed once, offline: (its JSON text, the value)."""
    data = tmp_path_factory.mktemp("clip")
    env = {**os.environ, "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
           "PYTHONIOENCODING": "utf-8"}
    tests_data = os.path.join(REPO, "tests", "data")
    result = subprocess.run(
        [sys.executable, "findaline.py", "--elevation-file", os.path.join(tests_data, "zabi_kon_nmt.tif"),
         "--surface-file", os.path.join(tests_data, "zabi_kon_nmpt.tif"), "--northing", "254608",
         "--easting", "553966", "--radius", "150", "--water", "off", "--fill-gaps", "off", "--no-plot", "--yes",
         "--ortho", "never", "--data-dir", str(data)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=600, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    path = next((data / "runs").glob("*/data.js"))
    text = path.read_text(encoding="utf-8").partition(" = ")[2].rstrip().rstrip(";")
    return text, json.loads(text)


def _js_number(v) -> str:
    """A number as JavaScript's String() writes it (the anchors in keys.lineCoords)."""
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def _key(row) -> str:
    """keys.lineCoords of a data.js row."""
    return ",".join(_js_number(v) for v in row[:4])


def _python_groups(rows, distance, by, keep=lambda r: True):
    """mapping.group_lines over the data.js rows that `keep` passes, as lists of the members' keys."""
    results = [{"a1_xy": (r[4], r[5]), "a2_xy": (r[6], r[7]), "length": r[8], "clearance": r[9],
                "over_obstacles": r[13], "height_max": r[16], "key": _key(r)} for r in rows if keep(r)]
    return [[m["key"] for m in g["members"]] for g in mapping.group_lines(results, distance, by)]


# Run with model.ts bundled as `m`: the model over a run's data (argv[2]) with a fake server
def test_folder_tree(tmp_path):
    """folders.ts: children by name, paths, depth and height, the moves jobs.Jobs.change_folder allows (not into
    itself or its own folders, not where it is, not deeper than FOLDER_DEPTH_MAX), counts with the folders inside."""
    found = run_ts(tmp_path, "folders", """
const f = (id, parent_id, name) => ({id, parent_id, name, created: ""});
const t = new m.FolderTree([f(1, null, "trips"), f(2, 1, "Tatras"), f(3, 2, "West"), f(4, null, "Alps"), f(5, 99, "lost")]);
const chain = new m.FolderTree(Array.from({length: 8}, (_, k) => f(k + 1, k ? k : null, "L" + (k + 1))));
const extra = new m.FolderTree([...chain.byId.values(), f(20, null, "x")]);
console.log(JSON.stringify({
  top: t.children(null).map((x) => x.name), path: t.path(3), depth: [t.depth(null), t.depth(3)], height: t.height(1),
  subtree: t.subtree(1).sort(), moves: [t.canMove(1, 3), t.canMove(1, 1), t.canMove(2, 1), t.canMove(2, 4), t.canMove(2, null)],
  deep: [chain.canAdd(8), chain.canAdd(7), extra.canMove(20, 8), extra.canMove(20, 7)],
  flat: t.flat().map(({folder, depth}) => folder.name + depth),
  counts: [...t.counts([{folder_id: 3}, {folder_id: 2}, {folder_id: null}, {folder_id: 42}]).entries()],
}));""")
    assert found["top"] == ["Alps", "lost", "trips"] and found["path"] == "trips / Tatras / West"
    assert found["depth"] == [0, 3] and found["height"] == 3 and found["subtree"] == [1, 2, 3]
    assert found["moves"] == [False, False, False, True, True]
    assert found["deep"] == [False, True, False, True]
    assert found["flat"] == ["Alps1", "lost1", "trips1", "Tatras2", "West3"]
    assert {k: v for k, v in found["counts"]} == {None: 4, 1: 2, 2: 2, 3: 1}


MODEL_SCRIPT = r"""
const data = JSON.parse(require("fs").readFileSync(process.argv[2], "utf-8"));
const keys = (g) => g.members.map((l) => [l.lat1, l.lon1, l.lat2, l.lon2].join(","));
const tick = () => new Promise((r) => setTimeout(r, 0));
const out = {};
(async () => {
  let favourites = [], refuse = false;
  const api = {loadFavourites: async () => favourites,
               saveFavourite: async () => { if (refuse) throw new Error("refused"); },
               profile: async () => ({d: [0, 1], z: [1, 2], min: [0, 3], water_m: 0, filled_m: 0})};
  let model = new m.ResultModel(data, {favourites: true, api});
  let notified = 0;
  model.subscribe(() => notified++);
  await tick();
  out.groups = model.groups.map(keys);
  out.rows = model.rows.map((r) => keys(r.group)[0]);
  out.rowLen = model.rows.map((r) => r.line.len);
  out.shown = model.shown;
  out.ranges = {len: model.ranges.len, group: model.ranges.group, favourites: model.ranges.favourites};
  model.setState({...model.state, by: "longest", distance: 7});
  out.longest = model.groups.map(keys);
  model.setState({...model.state, by: data.by, distance: data.distance});
  model.setList({...model.list, sort: "clr", desc: false});
  out.byClr = model.rows.map((r) => r.line.clr);
  model.setList({...model.list, sort: "len", desc: true});
  // Filters before grouping, the group size after it
  const lenMin = out.rowLen[Math.floor(out.rowLen.length / 2)];
  model.setFilters({...model.filters, lenMin});
  out.filtered = model.groups.map(keys);
  model.setFilters({...m.NO_FILTERS, groupMin: 3});
  out.groupMin = model.groups.map(keys);
  model.clearFilters();
  model.setState({...model.state, showOver: false});
  out.clearOnly = model.groups.map(keys);
  model.setState({...model.state, showOver: true, group: false});
  out.ungrouped = model.baseRows.map((r) => keys(r.group)[0]);  // the map's order
  model.setState({...model.state, group: true});
  // next / prev walk the rows
  const walk = [];
  model.next(); walk.push(model.position);
  model.next(); walk.push(model.position);
  model.prev(); walk.push(model.position);
  walk.push(model.prev(), model.position);
  out.walk = walk;
  out.first = model.selected.group === model.rows[0].group;
  // Choosing a line of a group keeps the groups; the selection follows it
  const big = model.rows.findIndex((r) => r.n >= 3);
  const group = model.rows[big].group, before = JSON.stringify(model.groups.map(keys));
  model.select(group);
  model.choose(group, group.members[2]);
  out.choose = {same: JSON.stringify(model.groups.map(keys)) === before, variant: model.variantIndex(),
                selected: model.selected.line === group.members[2], row: model.rows[big].line === group.members[2]};
  // A link to a line: found, chosen and selected in its group; hidden by filters: a note, its action clears them
  const multi = model.groups.filter((g) => g.members.length > 1)[1];
  const linked = multi.members[1], link = "#line=" + model.lineLink(linked);
  model.select(null);
  const found = model.openLinked(link);
  out.link = {found: found === linked, selected: model.selected.line === linked, variant: model.variantIndex(),
              unknown: model.openLinked("#line=1,2,3,4"), unknownNote: model.note.text};
  model.setFilters({...model.filters, lenMin: linked.len + 0.5});
  out.hidden = {found: model.openLinked(link), note: model.note.text};
  model.note.onAction();
  out.hidden.after = model.selected.line === linked && model.filters.lenMin === null;
  // Favourites: the favourite of a group is shown, "only favourites", a refused change rolled back
  favourites = [model.lineLink(model.groups.find((g) => g.members.length > 1).members[1])];
  model = new m.ResultModel(data, {favourites: true, api});  // its own lines
  await tick();
  const fav = model.lines.find((l) => model.lineLink(l) === favourites[0]);
  const g0 = model.groupOf(fav);
  out.fav = {on: model.favouritesOn, shown: model.shownOf(g0) === fav, isFav: model.isFavourite(fav),
             asFav: model.shownAsFavourite(g0)};
  model.setFilters({...model.filters, favOnly: true});
  out.favOnly = model.groups.map(keys);
  model.setFilters({...model.filters, favOnly: false});
  const other = model.groups.find((g) => !g.members.includes(fav)).rep;
  refuse = true;
  const saving = model.toggleFavourite(other);
  out.optimistic = model.isFavourite(other);
  await saving;
  out.rolledBack = {fav: model.isFavourite(other), note: model.note.text};
  refuse = false;
  await model.toggleFavourite(fav);
  out.unmarked = {fav: model.isFavourite(fav), stillShown: model.shownOf(model.groupOf(fav)) === fav};
  // A profile closed with × comes back when the spot is selected again
  model.select(model.rows[0].group);
  model.closeProfile();
  out.profileClosed = [model.profileClosed];
  model.select(model.rows[0].group);
  out.profileClosed.push(model.profileClosed);
  // Folders: where a favourite is, a new folder, a line marked into it, a favourite moved to the top level
  const changes = [], made = [];
  const folderApi = {...api,
    loadFavourites: async () => ({lines: [favourites[0]], in: {[favourites[0]]: 5},
                                  folders: [{id: 5, parent_id: null, name: "Trips", created: ""}]}),
    saveFavourite: async (run, l, on, folder) => { changes.push(["save", on, folder ?? null]); },
    moveFavourite: async (run, line, folder) => { changes.push(["move", folder]); },
    addFolder: async (name, parent) => { made.push([name, parent]); return 9; }};
  model = new m.ResultModel(data, {favourites: true, api: folderApi});
  await tick();
  const inTrips = model.lines.find((l) => model.lineLink(l) === favourites[0]);
  const plain = model.groups.find((g) => !g.members.includes(inTrips)).rep;
  out.folders = {of: model.folderOf(inTrips), path: model.tree.path(5), plain: model.folderOf(plain) === undefined};
  const nine = await model.newFolder(" Later ", 5);
  await model.saveTo(plain, nine);
  await model.saveTo(inTrips, null);
  out.folders.after = {made, changes, plain: model.folderOf(plain), trips: model.folderOf(inTrips),
                       path: model.tree.path(9)};
  // Profiles: saved ones as they are, others from the server
  const noProfile = model.lines.find((l) => !l.prof);
  out.profile = noProfile ? (await model.profile(noProfile)).min[1] : null;
  out.notified = notified;
  console.log(JSON.stringify(out));
})();
"""


def test_result_model(tmp_path, clip_data):
    """result/model.ts on a real run: its groups and rows (Python's grouping,
    clear groups first), filters before grouping and the group size after, next/prev, choosing a variant,
    links to lines, favourites (shown in their group, only favourites, a refused change rolled back)."""
    text, data = clip_data
    rows = data["lines"]
    assert any(r[13] for r in rows) and not all(r[13] for r in rows)  # clear lines and lines over trees
    data_path = tmp_path / "data.json"
    data_path.write_text(text, encoding="utf-8")
    bundle = tmp_path / "model.cjs"
    subprocess.run(["node", "-e", "require('esbuild').buildSync({entryPoints: ['src/result/model.ts'], bundle: true, "
                                  f"format: 'cjs', platform: 'node', outfile: {json.dumps(str(bundle))}}})"],
                   cwd=VIEWER, check=True)
    script = tmp_path / "model-test.cjs"
    script.write_text(f"const m = require({json.dumps(str(bundle))});\n{MODEL_SCRIPT}", encoding="utf-8")
    result = subprocess.run(["node", str(script), str(data_path)], capture_output=True, text=True,
                            encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    distance, by = data["distance"], data["by"]
    groups = _python_groups(rows, distance, by)
    assert out["groups"] == groups
    # The list sorted by length (the default), stable: equal lengths keep the groups' order
    length = {_key(r): r[8] for r in rows}
    assert out["rows"] == [g[0] for g in sorted(groups, key=lambda g: -length[g[0]])]
    assert out["shown"] == {"spots": len(groups), "lines": len(rows)}
    assert out["ranges"] == {"len": {"min": min(length.values()), "max": max(length.values())},
                             "group": {"min": 1, "max": max(len(g) for g in groups)}, "favourites": True}
    assert out["longest"] == _python_groups(rows, 7, "longest")
    assert out["byClr"] == sorted(out["byClr"])
    len_min = out["rowLen"][len(out["rowLen"]) // 2]
    assert out["filtered"] == _python_groups(rows, distance, by, lambda r: r[8] >= len_min) != groups
    assert out["groupMin"] == [g for g in groups if len(g) >= 3] and out["groupMin"]
    assert out["clearOnly"] == _python_groups(rows, distance, by, lambda r: not r[13])
    assert out["ungrouped"] == [_key(r) for r in rows if not r[13]] + [_key(r) for r in rows if r[13]]
    assert out["walk"] == [0, 1, 0, False, 0] and out["first"]
    assert out["choose"] == {"same": True, "variant": 2, "selected": True, "row": True}
    assert out["link"] == {"found": True, "selected": True, "variant": 1, "unknown": None,
                           "unknownNote": "The line of the link is not in this run."}
    assert out["hidden"] == {"found": None, "note": "The line of the link is hidden by the filters.", "after": True}
    assert out["fav"] == {"on": True, "shown": True, "isFav": True, "asFav": True}
    assert len(out["favOnly"]) == 1 and len(out["favOnly"][0]) == 1
    assert out["optimistic"] is True
    assert out["rolledBack"] == {"fav": False, "note": "The favourite was not saved: refused"}
    assert out["unmarked"] == {"fav": False, "stillShown": True}
    assert out["profileClosed"] == [True, False]
    assert out["folders"] == {"of": 5, "path": "Trips", "plain": True,
                              "after": {"made": [[" Later ", 5]], "changes": [["save", True, 9], ["move", None]],
                                        "plain": 9, "trips": None, "path": "Trips / Later"}}
    assert out["profile"] in (None, 3)
    assert out["notified"] > 10


EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_result_map_in_edge(tmp_path, clip_data):
    """result/ResultMap and loadRun in headless Edge, on a test page of a run served by the app (its CSP):
    the run loaded as a script, lines drawn, a selection with its variants, a click on a variant shows it,
    measuring (two clicks, Esc cancels), onMove, and destroy() leaves the map without a layer of it."""
    import re
    import test_web  # the app on a real server, its CSP stubs
    text, data = clip_data
    run_id = data["run"]["id"]
    folder = tmp_path / "runs" / run_id
    folder.mkdir(parents=True)
    (folder / "data.js").write_text(f"window.FINDALINE_RUN = {text};\n", encoding="utf-8")
    subprocess.run(["node", "-e", "require('esbuild').buildSync({entryPoints: ['test/resultmap_page.ts'], "
                                  "bundle: true, format: 'iife', target: 'es2020', loader: {'.png': 'dataurl'}, "
                                  f"outdir: {json.dumps(str(folder))}}})"], cwd=VIEWER, check=True)
    (folder / "csp_stub.js").write_text(test_web.CSP_STUB, encoding="utf-8")
    (folder / "resultmap_test.html").write_text(
        '<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="resultmap_page.css">'
        '<script src="csp_stub.js"></script></head><body><script src="resultmap_page.js"></script></body></html>',
        encoding="utf-8")
    dom = test_web._edge_page(tmp_path, f"runs/{run_id}/resultmap_test.html")
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, dom[-2000:]
    out = json.loads(found.group(1).replace("&quot;", '"').replace("&amp;", "&"))
    assert "error" not in out, out["error"]
    assert out["load"] == {"failed": True, "lines": len(data["lines"]), "cleared": True, "cached": True}
    assert out["drawn"] > 0 and out["selectedLayers"] > out["drawn"]
    assert out["clicked"] == ["variant"] and out["variantShown"] is True
    assert out["measured"] is True and out["escaped"] is True and out["moves"] == 1
    assert out["after"] == 0
    assert test_web._violations(dom) == []


def test_messages_match_python(tmp_path):
    """messages.py formats the catalogue as the page's i18next does: interpolation (numbers, values not
    given stay as {{name}}, spaces in the braces) and plural forms (_zero, _one, _other), the key for an unknown one."""
    import messages
    samples = {"lines_one": "{{count}} line in {{place}}", "lines_other": "{{count}} lines in {{place}}",
               "lines_zero": "no lines", "area": "The area is {{ km2 }} km², limit {{max}} km².",
               "plain": "Spot {{n}}"}
    cases = [("lines", {"count": 1, "place": "Gozo"}), ("lines", {"count": 3, "place": "Gozo"}),
             ("lines", {"count": 0}), ("lines", {"count": 1.5, "place": "x"}), ("area", {"km2": 51.25, "max": 50.0}),
             ("plain", {}), ("plain", {"n": "<b>"}), ("unknown.key", {"n": 1}), ("ui.try", {"ui": "D"})]
    found = run_ts(tmp_path, "i18n", f"""
const own = m.translator({json.dumps(samples)});
console.log(JSON.stringify({json.dumps(cases)}.map(([key, params]) =>
  key.startsWith("ui.") ? m.t(key, params) : own(key, params))));""")
    python = [messages.text(key, **params) if key.startswith("ui.") else messages.render(samples, key, **params)
              for key, params in cases]
    assert found == python
    assert python[:3] == ["1 line in Gozo", "3 lines in Gozo", "no lines"] and python[5] == "Spot {{n}}"
    assert python[4] == "The area is 51.25 km², limit 50 km²." and python[6] == "Spot <b>"  # Preact escapes


def test_times_in_the_viewers_zone(tmp_path):
    """time.ts shows the server's UTC times in the viewer's time zone (Warsaw here: CET, CEST in
    summer), times without an offset (stored before UTC) as the viewer's local time, and compares
    them as moments."""
    found = run_ts(tmp_path, "time", """
process.env.TZ = "Europe/Warsaw";
console.log(JSON.stringify([
  m.when("2026-10-31T06:17:52+00:00"), m.when("2026-07-01T23:30:00+00:00"), m.day("2026-07-01T23:30:00+00:00"),
  m.when("2026-10-31T06:17:52"), m.when("2026-10-31 06:17:52"), m.when(null, "never"), m.when("soon"),
  m.shortWhen("2026-10-31T06:17:52+00:00", new Date("2026-10-31T12:00:00+00:00")),
  m.shortWhen("2026-10-30T06:17:52+00:00", new Date("2026-10-31T12:00:00+00:00")),
  m.timeOf("2026-10-31T07:17:52+01:00") === m.timeOf("2026-10-31T06:17:52+00:00"), m.timeOf(undefined)]));""")
    assert found == ["31 Oct 2026, 07:17", "2 Jul 2026, 01:30", "2026-07-02",
                     "31 Oct 2026, 06:17", "31 Oct 2026, 06:17", "never", "soon",
                     "07:17", "2026-10-30 07:17", True, 0]


def test_error_texts_match_python(tmp_path):
    """The page renders a coded error as the server writes its web text (i18n.ts errorText and
    detailMessage, the text of app/api.ts ApiError): the field's label, plural forms, the server's
    message for a code it does not know, and a plain text as it is."""
    import messages
    details = [messages.UserError.web("positive", field="radius").detail(),
               messages.UserError.web("at_most", field="max_length", max="2000 m").detail(),
               messages.Note("x", "coast_pieces_left_out", count=1, near=200, far=400, lat=1, lon=2).info(),
               {"message": "A newer server's text", "code": "no_such_code", "params": {}},
               "a plain text", None]
    found = run_ts(tmp_path, "i18n", f"""
console.log(JSON.stringify({json.dumps(details)}.map((d) => m.detailMessage(d, "fallback"))));""")
    assert found == ["Search radius must be more than 0.", "Longest line: at most 2000 m on this site.",
                     messages.Note("x", "coast_pieces_left_out", count=1, near=200, far=400, lat=1, lon=2).web_text(),
                     "A newer server's text", "a plain text", "fallback"]
    assert found[2].startswith("1 part of the coast is 200–400 m")
