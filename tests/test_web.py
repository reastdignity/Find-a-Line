"""The web application (web.py): what it refuses, and a job through the queue with
real worker processes (offline: local files, any request goes to a closed port)."""

import contextlib
import json
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "tests", "data")
sys.path.insert(0, REPO)
from jobs import Jobs, Scheduler  # noqa: E402
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


def test_values_checked(client, tmp_path):
    """Finite values in range, the choices of the form, and a title from the validated values."""
    headers = {"x-findaline-token": TOKEN}
    point = {"lat": 50.1, "lon": 19.7}
    for params in ({"lat": 91, "lon": 19.7}, {**point, "source": "files"}, {**point, "slope_threshold": 0},
                   {**point, "ortho_detail": -1}, {**point, "name": "x" * 101}, {**point, "name": "a\nb"},
                   {"area": "coast", "within": "36.1,14.2,36.0,14.3"}, {"area": "coast", "within": "nan,1,2,3"}):
        response = client.post("/api/jobs", json={"params": params}, headers=headers)
        assert response.status_code == 400, params
    for text in ('{"params": {"lat": NaN, "lon": 19.7}}', '{"params": {"lat": 50, "lon": 19.7, "radius": Infinity}}'):
        response = client.post("/api/jobs", content=text, headers={**headers, "content-type": "application/json"})
        assert response.status_code == 400, text
    response = client.post("/api/jobs", json={"params": {"lat": "50.1", "lon": "19.7", "radius": 100}},
                           headers=headers)
    assert response.status_code == 200, response.text
    assert Jobs(str(tmp_path)).get(response.json()["id"])["title"] == "50.10000, 19.70000 (r 100 m)"


def test_geometry_checked(client, tmp_path):
    """A drawn or uploaded area: RFC 7946 GeoJSON within limits, valid shapes, finite coordinates in range."""
    headers = {"x-findaline-token": TOKEN}
    square = [[[19.75, 50.15], [19.76, 50.15], [19.76, 50.16], [19.75, 50.16], [19.75, 50.15]]]
    nested = {"type": "Point", "coordinates": [19.75, 50.15]}
    for _ in range(12):
        nested = {"type": "GeometryCollection", "geometries": [nested]}
    for geometry in ({"type": "LineString", "coordinates": [[19.75 + i * 1e-7, 50.15] for i in range(100_001)]},
                     nested,
                     {"type": "Polygon", "coordinates": "x"},
                     {"type": "Circle", "coordinates": [19.75, 50.15]},
                     {"type": "Point", "coordinates": [190, 50.15]},
                     {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]},  # a bow tie
                     {"type": "FeatureCollection", "features": []}):
        response = client.post("/api/jobs", json={"params": {}, "geometry": geometry}, headers=headers)
        assert response.status_code == 400, (str(geometry)[:80], response.text)
    text = '{"params": {}, "geometry": {"type": "Point", "coordinates": [NaN, 50.15]}}'
    response = client.post("/api/jobs", content=text, headers={**headers, "content-type": "application/json"})
    assert response.status_code == 400
    response = client.post("/api/jobs", json={"params": {"lat": 50.1, "lon": 19.7},
                                              "geometry": {"type": "Polygon", "coordinates": square}}, headers=headers)
    assert response.status_code == 400  # a centre and an area
    response = client.post("/api/jobs", json={"params": {"buffer": 50}, "geometry": {
        "type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": "x"},
                                                   "geometry": {"type": "Polygon", "coordinates": square}}]}},
        headers=headers)
    assert response.status_code == 200, response.text
    assert Jobs(str(tmp_path)).get(response.json()["id"])["title"] == "drawn area"
    assert not os.path.exists(tmp_path / "work" / "check")
    import params as pm
    assert "--geometry" not in pm.build_parser("").format_help()  # web only


def test_drawn_area_through_the_queue(tmp_path, monkeypatch):
    """A drawn geometry computed by the workers (in tiles) gives the lines of the same shapes as an
    --area file of the command line: a polygon as it is, a point grown by the buffer."""
    import subprocess
    _offline(monkeypatch)
    geometry = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[
            [19.7522, 50.1546], [19.7590, 50.1546], [19.7590, 50.1580], [19.7522, 50.1580], [19.7522, 50.1546]]]}},
        {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [19.7545, 50.1590]}}]}
    common = {"elevation_file": os.path.join(DATA, "zabi_kon_nmt.tif"), "max_length": 150, "buffer": 60,
              "water": "off", "fill_gaps": "off", "ortho": "never"}
    (tmp_path / "web").mkdir()
    jobs = Jobs(str(tmp_path / "web"))
    job = jobs.add({"params": {**common, "tile_size": 150}, "geometry": geometry}, "drawn")
    scheduler = Scheduler(jobs)
    scheduler.start()
    try:
        deadline = time.time() + 300
        while jobs.get(job)["status"] not in ("done", "failed") and time.time() < deadline:
            time.sleep(0.5)
    finally:
        scheduler.stop()
    result = jobs.get(job)
    assert result["status"] == "done", result["error"]
    folder = tmp_path / "web" / "runs" / result["run_id"]
    meta = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    assert meta["params"]["area"]["area"] == "drawn area" and meta["area"]["spec"]["area"] == "drawn area"
    assert not os.path.exists(tmp_path / "web" / "work" / "jobs" / job / "area.geojson")

    path = tmp_path / "drawn.geojson"
    path.write_text(json.dumps(geometry), encoding="utf-8")
    cli = subprocess.run(
        [sys.executable, "findaline.py", "--area", str(path), "--no-plot", "--yes", "--data-dir", str(tmp_path / "cli"),
         *[a for k, v in common.items() for a in ("--" + k.replace("_", "-"), str(v))]],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=600, env=dict(os.environ))
    assert cli.returncode == 0, cli.stdout + cli.stderr
    cli_csv = next((tmp_path / "cli" / "runs").glob("*/lines.csv"))
    lines = (folder / "lines.csv").read_bytes()
    assert len(lines.splitlines()) > 10 and lines == cli_csv.read_bytes()


