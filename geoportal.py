"""
Downloading elevation data from geoportal.gov.pl (GUGiK WCS services).

Data is fetched in square tiles laid on a fixed EPSG:2180 grid, so tiles
downloaded for one search area are reused by any later search that overlaps it.
Tiles are cached as compressed GeoTIFFs and stitched into a working grid
(stored as .npy files so worker processes can memory-map them).
"""

import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Tuple

import numpy as np
import rasterio as rio
import requests
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject

CRS = "EPSG:2180"  # PL-1992, used by all GUGiK elevation services
WCS_BASE = "https://mapy.geoportal.gov.pl/wss/service/PZGIK"

# Both layers use the same height system (PL-KRON86-NH) so they can be compared.
# tile_px is limited by the server: requests above ~2000x2000 px get dropped.
LAYERS = {
    "nmt": {
        "description": "NMT (digital terrain model)",
        "url": f"{WCS_BASE}/NMT/GRID1/WCS/DigitalTerrainModelFormatTIFF",
        "coverage": "DTM_PL-KRON86-NH_TIFF",
        "format": "image/tiff",
        "native_res": 1.0,
        "interpolation": "AVERAGE",  # smooth ground when downsampling
        "tile_px": 2000,
        "bytes_per_px": 4,
    },
    "nmpt": {
        "description": "NMPT (digital surface model: trees, buildings)",
        "url": f"{WCS_BASE}/NMPT/GRID1/WCS/DigitalSurfaceModel",
        "coverage": "DSM_PL-KRON86-NH",
        "format": "image/x-aaigrid",  # the only float format offered for NMPT
        "native_res": 0.5,
        "interpolation": "AVERAGE",  # same as NMT, so bare ground matches in both layers
        "tile_px": 1000,
        "bytes_per_px": 17,  # ASCII grid is text
    },
}

DOWNLOAD_THREADS = 4
RETRIES = 3


@dataclass
class Grid:
    """Working raster grid (EPSG:2180, north-up) around the search centre."""
    left: float
    top: float
    res: float
    height: int
    width: int
    center_row: float
    center_col: float
    radius_px: float

    @property
    def transform(self):
        return from_origin(self.left, self.top, self.res, self.res)

    def pixel_to_pl1992(self, row, col):
        """Return (easting, northing) of pixel centres."""
        easting = self.left + (np.asarray(col) + 0.5) * self.res
        northing = self.top - (np.asarray(row) + 0.5) * self.res
        return easting, northing


def make_grid(easting: float, northing: float, radius: float, res: float) -> Grid:
    """Build a grid covering a circle, with edges snapped to multiples of res."""
    left = math.floor((easting - radius) / res) * res
    right = math.ceil((easting + radius) / res) * res
    bottom = math.floor((northing - radius) / res) * res
    top = math.ceil((northing + radius) / res) * res
    width = int(round((right - left) / res))
    height = int(round((top - bottom) / res))
    return Grid(left=left, top=top, res=res, height=height, width=width,
                center_row=(top - northing) / res - 0.5,
                center_col=(easting - left) / res - 0.5,
                radius_px=radius / res)


def _tile_size_m(layer: str, res: float) -> float:
    return LAYERS[layer]["tile_px"] * res


def tiles_for_circle(layer: str, easting: float, northing: float,
                     radius: float, res: float) -> List[Tuple[int, int]]:
    """List (i, j) indices of tiles that intersect the search circle."""
    size = _tile_size_m(layer, res)
    tiles = []
    for i in range(math.floor((easting - radius) / size), math.floor((easting + radius) / size) + 1):
        for j in range(math.floor((northing - radius) / size), math.floor((northing + radius) / size) + 1):
            # Distance from the centre to the nearest point of the tile
            dx = max(i * size - easting, 0, easting - (i + 1) * size)
            dy = max(j * size - northing, 0, northing - (j + 1) * size)
            if dx * dx + dy * dy <= radius * radius:
                tiles.append((i, j))
    return tiles


def tile_path(cache_dir: str, layer: str, res: float, i: int, j: int) -> str:
    return os.path.join(cache_dir, layer, f"{res:g}m", f"{i}_{j}.tif")


def estimate_download(layer: str, tiles: List[Tuple[int, int]], cache_dir: str,
                      res: float, refresh: bool) -> Tuple[int, float]:
    """Return (number of tiles to download, approximate size in MB)."""
    missing = [t for t in tiles if refresh or not os.path.exists(tile_path(cache_dir, layer, res, *t))]
    px = LAYERS[layer]["tile_px"] ** 2
    return len(missing), len(missing) * px * LAYERS[layer]["bytes_per_px"] / 1e6


def _extract_multipart(content: bytes, content_type: str) -> bytes:
    """Pull the coverage out of a multipart/related WCS response."""
    boundary = re.search(r'boundary="?([^";]+)"?', content_type).group(1).encode()
    for part in content.split(b"--" + boundary):
        head, _, body = part.partition(b"\r\n\r\n")
        if b"Content-Type: image/" in head:
            return body.rstrip(b"\r\n")
    raise RuntimeError("WCS response did not contain coverage data")


