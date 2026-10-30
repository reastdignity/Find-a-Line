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
import messages  # noqa: E402
import web  # noqa: E402
from settings import AccountSettings, PublicSettings  # noqa: E402

pytest.importorskip("httpx2")
import httpx2  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "test-token"
URL = "https://lines.example.org"
LOCAL = "http://127.0.0.1:8765"  # the local app's own host (it refuses TestClient's "testserver")
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


def test_opentopography_key_from_a_secret(monkeypatch, tmp_path):
    """The OpenTopography key comes from the environment or a Docker secret file (the worker's
    in compose); the environment wins, an empty value is no key."""
    import settings
    from params import SearchParams
    monkeypatch.delenv("OPENTOPOGRAPHY_API_KEY", raising=False)
    monkeypatch.setattr(settings, "SECRETS_DIR", str(tmp_path / "nothing"))
    assert settings.opentopography_key() is None
    assert SearchParams(lat=50, lon=19).opentopo_key is None
    (tmp_path / "opentopography_api_key").write_text("secret-key\n")
    monkeypatch.setattr(settings, "SECRETS_DIR", str(tmp_path))
    assert settings.opentopography_key() == "secret-key"
    assert SearchParams(lat=50, lon=19).opentopo_key == "secret-key"
    assert "secret-key" not in repr(settings.ServiceKeys.load())
    monkeypatch.setenv("OPENTOPOGRAPHY_API_KEY", "env-key")
    assert settings.opentopography_key() == "env-key"
    monkeypatch.setenv("OPENTOPOGRAPHY_API_KEY", "")
    monkeypatch.setattr(settings, "SECRETS_DIR", str(tmp_path / "nothing"))
    assert settings.opentopography_key() is None


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
    # The mark of the beta notice: for the page's script (not HttpOnly), 10 minutes, only from a sign-in
    welcome = [c for c in answer.headers.get_list("set-cookie") if c.startswith(web.WELCOME_COOKIE + "=1")]
    assert len(welcome) == 1 and "httponly" not in welcome[0].lower() and all(
        f in welcome[0].lower() for f in ("secure", "samesite=lax", "path=/", "max-age=600")), welcome
    assert web.WELCOME_COOKIE not in public.get("/").headers.get("set-cookie", "")
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
    assert [r["id"] for r in runs] == [RUN_A]
    assert "window.FINDALINE_APP = {\"profiles\": true, \"favourites\": true};" in listed.text
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
    assert 'window.FINDALINE_APP = {"profiles": false, "favourites": false};' in shared_js.text
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
    for odd in ("-" + "x" * 42, "_" + "y" * 42):  # token_urlsafe may start with either
        jobs.set_share(RUN_A, ac.digest(odd))
        assert anonymous.get(f"/s/{odd}/map.html").status_code == 200, odd
    assert anonymous.get("/s/.hidden/map.html").status_code == 404
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
    assert listed(anonymous) == ({RUN_B}, {"profiles": False, "favourites": False})
    assert listed(pending) == ({RUN_B}, {"profiles": False, "favourites": False})
    assert listed(a) == ({RUN_A, RUN_B}, {"profiles": True, "favourites": True})
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

    def fields_of(client):
        return {f["name"]: f for f in client.get("/api/schema").json()["fields"]}

    def sources(client):
        fields = fields_of(client)
        return "threads" in fields, {"nasadem", "malta2012"} & set(fields.get("source", {}).get("choices", []))
    assert sources(a) == (False, set()) and sources(unlimited) == (True, set())
    assert sources(admin) == (True, {"nasadem", "malta2012"})
    # The downloads are the site's: a user's form has none of their fields, and a request with one is refused
    downloads = {"update", "source", "water", "shore_buffer", "fill_gaps", "coastline", "ortho", "ortho_detail"}
    assert not downloads & set(fields_of(a)) and downloads <= set(fields_of(unlimited)), fields_of(a).keys()
    for name, value in {"source": "geoportal", "water": "off", "ortho": "never", "fill_gaps": "off"}.items():
        refused = people["b"].post("/api/jobs", json={"params": {**point, name: value}})  # not a's rate
        detail = refused.json()["detail"]
        assert refused.status_code == 400 and detail["code"] == "not_allowed", (name, refused.text)
        assert detail["params"] == {"field": name} and "cannot be changed on this site" in detail["message"]
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
    assert day.status_code == 429 and day.json()["detail"]["params"] == {"max": 4}
    assert unlimited.delete(f"/api/jobs/{big.json()['id']}").status_code == 200
    assert unlimited.post(f"/api/jobs/{big.json()['id']}/again").status_code == 200

    # The size of the runs and the km² of the last 24 hours (B: a fresh user)
    b = people["b"]
    jobs.add_run("big-run", b.user_id, int(6e9))
    refused = b.post("/api/jobs", json=request)
    assert refused.status_code == 429 and refused.json()["detail"]["code"] == "quota_runs_gb"
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
    with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False), base_url=LOCAL) as local:
        assert local.delete(f"/api/runs/{RUN_B}", headers={"x-findaline-token": TOKEN}).status_code == 404
    assert (tmp_path / "runs" / RUN_B).exists()


FAVOURITE = {"len": 120.5, "clr": 1.2, "clrN": None, "e1": 330.0, "e2": 328.5, "over": False, "water": 0.0,
             "filled": 0.0, "hmax": 15.0, "hmean": 9.5, "highPct": 0.0, "angle1": 12.0, "angle2": None,
             "prom1": 3.0, "prom2": None, "drop1": 30.0, "drop2": None}
LINE, OTHER_LINE = "50.1570825,19.7572871,50.1561275,19.7547235", "50.157,19.757,1e-7,19.754"


def mark(client, run: str, line, on=True, data=FAVOURITE):
    return client.post(f"/api/runs/{run}/favourites",
                       json={"line": line, "favourite": on, **({"data": data} if on else {})})


def test_favourites(run_people, tmp_path, monkeypatch):
    """Favourite lines: a user marks and unmarks lines of the runs they may open (a demo run too, each
    their own), the map gets them with the session's token, the app lists them with their values
    while the run stays open to the user; odd lines and values are refused, a user has at most
    FAVOURITES_MAX; the audit log records them; deleting a run deletes its favourites."""
    import audit
    import jobs as jb
    a, b, admin, anonymous = run_people["a"], run_people["b"], run_people["admin"], run_people["anonymous"]
    ids, jobs = run_people["ids"], run_people["jobs"]
    lines = lambda client, run=RUN_A: client.get(f"/api/runs/{run}/favourites").json()["lines"]  # noqa: E731
    assert a.get(f"/api/runs/{RUN_A}/favourites").json() == {"lines": [], "in": {}, "folders": [],
                                                              "token": a.headers["x-findaline-token"]}
    assert mark(a, RUN_A, LINE).status_code == 200 and mark(a, RUN_A, LINE).status_code == 200  # once
    assert mark(a, RUN_A, OTHER_LINE).status_code == 200
    assert lines(a) == [OTHER_LINE, LINE]  # newest first
    assert mark(a, RUN_A, OTHER_LINE, on=False).status_code == 200 and lines(a) == [LINE]
    assert b.get(f"/api/runs/{RUN_A}/favourites").status_code == 404 and mark(b, RUN_A, LINE).status_code == 404
    assert anonymous.get(f"/api/runs/{RUN_A}/favourites").status_code == 401
    assert mark(anonymous, RUN_A, LINE).status_code == 401
    assert mark(run_people["pending"], RUN_A, LINE).status_code == 403
    for line in ("1,2,3", "1,2,3,x", "91,0,0,0", "1,2,3,4;", "1,2,3,nan", "1_0,2,3,4", "1," * 60 + "1", 5, None):
        assert mark(a, RUN_A, line).status_code == 400, line
    for data in ({**FAVOURITE, "len": "inf"}, {**FAVOURITE, "extra": 1}, {**FAVOURITE, "over": "no"}, None, []):
        assert mark(a, RUN_A, OTHER_LINE, data=data).status_code == 400, data
    assert a.post(f"/api/runs/{RUN_A}/favourites", json={"line": LINE, "favourite": "yes"}).status_code == 400
    assert a.post(f"/api/runs/{RUN_A}/favourites", json={"line": LINE, "favourite": True},
                  headers={"x-findaline-token": "wrong"}).status_code == 403

    assert admin.post(f"/api/runs/{RUN_A}/demo", json={"demo": True}).status_code == 200
    assert mark(b, RUN_A, OTHER_LINE).status_code == 200
    assert lines(b) == [OTHER_LINE] and lines(a) == [LINE]
    listed = b.get("/api/favourites").json()["favourites"]
    assert [(f["run_id"], f["line"], f["run_name"], f["data"]) for f in listed] == [(RUN_A, OTHER_LINE, "A's run",
                                                                                      FAVOURITE)]
    assert listed[0]["run_created"] and listed[0]["created"]
    assert admin.post(f"/api/runs/{RUN_A}/demo", json={"demo": False}).status_code == 200
    assert b.get("/api/favourites").json() == {"favourites": [], "folders": []}  # no longer a run b may open
    assert [f["line"] for f in a.get("/api/favourites").json()["favourites"]] == [LINE]

    monkeypatch.setattr(jb, "FAVOURITES_MAX", 1)
    refused = mark(a, RUN_A, OTHER_LINE)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "favourites_max"
    assert mark(a, RUN_A, LINE).status_code == 200  # marked already: no new row
    log = audit.Audit(str(tmp_path)).entries()
    assert {"line": LINE, "favourite": True} in [e["detail"] for e in log if e["action"] == "favourite"
                                                 and e["user_id"] == ids["a"] and e["target"] == RUN_A]
    assert any(e["action"] == "refused" and "favourite" in e["detail"]["reason"] for e in log)

    assert a.delete(f"/api/runs/{RUN_A}").status_code == 200
    assert jobs.favourites(ids["a"]) == [] and jobs.favourites(ids["b"]) == []


