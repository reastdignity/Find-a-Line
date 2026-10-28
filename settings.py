"""
Settings and limits of the web app, from environment variables (pydantic-settings).

- Limits: each field is read from FINDALINE_LIMIT_<NAME> (e.g. FINDALINE_LIMIT_COMPUTE_SECONDS=7200).
  Nothing set means no limit: the local app (start.bat, `findaline.py web`) runs as before.
  The public site (Limits.load(public=True)) starts from the values of PUBLIC instead, each
  overridable the same way (`none`: no limit). The fields: time limits of the worker
  (jobs.Scheduler), how long its housekeeping keeps old events, and limits on the values
  of a request (check_request, also the min/max of the form: web.schema), the cost of a
  search, the quotas and request rates of a user, and the safeguards of the server. Users
  without limits and administrators (accounts.User.limited) get None instead of a Limits
  for the request values, the search, the quotas and the rates; the safeguards (queue_max,
  free_disk_gb_min) and the time limits of the worker apply to everyone.
- PublicSettings: what the public site needs (`findaline.py web --public`, `worker --public`):
  FINDALINE_PUBLIC_URL, FINDALINE_TRUSTED_PROXY, FINDALINE_CONTACT, also read from Docker
  secrets (/run/secrets/findaline_<name>) when that folder exists.
- AccountSettings: the sign-in of the public site (`web --public` only; accounts.py):
  FINDALINE_SESSION_SECRET, FINDALINE_GITHUB_CLIENT_ID / _SECRET, FINDALINE_GOOGLE_CLIENT_ID /
  _SECRET, FINDALINE_ADMINS, FINDALINE_SIGNUP; secrets from the same Docker secrets folder
  (file names as the variables, in any case). A provider without its client id and secret
  is not offered.
- ServiceKeys: API keys of data services (OPENTOPOGRAPHY_API_KEY), from the environment or a
  Docker secret of the same name; params.SearchParams.opentopo_key defaults to it.
"""

import ipaddress
import os
from typing import Dict, List, Literal, Optional, Tuple
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRETS_DIR = "/run/secrets"  # Docker secrets
# The limits of the public site (--public) unless FINDALINE_LIMIT_<NAME> says otherwise
PUBLIC = {
    "buffer_max": 500, "max_length_max": 2000, "prominence_span_max": 1000, "drop_radius_max": 1000,
    "slope_threshold_min": 30, "group_distance_min_res": 2, "ortho_detail_max": 50,
    "not_allowed": ["ortho=always", "update", "coastline=world", "threads", "tile_size"],
    "radius_max": 3500, "resolution_max": 30,
    "region_km2_max": 2500, "area_km2_max": 50, "bbox_km_max": 75, "grid_mpx_max": 36, "tiles_max": 300, "anchors_max": 3_000_000,
    "lines_max": 3_000_000, "download_mb_max": 3000, "ortho_minutes": 10,
    "jobs_active_max": 2, "jobs_day_max": 20, "km2_day_max": 250, "runs_gb_max": 5, "nominatim_day_max": 50,
    "estimate_rate": "20/minute;300/hour", "profile_rate": "30/minute;500/hour",
    "queue_max": 50, "free_disk_gb_min": 20,
}
# Limits on request values: field of Limits -> (parameter of SearchParams, "min" or "max")
BOUNDS = {"buffer_max": ("buffer", "max"), "max_length_max": ("max_length", "max"),
          "prominence_span_max": ("prominence_span", "max"), "drop_radius_max": ("drop_radius", "max"),
          "slope_threshold_min": ("slope_threshold", "min"), "ortho_detail_max": ("ortho_detail", "max"),
          "radius_max": ("radius", "max"), "resolution_max": ("resolution", "max")}


