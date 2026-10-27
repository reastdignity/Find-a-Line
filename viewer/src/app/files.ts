// Area files (GeoJSON, KML, GPX) read in the browser: the server only ever gets the
// geometry, which it checks (area.check_geometry) without GDAL or a file.
// KML and GPX go through @tmcw/togeojson on the browser's own XML parser.

import { gpx, kml } from "@tmcw/togeojson";
import type { Feature, FeatureCollection, GeoJSON, Geometry } from "geojson";

export const MAX_FILE = 5_000_000;  // bytes of an area file

function features(data: GeoJSON): Feature[] {
  if (data.type === "FeatureCollection") return data.features;
  if (data.type === "Feature") return [data];
  return [{type: "Feature", properties: {}, geometry: data as Geometry}];
}

/** The shapes of an area file as a collection of features without properties. */
export async function readAreaFile(file: File): Promise<FeatureCollection> {
  if (file.size > MAX_FILE) throw new Error(`the file is larger than ${MAX_FILE / 1_000_000} MB`);
  const suffix = file.name.toLowerCase().split(".").pop() || "";
  const text = await file.text();
  let data: GeoJSON;
  if (suffix === "kml" || suffix === "gpx") {
    const doc = new DOMParser().parseFromString(text, "application/xml");
    if (doc.getElementsByTagName("parsererror").length) throw new Error(`could not read the ${suffix.toUpperCase()} file`);
    data = suffix === "kml" ? kml(doc) as FeatureCollection : gpx(doc);
  } else if (suffix === "geojson" || suffix === "json") {
    try {
      data = JSON.parse(text);
    } catch {
      throw new Error("could not read the GeoJSON file");
    }
    if (!data || typeof data !== "object" || typeof data.type !== "string") throw new Error("not a GeoJSON file");
  } else {
    throw new Error("GeoJSON, KML or GPX expected");
  }
  // Only the shapes go to the server (names, styles and track times are not needed)
  const shapes = features(data).filter((f) => f && f.geometry)
    .map((f): Feature => ({type: "Feature", properties: {}, geometry: f.geometry}));
  if (!shapes.length) throw new Error("no geometries in the file");
  return {type: "FeatureCollection", features: shapes};
}
