// The admin page of the public site (C and D; B shows it in its panel): the
// server (workers, free disk, queue), the users (approve, block, without limits, end their sessions),
// every search with its owner (cancel), every run (open, the demo flag), the users' problem reports (done,
// delete) and a link to the OpenStreetMap extracts (their own page: they need a map). Every request is an
// administrator's only (web.py).

import { Fragment } from "preact";
import { useEffect, useState } from "preact/hooks";
import { Button, Panel, Stat, Stats, StatusChip, type Status } from "../../ui/components";
import { api, type AdminReport, type AdminRun, type AdminStatus, type AdminUser, type Job } from "../api";
import { runHash } from "./route";
import { day, when } from "../../time";

const JOBS_SHOWN = 50;
const at = (iso: string | null | undefined, empty = "?") => when(iso, empty);

/** A job's status as a chip. */
function jobStatus(job: Job): [Status, string] {
  if (job.status === "done") return ["done", "done"];
  if (job.status === "failed" || job.status === "cancelled") return ["failed", job.status];
  if (job.status === "queued" || job.status === "ready") return ["waiting", job.status];
  return ["running", job.status];
}

function Users({users, onChange, onError}: {users: AdminUser[]; onChange(): void; onError(e: Error): void}) {
  const change = (p: Promise<unknown>) => p.then(onChange, onError);
  if (!users.length) return <p class="u-small">No users.</p>;
  return <ul class="c-admin-list">{users.map((u) => <li key={u.id} class="c-admin-row">
    <div class="c-admin-head">
      <b>{u.name}</b><span class="u-small u-mono">{u.provider}:{u.subject}</span>
      <span class={`u-badge c-user-${u.admin ? "admin" : u.status}`}>{u.admin ? "administrator" : u.status}</span>
      {u.unlimited && !u.admin && <span class="u-badge u-n">without limits</span>}
    </div>
    <div class="u-small">since {day(u.created, "?")} · last sign-in {at(u.last_login, "never")}
      {" · "}{u.sessions} {u.sessions === 1 ? "session" : "sessions"}</div>
    {!u.admin && <div class="c-admin-tools">
      {u.status !== "approved" && <Button kind="primary" onClick={() => change(api.adminSetUser(u.id, {status: "approved"}))}>
        {u.status === "blocked" ? "Unblock" : "Approve"}</Button>}
      {u.status !== "blocked" && <Button kind="danger" title="ends their sessions; their share links stop working"
        onClick={() => change(api.adminSetUser(u.id, {status: "blocked"}))}>Block</Button>}
      <label class="c-check"><input type="checkbox" checked={u.unlimited}
        onChange={(e) => change(api.adminSetUser(u.id, {unlimited: e.currentTarget.checked}))} />Without limits</label>
      {u.sessions > 0 && <Button kind="ghost" title="they have to sign in again"
        onClick={() => change(api.adminRevoke(u.id))}>End sessions</Button>}
    </div>}
  </li>)}</ul>;
}

function Runs({runs, names, onChange, onError}: {runs: AdminRun[]; names: Map<number, string>; onChange(): void;
                                                onError(e: Error): void}) {
  if (!runs.length) return <p class="u-small">No runs.</p>;
  return <ul class="c-admin-list">{runs.map((r) => <li key={r.run_id} class="c-admin-row">
    <div class="c-admin-head">
      <b>{r.name || r.run_id}</b>
      {r.shared && <StatusChip status="shared">shared by a link</StatusChip>}
      {r.visibility === "demo" && <span class="u-badge">demo</span>}
    </div>
    <div class="u-small">{names.get(r.user_id ?? -1) || "nobody"} · {(r.bytes / 1e6).toFixed(1)} MB · {at(r.created)}</div>
    <div class="c-admin-tools">
      {r.has_map && <Button kind="ghost" icon="next" href={runHash(r.run_id)}>Open</Button>}
      <label class="c-check"><input type="checkbox" checked={r.visibility === "demo"}
        onChange={(e) => api.setDemo(r.run_id, e.currentTarget.checked).then(onChange, onError)} />Demo (open to
        everyone)</label>
    </div>
  </li>)}</ul>;
}

const KINDS: Record<string, string> = {results: "results", water: "water level", interface: "interface", other: "other"};

