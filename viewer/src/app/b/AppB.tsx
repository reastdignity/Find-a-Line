// Prototype B (map-first, ?ui=b): one full-screen map (PlaceMap's Leaflet map) for the search, the
// running searches and the results; a slim rail on the left (Search · My searches · Favourites, Admin
// on the public site for administrators, OSM locally; the account at the bottom); floating panels over
// the map, bottom sheets on phones (a top bar with a menu instead of the rail). The route is the hash
// (route.ts): back and forward work, and a copied line link (#run=…&line=…) opens the line here.
// A search that finishes while the user is elsewhere shows a toast with "Show results".

import type { ComponentChildren } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import { api, user, type RunListed, type Schema, type SearchRequest } from "../api";
import { AdminPanel } from "../Admin";
import { OsmPanel } from "../OsmPanel";
import { PlaceMap } from "../PlaceMap";
import { FINISHED, useJobs } from "../useJobs";
import { DESKTOP, IconButton, Rail, Screen, Segmented, Sheet, Toast, useMedia, type NavItem,
         type Theme } from "../../ui/components";
import { Icon, Logo } from "../../ui/icons";
import { storedTheme, storeTheme, uiHref } from "../../ui/choice";
import { useUsage } from "../../search/useSearch";
import { forgetRun } from "../../result/load";
import { FavouritesB } from "./FavouritesB";
import { useJobAreas } from "./jobAreas";
import { ResultsB } from "./ResultsB";
import { allowedRoute, parseRoute, routeHash, type Route, type Section } from "./route";
import { SearchB } from "./SearchB";
import { runCounts, Searches } from "./Searches";
import "./b.css";

const LABELS: Record<Section, string> = {search: "Search", searches: "My searches", favourites: "Favourites",
                                         admin: "Admin", osm: "OpenStreetMap"};

interface Ready { jobId: string; runId: string | null; title: string; failed: boolean; text: string }

/** The theme of the page: ?theme=light|dark (screenshots, tests), else the remembered one. */
function firstTheme(): Theme {
  const asked = new URLSearchParams(location.search).get("theme");
  return asked === "light" || asked === "dark" ? asked : storedTheme();
}

/** The account: the user (public site), privacy and about, sign out, the theme, the other interfaces. */
function Account({theme, onTheme}: {theme: Theme; onTheme(theme: Theme): void}) {
  return <div class="b-account">
    {!user.local && <div class="b-account-user"><Icon name="user" /><b>{user.name}</b>
      {user.admin && <span class="u-small">administrator</span>}</div>}
    <Segmented label="Theme" value={theme} onChange={onTheme}
               options={[{value: "auto", label: "Auto"}, {value: "light", label: "Light"}, {value: "dark", label: "Dark"}]} />
    <div class="b-account-links">
      {!user.local && <><a href="/privacy" target="_blank">Privacy</a><a href="/about" target="_blank">About and data licences</a></>}
      <a href={uiHref("old")}>Old interface</a>
      <a href={uiHref("c")}>Try C</a>
    </div>
    {!user.local && <button type="button" class="u-btn u-ghost b-signout"
                            onClick={() => api.logout().finally(() => { location.href = "/"; })}>Sign out</button>}
  </div>;
}

