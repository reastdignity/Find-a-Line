// A finished run on a full-screen map, as prototype B shows it and D reuses: the run's data (loadRun),
// its model and its layer (ResultMap) on the caller's Leaflet map, a pill "‹ run name" with the
// caller's back action (and, in B, a switcher of runs), the run's actions with the caller's extra ones,
// the panel on the right (summary, filters, the list with the selected line's details under its row)
// and the profile docked at the bottom; on phones a top bar and a sheet whose peek is "Spot k of N"
// with ‹ ›. The selected line goes into the hash (the caller's `hashOf`, replaced, not pushed), so a
// reload or a copied link opens it again. Styles in workspace.css (the class names are B's: b-*).

import type { ComponentChildren } from "preact";
import type L from "leaflet";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import type { RunListed } from "../../app/api";
import { IconButton, Sheet, type Snap } from "../../ui/components";
import { Icon } from "../../ui/icons";
import type { Line, RunData } from "../../types";
import { loadRun } from "../load";
import { ResultModel } from "../model";
import { ResultMap, type Padding } from "../ResultMap";
import { Popover, useModel } from "./common";
import { FilterBar } from "./FilterBar";
import { LineDetails } from "./LineDetails";
import { LinesTable } from "./LinesTable";
import { ProfileDock } from "./ProfileDock";
import { ResultSummary } from "./ResultSummary";
import { RunActions } from "./RunActions";
import "./workspace.css";

const PANEL = 384 + 12;  // the results panel's width and its margin (workspace.css .b-results)

/** The pill's way back: its label (the accessible name; shown next to the arrow with `text`). */
export interface BackAction { label: string; onClick(): void; text?: boolean }

export interface ResultWorkspaceProps {
  /** The caller's Leaflet map; the workspace adds the run's layer and removes it when it goes. */
  map: L.Map;
  runId: string;
  /** The line of the route (#…&line=), opened in its group; `nav` changes with every navigation. */
  line: string | null;
  nav: number;
  wide: boolean;
  back: BackAction;
  /** The route's hash of the run with a selected line (its ResultModel.lineLink) or none. */
  hashOf(runId: string, line: string | null): string;
  /** The link "Copy link" gives (default: this page's address with the hash, LineDetails' appLink). */
  linkOf?(model: ResultModel, line: Line): string;
  /** The runs to switch to (the pill's menu, shown with two or more runs that have a map). */
  runs?: RunListed[] | null;
  onRun?(runId: string): void;
  /** Buttons before the run's actions (e.g. "Edit & run again"); `compact` on phones (icons only). */
  actions?(compact: boolean): ComponentChildren;
  /** The phone's menu button (the top bar is the results' own). */
  menu?: ComponentChildren;
  /** The run's data was loaded (e.g. for the map's base layers). */
  onData?(data: RunData): void;
}

function Back({back, wide}: {back: BackAction; wide: boolean}) {
  if (back.text && wide) return <button type="button" class="u-btn u-ghost b-back b-back-text" aria-label={back.label}
                                        onClick={back.onClick}><Icon name="back" />{back.label}</button>;
  return <IconButton icon="back" label={back.label} plain round onClick={back.onClick} class="b-back" />;
}

/** The pill: back, the run's name and date, and the other runs to switch to. */
function Pill({data, runs, back, wide, onRun}: {data: RunData; runs?: RunListed[] | null; back: BackAction; wide: boolean;
                                                onRun?(id: string): void}) {
  const others = (runs || []).filter((r) => r.has_map);
  const [switching, setSwitching] = useState(false);
  return <div class="b-pill">
    <Back back={back} wide={wide} />
    <span class="u-grow b-pill-name" title={data.run.name}>{data.run.name}</span>
    <span class="u-small b-pill-date">{data.run.created.slice(0, 10)}</span>
    {onRun && others.length > 1 && <Popover label="Other runs" button={(open, toggle) =>
      <IconButton icon="down" label="Switch to another run" plain round expanded={open} onClick={toggle} />}
      open={switching} onOpen={setSwitching}>
      {() => <ul class="b-switch">{others.map((r) => <li key={r.id}>
        <button type="button" class={r.id === data.run.id ? "u-on" : ""}
                onClick={() => { setSwitching(false); if (r.id !== data.run.id) onRun(r.id); }}>
          <b>{r.name}</b><span class="u-small">{r.created.slice(0, 10)} · {runCounts(r)}</span></button></li>)}</ul>}
    </Popover>}
  </div>;
}

