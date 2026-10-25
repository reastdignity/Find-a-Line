// The result viewer: one bundle for every run. A run's map.html loads this bundle,
// its data (data.js: window.FINDALINE_RUN) and the list of runs (../runs.js); the
// lines of other runs come from their lines.js ("Other runs").

import L from "leaflet";
import "leaflet/dist/leaflet.css";
import iconRetinaUrl from "leaflet/dist/images/marker-icon-2x.png";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import shadowUrl from "leaflet/dist/images/marker-shadow.png";
import { render } from "preact";
import "./viewer.css";
import { dh, lowerAnchor, passes, range as valueRange, saveFilters, savedFilters, type Filters } from "./filters";
import { groupLines, type Group } from "./grouping";
import { baseLayers } from "./layers";
import { inLocalPlane, listOthers, loadLines, colorOf, NEARBY_KM, saveChoice, savedChoice } from "./others";
import { control, LinesPanel, OtherRunsPanel, RunPanel, type LinesState, type OtherItem } from "./panels";
import { draggablePopups } from "./popups";
import { ProfilePanel } from "./Profile";
import { COLORS } from "./style";
import { toLine, type Line, type Profile, type RunData, type RunInfo } from "./types";

// The default marker icon finds its images through the CSS path, which a bundle does not have
L.Icon.Default.mergeOptions({iconRetinaUrl, iconUrl, shadowUrl});
delete (L.Icon.Default.prototype as any)._getIconUrl;

function range(values: number[], digits: number, unit: string): string {
  const lo = Math.min(...values), hi = Math.max(...values);
  return lo.toFixed(digits) === hi.toFixed(digits) ? lo.toFixed(digits) + unit
    : lo.toFixed(digits) + "&ndash;" + hi.toFixed(digits) + unit;
}

