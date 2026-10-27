"""
A search as a library: from SearchParams to a saved run, without terminal input.

    plan = plan_search(params, paths, on_event)   # source, grid, resolution
    estimate = estimate_search(plan)               # what would be downloaded, disk use
    data = prepare_data(plan)                      # step 1: download, mosaics, water, gaps
    lines = find_lines(plan, data)                 # steps 2-4: anchors, lines, profiles, groups
    result = save_run(plan, lines, confirm)        # step 5: the run, orthophoto, map
    # or all of it: run_search(params, paths, on_event, confirm)

Large areas (--area) are computed in square tiles (search_lines): each tile is a grid
with an overlap of --max-length around its core, and a line belongs to the tile whose
core holds its first anchor (the one with the smaller x, then y), so no line is found
twice. Each finished tile is saved in the run folder (tiles/<k>.pkl.gz); the same
command after an interruption finds the unfinished run and skips those tiles.

Messages go to on_event(kind, text) with kind "info" or "warning"; progress goes
through progress.py (progress.subscribe). Questions (an expensive run, orthophoto
downloads) go to confirm(kind, text) -> bool; without it the answer is yes.
A search that cannot go on raises SearchError with the message for the user.
"""

import dataclasses
import gc
import glob
import gzip
import hashlib
import json
import math
import os
import pickle
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import psutil
import requests

import area as ar
import cache
import coast
import grid as gd
import helpers as sh
import linesearch as ls
import mapping as mp
import orthophoto as ortho
import osm
import runs as rn
import sag as sg
import sources as src
import terrain
from params import SearchParams
from progress import progress

# Ask before continuing when a run looks this expensive
CONFIRM_DOWNLOAD_MB = 500
CONFIRM_DISK_GB = 4
CONFIRM_MINUTES = 30
ORTHO_SECONDS_PER_TILE = 0.3  # measured download rate with orthophoto.DOWNLOAD_THREADS
ORTHO_DETAIL = 50  # groups of an area with detailed orthophoto, unless the service or --ortho-detail says
# Tiles of large areas: the grids of one tile may use this share of the free memory,
# counting this many bytes per pixel (terrain, surface, water, gaps, masks, temporaries)
TILE_MEMORY_SHARE = 0.25
TILE_BYTES_PER_PIXEL = 100  # 64 of them, and the union-find of the anchor prominence (terrain.py)
DOWNLOAD_MB_PER_S = 5  # for the time estimate of area searches
MAX_MEASURED_PX = 6000  # the longest line measured on the map, in pixels (profile_of_line builds a square grid around it)
CGROUP_ROOT = "/sys/fs/cgroup"  # cgroup v2 of this process inside a container (its own namespace)

OnEvent = Callable[[str, str], None]
Confirm = Callable[[str, str], bool]


class SearchError(Exception):
    """The search cannot go on; the message is meant for the user."""


@dataclass
class Paths:
    """The data folder (see cache.py) and the work folder of this search."""
    data: str
    work: str

    @property
    def cache(self) -> str:
        return os.path.join(self.data, "cache")

    @property
    def runs(self) -> str:
        return os.path.join(self.data, "runs")


@dataclass
class Plan:
    params: SearchParams
    paths: Paths
    area: ar.Area
    source: src.Source
    grid: gd.Grid
    resolution: float
    group_distance: float
    nmpt: Optional[str]
    rigging: sg.Rigging
    layers: List[src.Layer]
    emit: OnEvent
    crs: str
    center_xy: tuple
    # Large areas: (core bounds, tile grid) per tile, with cores of tile_core metres; None: one grid
    tiles: Optional[List[Tuple[tuple, gd.Grid]]] = None
    tile_core: Optional[float] = None
    tile_label: str = ""  # "Tile 3/15 · " while a tile of a large area is computed


STEPS = 5


def _step(plan: Plan, n: int, title: str):
    """Start a step of the search: a message, and the step the progress bars belong to."""
    plan.emit("info", f"\nStep {n}: {title[0].upper() + title[1:]}...")
    progress.set_step(f"{plan.tile_label}Step {n}/{STEPS}: {title}")


@dataclass
class Estimate:
    download_mb: float
    work_gb: float
    reasons: List[str] = field(default_factory=list)  # why the run looks expensive


@dataclass
class Data:
    """Step 1 results: memory-mapped grids in the work folder."""
    elevation: np.ndarray
    surface: Optional[np.ndarray]
    water: Optional[np.ndarray]
    filled: Optional[np.ndarray]
    slope: Optional[np.ndarray]


@dataclass
class Lines:
    """Steps 2-4 results: every valid line (with its profile) and the groups ("spots")."""
    results: List[Dict]
    spot_groups: List[Dict]


@dataclass
class Result:
    results: List[Dict]
    spot_groups: List[Dict]
    meta: Dict
    run: Dict
    map_path: Optional[str]


def _print(kind: str, text: str):
    print(text)


