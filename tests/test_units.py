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
