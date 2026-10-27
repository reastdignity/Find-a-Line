"""
Saving every run (map, results, parameters) so earlier runs can be browsed.

Layout:
    runs/<date>_<place>/map.html       interactive map (the viewer, see mapping.save_map)
    runs/<date>_<place>/data.js        its data: lines and profiles
    runs/<date>_<place>/lines.js       its lines without profiles, for the "Other runs" of other maps
    runs/<date>_<place>/lines.csv      every valid line with its group
    runs/<date>_<place>/lines.geojson  the same as GeoJSON
    runs/<date>_<place>/run.json       parameters and summary
    runs/runs.js                       list of all runs, loaded by every map
    runs/_viewer/                      the viewer bundle shared by every map
    runs/index.html                    table of all runs
A page opened from disk cannot list a folder, so runs.js and index.html are
rewritten after every run.
"""

import csv
import html
import json
import math
import os
import re
import unicodedata
from datetime import datetime
from typing import Dict, List, Optional

import requests

import osm

MAX_FEATURE_DEG = 0.2  # about 20 km; larger natural features are not used in run names


def _reverse(lat: float, lon: float, cache_dir: str, **params) -> Dict:
    return osm.reverse(lat, lon, cache_dir, **{"accept-language": "pl"}, **params)


def _nearest_feature(lat: float, lon: float, cache_dir: str) -> Optional[str]:
    """Name of the nearest natural feature: rock, peak, valley, river...

    Very large features (a peninsula, a sea strait) say nothing about the spot
    and are skipped.
    """
    data = _reverse(lat, lon, cache_dir, zoom=18, layer="natural")
    try:
        south, north, west, east = map(float, data.get("boundingbox", []))
        if north - south > MAX_FEATURE_DEG or (east - west) * math.cos(math.radians(lat)) > MAX_FEATURE_DEG:
            return None
    except ValueError:
        pass
    return data.get("name") or None


def _locality(lat: float, lon: float, cache_dir: str) -> Optional[str]:
    """Hamlet, village or town at the point."""
    data = _reverse(lat, lon, cache_dir, zoom=14)
    if data.get("category") in ("place", "boundary") and data.get("name"):
        return data["name"]
    address = data.get("address", {})
    return next((address[k] for k in ("hamlet", "village", "town", "city", "municipality")
                 if k in address), None)


def place_name(lat: float, lon: float, cache_dir: str) -> Optional[str]:
    """Name of the place from OpenStreetMap, e.g. "Żabi Koń, Gaj Kobylański".

    The nearest named natural feature plus the locality; None if offline.
    """
    parts = []
    for lookup in (_nearest_feature, _locality):
        try:
            parts.append(lookup(lat, lon, cache_dir))
        except (requests.RequestException, ValueError, KeyError):
            pass
    parts = list(dict.fromkeys(p for p in parts if p))  # drop empty and duplicates
    return ", ".join(parts) or None


def _slug(text: str) -> str:
    text = text.replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:40] or "run"


def new_run(runs_dir: str, name: str, created: datetime) -> Dict:
    """Create the folder for a run and return its id and path."""
    run_id = f"{created:%Y-%m-%d_%H%M%S}_{_slug(name)}"
    path = os.path.join(runs_dir, run_id)
    os.makedirs(path, exist_ok=True)
    return {"id": run_id, "path": path}


FIELDS = ["group", "representative", "group_size", "length_m", "clearance_m",
          "clearance_nmpt_m", "over_trees_buildings", "water_m", "filled_m",
          "height_max_m", "height_mean_m", "high_pct",
          "anchor1_lat", "anchor1_lon", "anchor1_elev_m", "anchor1_northing", "anchor1_easting",
          "anchor1_wall_angle",
          "anchor2_lat", "anchor2_lon", "anchor2_elev_m", "anchor2_northing", "anchor2_easting",
          "anchor2_wall_angle"]


