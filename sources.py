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
  SheetTiles (map sheets from a WFS sheet index, geoportal.gov.pl).
- DegreeTiles: 1°×1° GeoTIFF tiles in WGS84 (Copernicus DEM, FABDEM) and
  OpenTopography's global DEM API (NASADEM).
- FileLayer: a local raster file (GeoTIFF, ASC...), e.g. NASADEM from OpenTopography.

To add a national source, add a Source with WcsTiles layers to SOURCES (see the
Malta entries) and its country code to `countries`; see README, "Data sources".
"""

import glob
import io
import json
import math
import os
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

import numpy as np
import rasterio as rio
import requests
from affine import Affine
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds
from rasterio.windows import from_bounds

import cache
import osm
from grid import Grid, utm_crs
from progress import progress

DOWNLOAD_THREADS = 4
RETRIES = 3
HEADERS = {"User-Agent": "Find-a-Line/1.0 (highline finder)"}
VALID_MIN = -500  # metres; anything lower is treated as "no data"


def _get(url: str, params=None, timeout: int = 300, binary_types=("image", "octet", "tiff", "multipart")):
    """GET with retries; raises RuntimeError with the server message on failure."""
    last_error = None
    for attempt in range(RETRIES):
        try:
            response = requests.get(url, params=params, timeout=timeout, headers=HEADERS)
            content_type = response.headers.get("content-type", "")
            if response.status_code == 404:
                return None
            if response.status_code != 200 or not any(t in content_type for t in binary_types):
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            return response
        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            time.sleep(2 ** attempt)
    raise RuntimeError(str(last_error))


def _part(path: str) -> str:
    """A temporary name next to path, unique per process and thread (searches may run in parallel)."""
    return f"{path}.{os.getpid()}-{threading.get_ident()}.part"


def _write_tile(path: str, band: np.ndarray, crs: str, transform):
    """Save a float32 tile as a compressed GeoTIFF (atomically)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = _part(path)
    with rio.open(tmp, "w", driver="GTiff", height=band.shape[0], width=band.shape[1], count=1,
                  dtype="float32", crs=crs, transform=transform, nodata=np.nan,
                  compress="deflate", predictor=3, tiled=True) as dst:
        dst.write(band.astype(np.float32), 1)
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


