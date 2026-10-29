// The queue: every job with its status, live progress, messages and result.

import { useState } from "preact/hooks";
import { api, type JobEvent } from "./api";
import { STATUS, type Bar, type JobView } from "./useJobs";

export type { Bar, JobView };

function ProgressBar({bar}: {bar: Bar}) {
  const share = bar.total ? Math.min(1, bar.count / bar.total) : bar.done ? 1 : 0;
  const extra = Object.entries(bar.extra).map(([k, v]) => `${k} ${v}`).join(", ");
  return <div class="fa-bar">
    <div class="fa-bar-text">{bar.bar}: {bar.stage} {bar.total ? `${+bar.count.toFixed(1)}/${+bar.total.toFixed(1)} ${bar.unit}` : ""}
      {extra && ` (${extra})`}{bar.done ? " ✓" : ""}</div>
    <div class="fa-bar-track"><div class="fa-bar-fill" style={`width:${(100 * share).toFixed(1)}%`}></div></div>
  </div>;
}

function JobItem({job, onEdit}: {job: JobView; onEdit(id: string): void}) {
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
        {["done", "failed", "cancelled"].includes(job.status) && <span>
          <button class="fa-small" title="queue the same search again (a large area resumes where it stopped)"
                  onClick={() => api.again(job.id).catch(() => {})}>run again</button>{" "}
          {!job.title.startsWith("OSM ") && <button class="fa-small" title="load it into New search to change it"
                                                    onClick={() => onEdit(job.id)}>edit</button>}
        </span>}
        <button class="fa-small" onClick={toggle}>{open ? "hide log" : "log"}</button>
      </span>
    </div>
    {active && job.step && <div class="fa-step">{job.step}</div>}
    {active && Object.values(job.bars).filter((bar) => !bar.done).map((bar) => <ProgressBar bar={bar} />)}
    {job.error && <div class="fa-error">{job.error}</div>}
    {open && <pre class="fa-log">{log.map((e) => (e.kind === "warning" ? "WARNING: " : "") + e.data).join("\n")}</pre>}
  </div>;
}

export function JobList({jobs, onEdit}: {jobs: JobView[]; onEdit(id: string): void}) {
  if (!jobs.length) return <p class="fa-hint">No searches yet.</p>;
  return <div>{jobs.map((job) => <JobItem key={job.id} job={job} onEdit={onEdit} />)}</div>;
}
