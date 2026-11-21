"""Static map model: terrain heights, a 2 m walk-surface grid, line of sight, and time-sliced A*."""
import heapq
import json
import math
import os
import sys
import time
from array import array

CELL = 2.0
SQ2 = math.sqrt(2.0)
NBRS = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
        (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2)]


class World:
    def __init__(self, map_dir, mech_cfg):
        base = os.path.join(map_dir, "v2")
        with open(os.path.join(base, "world.json")) as f:
            w = json.load(f)
        t = w["terrain"]
        self.tminx, self.tminy, self.tstep = t["minX"], t["minY"], t["step"]
        self.tnx, self.tny = t["nx"], t["ny"]
        self.tscale = t.get("scale", 0.01)
        th = array("h")
        with open(os.path.join(base, t.get("file", "terrain.i16")), "rb") as f:
            th.frombytes(f.read())
        if sys.byteorder != "little":
            th.byteswap()
        self.th = th
        b = w["bounds"]
        self.bminx, self.bmaxx, self.bminy, self.bmaxy = b["minX"], b["maxX"], b["minY"], b["maxY"]

        self.height = float(mech_cfg.get("height", 9))
        step_up = float(mech_cfg.get("step_up", 1.2))
        slope = math.tan(math.radians(float(mech_cfg.get("max_slope_deg", 50))))
        self.ledge_lim = step_up * 0.95
        self.slope_lim = min(CELL * slope * 0.92, 2.2)
        self.max_climb = 28.0  # jump + jets: 100 energy climbs ~60 m (protocol §7); keep half in reserve

        self.gx0, self.gy0 = self.tminx, self.tminy
        self.nx = int((self.tnx - 1) * self.tstep / CELL)
        self.ny = int((self.tny - 1) * self.tstep / CELL)
        n = self.nx * self.ny
        ground = array("f", [0.0]) * n
        for j in range(self.ny):
            y = self.gy0 + (j + 0.5) * CELL
            row = j * self.nx
            for i in range(self.nx):
                ground[row + i] = self.ground_h(self.gx0 + (i + 0.5) * CELL, y)
        self.ground = ground
        self.solids = [None] * n
        for p in w["prisms"]:
            self._raster(p)
        # walk surface: climb onto anything whose base is under our head
        surf = array("f", ground)
        isp = bytearray(n)
        for k in range(n):
            s = self.solids[k]
            if s:
                s.sort()
                h = surf[k]
                for lo, hi in s:
                    if lo < h + self.height and hi > h:
                        h = hi
                        isp[k] = 1
                surf[k] = h
        # out of bounds (and a margin) = wall
        oob = bytearray(n)
        for j in range(self.ny):
            y = self.gy0 + (j + 0.5) * CELL
            for i in range(self.nx):
                x = self.gx0 + (i + 0.5) * CELL
                if x < self.bminx + 4 or x > self.bmaxx - 4 or y < self.bminy + 4 or y > self.bmaxy - 4:
                    oob[j * self.nx + i] = 1
        self.surf, self.isp, self.oob = surf, isp, oob
        # wall-adjacent penalty so paths keep a body width off walls
        pen = bytearray(n)
        nx = self.nx
        for j in range(1, self.ny - 1):
            for i in range(1, nx - 1):
                k = j * nx + i
                h = surf[k]
                for di, dj, _ in NBRS:
                    if surf[k + dj * nx + di] - h > 3.0:
                        pen[k] = 1
                        break
        self.pen = pen

    # ---------------------------------------------------------------- sampling
    def ground_h(self, x, y):
        fx = (x - self.tminx) / self.tstep
        fy = (y - self.tminy) / self.tstep
        i = int(fx)
        j = int(fy)
        if i < 0 or j < 0 or i >= self.tnx - 1 or j >= self.tny - 1:
            i = min(max(i, 0), self.tnx - 2)
            j = min(max(j, 0), self.tny - 2)
            fx, fy = i, j
        ax, ay = fx - i, fy - j
        th, nx = self.th, self.tnx
        k = j * nx + i
        h = (th[k] * (1 - ax) + th[k + 1] * ax) * (1 - ay) + (th[k + nx] * (1 - ax) + th[k + nx + 1] * ax) * ay
        return h * self.tscale

    def cell(self, x, y):
        i = int((x - self.gx0) / CELL)
        j = int((y - self.gy0) / CELL)
        if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
            return -1
        return j * self.nx + i

    def center(self, k):
        j, i = divmod(k, self.nx)
        return (self.gx0 + (i + 0.5) * CELL, self.gy0 + (j + 0.5) * CELL)

    def _raster(self, p):
        ring = p["ring"]
        lo, hi = float(p["base"]), float(p["top"])
        edges = []
        for poly in [ring] + list(p.get("holes") or []):
            m = len(poly) // 2
            for a in range(m):
                b = (a + 1) % m
                edges.append((poly[2 * a], poly[2 * a + 1], poly[2 * b], poly[2 * b + 1]))
        ys = ring[1::2]
        j0 = max(0, int((min(ys) - self.gy0) / CELL - 0.5))
        j1 = min(self.ny - 1, int((max(ys) - self.gy0) / CELL + 0.5))
        for j in range(j0, j1 + 1):
            yc = self.gy0 + (j + 0.5) * CELL
            xs = []
            for x1, y1, x2, y2 in edges:
                if (y1 <= yc < y2) or (y2 <= yc < y1):
                    xs.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
            xs.sort()
            for a in range(0, len(xs) - 1, 2):
                i0 = max(0, math.ceil((xs[a] - self.gx0) / CELL - 0.5))
                i1 = min(self.nx - 1, math.floor((xs[a + 1] - self.gx0) / CELL - 0.5))
                for i in range(i0, i1 + 1):
                    k = j * self.nx + i
                    s = self.solids[k]
                    if s is None:
                        self.solids[k] = [(lo, hi)]
                    else:
                        s.append((lo, hi))

    # ---------------------------------------------------------------- queries
    def los(self, a, b, step=1.5):
        """3D segment a->b clear of terrain and prisms (trees ignored: they don't stop shots)."""
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        d = math.hypot(dx, dy)
        n = max(1, int(d / step))
        ground, solids, gx0, gy0, nx, ny = self.ground, self.solids, self.gx0, self.gy0, self.nx, self.ny
        for s in range(1, n):
            f = s / n
            x, y, z = a[0] + dx * f, a[1] + dy * f, a[2] + dz * f
            i = int((x - gx0) / CELL)
            j = int((y - gy0) / CELL)
            if i < 0 or j < 0 or i >= nx or j >= ny:
                return False
            k = j * nx + i
            if ground[k] > z + 0.6:
                return False
            sol = solids[k]
            if sol:
                for lo, hi in sol:
                    if lo <= z <= hi:
                        return False
        return True

    def passable(self, k, nk, diag):
        if self.oob[nk]:
            return False
        dh = self.surf[nk] - self.surf[k]
        lim = self.ledge_lim if (self.isp[k] or self.isp[nk]) else self.slope_lim
        return dh <= lim * (SQ2 if diag else 1.0)

    def climb_cost(self, k, nk, diag):
        """Extra cost of stepping k->nk: 0 walk, >0 needs a jump+jet, None impossible."""
        if self.oob[nk]:
            return None
        dh = self.surf[nk] - self.surf[k]
        lim = self.ledge_lim if (self.isp[k] or self.isp[nk]) else self.slope_lim
        if dh <= lim * (SQ2 if diag else 1.0):
            return 0.0
        if dh <= self.max_climb:
            return 10.0 + 2.0 * dh
        return None

    def walk_clear(self, a, b):
        """Straight walk a->b (2D) over passable cells."""
        dx, dy = b[0] - a[0], b[1] - a[1]
        d = math.hypot(dx, dy)
        n = max(1, int(d / 1.0))
        prev = self.cell(a[0], a[1])
        if prev < 0:
            return False
        for s in range(1, n + 1):
            f = s / n
            k = self.cell(a[0] + dx * f, a[1] + dy * f)
            if k < 0:
                return False
            if k != prev:
                if not self.passable(prev, k, False):
                    return False
                prev = k
        return True

    def nearest_open(self, k, max_r=25):
        """Nearest cell at ground-ish level that isn't a wall/roof/out of bounds."""
        if k < 0:
            return -1
        if not self.oob[k] and self.surf[k] - self.ground[k] < 1.5:
            return k
        j0, i0 = divmod(k, self.nx)
        for r in range(1, max_r):
            best, bd = -1, 1e9
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    i, j = i0 + di, j0 + dj
                    if 0 <= i < self.nx and 0 <= j < self.ny:
                        kk = j * self.nx + i
                        if not self.oob[kk] and self.surf[kk] - self.ground[kk] < 1.5 and not self.pen[kk]:
                            d = di * di + dj * dj
                            if d < bd:
                                best, bd = kk, d
            if best >= 0:
                return best
        return -1

    def cover_score(self, k):
        """How many cells within 2 cells are tall walls: higher = more cover."""
        j0, i0 = divmod(k, self.nx)
        h = self.surf[k]
        c = 0
        for dj in (-2, -1, 0, 1, 2):
            for di in (-2, -1, 0, 1, 2):
                i, j = i0 + di, j0 + dj
                if 0 <= i < self.nx and 0 <= j < self.ny and self.surf[j * self.nx + i] - h > 6:
                    c += 1
        return c


