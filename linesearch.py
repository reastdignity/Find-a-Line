"""
Fast search for valid lines between anchors (numba).

Three steps, none of which loses a valid line:
1. Free rings. For every anchor and each of SECTORS directions: how many rings of
   pixels around the anchor a line in that direction can cross before the terrain
   is certainly too high, using the weakest limit possible for any partner. Anchors
   with no open direction are dropped.
2. Pairs. Partners are found on a grid of buckets and must pass the length and
   height-difference limits, and both anchors must be open towards each other.
3. Exact check. The pixels of the line are checked from both anchors inwards and
   the check stops at the first pixel that is too high (most lines fail next to an
   anchor).
The kernels keep pairs within EPS of the limit; the pairs found are evaluated
once more by helpers.evaluate_line, which makes the final decision and gives the
clearances, so the results are exactly those of the plain pixel-by-pixel search.
"""

import math
import time
from typing import List, Optional

import numba
import numpy as np
from numba import njit, prange

import helpers as sh
from progress import progress

SECTORS = 64   # direction sectors per anchor
RINGS = 64     # rings of pixels around an anchor checked per sector
EPS = 1e-6     # metres; borderline pairs are passed on to evaluate_line
CHUNKS = 200   # anchors are processed in this many parts (progress updates)


@njit(cache=True)
def _index(x, xs):
    """Fractional position of x in the increasing array xs, clamped to its ends."""
    n = xs.size
    if x <= xs[0]:
        return 0.0
    if x >= xs[n - 1]:
        return float(n - 1)
    k = np.searchsorted(xs, x, side="right") - 1
    return k + (x - xs[k]) / (xs[k + 1] - xs[k])


@njit(cache=True)
def _line_ok(z, r1, c1, r2, c2, res, use_sag, log_spans, ratios, table, fall_zone):
    """Whether the terrain stays below the maximum safe elevation along the line.

    The same limit as helpers.max_safe_elevation (formula or sag table), within EPS.
    """
    dr, dc = r2 - r1, c2 - c1
    steps = max(abs(dr), abs(dc))
    if steps < 2:
        return False
    length = math.hypot(dr, dc) * res
    e1, e2 = np.float64(z[r1, c1]), np.float64(z[r2, c2])
    emin = min(e1, e2)
    i0, j0, a, b, points = 0, 0, 0.0, 0.0, table.shape[2]
    if use_sag:
        si = _index(math.log(length), log_spans)
        ri = _index((e1 - e2) / length, ratios)
        i0, j0 = min(int(si), log_spans.size - 2), min(int(ri), ratios.size - 2)
        a, b = si - i0, ri - j0

    lo, hi = 1, steps - 1
    while lo <= hi:
        for k in (lo, hi):
            t = k / steps
            r = r1 + int(np.rint(dr * t))  # the pixels of helpers.evaluate_line
            c = c1 + int(np.rint(dc * t))
            if use_sag:
                x = t * (points - 1)
                f = min(int(x), points - 2)
                w = x - f
                s0 = ((1 - a) * (1 - b) * table[i0, j0, f] + a * (1 - b) * table[i0 + 1, j0, f]
                      + (1 - a) * b * table[i0, j0 + 1, f] + a * b * table[i0 + 1, j0 + 1, f])
                s1 = ((1 - a) * (1 - b) * table[i0, j0, f + 1] + a * (1 - b) * table[i0 + 1, j0, f + 1]
                      + (1 - a) * b * table[i0, j0 + 1, f + 1] + a * b * table[i0 + 1, j0 + 1, f + 1])
                limit = e1 + (e2 - e1) * t - (s0 * (1 - w) + s1 * w) - fall_zone
            else:
                limit = emin - sh.SLOPE * min(t, 1 - t) * length - sh.LEASH_FALL
            if not (limit - np.float64(z[r, c]) > -EPS):  # NaN (no data) fails too
                return False
        lo += 1
        hi -= 1
    return True


