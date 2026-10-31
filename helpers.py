"""
Terrain analysis: finding promising anchors and evaluating highlines.
The search over all anchor pairs is in linesearch.py.

All rasters share one working grid (see geoportal.Grid). Pixel coordinates are
[row, col]; distances and elevations are in metres.
"""

from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Tuple

import numpy as np

from progress import progress

# Maximum elevation formula: max_elev = min_anchor_elev - SLOPE * distance - LEASH_FALL
LEASH_FALL = 6.0  # metres below the anchors for a 0 m line
SLOPE = 0.15      # metres of extra clearance per metre of distance from the nearest anchor
HIGH_ABOVE = 20.0  # metres above the terrain from which a pixel of a line counts as high (--high-above)


class Line(NamedTuple):
    """A valid highline between two anchors."""
    a1: Tuple[int, int]            # [row, col] of anchor 1
    a2: Tuple[int, int]            # [row, col] of anchor 2
    length: float                  # metres
    elev_a1: float                 # NMT elevation of anchor 1
    elev_a2: float                 # NMT elevation of anchor 2
    clearance: float               # smallest margin below the max elevation over terrain (NMT)
    clearance_nmpt: Optional[float]  # same, over trees/buildings (NMPT); None if NMPT not used
    height_max: float              # largest height of the webbing above the terrain (webbing_height)
    height_mean: float             # its mean over the pixels between the anchors
    high_pct: float                # % of those pixels where it is at least high_above
    angle1: float                  # degrees between the line and the downslope direction at anchor 1
    angle2: float                  # the same at anchor 2 (wall_angles)
    prom1: float                   # prominence of anchor 1 (terrain.prominence; NaN: not computed)
    prom2: float
    drop1: float                   # drop below anchor 1 (terrain.drop)
    drop2: float


# The values of a line in a LineTable: Line's fields after the anchors, float64 each
LINE_VALUES = Line._fields[2:]


@dataclass
class LineTable:
    """Lines as numpy columns, one row per line: a few hundred bytes per line where a Line and
    the dict of its coordinates took about 2.5 KB (docs/performance.md).

    columns: "a1", "a2" ([row, col], int64, n x 2), the LINE_VALUES (float64; clearance_nmpt NaN
    when the surface was not measured there), and after mapping.coordify "anchor1", "anchor2"
    ([lat, lon]), "a1_xy", "a2_xy" ([easting, northing] in the grid CRS), "over_obstacles"
    (bool), "water_m", "filled_m". profiles: the profile of a row (helpers.line_profile), only for
    the rows that have one. row(k) gives a line as the dict the rest of the code knew.
    """
    columns: Dict[str, np.ndarray]
    profiles: Dict[int, Dict] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(next(iter(self.columns.values())))

    @classmethod
    def from_rows(cls, rows: List[Dict]) -> "LineTable":
        """A table of lines given as dicts (as row() gives them; tests make lines this way)."""
        names = [k for k in rows[0] if k != "profile"]
        columns = {}
        for name in names:
            values = [r[name] for r in rows]
            if name == "clearance_nmpt":
                values = [np.nan if v is None else v for v in values]
            columns[name] = np.array(values, dtype=bool if name == "over_obstacles" else
                                     np.int64 if name in ("a1", "a2") else np.float64)
        return cls(columns, {k: r["profile"] for k, r in enumerate(rows) if r.get("profile") is not None})

    @classmethod
    def empty(cls) -> "LineTable":
        return cls({"a1": np.zeros((0, 2), dtype=np.int64), "a2": np.zeros((0, 2), dtype=np.int64),
                    **{k: np.zeros(0) for k in LINE_VALUES}})

    def take(self, index) -> "LineTable":
        """The rows at index (an array of row numbers or a boolean mask), in that order."""
        index = np.asarray(index)
        if index.dtype == bool:
            index = np.flatnonzero(index)
        new = {int(old): k for k, old in enumerate(index.tolist()) if old in self.profiles}
        return LineTable({name: column[index] for name, column in self.columns.items()},
                         {k: self.profiles[old] for old, k in new.items()})

    @classmethod
    def concat(cls, tables: List["LineTable"]) -> "LineTable":
        """The rows of several tables (with the same columns), one after another."""
        tables = [t for t in tables if len(t)] or tables[:1] or [cls.empty()]
        profiles, start = {}, 0
        for t in tables:
            profiles.update({start + k: p for k, p in t.profiles.items()})
            start += len(t)
        return cls({name: np.concatenate([t.columns[name] for t in tables]) for name in tables[0].columns},
                   profiles)

    def row(self, k: int) -> Dict:
        """Row k as a dict of Python values: pairs as tuples, clearance_nmpt None when not measured."""
        out = {}
        for name, column in self.columns.items():
            value = column[k].tolist()
            out[name] = tuple(value) if isinstance(value, list) else value
        if out["clearance_nmpt"] != out["clearance_nmpt"]:
            out["clearance_nmpt"] = None
        out["profile"] = self.profiles.get(k)
        return out

    def lines(self) -> List[Line]:
        """The rows as Line tuples (tests compare them with helpers.evaluate_line)."""
        values = [self.columns[k].tolist() for k in LINE_VALUES]
        return [Line(tuple(a1), tuple(a2), *v[:4], None if v[4] != v[4] else v[4], *v[5:])
                for a1, a2, *v in zip(self.columns["a1"].tolist(), self.columns["a2"].tolist(), *values)]


