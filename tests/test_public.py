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

    def __init__(self, url: str = URL):
        from joserfc.jwk import RSAKey
        self.url = url  # the site's address (FINDALINE_PUBLIC_URL)
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
        assert query["redirect_uri"] == f"{self.url}/auth/{provider}"
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
    assert "FINDALINE_RUNS = [];" in public.get("/runs/runs.js").text  # the demo runs: none
    for path in ("/api/schema", "/api/jobs", "/api/events", "/api/runs", "/api/profile?run=x&line=1,2,3,4"):
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
    assert rows == [("github", "1", None, "approved"),  # the first administrator, made at the start
                    ("github", "42", "alice", "pending")]
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
    assert db.execute("SELECT name FROM users WHERE subject = '42'").fetchone()[0] == "alice2"
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
    assert db.execute("SELECT provider, subject FROM users").fetchall() == [("github", "1"), ("google", "g-123")]
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
          "blocked": ("14", "blocked"), "admin": ("1", "approved"), "unlimited": ("15", "approved")}
DENIED = {"anonymous": 401, "pending": 403, "blocked": 403}  # whatever the route, before its own checks


def session_of(data_dir, account_settings, who: str):
    """(user id, session cookie, CSRF token) of one of PEOPLE, signed in without the providers."""
    users = ac.Accounts(str(data_dir), account_settings)
    subject, status = PEOPLE[who]
    user = users.sign_in("github", subject, who)
    if user.status != status and not user.admin:
        users.set_status(user.id, status)
    if who == "unlimited":
        users.set_unlimited(user.id, True)
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
            assert isinstance(route.app, web.RunFiles), f"{route.path}: a mount without access control"
        else:
            raise AssertionError(f"{route.path}: an unknown kind of route")
    assert "/api/jobs" in seen and "/api/events" in seen and "/app" in seen


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
              ("get", "/api/osm?lat=50&lon=19"), ("post", "/api/osm"),
              ("get", "/api/profile?run=x&line=1,2,3,4"), ("post", "/api/runs/x/demo")]
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


