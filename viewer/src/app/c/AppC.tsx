// Prototype C (dashboard): home with the searches as cards and the favourite lines as a trip list
// (Home), a focused search page (NewSearch), a three-column result (ResultPage), the account; pages
// in the hash (route.ts), so Back and Forward work. On phones (< 768 px) the cards stack and a bottom
// navigation leads to Home, Favourites and the account; the result is a map with a sheet.
// Styles in c.css (classes c-*, under the kit's .ui).

import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { BottomNav, DESKTOP, Panel, Screen, useMedia, type NavItem, type Theme } from "../../ui/components";
import { Icon, Logo } from "../../ui/icons";
import { storedTheme } from "../../ui/choice";
import { useUsage } from "../../search";
import { api, type RunListed, type Schema } from "../api";
import { useJobs } from "../useJobs";
import { AccountBody, AccountMenu, AdminPage, OsmPage } from "./Account";
import { Home, type RunsState } from "./Home";
import { NewSearch } from "./NewSearch";
import { ResultPage } from "./ResultPage";
import { go, parseRoute, type Route } from "./route";
import { Trip } from "./Trip";
import "./c.css";

type Nav = "home" | "favourites" | "account";
const NAV: NavItem<Nav>[] = [
  {value: "home", label: "Home", icon: "home"},
  {value: "favourites", label: "Favourites", icon: "star"},
  {value: "account", label: "Account", icon: "user"},
];

function initialTheme(): Theme {
  const asked = new URLSearchParams(location.search).get("theme");
  return asked === "light" || asked === "dark" ? asked : storedTheme();
}

/** The saved runs (api.runs), listed again on reload(). */
function useRuns(): RunsState {
  const [runs, setRuns] = useState<RunListed[] | null>(null);
  const [isPublic, setPublic] = useState(false);
  const [error, setError] = useState("");
  const reload = useCallback(() => {
    api.runs().then((r) => { setRuns(r.runs); setPublic(r.public); setError(""); }, (e) => setError(e.message));
  }, []);
  useEffect(reload, [reload]);
  const change = (id: string, change: Partial<RunListed> | null) => setRuns((now) => now && (change === null
    ? now.filter((r) => r.id !== id) : now.map((r) => r.id === id ? {...r, ...change} : r)));
  return {runs, public: isPublic, error, reload, change};
}

/** The desktop header: the logo, Home and Favourites, the account. */
function Header({route, theme, setTheme}: {route: Route; theme: Theme; setTheme(t: Theme): void}) {
  const on = (page: Route["page"]) => route.page === page ? "c-nav-item u-on" : "c-nav-item";
  return <header class="c-top">
    <a class="c-logo" href="#home" aria-label="Find-a-Line: home"><Logo /></a>
    <nav class="c-nav" aria-label="Main">
      <a class={on("home")} href="#home" aria-current={route.page === "home" ? "page" : undefined}><Icon name="home" />Home</a>
      <a class={on("favourites")} href="#favourites" aria-current={route.page === "favourites" ? "page" : undefined}>
        <Icon name="star" />Favourites</a>
    </nav>
    <span class="u-grow" />
    <AccountMenu theme={theme} setTheme={setTheme} />
  </header>;
}

/** The phones' header of the pages with the bottom navigation. */
function PhoneHeader() {
  return <header class="c-top c-top-phone"><a class="c-logo" href="#home" aria-label="Find-a-Line: home"><Logo /></a></header>;
}

export function AppC() {
  const wide = useMedia(DESKTOP);
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [route, setRoute] = useState<Route>(() => parseRoute(location.hash));
  const [schema, setSchema] = useState<Schema | null>(null);
  const [highlight, setHighlight] = useState("");
  const [error, setError] = useState("");
  const jobs = useJobs();
  const usage = useUsage();
  const runs = useRuns();

  useEffect(() => {
    const changed = () => setRoute(parseRoute(location.hash));
    window.addEventListener("hashchange", changed);
    api.schema().then(setSchema, (e) => setError(e.message));
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  useEffect(() => { if (route.page !== "home") setHighlight(""); }, [route.page]);

  // A search that ends saves a run: list the runs again, the quotas too
  const done = jobs.jobs.filter((j) => j.status === "done").map((j) => j.id).join();
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    runs.reload();
    usage.refresh();
  }, [done]);

  const started = (id: string) => {
    setHighlight(id);
    jobs.refresh();
    usage.refresh();
    go("#home", true);  // the new card, highlighted; Back does not return to the filled form
  };
  const editJob = route.page === "run" ? jobs.jobs.find((j) => j.run_id === route.id)?.id || null : null;
  const nav: Nav | null = route.page === "home" || route.page === "favourites" || route.page === "account" ? route.page
    : null;
  const focused = route.page === "new" || route.page === "run";  // pages with their own top bar

  let page;
  switch (route.page) {
    case "new":
      page = <NewSearch key={route.edit || "new"} edit={route.edit} schema={schema} usage={usage} wide={wide}
                        onStarted={started} />;
      break;
    case "run":
      page = <ResultPage key={route.id} id={route.id} line={route.line} editJob={editJob} wide={wide} />;
      break;
    case "favourites":
      page = <div class="c-page"><Panel title="Favourite lines" class="c-favs c-favs-page"><Trip full /></Panel></div>;
      break;
    case "account":
      page = <div class="c-page"><Panel title="Account"><AccountBody theme={theme} setTheme={setTheme} /></Panel></div>;
      break;
    case "admin":
      page = <AdminPage jobs={jobs.jobs} />;
      break;
    case "osm":
      page = <OsmPage />;
      break;
    default:
      page = <Home jobs={jobs} runs={runs} usage={usage} highlight={highlight} wide={wide} />;
  }

  return <Screen theme={theme} class={`u-app-c c-app${wide ? " c-wide" : " c-narrow"}${nav && !wide ? " c-has-nav" : ""}`}>
    {!focused && (wide ? <Header route={route} theme={theme} setTheme={setTheme} /> : <PhoneHeader />)}
    {error && <p class="c-error c-app-error">{error}</p>}
    <main class={`c-main c-main-${route.page}`}>{page}</main>
    {!wide && nav && <BottomNav items={NAV} value={nav} onChange={(v) => go("#" + v)} />}
  </Screen>;
}
