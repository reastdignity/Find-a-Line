"""
Search parameters: one schema for the command line and (later) the web API and the
browser form. Every field carries its help text, its group and, where needed, CLI
details; the argparse options of findaline.py are generated from it.

- SearchParams: what a search computes and saves (validated as a whole).
- CliOptions: what only the command line has (data folder, prompts, matplotlib window).
Fields marked cli_only (file paths, the API key) must not come from the web.
"""

import argparse
import os
import typing
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import PydanticUndefined

import sources as src

GROUPS = {
    "centre": "where to search: a circle around --point (possibly several times), --lat/--lon or "
              "--northing/--easting, or an area (--area)",
    "data": "elevation data",
    "search": "search",
    "rigging": "clearance model (the sag estimator is also shown in the profile of the map)",
    "output": "data and output",
}
SOURCE_CHOICES = ["auto", "files"] + list(src.SOURCES)


def field(default, help: str, group: str, *, default_text: Optional[str] = None, **extra):
    """A field with its help text and group; default_text replaces the shown default."""
    meta = {"group": group, **extra}
    if default_text:
        meta["default_text"] = default_text
    if callable(default):
        return Field(default_factory=default, description=help, json_schema_extra=meta)
    return Field(default, description=help, json_schema_extra=meta)


class SearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Search centre
    lat: Optional[float] = field(None, "latitude, WGS84 (e.g. 49.2320)", "centre")
    lon: Optional[float] = field(None, "longitude, WGS84 (e.g. 19.9817)", "centre")
    northing: Optional[float] = field(None, "PL-1992 (EPSG:2180) northing in metres; geoportal shows it as X",
                                      "centre")
    easting: Optional[float] = field(None, "PL-1992 (EPSG:2180) easting in metres; geoportal shows it as Y",
                                     "centre")
    area: Optional[str] = field(None, "search an area instead of a circle: 'coast' (within --buffer of the OSM "
                                      "coastline), 'cliffs' (within --buffer of OSM cliffs and crags), a GeoJSON/"
                                      "KML/GPX file (polygons as they are, points and lines grown by --buffer) or "
                                      "osm:<name> (a named OpenStreetMap area); both anchors of a line lie inside, "
                                      "the webbing may leave it", "centre")
    buffer: float = field(100, "metres around the coastline, cliffs, points or lines of --area", "centre")
    within: Optional[str] = field(None, "limit --area to osm:<name> or a box S,W,N,E in degrees (needed for "
                                        "coast and cliffs)", "centre")

    # Elevation data
    source: str = field("auto", "data source; 'auto' picks the national source for the country of "
                                "the centre (Poland, Malta) or the Europe-wide model elsewhere", "data",
                        choices=SOURCE_CHOICES)
    elevation_file: Optional[str] = field(None, "your own terrain model (GeoTIFF/ASC, e.g. NASADEM from "
                                                "OpenTopography); implies --source files", "data", cli_only=True)
    surface_file: Optional[str] = field(None, "your own surface model with trees/buildings, used with "
                                              "--elevation-file", "data", cli_only=True)
    water: Literal["fill", "off"] = field("fill", "'fill' gives the sea (0 m) and lakes (their shore level) "
                                                  "an elevation where the data has none, so lines over water "
                                                  "and the edges of sea cliffs are found", "data")
    coastline: Literal["overpass", "world", "copernicus"] = field(
        "overpass", "where the sea is: OpenStreetMap coastline from the Overpass API (falls back to the "
                    "Copernicus mask with a warning), OpenStreetMap water polygons of the whole world (one "
                    "download of about 900 MB), or the Copernicus water body mask (30 m, less accurate)", "data")
    shore_buffer: float = field(20, "metres around the sea (from --coastline) where pixels without data are "
                                    "still taken as water", "data")
    fill_gaps: Literal["on", "off"] = field("on", "fill gaps in the data on land from another source (Malta "
                                                  "2012 from 2018, others from FABDEM 30 m)", "data")
    opentopo_key: Optional[str] = field(lambda: os.environ.get("OPENTOPOGRAPHY_API_KEY"),
                                        "OpenTopography API key for --source nasadem", "data", cli_only=True,
                                        default_text="the OPENTOPOGRAPHY_API_KEY environment variable")

    # Search
    radius: float = field(1000, "search radius around the centre in metres", "search")
    resolution: Optional[float] = field(None, "working resolution in metres (data is resampled to it); "
                                              "default: 2 for national LiDAR sources, 30 for Europe-wide/NASADEM, "
                                              "the file resolution for --elevation-file", "search")
    min_length: float = field(30, "minimum line length in metres", "search")
    max_length: float = field(1000, "maximum line length in metres", "search")
    max_anchor_diff: float = field(5, "maximum height difference of the anchors in % of the line length",
                                   "search")
    max_anchor_diff_m: float = field(50, "maximum height difference of the anchors in metres", "search")
    slope_threshold: float = field(45, "minimum terrain slope in degrees for a pixel to be an anchor", "search")
    slope_file: Optional[str] = field(None, "use your own slope raster (degrees, GeoTIFF/ASC) instead of "
                                            "computing slope from NMT", "search", cli_only=True)
    nmpt: Literal["mark", "reject", "off"] = field(
        "mark", "also check clearance over trees/buildings (NMPT): 'mark' shows such lines in orange, "
                "'reject' drops them, 'off' skips the check (and the NMPT download)", "search")
    threads: Optional[int] = field(None, "number of threads for the line search (default: CPU count - 1)",
                                   "search")
    tile_size: Optional[float] = field(None, "large areas are computed in square tiles of this size in metres "
                                             "(plus an overlap of --max-length); default: from the free memory",
                                       "search")

    # Clearance model
    model: Literal["formula", "sag"] = field("formula", "'formula': maximum elevation formula (see README); "
                                                        "'sag': sag estimator below the webbing plus --fall-zone",
                                             "rigging")
    pretension: float = field(2.5, "pretension in kN", "rigging")
    stretch: float = field(5, "webbing stretch at 10 kN in %", "rigging")
    webbing_weight: float = field(70, "webbing weight in g/m", "rigging")
    athlete_mass: float = field(80, "athlete mass in kg", "rigging")
    fall_zone: float = field(3, "height below the sagged webbing that must stay free (leash fall), m", "rigging")

    # Output of the run
    update: Optional[Literal["stale", "all"]] = field(
        None, "check the cached data of the search area before the search: 'stale' checks what is older than "
              "its interval (see `findaline.py cache status`), 'all' checks everything that can change "
              "(versioned releases never do)", "data")
    group_distance: Optional[float] = field(None, "group similar lines: both anchors within this many metres "
                                                  "(also the initial setting of the interactive map); default: "
                                                  "20, or twice the resolution if that is more", "output")
    group_by: Literal["clearance", "longest"] = field("clearance", "line shown for each group: largest "
                                                                   "clearance or longest", "output")
    name: Optional[str] = field(None, "name of the run (default: place name from OpenStreetMap)", "output")
    no_html: bool = field(False, "do not save the interactive map", "output")
    ortho: Literal["auto", "always", "never"] = field(
        "auto", "orthophoto tiles for offline use of the map: 'auto' asks when they would take longer than "
                "30 min (--yes downloads), 'always', or 'never' (the map loads them online)", "output")
    ortho_detail: int = field(50, "for --area: detailed orthophoto (up to zoom 21) only around this many best "
                                  "groups; the others load it online", "output")

    @property
    def nmpt_mode(self) -> Optional[str]:
        """"mark", "reject", or None when the NMPT is not used."""
        return None if self.nmpt == "off" else self.nmpt

    @model_validator(mode="after")
    def check(self):
        wgs84 = self.lat is not None or self.lon is not None
        pl1992 = self.northing is not None or self.easting is not None
        if self.area is not None:
            if wgs84 or pl1992:
                raise ValueError("give either --area or a search centre, not both")
        elif wgs84 == pl1992:
            raise ValueError("give the search centre as either --lat/--lon or --northing/--easting")
        if wgs84 and (self.lat is None or self.lon is None):
            raise ValueError("both --lat and --lon are required")
        if pl1992 and (self.northing is None or self.easting is None):
            raise ValueError("both --northing and --easting are required")
        if self.source not in SOURCE_CHOICES:
            raise ValueError(f"unknown --source {self.source}")
        if self.resolution is not None and self.resolution <= 0:
            raise ValueError("--resolution must be positive")
        for path in (self.elevation_file, self.surface_file):
            if path and not os.path.exists(path):
                raise ValueError(f"file not found: {path}")
        if self.surface_file and not self.elevation_file:
            raise ValueError("--surface-file needs --elevation-file")
        if not 0 < self.min_length < self.max_length:
            raise ValueError("--min-length must be positive and smaller than --max-length")
        if self.radius <= 0:
            raise ValueError("--radius must be positive")
        if self.buffer <= 0:
            raise ValueError("--buffer must be positive")
        if self.tile_size is not None and self.tile_size < 100:
            raise ValueError("--tile-size must be at least 100 m")
        if self.within and self.area is None:
            raise ValueError("--within limits --area; give --area too")
        if self.shore_buffer < 0:
            raise ValueError("--shore-buffer cannot be negative")
        if self.group_distance is not None and self.group_distance <= 0:
            raise ValueError("--group-distance must be positive (use --all-lines to list every line)")
        if self.slope_file and not os.path.exists(self.slope_file):
            raise ValueError(f"slope file not found: {self.slope_file}")
        if min(self.max_anchor_diff, self.max_anchor_diff_m) < 0:
            raise ValueError("--max-anchor-diff and --max-anchor-diff-m cannot be negative")
        if min(self.pretension, self.stretch) <= 0 or min(self.webbing_weight, self.athlete_mass,
                                                          self.fall_zone) < 0:
            raise ValueError("rigging values must be positive")
        return self


