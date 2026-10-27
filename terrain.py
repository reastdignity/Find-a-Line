"""
Terrain measures of the anchors: how far an anchor stands out of its surroundings.

- prominence: the height of the anchor above the level at which the terrain at or above
  that level, connected to the anchor (8 neighbours), first spans `span` metres in rows
  or columns. A rock tower gets its height above the saddle that joins it to the rest,
  the edge of a plateau wider than the span about 0, a ridge narrower than the span the
  height of the ridge. Computed for all anchors at once by one union-find pass over the
  pixels from the highest down (numba).
- drop: the anchor minus the lowest terrain within a square of +-radius around it.

Both look at most max(span, radius) around an anchor, so a grid with that margin around
the anchors gives the values of an unlimited grid (search._margin): a component that
reaches beyond the margin spans more than `span` inside the grid too.
"""

import math

import numpy as np
from numba import njit
from scipy import ndimage


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

    order: the pixels with data by decreasing height. NaN for an anchor whose component
    never spans `span` (the grid is smaller than that).
    """
    h, w = z.shape
    n = h * w
    parent = np.full(n, -1, dtype=np.int32)  # -1: not added yet
    size = np.ones(n, dtype=np.int32)
    r0 = np.empty(n, dtype=np.int32)         # the box of a component, kept at its root
    r1 = np.empty(n, dtype=np.int32)
    c0 = np.empty(n, dtype=np.int32)
    c1 = np.empty(n, dtype=np.int32)
    big = np.zeros(n, dtype=np.bool_)
    head = np.full(n, -1, dtype=np.int32)    # the anchors waiting in a component, at its root
    tail = np.full(n, -1, dtype=np.int32)
    first = np.full(n, -1, dtype=np.int32)   # the anchors of each pixel, chained by nxt
    nxt = np.full(anchor_pixel.size, -1, dtype=np.int32)
    result = np.full(anchor_pixel.size, np.nan)
    for a in range(anchor_pixel.size):
        p = anchor_pixel[a]
        nxt[a] = first[p]
        first[p] = a
    waiting = anchor_pixel.size
    for k in range(order.size):
        if waiting == 0:
            break
        p = order[k]
        level = np.float64(z.flat[p])
        r, c = p // w, p % w
        parent[p] = p
        r0[p], r1[p], c0[p], c1[p] = r, r, c, c
        big[p] = span <= 1
        if first[p] >= 0:  # the anchors of this pixel start waiting
            head[p] = first[p]
            last = first[p]
            while nxt[last] >= 0:
                last = nxt[last]
            tail[p] = last
        root = p
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                rr, cc = r + dr, c + dc
                if (dr == 0 and dc == 0) or rr < 0 or cc < 0 or rr >= h or cc >= w:
                    continue
                q = rr * w + cc
                if parent[q] < 0:
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


def prominence(elevation: np.ndarray, anchors: np.ndarray, res: float, span: float) -> np.ndarray:
    """Prominence of every anchor ([row, col]) in metres, for a span in metres."""
    anchors = np.asarray(anchors, dtype=np.int64).reshape(-1, 2)
    if len(anchors) == 0:
        return np.zeros(0)
    z = np.ascontiguousarray(elevation, dtype=np.float32)
    flat = z.ravel()
    valid = np.flatnonzero(~np.isnan(flat))
    order = valid[np.argsort(-flat[valid])]  # the order of equal heights does not change the result
    pixels = anchors[:, 0] * z.shape[1] + anchors[:, 1]
    return _prominence(z, order.astype(np.int64), pixels, max(1, math.ceil(span / res)))


def drop(elevation: np.ndarray, anchors: np.ndarray, res: float, radius: float) -> np.ndarray:
    """The anchors' elevation minus the lowest terrain within +-radius metres (a square)."""
    anchors = np.asarray(anchors, dtype=np.int64).reshape(-1, 2)
    if len(anchors) == 0:
        return np.zeros(0)
    size = 2 * max(1, math.ceil(radius / res)) + 1
    z = np.where(np.isnan(elevation), np.inf, np.asarray(elevation, dtype=np.float32))
    lowest = ndimage.minimum_filter1d(z, size, axis=1, mode="constant", cval=np.inf)
    lowest = ndimage.minimum_filter1d(lowest, size, axis=0, mode="constant", cval=np.inf)
    e = np.asarray(elevation[anchors[:, 0], anchors[:, 1]], dtype=np.float64)
    return e - lowest[anchors[:, 0], anchors[:, 1]].astype(np.float64)
