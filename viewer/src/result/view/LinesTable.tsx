// The sortable list of the spots of a result (model.rows): length, clearance, height, lines in the
// spot and the favourite star; the selected row highlighted with the shell's details under it, a
// spot's lines unfolded under its row (the shown one marked), "only in the map's view" (the selected
// spot stays, marked outside the view), the first 100 rows and then more. Rows take the focus; Enter
// selects. Sorting is model.list (list.ts).

import type { ComponentChildren } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import { Icon } from "../../ui/icons";
import { useMedia } from "../../ui/components";
import type { Group } from "../../grouping";
import { formatValue, sortKey, SORT_KEYS, type SortKey } from "../../list";
import type { Line } from "../../types";
import type { ResultModel, Spot } from "../model";
import type { ResultMap } from "../ResultMap";
import { cx, lineColor, num, Swatch, useModel } from "./common";

const PAGE = 100;
const COLUMNS: {key: string; head: string; title: string}[] = [
  {key: "len", head: "Length", title: "length of the line"},
  {key: "clr", head: "Clear.", title: "smallest clearance under the webbing"},
  {key: "hmax", head: "Height", title: "highest point of the webbing above the ground"},
  {key: "n", head: "Lines", title: "similar lines in the spot"},
];

export interface LinesTableProps {
  model: ResultModel;
  /** The map layer: hover highlights and "only in the map's view" (hidden without it). */
  view?: ResultMap | null;
  /** Shown right under the selected row (e.g. <LineDetails>). */
  details?: ComponentChildren;
  /** A row or a line of a spot was selected by the user (e.g. to bring it into the map's view). */
  onSelect?(line: Line): void;
  /** Rows shown before "show more" (default 100). */
  page?: number;
  /** Scroll the list to the selected row when the selection changes (default true). */
  follow?: boolean;
}

/** Scroll the nearest scrolling ancestor of a row so that the row is in it (not scrollIntoView, which
 *  would scroll every ancestor, the map's too). */
function scrollToRow(row: HTMLElement) {
  for (let box = row.parentElement; box; box = box.parentElement) {
    const overflow = getComputedStyle(box).overflowY;
    if ((overflow === "auto" || overflow === "scroll") && box.scrollHeight > box.clientHeight) {
      const r = row.getBoundingClientRect(), b = box.getBoundingClientRect();
      const head = box.querySelector<HTMLElement>(".r-head")?.offsetHeight || 0;  // the sticky heading
      if (r.top < b.top + head) box.scrollTop -= b.top + head - r.top;
      else if (r.bottom > b.bottom) box.scrollTop += Math.min(r.bottom - b.bottom, r.top - b.top - head);
      return;
    }
  }
}

