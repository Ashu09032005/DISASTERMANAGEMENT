"""M4 — engine-agnostic detour synthesis.

When every candidate route is infeasible, via-waypoints are computed outside the
buffered blocking geometry and the *unchanged* routing engine is re-queried.

1. Blocking geometry B = union of the blocking slices near the *first* blocked
   point of the best candidate (detours are built one obstruction at a time).
2. Buffer B by beta, take the convex hull, and find where the route enters and
   leaves it.
3. Candidate via-points = hull vertices on the left and on the right of the
   entry->exit line with the largest perpendicular offset, pushed a little
   outwards and snapped to the road network.
4. Re-query O -> w -> D (and O -> w1 -> w2 -> D), evaluate with M3, and repeat
   with a larger beta up to N_max iterations.
"""
from __future__ import annotations

import math
import time

import shapely
from shapely.geometry import LineString, Point

from .feasibility import Assessment, FeasibilityEvaluator
from .routing.base import Route


def _side(a, b, p) -> float:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


class DetourSynthesizer:
    def __init__(self, engine, evaluator: FeasibilityEvaluator, settings):
        self.engine, self.ev, self.s = engine, evaluator, settings

    def synthesize(self, o, d, best: Route, best_assessment: Assessment, depart_t, vehicle, store) -> dict:
        t0 = time.perf_counter()
        index = store.index
        ordered = sorted(best_assessment.blocked, key=lambda x: x["arrival"])
        blockers = [store.get(b["slice_id"]) for b in ordered]
        blockers = [b for b in blockers if b is not None]
        focus = Point(ordered[0]["at"]) if ordered else None
        tried, queries = [], 0
        if not blockers:
            return {"route": None, "assessment": None, "tried": tried, "queries": 0, "ms": 0.0}
        beta = self.s.detour_initial_buffer_m
        line = LineString(best.coords)
        for it in range(self.s.detour_max_iter):
            near = [b.geom for b in blockers if b.geom.distance(focus) <= 800 + beta] or [blockers[0].geom]
            region = shapely.union_all(near).buffer(beta)
            hull = region.convex_hull
            inter = line.intersection(hull)
            if inter.is_empty:
                break
            entry = line.interpolate(line.project(Point(inter.coords[0] if inter.geom_type == "LineString" else list(inter.geoms)[0].coords[0])))
            last_geom = inter if inter.geom_type == "LineString" else list(inter.geoms)[-1]
            exitp = line.interpolate(line.project(Point(last_geom.coords[-1])))
            a, b = (entry.x, entry.y), (exitp.x, exitp.y)
            verts = list(hull.exterior.coords) if hull.geom_type == "Polygon" else [hull.coords[0]]
            left = [v for v in verts if _side(a, b, v) > 0]
            right = [v for v in verts if _side(a, b, v) < 0]
            L = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
            cands = []
            for group in (left, right):
                if not group:
                    continue
                v = max(group, key=lambda p: abs(_side(a, b, p)) / L)
                c = hull.centroid
                dx, dy = v[0] - c.x, v[1] - c.y
                n = math.hypot(dx, dy) or 1.0
                w = (v[0] + dx / n * 0.25 * beta, v[1] + dy / n * 0.25 * beta)
                cands.append([w])
                # two-point variant hugging the entry and exit sides
                ea = min(group, key=lambda p: math.dist(p, a))
                eb = min(group, key=lambda p: math.dist(p, b))
                if math.dist(ea, eb) > 1:
                    cands.append([ea, eb])
            best_found = None
            for vias in cands:
                if time.perf_counter() - t0 > self.s.detour_time_budget_s:
                    break
                snapped = [self.engine.nearest(v) for v in vias]
                queries += 1
                r = self.engine.route_via(o, snapped, d)
                if r is None:
                    continue
                asm = self.ev.assess(r, depart_t, vehicle, index)
                tried.append({"iteration": it, "beta_m": beta, "via": snapped, "route_id": r.id, "feasible": asm.feasible,
                              "duration_s": round(r.duration)})
                if asm.feasible and (best_found is None or r.duration < best_found[0].duration):
                    best_found = (r, asm)
                elif not asm.feasible:
                    for x in asm.blocked:
                        s = store.get(x["slice_id"])
                        if s is not None and all(s.id != bb.id for bb in blockers):
                            blockers.append(s)
            if best_found:
                return {"route": best_found[0], "assessment": best_found[1], "tried": tried, "queries": queries,
                        "ms": round((time.perf_counter() - t0) * 1000, 1)}
            beta *= 2
        return {"route": None, "assessment": None, "tried": tried, "queries": queries,
                "ms": round((time.perf_counter() - t0) * 1000, 1)}
