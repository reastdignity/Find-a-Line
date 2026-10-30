// A "?" after a field's label: a small round button with a bubble of text (role="tooltip"). The
// bubble opens while the mouse is over the button and while the button has the keyboard's focus; a
// click or a tap toggles it (phones have no hover); Esc and a click outside close it. The bubble is
// a popover in the top layer, so no scrolling panel cuts it off; it sits under the button (over it
// near the bottom of the window) and stays inside the window (positioned through the CSSOM, which
// the CSP allows). Styles in ui.css (u-hint*).

import type { ComponentChildren } from "preact";
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { t } from "../i18n";

let ids = 0;
const GAP = 6;     // between the button and the bubble
const MARGIN = 16; // between the bubble and the window's edges

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
  const close = () => { setHover(false); setFocus(false); setPinned(false); };

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
      const width = document.documentElement.clientWidth, height = document.documentElement.clientHeight;
      const w = tip.offsetWidth, h = tip.offsetHeight;
      const left = Math.max(MARGIN, Math.min(at.left, width - MARGIN - w));
      const below = at.bottom + GAP;
      const top = below + h > height - GAP && at.top - GAP - h > GAP ? at.top - GAP - h : below;
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
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", key);
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open]);

  return <span class="u-hint-wrap" ref={root}>
    <button type="button" ref={button} class="u-hint" aria-label={t("hint.about", {label})} aria-describedby={id}
            onPointerEnter={(e) => { if (e.pointerType === "mouse") setHover(true); }}
            onPointerLeave={(e) => { if (e.pointerType === "mouse") setHover(false); }}
            // the keyboard's focus only: a click focuses the button too, and toggles it itself
            onFocus={(e) => setFocus(e.currentTarget.matches(":focus-visible"))}
            onBlur={() => { setFocus(false); setPinned(false); }}
            onClick={() => { if (pinned) close(); else setPinned(true); }}>?</button>
    <span ref={bubble} id={id} role="tooltip" class={open ? "u-hint-tip u-hint-on" : "u-hint-tip"} popover="manual">
      {children}</span>
  </span>;
}
