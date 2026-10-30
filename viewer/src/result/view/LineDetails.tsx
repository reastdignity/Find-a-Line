// The selected line of a result (model.selected): "Spot k of N" with ‹ › and × (ends the selection), its
// numbers (the facts of lineInfo in main.tsx), the other lines of its spot, and its actions: favourite,
// copy link, zoom, and the profile again after it was closed.

import { Fragment } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import { Button, IconButton, Stat, Stats } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { dh } from "../../filters";
import type { Line } from "../../types";
import type { ResultModel } from "../model";
import { FavouritePicker } from "./FavouritePicker";
import { ResultNote } from "./ResultSummary";
import { cx, num, Swatch, useModel } from "./common";

const BY_LABEL: Record<string, string> = {clearance: "the most clearance", longest: "the longest",
                                          height: "the highest above the ground"};
const DOTS = 12;  // dots of the spot's lines; more are a number only

/** The app's link to a line: its address with `#run=<id>&line=<lat1,lon1,lat2,lon2>`. */
export function appLink(model: ResultModel, l: Line): string {
  const base = typeof location !== "undefined" ? location.origin + location.pathname + location.search : "";
  return `${base}#run=${encodeURIComponent(model.runOf(l))}&line=${encodeURIComponent(model.lineLink(l))}`;
}

/** Put a text on the clipboard; false when the browser does not allow it. */
async function copy(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** A link the clipboard did not take: selected in a read-only field to copy by hand. */
export function LinkField({text}: {text: string}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { ref.current?.focus(); ref.current?.select(); }, [text]);
  return <label class="r-linkfield"><span class="u-small">Copy this link:</span>
    <input ref={ref} class="u-input" readOnly value={text} onFocus={(e) => e.currentTarget.select()} /></label>;
}

export interface LineDetailsProps {
  model: ResultModel;
  /** The link "Copy link" gives (default appLink: this page with #run=…&line=…). */
  linkOf?(line: Line): string;
  /** "Zoom" was pressed (hidden without it). */
  onZoom?(line: Line): void;
  /** ‹ › or a line of the spot selected another line (e.g. to bring it into the map's view). */
  onStep?(line: Line): void;
  /** "head": "Spot k of N", title and ‹ › only (a sheet's peek); "body": the rest; default both. */
  part?: "all" | "head" | "body";
  /** Show model.note (default false: ResultSummary shows it). */
  note?: boolean;
}

/** The facts of a line beyond its four numbers, as label and value. */
export function lineFacts(l: Line, highAbove: number): [string, string][] {
  const facts: [string, string][] = [];
  if (l.run) facts.push(["Run", `${l.run.info.created.slice(0, 10)} ${l.run.info.name}`]);
  facts.push(["Anchor 1", `${l.lat1.toFixed(6)}, ${l.lon1.toFixed(6)} · ${num(l.e1, 1)} m`]);
  facts.push(["Anchor 2", `${l.lat2.toFixed(6)}, ${l.lon2.toFixed(6)} · ${num(l.e2, 1)} m`]);
  if (l.clrN !== null) facts.push(["Over trees/buildings", `${num(l.clrN, 1)} m clear`]);
  if (isFinite(l.hmax)) {
    facts.push(["Above the ground", `up to ${num(l.hmax)} m, mean ${num(l.hmean)} m, ` +
                                    `${num(l.highPct)}% of the line ${highAbove} m or more`]);
  }
  if (isFinite(l.angle1)) facts.push(["Angle to the wall", `A1 ${num(l.angle1)}°, A2 ${num(l.angle2)}° (0° straight out)`]);
  const m = (v: number | null) => v === null ? "–" : num(v) + " m";
  if (l.prom1 !== null || l.drop1 !== null) {
    facts.push(["Anchors stand out", `prominence ${m(l.prom1)} / ${m(l.prom2)} · drop ${m(l.drop1)} / ${m(l.drop2)}`]);
  }
  if (l.water > 0) facts.push(["Over water", `${num(l.water)} m of ${num(l.len)} m`]);
  if (l.filled > 0) facts.push(["Over filled gaps", `${num(l.filled)} m (less precise data)`]);
  return facts;
}

// The button pressed for ‹ ›: its details are drawn again under the next row (another component), which
// gives the same button the focus; a button disabled at the end hands it to its pair
let refocus: string | null = null;
const PAIRS: Record<string, string> = {"Previous spot": "Next spot", "Next spot": "Previous spot",
                                       "Previous line of the spot": "Next line of the spot",
                                       "Next line of the spot": "Previous line of the spot"};

