import type { GeoJSON } from "geojson";

// Requests to the local server (web.py). Changing requests carry the session token
// of the page, which a foreign website cannot read or set.

export interface Field {
  name: string;
  type: "number" | "integer" | "boolean" | "text" | "choice";
  choices: string[] | null;
  default: unknown;
  help: string;
  group: string;
  optional: boolean;
}

export interface Estimate {
  messages: [string, string][];
  reasons: string[];
  download_mb: number;
  work_gb: number;
  source: string;
  tiles: number;
  resolution: number;
  km2: number | null;
  area: GeoJSON;     // the area that will be searched
}

export interface Job {
  id: string;
  created: string;
  status: "queued" | "downloading" | "ready" | "computing" | "done" | "failed" | "cancelled";
  title: string;
  run_id: string | null;
  error: string | null;
  finished: string | null;
}

export interface JobEvent {
  seq: number;
  job: string;
  kind: "status" | "info" | "warning" | "progress";
  data: any;
}

export interface RunListed {
  id: string;
  name: string;
  created: string;
  description: string;
  summary: {lines: number; spots: number};
  has_map: boolean;
}

export interface OsmRegion { id: string; name: string; depth: number; downloaded: boolean }
export interface OsmExtract { id: string; mb: number; data: string | null; checked: string | null }
export interface OsmState { regions: OsmRegion[]; extracts: OsmExtract[] }

export interface SearchRequest {
  params: Record<string, unknown>;
  geometry: GeoJSON | null;
}

const token = document.querySelector<HTMLMetaElement>("meta[name=findaline-token]")?.content || "";

async function call<T>(method: string, url: string, body?: unknown): Promise<T> {
  const init: RequestInit = {method, headers: {"X-Findaline-Token": token}};
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }
  const response = await fetch(url, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || response.statusText));
  }
  return data as T;
}

export const api = {
  schema: () => call<{fields: Field[]; groups: Record<string, string>}>("GET", "/api/schema"),
  estimate: (request: SearchRequest) => call<Estimate>("POST", "/api/estimate", request),
  start: (request: SearchRequest) => call<{id: string}>("POST", "/api/jobs", request),
  jobs: () => call<{jobs: Job[]; seq: number}>("GET", "/api/jobs"),
  jobEvents: (id: string) => call<{events: JobEvent[]}>("GET", `/api/jobs/${encodeURIComponent(id)}/events`),
  cancel: (id: string) => call<{ok: boolean}>("DELETE", `/api/jobs/${encodeURIComponent(id)}`),
  job: (id: string) => call<Job & {request: SearchRequest & {osm?: unknown}}>("GET", `/api/jobs/${encodeURIComponent(id)}`),
  again: (id: string) => call<{id: string}>("POST", `/api/jobs/${encodeURIComponent(id)}/again`),
  runs: () => call<{runs: RunListed[]}>("GET", "/api/runs"),
  osm: (lat: number, lon: number) => call<OsmState>("GET", `/api/osm?lat=${lat}&lon=${lon}`),
  osmJob: (action: "download" | "update", region: string) =>
    call<{id: string}>("POST", "/api/osm", {action, region}),
};

/** Events of all jobs after `since` (Server-Sent Events, reconnected by the browser). */
export function subscribe(since: number, onEvent: (event: JobEvent) => void): () => void {
  const source = new EventSource(`/api/events?since=${since}`);
  source.onmessage = (message) => onEvent(JSON.parse(message.data));
  return () => source.close();
}