@pytest.fixture(scope="module")
def clip_run(tmp_path_factory):
    """The folder of a run of the Żabi Koń clip (computed once, offline), to copy into data folders."""
    import subprocess
    data = tmp_path_factory.mktemp("clip")
    env = {**os.environ, "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
           "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, "findaline.py", "--elevation-file", os.path.join(DATA, "zabi_kon_nmt.tif"),
         "--northing", "254608", "--easting", "553966", "--radius", "150", "--water", "off", "--fill-gaps", "off",
         "--no-plot", "--yes", "--ortho", "never", "--nmpt", "off", "--data-dir", str(data)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=600, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    runs = data / "runs"
    return next(runs / d for d in os.listdir(runs) if (runs / d / "run.json").is_file())


def copy_run(clip_run, data_dir, run_id: str, name: str):
    """The clip's run as another run of a data folder (its id and name changed)."""
    import shutil
    folder = data_dir / "runs" / run_id
    shutil.copytree(clip_run, folder)
    meta = json.loads((folder / "run.json").read_text("utf-8"))
    meta.update(id=run_id, name=name)
    (folder / "run.json").write_text(json.dumps(meta), "utf-8")
    data = (folder / "data.js").read_text("utf-8").replace(clip_run.name, run_id)
    (folder / "data.js").write_text(data, "utf-8")
    return folder


RUN_A, RUN_B, RUN_OLD = "2026-10-01_100000_A_00000000000000aa", "2026-10-01_100000_B_00000000000000bb", \
    "2026-09-01_100000_Old"


@pytest.fixture
def run_people(tmp_path, clip_run, settings, account_settings, providers):
    """A public app over a data folder with a run of A, one of B and one from before the public mode
    (no row: given to the first administrator when the app starts), with a client per person."""
    from jobs import Jobs
    for run_id, name in ((RUN_A, "A's run"), (RUN_B, "B's run"), (RUN_OLD, "Old run")):
        copy_run(clip_run, tmp_path, run_id, name)
    (tmp_path / "runs" / RUN_A / "progress.json").write_text("{}")
    (tmp_path / "runs" / RUN_A / "tiles").mkdir()
    (tmp_path / "runs" / RUN_A / "tiles" / "0.pkl.gz").write_bytes(b"x")
    (tmp_path / "jobs.sqlite").unlink(missing_ok=True)
    jobs = Jobs(str(tmp_path))
    clients, ids = {}, {}
    for who in list(PEOPLE) + ["anonymous"]:
        if who != "anonymous":
            ids[who], cookie, csrf = session_of(tmp_path, account_settings, who)
    jobs.add_run(RUN_A, ids["a"], 100)
    jobs.add_run(RUN_B, ids["b"], 100)
    app = _public_app(tmp_path, settings, account_settings, providers)  # adopts RUN_OLD
    for who in list(PEOPLE) + ["anonymous"]:
        client = TestClient(app, base_url=URL)
        client.headers["origin"] = URL
        if who != "anonymous":
            _, cookie, csrf = session_of(tmp_path, account_settings, who)
            client.cookies.set(ac.SESSION_COOKIE, cookie, domain="lines.example.org")
            client.headers["x-findaline-token"] = csrf
        clients[who] = client
    clients["ids"], clients["jobs"] = ids, jobs
    yield clients
    for who, client in clients.items():
        if isinstance(client, TestClient):
            client.close()


def test_run_files_behind_access_control(run_people, tmp_path):
    """A run's files (those of RUN_FILES only) for its owner and administrators; 404 for everyone else
    and for every other file; the viewer for anyone; runs.js lists one's own runs; runs from before
    the public mode belong to the first administrator; ETag and Range still work."""
    a, b, admin = run_people["a"], run_people["b"], run_people["admin"]
    jobs, ids = run_people["jobs"], run_people["ids"]
    assert jobs.run(RUN_OLD)["user_id"] == ids["admin"] and jobs.run(RUN_OLD)["visibility"] == "private"
    for name in web.RUN_FILES:
        assert a.get(f"/runs/{RUN_A}/{name}").status_code == 200, name
        assert admin.get(f"/runs/{RUN_A}/{name}").status_code == 200, name
        for who in ("b", "pending", "blocked", "anonymous"):
            assert run_people[who].get(f"/runs/{RUN_A}/{name}").status_code == 404, (who, name)
    for name in ("run.json", "progress.json", "tiles/0.pkl.gz", "", "nothing.js"):
        assert a.get(f"/runs/{RUN_A}/{name}").status_code == 404, name
    assert b.get(f"/runs/{RUN_B}/map.html").status_code == 200 and a.get(f"/runs/{RUN_OLD}/map.html").status_code == 404
    assert admin.get(f"/runs/{RUN_OLD}/map.html").status_code == 200
    assert run_people["anonymous"].get("/runs/_viewer/viewer.js").status_code == 200
    assert a.get("/runs/_viewer/../jobs.sqlite").status_code == 404

    whole = a.get(f"/runs/{RUN_A}/lines.csv")
    part = a.get(f"/runs/{RUN_A}/lines.csv", headers={"range": "bytes=0-9"})
    assert part.status_code == 206 and part.content == whole.content[:10]
    assert a.get(f"/runs/{RUN_A}/lines.csv", headers={"if-none-match": whole.headers["etag"]}).status_code == 304

    listed = a.get("/runs/runs.js")
    assert listed.status_code == 200 and listed.headers["content-type"].startswith("text/javascript")
    runs = json.loads(listed.text.split("window.FINDALINE_RUNS = ")[1].split(";\n")[0])
    assert [r["id"] for r in runs] == [RUN_A] and "window.FINDALINE_APP = {\"profiles\": true};" in listed.text
    assert {r["id"] for r in json.loads(admin.get("/runs/runs.js").text.split(" = ")[1].split(";\n")[0])} == {RUN_OLD}
    assert [r["id"] for r in a.get("/api/runs").json()["runs"]] == [RUN_A]
    assert "FINDALINE_RUNS = [];" in run_people["anonymous"].get("/runs/runs.js").text  # no demo runs yet
    redirect = a.get("/runs/index.html", follow_redirects=False)
    assert redirect.status_code == 303 and redirect.headers["location"] == "/"
    assert a.get(f"/api/profile?run={RUN_B}&line=1,2,3,4", headers={"x-findaline-profile": "1"}).status_code == 404
    assert a.get(f"/api/profile?run={RUN_A}&line=1,2,3,4", headers={"x-findaline-profile": "1"}).status_code == 400  # far


def test_run_file_path_tricks(run_people, tmp_path):
    """Encoded dots and slashes, absolute paths, NUL bytes and symbolic links out of a run folder get
    nothing; the folder's own files outside the list neither."""
    import http.client
    (tmp_path / "secret.txt").write_text("secret")
    a = run_people["a"]
    tricks = [f"/runs/%2e%2e/jobs.sqlite", f"/runs/{RUN_A}%2frun.json", f"/runs/{RUN_A}/..%2f..%2fjobs.sqlite",
              f"/runs/{RUN_A}/%2e%2e/{RUN_B}/map.html", f"/runs/{RUN_A}/%2e%2e%2f{RUN_B}%2fmap.html",
              f"/runs/{RUN_B}%2f..%2f{RUN_A}/map.html", "/runs/_viewer/%2e%2e%2fjobs.sqlite",
              f"/runs/{RUN_A}/map.html%00", f"/runs/{RUN_A}/run.json%00map.html", "/runs/%2fetc%2fpasswd/map.html",
              "/runs/C:%5cWindows/win.ini", f"/runs/{RUN_A}/map.html/", f"/runs/{RUN_A}/map.html%2f"]
    for path in tricks:
        assert a.get(path).status_code == 404, path
    try:
        os.symlink(tmp_path / "secret.txt", tmp_path / "runs" / RUN_A / "lines.geojson.tmp")
        os.replace(tmp_path / "runs" / RUN_A / "lines.geojson.tmp", tmp_path / "runs" / RUN_A / "lines.geojson")
    except OSError:  # Windows without the privilege of symbolic links
        pytest.skip("no symbolic links here")
    answer = a.get(f"/runs/{RUN_A}/lines.geojson")
    assert answer.status_code == 404 and "secret" not in answer.text


def test_share_links(run_people, tmp_path):
    """The owner makes, renews and revokes the share link of a run; the link opens the run's files
    (RUN_FILES only) for anyone, and nothing of other runs; only its sha256 is stored; a blocked
    owner's links stop working; orthophoto tiles stay reachable from a shared map."""
    import hashlib as hl
    from urllib.parse import urljoin
    a, b, anonymous = run_people["a"], run_people["b"], run_people["anonymous"]
    jobs = run_people["jobs"]
    assert a.get("/api/runs").json()["public"] is True
    assert b.post(f"/api/runs/{RUN_A}/share").status_code == 404           # not B's run
    assert run_people["admin"].post(f"/api/runs/{RUN_A}/share").status_code == 404  # the owner only
    assert anonymous.post(f"/api/runs/{RUN_A}/share").status_code == 401
    assert a.post(f"/api/runs/{RUN_A}/share", headers={"origin": "https://evil.example"}).status_code == 403
    url = a.post(f"/api/runs/{RUN_A}/share").json()["url"]
    assert url.startswith(f"{URL}/s/") and url.endswith("/map.html")
    token = url.split("/")[-2]
    assert len(token) >= 43 and jobs.run(RUN_A)["share_hash"] == hl.sha256(token.encode()).hexdigest()
    assert [r["shared"] for r in a.get("/api/runs").json()["runs"]] == [True]
    for name in web.RUN_FILES:
        assert anonymous.get(f"/s/{token}/{name}").status_code == 200, name
    page = anonymous.get(f"/s/{token}/map.html").text
    assert '<script src="../runs.js">' in page and "../_viewer/viewer.js" in page
    for path in (f"/s/{token}/run.json", f"/s/{token}/progress.json", f"/s/{token}/tiles/0.pkl.gz",
                 f"/s/{token}/%2e%2e/{RUN_B}/map.html", f"/s/{token}%2f..%2f{RUN_B}/map.html",
                 f"/s/{RUN_A}/map.html", f"/s/{RUN_B}/lines.js", f"/s/{token[:-1]}/map.html", "/s/x/map.html",
                 f"/s/{token}/../../jobs.sqlite", "/s/%2e%2e/jobs.sqlite"):
        assert anonymous.get(path).status_code == 404, path
    shared_js = anonymous.get("/s/runs.js")
    assert shared_js.status_code == 200 and "FINDALINE_RUNS" not in shared_js.text
    assert 'window.FINDALINE_APP = {"profiles": false};' in shared_js.text
    assert anonymous.get("/s/_viewer/viewer.js").status_code == 200
    redirect = anonymous.get("/s/index.html", follow_redirects=False)
    assert redirect.status_code == 303 and redirect.headers["location"] == "/"
    # The map's orthophoto tiles (../../cache/... from the map) are /cache/..., open to anyone
    assert urljoin(f"{URL}/s/{token}/map.html", "../../cache/poland/wm256/18/1/2.jpg") == \
        f"{URL}/cache/poland/wm256/18/1/2.jpg"
    tile = tmp_path / "cache" / "poland" / "wm256" / "18" / "1" / "2.jpg"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(b"\xff\xd8jpeg")
    assert anonymous.get("/cache/poland/wm256/18/1/2.jpg").status_code == 200
    assert anonymous.get("/cache/poland/index.sqlite").status_code == 404

    renewed = a.post(f"/api/runs/{RUN_A}/share").json()["url"].split("/")[-2]
    assert anonymous.get(f"/s/{token}/map.html").status_code == 404 and renewed != token
    assert anonymous.get(f"/s/{renewed}/map.html").status_code == 200
    assert b.delete(f"/api/runs/{RUN_A}/share").status_code == 404
    assert a.delete(f"/api/runs/{RUN_A}/share").status_code == 200
    assert anonymous.get(f"/s/{renewed}/map.html").status_code == 404 and jobs.run(RUN_A)["share_hash"] is None
    again = a.post(f"/api/runs/{RUN_A}/share").json()["url"].split("/")[-2]
    users = ac.Accounts(str(tmp_path), a.app.state.account_settings)
    users.set_status(run_people["ids"]["a"], "blocked")
    assert anonymous.get(f"/s/{again}/map.html").status_code == 404


def test_demo_runs_and_the_anonymous_page(run_people, tmp_path):
    """An administrator marks a run as a demo (nobody else may); its files (RUN_FILES only) are then
    open to everyone, also without an account; visitors get the demo runs in runs.js (no profiles)
    and on a read-only page with the sign-in links; signed-in users see their own runs and the
    demos, and share only their own."""
    a, admin, anonymous, pending = run_people["a"], run_people["admin"], run_people["anonymous"], run_people["pending"]
    jobs = run_people["jobs"]
    assert a.post(f"/api/runs/{RUN_B}/demo", json={"demo": True}).status_code == 403
    assert anonymous.post(f"/api/runs/{RUN_B}/demo", json={"demo": True}).status_code == 401
    assert admin.post(f"/api/runs/{RUN_B}/demo", json={"demo": "yes"}).status_code == 400
    assert admin.post("/api/runs/nothing/demo", json={"demo": True}).status_code == 404
    assert anonymous.get(f"/runs/{RUN_B}/map.html").status_code == 404
    assert admin.post(f"/api/runs/{RUN_B}/demo", json={"demo": True}).status_code == 200
    assert jobs.run(RUN_B)["visibility"] == "demo" and jobs.demo_runs() == {RUN_B}
    for who in ("anonymous", "pending", "blocked", "a"):
        for name in web.RUN_FILES:
            assert run_people[who].get(f"/runs/{RUN_B}/{name}").status_code == 200, (who, name)
        assert run_people[who].get(f"/runs/{RUN_B}/run.json").status_code == 404, who
        assert run_people[who].get(f"/runs/{RUN_B}/progress.json").status_code == 404, who
    assert anonymous.get(f"/runs/{RUN_A}/map.html").status_code == 404

    def listed(client):
        text = client.get("/runs/runs.js").text
        runs = json.loads(text.split("window.FINDALINE_RUNS = ")[1].split(";\n")[0])
        return {r["id"] for r in runs}, json.loads(text.split("window.FINDALINE_APP = ")[1].split(";\n")[0])
    assert listed(anonymous) == ({RUN_B}, {"profiles": False})
    assert listed(pending) == ({RUN_B}, {"profiles": False})
    assert listed(a) == ({RUN_A, RUN_B}, {"profiles": True})
    runs = {r["id"]: r for r in a.get("/api/runs").json()["runs"]}
    assert (runs[RUN_A]["own"], runs[RUN_A]["demo"], runs[RUN_B]["own"], runs[RUN_B]["demo"]) == (True, False, False, True)
    assert a.post(f"/api/runs/{RUN_B}/share").status_code == 404  # a demo of someone else: no share link
    profile = {"x-findaline-profile": "1"}
    assert a.get(f"/api/profile?run={RUN_B}&line=1,2,3,4", headers=profile).status_code == 400  # past access
    assert anonymous.get(f"/api/profile?run={RUN_B}&line=1,2,3,4", headers=profile).status_code == 401

    page = anonymous.get("/").text
    assert f"/runs/{RUN_B}/map.html" in page and "B&#x27;s run" in page and "/login/github" in page
    assert "<form" not in page and "<script" not in page and RUN_A not in page
    page = pending.get("/").text
    assert "waiting for an administrator" in page and f"/runs/{RUN_B}/map.html" in page
    assert admin.post(f"/api/runs/{RUN_B}/demo", json={"demo": False}).status_code == 200
    assert anonymous.get(f"/runs/{RUN_B}/map.html").status_code == 404 and RUN_B not in anonymous.get("/").text


def _clients(app, data_dir, account_settings, who=("a", "b", "unlimited", "admin")) -> dict:
    """A TestClient per person, signed in (with the CSRF token and the site's Origin)."""
    clients = {}
    for name in who:
        client = TestClient(app, base_url=URL)
        client.headers["origin"] = URL
        user_id, cookie, csrf = session_of(data_dir, account_settings, name)
        client.cookies.set(ac.SESSION_COOKIE, cookie, domain="lines.example.org")
        client.headers["x-findaline-token"] = csrf
        client.user_id = user_id
        clients[name] = client
    return clients


def test_quota_race(tmp_path):
    """Jobs.admission runs in the insert's write transaction: twelve requests at once of a user with
    at most two jobs waiting or running queue exactly two."""
    import threading

    from jobs import Jobs, QuotaExceeded
    from settings import Limits
    jobs = Jobs(str(tmp_path))
    limits = Limits(jobs_active_max=2)
    start, added, refused = threading.Barrier(12), [], []

    def one():
        start.wait()
        try:
            added.append(jobs.add({"params": {}}, "race", 7, check=jobs.admission(7, limits, True)))
        except QuotaExceeded as e:
            refused.append(e.status)
    threads = [threading.Thread(target=one) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(added) == 2 and refused == [429] * 10
    assert len([j for j in jobs.all(7) if j["status"] == "queued"]) == 2
    jobs.add({"params": {}}, "another user", 8, check=jobs.admission(8, limits, True))  # their own count
    jobs.add({"params": {}}, "no limits", 7, check=jobs.admission(7, limits, False))


def test_tiers_and_quotas(tmp_path, settings, account_settings, providers, monkeypatch):
    """A user has the request limits, the quotas and the rates of the site, and no nasadem or
    malta2012; a user without limits has none of them (the resource fields too) but no admin rights
    and no other users' data; an administrator has everything; the server's safeguards (queue, free
    disk) refuse everyone. Refusals come before the worker is asked (it does not run here: 503)."""
    import jobs as jb
    from settings import Limits
    point = {"lat": 50.1, "lon": 19.7, "radius": 100}
    request = {"params": point, "geometry": None}
    limits = Limits.load(public=True).model_copy(update={"jobs_day_max": 4, "estimate_rate": "2/minute"})
    app = _public_app(tmp_path, settings, account_settings, providers, limits=limits)
    people = _clients(app, tmp_path, account_settings)
    a, unlimited, admin = people["a"], people["unlimited"], people["admin"]
    jobs = app.state.jobs

    def sources(client):
        fields = {f["name"]: f for f in client.get("/api/schema").json()["fields"]}
        return "threads" in fields, {"nasadem", "malta2012"} & set(fields["source"]["choices"])
    assert sources(a) == (False, set()) and sources(unlimited) == (True, set())
    assert sources(admin) == (True, {"nasadem", "malta2012"})
    assert a.post("/api/jobs", json={"params": {**point, "threads": 2}}).status_code == 400
    assert a.post("/api/jobs", json={"params": {**point, "max_length": 5000}}).status_code == 400
    assert unlimited.post("/api/jobs", json={"params": {**point, "source": "nasadem"}}).status_code == 400
    assert unlimited.post("/api/jobs", json={"params": {**point, "source": "malta2012"}}).status_code == 400
    big = unlimited.post("/api/jobs", json={"params": {**point, "threads": 2, "max_length": 5000,
                                                       "update": "stale", "coastline": "world"}})
    assert big.status_code == 200, big.text
    assert jobs.get(big.json()["id"])["unlimited"] == 1
    assert admin.post("/api/jobs", json={"params": {**point, "source": "nasadem"}}).status_code == 200
    # No admin rights or other users' data for a user without limits
    assert unlimited.post("/api/osm", json={"action": "download", "region": "malta"}).status_code == 403
    assert unlimited.post("/api/runs/x/demo", json={"demo": True}).status_code == 403
    first = a.post("/api/jobs", json=request).json()["id"]
    assert jobs.get(first)["unlimited"] == 0
    assert unlimited.get(f"/api/jobs/{first}/events").status_code == 404
    assert unlimited.delete(f"/api/jobs/{first}").status_code == 404

    # Two jobs at once, four a day
    assert a.post("/api/jobs", json=request).status_code == 200
    third = a.post("/api/jobs", json=request)
    assert third.status_code == 429 and "at most 2" in third.text
    for _ in range(2):
        assert unlimited.post("/api/jobs", json=request).status_code == 200  # three at once
    for job in jobs.all(a.user_id):
        a.delete(f"/api/jobs/{job['id']}")
    assert a.post("/api/jobs", json=request).status_code == 200
    again = a.post(f"/api/jobs/{first}/again")  # run again counts as well
    assert again.status_code == 200
    for job in jobs.all(a.user_id):
        a.delete(f"/api/jobs/{job['id']}")
    day = a.post("/api/jobs", json=request)
    assert day.status_code == 429 and "4 searches in 24 hours" in day.text
    assert unlimited.delete(f"/api/jobs/{big.json()['id']}").status_code == 200
    assert unlimited.post(f"/api/jobs/{big.json()['id']}/again").status_code == 200

    # The size of the runs and the km² of the last 24 hours (B: a fresh user)
    b = people["b"]
    jobs.add_run("big-run", b.user_id, int(6e9))
    refused = b.post("/api/jobs", json=request)
    assert refused.status_code == 429 and "delete some in Runs" in refused.text
    jobs.delete_run("big-run", str(tmp_path / "runs"))
    jobs.use(b.user_id, "km2", 250, limits.km2_day_max)
    refused = b.post("/api/jobs", json=request)
    assert refused.status_code == 429 and "km²" in refused.text
    with pytest.raises(jb.QuotaExceeded):
        jobs.use(b.user_id, "km2", 1, limits.km2_day_max)
    jobs.add_run("big-run-2", unlimited.user_id, int(6e9))
    jobs.use(unlimited.user_id, "km2", 1000, None)
    assert unlimited.post("/api/jobs", json=request).status_code == 200

    # Rates of estimates: refused before the (missing) worker
    assert [b.post("/api/estimate", json=request).status_code for _ in range(3)] == [503, 503, 429]
    assert {unlimited.post("/api/estimate", json=request).status_code for _ in range(3)} == {503}
    assert b.post("/api/estimate", json={"params": {**point, "threads": 2}}).status_code == 400

    # The server's safeguards: everyone
    small = limits.model_copy(update={"queue_max": 1})
    app2 = _public_app(tmp_path, settings, account_settings, providers, limits=small)
    for client in _clients(app2, tmp_path, account_settings, ("admin", "unlimited")).values():
        full = client.post("/api/jobs", json=request)
        assert full.status_code == 503 and "queue is full" in full.text
    for job in jobs.waiting_or_running():
        jobs.request_cancel(job["id"])

    class Disk:
        free = 5e9
    monkeypatch.setattr(jb.shutil, "disk_usage", lambda path: Disk())
    for client in (admin, unlimited, b):
        short = client.post("/api/jobs", json=request)
        assert short.status_code == 503 and "disk" in short.text
    for client in people.values():
        client.close()


def test_nominatim_and_area_quotas_in_the_worker(tmp_path, monkeypatch):
    """The worker counts a user's uncached Nominatim requests (osm.BEFORE_REQUEST) and refuses the one
    over the quota before it is sent; cached answers are free; the km² of a search are counted when
    its area is known; a user without limits (None) counts nothing."""
    import osm
    from jobs import Jobs, QuotaExceeded, charge_area, nominatim_quota
    from settings import Limits
    jobs = Jobs(str(tmp_path))
    sent = []

    class Answer:
        def raise_for_status(self):
            pass

        def json(self):
            return []
    monkeypatch.setattr(osm, "_nominatim_turn", lambda cache_dir: None)
    monkeypatch.setattr(osm.requests, "get", lambda url, **kwargs: sent.append(kwargs["params"]["q"]) or Answer())
    monkeypatch.setattr(osm, "BEFORE_REQUEST", None)
    cache_dir = str(tmp_path / "nominatim")  # not the data folder's cache (its layout marker)
    nominatim_quota(jobs, 7, Limits(nominatim_day_max=2))
    osm.search("one", cache_dir)
    osm.search("two", cache_dir)
    osm.search("one", cache_dir)  # cached: not counted
    with pytest.raises(QuotaExceeded) as refused:
        osm.search("three", cache_dir)
    assert sent == ["one", "two"] and "Nominatim" in str(refused.value)
    nominatim_quota(jobs, 8, Limits(nominatim_day_max=2))  # another user
    osm.search("three", cache_dir)
    nominatim_quota(jobs, 7, None)  # without limits
    assert osm.BEFORE_REQUEST is None
    osm.search("four", cache_dir)
    assert sent == ["one", "two", "three", "four"]
    monkeypatch.setattr(osm, "BEFORE_REQUEST", None)

    plan = _plan(tmp_path, None, northing=254608, easting=553966, radius=150)  # 0.07 km²
    charge_area(jobs, 7, plan, Limits(km2_day_max=0.1))
    with pytest.raises(QuotaExceeded):
        charge_area(jobs, 7, plan, Limits(km2_day_max=0.1))
    charge_area(jobs, 7, plan, None)
    charge_area(jobs, None, plan, Limits(km2_day_max=0.1))  # the local app: nobody to count


def test_profiles_and_deleting_runs(run_people, tmp_path):
    """Profiles of the public site: points in the run's box with a margin of its --max-length, at most
    --max-length apart, and the rate of a user; the owner deletes a run (folder and row), nobody else."""
    a, b, admin = run_people["a"], run_people["b"], run_people["admin"]
    jobs = run_people["jobs"]
    meta = json.loads((tmp_path / "runs" / RUN_A / "run.json").read_text("utf-8"))
    lat, lon, ml = meta["area"]["lat"], meta["area"]["lon"], meta["params"]["max_length"]
    headers = {"x-findaline-profile": "1"}
    profile = lambda client, *p: client.get(f"/api/profile?run={RUN_A}&line={','.join(map(str, p))}",  # noqa: E731
                                            headers=headers)
    assert profile(a, lat, lon, lat + 0.0003, lon).status_code == 503  # fine: on to the (missing) worker
    far = profile(a, lat, lon, lat + 0.5, lon)
    assert far.status_code == 400 and "area" in far.text
    d = (150 + ml * 0.9) / 111320  # both corners inside the margin, too far apart
    long = profile(a, lat - d, lon - d, lat + d, lon + d)
    assert long.status_code == 400 and "apart" in long.text
    assert profile(admin, lat, lon, lat + 0.5, lon).status_code == 400  # everyone on the public site

    assert b.delete(f"/api/runs/{RUN_A}").status_code == 404
    assert admin.delete(f"/api/runs/{RUN_A}").status_code == 404  # the owner only
    assert a.delete(f"/api/runs/{RUN_A}").status_code == 200
    assert not (tmp_path / "runs" / RUN_A).exists() and jobs.run(RUN_A) is None
    assert a.get(f"/runs/{RUN_A}/map.html").status_code == 404 and a.get("/api/runs").json()["runs"] == []
    with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False)) as local:
        assert local.delete(f"/api/runs/{RUN_B}", headers={"x-findaline-token": TOKEN}).status_code == 404
    assert (tmp_path / "runs" / RUN_B).exists()


