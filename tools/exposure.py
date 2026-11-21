"""Offline: how visible is every standable spot on the (fixed) map? Writes bot/exposure.u8 and bot/hideouts.json.

Exposure of a cell = how many sample viewers (ground cells on a VIEW_STEP grid, eye = surface + 8 m) can see a mech
standing there (its center or top), using protocol.md section 8:
  - within near_range (45 m): line of sight blocked by buildings and terrain only (canopy doesn't hide you)
  - 45..200 m: also blocked by ~>3.5 m of dense tree crown along the line
FOV is ignored (anyone could turn to look). Run from the repo folder:  python3 tools/exposure.py ../arena
Pure Python, uses every core; a few minutes.
"""
import json
import math
import os
import sys
import time
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
from world import CELL, World  # noqa: E402

VIEW_STEP = 8.0      # viewer grid spacing (m); counts are rescaled to a 16 m grid
CAND_STEP = 2        # candidate every 2nd 2 m cell = 4 m grid
NEAR, FAR = 45.0, 200.0
EYE, CENTER, TOP = 8.0, 4.5, 9.0
CROWN_BLOCK = 3.5    # m of crown along a line that blocks sight (canopyDensity 0.35, canopyBlockT 0.25)
MAX_UP = 40.0        # roofs higher than this above local ground are out of reach: skip

W = None
CROWNS = None
VIEWERS = None
VBUCKET = None


