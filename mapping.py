"""
Converting results to GPS coordinates and displaying them on maps.
"""

import dataclasses
import html
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from pyproj import Transformer

import cache
from grid import Grid
from helpers import HIGH_ABOVE, LEASH_FALL, SLOPE, LineTable
from progress import progress
from runs import rounded
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


def coordify(lines: LineTable, grid: Grid, nmpt_mode: Optional[str]) -> LineTable:
    """Add the coordinates of the anchors to the lines (in place): "anchor1", "anchor2" (lat, lon),
    "a1_xy", "a2_xy" (easting, northing in metres of the grid CRS, used for grouping) and
    "over_obstacles" (the line passes over trees/buildings with --nmpt mark). Returns the lines."""
    c = lines.columns
    rows = np.column_stack([c["a1"][:, 0], c["a2"][:, 0]]).ravel()
    cols = np.column_stack([c["a1"][:, 1], c["a2"][:, 1]]).ravel()
    progress.compute.start("coordinates", len(lines), unit="line")
    if len(lines):
        easting, northing = grid.pixel_to_xy(rows, cols)
        lon, lat = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True).transform(easting, northing)
    else:
        easting = northing = lon = lat = np.zeros(0)
    pair = lambda a, b, first: np.column_stack([np.asarray(a, dtype=np.float64)[first::2],
                                                np.asarray(b, dtype=np.float64)[first::2]])
    c["anchor1"], c["anchor2"] = pair(lat, lon, 0), pair(lat, lon, 1)
    c["a1_xy"], c["a2_xy"] = pair(easting, northing, 0), pair(easting, northing, 1)
    c["over_obstacles"] = (c["clearance_nmpt"] <= 0) if nmpt_mode == "mark" else np.zeros(len(lines), dtype=bool)
    progress.compute.finish()
    return lines


def first_anchor(lines: LineTable) -> np.ndarray:
    """(x, y) of the first anchor of every line (the smaller x, then y): the one that decides
    which tile of a large area a line belongs to."""
    a1, a2 = lines.columns["a1_xy"], lines.columns["a2_xy"]
    first = (a1[:, 0] < a2[:, 0]) | ((a1[:, 0] == a2[:, 0]) & (a1[:, 1] <= a2[:, 1]))  # min() of the tuples
    return np.where(first[:, None], a1, a2)


@dataclass
class Groups:
    """Groups of near-identical lines ("spots") of a LineTable, clear lines first and best first:
    group g has the representative row rep[g] and the member rows members[start[g]:start[g + 1]]
    (the representative among them)."""
    rep: np.ndarray
    members: np.ndarray
    start: np.ndarray

    def __len__(self) -> int:
        return len(self.rep)

    def of(self, g: int) -> np.ndarray:
        return self.members[self.start[g]:self.start[g + 1]]

    def sizes(self) -> np.ndarray:
        return np.diff(self.start)

    @classmethod
    def each_alone(cls, n: int) -> "Groups":
        """Every line a group of its own (--all-lines)."""
        rows = np.arange(n, dtype=np.int64)
        return cls(rows, rows, np.arange(n + 1, dtype=np.int64))


def _rank(lines: LineTable, by: str) -> Tuple[np.ndarray, np.ndarray]:
    """The columns that order the lines, best representative first (both descending)."""
    c = lines.columns
    if by == "longest":
        return c["length"], c["clearance"]
    if by == "height":
        return c["height_max"], c["clearance"]
    return c["clearance"], c["length"]


