"""Survivor bot for mechfight/1: rotate early, loot safely, fight only from advantage, face threats,
and bank max HP for the zone endgame (protocol.md is the rulebook)."""
import gc
import math
import random
import time

import mechfight as mf
from combat import (aim_at, aim_err, closest_approach, d2, d3, lead, mortar_landing, mortar_pitch,
                    mortar_tof, wrap, yaw_to)
from world import PathSearch, World

NAME = "survivor-farm-drops"
UPGRADE_PRIORITY = [
    "plating", "armor", "nanites", "gun_rate", "gun_damage", "laser_damage", "lightning_efficiency",
    "laser_charge", "lightning_range", "gyros", "gun_mag", "servos", "sensors", "capacitor", "reactor",
    "jets", "gun_velocity", "mortar_reload", "lightning_chain", "mortar_velocity", "mortar_splash",
    "laser_pierce",
]
# v2: either slot may be swapped, so the gun goes once two better weapons are at hand
WEAPON_VALUE = {"lightning": 3.5, "laser": 3.0, "gun": 1.0, "mortar": 0.8}
LOUD = {"gun", "laser", "lightning", "mortar", "laser_charge", "step", "boost", "jet", "reload"}
BUDGET_S = 0.016  # our own per-decision compute cap (the server allows 50 ms)


