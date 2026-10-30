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


UGKK_CRS = ('COMPD_CS["S-JTSK [JTSK03] / Krovak East North + Baltic 1957 height",'
            'LOCAL_CS["S-JTSK [JTSK03] / Krovak East North",UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
            'AXIS["Easting",EAST],AXIS["Northing",NORTH],AUTHORITY["EPSG","8353"]],'
            'VERT_CS["Baltic 1957 height",VERT_DATUM["Baltic 1957",2005,AUTHORITY["EPSG","1202"]],'
            'UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Gravity-related height",UP],AUTHORITY["EPSG","8357"]]]')


def _synthetic_zip(tmp_path):
    """A 1 m raster in S-JTSK with centimetre heights and a hole, zipped like ÚGKK's: its CRS is
    named only, as in the national TIFFs (no projection, so the tiles must get EPSG:8353)."""
    import zipfile
    rng = np.random.default_rng(3)
    left, top, width, height = -594413.0, -1132310.0, 5000, 3000   # pixel edges on whole metres
    band = (300 + rng.random((height, width)) * 50).round(2).astype(np.float32)
    band[:, :700] = -9999.0                                       # no data on the west
    tif = tmp_path / "t.tif"
    with rio.open(tif, "w", driver="GTiff", width=width, height=height, count=1, dtype="float32", crs=UGKK_CRS,
                  transform=from_origin(left, top, 1, 1), nodata=-9999.0, tiled=True, blockxsize=128,
                  blockysize=128, compress="lzw") as dst:
        dst.write(band, 1)
    work = tmp_path / "work"
    work.mkdir()
    with zipfile.ZipFile(work / "test.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tif, "t.tif")
    return work, band, left, top


def _cut(tmp_path, monkeypatch, extract=False):
    """The synthetic raster cut by tools/sk_tiles.py into a mirror folder (<mirror>/slovakia/test/1m),
    from the ZIP or from the TIFF extracted beside it."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import sk_tiles
    work, band, left, top = _synthetic_zip(tmp_path)
    monkeypatch.setitem(sk_tiles.PRODUCTS, "test", {"zip": "https://example.org/test.zip", "member": "t.tif",
                                                   "title": "test"})
    mirror = tmp_path / "mirror"
    if extract:
        assert sk_tiles.extract("test", str(work)) == sk_tiles.extracted("test", str(work))
    sk_tiles.cut("test", str(work), str(mirror / "slovakia"))
    assert (mirror / "slovakia" / "README.md").exists()   # the dataset card goes with the tiles
    return mirror, mirror / "slovakia" / "test" / "1m", band, left, top


def _expected(band, grid, left, top):
    c0, r0 = round(grid.left - left), round(top - grid.top)
    expected = band[r0:r0 + grid.height, c0:c0 + grid.width].astype(np.float64)
    expected[expected == -9999.0] = np.nan
    return expected


def test_slovak_tiles_cut_and_downloaded(tmp_path, monkeypatch):
    """tools/sk_tiles.py cuts a zipped raster into 2 km tiles; GridTiles brings them back onto a grid."""
    import grid as gd
    _, root, band, left, top = _cut(tmp_path, monkeypatch)
    with open(root / "index.json", encoding="utf-8") as f:
        index = json.load(f)
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
    expected = _expected(band, grid, left, top)
    both = np.isfinite(expected)
    assert np.array_equal(np.isfinite(mosaic), both)                   # no data stays no data
    assert np.nanmax(np.abs(mosaic[both] - expected[both])) <= 0.005    # LERC: within 5 mm
    # A tile outside the data is recorded as missing, not fetched again
    outside = (math.floor(left / 2000) - 5, math.floor(top / 2000))
    assert layer.download_tile(outside, str(tmp_path / "x.tif")).received == 0


def test_slovak_tiles_from_the_local_mirror(tmp_path, monkeypatch):
    """Before the upload GridTiles copies the tiles from the folder made by tools/sk_tiles.py."""
    import grid as gd
    import cache
    mirror, root, band, left, top = _cut(tmp_path, monkeypatch, extract=True)
    monkeypatch.setattr(src, "_get", lambda *a, **k: pytest.fail("no network with a mirror"))
    layer = src.GridTiles("Test DSM", [], "EPSG:8353", "slovakia/test/1m", 2000, 1.0, 6e6, mirror="slovakia/test/1m")
    monkeypatch.delenv(src.MIRROR_ENV, raising=False)
    assert "set FINDALINE_MIRROR" in layer.unavailable()
    monkeypatch.setenv(src.MIRROR_ENV, str(tmp_path / "elsewhere"))
    assert "does not exist" in layer.unavailable()
    monkeypatch.setenv(src.MIRROR_ENV, str(mirror))
    assert layer.unavailable() is None

    grid = gd.make_grid((left + 500, top - 2800, left + 4600, top - 100), 1.0, "EPSG:8353")
    cache_dir = str(tmp_path / "cache")
    layer.download(grid, cache_dir, None, "test")
    mosaic = src.build_mosaic(layer.files(grid, cache_dir), grid, str(tmp_path / "m.npy"), "test")
    expected = _expected(band, grid, left, top)
    both = np.isfinite(expected)
    assert np.array_equal(np.isfinite(mosaic), both)
    assert np.nanmax(np.abs(mosaic[both] - expected[both])) <= 0.005
    assert {cache.state(cache_dir, layer.tile_path(cache_dir, t)) for t in layer.tiles(grid)} == {"ok"}
    with open(root / "index.json", encoding="utf-8") as f:
        index = json.load(f)["tiles"]
    # A tile without data (null in the index) is outside the data, recorded as missing
    empty = tuple(map(int, next(k for k, v in index.items() if v is None).split("_")))
    assert layer.download_tile(empty, str(tmp_path / "empty.tif")).received == 0
    assert not (tmp_path / "empty.tif").exists()

    # A tile not cut yet is an error, never "missing"; a damaged file is refused
    later = (math.floor(left / 2000), math.floor(top / 2000) + 3)
    assert f"{later[0]}_{later[1]}" not in index
    with pytest.raises(RuntimeError, match="not in .* yet"):
        layer.download_tile(later, str(tmp_path / "later.tif"))
    key = next(k for k, v in index.items() if v)
    tile = tuple(map(int, key.split("_")))
    with open(root / src.grid_tile_name(*tile), "ab") as f:
        f.write(b"x")
    with pytest.raises(RuntimeError, match="sha256"):
        layer.download_tile(tile, str(tmp_path / "damaged.tif"))



def test_slovakia_chosen_only_with_its_tiles(tmp_path, monkeypatch):
    """--source auto picks Slovakia only when a copy of the tiles is set up, else the Europe-wide model."""
    monkeypatch.setattr(src, "country_code", lambda lat, lon, cache_dir: "sk")
    monkeypatch.delenv(src.MIRROR_ENV, raising=False)
    source, reason = src.choose_source("auto", 49.17, 18.58, str(tmp_path))
    assert source.id == "europe" and "FINDALINE_MIRROR" in reason
    with pytest.raises(ValueError, match="FINDALINE_MIRROR"):
        src.choose_source("slovakia", 49.17, 18.58, str(tmp_path))
    for product in ("dmr5", "dmp1"):
        (tmp_path / "mirror" / "slovakia" / product / "1m").mkdir(parents=True)
    monkeypatch.setenv(src.MIRROR_ENV, str(tmp_path / "mirror"))
    source, reason = src.choose_source("auto", 49.17, 18.58, str(tmp_path))
    assert source.id == "slovakia" and source.default_resolution == 1.0 and source.crs == "EPSG:8353"

def _stac_tile(east, north, res, year, tile_m=100):
    """A COG of one STAC tile like swisstopo's: float32, -9999 as nodata, heights telling the year apart."""
    size = round(tile_m / res)
    band = np.full((size, size), 1000 + year % 100, np.float32)
    band[0, 0] = -9999.0
    with MemoryFile() as memfile:
        with memfile.open(driver="GTiff", width=size, height=size, count=1, dtype="float32", crs="EPSG:2056",
                          nodata=-9999.0, transform=from_origin(east * tile_m, (north + 1) * tile_m, res, res)) as dst:
            dst.write(band, 1)
        return memfile.read()


class FakeStac:
    """A STAC API with items <product>_<year>_<E>-<N> holding GeoTIFFs at 0.5 and 2 m (and an XYZ zip)."""

    def __init__(self, items):
        self.items, self.files, self.asked = items, {}, []
        for year, east, north in items:
            self._add(year, east, north)

    def _add(self, year, east, north):
        for res in (0.5, 2.0):
            self.files[f"https://files.example/t_{year}_{east}-{north}_{res:g}.tif"] = _stac_tile(east, north, res, year)

    def item(self, year, east, north):
        assets = {}
        for res in (0.5, 2.0):
            url = f"https://files.example/t_{year}_{east}-{north}_{res:g}.tif"
            sha = hashlib.sha256(self.files[url]).hexdigest()
            assets[url] = {"href": url, "gsd": res, "file:checksum": "1220" + sha,
                           "type": "image/tiff; application=geotiff; profile=cloud-optimized"}
            assets[url[:-4] + ".xyz.zip"] = {"href": url[:-4] + ".xyz.zip", "gsd": res,
                                             "type": "application/x.ascii-xyz+zip"}
        return {"id": f"test_{year}_{east}-{north}", "assets": assets}

    def get(self, url, params=None, **kwargs):
        """The items endpoint in pages of two (bbox ignored: every item is near)."""
        if url.startswith("offline"):
            raise src.requests.ConnectionError("offline")
        start = int(url.split("page=")[1]) if "page=" in url else 0
        self.asked.append(start)
        page = [self.item(*i) for i in self.items[start:start + 2]]
        links = [{"rel": "next", "href": f"https://stac.example/items?page={start + 2}"}]             if start + 2 < len(self.items) else []
        response = FakeResponse()
        response.json = lambda: {"features": page, "links": links}
        return response

    def download(self, url, params=None, **kwargs):
        return FakeResponse(self.files[url]) if url in self.files else None


def test_stac_tiles(tmp_path, monkeypatch):
    """StacTiles: newest survey per tile, the resolution for the grid, checksums, updates
    replacing older surveys, the cached tiles when the index cannot be reached."""
    import grid as gd
    import osm
    stac = FakeStac([(2019, 26000, 12000), (2025, 26000, 12000), (2019, 26001, 12000)])
    monkeypatch.setattr(src.requests, "get", stac.get)
    monkeypatch.setattr(src, "_get", stac.download)
    monkeypatch.setattr(src, "_sleep", lambda attempt: None)
    make = lambda: src.StacTiles("Test", "https://stac.example", "test", "ch/test", "EPSG:2056",
                                 sizes={0.5: 1e5, 2.0: 1e4}, tile_m=100)
    layer, cache_dir = make(), str(tmp_path / "cache")
    coarse = gd.make_grid((2600010, 1200010, 2600190, 1200090), 2.0, "EPSG:2056")
    fine = gd.make_grid((2600010, 1200010, 2600190, 1200090), 1.0, "EPSG:2056")

    assert layer.estimate(coarse, cache_dir, None) == (2, 2, 0.02)
    assert stac.asked == [0, 2]                                    # both pages of the index
    layer.download(coarse, cache_dir, None, "test")
    folder = tmp_path / "cache" / "ch" / "test"
    assert sorted(os.listdir(folder / "2m")) == ["2019_26001-12000.tif", "2025_26000-12000.tif"]
    mosaic = src.build_mosaic(layer.files(coarse, cache_dir), coarse, str(tmp_path / "m.npy"), "test")
    assert np.nanmax(mosaic[:, :45]) == 1025 and np.nanmin(mosaic[:, 45:]) == 1019  # newest year per tile
    assert np.isnan(mosaic).sum() == 0                             # the corner nodata lies outside the grid
    layer.download(fine, cache_dir, None, "test")                  # a 1 m grid takes the 0.5 m files
    assert sorted(os.listdir(folder / "0.5m")) == ["2019_26001-12000.tif", "2025_26000-12000.tif"]
    assert layer.used(coarse, cache_dir)["survey_years"] == [2019, 2025]

    # A newer survey: the update downloads it for both resolutions and deletes the older files
    stac.items.append((2026, 26001, 12000))
    stac._add(2026, 26001, 12000)
    west, south, east, north = src.transform_bounds("EPSG:2056", "EPSG:4326", *coarse.bounds)
    (lat, lon), = osm.tiles(west, south, east, north)
    assert layer.update_index_tile(cache_dir, lat, lon)[0::2] == (2, 2)
    assert sorted(os.listdir(folder / "2m")) == ["2025_26000-12000.tif", "2026_26001-12000.tif"]

    # A file that does not match its checksum is not kept
    stac.files["https://files.example/t_2025_26000-12000_2.tif"] = b"broken"
    with pytest.raises(RuntimeError, match="checksum"):
        layer.download_tile(layer._sheet({"year": 2025, "godlo": "26000-12000", "res": 2.0, "crs": "EPSG:2056",
                                          "url": "https://files.example/t_2025_26000-12000_2.tif",
                                          "sha256": "0" * 64}), str(tmp_path / "x.tif"))
    assert not os.path.exists(tmp_path / "x.tif")

    # Offline, with the index due for a check: the cached files of the grid's resolution
    offline = make()
    offline.index_url = "offline"
    assert offline.missing(coarse, cache_dir, "all") == []
    assert sorted(os.path.basename(p) for p in offline.files(coarse, cache_dir)) ==         ["2025_26000-12000.tif", "2026_26001-12000.tif"]
    assert os.path.dirname(offline.files(fine, cache_dir)[0]).endswith("0.5m")


def test_orthophoto_of_tiles_only(monkeypatch):
    """A service without a WMS (SWISSIMAGE): its own tiles, enlarged above its last zoom; the
    requests carry the common User-Agent (cache.http_headers)."""
    import cache
    import orthophoto
    from PIL import Image
    service = src.OrthoService("Fake", None, "", "fake/ortho/live", "test",
                               base_url="https://base/{z}/{x}/{y}.jpg", base_max_zoom=20)
    asked, sent = [], []

    def get(url, params, timeout, headers=None):
        asked.append(url)
        sent.append(headers)
        out = io.BytesIO()
        Image.fromarray(np.full((256, 256, 3), 120, np.uint8)).save(out, "JPEG")
        response = FakeResponse(out.getvalue())
        response.status_code, response.headers = 200, {"content-type": "image/jpeg"}
        return response
    monkeypatch.setattr(orthophoto.requests, "get", get)
    monkeypatch.setenv("FINDALINE_CONTACT", "admin@example.org")
    orthophoto.tile_content(service, 20, 10, 11)
    orthophoto.tile_content(service, 21, 21, 22)
    assert asked == ["https://base/20/10/11.jpg", "https://base/20/10/11.jpg"]
    assert sent == [cache.http_headers()] * 2 and "contact: admin@example.org" in sent[0]["User-Agent"]


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

    def get(url, params, timeout, headers=None):
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


# The USA (USGS 3DEP): blocks of big remote files, here local files at a coarse resolution

def _usgs_file(path, crs, left, top, res, band, nodata=-999999.0):
    """A tiled GeoTIFF like a 3DEP file (nodata -999999); NaN in band becomes nodata."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with rio.open(path, "w", driver="GTiff", width=band.shape[1], height=band.shape[0], count=1, dtype="float32",
                  crs=crs, nodata=nodata, transform=from_origin(left, top, res, res), tiled=True,
                  blockxsize=16, blockysize=16) as dst:
        dst.write(np.where(np.isnan(band), nodata, band).astype(np.float32), 1)


class FakeTnm:
    """The TNM Access API: the products of a dataset, in pages of two (the box is ignored:
    every product is near)."""

    def __init__(self):
        self.products, self.asked, self.offline = [], [], False

    def add(self, path, crs, extent, published="2024-01-01"):
        """A product whose file is at path; extent (in crs) gives its box."""
        west, south, east, north = src.transform_bounds(crs, "EPSG:4326", *extent)
        self.products.append({"downloadURL": path, "publicationDate": published,
                              "boundingBox": {"minX": west, "minY": south, "maxX": east, "maxY": north}})

    def get(self, url, params=None, **kwargs):
        if self.offline:
            raise src.requests.ConnectionError("offline")
        offset = params["offset"]
        self.asked.append((params["datasets"], offset))
        response = FakeResponse()
        response.json = lambda: {"total": len(self.products), "items": self.products[offset:offset + 2]}
        return response


RES = 50.0  # metres: a 10 km tile of the 1 m DEM is 200 + 12 pixels here


def _tile_1m(tnm, folder, project, value, zone=13, x=47, y=443, columns=slice(None), name=None, crs=None,
             published="2024-01-01"):
    """A 10 km tile of the 1 m DEM (6 pixels of overlap around) with data in the given columns."""
    band = np.full((212, 212), np.nan, np.float32)
    band[:, columns] = value
    name = name or f"USGS_1M_{zone}_x{x}y{y}_{project}.tif"
    path = (folder / "Projects" / project / "TIFF" / name).as_posix()
    _usgs_file(path, crs or f"EPSG:{26900 + zone}", x * 10000 - 6 * RES, y * 10000 + 6 * RES, RES, band)
    tnm.add(path, f"EPSG:{26900 + zone}", (x * 10000, (y - 1) * 10000, (x + 1) * 10000, y * 10000), published)
    return path


def test_usgs_1m_blocks(tmp_path, monkeypatch, capsys):
    """The 1 m DEM: blocks composed newest project first (a newer project covering part of a
    tile, older ones filling the rest and read only while needed), files listed but missing or
    in another CRS skipped, old names without a zone, a new project read in by an update with
    the older blocks deleted, the cached blocks when the index cannot be reached."""
    import cache
    import grid as gd
    import osm
    tnm, files = FakeTnm(), tmp_path / "files"
    monkeypatch.setattr(src.requests, "get", tnm.get)
    monkeypatch.setattr(src, "_sleep", lambda attempt: None)
    _tile_1m(tnm, files, "CO_West_2020_B20", 1020, columns=slice(0, 106))     # the western half (E < 475 000)
    _tile_1m(tnm, files, "CO_Full_2013", 1013)
    _tile_1m(tnm, files, "CO_Older_2010", 1010, name="USGS_one_meter_x47y443_CO_Older_2010.tif")  # no zone
    tnm.add((files / "Projects" / "CO_Gone_2024" / "TIFF" / "USGS_1M_13_x47y443_CO_Gone_2024.tif").as_posix(),
            "EPSG:26913", (470000, 4420000, 480000, 4430000))                 # listed, but not there
    _tile_1m(tnm, files, "CO_Wrong_2023", 1023, crs="EPSG:26912")              # not in its zone's CRS
    make = lambda: src.Usgs1mBlocks("Test 1 m", "Test DEM", "test1m", "usa/test1m", native_res=RES,  # noqa: E731
                                    block_m=1000, bytes_per_tile=1e4)
    layer, cache_dir = make(), str(tmp_path / "cache")
    grid = gd.make_grid((474100, 4424100, 475900, 4425900), RES, "EPSG:26913")

    assert layer.estimate(grid, cache_dir, None) == (4, 4, 0.04)                # 2 x 2 blocks of 1 km
    assert tnm.asked == [("Test DEM", 0), ("Test DEM", 2), ("Test DEM", 4)]   # three pages of the index
    block = layer.tiles(grid)[0]
    assert (block.crs, block.year) == ("EPSG:26913", 2024)
    assert [n for n, _, _ in block.files] == ["USGS_1M_13_x47y443_CO_Gone_2024", "USGS_1M_13_x47y443_CO_Wrong_2023",
                                              "USGS_1M_13_x47y443_CO_West_2020_B20",
                                              "USGS_1M_13_x47y443_CO_Full_2013", "USGS_one_meter_x47y443_CO_Older_2010"]
    layer.download(grid, cache_dir, None, "test")
    assert "CO_Wrong_2023 is in EPSG:26912, not EPSG:26913; not used" in capsys.readouterr().out
    mosaic = src.build_mosaic(layer.files(grid, cache_dir), grid, str(tmp_path / "m.npy"), "test")
    assert np.isnan(mosaic).sum() == 0
    assert (mosaic[:, :18] == 1020).all() and (mosaic[:, 18:] == 1013).all()  # the 2020 half, then 2013
    used = layer.used(grid, cache_dir)
    assert used["source_files"] == ["USGS_1M_13_x47y443_CO_Full_2013", "USGS_1M_13_x47y443_CO_West_2020_B20"]
    assert used["survey_years"] == [2013, 2020]                                # 2010 was never needed
    folder = tmp_path / "cache" / "usa" / "test1m" / "epsg26913" / "47_442"
    names = sorted(os.listdir(folder))
    assert len(names) == 4 and all(re.fullmatch(r"2024_47[45]_442[45]_[0-9a-f]{8}\.tif", n) for n in names)

    # A new project over the tile: an update reads the blocks again and deletes the old ones
    _tile_1m(tnm, files, "CO_New_2025", 1025, published="2026-01-01")
    west, south, east, north = src.transform_bounds("EPSG:26913", "EPSG:4326", *grid.bounds)
    results = [layer.update_index_tile(cache_dir, lat, lon) for lat, lon in osm.tiles(west, south, east, north)]
    assert sum(r[0] for r in results) == 4 and sum(r[2] for r in results) == 4
    assert len(os.listdir(folder)) == 4 and all(n.startswith("2025_") for n in os.listdir(folder))
    mosaic = src.build_mosaic(make().files(grid, cache_dir), grid, str(tmp_path / "m2.npy"), "test")
    assert (mosaic == 1025).all()

    # Offline and without the index answers: the cached blocks
    tnm.offline = True
    cache.forget(cache_dir, [str(p) for p in (tmp_path / "cache" / "usa" / "sheet-index").rglob("*.json")])
    offline = make()
    assert offline.estimate(grid, cache_dir, None) == (4, 0, 0.0)
    mosaic = src.build_mosaic(offline.files(grid, cache_dir), grid, str(tmp_path / "m3.npy"), "test")
    assert (mosaic == 1025).all()
    assert offline.has_data_near(37.0, -105.0, cache_dir) is None


def test_usgs_1m_zone_of_old_names():
    """A tile named without its zone gets the zone in which its extent lands on its box."""
    extent = [470000, 4420000, 480000, 4430000]
    west, south, east, north = src.transform_bounds("EPSG:26913", "EPSG:4326", *extent)
    assert src._utm_zone_of({"minX": west, "minY": south, "maxX": east, "maxY": north}, extent) == 13
    # near the edge of a zone the tile of the next zone is told apart by its box
    extent = [760000, 4420000, 770000, 4430000]
    west, south, east, north = src.transform_bounds("EPSG:26912", "EPSG:4326", *extent)
    assert int(((west + east) / 2 + 180) // 6) + 1 == 13 and \
        src._utm_zone_of({"minX": west, "minY": south, "maxX": east, "maxY": north}, extent) == 12
    assert src._survey_year("https://x/1m/Projects/UT_WestEast_B22/TIFF/a.tif", "2024-10-30") == 2022
    assert src._survey_year("https://x/1m/Projects/CA_YosemiteNP_2019_D19/TIFF/a.tif", "2021") == 2019
    assert src._survey_year("https://x/1m/Projects/Some_Project/TIFF/a.tif", "2021-06-30") == 2021


ALBERS_GRS80 = "+proj=aea +lat_0=50 +lon_0=-154 +lat_1=55 +lat_2=65 +x_0=0 +y_0=0 +ellps=GRS80 +units=m +no_defs"


def test_ifsar_blocks(tmp_path, monkeypatch):
    """Alaska's IfSAR: cells of varying extent in Alaska Albers named by their ellipsoid only
    (as USGS's files are), composed into blocks on a fixed grid of EPSG:3338."""
    import grid as gd
    tnm, files = FakeTnm(), tmp_path / "files"
    monkeypatch.setattr(src.requests, "get", tnm.get)
    for name, left, value in (("a", 1100000, 300), ("b", 1107500, 400)):  # meet inside a 5 km block
        path = (files / "Projects" / "AK_IFSAR_2012" / f"Cell_{name}_2012" / "TIFF" / f"USGS_AK5M_{name}.tif")
        _usgs_file(path.as_posix(), rio.crs.CRS.from_proj4(ALBERS_GRS80), left, 1200000, RES,
                   np.full((100, 150), value, np.float32))
        path = path.as_posix()
        tnm.add(path, "EPSG:3338", (left, 1195000, left + 7500, 1200000))
    layer = src.IfsarBlocks("Test IfSAR", "Test IfSAR DEM", "testifsar", "usa/testifsar", native_res=RES,
                            block_m=5000, bytes_per_tile=1e4)
    cache_dir = str(tmp_path / "cache")
    grid = gd.make_grid((1101000, 1196000, 1114000, 1199000), RES, "EPSG:3338")
    assert layer.estimate(grid, cache_dir, None)[:2] == (3, 3)
    assert [(b.i, b.j) for b in layer.tiles(grid)] == [(220, 239), (221, 239), (222, 239)]
    layer.download(grid, cache_dir, None, "test")
    mosaic = src.build_mosaic(layer.files(grid, cache_dir), grid, str(tmp_path / "m.npy"), "test")
    assert np.isnan(mosaic).sum() == 0
    columns = (1107500 - grid.left) / RES
    assert (mosaic[:, :int(columns)] == 300).all() and (mosaic[:, int(columns):] == 400).all()
    assert src._same_crs(rio.crs.CRS.from_proj4(ALBERS_GRS80), "EPSG:3338")
    assert not src._same_crs("EPSG:26912", "EPSG:26913")


def test_degree_blocks(tmp_path):
    """The 1/3″ DEM: blocks of 0.1° read from 1° files at fixed URLs (6 pixels of overlap
    around), exactly on their pixels; a tile the service does not have is missing."""
    import cache
    import grid as gd
    px = 100  # per degree (3DEP: 10 800)
    band = np.add.outer(np.arange(112) * 10.0, np.arange(112)).astype(np.float32)  # telling every pixel apart
    _usgs_file(str(tmp_path / "files" / "n38w120.tif"), "EPSG:4269", -120 - 6 / px, 38 + 6 / px, 1 / px, band)
    layer = src.DegreeBlocks("Test 1/3", "usa/test13/live", lambda name: str(tmp_path / "files" / f"{name}.tif"),
                             "EPSG:4269", px_per_degree=px, native_res=1000)
    cache_dir = str(tmp_path / "cache")
    x, y = src.rio.warp.transform("EPSG:4326", "EPSG:26911", [-119.55], [37.95])
    grid = gd.make_grid((x[0] - 2000, y[0] - 2000, x[0] + 2000, y[0] + 9000), 100, "EPSG:26911")
    tiles = layer.tiles(grid)
    assert (-1196, 379) in tiles and (-1196, 380) in tiles                     # 38.0° N is in the next file
    layer.download(grid, cache_dir, None, "test")
    path = layer.tile_path(cache_dir, (-1196, 379))
    with rio.open(path) as block:                                              # 37.9-38.0° N, 119.6-119.5° W
        assert block.crs.to_epsg() == 4269 and block.shape == (10, 10)
        assert (block.read(1) == band[6:16, 46:56]).all()
    assert cache.state(cache_dir, layer.tile_path(cache_dir, (-1196, 380))) == "missing"   # no n39w120
    relative = cache.relative(cache_dir, path)
    assert layer.tile_of(relative) == (-1196, 379) and layer.tile_of(relative.replace("n38", "n39")) is None
    assert layer.bounds_wgs84((-1196, 379)) == pytest.approx((-119.6, 37.9, -119.5, 38.0))
    assert layer.ttl_days == cache.TTL_DAYS["live"]


def test_usa_chosen_by_country_and_data(tmp_path, monkeypatch):
    """--source auto: the USA (also Puerto Rico) takes the 1 m DEM; Alaska too where it has
    1 m data near the centre, else its IfSAR; the working CRS is NAD83 / UTM."""
    import grid as gd
    codes = {}
    monkeypatch.setattr(src, "country_code", lambda lat, lon, cache_dir: codes[(lat, lon)])
    codes.update({(37.73, -119.64): "us", (18.2, -66.5): "pr", (58.3, -134.4): "us", (61.2, -149.9): "us"})
    near = {(58.3, -134.4): False, (61.2, -149.9): True}
    monkeypatch.setattr(src.USGS_1M, "has_data_near", lambda lat, lon, cache_dir: near[(lat, lon)])
    assert src.choose_source("auto", 37.73, -119.64, str(tmp_path))[0].id == "usa"
    assert src.choose_source("auto", 18.2, -66.5, str(tmp_path))[0].id == "usa"
    source, reason = src.choose_source("auto", 58.3, -134.4, str(tmp_path))
    assert source.id == "alaska_ifsar" and "no terrain data near it" in reason and source.default_resolution == 5
    assert src.choose_source("auto", 61.2, -149.9, str(tmp_path))[0].id == "alaska"
    assert src.SOURCES["alaska"].fallbacks == ["alaska_ifsar", "usgs10m"]
    assert src.SOURCES["usa"].working_crs(37.73, -119.64) == "EPSG:26911"
    assert src.SOURCES["europe"].working_crs(37.73, -119.64) == "EPSG:32611"
    assert gd.nad83_utm_crs(58.3, -134.4) == "EPSG:26908" and gd.nad83_utm_crs(13.4, 144.8) == "EPSG:32655"


def test_gaps_filled_in_turn(tmp_path):
    """A chain of fallbacks: the second fills only what the first left, the mask holds both."""
    import grid as gd
    grid = gd.make_grid((500000, 300000, 500100, 300100), 10, "EPSG:2180")
    elevation = np.full((grid.height, grid.width), 100.0, np.float32)
    elevation[:, :4] = np.nan                                                  # a gap of 4 columns
    path = str(tmp_path / "nmt.npy")
    np.save(path, elevation)
    sources = []
    for name, value, columns in (("first", 110.0, slice(0, 2)), ("second", 120.0, slice(None))):
        band = np.full((grid.height, grid.width), np.nan, np.float32)
        band[:, columns] = value
        band[:, 4:] = 100.0                                                    # where both meet the data
        file = str(tmp_path / f"{name}.tif")
        _usgs_file(file, "EPSG:2180", grid.left, grid.top, 10, band)
        sources.append(src.Source(id=name, name=name, description=name, nmt=src.FileLayer(name, file)))
    out, counts = src.fill_gaps_chain(path, None, grid, sources, str(tmp_path / "cache"), str(tmp_path / "filled.npy"))
    filled = np.load(out)
    assert [(s.id, n) for s, n in counts] == [("first", 2 * grid.height), ("second", 2 * grid.height)]
    assert filled[:, :4].all() and not filled[:, 4:].any()
    result = np.load(path)
    assert (result[:, :2] == 110).all() and (result[:, 2:4] == 120).all()
