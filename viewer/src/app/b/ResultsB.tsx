// The results of prototype B (B3, B5) on the app's one map: the run's data (loadRun), its model and
// its layer (ResultMap) on PlaceMap's Leaflet map, a pill "‹ run name" back to where the user came
// from with a switcher of runs, the run's actions, the panel on the right (summary, filters, the list
// with the selected line's details under its row) and the profile floating under the map's middle; on
// phones a top bar and a sheet whose peek is "Spot k of N" with ‹ ›. The selected line goes into the
// hash (#run=…&line=…, replaced, not pushed), so a reload or a copied link opens it again.

import type { ComponentChildren } from "preact";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import type { RunListed } from "../api";
import type { PlaceMap } from "../PlaceMap";
import { IconButton, Sheet, type Snap } from "../../ui/components";
import { Icon } from "../../ui/icons";
import type { Line, RunData } from "../../types";
import { loadRun } from "../../result/load";
import { ResultModel } from "../../result/model";
import { ResultMap, type Padding } from "../../result/ResultMap";
import { FilterBar, LineDetails, LinesTable, Popover, ProfileDock, ResultSummary, RunActions,
         useModel } from "../../result/view";
import { routeHash } from "./route";
import { runCounts } from "./Searches";

const PANEL = 384 + 12;  // the results panel's width and its margin (b.css .b-results)

export interface ResultsProps {
  place: PlaceMap;
  runId: string;
  /** The line of the route (#…&line=), opened in its group; `nav` changes with every navigation. */
  line: string | null;
  nav: number;
  wide: boolean;
  runs: RunListed[] | null;
  onBack(): void;
  onRun(runId: string): void;
  /** The phone's menu button (the top bar is the results' own). */
  menu?: ComponentChildren;
}

/** The pill: back, the run's name and date, and the other runs to switch to. */
function Pill({data, runs, onBack, onRun}: {data: RunData; runs: RunListed[] | null; onBack(): void;
                                           onRun(id: string): void}) {
  const others = (runs || []).filter((r) => r.has_map);
  return <div class="b-pill">
    <IconButton icon="back" label="Back" plain round onClick={onBack} class="b-back" />
    <span class="u-grow b-pill-name" title={data.run.name}>{data.run.name}</span>
    <span class="u-small b-pill-date">{data.run.created.slice(0, 10)}</span>
    {others.length > 1 && <Popover label="Other runs" button={(open, toggle) =>
      <IconButton icon="down" label="Switch to another run" plain round on={open} onClick={toggle} />}>
      {() => <ul class="b-switch">{others.map((r) => <li key={r.id}>
        <button type="button" class={r.id === data.run.id ? "u-on" : ""} onClick={() => onRun(r.id)}>
          <b>{r.name}</b><span class="u-small">{r.created.slice(0, 10)} · {runCounts(r)}</span></button></li>)}</ul>}
    </Popover>}
  </div>;
}

function Result({place, data, line, nav, wide, runs, onBack, onRun, menu}: ResultsProps & {data: RunData}) {
  const model = useMemo(() => new ResultModel(data, {list: {sort: "clr", desc: true}}), [data]);
  useModel(model);
  const [view, setView] = useState<ResultMap | null>(null);
  const [measured, setMeasured] = useState<Line | null>(null);
  const [snap, setSnap] = useState<Snap>("half");
  const dockRef = useRef<HTMLDivElement>(null);
  const wideRef = useRef(wide);
  wideRef.current = wide;

  /** The parts of the map the panels cover. */
  const padding = (): Padding => {
    if (!wideRef.current) return {top: 64, right: 0, bottom: Math.round(place.map.getSize().y * 0.5), left: 0};
    const dock = dockRef.current?.querySelector<HTMLElement>(".r-dock")?.offsetHeight ?? 0;
    return {top: 60, right: PANEL + 12, bottom: dock ? dock + 24 : 12, left: 12};
  };

  useEffect(() => {
    const resultMap = new ResultMap(place.map, model);
    (window as any).FINDALINE_RESULT = {model, view: resultMap};  // for tests and the browser console
    if (!line) resultMap.fitArea(padding(), false);
    setView(resultMap);
    return () => {
      resultMap.destroy();
      if ((window as any).FINDALINE_RESULT?.model === model) delete (window as any).FINDALINE_RESULT;
    };
  }, [model]);

  // The line of the route (a link, a favourite, back or forward)
  useEffect(() => {
    if (!view || !line) return;
    const opened = model.openLinked(line);
    if (opened) view.zoomTo(opened, padding(), false);
  }, [view, line, nav]);

  // The selection goes into the hash; a new one ends a measurement and shows the line on a phone's sheet
  useEffect(() => {
    let last = model.selected?.line;
    return model.subscribe(() => {
      const now = model.selected?.line;
      if (now === last) return;
      last = now;
      if (!now || !now.run) {
        const hash = routeHash({section: "run", run: model.runId, line: now ? model.lineLink(now) : null});
        if (location.hash !== hash) history.replaceState(history.state, "", hash);
      }
      if (!now) return;
      setMeasured(null);
      view?.clearMeasure();
      if (!wideRef.current) setSnap((s) => s === "full" ? "half" : s);
    });
  }, [model, view]);

  const reveal = (l: Line) => { if (view && !view.inView(l)) view.zoomTo(l, padding()); };
  const zoom = (l: Line) => view?.zoomTo(l, padding());
  const measuredNow = (l: Line | null) => { setMeasured(l); if (l && !wide) setSnap("half"); };
  const closeMeasured = () => { setMeasured(null); view?.clearMeasure(); };
  const pill = <Pill data={data} runs={runs} onBack={onBack} onRun={onRun} />;

  if (!wide) {
    return <>
      <div class="b-top b-top-run">{menu}{pill}
        <RunActions model={model} view={view} onMeasured={measuredNow} compact show={["measure", "share", "download"]} />
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
    </>;
  }
  const details = <LineDetails model={model} onZoom={zoom} onStep={reveal} />;
  return <>
    <div class="b-top-left">{pill}</div>
    <div class="b-run-actions"><RunActions model={model} view={view} onMeasured={measuredNow} /></div>
    <aside class="u-panel b-results" aria-label="Results">
      <div class="b-results-head"><ResultSummary model={model} /><FilterBar model={model} /></div>
      <div class="b-results-body"><LinesTable model={model} view={view} details={details} onSelect={reveal} /></div>
    </aside>
    <div class="b-dock" ref={dockRef}>
      <ProfileDock model={model} view={view} measured={measured} onCloseMeasured={closeMeasured} />
    </div>
  </>;
}

export function ResultsB(props: ResultsProps) {
  const [data, setData] = useState<RunData | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    setData(null);
    setError("");
    loadRun(props.runId).then((d) => { if (live) setData(d); }, (e) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [props.runId]);
  if (data && data.run.id === props.runId) return <Result key={props.runId} {...props} data={data} />;
  return <div class={props.wide ? "b-top-left" : "b-top"}>{!props.wide && props.menu}
    <div class="b-pill">
      <IconButton icon="back" label="Back" plain round onClick={props.onBack} class="b-back" />
      {error ? <span class="u-grow r-error"><Icon name="warn" /> {error}</span>
        : <span class="u-grow b-pill-name"><span class="r-spin" /> Loading the run…</span>}
    </div>
  </div>;
}
