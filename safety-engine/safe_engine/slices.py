"""Time-sliced hazard polygons, the versioned hazard store and the
spatio-temporal index used by the feasibility test (M3)."""
from __future__ import annotations

import itertools
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

import shapely
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

UNITS = {"flood": "m", "landslide": "blocked", "storm": "km/h", "closure": "blocked"}
_ids = itertools.count(1)


@dataclass
class HazardSlice:
    id: str
    hclass: str                 # flood | landslide | storm | closure
    geom: BaseGeometry          # local metric frame
    t_start: float
    t_end: float
    probability: float
    intensity: float            # flood depth (m) / wind (km/h) / 1.0 for blocked
    source: str = "model"       # model | crowd | cap | admin | llm
    status: str = "approved"    # approved | pending | rejected
    created_at: float = field(default_factory=time.time)
    approved_by: str | None = None
    version: int = 0
    note: str = ""
    band: str = ""

    @property
    def unit(self) -> str:
        return UNITS.get(self.hclass, "")

    def overlaps(self, t0: float, t1: float) -> bool:
        return self.t_start <= t1 and self.t_end >= t0

    def to_dict(self, frame) -> dict:
        return {"id": self.id, "class": self.hclass, "t_start": self.t_start, "t_end": self.t_end,
                "probability": round(self.probability, 3), "intensity": round(self.intensity, 3), "unit": self.unit,
                "source": self.source, "status": self.status, "version": self.version, "created_at": self.created_at,
                "approved_by": self.approved_by, "note": self.note, "geometry": frame.xy_to_geojson(self.geom)}


def new_id(prefix: str = "H") -> str:
    return f"{prefix}{next(_ids):06d}"


# ============================================================================ index

