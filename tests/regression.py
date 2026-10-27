"""Regression tests: run saved searches and compare the results with tests/regression.json.

    python tests/regression.py                 # the quick set (R13)
    python tests/regression.py R1 R7           # chosen tests
    python tests/regression.py --set pl        # a set: quick, pl, search, sources, water, network
    python tests/regression.py --all           # everything (R12 only with OPENTOPOGRAPHY_API_KEY)
    python tests/regression.py R1 --update     # store the new results as expected
    python tests/regression.py --write-doc     # rewrite the table in docs/regression-tests.md
    python tests/regression.py R1 --browser    # also count the spots of the map in a headless browser

Each test runs with its own temporary data folder (runs, map, work) whose cache/ links
to the real cache (or to --cache, e.g. an empty folder to test downloads). The output
of every search is kept in the log folder printed at the end.

What is compared (expected values in regression.json):
- the counts of the search: anchors, open anchors, checked pairs, lines, spots, water...;
- the content of the results: the sha256 of lines.csv, with readable totals (length,
  clearances, lines over trees) that show what changed when the checksum does;
- the data: the share of the search area with elevation data before any filling, and
  the tiles without data (a lost tile shows here, e.g. the north coast of Gozo);
- "same_as": lines.csv identical to that of another test (tiles against one grid);
- "count_anchors_in": osm:<name> or S,W,N,E: the lines with an anchor in it (lines_in_region),
  e.g. the lines reaching an islet.
Always checked, without expected values: the map data (data.js has every line, profiles
for every line of a circle and for the line shown for each group of an area, lines.js
the same lines), and with "check_profiles" that profiles computed again from the cache
(search.profile_of_line) equal the saved ones. A test more than twice as slow as its
recorded time gets a warning. Tests of kind "osm" compare the theme tiles built from a
Geofabrik extract with those from Overpass (network).
"""

import argparse
import contextlib
import csv
import glob
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cache  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(REPO, "tests", "regression.json")
DOC = os.path.join(REPO, "docs", "regression-tests.md")
TABLE_START, TABLE_END = "<!-- regression-table:start -->", "<!-- regression-table:end -->"
SLOWER = 2.0  # a test this many times slower than its recorded time gets a warning
BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "msedge", "google-chrome", "chromium"]

PATTERNS = {
    "source": (r"^Data: (.+) \([^()]*\) - ", str),  # "Data: <name> (<reason>) - <description>"
    "coverage": (r"Elevation data: ([\d.]+)% of the search area", float),
    "anchors": (r"Found (\d+) potential anchors", int),
    "open": (r"Anchors with an open direction: (\d+)", int),
    "checked": (r"Checked pixel by pixel: (\d+)", int),
    "filled_km2": (r"Filled ([\d.]+) km² of gaps", float),
    "filled_from": (r"Filled [\d.]+ km² of gaps in the data from (.+)$", str),
    "sea_km2": (r"filling sea and lakes.*done.*sea ([\d.]+) km²", float),
}
# Stored by --update when found; the others only when they were expected already (see stored)
ALWAYS = ("anchors", "open", "checked", "lines", "spots", "csv_sha256", "length_km", "clearance_min",
          "clearance_mean", "clearance_max", "over_trees", "coverage", "empty_tiles")


def load() -> dict:
    with open(TESTS, encoding="utf-8") as f:
        return json.load(f)


def full_args(tests: dict, test_id: str) -> list:
    test = tests[test_id]
    base = full_args(tests, test["extends"]) if "extends" in test else []
    return base + shlex.split(test["args"])


