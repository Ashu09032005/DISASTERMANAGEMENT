"""M2 — probability-parameterised, terrain-specific hazard geometry.

For each hazard class c and forecast interval [t+h_k, t+h_{k+1}) the calibrated
probabilities p_c(x, tau) are turned into polygons by a class-specific
transform:

flood      for each depth level L_j (0.15 / 0.25 / 0.40 / 0.60 m) M1 gives the calibrated
           exceedance probability P(depth > L_j). Cells with P >= P_MIN form a layer of
           polygons whose intensity is the NEXT level up (conservative) and whose probability
           is P(depth > L_j). A vehicle with wading limit W is therefore tested against
           the probability of exceeding the largest level <= W. At the lowest level a cell
           also needs a terrain signal: physics-lite ponding depth >= D0 * (1 - p).
landslide  cell included if p >= P_MIN and static susceptibility exceeds a
           cut-off that decreases as p rises: s >= S0 * (1 - p); buffered by a
           run-out distance. Intensity = 1 (road blocked).
storm      cells with P(wind > 50 km/h) >= P_MIN, buffered by a width scaled by p.
           Intensity = forecast wind speed.

A 3x3 speckle filter is applied to the probabilities first (DEM noise). Cells are then grouped into (intensity band, probability band) classes and each
group is dissolved into polygons; intensity and probability take the *upper*
edge of the band (conservative). Each polygon carries the validity interval.
"""
from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import box

from .hazard_model import FLOOD_LABEL_DEPTH
from .slices import HazardSlice, new_id

P_MIN = 0.10
P_BANDS = [0.10, 0.3, 0.45, 0.6, 0.8, 1.0001]
DEPTH_BANDS = [0.0, 0.15, 0.30, 0.60, 9.0]
DEPTH_REPR = [0.15, 0.30, 0.60, 0.90]
WIND_BANDS = [0, 50, 60, 70, 80, 90, 100, 999]
WIND_REPR = [50, 60, 70, 80, 90, 100, 130]
D0 = 0.10          # flood: depth level at p -> 0
S0 = 0.60          # landslide: susceptibility cut-off at p -> 0
RUNOUT_M = 40.0
STORM_BUFFER_M = 150.0


def _band(v: np.ndarray, edges) -> np.ndarray:
    return np.clip(np.searchsorted(edges, v, side="right") - 1, 0, len(edges) - 2)


def _polys_for_mask(mask: np.ndarray, cell: float, simplify: float):
    rs, cs = np.nonzero(mask)
    if rs.size == 0:
        return []
    boxes = shapely.box(cs * cell, rs * cell, (cs + 1) * cell, (rs + 1) * cell)
    try:
        u = shapely.coverage_union_all(boxes)
    except Exception:  # pragma: no cover - precision edge cases
        u = shapely.union_all(boxes)
    u = u.simplify(simplify, preserve_topology=True)
    geoms = list(u.geoms) if hasattr(u, "geoms") else [u]
    return [g for g in geoms if not g.is_empty and g.area > 0]


def smooth3(a: np.ndarray) -> np.ndarray:
    """3x3 smoothing that removes single-cell speckle caused by DEM noise while
    keeping isolated high-probability cells (max of the cell and the local mean)."""
    p = np.pad(a, 1, mode="edge")
    acc = np.zeros_like(a)
    for dr in (0, 1, 2):
        for dc in (0, 1, 2):
            acc += p[dr:dr + a.shape[0], dc:dc + a.shape[1]]
    mean = acc / 9.0
    return np.where(a >= 0.8, a, 0.5 * a + 0.5 * mean)


