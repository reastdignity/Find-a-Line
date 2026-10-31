"""
Terrain measures of the anchors: how far an anchor stands out of its surroundings.

- prominence: the height of the anchor above the level at which the terrain at or above
  that level, connected to the anchor (8 neighbours), first spans `span` metres in rows
  or columns. A rock tower gets its height above the saddle that joins it to the rest,
  the edge of a plateau wider than the span about 0, a ridge narrower than the span the
  height of the ridge. Computed for all anchors at once by one union-find pass over the
  pixels from the highest down (numba), over the pixels within span - 1 pixels of an anchor
  only (_window: the others cannot change the result), so only these are sorted and the
  union-find keeps its arrays for these only (4 bytes per pixel of the grid and about 41 per
  pixel in the windows, at most what one pass over every pixel took).
- drop: the anchor minus the lowest terrain within a square of +-radius around it: a
  separable minimum (van Herk / Gil-Werman, numba, in parallel), the second pass only at the
  anchors.

Both look at most max(span, radius) around an anchor, so a grid with that margin around
the anchors gives the values of an unlimited grid (search._margin): a component that
reaches beyond the margin spans more than `span` inside the grid too.
"""

import math

import numpy as np
from numba import njit, prange


@njit(cache=True)
def _find(parent, i):
    root = i
    while parent[root] != root:
        root = parent[root]
    while parent[i] != root:  # path compression
        parent[i], i = root, parent[i]
    return root


@njit(cache=True)
def _prominence(z, order, anchor_pixel, span):
    """Prominence of the anchors (pixel indices row * width + col) in pixels of `span`.

    order: the pixels taken into account (flat indices, with data) by decreasing height; the
    union-find keeps its arrays for these pixels only, a pixel's place in order is its index
    there. NaN for an anchor whose component never spans `span` (the grid is smaller than
    that) and for an anchor without data.
    """
    h, w = z.shape
    m = order.size
    index = np.full(h * w, -1, dtype=np.int32)  # pixel -> its place in order, -1: left out
    for k in range(m):
        index[order[k]] = k
    parent = np.empty(m, dtype=np.int32)     # set when the pixel is added (in order)
    size = np.ones(m, dtype=np.int32)
    r0 = np.empty(m, dtype=np.int32)         # the box of a component, kept at its root
    r1 = np.empty(m, dtype=np.int32)
    c0 = np.empty(m, dtype=np.int32)
    c1 = np.empty(m, dtype=np.int32)
    big = np.zeros(m, dtype=np.bool_)
    head = np.full(m, -1, dtype=np.int32)    # the anchors waiting in a component, at its root
    tail = np.full(m, -1, dtype=np.int32)
    first = np.full(m, -1, dtype=np.int32)   # the anchors of each pixel, chained by nxt
    nxt = np.full(anchor_pixel.size, -1, dtype=np.int32)
    result = np.full(anchor_pixel.size, np.nan)
    waiting = 0
    for a in range(anchor_pixel.size):
        i = index[anchor_pixel[a]]
        if i >= 0:
            nxt[a] = first[i]
            first[i] = a
            waiting += 1
    for i in range(m):
        if waiting == 0:
            break
        p = order[i]
        level = np.float64(z.flat[p])
        r, c = p // w, p % w
        parent[i] = i
        r0[i], r1[i], c0[i], c1[i] = r, r, c, c
        big[i] = span <= 1
        if first[i] >= 0:  # the anchors of this pixel start waiting
            head[i] = first[i]
            last = first[i]
            while nxt[last] >= 0:
                last = nxt[last]
            tail[i] = last
        root = i
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                rr, cc = r + dr, c + dc
                if (dr == 0 and dc == 0) or rr < 0 or cc < 0 or rr >= h or cc >= w:
                    continue
                q = index[rr * w + cc]
                if q < 0 or q > i:  # left out, or not added yet
                    continue
                other = _find(parent, q)
                if other == root:
                    continue
                if size[other] > size[root]:  # union by size
                    root, other = other, root
                parent[other] = root
                size[root] += size[other]
                r0[root], r1[root] = min(r0[root], r0[other]), max(r1[root], r1[other])
                c0[root], c1[root] = min(c0[root], c0[other]), max(c1[root], c1[other])
                big[root] = big[root] or big[other]
                if head[other] >= 0:
                    if head[root] >= 0:
                        nxt[tail[root]] = head[other]
                    else:
                        head[root] = head[other]
                    tail[root] = tail[other]
                    head[other] = -1
        if not big[root] and (r1[root] - r0[root] + 1 >= span or c1[root] - c0[root] + 1 >= span):
            big[root] = True
        if big[root] and head[root] >= 0:  # the waiting anchors get their prominence
            a = head[root]
            while a >= 0:
                result[a] = np.float64(z.flat[anchor_pixel[a]]) - level
                waiting -= 1
                a = nxt[a]
            head[root] = -1
    return result


