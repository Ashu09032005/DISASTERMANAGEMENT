"""M3 — ETA-aware, vehicle-specific segment feasibility.

For every chunk (~120 m) of a candidate route the arrival interval
[a-, a+] is computed from per-segment travel times with a relative and absolute
uncertainty. Only hazard slices that overlap the chunk *in space and in that
time interval* are retrieved from the spatio-temporal index. A slice is
dangerous for the vehicle when its intensity reaches the vehicle threshold.

* dangerous slice with p >= p_hard          -> chunk blocked (route infeasible)
* dangerous slices with p < p_hard          -> contribute to the route risk
* P_safe(R) = prod over distinct risky slices (1 - p)  (each slice counted once)
  -> route feasible iff no block and P_safe >= alpha
* safety margin = min over chunks of (onset of first blocking slice - a+)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import LineString, Point

from .routing.base import Route
from .vehicles import VehicleProfile


@dataclass
class Chunk:
    geom: LineString
    t0: float        # seconds from departure (start of chunk)
    t1: float        # seconds from departure (end of chunk)
    i0: int
    i1: int
    elevated: bool = False   # bridge / flyover: exempt from ponding (not from wind or closures)


@dataclass
class Assessment:
    route_id: str
    feasible: bool
    p_safe: float
    margin_s: float | None
    blocked: list = field(default_factory=list)     # [{slice_id, class, intensity, p, at:(x,y), arrival}]
    risks: list = field(default_factory=list)       # [{slice_id, class, p}]
    stats: dict = field(default_factory=dict)
    stale: bool = False
    reason: str = ""

    def to_dict(self):
        return {"route_id": self.route_id, "feasible": self.feasible, "p_safe": round(self.p_safe, 4),
                "margin_s": None if self.margin_s is None else round(self.margin_s), "blocked": self.blocked[:20],
                "risks": self.risks[:20], "stats": self.stats, "stale": self.stale, "reason": self.reason}


def chunk_route(route: Route, chunk_m: float) -> list[Chunk]:
    chunks, cur, t, t_start, i_start, length = [], [route.coords[0]], 0.0, 0.0, 0, 0.0
    for i, (a, b) in enumerate(zip(route.coords, route.coords[1:])):
        cur.append(b)
        length += math.hypot(b[0] - a[0], b[1] - a[1])
        t += route.seg_dur[i] if i < len(route.seg_dur) else 0.0
        if length >= chunk_m or i == len(route.coords) - 2 or (route.elevated and i + 1 < len(route.elevated)
                                                                and route.elevated[i + 1] != route.elevated[i]):
            if len(cur) >= 2:
                el = bool(route.elevated) and all(route.elevated[j] for j in range(i_start, i + 1) if j < len(route.elevated))
                chunks.append(Chunk(LineString(cur), t_start, t, i_start, i + 1, el))
            cur, t_start, i_start, length = [b], t, i + 1, 0.0
    return chunks


class FeasibilityEvaluator:
    def __init__(self, settings):
        self.s = settings

    def assess(self, route: Route, depart_t: float, vehicle: VehicleProfile, index, *,
               time_aware: bool = True, vehicle_aware: bool = True, probabilistic: bool = True,
               stale: bool = False, start_offset_s: float = 0.0, hard_only: bool = False) -> Assessment:
        s = self.s
        u, b = s.eta_rel_uncertainty, s.eta_abs_uncertainty_s
        alpha = s.alpha + (1 - s.alpha) * 0.5 if stale else s.alpha
        p_safe, margin, blocked, risks = 1.0, None, [], {}
        stats0 = dict(index.stats) if hasattr(index, "stats") else {}
        chunks = chunk_route(route, s.chunk_m)
        horizon_end = depart_t + route.duration * (1 + u) + 7200
        for ch in chunks:
            if time_aware:
                a_lo = depart_t + (ch.t0 - start_offset_s) * (1 - u)
                a_hi = depart_t + (ch.t1 - start_offset_s) * (1 + u) + b
            else:  # present-tense baseline: hazards as they are at departure
                a_lo = a_hi = depart_t
            hits = index.query(ch.geom, a_lo, a_hi)
            p_cls: dict[str, float] = {}
            for h in hits:
                if ch.elevated and h.hclass == "flood":
                    continue
                thr = vehicle.threshold(h.hclass) if vehicle_aware else 0.0
                if h.intensity <= thr and h.hclass in ("flood", "storm"):
                    continue
                p = h.probability if probabilistic else 1.0
                if p >= s.p_hard_block:
                    pt = ch.geom.interpolate(ch.geom.project(h.geom.representative_point())) if ch.geom.length > 0 else Point(ch.geom.coords[0])
                    blocked.append({"slice_id": h.id, "class": h.hclass, "intensity": h.intensity, "p": round(p, 3),
                                    "at": (pt.x, pt.y), "arrival": a_hi, "onset": h.t_start})
                else:
                    p_cls[h.hclass] = max(p_cls.get(h.hclass, 0.0), p)
                    if h.id not in risks or risks[h.id]["p"] < p:
                        risks[h.id] = {"slice_id": h.id, "class": h.hclass, "p": round(p, 3)}
            # time-to-impact: first blocking slice that starts after we pass
            if time_aware and not blocked:
                later = index.query(ch.geom, a_hi, horizon_end)
                for h in later:
                    if ch.elevated and h.hclass == "flood":
                        continue
                    thr = vehicle.threshold(h.hclass) if vehicle_aware else 0.0
                    if (h.intensity > thr or h.hclass in ("landslide", "closure")) and h.probability >= s.p_hard_block:
                        m = h.t_start - a_hi
                        margin = m if margin is None else min(margin, m)
        stats1 = dict(index.stats) if hasattr(index, "stats") else {}
        st = {k: stats1.get(k, 0) - stats0.get(k, 0) for k in stats1}
        st["chunks"] = len(chunks)
        # each hazard slice is one uncertain event: count it once even if several chunks cross it
        p_safe = math.prod(1 - r["p"] for r in risks.values()) if risks else 1.0
        feasible = not blocked and (hard_only or p_safe >= alpha)
        if blocked:
            p_safe = 0.0
        reason = "" if feasible else ("blocked by " + ", ".join(sorted({x["class"] for x in blocked})) if blocked
                                      else f"safety probability {p_safe:.2f} below {alpha:.2f}")
        return Assessment(route.id, feasible, p_safe, margin, blocked, list(risks.values()), st, stale, reason)
