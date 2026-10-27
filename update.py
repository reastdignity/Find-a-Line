"""
Cache commands of findaline.py: what is cached, and updating it.

    python findaline.py cache status [FOLDER]
    python findaline.py cache update [FOLDER] [--all] [--bbox S,W,N,E | --point LAT,LON]
    python findaline.py osm download REGION
    python findaline.py osm update [REGION]

Nothing is updated during a search unless it is asked for (--update stale|all of a
search checks the data of its area). How each kind of data is checked:

    never        versioned releases (Copernicus 2021, FABDEM v1-2, NASADEM, Malta 2012/2018)
    after TTL    tiles of services that serve the current state (IGN Spain, the orthophoto
                 WMS of geoportal.gov.pl and IGN) and OSM theme tiles from Overpass: downloaded
                 again; the same content only changes the check time
    sheet index  geoportal.gov.pl: the index is asked again; newer surveys of the cached sheets
                 are downloaded and the older files deleted
    newer file   OSM water polygons of the world (regenerated daily: checked after a year,
                 downloaded again when the server's file is newer than the folder's date)
    diffs        Geofabrik OSM extracts (osmextract.py); theme tiles built from them follow

The intervals are cache.TTL_DAYS; "missing" tiles of data that can change are checked
again after cache.MISSING_TTL_DAYS. `update` without --all checks only what is older
than its interval (stale); --all checks everything that can change.
"""

import argparse
import glob
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests

import cache
import coast
import orthophoto
import osm
import osmextract
import sources as src
from progress import progress

THREADS = 4


@dataclass
class Check:
    """One file (or set of files) an update would check."""
    what: str                     # kind of data, for the listing
    path: str                     # in the cache, relative
    extent: Optional[Tuple[float, float, float, float]]  # (west, south, east, north); None: everywhere
    action: Callable[[], object]  # True: changed (otherwise a new fetch time of the file tells)
    parallel: bool = True


@lru_cache(maxsize=None)
def _live_layers() -> List[src.Layer]:
    layers = {id(layer): layer for s in src.SOURCES.values() for layer in (s.nmt, s.nmpt)
              if layer is not None and layer.ttl_days is not None}
    return list(layers.values())


@lru_cache(maxsize=None)
def _sheet_layers() -> List[src.IndexedTiles]:
    layers = {id(layer): layer for s in src.SOURCES.values() for layer in (s.nmt, s.nmpt)
              if isinstance(layer, src.IndexedTiles)}
    return list(layers.values())


@lru_cache(maxsize=None)
def _ortho_services() -> List[src.OrthoService]:
    return list({s.ortho.folder: s.ortho for s in src.SOURCES.values()
                 if s.ortho is not None and s.ortho.ttl_days is not None}.values())


def _tile_extent(name: str) -> Tuple[float, float, float, float]:
    lat, lon = osmextract._tile_corner(name)
    return lon, lat, lon + osm.TILE_DEG, lat + osm.TILE_DEG


