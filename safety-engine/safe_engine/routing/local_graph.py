"""Built-in road graph + router used when no OSRM server is available (offline
demo, tests and the evaluation harness).

The graph is generated once and then treated as immutable ("preprocessed"),
mirroring how a contraction-hierarchy engine behaves.
"""
from __future__ import annotations

import heapq
import math

import numpy as np

from .base import Route, overlap_fraction


class LocalGraphEngine:
    name = "local-graph"

    def __init__(self, width_m: float, height_m: float, seed: int = 3, spacing: float = 600.0, step: float = 150.0,
                 is_channel=None):
        """is_channel(x, y) -> bool marks major river cells; edges crossing them are built as bridges."""
        rng = np.random.default_rng(seed)
        self.width, self.height = width_m, height_m
        nx, ny = int(width_m // spacing) + 1, int(height_m // spacing) + 1
        jitter = spacing * 0.18
        pts = {}
        for j in range(ny):
            for i in range(nx):
                x = min(max(i * spacing + rng.uniform(-jitter, jitter), 1), width_m - 1)
                y = min(max(j * spacing + rng.uniform(-jitter, jitter), 1), height_m - 1)
                pts[(i, j)] = (x, y)
        self.nodes: list[tuple[float, float]] = []
        nid = {}
        for k, p in pts.items():
            nid[k] = len(self.nodes)
            self.nodes.append(p)
        self.adj: dict[int, list] = {i: [] for i in range(len(self.nodes))}
        self.edges = []
        self.bridge: list[bool] = []
        arterial_every = 4

        def add(a, b, speed_kmh):
            pa, pb = self.nodes[a], self.nodes[b]
            n = max(1, int(math.hypot(pb[0] - pa[0], pb[1] - pa[1]) // step))
            geom = [(pa[0] + (pb[0] - pa[0]) * t / n, pa[1] + (pb[1] - pa[1]) * t / n) for t in range(n + 1)]
            length = sum(math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in zip(geom, geom[1:]))
            dur = length / (speed_kmh / 3.6)
            e = len(self.edges)
            bridge = bool(is_channel) and any(is_channel(q[0], q[1]) for q in geom)
            self.bridge.append(bridge)
            self.edges.append((a, b, geom, dur))
            self.adj[a].append((b, e, dur))
            self.adj[b].append((a, e, dur))

        for (i, j), a in nid.items():
            for di, dj in ((1, 0), (0, 1)):
                k2 = (i + di, j + dj)
                if k2 not in nid:
                    continue
                art = (j % arterial_every == 0) if di else (i % arterial_every == 0)
                if not art and rng.random() < 0.12:
                    continue  # missing street link
                add(a, nid[k2], 45 if art else 25)
            if (i + j) % 7 == 0 and (i + 1, j + 1) in nid and rng.random() < 0.5:
                add(a, nid[(i + 1, j + 1)], 30)  # occasional diagonal link road
        self._xy = np.array(self.nodes)

    # ------------------------------------------------------------------ search
    def _dijkstra(self, s: int, t: int, weight_mult=None):
        dist = {s: 0.0}
        prev = {}
        pq = [(0.0, s)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == t:
                break
            if d > dist.get(u, math.inf):
                continue
            for v, e, w in self.adj[u]:
                w2 = w * (weight_mult.get(e, 1.0) if weight_mult else 1.0)
                nd = d + w2
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = (u, e)
                    heapq.heappush(pq, (nd, v))
        if t not in dist:
            return None
        path = []
        u = t
        while u != s:
            pu, e = prev[u]
            path.append((pu, u, e))
            u = pu
        return path[::-1]

    def _to_route(self, path, o_xy, d_xy) -> Route:
        coords, durs, elev = [o_xy], [], [False]
        first = self.nodes[path[0][0]] if path else d_xy
        durs.append(math.hypot(first[0] - o_xy[0], first[1] - o_xy[1]) / (20 / 3.6))
        coords.append(first)
        for a, b, e in path:
            ea, eb, geom, dur = self.edges[e]
            g = geom if ea == a else geom[::-1]
            seg = dur / max(1, len(g) - 1)
            for q in g[1:]:
                coords.append(q)
                durs.append(seg)
                elev.append(self.bridge[e])
        coords.append(d_xy)
        last = coords[-2]
        durs.append(math.hypot(d_xy[0] - last[0], d_xy[1] - last[1]) / (20 / 3.6))
        elev.append(False)
        return Route(coords=coords, seg_dur=durs, engine=self.name, elevated=elev)

    def _node(self, p) -> int:
        return int(np.argmin(((self._xy - np.asarray(p)) ** 2).sum(axis=1)))

    # ------------------------------------------------------------------ API
    def nearest(self, p):
        return tuple(self.nodes[self._node(p)])

    def routes(self, o, d, k: int = 3) -> list[Route]:
        s, t = self._node(o), self._node(d)
        if s == t:
            return [Route(coords=[tuple(o), tuple(d)], seg_dur=[math.dist(o, d) / (20 / 3.6)], engine=self.name)]
        mult: dict[int, float] = {}
        out: list[Route] = []
        best = None
        for _ in range(k * 3):
            p = self._dijkstra(s, t, mult)
            if p is None:
                break
            r = self._to_route(p, tuple(o), tuple(d))
            best = best or r.duration
            if r.duration > 1.6 * best:
                break
            if all(overlap_fraction(r, q) < 0.8 for q in out):
                out.append(r)
            for _, _, e in p:
                mult[e] = mult.get(e, 1.0) * 1.6
            if len(out) >= k:
                break
        return out

    def route_via(self, o, vias, d) -> Route | None:
        pts = [tuple(o)] + [tuple(v) for v in vias] + [tuple(d)]
        coords, durs, elev = [], [], []
        for a, b in zip(pts, pts[1:]):
            p = self._dijkstra(self._node(a), self._node(b))
            if p is None:
                return None
            r = self._to_route(p, a, b) if p else Route(coords=[a, b], seg_dur=[math.dist(a, b) / (20 / 3.6)])
            if coords:
                coords.extend(r.coords[1:])
            else:
                coords.extend(r.coords)
            durs.extend(r.seg_dur)
            elev.extend(r.elevated or [False] * len(r.seg_dur))
        return Route(coords=coords, seg_dur=durs, via=[tuple(v) for v in vias], engine=self.name, elevated=elev)

    # ------------------------------------------------------------------ export (offline packs)
    def export(self, bbox=None) -> dict:
        nodes = self.nodes
        es = []
        for a, b, geom, dur in self.edges:
            if bbox and not any(bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3] for x, y in (nodes[a], nodes[b])):
                continue
            es.append([a, b, round(dur, 1)])
        used = {i for e in es for i in e[:2]}
        return {"nodes": {i: [round(nodes[i][0], 1), round(nodes[i][1], 1)] for i in used}, "edges": es}