class SpatioTemporalIndex:
    """Slices bucketed by time; one STRtree per bucket.

    ``query(geom, t0, t1)`` touches only buckets overlapping [t0, t1] and, inside
    them, only slices whose bounding box intersects ``geom``.
    """

    def __init__(self, slices: Iterable[HazardSlice], bucket_s: int = 900):
        self.bucket_s = bucket_s
        self.buckets: dict[int, list[HazardSlice]] = {}
        for s in slices:
            if s.status != "approved":
                continue
            for b in range(int(s.t_start // bucket_s), int(math.ceil(s.t_end / bucket_s))):
                self.buckets.setdefault(b, []).append(s)
        for ss in self.buckets.values():
            for s in ss:
                shapely.prepare(s.geom)  # prepared geometries make repeated intersects() cheap
        self.trees = {b: STRtree([s.geom for s in ss]) for b, ss in self.buckets.items()}
        self.stats = {"bbox_candidates": 0, "exact_tests": 0}

    def query(self, geom: BaseGeometry, t0: float, t1: float, hclass: str | None = None) -> list[HazardSlice]:
        hits: dict[str, HazardSlice] = {}
        for b in range(int(t0 // self.bucket_s), int(t1 // self.bucket_s) + 1):
            tree = self.trees.get(b)
            if tree is None:
                continue
            idx = tree.query(geom)
            self.stats["bbox_candidates"] += len(idx)
            for i in idx:
                s = self.buckets[b][int(i)]
                if s.id in hits or not s.overlaps(t0, t1) or (hclass and s.hclass != hclass):
                    continue
                self.stats["exact_tests"] += 1
                if s.geom.intersects(geom):
                    hits[s.id] = s
        return list(hits.values())

    def all_slices(self) -> list[HazardSlice]:
        seen = {}
        for ss in self.buckets.values():
            for s in ss:
                seen[s.id] = s
        return list(seen.values())


def brute_force_query(slices: list[HazardSlice], geom, t0, t1, stats: dict) -> list[HazardSlice]:
    """Reference implementation without any index (used by the evaluation baseline)."""
    out = []
    for s in slices:
        stats["exact_tests"] = stats.get("exact_tests", 0) + 1
        if s.status == "approved" and s.overlaps(t0, t1) and s.geom.intersects(geom):
            out.append(s)
    return out


# ============================================================================ store

@dataclass
class VersionDiff:
    version: int
    parts: list            # [(geometry, t0, t1)] regions whose hazard state changed, per time bucket
    added: int
    removed: int

    @property
    def changed_region(self) -> BaseGeometry | None:
        if not self.parts:
            return None
        return shapely.union_all([g for g, _, _ in self.parts])


class HazardStore:
    """Versioned set of hazard slices.

    * model slices are replaced wholesale on every refresh (auto-approved by policy);
    * crowd / LLM / CAP-derived manual slices enter as *pending* and need admin approval;
    * admin-drawn slices are approved on creation.
    Every publish increments the version and reports the *changed region*: the
    symmetric difference of hazard footprints (per class, intensity band and
    time bucket) between the old and new version.
    """

    def __init__(self, bucket_s: int = 900):
        self.bucket_s = bucket_s
        self.version = 0
        self.slices: dict[str, HazardSlice] = {}
        self.updated_at = 0.0
        self.audit: list[dict] = []
        self.lock = threading.RLock()
        self.index = SpatioTemporalIndex([], bucket_s)

    # ------------------------------------------------------------------ change detection
    def _raster_parts(self, old_r, new_r, cell: float) -> list:
        """Changed cells per absolute time bucket between two model rasters."""
        from .geometry import _polys_for_mask  # local import avoids a cycle

        def at(raster, t):
            for t0, t1, codes in raster or []:
                if t0 <= t < t1:
                    return codes
            return None

        buckets = set()
        for r in (old_r or []), (new_r or []):
            for t0, t1, _ in r:
                buckets.update(range(int(t0 // self.bucket_s), int(math.ceil(t1 / self.bucket_s))))
        parts = []
        for bkt in sorted(buckets):
            tb = bkt * self.bucket_s + 1
            a, b = at(old_r, tb), at(new_r, tb)
            if a is None and b is None:
                continue
            if a is None:
                mask = (b != 0).any(axis=0)
            elif b is None:
                mask = (a != 0).any(axis=0)
            else:
                mask = (a != b).any(axis=0)
            if mask.any():
                g = shapely.union_all(_polys_for_mask(mask, cell, 0.0))
                parts.append((g, bkt * self.bucket_s, (bkt + 1) * self.bucket_s))
        return parts

    # ------------------------------------------------------------------ mutations
    def publish_model(self, model_slices: list[HazardSlice], now: float, raster=None, cell: float = 100.0) -> VersionDiff:
        with self.lock:
            old_ids = set(self.slices)
            keep = {k: s for k, s in self.slices.items() if s.source != "model" and s.t_end > now}
            self.version += 1
            for s in model_slices:
                s.version = self.version
                s.approved_by = s.approved_by or "auto-policy"
                keep[s.id] = s
            removed = len(old_ids - set(keep))
            self.slices = keep
            parts = self._raster_parts(getattr(self, "_raster", None), raster, cell) if raster is not None else \
                [(s.geom, s.t_start, s.t_end) for s in model_slices]
            self._raster = raster
            self._reindex(now)
            return VersionDiff(self.version, parts, len(model_slices), removed)

    def add_manual(self, s: HazardSlice, now: float, actor: str) -> VersionDiff | None:
        with self.lock:
            s.version = self.version + 1
            self.slices[s.id] = s
            self.audit.append({"t": now, "actor": actor, "action": "add", "slice": s.id, "status": s.status, "source": s.source})
            if s.status == "approved":
                self.version += 1
                self._reindex(now)
                return VersionDiff(self.version, [(s.geom, s.t_start, s.t_end)], 1, 0)
            return None

    def set_status(self, sid: str, status: str, now: float, actor: str) -> VersionDiff | None:
        with self.lock:
            s = self.slices.get(sid)
            if s is None:
                raise KeyError(sid)
            was = s.status
            s.status = status
            s.approved_by = actor if status == "approved" else s.approved_by
            self.audit.append({"t": now, "actor": actor, "action": status, "slice": sid})
            self.version += 1
            self._reindex(now)
            if (was == "approved") != (status == "approved"):
                return VersionDiff(self.version, [(s.geom, s.t_start, s.t_end)], 0, 0)
            return VersionDiff(self.version, [], 0, 0)

    def _reindex(self, now: float):
        self.index = SpatioTemporalIndex(list(self.slices.values()), self.bucket_s)
        self.updated_at = now

    # ------------------------------------------------------------------ queries
    def get(self, sid: str) -> HazardSlice | None:
        return self.slices.get(sid)

    def active(self, t0: float, t1: float, statuses=("approved",)) -> list[HazardSlice]:
        return [s for s in self.slices.values() if s.status in statuses and s.overlaps(t0, t1)]

    def pending(self) -> list[HazardSlice]:
        return [s for s in self.slices.values() if s.status == "pending"]


def cell_box(x0: float, y0: float, size: float) -> Polygon:
    return Polygon([(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size)])