def _check(cache_dir: str, row: Dict, update: str) -> Optional[Check]:
    """The check of an index row, or None if it is not due (or its data never changes)."""
    path, full = row["path"], os.path.join(cache_dir, row["path"])
    for layer in _live_layers():
        tile = layer.tile_of(path)
        if tile is not None:
            if not cache.stale(row, layer.ttl_days, update):
                return None
            return Check(layer.description, path, layer.bounds_wgs84(tile),
                         lambda: layer._download_and_record(tile, cache_dir))
    for layer in _sheet_layers():
        top = layer.folder.split("/")[0]
        if path.startswith(f"{top}/sheet-index/{layer.prefix}/"):
            if not cache.stale(row, cache.TTL_DAYS["sheet-index"], update):
                return None
            name = os.path.basename(path)[:-len(".json")]
            lat, lon = osmextract._tile_corner(name)
            return Check(f"{layer.description}: sheet index", path, _tile_extent(name),
                         lambda: _sheets(layer, cache_dir, lat, lon), parallel=False)
    for service in _ortho_services():
        tile = orthophoto.tile_of(path) if path.startswith(service.folder + "/wm256/") else None
        if tile is not None:
            if not cache.stale(row, service.ttl_days, update):
                return None

            def ortho(service=service, tile=tile):
                orthophoto._download_tile(service, *tile, full)
                cache.record(cache_dir, full, url=service.url or service.base_url)
            return Check(service.name, path, orthophoto.bounds_wgs84(*tile), ortho)
    parts = path.split("/")
    if parts[:2] == ["osm", "themes"] and len(parts) == 4:
        if osm.from_extract(row) or not cache.stale(row, cache.TTL_DAYS["osm-theme"], update):
            return None
        lat, lon = osmextract._tile_corner(parts[3][:-len(".geojson")])
        return Check(f"OSM {parts[2]} tiles (Overpass)", path, _tile_extent(parts[3][:-len(".geojson")]),
                     lambda: osm.fetch_tile(parts[2], lat, lon, cache_dir), parallel=False)
    if parts[:2] == ["osm", "water-polygons"] and path.endswith(".shp"):
        if not cache.stale(row, cache.TTL_DAYS["water-polygons"], update):
            return None
        return Check("OSM water polygons (world)", path, None,
                     lambda: coast.world_polygons(cache_dir, update), parallel=False)
    if len(parts) == 3 and parts[1] == "osm-extract" and path.endswith(".osm.pbf"):
        if not cache.stale(row, cache.TTL_DAYS["osm-extract"], update):
            return None
        rid = parts[2][:-len(".osm.pbf")]
        outline = full[:-len(".osm.pbf")] + ".geojson"
        extent = osmextract._load(outline).bounds if os.path.exists(outline) else None
        return Check(f"OSM extract {rid}", path, extent, lambda: osmextract.update(cache_dir, rid, full),
                     parallel=False)
    return None


def _sheets(layer: src.IndexedTiles, cache_dir: str, lat: float, lon: float) -> bool:
    new, mb, deleted = layer.update_index_tile(cache_dir, lat, lon)
    if new:
        print(f"  {osm.tile_name(lat, lon)}: {new} newer sheets ({mb:.0f} MB), {deleted} older deleted")
    return new > 0


def _tops(cache_dir: str, folder: Optional[str]) -> List[str]:
    found = sorted(d for d in os.listdir(cache_dir) if os.path.isdir(os.path.join(cache_dir, d))) \
        if os.path.isdir(cache_dir) else []
    if folder:
        if folder not in found:
            raise SystemExit(f"No folder {folder} in {cache_dir} (there are: {', '.join(found) or 'none'})")
        return [folder]
    return found


def _adopt_world(cache_dir: str):
    """Index the files of a world water polygons folder that was put there by hand."""
    shapefile = coast.world_file(cache_dir)
    if shapefile:
        for path in glob.glob(os.path.join(os.path.dirname(os.path.dirname(shapefile)), "**", "*"), recursive=True):
            if os.path.isfile(path):
                cache.state(cache_dir, path)


def checks(cache_dir: str, folder: Optional[str] = None, update: str = "stale",
           bbox: Optional[Tuple[float, float, float, float]] = None) -> List[Check]:
    """What an update would check; bbox (west, south, east, north) limits it to a place."""
    _adopt_world(cache_dir)
    found = []
    for top in _tops(cache_dir, folder):
        for row in cache.rows(cache_dir, top):
            check = _check(cache_dir, row, update)
            if check is None:
                continue
            if bbox and check.extent:
                w, s, e, n = check.extent
                if w > bbox[2] or e < bbox[0] or s > bbox[3] or n < bbox[1]:
                    continue
            found.append(check)
    return found