class PathSearch:
    """Weighted A* on the 2 m grid that can be advanced a slice at a time across decisions."""

    def __init__(self, world, start, goal, weight=1.4, max_exp=60000):
        self.w = world
        self.start = world.nearest_open(world.cell(*start), 6)
        if self.start < 0:
            self.start = world.cell(*start)
        self.goal = world.nearest_open(world.cell(*goal))
        self.goal_xy = goal
        self.weight = weight
        self.max_exp = max_exp
        self.exp = 0
        self.done = self.start < 0 or self.goal < 0
        self.path = None
        if self.done:
            return
        self.gx, self.gy = divmod(self.goal, world.nx)[1], divmod(self.goal, world.nx)[0]
        self.g = {self.start: 0.0}
        self.came = {self.start: -1}
        self.open = [(self._h(self.start), self.start)]
        self.best, self.besth = self.start, self._h(self.start)

    def _h(self, k):
        j, i = divmod(k, self.w.nx)
        dx, dy = abs(i - self.gx), abs(j - self.gy)
        return self.weight * (max(dx, dy) + (SQ2 - 1) * min(dx, dy))

    def step(self, deadline):
        if self.done:
            return True
        w = self.w
        nx, ny = w.nx, w.ny
        g, came, op = self.g, self.came, self.open
        pen = w.pen
        it = 0
        while op:
            it += 1
            if (it & 15) == 0 and time.perf_counter() > deadline:
                return False
            _, k = heapq.heappop(op)
            if k == self.goal:
                self._finish(k)
                return True
            self.exp += 1
            if self.exp > self.max_exp:
                self._finish(self.best)
                return True
            gk = g[k]
            j, i = divmod(k, nx)
            for di, dj, c in NBRS:
                ii, jj = i + di, j + dj
                if ii < 0 or jj < 0 or ii >= nx or jj >= ny:
                    continue
                nk = jj * nx + ii
                diag = di != 0 and dj != 0
                extra = w.climb_cost(k, nk, diag)
                if extra is None:
                    continue
                if diag and not (w.passable(k, j * nx + ii, False) and w.passable(k, jj * nx + i, False)):
                    continue
                ng = gk + c + extra + (1.5 if pen[nk] else 0.0)
                if ng < g.get(nk, 1e18):
                    g[nk] = ng
                    came[nk] = k
                    h = self._h(nk)
                    if h < self.besth:
                        self.best, self.besth = nk, h
                    heapq.heappush(op, (ng + h, nk))
        self._finish(self.best)
        return True

    def _finish(self, k):
        cells = []
        while k != -1:
            cells.append(k)
            k = self.came[k]
        cells.reverse()
        self.path = [self.w.center(c) for c in cells]
        self.done = True
        self.g = self.came = self.open = None
