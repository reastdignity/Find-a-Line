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
import messages  # noqa: E402
from jobs import Jobs, Scheduler  # noqa: E402
from settings import Limits  # noqa: E402
import web  # noqa: E402

pytest.importorskip("httpx2")
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "test-token"
LOCAL = "http://127.0.0.1:8765"  # the local app's own host (it refuses TestClient's "testserver")


@pytest.fixture
def client(tmp_path):
    app = web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)
    with TestClient(app, base_url=LOCAL) as c:
        yield c


def test_guards(client, tmp_path):
    assert client.get("/api/schema", headers={"host": "evil.example"}).status_code == 400  # DNS rebinding
    assert client.post("/api/jobs", json={"params": {}}).status_code == 403          # no token
    headers = {"x-findaline-token": TOKEN}
    foreign = {**headers, "origin": "https://evil.example"}
    assert client.post("/api/jobs", json={"params": {}}, headers=foreign).status_code == 403
    own = {**headers, "origin": f"http://127.0.0.1:{web.PORT}"}
    assert client.post("/api/jobs", json={"params": {"lat": 50, "lon": 19}}, headers=own).status_code == 200
    for params in ({"elevation_file": "x.tif"}, {"opentopo_key": "k"}, {"area": "C:/secret.geojson"}):
        response = client.post("/api/jobs", json={"params": params}, headers=headers)
        assert response.status_code == 400, params
    (tmp_path / "cache" / "poland").mkdir(parents=True)
    (tmp_path / "cache" / "poland" / "index.sqlite").write_text("x")
    assert client.get("/cache/poland/index.sqlite").status_code == 404               # only orthophoto tiles
    page = client.get("/")
    assert TOKEN in page.text and "script-src 'self'" in page.headers["content-security-policy"]
    assert 'href="/app/favicon.svg"' in page.text and client.get("/app/favicon.svg").status_code == 200


def test_values_checked(client, tmp_path):
    """Finite values in range, the choices of the form, and a title from the validated values."""
    headers = {"x-findaline-token": TOKEN}
    point = {"lat": 50.1, "lon": 19.7}
    for params in ({"lat": 91, "lon": 19.7}, {**point, "source": "files"}, {**point, "slope_threshold": 0},
                   {**point, "ortho_detail": -1}, {**point, "name": "x" * 101}, {**point, "name": "a\nb"},
                   {"area": "coast", "within": "36.1,14.2,36.0,14.3"}, {"area": "coast", "within": "nan,1,2,3"},
                   {"area": "osm:" + "A" * 197}, {"area": "coast", "within": "osm:" + "A" * 197},
                   {"area": "osm:Gozo\u0000"}, {"area": "coast", "within": "osm:Go\nzo"}):
        response = client.post("/api/jobs", json={"params": params}, headers=headers)
        assert response.status_code == 400, params
    for text in ('{"params": {"lat": NaN, "lon": 19.7}}', '{"params": {"lat": 50, "lon": 19.7, "radius": Infinity}}'):
        response = client.post("/api/jobs", content=text, headers={**headers, "content-type": "application/json"})
        assert response.status_code == 400, text
    response = client.post("/api/jobs", json={"params": {"lat": "50.1", "lon": "19.7", "radius": 100}},
                           headers=headers)
    assert response.status_code == 200, response.text
    assert Jobs(str(tmp_path)).get(response.json()["id"])["title"] == "50.10000, 19.70000 (r 100 m)"


def test_long_area_names(client):
    """--area osm:<name> and --within have at most 200 characters (review B, poc_long_area); a job's
    title at most MAX_TITLE."""
    import params as pm
    headers = {"x-findaline-token": TOKEN}
    name = "osm:" + "A" * 196
    response = client.post("/api/jobs", json={"params": {"area": name}}, headers=headers)
    assert response.status_code == 200, response.text
    titles = [j["title"] for j in client.get("/api/jobs").json()["jobs"]]
    assert len(titles[0]) == web.MAX_TITLE and titles[0].endswith("…")
    assert web.title(pm.SearchParams(area="osm:Gozo")) == "osm:Gozo"


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
    assert not os.path.exists(jobs.folder(job))  # tile_core.json and the grids are gone with the job

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
    """A jobs.sqlite from before the schema versions keeps its jobs and gets the newest version,
    its times (the local time of the process then) in UTC; a failing step leaves a database as it was."""
    import sqlite3
    from datetime import datetime, timezone

    import cache
    import jobs as jb
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    db.executescript("""
        CREATE TABLE jobs (id TEXT PRIMARY KEY, created TEXT, status TEXT, request TEXT, title TEXT,
                           run_id TEXT, error TEXT, finished TEXT);
        CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, kind TEXT, data TEXT);
        INSERT INTO jobs VALUES ('old', '2026-10-01T10:00:00', 'done', '{}', 'Old job', 'run', NULL,
                                 '2026-10-01T11:00:00');
        INSERT INTO events (job, kind, data) VALUES
            ('old', 'status', '{"status": "queued", "title": "Old job", "created": "2026-10-01T10:00:00"}'),
            ('old', 'info', '"a message"');""")
    db.close()
    jobs = Jobs(str(tmp_path))
    utc = {t: datetime.fromisoformat(t).astimezone(timezone.utc).isoformat(timespec="seconds")
           for t in ("2026-10-01T10:00:00", "2026-10-01T11:00:00")}
    old = jobs.get("old")
    assert old["title"] == "Old job" and old["created"] == utc["2026-10-01T10:00:00"]
    assert old["finished"] == utc["2026-10-01T11:00:00"] and old["created"].endswith("+00:00")
    events = [e["data"] for e in jobs.events(0)]
    assert events == [{"status": "queued", "title": "Old job", "created": utc["2026-10-01T10:00:00"]}, "a message"]
    assert cache.utc_iso(old["created"]) == old["created"] and cache.utc_iso("soon") == "soon"
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


def test_status_events_in_the_order_of_the_changes(tmp_path):
    """A status change and its event are one transaction: a job set "ready" by its download process
    while the scheduler claims it for computing never gets the events "computing" before "ready"
    (the page would show "ready" while the job computes; CI on Windows, 2026-10-31)."""
    import threading
    jobs = Jobs(str(tmp_path))
    ids = [jobs.add({"params": {}}, f"job {k}") for k in range(150)]
    for job_id in ids:
        jobs.set(job_id, "downloading", when=("queued",))
    readied = threading.Event()

    def claim():
        while True:
            if not jobs.claim("ready", "computing", "w") and readied.is_set():
                return

    claimer = threading.Thread(target=claim)
    claimer.start()
    for job_id in ids:
        jobs.set(job_id, "ready", when=("downloading",))
    readied.set()
    claimer.join(timeout=60)
    assert not claimer.is_alive()
    for job_id in ids:
        statuses = [e["data"]["status"] for e in jobs.events(0, job_id) if e["kind"] == "status"]
        assert statuses == ["queued", "downloading", "ready", "computing"], (job_id, statuses)


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
    assert not os.path.exists(jobs.folder(job))
    assert jobs.get(other)["status"] == "cancelled"
    kinds = {e["kind"] for e in jobs.events(0, job)}
    assert {"status", "info", "progress"} <= kinds
    statuses = [e["data"]["status"] for e in jobs.events(0, job) if e["kind"] == "status"]
    assert statuses == ["queued", "downloading", "ready", "computing", "done"]
    steps = {e["data"]["step"].split(":")[0] for e in jobs.events(0, job) if e["kind"] == "progress"} - {""}
    assert steps == {f"Step {n}/5" for n in range(1, 6)}, steps
    stages = {e["data"]["stage"] for e in jobs.events(0, job) if e["kind"] == "progress"}
    assert {"checking lines", "coordinates", "saving results", "saving the map"} <= stages, stages


def test_two_jobs_computing_at_once(tmp_path, monkeypatch):
    """With two compute slots two jobs compute at the same time in their own processes and both
    save their runs; the slot's memory budget reaches the search of each phase (run_phase)."""
    import search
    from settings import Limits
    _offline(monkeypatch)
    jobs = Jobs(str(tmp_path))
    params = {"elevation_file": os.path.join(DATA, "zabi_kon_nmt.tif"), "northing": 254608, "easting": 553966,
              "radius": 150, "water": "off", "fill_gaps": "off", "ortho": "never"}
    added = [jobs.add({"params": {**params, "name": f"Clip {i}"}, "geometry": None}, f"clip {i}") for i in range(2)]
    scheduler = Scheduler(jobs, limits=Limits(compute_slots=2, download_slots=2))
    assert scheduler.memory and scheduler.memory > 2 ** 28
    scheduler.start()
    try:
        for job in added:
            _wait(jobs, job, ("done", "failed", "cancelled"), 300)
    finally:
        scheduler.stop()
    assert [jobs.get(job)["status"] for job in added] == ["done", "done"], [jobs.get(j)["error"] for j in added]
    seq = {(job, e["data"]["status"]): e["seq"] for job in added for e in jobs.events(0, job) if e["kind"] == "status"}
    assert seq[(added[1], "computing")] < seq[(added[0], "done")], seq  # the second computed beside the first

    # The budget given to a phase process is the search's Plan.memory (and sizes its tiles)
    seen = []

    def plan_search(params, paths, on_event, limits=None, memory=None):
        seen.append(memory)
        raise ValueError("planned")
    monkeypatch.setattr(search, "plan_search", plan_search)
    monkeypatch.setattr(sys, "stdout", sys.stdout)  # run_phase sends it to the job's events
    job = jobs.add({"params": params, "geometry": None}, "budget")
    jobs.set(job, "downloading")
    import jobs as jb
    jb.run_phase(str(tmp_path), job, "download", Limits(), False, 123456789)
    assert seen == [123456789] and jobs.get(job)["status"] == "failed"


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
    from jobs import Running
    scheduler = Scheduler(jobs)
    scheduler.running["download"].append(Running(job, _Ended(), time.time()))
    scheduler.running["compute"].append(Running(job, type("Alive", (), {"is_alive": lambda self: True})(), time.time()))
    scheduler._check()
    assert jobs.get(job)["status"] == "computing" and not scheduler.running["download"]
    scheduler.running["compute"] = [Running(job, _Ended(), time.time())]
    scheduler._check()
    assert jobs.get(job)["status"] == "failed"


