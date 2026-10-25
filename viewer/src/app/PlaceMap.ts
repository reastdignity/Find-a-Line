// The map of the app: a click sets the centre of a circle, or areas are drawn
// (Leaflet-Geoman: polygons and rectangles), or an uploaded area is shown.

import "@geoman-io/leaflet-geoman-free";
import "@geoman-io/leaflet-geoman-free/dist/leaflet-geoman.css";
import type { FeatureCollection, GeoJSON } from "geojson";
import L from "leaflet";
import { baseLayers } from "../layers";

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
  private mode: Mode = "point";
  onPoint: (lat: number, lon: number) => void = () => {};
  onDrawn: () => void = () => {};

  constructor(element: HTMLElement) {
    this.map = L.map(element, {center: START, zoom: 13, maxZoom: MAX_ZOOM});
    L.control.scale().addTo(this.map);
    const layers = baseLayers(null, MAX_ZOOM);
    Object.values(layers)[0].addTo(this.map);
    L.control.layers(layers, {}, {position: "topright"}).addTo(this.map);
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
      if (this.mode === "point") this.onPoint(e.latlng.lat, e.latlng.lng);
    });
  }

  setMode(mode: Mode) {
    this.mode = mode;
    if (mode === "draw") {
      this.map.pm.addControls({position: "topleft", drawMarker: false, drawCircleMarker: false,
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
      this.marker = L.marker([lat, lon]).addTo(this.map);
      this.circle = L.circle([lat, lon], {radius, color: COLOR, weight: 2, fill: false, interactive: false})
        .addTo(this.map);
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

  /** The visible part of the map as S,W,N,E (for --within). */
  viewBox(): string {
    const b = this.map.getBounds();
    return [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map((v) => v.toFixed(5)).join(",");
  }
}
