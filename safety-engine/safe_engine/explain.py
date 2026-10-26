"""M7 — auditable spatio-temporal route decision explanations.

Every safety recommendation can be accompanied by a *decision record* that ties
together (i) M3 chunk-level blocking and soft-risk evidence, (ii) counterfactual
departure times, (iii) comparison to the present-tense baseline, and (iv) local
M1 feature attribution at blocking locations. Records are deterministic given
engine state — suitable for audit logs, patent exhibits, and reviewer demos.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .assistant import templates as TPL
from .feasibility import chunk_route
from .hazard_model import FLOOD_DYNAMIC, FLOOD_LABEL_DEPTH, FLOOD_STATIC, LS_DYNAMIC, LS_STATIC, build_inputs
from .vehicles import get_profile

IST = timezone(timedelta(hours=5, minutes=30))

FLOOD_NAMES = FLOOD_STATIC + FLOOD_DYNAMIC
LS_NAMES = LS_STATIC + LS_DYNAMIC

_HAZARD_VERB = {
    "en": {"flood": "ponding / flooding", "landslide": "landslide debris", "storm": "strong wind", "closure": "official closure"},
    "hi": {"flood": "जल भराव / बाढ़", "landslide": "भूस्खलन", "storm": "तेज़ हवा", "closure": "आधिकारिक बंद"},
    "ta": {"flood": "வெள்ளம் / நீர் தேங்கல்", "landslide": "நிலச்சரிவு", "storm": "பலத்த காற்று", "closure": "அதிகாரப்பூர்வ மூடல்"},
}


def _fmt_t(t: float) -> str:
    return datetime.fromtimestamp(t, IST).strftime("%H:%M IST")


def _feature_labels(names: list[str]) -> dict[str, str]:
    pretty = {
        "fill_depth": "terrain depression depth",
        "log_acc": "upstream catchment area",
        "hand": "height above drainage",
        "elev": "elevation",
        "slope": "slope",
        "rain_now": "rainfall now",
        "acc_1h": "rain last 1 h",
        "acc_3h": "rain last 3 h",
        "acc_total": "storm rainfall so far",
        "basin_3h": "catchment rain last 3 h",
        "fc_acc": "forecast rain to horizon",
        "est_depth_now": "physics-lite water depth now",
        "est_depth_tau": "physics-lite depth at arrival",
        "tau_min": "forecast horizon (min)",
        "ls_s": "landslide susceptibility",
        "ls_ratio": "rain vs slope trigger ratio",
    }
    return {n: pretty.get(n, n) for n in names}


def attribute_row(model, x_row, names: list[str], top_k: int = 4) -> list[dict]:
    """Leave-one-perturbation attribution on one feature row (M1 stage-2 input)."""
    x = x_row.reshape(1, -1)
    base = float(model.predict(x)[0])
    labels = _feature_labels(names)
    n_static = model.n_static
    out = []
    for j, name in enumerate(names):
        x2 = x.copy()
        if j < n_static:
            x2[0, j] = float(model.scaler.mean_[j])
        else:
            x2[0, j] = 0.0
        delta = base - float(model.predict(x2)[0])
        if abs(delta) < 0.0008:
            continue
        out.append({"feature": name, "label": labels.get(name, name), "delta_p": round(delta, 4),
                    "direction": "increases" if delta > 0 else "reduces"})
    out.sort(key=lambda r: abs(r["delta_p"]), reverse=True)
    return out[:top_k]


def counterfactual_departures(svc, route_id: str, offsets_min=(0, 15, 30, 60)) -> list[dict]:
    c = svc.routes.get(route_id)
    if c is None:
        return []
    veh = get_profile(c["vehicle"])
    now = svc.now()
    rows = []
    for off in offsets_min:
        depart = c["depart_t"] + off * 60 if off else c["depart_t"]
        stale = (now - svc.store.updated_at) > svc.settings.hazard_ttl_s
        a = svc.evaluator.assess(c["route"], depart, veh, svc.store.index, stale=stale)
        rows.append({"depart_offset_min": off, "depart_at": depart, "feasible": a.feasible,
                     "p_safe": round(a.p_safe, 4), "margin_s": None if a.margin_s is None else round(a.margin_s),
                     "reason": a.reason})
    return rows


def local_forecast_at(svc, lon: float, lat: float, horizon_min: int = 60) -> dict | None:
    """Cell-level multi-hazard forecast with feature attribution (M1 + hydro state)."""
    x, y = svc.frame.to_xy(lon, lat)
    T = svc.terrain
    if x < 0 or y < 0 or x >= T.nx * T.cell or y >= T.ny * T.cell:
        return None
    r, c = int(y // T.cell), int(x // T.cell)
    flat = r * T.nx + c
    now = svc.now()
    target_s = horizon_min * 60
    hs = sorted(set(int(h) for h in svc.horizons))
    pick = min(hs, key=lambda h: abs(h - target_s))
    for hi in build_inputs(svc.state, svc.weather, now, hs):
        if hi.tau_s != pick:
            continue
        preds = svc.model.predict(hi)
        flood_p = float(preds["flood"].ravel()[flat])
        ls_p = float(preds["landslide"].ravel()[flat])
        storm_p = float(preds["storm"].ravel()[flat])
        depth_est = float(hi.est_depth.ravel()[flat])
        fx = hi.flood_X[flat]
        lx = hi.ls_X[flat]
        fl = svc.model.flood_levels.get(FLOOD_LABEL_DEPTH) or svc.model.flood
        return {
            "lon": lon, "lat": lat, "horizon_min": round(pick / 60),
            "issued_at": now, "valid_until": now + pick,
            "probabilities": {
                "flood_depth_ge_15cm": round(flood_p, 4),
                "landslide": round(ls_p, 4),
                "storm_wind_ge_50kmh": round(storm_p, 4),
            },
            "physics_lite_depth_m": round(depth_est, 3),
            "attribution": {
                "flood": attribute_row(fl, fx, FLOOD_NAMES),
                "landslide": attribute_row(svc.model.landslide, lx, LS_NAMES),
            },
            "evidence_nearby": len(svc.hazards_near(lon, lat, 1500, now, now + pick)),
            "synthetic": svc.synthetic,
        }
    return None


def build_decision_record(svc, route_id: str, lang: str = "en") -> dict | None:
    """Full auditable record for one assessed route."""
    c = svc.routes.get(route_id)
    if c is None:
        return None
    lang = lang if lang in ("en", "hi", "ta") else "en"
    r = c["route"]
    veh = get_profile(c["vehicle"])
    depart = c["depart_t"]
    stale = (svc.now() - svc.store.updated_at) > svc.settings.hazard_ttl_s
    a = svc.evaluator.assess(r, depart, veh, svc.store.index, stale=stale)
    baseline = svc.evaluator.assess(r, depart, veh, svc.store.index, time_aware=False, vehicle_aware=False, hard_only=True)

    blockers = []
    for b in a.blocked[:8]:
        lon, lat = svc.frame.to_lonlat(*b["at"])
        local = local_forecast_at(svc, lon, lat, horizon_min=max(15, int((b["arrival"] - depart) / 60)))
        cls = b["class"]
        verb = _HAZARD_VERB.get(lang, _HAZARD_VERB["en"]).get(cls, cls)
        inten = b["intensity"]
        if cls == "flood":
            inten_txt = f"{int(round(inten * 100))} cm water (model band)"
        elif cls == "storm":
            inten_txt = f"{int(round(inten))} km/h wind band"
        else:
            inten_txt = cls
        blockers.append({
            **b, "lon": lon, "lat": lat,
            "arrival_time": _fmt_t(b["arrival"]),
            "summary": f"{verb} · {inten_txt} · {int(round(b['p'] * 100))}% likely when you arrive",
            "local_forecast": local,
        })

    cf = counterfactual_departures(svc, route_id)
    better_later = next((x for x in cf if x["depart_offset_min"] > 0 and x["feasible"] and not a.feasible), None)

    narrative_parts = []
    if a.feasible:
        m = None if a.margin_s is None else max(0, round(a.margin_s / 60))
        narrative_parts.append(TPL.render("route_ok", lang, route_id=route_id,
                                          distance_km=f"{r.distance / 1000:.1f}", duration_min=round(r.duration / 60),
                                          p_safe=round(a.p_safe * 100), margin_text=TPL.render("margin", lang, margin_min=m) if m is not None else TPL.render("margin_none", lang)))
        if a.risks:
            narrative_parts.append(f"Soft risks on path: {len(a.risks)} area(s) below hard-block threshold; combined safety probability {round(a.p_safe * 100)}%.")
    else:
        if blockers:
            narrative_parts.append(f"Blocked by {blockers[0]['summary']} at {blockers[0]['arrival_time']}.")
        elif a.reason:
            narrative_parts.append(a.reason)
        if baseline.feasible and not a.feasible:
            narrative_parts.append("Present-tense hazard maps would treat this route as passable now, but it intersects a hazard during your actual arrival window.")

    if better_later:
        narrative_parts.append(f"Leaving {better_later['depart_offset_min']} min later may become feasible (safety probability {round(better_later['p_safe'] * 100)}%).")

    chunks = chunk_route(r, svc.settings.chunk_m)
    return {
        "route_id": route_id,
        "vehicle": veh.to_dict(),
        "lang": lang,
        "feasible": a.feasible,
        "p_safe": round(a.p_safe, 4),
        "margin_s": a.margin_s,
        "reason": a.reason,
        "narrative": " ".join(narrative_parts),
        "blockers": blockers,
        "soft_risks": a.risks[:10],
        "counterfactual_departures": cf,
        "baseline_present_tense": {"feasible": baseline.feasible, "note": "Static polygon at departure only (original design baseline B1)."},
        "method": {
            "time_aware": True,
            "vehicle_aware": True,
            "chance_constraint_alpha": svc.settings.alpha + (1 - svc.settings.alpha) * 0.5 if stale else svc.settings.alpha,
            "chunks_evaluated": len(chunks),
            "hazard_version": svc.store.version,
        },
        "synthetic": svc.synthetic,
    }
