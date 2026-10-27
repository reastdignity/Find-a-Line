"""Fast checks of single parts: search areas, the parameter schema, progress listeners, the cache index."""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import area as ar  # noqa: E402
import cache  # noqa: E402
import grid as gd  # noqa: E402
import params as pm  # noqa: E402
import sources as src  # noqa: E402
import update  # noqa: E402
from progress import progress  # noqa: E402


def test_polygon_mask_matches_circle():
    circle = ar.Circle(50.1559508, 19.7562722, 500)
    grid = gd.make_grid(circle.bounds("EPSG:2180"), 2, "EPSG:2180", (0.5, 0.5))
    polygon = ar.Polygon(circle.shape_wgs84())
    a, b = circle.mask(grid), polygon.mask(grid)
    assert a.sum() == pytest.approx(np.pi * 250 ** 2, rel=0.01)
    assert (a != b).sum() < 0.01 * a.sum()  # only pixels on the boundary differ


def test_circle_grid_snaps_to_origin():
    circle = ar.Circle(50.1559508, 19.7562722, 1000)
    grid = gd.make_grid(circle.bounds("EPSG:2180"), 2, "EPSG:2180", (0.5, 0.5))
    assert (grid.left - 0.5) % 2 == 0 and (grid.top - 0.5) % 2 == 0
    assert grid.width == grid.height == 1001


@pytest.mark.parametrize("values, message", [
    ({"lat": 50}, "both --lat and --lon are required"),
    ({"lat": 50, "lon": 19, "northing": 1, "easting": 2}, "either --lat/--lon or --northing/--easting"),
    ({"lat": 50, "lon": 19, "min_length": 50, "max_length": 40}, "--min-length must be positive"),
    ({"lat": 50, "lon": 19, "radius": 0}, "--radius must be positive"),
    ({"lat": 50, "lon": 19, "source": "mars"}, "unknown --source mars"),
])
def test_params_validation(values, message):
    with pytest.raises(ValueError, match=message):
        pm.SearchParams(**values)


def test_params_defaults_and_cli():
    [params], options = pm.parse_cli(["--lat", "50", "--lon", "19", "--nmpt", "off", "-y"])
    assert params.radius == 1000 and params.nmpt_mode is None and options.yes
    assert pm.SearchParams(lat=50, lon=19).nmpt_mode == "mark"


def test_points():
    searches, _ = pm.parse_cli(["--point", "50.1,19.7,Żabi Koń, Kobylany", "--point", "35.85,14.38",
                                "--radius", "500"])
    assert [(p.lat, p.lon, p.name, p.radius) for p in searches] == [
        (50.1, 19.7, "Żabi Koń, Kobylany", 500), (35.85, 14.38, None, 500)]
    with pytest.raises(SystemExit):
        pm.parse_cli(["--point", "50.1,19.7", "--lat", "50"])
    with pytest.raises(SystemExit):
        pm.parse_cli(["--point", "50.1"])


def test_progress_listener():
    events = []
    progress.subscribe(events.append)
    try:
        progress.compute.start("test stage", 3, unit="step")
        progress.compute.update(2, note="x")
        progress.compute.finish()
    finally:
        progress.unsubscribe(events.append)
    assert [e["count"] for e in events] == [0, 2, 3]
    assert events[-1]["done"] and events[0]["stage"] == "test stage"


def _age(cache_dir, path, days):
    """Pretend a file was checked (and fetched) `days` ago."""
    import sqlite3
    from datetime import datetime, timedelta, timezone
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    top, inner = cache.relative(cache_dir, path).split("/", 1)
    with sqlite3.connect(os.path.join(cache_dir, top, "index.sqlite")) as db:
        db.execute("UPDATE files SET checked_at = ?, fetched_at = ? WHERE path = ?", (when, when, inner))
    db.close()


def test_cache_same_content_keeps_fetch_time(tmp_path):
    path = tmp_path / "spain" / "mdt05" / "live" / "t" / "0_0.tif"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"a")
    cache.record(str(tmp_path), str(path))
    _age(str(tmp_path), str(path), 1000)
    old = cache.info(str(tmp_path), str(path))
    cache.record(str(tmp_path), str(path))           # downloaded again, same content
    same = cache.info(str(tmp_path), str(path))
    assert same["fetched_at"] == old["fetched_at"] and same["checked_at"] > old["checked_at"]
    path.write_bytes(b"b")
    cache.record(str(tmp_path), str(path))           # new content
    assert cache.info(str(tmp_path), str(path))["fetched_at"] > old["fetched_at"]