export function LinesTable({model, view, details, onSelect, page = PAGE, follow = true}: LinesTableProps) {
  useModel(model);
  const [limit, setLimit] = useState(page);
  // The unfolded spot by the key of its first line: compute() makes new groups on every filter or star
  const [unfolded, setUnfolded] = useState<string | null>(null);
  const hovered = useRef<Line | null>(null);  // the line highlighted on the map by the pointer
  const [, setMoved] = useState(0);
  const canHover = useMedia("(hover: hover)");
  const listRef = useRef<HTMLDivElement>(null);
  const list = model.list, inView = list.inView && !!view;

  useEffect(() => view ? view.onMove(() => setMoved((n) => n + 1)) : undefined, [view]);

  const selected = model.selected;
  const rows: Spot[] = inView ? model.rows.filter((r) => view!.inView(r.line) || r.group === selected?.group)
    : model.rows;
  const at = selected ? rows.findIndex((r) => r.group === selected.group) : -1;
  // The selected spot is always among the rows shown
  useEffect(() => { if (at >= limit) setLimit(Math.ceil((at + 1) / page) * page); }, [at]);
  useEffect(() => {
    const row = listRef.current?.querySelector<HTMLElement>(".r-row.r-sel");
    if (row && follow) scrollToRow(row);
  }, [selected?.line, at < limit]);  // again when "show more" brings it in

  const key = sortKey(list.sort);
  const sortBy = (k: string) => {
    const known = sortKey(k);
    model.setList({...list, sort: known.key, desc: list.sort === known.key ? !list.desc : known.desc});
  };
  /** A row: selects its spot, or unselects it when it is selected; a line of a spot: selects it as the spot's. */
  const pick = (target: Group<Line> | Line) => {
    if ("members" in target && selected?.group === target) { model.select(null); return; }
    model.select(target);
    if (model.selected) onSelect?.(model.selected.line);
  };
  const hover = (l: Line | null) => {
    if (!canHover) return;
    hovered.current = l;
    view?.highlight(l);
  };
  // A row that goes (a filter, a star with "only favourites") never sees the pointer leave: its line
  // would stay highlighted
  useEffect(() => {
    const l = hovered.current;
    if (l && !rows.slice(0, limit).some((r) => r.line === l || (r.members || []).includes(l))) hover(null);
  });
  const keys = (e: KeyboardEvent, target: Group<Line> | Line) => {
    const el = e.currentTarget as HTMLElement;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(target); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const all = Array.from(listRef.current?.querySelectorAll<HTMLElement>(".r-row[tabindex]") || []);
      all[all.indexOf(el) + (e.key === "ArrowDown" ? 1 : -1)]?.focus();
    }
  };
  const star = (l: Line) => model.favouritesOn
    ? <button type="button" class={cx("r-star", model.isFavourite(l) && "r-on")}
              aria-label={model.isFavourite(l) ? "Remove from favourites" : "Add to favourites"}
              aria-pressed={model.isFavourite(l)} title={model.isFavourite(l) ? "favourite" : "add to favourites"}
              onClick={(e) => { e.stopPropagation(); model.toggleFavourite(l); }}>
        <Icon name="star" fav={model.isFavourite(l)} /></button>
    : <span />;
  const cells = (l: Line) => <>
    <Swatch color={lineColor(l)} />
    <span>{num(l.len)} m</span><span>{num(l.clr, 1)} m</span><span>{isFinite(l.hmax) ? num(l.hmax) + " m" : "–"}</span>
  </>;

  return <div class="r-table" ref={listRef}>
    <div class="r-table-tools">
      <label class="r-sortby"><span class="u-small">Sort by</span>
        <select class="r-sort" value={key.key} onChange={(e) => {
          const k = sortKey(e.currentTarget.value);
          model.setList({...list, sort: k.key, desc: k.desc});
        }}>{SORT_KEYS.map((k: SortKey) => <option key={k.key} value={k.key}>{k.label}</option>)}</select></label>
      <button type="button" class="r-dir" aria-label={list.desc ? "Largest first" : "Smallest first"}
              title={list.desc ? "largest first" : "smallest first"}
              onClick={() => model.setList({...list, desc: !list.desc})}>{list.desc ? "↓" : "↑"}</button>
      <span class="u-grow" />
      {view && <label class="r-check r-inview"><input type="checkbox" checked={list.inView}
                                                     onChange={(e) => model.setList({...list, inView: e.currentTarget.checked})} />
        Only in the map's view</label>}
    </div>
    <div class="r-row r-head" role="presentation">
      <span />
      {COLUMNS.map((c) => <button type="button" key={c.key} title={c.title}
                                 class={cx("r-col", key.key === c.key && "r-on")}
                                 aria-sort={key.key === c.key ? (list.desc ? "descending" : "ascending") : undefined}
                                 onClick={() => sortBy(c.key)}>
        {c.head}{key.key === c.key && (list.desc ? " ↓" : " ↑")}</button>)}
      <span />
    </div>
    <div class="r-rows" role="listbox" aria-label="Lines">
      {rows.length === 0 && <p class="u-small r-empty">{inView ? "No lines in the map's view." : "No lines pass the filters."}</p>}
      {rows.slice(0, limit).map((r) => {
        const isSel = selected?.group === r.group, open = unfolded === model.keyOf(r.group.rep) && r.n > 1;
        const outside = inView && isSel && !view!.inView(r.line);
        return <div class={cx("r-spot", isSel && "r-spot-sel")} key={model.keyOf(r.group.rep)}>
          {outside && <p class="u-small r-outside">Selected, outside the map's view</p>}
          <div class={cx("r-row", isSel && "r-sel", r.line.over && "r-over")} role="option" aria-selected={isSel}
               tabIndex={0} onClick={() => pick(r.group)} onKeyDown={(e) => keys(e, r.group)}
               onPointerEnter={() => hover(r.line)} onPointerLeave={() => hover(null)}>
            {cells(r.line)}
            {r.n > 1 ? <button type="button" class={cx("r-unfold", open && "r-on")} aria-expanded={open}
                               aria-label={`${r.n} similar lines: ${open ? "fold" : "unfold"}`}
                               onClick={(e) => { e.stopPropagation(); setUnfolded(open ? null : model.keyOf(r.group.rep)); }}>
                {formatValue(sortKey("n"), r)}<Icon name="down" /></button>
              : <span class="r-muted">1</span>}
            {star(r.line)}
          </div>
          {open && r.members!.map((m, k) => {
            const shown = m === r.line;
            return <div key={k} class={cx("r-row r-member", shown && "r-shown", isSel && shown && "r-sel")}
                        role="option" aria-selected={isSel && shown} tabIndex={0}
                        title={k === 0 ? "the line the spot shows first" : undefined}
                        onClick={() => pick(m)} onKeyDown={(e) => keys(e, m)}
                        onPointerEnter={() => hover(m)} onPointerLeave={() => hover(null)}>
              {cells(m)}
              <span class="r-muted">{shown ? "shown" : k === 0 ? "first" : ""}</span>
              {star(m)}
            </div>;
          })}
          {isSel && details && <div class="r-details-slot">{details}</div>}
        </div>;
      })}
      {rows.length > limit && <button type="button" class="u-btn u-ghost u-full r-showmore"
                                      onClick={() => setLimit(limit + page)}>
        Show more ({rows.length - limit} more)</button>}
    </div>
  </div>;
}
