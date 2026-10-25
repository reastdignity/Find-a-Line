// A new search: where (a point, drawn areas, a file, or coast/cliffs/an OSM area),
// the parameters (generated from the schema of params.py), the estimate, the start.

import type { FeatureCollection } from "geojson";
import { useEffect, useState } from "preact/hooks";
import { api, type Estimate, type Field, type SearchRequest } from "./api";
import type { Mode, PlaceMap } from "./PlaceMap";

// Fields set by the "Where" part of the form, not in the parameter groups
const PLACE_FIELDS = new Set(["lat", "lon", "northing", "easting", "area", "buffer", "within", "radius", "name"]);
const GROUP_ORDER = ["search", "data", "rigging", "output"];

function label(name: string): string {
  return name.replace(/_/g, " ");
}

function Input({field, value, onChange}: {field: Field; value: unknown; onChange(v: unknown): void}) {
  const shown = value === undefined ? field.default : value;
  if (field.type === "boolean") {
    return <input type="checkbox" checked={!!shown} onChange={(e) => onChange(e.currentTarget.checked)} />;
  }
  if (field.type === "choice") {
    return <select value={shown === null || shown === undefined ? "" : String(shown)}
                   onChange={(e) => onChange(e.currentTarget.value === "" ? null : e.currentTarget.value)}>
      {field.optional && <option value="">(default)</option>}
      {field.choices!.map((c) => <option value={c}>{c}</option>)}
    </select>;
  }
  const numeric = field.type === "number" || field.type === "integer";
  const text = shown === null || shown === undefined ? "" : String(shown);
  const placeholder = field.optional ? "default" : "";
  const changed = (e: Event) => {
    const value = (e.currentTarget as HTMLInputElement).value.trim();
    onChange(value === "" ? (field.optional ? null : field.default) : numeric ? Number(value) : value);
  };
  // The page is drawn again on every event of the queue: a number field keeps what is
  // being typed (defaultValue) and is read when it is left; a text field follows every key
  return numeric
    ? <input type="number" step={field.type === "integer" ? 1 : "any"} defaultValue={text}
             placeholder={placeholder} onChange={changed} />
    : <input type="text" value={text} placeholder={placeholder} onInput={changed} />;
}

export interface SearchFormProps {
  place: PlaceMap;
  fields: Field[];
  groups: Record<string, string>;
  onStarted(id: string): void;
}