def test_favourite_folders(run_people, tmp_path, monkeypatch):
    """Folders of favourites (as a browser's bookmarks): nested, a user's own (another's answers 404),
    renamed and moved (never into themselves, at most FOLDER_DEPTH_MAX deep), deleted with the folders
    and favourites in them; a favourite is marked into a folder and moved, marking it again keeps its
    folder; names are checked, a user has at most FOLDERS_MAX; the audit log records them."""
    import audit
    import jobs as jb
    a, b, ids = run_people["a"], run_people["b"], run_people["ids"]
    add = lambda client, name, parent=None: client.post("/api/favourites/folders",  # noqa: E731
                                                        json={"name": name, "parent": parent})
    folders = lambda client: {f["name"]: f for f in client.get("/api/favourites").json()["folders"]}  # noqa: E731
    trips = add(a, "  Trips ").json()["id"]
    tatras = add(a, "Tatras", trips).json()["id"]
    later = add(a, "Later").json()["id"]
    assert {n: f["parent_id"] for n, f in folders(a).items()} == {"Trips": None, "Tatras": trips, "Later": None}
    assert folders(b) == {} and add(b, "Mine", trips).status_code == 404
    for name in ("", "   ", "x" * (jb.FOLDER_NAME_MAX + 1), "a\nb", 5, None):
        assert add(a, name).status_code == 400, name
    assert a.post("/api/favourites/folders", json={"name": "X", "parent": True}).status_code == 400
    assert a.post("/api/favourites/folders", json={"name": "X", "parent": "1"}).status_code == 400

    # Marked into a folder, again keeps it, moved; another user's folder is not found
    body = {"line": LINE, "favourite": True, "data": FAVOURITE, "folder": tatras}
    assert a.post(f"/api/runs/{RUN_A}/favourites", json=body).status_code == 200
    assert a.post(f"/api/runs/{RUN_A}/favourites", json={**body, "folder": later}).status_code == 200
    run_favourites = a.get(f"/api/runs/{RUN_A}/favourites").json()
    assert run_favourites["in"] == {LINE: tatras} and len(run_favourites["folders"]) == 3
    assert a.post(f"/api/runs/{RUN_A}/favourites", json={**body, "line": OTHER_LINE, "folder": 999}).status_code == 404
    move = lambda client, line, folder: client.post(f"/api/runs/{RUN_A}/favourites/folder",  # noqa: E731
                                                    json={"line": line, "folder": folder})
    assert move(a, LINE, later).status_code == 200 and jb.Jobs(str(tmp_path)).favourites(ids["a"])[0]["folder_id"] == later
    assert move(a, LINE, None).status_code == 200 and move(a, LINE, tatras).status_code == 200
    assert move(a, OTHER_LINE, None).status_code == 404 and move(a, LINE, 999).status_code == 404
    assert move(b, LINE, None).status_code == 404  # not b's run
    assert move(a, "1,2,3", None).status_code == 400

    # Renamed and moved; never into itself or deeper than the limit
    change = lambda client, folder, body: client.post(f"/api/favourites/folders/{folder}", json=body)  # noqa: E731
    assert change(a, tatras, {"name": "High Tatras"}).status_code == 200
    assert change(a, trips, {"parent": tatras}).status_code == 400 and change(a, trips, {"parent": trips}).status_code == 400
    assert change(a, tatras, {"parent": later}).status_code == 200 and folders(a)["High Tatras"]["parent_id"] == later
    assert change(a, tatras, {"parent": None}).status_code == 200 and folders(a)["High Tatras"]["parent_id"] is None
    assert change(a, tatras, {}).status_code == 400 and change(b, tatras, {"name": "B"}).status_code == 404
    deep = later
    for k in range(jb.FOLDER_DEPTH_MAX - 1):
        deep = add(a, f"level {k + 2}", deep).json()["id"]
    assert add(a, "too deep", deep).status_code == 400
    assert change(a, tatras, {"parent": deep}).status_code == 400  # depth 8 + 1

    # Deleted with what is in it
    assert change(a, tatras, {"parent": later}).status_code == 200
    assert b.delete(f"/api/favourites/folders/{later}").status_code == 404
    gone = a.delete(f"/api/favourites/folders/{later}")
    assert gone.json() == {"folders": jb.FOLDER_DEPTH_MAX + 1, "favourites": 1}, gone.text
    assert set(folders(a)) == {"Trips"} and a.get("/api/favourites").json()["favourites"] == []
    assert a.delete(f"/api/favourites/folders/{later}").status_code == 404

    monkeypatch.setattr(jb, "FOLDERS_MAX", 1)
    refused = add(a, "One more")
    assert refused.status_code == 409 and refused.json()["detail"] == {
        "message": "Folders: at most 1. Delete some first.", "code": "folders_max", "params": {"max": 1}}
    log = [e for e in audit.Audit(str(tmp_path)).entries(ids["a"]) if e["action"] == "favourite_folder"]
    assert [e["detail"]["do"] for e in log].count("add") == 3 + jb.FOLDER_DEPTH_MAX - 1
    assert {"do": "delete", "folders": jb.FOLDER_DEPTH_MAX + 1, "favourites": 1} in [e["detail"] for e in log]
    assert not any("name" in e["detail"] for e in log)  # the names stay out of the log


def test_problem_reports(run_people, tmp_path, monkeypatch):
    """Problem reports: signed-in users send them (category, text, the page's context; the server adds the
    User-Agent and leaves out a run the user may not open), within the report rate; administrators list
    them with names, mark them done and delete them; the audit log has no text."""
    import audit
    import jobs as jb
    a, b, admin = run_people["a"], run_people["b"], run_people["admin"]
    ids = run_people["ids"]
    body = {"category": "water", "text": "  The lake is 3 m too high.\nSee the second line.  ",
            "context": {"page": "#run=x&line=1", "ui": "d", "run": RUN_A, "line": LINE, "viewport": "1280x800"}}
    sent = a.post("/api/reports", json=body, headers={"user-agent": "TestBrowser/1.0"})
    assert sent.status_code == 200, sent.text
    assert run_people["anonymous"].post("/api/reports", json=body).status_code == 401
    assert run_people["pending"].post("/api/reports", json=body).status_code == 403
    for wrong in ({**body, "category": "bugs"}, {**body, "text": " "}, {**body, "text": "x" * (jb.REPORT_TEXT_MAX + 1)},
                  {**body, "text": "a\x00b"}, {**body, "text": 5}, {"text": "no category"}):
        assert admin.post("/api/reports", json=wrong).status_code == 400, wrong  # (no rate for administrators)
    odd = b.post("/api/reports", json={**body, "context": {"run": RUN_A, "line": LINE, "ui": "x", "viewport": "big",
                                                             "page": "p\x01"}})  # not b's run, odd values
    assert odd.status_code == 200
    listed = admin.get("/api/admin/reports").json()["reports"]
    assert [r["user_id"] for r in listed] == [ids["b"], ids["a"]]
    first = listed[1]
    assert first["text"] == "The lake is 3 m too high.\nSee the second line." and first["status"] == "open"
    assert first["context"] == {"page": "#run=x&line=1", "ui": "d", "run": RUN_A, "line": LINE, "viewport": "1280x800",
                                "user_agent": "TestBrowser/1.0"}
    assert first["user_name"] == "a" and first["run_name"] == "A's run"
    assert set(listed[0]["context"]) == {"user_agent"} and listed[0]["run_name"] is None
    assert a.get("/api/admin/reports").status_code == 403
    assert a.post(f"/api/admin/reports/{first['id']}", json={"status": "done"}).status_code == 403
    assert admin.post(f"/api/admin/reports/{first['id']}", json={"status": "done"}).status_code == 200
    assert admin.post(f"/api/admin/reports/{first['id']}", json={"status": "gone"}).status_code == 400
    assert admin.post("/api/admin/reports/999", json={"status": "done"}).status_code == 404
    assert admin.get("/api/admin/reports").json()["reports"][1]["status"] == "done"
    assert admin.delete(f"/api/admin/reports/{listed[0]['id']}").status_code == 200
    assert admin.delete(f"/api/admin/reports/{listed[0]['id']}").status_code == 404
    assert len(admin.get("/api/admin/reports").json()["reports"]) == 1
    statuses = [a.post("/api/reports", json=body).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]  # 3 a minute: one sent already
    log = [e for e in audit.Audit(str(tmp_path)).entries() if e["action"] == "report"]
    assert {e["detail"]["do"] for e in log} == {"add", "status", "delete"}
    assert not any("text" in e["detail"] or "lake" in json.dumps(e["detail"]) for e in log)
    privacy = run_people["anonymous"].get("/privacy").text
    assert "Problem reports" in privacy and web.WELCOME_COOKIE in privacy


