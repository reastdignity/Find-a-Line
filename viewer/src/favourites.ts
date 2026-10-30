// Favourite lines of the user (on the public site the signed-in one, locally the app's one user),
// kept by the server (web.py /api/runs/<run>/favourites, /api/favourites): a map marks them, the
// app lists all of them in their folders (nested, as a browser's bookmarks; folders.ts). Both export
// them as GeoJSON or CSV.

import { lineCoords } from "./keys";
import type { Line } from "./types";

/** The values of a line the server keeps with a favourite (web.FavouriteLine; null: not known). */
export interface FavouriteData {
  len: number; clr: number; clrN: number | null; e1: number; e2: number; over: boolean;
  water: number; filled: number; hmax: number | null; hmean: number | null; highPct: number | null;
  angle1: number | null; angle2: number | null; prom1: number | null; prom2: number | null;
  drop1: number | null; drop2: number | null;
}

/** A favourite as /api/favourites lists it. */
export interface Favourite {
  run_id: string;
  run_name: string;
  run_created: string;
  line: string;  // lat1,lon1,lat2,lon2 (keys.ts lineCoords)
  data: FavouriteData;
  created: string;
  folder_id: number | null;  // null: the top level
}

/** A folder of favourites (jobs.favourite_folders). */
export interface FavouriteFolder {
  id: number;
  parent_id: number | null;  // null: the top level
  name: string;
  created: string;
}

/** The user's favourites of one run as the server gives them to a map or the result view. */
export interface RunFavourites {
  lines: string[];                       // lineCoords
  in: Record<string, number | null>;     // the folder of each
  folders: FavouriteFolder[];
}

const known = (v: number | null) => v === null || !isFinite(v) ? null : v;

export function favouriteData(l: Line): FavouriteData {
  return {len: l.len, clr: l.clr, clrN: known(l.clrN), e1: l.e1, e2: l.e2, over: l.over, water: l.water,
          filled: l.filled, hmax: known(l.hmax), hmean: known(l.hmean), highPct: known(l.highPct),
          angle1: known(l.angle1), angle2: known(l.angle2), prom1: known(l.prom1), prom2: known(l.prom2),
          drop1: known(l.drop1), drop2: known(l.drop2)};
}

/** A favourite as a line of the viewer (without grid coordinates and profile). */
export function favouriteLine(f: Favourite): Line {
  const [lat1, lon1, lat2, lon2] = f.line.split(",").map(Number), d = f.data, nan = (v: number | null) => v ?? NaN;
  return {lat1, lon1, lat2, lon2, x1: 0, y1: 0, x2: 0, y2: 0, len: d.len, clr: d.clr, clrN: d.clrN, e1: d.e1,
          e2: d.e2, over: d.over, water: d.water, filled: d.filled, hmax: nan(d.hmax), hmean: nan(d.hmean),
          highPct: nan(d.highPct), angle1: nan(d.angle1), angle2: nan(d.angle2), prom1: d.prom1, prom2: d.prom2,
          drop1: d.drop1, drop2: d.drop2, prof: null};
}

let mapToken = "";  // the token of a map's changes, from the server's answer (a map is a static page)

/** The user's favourites of a run with their folders; rejects when the server keeps none for this page. */
export async function loadRunFavourites(runId: string): Promise<RunFavourites> {
  const response = await fetch("/api/runs/" + encodeURIComponent(runId) + "/favourites");
  if (!response.ok) throw new Error(response.statusText);
  const body = await response.json();
  mapToken = body.token;
  return {lines: body.lines, in: body.in || {}, folders: body.folders || []};
}

/** The user's favourite lines of a run (lineCoords): the map's. */
export async function loadFavourites(runId: string): Promise<string[]> {
  return (await loadRunFavourites(runId)).lines;
}

