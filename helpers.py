"""
Terrain analysis: finding promising anchors and evaluating highlines.

All rasters share one working grid (see geoportal.Grid). Pixel coordinates are
[row, col]; distances and elevations are in metres.
"""

import time
from multiprocessing import Pool, cpu_count
from typing import List, NamedTuple, Optional, Tuple

import numpy as np
from scipy.spatial import cKDTree

from progress import progress

# Maximum elevation formula: max_elev = min_anchor_elev - SLOPE * distance - LEASH_FALL
LEASH_FALL = 6.0  # metres below the anchors for a 0 m line
SLOPE = 0.15      # metres of extra clearance per metre of distance from the nearest anchor


class Line(NamedTuple):
    """A valid highline between two anchors."""
    a1: Tuple[int, int]            # [row, col] of anchor 1
    a2: Tuple[int, int]            # [row, col] of anchor 2
    length: float                  # metres
    elev_a1: float                 # NMT elevation of anchor 1
    elev_a2: float                 # NMT elevation of anchor 2
    clearance: float               # smallest margin below the max elevation over terrain (NMT)
    clearance_nmpt: Optional[float]  # same, over trees/buildings (NMPT); None if NMPT not used


def find_significant_slopes(elevation: np.ndarray, res: float, slope_threshold: float,
                            center: Tuple[float, float], radius_px: float,
                            slope: Optional[np.ndarray] = None,
                            block_rows: int = 512) -> np.ndarray:
    """Finds pixels steep enough to be promising anchors.

    Slope is computed from the elevation model unless a slope raster (in degrees)
    is given. Only pixels inside the search circle are returned.

    Args:
        elevation: NMT elevation grid
        res: Pixel size in metres
        slope_threshold: Minimum slope in degrees
        center: (row, col) of the search centre
        radius_px: Search radius in pixels
        slope: Optional slope grid in degrees, aligned with elevation
        block_rows: Rows processed at once (keeps memory use bounded)

    Returns:
        Array of [row, col] anchor coordinates (int32)
    """
    height, width = elevation.shape
    center_row, center_col = center
    anchors = []

    progress.compute.start("finding anchors", height, unit="row")
    for r0 in range(0, height, block_rows):
        r1 = min(height, r0 + block_rows)
        if slope is None:
            # One extra row on each side so the gradient is correct at block edges
            h0, h1 = max(0, r0 - 1), min(height, r1 + 1)
            z = np.asarray(elevation[h0:h1], dtype=np.float32)
            gy, gx = np.gradient(z, res)
            block = np.degrees(np.arctan(np.hypot(gx, gy)))[r0 - h0:r0 - h0 + (r1 - r0)]
        else:
            block = np.asarray(slope[r0:r1])

        rows, cols = np.nonzero(block >= slope_threshold)  # NaN compares False
        rows += r0
        inside = (rows - center_row) ** 2 + (cols - center_col) ** 2 <= radius_px ** 2
        anchors.append(np.column_stack([rows[inside], cols[inside]]).astype(np.int32))
        progress.compute.update(r1 - r0)
    progress.compute.finish()
    return np.concatenate(anchors) if anchors else np.empty((0, 2), dtype=np.int32)


def max_safe_elevation(t: np.ndarray, length: float, elev_a1: float, elev_a2: float,
                       sag_table=None) -> np.ndarray:
    """Highest allowed terrain/obstacle elevation at fractions t of a line.

    Without sag_table: the maximum elevation formula (see README).
    With a sag.EnvelopeTable: the chord between the anchors, minus the sag under the
    athlete (envelope from the sag estimator), minus the fall zone.
    """
    if sag_table is None:
        return min(elev_a1, elev_a2) - SLOPE * np.minimum(t, 1 - t) * length - LEASH_FALL
    chord = elev_a1 + (elev_a2 - elev_a1) * t
    return chord - sag_table(length, elev_a1 - elev_a2, t) - sag_table.rig.fall_zone