def test_jobs_database_migrated(tmp_path):
    """A jobs.sqlite from before the schema versions keeps its jobs and gets the newest version;
    a failing step leaves a database as it was."""
    import sqlite3

    import cache
    import jobs as jb
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    db.executescript("""
        CREATE TABLE jobs (id TEXT PRIMARY KEY, created TEXT, status TEXT, request TEXT, title TEXT,
                           run_id TEXT, error TEXT, finished TEXT);
        CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, kind TEXT, data TEXT);
        INSERT INTO jobs VALUES ('old', '2026-10-01T10:00:00', 'done', '{}', 'Old job', 'run', NULL,
                                 '2026-10-01T11:00:00');""")
    db.close()
    jobs = Jobs(str(tmp_path))
    assert jobs.get("old")["title"] == "Old job"
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(jb.MIGRATIONS)
    db.close()
    Jobs(str(tmp_path))  # opened again: nothing to do

    db = sqlite3.connect(tmp_path / "other.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        cache.migrate(db, ["CREATE TABLE a (x); CREATE TABLE b (y)", "CREATE TABLE c (z); CREATE TABLE a (x)"])
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    assert db.execute("SELECT name FROM sqlite_master").fetchall() == []
    assert cache.migrate(db, ["CREATE TABLE a (x); CREATE TABLE b (y)", lambda d: d.execute("CREATE TABLE c (z)")]) == 2
    assert {r[0] for r in db.execute("SELECT name FROM sqlite_master")} == {"a", "b", "c"}
    db.close()


def test_claim_is_atomic(tmp_path):
    """Workers taking jobs at the same time (each with its own connection) never take one twice,
    and take them oldest first."""
    from concurrent.futures import ThreadPoolExecutor
    jobs = Jobs(str(tmp_path))
    added = [jobs.add({"params": {}, "geometry": None}, f"job {i}") for i in range(30)]

    def take(_):
        mine = []
        while (job := Jobs(str(tmp_path)).claim("queued", "downloading")) is not None:
            mine.append(job["id"])
        return mine
    with ThreadPoolExecutor(6) as pool:
        taken = [job for mine in pool.map(take, range(6)) for job in mine]
    assert sorted(taken) == sorted(added) and len(set(taken)) == len(added)
    assert all(jobs.get(j)["status"] == "downloading" for j in added)
    first = jobs.add({"params": {}, "geometry": None}, "first")
    jobs.add({"params": {}, "geometry": None}, "second")
    assert jobs.claim("queued", "downloading")["id"] == first
    assert jobs.claim("ready", "computing") is None


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
    jobs = Jobs(str(tmp_path))
    params = {"elevation_file": os.path.join(DATA, "zabi_kon_nmt.tif"), "northing": 254608, "easting": 553966,
              "radius": 150, "water": "off", "fill_gaps": "off", "name": "Clip", "ortho": "never"}
    job = jobs.add({"params": params, "geometry": None}, "clip")
    other = jobs.add({"params": params, "geometry": None}, "clip again")
    scheduler = Scheduler(jobs)
    assert jobs.request_cancel(other)
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
    steps = {e["data"]["step"].split(":")[0] for e in jobs.events(0, job) if e["kind"] == "progress"} - {""}
    assert steps == {f"Step {n}/5" for n in range(1, 6)}, steps
    stages = {e["data"]["stage"] for e in jobs.events(0, job) if e["kind"] == "progress"}
    assert {"checking lines", "coordinates", "saving results", "saving the map"} <= stages, stages


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
    jobs = Jobs(str(tmp_path))
    job = jobs.add({"params": {}, "geometry": None}, "job")
    jobs.set(job, "computing")
    scheduler = Scheduler(jobs)
    scheduler.running["download"] = (job, _Ended())
    scheduler.running["compute"] = (job, type("Running", (), {"is_alive": lambda self: True})())
    scheduler._check()
    assert jobs.get(job)["status"] == "computing"
    scheduler.running["compute"] = (job, _Ended())
    scheduler._check()
    assert jobs.get(job)["status"] == "failed"


def test_worker_leases(tmp_path):
    """Only jobs of workers whose lease expired (or of no worker) count as interrupted; a web
    server starting and stopping its own worker leaves the jobs of another worker alone;
    cancelling a running job goes through its worker, unless that worker is gone."""
    import jobs as jb
    jobs = Jobs(str(tmp_path))
    ids = {name: jobs.add({"params": {}, "geometry": None}, name) for name in ("alive", "expired", "old")}
    jobs.register("w-alive")
    jobs.register("w-expired")
    with jobs._db() as db:
        db.execute("UPDATE workers SET expires = ? WHERE id = 'w-expired'", (time.time() - 1,))
        for name, worker in (("alive", "w-alive"), ("expired", "w-expired"), ("old", None)):
            db.execute("UPDATE jobs SET status = 'computing', worker_id = ? WHERE id = ?", (worker, ids[name]))
    assert not jobs.heartbeat("w-expired") and jobs.heartbeat("w-alive")
    assert sorted(jobs.interrupted()) == sorted([ids["expired"], ids["old"]])
    assert jobs.get(ids["expired"])["error"] == jb.INTERRUPTED and jobs.get(ids["old"])["status"] == "failed"
    assert jobs.get(ids["alive"])["status"] == "computing"

    app = web.create_app(str(tmp_path), token=TOKEN)  # its own worker in a thread
    with TestClient(app) as client:
        assert client.get("/api/jobs").json()["worker"]
        assert app.state.scheduler.worker_id in {r["id"] for r in jobs._db().execute("SELECT id FROM workers")}
        assert client.delete(f"/api/jobs/{ids['alive']}", headers={"x-findaline-token": TOKEN}).status_code == 200
    assert jobs.get(ids["alive"])["status"] == "computing" and jobs.get(ids["alive"])["cancel_requested"] == 1
    assert app.state.scheduler.worker_id not in {r["id"] for r in jobs._db().execute("SELECT id FROM workers")}
    jobs.unregister("w-alive")
    assert not jobs.worker_alive()
    other = jobs.add({"params": {}, "geometry": None}, "other")
    jobs.claim("queued", "downloading", "w-alive")
    assert jobs.request_cancel(other) and jobs.get(other)["status"] == "cancelled"  # its worker is gone
    assert not jobs.request_cancel(other)


@contextlib.contextmanager
def _black_hole():
    """A proxy that accepts connections and never answers (a request through it hangs until its timeout)."""
    import socket
    import threading
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held = []

    def accept():
        with contextlib.suppress(OSError):
            while True:
                held.append(server.accept()[0])
    threading.Thread(target=accept, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"
    finally:
        server.close()
        for connection in held:
            connection.close()


def _wait(jobs, job_id, statuses, seconds=60):
    deadline = time.time() + seconds
    while jobs.get(job_id)["status"] not in statuses and time.time() < deadline:
        time.sleep(0.2)
    return jobs.get(job_id)


def _lanes(pid: int) -> list:
    """The processes a worker process started (multiprocessing spawn: phases and the quick lane),
    without the processes these started."""
    import psutil
    spawned = {}
    for process in psutil.Process(pid).children(recursive=True):
        with contextlib.suppress(psutil.Error):
            if "spawn_main" in " ".join(process.cmdline()):
                spawned[process.pid] = process
    return [p for p in spawned.values() if p.ppid() not in spawned]


def test_worker_process(tmp_path, monkeypatch):
    """`findaline.py worker` as its own process: a restart of the web server (with its own worker)
    leaves its running job alone, cancelling kills the job's processes, and stopping the worker
    (Ctrl+Break here, SIGTERM on Linux) kills them too and marks the job interrupted."""
    import signal
    import subprocess

    import psutil
    with _black_hole() as proxy:
        env = dict(os.environ, HTTP_PROXY=proxy, HTTPS_PROXY=proxy, NO_PROXY="", PYTHONIOENCODING="utf-8")
        jobs = Jobs(str(tmp_path))
        hanging = {"params": {"area": "osm:Nowhere at all"}, "geometry": None}  # Nominatim never answers
        job = jobs.add(hanging, "hanging")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        worker = subprocess.Popen([sys.executable, "findaline.py", "worker", "--data-dir", str(tmp_path)], cwd=REPO,
                                  env=env, creationflags=flags, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8")
        try:
            assert _wait(jobs, job, ("downloading",))["status"] == "downloading"
            external = jobs.get(job)["worker_id"]
            for _ in range(2):  # the web server restarted, with its own worker
                with TestClient(web.create_app(str(tmp_path), token=TOKEN)) as client:
                    assert client.get("/api/jobs").json()["worker"]
            assert jobs.get(job)["status"] == "downloading" and jobs.get(job)["worker_id"] == external
            deadline = time.time() + 60
            while len(_lanes(worker.pid)) < 2 and time.time() < deadline:  # the download and the quick lane
                time.sleep(0.2)
            before = _lanes(worker.pid)
            assert len(before) == 2

            with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)) as client:
                assert client.delete(f"/api/jobs/{job}", headers={"x-findaline-token": TOKEN}).status_code == 200
            assert _wait(jobs, job, ("cancelled", "failed"), 30)["status"] == "cancelled"
            assert not os.path.exists(jobs.folder(job))  # work/jobs/<job>/download removed
            after = {p.pid for p in _lanes(worker.pid)}
            gone = [p for p in before if p.pid not in after]
            assert len(gone) == 1 and not (gone[0].is_running() and gone[0].status() != psutil.STATUS_ZOMBIE)

            second = jobs.add(hanging, "second")
            assert _wait(jobs, second, ("downloading",))["status"] == "downloading"
            if os.name == "nt":
                worker.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                worker.terminate()
            worker.wait(60)
            assert jobs.get(second)["status"] == "failed", jobs.get(second)
            assert not jobs.worker_alive()
        finally:
            if worker.poll() is None:
                worker.kill()
            output = worker.communicate()[0]
    assert "worker" in output, output


def test_stale_temporary_files(tmp_path):
    """The worker's housekeeping deletes temporary files of downloads not written to for a day."""
    folder = tmp_path / "cache" / "poland" / "nmt"
    folder.mkdir(parents=True)
    old, new, kept = folder / "a.tif.x1y2.part", folder / "b.tif.123-456.part", folder / "c.tif"
    for path in (old, new, kept):
        path.write_text("x")
    for path in (old, kept):
        os.utime(path, (time.time() - 2 * 86400,) * 2)
    Scheduler(Jobs(str(tmp_path))).housekeeping()
    assert not old.exists() and new.exists() and kept.exists()


def test_quick_lane(tmp_path, monkeypatch):
    """Estimates are answered by the worker's quick lane: without a worker the server says so; a
    task over the time limit gets its process killed, and a new one answers the next task."""
    headers = {"x-findaline-token": TOKEN}
    hanging = {"params": {"area": "coast", "within": "osm:Nowhere at all"}}  # Nominatim never answers
    with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)) as client:
        response = client.post("/api/estimate", json=hanging, headers=headers)
        assert response.status_code == 503 and "worker is not running" in response.text
    with _black_hole() as proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY"):
            monkeypatch.setenv(key, proxy)  # the processes of the worker inherit it
        monkeypatch.setenv("NO_PROXY", "")
        scheduler = Scheduler(Jobs(str(tmp_path)), quick_timeout=3)
        scheduler.start()
        try:
            with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)) as client:
                while scheduler.quick is None:
                    time.sleep(0.1)
                first = scheduler.quick.pid
                started = time.time()
                response = client.post("/api/estimate", json=hanging, headers=headers)
                assert response.status_code == 504 and "time limit" in response.text, response.text
                assert time.time() - started < 25  # the requests would wait 30 s for Nominatim
                response = client.post("/api/estimate", json={"params": {"lat": 95, "lon": 19.7}}, headers=headers)
                assert response.status_code == 400 and "lat" in response.text, response.text
                assert scheduler.quick.pid != first
        finally:
            scheduler.stop()
    assert Jobs(str(tmp_path))._db().execute("SELECT COUNT(*) FROM quick").fetchone()[0] == 0