def plan_search(params: SearchParams, paths: Paths, on_event: Optional[OnEvent] = None,
                with_margin: bool = True) -> Plan:
    """Choose the source and the grid, and describe the search. The grid covers the area
    and _margin around it, unless with_margin is False (a profile needs no anchor values)."""
    emit = on_event or _print
    src.used_data.clear()  # several searches may run in one process
    cache.check_layout(paths.cache)
    if params.area is not None:
        try:
            area = ar.from_spec(params.area, params.buffer, params.within, paths.cache, params.max_length)
        except (ValueError, requests.RequestException) as e:
            raise SearchError(f"Area: {e}")
        center_lat, center_lon = area.center
    else:
        if params.lat is not None:
            center_lat, center_lon = params.lat, params.lon
        else:
            center_lat, center_lon = mp.pl1992_to_wgs84(params.easting, params.northing)
        area = ar.Circle(center_lat, center_lon, params.radius)

    try:
        source, reason = src.choose_source(params.source, center_lat, center_lon, paths.cache,
                                           params.elevation_file, params.surface_file)
    except ValueError as e:
        raise SearchError(str(e))
    src.OpenTopographyTiles.api_key = params.opentopo_key
    nmpt = params.nmpt_mode
    if nmpt and source.nmpt is None:
        emit("info", f"Note: {source.name} has no surface model (trees/buildings), NMPT check is off")
        nmpt = None
    resolution = params.resolution or source.default_resolution
    group_distance = params.group_distance or max(20.0, 2 * resolution)
    native = source.nmt.native_res
    if isinstance(source.nmt, src.WcsTiles) and source.nmt.fetch_res is None and resolution < native:
        raise SearchError(f"--resolution cannot be finer than the {source.name} data ({native:g} m)")

    crs = source.working_crs(center_lat, center_lon)
    x, y = ar._to(crs)(center_lon, center_lat)
    margin = _margin(params, resolution) if with_margin else 0.0
    left, bottom, right, top = area.bounds(crs)
    grid = gd.make_grid((left - margin, bottom - margin, right + margin, top + margin), resolution, crs,
                        source.grid_origin)
    tiles = core = None
    if isinstance(area, ar.Polygon):
        grid.area_shape = ar.project(area.geometry, "EPSG:4326", crs)
        core = tile_core_size(params, resolution)
        tiles = _tiles(grid, params, resolution, source.grid_origin, core)
    rigging = sg.Rigging(params.pretension, params.stretch, params.webbing_weight,
                         params.athlete_mass, params.fall_zone)
    layers = [source.nmt] + ([source.nmpt] if nmpt else [])

    lines = ["=" * 60, "Find-a-Line - Highline Location Finder", "=" * 60,
             f"Centre: {center_lat:.6f}, {center_lon:.6f} (WGS84) / E {x:.0f}, N {y:.0f} ({crs})",
             f"Data: {source.name} ({reason}) - {source.description}"]
    if source.note:
        lines.append(f"Note: {source.note}")
    if isinstance(area, ar.Circle):
        lines.append(f"Radius: {params.radius:.0f}m, resolution: {resolution:g}m ({grid.width} x {grid.height} px)")
    else:
        lines.append(f"Area: {_area_text(params)}, {ar.size_km2(area.geometry):.2f} km², resolution: "
                     f"{resolution:g}m ({grid.width} x {grid.height} px, "
                     + (f"{len(tiles)} tiles)" if tiles else "one grid)"))
    if resolution < native * 0.99:
        lines.append(f"Note: the resolution is finer than the data ({native:g} m), which is interpolated")
    lines += [f"Length constraints: {params.min_length:g}m - {params.max_length:g}m",
              f"Anchor height difference: at most {params.max_anchor_diff:g}% of the length "
              f"and {params.max_anchor_diff_m:g}m",
              f"Slope threshold: {params.slope_threshold:g} degrees",
              f"NMPT (trees/buildings): {nmpt or 'not used'}"]
    if params.model == "sag":
        lines.append(f"Clearance model: sag estimator ({params.pretension:g} kN, {params.stretch:g}% @ 10 kN, "
                     f"{params.webbing_weight:g} g/m, athlete {params.athlete_mass:g} kg, "
                     f"fall zone {params.fall_zone:g} m)")
    else:
        lines.append("Clearance model: maximum elevation formula")
    lines.append("=" * 60)
    for line in lines:
        emit("info", line)
    for note in getattr(area, "notes", []):
        emit("warning", note)
    return Plan(params, paths, area, source, grid, resolution, group_distance, nmpt, rigging, layers,
                emit, crs, (x, y), tiles, core)


def _area_text(params: SearchParams) -> str:
    """E.g. "coast ±100 m within osm:Gozo", "osm:Gozo", "area.geojson"."""
    a = params.area
    if a in ("coast", "cliffs"):
        text = f"{a} ±{params.buffer:g} m"
    else:
        text = a if a.startswith("osm:") else os.path.basename(a)
    return text + (f" within {params.within}" if params.within else "")


def _margin(params: SearchParams, res: float) -> float:
    """Terrain around the anchors that their prominence and drop look at (terrain.py):
    the grid covers this much around the area, so the values do not depend on its edge."""
    return math.ceil(max(params.prominence_span, params.drop_radius) / res) * res


def _overlap(params: SearchParams, res: float) -> float:
    """Margin around a tile core: the longest line, the terrain around its far anchor
    (_margin), plus pixels for the slope at the edge."""
    return math.ceil(params.max_length / res) * res + _margin(params, res) + 2 * res


@dataclass
class CgroupLimits:
    """What a container (cgroup v2) lets this process use; None: no limit."""
    memory: Optional[int] = None  # bytes still free below memory.max
    cpus: Optional[int] = None  # CPUs the quota of cpu.max and the affinity allow


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="ascii") as f:
            return f.read().strip()
    except (OSError, ValueError):
        return None


