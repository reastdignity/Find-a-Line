// The app (python findaline.py web): the map on the left, a new search, the queue, the saved
// runs and the favourite lines on the right. Results open as the runs' own maps (runs/<id>/map.html).
// ?ui=b|c|d|kit shows a prototype of a new interface instead (ui/choice.ts); this one stays the default.

import "leaflet/dist/leaflet.css";
import L from "leaflet";
import iconRetinaUrl from "leaflet/dist/images/marker-icon-2x.png";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import shadowUrl from "leaflet/dist/images/marker-shadow.png";
import { render } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import "./app.css";
import "../ui/pages.css";
import { api, user, type Field, type RunListed, type SearchRequest } from "./api";
import { JobList } from "./Jobs";
import { useJobs } from "./useJobs";
import { AdminPanel } from "./Admin";
import { Favourites } from "./Favourites";
import { OsmPanel } from "./OsmPanel";
import { PlaceMap } from "./PlaceMap";
import { SearchForm } from "./SearchForm";
import { AppB } from "./b/AppB";
import { BetaNotice, ReportDialog } from "./ReportDialog";
import { AppC } from "./c/AppC";
import { AppD } from "./d/AppD";
import { Gallery } from "../ui/Gallery";
import { KitResult } from "../result/view/KitResult";
import { KitSearch } from "../search/KitSearch";
import { chooseUi, uiHref } from "../ui/choice";

L.Icon.Default.mergeOptions({iconRetinaUrl, iconUrl, shadowUrl});
delete (L.Icon.Default.prototype as any)._getIconUrl;

type Tab = "search" | "jobs" | "runs" | "favourites" | "osm" | "admin";

/** Share link of a run (the public site): made, renewed or revoked by its owner; a new link is shown
 *  once (the server keeps only its hash). */
function Share({run, onChange}: {run: RunListed; onChange(shared: boolean): void}) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const share = () => api.share(run.id).then((r) => { setUrl(r.url); setError(""); onChange(true); },
                                              (e) => setError(e.message));
  const stop = () => api.unshare(run.id).then(() => { setUrl(""); setError(""); onChange(false); },
                                              (e) => setError(e.message));
  return <div class="fa-share">
    {run.shared ? <span>
      <span class="fa-hint">shared by a link </span>
      <button class="fa-small" title="a new link; the old one stops working" onClick={share}>new link</button>{" "}
      <button class="fa-small" title="the link stops working" onClick={stop}>stop sharing</button>
    </span> : <button class="fa-small" title="a link that opens this map for anyone who has it"
                      onClick={share}>share link</button>}
    {url && <input class="fa-share-url" readOnly value={url} onFocus={(e) => e.currentTarget.select()} />}
    {error && <span class="fa-error"> {error}</span>}
  </div>;
}

function Runs() {
  const [runs, setRuns] = useState<RunListed[] | null>(null);
  const [sharing, setSharing] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { api.runs().then((r) => { setRuns(r.runs); setSharing(r.public); }); }, []);
  if (!runs) return <p class="fa-hint">Loading…</p>;
  if (!runs.length) return <p class="fa-hint">No saved runs yet.</p>;
  const changed = (id: string, shared: boolean) => setRuns(runs.map((r) => r.id === id ? {...r, shared} : r));
  const remove = (r: RunListed) => {
    if (!confirm(`Delete the run "${r.name}" and its files? This cannot be undone.`)) return;
    api.deleteRun(r.id).then(() => setRuns(runs.filter((x) => x.id !== r.id)), (e) => setError(e.message));
  };
  return <div>{error && <p class="fa-error">{error}</p>}{runs.map((r) => <div class="fa-run" key={r.id}>
    <div><b>{r.name}</b> <span class="fa-hint">{r.created.slice(0, 16).replace("T", " ")}</span>
      {r.demo && <span class="fa-badge" title="an example: its map is open to everyone">demo</span>}</div>
    <div class="fa-hint">{r.summary.spots} spots, {r.summary.lines} lines · {r.description}</div>
    {r.has_map && <a href={`/runs/${encodeURIComponent(r.id)}/map.html`} target="_blank">open map</a>}
    {sharing && r.has_map && r.own && <Share run={r} onChange={(shared) => changed(r.id, shared)} />}
    {sharing && r.own && <button class="fa-small" title="delete the run and its files (frees your quota)"
                                 onClick={() => remove(r)}>delete</button>}
  </div>)}</div>;
}

