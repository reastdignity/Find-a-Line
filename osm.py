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
  same point is not asked twice.

The public servers ask for moderation, and every process of the app (the CLI, the web
worker, its phase processes) shares them: Nominatim gets at most one request per second
through a rate limiter kept in cache/osm/nominatim-rate.sqlite (pyrate-limiter), Overpass
one query at a time through a lock file, cache/osm/overpass.lock (filelock).
"""

import contextlib
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests
from shapely.geometry import LineString, MultiLineString, Point, shape

import cache

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Tried in turn when a server is overloaded (502, 504, timeouts); both serve the whole planet
OVERPASS_URLS = [OVERPASS_URL, "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
OVERPASS_WAIT_S = 600  # how long a query waits for the one of another process (each server takes up to 120 s)
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
    with cache.replacing(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)


@contextlib.contextmanager
def _overpass_turn(cache_dir: str):
    """One Overpass query at a time across all processes of this data folder."""
    from filelock import FileLock, Timeout
    path = os.path.join(cache_dir, "osm", "overpass.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = FileLock(path)
    try:
        lock.acquire(timeout=OVERPASS_WAIT_S)
    except Timeout:
        raise requests.Timeout(f"Overpass is busy with the queries of another search (waited {OVERPASS_WAIT_S // 60} "
                               "min); try again later, or download the OpenStreetMap extract of the country "
                               "(findaline.py osm download)") from None
    try:
        yield
    finally:
        lock.release()


def fetch_tile(theme: str, lat: float, lon: float, cache_dir: str):
    """Build one theme tile: from the extracts when they cover it, else from Overpass."""
    import osmextract
    if osmextract.build_tile(cache_dir, theme, lat, lon):
        return
    path = theme_path(cache_dir, theme, lat, lon)
    bbox = f"{lat},{lon},{lat + TILE_DEG},{lon + TILE_DEG}"
    query = f"[out:json][timeout:90];({THEMES[theme].format(bbox=bbox)});out geom;"
    error = None
    with _overpass_turn(cache_dir):
        for url in OVERPASS_URLS:
            try:
                response = requests.get(url, params={"data": query}, headers=cache.http_headers(), timeout=120)
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
FAILED_DAYS = 30  # a search without a match (or a reverse lookup without an answer) is asked again after it
# Answers larger than this (an outline of a country) are stored with their outline simplified
MAX_ANSWER_BYTES = 5_000_000
# Nominatim's usage policy allows one request per second; the limiter admits one per 1.1 s,
# a margin for the way from the permit to the server
NOMINATIM_INTERVAL_MS = 1100
NOMINATIM_WAIT_S = 300  # how long a request waits for its turn
_limiters: Dict[str, object] = {}  # pyrate_limiter.Limiter per data folder, made once per process
# Called before every request to Nominatim (not for cached answers); the web worker sets it per job
# or quick task to count the requests of its user (jobs.nominatim_quota), None elsewhere
BEFORE_REQUEST: Optional[Callable[[], None]] = None
# The schema of nominatim.sqlite, one step per version (cache.migrate; never change a released step).
# Version 1 is the schema the database had before it had versions, so an old one keeps its rows.
MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS search (query TEXT PRIMARY KEY, answer TEXT, fetched_at TEXT);
    CREATE TABLE IF NOT EXISTS reverse (query TEXT PRIMARY KEY, answer TEXT, fetched_at TEXT);""",
]


def _nominatim_db(cache_dir: str) -> sqlite3.Connection:
    path = os.path.join(cache_dir, "osm", "nominatim.sqlite")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, timeout=60)
    cache.migrate(db, MIGRATIONS)
    return db


def _failed(answer: Dict) -> bool:
    """Whether a stored answer found nothing: no match for a name, an error for a point."""
    return not answer or "error" in answer


