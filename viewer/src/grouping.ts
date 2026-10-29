// Grouping of near-identical lines: the same greedy algorithm as mapping.group_lines
// (keep both in sync; tests/test_viewer.py compares them). Written for runs of hundreds of
// thousands of lines: the inner loops work on indices and typed arrays, not on objects, sets
// and string keys.

export interface Groupable {
  x1: number; y1: number; x2: number; y2: number;
  len: number; clr: number; over: boolean;
  hmax: number;  // largest height above the terrain (by "height")
}

export interface Group<T> {
  rep: T;
  members: T[];
}

/** Best first, compared as mapping._rank (on the values in typed arrays: the same numbers, as
 * a subtraction converts null and undefined just as a Float64Array does). */
function compare(by: string, len: Float64Array, clr: Float64Array, hmax: Float64Array) {
  if (by === "longest") return (a: number, b: number) => (len[b] - len[a]) || (clr[b] - clr[a]);
  if (by === "height") return (a: number, b: number) => (hmax[b] - hmax[a]) || (clr[b] - clr[a]);
  return (a: number, b: number) => (clr[b] - clr[a]) || (len[b] - len[a]);
}

function sq(ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax, dy = by - ay;
  return dx * dx + dy * dy;
}

// Cells of the grid in a hash table of linked lists (head and next in typed arrays): a cell's
// lines lie in the list of its bucket. Cells sharing a bucket merely add candidates (and so do
// cells whose numbers wrap in 32 bits): membership is decided by the distances.
const bucket = (cx: number, cy: number, mask: number) => (Math.imul(cx | 0, 73856093) ^ Math.imul(cy | 0, 19349663)) & mask;

/** Groups of lines whose anchors are both within `distance` of the representative's,
 * clear lines first (then lines over trees/buildings), best representative first. */
export function groupLines<T extends Groupable>(lines: T[], distance: number, by: string): Group<T>[] {
  const d2 = distance * distance, groups: Group<T>[] = [];
  const len = new Float64Array(lines.length), clr = new Float64Array(lines.length), hmax = new Float64Array(lines.length);
  lines.forEach((l, k) => { len[k] = l.len; clr[k] = l.clr; hmax[k] = l.hmax; });
  for (const over of [false, true]) {
    // The lines' indices in their order (a stable sort of the same comparisons as on the lines)
    const sorted: number[] = [];
    lines.forEach((l, k) => { if (l.over === over) sorted.push(k); });
    sorted.sort(compare(by, len, clr, hmax));
    const n = sorted.length;
    const x1 = new Float64Array(n), y1 = new Float64Array(n), x2 = new Float64Array(n), y2 = new Float64Array(n);
    // Lines by the cell (of size `distance`) of their first anchor: a member's first
    // anchor lies within `distance` of one of the representative's anchors
    const mask = 2 ** Math.ceil(Math.log2(2 * n + 1)) - 1;
    const head = new Int32Array(mask + 1).fill(-1), next = new Int32Array(n);
    for (let k = 0; k < n; k++) {
      const l = lines[sorted[k]];
      x1[k] = l.x1; y1[k] = l.y1; x2[k] = l.x2; y2[k] = l.y2;
      const b = bucket(Math.floor(l.x1 / distance), Math.floor(l.y1 / distance), mask);
      next[k] = head[b];
      head[b] = k;
    }
    const used = new Uint8Array(n), seen = new Int32Array(n).fill(-1), candidates = new Int32Array(n);
    let found = 0;
    // The free lines in the 3 x 3 cells around a point, each once per representative i; the lines
    // grouped already leave the lists, so dense cells are not read again and again
    const near = (x: number, y: number, i: number) => {
      const cx = Math.floor(x / distance), cy = Math.floor(y / distance);
      for (let dx = -1; dx <= 1; dx++) {
        for (let dy = -1; dy <= 1; dy++) {
          const b = bucket(cx + dx, cy + dy, mask);
          let before = -1;
          for (let k = head[b]; k !== -1; k = next[k]) {
            if (used[k]) {
              if (before < 0) head[b] = next[k]; else next[before] = next[k];
              continue;
            }
            before = k;
            if (seen[k] !== i) { seen[k] = i; candidates[found++] = k; }
          }
        }
      }
    };
    for (let i = 0; i < n; i++) {
      if (used[i]) continue;
      found = 0;
      near(x1[i], y1[i], i);
      near(x2[i], y2[i], i);
      const ax = x1[i], ay = y1[i], bx = x2[i], by_ = y2[i], members: T[] = [];
      for (const k of candidates.subarray(0, found).sort()) {  // in their order (a typed array sorts numbers)
        const same = sq(ax, ay, x1[k], y1[k]) <= d2 && sq(bx, by_, x2[k], y2[k]) <= d2;
        const flipped = sq(bx, by_, x1[k], y1[k]) <= d2 && sq(ax, ay, x2[k], y2[k]) <= d2;
        if (same || flipped) {
          used[k] = 1;
          members.push(lines[sorted[k]]);
        }
      }
      groups.push({rep: lines[sorted[i]], members});
    }
  }
  return groups;
}
