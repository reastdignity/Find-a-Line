import type { GeoJSON } from "geojson";
import type { Favourite } from "../favourites";

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
export interface AdminStatus {
  workers: {id: string; pid: number; host: string; started: string}[];
  free_gb: number; free_gb_min: number | null; queue: number; queue_max: number | null;
}

export interface OsmRegion { id: string; name: string; depth: number; downloaded: boolean }
export interface OsmExtract { id: string; mb: number; data: string | null; checked: string | null }
export interface OsmState { regions: OsmRegion[]; extracts: OsmExtract[] }

export interface SearchRequest {
  params: Record<string, unknown>;
  geometry: GeoJSON | null;
}

const token = document.querySelector<HTMLMetaElement>("meta[name=findaline-token]")?.content || "";

/** The signed-in user of the public site (web.py, accounts.User.public), or {local: true}. */
export interface PageUser { local?: boolean; name?: string; provider?: string; status?: string; admin?: boolean }
export const user: PageUser = JSON.parse(
  document.querySelector<HTMLMetaElement>("meta[name=findaline-user]")?.content || "{\"local\": true}");

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
  jobs: () => call<{jobs: Job[]; seq: number; worker: boolean}>("GET", "/api/jobs"),
  jobEvents: (id: string) => call<{events: JobEvent[]}>("GET", `/api/jobs/${encodeURIComponent(id)}/events`),
  cancel: (id: string) => call<{ok: boolean}>("DELETE", `/api/jobs/${encodeURIComponent(id)}`),
  job: (id: string) => call<Job & {request: SearchRequest & {osm?: unknown}}>("GET", `/api/jobs/${encodeURIComponent(id)}`),
  again: (id: string) => call<{id: string}>("POST", `/api/jobs/${encodeURIComponent(id)}/again`),
  runs: () => call<{runs: RunListed[]; public: boolean}>("GET", "/api/runs"),
  share: (id: string) => call<{url: string}>("POST", `/api/runs/${encodeURIComponent(id)}/share`),
  unshare: (id: string) => call<{ok: boolean}>("DELETE", `/api/runs/${encodeURIComponent(id)}/share`),
  deleteRun: (id: string) => call<{ok: boolean}>("DELETE", `/api/runs/${encodeURIComponent(id)}`),
  favourites: () => call<{favourites: Favourite[]}>("GET", "/api/favourites"),
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
