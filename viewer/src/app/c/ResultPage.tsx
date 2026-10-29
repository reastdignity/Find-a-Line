// C's result (c3): a header (Home, the run's name and date, Edit & run again, the run's actions) and
// three columns: the summary, filters and list | the map | the selected line's details and profile.
// On phones (< 768 px) the map with a sheet holding the list and the details. The selected line is
// kept in the hash (#run=<id>&line=…, replaced, so Back leaves the result); a hash with another line
// (Back, Forward, a copied link) opens that line.

import L from "leaflet";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { Button, IconButton, Sheet, type Snap } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { baseLayers } from "../../layers";
import type { Line, Ortho, RunData } from "../../types";
import { loadRun } from "../../result/load";
import { ResultModel } from "../../result/model";
import { ResultMap, type Padding } from "../../result/ResultMap";
import { FilterBar, LineDetails, LinesTable, ProfileDock, ResultSummary, RunActions, useModel } from "../../result/view";
import { editHash, parseRoute, runHash } from "./route";
import { when } from "./Home";

/** The orthophoto's cache folder is relative to the run's map.html; the app's page is elsewhere. */
function orthoForApp(ortho: Ortho | null, runId: string): Ortho | null {
  if (!ortho || !ortho.cache) return ortho;
  const map = `${location.origin}/runs/${encodeURIComponent(runId)}/map.html`;
  return {...ortho, cache: new URL(ortho.cache, map).pathname};
}

/** A model of the run; its list sorted by clearance, largest first, unless the user chose a sorting. */
function modelOf(data: RunData): ResultModel {
  return new ResultModel(data, {list: {sort: "clr", desc: true}});
}

interface ViewProps {
  model: ResultModel;
  data: RunData;
  line: string | null;
  editJob: string | null;
}

/** The map of the result in `element`: base layers, the run's lines (ResultMap); the linked line opened. */
function useResultMap(model: ResultModel, data: RunData, padding: () => Padding) {
  const ref = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<ResultMap | null>(null);
  useEffect(() => {
    const map = L.map(ref.current!, {center: data.center, zoom: 15, maxZoom: data.max_zoom});
    (window as any).FINDALINE_MAP = map;  // for tests and the browser console
    const layers = baseLayers(orthoForApp(data.ortho, data.run.id), data.max_zoom);
    Object.values(layers)[0].addTo(map);
    L.control.layers(layers, undefined, {position: "topright"}).addTo(map);
    L.control.scale({position: "bottomleft"}).addTo(map);
    const resultMap = new ResultMap(map, model);
    (window as any).FINDALINE_RESULT = {model, view: resultMap};
    if (model.selected) resultMap.zoomTo(model.selected.line, padding(), false);
    else resultMap.fitArea(padding(), false);
    setView(resultMap);
    const observer = new ResizeObserver(() => map.invalidateSize());
    observer.observe(ref.current!);
    return () => {
      observer.disconnect();
      resultMap.destroy();
      map.remove();
    };
  }, [model]);
  return {ref, view};
}

/** The selection follows the hash's line, and the hash follows the selection. */
function useLinkedLine(model: ResultModel, line: string | null, view: ResultMap | null, padding: () => Padding) {
  useEffect(() => {
    if (!view || !line) return;
    const sel = model.selected?.line;
    if (sel && model.runOf(sel) === model.runId && model.lineLink(sel) === line) return;
    const found = model.openLinked(line);
    if (found) view.zoomTo(found, padding(), false);
  }, [line, view]);
  useEffect(() => model.subscribe(() => {
    const sel = model.selected?.line;
    const want = sel && model.runOf(sel) === model.runId ? model.lineLink(sel) : null;
    const now = parseRoute(location.hash);
    if (now.page !== "run" || now.id !== model.runId || now.line === want) return;
    if (!want && now.line) return;  // nothing selected: the link stays (e.g. a line hidden by the filters)
    history.replaceState(history.state, "", runHash(model.runId, want));
  }), [model]);
}

function Title({data}: {data: RunData}) {
  return <div class="c-run-title">
    <b class="c-run-name">{data.run.name}</b>
    <span class="u-small">{when(data.run.created)} · {data.lines.length} {data.lines.length === 1 ? "line" : "lines"}</span>
  </div>;
}

