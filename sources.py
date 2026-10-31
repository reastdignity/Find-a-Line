"""
Elevation data sources.

A source provides a terrain model (NMT/DTM, ground only) and optionally a surface
model (NMPT/DSM, with trees and buildings), plus optionally an orthophoto service.
Every layer is downloaded in tiles into its own folder of the cache, so switching
between sources never downloads again what is already there.

Kinds of layers:
- WcsTiles: square tiles from an OGC WCS 2.0 service on a fixed grid in the
  service's coordinate system (Malta, IGN Spain).
- IndexedTiles: files found through an index, newest survey per sheet or tile:
  SheetTiles (map sheets from a WFS sheet index, geoportal.gov.pl) and StacTiles
  (tiles of a STAC collection, swisstopo).
- GridTiles: our own tiles on a fixed grid (Slovakia, made by tools/sk_tiles.py), from
  a local mirror (FINDALINE_MIRROR) or, once uploaded, from Hugging Face.
- TnmBlocks: blocks of the big files of The National Map (USGS 3DEP, the USA), found
  through its API and read by HTTP range requests, composed newest survey first; and
  DegreeBlocks: blocks of 1° files at a fixed URL (3DEP's 1/3″ DEM).
- DegreeTiles: 1°×1° GeoTIFF tiles in WGS84 (Copernicus DEM, FABDEM) and
  OpenTopography's global DEM API (NASADEM).
- FileLayer: a local raster file (GeoTIFF, ASC...), e.g. NASADEM from OpenTopography.

To add a national source, add a Source with WcsTiles layers to SOURCES (see the
Malta entries) and its country code to `countries`; see README, "Data sources".
The layers of a search are downloaded together (download_layers: one pool for all their
tiles). Searches of one data folder may download at once: each tile under its own lock
(tile_lock; the second search waits, then finds it cached) and at most HOST_SLOTS tiles
from one host at a time across all of them (host_slot), both lock files in cache/locks.

Every source and every layer used for filling (the water mask, the gap filling
source) names its licence and the notice its data must be shown with (`Credit`):
each run records them for the layers it used (run.json, the map, ATTRIBUTION.txt).
"""

import contextlib
import glob
import hashlib
import io
import json
import math
import os
import random
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import numpy as np
import rasterio as rio
import requests
from affine import Affine
from filelock import FileLock, Timeout
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds
from rasterio.windows import Window, from_bounds

import cache
import osm
from grid import Grid, nad83_utm_crs, utm_crs
from messages import ServiceError
from progress import progress

DOWNLOAD_THREADS = 8  # tiles of one download (all its layers) at a time; each connection is slow
# Tile downloads from one host at a time across all processes of a data folder (several jobs
# of the web worker download at once), through lock files in cache/locks/hosts (host_slot);
# HOST_SLOTS_OF holds the hosts that need another number
HOST_SLOTS = 8
HOST_SLOTS_OF: Dict[str, int] = {}
TILE_LOCKS = 4096  # lock files of tiles (tile_lock): a tile's lock is one of these, by its path
LOCK_WAIT_S = 3600  # longest wait for a host slot or a tile's lock (a download in another job)
RETRIES = 3
VALID_MIN = -500  # metres; anything lower is treated as "no data"
# Finest working resolution: finer grids bring nothing for highlines (clearances are
# metres; 0.5 m LiDAR grids are partly interpolated) and cost 4x per halving
MIN_RESOLUTION = 1.0


def _get(url: str, params=None, timeout: int = 300, binary_types=("image", "octet", "tiff", "multipart")):
    """GET with retries; raises RuntimeError with the server message on failure."""
    last_error = None
    for attempt in range(RETRIES):
        try:
            response = requests.get(url, params=params, timeout=timeout, headers=cache.http_headers())
            content_type = response.headers.get("content-type", "")
            if response.status_code == 404:
                return None
            if response.status_code != 200 or not any(t in content_type for t in binary_types):
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            return response
        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            _sleep(attempt)
    raise RuntimeError(cache.clean_error(last_error))  # no API key from a query in the message


def _sleep(attempt: int):
    """Exponential backoff with jitter (swisstopo's fair use asks for it), 0.5-1.5 times 2^attempt s."""
    time.sleep(2 ** attempt * (0.5 + random.random()))


def _locks(cache_dir: str, kind: str) -> str:
    folder = os.path.join(cache_dir, cache.LOCKS, kind)
    os.makedirs(folder, exist_ok=True)
    return folder


@contextlib.contextmanager
def host_slot(cache_dir: str, url: Optional[str]):
    """One of the HOST_SLOTS (HOST_SLOTS_OF) download slots of the host of url, across the
    threads and processes of a data folder: lock files <host>.<n>.lock, each tried in turn,
    waiting while all are taken. A file lock of the OS: a killed process releases it, and the
    files are a fixed few per host. Nothing to wait for without a remote host (a local mirror)."""
    host = urlsplit(url).hostname if url and re.match(r"https?://", url) else None
    if not host:
        yield
        return
    folder = _locks(cache_dir, "hosts")
    slots = list(range(HOST_SLOTS_OF.get(host, HOST_SLOTS)))
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        random.shuffle(slots)  # waiters spread over the slots
        for n in slots:
            # A new instance per attempt: filelock's instances are per thread, and two of them
            # on one file exclude each other also within a process (Windows LockFileEx, flock)
            lock = FileLock(os.path.join(folder, f"{host}.{n}.lock"))
            try:
                lock.acquire(blocking=False)
            except Timeout:
                continue
            try:
                yield
            finally:
                lock.release()
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"{host} is busy with the downloads of other searches "
                               f"(waited {LOCK_WAIT_S // 60} min); try again later")
        time.sleep(0.1 + 0.2 * random.random())


@contextlib.contextmanager
def tile_lock(cache_dir: str, path: str):
    """The lock of a cached file, so two searches never download it at once (the second waits
    and then finds it cached). One of TILE_LOCKS lock files chosen by the path, so their number
    stays fixed (on Linux filelock leaves its files behind); two tiles sharing one only wait
    for each other."""
    digest = hashlib.sha256(cache.relative(cache_dir, path).encode()).digest()
    name = f"{int.from_bytes(digest[:4], 'big') % TILE_LOCKS:04d}.lock"
    lock = FileLock(os.path.join(_locks(cache_dir, "tiles"), name))
    try:
        lock.acquire(timeout=LOCK_WAIT_S)
    except Timeout:
        raise RuntimeError(f"{cache.relative(cache_dir, path)} is being downloaded by another search "
                           f"(waited {LOCK_WAIT_S // 60} min); try again later") from None
    try:
        yield
    finally:
        lock.release()


def _part(path: str) -> str:
    """A temporary name next to path, unique per process and thread (searches may run in parallel)."""
    return f"{path}.{os.getpid()}-{threading.get_ident()}.part"


def _write_tile(path: str, band: np.ndarray, crs: str, transform, tags: Optional[Dict[str, str]] = None):
    """Save a float32 tile as a compressed GeoTIFF (atomically)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = _part(path)
    with rio.open(tmp, "w", driver="GTiff", height=band.shape[0], width=band.shape[1], count=1,
                  dtype="float32", crs=crs, transform=transform, nodata=np.nan,
                  compress="deflate", predictor=3, tiled=True) as dst:
        dst.write(band.astype(np.float32), 1)
        if tags:
            dst.update_tags(**tags)
    os.replace(tmp, path)


class Fetched(NamedTuple):
    """What a tile download got: bytes received (0: the service has no data there) and the source."""
    received: int
    url: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None

    @staticmethod
    def of(response, url: Optional[str] = None) -> "Fetched":
        return Fetched(len(response.content), url or response.url, response.headers.get("ETag"),
                       response.headers.get("Last-Modified"))


def _clean(band: np.ndarray, zero_is_nodata: bool = False) -> np.ndarray:
    band = band.astype(np.float32)
    band[band < VALID_MIN] = np.nan
    if zero_is_nodata:
        band[band == 0.0] = np.nan
    return band


@dataclass(frozen=True)
class Credit:
    """Who a dataset must be credited to and under which terms: recorded with every run
    that used it (run.json), shown in its map and written to its ATTRIBUTION.txt."""
    data: str         # the dataset
    attribution: str  # the notice to show with what is made from it (plain text)
    licence: str      # short name of the licence or terms of use
    url: str = ""     # where the licence or terms are stated
    notice: str = ""  # more the licence asks for in a notice (citation, disclaimer): ATTRIBUTION.txt only

    def as_dict(self) -> Dict:
        return asdict(self)


# Checked 2026-10-27 (docs/roadmap.md, "Before going public")
COPERNICUS_NOTICE = ("produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and "
                     "Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; "
                     "all rights reserved")
COPERNICUS_DISCLAIMER = ("The organisations in charge of the Copernicus programme by law or by delegation do "
                         "not incur any liability for any use of the Copernicus WorldDEM-30.")
COPERNICUS_LICENCE = ("Copernicus DEM licence", "https://docs.sentinel-hub.com/api/latest/static/files/data/dem/"
                                                "resources/license/License-COPDEM-30.pdf")
CC_BY = ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/")
GUGIK_CREDIT = Credit("NMT / NMPT (GUGiK, Poland)",
                      "Source: Główny Urząd Geodezji i Kartografii (GUGiK), geoportal.gov.pl",
                      "geoportal.gov.pl terms (free use, source stated)",
                      "https://www.geoportal.gov.pl/pl/o-geoportalu/regulamin/")
SWISSTOPO_CREDIT = Credit("swissALTI3D / swissSURFACE3D (swisstopo)", "© swisstopo",
                          "swisstopo OGD terms (free use, source stated)",
                          "https://www.swisstopo.admin.ch/en/conditions-geodata")
UGKK_CREDIT = Credit("DMR 5.0 / DMP 1.0 (ÚGKK SR, Slovakia)", "© ÚGKK SR", *CC_BY)
# IGN's form of a credit for a derived work (Orden FOM/2807/2015): "Obra derivada de <product> CC-BY 4.0 ign.es"
IGN_CREDIT = Credit("MDT05 / MDS05 (IGN Spain, PNOA-LiDAR)", "Obra derivada de MDT05 / MDS05 CC-BY 4.0 ign.es",
                    CC_BY[0], "https://www.ign.es/web/en/politica-datos")
MSDI = "https://msdi.data.gov.mt/geonetwork/"
MALTA_CREDITS = {
    2018: Credit("Malta LiDAR 2018 (Planning Authority, MSDI)", "Planning Authority Malta (MSDI), LiDAR 2018",
                 "no conditions on access and use (INSPIRE metadata)", MSDI),
    # The MSDI records were reported as CC BY-NC-ND 4.0, the INSPIRE ones as without conditions
    2012: Credit("Malta LiDAR 2012 (Planning Authority, MSDI)", "Planning Authority Malta (MSDI), LiDAR 2012",
                 "unresolved (CC BY-NC-ND 4.0 or no conditions)", MSDI),
}
COPERNICUS_CREDIT = Credit("Copernicus DEM GLO-30", COPERNICUS_NOTICE[0].upper() + COPERNICUS_NOTICE[1:],
                           *COPERNICUS_LICENCE, notice=COPERNICUS_DISCLAIMER)
WATER_MASK_CREDIT = Credit("Copernicus DEM GLO-30 water body mask", COPERNICUS_CREDIT.attribution,
                           *COPERNICUS_LICENCE, notice=COPERNICUS_DISCLAIMER)
# The record at data.bris.ac.uk states CC BY-NC-SA 4.0 in its description and its
# license.txt (its licence field says "Non-Commercial Government Licence"; both are non-commercial)
FABDEM_CREDIT = Credit(
    "FABDEM V1-2", "FABDEM V1-2 © University of Bristol; FABDEM is " + COPERNICUS_NOTICE,
    "CC BY-NC-SA 4.0", "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    notice="Neal, J., Hawker, L. (2023): FABDEM V1-2. https://doi.org/10.5523/bris.s5hqmjcdj8yo2ibzi9b4ew3sn. "
           "Non-commercial use only; results computed from FABDEM are adapted material, shared under "
           "the same licence. " + COPERNICUS_DISCLAIMER)
NASADEM_CREDIT = Credit("NASADEM", "NASADEM: NASA JPL (2020), NASA LP DAAC, via OpenTopography",
                        "NASA open data (no restrictions)",
                        "https://doi.org/10.5067/MEaSUREs/NASADEM/NASADEM_HGT.001",
                        notice="Data accessed through OpenTopography (opentopography.org).")
# All 3DEP products are public domain; USGS asks for a credit (copyrights-and-credits page)
USGS_TERMS = ("public domain (USGS)",
              "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits")
USGS_1M_CREDIT = Credit("3DEP 1 m DEM (USGS)", "USGS 3D Elevation Program (3DEP)", *USGS_TERMS)
USGS_IFSAR_CREDIT = Credit("3DEP Alaska IfSAR 5 m DTM (USGS)", "USGS 3D Elevation Program (3DEP)", *USGS_TERMS)
USGS_13_CREDIT = Credit("3DEP 1/3 arc-second DEM (USGS)", "USGS 3D Elevation Program (3DEP)", *USGS_TERMS)
OSM_CREDIT = Credit("OpenStreetMap", "© OpenStreetMap contributors", "ODbL 1.0",
                    "https://www.openstreetmap.org/copyright")


class Layer:
    """One elevation model of a source, cached in cache/<folder> (see cache.py)."""
    description = ""
    folder = ""
    credit: Optional[Credit] = None  # the source's, unless set for this layer (Source.__post_init__)
    # Correction (east, north) in metres of the layer CRS for data whose service is
    # georeferenced wrongly; applied when tiles are placed on the grid
    shift: Tuple[float, float] = (0.0, 0.0)
    # Version of what download_tile does with the data; tiles made by another version are fetched again
    processing = 1

    @property
    def ttl_days(self) -> Optional[int]:
        """Days between update checks; None for a release that never changes (see cache.py)."""
        return cache.TTL_DAYS["live"] if "/live" in self.folder else None

    def tiles(self, grid: Grid) -> list:
        raise NotImplementedError

    def tile_path(self, cache_dir: str, tile) -> str:
        raise NotImplementedError

    def bytes_per_tile(self) -> float:
        return 0

    def missing(self, grid: Grid, cache_dir: str, update: Optional[str]) -> list:
        """Tiles to download: not cached yet, or due for a check with update ("stale" or "all")."""
        return [t for t in self.tiles(grid)
                if cache.due(cache_dir, self.tile_path(cache_dir, t), self.processing, update, self.ttl_days)]

    def estimate(self, grid: Grid, cache_dir: str, update: Optional[str]) -> Tuple[int, int, float]:
        """(tiles, tiles to download, MB to download)."""
        missing = self.missing(grid, cache_dir, update)
        return len(self.tiles(grid)), len(missing), len(missing) * self.bytes_per_tile() / 1e6

    def download_tile(self, tile, path: str) -> Fetched:
        """Download one tile to path (received 0 and no file if the service has no data there)."""
        raise NotImplementedError

    def download(self, grid: Grid, cache_dir: str, update: Optional[str], label: str):
        download_layers([(self, label)], grid, cache_dir, update)

    def to_download(self, grid: Grid, cache_dir: str, update: Optional[str]) -> Tuple[list, Optional[str]]:
        """The tiles download_layers fetches, and the line saying how many there are (None: none)."""
        todo = self.missing(grid, cache_dir, update)
        total = len(self.tiles(grid))
        return todo, f"{self.description}: {total} tiles ({total - len(todo)} cached, {len(todo)} to download)"

    def downloaded(self, grid: Grid, cache_dir: str):
        """What a layer does once its tiles are downloaded (IndexedTiles: deletes older surveys)."""

    def tile_url(self, tile) -> Optional[str]:
        """Where a tile comes from, for the limit per host (host_slot); None: a local copy."""
        return None

    def unavailable(self) -> Optional[str]:
        """Why this layer cannot be used here (e.g. no copy of our own tiles is set up), or None."""
        return None

    def tile_of(self, path: str):
        """The tile of a cached file (path relative to the cache, with /), or None if it
        is not one of this layer's tiles. Used by update.py for data that can change."""
        return None

    def has_data_near(self, lat: float, lon: float, cache_dir: str) -> Optional[bool]:
        """Whether the layer has data near a point, when its index can tell (None: unknown)."""
        return None

    def _download_and_record(self, tile, cache_dir: str, update: Optional[str] = None,
                             if_due: bool = False) -> int:
        """Download a tile under its lock (tile_lock) and a slot of its host (host_slot); with
        if_due only if it is still due then (another search may have just downloaded it)."""
        path = self.tile_path(cache_dir, tile)
        with tile_lock(cache_dir, path):
            if if_due and not cache.due(cache_dir, path, self.processing, update, self.ttl_days):
                return 0
            with host_slot(cache_dir, self.tile_url(tile)):
                fetched = self.download_tile(tile, path)
            # A tile outside the data (e.g. sea) is remembered as missing
            cache.record(cache_dir, path, "ok" if os.path.exists(path) else "missing", fetched.url,
                         fetched.etag, fetched.last_modified, self.source_date(tile), self.processing)
        return fetched.received

    def source_date(self, tile) -> Optional[str]:
        """Date (or year) of the survey in the tile, when the service says it."""
        return None

    def files(self, grid: Grid, cache_dir: str) -> List[str]:
        return [p for p in (self.tile_path(cache_dir, t) for t in self.tiles(grid)) if os.path.exists(p)]

    def used(self, grid: Grid, cache_dir: str) -> Dict:
        """What a run records about the data it used (see runs.py)."""
        files = self.files(grid, cache_dir)
        info = {"layer": self.description, "folder": self.folder, "files": len(files),
                "fetched": cache.touch(cache_dir, files),
                "paths": sorted(cache.relative(cache_dir, p) for p in files)}
        if self.credit:
            info["credit"] = self.credit.as_dict()
        return info


