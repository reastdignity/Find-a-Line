// A run's own map (map.html, from disk, from the app's server or a share link): the result as the app
// shows it (result/view/RunOnMap). map.html loads this bundle after its data (data.js:
// window.FINDALINE_RUN) and the list of runs (../runs.js: window.FINDALINE_RUNS, none on a share
// link); the lines of other runs come from their lines.js (Compare). The selected line is the hash,
// #line=lat1,lon1,lat2,lon2 (keys.ts lineCoords), so a copied link opens it.

import "../leaflet";
import { render } from "preact";
import { useEffect, useState } from "preact/hooks";
import "../app/base.css";
import type { ResultModel } from "../result/model";
import { RunOnMap } from "../result/view/RunOnMap";
import type { BackAction } from "../result/view/ResultWorkspace";
import { DESKTOP, Screen, useMedia } from "../ui/components";
import { storedTheme } from "../ui/choice";
import type { Line, RunData } from "../types";

const hashOf = (_runId: string, line: string | null) => line ? "#line=" + encodeURIComponent(line) : "";
const here = () => location.href.replace(/#.*$/, "");

/** "Copy link": this map with the line, or the map of the other run the line is from. */
function linkOf(model: ResultModel, l: Line): string {
  const id = model.runOf(l), hash = hashOf(id, model.lineLink(l));
  return id === model.runId ? here() + hash : new URL("../" + encodeURIComponent(id) + "/map.html" + hash, here()).href;
}

/** The way back: the list of runs beside the map on disk, the app on its server, none on a share link. */
function backOf(runId: string): BackAction | undefined {
  if (location.protocol === "file:") return {label: "All runs", text: true, onClick: () => { location.href = "../index.html"; }};
  if (location.pathname.startsWith("/s/")) return undefined;
  return {label: "Find-a-Line", text: true, onClick: () => { location.href = "/#run=" + encodeURIComponent(runId); }};
}

function RunMap({data}: {data: RunData}) {
  const wide = useMedia(DESKTOP);
  const [route, setRoute] = useState({line: location.hash || null, nav: 0});
  useEffect(() => {
    const changed = () => setRoute((r) => ({line: location.hash || null, nav: r.nav + 1}));
    addEventListener("hashchange", changed);
    return () => removeEventListener("hashchange", changed);
  }, []);
  // Compare needs other runs with lines in the list of runs, which a share link has none of
  const others = (window.FINDALINE_RUNS || []).some((r) => r.id !== data.run.id && r.has_lines);
  const show = others ? undefined : (["measure", "share", "download"] as const).slice();
  return <Screen theme={storedTheme()}>
    <RunOnMap data={data} runId={data.run.id} line={route.line} nav={route.nav} wide={wide} back={backOf(data.run.id)}
              hashOf={hashOf} linkOf={linkOf} base="../" folder="" show={show} />
  </Screen>;
}

const data = window.FINDALINE_RUN, root = document.getElementById("map")!;
if (data) render(<RunMap data={data} />, root);
else root.textContent = "The run's data (data.js) could not be loaded.";