def test_local_favourites(tmp_path, clip_run, settings, account_settings, providers):
    """The local app: its one user's favourites (user_id NULL), its token for the map; when the data
    folder goes public, they go to the first administrator with the runs."""
    copy_run(clip_run, tmp_path, RUN_A, "A's run")
    with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False), base_url=LOCAL) as local:
        assert local.get(f"/api/runs/{RUN_A}/favourites").json() == {"lines": [], "in": {}, "folders": [],
                                                                     "token": TOKEN}
        assert mark(local, RUN_A, LINE).status_code == 403  # without the token
        local.headers["x-findaline-token"] = TOKEN
        assert mark(local, RUN_A, LINE).status_code == 200
        listed = local.get("/api/favourites").json()["favourites"]
        assert [(f["run_id"], f["line"]) for f in listed] == [(RUN_A, LINE)]
    _public_app(tmp_path, settings, account_settings, providers)  # adopts the runs and the favourites
    admin = ac.Accounts(str(tmp_path), account_settings).first_admin()
    from jobs import Jobs
    assert [f["line"] for f in Jobs(str(tmp_path)).favourites(admin)] == [LINE]
    assert Jobs(str(tmp_path)).favourites(None) == []


def test_admin_tab(run_people, tmp_path):
    """The admin tab's requests are an administrator's only (a user without limits too gets 403): the
    server's state, the users (approve, block and unblock, without limits, end sessions; not other
    administrators), every run with its owner, the OSM extracts; nothing of it locally."""
    admin, a, b, pending, anonymous = (run_people[w] for w in ("admin", "a", "b", "pending", "anonymous"))
    ids = run_people["ids"]
    routes = [("get", "/api/admin/status"), ("get", "/api/admin/users"), ("get", "/api/admin/runs"),
              ("post", f"/api/admin/users/{ids['b']}"), ("post", f"/api/admin/users/{ids['b']}/revoke"),
              ("get", "/api/osm?lat=50&lon=19"), ("post", "/api/osm")]
    for who, code in (("a", 403), ("unlimited", 403), ("pending", 403), ("anonymous", 401)):
        for method, path in routes:
            kwargs = {"json": {"status": "approved"}} if method == "post" else {}
            assert getattr(run_people[who], method)(path, **kwargs).status_code == code, (who, path)
    status = admin.get("/api/admin/status").json()
    assert status["free_gb"] > 0 and status["queue"] == 0 and status["queue_max"] == 50 and status["workers"] == []
    listed = {u["name"]: u for u in admin.get("/api/admin/users").json()["users"]}
    assert listed["pending"]["status"] == "pending" and listed["admin"]["admin"] and listed["unlimited"]["unlimited"]
    assert listed["a"]["sessions"] >= 1 and "subject" in listed["a"]

    assert admin.post(f"/api/admin/users/{ids['pending']}", json={"status": "approved"}).status_code == 200
    assert pending.get("/api/jobs").status_code == 200
    assert admin.post(f"/api/admin/users/{ids['a']}", json={"status": "blocked"}).status_code == 200
    assert a.get("/api/jobs").status_code == 401  # blocking ended the sessions
    assert admin.post(f"/api/admin/users/{ids['a']}", json={"status": "approved"}).status_code == 200
    users = ac.Accounts(str(tmp_path), admin.app.state.account_settings)
    assert users.user(ids["a"]).status == "approved"
    assert admin.post(f"/api/admin/users/{ids['b']}", json={"unlimited": True}).status_code == 200
    assert users.user(ids["b"]).unlimited and not users.user(ids["b"]).admin
    assert "threads" in {f["name"] for f in b.get("/api/schema").json()["fields"]}  # at the next request
    assert admin.post(f"/api/admin/users/{ids['b']}", json={"unlimited": False}).status_code == 200
    assert admin.post(f"/api/admin/users/{ids['b']}", json={"status": "admin"}).status_code == 400
    assert admin.post(f"/api/admin/users/{ids['admin']}", json={"status": "blocked"}).status_code == 400
    assert admin.post("/api/admin/users/9999", json={"status": "blocked"}).status_code == 404
    assert admin.post(f"/api/admin/users/{ids['b']}/revoke").status_code == 200
    assert b.get("/api/jobs").status_code == 401

    runs = {r["run_id"]: r for r in admin.get("/api/admin/runs").json()["runs"]}
    assert set(runs) == {RUN_A, RUN_B, RUN_OLD} and runs[RUN_A]["user_id"] == ids["a"]
    assert runs[RUN_A]["name"] == "A's run" and runs[RUN_A]["visibility"] == "private"
    assert admin.post(f"/api/runs/{RUN_A}/demo", json={"demo": True}).status_code == 200
    assert {r["run_id"]: r for r in admin.get("/api/admin/runs").json()["runs"]}[RUN_A]["visibility"] == "demo"
    assert anonymous.get(f"/runs/{RUN_A}/map.html").status_code == 200
    with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False), base_url=LOCAL) as local:
        assert local.get("/api/admin/users").status_code == 404 and local.get("/api/admin/status").status_code == 404


def test_audit_log(run_people, tmp_path, providers):
    """The audit log (audit.sqlite) records sign-ups and sign-ins, searches and cancels, share links,
    deletions, the demo flag, administrators' actions and refusals by a limit (the web server's and
    the worker's), with user ids and no IP addresses; the worker's housekeeping deletes the events
    older than 90 days; the local app keeps none."""
    import sqlite3

    import audit
    from jobs import Jobs, QuotaExceeded, Scheduler
    a, admin = run_people["a"], run_people["admin"]
    ids = run_people["ids"]
    request = {"params": {"lat": 50.1, "lon": 19.7, "radius": 100}, "geometry": None}
    visitor = TestClient(a.app, base_url=URL)
    assert sign_in(visitor, providers, {"id": 77, "login": "newcomer"}).status_code == 303
    assert sign_in(visitor, providers, {"id": 77, "login": "newcomer"}).status_code == 303
    job = a.post("/api/jobs", json=request).json()["id"]
    a.post("/api/jobs", json=request)
    assert a.post("/api/jobs", json=request).status_code == 429  # at most two at once
    assert admin.delete(f"/api/jobs/{job}").status_code == 200
    a.post(f"/api/runs/{RUN_A}/share")
    a.post(f"/api/runs/{RUN_A}/share")
    a.delete(f"/api/runs/{RUN_A}/share")
    admin.post(f"/api/runs/{RUN_B}/demo", json={"demo": True})
    admin.post(f"/api/admin/users/{ids['pending']}", json={"status": "approved"})
    admin.post(f"/api/admin/users/{ids['b']}", json={"unlimited": True})
    admin.post(f"/api/admin/users/{ids['b']}/revoke")
    assert a.delete(f"/api/runs/{RUN_A}").status_code == 200
    jobs = Jobs(str(tmp_path), public=True)  # the worker's: a refusal of its quotas
    with pytest.raises(QuotaExceeded):
        jobs.use(ids["a"], "nominatim", 1, 0.5)
    a.post("/logout")

    log = audit.Audit(str(tmp_path)).entries()
    actions = [(e["action"], e["user_id"]) for e in log]
    for expected in [("sign_up", None), ("sign_in", None), ("job_add", ids["a"]), ("refused", ids["a"]),
                     ("job_cancel", ids["admin"]), ("share_create", ids["a"]), ("share_renew", ids["a"]),
                     ("share_revoke", ids["a"]), ("run_demo", ids["admin"]), ("user_status", ids["admin"]),
                     ("user_unlimited", ids["admin"]), ("user_revoke", ids["admin"]), ("run_delete", ids["a"]),
                     ("sign_out", ids["a"])]:
        assert any(action == expected[0] and (expected[1] is None or user == expected[1])
                   for action, user in actions), expected
    by_action = {e["action"]: e for e in log}
    assert by_action["sign_up"]["detail"] == {"provider": "github", "status": "pending"}
    assert by_action["job_cancel"]["detail"] == {"owner": ids["a"]} and by_action["job_cancel"]["target"] == job
    assert by_action["user_status"]["target"] == str(ids["pending"])
    refusals = [e["detail"]["reason"] for e in log if e["action"] == "refused"]
    assert any("at most 2" in r for r in refusals) and any("Nominatim" in r for r in refusals)
    text = json.dumps(log)
    assert "testclient" not in text and "127.0.0.1" not in text  # no addresses
    assert "ip" not in {c[1] for c in sqlite3.connect(tmp_path / "audit.sqlite").execute("PRAGMA table_info(audit)")}

    db = sqlite3.connect(tmp_path / "audit.sqlite")
    db.execute("INSERT INTO audit (at, action) VALUES ('2020-01-01T00:00:00+00:00', 'sign_in')")
    db.commit()
    db.close()
    count = len(audit.Audit(str(tmp_path)).entries())
    Scheduler(jobs).housekeeping()
    assert len(audit.Audit(str(tmp_path)).entries()) == count - 1
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    with TestClient(web.create_app(str(local_dir), token=TOKEN, start_scheduler=False), base_url=LOCAL) as local:
        local.post("/api/jobs", json=request, headers={"x-findaline-token": TOKEN})
    assert not (local_dir / "audit.sqlite").exists()


