"""Hazard-evidence ingestion and fusion (official CAP alerts, crowd reports, sensors).

* Each item has a source-specific base confidence and decays with age:
  w = c * exp(-age / tau_source)   (CAP alerts: constant inside onset..expires).
* Crowd reports are de-duplicated per reporter and 250 m cell within 30 min, so
  one person cannot inflate the evidence; reporter reputation is a Beta(a, b)
  posterior updated when admins approve / reject the report.
* Evidence fields e(x) in [0, 1] are combined across independent items by
  noisy-OR and applied to the model probabilities as a bounded log-odds update:
  logit(p') = logit(p) + lambda * log(1 + 9 e) * decay(horizon).
* Every crowd report also creates a *pending* hazard slice for admin review;
  corroboration by >= 3 independent reporters auto-approves it (policy flag).
"""
from __future__ import annotations

import itertools
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import Point, Polygon

from .slices import HazardSlice, new_id

_eid = itertools.count(1)
CAP_NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}
DEPTH_LEVELS = {"ankle": 0.10, "knee": 0.45, "waist": 0.90, "unknown": 0.25}
SOURCE = {"cap": {"tau_s": None, "lambda": 0.6}, "crowd": {"tau_s": 2700.0, "lambda": 1.0},
          "sensor": {"tau_s": 900.0, "lambda": 1.5}}


@dataclass
class Evidence:
    kind: str
    hclass: str
    geom: object
    t: float
    confidence: float
    expires: float | None = None
    onset: float | None = None
    reporter: str | None = None
    depth_m: float | None = None
    text: str = ""
    id: str = ""
    slice_id: str | None = None
    status: str = "active"

    def __post_init__(self):
        if not self.id:
            self.id = f"E{next(_eid):06d}"

    def weight(self, now: float) -> float:
        if self.status == "rejected":
            return 0.0
        if self.kind == "cap":
            if (self.onset and now < self.onset - 3600) or (self.expires and now > self.expires):
                return 0.0
            return self.confidence
        tau = SOURCE[self.kind]["tau_s"]
        age = max(0.0, now - self.t)
        return self.confidence * math.exp(-age / tau)