def _cached(cache_dir: str, table: str, key: str) -> Optional[Dict]:
    """The stored answer to a query; None if there is none or it is a failure older than FAILED_DAYS.

    A row without a time (none is known, but the column allows it) counts as a lasting answer.
    """
    db = _nominatim_db(cache_dir)
    try:
        row = db.execute(f"SELECT answer, fetched_at FROM {table} WHERE query = ?", (key,)).fetchone()
    finally:
        db.close()
    if not row:
        return None
    answer = json.loads(row[0])
    if _failed(answer) and row[1]:
        if datetime.now(timezone.utc) - datetime.fromisoformat(row[1]) > timedelta(days=FAILED_DAYS):
            return None
    return answer


def _fit(answer: Dict) -> Optional[str]:
    """The answer as JSON of at most MAX_ANSWER_BYTES: its outline simplified (from about a metre,
    doubling the tolerance) until it fits; None if it cannot (then it is not stored)."""
    text = json.dumps(answer)
    tolerance = 1e-5  # degrees
    while len(text.encode("utf-8")) > MAX_ANSWER_BYTES:
        if not isinstance(answer.get("geojson"), dict) or tolerance > 0.1:
            return None
        outline = shape(answer["geojson"]).simplify(tolerance, preserve_topology=True)
        text = json.dumps({**answer, "geojson": outline.__geo_interface__})
        tolerance *= 2
    return text


def _store(cache_dir: str, table: str, key: str, answer: Dict) -> Dict:
    """Store an answer; returns it as stored (a simplified outline, see _fit), so that the
    first search and the later ones from the cache see the same."""
    text = _fit(answer)
    if text is None:
        return answer
    db = _nominatim_db(cache_dir)
    try:
        with db:
            db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?)", (key, text, cache._now()))
    finally:
        db.close()
    return json.loads(text)


def _nominatim_turn(cache_dir: str):
    """Wait until this process may send a request to Nominatim (all processes of the data folder
    share one limit: a pyrate-limiter SQLite bucket, its writes serialised by a file lock)."""
    path = os.path.join(cache_dir, "osm", "nominatim-rate.sqlite")
    limiter = _limiters.get(path)
    if limiter is None:
        from pyrate_limiter import Limiter, Rate, SQLiteBucket
        os.makedirs(os.path.dirname(path), exist_ok=True)
        bucket = SQLiteBucket.init_from_file([Rate(1, NOMINATIM_INTERVAL_MS)], table="nominatim", db_path=path,
                                             use_file_lock=True)
        limiter = _limiters[path] = Limiter(bucket)
    if not limiter.try_acquire("nominatim", timeout=NOMINATIM_WAIT_S):
        raise requests.Timeout(f"Nominatim is busy with the requests of other searches (waited "
                               f"{NOMINATIM_WAIT_S // 60} min); try again later")


def _ask(url: str, params: Dict, timeout: int, cache_dir: str):
    """One request to Nominatim, in its turn (_nominatim_turn); its JSON. BEFORE_REQUEST, when set,
    is called first (it raises to refuse the request: a user's quota)."""
    if BEFORE_REQUEST is not None:
        BEFORE_REQUEST()
    _nominatim_turn(cache_dir)
    response = requests.get(url, headers=cache.http_headers(), timeout=timeout, params=params)
    response.raise_for_status()
    return response.json()


def search(name: str, cache_dir: str) -> Dict:
    """The best Nominatim match for a name, with its outline ("geojson"); {} if none. Cached."""
    answer = _cached(cache_dir, "search", name)
    if answer is not None:
        return answer
    found = _ask(SEARCH_URL, {"q": name, "format": "jsonv2", "polygon_geojson": 1, "limit": 5}, 30, cache_dir)
    # The first match with an area (a peak or a bus stop is a point)
    answer = next((r for r in found if r.get("geojson", {}).get("type") in ("Polygon", "MultiPolygon")), {})
    return _store(cache_dir, "search", name, answer)


def reverse(lat: float, lon: float, cache_dir: str, **params) -> Dict:
    """Nominatim reverse geocoding, cached per point (to 1e-5°) and parameters."""
    query = {"lat": round(lat, 5), "lon": round(lon, 5), "format": "jsonv2", **params}
    key = json.dumps(query, sort_keys=True)
    answer = _cached(cache_dir, "reverse", key)
    if answer is not None:
        return answer
    return _store(cache_dir, "reverse", key, _ask(NOMINATIM_URL, query, 15, cache_dir))
