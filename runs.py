"""
Saving every run (map, results, parameters) so earlier runs can be browsed.

Layout:
    runs/<date>_<place>/map.html       interactive map
    runs/<date>_<place>/lines.csv      every valid line with its group
    runs/<date>_<place>/lines.geojson  the same as GeoJSON
    runs/<date>_<place>/run.json       parameters and summary
    runs/runs.js                       list of all runs, loaded by every map
    runs/index.html                    table of all runs
A page opened from disk cannot list a folder, so runs.js and index.html are
rewritten after every run.
"""

import csv
import html
import json
import os
import re
import time
import unicodedata
from datetime import datetime
from typing import Dict, List, Optional

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
HEADERS = {"User-Agent": "Find-a-Line/1.0 (highline finder)"}


def _reverse(lat: float, lon: float, **params) -> Dict:
    response = requests.get(NOMINATIM_URL, headers=HEADERS, timeout=15,
                            params={"lat": lat, "lon": lon, "format": "jsonv2",
                                    "accept-language": "pl", **params})
    response.raise_for_status()
    time.sleep(1)  # Nominatim usage policy: at most one request per second
    return response.json()


def _nearest_feature(lat: float, lon: float) -> Optional[str]:
    """Name of the nearest natural feature: rock, peak, valley, river..."""
    return _reverse(lat, lon, zoom=18, layer="natural").get("name") or None


def _locality(lat: float, lon: float) -> Optional[str]:
    """Hamlet, village or town at the point."""
    data = _reverse(lat, lon, zoom=14)
    if data.get("category") in ("place", "boundary") and data.get("name"):
        return data["name"]
    address = data.get("address", {})
    return next((address[k] for k in ("hamlet", "village", "town", "city", "municipality")
                 if k in address), None)


def place_name(lat: float, lon: float) -> Optional[str]:
    """Name of the place from OpenStreetMap, e.g. "Żabi Koń, Gaj Kobylański".

    The nearest named natural feature plus the locality; None if offline.
    """
    parts = []
    for lookup in (_nearest_feature, _locality):
        try:
            parts.append(lookup(lat, lon))
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
          "clearance_nmpt_m", "over_trees_buildings",
          "anchor1_lat", "anchor1_lon", "anchor1_elev_m", "anchor1_northing", "anchor1_easting",
          "anchor2_lat", "anchor2_lon", "anchor2_elev_m", "anchor2_northing", "anchor2_easting"]


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
                "anchor1_lat": round(r["anchor1"][0], 7), "anchor1_lon": round(r["anchor1"][1], 7),
                "anchor1_elev_m": round(r["elev_a1"], 2),
                "anchor1_northing": round(r["a1_xy"][1], 2), "anchor1_easting": round(r["a1_xy"][0], 2),
                "anchor2_lat": round(r["anchor2"][0], 7), "anchor2_lon": round(r["anchor2"][1], 7),
                "anchor2_elev_m": round(r["elev_a2"], 2),
                "anchor2_northing": round(r["a2_xy"][1], 2), "anchor2_easting": round(r["a2_xy"][0], 2),
            }


def save_results(run_path: str, groups: List[Dict], meta: Dict):
    """Write lines.csv, lines.geojson and run.json for a run."""
    rows = list(_rows(groups))
    with open(os.path.join(run_path, "lines.csv"), "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    features = [{
        "type": "Feature",
        "geometry": {"type": "LineString",
                     "coordinates": [[row["anchor1_lon"], row["anchor1_lat"]],
                                     [row["anchor2_lon"], row["anchor2_lat"]]]},
        "properties": row,
    } for row in rows]
    with open(os.path.join(run_path, "lines.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features, "findaline": meta},
                  f, ensure_ascii=False)

    with open(os.path.join(run_path, "run.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


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
                runs.append(run)
    return sorted(runs, key=lambda r: r["created"], reverse=True)


def params_summary(meta: Dict) -> str:
    p = meta["params"]
    nmpt = f"NMPT {p['nmpt']}" if p["nmpt"] else "no NMPT"
    return (f"radius {p['radius']:g} m, {p['resolution']:g} m grid, slope ≥{p['slope_threshold']:g}°, "
            f"{p['min_length']:g}–{p['max_length']:g} m, {nmpt}")


def write_index(runs_dir: str):
    """Rewrite runs.js (used by the maps) and index.html from the saved runs."""
    runs = list_runs(runs_dir)
    with open(os.path.join(runs_dir, "runs.js"), "w", encoding="utf-8") as f:
        f.write("window.FINDALINE_RUNS = ")
        json.dump(runs, f, ensure_ascii=False)
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