def run_update(cache_dir: str, folder: Optional[str] = None, update: str = "stale",
               bbox: Optional[Tuple[float, float, float, float]] = None) -> int:
    """Check and update; returns the number of failed checks (each is reported, none stops the rest)."""
    todo = checks(cache_dir, folder, update, bbox)
    if not todo:
        print("Nothing to check" + (" (everything was checked recently; --all checks it anyway)"
                                    if update == "stale" else ""))
        return 0
    groups: Dict[str, List[Check]] = {}
    for check in todo:
        groups.setdefault(check.what, []).append(check)
    failed = 0
    for what, group in groups.items():
        before = {c.path: (cache.info(cache_dir, os.path.join(cache_dir, c.path)) or {}).get("fetched_at")
                  for c in group}
        print(f"{what}: {len(group)} to check")
        errors = []
        progress.download.start(what, len(group), unit="file")

        reported = set()

        def run(check):
            try:
                if check.action() is True:
                    reported.add(check.path)
            except (RuntimeError, OSError, ValueError, requests.RequestException) as e:
                errors.append(f"{check.path}: {cache.clean_error(e)}")
            progress.download.update(1, failed=len(errors))
        if group[0].parallel:
            with ThreadPoolExecutor(max_workers=THREADS) as pool:
                for future in as_completed([pool.submit(run, c) for c in group]):
                    future.result()
        else:
            for check in group:
                run(check)
        changed = sum(p in reported or (cache.info(cache_dir, os.path.join(cache_dir, p)) or {}).get("fetched_at")
                      != fetched for p, fetched in before.items())
        progress.download.finish(f"{changed} changed")
        print(f"  {changed} of {len(group)} changed" + (f", {len(errors)} failed (e.g. {errors[0][:200]})"
                                                       if errors else ""))
        failed += len(errors)
    return failed


def _size(folder: str) -> int:
    total = 0
    for root, _, files in os.walk(folder):
        total += sum(os.path.getsize(os.path.join(root, f)) for f in files)
    return total


def _date(timestamp: Optional[str]) -> str:
    return timestamp[:10] if timestamp else "-"


def changed_runs(cache_dir: str, runs_dir: str) -> List[Tuple[str, int, int]]:
    """Runs computed on data that has changed since: (run folder, changed files, files)."""
    found = []
    for path in sorted(glob.glob(os.path.join(runs_dir, "*", "run.json"))):
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
        created = datetime.fromisoformat(meta["created"]).astimezone(timezone.utc)
        files = [p for entry in meta.get("data", []) for p in entry.get("paths", [])]
        changed = 0
        for rel in files:
            full = os.path.join(cache_dir, rel)
            row = cache.info(cache_dir, full) if os.path.exists(full) else None
            if row is None or (row["fetched_at"] and datetime.fromisoformat(row["fetched_at"]) > created):
                changed += 1  # newer data, or the file was replaced (a newer survey of a sheet)
        if changed:
            found.append((os.path.basename(os.path.dirname(path)), changed, len(files)))
    return found


def status(cache_dir: str, runs_dir: str, folder: Optional[str] = None):
    """Print the cached folders, what an update would check and the runs on changed data."""
    tops = _tops(cache_dir, folder)
    _adopt_world(cache_dir)
    print(f"Cache: {cache_dir}")
    print(f"{'Folder':<14} {'Size':>9} {'Files':>7}  {'Fetched':<23}  {'Last used':<10}  To check (stale)")
    for top in tops:
        rows = cache.rows(cache_dir, top)
        fetched = [r["fetched_at"] for r in rows if r["fetched_at"]]
        used = max((r["last_used_at"] for r in rows if r["last_used_at"]), default=None)
        due: Dict[str, int] = {}
        for row in rows:
            check = _check(cache_dir, row, "stale")
            if check:
                due[check.what] = due.get(check.what, 0) + 1
        span = f"{_date(min(fetched, default=None))} - {_date(max(fetched, default=None))}"
        print(f"{top:<14} {_size(os.path.join(cache_dir, top)) / 1e9:>7.2f}GB {len(rows):>7}  {span:<23}  "
              f"{_date(used):<10}  " + ("; ".join(f"{w}: {n}" for w, n in due.items()) or "-"))
    found = [(rid, pbf) for rid, pbf in osmextract.extracts(cache_dir)
             if not folder or cache.relative(cache_dir, pbf).startswith(folder + "/")]
    if found:
        print("\nOSM extracts:")
        for rid, pbf in found:
            row = cache.info(cache_dir, pbf) or {}
            print(f"  {rid}: {os.path.getsize(pbf) / 1e9:.2f} GB, data of {(row.get('source_date') or '?')[:16]}, "
                  f"checked {_date(row.get('checked_at'))}")
    runs = changed_runs(cache_dir, runs_dir)
    if runs:
        print("\nRuns computed on data that has changed since (run them again to use the new data):")
        for name, changed, total in runs:
            print(f"  {name}: {changed} of {total} files")
    print("\n`findaline.py cache update` checks what is stale; delete a folder to free its space "
          "(it is downloaded again when needed).")


