"""The public mode of the web app (web.py --public, limits.PublicSettings): settings, guards
and headers; without accounts, everything but the page and its assets answers 401."""

import os
import sys
import warnings

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import web  # noqa: E402
from limits import PublicSettings  # noqa: E402

pytest.importorskip("httpx2")
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "test-token"
URL = "https://lines.example.org"
SETTINGS = {"FINDALINE_PUBLIC_URL": URL, "FINDALINE_TRUSTED_PROXY": "172.30.0.2",
            "FINDALINE_CONTACT": "admin@example.org"}


@pytest.fixture
def settings(monkeypatch):
    for key, value in SETTINGS.items():
        monkeypatch.setenv(key, value)
    return PublicSettings.load()


@pytest.fixture
def public(tmp_path, settings):
    app = web.create_app(str(tmp_path), token=TOKEN, public=settings)  # start_scheduler is ignored
    with TestClient(app, base_url=URL) as c:
        yield c


def test_settings(monkeypatch, tmp_path):
    """--public refuses to start without its settings or with wrong ones; Docker secrets are read
    when their folder exists, and its absence gives no warning."""
    for key in SETTINGS:
        monkeypatch.delenv(key, raising=False)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(SystemExit) as stop:
            PublicSettings.load(str(tmp_path / "nothing"))
    assert all(key in str(stop.value) for key in SETTINGS), stop.value
    for name, value in SETTINGS.items():
        (tmp_path / name.lower()).write_text(value)
    loaded = PublicSettings.load(str(tmp_path))
    assert (loaded.public_url, loaded.host, loaded.contact) == (URL, "lines.example.org", "admin@example.org")
    for key, value in SETTINGS.items():
        monkeypatch.setenv(key, value)
    good = PublicSettings.load()
    assert PublicSettings(public_url="http://localhost:8799/").origin == "http://localhost:8799"  # tests only
    for wrong in ({"public_url": "http://lines.example.org"}, {"public_url": "https://lines.example.org/app"},
                  {"public_url": "lines.example.org"}, {"trusted_proxy": "*"}, {"trusted_proxy": " "},
                  {"contact": "a\nb"}):
        with pytest.raises(ValueError):
            PublicSettings(**{**good.model_dump(), **wrong})


def test_command_line(monkeypatch):
    """A non-loopback --host needs --public; --public needs its settings."""
    for key in SETTINGS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit) as stop:
        web.main(["--host", "0.0.0.0", "--no-browser"])
    assert stop.value.code == 2
    with pytest.raises(SystemExit) as stop:
        web.main(["--public", "--host", "0.0.0.0"])
    assert "FINDALINE_PUBLIC_URL" in str(stop.value)
    assert web.is_loopback("127.0.0.1") and web.is_loopback("::1") and not web.is_loopback("0.0.0.0")


def test_public_guards(public, tmp_path):
    """Only the page and its assets answer without signing in; only the public host; changes only from
    the site's Origin; the security headers on every answer; no worker in the web process."""
    page = public.get("/")
    assert page.status_code == 200 and TOKEN in page.text
    assert public.get("/app/app.js").status_code == 200
    for path in ("/api/schema", "/api/jobs", "/api/events", "/api/runs", "/runs/index.html", "/cache/x/wm256/1.jpg",
                 "/api/profile?run=x&line=1,2,3,4", "/docs", "/nothing"):
        assert public.get(path).status_code == 401, path
    headers = {"x-findaline-token": TOKEN, "origin": URL}
    assert public.post("/api/jobs", json={"params": {"lat": 50, "lon": 19}}, headers=headers).status_code == 401
    assert public.delete("/api/jobs/x", headers=headers).status_code == 401
    assert public.get("/", headers={"host": "127.0.0.1:8765"}).status_code == 400
    assert public.get("/", headers={"host": "evil.example"}).status_code == 400
    assert public.post("/", headers={"x-findaline-token": TOKEN}).status_code == 403  # no Origin
    assert public.post("/", headers={**headers, "origin": "https://evil.example"}).status_code == 403
    assert public.post("/", headers=headers).status_code == 405  # through the guard
    for response in (page, public.get("/api/jobs")):
        for name, value in web.SECURITY_HEADERS.items():
            assert response.headers[name] == value, name
        assert "server" not in response.headers or "uvicorn" not in response.headers["server"]
    assert "base-uri 'none'" in page.headers["content-security-policy"]
    assert not public.app.state.jobs.worker_alive()  # the worker runs as `findaline.py worker --public`


