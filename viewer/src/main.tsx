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
import { dh, drop, lowerAnchor, NO_FILTERS, passes, prominence, range as valueRange, saveFilters, savedFilters,
         wallAngle, type Filters } from "./filters";

const finite = (v: number) => isFinite(v) ? v : null;
import { groupLines, type Group } from "./grouping";
import { baseLayers } from "./layers";
import { lineCoords, lineKey } from "./keys";
import { saveList, savedList, type ListState, type Row } from "./list";
import { LinesList } from "./LinesList";
import { inLocalPlane, listOthers, loadLines, colorOf, NEARBY_KM, saveChoice, savedChoice } from "./others";
import { control, LinesPanel, OtherRunsPanel, RunPanel, type LinesState, type OtherItem } from "./panels";
import { draggablePopups } from "./popups";
import { ProfilePanel } from "./Profile";
import { COLORS } from "./style";
import { toLine, type Credit, type Line, type Profile, type RunData, type RunInfo } from "./types";

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

/** The data credits for Leaflet's attribution control, which renders HTML: escaped text, links to http(s) only. */
function creditsHtml(credits: Credit[]): string {
  const parts = credits.map((c) => escape(c.attribution) + " (" + (/^https?:\/\//.test(c.url)
    ? '<a href="' + escape(c.url) + '" target="_blank" rel="noopener">' + escape(c.licence) + "</a>"
    : escape(c.licence)) + ")");
  return "Data: " + Array.from(new Set(parts)).join(" | ");
}

let highAbove = 20;  // m, the run's high_above (set by start)
const PROFILE_HEIGHT = 270;  // px, the profile panel (.fal-profile in viewer.css)

function lineInfo(l: Line): string {
  let html = "";
  if (l.run) {
    // No style attribute (the CSP allows none): the colour is set on popupopen (showColors)
    html += "<b>Run:</b> <span class='fal-swatch' data-color='" + escape(l.run.color) + "'></span> " +
      escape(l.run.info.created.slice(0, 10) + " " + l.run.info.name) + " (<a href='../" +
      escape(encodeURIComponent(l.run.info.id)) + "/map.html'>its map, with the profile</a>)<br>";
  }
  html += "<b>Length:</b> " + l.len.toFixed(0) + " m<br>" +
    "<b>Anchor 1:</b> " + l.lat1.toFixed(6) + ", " + l.lon1.toFixed(6) + " (" + l.e1.toFixed(1) + " m)<br>" +
    "<b>Anchor 2:</b> " + l.lat2.toFixed(6) + ", " + l.lon2.toFixed(6) + " (" + l.e2.toFixed(1) + " m)<br>" +
    "<b>Min. clearance over terrain:</b> " + l.clr.toFixed(1) + " m";
  if (l.clrN !== null) html += "<br><b>Min. clearance over trees/buildings:</b> " + l.clrN.toFixed(1) + " m";
  if (isFinite(l.hmax)) {
    html += "<br><b>Webbing above the terrain:</b> up to " + l.hmax.toFixed(0) + " m, mean " + l.hmean.toFixed(0) +
      " m, " + l.highPct.toFixed(0) + "% of the line " + highAbove + " m or more";
  }
  if (isFinite(l.angle1)) {
    html += "<br><b>Angle to the wall:</b> A1 " + l.angle1.toFixed(0) + "°, A2 " + l.angle2.toFixed(0) +
      "° (0° straight out over the drop)";
  }
  const m = (v: number | null) => v === null ? "–" : v.toFixed(0) + " m";
  if (l.prom1 !== null || l.drop1 !== null) {
    html += "<br><b>Anchors stand out:</b> prominence A1 " + m(l.prom1) + ", A2 " + m(l.prom2) +
      "; drop A1 " + m(l.drop1) + ", A2 " + m(l.drop2);
  }
  if (l.water > 0) html += "<br><b>Over water:</b> " + l.water.toFixed(0) + " m of " + l.len.toFixed(0) + " m";
  if (l.filled > 0)
    html += "<br><b>Over filled gaps in the data:</b> " + l.filled.toFixed(0) + " m (less precise)";
  if (!l.prof && !l.run) html += "<div class='fal-profile-note'></div>";
  return html;
}