@njit(parallel=True, cache=True)
def _free_rings(z, anchors, res, gain, cap, offset):
    """For every anchor and sector: the number of rings a line can cross from the anchor.

    The k-th pixel of a line (DDA) lies on ring k (Chebyshev distance k) and at most
    half a pixel from the exact line, so its direction differs from the line's by at
    most asin(0.71 / k). A sector stays open at ring k if some pixel that may belong
    to a line in it is below e + min(gain * k * res, cap) - offset, where e is the
    elevation of the anchor: no limit for any partner is weaker than that.
    """
    n = anchors.shape[0]
    h, w = z.shape
    rings = np.zeros((n, SECTORS), dtype=np.int32)
    width = 2 * np.pi / SECTORS
    for a in prange(n):
        r0, c0 = anchors[a, 0], anchors[a, 1]
        e = np.float64(z[r0, c0])
        alive = np.ones(SECTORS, dtype=np.bool_)
        below = np.zeros(SECTORS, dtype=np.bool_)
        for k in range(1, RINGS + 1):
            below[:] = False
            limit = e + min(gain * k * res, cap) - offset + EPS
            delta = math.asin(min(1.0, 0.7072 / k))
            for i in range(-k, k + 1):
                step = 1 if abs(i) == k else 2 * k  # whole top/bottom rows, two ends otherwise
                for j in range(-k, k + 1, step):
                    rr, cc = r0 + i, c0 + j
                    if rr < 0 or cc < 0 or rr >= h or cc >= w:
                        continue
                    if not (np.float64(z[rr, cc]) < limit):
                        continue
                    angle = math.atan2(i, j)
                    for s in range(int(math.floor((angle - delta) / width)),
                                   int(math.floor((angle + delta) / width)) + 1):
                        below[s % SECTORS] = True
            open_any = False
            for s in range(SECTORS):
                if alive[s] and below[s]:
                    rings[a, s] = k
                    open_any = True
                else:
                    alive[s] = False
            if not open_any:
                break
    return rings


@njit(cache=True)
def _sector(dr, dc):
    return int(math.floor(math.atan2(dr, dc) / (2 * np.pi / SECTORS))) % SECTORS


@njit(parallel=True, cache=True)
def _pairs(z, anchors, elev, rings, todo, order, starts, cell, cells_x, res,
           min_length, max_length, max_dh_ratio, max_dh,
           use_sag, log_spans, ratios, table, fall_zone, first_only, offsets, out, counts):
    """Partners of the anchors in todo that pass all tests: only partners with a higher index,
    or with first_only those for which the anchor is the first (smaller x, then y; a tile
    keeps the lines whose first anchor lies in its core). A pair is always checked and
    written from its lower index: the pixels sampled depend on the direction (rint).

    With offsets None, counts[q] = [candidate pairs, pairs checked, lines] for todo[q].
    Otherwise the pairs are written to out from offsets[q] on.
    """
    reach = int(max_length / res) // cell + 1
    cells_y = starts.size // cells_x
    for q in prange(todo.size):
        i = todo[q]
        ri, ci = anchors[i, 0], anchors[i, 1]
        cy, cx = ri // cell, ci // cell
        candidates, checked, found = 0, 0, 0
        for gy in range(max(0, cy - reach), min(cells_y, cy + reach + 1)):
            for gx in range(max(0, cx - reach), min(cells_x, cx + reach + 1)):
                g = gy * cells_x + gx
                end = starts[g + 1] if g + 1 < starts.size else order.size
                for p in range(starts[g], end):
                    j = order[p]
                    if first_only:
                        if anchors[j, 1] < ci or (anchors[j, 1] == ci and anchors[j, 0] >= ri):
                            continue
                    elif j <= i:
                        continue
                    a, b = (i, j) if i < j else (j, i)
                    dr, dc = anchors[b, 0] - anchors[a, 0], anchors[b, 1] - anchors[a, 1]
                    length = math.hypot(dr, dc) * res
                    if length > max_length or length < min_length:
                        continue
                    if not (abs(elev[b] - elev[a]) <= min(max_dh_ratio * length, max_dh)):
                        continue
                    candidates += 1
                    need = min(RINGS, max(abs(dr), abs(dc)) // 2)
                    if rings[a, _sector(dr, dc)] < need or rings[b, _sector(-dr, -dc)] < need:
                        continue
                    checked += 1
                    if _line_ok(z, anchors[a, 0], anchors[a, 1], anchors[b, 0], anchors[b, 1], res,
                                use_sag, log_spans, ratios, table, fall_zone):
                        if offsets is not None:
                            out[offsets[q] + found, 0] = a
                            out[offsets[q] + found, 1] = b
                        found += 1
        counts[q, 0], counts[q, 1], counts[q, 2] = candidates, checked, found


@njit(cache=True)
def _interp(x, xp, fp):
    """np.interp of one value, with numpy's arithmetic (the same result to the last bit)."""
    n = xp.size
    if x != x:
        return x
    if x < xp[0]:
        return fp[0]
    if x > xp[n - 1]:
        return fp[n - 1]
    j = np.searchsorted(xp, x, side="right") - 1
    if j == n - 1 or xp[j] == x:
        return fp[j]
    slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j])
    result = slope * (x - xp[j]) + fp[j]
    if result != result:
        result = slope * (x - xp[j + 1]) + fp[j + 1]
        if result != result and fp[j] == fp[j + 1]:
            result = fp[j]
    return result


