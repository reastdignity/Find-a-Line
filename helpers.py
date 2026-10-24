"""
Terrain analysis: finding promising anchors and evaluating highlines.
The search over all anchor pairs is in linesearch.py.

All rasters share one working grid (see geoportal.Grid). Pixel coordinates are
[row, col]; distances and elevations are in metres.
"""

from typing import NamedTuple, Optional, Tuple

import numpy as np

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
                            area_mask: np.ndarray, slope: Optional[np.ndarray] = None,
                            block_rows: int = 512) -> np.ndarray:
    """Finds pixels steep enough to be promising anchors.

    Slope is computed from the elevation model unless a slope raster (in degrees)
    is given. Only pixels inside the search area are returned.

    Args:
        elevation: NMT elevation grid
        res: Pixel size in metres
        slope_threshold: Minimum slope in degrees
        area_mask: True inside the search area (area.Area.mask), aligned with elevation
        slope: Optional slope grid in degrees, aligned with elevation
        block_rows: Rows processed at once (keeps memory use bounded)

    Returns:
        Array of [row, col] anchor coordinates (int32)
    """
    height, width = elevation.shape
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
        inside = area_mask[rows, cols]
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
