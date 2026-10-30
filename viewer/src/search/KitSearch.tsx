// ?ui=kit-search: the search panel (search/) on its own, to develop and test its pieces without
// the shells: a full-screen map (PlaceMap), B's floating panel on desktops or a sheet below 768 px,
// a switch to C's variant (on desktops the form in a column left of the map, presets as cards, the
// estimate in a bottom bar), the usage meters and the user's searches with their progress
// (useJobs, jobProgress). ?variant=b|c picks the variant, ?theme=light|dark the theme.

import { useEffect, useRef, useState } from "preact/hooks";
import { api, jobError, type Schema } from "../app/api";
import { PlaceMap } from "../app/PlaceMap";
import { jobProgress, useJobs, type JobsState, type JobView } from "../app/useJobs";
import { storedTheme } from "../ui/choice";
import { DESKTOP, Panel, Progress, Screen, Segmented, Sheet, StatusChip, useMedia, type Theme } from "../ui/components";
import { EstimateBar, SearchPanel, UsageMeters } from "./SearchPanel";
import { useSearch, useUsage, type UsageState } from "./useSearch";

type Variant = "b" | "c";

function JobCard({job}: {job: JobView}) {
  const p = jobProgress(job);
  return <div class="s-job" data-job={job.id} data-status={job.status}>
    <div class="s-job-head"><StatusChip status={p.state}>{p.label}</StatusChip><b>{job.title}</b></div>
    {!p.finished && <Progress value={p.fraction} label={p.step || undefined} />}
    {job.error && <div class="s-job-error">{jobError(job)}</div>}
  </div>;
}

function Searches({jobs, usage}: {jobs: JobsState; usage: UsageState}) {
  return <div class="s-searches">
    <UsageMeters usage={usage.usage} />
    {!jobs.worker && <p class="s-hint">The worker is not running: searches wait until it starts.</p>}
    {jobs.error && <p class="s-problem">{jobs.error}</p>}
    <div class="s-jobs">
      {jobs.jobs.length ? jobs.jobs.slice(0, 8).map((j) => <JobCard key={j.id} job={j} />)
        : <p class="s-hint">No searches yet.</p>}
    </div>
  </div>;
}

/** Everything around the map, once the map and the schema are there. The grid of .s-kit places
 *  C's column (s-c-form), the overlays of the map (s-kit-over) and C's bar (s-c-bar). */
function Demo({place, schema, variant, setVariant}:
              {place: PlaceMap; schema: Schema; variant: Variant; setVariant(v: Variant): void}) {
  const wide = useMedia(DESKTOP);
  const jobs = useJobs();
  const usage = useUsage();
  const [started, setStarted] = useState("");
  const search = useSearch({place, schema, onStarted: (id) => {
    setStarted(id);
    jobs.refresh();
    usage.refresh();
  }});
  (window as any).FINDALINE_SEARCH = search;  // for tests and the browser console
  useEffect(() => { usage.refresh(); }, [jobs.active]);

  const columns = variant === "c" && wide;
  const toggle = <div class="s-kit-toggle">
    <Segmented label="Variant" value={variant} onChange={setVariant}
               options={[{value: "b", label: "B · panel"}, {value: "c", label: "C · form"}]} />
  </div>;
  const startedNote = started && <p class="s-hint s-started" data-started={started}>Queued: {started}</p>;
  const panel = <SearchPanel place={place} schema={schema} onStarted={() => {}} search={search} variant={variant}
                             estimate={!columns} usage={usage.usage} phone={!wide} />;

  if (!wide) {
    return <div class="s-kit-over">
      {toggle}
      <Sheet label="New search" desktop="sheet" initial="half" peek={150}>
        {panel}
        {startedNote}
        <section class="s-sheet-jobs"><h2 class="u-h">Your searches</h2><Searches jobs={jobs} usage={usage} /></section>
      </Sheet>
    </div>;
  }
  return <>
    {columns && <aside class="s-c-form" aria-label="New search">{panel}{startedNote}</aside>}
    <div class="s-kit-over">
      {toggle}
      {!columns && <Sheet label="New search" desktop="float">{panel}{startedNote}</Sheet>}
      <aside class="s-kit-jobs"><Panel title="Your searches"><Searches jobs={jobs} usage={usage} /></Panel></aside>
    </div>
    {columns && <footer class="s-c-bar"><EstimateBar search={search} usage={usage.usage} layout="bar" /></footer>}
  </>;
}

export function KitSearch() {
  const params = new URLSearchParams(location.search);
  const asked = params.get("theme");
  const theme: Theme = asked === "light" || asked === "dark" ? asked : storedTheme();
  const wide = useMedia(DESKTOP);
  const [variant, setVariant] = useState<Variant>(params.get("variant") === "c" ? "c" : "b");
  const mapRef = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<PlaceMap | null>(null);
  const [schema, setSchema] = useState<Schema | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const placeMap = new PlaceMap(mapRef.current!, {controls: "bottomright"});
    (window as any).FINDALINE_PLACE = placeMap;  // for tests and the browser console
    setPlace(placeMap);
    api.schema().then(setSchema, (e) => setError(e.message));
    const observer = new ResizeObserver(() => placeMap.map.invalidateSize());
    observer.observe(mapRef.current!);
    return () => { observer.disconnect(); placeMap.map.remove(); };
  }, []);

  return <Screen theme={theme} class={`s-kit${variant === "c" && wide ? " s-cols" : ""}`}>
    <div class="s-kit-map" ref={mapRef} />
    {error && <p class="s-problem s-kit-error">{error}</p>}
    {place && schema && <Demo place={place} schema={schema} variant={variant} setVariant={setVariant} />}
  </Screen>;
}
