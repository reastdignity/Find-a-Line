"""Download code against fake services (no network)."""

import hashlib
import io
import json
import math
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


def _synthetic_zip(tmp_path):
    """A 1 m raster in S-JTSK (EPSG:8353) with centimetre heights and a hole, zipped like ÚGKK's."""
    import zipfile
    rng = np.random.default_rng(3)
    left, top, width, height = -594413.0, -1132310.0, 5000, 3000   # pixel edges on whole metres
    band = (300 + rng.random((height, width)) * 50).round(2).astype(np.float32)
    band[:, :700] = -9999.0                                       # no data on the west
    tif = tmp_path / "t.tif"
    with rio.open(tif, "w", driver="GTiff", width=width, height=height, count=1, dtype="float32", crs="EPSG:8353",
                  transform=from_origin(left, top, 1, 1), nodata=-9999.0, tiled=True, blockxsize=128,
                  blockysize=128, compress="lzw") as dst:
        dst.write(band, 1)
    work = tmp_path / "work"
    work.mkdir()
    with zipfile.ZipFile(work / "test.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tif, "t.tif")
    return work, band, left, top


def test_slovak_tiles_cut_and_downloaded(tmp_path, monkeypatch):
    """tools/sk_tiles.py cuts a zipped raster into 2 km tiles; GridTiles brings them back onto a grid."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import sk_tiles
    import grid as gd
    work, band, left, top = _synthetic_zip(tmp_path)
    monkeypatch.setitem(sk_tiles.PRODUCTS, "test", {"zip": "https://example.org/test.zip", "member": "t.tif",
                                                   "title": "test"})
    sk_tiles.cut("test", str(work))
    root = work / "tiles" / "test" / "1m"
    index = json.load(open(root / "index.json", encoding="utf-8"))
    written = {k: v for k, v in index["tiles"].items() if v}
    assert written and None in index["tiles"].values()    # tiles in the hole on the west have no data
    for key, sha in written.items():
        assert hashlib.sha256((root / src.grid_tile_name(*map(int, key.split("_")))).read_bytes()).hexdigest() == sha

    def fake_server(url, params=None, **kwargs):
        if url.startswith("https://down.example"):
            raise RuntimeError("HTTP 503")                   # the first server is down: the next is asked
        path = root / url.split("/test/1m/", 1)[1]
        return FakeResponse(path.read_bytes()) if path.exists() else None
    monkeypatch.setattr(src, "_get", fake_server)
    layer = src.GridTiles("Test DSM", ["https://down.example/test/1m", "https://hf.example/test/1m"],
                          "EPSG:8353", "slovakia/test/1m", 2000, 1.0, 6e6)
    grid = gd.make_grid((left + 500, top - 2800, left + 4600, top - 100), 1.0, "EPSG:8353")
    cache_dir = str(tmp_path / "cache")
    layer.download(grid, cache_dir, None, "test")
    mosaic = src.build_mosaic(layer.files(grid, cache_dir), grid, str(tmp_path / "m.npy"), "test")
    c0, r0 = round(grid.left - left), round(top - grid.top)
    expected = band[r0:r0 + grid.height, c0:c0 + grid.width].astype(np.float64)
    expected[expected == -9999.0] = np.nan
    both = np.isfinite(expected)
    assert np.array_equal(np.isfinite(mosaic), both)                   # no data stays no data
    assert np.nanmax(np.abs(mosaic[both] - expected[both])) <= 0.005    # LERC: within 5 mm
    # A tile outside the data is recorded as missing, not fetched again
    outside = (math.floor(left / 2000) - 5, math.floor(top / 2000))
    assert layer.download_tile(outside, str(tmp_path / "x.tif")).received == 0


def _image(fmt, color, covered):
    """A 256 px tile: color where covered (columns), white (JPEG) or transparent (PNG) elsewhere."""
    from PIL import Image
    tile = np.zeros((256, 256, 4), np.uint8)
    tile[:, :] = (255, 255, 255, 0 if fmt == "PNG" else 255)
    tile[:, covered] = (*color, 255)
    out = io.BytesIO()
    Image.fromarray(tile if fmt == "PNG" else tile[:, :, :3]).save(out, fmt)
    return out.getvalue()


def test_orthophoto_falls_back_to_the_base_tiles(tmp_path, monkeypatch):
    """Where the WMS has no imagery (white), the cached tile comes from the base tiles."""
    import orthophoto
    from PIL import Image
    service = src.OrthoService("Fake", "https://high/wms", "top", "fake/ortho/live", "test",
                               base_url="https://base/{z}/{x}/{y}.jpg", base_max_zoom=16)
    tiles = {"full": (16, 1, 1), "none": (16, 2, 1), "edge": (16, 3, 1), "deep": (18, 4 * 5 + 1, 4 * 1 + 2)}
    covered = {"full": slice(0, 256), "none": slice(0, 0), "edge": slice(0, 128), "deep": slice(0, 0)}
    by_bbox = {orthophoto._wms_bbox(*t): name for name, t in tiles.items()}
    asked = []

    def get(url, params, timeout):
        if params is None:  # a base tile: left half dark, right half light green
            z, x, y = (int(v) for v in url[len("https://base/"):-len(".jpg")].split("/"))
            asked.append(("base", z, x, y))
            tile = np.zeros((256, 256, 3), np.uint8)
            tile[:, :128], tile[:, 128:] = (20, 90, 20), (60, 220, 60)
            out = io.BytesIO()
            Image.fromarray(tile).save(out, "JPEG")
            content, kind = out.getvalue(), "jpeg"
        else:
            name, kind = by_bbox[params["BBOX"]], "png" if params["FORMAT"] == "image/png" else "jpeg"
            asked.append((name, kind))
            content = _image(kind.upper(), (200, 40, 40), covered[name])
        response = FakeResponse(content)
        response.status_code, response.headers = 200, {"content-type": f"image/{kind}"}
        return response
    monkeypatch.setattr(orthophoto.requests, "get", get)

    def colour(name):
        path = str(tmp_path / f"{name}.jpg")
        orthophoto._download_tile(service, *tiles[name], path)
        return np.asarray(Image.open(path).convert("RGB")).astype(int)

    full, none, edge, deep = colour("full"), colour("none"), colour("edge"), colour("deep")
    assert abs(full - (200, 40, 40)).max() < 10                 # the WMS has imagery: its tile
    assert ("base", 16, 1, 1) not in asked
    assert abs(none[:, :120] - (20, 90, 20)).max() < 10         # none: the base's tile, never white
    assert abs(none[:, 136:] - (60, 220, 60)).max() < 10
    assert abs(edge[:, :120] - (200, 40, 40)).max() < 10        # the edge of its coverage:
    assert abs(edge[:, 136:] - (60, 220, 60)).max() < 10        # the WMS over the base
    assert ("edge", "png") in asked
    # deeper than the base goes: a quarter of its tile at zoom 16 (x 21 = 4 * 5 + 1: the
    # second quarter from the left, all of it in the dark half), enlarged
    assert ("base", 16, 5, 1) in asked and abs(deep[:, :200] - (20, 90, 20)).max() < 10
    # without a base the tile is kept as the WMS sent it
    plain = src.OrthoService("Fake", "https://high/wms", "top", "fake/ortho/live", "test")
    assert orthophoto.tile_content(plain, *tiles["none"]) == _image("JPEG", (200, 40, 40), slice(0, 0))
