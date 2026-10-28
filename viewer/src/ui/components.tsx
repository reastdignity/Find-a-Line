// Base components of the new interfaces (prototypes B and C). Styles in ui.css (classes u-*),
// which apply only inside an element of the class "ui" (see Screen). No inline style strings:
// the only `style` props are CSSOM (allowed by the CSP's style-src 'self').

import type { ButtonHTMLAttributes, ComponentChildren } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import { Icon, type IconName } from "./icons";
import "./ui.css";

const cx = (...names: (string | false | null | undefined)[]) => names.filter(Boolean).join(" ");

export type Theme = "auto" | "light" | "dark";

/** The root of a new interface: a full-screen page (`screen`) or a block, with its theme. */
export function Screen({theme = "auto", screen = true, class: cls, children}:
                       {theme?: Theme; screen?: boolean; class?: string; children?: ComponentChildren}) {
  return <div class={cx("ui", screen && "u-screen", cls)} data-theme={theme === "auto" ? undefined : theme}>
    {children}</div>;
}

/** Whether a media query matches, following its changes. */
export function useMedia(query: string): boolean {
  const get = () => typeof matchMedia === "function" && matchMedia(query).matches;
  const [matches, setMatches] = useState(get);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const list = matchMedia(query);
    const changed = () => setMatches(list.matches);
    changed();
    list.addEventListener("change", changed);
    return () => list.removeEventListener("change", changed);
  }, [query]);
  return matches;
}

/** Wide enough for side panels instead of bottom sheets. */
export const DESKTOP = "(min-width: 768px)";

// ---- buttons ---------------------------------------------------------------------------------

type ButtonProps = {
  kind?: "default" | "primary" | "ghost" | "danger"; large?: boolean; full?: boolean;
  icon?: IconName; iconRight?: IconName; href?: string;
  children?: ComponentChildren; class?: string;
} & Omit<ButtonHTMLAttributes<HTMLButtonElement>, "icon" | "class">;

/** A button; with `href` a link that looks like one. */
export function Button({kind = "default", large, full, icon, iconRight, href, children, class: cls, ...rest}: ButtonProps) {
  const className = cx("u-btn", kind === "primary" && "u-pri", kind === "ghost" && "u-ghost", kind === "danger" && "u-danger",
                       large && "u-lg", full && "u-full", cls);
  const inner = <>{icon && <Icon name={icon} />}{children}{iconRight && <Icon name={iconRight} />}</>;
  if (href) return <a class={className} href={href}>{inner}</a>;
  return <button type="button" class={className} {...rest}>{inner}</button>;
}

/** A square button with an icon only; `label` is its accessible name and tooltip. */
export function IconButton({icon, label, on, plain, round, fav, class: cls, ...rest}:
                           {icon: IconName; label: string; on?: boolean; plain?: boolean; round?: boolean; fav?: boolean;
                            class?: string} & Omit<ButtonHTMLAttributes<HTMLButtonElement>, "icon" | "class">) {
  return <button type="button" class={cx("u-ib", on && "u-on", plain && "u-plain", round && "u-round", cls)}
                 aria-label={label} title={label} aria-pressed={on === undefined ? undefined : on} {...rest}>
    <Icon name={icon} fav={fav} /></button>;
}

// ---- chips and choices -----------------------------------------------------------------------

/** A chip: a toggle (`on`), a removable value (`onRemove`) or a dashed "add" button (`add`). */
export function Chip({on, add, icon, onClick, onRemove, removeLabel = "Remove", children}:
                     {on?: boolean; add?: boolean; icon?: IconName; onClick?: () => void; onRemove?: () => void;
                      removeLabel?: string; children?: ComponentChildren}) {
  const className = cx("u-chip", on && "u-on", add && "u-add");
  const body = <>{(icon || add) && <Icon name={icon || "plus"} />}{children}</>;
  if (onRemove) {  // a span with two buttons: buttons may not nest
    return <span class={className}>
      {onClick ? <button type="button" class="u-chip-x" onClick={onClick}>{body}</button> : body}
      <button type="button" class="u-chip-x" aria-label={removeLabel} title={removeLabel}
              onClick={(e) => { e.stopPropagation(); onRemove(); }}><Icon name="close" /></button>
    </span>;
  }
  return <button type="button" class={className} aria-pressed={on === undefined ? undefined : on} onClick={onClick}>
    {body}</button>;
}

