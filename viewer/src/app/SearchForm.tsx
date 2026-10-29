// A new search: where (a point, drawn areas, a file, or coast/cliffs/an OSM area),
// the parameters (generated from the schema of params.py), the estimate, the start.

import type { FeatureCollection } from "geojson";
import { useEffect, useMemo, useState } from "preact/hooks";
import { api, type Estimate, type Field, type SearchRequest } from "./api";
import { countAreas } from "./areas";
import { readAreaFile } from "./files";
import type { Mode, PlaceMap } from "./PlaceMap";
import { buildRequest, fromRequest, parseValue, PLACE_FIELDS, withValue } from "./request";

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
  const bounds = {min: field.min ?? undefined, max: field.max ?? undefined};  // the site's limits
  const placeholder = field.optional ? "default" : "";
  const changed = (e: Event) => onChange(parseValue(field, (e.currentTarget as HTMLInputElement).value));
  // The page is drawn again on every event of the queue: a number field keeps what is
  // being typed (defaultValue) and is read when it is left; a text field follows every key
  return numeric
    ? <input type="number" step={field.type === "integer" ? 1 : "any"} defaultValue={text} {...bounds}
             placeholder={placeholder} onChange={changed} />
    : <input type="text" value={text} placeholder={placeholder} onInput={changed} />;
}

export interface SearchFormProps {
  place: PlaceMap;
  fields: Field[];
  groups: Record<string, string>;
  initial?: SearchRequest | null;  // a job to edit ("edit" in the queue)
  onStarted(id: string): void;
}

export function SearchForm({place, fields, groups, initial, onStarted}: SearchFormProps) {
  const byName = Object.fromEntries(fields.map((f) => [f.name, f]));
  const start = fromRequest(initial, byName);
  const [mode, setMode] = useState<Mode>(start.mode);
  const [point, setPoint] = useState<[number, number] | null>(start.point);
  const [radius, setRadius] = useState(start.radius);
  const [buffer, setBuffer] = useState(start.buffer);
  const [preset, setPreset] = useState(start.preset);
  const [osmName, setOsmName] = useState(start.osmName);
  const [within, setWithin] = useState(start.within);
  const [name, setName] = useState(start.name);
  const [file, setFile] = useState<{name: string; areas: FeatureCollection} | null>(null);
  const [fileKey, setFileKey] = useState(0);  // a new file input (empty) after clearing
  const [drawn, setDrawn] = useState(0);  // changes when the drawn areas change
  const [values, setValues] = useState<Record<string, unknown>>(start.values);
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    place.onPoint = (lat, lon) => setPoint([lat, lon]);
    place.onDrawn = () => setDrawn((n) => n + 1);
  }, [place]);
  useEffect(() => place.setMode(mode), [place, mode]);
  useEffect(() => {  // a job to edit: its drawn or uploaded area becomes editable on the map
    if (initial?.geometry) place.loadDrawn(initial.geometry);
    if (start.point) place.map.setView(start.point, Math.max(place.map.getZoom(), 14));
  }, [place, initial]);
  useEffect(() => { if (point) place.setPoint(point[0], point[1], radius); }, [place, point, radius]);
  // Any change makes the estimate stale
  useEffect(() => { setEstimate(null); setError(""); place.showPreview(null); },
            [mode, point, radius, buffer, preset, osmName, within, file, drawn, values]);

  // Separate areas (shapes that overlap or touch are one), drawn or from the file
  const areas = useMemo(() => mode === "draw" ? countAreas(place.drawnAreas(), buffer)
    : mode === "file" && file ? countAreas(file.areas, buffer) : 0, [place, mode, drawn, file, buffer]);
  const shapes = mode === "draw" ? place.drawnAreas()?.features.length || 0 : file?.areas.features.length || 0;

  /** Where the search goes, cleared (after a start, or "Clear all"); the parameters stay. */
  const clearPlace = () => {
    place.clear();
    setPoint(null);
    setFile(null);
    setFileKey((k) => k + 1);
    setOsmName("");
    setWithin("");
    setName("");
  };

  const request = (): SearchRequest | string =>
    buildRequest({mode, point, radius, buffer, preset, osmName, within, name}, values, place.drawnAreas(), file);

  const run = async (start: boolean) => {
    const r = request();
    if (typeof r === "string") { setError(r); return; }
    setBusy(true);
    setError("");
    try {
      if (start) {
        const {id} = await api.start(r);
        clearPlace();
        onStarted(id);
      } else {
        const result = await api.estimate(r);
        setEstimate(result);
        place.showPreview(result.area);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const readFile = async (input: HTMLInputElement) => {
    const chosen = input.files?.[0];
    if (!chosen) return;
    try {
      const areas = await readAreaFile(chosen);
      setFile({name: chosen.name, areas});
      place.showFile(areas);
    } catch (e) {
      setError((e as Error).message);
      setFile(null);
      place.showFile(null);
    }
  };

  const set = (key: string, value: unknown) => setValues(withValue(values, byName[key], value));
  const bufferInput = <label class="fa-row" title={byName.buffer?.help}>buffer, m <input
    type="number" min={0} max={byName.buffer?.max ?? undefined} defaultValue={String(buffer)}
    onChange={(e) => setBuffer(Number(e.currentTarget.value))} />
  </label>;

  return <div class="fa-form">
    <h3>Where</h3>
    <div class="fa-modes">
      {([["point", "Point"], ["draw", "Draw"], ["file", "File"], ["preset", "Coast, cliffs, OSM"]] as [Mode, string][])
        .map(([m, text]) => <button class={mode === m ? "fa-on" : ""} onClick={() => setMode(m)}>{text}</button>)}
      <button class="fa-small fa-clear" title="Remove the point, the drawn areas and the file"
              onClick={clearPlace}>Clear all</button>
    </div>
    {(mode === "draw" || mode === "file") && shapes > 0 && <p class="fa-areas">
      {areas === 1 ? "1 area" : `${areas} separate areas`}
      {shapes !== areas && ` (${shapes} shapes; those that overlap or touch are one area)`}</p>}
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
      <input key={fileKey} type="file" accept=".geojson,.json,.kml,.gpx" onChange={(e) => readFile(e.currentTarget)} />
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
      {estimate.messages.some(([kind]) => kind === "warning") && <ul class="fa-warn">
        {estimate.messages.filter(([kind]) => kind === "warning").map(([, text]) => <li>{text}</li>)}</ul>}
      <p class="fa-hint">The area to be searched is drawn on the map (orange, dashed).</p>
      <details><summary>Details</summary>
        <pre>{estimate.messages.map(([, text]) => text).join("\n")}</pre></details>
    </div>}
  </div>;
}
