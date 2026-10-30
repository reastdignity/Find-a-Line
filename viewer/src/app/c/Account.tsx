// C's account: the user (the public site: name, privacy and about, sign out; Admin for administrators),
// the OSM extracts locally and for administrators, the theme and the way to the other interfaces. A menu
// under the header's account button on desktops, a page (#account) on phones. The Admin page (AdminC) is a
// page of panels; the OSM page (OsmC) a panel next to the map it needs (the regions around its centre).

import { useEffect, useRef, useState } from "preact/hooks";
import type { ComponentChildren } from "preact";
import { Button, Segmented, type Theme } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { storeTheme, uiHref, type UiChoice } from "../../ui/choice";
import { Popover } from "../../result/view";
import { api, user } from "../api";
import { canReport, openReport } from "../../report";
import { PlaceMap } from "../PlaceMap";
import type { Job } from "../api";
import { AdminC } from "./AdminC";
import { OsmC } from "./OsmC";
import { go } from "./route";

const THEMES = [{value: "auto" as const, label: "Auto"}, {value: "light" as const, label: "Light"},
                {value: "dark" as const, label: "Dark"}];

/** The other interfaces the account links to (C: B). */
export const TRIES_C: UiChoice[] = ["b"];

export function AccountBody({theme, setTheme, onPick, tries = TRIES_C}:
                            {theme: Theme; setTheme(t: Theme): void; onPick?(): void; tries?: UiChoice[]}) {
  const pick = () => onPick?.();
  return <div class="c-account">
    <div class="c-account-who">
      <span class="c-avatar"><Icon name="user" /></span>
      <span><b>{user.local ? "This computer" : user.name}</b>
        <span class="u-small">{user.local ? "the local app: no account" : user.admin ? "administrator" : "signed in"}</span></span>
    </div>
    <nav class="c-account-links">
      {!user.local && user.admin && <a class="c-menu-item" href="#admin" onClick={pick}><Icon name="user" />Admin</a>}
      {(user.local || user.admin) && <a class="c-menu-item" href="#osm" onClick={pick}><Icon name="layers" />OpenStreetMap extracts</a>}
      {canReport() && <button type="button" class="c-menu-item" onClick={() => { pick(); openReport(); }}>
        <Icon name="warn" />Report a problem</button>}
      {!user.local && <a class="c-menu-item" href="/privacy" target="_blank"><Icon name="info" />Privacy</a>}
      {!user.local && <a class="c-menu-item" href="/about" target="_blank"><Icon name="info" />About and data licences</a>}
      {tries.map((ui) => <a key={ui} class="c-menu-item" href={uiHref(ui)}><Icon name="next" />Try {ui.toUpperCase()}</a>)}
    </nav>
    <div class="c-account-theme"><span class="u-small">Theme</span>
      <Segmented label="Theme" options={THEMES} value={theme} onChange={(t) => { storeTheme(t); setTheme(t); }} /></div>
    {!user.local && <Button kind="danger" class="c-signout"
                            onClick={() => api.logout().finally(() => { location.href = "/"; })}>Sign out</Button>}
  </div>;
}

/** The header's account button with its menu. */
export function AccountMenu({theme, setTheme, tries}: {theme: Theme; setTheme(t: Theme): void; tries?: UiChoice[]}) {
  const [open, setOpen] = useState(false);
  return <Popover label="Account" align="right" open={open} onOpen={setOpen}
                  button={(on, toggle) => <button type="button" class={`c-account-btn${on ? " u-on" : ""}`}
                                                  aria-expanded={on} onClick={toggle}>
                    <span class="c-avatar"><Icon name="user" /></span>
                    <span class="c-account-name">{user.local ? "Local" : user.name}</span>
                    <Icon name="down" /></button>}>
    <AccountBody theme={theme} setTheme={setTheme} onPick={() => setOpen(false)} tries={tries} />
  </Popover>;
}

/** A page with a panel on the left and the map it uses on the right (below it on phones). */
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
    <aside class="c-sidepanel" aria-label={title}>
      <h1 class="u-h">{title}</h1>
      {place && children(place)}
    </aside>
    <div class="c-newmap" ref={ref} />
  </div>;
}

export function AdminPage({jobs}: {jobs: Job[]}) {
  if (user.local || !user.admin) return <div class="c-page"><p>Administrators of the public site only.</p></div>;
  return <AdminC jobs={jobs} />;
}

export function OsmPage() {
  if (!user.local && !user.admin) return <div class="c-page"><p>The local app and administrators only.</p></div>;
  return <MapPage title="OpenStreetMap extracts">{(place) => <OsmC place={place} onStarted={() => go("#home")} />}</MapPage>;
}

