"""OpenStreetMap extracts without the network: which theme tiles they can build."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import osmextract  # noqa: E402


def square(west, south, east, north):
    return {"type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north], [west, north],
                                                [west, south]]]}


def fake_cache(tmp_path):
    """A Geofabrik index with a continent and two neighbouring countries; the extract of "island"."""
    cache_dir = tmp_path / "cache"
    (cache_dir / "osm").mkdir(parents=True)
    index = {"features": [
        {"properties": {"id": "europe", "name": "Europe"}, "geometry": square(0, 40, 30, 60)},
        {"properties": {"id": "island", "name": "Island", "parent": "europe"}, "geometry": square(10.05, 50.05, 10.2, 50.2)},
        {"properties": {"id": "mainland", "name": "Mainland", "parent": "europe"}, "geometry": square(10.3, 50.0, 12, 52)},
    ]}
    (cache_dir / "osm" / "geofabrik-index.json").write_text(json.dumps(index))
    folder = cache_dir / "island" / "osm-extract"
    folder.mkdir(parents=True)
    (folder / "island.osm.pbf").write_bytes(b"")
    (folder / "island.geojson").write_text(json.dumps(square(10.05, 50.05, 10.2, 50.2)))
    cliff = {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[10.1, 50.1], [10.11, 50.11]]},
             "properties": {"id": "way/1", "tags": {"natural": "cliff"}}}
    (folder / "island.themes.json").write_text(json.dumps(
        {"timestamp": "2026-10-25T00:00:00", "themes": {"cliff": [cliff], "coastline": []}}))
    return str(cache_dir)


def test_tile_with_open_sea_is_built_from_the_extract(tmp_path):
    cache_dir = fake_cache(tmp_path)
    # Tile 50.00-50.25 N, 10.00-10.25 E: the island and the sea around it, no other country
    assert osmextract.build_tile(cache_dir, "cliff", 50.0, 10.0)
    with open(os.path.join(cache_dir, "osm", "themes", "cliff", "N50.00_E010.00.geojson")) as f:
        assert [f["properties"]["id"] for f in json.load(f)["features"]] == ["way/1"]
    # Tile 50.00-50.25 N, 10.25-10.50 E reaches the mainland, whose extract is not there: Overpass
    assert not osmextract.build_tile(cache_dir, "cliff", 50.0, 10.25)


def test_regions_at_a_point(tmp_path):
    cache_dir = fake_cache(tmp_path)
    assert osmextract.regions_at(cache_dir, 50.1, 10.1) == [
        {"id": "island", "name": "Island", "depth": 1, "downloaded": True}]
    assert [r["id"] for r in osmextract.regions_at(cache_dir, 51, 11)] == ["mainland"]


def test_user_agent(monkeypatch):
    """One User-Agent for every request; a public site adds its operator's contact."""
    import cache
    monkeypatch.delenv("FINDALINE_CONTACT", raising=False)
    assert cache.http_headers() == {"User-Agent": "Find-a-Line/1.0 (highline finder)"}
    monkeypatch.setenv("FINDALINE_CONTACT", " admin@example.org\r\n")
    assert cache.http_headers() == {"User-Agent": "Find-a-Line/1.0 (highline finder; contact: admin@example.org)"}


def _old_nominatim_db(cache_dir: str, rows):
    """nominatim.sqlite as it was before schema versions (user_version 0)."""
    import sqlite3
    os.makedirs(os.path.join(cache_dir, "osm"), exist_ok=True)
    db = sqlite3.connect(os.path.join(cache_dir, "osm", "nominatim.sqlite"))
    with db:
        db.execute("CREATE TABLE search (query TEXT PRIMARY KEY, answer TEXT, fetched_at TEXT)")
        db.execute("CREATE TABLE reverse (query TEXT PRIMARY KEY, answer TEXT, fetched_at TEXT)")
        db.executemany("INSERT INTO search VALUES (?, ?, ?)", rows)
    db.close()


def _fake_nominatim(monkeypatch, answers):
    """osm._ask answering from a dict (by the "q" of a search); returns the list of names asked."""
    import osm
    asked = []

    def ask(url, params, timeout, cache_dir):
        asked.append(params.get("q"))
        return answers.get(params.get("q"), [])
    monkeypatch.setattr(osm, "_ask", ask)
    return asked


def test_nominatim_cache_migration_keeps_rows(tmp_path, monkeypatch):
    """An old nominatim.sqlite gets schema version 1 and keeps its answers, also those without a time."""
    import sqlite3

    import osm
    cache_dir = str(tmp_path)
    island = {"name": "Island", "geojson": square(10.05, 50.05, 10.2, 50.2)}
    _old_nominatim_db(cache_dir, [("Island", json.dumps(island), "2020-01-01T00:00:00+00:00"),
                                  ("Nowhere", "{}", None)])  # no time: kept like a successful answer
    asked = _fake_nominatim(monkeypatch, {})
    assert osm.search("Island", cache_dir) == island
    assert osm.search("Nowhere", cache_dir) == {}
    assert asked == []
    db = sqlite3.connect(os.path.join(cache_dir, "osm", "nominatim.sqlite"))
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(osm.MIGRATIONS) == 1
    assert db.execute("SELECT COUNT(*) FROM search").fetchone()[0] == 2
    db.close()


def test_nominatim_failed_searches_expire(tmp_path, monkeypatch):
    """A search without a match is asked again after FAILED_DAYS; a match is kept however old."""
    from datetime import datetime, timedelta, timezone

    import osm
    cache_dir = str(tmp_path)
    island = {"name": "Island", "geojson": square(10.05, 50.05, 10.2, 50.2)}
    long_ago = (datetime.now(timezone.utc) - timedelta(days=osm.FAILED_DAYS + 1)).isoformat(timespec="seconds")
    recently = (datetime.now(timezone.utc) - timedelta(days=osm.FAILED_DAYS - 1)).isoformat(timespec="seconds")
    _old_nominatim_db(cache_dir, [("Island", json.dumps(island), long_ago), ("Old miss", "{}", long_ago),
                                  ("New miss", "{}", recently)])
    asked = _fake_nominatim(monkeypatch, {"Old miss": [{"name": "Old miss", "geojson": square(0, 0, 1, 1)}]})
    assert osm.search("Island", cache_dir) == island
    assert osm.search("New miss", cache_dir) == {}
    assert osm.search("Old miss", cache_dir)["name"] == "Old miss"  # asked again: found now
    assert osm.search("Old miss", cache_dir)["name"] == "Old miss"  # and stored
    assert asked == ["Old miss"]


def test_nominatim_large_answers_are_simplified(tmp_path, monkeypatch):
    """An outline above MAX_ANSWER_BYTES is stored simplified, and the first answer is the stored one."""
    import math

    import osm
    cache_dir = str(tmp_path)
    ring = [[10 + 0.1 * math.cos(i / 5000 * 2 * math.pi), 50 + 0.1 * math.sin(i / 5000 * 2 * math.pi)]
            for i in range(5000)]
    big = {"name": "Big", "geojson": {"type": "Polygon", "coordinates": [ring + [ring[0]]]}}
    monkeypatch.setattr(osm, "MAX_ANSWER_BYTES", len(json.dumps(big)) // 4)
    asked = _fake_nominatim(monkeypatch, {"Big": [big]})
    first = osm.search("Big", cache_dir)
    assert len(json.dumps(first)) <= osm.MAX_ANSWER_BYTES
    assert first["name"] == "Big" and len(first["geojson"]["coordinates"][0]) < 5000
    assert osm.search("Big", cache_dir) == first and asked == ["Big"]