function Reports({reports, onChange, onError}: {reports: AdminReport[]; onChange(): void; onError(e: Error): void}) {
  const [asking, setAsking] = useState<number | null>(null);
  const change = (p: Promise<unknown>) => p.then(onChange, onError);
  if (!reports.length) return <p class="u-small">No reports.</p>;
  return <ul class="c-admin-list">{reports.map((r) => {
    const c = r.context;
    return <li key={r.id} class={"c-admin-row c-report" + (r.status === "done" ? " c-report-done" : "")} data-report={r.id}>
      <div class="c-admin-head">
        <span class="u-badge">{KINDS[r.category] || r.category}</span>
        <StatusChip status={r.status === "done" ? "done" : "waiting"}>{r.status}</StatusChip>
        <b>{r.user_name || "?"}</b><span class="u-small">{at(r.created)}</span>
      </div>
      <p class="c-report-text">{r.text}</p>
      <div class="u-small c-report-context">
        {[c.run && (r.run_name ? <a href={runHash(c.run, c.line || null)}>{r.run_name}{c.line ? ", the line" : ""}</a>
            : <span>run {c.run} (deleted)</span>),
          c.page && <span>page {c.page}</span>, c.ui && <span>interface {c.ui}</span>,
          c.viewport && <span>window {c.viewport}</span>]
          .filter(Boolean).map((part, i) => <Fragment key={i}>{i > 0 && " · "}{part}</Fragment>)}
        {c.user_agent && <div class="u-mono c-report-agent">{c.user_agent}</div>}
      </div>
      <div class="c-admin-tools">
        <Button kind={r.status === "open" ? "primary" : "default"}
                onClick={() => change(api.adminReportStatus(r.id, r.status === "open" ? "done" : "open"))}>
          {r.status === "open" ? "Done" : "Reopen"}</Button>
        {asking === r.id
          ? <><Button kind="danger" class="c-report-delete" onClick={() => { setAsking(null); change(api.adminDeleteReport(r.id)); }}>
              Delete it</Button><Button kind="ghost" onClick={() => setAsking(null)}>Keep</Button></>
          : <Button kind="ghost" onClick={() => setAsking(r.id)}>Delete…</Button>}
      </div>
    </li>;
  })}</ul>;
}

export function AdminC({jobs}: {jobs: Job[]}) {
  const [status, setStatus] = useState<AdminStatus | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [runs, setRuns] = useState<AdminRun[]>([]);
  const [reports, setReports] = useState<AdminReport[]>([]);
  const [error, setError] = useState("");
  const onError = (e: Error) => setError(e.message);
  const load = () => {
    setError("");
    api.adminStatus().then(setStatus, onError);
    api.adminUsers().then((r) => setUsers(r.users), onError);
    api.adminRuns().then((r) => setRuns(r.runs), onError);
    api.adminReports().then((r) => setReports(r.reports), onError);
  };
  useEffect(load, []);
  const names = new Map(users.map((u) => [u.id, u.name]));
  const pending = users.filter((u) => u.status === "pending").length;
  return <div class="c-page c-admin">
    <h1 class="u-h">Admin</h1>
    {error && <p class="c-error" role="alert">{error}</p>}
    <Panel title="Server" actions={<Button kind="ghost" icon="refresh" onClick={load}>Refresh</Button>}>
      {status && <>
        <Stats>
          <Stat value={status.workers.length} label={status.workers.length === 1 ? "worker" : "workers"} />
          <Stat value={status.free_gb} unit="GB" label="free disk" />
          <Stat value={status.queue} unit={status.queue_max !== null ? `/ ${status.queue_max}` : undefined}
                label="in the queue" />
        </Stats>
        {(status.free_gb_min !== null || status.queue_max !== null) && <p class="u-small c-admin-limits">
          {[status.free_gb_min !== null && `No new searches below ${status.free_gb_min} GB free.`,
            status.queue_max !== null && `At most ${status.queue_max} searches waiting or running.`]
            .filter(Boolean).join(" ")}</p>}
        {status.workers.length ? <ul class="c-admin-workers u-small">{status.workers.map((w) =>
            <li key={w.id}>worker {w.id} since {at(w.started)}</li>)}</ul>
          : <p class="c-error">No worker is running.</p>}
      </>}
    </Panel>
    <Panel title={`Users${pending ? ` (${pending} waiting)` : ""}`}>
      <Users users={users} onChange={load} onError={onError} />
    </Panel>
    <Panel title="All searches">
      {jobs.length ? <ul class="c-admin-list">{jobs.slice(0, JOBS_SHOWN).map((j) => {
        const [st, text] = jobStatus(j);
        const finished = ["done", "failed", "cancelled"].includes(j.status);
        return <li key={j.id} class="c-admin-row c-admin-job">
          <div class="c-admin-head"><b>{j.title}</b><StatusChip status={st}>{text}</StatusChip></div>
          <div class="u-small">{j.user_id === undefined ? "?" : names.get(j.user_id ?? -1) || "nobody"} · {at(j.created)}</div>
          {!finished && <div class="c-admin-tools">
            <Button kind="danger" onClick={() => api.cancel(j.id).catch(onError)}>Cancel</Button></div>}
        </li>;
      })}</ul> : <p class="u-small">No searches.</p>}
    </Panel>
    <Panel title={`Problem reports${reports.some((r) => r.status === "open")
      ? ` (${reports.filter((r) => r.status === "open").length} open)` : ""}`}>
      <Reports reports={reports} onChange={load} onError={onError} />
    </Panel>
    <Panel title="Runs">
      <Runs runs={runs} names={names} onChange={load} onError={onError} />
    </Panel>
    <Panel title="OpenStreetMap extracts">
      <p class="u-small">The coastline and the cliffs from a downloaded extract instead of the Overpass API.</p>
      <div class="c-admin-tools"><Button icon="layers" href="#osm">OpenStreetMap extracts</Button></div>
    </Panel>
  </div>;
}
