// The map of the app: a click sets the centre of a circle, or areas are drawn
// (Leaflet-Geoman: polygons and rectangles), or an uploaded area is shown.
// The new interfaces reuse its Leaflet map (`map`) for the results too: setSearchVisible(false)
// takes the search's layers, tools and clicks off it, and `controls` moves its controls (the old
// app keeps the defaults).

import "@geoman-io/leaflet-geoman-free";
import "@geoman-io/leaflet-geoman-free/dist/leaflet-geoman.css";
import type { FeatureCollection, GeoJSON } from "geojson";
import L from "leaflet";
import { baseLayers } from "../layers";
import { COLORS } from "../style";
import type { Line } from "../types";

export type Mode = "point" | "draw" | "file" | "preset";
const COLOR = "#1f77b4";
const START: [number, number] = [50.1559508, 19.7562722];  // Żabi Koń
const MAX_ZOOM = 21;

export class PlaceMap {
  map: L.Map;
  private marker: L.Marker | null = null;
  private circle: L.Circle | null = null;
  private drawn = L.featureGroup();
  private shown = L.geoJSON(undefined, {style: {color: COLOR, weight: 2}});
  private preview = L.geoJSON(undefined, {interactive: false,
                                          style: {color: "#d95f02", weight: 2, dashArray: "6 4", fillOpacity: 0.08}});
  private favourites = L.featureGroup();  // the tab "Favourites"
  private mode: Mode = "point";
  private hidden = false;    // setSearchVisible(false)
  private controls: L.ControlPosition;  // of the zoom buttons, the layers and the drawing tools
  onPoint: (lat: number, lon: number) => void = () => {};
  onDrawn: () => void = () => {};

  /** `options.controls`: where the zoom buttons, the layer switch and the drawing tools go (the old
   *  app: zoom and tools top left, layers top right). */
  constructor(element: HTMLElement, options: {controls?: L.ControlPosition} = {}) {
    this.controls = options.controls ?? "topleft";
    this.map = L.map(element, {center: START, zoom: 13, maxZoom: MAX_ZOOM, zoomControl: !options.controls});
    if (options.controls) L.control.zoom({position: options.controls}).addTo(this.map);
    L.control.scale().addTo(this.map);
    const layers = baseLayers(null, MAX_ZOOM);
    Object.values(layers)[0].addTo(this.map);
    L.control.layers(layers, {}, {position: options.controls ?? "topright"}).addTo(this.map);
    this.drawn.addTo(this.map);
    this.shown.addTo(this.map);
    this.preview.addTo(this.map);
    this.map.pm.setGlobalOptions({layerGroup: this.drawn, pathOptions: {color: COLOR}} as any);
    this.map.on("pm:create", (e: any) => {
      e.layer.on("pm:edit", () => this.onDrawn());
      this.onDrawn();
    });
    this.map.on("pm:remove", () => this.onDrawn());
    this.map.on("click", (e: L.LeafletMouseEvent) => {
      if (this.mode === "point" && !this.hidden) this.onPoint(e.latlng.lat, e.latlng.lng);
    });
  }

  /** Shows or hides the search on the map: the point and circle, the drawn and uploaded areas, the
   *  preview, the drawing tools and the clicks that set the centre (the results use the map then). */
  setSearchVisible(visible: boolean) {
    if (visible === !this.hidden) return;
    this.hidden = !visible;
    if (visible) {
      this.preview.addTo(this.map);
      this.setMode(this.mode);
      return;
    }
    this.map.pm.removeControls();
    for (const layer of [this.marker, this.circle, this.drawn, this.shown, this.preview]) layer?.remove();
    this.map.getContainer().style.cursor = "";
  }

  get searchVisible(): boolean {
    return !this.hidden;
  }