@dataclass
class LineLimits:
    """Search limits of the lines (None: no limit): the webbing heights above the terrain
    (--min-height, --min-mean-height, --min-high-share as % of high_above) and the angle
    to the wall at both anchors (--max-wall-angle)."""
    min_height: Optional[float] = None
    min_mean_height: Optional[float] = None
    min_high_share: Optional[float] = None
    max_wall_angle: Optional[float] = None
    left_out: int = 0  # valid lines that did not reach the limits

    @property
    def active(self) -> bool:
        return any(v is not None for v in (self.min_height, self.min_mean_height, self.min_high_share,
                                           self.max_wall_angle))

    def passes(self, height_max: np.ndarray, height_mean: np.ndarray, high_pct: np.ndarray, angle1: np.ndarray,
               angle2: np.ndarray, valid: np.ndarray) -> np.ndarray:
        """Which of the valid lines (columns of their values) reach the limits; counts the others."""
        ok = valid.copy()
        if self.min_height is not None:
            ok &= height_max >= self.min_height
        if self.min_mean_height is not None:
            ok &= height_mean >= self.min_mean_height
        if self.min_high_share is not None:
            ok &= high_pct >= self.min_high_share
        if self.max_wall_angle is not None:
            ok &= np.where(angle2 > angle1, angle2, angle1) <= self.max_wall_angle  # max() of Python
        self.left_out += int(valid.sum() - ok.sum())
        return ok

    def describe(self) -> str:
        parts = [f"{text} {v:g}" for text, v in (("height ≥", self.min_height), ("mean ≥", self.min_mean_height),
                                                  ("high % ≥", self.min_high_share),
                                                  ("wall angle ≤", self.max_wall_angle)) if v is not None]
        return ", ".join(parts)


def downslope(elevation: np.ndarray, anchors: np.ndarray, res: float) -> np.ndarray:
    """Unit vectors (row, col) of the downslope direction at the anchors: the Horn gradient
    of the 3 x 3 pixels around each (pixels outside the grid or without data take the
    anchor's elevation); (0, 0) where the terrain is flat."""
    anchors = np.asarray(anchors, dtype=np.int64).reshape(-1, 2)
    h, w = elevation.shape
    z = np.empty((len(anchors), 3, 3))
    centre = np.asarray(elevation[anchors[:, 0], anchors[:, 1]], dtype=np.float64)
    for i in range(3):
        for j in range(3):
            r, c = anchors[:, 0] + i - 1, anchors[:, 1] + j - 1
            inside = (r >= 0) & (r < h) & (c >= 0) & (c < w)
            values = np.full(len(anchors), np.nan)
            values[inside] = elevation[r[inside], c[inside]]
            z[:, i, j] = np.where(np.isnan(values), centre, values)
    weights = np.array([1.0, 2.0, 1.0])
    d_col = ((z[:, :, 2] - z[:, :, 0]) @ weights) / (8 * res)
    d_row = ((z[:, 2, :] - z[:, 0, :]) @ weights) / (8 * res)
    vector = -np.column_stack([d_row, d_col])
    norm = np.hypot(vector[:, 0], vector[:, 1])
    return np.divide(vector, norm[:, None], out=np.zeros_like(vector), where=norm[:, None] > 0)


