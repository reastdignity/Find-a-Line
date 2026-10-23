"""
Converting results to GPS coordinates and displaying them on maps.
"""

import os
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from pyproj import Transformer

from geoportal import CRS, Grid
from helpers import Line
from orthophoto import WMS_URL as ORTHOPHOTO_WMS

MAX_ZOOM = 21  # about 5 cm per pixel, the detail of the best geoportal orthophotos

COLOR_VALID = "#FF10F0"     # neon pink
COLOR_OBSTACLE = "#FF8C00"  # orange: line passes too close to trees/buildings (NMPT)

_to_wgs84 = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
_to_pl1992 = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)


def wgs84_to_pl1992(lat: float, lon: float):
    """Return (easting, northing) in EPSG:2180."""
    return _to_pl1992.transform(lon, lat)


def pl1992_to_wgs84(easting: float, northing: float):
    """Return (lat, lon)."""
    lon, lat = _to_wgs84.transform(easting, northing)
    return lat, lon


def coordify(lines: List[Line], grid: Grid, nmpt_mode: Optional[str]) -> List[Dict]:
    """Convert pixel coordinates of each line to GPS coordinates (lat, lon).

    Returns:
        One dict per line with anchor coordinates and line statistics
    """
    if not lines:
        return []
    rows = np.array([[line.a1[0], line.a2[0]] for line in lines]).ravel()
    cols = np.array([[line.a1[1], line.a2[1]] for line in lines]).ravel()
    easting, northing = grid.pixel_to_pl1992(rows, cols)
    lon, lat = _to_wgs84.transform(easting, northing)

    results = []
    for k, line in enumerate(lines):
        over_obstacles = (nmpt_mode == "mark" and line.clearance_nmpt is not None
                          and line.clearance_nmpt <= 0)
        results.append({
            "anchor1": (float(lat[2 * k]), float(lon[2 * k])),
            "anchor2": (float(lat[2 * k + 1]), float(lon[2 * k + 1])),
            # PL-1992 (easting, northing) in metres, used for grouping
            "a1_xy": (float(easting[2 * k]), float(northing[2 * k])),
            "a2_xy": (float(easting[2 * k + 1]), float(northing[2 * k + 1])),
            "length": line.length,
            "elev_a1": line.elev_a1,
            "elev_a2": line.elev_a2,
            "clearance": line.clearance,
            "clearance_nmpt": line.clearance_nmpt,
            "over_obstacles": over_obstacles,
        })
    return results


def _rank(by: str):
    """Sort key: best representative first (with reverse=True)."""
    if by == "longest":
        return lambda r: (r["length"], r["clearance"])
    return lambda r: (r["clearance"], r["length"])


def group_lines(results: List[Dict], distance: float, by: str = "clearance") -> List[Dict]:
    """Group near-identical lines (both anchors within `distance` metres).

    Greedy grouping around a representative: the best remaining line (by length
    or clearance) takes every remaining line whose anchors are close to its own,
    so no member is further than `distance` from the representative. Lines over
    trees/buildings are grouped separately from clear lines.
    The same algorithm runs in the interactive map (see GROUPING_SCRIPT).

    Returns:
        Groups, clear lines first and best first: {"rep": result, "members": [results]}
    """
    rank = _rank(by)
    d2 = distance ** 2
    groups = []
    for over in (False, True):
        lines = sorted((r for r in results if r["over_obstacles"] == over), key=rank, reverse=True)
        if not lines:
            continue
        a1 = np.array([r["a1_xy"] for r in lines])
        a2 = np.array([r["a2_xy"] for r in lines])
        free = np.ones(len(lines), dtype=bool)
        for i in range(len(lines)):
            if not free[i]:
                continue
            same = (((a1 - a1[i]) ** 2).sum(1) <= d2) & (((a2 - a2[i]) ** 2).sum(1) <= d2)
            flipped = (((a1 - a2[i]) ** 2).sum(1) <= d2) & (((a2 - a1[i]) ** 2).sum(1) <= d2)
            members = np.flatnonzero(free & (same | flipped))
            free[members] = False
            groups.append({"rep": lines[i], "members": [lines[k] for k in members]})
    # Clear lines first, then lines over trees/buildings; best first within each
    groups.sort(key=lambda g: (not g["rep"]["over_obstacles"], rank(g["rep"])), reverse=True)
    return groups


