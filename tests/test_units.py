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
        values = rng.random((len(anchors), 2)) * 50  # prominence, drop of the anchors
        fast = ls.evaluate_lines(found, anchors, terrain, 1.0, surface, table, mode, high_above,
                                 anchor_values=values).lines()
        down = sh.downslope(terrain, anchors, 1.0)
        angles = np.column_stack(sh.wall_angles(anchors[found[:, 0]], anchors[found[:, 1]],
                                                down[found[:, 0]], down[found[:, 1]])).tolist()
        slow = []
        for (i, j), (angle1, angle2) in zip(found, angles):
            result = sh.evaluate_line(anchors[i], anchors[j], terrain, 1.0, surface, table, high_above)
            if sh.is_valid(result, mode):  # as find_valid_lines always made them
                elev_a1, elev_a2, length, clearance, clearance_nmpt, *heights = result
                slow.append(sh.Line(tuple(map(int, anchors[i])), tuple(map(int, anchors[j])),
                                    length, elev_a1, elev_a2, clearance, clearance_nmpt, *heights, angle1, angle2,
                                    values[i, 0], values[j, 0], values[i, 1], values[j, 1]))
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


def _flooded_prominence(z, pixel, span):
    """Prominence by labelling the terrain at or above every level (slow, for the test)."""
    from scipy import ndimage
    e = z[pixel]
    for level in sorted(set(z[~np.isnan(z)].tolist()), reverse=True):
        if level <= e:
            labels, _ = ndimage.label(z >= level, structure=np.ones((3, 3)))
            rows, cols = np.nonzero(labels == labels[pixel])
            if rows.max() - rows.min() + 1 >= span or cols.max() - cols.min() + 1 >= span:
                return e - level
    return np.nan


def test_prominence_matches_flooding():
    import terrain
    rng = np.random.default_rng(3)
    z = np.round(rng.random((40, 50)) * 20, 1).astype(np.float32)
    z[rng.random(z.shape) < 0.05] = np.nan
    anchors = np.argwhere(~np.isnan(z))[rng.integers(0, 1800, 60)]
    anchors = np.vstack([anchors, anchors[:3]])  # several anchors on one pixel
    fast = terrain.prominence(z, anchors, 2.0, 24)  # 12 pixels
    slow = np.array([_flooded_prominence(z, tuple(a), 12) for a in anchors])
    assert np.allclose(fast, slow, equal_nan=True) and np.nanmax(fast) > 3


def test_prominence_and_drop_of_a_tower():
    """A 30 m cone on a plain next to a wall: an anchor on the cone has its height above the
    plain, the edge of the wall none; the drop is the height above the lowest ground near."""
    import terrain
    yy, xx = np.mgrid[0:400, 0:400]
    z = 100.0 + np.maximum(0, 30 - np.hypot(yy - 200, xx - 200) * 1.5)
    z[:, 330:] = 200
    anchors = np.array([[200, 205], [200, 335], [200, 330]])
    assert np.allclose(terrain.prominence(z, anchors, 1.0, 300), [22.5, 0, 0])
    assert np.isnan(terrain.prominence(z, anchors, 1.0, 1000)).all()  # nothing spans more than the grid
    import runs
    assert runs.rounded(float("nan"), 1) is None and runs.rounded(2.345, 1) == 2.3
    assert np.allclose(terrain.drop(z, anchors, 1.0, 50), [22.5, 100, 100])
    assert np.allclose(terrain.drop(z, anchors[1:2], 1.0, 4), [0])  # the wall's foot is 5 px away


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
    import helpers as sh
    return sh.LineTable.from_rows([
        {"a1": (k, 0), "a2": (k, 1), "anchor1": (50 + k / 1e4, 19.1234567), "anchor2": (50.0001, 19 + k / 1e4),
         "a1_xy": (k * 0.5, 1.0), "a2_xy": (2.0, k / 3), "length": 100 + k / 7, "clearance": k / 3,
         "clearance_nmpt": None if k % 2 else k / 9, "over_obstacles": bool(k % 3), "elev_a1": 400 + k / 11,
         "elev_a2": 410.0, "water_m": 0.0, "filled_m": k / 13, "height_max": 30 + k / 17, "height_mean": 20 + k / 19,
         "high_pct": k / 0.23, "angle1": k * 7.3, "angle2": 90 - k / 3, "prom1": np.nan if k % 4 else k / 1.7,
         "prom2": 4.0, "drop1": k * 2.1, "drop2": 30.0,
         "profile": {"d": [0, k], "z": [1.5, None]} if k in (0, 4) else None}
        for k in range(n)])


