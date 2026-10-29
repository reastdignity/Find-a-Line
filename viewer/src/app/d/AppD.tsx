// Prototype D (?ui=d): C's home, new search, favourites and account (ShellC and its pages, app/c) with
// B's way of working with a finished run (result/view/ResultWorkspace, shared with B) on its own
// full-screen map: the pill "‹ Home", Edit & run again and the run's actions on top, the panel on the
// right with the list and the selected line's details, the profile docked at the bottom; on phones
// C's bottom navigation and B's sheet ("Spot k of N" with ‹ ›). The selected line is kept in C's hash
// (#run=<id>&line=…, replaced), and a copied link opens it in D. Styles in d.css (classes d-*).

import L from "leaflet";
import { useEffect, useRef, useState } from "preact/hooks";
import { Button } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { uiHref, type UiChoice } from "../../ui/choice";
import { baseLayers } from "../../layers";
import type { Line, RunData } from "../../types";
import type { ResultModel } from "../../result/model";
import { orthoForApp } from "../../result/load";
import { ResultWorkspace } from "../../result/view/ResultWorkspace";
import { ShellC, type ResultPageProps } from "../c/AppC";
import { editHash, go, runHash } from "../c/route";
import "../c/c.css";
import "./d.css";

const TRIES: UiChoice[] = ["old", "b", "c"];

/** The link "Copy link" gives: this page in D with the line's hash, whichever interface is remembered. */
const linkOf = (model: ResultModel, l: Line) => location.origin + uiHref("d") + runHash(model.runOf(l), model.lineLink(l));
const back = {label: "Home", text: true, onClick: () => go("#home")};

/** The result: its own map (base layers once the run's data is there) and the workspace over it. */
function ResultD({id, line, nav, editJob, wide}: ResultPageProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<L.Map | null>(null);
  useEffect(() => {
    const leaflet = L.map(ref.current!, {zoomControl: false, center: [50, 19.75], zoom: 5});
    L.control.zoom({position: "topright"}).addTo(leaflet);
    (window as any).FINDALINE_MAP = leaflet;  // for tests and the browser console
    setMap(leaflet);
    const observer = new ResizeObserver(() => leaflet.invalidateSize());
    observer.observe(ref.current!);
    return () => { observer.disconnect(); leaflet.remove(); };
  }, []);
  const loaded = (data: RunData) => {
    if (!map) return;
    map.setMaxZoom(data.max_zoom);
    const layers = baseLayers(orthoForApp(data.ortho, data.run.id), data.max_zoom);
    Object.values(layers)[0].addTo(map);
    L.control.layers(layers, undefined, {position: "topright"}).addTo(map);
    L.control.scale({position: "bottomleft"}).addTo(map);
  };
  const actions = (compact: boolean) => editJob && (compact
    ? <a class="u-ib u-round" href={editHash(editJob)} aria-label="Edit & run again" title="Edit & run again">
        <Icon name="edit" /></a>
    : <Button icon="edit" href={editHash(editJob)}>Edit &amp; run again</Button>);

  return <div class={`d-result ${wide ? "d-wide" : "d-phone"}`}>
    <div class="d-map" ref={ref} />
    {map && <div class="d-layer">
      <ResultWorkspace map={map} runId={id} line={line} nav={nav} wide={wide} back={back} hashOf={runHash}
                       linkOf={linkOf} actions={actions} onData={loaded} />
    </div>}
  </div>;
}

export function AppD() {
  return <ShellC ui="u-app-d d-app" tries={TRIES} result={(props) => <ResultD key={props.id} {...props} />} />;
}
