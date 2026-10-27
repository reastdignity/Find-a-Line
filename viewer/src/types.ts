import type { Geometry } from "geojson";

// The data of one run, written by mapping.save_map into <run>/data.js
// (window.FINDALINE_RUN), and the list of runs in runs/runs.js (window.FINDALINE_RUNS).

export interface Rigging {
  pretension: number;      // kN
  stretch: number;         // % at 10 kN
  webbing_weight: number;  // g/m
  athlete_mass: number;    // kg
  fall_zone: number;       // m
}

/** Terrain profile of a line (helpers.line_profile). */
export interface Profile {
  d: number[];                    // distance from anchor 1, m
  z: (number | null)[];           // terrain
  s?: (number | null)[] | null;   // trees/buildings
  w?: number[] | null;            // 1: water
  g?: number[] | null;            // 1: filled gap
  min: [number, number];          // (distance, clearance) of the smallest clearance over terrain
  min_nmpt?: [number, number] | null;
  max_height?: [number, number];  // (distance, height) of the webbing highest above the terrain
  water_m: number;
  filled_m: number;
}

// One line as an array (compact):
// [lat1, lon1, lat2, lon2, x1, y1, x2, y2, length, clearance, clearance_nmpt, elev1, elev2, over,
//  metres over water, metres over filled gaps, largest and mean height of the webbing above the
//  terrain, % of the line at least high_above, wall angle at anchor 1 and 2 (degrees from the
//  downslope direction), profile] (lines.js leaves out the profile)
export type LineRow = [number, number, number, number, number, number, number, number, number, number,
  number | null, number, number, boolean, number, number, number, number, number, number, number,
  (Profile | null)?];

export interface Line {
  lat1: number; lon1: number; lat2: number; lon2: number;
  x1: number; y1: number; x2: number; y2: number;  // metres in the grid CRS, for grouping
  len: number; clr: number; clrN: number | null; e1: number; e2: number;
  over: boolean; water: number; filled: number;  // metres over water and over filled gaps
  hmax: number; hmean: number; highPct: number;  // webbing above the terrain (NaN: not known)
  angle1: number; angle2: number;  // degrees between the line and the downslope direction at the anchors
  prof: Profile | null;
  run?: OtherRun;  // set for the lines of another run
}

/** Another run shown with this one ("Other runs"). */
export interface OtherRun {
  info: RunInfo;
  color: string;
}

/** <run>/lines.js: window.FINDALINE_LINES[id], the lines without profiles. */
export interface RunLines {
  crs: string;
  area: Geometry;
  lines: LineRow[];  // without the profile at the end
}

export interface Ortho {
  name: string;
  url: string | null;   // WMS; null: the base tiles only
  layer: string;
  attribution: string;
  // tiles ({z}/{x}/{y}) with full coverage under the WMS, where it has no imagery (the WMS
  // is then requested as a transparent PNG; cached tiles have both already), or the
  // orthophoto itself without a WMS
  base: {url: string; max_zoom: number} | null;
  cache: string | null; // {z}/{x}/{y}.jpg folder relative to the map, or null
}

export interface RunInfo {
  id: string;
  name: string;
  created: string;
  description: string;
  has_map?: boolean;
  has_lines?: boolean;
  center?: {lat: number; lon: number};
  bbox?: [number, number, number, number] | null;  // west, south, east, north
}

export interface RunData {
  run: RunInfo;
  center: [number, number];
  area: Geometry;
  crs: string;                    // of x1, y1, x2, y2
  ortho: Ortho | null;
  distance: number;               // grouping distance, m
  by: "clearance" | "longest";
  model: "formula" | "sag";
  rigging: Rigging;
  slope: number;                  // formula: metres of drop per metre from the nearer anchor
  leash_fall: number;
  high_above: number;             // m: from this height above the terrain a part of a line is high
  max_zoom: number;
  lines: LineRow[];
}

declare global {
  interface Window {
    FINDALINE_RUN?: RunData;
    FINDALINE_RUNS?: RunInfo[];
    FINDALINE_LINES?: Record<string, RunLines>;
  }
}

export function toLine(d: LineRow, run?: OtherRun): Line {
  return {lat1: d[0], lon1: d[1], lat2: d[2], lon2: d[3], x1: d[4], y1: d[5], x2: d[6], y2: d[7],
          len: d[8], clr: d[9], clrN: d[10], e1: d[11], e2: d[12], over: d[13], water: d[14], filled: d[15],
          hmax: d[16], hmean: d[17], highPct: d[18], angle1: d[19], angle2: d[20], prof: d[21] || null, run};
}
