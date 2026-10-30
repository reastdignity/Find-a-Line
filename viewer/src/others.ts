// Other runs shown with the current one: their lines.js, colours and which are nearby.

import type { Line, RunInfo, RunLines } from "./types";

export const NEARBY_KM = 20;
// Colours of other runs (not the pink and orange of the current run)
const PALETTE = ["#1f77b4", "#2ca02c", "#9467bd", "#17becf", "#8c564b", "#7f7f00", "#1a237e", "#d62728"];
const EARTH_RADIUS = 6371008.8;

/** The same colour for a run on every map. */
export function colorOf(id: string): string {
  let hash = 0;
  for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) | 0;
  return PALETTE[Math.abs(hash) % PALETTE.length];
}

/** Load ../<id>/lines.js (a script: pages opened from disk cannot fetch JSON); `base`: the folder of the
 * runs relative to the page (the app's pages: "/runs/"). */
export function loadLines(id: string, base = "../"): Promise<RunLines> {
  const loaded = window.FINDALINE_LINES?.[id];
  if (loaded) return Promise.resolve(loaded);
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = base + encodeURIComponent(id) + "/lines.js";
    script.onload = () => {
      const lines = window.FINDALINE_LINES?.[id];
      if (lines) resolve(lines); else reject(new Error("no lines in " + script.src));
    };
    script.onerror = () => reject(new Error("could not load " + script.src));
    document.head.appendChild(script);
  });
}

/** Distance in km between two boxes (west, south, east, north); 0 when they overlap. */
export function boxDistanceKm(a: number[], b: number[]): number {
  const dLon = Math.max(0, b[0] - a[2], a[0] - b[2]), dLat = Math.max(0, b[1] - a[3], a[1] - b[3]);
  const lat = (a[1] + a[3]) / 2;
  return Math.hypot(dLon * Math.cos(lat * Math.PI / 180), dLat) * Math.PI / 180 * EARTH_RADIUS / 1000;
}

/** Runs that can be shown (they have lines.js), with their distance, nearest first. */
export function listOthers(runs: RunInfo[], currentId: string, box: number[]): {run: RunInfo; km: number}[] {
  return runs.filter((r) => r.id !== currentId && r.has_lines && r.bbox)
    .map((run) => ({run, km: boxDistanceKm(box, run.bbox!)}))
    .sort((a, b) => a.km - b.km);
}

/** Lines with coordinates in metres of a local plane around (lat0, lon0), for grouping
 * lines of runs computed in different coordinate systems together. */
export function inLocalPlane(lines: Line[], lat0: number, lon0: number): Line[] {
  const k = Math.PI / 180 * EARTH_RADIUS, c = Math.cos(lat0 * Math.PI / 180);
  return lines.map((l) => ({...l, x1: (l.lon1 - lon0) * k * c, y1: (l.lat1 - lat0) * k,
                            x2: (l.lon2 - lon0) * k * c, y2: (l.lat2 - lat0) * k}));
}