def test_chunked_writes_match_one_dump(tmp_path, monkeypatch):
    """Results, the map data and tile files written in chunks: the same text, the same lines."""
    import json
    from shapely.geometry import Point
    import helpers as sh
    import mapping as mp
    import runs as rn
    import search

    class Area:
        center = (50.0, 19.1)

        @staticmethod
        def shape_wgs84():
            return Point(19.1, 50.0).buffer(0.01)

    lines = _fake_lines(10)
    groups = mp.Groups(np.array([4, 0]), np.array([4, 5, 6, 7, 8, 9, 3, 0, 1, 2]), np.array([0, 6, 10]))
    meta = {"name": "Żabi Koń", "summary": {"lines": 10}}
    run = {"id": "r", "name": "Żabi Koń", "created": "2026-10-31T10:00:00+00:00"}
    for chunk, folder in ((3, tmp_path / "small"), (10_000, tmp_path / "one")):
        monkeypatch.setattr(rn, "LINES_CHUNK", chunk)
        monkeypatch.setattr(mp, "LINES_CHUNK", chunk)
        folder.mkdir()
        counts = []
        rn.save_results(str(folder), lines, groups, meta, lambda n, file: counts.append(n))
        assert sum(counts) == len(lines)
        counts = []
        mp.save_map(lines, Area, str(folder), str(tmp_path / "runs"), run, "EPSG:2180",
                    on_lines=lambda n, file: counts.append(n))
        assert sum(counts) == len(lines)
    for name in ("lines.csv", "lines.geojson", "run.json", "data.js", "lines.js"):
        assert (tmp_path / "small" / name).read_bytes() == (tmp_path / "one" / name).read_bytes()
    text = (tmp_path / "small" / "lines.geojson").read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), ensure_ascii=False)
    # Group by group, each representative first; NaN and a missing clearance as empty cells
    rows = (tmp_path / "small" / "lines.csv").read_text(encoding="utf-8-sig").splitlines()[1:]
    assert [r.split(",")[:3] for r in rows][:3] == [["1", "True", "6"], ["1", "False", "6"], ["1", "False", "6"]]
    assert [int(float(r.split(",")[3]) * 7 - 700 + 0.5) for r in rows] == [4, 5, 6, 7, 8, 9, 0, 3, 1, 2]
    for name in ("data.js", "lines.js"):
        text = (tmp_path / "small" / name).read_text(encoding="utf-8")
        value = json.loads(text[text.index("= {") + 2:-2])
        assert text.endswith("};\n") and len(value["lines"]) == 10
    data = (tmp_path / "small" / "data.js").read_text(encoding="utf-8")
    run_value = json.loads(data[data.index("= {") + 2:-2])
    assert [row[-1] for row in run_value["lines"]][:5] == [{"d": [0, 0], "z": [1.5, None]}, None, None, None,
                                                           {"d": [0, 4], "z": [1.5, None]}]

    path = str(tmp_path / "0.table.pkl.gz")
    search._save_tile(path, lines)
    loaded = search._load_tile(path)
    assert loaded.profiles == lines.profiles and loaded.columns.keys() == lines.columns.keys()
    assert all(np.array_equal(loaded.columns[k], lines.columns[k], equal_nan=True) for k in lines.columns)
    search._save_tile(path, sh.LineTable.empty())
    assert len(search._load_tile(path)) == 0


def test_line_table():
    """Rows taken and joined keep their profiles; a row as the dict of before."""
    import helpers as sh
    lines = _fake_lines(6)
    taken = lines.take(np.array([4, 1, 0]))
    assert taken.columns["length"].tolist() == [lines.columns["length"][k] for k in (4, 1, 0)]
    assert taken.profiles == {0: lines.profiles[4], 2: lines.profiles[0]}
    both = sh.LineTable.concat([taken, lines.take(lines.columns["over_obstacles"])])
    assert len(both) == 3 + 4 and both.profiles == {0: lines.profiles[4], 2: lines.profiles[0], 5: lines.profiles[4]}
    row = lines.row(1)
    assert row["clearance_nmpt"] is None and row["anchor1"] == (50.0001, 19.1234567) and row["a1"] == (1, 0)
    assert lines.row(0)["clearance_nmpt"] == 0.0 and row["over_obstacles"] is True and row["profile"] is None
    assert len(sh.LineTable.concat([sh.LineTable.empty(), sh.LineTable.empty()])) == 0