class _Phase:
    """A fake process of a phase: alive until it is ended or killed (kill_tree finds no such pid)."""
    started = []

    def __init__(self, target=None, daemon=None, args=()):
        self.args, self.alive, self.exitcode, self.pid = args, False, None, 2 ** 31 - 2

    def start(self):
        self.alive = True
        _Phase.started.append(self)

    def is_alive(self):
        return self.alive

    def join(self):
        self.alive = False


def test_slots_of_the_worker(tmp_path, monkeypatch):
    """With Limits.compute_slots and download_slots the worker runs that many jobs of each phase
    at once (1 and 1 by default); cancelling one of two running jobs kills only its process, a
    phase over its time limit only its own, and each slot's process gets the slot's memory budget."""
    import jobs as jb
    import search
    from settings import Limits
    _Phase.started.clear()
    assert Limits().compute_slots == 1 and Limits().download_slots == 1 and Limits.load(True).compute_slots == 1
    monkeypatch.setenv("FINDALINE_LIMIT_COMPUTE_SLOTS", "3")
    assert Limits.load(True).compute_slots == 3
    monkeypatch.delenv("FINDALINE_LIMIT_COMPUTE_SLOTS")
    monkeypatch.setattr(search, "available_memory", lambda: 20 * 2 ** 30)
    jobs = Jobs(str(tmp_path))
    added = [jobs.add({"params": {}, "geometry": None}, f"job {i}") for i in range(5)]
    one = Scheduler(jobs)
    assert one.slots == {"download": 1, "compute": 1} and one.memory is None  # locally: as before

    scheduler = Scheduler(jobs, limits=Limits(compute_slots=2, download_slots=3, compute_seconds=60))
    assert scheduler.memory == search.slot_memory(scheduler.limits) == int(
        (20 * 2 ** 30 - search.MAX_MEASURED_PX ** 2 * search.PIXEL_BYTES - 3 * search.DOWNLOAD_PROCESS_BYTES) // 2)
    monkeypatch.setattr(scheduler, "context", type("Spawn", (), {"Process": _Phase})())
    scheduler._begin()
    try:
        scheduler._check()
        downloading = [r.job_id for r in scheduler.running["download"]]
        assert downloading == added[:3] and not scheduler.running["compute"]
        assert all(jobs.get(j)["status"] == "downloading" for j in downloading)
        assert all(p.args[2:] == ("download", scheduler.limits, False, scheduler.memory) for p in _Phase.started)
        for job in added[:3]:  # their downloads end
            jobs.set(job, "ready")
        for running in scheduler.running["download"]:
            running.process.alive = False
        scheduler._check()
        computing = [r.job_id for r in scheduler.running["compute"]]
        assert computing == added[:2] and [r.job_id for r in scheduler.running["download"]] == added[3:]
        assert jobs.get(added[2])["status"] == "ready"  # waits for a compute slot
        assert scheduler.running["compute"][0].process.args[2:] == ("compute", scheduler.limits, False,
                                                                    scheduler.memory)

        # Cancelling one of the two computing jobs: its process only, then the next one takes the slot
        first = scheduler.running["compute"][0].process
        assert jobs.request_cancel(added[0])
        scheduler._check()
        assert jobs.get(added[0])["status"] == "cancelled" and not first.alive
        assert [r.job_id for r in scheduler.running["compute"]] == [added[1], added[2]]
        assert all(r.process.alive for r in scheduler.running["compute"])

        # A phase over its time limit: only that one fails
        late = scheduler.running["compute"][0]
        scheduler.running["compute"][0] = late._replace(started=time.time() - 61)
        scheduler._check()
        assert jobs.get(added[1])["status"] == "failed" and jobs.get(added[1])["error"].startswith("time limit")
        assert jobs.get(added[2])["status"] == "computing" and not late.process.alive
        # A process that ended without a word fails its job only
        scheduler.running["download"][0].process.alive = False
        scheduler._check()
        assert jobs.get(added[3])["status"] == "failed" and jobs.get(added[4])["status"] == "downloading"
    finally:
        scheduler.stop()
    assert all(not p.alive for p in _Phase.started) and not any(scheduler.running.values())
    assert jobs.get(added[2])["status"] == "failed" and jobs.get(added[2])["error"] == jb.INTERRUPTED
    _Phase.started.clear()


def test_fair_claiming(tmp_path):
    """The worker takes the oldest waiting job of the user with the fewest jobs running (in any
    phase, any worker); the local app's jobs (no user) count as one user's."""
    jobs = Jobs(str(tmp_path))
    a1, a2, a3 = (jobs.add({"params": {}, "geometry": None}, f"a{i}", user_id=1) for i in range(3))
    b1, b2 = (jobs.add({"params": {}, "geometry": None}, f"b{i}", user_id=2) for i in range(2))
    l1, l2 = (jobs.add({"params": {}, "geometry": None}, f"local{i}") for i in range(2))
    order = []
    while (job := jobs.claim("queued", "downloading")) is not None:
        order.append(job["id"])
    # a1 (all at 0, oldest), b1 (user 2 at 0), l1 (no user at 0), then a2, b2, l2 at 1 each, a3
    assert order == [a1, b1, l1, a2, b2, l2, a3]
    # A user's jobs computing count too, and a finished one does not
    c1, c2 = (jobs.add({"params": {}, "geometry": None}, f"c{i}", user_id=3) for i in range(2))
    d1 = jobs.add({"params": {}, "geometry": None}, "d1", user_id=4)
    for job in (a1, a2, a3, b1, b2, l1, l2):
        jobs.set(job, "done")
    jobs.set(c1, "computing")
    jobs.set(c2, "ready")
    jobs.set(d1, "ready")
    assert jobs.claim("ready", "computing")["id"] == d1  # user 3 has c1 computing
    assert jobs.claim("ready", "computing")["id"] == c2


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
    with TestClient(app, base_url=LOCAL) as client:
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
                with TestClient(web.create_app(str(tmp_path), token=TOKEN), base_url=LOCAL) as client:
                    assert client.get("/api/jobs").json()["worker"]
            assert jobs.get(job)["status"] == "downloading" and jobs.get(job)["worker_id"] == external
            deadline = time.time() + 60
            while len(_lanes(worker.pid)) < 2 and time.time() < deadline:  # the download and the quick lane
                time.sleep(0.2)
            before = _lanes(worker.pid)
            assert len(before) == 2

            with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False), base_url=LOCAL) as client:
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
    with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False), base_url=LOCAL) as client:
        response = client.post("/api/estimate", json=hanging, headers=headers)
        assert response.status_code == 503 and response.json()["detail"]["code"] == "no_worker_local"
    with _black_hole() as proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY"):
            monkeypatch.setenv(key, proxy)  # the processes of the worker inherit it
        monkeypatch.setenv("NO_PROXY", "")
        scheduler = Scheduler(Jobs(str(tmp_path)), limits=Limits(quick_seconds=3))
        scheduler.start()
        try:
            with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False), base_url=LOCAL) as client:
                while scheduler.quick is None:
                    time.sleep(0.1)
                first = scheduler.quick.pid
                started = time.time()
                response = client.post("/api/estimate", json=hanging, headers=headers)
                assert response.status_code == 504 and "time limit" in response.text, response.text
                assert time.time() - started < 25  # the requests would wait 30 s for Nominatim
                response = client.post("/api/estimate", json={"params": {"lat": 95, "lon": 19.7}}, headers=headers)
                assert response.status_code == 400 and response.json()["detail"] == {
                    "message": messages.text("error.lat_lon_range"), "code": "lat_lon_range", "params": {}},                     response.text
                assert scheduler.quick.pid != first
        finally:
            scheduler.stop()
    assert Jobs(str(tmp_path))._db().execute("SELECT COUNT(*) FROM quick").fetchone()[0] == 0


def test_quick_lane_idle_and_prompt(tmp_path, monkeypatch):
    """The idle quick lane only reads (no write transaction while nothing waits), and a task that
    arrives is answered within a few of its looks."""
    import subprocess
    import threading

    import jobs as jb
    jobs = Jobs(str(tmp_path))
    writes = []
    original = jb.Jobs._write

    def counted(self):
        writes.append(threading.current_thread().name)
        return original(self)
    monkeypatch.setattr(jb.Jobs, "_write", counted)
    monkeypatch.setitem(jb.QUICK, "noop", lambda data_dir, request, limits: {"ok": request["n"]})
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])  # the lane's "worker"
    lane = threading.Thread(target=jb.run_quick, args=(str(tmp_path), "w1", parent.pid), name="lane", daemon=True)
    try:
        lane.start()
        time.sleep(1.5)  # importing search and its looks at the table
        assert "lane" not in writes
        for n in range(5):
            started = time.monotonic()
            task = jobs.quick_add("noop", {"n": n})
            while (answer := jobs.quick_take(task)) is None:
                assert time.monotonic() - started < 2
                time.sleep(0.01)
            assert answer == {"code": 200, "body": {"ok": n}} and time.monotonic() - started < 0.5
        assert writes.count("lane") == 5  # the claim of each task, nothing between
    finally:
        parent.kill()
        parent.wait()
        lane.join(10)
    assert not lane.is_alive()