export function Chips({children}: {children?: ComponentChildren}) {
  return <div class="u-chips">{children}</div>;
}

export interface Option<T extends string> { value: T; label: string; icon?: IconName }

/** One of a few choices (a radio group). */
export function Segmented<T extends string>({options, value, onChange, label}:
                                            {options: Option<T>[]; value: T; onChange: (value: T) => void; label?: string}) {
  return <div class="u-seg" role="radiogroup" aria-label={label}>
    {options.map((o) => <button type="button" key={o.value} role="radio" aria-checked={o.value === value}
                                class={o.value === value ? "u-on" : ""} onClick={() => onChange(o.value)}>
      {o.icon && <Icon name={o.icon} />}{o.label}</button>)}
  </div>;
}

export interface TabItem<T extends string> extends Option<T> { badge?: string | number }

/** Tabs over a panel's content (the content is the caller's). */
export function Tabs<T extends string>({tabs, value, onChange, label}:
                                       {tabs: TabItem<T>[]; value: T; onChange: (value: T) => void; label?: string}) {
  return <div class="u-tabs" role="tablist" aria-label={label}>
    {tabs.map((t) => <button type="button" key={t.value} role="tab" aria-selected={t.value === value}
                             class={t.value === value ? "u-on" : ""} onClick={() => onChange(t.value)}>
      {t.icon && <Icon name={t.icon} />}{t.label}
      {t.badge !== undefined && t.badge !== "" && <span class="u-badge u-n">{t.badge}</span>}</button>)}
  </div>;
}

// ---- panels and fields -----------------------------------------------------------------------

/** A raised card; `padded` lays its children out in a column with gaps. */
export function Panel({title, actions, padded = true, class: cls, children}:
                      {title?: ComponentChildren; actions?: ComponentChildren; padded?: boolean; class?: string;
                       children?: ComponentChildren}) {
  return <section class={cx("u-panel", padded && "u-pad", cls)}>
    {(title || actions) && <div class="u-panel-head">
      {title && <h2 class="u-h">{title}</h2>}{actions && <div class="u-row">{actions}</div>}</div>}
    {children}
  </section>;
}

/** A labelled field: the label, its current value on the right, the control, a help text. */
export function Field({label, value, help, htmlFor, children}:
                      {label: ComponentChildren; value?: ComponentChildren; help?: ComponentChildren; htmlFor?: string;
                       children?: ComponentChildren}) {
  return <div class="u-field">
    <div class="u-field-lab"><label for={htmlFor}>{label}</label>{value !== undefined && <b>{value}</b>}</div>
    {children}
    {help && <div class="u-field-help">{help}</div>}
  </div>;
}

const fill = (value: number, min: number, max: number) =>
  `${max > min ? Math.max(0, Math.min(100, (value - min) / (max - min) * 100)) : 0}%`;

/** A slider (a styled native range input). */
export function Range({value, min, max, step = 1, onInput, label, id}:
                      {value: number; min: number; max: number; step?: number; onInput: (value: number) => void;
                       label?: string; id?: string}) {
  return <input type="range" class="u-range" id={id} aria-label={label} min={min} max={max} step={step} value={value}
                style={{"--u-fill": fill(value, min, max)}}
                onInput={(e) => onInput(Number(e.currentTarget.value))} />;
}

/** A min/max slider: two native range inputs over one track; the thumbs never cross. */
export function RangePair({low, high, min, max, step = 1, onInput, label = ""}:
                          {low: number; high: number; min: number; max: number; step?: number;
                           onInput: (low: number, high: number) => void; label?: string}) {
  return <div class="u-range2">
    <i style={{left: fill(low, min, max), width: `calc(${fill(high, min, max)} - ${fill(low, min, max)})`}} />
    <input type="range" class="u-range" aria-label={`${label} minimum`.trim()} min={min} max={max} step={step} value={low}
           onInput={(e) => { const v = Math.min(Number(e.currentTarget.value), high); e.currentTarget.value = String(v);
                             onInput(v, high); }} />
    <input type="range" class="u-range" aria-label={`${label} maximum`.trim()} min={min} max={max} step={step} value={high}
           onInput={(e) => { const v = Math.max(Number(e.currentTarget.value), low); e.currentTarget.value = String(v);
                             onInput(low, v); }} />
  </div>;
}

