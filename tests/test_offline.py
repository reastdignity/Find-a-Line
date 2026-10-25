"""Searches on local files only, no network (any request goes to a closed port).

- A synthetic canyon: two plateaus 40 m above a 50 m wide floor, with a block of
  "trees" taller than the plateaus in its northern third.
- A 600 x 600 m clip of Żabi Koń (tests/data, see make_fixture.py) with known results.
"""

import csv
import glob
import json
import os
import re
import subprocess
import sys

import numpy as np
import pytest
import rasterio as rio
from rasterio.transform import from_origin

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "tests", "data")
OFFLINE_ENV = {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
               "FINDALINE_PROGRESS": "text", "PYTHONIOENCODING": "utf-8"}


def run_search(tmp_path, *args) -> tuple:
    """Run findaline.py offline; return (output, rows of lines.csv)."""
    env = {**os.environ, **OFFLINE_ENV}
    result = subprocess.run(
        [sys.executable, "findaline.py", *args, "--water", "off", "--fill-gaps", "off", "--no-plot", "--yes",
         "--data-dir", str(tmp_path)],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    runs = glob.glob(str(tmp_path / "runs" / "*" / "lines.csv"))
    assert len(runs) == 1, runs
    with open(runs[0], encoding="utf-8-sig") as f:
        return result.stdout, list(csv.DictReader(f))


def write_raster(path, band, left, top):
    with rio.open(path, "w", driver="GTiff", width=band.shape[1], height=band.shape[0], count=1,
                  dtype="float32", crs="EPSG:2180", transform=from_origin(left, top, 1, 1)) as dst:
        dst.write(band.astype(np.float32), 1)


def test_synthetic_canyon(tmp_path):
    left, top, size = 500000, 300000, 300
    terrain = np.full((size, size), 240.0)
    terrain[:, 120:170] = 200.0                 # canyon floor, columns 120-169
    surface = terrain.copy()
    surface[:100, 120:170] = 245.0              # trees above the plateaus in the north
    write_raster(tmp_path / "nmt.tif", terrain, left, top)
    write_raster(tmp_path / "nmpt.tif", surface, left, top)

    output, rows = run_search(tmp_path, "--elevation-file", str(tmp_path / "nmt.tif"),
                              "--surface-file", str(tmp_path / "nmpt.tif"),
                              "--northing", str(top - size / 2), "--easting", str(left + size / 2),
                              "--radius", "140", "--resolution", "1", "--min-length", "20", "--max-length", "200")
    assert rows, output
    for row in rows:
        east = sorted([float(row["anchor1_easting"]), float(row["anchor2_easting"])])
        assert east[0] - left < 120 and east[1] - left >= 170, row   # one anchor on each rim
        assert float(row["length_m"]) >= 50, row
        assert float(row["clearance_m"]) >= 25, row                   # 40 m deep, minus fall zone and sag
    shortest = min(float(r["length_m"]) for r in rows)
    assert shortest == pytest.approx(51, abs=0.5)                     # pixel centres 119.5 and 170.5
    north = [r for r in rows if max(float(r["anchor1_northing"]), float(r["anchor2_northing"])) > top - 90]
    south = [r for r in rows if max(float(r["anchor1_northing"]), float(r["anchor2_northing"])) < top - 110]
    assert north and all(r["over_trees_buildings"] == "True" for r in north)
    assert south and all(r["over_trees_buildings"] == "False" for r in south)


# Known results of the Żabi Koń clip (update together with a deliberate change)
ZABI_KON = {"anchors": 11609, "open": 620, "checked": 11313, "lines": 1303, "spots": 43}


def test_zabi_kon_clip(tmp_path):
    output, rows = run_search(tmp_path, "--elevation-file", os.path.join(DATA, "zabi_kon_nmt.tif"),
                              "--surface-file", os.path.join(DATA, "zabi_kon_nmpt.tif"),
                              "--northing", "254608", "--easting", "553966", "--radius", "290")
    found = {
        "anchors": int(re.search(r"Found (\d+) potential anchors", output).group(1)),
        "open": int(re.search(r"Anchors with an open direction: (\d+)", output).group(1)),
        "checked": int(re.search(r"Checked pixel by pixel: (\d+)", output).group(1)),
        "lines": len(rows),
        "spots": sum(r["representative"] == "True" for r in rows),
    }
    assert found == ZABI_KON, output
    assert "[Download]" not in output  # nothing fetched (and requests would fail anyway)


def _lines(rows):
    return {tuple(sorted([(r["anchor1_easting"], r["anchor1_northing"]), (r["anchor2_easting"], r["anchor2_northing"])]))
            for r in rows}


def test_area_in_tiles(tmp_path):
    """An area computed in tiles gives exactly the lines of one grid."""
    square = {"type": "Polygon", "coordinates": [[[19.7522, 50.1546], [19.7590, 50.1546], [19.7590, 50.1580],
                                                  [19.7522, 50.1580], [19.7522, 50.1546]]]}
    path = tmp_path / "square.geojson"
    path.write_text(json.dumps({"type": "Feature", "properties": {}, "geometry": square}))
    common = ["--elevation-file", os.path.join(DATA, "zabi_kon_nmt.tif"), "--area", str(path),
              "--max-length", "150"]
    one_out, one = run_search(tmp_path / "one", *common)
    tiled_out, tiled = run_search(tmp_path / "tiled", *common, "--tile-size", "150")
    assert "one grid" in one_out and "tiles)" in tiled_out
    assert one and _lines(one) == _lines(tiled)
    assert len(glob.glob(str(tmp_path / "tiled" / "runs" / "*" / "tiles" / "*.pkl.gz"))) > 1


def test_two_points(tmp_path):
    """Two --point searches in one call: one run each, with its own name and data record."""
    env = {**os.environ, **OFFLINE_ENV}
    result = subprocess.run(
        [sys.executable, "findaline.py", "--elevation-file", os.path.join(DATA, "zabi_kon_nmt.tif"),
         "--point", "50.15624,19.75569,West", "--point", "50.15600,19.75630,East", "--radius", "100",
         "--water", "off", "--fill-gaps", "off", "--no-plot", "--yes", "--data-dir", str(tmp_path)],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    runs = sorted(glob.glob(str(tmp_path / "runs" / "*" / "run.json")))
    assert len(runs) == 2, result.stdout
    metas = [json.load(open(path, encoding="utf-8")) for path in runs]
    assert {m["name"] for m in metas} == {"West", "East"}
    assert all(len(m["data"]) == 1 for m in metas)  # each run records only its own data
    assert not os.listdir(tmp_path / "work")       # both work folders deleted
