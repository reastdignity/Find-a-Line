"""
OpenStreetMap data kept in the cache (cache/osm, see cache.py).

- Themes: features of one kind on a fixed grid of 0.25° tiles, fetched from the
  Overpass API and stored as GeoJSON (cache/osm/themes/<theme>/<tile>.geojson), so
  nearby searches share tiles and large areas are just more tiles:
    coastline  natural=coastline ways (the sea mask, coast.py)
    cliff      natural=cliff ways and climbing=crag features (search areas, later)
- Nominatim answers (cache/osm/nominatim.sqlite): place names and countries, so the
  same point is not asked twice (Nominatim allows one request per second).
"""

import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Tuple

import requests
from shapely.geometry import LineString, MultiLineString, Point, shape

import cache

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
HEADERS = {"User-Agent": "Find-a-Line/1.0 (highline finder)"}
TILE_DEG = 0.25
THEMES = {
    "coastline": 'way["natural"="coastline"]({bbox});',
    "cliff": 'way["natural"="cliff"]({bbox});nwr["climbing"="crag"]({bbox});',
}


def tiles(west: float, south: float, east: float, north: float) -> List[Tuple[float, float]]:
    """South-west corners of the 0.25° tiles covering a WGS84 box."""
    def steps(a, b):
        return range(math.floor(a / TILE_DEG), math.floor(b / TILE_DEG) + 1)
    return [(i * TILE_DEG, j * TILE_DEG) for i in steps(south, north) for j in steps(west, east)]


def tile_name(lat: float, lon: float) -> str:
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):05.2f}_{'E' if lon >= 0 else 'W'}{abs(lon):06.2f}"


def _geometry(element: Dict):
    if element["type"] == "node":
        return Point(element["lon"], element["lat"])
    if element["type"] == "way" and len(element.get("geometry", [])) > 1:
        return LineString([(p["lon"], p["lat"]) for p in element["geometry"]])
    if element["type"] == "relation":
        parts = [[(p["lon"], p["lat"]) for p in m["geometry"]] for m in element.get("members", [])
                 if len(m.get("geometry") or []) > 1]
        return MultiLineString(parts) if parts else None
    return None


def _fetch_tile(theme: str, lat: float, lon: float, path: str, cache_dir: str):
    bbox = f"{lat},{lon},{lat + TILE_DEG},{lon + TILE_DEG}"
    query = f"[out:json][timeout:90];({THEMES[theme].format(bbox=bbox)});out geom;"
    response = requests.get(OVERPASS_URL, params={"data": query}, headers=HEADERS, timeout=120)
    response.raise_for_status()
    features = []
    for element in response.json().get("elements", []):
        geometry = _geometry(element)
        if geometry is not None:
            features.append({"type": "Feature", "geometry": geometry.__geo_interface__,
                             "properties": {"id": f"{element['type']}/{element['id']}",
                                            "tags": element.get("tags", {})}})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    os.replace(tmp, path)
    cache.record(cache_dir, path, url=OVERPASS_URL)


def theme(name: str, west: float, south: float, east: float, north: float, cache_dir: str,
          refresh: bool = False) -> List[Dict]:
    """Features of a theme over a WGS84 box (each once, with "geometry" as a shapely shape).

    Missing tiles are fetched from Overpass; requests errors are passed on.
    """
    found: Dict[str, Dict] = {}
    for lat, lon in tiles(west, south, east, north):
        path = os.path.join(cache_dir, "osm", "themes", name, f"{tile_name(lat, lon)}.geojson")
        if refresh or cache.state(cache_dir, path) is None:
            _fetch_tile(name, lat, lon, path, cache_dir)
        with open(path, encoding="utf-8") as f:
            for feature in json.load(f)["features"]:
                found.setdefault(feature["properties"]["id"],
                                 {**feature["properties"], "geometry": shape(feature["geometry"])})
    return list(found.values())


def reverse(lat: float, lon: float, cache_dir: str, **params) -> Dict:
    """Nominatim reverse geocoding, cached per point (to 1e-5°) and parameters."""
    query = {"lat": round(lat, 5), "lon": round(lon, 5), "format": "jsonv2", **params}
    key = json.dumps(query, sort_keys=True)
    path = os.path.join(cache_dir, "osm", "nominatim.sqlite")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sqlite3.connect(path, timeout=60) as db:
        db.execute("CREATE TABLE IF NOT EXISTS reverse (query TEXT PRIMARY KEY, answer TEXT, fetched_at TEXT)")
        row = db.execute("SELECT answer FROM reverse WHERE query = ?", (key,)).fetchone()
    db.close()
    if row:
        return json.loads(row[0])
    response = requests.get(NOMINATIM_URL, headers=HEADERS, timeout=15, params=query)
    response.raise_for_status()
    answer = response.json()
    time.sleep(1)  # Nominatim usage policy: at most one request per second
    with sqlite3.connect(path, timeout=60) as db:
        db.execute("INSERT OR REPLACE INTO reverse VALUES (?, ?, ?)",
                   (key, json.dumps(answer), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    db.close()
    return answer
