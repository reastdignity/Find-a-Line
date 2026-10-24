"""Cut the Żabi Koń fixture for tests/test_offline.py from the cached geoportal sheets.

Run once from the repository root after a search at Żabi Koń (R1) has cached the
sheets: python tests/data/make_fixture.py. Writes zabi_kon_nmt.tif and
zabi_kon_nmpt.tif (600 x 600 m at 1 m, EPSG:2180, pixel edges on half metres like
the sheets).
"""

import glob
import os

import numpy as np
import rasterio as rio
from rasterio.transform import from_origin
from rasterio.windows import from_bounds

HERE = os.path.dirname(os.path.abspath(__file__))
LEFT, TOP, SIZE = 553665.5, 254908.5, 600  # around R1 spot 1
SHEET = "*M-34-64-D-a-1-1.tif"


def cut(kind: str):
    path = sorted(glob.glob(os.path.join("cache", "poland", kind, SHEET)))[-1]  # newest sheet
    with rio.open(path) as src:
        window = from_bounds(LEFT, TOP - SIZE, LEFT + SIZE, TOP, src.transform)
        band = src.read(1, window=window).astype(np.float32)
        crs = src.crs
    assert band.shape == (SIZE, SIZE) and np.isfinite(band).all(), (path, band.shape)
    out = os.path.join(HERE, f"zabi_kon_{kind}.tif")
    with rio.open(out, "w", driver="GTiff", width=SIZE, height=SIZE, count=1, dtype="float32", crs=crs,
                  transform=from_origin(LEFT, TOP, 1, 1), nodata=np.nan,
                  compress="deflate", predictor=3, tiled=True) as dst:
        dst.write(band, 1)
    print(f"{out}: from {os.path.basename(path)}, {os.path.getsize(out) / 1e6:.1f} MB")


if __name__ == "__main__":
    cut("nmt")
    cut("nmpt")