def download_layers(layers: Sequence[Tuple[Layer, str]], grid: Grid, cache_dir: str,
                    update: Optional[str] = None):
    """Download what the layers (layer, label) miss over the grid in one pool of DOWNLOAD_THREADS,
    so a slow tile of one layer does not hold the others: one progress bar for all their tiles,
    a line per layer of how many it has. Each tile under its lock and a slot of its host
    (Layer._download_and_record), skipped when another search downloaded it meanwhile."""
    # The layers' indexes asked at the same time too (a WFS query of geoportal.gov.pl takes
    # seconds), each layer once; the lines in the order of the layers
    distinct = list({id(layer): layer for layer, _ in layers}.values())
    with ThreadPoolExecutor(max_workers=len(distinct)) as pool:
        listed = list(pool.map(lambda layer: layer.to_download(grid, cache_dir, update), distinct))
    todo = []
    for layer, (tiles, line) in zip(distinct, listed):
        if line:
            print(line)
        todo += [(layer, tile) for tile in tiles]
    if todo:
        labels = list(dict.fromkeys(label for layer, label in layers if any(t[0] is layer for t in todo)))
        progress.download.start(f"{', '.join(labels)} tiles", len(todo), unit="tile")
        received = 0
        pool = ThreadPoolExecutor(max_workers=DOWNLOAD_THREADS)
        try:
            futures = [pool.submit(layer._download_and_record, tile, cache_dir, update, True)
                       for layer, tile in todo]
            for future in as_completed(futures):
                received += future.result()
                progress.download.update(1, MB=f"{received / 1e6:.0f}")
        finally:
            pool.shutdown(cancel_futures=True)  # after an error: the tiles not started yet are not
        progress.download.finish(f"{received / 1e6:.0f} MB")
    for layer, _ in layers:
        layer.downloaded(grid, cache_dir)