class Limits(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FINDALINE_LIMIT_", extra="ignore", env_parse_none_str="none")

    download_seconds: Optional[float] = Field(
        None, gt=0, description="time limit of the download phase of a job (its processes are killed, the "
                                "job fails with \"time limit\")")
    compute_seconds: Optional[float] = Field(
        None, gt=0, description="time limit of the computing phase of a job")
    osm_seconds: Optional[float] = Field(
        None, gt=0, description="time limit of a job that downloads or updates an OpenStreetMap extract (not "
                                "the download phase's)")
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
    radius_max: Optional[float] = Field(None, gt=0, description="largest --radius, m")
    resolution_max: Optional[float] = Field(
        None, gt=0, description="largest --resolution, m (also the data's own, when none is given)")
    not_allowed: List[str] = Field(
        default_factory=list, description="parameters the web may not set (\"threads\"), or values of them "
                                          "(\"ortho=always\"); a JSON list")

    # The cost of a search (search.plan_search, check_estimate, _lines; refused before the work)
    region_km2_max: Optional[float] = Field(
        None, gt=0, description="largest bounding box of a --within box or of a named OSM area (osm:<name>, "
                                "from Nominatim), km²; checked before OSM is asked for coastline or cliffs in it")
    area_km2_max: Optional[float] = Field(
        None, gt=0, description="largest search area (--area, drawn, or a circle: π × --radius²), km²")
    bbox_km_max: Optional[float] = Field(
        None, gt=0, description="longest side of the bounding box (WGS84) of a search, km: of its area, and of its "
                                "grid with the margin; checked before any service is asked for data in it (sheet "
                                "indexes and Overpass are asked per 0.25° tile of the box)")
    grid_mpx_max: Optional[float] = Field(
        None, gt=0, description="most pixels of a grid (a circle) or of a tile of an area with its overlap, millions "
                                "(about 100 bytes of memory each); tiles are made smaller to fit")
    tiles_max: Optional[int] = Field(None, gt=0, description="most tiles over the bounding box of an area")
    anchors_max: Optional[int] = Field(None, gt=0, description="most anchors of a grid or a tile")
    lines_max: Optional[int] = Field(None, gt=0, description="most lines of a search (all its tiles)")
    download_mb_max: Optional[float] = Field(None, gt=0, description="largest download of a search, MB (its estimate)")
    ortho_minutes: Optional[float] = Field(
        None, ge=0, description="orthophoto tiles of a run that would take longer to download than this many "
                                "minutes are not cached: the map loads them online (--ortho auto)")

    # Quotas of a user (web.py, jobs.py); users without limits and administrators have none
    jobs_active_max: Optional[int] = Field(None, gt=0, description="searches of a user waiting or running at once")
    jobs_day_max: Optional[int] = Field(None, gt=0, description="searches a user may start in 24 hours")
    km2_day_max: Optional[float] = Field(
        None, gt=0, description="km² of the search areas of a user in 24 hours (counted by the worker, which knows "
                                "the area of osm: and coast/cliffs searches; circles count too)")
    runs_gb_max: Optional[float] = Field(
        None, gt=0, description="GB of a user's saved runs; no new search above it (runs can be deleted in Runs)")
    nominatim_day_max: Optional[int] = Field(
        None, gt=0, description="requests to Nominatim not answered from its cache (osm:<name> areas, run names, "
                                "countries) of a user's searches, estimates and profiles in 24 hours")
    estimate_rate: Optional[str] = Field(
        None, description="estimates a user may ask for, in the notation of the limits library "
                          "(\"20/minute;300/hour\")")
    profile_rate: Optional[str] = Field(None, description="profiles a user may ask for, the same notation")
    # Safeguards of the server, for everyone (also users without limits and administrators)
    queue_max: Optional[int] = Field(None, gt=0, description="searches waiting or running on the site at once")
    free_disk_gb_min: Optional[float] = Field(
        None, ge=0, description="no new searches while the disk of the data folder has less free space, GB")

    @field_validator("estimate_rate", "profile_rate")
    @classmethod
    def check_rate(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            from limits import parse_many
            parse_many(value)  # ValueError if it is not "N/unit;..."
        return value

    @classmethod
    def load(cls, public: bool = False) -> "Limits":
        """The limits from FINDALINE_LIMIT_*; for the public site those not set there are PUBLIC's."""
        env = cls()
        return cls(**{**(PUBLIC if public else {}), **env.model_dump(include=env.model_fields_set)})

    def phase_seconds(self, phase: str, osm: bool = False) -> Optional[float]:
        """The time limit of a phase of a job ("download" or "compute"); osm: of a job of an OSM extract."""
        if osm:
            return self.osm_seconds
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

    def check_request(self, params: Dict):
        """Refuse the fields and values of a request the web may not set (ValueError naming each),
        before the parameters are validated: the names, and the values as the request spells them
        (a choice is one exact text). The bounds are checked on the validated values (check_params)."""
        problems = []
        for name, value in params.items():
            if value is not None and not self.allows(name, value):
                problems.append(f"{name if not self.allows(name) else f'{name}={value}'}: not allowed on this site")
        if problems:
            raise ValueError("; ".join(problems))

    def check_params(self, p, resolution: Optional[float] = None, allowed: bool = True,
                     min_resolution: float = 1.0):
        """Refuse validated parameters (params.SearchParams) over the limits (ValueError naming each):
        the bounds of BOUNDS on the values pydantic read (so no spelling of a number skips them) and,
        with `allowed`, the fields and values the web may not set (the worker checks the bounds only:
        it sets some of those fields itself). `resolution`: the search's (by default the request's,
        or the finest, min_resolution), for --resolution and --group-distance."""
        problems = []
        if allowed:
            for name in type(p).model_fields:
                value = getattr(p, name)
                if name in p.model_fields_set and value is not None and not self.allows(name, value):
                    problems.append(f"{name if not self.allows(name) else f'{name}={value}'}: not allowed on "
                                    f"this site")
        resolution = resolution if resolution is not None else p.resolution
        res = max(resolution, min_resolution) if resolution is not None else min_resolution
        for name in dict.fromkeys(param for param, _ in BOUNDS.values()) | {"group_distance": None}:
            value = resolution if name == "resolution" else getattr(p, name)
            if value is None:
                continue
            low, high = self.bounds(name, res)
            if low is not None and value < low:
                problems.append(f"{name}: at least {low:g} on this site")
            if high is not None and value > high:
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


PROVIDERS = ("github", "google")  # the sign-in providers (accounts.py)


class AccountSettings(BaseSettings):
    """The sign-in of the public site (`findaline.py web --public`); the worker does not need them."""
    model_config = SettingsConfigDict(env_prefix="FINDALINE_", extra="ignore")

    session_secret: SecretStr = Field(
        description="random secret (at least 32 characters) signing the short-lived cookie of a sign-in in "
                    "progress (OAuth state, nonce and PKCE verifier)")
    github_client_id: Optional[str] = Field(None, description="client id of the GitHub OAuth app")
    github_client_secret: Optional[SecretStr] = Field(None, description="client secret of the GitHub OAuth app")
    google_client_id: Optional[str] = Field(None, description="client id of the Google OAuth client")
    google_client_secret: Optional[SecretStr] = Field(None, description="client secret of the Google OAuth client")
    admins: str = Field("", description="administrators as <provider>:<id>, comma-separated (github:12345 with "
                                        "the numeric GitHub user id, google:<sub>); the first one owns the runs "
                                        "from before the public mode")
    signup: Literal["approval", "open", "closed"] = Field(
        "approval", description="new accounts: approval (they wait for an administrator), open (approved at "
                                "once) or closed (only the administrators can sign in)")

    @field_validator("session_secret")
    @classmethod
    def check_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value().strip()
        if len(secret) < 32:
            raise ValueError("at least 32 random characters (python -c \"import secrets; "
                             "print(secrets.token_urlsafe(32))\")")
        return SecretStr(secret)

    @field_validator("admins")
    @classmethod
    def check_admins(cls, value: str) -> str:
        items = [item.strip() for item in value.split(",") if item.strip()]
        for item in items:
            provider, _, subject = item.partition(":")
            if provider not in PROVIDERS or not subject:
                raise ValueError(f"{item!r}: <provider>:<id> expected, the provider one of {', '.join(PROVIDERS)}")
        return ",".join(items)

    @property
    def admin_ids(self) -> List[Tuple[str, str]]:
        """The administrators as (provider, id), in the order given."""
        return [tuple(item.split(":", 1)) for item in self.admins.split(",") if item]

    def client(self, provider: str) -> Optional[Tuple[str, str]]:
        """(client id, client secret) of a provider; None when it is not configured (not offered)."""
        client_id, secret = getattr(self, f"{provider}_client_id"), getattr(self, f"{provider}_client_secret")
        if not client_id or not client_id.strip() or not secret or not secret.get_secret_value().strip():
            return None
        return client_id.strip(), secret.get_secret_value().strip()

    @property
    def providers(self) -> List[str]:
        """The providers offered (those with a client id and secret)."""
        return [p for p in PROVIDERS if self.client(p)]

    @classmethod
    def load(cls, secrets_dir: str = SECRETS_DIR) -> "AccountSettings":
        """Like PublicSettings.load; also refuses to start without any provider."""
        from pydantic import ValidationError
        try:
            settings = cls(_secrets_dir=secrets_dir if os.path.isdir(secrets_dir) else None)
        except ValidationError as e:
            raise SystemExit("--public needs these settings (environment or Docker secrets):\n"
                             + describe_error(e, "FINDALINE_"))
        if not settings.providers:
            raise SystemExit("--public needs a sign-in provider: FINDALINE_GITHUB_CLIENT_ID and "
                             "FINDALINE_GITHUB_CLIENT_SECRET, or FINDALINE_GOOGLE_CLIENT_ID and "
                             "FINDALINE_GOOGLE_CLIENT_SECRET")
        return settings


class ServiceKeys(BaseSettings):
    """API keys of data services: from the environment or a Docker secret of the same name in
    lower case (/run/secrets/opentopography_api_key; compose gives it to the worker only). The
    environment wins over the secret, as for the other settings."""
    model_config = SettingsConfigDict(extra="ignore")

    opentopography_api_key: Optional[SecretStr] = Field(
        None, description="OpenTopography API key for --source nasadem (OPENTOPOGRAPHY_API_KEY)")

    @classmethod
    def load(cls, secrets_dir: Optional[str] = None) -> "ServiceKeys":
        secrets_dir = secrets_dir or SECRETS_DIR
        return cls(_secrets_dir=secrets_dir if os.path.isdir(secrets_dir) else None)


def opentopography_key(secrets_dir: Optional[str] = None) -> Optional[str]:
    """The OpenTopography API key (environment or Docker secret); None when not set or empty."""
    key = ServiceKeys.load(secrets_dir).opentopography_api_key
    return (key.get_secret_value().strip() or None) if key else None


def describe_error(error, prefix: str) -> str:
    """The missing or wrong settings of a pydantic ValidationError, one per line."""
    lines = []
    for e in error.errors():
        name = prefix + "_".join(map(str, e["loc"])).upper()
        lines.append(f"{name}: {'missing' if e['type'] == 'missing' else e['msg']}")
    return "\n".join(lines)