function escape(text: string): string {
  return text.replace(/[&<>"']/g, (c) => "&#" + c.charCodeAt(0) + ";");
}

function lineInfo(l: Line): string {
  let html = "";
  if (l.run) {
    html += "<b>Run:</b> <span class='fal-swatch' style='background:" + l.run.color + "'></span> " +
      escape(l.run.info.created.slice(0, 10) + " " + l.run.info.name) + " (<a href='../" +
      encodeURIComponent(l.run.info.id) + "/map.html'>its map, with the profile</a>)<br>";
  }
  html += "<b>Length:</b> " + l.len.toFixed(0) + " m<br>" +
    "<b>Anchor 1:</b> " + l.lat1.toFixed(6) + ", " + l.lon1.toFixed(6) + " (" + l.e1.toFixed(1) + " m)<br>" +
    "<b>Anchor 2:</b> " + l.lat2.toFixed(6) + ", " + l.lon2.toFixed(6) + " (" + l.e2.toFixed(1) + " m)<br>" +
    "<b>Min. clearance over terrain:</b> " + l.clr.toFixed(1) + " m";
  if (l.clrN !== null) html += "<br><b>Min. clearance over trees/buildings:</b> " + l.clrN.toFixed(1) + " m";
  if (l.water > 0) html += "<br><b>Over water:</b> " + l.water.toFixed(0) + " m of " + l.len.toFixed(0) + " m";
  if (l.filled > 0)
    html += "<br><b>Over filled gaps in the data:</b> " + l.filled.toFixed(0) + " m (less precise)";
  if (!l.prof && !l.run) html += "<div class='fal-profile-note'></div>";
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
  const lines = data.lines.map((row) => toLine(row));
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
  // Lines of other runs in the run's colour, dashed over trees/buildings
  const polyline = (l: Line, options: L.PolylineOptions) => L.polyline(
    [[l.lat1, l.lon1], [l.lat2, l.lon2]],
    {color: l.run ? l.run.color : l.over ? COLORS.over : COLORS.clean, renderer, bubblingMouseEvents: false,
     dashArray: l.run && l.over ? "6 4" : undefined, ...options});

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
  let measuredLine: Line | null = null, endMeasure = () => {};
  const drawProfile = () => {
    map.getContainer().classList.toggle("fal-profile-open", profileLine !== null);
    if (profileLine !== measuredLine) endMeasure();
    render(profileLine && <ProfilePanel line={profileLine} model={data.model} slope={data.slope}
                                        leashFall={data.leash_fall} rigging={data.rigging} onHover={onHover}
                                        measured={profileLine === measuredLine}
                                        onClose={() => { profileLine = null; onHover(null); drawProfile(); }} />,
           profileDiv);
  };
  const showProfile = (l: Line) => {
    profileLine = l.prof ? l : null;  // lines of other runs come without profiles
    if (!profileLine) onHover(null);
    drawProfile();
  };
  // Maps of areas keep a profile only for the line saved for each group; the app's server
  // computes the others from the cached data (web.py /api/profile)
  const computed = new Map<string, Profile>();
  const fromServer = location.protocol.startsWith("http");
  const fetchProfile = (l: Line, note: HTMLElement | null | undefined, stillOpen: () => boolean) => {
    const key = [l.lat1, l.lon1, l.lat2, l.lon2].join(",");
    const known = computed.get(key);
    if (known) { l.prof = known; showProfile(l); return; }
    if (!fromServer) {
      if (note) note.innerHTML = "<i>No profile saved for this line (in the map of an area only the line saved " +
        "for each group has one). Open this map from the app (<code>findaline.py web</code>) to compute it.</i>";
      return;
    }
    if (note) note.innerHTML = "<i>Computing the terrain profile…</i>";
    fetch("/api/profile?run=" + encodeURIComponent(data.run.id) + "&line=" + key,
          {headers: {"X-Findaline-Profile": "1"}})
      .then((response) => response.json().then((body) => ({ok: response.ok, body})))
      .then(({ok, body}) => {
        if (!ok) throw new Error(body.detail || "error");
        computed.set(key, body);
        l.prof = body;
        if (note) note.innerHTML = "<i>Profile computed again from the cached data.</i>";
        if (stillOpen()) showProfile(l);
      })
      .catch((e) => { if (note) note.innerHTML = "<i>The profile could not be computed: " + escape(String(e.message)) + "</i>"; });
  };

  // Measuring: the profile between two points clicked on the map, computed by the server
  // like that of a line (anchors on the ground; their height above it is set in the panel)
  if (fromServer) {
    const measureLayer = L.layerGroup().addTo(map);
    let measuring = false, points: L.LatLng[] = [];
    const box = control(map, "topleft", "fal-measure leaflet-bar");
    const drawBox = (note = "") => box(<div>
      <button class={measuring ? "fal-active" : ""} title="Profile between two points"
              onClick={() => measuring ? stop() : begin()}>{measuring ? "Cancel" : "Measure"}</button>
      {note && <span class="fal-measure-note">{note}</span>}
    </div>);
    const setMode = (on: boolean) => {
      measuring = on;
      map.getContainer().classList.toggle("fal-measuring", on);  // clicks go through the lines (CSS)
    };
    const begin = () => {
      measureLayer.clearLayers();
      points = [];
      setMode(true);
      drawBox("Click the first point");
    };
    const stop = (note = "") => { setMode(false); points = []; drawBox(note); };
    endMeasure = () => { if (measuredLine) { measuredLine = null; measureLayer.clearLayers(); drawBox(); } };
    map.on("click", (e: L.LeafletMouseEvent) => {
      if (!measuring) return;
      points.push(e.latlng);
      L.circleMarker(e.latlng, {radius: 5, color: "#000", weight: 2, fillColor: "#fff", fillOpacity: 1,
                                interactive: false}).addTo(measureLayer);
      if (points.length < 2) { drawBox("Click the second point"); return; }
      const [a, b] = points;
      L.polyline([a, b], {color: "#000", weight: 2, dashArray: "6 4", interactive: false}).addTo(measureLayer);
      stop("Computing the profile…");
      const key = [a.lat, a.lng, b.lat, b.lng].map((v) => v.toFixed(7)).join(",");
      fetch("/api/profile?run=" + encodeURIComponent(data.run.id) + "&line=" + key,
            {headers: {"X-Findaline-Profile": "1"}})
        .then((response) => response.json().then((body) => ({ok: response.ok, body})))
        .then(({ok, body}) => {
          if (!ok) throw new Error(body.detail || "error");
          const p = body as Profile, n = p.d.length - 1;
          if (p.z[0] === null || p.z[n] === null) throw new Error("no terrain data at a point");
          measuredLine = {lat1: a.lat, lon1: a.lng, lat2: b.lat, lon2: b.lng, x1: 0, y1: 0, x2: 0, y2: 0,
                          len: p.d[n], clr: p.min[1], clrN: p.min_nmpt ? p.min_nmpt[1] : null,
                          e1: p.z[0]!, e2: p.z[n]!, over: false, water: p.water_m, filled: p.filled_m, prof: p};
          drawBox();
          showProfile(measuredLine);
        })
        .catch((e) => { measureLayer.clearLayers(); drawBox("Not computed: " + e.message); });
    });
    L.DomEvent.on(document as any, "keydown", (e: Event) => {
      if ((e as KeyboardEvent).key === "Escape" && measuring) { measureLayer.clearLayers(); stop(); }
    });
    drawBox();
  }

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
  let filters: Filters = savedFilters(), largestGroup = 1;
  let all = lines;  // with the lines of the other runs shown
  const panel = control(map, "bottomleft", "fal-panel");
  const drawPanel = () => panel(
    <LinesPanel state={state} maxDistance={Math.max(100, Math.round(3 * data.distance))}
                nClean={all.filter((l) => !l.over).length} nOver={all.filter((l) => l.over).length}
                shown={shown} focused={focused !== null} filters={filters}
                ranges={{len: valueRange(all, (l) => l.len), clr: valueRange(all, (l) => l.clr),
                         clrN: valueRange(all, (l) => l.clrN), dh: valueRange(all, dh),
                         elev: valueRange(all, lowerAnchor),
                         group: largestGroup > 1 ? {min: 1, max: largestGroup} : null,
                         water: all.some((l) => l.water > 0), filled: all.some((l) => l.filled > 0)}}
                onChange={(next) => { state = next; drawLines(); }}
                onFilters={(next) => { filters = next; saveFilters(filters); drawLines(); }}
                onShowAll={() => { map.closePopup(); unfocus(); }} />);

  const drawLines = () => {
    unfocus();
    linesLayer.clearLayers();
    variantsLayer.clearLayers();
    // Filters first, then the groups of what is left; the group size last
    const visible = framed(all).filter((l) => (l.over ? state.showOver : state.showClean) && passes(l, filters));
    let groups = state.group ? groupLines(visible, state.distance, state.by)
      : visible.map((l) => ({rep: l, members: [l]}));
    largestGroup = groups.reduce((m, g) => Math.max(m, g.members.length), 1);
    if (state.group && filters.groupMin !== null) groups = groups.filter((g) => g.members.length >= filters.groupMin!);
    // Lines over trees/buildings first, so clear lines stay on top
    groups.sort((a, b) => +b.rep.over - +a.rep.over);
    for (const g of groups) {
      const l = g.rep, pl = polyline(l, {weight: l.run ? 2.5 : 3, opacity: 0.9});
      pl.bindTooltip((l.run ? escape(l.run.info.name) + ": " : "") + l.len.toFixed(0) + " m, clearance " + l.clr.toFixed(1) + " m" +
                     (g.members.length > 1 ? " (" + g.members.length + " similar lines)" : ""));
      pl.bindPopup("<div class='fal-drag'>&#10021; drag to move</div>" + groupInfo(g, state.by) +
                   "<div class='fal-popup-tools'><button class='fal-zoom'>Zoom to line</button></div>");
      pl.on("popupopen", (e) => {
        focusOn(pl);
        showProfile(l);
        if (!l.prof && !l.run) {
          fetchProfile(l, e.popup.getElement()?.querySelector<HTMLElement>(".fal-profile-note"), () => focused === pl);
        }
        const button = e.popup.getElement()?.querySelector<HTMLButtonElement>(".fal-zoom");
        if (button) button.onclick = () => zoomTo(pl);
        for (const m of g.members) {
          if (m !== g.rep) polyline(m, {weight: 1, opacity: 0.8, interactive: false}).addTo(variantsLayer);
        }
      });
      pl.on("popupclose", () => { unfocus(); variantsLayer.clearLayers(); });
      pl.addTo(linesLayer);
    }
    shown = {spots: groups.length, lines: groups.reduce((n, g) => n + g.members.length, 0)};
    drawPanel();
  };

  // Other runs: their lines (from lines.js) and areas, grouped together with this run's
  const runs = (window.FINDALINE_RUNS || [data.run]).filter((r) => r.has_map !== false || r.id === data.run.id);
  const bounds = area.getBounds();
  const items: OtherItem[] = listOthers(runs, data.run.id, [bounds.getWest(), bounds.getSouth(), bounds.getEast(),
                                                            bounds.getNorth()])
    .map(({run, km}) => ({run, km, color: colorOf(run.id), status: "off"}));
  const shownRuns = new Map<string, {lines: Line[]; area: L.Layer; crs: string}>();
  // Runs in another coordinate system are grouped in a local plane around this run
  const framed = (lines: Line[]) => Array.from(shownRuns.values()).some((r) => r.crs !== data.crs)
    ? inLocalPlane(lines, data.center[0], data.center[1]) : lines;
  const runPanel = control(map, "topleft", "fal-panel fal-run");
  const drawRunPanel = () => runPanel(<div>
    <RunPanel current={data.run} runs={runs} />
    <OtherRunsPanel items={items} nearbyKm={NEARBY_KM} onToggle={toggle} />
  </div>);
  const update = () => {
    all = lines.concat(...Array.from(shownRuns.values()).map((r) => r.lines));
    saveChoice(items.filter((i) => i.status === "on" || i.status === "loading").map((i) => i.run.id));
    drawRunPanel();
    drawLines();
  };
  const show = (item: OtherItem) => {
    item.status = "loading";
    drawRunPanel();
    loadLines(item.run.id).then((loaded) => {
      if (item.status !== "loading") return;  // switched off meanwhile
      const run = {info: item.run as RunInfo, color: item.color};
      const runLines = loaded.lines.map((row) => toLine(row, run));
      const outline = L.geoJSON(loaded.area as any, {interactive: false,
                                                     style: {color: item.color, weight: 1, dashArray: "6", fill: false}});
      outline.addTo(map);
      shownRuns.set(item.run.id, {lines: runLines, area: outline, crs: loaded.crs});
      item.status = "on";
      item.lines = runLines.length;
      update();
    }, () => {
      item.status = "error";
      update();
    });
  };
  function toggle(id: string) {
    const item = items.find((i) => i.run.id === id)!;
    if (item.status === "on" || item.status === "loading") {
      const shownRun = shownRuns.get(id);
      if (shownRun) map.removeLayer(shownRun.area);
      shownRuns.delete(id);
      item.status = "off";
      update();
    } else show(item);
  }
  drawRunPanel();
  drawLines();
  const chosen = savedChoice();
  items.filter((i) => chosen.includes(i.run.id)).forEach(show);
}

if (window.FINDALINE_RUN) start(window.FINDALINE_RUN);
else document.body.textContent = "No run data (data.js) next to this map.";
