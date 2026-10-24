"""
A search as a library: from SearchParams to a saved run, without terminal input.

    plan = plan_search(params, paths, on_event)   # source, grid, resolution
    estimate = estimate_search(plan)               # what would be downloaded, disk use
    data = prepare_data(plan)                      # step 1: download, mosaics, water, gaps
    lines = find_lines(plan, data)                 # steps 2-4: anchors, lines, profiles, groups
    result = save_run(plan, lines, confirm)        # step 5: the run, orthophoto, map
    # or all of it: run_search(params, paths, on_event, confirm)

Messages go to on_event(kind, text) with kind "info" or "warning"; progress goes
through progress.py (progress.subscribe). Questions (an expensive run, orthophoto
downloads) go to confirm(kind, text) -> bool; without it the answer is yes.
A search that cannot go on raises SearchError with the message for the user.
"""

import dataclasses
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

import numpy as np

import area as ar
import cache
import coast
import grid as gd
import helpers as sh
import linesearch as ls
import mapping as mp
import orthophoto as ortho
import runs as rn
import sag as sg
import sources as src
from params import SearchParams
from progress import progress

# Ask before continuing when a run looks this expensive
CONFIRM_DOWNLOAD_MB = 500
CONFIRM_DISK_GB = 4
CONFIRM_MINUTES = 30
ORTHO_SECONDS_PER_TILE = 0.3  # measured download rate with orthophoto.DOWNLOAD_THREADS

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


def plan_search(params: SearchParams, paths: Paths, on_event: Optional[OnEvent] = None) -> Plan:
    """Choose the source and the grid, and describe the search."""
    emit = on_event or _print
    src.used_data.clear()  # several searches may run in one process
    if params.lat is not None:
        center_lat, center_lon = params.lat, params.lon
    else:
        center_lat, center_lon = mp.pl1992_to_wgs84(params.easting, params.northing)
    area = ar.Circle(center_lat, center_lon, params.radius)

    cache.check_layout(paths.cache)
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
    x, y = area._xy(crs)
    grid = gd.make_grid(area.bounds(crs), resolution, crs, source.grid_origin)
    rigging = sg.Rigging(params.pretension, params.stretch, params.webbing_weight,
                         params.athlete_mass, params.fall_zone)
    layers = [source.nmt] + ([source.nmpt] if nmpt else [])

    lines = ["=" * 60, "Find-a-Line - Highline Location Finder", "=" * 60,
             f"Centre: {center_lat:.6f}, {center_lon:.6f} (WGS84) / E {x:.0f}, N {y:.0f} ({crs})",
             f"Data: {source.name} ({reason}) - {source.description}"]
    if source.note:
        lines.append(f"Note: {source.note}")
    lines.append(f"Radius: {params.radius:.0f}m, resolution: {resolution:g}m ({grid.width} x {grid.height} px)")
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
    return Plan(params, paths, area, source, grid, resolution, group_distance, nmpt, rigging, layers,
                emit, crs, (x, y))


def estimate_search(plan: Plan) -> Estimate:
    """What the search would download and how much disk its working grids take."""
    p, emit = plan.params, plan.emit
    download_mb = 0.0
    for layer in plan.layers:
        n_tiles, n_missing, mb = layer.estimate(plan.grid, plan.paths.cache, p.refresh)
        download_mb += mb
        emit("info", f"{layer.description}: {n_tiles} tiles, {n_missing} to download (~{mb:.0f} MB)")
    if p.water == "fill" and p.coastline == "world" and coast.world_file(plan.paths.cache) is None:
        download_mb += coast.WORLD_MB
        emit("info", f"OpenStreetMap water polygons (world): one download of ~{coast.WORLD_MB} MB")
    work_gb = plan.grid.width * plan.grid.height * 4 * (len(plan.layers) + bool(p.slope_file)) / 1e9
    estimate = Estimate(download_mb, work_gb)
    if download_mb > CONFIRM_DOWNLOAD_MB:
        estimate.reasons.append(f"about {download_mb:.0f} MB to download ({plan.source.name})")
    if work_gb > CONFIRM_DISK_GB:
        estimate.reasons.append(f"working grids take about {work_gb:.1f} GB of disk in {plan.paths.work}")
    return estimate