def _download_tile(layer: str, res: float, i: int, j: int, path: str):
    spec = LAYERS[layer]
    size = _tile_size_m(layer, res)
    px = spec["tile_px"]
    left, bottom = i * size, j * size

    params = [
        ("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
        ("COVERAGEID", spec["coverage"]), ("FORMAT", spec["format"]),
        ("SUBSET", f"x({left:.3f},{left + size:.3f})"),
        ("SUBSET", f"y({bottom:.3f},{bottom + size:.3f})"),
    ]
    if not math.isclose(res, spec["native_res"]):
        params += [("SCALESIZE", f"x({px}),y({px})"),
                   ("INTERPOLATION", spec["interpolation"])]

    last_error = None
    for attempt in range(RETRIES):
        try:
            response = requests.get(spec["url"], params=params, timeout=300)
            content_type = response.headers.get("content-type", "")
            if response.status_code != 200 or "xml" in content_type or "html" in content_type:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            data = response.content
            if "multipart" in content_type:
                data = _extract_multipart(data, content_type)
            break
        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"Could not download {layer} tile {i}_{j}: {last_error}")

    ext = ".asc" if "aaigrid" in spec["format"] else ".tif"
    with MemoryFile(data, ext=ext) as memfile, memfile.open() as src:
        band = src.read(1, masked=True).astype(np.float32).filled(np.nan)
        src_crs = src.crs or CRS  # ASCII grids come without a CRS
        src_transform = src.transform

    dst_transform = from_origin(left, bottom + size, res, res)
    if band.shape != (px, px) or not src_transform.almost_equals(dst_transform):
        resampled = np.full((px, px), np.nan, dtype=np.float32)
        reproject(band, resampled, src_transform=src_transform, src_crs=src_crs,
                  dst_transform=dst_transform, dst_crs=CRS,
                  src_nodata=np.nan, dst_nodata=np.nan, resampling=Resampling.nearest)
        band = resampled

    # Areas outside Poland are returned as exactly 0.0
    band[band == 0.0] = np.nan

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".part"
    with rio.open(tmp_path, "w", driver="GTiff", height=px, width=px, count=1,
                  dtype="float32", crs=CRS, transform=dst_transform, nodata=np.nan,
                  compress="deflate", predictor=3, tiled=True) as dst:
        dst.write(band, 1)
    os.replace(tmp_path, path)


def download_tiles(layer: str, tiles: List[Tuple[int, int]], cache_dir: str,
                   res: float, refresh: bool = False):
    """Download missing tiles (in parallel) into the cache."""
    todo = [(i, j) for i, j in tiles
            if refresh or not os.path.exists(tile_path(cache_dir, layer, res, i, j))]
    cached = len(tiles) - len(todo)
    print(f"{LAYERS[layer]['description']}: {len(tiles)} tiles "
          f"({cached} cached, {len(todo)} to download)")
    if not todo:
        return

    start = time.time()
    with ThreadPoolExecutor(max_workers=DOWNLOAD_THREADS) as pool:
        futures = {pool.submit(_download_tile, layer, res, i, j,
                               tile_path(cache_dir, layer, res, i, j)): (i, j) for i, j in todo}
        for done, future in enumerate(as_completed(futures), 1):
            future.result()
            elapsed = time.time() - start
            eta = elapsed / done * (len(todo) - done)
            print(f"  Downloaded {done}/{len(todo)} tiles - "
                  f"elapsed {elapsed:.0f}s, remaining ~{eta:.0f}s")


def build_mosaic(layer: str, tiles: List[Tuple[int, int]], cache_dir: str,
                 grid: Grid, out_path: str) -> np.ndarray:
    """Stitch cached tiles into a memory-mapped array covering the grid."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    mosaic = np.lib.format.open_memmap(out_path, mode="w+", dtype=np.float32,
                                       shape=(grid.height, grid.width))
    mosaic[:] = np.nan

    for i, j in tiles:
        with rio.open(tile_path(cache_dir, layer, grid.res, i, j)) as src:
            row_off = int(round((grid.top - src.bounds.top) / grid.res))
            col_off = int(round((src.bounds.left - grid.left) / grid.res))
            r0, c0 = max(row_off, 0), max(col_off, 0)
            r1 = min(row_off + src.height, grid.height)
            c1 = min(col_off + src.width, grid.width)
            if r0 >= r1 or c0 >= c1:
                continue
            window = rio.windows.Window(c0 - col_off, r0 - row_off, c1 - c0, r1 - r0)
            mosaic[r0:r1, c0:c1] = src.read(1, window=window)

    mosaic.flush()
    return mosaic


def prepare_layer(layer: str, easting: float, northing: float, radius: float,
                  grid: Grid, cache_dir: str, refresh: bool = False) -> str:
    """Make sure a layer is downloaded and stitched. Returns the .npy path."""
    tiles = tiles_for_circle(layer, easting, northing, radius, grid.res)
    download_tiles(layer, tiles, cache_dir, grid.res, refresh)
    out_path = os.path.join(cache_dir, "work", f"{layer}.npy")
    build_mosaic(layer, tiles, cache_dir, grid, out_path)
    return out_path


def load_slope_file(slope_file: str, grid: Grid, out_path: str) -> str:
    """Resample a user supplied slope raster (degrees) onto the working grid."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    slope = np.lib.format.open_memmap(out_path, mode="w+", dtype=np.float32,
                                      shape=(grid.height, grid.width))
    slope[:] = np.nan
    with rio.open(slope_file) as src:
        src_crs = src.crs
        if src_crs is None:
            print(f"  {slope_file} has no coordinate system, assuming {CRS} (PL-1992)")
            src_crs = CRS
        reproject(rio.band(src, 1), slope, src_crs=src_crs, src_transform=src.transform,
                  src_nodata=src.nodata, dst_transform=grid.transform, dst_crs=CRS,
                  dst_nodata=np.nan, resampling=Resampling.average)
    slope.flush()
    return out_path
