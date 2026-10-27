"""
Find-a-Line - Highline Location Finder
========================================

This application finds locations with significant highline potential by analyzing
elevation data: terrain (NMT/DTM) and surface (NMPT/DSM) models from national
LiDAR services (geoportal.gov.pl, Malta), Europe-wide models (FABDEM, Copernicus),
NASADEM or local files. See sources.py.

This file is the command line: the options come from params.py, the search itself
is search.py.

Example:
    python findaline.py --lat 50.1559508 --lon 19.7562722

Run `python findaline.py --help` for all options. The cache commands
(`python findaline.py cache status|update`, `osm download|update`) are update.py,
the web application (`python findaline.py web`) is web.py, its worker as a process of
its own (`python findaline.py worker`, with `web --no-worker`) jobs.py.
"""

import gc
import os
import sys

import cache
import mapping as mp
import params as pm
import runs as rn
import search
from progress import progress

DESCRIPTION = ("Find highline spots using elevation data (geoportal.gov.pl, Malta, FABDEM/"
               "Copernicus, NASADEM or your own files).")


def print_event(kind: str, text: str):
    if kind == "warning":
        print("\n" + "!" * 60 + "\nWARNING: " + text + "\n" + "!" * 60 + "\n")
    else:
        print(text)


def ask(question):
    """Ask a yes/no question; anything but yes (or no terminal) means no."""
    try:
        answer = progress.ask(question)
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes", "t", "tak")


def confirmer(assume_yes: bool):
    """search.Confirm for the terminal."""
    def confirm(kind: str, text: str) -> bool:
        if kind == "expensive":
            print("\nThis run looks expensive:")
            for reason in text.splitlines():
                print(f"  - {reason}")
            print("Consider a smaller --radius or area, or a coarser --resolution.")
            if assume_yes:
                return True
            if not ask("Continue? [y/N] "):
                sys.exit("Stopped. Use --yes to skip this question.")
            return True
        return assume_yes or ask(text)
    return confirm


def print_lines(lines: search.Lines, group_distance: float, group_by: str, all_lines: bool):
    results = lines.results
    if all_lines:
        groups = [{"rep": r, "members": [r]} for r in results]
        print(f"\nFound {len(results)} valid lines")
    else:
        groups = lines.spot_groups
        shown = {"clearance": "largest clearance", "longest": "longest", "height": "highest"}[group_by]
        print(f"\nFound {len(results)} valid lines in {len(groups)} spots "
              f"(lines with both anchors within {group_distance:g}m are grouped, "
              f"showing the one with the {shown}; use --all-lines to list every line)")
    print("GPS Coordinates (lat, lon):")
    for n, g in enumerate(groups, 1):
        r, members = g["rep"], g["members"]
        flag = "  [over trees/buildings]" if r["over_obstacles"] else ""
        label = "Line" if all_lines else "Spot"
        if r["water_m"]:
            flag += f"  [{r['water_m']:.0f}m over water]"
        if r["filled_m"]:
            flag += f"  [{r['filled_m']:.0f}m over filled gaps]"
        print(f"  {label} {n}: {r['length']:.0f}m, min. clearance {r['clearance']:.1f}m{flag}")
        if len(members) > 1:
            lengths = [m["length"] for m in members]
            print(f"    {len(members)} similar lines, {min(lengths):.0f}-{max(lengths):.0f}m")
        print(f"    Anchor 1: {r['anchor1'][0]:.6f}, {r['anchor1'][1]:.6f}")
        print(f"    Anchor 2: {r['anchor2'][0]:.6f}, {r['anchor2'][1]:.6f}")
    return groups


def main(params: pm.SearchParams, options: pm.CliOptions, paths: search.Paths):
    """One search; a search.SearchError ends it with its message."""
    confirm = confirmer(options.yes)
    try:
        plan = search.plan_search(params, paths, print_event)
        estimate = search.estimate_search(plan)
        if estimate.reasons:
            confirm("expensive", "\n".join(estimate.reasons))
        lines, run = search.search_lines(plan)
        groups = print_lines(lines, plan.group_distance, params.group_by, options.all_lines)
        result = search.save_run(plan, lines, confirm, run)
    except search.SearchError as e:
        sys.exit(str(e))

    html = options.html or os.path.join(paths.data, "findaline_map.html")
    if result.map_path:
        rn.write_latest_redirect(html, result.map_path)
        print(f"Interactive map saved to {os.path.abspath(result.map_path)}")
        print(f"Open {os.path.abspath(html)} in a web browser to see the latest run")
    print(f"All runs: {os.path.abspath(os.path.join(paths.runs, 'index.html'))}")
    if not options.no_plot:
        mp.mapify([g["rep"] for g in groups], plan.area)

    print("\n" + "=" * 60)
    print("Analysis complete!")
    print("=" * 60)


if __name__ == "__main__":
    # Output redirected to a file gets the Windows code page, which has no "²" and the like
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 1 and sys.argv[1] in ("cache", "osm"):
        import update
        sys.exit(update.main(sys.argv[1:]))
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        import web
        sys.exit(web.main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        import jobs
        sys.exit(jobs.main(sys.argv[2:]))
    searches, options = pm.parse_cli(description=DESCRIPTION)
    data_dir = cache.data_dir(options.data_dir)
    stopped = []
    for n, params in enumerate(searches, 1):
        if len(searches) > 1:
            print(f"\n{'#' * 60}\nPoint {n} of {len(searches)}: {params.lat}, {params.lon}"
                  + (f" ({params.name})" if params.name else "") + f"\n{'#' * 60}")
        paths = search.Paths(data_dir, cache.new_work(data_dir))
        failed = True
        try:
            with progress.bars():
                main(params, options, paths)
            failed = False
        except SystemExit as e:  # a stop with a message (no lines found, bad input...) is not a failure
            failed = e.code not in (None, 0) and not isinstance(e.code, str)
            if failed or len(searches) == 1:
                raise
            print(e.code)
            stopped.append(n)
        finally:
            if failed:
                print(f"The working grids are kept in {paths.work} (delete the folder when done)")
            else:
                gc.collect()  # close the memory-mapped grids before deleting them (Windows)
                cache.remove_work(paths.work)
    if stopped:
        sys.exit(f"Stopped without a run: point{'s' if len(stopped) > 1 else ''} "
                 f"{', '.join(map(str, stopped))} of {len(searches)}")