def test_privacy_and_about_pages(run_people):
    """The privacy and about pages: for anyone, server-rendered (no script, no inline style), linked
    from the anonymous page and the app; privacy states what is stored, for how long, that
    administrators open private runs and that orthophoto tiles are open, with the contact; about
    names every source's credits and OpenStreetMap."""
    import sources as src
    anonymous, a = run_people["anonymous"], run_people["a"]
    privacy = anonymous.get("/privacy")
    about = anonymous.get("/about")
    for page in (privacy, about):
        assert page.status_code == 200 and "<script" not in page.text and "style=" not in page.text
        assert "<style" not in page.text and "admin@example.org" in page.text
    text = privacy.text
    for fact in ("display name", "SHA-256", "30 days", "7 days", "90 days", "Audit log", "open every run",
                 "orthophoto tiles", "stores none", "No analytics"):
        assert fact in text, fact
    for source in src.SOURCES.values():
        for credit in source.credits:
            assert web.html.escape(credit.data) in about.text, credit.data
    assert "OpenStreetMap contributors" in about.text and "ODbL" in about.text
    page = anonymous.get("/").text
    assert "href=\"/privacy\"" in page and "href=\"/about\"" in page
    bundle = a.get("/app/app.js").text
    assert "/privacy" in bundle and "/about" in bundle


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
  const wait = (ok, then, tries = 100) => ok() ? then() : tries ? setTimeout(() => wait(ok, then, tries - 1), 100)
                                                    : (out.textContent = "timeout " + location.href + " " + document.body.className + " " + [...document.querySelectorAll("[class]")].slice(0, 12).map((e) => e.className).join(","));
  const panels = () => [...document.querySelectorAll(".c-admin .u-panel")];
  wait(() => document.querySelector(".c-home"), () => setTimeout(() => {  // after the shell's hash listener
    location.hash = "#account";
    wait(() => document.querySelector(".c-account"), () => {
      const result = {user: document.querySelector(".c-account-who").textContent,
        signout: !!document.querySelector(".c-signout"),
        links: [...document.querySelectorAll(".c-account-links .c-menu-item")].map((a) => a.textContent.trim())};
      const done = () => { out.textContent = JSON.stringify(result); };
      if (!result.links.includes("Admin")) return done();
      location.hash = "#admin";
      wait(() => panels().length && document.querySelector(".c-admin-row"), () => {
        result.sections = panels().map((p) => p.querySelector(".u-h").textContent);
        result.users = panels().find((p) => p.querySelector(".u-h").textContent.startsWith("Users"))
          .querySelectorAll(".c-admin-row").length;
        done();
      });
    });
  }, 300));
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
    for name in ("SIGNIN_HTML", "PAGE_HTML"):  # the anonymous, privacy and about pages: count violations
        monkeypatch.setattr(web, name, getattr(web, name).replace(
            "</head>", '<script src="/test/csp_stub.js"></script></head>'))
    monkeypatch.setattr(web, "APP_HTML", web.APP_HTML.replace(
        '<script src="/app/app.js"></script>',
        '<script src="/test/app_stubs.js"></script><script src="/app/app.js"></script>'
        '<script src="/test/app_check.js"></script>'))

    data = tmp_path / "data"
    data.mkdir()
    (data / "cache" / "osm").mkdir(parents=True)  # the admin tab's OSM panel: no Geofabrik regions
    (data / "cache" / "osm" / "geofabrik-index.json").write_text(json.dumps({"features": []}))
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

    signing_in = [{"id": 11, "login": "a"}]  # who agrees at the fake provider

    @app.get("/test-provider/authorize")
    def authorize(request: Request):  # the provider's page: the user agrees at once
        from fastapi.responses import RedirectResponse
        return RedirectResponse(providers.authorize(str(request.url), signing_in[0]), 302)
    app.mount("/test", StaticFiles(directory=str(stubs)))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        while not server.started:
            time.sleep(0.1)
        dom = _edge(f"{site}/login/github?next=/", tmp_path / "edge")  # D, the default
        result = json.loads(re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1).replace("&quot;", '"'))
        assert result["user"] and result["user"].startswith("a") and result["signout"], result
        assert "Admin" not in result["links"] and "OpenStreetMap extracts" not in result["links"], result
        assert _violations(dom) == []
        signing_in[0] = {"id": 1, "login": "admin"}  # FINDALINE_ADMINS: the admin page
        dom = _edge(f"{site}/login/github?next=/", tmp_path / "edge-admin")
        result = json.loads(re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1).replace("&quot;", '"'))
        assert {"Admin", "OpenStreetMap extracts"} <= set(result["links"]), result
        assert {"Server", "All searches", "Runs", "OpenStreetMap extracts"} <= set(result["sections"]), result
        assert any(s.startswith("Users") for s in result["sections"]), result
        assert result["users"] >= 2 and _violations(dom) == []
        dom = _edge(f"{site}/s/{token}/map.html", tmp_path / "edge-anonymous")
        result = json.loads(re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1).replace("&quot;", '"'))
        assert result["map"] and result["runs"] and result["app"] == {"profiles": False, "favourites": False}, result
        assert not result["measure"] and result["others"] == 0 and result["lines"] > 0, result
        assert _violations(dom) == []
        Jobs(str(data)).set_visibility(RUN_A, "demo")  # a demo: the anonymous page and the map
        dom = _edge(f"{site}/", tmp_path / "edge-visitor")
        assert f"/runs/{RUN_A}/map.html" in dom and _violations(dom) == []
        dom = _edge(f"{site}/privacy", tmp_path / "edge-visitor")
        assert "What this site stores" in dom and _violations(dom) == []
        dom = _edge(f"{site}/runs/{RUN_A}/map.html", tmp_path / "edge-visitor")
        result = json.loads(re.search(r'<pre id="result">(.*?)</pre>', dom, re.S).group(1).replace("&quot;", '"'))
        assert result["map"] and result["app"] == {"profiles": False, "favourites": False}, result
        assert not result["measure"], result
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


def test_jobs_database_gets_favourites(tmp_path):
    """jobs.sqlite of version 7 gets the table of favourite lines, its jobs and runs stay; a line is a
    favourite of a user once, of the local user (NULL) too."""
    import sqlite3

    import cache
    import jobs as jb
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    cache.migrate(db, jb.MIGRATIONS[:7])
    db.execute("INSERT INTO runs (run_id, user_id) VALUES ('old', 11)")
    db.commit()
    db.close()
    jobs = jb.Jobs(str(tmp_path))
    assert jobs.run("old")["user_id"] == 11
    for user in (11, 11, None, None):
        jobs.set_favourite(user, "old", "1,2,3,4", {"len": 1})
    assert [f["data"] for f in jobs.favourites(11)] == [{"len": 1}] and len(jobs.favourites(None)) == 1
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(jb.MIGRATIONS) >= 8
    db.close()


def test_jobs_database_gets_folders(tmp_path):
    """jobs.sqlite of version 8 gets the folders of favourites; its favourites stay, at the top level; the
    local user's folders go to the first administrator with its favourites (adopt_runs)."""
    import sqlite3

    import cache
    import jobs as jb
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    cache.migrate(db, jb.MIGRATIONS[:8])
    db.execute("INSERT INTO favourites (user_id, run_id, line, data, created) VALUES (NULL, 'old', '1,2,3,4', '{}', '')")
    db.commit()
    db.close()
    jobs = jb.Jobs(str(tmp_path))
    assert [(f["line"], f["folder_id"]) for f in jobs.favourites(None)] == [("1,2,3,4", None)]
    folder = jobs.add_folder(None, "Local")
    jobs.move_favourite(None, "old", "1,2,3,4", folder)
    jobs.adopt_runs(str(tmp_path / "runs"), 7)
    assert [f["name"] for f in jobs.folders(7)] == ["Local"] and jobs.favourites(7)[0]["folder_id"] == folder
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(jb.MIGRATIONS) >= 9
    db.close()


def test_jobs_database_gets_reports(tmp_path):
    """jobs.sqlite of version 9 gets the table of problem reports; its favourites stay."""
    import sqlite3

    import cache
    import jobs as jb
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    cache.migrate(db, jb.MIGRATIONS[:9])
    db.execute("INSERT INTO favourites (user_id, run_id, line, data, created) VALUES (3, 'old', '1,2,3,4', '{}', '')")
    db.commit()
    db.close()
    jobs = jb.Jobs(str(tmp_path))
    assert len(jobs.favourites(3)) == 1
    report = jobs.add_report(3, "results", "none found", {"page": "#home"})
    assert [(r["id"], r["status"], r["context"]) for r in jobs.reports()] == [(report, "open", {"page": "#home"})]
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(jb.MIGRATIONS) >= 10
    db.close()


def test_jobs_database_gets_error_codes(tmp_path):
    """jobs.sqlite of version 10 gets the code of a job's error (error_info): NULL for the jobs of
    before, which keep their text; a coded error is stored as its web text with its code and
    parameters (the public site's without server paths), a text without a code, none for a job done."""
    import sqlite3

    import cache
    import jobs as jb
    import search
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    cache.migrate(db, jb.MIGRATIONS[:10])
    db.execute("INSERT INTO jobs (id, created, status, title, error) VALUES ('old', '', 'failed', 'Old', 'it broke')")
    db.commit()
    db.close()
    jobs = jb.Jobs(str(tmp_path), public=True)
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(jb.MIGRATIONS) == 11
    db.close()
    assert [(j["id"], j["error"], j["error_info"]) for j in jobs.all()] == [("old", "it broke", None)]
    job = jobs.add({"params": {}, "geometry": None}, "New")
    error = search.SearchError("Not enough steep terrain. Try a lower --slope-threshold", "no_steep_terrain")
    assert jobs.set(job, "failed", error=error)
    stored = jobs.get(job)
    assert stored["error"] == messages.text("error.no_steep_terrain") and "--" not in stored["error"]
    assert next(j for j in jobs.all() if j["id"] == job)["error_info"] == {"code": "no_steep_terrain", "params": {}}
    status = jobs.job_events(job)["events"][-1]["data"]
    assert status["error_info"] == {"code": "no_steep_terrain", "params": {}}, status
    folder = str(tmp_path / "cache" / "x.tif")
    jobs.set(job, "failed", error=search.SearchError(f"Download failed: {folder}", "download_failed", reason=folder))
    info = next(j for j in jobs.all() if j["id"] == job)["error_info"]
    assert info["code"] == "download_failed" and str(tmp_path) not in json.dumps(info), info
    jobs.set(job, "failed", error=ValueError("a text"))
    assert (jobs.get(job)["error"], jobs.get(job)["error_info"]) == ("a text", None)
    jobs.set(job, "done", error=None)
    assert (jobs.get(job)["error"], jobs.get(job)["error_info"]) == (None, None)


