// The panels on the map: grouping of lines (bottom left) and the run (top left).

import L from "leaflet";
import { render, type ComponentChild } from "preact";
import { COLORS } from "./style";
import type { RunInfo } from "./types";

/** A Leaflet control whose content is rendered by Preact; returns the function that renders it. */
export function control(map: L.Map, position: L.ControlPosition, className: string) {
  const div = L.DomUtil.create("div", className);
  L.DomEvent.disableClickPropagation(div);
  L.DomEvent.disableScrollPropagation(div);
  const Control = L.Control.extend({options: {position}, onAdd: () => div});
  new Control().addTo(map);
  return (content: ComponentChild) => render(content, div);
}

export interface LinesState {
  group: boolean;
  distance: number;
  by: string;
  showClean: boolean;
  showOver: boolean;
}

export interface LinesPanelProps {
  state: LinesState;
  maxDistance: number;
  nClean: number;
  nOver: number;
  shown: {spots: number; lines: number};
  focused: boolean;            // one line is focused: offer "Show all lines"
  onChange(state: LinesState): void;
  onShowAll(): void;
}

export function LinesPanel(props: LinesPanelProps) {
  const {state, nClean, nOver, shown} = props;
  const set = (change: Partial<LinesState>) => props.onChange({...state, ...change});
  return <div>
    <div class="fal-summary">{state.group
      ? <span><b>{shown.spots}</b> spots ({shown.lines} lines)</span>
      : <span><b>{shown.lines}</b> lines</span>}</div>
    {props.focused && <button class="fal-show-all" onClick={props.onShowAll}>Show all lines</button>}
    <label><input type="checkbox" class="fal-group" checked={state.group}
                  onChange={(e) => set({group: e.currentTarget.checked})} /> Group similar lines</label>
    <div class={"fal-opts" + (state.group ? "" : " fal-dim")}>
      Anchors within <DistanceSlider value={state.distance} max={props.maxDistance}
                                     onChange={(distance) => set({distance})} /><br />
      Show per group: <label><input type="radio" name="fal-by" value="clearance" checked={state.by === "clearance"}
                                    onChange={() => set({by: "clearance"})} /> largest clearance</label>{" "}
      <label><input type="radio" name="fal-by" value="longest" checked={state.by === "longest"}
                    onChange={() => set({by: "longest"})} /> longest</label>
    </div>
    {nOver > 0 && <div>
      <label><input type="checkbox" class="fal-clean" checked={state.showClean}
                    onChange={(e) => set({showClean: e.currentTarget.checked})} />{" "}
        <span class="fal-swatch" style={"background:" + COLORS.clean}></span> Clear lines ({nClean})</label><br />
      <label><input type="checkbox" class="fal-over" checked={state.showOver}
                    onChange={(e) => set({showOver: e.currentTarget.checked})} />{" "}
        <span class="fal-swatch" style={"background:" + COLORS.over}></span> Over trees/buildings ({nOver})</label>
    </div>}
  </div>;
}

/** The slider shows the value while dragging and regroups when it is released. */
function DistanceSlider({value, max, onChange}: {value: number; max: number; onChange(v: number): void}) {
  return <span>
    <input type="range" class="fal-distance" min={5} max={max} step={1} value={value}
           onInput={(e) => {
             const label = e.currentTarget.nextElementSibling;
             if (label) label.textContent = e.currentTarget.value + " m";
           }}
           onChange={(e) => onChange(+e.currentTarget.value)} />{" "}
    <span class="fal-distance-label">{value + " m"}</span>
  </span>;
}

/** The run's parameters and a list of all saved runs (runs/runs.js; without it, this run only). */
export function RunPanel({current, runs}: {current: RunInfo; runs: RunInfo[]}) {
  const label = (r: RunInfo) => r.created.slice(0, 16).replace("T", " ") + " - " + r.name;
  return <div>
    <b>Run</b> · <a href="../index.html">all runs</a><br />
    <select onChange={(e) => { location.href = "../" + encodeURIComponent(e.currentTarget.value) + "/map.html"; }}>
      {runs.map((r) => <option value={r.id} selected={r.id === current.id}>{label(r)}</option>)}
    </select>
    <div class="fal-params">{current.description}</div>
  </div>;
}

export interface OtherItem {
  run: RunInfo;
  km: number;
  color: string;
  status: "off" | "loading" | "on" | "error";
  lines?: number;
}

/** "Other runs": a checkbox per saved run, nearby ones first, the rest folded. */
export function OtherRunsPanel({items, nearbyKm, onToggle}:
                               {items: OtherItem[]; nearbyKm: number; onToggle(id: string): void}) {
  if (!items.length) return null;
  const row = (item: OtherItem) => <label class="fal-other" title={item.run.description}>
    <input type="checkbox" checked={item.status === "on" || item.status === "loading"}
           onChange={() => onToggle(item.run.id)} />{" "}
    <span class="fal-swatch" style={"background:" + item.color}></span>{" "}
    {item.run.created.slice(0, 10)} {item.run.name}
    <span class="fal-dim"> {item.km < 1 ? "here" : item.km.toFixed(0) + " km"}
      {item.status === "loading" ? ", loading…" : item.status === "error" ? ", no lines.js" :
        item.lines !== undefined && item.status === "on" ? ", " + item.lines + " lines" : ""}</span>
    {" "}<a href={"../" + encodeURIComponent(item.run.id) + "/map.html"} title="Open its map">↗</a>
  </label>;
  const near = items.filter((i) => i.km <= nearbyKm), far = items.filter((i) => i.km > nearbyKm);
  return <div class="fal-others">
    <b>Other runs</b>
    {near.map(row)}
    {far.length > 0 && <details open={far.some((i) => i.status !== "off")}>
      <summary>More runs ({far.length})</summary>
      {far.map(row)}
    </details>}
  </div>;
}