def _clip_run(tmp_path, monkeypatch, *extra) -> str:
    """A run of the Żabi Koń clip (sag model) in the data folder tmp_path; its id."""
    import subprocess
    _offline(monkeypatch)
    runs = tmp_path / "runs"
    before = set(os.listdir(runs)) if runs.exists() else set()
    result = subprocess.run(
        [sys.executable, "findaline.py", "--elevation-file", os.path.join(DATA, "zabi_kon_nmt.tif"),
         "--surface-file", os.path.join(DATA, "zabi_kon_nmpt.tif"), "--northing", "254608", "--easting", "553966",
         "--radius", "150", "--model", "sag", "--water", "off", "--fill-gaps", "off", "--no-plot", "--yes",
         "--ortho", "never", "--data-dir", str(tmp_path), *extra],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=600, env=dict(os.environ))
    assert result.returncode == 0, result.stdout + result.stderr
    return next(d for d in set(os.listdir(runs)) - before if os.path.isfile(runs / d / "run.json"))


def _lines_of(tmp_path, run_id):
    text = (tmp_path / "runs" / run_id / "data.js").read_text(encoding="utf-8")
    return json.loads(text.partition(" = ")[2].rstrip().rstrip(";"))["lines"]


def test_profile_on_demand(tmp_path, monkeypatch):
    """A profile computed again from the cache equals the one the search saved."""
    run_id = _clip_run(tmp_path, monkeypatch)
    rows = _lines_of(tmp_path, run_id)
    app = web.create_app(str(tmp_path), token=TOKEN)  # profiles come from the worker's quick lane
    with TestClient(app) as client:
        for row in rows[:3]:
            line = ",".join(str(v) for v in row[:4])
            assert client.get(f"/api/profile?run={run_id}&line={line}").status_code == 403  # header needed
            response = client.get(f"/api/profile?run={run_id}&line={line}", headers={"x-findaline-profile": "1"})
            assert response.status_code == 200, response.text
            assert response.json() == row[-1]  # the smaller grid samples the same pixels
        assert client.get("/api/profile?run=..&line=1,2,3,4", headers={"x-findaline-profile": "1"}).status_code == 404

        # Two points measured on the map: a line shorter than --min-length gets the sag of its own span
        import numpy as np
        import sag
        lat1, lon1, lat2, lon2 = rows[0][:4]
        f = 15 / rows[0][8]
        line = f"{lat1},{lon1},{lat1 + (lat2 - lat1) * f},{lon1 + (lon2 - lon1) * f}"
        p = client.get(f"/api/profile?run={run_id}&line={line}", headers={"x-findaline-profile": "1"}).json()
        d, z = np.array(p["d"]), np.array(p["z"], dtype=float)
        assert 13 < d[-1] < 17
        rig = sag.Rigging(**json.loads((tmp_path / "runs" / run_id / "run.json").read_text("utf-8"))["params"]["rigging"])
        fractions, envelope = sag.envelope(d[-1], z[0] - z[-1], rig)
        limit = z[0] + (z[-1] - z[0]) * d / d[-1] - np.interp(d / d[-1], fractions, envelope) - rig.fall_zone
        assert p["min"][1] == pytest.approx(np.min((limit - z)[1:-1]), abs=0.05)  # between the anchors
        for far in (f"{lat1},{lon1},{lat1 + 0.1},{lon1}", f"{lat1},{lon1},{lat1},{lon1 + 0.000001}"):
            response = client.get(f"/api/profile?run={run_id}&line={far}", headers={"x-findaline-profile": "1"})
            assert response.status_code == 400 and ("km apart" in response.text or "too close" in response.text)