export function LineDetails({model, linkOf, onZoom, onStep, part = "all", note = false}: LineDetailsProps) {
  useModel(model);
  const [copied, setCopied] = useState<{line: Line; ok: boolean; text: string} | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const sel = model.selected;
  useEffect(() => {
    if (!refocus || !root.current) return;
    const find = (label: string) => root.current!.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
    const button = find(refocus), pair = find(PAIRS[refocus]);
    const target = button && !button.disabled ? button : pair && !pair.disabled ? pair : null;
    if (target) { target.focus(); refocus = null; }
  });
  if (!sel) return note ? <ResultNote model={model} /> : null;
  const {group, line: l} = sel;
  const k = model.position, n = model.rows.length, size = group.members.length, at = model.variantIndex();
  const pressed = (e: Event) => { refocus = (e.currentTarget as HTMLElement).getAttribute("aria-label"); };
  const step = (by: 1 | -1, e: Event) => {
    pressed(e);
    if (by > 0 ? model.next() : model.prev()) onStep?.(model.selected!.line);
  };
  const variant = (to: number, e?: Event) => {
    if (e) pressed(e);
    model.choose(group, group.members[to]);
    onStep?.(group.members[to]);
  };
  const link = () => {
    const text = (linkOf || ((x: Line) => appLink(model, x)))(l);
    copy(text).then((ok) => setCopied({line: l, ok, text}));
  };
  const shownCopy = copied && copied.line === l ? copied : null;

  const head = <div class="r-dhead">
    <div class="u-grow">
      <div class="r-eyebrow">{k >= 0 ? `Spot ${k + 1} of ${n}` : "Spot"}{size > 1 && ` · ${size} similar`}</div>
      <div class="r-dtitle">{l.run && <Swatch color={l.run.color} />}{num(l.len)} m · {num(l.clr, 1)} m clear</div>
    </div>
    <div class="r-pager">
      <IconButton icon="back" label="Previous spot" disabled={k <= 0 && k !== -1} onClick={(e) => step(-1, e)} />
      <IconButton icon="next" label="Next spot" disabled={k >= n - 1} onClick={(e) => step(1, e)} />
      <IconButton icon="close" label="Close the spot" class="r-dclose" onClick={() => model.select(null)} />
    </div>
  </div>;
  if (part === "head") return <div class="r-details" ref={root}>{head}</div>;

  return <div class={cx("r-details", l.over && "r-over")} ref={root}>
    {part === "all" && head}
    {note && <ResultNote model={model} />}
    <Stats>
      <Stat value={num(l.len)} unit="m" label="long" />
      <Stat value={num(l.clr, 1)} unit="m" label={l.over ? "clear of terrain" : "clearance"} />
      <Stat value={num(l.hmax)} unit="m" label="max height" />
      <Stat value={num(dh(l), 1)} unit="m" label="Δh anchors" />
    </Stats>
    {l.over && <p class="r-warn"><Icon name="warn" />Passes over trees or buildings: check it on the photo.</p>}
    {size > 1 && <div class="r-var">
      <IconButton icon="back" label="Previous line of the spot" plain disabled={at <= 0}
                  onClick={(e) => variant(at - 1, e)} />
      {size <= DOTS && <span class="r-dots" aria-hidden="true">{group.members.map((_, i) =>
        <i key={i} class={i === at ? "r-on" : ""} />)}</span>}
      <span class="u-grow"><b>{at + 1} of {size}</b> similar lines
        {at === 0 ? ` · ${BY_LABEL[model.state.by] || BY_LABEL.clearance}`
          : <> · <button type="button" class="r-link" onClick={() => variant(0)}>back to the first</button></>}</span>
      <IconButton icon="next" label="Next line of the spot" plain disabled={at >= size - 1}
                  onClick={(e) => variant(at + 1, e)} />
    </div>}
    <div class="r-acts">
      {model.favouritesOn && <FavouritePicker model={model} line={l} />}
      <Button icon="link" class="r-copy" onClick={link}>{shownCopy?.ok ? "Link copied" : "Copy link"}</Button>
      {onZoom && <Button icon="zoom" onClick={() => onZoom(l)}>Zoom</Button>}
      {model.profileClosed && <Button icon="ruler" class="r-reprofile" onClick={() => model.showProfile()}>Profile</Button>}
    </div>
    {shownCopy && !shownCopy.ok && <LinkField text={shownCopy.text} />}
    {shownCopy?.ok && <input type="hidden" class="r-copied" value={shownCopy.text} />}
    <dl class="r-kv">{lineFacts(l, model.data.high_above).map(([label, value]) =>
      <Fragment key={label}><dt>{label}</dt><dd>{value}</dd></Fragment>)}</dl>
  </div>;
}
