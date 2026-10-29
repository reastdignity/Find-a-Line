// The areas of the searches waiting or running, drawn on the map while My searches is open (B2): the
// circle or the drawn/uploaded areas of each job's request (/api/jobs/<id>). Areas named by a place
// (an OSM name, cliffs or coast within a box) are known only to the worker: they are not drawn.

import L from "leaflet";
import { useEffect } from "preact/hooks";
import { api, type Schema, type SearchRequest } from "../api";
import type { PlaceMap } from "../PlaceMap";
import { FINISHED, type JobView } from "../useJobs";

const STYLE: L.PathOptions = {color: "#2350c8", weight: 2, dashArray: "6 4", fillOpacity: 0.06, interactive: false};

/** The layer of a request's area, or null when the request names its area instead. */
export function requestArea(request: SearchRequest, schema: Schema | null): L.Layer | null {
  if (request.geometry) return L.geoJSON(request.geometry as any, {style: STYLE});
  const p = request.params;
  if (typeof p.lat !== "number" || typeof p.lon !== "number") return null;
  const fallback = schema?.fields.find((f) => f.name === "radius")?.default;
  const radius = Number(p.radius ?? fallback);
  return radius > 0 ? L.circle([p.lat, p.lon], {...STYLE, radius}) : null;
}

/** Draw the areas of the active `jobs` on `place`'s map while `on`. */
export function useJobAreas(place: PlaceMap | null, schema: Schema | null, jobs: JobView[], on: boolean) {
  const active = jobs.filter((j) => !FINISHED.includes(j.status) && !j.title.startsWith("OSM ")).map((j) => j.id);
  const key = active.join(",");
  useEffect(() => {
    if (!place || !on || !active.length) return;
    const layer = L.layerGroup().addTo(place.map);
    let live = true;
    for (const id of active) {
      api.job(id).then((job) => {
        const area = live && job.request ? requestArea(job.request, schema) : null;
        area?.addTo(layer);
      }, () => {});
    }
    return () => { live = false; layer.remove(); };
  }, [place, schema, on, key]);
}
