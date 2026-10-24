"""
Search areas: where the anchors may be. Both anchors of a line must be inside the
area; the webbing may pass outside it (e.g. over a bay wider than a coastal band).

An area gives the box the working grid has to cover, a boolean mask on that grid,
and its shape for the maps and the orthophoto cache.
- Circle: a centre and a radius (--lat/--lon or --northing/--easting, --radius).
- Polygon: any shape in WGS84 (search areas from files or OSM, later).
"""

import math
from typing import Dict, Tuple

import numpy as np
import shapely
from pyproj import Transformer
from rasterio.features import rasterize
from shapely.geometry import Point, mapping

from grid import Grid

Bounds = Tuple[float, float, float, float]  # left, bottom, right, top


def _to(crs: str):
    return Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform


def project(geometry, src: str, dst: str):
    """A shapely geometry from one CRS to another (x, y order)."""
    to = Transformer.from_crs(src, dst, always_xy=True).transform
    return shapely.transform(geometry, lambda xy: np.column_stack(to(xy[:, 0], xy[:, 1])))


class Area:
    center: Tuple[float, float]  # (lat, lon), used for names and to centre the maps

    def bounds(self, crs: str) -> Bounds:
        """The box (in crs) the working grid has to cover."""
        raise NotImplementedError

    def mask(self, grid: Grid) -> np.ndarray:
        """True for the pixels of the grid inside the area."""
        raise NotImplementedError

    def shape_wgs84(self):
        """The outline in WGS84 (lon, lat), for the maps."""
        raise NotImplementedError

    def shape_mercator(self):
        """The outline in Web Mercator metres, for the orthophoto tiles."""
        return project(self.shape_wgs84(), "EPSG:4326", "EPSG:3857")

    def describe(self) -> Dict:
        """What run.json records."""
        raise NotImplementedError


class Circle(Area):
    def __init__(self, lat: float, lon: float, radius: float):
        self.center, self.radius = (lat, lon), radius

    def _xy(self, crs: str) -> Tuple[float, float]:
        lat, lon = self.center
        return _to(crs)(lon, lat)

    def bounds(self, crs: str) -> Bounds:
        x, y = self._xy(crs)
        r = self.radius
        return x - r, y - r, x + r, y + r

    def mask(self, grid: Grid) -> np.ndarray:
        x, y = self._xy(grid.crs)
        center_row = (grid.top - y) / grid.res - 0.5
        center_col = (x - grid.left) / grid.res - 0.5
        rows, cols = np.ogrid[:grid.height, :grid.width]
        return (rows - center_row) ** 2 + (cols - center_col) ** 2 <= (self.radius / grid.res) ** 2

    def shape_wgs84(self):
        lat, lon = self.center
        local = f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m"
        return project(Point(0, 0).buffer(self.radius, 128), local, "EPSG:4326")

    def shape_mercator(self):
        # Web Mercator stretches distances by 1/cos(latitude)
        lat, lon = self.center
        x, y = _to("EPSG:3857")(lon, lat)
        return Point(x, y).buffer(self.radius / math.cos(math.radians(lat)))

    def describe(self) -> Dict:
        return {"type": "circle", "lat": self.center[0], "lon": self.center[1], "radius": self.radius}


class Polygon(Area):
    def __init__(self, geometry):
        """geometry: a shapely (Multi)Polygon in WGS84 (lon, lat)."""
        self.geometry = geometry
        centroid = geometry.centroid
        self.center = (centroid.y, centroid.x)

    def bounds(self, crs: str) -> Bounds:
        return project(self.geometry, "EPSG:4326", crs).bounds

    def mask(self, grid: Grid) -> np.ndarray:
        shape = project(self.geometry, "EPSG:4326", grid.crs)
        return rasterize([(mapping(shape), 1)], out_shape=(grid.height, grid.width),
                         transform=grid.transform, fill=0, dtype="uint8").astype(bool)

    def shape_wgs84(self):
        return self.geometry

    def describe(self) -> Dict:
        return {"type": "polygon", "geometry": mapping(self.geometry)}
