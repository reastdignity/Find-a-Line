// C's account: the user (the public site: name, privacy and about, sign out; Admin for administrators),
// the OSM extracts locally, the theme and the way to the other interfaces. A menu under the header's
// account button on desktops, a page (#account) on phones. The Admin and OSM pages embed the old app's
// panels next to a map they need (the OSM regions around its centre).

import { useEffect, useRef, useState } from "preact/hooks";
import type { ComponentChildren } from "preact";
import { Button, Segmented, type Theme } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { storeTheme, uiHref } from "../../ui/choice";
import { Popover } from "../../result/view";
import { AdminPanel } from "../Admin";
import { api, user } from "../api";
import { OsmPanel } from "../OsmPanel";
import { PlaceMap } from "../PlaceMap";
import type { Job } from "../api";
import { go } from "./route";

const THEMES = [{value: "auto" as const, label: "Auto"}, {value: "light" as const, label: "Light"},
                {value: "dark" as const, label: "Dark"}];

export function AccountBody({theme, setTheme, onPick}: {theme: Theme; setTheme(t: Theme): void; onPick?(): void}) {
  const pick = () => onPick?.();
  return <div class="c-account">
    <div class="c-account-who">
      <span class="c-avatar"><Icon name="user" /></span>
      <span><b>{user.local ? "This computer" : user.name}</b>
        <span class="u-small">{user.local ? "the local app: no account" : user.admin ? "administrator" : "signed in"}</span></span>
    </div>
    <nav class="c-account-links">
      {!user.local && user.admin && <a class="c-menu-item" href="#admin" onClick={pick}><Icon name="user" />Admin</a>}
      {user.local && <a class="c-menu-item" href="#osm" onClick={pick}><Icon name="layers" />OpenStreetMap extracts</a>}
      {!user.local && <a class="c-menu-item" href="/privacy" target="_blank"><Icon name="info" />Privacy</a>}
      {!user.local && <a class="c-menu-item" href="/about" target="_blank"><Icon name="info" />About and data licences</a>}
      <a class="c-menu-item" href={uiHref("old")}><Icon name="back" />Old interface</a>
      <a class="c-menu-item" href={uiHref("b")}><Icon name="next" />Try B</a>
    </nav>
    <div class="c-account-theme"><span class="u-small">Theme</span>
      <Segmented label="Theme" options={THEMES} value={theme} onChange={(t) => { storeTheme(t); setTheme(t); }} /></div>
    {!user.local && <Button kind="danger" class="c-signout"
                            onClick={() => api.logout().finally(() => { location.href = "/"; })}>Sign out</Button>}
  </div>;
}

/** The header's account button with its menu. */
export function AccountMenu({theme, setTheme}: {theme: Theme; setTheme(t: Theme): void}) {
  const [open, setOpen] = useState(false);
  return <Popover label="Account" align="right" open={open} onOpen={setOpen}
                  button={(on, toggle) => <button type="button" class={`c-account-btn${on ? " u-on" : ""}`}
                                                  aria-expanded={on} onClick={toggle}>
                    <span class="c-avatar"><Icon name="user" /></span>
                    <span class="c-account-name">{user.local ? "Local" : user.name}</span>
                    <Icon name="down" /></button>}>
    <AccountBody theme={theme} setTheme={setTheme} onPick={() => setOpen(false)} />
  </Popover>;
}

/** A page with an old panel on the left and the map it uses on the right. */
export function MapPage({title, children}: {title: string; children: (place: PlaceMap) => ComponentChildren}) {
  const ref = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<PlaceMap | null>(null);
  useEffect(() => {
    const placeMap = new PlaceMap(ref.current!, {controls: "bottomright"});
    placeMap.setSearchVisible(false);
    (window as any).FINDALINE_PLACE = placeMap;
    setPlace(placeMap);
    const observer = new ResizeObserver(() => placeMap.map.invalidateSize());
    observer.observe(ref.current!);
    return () => { observer.disconnect(); placeMap.map.remove(); };
  }, []);
  return <div class="c-mappage">
    <aside class="c-oldpanel" aria-label={title}>
      <h1 class="u-h">{title}</h1>
      {place && children(place)}
    </aside>
    <div class="c-newmap" ref={ref} />
  </div>;
}

export function AdminPage({jobs}: {jobs: Job[]}) {
  if (user.local || !user.admin) return <div class="c-page"><p>Administrators of the public site only.</p></div>;
  return <MapPage title="Admin">{(place) => <AdminPanel place={place} jobs={jobs} onStarted={() => go("#home")} />}</MapPage>;
}

export function OsmPage() {
  if (!user.local) return <div class="c-page"><p>On the local app only.</p></div>;
  return <MapPage title="OpenStreetMap extracts">{(place) =>
    <><p class="u-small">The regions around the map's centre.</p>
      <OsmPanel place={place} onStarted={() => go("#home")} /></>}</MapPage>;
}