/** Swatches of a popup get their colour (data-color) through the CSSOM, which the CSP allows. */
function showColors(element: HTMLElement | undefined) {
  element?.querySelectorAll<HTMLElement>("[data-color]").forEach((s) => { s.style.background = s.dataset.color!; });
}

const BY_LABEL: Record<string, string> = {longest: "longest", height: "highest above the terrain",
                                          clearance: "largest clearance"};

/** The popup of a group with its line `shown`: its members are in the order of the grouping (`by`), so
 * the first is the representative; ◀ ▶ step through them (wired in popupopen). */
function groupInfo(g: Group<Line>, shown: Line, by: string): string {
  if (g.members.length === 1) return lineInfo(shown);
  const k = g.members.indexOf(shown), n = g.members.length, label = BY_LABEL[by] || BY_LABEL.clearance;
  return "<b>" + n + " similar lines</b> (shown thin: click one to show it)<br>" +
    "<b>Lengths:</b> " + range(g.members.map((m) => m.len), 0, " m") + "<br>" +
    "<b>Clearances:</b> " + range(g.members.map((m) => m.clr), 1, " m") + "<br>" +
    "<hr class='fal-hr'><div class='fal-choice'>" +
    "<button class='fal-prev' title='the previous line of the group'" + (k === 0 ? " disabled" : "") + ">&#9664;</button>" +
    " line <b>" + (k + 1) + "</b> of " + n + " " +
    "<button class='fal-next' title='the next line of the group'" + (k === n - 1 ? " disabled" : "") + ">&#9654;</button>" +
    (k > 0 ? " <button class='fal-default'>back to the " + label + "</button>" : "") + "</div>" +
    "<i>Shown line (" + (k === 0 ? label : "chosen; line 1 is the " + label) + "):</i><br>" + lineInfo(shown);
}

