// A test page of result/ResultMap (tests/test_viewer.py builds it into a run's folder and opens it
// in headless Edge from the app's server): loadRun, a model, its map, a selection, measuring and
// destroy; the findings go into <pre id="result"> as JSON.

import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { loadRun } from "../src/result/load";
import { ResultModel } from "../src/result/model";
import { ResultMap } from "../src/result/ResultMap";

const out: Record<string, unknown> = {};
const finish = () => {
  const pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
};
const runId = decodeURIComponent(location.pathname.split("/")[2]);

(async () => {
  try {
    const failed = await loadRun("no such run").then(() => false, () => true);
    const data = await loadRun(runId);
    out.load = {failed, lines: data.lines.length, cleared: window.FINDALINE_RUN === undefined,
                cached: (await loadRun(runId)) === data};
    const div = document.createElement("div");
    div.id = "map";
    div.style.width = "800px";
    div.style.height = "600px";
    document.body.appendChild(div);
    const map = L.map(div, {center: data.center, zoom: 16});
    const before = Object.keys((map as any)._layers).length;
    const model = new ResultModel(data, {favourites: false, profiles: false});
    const clicked: string[] = [];
    const view = new ResultMap(map, model, {onLine: (l, g, variant) => {
      clicked.push(variant ? "variant" : "line");
      if (variant) model.choose(g, l); else model.select(l);
    }});
    view.fitArea();
    const count = () => Object.keys((map as any)._layers).length;
    out.drawn = count() - before;
    // Selecting a group: its line, halo, anchors and variants drawn on top
    const group = model.groups.find((g) => g.members.length > 2)!;
    model.select(group);
    view.zoomTo(model.selected!.line, {top: 0, right: 300, bottom: 200, left: 0}, false);
    out.selectedLayers = count() - before;
    view.highlight(group.members[1]);
    view.hoverAt([group.rep.lat1, group.rep.lon1]);
    // A click on a variant (the middle of the line, in screen pixels) through Leaflet's canvas
    const variant = group.members.find((m) => m !== model.selected!.line)!;
    const p = map.latLngToContainerPoint([(variant.lat1 + variant.lat2) / 2, (variant.lon1 + variant.lon2) / 2]);
    const canvas: HTMLCanvasElement = (view as any).variantRenderer._container;  // the variants' own canvas
    const fire = (type: string, x: number, y: number) => {
      const r = div.getBoundingClientRect();
      canvas.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, clientX: r.left + x,
                                                 clientY: r.top + y}));
    };
    fire("mousemove", p.x, p.y);
    fire("click", p.x, p.y);
    out.clicked = clicked;
    out.variantShown = model.selected?.line === variant;
    // Measuring: two clicks on the map
    let measured: number[] | null = null;
    view.startMeasure((a, b) => { measured = [a.lat, b.lat]; });
    map.fire("click", {latlng: L.latLng(data.center), containerPoint: L.point(0, 0)} as any);
    map.fire("click", {latlng: L.latLng(data.center[0] + 0.001, data.center[1]), containerPoint: L.point(0, 0)} as any);
    out.measured = measured !== null && !view.isMeasuring;
    view.startMeasure(() => {});
    document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));
    out.escaped = !view.isMeasuring;
    view.destroy();
    out.after = count() - before;
    model.select(null);  // no map to draw on any more: nothing happens
  } catch (e) {
    out.error = String(e && (e as Error).stack || e);
  }
  finish();
})();