def build_crowns(world, map_dir):
    with open(os.path.join(map_dir, "v2", "world.json")) as f:
        tr = json.load(f)["trees"]
    fl = tr["fields"]
    d = tr["data"]
    n = len(fl)
    ix = {k: fl.index(k) for k in fl}
    crowns = [None] * (world.nx * world.ny)
    for t in range(len(d) // n):
        r = d[t * n:(t + 1) * n]
        z = r[ix["z"]]
        lo, hi = z + r[ix["crownBase"]], z + r[ix["height"]]
        cx = r[ix["crownX"]] if "crownX" in ix else r[ix["x"]]
        cy = r[ix["crownY"]] if "crownY" in ix else r[ix["y"]]
        rad = r[ix["crownR"]]
        i0 = int((cx - rad - world.gx0) / CELL)
        i1 = int((cx + rad - world.gx0) / CELL)
        j0 = int((cy - rad - world.gy0) / CELL)
        j1 = int((cy + rad - world.gy0) / CELL)
        for j in range(max(0, j0), min(world.ny - 1, j1) + 1):
            for i in range(max(0, i0), min(world.nx - 1, i1) + 1):
                x, y = world.gx0 + (i + 0.5) * CELL, world.gy0 + (j + 0.5) * CELL
                if (x - cx) ** 2 + (y - cy) ** 2 <= rad * rad:
                    k = j * world.nx + i
                    if crowns[k] is None:
                        crowns[k] = [(lo, hi)]
                    else:
                        crowns[k].append((lo, hi))
    return crowns


def sees(a, b, canopy, step=1.5):
    w = W
    dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    d = math.sqrt(dx * dx + dy * dy + dz * dz)
    n = max(1, int(d / step))
    seg = d / n
    ground, solids, crowns = w.ground, w.solids, CROWNS
    gx0, gy0, nx, ny = w.gx0, w.gy0, w.nx, w.ny
    foliage = 0.0
    for s in range(1, n):
        f = s / n
        x, y, z = a[0] + dx * f, a[1] + dy * f, a[2] + dz * f
        i = int((x - gx0) / CELL)
        j = int((y - gy0) / CELL)
        if i < 0 or j < 0 or i >= nx or j >= ny:
            return False
        k = j * nx + i
        if ground[k] > z + 0.3:
            return False
        sol = solids[k]
        if sol:
            for lo, hi in sol:
                if lo <= z <= hi:
                    return False
        if canopy:
            cr = crowns[k]
            if cr:
                for lo, hi in cr:
                    if lo <= z <= hi:
                        foliage += seg
                        if foliage > CROWN_BLOCK:
                            return False
                        break
    return True


def init(map_dir):
    global W, CROWNS, VIEWERS, VBUCKET
    W = World(map_dir, {})
    CROWNS = build_crowns(W, map_dir)
    VIEWERS, VBUCKET = [], {}
    y = W.gy0 + VIEW_STEP / 2
    while y < W.gy0 + W.ny * CELL:
        x = W.gx0 + VIEW_STEP / 2
        while x < W.gx0 + W.nx * CELL:
            k = W.cell(x, y)
            if k >= 0 and not W.oob[k] and W.surf[k] - W.ground[k] < 1.5:
                v = (x, y, W.surf[k] + EYE)
                VBUCKET.setdefault((int((x - W.gx0) // VIEW_STEP), int((y - W.gy0) // VIEW_STEP)), []).append(v)
                VIEWERS.append(v)
            x += VIEW_STEP
        y += VIEW_STEP


def standable(k):
    if W.oob[k]:
        return False
    return W.surf[k] - W.ground[k] <= MAX_UP


def exposure_of(k):
    x, y = W.center(k)
    h = W.surf[k]
    c, t = (x, y, h + CENTER), (x, y, h + TOP)
    bi, bj = int((x - W.gx0) // VIEW_STEP), int((y - W.gy0) // VIEW_STEP)
    r = int(FAR // VIEW_STEP) + 1
    count = 0
    for dj in range(-r, r + 1):
        for di in range(-r, r + 1):
            for v in VBUCKET.get((bi + di, bj + dj), ()):
                dd = math.sqrt((v[0] - x) ** 2 + (v[1] - y) ** 2 + (v[2] - c[2]) ** 2)
                if dd > FAR or dd < 3:
                    continue
                canopy = dd > NEAR
                if sees(v, t, canopy) or sees(v, c, canopy):
                    count += 1
    return count


def work(ks):
    scale = (VIEW_STEP / 16.0) ** 2
    return [(k, exposure_of(k) * scale) for k in ks]


def main():
    map_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BOT, "..", "arena")
    t0 = time.time()
    init(map_dir)
    cands = [j * W.nx + i for j in range(0, W.ny, CAND_STEP) for i in range(0, W.nx, CAND_STEP) if standable(j * W.nx + i)]
    print(f"{len(VIEWERS)} viewers, {len(cands)} candidates; map load {time.time() - t0:.1f}s", flush=True)
    chunks = [cands[i::64] for i in range(64)]
    res = {}
    with Pool(os.cpu_count(), initializer=init, initargs=(map_dir,)) as pool:
        for n, part in enumerate(pool.imap_unordered(work, chunks)):
            res.update(part)
            print(f"  {n + 1}/64 chunks, {time.time() - t0:.0f}s", flush=True)
    # spread the 4 m samples onto the 2 m grid; unreachable/out-of-bounds cells get 255
    exp = bytearray([255]) * (W.nx * W.ny)
    for j in range(W.ny):
        for i in range(W.nx):
            k = j * W.nx + i
            if not standable(k):
                continue
            kk = (j - j % CAND_STEP) * W.nx + (i - i % CAND_STEP)
            v = res.get(kk)
            if v is None:
                continue
            exp[k] = min(254, int(round(v)))
    with open(os.path.join(BOT, "exposure.u8"), "wb") as f:
        f.write(bytes(exp))
    # hideouts: low-exposure 4 m samples whose 3x3 neighbourhood is also low (room to shuffle), thinned to 10 m apart
    picks = []
    for k, v in res.items():
        j, i = divmod(k, W.nx)
        nb = [res.get((j + dj) * W.nx + (i + di)) for dj in (-CAND_STEP, 0, CAND_STEP) for di in (-CAND_STEP, 0, CAND_STEP)]
        if any(x is None for x in nb):
            continue
        score = max(nb)
        if score <= 25:
            x, y = W.center(k)
            picks.append((round(score, 1), round(v, 1), x, y, round(W.surf[k], 2), round(W.surf[k] - W.ground[k], 2)))
    picks.sort()
    hide = []
    for p in picks:
        if all((p[2] - q[2]) ** 2 + (p[3] - q[3]) ** 2 > 100 for q in hide):
            hide.append(p)
    with open(os.path.join(BOT, "hideouts.json"), "w") as f:
        json.dump([{"x": round(p[2], 1), "y": round(p[3], 1), "z": p[4], "up": p[5], "exp": p[1], "nbr": p[0]} for p in hide], f)
    print(f"done in {time.time() - t0:.0f}s: {len(hide)} hideouts", flush=True)


if __name__ == "__main__":
    main()