def test_local_headers(tmp_path):
    """The local app sends the same security headers and still takes requests without an Origin."""
    with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False), base_url=LOCAL) as client:
        response = client.get("/api/schema")
        assert response.status_code == 200
        assert response.headers["referrer-policy"] == "strict-origin"
        assert response.headers["cross-origin-opener-policy"] == "same-origin"
        assert client.get("/", headers={"host": "localhost:8765"}).status_code == 200


def test_public_time_limits_and_one_waiting_estimate(tmp_path, settings, account_settings, providers):
    """The public site has time limits by default (FINDALINE_LIMIT_* still override them); a user has
    one estimate or profile waiting at most (others' tasks do not count)."""
    from jobs import QuotaExceeded
    from settings import Limits
    limits = Limits.load(public=True)
    assert (limits.download_seconds, limits.compute_seconds, limits.osm_seconds, limits.quick_seconds) == \
        (3600, 7200, 7200, 120)
    app = _public_app(tmp_path, settings, account_settings, providers)
    people = _clients(app, tmp_path, account_settings, ("a", "b"))
    jobs = app.state.jobs
    jobs.register("busy-worker")
    jobs.quick_add("estimate", {}, people["a"].user_id, one_waiting=True)
    with pytest.raises(QuotaExceeded):
        jobs.quick_add("estimate", {}, people["a"].user_id, one_waiting=True)
    answer = people["a"].post("/api/estimate", json={"params": POINT})
    assert answer.status_code == 429 and answer.json()["detail"]["code"] == "quick_waiting"
    jobs.quick_add("estimate", {}, None, one_waiting=True)  # the local app: no such rule
    jobs.quick_add("estimate", {}, people["b"].user_id, one_waiting=True)
    for client in people.values():
        client.close()


def test_place_lookup(tmp_path, settings, account_settings, providers):
    """/api/place: signed-in users only, a name of 1 to PLACE_CHARS_MAX characters, the place rate of a
    limited user; answered by the worker's quick lane (503 without one)."""
    from settings import Limits
    limits = Limits.load(public=True).model_copy(update={"place_rate": "2/minute"})
    app = _public_app(tmp_path, settings, account_settings, providers, limits=limits)
    with TestClient(app, base_url=URL) as visitor:
        headers = {"x-findaline-token": TOKEN, "origin": URL}
        assert visitor.post("/api/place", json={"q": "Kraków"}, headers=headers).status_code == 401
    people = _clients(app, tmp_path, account_settings, ("a", "unlimited"))
    a = people["a"]
    for body in ({}, {"q": " "}, {"q": 5}, {"q": "x" * (web.PLACE_CHARS_MAX + 1)}):
        assert a.post("/api/place", json=body).status_code == 400, body
    assert [a.post("/api/place", json={"q": "Kraków"}).status_code for _ in range(3)] == [503, 503, 429]
    assert {people["unlimited"].post("/api/place", json={"q": "Kraków"}).status_code for _ in range(3)} == {503}
    for client in people.values():
        client.close()


def test_new_jobs_rate_and_quota_before_validation(tmp_path, settings, account_settings, providers,
                                                    monkeypatch):
    """/api/jobs refuses a user over a quota before it validates the request (a large drawn geometry
    costs time; review B, poc_geometry_cost), and limits the rate of a user's requests."""
    import jobs as jb
    from settings import Limits
    limits = Limits.load(public=True).model_copy(update={"jobs_active_max": 1, "job_rate": "3/minute"})
    app = _public_app(tmp_path, settings, account_settings, providers, limits=limits)
    people = _clients(app, tmp_path, account_settings, ("a", "b", "unlimited"))
    a, b = people["a"], people["b"]
    assert a.post("/api/jobs", json={"params": POINT}).status_code == 200
    validated = []
    monkeypatch.setattr(web, "search_params", lambda request: validated.append(request) or jb.search_params(request))
    answer = a.post("/api/jobs", json={"params": {}, "geometry": FAR_APART})
    assert answer.status_code == 429 and answer.json()["detail"]["code"] == "quota_active" and validated == []
    assert [a.post("/api/jobs", json={"params": POINT}).status_code for _ in range(2)] == [429, 429]
    answer = a.post("/api/jobs", json={"params": POINT})
    assert answer.status_code == 429 and answer.json()["detail"]["code"] == "rate_job"
    assert b.post("/api/jobs", json={"params": POINT}).status_code == 200  # their own rate
    assert {people["unlimited"].post("/api/jobs", json={"params": POINT}).status_code for _ in range(4)} == {200}
    for client in people.values():
        client.close()


def test_refusals_in_the_audit_log_once_per_window(tmp_path, settings, account_settings, providers,
                                                    monkeypatch):
    """A refusal by a rate is one audit row per user and kind in REFUSAL_WINDOW seconds; the next row
    after the window says how many were not recorded (review A, 5)."""
    import audit
    from settings import Limits
    limits = Limits.load(public=True).model_copy(update={"estimate_rate": "1/hour"})
    app = _public_app(tmp_path, settings, account_settings, providers, limits=limits)
    people = _clients(app, tmp_path, account_settings, ("a", "b"))
    a, b = people["a"], people["b"]
    assert a.post("/api/estimate", json={"params": POINT}).status_code == 503  # the one allowed (no worker)
    assert {a.post("/api/estimate", json={"params": POINT}).status_code for _ in range(200)} == {429}
    assert [b.post("/api/estimate", json={"params": POINT}).status_code for _ in range(3)] == [503, 429, 429]
    rows = lambda who: [e for e in audit.Audit(str(tmp_path)).entries(who.user_id)  # noqa: E731
                        if e["action"] == "refused"]
    assert len(rows(a)) == 1 and len(rows(b)) == 1 and rows(a)[0]["detail"] == {"reason": "estimate rate",
                                                                                 "status": 429}
    monkeypatch.setattr(web, "REFUSAL_WINDOW", 0)  # the window ended: the next refusal is recorded
    assert a.post("/api/estimate", json={"params": POINT}).status_code == 429
    assert rows(a)[-1]["detail"] == {"reason": "estimate rate", "status": 429, "repeated": 199}
    for client in people.values():
        client.close()


def test_rate_limiters_forget(tmp_path, settings, account_settings, providers, monkeypatch):
    """The sign-in limiter keys a client by its address, an IPv6 one by its /64, and forgets the keys
    after their window (the moving window of limits' MemoryStorage kept every key; review A, 6)."""
    monkeypatch.setattr(ac, "LOGIN_RATE", "4/second")
    app = _public_app(tmp_path, settings, account_settings, providers)

    async def from_address(scope, receive, send):  # what uvicorn's proxy headers set
        if scope["type"] == "http":
            for name, value in scope["headers"]:
                if name == b"x-test-ip":
                    scope = {**scope, "client": (value.decode(), 1234)}
        await app(scope, receive, send)
    client = TestClient(from_address, base_url=URL)
    login = lambda ip: client.get("/login/github", headers={"x-test-ip": ip},  # noqa: E731
                                  follow_redirects=False).status_code
    assert [login(f"2001:db8:1:2::{i}") for i in range(6)].count(429) >= 1  # one /64: one counter
    assert login("2001:db8:1:3::1") == 302 and login("192.0.2.1") == 302
    for i in range(300):
        login(f"2001:db8:{i:x}::1")
    limiter = app.state.limiters["login"]
    assert len(limiter.storage.storage) > 300
    time.sleep(2.5)  # two windows: the sliding counter's keys expire
    login("192.0.2.2")  # runs the expiry
    time.sleep(0.5)
    assert len(limiter.storage.storage) <= 2 and not limiter.storage.events
    assert web.client_key("2001:db8:1:2:aaaa::1") == "2001:db8:1:2::/64"
    assert web.client_key("::ffff:192.0.2.7") == "192.0.2.7" and web.client_key("?") == "?"
    client.close()


def _stream(port, cookie, last: bytes = b""):
    """A raw request of /api/events (headers as bytes); the socket."""
    import socket
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(b"GET /api/events HTTP/1.1\r\nHost: lines.example.org\r\n"
                 + f"Cookie: {ac.SESSION_COOKIE}={cookie}\r\n".encode()
                 + (b"Last-Event-ID: " + last + b"\r\n" if last else b"") + b"\r\n")
    return sock