  setMode(mode: Mode) {
    this.mode = mode;
    if (this.hidden) return;  // setSearchVisible(true) applies it
    if (mode === "draw") {
      this.map.pm.addControls({position: this.controls, drawMarker: false, drawCircleMarker: false,
                               drawPolyline: false, drawText: false, drawCircle: false, cutPolygon: false,
                               rotateMode: false});
    } else this.map.pm.removeControls();
    const point = mode === "point";
    for (const layer of [this.marker, this.circle]) {
      if (layer && point) layer.addTo(this.map); else if (layer) layer.remove();
    }
    if (mode === "draw") this.drawn.addTo(this.map); else this.drawn.remove();
    if (mode === "file") this.shown.addTo(this.map); else this.shown.remove();
    this.map.getContainer().style.cursor = point ? "crosshair" : "";
  }

  /** The centre of the circle and its radius in metres. */
  setPoint(lat: number, lon: number, radius: number) {
    if (!this.marker) {
      this.marker = L.marker([lat, lon]);
      this.circle = L.circle([lat, lon], {radius, color: COLOR, weight: 2, fill: false, interactive: false});
      if (!this.hidden) {
        this.marker.addTo(this.map);
        this.circle.addTo(this.map);
      }
    }
    this.marker.setLatLng([lat, lon]);
    this.circle!.setLatLng([lat, lon]).setRadius(radius);
  }

  /** Removes the point, the drawn areas, the uploaded file and the preview. */
  clear() {
    this.marker?.remove();
    this.circle?.remove();
    this.marker = this.circle = null;
    this.drawn.clearLayers();
    this.shown.clearLayers();
    this.preview.clearLayers();
    this.onDrawn();
  }

  /** Areas to edit (a job's drawn or uploaded area): they replace the drawn ones. */
  loadDrawn(areas: GeoJSON) {
    this.drawn.clearLayers();
    L.geoJSON(areas as any, {style: {color: COLOR}}).eachLayer((layer) => {
      layer.on("pm:edit", () => this.onDrawn());
      this.drawn.addLayer(layer);
    });
    if (this.drawn.getLayers().length) this.map.fitBounds(this.drawn.getBounds(), {maxZoom: 17});
    this.onDrawn();
  }

  /** The drawn polygons and rectangles, or null. */
  drawnAreas(): FeatureCollection | null {
    const collection = this.drawn.toGeoJSON() as FeatureCollection;
    return collection.features.length ? collection : null;
  }

  showFile(collection: FeatureCollection | null) {
    this.shown.clearLayers();
    if (collection) {
      this.shown.addData(collection);
      this.map.fitBounds(this.shown.getBounds(), {maxZoom: 16});
    }
  }

  /** The area the estimate says will be searched (null: none). */
  showPreview(area: GeoJSON | null) {
    this.preview.clearLayers();
    if (area) this.preview.addData(area as any);
  }

  /** The user's favourite lines (the tab "Favourites"; null: none), the map fitted to them. */
  showFavourites(lines: {line: Line; label: string}[] | null) {
    this.favourites.clearLayers();
    if (!lines) {
      this.favourites.remove();
      return;
    }
    for (const {line, label} of lines) {
      L.polyline([[line.lat1, line.lon1], [line.lat2, line.lon2]], {color: COLORS.favourite, weight: 5, opacity: 0.9})
        .bindTooltip(label + ": " + line.len.toFixed(0) + " m").addTo(this.favourites);
    }
    this.favourites.addTo(this.map);
    if (lines.length) this.map.fitBounds(this.favourites.getBounds(), {maxZoom: 17, padding: [30, 30]});
  }

  zoomToLine(line: Line) {
    this.map.fitBounds([[line.lat1, line.lon1], [line.lat2, line.lon2]], {maxZoom: 19, padding: [60, 60]});
  }

  /** The visible part of the map as S,W,N,E (for --within). */
  viewBox(): string {
    const b = this.map.getBounds();
    return [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map((v) => v.toFixed(5)).join(",");
  }
}
