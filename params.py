"""
Search parameters: one schema for the command line and (later) the web API and the
browser form. Every field carries its help text, its group and, where needed, CLI
details; the argparse options of findaline.py are generated from it.

- SearchParams: what a search computes and saves (validated as a whole).
- CliOptions: what only the command line has (data folder, prompts, matplotlib window).
Fields marked cli_only (file paths, the API key) must not come from the web; web_only
fields (a drawn geometry) are set by the web app's server, not by its form or the CLI.
"""

import argparse
import os
import typing
import unicodedata
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticUndefined

import settings
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


MAX_NAME = 100  # characters of a run name
MAX_AREA_TEXT = 200  # characters of --within, and of an --area that is no file (osm:<name>)


class SearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)  # no NaN or Inf anywhere

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
    geometry: Optional[dict] = field(None, "a drawn or uploaded area (GeoJSON, WGS84) instead of --area; set by "
                                           "the web app", "centre", web_only=True)
    buffer: float = field(100, "metres around the coastline, cliffs, points or lines of --area", "centre")
    within: Optional[str] = field(None, "where --area coast or cliffs applies: osm:<name> takes the coastline or "
                                        "cliffs within --max-length of that area (islets off its coast, its sea "
                                        "side and cliff edges included); a box S,W,N,E in degrees clips the band",
                                  "centre")

    # Elevation data
    source: str = field("auto", "data source; 'auto' picks the national source for the country of "
                                "the centre (Poland, Malta, Spain, Switzerland, Slovakia when its tiles are "
                                "set up, the USA) or the Europe-wide model elsewhere", "data",
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
                                                  "2012 from 2018, the USA from USGS 10 m, Alaska from its "
                                                  "5 m IfSAR first, others from FABDEM 30 m)", "data")
    opentopo_key: Optional[str] = field(settings.opentopography_key,
                                        "OpenTopography API key for --source nasadem", "data", cli_only=True,
                                        default_text="the OPENTOPOGRAPHY_API_KEY environment variable or "
                                                     "Docker secret")

    # Search
    radius: float = field(1000, "search radius around the centre in metres", "search")
    resolution: Optional[float] = field(None, "working resolution in metres (data is resampled to it), at "
                                              "least 1", "search",
                                        default_text="the resolution of the data, at least 1: 1 for the national "
                                                     "LiDAR sources, 5 for Spain and Alaska's IfSAR, 10 for "
                                                     "USGS 1/3 arc-second, 30 for Europe-wide/NASADEM")
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
    high_above: float = field(20, "height of the webbing above the terrain in metres from which a part of a "
                                  "line counts as high (the share of each line saved as high_pct)", "search")
    min_height: Optional[float] = field(None, "keep only lines whose webbing gets at least this many metres "
                                              "above the terrain somewhere (webbing of --model: with the sag "
                                              "estimator under the athlete)", "search")
    min_mean_height: Optional[float] = field(None, "keep only lines whose webbing is on average at least this "
                                                   "many metres above the terrain", "search")
    min_high_share: Optional[float] = field(None, "keep only lines that are at least --high-above metres above "
                                                  "the terrain over at least this % of their length", "search")
    max_wall_angle: Optional[float] = field(None, "keep only lines that leave both anchors at most this many "
                                                  "degrees from the downslope direction there (0: straight out "
                                                  "over the drop, 90: along the wall)", "search")
    min_prominence: Optional[float] = field(None, "keep only lines with at least one anchor this many metres "
                                                  "prominent (its height above the level at which the rock it "
                                                  "stands on joins terrain wider than --prominence-span)", "search")
    min_prominence_both: Optional[float] = field(None, "the same for both anchors of each line", "search")
    prominence_span: float = field(300, "width in metres from which terrain no longer counts as the anchor's "
                                        "own rock (for the prominence)", "search")
    min_drop: Optional[float] = field(None, "keep only lines whose anchors are both at least this many metres "
                                            "above the lowest terrain within --drop-radius", "search")
    drop_radius: float = field(50, "metres around an anchor in which its drop is measured", "search")
    threads: Optional[int] = field(None, "number of threads for the line search (default: CPU count - 1, "
                                         "at most a container's CPU quota)", "search")
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
    group_by: Literal["clearance", "longest", "height"] = field(
        "clearance", "line shown for each group: largest clearance, longest, or highest above the terrain "
                     "(height_max)", "output")
    name: Optional[str] = field(None, "name of the run (default: place name from OpenStreetMap)", "output")
    no_html: bool = field(False, "do not save the interactive map", "output")
    ortho: Literal["auto", "always", "never"] = field(
        "auto", "orthophoto tiles for offline use of the map: 'auto' asks when they would take longer than "
                "30 min (--yes downloads), 'always', or 'never' (the map loads them online)", "output")
    ortho_detail: Optional[int] = field(None, "detailed orthophoto (up to zoom 21) only around this many best "
                                              "groups; the others load it online", "output",
                                        default_text="50 for --area, all for a circle; swisstopo (fair use): 20")

    @property
    def nmpt_mode(self) -> Optional[str]:
        """"mark", "reject", or None when the NMPT is not used."""
        return None if self.nmpt == "off" else self.nmpt

    @field_validator("geometry")
    @classmethod
    def check_geometry(cls, value):
        if value is not None:
            from area import check_geometry
            check_geometry(value)
        return value

    @model_validator(mode="after")
    def check(self):
        wgs84 = self.lat is not None or self.lon is not None
        pl1992 = self.northing is not None or self.easting is not None
        if self.area is not None and self.geometry is not None:
            raise ValueError("give either --area or a drawn geometry, not both")
        if self.area is not None or self.geometry is not None:
            if wgs84 or pl1992:
                raise ValueError("give either --area or a search centre, not both")
        elif wgs84 == pl1992:
            raise ValueError("give the search centre as either --lat/--lon or --northing/--easting")
        if wgs84 and (self.lat is None or self.lon is None):
            raise ValueError("both --lat and --lon are required")
        if wgs84 and not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180):
            raise ValueError("--lat is -90 to 90 and --lon -180 to 180 degrees")
        if pl1992 and (self.northing is None or self.easting is None):
            raise ValueError("both --northing and --easting are required")
        if self.source not in SOURCE_CHOICES:
            raise ValueError(f"unknown --source {self.source}")
        if self.resolution is not None and self.resolution < src.MIN_RESOLUTION:
            raise ValueError(f"--resolution must be at least {src.MIN_RESOLUTION:g} m")
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
        if self.high_above < 0:
            raise ValueError("--high-above cannot be negative")
        if self.min_high_share is not None and not 0 <= self.min_high_share <= 100:
            raise ValueError("--min-high-share is a share of the length: 0 to 100")
        if self.max_wall_angle is not None and not 0 <= self.max_wall_angle <= 180:
            raise ValueError("--max-wall-angle is 0 to 180 degrees")
        if self.prominence_span <= 0 or self.drop_radius <= 0:
            raise ValueError("--prominence-span and --drop-radius must be positive")
        if self.tile_size is not None and self.tile_size < 100:
            raise ValueError("--tile-size must be at least 100 m")
        for option, text in (("--area", self.area), ("--within", self.within)):
            if text is not None and any(unicodedata.category(c) == "Cc" for c in text):
                raise ValueError(f"{option}: no control characters")
        if (self.within and len(self.within) > MAX_AREA_TEXT) or (
                self.area and self.area.startswith("osm:") and len(self.area) > MAX_AREA_TEXT):
            raise ValueError(f"--area osm:<name> and --within: at most {MAX_AREA_TEXT} characters")
        if self.within and self.area is None and self.geometry is None:
            raise ValueError("--within limits --area; give --area too")
        if self.within and not self.within.startswith("osm:"):
            from area import box_bounds
            box_bounds(self.within)  # a box S,W,N,E in range
        if not 0 < self.slope_threshold <= 90:
            raise ValueError("--slope-threshold is more than 0 and at most 90 degrees")
        if self.ortho_detail is not None and self.ortho_detail < 0:
            raise ValueError("--ortho-detail cannot be negative")
        if self.name is not None and (len(self.name) > MAX_NAME
                                      or any(unicodedata.category(c) == "Cc" for c in self.name)):
            raise ValueError(f"--name: at most {MAX_NAME} characters, no control characters")
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


