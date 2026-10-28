// The list of lines next to the map: what can be sorted, and how. Kept in the browser for every map.

import { dh, drop, lowerAnchor, prominence, wallAngle } from "./filters";
import type { Line } from "./types";

/** A row of the list: a line and the size of its group (1 without grouping), with the lines of its
 * group in their order (the first is the representative). */
export interface Row<T = Line> {
  line: T;
  n: number;
  members?: T[];
}

export interface SortKey {
  key: string;
  label: string;     // in the "sort by" choice
  head: string;      // the column's heading
  value(r: Row): number | null;  // null or NaN: not known, always last
  digits: number;
  unit: string;
  desc: boolean;     // the first direction: largest first
}

const known = (v: number | null) => v === null || !isFinite(v) ? null : v;

export const SORT_KEYS: SortKey[] = [
  {key: "len", label: "length", head: "length", value: (r) => r.line.len, digits: 0, unit: " m", desc: true},
  {key: "clr", label: "clearance over terrain", head: "clear.", value: (r) => r.line.clr, digits: 1, unit: " m",
   desc: true},
  {key: "clrN", label: "clearance over trees/buildings", head: "trees", value: (r) => r.line.clrN, digits: 1,
   unit: " m", desc: true},
  {key: "hmax", label: "height above the terrain", head: "height", value: (r) => known(r.line.hmax), digits: 0,
   unit: " m", desc: true},
  {key: "hmean", label: "mean height", head: "mean", value: (r) => known(r.line.hmean), digits: 0, unit: " m",
   desc: true},
  {key: "highPct", label: "share of the line high", head: "high", value: (r) => known(r.line.highPct), digits: 0,
   unit: " %", desc: true},
  {key: "dh", label: "anchor height difference", head: "Δh", value: (r) => dh(r.line), digits: 1, unit: " m",
   desc: false},
  {key: "elev", label: "lower anchor elevation", head: "elev.", value: (r) => lowerAnchor(r.line), digits: 0,
   unit: " m", desc: true},
  {key: "angle", label: "angle to the wall", head: "angle", value: (r) => known(wallAngle(r.line)), digits: 0,
   unit: "°", desc: false},
  {key: "prom", label: "prominence of an anchor", head: "prom.", value: (r) => known(prominence(r.line, Math.max)),
   digits: 0, unit: " m", desc: true},
  {key: "promBoth", label: "prominence of both anchors", head: "prom. both",
   value: (r) => known(prominence(r.line, Math.min)), digits: 0, unit: " m", desc: true},
  {key: "drop", label: "drop below both anchors", head: "drop", value: (r) => known(drop(r.line)), digits: 0,
   unit: " m", desc: true},
  {key: "n", label: "lines in the group", head: "lines", value: (r) => r.n, digits: 0, unit: "", desc: true},
];

export function sortKey(key: string): SortKey {
  return SORT_KEYS.find((k) => k.key === key) || SORT_KEYS[0];
}

/** The rows sorted by a key (stable: equal values keep their order); unknown values last in both directions. */
export function sortRows<T extends Row<any>>(rows: T[], key: SortKey, desc: boolean): T[] {
  const sign = desc ? -1 : 1;
  return rows.map((r, i) => ({r, i, v: key.value(r)}))
    .sort((a, b) => {
      const av = a.v === null || isNaN(a.v), bv = b.v === null || isNaN(b.v);
      if (av || bv) return av === bv ? a.i - b.i : av ? 1 : -1;
      return sign * (a.v! - b.v!) || a.i - b.i;
    })
    .map((x) => x.r);
}

export function formatValue(key: SortKey, r: Row): string {
  const v = key.value(r);
  return v === null || isNaN(v) ? "–" : v.toFixed(key.digits);
}

export interface ListState {
  sort: string;
  desc: boolean;
  inView: boolean;  // only the lines in the map's view
  open: boolean;    // the list is unfolded
}

export const LIST_DEFAULTS: ListState = {sort: "len", desc: true, inView: false, open: false};
const STORAGE_KEY = "findaline.list";

export function savedList(): ListState {
  try {
    return {...LIST_DEFAULTS, ...JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}")};
  } catch {
    return {...LIST_DEFAULTS};
  }
}

export function saveList(s: ListState) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch {
    // storage blocked: the choice is just not kept
  }
}
