"""
Run by hand: the memory of the lines of a search per line (docs/performance.md; search.LINE_BYTES).

Takes the lines of a tiled run (its tiles/<k>.table.pkl.gz) and passes them through steps 4 and 5
as the code of --repo does (this checkout, or another one, e.g. an older version, given as
--legacy when it keeps a dict per line), measuring the traced heap (tracemalloc) above what was
there before, divided by the number of lines:
    lines found     the output of the line search (find_valid_lines)
    step 4 peak     coordify and group_lines over them (the peak of a tile's step 4, the grids apart)
    held            the lines with their coordinates, as a finished tile keeps them
    step 5 peak     save_results and save_map of all of them
The grids of a tile are not included (search.PIXEL_BYTES), nor the profiles: the representatives
get one profile of the run (a circle keeps one for every line: measure it with perf_profile.py).

    uv run tools/perf_lines_memory.py --tiles <run>/tiles --out <scratch> [--repo <checkout> --legacy]
"""

import argparse
import glob
import gzip
import os
import pickle
import sys
import time
import tracemalloc

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Table:
    """A tile's helpers.LineTable read without this checkout's helpers (another --repo)."""


class _Unpickler(pickle.Unpickler):
    def find_class(self, module, name):
        return _Table if name == "LineTable" else super().find_class(module, name)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tiles", required=True, help="the tiles folder of a tiled run of this version")
    parser.add_argument("--out", required=True, help="a scratch folder for the saved files")
    parser.add_argument("--repo", default=HERE, help="the checkout whose code is measured")
    parser.add_argument("--legacy", action="store_true", help="the code of --repo keeps a dict per line")
    args = parser.parse_args()
    sys.path.insert(0, args.repo)
    import numpy as np
    import grid as gd
    import helpers as sh
    import mapping as mp
    import runs as rn

    tables = []
    for path in sorted(glob.glob(os.path.join(args.tiles, "*.table.pkl.gz"))):
        with gzip.open(path, "rb") as f:
            table = _Unpickler(f).load()
        if len(table.columns["a1"]):
            tables.append(table)
    a1 = np.concatenate([t.columns["a1"] for t in tables])
    a2 = np.concatenate([t.columns["a2"] for t in tables])
    values = {k: np.concatenate([t.columns[k] for t in tables]) for k in sh.Line._fields[2:]}
    some_profile = next(p for t in tables for p in t.profiles.values())
    del tables
    n = len(a1)
    grid = gd.Grid(left=430000.0, top=3990000.0, res=1.0, height=int(a1[:, 0].max()) + 1,
                   width=int(a1[:, 1].max()) + 1, crs="EPSG:32633")

    class Area:
        center = (36.05, 14.25)

        @staticmethod
        def shape_wgs84():
            from shapely.geometry import Point
            return Point(14.25, 36.05).buffer(0.05)

    os.makedirs(args.out, exist_ok=True)
    run = {"id": "r", "name": "memory", "created": "2026-10-31T10:00:00+00:00"}
    results = {}
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]

    def mark(name, peak=False):
        current, top = tracemalloc.get_traced_memory()
        results[name] = ((top if peak else current) - base) / n
        tracemalloc.reset_peak()

    # The output of the line search
    if args.legacy:
        names = sh.Line._fields[2:]
        lines = [sh.Line(tuple(p), tuple(q), *v[:4], None if v[4] != v[4] else v[4], *v[5:])
                 for p, q, *v in zip(a1.tolist(), a2.tolist(), *(values[k].tolist() for k in names))]
    else:
        lines = sh.LineTable({"a1": a1.copy(), "a2": a2.copy(), **{k: v.copy() for k, v in values.items()}})
    del a1, a2, values
    mark("lines found")
    t = time.time()
    # Step 4: coordinates, metres over water and gaps, groups, the representatives' profiles
    if args.legacy:
        found = mp.coordify(lines, grid, "mark")
        for r in found:
            r["water_m"], r["filled_m"], r["profile"] = 0.0, 0.0, None
        groups = mp.group_lines(found, 20, "clearance")
        for g in groups:
            g["rep"]["profile"] = some_profile
    else:
        found = mp.coordify(lines, grid, "mark")
        found.columns["water_m"] = np.zeros(n)
        found.columns["filled_m"] = np.zeros(n)
        groups = mp.group_lines(found, 20, "clearance")
        found.profiles = {k: some_profile for k in groups.rep.tolist()}
    mark("step 4 peak", peak=True)
    del lines
    mark("held")
    step4 = time.time() - t
    t = time.time()
    meta = {"name": "memory", "summary": {"lines": n}}
    if args.legacy:
        rn.save_results(args.out, groups, meta)
    else:
        rn.save_results(args.out, found, groups, meta)
    mp.save_map(found, Area, args.out, os.path.join(args.out, "runs"), run, "EPSG:32633")
    mark("step 5 peak", peak=True)
    print(f"{n} lines, {len(groups)} groups ({'dicts' if args.legacy else 'columns'}"
          f"): step 4 {step4:.1f} s, "
          f"step 5 {time.time() - t:.1f} s")
    for name, value in results.items():
        print(f"  {name:12} {value:8.0f} bytes per line")


if __name__ == "__main__":
    main()