def test_event_streams_at_once_and_after_sign_out(tmp_path, settings, account_settings, providers, monkeypatch):
    """Twenty streams of one user opened at once: SSE_PER_USER are accepted (review A, 2); a stream
    ends when its session ends (blocked, signed out; review A, 3); a Last-Event-ID that is no number
    (a Unicode digit) is 0, not an error (review A, 4b)."""
    import threading

    from jobs import Jobs
    monkeypatch.setattr(web, "PING", 1)
    monkeypatch.setattr(web, "SESSION_CHECK", 1)
    user_id, cookie, _ = session_of(tmp_path, account_settings, "a")
    with _public_server(tmp_path, settings, account_settings, providers) as port:
        barrier, codes, socks = threading.Barrier(20), [], []

        def one():
            barrier.wait()
            sock = _stream(port, cookie)
            socks.append(sock)
            codes.append(sock.recv(64).split(b"\r\n")[0])
        threads = [threading.Thread(target=one) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert sum(code.startswith(b"HTTP/1.1 200") for code in codes) == web.SSE_PER_USER, codes
        for sock in socks:
            sock.close()
        odd = _stream(port, session_of(tmp_path, account_settings, "b")[1], bytes([0xB2]))
        assert odd.recv(64).startswith(b"HTTP/1.1 200")
        odd.close()

        jobs = Jobs(str(tmp_path))
        jobs.add({"params": {}}, "a's job", user_id)
        deadline = time.time() + 15
        while time.time() < deadline:  # the closed streams are released at their next ping
            sock = _stream(port, cookie)
            if sock.recv(64).startswith(b"HTTP/1.1 200"):
                break
            sock.close()
            time.sleep(0.5)
        users = ac.Accounts(str(tmp_path), account_settings)
        users.set_status(user_id, "blocked")
        ended, deadline = False, time.time() + 15
        while time.time() < deadline and not ended:
            ended = sock.recv(4096).endswith(b"0\r\n\r\n")  # the end of the chunked answer
        sock.close()
        assert ended


def test_blocking_cancels_the_jobs(tmp_path, settings, account_settings, providers):
    """Blocking a user cancels their waiting and running jobs and deletes their waiting estimates and
    profiles, with an audit entry; other users' work stays (review A, 7)."""
    import audit
    app = _public_app(tmp_path, settings, account_settings, providers)
    people = _clients(app, tmp_path, account_settings, ("a", "b", "admin"))
    a, b, admin = people["a"], people["b"], people["admin"]
    jobs = app.state.jobs
    first, second = (a.post("/api/jobs", json={"params": POINT}).json()["id"] for _ in range(2))
    other = b.post("/api/jobs", json={"params": POINT}).json()["id"]
    jobs.quick_add("estimate", {}, a.user_id)
    kept = jobs.quick_add("estimate", {}, b.user_id)
    assert admin.post(f"/api/admin/users/{a.user_id}", json={"status": "blocked"}).status_code == 200
    assert {jobs.get(j)["status"] for j in (first, second)} == {"cancelled"} and jobs.get(other)["status"] == "queued"
    assert jobs.claim("queued", "downloading", "w1")["id"] == other
    assert [r[0] for r in jobs._db().execute("SELECT id FROM quick")] == [kept]
    entry = [e for e in audit.Audit(str(tmp_path)).entries() if e["action"] == "user_status"][-1]
    assert entry["detail"]["cancelled"] == [first, second] or set(entry["detail"]["cancelled"]) == {first, second}
    assert entry["detail"]["quick_deleted"] == 1
    for client in people.values():
        client.close()


def test_odd_input_is_no_server_error(tmp_path, settings, account_settings, providers, run_people):
    """A non-ASCII token (header or the logout form) is a wrong token, not a 500 (review A, 4); the
    local app takes only its own hosts (review A, 8); /runs//<run>/... and /s//<token>/... are 404,
    not the run with a path the logs do not mask."""
    a = run_people["a"]
    client = TestClient(a.app, base_url=URL, raise_server_exceptions=False)
    client.headers["origin"] = URL
    client.cookies.set(ac.SESSION_COOKIE, session_of(tmp_path, account_settings, "pending")[1],
                       domain="lines.example.org")
    answer = client.post("/api/jobs", json={"params": POINT}, headers={"x-findaline-token": b"\xe9"})
    assert answer.status_code == 403
    form = client.post("/logout", content="csrf=%C3%A9", headers={"content-type": "application/x-www-form-urlencoded"})
    assert form.status_code == 403
    client.close()
    (tmp_path / "local").mkdir()
    with TestClient(web.create_app(str(tmp_path / "local"), token=TOKEN, start_scheduler=False),
                    base_url="http://testserver") as local:
        assert local.get("/").status_code == 400
    assert a.get(f"/runs/{RUN_A}/data.js").status_code == 200
    assert a.get(f"/runs//{RUN_A}/data.js").status_code == 404
    token = a.post(f"/api/runs/{RUN_A}/share").json()["url"].split("/s/")[1].split("/")[0]
    anonymous = run_people["anonymous"]
    assert anonymous.get(f"/s/{token}/data.js").status_code == 200
    assert anonymous.get(f"/s//{token}/data.js").status_code == 404


def test_referrer_is_the_origin_only(run_people):
    """Every answer, a share page's too, asks for the origin only as the Referer: the browser's own
    requests from /s/<token>/map.html (favicon, cached tiles) do not carry the token."""
    a, anonymous = run_people["a"], run_people["anonymous"]
    token = a.post(f"/api/runs/{RUN_A}/share").json()["url"].split("/s/")[1].split("/")[0]
    for client, path in ((anonymous, f"/s/{token}/map.html"), (a, "/"), (anonymous, "/cache/x/wm256/1.jpg"),
                         (a, f"/runs/{RUN_A}/map.html")):
        assert client.get(path).headers["referrer-policy"] == "strict-origin", path


def test_cache_control(run_people, tmp_path):
    """no-store for the answers with a user's data, private for the files of runs and share links,
    revalidation for the rest; the local app the same way (review C, I4)."""
    a, anonymous = run_people["a"], run_people["anonymous"]
    token = a.post(f"/api/runs/{RUN_A}/share").json()["url"].split("/s/")[1].split("/")[0]
    expected = [(a, "/", "no-store"), (anonymous, "/", "no-cache"), (a, "/api/jobs", "no-store"),
                (anonymous, "/api/jobs", "no-store"), (a, "/runs/runs.js", "no-store"),
                (a, f"/runs/{RUN_A}/map.html", "private, no-cache"), (a, f"/runs/{RUN_A}/data.js", "private, no-cache"),
                (anonymous, f"/s/{token}/map.html", "private, no-cache"), (anonymous, "/s/runs.js", "no-cache"),
                (anonymous, "/runs/_viewer/viewer.js", "no-cache"), (anonymous, "/app/app.js", "no-cache"),
                (anonymous, "/privacy", "no-cache")]
    for client, path, value in expected:
        answer = client.get(path)
        assert answer.headers["cache-control"] == value, (path, answer.status_code, answer.headers["cache-control"])
    with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False), base_url=LOCAL) as local:
        for path, value in (("/", "no-store"), ("/api/schema", "no-store"), (f"/runs/{RUN_A}/map.html",
                            "private, no-cache"), ("/app/app.js", "no-cache")):
            answer = local.get(path)
            assert answer.status_code == 200 and answer.headers["cache-control"] == value, path


def test_web_process_keeps_off_the_workers_files(tmp_path, settings, account_settings, providers, monkeypatch,
                                                 run_people):
    """FINDALINE_ACCOUNTS_DIR moves accounts.sqlite off the data folder (default: the data folder);
    the public site serves the viewer from the code, not from runs/_viewer (which the worker
    writes); the web process never imports numba (no compiling, no numba cache)."""
    import subprocess
    anonymous = run_people["anonymous"]
    viewer = tmp_path / "runs" / "_viewer"
    viewer.mkdir(exist_ok=True)
    (viewer / "viewer.js").write_text("window.EVIL = 1;")
    with open(os.path.join(REPO, "viewer", "dist", "viewer.js"), "rb") as f:
        assert anonymous.get("/runs/_viewer/viewer.js").content == f.read()
    assert anonymous.get("/s/_viewer/viewer.js").content != b"window.EVIL = 1;"
    assert anonymous.get("/runs/_viewer/missing.js").status_code == 404

    elsewhere = tmp_path / "accounts"
    monkeypatch.setenv("FINDALINE_ACCOUNTS_DIR", str(elsewhere))
    moved = AccountSettings.load()
    data = tmp_path / "data"
    data.mkdir()
    with TestClient(_public_app(data, settings, moved, providers), base_url=URL) as client:
        assert client.get("/").status_code == 200
    assert (elsewhere / "accounts.sqlite").exists() and not (data / "accounts.sqlite").exists()
    assert ac.Accounts(str(data), account_settings).path == str(data / "accounts.sqlite")

    (tmp_path / "local").mkdir()
    code = (f"import json, sys; sys.path.insert(0, {REPO!r}); import web\n"
            "from fastapi.testclient import TestClient\n"
            f"c = TestClient(web.create_app({str(tmp_path / 'local')!r}, token='t', start_scheduler=False), "
            "base_url='http://127.0.0.1:8765')\n"
            "h = {'x-findaline-token': 't'}\n"
            "c.get('/api/schema'); c.get('/api/runs'); c.post('/api/estimate', json={'params': {'lat': 50, 'lon': 19}}, "
            "headers=h)\n"
            "c.post('/api/jobs', json={'params': {}, 'geometry': {'type': 'Polygon', 'coordinates': "
            "[[[19, 50], [19.01, 50], [19.01, 50.01], [19, 50]]]}}, headers=h)\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in ('numba', 'linesearch', 'search'))))")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []


