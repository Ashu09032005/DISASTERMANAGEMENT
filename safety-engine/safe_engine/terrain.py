"""Terrain grid and hydrological derivatives used by the hazard model (M1) and
hazard-geometry generator (M2).

Derived layers
--------------
fill_depth  : depth of each closed depression (priority-flood, Barnes et al. 2014)
acc         : D8 flow accumulation (number of upstream cells)
hand        : Height Above Nearest Drainage (m)
slope_deg   : terrain slope
landslide_s : static landslide susceptibility in [0, 1]

A real DEM can be loaded from an ESRI ASCII grid (``.asc``), e.g. exported from
SRTM / CartoDEM with ``gdal_translate -of AAIGrid``. For offline demonstration
and evaluation :func:`synthetic_terrain` builds a reproducible coastal-city
terrain (clearly labelled as synthetic in the API).
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

import numpy as np

from .geo import LocalFrame

_NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


@dataclass
class TerrainGrid:
    frame: LocalFrame
    cell: float                  # metres
    elev: np.ndarray             # (ny, nx); row 0 = south edge
    synthetic: bool = False
    # hidden "truth" layers only present for synthetic scenarios (used by the simulator, never by M1)
    truth: dict = field(default_factory=dict)

    # derived
    fill_depth: np.ndarray | None = None
    acc: np.ndarray | None = None
    hand: np.ndarray | None = None
    slope_deg: np.ndarray | None = None
    landslide_s: np.ndarray | None = None
    river_mask: np.ndarray | None = None
    near_river: np.ndarray | None = None

    @property
    def ny(self) -> int:
        return self.elev.shape[0]

    @property
    def nx(self) -> int:
        return self.elev.shape[1]

    # ---------------------------------------------------------------- indexing
    def cell_center(self, r: int, c: int) -> tuple[float, float]:
        return (c + 0.5) * self.cell, (r + 0.5) * self.cell

    def rc(self, x: float, y: float) -> tuple[int, int] | None:
        c, r = int(x // self.cell), int(y // self.cell)
        if 0 <= r < self.ny and 0 <= c < self.nx:
            return r, c
        return None

    def centers(self) -> tuple[np.ndarray, np.ndarray]:
        xs = (np.arange(self.nx) + 0.5) * self.cell
        ys = (np.arange(self.ny) + 0.5) * self.cell
        return np.meshgrid(xs, ys)

    # ---------------------------------------------------------------- derive
    def derive(self, river_acc_cells: int = 2500) -> "TerrainGrid":
        self.slope_deg = _slope(self.elev, self.cell)
        filled, filled_eps, order = _priority_flood(self.elev)
        self.fill_depth = np.maximum(filled - self.elev, 0.0)
        down = _d8_down(filled_eps)
        self.acc = _accumulate(down, order).reshape(self.elev.shape)
        self.river_mask = self.acc >= river_acc_cells
        self.hand = _hand(self.elev, down, order, self.river_mask)
        # cells within ~0.8 km of a major channel (river flooding is limited to these)
        near = self.river_mask.copy()
        for _ in range(int(800 // self.cell)):
            p = np.pad(near, 1)
            near = near | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
        self.near_river = near
        # 100 m DEMs under-estimate slope, so the susceptibility midpoint is set lower than field values
        s = 1 / (1 + np.exp(-(self.slope_deg - 6.5) / 1.5))
        self.landslide_s = np.clip(s, 0, 1)
        return self


# --------------------------------------------------------------------------- algorithms

def _slope(z: np.ndarray, cell: float) -> np.ndarray:
    gy, gx = np.gradient(z, cell)
    return np.degrees(np.arctan(np.hypot(gx, gy)))


def _priority_flood(z: np.ndarray):
    """Priority-flood depression filling. Returns (filled, filled_with_epsilon, pop_order)."""
    ny, nx = z.shape
    filled = z.copy()
    feps = z.copy()
    closed = np.zeros_like(z, dtype=bool)
    heap: list = []
    for r in range(ny):
        for c in (0, nx - 1):
            if not closed[r, c]:
                closed[r, c] = True
                heapq.heappush(heap, (z[r, c], r, c))
    for c in range(nx):
        for r in (0, ny - 1):
            if not closed[r, c]:
                closed[r, c] = True
                heapq.heappush(heap, (z[r, c], r, c))
    order = []
    eps = 1e-4
    while heap:
        h, r, c = heapq.heappop(heap)
        order.append(r * nx + c)
        for dr, dc in _NB:
            rr, cc = r + dr, c + dc
            if 0 <= rr < ny and 0 <= cc < nx and not closed[rr, cc]:
                closed[rr, cc] = True
                filled[rr, cc] = max(z[rr, cc], filled[r, c])
                feps[rr, cc] = max(z[rr, cc], feps[r, c] + eps)
                heapq.heappush(heap, (feps[rr, cc], rr, cc))
    return filled, feps, np.array(order)


def _d8_down(f: np.ndarray) -> np.ndarray:
    ny, nx = f.shape
    down = -np.ones(ny * nx, dtype=np.int64)
    for r in range(ny):
        for c in range(nx):
            best, bi = 0.0, -1
            for dr, dc in _NB:
                rr, cc = r + dr, c + dc
                if 0 <= rr < ny and 0 <= cc < nx:
                    d = (f[r, c] - f[rr, cc]) / (math.sqrt(2) if dr and dc else 1.0)
                    if d > best:
                        best, bi = d, rr * nx + cc
            down[r * nx + c] = bi
    return down


def _accumulate(down: np.ndarray, order: np.ndarray) -> np.ndarray:
    n = down.size
    acc = np.ones(n)
    for i in order[::-1]:          # highest first
        d = down[i]
        if d >= 0:
            acc[d] += acc[i]
    return acc


def _hand(z, down, order, river_mask):
    ny, nx = z.shape
    zf = z.ravel()
    rv = river_mask.ravel() if river_mask.ndim == 1 else river_mask.ravel()
    ref = np.arange(zf.size)
    for i in order:                # lowest first: downstream resolved before upstream
        if rv[i]:
            ref[i] = i
        elif down[i] >= 0:
            ref[i] = ref[down[i]]
        else:
            ref[i] = i
    return np.maximum(zf - zf[ref], 0.0).reshape(ny, nx)


# --------------------------------------------------------------------------- IO

def load_esri_ascii(path: str, frame: LocalFrame | None = None) -> TerrainGrid:
    """Load an ESRI ASCII grid in geographic (lon/lat) coordinates."""
    header = {}
    with open(path) as fh:
        for _ in range(6):
            k, v = fh.readline().split()
            header[k.lower()] = float(v)
        data = np.loadtxt(fh)
    nodata = header.get("nodata_value", -9999)
    data = np.where(data == nodata, np.nan, data)
    data = np.flipud(data)  # file rows are north->south
    xll, yll, cs = header["xllcorner"], header["yllcorner"], header["cellsize"]
    fr = frame or LocalFrame(xll, yll)
    # resample to a metric grid (nearest neighbour) so cells are square in metres
    cell_m = cs * 110_540.0
    nyy, nxx = data.shape
    width_m = nxx * cs * fr.kx
    height_m = nyy * cs * 110_540.0
    nx, ny = int(width_m // cell_m), int(height_m // cell_m)
    out = np.empty((ny, nx))
    for r in range(ny):
        for c in range(nx):
            lon, lat = fr.to_lonlat((c + 0.5) * cell_m, (r + 0.5) * cell_m)
            cc, rr = int((lon - xll) / cs), int((lat - yll) / cs)
            out[r, c] = data[min(max(rr, 0), nyy - 1), min(max(cc, 0), nxx - 1)]
    out = np.where(np.isnan(out), np.nanmin(out), out)
    return TerrainGrid(frame=fr, cell=cell_m, elev=out).derive()


# --------------------------------------------------------------------------- synthetic

def _smooth_noise(rng, ny, nx, scale_cells, amp):
    small = rng.normal(size=(max(2, ny // scale_cells + 2), max(2, nx // scale_cells + 2)))
    yi = np.linspace(0, small.shape[0] - 1.001, ny)
    xi = np.linspace(0, small.shape[1] - 1.001, nx)
    y0, x0 = np.floor(yi).astype(int), np.floor(xi).astype(int)
    fy, fx = (yi - y0)[:, None], (xi - x0)[None, :]
    a = small[y0][:, x0]
    b = small[y0][:, x0 + 1]
    c = small[y0 + 1][:, x0]
    d = small[y0 + 1][:, x0 + 1]
    return amp * ((a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy)


def synthetic_terrain(frame: LocalFrame, width_m: float, height_m: float, cell: float, seed: int = 7) -> TerrainGrid:
    """Coastal-city terrain: inland rise to the west, hills in the south-west,
    two meandering rivers draining east, and scattered urban depressions.
    Also builds hidden truth layers (drainage capacity, tree cover, landslide thresholds)."""
    rng = np.random.default_rng(seed)
    nx, ny = int(width_m // cell), int(height_m // cell)
    xs = (np.arange(nx) + 0.5) * cell
    ys = (np.arange(ny) + 0.5) * cell
    X, Y = np.meshgrid(xs, ys)
    u = X / width_m

    z = 2.0 + 22.0 * (1 - u) ** 1.4
    # south-west hills (steep enough for landslide susceptibility)
    for (hx, hy, h, r) in [(0.12, 0.18, 200, 650), (0.25, 0.10, 150, 520), (0.08, 0.40, 120, 450), (0.20, 0.30, 90, 380)]:
        z += h * np.exp(-(((X - hx * width_m) ** 2 + (Y - hy * height_m) ** 2) / (2 * r ** 2)))
    z += _smooth_noise(rng, ny, nx, 12, 1.2) + _smooth_noise(rng, ny, nx, 4, 0.35)

    rivers = []
    river_dist = np.full_like(z, np.inf)
    for yc, amp, ph in [(0.36, 0.05, 0.3), (0.70, 0.04, 1.7)]:
        line_y = (yc + amp * np.sin(2 * np.pi * (2.2 * u + ph))) * height_m
        rivers.append(line_y[0])
        d = np.abs(Y - line_y)
        river_dist = np.minimum(river_dist, d)
        z -= 4.5 * np.exp(-(d / 140.0) ** 2) + 1.6 * np.exp(-(d / 650.0) ** 2)

    # urban depressions (underpasses, low streets) – the source of most road flooding
    for _ in range(55):
        cx, cy = rng.uniform(0.15, 0.97) * width_m, rng.uniform(0.03, 0.97) * height_m
        depth, rad = rng.uniform(0.25, 1.1), rng.uniform(90, 260)
        z -= depth * np.exp(-(((X - cx) ** 2 + (Y - cy) ** 2) / (2 * rad ** 2)))

    true_grid = TerrainGrid(frame=frame, cell=cell, elev=z, synthetic=True).derive()
    # The model never sees the true surface: it sees a DEM with realistic vertical error
    # (smooth bias + pixel noise, comparable to 30 m global DEMs after resampling).
    dem = z + _smooth_noise(rng, ny, nx, 8, 0.7) + rng.normal(0, 0.35, z.shape)
    terrain = TerrainGrid(frame=frame, cell=cell, elev=dem, synthetic=True)
    terrain.derive()

    # hidden truth layers (unknown to the model): spatially variable drainage, trees, slope failure thresholds
    drainage = np.clip(18 + _smooth_noise(rng, ny, nx, 10, 10) + rng.normal(0, 4, z.shape), 3, 45)  # mm/h
    clogged = rng.random(z.shape) < 0.12
    drainage[clogged] *= 0.35
    trees = np.clip(0.5 + _smooth_noise(rng, ny, nx, 6, 0.35), 0, 1)
    ls_thr = 70 + 220 * (1 - true_grid.landslide_s) + rng.normal(0, 35, z.shape)  # mm antecedent rain
    terrain.truth = {"drainage_mmh": drainage, "trees": trees, "landslide_thr_mm": ls_thr,
                     "catch_noise": np.exp(rng.normal(0, 0.35, z.shape)), "river_dist": river_dist,
                     "true_grid": true_grid}
    return terrain


def catchment_multiplier(acc: np.ndarray) -> np.ndarray:
    """Converts flow accumulation into a run-on multiplier for local ponding."""
    return np.clip(1.0 + np.log1p(acc) / 2.0, 1.0, 6.0)
