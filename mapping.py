"""
Converting results to GPS coordinates and displaying them on maps.
"""

from typing import Dict, List, Optional

import numpy as np
from pyproj import Transformer

from geoportal import CRS, Grid
from helpers import Line

ORTHOPHOTO_WMS = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/HighResolution"

COLOR_VALID = "#FF10F0"     # neon pink
COLOR_OBSTACLE = "#FF8C00"  # orange: line passes too close to trees/buildings (NMPT)

_to_wgs84 = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
_to_pl1992 = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)


def wgs84_to_pl1992(lat: float, lon: float):
    """Return (easting, northing) in EPSG:2180."""
    return _to_pl1992.transform(lon, lat)


def pl1992_to_wgs84(easting: float, northing: float):
    """Return (lat, lon)."""
    lon, lat = _to_wgs84.transform(easting, northing)
    return lat, lon


def coordify(lines: List[Line], grid: Grid, nmpt_mode: Optional[str]) -> List[Dict]:
    """Convert pixel coordinates of each line to GPS coordinates (lat, lon).

    Returns:
        One dict per line with anchor coordinates and line statistics
    """
    if not lines:
        return []
    rows = np.array([[line.a1[0], line.a2[0]] for line in lines]).ravel()
    cols = np.array([[line.a1[1], line.a2[1]] for line in lines]).ravel()
    easting, northing = grid.pixel_to_pl1992(rows, cols)
    lon, lat = _to_wgs84.transform(easting, northing)

    results = []
    for k, line in enumerate(lines):
        over_obstacles = (nmpt_mode == "mark" and line.clearance_nmpt is not None
                          and line.clearance_nmpt <= 0)
        results.append({
            "anchor1": (float(lat[2 * k]), float(lon[2 * k])),
            "anchor2": (float(lat[2 * k + 1]), float(lon[2 * k + 1])),
            "length": line.length,
            "elev_a1": line.elev_a1,
            "elev_a2": line.elev_a2,
            "clearance": line.clearance,
            "clearance_nmpt": line.clearance_nmpt,
            "over_obstacles": over_obstacles,
        })
    return results


def _search_area(center_lat: float, center_lon: float, radius: float):
    """Search circle as a polygon in WGS84 (geopandas)."""
    import geopandas as gpd
    from shapely.geometry import Point

    easting, northing = wgs84_to_pl1992(center_lat, center_lon)
    circle = gpd.GeoSeries([Point(easting, northing).buffer(radius)], crs=CRS)
    return circle.to_crs("EPSG:4326")


def mapify(results: List[Dict], center_lat: float, center_lon: float, radius: float):
    """Visualize lines on a static map (matplotlib + OpenTopoMap)."""
    import contextily as ctx
    import geopandas as gpd
    import matplotlib.pyplot as plt
    from shapely.geometry import LineString

    if not results:
        print("No lines to display.")
        return

    lines = [LineString([(r["anchor1"][1], r["anchor1"][0]), (r["anchor2"][1], r["anchor2"][0])])
             for r in results]
    colors = [COLOR_OBSTACLE if r["over_obstacles"] else COLOR_VALID for r in results]
    gdf = gpd.GeoDataFrame({"color": colors}, geometry=lines, crs="EPSG:4326")

    fig, ax = plt.subplots(figsize=(10, 10))
    _search_area(center_lat, center_lon, radius).boundary.plot(
        ax=ax, color="black", linestyle="--", linewidth=0.8)
    gdf.plot(ax=ax, color=gdf["color"], linewidth=0.8, alpha=0.9)

    # Show circles at line midpoints
    midpoints = gpd.GeoDataFrame({"color": colors}, crs="EPSG:4326",
                                 geometry=[line.interpolate(0.5, normalized=True) for line in lines])
    midpoints.plot(ax=ax, color=midpoints["color"], markersize=100, marker="o", alpha=0.6, zorder=3)

    ctx.add_basemap(ax, crs=gdf.crs, source=ctx.providers.OpenTopoMap)
    plt.show()


