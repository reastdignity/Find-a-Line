// Grouping of near-identical lines: the same greedy algorithm as mapping.group_lines
// (keep both in sync; tests/test_viewer.py compares them).

export interface Groupable {
  x1: number; y1: number; x2: number; y2: number;
  len: number; clr: number; over: boolean;
}

export interface Group<T> {
  rep: T;
  members: T[];
}

function compare(by: string) {
  return by === "longest"
    ? (a: Groupable, b: Groupable) => (b.len - a.len) || (b.clr - a.clr)
    : (a: Groupable, b: Groupable) => (b.clr - a.clr) || (b.len - a.len);
}

function sq(ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax, dy = by - ay;
  return dx * dx + dy * dy;
}

/** Groups of lines whose anchors are both within `distance` of the representative's,
 * clear lines first (then lines over trees/buildings), best representative first. */
export function groupLines<T extends Groupable>(lines: T[], distance: number, by: string): Group<T>[] {
  const d2 = distance * distance, groups: Group<T>[] = [];
  for (const over of [false, true]) {
    const sorted = lines.filter((l) => l.over === over).sort(compare(by));
    // Lines by the cell (of size `distance`) of their first anchor: a member's first
    // anchor lies within `distance` of one of the representative's anchors
    const cells = new Map<string, number[]>();
    sorted.forEach((l, k) => {
      const key = Math.floor(l.x1 / distance) + "," + Math.floor(l.y1 / distance);
      const list = cells.get(key);
      if (list) list.push(k); else cells.set(key, [k]);
    });
    const near = (x: number, y: number, into: Set<number>) => {
      const cx = Math.floor(x / distance), cy = Math.floor(y / distance);
      for (let dx = -1; dx <= 1; dx++) {
        for (let dy = -1; dy <= 1; dy++) {
          for (const k of cells.get((cx + dx) + "," + (cy + dy)) || []) into.add(k);
        }
      }
    };
    const used = new Uint8Array(sorted.length);
    for (let i = 0; i < sorted.length; i++) {
      if (used[i]) continue;
      const rep = sorted[i], candidates = new Set<number>();
      near(rep.x1, rep.y1, candidates);
      near(rep.x2, rep.y2, candidates);
      const members: T[] = [];
      for (const k of Array.from(candidates).sort((a, b) => a - b)) {
        if (used[k]) continue;
        const c = sorted[k];
        const same = sq(rep.x1, rep.y1, c.x1, c.y1) <= d2 && sq(rep.x2, rep.y2, c.x2, c.y2) <= d2;
        const flipped = sq(rep.x2, rep.y2, c.x1, c.y1) <= d2 && sq(rep.x1, rep.y1, c.x2, c.y2) <= d2;
        if (same || flipped) {
          used[k] = 1;
          members.push(c);
        }
      }
      groups.push({rep, members});
    }
  }
  return groups;
}
