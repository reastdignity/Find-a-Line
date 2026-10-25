"""
Converting results to GPS coordinates and displaying them on maps.
"""

import dataclasses
import html
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from pyproj import Transformer

from grid import Grid
from helpers import LEASH_FALL, SLOPE, Line
from progress import progress
from sag import Rigging

MAX_ZOOM = 21  # about 5 cm per pixel, the detail of the best geoportal orthophotos

COLOR_VALID = "#FF10F0"     # neon pink
COLOR_OBSTACLE = "#FF8C00"  # orange: line passes too close to trees/buildings (NMPT)

PL1992 = "EPSG:2180"  # Polish national grid, accepted by --northing/--easting
_to_wgs84 = Transformer.from_crs(PL1992, "EPSG:4326", always_xy=True)


def pl1992_to_wgs84(easting: float, northing: float):
    """Return (lat, lon)."""
    lon, lat = _to_wgs84.transform(easting, northing)
    return lat, lon


def coordify(lines: List[Line], grid: Grid, nmpt_mode: Optional[str]) -> List[Dict]:
    """Convert pixel coordinates of each line to GPS coordinates (lat, lon).

    Returns:
        One dict per line with anchor coordinates and line statistics
    """
    if not lines:
        return []
    rows = np.array([[line.a1[0], line.a2[0]] for line in lines]).ravel()
    cols = np.array([[line.a1[1], line.a2[1]] for line in lines]).ravel()
    easting, northing = grid.pixel_to_xy(rows, cols)
    lon, lat = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True).transform(easting, northing)

    results = []
    for k, line in enumerate(lines):
        over_obstacles = (nmpt_mode == "mark" and line.clearance_nmpt is not None
                          and line.clearance_nmpt <= 0)
        results.append({
            "anchor1": (float(lat[2 * k]), float(lon[2 * k])),
            "anchor2": (float(lat[2 * k + 1]), float(lon[2 * k + 1])),
            # (easting, northing) in metres in the grid CRS, used for grouping
            "a1_xy": (float(easting[2 * k]), float(northing[2 * k])),
            "a2_xy": (float(easting[2 * k + 1]), float(northing[2 * k + 1])),
            "length": line.length,
            "elev_a1": line.elev_a1,
            "elev_a2": line.elev_a2,
            "clearance": line.clearance,
            "clearance_nmpt": line.clearance_nmpt,
            "over_obstacles": over_obstacles,
        })
    return results


def _rank(by: str):
    """Sort key: best representative first (with reverse=True)."""
    if by == "longest":
        return lambda r: (r["length"], r["clearance"])
    return lambda r: (r["clearance"], r["length"])


def group_lines(results: List[Dict], distance: float, by: str = "clearance") -> List[Dict]:
    """Group near-identical lines (both anchors within `distance` metres).

    Greedy grouping around a representative: the best remaining line (by length
    or clearance) takes every remaining line whose anchors are close to its own,
    so no member is further than `distance` from the representative. Lines over
    trees/buildings are grouped separately from clear lines.
    The same algorithm runs in the interactive map (viewer/src/grouping.ts).

    Returns:
        Groups, clear lines first and best first: {"rep": result, "members": [results]}
    """
    rank = _rank(by)
    d2 = distance ** 2
    groups = []
    progress.compute.start("grouping lines", len(results), unit="line")
    for over in (False, True):
        lines = sorted((r for r in results if r["over_obstacles"] == over), key=rank, reverse=True)
        if not lines:
            continue
        a1 = np.array([r["a1_xy"] for r in lines])
        a2 = np.array([r["a2_xy"] for r in lines])
        free = np.ones(len(lines), dtype=bool)
        # Lines by the cell (of size `distance`) of their first anchor: a member's first
        # anchor lies within `distance` of one of the representative's anchors, so in
        # one of the 3 x 3 cells around it
        cells: Dict[Tuple[int, int], List[int]] = {}
        for k, cell in enumerate(map(tuple, np.floor(a1 / distance).astype(np.int64).tolist())):
            cells.setdefault(cell, []).append(k)

        def near(point) -> List[int]:
            cx, cy = (int(v) for v in np.floor(point / distance))
            return [k for dx in (-1, 0, 1) for dy in (-1, 0, 1) for k in cells.get((cx + dx, cy + dy), ())]

        for i in range(len(lines)):
            if not free[i]:
                continue
            candidates = np.unique(np.array(near(a1[i]) + near(a2[i]), dtype=np.int64))
            candidates = candidates[free[candidates]]
            c1, c2 = a1[candidates], a2[candidates]
            same = (((c1 - a1[i]) ** 2).sum(1) <= d2) & (((c2 - a2[i]) ** 2).sum(1) <= d2)
            flipped = (((c1 - a2[i]) ** 2).sum(1) <= d2) & (((c2 - a1[i]) ** 2).sum(1) <= d2)
            members = candidates[same | flipped]
            free[members] = False
            groups.append({"rep": lines[i], "members": [lines[k] for k in members]})
            progress.compute.update(len(members), spots=len(groups))
    progress.compute.finish(f"{len(groups)} spots")
    # Clear lines first, then lines over trees/buildings; best first within each
    groups.sort(key=lambda g: (not g["rep"]["over_obstacles"], rank(g["rep"])), reverse=True)
    return groups


def _search_area(area):
    """The search area (area.Area) as a polygon in WGS84 (geopandas)."""
    import geopandas as gpd

    return gpd.GeoSeries([area.shape_wgs84()], crs="EPSG:4326")


