// OpenStreetMap extracts: download the extract of the region at the map centre, so the
// coastline and the cliffs come from it instead of the (often overloaded) Overpass API.

import { useEffect, useState } from "preact/hooks";
import { api, type OsmRegion, type OsmState } from "./api";
import type { PlaceMap } from "./PlaceMap";

export function OsmPanel({place, onStarted}: {place: PlaceMap; onStarted(): void}) {
  const [state, setState] = useState<OsmState | null>(null);
  const [error, setError] = useState("");
  const [where, setWhere] = useState("");

  const look = () => {
    const c = place.map.getCenter();
    setWhere(`${c.lat.toFixed(4)}, ${c.lng.toFixed(4)}`);
    setError("");
    api.osm(c.lat, c.lng).then(setState, (e) => setError(e.message));
  };
  useEffect(look, [place]);

  const start = (action: "download" | "update", region: string) =>
    api.osmJob(action, region).then(onStarted, (e) => setError(e.message));

  const row = (r: OsmRegion) => <div class="fa-run">
    <b>{r.name}</b> <span class="fa-hint">{r.id}</span>{" "}
    {r.downloaded ? <span class="fa-hint">downloaded</span>
      : <button class="fa-small" onClick={() => start("download", r.id)}>download</button>}
  </div>;

  return <div class="fa-form">
    <p class="fa-hint">The coastline (sea) and the cliffs of <i>Coast, cliffs</i> come from the Overpass API, which is
      often overloaded. With the OpenStreetMap extract of a country or region (from Geofabrik, updated with daily
      diffs) they come from your disk. A country is large (Poland about 2 GB, Malta 9 MB); a smaller region is
      enough when the search stays inside it.</p>
    <h3>At the map centre</h3>
    <p class="fa-hint">{where} <button class="fa-small" onClick={look}>look again here</button></p>
    {error && <p class="fa-error">{error}</p>}
    {state && (state.regions.length ? state.regions.map(row) : <p class="fa-hint">No Geofabrik region here.</p>)}
    <h3>Downloaded</h3>
    {state && (state.extracts.length ? state.extracts.map((e) => <div class="fa-run">
      <b>{e.id}</b> <span class="fa-hint">{e.mb} MB, data of {(e.data || "?").slice(0, 10)},
        checked {(e.checked || "?").slice(0, 10)}</span>{" "}
      <button class="fa-small" onClick={() => start("update", e.id)}>update</button>
    </div>) : <p class="fa-hint">None yet.</p>)}
  </div>;
}