/** "345 lines in 12 spots" of a listed run (B's cards and the pill's switcher). */
export function runCounts(run: RunListed): string {
  const {lines, spots} = run.summary;
  return `${lines} ${lines === 1 ? "line" : "lines"} in ${spots} ${spots === 1 ? "spot" : "spots"}`;
}

function Result({map, data, line, nav, wide, back, hashOf, linkOf, runs, onRun, actions, menu}:
                ResultWorkspaceProps & {data: RunData}) {
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
    if (!wideRef.current) return {top: 64, right: 0, bottom: Math.round(map.getSize().y * 0.5), left: 0};
    const dock = dockRef.current?.querySelector<HTMLElement>(".r-dock")?.offsetHeight ?? 0;
    return {top: 60, right: PANEL + 12, bottom: dock ? dock + 24 : 12, left: 12};
  };

  useEffect(() => {
    const resultMap = new ResultMap(map, model);
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
        const hash = hashOf(model.runId, now ? model.lineLink(now) : null);
        if (location.hash !== hash) history.replaceState(history.state, "", hash);
      }
      if (!now) return;
      setMeasured(null);
      view?.clearMeasure();
      if (!wideRef.current) setSnap((s) => s === "full" ? "half" : s);
    });
  }, [model, view]);

  // Measuring on a phone: the sheet goes down, the map is where the points are clicked
  useEffect(() => view?.onMeasuring((on) => { if (on && !wideRef.current) setSnap("peek"); }), [view]);

  const reveal = (l: Line) => { if (view && !view.inView(l)) view.zoomTo(l, padding()); };
  const zoom = (l: Line) => view?.zoomTo(l, padding());
  const measuredNow = (l: Line | null) => { setMeasured(l); if (l && !wide) setSnap("half"); };
  const closeMeasured = () => { setMeasured(null); view?.clearMeasure(); };
  const link = linkOf && ((l: Line) => linkOf(model, l));
  const pill = <Pill data={data} runs={runs} back={back} wide={wide} onRun={onRun} />;

  if (!wide) {
    return <div class="r-ws">
      <div class="b-top b-top-run">{menu}{pill}{actions?.(true)}
        <RunActions model={model} view={view} onMeasured={measuredNow} compact show={["measure", "share", "download"]} />
      </div>
      <Sheet label="Results" desktop="sheet" snap={snap} onSnap={setSnap} peek={132} top={64}
             header={model.selected ? <LineDetails model={model} part="head" onStep={reveal} />
               : <ResultSummary model={model} legend={false} />}>
        {model.selected && <LineDetails model={model} part="body" onZoom={zoom} linkOf={link} />}
        <ProfileDock model={model} view={view} measured={measured} onCloseMeasured={closeMeasured} class="r-dock-sheet" />
        {model.selected && <ResultSummary model={model} />}
        <FilterBar model={model} />
        <LinesTable model={model} view={view} onSelect={reveal} follow={false} />
      </Sheet>
    </div>;
  }
  const details = <LineDetails model={model} onZoom={zoom} onStep={reveal} linkOf={link} />;
  return <div class="r-ws">
    <div class="b-top-left">{pill}</div>
    <div class="b-run-actions">{actions?.(false)}<RunActions model={model} view={view} onMeasured={measuredNow} /></div>
    <aside class="u-panel b-results" aria-label="Results">
      <div class="b-results-head"><ResultSummary model={model} /><FilterBar model={model} /></div>
      <div class="b-results-body"><LinesTable model={model} view={view} details={details} onSelect={reveal} /></div>
    </aside>
    <div class="b-dock" ref={dockRef}>
      <ProfileDock model={model} view={view} measured={measured} onCloseMeasured={closeMeasured} />
    </div>
  </div>;
}

/** A run's result on the caller's map: loaded first (the pill says so meanwhile). */
export function ResultWorkspace(props: ResultWorkspaceProps) {
  const [data, setData] = useState<RunData | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    setData(null);
    setError("");
    loadRun(props.runId).then((d) => { if (!live) return; props.onData?.(d); setData(d); },
                              (e) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [props.runId]);
  if (data && data.run.id === props.runId) return <Result key={props.runId} {...props} data={data} />;
  return <div class="r-ws"><div class={props.wide ? "b-top-left" : "b-top"}>{!props.wide && props.menu}
    <div class="b-pill">
      <Back back={props.back} wide={props.wide} />
      {error ? <span class="u-grow r-error"><Icon name="warn" /> {error}</span>
        : <span class="u-grow b-pill-name"><span class="r-spin" /> Loading the run…</span>}
    </div>
  </div></div>;
}
