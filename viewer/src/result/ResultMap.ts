// Draws a ResultModel on a Leaflet map that the caller (a shell of the app) owns: the run's area,
// its lines (pink clear, orange over trees/buildings), gold halos of favourites, other runs in their
// colours, the selected line emphasised (halo, anchors) with the others dimmed and its group's
// variants thin and clickable, a hover highlight, the profile's hover marker and a measure mode.
// No popups, no controls, no DOM outside the map: the shell's panels show the rest.

import L from "leaflet";
import type { Group } from "../grouping";
import { COLORS } from "../style";
import type { Line } from "../types";
import type { ResultModel } from "./model";

export interface Padding { top: number; right: number; bottom: number; left: number }

export interface ResultMapOptions {
  /** A line (`variant`: a thin line of the selected group) was clicked; default: model.select(line). */
  onLine?(line: Line, group: Group<Line>, variant: boolean): void;
  /** The mouse entered (a line) or left (null) a line. */
  onHover?(line: Line | null): void;
}

const DIM = 0.25;  // opacity of the lines not selected while one is

export class ResultMap {
  private readonly map: L.Map;
  private readonly model: ResultModel;
  private readonly options: ResultMapOptions;
  private readonly renderer = L.canvas({padding: 0.5});
  private readonly variantRenderer = L.canvas({padding: 0.5, tolerance: 5});  // thin lines that can still be clicked
  private readonly area: L.GeoJSON;
  private readonly otherAreas = L.layerGroup();
  private readonly linesLayer = L.layerGroup();
  private readonly topLayer = L.layerGroup();      // the selection and its variants
  private readonly hoverLayer = L.layerGroup();
  private readonly measureLayer = L.layerGroup();
  private readonly layers: L.Layer[];
  private marker: L.CircleMarker | null = null;
  private hovered: Line | null = null;
  private measuring: ((a: L.LatLng, b: L.LatLng) => void) | null = null;
  private points: L.LatLng[] = [];
  private readonly unsubscribe: () => void;

  constructor(map: L.Map, model: ResultModel, options: ResultMapOptions = {}) {
    this.map = map;
    this.model = model;
    this.options = options;
    this.area = L.geoJSON(model.data.area as any, {interactive: false,
                                                   style: {renderer: this.renderer, color: "black", weight: 1,
                                                           dashArray: "6", fill: false}});
    this.layers = [this.area, this.otherAreas, this.linesLayer, this.topLayer, this.hoverLayer, this.measureLayer];
    this.layers.forEach((layer) => layer.addTo(map));
    map.on("click", this.onMapClick);
    document.addEventListener("keydown", this.onKey);
    this.unsubscribe = model.subscribe(() => this.render());
    this.render();
  }

  /** Remove every layer and handler of this map. */
  destroy() {
    this.unsubscribe();
    this.cancelMeasure();
    this.measureLayer.clearLayers();
    this.hoverAt(null);
    this.layers.forEach((layer) => this.map.removeLayer(layer));
    [this.renderer, this.variantRenderer].forEach((r) => { if (this.map.hasLayer(r)) this.map.removeLayer(r); });
    this.map.off("click", this.onMapClick);
    document.removeEventListener("keydown", this.onKey);
  }

  private color(l: Line): string {
    return l.run ? l.run.color : l.over ? COLORS.over : COLORS.clean;
  }

  private weight(l: Line): number {
    return l.run ? 2.5 : 3;
  }

  /** A line of the map; lines of other runs are dashed over trees/buildings (as main.tsx). */
  private polyline(l: Line, options: L.PolylineOptions): L.Polyline {
    return L.polyline([[l.lat1, l.lon1], [l.lat2, l.lon2]],
                      {color: this.color(l), renderer: this.renderer, bubblingMouseEvents: false,
                       dashArray: l.run && l.over ? "6 4" : undefined, ...options});
  }

  /** Clicks and hovers of a line (a click while measuring is a point of the measurement). */
  private wire(pl: L.Polyline, l: Line, g: Group<Line>, variant: boolean) {
    pl.on("click", (e: L.LeafletMouseEvent) => {
      if (this.measuring) { this.addPoint(e.latlng); return; }
      if (this.options.onLine) this.options.onLine(l, g, variant);
      else this.model.select(l);
    });
    pl.on("mouseover", () => this.options.onHover?.(l));
    pl.on("mouseout", () => this.options.onHover?.(null));
  }

