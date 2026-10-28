"""The public mode of the web app (web.py --public, settings.PublicSettings): settings, guards
and headers; signing in with GitHub and Google (accounts.py) against fake providers (an
httpx2.MockTransport given to Authlib's clients), sessions, and who may do what."""

import base64
import hashlib
import json
import os
import sys
import time
import warnings
from urllib.parse import parse_qs, urlsplit

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import accounts as ac  # noqa: E402
import web  # noqa: E402
from settings import AccountSettings, PublicSettings  # noqa: E402

pytest.importorskip("httpx2")
import httpx2  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "test-token"
URL = "https://lines.example.org"
SETTINGS = {"FINDALINE_PUBLIC_URL": URL, "FINDALINE_TRUSTED_PROXY": "172.30.0.2",
            "FINDALINE_CONTACT": "admin@example.org"}
ADMIN = ("github", "1")  # FINDALINE_ADMINS=github:1
ACCOUNTS = {"FINDALINE_SESSION_SECRET": "s3cret-" * 6, "FINDALINE_GITHUB_CLIENT_ID": "gh-client",
            "FINDALINE_GITHUB_CLIENT_SECRET": "gh-secret", "FINDALINE_GOOGLE_CLIENT_ID": "google-client",
            "FINDALINE_GOOGLE_CLIENT_SECRET": "google-secret", "FINDALINE_ADMINS": "github:1"}


