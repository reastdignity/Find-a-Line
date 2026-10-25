"""
The working grid: a north-up raster in a metric coordinate system around the search centre.

All elevation layers are brought onto this grid, whatever their source, so the
line search works in pixels of a known size in metres.
"""

import math
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from rasterio.transform import from_origin


def utm_crs(lat: float, lon: float) -> str:
    """UTM zone (WGS84) containing the point, e.g. EPSG:32633."""
    zone = min(60, int((lon + 180) // 6) + 1)
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


@dataclass
class Grid:
    """Working raster grid covering the search area (see area.py for its mask)."""
    left: float
    top: float
    res: float
    height: int
    width: int
    crs: str
    # Outline of a search area other than a circle (shapely, grid CRS): data tiles are
    # picked under it instead of under the circle inscribed in the grid
    area_shape: Optional[object] = None

    @property
    def transform(self):
        return from_origin(self.left, self.top, self.res, self.res)

    @property
    def bounds(self):
        """(left, bottom, right, top) in the grid CRS."""
        return (self.left, self.top - self.height * self.res,
                self.left + self.width * self.res, self.top)

    def pixel_to_xy(self, row, col):
        """Return (x, y) = (easting, northing) of pixel centres in the grid CRS."""
        x = self.left + (np.asarray(col) + 0.5) * self.res
        y = self.top - (np.asarray(row) + 0.5) * self.res
        return x, y


def make_grid(bounds: Tuple[float, float, float, float], res: float, crs: str,
              origin: Tuple[float, float] = (0.0, 0.0)) -> Grid:
    """Build a grid covering bounds (left, bottom, right, top), with edges snapped to
    origin + multiples of res.

    origin (x, y) matches the pixel edges of the source data ((0.5, 0.5) when its pixel
    centres are on whole metres), so source pixels are copied or averaged whole, not
    interpolated.
    """
    ox, oy = origin
    b_left, b_bottom, b_right, b_top = bounds
    left = math.floor((b_left - ox) / res) * res + ox
    right = math.ceil((b_right - ox) / res) * res + ox
    bottom = math.floor((b_bottom - oy) / res) * res + oy
    top = math.ceil((b_top - oy) / res) * res + oy
    width = int(round((right - left) / res))
    height = int(round((top - bottom) / res))
    return Grid(left=left, top=top, res=res, height=height, width=width, crs=crs)