class CliOptions(BaseModel):
    point: Optional[List[str]] = field(None, "LAT,LON[,NAME]: search around this point (WGS84), with an "
                                             "optional name for the run; repeat for several points, each "
                                             "searched with the same options and saved as its own run",
                                       "centre")
    data_dir: Optional[str] = field(None, "folder holding cache/ (downloaded data), runs/ (saved runs) and "
                                          "work/ (working grids)", "output",
                                    default_text="FINDALINE_DATA, else the folder of the script")
    html: Optional[str] = field(None, "page that opens the map of the latest run", "output",
                                default_text="findaline_map.html in the data folder")
    all_lines: bool = field(False, "list every line instead of one line per group", "output")
    no_plot: bool = field(False, "do not open the matplotlib map", "output")
    yes: bool = field(False, "do not ask for confirmation before long runs", "output", flags=["-y"])


def _argument(name: str, info) -> tuple:
    """argparse flags and keyword arguments for one field."""
    extra = info.json_schema_extra or {}
    flags = extra.get("flags", []) + ["--" + name.replace("_", "-")]
    kwargs = {"dest": name, "help": (info.description or "").replace("%", "%%")}
    kind = info.annotation
    if typing.get_origin(kind) is typing.Union:  # Optional[X]
        kind = next(a for a in typing.get_args(kind) if a is not type(None))
    default = info.default if info.default is not PydanticUndefined else None
    if kind is bool:
        kwargs.update(action="store_true")
        return flags, kwargs
    if typing.get_origin(kind) is list:  # repeatable option
        kwargs.update(action="append", type=typing.get_args(kind)[0], metavar=name.upper())
        return flags, kwargs
    if typing.get_origin(kind) is Literal:
        kwargs["choices"] = list(typing.get_args(kind))
    elif "choices" in extra:
        kwargs["choices"] = extra["choices"]
    else:
        kwargs["type"] = kind
    if "default_text" in extra:
        kwargs["help"] += f" (default: {extra['default_text']})"
        kwargs["default"] = argparse.SUPPRESS  # not given: the model's default applies
    else:
        kwargs["default"] = default
    return flags, kwargs


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    groups = {key: parser.add_argument_group(title) for key, title in GROUPS.items()}
    for model in (SearchParams, CliOptions):
        for name, info in model.model_fields.items():
            flags, kwargs = _argument(name, info)
            groups[info.json_schema_extra["group"]].add_argument(*flags, **kwargs)
    return parser