def _box(text: str) -> Tuple[float, float, float, float]:
    """--bbox S,W,N,E or --point LAT,LON as (west, south, east, north)."""
    values = [float(v) for v in text.split(",")]
    if len(values) == 2:
        return values[1], values[0], values[1], values[0]
    south, west, north, east = values
    return west, south, east, north


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="findaline.py", description="Cache and OpenStreetMap commands.")
    parser.add_argument("--data-dir", help="data folder (default: FINDALINE_DATA or the folder of the scripts)")
    commands = parser.add_subparsers(dest="group", required=True)
    cache_cmd = commands.add_parser("cache", help="what is cached, and updating it")
    cache_sub = cache_cmd.add_subparsers(dest="command", required=True)
    st = cache_sub.add_parser("status", help="folders, sizes, what is stale, runs on changed data")
    st.add_argument("folder", nargs="?", help="one top-level folder (e.g. poland, osm)")
    up = cache_sub.add_parser("update", help="check the stale data again (--all: everything that can change)")
    up.add_argument("folder", nargs="?", help="one top-level folder (e.g. poland, osm)")
    up.add_argument("--all", action="store_true", help="check everything that can change, however recent")
    place = up.add_mutually_exclusive_group()
    place.add_argument("--bbox", help="only data over S,W,N,E (WGS84)")
    place.add_argument("--point", help="only data at LAT,LON (WGS84)")
    osm_cmd = commands.add_parser("osm", help="OpenStreetMap extracts from Geofabrik")
    osm_sub = osm_cmd.add_subparsers(dest="command", required=True)
    dl = osm_sub.add_parser("download", help="download the extract of a country or region (e.g. poland, malta); "
                                             "the theme tiles (coastline, cliffs) are then built from it")
    dl.add_argument("region", help="Geofabrik id or name, see https://download.geofabrik.de")
    ou = osm_sub.add_parser("update", help="apply the daily diffs to the downloaded extracts")
    ou.add_argument("region", nargs="?", help="only this extract")
    for sub in (st, up, dl, ou):
        sub.add_argument("--data-dir", default=argparse.SUPPRESS, help="data folder")
    args = parser.parse_args(argv)

    data = cache.data_dir(args.data_dir)
    cache_dir = os.path.join(data, "cache")
    cache.check_layout(cache_dir)
    try:
        with progress.bars():
            if args.group == "cache" and args.command == "status":
                status(cache_dir, os.path.join(data, "runs"), args.folder)
            elif args.group == "cache":
                where = args.bbox or args.point
                return 1 if run_update(cache_dir, args.folder, "all" if args.all else "stale",
                                       _box(where) if where else None) else 0
            elif args.command == "download":
                osmextract.download(cache_dir, args.region)
            else:
                found = [(rid, pbf) for rid, pbf in osmextract.extracts(cache_dir)
                         if not args.region or rid == args.region]
                if not found:
                    raise SystemExit("No such extract downloaded (`findaline.py osm download REGION`)"
                                     if args.region else "No extracts downloaded (`findaline.py osm download REGION`)")
                for rid, pbf in found:
                    osmextract.update(cache_dir, rid, pbf)
    except (ValueError, RuntimeError, requests.RequestException) as e:
        raise SystemExit(f"Failed: {cache.clean_error(e)}")
    return 0
