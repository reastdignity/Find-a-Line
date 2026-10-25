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
