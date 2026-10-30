// ?ui=kit: every base component in the light and the dark theme side by side, for review and tests.
// The toggle shows one column in the chosen theme instead (remembered for B and C).

import { useState } from "preact/hooks";
import { BottomNav, Button, Chip, Chips, Field, IconButton, Panel, Progress, Range, RangePair, Rail, Screen,
         Segmented, Sheet, Stat, Stats, StatusChip, Tabs, Toast, type Snap, type Theme } from "./components";
import { ICONS, Icon, Logo, type IconName } from "./icons";
import { storeTheme, storedTheme, uiHref } from "./choice";

function Column({theme}: {theme: Theme}) {
  const [mode, setMode] = useState<"point" | "area" | "cliffs" | "file">("point");
  const [tab, setTab] = useState<"lines" | "details">("lines");
  const [chips, setChips] = useState({clear: true, trees: false});
  const [extra, setExtra] = useState(["≤ 10 m Δh"]);
  const [length, setLength] = useState(120);
  const [span, setSpan] = useState<[number, number]>([30, 150]);
  const [nav, setNav] = useState<"search" | "searches" | "favourites">("search");
  const [snap, setSnap] = useState<Snap>("half");
  const [toast, setToast] = useState(true);
  return <Screen theme={theme} screen={false} class="u-gal-col">
    <h2 class="u-h u-xl">{{auto: "System theme", light: "Light", dark: "Dark"}[theme]}</h2>

    <section class="u-gal-sec"><h3>Logo and icons</h3>
      <Logo />
      <div class="u-gal-icons">{(Object.keys(ICONS) as IconName[]).map((n) =>
        <div key={n}><Icon name={n} />{n}</div>)}<div><Icon name="star" fav />star (fav)</div></div>
    </section>

    <section class="u-gal-sec"><h3>Buttons</h3>
      <div class="u-row">
        <Button kind="primary" icon="search">Find lines</Button>
        <Button icon="share">Share</Button>
        <Button kind="ghost" icon="back">Back</Button>
        <Button kind="danger">Delete</Button>
        <Button disabled>Disabled</Button>
      </div>
      <Button kind="primary" large full iconRight="next">Start search</Button>
      <div class="u-row">
        <IconButton icon="layers" label="Layers" />
        <IconButton icon="ruler" label="Measure" on />
        <IconButton icon="star" label="Favourite" fav plain />
        <IconButton icon="more" label="More" round />
      </div>
    </section>

    <section class="u-gal-sec"><h3>Chips</h3>
      <Chips>
        <Chip on={chips.clear} onClick={() => setChips({...chips, clear: !chips.clear})}>Clear</Chip>
        <Chip on={chips.trees} onClick={() => setChips({...chips, trees: !chips.trees})}>Over trees</Chip>
        {extra.map((e) => <Chip key={e} on onRemove={() => setExtra(extra.filter((x) => x !== e))}>{e}</Chip>)}
        <Chip add onClick={() => setExtra([...extra, `filter ${extra.length + 1}`])}>More filters</Chip>
      </Chips>
    </section>

    <section class="u-gal-sec"><h3>Segmented and tabs</h3>
      <Segmented label="Where" value={mode} onChange={setMode} options={[
        {value: "point", label: "Point", icon: "pin"}, {value: "area", label: "Area", icon: "poly"},
        {value: "cliffs", label: "Along cliffs", icon: "cliff"}, {value: "file", label: "File", icon: "file"}]} />
      <Tabs label="Result" value={tab} onChange={setTab} tabs={[
        {value: "lines", label: "Lines", icon: "list", badge: 42}, {value: "details", label: "Details", icon: "info"}]} />
    </section>

    <section class="u-gal-sec"><h3>Panel, fields and ranges</h3>
      <Panel title="Search settings" actions={<IconButton icon="close" label="Close" plain />}>
        <Field label="Longest line" value={`${length} m`} help="Lines up to this length are checked." htmlFor={`len-${theme}`}>
          <Range id={`len-${theme}`} label="Longest line" value={length} min={10} max={500} step={5} onInput={setLength} />
        </Field>
        <Field label="Length" value={`${span[0]}–${span[1]} m`}>
          <RangePair label="Length" low={span[0]} high={span[1]} min={10} max={500} step={5}
                     onInput={(lo, hi) => setSpan([lo, hi])} />
        </Field>
        <Field label="Place" help="A name, coordinates or a link." htmlFor={`place-${theme}`}>
          <input id={`place-${theme}`} class="u-input" placeholder="Żabi Koń" />
        </Field>
      </Panel>
    </section>

    <section class="u-gal-sec"><h3>Status, progress, stats</h3>
      <div class="u-row">
        <StatusChip status="running" /><StatusChip status="done" /><StatusChip status="waiting" />
        <StatusChip status="failed" /><StatusChip status="shared" />
      </div>
      <Progress value={0.62} label="Downloading · 62%" />
      <Progress value={null} label="Computing…" />
      <Stats><Stat value={142} unit="m" label="length" /><Stat value={38} unit="m" label="height" />
        <Stat value="4.1" unit="m" label="clearance" /><Stat value={12} label="variants" /></Stats>
    </section>

    <section class="u-gal-sec"><h3>Toast</h3>
      {toast ? <Toast action={{label: "Show results", onClick: () => setToast(false)}} onClose={() => setToast(false)}>
        Search "Żabi Koń" is done: 42 lines</Toast>
        : <Button onClick={() => setToast(true)}>Show the toast</Button>}
    </section>

    <section class="u-gal-sec"><h3>Rail, sheet and bottom navigation</h3>
      <div class="u-gal-stage">
        <Rail items={[{value: "search", label: "Search", icon: "search"},
                      {value: "searches", label: "Searches", icon: "clock", badge: 2},
                      {value: "favourites", label: "Favourites", icon: "star"}]}
              value={nav} onChange={setNav} top={<span class="u-mark"><Logo name={false} /></span>}
              bottom={<IconButton icon="user" label="Account" round plain />} />
        <Sheet label="Lines" desktop="sheet" snap={snap} onSnap={setSnap} peek={90}
               header={<div class="u-row"><b class="u-grow">42 lines</b><span class="u-small">{snap}</span></div>}>
          <p>Drag the handle, click it, or use the arrow keys on it.</p>
          {Array.from({length: 12}, (_, i) => <p key={i} class="u-small">Line {i + 1}</p>)}
        </Sheet>
      </div>
      <div class="u-gal-stage">
        <BottomNav items={[{value: "search", label: "Home", icon: "home"},
                           {value: "searches", label: "Search", icon: "plus"},
                           {value: "favourites", label: "Favourites", icon: "star", badge: 3}]}
                   value={nav} onChange={setNav} />
      </div>
    </section>
  </Screen>;
}

export function Gallery() {
  const [theme, setTheme] = useState<Theme | "both">("both");
  const choose = (t: Theme | "both") => { setTheme(t); if (t !== "both") storeTheme(t); };
  return <Screen theme={theme === "both" ? storedTheme() : theme} class="u-gallery">
    <header class="u-gal-top">
      <Logo />
      <b class="u-grow">Component gallery</b>
      <Segmented label="Theme" value={theme} onChange={choose} options={[
        {value: "both", label: "Both"}, {value: "auto", label: "Auto"}, {value: "light", label: "Light"}, {value: "dark", label: "Dark"}]} />
      <a href={uiHref("b")}>B</a><a href={uiHref("c")}>C</a><a href={uiHref("d")}>D</a>
    </header>
    <div class="u-gal-cols">
      {theme === "both" ? <><Column theme="light" /><Column theme="dark" /></> : <Column key={theme} theme={theme} />}
    </div>
  </Screen>;
}