def _rows(groups: List[Dict]):
    for number, group in enumerate(groups, 1):
        members = [group["rep"]] + [m for m in group["members"] if m is not group["rep"]]
        for r in members:
            yield {
                "group": number,
                "representative": r is group["rep"],
                "group_size": len(group["members"]),
                "length_m": round(r["length"], 1),
                "clearance_m": round(r["clearance"], 2),
                "clearance_nmpt_m": None if r["clearance_nmpt"] is None else round(r["clearance_nmpt"], 2),
                "over_trees_buildings": r["over_obstacles"],
                "water_m": r.get("water_m", 0.0),
                "filled_m": r.get("filled_m", 0.0),
                "height_max_m": round(r["height_max"], 2),
                "height_mean_m": round(r["height_mean"], 2),
                "high_pct": round(r["high_pct"], 1),
                "anchor1_lat": round(r["anchor1"][0], 7), "anchor1_lon": round(r["anchor1"][1], 7),
                "anchor1_elev_m": round(r["elev_a1"], 2),
                "anchor1_northing": round(r["a1_xy"][1], 2), "anchor1_easting": round(r["a1_xy"][0], 2),
                "anchor1_wall_angle": round(r["angle1"], 1),
                "anchor2_lat": round(r["anchor2"][0], 7), "anchor2_lon": round(r["anchor2"][1], 7),
                "anchor2_elev_m": round(r["elev_a2"], 2),
                "anchor2_northing": round(r["a2_xy"][1], 2), "anchor2_easting": round(r["a2_xy"][0], 2),
                "anchor2_wall_angle": round(r["angle2"], 1),
            }


LINES_CHUNK = 10_000  # lines written between two progress updates