@njit(parallel=True, cache=True)
def _evaluate(z, s, has_s, anchors, found, res, use_sag, log_spans, span_index, ratios, ratio_index,
              sag, fractions, fall_zone, high_above, out):
    """helpers.evaluate_line for many lines, with the same arithmetic (identical results).

    out[k]: elev_a1, elev_a2, length, clearance, clearance over the surface (NaN: none),
    1 when the line has pixels between its anchors, then the largest and mean height of
    the webbing above the terrain and the % of pixels at least high_above.
    """
    blend = np.empty(fractions.size)
    fall = fall_zone if use_sag else sh.LEASH_FALL  # helpers.fall_allowance
    for k in prange(found.shape[0]):
        r1, c1 = anchors[found[k, 0], 0], anchors[found[k, 0], 1]
        r2, c2 = anchors[found[k, 1], 0], anchors[found[k, 1], 1]
        dr, dc = r2 - r1, c2 - c1
        steps = max(abs(dr), abs(dc))
        out[k, 5] = 0.0
        if steps < 2:
            continue
        length = np.hypot(np.float64(dr), np.float64(dc)) * res
        e1, e2 = np.float64(z[r1, c1]), np.float64(z[r2, c2])
        emin = min(e1, e2)
        i0, j0, a, b = 0, 0, 0.0, 0.0
        if use_sag:  # sag.EnvelopeTable.__call__
            si = _interp(math.log(length), log_spans, span_index)
            ri = _interp((e1 - e2) / length, ratios, ratio_index)
            i0, j0 = min(int(si), log_spans.size - 2), min(int(ri), ratios.size - 2)
            a, b = si - i0, ri - j0
        clearance, nmpt, seen = np.inf, np.inf, False
        height_max, height_sum, high = -np.inf, 0.0, 0
        row = np.empty(fractions.size) if use_sag else blend
        if use_sag:
            for f in range(fractions.size):
                row[f] = ((1 - a) * (1 - b) * sag[i0, j0, f] + a * (1 - b) * sag[i0 + 1, j0, f]
                          + (1 - a) * b * sag[i0, j0 + 1, f] + a * b * sag[i0 + 1, j0 + 1, f])
        for i in range(1, steps):
            t = i / steps
            r = r1 + int(np.rint(dr * t))
            c = c1 + int(np.rint(dc * t))
            if use_sag:
                limit = e1 + (e2 - e1) * t - _interp(t, fractions, row) - fall_zone
            else:
                limit = emin - sh.SLOPE * min(t, 1 - t) * length - sh.LEASH_FALL
            margin = limit - np.float64(z[r, c])
            if margin != margin or clearance != clearance:
                clearance = np.nan  # np.min: no data anywhere makes the line fail
            elif margin < clearance:
                clearance = margin
            height = margin + fall  # helpers.webbing_height
            if height != height or height > height_max:  # np.max: NaN stays
                height_max = height
            height_sum += height
            if height >= high_above:
                high += 1
            if has_s:
                over = limit - np.float64(s[r, c])
                if over == over:  # np.nanmin
                    seen = True
                    if over < nmpt:
                        nmpt = over
        out[k, 0], out[k, 1], out[k, 2], out[k, 3] = e1, e2, length, clearance
        out[k, 4] = nmpt if seen else np.nan
        out[k, 5] = 1.0
        out[k, 6], out[k, 7] = height_max, height_sum / (steps - 1)
        out[k, 8] = 100.0 * high / (steps - 1)


