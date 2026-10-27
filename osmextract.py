"""
OpenStreetMap extracts from Geofabrik (`findaline.py osm download <region>`).

An extract is the whole OSM data of a country or a region (.osm.pbf), kept in
cache/<country>/osm-extract/ (deleting the country folder removes it too):
    <region>.osm.pbf        the extract
    <region>.geojson        its outline (from the Geofabrik index)
    <region>.themes.json    the features of the themes (osm.THEME_TAGS) taken from it
`findaline.py osm update` (and `cache update`, after cache.TTL_DAYS["osm-extract"])
applies the diffs of Geofabrik's replication service with pyosmium, so only the
changes are downloaded.

A theme tile (osm.py) is built from the extracts when the part of the tile outside
their outlines is open sea: it lies in no other country of the Geofabrik index. A
tile on a border needs the extracts of both countries, otherwise it comes from
Overpass. After a download or an update every cached theme tile the extracts cover
is built again.
"""

import glob
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Tuple

import requests
from shapely.geometry import LineString, MultiLineString, Point, box, shape
from shapely.ops import unary_union

import cache
import osm
from progress import progress

INDEX_URL = "https://download.geofabrik.de/index-v1.json"
HEADERS = {"User-Agent": "Find-a-Line/1.0 (highline finder)"}
DIFF_BATCH_KB = 100 * 1024  # diffs kept in memory at a time while updating


def _index(cache_dir: str, refresh: bool = False) -> Dict[str, Dict]:
    """The Geofabrik regions by id (cached in cache/osm/geofabrik-index.json)."""
    path = os.path.join(cache_dir, "osm", "geofabrik-index.json")
    if refresh or not os.path.exists(path):
        response = requests.get(INDEX_URL, headers=HEADERS, timeout=120)
        response.raise_for_status()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "wb") as f:
            f.write(response.content)
        os.replace(path + ".part", path)
        cache.record(cache_dir, path, url=INDEX_URL, last_modified=response.headers.get("Last-Modified"))
    with open(path, encoding="utf-8") as f:
        return {feature["properties"]["id"]: feature for feature in json.load(f)["features"]}


def region(cache_dir: str, name: str) -> Dict:
    """The Geofabrik region with this id or name (e.g. "poland", "malta", "malopolskie")."""
    for refresh in (False, True):
        regions = _index(cache_dir, refresh)
        for rid, feature in regions.items():
            if name.lower() in (rid, feature["properties"]["name"].lower()):
                return feature
    similar = sorted(rid for rid in regions if name.lower()[:4] in rid)
    raise ValueError(f"no Geofabrik region \"{name}\"" + (f" (similar: {', '.join(similar[:10])})" if similar else "")
                     + "; see https://download.geofabrik.de")


def _country(regions: Dict[str, Dict], rid: str) -> str:
    """The country a region belongs to (the region below its continent), for the cache folder."""
    while True:
        parent = regions.get(regions[rid]["properties"].get("parent", ""))
        if parent is None or "parent" not in parent["properties"]:
            return rid
        rid = parent["properties"]["id"]


def _paths(cache_dir: str, folder: str, rid: str) -> Tuple[str, str, str]:
    base = os.path.join(cache_dir, folder, "osm-extract", rid)
    return f"{base}.osm.pbf", f"{base}.geojson", f"{base}.themes.json"


def extracts(cache_dir: str) -> List[Tuple[str, str]]:
    """(region id, path of the .osm.pbf) of the downloaded extracts."""
    found = glob.glob(os.path.join(cache_dir, "*", "osm-extract", "*.osm.pbf"))
    return sorted((os.path.basename(p)[:-len(".osm.pbf")], p) for p in found)


_loaded: Dict[str, tuple] = {}


def _load(path: str):
    """A JSON file, kept in memory until it changes."""
    mtime = os.path.getmtime(path)
    if path not in _loaded or _loaded[path][0] != mtime:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if path.endswith(".geojson"):
            data = shape(data)
        _loaded[path] = (mtime, data)
    return _loaded[path][1]


def _countries(cache_dir: str) -> List[Tuple[str, object]]:
    """(id, outline) of the countries of the Geofabrik index (the regions below a continent)."""
    path = os.path.join(cache_dir, "osm", "geofabrik-index.json")
    if path not in _loaded or _loaded[path][0] != os.path.getmtime(path):
        regions = _index(cache_dir)
        countries = [(rid, shape(f["geometry"])) for rid, f in regions.items()
                     if f["geometry"] and _country(regions, rid) == rid and "parent" in f["properties"]]
        _loaded[path] = (os.path.getmtime(path), countries)
    return _loaded[path][1]


def _covered(cache_dir: str, tile, used: List[tuple]) -> bool:
    """Whether the extracts give everything in the tile: what their outlines leave out lies
    in no other country (open sea)."""
    rest = tile.difference(unary_union([u[1] for u in used]))
    if rest.is_empty:
        return True
    regions = _index(cache_dir)
    mine = {_country(regions, u[0]) for u in used if u[0] in regions}
    return not any(rid not in mine and outline.intersects(rest) for rid, outline in _countries(cache_dir))


