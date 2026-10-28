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