def _script_value(path: str):
    """The value a data.js / lines.js script assigns."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return json.loads(text[text.index(" = {") + 3:].rstrip().rstrip(";"))


def measure(output: str, run_dir: str) -> dict:
    found = {}
    for key, (pattern, kind) in PATTERNS.items():
        match = re.search(pattern, output, re.M)
        if match:
            found[key] = kind(match.group(1))
    empty = {}  # per layer, the most tiles without data seen (tiled areas report every tile)
    for layer, n in re.findall(r"^(.+): (\d+) of \d+ tiles without data", output, re.M):
        empty[layer] = max(empty.get(layer, 0), int(n))
    found["empty_tiles"] = sum(empty.values())
    files = glob.glob(os.path.join(run_dir, "*", "lines.csv"))
    if files:
        with open(files[0], "rb") as f:
            found["csv_sha256"] = hashlib.sha256(f.read()).hexdigest()
        with open(files[0], encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        found["lines"] = len(rows)
        found["spots"] = sum(r["representative"] == "True" for r in rows)
        found["water_lines"] = sum(float(r["water_m"]) > 0 for r in rows)
        found["water_spots"] = sum(float(r["water_m"]) > 0 and r["representative"] == "True" for r in rows)
        clearances = [float(r["clearance_m"]) for r in rows]
        found["length_km"] = round(sum(float(r["length_m"]) for r in rows) / 1000, 3)
        found["clearance_min"] = min(clearances, default=0.0)
        found["clearance_mean"] = round(sum(clearances) / len(clearances), 3) if clearances else 0.0
        found["clearance_max"] = max(clearances, default=0.0)
        found["over_trees"] = sum(r["over_trees_buildings"] == "True" for r in rows)
    return found


def count_in_region(run_dir: str, where: str, cache_dir: str) -> int:
    """Lines with at least one anchor in a region (area.region: osm:<name> or S,W,N,E)."""
    import area
    from shapely.geometry import Point
    region = area.region(where, cache_dir)
    with open(glob.glob(os.path.join(run_dir, "*", "lines.csv"))[0], encoding="utf-8-sig") as f:
        return sum(any(region.contains(Point(float(r[f"anchor{k}_lon"]), float(r[f"anchor{k}_lat"]))) for k in (1, 2))
                   for r in csv.DictReader(f))


def check_map(run_dir: str, found: dict) -> list:
    """The map data agrees with lines.csv: problems as text."""
    folders = [os.path.dirname(p) for p in glob.glob(os.path.join(run_dir, "*", "run.json"))]
    if not folders or not os.path.exists(os.path.join(folders[0], "data.js")):
        return []
    folder = folders[0]
    with open(os.path.join(folder, "run.json"), encoding="utf-8") as f:
        circle = json.load(f)["params"].get("radius") is not None
    rows = _script_value(os.path.join(folder, "data.js"))["lines"]
    others = _script_value(os.path.join(folder, "lines.js"))["lines"]
    problems = []
    if len(rows) != found.get("lines"):
        problems.append(f"data.js has {len(rows)} lines, lines.csv {found.get('lines')}")
    profiles = sum(1 for r in rows if r[-1])
    wanted = found.get("lines") if circle else found.get("spots")
    if profiles != wanted:
        problems.append(f"data.js has {profiles} profiles, expected {wanted} ({'circle' if circle else 'area'})")
    if [r[:-1] for r in rows] != others:  # lines.js: the rows without the profile
        problems.append("lines.js differs from data.js")
    return problems


def check_profiles(run_dir: str, work: str, count: int) -> list:
    """Profiles computed again from the cache equal the saved ones (the first `count`)."""
    import search
    folder = os.path.dirname(glob.glob(os.path.join(run_dir, "*", "run.json"))[0])
    with open(os.path.join(folder, "run.json"), encoding="utf-8") as f:
        meta = json.load(f)
    rows = [r for r in _script_value(os.path.join(folder, "data.js"))["lines"] if r[-1]][:count]
    problems = []
    for row in rows:
        paths = search.Paths(os.path.dirname(run_dir), os.path.join(work, "profile-work"))
        os.makedirs(paths.work, exist_ok=True)
        with contextlib.redirect_stdout(io.StringIO()):  # the messages of building the grids
            again = search.profile_of_line(meta, paths, (row[0], row[1]), (row[2], row[3]))
        shutil.rmtree(paths.work, ignore_errors=True)
        if again != row[-1]:
            keys = [k for k in row[-1] if again.get(k) != row[-1][k]]
            problems.append(f"profile of the line at {row[0]:.6f}, {row[1]:.6f} computed again differs in {keys}")
    return problems


def check_browser(run_dir: str, work: str, found: dict) -> list:
    """The spot count of the map in a headless browser equals the one of lines.csv."""
    browser = next((b for b in BROWSERS if os.path.exists(b) or shutil.which(b)), None)
    if browser is None:
        return ["no headless browser found (Edge or Chrome)"]
    folder = os.path.dirname(glob.glob(os.path.join(run_dir, "*", "map.html"))[0])
    with open(os.path.join(folder, "zz_check.js"), "w", encoding="utf-8") as f:
        f.write('window.addEventListener("load", function() { setTimeout(function() {'
                'var p = document.createElement("pre"); p.id = "result";'
                'p.textContent = document.querySelector(".fal-summary").textContent;'
                'document.body.appendChild(p); }, 500); });')
    with open(os.path.join(folder, "map.html"), encoding="utf-8") as f:
        page = f.read().replace("</body>", '<script src="zz_check.js"></script></body>')
    with open(os.path.join(folder, "zz_check.html"), "w", encoding="utf-8") as f:
        f.write(page)
    url = "file:///" + os.path.join(folder, "zz_check.html").replace(os.sep, "/")
    result = subprocess.run([shutil.which(browser) or browser, "--headless", "--disable-gpu",
                             f"--user-data-dir={os.path.join(work, 'browser-profile')}",
                             "--allow-file-access-from-files", "--virtual-time-budget=20000", "--dump-dom", url],
                            capture_output=True, text=True, encoding="utf-8", timeout=300)
    match = re.search(r'id="result">(\d+) spots', result.stdout)
    if not match:
        return ["the map did not show its spots in the browser"]
    if int(match.group(1)) != found.get("spots"):
        return [f"the map shows {match.group(1)} spots, lines.csv has {found.get('spots')}"]
    return []


def link(target: str, link_path: str):
    """A directory link (a junction on Windows: no special rights needed)."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(os.path.abspath(target), link_path)
    else:
        os.symlink(os.path.abspath(target), link_path)


