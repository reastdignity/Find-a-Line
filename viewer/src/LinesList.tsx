// The list of the lines shown on the map (top right): one row per spot (with grouping) or line,
// sorted by any of their values; clicking a row opens the line on the map.

import { useState } from "preact/hooks";
import { formatValue, sortKey, sortRows, SORT_KEYS, type ListState, type Row, type SortKey } from "./list";
import { COLORS } from "./style";
import type { Line } from "./types";

const PAGE = 100;  // rows shown at first, and more each time

export interface LinesListProps {
  rows: Row[];        // what the map shows, in its order (already only those in view with state.inView)
  total: number;      // rows on the map, in view or not
  grouping: boolean;
  state: ListState;
  onState(state: ListState): void;
  onPick(line: Line): void;
  onHover(line: Line | null): void;
}

export function LinesList(props: LinesListProps) {
  const {rows, state, grouping} = props;
  const [limit, setLimit] = useState(PAGE);
  const set = (change: Partial<ListState>) => props.onState({...state, ...change});
  const key = sortKey(state.sort === "n" && !grouping ? "len" : state.sort);
  const by = (k: SortKey) => set(k.key === key.key ? {sort: k.key, desc: !state.desc} : {sort: k.key, desc: k.desc});
  const columns = ["len", "clr", "hmax"].map(sortKey);
  if (!columns.includes(key)) columns.push(key);
  if (grouping && key.key !== "n") columns.push(sortKey("n"));
  const sorted = sortRows(rows, key, state.desc);
  const count = state.inView ? `${rows.length} of ${props.total}` : String(props.total);
  return <details class="fal-list" open={state.open}
                  onToggle={(e) => { if (e.currentTarget.open !== state.open) set({open: e.currentTarget.open}); }}>
    <summary><b>Lines</b> ({count})</summary>
    <div class="fal-list-tools">
      Sort by <select class="fal-list-sort" value={key.key}
                      onChange={(e) => { const k = sortKey(e.currentTarget.value); set({sort: k.key, desc: k.desc}); }}>
        {SORT_KEYS.filter((k) => grouping || k.key !== "n").map((k) => <option value={k.key}>{k.label}</option>)}
      </select>{" "}
      <button class="fal-list-dir" title="reverse the order" onClick={() => set({desc: !state.desc})}>
        {state.desc ? "↓" : "↑"}</button><br />
      <label><input type="checkbox" class="fal-list-view" checked={state.inView}
                    onChange={(e) => set({inView: e.currentTarget.checked})} /> only in the map's view</label>
    </div>
    {sorted.length === 0 ? <div class="fal-dim">No lines{state.inView ? " in the view" : ""}.</div> :
      <div class="fal-list-body">
        <table class="fal-list-table">
          <thead><tr><th></th>{columns.map((c) => <th class={c === key ? "fal-sorted" : ""}
                                                      title={c.label + (c.unit ? ` (${c.unit.trim()})` : "")}
                                                      onClick={() => by(c)}>
            {c.head}{c === key ? (state.desc ? " ↓" : " ↑") : ""}</th>)}</tr></thead>
          <tbody>{sorted.slice(0, limit).map((r) => <tr onClick={() => props.onPick(r.line)}
                                                         onMouseEnter={() => props.onHover(r.line)}
                                                         onMouseLeave={() => props.onHover(null)}>
            <td><span class="fal-swatch" style={"background:" + (r.line.run ? r.line.run.color
              : r.line.over ? COLORS.over : COLORS.clean)}></span></td>
            {columns.map((c) => <td>{formatValue(c, r)}</td>)}
          </tr>)}</tbody>
        </table>
        {sorted.length > limit && <button class="fal-list-more" onClick={() => setLimit(limit + PAGE)}>
          show {Math.min(PAGE, sorted.length - limit)} more of {sorted.length - limit}</button>}
      </div>}
  </details>;
}