def test_osm_jobs(client, tmp_path):
    import json
    (tmp_path / "cache" / "osm").mkdir(parents=True, exist_ok=True)
    box = {"type": "Polygon", "coordinates": [[[14, 35.7], [14.7, 35.7], [14.7, 36.1], [14, 36.1], [14, 35.7]]]}
    (tmp_path / "cache" / "osm" / "geofabrik-index.json").write_text(json.dumps({"features": [
        {"properties": {"id": "europe", "name": "Europe"}, "geometry": box},
        {"properties": {"id": "malta", "name": "Malta", "parent": "europe"}, "geometry": box}]}))
    headers = {"x-findaline-token": TOKEN}
    response = client.post("/api/osm", json={"action": "download", "region": "europe"}, headers=headers)
    assert response.status_code == 400 and "continent" in response.text
    assert client.post("/api/osm", json={"action": "download", "region": "atlantis"}, headers=headers).status_code == 400
    assert client.post("/api/osm", json={"action": "update", "region": "malta"}, headers=headers).status_code == 400
    assert client.post("/api/osm", json={"action": "delete", "region": "malta"}, headers=headers).status_code == 400
    response = client.post("/api/osm", json={"action": "download", "region": "malta"}, headers=headers)
    assert response.status_code == 200
    jobs = Jobs(str(tmp_path))
    assert jobs.get(response.json()["id"])["title"] == "OSM download: malta"
    again = client.post("/api/osm", json={"action": "download", "region": "malta"}, headers=headers)
    assert again.status_code == 409  # already waiting
    jobs.set(response.json()["id"], "downloading")
    assert client.post("/api/osm", json={"action": "download", "region": "malta"}, headers=headers).status_code == 409
    jobs.set(response.json()["id"], "done")
    assert client.post("/api/osm", json={"action": "download", "region": "malta"}, headers=headers).status_code == 200