def test_names_paths_and_profiles(run_people, clip_run, tmp_path, account_settings, capsys):
    """Display names lose control and bidi characters; the files of a run made from a local file name
    it, not its server path (run.json keeps the path for profiles); Redact takes a path after a
    colon (review B, poc_redact); profiles of a demo run from nasadem or malta2012 are for
    administrators (the server's OpenTopography key); the public worker warns without cgroup v2."""
    import jobs as jb
    import runs as rn
    assert ac.clean_name("Ann‮A​ B ") == "AnnA B" and ac.clean_name("‮") is None
    users = ac.Accounts(str(tmp_path), account_settings)
    assert users.sign_in("github", "99", "Eve‮gnp.exe").name == "Evegnp.exe"

    folder = os.path.join(REPO, "tests", "data")
    for name in ("lines.geojson", "ATTRIBUTION.txt", "data.js", "map.html"):
        text = (clip_run / name).read_text("utf-8")
        for form in (folder, folder.replace("\\", "/"), json.dumps(folder)[1:-1]):
            assert form not in text, (name, form)
    meta = json.loads((clip_run / "run.json").read_text("utf-8"))
    assert meta["source"]["description"] == "local files: zabi_kon_nmt.tif"
    assert os.path.isabs(meta["data"][0]["file"])  # profile_of_line reads it
    old = {**meta, "source": {**meta["source"], "description": f"local files: {meta['data'][0]['file']}"}}
    listed = json.dumps(rn._listed({**old, "has_map": True}))
    assert folder not in listed and json.dumps(folder)[1:-1] not in listed and "zabi_kon_nmt.tif" in listed

    redact = jb.Redact(str(tmp_path))
    assert redact("error at path:/tmp/tmpq1/x.part") == "error at path:<path>"
    assert redact("see https://a.example/b/c and http://x/y") == "see https://a.example/b/c and http://x/y"

    a, admin = run_people["a"], run_people["admin"]
    run_json = tmp_path / "runs" / RUN_B / "run.json"
    meta = json.loads(run_json.read_text("utf-8"))
    run_json.write_text(json.dumps({**meta, "source": {**meta["source"], "id": "nasadem"}}), "utf-8")
    run_people["jobs"].set_visibility(RUN_B, "demo")
    lat, lon = meta["area"]["lat"], meta["area"]["lon"]
    path = f"/api/profile?run={RUN_B}&line={lat},{lon},{lat + 0.0003},{lon}"
    answer = a.get(path, headers={"x-findaline-profile": "1"})
    assert answer.status_code == 400 and "administrators only" in answer.text
    assert admin.get(path, headers={"x-findaline-profile": "1"}).status_code == 503  # on to the (missing) worker

    capsys.readouterr()
    assert jb.warn_without_cgroup(str(tmp_path / "no-cgroup"))
    assert "no cgroup v2" in capsys.readouterr().err
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    for name, value in (("cgroup.controllers", "cpu memory"), ("memory.max", "8000000000"),
                        ("memory.current", "1000"), ("cpu.max", "400000 100000")):
        (cgroup / name).write_text(value)
    assert not jb.warn_without_cgroup(str(cgroup))


def test_disk_and_download_quota_in_the_worker(tmp_path, monkeypatch):
    """The worker checks the free disk again before each phase (also for a user without limits), counts
    the estimate of a download against the user's MB of 24 hours, and on the public site removes the
    work folder of a job that failed unexpectedly."""
    import jobs as jb
    import search
    from settings import Limits
    _phase_in_this_process(monkeypatch)
    jobs = jb.Jobs(str(tmp_path), public=True)
    clip = {"params": {**CLIP, "northing": 254608, "easting": 553966, "radius": 150}, "geometry": None}

    def phase(job, name, limits):
        jobs.claim("queued" if name == "download" else "ready", jb.PHASE_STATUS[name], "w1")
        jb.run_phase(str(tmp_path), job, name, limits, True)
        return jobs.get(job)

    class Disk:
        free = 5e9
    real = jb.shutil.disk_usage
    monkeypatch.setattr(jb.shutil, "disk_usage", lambda path: Disk())
    short = Limits(free_disk_gb_min=20)
    for unlimited in (False, True):
        job = jobs.add(clip, "short of disk", user_id=7, unlimited=unlimited)
        result = phase(job, "download", short)
        assert result["status"] == "failed" and "disk space" in result["error"], result
    job = jobs.add(clip, "ready", user_id=7)
    monkeypatch.setattr(jb.shutil, "disk_usage", real)
    assert phase(job, "download", Limits())["status"] == "ready"
    monkeypatch.setattr(jb.shutil, "disk_usage", lambda path: Disk())
    result = phase(job, "compute", short)
    assert result["status"] == "failed" and result["error"] == messages.text("error.disk_full_search")
    assert json.loads(result["error_info"]) == {"code": "disk_full_search", "params": {}}
    monkeypatch.setattr(jb.shutil, "disk_usage", real)

    monkeypatch.setattr(search, "estimate_search", lambda plan: search.Estimate(download_mb=600, work_gb=0))
    quota = Limits(download_mb_day_max=1000)
    first, second = jobs.add(clip, "first", user_id=8), jobs.add(clip, "second", user_id=8)
    assert phase(first, "download", quota)["status"] == "ready"
    result = phase(second, "download", quota)
    assert result["status"] == "failed" and "MB of data: you used 600 of 1000 MB" in result["error"], result
    assert phase(jobs.add(clip, "another user", user_id=9), "download", quota)["status"] == "ready"

    broken = jobs.add({"geometry": None}, "broken", user_id=8)  # no params: an unexpected KeyError
    jobs.claim("queued", "downloading", "w1")
    with pytest.raises(KeyError):
        jb.run_phase(str(tmp_path), broken, "download", None, True)
    assert jobs.get(broken)["error"] == f"internal error, ref {broken}"
    assert not os.path.exists(jobs.folder(broken))


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
    for params in ({**point, "max_length": 2000, "slope_threshold": 30},
                   {**point, "resolution": 5, "group_distance": 10}, {**point, "max_length": "1500"}):
        web.check_request({"params": params}, limits)
    # The download fields are not allowed at all; a site that allows them again still has their bounds
    reopened = limits.model_copy(update={"not_allowed": []})
    with pytest.raises(ValueError, match="ortho_detail: at most 50"):
        web.check_request({"params": {**point, "ortho_detail": 51}}, reopened)
    web.check_request({"params": {**point, "ortho_detail": 50, "ortho": "always", "source": "geoportal"}}, reopened)
    with pytest.raises(ValueError) as error:
        web.check_request({"params": {**point, "max_length": 2500, "prominence_span": 1500}}, limits)
    assert "max_length: at most 2000" in str(error.value) and "prominence_span: at most 1000" in str(error.value)
    with pytest.raises(ValueError) as error:
        web.check_request({"params": {**point, "threads": 2, "update": "stale"}}, limits)
    assert "threads: not allowed" in str(error.value) and "update: not allowed" in str(error.value)

    fields = {f["name"]: f for f in web.schema(limits)}
    assert not {"threads", "tile_size", "update", "source", "water", "shore_buffer", "fill_gaps", "coastline",
                "ortho", "ortho_detail"} & set(fields)
    assert (fields["max_length"]["max"], fields["slope_threshold"]["min"], fields["group_distance"]["min"]) == (2000, 30, 2)
    # The page says the bounds in the hint (hint.bounds.*), the help is the catalogue's text alone
    assert fields["max_length"]["help"] == messages.text("field.max_length.help")
    assert fields["group_distance"]["min_factor"] == 2 and fields["max_length"]["min_factor"] is None
    local = {f["name"]: f for f in web.schema()}
    assert local["max_length"]["max"] is None and "threads" in local

    with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False, limits=limits), base_url=LOCAL) as client:
        headers = {"x-findaline-token": TOKEN}
        response = client.post("/api/jobs", json={"params": {**point, "max_length": 2500}}, headers=headers)
        assert response.status_code == 400 and "at most 2000" in response.text
        assert client.post("/api/estimate", json={"params": {**point, "threads": 2}}, headers=headers).status_code == 400
        names = {f["name"] for f in client.get("/api/schema").json()["fields"]}
        assert "threads" not in names
    with TestClient(web.create_app(str(tmp_path), token=TOKEN, start_scheduler=False), base_url=LOCAL) as client:
        assert "threads" in {f["name"] for f in client.get("/api/schema").json()["fields"]}  # locally all


# Numbers pydantic reads (the worker would use 5000) that Python's float() refuses (review A, 1)
SPELLINGS = ["5_e3", "5e_3", "+_5000", "5_000_.", "._5e4"]
POINT = {"lat": 50.1, "lon": 19.7, "radius": 100}


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_request_limits_on_validated_values(tmp_path, settings, account_settings, providers, spelling):
    """The bounds of the request values are checked on the values pydantic read, on /api/jobs and
    /api/estimate: no spelling of a number skips them, a resolution spelled "1_e0" does not skip the
    other bounds, and a value pydantic cannot read is refused."""
    app = _public_app(tmp_path, settings, account_settings, providers)
    a = _clients(app, tmp_path, account_settings, ("a",))["a"]
    for path in ("/api/jobs", "/api/estimate"):
        answer = a.post(path, json={"params": {**POINT, "max_length": spelling}})
        assert answer.status_code == 400 and answer.json()["detail"] == {
            "message": "Longest line: at most 2000 m on this site.", "code": "at_most",
            "params": {"field": "max_length", "max": "2000 m"}}, (path, answer.text)
        answer = a.post(path, json={"params": {**POINT, "resolution": "1_e0", "slope_threshold": 1}})
        assert answer.status_code == 400 and answer.json()["detail"]["message"] ==             "Anchor steepness: at least 30° on this site.", (path, answer.text)
        answer = a.post(path, json={"params": {**POINT, "max_length": "5,000"}})  # pydantic cannot read it
        assert answer.status_code == 400 and answer.json()["detail"] == {
            "message": "Longest line: enter a number.", "code": "not_a_number",
            "params": {"field": "max_length"}}, answer.text
    assert app.state.jobs.all() == []
    a.close()


def _phase_in_this_process(monkeypatch):
    """jobs.run_phase called in the test's process: what it changes there (stdout, the progress
    listeners and text interval, the Nominatim hook) is put back after the test."""
    import osm
    import progress
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(progress, "TEXT_INTERVAL", progress.TEXT_INTERVAL)
    monkeypatch.setattr(progress, "_listeners", list(progress._listeners))
    monkeypatch.setattr(osm, "BEFORE_REQUEST", osm.BEFORE_REQUEST)