def _edge(url: str, profile) -> str:
    """The DOM of a page after headless Edge ran its scripts (its own profile folder)."""
    import subprocess
    return subprocess.run(
        [EDGE, "--headless", "--disable-gpu", f"--user-data-dir={profile}", "--no-first-run",
         "--virtual-time-budget=60000", "--dump-dom", url],
        capture_output=True, text=True, encoding="utf-8", timeout=180).stdout


EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

APP_CHECK = """
(function () {
  const out = document.createElement("pre");
  out.id = "result";
  document.body.appendChild(out);
  setTimeout(() => {
    const user = document.querySelector(".fa-user");
    out.textContent = JSON.stringify({user: user ? user.textContent : null,
      tabs: [...document.querySelectorAll(".fa-tabs button")].map((b) => b.textContent)});
  }, 3000);
})();
"""

SHARE_CHECK = """
(function () {
  const out = document.createElement("pre");
  out.id = "result";
  document.body.appendChild(out);
  setTimeout(() => {
    out.textContent = JSON.stringify({map: !!window.FINDALINE_MAP, runs: window.FINDALINE_RUNS === undefined,
      app: window.FINDALINE_APP, measure: !!document.querySelector(".fal-measure"),
      others: document.querySelectorAll(".fal-other input").length,
      lines: Object.values(window.FINDALINE_MAP._layers).filter((l) => l.getLatLngs).length});
  }, 3000);
})();
"""


