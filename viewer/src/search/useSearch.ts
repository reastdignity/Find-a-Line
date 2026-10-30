// The state of a new search in the new interfaces: where (four modes), the parameters with the
// presets, the live estimate and the start. useSearch() holds it, so a shell can show the panel
// (SearchPanel) and the estimate with the start (EstimateBar) in different places; useUsage() is
// the user's use of the quotas (/api/usage). The request is built by app/request.ts.

import type { FeatureCollection } from "geojson";
import { useCallback, useEffect, useMemo, useRef, useState } from "preact/hooks";
import { api, ApiError, type Estimate, type Field, type Schema, type SearchRequest, type Usage } from "../app/api";
import { countAreas } from "../app/areas";
import { readAreaFile } from "../app/files";
import type { Mode, PlaceMap } from "../app/PlaceMap";
import { buildRequest, fromRequest, valueProblem, withValue, type AreaFile, type Place } from "../app/request";

export type SearchMode = "point" | "area" | "cliffs" | "file";
export type AreaKind = "draw" | "osm";       // the Area mode: drawn on the map, or a named OSM area
export type CoastKind = "cliffs" | "coast";  // the Along cliffs mode

/** The key fields under the presets (the presets set the first three). */
export const KEY_FIELDS = ["min_length", "max_length", "max_anchor_diff_m", "source"];

export const DEBOUNCE_MS = 800;   // the estimate is asked for this long after the last change
export const RETRY_MS = 20_000;   // an estimate refused by the rate (429) is asked again once, after this

export type EstimateStatus =
  | "incomplete"   // the place is not complete yet (message: what is missing)
  | "waiting"      // a change; asked for after DEBOUNCE_MS
  | "loading"      // asked for
  | "ready"        // estimate
  | "paused"       // too many estimates (429): asked again once after RETRY_MS
  | "refused"      // the server refused the request (a value over the limits, a bad area): no start
  | "unavailable"; // no answer (no worker, busy, offline): the search can still be started

export interface EstimateState {
  status: EstimateStatus;
  estimate: Estimate | null;
  message: string;
  retrying: boolean;  // paused, and the one retry is still to come
  retry(): void;      // ask again now (a button)
}

/** The estimate of a request, asked for DEBOUNCE_MS after its last change; an answer to an older
 *  request is dropped (and its fetch aborted, so the server forgets its task). The area it will
 *  search is drawn on `place` (the preview) while the estimate is current. */
export function useEstimate(request: SearchRequest | string, place: PlaceMap | null): EstimateState {
  const key = typeof request === "string" ? "" : JSON.stringify(request);
  const [state, setState] = useState<Omit<EstimateState, "retry">>(
    {status: "incomplete", estimate: null, message: "", retrying: false});
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const current = useRef(request);
  current.current = request;

  const load = useCallback((asked: number, retried: boolean) => {
    const r = current.current;
    if (asked !== generation.current || typeof r === "string") return;
    controller.current?.abort();
    const mine = new AbortController();
    controller.current = mine;
    setState({status: "loading", estimate: null, message: "", retrying: false});
    api.estimate(r, mine.signal).then((estimate) => {
      if (asked !== generation.current) return;
      setState({status: "ready", estimate, message: "", retrying: false});
      place?.showPreview(estimate.area);
    }, (e: Error) => {
      if (asked !== generation.current || mine.signal.aborted) return;
      const code = e instanceof ApiError ? e.status : 0;
      if (code === 429) {
        setState({status: "paused", estimate: null, message: e.message, retrying: !retried});
        if (!retried) timer.current = setTimeout(() => load(asked, true), RETRY_MS);
      } else if (code >= 400 && code < 500) {
        setState({status: "refused", estimate: null, message: e.message, retrying: false});
      } else {
        setState({status: "unavailable", estimate: null, message: e.message, retrying: false});
      }
    });
  }, [place]);

  useEffect(() => {
    const asked = ++generation.current;
    controller.current?.abort();
    controller.current = null;
    place?.showPreview(null);
    if (!key) {
      setState({status: "incomplete", estimate: null, message: "", retrying: false});
      return;
    }
    setState({status: "waiting", estimate: null, message: "", retrying: false});
    timer.current = setTimeout(() => load(asked, false), DEBOUNCE_MS);
    return () => clearTimeout(timer.current);
  }, [key, place]);

  useEffect(() => () => { generation.current++; controller.current?.abort(); }, []);

  const retry = () => {
    clearTimeout(timer.current);
    load(generation.current, true);
  };
  return {...state, message: typeof request === "string" ? request : state.message, retry};
}

export interface UsageState {
  usage: Usage | null;
  error: string;
  refresh(): void;
}

/** The user's use of the quotas and the quotas (/api/usage), asked for once and on refresh(). */
export function useUsage(): UsageState {
  const [usage, setUsage] = useState<Usage | null>(null);
  const [error, setError] = useState("");
  const refresh = useCallback(() => {
    api.usage().then((u) => { setUsage(u); setError(""); }, (e) => setError(e.message));
  }, []);
  useEffect(refresh, [refresh]);
  return {usage, error, refresh};
}