def group_lines(lines: LineTable, distance: float, by: str = "clearance") -> Groups:
    """Group near-identical lines (both anchors within `distance` metres).

    Greedy grouping around a representative: the best remaining line (by length
    or clearance) takes every remaining line whose anchors are close to its own,
    so no member is further than `distance` from the representative. Lines over
    trees/buildings are grouped separately from clear lines.
    The same algorithm runs in the interactive map (viewer/src/grouping.ts); lines that
    rank the same keep their order (a stable sort, as sorted(reverse=True) there).
    """
    first, second = _rank(lines, by)
    over_all = lines.columns["over_obstacles"]
    d2 = distance ** 2
    reps: List[int] = []
    members: List[np.ndarray] = []
    progress.compute.start("grouping lines", len(lines), unit="line")
    for over in (False, True):
        rows = np.flatnonzero(over_all == over)
        if not len(rows):
            continue
        rows = rows[np.lexsort((-second[rows], -first[rows]))]
        a1 = lines.columns["a1_xy"][rows]
        a2 = lines.columns["a2_xy"][rows]
        free = np.ones(len(rows), dtype=bool)
        # Lines by the cell (of size `distance`) of their first anchor: a member's first
        # anchor lies within `distance` of one of the representative's anchors, so in
        # one of the 3 x 3 cells around it
        cells: Dict[Tuple[int, int], List[int]] = {}
        for k, cell in enumerate(map(tuple, np.floor(a1 / distance).astype(np.int64).tolist())):
            cells.setdefault(cell, []).append(k)

        def near(point) -> List[int]:
            cx, cy = (int(v) for v in np.floor(point / distance))
            return [k for dx in (-1, 0, 1) for dy in (-1, 0, 1) for k in cells.get((cx + dx, cy + dy), ())]

        for i in range(len(rows)):
            if not free[i]:
                continue
            candidates = np.unique(np.array(near(a1[i]) + near(a2[i]), dtype=np.int64))
            candidates = candidates[free[candidates]]
            c1, c2 = a1[candidates], a2[candidates]
            same = (((c1 - a1[i]) ** 2).sum(1) <= d2) & (((c2 - a2[i]) ** 2).sum(1) <= d2)
            flipped = (((c1 - a2[i]) ** 2).sum(1) <= d2) & (((c2 - a1[i]) ** 2).sum(1) <= d2)
            found = candidates[same | flipped]
            free[found] = False
            reps.append(int(rows[i]))
            members.append(rows[found])
            progress.compute.update(len(found), spots=len(reps))
    progress.compute.finish(f"{len(reps)} spots")
    # Clear lines first, then lines over trees/buildings; best first within each
    rep = np.array(reps, dtype=np.int64)
    order = np.lexsort((-second[rep], -first[rep], over_all[rep])) if len(rep) else np.zeros(0, dtype=np.int64)
    members = [members[g] for g in order.tolist()]
    start = np.concatenate([[0], np.cumsum([len(m) for m in members], dtype=np.int64)]).astype(np.int64)
    return Groups(rep[order], np.concatenate(members).astype(np.int64) if members else np.zeros(0, dtype=np.int64),
                  start)


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
            with cache.replacing(copy) as f:
                f.write(content)


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


LINES_CHUNK = 10_000  # lines written between two progress updates


def _script_head(target: str, value: Dict) -> str:
    """The start of a script that assigns value (pages opened from disk cannot fetch JSON) with
    its last key "lines" still open: the text of json.dump of the whole value up to its first line."""
    assert list(value)[-1] == "lines"
    head = json.JSONEncoder(separators=(",", ":")).encode({k: v for k, v in value.items() if k != "lines"})
    return f"{target} = {head[:-1]},\"lines\":["


def map_rows(lines: LineTable, start: int, stop: int, profiles: bool) -> List[list]:
    """The rows of data.js (with the profile last) or lines.js (without) of lines start..stop
    (viewer/src/types.ts, toLine). Values used for grouping are not rounded, so the browser
    groups exactly like group_lines()."""
    c = {name: column[start:stop].tolist() for name, column in lines.columns.items()}
    rows = []
    for k, (lat1, lon1), (lat2, lon2), a1_xy, a2_xy, length, clearance, nmpt, e1, e2, over, water, filled,             height_max, height_mean, high_pct, angle1, angle2, prom1, prom2, drop1, drop2 in zip(
            range(start, stop), c["anchor1"], c["anchor2"], c["a1_xy"], c["a2_xy"], c["length"], c["clearance"],
            c["clearance_nmpt"], c["elev_a1"], c["elev_a2"], c["over_obstacles"], c["water_m"], c["filled_m"],
            c["height_max"], c["height_mean"], c["high_pct"], c["angle1"], c["angle2"], c["prom1"], c["prom2"],
            c["drop1"], c["drop2"]):
        row = [round(lat1, 7), round(lon1, 7), round(lat2, 7), round(lon2, 7), *a1_xy, *a2_xy, length, clearance,
               rounded(nmpt, 2), round(e1, 2), round(e2, 2), over, round(water, 1), round(filled, 1),
               height_max, round(height_mean, 2), round(high_pct, 1), round(angle1, 1), round(angle2, 1),
               rounded(prom1, 1), rounded(prom2, 1), rounded(drop1, 1), rounded(drop2, 1)]
        if profiles:
            row.append(lines.profiles.get(k))
        rows.append(row)
    return rows