def wall_angles(a1: np.ndarray, a2: np.ndarray, down1: np.ndarray, down2: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Degrees (0-180) between each line, seen from its anchor, and the downslope direction
    there (downslope): 0 the line leaves the edge straight over the drop, 90 it runs along
    the wall (or the terrain at the anchor is flat)."""
    d = (np.asarray(a2, dtype=np.float64) - np.asarray(a1, dtype=np.float64)).reshape(-1, 2)
    n = np.hypot(d[:, 0], d[:, 1])
    with np.errstate(invalid="ignore"):  # a pair of the same pixel (never a line): NaN
        cos1 = (d * down1).sum(axis=1) / n
        cos2 = -(d * down2).sum(axis=1) / n
    return tuple(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))) for c in (cos1, cos2))


def fall_allowance(sag_table=None) -> float:
    """What max_safe_elevation keeps free below the webbing: the leash fall of the formula,
    or the fall zone below the sagged webbing."""
    return LEASH_FALL if sag_table is None else float(sag_table.rig.fall_zone)


def webbing_height(margin, sag_table=None):
    """Height of the webbing above the terrain from the margin below max_safe_elevation.

    The webbing is the limit plus fall_allowance: with the sag estimator the chord minus the
    sag under the athlete, with the formula min_anchor_elev - SLOPE * distance.
    """
    return margin + fall_allowance(sag_table)


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
                  surface: Optional[np.ndarray] = None, sag_table=None, high_above: float = HIGH_ABOVE
                  ) -> Optional[Tuple[float, float, float, float, Optional[float], float, float, float]]:
    """Evaluates clearance along a line between two anchor points.

    Draws a line between the anchors using a DDA algorithm and, for every pixel
    in between, compares its elevation with the maximum safe elevation.

    Args:
        a1, a2: [row, col] anchor coordinates
        elevation: NMT elevation grid
        res: Pixel size in metres
        surface: Optional NMPT grid (trees, buildings)
        sag_table: sag.EnvelopeTable to use the sag estimator instead of the formula
        high_above: Height above the terrain from which a pixel counts as high

    Returns:
        (elev_a1, elev_a2, length, min clearance over NMT, min clearance over NMPT or None,
        largest and mean height of the webbing above NMT, % of the pixels at least
        high_above), or None if the line has no pixels between the anchors.
        Positive clearance means the terrain is below the maximum safe elevation.
    """
    r1, c1 = int(a1[0]), int(a1[1])
    r2, c2 = int(a2[0]), int(a2[1])
    dr, dc = r2 - r1, c2 - c1
    steps = max(abs(dr), abs(dc))
    if steps < 2:
        return None

    # DDA: every pixel between the anchors (anchors themselves excluded). The offset from
    # anchor 1 is rounded, not the position: np.rint rounds halves to even, so rounding the
    # position would pick other pixels on a grid shifted by an odd number of pixels
    t = np.arange(1, steps) / steps
    rows = r1 + np.rint(dr * t).astype(np.intp)
    cols = c1 + np.rint(dc * t).astype(np.intp)

    length = float(np.hypot(dr, dc)) * res
    elev_a1 = float(elevation[r1, c1])
    elev_a2 = float(elevation[r2, c2])
    max_elev = max_safe_elevation(t, length, elev_a1, elev_a2, sag_table)

    # NaN (no data) makes the clearance NaN, so such lines never pass
    margin = max_elev - elevation[rows, cols]
    clearance = float(np.min(margin))
    # Summed one pixel after another, as linesearch._evaluate: np.cumsum, not np.mean (pairwise)
    # or sum() (compensated since Python 3.12)
    heights = webbing_height(margin, sag_table)
    height_max = float(np.max(heights))
    height_mean = float(np.cumsum(heights)[-1]) / len(heights)
    high_pct = 100.0 * int(np.count_nonzero(heights >= high_above)) / len(heights)

    clearance_nmpt = None
    if surface is not None:
        obstacles = surface[rows, cols]
        if not np.all(np.isnan(obstacles)):
            clearance_nmpt = float(np.nanmin(max_elev - obstacles))

    return elev_a1, elev_a2, length, clearance, clearance_nmpt, height_max, height_mean, high_pct


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
         "max_height": [distance, height] of the webbing highest above NMT (webbing_height),
         "w": 1 where the point is water, or None, "water_m": length over water [m],
         "g": 1 where the data was filled from another source, or None, "filled_m": that length [m]}
    """
    r1, c1 = a1
    r2, c2 = a2
    dr, dc = r2 - r1, c2 - c1
    steps = max(abs(dr), abs(dc))
    t = np.arange(steps + 1) / steps  # anchors included
    rows = r1 + np.rint(dr * t).astype(np.intp)  # the same pixels as evaluate_line
    cols = c1 + np.rint(dc * t).astype(np.intp)

    length = float(np.hypot(dr, dc)) * res
    distance = t * length
    terrain = np.asarray(elevation[rows, cols], dtype=np.float64)
    obstacles = None if surface is None else np.asarray(surface[rows, cols], dtype=np.float64)

    # Same limit as evaluate_line, anchors excluded from the minimum
    max_elev = max_safe_elevation(t, length, terrain[0], terrain[-1], sag_table)
    inner = slice(1, steps)
    i_min = 1 + int(np.nanargmin((max_elev - terrain)[inner]))
    i_max = 1 + int(np.nanargmax((max_elev - terrain)[inner]))
    keep = {0, steps, i_min, i_max}
    profile = {"min": [round(distance[i_min], 1), round(float(max_elev[i_min] - terrain[i_min]), 2)],
               "min_nmpt": None,
               "max_height": [round(distance[i_max], 1),
                              round(float(webbing_height(max_elev[i_max] - terrain[i_max], sag_table)), 2)]}
    if obstacles is not None and not np.all(np.isnan(obstacles[inner])):
        j_min = 1 + int(np.nanargmin((max_elev - obstacles)[inner]))
        keep.add(j_min)
        profile["min_nmpt"] = [round(distance[j_min], 1),
                               round(float(max_elev[j_min] - obstacles[j_min]), 2)]

    keep |= set(np.linspace(0, steps, min(max_points, steps + 1)).round().astype(int).tolist())
    idx = sorted(keep)

    def rounded(values):
        return [None if v != v else round(v, 2) for v in values[idx].tolist()]  # v != v: NaN

    profile.update(d=[round(v, 1) for v in distance[idx].tolist()], z=rounded(terrain),
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
    clearance, clearance_nmpt = result[3], result[4]
    if not clearance > 0:
        return False
    if nmpt_mode == "reject" and clearance_nmpt is not None and clearance_nmpt <= 0:
        return False
    return True