def test_quick_wait_ends(tmp_path, monkeypatch):
    """The server waits for the quick lane at most the lane's time limit per task ahead and its own
    (plus QUICK_SLACK), and not after the client went away; either way it deletes its waiting task.
    A running task stays for the lane's own time limit (review B, poc_quick_wait)."""
    import requests
    monkeypatch.setattr(web, "QUICK_SLACK", 0.5)
    jobs = Jobs(str(tmp_path))
    jobs.register("busy-worker")  # a worker is alive, its quick lane never answers here
    headers = {"x-findaline-token": TOKEN}
    point = {"params": {"lat": 50.1, "lon": 19.7}}
    app = web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False, limits=Limits(quick_seconds=0.5))
    with TestClient(app, base_url=LOCAL) as client:
        ahead = jobs.quick_add("estimate", {})  # one task ahead: 2 x 0.5 + 0.5 s
        jobs.quick_claim("busy-worker")
        started = time.time()
        response = client.post("/api/estimate", json=point, headers=headers)
        assert response.status_code == 504 and "1.5 s" in response.text, response.text
        assert time.time() - started < 5
    assert [r[0] for r in jobs._db().execute("SELECT id FROM quick")] == [ahead]  # the running one stays
    jobs.quick_forget(ahead)
    assert [r[0] for r in jobs._db().execute("SELECT id FROM quick")] == [ahead]
    with _server(tmp_path) as port:  # the local app: no time limit, but the client goes away
        jobs.heartbeat("busy-worker")
        with pytest.raises(requests.Timeout):
            requests.post(f"http://127.0.0.1:{port}/api/estimate", json=point, headers=headers, timeout=1.5)
        deadline = time.time() + 10
        while jobs._db().execute("SELECT COUNT(*) FROM quick WHERE status = 'waiting'").fetchone()[0] \
                and time.time() < deadline:
            time.sleep(0.2)
        assert jobs._db().execute("SELECT COUNT(*) FROM quick WHERE status = 'waiting'").fetchone()[0] == 0


def test_job_events_capped(client, tmp_path):
    """/api/jobs/<id>/events gives the newest JOB_EVENTS_MAX messages and the last progress, not every
    event of a long job."""
    import jobs as jb
    jobs = client.app.state.jobs
    job = jobs.add({"params": {}, "geometry": None}, "long")
    for i in range(jb.JOB_EVENTS_MAX + 50):
        jobs.event(job, "info", f"line {i}")
        jobs.event(job, "progress", {"done": i})
    answer = client.get(f"/api/jobs/{job}/events").json()
    kinds = [e["kind"] for e in answer["events"]]
    assert kinds.count("progress") == 1 and kinds.count("info") == jb.JOB_EVENTS_MAX  # the oldest (status) left out
    assert answer["events"][-1]["data"] == {"done": jb.JOB_EVENTS_MAX + 49}  # the last progress
    assert answer["events"][-2]["data"] == f"line {jb.JOB_EVENTS_MAX + 49}" and answer["omitted"] == 51
    seqs = [e["seq"] for e in answer["events"]]
    assert seqs == sorted(seqs)


def test_time_limits(tmp_path, monkeypatch):
    """Limits come from FINDALINE_LIMIT_* (none by default); a phase over its limit gets its processes
    killed and its folder removed, and its job fails with "time limit"."""
    import pydantic
    assert Limits().download_seconds is None
    monkeypatch.setenv("FINDALINE_LIMIT_COMPUTE_SECONDS", "7200")
    assert Limits().compute_seconds == 7200 and Limits().phase_seconds("compute") == 7200
    monkeypatch.setenv("FINDALINE_LIMIT_QUICK_SECONDS", "-1")
    with pytest.raises(pydantic.ValidationError):
        Limits()
    monkeypatch.delenv("FINDALINE_LIMIT_QUICK_SECONDS")
    with _black_hole() as proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY"):
            monkeypatch.setenv(key, proxy)  # the processes of the worker inherit it
        monkeypatch.setenv("NO_PROXY", "")
        jobs = Jobs(str(tmp_path))
        job = jobs.add({"params": {"area": "osm:Nowhere at all"}, "geometry": None}, "hanging")
        scheduler = Scheduler(jobs, limits=Limits(download_seconds=3))
        scheduler.start()
        try:
            result = _wait(jobs, job, ("failed", "done", "cancelled"), 25)  # Nominatim would wait 30 s
        finally:
            scheduler.stop()
    assert result["status"] == "failed" and result["error"].startswith("time limit"), result
    assert not os.path.exists(jobs.folder(job))


def test_time_limit_of_osm_jobs(tmp_path, monkeypatch):
    """A job of an OSM extract has its own time limit (Limits.osm_seconds), not the download phase's."""
    with _black_hole() as proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY"):
            monkeypatch.setenv(key, proxy)  # the Geofabrik index never comes
        monkeypatch.setenv("NO_PROXY", "")
        jobs = Jobs(str(tmp_path))
        job = jobs.add({"osm": {"action": "download", "region": "malta"}}, "OSM download: malta")
        scheduler = Scheduler(jobs, limits=Limits(download_seconds=1, osm_seconds=6))
        scheduler.start()
        try:
            assert _wait(jobs, job, ("downloading",))["status"] == "downloading"
            time.sleep(3)  # over the download phase's limit
            assert jobs.get(job)["status"] == "downloading"
            result = _wait(jobs, job, ("failed", "done", "cancelled"), 25)
        finally:
            scheduler.stop()
    assert result["status"] == "failed" and result["error"] == "time limit: the OSM extract job was stopped after 6 s"


def test_housekeeping(tmp_path):
    """Events of jobs finished long ago, answers nobody took, the work folders of ended jobs after a
    week and leftovers of estimates and profiles after a day are deleted; everything else stays."""
    jobs = Jobs(str(tmp_path))
    old, recent, running = (jobs.add({"params": {}, "geometry": None}, name) for name in ("old", "recent", "running"))
    jobs.set(old, "done")
    jobs.set(recent, "failed", error="x")
    jobs.set(running, "computing")
    with jobs._db() as db:
        db.execute("UPDATE jobs SET finished = '2020-01-01T00:00:00' WHERE id = ?", (old,))
    taken = jobs.quick_add("estimate", {})
    with jobs._db() as db:
        db.execute("UPDATE quick SET status = 'done', created = ? WHERE id = ?", (time.time() - 7200, taken))
    fresh = jobs.quick_add("estimate", {})
    awaited, orphan = jobs.quick_add("estimate", {}), jobs.quick_add("estimate", {})  # waiting 2 h and 2 days
    with jobs._db() as db:
        db.execute("UPDATE quick SET created = ? WHERE id = ?", (time.time() - 7200, awaited))
        db.execute("UPDATE quick SET created = ? WHERE id = ?", (time.time() - 2 * 86400, orphan))
    week_ago = time.time() - 8 * 86400
    folders = {name: tmp_path / "work" / kind / name for kind, name in (
        ("jobs", old), ("jobs", running), ("jobs", "unknown"), ("estimates", "a"), ("profiles", "b"))}
    for folder in folders.values():
        folder.mkdir(parents=True)
        os.utime(folder, (week_ago, week_ago))
    (tmp_path / "work" / "estimates" / "new").mkdir()
    Scheduler(jobs).housekeeping()
    assert jobs.events(0, old) == [] and jobs.events(0, recent) and jobs.events(0, running)
    assert jobs.get(old)["status"] == "done"  # the job stays
    remaining = {r[0] for r in jobs._db().execute("SELECT id FROM quick")}
    assert remaining == {fresh, awaited}  # a task someone may still wait for stays
    assert [n for n, f in folders.items() if f.exists()] == [running]
    assert (tmp_path / "work" / "estimates" / "new").exists()


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
    with TestClient(app, base_url=LOCAL) as client:
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


def _edge_page(tmp_path, page: str, start_scheduler: bool = False, size: str = "", profile: str = "edge") -> str:
    """The DOM of a page of the app (a path on a real server over tmp_path) after headless Edge ran its scripts
    (`size`: the window's "width,height"; Edge's headless window is at least about 500 px wide)."""
    import subprocess
    with _server(tmp_path, start_scheduler) as port:
        return subprocess.run(
            [EDGE, "--headless", "--disable-gpu", f"--user-data-dir={tmp_path / profile}", "--no-first-run",
             *([f"--window-size={size}"] if size else []),
             "--virtual-time-budget=60000", "--dump-dom", f"http://127.0.0.1:{port}/{page}"],
            capture_output=True, text=True, encoding="utf-8", timeout=180).stdout


# The intros of the app (ui/Intro.tsx) put away, so that no dialog covers a test's page
INTROS_SEEN = """
try { for (const name of ["search", "result"]) localStorage.setItem("findaline.intro." + name, "seen"); } catch (e) {}
"""