def build_tile(cache_dir: str, theme: str, lat: float, lon: float) -> bool:
    """Build a theme tile from the extracts if they give all of it; whether they did."""
    tile = box(lon, lat, lon + osm.TILE_DEG, lat + osm.TILE_DEG)
    used = []
    for rid, pbf in extracts(cache_dir):
        outline, themes = pbf[:-len(".osm.pbf")] + ".geojson", pbf[:-len(".osm.pbf")] + ".themes.json"
        if os.path.exists(outline) and os.path.exists(themes) and _load(outline).intersects(tile):
            used.append((rid, _load(outline), _load(themes)))
    if not used or not _covered(cache_dir, tile, used):
        return False
    features: Dict[str, Dict] = {}
    for _, _, data in used:
        for feature in data["themes"][theme]:
            if feature["properties"]["id"] not in features and shape(feature["geometry"]).intersects(tile):
                features[feature["properties"]["id"]] = feature
    path = osm.theme_path(cache_dir, theme, lat, lon)
    osm.write_tile(path, list(features.values()))
    cache.record(cache_dir, path, url="geofabrik:" + "+".join(u[0] for u in used),
                 source_date=min(u[2]["timestamp"] for u in used))
    return True


def _take_features(pbf: str, out: str, timestamp: str):
    """Save the features of every theme in an extract (three passes over the file:
    the tagged objects, the ways of relations, the nodes of all ways)."""
    import osmium
    wanted = {(kind, key, value): name for name, tags in osm.THEME_TAGS.items() for kind, key, value in tags}
    items, way_refs = [], {}
    progress.compute.start("OSM extract: themes", 3, unit="pass")
    tag_filter = osmium.filter.TagFilter(*{(key, value) for _, key, value in wanted})
    for obj in osmium.FileProcessor(pbf).with_filter(tag_filter):
        kind = "node" if obj.is_node() else "way" if obj.is_way() else "relation"
        themes = sorted({name for (k, key, value), name in wanted.items() if k == kind and obj.tags.get(key) == value})
        if not themes:
            continue
        item = {"id": f"{kind}/{obj.id}", "tags": dict(obj.tags), "themes": themes}
        if kind == "node":
            if not obj.location.valid():
                continue
            item["point"] = (obj.location.lon, obj.location.lat)
        elif kind == "way":
            way_refs[obj.id] = [n.ref for n in obj.nodes]
        else:
            item["ways"] = [m.ref for m in obj.members if m.type == "w"]
        items.append(item)
    progress.compute.update(1)
    members = {ref for item in items for ref in item.get("ways", ())} - set(way_refs)
    if members:
        for way in osmium.FileProcessor(pbf, osmium.osm.WAY).with_filter(osmium.filter.IdFilter(members)):
            way_refs[way.id] = [n.ref for n in way.nodes]
    progress.compute.update(1)
    nodes = {ref for refs in way_refs.values() for ref in refs}
    location = {}
    for node in osmium.FileProcessor(pbf, osmium.osm.NODE).with_filter(osmium.filter.IdFilter(nodes)):
        if node.location.valid():
            location[node.id] = (node.location.lon, node.location.lat)
    progress.compute.update(1)

    def line(way_id):
        points = [location[ref] for ref in way_refs.get(way_id, ()) if ref in location]
        return points if len(points) > 1 else None

    themes: Dict[str, List[Dict]] = {name: [] for name in osm.THEME_TAGS}
    for item in items:
        kind, number = item["id"].split("/")
        if kind == "node":
            geometry = Point(item["point"])
        elif kind == "way":
            points = line(int(number))
            geometry = LineString(points) if points else None
        else:  # like Overpass "out geom": the member ways as lines
            parts = [p for p in map(line, item["ways"]) if p]
            geometry = MultiLineString(parts) if parts else None
        if geometry is None:
            continue
        feature = {"type": "Feature", "geometry": geometry.__geo_interface__,
                   "properties": {"id": item["id"], "tags": item["tags"]}}
        for name in item["themes"]:
            themes[name].append(feature)
    progress.compute.finish(", ".join(f"{name} {len(found)}" for name, found in themes.items()))
    with open(out + ".part", "w", encoding="utf-8") as f:
        json.dump({"timestamp": timestamp, "themes": themes}, f)
    os.replace(out + ".part", out)


def _rebuild_tiles(cache_dir: str) -> int:
    """Build again every cached theme tile the extracts cover; how many."""
    built = 0
    for row in cache.rows(cache_dir, "osm"):
        parts = row["path"].split("/")
        if len(parts) == 4 and parts[1] == "themes" and parts[3].endswith(".geojson"):
            lat, lon = _tile_corner(parts[3][:-len(".geojson")])
            built += build_tile(cache_dir, parts[2], lat, lon)
    return built


def offered(feature: Dict) -> bool:
    """Whether a Geofabrik region is offered in the app: countries and smaller regions,
    not continents (no parent or no outline; their extracts are tens of GB)."""
    return "parent" in feature["properties"] and bool(feature["geometry"])