function Desktop({model, data, line, editJob}: ViewProps) {
  useModel(model);
  const [measured, setMeasured] = useState<Line | null>(null);
  const padding = (): Padding => ({top: 24, right: 24, bottom: 24, left: 24});
  const {ref, view} = useResultMap(model, data, padding);
  useLinkedLine(model, line, view, padding);
  useEffect(() => {  // a new selection ends a measurement
    let last = model.selected?.line;
    return model.subscribe(() => {
      const now = model.selected?.line;
      if (now === last) return;
      last = now;
      if (now) { setMeasured(null); view?.clearMeasure(); }
    });
  }, [model, view]);
  const reveal = (l: Line) => { if (view && !view.inView(l)) view.zoomTo(l, padding()); };
  const zoom = (l: Line) => view?.zoomTo(l, padding());
  const closeMeasured = () => { setMeasured(null); view?.clearMeasure(); };

  return <div class="c-result">
    <header class="c-subtop c-result-top">
      <Button kind="ghost" icon="back" href="#home">Home</Button>
      <Title data={data} />
      <span class="u-grow" />
      {editJob && <Button icon="edit" href={editHash(editJob)}>Edit &amp; run again</Button>}
      <RunActions model={model} view={view} onMeasured={setMeasured} />
    </header>
    <div class="c-cols">
      <aside class="c-col-list" aria-label="Lines">
        <div class="c-col-head"><ResultSummary model={model} /><FilterBar model={model} /></div>
        <div class="c-col-body"><LinesTable model={model} view={view} onSelect={reveal} /></div>
      </aside>
      <div class="c-col-map"><div class="c-leaflet" ref={ref} /></div>
      <aside class="c-col-details" aria-label="The selected line">
        {model.selected ? <LineDetails model={model} onZoom={zoom} onStep={reveal} />
          : !measured && <p class="c-pick"><Icon name="info" />Select a line on the map or in the list.</p>}
        <ProfileDock model={model} view={view} measured={measured} onCloseMeasured={closeMeasured} class="c-dock" />
      </aside>
    </div>
  </div>;
}

function Phone({model, data, line, editJob}: ViewProps) {
  useModel(model);
  const [measured, setMeasured] = useState<Line | null>(null);
  const [snap, setSnap] = useState<Snap>("half");
  const padding = (): Padding => ({top: 64, right: 0, bottom: Math.round(innerHeight * 0.5), left: 0});
  const {ref, view} = useResultMap(model, data, padding);
  useLinkedLine(model, line, view, padding);
  useEffect(() => {  // a new selection ends a measurement and shows the line in the sheet
    let last = model.selected?.line;
    return model.subscribe(() => {
      const now = model.selected?.line;
      if (now === last) return;
      last = now;
      if (!now) return;
      setMeasured(null);
      view?.clearMeasure();
      setSnap((s) => s === "full" ? "half" : s);
    });
  }, [model, view]);
  const reveal = (l: Line) => { if (view && !view.inView(l)) view.zoomTo(l, padding()); };
  const zoom = (l: Line) => view?.zoomTo(l, padding());
  const measuredNow = (l: Line | null) => { setMeasured(l); if (l) setSnap("half"); };
  const closeMeasured = () => { setMeasured(null); view?.clearMeasure(); };

  return <div class="c-result c-result-phone">
    <div class="c-col-map"><div class="c-leaflet" ref={ref} /></div>
    <div class="c-phone-top">
      <div class="c-pill">
        <a class="u-ib u-plain u-round" href="#home" aria-label="Home" title="Home"><Icon name="back" /></a>
        <span class="u-grow c-pill-name">{data.run.name}</span>
        {editJob && <a class="u-ib u-plain u-round" href={editHash(editJob)} aria-label="Edit & run again"
                       title="Edit & run again"><Icon name="edit" /></a>}
      </div>
      <RunActions model={model} view={view} onMeasured={measuredNow} compact />
    </div>
    <Sheet label="Results" desktop="sheet" snap={snap} onSnap={setSnap} peek={132}
           header={model.selected ? <LineDetails model={model} part="head" onStep={reveal} />
             : <ResultSummary model={model} legend={false} />}>
      {model.selected && <LineDetails model={model} part="body" onZoom={zoom} />}
      <ProfileDock model={model} view={view} measured={measured} onCloseMeasured={closeMeasured} class="r-dock-sheet" />
      {model.selected && <ResultSummary model={model} />}
      <FilterBar model={model} />
      <LinesTable model={model} view={view} onSelect={reveal} follow={false} />
    </Sheet>
  </div>;
}

export function ResultPage({id, line, editJob, wide}: {id: string; line: string | null; editJob: string | null; wide: boolean}) {
  const [data, setData] = useState<RunData | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setData(null);
    setError("");
    loadRun(id).then(setData, (e) => setError(e.message));
  }, [id]);
  const model = useMemo(() => data && modelOf(data), [data]);
  if (error) return <div class="c-page"><Button kind="ghost" icon="back" href="#home">Home</Button>
    <p class="c-error">{error}</p></div>;
  if (!data || !model) return <div class="c-page"><p class="u-small">Loading the run…</p></div>;
  const props = {model, data, line, editJob};
  return wide ? <Desktop key="wide" {...props} /> : <Phone key="phone" {...props} />;
}
