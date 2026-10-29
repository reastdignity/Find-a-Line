// Shared pieces of the results UI (result/view): following a ResultModel, numbers and colours of
// lines, a popover and the limit of a filter. Styles in result.css (classes r-*, under .ui).

import type { ComponentChildren } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import type { ResultModel } from "../model";
import { COLORS } from "../../style";
import type { Line } from "../../types";
import "./result.css";

/** Draw the component again after every change of the model. */
export function useModel(model: ResultModel) {
  const [, setTick] = useState(0);
  useEffect(() => model.subscribe(() => setTick((n) => n + 1)), [model]);
}

/** A number with `digits` decimals, "–" when it is not known. */
export function num(v: number | null | undefined, digits = 0): string {
  return v === null || v === undefined || !isFinite(v) ? "–" : v.toFixed(digits);
}

/** The colour of a line on the map (style.ts; another run's colour for its lines). */
export function lineColor(l: Line): string {
  return l.run ? l.run.color : l.over ? COLORS.over : COLORS.clean;
}

/** A small bar in a line's colour (CSSOM, which the CSP allows). */
export function Swatch({color, class: cls}: {color: string; class?: string}) {
  return <i class={"r-sw" + (cls ? " " + cls : "")} style={{background: color}} aria-hidden="true" />;
}

const cx = (...names: (string | false | null | undefined)[]) => names.filter(Boolean).join(" ");
export { cx };

/** A button that opens a panel under it; Esc or a click outside closes it. */
export function Popover({button, label, align = "left", children, open: openProp, onOpen}:
                        {button: (open: boolean, toggle: () => void) => ComponentChildren; label: string;
                         align?: "left" | "right"; children: ComponentChildren | (() => ComponentChildren);
                         open?: boolean; onOpen?: (open: boolean) => void}) {
  const [own, setOwn] = useState(false);
  const open = openProp ?? own;
  const set = (v: boolean) => { setOwn(v); onOpen?.(v); };
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (e: Event) => { if (ref.current && !ref.current.contains(e.target as Node)) set(false); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") set(false); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", key); };
  }, [open]);
  return <div class="r-popwrap" ref={ref}>
    {button(open, () => set(!open))}
    {open && <div class={cx("u-panel r-pop", align === "right" && "r-pop-right")} role="dialog" aria-label={label}>
      {typeof children === "function" ? children() : children}</div>}
  </div>;
}

let limits = 0;  // ids of the limits' sliders

export interface Bounds { min: number; max: number }

/** A filter's limit: a slider over the lines' range and a number field; at the end of the range, no
 *  limit (as panels.tsx Limit). The groups are computed when the slider is released. */
export function Limit({label, value, range, step, kind, unit, onChange}: {
  label: string; value: number | null; range: Bounds; step: number; kind: "min" | "max"; unit: string;
  onChange(v: number | null): void;
}) {
  const [draft, setDraft] = useState<number | null>(null);
  const low = Math.floor(range.min / step) * step, high = Math.ceil(range.max / step) * step;
  const end = kind === "min" ? low : high;
  const shown = draft ?? value ?? end;
  const commit = (v: number) => {
    setDraft(null);
    onChange(!isFinite(v) || (kind === "min" ? v <= low : v >= high) ? null : v);
  };
  const digits = step < 1 ? 1 : 0;
  const fill = `${high > low ? Math.max(0, Math.min(100, (shown - low) / (high - low) * 100)) : 0}%`;
  const style = {"--u-fill": fill};
  const id = useRef("r-lim-" + (++limits)).current;
  return <div class={cx("r-limit", value !== null && "r-set")}>
    <div class="r-limit-head">
      <label for={id}>{label}</label>
      <span class="r-limit-val">
        <input type="number" class="u-input r-num" key={String(shown)} step={step} aria-label={label}
               placeholder={end.toFixed(digits)}
               defaultValue={value === null && draft === null ? "" : shown.toFixed(digits)}
               onChange={(e) => e.currentTarget.value.trim() === "" ? onChange(null) : commit(+e.currentTarget.value)} />
        <span class="u-small">{unit}</span>
      </span>
    </div>
    <input type="range" id={id} class="u-range" min={low} max={high} step={step} value={shown} style={style}
           onInput={(e) => setDraft(+e.currentTarget.value)} onChange={(e) => commit(+e.currentTarget.value)} />
  </div>;
}