def _search_area(center_lat: float, center_lon: float, radius: float):
    """Search circle as a polygon in WGS84 (geopandas)."""
    import geopandas as gpd
    from shapely.geometry import Point

    easting, northing = wgs84_to_pl1992(center_lat, center_lon)
    circle = gpd.GeoSeries([Point(easting, northing).buffer(radius)], crs=CRS)
    return circle.to_crs("EPSG:4326")


def mapify(results: List[Dict], center_lat: float, center_lon: float, radius: float):
    """Visualize lines on a static map (matplotlib + OpenTopoMap)."""
    import contextily as ctx
    import geopandas as gpd
    import matplotlib.pyplot as plt
    from shapely.geometry import LineString

    if not results:
        print("No lines to display.")
        return

    lines = [LineString([(r["anchor1"][1], r["anchor1"][0]), (r["anchor2"][1], r["anchor2"][0])])
             for r in results]
    colors = [COLOR_OBSTACLE if r["over_obstacles"] else COLOR_VALID for r in results]
    gdf = gpd.GeoDataFrame({"color": colors}, geometry=lines, crs="EPSG:4326")

    fig, ax = plt.subplots(figsize=(10, 10))
    _search_area(center_lat, center_lon, radius).boundary.plot(
        ax=ax, color="black", linestyle="--", linewidth=0.8)
    gdf.plot(ax=ax, color=gdf["color"], linewidth=0.8, alpha=0.9)

    # Show circles at line midpoints
    midpoints = gpd.GeoDataFrame({"color": colors}, crs="EPSG:4326",
                                 geometry=[line.interpolate(0.5, normalized=True) for line in lines])
    midpoints.plot(ax=ax, color=midpoints["color"], markersize=100, marker="o", alpha=0.6, zorder=3)

    ctx.add_basemap(ax, crs=gdf.crs, source=ctx.providers.OpenTopoMap)
    plt.show()