# Loaded first on a test page: records every violation of the Content-Security-Policy and
# writes them into <pre id="csp"> after 30 s (virtual time); the intros put away (INTROS_SEEN)
CSP_STUB = INTROS_SEEN + """
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
  const areas = () => (document.querySelector(".s-areas") || {}).textContent || "none";
  const button = (text) => [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === text);
  const wait = (ok, then, tries = 100) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 100)
                                                    : say("timeout");
  wait(() => window.FINDALINE_PLACE && button("Area"), () => {
    button("Area").click();
    setTimeout(() => {
      const place = window.FINDALINE_PLACE;
      place.loadDrawn(shapes);
      wait(() => areas() !== "none", () => {
        say(areas());
        button("Clear the place").click();
        wait(() => areas() === "none", () => {
          say(areas() + ", drawn " + (place.drawnAreas() ? "yes" : "no"));
          place.loadDrawn(shapes);
          wait(() => button("Start search") && !button("Start search").disabled, () => {
            button("Start search").click();
            wait(() => !location.hash.startsWith("#new"), () => say("started: " + location.hash));
          });
        });
      });
    }, 200);
  });
})();
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_app_counts_and_clears_areas(tmp_path):
    """The new search (D) counts separate drawn areas (overlapping shapes are one), "Clear the place"
    removes them, and a search starts with all the shapes drawn again."""
    import re
    import urllib.request
    folder = tmp_path / "runs" / "apptest"
    folder.mkdir(parents=True)
    (folder / "stubs.js").write_text(APP_STUBS, encoding="utf-8")
    (folder / "test.js").write_text(APP_SCRIPT, encoding="utf-8")
    app = web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)
    with TestClient(app, base_url=LOCAL) as client:
        page = client.get("/").text
    (folder / "index.html").write_text(page.replace(
        '<script src="/app/app.js"></script>',
        '<script src="stubs.js"></script><script src="/app/app.js"></script><script src="test.js"></script>'),
        encoding="utf-8")
    dom = _edge_page(tmp_path, "runs/apptest/index.html?ui=d#new")
    text = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1)
    drawn, cleared, started = text.split(" | ")[:3]
    assert drawn.startswith("2 separate areas (3 shapes"), text
    assert cleared == "none, drawn no", text
    assert started.startswith("started: "), text
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
  const button = (text) => [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === text);
  const where = () => (document.querySelector(".s-where") || {}).textContent || "";
  const help = (name) => { const at = where().indexOf(name + ": "); return at < 0 ? "" : where().slice(at).split(" shapes")[0] + " shapes"; };
  const wait = (ok, then, tries = 100) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 100)
                                                    : say("timeout: " + where());
  const load = (name, then) => {
    const input = document.querySelector("input.s-file");
    const files = new DataTransfer();
    files.items.add(new File([FILES[name]], name));
    input.files = files.files;
    input.dispatchEvent(new Event("change", {bubbles: true}));
    wait(() => help(name), () => { say(help(name)); then(); });
  };
  wait(() => window.FINDALINE_PLACE && button("File"), () => {
    button("File").click();
    setTimeout(() => load("crag.kml", () => load("walk.gpx", () => {
      wait(() => button("Start search") && !button("Start search").disabled, () => {
        button("Start search").click();
        wait(() => !location.hash.startsWith("#new"), () => say("started"));
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
    with TestClient(app, base_url=LOCAL) as client:
        page = client.get("/").text
        assert client.post("/api/area-file", headers={"x-findaline-token": TOKEN}).status_code in (404, 405)
    (folder / "index.html").write_text(page.replace(
        '<script src="/app/app.js"></script>',
        '<script src="stubs.js"></script><script src="/app/app.js"></script><script src="test.js"></script>'),
        encoding="utf-8")
    dom = _edge_page(tmp_path, "runs/apptest/index.html?ui=d#new")
    text = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1)
    assert text.count(" | ") >= 3, text
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

# Between data.js and the viewer: credits as a run records them, one with markup in its texts
CREDITS_STUB = """
window.FINDALINE_RUN.attribution = [
  {data: "FABDEM V1-2", attribution: "FABDEM V1-2 © University of Bristol", licence: "CC BY-NC-SA 4.0",
   url: "https://creativecommons.org/licenses/by-nc-sa/4.0/", notice: ""},
  {data: "Bad", attribution: "<img src=x onerror=window.XSS=1>", licence: "<b>x</b>",
   url: "javascript:window.XSS=2", notice: ""}];
"""

# The steps of a test of a run's map.html (the result view of the app, result/view/RunOnMap): each step
# runs when its page shows what it waits for; the result (or where it stopped) goes into <pre id="result">
MAP_HELPERS = """
const out = document.createElement("pre");
out.id = "result";
const result = {};
const done = () => { out.textContent = JSON.stringify(result); document.body.appendChild(out); };
let step = "";
const show = () => { result.at = step; done(); };  // what was seen so far, should the page's time run out
window.addEventListener("error", (e) => { result.error = e.message; show(); });
window.addEventListener("unhandledrejection", (e) => { result.error = String(e.reason); show(); });
const $ = (s) => document.querySelector(s), $$ = (s) => [...document.querySelectorAll(s)];
const text = (s) => ($(s) || {}).textContent || "";
const button = (label) => $$("button").find((b) => b.textContent.trim() === label || b.getAttribute("aria-label") === label);
const rows = () => $$(".b-results .r-rows > .r-spot > .r-row");
const model = () => window.FINDALINE_RESULT && window.FINDALINE_RESULT.model;
const later = (then, ms = 300) => setTimeout(then, ms);
const wait = (ok, then, tries = 150) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 200)
                                                  : (result.timeout = step, done());
const run = (steps) => {
  const names = Object.keys(steps);
  const go = (k) => { if (k >= names.length) { step = "end"; return show(); } step = names[k]; show(); steps[step](() => go(k + 1)); };
  go(0);
};
const credits = () => {
  const control = $(".leaflet-control-attribution");
  return {text: control.textContent, links: $$(".leaflet-control-attribution a").map((a) => a.getAttribute("href")),
          imgs: control.querySelectorAll("img").length};
};
"""

# A map served by the app (with its worker: profiles and measuring)
MAP_SCRIPT = MAP_HELPERS + """
const saved = () => fetch("/api/runs/" + encodeURIComponent(model().runId) + "/favourites").then((r) => r.json());
const until = (count, then, tries = 30) => saved().then((body) => body.lines.length === count || !tries
  ? then(body) : later(() => until(count, then, tries - 1)));