class Layer:
    """One elevation model of a source, cached in cache/<folder> (see cache.py)."""
    description = ""
    folder = ""
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
        todo = self.missing(grid, cache_dir, update)
        total = len(self.tiles(grid))
        print(f"{self.description}: {total} tiles ({total - len(todo)} cached, {len(todo)} to download)")
        if not todo:
            return
        progress.download.start(f"{label} tiles", len(todo), unit="tile")
        received = 0
        with ThreadPoolExecutor(max_workers=DOWNLOAD_THREADS) as pool:
            futures = {pool.submit(self._download_and_record, t, cache_dir): t for t in todo}
            for future in as_completed(futures):
                received += future.result()
                progress.download.update(1, MB=f"{received / 1e6:.0f}")
        progress.download.finish(f"{received / 1e6:.0f} MB")

    def tile_of(self, path: str):
        """The tile of a cached file (path relative to the cache, with /), or None if it
        is not one of this layer's tiles. Used by update.py for data that can change."""
        return None

    def _download_and_record(self, tile, cache_dir: str) -> int:
        path = self.tile_path(cache_dir, tile)
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
        return {"layer": self.description, "folder": self.folder, "files": len(files),
                "fetched": cache.touch(cache_dir, files),
                "paths": sorted(cache.relative(cache_dir, p) for p in files)}


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
                response = requests.get(self.url, timeout=60, headers=HEADERS, params={
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


class GridTiles(Layer):
    """Square tiles on a fixed grid in a metric CRS, downloaded from one of a list of
    base URLs as <base>/<grid_tile_name> (our own hosted tiles: tools/sk_tiles.py).

    Tile (i, j) covers [i, i + 1) x [j, j + 1) tile sizes in the CRS; a tile that none of
    the servers has (404) is outside the data and recorded as missing.
    """

    def __init__(self, description: str, urls: List[str], crs: str, folder: str, tile_m: float,
                 native_res: float, bytes_per_tile: float):
        self.description, self.urls, self.crs, self.folder = description, urls, crs, folder
        self.tile_m, self.native_res, self._bytes = tile_m, native_res, bytes_per_tile

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

    def download_tile(self, tile, path: str) -> Fetched:
        name, error = grid_tile_name(*tile), None
        for base in self.urls:  # the first server that answers
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
        raise RuntimeError(f"Could not download {self.description} tile {name}: {error}")


class DegreeTiles(Layer):
    """1°×1° tiles in WGS84, downloaded whole at native resolution."""

    def __init__(self, description: str, folder: str, url: Callable[[int, int], str],
                 native_res: float = 30, bytes_per_tile: float = 15e6):
        self.description, self.folder, self.url = description, folder, url
        self.native_res, self._bytes = native_res, bytes_per_tile

    def tiles(self, grid: Grid):
        west, south, east, north = transform_bounds(grid.crs, "EPSG:4326", *grid.bounds)
        return [(lat, lon) for lat in range(math.floor(south), math.floor(north) + 1)
                for lon in range(math.floor(west), math.floor(east) + 1)]

    def tile_path(self, cache_dir: str, tile) -> str:
        lat, lon = tile
        return os.path.join(cache_dir, self.folder, f"{_hemi(lat, 'N', 'S', 2)}{_hemi(lon, 'E', 'W', 3)}.tif")

    def bytes_per_tile(self) -> float:
        return self._bytes

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
            raise RuntimeError("an OpenTopography API key is needed: set OPENTOPOGRAPHY_API_KEY "
                               "or use --opentopo-key (free at portal.opentopography.org)")
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
        if cache.due(cache_dir, path, update=update, ttl=cache.TTL_DAYS["sheet-index"]):
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

    def download(self, grid: Grid, cache_dir: str, update: Optional[str], label: str):
        self._use(cache_dir, update)
        sheets = self.tiles(grid)
        if sheets is not None:
            super().download(grid, cache_dir, update, label)
            pruned = self._prune(cache_dir, sheets)
            if pruned:
                print(f"{self.description}: {pruned} older surveys replaced by newer ones (deleted)")

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
        response = requests.get(self.index_url, timeout=300, headers=HEADERS,
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
    """A WMS with orthophotos, requested in WGS84 (see orthophoto.py)."""
    name: str
    url: str
    layer: str
    folder: str      # in the cache; tiles go to <folder>/wm256/{z}/{x}/{y}.jpg
    attribution: str
    # Tiles ({z}/{x}/{y} in Web Mercator, up to base_max_zoom) with full coverage under this
    # service, where it has no imagery and answers with white (geoportal's HighResolution
    # covers only sheets of 10 cm or finer)
    base_url: Optional[str] = None
    base_max_zoom: int = 19

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
    default_resolution: float = 2
    ortho: Optional[OrthoService] = None
    note: str = ""                  # licence or quality note printed at the start
    fallback: Optional[str] = "europe"  # source that fills gaps in the data on land
    bbox: Optional[Tuple[float, float, float, float]] = None  # (south, north, west, east) for --source auto
    grid_origin: Tuple[float, float] = (0.0, 0.0)  # pixel edges of the data in the working CRS (grid.make_grid)

    def covers(self, lat: float, lon: float) -> bool:
        if self.bbox is None:
            return True
        south, north, west, east = self.bbox
        return south <= lat <= north and west <= lon <= east

    def working_crs(self, lat: float, lon: float) -> str:
        return self.crs or utm_crs(lat, lon)


GEOPORTAL = "https://mapy.geoportal.gov.pl/wss/service/PZGIK"
MALTA = "https://malta.coverage.wetransform.eu"
SPAIN_MDT = "https://servicios.idee.es/wcs-inspire/mdt"
SPAIN_MDS = "https://wcs-mds.idee.es/mds"
SPAIN_ORTHO = OrthoService("Orthophoto PNOA (IGN Spain)", "https://www.ign.es/wms-inspire/pnoa-ma",
                           "OI.OrthoimageCoverage", "spain/ortho/live", "PNOA © IGN")
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
        countries=("es",), crs=crs, default_resolution=5, ortho=SPAIN_ORTHO,
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
        countries=("mt",) if year == 2018 else (), crs=crs, default_resolution=2,
        fallback="malta" if year == 2012 else "europe",
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
        countries=("pl",), crs="EPSG:2180", default_resolution=2, grid_origin=(0.5, 0.5),
        ortho=OrthoService("Orthophoto (geoportal.gov.pl)", f"{GEOPORTAL}/ORTO/WMS/HighResolution",
                           "Raster", "poland/ortho/live", "GUGiK",
                           # the WMS of StandardResolution answers about half of the requests
                           # with 404 (2026-10-25); its WMTS answers all
                           base_url=f"{GEOPORTAL}/ORTO/WMTS/StandardResolution?SERVICE=WMTS&REQUEST=GetTile"
                                    "&VERSION=1.0.0&LAYER=ORTOFOTOMAPA&STYLE=default&FORMAT=image/jpeg"
                                    "&TILEMATRIXSET=EPSG:3857&TILEMATRIX=EPSG:3857:{z}&TILEROW={y}&TILECOL={x}")),
    _malta(2018, note="Malta 2018 heights are published in whole metres, which makes slopes and "
                      "clearances coarser; --source malta2012 has centimetre precision but gaps "
                      "along the coast (filled from 2018)."),
    _malta(2012),
    _spain(canary=True),
    _spain(canary=False),
    Source(
        id="europe", name="FABDEM + Copernicus DEM",
        description="FABDEM V1-2 (terrain, trees and buildings removed) and Copernicus GLO-30 (surface), 30 m",
        nmt=DegreeTiles("FABDEM (terrain)", "fabdem/v1-2", _fabdem_url, native_res=30, bytes_per_tile=20e6),
        nmpt=DegreeTiles("Copernicus DEM GLO-30 (surface)", "copernicus30/2021/dem", _copernicus_url,
                         native_res=30, bytes_per_tile=20e6),
        default_resolution=30, fallback=None,
        note="FABDEM is free for non-commercial use only (Non-Commercial Government Licence). "
             "At 30 m only long lines can be found reliably."),
    Source(
        id="nasadem", name="NASADEM (OpenTopography)",
        description="NASADEM 30 m (SRTM, includes some vegetation), from the OpenTopography API",
        nmt=OpenTopographyTiles("NASADEM", "nasadem/v1", "NASADEM"),
        default_resolution=30,
        note="NASADEM is a radar surface model at 30 m: only long lines can be found reliably."),
]}

