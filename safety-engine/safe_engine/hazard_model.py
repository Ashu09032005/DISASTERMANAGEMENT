"""M1 — calibrated, multi-horizon hazard model.

Stage 1 (static susceptibility prior)
    Logistic regression on terrain-only features (depression depth, flow
    accumulation, height above drainage, elevation, slope) -> prior p0(x).
Stage 2 (temporal trigger)
    Gradient-boosted trees on logit(p0) + rainfall history + nowcast rainfall
    over the horizon + the physics-lite intensity estimate -> raw score.
Calibration
    Isotonic regression fitted on separate calibration storms, so that the
    output is a probability usable directly in the routing chance constraint.

Classes: flood (truth depth >= 0.15 m at t+tau) and landslide (slope failure
triggered by t+tau). Storm/wind is handled analytically from the forecast
wind distribution (see :func:`storm_probability`).
"""
from __future__ import annotations

import math
import os
import pickle
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, f1_score, roc_auc_score
from sklearn.preprocessing import StandardScaler

from .scenario import DT_S, WaterBalance
from .terrain import TerrainGrid, catchment_multiplier

FLOOD_LABEL_DEPTH = 0.15
# Depth-exceedance levels (m). One calibrated head per level gives P(depth > level), so the
# routing check can use the probability that matters for each vehicle's wading limit.
FLOOD_LEVELS = (0.15, 0.25, 0.40, 0.60)
NOMINAL_DRAINAGE_MMH = 20.0

FLOOD_STATIC = ["fill_depth", "log_acc", "hand", "elev", "slope"]
FLOOD_DYNAMIC = ["rain_now", "acc_1h", "acc_3h", "acc_total", "basin_3h", "fc_acc", "est_depth_now", "est_depth_tau", "tau_min"]
LS_STATIC = ["ls_s", "slope", "elev"]
LS_DYNAMIC = ["acc_total", "acc_3h", "fc_acc", "ls_ratio", "tau_min"]


# ============================================================================ hydrological state

class HydroState:
    """Running estimate of hydrological state from *observed* rainfall only."""

    def __init__(self, terrain: TerrainGrid, start_t: float):
        self.terrain = terrain
        c = catchment_multiplier(terrain.acc)
        self.wb = WaterBalance(terrain, np.full_like(terrain.elev, NOMINAL_DRAINAGE_MMH), c)
        self.t = start_t
        self.acc_total = np.zeros_like(terrain.elev)
        self.hist: deque = deque(maxlen=36)  # 3 h of 5-min rain fields
        self.basin_hist: deque = deque(maxlen=36)
        self.last_rain = np.zeros_like(terrain.elev)

    def advance_to(self, t: float, weather) -> None:
        while self.t + DT_S <= t + 1e-6:
            self.t += DT_S
            r = weather.observed_rain(self.t)
            self.wb.step(r)
            mm = r * DT_S / 3600.0
            self.acc_total += mm
            self.hist.append(mm)
            self.basin_hist.append(float(mm.mean()))
            self.last_rain = r

    def acc_last(self, steps: int) -> np.ndarray:
        h = list(self.hist)[-steps:]
        return np.sum(h, axis=0) if h else np.zeros_like(self.acc_total)


@dataclass
class HorizonInputs:
    tau_s: int
    flood_X: np.ndarray
    ls_X: np.ndarray
    est_depth: np.ndarray
    fc_acc: np.ndarray
    wind_fc: np.ndarray
    wind_sigma: float


def build_inputs(state: HydroState, weather, issue_t: float, horizons_s) -> list[HorizonInputs]:
    T = state.terrain
    n = T.elev.size
    static_f = np.column_stack([T.fill_depth.ravel(), np.log1p(T.acc).ravel(), T.hand.ravel(), T.elev.ravel(), T.slope_deg.ravel()])
    ls_static = np.column_stack([T.landslide_s.ravel(), T.slope_deg.ravel(), T.elev.ravel()])
    rain_now = state.last_rain.ravel()
    acc_1h, acc_3h = state.acc_last(12).ravel(), state.acc_last(36).ravel()
    acc_total = state.acc_total.ravel()
    basin_3h = np.full(n, float(np.sum(state.basin_hist)))
    est_now = state.wb.depth().ravel()
    nominal_thr = (70 + 220 * (1 - T.landslide_s)).ravel()

    wb = state.wb.copy()
    fc_acc = np.zeros_like(T.elev)
    out: list[HorizonInputs] = []
    hs = sorted(set(int(h) for h in horizons_s))
    max_h = max(hs)
    snapshots = {0: (wb.depth().copy(), fc_acc.copy())}
    lead = 0
    while lead < max_h:
        lead += DT_S
        r = weather.forecast_rain(issue_t, lead)
        wb.step(r)
        fc_acc += r * DT_S / 3600.0
        if lead in hs:
            snapshots[lead] = (wb.depth().copy(), fc_acc.copy())
    for h in hs:
        if h not in snapshots:  # horizon not aligned to 5 min: use nearest earlier snapshot
            k = max(k for k in snapshots if k <= h)
            snapshots[h] = snapshots[k]
        d_tau, fa = snapshots[h]
        tau = np.full(n, h / 60.0)
        flood_X = np.column_stack([static_f, rain_now, acc_1h, acc_3h, acc_total, basin_3h, fa.ravel(), est_now, d_tau.ravel(), tau])
        ratio = (acc_total + fa.ravel()) / nominal_thr
        ls_X = np.column_stack([ls_static, acc_total, acc_3h, fa.ravel(), ratio, tau])
        out.append(HorizonInputs(h, flood_X, ls_X, d_tau, fa, weather.forecast_wind(issue_t, h), weather.wind_sigma(h)))
    return out