# Draws the lines on the interactive map and adds a panel to group them.
# groupLines() mirrors group_lines() above.
GROUPING_SCRIPT = """
{% macro header(this, kwargs) %}
<style>
  .fal-panel { background: white; padding: 8px 10px; border-radius: 5px; line-height: 1.6;
               box-shadow: 0 1px 5px rgba(0,0,0,0.4); font: 12px/1.6 sans-serif; }
  .fal-panel input[type=range] { width: 140px; vertical-align: middle; }
  .fal-panel .fal-dim { color: #999; }
  .fal-swatch { display: inline-block; width: 14px; height: 3px; vertical-align: middle; }
  .fal-drag { cursor: move; margin: -6px -6px 6px; padding: 2px 6px; border-radius: 4px;
              background: #eee; color: #888; font-size: 11px; text-align: center; user-select: none; }
  .fal-dragged .leaflet-popup-tip-container { visibility: hidden; }
</style>
{% endmacro %}

{% macro script(this, kwargs) %}
(function(map) {
  var COLORS = {clean: {{ this.color_valid|tojson }}, over: {{ this.color_obstacle|tojson }}};
  var lines = {{ this.lines|tojson }}.map(function(d) {
    return {lat1: d[0], lon1: d[1], lat2: d[2], lon2: d[3], x1: d[4], y1: d[5], x2: d[6], y2: d[7],
            len: d[8], clr: d[9], clrN: d[10], e1: d[11], e2: d[12], over: d[13]};
  });
  var state = {group: true, distance: {{ this.distance|tojson }}, by: {{ this.by|tojson }},
               showClean: true, showOver: true};
  var renderer = L.canvas({padding: 0.5});
  var linesLayer = L.layerGroup().addTo(map);
  var variantsLayer = L.layerGroup().addTo(map);

  function sq(ax, ay, bx, by) { var dx = ax - bx, dy = ay - by; return dx * dx + dy * dy; }
  function near(a, b, d2) {
    return (sq(a.x1, a.y1, b.x1, b.y1) <= d2 && sq(a.x2, a.y2, b.x2, b.y2) <= d2) ||
           (sq(a.x1, a.y1, b.x2, b.y2) <= d2 && sq(a.x2, a.y2, b.x1, b.y1) <= d2);
  }
  function compare(by) {
    return by === "longest" ? function(a, b) { return (b.len - a.len) || (b.clr - a.clr); }
                            : function(a, b) { return (b.clr - a.clr) || (b.len - a.len); };
  }
  function groupLines(visible, distance, by) {
    var d2 = distance * distance, groups = [];
    [false, true].forEach(function(over) {
      var sorted = visible.filter(function(l) { return l.over === over; }).sort(compare(by));
      var used = new Uint8Array(sorted.length);
      for (var i = 0; i < sorted.length; i++) {
        if (used[i]) continue;
        var rep = sorted[i], members = [rep];
        used[i] = 1;
        for (var j = i + 1; j < sorted.length; j++) {
          if (!used[j] && near(rep, sorted[j], d2)) { used[j] = 1; members.push(sorted[j]); }
        }
        groups.push({rep: rep, members: members});
      }
    });
    return groups;
  }

  function range(values, digits, unit) {
    var lo = Math.min.apply(null, values), hi = Math.max.apply(null, values);
    return lo.toFixed(digits) === hi.toFixed(digits) ? lo.toFixed(digits) + unit
                                                     : lo.toFixed(digits) + "&ndash;" + hi.toFixed(digits) + unit;
  }
  function lineInfo(l) {
    var html = "<b>Length:</b> " + l.len.toFixed(0) + " m<br>" +
      "<b>Anchor 1:</b> " + l.lat1.toFixed(6) + ", " + l.lon1.toFixed(6) + " (" + l.e1.toFixed(1) + " m)<br>" +
      "<b>Anchor 2:</b> " + l.lat2.toFixed(6) + ", " + l.lon2.toFixed(6) + " (" + l.e2.toFixed(1) + " m)<br>" +
      "<b>Min. clearance over terrain:</b> " + l.clr.toFixed(1) + " m";
    if (l.clrN !== null) html += "<br><b>Min. clearance over trees/buildings:</b> " + l.clrN.toFixed(1) + " m";
    return html;
  }
  function groupInfo(g) {
    if (g.members.length === 1) return lineInfo(g.rep);
    return "<b>" + g.members.length + " similar lines</b> (shown thin)<br>" +
      "<b>Lengths:</b> " + range(g.members.map(function(m) { return m.len; }), 0, " m") + "<br>" +
      "<b>Clearances:</b> " + range(g.members.map(function(m) { return m.clr; }), 1, " m") + "<br>" +
      "<hr style='margin:4px 0'><i>Shown line (" +
      (state.by === "longest" ? "longest" : "largest clearance") + "):</i><br>" + lineInfo(g.rep);
  }
  function polyline(l, options) {
    return L.polyline([[l.lat1, l.lon1], [l.lat2, l.lon2]], Object.assign({
      color: l.over ? COLORS.over : COLORS.clean, renderer: renderer, bubblingMouseEvents: false
    }, options));
  }
  function showVariants(g) {
    variantsLayer.clearLayers();
    g.members.forEach(function(m) {
      if (m !== g.rep) polyline(m, {weight: 1, opacity: 0.8, interactive: false}).addTo(variantsLayer);
    });
  }

  // Popups can be dragged by their top bar, so they don't cover the line.
  // The offset is kept when the map is zoomed or panned.
  function makeDraggable(popup) {
    var container = popup.getElement(), handle = container.querySelector(".fal-drag");
    if (!handle || popup._falDraggable) return;
    popup._falOffset = L.point(0, 0);

    var draggable = new L.Draggable(container, handle);
    var start;
    draggable.on("dragstart", function() { start = L.DomUtil.getPosition(container); });
    draggable.on("drag", function() {
      popup._falOffset = popup._falOffset.add(L.DomUtil.getPosition(container).subtract(start));
      start = L.DomUtil.getPosition(container);
      L.DomUtil.addClass(container, "fal-dragged");
    });
    draggable.enable();
    popup._falDraggable = draggable;
  }
  // Patched on the prototype: popups register these as event handlers when they open
  ["_updatePosition", "_animateZoom"].forEach(function(name) {
    var original = L.Popup.prototype[name];
    L.Popup.prototype[name] = function() {
      original.apply(this, arguments);
      if (this._falOffset && this._container) {
        L.DomUtil.setPosition(this._container,
                              L.DomUtil.getPosition(this._container).add(this._falOffset));
      }
    };
  });
  map.on("popupopen", function(e) { makeDraggable(e.popup); });
  map.on("popupclose", function(e) {
    // Reopened popups start next to the line again
    e.popup._falOffset = L.point(0, 0);
    if (e.popup.getElement()) L.DomUtil.removeClass(e.popup.getElement(), "fal-dragged");
  });

  function render() {
    linesLayer.clearLayers();
    variantsLayer.clearLayers();
    var visible = lines.filter(function(l) { return l.over ? state.showOver : state.showClean; });
    var groups = state.group ? groupLines(visible, state.distance, state.by)
                             : visible.map(function(l) { return {rep: l, members: [l]}; });
    // Draw lines over trees/buildings first, so clear lines stay on top
    groups.sort(function(a, b) { return b.rep.over - a.rep.over; });
    groups.forEach(function(g) {
      var l = g.rep;
      var pl = polyline(l, {weight: 3, opacity: 0.9});
      pl.bindTooltip(l.len.toFixed(0) + " m, clearance " + l.clr.toFixed(1) + " m" +
                     (g.members.length > 1 ? " (" + g.members.length + " similar lines)" : ""));
      pl.bindPopup("<div class='fal-drag'>&#10021; drag to move</div>" + groupInfo(g));
      if (g.members.length > 1) {
        pl.on("popupopen", function() { showVariants(g); });
        pl.on("popupclose", function() { variantsLayer.clearLayers(); });
      }
      pl.addTo(linesLayer);
    });
    summary.innerHTML = state.group
      ? "<b>" + groups.length + "</b> spots (" + visible.length + " lines)"
      : "<b>" + visible.length + "</b> lines";
  }

  var nClean = lines.filter(function(l) { return !l.over; }).length;
  var nOver = lines.length - nClean;
  var summary;
  var Panel = L.Control.extend({
    options: {position: "bottomleft"},
    onAdd: function() {
      var div = L.DomUtil.create("div", "fal-panel");
      div.innerHTML =
        "<div class='fal-summary'></div>" +
        "<label><input type='checkbox' class='fal-group'> Group similar lines</label>" +
        "<div class='fal-opts'>" +
        "Anchors within <input type='range' class='fal-distance' min='5' max='100' step='1'> " +
        "<span class='fal-distance-label'></span><br>" +
        "Show per group: <label><input type='radio' name='fal-by' value='clearance'> largest clearance</label> " +
        "<label><input type='radio' name='fal-by' value='longest'> longest</label></div>" +
        (nOver ? "<label><input type='checkbox' class='fal-clean' checked> " +
                 "<span class='fal-swatch' style='background:" + COLORS.clean + "'></span> Clear lines (" + nClean + ")</label><br>" +
                 "<label><input type='checkbox' class='fal-over' checked> " +
                 "<span class='fal-swatch' style='background:" + COLORS.over + "'></span> Over trees/buildings (" + nOver + ")</label>"
               : "");
      L.DomEvent.disableClickPropagation(div);
      L.DomEvent.disableScrollPropagation(div);

      summary = div.querySelector(".fal-summary");
      var group = div.querySelector(".fal-group"), opts = div.querySelector(".fal-opts");
      var distance = div.querySelector(".fal-distance"), label = div.querySelector(".fal-distance-label");
      group.checked = state.group;
      distance.value = state.distance;
      label.textContent = state.distance + " m";
      div.querySelector("input[value='" + state.by + "']").checked = true;

      group.addEventListener("change", function() {
        state.group = group.checked;
        opts.classList.toggle("fal-dim", !state.group);
        render();
      });
      distance.addEventListener("input", function() { label.textContent = distance.value + " m"; });
      distance.addEventListener("change", function() { state.distance = +distance.value; render(); });
      div.querySelectorAll("input[name='fal-by']").forEach(function(radio) {
        radio.addEventListener("change", function() { state.by = radio.value; render(); });
      });
      if (nOver) {
        div.querySelector(".fal-clean").addEventListener("change", function(e) { state.showClean = e.target.checked; render(); });
        div.querySelector(".fal-over").addEventListener("change", function(e) { state.showOver = e.target.checked; render(); });
      }
      return div;
    }
  });
  new Panel().addTo(map);
  render();
})({{ this._parent.get_name() }});
{% endmacro %}
"""


