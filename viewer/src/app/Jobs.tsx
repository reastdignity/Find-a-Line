// The queue: every job with its status, live progress, messages and result.

import { useState } from "preact/hooks";
import { api, type Job, type JobEvent } from "./api";

export interface Bar {
  bar: string;
  stage: string;
  count: number;
  total: number;
  unit: string;
  extra: Record<string, unknown>;
  done: boolean;
}

export interface JobView extends Job {
  bars: Record<string, Bar>;   // "Download", "Compute"
  log: JobEvent[];             // messages seen since the page was opened
}

const STATUS: Record<Job["status"], string> = {
  queued: "waiting", downloading: "downloading data", ready: "data ready, waiting to compute",
  computing: "computing", done: "done", failed: "failed", cancelled: "cancelled"};

function ProgressBar({bar}: {bar: Bar}) {
  const share = bar.total ? Math.min(1, bar.count / bar.total) : bar.done ? 1 : 0;
  const extra = Object.entries(bar.extra).map(([k, v]) => `${k} ${v}`).join(", ");
  return <div class="fa-bar">
    <div class="fa-bar-text">{bar.bar}: {bar.stage} {bar.total ? `${+bar.count.toFixed(1)}/${+bar.total.toFixed(1)} ${bar.unit}` : ""}
      {extra && ` (${extra})`}{bar.done ? " ✓" : ""}</div>
    <div class="fa-bar-track"><div class="fa-bar-fill" style={`width:${(100 * share).toFixed(1)}%`}></div></div>
  </div>;
}

function JobItem({job}: {job: JobView}) {
  const [open, setOpen] = useState(false);
  const [history, setHistory] = useState<JobEvent[] | null>(null);
  const active = job.status === "downloading" || job.status === "computing";
  const toggle = async () => {
    if (!open && history === null) setHistory((await api.jobEvents(job.id)).events);
    setOpen(!open);
  };
  const log = (history || []).concat(job.log.filter((e) => !history || e.seq > (history.length ? history[history.length - 1].seq : 0)))
    .filter((e) => e.kind === "info" || e.kind === "warning");
  return <div class={"fa-job fa-" + job.status}>
    <div class="fa-job-head">
      <b>{job.title}</b> <span class="fa-status">{STATUS[job.status]}</span>
      <span class="fa-job-tools">
        {job.status === "done" && job.run_id &&
          <a href={`/runs/${encodeURIComponent(job.run_id)}/map.html`} target="_blank">open map</a>}
        {!["done", "failed", "cancelled"].includes(job.status) &&
          <button class="fa-small" onClick={() => api.cancel(job.id).catch(() => {})}>cancel</button>}
        <button class="fa-small" onClick={toggle}>{open ? "hide log" : "log"}</button>
      </span>
    </div>
    {active && Object.values(job.bars).map((bar) => <ProgressBar bar={bar} />)}
    {job.error && <div class="fa-error">{job.error}</div>}
    {open && <pre class="fa-log">{log.map((e) => (e.kind === "warning" ? "WARNING: " : "") + e.data).join("\n")}</pre>}
  </div>;
}

export function JobList({jobs}: {jobs: JobView[]}) {
  if (!jobs.length) return <p class="fa-hint">No searches yet.</p>;
  return <div>{jobs.map((job) => <JobItem key={job.id} job={job} />)}</div>;
}
