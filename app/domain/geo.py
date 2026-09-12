"""Coordinate handling.

Framework §6.5 / §11: GCJ-02 (amap/tencent), BD-09 (baidu) and WGS-84 (GPS)
differ by hundreds of metres. Every coordinate entering the system carries its
CRS, and comparisons happen only after normalisation.
"""

from __future__ import annotations

import math
from enum import StrEnum

from pydantic import BaseModel, Field

from app.errors import CoordinateSystemMismatch


class Crs(StrEnum):
    WGS84 = "wgs84"  # GPS, OpenStreetMap
    GCJ02 = "gcj02"  # amap, tencent
    BD09 = "bd09"  # baidu


_A = 6378245.0
_EE = 0.00669342162296594323
_X_PI = math.pi * 3000.0 / 180.0


def _out_of_china(lat: float, lon: float) -> bool:
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lat + dlat, lon + dlon


def gcj02_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
    if _out_of_china(lat, lon):
        return lat, lon
    wlat, wlon = lat, lon
    for _ in range(12):
        glat, glon = wgs84_to_gcj02(wlat, wlon)
        dlat, dlon = glat - lat, glon - lon
        if abs(dlat) < 1e-10 and abs(dlon) < 1e-10:
            break
        wlat -= dlat
        wlon -= dlon
    return wlat, wlon


def gcj02_to_bd09(lat: float, lon: float) -> tuple[float, float]:
    z = math.sqrt(lat * lat + lon * lon) + 0.00002 * math.sin(lat * _X_PI)
    theta = math.atan2(lat, lon) + 0.000003 * math.cos(lon * _X_PI)
    return z * math.sin(theta) + 0.006, z * math.cos(theta) + 0.0065


def bd09_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    x = lon - 0.0065
    y = lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    return z * math.sin(theta), z * math.cos(theta)


_TO_WGS84 = {
    Crs.WGS84: lambda lat, lon: (lat, lon),
    Crs.GCJ02: gcj02_to_wgs84,
    Crs.BD09: lambda lat, lon: gcj02_to_wgs84(*bd09_to_gcj02(lat, lon)),
}

_FROM_WGS84 = {
    Crs.WGS84: lambda lat, lon: (lat, lon),
    Crs.GCJ02: wgs84_to_gcj02,
    Crs.BD09: lambda lat, lon: gcj02_to_bd09(*wgs84_to_gcj02(lat, lon)),
}


class Coord(BaseModel):
    """A geographic point that always knows its own CRS."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    crs: Crs = Crs.GCJ02

    def to(self, target: Crs) -> "Coord":
        if target == self.crs:
            return self
        wlat, wlon = _TO_WGS84[self.crs](self.lat, self.lon)
        lat, lon = _FROM_WGS84[target](wlat, wlon)
        return Coord(lat=lat, lon=lon, crs=target)

    def to_wgs84(self) -> "Coord":
        return self.to(Crs.WGS84)

    def distance_m(self, other: "Coord") -> float:
        """Great-circle distance in metres, CRS-independent."""

        a = self.to_wgs84()
        b = other.to_wgs84()
        r = 6_371_008.8
        p1, p2 = math.radians(a.lat), math.radians(b.lat)
        dp = p2 - p1
        dl = math.radians(b.lon - a.lon)
        h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def normalize_key(coord: Coord, precision: int = 4) -> str:
    """Coarse key for cache lookups (framework §7.5).

    Truncating to ``precision`` decimals (~11 m at 4) is what makes geocode
    caching actually hit; full-precision keys essentially never repeat.
    """

    w = coord.to_wgs84()
    return f"{round(w.lat, precision)}:{round(w.lon, precision)}"


def assert_same_crs(coords: list[Coord]) -> None:
    """Guard used before any geometry that assumes a shared projection."""

    crs_set = {c.crs for c in coords}
    if len(crs_set) > 1:
        raise CoordinateSystemMismatch(
            "coordinates from different CRS were combined without normalisation",
            crs=sorted(str(c) for c in crs_set),
        )