def regions_at(cache_dir: str, lat: float, lon: float) -> List[Dict]:
    """The Geofabrik regions containing a point (a country, then its smaller regions)."""
    import re
    from shapely.geometry import Point
    regions = _index(cache_dir)
    downloaded = {rid for rid, _ in extracts(cache_dir)}
    point, found = Point(lon, lat), []
    for rid, feature in regions.items():
        if not offered(feature):
            continue  # continents
        if shape(feature["geometry"]).contains(point):
            depth, parent = 0, rid
            while regions.get(parent, {}).get("properties", {}).get("parent") in regions:
                depth, parent = depth + 1, regions[parent]["properties"]["parent"]
            name = re.sub(r"<br\s*/?>.*", "", feature["properties"]["name"])
            found.append({"id": rid, "name": name, "depth": depth, "downloaded": rid in downloaded})
    return sorted(found, key=lambda r: r["depth"])


def downloaded(cache_dir: str) -> List[Dict]:
    """The downloaded extracts: id, size, date of the data, last check."""
    found = []
    for rid, pbf in extracts(cache_dir):
        row = cache.info(cache_dir, pbf) or {}
        found.append({"id": rid, "mb": round(os.path.getsize(pbf) / 1e6), "data": row.get("source_date"),
                      "checked": row.get("checked_at")})
    return found


def _tile_corner(name: str) -> Tuple[float, float]:
    """(lat, lon) of a tile name such as N50.00_E019.75 (osm.tile_name)."""
    lat, lon = name.split("_")
    return (float(lat[1:]) * (1 if lat[0] == "N" else -1), float(lon[1:]) * (1 if lon[0] == "E" else -1))


def _finish(cache_dir: str, rid: str, pbf: str, themes: str, url: str):
    """Record the extract, take its theme features and rebuild the tiles."""
    from osmium.replication.utils import get_replication_header
    header = get_replication_header(pbf)
    timestamp = header.timestamp.isoformat() if header.timestamp else datetime.now(timezone.utc).isoformat()
    cache.record(cache_dir, pbf, url=url, source_date=timestamp)
    _take_features(pbf, themes, timestamp)
    cache.record(cache_dir, themes, url=url, source_date=timestamp)
    tiles = _rebuild_tiles(cache_dir)
    print(f"OSM extract {rid}: data of {timestamp[:16].replace('T', ' ')} UTC; {tiles} cached theme tiles rebuilt")


def download(cache_dir: str, name: str):
    """Download the extract of a Geofabrik region (again, if it is there already)."""
    feature = region(cache_dir, name)
    rid = feature["properties"]["id"]
    regions = _index(cache_dir)
    pbf, outline, themes = _paths(cache_dir, _country(regions, rid), rid)
    url = feature["properties"]["urls"]["pbf"]
    os.makedirs(os.path.dirname(pbf), exist_ok=True)
    with requests.get(url, stream=True, headers=HEADERS, timeout=120) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        progress.download.start(f"OSM extract {rid}", total / 1e6, unit="MB")
        with open(pbf + ".part", "wb") as f:
            for chunk in response.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                progress.download.update(len(chunk) / 1e6)
        progress.download.finish()
    os.replace(pbf + ".part", pbf)
    with open(outline, "w", encoding="utf-8") as f:
        json.dump(feature["geometry"], f)
    cache.record(cache_dir, outline, url=INDEX_URL)
    _finish(cache_dir, rid, pbf, themes, url)


def update(cache_dir: str, rid: str, pbf: str) -> bool:
    """Apply the replication diffs to an extract; whether there was anything new.

    When the diffs cannot be applied (too old for the server), the extract is
    downloaded again.
    """
    from osmium.replication.server import ReplicationServer
    from osmium.replication.utils import get_replication_header
    header = get_replication_header(pbf)
    themes = pbf[:-len(".osm.pbf")] + ".themes.json"
    if not header.url or header.sequence is None:
        print(f"OSM extract {rid}: no replication information, downloading it again")
        download(cache_dir, rid)
        return True
    server = ReplicationServer(header.url)
    newest = server.get_state_info()
    if newest is None:
        raise RuntimeError(f"the replication service {header.url} does not answer")
    if newest.sequence <= header.sequence:
        cache.checked(cache_dir, [pbf])
        print(f"OSM extract {rid}: up to date ({header.timestamp:%Y-%m-%d %H:%M} UTC)")
        return False
    progress.download.start(f"OSM extract {rid}: diffs", newest.sequence - header.sequence, unit="diff")
    sequence = header.sequence
    while sequence < newest.sequence:
        out = pbf + ".new.osm.pbf"
        if os.path.exists(out):
            os.remove(out)
        result = server.apply_diffs_to_file(pbf, out, sequence + 1, max_size=DIFF_BATCH_KB)
        if result is None:
            progress.download.finish("failed")
            print(f"OSM extract {rid}: the diffs could not be applied, downloading it again")
            download(cache_dir, rid)
            return True
        os.replace(out, pbf)
        progress.download.update(result[0] - sequence)
        sequence = result[0]
    progress.download.finish()
    _finish(cache_dir, rid, pbf, themes, (cache.info(cache_dir, pbf) or {}).get("url") or header.url)
    return True
