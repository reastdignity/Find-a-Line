// Prototype C (dashboard): home cards, a focused search page, a three-column result, the bottom
// navigation on phones. A stub of wave 1; wave 3 replaces it.

import { useState } from "preact/hooks";
import { BottomNav, Button, DESKTOP, Panel, Screen, useMedia, type NavItem } from "../../ui/components";
import { Logo } from "../../ui/icons";
import { storedTheme, uiHref } from "../../ui/choice";

type Page = "home" | "search" | "favourites";
const NAV: NavItem<Page>[] = [
  {value: "home", label: "Home", icon: "home"},
  {value: "search", label: "New search", icon: "plus"},
  {value: "favourites", label: "Favourites", icon: "star"},
];

export function AppC() {
  const [page, setPage] = useState<Page>("home");
  const wide = useMedia(DESKTOP);
  return <Screen theme={storedTheme()} class="u-app-c">
    <main class="u-stub">
      <Logo />
      <Panel title="Prototype C: dashboard">
        <p>Coming in wave 3: your searches as cards, favourites as a trip list, a focused search page and a
          three-column result.</p>
        <Button kind="ghost" icon="back" href={uiHref("old")}>Old interface</Button>
      </Panel>
    </main>
    {!wide && <BottomNav items={NAV} value={page} onChange={setPage} />}
  </Screen>;
}
