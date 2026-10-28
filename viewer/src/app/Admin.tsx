// The admin tab of the public site: the server (workers, free disk, queue), the users (approve,
// block, without limits, end their sessions), every job with its owner (cancel), every run (the
// demo flag) and the OpenStreetMap extracts. Every request is an administrator's only (web.py).

import { useEffect, useState } from "preact/hooks";
import { api, type AdminRun, type AdminStatus, type AdminUser, type Job } from "./api";
import { OsmPanel } from "./OsmPanel";
import type { PlaceMap } from "./PlaceMap";

const FINISHED = ["done", "failed", "cancelled"];
const JOBS_SHOWN = 50;

function Users({users, onChange, onError}: {users: AdminUser[]; onChange(): void; onError(e: Error): void}) {
  const change = (p: Promise<unknown>) => p.then(onChange, onError);
  return <div>{users.map((u) => <div class="fa-run" key={u.id}>
    <div><b>{u.name}</b> <span class="fa-hint">{u.provider}:{u.subject}</span>
      <span class={"fa-badge fa-user-" + u.status}>{u.admin ? "administrator" : u.status}</span>
      {u.unlimited && !u.admin && <span class="fa-badge">without limits</span>}</div>
    <div class="fa-hint">since {(u.created || "?").slice(0, 10)}, last sign-in {(u.last_login || "never").slice(0, 16)
      .replace("T", " ")}, {u.sessions} session(s)</div>
    {!u.admin && <div class="fa-admin-tools">
      {u.status !== "approved" && <button class="fa-small"
        onClick={() => change(api.adminSetUser(u.id, {status: "approved"}))}>
        {u.status === "blocked" ? "unblock" : "approve"}</button>}
      {u.status !== "blocked" && <button class="fa-small" title="ends their sessions; their share links stop working"
        onClick={() => change(api.adminSetUser(u.id, {status: "blocked"}))}>block</button>}
      <label class="fa-hint"><input type="checkbox" checked={u.unlimited}
        onChange={(e) => change(api.adminSetUser(u.id, {unlimited: e.currentTarget.checked}))} /> without limits</label>
      {u.sessions > 0 && <button class="fa-small" title="they have to sign in again"
        onClick={() => change(api.adminRevoke(u.id))}>end sessions</button>}
    </div>}
  </div>)}</div>;
}

function Runs({runs, names, onChange, onError}: {runs: AdminRun[]; names: Map<number, string>; onChange(): void;
                                                onError(e: Error): void}) {
  if (!runs.length) return <p class="fa-hint">No runs.</p>;
  return <div>{runs.map((r) => <div class="fa-run" key={r.run_id}>
    <div><b>{r.name || r.run_id}</b> <span class="fa-hint">{names.get(r.user_id ?? -1) || "nobody"},
      {" "}{(r.bytes / 1e6).toFixed(1)} MB{r.shared ? ", shared by a link" : ""}</span></div>
    <div class="fa-admin-tools">
      {r.has_map && <a href={`/runs/${encodeURIComponent(r.run_id)}/map.html`} target="_blank">open map</a>}
      <label class="fa-hint"><input type="checkbox" checked={r.visibility === "demo"}
        onChange={(e) => api.setDemo(r.run_id, e.currentTarget.checked).then(onChange, onError)} /> demo (open to
        everyone)</label>
    </div>
  </div>)}</div>;
}

export function AdminPanel({place, jobs, onStarted}: {place: PlaceMap; jobs: Job[]; onStarted(): void}) {
  const [status, setStatus] = useState<AdminStatus | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [runs, setRuns] = useState<AdminRun[]>([]);
  const [error, setError] = useState("");
  const onError = (e: Error) => setError(e.message);
  const load = () => {
    setError("");
    api.adminStatus().then(setStatus, onError);
    api.adminUsers().then((r) => setUsers(r.users), onError);
    api.adminRuns().then((r) => setRuns(r.runs), onError);
  };
  useEffect(load, []);
  const names = new Map(users.map((u) => [u.id, u.name]));
  const pending = users.filter((u) => u.status === "pending").length;
  return <div class="fa-form fa-admin">
    {error && <p class="fa-error">{error}</p>}
    <h3>Server <button class="fa-small" onClick={load}>refresh</button></h3>
    {status && <div class="fa-hint">
      <div>{status.workers.length ? status.workers.map((w) => <div key={w.id}>worker {w.id} since
        {" "}{w.started.slice(0, 16).replace("T", " ")}</div>) : <span class="fa-error">no worker is running</span>}</div>
      <div>{status.free_gb} GB free{status.free_gb_min !== null ? ` (no new searches below ${status.free_gb_min} GB)` : ""}
        , {status.queue} search(es) waiting or running{status.queue_max !== null ? ` (at most ${status.queue_max})` : ""}</div>
    </div>}
    <h3>Users{pending ? ` (${pending} waiting)` : ""}</h3>
    <Users users={users} onChange={load} onError={onError} />
    <h3>All searches</h3>
    {jobs.slice(0, JOBS_SHOWN).map((j) => <div class="fa-run" key={j.id}>
      <b>{j.title}</b> <span class="fa-hint">{j.user_id === undefined ? "?" : names.get(j.user_id ?? -1) || "nobody"}, {j.status},
        {" "}{j.created.slice(0, 16).replace("T", " ")}</span>{" "}
      {!FINISHED.includes(j.status) && <button class="fa-small" onClick={() => api.cancel(j.id).catch(onError)}>
        cancel</button>}
    </div>)}
    <h3>Runs</h3>
    <Runs runs={runs} names={names} onChange={load} onError={onError} />
    <h3>OpenStreetMap extracts</h3>
    <OsmPanel place={place} onStarted={onStarted} />
  </div>;
}
