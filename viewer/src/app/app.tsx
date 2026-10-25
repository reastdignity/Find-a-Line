// The app (python findaline.py web): the map on the left, a new search, the queue and
// the saved runs on the right. Results open as the runs' own maps (runs/<id>/map.html).

import "leaflet/dist/leaflet.css";
import L from "leaflet";
import iconRetinaUrl from "leaflet/dist/images/marker-icon-2x.png";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import shadowUrl from "leaflet/dist/images/marker-shadow.png";
import { render } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import "./app.css";
import { api, subscribe, type Field, type JobEvent, type RunListed } from "./api";
import { JobList, type Bar, type JobView } from "./Jobs";
import { OsmPanel } from "./OsmPanel";
import { PlaceMap } from "./PlaceMap";
import { SearchForm } from "./SearchForm";

L.Icon.Default.mergeOptions({iconRetinaUrl, iconUrl, shadowUrl});
delete (L.Icon.Default.prototype as any)._getIconUrl;

type Tab = "search" | "jobs" | "runs" | "osm";
const LOG_KEEP = 500;  // messages kept per job

function Runs() {
  const [runs, setRuns] = useState<RunListed[] | null>(null);
  useEffect(() => { api.runs().then((r) => setRuns(r.runs)); }, []);
  if (!runs) return <p class="fa-hint">Loading…</p>;
  if (!runs.length) return <p class="fa-hint">No saved runs yet.</p>;
  return <div>{runs.map((r) => <div class="fa-run">
    <div><b>{r.name}</b> <span class="fa-hint">{r.created.slice(0, 16).replace("T", " ")}</span></div>
    <div class="fa-hint">{r.summary.spots} spots, {r.summary.lines} lines · {r.description}</div>
    {r.has_map && <a href={`/runs/${encodeURIComponent(r.id)}/map.html`} target="_blank">open map</a>}
  </div>)}</div>;
}

function App() {
  const mapRef = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<PlaceMap | null>(null);
  const [schema, setSchema] = useState<{fields: Field[]; groups: Record<string, string>} | null>(null);
  const [tab, setTab] = useState<Tab>("search");
  const [jobs, setJobs] = useState<JobView[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    setPlace(new PlaceMap(mapRef.current!));
    api.schema().then(setSchema, (e) => setError(e.message));
    let stop = () => {};
    api.jobs().then(({jobs: listed, seq}) => {
      setJobs(listed.map((j) => ({...j, bars: {}, log: [], step: ""})));
      stop = subscribe(seq, (event) => setJobs((current) => apply(current, event)));
    }, (e) => setError(e.message));
    return () => stop();
  }, []);

  const active = jobs.filter((j) => !["done", "failed", "cancelled"].includes(j.status)).length;
  return <div class="fa-app">
    <div class="fa-map" ref={mapRef}></div>
    <div class="fa-side">
      <div class="fa-tabs">
        <button class={tab === "search" ? "fa-on" : ""} onClick={() => setTab("search")}>New search</button>
        <button class={tab === "jobs" ? "fa-on" : ""} onClick={() => setTab("jobs")}>
          Queue{active ? ` (${active})` : ""}</button>
        <button class={tab === "runs" ? "fa-on" : ""} onClick={() => setTab("runs")}>Runs</button>
        <button class={tab === "osm" ? "fa-on" : ""} onClick={() => setTab("osm")}>OSM</button>
      </div>
      {error && <p class="fa-error">{error}</p>}
      <div class="fa-panel-body">
        <div hidden={tab !== "search"}>
          {place && schema && <SearchForm place={place} fields={schema.fields} groups={schema.groups}
                                          onStarted={() => setTab("jobs")} />}
        </div>
        {tab === "jobs" && <JobList jobs={jobs} />}
        {tab === "runs" && <Runs />}
        {tab === "osm" && place && <OsmPanel place={place} onStarted={() => setTab("jobs")} />}
      </div>
    </div>
  </div>;
}

/** A job event applied to the list of jobs. */
function apply(jobs: JobView[], event: JobEvent): JobView[] {
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

render(<App />, document.getElementById("app")!);