def generate_slices(terrain, issue_t: float, horizons_s: list[int], preds: list[dict], inputs: list):
    """preds[k] = {'flood': p, 'landslide': p, 'storm': p} for horizon k (flattened grids).

    Returns (slices, raster) where raster is a list of (t0, t1, codes[3, ny, nx]) giving the
    band code of every cell per class and interval; the hazard store diffs these rasters
    between versions to find the changed region exactly and cheaply."""
    shape = terrain.elev.shape
    cell = terrain.cell
    simplify = cell * 0.25
    out: list[HazardSlice] = []
    raster = []
    hs = list(horizons_s)
    for k, h in enumerate(hs):
        h_next = hs[k + 1] if k + 1 < len(hs) else h + 1800
        t0, t1 = issue_t + h, issue_t + h_next
        nxt = min(k + 1, len(hs) - 1)
        # conservative over the interval: max of both ends
        pf = smooth3(np.maximum(preds[k]["flood"], preds[nxt]["flood"]).reshape(shape))
        pl = smooth3(np.maximum(preds[k]["landslide"], preds[nxt]["landslide"]).reshape(shape))
        ps = np.maximum(preds[k]["storm"], preds[nxt]["storm"]).reshape(shape)
        depth = np.maximum(inputs[k].est_depth, inputs[nxt].est_depth)
        wind = np.maximum(inputs[k].wind_fc, inputs[nxt].wind_fc)

        codes = np.zeros((3,) + shape, dtype=np.int32)
        # ---------------- flood: one layer per depth-exceedance level
        lv_k = dict(preds[k].get("flood_levels") or [(FLOOD_LABEL_DEPTH, preds[k]["flood"])])
        lv_n = dict(preds[nxt].get("flood_levels") or [(FLOOD_LABEL_DEPTH, preds[nxt]["flood"])])
        levels = sorted(lv_k)
        for j, lv in enumerate(levels):
            pj = smooth3(np.maximum(lv_k[lv], lv_n.get(lv, lv_k[lv])).reshape(shape))
            inc = pj >= P_MIN
            if j == 0:
                inc &= np.maximum(depth, np.where(pj >= 0.5, FLOOD_LABEL_DEPTH, 0)) >= D0 * (1 - pj)
            if not inc.any():
                continue
            pb = _band(pj, P_BANDS)
            codes[0][inc] += ((pb + 1) * 8 ** j)[inc]          # unique code per (level, probability band)
            rep = levels[j + 1] if j + 1 < len(levels) else 0.90
            for pi in np.unique(pb[inc]):
                for g in _polys_for_mask(inc & (pb == pi), cell, simplify):
                    out.append(HazardSlice(new_id(), "flood", g, t0, t1, min(P_BANDS[pi + 1], 1.0), rep,
                                           band=f"L{j}p{pi}", note=f"P(depth > {lv:.2f} m)"))
        # ---------------- landslide
        inc = (pl >= P_MIN) & (terrain.landslide_s >= S0 * (1 - pl))
        codes[1][inc] = (_band(pl, P_BANDS) + 1)[inc]
        if inc.any():
            pb = _band(pl, P_BANDS)
            for pi in np.unique(pb[inc]):
                for g in _polys_for_mask(inc & (pb == pi), cell, simplify):
                    out.append(HazardSlice(new_id(), "landslide", g.buffer(RUNOUT_M), t0, t1,
                                           min(P_BANDS[pi + 1], 1.0), 1.0, band=f"p{pi}"))
        # ---------------- storm (wind / debris)
        inc = ps >= P_MIN
        codes[2][inc] = (_band(wind, WIND_BANDS) * 10 + _band(ps, P_BANDS) + 1)[inc]
        if inc.any():
            wb, pb = _band(wind, WIND_BANDS), _band(ps, P_BANDS)
            for wi in np.unique(wb[inc]):
                for pi in np.unique(pb[inc & (wb == wi)]):
                    p_rep = min(P_BANDS[pi + 1], 1.0)
                    for g in _polys_for_mask(inc & (wb == wi) & (pb == pi), cell, simplify):
                        out.append(HazardSlice(new_id(), "storm", g.buffer(STORM_BUFFER_M * p_rep), t0, t1,
                                               p_rep, float(WIND_REPR[wi]), band=f"w{wi}p{pi}"))
        raster.append((t0, t1, codes))
    return out, raster
