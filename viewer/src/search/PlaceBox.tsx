// The place box of the search panel: a name looked up on Enter (/api/place: Nominatim through the
// worker, whose usage policy allows no lookups while typing), up to five matches listed; picking one
// moves the map there (the centre of a search is still set by a click). An own component rather than
// leaflet-control-geocoder: that one would need an adapter to /api/place all the same, brings its own
// light styles and sits on the map, not in the panel (docs/roadmap.md, UI prototypes).

import { useRef, useState } from "preact/hooks";
import { api, type Place } from "../app/api";
import type { PlaceMap } from "../app/PlaceMap";
import { Icon } from "../ui/icons";

let ids = 0;

export function PlaceBox({place}: {place: PlaceMap}) {
  const [id] = useState(() => `s-place-${++ids}`);
  const [text, setText] = useState("");
  const [found, setFound] = useState<Place[] | null>(null);  // null: nothing looked up (or closed)
  const [active, setActive] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const asked = useRef(0);  // the newest lookup; older answers are dropped

  const lookUp = () => {
    const q = text.trim();
    if (!q || busy) return;
    const n = ++asked.current;
    setBusy(true);
    setError("");
    api.places(q).then((r) => {
      if (n !== asked.current) return;
      setFound(r.places);
      setActive(0);
    }, (e: Error) => {
      if (n === asked.current) { setFound(null); setError(e.message); }
    }).finally(() => { if (n === asked.current) setBusy(false); });
  };

  const pick = (p: Place) => {
    if (p.bbox) place.map.fitBounds([[p.bbox[0], p.bbox[1]], [p.bbox[2], p.bbox[3]]], {maxZoom: 15});
    else place.map.setView([p.lat, p.lon], 15);
    setText(p.name.split(",")[0]);
    setFound(null);
  };

  const onKey = (e: KeyboardEvent) => {
    const list = found && found.length ? found : null;
    if (e.key === "Enter") {
      e.preventDefault();
      if (list) pick(list[active]); else lookUp();
    } else if (e.key === "Escape" && found) {
      e.preventDefault();
      setFound(null);
    } else if (list && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      setActive((active + (e.key === "ArrowDown" ? 1 : list.length - 1)) % list.length);
    }
  };

  const open = !!found && found.length > 0;
  return <div class="s-placebox">
    <div class="s-placebox-input">
      <Icon name="search" />
      <input id={id} class="u-input" type="search" value={text} placeholder="Go to a place (Enter)"
             aria-label="Go to a place" role="combobox" list={undefined} aria-expanded={open}
             aria-controls={`${id}-list`}
             aria-activedescendant={open ? `${id}-${active}` : undefined} autocomplete="off"
             onInput={(e) => { setText(e.currentTarget.value); setFound(null); }} onKeyDown={onKey} />
    </div>
    {busy && <p class="s-hint">Looking up…</p>}
    {error && <p class="s-hint s-placebox-error" role="alert">{error}</p>}
    {found && !found.length && <p class="s-hint">Nothing found by that name.</p>}
    {open && <ul class="s-placebox-list" id={`${id}-list`} role="listbox" aria-label="Places">
      {found!.map((p, i) => <li key={i} id={`${id}-${i}`} role="option" aria-selected={i === active}
                                class={i === active ? "u-on" : undefined}
                                onMouseDown={(e) => e.preventDefault()} onClick={() => pick(p)}>
        <b>{p.name.split(",")[0]}</b>
        <span class="u-small">{p.name.split(",").slice(1).join(",").trim()}{p.type && ` · ${p.type}`}</span>
      </li>)}
    </ul>}
  </div>;
}