def evaluate_line(a1: np.ndarray, a2: np.ndarray, elevation: np.ndarray, res: float,
                  surface: Optional[np.ndarray] = None, sag_table=None
                  ) -> Optional[Tuple[float, float, float, float, Optional[float]]]:
    """Evaluates clearance along a line between two anchor points.

    Draws a line between the anchors using a DDA algorithm and, for every pixel
    in between, compares its elevation with the maximum safe elevation.

    Args:
        a1, a2: [row, col] anchor coordinates
        elevation: NMT elevation grid
        res: Pixel size in metres
        surface: Optional NMPT grid (trees, buildings)
        sag_table: sag.EnvelopeTable to use the sag estimator instead of the formula

    Returns:
        (elev_a1, elev_a2, length, min clearance over NMT, min clearance over NMPT or None),
        or None if the line has no pixels between the anchors.
        Positive clearance means the terrain is below the maximum safe elevation.
    """
    r1, c1 = int(a1[0]), int(a1[1])
    r2, c2 = int(a2[0]), int(a2[1])
    dr, dc = r2 - r1, c2 - c1
    steps = max(abs(dr), abs(dc))
    if steps < 2:
        return None

    # DDA: every pixel between the anchors (anchors themselves excluded)
    t = np.arange(1, steps) / steps
    rows = np.rint(r1 + dr * t).astype(np.intp)
    cols = np.rint(c1 + dc * t).astype(np.intp)

    length = float(np.hypot(dr, dc)) * res
    elev_a1 = float(elevation[r1, c1])
    elev_a2 = float(elevation[r2, c2])
    max_elev = max_safe_elevation(t, length, elev_a1, elev_a2, sag_table)

    # NaN (no data) makes the clearance NaN, so such lines never pass
    clearance = float(np.min(max_elev - elevation[rows, cols]))

    clearance_nmpt = None
    if surface is not None:
        obstacles = surface[rows, cols]
        if not np.all(np.isnan(obstacles)):
            clearance_nmpt = float(np.nanmin(max_elev - obstacles))

    return elev_a1, elev_a2, length, clearance, clearance_nmpt


def line_profile(a1: Tuple[int, int], a2: Tuple[int, int], elevation: np.ndarray, res: float,
                 surface: Optional[np.ndarray] = None, max_points: int = 200,
                 sag_table=None, water: Optional[np.ndarray] = None,
                 filled: Optional[np.ndarray] = None) -> dict:
    """Terrain (and NMPT) profile along a line, for the profile chart of the map.

    Every pixel between the anchors is sampled (as in evaluate_line), then the
    profile is thinned to at most max_points, always keeping both anchors and the
    points of the smallest clearance.

    Returns:
        {"d": distances from anchor 1 [m], "z": NMT [m], "s": NMPT [m] or None,
         "min": [distance, clearance] over NMT, "min_nmpt": [distance, clearance] or None,
         "w": 1 where the point is water, or None, "water_m": length over water [m],
         "g": 1 where the data was filled from another source, or None, "filled_m": that length [m]}
    """
    r1, c1 = a1
    r2, c2 = a2
    dr, dc = r2 - r1, c2 - c1
    steps = max(abs(dr), abs(dc))
    t = np.arange(steps + 1) / steps  # anchors included
    rows = np.rint(r1 + dr * t).astype(np.intp)
    cols = np.rint(c1 + dc * t).astype(np.intp)

    length = float(np.hypot(dr, dc)) * res
    distance = t * length
    terrain = np.asarray(elevation[rows, cols], dtype=np.float64)
    obstacles = None if surface is None else np.asarray(surface[rows, cols], dtype=np.float64)

    # Same limit as evaluate_line, anchors excluded from the minimum
    max_elev = max_safe_elevation(t, length, terrain[0], terrain[-1], sag_table)
    inner = slice(1, steps)
    i_min = 1 + int(np.nanargmin((max_elev - terrain)[inner]))
    keep = {0, steps, i_min}
    profile = {"min": [round(distance[i_min], 1), round(float(max_elev[i_min] - terrain[i_min]), 2)],
               "min_nmpt": None}
    if obstacles is not None and not np.all(np.isnan(obstacles[inner])):
        j_min = 1 + int(np.nanargmin((max_elev - obstacles)[inner]))
        keep.add(j_min)
        profile["min_nmpt"] = [round(distance[j_min], 1),
                               round(float(max_elev[j_min] - obstacles[j_min]), 2)]

    keep |= set(np.linspace(0, steps, min(max_points, steps + 1)).round().astype(int).tolist())
    idx = sorted(keep)

    def rounded(values):
        return [None if np.isnan(v) else round(float(v), 2) for v in values[idx]]

    profile.update(d=[round(float(v), 1) for v in distance[idx]], z=rounded(terrain),
                   s=None if obstacles is None else rounded(obstacles))
    for mask, flags, metres in ((water, "w", "water_m"), (filled, "g", "filled_m")):
        profile[flags], profile[metres] = None, 0.0
        if mask is not None:
            over = np.asarray(mask[rows, cols], dtype=bool)
            if over.any():
                profile[flags] = over[idx].astype(int).tolist()
                profile[metres] = round(float(over[1:steps].sum()) * length / steps, 1)
    return profile


