"""Download code against fake services (no network)."""

import os
import re
import sys

import numpy as np
import pytest
import rasterio as rio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sources as src  # noqa: E402

EXTENT = (-1000.0, -1000.0, 1000.0, 63.4)  # the coverage ends 63.4 m above the bottom of tile 0_0


def height(x, y):
    """The value of the fake coverage at a point."""
    return x * 1000 + y


class FakeResponse:
    def __init__(self, content=b"", text=""):
        self.content, self.text = content, text
        self.headers = {"content-type": "image/tiff"}
        self.url = "https://wcs.example/ows"

    def raise_for_status(self):
        pass


def fake_get(url, params=None, **kwargs):
    """GetCoverage like Malta's WCS: 404 (None) when the box reaches beyond the coverage."""
    boxes = {}
    for key, value in params:
        if key == "SUBSET":
            axis, low, high = re.fullmatch(r"(\w+)\(([^,]+),([^)]+)\)", value).groups()
            boxes[axis] = (float(low), float(high))
    (left, right), (bottom, top) = boxes["E"], boxes["N"]
    if left < EXTENT[0] or bottom < EXTENT[1] or right > EXTENT[2] or top > EXTENT[3]:
        return None
    width, rows = round(right - left), round(top - bottom)
    ys, xs = np.mgrid[0:rows, 0:width]
    band = height(left + xs + 0.5, top - ys - 0.5).astype(np.float32)
    with MemoryFile() as memfile:
        with memfile.open(driver="GTiff", width=width, height=rows, count=1, dtype="float32",
                          crs="EPSG:32633", transform=from_origin(left, top, 1, 1)) as dst:
            dst.write(band, 1)
        return FakeResponse(memfile.read())


def fake_describe(url, params=None, **kwargs):
    return FakeResponse(text=f'<gml:Envelope axisLabels="E N"><gml:lowerCorner>{EXTENT[0]} {EXTENT[1]}'
                             f'</gml:lowerCorner><gml:upperCorner>{EXTENT[2]} {EXTENT[3]}</gml:upperCorner>')


@pytest.fixture
def layer(monkeypatch):
    monkeypatch.setattr(src, "_get", fake_get)
    monkeypatch.setattr(src.requests, "get", fake_describe)
    return src.WcsTiles("Fake DTM", "https://wcs.example/ows", "dtm", "EPSG:32633", "fake/dtm/1", native_res=1.0,
                        tile_px=100, fetch_res=1.0, axes=("E", "N"))


def test_tile_at_the_edge_of_the_coverage(layer, tmp_path):
    """A tile reaching beyond the coverage gets the data inside it, on its own pixel grid."""
    path = str(tmp_path / "0_0.tif")
    fetched = layer.download_tile((1.0, 0, 0), path)
    assert fetched.received > 0
    with rio.open(path) as tile:
        band = tile.read(1)
        assert tile.transform == from_origin(0, 100, 1, 1)
    inside = 63  # whole pixels below 63.4 m
    assert np.isnan(band[:100 - inside]).all()
    ys, xs = np.mgrid[100 - inside:100, 0:100]
    assert np.array_equal(band[100 - inside:], height(xs + 0.5, 100 - ys - 0.5).astype(np.float32))


def test_tile_outside_the_coverage(layer, tmp_path):
    path = str(tmp_path / "0_1.tif")
    assert layer.download_tile((1.0, 0, 1), path).received == 0  # recorded as missing
    assert not os.path.exists(path)