/** Whether an estimate fits the user's daily quotas (km², downloads): null without quotas or estimate.
 *  `area`: the km² of the search (the estimate's, or a circle's), null when not known before it runs. */
export function withinLimits(usage: Usage | null, estimate: Estimate | null,
                             area: number | null = estimate?.km2 ?? null): {ok: boolean; text: string} | null {
  if (!usage || !estimate || !usage.limited) return null;
  const {km2, download_mb} = usage.usage;
  if (km2.limit !== null && km2.used !== null && area !== null) {
    if (km2.used + area > km2.limit) {
      return {ok: false, text: `Over your daily limit: ${fmt(km2.used)} of ${fmt(km2.limit)} km² used, ` +
                               `this search needs ${fmt(area)}`};
    }
  }
  if (download_mb.limit !== null && download_mb.used !== null &&
      download_mb.used + estimate.download_mb > download_mb.limit) {
    return {ok: false, text: `Over your daily download limit: ${fmt(download_mb.used)} of ` +
                             `${fmt(download_mb.limit)} MB used`};
  }
  if (km2.limit !== null && km2.used !== null && area !== null) {
    return {ok: true, text: `Within your daily limit (${fmt(km2.used + area)} of ${fmt(km2.limit)} km²)`};
  }
  return {ok: true, text: "Within your daily limits"};
}

/** A number for the panel: two significant decimals below 10, one below 100, none above. */
export function fmt(value: number): string {
  const digits = Math.abs(value) < 10 ? 2 : Math.abs(value) < 100 ? 1 : 0;
  return String(+value.toFixed(digits));
}

export interface SearchOptions {
  place: PlaceMap;
  schema: Schema;
  onStarted(jobId: string): void;
  initial?: SearchRequest | null;  // a search to edit and run again
  preset?: string;                 // the preset a new search starts with (not one to edit); none: the defaults
}

export interface SearchState {
  place: PlaceMap;
  schema: Schema;
  byName: Record<string, Field>;
  mode: SearchMode;
  setMode(mode: SearchMode): void;
  areaKind: AreaKind;
  setAreaKind(kind: AreaKind): void;
  coastKind: CoastKind;
  setCoastKind(kind: CoastKind): void;
  point: [number, number] | null;
  radius: number;
  setRadius(radius: number): void;
  buffer: number;
  setBuffer(buffer: number): void;
  osmName: string;
  setOsmName(name: string): void;
  within: string;
  setWithin(within: string): void;
  name: string;
  setName(name: string): void;
  file: AreaFile | null;
  fileKey: number;                     // a new (empty) file input after clearing
  readFile(input: HTMLInputElement): Promise<void>;
  shapes: number;                      // drawn shapes or shapes of the file
  areas: number;                       // separate areas of them (overlapping or touching shapes are one)
  values: Record<string, unknown>;     // the parameters changed from their defaults
  value(name: string): unknown;        // a parameter's value (its default when unchanged)
  setValue(name: string, value: unknown): void;
  setValues(values: Record<string, unknown>): void;
  problems: Record<string, string>;    // values outside the site's bounds, by field
  preset: string;                      // the preset the values match ("custom" if none)
  applyPreset(id: string): void;
  request: SearchRequest | string;     // the request, or what is missing
  estimate: EstimateState;
  km2: number | null;                  // the area of the search: the estimate's, or a circle's (π r²)
  canStart: boolean;
  busy: boolean;
  error: string;                       // of the start
  start(): Promise<void>;
  clear(): void;                       // the place (point, drawn areas, file, names); the parameters stay
}

/** The place mode of the map (PlaceMap) for a mode of the panel. */
function mapMode(mode: SearchMode, areaKind: AreaKind): Mode {
  return mode === "point" ? "point" : mode === "file" ? "file" : mode === "area" && areaKind === "draw" ? "draw"
    : "preset";
}

