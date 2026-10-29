"""OpenStreetMap without the network: which theme tiles the extracts can build, the Nominatim cache,
the shared rate limits of Nominatim and Overpass (against a local fake server), the User-Agent."""

import contextlib
import json
import os
import sys
import threading
import time

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
    """One User-Agent for every request with the version of pyproject.toml: "self-hosted", or a public
    site's address; the operator's contact when set."""
    import tomllib
    import cache
    with open(os.path.join(os.path.dirname(cache.__file__), "pyproject.toml"), "rb") as f:
        version = tomllib.load(f)["project"]["version"]
    assert cache.VERSION == version
    monkeypatch.delenv("FINDALINE_CONTACT", raising=False)
    monkeypatch.delenv("FINDALINE_PUBLIC_URL", raising=False)
    assert cache.http_headers() == {"User-Agent": f"Find-a-Line/{version} (highline finder; self-hosted)"}
    monkeypatch.setenv("FINDALINE_CONTACT", " admin@example.org\r\n")
    assert cache.http_headers() == {"User-Agent": f"Find-a-Line/{version} (highline finder; self-hosted; "
                                                  "contact: admin@example.org)"}
    monkeypatch.setenv("FINDALINE_PUBLIC_URL", "https://findaline.example")
    assert cache.http_headers() == {"User-Agent": f"Find-a-Line/{version} (highline finder; +https://findaline.example; "
                                                  "contact: admin@example.org)"}


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


@contextlib.contextmanager
def _fake_server(body: bytes, seconds: float = 0.0):
    """A local HTTP server answering every GET with `body` after `seconds`; yields its URL and the
    list of (start, end) times of the requests it served."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    served = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            start = time.time()
            time.sleep(seconds)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            served.append((start, time.time()))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/", served
    finally:
        server.shutdown()
        server.server_close()


def _in_turn(kind: str, url: str, cache_dir: str, names, start: float):
    """Run in a spawned process: Nominatim searches or Overpass tiles against the fake server."""
    os.environ["NO_PROXY"] = "127.0.0.1"
    import osm
    import osmextract
    osmextract.build_tile = lambda *args: False  # no extracts: Overpass
    osm.SEARCH_URL = osm.OVERPASS_URLS[0] = url
    time.sleep(max(0.0, start - time.time()))  # both processes begin together
    for name in names:
        if kind == "nominatim":
            osm.search(name, cache_dir)
        else:
            osm.fetch_tile("cliff", name, 10.0, cache_dir)


def _two_processes(kind: str, url: str, cache_dir: str, names):
    import multiprocessing
    context = multiprocessing.get_context("spawn")
    start = time.time() + 5  # time to start both
    processes = [context.Process(target=_in_turn, args=(kind, url, cache_dir, names[i::2], start)) for i in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(120)
        assert process.exitcode == 0


def test_nominatim_one_request_per_second_across_processes(tmp_path):
    """Two processes searching at once: the requests reach Nominatim at least a second apart."""
    with _fake_server(b"[]") as (url, served):
        _two_processes("nominatim", url, str(tmp_path), [f"place {i}" for i in range(6)])
    starts = sorted(start for start, _ in served)
    assert len(starts) == 6
    assert min(b - a for a, b in zip(starts, starts[1:])) >= 1.0


def test_overpass_one_query_at_a_time_across_processes(tmp_path):
    """Two processes building theme tiles at once: their Overpass queries never overlap."""
    with _fake_server(b'{"elements": []}', seconds=0.5) as (url, served):
        _two_processes("overpass", url, str(tmp_path), [50.0, 50.25, 50.5, 50.75])
    served.sort()
    assert len(served) == 4
    assert all(end <= next_start for (_, end), (next_start, _) in zip(served, served[1:]))


def test_overpass_busy(tmp_path, monkeypatch):
    """A query that cannot get its turn fails with a readable requests error (callers handle those)."""
    import pytest
    import requests
    from filelock import FileLock

    import osm
    import osmextract
    monkeypatch.setattr(osmextract, "build_tile", lambda *args: False)
    monkeypatch.setattr(osm, "OVERPASS_WAIT_S", 0.2)
    (tmp_path / "osm").mkdir()
    with FileLock(str(tmp_path / "osm" / "overpass.lock")):  # another search's query
        with pytest.raises(requests.Timeout, match="Overpass is busy"):
            osm.fetch_tile("cliff", 50.0, 10.0, str(tmp_path))
