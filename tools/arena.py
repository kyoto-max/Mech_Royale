"""Local practice arena: our bot (in-process) vs 15 house bots acting as one squad, as the track rules describe:
they share vision, never fight each other, and hunt us weakest first (one early, one more per kill, all late).

A rough model, not the real server: real map, movement, line of sight, vision (FOV, near range, canopy), gun /
lightning / laser / mortar, rear armor, regen, zone, pickups, kill cubes. No drones, crates, drops or upgrades.
House-bot skill (aim error, reaction, weapons) is a guess per tier. Use it to rank variants, then confirm by submission.

    python3 tools/arena.py [--matches 24] [--strategy duelist,survivor] [--seed 1]
"""
import argparse
import gzip
import json
import math
import os
import random
import sys
import time
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
KIT = os.path.dirname(BOT)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)

DT = 0.05
TIERS = (["easy"] * 4) + (["medium"] * 4) + (["hard"] * 4) + (["expert"] * 3)
SKILL = {  # aim noise (deg), reaction (s), lead fraction, strafe, loadout, engage range for idle bots
    "easy":   dict(noise=3.5, react=0.7, lead=0.0, strafe=0.0, weapons=["gun"]),
    "medium": dict(noise=2.2, react=0.45, lead=0.6, strafe=0.5, weapons=["gun", "mortar"]),
    "hard":   dict(noise=1.4, react=0.3, lead=1.0, strafe=0.8, weapons=["gun", "laser"]),
    "expert": dict(noise=0.8, react=0.2, lead=1.0, strafe=1.0, weapons=["laser", "lightning"]),
}
LATE_T = float(os.environ.get("ARENA_LATE", 300))
IDLE_FIGHT = float(os.environ.get("ARENA_IDLE_FIGHT", 0))     # idle (non-hunting) bots fight us within this range
NOISE_X = float(os.environ.get("ARENA_NOISE", 1.6))            # scales house-bot aim error
PROVOKE = os.environ.get("ARENA_PROVOKE", "1") == "1"          # a bot we hit becomes a hunter

W = None
E = None
HELLO = None
BASE_OBS = None


def setup():
    global W, E, HELLO, BASE_OBS
    import exposure as E_
    E = E_
    E.init(os.path.join(KIT, "arena"))
    W = E.W
    msgs = [json.loads(l) for l in gzip.open(os.path.join(KIT, "check", "stream.jsonl.gz"), "rt") if l.strip()]
    HELLO = msgs[0]
    HELLO["map"]["dir"] = os.path.join(KIT, "arena")
    BASE_OBS = [m for m in msgs if m.get("type") == "obs"][0]


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Mech:
    def __init__(self, mid, pos, cfg, tier=None):
        m = cfg["mech"]
        self.id, self.tier = mid, tier
        self.pos = list(pos)
        self.vel = [0.0, 0.0, 0.0]
        self.aim = [0.0, 0.0]
        self.want_aim = [0.0, 0.0]
        self.hp = self.max_hp = float(m["hp"])
        self.energy = float(m["energy"])
        self.spend_t = -9.0
        self.hit_t = -9.0
        self.cubes = 0
        self.alive = True
        self.grounded = True
        self.weapons = [None, None]
        self.kills = 0
        self.act = {}
        self.charging = [0.0, 0.0]
        self.short_until = 0.0
        self.step_t = 0.0

    def give(self, slot, kind, cfg):
        st = dict(cfg["weapons"][kind])
        w = {"kind": kind, "cooldown": 0.0, "stats": st}
        if kind == "gun":
            w["ammo"], w["reload"] = st["mag"], 0.0
        if kind == "laser":
            w["charge"] = 0.0
        self.weapons[slot] = w

    def kinds(self):
        return [w["kind"] for w in self.weapons if w]

    def dmg_mult(self, cfg):
        return 1 + cfg["cubes"]["damage_per_cube"] * min(self.cubes, cfg["cubes"]["stat_cap"])


