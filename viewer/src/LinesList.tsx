// The list of the lines shown on the map (top right): one row per spot (with grouping) or line,
// sorted by any of their values; clicking a row opens the line on the map, and the row of a group
// unfolds into its lines, any of which can be shown instead.

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
  keyOf(l: Line): string;
  note: {text: string; action?: string; onAction?(): void} | null;  // shown above the list
  onState(state: ListState): void;
  onPick(line: Line): void;               // a row: open its line
  onChoose(row: Row, line: Line): void;   // a line of a group: show it for the group and open it
  onHover(line: Line | null): void;
}

export function LinesList(props: LinesListProps) {
  const {rows, state, grouping, keyOf} = props;
  const [limit, setLimit] = useState(PAGE);
  const [unfolded, setUnfolded] = useState<Set<string>>(new Set());  // keys of the groups' first lines
  const set = (change: Partial<ListState>) => props.onState({...state, ...change});
  const key = sortKey(state.sort === "n" && !grouping ? "len" : state.sort);
  const by = (k: SortKey) => set(k.key === key.key ? {sort: k.key, desc: !state.desc} : {sort: k.key, desc: k.desc});
  const columns = ["len", "clr", "hmax"].map(sortKey);
  if (!columns.includes(key)) columns.push(key);
  if (grouping && key.key !== "n") columns.push(sortKey("n"));
  const sorted = sortRows(rows, key, state.desc);
  const count = state.inView ? `${rows.length} of ${props.total}` : String(props.total);
  const swatch = (l: Line) => <span class="fal-swatch" style={"background:" + (l.run ? l.run.color
    : l.over ? COLORS.over : COLORS.clean)}></span>;
  const hover = (l: Line) => ({onMouseEnter: () => props.onHover(l), onMouseLeave: () => props.onHover(null)});
  const row = (r: Row) => {
    const members = r.members && r.members.length > 1 ? r.members : null;
    const group = members ? keyOf(members[0]) : "", open = members !== null && unfolded.has(group);
    const toggle = (e: Event) => {
      e.stopPropagation();
      const next = new Set(unfolded);
      if (open) next.delete(group); else next.add(group);
      setUnfolded(next);
    };
    return [
      <tr key={group || keyOf(r.line)} class={open ? "fal-unfolded" : ""} onClick={() => props.onPick(r.line)}
          {...hover(r.line)}>
        <td class="fal-list-first">
          {members ? <button class="fal-list-unfold" title={open ? "fold the group" : "the lines of the group"}
                             onClick={toggle}>{open ? "▾" : "▸"}</button> : <span class="fal-list-gap"></span>}
          {swatch(r.line)}</td>
        {columns.map((c) => <td>{formatValue(c, r)}</td>)}
      </tr>,
      ...(open ? sortRows(members!.map((m) => ({line: m, n: r.n})), key, state.desc).map((m) =>
        <tr key={keyOf(m.line)} class={"fal-member" + (m.line === r.line ? " fal-chosen" : "")}
            title={m.line === members![0] ? "the group's first line" : "show this line for the group"}
            onClick={() => props.onChoose(r, m.line)} {...hover(m.line)}>
          <td class="fal-list-first">{m.line === r.line ? "●" : ""}</td>
          {columns.map((c) => <td>{c.key === "n" ? "" : formatValue(c, m)}</td>)}
        </tr>) : []),
    ];
  };
  return <div>
    {props.note && <div class="fal-list-note">{props.note.text}{" "}
      {props.note.action && <button onClick={props.note.onAction}>{props.note.action}</button>}</div>}
    <details class="fal-list" open={state.open}
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
            <tbody>{sorted.slice(0, limit).map(row)}</tbody>
          </table>
          {sorted.length > limit && <button class="fal-list-more" onClick={() => setLimit(limit + PAGE)}>
            show {Math.min(PAGE, sorted.length - limit)} more of {sorted.length - limit}</button>}
        </div>}
    </details>
  </div>;
}