def test_run_again_and_edit(client, tmp_path):
    jobs = Jobs(str(tmp_path))
    request = {"params": {"name": "Cliff", "radius": 300}, "geometry": {"type": "Point", "coordinates": [14.2, 36.0]}}
    job = jobs.add(request, "Cliff")
    headers = {"x-findaline-token": TOKEN}
    assert client.post(f"/api/jobs/{job}/again", headers=headers).status_code == 409   # still queued
    assert client.post(f"/api/jobs/{job}/again").status_code == 403                    # no token
    jobs.set(job, "failed", error="interrupted")
    again = client.post(f"/api/jobs/{job}/again", headers=headers).json()["id"]
    assert client.post(f"/api/jobs/{again}/again", headers=headers).status_code == 409  # the copy waits
    jobs.set(again, "done")
    twice = client.post(f"/api/jobs/{again}/again", headers=headers).json()["id"]
    assert jobs.get(again)["title"] == jobs.get(twice)["title"] == "Cliff (again)"
    assert json.loads(jobs.get(twice)["request"]) == request
    detail = client.get(f"/api/jobs/{job}").json()
    assert detail["request"] == request and detail["status"] == "failed"
    assert client.get("/api/jobs/nothing").status_code == 404
    osm_job = jobs.add({"osm": {"action": "download", "region": "malta"}}, "OSM download: malta")
    jobs.set(osm_job, "done")
    copy = client.post(f"/api/jobs/{osm_job}/again", headers=headers).json()["id"]
    assert json.loads(jobs.get(copy)["request"]) == {"osm": {"action": "download", "region": "malta"}}


EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

MEASURE_SCRIPT = """
(function () {
  const out = document.createElement("pre");
  out.id = "result";
  document.body.appendChild(out);
  const row = window.FINDALINE_RUN.lines[0], map = window.FINDALINE_MAP;
  const title = () => (document.querySelector(".fal-profile-title") || {}).textContent || "";
  const wait = (ok, then, tries = 100) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 200)
                                                    : (out.textContent += " | timeout: " + title());
  setTimeout(() => {
    document.querySelector(".fal-measure button").click();
    map.fire("click", {latlng: {lat: row[0], lng: row[1]}});
    map.fire("click", {latlng: {lat: row[2], lng: row[3]}});
    wait(() => title().startsWith("Measured"), () => {
      out.textContent = title();
      const input = document.querySelector(".fal-heights input");
      input.value = "5";
      input.dispatchEvent(new Event("change"));
      setTimeout(() => { out.textContent += " | " + title(); }, 300);
    });
  }, 500);
})();
"""




