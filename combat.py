"""Aiming math: angle helpers, gun lead, mortar ballistics, projectile threat prediction."""
import math

PI = math.pi


def wrap(a):
    return (a + PI) % (2 * PI) - PI


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def d3(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def yaw_to(a, b):
    return math.atan2(b[1] - a[1], b[0] - a[0])


def aim_at(m, p):
    """[yaw, pitch] from point m to point p."""
    return [math.atan2(p[1] - m[1], p[0] - m[0]), math.atan2(p[2] - m[2], math.hypot(p[0] - m[0], p[1] - m[1]))]


def lead(m, p, v, speed, extra=0.0):
    """Intercept point for a straight projectile of `speed` from m at a target at p moving with v.
    `extra` adds a fixed delay (our decision latency) before the shot leaves."""
    t = d3(m, p) / speed
    for _ in range(4):
        tt = t + extra
        q = (p[0] + v[0] * tt, p[1] + v[1] * tt, p[2] + v[2] * tt * 0.5)
        t = d3(m, q) / speed
    tt = t + extra
    return (p[0] + v[0] * tt, p[1] + v[1] * tt, p[2] + v[2] * tt * 0.5), t


def aim_err(actual, want):
    """Angular error between two [yaw, pitch] aims (radians, approximate)."""
    dy = wrap(want[0] - actual[0]) * math.cos(want[1])
    dp = want[1] - actual[1]
    return math.hypot(dy, dp)


def mortar_pitch(dx, dz, v, g, high=False):
    """Launch pitch to hit a point dx away horizontally and dz up, or None if out of reach."""
    if dx < 1e-3:
        return None
    v2 = v * v
    disc = v2 * v2 - g * (g * dx * dx + 2 * dz * v2)
    if disc < 0:
        return None
    r = math.sqrt(disc)
    th = math.atan((v2 + r) / (g * dx)) if high else math.atan((v2 - r) / (g * dx))
    return th


def mortar_tof(dx, th, v):
    c = v * math.cos(th)
    return dx / c if c > 1e-3 else 99.0


def mortar_landing(p, v, g, ground_fn, max_t=8.0):
    """Where a falling shell (pos p, vel v) meets the ground: (x, y, z, t) or None."""
    t = 0.0
    dt = 0.05
    while t < max_t:
        t += dt
        x = p[0] + v[0] * t
        y = p[1] + v[1] * t
        z = p[2] + v[2] * t - 0.5 * g * t * t
        if z <= ground_fn(x, y) + 0.2:
            return (x, y, z, t)
    return None


def closest_approach(p, v, c, horizon):
    """Min distance and time of a straight-moving point (p, v) to fixed point c within horizon s."""
    rx, ry, rz = c[0] - p[0], c[1] - p[1], c[2] - p[2]
    vv = v[0] * v[0] + v[1] * v[1] + v[2] * v[2]
    if vv < 1e-6:
        return math.sqrt(rx * rx + ry * ry + rz * rz), 0.0
    t = max(0.0, min(horizon, (rx * v[0] + ry * v[1] + rz * v[2]) / vv))
    dx, dy, dz = p[0] + v[0] * t - c[0], p[1] + v[1] * t - c[1], p[2] + v[2] * t - c[2]
    return math.sqrt(dx * dx + dy * dy + dz * dz), t