def mapify(results: List[Dict], area):
    """Visualize lines on a static map (matplotlib + OpenTopoMap)."""
    import contextily as ctx
    import geopandas as gpd
    import matplotlib.pyplot as plt
    from shapely.geometry import LineString

    if not results:
        print("No lines to display.")
        return

    lines = [LineString([(r["anchor1"][1], r["anchor1"][0]), (r["anchor2"][1], r["anchor2"][0])])
             for r in results]
    colors = [COLOR_OBSTACLE if r["over_obstacles"] else COLOR_VALID for r in results]
    gdf = gpd.GeoDataFrame({"color": colors}, geometry=lines, crs="EPSG:4326")

    fig, ax = plt.subplots(figsize=(10, 10))
    _search_area(area).boundary.plot(
        ax=ax, color="black", linestyle="--", linewidth=0.8)
    gdf.plot(ax=ax, color=gdf["color"], linewidth=0.8, alpha=0.9)

    # Show circles at line midpoints
    midpoints = gpd.GeoDataFrame({"color": colors}, crs="EPSG:4326",
                                 geometry=[line.interpolate(0.5, normalized=True) for line in lines])
    midpoints.plot(ax=ax, color=midpoints["color"], markersize=100, marker="o", alpha=0.6, zorder=3)

    ctx.add_basemap(ax, crs=gdf.crs, source=ctx.providers.OpenTopoMap)
    plt.show()


VIEWER_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer", "dist")
VIEWER_FOLDER = "_viewer"  # in runs/: the viewer bundle shared by every map


def _relative_url(folder: str, html_path: str) -> str:
    """URL of a folder as seen from the HTML file (relative when possible)."""
    try:
        return os.path.relpath(folder, os.path.dirname(os.path.abspath(html_path))).replace(os.sep, "/")
    except ValueError:  # different drive on Windows
        return Path(os.path.abspath(folder)).as_uri()


def install_viewer(runs_dir: str):
    """Copy the viewer bundle (viewer/dist, built from viewer/src) into runs/_viewer when it changed."""
    target = os.path.join(runs_dir, VIEWER_FOLDER)
    os.makedirs(target, exist_ok=True)
    for name in os.listdir(VIEWER_DIST):
        source, copy = os.path.join(VIEWER_DIST, name), os.path.join(target, name)
        with open(source, "rb") as f:
            content = f.read()
        if not os.path.exists(copy) or open(copy, "rb").read() != content:
            with open(copy + ".part", "wb") as f:
                f.write(content)
            os.replace(copy + ".part", copy)


MAP_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - Find-a-Line</title>
<link rel="stylesheet" href="../{viewer}/viewer.css">
</head><body><div id="map"></div>
<script src="data.js"></script>
<script src="../runs.js"></script>
<script src="../{viewer}/viewer.js"></script>
</body></html>
"""


def save_map(results: List[Dict], area, run_path: str, runs_dir: str, run: Dict,
             group_distance: float = 20, group_by: str = "clearance", ortho_cache: Optional[str] = None,
             ortho_service=None, model: str = "formula", rigging: Optional[Rigging] = None) -> str:
    """Save the interactive map of a run: map.html and data.js in its folder.

    The map is the viewer (viewer/src, TypeScript), the same bundle for every run in
    runs/_viewer; data.js holds the run's lines with their profiles
    (window.FINDALINE_RUN, read by viewer/src/types.ts). Lines are grouped in the
    browser like group_lines. Orthophoto tiles are read from ortho_cache (a
    {z}/{x}/{y}.jpg folder) when present there, and from the WMS of ortho_service
    (a sources.OrthoService) otherwise. Returns the path of map.html.
    """
    install_viewer(runs_dir)
    path = os.path.join(run_path, "map.html")
    data = {
        "run": {"id": run["id"], "name": run["name"], "created": run["created"],
                "description": run.get("description", "")},
        "center": list(area.center),
        "area": area.shape_wgs84().__geo_interface__,
        "ortho": None if ortho_service is None else {
            "name": ortho_service.name, "url": ortho_service.url, "layer": ortho_service.layer,
            "attribution": ortho_service.attribution,
            "cache": _relative_url(ortho_cache, path) if ortho_cache else None},
        "distance": group_distance, "by": group_by, "model": model,
        "rigging": dataclasses.asdict(rigging or Rigging()),
        "slope": SLOPE, "leash_fall": LEASH_FALL, "max_zoom": MAX_ZOOM,
        # Values used for grouping are not rounded, so the browser groups exactly like group_lines()
        "lines": [[round(r["anchor1"][0], 7), round(r["anchor1"][1], 7),
                   round(r["anchor2"][0], 7), round(r["anchor2"][1], 7),
                   *r["a1_xy"], *r["a2_xy"], r["length"], r["clearance"],
                   None if r["clearance_nmpt"] is None else round(r["clearance_nmpt"], 2),
                   round(r["elev_a1"], 2), round(r["elev_a2"], 2), r["over_obstacles"], r.get("profile")]
                  for r in results],
    }
    with open(os.path.join(run_path, "data.js"), "w", encoding="utf-8") as f:
        f.write("window.FINDALINE_RUN = ")
        json.dump(data, f, separators=(",", ":"))
        f.write(";\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(MAP_HTML.format(title=html.escape(run["name"]), viewer=VIEWER_FOLDER))
    return path