@contextlib.contextmanager
def _server(data_dir, start_scheduler: bool = False):
    """The app on a real server (uvicorn in a thread) over the data folder; yields its port."""
    import socket
    import threading

    import uvicorn
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(web.create_app(str(data_dir), token=TOKEN, port=port,
                                                          start_scheduler=start_scheduler),
                                           host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        while not server.started:
            time.sleep(0.1)
        yield port
    finally:
        server.should_exit = True
        thread.join(10)


def test_event_stream(tmp_path, monkeypatch):
    """/api/events sends the events after Last-Event-ID (or `since`) with their ids, and keep-alive
    comments while nothing happens."""
    import http.client
    monkeypatch.setattr(web, "PING", 1)
    jobs = Jobs(str(tmp_path))
    jobs.add({"params": {}, "geometry": None}, "first")
    seen = jobs.last_seq()
    second = jobs.add({"params": {}, "geometry": None}, "second")
    with _server(tmp_path) as port:
        for headers, url in (({"Last-Event-ID": str(seen)}, "/api/events"), ({}, f"/api/events?since={seen}")):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            connection.request("GET", url, headers=headers)
            response = connection.getresponse()
            assert response.status == 200 and response.getheader("content-type").startswith("text/event-stream")
            lines, started = [], time.time()
            while not any(line.startswith(":") for line in lines) and time.time() - started < 15:
                lines.append(response.fp.readline().decode("utf-8").rstrip("\r\n"))
            connection.close()
            data = [json.loads(line[5:]) for line in lines if line.startswith("data:")]
            assert {"kind": "worker", "data": {"alive": False}} in data, lines  # sent without an id
            data = [e for e in data if "seq" in e]
            assert [(e["job"], e["data"]["title"]) for e in data] == [(second, "second")], lines
            assert f"id: {data[0]['seq']}" in lines and data[0]["seq"] > seen
            assert any(line.startswith(":") for line in lines), lines  # the keep-alive


def _edge_page(tmp_path, page: str, start_scheduler: bool = False) -> str:
    """The DOM of a page of the app (a path on a real server over tmp_path) after headless Edge ran its scripts."""
    import subprocess
    with _server(tmp_path, start_scheduler) as port:
        return subprocess.run(
            [EDGE, "--headless", "--disable-gpu", f"--user-data-dir={tmp_path / 'edge'}", "--no-first-run",
             "--virtual-time-budget=60000", "--dump-dom", f"http://127.0.0.1:{port}/{page}"],
            capture_output=True, text=True, encoding="utf-8", timeout=180).stdout


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_measure_in_the_map(tmp_path, monkeypatch):
    """The Measure button of a map served by the app: two clicks give the profile between
    them, and a height above the ground raises its anchor."""
    import re
    run_id = _clip_run(tmp_path, monkeypatch)
    row = _lines_of(tmp_path, run_id)[0]
    folder = tmp_path / "runs" / run_id
    (folder / "measure_test.js").write_text(MEASURE_SCRIPT, encoding="utf-8")
    page = (folder / "map.html").read_text(encoding="utf-8")
    (folder / "measure_test.html").write_text(
        page.replace("</body>", '<script src="measure_test.js"></script></body>'), encoding="utf-8")
    dom = _edge_page(tmp_path, f"runs/{run_id}/measure_test.html", start_scheduler=True)
    text = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1)
    first, _, raised = text.partition(" | ")
    assert first.startswith(f"Measured: {row[8]:.0f} m, A1 {row[11]:.1f} m"), text
    assert raised.startswith(f"Measured: {row[8]:.0f} m, A1 {row[11] + 5:.1f} m"), text


# Loaded first on a test page: records every violation of the Content-Security-Policy and
# writes them into <pre id="csp"> after 30 s (virtual time)
CSP_STUB = """
window.CSP_VIOLATIONS = [];
document.addEventListener("securitypolicyviolation", (e) => CSP_VIOLATIONS.push(
  e.violatedDirective + " " + e.blockedURI + " " + (e.sample || "")));
setTimeout(() => {
  const out = document.createElement("pre");
  out.id = "csp";
  out.textContent = JSON.stringify(CSP_VIOLATIONS);
  document.body.appendChild(out);
}, 30000);
"""


def _violations(dom: str) -> list:
    import re
    found = re.search(r'<pre id="csp">(.*?)</pre>', dom, re.S)
    assert found, dom[-2000:]
    return json.loads(found.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<")
                      .replace("&gt;", ">"))


# Before the app: no event stream (headless Edge would wait for it), a fixed estimate
APP_STUBS = CSP_STUB + """
window.EventSource = class { constructor() {} close() {} };
const realFetch = window.fetch;
window.fetch = (url, init) => String(url).startsWith("/api/estimate")
  ? Promise.resolve(new Response(JSON.stringify({source: "test", resolution: 1, km2: 1, tiles: 1, download_mb: 0,
      work_gb: 0, reasons: [], messages: [], area: null}), {headers: {"content-type": "application/json"}}))
  : realFetch(url, init);
"""

APP_SCRIPT = """
(function () {
  const out = document.createElement("pre");
  out.id = "result";
  document.body.appendChild(out);
  const box = (w, s, e, n) => ({type: "Feature", properties: {},
    geometry: {type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}});
  const shapes = {type: "FeatureCollection", features: [box(19.70, 50.10, 19.71, 50.11),
    box(19.705, 50.105, 19.715, 50.115), box(19.80, 50.10, 19.81, 50.11)]};  // two overlap
  const say = (what) => { out.textContent += what + " | "; };
  const areas = () => (document.querySelector(".fa-areas") || {}).textContent || "none";
  const button = (text) => [...document.querySelectorAll("button")].find((b) => b.textContent === text);
  const wait = (ok, then, tries = 100) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 100)
                                                    : say("timeout");
  wait(() => window.FINDALINE_PLACE && button("Draw"), () => {
    button("Draw").click();
    setTimeout(() => {
      const place = window.FINDALINE_PLACE;
      place.loadDrawn(shapes);
      setTimeout(() => {
        say(areas());
        button("Clear all").click();
        setTimeout(() => {
          say(areas() + ", drawn " + (place.drawnAreas() ? "yes" : "no"));
          place.loadDrawn(shapes);
          setTimeout(() => {
            button("Estimate").click();
            wait(() => button("Start") && !button("Start").disabled, () => {
              button("Start").click();
              wait(() => !place.drawnAreas(), () => say("after start: " + areas()));
            });
          }, 200);
        }, 200);
      }, 200);
    }, 200);
  });
})();
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_app_counts_and_clears_areas(tmp_path):
    """The form counts separate drawn areas (overlapping shapes are one), "Clear all"
    removes them, and starting a search clears the map for the next one."""
    import re
    import urllib.request
    folder = tmp_path / "runs" / "apptest"
    folder.mkdir(parents=True)
    (folder / "stubs.js").write_text(APP_STUBS, encoding="utf-8")
    (folder / "test.js").write_text(APP_SCRIPT, encoding="utf-8")
    app = web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)
    with TestClient(app) as client:
        page = client.get("/").text
    (folder / "index.html").write_text(page.replace(
        '<script src="/app/app.js"></script>',
        '<script src="stubs.js"></script><script src="/app/app.js"></script><script src="test.js"></script>'),
        encoding="utf-8")
    dom = _edge_page(tmp_path, "runs/apptest/index.html")
    text = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1)
    drawn, cleared, started = text.split(" | ")[:3]
    assert drawn.startswith("2 separate areas (3 shapes"), text
    assert cleared == "none, drawn no", text
    assert started == "after start: none", text
    assert "The worker is not running" in dom  # no worker in this test
    assert _violations(dom) == []  # style-src 'self': no inline styles in the app
    jobs = Jobs(str(tmp_path))
    queued = jobs.all()
    assert len(queued) == 1
    assert len(json.loads(jobs.get(queued[0]["id"])["request"])["geometry"]["features"]) == 3


AREA_FILES = {
    "crag.kml": """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>Crag</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