def cgroup_limits(root: Optional[str] = None) -> CgroupLimits:
    """Memory and CPU limits of a container (Docker's mem_limit and cpus), from the
    cgroup v2 files under root. Missing files (Windows, macOS, cgroup v1, no
    container) or "max" mean no limit; nothing changes outside a container then."""
    root = root or CGROUP_ROOT
    limits = CgroupLimits()
    limit, used = _read(os.path.join(root, "memory.max")), _read(os.path.join(root, "memory.current"))
    if limit and limit != "max" and used:
        try:
            used_bytes = int(used)
            # The page cache that may be dropped counts as free, as in docker stats
            for line in (_read(os.path.join(root, "memory.stat")) or "").splitlines():
                key, _, value = line.partition(" ")
                if key == "inactive_file":
                    used_bytes -= int(value)
            limits.memory = max(0, int(limit) - max(0, used_bytes))
        except ValueError:
            pass
    quota = (_read(os.path.join(root, "cpu.max")) or "max").split()
    if quota[0] != "max":
        try:
            period = int(quota[1]) if len(quota) > 1 else 100000
            limits.cpus = max(1, math.ceil(int(quota[0]) / period))
        except ValueError:
            pass
    if hasattr(os, "sched_getaffinity") and limits.cpus is not None:
        limits.cpus = min(limits.cpus, len(os.sched_getaffinity(0)))
    return limits


def available_memory() -> int:
    """Free memory in bytes: the machine's, or less when a container limits it."""
    free = psutil.virtual_memory().available
    limit = cgroup_limits().memory
    return free if limit is None else min(free, limit)


def line_threads(params: SearchParams) -> Optional[int]:
    """Threads of the line search: --threads, or at most the CPUs of a container's
    quota (numba itself takes the affinity, and NUMBA_NUM_THREADS when it is set);
    None leaves the default of find_valid_lines (CPU count - 1)."""
    if params.threads or "NUMBA_NUM_THREADS" in os.environ:
        return params.threads
    cpus = cgroup_limits().cpus
    if cpus is None:
        return None
    import numba
    return max(1, min(cpus, numba.config.NUMBA_NUM_THREADS - 1))


def tile_core_size(params: SearchParams, res: float) -> float:
    """Side of a tile core in metres: --tile-size, or what the free memory allows
    (in steps of 500 m, so the tiles stay the same when the free memory changes a little)."""
    if params.tile_size:
        core = params.tile_size
    else:
        side_px = math.sqrt(available_memory() * TILE_MEMORY_SHARE / TILE_BYTES_PER_PIXEL)
        core = max(500.0, math.floor((side_px * res - 2 * _overlap(params, res)) / 500) * 500)
    return max(res, math.floor(max(core, 10 * res) / res) * res)


def _tiles(grid: gd.Grid, params: SearchParams, res: float, origin, core: float) -> Optional[list]:
    """(core bounds, grid with overlap) of each tile under the area; None if one grid will do."""
    from shapely.geometry import box
    # Cores over the area (the grid without its _margin); the margin is for the overlaps
    margin = _margin(params, res)
    g_left, g_bottom, g_right, g_top = grid.bounds
    left, bottom, right, top = g_left + margin, g_bottom + margin, g_right - margin, g_top - margin
    if max(right - left, top - bottom) <= core:
        return None
    overlap = _overlap(params, res)
    tiles = []
    for j in range(math.ceil((top - bottom) / core)):
        for i in range(math.ceil((right - left) / core)):
            c = (left + i * core, max(bottom, top - (j + 1) * core), min(right, left + (i + 1) * core), top - j * core)
            if not grid.area_shape.intersects(box(*c)):
                continue
            ext = (max(g_left, c[0] - overlap), max(g_bottom, c[1] - overlap),
                   min(g_right, c[2] + overlap), min(g_top, c[3] + overlap))
            tile = gd.make_grid(ext, res, grid.crs, origin)
            tile.area_shape = grid.area_shape
            tiles.append((c, tile))
    return tiles


def estimate_search(plan: Plan) -> Estimate:
    """What the search would download and how much disk its working grids take."""
    p, emit = plan.params, plan.emit
    download_mb = 0.0
    for layer in plan.layers:
        n_tiles, n_missing, mb = layer.estimate(plan.grid, plan.paths.cache, p.update)
        download_mb += mb
        emit("info", f"{layer.description}: {n_tiles} tiles, {n_missing} to download (~{mb:.0f} MB)")
    if p.water == "fill" and p.coastline == "world" and coast.world_file(plan.paths.cache) is None:
        download_mb += coast.WORLD_MB
        emit("info", f"OpenStreetMap water polygons (world): one download of ~{coast.WORLD_MB} MB")
    largest = max((t for _, t in plan.tiles), key=lambda t: t.width * t.height) if plan.tiles else plan.grid
    work_gb = largest.width * largest.height * 4 * (len(plan.layers) + bool(p.slope_file)) / 1e9
    estimate = Estimate(download_mb, work_gb)
    if isinstance(plan.area, ar.Polygon):  # areas are always confirmed: they can be large
        estimate.reasons.append(
            f"area of {ar.size_km2(plan.area.geometry):.2f} km² in {len(plan.tiles) if plan.tiles else 1} "
            f"tile(s); about {download_mb:.0f} MB to download (~{download_mb / DOWNLOAD_MB_PER_S / 60:.0f} min)")
    elif download_mb > CONFIRM_DOWNLOAD_MB:
        estimate.reasons.append(f"about {download_mb:.0f} MB to download ({plan.source.name})")
    if work_gb > CONFIRM_DISK_GB:
        estimate.reasons.append(f"working grids take about {work_gb:.1f} GB of disk in {plan.paths.work}")
    return estimate