def save_map(lines: LineTable, area, run_path: str, runs_dir: str, run: Dict, crs: str,
             group_distance: float = 20, group_by: str = "clearance", ortho_cache: Optional[str] = None,
             ortho_service=None, model: str = "formula", rigging: Optional[Rigging] = None,
             on_lines=lambda n, file: None, high_above: float = HIGH_ABOVE) -> str:
    """Save the interactive map of a run: map.html, data.js and lines.js in its folder.

    The map is the viewer (viewer/src, TypeScript), the same bundle for every run in
    runs/_viewer; data.js holds the run's lines with their profiles
    (window.FINDALINE_RUN, read by viewer/src/types.ts), lines.js the same lines
    without profiles, which other maps load to show this run with theirs
    (window.FINDALINE_LINES[id]). Lines are grouped in the
    browser like group_lines. Orthophoto tiles are read from ortho_cache (a
    {z}/{x}/{y}.jpg folder) when present there, and from the WMS of ortho_service
    (a sources.OrthoService) otherwise. Both scripts are written together, LINES_CHUNK
    lines at a time, never all rows at once; on_lines(n, file) reports progress after
    each chunk. Returns the path of map.html.
    """
    install_viewer(runs_dir)
    path = os.path.join(run_path, "map.html")
    data = {
        "run": {"id": run["id"], "name": run["name"], "created": run["created"],
                "description": run.get("description", "")},
        "center": list(area.center),
        "area": area.shape_wgs84().__geo_interface__,
        "crs": crs,
        "ortho": None if ortho_service is None else {
            "name": ortho_service.name, "url": ortho_service.url, "layer": ortho_service.layer,
            "attribution": ortho_service.attribution,
            "base": {"url": ortho_service.base_url, "max_zoom": ortho_service.base_max_zoom}
                    if ortho_service.base_url else None,
            "cache": _relative_url(ortho_cache, path) if ortho_cache else None},
        "attribution": run.get("attribution", []),  # credits of the data used (sources.Credit)
        "distance": group_distance, "by": group_by, "model": model,
        "rigging": dataclasses.asdict(rigging or Rigging()),
        "slope": SLOPE, "leash_fall": LEASH_FALL, "high_above": high_above, "max_zoom": MAX_ZOOM,
        "lines": None,
    }
    encoder = json.JSONEncoder(separators=(",", ":"))
    target = f"(window.FINDALINE_LINES = window.FINDALINE_LINES || {{}})[{json.dumps(run['id'])}]"
    with open(os.path.join(run_path, "data.js"), "w", encoding="utf-8") as full,             open(os.path.join(run_path, "lines.js"), "w", encoding="utf-8") as short:
        full.write(_script_head("window.FINDALINE_RUN", data))
        short.write(_script_head(target, {"crs": crs, "area": data["area"], "lines": None}))
        for start in range(0, len(lines), LINES_CHUNK):
            stop = min(start + LINES_CHUNK, len(lines))
            separator = "," if start else ""
            full.write(separator + encoder.encode(map_rows(lines, start, stop, True))[1:-1])
            short.write(separator + encoder.encode(map_rows(lines, start, stop, False))[1:-1])
            on_lines(stop - start, "data.js, lines.js")
        full.write("]};\n")
        short.write("]};\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(MAP_HTML.format(title=html.escape(run["name"]), viewer=VIEWER_FOLDER))
    return path