def is_valid(result, nmpt_mode: Optional[str]) -> bool:
    """Decide whether an evaluated line is kept."""
    if result is None:
        return False
    _, _, _, clearance, clearance_nmpt = result
    if not clearance > 0:
        return False
    if nmpt_mode == "reject" and clearance_nmpt is not None and clearance_nmpt <= 0:
        return False
    return True


def pair_filter(i: int, partners: np.ndarray, anchors: np.ndarray, anchor_elev: np.ndarray,
                res: float, min_length: float, max_dh_ratio: float, max_dh: float) -> np.ndarray:
    """Which partners of anchor i make a candidate line.

    The line must be at least min_length long and the anchors may differ in
    elevation by at most max_dh_ratio of the line length, and never more than max_dh.
    """
    lengths = np.hypot(*(anchors[partners] - anchors[i]).T) * res
    dh = np.abs(anchor_elev[partners] - anchor_elev[i])
    return (lengths >= min_length) & (dh <= np.minimum(max_dh_ratio * lengths, max_dh))


# Data shared by each worker process (loaded once per worker)
_worker = {}


def _worker_init(elevation_path: str, surface_path: Optional[str], anchors_path: str,
                 res: float, min_length: float, max_length: float, nmpt_mode: Optional[str],
                 sag_table=None, max_dh_ratio: float = np.inf, max_dh: float = np.inf):
    """Initialize worker process - memory-map the grids and index the anchors."""
    anchors = np.load(anchors_path)
    elevation = np.load(elevation_path, mmap_mode="r")
    _worker.update(
        elevation=elevation,
        surface=np.load(surface_path, mmap_mode="r") if surface_path else None,
        anchors=anchors,
        anchor_elev=np.asarray(elevation[anchors[:, 0], anchors[:, 1]]),
        tree=cKDTree(anchors * res),
        res=res, min_length=min_length, max_length=max_length, nmpt_mode=nmpt_mode,
        sag_table=sag_table, max_dh_ratio=max_dh_ratio, max_dh=max_dh,
    )


def _search_chunk(bounds: Tuple[int, int]) -> Tuple[int, int, List[Line]]:
    """Evaluate every line starting at anchors[start:end].

    Each pair is evaluated once (only partners with a higher index are checked).
    Returns (anchors processed, pairs evaluated, valid lines).
    """
    w = _worker
    start, end = bounds
    anchors, res = w["anchors"], w["res"]
    neighbours = w["tree"].query_ball_point(anchors[start:end] * res, r=w["max_length"])

    evaluated = 0
    lines = []
    for k, partners in enumerate(neighbours):
        i = start + k
        partners = np.asarray(partners, dtype=np.intp)
        partners = partners[partners > i]
        if partners.size == 0:
            continue
        partners = partners[pair_filter(i, partners, anchors, w["anchor_elev"], res, w["min_length"],
                                        w["max_dh_ratio"], w["max_dh"])]
        evaluated += partners.size

        for j in partners:
            result = evaluate_line(anchors[i], anchors[j], w["elevation"], res, w["surface"],
                                   w["sag_table"])
            if is_valid(result, w["nmpt_mode"]):
                elev_a1, elev_a2, length, clearance, clearance_nmpt = result
                lines.append(Line(tuple(map(int, anchors[i])), tuple(map(int, anchors[j])),
                                  length, elev_a1, elev_a2, clearance, clearance_nmpt))
    return end - start, evaluated, lines