def evaluate_lines(found: np.ndarray, anchors: np.ndarray, elevation: np.ndarray, res: float,
                   surface: Optional[np.ndarray], sag_table, nmpt_mode: Optional[str],
                   high_above: float = sh.HIGH_ABOVE, heights: Optional[sh.HeightLimits] = None) -> List[sh.Line]:
    """The lines among the pairs found (indices into anchors) that pass helpers.evaluate_line
    and helpers.is_valid: the same decision and values, computed in parallel. With heights,
    only the lines high enough above the terrain (sh.HeightLimits.passes)."""
    out = np.zeros((len(found), 9))
    if len(found):
        if sag_table is None:
            log_spans = span_index = ratios = ratio_index = fractions = np.zeros(2)
            sag, fall_zone = np.zeros((2, 2, 2)), 0.0
        else:
            log_spans = np.log(sag_table.spans)  # as in EnvelopeTable.__call__
            span_index = np.arange(len(sag_table.spans)).astype(np.float64)
            ratios = sag_table.RATIOS.astype(np.float64)
            ratio_index = np.arange(len(ratios)).astype(np.float64)
            sag, fractions = np.ascontiguousarray(sag_table.sag), sag_table.fractions.astype(np.float64)
            fall_zone = float(sag_table.rig.fall_zone)
        z = np.asarray(elevation)
        s = np.asarray(surface) if surface is not None else np.zeros((1, 1), dtype=z.dtype)
        _evaluate(z, s, surface is not None, np.ascontiguousarray(anchors, dtype=np.int64),
                  np.ascontiguousarray(found, dtype=np.int64), float(res), sag_table is not None, log_spans,
                  span_index, ratios, ratio_index, sag, fractions, fall_zone, float(high_above), out)
    lines = []
    for (i, j), (e1, e2, length, clearance, nmpt, ok, *values) in zip(found.tolist(), out.tolist()):
        result = (e1, e2, length, clearance, None if nmpt != nmpt else nmpt) if ok else None
        if sh.is_valid(result, nmpt_mode) and (heights is None or heights.passes(*values)):
            lines.append(sh.Line(tuple(anchors[i].tolist()), tuple(anchors[j].tolist()), length, e1, e2,
                                 clearance, result[4], *values))
    return lines


@njit(parallel=True, cache=True)
def _covered_pixels(a1, a2, mask, out):
    """Pixels of each line (anchors excluded, the pixels of helpers.line_profile) that are set in mask."""
    for k in prange(a1.shape[0]):
        r1, c1, r2, c2 = a1[k, 0], a1[k, 1], a2[k, 0], a2[k, 1]
        dr, dc = r2 - r1, c2 - c1
        steps = max(abs(dr), abs(dc))
        n = 0
        for i in range(1, steps):
            t = i / steps
            if mask[r1 + int(np.rint(dr * t)), c1 + int(np.rint(dc * t))]:
                n += 1
        out[k] = n


def covered_metres(a1: np.ndarray, a2: np.ndarray, res: float, mask: Optional[np.ndarray]) -> List[float]:
    """Metres of each line over the pixels of a mask (water, filled gaps): the values of
    helpers.line_profile ("water_m", "filled_m") without the rest of the profile."""
    if mask is None or len(a1) == 0:
        return [0.0] * len(a1)
    a1, a2 = np.ascontiguousarray(a1, dtype=np.int64), np.ascontiguousarray(a2, dtype=np.int64)
    counts = np.zeros(len(a1), dtype=np.int64)
    _covered_pixels(a1, a2, np.ascontiguousarray(mask, dtype=np.bool_), counts)
    d = a2 - a1
    steps = np.maximum(np.abs(d[:, 0]), np.abs(d[:, 1]))
    lengths = np.hypot(d[:, 0], d[:, 1]) * res
    return [round(float(n) * length / s, 1) if n else 0.0
            for n, length, s in zip(counts.tolist(), lengths.tolist(), steps.tolist())]