def _cgroup(folder, **files):
    folder.mkdir(exist_ok=True)
    for name, text in files.items():
        (folder / name.replace("_", ".", 1)).write_text(text + "\n")
    return str(folder)


def test_cgroup_limits(tmp_path, monkeypatch):
    import search
    gib = 1 << 30
    # Docker: mem_limit 4g with 1 GiB used, a third of it droppable page cache; cpus 1.5
    root = _cgroup(tmp_path / "limited", memory_max=str(4 * gib), memory_current=str(gib),
                   memory_stat=f"anon {gib // 2}\ninactive_file {gib // 3}\nactive_file 1",
                   cpu_max="150000 100000")
    limits = search.cgroup_limits(root)
    assert limits.memory == 4 * gib - (gib - gib // 3)
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else 2
    assert limits.cpus == min(2, cpus)
    # No limits set, or no cgroup v2 files at all (Windows, macOS, cgroup v1)
    unlimited = _cgroup(tmp_path / "max", memory_max="max", memory_current=str(gib), cpu_max="max 100000")
    assert search.cgroup_limits(unlimited) == search.CgroupLimits()
    assert search.cgroup_limits(str(tmp_path / "missing")) == search.CgroupLimits()

    # The tiles follow the container's memory, the threads its quota, unless set explicitly
    params = pm.SearchParams(lat=50, lon=19, max_length=100)
    monkeypatch.setattr(search.psutil, "virtual_memory", lambda: type("M", (), {"available": 64 * gib})())
    small = _cgroup(tmp_path / "small", memory_max=str(4 * gib), memory_current="0", cpu_max="100000 100000")
    monkeypatch.setattr(search, "CGROUP_ROOT", small)
    assert search.tile_core_size(params, 1.0) == 2000  # sqrt(4 GiB / 4 / 100 B) px less the overlaps
    monkeypatch.delenv("NUMBA_NUM_THREADS", raising=False)
    assert search.line_threads(params) == 1
    assert search.line_threads(params.model_copy(update={"threads": 3})) == 3
    # NUMBA_NUM_THREADS of compose.yaml (the quota's cores): all of them, never more than the quota
    import numba
    monkeypatch.setenv("NUMBA_NUM_THREADS", "4")
    assert search.line_threads(params) == 1
    seven = _cgroup(tmp_path / "seven", memory_max="max", memory_current="0", cpu_max="700000 100000")
    monkeypatch.setattr(search, "CGROUP_ROOT", seven)
    monkeypatch.setattr(numba.config, "NUMBA_NUM_THREADS", 7)
    monkeypatch.setattr(search.os, "sched_getaffinity", lambda pid: set(range(8)), raising=False)
    assert search.line_threads(params) == 7
    monkeypatch.setattr(search, "CGROUP_ROOT", unlimited)
    assert search.line_threads(params) is None
    monkeypatch.delenv("NUMBA_NUM_THREADS")
    assert search.line_threads(params) is None
    assert search.tile_core_size(params, 1.0) == 12000


def test_errors_without_query_strings(monkeypatch):
    """A failed request reports its URL without the query (an API key must not reach messages)."""
    monkeypatch.setattr(src, "_sleep", lambda attempt: None)
    url = "http://127.0.0.1:9/API/globaldem?demtype=NASADEM&API_Key=secret123"
    with pytest.raises(RuntimeError) as error:
        src._get(url, timeout=2)
    assert "secret123" not in str(error.value) and "/API/globaldem" in str(error.value)
    with pytest.raises(RuntimeError) as error:
        src._get("http://127.0.0.1:9/API/globaldem", params={"API_Key": "secret123"}, timeout=2)
    assert "secret123" not in str(error.value)
    assert cache.clean_error("404 for url: https://x.org/a?key=k&b=1 (why?)") == "404 for url: https://x.org/a?... (why?)"


def test_run_ids_and_public_meta(tmp_path):
    """Run ids are unique within one second and not guessable; lines.geojson lists no cache files."""
    import json
    import re
    from datetime import datetime
    import helpers as sh
    import mapping as mp
    import runs as rn
    created = datetime(2026, 10, 27, 12, 0, 0)
    first, second = (rn.new_run(str(tmp_path), "Żabi Koń", created) for _ in range(2))
    assert first["id"] != second["id"]
    assert re.fullmatch(r"2026-10-27_120000_Zabi-Kon_[0-9a-f]{16}", first["id"]), first["id"]
    meta = {"name": "x", "summary": {"lines": 0},
            "data": [{"layer": "NMT", "folder": "poland/nmt", "files": 2, "paths": ["poland/nmt/a.tif", "b.tif"]}]}
    rn.save_results(first["path"], sh.LineTable.empty(), mp.Groups.each_alone(0), meta)
    saved = json.loads(open(os.path.join(first["path"], "lines.geojson"), encoding="utf-8").read())["findaline"]
    assert saved["data"] == [{"layer": "NMT", "folder": "poland/nmt", "files": 2}]
    assert json.load(open(os.path.join(first["path"], "run.json"), encoding="utf-8")) == meta  # run.json keeps them


def test_every_source_has_licence_and_attribution():
    """Every source, each of its layers and the layers used for filling name their licence and notice."""
    layers = [src.WATER_MASK]
    for source in src.SOURCES.values():
        assert source.licence and source.attribution, source.id
        layers += [layer for layer in (source.nmt, source.nmpt) if layer is not None]
    for layer in layers:
        credit = layer.credit
        assert credit and credit.data and credit.attribution and credit.licence, layer.description
        assert credit.url.startswith("https://"), layer.description
    assert src.SOURCES["europe"].nmt.credit is src.FABDEM_CREDIT  # the gap filling layer has its own
    assert "CC BY-NC-SA 4.0" in src.SOURCES["europe"].licence


def test_used_layers_carry_their_credit(tmp_path):
    """What a run records about a layer (run.json "data") includes its credit."""
    from types import SimpleNamespace
    grid = SimpleNamespace(crs="EPSG:2180", bounds=(553000, 254000, 554000, 255000))
    assert src.WATER_MASK.used(grid, str(tmp_path))["credit"] == src.WATER_MASK_CREDIT.as_dict()
    assert src.SOURCES["geoportal"].nmt.credit is src.GUGIK_CREDIT


def test_run_credits(tmp_path):
    """A run lists each dataset's credit once, OSM too when the area came from it, and writes
    them to ATTRIBUTION.txt; runs.js leaves them out."""
    import json
    from datetime import datetime
    import helpers as sh
    import mapping as mp
    import runs as rn
    import search
    from types import SimpleNamespace
    europe = src.SOURCES["europe"]
    data = [{"layer": "FABDEM", "credit": europe.nmt.credit.as_dict()},
            {"layer": "FABDEM", "credit": europe.nmt.credit.as_dict()},  # e.g. the gap filling of a second tile
            {"layer": "Terrain file", "file": "x.tif"}]
    area = ar.Circle(50.0, 19.0, 100)
    assert search._credits(SimpleNamespace(area=area), data) == [src.FABDEM_CREDIT.as_dict()]
    area.spec = {"area": "cliffs", "within": "osm:Gozo"}
    credits = search._credits(SimpleNamespace(area=area), data)
    assert credits == [src.FABDEM_CREDIT.as_dict(), src.OSM_CREDIT.as_dict()]
    run = rn.new_run(str(tmp_path), "Gozo", datetime(2026, 10, 27, 12, 0, 0))
    meta = {"id": run["id"], "name": "Gozo", "created": "2026-10-27T12:00:00", "description": "",
            "summary": {"lines": 0}, "source": {"id": "europe"}, "data": data, "attribution": credits}
    rn.save_results(run["path"], sh.LineTable.empty(), mp.Groups.each_alone(0), meta)
    with open(os.path.join(run["path"], "ATTRIBUTION.txt"), encoding="utf-8") as f:
        text = f.read()
    for credit in credits:
        assert credit["attribution"] in text and credit["licence"] in text and credit["url"] in text
    assert src.COPERNICUS_DISCLAIMER in text  # FABDEM's notice
    assert "attribution" not in rn._listed(meta)  # runs.js
    with open(os.path.join(run["path"], "run.json"), encoding="utf-8") as f:
        assert json.load(f)["attribution"] == credits


def test_replacing(tmp_path):
    """A file written under a unique temporary name replaces the old one only when complete."""
    path = tmp_path / "runs.js"
    path.write_text("old", encoding="utf-8")
    with cache.replacing(str(path), "w", encoding="utf-8") as a, cache.replacing(str(path), "w", encoding="utf-8") as b:
        assert a.name != b.name and path.read_text(encoding="utf-8") == "old"  # two writers, two files
        a.write("first")
        b.write("second")
    assert path.read_text(encoding="utf-8") == "first"  # the writer that finished last (b closes first)
    with pytest.raises(ValueError):
        with cache.replacing(str(path)) as f:
            f.write(b"half")
            raise ValueError("failed")
    assert path.read_text(encoding="utf-8") == "first" and os.listdir(tmp_path) == ["runs.js"]


def test_labels_topics_and_presets():
    """Every field the web may set has a label, a unit and a hint in the message catalogue
    (locales/en.json), and every choice of a choice field a label; every such field belongs to one
    topic of the form (params.TOPICS), every topic and preset has a title; each preset sets only
    fields of PRESET_FIELDS and validates as SearchParams; a preset outside a user's bounds is
    unavailable."""
    import messages
    import web
    from settings import Limits
    texts = messages.catalogue()
    web_fields = [f["name"] for f in web.schema()]
    assert set(web_fields) == {n for n, i in pm.SearchParams.model_fields.items()
                               if not {"cli_only", "web_only"} & set(i.json_schema_extra or {})}
    for name in web_fields:
        assert texts[f"field.{name}.label"] and not texts[f"field.{name}.label"].endswith("."), name
        assert f"field.{name}.unit" in texts and texts[f"field.{name}.help"].endswith("."), name
    for field in web.schema():
        for choice in field["choices"] or []:
            assert field["name"] == "source" or f"choice.{field['name']}.{choice}" in texts, (field["name"], choice)
    named = {k.split(".")[1] for k in texts if k.startswith("field.")}
    assert named == set(web_fields), named ^ set(web_fields)  # no texts of fields that are gone
    topics = [n for names in pm.TOPICS.values() for n in names]
    assert sorted(topics) == sorted(web_fields), set(topics) ^ set(web_fields)
    assert all(f"topic.{t}" in texts for t in pm.TOPICS) and all(f"preset.{p}" in texts for p in pm.PRESETS)
    for preset, values in pm.PRESETS.items():
        assert (values is None or set(values) <= set(pm.PRESET_FIELDS)), preset
        full = pm.preset_values(preset)
        if full is not None:
            assert set(full) == set(pm.PRESET_FIELDS)
            pm.SearchParams(lat=50.1, lon=19.7, **full)  # valid on its own
    assert pm.preset_values("typical") == {"min_length": 30, "max_length": 150, "max_anchor_diff_m": 10}
    assert pm.preset_values("long")["max_anchor_diff_m"] == pm.SearchParams.model_fields["max_anchor_diff_m"].default
    offered = {p["id"]: p for p in web.presets(Limits.load(public=True))}
    assert list(offered) == ["typical", "long", "short", "custom"] and all(p["available"] for p in offered.values())
    assert offered["custom"]["values"] is None
    tight = {p["id"]: p for p in web.presets(Limits(max_length_max=200))}
    assert tight["typical"]["available"] and not tight["long"]["available"]
    assert tight["long"]["reason"] == "Longest line: at most 200 m on this site."
    fields = {f["name"]: f for f in web.schema()}
    assert offered["short"]["label"] == "Short & low" and fields["nmpt"]["choice_labels"]["off"] == "Do not check"
    assert fields["max_anchor_diff_m"]["label"] == "Anchor height difference"
    assert fields["max_anchor_diff_m"]["unit"] == "m" and fields["max_anchor_diff_m"]["topic"] == "lines"
    assert fields["source"]["choice_labels"]["auto"] == "Automatic"


def test_viewer_keys_in_catalogue():
    """Every key the page asks for by its literal (t("...") or t('...') in viewer/src) is in the message
    catalogue, in its plain or a plural form."""
    import re

    import messages
    texts = messages.catalogue()
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer", "src")
    used = set()
    for folder, _, files in os.walk(root):
        for file in files:
            if file.endswith((".ts", ".tsx")):
                with open(os.path.join(folder, file), encoding="utf-8") as f:
                    used |= set(re.findall(r"""\bt\(\s*["']([^"'`]+)["']""", f.read()))
    assert "ui.try" in used
    missing = [k for k in used if not {k, f"{k}_one", f"{k}_other"} & set(texts)]
    assert not missing, missing


def _error_codes() -> dict:
    """The codes of the coded errors and notes of the server's modules (messages.UserError, Note,
    ServiceError, search.SearchError, jobs.QuotaExceeded): {code: "file:line"}, read from the source."""
    import ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    kinds = {"rate_": ["estimate", "profile", "place", "report", "job"],  # web.RATE_NAMES
             "quota_use_": ["km2", "nominatim", "download_mb"]}           # jobs.USAGE
    position = {"UserError": 1, "SearchError": 1, "Note": 1, "ServiceError": 1, "QuotaExceeded": 2}

    def literals(node) -> list:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.IfExp):
            return literals(node.body) + literals(node.orelse)
        if isinstance(node, ast.JoinedStr) and isinstance(node.values[0], ast.Constant):
            return [node.values[0].value + kind for kind in kinds[node.values[0].value]]
        return []

    found = {}
    for name in sorted(os.listdir(root)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(root, name), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            web = called == "web" and isinstance(func, ast.Attribute)
            of = called == "of" and isinstance(func, ast.Attribute)  # SearchError.of(error, text, code)
            if web or of:
                called = func.value.attr if isinstance(func.value, ast.Attribute) else getattr(func.value, "id", "")
            if called == "E":  # web.py's short name of messages.UserError
                called = "UserError"
            if called not in position:
                continue
            code = next((k.value for k in node.keywords if k.arg == "code"), None)
            index = 0 if web else 2 if of else position[called]
            if code is None and len(node.args) > index:
                code = node.args[index]
            for value in literals(code) if code is not None else []:
                found[value] = f"{name}:{node.lineno}"
    import messages
    for value in set(messages._PYDANTIC.values()) | {"invalid_request"}:
        found[value] = "messages.py"
    return found


def test_error_codes_in_catalogue():
    """Every code of a coded error or note has its text in the message catalogue ("error.<code>", or
    its plural forms), and no text of the web names a command-line option or a field's snake_case name."""
    import messages
    import params as pm
    texts = messages.catalogue()
    codes = _error_codes()
    assert {"lat_lon_range", "at_most", "too_many_lines", "quota_use_km2", "coast_pieces_left_out", "rate_job",
            "no_worker", "opentopo_key", "not_a_number", "download_failed", "no_source"} <= set(codes), sorted(codes)
    missing = [f"{c} ({w})" for c, w in codes.items()
               if not {f"error.{c}", f"error.{c}_one", f"error.{c}_other"} & set(texts)]
    assert not missing, missing
    names = [n for n in list(pm.SearchParams.model_fields) + list(pm.CliOptions.model_fields) if "_" in n]
    for key, value in texts.items():
        if key.startswith("error."):
            assert "--" not in value and not [n for n in names if n in value], (key, value)


def test_coded_errors():
    """A coded error keeps the command line's text as str() and gives the web its own, with the field's
    label; pydantic's errors are unwrapped: a validator's coded error, or a code of their type."""
    import messages
    import params as pm
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as error:
        pm.SearchParams(lat=50, lon=19, radius=0)
    coded = messages.user_error(error.value)
    assert str(coded) == "--radius must be positive" and coded.code == "positive"
    assert coded.detail() == {"message": "Search radius must be more than 0.", "code": "positive",
                              "params": {"field": "radius"}}
    with pytest.raises(ValidationError) as error:
        pm.SearchParams(lat=50, lon=19, threads="many")
    assert messages.user_error(error.value).detail() == {
        "message": "Threads: enter a whole number.", "code": "not_whole_number", "params": {"field": "threads"}}
    assert messages.user_error(ValueError("plain")) is None
    note = messages.Note("1 piece(s) ... --max-length", "coast_pieces_left_out", count=3, near=200, far=400,
                         lat=1.5, lon=2.5)
    assert str(note).endswith("--max-length") and note.web_text().startswith("3 parts of the coast are 200–400 m")
    assert messages.quantity("slope_threshold", 45.0) == "45°" and messages.quantity("ortho_detail", 5) == "5"
