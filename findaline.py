"""
Find-a-Line - Highline Location Finder
========================================

This application finds locations with significant highline potential by analyzing
elevation data: terrain (NMT/DTM) and surface (NMPT/DSM) models from national
LiDAR services (geoportal.gov.pl, Malta), Europe-wide models (FABDEM, Copernicus),
NASADEM or local files. See sources.py.

Example:
    python findaline.py --lat 50.1559508 --lon 19.7562722

Run `python findaline.py --help` for all options.
"""

import argparse
import dataclasses
import os
import sys
from datetime import datetime

import numpy as np
from pyproj import Transformer

import grid as gd
import helpers as sh
import mapping as mp
import orthophoto as ortho
from progress import progress
import runs as rn
import sag as sg
import sources as src

# Ask before continuing when a run looks this expensive
CONFIRM_DOWNLOAD_MB = 500
CONFIRM_DISK_GB = 4
CONFIRM_MINUTES = 30
ORTHO_SECONDS_PER_TILE = 0.3  # measured download rate with orthophoto.DOWNLOAD_THREADS


def parse_args():
    parser = argparse.ArgumentParser(
        description="Find highline spots using elevation data (geoportal.gov.pl, Malta, FABDEM/"
                    "Copernicus, NASADEM or your own files).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    location = parser.add_argument_group(
        "search centre (give either --lat/--lon or --northing/--easting)")
    location.add_argument("--lat", type=float, help="latitude, WGS84 (e.g. 49.2320)")
    location.add_argument("--lon", type=float, help="longitude, WGS84 (e.g. 19.9817)")
    location.add_argument("--northing", type=float,
                          help="PL-1992 (EPSG:2180) northing in metres; geoportal shows it as X")
    location.add_argument("--easting", type=float,
                          help="PL-1992 (EPSG:2180) easting in metres; geoportal shows it as Y")

    data = parser.add_argument_group("elevation data")
    data.add_argument("--source", default="auto", choices=["auto", "files"] + list(src.SOURCES),
                      help="data source; 'auto' picks the national source for the country of "
                           "the centre (Poland, Malta) or the Europe-wide model elsewhere")
    data.add_argument("--elevation-file",
                      help="your own terrain model (GeoTIFF/ASC, e.g. NASADEM from OpenTopography); "
                           "implies --source files")
    data.add_argument("--surface-file",
                      help="your own surface model with trees/buildings, used with --elevation-file")
    data.add_argument("--opentopo-key", default=os.environ.get("OPENTOPOGRAPHY_API_KEY"),
                      help="OpenTopography API key for --source nasadem "
                           "(default: the OPENTOPOGRAPHY_API_KEY environment variable)")

    search = parser.add_argument_group("search")
    search.add_argument("--radius", type=float, default=1000,
                        help="search radius around the centre in metres")
    search.add_argument("--resolution", type=float,
                        help="working resolution in metres (data is resampled to it); "
                             "default: 2 for national LiDAR sources, 30 for Europe-wide/NASADEM, "
                             "the file resolution for --elevation-file")
    search.add_argument("--min-length", type=float, default=30, help="minimum line length in metres")
    search.add_argument("--max-length", type=float, default=1000, help="maximum line length in metres")
    search.add_argument("--max-anchor-diff", type=float, default=5,
                        help="maximum height difference of the anchors in %% of the line length")
    search.add_argument("--max-anchor-diff-m", type=float, default=50,
                        help="maximum height difference of the anchors in metres")
    search.add_argument("--slope-threshold", type=float, default=45,
                        help="minimum terrain slope in degrees for a pixel to be an anchor")
    search.add_argument("--slope-file",
                        help="use your own slope raster (degrees, GeoTIFF/ASC) "
                             "instead of computing slope from NMT")
    search.add_argument("--nmpt", choices=["mark", "reject", "off"], default="mark",
                        help="also check clearance over trees/buildings (NMPT): "
                             "'mark' shows such lines in orange, 'reject' drops them, "
                             "'off' skips the check (and the NMPT download)")
    search.add_argument("--processes", type=int,
                        help="number of parallel processes (default: CPU count - 1)")

    rigging = parser.add_argument_group(
        "clearance model (the sag estimator is also shown in the profile of the map)")
    rigging.add_argument("--model", choices=["formula", "sag"], default="formula",
                         help="'formula': maximum elevation formula (see README); "
                              "'sag': sag estimator below the webbing plus --fall-zone")
    rigging.add_argument("--pretension", type=float, default=2.5, help="pretension in kN")
    rigging.add_argument("--stretch", type=float, default=5, help="webbing stretch at 10 kN in %%")
    rigging.add_argument("--webbing-weight", type=float, default=70, help="webbing weight in g/m")
    rigging.add_argument("--athlete-mass", type=float, default=80, help="athlete mass in kg")
    rigging.add_argument("--fall-zone", type=float, default=3,
                         help="height below the sagged webbing that must stay free (leash fall), m")

    output = parser.add_argument_group("data and output")
    output.add_argument("--cache-dir", default="cache", help="folder for downloaded data")
    output.add_argument("--refresh", action="store_true",
                        help="download data again even if it is cached")
    output.add_argument("--group-distance", type=float,
                        help="group similar lines: both anchors within this many metres "
                             "(also the initial setting of the interactive map); "
                             "default: 20, or twice the resolution if that is more")
    output.add_argument("--group-by", choices=["clearance", "longest"], default="clearance",
                        help="line shown for each group: largest clearance or longest")
    output.add_argument("--all-lines", action="store_true",
                        help="list every line instead of one line per group")
    output.add_argument("--runs-dir", default="runs",
                        help="folder where every run (map, CSV, GeoJSON, parameters) is saved")
    output.add_argument("--name",
                        help="name of the run (default: place name from OpenStreetMap)")
    output.add_argument("--html", default="findaline_map.html",
                        help="page that opens the map of the latest run")
    output.add_argument("--no-html", action="store_true", help="do not save the interactive map")
    output.add_argument("--no-ortho-cache", action="store_true",
                        help="do not download orthophoto tiles for offline use of the map")
    output.add_argument("--no-plot", action="store_true", help="do not open the matplotlib map")
    output.add_argument("-y", "--yes", action="store_true",
                        help="do not ask for confirmation before long runs")

    args = parser.parse_args()

    wgs84 = args.lat is not None or args.lon is not None
    pl1992 = args.northing is not None or args.easting is not None
    if wgs84 == pl1992:
        parser.error("give the search centre as either --lat/--lon or --northing/--easting")
    if wgs84 and (args.lat is None or args.lon is None):
        parser.error("both --lat and --lon are required")
    if pl1992 and (args.northing is None or args.easting is None):
        parser.error("both --northing and --easting are required")
    if args.resolution is not None and args.resolution <= 0:
        parser.error("--resolution must be positive")
    for path in (args.elevation_file, args.surface_file):
        if path and not os.path.exists(path):
            parser.error(f"file not found: {path}")
    if args.surface_file and not args.elevation_file:
        parser.error("--surface-file needs --elevation-file")
    if not 0 < args.min_length < args.max_length:
        parser.error("--min-length must be positive and smaller than --max-length")
    if args.radius <= 0:
        parser.error("--radius must be positive")
    if args.group_distance is not None and args.group_distance <= 0:
        parser.error("--group-distance must be positive (use --all-lines to list every line)")
    if args.slope_file and not os.path.exists(args.slope_file):
        parser.error(f"slope file not found: {args.slope_file}")
    if min(args.max_anchor_diff, args.max_anchor_diff_m) < 0:
        parser.error("--max-anchor-diff and --max-anchor-diff-m cannot be negative")
    if min(args.pretension, args.stretch) <= 0 or min(args.webbing_weight, args.athlete_mass,
                                                         args.fall_zone) < 0:
        parser.error("rigging values must be positive")
    if args.nmpt == "off":
        args.nmpt = None
    return args


