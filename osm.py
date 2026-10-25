"""
OpenStreetMap data kept in the cache (cache/osm, see cache.py).

- Themes: features of one kind on a fixed grid of 0.25° tiles, stored as GeoJSON
  (cache/osm/themes/<theme>/<tile>.geojson), so nearby searches share tiles and
  large areas are just more tiles:
    coastline  natural=coastline ways (the sea mask, coast.py)
    cliff      natural=cliff ways and climbing=crag features (search areas)
  A tile comes from the Overpass API, or from the downloaded Geofabrik extracts
  (osmextract.py) when they cover it whole; such tiles are rebuilt when the extracts
  are updated.
- Nominatim answers (cache/osm/nominatim.sqlite): place names and countries, so the
  same point is not asked twice (Nominatim allows one request per second).
"""

import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from shapely.geometry import LineString, MultiLineString, Point, shape

import cache

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Tried in turn when a server is overloaded (502, 504, timeouts); both serve the whole planet
OVERPASS_URLS = [OVERPASS_URL, "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
HEADERS = {"User-Agent": "Find-a-Line/1.0 (highline finder)"}
TILE_DEG = 0.25
# What each theme holds: (OSM type, key, value); the Overpass queries and the
# extracts (osmextract.py) use the same list
THEME_TAGS = {
    "coastline": [("way", "natural", "coastline")],
    "cliff": [("way", "natural", "cliff"), ("node", "climbing", "crag"), ("way", "climbing", "crag"),
              ("relation", "climbing", "crag")],
}
THEMES = {name: "".join(f'{kind}["{key}"="{value}"]({{bbox}});' for kind, key, value in tags)
          for name, tags in THEME_TAGS.items()}


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


def theme_path(cache_dir: str, theme: str, lat: float, lon: float) -> str:
    return os.path.join(cache_dir, "osm", "themes", theme, f"{tile_name(lat, lon)}.geojson")


def write_tile(path: str, features: List[Dict]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    os.replace(tmp, path)


def fetch_tile(theme: str, lat: float, lon: float, cache_dir: str):
    """Build one theme tile: from the extracts when they cover it, else from Overpass."""
    import osmextract
    if osmextract.build_tile(cache_dir, theme, lat, lon):
        return
    path = theme_path(cache_dir, theme, lat, lon)
    bbox = f"{lat},{lon},{lat + TILE_DEG},{lon + TILE_DEG}"
    query = f"[out:json][timeout:90];({THEMES[theme].format(bbox=bbox)});out geom;"
    error = None
    for url in OVERPASS_URLS:
        try:
            response = requests.get(url, params={"data": query}, headers=HEADERS, timeout=120)
            response.raise_for_status()
            break
        except requests.RequestException as e:
            error = e
    else:
        raise error
    features = []
    for element in response.json().get("elements", []):
        geometry = _geometry(element)
        if geometry is not None:
            features.append({"type": "Feature", "geometry": geometry.__geo_interface__,
                             "properties": {"id": f"{element['type']}/{element['id']}",
                                            "tags": element.get("tags", {})}})
    write_tile(path, features)
    cache.record(cache_dir, path, url=response.url.split("?")[0])


def from_extract(row: Optional[Dict]) -> bool:
    """Whether an index row is a theme tile built from extracts (updated with them)."""
    return bool(row and (row.get("url") or "").startswith("geofabrik:"))


def theme(name: str, west: float, south: float, east: float, north: float, cache_dir: str,
          update: Optional[str] = None) -> List[Dict]:
    """Features of a theme over a WGS84 box (each once, with "geometry" as a shapely shape).

    Missing tiles (and with update, stale ones from Overpass) are built; requests
    errors are passed on.
    """
    found: Dict[str, Dict] = {}
    for lat, lon in tiles(west, south, east, north):
        path = theme_path(cache_dir, name, lat, lon)
        row = cache.info(cache_dir, path)
        if cache.due(cache_dir, path, update=None if from_extract(row) else update,
                     ttl=cache.TTL_DAYS["osm-theme"]):
            fetch_tile(name, lat, lon, cache_dir)
        with open(path, encoding="utf-8") as f:
            for feature in json.load(f)["features"]:
                found.setdefault(feature["properties"]["id"],
                                 {**feature["properties"], "geometry": shape(feature["geometry"])})
    return list(found.values())


SEARCH_URL = "https://nominatim.openstreetmap.org/search"


def search(name: str, cache_dir: str) -> Dict:
    """The best Nominatim match for a name, with its outline ("geojson"); {} if none. Cached."""
    path = os.path.join(cache_dir, "osm", "nominatim.sqlite")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sqlite3.connect(path, timeout=60) as db:
        db.execute("CREATE TABLE IF NOT EXISTS search (query TEXT PRIMARY KEY, answer TEXT, fetched_at TEXT)")
        row = db.execute("SELECT answer FROM search WHERE query = ?", (name,)).fetchone()
    db.close()
    if row:
        return json.loads(row[0])
    response = requests.get(SEARCH_URL, headers=HEADERS, timeout=30,
                            params={"q": name, "format": "jsonv2", "polygon_geojson": 1, "limit": 5})
    response.raise_for_status()
    time.sleep(1)  # Nominatim usage policy: at most one request per second
    # The first match with an area (a peak or a bus stop is a point)
    answer = next((r for r in response.json()
                   if r.get("geojson", {}).get("type") in ("Polygon", "MultiPolygon")), {})
    with sqlite3.connect(path, timeout=60) as db:
        db.execute("INSERT OR REPLACE INTO search VALUES (?, ?, ?)",
                   (name, json.dumps(answer), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    db.close()
    return answer


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