// ---- status ----------------------------------------------------------------------------------

export type Status = "running" | "done" | "waiting" | "failed" | "shared";
const STATUS: Record<Status, [string, IconName]> = {
  running: ["running", "refresh"], done: ["done", "check"], waiting: ["waiting", "clock"], failed: ["failed", "warn"],
  shared: ["shared", "link"],
};

/** The state of a search or run as a small coloured label. */
export function StatusChip({status, children}: {status: Status; children?: ComponentChildren}) {
  const [text, icon] = STATUS[status];
  return <span class={`u-st u-${status}`}><Icon name={icon} />{children ?? text}</span>;
}

/** A progress bar: `value` from 0 to 1, or null while the amount is unknown. */
export function Progress({value, label}: {value: number | null; label?: ComponentChildren}) {
  const bar = <div class={cx("u-prog", value === null && "u-busy")} role="progressbar" aria-valuemin={0} aria-valuemax={100}
                   aria-valuenow={value === null ? undefined : Math.round(value * 100)}>
    <i style={value === null ? undefined : {width: `${Math.max(0, Math.min(1, value)) * 100}%`}} />
  </div>;
  return label ? <div class="u-progress">{label}{bar}</div> : bar;
}

/** A number with its unit and caption; put several in <Stats>. */
export function Stat({value, unit, label}: {value: ComponentChildren; unit?: string; label: ComponentChildren}) {
  return <div class="u-stat"><b>{value}{unit && <small>{unit}</small>}</b><span>{label}</span></div>;
}

export function Stats({children}: {children?: ComponentChildren}) {
  return <div class="u-stats">{children}</div>;
}

/** A dark message bar with an optional action and a close button (placing it is the caller's). */
export function Toast({children, action, onClose}:
                      {children?: ComponentChildren; action?: {label: string; onClick: () => void};
                       onClose?: () => void}) {
  return <div class="u-toast" role="status">
    <div class="u-toast-text">{children}</div>
    {action && <Button onClick={action.onClick}>{action.label}</Button>}
    {onClose && <IconButton icon="close" label="Close" onClick={onClose} />}
  </div>;
}

// ---- navigation ------------------------------------------------------------------------------

export interface NavItem<T extends string> { value: T; label: string; icon: IconName; badge?: string | number }

/** B's left navigation: the mark on top, the items, then `bottom` (e.g. the account). */
export function Rail<T extends string>({items, value, onChange, top, bottom}:
                                       {items: NavItem<T>[]; value: T | null; onChange: (value: T) => void;
                                        top?: ComponentChildren; bottom?: ComponentChildren}) {
  return <nav class="u-rail" aria-label="Main">
    {top}
    {items.map((it) => <button type="button" key={it.value} class={cx("u-rail-item", it.value === value && "u-on")}
                               aria-current={it.value === value ? "page" : undefined} onClick={() => onChange(it.value)}>
      <Icon name={it.icon} />{it.label}
      {it.badge !== undefined && it.badge !== "" && <span class="u-badge">{it.badge}</span>}</button>)}
    <div class="u-rail-sp" />
    {bottom}
  </nav>;
}

/** C's navigation on phones, at the bottom of the screen. */
export function BottomNav<T extends string>({items, value, onChange}:
                                            {items: NavItem<T>[]; value: T | null; onChange: (value: T) => void}) {
  return <nav class="u-bnav" aria-label="Main">
    {items.map((it) => <button type="button" key={it.value} class={cx("u-bnav-item", it.value === value && "u-on")}
                               aria-current={it.value === value ? "page" : undefined} onClick={() => onChange(it.value)}>
      <Icon name={it.icon} />{it.label}
      {it.badge !== undefined && it.badge !== "" && <span class="u-badge">{it.badge}</span>}</button>)}
  </nav>;
}

// ---- sheet -----------------------------------------------------------------------------------

export type Snap = "peek" | "half" | "full";
const SNAPS: Snap[] = ["peek", "half", "full"];