class Bot:
    name = NAME

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.me_id = hello["slot"]
        self.rng = random.Random(hello.get("seed", 0))
        self.world = None
        try:
            self.world = World(hello["map"]["dir"], self.cfg["mech"])
        except Exception as e:  # play without a map rather than not at all
            mf.log(f"map load failed: {e!r}")
        # the map is ~10^5 long-lived objects: keep the collector from re-scanning them mid-match
        gc.collect()
        gc.freeze()
        gc.set_threshold(20000, 50, 100)
        c = self.cfg
        self.walk = c["mech"]["walk_speed"]
        self.memory = {}          # mech id -> last seen info (our own long memory)
        self.farm_mem = {}        # v4: drone / crate id -> {"pos", "kind"}: crates are static, drones leashed to home
        self.visited = []         # v4: points we've passed, for exploring
        self.threats = []         # (t, bearing, weight)
        self.hit_by = {}          # mech id -> last t it damaged us
        self.last_damage_t = -99.0
        self.last_deal_t = -99.0
        self.search = None
        self.path = None
        self.path_goal = None
        self.path_idx = 0
        self.path_t = -99.0
        self.pos_hist = []
        self.unstick_until = -1.0
        self.stuck_count = 0
        self.strafe = 1
        self.strafe_flip_t = 0.0
        self.hold_spot = None
        self.hold_key = None
        self.scan_base = 0.0
        self.mode = "start"

    # ======================================================================== main
    def act(self, obs):
        t0 = time.perf_counter()
        self.deadline = t0 + BUDGET_S
        me = obs["self"]
        t = obs["t"]
        pos = me["pos"]
        st = me["stats"]
        self.t, self.me, self.pos, self.st = t, me, pos, st
        self.muzzle = (pos[0], pos[1], pos[2] + st.get("muzzle_height", 7))
        self.hp_frac = me["hp"] / max(1.0, me["max_hp"])
        act = {}

        self._ingest(obs)

        if obs.get("offer"):
            act["pick"] = self._pick_upgrade(obs["offer"])

        self._loot_take(obs, act)

        enemies = [m for m in obs["mechs"] if m["kind"] == "mech" and m["id"] != self.me_id]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        target = self._choose_target(enemies)

        # ---- where to go
        goal, urgent, why = self._strategic_goal(obs, enemies)
        move, boost = self._navigate(goal, urgent)
        climb = self.climb

        # ---- combat
        aim = None
        fire = [False, False]
        if target is not None:
            aim, fire = self._engage(target, fire)
            move, boost, why = self._combat_move(target, move, boost, obs, why)
        else:
            # farm crates / NPCs when quiet and healthy
            farm = self._farm_target(obs, npcs)
            if farm is not None:
                aim, fire = self._engage(farm, fire, farm=True)
                why += " farm"
        if aim is None:
            aim = self._idle_aim(move)

        # ---- reflexes: dodge shells and charged lasers, stay out of the zone edge
        dodge = self._dodge(obs, enemies)
        if dodge is not None:
            move, boost = dodge, True
            why += " dodge"

        # ---- reload when safe or empty
        for i, w in enumerate(me["weapons"]):
            if w and w["kind"] == "gun" and w.get("reload", 0) == 0:
                if w.get("ammo", 1) == 0 or (target is None and w.get("ammo", 99) < w["stats"]["mag"] * 0.5):
                    act["reload"] = True

        jump, boost = self._unstick(move, boost)
        if climb and target is None:
            jump = jump or bool(me["grounded"])
            boost = True

        if me["energy"] < 12 and not urgent and dodge is None:
            boost = False
        act["move"] = [round(move[0], 3), round(move[1], 3)]
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)
        act["aim"] = [round(aim[0], 4), round(max(st.get("min_pitch", -0.7), min(st.get("max_pitch", 1.3), aim[1])), 4)]
        act["fire"] = fire
        self.mode = why
        mf.draw.label(f"{why} hp{me['hp']:.0f} c{me['cubes']:.0f}")
        if goal is not None:
            mf.draw.point([goal[0], goal[1], pos[2] + 1], "#40c0ff", 5)
        return act

    # ======================================================================== memory
    def _ingest(self, obs):
        t = self.t
        for m in obs["mechs"]:
            if m["kind"] == "mech":
                self.memory[m["id"]] = {"pos": m["pos"], "vel": m["vel"], "t": t, "hp": m.get("hp", 1400),
                                        "max_hp": m.get("max_hp", 1400), "weapons": m.get("weapons", [])}
        for e in obs["events"]:
            ty = e.get("type")
            if ty == "damaged":
                self.last_damage_t = t
                if e.get("bearing") is not None and e.get("weapon") not in ("zone",):
                    self.threats.append((t, e["bearing"], 3.0))
                if e.get("by") is not None:
                    self.hit_by[e["by"]] = t
            elif ty == "hit":
                self.last_deal_t = t
            elif ty == "kill":
                self.memory.pop(e.get("target"), None)
        for s in obs["sounds"]:
            if s["kind"] in LOUD and s["dist"] in ("near", "mid"):
                self.threats.append((t - s.get("age", 0), s["bearing"], 2.0 if s["dist"] == "near" else 1.0))
        self.threats = [x for x in self.threats if t - x[0] < 4.0][-30:]
        for k in [k for k, v in self.memory.items() if t - v["t"] > 30]:
            del self.memory[k]
        # v4: remember drones and ground-level crates; forget ones we're next to but no longer see
        vis = set()
        for m in obs["mechs"]:
            if m["kind"] == "npc":
                vis.add(m["id"])
                self.farm_mem[m["id"]] = {"pos": m["pos"], "kind": "npc"}
        w = self.world
        for c in obs["crates"]:
            if w is not None and c["pos"][2] - w.ground_h(c["pos"][0], c["pos"][1]) > 3:
                continue
            vis.add(c["id"])
            self.farm_mem[c["id"]] = {"pos": c["pos"], "kind": "crate"}
        for k in [k for k, v in self.farm_mem.items() if k not in vis and d2(v["pos"], self.pos) < 25]:
            del self.farm_mem[k]
        if not self.visited or d2(self.visited[-1], self.pos) > 40:
            self.visited.append((self.pos[0], self.pos[1]))

    # ======================================================================== decisions
    def _pick_upgrade(self, offer):
        ids = [c["id"] for c in offer["choices"]]
        for want in UPGRADE_PRIORITY:
            if want in ids:
                return ids.index(want)
        return 0

    def _held(self):
        return [w["kind"] if w else None for w in self.me["weapons"]]

    def _worst_slot(self):
        """(slot, value) of the weakest weapon we'd give up; empty = 0, a charging laser can't be dropped."""
        vals = []
        for w in self.me["weapons"]:
            if w is None:
                vals.append(0.0)
            elif w.get("charge", 0) > 0:
                vals.append(99.0)
            else:
                vals.append(WEAPON_VALUE.get(w["kind"], 0.0))
        i = min(range(len(vals)), key=lambda k: vals[k])
        return i, vals[i]

    def _loot_take(self, obs, act):
        held = self._held()
        reach = self.cfg["loot"]["pickup_radius"]
        slot, worst = self._worst_slot()
        best, bg = None, 0.0
        for p in obs["pickups"]:
            if p["kind"] in held:
                continue
            if d2(p["pos"], self.pos) > reach - 0.3 or abs(p["pos"][2] - self.pos[2]) > 3.8:
                continue
            gain = WEAPON_VALUE.get(p["kind"], 0) - worst
            if gain > bg:
                best, bg = p, gain
        if best is None:
            return
        act["take"] = best["id"]
        if self.me["weapons"][slot] is not None:
            act["drop"] = slot  # either slot, the gun included

    def _want_pickup(self, p):
        if p["kind"] in self._held():
            return 0
        return max(0.0, WEAPON_VALUE.get(p["kind"], 0) - self._worst_slot()[1])

    def _safe_ring(self, zone):
        """(center, radius) we want to stand in: the next circle with margin."""
        c, r = zone["next_center"], zone["next_radius"]
        if r <= 30:
            return c, max(1.5, r * 0.35)
        return c, r * 0.62

    def _in_zone_at(self, p, zone, margin=0.0):
        return d2(p, zone["center"]) < zone["radius"] - margin

    def _strategic_goal(self, obs, enemies):
        zone = obs["zone"]
        t = self.t
        pos = self.pos
        c, r = self._safe_ring(zone)
        d = d2(pos, c)
        out = max(0.0, d - r)
        time_left = zone["shrink_end"] - t
        travel = out / self.walk * 1.4 + 8.0
        if not self.me["in_zone"]:
            return self._ring_point(c, r, d), True, "zone!"
        if out > 0 and (time_left < travel or zone["shrink_start"] - t < travel):
            return self._ring_point(c, r, d), time_left < travel * 0.7, "rotate"

        # supply drop if uncontested and in reach
        drop = self._drop_goal(obs, enemies)
        if drop is not None:
            return drop, d2(drop, pos) > 80, "drop"

        # loot near us (weapons, cubes, crates) that is inside the safe area
        loot = self._loot_goal(obs, zone)
        if loot is not None:
            return loot, False, "loot"

        # v4: early on (one easy hunter), go farm drones and crates for cubes: +4% HP and damage each
        farm = self._farm_goal(zone)
        if farm is not None:
            return farm, False, "farmrun"

        # v1 + seek: nothing to do here, so go where a mech probably is instead of waiting
        prey = self._seek_goal(zone)
        if prey is not None:
            return prey, False, "seek"

        # otherwise hold a covered spot inside the ring
        if out > 0:
            return self._ring_point(c, r, d), False, "rotate"
        return self._hold_point(c, r), False, "hold"

    def _seek_goal(self, zone):
        """Last seen mech (30 s), else fresh fighting/footstep sounds, else the next circle's center."""
        t, pos = self.t, self.pos
        best, bs = None, 1e18
        for v in self.memory.values():
            age = t - v["t"]
            if age > 30 or not self._in_zone_at(v["pos"], zone, 15):
                continue
            s = d2(v["pos"], pos) + age * 6 + 200 * v["hp"] / max(1.0, v["max_hp"])
            if s < bs:
                best, bs = (v["pos"][0], v["pos"][1]), s
        if best is not None:
            return best
        for tt, bear, wgt in sorted(self.threats, key=lambda x: -x[0]):
            if t - tt < 3:
                rng = 40 if wgt >= 2 else 100
                p = (pos[0] + math.cos(bear) * rng, pos[1] + math.sin(bear) * rng)
                if self._in_zone_at(p, zone, 15):
                    return p
        c = zone["next_center"]
        return (c[0], c[1]) if d2(c, pos) > 25 else None

    def _farm_goal(self, zone):
        if self.t > 200 or self.me["cubes"] >= 10 or self.hp_frac < 0.5:
            return None
        pos = self.pos
        best, bd = None, 1e18
        for v in self.farm_mem.values():
            p = v["pos"]
            if not self._in_zone_at(p, zone, 15):
                continue
            dd = d2(p, pos) + (0 if v["kind"] == "npc" else 30)  # drones give 2 cubes, crates 1
            if dd < bd:
                best, bd = v, dd
        if best is not None:
            p = best["pos"]
            stand = 60.0 if best["kind"] == "npc" else 25.0
            dist = d2(p, pos)
            if dist <= stand + 5:
                return (pos[0], pos[1])  # in range: hold and let _farm_target shoot
            f = (dist - stand) / dist
            return (pos[0] + (p[0] - pos[0]) * f, pos[1] + (p[1] - pos[1]) * f)
        # nothing known: explore the nearest unvisited part of the zone (100 m grid)
        c, r = zone["center"], zone["radius"] - 30
        bestp, bd = None, 1e18
        step = 100.0
        n = int(r // step)
        for i in range(-n, n + 1):
            for j in range(-n, n + 1):
                p = (c[0] + i * step, c[1] + j * step)
                if d2(p, c) > r or any(d2(p, q) < 70 for q in self.visited):
                    continue
                if self.world is not None:
                    k = self.world.cell(*p)
                    if k < 0 or self.world.oob[k]:
                        continue
                dd = d2(p, pos)
                if dd < bd:
                    bestp, bd = p, dd
        return bestp

    def _ring_point(self, c, r, d):
        if d < 1e-3:
            return (c[0], c[1])
        f = max(0.0, (r * 0.85)) / d
        return (c[0] + (self.pos[0] - c[0]) * f, c[1] + (self.pos[1] - c[1]) * f)

    def _hold_point(self, c, r):
        key = (round(c[0]), round(c[1]), round(r))
        if self.hold_key == key and self.hold_spot is not None:
            return self.hold_spot
        w = self.world
        here = (self.pos[0], self.pos[1])
        spot = here if d2(here, c) < r else self._ring_point(c, r, d2(here, c))
        if w is not None and r > 6:
            best, bs = None, -1e9
            k0 = w.cell(*spot)
            if k0 >= 0:
                j0, i0 = divmod(k0, w.nx)
                rad = 12
                for dj in range(-rad, rad + 1, 2):
                    for di in range(-rad, rad + 1, 2):
                        i, j = i0 + di, j0 + dj
                        if not (0 <= i < w.nx and 0 <= j < w.ny):
                            continue
                        k = j * w.nx + i
                        if w.oob[k] or w.surf[k] - w.ground[k] > 1.5:
                            continue
                        p = w.center(k)
                        if d2(p, c) > r:
                            continue
                        s = min(w.cover_score(k), 8) * 2.0 - d2(p, spot) * 0.15
                        if s > bs:
                            best, bs = p, s
            if best is not None:
                spot = best
        self.hold_key, self.hold_spot = key, spot
        return spot

    def _drop_goal(self, obs, enemies):
        for dr in obs.get("drops", []):
            p = dr["pos"]
            dist = d2(p, self.pos)
            # v6: drops are 3 cubes + full HP/energy: go farther and only skip a crowd
            if dist > 350 or not self._in_zone_at(p, obs["zone"], 10):
                continue
            if dr.get("holder") not in (None, self.me_id) and self.hp_frac < 0.6:
                continue
            if sum(1 for e in enemies if d2(e["pos"], p) < 60) >= 2:
                continue
            if dr["lands_in"] > dist / self.walk + 6:
                continue
            if self.hp_frac < 0.3 and enemies:
                continue
            return (p[0], p[1])
        return None

    def _loot_goal(self, obs, zone):
        best, bs = None, -1e9
        pos = self.pos
        w = self.world
        for p in obs["pickups"]:
            v = self._want_pickup(p)
            if v <= 0:
                continue
            if w is not None and p["pos"][2] - w.ground_h(p["pos"][0], p["pos"][1]) > 3:
                continue  # on a roof: not worth the climb
            dist = d2(p["pos"], pos)
            if dist > 120 or not self._in_zone_at(p["pos"], zone, 15):
                continue
            s = v * 40 - dist
            if s > bs:
                best, bs = (p["pos"][0], p["pos"][1]), s
        cubes_now = self.me["cubes"]
        for cb in obs["cubes"]:
            dist = d2(cb["pos"], pos)
            if dist > 90 or not self._in_zone_at(cb["pos"], zone, 10):
                continue
            if w is not None and cb["pos"][2] - w.ground_h(cb["pos"][0], cb["pos"][1]) > 3:
                continue
            s = (60 if cubes_now < 10 else 25) - dist
            if s > bs:
                best, bs = (cb["pos"][0], cb["pos"][1]), s
        return best

    # ======================================================================== targeting
    def _choose_target(self, enemies):
        best, bs = None, -1e9
        for e in enemies:
            dist = d3(e["pos"], self.pos)
            if dist > 260:
                continue
            hpf = e.get("hp", 1400) / max(1.0, e.get("max_hp", 1400))
            s = -dist - hpf * 80
            if self.t - self.hit_by.get(e["id"], -99) < 3:
                s += 60
            if self.world is not None and not self.world.los(self.muzzle, self._center(e)):
                s -= 70
            if s > bs:
                best, bs = e, s
        return best

    def _center(self, e):
        if e["kind"] == "npc":
            return (e["pos"][0], e["pos"][1], e["pos"][2] + self.cfg["npc"].get("height", 3.5) * 0.5)
        sc = 1 + self.cfg["cubes"].get("size_per_cube", 0.05) * e.get("cubes", 0)
        return (e["pos"][0], e["pos"][1], e["pos"][2] + self.cfg["mech"]["height"] * 0.5 * sc)

    def _farm_target(self, obs, npcs):
        # don't spoil out-of-combat regen when hurt
        if self.hp_frac < 0.6 or self.t - self.last_damage_t < 2.5 and not npcs:
            return None
        recent_mech = any(self.t - v["t"] < 6 for v in self.memory.values())
        best, bs = None, -1e9
        for n in npcs:
            dist = d3(n["pos"], self.pos)
            if dist > 120 or (recent_mech and dist > 45):
                continue
            s = 50 - dist
            if s > bs:
                best, bs = n, s
        if recent_mech:
            return best
        for c in obs["crates"]:
            dist = d3(c["pos"], self.pos)
            if dist > 110:
                continue
            s = 30 - dist
            if s > bs:
                best, bs = {"kind": "crate", "id": c["id"], "pos": c["pos"], "vel": [0, 0, 0]}, s
        return best

    def _tcenter(self, e):
        if e.get("kind") == "crate":
            return (e["pos"][0], e["pos"][1], e["pos"][2] + self.cfg["crates"].get("height", 2.4) * 0.5)
        return self._center(e)

    def _engage(self, tgt, fire, farm=False):
        me, st = self.me, self.st
        m = self.muzzle
        c = self._tcenter(tgt)
        v = tgt.get("vel", [0, 0, 0])
        dist = d3(m, c)
        w = self.world
        has_los = w.los(m, c) if w is not None else True
        held = me["weapons"]
        aim = None
        gun_aim = None
        size = (2.5 if tgt.get("kind") == "mech" else 1.6) * 0.8

        for i, wp in enumerate(held):
            if wp and wp["kind"] == "gun":
                p, _ = lead(m, c, v, wp["stats"]["speed"], extra=0.05)
                gun_aim = aim_at(m, p)
        direct = aim_at(m, (c[0] + v[0] * 0.1, c[1] + v[1] * 0.1, c[2]))

        # choose what drives the aim
        laser_i = next((i for i, wp in enumerate(held) if wp and wp["kind"] == "laser"), None)
        light_i = next((i for i, wp in enumerate(held) if wp and wp["kind"] == "lightning"), None)
        mortar_i = next((i for i, wp in enumerate(held) if wp and wp["kind"] == "mortar"), None)
        laser = held[laser_i] if laser_i is not None else None
        if laser is not None and (laser.get("charge", 0) > 0 or laser["cooldown"] < 0.3) and has_los and dist < laser["stats"]["range"] and not farm:
            aim = direct
        elif gun_aim is not None:
            aim = gun_aim
        else:
            aim = direct

        mortar_aim = None
        if mortar_i is not None and not farm and held[mortar_i]["cooldown"] <= 0.1:
            ms = held[mortar_i]["stats"]
            tube = (self.pos[0], self.pos[1], self.pos[2] + self.st.get("height", 9) + 0.5)
            hd = d2(tube, c)
            if 25 < hd < 0.95 * ms["speed"] ** 2 / ms["gravity"] and (not has_los or tgt.get("kind") == "mech"):
                th = mortar_pitch(hd, tgt["pos"][2] - tube[2], ms["speed"], ms["gravity"], high=not has_los)
                if th is not None and th <= st.get("max_pitch", 1.3):
                    tof = mortar_tof(hd, th, ms["speed"])
                    p = (tgt["pos"][0] + v[0] * tof, tgt["pos"][1] + v[1] * tof)
                    hd2 = d2(tube, p)
                    th2 = mortar_pitch(hd2, tgt["pos"][2] - tube[2], ms["speed"], ms["gravity"], high=not has_los)
                    if th2 is not None:
                        mortar_aim = [yaw_to(tube, p), th2]
        if mortar_aim is not None and (not has_los or (gun_aim is None and laser is None)):
            aim = mortar_aim

        actual = me["aim"]
        tol = max(0.012, math.atan2(size, max(dist, 1.0)))
        for i, wp in enumerate(held):
            if not wp:
                continue
            k = wp["kind"]
            s = wp["stats"]
            if k == "gun":
                fire[i] = (has_los and gun_aim is not None and dist < s["range"] and wp.get("ammo", 1) > 0
                           and wp.get("reload", 0) == 0 and aim_err(actual, gun_aim) < tol * 1.3)
            elif k == "lightning":
                cone = math.radians(s.get("cone_deg", 30)) * 0.5
                fire[i] = (has_los and dist < s["range"] - 1.5 and abs(wrap(direct[0] - actual[0])) < cone * 0.8
                           and me["energy"] >= s.get("energy", 3) + (2 if not farm else 25))
            elif k == "laser":
                ch = wp.get("charge", 0)
                full = s.get("charge", 0.75)
                good = has_los and dist < s["range"]
                if farm:
                    fire[i] = False
                elif ch > 0:
                    # keep charging; release when full and on target (or we lost the line)
                    release = ch >= full - 0.02 and aim_err(actual, direct) < tol
                    if not good and ch >= full:
                        release = True
                    fire[i] = not release
                else:
                    fire[i] = good and wp["cooldown"] <= 0.05 and aim_err(actual, direct) < 0.5
            elif k == "mortar":
                fire[i] = mortar_aim is not None and aim is mortar_aim and aim_err(actual, mortar_aim) < 0.02 and d2(c, self.pos) > 18
        if any(fire):
            mf.draw.line(list(m), list(c), "#ff5040")
        return aim, fire

    # ======================================================================== movement
    def _combat_move(self, tgt, move, boost, obs, why):
        held = self._held()
        dist = d2(tgt["pos"], self.pos)
        ehp = tgt.get("hp", 1400) / max(1.0, tgt.get("max_hp", 1400))
        me = self.me
        if "lightning" in held and me["energy"] > 20:
            want = 22.0
        elif "laser" in held:
            want = 90.0
        else:
            want = 55.0
        retreat = (self.hp_frac < 0.35 and ehp > self.hp_frac + 0.1) or (self.hp_frac < 0.55 and ehp > 0.8 and dist > 40)
        zone_urgent = why in ("zone!",)
        if self.t > self.strafe_flip_t:
            self.strafe = -self.strafe
            self.strafe_flip_t = self.t + self.rng.uniform(0.7, 1.6)
        by = yaw_to(self.pos, tgt["pos"])
        perp = (-math.sin(by) * self.strafe, math.cos(by) * self.strafe)
        radial = (math.cos(by), math.sin(by))
        if retreat:
            # keep the zone path but bias away from the enemy, strafing to spoil its aim
            mv = (move[0] - radial[0] * 1.2 + perp[0] * 0.6, move[1] - radial[1] * 1.2 + perp[1] * 0.6)
            return self._clip_move(mv), True, why + " retreat"
        if zone_urgent:
            mv = (move[0] + perp[0] * 0.5, move[1] + perp[1] * 0.5)
            return self._clip_move(mv), True, why + " fight"
        rk = max(-1.0, min(1.0, (dist - want) / 25.0))
        mv = (perp[0] + radial[0] * rk, perp[1] + radial[1] * rk)
        mv = self._clip_move(mv)
        # don't strafe into a wall or out of the zone
        if self.world is not None and not self.world.walk_clear(self.pos, (self.pos[0] + mv[0] * 5, self.pos[1] + mv[1] * 5)):
            self.strafe = -self.strafe
            mv = (-perp[0] + radial[0] * rk, -perp[1] + radial[1] * rk)
            mv = self._clip_move(mv)
        z = obs["zone"]
        nxt = (self.pos[0] + mv[0] * 8, self.pos[1] + mv[1] * 8)
        if d2(nxt, z["center"]) > z["radius"] - 5:
            mv = self._clip_move((mv[0] + (z["center"][0] - self.pos[0]) * 0.05, mv[1] + (z["center"][1] - self.pos[1]) * 0.05))
        return mv, boost or (abs(rk) > 0.8 and me["energy"] > 50), why + " fight"

    def _clip_move(self, mv):
        n = math.hypot(mv[0], mv[1])
        if n < 1e-6:
            return (0.0, 0.0)
        return (mv[0] / n, mv[1] / n)

    def _navigate(self, goal, urgent):
        self.climb = False
        if goal is None:
            return (0.0, 0.0), False
        pos = self.pos
        dist = d2(goal, pos)
        if dist < 2.0:
            return (0.0, 0.0), False
        w = self.world
        target = goal
        if w is not None and dist > 6:
            replan = (self.path_goal is None or d2(self.path_goal, goal) > 8 or self.t - self.path_t > 8)
            if self.path and self.path_idx < len(self.path) and d2(self.path[self.path_idx], pos) > 14:
                replan = True
            if replan and (self.search is None or self.search.done):
                self.search = PathSearch(w, (pos[0], pos[1]), goal)
                self.path_goal = goal
                self.path_t = self.t
            if self.search is not None and not self.search.done:
                self.search.step(self.deadline)
                if self.search.done:
                    self.path = self.search.path
                    self.path_idx = 0
            if self.path:
                P = self.path
                while self.path_idx < len(P) - 1 and d2(P[self.path_idx], pos) < 3.0:
                    self.path_idx += 1
                # look ahead along the path as far as we can walk straight
                best = self.path_idx
                for j in (min(len(P) - 1, self.path_idx + s) for s in (12, 7, 4, 2)):
                    if j > best and w.walk_clear(pos, P[j]):
                        best = j
                        break
                target = P[best]
                # a ledge too tall to walk just ahead on the path: jump and jet over it
                k0 = w.cell(pos[0], pos[1])
                if k0 >= 0:
                    for a in range(self.path_idx, min(len(P), self.path_idx + 3)):
                        ka = w.cell(*P[a])
                        rise = w.surf[ka] - pos[2]
                        if ka >= 0 and d2(P[a], pos) < 5.0 and rise > w.ledge_lim:
                            self.climb = True
                            target = P[a]
                            # don't start a climb the jets can't finish: wait at the foot for energy
                            if self.me["grounded"] and self.me["energy"] < min(95.0, rise * 1.8 + 12):
                                self.climb = False
                                return (0.0, 0.0), False
                            break
                if best == len(P) - 1 and d2(target, goal) > 3:
                    target = goal
                for a in range(self.path_idx, min(len(P) - 1, self.path_idx + 20)):
                    mf.draw.line([P[a][0], P[a][1], pos[2] + 1], [P[a + 1][0], P[a + 1][1], pos[2] + 1], "#40ff80")
        yaw = yaw_to(pos, target)
        speed = 1.0 if dist > 4 else 0.6
        boost = urgent or (dist > 90 and self.me["energy"] > 70)
        return (math.cos(yaw) * speed, math.sin(yaw) * speed), boost

    def _unstick(self, move, boost):
        t = self.t
        pos = self.pos
        self.pos_hist.append((t, pos[0], pos[1]))
        self.pos_hist = [p for p in self.pos_hist if t - p[0] <= 1.2]
        jump = False
        if t < self.unstick_until:
            return bool(self.me["grounded"]), True
        wants = math.hypot(move[0], move[1]) > 0.5
        if wants and len(self.pos_hist) >= 10:
            p0 = self.pos_hist[0]
            if math.hypot(pos[0] - p0[1], pos[1] - p0[2]) < 1.5:
                self.stuck_count += 1
                self.unstick_until = t + 0.8
                self.path_goal = None  # force a replan
                self.pos_hist = []
                return True, True
        return jump, boost

    def _idle_aim(self, move):
        t = self.t
        # look where trouble came from
        if self.threats:
            tt, b, wgt = max(self.threats, key=lambda x: x[0] + x[2])
            if t - tt < 2.5:
                return [b, 0.0]
        # look at the last known position of a recently seen mech
        recent = [v for v in self.memory.values() if t - v["t"] < 8]
        if recent:
            v = min(recent, key=lambda v: d2(v["pos"], self.pos))
            return aim_at(self.muzzle, (v["pos"][0], v["pos"][1], v["pos"][2] + 4.5))
        # sweep: center on travel direction (or the zone center when standing still)
        if math.hypot(move[0], move[1]) > 0.3:
            self.scan_base = math.atan2(move[1], move[0])
        return [wrap(self.scan_base + math.sin(t * 0.9) * 1.7), 0.0]

    def _dodge(self, obs, enemies):
        pos = self.pos
        me_c = (pos[0], pos[1], pos[2] + 4.5)
        w = self.world
        g = self.cfg["weapons"]["mortar"]["gravity"]
        splash = self.cfg["weapons"]["mortar"]["splash_radius"]
        for p in obs["projectiles"]:
            if p["kind"] == "mortar" and w is not None:
                land = mortar_landing(p["pos"], p["vel"], g, w.ground_h)
                if land and d2(land, pos) < splash + 4.5 and land[3] < 3.5:
                    away = (pos[0] - land[0], pos[1] - land[1])
                    if math.hypot(*away) < 0.5:
                        away = (-p["vel"][1], p["vel"][0])
                    return self._clip_move(away)
        for e in enemies:
            if e.get("charging") and d3(e["pos"], pos) < 280:
                want = yaw_to(e["pos"], pos)
                if abs(wrap(e["aim"][0] - want)) < 0.12:
                    by = yaw_to(pos, e["pos"])
                    s = self.strafe
                    return (-math.sin(by) * s, math.cos(by) * s)
        for p in obs["projectiles"]:
            if p["kind"] == "gun":
                dmin, tt = closest_approach(p["pos"], p["vel"], me_c, 0.4)
                if dmin < 3.0 and tt > 0.05:
                    v = p["vel"]
                    n = math.hypot(v[0], v[1]) or 1.0
                    s = self.strafe
                    return (-v[1] / n * s, v[0] / n * s)
        return None


if __name__ == "__main__":
    mf.run(Bot)
