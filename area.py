"""
Search areas: where the anchors may be. Both anchors of a line must be inside the
area; the webbing may pass outside it (e.g. over a bay wider than a coastal band).

An area gives the box the working grid has to cover, a boolean mask on that grid,
and its shape for the maps and the orthophoto cache.
- Circle: a centre and a radius (--lat/--lon or --northing/--easting, --radius).
- Polygon: any shape in WGS84 (search areas from files or OSM, later).
"""

import math
from typing import Dict, Tuple

import numpy as np
import shapely
from pyproj import Transformer
from rasterio.features import rasterize
from shapely.geometry import Point, mapping

from grid import Grid

Bounds = Tuple[float, float, float, float]  # left, bottom, right, top


def _to(crs: str):
    return Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform


def project(geometry, src: str, dst: str):
    """A shapely geometry from one CRS to another (x, y order)."""
    to = Transformer.from_crs(src, dst, always_xy=True).transform
    return shapely.transform(geometry, lambda xy: np.column_stack(to(xy[:, 0], xy[:, 1])))


class Area:
    center: Tuple[float, float]  # (lat, lon), used for names and to centre the maps

    def bounds(self, crs: str) -> Bounds:
        """The box (in crs) the working grid has to cover."""
        raise NotImplementedError

    def mask(self, grid: Grid) -> np.ndarray:
        """True for the pixels of the grid inside the area."""
        raise NotImplementedError

    def shape_wgs84(self):
        """The outline in WGS84 (lon, lat), for the maps."""
        raise NotImplementedError

    def shape_mercator(self):
        """The outline in Web Mercator metres, for the orthophoto tiles."""
        return project(self.shape_wgs84(), "EPSG:4326", "EPSG:3857")

    def describe(self) -> Dict:
        """What run.json records."""
        raise NotImplementedError


class Circle(Area):
    def __init__(self, lat: float, lon: float, radius: float):
        self.center, self.radius = (lat, lon), radius

    def _xy(self, crs: str) -> Tuple[float, float]:
        lat, lon = self.center
        return _to(crs)(lon, lat)

    def bounds(self, crs: str) -> Bounds:
        x, y = self._xy(crs)
        r = self.radius
        return x - r, y - r, x + r, y + r

    def mask(self, grid: Grid) -> np.ndarray:
        x, y = self._xy(grid.crs)
        center_row = (grid.top - y) / grid.res - 0.5
        center_col = (x - grid.left) / grid.res - 0.5
        rows, cols = np.ogrid[:grid.height, :grid.width]
        return (rows - center_row) ** 2 + (cols - center_col) ** 2 <= (self.radius / grid.res) ** 2

    def shape_wgs84(self):
        lat, lon = self.center
        local = f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m"
        return project(Point(0, 0).buffer(self.radius, 128), local, "EPSG:4326")

    def shape_mercator(self):
        # Web Mercator stretches distances by 1/cos(latitude)
        lat, lon = self.center
        x, y = _to("EPSG:3857")(lon, lat)
        return Point(x, y).buffer(self.radius / math.cos(math.radians(lat)))

    def describe(self) -> Dict:
        return {"type": "circle", "lat": self.center[0], "lon": self.center[1], "radius": self.radius}


class Polygon(Area):
    def __init__(self, geometry):
        """geometry: a shapely (Multi)Polygon in WGS84 (lon, lat)."""
        self.geometry = geometry
        centroid = geometry.centroid
        self.center = (centroid.y, centroid.x)

    def bounds(self, crs: str) -> Bounds:
        return project(self.geometry, "EPSG:4326", crs).bounds

    def mask(self, grid: Grid) -> np.ndarray:
        shape = project(self.geometry, "EPSG:4326", grid.crs)
        return rasterize([(mapping(shape), 1)], out_shape=(grid.height, grid.width),
                         transform=grid.transform, fill=0, dtype="uint8").astype(bool)

    def shape_wgs84(self):
        return self.geometry

    def describe(self) -> Dict:
        return {"type": "polygon", "spec": getattr(self, "spec", None), "geometry": mapping(self.geometry)}


def _metric(geometry) -> str:
    """A local metric CRS (UTM) for a WGS84 geometry."""
    from grid import utm_crs
    c = geometry.centroid
    return utm_crs(c.y, c.x)


def box_bounds(text: str) -> Bounds:
    """(south, west, north, east) of a box "S,W,N,E" in degrees, checked to be a box on the globe."""
    try:
        south, west, north, east = (float(v) for v in text.split(","))
    except ValueError:
        raise ValueError(f"{text!r}: expected osm:<name> or S,W,N,E")
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):  # NaN fails too
        raise ValueError(f"{text!r}: S,W,N,E with -90 <= S < N <= 90 and -180 <= W < E <= 180")
    return south, west, north, east


def region(text: str, cache_dir: str):
    """A WGS84 polygon from "osm:<name>" (Nominatim) or "S,W,N,E" (a box in degrees)."""
    from shapely.geometry import box, shape
    import osm
    if text.startswith("osm:"):
        found = osm.search(text[4:].strip(), cache_dir)
        geometry = found and shape(found["geojson"])
        if not geometry or geometry.geom_type not in ("Polygon", "MultiPolygon"):
            raise ValueError(f"{text}: no area of that name in OpenStreetMap")
        return geometry
    south, west, north, east = box_bounds(text)
    return box(west, south, east, north)