run({
  linked(next) { wait(() => model() && $(".r-note"), () => {
    // the map was opened with #line= of a line hidden by the filters: the note clears them and opens it
    result.back = $$(".b-pill .b-back").map((b) => b.getAttribute("aria-label"));
    result.credits = credits();
    result.note = text(".r-note");
    button("clear filters").click();
    wait(() => rows().length, () => {
      result.linked = model().selected ? model().lineLink(model().selected.line) : null;
      result.hashKept = decodeURIComponent(location.hash);
      result.spots = model().shown.spots;
      result.summary = text(".r-summary-count");
      next();
    });
  }); },
  select(next) {
    // the linked line is selected: until the page is drawn again, the details are still its own
    rows()[0].click();
    wait(() => rows()[0].getAttribute("aria-selected") === "true" && $(".b-results .r-spot-sel .r-dtitle")
                && $(".b-dock .fal-profile-title"), () => {
      result.profile = text(".b-dock .fal-profile-title");
      result.hash = decodeURIComponent(location.hash) === "#line=" + model().lineLink(model().selected.line);
      next();
    });
  },
  favourite(next) {
    result.favouritesOn = model().favouritesOn;
    $(".b-results .r-details .r-fav").click();
    wait(() => $(".r-pop .r-folder"), () => { $(".r-pop .r-folder").click(); until(1, (body) => {
      result.favourite = body.lines[0] === model().lineLink(model().selected.line);
      next();
    }); });
  },
  downloads(next) {
    button("Download").click();
    later(() => {
      const links = $$(".r-downloads a").map((a) => a.getAttribute("href"));
      Promise.all(links.map((href) => fetch(href).then((r) => r.status))).then((statuses) => {
        result.downloads = links.map((href, k) => href + " " + statuses[k]);
        button("Download").click();
        next();
      });
    });
  },
  compare(next) {
    button("Compare").click();
    wait(() => $(".r-other input"), () => {
      result.others = $$(".r-other").map((o) => o.textContent);
      $(".r-other input").click();
      wait(() => model().others.size === 1, () => {
        result.otherLines = [...model().others.values()][0].lines.length;
        result.xss = window.XSS || null;
        result.imgs = $$(".r-others img").length;
        button("Compare").click();
        next();
      });
    });
  },
  measure(next) {
    const map = window.FINDALINE_MAP, l = model().lines[0];
    button("Measure").click();
    map.fire("click", {latlng: {lat: l.lat1, lng: l.lon1}});
    map.fire("click", {latlng: {lat: l.lat2, lng: l.lon2}});
    wait(() => text(".b-dock .fal-profile-title").startsWith("Measured"), () => {
      result.measured = text(".b-dock .fal-profile-title");
      result.expectedLength = l.len.toFixed(0);
      next();
    });
  },
});
"""


def _map_page(folder, name: str, script: str, before_data: str = "", after_data: str = "") -> str:
    """A copy of the run's map.html with test scripts (`before_data` first in its head, `after_data` between
    data.js and the viewer, `script` at the end); its file name."""
    page = (folder / "map.html").read_text(encoding="utf-8")
    assert "<style" not in page and " style=" not in page
    for suffix, content in (("before", before_data), ("after", after_data), ("test", script)):
        (folder / f"{name}_{suffix}.js").write_text(content, encoding="utf-8")
    (folder / f"{name}.html").write_text(
        page.replace("<head>", f'<head><script src="{name}_before.js"></script>')
            .replace('<script src="data.js"></script>', f'<script src="data.js"></script><script src="{name}_after.js"></script>')
            .replace("</body>", f'<script src="{name}_test.js"></script></body>'), encoding="utf-8")
    return f"{name}.html"


def _result_in(dom: str) -> dict:
    import html
    import re
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, dom[-3000:]
    return json.loads(html.unescape(found.group(1)))


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_run_map(tmp_path, monkeypatch):
    """A run's map.html served by the app shows the app's result view: the way back to the app, the credits of
    its data as text (no markup, http(s) links only, ATTRIBUTION.txt), #line= of a line hidden by the filters
    named and opened, a row selects its line (profile, hash), a favourite kept by the server, the downloads
    beside it, another run (named like an HTML tag) compared, measuring; no CSP violations."""
    run_id = _clip_run(tmp_path, monkeypatch)
    other = _clip_run(tmp_path, monkeypatch, "--name", XSS_NAME, "--easting", "554066")  # lines of its own
    row = _lines_of(tmp_path, run_id)[3]
    folder = tmp_path / "runs" / run_id
    assert (folder / "ATTRIBUTION.txt").exists()
    filters = 'localStorage.setItem("findaline.filters", \'{"lenMin": 100000}\');'
    page = _map_page(folder, "map_test", MAP_SCRIPT, before_data=CSP_STUB + filters, after_data=CREDITS_STUB)
    line = ",".join(str(v) for v in row[:4])
    dom = _edge_page(tmp_path, f"runs/{run_id}/{page}#line={line}", start_scheduler=True, size="1280,800")
    r = _result_in(dom)
    assert "timeout" not in r and r["at"] == "end", r
    assert r["summary"].startswith(f"{r['spots']} spots") and r["back"] == ["Find-a-Line"], r
    assert "Data: FABDEM V1-2 © University of Bristol (CC BY-NC-SA 4.0)" in r["credits"]["text"], r
    assert "<img src=x onerror=window.XSS=1> (<b>x</b>)" in r["credits"]["text"], r
    links = r["credits"]["links"]
    assert "https://creativecommons.org/licenses/by-nc-sa/4.0/" in links and "ATTRIBUTION.txt" in links, r
    assert not any(link.startswith("javascript") for link in links if link) and r["credits"]["imgs"] == 0, r
    assert r["note"].startswith("The line of the link is hidden by the filters"), r
    assert r["linked"] == line and r["hashKept"] == "#line=" + line, r
    assert r["profile"].startswith("Profile: ") and r["hash"], r
    assert r["favouritesOn"] and r["favourite"], r
    assert sorted(r["downloads"]) == ["lines.csv 200", "lines.geojson 200"], r
    assert len(r["others"]) == 1 and XSS_NAME in r["others"][0], r
    assert r["otherLines"] > 0 and r["xss"] is None and r["imgs"] == 0, r
    assert r["measured"].startswith(f"Measured: {r['expectedLength']} m"), r
    assert _violations(dom) == []
    index = (tmp_path / "runs" / "index.html").read_text(encoding="utf-8")
    assert "<style" not in index and "_viewer/index.css" in index and XSS_NAME not in index
    assert os.path.exists(tmp_path / "runs" / "_viewer" / "index.css")
    assert other in index


# A map opened from disk: no server, so no profiles, measuring or favourites; the other runs from ../runs.js
DISK_SCRIPT = MAP_HELPERS + """
run({
  loaded(next) { wait(() => rows().length && model(), () => {
    result.spots = model().shown.spots;
    result.back = $$(".b-pill .b-back").map((b) => b.getAttribute("aria-label"));
    result.measure = !!button("Measure");
    result.favouritesOn = model().favouritesOn;
    result.canCompute = model().canCompute;
    result.credits = credits().links;
    next();
  }); },
  select(next) {
    rows()[0].click();
    wait(() => $(".b-results .r-dtitle"), () => {
      result.profile = text(".b-dock .fal-profile-title");
      result.hash = decodeURIComponent(location.hash) === "#line=" + model().lineLink(model().selected.line);
      next();
    });
  },
  compare(next) {
    button("Compare").click();
    wait(() => $(".r-other input"), () => {
      $(".r-other input").click();
      wait(() => model().others.size === 1, () => { result.otherLines = [...model().others.values()][0].lines.length; next(); });
    });
  },
});
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_run_map_from_disk(tmp_path, monkeypatch):
    """A run's map.html opened from disk (file://): the way back is the list of runs beside it, no measuring or
    favourites (no server), the saved profile of a line, its hash, and another run compared from its lines.js."""
    import subprocess
    run_id = _clip_run(tmp_path, monkeypatch)
    _clip_run(tmp_path, monkeypatch, "--easting", "554066")
    folder = tmp_path / "runs" / run_id
    page = _map_page(folder, "disk_test", DISK_SCRIPT, before_data=INTROS_SEEN, after_data=CREDITS_STUB)
    dom = subprocess.run(
        [EDGE, "--headless", "--disable-gpu", f"--user-data-dir={tmp_path / 'edge'}", "--no-first-run",
         "--window-size=1280,800", "--allow-file-access-from-files", "--virtual-time-budget=60000", "--dump-dom",
         (folder / page).as_uri()], capture_output=True, text=True, encoding="utf-8", timeout=180).stdout
    r = _result_in(dom)
    assert "timeout" not in r and r["at"] == "end", r
    assert r["spots"] > 0 and r["back"] == ["All runs"], r
    assert not r["measure"] and not r["favouritesOn"] and not r["canCompute"], r
    assert "ATTRIBUTION.txt" in r["credits"], r
    assert r["profile"].startswith("Profile: ") and r["hash"], r
    assert r["otherLines"] > 0, r


FAVOURITES_TAB_SCRIPT = """
(function () {
  const out = document.createElement("pre");
  out.id = "result";
  document.body.appendChild(out);
  const result = {};
  const rows = () => [...document.querySelectorAll(".c-fav[data-line]")];
  const wait = (ok, then, tries = 100) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 100)
                                                    : (out.textContent = "timeout " + JSON.stringify(result));
  wait(() => rows().length, () => {
    result.cards = rows().length;
    const select = document.querySelector(".c-trip-sort select");
    select.value = "len";
    select.dispatchEvent(new Event("change", {bubbles: true}));
    setTimeout(() => {
      result.lengths = rows().map((c) => parseFloat(c.querySelector(".c-fav-open b").textContent));
      result.link = rows()[0].querySelector(".c-fav-open").getAttribute("href");
      rows()[0].querySelector(".c-fav-star").click();
      wait(() => rows().length === result.cards - 1, () => {
        result.after = rows().length;
        out.textContent = JSON.stringify(result);
      });
    }, 300);
  });
})();
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_favourites_page_of_the_app(tmp_path, monkeypatch):
    """The app's Favourites page (D) lists the favourite lines the server keeps, sorted, with a link to the
    line in the app, and removes one."""
    import re
    run_id = _clip_run(tmp_path, monkeypatch)
    app = web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)
    rows = _lines_of(tmp_path, run_id)
    with TestClient(app, base_url=LOCAL) as client:
        for k, row in enumerate((rows[0], rows[-1])):
            data = {"len": 999.0 if k else row[8], "clr": 1.0, "clrN": None, "e1": 1.0, "e2": 2.0, "over": False,
                    "water": 0.0, "filled": 0.0, "hmax": None, "hmean": None, "highPct": None, "angle1": None,
                    "angle2": None, "prom1": None, "prom2": None, "drop1": None, "drop2": None}
            line = ",".join(str(v) for v in row[:4])
            assert client.post(f"/api/runs/{run_id}/favourites", json={"line": line, "favourite": True, "data": data},
                               headers={"x-findaline-token": TOKEN}).status_code == 200
        page = client.get("/").text
    tab = tmp_path / "runs" / "apptest"
    tab.mkdir()
    (tab / "stubs.js").write_text(APP_STUBS, encoding="utf-8")
    (tab / "test.js").write_text(FAVOURITES_TAB_SCRIPT, encoding="utf-8")
    (tab / "index.html").write_text(page.replace(
        '<script src="/app/app.js"></script>',
        '<script src="stubs.js"></script><script src="/app/app.js"></script><script src="test.js"></script>'),
        encoding="utf-8")
    dom = _edge_page(tmp_path, "runs/apptest/index.html?ui=d#favourites")
    result = json.loads(re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1).replace("&quot;", '"'))
    assert result["cards"] == 2, result
    assert result["lengths"][0] == 999 and result["lengths"] == sorted(result["lengths"], reverse=True), result
    assert result["link"].replace("&amp;", "&") == f"#run={run_id}&line=" + line.replace(",", "%2C"), result
    assert result["after"] == 1 and _violations(dom) == []


# Reports which interface the app page rendered (and, in the gallery, that the sheet's handle moves it)
UI_SCRIPT = """
(function () {
  const out = document.createElement("pre");
  out.id = "result";
  document.body.appendChild(out);
  setTimeout(() => {
    const found = ["fa-tabs", "fa-try", "u-gallery", "u-app-b", "u-app-c", "u-app-d", "u-rail", "u-bnav", "u-sheet"]
      .filter((name) => document.querySelector("." + name));
    const sheet = document.querySelector(".u-sheet");
    const kept = localStorage.getItem("findaline-ui");
    let snaps = "";
    if (sheet) {
      const before = sheet.className;
      sheet.querySelector(".u-sheet-handle").click();  // a keyboard-like click moves it one state
      snaps = before + " -> ";
      setTimeout(() => { out.textContent = JSON.stringify({found, kept, snaps: snaps + sheet.className}); }, 300);
    } else out.textContent = JSON.stringify({found, kept, snaps});
  }, 2000);
})();
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_interface_switch(tmp_path):
    """?ui= picks the interface: D without it (and for the old app, which is gone), the gallery of the new components (kit) and the
    prototypes B and C, each without CSP violations; the choice is remembered (localStorage)."""
    import html
    import re
    folder = tmp_path / "runs" / "uitest"
    folder.mkdir(parents=True)
    (folder / "stubs.js").write_text(APP_STUBS, encoding="utf-8")
    (folder / "test.js").write_text(UI_SCRIPT, encoding="utf-8")
    app = web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)
    with TestClient(app, base_url=LOCAL) as client:
        page = client.get("/").text
    (folder / "index.html").write_text(page.replace(
        '<script src="/app/app.js"></script>',
        '<script src="stubs.js"></script><script src="/app/app.js"></script><script src="test.js"></script>'),
        encoding="utf-8")

    def shown(query):
        dom = _edge_page(tmp_path, "runs/uitest/index.html" + query)
        assert _violations(dom) == [], query  # style-src 'self': no inline styles
        return json.loads(html.unescape(re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1)))

    assert "u-app-d" in shown("")["found"]  # the default
    old = shown("?ui=old")["found"]  # the old app is gone: the default instead
    assert "u-app-d" in old and "fa-tabs" not in old, old
    kit = shown("?ui=kit")
    assert {"u-gallery", "u-rail", "u-bnav", "u-sheet"} <= set(kit["found"]), kit
    assert "u-half" in kit["snaps"].split(" -> ")[0] and "u-full" in kit["snaps"].split(" -> ")[1], kit
    assert "u-app-b" in shown("?ui=b")["found"]
    assert "u-app-c" in shown("?ui=c")["found"]
    assert shown("?ui=c")["kept"] == "c"  # every server has its own port: the next page would not see it