19.7522,50.1546,0 19.7590,50.1546,0 19.7590,50.1580,0 19.7522,50.1580,0 19.7522,50.1546,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name>Rock</name><Point><coordinates>19.7545,50.1590,0</coordinates></Point></Placemark>
</Document></kml>""",
    "walk.gpx": """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1"><trk><name>Walk</name><trkseg>
<trkpt lat="50.1550" lon="19.7530"><time>2026-10-27T10:00:00Z</time></trkpt>
<trkpt lat="50.1560" lon="19.7550"><time>2026-10-27T10:05:00Z</time></trkpt>
<trkpt lat="50.1570" lon="19.7560"><time>2026-10-27T10:10:00Z</time></trkpt>
</trkseg></trk></gpx>""",
}

FILES_SCRIPT = """
(function () {
  const out = document.createElement("pre");
  out.id = "result";
  document.body.appendChild(out);
  const say = (what) => { out.textContent += what + " | "; };
  const button = (text) => [...document.querySelectorAll("button")].find((b) => b.textContent === text);
  const hint = () => [...document.querySelectorAll(".fa-hint")].map((p) => p.textContent).join(" ");
  const wait = (ok, then, tries = 100) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 100)
                                                    : say("timeout: " + hint() + " " + document.querySelector(".fa-error")?.textContent);
  const load = (name, then) => {
    const input = document.querySelector("input[type=file]");
    const files = new DataTransfer();
    files.items.add(new File([FILES[name]], name));
    input.files = files.files;
    input.dispatchEvent(new Event("change", {bubbles: true}));
    wait(() => hint().includes(name + ":"), () => { say(hint()); then(); });
  };
  wait(() => window.FINDALINE_PLACE && button("File"), () => {
    button("File").click();
    setTimeout(() => load("crag.kml", () => load("walk.gpx", () => {
      button("Estimate").click();
      wait(() => button("Start") && !button("Start").disabled, () => {
        button("Start").click();
        wait(() => !hint().includes("walk.gpx"), () => say("started"));
      });
    })), 200);
  });
})();
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_app_reads_area_files(tmp_path):
    """KML and GPX files are read in the browser (togeojson); the server takes the shapes as the
    geometry of the job (there is no upload endpoint any more)."""
    import re
    folder = tmp_path / "runs" / "apptest"
    folder.mkdir(parents=True)
    (folder / "stubs.js").write_text(APP_STUBS, encoding="utf-8")
    (folder / "test.js").write_text("const FILES = " + json.dumps(AREA_FILES) + ";\n" + FILES_SCRIPT, encoding="utf-8")
    app = web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)
    with TestClient(app) as client:
        page = client.get("/").text
        assert client.post("/api/area-file", headers={"x-findaline-token": TOKEN}).status_code in (404, 405)
    (folder / "index.html").write_text(page.replace(
        '<script src="/app/app.js"></script>',
        '<script src="stubs.js"></script><script src="/app/app.js"></script><script src="test.js"></script>'),
        encoding="utf-8")
    dom = _edge_page(tmp_path, "runs/apptest/index.html")
    text = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1)
    kml, gpx, started = text.split(" | ")[:3]
    assert kml.startswith("crag.kml: 2 shapes"), text
    assert gpx.startswith("walk.gpx: 1 shapes"), text
    assert started == "started", text
    queued = Jobs(str(tmp_path)).all()
    assert len(queued) == 1 and queued[0]["title"] == "drawn area"
    geometry = json.loads(Jobs(str(tmp_path)).get(queued[0]["id"])["request"])["geometry"]
    assert [f["geometry"]["type"] for f in geometry["features"]] == ["LineString"]
    assert geometry["features"][0]["geometry"]["coordinates"][0][:2] == [19.753, 50.155]


XSS_NAME = "<img src=x onerror=window.XSS=1>"

