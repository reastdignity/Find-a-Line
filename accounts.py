"""
Accounts of the public site (`findaline.py web --public`): sign-in with GitHub or Google,
the users and their sessions (accounts.sqlite in the data folder). The local app has no
accounts: its one user is LOCAL, an administrator.

- Sign-in: OAuth 2 through Authlib's Starlette client. The state, the OpenID nonce and the
  PKCE verifier (S256, both providers) of a sign-in in progress live in a signed cookie of
  Starlette's SessionMiddleware (OAUTH_COOKIE, ten minutes), which binds the callback to
  the browser that started it (no login CSRF). Authlib's cache is not configured. Google
  is OpenID Connect (server metadata; Authlib checks the ID token: signature from the JWKS,
  iss, aud, exp, nonce), GitHub plain OAuth 2 (`GET /user` gives the id). The callback
  address comes from FINDALINE_PUBLIC_URL, never from the request.
- A user is (provider, its id: Google's `sub`, GitHub's numeric id), no e-mail. New accounts
  are `pending` until an administrator approves them (FINDALINE_SIGNUP=approval), approved
  at once (open), or refused (closed). Administrators are FINDALINE_ADMINS (github:12345):
  always approved, never blocked. An administrator can lift the limits of the site for a
  user (users.unlimited) without making them an administrator.
- Sessions are our own, so they can be revoked: a random token of SESSION_BYTES in the
  cookie SESSION_COOKIE (Secure, HttpOnly, SameSite=Lax), only its sha256 in the database;
  they end after SESSION_DAYS, or IDLE_DAYS without a request; every sign-in makes a new
  one. Each session has its CSRF token, which the page sends as X-Findaline-Token.
"""

import hashlib
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import cache
from settings import PROVIDERS, AccountSettings

SESSION_COOKIE = "__Host-findaline"        # our session (the __Host- prefix: Secure, path /, no Domain)
OAUTH_COOKIE = "__Host-findaline-oauth"    # a sign-in in progress (SessionMiddleware)
OAUTH_SECONDS = 600                        # a sign-in must end within this time
SESSION_BYTES = 32
SESSION_DAYS = 30                          # a session ends after this time...
IDLE_DAYS = 7                              # ...or this long without a request
SEEN_EVERY = 60                            # seconds between updates of a session's last request
LOGIN_RATE = "20/minute;100/hour"          # sign-in requests (/login, /auth) per IP address
STATUSES = ("pending", "approved", "blocked")
# The schema of accounts.sqlite, one step per version (cache.migrate; never change a released step)
MIGRATIONS = [
    """CREATE TABLE users (
           id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, subject TEXT NOT NULL,
           name TEXT, status TEXT NOT NULL, created TEXT, last_login TEXT, UNIQUE (provider, subject));
       CREATE TABLE sessions (
           hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id), csrf TEXT NOT NULL,
           created REAL NOT NULL, seen REAL NOT NULL);
       CREATE INDEX sessions_user ON sessions (user_id);""",
    # 2: users without limits (set by an administrator; settings.Limits does not apply to them)
    """ALTER TABLE users ADD COLUMN unlimited INTEGER NOT NULL DEFAULT 0;""",
]
# The providers: Authlib's registration and how to read the user's id and name
GITHUB = {"authorize_url": "https://github.com/login/oauth/authorize",
          "access_token_url": "https://github.com/login/oauth/access_token",
          "api_base_url": "https://api.github.com/"}
GOOGLE = {"server_metadata_url": "https://accounts.google.com/.well-known/openid-configuration"}
SCOPES = {"github": None, "google": "openid profile"}  # GitHub: no scope (public data only)
NAMES = {"github": "GitHub", "google": "Google"}


