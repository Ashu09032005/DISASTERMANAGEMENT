"""OSRM HTTP client (works with a self-hosted OSRM or the public demo server).

Uses ``annotations=duration`` so that each coordinate segment of the returned
geometry has its own travel time; the safety engine needs per-segment ETAs.
"""
from __future__ import annotations

import httpx

from .base import Route


class OsrmEngine:
    name = "osrm"

    def __init__(self, base_url: str, frame, profile: str = "driving", client: httpx.Client | None = None):
        self.base = base_url.rstrip("/")
        self.frame = frame
        self.profile = profile
        self.client = client or httpx.Client(timeout=10)

    def _ll(self, p) -> str:
        lon, lat = self.frame.to_lonlat(p[0], p[1])
        return f"{lon:.6f},{lat:.6f}"

    def _parse(self, r: dict, vias=()) -> Route:
        coords_ll = r["geometry"]["coordinates"]
        durs = []
        for leg in r["legs"]:
            durs.extend(leg.get("annotation", {}).get("duration", []))
        coords = self.frame.coords_to_xy(coords_ll)
        if len(durs) != len(coords) - 1:  # fall back to uniform split
            total = r["duration"]
            n = max(1, len(coords) - 1)
            durs = [total / n] * n
        return Route(coords=coords, seg_dur=durs, via=list(vias), engine=self.name)

    def routes(self, o, d, k: int = 3) -> list[Route]:
        url = f"{self.base}/route/v1/{self.profile}/{self._ll(o)};{self._ll(d)}"
        resp = self.client.get(url, params={"alternatives": str(max(0, k - 1)) if k > 1 else "false",
                                             "overview": "full", "geometries": "geojson", "annotations": "duration"})
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok":
            return []
        return [self._parse(r) for r in data["routes"][:k]]

    def route_via(self, o, vias, d) -> Route | None:
        pts = ";".join(self._ll(p) for p in [o, *vias, d])
        resp = self.client.get(f"{self.base}/route/v1/{self.profile}/{pts}",
                               params={"overview": "full", "geometries": "geojson", "annotations": "duration"})
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        return self._parse(data["routes"][0], vias)

    def nearest(self, p):
        resp = self.client.get(f"{self.base}/nearest/v1/{self.profile}/{self._ll(p)}", params={"number": 1})
        resp.raise_for_status()
        wp = resp.json()["waypoints"][0]["location"]
        return self.frame.to_xy(wp[0], wp[1])
