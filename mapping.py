"""
Converting results to GPS coordinates and displaying them on maps.
"""

import dataclasses
import os
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from pyproj import Transformer

from grid import Grid
from helpers import LEASH_FALL, SLOPE, Line
from progress import progress
from sag import Rigging

MAX_ZOOM = 21  # about 5 cm per pixel, the detail of the best geoportal orthophotos

COLOR_VALID = "#FF10F0"     # neon pink
COLOR_OBSTACLE = "#FF8C00"  # orange: line passes too close to trees/buildings (NMPT)

PL1992 = "EPSG:2180"  # Polish national grid, accepted by --northing/--easting
_to_wgs84 = Transformer.from_crs(PL1992, "EPSG:4326", always_xy=True)


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
    easting, northing = grid.pixel_to_xy(rows, cols)
    lon, lat = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True).transform(easting, northing)

    results = []
    for k, line in enumerate(lines):
        over_obstacles = (nmpt_mode == "mark" and line.clearance_nmpt is not None
                          and line.clearance_nmpt <= 0)
        results.append({
            "anchor1": (float(lat[2 * k]), float(lon[2 * k])),
            "anchor2": (float(lat[2 * k + 1]), float(lon[2 * k + 1])),
            # (easting, northing) in metres in the grid CRS, used for grouping
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
    progress.compute.start("grouping lines", len(results), unit="line")
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
            progress.compute.update(len(members), spots=len(groups))
    progress.compute.finish(f"{len(groups)} spots")
    # Clear lines first, then lines over trees/buildings; best first within each
    groups.sort(key=lambda g: (not g["rep"]["over_obstacles"], rank(g["rep"])), reverse=True)
    return groups


def _search_area(center_lat: float, center_lon: float, radius: float):
    """Search circle as a polygon in WGS84 (geopandas)."""
    import geopandas as gpd
    from shapely.geometry import Point

    local = f"+proj=aeqd +lat_0={center_lat} +lon_0={center_lon} +datum=WGS84 +units=m"
    circle = gpd.GeoSeries([Point(0, 0).buffer(radius, 128)], crs=local)
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
  .fal-profile { position: absolute; left: 0; right: 0; bottom: 0; height: 270px; z-index: 1000;
                 background: white; box-shadow: 0 -1px 6px rgba(0,0,0,0.35); font: 12px/1.5 sans-serif; }
  .fal-profile-open .leaflet-bottom { bottom: 270px; }
  .fal-profile-controls { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 14px;
                          padding: 3px 10px; color: #444; }
  .fal-profile-controls label { margin-right: 10px; white-space: nowrap; }
  .fal-profile-controls input[type=number] { width: 52px; font: inherit; }
  .fal-profile-head { display: flex; justify-content: space-between; align-items: center;
                      gap: 8px; padding: 4px 10px; border-bottom: 1px solid #eee; }
  .fal-profile-head button { font: inherit; cursor: pointer; }
  .fal-profile-legend span { margin-left: 10px; white-space: nowrap; color: #555; }
  .fal-profile-chart svg { display: block; }
  .fal-profile-readout { padding: 0 10px; color: #333; min-height: 18px; }
</style>
{% endmacro %}

{% macro script(this, kwargs) %}
(function(map) {
  var COLORS = {clean: {{ this.color_valid|tojson }}, over: {{ this.color_obstacle|tojson }}};
  var lines = {{ this.lines|tojson }}.map(function(d) {
    return {lat1: d[0], lon1: d[1], lat2: d[2], lon2: d[3], x1: d[4], y1: d[5], x2: d[6], y2: d[7],
            len: d[8], clr: d[9], clrN: d[10], e1: d[11], e2: d[12], over: d[13], prof: d[14]};
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
    if (l.prof && l.prof.water_m > 0)
      html += "<br><b>Over water:</b> " + l.prof.water_m.toFixed(0) + " m of " + l.len.toFixed(0) + " m";
    if (l.prof && l.prof.filled_m > 0)
      html += "<br><b>Over filled gaps in the data:</b> " + l.prof.filled_m.toFixed(0) + " m (less precise)";
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

  // Sag estimator: the same algorithm as sag.py (keep both in sync)
  var Sag = (function() {
    var G = 9.80665, POINTS = 40;
    function constants(r) {
      return {H0: r.pretension * 1e3, EA: 1e4 / (r.stretch / 100),
              w: Math.max(r.webbing_weight / 1e3 * G, 1e-6), P: r.athlete_mass * G};
    }
    function segment(H, V, s, w, EA) {
      var Ve = V - w * s;
      return [H * s / EA + H / w * (Math.asinh(V / H) - Math.asinh(Ve / H)),
              (V * s - 0.5 * w * s * s) / EA + (Math.hypot(H, V) - Math.hypot(H, Ve)) / w, Ve];
    }
    function norm(r) { return Math.sqrt(r.reduce(function(a, v) { return a + v * v; }, 0)); }
    function finite(r) { return r.every(function(v) { return isFinite(v); }); }
    function solveLinear(A, b) {  // Gaussian elimination with partial pivoting
      var n = b.length, M = A.map(function(row, i) { return row.concat([b[i]]); });
      for (var c = 0; c < n; c++) {
        var p = c;
        for (var r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
        if (Math.abs(M[p][c]) < 1e-300) return null;
        var tmp = M[c]; M[c] = M[p]; M[p] = tmp;
        for (r = c + 1; r < n; r++) {
          var k = M[r][c] / M[c][c];
          for (var j = c; j <= n; j++) M[r][j] -= k * M[c][j];
        }
      }
      var x = new Array(n);
      for (var i = n - 1; i >= 0; i--) {
        var s = M[i][n];
        for (j = i + 1; j < n; j++) s -= M[i][j] * x[j];
        x[i] = s / M[i][i];
      }
      return x;
    }
    function newton(f, x0, scale) {  // damped Newton's method, forward-difference Jacobian
      var x = x0.slice(), r = f(x);
      if (!finite(r)) return null;
      for (var it = 0; it < 60; it++) {
        var n = norm(r);
        if (n < 1e-9) return x;
        var J = r.map(function() { return []; });
        for (var j = 0; j < x.length; j++) {
          var h = 1e-7 * Math.max(Math.abs(x[j]), scale[j]), xh = x.slice();
          xh[j] += h;
          var rh = f(xh);
          for (var i = 0; i < r.length; i++) J[i][j] = (rh[i] - r[i]) / h;
        }
        var step = solveLinear(J, r.map(function(v) { return -v; }));
        if (!step) return null;
        var lam = 1, xn = null, rn = null;
        while (lam > 1e-4) {
          xn = x.map(function(v, k) { return v + lam * step[k]; });
          rn = f(xn);
          if (finite(rn) && norm(rn) < n) break;
          lam /= 2;
        }
        if (lam <= 1e-4) return n < 1e-6 ? x : null;
        x = xn; r = rn;
      }
      return norm(r) < 1e-6 ? x : null;
    }
    function unstretched(span, dh, c) {
      var chord = Math.hypot(span, dh);
      var v = newton(function(v) {
        var e = segment(c.H0, v[0], v[1], c.w, c.EA);
        return [e[0] - span, e[1] - dh];
      }, [0.5 * c.w * chord + c.H0 * dh / span, chord / (1 + c.H0 / c.EA)], [c.H0, span]);
      return v && {L0: v[1], V: v[0]};
    }
    function loaded(span, dh, c, x, L0, guess) {
      function residualFor(P) {
        return function(v) {
          var a = segment(v[0], v[1], v[2], c.w, c.EA), b = segment(v[0], a[2] - P, L0 - v[2], c.w, c.EA);
          return [a[0] + b[0] - span, a[1] + b[1] - dh, a[0] - x];
        };
      }
      var scale = [c.H0, c.H0, span];
      if (!guess) {
        var u = unstretched(span, dh, c);
        if (!u) return null;
        guess = [c.H0, u.V, L0 * x / span];
        for (var k = 1; k <= 8 && guess; k++) guess = newton(residualFor(c.P * k / 8), guess, scale);
      } else guess = newton(residualFor(c.P), guess, scale);
      if (!guess) return null;
      var y1 = segment(guess[0], guess[1], guess[2], c.w, c.EA)[1];
      return {H: guess[0], sag: y1 - dh / span * x, solution: guess};
    }
    function loadedCurve(c, s, L0, n) {
      var H = s.solution[0], V = s.solution[1], u = s.solution[2];
      var k1 = Math.max(1, Math.round(n * u / L0)), pts = [], k;
      for (k = 0; k <= k1; k++) pts.push(segment(H, V, u * k / k1, c.w, c.EA).slice(0, 2));
      var end = segment(H, V, u, c.w, c.EA), k2 = Math.max(1, n - k1);
      for (k = 1; k <= k2; k++) {
        var p = segment(H, end[2] - c.P, (L0 - u) * k / k2, c.w, c.EA);
        pts.push([end[0] + p[0], end[1] + p[1]]);
      }
      return pts;
    }
    // Everything the profile needs for one line: unloaded webbing and the envelope
    function estimate(span, dh, rigging) {
      var c = constants(rigging), u = unstretched(span, dh, c);
      if (!u) return null;
      var unloadedPts = [];
      for (var k = 0; k <= 50; k++) unloadedPts.push(segment(c.H0, u.V, u.L0 * k / 50, c.w, c.EA).slice(0, 2));
      var n = POINTS + 2, f = [], sag = [], H = [], solutions = [], mid = Math.floor(n / 2);
      for (k = 0; k < n; k++) { f.push(k / (n - 1)); sag.push(0); H.push(c.H0); solutions.push(null); }
      var first = loaded(span, dh, c, f[mid] * span, u.L0);
      if (!first) return null;
      sag[mid] = first.sag; H[mid] = first.H; solutions[mid] = first.solution;
      for (var dir = -1; dir <= 1; dir += 2) {
        var guess = first.solution;
        for (k = mid + dir; dir < 0 ? k > 0 : k < n - 1; k += dir) {
          var r = loaded(span, dh, c, f[k] * span, u.L0, guess) || loaded(span, dh, c, f[k] * span, u.L0);
          if (!r) return null;
          sag[k] = r.sag; H[k] = r.H; solutions[k] = r.solution; guess = r.solution;
        }
      }
      return {c: c, L0: u.L0, unloaded: unloadedPts, f: f, sag: sag, H: H, solutions: solutions,
              sagAt: function(t) {  // envelope at fraction t (linear interpolation)
                var i = Math.min(n - 2, Math.floor(t * (n - 1))), a = t * (n - 1) - i;
                return sag[i] * (1 - a) + sag[i + 1] * a;
              },
              athlete: function(x) {  // loaded webbing with the athlete at x
                var t = x / span, i = Math.max(1, Math.min(n - 2, Math.round(t * (n - 1))));
                var s = loaded(span, dh, c, x, u.L0, solutions[i]) || loaded(span, dh, c, x, u.L0);
                return s && {sag: s.sag, H: s.H, curve: loadedCurve(c, s, u.L0, 60)};
              }};
    }
    return {estimate: estimate};
  })();

  // Terrain profile of the clicked line, in a panel along the bottom of the map
  var SLOPE = {{ this.slope|tojson }}, LEASH_FALL = {{ this.leash_fall|tojson }};
  var MODEL = {{ this.model|tojson }};
  var PROFILE = {terrain: "#8a7656", terrainFill: "#e3d8c3", trees: "#3f8f35",
                 treesFill: "rgba(76,153,60,0.45)", limit: "#d62728", envelope: "#6a3d9a",
                 fallFill: "rgba(214,39,40,0.12)", athlete: "#222", water: "#2f6fc0",
                 waterFill: "rgba(47,111,192,0.45)"};
  var RIGGING = [["pretension", "kN pretension", 0.1], ["stretch", "% stretch @ 10 kN", 0.5],
                 ["webbing_weight", "g/m webbing", 5], ["athlete_mass", "kg athlete", 5],
                 ["fall_zone", "m fall zone", 0.5]];
  var LAYERS = [["webbing", "webbing"], ["athlete", "athlete under cursor"],
                ["envelope", "sag envelope"], ["fall", "fall zone"]];
  var profile = {panel: null, line: null, equal: false, marker: null, estimate: null, key: null,
                 rigging: {{ this.rigging|tojson }},
                 show: {webbing: true, athlete: true, envelope: true, fall: true}};

  function svg(tag, attrs, text) {
    var s = "<" + tag;
    for (var k in attrs) s += " " + k + "='" + attrs[k] + "'";
    return text === undefined ? s + "/>" : s + ">" + text + "</" + tag + ">";
  }
  function niceStep(span, count) {
    var raw = span / count, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var steps = [1, 2, 5, 10];
    for (var i = 0; i < steps.length; i++) if (steps[i] * mag >= raw) return steps[i] * mag;
    return 10 * mag;
  }
  function formulaLimit(l, d) {
    return Math.min(l.e1, l.e2) - SLOPE * Math.min(d, l.len - d) - LEASH_FALL;
  }

  function showProfile(l) {
    if (!l.prof) return;
    profile.line = l;
    if (!profile.panel) createProfilePanel();
    profile.panel.hidden = false;
    L.DomUtil.addClass(map.getContainer(), "fal-profile-open");
    drawProfile();
  }
  function hideProfile() {
    profile.panel.hidden = true;
    L.DomUtil.removeClass(map.getContainer(), "fal-profile-open");
    removeMarker();
  }
  function removeMarker() {
    if (profile.marker) { map.removeLayer(profile.marker); profile.marker = null; }
  }
  function swatch(style) { return "<span class='fal-swatch' style='" + style + "'></span> "; }
  function createProfilePanel() {
    var div = L.DomUtil.create("div", "fal-profile", map.getContainer());
    div.innerHTML =
      "<div class='fal-profile-head'><div><b class='fal-profile-title'></b>" +
      "<span class='fal-profile-legend'>" +
      "<span>" + swatch("height:8px;background:" + PROFILE.terrainFill + ";border:1px solid " + PROFILE.terrain) + "terrain</span>" +
      "<span>" + swatch("height:8px;background:" + PROFILE.treesFill) + "trees/buildings</span>" +
      "<span>" + swatch("height:8px;background:" + PROFILE.waterFill) + "water</span>" +
      "<span>" + swatch("border-top:2px dashed " + PROFILE.limit + ";height:0") + "safety limit (" +
      (MODEL === "sag" ? "sag model" : "formula") + ")</span></span></div>" +
      "<div><button class='fal-scale'></button> <button class='fal-close' title='Close'>&times;</button></div></div>" +
      "<div class='fal-profile-controls'><span class='fal-rigging'></span><span class='fal-layers'></span></div>" +
      "<div class='fal-profile-chart'></div><div class='fal-profile-readout'></div>";
    L.DomEvent.disableClickPropagation(div);
    L.DomEvent.disableScrollPropagation(div);

    var rigging = div.querySelector(".fal-rigging");
    RIGGING.forEach(function(r) {
      var label = document.createElement("label"), input = document.createElement("input");
      input.type = "number"; input.min = 0; input.step = r[2]; input.value = profile.rigging[r[0]];
      input.addEventListener("change", function() {
        var v = parseFloat(input.value);
        if (isFinite(v) && (v > 0 || (r[0] !== "pretension" && r[0] !== "stretch" && v >= 0))) {
          profile.rigging[r[0]] = v;
          drawProfile();
        } else input.value = profile.rigging[r[0]];
      });
      label.appendChild(input);
      label.appendChild(document.createTextNode(" " + r[1]));
      rigging.appendChild(label);
    });
    var layers = div.querySelector(".fal-layers");
    var layerStyle = {webbing: "height:3px;background:" + COLORS.clean,
                      athlete: "height:2px;background:" + PROFILE.athlete,
                      envelope: "border-top:2px dotted " + PROFILE.envelope + ";height:0",
                      fall: "height:8px;background:" + PROFILE.fallFill + ";border:1px solid " + PROFILE.limit};
    LAYERS.forEach(function(layer) {
      var label = document.createElement("label"), box = document.createElement("input");
      box.type = "checkbox"; box.checked = profile.show[layer[0]];
      box.addEventListener("change", function() { profile.show[layer[0]] = box.checked; drawProfile(); });
      label.appendChild(box);
      label.insertAdjacentHTML("beforeend", " " + swatch(layerStyle[layer[0]]) + layer[1]);
      layers.appendChild(label);
    });

    div.querySelector(".fal-close").addEventListener("click", hideProfile);
    div.querySelector(".fal-scale").addEventListener("click", function() {
      profile.equal = !profile.equal;
      drawProfile();
    });
    window.addEventListener("resize", function() { if (!div.hidden) drawProfile(); });
    profile.panel = div;
  }

  function currentEstimate(l) {
    var key = [l.x1, l.y1, l.x2, l.y2, JSON.stringify(profile.rigging)].join("|");
    if (profile.key !== key) {
      profile.key = key;
      profile.estimate = Sag.estimate(l.len, l.e1 - l.e2, profile.rigging);
    }
    return profile.estimate;
  }

  function drawProfile() {
    var l = profile.line, p = l.prof, panel = profile.panel, est = currentEstimate(l);
    var chart = panel.querySelector(".fal-profile-chart");
    var W = chart.clientWidth || 800, H = 170, m = {l: 58, r: 20, t: 18, b: 24};
    var pw = W - m.l - m.r, ph = H - m.t - m.b;
    var color = l.over ? COLORS.over : COLORS.clean;
    var chord = function(d) { return l.e1 + (l.e2 - l.e1) * d / l.len; };
    var fall = profile.rigging.fall_zone;
    var envelopeAt = function(d) { return chord(d) - est.sagAt(d / l.len); };
    var limit = function(d) {
      if (MODEL !== "sag") return formulaLimit(l, d);
      return est ? envelopeAt(d) - fall : NaN;
    };

    // Vertical range: everything that is drawn
    var values = p.z.concat(p.s || [], [l.e1, l.e2]);
    var dsamples = [];
    for (var q = 0; q <= 40; q++) dsamples.push(l.len * q / 40);
    dsamples.forEach(function(d) {
      values.push(limit(d));
      if (est) values.push(envelopeAt(d) - (profile.show.fall ? fall : 0));
    });
    values = values.filter(function(v) { return v !== null && isFinite(v); });
    var zmin = Math.min.apply(null, values), zmax = Math.max.apply(null, values);
    var pad = Math.max((zmax - zmin) * 0.08, 1);
    zmin -= pad; zmax += pad;
    var sx = pw / l.len, sy = ph / (zmax - zmin), xoff = 0;
    if (profile.equal) {  // same metres per pixel on both axes
      sx = sy = Math.min(sx, sy);
      xoff = (pw - l.len * sx) / 2;
    }
    var zmid = (zmin + zmax) / 2;
    var X = function(d) { return m.l + xoff + d * sx; };
    var Y = function(z) { return m.t + ph / 2 - (z - zmid) * sy; };
    var visibleMin = zmid - ph / 2 / sy, visibleMax = zmid + ph / 2 / sy;
    var pt = function(d, z) { return X(d).toFixed(1) + "," + Y(z).toFixed(1); };

    var out = "";
    // Grid and axis labels
    var yStep = niceStep(visibleMax - visibleMin, 4);
    for (var z = Math.ceil(visibleMin / yStep) * yStep; z <= visibleMax; z += yStep) {
      out += svg("line", {x1: m.l, x2: W - m.r, y1: Y(z), y2: Y(z), stroke: "#eee"});
      out += svg("text", {x: m.l - 6, y: Y(z) + 4, "text-anchor": "end", fill: "#777"}, z.toFixed(0) + " m");
    }
    var xStep = niceStep(l.len, 6);
    for (var d = 0; d <= l.len + 0.01; d += xStep) {
      out += svg("text", {x: X(d), y: H - 6, "text-anchor": "middle", fill: "#777"}, d.toFixed(0) + " m");
    }

    // Fall zone: from the sag envelope down by the fall zone height
    if (est && profile.show.fall) {
      var upper = dsamples.map(function(d) { return pt(d, envelopeAt(d)); });
      var lower = dsamples.map(function(d) { return pt(d, envelopeAt(d) - fall); }).reverse();
      out += svg("polygon", {points: upper.concat(lower).join(" "), fill: PROFILE.fallFill, stroke: "none"});
    }

    // Terrain (NMT) and trees/buildings (NMPT)
    var ground = [], top = [], bottom = [], i;
    for (i = 0; i < p.d.length; i++) if (p.z[i] !== null) ground.push(pt(p.d[i], p.z[i]));
    if (p.s) {
      for (i = 0; i < p.d.length; i++) {
        if (p.s[i] === null || p.z[i] === null) continue;
        top.push(pt(p.d[i], Math.max(p.s[i], p.z[i])));
        bottom.unshift(pt(p.d[i], p.z[i]));
      }
      out += svg("polygon", {points: top.concat(bottom).join(" "), fill: PROFILE.treesFill, stroke: "none"});
    }
    out += svg("polygon", {points: ground.concat([X(l.len).toFixed(1) + "," + (H - m.b),
                                                  X(0).toFixed(1) + "," + (H - m.b)]).join(" "),
                           fill: PROFILE.terrainFill, stroke: PROFILE.terrain, "stroke-width": 1});
    // Water (sea and lakes): each run of water points, from the surface down
    if (p.w) {
      var run = [];
      var flush = function() {
        if (run.length > 1) {
          out += svg("polygon", {points: run.map(function(j) { return pt(p.d[j], p.z[j]); }).concat(
                       [X(p.d[run[run.length - 1]]).toFixed(1) + "," + (H - m.b),
                        X(p.d[run[0]]).toFixed(1) + "," + (H - m.b)]).join(" "),
                     fill: PROFILE.waterFill, stroke: PROFILE.water, "stroke-width": 1});
        }
        run = [];
      };
      for (i = 0; i < p.d.length; i++) {
        if (p.w[i] && p.z[i] !== null) run.push(i); else flush();
      }
      flush();
    }

    // Safety limit used by the search
    out += svg("polyline", {points: dsamples.map(function(d) { return pt(d, limit(d)); }).join(" "),
                            fill: "none", stroke: PROFILE.limit, "stroke-width": 1.5, "stroke-dasharray": "6 4"});

    // Sag estimator: envelope and the webbing without the athlete
    if (est && profile.show.envelope) {
      out += svg("polyline", {points: dsamples.map(function(d) { return pt(d, envelopeAt(d)); }).join(" "),
                              fill: "none", stroke: PROFILE.envelope, "stroke-width": 1.5, "stroke-dasharray": "2 3"});
    }
    if (profile.show.webbing) {
      var webbing = est ? est.unloaded.map(function(e) { return pt(e[0], l.e1 - e[1]); })
                        : [pt(0, l.e1), pt(l.len, l.e2)];
      out += svg("polyline", {points: webbing.join(" "), fill: "none", stroke: color, "stroke-width": 2.5});
    }
    out += svg("polyline", {"class": "fal-loaded", fill: "none", stroke: PROFILE.athlete, "stroke-width": 1.5,
                            visibility: "hidden"});
    out += svg("circle", {"class": "fal-athlete", r: 4, fill: PROFILE.athlete, visibility: "hidden"});

    // Smallest clearance over terrain and over trees/buildings (from the search)
    function minMarker(min, stroke, label, below) {
      var topZ = limit(min[0]), bottomZ = topZ - min[1];
      if (!isFinite(topZ)) return;
      var right = min[0] > l.len / 2;  // label on the inner side, so it is not cut off
      out += svg("line", {x1: X(min[0]), x2: X(min[0]), y1: Y(topZ), y2: Y(bottomZ), stroke: stroke,
                          "stroke-width": 2});
      out += svg("text", {x: X(min[0]) + (right ? -4 : 4), y: (below ? Y(bottomZ) + 12 : Y(topZ) - 4),
                          fill: stroke, "text-anchor": right ? "end" : "start"},
                 label + " " + min[1].toFixed(1) + " m");
    }
    minMarker(p.min, PROFILE.terrain, "min. clearance", true);
    if (p.min_nmpt && p.min_nmpt[1] < p.min[1] - 0.05) minMarker(p.min_nmpt, PROFILE.trees, "over trees", false);

    // Anchors
    [[0, l.e1, "A1", "start"], [l.len, l.e2, "A2", "end"]].forEach(function(a) {
      out += svg("circle", {cx: X(a[0]), cy: Y(a[1]), r: 4, fill: color});
      out += svg("text", {x: X(a[0]) + (a[3] === "start" ? 6 : -6), y: Y(a[1]) - 7, "text-anchor": a[3],
                          "font-weight": "bold"}, a[2] + " " + a[1].toFixed(1) + " m");
    });

    // Hover readout
    out += svg("line", {"class": "fal-cursor", y1: m.t, y2: H - m.b, stroke: "#333", "stroke-dasharray": "2 2",
                        visibility: "hidden"});
    out += svg("rect", {"class": "fal-hover", x: m.l, y: m.t, width: pw, height: ph, fill: "transparent"});
    chart.innerHTML = "<svg width='" + W + "' height='" + H + "' font-size='11'>" + out + "</svg>";

    var dh = Math.abs(l.e1 - l.e2);
    panel.querySelector(".fal-profile-title").textContent =
      "Profile: " + l.len.toFixed(0) + " m, A1 " + l.e1.toFixed(1) + " m → A2 " + l.e2.toFixed(1) +
      " m (Δh " + dh.toFixed(1) + " m), min. clearance " + l.clr.toFixed(1) + " m";
    panel.querySelector(".fal-scale").textContent =
      profile.equal ? "Auto scale" : "1:1 scale (now vertical ×" + (sy / sx).toFixed(1) + ")";

    var summary = "Sag estimator did not converge for these values.";
    if (est) {
      var k = est.sag.indexOf(Math.max.apply(null, est.sag));
      summary = "Sag estimator: max sag " + est.sag[k].toFixed(2) + " m below the chord with the athlete at " +
        (est.f[k] * l.len).toFixed(0) + " m (line tension " + (est.H[k] / 1e3).toFixed(1) +
        " kN); without the athlete " + Math.max.apply(null, est.unloaded.map(function(e, j) {
          return e[1] - (l.e1 - l.e2) * e[0] / l.len; })).toFixed(2) + " m.";
    }
    var readout = panel.querySelector(".fal-profile-readout");
    readout.textContent = summary;

    var svgEl = chart.querySelector("svg"), cursor = chart.querySelector(".fal-cursor");
    var hover = chart.querySelector(".fal-hover"), loadedLine = chart.querySelector(".fal-loaded");
    var athleteDot = chart.querySelector(".fal-athlete");
    hover.addEventListener("mousemove", function(e) {
      var box = svgEl.getBoundingClientRect();
      var d = Math.max(0, Math.min(l.len, (e.clientX - box.left - m.l - xoff) / sx));
      var lo = 0, hi = p.d.length - 1;  // nearest terrain sample
      while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (p.d[mid] < d) lo = mid; else hi = mid; }
      var k = (d - p.d[lo] < p.d[hi] - d) ? lo : hi;
      cursor.setAttribute("x1", X(d));
      cursor.setAttribute("x2", X(d));
      cursor.setAttribute("visibility", "visible");

      var text = d.toFixed(0) + " m from A1";
      if (p.z[k] !== null) text += " · terrain " + p.z[k].toFixed(1) + " m";
      if (p.w && p.w[k]) text += " (water)";
      if (p.g && p.g[k]) text += " (filled gap)";
      if (p.s && p.s[k] !== null) text += " · trees/buildings " + p.s[k].toFixed(1) + " m";
      var lim = limit(d);
      if (isFinite(lim)) {
        text += " · safety limit " + lim.toFixed(1) + " m";
        if (p.z[k] !== null) text += " · clearance " + (lim - p.z[k]).toFixed(1) + " m";
        if (p.s && p.s[k] !== null) text += " (over trees/buildings " + (lim - Math.max(p.s[k], p.z[k])).toFixed(1) + " m)";
      }
      var athlete = est && profile.show.athlete && d > 0.01 * l.len && d < 0.99 * l.len ? est.athlete(d) : null;
      if (athlete) {
        loadedLine.setAttribute("points", athlete.curve.map(function(e) { return pt(e[0], l.e1 - e[1]); }).join(" "));
        loadedLine.setAttribute("visibility", "visible");
        athleteDot.setAttribute("cx", X(d));
        athleteDot.setAttribute("cy", Y(chord(d) - athlete.sag));
        athleteDot.setAttribute("visibility", "visible");
        text += " · athlete here: sag " + athlete.sag.toFixed(2) + " m, tension " + (athlete.H / 1e3).toFixed(1) + " kN";
        if (p.z[k] !== null) text += ", " + (chord(d) - athlete.sag - p.z[k]).toFixed(1) + " m above terrain";
      } else {
        loadedLine.setAttribute("visibility", "hidden");
        athleteDot.setAttribute("visibility", "hidden");
      }
      readout.textContent = text;

      var t = d / l.len, at = [l.lat1 + (l.lat2 - l.lat1) * t, l.lon1 + (l.lon2 - l.lon1) * t];
      if (!profile.marker) {
        profile.marker = L.circleMarker(at, {radius: 6, color: "#000", weight: 2, fillColor: "#fff",
                                             fillOpacity: 1, interactive: false}).addTo(map);
      } else profile.marker.setLatLng(at);
    });
    hover.addEventListener("mouseleave", function() {
      cursor.setAttribute("visibility", "hidden");
      loadedLine.setAttribute("visibility", "hidden");
      athleteDot.setAttribute("visibility", "hidden");
      readout.textContent = summary;
      removeMarker();
    });
  }

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
      pl.on("popupopen", function() { showProfile(l); });
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
        "Anchors within <input type='range' class='fal-distance' min='5' max='" +
        Math.max(100, Math.round(3 * state.distance)) + "' step='1'> " +
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


# Panel with the run's parameters and a list of all saved runs (from runs.js).
# Without runs.js (e.g. the map was moved) only the current run is listed.
RUNS_SCRIPT = """
{% macro header(this, kwargs) %}
<script src="{{ this.runs_js|e }}"></script>
<style>
  .fal-run { max-width: 300px; }
  .fal-run select { max-width: 100%; margin: 2px 0; }
  .fal-run .fal-params { color: #555; }
</style>
{% endmacro %}

{% macro script(this, kwargs) %}
(function(map) {
  var current = {{ this.run|tojson }};
  var runs = (window.FINDALINE_RUNS || [current]).filter(function(r) {
    return r.has_map !== false || r.id === current.id;
  });
  var label = function(r) { return r.created.slice(0, 16).replace("T", " ") + " - " + r.name; };
  var Panel = L.Control.extend({
    options: {position: "topleft"},
    onAdd: function() {
      var div = L.DomUtil.create("div", "fal-panel fal-run");
      div.innerHTML = "<b>Run</b> &middot; <a>all runs</a><br>";
      div.querySelector("a").href = {{ this.index_href|tojson }};
      var select = document.createElement("select");
      runs.forEach(function(r) {
        var option = document.createElement("option");
        option.value = r.id;
        option.textContent = label(r);
        option.selected = r.id === current.id;
        select.appendChild(option);
      });
      select.addEventListener("change", function() {
        location.href = {{ this.runs_base|tojson }} + encodeURIComponent(select.value) + "/map.html";
      });
      div.appendChild(select);
      var params = document.createElement("div");
      params.className = "fal-params";
      params.textContent = current.description;
      div.appendChild(params);
      L.DomEvent.disableClickPropagation(div);
      L.DomEvent.disableScrollPropagation(div);
      return div;
    }
  });
  new Panel().addTo(map);
})({{ this._parent.get_name() }});
{% endmacro %}
"""


def _relative_url(folder: str, html_path: str) -> str:
    """URL of a folder as seen from the HTML file (relative when possible)."""
    try:
        return os.path.relpath(folder, os.path.dirname(os.path.abspath(html_path))).replace(os.sep, "/")
    except ValueError:  # different drive on Windows
        return Path(os.path.abspath(folder)).as_uri()


def save_html_map(results: List[Dict], center_lat: float, center_lon: float,
                  radius: float, path: str, group_distance: float = 20,
                  group_by: str = "clearance", ortho_cache: Optional[str] = None,
                  ortho_service=None,
                  run: Optional[Dict] = None, runs_dir: Optional[str] = None,
                  model: str = "formula", rigging: Optional[Rigging] = None):
    """Save an interactive map (Leaflet) with OpenTopoMap, OSM, Esri imagery and the
    orthophoto of the data source (ortho_service, a sources.OrthoService) if it has one.

    Lines are grouped in the browser; the panel on the map switches grouping on
    and off and sets the grouping distance and representative line.
    Orthophoto tiles are read from ortho_cache (a {z}/{x}/{y}.jpg folder) when
    present there, and from the WMS otherwise.
    With run (its run.json metadata) and runs_dir, the map shows the run's
    parameters and a list of all runs. model and rigging set the clearance model
    shown in the profile and the initial values of the sag estimator.
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
    if ortho_service:
        # National WMS (e.g. geoportal) may not support Web Mercator, so tiles are requested
        # in WGS84. WMS renders at any scale, so the orthophoto stays sharp up to MAX_ZOOM.
        ortho = OrthophotoLayer(ortho_service.url, layers=ortho_service.layer, fmt="image/jpeg",
                                version="1.3.0", name=ortho_service.name,
                                attr=ortho_service.attribution, max_zoom=MAX_ZOOM,
                                overlay=False, show=False, crs=JsCode("L.CRS.EPSG4326"))
        ortho.cache_url = _relative_url(ortho_cache, path) if ortho_cache else None
        ortho.add_to(fmap)
    # Worldwide imagery, online only (Esri's terms do not allow caching tiles for offline use)
    folium.TileLayer(xyz.Esri.WorldImagery, name="Esri World Imagery (online)", show=False,
                     max_zoom=MAX_ZOOM, max_native_zoom=xyz.Esri.WorldImagery.get("max_zoom", 18)
                     ).add_to(fmap)

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
         round(r["elev_a1"], 2), round(r["elev_a2"], 2), r["over_obstacles"], r.get("profile")]
        for r in results]
    lines.slope, lines.leash_fall = SLOPE, LEASH_FALL
    lines.model = model
    lines.rigging = dataclasses.asdict(rigging or Rigging())
    lines.add_to(fmap)

    if run and runs_dir:
        runs_panel = MacroElement()
        runs_panel._template = Template(RUNS_SCRIPT)
        runs_base = _relative_url(runs_dir, path) + "/"
        runs_panel.run = run
        runs_panel.runs_js = runs_base + "runs.js"
        runs_panel.index_href = runs_base + "index.html"
        runs_panel.runs_base = runs_base
        runs_panel.add_to(fmap)

    folium.Marker([center_lat, center_lon], tooltip="Search centre").add_to(fmap)
    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.fit_bounds(_search_area(center_lat, center_lon, radius).total_bounds[[1, 0, 3, 2]]
                    .reshape(2, 2).tolist())
    fmap.save(path)