class WcsTiles(Layer):
    """Square tiles from a WCS 2.0 service, on a fixed grid in the service CRS.

    fetch_res=None requests tiles at the working resolution (the server
    downsamples); a number requests tiles at that resolution, cached independently
    of the working resolution and downsampled locally.

    Some services (Malta) refuse a request that reaches beyond the coverage with a 404,
    which would make every tile at the edge of the data "missing" (the whole north
    coast of Gozo). Then the request is clipped to the coverage extent
    (DescribeCoverage), on whole pixels of the tile grid, and the rest of the tile
    stays without data.
    """

    def __init__(self, description: str, url: str, coverage: str, crs: str, folder: str,
                 native_res: float, tile_px: int = 2000, fmt: str = "image/tiff",
                 axes: Tuple[str, str] = ("x", "y"), fetch_res: Optional[float] = None,
                 interpolation: Optional[str] = None, zero_is_nodata: bool = False,
                 bytes_per_px: float = 4, shift: Tuple[float, float] = (0.0, 0.0)):
        self.description, self.url, self.coverage, self.crs = description, url, coverage, crs
        self.folder, self.native_res, self.tile_px, self.fmt = folder, native_res, tile_px, fmt
        self.axes, self.fetch_res, self.interpolation = axes, fetch_res, interpolation
        self.zero_is_nodata, self.bytes_per_px, self.shift = zero_is_nodata, bytes_per_px, shift
        self._res = None
        self._extent: Optional[tuple] = None  # (left, bottom, right, top) of the coverage; () if unknown

    def _tile_res(self, grid: Grid) -> float:
        return self.fetch_res or grid.res

    def tiles(self, grid: Grid):
        self._res = res = self._tile_res(grid)
        size = self.tile_px * res
        left, bottom, right, top = transform_bounds(grid.crs, self.crs, *grid.bounds)
        # Tiles hold the data where the service puts it, i.e. before the shift correction
        cx, cy, radius = (left + right) / 2 - self.shift[0], (bottom + top) / 2 - self.shift[1], (right - left) / 2
        shape = None
        if grid.area_shape is not None:
            from area import project
            from shapely.affinity import translate
            shape = translate(project(grid.area_shape, grid.crs, self.crs), -self.shift[0], -self.shift[1])
        tiles = []
        for i in range(math.floor((cx - radius) / size), math.floor((cx + radius) / size) + 1):
            for j in range(math.floor((cy - radius) / size), math.floor((cy + radius) / size) + 1):
                if shape is not None:  # any area: the tiles under its outline
                    from shapely.geometry import box
                    if shape.intersects(box(i * size, j * size, (i + 1) * size, (j + 1) * size)):
                        tiles.append((res, i, j))
                    continue
                dx = max(i * size - cx, 0, cx - (i + 1) * size)  # nearest point of the tile
                dy = max(j * size - cy, 0, cy - (j + 1) * size)
                if dx * dx + dy * dy <= radius * radius:
                    tiles.append((res, i, j))
        return tiles

    def _tiling(self, res: float) -> str:
        # The tiling in the path: tiles of another CRS, size or format never mix
        fmt = "asc" if re.search(r"aaigrid|asc", self.fmt) else "tif"
        return f"{self.crs.lower().replace(':', '')}-{res:g}m-{self.tile_px}px-{fmt}"

    def tile_path(self, cache_dir: str, tile) -> str:
        res, i, j = tile
        return os.path.join(cache_dir, self.folder, self._tiling(res), f"{i}_{j}.tif")

    def tile_of(self, path: str):
        match = re.fullmatch(rf"{re.escape(self.folder)}/([^/]+)/(-?\d+)_(-?\d+)\.tif", path)
        if not match:
            return None
        res = float(re.search(r"-([\d.]+)m-", match.group(1)).group(1))
        return (res, int(match.group(2)), int(match.group(3))) if match.group(1) == self._tiling(res) else None

    def bounds_wgs84(self, tile) -> Tuple[float, float, float, float]:
        """(west, south, east, north) of a tile."""
        res, i, j = tile
        size = self.tile_px * res
        return transform_bounds(self.crs, "EPSG:4326", i * size, j * size, (i + 1) * size, (j + 1) * size)

    def bytes_per_tile(self) -> float:
        return self.tile_px ** 2 * self.bytes_per_px

    def coverage_extent(self) -> Optional[tuple]:
        """(left, bottom, right, top) of the coverage in the service CRS, or None if unknown."""
        if self._extent is None:
            try:
                response = requests.get(self.url, timeout=60, headers=cache.http_headers(), params={
                    "SERVICE": "WCS", "VERSION": "2.0.1", "REQUEST": "DescribeCoverage", "COVERAGEID": self.coverage})
                response.raise_for_status()
                text = response.text
                lower = [float(v) for v in re.search(r"<gml:lowerCorner>([^<]+)<", text).group(1).split()]
                upper = [float(v) for v in re.search(r"<gml:upperCorner>([^<]+)<", text).group(1).split()]
                labels = re.search(r'axisLabels="([^"]+)"', text)
                if labels and labels.group(1).split()[0] == self.axes[1]:  # northing first
                    lower, upper = lower[::-1], upper[::-1]
                self._extent = (lower[0], lower[1], upper[0], upper[1])
            except (requests.RequestException, AttributeError, ValueError, IndexError):
                self._extent = ()
        return self._extent or None

    def tile_url(self, tile) -> Optional[str]:
        return self.url

    def _request(self, left: float, bottom: float, right: float, top: float, res: float):
        """GetCoverage of a box; None if the service has no data there (404)."""
        ax, ay = self.axes
        params = [("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
                  ("COVERAGEID", self.coverage), ("FORMAT", self.fmt),
                  ("SUBSET", f"{ax}({left:.3f},{right:.3f})"),
                  ("SUBSET", f"{ay}({bottom:.3f},{top:.3f})")]
        if not math.isclose(res, self.native_res):
            params.append(("SCALESIZE", f"{ax}({round((right - left) / res)}),{ay}({round((top - bottom) / res)})"))
            if self.interpolation:
                params.append(("INTERPOLATION", self.interpolation))
        return _get(self.url, params)

    def download_tile(self, tile, path: str) -> Fetched:
        res, i, j = tile
        size, px = self.tile_px * res, self.tile_px
        left, bottom = i * size, j * size
        try:
            response = self._request(left, bottom, left + size, bottom + size, res)
            extent = self.coverage_extent() if response is None else None
            if extent:
                # Inside the coverage, on whole pixels of the tile
                inner = (left + math.ceil(max(0.0, extent[0] - left) / res) * res,
                         bottom + math.ceil(max(0.0, extent[1] - bottom) / res) * res,
                         left + size - math.ceil(max(0.0, left + size - extent[2]) / res) * res,
                         bottom + size - math.ceil(max(0.0, bottom + size - extent[3]) / res) * res)
                if inner[0] < inner[2] and inner[1] < inner[3] and inner != (left, bottom, left + size, bottom + size):
                    response = self._request(*inner, res)
        except RuntimeError as e:
            raise RuntimeError(f"Could not download {self.description} tile {i}_{j}: {e}")
        if response is None:
            return Fetched(0, self.url)
        data = response.content
        content_type = response.headers.get("content-type", "")
        if "multipart" in content_type:
            data = _extract_multipart(data, content_type)

        ext = ".asc" if "aaigrid" in self.fmt else ".tif"
        with MemoryFile(data, ext=ext) as memfile, memfile.open() as src:
            band = src.read(1, masked=True).astype(np.float32).filled(np.nan)
            src_crs = src.crs or self.crs  # ASCII grids come without a CRS
            src_transform = src.transform
        dst_transform = from_origin(left, bottom + size, res, res)
        if band.shape != (px, px) or not src_transform.almost_equals(dst_transform):
            resampled = np.full((px, px), np.nan, dtype=np.float32)
            reproject(band, resampled, src_transform=src_transform, src_crs=src_crs,
                      dst_transform=dst_transform, dst_crs=self.crs,
                      src_nodata=np.nan, dst_nodata=np.nan, resampling=Resampling.nearest)
            band = resampled
        _write_tile(path, _clean(band, self.zero_is_nodata), self.crs, dst_transform)
        return Fetched.of(response)


def _extract_multipart(content: bytes, content_type: str) -> bytes:
    """Pull the coverage out of a multipart/related WCS response."""
    boundary = re.search(r'boundary="?([^";]+)"?', content_type).group(1).encode()
    for part in content.split(b"--" + boundary):
        head, _, body = part.partition(b"\r\n\r\n")
        kind = re.search(rb"Content-Type:\s*([^\r\n;]+)", head, re.I)
        # The coverage itself (image/tiff, image/x-aaigrid, application/asc...), not its XML description
        if kind and not re.search(rb"xml|gml|text/", kind.group(1), re.I):
            return body.rstrip(b"\r\n")
    raise RuntimeError("WCS response did not contain coverage data")


def _hemi(value: int, positive: str, negative: str, digits: int) -> str:
    return f"{positive if value >= 0 else negative}{abs(value):0{digits}d}"


def grid_tile_name(i: int, j: int, block: int = 10) -> str:
    """Path of tile (i, j) of a GridTiles layer: blocks of block x block tiles per folder
    (shared with tools/sk_tiles.py, which makes the tiles)."""
    return f"{i // block}_{j // block}/{i}_{j}.tif"


# The folder above the local copies of our own tiles (tools/sk_tiles.py writes them in the
# layout of the Hugging Face dataset, e.g. <mirror>/slovakia/dmr5/1m/...)
MIRROR_ENV = "FINDALINE_MIRROR"


class GridTiles(Layer):
    """Square tiles on a fixed grid in a metric CRS, our own hosted tiles (tools/sk_tiles.py),
    as <base>/<grid_tile_name>.

    The bases are tried in turn: the local mirror first (<FINDALINE_MIRROR>/<mirror>, when
    that folder exists), then the URLs. A mirror is copied from like a server: its
    index.json says which tiles exist (null: outside the data, recorded as missing) and
    their sha256; a tile not in the index is not cut yet and is looked for further. Over
    HTTP a tile that the server does not have (404) is outside the data.
    Tile (i, j) covers [i, i + 1) x [j, j + 1) tile sizes in the CRS.
    """

    def __init__(self, description: str, urls: List[str], crs: str, folder: str, tile_m: float,
                 native_res: float, bytes_per_tile: float, mirror: Optional[str] = None):
        self.description, self.urls, self.crs, self.folder = description, urls, crs, folder
        self.tile_m, self.native_res, self._bytes, self.mirror = tile_m, native_res, bytes_per_tile, mirror
        self._indexes: Dict[str, Dict] = {}

    def bases(self) -> List[str]:
        """Where the tiles come from, in order (the mirror is read from the environment now,
        so a worker process sees the same as the app)."""
        root = os.environ.get(MIRROR_ENV)
        local = os.path.join(root, *self.mirror.split("/")) if root and self.mirror else None
        return ([local] if local and os.path.isdir(local) else []) + list(self.urls)

    def unavailable(self) -> Optional[str]:
        if self.bases():
            return None
        where = (f"{os.environ[MIRROR_ENV]}/{self.mirror} does not exist" if os.environ.get(MIRROR_ENV)
                 else f"set {MIRROR_ENV} to the folder holding {self.mirror} (made by tools/sk_tiles.py)")
        return f"{self.description}: no copy of the tiles is set up ({where})"

    def tiles(self, grid: Grid):
        left, bottom, right, top = transform_bounds(grid.crs, self.crs, *grid.bounds)
        shape = None
        if grid.area_shape is not None:
            from area import project
            shape = project(grid.area_shape, grid.crs, self.crs)
        found = []
        for j in range(math.floor(bottom / self.tile_m), math.floor(top / self.tile_m) + 1):
            for i in range(math.floor(left / self.tile_m), math.floor(right / self.tile_m) + 1):
                if shape is not None:
                    from shapely.geometry import box
                    if not shape.intersects(box(i * self.tile_m, j * self.tile_m,
                                                (i + 1) * self.tile_m, (j + 1) * self.tile_m)):
                        continue
                found.append((i, j))
        return found

    def tile_path(self, cache_dir: str, tile) -> str:
        return os.path.join(cache_dir, self.folder, grid_tile_name(*tile))

    def bytes_per_tile(self) -> float:
        return self._bytes

    def _index(self, base: str) -> Dict:
        """The tiles of a local mirror (read once; {} while it has no index)."""
        if base not in self._indexes:
            path, tiles = os.path.join(base, "index.json"), {}
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    tiles = json.load(f)["tiles"]
            self._indexes[base] = tiles  # whole: the download threads read it meanwhile
        return self._indexes[base]

    def _copy(self, base: str, tile, path: str) -> Optional[Fetched]:
        """Copy a tile from a local mirror; None if it is not cut there yet."""
        key = f"{tile[0]}_{tile[1]}"
        index = self._index(base)
        if key not in index:
            return None
        source = os.path.join(base, *grid_tile_name(*tile).split("/"))
        if index[key] is None:
            return Fetched(0, source)  # not in the data
        with open(source, "rb") as f:
            data = f.read()
        if hashlib.sha256(data).hexdigest() != index[key]:
            raise RuntimeError(f"{source} does not match the sha256 in its index.json (cut it again)")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = _part(path)
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return Fetched(len(data), source)

    def tile_url(self, tile) -> Optional[str]:
        bases = self.bases()
        return bases[0] if bases else None  # a local mirror first: no host

    def download_tile(self, tile, path: str) -> Fetched:
        name, error = grid_tile_name(*tile), None
        for base in self.bases():  # the first that has it
            if not re.match(r"https?://", base):
                fetched = self._copy(base, tile, path)
                if fetched is not None:
                    return fetched
                error = f"not in {base} yet"
                continue
            url = f"{base}/{name}"
            try:
                response = _get(url)
            except RuntimeError as e:
                error = e
                continue
            if response is None:
                return Fetched(0, url)  # not in the data
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = _part(path)
            with open(tmp, "wb") as f:
                f.write(response.content)
            os.replace(tmp, path)
            return Fetched.of(response, url)
        raise RuntimeError(f"Could not download {self.description} tile {name}: {error or self.unavailable()}")


class DegreeTiles(Layer):
    """1°×1° tiles in WGS84, downloaded whole at native resolution."""

    def __init__(self, description: str, folder: str, url: Callable[[int, int], str],
                 native_res: float = 30, bytes_per_tile: float = 15e6, credit: Optional[Credit] = None):
        self.description, self.folder, self.url = description, folder, url
        self.native_res, self._bytes = native_res, bytes_per_tile
        self.credit = credit

    def tiles(self, grid: Grid):
        west, south, east, north = transform_bounds(grid.crs, "EPSG:4326", *grid.bounds)
        return [(lat, lon) for lat in range(math.floor(south), math.floor(north) + 1)
                for lon in range(math.floor(west), math.floor(east) + 1)]

    def tile_path(self, cache_dir: str, tile) -> str:
        lat, lon = tile
        return os.path.join(cache_dir, self.folder, f"{_hemi(lat, 'N', 'S', 2)}{_hemi(lon, 'E', 'W', 3)}.tif")

    def bytes_per_tile(self) -> float:
        return self._bytes

    def tile_url(self, tile) -> Optional[str]:
        return self.url(*tile)

    def download_tile(self, tile, path: str) -> Fetched:
        lat, lon = tile
        url = self.url(lat, lon)
        try:
            response = _get(url)
        except RuntimeError as e:
            raise RuntimeError(f"Could not download {self.description} tile {lat},{lon}: {e}")
        if response is None:  # no tile there (sea)
            return Fetched(0, url)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = _part(path)
        with open(tmp, "wb") as f:
            f.write(response.content)
        os.replace(tmp, path)
        return Fetched.of(response, url)


class OpenTopographyTiles(DegreeTiles):
    """Global DEMs from the OpenTopography API (needs a free API key)."""
    API = "https://portal.opentopography.org/API/globaldem"
    api_key: Optional[str] = None

    def __init__(self, description: str, folder: str, demtype: str, native_res: float = 30):
        super().__init__(description, folder, self._url, native_res, bytes_per_tile=8e6)
        self.demtype = demtype

    def _url(self, lat: int, lon: int) -> str:
        if not self.api_key:
            raise ServiceError("an OpenTopography API key is needed: set OPENTOPOGRAPHY_API_KEY "
                               "or use --opentopo-key (free at portal.opentopography.org)", "opentopo_key")
        return (f"{self.API}?demtype={self.demtype}&south={lat}&north={lat + 1}&west={lon}"
                f"&east={lon + 1}&outputFormat=GTiff&API_Key={self.api_key}")


@dataclass(frozen=True)
class Sheet:
    """One file of a tile index: a map sheet or a tile of one survey (see IndexedTiles)."""
    year: int
    name: str   # file name without extension, e.g. 77532_1356765_M-34-64-D-a-1-1
    godlo: str  # the sheet or tile it covers; the newest survey of each is used
    url: str
    crs: str
    res: float
    sha256: Optional[str] = None  # of the file, when the index gives it


class IndexedTiles(Layer):
    """Files found through an index of the service, one per sheet or tile and survey.

    For every sheet or tile (godło) only the newest survey is used, and the files are
    ordered newest first, so where sheets of different series overlap the newest data
    wins in the mosaic. Each file is cached as a GeoTIFF named <year>_<name>.tif.

    The index answers are cached too, per 0.25° tile (osm.tiles) in
    <top folder>/sheet-index/<prefix>/<tile>.json with every listed version and its
    extent (in index_crs), so a search in a known place asks the index nothing. Without
    the index (offline) and without its cached answer, the cached files over the area
    are used. An update asks the index again and replaces older surveys (update.py).

    Subclasses ask the index (_fetch_tile) and download a file (download_tile).
    """
    index_crs = ""  # CRS of the extents in the cached index answers

    def __init__(self, description: str, index_url: str, prefix: str, folder: str,
                 native_res: float, bytes_per_tile: float):
        self.description, self.index_url, self.prefix = description, index_url, prefix
        self.folder, self.native_res, self._bytes = folder, native_res, bytes_per_tile
        self._listed: Dict[tuple, list] = {}
        self._versions: Dict[str, set] = {}  # every listed survey of a godło
        self._cache_dir: Optional[str] = None  # set by the methods that get it (tiles() does not)
        self._update: Optional[str] = None

    def _fetch_tile(self, lat: float, lon: float, path: str):
        """Every usable version listed by the index in one 0.25° tile, saved as JSON
        ({"sheets": [{"year", "godlo", "url", "crs", "res", "extent"}, ...]})."""
        raise NotImplementedError

    def index_path(self, cache_dir: str, lat: float, lon: float) -> str:
        """The cached index answer of one 0.25° tile."""
        top = self.folder.split("/")[0]
        return os.path.join(cache_dir, top, "sheet-index", self.prefix, f"{osm.tile_name(lat, lon)}.json")

    def _index_tile(self, cache_dir: str, lat: float, lon: float, update: Optional[str] = None) -> List[Dict]:
        """The sheets listed in one tile, asking the index when not cached or stale."""
        path = self.index_path(cache_dir, lat, lon)
        due = lambda: cache.due(cache_dir, path, update=update, ttl=cache.TTL_DAYS["sheet-index"])  # noqa: E731
        if due():
            with tile_lock(cache_dir, path):  # another search may be asking for it now
                if due():
                    self._fetch_tile(lat, lon, path)
                    cache.record(cache_dir, path, url=self.index_url)
        with open(path, encoding="utf-8") as f:
            return json.load(f)["sheets"]

    def _sheet(self, item: Dict) -> Sheet:
        url = item["url"]
        return Sheet(item["year"], re.sub(r"(\.[a-z]+)+$", "", url.rsplit("/", 1)[-1], flags=re.I),
                     item["godlo"], url, item["crs"], item["res"])

    def _wanted(self, sheet: Sheet, grid: Grid) -> bool:
        """Whether a listed file is used for this grid."""
        return True

    def _query(self, grid: Grid, cache_dir: str) -> list:
        """The newest sheet per godło over the grid, newest first."""
        west, south, east, north = transform_bounds(grid.crs, "EPSG:4326", *grid.bounds)
        g_left, g_bottom, g_right, g_top = transform_bounds(grid.crs, self.index_crs, *grid.bounds)
        shape = None
        if grid.area_shape is not None:
            from area import project
            from shapely.geometry import box
            shape = project(grid.area_shape, grid.crs, self.index_crs).intersection(
                box(g_left, g_bottom, g_right, g_top))
        newest: Dict[str, Sheet] = {}
        for lat, lon in osm.tiles(west, south, east, north):
            for item in self._index_tile(cache_dir, lat, lon, self._update):
                left, bottom, right, top = item["extent"]
                if not (left < g_right and right > g_left and bottom < g_top and top > g_bottom):
                    continue
                if shape is not None and not shape.intersects(box(left, bottom, right, top)):
                    continue
                sheet = self._sheet(item)
                if not self._wanted(sheet, grid):
                    continue
                self._versions.setdefault(sheet.godlo, set()).add(sheet)
                old = newest.get(sheet.godlo)
                if old is None or _sheet_order(sheet) > _sheet_order(old):
                    newest[sheet.godlo] = sheet
        return sorted(newest.values(), key=_sheet_order, reverse=True)

    def _prune(self, cache_dir: str, sheets: List[Sheet]) -> int:
        """Delete cached older surveys of sheets whose newest survey is cached; how many."""
        older = []
        for sheet in sheets:
            if os.path.exists(self.tile_path(cache_dir, sheet)):
                older += [self.tile_path(cache_dir, v) for v in self._versions.get(sheet.godlo, ())
                          if v != sheet and os.path.exists(self.tile_path(cache_dir, v))]
        cache.forget(cache_dir, older)
        return len(older)

    def update_index_tile(self, cache_dir: str, lat: float, lon: float) -> Tuple[int, int, int]:
        """Ask the index again for one tile and download the newer surveys of the cached
        sheets there (the older files are deleted). Returns (new sheets, MB, deleted)."""
        listed = [self._sheet(item) for item in self._index_tile(cache_dir, lat, lon, "all")]
        newest: Dict[str, Sheet] = {}
        for sheet in listed:
            self._versions.setdefault(sheet.godlo, set()).add(sheet)
            if sheet.godlo not in newest or _sheet_order(sheet) > _sheet_order(newest[sheet.godlo]):
                newest[sheet.godlo] = sheet
        cached = {s.godlo for s in listed if os.path.exists(self.tile_path(cache_dir, s))}
        todo = [s for g, s in newest.items() if g in cached and not os.path.exists(self.tile_path(cache_dir, s))]
        received = sum(self._download_and_record(s, cache_dir) for s in todo)
        return len(todo), received / 1e6, self._prune(cache_dir, list(newest.values()))

    def _cached_files(self, grid: Grid, cache_dir: str) -> Tuple[List[str], float]:
        """The cached files that could be used for this grid, and their resolution."""
        return glob.glob(os.path.join(cache_dir, self.folder, "*.tif")), self.native_res

    def _cached(self, grid: Grid, cache_dir: str) -> list:
        """Cached sheets over the grid, newest first (when the index cannot be reached)."""
        sheets = []
        paths, res = self._cached_files(grid, cache_dir)
        for path in paths:
            with rio.open(path) as src:
                left, bottom, right, top = transform_bounds(src.crs, grid.crs, *src.bounds)
            g_left, g_bottom, g_right, g_top = grid.bounds
            if left < g_right and right > g_left and bottom < g_top and top > g_bottom:
                year, name = os.path.basename(path)[:-4].split("_", 1)
                sheets.append(Sheet(int(year), name, "", "", "", res))
        return sorted(sheets, key=_sheet_order, reverse=True)

    def tiles(self, grid: Grid) -> list:
        key = (grid.crs, tuple(grid.bounds), grid.res, self._update)
        if key not in self._listed:
            try:
                self._listed[key] = self._query(grid, self._cache_dir)
            except requests.RequestException as e:
                print(f"{self.description}: index not available ({type(e).__name__}); "
                      "using the cached files")
                self._listed[key] = None
        return self._listed[key]

    def _use(self, cache_dir: str, update: Optional[str] = None):
        """What tiles() needs (it gets only the grid)."""
        self._cache_dir, self._update = cache_dir, update

    def missing(self, grid: Grid, cache_dir: str, update: Optional[str]) -> list:
        self._use(cache_dir, update)
        if self.tiles(grid) is None:
            return []
        return super().missing(grid, cache_dir, update)

    def estimate(self, grid: Grid, cache_dir: str, update: Optional[str]) -> Tuple[int, int, float]:
        self._use(cache_dir, update)
        if self.tiles(grid) is None:
            return len(self._cached(grid, cache_dir)), 0, 0.0
        return super().estimate(grid, cache_dir, update)

    def to_download(self, grid: Grid, cache_dir: str, update: Optional[str]) -> Tuple[list, Optional[str]]:
        self._use(cache_dir, update)
        if self.tiles(grid) is None:
            return [], None  # the index cannot be reached: the cached files are used
        return super().to_download(grid, cache_dir, update)

    def downloaded(self, grid: Grid, cache_dir: str):
        sheets = self.tiles(grid)
        if sheets is not None:
            pruned = self._prune(cache_dir, sheets)
            if pruned:
                print(f"{self.description}: {pruned} older surveys replaced by newer ones (deleted)")

    def tile_url(self, tile) -> Optional[str]:
        return tile.url

    def files(self, grid: Grid, cache_dir: str) -> List[str]:
        self._use(cache_dir, self._update)
        if self.tiles(grid) is None:
            return [self.tile_path(cache_dir, s) for s in self._cached(grid, cache_dir)]
        return super().files(grid, cache_dir)

    def tile_path(self, cache_dir: str, tile) -> str:
        return os.path.join(cache_dir, self.folder, f"{tile.year}_{tile.name}.tif")

    def source_date(self, tile) -> Optional[str]:
        return str(tile.year)

    def used(self, grid: Grid, cache_dir: str) -> Dict:
        info = super().used(grid, cache_dir)
        years = [int(os.path.basename(p).split("_", 1)[0]) for p in self.files(grid, cache_dir)]
        info["survey_years"] = [min(years), max(years)] if years else None
        return info

    def bytes_per_tile(self) -> float:
        return self._bytes


# Coordinate systems named in the uklad_xy attribute of the geoportal.gov.pl sheet indexes
SHEET_CRS = {"PL-1992": "EPSG:2180", "PL-2000:S5": "EPSG:2176", "PL-2000:S6": "EPSG:2177",
             "PL-2000:S7": "EPSG:2178", "PL-2000:S8": "EPSG:2179"}
SHEET_FORMATS = ("ARC/INFO ASCII GRID", "ASCII XYZ GRID")


class SheetTiles(IndexedTiles):
    """Map sheets listed in a WFS sheet index, downloaded as files (geoportal.gov.pl).

    The index has one layer per year (`<prefix><year>`); every sheet links to a file
    (ASCII grid or XYZ, sometimes zipped). Extents are cached in EPSG:2180.
    """
    index_crs = "EPSG:2180"

    def __init__(self, description: str, index_url: str, prefix: str, folder: str,
                 native_res: float, min_res: float = 0.5, bytes_per_tile: float = 30e6):
        """Sheets with a resolution between min_res and native_res are used (finer ones
        are huge as text: a 0.15 m sheet is about 2 GB)."""
        super().__init__(description, index_url, prefix, folder, native_res, bytes_per_tile)
        self.min_res = min_res
        self._years: Optional[List[str]] = None

    def _wfs(self, **params) -> str:
        response = requests.get(self.index_url, timeout=300, headers=cache.http_headers(),
                                params={"SERVICE": "WFS", "VERSION": "2.0.0", **params})
        response.raise_for_status()
        return response.text

    def _year_layers(self) -> List[str]:
        if self._years is None:
            names = re.findall(rf"<Name>(gugik:{self.prefix}\d{{4}})</Name>", self._wfs(REQUEST="GetCapabilities"))
            self._years = sorted(set(names))
        return self._years

    def _fetch_tile(self, lat: float, lon: float, path: str):
        sheets = []
        for layer in self._year_layers():
            text = self._wfs(REQUEST="GetFeature", TYPENAMES=layer, COUNT=10000,
                             PROPERTYNAME="godlo,akt_rok,char_przestrz,format,uklad_xy,url_do_pobrania,msGeometry",
                             BBOX=f"{lat},{lon},{lat + osm.TILE_DEG},{lon + osm.TILE_DEG},urn:ogc:def:crs:EPSG::4326")
            for member in text.split("<wfs:member>")[1:]:
                f = dict(re.findall(r"<gugik:(\w+)>([^<]*)</gugik:\1>", member))
                res = re.match(r"[\d.]+", f.get("char_przestrz", ""))
                url = f.get("url_do_pobrania", "")
                if not (res and url and f.get("format") in SHEET_FORMATS and f.get("uklad_xy") in SHEET_CRS):
                    continue
                if not self.min_res - 1e-6 <= float(res.group()) <= self.native_res + 1e-6:
                    continue
                # Extent from the sheet outline; EPSG:2180 lists northing before easting
                values = [float(v) for v in " ".join(re.findall(r"<gml:posList[^>]*>([^<]*)<", member)).split()]
                north, east = values[0::2], values[1::2]
                if not north:
                    continue
                sheets.append({"year": int(f["akt_rok"]), "godlo": f.get("godlo", ""), "url": url,
                               "crs": SHEET_CRS[f["uklad_xy"]], "res": float(res.group()),
                               "extent": [min(east), min(north), max(east), max(north)]})
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = _part(path)
        with open(tmp, "w", encoding="utf-8") as out:
            json.dump({"layers": self._year_layers(), "sheets": sheets}, out)
        os.replace(tmp, path)

    def download_tile(self, tile, path: str) -> Fetched:
        try:
            response = _get(tile.url, binary_types=("octet", "zip", "text", "image"))
        except RuntimeError as e:
            raise RuntimeError(f"Could not download {self.description} sheet {tile.godlo}: {e}")
        if response is None:
            return Fetched(0, tile.url)
        name, data = tile.url.lower(), response.content
        while name.endswith(".zip"):  # sometimes zipped twice
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                name = next(n for n in archive.namelist() if n.lower().endswith((".asc", ".xyz", ".txt", ".zip")))
                data = archive.read(name)
                name = name.lower()
        if name.endswith(".asc"):
            with MemoryFile(data, ext=".asc") as memfile, memfile.open() as src:
                band = src.read(1, masked=True).astype(np.float32).filled(np.nan)
                transform = src.transform
        else:
            band, transform = _xyz_grid(data, tile.res)
        _write_tile(path, _clean(band), tile.crs, transform)
        return Fetched.of(response, tile.url)


class StacTiles(IndexedTiles):
    """Square tiles of a STAC collection, one item per tile and survey year (swisstopo).

    An item (`<product>_<year>_<E>-<N>`, E and N in km) holds the tile as a
    cloud-optimized GeoTIFF at one or more resolutions (swissALTI3D: 0.5 and 2 m): the
    coarsest one not coarser than the working grid is used, cached in
    <folder>/<res>m/<year>_<E>-<N>.tif as published. Every file is checked against the
    sha256 of its item (file:checksum). The index answers (items per 0.25° tile) are
    cached and updated like a sheet index, so a newer survey of a tile replaces the
    older one.
    """

    def __init__(self, description: str, api: str, collection: str, folder: str, crs: str,
                 sizes: Dict[float, float], tile_m: float = 1000):
        """sizes: bytes per tile of every resolution used."""
        super().__init__(description, f"{api}/collections/{collection}/items", collection, folder,
                         native_res=min(sizes), bytes_per_tile=max(sizes.values()))
        self.index_crs, self.crs, self.sizes, self.tile_m = crs, crs, sizes, tile_m

    def _fetch_tile(self, lat: float, lon: float, path: str):
        sheets = []
        url = self.index_url
        params = {"bbox": f"{lon},{lat},{lon + osm.TILE_DEG},{lat + osm.TILE_DEG}", "limit": 100}
        while url:  # pages of 100 items
            response = requests.get(url, params=params, timeout=120, headers=cache.http_headers())
            response.raise_for_status()
            page = response.json()
            for item in page["features"]:
                name = re.search(r"_(\d{4})_(\d+)-(\d+)$", item["id"])
                if not name:
                    continue
                year, east, north = int(name.group(1)), int(name.group(2)), int(name.group(3))
                for asset in item["assets"].values():
                    res = float(asset.get("gsd") or 0)
                    if "tiff" not in asset.get("type", "") or res not in self.sizes:
                        continue
                    checksum = asset.get("file:checksum", "").lower()
                    sheets.append({"year": year, "godlo": f"{east}-{north}", "url": asset["href"],
                                   "crs": self.crs, "res": res,
                                   "sha256": checksum[4:] if checksum.startswith("1220") else None,
                                   "extent": [east * self.tile_m, north * self.tile_m,
                                              (east + 1) * self.tile_m, (north + 1) * self.tile_m]})
            url = next((link["href"] for link in page.get("links", []) if link.get("rel") == "next"), None)
            params = None  # the next link carries them
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = _part(path)
        with open(tmp, "w", encoding="utf-8") as out:
            json.dump({"sheets": sheets}, out)
        os.replace(tmp, path)

    def _sheet(self, item: Dict) -> Sheet:
        # One godło per tile and resolution: the newest survey and pruning work per resolution
        return Sheet(item["year"], item["godlo"], f"{item['godlo']}/{item['res']:g}m", item["url"],
                     item["crs"], item["res"], item.get("sha256"))

    def res_for(self, grid: Grid) -> float:
        """The resolution used for a grid: the coarsest one not coarser than the grid."""
        fitting = [r for r in self.sizes if r <= grid.res + 1e-6]
        return max(fitting) if fitting else min(self.sizes)

    def _wanted(self, sheet: Sheet, grid: Grid) -> bool:
        return sheet.res == self.res_for(grid)

    def _cached_files(self, grid: Grid, cache_dir: str) -> Tuple[List[str], float]:
        res = self.res_for(grid)
        return glob.glob(os.path.join(cache_dir, self.folder, f"{res:g}m", "*.tif")), res

    def tile_path(self, cache_dir: str, tile) -> str:
        return os.path.join(cache_dir, self.folder, f"{tile.res:g}m", f"{tile.year}_{tile.name}.tif")

    def estimate(self, grid: Grid, cache_dir: str, update: Optional[str]) -> Tuple[int, int, float]:
        self._use(cache_dir, update)
        if self.tiles(grid) is None:
            return super().estimate(grid, cache_dir, update)
        missing = self.missing(grid, cache_dir, update)
        return len(self.tiles(grid)), len(missing), sum(self.sizes[t.res] for t in missing) / 1e6

    def download_tile(self, tile, path: str) -> Fetched:
        for attempt in range(RETRIES):
            try:
                response = _get(tile.url, binary_types=("tiff", "octet"))
            except RuntimeError as e:
                raise RuntimeError(f"Could not download {self.description} tile {tile.godlo} ({tile.year}): {e}")
            if response is None:
                return Fetched(0, tile.url)
            if not tile.sha256 or hashlib.sha256(response.content).hexdigest() == tile.sha256:
                break
            _sleep(attempt)
        else:
            raise RuntimeError(f"{self.description} tile {tile.godlo} ({tile.year}): wrong checksum")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = _part(path)
        with open(tmp, "wb") as f:
            f.write(response.content)
        os.replace(tmp, path)
        return Fetched.of(response, tile.url)


def _remote(url: str) -> str:
    """What rasterio opens for a file of an index: /vsicurl/ (HTTP range requests) for a URL,
    a local path as it is (tests)."""
    return f"/vsicurl/{url}" if re.match(r"https?://", url) else url


def _gdal_http() -> rio.Env:
    """GDAL's settings for reading windows of remote files: our User-Agent, no directory
    listing, retries of failed requests, neighbouring blocks in one request."""
    return rio.Env(GDAL_HTTP_USERAGENT=cache.http_headers()["User-Agent"],
                   GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                   GDAL_HTTP_MAX_RETRY=str(RETRIES), GDAL_HTTP_RETRY_DELAY="2", GDAL_HTTP_TIMEOUT="120",
                   GDAL_HTTP_MULTIRANGE="YES", GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES")


def _same_crs(a, b) -> bool:
    """Whether two CRSs are the same; also when one names only the ellipsoid of the other's
    datum (Alaska's IfSAR files: Alaska Albers on "GRS80, datum not specified")."""
    a, b = rio.crs.CRS.from_user_input(a), rio.crs.CRS.from_user_input(b)
    if a == b or (a.to_epsg() is not None and a.to_epsg() == b.to_epsg()):
        return True
    ellipsoid = {"NAD83": "GRS80", "WGS84": "WGS84"}

    def plain(crs) -> Dict:
        d = {k: v for k, v in crs.to_dict().items() if k not in ("no_defs", "type", "towgs84")}
        if "datum" in d:
            d["ellps"] = ellipsoid.get(d.pop("datum"), "?")
        return d
    pa, pb = plain(a), plain(b)
    return pa.keys() == pb.keys() and all(
        math.isclose(v, pb[k]) if isinstance(v, (int, float)) and isinstance(pb[k], (int, float)) else v == pb[k]
        for k, v in pa.items())


def _read_window(src, transform: Affine, shape: Tuple[int, int]) -> np.ndarray:
    """The values of an open raster on the pixels of transform and shape (in its CRS), NaN
    where it has none: read as they are when the pixels coincide (the usual case: the
    blocks are on the files' own pixels), resampled bilinearly otherwise."""
    t = src.transform
    out = np.full(shape, np.nan, dtype=np.float32)
    col, row = (transform.c - t.c) / t.a, (transform.f - t.f) / t.e
    if math.isclose(t.a, transform.a, rel_tol=1e-6) and math.isclose(t.e, transform.e, rel_tol=1e-6) \
            and abs(col - round(col)) < 0.01 and abs(row - round(row)) < 0.01:
        r0, c0 = round(row), round(col)
        r1, c1 = max(r0, 0), max(c0, 0)
        r2, c2 = min(r0 + shape[0], src.height), min(c0 + shape[1], src.width)
        if r1 < r2 and c1 < c2:
            part = src.read(1, window=Window(c1, r1, c2 - c1, r2 - r1), masked=True)
            out[r1 - r0:r2 - r0, c1 - c0:c2 - c0] = _clean(part.astype(np.float32).filled(np.nan))
        return out
    left, top = transform.c, transform.f
    right, bottom = left + shape[1] * transform.a, top + shape[0] * transform.e
    c1 = max(math.floor((left - t.c) / t.a) - 2, 0)
    c2 = min(math.ceil((right - t.c) / t.a) + 2, src.width)
    r1 = max(math.floor((top - t.f) / t.e) - 2, 0)
    r2 = min(math.ceil((bottom - t.f) / t.e) + 2, src.height)
    if r1 < r2 and c1 < c2:
        window = Window(c1, r1, c2 - c1, r2 - r1)
        band = _clean(src.read(1, window=window, masked=True).astype(np.float32).filled(np.nan))
        reproject(band, out, src_transform=src.window_transform(window), src_crs=src.crs,
                  dst_transform=transform, dst_crs=src.crs, src_nodata=np.nan, dst_nodata=np.nan,
                  resampling=Resampling.bilinear)
    return out


def _read_block(files: Sequence[Tuple[str, str, int]], crs: str, transform: Affine, shape: Tuple[int, int],
                path: str, what: str) -> Fetched:
    """Compose a block from windows of remote files (name, url, survey year), newest first:
    each file fills only the pixels still without data, and the next one is read only while
    some remain. Saved to path with the names and years of the files used (tags "sources",
    "years"); no file when none of them has data there."""
    block = np.full(shape, np.nan, dtype=np.float32)
    used, years = [], []
    with _gdal_http():
        for name, url, year in files:
            try:
                with rio.open(_remote(url)) as src:
                    if src.crs is not None and not _same_crs(src.crs, crs):
                        print(f"{what}: {name} is in {src.crs.to_string()}, not {crs}; not used")
                        continue
                    values = _read_window(src, transform, shape)
            except rio.errors.RasterioIOError as e:
                if re.search(r"\b404\b|No such file", str(e)):
                    continue  # listed, but not (or no longer) there
                raise RuntimeError(f"Could not read {what} {name}: {cache.clean_error(e)}")
            new = np.isnan(block) & np.isfinite(values)
            if new.any():
                block[new] = values[new]
                used.append(name)
                years.append(str(year))
            if not np.isnan(block).any():
                break
    url = files[0][1] if files else None
    if not used:
        return Fetched(0, url)
    _write_tile(path, block, crs, transform, tags={"sources": ",".join(used), "years": ",".join(years)})
    return Fetched(os.path.getsize(path), url)


TNM_API = "https://tnmaccess.nationalmap.gov/api/v1/products"


def _survey_year(url: str, published: str) -> int:
    """The year of a 3DEP project from its folders (e.g. CA_YosemiteNP_2019_D19 or
    AK_IFSAR_2010/AK_IFSAR-Cell23_2010): a year in the name, else the fiscal year of a work
    unit suffix (UT_WestEast_B22), else the year of publication."""
    folders = url.split("/Projects/", 1)[-1].rsplit("/", 2)[0] if "/Projects/" in url else ""
    years = re.findall(r"(?<!\d)((?:19|20)\d\d)(?!\d)", folders)
    if years:
        return max(int(y) for y in years)
    suffix = re.findall(r"_[A-Z](\d\d)(?=/|$)", folders)
    if suffix:
        return 2000 + max(int(y) for y in suffix)
    return int(published[:4]) if published[:4].isdigit() else 0


@dataclass(frozen=True)
class Block:
    """A square of a TnmBlocks layer: (i, j) on a grid of `size` metres in crs, composed from
    the files (name, url, survey year), newest first. The key tells sets of files apart: a new survey over
    the block gives it another key, so it is read again."""
    crs: str
    i: int
    j: int
    size: float
    year: int  # of the newest file
    key: str
    files: Tuple[Tuple[str, str, int], ...] = ()


class TnmBlocks(IndexedTiles):
    """Blocks of the files of a dataset of The National Map (USGS 3DEP), found through the
    TNM Access API and read by HTTP range requests.

    The files are big (10 km tiles of the 1 m DEM: 10-450 MB; Alaska's IfSAR cells: up to
    900 MB) and tiled, mostly cloud-optimized, so only the blocks over the grid are read:
    squares of block_m on a fixed grid in the files' CRS, each cached as a GeoTIFF of its own
    (<folder>/<epsg>/<i // 10>_<j // 10>/<year>_<i>_<j>_<key>.tif). A place is often in
    several files (surveys, "projects"), and a newer project may cover only part of a tile,
    so a block is composed newest first: an older file fills only the pixels still without
    data and is read only while some remain. The index answers are cached per 0.25° tile
    like a sheet index; a new project over a block changes its key, so the block is read
    again (update.py, or a search after the answer was renewed) and the older file deleted.
    """
    index_crs = "EPSG:4326"

    def __init__(self, description: str, dataset: str, prefix: str, folder: str, native_res: float,
                 block_m: float, bytes_per_tile: float, api: str = TNM_API):
        super().__init__(description, api, prefix, folder, native_res, bytes_per_tile)
        self.dataset, self.block_m = dataset, block_m

    def _item(self, product: Dict) -> Optional[Dict]:
        """A product of the API as cached ({name, url, crs, extent (in crs), year, published}),
        or None if it is not used."""
        raise NotImplementedError

    def _fetch_tile(self, lat: float, lon: float, path: str):
        items: Dict[str, Dict] = {}
        offset, total = 0, 1
        while offset < total:  # pages of up to 500 products
            for attempt in range(RETRIES):
                try:
                    response = requests.get(self.index_url, timeout=120, headers=cache.http_headers(), params={
                        "datasets": self.dataset, "max": 500, "offset": offset,
                        "bbox": f"{lon},{lat},{lon + osm.TILE_DEG},{lat + osm.TILE_DEG}"})
                    response.raise_for_status()
                    page = response.json()
                    break
                except (requests.RequestException, ValueError) as e:  # the API is sometimes slow or down
                    if attempt == RETRIES - 1:
                        raise requests.RequestException(f"TNM API: {e}")
                    _sleep(attempt)
            products = page.get("items") or []
            for product in products:
                item = self._item(product)
                if item:
                    items[item["url"]] = item
            offset, total = offset + len(products), (page.get("total") or 0) if products else 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = _part(path)
        with open(tmp, "w", encoding="utf-8") as out:
            json.dump({"sheets": list(items.values())}, out)
        os.replace(tmp, path)

    def _items(self, cache_dir: str, west: float, south: float, east: float, north: float,
               update: Optional[str] = None) -> List[Dict]:
        """The listed files of the index tiles over a box (WGS84)."""
        items: Dict[str, Dict] = {}
        for lat, lon in osm.tiles(west, south, east, north):
            for item in self._index_tile(cache_dir, lat, lon, update):
                items[item["url"]] = item
        return list(items.values())

    def _blocks(self, items: List[Dict], bounds: Dict[str, tuple], shape=None) -> List[Block]:
        """The blocks within bounds (per CRS: left, bottom, right, top), and under the shape
        (in WGS84) if given, with the files over each, newest first."""
        size = self.block_m
        over: Dict[tuple, List[Dict]] = {}
        for item in items:
            if item["crs"] not in bounds:
                continue
            left, bottom, right, top = bounds[item["crs"]]
            e_left, e_bottom, e_right, e_top = item["extent"]
            left, bottom, right, top = max(left, e_left), max(bottom, e_bottom), min(right, e_right), min(top, e_top)
            if left >= right or bottom >= top:
                continue
            for i in range(math.floor(left / size), math.ceil(right / size)):
                for j in range(math.floor(bottom / size), math.ceil(top / size)):
                    over.setdefault((item["crs"], i, j), []).append(item)
        if shape is not None:
            from area import project
            from shapely.geometry import box
            projected = {crs: project(shape, "EPSG:4326", crs) for crs in bounds}
            over = {k: v for k, v in over.items()
                    if projected[k[0]].intersects(box(k[1] * size, k[2] * size, (k[1] + 1) * size, (k[2] + 1) * size))}
        blocks = []
        for (crs, i, j), files in over.items():
            files = sorted(files, key=lambda f: (f["year"], f.get("published", ""), f["name"]), reverse=True)
            key = hashlib.sha1(",".join(sorted(f["name"] for f in files)).encode()).hexdigest()[:8]
            blocks.append(Block(crs, i, j, size, files[0]["year"], key,
                                tuple((f["name"], f["url"], f["year"]) for f in files)))
        return blocks

    def _query(self, grid: Grid, cache_dir: str) -> list:
        west, south, east, north = transform_bounds(grid.crs, "EPSG:4326", *grid.bounds)
        items = self._items(cache_dir, west, south, east, north, self._update)
        bounds = {crs: transform_bounds(grid.crs, crs, *grid.bounds) for crs in {item["crs"] for item in items}}
        shape = None
        if grid.area_shape is not None:
            from area import project
            shape = project(grid.area_shape, grid.crs, "EPSG:4326")
        # Blocks in the working CRS first: copied onto the grid as they are, they win where
        # a tile is delivered in two UTM zones
        return sorted(self._blocks(items, bounds, shape),
                      key=lambda b: (not _same_crs(b.crs, grid.crs), b.crs, b.j, b.i))

    def tile_path(self, cache_dir: str, tile) -> str:
        return os.path.join(cache_dir, self.folder, tile.crs.lower().replace(":", ""),
                            f"{tile.i // 10}_{tile.j // 10}", f"{tile.year}_{tile.i}_{tile.j}_{tile.key}.tif")

    def _block_of(self, path: str) -> Optional[Block]:
        """The block of a cached file (without its files)."""
        match = re.search(r"[\\/](epsg\d+)[\\/]-?\d+_-?\d+[\\/](\d+)_(-?\d+)_(-?\d+)_([0-9a-f]+)\.tif$", path)
        if not match:
            return None
        return Block(match.group(1).upper().replace("EPSG", "EPSG:"), int(match.group(3)), int(match.group(4)),
                     self.block_m, int(match.group(2)), match.group(5))

    def _cached_paths(self, cache_dir: str) -> List[str]:
        return glob.glob(os.path.join(cache_dir, self.folder, "epsg*", "*", "*.tif"))

    def _cached(self, grid: Grid, cache_dir: str) -> list:
        blocks = []
        for path in self._cached_paths(cache_dir):
            block = self._block_of(path)
            if block is None:
                continue
            s = block.size
            left, bottom, right, top = transform_bounds(block.crs, grid.crs, block.i * s, block.j * s,
                                                        (block.i + 1) * s, (block.j + 1) * s)
            g_left, g_bottom, g_right, g_top = grid.bounds
            if left < g_right and right > g_left and bottom < g_top and top > g_bottom:
                blocks.append(block)
        return sorted(blocks, key=lambda b: (not _same_crs(b.crs, grid.crs), -b.year, b.crs, b.j, b.i))

    def _others(self, cache_dir: str, block: Block) -> List[str]:
        """The cached files of the same block made from other sets of files."""
        folder = os.path.dirname(self.tile_path(cache_dir, block))
        found = [os.path.join(folder, n) for n in os.listdir(folder)] if os.path.isdir(folder) else []
        return [p for p in found if (b := self._block_of(p)) is not None and (b.i, b.j) == (block.i, block.j)
                and b.key != block.key]

    def _prune(self, cache_dir: str, sheets: List[Block]) -> int:
        older = []
        for block in sheets:
            if os.path.exists(self.tile_path(cache_dir, block)):
                older += self._others(cache_dir, block)
        cache.forget(cache_dir, older)
        return len(older)

    def _block_wgs84(self, block: Block) -> Tuple[float, float, float, float]:
        s = block.size
        return transform_bounds(block.crs, "EPSG:4326", block.i * s, block.j * s, (block.i + 1) * s, (block.j + 1) * s)

    def update_index_tile(self, cache_dir: str, lat: float, lon: float) -> Tuple[int, int, int]:
        """Ask the index again for one tile and read again the cached blocks (centred in it)
        over which the set of files changed (the older files are deleted)."""
        self._index_tile(cache_dir, lat, lon, "all")
        todo = []
        for path in self._cached_paths(cache_dir):
            block = self._block_of(path)
            if block is None:
                continue
            west, south, east, north = self._block_wgs84(block)
            if not (lat <= (south + north) / 2 < lat + osm.TILE_DEG and lon <= (west + east) / 2 < lon + osm.TILE_DEG):
                continue
            s = block.size
            inner = (block.i * s + s / 4, block.j * s + s / 4, (block.i + 1) * s - s / 4, (block.j + 1) * s - s / 4)
            now = [b for b in self._blocks(self._items(cache_dir, west, south, east, north), {block.crs: inner})
                   if (b.i, b.j) == (block.i, block.j)]
            if now and now[0].key != block.key and not os.path.exists(self.tile_path(cache_dir, now[0])):
                todo.append(now[0])
        received = sum(self._download_and_record(b, cache_dir) for b in todo)
        return len(todo), received / 1e6, self._prune(cache_dir, todo)

    def has_data_near(self, lat: float, lon: float, cache_dir: str, metres: float = 2000) -> Optional[bool]:
        try:
            items = self._items(cache_dir, lon, lat, lon, lat)
        except requests.RequestException:
            return None
        for item in items:
            x, y = rio.warp.transform("EPSG:4326", item["crs"], [lon], [lat])
            left, bottom, right, top = item["extent"]
            if left - metres <= x[0] <= right + metres and bottom - metres <= y[0] <= top + metres:
                return True
        return False

    def tile_url(self, tile) -> Optional[str]:
        return tile.files[0][1] if tile.files else None

    def download_tile(self, tile, path: str) -> Fetched:
        px = round(tile.size / self.native_res)
        transform = from_origin(tile.i * tile.size, (tile.j + 1) * tile.size, self.native_res, self.native_res)
        return _read_block(tile.files, tile.crs, transform, (px, px), path, self.description)

    def used(self, grid: Grid, cache_dir: str) -> Dict:
        """With the files the blocks were read from and the years of their surveys (the
        newest year of a block's name may come from a file without data in it)."""
        info = super().used(grid, cache_dir)
        names, years = set(), set()
        for path in self.files(grid, cache_dir):
            with rio.open(path) as src:
                tags = src.tags()
            names.update(n for n in tags.get("sources", "").split(",") if n)
            years.update(int(y) for y in tags.get("years", "").split(",") if y)
        info["source_files"] = sorted(names)
        info["survey_years"] = [min(years), max(years)] if years else info["survey_years"]
        return info


def _utm_zone_of(box: Dict, extent: List[float]) -> Optional[int]:
    """The UTM zone of a 1 m tile whose file name does not say it: the zone (of the centre of
    its box, or a neighbour) in which the tile's extent lands on its box."""
    lon, lat = (box["minX"] + box["maxX"]) / 2, (box["minY"] + box["maxY"]) / 2
    zone = int((lon + 180) // 6) + 1
    cx, cy = (extent[0] + extent[2]) / 2, (extent[1] + extent[3]) / 2
    for z in (zone, zone - 1, zone + 1):
        if 1 <= z <= 23:
            x, y = rio.warp.transform(f"EPSG:{26900 + z}", "EPSG:4326", [cx], [cy])
            if box["minX"] - 0.01 <= x[0] <= box["maxX"] + 0.01 and box["minY"] - 0.01 <= y[0] <= box["maxY"] + 0.01:
                return z
    return None


class Usgs1mBlocks(TnmBlocks):
    """3DEP's standard 1 m DEM: 10 km tiles, USGS_1M_<zone>_x<E / 10 km>y<N of the top / 10 km>_
    <project>.tif, in NAD83 / UTM of their zone (a tile crossing two zones is delivered in
    both), with 6 pixels of overlap around; older names give no zone (USGS_one_meter_x..y..)."""

    def _item(self, product: Dict) -> Optional[Dict]:
        url = product.get("downloadURL") or ""
        name = url.rsplit("/", 1)[-1]
        xy = re.search(r"_x(\d+)y(\d+)_", name, re.I)
        if not name.lower().endswith(".tif") or not xy:
            return None
        x, y = int(xy.group(1)), int(xy.group(2))
        extent = [x * 10000, (y - 1) * 10000, (x + 1) * 10000, y * 10000]
        zone = re.match(r"USGS_1M_(\d+)_", name, re.I)
        zone = int(zone.group(1)) if zone else _utm_zone_of(product.get("boundingBox") or {}, extent)
        if zone is None:
            return None
        published = product.get("publicationDate") or ""
        return {"name": name[:-4], "url": url, "crs": f"EPSG:{26900 + zone}", "extent": extent,
                "year": _survey_year(url, published), "published": published}


class IfsarBlocks(TnmBlocks):
    """Alaska's 5 m IfSAR DTM: cells of varying extent in Alaska Albers (EPSG:3338), on whole
    5 m pixels. Their extent is the box of the API projected (a little too large: a file
    without data in a block costs one opening)."""
    crs = "EPSG:3338"

    def _item(self, product: Dict) -> Optional[Dict]:
        url, box = product.get("downloadURL") or "", product.get("boundingBox")
        if not url.lower().endswith(".tif") or not box:
            return None
        extent = transform_bounds("EPSG:4326", self.crs, box["minX"], box["minY"], box["maxX"], box["maxY"],
                                  densify_pts=21)
        published = product.get("publicationDate") or ""
        return {"name": url.rsplit("/", 1)[-1][:-4], "url": url, "crs": self.crs, "extent": list(extent),
                "year": _survey_year(url, published), "published": published}


class DegreeBlocks(Layer):
    """Blocks of 1°×1° cloud-optimized GeoTIFFs at a fixed URL per tile (3DEP's seamless 1/3″
    DEM, NAD83 geographic), read by HTTP range requests: a tile is about 450 MB, a block of
    0.1° about 2 MB. Block (i, j) covers [i, i + 1) × [j, j + 1) tenths of a degree, cached in
    <folder>/<tile>/<i>_<j>.tif; a tile the service does not have (404: sea, Canada) is missing.
    """

    def __init__(self, description: str, folder: str, url: Callable[[str], str], crs: str,
                 px_per_degree: int, native_res: float, bytes_per_tile: float = 2e6, credit: Optional[Credit] = None):
        self.description, self.folder, self.url, self.crs = description, folder, url, crs
        self.px_per_degree, self.native_res, self._bytes = px_per_degree, native_res, bytes_per_tile
        self.credit = credit

    @staticmethod
    def tile_name(i: int, j: int) -> str:
        """The 1° tile of a block, named by its north-west corner (n38w120: 37-38° N, 120-119° W)."""
        return f"{_hemi(j // 10 + 1, 'n', 's', 2)}{_hemi(i // 10, 'e', 'w', 3)}"

    def tiles(self, grid: Grid) -> list:
        west, south, east, north = transform_bounds(grid.crs, self.crs, *grid.bounds)
        shape = None
        if grid.area_shape is not None:
            from area import project
            shape = project(grid.area_shape, grid.crs, self.crs)
        found = []
        for j in range(math.floor(south * 10), math.floor(north * 10) + 1):
            for i in range(math.floor(west * 10), math.floor(east * 10) + 1):
                if shape is not None:
                    from shapely.geometry import box
                    if not shape.intersects(box(i / 10, j / 10, (i + 1) / 10, (j + 1) / 10)):
                        continue
                found.append((i, j))
        return found

    def tile_path(self, cache_dir: str, tile) -> str:
        return os.path.join(cache_dir, self.folder, self.tile_name(*tile), f"{tile[0]}_{tile[1]}.tif")

    def tile_of(self, path: str):
        match = re.fullmatch(rf"{re.escape(self.folder)}/([ns]\d\d[ew]\d\d\d)/(-?\d+)_(-?\d+)\.tif", path)
        if not match or self.tile_name(int(match.group(2)), int(match.group(3))) != match.group(1):
            return None
        return int(match.group(2)), int(match.group(3))

    def bounds_wgs84(self, tile) -> Tuple[float, float, float, float]:
        i, j = tile
        return i / 10, j / 10, (i + 1) / 10, (j + 1) / 10

    def bytes_per_tile(self) -> float:
        return self._bytes

    def tile_url(self, tile) -> Optional[str]:
        return self.url(self.tile_name(*tile))

    def download_tile(self, tile, path: str) -> Fetched:
        i, j = tile
        name = self.tile_name(i, j)
        px = self.px_per_degree // 10
        transform = from_origin(i / 10, (j + 1) / 10, 1 / self.px_per_degree, 1 / self.px_per_degree)
        return _read_block([(name, self.url(name), 0)], self.crs, transform, (px, px), path, self.description)


def _sheet_order(sheet: Sheet) -> tuple:
    """Newer year first; within a year, the later publication (higher numbers in the file name)."""
    return (sheet.year,) + tuple(int(n) for n in re.findall(r"\d+", sheet.name)[:2])


def _xyz_grid(data: bytes, res: float) -> Tuple[np.ndarray, Affine]:
    """A raster from "x y z" lines of a regular grid (cell centres)."""
    xyz = np.loadtxt(io.BytesIO(data), dtype=np.float64, ndmin=2)  # ~30x faster than np.fromstring
    x, y, z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    left, top = x.min(), y.max()
    cols = np.rint((x - left) / res).astype(np.int64)
    rows = np.rint((top - y) / res).astype(np.int64)
    band = np.full((rows.max() + 1, cols.max() + 1), np.nan, dtype=np.float32)
    band[rows, cols] = z
    return band, from_origin(left - res / 2, top + res / 2, res, res)


class FileLayer(Layer):
    """A local raster file; nothing to download."""

    def __init__(self, description: str, path: str):
        self.description, self.path = description, path
        with rio.open(path) as src:
            crs = src.crs
            res = abs(src.res[0])
            self.native_res = res * 111320 if crs is not None and crs.is_geographic else res

    def tiles(self, grid: Grid):
        return [self.path]

    def tile_path(self, cache_dir: str, tile) -> str:
        return tile

    def missing(self, grid, cache_dir, update):
        return []

    def used(self, grid: Grid, cache_dir: str) -> Dict:
        return {"layer": self.description, "file": os.path.abspath(self.path)}


@dataclass
class OrthoService:
    """A WMS with orthophotos, requested in WGS84, and/or tiles in Web Mercator (see orthophoto.py)."""
    name: str
    url: Optional[str]  # the WMS; None: the base tiles only
    layer: str
    folder: str      # in the cache; tiles go to <folder>/wm256/{z}/{x}/{y}.jpg
    attribution: str
    # Tiles ({z}/{x}/{y} in Web Mercator, up to base_max_zoom, enlarged above it) with full
    # coverage under this service, where it has no imagery and answers with white
    # (geoportal's HighResolution covers only sheets of 10 cm or finer)
    base_url: Optional[str] = None
    base_max_zoom: int = 19
    # Fair use: detailed tiles (zoom 17 and more) for at most this many groups, circles too
    # (--ortho-detail overrides it)
    detail: Optional[int] = None

    @property
    def ttl_days(self) -> Optional[int]:
        """Days between update checks; None for a dated orthophoto that never changes."""
        return cache.TTL_DAYS["live"] if "/live" in self.folder else None


@dataclass
class Source:
    id: str
    name: str
    description: str
    nmt: Layer
    nmpt: Optional[Layer] = None
    countries: Tuple[str, ...] = ()
    crs: Optional[str] = None       # working CRS; None = UTM zone of the search centre
    default_resolution: Optional[float] = None  # None: the terrain data's own, at least MIN_RESOLUTION
    ortho: Optional[OrthoService] = None
    note: str = ""                  # licence or quality note printed at the start
    fallback: Optional[str] = "europe"  # source that fills gaps in the data on land
    # Sources that fill, in turn, what the fallback left (Alaska: its IfSAR, then 1/3″)
    more_fallbacks: Tuple[str, ...] = ()
    bbox: Optional[Tuple[float, float, float, float]] = None  # (south, north, west, east) for --source auto
    grid_origin: Tuple[float, float] = (0.0, 0.0)  # pixel edges of the data in the working CRS (grid.make_grid)
    # Licences and notices of the data; a source with one credit gives it to its layers,
    # one with more sets each layer's own (europe: FABDEM and Copernicus)
    credits: Tuple[Credit, ...] = ()
    # The working CRS of a centre when crs is None (the USA: NAD83 / UTM, the CRS of the 1 m tiles)
    local_crs: Callable[[float, float], str] = utm_crs
    # The source --source auto takes instead when the terrain layer has no data near the
    # centre (Alaska: its IfSAR where there is no 1 m lidar)
    instead_without_data: Optional[str] = None

    def __post_init__(self):
        if self.default_resolution is None:
            self.default_resolution = max(float(self.nmt.native_res), MIN_RESOLUTION)
        for layer in (self.nmt, self.nmpt):
            if layer is not None and layer.credit is None and len(self.credits) == 1:
                layer.credit = self.credits[0]

    @property
    def licence(self) -> str:
        """The licences of the source's data ("" for local files)."""
        return "; ".join(dict.fromkeys(f"{c.data}: {c.licence}" if len(self.credits) > 1 else c.licence
                                       for c in self.credits))

    @property
    def attribution(self) -> str:
        """The notice its data must be shown with ("" for local files)."""
        return "; ".join(dict.fromkeys(c.attribution for c in self.credits))

    def covers(self, lat: float, lon: float) -> bool:
        if self.bbox is None:
            return True
        south, north, west, east = self.bbox
        return south <= lat <= north and west <= lon <= east

    def working_crs(self, lat: float, lon: float) -> str:
        return self.crs or self.local_crs(lat, lon)

    @property
    def fallbacks(self) -> List[str]:
        """The sources that fill gaps on land, in turn."""
        return [self.fallback, *self.more_fallbacks] if self.fallback else []


GEOPORTAL = "https://mapy.geoportal.gov.pl/wss/service/PZGIK"
MALTA = "https://malta.coverage.wetransform.eu"
SPAIN_MDT = "https://servicios.idee.es/wcs-inspire/mdt"
SPAIN_MDS = "https://wcs-mds.idee.es/mds"
SPAIN_ORTHO = OrthoService("Orthophoto PNOA (IGN Spain)", "https://www.ign.es/wms-inspire/pnoa-ma",
                           "OI.OrthoimageCoverage", "spain/ortho/live", "PNOA © IGN")
SWISSTOPO_STAC = "https://data.geo.admin.ch/api/stac/v1"
# SWISSIMAGE in the map's own tiling (CDN), imagery up to zoom 20; there is no WMS step.
# Fewer detailed tiles than elsewhere: swisstopo's terms ask for no bulk downloads.
SWISSIMAGE = OrthoService("SWISSIMAGE (swisstopo)", None, "", "switzerland/ortho/live", "© swisstopo",
                          base_url="https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swissimage/default/current"
                                   "/3857/{z}/{x}/{y}.jpeg", base_max_zoom=20, detail=20)
# Our own copy of ÚGKK SR's national ALS models (tools/sk_tiles.py): only the local mirror
# (FINDALINE_MIRROR) for now; the Hugging Face base goes here once the tiles are uploaded
SLOVAK_TILES: List[str] = []
ZBGIS_ORTHO = OrthoService("Orthophoto ZBGIS (ÚGKK SR)",
                           "https://zbgisws.skgeodesy.sk/zbgis_ortofoto_wms/service.svc/get", "1",
                           "slovakia/ortho/live", "© ÚGKK SR")
# USGS 3DEP (the USA): blocks read from the files on S3 by HTTP range requests; the 1 m
# DEM per project (newest first), Alaska's IfSAR DTM, and the 1/3″ DEM for the gaps
USGS_3DEP = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation"
USGS_1M = Usgs1mBlocks("3DEP 1 m DEM (terrain)", "Digital Elevation Model (DEM) 1 meter", "dem1m",
                       "usa/3dep-1m", native_res=1.0, block_m=1000, bytes_per_tile=2e6)
USGS_1M.credit = USGS_1M_CREDIT
USGS_IFSAR = IfsarBlocks("3DEP Alaska IfSAR 5 m DTM (terrain)", "Alaska IFSAR 5 meter DEM", "ifsar5m",
                         "usa/ifsar-5m", native_res=5.0, block_m=5000, bytes_per_tile=2e6)
USGS_IFSAR.credit = USGS_IFSAR_CREDIT
USGS_13 = DegreeBlocks("3DEP 1/3 arc-second DEM (terrain)", "usa/3dep-13/live",
                       lambda name: f"{USGS_3DEP}/13/TIFF/current/{name}/USGS_13_{name}.tif",
                       "EPSG:4269", px_per_degree=10800, native_res=10.0, credit=USGS_13_CREDIT)
# NAIP in the map's own tiling; the cached tiles end at zoom 16 (enlarged above it)
USGS_ORTHO = OrthoService("USGS Imagery (USDA NAIP)", None, "", "usa/ortho/live",
                          "USGS The National Map: Orthoimagery (USDA NAIP)",
                          base_url="https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer"
                                   "/tile/{z}/{y}/{x}", base_max_zoom=16)
USA_NOTE = ("USGS 3DEP data, public domain. There is no surface model (trees, buildings) for the USA yet: "
            "lines over trees or buildings are not recognised. The orthophoto ends at zoom 16.")
# Alaska for --source auto (the Aleutians west of 180° are left to the usa source)
ALASKA_BOX = (51.0, 71.5, -180.0, -129.9)
FABDEM = "https://huggingface.co/buckets/links-ads/fabdem/resolve/tiles"
COPERNICUS = "https://copernicus-dem-30m.s3.amazonaws.com"


def _fabdem_url(lat: int, lon: int) -> str:
    g_lat, g_lon = lat // 10 * 10, lon // 10 * 10
    group = (f"{_hemi(g_lat, 'N', 'S', 2)}{_hemi(g_lon, 'E', 'W', 3)}-"
             f"{_hemi(g_lat + 10, 'N', 'S', 2)}{_hemi(g_lon + 10, 'E', 'W', 3)}")
    return f"{FABDEM}/{group}_FABDEM_V1-2/{_hemi(lat, 'N', 'S', 2)}{_hemi(lon, 'E', 'W', 3)}_FABDEM_V1-2.tif"


def _copernicus_url(lat: int, lon: int, aux: Optional[str] = None) -> str:
    """URL of a Copernicus DEM tile, or of one of its auxiliary files (e.g. aux="WBM")."""
    name = f"Copernicus_DSM_COG_10_{_hemi(lat, 'N', 'S', 2)}_00_{_hemi(lon, 'E', 'W', 3)}_00"
    if aux:
        return f"{COPERNICUS}/{name}_DEM/AUXFILES/{name}_{aux}.tif"
    return f"{COPERNICUS}/{name}_DEM/{name}_DEM.tif"


# The 2012 Malta services are georeferenced about 56 m east and 196 m north of the
# true position: measured against Copernicus DEM and the 2018 survey at Dingli
# (-56, -196 m), Valletta (-50, -190) and Gozo (-60, -190).
MALTA_2012_SHIFT = (-56.0, -196.0)


def _spain(canary: bool) -> Source:
    """IGN Spain: MDT05 (terrain) and MDS05 (surface), 5 m from PNOA-LiDAR, via WCS.

    The peninsula and the Balearic Islands are in ETRS89 / UTM 30N (extended zone),
    the Canary Islands in REGCAN95 / UTM 28N; MDS05 covers both in UTM 30N. The ASCII
    grid format keeps centimetres (the GeoTIFF output is rounded to whole metres).
    """
    crs, coverage = ("EPSG:4083", "Elevacion4083_5") if canary else ("EPSG:25830", "Elevacion25830_5")
    where = "Canary Islands" if canary else "Spain"
    return Source(
        id="spain_canarias" if canary else "spain", name=f"IGN {where} (MDT05/MDS05)",
        description=f"IGN Spain PNOA-LiDAR, MDT05 terrain and MDS05 surface, 5 m ({where})",
        nmt=WcsTiles(f"MDT05 (terrain) {where}", SPAIN_MDT, coverage, crs,
                     "spain/mdt05/live", native_res=5.0, fetch_res=5.0,
                     fmt="application/asc", bytes_per_px=8),
        # MDS05's grid is 5.0045 m; tiles are resampled to 5 m on download
        nmpt=WcsTiles(f"MDS05 (trees, buildings) {where}", SPAIN_MDS, "mds05", "EPSG:25830",
                      "spain/mds05/live", native_res=5.0, fetch_res=5.0, fmt="application/asc", bytes_per_px=8),
        countries=("es",), crs=crs, ortho=SPAIN_ORTHO, credits=(IGN_CREDIT,),
        bbox=(27.0, 29.6, -18.6, -13.0) if canary else None,
        note="IGN Spain data is available at 5 m: lines shorter than about 30 m are not reliable.")


def _malta(year: int, note: str = "") -> Source:
    crs = "EPSG:25833" if year == 2012 else "EPSG:32633"
    layer = lambda kind, desc: WcsTiles(
        f"{desc} Malta {year}", f"{MALTA}/{kind}_1m_{year}/ows", f"{kind}_1m_{year}", crs,
        f"malta/{kind}/{year}", native_res=1.0 if year == 2012 else 0.15, fetch_res=1.0, axes=("E", "N"),
        shift=MALTA_2012_SHIFT if year == 2012 else (0.0, 0.0), bytes_per_px=0.8)  # measured: ~3 MB per tile
    return Source(
        id="malta" if year == 2018 else f"malta{year}", name=f"Malta {year} (MSDI)",
        description=f"Malta Planning Authority LiDAR {year}, DTM and DSM 1 m",
        nmt=layer("dtm", "DTM (terrain)"), nmpt=layer("dsm", "DSM (trees, buildings)"),
        countries=("mt",) if year == 2018 else (), crs=crs,
        fallback="malta" if year == 2012 else "europe", credits=(MALTA_CREDITS[year],),
        ortho=OrthoService("Orthophoto Malta 2018 (MSDI)", f"{MALTA}/wms/ortho_2018/ows",
                           "OI.OrthoimageCoverage", "malta/ortho/2018", "Planning Authority Malta"),
        note=note)


SOURCES: Dict[str, Source] = {s.id: s for s in [
    Source(
        id="geoportal", name="geoportal.gov.pl",
        description="GUGiK (Poland): NMT and NMPT sheets of 1 m or finer, newest survey, PL-EVRF2007-NH",
        # Sheet files from opendata.geoportal.gov.pl, found through the EVRF2007 sheet
        # indexes: about 2 s per sheet, while the WCS spends 30-50 s generating each ASCII
        # tile, its downsampling loses data at coarse resolutions (Tatras at 5 m) and its
        # PL-KRON86-NH coverages hold surveys up to 2019 only.
        nmt=SheetTiles("NMT (digital terrain model)", f"{GEOPORTAL}/NumerycznyModelTerenuEVRF2007/WFS/Skorowidze",
                       "SkorowidzNMT", "poland/nmt", native_res=1.0),
        nmpt=SheetTiles("NMPT (digital surface model: trees, buildings)",
                        f"{GEOPORTAL}/NumerycznyModelPokryciaTerenuEVRF2007/WFS/Skorowidze",
                        "SkorowidzNMPT", "poland/nmpt", native_res=1.0),
        countries=("pl",), crs="EPSG:2180", grid_origin=(0.5, 0.5), credits=(GUGIK_CREDIT,),
        ortho=OrthoService("Orthophoto (geoportal.gov.pl)", f"{GEOPORTAL}/ORTO/WMS/HighResolution",
                           "Raster", "poland/ortho/live", "GUGiK",
                           # the WMS of StandardResolution answers about half of the requests
                           # with 404 (2026-10-25); its WMTS answers all
                           base_url=f"{GEOPORTAL}/ORTO/WMTS/StandardResolution?SERVICE=WMTS&REQUEST=GetTile"
                                    "&VERSION=1.0.0&LAYER=ORTOFOTOMAPA&STYLE=default&FORMAT=image/jpeg"
                                    "&TILEMATRIXSET=EPSG:3857&TILEMATRIX=EPSG:3857:{z}&TILEROW={y}&TILECOL={x}")),
    Source(
        id="swisstopo", name="swisstopo (swissALTI3D / swissSURFACE3D)",
        description="swisstopo (Switzerland, Liechtenstein): swissALTI3D terrain 0.5/2 m and swissSURFACE3D "
                    "Raster surface 0.5 m, newest survey per 1 km tile, LV95 / LN02",
        # 1 km COG tiles from the STAC API (swisstopo's download service): no elevation WCS
        # exists. The 2 m terrain files serve grids of 2 m and coarser; the surface model
        # is published at 0.5 m only (about 17 MB per km²).
        nmt=StacTiles("swissALTI3D (terrain)", SWISSTOPO_STAC, "ch.swisstopo.swissalti3d",
                      "switzerland/swissalti3d", "EPSG:2056", sizes={0.5: 20e6, 2.0: 1.3e6}),
        nmpt=StacTiles("swissSURFACE3D Raster (trees, buildings)", SWISSTOPO_STAC,
                       "ch.swisstopo.swisssurface3d-raster", "switzerland/swisssurface3d", "EPSG:2056",
                       sizes={0.5: 17e6}),
        countries=("ch", "li"), crs="EPSG:2056", ortho=SWISSIMAGE, credits=(SWISSTOPO_CREDIT,),
        note="Data © swisstopo."),
    Source(
        id="slovakia", name="ÚGKK SR (DMR 5.0 / DMP 1.0)",
        description="ÚGKK SR (Slovakia): ALS 2017-2023, DMR 5.0 terrain and DMP 1.0 surface, 1 m, "
                    "S-JTSK [JTSK03] / Bpv",
        # Published only as one ZIP of 155-200 GB per model: our own 2 km tiles of them
        # (tools/sk_tiles.py), on the national grid, pixel edges on whole metres
        nmt=GridTiles("DMR 5.0 (terrain)", [f"{base}/dmr5/1m" for base in SLOVAK_TILES], "EPSG:8353",
                      "slovakia/dmr5/5.0/epsg8353-1m-2km", 2000, 1.0, 4.8e6, mirror="slovakia/dmr5/1m"),
        nmpt=GridTiles("DMP 1.0 (trees, buildings)", [f"{base}/dmp1/1m" for base in SLOVAK_TILES], "EPSG:8353",
                       "slovakia/dmp1/1.0/epsg8353-1m-2km", 2000, 1.0, 5.7e6, mirror="slovakia/dmp1/1m"),
        countries=("sk",), crs="EPSG:8353", ortho=ZBGIS_ORTHO, credits=(UGKK_CREDIT,),
        note="Data © ÚGKK SR, CC BY 4.0 (laser scanning 2017-2023)."),
    _malta(2018, note="Malta 2018 heights are published in whole metres, which makes slopes and "
                      "clearances coarser; --source malta2012 has centimetre precision but gaps "
                      "along the coast (filled from 2018)."),
    _malta(2012),
    _spain(canary=True),
    _spain(canary=False),
    Source(
        id="alaska", name="USGS 3DEP (1 m DEM, Alaska)",
        description="USGS 3DEP (Alaska): 1 m lidar DEM where there is one, newest survey first; gaps from "
                    "the 5 m IfSAR DTM, then the 1/3 arc-second DEM; NAD83 / NAVD88",
        nmt=USGS_1M, countries=("us",), bbox=ALASKA_BOX, local_crs=nad83_utm_crs, ortho=USGS_ORTHO,
        fallback="alaska_ifsar", more_fallbacks=("usgs10m",), instead_without_data="alaska_ifsar",
        credits=(USGS_1M_CREDIT,), note=USA_NOTE),
    Source(
        id="alaska_ifsar", name="USGS 3DEP (Alaska IfSAR 5 m)",
        description="USGS 3DEP (Alaska): IfSAR DTM 5 m (radar, 2012-2019), gaps from the 1/3 arc-second DEM",
        nmt=USGS_IFSAR, local_crs=nad83_utm_crs, ortho=USGS_ORTHO, fallback="usgs10m",
        credits=(USGS_IFSAR_CREDIT,),
        note=USA_NOTE + " Alaska's IfSAR is a radar model at 5 m: lines shorter than about 30 m are not reliable."),
    Source(
        id="usa", name="USGS 3DEP (1 m DEM)",
        description="USGS 3DEP (USA, Puerto Rico, US Virgin Islands): 1 m lidar DEM, newest survey first, "
                    "NAD83 / UTM, NAVD88; gaps from the 1/3 arc-second DEM",
        # 10 km files of 10-450 MB per project: blocks of 1 km read by HTTP range requests
        nmt=USGS_1M, countries=("us", "pr", "vi"), local_crs=nad83_utm_crs, ortho=USGS_ORTHO,
        fallback="usgs10m", credits=(USGS_1M_CREDIT,), note=USA_NOTE),
    Source(
        id="usgs10m", name="USGS 3DEP (1/3 arc-second DEM)",
        description="USGS 3DEP seamless 1/3 arc-second DEM (about 10 m): the USA, Hawaii and the territories",
        nmt=USGS_13, local_crs=nad83_utm_crs, ortho=USGS_ORTHO, credits=(USGS_13_CREDIT,),
        note="The 1/3 arc-second DEM has pixels of about 10 m: lines shorter than about 60 m are not reliable."),
    Source(
        id="europe", name="FABDEM + Copernicus DEM",
        description="FABDEM V1-2 (terrain, trees and buildings removed) and Copernicus GLO-30 (surface), 30 m",
        nmt=DegreeTiles("FABDEM (terrain)", "fabdem/v1-2", _fabdem_url, native_res=30, bytes_per_tile=20e6,
                        credit=FABDEM_CREDIT),
        nmpt=DegreeTiles("Copernicus DEM GLO-30 (surface)", "copernicus30/2021/dem", _copernicus_url,
                         native_res=30, bytes_per_tile=20e6, credit=COPERNICUS_CREDIT),
        fallback=None, credits=(FABDEM_CREDIT, COPERNICUS_CREDIT),
        note="FABDEM is licensed for non-commercial use only (CC BY-NC-SA 4.0): results made from it may "
             "be shared only under the same terms. At 30 m only long lines can be found reliably."),
    Source(
        id="nasadem", name="NASADEM (OpenTopography)",
        description="NASADEM 30 m (SRTM, includes some vegetation), from the OpenTopography API",
        nmt=OpenTopographyTiles("NASADEM", "nasadem/v1", "NASADEM"), credits=(NASADEM_CREDIT,),
        note="NASADEM is a radar surface model at 30 m: only long lines can be found reliably."),
]}

# Rough boxes used when the country cannot be looked up (offline)
OFFLINE_BOXES = [("mt", (35.78, 36.10, 14.15, 14.60)), ("pl", (49.0, 54.9, 14.1, 24.2)),
                 ("es", (27.0, 43.9, -18.6, 4.4)), ("sk", (47.7, 49.65, 16.8, 22.6)),
                 ("ch", (45.8, 47.85, 5.9, 10.5)), ("us", (24.4, 49.4, -125.0, -66.9)), ("us", ALASKA_BOX),
                 ("pr", (17.8, 18.6, -67.4, -65.2))]


def country_code(lat: float, lon: float, cache_dir: str) -> Optional[str]:
    """ISO country code of a point (OpenStreetMap), or a rough guess when offline."""
    try:
        code = osm.reverse(lat, lon, cache_dir, zoom=3).get("address", {}).get("country_code")
        if code:
            return code.lower()
    except (requests.RequestException, ValueError):
        pass
    for code, (s, n, w, e) in OFFLINE_BOXES:
        if s <= lat <= n and w <= lon <= e:
            return code
    return None


def choose_source(name: str, lat: float, lon: float, cache_dir: str, elevation_file: Optional[str] = None,
                  surface_file: Optional[str] = None) -> Tuple[Source, str]:
    """The source to use and why."""
    if elevation_file or name == "files":
        if not elevation_file:
            raise ValueError("--source files needs --elevation-file")
        # File names only: the description goes into the run's files (runs.js, lines.geojson, ATTRIBUTION.txt)
        source = Source(id="files", name="local files", description=f"local files: {os.path.basename(elevation_file)}"
                        + (f", {os.path.basename(surface_file)}" if surface_file else ""),
                        nmt=FileLayer("Terrain file", elevation_file),
                        nmpt=FileLayer("Surface file", surface_file) if surface_file else None)
        with rio.open(elevation_file) as src:
            if src.crs is not None and src.crs.is_projected and src.crs.linear_units in ("metre", "meter"):
                source.crs = src.crs.to_string()
                # The grid is in the file's CRS: put its edges on the file's pixel edges
                source.grid_origin = (src.transform.c % abs(src.res[0]), src.transform.f % abs(src.res[1]))
        native = source.nmt.native_res
        source.default_resolution = float(round(native)) if native >= 5 else max(MIN_RESOLUTION, round(native, 1))
        return source, "--elevation-file given"
    if name != "auto":
        why = source_unavailable(SOURCES[name])
        if why:
            raise ValueError(why)
        return SOURCES[name], f"--source {name}"
    code = country_code(lat, lon, cache_dir)
    for source in SOURCES.values():
        if code and code in source.countries and source.covers(lat, lon):
            why = source_unavailable(source)
            if why:  # e.g. our own Slovak tiles without a copy set up
                return SOURCES["europe"], f"the centre is in {code.upper()}, but {why}"
            if source.instead_without_data and source.nmt.has_data_near(lat, lon, cache_dir) is False:
                return (SOURCES[source.instead_without_data],
                        f"the centre is in {code.upper()}, but {source.id} has no terrain data near it")
            return source, f"the centre is in {code.upper()}"
    return SOURCES["europe"], f"no national source for {code.upper() if code else 'this place'}"


def source_unavailable(source: Source) -> Optional[str]:
    """Why a source cannot be used here, or None."""
    for layer in (source.nmt, source.nmpt):
        why = layer.unavailable() if layer else None
        if why:
            return why
    return None


# What the layers prepared in this process came from (Layer.used), saved with the run
used_data: List[Dict] = []


def prepare_layer(layer: Layer, grid: Grid, cache_dir: str, out_path: str, label: str,
                  update: Optional[str] = None, download: bool = True) -> str:
    """Download what is missing (unless done already: download False) and bring the layer
    onto the working grid (.npy)."""
    if download:
        layer.download(grid, cache_dir, update, label)
    tiles = (layer.tiles(grid) or []) if layer.folder else []  # local files are not in the cache
    empty = sum(cache.state(cache_dir, layer.tile_path(cache_dir, t)) == "missing" for t in tiles)
    if empty:  # normal over the sea; on land a sign of a lost tile (see the regression tests)
        print(f"{layer.description}: {empty} of {len(tiles)} tiles without data (the service has none there)")
    build_mosaic(layer.files(grid, cache_dir), grid, out_path, label, shift=layer.shift)
    used_data.append(layer.used(grid, cache_dir))
    return out_path


def build_mosaic(files: List[str], grid: Grid, out_path: str, label: str,
                 categorical: bool = False, shift: Tuple[float, float] = (0.0, 0.0)) -> np.ndarray:
    """Stitch rasters into a memory-mapped array covering the grid.

    Tiles already on the grid are copied; others are reprojected, averaging when
    the data is finer than the grid and interpolating bilinearly when it is coarser
    (categorical rasters, such as a water mask, use the nearest value instead).
    """
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    mosaic = np.lib.format.open_memmap(out_path, mode="w+", dtype=np.float32,
                                       shape=(grid.height, grid.width))
    mosaic[:] = np.nan
    progress.compute.start(f"stitching {label}", len(files), unit="tile")
    for path in files:
        progress.compute.update(1)
        with rio.open(path) as src:
            _place(src, grid, mosaic, categorical, shift)
    mosaic.flush()
    progress.compute.finish()
    return mosaic


def _place(src, grid: Grid, mosaic: np.ndarray, categorical: bool = False,
           shift: Tuple[float, float] = (0.0, 0.0)):
    clean = (lambda band: band.astype(np.float32)) if categorical else _clean
    grid_crs = rio.crs.CRS.from_user_input(grid.crs)
    src_crs = src.crs or grid_crs
    moved = Affine.translation(*shift)
    src_bounds = rio.coords.BoundingBox(src.bounds.left + shift[0], src.bounds.bottom + shift[1],
                                        src.bounds.right + shift[0], src.bounds.top + shift[1])
    same_grid = src_crs == grid_crs and shift == (0.0, 0.0) and math.isclose(abs(src.res[0]), grid.res) \
        and math.isclose(abs(src.res[1]), grid.res)
    if same_grid:
        row_off = int(round((grid.top - src.bounds.top) / grid.res))
        col_off = int(round((src.bounds.left - grid.left) / grid.res))
        if abs(row_off * grid.res - (grid.top - src.bounds.top)) < grid.res / 100:
            r0, c0 = max(row_off, 0), max(col_off, 0)
            r1, c1 = min(row_off + src.height, grid.height), min(col_off + src.width, grid.width)
            if r0 < r1 and c0 < c1:
                window = rio.windows.Window(c0 - col_off, r0 - row_off, c1 - c0, r1 - r0)
                values = clean(src.read(1, window=window, masked=True).astype(np.float32).filled(np.nan))
                target = mosaic[r0:r1, c0:c1]
                mosaic[r0:r1, c0:c1] = np.where(np.isnan(target), values, target)
            return

    # Part of the grid covered by this raster
    left, bottom, right, top = transform_bounds(src_crs, grid.crs, *src_bounds)
    c0 = max(0, int(math.floor((left - grid.left) / grid.res)))
    c1 = min(grid.width, int(math.ceil((right - grid.left) / grid.res)))
    r0 = max(0, int(math.floor((grid.top - top) / grid.res)))
    r1 = min(grid.height, int(math.ceil((grid.top - bottom) / grid.res)))
    if r0 >= r1 or c0 >= c1:
        return
    src_res = abs(src.res[0]) * (111320 if src_crs.is_geographic else 1)
    if categorical:
        resampling = Resampling.nearest
    else:
        resampling = Resampling.average if src_res < grid.res * 0.75 else Resampling.bilinear
    window_transform = from_origin(grid.left + c0 * grid.res, grid.top - r0 * grid.res, grid.res, grid.res)
    values = np.full((r1 - r0, c1 - c0), np.nan, dtype=np.float32)

    # Read only the part of the raster under the grid (plus a margin for resampling)
    area = transform_bounds(grid.crs, src_crs, *grid.bounds)
    area = (area[0] - shift[0], area[1] - shift[1], area[2] - shift[0], area[3] - shift[1])
    margin = 3 * max(abs(src.res[0]), abs(src.res[1]))
    window = from_bounds(area[0] - margin, area[1] - margin, area[2] + margin, area[3] + margin,
                         src.transform).round_offsets().round_lengths()
    window = window.intersection(rio.windows.Window(0, 0, src.width, src.height))
    band = src.read(1, window=window, masked=True).astype(np.float32).filled(np.nan)
    reproject(clean(band), values, src_transform=moved * src.window_transform(window), src_crs=src_crs,
              dst_transform=window_transform, dst_crs=grid.crs,
              src_nodata=np.nan, dst_nodata=np.nan, resampling=resampling)
    target = mosaic[r0:r1, c0:c1]
    mosaic[r0:r1, c0:c1] = np.where(np.isnan(target), values, target)


# Copernicus water body mask, 1°×1° tiles next to the DEM tiles: 1 ocean, 2 lake, 3 river
OCEAN, LAKE = 1, 2
GAP_RING = 3  # pixels around a gap used to match the filling source's heights
GAP_MATCH_SLOPE = 15  # degrees; only gentler ground is used to match heights
GAP_MAX_LOCAL_SHIFT = 3  # m; local height offsets differing more from the overall one are ignored
SHORE_BUFFER = 45  # m around water in the mask that may still be water (the mask is 30 m)
SEA_LEVEL_TOLERANCE = 1.0  # m; data at most this high inside the sea counts as water
WATER_MASK = DegreeTiles("Copernicus water body mask", "copernicus30/2021/wbm",
                         lambda lat, lon: _copernicus_url(lat, lon, "WBM"), native_res=30,
                         bytes_per_tile=1e5, credit=WATER_MASK_CREDIT)


def water_body_mask(grid: Grid, cache_dir: str, out_path: str, update: Optional[str] = None,
                    download: bool = True) -> np.ndarray:
    """Copernicus water body mask on the grid (OCEAN, LAKE...), open sea where tiles are missing
    (downloaded first unless done already: download False)."""
    if download:
        WATER_MASK.download(grid, cache_dir, update, "water mask")
    wbm = build_mosaic(WATER_MASK.files(grid, cache_dir), grid, out_path, "water mask", categorical=True)
    used_data.append(WATER_MASK.used(grid, cache_dir))
    for tile in WATER_MASK.tiles(grid):
        if cache.state(cache_dir, WATER_MASK.tile_path(cache_dir, tile)) == "missing":
            lat, lon = tile
            left, bottom, right, top = transform_bounds("EPSG:4326", grid.crs, lon, lat, lon + 1, lat + 1)
            c0, c1 = max(0, int((left - grid.left) // grid.res)), min(grid.width, int((right - grid.left) // grid.res) + 1)
            r0, r1 = max(0, int((grid.top - top) // grid.res)), min(grid.height, int((grid.top - bottom) // grid.res) + 1)
            if r0 < r1 and c0 < c1:
                block = wbm[r0:r1, c0:c1]
                block[np.isnan(block)] = OCEAN
    return np.asarray(wbm)


def near(mask: np.ndarray, metres: float, res: float) -> np.ndarray:
    """The mask grown by about `metres` (square neighbourhood)."""
    from scipy import ndimage
    if metres <= 0:
        return mask
    return ndimage.binary_dilation(mask, structure=np.ones((3, 3)), iterations=max(1, math.ceil(metres / res)))


def fill_water(elevation_path: str, surface_path: Optional[str], grid: Grid, wbm: np.ndarray,
               sea_zone: np.ndarray, out_path: str) -> str:
    """Give the sea and lakes an elevation where the data has none.

    National LiDAR models have no data over water, so lines across the sea (and
    the edges of sea cliffs, whose slope needs the neighbouring pixels) would be
    missed. Pixels without data in sea_zone (the sea mask of coast.py grown by
    --shore-buffer, see near) get 0 m. Pixels without data next to lakes of the
    Copernicus mask (within SHORE_BUFFER, as the mask is 30 m) get the level of
    the lake's shore (the median elevation around it). The exact shoreline comes
    from the elevation data itself; gaps in the data on land stay without data.
    Saves a boolean water mask (filled pixels, and lakes that have data) to out_path.
    """
    from scipy import ndimage

    elevation = np.load(elevation_path, mmap_mode="r+")
    surface = np.load(surface_path, mmap_mode="r+") if surface_path else None
    progress.compute.start("filling sea and lakes", 3, unit="step")
    empty = np.isnan(elevation)
    sea_fill = empty & sea_zone
    progress.compute.update(1)
    lake = empty & near(wbm == LAKE, SHORE_BUFFER, grid.res) & ~sea_fill
    progress.compute.update(1)

    elevation[sea_fill] = 0.0
    if surface is not None:
        surface[sea_fill & np.isnan(surface)] = 0.0
    water = sea_fill.copy()
    lake_px = 0
    labels, _ = ndimage.label(lake, structure=np.ones((3, 3)))
    for index, region in enumerate(ndimage.find_objects(labels), 1):
        if region is None:
            continue
        rows = slice(max(region[0].start - 1, 0), region[0].stop + 1)
        cols = slice(max(region[1].start - 1, 0), region[1].stop + 1)
        area = labels[rows, cols] == index
        shore = ndimage.binary_dilation(area) & ~empty[rows, cols]
        heights = elevation[rows, cols][shore]
        heights = heights[np.isfinite(heights)]
        if heights.size == 0:
            continue
        level = float(np.median(heights))
        block = elevation[rows, cols]
        block[area] = level
        if surface is not None:
            sblock = surface[rows, cols]
            sblock[area & np.isnan(sblock)] = level
        water[rows, cols] |= area
        lake_px += int(area.sum())
    # Water that the elevation data covers: lakes measured by LiDAR, and sea given as
    # about 0 m by the data itself (e.g. IGN Spain)
    water |= (wbm == LAKE) & ~empty
    water |= sea_zone & ~empty & (np.asarray(elevation) <= SEA_LEVEL_TOLERANCE)
    elevation.flush()
    if surface is not None:
        surface.flush()
    np.save(out_path, water)
    progress.compute.update(1)
    km2 = lambda px: px * grid.res ** 2 / 1e6
    progress.compute.finish(f"sea {km2(int(sea_fill.sum())):.2f} km², lakes {km2(lake_px):.2f} km²")
    return out_path


def fill_gaps(elevation_path: str, surface_path: Optional[str], grid: Grid, fallback: "Source",
              cache_dir: str, out_path: str, update: Optional[str] = None,
              exclude: Optional[np.ndarray] = None, name: str = "gap") -> Tuple[str, int]:
    """Fill the remaining gaps in the data on land from another source.

    Each gap gets the other source shifted by the median height difference in a
    ring of pixels around that gap, so it joins its surroundings without a step
    (different surveys and height systems differ by up to a few metres). Only
    gentle slopes are used for this: on a cliff, a shift of a metre between two
    surveys changes the height by tens of metres. A local offset more than
    GAP_MAX_LOCAL_SHIFT from the overall median is not trusted.
    Saves a boolean mask of the filled pixels to out_path; returns
    (out_path, number of filled pixels). Pixels in `exclude` (the sea) are left alone.
    The fallback's grids are saved next to out_path as <name>_nmt.npy and <name>_nmpt.npy.
    """
    from scipy import ndimage

    work = os.path.dirname(out_path)
    elevation = np.load(elevation_path, mmap_mode="r+")
    filled = np.isnan(elevation)
    if exclude is not None:
        filled &= ~exclude
    if not filled.any():
        np.save(out_path, filled)
        return out_path, 0
    pairs = [(elevation, fallback.nmt, f"{name}_nmt.npy")]
    if surface_path:
        surface = np.load(surface_path, mmap_mode="r+")
        pairs.append((surface, fallback.nmpt or fallback.nmt, f"{name}_nmpt.npy"))
    for target, layer, name in pairs:
        gaps = np.isnan(target) if exclude is None else np.isnan(target) & ~exclude
        if not gaps.any():
            continue
        other = np.asarray(np.load(prepare_layer(layer, grid, cache_dir, os.path.join(work, name),
                                                 f"gap fill ({fallback.name})", update), mmap_mode="r"))
        values = np.asarray(target)
        gy, gx = np.gradient(np.where(np.isfinite(values), values, other), grid.res)
        flat = np.degrees(np.arctan(np.hypot(gx, gy))) < GAP_MATCH_SLOPE
        both = ~gaps & np.isfinite(values) & np.isfinite(other) & flat
        overall = float(np.median(values[both] - other[both])) if both.any() else 0.0
        # Offset of every gap from the ring of pixels around it
        labels, count = ndimage.label(gaps, structure=np.ones((3, 3)))
        ring = ndimage.grey_dilation(labels, size=(GAP_RING * 2 + 1,) * 2) * (both & (labels == 0))
        offsets = np.full(count + 1, overall)
        if ring.any():
            # Only the ring pixels: scipy sorts every pixel it is given (10 s for a 58 Mpx tile)
            near = ring > 0
            local = ndimage.median(values[near] - other[near], ring[near], index=np.arange(1, count + 1))
            local = np.asarray(local, dtype=float)
            # A local offset far from the overall one comes from misaligned edges, not heights
            usable = np.isfinite(local) & (np.abs(local - overall) <= GAP_MAX_LOCAL_SHIFT)
            offsets[1:] = np.where(usable, local, overall)
        target[gaps] = other[gaps] + offsets[labels[gaps]]
        target.flush()
    filled &= np.isfinite(elevation)
    np.save(out_path, filled)
    return out_path, int(filled.sum())


def fill_gaps_chain(elevation_path: str, surface_path: Optional[str], grid: Grid, fallbacks: List["Source"],
                    cache_dir: str, out_path: str, update: Optional[str] = None,
                    exclude: Optional[np.ndarray] = None) -> Tuple[str, List[Tuple["Source", int]]]:
    """Fill the gaps on land from each fallback in turn (fill_gaps), each only what the ones
    before it left (most sources have one; Alaska: its IfSAR, then the 1/3″ DEM). Saves the
    mask of all filled pixels to out_path; returns (out_path, [(fallback, filled pixels)])."""
    filled, counts = None, []
    for n, fallback in enumerate(fallbacks):
        step = out_path if n == 0 else f"{os.path.splitext(out_path)[0]}{n}.npy"
        _, pixels = fill_gaps(elevation_path, surface_path, grid, fallback, cache_dir, step, update,
                              exclude=exclude, name="gap" if n == 0 else f"gap{n}")
        counts.append((fallback, pixels))
        if n == 0:
            filled = np.load(step)
        elif pixels:
            filled |= np.load(step)
            np.save(out_path, filled)
    return out_path, counts


def load_slope_file(slope_file: str, grid: Grid, out_path: str) -> str:
    """Resample a user supplied slope raster (degrees) onto the working grid."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    slope = np.lib.format.open_memmap(out_path, mode="w+", dtype=np.float32,
                                      shape=(grid.height, grid.width))
    slope[:] = np.nan
    with rio.open(slope_file) as src:
        src_crs = src.crs
        if src_crs is None:
            print(f"  {slope_file} has no coordinate system, assuming {grid.crs}")
            src_crs = grid.crs
        reproject(rio.band(src, 1), slope, src_crs=src_crs, src_transform=src.transform,
                  src_nodata=src.nodata, dst_transform=grid.transform, dst_crs=grid.crs,
                  dst_nodata=np.nan, resampling=Resampling.average)
    slope.flush()
    return out_path