@pytest.mark.parametrize("status, age, ttl, update, expected", [
    ("ok", 800, 730, None, False),       # never without an update
    ("ok", 800, None, "all", False),     # versioned releases never change
    ("ok", 10, 730, "all", True),
    ("ok", 10, 730, "stale", False),
    ("ok", 800, 730, "stale", True),
    ("missing", 100, 730, "stale", True),  # missing: after MISSING_TTL_DAYS
    ("missing", 100, None, "stale", False),
])
def test_cache_stale(status, age, ttl, update, expected):
    from datetime import datetime, timedelta, timezone
    row = {"status": status, "checked_at": (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()}
    assert cache.stale(row, ttl, update) is expected


def test_update_checks(tmp_path):
    """Live tiles are checked after their interval, releases never; --bbox limits the checks."""
    spain = src.SOURCES["spain"].nmt
    malta = src.SOURCES["malta"].nmt
    live = spain.tile_path(str(tmp_path), (5.0, 100, 2000))
    release = malta.tile_path(str(tmp_path), (1.0, 230, 1790))
    for path in (live, release):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(b"x")
        cache.record(str(tmp_path), path)
        _age(str(tmp_path), path, 1000)
    assert spain.tile_of(cache.relative(str(tmp_path), live)) == (5.0, 100, 2000)
    assert spain.tile_of(cache.relative(str(tmp_path), release)) is None
    found = update.checks(str(tmp_path))
    assert [c.path for c in found] == [cache.relative(str(tmp_path), live)]
    west, south, east, north = found[0].extent
    assert update.checks(str(tmp_path), bbox=(west - 1, south - 1, west - 0.5, south - 0.5)) == []
    assert len(update.checks(str(tmp_path), bbox=(west, south, west, south))) == 1


def test_covered_metres_match_profiles():
    """The metres over water of every line (numba) equal those of its profile."""
    import helpers as sh
    import linesearch as ls
    rng = np.random.default_rng(5)
    elevation = rng.random((200, 200)) * 10
    mask = rng.random((200, 200)) < 0.3
    a1 = rng.integers(0, 200, (300, 2))
    a2 = rng.integers(0, 200, (300, 2))
    keep = np.abs(a2 - a1).max(axis=1) >= 2
    a1, a2 = a1[keep], a2[keep]
    fast = ls.covered_metres(a1, a2, 2.0, mask)
    slow = [sh.line_profile(tuple(p), tuple(q), elevation, 2.0, water=mask)["water_m"] for p, q in zip(a1, a2)]
    assert fast == slow


@pytest.mark.parametrize("model", ["formula", "sag"])
def test_evaluate_lines_matches_evaluate_line(model):
    """The numba check gives exactly (==) the decisions and values of helpers.evaluate_line."""
    import helpers as sh
    import linesearch as ls
    import sag
    rng = np.random.default_rng(7)
    terrain = (rng.random((120, 120)) * 30 + 100).astype(np.float32)
    terrain[rng.random((120, 120)) < 0.002] = np.nan          # a few pixels without data
    surface = (terrain + rng.random((120, 120)) * 15).astype(np.float32)
    surface[rng.random((120, 120)) < 0.3] = np.nan            # no surface model in places
    terrain[:, :8] += 60                                     # cliffs on both sides, so lines pass
    terrain[:, -8:] += 60
    anchors = np.column_stack([rng.integers(0, 120, 400),  # on the cliffs, and some anywhere
                               np.where(rng.random(400) < 0.5, rng.integers(0, 8, 400), rng.integers(112, 120, 400))])
    anchors[:50] = rng.integers(0, 120, (50, 2))
    found = rng.integers(0, 400, (3000, 2))
    table = sag.EnvelopeTable(sag.Rigging(), 20, 200) if model == "sag" else None
    for mode, high_above in (("mark", sh.HIGH_ABOVE), ("reject", 35.0)):
        fast = ls.evaluate_lines(found, anchors, terrain, 1.0, surface, table, mode, high_above)
        down = sh.downslope(terrain, anchors, 1.0)
        angles = np.column_stack(sh.wall_angles(anchors[found[:, 0]], anchors[found[:, 1]],
                                                down[found[:, 0]], down[found[:, 1]])).tolist()
        slow = []
        for (i, j), (angle1, angle2) in zip(found, angles):
            result = sh.evaluate_line(anchors[i], anchors[j], terrain, 1.0, surface, table, high_above)
            if sh.is_valid(result, mode):  # as find_valid_lines always made them
                elev_a1, elev_a2, length, clearance, clearance_nmpt, *heights = result
                slow.append(sh.Line(tuple(map(int, anchors[i])), tuple(map(int, anchors[j])),
                                    length, elev_a1, elev_a2, clearance, clearance_nmpt, *heights, angle1, angle2))
        assert slow and fast == slow
        assert 0 < sum(0 < line.high_pct < 100 for line in fast)  # the threshold is crossed


def test_wall_angles():
    """The downslope direction of a slope and the angle of lines leaving it."""
    import helpers as sh
    rows, cols = np.mgrid[0:20, 0:20]
    terrain = 100.0 - 2.0 * cols  # falls towards the east (growing col)
    terrain[5, 5] = np.nan        # no data next to an anchor: its elevation is used instead
    down = sh.downslope(terrain, [(10, 10), (4, 4), (0, 0)], 1.0)
    assert np.allclose(down[0], [0, 1])
    # next to no data and at the corner of the grid: still a direction, roughly east
    assert np.allclose(np.hypot(down[1:, 0], down[1:, 1]), 1) and (down[1:, 1] > 0.9).all()
    a1, a2 = np.array([[10, 10], [10, 10], [10, 10]]), np.array([[10, 18], [2, 10], [18, 18]])
    angle1, angle2 = sh.wall_angles(a1, a2, down[[0, 0, 0]], down[[0, 0, 0]])
    assert np.allclose(angle1, [0, 90, 45]) and np.allclose(angle2, [180, 90, 135])  # east, north, south-east
    assert np.array_equal(sh.downslope(np.full((3, 3), 5.0), [(1, 1)], 1.0), [[0, 0]])  # flat: 90° to all
    assert np.allclose(sh.wall_angles(np.array([[1, 1]]), np.array([[1, 2]]), np.zeros((1, 2)), np.zeros((1, 2))), 90)


def test_webbing_heights():
    """Heights of the webbing above the terrain: margin + leash fall (formula) or fall zone (sag)."""
    import helpers as sh
    import sag
    terrain = np.full((3, 21), 100.0)
    terrain[1, 0] = terrain[1, 20] = 140.0  # two anchors 20 m apart, 40 m above the flat ground
    _, _, length, clearance, _, height_max, height_mean, high_pct = sh.evaluate_line(
        (1, 0), (1, 20), terrain, 1.0, high_above=39.0)
    # the formula's webbing: 140 - 0.15 * (distance to the nearer anchor), lowest in the middle
    assert abs(height_max - (40 - 0.15)) < 1e-9 and abs(clearance - (40 - 0.15 * 10 - sh.LEASH_FALL)) < 1e-9
    assert abs(height_mean - (40 - 0.15 * sum(min(k, 20 - k) for k in range(1, 20)) / 19)) < 1e-9
    assert high_pct == 100.0 * 12 / 19  # 39 m or more: within 6 m of an anchor, 2 x 6 of 19 pixels
    profile = sh.line_profile((1, 0), (1, 20), terrain, 1.0)
    assert profile["max_height"] == [1.0, 39.85]
    # sag: the chord (140) minus the sag under the athlete, so the highest point is next to an anchor
    table = sag.EnvelopeTable(sag.Rigging(fall_zone=3), 10, 40)
    t = np.arange(1, 20) / 20
    webbing = sh.max_safe_elevation(t, 20.0, 140.0, 140.0, table) + 3
    result = sh.evaluate_line((1, 0), (1, 20), terrain, 1.0, sag_table=table)
    assert result[5] == float(np.max(webbing - 100.0)) and result[6] == float(np.cumsum(webbing - 100.0)[-1]) / 19


def test_coast_within_osm_keeps_islets_and_the_sea_side(monkeypatch):
    """coast within osm:<name>: the coastline within the reach of the area, not clipped to its land."""
    import osm
    from shapely.geometry import Point, box
    m = 1 / 111320  # about a metre in degrees here (near the equator)
    island = box(0, 0, 1000 * m, 1000 * m)
    rings = {"island": island.exterior, "islet": Point(1300 * m, 500 * m).buffer(50 * m).exterior,
             "far": Point(2600 * m, 500 * m).buffer(50 * m).exterior, "away": Point(5000 * m, 500 * m).buffer(50 * m).exterior}
    monkeypatch.setattr(osm, "search", lambda name, cache_dir: {"geojson": island.__geo_interface__})
    monkeypatch.setattr(osm, "theme", lambda theme, *bounds_and_cache: [
        {"id": k, "geometry": g} for k, g in rings.items()])
    area = ar.from_spec("coast", 100, "osm:Island", "unused", reach=1000)
    assert area.geometry.contains(Point(1300 * m, 550 * m))     # the islet 300 m off the coast
    assert area.geometry.contains(Point(1050 * m, 500 * m))     # the sea side of the coastline
    assert not area.geometry.contains(Point(2600 * m, 550 * m))  # 1.6 km away: left out...
    assert len(area.notes) == 1 and "1 piece(s)" in area.notes[0]  # ...with a note; 4 km away: nothing
    clipped = ar.from_spec("coast", 100, f"0,0,{1000 * m},{1000 * m}", "unused", reach=1000)
    assert not clipped.geometry.contains(Point(1050 * m, 500 * m))  # a box clips the band


def _fake_lines(n):
    return [{"anchor1": (50 + k / 1e4, 19.1234567), "anchor2": (50.0001, 19 + k / 1e4), "a1_xy": (k * 0.5, 1.0),
             "a2_xy": (2.0, k / 3), "length": 100 + k / 7, "clearance": k / 3, "clearance_nmpt": None if k % 2 else k / 9,
             "over_obstacles": bool(k % 3), "elev_a1": 400 + k / 11, "elev_a2": 410.0, "water_m": 0.0, "filled_m": k / 13,
             "height_max": 30 + k / 17, "height_mean": 20 + k / 19, "high_pct": k / 0.23, "angle1": k * 7.3,
             "angle2": 90 - k / 3, "profile": None}
            for k in range(n)]


def test_chunked_writes_match_one_dump(tmp_path, monkeypatch):
    """Results, the map data and tile files written in chunks: the same text, the same lines."""
    import json
    import mapping as mp
    import runs as rn
    import search
    lines = _fake_lines(10)
    groups = [{"rep": lines[0], "members": lines[:4]}, {"rep": lines[4], "members": lines[4:]}]
    meta = {"name": "Żabi Koń", "summary": {"lines": 10}}
    for chunk, folder in ((3, tmp_path / "small"), (10_000, tmp_path / "one")):
        monkeypatch.setattr(rn, "LINES_CHUNK", chunk)
        monkeypatch.setattr(mp, "LINES_CHUNK", chunk)
        folder.mkdir()
        counts = []
        rn.save_results(str(folder), groups, meta, lambda n, file: counts.append(n))
        assert sum(counts) == 3 * len(lines)
        value = {"crs": "EPSG:2180", "area": None, "lines": [[k, None, k / 3, "ł"] for k in range(10)]}
        mp._write_js(str(folder / "lines.js"), "window.X", value)
        assert (folder / "lines.js").read_text(encoding="utf-8") == \
            "window.X = " + json.dumps(value, separators=(",", ":")) + ";\n"
    for name in ("lines.csv", "lines.geojson", "run.json"):
        assert (tmp_path / "small" / name).read_bytes() == (tmp_path / "one" / name).read_bytes()
    text = (tmp_path / "small" / "lines.geojson").read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), ensure_ascii=False)

    monkeypatch.setattr(search, "TILE_CHUNK", 3)
    path = str(tmp_path / "0.pkl.gz")
    search._save_tile(path, lines)
    assert search._load_tile(path) == lines
    search._save_tile(path, [])
    assert search._load_tile(path) == []
