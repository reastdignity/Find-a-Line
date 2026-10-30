import type { GeoJSON } from "geojson";
import type { Favourite, FavouriteFolder } from "../favourites";

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
  min: number | null;  // the site's limits (web.schema), null: none
  max: number | null;
  label: string;       // a short human label (params.LABELS)
  unit: string;        // "m", "%", "°", ... or ""
  topic: string | null;  // the topic of the new form (params.TOPICS)
  choice_labels: Record<string, string> | null;  // names of the choices (the sources)
}

/** A preset of the kind of lines (params.PRESETS, web.presets). */
export interface Preset {
  id: string;
  label: string;
  values: Record<string, number> | null;  // null: "custom", the values stay as they are
  available: boolean;                     // false: a value is outside the user's bounds
  reason: string | null;
}

/** /api/schema: the fields, the old form's groups, the new form's topics and the presets. */
export interface Schema {
  fields: Field[];
  groups: Record<string, string>;
  topics: Record<string, string>;
  presets: Preset[];
}

/** One quota of /api/usage: what is used (null: not counted) and the quota (null: none). */
export interface UsageItem { used: number | null; limit: number | null }
export interface Usage {
  limited: boolean;
  hours: number;
  usage: Record<"km2" | "download_mb" | "nominatim" | "jobs_day" | "jobs_active" | "runs_gb", UsageItem>;
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
  user_id?: number | null;  // the owner (an administrator sees every job)
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
  shared: boolean;   // has a share link (the public site)
  own: boolean;      // the user's own run (else a demo run of someone else)
  demo: boolean;     // a demo run: its map is open to everyone (the public site)
}

/** The admin tab (the public site): users, every run, the server. */
export interface AdminUser {
  id: number; provider: string; subject: string; name: string; status: "pending" | "approved" | "blocked";
  admin: boolean; unlimited: boolean; created: string | null; last_login: string | null; sessions: number;
}
export interface AdminRun {
  run_id: string; user_id: number | null; visibility: "private" | "demo"; bytes: number; created: string;
  shared: boolean; name: string | null; has_map: boolean;
}
/** A problem report as the admin page lists it (web.py /api/admin/reports). */
export interface AdminReport {
  id: number; user_id: number | null; user_name: string | null; created: string; category: string; text: string;
  status: "open" | "done"; run_name: string | null;
  context: {page?: string; ui?: string; run?: string; line?: string; viewport?: string; user_agent?: string};
}
export interface AdminStatus {
  workers: {id: string; pid: number; host: string; started: string}[];
  free_gb: number; free_gb_min: number | null; queue: number; queue_max: number | null;
}

export interface OsmRegion { id: string; name: string; depth: number; downloaded: boolean }
export interface OsmExtract { id: string; mb: number; data: string | null; checked: string | null }
export interface OsmState { regions: OsmRegion[]; extracts: OsmExtract[] }

/** A match of the place box (/api/place, osm.places): bbox [S, W, N, E] or null for a point. */
export interface Place { name: string; lat: number; lon: number; bbox: [number, number, number, number] | null; type: string }

export interface SearchRequest {
  params: Record<string, unknown>;
  geometry: GeoJSON | null;
}

const token = document.querySelector<HTMLMetaElement>("meta[name=findaline-token]")?.content || "";

/** The signed-in user of the public site (web.py, accounts.User.public), or {local: true}. */
export interface PageUser { local?: boolean; name?: string; provider?: string; status?: string; admin?: boolean }
export const user: PageUser = JSON.parse(
  document.querySelector<HTMLMetaElement>("meta[name=findaline-user]")?.content || "{\"local\": true}");

/** An error answer of the server, with its HTTP status (429: a rate, 503: no worker, ...). */
export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

async function call<T>(method: string, url: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const init: RequestInit = {method, headers: {"X-Findaline-Token": token}, signal};
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }
  const response = await fetch(url, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    throw new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail || response.statusText),
                       response.status);
  }
  return data as T;
}

