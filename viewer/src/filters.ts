// Filters of the lines on the map, applied before grouping (the group size after it).
// null means no limit. Kept in the browser for every map (but "only favourites": the map's own).

import type { Line } from "./types";

export interface Filters {
  lenMin: number | null;
  lenMax: number | null;
  clrMin: number | null;      // clearance over terrain
  clrNMin: number | null;     // clearance over trees/buildings (lines without the surface model pass)
  dhMax: number | null;       // height difference of the anchors
  elevMin: number | null;     // height of the lower anchor
  heightMin: number | null;   // largest height of the webbing above the terrain
  meanMin: number | null;     // its mean along the line
  highPctMin: number | null;  // % of the line at least high_above over the terrain
  angleMax: number | null;    // angle to the wall at both anchors
  promMin: number | null;     // prominence of the more prominent anchor
  promBothMin: number | null; // ... of the less prominent one
  dropMin: number | null;     // drop below both anchors
  water: "all" | "water" | "land";
  hideFilled: boolean;        // hide lines over gaps filled from another source
  groupMin: number | null;    // similar lines in a group (with grouping on)
  favOnly: boolean;           // only favourite lines (applied by result/model.ts, which knows them)
}

export const NO_FILTERS: Filters = {lenMin: null, lenMax: null, clrMin: null, clrNMin: null, dhMax: null,
                                    elevMin: null, heightMin: null, meanMin: null, highPctMin: null,
                                    angleMax: null, promMin: null, promBothMin: null, dropMin: null, water: "all", hideFilled: false, groupMin: null,
                                    favOnly: false};
const STORAGE_KEY = "findaline.filters";

export const dh = (l: Line) => Math.abs(l.e1 - l.e2);
export const lowerAnchor = (l: Line) => Math.min(l.e1, l.e2);
export const wallAngle = (l: Line) => Math.max(l.angle1, l.angle2);
const known = (v: number | null) => v === null ? NaN : v;  // NaN fails every limit
/** The prominence of the more (Math.max) or less (Math.min) prominent anchor; NaN if not known. */
export const prominence = (l: Line, pick: (a: number, b: number) => number) => pick(known(l.prom1), known(l.prom2));
export const drop = (l: Line) => Math.min(known(l.drop1), known(l.drop2));

export function passes(l: Line, f: Filters): boolean {
  return (f.lenMin === null || l.len >= f.lenMin) && (f.lenMax === null || l.len <= f.lenMax) &&
    (f.clrMin === null || l.clr >= f.clrMin) &&
    (f.clrNMin === null || l.clrN === null || l.clrN >= f.clrNMin) &&
    (f.dhMax === null || dh(l) <= f.dhMax) && (f.elevMin === null || lowerAnchor(l) >= f.elevMin) &&
    (f.heightMin === null || l.hmax >= f.heightMin) && (f.meanMin === null || l.hmean >= f.meanMin) &&
    (f.highPctMin === null || l.highPct >= f.highPctMin) && (f.angleMax === null || wallAngle(l) <= f.angleMax) &&
    (f.promMin === null || prominence(l, Math.max) >= f.promMin) &&
    (f.promBothMin === null || prominence(l, Math.min) >= f.promBothMin) &&
    (f.dropMin === null || drop(l) >= f.dropMin) &&
    (f.water === "all" || (f.water === "water" ? l.water > 0 : l.water === 0)) &&
    (!f.hideFilled || l.filled === 0);
}

/** How many filters are set. */
export function active(f: Filters): number {
  return (Object.keys(NO_FILTERS) as (keyof Filters)[]).filter((k) => f[k] !== NO_FILTERS[k]).length;
}

export interface Range { min: number; max: number }

/** The ranges of the run's values the filters offer (null: no line has the value). */
export interface FilterRanges {
  len: Range | null;
  clr: Range | null;
  clrN: Range | null;
  dh: Range | null;
  elev: Range | null;
  hmax: Range | null;
  hmean: Range | null;
  highPct: Range | null;
  highAbove: number;  // m, the height of highPct
  angle: Range | null;
  prom: Range | null;
  drop: Range | null;
  group: Range | null;
  water: boolean;   // some lines are over water
  filled: boolean;  // some lines are over filled gaps
  favourites: boolean;  // the user's favourite lines are known
}

/** Which lines are shown and how they are grouped. */
export interface LinesState {
  group: boolean;
  distance: number;
  by: string;
  showClean: boolean;
  showOver: boolean;
}

/** The range of a value over the lines (for the sliders); null without lines. */
export function range(lines: Line[], value: (l: Line) => number | null): Range | null {
  let min = Infinity, max = -Infinity;
  for (const l of lines) {
    const v = value(l);
    if (v === null) continue;
    if (v < min) min = v;
    if (v > max) max = v;
  }
  return min <= max ? {min, max} : null;
}

export function savedFilters(): Filters {
  try {
    return {...NO_FILTERS, ...JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"), favOnly: false};
  } catch {
    return {...NO_FILTERS};
  }
}

export function saveFilters(f: Filters) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(f));
  } catch {
    // storage blocked: the filters are just not kept
  }
}