def test_old_app_is_gone():
    """The old app (its tabs, form and panels, classes fa-*) is not in the bundle any more: B shows C's
    Admin and OSM pages, and ?ui=old is not a choice (a browser that remembered it gets D)."""
    root = os.path.join(os.path.dirname(__file__), "..", "viewer")
    with open(os.path.join(root, "dist", "app.js"), encoding="utf-8") as f:
        bundle = f.read()
    assert "fa-tabs" not in bundle and "fa-panel-body" not in bundle
    with open(os.path.join(root, "src", "ui", "choice.ts"), encoding="utf-8") as f:
        assert '"old"' not in f.read()

# The results UI's demo page (?ui=kit-result&run=<id>): before the app, the clipboard records what it gets
RESULTS_STUBS = APP_STUBS + """
window.COPIED = [];
Object.defineProperty(navigator, "clipboard", {value: {writeText: (text) => { COPIED.push(text); return Promise.resolve(); }}});
"""

RESULTS_SCRIPT = """
(function () {
  const out = document.createElement("pre");
  out.id = "result";
  const result = {};
  const done = () => { out.textContent = JSON.stringify(result); document.body.appendChild(out); };
  const $ = (s) => document.querySelector(s), $$ = (s) => [...document.querySelectorAll(s)];
  const text = (s) => ($(s) || {}).textContent || "";
  const button = (label) => $$("button").find((b) => b.textContent.trim() === label || b.getAttribute("aria-label") === label);
  const rows = () => $$(".r-rows > .r-spot > .r-row");
  const later = (then, ms = 300) => setTimeout(then, ms);
  const run = new URLSearchParams(location.search).get("run");
  const saved = () => fetch("/api/runs/" + encodeURIComponent(run) + "/favourites").then((r) => r.json());
  const until = (count, then, tries = 30) => saved().then((body) => body.lines.length === count || !tries
    ? then(body) : later(() => until(count, then, tries - 1)));
  let step = "";
  const wait = (ok, then, tries = 150) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 200)
                                                    : (result.timeout = step + ": " + text(".r-tip") + " | "
                                                       + text(".r-dock .fal-profile-title"), done());
  const steps = {
    loaded(next) { wait(() => rows().length && window.FINDALINE_RESULT, () => {
      const model = window.FINDALINE_RESULT.model;
      result.wide = innerWidth >= 768 && !!$(".r-panel") && !$(".u-sheet");
      result.summary = text(".r-summary-count");
      result.spots = model.shown.spots;
      result.rows = rows().length;
      result.more = !!$(".r-showmore");
      result.legend = $$(".r-legend span").map((s) => s.textContent);
      next();
    }); },
    select(next) {
      rows()[0].click();
      wait(() => $(".r-details .r-dtitle") && $(".r-dock .fal-profile-title"), () => {
        const model = window.FINDALINE_RESULT.model, l = model.selected.line;
        result.title = text(".r-details .r-dtitle");
        result.expected = l.len.toFixed(0) + " m · " + l.clr.toFixed(1) + " m clear";
        result.eyebrow = text(".r-details .r-eyebrow");
        result.profile = text(".r-dock .fal-profile-title");
        result.selectedRow = rows()[0].getAttribute("aria-selected");
        next();
      });
    },
    touch(next) {
      const rect = $(".r-dock .fal-hover"), box = rect.getBoundingClientRect();
      rect.dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, pointerType: "touch", pointerId: 7,
        clientX: box.left + box.width / 2, clientY: box.top + box.height / 2}));
      later(() => { result.readout = text(".r-dock .fal-profile-readout"); next(); });
    },
    step(next) {
      button("Next spot").click();
      later(() => {
        result.stepped = text(".r-details .r-eyebrow");
        result.position = window.FINDALINE_RESULT.model.position;
        next();
      });
    },
    keyboard(next) {
      rows()[3].dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true}));
      later(() => { result.keyboard = window.FINDALINE_RESULT.model.position; next(); });
    },
    filter(next) {
      const model = window.FINDALINE_RESULT.model, before = model.shown.spots;
      button("Clearance").click();
      wait(() => $(".r-editor input[type=number]"), () => {
        const input = $(".r-editor input[type=number]");
        input.value = "1";
        input.dispatchEvent(new Event("change", {bubbles: true}));
        later(() => {
          result.filtered = [before, model.shown.spots, text(".r-summary-count"),
                             $$(".u-chip.u-on").map((c) => c.textContent.trim())];
          button("Remove the clearance filter").click();
          later(() => { result.cleared = model.shown.spots; next(); });
        });
      });
    },
    favourite(next) {
      const model = window.FINDALINE_RESULT.model;
      if (!model.selected) rows()[0].click();  // the filter may have hidden the selected line
      result.favouritesOn = model.favouritesOn;
      $(".r-details .r-fav").click();  // its folders: the top level
      later(() => { $(".r-pop .r-folder").click(); until(1, (body) => {
        result.favourites = body.lines.length;
        result.favLine = body.lines[0] === model.lineLink(model.selected.line);
        result.stars = $$(".r-star.r-on").length;
        result.favButton = text(".r-details .r-fav");
        $(".r-details .r-fav").click();
        later(() => { $(".r-pop .r-unfav").click(); until(0, (after) => { result.unmarked = after.lines.length; next(); }); });
      }); });
    },
    copy(next) {
      const model = window.FINDALINE_RESULT.model;
      $(".r-details .r-copy").click();
      later(() => {
        result.copied = COPIED[0] || "";
        result.coords = model.lineLink(model.selected.line);
        result.copyButton = text(".r-details .r-copy");
        next();
      });
    },
    downloads(next) {
      button("Download").click();
      later(() => {
        const links = $$(".r-downloads a").map((a) => a.getAttribute("href"));
        Promise.all(links.map((href) => fetch(href).then((r) => r.status))).then((statuses) => {
          result.downloads = links.map((href, k) => href + " " + statuses[k]);
          next();
        });
      });
    },
    linked(next) {
      const model = window.FINDALINE_RESULT.model, target = model.rows[5].group.members.slice(-1)[0];
      location.hash = "#line=" + encodeURIComponent(model.lineLink(target));
      later(() => { result.linked = !!model.selected && model.selected.line === target; next(); }, 500);
    },
    measure(next) {
      const map = window.FINDALINE_MAP, l = window.FINDALINE_RESULT.model.lines[0];
      button("Measure").click();
      map.fire("click", {latlng: {lat: l.lat1, lng: l.lon1}});
      map.fire("click", {latlng: {lat: l.lat2, lng: l.lon2}});
      wait(() => text(".r-dock .fal-profile-title").startsWith("Measured"), () => {
        result.measured = text(".r-dock .fal-profile-title");
        result.expectedLength = l.len.toFixed(0);
        next();
      });
    },
  };
  const names = Object.keys(steps);
  const go = (k) => { if (k >= names.length) return done(); step = names[k]; steps[step](() => go(k + 1)); };
  go(0);
})();
"""