export const api = {
  schema: () => call<Schema>("GET", "/api/schema"),
  usage: () => call<Usage>("GET", "/api/usage"),
  /** `signal` aborts the request: the server forgets its waiting task (a newer one replaces it). */
  estimate: (request: SearchRequest, signal?: AbortSignal) => call<Estimate>("POST", "/api/estimate", request, signal),
  places: (q: string) => call<{places: Place[]}>("POST", "/api/place", {q}),
  start: (request: SearchRequest) => call<{id: string}>("POST", "/api/jobs", request),
  jobs: () => call<{jobs: Job[]; seq: number; worker: boolean}>("GET", "/api/jobs"),
  jobEvents: (id: string) => call<{events: JobEvent[]}>("GET", `/api/jobs/${encodeURIComponent(id)}/events`),
  cancel: (id: string) => call<{ok: boolean}>("DELETE", `/api/jobs/${encodeURIComponent(id)}`),
  job: (id: string) => call<Job & {request: SearchRequest & {osm?: unknown}}>("GET", `/api/jobs/${encodeURIComponent(id)}`),
  again: (id: string) => call<{id: string}>("POST", `/api/jobs/${encodeURIComponent(id)}/again`),
  runs: () => call<{runs: RunListed[]; public: boolean}>("GET", "/api/runs"),
  share: (id: string) => call<{url: string}>("POST", `/api/runs/${encodeURIComponent(id)}/share`),
  unshare: (id: string) => call<{ok: boolean}>("DELETE", `/api/runs/${encodeURIComponent(id)}/share`),
  deleteRun: (id: string) => call<{ok: boolean}>("DELETE", `/api/runs/${encodeURIComponent(id)}`),
  report: (body: {category: string; text: string; context: object}) =>
    call<{id: number}>("POST", "/api/reports", body),
  adminReports: () => call<{reports: AdminReport[]}>("GET", "/api/admin/reports"),
  adminReportStatus: (id: number, status: "open" | "done") =>
    call<{ok: boolean}>("POST", `/api/admin/reports/${id}`, {status}),
  adminDeleteReport: (id: number) => call<{ok: boolean}>("DELETE", `/api/admin/reports/${id}`),
  favourites: () => call<{favourites: Favourite[]; folders: FavouriteFolder[]}>("GET", "/api/favourites"),
  moveFavourite: (run: string, line: string, folder: number | null) =>
    call<{ok: boolean}>("POST", `/api/runs/${encodeURIComponent(run)}/favourites/folder`, {line, folder}),
  addFolder: (name: string, parent: number | null) => call<{id: number}>("POST", "/api/favourites/folders", {name, parent}),
  changeFolder: (id: number, change: {name?: string; parent?: number | null}) =>
    call<{ok: boolean}>("POST", `/api/favourites/folders/${id}`, change),
  deleteFolder: (id: number) => call<{folders: number; favourites: number}>("DELETE", `/api/favourites/folders/${id}`),
  unfavourite: (run: string, line: string) =>
    call<{ok: boolean}>("POST", `/api/runs/${encodeURIComponent(run)}/favourites`, {line, favourite: false}),
  osm: (lat: number, lon: number) => call<OsmState>("GET", `/api/osm?lat=${lat}&lon=${lon}`),
  osmJob: (action: "download" | "update", region: string) =>
    call<{id: string}>("POST", "/api/osm", {action, region}),
  logout: () => call<unknown>("POST", "/logout"),
  adminStatus: () => call<AdminStatus>("GET", "/api/admin/status"),
  adminUsers: () => call<{users: AdminUser[]}>("GET", "/api/admin/users"),
  adminSetUser: (id: number, change: {status?: string; unlimited?: boolean}) =>
    call<{ok: boolean}>("POST", `/api/admin/users/${id}`, change),
  adminRevoke: (id: number) => call<{ok: boolean}>("POST", `/api/admin/users/${id}/revoke`),
  adminRuns: () => call<{runs: AdminRun[]}>("GET", "/api/admin/runs"),
  setDemo: (id: string, demo: boolean) => call<{ok: boolean}>("POST", `/api/runs/${encodeURIComponent(id)}/demo`, {demo}),  // answered with the sign-in page
};

/** Events of all jobs after `since` (Server-Sent Events, reconnected by the browser), and whether
 *  a worker runs (sent when the stream starts and when it changes). */
export function subscribe(since: number, onEvent: (event: JobEvent) => void,
                          onWorker: (alive: boolean) => void): () => void {
  const source = new EventSource(`/api/events?since=${since}`);
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.kind === "worker") onWorker(event.data.alive);
    else onEvent(event);
  };
  return () => source.close();
}
