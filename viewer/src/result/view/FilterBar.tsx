// The filters of a result: chips of the main ones (length, clearance, height above the ground, only
// clear lines; a set one is removable, a click edits it) and "More filters": every filter of the old
// map (panels.tsx FiltersPanel, filters.ts), shown only when the lines have its data, and the grouping.
// Values come from and go to the ResultModel (filters, state, ranges). "How to read the result"
// (ResultIntro) opens once, from here: the FilterBar is in every layout of a result.

import { useRef, useState } from "preact/hooks";
import { Button, Chip, Chips, Segmented } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { IntroLink, useIntro } from "../../ui/Intro";
import { t } from "../../i18n";
import { active, NO_FILTERS, type Filters } from "../../filters";
import { COLORS } from "../../style";
import type { ResultModel } from "../model";
import { Limit, Swatch, num, useModel } from "./common";
import { ResultIntro } from "./ResultIntro";

type Main = "len" | "clr" | "height";
const MAIN_KEYS: (keyof Filters)[] = ["lenMin", "lenMax", "clrMin", "heightMin"];

export interface FilterBarProps {
  model: ResultModel;
  /** "More filters" was pressed: the shell shows <MoreFilters> itself; without it they unfold under the chips. */
  onMore?: () => void;
}

const set = (model: ResultModel, change: Partial<Filters>) => model.setFilters({...model.filters, ...change});

/** How many filters are set beyond the chips (the "More filters" badge). */
function moreActive(model: ResultModel): number {
  const f = model.filters;
  const main = MAIN_KEYS.filter((k) => f[k] !== NO_FILTERS[k]).length;
  return active(f) - main + (!model.state.showClean ? 1 : 0);
}

function lengthLabel(f: Filters): string {
  if (f.lenMin !== null && f.lenMax !== null) return `Length ${num(f.lenMin)}–${num(f.lenMax)} m`;
  if (f.lenMin !== null) return `Length ≥ ${num(f.lenMin)} m`;
  if (f.lenMax !== null) return `Length ≤ ${num(f.lenMax)} m`;
  return "Length";
}

/** The editor of a main filter, under the chips. */
function MainEditor({model, which}: {model: ResultModel; which: Main}) {
  const f = model.filters, r = model.ranges;
  if (which === "len" && r.len) {
    return <>
      <Limit label="Length from" value={f.lenMin} range={r.len} step={1} kind="min" unit="m"
             onChange={(v) => set(model, {lenMin: v})} />
      <Limit label="Length to" value={f.lenMax} range={r.len} step={1} kind="max" unit="m"
             onChange={(v) => set(model, {lenMax: v})} />
    </>;
  }
  if (which === "clr" && r.clr) {
    return <Limit label="Clearance at least" value={f.clrMin} range={r.clr} step={0.1} kind="min" unit="m"
                  onChange={(v) => set(model, {clrMin: v})} />;
  }
  if (which === "height" && r.hmax) {
    return <Limit label="Height above the ground at least" value={f.heightMin} range={r.hmax} step={1} kind="min"
                  unit="m" onChange={(v) => set(model, {heightMin: v})} />;
  }
  return null;
}

