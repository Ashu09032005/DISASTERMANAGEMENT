"""Coordinate helpers.

All internal geometry is kept in a local metric frame (metres east / north of a
reference point) so that buffers, distances and grid lookups are exact enough
for a city-sized region. The API converts to and from WGS84 lon/lat.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

M_PER_DEG_LAT = 110_540.0
M_PER_DEG_LON_EQ = 111_320.0


@dataclass(frozen=True)
class LocalFrame:
    """Equirectangular projection around (lon0, lat0). Error < 0.5% within ~50 km."""

    lon0: float
    lat0: float

    @property
    def kx(self) -> float:
        return M_PER_DEG_LON_EQ * math.cos(math.radians(self.lat0))

    def to_xy(self, lon: float, lat: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * M_PER_DEG_LAT

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        return self.lon0 + x / self.kx, self.lat0 + y / M_PER_DEG_LAT

    def coords_to_xy(self, coords: Iterable[Sequence[float]]) -> list[tuple[float, float]]:
        return [self.to_xy(c[0], c[1]) for c in coords]

    def coords_to_lonlat(self, coords: Iterable[Sequence[float]]) -> list[tuple[float, float]]:
        return [self.to_lonlat(c[0], c[1]) for c in coords]

    def geom_to_lonlat(self, geom: BaseGeometry) -> BaseGeometry:
        kx = self.kx
        return transform(lambda x, y, z=None: (self.lon0 + x / kx, self.lat0 + y / M_PER_DEG_LAT), geom)

    def geom_to_xy(self, geom: BaseGeometry) -> BaseGeometry:
        kx = self.kx
        return transform(lambda x, y, z=None: ((x - self.lon0) * kx, (y - self.lat0) * M_PER_DEG_LAT), geom)

    def geojson_to_xy(self, gj: dict) -> BaseGeometry:
        return self.geom_to_xy(shape(gj))

    def xy_to_geojson(self, geom: BaseGeometry, precision: int = 6) -> dict:
        g = mapping(self.geom_to_lonlat(geom))
        return _round_geojson(g, precision)


def _round_geojson(obj, nd):
    if isinstance(obj, dict):
        return {k: _round_geojson(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_geojson(v, nd) for v in obj]
    if isinstance(obj, float):
        return round(obj, nd)
    return obj


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def dist(a: Sequence[float], b: Sequence[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
