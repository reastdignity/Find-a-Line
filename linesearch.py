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
           use_sag, log_spans, ratios, table, fall_zone, offsets, out, counts):
    """Partners of the anchors in todo that pass all tests (only partners with a higher index).

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
                    if j <= i:
                        continue
                    dr, dc = anchors[j, 0] - ri, anchors[j, 1] - ci
                    length = math.hypot(dr, dc) * res
                    if length > max_length or length < min_length:
                        continue
                    if not (abs(elev[j] - elev[i]) <= min(max_dh_ratio * length, max_dh)):
                        continue
                    candidates += 1
                    need = min(RINGS, max(abs(dr), abs(dc)) // 2)
                    if rings[i, _sector(dr, dc)] < need or rings[j, _sector(-dr, -dc)] < need:
                        continue
                    checked += 1
                    if _line_ok(z, ri, ci, anchors[j, 0], anchors[j, 1], res,
                                use_sag, log_spans, ratios, table, fall_zone):
                        if offsets is not None:
                            out[offsets[q] + found, 0] = i
                            out[offsets[q] + found, 1] = j
                        found += 1
        counts[q, 0], counts[q, 1], counts[q, 2] = candidates, checked, found


def find_valid_lines(elevation: np.ndarray, surface: Optional[np.ndarray], anchors: np.ndarray,
                     res: float, min_length: float, max_length: float,
                     nmpt_mode: Optional[str] = None, threads: Optional[int] = None,
                     sag_table=None, max_dh_ratio: float = np.inf,
                     max_dh: float = np.inf) -> List[sh.Line]:
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

    # Step 2 + 3: bucket the useful anchors, then find and check the pairs
    cell = max(1, int(max_length / res) // 4)
    cells_x = z.shape[1] // cell + 1
    cells_y = z.shape[0] // cell + 1
    keys = (anchors[useful, 0] // cell) * cells_x + anchors[useful, 1] // cell
    order = useful[np.argsort(keys, kind="stable")]
    starts = np.searchsorted(np.sort(keys), np.arange(cells_y * cells_x)).astype(np.int64)

    args = (z, anchors, elev, rings)
    tail = (order, starts, cell, cells_x, res, float(min_length), float(max_length),
            float(max_dh_ratio), float(max_dh), use_sag, log_spans, ratios, table, float(fall_zone))
    pairs, candidates, checked = [], 0, 0
    progress.compute.start("anchor pairs", len(useful), unit="anchor")
    for todo in np.array_split(useful, min(CHUNKS, max(1, len(useful)))):
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

    # Final decision and clearances, exactly as in the pixel-by-pixel search
    valid_lines: List[sh.Line] = []
    for i, j in np.concatenate(pairs) if pairs else []:
        result = sh.evaluate_line(anchors[i], anchors[j], elevation, res, surface, sag_table)
        if sh.is_valid(result, nmpt_mode):
            elev_a1, elev_a2, length, clearance, clearance_nmpt = result
            valid_lines.append(sh.Line(tuple(map(int, anchors[i])), tuple(map(int, anchors[j])),
                                       length, elev_a1, elev_a2, clearance, clearance_nmpt))

    print(f"\n{'=' * 60}")
    print(f"Completed in {time.time() - start_time:.2f} seconds")
    print(f"Candidate pairs between anchors with an open direction: {candidates}")
    print(f"Checked pixel by pixel: {checked}")
    print(f"Found {len(valid_lines)} valid lines")
    print(f"{'=' * 60}")

    valid_lines.sort(key=lambda line: line.length, reverse=True)
    return valid_lines