export function FilterBar({model, onMore}: FilterBarProps) {
  useModel(model);
  const [editing, setEditing] = useState<Main | null>(null);
  const [more, setMore] = useState(false);
  const bar = useRef<HTMLDivElement>(null);
  /** Close the editor; the focus back to its chip when it was in the editor (Esc, ×). */
  const closeEditor = () => {
    const chip = bar.current?.querySelector<HTMLElement>("[aria-expanded=true]");
    const inside = bar.current?.querySelector(".r-editor")?.contains(document.activeElement);
    setEditing(null);
    if (inside) chip?.focus();
  };
  const removed = (which: Main, change: Partial<Filters>) => {
    set(model, change);
    if (editing === which) setEditing(null);
  };
  const f = model.filters, r = model.ranges, s = model.state;
  const onlyClear = !s.showOver && s.showClean;
  const anything = active(f) > 0 || !s.showOver || !s.showClean;
  const toggle = (which: Main) => setEditing(editing === which ? null : which);
  const extra = moreActive(model);
  const intro = useIntro("result");
  return <div class="r-filters" ref={bar}>
    <ResultIntro state={intro} />
    <Chips>
      {r.len && <Chip on={f.lenMin !== null || f.lenMax !== null} onClick={() => toggle("len")} expanded={editing === "len"}
                      onRemove={f.lenMin !== null || f.lenMax !== null
                        ? () => removed("len", {lenMin: null, lenMax: null}) : undefined}
                      removeLabel="Remove the length filter">{lengthLabel(f)}</Chip>}
      {r.clr && <Chip on={f.clrMin !== null} onClick={() => toggle("clr")} expanded={editing === "clr"}
                      onRemove={f.clrMin !== null ? () => removed("clr", {clrMin: null}) : undefined}
                      removeLabel="Remove the clearance filter">
        {f.clrMin !== null ? `Clearance ≥ ${num(f.clrMin, 1)} m` : "Clearance"}</Chip>}
      {r.hmax && <Chip on={f.heightMin !== null} onClick={() => toggle("height")} expanded={editing === "height"}
                       onRemove={f.heightMin !== null ? () => removed("height", {heightMin: null}) : undefined}
                       removeLabel="Remove the height filter">
        {f.heightMin !== null ? `Height ≥ ${num(f.heightMin)} m` : "Height"}</Chip>}
      {model.nOver > 0 && <Chip on={onlyClear} onClick={() => model.setState({...s, showClean: true, showOver: onlyClear})}>
        Only clear</Chip>}
      <Chip add icon="filter" on={more || extra > 0}
            onClick={() => { if (onMore) onMore(); else setMore(!more); }}>
        More filters{extra > 0 && <span class="u-badge">{extra}</span>}</Chip>
      {anything && <button type="button" class="r-link" onClick={() => {
        model.setState({...s, showClean: true, showOver: true});
        model.clearFilters();
        setEditing(null);
      }}>Clear all</button>}
      <IntroLink state={intro} class="r-link r-intro-link">{t("intro.result.link")}</IntroLink>
    </Chips>
    {editing && <div class="r-editor" role="group" aria-label="Filter"
                     onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeEditor(); } }}>
      <MainEditor model={model} which={editing} />
      <button type="button" class="r-note-x r-editor-x" aria-label="Close the filter" onClick={closeEditor}>
        <Icon name="close" /></button></div>}
    {more && !onMore && <MoreFilters model={model} onClose={() => setMore(false)} />}
  </div>;
}

/** The grouping distance: the label shows the value while dragging, the groups change when released. */
function Distance({value, max, onChange}: {value: number; max: number; onChange(v: number): void}) {
  const [draft, setDraft] = useState<number | null>(null);
  const shown = draft ?? value;
  const fill = `${Math.max(0, Math.min(100, (shown - 5) / (max - 5) * 100))}%`;
  const id = useRef("r-dist-" + Math.random().toString(36).slice(2)).current;
  return <div class="r-limit r-set">
    <div class="r-limit-head"><label for={id}>Anchors within</label><b class="r-mono">{shown} m</b></div>
    <input type="range" id={id} class="u-range r-distance" min={5} max={max} step={1} value={shown}
           style={{"--u-fill": fill}}
           onInput={(e) => setDraft(+e.currentTarget.value)}
           onChange={(e) => { setDraft(null); onChange(+e.currentTarget.value); }} />
  </div>;
}

export interface MoreFiltersProps {
  model: ResultModel;
  onClose?: () => void;
}

