// The panels on the map: grouping of lines (bottom left) and the run (top left).

import L from "leaflet";
import { render, type ComponentChild } from "preact";
import { useState } from "preact/hooks";
import { active, NO_FILTERS, type Filters, type Range } from "./filters";
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
  filters: Filters;
  ranges: FilterRanges;
  onChange(state: LinesState): void;
  onFilters(filters: Filters): void;
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
    <FiltersPanel filters={props.filters} ranges={props.ranges} grouping={state.group} onChange={props.onFilters} />
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
    <b>Run</b> · <a href="../index.html">all runs</a>
    {location.protocol.startsWith("http") && <span> · <a href="/">app</a></span>}<br />
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

export interface FilterRanges {
  len: Range | null;
  clr: Range | null;
  clrN: Range | null;
  dh: Range | null;
  elev: Range | null;
  hmax: Range | null;
  hmean: Range | null;
  highPct: Range | null;
  highAbove: number;  // m, the height of highPct
  angle: Range | null;
  prom: Range | null;
  drop: Range | null;
  group: Range | null;
  water: boolean;   // some lines are over water
  filled: boolean;  // some lines are over filled gaps
}

/** A limit: a slider over the range of the lines and a number field; at the end of the range, no limit. */
function Limit({label, value, range, step, kind, unit, onChange}: {
  label: string; value: number | null; range: Range; step: number; kind: "min" | "max"; unit: string;
  onChange(v: number | null): void;
}) {
  const [draft, setDraft] = useState<number | null>(null);  // while the slider is dragged
  const low = Math.floor(range.min / step) * step, high = Math.ceil(range.max / step) * step;
  const end = kind === "min" ? low : high;
  const shown = draft ?? value ?? end;
  const commit = (v: number) => {
    setDraft(null);
    onChange(!isFinite(v) || (kind === "min" ? v <= low : v >= high) ? null : v);
  };
  const digits = step < 1 ? 1 : 0;
  return <div class="fal-limit">
    <span class={value === null ? "" : "fal-set"}>{label}</span>
    <input type="range" min={low} max={high} step={step} value={shown}
           onInput={(e) => setDraft(+e.currentTarget.value)} onChange={(e) => commit(+e.currentTarget.value)} />
    <input type="number" key={String(shown)} step={step} placeholder={end.toFixed(digits)}
           defaultValue={value === null && draft === null ? "" : shown.toFixed(digits)}
           onChange={(e) => e.currentTarget.value.trim() === "" ? onChange(null) : commit(+e.currentTarget.value)} />
    {unit}
  </div>;
}

/** The filters (filters.ts): lines that do not pass are left out before grouping. */
export function FiltersPanel({filters, ranges, grouping, onChange}: {
  filters: Filters; ranges: FilterRanges; grouping: boolean; onChange(f: Filters): void;
}) {
  const set = (change: Partial<Filters>) => onChange({...filters, ...change});
  const n = active(filters);
  const limit = (label: string, key: keyof Filters, range: Range | null, step: number, kind: "min" | "max",
                 unit = " m") => range && <Limit label={label} value={filters[key] as number | null} range={range}
                                                  step={step} kind={kind} unit={unit}
                                                  onChange={(v) => set({[key]: v} as Partial<Filters>)} />;
  return <details class="fal-filters" open={n > 0}>
    <summary>Filters{n > 0 && <b> ({n} active)</b>}
      {n > 0 && <button class="fal-reset" onClick={(e) => { e.preventDefault(); onChange({...NO_FILTERS}); }}>
        clear</button>}</summary>
    {limit("length from", "lenMin", ranges.len, 1, "min")}
    {limit("length to", "lenMax", ranges.len, 1, "max")}
    {limit("clearance at least", "clrMin", ranges.clr, 0.1, "min")}
    {limit("over trees at least", "clrNMin", ranges.clrN, 0.1, "min")}
    {limit("anchor Δh at most", "dhMax", ranges.dh, 0.5, "max")}
    {limit("lower anchor at least", "elevMin", ranges.elev, 1, "min", " m a.s.l.")}
    {limit("height above ground at least", "heightMin", ranges.hmax, 1, "min")}
    {limit("mean height at least", "meanMin", ranges.hmean, 1, "min")}
    {limit(`${ranges.highAbove} m above ground at least`, "highPctMin", ranges.highPct, 1, "min", " % of the line")}
    {limit("angle to the wall at most", "angleMax", ranges.angle, 1, "max", "°")}
    {limit("an anchor prominent at least", "promMin", ranges.prom, 0.5, "min")}
    {limit("both anchors prominent at least", "promBothMin", ranges.prom, 0.5, "min")}
    {limit("drop below both anchors at least", "dropMin", ranges.drop, 1, "min")}
    {grouping && limit("lines in group at least", "groupMin", ranges.group, 1, "min", "")}
    {ranges.water && <div class="fal-limit"><span class={filters.water === "all" ? "" : "fal-set"}>water</span>
      <select value={filters.water} onChange={(e) => set({water: e.currentTarget.value as Filters["water"]})}>
        <option value="all">all lines</option>
        <option value="water">only over water</option>
        <option value="land">only over land</option>
      </select></div>}
    {ranges.filled && <label class="fal-limit"><input type="checkbox" checked={filters.hideFilled}
                                                       onChange={(e) => set({hideFilled: e.currentTarget.checked})} />
      hide lines over filled gaps in the data</label>}
  </details>;
}
