// Filters of the lines on the map, applied before grouping (the group size after it).
// null means no limit. Kept in the browser for every map.

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
  water: "all" | "water" | "land";
  hideFilled: boolean;        // hide lines over gaps filled from another source
  groupMin: number | null;    // similar lines in a group (with grouping on)
}

export const NO_FILTERS: Filters = {lenMin: null, lenMax: null, clrMin: null, clrNMin: null, dhMax: null,
                                    elevMin: null, heightMin: null, meanMin: null, highPctMin: null,
                                    water: "all", hideFilled: false, groupMin: null};
const STORAGE_KEY = "findaline.filters";

export const dh = (l: Line) => Math.abs(l.e1 - l.e2);
export const lowerAnchor = (l: Line) => Math.min(l.e1, l.e2);

export function passes(l: Line, f: Filters): boolean {
  return (f.lenMin === null || l.len >= f.lenMin) && (f.lenMax === null || l.len <= f.lenMax) &&
    (f.clrMin === null || l.clr >= f.clrMin) &&
    (f.clrNMin === null || l.clrN === null || l.clrN >= f.clrNMin) &&
    (f.dhMax === null || dh(l) <= f.dhMax) && (f.elevMin === null || lowerAnchor(l) >= f.elevMin) &&
    (f.heightMin === null || l.hmax >= f.heightMin) && (f.meanMin === null || l.hmean >= f.meanMin) &&
    (f.highPctMin === null || l.highPct >= f.highPctMin) &&
    (f.water === "all" || (f.water === "water" ? l.water > 0 : l.water === 0)) &&
    (!f.hideFilled || l.filled === 0);
}

/** How many filters are set. */
export function active(f: Filters): number {
  return (Object.keys(NO_FILTERS) as (keyof Filters)[]).filter((k) => f[k] !== NO_FILTERS[k]).length;
}

export interface Range { min: number; max: number }

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
    return {...NO_FILTERS, ...JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}")};
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