def _buffered(geometries, metres: float, within=None):
    """Union of the geometries grown by `metres`, clipped to `within` (all WGS84)."""
    from shapely.ops import unary_union
    if not geometries:
        raise ValueError("nothing found to build the area from")
    union = unary_union(geometries)
    crs = _metric(within if within is not None else union)
    grown = project(union, "EPSG:4326", crs).buffer(metres)
    if within is not None:
        grown = grown.intersection(project(within, "EPSG:4326", crs))
    if grown.is_empty:
        raise ValueError("the area is empty")
    return project(grown, crs, "EPSG:4326")


def _read_file(path: str) -> list:
    """Geometries (WGS84) of every layer of a GeoJSON, KML, GPX... file."""
    import geopandas as gpd
    import pyogrio
    geometries = []
    for name, _ in pyogrio.list_layers(path):
        frame = gpd.read_file(path, layer=name)
        if len(frame) and frame.crs is not None:
            frame = frame.to_crs("EPSG:4326")
        geometries += [g for g in frame.geometry if g is not None and not g.is_empty]
    return geometries


def _near(theme: str, limit, reach: float, buffer: float, cache_dir: str):
    """Land and sea within `buffer` of the parts of an OSM theme within `reach` metres of
    `limit` (WGS84), not clipped to it; and the notes about parts a little further away.

    A named area (osm:Gozo) is the land of an island: clipping the band to it would cut
    off its sea side, the edges of its cliffs and the islets off its coast. The parts
    within `reach` (the longest line) are those a line could reach from the area.
    """
    from shapely.ops import unary_union
    import osm
    crs = _metric(limit)
    zone = project(limit, "EPSG:4326", crs)
    near_zone, far_zone = zone.buffer(reach), zone.buffer(2 * reach)
    features = osm.theme(theme, *project(far_zone, crs, "EPSG:4326").bounds, cache_dir)
    near, beyond = [], []
    for feature in features:
        geometry = project(feature["geometry"], "EPSG:4326", crs)
        part = geometry.intersection(near_zone)
        if not part.is_empty:
            near.append(part)
        rest = geometry.difference(near_zone).intersection(far_zone)
        if not rest.is_empty:
            beyond.append(rest)
    if not near:
        raise ValueError(f"no {theme} within {reach:g} m of the area")
    grown = unary_union(near).buffer(buffer)
    notes = []
    if beyond:
        pieces = list(getattr(unary_union(beyond), "geoms", [unary_union(beyond)]))
        where = project(pieces[0].representative_point(), crs, "EPSG:4326")
        name = "coastline" if theme == "coastline" else "cliffs and crags"
        notes.append(f"{len(pieces)} piece(s) of {name} lie {reach:g}-{2 * reach:g} m from the area and are left out "
                     f"(e.g. at {where.y:.5f}, {where.x:.5f}): a longer --max-length or a box as --within "
                     f"takes them in")
    return project(grown, crs, "EPSG:4326"), notes


def from_shapes(shapes: list, buffer: float, limit=None, label: str = "the area"):
    """One WGS84 geometry from shapes (WGS84): polygons as they are, points and lines grown
    by `buffer` metres, all clipped to `limit` (a WGS84 polygon) if given."""
    from shapely.ops import unary_union
    areas = [g for g in shapes if g.geom_type in ("Polygon", "MultiPolygon")]
    others = [g for g in shapes if g.geom_type not in ("Polygon", "MultiPolygon")]
    parts = areas + ([_buffered(others, buffer)] if others else [])
    geometry = unary_union(parts) if parts else None
    if geometry is None or geometry.is_empty:
        raise ValueError(f"{label}: no geometries")
    if limit is not None:
        geometry = geometry.intersection(limit)
    return geometry


def from_spec(spec: str, buffer: float, within: str, cache_dir: str, reach: float = 0.0) -> "Polygon":
    """The search area of --area (with --buffer and --within):
    coast    land and sea within `buffer` of the OSM coastline
    cliffs   within `buffer` of OSM cliffs (natural=cliff) and crags (climbing=crag)
    <file>   polygons of a GeoJSON/KML/GPX file as they are, points and lines grown by `buffer`
    osm:<name>  the area of a named OpenStreetMap object (Nominatim)
    For coast and cliffs, `within osm:<name>` takes the coastline or cliffs within `reach`
    metres of that area (the longest line: islets off its coast, its sea side and cliff
    edges stay in); a box S,W,N,E clips the band. Notes for the user are in area.notes.
    """
    import os
    import osm
    limit = region(within, cache_dir) if within else None
    notes = []
    if spec in ("coast", "cliffs"):
        if limit is None:
            raise ValueError(f"--area {spec} needs --within (osm:<name> or S,W,N,E)")
        theme = "coastline" if spec == "coast" else "cliff"
        if within.startswith("osm:"):
            geometry, notes = _near(theme, limit, reach, buffer, cache_dir)
        else:
            features = osm.theme(theme, *limit.bounds, cache_dir)
            geometry = _buffered([f["geometry"] for f in features], buffer, limit)
    elif spec.startswith("osm:"):
        geometry = region(spec, cache_dir)
        if limit is not None:
            geometry = geometry.intersection(limit)
    elif os.path.exists(spec):
        geometry = from_shapes(_read_file(spec), buffer, limit, spec)
    else:
        raise ValueError(f"--area {spec!r}: expected coast, cliffs, osm:<name> or an existing file")
    if geometry.is_empty:
        raise ValueError(f"--area {spec}: the area is empty")
    area = Polygon(geometry)
    area.spec = {"area": spec, "buffer": buffer, "within": within}
    area.notes = notes
    return area


def size_km2(geometry) -> float:
    """Area of a WGS84 geometry in km²."""
    return project(geometry, "EPSG:4326", _metric(geometry)).area / 1e6