/** A new search: the state of the panel, its request, the live estimate and the start. */
export function useSearch({place, schema, onStarted, initial, preset: preset0}: SearchOptions): SearchState {
  const byName = useMemo(() => Object.fromEntries(schema.fields.map((f) => [f.name, f])), [schema]);
  const start0 = useMemo(() => {
    const found = fromRequest(initial, byName);
    const chosen = !initial && schema.presets.find((p) => p.id === preset0);
    if (chosen && chosen.available && chosen.values) {
      for (const [key, v] of Object.entries(chosen.values)) {
        if (byName[key]) found.values = withValue(found.values, byName[key], v);
      }
    }
    return found;
  }, []);
  const [mode, setMode] = useState<SearchMode>(start0.mode === "point" ? "point" : start0.mode === "file" ? "file"
    : start0.mode === "draw" || start0.preset === "osm" ? "area" : "cliffs");
  const [areaKind, setAreaKind] = useState<AreaKind>(start0.mode === "preset" && start0.preset === "osm" ? "osm" : "draw");
  const [coastKind, setCoastKind] = useState<CoastKind>(start0.preset === "coast" ? "coast" : "cliffs");
  const [point, setPoint] = useState(start0.point);
  const [radius, setRadius] = useState(start0.radius);
  const [buffer, setBuffer] = useState(start0.buffer);
  const [osmName, setOsmName] = useState(start0.osmName);
  const [within, setWithin] = useState(start0.within);
  const [name, setName] = useState(start0.name);
  const [file, setFile] = useState<AreaFile | null>(null);
  const [fileKey, setFileKey] = useState(0);
  const [drawn, setDrawn] = useState(0);  // changes when the drawn areas change
  const [values, setValues] = useState<Record<string, unknown>>(start0.values);
  const [custom, setCustom] = useState(false);  // "Custom" chosen while the values match a preset
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    place.onPoint = (lat, lon) => setPoint([lat, lon]);
    place.onDrawn = () => setDrawn((n) => n + 1);
  }, [place]);
  useEffect(() => place.setMode(mapMode(mode, areaKind)), [place, mode, areaKind]);
  useEffect(() => {  // a search to edit: its drawn or uploaded area becomes editable on the map
    if (initial?.geometry) place.loadDrawn(initial.geometry);
    if (start0.point) place.map.setView(start0.point, Math.max(place.map.getZoom(), 14));
  }, [place, initial]);
  useEffect(() => { if (point) place.setPoint(point[0], point[1], radius); }, [place, point, radius]);
  useEffect(() => setError(""), [mode, areaKind, coastKind, point, radius, buffer, osmName, within, file, drawn, values]);

  const drawnAreas = useMemo((): FeatureCollection | null => place.drawnAreas(), [place, drawn]);
  const collection = mode === "area" && areaKind === "draw" ? drawnAreas : mode === "file" ? file?.areas ?? null : null;
  const areas = useMemo(() => countAreas(collection, buffer), [collection, buffer]);
  const shapes = collection?.features.length || 0;

  const value = (key: string) => key in values ? values[key] : byName[key]?.default;
  const problems = useMemo(() => {
    const found: Record<string, string> = {};
    for (const [key, v] of Object.entries(values)) {
      const problem = byName[key] ? valueProblem(byName[key], v) : "";
      if (problem) found[key] = problem;
    }
    const low = Number(value("min_length")), high = Number(value("max_length"));
    if (!(low > 0 && low < high)) found.min_length = "more than 0 and less than the longest line";
    return found;
  }, [values, byName]);

  const matching = schema.presets.find((p) => p.values && Object.entries(p.values).every(([k, v]) => value(k) === v));
  const preset = custom || !matching ? (schema.presets.some((p) => p.id === "custom") ? "custom" : "") : matching.id;
  const applyPreset = (id: string) => {
    const chosen = schema.presets.find((p) => p.id === id);
    if (!chosen || !chosen.available) return;
    if (!chosen.values) { setCustom(true); return; }
    setCustom(false);
    let next = values;
    for (const [key, v] of Object.entries(chosen.values)) if (byName[key]) next = withValue(next, byName[key], v);
    setValues(next);
  };

  const request = useMemo((): SearchRequest | string => {
    const placed: Place = {mode: mapMode(mode, areaKind), point, radius, buffer,
                           preset: mode === "area" ? "osm" : coastKind, osmName,
                           within: mode === "cliffs" ? within : "", name};
    const built = buildRequest(placed, values, drawnAreas, file);
    if (typeof built === "string") return built;
    const wrong = Object.entries(problems);
    if (wrong.length) {
      return "Check the settings: " + wrong.map(([k, p]) => `${byName[k]?.label ?? k}: ${p}`).join("; ") + ".";
    }
    return built;
  }, [mode, areaKind, coastKind, point, radius, buffer, osmName, within, name, values, drawnAreas, file, problems]);

  const estimate = useEstimate(request, place);
  const canStart = typeof request !== "string" && estimate.status !== "refused" && !busy;
  // The worker charges a circle as π r² (jobs.charge_area); the estimate gives km² for areas only
  const km2 = estimate.estimate?.km2 ?? (mode === "point" && point ? Math.PI * radius * radius / 1e6 : null);

  const clear = () => {
    place.clear();
    setPoint(null);
    setFile(null);
    setFileKey((k) => k + 1);
    setOsmName("");
    setWithin("");
    setName("");
  };

  const start = async () => {
    if (typeof request === "string" || busy) return;
    setBusy(true);
    setError("");
    try {
      const {id} = await api.start(request);
      onStarted(id);
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
      const read = await readAreaFile(chosen);
      setFile({name: chosen.name, areas: read});
      place.showFile(read);
    } catch (e) {
      setError((e as Error).message);
      setFile(null);
      place.showFile(null);
    }
  };

  return {
    place, schema, byName, mode, setMode, areaKind, setAreaKind, coastKind, setCoastKind, point, radius, setRadius,
    buffer, setBuffer, osmName, setOsmName, within, setWithin, name, setName, file, fileKey, readFile, shapes, areas,
    values, value, setValue: (key, v) => { if (byName[key]) setValues((now) => withValue(now, byName[key], v)); },
    setValues, problems, preset, applyPreset, request, estimate, km2, canStart, busy, error, start, clear,
  };
}