def download_data(plan: Plan):
    """Download what the search area needs into the cache, without building grids.

    The web worker does this for the next job while the current one computes; the
    search itself then finds the data cached. Gap filling data is left to the search
    (whether it is needed shows only on the grid).
    """
    p, grid, cache_dir = plan.params, plan.grid, plan.paths.cache
    try:
        for layer, label in zip(plan.layers, ("terrain", "surface")):
            layer.download(grid, cache_dir, p.update, label)
        if p.water == "fill" or (p.fill_gaps == "on" and plan.source.fallback):
            src.WATER_MASK.download(grid, cache_dir, p.update, "water mask")
        if p.water == "fill" and p.coastline == "overpass" and coast.world_file(cache_dir) is None:
            try:
                osm.theme("coastline", *coast._grid_bbox_wgs84(grid), cache_dir, p.update)
            except requests.RequestException:
                pass  # the search falls back to the Copernicus mask with a warning
    except RuntimeError as e:
        raise SearchError(f"Download failed: {e}")


def prepare_data(plan: Plan) -> Data:
    """Step 1: download what is missing and build the grids (terrain, surface, water, gaps)."""
    p, grid, source, emit = plan.params, plan.grid, plan.source, plan.emit
    cache_dir, work = plan.paths.cache, plan.paths.work
    _step(plan, 1, f"downloading elevation data ({source.name})")
    try:
        elevation_path = src.prepare_layer(source.nmt, grid, cache_dir,
                                           os.path.join(work, "nmt.npy"), "terrain", p.update)
        mask = plan.area.mask(grid)
        with_data = float(np.isfinite(np.load(elevation_path, mmap_mode="r")[mask]).mean()) if mask.any() else 0.0
        emit("info", f"Elevation data: {100 * with_data:.2f}% of the search area from {source.name}")
        surface_path = None
        if plan.nmpt:
            surface_path = src.prepare_layer(source.nmpt, grid, cache_dir,
                                             os.path.join(work, "nmpt.npy"), "surface", p.update)
        # Order matters: where the sea is, then gaps on land (outside the sea), then the water
        water_path = filled_path = sea_zone = None
        if p.water == "fill":
            wbm = src.water_body_mask(grid, cache_dir, os.path.join(work, "wbm.npy"), p.update)
            sea, coast_source, warning = coast.sea_mask(grid, cache_dir, p.coastline, wbm == src.OCEAN, p.update)
            emit("info", f"Coastline: {coast_source}")
            if warning:
                emit("warning", warning)
            sea_zone = src.near(sea, p.shore_buffer, grid.res)
        elif p.fill_gaps == "on" and source.fallback:
            # Without --water fill the sea stays without data; keep the gap filling off it too
            wbm = src.water_body_mask(grid, cache_dir, os.path.join(work, "wbm.npy"), p.update)
            sea_zone = src.near(wbm == src.OCEAN, src.SHORE_BUFFER, grid.res)
        if p.fill_gaps == "on" and source.fallback:
            fallback = src.SOURCES[source.fallback]
            filled_path, filled_px = src.fill_gaps(elevation_path, surface_path, grid, fallback, cache_dir,
                                                   os.path.join(work, "filled.npy"), p.update, exclude=sea_zone)
            if filled_px:
                emit("info", f"Filled {filled_px * plan.resolution ** 2 / 1e6:.2f} km² of gaps in the data "
                             f"from {fallback.name}")
        if p.water == "fill":
            water_path = src.fill_water(elevation_path, surface_path, grid, wbm, sea_zone,
                                        os.path.join(work, "water.npy"))
    except RuntimeError as e:
        raise SearchError(f"Download failed: {e}")
    elevation = np.load(elevation_path, mmap_mode="r")
    surface = np.load(surface_path, mmap_mode="r") if surface_path else None
    if surface is not None:
        land = plan.area.mask(grid) & ~np.isnan(elevation)
        no_surface = (np.isnan(surface) & land).sum() / max(land.sum(), 1)
        if no_surface > 0.01:
            emit("warning", f"no surface model (trees, buildings) for {no_surface:.0%} of the search area.\n"
                            "Lines there are checked against the bare terrain only: trees and buildings under\n"
                            "them are not marked.")
    slope = None
    if p.slope_file:
        slope_path = src.load_slope_file(p.slope_file, grid, os.path.join(work, "slope.npy"))
        slope = np.load(slope_path, mmap_mode="r")
    return Data(elevation, surface,
                np.load(water_path, mmap_mode="r") if water_path else None,
                np.load(filled_path, mmap_mode="r") if filled_path else None, slope)


def find_lines(plan: Plan, data: Data) -> Lines:
    """Steps 2-4: anchors, line search, coordinates and profiles, groups."""
    results, groups = _lines(plan, data)
    return Lines(results, groups)


