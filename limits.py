"""
Settings and limits of the web app, from environment variables (pydantic-settings).

- Limits: each field is read from FINDALINE_LIMIT_<NAME> (e.g. FINDALINE_LIMIT_COMPUTE_SECONDS=7200).
  Nothing set means no limit: the local app (start.bat, `findaline.py web`) runs as before;
  the public site sets them for its worker. For now these are the time limits of the
  worker (jobs.Scheduler) and how long its housekeeping keeps old events; the limits on
  request values come with the public mode.
- PublicSettings: what the public site needs (`findaline.py web --public`, `worker --public`):
  FINDALINE_PUBLIC_URL, FINDALINE_TRUSTED_PROXY, FINDALINE_CONTACT, also read from Docker
  secrets (/run/secrets/findaline_<name>) when that folder exists.
"""

import ipaddress
import os
from typing import Optional
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRETS_DIR = "/run/secrets"  # Docker secrets


class Limits(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FINDALINE_LIMIT_", extra="ignore")

    download_seconds: Optional[float] = Field(
        None, gt=0, description="time limit of the download phase of a job (its processes are killed, the "
                                "job fails with \"time limit\")")
    compute_seconds: Optional[float] = Field(
        None, gt=0, description="time limit of the computing phase of a job")
    quick_seconds: Optional[float] = Field(
        None, gt=0, description="time limit of an estimate or a profile (the quick lane's process is killed "
                                "and started again)")
    keep_events_days: Optional[float] = Field(
        90, gt=0, description="days the events (messages, progress) of a finished job are kept; the job "
                              "itself stays in the queue")

    def phase_seconds(self, phase: str) -> Optional[float]:
        """The time limit of a phase of a job ("download" or "compute")."""
        return {"download": self.download_seconds, "compute": self.compute_seconds}[phase]


class PublicSettings(BaseSettings):
    """The settings of the public site; `--public` refuses to start without them."""
    model_config = SettingsConfigDict(env_prefix="FINDALINE_", extra="ignore")

    public_url: str = Field(description="address of the site, https://... (http:// only on localhost, for tests)")
    trusted_proxy: str = Field(description="address of the reverse proxy (SWAG) whose X-Forwarded-* headers "
                                           "are trusted: IP addresses or networks, comma-separated")
    contact: str = Field(description="e-mail or URL of the operator, sent in the User-Agent of the requests to "
                                     "Nominatim, Overpass and the data services")
    concurrency: int = Field(400, gt=0, description="connections and requests the server handles at once (open "
                                                    "event streams count); more are answered with 503")

    @field_validator("public_url")
    @classmethod
    def check_url(cls, value: str) -> str:
        parts = urlsplit(value)
        local = parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")
        if not parts.hostname or (parts.scheme != "https" and not local):
            raise ValueError("https://<host> expected (http:// only on localhost, for tests)")
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            raise ValueError("the address of the site only: https://<host>[:port]")
        return f"{parts.scheme}://{parts.netloc.lower()}"

    @field_validator("trusted_proxy")
    @classmethod
    def check_proxy(cls, value: str) -> str:
        items = [item.strip() for item in value.split(",") if item.strip()]
        if not items:
            raise ValueError("the address of the reverse proxy is needed")
        for item in items:
            ipaddress.ip_network(item, strict=False)  # no "*": not every client may set X-Forwarded-For
        return ",".join(items)

    @field_validator("contact")
    @classmethod
    def check_contact(cls, value: str) -> str:
        value = value.strip()
        if not value or not all(" " <= c <= "~" for c in value):
            raise ValueError("printable ASCII expected (it goes into a header)")
        return value

    @property
    def origin(self) -> str:
        """scheme://host[:port] of the site, what browsers send as Origin."""
        return self.public_url

    @property
    def host(self) -> str:
        """The host name of the site (the Host header without the port)."""
        return urlsplit(self.public_url).hostname

    @classmethod
    def load(cls, secrets_dir: str = SECRETS_DIR) -> "PublicSettings":
        """From the environment and, when the folder exists, Docker secrets (no warning without
        it); SystemExit naming what is missing or wrong (`--public` does not start then)."""
        from pydantic import ValidationError
        try:
            return cls(_secrets_dir=secrets_dir if os.path.isdir(secrets_dir) else None)
        except ValidationError as e:
            raise SystemExit("--public needs these settings (environment or Docker secrets):\n"
                             + describe_error(e, "FINDALINE_"))


def describe_error(error, prefix: str) -> str:
    """The missing or wrong settings of a pydantic ValidationError, one per line."""
    lines = []
    for e in error.errors():
        name = prefix + "_".join(map(str, e["loc"])).upper()
        lines.append(f"{name}: {'missing' if e['type'] == 'missing' else e['msg']}")
    return "\n".join(lines)