class Match:
    def __init__(self, seed, strategy):
        import bot as B
        B.STRATEGY = strategy
        B.PROF = B.PROFILES[strategy]
        self.B = B
        self.rng = random.Random(seed)
        self.cfg = HELLO["config"]
        self.t = 0.0
        self.tick = 0
        self.bullets = []   # [pos, vel, owner, age, kind]
        self.shells = []
        self.pickups = {}
        self.cubes = {}
        self.events = {0: []}
        self.sounds = {0: []}
        self.beams = []
        self.next_id = 8000
        self.cube_id = 5000
        self.squad_seen_t = -99.0
        self.squad_last = None
        self.kills_by_us = 0
        self.death_order = []
        self.memory = {}  # house id -> last t we saw it (for our 5 s tracking)
        self._zone_init()
        spawns = self._spawns(16)
        self.mechs = []
        for i in range(16):
            tier = None if i == 0 else TIERS[i - 1]
            m = Mech(i, spawns[i], self.cfg, tier)
            m.give(0, "gun", self.cfg)
            if tier:
                ws = SKILL[tier]["weapons"]
                m.weapons = [None, None]
                for s, k in enumerate(ws):
                    m.give(s, k, self.cfg)
                m.aim = [self.rng.uniform(-3, 3), 0.0]
                m.path, m.path_goal, m.path_t, m.seen_t, m.strafe, m.flip = None, None, -9, -9, 1, 0
            self.mechs.append(m)
        for _ in range(self.cfg["loot"]["floor_count"]):
            p = self._open_point()
            kind = self.rng.choice(["mortar", "laser", "lightning"])
            self.pickups[self.next_id] = {"id": self.next_id, "kind": kind, "pos": [p[0], p[1], W.surf[W.cell(*p)]]}
            self.next_id += 1
        hello = json.loads(json.dumps(HELLO))
        hello["slot"] = 0
        hello["seed"] = seed
        self.bot = B.Bot.__new__(B.Bot)
        # reuse the loaded map: skip World() in __init__ by injecting it after a cheap construct
        hello2 = dict(hello, map={"dir": "/nonexistent", "files": []})
        _log = B.mf.log
        B.mf.log = lambda *a: None
        self.bot.__init__(hello2)
        B.mf.log = _log
        self.bot.world = W

    # ------------------------------------------------------------------ setup helpers
    def _open_point(self, center=None, radius=None):
        for _ in range(2000):
            if center is None:
                x = self.rng.uniform(W.bminx + 40, W.bmaxx - 40)
                y = self.rng.uniform(W.bminy + 40, W.bmaxy - 40)
            else:
                a, r = self.rng.uniform(0, 6.283), radius * math.sqrt(self.rng.random())
                x, y = center[0] + r * math.cos(a), center[1] + r * math.sin(a)
            k = W.cell(x, y)
            if k >= 0 and not W.oob[k] and W.surf[k] - W.ground[k] < 0.5 and not W.pen[k]:
                return (x, y)
        return (0.0, 0.0)

    def _spawns(self, n):
        z = self.cfg["zone"]
        pts = []
        sep = self.cfg["match"]["spawn_min_separation"]
        tries = 0
        while len(pts) < n:
            tries += 1
            p = self._open_point(z["start_center"], z["start_radius"] * self.cfg["match"]["spawn_zone_fraction"])
            if all(d2(p, q) > sep for q in pts) or tries > 5000:
                pts.append(p)
        return [(p[0], p[1], W.surf[W.cell(*p)]) for p in pts]

    def _zone_init(self):
        z = self.cfg["zone"]
        self.circles = [(tuple(z["start_center"]), z["start_radius"])]
        c, r = z["start_center"], z["start_radius"]
        for ph in z["phases"]:
            nr = ph["radius"]
            a = self.rng.uniform(0, 6.283)
            off = (r - nr) * math.sqrt(self.rng.random()) * 0.9
            c = (c[0] + off * math.cos(a), c[1] + off * math.sin(a))
            # keep the playable circle on solid ground
            k = W.cell(*c)
            if k < 0 or W.oob[k]:
                c = self.circles[-1][0]
            self.circles.append((c, nr))
            r = nr
        self.phase_times = []
        t = 0.0
        for ph in z["phases"]:
            self.phase_times.append((t + ph["hold_s"], t + ph["hold_s"] + ph["shrink_s"], ph["dps"]))
            t += ph["hold_s"] + ph["shrink_s"]

    def zone(self):
        t = self.t
        for i, (s0, s1, dps) in enumerate(self.phase_times):
            if t < s1:
                (c0, r0), (c1, r1) = self.circles[i], self.circles[i + 1]
                if t < s0:
                    return {"center": list(c0), "radius": r0, "next_center": list(c1), "next_radius": r1,
                            "shrink_start": s0, "shrink_end": s1, "dps": dps}
                f = (t - s0) / (s1 - s0)
                c = [c0[0] + (c1[0] - c0[0]) * f, c0[1] + (c1[1] - c0[1]) * f]
                return {"center": c, "radius": r0 + (r1 - r0) * f, "next_center": list(c1), "next_radius": r1,
                        "shrink_start": s0, "shrink_end": s1, "dps": dps}
        c, r = self.circles[-1]
        return {"center": list(c), "radius": r, "next_center": list(c), "next_radius": r,
                "shrink_start": 9e9, "shrink_end": 9e9, "dps": self.phase_times[-1][2]}

    # ------------------------------------------------------------------ vision
    def sees(self, a, b):
        if not b.alive:
            return False
        eye = (a.pos[0], a.pos[1], a.pos[2] + 8)
        dist = math.dist(eye, (b.pos[0], b.pos[1], b.pos[2] + 4.5))
        if dist > 200:
            return False
        near = dist <= 45
        if not near and abs(wrap(math.atan2(b.pos[1] - a.pos[1], b.pos[0] - a.pos[0]) - a.aim[0])) > math.radians(60):
            return False
        top = (b.pos[0], b.pos[1], b.pos[2] + 9)
        cen = (b.pos[0], b.pos[1], b.pos[2] + 4.5)
        return E.sees(eye, top, not near) or E.sees(eye, cen, not near)

    def point_visible(self, a, p):
        eye = (a.pos[0], a.pos[1], a.pos[2] + 8)
        dist = math.dist(eye, p)
        if dist > 200:
            return False
        near = dist <= 45
        if not near and abs(wrap(math.atan2(p[1] - a.pos[1], p[0] - a.pos[0]) - a.aim[0])) > math.radians(60):
            return False
        return E.sees(eye, (p[0], p[1], p[2] + 0.5), not near)

    # ------------------------------------------------------------------ observation for our bot
    def obs(self):
        me = self.mechs[0]
        z = self.zone()
        st = dict(self.cfg["mech"])
        sc = 1 + 0.05 * me.cubes
        st.update(muzzle_height=7 * sc, eye_height=8 * sc, radius=2.5 * sc, height=9 * sc)
        mechs = []
        for m in self.mechs[1:]:
            if not m.alive:
                continue
            if self.sees(me, m):
                self.memory[m.id] = self.t
            if self.t - self.memory.get(m.id, -99) <= 5.0:
                mechs.append({"id": m.id, "kind": "mech", "pos": [round(v, 2) for v in m.pos], "vel": [round(v, 2) for v in m.vel],
                              "aim": list(m.aim), "hp": m.hp, "max_hp": m.max_hp, "cubes": m.cubes, "weapons": m.kinds(),
                              "charging": any(c > 0 for c in m.charging)})
        projs = []
        for b in self.bullets + self.shells:
            if b[2] != 0 and math.dist(b[0], me.pos) < 200:
                projs.append({"id": id(b) % 100000, "kind": b[4], "pos": list(b[0]), "vel": list(b[1])})
        pk = [dict(p) for p in self.pickups.values() if d2(p["pos"], me.pos) < 200 and self.point_visible(me, p["pos"])]
        cb = [{"id": k, "pos": list(v)} for k, v in self.cubes.items() if d2(v, me.pos) < 200 and self.point_visible(me, v)]
        ws = []
        for w in me.weapons:
            if w is None:
                ws.append(None)
                continue
            o = {"kind": w["kind"], "cooldown": max(0.0, w["cooldown"]), "stats": w["stats"]}
            if w["kind"] == "gun":
                o["ammo"], o["reload"] = w["ammo"], max(0.0, w["reload"])
            if w["kind"] == "laser":
                o["charge"] = w.get("charge", 0.0)
            ws.append(o)
        o = {"type": "obs", "tick": self.tick, "t": round(self.t, 2), "alive": sum(m.alive for m in self.mechs),
             "self": {"id": 0, "pos": list(me.pos), "vel": list(me.vel), "aim": list(me.aim), "hp": me.hp, "max_hp": me.max_hp,
                      "energy": me.energy, "max_energy": self.cfg["mech"]["energy"], "grounded": me.grounded, "cubes": me.cubes,
                      "upgrades": [], "weapons": ws, "in_zone": d2(me.pos, z["center"]) < z["radius"], "stats": st},
             "mechs": mechs, "projectiles": projs, "crates": [], "cubes": cb, "pickups": pk,
             "sounds": self.sounds[0], "beams": [], "events": self.events[0], "drops": [], "zone": z, "offer": None}
        self.sounds[0], self.events[0] = [], []
        return o

    # ------------------------------------------------------------------ house AI
    def house_decide(self):
        us = self.mechs[0]
        house = [m for m in self.mechs[1:] if m.alive]
        seers = [m for m in house if self.sees(m, us)]
        if seers:
            self.squad_seen_t, self.squad_last = self.t, list(us.pos)
        n_hunt = len(house) if self.t > LATE_T else min(len(house), 1 + self.kills_by_us)
        order = sorted(house, key=lambda m: TIERS.index(m.tier) + m.id * 1e-3)
        hunters = set(m.id for m in order[:n_hunt])
        z = self.zone()
        for m in house:
            sk = SKILL[m.tier]
            sees = m in seers
            if sees and m.seen_t < 0:
                m.seen_t = self.t
            if not sees:
                m.seen_t = -9
            dist = d2(m.pos, us.pos)
            if PROVOKE and self.t - getattr(m, "hurt_by_us", -99) < 8:
                hunters.add(m.id)
            fight = sees and (m.id in hunters or dist < IDLE_FIGHT)
            act = {"move": [0.0, 0.0], "fire": [False, False], "boost": False}
            if fight:
                # aim (lead + noise), fire after reaction time
                bspeed = m.weapons[0]["stats"].get("speed", 252) if m.weapons[0] and m.weapons[0]["kind"] == "gun" else 1e9
                tgt = [us.pos[0], us.pos[1], us.pos[2] + 4.5]
                tof = math.dist(tgt, m.pos) / bspeed if bspeed < 1e8 else 0
                tgt = [tgt[i] + us.vel[i] * tof * sk["lead"] for i in range(3)]
                n = math.radians(sk["noise"] * NOISE_X)
                yaw = math.atan2(tgt[1] - m.pos[1], tgt[0] - m.pos[0]) + self.rng.gauss(0, n)
                pit = math.atan2(tgt[2] - m.pos[2] - 7, d2(tgt, m.pos)) + self.rng.gauss(0, n * 0.5)
                act["aim"] = [yaw, pit]
                ready = self.t - m.seen_t >= sk["react"]
                for i, w in enumerate(m.weapons):
                    if not w or not ready:
                        continue
                    k = w["kind"]
                    if k == "gun":
                        act["fire"][i] = dist < 220
                    elif k == "lightning":
                        act["fire"][i] = dist < 44 and m.energy > 5
                    elif k == "laser":
                        act["fire"][i] = (m.charging[i] < w["stats"]["charge"]) and dist < 270
                    elif k == "mortar":
                        act["fire"][i] = 25 < dist < 200
                # movement: hold a band distance and strafe
                want = 25 if "lightning" in m.kinds() else 70
                by = math.atan2(us.pos[1] - m.pos[1], us.pos[0] - m.pos[0])
                if self.t > m.flip:
                    m.strafe, m.flip = -m.strafe, self.t + self.rng.uniform(0.6, 1.6)
                rk = max(-1, min(1, (dist - want) / 20))
                act["move"] = [math.cos(by) * rk - math.sin(by) * m.strafe * sk["strafe"],
                               math.sin(by) * rk + math.cos(by) * m.strafe * sk["strafe"]]
            else:
                if m.id in hunters:
                    goal = self.squad_last if (self.squad_last and self.t - self.squad_seen_t < 15) else us.pos
                    if not (self.squad_last and self.t - self.squad_seen_t < 15):
                        goal = [us.pos[0] + self.rng.gauss(0, 25), us.pos[1] + self.rng.gauss(0, 25)]
                    act["boost"] = m.energy > 50 and dist > 120
                else:
                    c, r = z["next_center"], z["next_radius"]
                    if not hasattr(m, "roam") or d2(m.roam, c) > r * 0.6 or d2(m.pos, m.roam) < 4:
                        m.roam = self._open_point(c, r * 0.55)
                    goal = m.roam
                act["move"] = self._steer(m, goal)
                act["aim"] = [math.atan2(act["move"][1], act["move"][0]) if any(act["move"]) else m.aim[0], 0.0]
                if self.t - self.squad_seen_t < 3 and m.id in hunters:
                    act["aim"] = [math.atan2(us.pos[1] - m.pos[1], us.pos[0] - m.pos[0]), 0.0]
            # stay in zone
            if d2(m.pos, z["center"]) > z["radius"] - 5:
                by = math.atan2(z["center"][1] - m.pos[1], z["center"][0] - m.pos[0])
                act["move"] = [math.cos(by), math.sin(by)]
                act["boost"] = True
            m.act = act

    def _steer(self, m, goal):
        import world
        goal = (goal[0], goal[1])
        if m.path is None or m.path_goal is None or d2(m.path_goal, goal) > 15 or self.t - m.path_t > 6:
            ps = world.PathSearch(W, (m.pos[0], m.pos[1]), goal, max_exp=6000)
            while not ps.step(time.perf_counter() + 1):
                pass
            m.path, m.path_goal, m.path_t, m.pi = ps.path or [], goal, self.t, 0
        P = m.path
        while P and m.pi < len(P) - 1 and d2(P[m.pi], m.pos) < 3:
            m.pi += 1
        tgt = P[min(m.pi + 2, len(P) - 1)] if P else goal
        dx, dy = tgt[0] - m.pos[0], tgt[1] - m.pos[1]
        n = math.hypot(dx, dy)
        return [dx / n, dy / n] if n > 1.5 else [0.0, 0.0]

    # ------------------------------------------------------------------ physics and weapons
    def damage(self, src, tgt, amount, weapon, from_xy):
        if not tgt.alive:
            return
        cfg = self.cfg["mech"]
        bearing = math.atan2(from_xy[1] - tgt.pos[1], from_xy[0] - tgt.pos[0])
        rear = abs(wrap(bearing - tgt.aim[0])) > math.radians(cfg["front_arc_deg"] / 2)
        amt = amount * (src.dmg_mult(self.cfg) if src else 1.0) * (cfg["rear_mult"] if rear else 1.0)
        tgt.hp -= amt
        tgt.hit_t = self.t
        if src:
            src.hit_t = self.t
        if tgt.id == 0:
            self.events[0].append({"type": "damaged", "amount": round(amt, 1), "weapon": weapon, "bearing": bearing,
                                   "by": src.id if src else None, "rear": rear})
        if src is not None and src.id == 0:
            tgt.hurt_by_us = self.t
            self.events[0].append({"type": "hit", "target": tgt.id, "amount": round(amt, 1), "weapon": weapon})
        if tgt.hp <= 0:
            tgt.alive = False
            self.death_order.append((self.t, tgt.id))
            if src is not None and src.id == 0:
                src.kills += 1
                self.kills_by_us += 1
                self.events[0].append({"type": "kill", "target": tgt.id})
            for _ in range(tgt.cubes + self.cfg["cubes"]["death_drop_extra"]):
                self.cubes[self.cube_id] = [tgt.pos[0] + self.rng.uniform(-2, 2), tgt.pos[1] + self.rng.uniform(-2, 2), tgt.pos[2]]
                self.cube_id += 1
            for w in tgt.weapons:
                if w and w["kind"] != "gun":
                    self.pickups[self.next_id] = {"id": self.next_id, "kind": w["kind"], "pos": list(tgt.pos)}
                    self.next_id += 1

    def sound(self, kind, at, by):
        rng = self.cfg["sound"]["range"].get(kind, 100)
        if by == 0:
            return
        me = self.mechs[0]
        d = d2(at, me.pos)
        if d <= rng:
            self.sounds[0].append({"kind": kind, "bearing": math.atan2(at[1] - me.pos[1], at[0] - me.pos[0]) + self.rng.gauss(0, math.radians(7)),
                                   "dist": "near" if d < 60 else "mid" if d < 160 else "far", "age": 0.05})

    def fire(self, m, i, w):
        k = w["kind"]
        st = w["stats"]
        d = [math.cos(m.aim[1]) * math.cos(m.aim[0]), math.cos(m.aim[1]) * math.sin(m.aim[0]), math.sin(m.aim[1])]
        mz = [m.pos[0] + 3 * d[0], m.pos[1] + 3 * d[1], m.pos[2] + 7 + 3 * d[2]]
        if k == "gun":
            if w["reload"] > 0 or w["ammo"] <= 0 or w["cooldown"] > 0:
                return
            sp = math.radians(st["spread_deg"])
            y, p = m.aim[0] + self.rng.gauss(0, sp), m.aim[1] + self.rng.gauss(0, sp)
            v = [st["speed"] * math.cos(p) * math.cos(y), st["speed"] * math.cos(p) * math.sin(y), st["speed"] * math.sin(p)]
            self.bullets.append([mz, v, m.id, 0.0, "gun", st["damage"], st["range"] / st["speed"]])
            w["cooldown"] = st["cooldown"]
            w["ammo"] -= 1
            if w["ammo"] <= 0:
                w["reload"] = st["reload"]
            self.sound("gun", m.pos, m.id)
        elif k == "lightning":
            if w["cooldown"] > 0 or m.energy < st["energy"]:
                return
            m.energy -= st["energy"]
            m.spend_t = self.t
            w["cooldown"] = st["cooldown"]
            best, bd = None, 1e9
            for o in self.mechs:
                if o is m or not o.alive or (o.id != 0 and m.id != 0):
                    continue
                dd = math.dist(m.pos, o.pos)
                if dd < st["range"] and abs(wrap(math.atan2(o.pos[1] - m.pos[1], o.pos[0] - m.pos[0]) - m.aim[0])) < math.radians(st["cone_deg"] / 2):
                    if dd < bd and W.los(mz, (o.pos[0], o.pos[1], o.pos[2] + 4.5)):
                        best, bd = o, dd
            if best:
                self.damage(m, best, st["damage"], "lightning", m.pos)
                if "laser" in best.kinds():
                    best.short_until = self.t + st["short_s"]
                    best.charging = [0.0, 0.0]
            self.sound("lightning", m.pos, m.id)
        elif k == "mortar":
            if w["cooldown"] > 0:
                return
            w["cooldown"] = st["cooldown"]
            v = [st["speed"] * x for x in d]
            self.shells.append([[m.pos[0], m.pos[1], m.pos[2] + 9.5], v, m.id, 0.0, "mortar"])
            self.sound("mortar", m.pos, m.id)

    def laser_shot(self, m, w, charge):
        st = w["stats"]
        frac = st["min_frac"] + (1 - st["min_frac"]) * min(1.0, charge / st["charge"])
        dmg = st["damage"] * frac
        d = [math.cos(m.aim[1]) * math.cos(m.aim[0]), math.cos(m.aim[1]) * math.sin(m.aim[0]), math.sin(m.aim[1])]
        mz = [m.pos[0], m.pos[1], m.pos[2] + 7]
        best, bt = None, 1e9
        for o in self.mechs:
            if o is m or not o.alive or (o.id != 0 and m.id != 0):
                continue
            c = [o.pos[0] - mz[0], o.pos[1] - mz[1]]
            tt = c[0] * d[0] + c[1] * d[1]
            if tt <= 0 or tt > st["range"]:
                continue
            px, py = d[0] * tt - c[0], d[1] * tt - c[1]
            zz = mz[2] + d[2] * tt / max(1e-6, math.hypot(d[0], d[1]))
            if px * px + py * py < 2.5 ** 2 and o.pos[2] - 0.5 <= zz <= o.pos[2] + 9.5 and tt < bt:
                if W.los(mz, (o.pos[0], o.pos[1], zz)):
                    best, bt = o, tt
        if best:
            mult = 1 + st.get("cookoff", 0) if "mortar" in best.kinds() else 1
            self.damage(m, best, dmg * mult, "laser", m.pos)
        w["cooldown"] = st["cooldown"]
        self.sound("laser", m.pos, m.id)

    def step(self):
        cfg = self.cfg["mech"]
        z = self.zone()
        for m in self.mechs:
            if not m.alive:
                continue
            a = m.act
            # aim slew
            want = a.get("aim", m.aim)
            dy = wrap(want[0] - m.aim[0])
            m.aim[0] = wrap(m.aim[0] + max(-cfg["turn_rate"] * DT, min(cfg["turn_rate"] * DT, dy)))
            m.aim[1] += max(-cfg["pitch_rate"] * DT, min(cfg["pitch_rate"] * DT, want[1] - m.aim[1]))
            m.aim[1] = max(cfg["min_pitch"], min(cfg["max_pitch"], m.aim[1]))
            # weapons
            for i, w in enumerate(m.weapons):
                if not w:
                    continue
                w["cooldown"] -= DT
                if w["kind"] == "gun" and w["reload"] > 0:
                    w["reload"] -= DT
                    if w["reload"] <= 0:
                        w["ammo"], w["reload"] = w["stats"]["mag"], 0.0
                if w["kind"] == "gun" and a.get("reload") and w["reload"] <= 0 and w["ammo"] < w["stats"]["mag"]:
                    w["reload"] = w["stats"]["reload"]
                trig = bool(a.get("fire", [False, False])[i])
                if w["kind"] == "laser":
                    if self.t < m.short_until:
                        m.charging[i] = 0.0
                        w["charge"] = 0.0
                        continue
                    if trig and w["cooldown"] <= 0:
                        m.charging[i] += DT
                        w["charge"] = m.charging[i]
                        if m.charging[i] >= w["stats"]["charge"] + w["stats"]["hold_s"]:
                            self.laser_shot(m, w, m.charging[i])
                            m.charging[i] = w["charge"] = 0.0
                    elif m.charging[i] > 0:
                        if m.charging[i] >= w["stats"]["min_charge"]:
                            self.laser_shot(m, w, m.charging[i])
                            m.charging[i] = w["charge"] = 0.0
                        else:
                            m.charging[i] += DT
                            w["charge"] = m.charging[i]
                elif trig:
                    self.fire(m, i, w)
            # movement
            mv = a.get("move", [0, 0])
            n = math.hypot(mv[0], mv[1])
            if n > 1:
                mv = [mv[0] / n, mv[1] / n]
            boost = a.get("boost") and m.energy > 0.5
            speed = cfg["boost_speed"] if (boost and m.grounded) else cfg["walk_speed"]
            if any(c > 0 for c in m.charging):
                speed *= cfg["charge_move_mult"]
            acc = cfg["ground_accel"] if m.grounded else cfg["air_accel"]
            for i in range(2):
                dv = mv[i] * speed - m.vel[i]
                m.vel[i] += max(-acc * DT, min(acc * DT, dv))
            if boost and (math.hypot(mv[0], mv[1]) > 0.1 or not m.grounded):
                m.energy -= (cfg["boost_drain"] if m.grounded else cfg["jet_drain"]) * DT
                m.spend_t = self.t
            if a.get("jump") and m.grounded:
                m.vel[2] = cfg["jump_speed"]
                m.grounded = False
            if not m.grounded:
                if boost:
                    m.vel[2] = min(cfg["max_rise_speed"], m.vel[2] + (cfg["jet_accel"] - cfg["gravity"]) * DT)
                else:
                    m.vel[2] -= cfg["gravity"] * DT
            k = W.cell(m.pos[0], m.pos[1])
            for nx_, ny_ in ((m.pos[0] + m.vel[0] * DT, m.pos[1] + m.vel[1] * DT), (m.pos[0] + m.vel[0] * DT, m.pos[1]), (m.pos[0], m.pos[1] + m.vel[1] * DT)):
                nk = W.cell(nx_, ny_)
                if nk < 0 or W.oob[nk]:
                    continue
                if nk == k or (m.grounded and W.passable(k, nk, False)) or W.surf[nk] <= m.pos[2] + 0.3:
                    m.pos[0], m.pos[1] = nx_, ny_
                    break
            else:
                m.vel[0] = m.vel[1] = 0.0
            m.pos[2] += m.vel[2] * DT
            floor = W.surf[W.cell(m.pos[0], m.pos[1])]
            if m.pos[2] <= floor:
                m.pos[2], m.vel[2], m.grounded = floor, 0.0, True
            elif m.pos[2] > floor + 0.3 and m.grounded:
                if m.pos[2] - floor < 2.5:
                    m.pos[2] = floor
                else:
                    m.grounded = False
            if self.t - m.spend_t > cfg["energy_regen_delay"]:
                m.energy = min(cfg["energy"], m.energy + cfg["energy_regen"] * DT)
            if m.id == 0 and m.grounded and math.hypot(m.vel[0], m.vel[1]) > 5 and self.t - m.step_t > 0.6:
                m.step_t = self.t
            # zone and regen
            if d2(m.pos, z["center"]) > z["radius"]:
                m.hp -= z["dps"] * DT
                m.hit_t = self.t
                if m.id == 0:
                    pass
                if m.hp <= 0:
                    m.alive = False
                    self.death_order.append((self.t, m.id))
            elif self.t - m.hit_t > cfg["regen_delay"]:
                m.hp = min(m.max_hp, m.hp + cfg["regen_per_s"] * DT)
        # take / cubes for us
        me = self.mechs[0]
        if me.alive:
            a = me.act
            tk = a.get("take")
            if tk is not None and tk in self.pickups:
                p = self.pickups[tk]
                if d2(p["pos"], me.pos) <= 4 and abs(p["pos"][2] - me.pos[2]) <= 4 and p["kind"] not in me.kinds():
                    slot = a.get("drop")
                    if slot is None:
                        slot = 0 if me.weapons[0] is None else 1 if me.weapons[1] is None else None
                    if slot is not None:
                        old = me.weapons[slot]
                        if old is not None:
                            self.pickups[self.next_id] = {"id": self.next_id, "kind": old["kind"], "pos": list(me.pos)}
                            self.next_id += 1
                        me.give(slot, p["kind"], self.cfg)
                        me.weapons[slot]["cooldown"] = max(self.cfg["loot"]["equip_s"], me.weapons[slot]["stats"].get("cooldown", 0))
                        del self.pickups[tk]
                        self.events[0].append({"type": "took", "id": tk, "kind": p["kind"], "slot": slot})
                me.act = dict(a)
                me.act.pop("take", None)
                me.act.pop("drop", None)
            for cid in [c for c, v in self.cubes.items() if d2(v, me.pos) < 3.5]:
                del self.cubes[cid]
                me.cubes += 1
                if me.cubes <= self.cfg["cubes"]["stat_cap"]:
                    gain = self.cfg["mech"]["hp"] * self.cfg["cubes"]["hp_per_cube"]
                    me.max_hp += gain
                    me.hp += gain
                self.events[0].append({"type": "pickup", "cubes": me.cubes})
        # bullets
        nb = []
        for b in self.bullets:
            p0 = b[0]
            p1 = [p0[i] + b[1][i] * DT for i in range(3)]
            hit = None
            for o in self.mechs:
                if o.id == b[2] or not o.alive or (o.id != 0 and b[2] != 0):
                    continue
                # closest approach of the segment to the mech axis
                sx, sy = p1[0] - p0[0], p1[1] - p0[1]
                L = sx * sx + sy * sy
                tt = 0 if L == 0 else max(0, min(1, ((o.pos[0] - p0[0]) * sx + (o.pos[1] - p0[1]) * sy) / L))
                cx, cy, cz = p0[0] + sx * tt, p0[1] + sy * tt, p0[2] + (p1[2] - p0[2]) * tt
                if (cx - o.pos[0]) ** 2 + (cy - o.pos[1]) ** 2 < (2.5 + 0.3) ** 2 and o.pos[2] <= cz <= o.pos[2] + 9:
                    hit = o
                    break
            if hit is not None:
                if W.los(p0, (hit.pos[0], hit.pos[1], p0[2])):
                    self.damage(self.mechs[b[2]], hit, b[5], "gun", p0)
                continue
            if not W.los(p0, p1, step=2.0):
                continue
            b[0], b[3] = p1, b[3] + DT
            if b[3] < b[6]:
                nb.append(b)
        self.bullets = nb
        ns = []
        g = self.cfg["weapons"]["mortar"]
        for s in self.shells:
            s[1][2] -= g["gravity"] * DT
            s[0] = [s[0][i] + s[1][i] * DT for i in range(3)]
            s[3] += DT
            k = W.cell(s[0][0], s[0][1])
            if k < 0 or s[0][2] <= W.surf[k] or s[3] > g["ttl"]:
                src = self.mechs[s[2]]
                for o in self.mechs:
                    if not o.alive or (o.id != 0 and s[2] != 0 and o.id != s[2]):
                        continue
                    dd = math.dist(s[0], (o.pos[0], o.pos[1], o.pos[2] + 2))
                    if dd < g["splash_radius"]:
                        f = 1 - (1 - g["splash_edge"]) * dd / g["splash_radius"]
                        amt = g["splash"] * f * (self.cfg["mech"]["self_damage_mult"] if o is src else 1)
                        self.damage(src, o, amt, "mortar", s[0])
                self.sound("explosion", s[0], -1)
                continue
            ns.append(s)
        self.shells = ns

    # ------------------------------------------------------------------ run
    def run(self):
        me = self.mechs[0]
        dur = self.cfg["match"]["duration_s"]
        mf = self.B.mf
        while self.t < dur:
            if self.tick % 2 == 0:
                if me.alive:
                    o = self.obs()
                    a = self.bot.act(o)
                    mf.draw.clear()
                    keep = {k: me.act[k] for k in ("move", "aim", "boost") if k in me.act}
                    keep.update(a)
                    me.act = keep
                self.house_decide()
            self.step()
            self.tick += 1
            self.t = self.tick * DT
            if not me.alive or sum(m.alive for m in self.mechs) <= 1:
                break
        alive_house = sum(m.alive for m in self.mechs[1:])
        if me.alive:
            # survived the clock or the last circle: ranked by HP among survivors
            place = 1 + sum(1 for m in self.mechs[1:] if m.alive and m.hp > me.hp)
        else:
            place = alive_house + 1
        return {"score": 16 - place, "kills": me.kills, "death_t": round(self.t, 1), "alive": me.alive,
                "errors": getattr(self.bot, "errors", 0), "cubes": me.cubes, "weapons": me.kinds()}


