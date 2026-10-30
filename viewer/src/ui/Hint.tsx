// A "?" after a field's label: a small round button with a bubble of text (role="tooltip"). The
// bubble opens while the mouse is over the button and while the button has the keyboard's focus; a
// click or a tap toggles it (phones have no hover); Esc and a click outside close it. The mouse may
// move from the button onto the bubble (to read or select its text): leaving either closes it after
// CLOSE_MS unless the pointer enters one of them again. The bubble is a popover in the top layer, so
// no scrolling panel cuts it off; it sits under the button, or over it when it would not fit in the
// visible part below (`visibleBottom`: the visual viewport, the scrolling panels around the button,
// a bar that stays at their bottom, data-hint-floor) and there is room above; it stays inside the
// window (positioned through the CSSOM, which the CSP allows). Styles in ui.css (u-hint*).

import type { ComponentChildren } from "preact";
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { t } from "../i18n";

let ids = 0;
const GAP = 6;     // between the button and the bubble
const MARGIN = 16; // between the bubble and the window's edges
const CLOSE_MS = 150;  // from leaving the button or the bubble to closing

/** Where the visible room under `button` ends (px from the top of the layout viewport): the bottom
 *  of the visual viewport (a phone's keyboard, a pinch zoom), of every scrolling panel around the
 *  button (C's form above its bottom bar, B's sheet) and the top of a bar that stays at the bottom
 *  over the content (data-hint-floor while it is sticky or fixed: the estimate in B's sheet). */
function visibleBottom(button: HTMLElement, at: DOMRect): number {
  const view = window.visualViewport;
  let bottom = view ? view.offsetTop + view.height : document.documentElement.clientHeight;
  for (let el = button.parentElement; el && el !== document.body; el = el.parentElement) {
    if (/auto|scroll/.test(getComputedStyle(el).overflowY)) bottom = Math.min(bottom, el.getBoundingClientRect().bottom);
  }
  for (const floor of document.querySelectorAll<HTMLElement>("[data-hint-floor]")) {
    const r = floor.getBoundingClientRect();
    const position = getComputedStyle(floor).position;
    if (r.height > 0 && r.top >= at.bottom && r.left < at.right && r.right > at.left
        && (position === "sticky" || position === "fixed")) bottom = Math.min(bottom, r.top);
  }
  return bottom;
}

/** The "?" button of `label` (its accessible name: "About <label>") with its bubble. */
export function Hint({label, children}: {label: string; children: ComponentChildren}) {
  const [id] = useState(() => `u-hint-${++ids}`);
  const [hover, setHover] = useState(false);
  const [focus, setFocus] = useState(false);
  const [pinned, setPinned] = useState(false);  // opened by a click or a tap
  const open = hover || focus || pinned;
  const root = useRef<HTMLSpanElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const bubble = useRef<HTMLSpanElement>(null);
  const timer = useRef<number | undefined>(undefined);
  const close = () => { clearTimeout(timer.current); setHover(false); setFocus(false); setPinned(false); };
  const enter = (e: PointerEvent) => {
    if (e.pointerType !== "mouse") return;
    clearTimeout(timer.current);
    setHover(true);
  };
  const leave = (e: PointerEvent) => {
    if (e.pointerType !== "mouse") return;
    clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setHover(false), CLOSE_MS);
  };
  useEffect(() => () => clearTimeout(timer.current), []);

  useLayoutEffect(() => {
    const tip = bubble.current;
    if (!tip) return;
    if (!open) {
      if (tip.matches?.(":popover-open")) tip.hidePopover();
      return;
    }
    if (tip.showPopover && !tip.matches(":popover-open")) tip.showPopover();
    const place = () => {
      const at = button.current?.getBoundingClientRect();
      if (!at) return;
      const width = document.documentElement.clientWidth;
      const w = tip.offsetWidth, h = tip.offsetHeight;
      const left = Math.max(MARGIN, Math.min(at.left, width - MARGIN - w));
      const below = at.bottom + GAP, above = at.top - GAP - h;
      const ceiling = (window.visualViewport?.offsetTop ?? 0) + GAP;
      // below unless it would run past the visible part there and fits above
      const top = below + h > visibleBottom(button.current!, at) - GAP && above >= ceiling ? above : below;
      tip.style.left = `${Math.round(left)}px`;
      tip.style.top = `${Math.round(top)}px`;
    };
    place();
    const outside = (e: Event) => { if (root.current && !root.current.contains(e.target as Node)) close(); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); close(); } };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", key);
    window.addEventListener("scroll", place, true);  // the panel under it scrolls: follow the button
    window.addEventListener("resize", place);
    window.visualViewport?.addEventListener("resize", place);  // a phone's keyboard, a zoom
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", key);
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
      window.visualViewport?.removeEventListener("resize", place);
    };
  }, [open]);

  return <span class="u-hint-wrap" ref={root}>
    <button type="button" ref={button} class="u-hint" aria-label={t("hint.about", {label})} aria-describedby={id}
            onPointerEnter={enter} onPointerLeave={leave}
            // the keyboard's focus only: a click focuses the button too, and toggles it itself
            onFocus={(e) => setFocus(e.currentTarget.matches(":focus-visible"))}
            onBlur={() => { setFocus(false); setPinned(false); }}
            onClick={() => { if (pinned) close(); else setPinned(true); }}>?</button>
    <span ref={bubble} id={id} role="tooltip" class={open ? "u-hint-tip u-hint-on" : "u-hint-tip"} popover="manual"
          onPointerEnter={enter} onPointerLeave={leave}>
      {children}</span>
  </span>;
}