# --- What the web form shows (web.schema): a short label and the unit of each field, the fields by
# topic with readable titles (the form's "More settings"), and presets of the key fields ---

LABELS = {  # field -> (label, unit); every field the web may set has one (tests/test_units.py)
    "lat": ("Latitude", "°"), "lon": ("Longitude", "°"), "northing": ("Northing (PL-1992)", "m"),
    "easting": ("Easting (PL-1992)", "m"), "area": ("Area", ""), "buffer": ("Buffer", "m"),
    "within": ("Within", ""), "radius": ("Radius", "m"),
    "source": ("Elevation data", ""), "water": ("Water", ""), "coastline": ("Where the sea is", ""),
    "shore_buffer": ("Shore buffer", "m"), "fill_gaps": ("Fill gaps in the data", ""),
    "update": ("Check cached data first", ""), "resolution": ("Resolution", "m"),
    "min_length": ("Shortest line", "m"), "max_length": ("Longest line", "m"),
    "max_anchor_diff": ("Height difference of the anchors, share of the length", "%"),
    "max_anchor_diff_m": ("Height difference of the anchors", "m"),
    "slope_threshold": ("Steepness of an anchor", "°"), "nmpt": ("Trees and buildings", ""),
    "high_above": ("High from", "m"), "min_height": ("Highest point of the webbing, at least", "m"),
    "min_mean_height": ("Mean height of the webbing, at least", "m"),
    "min_high_share": ("Share of the line high up, at least", "%"),
    "max_wall_angle": ("Angle to the wall, at most", "°"),
    "min_prominence": ("Prominence of one anchor, at least", "m"),
    "min_prominence_both": ("Prominence of both anchors, at least", "m"),
    "prominence_span": ("Width of the anchor's own rock", "m"),
    "min_drop": ("Drop below both anchors, at least", "m"), "drop_radius": ("Drop measured within", "m"),
    "threads": ("Threads", ""), "tile_size": ("Tile size", "m"),
    "model": ("Clearance model", ""), "pretension": ("Pretension", "kN"), "stretch": ("Webbing stretch at 10 kN", "%"),
    "webbing_weight": ("Webbing weight", "g/m"), "athlete_mass": ("Athlete mass", "kg"),
    "fall_zone": ("Fall zone", "m"),
    "group_distance": ("Group similar lines within", "m"), "group_by": ("Line shown for each group", ""),
    "name": ("Name of the run", ""), "no_html": ("No interactive map", ""),
    "ortho": ("Orthophoto for offline use", ""), "ortho_detail": ("Detailed orthophoto around the best groups", ""),
}

