// The request of a new search from the place and the parameters, for the search panel (search/).
// Pure functions, no Preact.

import type { FeatureCollection } from "geojson";
import type { Field, SearchRequest } from "./api";
import type { Mode } from "./PlaceMap";
import { quantity, t } from "../i18n";

/** Fields set by the "Where" part of the form, not in the parameter groups. */
export const PLACE_FIELDS = new Set(["lat", "lon", "northing", "easting", "area", "buffer", "within", "radius", "name"]);

/** Where a search goes. */
export interface Place {
  mode: Mode;
  point: [number, number] | null;
  radius: number;
  buffer: number;
  preset: string;     // with mode "preset": "cliffs", "coast" or "osm" (a named OSM area)
  osmName: string;
  within: string;
  name: string;
}

/** An area file read in the browser (files.ts). */
export interface AreaFile { name: string; areas: FeatureCollection }

/** The starting state of a form from a job's request ("edit"): the place and the changed values. */
export function fromRequest(request: SearchRequest | null | undefined, byName: Record<string, Field>):
    Place & {values: Record<string, unknown>} {
  const p = (request?.params || {}) as Record<string, any>;
  const values: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(p)) {
    if (!PLACE_FIELDS.has(key) && byName[key] && value !== byName[key].default) values[key] = value;
  }
  const area = p.area as string | undefined;
  return {
    mode: (request?.geometry ? "draw" : area ? "preset" : "point") as Mode,
    point: p.lat !== undefined && p.lat !== null ? [p.lat, p.lon] as [number, number] : null,
    radius: Number(p.radius ?? byName.radius?.default ?? 1000),
    buffer: Number(p.buffer ?? byName.buffer?.default ?? 100),
    preset: area?.startsWith("osm:") ? "osm" : area || "cliffs",
    osmName: area?.startsWith("osm:") ? area.slice(4) : "",
    within: (p.within as string) || "",
    name: (p.name as string) || "",
    values,
  };
}

/** --within as the server takes it: an OSM name gets "osm:", a box S,W,N,E stays. */
export function withinValue(text: string): string {
  const trimmed = text.trim();
  return trimmed.startsWith("osm:") || /^[-\d.,\s]+$/.test(text) ? trimmed : "osm:" + trimmed;
}

/** The request of a search, or what is missing (a message). `drawn`: the drawn areas of the map. */
export function buildRequest(place: Place, values: Record<string, unknown>, drawn: FeatureCollection | null,
                             file: AreaFile | null): SearchRequest | string {
  const params: Record<string, unknown> = {...values};
  if (place.name.trim()) params.name = place.name.trim();
  let geometry = null;
  if (place.mode === "point") {
    if (!place.point) return t("error.no_centre");
    Object.assign(params, {lat: place.point[0], lon: place.point[1], radius: place.radius});
  } else if (place.mode === "draw") {
    geometry = drawn;
    if (!geometry) return t("error.need_drawing");
    params.buffer = place.buffer;
  } else if (place.mode === "file") {
    if (!file) return t("error.need_file");
    geometry = file.areas;
    params.buffer = place.buffer;
  } else {
    if (place.preset === "osm") {
      if (!place.osmName.trim()) return t("error.need_osm_name");
      params.area = "osm:" + place.osmName.trim();
    } else {
      if (!place.within.trim()) return t("error.within_missing");
      params.area = place.preset;
    }
    params.buffer = place.buffer;
    if (place.within.trim()) params.within = withinValue(place.within);
  }
  return {params, geometry};
}

/** The value of a field from the text of its input: empty is the default (null for an optional field). */
export function parseValue(field: Field, text: string): unknown {
  const value = text.trim();
  const numeric = field.type === "number" || field.type === "integer";
  return value === "" ? (field.optional ? null : field.default) : numeric ? Number(value) : value;
}

/** The changed values with one more: a value equal to the default is not kept. */
export function withValue(values: Record<string, unknown>, field: Field, value: unknown): Record<string, unknown> {
  const next = {...values};
  if (value === field.default) delete next[field.name]; else next[field.name] = value;
  return next;
}

/** What is wrong with a value under the field's bounds (the site's limits), or "": the texts of the
 *  server's errors (error.not_a_number, error.at_least, ...), with the field's label. */
export function valueProblem(field: Field, value: unknown): string {
  if (value === null || value === undefined || typeof value !== "number") return "";
  const label = field.label || field.name;
  if (Number.isNaN(value)) return t("error.not_a_number", {label});
  if (field.type === "integer" && !Number.isInteger(value)) return t("error.not_whole_number", {label});
  if (field.min !== null && value < field.min) return t("error.at_least", {label, min: quantity(field.name, field.min)});
  if (field.max !== null && value > field.max) return t("error.at_most", {label, max: quantity(field.name, field.max)});
  return "";
}