def _lines(plan: Plan, data: Data, core: Optional[tuple] = None) -> Tuple[List[Dict], List[Dict]]:
    """Steps 2-4: the lines and their groups; with core (bounds), only lines whose first
    anchor (smaller x, then y) lies in it.

    Every line gets its metres over water and over filled gaps. Circles keep the profile
    of every line (the map shows each); areas only that of the line shown for each group,
    as the profiles of millions of lines would take long and be thrown away.
    """
    p, grid, emit = plan.params, plan.grid, plan.emit

    # Step 2: Find raster pixels with significant slope for promising anchors
    _step(plan, 2, "finding significant slopes")
    anchors = sh.find_significant_slopes(data.elevation, plan.resolution, p.slope_threshold,
                                         plan.area.mask(grid), data.slope)
    if data.filled is not None:
        # Slopes across the edge of a filled gap mix two surveys: not reliable anchors there
        from scipy import ndimage
        gaps = np.asarray(data.filled)
        unreliable = ndimage.binary_dilation(gaps, structure=np.ones((3, 3))) & \
            ~ndimage.binary_erosion(gaps, structure=np.ones((3, 3)))
        anchors = anchors[~unreliable[anchors[:, 0], anchors[:, 1]]]
    emit("info", f"Found {len(anchors)} potential anchors to evaluate")
    if len(anchors) < 2:
        if core is not None:  # a tile of a large area: nothing here
            return [], []
        raise SearchError("Not enough steep terrain. Try a lower --slope-threshold or a larger --radius.")

    # How far each anchor stands out (terrain.py); the limits for both anchors leave anchors out
    progress.compute.start("anchor prominence and drop", 2, unit="step")
    prom = terrain.prominence(data.elevation, anchors, plan.resolution, p.prominence_span)
    progress.compute.update(1)
    drop = terrain.drop(data.elevation, anchors, plan.resolution, p.drop_radius)
    progress.compute.finish()
    keep = np.ones(len(anchors), dtype=bool)
    if p.min_prominence_both is not None:
        keep &= prom >= p.min_prominence_both
    if p.min_drop is not None:
        keep &= drop >= p.min_drop
    if not keep.all():
        emit("info", f"Anchors left after the prominence and drop limits: {int(keep.sum())}")
        anchors, prom, drop = anchors[keep], prom[keep], drop[keep]
    # --min-prominence: one anchor of each line; the others stay as partners
    strong = None if p.min_prominence is None else prom >= p.min_prominence
    if len(anchors) < 2 or (strong is not None and not strong.any()):
        if core is not None:
            return [], []
        raise SearchError("No anchors stand out enough. Try a lower --min-prominence, --min-prominence-both "
                          "or --min-drop.")

    # Step 3: Compare each promising anchor to every other anchor
    _step(plan, 3, "evaluating anchor pairs")
    sag_table = None
    if p.model == "sag":
        emit("info", "Precomputing the sag estimator for all line lengths...")
        sag_table = sg.EnvelopeTable(plan.rigging, p.min_length, p.max_length)
    first = None
    if core is not None:  # pairs only from the anchors in the core: the overlap is for partners
        left, bottom, right, top = core
        x, y = grid.pixel_to_xy(anchors[:, 0], anchors[:, 1])
        first = (left <= x) & (x < right) & (bottom <= y) & (y < top)
    valid_lines = ls.find_valid_lines(data.elevation, data.surface, anchors, plan.resolution,
                                      p.min_length, p.max_length, plan.nmpt, line_threads(p),
                                      sag_table, p.max_anchor_diff / 100, p.max_anchor_diff_m, first,
                                      p.high_above, sh.LineLimits(p.min_height, p.min_mean_height,
                                                                  p.min_high_share, p.max_wall_angle),
                                      np.column_stack([prom, drop]), strong)
    if not valid_lines and core is None:
        raise SearchError("No lines found matching elevation requirements.")

    # Step 4: coordinates, water and gaps, groups, profiles
    _step(plan, 4, "coordinates, groups and profiles")
    results = mp.coordify(valid_lines, grid, plan.nmpt)
    if core is not None:
        left, bottom, right, top = core
        keep = [k for k, r in enumerate(results)
                if left <= min(r["a1_xy"], r["a2_xy"])[0] < right and bottom <= min(r["a1_xy"], r["a2_xy"])[1] < top]
        results, valid_lines = [results[k] for k in keep], [valid_lines[k] for k in keep]
    a1 = np.array([line.a1 for line in valid_lines], dtype=np.int64).reshape(-1, 2)
    a2 = np.array([line.a2 for line in valid_lines], dtype=np.int64).reshape(-1, 2)
    progress.compute.start("over water and filled gaps", 2, unit="mask")
    water = ls.covered_metres(a1, a2, plan.resolution, data.water)
    progress.compute.update(1)
    filled = ls.covered_metres(a1, a2, plan.resolution, data.filled)
    progress.compute.finish()
    for r, w, g in zip(results, water, filled):
        r["water_m"], r["filled_m"], r["profile"] = w, g, None
    groups = mp.group_lines(results, plan.group_distance, p.group_by)
    if isinstance(plan.area, ar.Circle):
        wanted = range(len(results))
    else:
        index = {id(r): k for k, r in enumerate(results)}
        wanted = sorted(index[id(g["rep"])] for g in groups)
    progress.compute.start("line profiles", len(wanted), unit="line")
    for k in wanted:
        line = valid_lines[k]
        results[k]["profile"] = sh.line_profile(line.a1, line.a2, data.elevation, plan.resolution, data.surface,
                                                sag_table=sag_table, water=data.water, filled=data.filled)
        progress.compute.update(1)
    progress.compute.finish()
    return results, groups


def _run_name(plan: Plan) -> str:
    p = plan.params
    if p.name:
        return p.name
    if p.area and p.area.startswith("osm:"):
        return p.area[4:]
    if p.area:
        return _area_text(p)
    lat, lon = plan.area.center
    return rn.place_name(lat, lon, plan.paths.cache) or f"{lat:.5f}, {lon:.5f}"