def save_html_map(results: List[Dict], center_lat: float, center_lon: float,
                  radius: float, path: str):
    """Save an interactive map (Leaflet) with OpenTopoMap, OSM and geoportal orthophoto."""
    import folium
    import xyzservices.providers as xyz
    from folium.template import Template
    from folium.utilities import JsCode

    class Wgs84WmsTileLayer(folium.WmsTileLayer):
        # Same as WmsTileLayer, but options may contain JavaScript (the crs object)
        _template = Template("""
            {% macro script(this, kwargs) %}
                var {{ this.get_name() }} = L.tileLayer.wms(
                    {{ this.url|tojson }},
                    {{ this.options|tojavascript }}
                );
            {% endmacro %}
            """)

    fmap = folium.Map(location=[center_lat, center_lon], tiles=None, control_scale=True)
    folium.TileLayer(xyz.OpenTopoMap, name="OpenTopoMap").add_to(fmap)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap", show=False).add_to(fmap)
    # The geoportal WMS does not support Web Mercator, so tiles are requested in WGS84
    Wgs84WmsTileLayer(ORTHOPHOTO_WMS, layers="Raster", fmt="image/jpeg", version="1.3.0",
                      name="Orthophoto (geoportal.gov.pl)", attr="GUGiK",
                      overlay=False, show=False, crs=JsCode("L.CRS.EPSG4326")).add_to(fmap)

    folium.GeoJson(_search_area(center_lat, center_lon, radius).__geo_interface__,
                   name="Search area", interactive=False,
                   style_function=lambda _: {"color": "black", "weight": 1,
                                             "dashArray": "6", "fill": False}).add_to(fmap)

    groups = [
        ("Lines", [r for r in results if not r["over_obstacles"]], COLOR_VALID),
        ("Lines over trees/buildings (NMPT)", [r for r in results if r["over_obstacles"]],
         COLOR_OBSTACLE),
    ]
    for name, group, color in groups:
        if not group:
            continue
        features = [_line_feature(r) for r in group]
        folium.GeoJson(
            {"type": "FeatureCollection", "features": features},
            name=f"{name} ({len(group)})",
            style_function=lambda _, c=color: {"color": c, "weight": 3, "opacity": 0.9},
            highlight_function=lambda _: {"weight": 6},
            tooltip=folium.GeoJsonTooltip(fields=["length_m", "clearance_m"],
                                          aliases=["Length [m]", "Min. clearance [m]"]),
            popup=folium.GeoJsonPopup(fields=["info"], labels=False),
        ).add_to(fmap)

    folium.Marker([center_lat, center_lon], tooltip="Search centre").add_to(fmap)
    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.fit_bounds(_search_area(center_lat, center_lon, radius).total_bounds[[1, 0, 3, 2]]
                    .reshape(2, 2).tolist())
    fmap.save(path)


def _line_feature(r: Dict) -> Dict:
    (lat1, lon1), (lat2, lon2) = r["anchor1"], r["anchor2"]
    info = (f"<b>Length:</b> {r['length']:.0f} m<br>"
            f"<b>Anchor 1:</b> {lat1:.6f}, {lon1:.6f} ({r['elev_a1']:.1f} m)<br>"
            f"<b>Anchor 2:</b> {lat2:.6f}, {lon2:.6f} ({r['elev_a2']:.1f} m)<br>"
            f"<b>Min. clearance over terrain:</b> {r['clearance']:.1f} m")
    if r["clearance_nmpt"] is not None:
        info += f"<br><b>Min. clearance over trees/buildings:</b> {r['clearance_nmpt']:.1f} m"
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[lon1, lat1], [lon2, lat2]]},
        "properties": {"length_m": round(r["length"]), "clearance_m": round(r["clearance"], 1),
                       "info": info},
    }
