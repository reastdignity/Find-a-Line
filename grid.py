"""
The working grid: a north-up raster in a metric coordinate system around the search centre.

All elevation layers are brought onto this grid, whatever their source, so the
line search works in pixels of a known size in metres.
"""

import math
from dataclasses import dataclass
from typing import Tuple

import numpy as np
from rasterio.transform import from_origin


def utm_crs(lat: float, lon: float) -> str:
    """UTM zone (WGS84) containing the point, e.g. EPSG:32633."""
    zone = min(60, int((lon + 180) // 6) + 1)
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


@dataclass
class Grid:
    """Working raster grid around the search centre."""
    left: float
    top: float
    res: float
    height: int
    width: int
    center_row: float
    center_col: float
    radius_px: float
    crs: str

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


def make_grid(x: float, y: float, radius: float, res: float, crs: str,
              origin: Tuple[float, float] = (0.0, 0.0)) -> Grid:
    """Build a grid covering a circle, with edges snapped to origin + multiples of res.

    origin (x, y) matches the pixel edges of the source data ((0.5, 0.5) when its pixel
    centres are on whole metres), so source pixels are copied or averaged whole, not
    interpolated.
    """
    ox, oy = origin
    left = math.floor((x - radius - ox) / res) * res + ox
    right = math.ceil((x + radius - ox) / res) * res + ox
    bottom = math.floor((y - radius - oy) / res) * res + oy
    top = math.ceil((y + radius - oy) / res) * res + oy
    width = int(round((right - left) / res))
    height = int(round((top - bottom) / res))
    return Grid(left=left, top=top, res=res, height=height, width=width,
                center_row=(top - y) / res - 0.5,
                center_col=(x - left) / res - 0.5,
                radius_px=radius / res, crs=crs)