/** The height of a snap state in a container of `height` pixels. */
function snapHeight(snap: Snap, height: number, peek: number): number {
  if (snap === "peek") return Math.min(peek, height);
  if (snap === "half") return Math.max(Math.min(peek, height), Math.round(height * 0.5));
  return Math.max(0, height - 16);
}

/** A bottom sheet on phones: peek, half or full height, dragged by its handle (pointer events, so
 *  touch too), or moved one state with a click or the arrow keys. Wider screens (`DESKTOP`) get
 *  `desktop`: a panel at the left or right side (full height), floating (its content's height), docked
 *  (in the page's flow, e.g. a flex column) or the sheet as on phones. Its position is absolute in
 *  the nearest positioned ancestor (the map's container). `snap`/`onSnap` control it, or it keeps
 *  its own state from `initial`. */
export function Sheet({label, header, children, snap, onSnap, initial = "half", peek = 120,
                       desktop = "left"}:
                      {label: string; header?: ComponentChildren; children?: ComponentChildren; snap?: Snap;
                       onSnap?: (snap: Snap) => void; initial?: Snap; peek?: number;
                       desktop?: "left" | "right" | "float" | "docked" | "sheet"}) {
  const wide = useMedia(DESKTOP);
  const [own, setOwn] = useState<Snap>(initial);
  const current = snap ?? own;
  const set = (s: Snap) => { setOwn(s); onSnap?.(s); };
  const ref = useRef<HTMLElement>(null);
  const [drag, setDrag] = useState<{start: number; from: number; height: number} | null>(null);
  const [dragHeight, setDragHeight] = useState<number | null>(null);
  const [box, setBox] = useState(0);  // the container's height

  useEffect(() => {
    const parent = ref.current?.parentElement;
    if (!parent) return;
    const measure = () => setBox(parent.clientHeight);
    measure();
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(measure);
    observer.observe(parent);
    return () => observer.disconnect();
  }, [wide, desktop]);

  if (wide && desktop !== "sheet") {
    const side = desktop === "float" ? "u-left u-float" : desktop === "docked" ? "u-docked" : `u-${desktop}`;
    return <section ref={ref} class={`u-panel u-side ${side}`} aria-label={label}>
      {header && <div class="u-sheet-head">{header}</div>}
      <div class="u-sheet-body">{children}</div>
    </section>;
  }

  const height = dragHeight ?? snapHeight(current, box, peek);
  const step = (by: number) => set(SNAPS[Math.max(0, Math.min(2, SNAPS.indexOf(current) + by))]);
  const down = (e: PointerEvent) => {
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    setDrag({start: e.clientY, from: height, height});
  };
  const move = (e: PointerEvent) => {
    if (!drag) return;
    const next = Math.max(Math.min(peek, box) * 0.5, Math.min(box, drag.from + drag.start - e.clientY));
    setDrag({...drag, height: next});
    setDragHeight(next);
  };
  const up = (e: PointerEvent) => {
    if (!drag) return;
    const moved = Math.abs(e.clientY - drag.start);
    setDrag(null);
    setDragHeight(null);
    if (moved < 6) { set(current === "full" ? "peek" : SNAPS[SNAPS.indexOf(current) + 1]); return; }  // a tap
    // the nearest state, leaning towards the direction of the drag
    const target = drag.height + (drag.height - drag.from) * 0.3;
    let best: Snap = current, distance = Infinity;
    for (const s of SNAPS) {
      const d = Math.abs(snapHeight(s, box, peek) - target);
      if (d < distance) { best = s; distance = d; }
    }
    set(best);
  };
  return <section ref={ref} class={cx("u-sheet", `u-${current}`, drag && "u-dragging")} aria-label={label}
                  style={box ? {height: `${height}px`} : undefined}>
    <button type="button" class="u-sheet-handle" aria-label={`${label}: ${current}; drag or press to resize`}
            onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up}
            onKeyDown={(e) => {
              if (e.key === "ArrowUp") { step(1); e.preventDefault(); }
              else if (e.key === "ArrowDown") { step(-1); e.preventDefault(); }
            }}
            onClick={(e) => { if (e.detail === 0) set(current === "full" ? "peek" : SNAPS[SNAPS.indexOf(current) + 1]); }} />
    {header && <div class="u-sheet-head">{header}</div>}
    <div class="u-sheet-body">{children}</div>
  </section>;
}