def prepare_data(plan: Plan) -> Data:
    """Step 1: download what is missing and build the grids (terrain, surface, water, gaps)."""
    p, grid, source, emit = plan.params, plan.grid, plan.source, plan.emit
    cache_dir, work = plan.paths.cache, plan.paths.work
    emit("info", f"\nStep 1: Downloading elevation data ({source.name})...")
    try:
        elevation_path = src.prepare_layer(source.nmt, grid, cache_dir,
                                           os.path.join(work, "nmt.npy"), "terrain", p.refresh)
        surface_path = None
        if plan.nmpt:
            surface_path = src.prepare_layer(source.nmpt, grid, cache_dir,
                                             os.path.join(work, "nmpt.npy"), "surface", p.refresh)
        # Order matters: where the sea is, then gaps on land (outside the sea), then the water
        water_path = filled_path = sea_zone = None
        if p.water == "fill":
            wbm = src.water_body_mask(grid, cache_dir, os.path.join(work, "wbm.npy"), p.refresh)
            sea, coast_source, warning = coast.sea_mask(grid, cache_dir, p.coastline, wbm == src.OCEAN, p.refresh)
            emit("info", f"Coastline: {coast_source}")
            if warning:
                emit("warning", warning)
            sea_zone = src.near(sea, p.shore_buffer, grid.res)
        elif p.fill_gaps == "on" and source.fallback:
            # Without --water fill the sea stays without data; keep the gap filling off it too
            wbm = src.water_body_mask(grid, cache_dir, os.path.join(work, "wbm.npy"), p.refresh)
            sea_zone = src.near(wbm == src.OCEAN, src.SHORE_BUFFER, grid.res)
        if p.fill_gaps == "on" and source.fallback:
            fallback = src.SOURCES[source.fallback]
            filled_path, filled_px = src.fill_gaps(elevation_path, surface_path, grid, fallback, cache_dir,
                                                   os.path.join(work, "filled.npy"), p.refresh, exclude=sea_zone)
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
    p, grid, emit = plan.params, plan.grid, plan.emit

    # Step 2: Find raster pixels with significant slope for promising anchors
    emit("info", "\nStep 2: Finding significant slopes...")
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
        raise SearchError("Not enough steep terrain. Try a lower --slope-threshold or a larger --radius.")

    # Step 3: Compare each promising anchor to every other anchor
    emit("info", "\nStep 3: Evaluating anchor pairs...")
    sag_table = None
    if p.model == "sag":
        emit("info", "Precomputing the sag estimator for all line lengths...")
        sag_table = sg.EnvelopeTable(plan.rigging, p.min_length, p.max_length)
    valid_lines = ls.find_valid_lines(data.elevation, data.surface, anchors, plan.resolution,
                                      p.min_length, p.max_length, plan.nmpt, p.threads,
                                      sag_table, p.max_anchor_diff / 100, p.max_anchor_diff_m)
    if not valid_lines:
        raise SearchError("No lines found matching elevation requirements.")

    # Step 4: Convert pixel coordinates to GPS coordinates
    emit("info", "\nStep 4: Converting pixel coordinates to GPS coordinates...")
    results = mp.coordify(valid_lines, grid, plan.nmpt)
    progress.compute.start("line profiles", len(results), unit="line")
    for r, line in zip(results, valid_lines):
        r["profile"] = sh.line_profile(line.a1, line.a2, data.elevation, plan.resolution, data.surface,
                                       sag_table=sag_table, water=data.water, filled=data.filled)
        r["water_m"] = r["profile"]["water_m"]
        r["filled_m"] = r["profile"]["filled_m"]
        progress.compute.update(1)
    progress.compute.finish()
    spot_groups = mp.group_lines(results, plan.group_distance, p.group_by)
    return Lines(results, spot_groups)


