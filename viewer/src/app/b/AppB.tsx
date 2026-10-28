// Prototype B (map-first): a rail on the left, floating panels over the map. A stub of wave 1;
// wave 3 replaces it.

import { useState } from "preact/hooks";
import { Button, Panel, Rail, Screen, type NavItem } from "../../ui/components";
import { Logo } from "../../ui/icons";
import { storedTheme, uiHref } from "../../ui/choice";

type Place = "search" | "searches" | "favourites";
const NAV: NavItem<Place>[] = [
  {value: "search", label: "Search", icon: "search"},
  {value: "searches", label: "Searches", icon: "clock"},
  {value: "favourites", label: "Favourites", icon: "star"},
];

export function AppB() {
  const [place, setPlace] = useState<Place>("search");
  return <Screen theme={storedTheme()} class="u-app-b">
    <Rail items={NAV} value={place} onChange={setPlace} top={<span class="u-mark"><Logo name={false} /></span>} />
    <main class="u-stub">
      <Logo />
      <Panel title="Prototype B: map first">
        <p>Coming in wave 3: the map with floating panels, the search on the map and the results in the same view.</p>
        <Button kind="ghost" icon="back" href={uiHref("old")}>Old interface</Button>
      </Panel>
    </main>
  </Screen>;
}
