"""
Settings and limits of the web app, from environment variables (pydantic-settings).

- Limits: each field is read from FINDALINE_LIMIT_<NAME> (e.g. FINDALINE_LIMIT_COMPUTE_SECONDS=7200).
  Nothing set means no limit: the local app (start.bat, `findaline.py web`) runs as before.
  The public site (Limits.load(public=True)) starts from the values of PUBLIC instead, each
  overridable the same way (`none`: no limit). The fields: time limits of the worker
  (jobs.Scheduler), how long its housekeeping keeps old events, and limits on the values
  of a request (check_request, also the min/max of the form: web.schema). A user without
  limits (accounts, later) gets None instead of a Limits.
- PublicSettings: what the public site needs (`findaline.py web --public`, `worker --public`):
  FINDALINE_PUBLIC_URL, FINDALINE_TRUSTED_PROXY, FINDALINE_CONTACT, also read from Docker
  secrets (/run/secrets/findaline_<name>) when that folder exists.
"""

import ipaddress
import os
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRETS_DIR = "/run/secrets"  # Docker secrets
# The limits of the public site (--public) unless FINDALINE_LIMIT_<NAME> says otherwise
PUBLIC = {
    "buffer_max": 500, "max_length_max": 2000, "prominence_span_max": 1000, "drop_radius_max": 1000,
    "slope_threshold_min": 30, "group_distance_min_res": 2, "ortho_detail_max": 50,
    "not_allowed": ["ortho=always", "update", "coastline=world", "threads", "tile_size"],
}
# Limits on request values: field of Limits -> (parameter of SearchParams, "min" or "max")
BOUNDS = {"buffer_max": ("buffer", "max"), "max_length_max": ("max_length", "max"),
          "prominence_span_max": ("prominence_span", "max"), "drop_radius_max": ("drop_radius", "max"),
          "slope_threshold_min": ("slope_threshold", "min"), "ortho_detail_max": ("ortho_detail", "max")}


class Limits(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FINDALINE_LIMIT_", extra="ignore", env_parse_none_str="none")

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


    # Values of a request (web.check_request; the form gets them as min/max)
    buffer_max: Optional[float] = Field(None, gt=0, description="largest --buffer, m")
    max_length_max: Optional[float] = Field(None, gt=0, description="largest --max-length, m")
    prominence_span_max: Optional[float] = Field(None, gt=0, description="largest --prominence-span, m")
    drop_radius_max: Optional[float] = Field(None, gt=0, description="largest --drop-radius, m")
    slope_threshold_min: Optional[float] = Field(None, ge=0, description="smallest --slope-threshold, degrees")
    group_distance_min_res: Optional[float] = Field(
        None, ge=0, description="smallest --group-distance, in multiples of the resolution")
    ortho_detail_max: Optional[int] = Field(None, ge=0, description="largest --ortho-detail")
    not_allowed: List[str] = Field(
        default_factory=list, description="parameters the web may not set (\"threads\"), or values of them "
                                          "(\"ortho=always\"); a JSON list")

    @classmethod
    def load(cls, public: bool = False) -> "Limits":
        """The limits from FINDALINE_LIMIT_*; for the public site those not set there are PUBLIC's."""
        env = cls()
        return cls(**{**(PUBLIC if public else {}), **env.model_dump(include=env.model_fields_set)})

    def phase_seconds(self, phase: str) -> Optional[float]:
        """The time limit of a phase of a job ("download" or "compute")."""
        return {"download": self.download_seconds, "compute": self.compute_seconds}[phase]

    def allows(self, name: str, value=None) -> bool:
        """Whether the web may set a parameter (to a value)."""
        return name not in self.not_allowed and (value is None or f"{name}={value}" not in self.not_allowed)

    def bounds(self, name: str, resolution: float = 1.0) -> Tuple[Optional[float], Optional[float]]:
        """(smallest, largest) value of a parameter (None: no limit); --group-distance at the given
        resolution (the finest one when it is not known yet)."""
        low = high = None
        for field, (param, side) in BOUNDS.items():
            value = getattr(self, field)
            if param == name and value is not None:
                low, high = (value, high) if side == "min" else (low, value)
        if name == "group_distance" and self.group_distance_min_res is not None:
            low = self.group_distance_min_res * resolution
        return low, high

    def check_request(self, params: Dict, min_resolution: float = 1.0):
        """Refuse values of a request over the limits (ValueError naming each). Runs before the
        parameters are validated: a value that is no number is left to SearchParams."""
        problems = []
        resolution = params.get("resolution")
        for name, value in params.items():
            if value is None:
                continue
            if not self.allows(name, value):
                problems.append(f"{name if not self.allows(name) else f'{name}={value}'}: not allowed on this site")
                continue
            try:
                number = float(value)
                res = max(float(resolution), min_resolution) if resolution is not None else min_resolution
            except (TypeError, ValueError):
                continue
            low, high = self.bounds(name, res)
            if low is not None and number < low:
                problems.append(f"{name}: at least {low:g} on this site")
            if high is not None and number > high:
                problems.append(f"{name}: at most {high:g} on this site")
        if problems:
            raise ValueError("; ".join(problems))


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