export function AppB() {
  const wide = useMedia(DESKTOP);
  const [theme, setTheme] = useState<Theme>(firstTheme);
  const mapRef = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<PlaceMap | null>(null);
  const [schema, setSchema] = useState<Schema | null>(null);
  const [error, setError] = useState("");
  const [route, setRoute] = useState<Route>(() => allowedRoute(parseRoute(location.hash), user));
  const [nav, setNav] = useState(0);              // navigations seen (a new one re-opens the route's line)
  const back = useRef<Section>("searches");       // where a result's "back" leads without history
  const moved = useRef(false);                    // navigated inside the app: "back" is history.back()
  const jobs = useJobs();
  const usage = useUsage();
  const [runs, setRuns] = useState<RunListed[] | null>(null);
  const [sharing, setSharing] = useState(false);
  const [editing, setEditing] = useState<{request: SearchRequest | null; key: number}>({request: null, key: 0});
  const [ready, setReady] = useState<Ready | null>(null);
  const [menu, setMenu] = useState(false);        // the phone's menu, or the rail's account panel
  const seen = useRef(new Map<string, string>());  // the jobs' statuses seen
  const [following, setFollowing] = useState("");  // the job the search panel follows

  const refreshRuns = () => api.runs().then((r) => { setRuns(r.runs); setSharing(r.public); return r.runs; },
                                            (e) => { setError(e.message); return [] as RunListed[]; });

  useEffect(() => {
    const placeMap = new PlaceMap(mapRef.current!, {controls: "topright"});
    (window as any).FINDALINE_PLACE = placeMap;  // for tests and the browser console
    (window as any).FINDALINE_MAP = placeMap.map;
    setPlace(placeMap);
    api.schema().then(setSchema, (e) => setError(e.message));
    refreshRuns();
    const observer = new ResizeObserver(() => placeMap.map.invalidateSize());
    observer.observe(mapRef.current!);
    const changed = () => {
      moved.current = true;
      setRoute(allowedRoute(parseRoute(location.hash), user));
      setNav((n) => n + 1);
      setMenu(false);
    };
    window.addEventListener("hashchange", changed);
    return () => { window.removeEventListener("hashchange", changed); observer.disconnect(); placeMap.map.remove(); };
  }, []);

  useEffect(() => {
    if (route.section !== "run") back.current = route.section;
    place?.setSearchVisible(route.section === "search");
    document.title = route.section === "run" ? "Find-a-Line · results" : `Find-a-Line · ${LABELS[route.section]}`;
  }, [route, place]);

  // A search that finished since the page listed it: the toast, the runs and the usage again
  useEffect(() => {
    for (const job of jobs.jobs) {
      const before = seen.current.get(job.id);
      seen.current.set(job.id, job.status);
      if (!before || FINISHED.includes(before as any) || !FINISHED.includes(job.status)) continue;
      usage.refresh();
      refreshRuns();
      if (job.id === following && route.section === "search") continue;  // the panel shows it: not elsewhere
      if (job.status === "done") {
        api.runs().then(({runs: listed}) => {
          const run = listed.find((r) => r.id === job.run_id);
          const name = run?.name || job.title;
          setReady({jobId: job.id, runId: job.run_id, title: name, failed: false,
                    text: run ? `${name} is ready: ${runCounts(run)}` : `${name} is ready`});
        });
      } else if (job.status === "failed") {
        setReady({jobId: job.id, runId: null, title: job.title, failed: true, text: `${job.title} failed`});
      }
    }
  }, [jobs.jobs]);
  useJobAreas(place, schema, jobs.jobs, route.section === "searches");

  const go = (next: Route) => {
    const hash = routeHash(next);
    if (location.hash === hash) { setRoute(next); setNav((n) => n + 1); setMenu(false); return; }
    location.hash = hash;  // hashchange sets the route
  };
  const show = (runId: string, line: string | null = null) => {
    if (ready?.runId === runId) setReady(null);
    go({section: "run", run: runId, line});
  };
  const leaveResult = () => { if (moved.current) history.back(); else go({section: back.current}); };
  const edit = (jobId: string) => api.job(jobId).then((job) => {
    place?.clear();
    setEditing((e) => ({request: job.request, key: e.key + 1}));  // a new search panel with the job's values
    go({section: "search"});
  }, (e) => setError(e.message));
  const deleted = (runId: string) => { forgetRun(runId); setRuns((all) => all && all.filter((r) => r.id !== runId)); };
  const changeTheme = (t: Theme) => { setTheme(t); storeTheme(t); };

  const items: NavItem<Section>[] = [
    {value: "search", label: "Search", icon: "search"},
    {value: "searches", label: "My searches", icon: "list", badge: jobs.active || undefined},
    {value: "favourites", label: "Favourites", icon: "star"},
    ...(!user.local && user.admin ? [{value: "admin" as Section, label: "Admin", icon: "user" as const}] : []),
    ...(user.local ? [{value: "osm" as Section, label: "OSM", icon: "layers" as const}] : []),
  ];
  const section = route.section;
  const current: Section | null = section === "run" ? "searches" : section;

  const menuButton = <IconButton icon="menu" label="Menu" round on={menu} class="b-menu-button"
                                 onClick={() => setMenu(!menu)} />;
  const panel = (title: string, body: ComponentChildren, cls = "") =>
    <Sheet label={title} desktop="float" initial="half" peek={150}
           header={<h2 class={`u-h b-panel-title ${cls}`}>{title}</h2>}>{body}</Sheet>;

  return <Screen theme={theme} class={`u-app-b b-app b-in-${section}${wide ? "" : " b-phone"}`}>
    {wide && <Rail items={items} value={current} onChange={(s) => go({section: s})}
                   top={<span class="u-mark b-mark" title="Find-a-Line"><Logo name={false} /></span>}
                   bottom={<button type="button" class={`u-rail-item b-account-button${menu ? " u-on" : ""}`}
                                   aria-expanded={menu} onClick={() => setMenu(!menu)}>
                     <Icon name="user" />{user.local ? "Settings" : "Account"}</button>} />}
    <div class="b-stage">
      <div class="b-map" ref={mapRef} />
      {place && schema && <>
        <div class="b-layer" hidden={section !== "search"}>
          <SearchB key={editing.key} place={place} schema={schema} initial={editing.request} jobs={jobs} usage={usage}
                   runs={runs} wide={wide} onShow={(id) => show(id)} onEdit={edit}
                   onFollow={setFollowing} />
        </div>
        {section === "searches" && <div class="b-layer">{panel("My searches",
          <Searches jobs={jobs} runs={runs} sharing={sharing} usage={usage.usage} onShow={(id) => show(id)}
                    onEdit={edit} onDeleted={deleted} />)}</div>}
        {section === "favourites" && <div class="b-layer">{panel("Favourites",
          <FavouritesB place={place} onOpen={(run, line) => show(run, line)} />)}</div>}
        {section === "admin" && user.admin && <div class="b-layer">{panel("Admin",
          <div class="b-legacy"><AdminPanel place={place} jobs={jobs.jobs} onStarted={() => go({section: "searches"})} /></div>)}
        </div>}
        {section === "osm" && user.local && <div class="b-layer">{panel("OpenStreetMap extracts",
          <div class="b-legacy"><OsmPanel place={place} onStarted={() => go({section: "searches"})} /></div>)}</div>}
        {section === "run" && <div class="b-layer">
          <ResultsB place={place} runId={route.run} line={route.line} nav={nav} wide={wide} runs={runs}
                    onBack={leaveResult} onRun={(id) => show(id)} menu={menuButton} />
        </div>}
      </>}
      {!wide && section !== "run" && <div class="b-top">{menuButton}
        <span class="b-top-title"><Logo name={false} />{LABELS[section]}</span>
        {jobs.active > 0 && section !== "searches" &&
          <button type="button" class="b-top-jobs" onClick={() => go({section: "searches"})}>
            <span class="r-spin" />{jobs.active} running</button>}
      </div>}
      {error && <p class="b-error" role="alert">{error}</p>}
      {ready && !(section === "run" && route.run === ready.runId) && <div class="b-toast">
        <Toast action={ready.failed ? {label: "Details", onClick: () => { setReady(null); go({section: "searches"}); }}
                 : ready.runId ? {label: "Show results", onClick: () => show(ready.runId!)} : undefined}
               onClose={() => setReady(null)}>
          <Icon name={ready.failed ? "warn" : "check"} /> {ready.text}</Toast>
      </div>}
      {menu && <div class="b-pop u-panel" role="dialog" aria-label={wide ? "Account" : "Menu"}>
        {!wide && <nav class="b-menu" aria-label="Main">{items.map((it) =>
          <button type="button" key={it.value} class={it.value === current ? "u-on" : ""}
                  aria-current={it.value === current ? "page" : undefined} onClick={() => go({section: it.value})}>
            <Icon name={it.icon} />{it.label}{it.badge ? <span class="u-badge">{it.badge}</span> : null}</button>)}
        </nav>}
        <Account theme={theme} onTheme={changeTheme} />
      </div>}
    </div>
  </Screen>;
}