PHONE_SCRIPT = """
(function () {
  const out = document.createElement("pre");
  out.id = "result";
  const wait = (ok, then, tries = 150) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 200) : then();
  wait(() => document.querySelector(".r-details .r-dtitle"), () => {
    out.textContent = JSON.stringify({width: innerWidth, sheet: !!document.querySelector(".u-sheet"),
      panel: !!document.querySelector(".r-panel"),
      head: (document.querySelector(".u-sheet-head .r-eyebrow") || {}).textContent || "",
      profile: !!document.querySelector(".u-sheet .r-dock .fal-profile-title"),
      wider: document.documentElement.scrollWidth > innerWidth});
    document.body.appendChild(out);
  });
})();
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_results_ui(tmp_path, monkeypatch):
    """The results UI of the new interfaces (result/view) on its demo page, ?ui=kit-result&run=<id>: the summary
    and the list, a row selects its line (details, profile; a touch on the profile reads a point), the next
    spot, Enter on a row, a filter chip changes the counts and is removed, a favourite kept by the server,
    the copied link holds the line, the downloads answer, #line= opens a line, measuring shows its profile;
    below 768 px a sheet instead of the panel; no CSP violations."""
    import html
    import re
    import urllib.parse
    run_id = _clip_run(tmp_path, monkeypatch)
    folder = tmp_path / "runs" / "kittest"
    folder.mkdir(parents=True)
    (folder / "stubs.js").write_text(RESULTS_STUBS, encoding="utf-8")
    app = web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)
    with TestClient(app, base_url=LOCAL) as client:
        page = client.get("/").text

    def page_with(script: str, name: str):
        (folder / f"{name}.js").write_text(script, encoding="utf-8")
        (folder / f"{name}.html").write_text(page.replace(
            '<script src="/app/app.js"></script>',
            f'<script src="stubs.js"></script><script src="/app/app.js"></script><script src="{name}.js"></script>'),
            encoding="utf-8")

    def result_of(dom):
        found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
        assert found, dom[-3000:]
        return json.loads(html.unescape(found.group(1)))

    query = "?ui=kit-result&run=" + urllib.parse.quote(run_id)
    page_with(RESULTS_SCRIPT, "desk")
    dom = _edge_page(tmp_path, f"runs/kittest/desk.html{query}", start_scheduler=True, size="1280,800")
    r = result_of(dom)
    assert "timeout" not in r, json.dumps(r)
    assert r["wide"] and r["rows"] == min(100, r["spots"]) and r["more"] == (r["spots"] > 100), r
    assert r["summary"].startswith(f"{r['spots']} spots"), r
    assert r["legend"][0] == "Clear", r
    assert r["title"] == r["expected"] and r["eyebrow"].startswith(f"Spot 1 of {r['spots']}"), r
    assert r["profile"].startswith("Profile: ") and r["selectedRow"] == "true", r
    assert " m from A1" in r["readout"], r
    assert r["stepped"].startswith(f"Spot 2 of {r['spots']}") and r["position"] == 1 and r["keyboard"] == 3, r
    before, after, summary, chips = r["filtered"]
    assert after < before and summary.startswith(f"{after} spots") and "Clearance ≥ 1.0 m" in chips, r
    assert r["cleared"] == before, r
    assert r["favouritesOn"] and r["favourites"] == 1 and r["favLine"] and r["stars"] == 1, r
    assert r["favButton"] == "Favourite" and r["unmarked"] == 0, r
    assert r["copied"].endswith("&line=" + urllib.parse.quote(r["coords"], safe="")), r
    assert r["copyButton"] == "Link copied", r
    assert sorted(r["downloads"]) == sorted(f"/runs/{urllib.parse.quote(run_id)}/{f} 200"
                                            for f in ("lines.csv", "lines.geojson")), r
    assert r["linked"], r
    assert r["measured"].startswith(f"Measured: {r['expectedLength']} m"), r
    assert _violations(dom) == []

    # A phone: Edge's headless window is at least about 500 px wide, below the 768 px of a desktop
    page_with(PHONE_SCRIPT, "phone")
    row = _lines_of(tmp_path, run_id)[0]
    link = "#line=" + urllib.parse.quote(",".join(str(v) for v in row[:4]))
    dom = _edge_page(tmp_path, f"runs/kittest/phone.html{query}{link}", size="500,900", profile="edge-phone")
    r = result_of(dom)
    assert r["width"] < 768 and r["sheet"] and not r["panel"], r
    assert r["head"].startswith("Spot ") and r["profile"] and not r["wider"], r
    assert _violations(dom) == []


def test_schema_has_labels_topics_and_presets(client):
    """The form's metadata: each field's label, unit and topic, the topics' titles and the presets
    (their values validate as a request of the web)."""
    schema = client.get("/api/schema").json()
    assert all(f["label"] and f["topic"] in schema["topics"] for f in schema["fields"])
    assert schema["topics"]["lines"] == "Lines" and "groups" not in schema  # only the old form had them
    presets = {p["id"]: p for p in schema["presets"]}
    assert [p["label"] for p in schema["presets"]] == ["Typical highline", "Long lines", "Short & low", "Custom"]
    for preset in presets.values():
        if preset["values"]:
            web.check_request({"params": {"lat": 50.1, "lon": 19.7, **preset["values"]}})



# The search panel's demo page (?ui=kit-search): no event stream; the estimates are answered here and
# recorded (their bodies), RATE_LIMITED of them answered with 429 first, SLOW_FIRST delays the first one
SEARCH_STUBS = CSP_STUB + """
window.EventSource = class { constructor() {} close() {} };
window.ESTIMATES = [];
window.RATE_LIMITED = 0;
window.SLOW_FIRST = 0;
const realFetch = window.fetch;
window.fetch = (url, init) => {
  if (!String(url).startsWith("/api/estimate")) return realFetch(url, init);
  const n = ESTIMATES.push(JSON.parse(init.body));
  const json = (body, status) => new Response(JSON.stringify(body), {status, headers: {"content-type": "application/json"}});
  if (RATE_LIMITED > 0) {
    RATE_LIMITED--;
    return Promise.resolve(json({detail: "too many estimates: try again in a minute"}, 429));
  }
  const answer = json({source: "test-" + n, resolution: 1, km2: null, tiles: 1, download_mb: 3, work_gb: 0,
                       reasons: [], messages: [], area: null}, 200);
  return new Promise((resolve) => setTimeout(() => resolve(answer), n === 1 && SLOW_FIRST ? SLOW_FIRST : 50));
};
"""

# Helpers of the search panel's tests (before each script)
SEARCH_HELPERS = """
const out = document.createElement("pre");
out.id = "result";
document.body.appendChild(out);
const found = {};
const say = (key, value) => { found[key] = value; out.textContent = JSON.stringify(found); };
const $ = (selector) => document.querySelector(selector);
const text = (selector) => ($(selector) || {}).textContent || "";
const button = (label) => [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === label);
const wait = (ok, then, tries = 150) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 100)
                                                  : say("timeout", text(".s-est-line") + " | " + text(".s-panel").slice(0, 200));
const status = () => text(".s-est-line") || text(".s-bar-line");  // B's box or C's bar
const fieldValue = (label) => [...document.querySelectorAll(".u-field-lab")].find((l) => l.textContent.startsWith(label))
  ?.querySelector("b")?.textContent;
const setRange = (input, value) => { input.value = String(value); input.dispatchEvent(new Event("input", {bubbles: true})); };
"""

SEARCH_SCRIPT = SEARCH_HELPERS + """
wait(() => window.FINDALINE_PLACE && $(".s-panel"), () => {
  const map = window.FINDALINE_PLACE.map;
  say("initial", {estimate: text(".s-est-line"), preset: text(".s-preset.u-on")});
  map.fire("click", {latlng: {lat: 50.1559508, lng: 19.7562722}});
  setTimeout(() => setRange($(".s-where input[type=range]"), 150), 100);
  wait(() => text(".s-est-line").startsWith("Ready"), () => {
    say("point", {centre: text(".s-point"), radius: fieldValue("Search radius"), calls: ESTIMATES.length,
                  stats: text(".s-est-row"), body: ESTIMATES[ESTIMATES.length - 1]});
    button("Typical highline").click();
    setTimeout(() => {
      say("preset", {length: fieldValue("Line length"), diff: fieldValue("Anchor height difference"),
                     on: text(".s-preset.u-on")});
      wait(() => ESTIMATES.length === 2 && text(".s-est-line").startsWith("Ready"), () => {
        say("again", {source: text(".s-est-row"), body: ESTIMATES[1].params});
        button("Find lines").click();
        wait(() => $(".s-started") && $(".s-job"), () => {
          say("started", {id: $(".s-started").dataset.started, job: $(".s-job").dataset.job, card: text(".s-job"),
                          meters: [...document.querySelectorAll(".s-meter")].map((m) => m.dataset.usage),
                          usage: text(".s-meters")});
        });
      });
    }, 100);
  });
});
"""


def _search_page(tmp_path, script: str, stubs: str = SEARCH_STUBS, folder_name: str = "searchtest") -> str:
    """A copy of the app's page in runs/<folder_name>/ with the stubs before and the script after the app."""
    folder = tmp_path / "runs" / folder_name
    folder.mkdir(parents=True)
    (folder / "stubs.js").write_text(stubs, encoding="utf-8")
    (folder / "test.js").write_text(script, encoding="utf-8")
    app = web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)
    with TestClient(app, base_url=LOCAL) as client:
        page = client.get("/").text
    (folder / "index.html").write_text(page.replace(
        '<script src="/app/app.js"></script>',
        '<script src="stubs.js"></script><script src="/app/app.js"></script><script src="test.js"></script>'),
        encoding="utf-8")
    return f"runs/{folder_name}/index.html"