class FakeProviders:
    """GitHub and Google as Authlib's clients see them (an httpx2.MockTransport): codes are given out by
    `authorize` (what the user's browser would get from the provider) and taken once, with the PKCE
    verifier of their challenge; Google answers with an ID token signed by its own key."""

    ISSUER = "https://accounts.google.com"

    def __init__(self):
        from joserfc.jwk import RSAKey
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
        self.codes, self.tokens, self.calls = {}, {}, []
        self.transport = httpx2.MockTransport(self.handle)
        self.id_token_changes = {}  # claims to change in the next ID token (tests of its checks)

    def authorize(self, location: str, user: dict) -> str:
        """The callback address the provider sends the browser to, after `user` agreed."""
        url = urlsplit(location)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
        provider = "google" if url.hostname == "accounts.google.com" else "github"
        assert query["redirect_uri"] == f"{URL}/auth/{provider}"
        if provider == "google":
            assert query["scope"] == "openid profile" and query["nonce"]
        else:
            assert "scope" not in query  # GitHub: public data only
        code = os.urandom(8).hex()
        self.codes[code] = {"challenge": query["code_challenge"], "user": user, "nonce": query.get("nonce"),
                            "provider": provider}
        return f"/auth/{provider}?code={code}&state={query['state']}"

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        from joserfc import jwt
        url = str(request.url)
        self.calls.append(url)
        if url == "https://accounts.google.com/.well-known/openid-configuration":
            return httpx2.Response(200, json={
                "issuer": self.ISSUER, "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_endpoint": "https://oauth2.googleapis.com/token",
                "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                "id_token_signing_alg_values_supported": ["RS256"]})
        if url == "https://www.googleapis.com/oauth2/v3/certs":
            return httpx2.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if url in ("https://github.com/login/oauth/access_token", "https://oauth2.googleapis.com/token"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            grant = self.codes.pop(form.get("code"), None)  # a code is taken once
            if grant is None:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            verifier = form.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if challenge != grant["challenge"]:
                return httpx2.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
            access = os.urandom(8).hex()
            self.tokens[access] = grant["user"]
            answer = {"access_token": access, "token_type": "bearer"}
            if grant["provider"] == "google":
                now = int(time.time())
                claims = {"iss": self.ISSUER, "aud": "google-client", "sub": grant["user"]["sub"],
                          "name": grant["user"].get("name"), "nonce": grant["nonce"], "iat": now, "exp": now + 600,
                          "at_hash": None}
                claims.pop("at_hash")
                claims.update(self.id_token_changes)
                answer["id_token"] = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, self.key)
            return httpx2.Response(200, json=answer)
        if url == "https://api.github.com/user":
            user = self.tokens.get(request.headers.get("authorization", "").removeprefix("Bearer "))
            return httpx2.Response(200, json=user) if user else httpx2.Response(401, json={})
        return httpx2.Response(404, json={})


@pytest.fixture
def settings(monkeypatch):
    for key, value in SETTINGS.items():
        monkeypatch.setenv(key, value)
    return PublicSettings.load()


@pytest.fixture
def account_settings(monkeypatch):
    for key, value in ACCOUNTS.items():
        monkeypatch.setenv(key, value)
    return AccountSettings.load()


@pytest.fixture
def providers():
    return FakeProviders()


def _public_app(data_dir, settings, account_settings, providers, **kwargs):
    return web.create_app(str(data_dir), token=TOKEN, public=settings, accounts=account_settings,
                          oauth_transport=providers.transport, **kwargs)  # start_scheduler is ignored


@pytest.fixture
def public(tmp_path, settings, account_settings, providers):
    with TestClient(_public_app(tmp_path, settings, account_settings, providers), base_url=URL) as c:
        yield c


def sign_in(client, providers, user: dict, provider: str = "github", next_path: str = "/"):
    """Sign a TestClient in through the fake provider; the last answer (the redirect to `next`)."""
    start = client.get(f"/login/{provider}", params={"next": next_path}, follow_redirects=False)
    assert start.status_code == 302, start.text
    return client.get(providers.authorize(start.headers["location"], user), follow_redirects=False)


def csrf_of(client) -> str:
    """The CSRF token of a signed-in client's session, from its page."""
    import re
    page = client.get("/").text
    return re.search(r'name="findaline-token" content="([^"]+)"', page).group(1)


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


def test_public_guards(public, providers):
    """Without signing in only the sign-in page and its assets answer; only the public host; changes
    only from the site's Origin and with the session's token; the security headers on every answer;
    no worker in the web process."""
    page = public.get("/")
    assert page.status_code == 200 and "Sign in with GitHub" in page.text and "Sign in with Google" in page.text
    assert TOKEN not in page.text and "<script" not in page.text
    assert public.get("/app/app.js").status_code == 200
    for path in ("/api/schema", "/api/jobs", "/api/events", "/api/runs", "/runs/index.html", "/cache/x/wm256/1.jpg",
                 "/api/profile?run=x&line=1,2,3,4"):
        assert public.get(path).status_code == 401, path
    assert public.get("/docs").status_code == public.get("/nothing").status_code == 404
    headers = {"x-findaline-token": TOKEN, "origin": URL}
    assert public.post("/api/jobs", json={"params": {"lat": 50, "lon": 19}}, headers=headers).status_code == 401
    assert public.delete("/api/jobs/x", headers=headers).status_code == 401
    assert public.get("/", headers={"host": "127.0.0.1:8765"}).status_code == 400
    assert public.get("/", headers={"host": "evil.example"}).status_code == 400
    assert public.post("/", headers={"x-findaline-token": TOKEN}).status_code == 403  # no Origin
    assert public.post("/", headers={**headers, "origin": "https://evil.example"}).status_code == 403
    assert public.post("/", headers=headers).status_code == 401  # no session: the app's token is nothing
    sign_in(public, providers, {"id": 1, "login": "admin"})
    own = {"x-findaline-token": csrf_of(public), "origin": URL}
    assert public.post("/", headers=own).status_code == 405  # through the guard
    assert public.post("/", headers={**own, "x-findaline-token": TOKEN}).status_code == 403
    for response in (page, public.get("/api/jobs")):
        for name, value in web.SECURITY_HEADERS.items():
            assert response.headers[name] == value, name
        assert "server" not in response.headers or "uvicorn" not in response.headers["server"]
    assert "base-uri 'none'" in page.headers["content-security-policy"]
    assert not public.app.state.jobs.worker_alive()  # the worker runs as `findaline.py worker --public`


def test_account_settings(monkeypatch, tmp_path):
    """--public needs the session secret and a provider; a provider without its id and secret is not
    offered; Docker secrets are read whatever the case of their file names; administrators are
    <provider>:<id>."""
    for key in ACCOUNTS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit) as stop:
        AccountSettings.load(str(tmp_path / "nothing"))
    assert "FINDALINE_SESSION_SECRET" in str(stop.value)
    monkeypatch.setenv("FINDALINE_SESSION_SECRET", "x" * 40)
    with pytest.raises(SystemExit) as stop:
        AccountSettings.load(str(tmp_path / "nothing"))
    assert "provider" in str(stop.value)
    monkeypatch.setenv("FINDALINE_GOOGLE_CLIENT_ID", "google-client")  # no secret: not offered
    with pytest.raises(SystemExit):
        AccountSettings.load(str(tmp_path / "nothing"))
    monkeypatch.delenv("FINDALINE_SESSION_SECRET")
    (tmp_path / "FINDALINE_SESSION_SECRET").write_text("y" * 40 + "\n")
    (tmp_path / "findaline_github_client_secret").write_text("gh-secret\n")
    monkeypatch.setenv("FINDALINE_GITHUB_CLIENT_ID", "gh-client")
    loaded = AccountSettings.load(str(tmp_path))
    assert loaded.session_secret.get_secret_value() == "y" * 40 and loaded.providers == ["github"]
    assert loaded.client("github") == ("gh-client", "gh-secret") and loaded.signup == "approval"
    assert "y" * 40 not in repr(loaded)
    # compose.yaml mounts every secret file: an empty one (or an empty client id) is a provider not offered
    (tmp_path / "findaline_google_client_secret").write_text("")
    monkeypatch.setenv("FINDALINE_GOOGLE_CLIENT_ID", "google-client")
    assert AccountSettings.load(str(tmp_path)).providers == ["github"]
    (tmp_path / "findaline_google_client_secret").write_text("google-secret\n")
    monkeypatch.setenv("FINDALINE_GOOGLE_CLIENT_ID", " ")
    assert AccountSettings.load(str(tmp_path)).providers == ["github"]
    (tmp_path / "findaline_github_client_secret").write_text("\n")
    with pytest.raises(SystemExit):  # no provider left
        AccountSettings.load(str(tmp_path))
    (tmp_path / "FINDALINE_SESSION_SECRET").write_text("")
    with pytest.raises(SystemExit):  # an empty session secret is refused
        AccountSettings.load(str(tmp_path))
    for wrong in ({"session_secret": "short"}, {"admins": "12345"}, {"admins": "gitlab:1"}, {"signup": "maybe"}):
        with pytest.raises(ValueError):
            AccountSettings(**{"session_secret": "z" * 40, **wrong})
    assert AccountSettings(session_secret="z" * 40, admins=" github:1, google:abc ").admin_ids == \
        [("github", "1"), ("google", "abc")]


def test_sign_in_with_github(public, providers, tmp_path):
    """A new GitHub account waits for approval (its page says so, the API answers 403); an approved
    one gets the app with its session's token; the identity is the numeric id, kept with its name;
    every sign-in makes a new session; administrators are approved at once."""
    answer = sign_in(public, providers, {"id": 42, "login": "alice", "email": "a@example.org"}, next_path="/?x=1")
    assert answer.status_code == 303 and answer.headers["location"] == "/?x=1"
    cookie = answer.headers["set-cookie"]
    assert cookie.startswith(ac.SESSION_COOKIE + "=") and all(f in cookie.lower() for f in (
        "secure", "httponly", "samesite=lax", "path=/", "max-age=2592000")) and "domain" not in cookie.lower()
    page = public.get("/")
    assert "waiting for an administrator" in page.text and "alice" in page.text and "findaline-token" not in page.text
    assert public.get("/api/jobs").status_code == 403
    import sqlite3
    db = sqlite3.connect(tmp_path / "accounts.sqlite")
    rows = db.execute("SELECT provider, subject, name, status FROM users").fetchall()
    assert rows == [("github", "42", "alice", "pending")]
    assert "a@example.org" not in json.dumps(db.execute("SELECT * FROM users").fetchall())
    stored = db.execute("SELECT hash FROM sessions").fetchall()
    token = public.cookies.get(ac.SESSION_COOKIE)
    assert stored == [(hashlib.sha256(token.encode()).hexdigest(),)]  # only the hash
    db.execute("UPDATE users SET status = 'approved'")
    db.commit()
    page = public.get("/")
    assert 'name="findaline-token"' in page.text and "&quot;name&quot;: &quot;alice&quot;" in page.text
    assert csrf_of(public) != TOKEN and public.get("/api/jobs").status_code == 200
    sign_in(public, providers, {"id": 42, "login": "alice2"})
    assert public.cookies.get(ac.SESSION_COOKIE) != token  # a new session; the old one ended
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert db.execute("SELECT name FROM users").fetchone()[0] == "alice2"
    with TestClient(public.app, base_url=URL) as admin:
        sign_in(admin, providers, {"id": 1, "login": "root"})
        assert admin.get("/api/jobs").status_code == 200 and "(administrator)" not in admin.get("/").text
        assert "&quot;admin&quot;: true" in admin.get("/").text
    db.close()


def test_sign_in_with_google(public, providers, tmp_path):
    """Google: OpenID Connect; Authlib checks the ID token (nonce, audience, issuer, expiry, signature)."""
    answer = sign_in(public, providers, {"sub": "g-123", "name": "Gina"}, provider="google")
    assert answer.status_code == 303, answer.text
    assert "Gina" in public.get("/").text
    for change in ({"nonce": "other"}, {"aud": "someone-else"}, {"iss": "https://evil.example"},
                   {"exp": int(time.time()) - 3600}):
        providers.id_token_changes = change
        with TestClient(public.app, base_url=URL) as other:
            answer = sign_in(other, providers, {"sub": "g-456", "name": "Mallory"}, provider="google")
            assert answer.status_code == 400 and ac.SESSION_COOKIE not in other.cookies, change
    import sqlite3
    db = sqlite3.connect(tmp_path / "accounts.sqlite")
    assert db.execute("SELECT provider, subject FROM users").fetchall() == [("google", "g-123")]
    db.close()


def test_login_csrf_and_replayed_state(public, providers):
    """A callback is accepted only by the browser that started the sign-in (its signed cookie holds
    the state): another browser's state, a replayed state, a state without a cookie or a foreign
    cookie all fail; so does a code without its PKCE verifier."""
    start = public.get("/login/github", follow_redirects=False)
    callback = providers.authorize(start.headers["location"], {"id": 7, "login": "victim"})
    with TestClient(public.app, base_url=URL) as attacker:  # login CSRF: the victim's callback elsewhere
        attacker.get("/login/github", follow_redirects=False)  # a cookie of its own sign-in
        assert attacker.get(callback, follow_redirects=False).status_code == 400
    with TestClient(public.app, base_url=URL) as nobody:
        assert nobody.get(callback, follow_redirects=False).status_code == 400
    oauth_cookie = public.cookies.get(ac.OAUTH_COOKIE)
    assert public.get(callback, follow_redirects=False).status_code == 303
    assert public.get(callback, follow_redirects=False).status_code == 400  # replayed
    public.cookies.set(ac.OAUTH_COOKIE, oauth_cookie, domain="lines.example.org")  # the old cookie again
    assert public.get(callback, follow_redirects=False).status_code == 400  # the code is taken once
    public.cookies.set(ac.OAUTH_COOKIE, oauth_cookie[:-3] + "AAA", domain="lines.example.org")  # forged
    assert public.get(callback, follow_redirects=False).status_code == 400
    start = public.get("/login/github", follow_redirects=False)
    callback = providers.authorize(start.headers["location"], {"id": 7, "login": "victim"})
    code = parse_qs(urlsplit(callback).query)["code"][0]
    providers.codes[code]["challenge"] = "x" * 43  # the verifier no longer fits
    assert public.get(callback, follow_redirects=False).status_code == 400
    assert public.get("/login/gitlab", follow_redirects=False).status_code == 404


def test_next_is_a_local_path(public, providers):
    """`next` of a sign-in is a path of this site only (no open redirect)."""
    for wrong in ("//evil.example/x", "https://evil.example", "/\\evil.example", "javascript:alert(1)",
                  "evil.example", "/\tx", ""):
        with TestClient(public.app, base_url=URL) as client:
            answer = sign_in(client, providers, {"id": 9, "login": "u"}, next_path=wrong)
            assert answer.headers["location"] == "/", wrong
    answer = sign_in(public, providers, {"id": 9, "login": "u"}, next_path="/runs/x/map.html?a=1")
    assert answer.headers["location"] == "/runs/x/map.html?a=1"
    location = public.get("/login/github?next=//evil.example", follow_redirects=False).headers["location"]
    assert parse_qs(urlsplit(location).query)["redirect_uri"] == [f"{URL}/auth/github"]  # not from Host


def test_sessions_end(public, providers, tmp_path):
    """A session ends when it expires, after a week without requests, when it is revoked, when its
    user is blocked, and when signing out (a POST with its token, from the header or the form)."""
    import sqlite3
    db = sqlite3.connect(tmp_path / "accounts.sqlite")
    sign_in(public, providers, {"id": 1, "login": "admin"})
    assert public.get("/api/jobs").status_code == 200
    db.execute("UPDATE sessions SET created = created - ?", (31 * 86400,))
    db.commit()
    assert public.get("/api/jobs").status_code == 401 and "Sign in with" in public.get("/").text
    sign_in(public, providers, {"id": 1, "login": "admin"})
    db.execute("UPDATE sessions SET seen = seen - ?", (8 * 86400,))
    db.commit()
    assert public.get("/api/jobs").status_code == 401
    sign_in(public, providers, {"id": 1, "login": "admin"})
    db.execute("UPDATE sessions SET seen = seen - ?", (6 * 86400,))  # idle less than a week: still on
    db.commit()
    assert public.get("/api/jobs").status_code == 200
    users = ac.Accounts(str(tmp_path), public.app.state.account_settings)
    users.revoke(users.ensure_user("github", "1"))
    assert public.get("/api/jobs").status_code == 401

    with TestClient(public.app, base_url=URL) as bob:
        sign_in(bob, providers, {"id": 5, "login": "bob"})
        bob_id = users.ensure_user("github", "5")
        users.set_status(bob_id, "approved")
        assert bob.get("/api/jobs").status_code == 200
        users.set_status(bob_id, "blocked")  # ends its sessions
        assert bob.get("/api/jobs").status_code == 401
        sign_in(bob, providers, {"id": 5, "login": "bob"})
        assert bob.get("/api/jobs").status_code == 403 and "is blocked" in bob.get("/").text
        assert users.user(bob_id).status == "blocked"

    sign_in(public, providers, {"id": 1, "login": "admin"})
    csrf = csrf_of(public)
    assert public.post("/logout", headers={"origin": URL}).status_code == 403          # no token
    assert public.post("/logout", data={"csrf": "wrong"}, headers={"origin": URL}).status_code == 403
    assert public.post("/logout", data={"csrf": csrf}).status_code == 403              # no Origin
    answer = public.post("/logout", data={"csrf": csrf}, headers={"origin": URL}, follow_redirects=False)
    assert answer.status_code == 303 and public.get("/api/jobs").status_code == 401
    assert db.execute("SELECT COUNT(*) FROM sessions s JOIN users u ON u.id = s.user_id "
                      "WHERE u.subject = '1'").fetchone()[0] == 0
    sign_in(public, providers, {"id": 1, "login": "admin"})
    answer = public.post("/logout", headers={"origin": URL, "x-findaline-token": csrf_of(public)},
                         follow_redirects=False)
    assert answer.status_code == 303 and public.get("/api/jobs").status_code == 401
    db.close()


def test_signup_modes_and_login_rate(tmp_path, settings, account_settings, providers):
    """FINDALINE_SIGNUP=open approves new accounts, closed refuses them (administrators still sign in);
    sign-in requests are limited per IP address."""
    import sqlite3
    (tmp_path / "open").mkdir()
    (tmp_path / "closed").mkdir()
    open_ = account_settings.model_copy(update={"signup": "open"})
    with TestClient(_public_app(tmp_path / "open", settings, open_, providers), base_url=URL) as client:
        sign_in(client, providers, {"id": 3, "login": "carol"})
        assert client.get("/api/jobs").status_code == 200
    closed = account_settings.model_copy(update={"signup": "closed"})
    with TestClient(_public_app(tmp_path / "closed", settings, closed, providers), base_url=URL) as client:
        answer = sign_in(client, providers, {"id": 3, "login": "carol"})
        assert answer.status_code == 403 and "not accepted" in answer.text
        assert sign_in(client, providers, {"id": 1, "login": "admin"}).status_code == 303
        db = sqlite3.connect(tmp_path / "closed" / "accounts.sqlite")
        assert db.execute("SELECT subject FROM users").fetchall() == [("1",)]
        db.close()
        codes = [client.get("/login/github", follow_redirects=False).status_code for _ in range(25)]
        assert codes.count(429) >= 3 and codes[0] == 302


# The people of the authorization matrix: (provider id, status); "admin" is FINDALINE_ADMINS
PEOPLE = {"a": ("11", "approved"), "b": ("12", "approved"), "pending": ("13", "pending"),
          "blocked": ("14", "blocked"), "admin": ("1", "approved")}
DENIED = {"anonymous": 401, "pending": 403, "blocked": 403}  # whatever the route, before its own checks


def session_of(data_dir, account_settings, who: str):
    """(user id, session cookie, CSRF token) of one of PEOPLE, signed in without the providers."""
    users = ac.Accounts(str(data_dir), account_settings)
    subject, status = PEOPLE[who]
    user = users.sign_in("github", subject, who)
    if user.status != status and not user.admin:
        users.set_status(user.id, status)
    cookie, csrf = users.new_session(user.id)
    return user.id, cookie, csrf


@pytest.fixture
def people(public, tmp_path, account_settings):
    """A TestClient per person of the matrix (and "anonymous"), each with its own cookies and token."""
    clients, ids = {}, {}
    for who in list(PEOPLE) + ["anonymous"]:
        client = TestClient(public.app, base_url=URL)
        client.headers["origin"] = URL
        if who != "anonymous":
            ids[who], cookie, csrf = session_of(tmp_path, account_settings, who)
            client.cookies.set(ac.SESSION_COOKIE, cookie, domain="lines.example.org")
            client.headers["x-findaline-token"] = csrf
        clients[who] = client
    clients["ids"] = ids
    yield clients
    for who, client in clients.items():
        if who != "ids":
            client.close()


def test_every_route_has_its_access_rule(public):
    """Every route outside the explicit public list depends on current_user, require_admin or
    run_access (a FastAPI dependency, not a check inside the endpoint); the run files have theirs."""
    from fastapi.routing import APIRoute
    from starlette.routing import Mount

    def names(dependant) -> set:
        found = set()
        for dependency in dependant.dependencies:
            found.add(getattr(dependency.call, "__name__", ""))
            found |= names(dependency)
        return found
    rules = {"current_user", "require_admin", "run_access"}
    seen = []
    for route in public.app.routes:
        seen.append(route.path)
        if route.path in web.PUBLIC_ROUTES:
            continue
        if isinstance(route, APIRoute):
            assert names(route.dependant) & rules, f"{route.path} has no access rule"
        elif isinstance(route, Mount):
            assert route.path in GUARDED_MOUNTS, f"{route.path}: a mount without access control"
        else:
            raise AssertionError(f"{route.path}: an unknown kind of route")
    assert "/api/jobs" in seen and "/api/events" in seen and "/app" in seen


GUARDED_MOUNTS = {"/runs"}  # web.guard: approved users only (until the run files have their own rule)


def test_authorization_of_jobs(public, people, tmp_path):
    """Who may do what with jobs: anonymous 401, pending and blocked 403 everywhere; a user sees and
    changes only their own jobs (other jobs are 404); an administrator sees and cancels every job,
    but edits and queues again only their own; OSM extracts for administrators only."""
    from jobs import Jobs
    jobs = Jobs(str(tmp_path))
    request = {"params": {"lat": 50.1, "lon": 19.7, "radius": 100}, "geometry": None}
    a, b = people["a"], people["b"]
    job_a = a.post("/api/jobs", json=request).json()["id"]
    job_b = b.post("/api/jobs", json=request).json()["id"]
    old = jobs.add(request, "from before the accounts")  # user_id NULL
    assert jobs.get(job_a)["user_id"] == people["ids"]["a"] and jobs.get(old)["user_id"] is None
    for job in (job_a, job_b, old):
        jobs.set(job, "failed", error="x")
    routes = [("get", "/api/schema"), ("get", "/api/jobs"), ("get", "/api/runs"), ("post", "/api/jobs"),
              ("post", "/api/estimate"), ("get", f"/api/jobs/{job_a}"), ("get", f"/api/jobs/{job_a}/events"),
              ("post", f"/api/jobs/{job_a}/again"), ("delete", f"/api/jobs/{job_a}"), ("get", "/api/events"),
              ("get", "/api/osm?lat=50&lon=19"), ("post", "/api/osm"), ("get", "/cache/x/wm256/1/2/3.jpg"),
              ("get", f"/api/profile?run=x&line=1,2,3,4"), ("get", "/runs/x/map.html")]
    for who, code in DENIED.items():
        for method, path in routes:
            kwargs = {"json": request} if method == "post" else {}
            answer = getattr(people[who], method)(path, **kwargs)
            assert answer.status_code == code, (who, method, path, answer.status_code)

    ids = lambda client: {j["id"] for j in client.get("/api/jobs").json()["jobs"]}  # noqa: E731
    assert ids(a) == {job_a} and ids(b) == {job_b} and ids(people["admin"]) == {job_a, job_b, old}
    for path in (f"/api/jobs/{job_b}", f"/api/jobs/{job_b}/events", f"/api/jobs/nothing"):
        assert a.get(path).status_code == 404, path
    assert a.post(f"/api/jobs/{job_b}/again").status_code == 404
    assert a.delete(f"/api/jobs/{job_b}").status_code == 404
    assert a.get(f"/api/jobs/{job_a}").json()["request"] == request
    copy = a.post(f"/api/jobs/{job_a}/again").json()["id"]
    assert jobs.get(copy)["user_id"] == people["ids"]["a"]
    assert a.delete(f"/api/jobs/{copy}").status_code == 200
    admin = people["admin"]
    assert admin.get(f"/api/jobs/{job_b}").status_code == 404           # edit: own jobs only
    assert admin.post(f"/api/jobs/{job_b}/again").status_code == 404
    assert admin.get(f"/api/jobs/{job_b}/events").status_code == 200    # but sees every job
    assert admin.get(f"/api/jobs/{old}").status_code == 200             # and those from before the accounts
    again = b.post(f"/api/jobs/{job_b}/again").json()["id"]
    assert admin.delete(f"/api/jobs/{again}").status_code == 200        # and cancels every job
    assert jobs.get(again)["status"] == "cancelled"
    events = a.get(f"/api/jobs/{job_a}/events").json()["events"]
    assert events and all(e["job"] == job_a for e in events)
    assert a.get("/api/jobs").json()["seq"] < admin.get("/api/jobs").json()["seq"]
    assert a.post("/api/estimate", json=request).status_code == 503     # signed in: to the (missing) worker

    (tmp_path / "cache" / "osm").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache" / "osm" / "geofabrik-index.json").write_text(json.dumps({"features": []}))
    assert a.post("/api/osm", json={"action": "download", "region": "malta"}).status_code == 403
    assert admin.post("/api/osm", json={"action": "download", "region": "malta"}).status_code == 400  # unknown
    assert a.get("/api/profile?run=x&line=1,2,3,4", headers={"x-findaline-profile": "1"}).status_code == 404


def _public_server(data_dir, settings, account_settings, providers):
    """The public app on a real server (uvicorn in a thread); yields its port."""
    import contextlib
    import socket
    import threading

    import uvicorn

    @contextlib.contextmanager
    def serve():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        app = _public_app(data_dir, settings, account_settings, providers)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            while not server.started:
                time.sleep(0.1)
            yield port
        finally:
            server.should_exit = True
            thread.join(10)
    return serve()


def test_event_stream_per_user(tmp_path, settings, account_settings, providers, monkeypatch):
    """The event stream sends a user the events of their own jobs only (an administrator's: all), to
    signed-in, approved users only, and at most SSE_PER_USER streams per user."""
    import http.client

    from jobs import Jobs
    monkeypatch.setattr(web, "PING", 1)
    jobs = Jobs(str(tmp_path))
    sessions = {who: session_of(tmp_path, account_settings, who) for who in ("a", "b", "pending", "admin")}
    mine = jobs.add({"params": {}}, "a's", sessions["a"][0])
    other = jobs.add({"params": {}}, "b's", sessions["b"][0])

    def open_stream(port, who):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        headers = {"Host": "lines.example.org"}
        if who:
            headers["Cookie"] = f"{ac.SESSION_COOKIE}={sessions[who][1]}"
        connection.request("GET", "/api/events", headers=headers)
        return connection, connection.getresponse()

    def jobs_seen(response):
        lines = []
        while not any(line.startswith(":") for line in lines):
            lines.append(response.fp.readline().decode("utf-8").rstrip("\r\n"))
        return {json.loads(line[5:]).get("job") for line in lines if line.startswith("data:")} - {None}

    with _public_server(tmp_path, settings, account_settings, providers) as port:
        for who, code in ((None, 401), ("pending", 403)):
            connection, response = open_stream(port, who)
            assert response.status == code
            connection.close()
        opened = [open_stream(port, "a") for _ in range(web.SSE_PER_USER)]
        assert jobs_seen(opened[0][1]) == {mine}
        connection, response = open_stream(port, "a")
        assert response.status == 429  # one stream too many
        connection.close()
        b_connection, b_response = open_stream(port, "b")  # another user's own count
        assert b_response.status == 200 and jobs_seen(b_response) == {other}
        admin, admin_response = open_stream(port, "admin")
        assert jobs_seen(admin_response) == {mine, other}
        for connection, _ in opened:
            connection.close()
        deadline = time.time() + 15
        while time.time() < deadline:  # the closed streams are noticed at their next ping
            connection, response = open_stream(port, "a")
            if response.status == 200:
                break
            connection.close()
            time.sleep(0.5)
        assert response.status == 200
        for c in (connection, b_connection, admin):
            c.close()


def test_jobs_database_gets_owners(tmp_path):
    """A jobs.sqlite of version 4 gets the owners' column (its jobs: NULL, the local app's, which an
    administrator sees on the public site); a tiled search resumes only for the same owner, and
    without one (locally) its key is that of before."""
    import sqlite3

    import cache
    import jobs as jb
    import search
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    cache.migrate(db, jb.MIGRATIONS[:4])
    db.execute("INSERT INTO jobs (id, created, status, request, title) VALUES ('old', '2026-10-01T10:00:00', "
               "'done', '{}', 'Old job')")
    db.commit()
    db.close()
    jobs = jb.Jobs(str(tmp_path))
    assert jobs.get("old")["user_id"] is None and [j["id"] for j in jobs.all()] == ["old"]
    assert jobs.all(7) == []
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(jb.MIGRATIONS) >= 5
    db.close()

    from settings import Limits
    plan = _plan(tmp_path, Limits(), northing=254608, easting=553966, radius=150)
    import hashlib as hl
    p = plan.params.model_dump(exclude={"name", "update", "no_html", "ortho", "ortho_detail", "threads",
                                        "opentopo_key", "tile_size", "group_distance", "group_by"})
    before = hl.sha1(json.dumps({"params": p, "source": plan.source.id, "resolution": plan.resolution},
                                sort_keys=True).encode()).hexdigest()[:16]
    assert search._search_hash(plan) == before
    plan.owner = 7
    assert search._search_hash(plan) != before


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
    from settings import PUBLIC, Limits
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


DATA = os.path.join(REPO, "tests", "data")
CLIP = {"elevation_file": os.path.join(DATA, "zabi_kon_nmt.tif"), "water": "off", "fill_gaps": "off",
        "ortho": "never", "nmpt": "off"}
SQUARE = {"type": "Polygon", "coordinates": [[[19.7522, 50.1546], [19.7590, 50.1546], [19.7590, 50.1580],
                                              [19.7522, 50.1580], [19.7522, 50.1546]]]}


def _plan(tmp_path, limits, **values):
    import search
    from params import SearchParams
    paths = search.Paths(str(tmp_path), str(tmp_path / "work"))
    return search.plan_search(SearchParams(**{**CLIP, **values}), paths, lambda kind, text: None, limits=limits)


def test_search_limits_before_requests(tmp_path, monkeypatch):
    """A `within` box or a Nominatim area over the limit is refused before OpenStreetMap is asked
    for its coastline or cliffs (a fake Nominatim counts the calls); the area, the pixels of a circle
    or a tile and the tiles before any data; nothing is checked without limits."""
    import osm
    import search
    from settings import Limits
    from shapely.geometry import box, mapping
    calls = {"search": 0, "theme": 0}

    def fake_search(name, cache_dir):
        calls["search"] += 1
        return {"geojson": mapping(box(14.0, 35.7, 15.0, 36.5))}  # about 8000 km²

    def fake_theme(*args, **kwargs):
        calls["theme"] += 1
        return []
    monkeypatch.setattr(osm, "search", fake_search)
    monkeypatch.setattr(osm, "theme", fake_theme)
    limits = Limits(region_km2_max=2500)
    for values in ({"area": "coast", "within": "osm:Big island"}, {"area": "osm:Big island"},
                   {"geometry": SQUARE, "within": "osm:Big island"}):
        with pytest.raises(search.SearchError) as error:
            _plan(tmp_path, limits, **values)
        assert "at most 2500 km²" in str(error.value), error.value
    assert calls == {"search": 3, "theme": 0}
    with pytest.raises(search.SearchError):
        _plan(tmp_path, limits, area="cliffs", within="35.0,14.0,36.5,15.0")  # a box: no request at all
    assert calls == {"search": 3, "theme": 0}

    point = {"northing": 254608, "easting": 553966, "radius": 150}
    with pytest.raises(search.SearchError) as error:
        _plan(tmp_path, Limits(grid_mpx_max=0.1), **point)
    assert "million pixels" in str(error.value) and "--resolution" in str(error.value)
    assert _plan(tmp_path, Limits(grid_mpx_max=1), **point).grid.width < 1000
    with pytest.raises(search.SearchError) as error:
        _plan(tmp_path, Limits(area_km2_max=0.1), geometry=SQUARE)
    assert "km²" in str(error.value)
    with pytest.raises(search.SearchError) as error:
        _plan(tmp_path, Limits(tiles_max=2), geometry=SQUARE, max_length=150, tile_size=150)
    assert "tiles" in str(error.value)
    with pytest.raises(search.SearchError):  # the overlap alone is over the pixels
        _plan(tmp_path, Limits(grid_mpx_max=0.5), geometry=SQUARE, max_length=1000)
    with pytest.raises(search.SearchError) as error:
        _plan(tmp_path, Limits(group_distance_min_res=2), **point, resolution=5, group_distance=8)
    assert "at least 10 m" in str(error.value)
    plan = _plan(tmp_path, Limits(grid_mpx_max=3), geometry=SQUARE, max_length=150)
    assert plan.grid.width * plan.grid.height <= 3e6
    plan.limits = Limits(download_mb_max=3000)
    with pytest.raises(search.SearchError) as error:
        search.check_estimate(plan, search.Estimate(download_mb=5000, work_gb=1))
    plan.limits = Limits(download_mb_max=6000)
    search.check_estimate(plan, search.Estimate(download_mb=5000, work_gb=1))


def test_search_limits_of_anchors_and_lines(tmp_path):
    """Too many anchors or lines stop the search with a hint, before the profiles."""
    import search
    from settings import Limits
    point = {"northing": 254608, "easting": 553966, "radius": 150}
    for limits, text in ((Limits(anchors_max=10), "anchors, at most 10"), (Limits(lines_max=5), "lines, at most 5")):
        plan = _plan(tmp_path, limits, **point)
        with pytest.raises(search.SearchError) as error:
            search.find_lines(plan, search.prepare_data(plan))
        assert text in str(error.value) and "--resolution" in str(error.value), error.value
        import gc
        gc.collect()


def test_worker_orthophoto_limit(monkeypatch):
    """The worker answers no to orthophoto over Limits.ortho_minutes (the map loads it online), yes
    to everything without the limit."""
    import jobs
    import search
    from settings import Limits
    monkeypatch.setattr(search, "CONFIRM_MINUTES", search.CONFIRM_MINUTES)
    said = []
    assert jobs.ortho_confirm(Limits(), lambda kind, text: said.append(text))("ortho", "?")
    confirm = jobs.ortho_confirm(Limits(ortho_minutes=10), lambda kind, text: said.append(text))
    assert search.CONFIRM_MINUTES == 10 and not confirm("ortho", "?") and confirm("expensive", "?")
    assert "online" in said[0]


def test_redact():
    """Texts of the public site lose the data folder (paths in it become relative), the folder of the
    app and other absolute paths; URLs and ordinary slashes stay."""
    import jobs
    redact = jobs.Redact(r"C:\Data\find" if os.name == "nt" else "/srv/find")
    data = r"C:\Data\find" if os.name == "nt" else "/srv/find"
    assert redact(f"Run saved to {data}{os.sep}runs{os.sep}abc") == f"Run saved to runs{os.sep}abc"
    assert redact(f"in {data}") == "in <data folder>"
    assert redact(f'"{data}"') == '"<data folder>"'
    assert redact(f"old {data}-old{os.sep}x") == "old <path>"  # a sibling folder: not made relative
    assert redact("Terrain file: " + os.path.join(jobs.APP_DIR, "tests", "x.tif") + " (1 m)") == \
        "Terrain file: <path> (1 m)"
    assert redact("from /home/u/x.tif and D:/other/y via https://a.example/b/c?d=1") == \
        "from <path> and <path> via https://a.example/b/c?d=1"
    assert redact("50 km/h, 1/2, S,W,N,E, and/or") == "50 km/h, 1/2, S,W,N,E, and/or"
    assert redact({"a": [f"{data}{os.sep}x", 3]}) == {"a": ["x", 3]}


def test_no_server_paths_in_public_events(tmp_path, monkeypatch):
    """The public worker's events, errors and answers hold no server paths; an unexpected exception
    shows a reference only (its traceback goes to the log)."""
    import time

    from jobs import Jobs, Scheduler
    for key, value in {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
                       "PYTHONIOENCODING": "utf-8"}.items():
        monkeypatch.setenv(key, value)
    jobs = Jobs(str(tmp_path), public=True)
    params = {**CLIP, "northing": 254608, "easting": 553966, "radius": 150, "name": "Clip"}
    job = jobs.add({"params": params, "geometry": None}, "clip")
    missing = str(tmp_path / "missing.tif")  # an expected error naming a file in the data folder: made relative
    failing = jobs.add({"params": {**params, "elevation_file": missing}, "geometry": None}, "fails")
    broken = jobs.add({"geometry": None}, "broken")  # no params: an unexpected KeyError
    scheduler = Scheduler(jobs, public=True)
    scheduler.start()
    try:
        deadline = time.time() + 300
        while any(jobs.get(j)["status"] not in ("done", "failed") for j in (job, failing, broken)) \
                and time.time() < deadline:
            time.sleep(0.5)
        task = jobs.quick_add("estimate", {"params": {**params, "radius": 100}})
        while (answer := jobs.quick_take(task)) is None and time.time() < deadline:
            time.sleep(0.2)
    finally:
        scheduler.stop()
    assert jobs.get(job)["status"] == "done", jobs.get(job)["error"]
    assert jobs.get(broken)["error"] == f"internal error, ref {broken}"
    assert answer["code"] == 200, answer
    assert jobs.get(failing)["status"] == "failed" and "file not found: missing.tif" in jobs.get(failing)["error"]
    ended = [{k: jobs.get(j)[k] for k in ("status", "error", "run_id")} for j in (job, failing, broken)]
    texts = json.dumps([jobs.events(0), ended, answer])
    for folder in (str(tmp_path), REPO):
        for form in (folder, folder.replace("\\", "/"), json.dumps(folder)[1:-1]):
            assert form not in texts, form
    assert "saved to runs" in texts