/** Every filter and the grouping of the lines. */
export function MoreFilters({model, onClose}: MoreFiltersProps) {
  useModel(model);
  const f = model.filters, r = model.ranges, s = model.state;
  const limit = (label: string, key: keyof Filters, range: typeof r.len, step: number, kind: "min" | "max",
                 unit = "m") => range && <Limit label={label} value={f[key] as number | null} range={range}
                                                 step={step} kind={kind} unit={unit}
                                                 onChange={(v) => set(model, {[key]: v} as Partial<Filters>)} />;
  const maxDistance = Math.max(100, Math.round(3 * model.data.distance));
  return <section class="r-more" aria-label="More filters">
    <div class="r-more-head"><b class="u-h4">Filters</b>
      <span class="u-grow" />
      <Button kind="ghost" onClick={() => { model.setState({...s, showClean: true, showOver: true}); model.clearFilters(); }}>
        Clear all</Button>
      {onClose && <button type="button" class="r-note-x" aria-label="Close the filters" onClick={onClose}>
        <Icon name="close" /></button>}
    </div>

    <div class="r-more-sec">
      <h3>Lines shown</h3>
      {model.nOver > 0 && <>
        <label class="r-check"><input type="checkbox" checked={s.showClean}
                                      onChange={(e) => model.setState({...s, showClean: e.currentTarget.checked})} />
          <Swatch color={COLORS.clean} /> Clear lines ({model.nClean})</label>
        <label class="r-check"><input type="checkbox" checked={s.showOver}
                                      onChange={(e) => model.setState({...s, showOver: e.currentTarget.checked})} />
          <Swatch color={COLORS.over} /> Over trees or buildings ({model.nOver})</label>
      </>}
      {r.favourites && <label class="r-check"><input type="checkbox" class="r-fav-only" checked={f.favOnly}
                                                     onChange={(e) => set(model, {favOnly: e.currentTarget.checked})} />
        Only favourite lines</label>}
      {r.water && <label class="r-check r-select">Water
        <select class="u-input" value={f.water}
                onChange={(e) => set(model, {water: e.currentTarget.value as Filters["water"]})}>
          <option value="all">all lines</option>
          <option value="water">only over water</option>
          <option value="land">only over land</option>
        </select></label>}
      {r.filled && <label class="r-check"><input type="checkbox" checked={f.hideFilled}
                                                 onChange={(e) => set(model, {hideFilled: e.currentTarget.checked})} />
        Hide lines over filled gaps in the data</label>}
    </div>

    <div class="r-more-sec">
      <h3>Length and clearance</h3>
      {limit("Length from", "lenMin", r.len, 1, "min")}
      {limit("Length to", "lenMax", r.len, 1, "max")}
      {limit("Clearance at least", "clrMin", r.clr, 0.1, "min")}
      {limit("Clearance over trees at least", "clrNMin", r.clrN, 0.1, "min")}
    </div>

    <div class="r-more-sec">
      <h3>Anchors</h3>
      {limit("Height difference at most", "dhMax", r.dh, 0.5, "max")}
      {limit("Lower anchor at least", "elevMin", r.elev, 1, "min", "m a.s.l.")}
      {limit("Angle to the wall at most", "angleMax", r.angle, 1, "max", "°")}
      {limit("One anchor prominent at least", "promMin", r.prom, 0.5, "min")}
      {limit("Both anchors prominent at least", "promBothMin", r.prom, 0.5, "min")}
      {limit("Drop below both anchors at least", "dropMin", r.drop, 1, "min")}
    </div>

    {(r.hmax || r.hmean || r.highPct) && <div class="r-more-sec">
      <h3>Height above the ground</h3>
      {limit("Highest point at least", "heightMin", r.hmax, 1, "min")}
      {limit("Mean height at least", "meanMin", r.hmean, 1, "min")}
      {limit(`${r.highAbove} m or more above the ground, at least`, "highPctMin", r.highPct, 1, "min",
             "% of the line")}
    </div>}

    <div class="r-more-sec">
      <h3>Grouping</h3>
      <label class="r-check"><input type="checkbox" class="r-group" checked={s.group}
                                    onChange={(e) => model.setState({...s, group: e.currentTarget.checked})} />
        Group similar lines into spots</label>
      {s.group && <>
        <Distance value={s.distance} max={maxDistance} onChange={(distance) => model.setState({...s, distance})} />
        <div class="r-field-lab">Show per spot</div>
        <Segmented label="Show per spot" value={s.by} onChange={(by) => model.setState({...s, by})}
                   options={[{value: "clearance", label: "Most clearance"}, {value: "longest", label: "Longest"},
                             {value: "height", label: "Highest"}]} />
        {limit("Lines in a spot at least", "groupMin", r.group, 1, "min", "")}
      </>}
    </div>
  </section>;
}