OTHER_RUN_SCRIPT = """
(function () {
  const out = document.createElement("pre");
  out.id = "result";
  document.body.appendChild(out);
  const map = window.FINDALINE_MAP;
  const wait = (ok, then, tries = 100) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 200)
                                                    : (out.textContent += "timeout: " + JSON.stringify({
      boxes: document.querySelectorAll(".fal-other input").length,
      others: document.querySelector(".fal-others") && document.querySelector(".fal-others").textContent,
      popups: Object.values(map._layers).filter((l) => l.getPopup && l.getPopup()).length}));
  const box = () => [...document.querySelectorAll(".fal-other input")][0];
  const otherLine = () => Object.values(map._layers).find((l) => l.getPopup && l.getPopup()
    && String(l.getPopup().getContent()).includes("Run:"));
  wait(() => box(), () => {
    box().click();
    wait(() => otherLine(), () => {
      otherLine().openPopup();
      setTimeout(() => {
        const popup = document.querySelector(".leaflet-popup-content");
        const swatch = popup.querySelector(".fal-swatch");
        const link = popup.querySelector("a");
        out.textContent = JSON.stringify({swatch: swatch.style.background, link: link.getAttribute("href"),
          text: popup.textContent.slice(0, 200), xss: window.XSS || null, imgs: popup.querySelectorAll("img").length});
      }, 300);
    });
  });
})();
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_map_without_inline_styles(tmp_path, monkeypatch):
    """A map served by the app runs under style-src 'self' without violations; another run shown
    in it, named like an HTML tag, gets its swatch colour and its link, and runs no script."""
    import re
    run_id = _clip_run(tmp_path, monkeypatch)
    other = _clip_run(tmp_path, monkeypatch, "--name", XSS_NAME, "--easting", "554066")  # lines of its own
    folder = tmp_path / "runs" / run_id
    (folder / "csp_stub.js").write_text(CSP_STUB, encoding="utf-8")
    (folder / "other_test.js").write_text(OTHER_RUN_SCRIPT, encoding="utf-8")
    page = (folder / "map.html").read_text(encoding="utf-8")
    assert "<style" not in page and " style=" not in page
    (folder / "other_test.html").write_text(
        page.replace("<head>", '<head><script src="csp_stub.js"></script>')
            .replace("</body>", '<script src="other_test.js"></script></body>'), encoding="utf-8")
    dom = _edge_page(tmp_path, f"runs/{run_id}/other_test.html")
    result = json.loads(re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1)
                        .replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    assert result["swatch"].startswith("rgb"), result       # coloured through the CSSOM
    assert result["link"] == f"../{other}/map.html", result
    assert XSS_NAME in result["text"] and result["xss"] is None and result["imgs"] == 0, result
    assert _violations(dom) == []
    index = (tmp_path / "runs" / "index.html").read_text(encoding="utf-8")
    assert "<style" not in index and "_viewer/index.css" in index and XSS_NAME not in index
    assert os.path.exists(tmp_path / "runs" / "_viewer" / "index.css")


# Between data.js and the viewer: credits as a run records them, one with markup in its texts
CREDITS_STUB = """
window.FINDALINE_RUN.attribution = [
  {data: "FABDEM V1-2", attribution: "FABDEM V1-2 © University of Bristol", licence: "CC BY-NC-SA 4.0",
   url: "https://creativecommons.org/licenses/by-nc-sa/4.0/", notice: ""},
  {data: "Bad", attribution: "<img src=x onerror=window.XSS=1>", licence: "<b>x</b>",
   url: "javascript:window.XSS=2", notice: ""}];
"""

CREDITS_SCRIPT = """
(function () {
  const out = document.createElement("pre");
  out.id = "result";
  document.body.appendChild(out);
  setTimeout(() => {
    const control = document.querySelector(".leaflet-control-attribution");
    const panel = document.querySelector(".fal-credits");
    out.textContent = JSON.stringify({control: control.textContent, panel: panel.textContent,
      links: [...control.querySelectorAll("a"), ...panel.querySelectorAll("a")].map((a) => a.getAttribute("href")),
      imgs: control.querySelectorAll("img").length + panel.querySelectorAll("img").length,
      xss: window.XSS || null});
  }, 1000);
})();
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_map_shows_data_credits(tmp_path, monkeypatch):
    """The map shows the credits of its data in the attribution control and the run panel,
    as text: markup in them is not rendered and only http(s) addresses become links."""
    import re
    run_id = _clip_run(tmp_path, monkeypatch)
    folder = tmp_path / "runs" / run_id
    assert (folder / "ATTRIBUTION.txt").exists()
    (folder / "credits_stub.js").write_text(CREDITS_STUB, encoding="utf-8")
    (folder / "credits_test.js").write_text(CREDITS_SCRIPT, encoding="utf-8")
    page = (folder / "map.html").read_text(encoding="utf-8")
    (folder / "credits_test.html").write_text(
        page.replace('<script src="data.js"></script>', '<script src="data.js"></script><script src="credits_stub.js">'
                                                         '</script>')
            .replace("</body>", '<script src="credits_test.js"></script></body>'), encoding="utf-8")
    dom = _edge_page(tmp_path, f"runs/{run_id}/credits_test.html")
    result = json.loads(re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1)
                        .replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    assert "Data: FABDEM V1-2 © University of Bristol (CC BY-NC-SA 4.0)" in result["control"], result
    assert "<img src=x onerror=window.XSS=1> (<b>x</b>)" in result["control"], result
    assert "FABDEM V1-2 (CC BY-NC-SA 4.0); Bad (<b>x</b>)" in result["panel"], result
    links = result["links"]
    assert links.count("https://creativecommons.org/licenses/by-nc-sa/4.0/") == 2 and "ATTRIBUTION.txt" in links, result
    assert not any(link.startswith("javascript") for link in links if link), result
    assert result["imgs"] == 0 and result["xss"] is None, result
