// "My searches" of prototype B (B2): the searches waiting or running with their steps and progress
// (useJobs, jobProgress), the failed ones with their error, and the saved runs (/api/runs) with their
// numbers: show the results, edit and run again, share (the public site) and delete (the public site,
// own runs; confirmed in the card). The usage meters on top, and the old app's notice when no worker runs.

import type { ComponentChildren } from "preact";
import { useState } from "preact/hooks";
import { api, user, type RunListed, type Usage } from "../api";
import { beingSaved, FINISHED, jobProgress, type JobsState, type JobView } from "../useJobs";
import { Button, Progress, StatusChip } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { UsageMeters } from "../../search/SearchPanel";
import { ShareRun } from "../../result/view/RunActions";
import { runCounts } from "../../result/view/ResultWorkspace";

export { runCounts };

const cx = (...names: (string | false | null | undefined)[]) => names.filter(Boolean).join(" ");

/** "2026-10-29 14:05" of an ISO time; the time only for today. */
export function when(iso: string | null | undefined): string {
  if (!iso) return "";
  const day = iso.slice(0, 10), today = new Date().toISOString().slice(0, 10);
  return day === today ? iso.slice(11, 16) : iso.slice(0, 16).replace("T", " ");
}

/** The steps of a search: the elevation data, finding the lines (tiles and steps), the map. */
export function JobSteps({job}: {job: JobView}) {
  const s = job.status;
  const downloaded = s === "ready" || s === "computing" || s === "done";
  const bar = job.bars.Download;
  const data = bar && bar.total && bar.unit ? ` · ${+bar.count.toFixed(1)} of ${+bar.total.toFixed(1)} ${bar.unit}` : "";
  const steps: {label: string; state: "done" | "now" | "next"}[] = [
    {label: "Elevation data" + (s === "downloading" ? data : ""),
     state: downloaded ? "done" : s === "downloading" ? "now" : "next"},
    {label: s === "computing" && job.step ? `Finding lines · ${job.step}` : "Finding lines",
     state: s === "done" ? "done" : s === "computing" ? "now" : "next"},
    {label: "Map and results", state: s === "done" ? "done" : "next"},
  ];
  return <ol class="b-steps">{steps.map((step, k) => <li key={k} class={`b-step b-${step.state}`}>
    <span class="b-step-n">{step.state === "done" ? <Icon name="check" /> : k + 1}</span>{step.label}</li>)}</ol>;
}

/** A search waiting or running (or one just finished, in the search panel): its status, steps and bar. */
export function JobCard({job, run, onShow, onEdit, onCancel, actions}:
                        {job: JobView; run?: RunListed | null; onShow?(runId: string): void; onEdit?(jobId: string): void;
                         onCancel?(jobId: string): void; actions?: ComponentChildren}) {
  const p = jobProgress(job);
  const osm = job.title.startsWith("OSM ");
  return <article class={cx("b-card", `b-${p.state}`)} data-job={job.id} data-status={job.status}>
    <div class="b-card-head"><StatusChip status={p.state}>{p.state === "waiting" ? "Waiting" : p.state === "running"
      ? "Running" : p.state === "done" ? "Done" : job.status === "cancelled" ? "Cancelled" : "Failed"}</StatusChip>
      <b class="b-card-title">{job.title}</b><span class="u-small">{when(job.created)}</span></div>
    {!p.finished && <>
      {!osm && <JobSteps job={job} />}
      <Progress value={p.fraction} label={<span class="u-small">{p.label}</span>} />
    </>}
    {job.status === "done" && run && <span class="u-small b-counts">{runCounts(run)}</span>}
    {job.error && <p class="b-card-error">{job.error}</p>}
    <div class="b-card-acts">
      {job.status === "done" && job.run_id && onShow &&
        <Button kind="primary" icon="next" onClick={() => onShow(job.run_id!)}>Show results</Button>}
      {!p.finished && onCancel && <Button kind="ghost" icon="close" onClick={() => onCancel(job.id)}>Cancel</Button>}
      {p.finished && job.status !== "done" && !osm && onEdit &&
        <Button icon="edit" onClick={() => onEdit(job.id)}>Edit &amp; retry</Button>}
      {actions}
    </div>
  </article>;
}