def test_local_headers(tmp_path):
    """The local app sends the same security headers and still takes requests without an Origin."""
    with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)) as client:
        response = client.get("/api/schema")
        assert response.status_code == 200
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert response.headers["cross-origin-opener-policy"] == "same-origin"
        assert client.get("/", headers={"host": "localhost:8765"}).status_code == 200


def test_request_limits(monkeypatch, tmp_path):
    """The public site's limits on request values (each overridable, `none` for no limit) refuse
    values over them and fields it does not allow, and give the form its min/max; locally, and for
    limits=None (a user without limits, later), nothing applies."""
    from limits import PUBLIC, Limits
    assert Limits.load() == Limits() and Limits().max_length_max is None and Limits().not_allowed == []
    limits = Limits.load(public=True)
    assert limits.max_length_max == PUBLIC["max_length_max"] == 2000 and "threads" in limits.not_allowed
    monkeypatch.setenv("FINDALINE_LIMIT_MAX_LENGTH_MAX", "3000")
    monkeypatch.setenv("FINDALINE_LIMIT_BUFFER_MAX", "none")
    monkeypatch.setenv("FINDALINE_LIMIT_NOT_ALLOWED", '["update"]')
    changed = Limits.load(public=True)
    assert (changed.max_length_max, changed.buffer_max, changed.not_allowed) == (3000, None, ["update"])
    assert changed.slope_threshold_min == 30
    for key in ("FINDALINE_LIMIT_MAX_LENGTH_MAX", "FINDALINE_LIMIT_BUFFER_MAX", "FINDALINE_LIMIT_NOT_ALLOWED"):
        monkeypatch.delenv(key)

    point = {"lat": 50.1, "lon": 19.7}
    refused = [{**point, "max_length": 2500}, {"area": "coast", "within": "osm:Gozo", "buffer": 600},
               {**point, "prominence_span": 1500}, {**point, "drop_radius": 1001}, {**point, "slope_threshold": 20},
               {**point, "ortho_detail": 51}, {**point, "ortho": "always"}, {**point, "update": "stale"},
               {**point, "coastline": "world"}, {**point, "threads": 2}, {**point, "tile_size": 500},
               {**point, "resolution": 5, "group_distance": 9}, {**point, "group_distance": 1}]
    for params in refused:
        with pytest.raises(ValueError):
            web.check_request({"params": params}, limits)
        web.check_request({"params": params}, None)  # no limits
    for params in ({**point, "max_length": 2000, "slope_threshold": 30, "ortho": "never"},
                   {**point, "resolution": 5, "group_distance": 10}, {**point, "max_length": "1500"}):
        web.check_request({"params": params}, limits)
    with pytest.raises(ValueError) as error:
        web.check_request({"params": {**point, "max_length": 2500, "threads": 2}}, limits)
    assert "max_length: at most 2000" in str(error.value) and "threads: not allowed" in str(error.value)

    fields = {f["name"]: f for f in web.schema(limits)}
    assert not {"threads", "tile_size", "update"} & set(fields)
    assert "always" not in fields["ortho"]["choices"] and "world" not in fields["coastline"]["choices"]
    assert (fields["max_length"]["max"], fields["slope_threshold"]["min"], fields["group_distance"]["min"]) == (2000, 30, 2)
    assert "at most 2000" in fields["max_length"]["help"]
    local = {f["name"]: f for f in web.schema()}
    assert local["max_length"]["max"] is None and "threads" in local

    with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False, limits=limits)) as client:
        headers = {"x-findaline-token": TOKEN}
        response = client.post("/api/jobs", json={"params": {**point, "max_length": 2500}}, headers=headers)
        assert response.status_code == 400 and "at most 2000" in response.text
        assert client.post("/api/estimate", json={"params": {**point, "threads": 2}}, headers=headers).status_code == 400
        names = {f["name"] for f in client.get("/api/schema").json()["fields"]}
        assert "threads" not in names
    with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)) as client:
        assert "threads" in {f["name"] for f in client.get("/api/schema").json()["fields"]}  # locally all