function start(data: RunData) {
  const lines = data.lines.map((row) => toLine(row));
  highAbove = data.high_above;
  const map = L.map("map", {center: data.center, zoom: 15, maxZoom: data.max_zoom});
  (window as any).FINDALINE_MAP = map;  // for tests and the browser console
  L.control.scale().addTo(map);
  const credits = data.attribution || [];
  if (credits.length) map.attributionControl.addAttribution(creditsHtml(credits));
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
  const weightOf = (l: Line) => l.run ? 2.5 : 3;
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
  const canCompute = window.FINDALINE_APP ? window.FINDALINE_APP.profiles : fromServer;
  const fetchProfile = (l: Line, note: HTMLElement | null | undefined, stillOpen: () => boolean) => {
    const key = lineCoords(l);
    const known = computed.get(key);
    if (known) { l.prof = known; showProfile(l); return; }
    if (!canCompute) {
      if (note) note.innerHTML = "<i>No profile saved for this line (in the map of an area only the line saved " +
        "for each group has one). " + (fromServer ? "Its owner can compute it in the app." :
        "Open this map from the app (<code>findaline.py web</code>) to compute it.") + "</i>";
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
  if (canCompute) {
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
                          e1: p.z[0]!, e2: p.z[n]!, over: false, water: p.water_m, filled: p.filled_m,
                          hmax: p.max_height ? p.max_height[1] : NaN, hmean: NaN, highPct: NaN,
                          angle1: NaN, angle2: NaN, prom1: null, prom2: null, drop1: null, drop2: null, prof: p};
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
  // `profile`: the profile panel will be open (a line about to be opened from the list)
  const zoomTo = (pl: L.Polyline, profile = profileLine !== null, animate = true) => {
    const bottom = profile ? profileDiv.firstElementChild?.clientHeight || PROFILE_HEIGHT : 0;
    map.fitBounds(pl.getBounds(), {paddingTopLeft: [60, 60], paddingBottomRight: [60, bottom + 60],
                                   maxZoom: data.max_zoom, animate});
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
                         elev: valueRange(all, lowerAnchor), hmax: valueRange(all, (l) => l.hmax),
                         hmean: valueRange(all, (l) => l.hmean), highPct: valueRange(all, (l) => l.highPct),
                         highAbove: data.high_above, angle: valueRange(all, wallAngle),
                         prom: valueRange(all, (l) => finite(prominence(l, Math.max))),
                         drop: valueRange(all, (l) => finite(drop(l))),
                         group: largestGroup > 1 ? {min: 1, max: largestGroup} : null,
                         water: all.some((l) => l.water > 0), filled: all.some((l) => l.filled > 0)}}
                onChange={(next) => { state = next; drawLines(); }}
                onFilters={(next) => { filters = next; saveFilters(filters); drawLines(); }}
                onShowAll={() => { map.closePopup(); unfocus(); }} />);

  // A group's line on the map: the one chosen in it (a variant clicked, ◀ ▶ in its popup, a row of
  // the list, a link to the line), else its representative. Only the line shown changes: the groups
  // stay those of groupLines (as mapping.group_lines). Kept until the map is closed.
  const keyOf = (l: Line) => lineKey(l, data.run.id);
  const chosen = new Set<string>();
  const shownOf = (g: Group<Line>) => g.members.find((m) => chosen.has(keyOf(m))) || g.rep;
  const variantRenderer = L.canvas({padding: 0.5, tolerance: 5});  // thin lines that can still be clicked

  // The list of the lines shown (top right, under the layers): its rows are the groups drawn last
  type Spot = Row & {group: Group<Line>};
  let listState: ListState = savedList(), rows: Spot[] = [];
  let linkNote: {text: string; action?: string; onAction?(): void} | null = null;  // about a link to a line
  const polylineOf = new Map<string, L.Polyline>();  // by keyOf of the line shown
  let hovered: Line | null = null;  // the line of the row under the mouse, drawn wider
  const list = control(map, "topright", "fal-panel fal-list-panel");
  const inView = (l: Line) => map.getBounds().intersects(L.latLngBounds([l.lat1, l.lon1], [l.lat2, l.lon2]));
  // Fitted at once, then the popup (which pans itself into the view) focuses the line and opens its
  // profile; lines of other runs have none
  const open = (l: Line, pl: L.Polyline) => { zoomTo(pl, !l.run, false); pl.openPopup(); };
  const drawList = () => list(
    <LinesList rows={listState.inView ? rows.filter((r) => inView(r.line)) : rows} total={rows.length}
               grouping={state.group} state={listState} keyOf={keyOf} note={linkNote}
               onState={(next) => { listState = next; saveList(listState); drawList(); }}
               onPick={(l) => { const pl = polylineOf.get(keyOf(l)); if (pl) open(l, pl); }}
               onChoose={(row, l) => choose((row as Spot).group, l, true)}
               onHover={(l) => {
                 const before = hovered && polylineOf.get(keyOf(hovered)), pl = l && polylineOf.get(keyOf(l));
                 if (before) before.setStyle({weight: weightOf(hovered!)});
                 hovered = pl ? l : null;
                 if (pl) pl.setStyle({weight: 7});
                 if (pl && map.hasLayer(pl)) pl.bringToFront();
               }} />);
  map.on("moveend", () => { if (listState.inView && listState.open) drawList(); });

  /** The line of a group on the map, with its tooltip and popup. */
  const drawGroup = (g: Group<Line>): L.Polyline => {
    const l = shownOf(g), pl = polyline(l, {weight: weightOf(l), opacity: 0.9});
    polylineOf.set(keyOf(l), pl);
    pl.bindTooltip((l.run ? escape(l.run.info.name) + ": " : "") + l.len.toFixed(0) + " m, clearance " + l.clr.toFixed(1) + " m" +
                   (g.members.length > 1 ? " (" + g.members.length + " similar lines)" : ""));
    pl.bindPopup("<div class='fal-drag'>&#10021; drag to move</div>" + groupInfo(g, l, state.by) +
                 "<div class='fal-popup-tools'><button class='fal-zoom'>Zoom to line</button></div>");
    pl.on("popupopen", (e) => {
      const element = e.popup.getElement();
      showColors(element);
      focusOn(pl);
      showProfile(l);
      if (!l.prof && !l.run) {
        fetchProfile(l, element?.querySelector<HTMLElement>(".fal-profile-note"), () => focused === pl);
      }
      const button = (name: string, click: () => void) => {
        const b = element?.querySelector<HTMLButtonElement>(name);
        if (b) b.onclick = click;
      };
      const k = g.members.indexOf(l);
      button(".fal-zoom", () => zoomTo(pl));
      button(".fal-prev", () => choose(g, g.members[k - 1]));
      button(".fal-next", () => choose(g, g.members[k + 1]));
      button(".fal-default", () => choose(g, g.rep));
      for (const m of g.members) {
        if (m === l) continue;
        const variant = polyline(m, {weight: 1, opacity: 0.8, renderer: variantRenderer});
        variant.bindTooltip(m.len.toFixed(0) + " m, clearance " + m.clr.toFixed(1) + " m: click to show this line");
        variant.on("click", () => choose(g, m));
        variant.addTo(variantsLayer);
      }
    });
    pl.on("popupclose", () => { unfocus(); variantsLayer.clearLayers(); });
    pl.addTo(linesLayer);
    return pl;
  };

  /** Show another line of a group (its representative: no choice) and open its popup; `zoom`: fit the map to it. */
  function choose(g: Group<Line>, line: Line | undefined, zoom = false) {
    if (!line) return;
    const before = shownOf(g), old = polylineOf.get(keyOf(before));
    g.members.forEach((m) => chosen.delete(keyOf(m)));
    if (line !== g.rep) chosen.add(keyOf(line));
    map.closePopup();  // shows the lines hidden by its focus again
    if (old) {
      polylineOf.delete(keyOf(before));
      linesLayer.removeLayer(old);
    }
    if (hovered === before) hovered = null;
    const pl = drawGroup(g);
    const row = rows.find((r) => r.group === g);
    if (row) row.line = line;
    drawList();
    if (zoom) open(line, pl); else pl.openPopup();
  }

  const drawLines = () => {
    // The line whose popup is open stays open if it is still shown (filters changed, other runs loaded)
    const reopen = Array.from(polylineOf).find(([, pl]) => pl === focused)?.[0];
    unfocus();
    linesLayer.clearLayers();
    variantsLayer.clearLayers();
    polylineOf.clear();
    hovered = null;
    // Filters first, then the groups of what is left; the group size last
    const visible = framed(all).filter((l) => (l.over ? state.showOver : state.showClean) && passes(l, filters));
    let groups = state.group ? groupLines(visible, state.distance, state.by)
      : visible.map((l) => ({rep: l, members: [l]}));
    largestGroup = groups.reduce((m, g) => Math.max(m, g.members.length), 1);
    if (state.group && filters.groupMin !== null) groups = groups.filter((g) => g.members.length >= filters.groupMin!);
    // Lines over trees/buildings first, so clear lines stay on top
    groups.sort((a, b) => +b.rep.over - +a.rep.over);
    groups.forEach(drawGroup);
    shown = {spots: groups.length, lines: groups.reduce((n, g) => n + g.members.length, 0)};
    // In the order of the groups: clear lines first, the best of each group first
    rows = groups.map((g) => ({line: shownOf(g), n: g.members.length, members: g.members, group: g}))
      .sort((a, b) => +a.group.rep.over - +b.group.rep.over);
    drawPanel();
    drawList();
    if (reopen) polylineOf.get(reopen)?.openPopup();
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
    <RunPanel current={data.run} runs={runs} credits={credits} />
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
  // A link to a line of this run (map.html#line=lat1,lon1,lat2,lon2, as lineCoords): it is shown in its
  // group, the map fits it and its popup opens; filters that hide it can be cleared from the list
  const openLinked = () => {
    if (!location.hash.startsWith("#line=")) return;
    const wanted = decodeURIComponent(location.hash.slice("#line=".length));
    const line = lines.find((l) => lineCoords(l) === wanted);
    linkNote = line ? null : {text: "The line of the link is not in this run."};
    if (line) {
      chosen.add(keyOf(line));
      drawLines();
      const pl = polylineOf.get(keyOf(line));
      if (pl) open(line, pl);
      else linkNote = {text: "The line of the link is hidden by the filters.", action: "clear filters", onAction: () => {
        filters = {...NO_FILTERS};
        saveFilters(filters);
        state = {...state, showClean: true, showOver: true};
        openLinked();
      }};
    }
    drawList();
  };
  window.addEventListener("hashchange", openLinked);

  drawRunPanel();
  drawLines();
  openLinked();
  const chosenRuns = savedChoice();
  items.filter((i) => chosenRuns.includes(i.run.id)).forEach(show);
}

if (window.FINDALINE_RUN) start(window.FINDALINE_RUN);
else document.body.textContent = "No run data (data.js) next to this map.";