def estimate_search(anchors: np.ndarray, elevation: np.ndarray, res: float,
                    min_length: float, max_length: float,
                    surface: Optional[np.ndarray] = None, sag_table=None,
                    max_dh_ratio: float = np.inf, max_dh: float = np.inf,
                    sample_size: int = 2000) -> Tuple[float, float]:
    """Estimate the number of anchor pairs and the time to evaluate one pair.

    Pairs are counted with the same filter as the search (length, height difference)
    for a random sample of anchors.

    Returns:
        (estimated pairs, seconds per pair on a single core)
    """
    if len(anchors) < 2:
        return 0, 0
    rng = np.random.default_rng(0)
    points = anchors * res
    tree = cKDTree(points)
    sample = rng.choice(len(anchors), min(len(anchors), sample_size), replace=False)
    anchor_elev = np.asarray(elevation[anchors[:, 0], anchors[:, 1]])
    candidates = []
    for i, partners in zip(sample, tree.query_ball_point(points[sample], r=max_length)):
        partners = np.asarray(partners, dtype=np.intp)
        partners = partners[partners != i]
        candidates.append(partners[pair_filter(i, partners, anchors, anchor_elev, res, min_length,
                                               max_dh_ratio, max_dh)])
    pairs = len(anchors) * float(np.mean([len(c) for c in candidates])) / 2

    # Time a handful of real evaluations
    timed, start = 0, time.perf_counter()
    for i, partners in zip(sample[:200], candidates[:200]):
        for j in partners[:20]:
            evaluate_line(anchors[i], anchors[j], elevation, res, surface, sag_table)
            timed += 1
    seconds_per_pair = (time.perf_counter() - start) / timed if timed else 0
    return pairs, seconds_per_pair


def find_valid_lines(elevation_path: str, surface_path: Optional[str], anchors_path: str,
                     num_anchors: int, res: float, min_length: float, max_length: float,
                     nmpt_mode: Optional[str] = None,
                     num_processes: Optional[int] = None, sag_table=None,
                     max_dh_ratio: float = np.inf, max_dh: float = np.inf) -> List[Line]:
    """Finds valid lines between anchors using parallel processing.

    Anchor pairs are generated on the fly with a KD-tree (only pairs within the
    length constraints), so memory use does not grow with the number of pairs.

    Args:
        elevation_path: .npy file with the NMT grid
        surface_path: .npy file with the NMPT grid, or None
        anchors_path: .npy file with [row, col] anchors
        num_anchors: Number of anchors in anchors_path
        res: Pixel size in metres
        min_length, max_length: Line length constraints in metres
        nmpt_mode: None, "mark" or "reject"
        num_processes: Number of parallel processes (defaults to CPU count - 1)
        sag_table: sag.EnvelopeTable to use the sag estimator instead of the formula
        max_dh_ratio, max_dh: Limits of the anchor height difference (see pair_filter)

    Returns:
        List of valid lines
    """
    if num_processes is None:
        num_processes = max(1, cpu_count() - 1)  # Leave one core free for system

    chunk = max(50, min(2000, num_anchors // (num_processes * 20) or 1))
    chunks = [(s, min(s + chunk, num_anchors)) for s in range(0, num_anchors, chunk)]
    print(f"Using {num_processes} parallel processes, {len(chunks)} work chunks...")

    start_time = time.time()
    evaluated = 0
    valid_lines: List[Line] = []
    progress.compute.start("anchor pairs", num_anchors, unit="anchor")

    initargs = (elevation_path, surface_path, anchors_path, res, min_length, max_length, nmpt_mode,
                sag_table, max_dh_ratio, max_dh)
    with Pool(processes=num_processes, initializer=_worker_init, initargs=initargs) as pool:
        for n_anchors, n_pairs, lines in pool.imap_unordered(_search_chunk, chunks):
            evaluated += n_pairs
            valid_lines.extend(lines)
            progress.compute.update(n_anchors, pairs=f"{evaluated:,}", lines=len(valid_lines))
    progress.compute.finish(f"{len(valid_lines)} lines")

    elapsed_total = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"Completed in {elapsed_total:.2f} seconds")
    print(f"Evaluated {evaluated} candidate pairs")
    print(f"Found {len(valid_lines)} valid lines")
    if elapsed_total > 0:
        print(f"Average rate: {evaluated / elapsed_total:.0f} pairs/second")
    print(f"{'=' * 60}")

    valid_lines.sort(key=lambda line: line.length, reverse=True)
    return valid_lines