def find_valid_lines(elevation: np.ndarray, surface: Optional[np.ndarray], anchors: np.ndarray,
                     res: float, min_length: float, max_length: float,
                     nmpt_mode: Optional[str] = None, threads: Optional[int] = None,
                     sag_table=None, max_dh_ratio: float = np.inf,
                     max_dh: float = np.inf, first: Optional[np.ndarray] = None,
                     high_above: float = sh.HIGH_ABOVE, heights: Optional[sh.HeightLimits] = None
                     ) -> List[sh.Line]:
    """Finds valid lines between anchors.

    Args:
        elevation: NMT elevation grid
        surface: NMPT grid (trees, buildings), or None
        anchors: [row, col] anchor coordinates
        res: Pixel size in metres
        min_length, max_length: Line length limits in metres
        nmpt_mode: None, "mark" or "reject"
        threads: Number of threads (defaults to CPU count - 1)
        sag_table: sag.EnvelopeTable to use the sag estimator instead of the formula
        max_dh_ratio, max_dh: The anchors may differ in elevation by at most
            max_dh_ratio of the line length, and never more than max_dh
        first: Optional mask of the anchors whose lines are wanted when they are the
            first anchor (smaller x, then y): the anchors in the core of a tile
        high_above: Height above the terrain from which a pixel of a line counts as high
        heights: Optional limits of the heights above the terrain; lines below them are
            left out (counted in heights.left_out)

    Returns:
        Valid lines, longest first
    """
    numba.set_num_threads(max(1, min(threads or numba.config.NUMBA_NUM_THREADS - 1,
                                     numba.config.NUMBA_NUM_THREADS)))
    start_time = time.time()
    z = np.asarray(elevation)
    anchors = np.ascontiguousarray(anchors, dtype=np.int64)
    elev = np.asarray(z[anchors[:, 0], anchors[:, 1]])

    if sag_table is None:
        use_sag, log_spans, ratios, table, fall_zone = False, np.zeros(2), np.zeros(2), np.zeros((2, 2, 2)), 0.0
        gain, cap, offset = -sh.SLOPE, np.inf, sh.LEASH_FALL
    else:
        use_sag = True
        log_spans, ratios = np.log(sag_table.spans), sag_table.RATIOS.astype(np.float64)
        table, fall_zone = np.ascontiguousarray(sag_table.sag), sag_table.rig.fall_zone
        # chord rises by at most the allowed height difference; the sag is never negative
        gain, cap = max_dh_ratio * math.sqrt(2), max_dh
        offset = fall_zone + min(0.0, float(table.min()))

    # Step 1: open directions of every anchor
    progress.compute.start("free directions", 1, unit="step")
    rings = _free_rings(z, anchors, res, gain, cap, offset)
    progress.compute.finish()
    need_min = min(RINGS, int(min_length / (res * math.sqrt(2))) // 2)
    useful = np.flatnonzero(rings.max(axis=1) >= need_min)
    print(f"Anchors with an open direction: {len(useful)} of {len(anchors)}")
    # Pairs are searched from the anchors in todo; partners may be any useful anchor
    todo_all = useful if first is None else useful[np.asarray(first)[useful]]

    # Step 2 + 3: bucket the useful anchors, then find and check the pairs
    cell = max(1, int(max_length / res) // 4)
    cells_x = z.shape[1] // cell + 1
    cells_y = z.shape[0] // cell + 1
    keys = (anchors[useful, 0] // cell) * cells_x + anchors[useful, 1] // cell
    order = useful[np.argsort(keys, kind="stable")]
    starts = np.searchsorted(np.sort(keys), np.arange(cells_y * cells_x)).astype(np.int64)

    args = (z, anchors, elev, rings)
    tail = (order, starts, cell, cells_x, res, float(min_length), float(max_length),
            float(max_dh_ratio), float(max_dh), use_sag, log_spans, ratios, table, float(fall_zone),
            first is not None)
    pairs, candidates, checked = [], 0, 0
    progress.compute.start("anchor pairs", len(todo_all), unit="anchor")
    for todo in np.array_split(todo_all, min(CHUNKS, max(1, len(todo_all)))):
        counts = np.zeros((len(todo), 3), dtype=np.int64)
        _pairs(*args, todo, *tail, None, np.zeros((0, 2), dtype=np.int64), counts)
        offsets = np.concatenate([[0], np.cumsum(counts[:, 2])[:-1]]).astype(np.int64)
        out = np.empty((int(counts[:, 2].sum()), 2), dtype=np.int64)
        _pairs(*args, todo, *tail, offsets, out, np.zeros_like(counts))
        pairs.append(out)
        candidates += int(counts[:, 0].sum())
        checked += int(counts[:, 1].sum())
        progress.compute.update(len(todo), pairs=f"{checked:,}", lines=sum(len(p) for p in pairs))
    progress.compute.finish()

    # Final decision and clearances, exactly as in the pixel-by-pixel search (evaluate_line)
    found = np.concatenate(pairs) if pairs else np.zeros((0, 2), dtype=np.int64)
    valid_lines: List[sh.Line] = []
    progress.compute.start("checking lines", len(found), unit="line")
    for part in np.array_split(found, max(1, min(CHUNKS, len(found) // 10000))):
        valid_lines += evaluate_lines(part, anchors, elevation, res, surface, sag_table, nmpt_mode, high_above,
                                      heights)
        progress.compute.update(len(part), valid=len(valid_lines))
    progress.compute.finish(f"{len(valid_lines)} valid")
    print(f"\n{'=' * 60}")
    print(f"Completed in {time.time() - start_time:.2f} seconds")
    print(f"Candidate pairs between anchors with an open direction: {candidates}")
    print(f"Checked pixel by pixel: {checked}")
    if heights is not None and heights.active:
        print(f"Left out as not high enough above the terrain ({heights.describe()}): {heights.left_out}")
    print(f"Found {len(valid_lines)} valid lines")
    print(f"{'=' * 60}")

    valid_lines.sort(key=lambda line: line.length, reverse=True)
    return valid_lines
