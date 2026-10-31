"""
Run by hand: how fast the download phase of a search is with N download threads (docs/performance.md).

For each count of threads: a new data folder under <out> whose cache holds only the OSM data
of --osm-from (themes and Nominatim answers, no world files or extracts), so the elevation
tiles and the water mask are downloaded from scratch; then search.download_data, as the web
worker's download phase runs it, with sources.DOWNLOAD_THREADS set. Prints wall time, MB
received, MB/s and the CPU time of the process. The real cache is never written.

    uv run tools/perf_download.py --out <scratch> --osm-from cache/osm --threads 2 4 8 -- --lat ... --lon ...
"""

import argparse
import os
import shutil
import sys
import time

import psutil

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import cache  # noqa: E402
import params as pm  # noqa: E402
import search  # noqa: E402
import sources  # noqa: E402


def folder_bytes(path: str, skip: str) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if os.path.join(root, d) != skip]
        total += sum(os.path.getsize(os.path.join(root, f)) for f in files if not f.endswith(".sqlite"))
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="folder for the data folders of the runs")
    parser.add_argument("--osm-from", required=True, help="cache/osm to copy the OSM themes and answers from")
    parser.add_argument("--threads", type=int, nargs="+", default=[4])
    parser.add_argument("--name", default="download")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="-- and the search options of findaline.py")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):  # "km²" on a Windows console
        stream.reconfigure(encoding="utf-8", errors="replace")
    search_args = args.args[1:] if args.args[:1] == ["--"] else args.args
    (params,), _ = pm.parse_cli(search_args)
    me = psutil.Process()
    for threads in args.threads:
        data = os.path.join(args.out, f"{args.name}-{threads}")
        shutil.rmtree(data, ignore_errors=True)
        osm = os.path.join(data, "cache", "osm")
        shutil.copytree(args.osm_from, osm, ignore=shutil.ignore_patterns(
            "water-polygons", "land-polygons", "osm-extract", "*-wal", "*-shm"))
        with open(os.path.join(data, "cache", "LAYOUT"), "w") as f:
            f.write(str(cache.LAYOUT))
        paths = search.Paths(data, cache.new_work(data))
        plan = search.plan_search(params, paths)
        sources.DOWNLOAD_THREADS = threads
        cpu0, t0 = sum(me.cpu_times()[:2]), time.monotonic()
        search.download_data(plan)
        wall, cpu = time.monotonic() - t0, sum(me.cpu_times()[:2]) - cpu0
        mb = folder_bytes(os.path.join(data, "cache"), osm) / 1e6
        print(f"{args.name}: {threads} threads: {wall:.1f} s, {mb:.0f} MB on disk, {mb / wall:.1f} MB/s, "
              f"CPU {cpu:.1f} s ({cpu / wall:.2f} cores)", flush=True)


if __name__ == "__main__":
    main()