@njit(parallel=True, cache=True)
def _window(z, rows, cols, row_starts, reach, index_type):
    """The pixels with data within `reach` pixels (Chebyshev) of an anchor, as flat indices
    of index_type's type in increasing order.

    The anchors are sorted by row, then column; row_starts[r]:row_starts[r + 1] are those of
    row r. These pixels are all the prominence of the anchors needs (span - 1 around them):
    an anchor's result is the highest level at which its component of the terrain at or
    above that level spans `span` pixels, whatever the order of equal heights. Within the
    anchor's own window W the component is the same as in the whole grid as long as it spans
    less than `span` (all of it lies in W then), and once the whole grid's component reaches
    beyond W, its path from the anchor to there passes a pixel at distance span - 1 inside W,
    so the component within W spans `span` too. Both become big at the same level, and so
    does the component within the union of all windows, which lies between the two.
    """
    h, w = z.shape
    near = np.zeros((h, w), dtype=np.uint8)  # within reach of an anchor of the same row
    for r in prange(h):
        done = 0  # the columns before it are marked
        for t in range(row_starts[r], row_starts[r + 1]):
            lo, hi = max(done, cols[t] - reach), min(w, cols[t] + reach + 1)
            for c in range(lo, hi):
                near[r, c] = 1
            done = max(done, hi)
    inside = np.zeros((h, w), dtype=np.uint8)  # and within reach in the column
    group = 64
    for g in prange((w + group - 1) // group):
        c0 = g * group
        width = min(w, c0 + group) - c0
        since = np.full(width, reach + 1, dtype=np.int64)  # rows since the last marked one
        for r in range(h):
            for i in range(width):
                since[i] = 0 if near[r, c0 + i] else since[i] + 1
                inside[r, c0 + i] = since[i] <= reach
        since[:] = reach + 1
        for r in range(h - 1, -1, -1):
            for i in range(width):
                since[i] = 0 if near[r, c0 + i] else since[i] + 1
                if since[i] <= reach:
                    inside[r, c0 + i] = 1
                if np.isnan(z[r, c0 + i]):
                    inside[r, c0 + i] = 0
    count = np.zeros(h + 1, dtype=np.int64)
    for r in prange(h):
        n = 0
        for c in range(w):
            if inside[r, c]:
                n += 1
        count[r + 1] = n
    for r in range(h):
        count[r + 1] += count[r]
    pixels = np.empty(count[h], dtype=index_type.dtype)
    for r in prange(h):
        k = count[r]
        for c in range(w):
            if inside[r, c]:
                pixels[k] = r * w + c
                k += 1
    return pixels


def prominence(elevation: np.ndarray, anchors: np.ndarray, res: float, span: float) -> np.ndarray:
    """Prominence of every anchor ([row, col]) in metres, for a span in metres."""
    anchors = np.asarray(anchors, dtype=np.int64).reshape(-1, 2)
    if len(anchors) == 0:
        return np.zeros(0)
    z = np.ascontiguousarray(elevation, dtype=np.float32)
    h, w = z.shape
    span_px = max(1, math.ceil(span / res))
    by_row = np.lexsort((anchors[:, 1], anchors[:, 0]))
    rows, cols = anchors[by_row, 0], anchors[by_row, 1]
    row_starts = np.searchsorted(rows, np.arange(h + 1))
    index_type = np.zeros(0, dtype=np.int32 if h * w < 2 ** 31 else np.int64)
    pixels = _window(z, rows, cols, row_starts, span_px - 1, index_type)
    order = pixels[np.argsort(-z.ravel()[pixels])]  # the order of equal heights does not change the result
    del pixels
    return _prominence(z, order, anchors[:, 0] * w + anchors[:, 1], span_px)


@njit(parallel=True, cache=True)
def _min_rows(z, rad):
    """The minimum of every row over +-rad columns (NaN as inf, inf beyond the edges)."""
    h, w = z.shape
    k = 2 * rad + 1
    n = w + 2 * rad  # the row with rad pixels of inf on both sides
    out = np.empty((h, w), dtype=np.float32)
    inf = np.float32(np.inf)
    for r in prange(h):
        pre = np.empty(n, dtype=np.float32)  # minimum from the start of its block of k
        suf = np.empty(n, dtype=np.float32)  # minimum to the end of its block
        for start in range(0, n, k):
            stop = min(start + k, n)
            m = inf
            for j in range(start, stop):
                c = j - rad
                if 0 <= c < w:
                    v = z[r, c]
                    if v < m:  # NaN never is
                        m = v
                pre[j] = m
            m = inf
            for j in range(stop - 1, start - 1, -1):
                c = j - rad
                if 0 <= c < w:
                    v = z[r, c]
                    if v < m:
                        m = v
                suf[j] = m
        for c in range(w):  # the window [c, c + k - 1] of the padded row
            a, b = suf[c], pre[c + k - 1]
            out[r, c] = a if a < b else b
    return out


@njit(parallel=True, cache=True)
def _min_cols_at(x, rad, rows, cols, starts):
    """The minimum of x over +-rad rows (inf beyond the edges) at the given pixels.

    The pixels are sorted by column; starts[g]:starts[g + 1] are those of the columns
    g * 64 to g * 64 + 63, which are done together (rows of x are contiguous).
    """
    h, w = x.shape
    k = 2 * rad + 1
    n = h + 2 * rad
    group = 64
    out = np.empty(rows.size, dtype=np.float32)
    inf = np.float32(np.inf)
    for g in prange(starts.size - 1):
        if starts[g] == starts[g + 1]:
            continue
        c0 = g * group
        width = min(w, c0 + group) - c0
        pre = np.empty((n, width), dtype=np.float32)
        suf = np.empty((n, width), dtype=np.float32)
        for start in range(0, n, k):
            stop = min(start + k, n)
            for j in range(start, stop):
                r = j - rad
                for i in range(width):
                    v = x[r, c0 + i] if 0 <= r < h else inf
                    if j > start and pre[j - 1, i] < v:
                        v = pre[j - 1, i]
                    pre[j, i] = v
            for j in range(stop - 1, start - 1, -1):
                r = j - rad
                for i in range(width):
                    v = x[r, c0 + i] if 0 <= r < h else inf
                    if j < stop - 1 and suf[j + 1, i] < v:
                        v = suf[j + 1, i]
                    suf[j, i] = v
        for t in range(starts[g], starts[g + 1]):
            i = cols[t] - c0
            a, b = suf[rows[t], i], pre[rows[t] + k - 1, i]
            out[t] = a if a < b else b
    return out


def drop(elevation: np.ndarray, anchors: np.ndarray, res: float, radius: float) -> np.ndarray:
    """The anchors' elevation minus the lowest terrain within +-radius metres (a square)."""
    anchors = np.asarray(anchors, dtype=np.int64).reshape(-1, 2)
    if len(anchors) == 0:
        return np.zeros(0)
    rad = max(1, math.ceil(radius / res))
    z = np.ascontiguousarray(elevation, dtype=np.float32)
    lowest_in_rows = _min_rows(z, rad)
    by_col = np.argsort(anchors[:, 1], kind="stable")
    rows, cols = anchors[by_col, 0], anchors[by_col, 1]
    starts = np.searchsorted(cols, np.arange(0, z.shape[1] + 64, 64)[:(z.shape[1] + 63) // 64 + 1])
    lowest = np.empty(len(anchors), dtype=np.float32)
    lowest[by_col] = _min_cols_at(lowest_in_rows, rad, rows, cols, starts)
    e = np.asarray(elevation[anchors[:, 0], anchors[:, 1]], dtype=np.float64)
    return e - lowest.astype(np.float64)