def _cache_url(cache_path: str, html_path: str) -> str:
    """URL of a cache folder as seen from the HTML file (relative when possible)."""
    try:
        return os.path.relpath(cache_path, os.path.dirname(os.path.abspath(html_path))).replace(os.sep, "/")
    except ValueError:  # different drive on Windows
        return Path(os.path.abspath(cache_path)).as_uri()


def save_html_map(results: List[Dict], center_lat: float, center_lon: float,
                  radius: float, path: str, group_distance: float = 20,
                  group_by: str = "clearance", ortho_cache: Optional[str] = None):
    """Save an interactive map (Leaflet) with OpenTopoMap, OSM and geoportal orthophoto.

    Lines are grouped in the browser; the panel on the map switches grouping on
    and off and sets the grouping distance and representative line.
    Orthophoto tiles are read from ortho_cache (a {z}/{x}/{y}.jpg folder) when
    present there, and from the geoportal WMS otherwise.
    """
    import folium
    import xyzservices.providers as xyz
    from branca.element import MacroElement
    from folium.template import Template
    from folium.utilities import JsCode

    class OrthophotoLayer(folium.WmsTileLayer):
        # WMS layer whose options may contain JavaScript (the crs object) and which
        # tries the local tile cache first, falling back to the WMS for missing tiles
        _template = Template("""
            {% macro script(this, kwargs) %}
                var {{ this.get_name() }} = new (L.TileLayer.WMS.extend({
                    createTile: function(coords, done) {
                        var layer = this, cache = {{ this.cache_url|tojson }}, online = !cache;
                        var tile = document.createElement("img");
                        tile.alt = "";
                        tile.setAttribute("role", "presentation");
                        L.DomEvent.on(tile, "load", L.Util.bind(this._tileOnLoad, this, done, tile));
                        L.DomEvent.on(tile, "error", function(e) {
                            if (!online) { online = true; tile.src = layer.getTileUrl(coords); }
                            else layer._tileOnError(done, tile, e);
                        });
                        tile.src = online ? this.getTileUrl(coords)
                                          : cache + "/" + coords.z + "/" + coords.x + "/" + coords.y + ".jpg";
                        return tile;
                    }
                }))(
                    {{ this.url|tojson }},
                    {{ this.options|tojavascript }}
                );
            {% endmacro %}
            """)

    fmap = folium.Map(location=[center_lat, center_lon], tiles=None, control_scale=True)
    # The map's maximum zoom comes from its layers.
    # Beyond their native zoom, OpenTopoMap and OSM tiles are scaled up
    folium.TileLayer(xyz.OpenTopoMap, name="OpenTopoMap", max_zoom=MAX_ZOOM,
                     max_native_zoom=xyz.OpenTopoMap["max_zoom"]).add_to(fmap)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap", show=False, max_zoom=MAX_ZOOM,
                     max_native_zoom=xyz.OpenStreetMap.Mapnik["max_zoom"]).add_to(fmap)
    # The geoportal WMS does not support Web Mercator, so tiles are requested in WGS84.
    # WMS renders at any scale, so the orthophoto stays sharp up to MAX_ZOOM.
    ortho = OrthophotoLayer(ORTHOPHOTO_WMS, layers="Raster", fmt="image/jpeg", version="1.3.0",
                            name="Orthophoto (geoportal.gov.pl)", attr="GUGiK", max_zoom=MAX_ZOOM,
                            overlay=False, show=False, crs=JsCode("L.CRS.EPSG4326"))
    ortho.cache_url = _cache_url(ortho_cache, path) if ortho_cache else None
    ortho.add_to(fmap)

    folium.GeoJson(_search_area(center_lat, center_lon, radius).__geo_interface__,
                   name="Search area", interactive=False,
                   style_function=lambda _: {"color": "black", "weight": 1,
                                             "dashArray": "6", "fill": False}).add_to(fmap)

    lines = MacroElement()
    lines._template = Template(GROUPING_SCRIPT)
    lines.color_valid, lines.color_obstacle = COLOR_VALID, COLOR_OBSTACLE
    lines.distance, lines.by = group_distance, group_by
    # Values used for grouping are not rounded, so the browser groups exactly like group_lines()
    lines.lines = [
        [round(r["anchor1"][0], 7), round(r["anchor1"][1], 7),
         round(r["anchor2"][0], 7), round(r["anchor2"][1], 7),
         *r["a1_xy"], *r["a2_xy"], r["length"], r["clearance"],
         None if r["clearance_nmpt"] is None else round(r["clearance_nmpt"], 2),
         round(r["elev_a1"], 2), round(r["elev_a2"], 2), r["over_obstacles"]]
        for r in results]
    lines.add_to(fmap)

    folium.Marker([center_lat, center_lon], tooltip="Search centre").add_to(fmap)
    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.fit_bounds(_search_area(center_lat, center_lon, radius).total_bounds[[1, 0, 3, 2]]
                    .reshape(2, 2).tolist())
    fmap.save(path)