# topic -> (title, fields in the form's order); "where" is the place part of the form, not "More settings"
TOPICS = {
    "where": ("Where", ["lat", "lon", "northing", "easting", "radius", "area", "buffer", "within", "name"]),
    "height": ("Height above the terrain", ["nmpt", "high_above", "min_height", "min_mean_height",
                                            "min_high_share"]),
    "anchors": ("Anchors", ["slope_threshold", "min_prominence", "min_prominence_both", "prominence_span",
                            "min_drop", "drop_radius"]),
    "lines": ("Lines", ["min_length", "max_length", "max_anchor_diff_m", "max_anchor_diff", "max_wall_angle"]),
    "data": ("Elevation data", ["source", "resolution", "water", "coastline", "shore_buffer", "fill_gaps",
                                "update"]),
    "rigging": ("Clearance model", ["model", "pretension", "stretch", "webbing_weight", "athlete_mass",
                                    "fall_zone"]),
    "output": ("Results and map", ["group_distance", "group_by", "no_html", "ortho", "ortho_detail"]),
    "computing": ("Computing", ["threads", "tile_size"]),
}
TOPIC_OF = {name: topic for topic, (_, names) in TOPICS.items() for name in names}

# The fields a preset sets: one it leaves out gets its default
PRESET_FIELDS = ("min_length", "max_length", "max_anchor_diff_m")
# Presets of the kind of lines (the form's chips or cards): id -> (label, values); "custom" sets nothing
# (the values stay as they are: the defaults, or those of the search being edited)
PRESETS = {
    "typical": ("Typical highline", {"min_length": 30, "max_length": 150, "max_anchor_diff_m": 10}),
    "long": ("Long lines", {"min_length": 100, "max_length": 400}),
    "short": ("Short & low", {"min_length": 15, "max_length": 60}),
    "custom": ("Custom", None),
}


def preset_values(preset: str) -> Optional[dict]:
    """The values of a preset for every field of PRESET_FIELDS (defaults for those it leaves out);
    None for "custom"."""
    values = PRESETS[preset][1]
    if values is None:
        return None
    return {name: values.get(name, SearchParams.model_fields[name].default) for name in PRESET_FIELDS}


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
            if info.json_schema_extra.get("web_only"):
                continue
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