def confirm(reasons, assume_yes):
    """Ask the user to confirm an expensive run."""
    if not reasons:
        return
    print("\nThis run looks expensive:")
    for reason in reasons:
        print(f"  - {reason}")
    print("Consider a smaller --radius or a coarser --resolution.")
    if assume_yes:
        return
    if not ask("Continue? [y/N] "):
        sys.exit("Stopped. Use --yes to skip this question.")


def ask(question):
    """Ask a yes/no question; anything but yes (or no terminal) means no."""
    try:
        answer = progress.ask(question)
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes", "t", "tak")


def main(args):
    if args.lat is not None:
        center_lat, center_lon = args.lat, args.lon
    else:
        center_lat, center_lon = mp.pl1992_to_wgs84(args.easting, args.northing)

    src.migrate_cache(args.cache_dir)
    try:
        source, reason = src.choose_source(args.source, center_lat, center_lon,
                                           args.elevation_file, args.surface_file)
    except ValueError as e:
        sys.exit(str(e))
    src.OpenTopographyTiles.api_key = args.opentopo_key
    if args.nmpt and source.nmpt is None:
        print(f"Note: {source.name} has no surface model (trees/buildings), NMPT check is off")
        args.nmpt = None
    args.resolution = args.resolution or source.default_resolution
    args.group_distance = args.group_distance or max(20.0, 2 * args.resolution)
    native = source.nmt.native_res
    if isinstance(source.nmt, src.WcsTiles) and source.nmt.fetch_res is None and args.resolution < native:
        sys.exit(f"--resolution cannot be finer than the {source.name} data ({native:g} m)")

    crs = source.working_crs(center_lat, center_lon)
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(center_lon, center_lat)
    grid = gd.make_grid(x, y, args.radius, args.resolution, crs)
    rigging = sg.Rigging(args.pretension, args.stretch, args.webbing_weight,
                         args.athlete_mass, args.fall_zone)
    layers = [source.nmt] + ([source.nmpt] if args.nmpt else [])

    print("=" * 60)
    print("Find-a-Line - Highline Location Finder")
    print("=" * 60)
    print(f"Centre: {center_lat:.6f}, {center_lon:.6f} (WGS84) / "
          f"E {x:.0f}, N {y:.0f} ({crs})")
    print(f"Data: {source.name} ({reason}) - {source.description}")
    if source.note:
        print(f"Note: {source.note}")
    print(f"Radius: {args.radius:.0f}m, resolution: {args.resolution:g}m "
          f"({grid.width} x {grid.height} px)")
    if args.resolution < native * 0.99:
        print(f"Note: the resolution is finer than the data ({native:g} m), which is interpolated")
    print(f"Length constraints: {args.min_length:g}m - {args.max_length:g}m")
    print(f"Anchor height difference: at most {args.max_anchor_diff:g}% of the length "
          f"and {args.max_anchor_diff_m:g}m")
    print(f"Slope threshold: {args.slope_threshold:g} degrees")
    print(f"NMPT (trees/buildings): {args.nmpt or 'not used'}")
    if args.model == "sag":
        print(f"Clearance model: sag estimator ({args.pretension:g} kN, {args.stretch:g}% @ 10 kN, "
              f"{args.webbing_weight:g} g/m, athlete {args.athlete_mass:g} kg, "
              f"fall zone {args.fall_zone:g} m)")
    else:
        print("Clearance model: maximum elevation formula")
    print("=" * 60)

    # Warn before large downloads / disk use
    reasons = []
    download_mb = 0.0
    for layer in layers:
        n_tiles, n_missing, mb = layer.estimate(grid, args.cache_dir, args.refresh)
        download_mb += mb
        print(f"{layer.description}: {n_tiles} tiles, {n_missing} to download (~{mb:.0f} MB)")
    work_gb = grid.width * grid.height * 4 * (len(layers) + bool(args.slope_file)) / 1e9
    if download_mb > CONFIRM_DOWNLOAD_MB:
        reasons.append(f"about {download_mb:.0f} MB to download ({source.name})")
    if work_gb > CONFIRM_DISK_GB:
        reasons.append(f"working grids take about {work_gb:.1f} GB of disk in {args.cache_dir}")
    confirm(reasons, args.yes)

    # Step 1: Download elevation data
    print(f"\nStep 1: Downloading elevation data ({source.name})...")
    work = os.path.join(args.cache_dir, "work")
    try:
        elevation_path = src.prepare_layer(source.nmt, grid, args.cache_dir,
                                           os.path.join(work, "nmt.npy"), "terrain", args.refresh)
        surface_path = None
        if args.nmpt:
            surface_path = src.prepare_layer(source.nmpt, grid, args.cache_dir,
                                             os.path.join(work, "nmpt.npy"), "surface", args.refresh)
    except RuntimeError as e:
        sys.exit(f"Download failed: {e}")
    elevation = np.load(elevation_path, mmap_mode="r")
    surface = np.load(surface_path, mmap_mode="r") if surface_path else None

    # Step 2: Find raster pixels with significant slope for promising anchors
    print("\nStep 2: Finding significant slopes...")
    slope = None
    if args.slope_file:
        slope_path = src.load_slope_file(args.slope_file, grid,
                                        os.path.join(args.cache_dir, "work", "slope.npy"))
        slope = np.load(slope_path, mmap_mode="r")
    anchors = sh.find_significant_slopes(elevation, args.resolution, args.slope_threshold,
                                         (grid.center_row, grid.center_col), grid.radius_px, slope)
    print(f"Found {len(anchors)} potential anchors to evaluate")
    if len(anchors) < 2:
        sys.exit("Not enough steep terrain. Try a lower --slope-threshold or a larger --radius.")
    anchors_path = os.path.join(args.cache_dir, "work", "anchors.npy")
    np.save(anchors_path, anchors)

    # Step 3: Compare each promising anchor to every other anchor
    print("\nStep 3: Evaluating anchor pairs...")
    num_processes = args.processes or max(1, os.cpu_count() - 1)
    sag_table = None
    if args.model == "sag":
        print("Precomputing the sag estimator for all line lengths...")
        sag_table = sg.EnvelopeTable(rigging, args.min_length, args.max_length)
    max_dh_ratio = args.max_anchor_diff / 100
    pairs, seconds_per_pair = sh.estimate_search(anchors, elevation, args.resolution,
                                                 args.min_length, args.max_length, surface,
                                                 sag_table, max_dh_ratio, args.max_anchor_diff_m)
    minutes = pairs * seconds_per_pair / num_processes / 60
    print(f"Estimated {pairs:,.0f} anchor pairs within length constraints, "
          f"~{minutes:.0f} min on {num_processes} processes")
    if pairs == 0:
        sys.exit("No anchor pairs found within specified length constraints.")
    if minutes > CONFIRM_MINUTES:
        confirm([f"about {pairs:,.0f} anchor pairs to evaluate, ~{minutes:.0f} minutes"], args.yes)

    valid_lines = sh.find_valid_lines(elevation_path, surface_path, anchors_path, len(anchors),
                                      args.resolution, args.min_length, args.max_length,
                                      args.nmpt, num_processes, sag_table,
                                      max_dh_ratio, args.max_anchor_diff_m)
    if not valid_lines:
        sys.exit("No lines found matching elevation requirements.")

    # Step 4: Convert pixel coordinates to GPS coordinates
    print("\nStep 4: Converting pixel coordinates to GPS coordinates...")
    results = mp.coordify(valid_lines, grid, args.nmpt)
    if not args.no_html:
        progress.compute.start("line profiles", len(results), unit="line")
        for r, line in zip(results, valid_lines):
            r["profile"] = sh.line_profile(line.a1, line.a2, elevation, args.resolution, surface,
                                           sag_table=sag_table)
            progress.compute.update(1)
        progress.compute.finish()
    spot_groups = mp.group_lines(results, args.group_distance, args.group_by)
    if args.all_lines:
        groups = [{"rep": r, "members": [r]} for r in results]
        print(f"\nFound {len(results)} valid lines")
    else:
        groups = spot_groups
        shown = "largest clearance" if args.group_by == "clearance" else "longest"
        print(f"\nFound {len(results)} valid lines in {len(groups)} spots "
              f"(lines with both anchors within {args.group_distance:g}m are grouped, "
              f"showing the one with the {shown}; use --all-lines to list every line)")
    print("GPS Coordinates (lat, lon):")
    for n, g in enumerate(groups, 1):
        r, members = g["rep"], g["members"]
        flag = "  [over trees/buildings]" if r["over_obstacles"] else ""
        label = "Line" if args.all_lines else "Spot"
        print(f"  {label} {n}: {r['length']:.0f}m, min. clearance {r['clearance']:.1f}m{flag}")
        if len(members) > 1:
            lengths = [m["length"] for m in members]
            print(f"    {len(members)} similar lines, {min(lengths):.0f}-{max(lengths):.0f}m")
        print(f"    Anchor 1: {r['anchor1'][0]:.6f}, {r['anchor1'][1]:.6f}")
        print(f"    Anchor 2: {r['anchor2'][0]:.6f}, {r['anchor2'][1]:.6f}")

    # Step 5: Save the run and display results on a map
    print("\nStep 5: Saving the run and displaying results on map...")
    created = datetime.now()
    name = args.name or rn.place_name(center_lat, center_lon) or f"{center_lat:.5f}, {center_lon:.5f}"
    run = rn.new_run(args.runs_dir, name, created)
    meta = {
        "id": run["id"],
        "name": name,
        "created": created.isoformat(timespec="seconds"),
        "center": {"lat": center_lat, "lon": center_lon, "x": x, "y": y, "crs": crs},
        "source": {"id": source.id, "name": source.name, "description": source.description},
        "params": {"radius": args.radius, "resolution": args.resolution,
                   "min_length": args.min_length, "max_length": args.max_length,
                   "max_anchor_diff": args.max_anchor_diff,
                   "max_anchor_diff_m": args.max_anchor_diff_m,
                   "slope_threshold": args.slope_threshold, "slope_file": args.slope_file,
                   "nmpt": args.nmpt, "group_distance": args.group_distance,
                   "group_by": args.group_by, "model": args.model,
                   "rigging": dataclasses.asdict(rigging)},
        "summary": {"lines": len(results), "spots": len(spot_groups),
                    "clear_spots": sum(not g["rep"]["over_obstacles"] for g in spot_groups)},
    }
    meta["description"] = rn.params_summary(meta)
    rn.save_results(run["path"], spot_groups, meta)
    print(f"Run \"{name}\" saved to {os.path.abspath(run['path'])}")

    if not args.no_html:
        ortho_cache = None
        if source.ortho and not args.no_ortho_cache:
            # Detailed orthophoto only around one line per group (variants lie within the strip)
            spots = [g["rep"] for g in spot_groups]
            ortho_folder = ortho.cache_folder(args.cache_dir, source.ortho)
            tiles = ortho.missing_tiles(ortho.tiles_to_cache(spots, center_lat, center_lon,
                                                             args.radius), ortho_folder)
            minutes = len(tiles) * ORTHO_SECONDS_PER_TILE / 60
            print(f"Orthophoto for offline use: {len(tiles)} tiles to download "
                  f"(~{len(tiles) * ortho.APPROX_TILE_KB / 1000:.0f} MB, ~{minutes:.0f} min)")
            if minutes > CONFIRM_MINUTES and not args.yes and not ask(
                    "Download now? Otherwise the map loads the orthophoto online. [y/N] "):
                tiles = []
            ortho.download(source.ortho, tiles, ortho_folder)
            ortho_cache = ortho_folder
        map_path = os.path.join(run["path"], "map.html")
        mp.save_html_map(results, center_lat, center_lon, args.radius, map_path,
                         args.group_distance, args.group_by, ortho_cache, source.ortho, meta,
                         args.runs_dir, args.model, rigging)
        rn.write_latest_redirect(args.html, map_path)
        print(f"Interactive map saved to {os.path.abspath(map_path)}")
        print(f"Open {os.path.abspath(args.html)} in a web browser to see the latest run")
    rn.write_index(args.runs_dir)
    print(f"All runs: {os.path.abspath(os.path.join(args.runs_dir, 'index.html'))}")
    if not args.no_plot:
        mp.mapify([g["rep"] for g in groups], center_lat, center_lon, args.radius)

    print("\n" + "=" * 60)
    print("Analysis complete!")
    print("=" * 60)


if __name__ == "__main__":
    with progress.bars():
        main(parse_args())