# ============================================================================ model

class TwoStageCalibrated:
    def __init__(self, n_static: int, seed: int = 0):
        self.n_static = n_static
        self.scaler = StandardScaler()
        self.prior = LogisticRegression(max_iter=500, class_weight="balanced")
        self.gbm = HistGradientBoostingClassifier(max_iter=320, learning_rate=0.06, max_leaf_nodes=56,
                                                  min_samples_leaf=40, l2_regularization=1.2,
                                                  class_weight="balanced", random_state=seed)
        self.iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)

    def _stage2(self, X):
        s = self.scaler.transform(X[:, : self.n_static])
        p0 = np.clip(self.prior.predict_proba(s)[:, 1], 1e-4, 1 - 1e-4)
        return np.column_stack([np.log(p0 / (1 - p0)), X])

    def fit(self, X, y, X_cal, y_cal, y_static=None):
        self.scaler.fit(X[:, : self.n_static])
        ys = y if y_static is None else y_static
        self.prior.fit(self.scaler.transform(X[:, : self.n_static]), ys)
        self.gbm.fit(self._stage2(X), y)
        raw = self.gbm.predict_proba(self._stage2(X_cal))[:, 1]
        self.iso.fit(raw, y_cal)
        return self

    def raw(self, X):
        return self.gbm.predict_proba(self._stage2(X))[:, 1]

    def predict(self, X):
        return self.iso.predict(self.raw(X))

    def uncertainty(self, X) -> np.ndarray:
        """Epistemic spread: |raw GBM score − isotonic score| (wider when calibration shifts mass)."""
        raw = self.raw(X)
        cal = self.iso.predict(raw)
        return np.abs(raw - cal)


@dataclass
class HazardModel:
    flood: TwoStageCalibrated                      # level 0.15 m (kept for compatibility)
    landslide: TwoStageCalibrated
    metrics: dict = field(default_factory=dict)
    version: str = "m1-3"
    flood_levels: dict = field(default_factory=dict)   # level -> TwoStageCalibrated

    def predict(self, hi: HorizonInputs) -> dict[str, np.ndarray]:
        levels = []
        prev = None
        for lv in FLOOD_LEVELS:
            m = self.flood_levels.get(lv) or (self.flood if lv == FLOOD_LABEL_DEPTH else None)
            if m is None:
                continue
            p = m.predict(hi.flood_X)
            if prev is not None:
                p = np.minimum(p, prev)          # exceedance probabilities must decrease with depth
            levels.append((lv, p))
            prev = p
        return {
            "flood": levels[0][1],
            "flood_levels": levels,
            "landslide": self.landslide.predict(hi.ls_X),
            "storm": storm_probability(hi.wind_fc.ravel(), hi.wind_sigma),
        }

    def save(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import sklearn
        with open(path, "wb") as fh:
            pickle.dump({"model": self, "sklearn": sklearn.__version__}, fh)

    @staticmethod
    def load(path: str) -> "HazardModel | None":
        import sklearn
        if not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            obj = pickle.load(fh)
        if obj.get("sklearn") != sklearn.__version__:
            return None
        return obj["model"]


def storm_probability(wind_fc: np.ndarray, sigma: float, threshold_kmh: float = 50.0) -> np.ndarray:
    """P(wind > threshold) under a Gaussian forecast error."""
    z = (threshold_kmh - wind_fc) / (sigma * math.sqrt(2))
    return 0.5 * _erfc(z)


def _erfc(z):
    # vectorised erfc via numpy (Abramowitz-Stegun 7.1.26, |err| < 1.5e-7)
    s = np.sign(z)
    a = np.abs(z)
    t = 1 / (1 + 0.3275911 * a)
    y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-a * a)
    erf = s * y
    return 1 - erf


# ============================================================================ metrics

def ece(y, p, bins: int = 15) -> float:
    y, p = np.asarray(y), np.asarray(p)
    edges = np.linspace(0, 1, bins + 1)
    tot = 0.0
    for i in range(bins):
        m = (p >= edges[i]) & (p < edges[i + 1] if i < bins - 1 else p <= edges[i + 1])
        if m.any():
            tot += m.mean() * abs(p[m].mean() - y[m].mean())
    return float(tot)


def score(y, p) -> dict:
    y = np.asarray(y).astype(int)
    out = {"n": int(len(y)), "base_rate": float(y.mean())}
    if 0 < y.sum() < len(y):
        out.update(auc_pr=float(average_precision_score(y, p)), roc_auc=float(roc_auc_score(y, p)))
    out.update(brier=float(brier_score_loss(y, p)), ece=ece(y, p), f1_at_0_5=float(f1_score(y, p >= 0.5, zero_division=0)))
    return out