def test_worker_checks_the_request_limits(tmp_path, monkeypatch):
    """A request that skipped the web's check (written to the queue directly) is refused by the
    worker before any request to a service: plan_search(limits=) and the download phase."""
    import requests

    import jobs as jb
    import search
    from settings import Limits

    def no_network(*args, **kwargs):
        raise AssertionError("a request was sent")
    monkeypatch.setattr(requests.Session, "request", no_network)
    limits = Limits.load(public=True)
    params = jb.search_params({"params": {**POINT, "max_length": "5_e3", "resolution": "1_e0"}})
    with pytest.raises(search.SearchError) as error:
        search.plan_search(params, search.Paths(str(tmp_path), str(tmp_path / "work")), lambda k, t: None,
                           limits=limits)
    assert "max_length: at most 2000" in str(error.value)
    jobs = jb.Jobs(str(tmp_path), public=True)
    job = jobs.add({"params": {**POINT, "drop_radius": "5_e3"}, "geometry": None}, "skipped the web", user_id=7)
    jobs.claim("queued", "downloading", "w1")
    _phase_in_this_process(monkeypatch)
    jb.run_phase(str(tmp_path), job, "download", limits, True)
    assert jobs.get(job)["status"] == "failed" and "Drop: measured within: at most 1000 m" in jobs.get(job)["error"]


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
    assert "group_distance: at least 10" in str(error.value)
    plan = _plan(tmp_path, Limits(grid_mpx_max=3), geometry=SQUARE, max_length=150)
    assert plan.grid.width * plan.grid.height <= 3e6
    plan.limits = Limits(download_mb_max=3000)
    with pytest.raises(search.SearchError) as error:
        search.check_estimate(plan, search.Estimate(download_mb=5000, work_gb=1))
    plan.limits = Limits(download_mb_max=6000)
    search.check_estimate(plan, search.Estimate(download_mb=5000, work_gb=1))


def _square(lon, lat, d=0.01):
    return [[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]


# Review B: two 1 km² squares about 560 km apart (Gdańsk, Kraków), and a circle of 100 km at 40 m
FAR_APART = {"type": "MultiPolygon", "coordinates": [[_square(18.6, 54.35)], [_square(20.0, 49.3)]]}
HUGE_CIRCLE = {"lat": 52.0, "lon": 19.0, "radius": 100_000, "resolution": 40, "source": "geoportal"}


def test_bounding_box_limits_before_requests(tmp_path, monkeypatch, settings, account_settings, providers):
    """Under the public limits a huge circle (radius, resolution, area) and two small areas far apart
    (the bounding box) are refused by the web, by plan_search, by the quick lane's estimate and by
    the download phase, before a single request (each would have asked the sheet index and
    Overpass for every 0.25° tile of the box); without limits they are planned as before."""
    import requests

    import jobs as jb
    import search
    from settings import Limits
    sent = []

    def no_network(self, method, url, *args, **kwargs):
        sent.append(url)
        raise AssertionError(f"a request was sent: {url}")
    monkeypatch.setattr(requests.Session, "request", no_network)
    limits = Limits.load(public=True)
    assert (limits.radius_max, limits.resolution_max, limits.bbox_km_max) == (3500, 30, 75)
    far = {"params": {"resolution": 30, "source": "geoportal"}, "geometry": FAR_APART}
    huge = {"params": HUGE_CIRCLE, "geometry": None}

    def without_source(request):  # the web refuses the field itself (not_allowed); here the bounds are checked
        return {**request, "params": {k: v for k, v in request["params"].items() if k != "source"}}
    with pytest.raises(ValueError) as error:
        web.check_request(without_source(huge), limits)
    assert "radius: at most 3500" in str(error.value) and "resolution: at most 30" in str(error.value)
    web.check_request(without_source(far), limits)  # its values pass: its box is the plan's to refuse
    paths = search.Paths(str(tmp_path), str(tmp_path / "work"))
    for request, text in ((huge, "radius: at most 3500"), (far, "bounding box"),
                          ({"params": {**HUGE_CIRCLE, "radius": 3500, "resolution": 30}}, None)):
        params = jb.search_params(request)
        if text is None:  # within the bounds: planned (its estimate asks the index)
            search.plan_search(params, paths, lambda k, t: None, limits=limits)
            continue
        with pytest.raises(search.SearchError) as error:
            search.plan_search(params, paths, lambda k, t: None, limits=limits)
        assert text in str(error.value), error.value
        with pytest.raises(search.SearchError):
            jb.quick_estimate(str(tmp_path), request, limits)
    with pytest.raises(search.SearchError) as error:  # the area limit applies to circles too
        search.plan_search(jb.search_params({"params": {**HUGE_CIRCLE, "radius": 3000, "resolution": 30}}), paths,
                           lambda k, t: None, limits=limits.model_copy(update={"area_km2_max": 20}))
    assert "The circle covers 28.3 km², at most 20 km²" in str(error.value)
    jobs = jb.Jobs(str(tmp_path), public=True)
    _phase_in_this_process(monkeypatch)
    for request in (far, huge):
        job = jobs.add(request, "far apart", user_id=7)
        jobs.claim("queued", "downloading", "w1")
        jb.run_phase(str(tmp_path), job, "download", limits, True)
        assert jobs.get(job)["status"] == "failed", jobs.get(job)
    app = _public_app(tmp_path, settings, account_settings, providers)
    a = _clients(app, tmp_path, account_settings, ("a",))["a"]
    for path in ("/api/jobs", "/api/estimate"):
        answer = a.post(path, json=without_source(huge))
        assert answer.status_code == 400 and "radius: at most 3500" in answer.text
    fields = {f["name"]: f for f in a.get("/api/schema").json()["fields"]}
    assert fields["radius"]["max"] == 3500 and fields["resolution"]["max"] == 30
    a.close()
    assert sent == []
    plan = search.plan_search(jb.search_params(far), paths, lambda k, t: None, limits=Limits())  # locally
    assert plan.grid.height * plan.resolution > 500_000


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


def test_usage(tmp_path, settings, account_settings, providers, monkeypatch):
    """GET /api/usage: a limited user's use of the last 24 hours (km², downloads, Nominatim, searches),
    the searches waiting or running and the size of the runs, each with its quota, and the limits of one
    search (area, radius, longest line, bounding box); users without limits
    and administrators get the counts of jobs and runs without quotas; the local app (no
    FINDALINE_LIMIT_*, or with them: its one user has no quotas) the same. Nothing is recorded."""
    from settings import Limits
    limits = Limits.load(public=True)
    app = _public_app(tmp_path, settings, account_settings, providers, limits=limits)
    people = _clients(app, tmp_path, account_settings, ("a", "unlimited", "admin"))
    jobs = app.state.jobs
    a = people["a"]
    point = {"params": {"lat": 50.1, "lon": 19.7, "radius": 100}, "geometry": None}
    assert a.post("/api/jobs", json=point).status_code == 200
    jobs.use(a.user_id, "km2", 3.5, limits.km2_day_max)
    jobs.use(a.user_id, "download_mb", 12, limits.download_mb_day_max)
    jobs.add_run("run-a", a.user_id, int(2e8))
    import sqlite3

    def audit_rows() -> int:
        db = sqlite3.connect(tmp_path / "audit.sqlite")
        try:
            return db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        finally:
            db.close()
    audited = audit_rows()
    answer = a.get("/api/usage")
    assert answer.status_code == 200
    usage = answer.json()
    assert usage["limited"] and usage["hours"] == 24
    assert usage["usage"]["km2"] == {"used": 3.5, "limit": limits.km2_day_max}
    assert usage["usage"]["download_mb"] == {"used": 12, "limit": limits.download_mb_day_max}
    assert usage["usage"]["nominatim"] == {"used": 0, "limit": limits.nominatim_day_max}
    assert usage["usage"]["jobs_day"] == {"used": 1, "limit": limits.jobs_day_max}
    assert usage["usage"]["jobs_active"] == {"used": 1, "limit": limits.jobs_active_max}
    assert usage["usage"]["runs_gb"] == {"used": 0.2, "limit": limits.runs_gb_max}
    assert usage["search"] == {"area_km2": limits.area_km2_max, "radius_m": limits.radius_max,
                               "max_length_m": limits.max_length_max, "bbox_km": limits.bbox_km_max}
    assert audit_rows() == audited  # read-only: no audit event
    for who in ("unlimited", "admin"):
        usage = people[who].get("/api/usage").json()
        assert not usage["limited"], who
        assert all(item["limit"] is None for item in usage["usage"].values()), usage
        assert usage["usage"]["km2"]["used"] is None and usage["usage"]["jobs_active"]["used"] == 0
        assert all(value is None for value in usage["search"].values()), usage
    anonymous = TestClient(app, base_url=URL)
    assert anonymous.get("/api/usage").status_code == 401
    for client in [*people.values(), anonymous]:
        client.close()
    for env in ({}, {"FINDALINE_LIMIT_KM2_DAY_MAX": "10"}):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        local = tmp_path / ("local" + str(len(env)))
        local.mkdir()
        with TestClient(web.create_app(str(local), token=TOKEN, start_scheduler=False), base_url=LOCAL) as client:
            assert client.post("/api/jobs", json=point, headers={"x-findaline-token": TOKEN}).status_code == 200
            usage = client.get("/api/usage").json()
        assert not usage["limited"] and usage["usage"]["jobs_active"] == {"used": 1, "limit": None}, usage
        assert usage["usage"]["km2"] == {"used": None, "limit": None}
        assert all(value is None for value in usage["search"].values()), usage