@pytest.mark.skipif(not os.path.exists(EDGE), reason="needs Microsoft Edge")
def test_signed_in_app_and_shared_map_in_edge(tmp_path, clip_run, account_settings, monkeypatch):
    """Headless Edge signs in through a fake GitHub (its authorize page is a route of the test app,
    tokens and the user come from FakeProviders), gets the app with its user and no CSP violation;
    a share link opens the map without the list of runs, measuring or violations."""
    import re
    import socket
    import threading

    import uvicorn
    from fastapi import Request
    from starlette.staticfiles import StaticFiles

    from jobs import Jobs
    from test_web import APP_STUBS, CSP_STUB, _violations
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    site = f"http://localhost:{port}"
    providers = FakeProviders(site)
    monkeypatch.setitem(ac.GITHUB, "authorize_url", f"{site}/test-provider/authorize")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "app_stubs.js").write_text(APP_STUBS, encoding="utf-8")
    (stubs / "app_check.js").write_text(APP_CHECK, encoding="utf-8")
    (stubs / "csp_stub.js").write_text(CSP_STUB, encoding="utf-8")
    (stubs / "share_check.js").write_text(SHARE_CHECK, encoding="utf-8")
    monkeypatch.setattr(web, "SIGNIN_HTML", web.SIGNIN_HTML.replace(
        "</head>", '<script src="/test/csp_stub.js"></script></head>'))
    monkeypatch.setattr(web, "APP_HTML", web.APP_HTML.replace(
        '<script src="/app/app.js"></script>',
        '<script src="/test/app_stubs.js"></script><script src="/app/app.js"></script>'
        '<script src="/test/app_check.js"></script>'))

    data = tmp_path / "data"
    data.mkdir()
    copy_run(clip_run, data, RUN_A, "A's run")
    map_html = data / "runs" / RUN_A / "map.html"
    map_html.write_text(map_html.read_text("utf-8").replace("<head>", '<head><script src="/test/csp_stub.js"></script>')
                        .replace("</body>", '<script src="/test/share_check.js"></script></body>'), "utf-8")
    user_id, _, _ = session_of(data, account_settings, "a")
    Jobs(str(data)).add_run(RUN_A, user_id, 1)
    token = "edge-share-token-" + "x" * 30
    Jobs(str(data)).set_share(RUN_A, ac.digest(token))

    settings = PublicSettings(public_url=site, trusted_proxy="127.0.0.1", contact="admin@example.org")
    app = web.create_app(str(data), public=settings, accounts=account_settings, oauth_transport=providers.transport)

    @app.get("/test-provider/authorize")
    def authorize(request: Request):  # the provider's page: the user agrees at once
        from fastapi.responses import RedirectResponse
        return RedirectResponse(providers.authorize(str(request.url), {"id": 11, "login": "a"}), 302)
    app.mount("/test", StaticFiles(directory=str(stubs)))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        while not server.started:
            time.sleep(0.1)
        dom = _edge(f"{site}/login/github?next=/", tmp_path / "edge")
        result = json.loads(re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1).replace("&quot;", '"'))
        assert result["user"] and result["user"].startswith("a") and "sign out" in result["user"], result
        assert _violations(dom) == []
        dom = _edge(f"{site}/s/{token}/map.html", tmp_path / "edge-anonymous")
        result = json.loads(re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1).replace("&quot;", '"'))
        assert result["map"] and result["runs"] and result["app"] == {"profiles": False}, result
        assert not result["measure"] and result["others"] == 0 and result["lines"] > 0, result
        assert _violations(dom) == []
        Jobs(str(data)).set_visibility(RUN_A, "demo")  # a demo: the anonymous page and the map
        dom = _edge(f"{site}/", tmp_path / "edge-visitor")
        assert f"/runs/{RUN_A}/map.html" in dom and _violations(dom) == []
        dom = _edge(f"{site}/runs/{RUN_A}/map.html", tmp_path / "edge-visitor")
        result = json.loads(re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1).replace("&quot;", '"'))
        assert result["map"] and result["app"] == {"profiles": False} and not result["measure"], result
        assert result["lines"] > 0 and _violations(dom) == []
    finally:
        server.should_exit = True
        thread.join(10)


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


