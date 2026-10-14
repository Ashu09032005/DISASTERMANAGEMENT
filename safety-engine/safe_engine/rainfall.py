"""Rainfall / wind providers.

The engine only talks to the :class:`WeatherProvider` interface:

* ``observed_rain(t)``           – current rain rate field (mm/h), e.g. radar / IMD AWS gridded
* ``forecast_rain(issue, lead)`` – nowcast/forecast rain rate field at issue+lead
* ``forecast_wind(issue, lead)`` – forecast wind speed field (km/h)
* ``wind_sigma(lead)``           – forecast wind error standard deviation (km/h)
* ``data_time()``                – timestamp of the newest observation (for freshness checks)

``ScenarioWeather`` wraps a synthetic :class:`StormScenario`.
``LiveWeather`` queries a point-forecast API (Open-Meteo compatible) on a coarse
lattice and interpolates it to the grid; it needs internet access at runtime.
"""
from __future__ import annotations

import time
from typing import Protocol

import numpy as np

from .terrain import TerrainGrid


class WeatherProvider(Protocol):
    def observed_rain(self, t: float) -> np.ndarray: ...
    def forecast_rain(self, issue_t: float, lead_s: float) -> np.ndarray: ...
    def forecast_wind(self, issue_t: float, lead_s: float) -> np.ndarray: ...
    def wind_sigma(self, lead_s: float) -> float: ...
    def data_time(self, now: float) -> float: ...


class ScenarioWeather:
    synthetic = True

    def __init__(self, scenario):
        self.sc = scenario

    def observed_rain(self, t):
        return self.sc.observed_rain(t)

    def forecast_rain(self, issue_t, lead_s):
        return self.sc.forecast_rain(issue_t, lead_s)

    def forecast_wind(self, issue_t, lead_s):
        return self.sc.forecast_wind(issue_t, lead_s)

    def wind_sigma(self, lead_s):
        return self.sc.wind_sigma(lead_s)

    def data_time(self, now):
        return now  # simulated radar is always current


class LiveWeather:
    """Point forecasts on an n×n lattice, bilinearly interpolated to the grid.

    Uses the Open-Meteo forecast API format (``minutely_15`` precipitation and
    wind speed). Results are cached for ``cache_s`` seconds.
    """

    synthetic = False

    def __init__(self, terrain: TerrainGrid, api_url: str, lattice: int = 4, cache_s: int = 600, client=None):
        import httpx
        self.t = terrain
        self.url = api_url
        self.n = lattice
        self.cache_s = cache_s
        self.client = client or httpx.Client(timeout=15)
        self._cache = None
        self._fetched = 0.0

    def _fetch(self):
        if self._cache is not None and time.time() - self._fetched < self.cache_s:
            return self._cache
        fr, W, H = self.t.frame, self.t.nx * self.t.cell, self.t.ny * self.t.cell
        lats, lons, pts = [], [], []
        for j in range(self.n):
            for i in range(self.n):
                lon, lat = fr.to_lonlat(W * i / (self.n - 1), H * j / (self.n - 1))
                lons.append(round(lon, 4)); lats.append(round(lat, 4)); pts.append((i, j))
        r = self.client.get(self.url, params={
            "latitude": ",".join(map(str, lats)), "longitude": ",".join(map(str, lons)),
            "minutely_15": "precipitation,wind_speed_10m", "forecast_minutely_15": 12,
            "past_minutely_15": 12, "timeformat": "unixtime"})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data = [data]
        times = np.array(data[0]["minutely_15"]["time"], dtype=float)
        rain = np.zeros((len(times), self.n, self.n))
        wind = np.zeros_like(rain)
        for k, (i, j) in enumerate(pts):
            m = data[k]["minutely_15"]
            rain[:, j, i] = np.array(m["precipitation"], dtype=float) * 4.0  # mm per 15 min -> mm/h
            wind[:, j, i] = np.array(m["wind_speed_10m"], dtype=float)
        self._cache = (times, rain, wind)
        self._fetched = time.time()
        return self._cache

    def _interp(self, lattice_vals: np.ndarray) -> np.ndarray:
        ny, nx = self.t.ny, self.t.nx
        yi = np.linspace(0, self.n - 1, ny)
        xi = np.linspace(0, self.n - 1, nx)
        y0 = np.clip(np.floor(yi).astype(int), 0, self.n - 2)
        x0 = np.clip(np.floor(xi).astype(int), 0, self.n - 2)
        fy, fx = (yi - y0)[:, None], (xi - x0)[None, :]
        a, b = lattice_vals[y0][:, x0], lattice_vals[y0][:, x0 + 1]
        c, d = lattice_vals[y0 + 1][:, x0], lattice_vals[y0 + 1][:, x0 + 1]
        return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy

    def _at(self, which: int, t: float) -> np.ndarray:
        times, rain, wind = self._fetch()
        k = int(np.clip(np.searchsorted(times, t, side="right") - 1, 0, len(times) - 1))
        return self._interp((rain if which == 0 else wind)[k])

    def observed_rain(self, t):
        return self._at(0, t)

    def forecast_rain(self, issue_t, lead_s):
        return self._at(0, issue_t + lead_s)

    def forecast_wind(self, issue_t, lead_s):
        return self._at(1, issue_t + lead_s)

    def wind_sigma(self, lead_s):
        return 8.0 + 12.0 * lead_s / 3600.0

    def data_time(self, now):
        return self._fetched or 0.0
