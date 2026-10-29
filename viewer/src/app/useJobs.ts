// The list of the user's jobs for every interface: listed once (api.jobs), then kept up to date by
// the event stream (subscribe, applyEvent), with whether a worker runs. jobProgress() reads a job's
// state as one step, one bar and one label, for the new shells' cards.
// (Named useJobs.ts, not jobs.ts: on Windows "./Jobs" would resolve to jobs.ts.)

import { useCallback, useEffect, useState } from "preact/hooks";
import { api, subscribe, type Job, type JobEvent } from "./api";
import { beingSaved, FINISHED } from "./jobStatus";

export interface Bar {
  bar: string;
  stage: string;
  count: number;
  total: number;
  unit: string;
  extra: Record<string, unknown>;
  done: boolean;
  step?: string;
}

export interface JobView extends Job {
  step: string;                // e.g. "Tile 3/15 · Step 3/5: evaluating anchor pairs"
  bars: Record<string, Bar>;   // "Download", "Compute"
  log: JobEvent[];             // messages seen since the page was opened
}

/** The status of a job in words. */
export const STATUS: Record<Job["status"], string> = {
  queued: "waiting", downloading: "downloading data", ready: "data ready, waiting to compute",
  computing: "computing", done: "done", failed: "failed", cancelled: "cancelled"};

export { beingSaved, FINISHED };
const LOG_KEEP = 500;  // messages kept per job

const fresh = (job: Job): JobView => ({...job, bars: {}, log: [], step: ""});

/** A job event applied to the list of jobs. */
export function applyEvent(jobs: JobView[], event: JobEvent): JobView[] {
  let found = jobs.some((j) => j.id === event.job);
  if (!found && event.kind === "status") {  // a new job (from this or another page)
    jobs = [{id: event.job, created: event.data.created || "", status: event.data.status,
             title: event.data.title || event.job, run_id: null, error: null, finished: null, bars: {}, log: [],
             step: ""},
            ...jobs];
    found = true;
  }
  return jobs.map((job) => {
    if (job.id !== event.job) return job;
    if (event.kind === "status") {
      const next = {...job, ...event.data};
      if (event.data.status === "downloading" || event.data.status === "computing") { next.bars = {}; next.step = ""; }
      return next;
    }
    if (event.kind === "progress") {
      const bar = event.data as Bar;
      const step = bar.step ?? job.step;
      return bar.bar ? {...job, step, bars: {...job.bars, [bar.bar]: bar}} : {...job, step};
    }
    return {...job, log: job.log.concat([event]).slice(-LOG_KEEP)};
  });
}

/** A new listing merged into the list: the jobs keep their progress and messages seen so far. */
function merge(current: JobView[], listed: Job[]): JobView[] {
  const known = new Map(current.map((j) => [j.id, j]));
  return listed.map((job) => {
    const view = known.get(job.id);
    return view ? {...view, ...job} : fresh(job);
  });
}

export interface JobsState {
  jobs: JobView[];
  worker: boolean;     // whether a worker runs the searches
  error: string;
  active: number;      // jobs waiting or running
  /** Lists the jobs again (e.g. after starting one, when the event stream may lag). */
  refresh(): Promise<void>;
}

/** The user's jobs, kept up to date by the event stream. */
export function useJobs(): JobsState {
  const [jobs, setJobs] = useState<JobView[]>([]);
  const [worker, setWorker] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let stop = () => {};
    let closed = false;
    api.jobs().then(({jobs: listed, seq, worker: alive}) => {
      if (closed) return;
      setJobs(listed.map(fresh));
      setWorker(alive);
      stop = subscribe(seq, (event) => setJobs((current) => applyEvent(current, event)), setWorker);
    }, (e) => setError(e.message));
    return () => { closed = true; stop(); };
  }, []);

  const refresh = useCallback(() => api.jobs().then(({jobs: listed, worker: alive}) => {
    setJobs((current) => merge(current, listed));
    setWorker(alive);
  }, (e) => setError(e.message)), []);

  const active = jobs.filter((j) => !FINISHED.includes(j.status)).length;
  return {jobs, worker, error, active, refresh};
}

export interface JobProgress {
  state: "running" | "waiting" | "done" | "failed";  // the kit's StatusChip
  label: string;           // the status in words
  step: string;            // the current step ("Step 3/5: evaluating anchor pairs", "Tile 2/4 · ..."), or ""
  fraction: number | null; // 0..1 of the whole job; null while unknown
  finished: boolean;
}

const share = (bar: Bar | undefined) => !bar ? 0 : bar.total ? Math.min(1, bar.count / bar.total) : bar.done ? 1 : 0;

/** A job's progress as one step, one bar and one label: the download (its bar), then the computing
 *  (its tiles and the five steps of each, the current step by its bar). */
export function jobProgress(job: JobView): JobProgress {
  const finished = FINISHED.includes(job.status);
  const state = job.status === "done" ? "done" : job.status === "failed" || job.status === "cancelled" ? "failed"
    : job.status === "downloading" || job.status === "computing" ? "running" : "waiting";
  let fraction: number | null = null;
  if (job.status === "done") fraction = 1;
  else if (job.status === "downloading") {
    const bar = job.bars.Download;
    fraction = bar && bar.total ? share(bar) : null;
  } else if (job.status === "computing") {
    const tile = /Tile (\d+)\/(\d+)/.exec(job.step);
    const step = /Step (\d+)\/(\d+)/.exec(job.step);
    if (step) {
      const [n, steps] = [Number(step[1]), Number(step[2])];
      const [t, tiles] = tile ? [Number(tile[1]), Number(tile[2])] : [1, 1];
      fraction = Math.min(1, ((t - 1) + (n - 1 + share(job.bars.Compute)) / steps) / tiles);
    }
  } else if (job.status === "ready") fraction = 0;
  return {state, label: STATUS[job.status], step: finished ? "" : job.step, fraction, finished};
}
