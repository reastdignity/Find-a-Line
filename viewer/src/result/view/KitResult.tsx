// ?ui=kit-result&run=<id>: the results UI (result/view) on its own, to develop and test its pieces
// without the shells: a full-screen map with the run (loadRun, ResultMap), a panel on the right
// (summary, filters, the list with the selected line's details under its row, as B3), the profile
// docked at the bottom and the run's actions on top; below 768 px the panel is a sheet (as B5).
// #line=<lat1,lon1,lat2,lon2> (or #run=…&line=…) opens that line. ?theme=light|dark picks a theme.

import L from "leaflet";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { DESKTOP, Screen, Sheet, useMedia, type Snap, type Theme } from "../../ui/components";
import { Logo } from "../../ui/icons";
import { storedTheme, uiHref } from "../../ui/choice";
import { api, type RunListed } from "../../app/api";
import { baseLayers } from "../../layers";
import type { Line, RunData } from "../../types";
import { loadRun, orthoForApp } from "../load";
import { ResultModel } from "../model";
import { ResultMap, type Padding } from "../ResultMap";
import { FilterBar } from "./FilterBar";
import { LineDetails } from "./LineDetails";
import { LinesTable } from "./LinesTable";
import { ProfileDock } from "./ProfileDock";
import { ResultSummary } from "./ResultSummary";
import { RunActions } from "./RunActions";
import { useModel } from "./common";
import { day, when } from "../../time";

const PANEL = 384 + 16;  // the panel's width and its margin (result.css .r-panel)

/** The `line` of a link: "#line=…" or "#run=…&line=…". */
function linkedLine(hash: string): string | null {
  if (!hash || hash === "#") return null;
  return new URLSearchParams(hash.slice(1)).get("line");
}

function RunPicker() {
  const [runs, setRuns] = useState<RunListed[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { api.runs().then((r) => setRuns(r.runs), (e) => setError(e.message)); }, []);
  const href = (id: string) => `${uiHref("kit-result")}&run=${encodeURIComponent(id)}`;
  return <div class="u-stub">
    <Logo />
    <h1 class="u-h u-xl">Results UI: pick a run</h1>
    {error && <p class="r-error">{error}</p>}
    {runs === null ? <p class="u-small">Loading…</p> : runs.length === 0 ? <p>No saved runs yet.</p>
      : <ul class="r-runs">{runs.filter((r) => r.has_map).map((r) => <li key={r.id}>
          <a href={href(r.id)}>{r.name}</a> <span class="u-small">{when(r.created)} ·
            {" "}{r.summary.spots} spots, {r.summary.lines} lines</span></li>)}</ul>}
    <a href={uiHref("kit")}>Component gallery</a>
  </div>;
}