def play(args):
    seed, strategy = args
    if W is None:
        setup()
    t0 = time.time()
    r = Match(seed, strategy).run()
    r.update(seed=seed, strategy=strategy, wall=round(time.time() - t0, 1))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", type=int, default=24)
    ap.add_argument("--strategy", default="duelist")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    jobs = [(a.seed * 1000 + i, s) for s in a.strategy.split(",") for i in range(a.matches)]
    with Pool(os.cpu_count(), initializer=setup) as pool:
        res = pool.map(play, jobs, chunksize=1)
    for s in a.strategy.split(","):
        rs = [r for r in res if r["strategy"] == s]
        n = len(rs)
        mean = sum(r["score"] for r in rs) / n
        sd = (sum((r["score"] - mean) ** 2 for r in rs) / max(1, n - 1)) ** 0.5
        print(f"{s:9s} score {mean:5.2f} ±{sd / n ** 0.5:.2f}  kills {sum(r['kills'] for r in rs) / n:4.2f}  "
              f"death t {sum(r['death_t'] for r in rs) / n:5.0f}s  survived {sum(r['alive'] for r in rs)}/{n}  "
              f"errors {sum(r['errors'] for r in rs)}  cubes {sum(r['cubes'] for r in rs) / n:.1f}  wall {sum(r['wall'] for r in rs) / n:.0f}s")


if __name__ == "__main__":
    main()
