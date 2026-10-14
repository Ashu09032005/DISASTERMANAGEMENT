"""Train and evaluate the M1 hazard model on simulated storms.

    python -m safe_engine.train            # trains, evaluates, writes models/hazard_model.pkl

Storms are split by event (train / calibration / test) so that test storms are
never seen during training or calibration. The baseline (B3) reproduces the
original project's approach: a Random Forest on city-level weather only.

The training data are SYNTHETIC (see scenario.py). For deployment, replace
``generate_dataset`` with rows built from observed rainfall (IMD / radar) and
recorded road-flooding / landslide events.
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
from sklearn.ensemble import RandomForestClassifier

from .config import settings
from .geo import LocalFrame
from .hazard_model import (FLOOD_DYNAMIC, FLOOD_STATIC, LS_DYNAMIC, LS_STATIC, FLOOD_LABEL_DEPTH, FLOOD_LEVELS,
                           HazardModel, HydroState, TwoStageCalibrated, build_inputs, score)
from .rainfall import ScenarioWeather
from .scenario import StormScenario, TruthSimulator
from .terrain import synthetic_terrain

TRAIN_SEEDS = [11, 12, 13, 14, 15, 16]
CAL_SEEDS = [17, 18]
TEST_SEEDS = [19, 20]
START = 1_800_000_000.0


def make_terrain():
    return synthetic_terrain(LocalFrame(settings.region_lon0, settings.region_lat0), settings.region_width_m,
                             settings.region_height_m, settings.cell_m, seed=settings.seed)


def generate_dataset(terrain, seeds, cells_per_issue=800, issue_every_s=1200, rng_seed=0):
    horizons = [h * 60 for h in settings.horizons_min]
    rng = np.random.default_rng(rng_seed)
    F, L, Fy, Ly, Fys, B, D = [], [], [], [], [], [], []
    n = terrain.elev.size
    for sd in seeds:
        sc = StormScenario(terrain, sd, START)
        truth = TruthSimulator(terrain, sc)
        weather = ScenarioWeather(sc)
        ever = (truth.depth.max(axis=0) >= FLOOD_LABEL_DEPTH).ravel()
        state = HydroState(terrain, START)
        for t in np.arange(START, START + sc.duration_s + 1, issue_every_s):
            state.advance_to(t, weather)
            for hi in build_inputs(state, weather, t, horizons):
                idx = rng.choice(n, cells_per_issue, replace=False)
                F.append(hi.flood_X[idx]); L.append(hi.ls_X[idx])
                dep = truth.depth_at(t + hi.tau_s).ravel()[idx]
                Fy.append(dep >= FLOOD_LABEL_DEPTH)
                D.append(dep)
                Ly.append(truth.landslide_at(t + hi.tau_s).ravel()[idx])
                Fys.append(ever[idx])
                X = hi.flood_X[idx]
                # baseline B3: city-level weather only (no terrain, no cell information)
                city = np.column_stack([np.full(len(idx), X[:, 5].mean()), np.full(len(idx), X[:, 7].mean()),
                                        np.full(len(idx), X[:, 8].mean()), np.full(len(idx), X[:, 10].mean()), X[:, -1]])
                B.append(city)
    return (np.vstack(F), np.concatenate(Fy).astype(int), np.concatenate(Fys).astype(int),
            np.vstack(L), np.concatenate(Ly).astype(int), np.vstack(B), np.concatenate(D))


def train(out_path: str | None = None, verbose: bool = True) -> HazardModel:
    t0 = time.time()
    terrain = make_terrain()
    log = print if verbose else (lambda *a, **k: None)
    log("generating training data (synthetic storms)…")
    Ftr, Fytr, Fystr, Ltr, Lytr, Btr, Dtr = generate_dataset(terrain, TRAIN_SEEDS, rng_seed=1)
    Fca, Fyca, _, Lca, Lyca, _, Dca = generate_dataset(terrain, CAL_SEEDS, rng_seed=2)
    Fte, Fyte, _, Lte, Lyte, Bte, Dte = generate_dataset(terrain, TEST_SEEDS, rng_seed=3)
    log(f"rows: train={len(Fytr)} cal={len(Fyca)} test={len(Fyte)}  ({time.time()-t0:.1f}s)")

    flood = TwoStageCalibrated(len(FLOOD_STATIC), seed=1).fit(Ftr, Fytr, Fca, Fyca, y_static=Fystr)
    ls = TwoStageCalibrated(len(LS_STATIC), seed=2).fit(Ltr, Lytr, Lca, Lyca)
    levels = {FLOOD_LABEL_DEPTH: flood}
    for i, lv in enumerate(FLOOD_LEVELS):
        if lv == FLOOD_LABEL_DEPTH:
            continue
        levels[lv] = TwoStageCalibrated(len(FLOOD_STATIC), seed=10 + i).fit(
            Ftr, (Dtr >= lv).astype(int), Fca, (Dca >= lv).astype(int), y_static=Fystr)
        log(f"trained flood level {lv} m")

    sub = np.random.default_rng(4).choice(len(Fytr), min(200_000, len(Fytr)), replace=False)
    rf = RandomForestClassifier(n_estimators=100, min_samples_leaf=20, n_jobs=-1, random_state=0).fit(Btr[sub], Fytr[sub])

    metrics = {
        "data": "SYNTHETIC storms (scenario.py); event-split train/cal/test",
        "seeds": {"train": TRAIN_SEEDS, "calibration": CAL_SEEDS, "test": TEST_SEEDS},
        "flood": {
            "M1_calibrated": score(Fyte, flood.predict(Fte)),
            "M1_uncalibrated": score(Fyte, flood.raw(Fte)),
            "B3_city_weather_RF": score(Fyte, rf.predict_proba(Bte)[:, 1]),
            "physics_only_est_depth": score(Fyte, np.clip(Fte[:, FLOOD_STATIC.__len__() + FLOOD_DYNAMIC.index("est_depth_tau")] / 0.3, 0, 1)),
        },
        "flood_levels": {f"depth>={lv}m": score((Dte >= lv).astype(int), m.predict(Fte)) for lv, m in levels.items()},
        "landslide": {
            "M1_calibrated": score(Lyte, ls.predict(Lte)),
            "M1_uncalibrated": score(Lyte, ls.raw(Lte)),
        },
        "train_seconds": round(time.time() - t0, 1),
    }
    model = HazardModel(flood=flood, landslide=ls, metrics=metrics, flood_levels=levels)
    path = out_path or os.path.join(settings.model_dir, "hazard_model.pkl")
    model.save(path)
    with open(os.path.join(os.path.dirname(path), "hazard_model_metrics.json"), "w") as fh:
        json.dump(metrics, fh, indent=2)
    log(json.dumps(metrics, indent=2))
    return model


def load_or_train() -> HazardModel:
    path = os.path.join(settings.model_dir, "hazard_model.pkl")
    m = HazardModel.load(path)
    return m if m is not None else train(path, verbose=True)


if __name__ == "__main__":
    train()
