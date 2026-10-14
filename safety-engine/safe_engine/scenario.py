"""Synthetic storm scenarios and the ground-truth hazard simulator.

These are used (a) to run the system fully offline for demonstration and (b) to
generate labelled data and ground truth for the evaluation harness. Nothing in
this module is used by the hazard model at inference time except the
*observed* rainfall and the *forecast* rainfall/wind, which deliberately carry
realistic errors.

ALL OUTPUTS OF THIS MODULE ARE SYNTHETIC. Replace with radar/IMD rainfall,
nowcasts and observed flood records for real deployment.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .terrain import TerrainGrid, catchment_multiplier

DT_S = 300  # simulation step: 5 minutes


@dataclass
class StormParams:
    x0: float
    y0: float
    x1: float
    y1: float
    peak_mmh: float
    radius_m: float
    wind_peak_kmh: float
    wind_radius_m: float
    duration_s: float
    ramp_s: float


class StormScenario:
    def __init__(self, terrain: TerrainGrid, seed: int, start_epoch: float, duration_h: float = 8.0):
        self.terrain = terrain
        self.seed = seed
        self.start = start_epoch
        self.duration_s = duration_h * 3600
        rng = np.random.default_rng(seed + 1000)
        W, H = terrain.nx * terrain.cell, terrain.ny * terrain.cell
        self.params = StormParams(
            x0=rng.uniform(-0.3, 0.1) * W, y0=rng.uniform(0.0, 1.0) * H,
            x1=rng.uniform(0.9, 1.3) * W, y1=rng.uniform(0.0, 1.0) * H,
            peak_mmh=rng.uniform(55, 115), radius_m=rng.uniform(3500, 6500),
            wind_peak_kmh=rng.uniform(55, 100), wind_radius_m=rng.uniform(2500, 4500),
            duration_s=self.duration_s, ramp_s=rng.uniform(1.0, 2.0) * 3600,
        )
        self._X, self._Y = terrain.centers()
        bands = rng.normal(size=terrain.elev.shape)
        self._texture = np.clip(1 + 0.25 * _blur(bands, 3), 0.4, 1.6)

    # ----------------------------------------------------------------- truth fields
    def _center(self, t_rel: float, err=(0.0, 0.0)):
        p = self.params
        f = min(max(t_rel / p.duration_s, 0.0), 1.0)
        return p.x0 + (p.x1 - p.x0) * f + err[0], p.y0 + (p.y1 - p.y0) * f + err[1]

    def _envelope(self, t_rel: float) -> float:
        p = self.params
        if t_rel < 0 or t_rel > p.duration_s:
            return 0.0
        up = min(1.0, t_rel / p.ramp_s)
        down = min(1.0, (p.duration_s - t_rel) / (0.35 * p.duration_s))
        return max(0.0, min(up, down))

    def rain(self, t: float, err=(0.0, 0.0), mult: float = 1.0) -> np.ndarray:
        """Rain rate (mm/h) on the grid at absolute time t."""
        tr = t - self.start
        cx, cy = self._center(tr, err)
        p = self.params
        r2 = (self._X - cx) ** 2 + (self._Y - cy) ** 2
        core = p.peak_mmh * np.exp(-r2 / (2 * p.radius_m ** 2))
        spiral = 0.35 * p.peak_mmh * np.exp(-((np.sqrt(r2) - 2.2 * p.radius_m) ** 2) / (2 * (0.5 * p.radius_m) ** 2))
        return np.maximum(0.0, (core + spiral) * self._texture * self._envelope(tr) * mult + 0.5 * self._envelope(tr))

    def wind(self, t: float, err=(0.0, 0.0), mult: float = 1.0) -> np.ndarray:
        tr = t - self.start
        cx, cy = self._center(tr, err)
        p = self.params
        r = np.sqrt((self._X - cx) ** 2 + (self._Y - cy) ** 2)
        prof = (r / p.wind_radius_m) * np.exp(1 - r / p.wind_radius_m)  # Rankine-like ring
        return p.wind_peak_kmh * prof * self._envelope(tr) * mult

    # ----------------------------------------------------------------- observations / forecasts
    def observed_rain(self, t: float) -> np.ndarray:
        """Radar-like observation of the current rain rate (storm bias + 25 % pixel noise)."""
        rng = np.random.default_rng((self.seed, int(t) // DT_S, 1))
        bias = math.exp(np.random.default_rng((self.seed, 9)).normal(0, 0.15))  # storm-level radar bias
        return self.rain(t) * bias * np.exp(rng.normal(0, 0.25, self._X.shape))

    def _fc_error(self, issue_t: float, lead_s: float):
        rng = np.random.default_rng((self.seed, int(issue_t) // DT_S, 2))
        sd = 500.0 * lead_s / 1800.0
        ex, ey = rng.normal(0, 1, 2) * sd
        mult = math.exp(rng.normal(0, 0.10 + 0.20 * lead_s / 3600.0))
        return (ex, ey), mult

    def forecast_rain(self, issue_t: float, lead_s: float) -> np.ndarray:
        err, mult = self._fc_error(issue_t, lead_s)
        return self.rain(issue_t + lead_s, err, mult)

    def forecast_wind(self, issue_t: float, lead_s: float) -> np.ndarray:
        err, mult = self._fc_error(issue_t, lead_s)
        return self.wind(issue_t + lead_s, err, mult)

    def wind_sigma(self, lead_s: float) -> float:
        return 8.0 + 12.0 * lead_s / 3600.0


def _blur(a: np.ndarray, k: int) -> np.ndarray:
    out = a.copy()
    for _ in range(k):
        out = (out + np.roll(out, 1, 0) + np.roll(out, -1, 0) + np.roll(out, 1, 1) + np.roll(out, -1, 1)) / 5
    return out / (out.std() + 1e-9)


# ============================================================================ water balance

class WaterBalance:
    """Bucket-style ponding + river-stage model.

    The *same equations* are used by the truth simulator (with hidden,
    spatially variable drainage and catchment noise) and by the engine's
    physics-lite intensity estimator (with nominal parameters). The structural
    difference is what the ML model has to learn to correct.
    """

    def __init__(self, terrain: TerrainGrid, drainage_mmh, catch_mult):
        self.t = terrain
        self.drain = drainage_mmh
        self.c = catch_mult
        self.cap = terrain.fill_depth + 0.03          # depressions + 3 cm sheet flow
        self.pond = np.zeros_like(terrain.elev)
        self.stage = 0.0
        self.basin_hist: list[float] = []
        self.near_river = terrain.truth.get("river_dist", None) if terrain.truth else None
        self.floodplain = (terrain.hand < 4.0) & terrain.near_river
        self.k_stage, self.tau_h = 0.0065, 2.5

    def step(self, rain_mmh: np.ndarray, dt_s: float = DT_S):
        dt_h = dt_s / 3600.0
        self.pond = np.clip(self.pond + (rain_mmh * self.c - self.drain) / 1000.0 * dt_h, 0.0, self.cap)
        basin = float(rain_mmh.mean())
        # linear reservoir: stage responds to basin rain with ~2.5 h time constant
        self.stage += (self.k_stage * basin - self.stage / self.tau_h) * dt_h
        self.stage = max(self.stage, 0.0)

    def depth(self) -> np.ndarray:
        river = np.where(self.floodplain, np.maximum(0.0, self.stage - self.t.hand), 0.0)
        return np.maximum(self.pond, river)

    def copy(self) -> "WaterBalance":
        wb = WaterBalance.__new__(WaterBalance)
        wb.__dict__.update(self.__dict__)
        wb.pond = self.pond.copy()
        wb.basin_hist = list(self.basin_hist)
        return wb


class TruthSimulator:
    """Pre-computes ground-truth hazard fields for a scenario at 5-minute steps."""

    def __init__(self, terrain: TerrainGrid, scenario: StormScenario):
        self.terrain, self.sc = terrain, scenario
        tr = terrain.truth
        g = tr.get("true_grid", terrain)          # true surface, not the DEM the model sees
        c = catchment_multiplier(g.acc) * tr["catch_noise"]
        wb = WaterBalance(g, tr["drainage_mmh"], c)
        rng = np.random.default_rng(scenario.seed + 77)
        wb.k_stage = rng.uniform(0.004, 0.009)     # storm-specific river response (unknown to the model)
        wb.tau_h = rng.uniform(1.5, 4.0)
        n = int(scenario.duration_s // DT_S) + 1 + 24  # 2 h tail after the storm
        self.times = scenario.start + np.arange(n) * DT_S
        self.depth = np.zeros((n,) + terrain.elev.shape, dtype=np.float32)
        self.landslide = np.zeros((n,) + terrain.elev.shape, dtype=bool)
        self.wind = np.zeros((n,) + terrain.elev.shape, dtype=np.float32)
        accum = np.zeros_like(terrain.elev)
        trig = np.zeros_like(terrain.elev, dtype=bool)
        for i, t in enumerate(self.times):
            r = scenario.rain(t)
            wb.step(r)
            accum += r * DT_S / 3600.0
            trig |= (accum >= tr["landslide_thr_mm"]) & (g.landslide_s > 0.3)
            self.depth[i] = wb.depth()
            self.landslide[i] = trig
            w = scenario.wind(t)
            self.wind[i] = np.where(tr["trees"] > 0.55, w, w * 0.8)  # debris risk where trees are dense

    def idx(self, t: float) -> int:
        return int(min(max(round((t - self.times[0]) / DT_S), 0), len(self.times) - 1))

    def depth_at(self, t: float) -> np.ndarray:
        return self.depth[self.idx(t)]

    def landslide_at(self, t: float) -> np.ndarray:
        return self.landslide[self.idx(t)]

    def wind_at(self, t: float) -> np.ndarray:
        return self.wind[self.idx(t)]
