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
    result = subprocess.run(["node", str(path)], capture_output=True, text=True, check=True)
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


def test_bundle_is_built_from_sources(tmp_path):
    """viewer/dist is committed: rebuild it (cd viewer; npm run build) after changing viewer/src."""
    subprocess.run(["node", "build.mjs", "--out", str(tmp_path)], cwd=VIEWER, check=True)
    for name in os.listdir(os.path.join(VIEWER, "dist")):
        with open(os.path.join(VIEWER, "dist", name), "rb") as a, open(tmp_path / name, "rb") as b:
            assert a.read() == b.read(), f"viewer/dist/{name} is out of date: run `npm run build` in viewer/"