/** A saved run: its numbers and actions. */
function RunCard({run, jobId, sharing, onShow, onEdit, onDeleted}:
                 {run: RunListed; jobId: string | null; sharing: boolean; onShow(runId: string): void;
                  onEdit(jobId: string): void; onDeleted(runId: string): void}) {
  const [shared, setShared] = useState(run.shared);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState("");
  const remove = () => api.deleteRun(run.id).then(() => onDeleted(run.id), (e) => setError(e.message));
  return <article class="b-card b-done" data-run={run.id}>
    <div class="b-card-head">
      <StatusChip status={shared ? "shared" : "done"}>{shared ? "Shared" : "Done"}</StatusChip>
      <b class="b-card-title">{run.name}</b><span class="u-small">{when(run.created)}</span></div>
    <span class="u-small b-counts">{runCounts(run)}{run.demo && " · demo, open to everyone"}</span>
    {run.description && <span class="u-small b-desc">{run.description}</span>}
    <div class="b-card-acts">
      {run.has_map && <Button kind="primary" icon="next" onClick={() => onShow(run.id)}>Show results</Button>}
      {jobId && <Button icon="edit" onClick={() => onEdit(jobId)}>Edit &amp; run again</Button>}
      {sharing && run.has_map && run.own &&
        <ShareRun runId={run.id} run={{...run, shared}} align="left" onChange={setShared} />}
      {sharing && run.own && !asking && <Button kind="ghost" class="b-delete" onClick={() => setAsking(true)}>Delete</Button>}
    </div>
    {asking && <div class="b-confirm" role="alertdialog" aria-label="Delete the run">
      <span class="u-grow">Delete “{run.name}” and its files? This cannot be undone.</span>
      <Button kind="danger" onClick={remove}>Delete</Button>
      <Button kind="ghost" onClick={() => setAsking(false)}>Keep</Button>
    </div>}
    {error && <p class="b-card-error" role="alert">{error}</p>}
  </article>;
}

const FAILED_SHOWN = 5;  // the latest failed or cancelled searches listed

export interface SearchesProps {
  jobs: JobsState;
  runs: RunListed[] | null;
  /** The public site: share and delete one's own runs. */
  sharing: boolean;
  usage: Usage | null;
  onShow(runId: string): void;
  onEdit(jobId: string): void;
  onDeleted(runId: string): void;
}

export function Searches({jobs, runs, sharing, usage, onShow, onEdit, onDeleted}: SearchesProps) {
  const active = jobs.jobs.filter((j) => !FINISHED.includes(j.status));
  const failed = jobs.jobs.filter((j) => j.status === "failed" || j.status === "cancelled").slice(0, FAILED_SHOWN);
  const jobOf = (runId: string) => jobs.jobs.find((j) => j.run_id === runId && !j.title.startsWith("OSM "))?.id ?? null;
  const cancel = (id: string) => api.cancel(id).catch(() => {});
  return <div class="b-searches">
    <UsageMeters usage={usage} compact />
    {!jobs.worker && <p class="b-no-worker" role="alert">The worker is not running: searches wait in the queue
      until it starts (<code>findaline.py worker</code>, or <code>findaline.py web</code> without --no-worker).</p>}
    {jobs.error && <p class="b-card-error">{jobs.error}</p>}
    {active.map((j) => <JobCard key={j.id} job={j} onCancel={cancel} />)}
    {failed.map((j) => <JobCard key={j.id} job={j} onEdit={onEdit} />)}
    {runs === null ? <p class="u-small">Loading the saved runs…</p>
      : runs.filter((r) => !beingSaved(r, jobs.jobs)).map((r) => <RunCard key={r.id} run={r} jobId={jobOf(r.id)} sharing={sharing && !user.local}
                                 onShow={onShow} onEdit={onEdit} onDeleted={onDeleted} />)}
    {runs !== null && !runs.length && !active.length && !failed.length &&
      <p class="u-small">No searches yet: start one in Search.</p>}
  </div>;
}