function Result({data}: {data: RunData}) {
  const wide = useMedia(DESKTOP);
  const model = useMemo(() => new ResultModel(data), [data]);
  useModel(model);
  const mapRef = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<ResultMap | null>(null);
  const [measured, setMeasured] = useState<Line | null>(null);
  const [snap, setSnap] = useState<Snap>("half");
  const wideRef = useRef(wide);
  wideRef.current = wide;

  const padding = (): Padding => wideRef.current ? {top: 16, right: PANEL + 16, bottom: 16, left: 16}
    : {top: 60, right: 0, bottom: Math.round(innerHeight * 0.5), left: 0};

  useEffect(() => {
    const map = L.map(mapRef.current!, {center: data.center, zoom: 15, maxZoom: data.max_zoom});
    (window as any).FINDALINE_MAP = map;  // for tests and the browser console
    const layers = baseLayers(orthoForApp(data.ortho, data.run.id), data.max_zoom);
    Object.values(layers)[0].addTo(map);
    L.control.layers(layers, undefined, {position: "topleft"}).addTo(map);
    L.control.scale({position: "bottomleft"}).addTo(map);
    const resultMap = new ResultMap(map, model);
    (window as any).FINDALINE_RESULT = {model, view: resultMap};
    resultMap.fitArea(padding(), false);
    setView(resultMap);
    const observer = new ResizeObserver(() => map.invalidateSize());
    observer.observe(mapRef.current!);
    const open = () => {
      const wanted = linkedLine(location.hash);
      if (!wanted) return;
      const line = model.openLinked(wanted);
      if (line) resultMap.zoomTo(line, padding(), false);
    };
    open();
    window.addEventListener("hashchange", open);
    return () => {
      window.removeEventListener("hashchange", open);
      observer.disconnect();
      resultMap.destroy();
      map.remove();
    };
  }, [model]);

  // A new selection ends a measurement and shows the line on a phone's sheet
  useEffect(() => {
    let last = model.selected?.line;
    return model.subscribe(() => {
      const now = model.selected?.line;
      if (now === last) return;
      last = now;
      if (!now) return;
      setMeasured(null);
      view?.clearMeasure();
      if (!wideRef.current) setSnap((s) => s === "full" ? "half" : s);
    });
  }, [model, view]);

  /** Bring a line into the map's view (a row or ‹ › chose it). */
  const reveal = (l: Line) => { if (view && !view.inView(l)) view.zoomTo(l, padding()); };
  const zoom = (l: Line) => view?.zoomTo(l, padding());
  const measuredNow = (l: Line | null) => { setMeasured(l); if (l && !wide) setSnap("half"); };
  const closeMeasured = () => { setMeasured(null); view?.clearMeasure(); };
  const details = <LineDetails model={model} onZoom={zoom} onStep={reveal} />;
  const title = <div class="r-pill">
    <a class="u-ib u-plain u-round" href={uiHref("kit-result")} aria-label="All runs" title="All runs">‹</a>
    <span class="u-grow r-pill-name">{data.run.name}</span>
    <span class="u-small">{day(data.run.created)}</span>
  </div>;

  return <div class="r-kit-main">
    <div class="r-kit-map">
      <div class="r-kit-leaflet" ref={mapRef} />
      <div class="r-kit-top">{title}
        {!wide && <RunActions model={model} view={view} onMeasured={measuredNow} compact />}</div>
      {wide && <div class="r-kit-actions"><RunActions model={model} view={view} onMeasured={measuredNow} /></div>}
      {wide ? <aside class="u-panel r-panel" aria-label="Results">
        <div class="r-panel-head"><ResultSummary model={model} /><FilterBar model={model} /></div>
        <div class="r-panel-body"><LinesTable model={model} view={view} details={details} onSelect={reveal} /></div>
      </aside> : <Sheet label="Results" desktop="sheet" snap={snap} onSnap={setSnap} peek={132}
                        header={model.selected ? <LineDetails model={model} part="head" onStep={reveal} />
                          : <ResultSummary model={model} legend={false} />}>
        {model.selected && <LineDetails model={model} part="body" onZoom={zoom} />}
        <ProfileDock model={model} view={view} measured={measured} onCloseMeasured={closeMeasured} class="r-dock-sheet" />
        {model.selected && <ResultSummary model={model} />}
        <FilterBar model={model} />
        <LinesTable model={model} view={view} onSelect={reveal} follow={false} />
      </Sheet>}
    </div>
    {wide && <ProfileDock model={model} view={view} measured={measured} onCloseMeasured={closeMeasured} />}
  </div>;
}

export function KitResult() {
  const params = new URLSearchParams(location.search);
  const runId = params.get("run");
  const asked = params.get("theme");
  const theme: Theme = asked === "light" || asked === "dark" ? asked : storedTheme();
  const [data, setData] = useState<RunData | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { if (runId) loadRun(runId).then(setData, (e) => setError(e.message)); }, [runId]);
  return <Screen theme={theme} class="r-kit">
    {!runId ? <RunPicker /> : error ? <div class="u-stub"><p class="r-error">{error}</p>
      <a href={uiHref("kit-result")}>All runs</a></div>
      : !data ? <div class="u-stub"><p class="u-small">Loading the run…</p></div> : <Result data={data} />}
  </Screen>;
}

