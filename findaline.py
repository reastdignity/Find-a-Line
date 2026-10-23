"""
Find-a-Line - Highline Location Finder
========================================

This application finds locations with significant highline potential by analyzing
elevation data (NMT / NMPT) downloaded from geoportal.gov.pl.

Example:
    python findaline.py --lat 49.2320 --lon 19.9817 --radius 2000

Run `python findaline.py --help` for all options.
"""

import argparse
import os
import sys

import numpy as np

import geoportal as gp
import helpers as sh
import mapping as mp

# Ask before continuing when a run looks this expensive
CONFIRM_DOWNLOAD_MB = 500
CONFIRM_DISK_GB = 4
CONFIRM_MINUTES = 30


def parse_args():
    parser = argparse.ArgumentParser(
        description="Find highline spots using elevation data from geoportal.gov.pl.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    location = parser.add_argument_group(
        "search centre (give either --lat/--lon or --northing/--easting)")
    location.add_argument("--lat", type=float, help="latitude, WGS84 (e.g. 49.2320)")
    location.add_argument("--lon", type=float, help="longitude, WGS84 (e.g. 19.9817)")
    location.add_argument("--northing", type=float,
                          help="PL-1992 (EPSG:2180) northing in metres; geoportal shows it as X")
    location.add_argument("--easting", type=float,
                          help="PL-1992 (EPSG:2180) easting in metres; geoportal shows it as Y")

    search = parser.add_argument_group("search")
    search.add_argument("--radius", type=float, default=20000,
                        help="search radius around the centre in metres")
    search.add_argument("--resolution", type=float, default=2,
                        help="working resolution in metres (data is downsampled to it); min 1")
    search.add_argument("--min-length", type=float, default=30, help="minimum line length in metres")
    search.add_argument("--max-length", type=float, default=1000, help="maximum line length in metres")
    search.add_argument("--slope-threshold", type=float, default=30,
                        help="minimum terrain slope in degrees for a pixel to be an anchor")
    search.add_argument("--slope-file",
                        help="use your own slope raster (degrees, GeoTIFF/ASC) "
                             "instead of computing slope from NMT")
    search.add_argument("--nmpt", choices=["mark", "reject"],
                        help="also check clearance over trees/buildings (NMPT): "
                             "'mark' shows such lines in orange, 'reject' drops them")
    search.add_argument("--processes", type=int,
                        help="number of parallel processes (default: CPU count - 1)")

    output = parser.add_argument_group("data and output")
    output.add_argument("--cache-dir", default="cache", help="folder for downloaded data")
    output.add_argument("--refresh", action="store_true",
                        help="download data again even if it is cached")
    output.add_argument("--group-distance", type=float, default=20,
                        help="group similar lines: both anchors within this many metres "
                             "(also the initial setting of the interactive map)")
    output.add_argument("--group-by", choices=["clearance", "longest"], default="clearance",
                        help="line shown for each group: largest clearance or longest")
    output.add_argument("--all-lines", action="store_true",
                        help="list every line instead of one line per group")
    output.add_argument("--html", default="findaline_map.html", help="interactive map file")
    output.add_argument("--no-html", action="store_true", help="do not save the interactive map")
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
    if args.resolution < gp.LAYERS["nmt"]["native_res"]:
        parser.error("--resolution cannot be finer than the NMT resolution (1 m)")
    if not 0 < args.min_length < args.max_length:
        parser.error("--min-length must be positive and smaller than --max-length")
    if args.radius <= 0:
        parser.error("--radius must be positive")
    if args.group_distance <= 0:
        parser.error("--group-distance must be positive (use --all-lines to list every line)")
    if args.slope_file and not os.path.exists(args.slope_file):
        parser.error(f"slope file not found: {args.slope_file}")
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
    try:
        answer = input("Continue? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes", "t", "tak"):
        sys.exit("Stopped. Use --yes to skip this question.")


