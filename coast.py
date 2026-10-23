"""
Where the sea is: a sea mask on the working grid, used to fill the sea where
the elevation data has none (see sources.fill_water).

Three ways to get it (--coastline):
- overpass:   OpenStreetMap coastline for the search area from the Overpass API
              (small, cached). If Overpass does not answer, the Copernicus water
              body mask is used instead, with a warning about its accuracy.
- world:      OpenStreetMap water polygons of the whole world (osmdata.openstreetmap.de,
              about 900 MB zipped), downloaded once into the cache.
- copernicus: the Copernicus water body mask (30 m). Measured at the Maltese
              coast, 90% of the shoreline is within 70 m of the real one, against
              13 m for the OpenStreetMap coastline.
"""

import hashlib
import json
import os
import zipfile
from typing import Optional, Tuple

import numpy as np
import requests
from rasterio.features import rasterize
from rasterio.warp import transform_bounds
from shapely.geometry import LineString, box, mapping
from shapely.ops import polygonize, transform as shape_transform, unary_union
from pyproj import Transformer

from grid import Grid
from progress import progress

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
WORLD_URL = "https://osmdata.openstreetmap.de/download/water-polygons-split-4326.zip"
WORLD_MB = 910
HEADERS = {"User-Agent": "Find-a-Line/1.0 (highline finder)"}
MARGIN_DEG = 0.02  # the coastline is fetched a little beyond the grid, so faces close properly


def _grid_bbox_wgs84(grid: Grid) -> Tuple[float, float, float, float]:
    west, south, east, north = transform_bounds(grid.crs, "EPSG:4326", *grid.bounds)
    return west - MARGIN_DEG, south - MARGIN_DEG, east + MARGIN_DEG, north + MARGIN_DEG


def _rasterize(geometries, grid: Grid) -> np.ndarray:
    shapes = [(mapping(g), 1) for g in geometries if not g.is_empty]
    if not shapes:
        return np.zeros((grid.height, grid.width), dtype=bool)
    return rasterize(shapes, out_shape=(grid.height, grid.width), transform=grid.transform,
                     fill=0, dtype="uint8").astype(bool)


def _overpass_coastline(grid: Grid, cache_dir: str, refresh: bool):
    """OSM coastline ways around the grid as lines in the grid CRS (cached)."""
    west, south, east, north = _grid_bbox_wgs84(grid)
    key = hashlib.sha1(f"{south:.4f},{west:.4f},{north:.4f},{east:.4f}".encode()).hexdigest()[:16]
    path = os.path.join(cache_dir, "osm", "coastline", f"{key}.json")
    if refresh or not os.path.exists(path):
        query = (f'[out:json][timeout:90];way["natural"="coastline"]'
                 f'({south},{west},{north},{east});out geom;')
        response = requests.get(OVERPASS_URL, params={"data": query}, headers=HEADERS, timeout=120)
        response.raise_for_status()
        data = response.json()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    to_grid = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True).transform
    lines = [LineString([(p["lon"], p["lat"]) for p in way["geometry"]])
             for way in data.get("elements", []) if len(way.get("geometry", [])) > 1]
    return [shape_transform(to_grid, line) for line in lines]


def _sea_from_coastline(lines, grid: Grid, copernicus_sea: np.ndarray) -> np.ndarray:
    """Split the area along the coastline and keep the parts the Copernicus mask calls sea.

    The coastline gives the exact shore; the coarse mask only decides which side
    of it is water, which it does reliably away from the shore.
    """
    frame = box(*grid.bounds)
    faces = list(polygonize(unary_union(lines + [frame.exterior])))
    sea = []
    for face in faces:
        face = face.intersection(frame)
        if face.is_empty:
            continue
        inside = _rasterize([face], grid)
        if inside.any() and copernicus_sea[inside].mean() > 0.5:
            sea.append(face)
    return _rasterize(sea, grid)


def _world_polygons(cache_dir: str, refresh: bool) -> str:
    """Path of the OSM water polygons shapefile, downloading it once (about 900 MB)."""
    folder = os.path.join(cache_dir, "osm", "water-polygons")
    shapefile = os.path.join(folder, "water-polygons-split-4326", "water_polygons.shp")
    if os.path.exists(shapefile) and not refresh:
        return shapefile
    os.makedirs(folder, exist_ok=True)
    archive = os.path.join(folder, "water-polygons-split-4326.zip")
    with requests.get(WORLD_URL, stream=True, headers=HEADERS, timeout=120) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        progress.download.start("OSM water polygons (world)", total / 1e6, unit="MB")
        with open(archive + ".part", "wb") as f:
            for chunk in response.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                progress.download.update(len(chunk) / 1e6)
        progress.download.finish()
    os.replace(archive + ".part", archive)
    with zipfile.ZipFile(archive) as z:
        z.extractall(folder)
    os.remove(archive)
    return shapefile


def sea_mask(grid: Grid, cache_dir: str, mode: str, copernicus_sea: np.ndarray,
             refresh: bool = False) -> Tuple[np.ndarray, str, Optional[str]]:
    """Boolean sea mask on the grid.

    Returns (mask, description of the source, warning or None).
    """
    if not copernicus_sea.any():
        # Inland: the mask shows no sea anywhere in the area. Its shoreline is off by
        # about 100 m at most, so at worst a thin strip at the edge of the area is missed
        return copernicus_sea, "no sea in the area", None
    if mode == "world":
        import geopandas as gpd
        shapefile = _world_polygons(cache_dir, refresh)
        water = gpd.read_file(shapefile, bbox=_grid_bbox_wgs84(grid)).to_crs(grid.crs)
        return _rasterize(water.geometry, grid), "OpenStreetMap water polygons (world file)", None

    if mode == "overpass":
        try:
            lines = _overpass_coastline(grid, cache_dir, refresh)
        except (requests.RequestException, ValueError) as e:
            warning = (f"OpenStreetMap coastline could not be downloaded from Overpass ({e}). "
                       "Using the Copernicus water body mask instead: its shoreline is only accurate "
                       "to about 70 m (90% of the coast), so lines near the shore may be missed or "
                       "land next to the sea may be taken for water. Run again later, or use "
                       "--coastline world.")
            return copernicus_sea, "Copernicus water body mask (fallback)", warning
        if not lines:  # no coast near the area: the mask is right away from the shore
            return copernicus_sea, "no coastline nearby (OpenStreetMap)", None
        return _sea_from_coastline(lines, grid, copernicus_sea), "OpenStreetMap coastline (Overpass)", None

    return copernicus_sea, "Copernicus water body mask", None
