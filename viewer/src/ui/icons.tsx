// The icon set of the new interfaces (24×24, stroked with currentColor) and the Find-a-Line logo.
// The paths are static markup (no style attributes), so setting them as innerHTML is safe under the CSP.
import LOGO_SVG from "./logo/logo.svg";
import MARK_SVG from "./logo/mark.svg";

export const ICONS = {
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
  folder: '<path d="M3 6.5A1.5 1.5 0 014.5 5H9l2 2.5h8.5A1.5 1.5 0 0121 9v9.5a1.5 1.5 0 01-1.5 1.5h-15A1.5 1.5 0 013 18.5z"/>',
  pin: '<path d="M12 21s-7-6.2-7-11a7 7 0 0114 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>',
  poly: '<path d="M5 7l7-3 7 5-2 9H7z"/>',
  file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>',
  cliff: '<path d="M3 20l6-11 4 6 3-4 5 9z"/>',
  layers: '<path d="M12 4l9 5-9 5-9-5z"/><path d="M3 14l9 5 9-5"/>',
  share: '<circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/>' +
    '<path d="M8.2 10.8l7.6-3.6M8.2 13.2l7.6 3.6"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  star: '<path d="M12 3.5l2.6 5.3 5.8.8-4.2 4.1 1 5.8L12 16.8 6.8 19.5l1-5.8-4.2-4.1 5.8-.8z"/>',
  back: '<path d="M15 5l-7 7 7 7"/>',
  next: '<path d="M9 5l7 7-7 7"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  filter: '<path d="M4 6h16M7 12h10M10 18h4"/>',
  ruler: '<path d="M4 16L16 4l4 4L8 20z"/><path d="M8 12l2 2M11 9l2 2M14 6l2 2"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 4.5-6 8-6s7 2 8 6"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  check: '<path d="M5 12l5 5 9-10"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r=".8"/><circle cx="4.5" cy="12" r=".8"/>' +
    '<circle cx="4.5" cy="18" r=".8"/>',
  home: '<path d="M4 11l8-7 8 7v9h-5v-6H9v6H4z"/>',
  link: '<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"/>',
  zoom: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4M8 11h6M11 8v6"/>',
  compare: '<path d="M7 4v16M17 4v16M3 8h8M13 16h8"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  more: '<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>',
  locate: '<circle cx="12" cy="12" r="4"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  rope: '<path d="M3 7 Q12 15 21 7"/><circle cx="3" cy="7" r="1.6"/><circle cx="21" cy="7" r="1.6"/>',
  warn: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17v.5"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  refresh: '<path d="M20 11a8 8 0 10-2.3 5.7M20 4v7h-7"/>',
} as const;

export type IconName = keyof typeof ICONS;

/** An icon; decorative (aria-hidden) unless it has a `title`. `fav` fills it (a marked star). */
export function Icon({name, class: cls = "", title, fav}: {name: IconName; class?: string; title?: string; fav?: boolean}) {
  const markup = (title ? `<title>${title.replace(/[<&]/g, "")}</title>` : "") + ICONS[name];
  return <svg class={`u-ic${fav ? " u-fav" : ""}${cls ? " " + cls : ""}`} viewBox="0 0 24 24"
              aria-hidden={title ? undefined : "true"} role={title ? "img" : undefined}
              dangerouslySetInnerHTML={{__html: markup}} />;
}

/** The mark (logo/mark.svg): an A-frame in perspective, the line rising from an anchor on the ground,
 *  hung under the frame and running off into the distance. Coloured by the tokens (ui.css). */
export function LogoMark() {
  return <span class="u-lw" dangerouslySetInnerHTML={{__html: MARK_SVG}} />;
}

/** The logo (`name={false}`: the mark only): logo/logo.svg, "Find" in outlines and the line of the mark
 *  writing "line"; the name stays as text for screen readers. web.py's pages take the same file from dist/. */
export function Logo({name = true}: {name?: boolean}) {
  if (!name) return <span class="u-logo"><LogoMark /></span>;
  return <span class="u-logo"><span class="u-lw" dangerouslySetInnerHTML={{__html: LOGO_SVG}} />
    <span class="u-sr">Find-a-Line</span></span>;
}