export function SearchForm({place, fields, groups, onStarted}: SearchFormProps) {
  const byName = Object.fromEntries(fields.map((f) => [f.name, f]));
  const [mode, setMode] = useState<Mode>("point");
  const [point, setPoint] = useState<[number, number] | null>(null);
  const [radius, setRadius] = useState(Number(byName.radius?.default ?? 1000));
  const [buffer, setBuffer] = useState(Number(byName.buffer?.default ?? 100));
  const [preset, setPreset] = useState("cliffs");
  const [osmName, setOsmName] = useState("");
  const [within, setWithin] = useState("");
  const [name, setName] = useState("");
  const [file, setFile] = useState<{name: string; areas: FeatureCollection} | null>(null);
  const [drawn, setDrawn] = useState(0);  // changes when the drawn areas change
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    place.onPoint = (lat, lon) => setPoint([lat, lon]);
    place.onDrawn = () => setDrawn((n) => n + 1);
  }, [place]);
  useEffect(() => place.setMode(mode), [place, mode]);
  useEffect(() => { if (point) place.setPoint(point[0], point[1], radius); }, [place, point, radius]);
  // Any change makes the estimate stale
  useEffect(() => { setEstimate(null); setError(""); },
            [mode, point, radius, buffer, preset, osmName, within, file, drawn, values]);

  const request = (): SearchRequest | string => {
    const params: Record<string, unknown> = {...values};
    if (name.trim()) params.name = name.trim();
    let geometry = null;
    if (mode === "point") {
      if (!point) return "Click the map to set the centre of the search.";
      Object.assign(params, {lat: point[0], lon: point[1], radius});
    } else if (mode === "draw") {
      geometry = place.drawnAreas();
      if (!geometry) return "Draw a polygon or a rectangle with the tools on the left of the map.";
      params.buffer = buffer;
    } else if (mode === "file") {
      if (!file) return "Choose a GeoJSON, KML or GPX file.";
      geometry = file.areas;
      params.buffer = buffer;
    } else {
      if (preset === "osm") {
        if (!osmName.trim()) return "Give the name of an OpenStreetMap area.";
        params.area = "osm:" + osmName.trim();
      } else {
        if (!within.trim()) return "Coast and cliffs need a limit: an OSM name or the map view.";
        params.area = preset;
      }
      params.buffer = buffer;
      if (within.trim()) params.within = within.trim().startsWith("osm:") || /^[-\d.,\s]+$/.test(within)
        ? within.trim() : "osm:" + within.trim();
    }
    return {params, geometry};
  };

  const run = async (start: boolean) => {
    const r = request();
    if (typeof r === "string") { setError(r); return; }
    setBusy(true);
    setError("");
    try {
      if (start) {
        const {id} = await api.start(r);
        onStarted(id);
      } else setEstimate(await api.estimate(r));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const uploadFile = async (input: HTMLInputElement) => {
    const chosen = input.files?.[0];
    if (!chosen) return;
    try {
      const areas = await api.areaFile(chosen);
      setFile({name: chosen.name, areas});
      place.showFile(areas);
    } catch (e) {
      setError((e as Error).message);
      setFile(null);
      place.showFile(null);
    }
  };

  const set = (key: string, value: unknown) => {
    const next = {...values};
    if (value === byName[key].default) delete next[key]; else next[key] = value;
    setValues(next);
  };
  const bufferInput = <label class="fa-row">buffer, m <input type="number" min={0} defaultValue={String(buffer)}
                                                              onChange={(e) => setBuffer(Number(e.currentTarget.value))} />
  </label>;

  return <div class="fa-form">
    <h3>Where</h3>
    <div class="fa-modes">
      {([["point", "Point"], ["draw", "Draw"], ["file", "File"], ["preset", "Coast, cliffs, OSM"]] as [Mode, string][])
        .map(([m, text]) => <button class={mode === m ? "fa-on" : ""} onClick={() => setMode(m)}>{text}</button>)}
    </div>
    {mode === "point" && <div>
      <p class="fa-hint">{point ? `Centre ${point[0].toFixed(5)}, ${point[1].toFixed(5)}` : "Click the map."}</p>
      <label class="fa-row">radius, m <input type="number" min={10} defaultValue={String(radius)}
                                             onChange={(e) => setRadius(Number(e.currentTarget.value))} /></label>
    </div>}
    {mode === "draw" && <div>
      <p class="fa-hint">Draw polygons or rectangles with the tools on the left of the map; drag their corners to
        change them. Both anchors of a line lie inside, the webbing may leave the area.</p>
    </div>}
    {mode === "file" && <div>
      <input type="file" accept=".geojson,.json,.kml,.gpx" onChange={(e) => uploadFile(e.currentTarget)} />
      <p class="fa-hint">{file ? `${file.name}: ${file.areas.features.length} shapes` :
        "Polygons are used as they are, points and lines grown by the buffer."}</p>
      {bufferInput}
    </div>}
    {mode === "preset" && <div>
      <select value={preset} onChange={(e) => setPreset(e.currentTarget.value)}>
        <option value="cliffs">cliffs and crags (OSM)</option>
        <option value="coast">coast (OSM coastline)</option>
        <option value="osm">a named OSM area</option>
      </select>
      {preset === "osm" && <label class="fa-row">name <input type="text" value={osmName} placeholder="e.g. Gozo"
                                                             onInput={(e) => setOsmName(e.currentTarget.value)} /></label>}
      {preset !== "osm" && bufferInput}
      <label class="fa-row">within <input type="text" value={within} placeholder="OSM name or S,W,N,E"
                                          onInput={(e) => setWithin(e.currentTarget.value)} /></label>
      <button class="fa-small" onClick={() => setWithin(place.viewBox())}>use the map view</button>
    </div>}
    <label class="fa-row">name of the run <input type="text" value={name} placeholder="from OpenStreetMap"
                                                 onInput={(e) => setName(e.currentTarget.value)} /></label>

    {GROUP_ORDER.filter((g) => groups[g]).map((group) => <details open={group === "search"}>
      <summary>{groups[group]}</summary>
      {fields.filter((f) => f.group === group && !PLACE_FIELDS.has(f.name)).map((f) =>
        <label class="fa-row" title={f.help}>
          <span class={f.name in values ? "fa-changed" : ""}>{label(f.name)}</span>
          <Input field={f} value={values[f.name]} onChange={(v) => set(f.name, v)} />
        </label>)}
    </details>)}

    <div class="fa-actions">
      <button disabled={busy} onClick={() => run(false)}>Estimate</button>{" "}
      <button disabled={busy || !estimate} class="fa-primary" onClick={() => run(true)}>
        {estimate && estimate.reasons.length ? "Start anyway" : "Start"}</button>
      {busy && <span class="fa-hint"> working…</span>}
    </div>
    {error && <p class="fa-error">{error}</p>}
    {estimate && <div class="fa-estimate">
      <b>{estimate.source}</b>, {estimate.resolution} m grid{estimate.km2 !== null &&
        `, ${estimate.km2.toFixed(2)} km² in ${estimate.tiles} tile(s)`}, about {estimate.download_mb} MB
      to download, {estimate.work_gb} GB of working grids.
      {estimate.reasons.length > 0 && <ul class="fa-warn">{estimate.reasons.map((r) => <li>{r}</li>)}</ul>}
      <details><summary>Details</summary>
        <pre>{estimate.messages.map(([, text]) => text).join("\n")}</pre></details>
    </div>}
  </div>;
}