# Rough boxes used when the country cannot be looked up (offline)
OFFLINE_BOXES = {"mt": (35.78, 36.10, 14.15, 14.60), "pl": (49.0, 54.9, 14.1, 24.2),
                 "es": (27.0, 43.9, -18.6, 4.4), "sk": (47.7, 49.65, 16.8, 22.6)}


def country_code(lat: float, lon: float, cache_dir: str) -> Optional[str]:
    """ISO country code of a point (OpenStreetMap), or a rough guess when offline."""
    try:
        code = osm.reverse(lat, lon, cache_dir, zoom=3).get("address", {}).get("country_code")
        if code:
            return code.lower()
    except (requests.RequestException, ValueError):
        pass
    for code, (s, n, w, e) in OFFLINE_BOXES.items():
        if s <= lat <= n and w <= lon <= e:
            return code
    return None


def choose_source(name: str, lat: float, lon: float, cache_dir: str, elevation_file: Optional[str] = None,
                  surface_file: Optional[str] = None) -> Tuple[Source, str]:
    """The source to use and why."""
    if elevation_file or name == "files":
        if not elevation_file:
            raise ValueError("--source files needs --elevation-file")
        source = Source(id="files", name="local files", description=f"local files: {elevation_file}"
                        + (f", {surface_file}" if surface_file else ""),
                        nmt=FileLayer("Terrain file", elevation_file),
                        nmpt=FileLayer("Surface file", surface_file) if surface_file else None)
        with rio.open(elevation_file) as src:
            if src.crs is not None and src.crs.is_projected and src.crs.linear_units in ("metre", "meter"):
                source.crs = src.crs.to_string()
                # The grid is in the file's CRS: put its edges on the file's pixel edges
                source.grid_origin = (src.transform.c % abs(src.res[0]), src.transform.f % abs(src.res[1]))
        native = source.nmt.native_res
        source.default_resolution = float(round(native)) if native >= 5 else max(1.0, round(native, 1))
        return source, "--elevation-file given"
    if name != "auto":
        return SOURCES[name], f"--source {name}"
    code = country_code(lat, lon, cache_dir)
    for source in SOURCES.values():
        if code and code in source.countries and source.covers(lat, lon):
            return source, f"the centre is in {code.upper()}"
    return SOURCES["europe"], f"no national source for {code.upper() if code else 'this place'}"


# What the layers prepared in this process came from (Layer.used), saved with the run
used_data: List[Dict] = []


def prepare_layer(layer: Layer, grid: Grid, cache_dir: str, out_path: str, label: str,
                  update: Optional[str] = None) -> str:
    """Download what is missing and bring the layer onto the working grid (.npy)."""
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
                         bytes_per_tile=1e5)


def water_body_mask(grid: Grid, cache_dir: str, out_path: str, update: Optional[str] = None) -> np.ndarray:
    """Copernicus water body mask on the grid (OCEAN, LAKE...), open sea where tiles are missing."""
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
              exclude: Optional[np.ndarray] = None) -> Tuple[str, int]:
    """Fill the remaining gaps in the data on land from another source.

    Each gap gets the other source shifted by the median height difference in a
    ring of pixels around that gap, so it joins its surroundings without a step
    (different surveys and height systems differ by up to a few metres). Only
    gentle slopes are used for this: on a cliff, a shift of a metre between two
    surveys changes the height by tens of metres. A local offset more than
    GAP_MAX_LOCAL_SHIFT from the overall median is not trusted.
    Saves a boolean mask of the filled pixels to out_path; returns
    (out_path, number of filled pixels). Pixels in `exclude` (the sea) are left alone.
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
    pairs = [(elevation, fallback.nmt, "gap_nmt.npy")]
    if surface_path:
        surface = np.load(surface_path, mmap_mode="r+")
        pairs.append((surface, fallback.nmpt or fallback.nmt, "gap_nmpt.npy"))
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
            local = ndimage.median(values - np.where(both, other, 0), ring, index=np.arange(1, count + 1))
            local = np.asarray(local, dtype=float)
            # A local offset far from the overall one comes from misaligned edges, not heights
            usable = np.isfinite(local) & (np.abs(local - overall) <= GAP_MAX_LOCAL_SHIFT)
            offsets[1:] = np.where(usable, local, overall)
        target[gaps] = other[gaps] + offsets[labels[gaps]]
        target.flush()
    filled &= np.isfinite(elevation)
    np.save(out_path, filled)
    return out_path, int(filled.sum())


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