def save_results(run_path: str, groups: List[Dict], meta: Dict, on_lines=lambda n, file: None):
    """Write lines.csv, lines.geojson and run.json for a run.

    on_lines(n, file) reports progress: three passes over the lines (the rows, lines.csv,
    lines.geojson), then run.json with n=0.
    """
    rows = []
    for row in _rows(groups):
        rows.append(row)
        if len(rows) % LINES_CHUNK == 0:
            on_lines(LINES_CHUNK, "lines.csv")
    on_lines(len(rows) % LINES_CHUNK, "lines.csv")
    with open(os.path.join(run_path, "lines.csv"), "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for start in range(0, len(rows), LINES_CHUNK):
            writer.writerows(rows[start:start + LINES_CHUNK])
            on_lines(len(rows[start:start + LINES_CHUNK]), "lines.csv")

    # The same text as json.dump of the whole collection, written in chunks of features
    encoder = json.JSONEncoder(ensure_ascii=False)
    with open(os.path.join(run_path, "lines.geojson"), "w", encoding="utf-8") as f:
        f.write('{"type": "FeatureCollection", "features": [')
        for start in range(0, len(rows), LINES_CHUNK):
            features = [{
                "type": "Feature",
                "geometry": {"type": "LineString",
                             "coordinates": [[row["anchor1_lon"], row["anchor1_lat"]],
                                             [row["anchor2_lon"], row["anchor2_lat"]]]},
                "properties": row,
            } for row in rows[start:start + LINES_CHUNK]]
            f.write((", " if start else "") + encoder.encode(features)[1:-1])
            on_lines(len(features), "lines.geojson")
        f.write('], "findaline": ' + encoder.encode(meta) + "}")

    with open(os.path.join(run_path, "run.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    on_lines(0, "run.json")


def list_runs(runs_dir: str) -> List[Dict]:
    """All saved runs, newest first."""
    runs = []
    if os.path.isdir(runs_dir):
        for run_id in os.listdir(runs_dir):
            path = os.path.join(runs_dir, run_id, "run.json")
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    run = json.load(f)
                run["has_map"] = os.path.isfile(os.path.join(runs_dir, run_id, "map.html"))
                run["has_lines"] = os.path.isfile(os.path.join(runs_dir, run_id, "lines.js"))
                runs.append(run)
    return sorted(runs, key=lambda r: r["created"], reverse=True)


def params_summary(meta: Dict) -> str:
    p = meta["params"]
    nmpt = f"NMPT {p['nmpt']}" if p["nmpt"] else "no NMPT"
    if p.get("area"):
        a = p["area"]
        where = f"area {a['area']}" + (f" ±{a['buffer']:g} m" if a["area"] in ("coast", "cliffs") else "") \
            + (f" within {a['within']}" if a.get("within") else "")
    else:
        where = f"radius {p['radius']:g} m"
    text = (f"{where}, {p['resolution']:g} m grid, slope ≥{p['slope_threshold']:g}°, "
            f"{p['min_length']:g}–{p['max_length']:g} m, {nmpt}")
    if "source" in meta:
        text = f"{meta['source']['name']}, {text}"
    if "max_anchor_diff" in p:
        text += f", Δh ≤{p['max_anchor_diff']:g}% / {p['max_anchor_diff_m']:g} m"
    limits = [f"{text}{p[key]:g}{unit}" for key, text, unit in (
        ("min_height", "height ≥", " m"), ("min_mean_height", "mean height ≥", " m"),
        ("min_high_share", f"≥{p.get('high_above', 20):g} m high over ≥", "%"),
        ("max_wall_angle", "wall angle ≤", "°")) if p.get(key) is not None]
    if limits:
        text += ", " + ", ".join(limits)
    if p.get("model") == "sag":
        r = p["rigging"]
        text += (f", sag model ({r['pretension']:g} kN, {r['stretch']:g}%, {r['webbing_weight']:g} g/m, "
                 f"{r['athlete_mass']:g} kg, fall {r['fall_zone']:g} m)")
    return text


def _bbox(run: Dict) -> Optional[List[float]]:
    """(west, south, east, north) of a run's search area."""
    area = run.get("area") or {}
    if area.get("type") == "polygon":
        from shapely.geometry import shape
        return list(shape(area["geometry"]).bounds)
    if area.get("type") == "circle":
        dlat = area["radius"] / 111320
        dlon = dlat / max(math.cos(math.radians(area["lat"])), 0.01)
        return [area["lon"] - dlon, area["lat"] - dlat, area["lon"] + dlon, area["lat"] + dlat]
    return None


def _listed(run: Dict) -> Dict:
    """A run as runs.js lists it: without the data files and the area outline, with its box."""
    listed = {k: v for k, v in run.items() if k not in ("data", "area")}
    listed["bbox"] = _bbox(run)
    return listed


def write_index(runs_dir: str):
    """Rewrite runs.js (used by the maps) and index.html from the saved runs."""
    runs = list_runs(runs_dir)
    with open(os.path.join(runs_dir, "runs.js"), "w", encoding="utf-8") as f:
        f.write("window.FINDALINE_RUNS = ")
        json.dump([_listed(r) for r in runs], f, ensure_ascii=False)
        f.write(";\n")

    def e(value):
        return html.escape(str(value))

    rows = []
    for r in runs:
        links = []
        for label, file in (("map", "map.html"), ("CSV", "lines.csv"), ("GeoJSON", "lines.geojson")):
            if os.path.exists(os.path.join(runs_dir, r["id"], file)):
                links.append(f"<a href='{e(r['id'])}/{file}'>{label}</a>")
        rows.append(
            f"<tr><td>{e(r['created'][:16].replace('T', ' '))}</td><td>{e(r['name'])}</td>"
            f"<td>{r['center']['lat']:.5f}, {r['center']['lon']:.5f}</td>"
            f"<td>{e(params_summary(r))}</td>"
            f"<td class='n'>{r['summary']['lines']}</td><td class='n'>{r['summary']['spots']}</td>"
            f"<td>{' · '.join(links)}</td></tr>")

    with open(os.path.join(runs_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Find-a-Line runs</title>
<style>
  body {{ font: 14px/1.5 sans-serif; margin: 24px; color: #222; }}
  table {{ border-collapse: collapse; }}
  th, td {{ padding: 4px 10px; border-bottom: 1px solid #ddd; text-align: left; vertical-align: top; }}
  th {{ background: #f4f4f4; }}
  td.n {{ text-align: right; }}
  tr:hover td {{ background: #fafafa; }}
</style></head>
<body>
<h1>Find-a-Line runs</h1>
<p>{len(runs)} runs, newest first. Spots are groups of similar lines.</p>
<table>
<tr><th>Date</th><th>Place</th><th>Centre (lat, lon)</th><th>Parameters</th>
<th>Lines</th><th>Spots</th><th>Files</th></tr>
{chr(10).join(rows)}
</table>
</body></html>
""")


def write_latest_redirect(path: str, map_path: str):
    """A page that opens the map of the latest run."""
    target = os.path.relpath(map_path, os.path.dirname(os.path.abspath(path))).replace(os.sep, "/")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Find-a-Line - latest run</title>
<meta http-equiv="refresh" content="0; url={html.escape(target)}"></head>
<body><a href="{html.escape(target)}">Open the latest run</a></body></html>
""")