def _search_hash(plan: Plan) -> str:
    """What identifies the same tiled search, to resume it."""
    p = plan.params.model_dump(exclude={"name", "update", "no_html", "ortho", "ortho_detail", "threads",
                                        "opentopo_key", "tile_size", "group_distance", "group_by"})
    key = {"params": p, "source": plan.source.id, "resolution": plan.resolution}
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def _open_run(plan: Plan) -> Dict:
    """The run folder of a tiled search: an unfinished one of the same search (whose tile
    size is then used again, so its finished tiles fit), or a new one."""
    key = _search_hash(plan)
    for path in sorted(glob.glob(os.path.join(plan.paths.runs, "*", "progress.json")), reverse=True):
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("status") == "running" and state.get("search") == key:
            folder = os.path.dirname(path)
            if state["tile_core"] != plan.tile_core:
                plan.tile_core = state["tile_core"]
                plan.tiles = _tiles(plan.grid, plan.params, plan.resolution, plan.source.grid_origin, plan.tile_core)
            done = len(glob.glob(os.path.join(folder, "tiles", "*.pkl.gz")))
            plan.emit("info", f"Resuming run {os.path.basename(folder)}: {done} of {len(plan.tiles)} tiles done")
            return {"id": os.path.basename(folder), "path": folder, "name": state["name"],
                    "created": datetime.fromisoformat(state["created"]), "search": key,
                    "tile_core": state["tile_core"]}
    created = datetime.now()
    name = _run_name(plan)
    run = rn.new_run(plan.paths.runs, name, created)
    run.update(name=name, created=created, search=key, tile_core=plan.tile_core)
    _progress(run, "running")
    return run


def _progress(run: Dict, status: str):
    """progress.json of a tiled run: its state while running (run.json is written at the end)."""
    with open(os.path.join(run["path"], "progress.json"), "w", encoding="utf-8") as f:
        json.dump({"status": status, "search": run["search"], "tile_core": run["tile_core"],
                   "name": run["name"], "created": run["created"].isoformat()}, f)


TILE_CHUNK = 100_000  # lines per pickle in a tile file (one progress update each)


def _save_tile(path: str, lines: List[Dict]):
    """A finished tile's lines: their number, then pickles of TILE_CHUNK lines, gzipped.

    Level 1: level 9 took 23 times as long for 21% less (the floats hardly compress),
    11 minutes for a tile of 12.5 million lines.
    """
    progress.compute.start("saving tile lines", len(lines), unit="line")
    with gzip.open(path + ".part", "wb", compresslevel=1) as f:
        pickle.dump(len(lines), f, pickle.HIGHEST_PROTOCOL)
        for start in range(0, len(lines), TILE_CHUNK):
            chunk = lines[start:start + TILE_CHUNK]
            pickle.dump(chunk, f, pickle.HIGHEST_PROTOCOL)
            progress.compute.update(len(chunk))
    os.replace(path + ".part", path)
    progress.compute.finish()


def _load_tile(path: str) -> List[Dict]:
    """The lines of a tile saved by _save_tile."""
    lines = []
    with gzip.open(path, "rb") as f:
        total = pickle.load(f)
        shown = total > TILE_CHUNK  # small tiles load at once: no bar for each
        if shown:
            progress.compute.start("loading tile lines", total, unit="line")
        while len(lines) < total:
            chunk = pickle.load(f)
            lines += chunk
            if shown:
                progress.compute.update(len(chunk))
    if shown:
        progress.compute.finish()
    return lines


def search_lines(plan: Plan) -> Tuple[Lines, Optional[Dict]]:
    """Steps 1-4 for one grid or, for a large area, tile by tile (resumable).

    Returns the lines and, for a tiled search, its run (to be completed by save_run).
    """
    if not plan.tiles:
        lines = find_lines(plan, prepare_data(plan))
        progress.set_step("")
        return lines, None
    run = _open_run(plan)
    folder = os.path.join(run["path"], "tiles")
    os.makedirs(folder, exist_ok=True)
    results = []
    for k, (core, tile) in enumerate(plan.tiles):
        path = os.path.join(folder, f"{k}.pkl.gz")
        if not os.path.exists(path):
            plan.emit("info", f"\n--- Tile {k + 1} of {len(plan.tiles)} ({tile.width} x {tile.height} px) ---")
            work = os.path.join(plan.paths.work, f"tile{k}")
            os.makedirs(work, exist_ok=True)
            tile_plan = dataclasses.replace(plan, grid=tile, paths=Paths(plan.paths.data, work), tiles=None,
                                            tile_label=f"Tile {k + 1}/{len(plan.tiles)} · ")
            data = prepare_data(tile_plan)
            found, _ = _lines(tile_plan, data, core)
            del data
            gc.collect()  # close the memory-mapped grids before deleting them (Windows)
            shutil.rmtree(work, ignore_errors=True)
            progress.set_step(f"{tile_plan.tile_label}Step 4/{STEPS}: saving the tile's lines")
            _save_tile(path, found)
            del found
        progress.set_step(f"Step 4/{STEPS}: loading tile {k + 1}/{len(plan.tiles)}")
        results += _load_tile(path)
    if not results:
        raise SearchError("No lines found matching elevation requirements.")
    progress.set_step(f"Step 4/{STEPS}: groups of all tiles")
    groups = mp.group_lines(results, plan.group_distance, plan.params.group_by)
    # A line shown for a group is the best of its tile's group too, except near the edge of a
    # tile, where a group may join lines of two tiles: those get their profile now
    missing = [g["rep"] for g in groups if g["rep"]["profile"] is None]
    if missing:
        meta = {"params": _run_params(plan), "source": {"id": plan.source.id}, "center": {"crs": plan.crs},
                "data": json.loads(json.dumps(src.used_data))}
        used = list(src.used_data)  # plan_search starts a new record of the data used
        progress.compute.start("profiles at tile edges", len(missing), unit="line")
        for r in missing:
            paths = Paths(plan.paths.data, os.path.join(plan.paths.work, "edge"))
            os.makedirs(paths.work, exist_ok=True)
            r["profile"] = profile_of_line(meta, paths, r["anchor1"], r["anchor2"])
            shutil.rmtree(paths.work, ignore_errors=True)
            progress.compute.update(1)
        progress.compute.finish()
        src.used_data[:] = used
    progress.set_step("")
    return Lines(results, groups), run