if __name__ == "__main__":
    args = parse_args()

    if args.lat is not None:
        center_lat, center_lon = args.lat, args.lon
        easting, northing = mp.wgs84_to_pl1992(args.lat, args.lon)
    else:
        easting, northing = args.easting, args.northing
        center_lat, center_lon = mp.pl1992_to_wgs84(easting, northing)

    grid = gp.make_grid(easting, northing, args.radius, args.resolution)
    layers = ["nmt"] + (["nmpt"] if args.nmpt else [])

    print("=" * 60)
    print("Find-a-Line - Highline Location Finder")
    print("=" * 60)
    print(f"Centre: {center_lat:.6f}, {center_lon:.6f} (WGS84) / "
          f"N {northing:.0f}, E {easting:.0f} (PL-1992)")
    print(f"Radius: {args.radius:.0f}m, resolution: {args.resolution:g}m "
          f"({grid.width} x {grid.height} px)")
    print(f"Length constraints: {args.min_length:g}m - {args.max_length:g}m")
    print(f"Slope threshold: {args.slope_threshold:g} degrees")
    print(f"NMPT (trees/buildings): {args.nmpt or 'not used'}")
    print("=" * 60)

    # Warn before large downloads / disk use
    reasons = []
    download_mb = 0.0
    for layer in layers:
        tiles = gp.tiles_for_circle(layer, easting, northing, args.radius, args.resolution)
        n_missing, mb = gp.estimate_download(layer, tiles, args.cache_dir, args.resolution, args.refresh)
        download_mb += mb
        print(f"{gp.LAYERS[layer]['description']}: {len(tiles)} tiles, {n_missing} to download (~{mb:.0f} MB)")
    work_gb = grid.width * grid.height * 4 * (len(layers) + bool(args.slope_file)) / 1e9
    if download_mb > CONFIRM_DOWNLOAD_MB:
        reasons.append(f"about {download_mb:.0f} MB to download from geoportal.gov.pl")
    if work_gb > CONFIRM_DISK_GB:
        reasons.append(f"working grids take about {work_gb:.1f} GB of disk in {args.cache_dir}")
    confirm(reasons, args.yes)

    # Step 1: Download elevation data
    print("\nStep 1: Downloading elevation data from geoportal.gov.pl...")
    elevation_path = gp.prepare_layer("nmt", easting, northing, args.radius, grid,
                                      args.cache_dir, args.refresh)
    surface_path = None
    if args.nmpt:
        surface_path = gp.prepare_layer("nmpt", easting, northing, args.radius, grid,
                                        args.cache_dir, args.refresh)
    elevation = np.load(elevation_path, mmap_mode="r")
    surface = np.load(surface_path, mmap_mode="r") if surface_path else None

    # Step 2: Find raster pixels with significant slope for promising anchors
    print("\nStep 2: Finding significant slopes...")
    slope = None
    if args.slope_file:
        slope_path = gp.load_slope_file(args.slope_file, grid,
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
    pairs, seconds_per_pair = sh.estimate_search(anchors, elevation, args.resolution,
                                                 args.min_length, args.max_length, surface)
    minutes = pairs * seconds_per_pair / num_processes / 60
    print(f"Estimated {pairs:,.0f} anchor pairs within length constraints, "
          f"~{minutes:.0f} min on {num_processes} processes")
    if pairs == 0:
        sys.exit("No anchor pairs found within specified length constraints.")
    if minutes > CONFIRM_MINUTES:
        confirm([f"about {pairs:,.0f} anchor pairs to evaluate, ~{minutes:.0f} minutes"], args.yes)

    valid_lines = sh.find_valid_lines(elevation_path, surface_path, anchors_path, len(anchors),
                                      args.resolution, args.min_length, args.max_length,
                                      args.nmpt, num_processes)
    if not valid_lines:
        sys.exit("No lines found matching elevation requirements.")

    # Step 4: Convert pixel coordinates to GPS coordinates
    print("\nStep 4: Converting pixel coordinates to GPS coordinates...")
    results = mp.coordify(valid_lines, grid, args.nmpt)
    if args.all_lines:
        groups = [{"rep": r, "members": [r]} for r in results]
        print(f"\nFound {len(results)} valid lines")
    else:
        groups = mp.group_lines(results, args.group_distance, args.group_by)
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

    # Step 5: Display results on a map
    print("\nStep 5: Displaying results on map...")
    if not args.no_html:
        mp.save_html_map(results, center_lat, center_lon, args.radius, args.html,
                         args.group_distance, args.group_by)
        print(f"Interactive map saved to {os.path.abspath(args.html)} (open it in a web browser)")
    if not args.no_plot:
        mp.mapify([g["rep"] for g in groups], center_lat, center_lon, args.radius)

    print("\n" + "=" * 60)
    print("Analysis complete!")
    print("=" * 60)
