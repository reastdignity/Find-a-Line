"""The web application (web.py): what it refuses, and a job through the queue with
real worker processes (offline: local files, any request goes to a closed port)."""

import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "tests", "data")
sys.path.insert(0, REPO)
import web  # noqa: E402

pytest.importorskip("httpx2")
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "test-token"


@pytest.fixture
def client(tmp_path):
    app = web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)
    with TestClient(app) as c:
        yield c


def test_guards(client, tmp_path):
    assert client.get("/api/schema", headers={"host": "evil.example"}).status_code == 403
    assert client.post("/api/jobs", json={"params": {}}).status_code == 403          # no token
    headers = {"x-findaline-token": TOKEN}
    for params in ({"elevation_file": "x.tif"}, {"opentopo_key": "k"}, {"area": "C:/secret.geojson"}):
        response = client.post("/api/jobs", json={"params": params}, headers=headers)
        assert response.status_code == 400, params
    (tmp_path / "cache" / "poland").mkdir(parents=True)
    (tmp_path / "cache" / "poland" / "index.sqlite").write_text("x")
    assert client.get("/cache/poland/index.sqlite").status_code == 404               # only orthophoto tiles
    page = client.get("/")
    assert TOKEN in page.text and "script-src 'self'" in page.headers["content-security-policy"]


def test_schema_has_no_file_fields(client):
    names = {f["name"] for f in client.get("/api/schema").json()["fields"]}
    assert {"lat", "radius", "model", "ortho"} <= names
    assert not names & {"elevation_file", "surface_file", "slope_file", "opentopo_key"}


def _offline(monkeypatch):
    for key, value in {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
                       "PYTHONIOENCODING": "utf-8"}.items():
        monkeypatch.setenv(key, value)


def test_job_through_the_queue(tmp_path, monkeypatch):
    """A job (added directly: local files are refused over the web) downloads, computes
    and saves a run in worker processes; a queued job can be cancelled."""
    _offline(monkeypatch)
    jobs = web.Jobs(str(tmp_path))
    params = {"elevation_file": os.path.join(DATA, "zabi_kon_nmt.tif"), "northing": 254608, "easting": 553966,
              "radius": 150, "water": "off", "fill_gaps": "off", "name": "Clip", "ortho": "never"}
    job = jobs.add({"params": params, "geometry": None}, "clip")
    other = jobs.add({"params": params, "geometry": None}, "clip again")
    scheduler = web.Scheduler(jobs)
    assert scheduler.cancel(other)
    scheduler.start()
    try:
        deadline = time.time() + 300
        while jobs.get(job)["status"] not in ("done", "failed") and time.time() < deadline:
            time.sleep(0.5)
    finally:
        scheduler.stop()
    result = jobs.get(job)
    assert result["status"] == "done", result["error"]
    assert os.path.exists(tmp_path / "runs" / result["run_id"] / "map.html")
    assert jobs.get(other)["status"] == "cancelled"
    kinds = {e["kind"] for e in jobs.events(0, job)}
    assert {"status", "info", "progress"} <= kinds
    statuses = [e["data"]["status"] for e in jobs.events(0, job) if e["kind"] == "status"]
    assert statuses == ["queued", "downloading", "ready", "computing", "done"]


class _Ended:
    """A worker process that has ended."""
    exitcode = 0

    def is_alive(self):
        return False

    def join(self):
        pass


def test_download_process_ending_late(tmp_path):
    """A download process that exits after its job has started computing does not fail the job;
    a compute process that ends without a word does."""
    jobs = web.Jobs(str(tmp_path))
    job = jobs.add({"params": {}, "geometry": None}, "job")
    jobs.set(job, "computing")
    scheduler = web.Scheduler(jobs)
    scheduler.running["download"] = (job, _Ended())
    scheduler.running["compute"] = (job, type("Running", (), {"is_alive": lambda self: True})())
    scheduler._check()
    assert jobs.get(job)["status"] == "computing"
    scheduler.running["compute"] = (job, _Ended())
    scheduler._check()
    assert jobs.get(job)["status"] == "failed"


def test_profile_on_demand(tmp_path, monkeypatch):
    """A profile computed again from the cache equals the one the search saved."""
    import json
    import subprocess
    _offline(monkeypatch)
    result = subprocess.run(
        [sys.executable, "findaline.py", "--elevation-file", os.path.join(DATA, "zabi_kon_nmt.tif"),
         "--surface-file", os.path.join(DATA, "zabi_kon_nmpt.tif"), "--northing", "254608", "--easting", "553966",
         "--radius", "150", "--model", "sag", "--water", "off", "--fill-gaps", "off", "--no-plot", "--yes",
         "--ortho", "never", "--data-dir", str(tmp_path)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=600, env=dict(os.environ))
    assert result.returncode == 0, result.stdout + result.stderr
    run_id = next(d for d in os.listdir(tmp_path / "runs") if os.path.isfile(tmp_path / "runs" / d / "run.json"))
    text = (tmp_path / "runs" / run_id / "data.js").read_text(encoding="utf-8")
    rows = json.loads(text.partition(" = ")[2].rstrip().rstrip(";"))["lines"]
    app = web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)
    with TestClient(app) as client:
        for row in rows[:3]:
            line = ",".join(str(v) for v in row[:4])
            assert client.get(f"/api/profile?run={run_id}&line={line}").status_code == 403  # header needed
            response = client.get(f"/api/profile?run={run_id}&line={line}", headers={"x-findaline-profile": "1"})
            assert response.status_code == 200, response.text
            assert response.json() == row[16]  # the smaller grid samples the same pixels
        assert client.get("/api/profile?run=..&line=1,2,3,4", headers={"x-findaline-profile": "1"}).status_code == 404


def test_osm_jobs(client, tmp_path):
    import json
    (tmp_path / "cache" / "osm").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache" / "osm" / "geofabrik-index.json").write_text(json.dumps({"features": [
        {"properties": {"id": "malta", "name": "Malta", "parent": "europe"}, "geometry": None}]}))
    headers = {"x-findaline-token": TOKEN}
    assert client.post("/api/osm", json={"action": "download", "region": "atlantis"}, headers=headers).status_code == 400
    assert client.post("/api/osm", json={"action": "update", "region": "malta"}, headers=headers).status_code == 400
    assert client.post("/api/osm", json={"action": "delete", "region": "malta"}, headers=headers).status_code == 400
    response = client.post("/api/osm", json={"action": "download", "region": "malta"}, headers=headers)
    assert response.status_code == 200
    assert web.Jobs(str(tmp_path)).get(response.json()["id"])["title"] == "OSM download: malta"