def _merged_data() -> List[Dict]:
    """src.used_data with the entries of several tiles merged per layer."""
    merged: Dict[tuple, Dict] = {}
    for entry in src.used_data:
        key = (entry.get("layer"), entry.get("folder"), entry.get("file"))
        if key not in merged:
            merged[key] = json.loads(json.dumps(entry))
            continue
        m = merged[key]
        if "paths" in entry:  # files shared by tiles are listed once
            m["paths"] = sorted(set(m["paths"]) | set(entry["paths"]))
            m["files"] = len(m["paths"])
        for k, pick in (("oldest", min), ("newest", max)):
            values = [v for v in (m.get("fetched", {}).get(k), entry.get("fetched", {}).get(k)) if v]
            if values:
                m.setdefault("fetched", {})[k] = pick(values)
        if entry.get("survey_years") and m.get("survey_years"):
            m["survey_years"] = [min(m["survey_years"][0], entry["survey_years"][0]),
                                 max(m["survey_years"][1], entry["survey_years"][1])]
    return list(merged.values())


def _run_params(plan: Plan) -> Dict:
    """The parameters run.json records (also what profile_of_line needs)."""
    p, circle = plan.params, isinstance(plan.area, ar.Circle)
    return {"radius": p.radius if circle else None,
            "area": None if circle else {"area": p.area, "buffer": p.buffer, "within": p.within},
            "resolution": plan.resolution,
            "min_length": p.min_length, "max_length": p.max_length,
            "max_anchor_diff": p.max_anchor_diff,
            "max_anchor_diff_m": p.max_anchor_diff_m,
            "slope_threshold": p.slope_threshold, "slope_file": p.slope_file, "high_above": p.high_above,
            "min_height": p.min_height, "min_mean_height": p.min_mean_height, "min_high_share": p.min_high_share,
            "max_wall_angle": p.max_wall_angle, "min_prominence": p.min_prominence,
            "min_prominence_both": p.min_prominence_both, "prominence_span": p.prominence_span,
            "min_drop": p.min_drop, "drop_radius": p.drop_radius,
            "nmpt": plan.nmpt, "water": p.water, "coastline": p.coastline,
            "shore_buffer": p.shore_buffer, "fill_gaps": p.fill_gaps,
            "group_distance": plan.group_distance,
            "group_by": p.group_by, "model": p.model,
            "rigging": dataclasses.asdict(plan.rigging)}


def save_run(plan: Plan, lines: Lines, confirm: Optional[Confirm] = None, run: Optional[Dict] = None) -> Result:
    """Step 5: save the run (results, parameters, data used), the orthophoto tiles and the map.

    run: the run folder of a tiled search (search_lines); otherwise a new one is made.
    """
    p, emit = plan.params, plan.emit
    results, spot_groups = lines.results, lines.spot_groups
    confirm = confirm or (lambda kind, text: True)
    _step(plan, 5, "saving the run and the map")
    center_lat, center_lon = plan.area.center
    x, y = plan.center_xy
    if run is None:
        created = datetime.now()
        name = _run_name(plan)
        run = rn.new_run(plan.paths.runs, name, created)
    else:
        created, name = run["created"], run["name"]
    circle = isinstance(plan.area, ar.Circle)
    meta = {
        "id": run["id"],
        "name": name,
        "created": created.isoformat(timespec="seconds"),
        "center": {"lat": center_lat, "lon": center_lon, "x": x, "y": y, "crs": plan.crs},
        "area": plan.area.describe(),
        "source": {"id": plan.source.id, "name": plan.source.name, "description": plan.source.description},
        "data": _merged_data(),  # folders, files and fetch times of the data used
        "params": _run_params(plan),
        "summary": {"lines": len(results), "spots": len(spot_groups),
                    "clear_spots": sum(not g["rep"]["over_obstacles"] for g in spot_groups)},
    }
    meta["description"] = rn.params_summary(meta)
    def on_lines(n: int, file: str):
        progress.compute.update(n, file=file)

    progress.compute.start("saving results", 3 * len(results), unit="line")  # three passes, see save_results
    rn.save_results(run["path"], spot_groups, meta, on_lines)
    progress.compute.finish()
    if "search" in run:
        _progress(run, "done")
    emit("info", f"Run \"{name}\" saved to {os.path.abspath(run['path'])}")
    if not circle:
        # Large areas: only the group representatives carry a profile in the map
        representatives = {id(g["rep"]) for g in spot_groups}
        for r in results:
            if id(r) not in representatives:
                r["profile"] = None

    map_path = None
    if not p.no_html:
        source = plan.source
        ortho_cache = None
        if source.ortho and p.ortho != "never":
            # Detailed orthophoto only around one line per group (variants lie within the strip),
            # for the best groups of an area or as the service's fair use allows
            detail = p.ortho_detail if p.ortho_detail is not None else source.ortho.detail
            if detail is None and not circle:
                detail = ORTHO_DETAIL
            spots = [g["rep"] for g in spot_groups][:detail]
            ortho_folder = ortho.cache_folder(plan.paths.cache, source.ortho)
            max_zoom = ortho.DETAIL_ZOOMS[-1] if source.ortho.url else source.ortho.base_max_zoom
            tiles = ortho.missing_tiles(ortho.tiles_to_cache(spots, plan.area, max_zoom), ortho_folder,
                                        plan.paths.cache, p.update, source.ortho.ttl_days)
            minutes = len(tiles) * ORTHO_SECONDS_PER_TILE / 60
            emit("info", f"Orthophoto for offline use: {len(tiles)} tiles to download "
                         f"(~{len(tiles) * ortho.APPROX_TILE_KB / 1000:.0f} MB, ~{minutes:.0f} min)")
            if p.ortho == "auto" and minutes > CONFIRM_MINUTES and not confirm(
                    "ortho", "Download now? Otherwise the map loads the orthophoto online. [y/N] "):
                tiles = []
            ortho.download(source.ortho, tiles, ortho_folder, plan.paths.cache)
            ortho_cache = ortho_folder
        progress.compute.start("saving the map", 3 * len(results), unit="line")  # three passes, see save_map
        map_path = mp.save_map(results, plan.area, run["path"], plan.paths.runs, meta, plan.crs, plan.group_distance,
                               p.group_by, ortho_cache, source.ortho, p.model, plan.rigging, on_lines,
                               p.high_above)
        progress.compute.finish()
    rn.write_index(plan.paths.runs)
    progress.set_step("")
    return Result(results, spot_groups, meta, run, map_path)