def save_run(plan: Plan, lines: Lines, confirm: Optional[Confirm] = None) -> Result:
    """Step 5: save the run (results, parameters, data used), the orthophoto tiles and the map."""
    p, emit = plan.params, plan.emit
    results, spot_groups = lines.results, lines.spot_groups
    confirm = confirm or (lambda kind, text: True)
    emit("info", "\nStep 5: Saving the run and displaying results on map...")
    center_lat, center_lon = plan.area.center
    x, y = plan.center_xy
    created = datetime.now()
    name = p.name or rn.place_name(center_lat, center_lon, plan.paths.cache) or f"{center_lat:.5f}, {center_lon:.5f}"
    run = rn.new_run(plan.paths.runs, name, created)
    meta = {
        "id": run["id"],
        "name": name,
        "created": created.isoformat(timespec="seconds"),
        "center": {"lat": center_lat, "lon": center_lon, "x": x, "y": y, "crs": plan.crs},
        "area": plan.area.describe(),
        "source": {"id": plan.source.id, "name": plan.source.name, "description": plan.source.description},
        "data": src.used_data,  # folders, files and fetch times of the data used
        "params": {"radius": p.radius, "resolution": plan.resolution,
                   "min_length": p.min_length, "max_length": p.max_length,
                   "max_anchor_diff": p.max_anchor_diff,
                   "max_anchor_diff_m": p.max_anchor_diff_m,
                   "slope_threshold": p.slope_threshold, "slope_file": p.slope_file,
                   "nmpt": plan.nmpt, "water": p.water, "coastline": p.coastline,
                   "shore_buffer": p.shore_buffer, "fill_gaps": p.fill_gaps,
                   "group_distance": plan.group_distance,
                   "group_by": p.group_by, "model": p.model,
                   "rigging": dataclasses.asdict(plan.rigging)},
        "summary": {"lines": len(results), "spots": len(spot_groups),
                    "clear_spots": sum(not g["rep"]["over_obstacles"] for g in spot_groups)},
    }
    meta["description"] = rn.params_summary(meta)
    rn.save_results(run["path"], spot_groups, meta)
    emit("info", f"Run \"{name}\" saved to {os.path.abspath(run['path'])}")

    map_path = None
    if not p.no_html:
        source = plan.source
        ortho_cache = None
        if source.ortho and p.ortho != "never":
            # Detailed orthophoto only around one line per group (variants lie within the strip)
            spots = [g["rep"] for g in spot_groups]
            ortho_folder = ortho.cache_folder(plan.paths.cache, source.ortho)
            tiles = ortho.missing_tiles(ortho.tiles_to_cache(spots, plan.area), ortho_folder)
            minutes = len(tiles) * ORTHO_SECONDS_PER_TILE / 60
            emit("info", f"Orthophoto for offline use: {len(tiles)} tiles to download "
                         f"(~{len(tiles) * ortho.APPROX_TILE_KB / 1000:.0f} MB, ~{minutes:.0f} min)")
            if p.ortho == "auto" and minutes > CONFIRM_MINUTES and not confirm(
                    "ortho", "Download now? Otherwise the map loads the orthophoto online. [y/N] "):
                tiles = []
            ortho.download(source.ortho, tiles, ortho_folder, plan.paths.cache)
            ortho_cache = ortho_folder
        map_path = os.path.join(run["path"], "map.html")
        mp.save_html_map(results, plan.area, map_path, plan.group_distance, p.group_by, ortho_cache,
                         source.ortho, meta, plan.paths.runs, p.model, plan.rigging)
    rn.write_index(plan.paths.runs)
    return Result(results, spot_groups, meta, run, map_path)


def run_search(params: SearchParams, paths: Paths, on_event: Optional[OnEvent] = None,
               confirm: Optional[Confirm] = None) -> Result:
    """The whole search; confirm("expensive", reasons) is asked before downloading."""
    plan = plan_search(params, paths, on_event)
    estimate = estimate_search(plan)
    if estimate.reasons and confirm and not confirm("expensive", "\n".join(estimate.reasons)):
        raise SearchError("Stopped.")
    return save_run(plan, find_lines(plan, prepare_data(plan)), confirm)
