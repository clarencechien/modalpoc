"""Small, dependency-free geodesy helpers shared by the fetchers and the Blender build.

Local frame: East-North-Up (ENU) metres around the site centre. +X = east, +Y = north,
+Z = up. Blender uses the same right-handed Z-up convention, so ENU maps 1:1 onto
Blender world space. The glTF exporter converts to Y-up on export.
"""
from __future__ import annotations

import math

WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)


def lla_to_ecef(lat_deg: float, lon_deg: float, h: float = 0.0) -> tuple[float, float, float]:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * sl * sl)
    return ((n + h) * cl * math.cos(lon), (n + h) * cl * math.sin(lon), (n * (1 - WGS84_E2) + h) * sl)


def ecef_to_lla(x: float, y: float, z: float) -> tuple[float, float, float]:
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - WGS84_E2))
    for _ in range(6):
        sl = math.sin(lat)
        n = WGS84_A / math.sqrt(1 - WGS84_E2 * sl * sl)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - WGS84_E2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon), h


def enu_matrix(lat_deg: float, lon_deg: float, h: float = 0.0) -> list[list[float]]:
    """4x4 row-major matrix taking ECEF (m) to ENU (m) around the given origin."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sl, cl, so, co = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    ox, oy, oz = lla_to_ecef(lat_deg, lon_deg, h)
    r = [
        [-so, co, 0.0],
        [-sl * co, -sl * so, cl],
        [cl * co, cl * so, sl],
    ]
    t = [-(r[i][0] * ox + r[i][1] * oy + r[i][2] * oz) for i in range(3)]
    return [
        [r[0][0], r[0][1], r[0][2], t[0]],
        [r[1][0], r[1][1], r[1][2], t[1]],
        [r[2][0], r[2][1], r[2][2], t[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]


def lla_to_enu(lat: float, lon: float, h: float, origin: tuple[float, float, float]) -> tuple[float, float, float]:
    m = enu_matrix(*origin)
    x, y, z = lla_to_ecef(lat, lon, h)
    return (
        m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3],
        m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3],
        m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3],
    )


# --- Web Mercator (EPSG:3857) XYZ tile maths ---------------------------------

def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    lr = math.radians(lat)
    y = (1.0 - math.log(math.tan(lr) + 1.0 / math.cos(lr)) / math.pi) / 2.0 * n
    return x, y


def tile_to_lonlat(x: float, y: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


def metres_per_degree(lat_deg: float) -> tuple[float, float]:
    """(m per degree of longitude, m per degree of latitude) at this latitude."""
    lat = math.radians(lat_deg)
    m_lat = 111132.954 - 559.822 * math.cos(2 * lat) + 1.175 * math.cos(4 * lat)
    m_lon = 111412.84 * math.cos(lat) - 93.5 * math.cos(3 * lat)
    return m_lon, m_lat


def bbox_around(lat: float, lon: float, half_size_m: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) box of +-half_size_m around a point."""
    m_lon, m_lat = metres_per_degree(lat)
    dlat, dlon = half_size_m / m_lat, half_size_m / m_lon
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon
