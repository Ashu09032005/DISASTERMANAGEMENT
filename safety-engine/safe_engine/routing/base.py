"""Routing-engine abstraction.

The safety engine treats the router as a black box with a *fixed, preprocessed*
road graph (as with OSRM's contraction hierarchies): it can ask for k
alternative routes, routes through via-waypoints, and the nearest road point,
but it never modifies the graph. This is what makes detour synthesis (M4)
engine-agnostic.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Protocol

_rid = itertools.count(1)


@dataclass
class Route:
    coords: list                 # [(x, y)] local metres
    seg_dur: list                # seconds per coordinate segment (len = len(coords) - 1)
    via: list = field(default_factory=list)   # [(x, y)] via-waypoints used
    id: str = ""
    elevated: list | None = None              # per segment: True on bridges / flyovers (exempt from ponding)
    engine: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = f"R{next(_rid):06d}"

    @property
    def duration(self) -> float:
        return float(sum(self.seg_dur))

    @property
    def distance(self) -> float:
        return float(sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(self.coords, self.coords[1:])))


class RoutingEngine(Protocol):
    name: str

    def routes(self, o, d, k: int = 3) -> list[Route]: ...
    def route_via(self, o, vias: list, d) -> Route | None: ...
    def nearest(self, p) -> tuple[float, float]: ...


def overlap_fraction(a: Route, b: Route, tol: float = 30.0) -> float:
    """Fraction of a's length lying within tol metres of b (approximate, via shapely)."""
    from shapely.geometry import LineString
    la, lb = LineString(a.coords), LineString(b.coords)
    if la.length == 0:
        return 1.0
    return la.intersection(lb.buffer(tol)).length / la.length