  /** Draw the model again (called on every change of it). */
  render() {
    const model = this.model, selected = model.selected;
    this.linesLayer.clearLayers();
    this.topLayer.clearLayers();
    this.otherAreas.clearLayers();
    model.others.forEach((run) => {
      L.geoJSON(run.area as any, {interactive: false,
                                  style: {renderer: this.renderer, color: run.color, weight: 1, dashArray: "6",
                                          fill: false}})
        .addTo(this.otherAreas);
    });
    const opacity = selected ? DIM : 0.9;
    // Lines over trees/buildings first, so clear lines stay on top (as main.tsx)
    const order = model.groups.filter((g) => g.rep.over).concat(model.groups.filter((g) => !g.rep.over));
    for (const g of order) {
      if (selected && g === selected.group) continue;
      const l = model.shownOf(g);
      if (model.favouritesOn && model.isFavourite(l)) {
        this.polyline(l, {color: COLORS.favourite, weight: this.weight(l) + 7, opacity: 0.6 * (selected ? DIM : 1),
                          dashArray: undefined, interactive: false}).addTo(this.linesLayer);
      }
      const pl = this.polyline(l, {weight: this.weight(l), opacity});
      this.wire(pl, l, g, false);
      pl.addTo(this.linesLayer);
    }
    if (selected) {
      const {group, line} = selected, w = this.weight(line);
      // The variants of its group, thin; then the line over a dark halo (gold for a favourite), its anchors marked
      for (const m of group.members) {
        if (m === line) continue;
        const variant = this.polyline(m, {weight: 1, opacity: 0.8, renderer: this.variantRenderer});
        this.wire(variant, m, group, true);
        variant.addTo(this.topLayer);
      }
      const favourite = model.favouritesOn && model.isFavourite(line);
      this.polyline(line, {color: favourite ? COLORS.favourite : "#000", weight: w + 8, opacity: favourite ? 0.8 : 0.45,
                           dashArray: undefined, interactive: false}).addTo(this.topLayer);
      const pl = this.polyline(line, {weight: w + 2, opacity: 1});
      this.wire(pl, line, group, false);
      pl.addTo(this.topLayer);
      for (const at of [[line.lat1, line.lon1], [line.lat2, line.lon2]] as [number, number][]) {
        L.circleMarker(at, {radius: 5, color: "#000", weight: 2, fillColor: "#fff", fillOpacity: 1,
                            interactive: false, renderer: this.renderer}).addTo(this.topLayer);
      }
    }
    this.drawHover();
  }

  /** Draw a line wider (a row of the list under the mouse); null: none. */
  highlight(line: Line | null) {
    this.hovered = line;
    this.drawHover();
  }

  private drawHover() {
    this.hoverLayer.clearLayers();
    const l = this.hovered;
    if (l) this.polyline(l, {weight: 7, opacity: 1, interactive: false}).addTo(this.hoverLayer);
  }

  /** The point of the profile under the mouse; null: none. */
  hoverAt(at: L.LatLngExpression | null) {
    if (!at) {
      if (this.marker) { this.map.removeLayer(this.marker); this.marker = null; }
    } else if (!this.marker) {
      this.marker = L.circleMarker(at, {radius: 6, color: "#000", weight: 2, fillColor: "#fff", fillOpacity: 1,
                                        interactive: false, renderer: this.renderer}).addTo(this.map);
    } else this.marker.setLatLng(at);
  }

  /** The bounds of the run's area. */
  areaBounds(): L.LatLngBounds {
    return this.area.getBounds();
  }

  fitArea(padding: Padding = {top: 0, right: 0, bottom: 0, left: 0}) {
    this.map.fitBounds(this.area.getBounds(), {paddingTopLeft: [padding.left, padding.top],
                                               paddingBottomRight: [padding.right, padding.bottom]});
  }

  /** Fit the map to a line, `padding` px kept free on each side (panels over the map) plus a margin. */
  zoomTo(line: Line, padding: Padding = {top: 0, right: 0, bottom: 0, left: 0}, animate = true) {
    const m = 60;
    this.map.fitBounds(L.latLngBounds([line.lat1, line.lon1], [line.lat2, line.lon2]),
                       {paddingTopLeft: [padding.left + m, padding.top + m],
                        paddingBottomRight: [padding.right + m, padding.bottom + m],
                        maxZoom: this.model.data.max_zoom, animate});
  }

  /** Is a line within the map's view (the list's "only in the map's view"). */
  inView(line: Line): boolean {
    return this.map.getBounds().intersects(L.latLngBounds([line.lat1, line.lon1], [line.lat2, line.lon2]));
  }

  // --- measuring ---

  get isMeasuring(): boolean {
    return this.measuring !== null;
  }

  /** Measure mode: the next two clicks on the map are the points, given to `done`; Esc cancels. */
  startMeasure(done: (a: L.LatLng, b: L.LatLng) => void) {
    this.measureLayer.clearLayers();
    this.points = [];
    this.measuring = done;
    this.map.getContainer().style.cursor = "crosshair";
  }

  /** Leave measure mode (the points drawn stay until clearMeasure). */
  cancelMeasure() {
    if (!this.measuring) return;
    this.measuring = null;
    this.points = [];
    this.map.getContainer().style.cursor = "";
  }

  /** Remove the points and line of a measurement. */
  clearMeasure() {
    this.measureLayer.clearLayers();
  }

  private addPoint(at: L.LatLng) {
    const done = this.measuring;
    if (!done) return;
    this.points.push(at);
    L.circleMarker(at, {radius: 5, color: "#000", weight: 2, fillColor: "#fff", fillOpacity: 1,
                        interactive: false, renderer: this.renderer}).addTo(this.measureLayer);
    if (this.points.length < 2) return;
    const [a, b] = this.points;
    L.polyline([a, b], {color: "#000", weight: 2, dashArray: "6 4", interactive: false, renderer: this.renderer})
      .addTo(this.measureLayer);
    this.cancelMeasure();
    done(a, b);
  }

  private onMapClick = (e: L.LeafletMouseEvent) => {
    if (this.measuring) this.addPoint(e.latlng);
  };

  private onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape" && this.measuring) { this.cancelMeasure(); this.clearMeasure(); }
  };
}