/** A change of the user's favourites; the server's message when it refuses. */
async function post(url: string, body: unknown, token: string, method = "POST"): Promise<any> {
  const response = await fetch(url, {
    method, headers: {"Content-Type": "application/json", "X-Findaline-Token": token},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const answer = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof answer.detail === "string" ? answer.detail : response.statusText);
  return answer;
}

/** Mark a line of a run as a favourite (into `folder`, null: the top level), or unmark it; `token`: the
 *  app's own (a map uses its run's). */
export async function saveFavourite(runId: string, l: Line, on: boolean, token = mapToken,
                                    folder: number | null = null): Promise<void> {
  await post("/api/runs/" + encodeURIComponent(runId) + "/favourites",
             {line: lineCoords(l), favourite: on, ...(on ? {data: favouriteData(l), folder} : {})}, token);
}

/** Move a favourite (`line`: lineCoords) of a run into a folder (null: the top level). */
export async function moveFavourite(runId: string, line: string, folder: number | null, token = mapToken): Promise<void> {
  await post("/api/runs/" + encodeURIComponent(runId) + "/favourites/folder", {line, folder}, token);
}

/** A new folder in `parent` (null: the top level); its id. */
export async function addFolder(name: string, parent: number | null, token = mapToken): Promise<number> {
  return (await post("/api/favourites/folders", {name, parent}, token)).id;
}

/** Rename a folder and/or move it (`parent` given: null is the top level). */
export async function changeFolder(id: number, change: {name?: string; parent?: number | null},
                                   token = mapToken): Promise<void> {
  await post("/api/favourites/folders/" + id, change, token);
}

/** Delete a folder with what is in it; how many folders and favourites went. */
export async function deleteFolder(id: number, token = mapToken): Promise<{folders: number; favourites: number}> {
  return post("/api/favourites/folders/" + id, undefined, token, "DELETE");
}

/** A line to export with its run (and its folder's path, "Trips / Tatras", when it has one). */
export interface Exported {
  line: Line;
  runId: string;
  runName: string;
  folder?: string;
}

// The columns of an export: the names of lines.csv (runs.FIELDS) for the values the viewer has
const COLUMNS: [string, (e: Exported) => string | number | boolean | null][] = [
  ["run_id", (e) => e.runId], ["run_name", (e) => e.runName], ["folder", (e) => e.folder ?? ""],
  ["length_m", (e) => e.line.len], ["clearance_m", (e) => e.line.clr], ["clearance_nmpt_m", (e) => e.line.clrN],
  ["over_trees_buildings", (e) => e.line.over], ["water_m", (e) => e.line.water], ["filled_m", (e) => e.line.filled],
  ["height_max_m", (e) => known(e.line.hmax)], ["height_mean_m", (e) => known(e.line.hmean)],
  ["high_pct", (e) => known(e.line.highPct)],
  ["anchor1_lat", (e) => e.line.lat1], ["anchor1_lon", (e) => e.line.lon1], ["anchor1_elev_m", (e) => e.line.e1],
  ["anchor1_wall_angle", (e) => known(e.line.angle1)], ["anchor1_prominence_m", (e) => e.line.prom1],
  ["anchor1_drop_m", (e) => e.line.drop1],
  ["anchor2_lat", (e) => e.line.lat2], ["anchor2_lon", (e) => e.line.lon2], ["anchor2_elev_m", (e) => e.line.e2],
  ["anchor2_wall_angle", (e) => known(e.line.angle2)], ["anchor2_prominence_m", (e) => e.line.prom2],
  ["anchor2_drop_m", (e) => e.line.drop2],
];

/** A CSV cell: text quoted, and kept from being read as a formula by a spreadsheet. */
function cell(v: string | number | boolean | null): string {
  if (v === null) return "";
  if (typeof v !== "string") return String(v);
  const text = /^[=+\-@\t\r]/.test(v) ? "'" + v : v;
  return '"' + text.replace(/"/g, '""') + '"';
}

export function favouritesCsv(lines: Exported[]): string {
  return [COLUMNS.map(([name]) => name).join(","),
          ...lines.map((e) => COLUMNS.map(([, value]) => cell(value(e))).join(","))].join("\r\n") + "\r\n";
}

export function favouritesGeoJSON(lines: Exported[]): string {
  return JSON.stringify({type: "FeatureCollection", features: lines.map((e) => ({
    type: "Feature",
    geometry: {type: "LineString", coordinates: [[e.line.lon1, e.line.lat1], [e.line.lon2, e.line.lat2]]},
    properties: Object.fromEntries(COLUMNS.map(([name, value]) => [name, value(e)])),
  }))}, null, 1);
}

/** Give the browser a text as a file to save. */
export function download(name: string, text: string, type: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], {type}));
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/** The buttons' action: favourites as favourites.geojson or favourites.csv. */
export function exportFavourites(lines: Exported[], kind: "geojson" | "csv") {
  if (kind === "csv") download("favourites.csv", favouritesCsv(lines), "text/csv");
  else download("favourites.geojson", favouritesGeoJSON(lines), "application/geo+json");
}