def run(tests: dict, test_id: str, work: str, cache_dir: str, browser: bool) -> dict:
    out = os.path.join(work, test_id)
    os.makedirs(out, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    link(cache_dir, os.path.join(out, "cache"))
    # Orthophoto tiles are not compared and can take many minutes to fetch (R9: 15 000 tiles)
    cmd = [sys.executable, "findaline.py", *full_args(tests, test_id), "--no-plot", "--yes", "--ortho", "never",
           "--data-dir", out]
    env = {**os.environ, "FINDALINE_PROGRESS": "text", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8")
    with open(os.path.join(out, "output.log"), "w", encoding="utf-8") as f:
        f.write(result.stdout + result.stderr)
    runs = os.path.join(out, "runs")
    found = measure(result.stdout, runs)
    found["exit"] = result.returncode
    if not result.returncode:
        problems = check_map(runs, found)
        if tests[test_id].get("count_anchors_in"):
            found["lines_in_region"] = count_in_region(runs, tests[test_id]["count_anchors_in"], cache_dir)
        if tests[test_id].get("check_profiles"):
            problems += check_profiles(runs, out, tests[test_id]["check_profiles"])
        if browser:
            problems += check_browser(runs, out, found)
        found["problems"] = problems
    return found


def run_osm(test: dict, work: str, cache_dir: str) -> dict:
    """Theme tiles of a box built from a Geofabrik extract and fetched from Overpass, compared."""
    import osm
    import osmextract
    import requests
    region, (west, south, east, north) = test["region"], test["bbox"]
    from_extract = os.path.join(work, "extract", "cache")
    from_overpass = os.path.join(work, "overpass", "cache")
    for folder in (from_extract, from_overpass):
        os.makedirs(os.path.join(folder, "osm"), exist_ok=True)
    index = os.path.join(cache_dir, "osm", "geofabrik-index.json")
    country = next((os.path.basename(os.path.dirname(os.path.dirname(p))) for p in
                    glob.glob(os.path.join(cache_dir, "*", "osm-extract", f"{region}.osm.pbf"))), None)
    if country is None or not os.path.exists(index):
        return {"skipped": f"the {region} extract is not downloaded (findaline.py osm download {region})"}
    shutil.copy(index, os.path.join(from_extract, "osm", "geofabrik-index.json"))
    link(os.path.join(cache_dir, country), os.path.join(from_extract, country))
    found, same = {}, True
    for theme in test["themes"]:
        features = {}
        for name, folder in (("extract", from_extract), ("overpass", from_overpass)):
            for lat, lon in osm.tiles(west, south, east, north):
                if name == "extract":
                    if not osmextract.build_tile(folder, theme, lat, lon):
                        return {"skipped": f"the {region} extract does not cover the tile {lat}, {lon}"}
                else:
                    try:
                        osm.fetch_tile(theme, lat, lon, folder)  # no extract here: Overpass
                    except requests.RequestException as e:
                        return {"skipped": f"Overpass did not answer ({e})"}
            features[name] = {f["id"]: f["geometry"] for f in osm.theme(theme, west, south, east, north, folder)}
        found[theme] = len(features["extract"])
        a, b = features["extract"], features["overpass"]
        same &= set(a) == set(b) and all(a[k].equals(b[k]) for k in a)
    found["same"] = same
    return found


def compare(expected: dict, found: dict) -> list:
    """Differences as 'key: expected -> found'."""
    diffs = [f"exit code {found['exit']}"] if found.get("exit") else []
    for key, value in expected.items():
        got = found.get(key)
        same = got is not None and (abs(got - value) < 1e-9 if isinstance(value, float) else got == value)
        if not same:
            diffs.append(f"{key}: {value} -> {got}")
    return diffs + found.get("problems", [])


def stored(found: dict, old: dict) -> dict:
    """What --update keeps: the always-present values plus the optional values that matter."""
    keep = {k: found[k] for k in ALWAYS if k in found}
    for key in ("source", "filled_km2", "filled_from"):
        if key in found and (key in old or key != "source"):
            keep[key] = found[key]
    for key in ("sea_km2", "water_lines", "water_spots"):
        if key in old or found.get(key):
            keep[key] = found.get(key, 0)
    if "lines_in_region" in found:
        keep["lines_in_region"] = found["lines_in_region"]
    return keep


def number(n) -> str:
    return f"{n:,}".replace(",", " ")


def write_doc(tests: dict):
    rows = ["| ID | Place / what it covers | Arguments | Anchors | Open anchors / checked pairs | Lines / spots | Also check |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for test_id, test in tests.items():
        e = test["expected"]
        if test.get("kind") == "osm":
            rows.append(f"| {test_id} | {test['place']} | extract `{test['region']}`, box {test['bbox']} | | | | "
                        + "; ".join(f"{e.get(t)} {t} features" for t in test["themes"])
                        + ", the same from Overpass |")
            continue
        args = f"{test['extends']} + `{test['args']}`" if "extends" in test else f"`{test['args']}`"
        also = []
        if "source" in e:
            also.append(f"source `{e['source']}`")
        if "coverage" in e:
            also.append(f"data {e['coverage']:g}%" + (f", {e['empty_tiles']} tiles without data"
                                                      if e.get("empty_tiles") else ""))
        if "filled_km2" in e:
            also.append(f"`Filled {e['filled_km2']:.2f} km² ... from {e.get('filled_from', '')}`")
        if e.get("sea_km2"):
            also.append(f"sea {e['sea_km2']:.2f} km²")
        if e.get("water_spots"):
            also.append(f"{number(e['water_spots'])} spots over water")
        if e.get("water_lines"):
            also.append(f"{number(e['water_lines'])} lines with `water_m > 0`")
        if test.get("same_as"):
            also.append(f"lines.csv identical to {test['same_as']}")
        if test.get("check_profiles"):
            also.append(f"{test['check_profiles']} profiles computed again")
        if "lines_in_region" in e:
            also.append(f"{number(e['lines_in_region'])} lines with an anchor in {test['count_anchors_in']}")
        if test.get("notes"):
            also.append(test["notes"])
        rows.append(f"| {test_id} | {test['place']} | {args} | {number(e['anchors'])} | "
                    f"{number(e['open'])} / {number(e['checked'])} | {number(e['lines'])} / {number(e['spots'])} | "
                    f"{'; '.join(also)} |")
    with open(DOC, encoding="utf-8") as f:
        text = f.read()
    start, end = text.index(TABLE_START) + len(TABLE_START), text.index(TABLE_END)
    with open(DOC, "w", encoding="utf-8", newline="\n") as f:
        f.write(text[:start] + "\n" + "\n".join(rows) + "\n" + text[end:])
    print(f"Table written to {DOC}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", help="test IDs (default: the quick set)")
    parser.add_argument("--set", action="append", default=[], help="run a named set (repeatable)")
    parser.add_argument("--all", action="store_true", help="run every test")
    parser.add_argument("--update", action="store_true", help="store the results of the run tests as expected")
    parser.add_argument("--write-doc", action="store_true", help="rewrite the table in docs/regression-tests.md")
    parser.add_argument("--browser", action="store_true", help="also check the map in a headless browser")
    parser.add_argument("--cache", help="cache folder for the searches (default: the real one, "
                                         "cache/ in FINDALINE_DATA or the repository)")
    args = parser.parse_args()

    tests = load()
    ids = list(args.ids)
    for name in args.set:
        ids += [t for t, test in tests.items() if name in test["sets"]]
    if args.all:
        ids = list(tests)
    if not ids and not (args.write_doc and not args.update):
        ids = [t for t, test in tests.items() if "quick" in test["sets"]]
    ids = list(dict.fromkeys(ids))
    unknown = [t for t in ids if t not in tests]
    if unknown:
        sys.exit(f"Unknown tests: {', '.join(unknown)}")

    work = tempfile.mkdtemp(prefix="findaline-regression-")
    cache_dir = args.cache or os.path.join(cache.data_dir(), "cache")
    failed, skipped, results = [], [], {}
    for test_id in ids:
        test = tests[test_id]
        if test.get("needs_env") and not os.environ.get(test["needs_env"]):
            print(f"{test_id}: skipped ({test['needs_env']} not set)")
            skipped.append(test_id)
            continue
        start = time.time()
        if test.get("kind") == "osm":
            found = run_osm(test, os.path.join(work, test_id), cache_dir)
        else:
            found = run(tests, test_id, work, cache_dir, args.browser)
        seconds = time.time() - start
        if "skipped" in found:
            print(f"{test_id}: skipped ({found['skipped']})")
            skipped.append(test_id)
            continue
        results[test_id] = found
        diffs = compare(test["expected"], found)
        other = test.get("same_as")
        if other:
            sha = results.get(other, {}).get("csv_sha256") or tests[other]["expected"].get("csv_sha256")
            if found.get("csv_sha256") != sha:
                diffs.append(f"lines.csv differs from that of {other}")
        status = "ok" if not diffs else "DIFFERENT"
        slow = test.get("seconds") and seconds > SLOWER * test["seconds"] and seconds - test["seconds"] > 5
        print(f"{test_id}: {status} ({seconds:.0f} s" + (f", WARNING: recorded {test['seconds']:.0f} s" if slow else "")
              + ")" + "".join(f"\n    {d}" for d in diffs), flush=True)
        if diffs:
            failed.append(test_id)
        if args.update and not found.get("exit") and not found.get("problems"):
            test["expected"] = ({k: v for k, v in found.items() if k != "exit"} if test.get("kind") == "osm"
                                else stored(found, test["expected"]))
            test["seconds"] = round(seconds, 1)
    if args.update:
        with open(TESTS, "w", encoding="utf-8", newline="\n") as f:
            json.dump(tests, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"Expected values updated in {TESTS}")
    if args.write_doc:
        write_doc(tests)
    if ids:
        print(f"\n{len(ids) - len(failed) - len(skipped)} ok, {len(failed)} different, {len(skipped)} skipped; "
              f"logs in {work}")
    sys.exit(1 if failed and not args.update else 0)


if __name__ == "__main__":
    main()