class EvidenceFusion:
    def __init__(self, frame, auto_approve_corroborated: bool = True):
        self.frame = frame
        self.items: list[Evidence] = []
        self.reputation: dict[str, list[float]] = {}
        self.auto = auto_approve_corroborated
        self.last_update = 0.0

    # ------------------------------------------------------------------ reputation
    def rep(self, reporter: str | None) -> float:
        a, b = self.reputation.get(reporter or "anon", [2.0, 2.0])
        return a / (a + b)

    def feedback(self, reporter: str | None, correct: bool):
        ab = self.reputation.setdefault(reporter or "anon", [2.0, 2.0])
        ab[0 if correct else 1] += 1

    # ------------------------------------------------------------------ crowd reports
    def add_crowd(self, x: float, y: float, hclass: str, now: float, reporter: str | None,
                  depth_level: str = "unknown", text: str = "") -> tuple[Evidence, HazardSlice | None, bool]:
        """Returns (evidence, pending_slice_or_None, duplicate_flag)."""
        key = (int(x // 250), int(y // 250))
        for e in self.items:
            if e.kind == "crowd" and e.reporter == reporter and e.hclass == hclass and now - e.t < 1800 \
                    and (int(e.geom.x // 250), int(e.geom.y // 250)) == key:
                e.t = now  # refresh, do not double count
                return e, None, True
        depth = DEPTH_LEVELS.get(depth_level, 0.25) if hclass == "flood" else None
        conf = 0.35 + 0.5 * self.rep(reporter)
        e = Evidence("crowd", hclass, Point(x, y), now, conf, reporter=reporter, depth_m=depth, text=text[:280])
        self.items.append(e)
        self.last_update = now
        intensity = depth if depth is not None else 1.0
        s = HazardSlice(new_id("C"), hclass if hclass in ("flood", "landslide", "storm") else "closure",
                        Point(x, y).buffer(80), now, now + 7200, conf, intensity, source="crowd", status="pending",
                        note=f"crowd report {e.id} by {reporter or 'anon'}: {text[:120]}")
        e.slice_id = s.id
        return e, s, False

    def corroboration(self, e: Evidence, now: float) -> int:
        """Number of distinct reporters with an active report of the same class within 250 m."""
        who = {x.reporter for x in self.items if x.kind == "crowd" and x.hclass == e.hclass and x.status != "rejected"
               and now - x.t < 1800 and x.geom.distance(e.geom) <= 250}
        return len(who)

    # ------------------------------------------------------------------ sensors
    def add_sensor(self, x, y, depth_m: float, now: float, sensor_id: str):
        e = Evidence("sensor", "flood", Point(x, y), now, 0.95 if depth_m >= 0.15 else 0.0, reporter=sensor_id, depth_m=depth_m)
        self.items.append(e)
        self.last_update = now
        return e

    # ------------------------------------------------------------------ CAP
    def add_cap_xml(self, xml_text: str, now: float) -> list[Evidence]:
        root = ET.fromstring(xml_text)
        for el in root.iter():               # strip XML namespaces (CAP 1.1 / 1.2 / none)
            if isinstance(el.tag, str) and "}" in el.tag:
                el.tag = el.tag.split("}", 1)[1]
        ns = None
        q = lambda p: p  # noqa: E731
        out = []
        for info in root.findall(q("info"), ns):
            event = (info.findtext(q("event"), "", ns) or "").lower()
            hclass = "flood" if any(w in event for w in ("flood", "inundat", "rain")) else \
                "landslide" if "landslide" in event else \
                "storm" if any(w in event for w in ("cyclone", "storm", "wind", "thunder")) else None
            if hclass is None:
                continue
            sev = {"extreme": 0.95, "severe": 0.85, "moderate": 0.6, "minor": 0.4}.get((info.findtext(q("severity"), "", ns) or "").lower(), 0.5)
            cer = {"observed": 1.0, "likely": 0.8, "possible": 0.5}.get((info.findtext(q("certainty"), "", ns) or "").lower(), 0.6)
            onset, expires = _iso(info.findtext(q("onset"), None, ns)), _iso(info.findtext(q("expires"), None, ns))
            for area in info.findall(q("area"), ns):
                for poly in area.findall(q("polygon"), ns):
                    pts = [tuple(map(float, p.split(","))) for p in poly.text.split()]
                    g = Polygon([self.frame.to_xy(lon, lat) for lat, lon in pts])
                    out.append(Evidence("cap", hclass, g, now, sev * cer, expires=expires, onset=onset, text=event))
                for circ in area.findall(q("circle"), ns):
                    c, r = circ.text.split()
                    lat, lon = map(float, c.split(","))
                    x, y = self.frame.to_xy(lon, lat)
                    out.append(Evidence("cap", hclass, Point(x, y).buffer(float(r) * 1000), now, sev * cer,
                                        expires=expires, onset=onset, text=event))
        self.items.extend(out)
        if out:
            self.last_update = now
        return out

    # ------------------------------------------------------------------ fields
    def field(self, terrain, hclass: str, now: float, kinds=("crowd", "sensor", "cap")) -> tuple[np.ndarray, np.ndarray]:
        """Returns (evidence e in [0,1], depth floor in m) on the terrain grid."""
        X, Y = terrain.centers()
        e = np.zeros_like(X)
        depth = np.zeros_like(X)
        per_reporter: dict[str, np.ndarray] = {}
        for it in self.items:
            if it.hclass != hclass or it.kind not in kinds:
                continue
            w = it.weight(now)
            if w <= 0.01:
                continue
            if it.geom.geom_type == "Point":
                r = 150.0
                m = (X - it.geom.x) ** 2 + (Y - it.geom.y) ** 2 <= r * r
            else:
                minx, miny, maxx, maxy = it.geom.bounds
                m = (X >= minx) & (X <= maxx) & (Y >= miny) & (Y <= maxy)
                if m.any():
                    import shapely
                    pts = shapely.points(X[m], Y[m])
                    inside = shapely.contains(it.geom, pts)
                    m2 = np.zeros_like(m)
                    m2[m] = inside
                    m = m2
            lam = SOURCE[it.kind]["lambda"]
            key = f"{it.kind}:{it.reporter or it.id}"
            cur = per_reporter.setdefault(key, np.zeros_like(X))
            np.maximum(cur, np.where(m, min(1.0, w * lam), 0.0), out=cur)   # one reporter counts once per cell
            if it.depth_m:
                np.maximum(depth, np.where(m, it.depth_m * min(1.0, w / 0.5), 0.0), out=depth)
        for v in per_reporter.values():
            e = 1 - (1 - e) * (1 - v)                                        # noisy-OR across independent sources
        return e, depth

    def to_list(self, now: float) -> list[dict]:
        return [{"id": e.id, "kind": e.kind, "class": e.hclass, "t": e.t, "weight": round(e.weight(now), 3),
                 "reporter": e.reporter, "depth_m": e.depth_m, "text": e.text, "slice_id": e.slice_id,
                 "status": e.status, "geometry": self.frame.xy_to_geojson(e.geom)} for e in self.items[-500:]]


def apply_evidence(p: np.ndarray, e: np.ndarray, horizon_s: float, tau_h_s: float = 3600.0) -> np.ndarray:
    """Bounded Bayesian-style log-odds update of model probabilities by evidence."""
    if not np.any(e > 0):
        return p
    pc = np.clip(p, 1e-4, 1 - 1e-4)
    lo = np.log(pc / (1 - pc)) + np.log1p(9.0 * e) * math.exp(-horizon_s / tau_h_s)
    return 1 / (1 + np.exp(-lo))


def _iso(s: str | None) -> float | None:
    if not s:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