def _point(text: str) -> dict:
    """{"lat", "lon"[, "name"]} from "LAT,LON[,NAME]"."""
    parts = [p.strip() for p in text.split(",", 2)]
    try:
        point = {"lat": float(parts[0]), "lon": float(parts[1])}
    except (ValueError, IndexError):
        raise ValueError(f"--point {text!r}: expected LAT,LON or LAT,LON,NAME")
    if len(parts) == 3 and parts[2]:
        point["name"] = parts[2]
    return point


def parse_cli(argv=None, description: str = "") -> tuple:
    """([SearchParams, one per point], CliOptions) from the command line; errors end in parser.error."""
    parser = build_parser(description)
    values = vars(parser.parse_args(argv))
    given = lambda model: {k: v for k, v in values.items() if k in model.model_fields and v is not None}
    try:
        options = CliOptions(**given(CliOptions))
        common = given(SearchParams)
        if not options.point:
            return [SearchParams(**common)], options
        if any(k in common for k in ("lat", "lon", "northing", "easting", "area")):
            raise ValueError("give the search centre as either --point or --lat/--lon or --northing/--easting, "
                             "or an --area")
        if "name" in common and len(options.point) > 1:
            raise ValueError("--name is for a single point; give each point its name: --point LAT,LON,NAME")
        return [SearchParams(**{**common, **_point(text)}) for text in options.point], options
    except ValidationError as e:
        error = e.errors()[0]
        parser.error(str(error.get("ctx", {}).get("error") or error["msg"]))
    except ValueError as e:
        parser.error(str(e))