def _result_of(dom: str) -> dict:
    import html
    import re
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, dom[-3000:]
    return json.loads(html.unescape(found.group(1)))


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_search_panel(tmp_path):
    """The search panel (?ui=kit-search): a click on the map sets the centre, the estimate comes by
    itself once the changes stop (one request for the click and the radius), a preset sets the length
    fields (and asks again), Find lines queues the job that useJobs lists; the usage meters show; no
    CSP violations."""
    page = _search_page(tmp_path, SEARCH_SCRIPT)
    dom = _edge_page(tmp_path, page + "?ui=kit-search&theme=light")
    r = _result_of(dom)
    assert "timeout" not in r, r
    assert r["initial"]["estimate"].startswith("Select the centre of the search") and r["initial"]["preset"] == "Custom", r
    point = r["point"]
    assert "50.15595, 19.75627" in point["centre"] and point["radius"] == "150 m", point
    assert point["calls"] == 1, point  # debounced: the click and the radius are one estimate
    assert point["body"] == {"params": {"lat": 50.1559508, "lon": 19.7562722, "radius": 150}, "geometry": None}
    assert "0.07 km²" in point["stats"] and "test-1" in point["stats"], point  # π × 150² for a circle
    assert r["preset"] == {"length": "30 – 150 m", "diff": "≤ 10 m", "on": "Typical highline"}, r
    assert r["again"]["body"] == {"lat": 50.1559508, "lon": 19.7562722, "radius": 150, "max_length": 150,
                                  "max_anchor_diff_m": 10}, r  # min_length 30 is the default: not sent
    started = r["started"]
    assert started["id"] == started["job"] and "waiting" in started["card"], started
    assert started["meters"] == ["runs_gb", "jobs_day", "jobs_active"], started  # locally: no km² counted
    assert "No daily limits" in started["usage"] and "Searches today · 1" in started["usage"], started
    queued = Jobs(str(tmp_path)).get(started["id"])
    assert json.loads(queued["request"])["params"] == r["again"]["body"]
    assert _violations(dom) == []


RATE_SCRIPT = SEARCH_HELPERS + """
window.SLOW_FIRST = 1500;
wait(() => window.FINDALINE_PLACE && $(".s-panel"), () => {
  const map = window.FINDALINE_PLACE.map;
  map.fire("click", {latlng: {lat: 50.1559508, lng: 19.7562722}});
  // A change while the first estimate is in flight: its (slow) answer is dropped
  wait(() => ESTIMATES.length === 1, () => {
    setRange($(".s-where input[type=range]"), 200);
    wait(() => ESTIMATES.length === 2 && text(".s-est-line").startsWith("Ready"), () => {
      setTimeout(() => {
        say("dropped", {stats: text(".s-est-row"), calls: ESTIMATES.length});
        // Too many estimates: paused, asked again once after RETRY_MS, never more
        window.RATE_LIMITED = 5;
        setRange($(".s-where input[type=range]"), 250);
        wait(() => text(".s-est-line").startsWith("Estimate paused"), () => {
          say("paused", {line: text(".s-est-line"), calls: ESTIMATES.length, start: !button("Find lines").disabled});
          setTimeout(() => say("later", {line: text(".s-est-line"), calls: ESTIMATES.length,
                                         again: !!button("Try again")}), 26000);
        });
      }, 2000);
    });
  });
});
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_search_panel_estimate_rate(tmp_path):
    """The live estimate drops the answer to a request made before the last change; refused by the rate
    (429) it says so, keeps Start enabled, asks again once after RETRY_MS and then waits for the user."""
    page = _search_page(tmp_path, RATE_SCRIPT)
    r = _result_of(_edge_page(tmp_path, page + "?ui=kit-search"))
    assert "timeout" not in r, r
    assert "test-2" in r["dropped"]["stats"] and r["dropped"]["calls"] == 2, r  # the first answer came later
    assert r["paused"]["calls"] == 3 and r["paused"]["start"], r
    assert "Trying again" in r["paused"]["line"], r
    assert r["later"]["calls"] == 4 and r["later"]["line"].startswith("Estimate paused") and r["later"]["again"], r


AREA_SCRIPT = SEARCH_HELPERS + """
const box = (w, s, e, n) => ({type: "Feature", properties: {},
  geometry: {type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}});
const shapes = {type: "FeatureCollection", features: [box(19.750, 50.154, 19.756, 50.157),
  box(19.753, 50.155, 19.759, 50.158), box(19.770, 50.150, 19.772, 50.152)]};  // two overlap
const load = (name, then) => {
  const input = $("input[type=file]");
  const files = new DataTransfer();
  files.items.add(new File([FILES[name]], name));
  input.files = files.files;
  input.dispatchEvent(new Event("change", {bubbles: true}));
  wait(() => text(".s-where").includes(name + ":"), then);
};
const last = () => ESTIMATES[ESTIMATES.length - 1] || {params: {}};
wait(() => window.FINDALINE_PLACE && $(".s-panel"), () => {
  const place = window.FINDALINE_PLACE;
  button("Area").click();
  setTimeout(() => {
    say("draw", {tools: !!$(".leaflet-pm-toolbar"), buffer: fieldValue("Band width")});
    place.loadDrawn(shapes);
    wait(() => text(".s-areas") && status().startsWith("Ready"), () => {
      say("area", {areas: text(".s-areas"), features: last().geometry.features.length, buffer: last().params.buffer});
      button("Named OSM area").click();
      setTimeout(() => {
        const input = $(".s-where input[type=text]");
        input.value = "Gozo";
        input.dispatchEvent(new Event("input", {bubbles: true}));
        wait(() => last().params.area === "osm:Gozo", () => {
          say("osm", last());
          button("Along cliffs").click();
          setTimeout(() => {
            say("cliffs", {line: status(), tools: !!$(".leaflet-pm-toolbar")});
            button("Use the map view").click();
            wait(() => last().params.area === "cliffs", () => {
              say("within", last().params);
              button("File").click();
              setTimeout(() => load("crag.kml", () => {
                say("file", {help: text(".s-where"), areas: text(".s-areas")});
                wait(() => last().geometry && status().startsWith("Ready"), () => {
                  button("Start search").click();
                  wait(() => $(".s-started"), () => say("started", $(".s-started").dataset.started));
                });
              }), 200);
            });
          }, 200);
        });
      }, 200);
    });
  }, 200);
});
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_search_panel_modes(tmp_path):
    """C's variant (the estimate in the bottom bar): Area (drawn with Geoman's tools, counted as separate
    areas, the buffer shown; or a named OSM area), Along cliffs (the limit from the map view) and File (a
    KML read in the browser, started as a job)."""
    page = _search_page(tmp_path, "const FILES = " + json.dumps(AREA_FILES) + ";\n" + AREA_SCRIPT)
    dom = _edge_page(tmp_path, page + "?ui=kit-search&variant=c", size="1280,900")
    r = _result_of(dom)
    assert "timeout" not in r, r
    assert r["draw"] == {"tools": True, "buffer": "100 m"}, r
    assert r["area"]["areas"].startswith("2 separate areas (3 shapes") and r["area"]["features"] == 3, r
    assert r["area"]["buffer"] == 100, r
    assert r["osm"] == {"params": {"buffer": 100, "area": "osm:Gozo"}, "geometry": None}, r
    assert r["cliffs"]["line"] == messages.text("error.within_missing") and not r["cliffs"]["tools"], r
    within = r["within"]
    assert within["area"] == "cliffs" and len(within["within"].split(",")) == 4, within  # S,W,N,E of the view
    assert "crag.kml: 2 shapes" in r["file"]["help"], r
    queued = Jobs(str(tmp_path)).get(r["started"])
    geometry = json.loads(queued["request"])["geometry"]
    assert [f["geometry"]["type"] for f in geometry["features"]] == ["Polygon", "Point"]
    assert _violations(dom) == []


PHONE_SEARCH_SCRIPT = SEARCH_HELPERS + """
wait(() => window.FINDALINE_PLACE && $(".s-panel"), () => {
  window.FINDALINE_PLACE.map.fire("click", {latlng: {lat: 50.1559508, lng: 19.7562722}});
  wait(() => text(".s-est-line").startsWith("Ready"), () => {
    const start = button("Find lines").getBoundingClientRect();
    say("phone", {width: innerWidth, scroll: document.documentElement.scrollWidth, sheet: !!$(".u-sheet"),
                  side: !!$(".u-side"), jobs: !!$(".s-sheet-jobs"),
                  modes: [...document.querySelectorAll(".s-where .u-seg button")].map((b) => b.textContent),
                  start: start.bottom <= innerHeight && start.top >= 0});
  });
});
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_search_panel_on_a_phone(tmp_path):
    """Below 768 px the panel is a bottom sheet with short mode names, the page does not scroll sideways
    and Find lines stays in view (sticky at the bottom of the sheet)."""
    page = _search_page(tmp_path, PHONE_SEARCH_SCRIPT)
    dom = _edge_page(tmp_path, page + "?ui=kit-search", size="500,900")
    r = _result_of(dom)["phone"]
    assert r["width"] < 768 and r["scroll"] <= r["width"], r
    assert r["sheet"] and not r["side"] and r["jobs"], r
    assert r["modes"][:2] == ["Point", "Area"] and r["modes"][2:4] == ["Cliffs", "File"] and r["start"], r
    assert _violations(dom) == []
