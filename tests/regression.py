"""Regression tests: run saved searches and compare the results with tests/regression.json.

    python tests/regression.py                 # the quick set (R13)
    python tests/regression.py R1 R7           # chosen tests
    python tests/regression.py --set pl        # a set: quick, pl, search, sources, water
    python tests/regression.py --all           # everything (R12 only with OPENTOPOGRAPHY_API_KEY)
    python tests/regression.py R1 --update     # store the new results as expected
    python tests/regression.py --write-doc     # rewrite the table in docs/regression-tests.md

Each test runs with its own temporary data folder (runs, map, work) whose cache/ links
to the real cache (or to --cache, e.g. an empty folder to test downloads). The output
of every search is kept in the log folder printed at the end.
"""

import argparse
import csv
import glob
import json
import os
import re
import shlex
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

PATTERNS = {
    "source": (r"^Data: (.+) \([^()]*\) - ", str),  # "Data: <name> (<reason>) - <description>"
    "anchors": (r"Found (\d+) potential anchors", int),
    "open": (r"Anchors with an open direction: (\d+)", int),
    "checked": (r"Checked pixel by pixel: (\d+)", int),
    "filled_km2": (r"Filled ([\d.]+) km² of gaps", float),
    "filled_from": (r"Filled [\d.]+ km² of gaps in the data from (.+)$", str),
    "sea_km2": (r"filling sea and lakes.*done.*sea ([\d.]+) km²", float),
}


def load() -> dict:
    with open(TESTS, encoding="utf-8") as f:
        return json.load(f)


def full_args(tests: dict, test_id: str) -> list:
    test = tests[test_id]
    base = full_args(tests, test["extends"]) if "extends" in test else []
    return base + shlex.split(test["args"])


def measure(output: str, run_dir: str) -> dict:
    found = {}
    for key, (pattern, kind) in PATTERNS.items():
        match = re.search(pattern, output, re.M)
        if match:
            found[key] = kind(match.group(1))
    files = glob.glob(os.path.join(run_dir, "*", "lines.csv"))
    if files:
        with open(files[0], encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        found["lines"] = len(rows)
        found["spots"] = sum(r["representative"] == "True" for r in rows)
        found["water_lines"] = sum(float(r["water_m"]) > 0 for r in rows)
        found["water_spots"] = sum(float(r["water_m"]) > 0 and r["representative"] == "True" for r in rows)
    return found


def link(target: str, link_path: str):
    """A directory link (a junction on Windows: no special rights needed)."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(os.path.abspath(target), link_path)
    else:
        os.symlink(os.path.abspath(target), link_path)


def run(tests: dict, test_id: str, work: str, cache_dir: str) -> dict:
    out = os.path.join(work, test_id)
    os.makedirs(out, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    link(cache_dir, os.path.join(out, "cache"))
    # Orthophoto tiles are not compared and can take many minutes to fetch (R9: 15 000 tiles)
    cmd = [sys.executable, "findaline.py", *full_args(tests, test_id), "--no-plot", "--yes", "--no-ortho-cache",
           "--data-dir", out]
    env = {**os.environ, "FINDALINE_PROGRESS": "text", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8")
    with open(os.path.join(out, "output.log"), "w", encoding="utf-8") as f:
        f.write(result.stdout + result.stderr)
    found = measure(result.stdout, os.path.join(out, "runs"))
    found["exit"] = result.returncode
    return found


def compare(expected: dict, found: dict) -> list:
    """Differences as 'key: expected -> found'."""
    diffs = [f"exit code {found['exit']}"] if found.get("exit") else []
    for key, value in expected.items():
        got = found.get(key)
        same = got is not None and (abs(got - value) < 1e-9 if isinstance(value, float) else got == value)
        if not same:
            diffs.append(f"{key}: {value} -> {got}")
    return diffs


def stored(found: dict, old: dict) -> dict:
    """What --update keeps: the always-present counts plus the optional values that matter."""
    keep = {k: found[k] for k in ("anchors", "open", "checked", "lines", "spots") if k in found}
    for key in ("source", "filled_km2", "filled_from"):
        if key in found and (key in old or key != "source"):
            keep[key] = found[key]
    for key in ("sea_km2", "water_lines", "water_spots"):
        if key in old or found.get(key):
            keep[key] = found.get(key, 0)
    return keep


def number(n) -> str:
    return f"{n:,}".replace(",", " ")


def write_doc(tests: dict):
    rows = ["| ID | Place / what it covers | Arguments | Anchors | Open anchors / checked pairs | Lines / spots | Also check |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for test_id, test in tests.items():
        e = test["expected"]
        args = f"{test['extends']} + `{test['args']}`" if "extends" in test else f"`{test['args']}`"
        also = []
        if "source" in e:
            also.append(f"source `{e['source']}`")
        if "filled_km2" in e:
            also.append(f"`Filled {e['filled_km2']:.2f} km² ... from {e.get('filled_from', '')}`")
        if e.get("sea_km2"):
            also.append(f"sea {e['sea_km2']:.2f} km²")
        if e.get("water_spots"):
            also.append(f"{number(e['water_spots'])} spots over water")
        if e.get("water_lines"):
            also.append(f"{number(e['water_lines'])} lines with `water_m > 0`")
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
    failed, skipped = [], []
    for test_id in ids:
        test = tests[test_id]
        if test.get("needs_env") and not os.environ.get(test["needs_env"]):
            print(f"{test_id}: skipped ({test['needs_env']} not set)")
            skipped.append(test_id)
            continue
        start = time.time()
        found = run(tests, test_id, work, args.cache or os.path.join(cache.data_dir(), "cache"))
        diffs = compare(test["expected"], found)
        status = "ok" if not diffs else "DIFFERENT"
        print(f"{test_id}: {status} ({time.time() - start:.0f} s)" + "".join(f"\n    {d}" for d in diffs), flush=True)
        if diffs:
            failed.append(test_id)
        if args.update and not found.get("exit"):
            test["expected"] = stored(found, test["expected"])
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