/** The way to the prototypes of a new interface. */
function TryNew() {
  return <span class="fa-try">Try the new interface: <a href={uiHref("b")}>B</a> · <a href={uiHref("c")}>C</a> · <a href={uiHref("d")}>D</a></span>;
}

function App() {
  const mapRef = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<PlaceMap | null>(null);
  const [schema, setSchema] = useState<{fields: Field[]; groups: Record<string, string>} | null>(null);
  const [tab, setTab] = useState<Tab>("search");
  const {jobs, worker, error: jobsError, active} = useJobs();
  const [ownError, setError] = useState("");
  const error = ownError || jobsError;
  const [editing, setEditing] = useState<{request: SearchRequest | null; key: number}>({request: null, key: 0});
  const edit = (id: string) => api.job(id).then((job) => {
    setEditing((e) => ({request: job.request, key: e.key + 1}));  // a new form with the job's values
    setTab("search");
  }, (e) => setError(e.message));

  useEffect(() => {
    const placeMap = new PlaceMap(mapRef.current!);
    (window as any).FINDALINE_PLACE = placeMap;  // for tests and the browser console
    setPlace(placeMap);
    api.schema().then(setSchema, (e) => setError(e.message));
  }, []);

  return <div class="fa-app">
    <div class="fa-map" ref={mapRef}></div>
    <div class="fa-side">
      {!user.local && <div class="fa-user">
        <span>{user.name}{user.admin ? " (administrator)" : ""}</span>
        <span class="fa-links"><a href="/privacy" target="_blank">privacy</a>{" "}
          <a href="/about" target="_blank">about / data licences</a></span>
        <TryNew />
        <button class="fa-small" onClick={() => api.logout().finally(() => { location.href = "/"; })}>sign out</button>
      </div>}
      {user.local && <div class="fa-try-bar"><TryNew /></div>}
      <div class="fa-tabs">
        <button class={tab === "search" ? "fa-on" : ""} onClick={() => setTab("search")}>New search</button>
        <button class={tab === "jobs" ? "fa-on" : ""} onClick={() => setTab("jobs")}>
          Queue{active ? ` (${active})` : ""}</button>
        <button class={tab === "runs" ? "fa-on" : ""} onClick={() => setTab("runs")}>Runs</button>
        <button class={tab === "favourites" ? "fa-on" : ""} onClick={() => setTab("favourites")}>Favourites</button>
        {user.local && <button class={tab === "osm" ? "fa-on" : ""} onClick={() => setTab("osm")}>OSM</button>}
        {!user.local && user.admin &&
          <button class={tab === "admin" ? "fa-on" : ""} onClick={() => setTab("admin")}>Admin</button>}
      </div>
      {error && <p class="fa-error">{error}</p>}
      {!worker && <p class="fa-error fa-no-worker">The worker is not running: searches wait in the queue
        until it starts (<code>findaline.py worker</code>, or <code>findaline.py web</code> without
        --no-worker).</p>}
      <div class="fa-panel-body">
        <div hidden={tab !== "search"}>
          {place && schema && <SearchForm key={editing.key} initial={editing.request} place={place}
                                          fields={schema.fields} groups={schema.groups}
                                          onStarted={() => setTab("jobs")} />}
        </div>
        {tab === "jobs" && <JobList jobs={jobs} onEdit={edit} />}
        {tab === "runs" && <Runs />}
        {tab === "favourites" && place && <Favourites place={place} />}
        {tab === "osm" && place && <OsmPanel place={place} onStarted={() => setTab("jobs")} />}
        {tab === "admin" && place && <AdminPanel place={place} jobs={jobs} onStarted={() => setTab("jobs")} />}
      </div>
    </div>
  </div>;
}

const SHELLS = {old: App, b: AppB, c: AppC, d: AppD, kit: Gallery, "kit-result": KitResult, "kit-search": KitSearch};
const choice = chooseUi(), Shell = SHELLS[choice];
// The beta notice after a sign-in and Report a problem (the public site), whichever interface is shown
const dialogs = ["old", "b", "c", "d"].includes(choice) && <><BetaNotice /><ReportDialog /></>;
render(<><Shell />{dialogs}</>, document.getElementById("app")!);