@dataclass(frozen=True)
class User:
    id: Optional[int]   # None: the local app's user
    provider: str
    subject: str
    name: str
    status: str         # pending, approved, blocked
    admin: bool
    unlimited: bool = False  # without the limits of the site (set by an administrator)

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    @property
    def limited(self) -> bool:
        """Whether the limits of the site (request values, search, quotas, rates) apply: not to users
        without limits and administrators (the safeguards of the server apply to everyone)."""
        return not (self.admin or self.unlimited)

    def public(self) -> Dict:
        """What the page may know about its user."""
        return {"name": self.name, "provider": self.provider, "status": self.status, "admin": self.admin,
                "unlimited": self.unlimited}


LOCAL = User(None, "local", "", "local", "approved", True)


def digest(token: str) -> str:
    """sha256 of a token (sessions and share links are stored as it)."""
    return hashlib.sha256(token.encode()).hexdigest()


class SignupClosed(Exception):
    """FINDALINE_SIGNUP=closed and the account is new."""


class Accounts:
    """The users and sessions of the public site (accounts.sqlite)."""

    def __init__(self, data_dir: str, settings: AccountSettings):
        self.settings = settings
        self.admins = set(settings.admin_ids)
        self.path = os.path.join(data_dir, "accounts.sqlite")
        db = self._db()
        try:
            cache.migrate(db, MIGRATIONS)
        finally:
            db.close()

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=60)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = sqlite3.Row
        return db

    def _user(self, row) -> User:
        admin = (row["provider"], row["subject"]) in self.admins
        return User(row["id"], row["provider"], row["subject"], row["name"] or f"{row['provider']}:{row['subject']}",
                    "approved" if admin else row["status"], admin, bool(row["unlimited"]))

    def user(self, user_id: int) -> Optional[User]:
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._user(row) if row else None

    def users(self) -> List[User]:
        with self._db() as db:
            return [self._user(r) for r in db.execute("SELECT * FROM users ORDER BY id").fetchall()]

    def ensure_user(self, provider: str, subject: str) -> int:
        """The id of a user, made (approved, without a name) if it has never signed in."""
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO users (provider, subject, status, created) VALUES (?, ?, 'approved', ?)",
                       (provider, subject, datetime.now().isoformat(timespec="seconds")))
            return db.execute("SELECT id FROM users WHERE provider = ? AND subject = ?",
                              (provider, subject)).fetchone()[0]

    def first_admin(self) -> Optional[int]:
        """The id of the first administrator of FINDALINE_ADMINS (made if it has never signed in), who
        owns the runs from before the public mode; None without administrators."""
        ids = self.settings.admin_ids
        return self.ensure_user(*ids[0]) if ids else None

    def sign_in(self, provider: str, subject: str, name: Optional[str]) -> User:
        """The user of a provider's identity after a sign-in: a new one is pending, approved or
        refused (SignupClosed) by FINDALINE_SIGNUP; administrators are always approved."""
        if provider not in PROVIDERS or not subject:
            raise ValueError("unknown identity")
        now = datetime.now().isoformat(timespec="seconds")
        admin = (provider, subject) in self.admins
        name = (name or "").strip()[:100] or None
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE provider = ? AND subject = ?", (provider, subject)).fetchone()
            if row is None:
                if self.settings.signup == "closed" and not admin:
                    raise SignupClosed()
                status = "approved" if admin or self.settings.signup == "open" else "pending"
                db.execute("INSERT INTO users (provider, subject, name, status, created, last_login) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (provider, subject, name, status, now, now))
            else:
                db.execute("UPDATE users SET name = COALESCE(?, name), last_login = ? WHERE id = ?",
                           (name, now, row["id"]))
            row = db.execute("SELECT * FROM users WHERE provider = ? AND subject = ?", (provider, subject)).fetchone()
        return self._user(row)

    def set_status(self, user_id: int, status: str):
        """Approve or block a user (the admin tab); blocking ends all its sessions."""
        if status not in STATUSES:
            raise ValueError(status)
        with self._db() as db:
            db.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
        if status == "blocked":
            self.revoke(user_id)

    def set_unlimited(self, user_id: int, unlimited: bool):
        """Lift the limits of the site for a user, or set them again (the admin tab)."""
        with self._db() as db:
            db.execute("UPDATE users SET unlimited = ? WHERE id = ?", (int(bool(unlimited)), user_id))

    # --- Sessions ---

    def new_session(self, user_id: int) -> Tuple[str, str]:
        """A new session of a user: (its token for the cookie, its CSRF token)."""
        token, csrf, now = secrets.token_urlsafe(SESSION_BYTES), secrets.token_urlsafe(24), time.time()
        with self._db() as db:
            db.execute("INSERT INTO sessions (hash, user_id, csrf, created, seen) VALUES (?, ?, ?, ?, ?)",
                       (digest(token), user_id, csrf, now, now))
        return token, csrf

    def session(self, token: Optional[str]) -> Optional[Tuple[User, str]]:
        """(user, CSRF token) of a session cookie; None when it is unknown, expired or idle too long."""
        if not token or len(token) > 100:
            return None
        now = time.time()
        with self._db() as db:
            row = db.execute("SELECT s.csrf, s.created AS s_created, s.seen AS s_seen, s.hash, u.* FROM sessions s JOIN users u ON "
                             "u.id = s.user_id WHERE s.hash = ?", (digest(token),)).fetchone()
            if row is None:
                return None
            if now - row["s_created"] > SESSION_DAYS * 86400 or now - row["s_seen"] > IDLE_DAYS * 86400:
                db.execute("DELETE FROM sessions WHERE hash = ?", (row["hash"],))
                return None
            if now - row["s_seen"] > SEEN_EVERY:
                db.execute("UPDATE sessions SET seen = ? WHERE hash = ?", (now, row["hash"]))
        return self._user(row), row["csrf"]

    def end_session(self, token: Optional[str]):
        if token:
            with self._db() as db:
                db.execute("DELETE FROM sessions WHERE hash = ?", (digest(token),))

    def revoke(self, user_id: int):
        """End every session of a user."""
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def forget_sessions(self) -> int:
        """Delete expired and idle sessions."""
        now = time.time()
        with self._db() as db:
            return db.execute("DELETE FROM sessions WHERE created < ? OR seen < ?",
                              (now - SESSION_DAYS * 86400, now - IDLE_DAYS * 86400)).rowcount


def oauth_clients(settings: AccountSettings, transport=None):
    """Authlib's registry with the configured providers (`transport`: an httpx2 transport for tests)."""
    from authlib.integrations.starlette_client import OAuth
    oauth = OAuth()  # no cache: the state stays in the signed cookie (Authlib 1.6.11 fixed CSRF with a cache)
    extra = {"timeout": 20, **({"transport": transport} if transport is not None else {})}
    for provider in settings.providers:
        client_id, secret = settings.client(provider)
        kwargs = {"code_challenge_method": "S256", **extra}
        if SCOPES[provider]:
            kwargs["scope"] = SCOPES[provider]
        oauth.register(provider, client_id=client_id, client_secret=secret, client_kwargs=kwargs,
                       **(GITHUB if provider == "github" else GOOGLE))
    return oauth


async def identity(client, provider: str, token: Dict) -> Tuple[str, Optional[str]]:
    """(id, display name) of the user who signed in, from the token Authlib fetched."""
    if provider == "google":
        info = token.get("userinfo")  # the ID token, checked by Authlib (nonce, iss, aud, exp, signature)
        if not info or not info.get("sub"):
            raise ValueError("no ID token")
        return str(info["sub"]), info.get("name")
    response = await client.get("user", token=token)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data.get("id"), int):
        raise ValueError("no GitHub user id")
    return str(data["id"]), data.get("login")


def local_path(value: Optional[str]) -> str:
    """`next` of a sign-in: a path of this site only (not //host, not \\host, no scheme), else "/"."""
    if (not value or not value.startswith("/") or value.startswith("//") or "\\" in value or len(value) > 500
            or any(c < " " or c == "\x7f" for c in value)):
        return "/"
    return value