_sag_tables: Dict[str, "sg.EnvelopeTable"] = {}


def profile_of_line(meta: Dict, paths: Paths, anchor1: Tuple[float, float], anchor2: Tuple[float, float]) -> Dict:
    """The profile of a line of a saved run (meta: its run.json), computed again from the cache.

    Maps of areas keep a profile only for the line shown for each group; this gives any
    other line its profile, and any two points measured on the map (at most MAX_MEASURED_PX
    pixels apart) theirs. A small grid around the line is built with the run's source,
    resolution and filling, on the same pixel grid as the search, so terrain and surface
    are the same; offsets of filled gaps may differ a little (they come from a smaller
    surrounding). anchor1, anchor2: (lat, lon) of the anchors as the run saved them.
    """
    p, rig = meta["params"], meta["params"]["rigging"]
    values = {k: p[k] for k in ("resolution", "min_length", "max_length", "max_anchor_diff", "max_anchor_diff_m",
                                "slope_threshold", "water", "coastline", "shore_buffer", "fill_gaps", "model")}
    values.update(rig, nmpt=p["nmpt"] or "off", source=meta["source"]["id"], ortho="never", no_html=True)
    if values["source"] == "files":
        files = {d.get("layer"): d.get("file") for d in meta.get("data", [])}
        values.update(elevation_file=files.get("Terrain file"), surface_file=files.get("Surface file"))
    to_xy = ar._to(meta["center"]["crs"])
    (x1, y1), (x2, y2) = to_xy(anchor1[1], anchor1[0]), to_xy(anchor2[1], anchor2[0])
    length = math.hypot(x2 - x1, y2 - y1)
    if length > MAX_MEASURED_PX * p["resolution"]:
        raise SearchError(f"the points are {length / 1000:.1f} km apart; at the {p['resolution']:g} m resolution "
                          f"of this run profiles reach {MAX_MEASURED_PX * p['resolution'] / 1000:g} km")
    if length < 2 * p["resolution"]:
        raise SearchError("the points are too close")
    lat, lon = (anchor1[0] + anchor2[0]) / 2, (anchor1[1] + anchor2[1]) / 2
    values.update(lat=lat, lon=lon, radius=length / 2 + 10 * p["resolution"] + 20)
    plan = plan_search(SearchParams(**values), paths, lambda kind, text: None, with_margin=False)
    if plan.crs != meta["center"]["crs"]:
        raise SearchError(f"the run used {meta['center']['crs']}, a search here would use {plan.crs}")
    data = prepare_data(plan)
    grid, res = plan.grid, plan.resolution
    pixel = lambda x, y: (int(round((grid.top - y) / res - 0.5)), int(round((x - grid.left) / res - 0.5)))
    sag_table = None
    if p["model"] == "sag":
        # The same table as the search, so the same limits; a line measured on the map
        # may be shorter or longer than the search allowed: a table that reaches it
        spans = (min(p["min_length"], 0.99 * length), max(p["max_length"], 1.01 * length))
        if p["min_length"] <= length <= p["max_length"]:
            spans = (p["min_length"], p["max_length"])
        key = json.dumps([rig, *spans], sort_keys=True)
        if key not in _sag_tables:
            _sag_tables[key] = sg.EnvelopeTable(plan.rigging, *spans)
        sag_table = _sag_tables[key]
    profile = sh.line_profile(pixel(x1, y1), pixel(x2, y2), data.elevation, res, data.surface,
                              sag_table=sag_table, water=data.water, filled=data.filled)
    del data
    gc.collect()  # close the memory-mapped grids before the work folder is deleted (Windows)
    return profile


def run_search(params: SearchParams, paths: Paths, on_event: Optional[OnEvent] = None,
               confirm: Optional[Confirm] = None) -> Result:
    """The whole search; confirm("expensive", reasons) is asked before downloading."""
    plan = plan_search(params, paths, on_event)
    estimate = estimate_search(plan)
    if estimate.reasons and confirm and not confirm("expensive", "\n".join(estimate.reasons)):
        raise SearchError("Stopped.")
    lines, run = search_lines(plan)
    return save_run(plan, lines, confirm, run)
