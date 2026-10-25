// Background layers: OpenTopoMap, OpenStreetMap, the orthophoto of the data source
// (from the local tile cache when there, else its WMS over its base tiles, if any) and
// Esri World Imagery.

import L from "leaflet";
import type { Ortho } from "./types";

const OSM = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

/** A WMS layer that tries the cached tile ({cache}/{z}/{x}/{y}.jpg) first. */
const CachedWMS = L.TileLayer.WMS.extend({
  createTile(this: L.TileLayer.WMS & {_cache: string | null}, coords: L.Coords, done: L.DoneCallback) {
    const layer = this as any, cache = this._cache;
    let online = !cache;
    const tile = document.createElement("img");
    tile.alt = "";
    tile.setAttribute("role", "presentation");
    L.DomEvent.on(tile, "load", L.Util.bind(layer._tileOnLoad, layer, done, tile));
    L.DomEvent.on(tile, "error", (e) => {
      if (!online) { online = true; tile.src = this.getTileUrl(coords); }
      else layer._tileOnError(done, tile, e);
    });
    tile.src = online ? this.getTileUrl(coords) : cache + "/" + coords.z + "/" + coords.x + "/" + coords.y + ".jpg";
    return tile;
  },
});

export function baseLayers(ortho: Ortho | null, maxZoom: number): Record<string, L.Layer> {
  const layers: Record<string, L.Layer> = {
    "OpenTopoMap": L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
      maxZoom, maxNativeZoom: 17,
      attribution: "Map data: " + OSM + ', <a href="http://viewfinderpanoramas.org">SRTM</a> | Map style: ' +
        '&copy; <a href="https://opentopomap.org">OpenTopoMap</a> ' +
        '(<a href="https://creativecommons.org/licenses/by-sa/3.0/">CC-BY-SA</a>)'}),
    "OpenStreetMap": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom, maxNativeZoom: 19, attribution: OSM}),
  };
  if (ortho) {
    // National WMS (e.g. geoportal) may not support Web Mercator, so tiles are requested
    // in WGS84. WMS renders at any scale, so the orthophoto stays sharp up to maxZoom.
    const base = ortho.base;
    const layer = new (CachedWMS as any)(ortho.url, {
      layers: ortho.layer, format: base ? "image/png" : "image/jpeg", version: "1.3.0", transparent: !!base,
      attribution: ortho.attribution, maxZoom, crs: L.CRS.EPSG4326}) as L.TileLayer.WMS;
    (layer as any)._cache = ortho.cache;
    // Where the WMS has no imagery its PNG is transparent and shows the base under it
    // (cached tiles have both already and cover the base)
    layers[ortho.name] = base ? L.layerGroup([L.tileLayer(base.url, {maxZoom, maxNativeZoom: base.max_zoom}), layer])
      : layer;
  }
  // Worldwide imagery, online only (Esri's terms do not allow caching tiles for offline use)
  layers["Esri World Imagery (online)"] = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
      maxZoom, maxNativeZoom: 18,
      attribution: "Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, " +
        "Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community"});
  return layers;
}
