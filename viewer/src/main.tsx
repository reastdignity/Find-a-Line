// The result viewer: one bundle for every run. A run's map.html loads this bundle,
// its data (data.js: window.FINDALINE_RUN) and the list of runs (../runs.js).

import L from "leaflet";
import "leaflet/dist/leaflet.css";
import iconRetinaUrl from "leaflet/dist/images/marker-icon-2x.png";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import shadowUrl from "leaflet/dist/images/marker-shadow.png";
import { render } from "preact";
import "./viewer.css";
import { groupLines, type Group } from "./grouping";
import { baseLayers } from "./layers";
import { control, LinesPanel, RunPanel, type LinesState } from "./panels";
import { draggablePopups } from "./popups";
import { ProfilePanel } from "./Profile";
import { COLORS } from "./style";
import { toLine, type Line, type RunData } from "./types";

// The default marker icon finds its images through the CSS path, which a bundle does not have
L.Icon.Default.mergeOptions({iconRetinaUrl, iconUrl, shadowUrl});
delete (L.Icon.Default.prototype as any)._getIconUrl;

function range(values: number[], digits: number, unit: string): string {
  const lo = Math.min(...values), hi = Math.max(...values);
  return lo.toFixed(digits) === hi.toFixed(digits) ? lo.toFixed(digits) + unit
    : lo.toFixed(digits) + "&ndash;" + hi.toFixed(digits) + unit;
}

function lineInfo(l: Line): string {
  let html = "<b>Length:</b> " + l.len.toFixed(0) + " m<br>" +
    "<b>Anchor 1:</b> " + l.lat1.toFixed(6) + ", " + l.lon1.toFixed(6) + " (" + l.e1.toFixed(1) + " m)<br>" +
    "<b>Anchor 2:</b> " + l.lat2.toFixed(6) + ", " + l.lon2.toFixed(6) + " (" + l.e2.toFixed(1) + " m)<br>" +
    "<b>Min. clearance over terrain:</b> " + l.clr.toFixed(1) + " m";
  if (l.clrN !== null) html += "<br><b>Min. clearance over trees/buildings:</b> " + l.clrN.toFixed(1) + " m";
  if (l.prof && l.prof.water_m > 0)
    html += "<br><b>Over water:</b> " + l.prof.water_m.toFixed(0) + " m of " + l.len.toFixed(0) + " m";
  if (l.prof && l.prof.filled_m > 0)
    html += "<br><b>Over filled gaps in the data:</b> " + l.prof.filled_m.toFixed(0) + " m (less precise)";
  return html;
}

function groupInfo(g: Group<Line>, by: string): string {
  if (g.members.length === 1) return lineInfo(g.rep);
  return "<b>" + g.members.length + " similar lines</b> (shown thin)<br>" +
    "<b>Lengths:</b> " + range(g.members.map((m) => m.len), 0, " m") + "<br>" +
    "<b>Clearances:</b> " + range(g.members.map((m) => m.clr), 1, " m") + "<br>" +
    "<hr style='margin:4px 0'><i>Shown line (" + (by === "longest" ? "longest" : "largest clearance") +
    "):</i><br>" + lineInfo(g.rep);
}