def test_databases_get_the_tiers_and_usage(tmp_path, account_settings):
    """accounts.sqlite of version 1 gets users.unlimited (its users: with limits), jobs.sqlite of
    version 6 the jobs' and quick tasks' tier and the table usage (its jobs: with limits)."""
    import sqlite3

    import cache
    import jobs as jb
    db = sqlite3.connect(tmp_path / "accounts.sqlite")
    cache.migrate(db, ac.MIGRATIONS[:1])
    db.execute("INSERT INTO users (provider, subject, name, status) VALUES ('github', '11', 'a', 'approved')")
    db.commit()
    db.close()
    users = ac.Accounts(str(tmp_path), account_settings)
    user = users.users()[0]
    assert user.name == "a" and not user.unlimited and user.limited
    users.set_unlimited(user.id, True)
    assert not users.user(user.id).limited and not users.user(user.id).admin
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    cache.migrate(db, jb.MIGRATIONS[:6])
    db.execute("INSERT INTO jobs (id, created, status, request, title, user_id) VALUES ('old', "
               "'2026-10-01T10:00:00', 'queued', '{}', 'Old job', 11)")
    db.commit()
    db.close()
    jobs = jb.Jobs(str(tmp_path))
    assert jobs.get("old")["unlimited"] == 0
    jobs.use(11, "km2", 3, None)
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(jb.MIGRATIONS) >= 7
    assert db.execute("SELECT amount FROM usage").fetchall() == [(3.0,)]
    db.close()


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
    job = jobs.add({"params": params, "geometry": None}, "clip", user_id=5)
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
    saved = jobs.run(jobs.get(job)["run_id"])  # the run belongs to the job's owner
    assert saved["user_id"] == 5 and saved["visibility"] == "private" and saved["bytes"] > 0
    assert jobs.get(broken)["error"] == f"internal error, ref {broken}"
    assert answer["code"] == 200, answer
    assert jobs.get(failing)["status"] == "failed" and "file not found: missing.tif" in jobs.get(failing)["error"]
    ended = [{k: jobs.get(j)[k] for k in ("status", "error", "run_id")} for j in (job, failing, broken)]
    texts = json.dumps([jobs.events(0), ended, answer])
    for folder in (str(tmp_path), REPO):
        for form in (folder, folder.replace("\\", "/"), json.dumps(folder)[1:-1]):
            assert form not in texts, form
    assert "saved to runs" in texts