function start(data: RunData) {
  const lines = data.lines.map(toLine);
  const map = L.map("map", {center: data.center, zoom: 15, maxZoom: data.max_zoom});
  (window as any).FINDALINE_MAP = map;  // for tests and the browser console
  L.control.scale().addTo(map);
  const layers = baseLayers(data.ortho, data.max_zoom);
  Object.values(layers)[0].addTo(map);
  draggablePopups(map);
  const area = L.geoJSON(data.area as any, {interactive: false,
                                            style: {color: "black", weight: 1, dashArray: "6", fill: false}});
  area.addTo(map);
  L.marker(data.center).bindTooltip("Search centre").addTo(map);
  L.control.layers(layers, {"Search area": area}, {collapsed: false}).addTo(map);
  map.fitBounds(area.getBounds());

  const renderer = L.canvas({padding: 0.5});
  const linesLayer = L.layerGroup().addTo(map);
  const variantsLayer = L.layerGroup().addTo(map);
  const polyline = (l: Line, options: L.PolylineOptions) => L.polyline(
    [[l.lat1, l.lon1], [l.lat2, l.lon2]],
    {color: l.over ? COLORS.over : COLORS.clean, renderer, bubblingMouseEvents: false, ...options});

  // Profile of the clicked line, along the bottom of the map
  const profileDiv = L.DomUtil.create("div", "", map.getContainer());
  L.DomEvent.disableClickPropagation(profileDiv);
  L.DomEvent.disableScrollPropagation(profileDiv);
  let profileLine: Line | null = null, marker: L.CircleMarker | null = null;
  const onHover = (at: [number, number] | null) => {
    if (!at) {
      if (marker) { map.removeLayer(marker); marker = null; }
    } else if (!marker) {
      marker = L.circleMarker(at, {radius: 6, color: "#000", weight: 2, fillColor: "#fff", fillOpacity: 1,
                                   interactive: false}).addTo(map);
    } else marker.setLatLng(at);
  };
  const drawProfile = () => {
    map.getContainer().classList.toggle("fal-profile-open", profileLine !== null);
    render(profileLine && <ProfilePanel line={profileLine} model={data.model} slope={data.slope}
                                        leashFall={data.leash_fall} rigging={data.rigging} onHover={onHover}
                                        onClose={() => { profileLine = null; onHover(null); drawProfile(); }} />,
           profileDiv);
  };
  const showProfile = (l: Line) => {
    if (!l.prof) return;
    profileLine = l;
    drawProfile();
  };

  // Clicking a line hides all the others (its group's variants stay, thin) until
  // its popup is closed or "Show all lines" is pressed
  let focused: L.Polyline | null = null, hidden: L.Layer[] = [];
  const unfocus = () => {
    hidden.forEach((other) => linesLayer.addLayer(other));
    hidden = [];
    if (focused) { focused = null; drawPanel(); }
  };
  const focusOn = (pl: L.Polyline) => {
    if (focused === pl) return;
    unfocus();
    focused = pl;
    linesLayer.eachLayer((other) => { if (other !== pl) hidden.push(other); });
    hidden.forEach((other) => linesLayer.removeLayer(other));
    drawPanel();
  };
  const zoomTo = (pl: L.Polyline) => {
    const bottom = profileLine ? profileDiv.firstElementChild?.clientHeight || 0 : 0;
    map.fitBounds(pl.getBounds(), {paddingTopLeft: [60, 60], paddingBottomRight: [60, bottom + 60],
                                   maxZoom: data.max_zoom});
  };

  let state: LinesState = {group: true, distance: data.distance, by: data.by, showClean: true, showOver: true};
  let shown = {spots: 0, lines: 0};
  const nClean = lines.filter((l) => !l.over).length, nOver = lines.length - nClean;
  const panel = control(map, "bottomleft", "fal-panel");
  const drawPanel = () => panel(
    <LinesPanel state={state} maxDistance={Math.max(100, Math.round(3 * data.distance))} nClean={nClean}
                nOver={nOver} shown={shown} focused={focused !== null}
                onChange={(next) => { state = next; drawLines(); }}
                onShowAll={() => { map.closePopup(); unfocus(); }} />);

  const drawLines = () => {
    unfocus();
    linesLayer.clearLayers();
    variantsLayer.clearLayers();
    const visible = lines.filter((l) => l.over ? state.showOver : state.showClean);
    const groups = state.group ? groupLines(visible, state.distance, state.by)
      : visible.map((l) => ({rep: l, members: [l]}));
    // Lines over trees/buildings first, so clear lines stay on top
    groups.sort((a, b) => +b.rep.over - +a.rep.over);
    for (const g of groups) {
      const l = g.rep, pl = polyline(l, {weight: 3, opacity: 0.9});
      pl.bindTooltip(l.len.toFixed(0) + " m, clearance " + l.clr.toFixed(1) + " m" +
                     (g.members.length > 1 ? " (" + g.members.length + " similar lines)" : ""));
      pl.bindPopup("<div class='fal-drag'>&#10021; drag to move</div>" + groupInfo(g, state.by) +
                   "<div class='fal-popup-tools'><button class='fal-zoom'>Zoom to line</button></div>");
      pl.on("popupopen", (e) => {
        focusOn(pl);
        showProfile(l);
        const button = e.popup.getElement()?.querySelector<HTMLButtonElement>(".fal-zoom");
        if (button) button.onclick = () => zoomTo(pl);
        for (const m of g.members) {
          if (m !== g.rep) polyline(m, {weight: 1, opacity: 0.8, interactive: false}).addTo(variantsLayer);
        }
      });
      pl.on("popupclose", () => { unfocus(); variantsLayer.clearLayers(); });
      pl.addTo(linesLayer);
    }
    shown = {spots: groups.length, lines: visible.length};
    drawPanel();
  };

  const runs = (window.FINDALINE_RUNS || [data.run]).filter((r) => r.has_map !== false || r.id === data.run.id);
  control(map, "topleft", "fal-panel fal-run")(<RunPanel current={data.run} runs={runs} />);
  drawLines();
}

if (window.FINDALINE_RUN) start(window.FINDALINE_RUN);
else document.body.textContent = "No run data (data.js) next to this map.";
