"""Moving an idle machine by writing its pose into the game's master machine record.

The game does not run drive physics for a machine nobody is in, so a pose written here
stays put and is drawn as-is. All offsets are relative to the record's fuel field (F).
Conventions (measured): forward = (sin h, cos h); right = (cos h, -sin h);
pitch = atan(forward slope); roll = atan(right slope);
chassis quaternion = yaw(h) * rotX(-pitch) * rotZ(roll).
"""
import math

import numpy as np

from gwmem import GameGone

STRIDE = 0x5D0
GRID = 785
HALF = 392

O_FUEL, O_POS, O_HEAD, O_PITCH, O_ROLL = 0x0, 0x4, 0x10, 0x14, 0x18
O_SPEED, O_SWING, O_TIPPER, O_PAYLOAD, O_PBRAKE = 0x24, 0x4C, 0x54, 0x58, 0x6F
O_WHEEL, O_STEER = 0x28, 0x2C                   # wheel spin angle (rad, wraps), steering input -1..1
O_STEER_ANGLES = -0x20C                         # k440 block: front wheel steer angles (2 x f32)
KAPPA_PER_STEER = 0.108                         # measured on the ah-66: curvature (1/m) at full lock
STEER_ANGLE_PER_STEER = 0.663                   # wheel angle (rad) at full lock
WHEEL_RAD_PER_M = 0.567                         # wheel_angle change per metre driven
O_QUAT, O_CHASSIS_VEL = -0x1A4, -0x1BC          # quat xyzw; vel xyz + angular xyz

WHEELBASE_HALF = 2.5     # m, for pitch sampling
TRACK_HALF = 1.4         # m, for roll sampling


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def heading_of(dx, dz):
    return math.atan2(dx, dz)


def quat(h, pitch, roll):
    def ax(i, a):
        v = [0.0, 0.0, 0.0]; v[i] = math.sin(a / 2); return (v[0], v[1], v[2], math.cos(a / 2))

    def mul(a, b):
        ax_, ay, az, aw = a; bx, by, bz, bw = b
        return (aw * bx + ax_ * bw + ay * bz - az * by, aw * by - ax_ * bz + ay * bw + az * bx,
                aw * bz + ax_ * by - ay * bx + az * bw, aw * bw - ax_ * bx - ay * by - az * bz)
    return mul(ax(1, h), mul(ax(0, -pitch), ax(2, roll)))


class Ground:
    """Bilinear reads of the live terrain height grid."""

    def __init__(self, proc, base):
        self.p, self.base = proc, base

    def patch(self, x0, z0, x1, z1):
        ix0 = max(0, int(math.floor(min(x0, x1) + HALF)) - 1)
        ix1 = min(GRID - 1, int(math.floor(max(x0, x1) + HALF)) + 2)
        iz0 = max(0, int(math.floor(min(z0, z1) + HALF)) - 1)
        iz1 = min(GRID - 1, int(math.floor(max(z0, z1) + HALF)) + 2)
        w = ix1 - ix0 + 1
        rows = [self.p.read(self.base + (iz * GRID + ix0) * 4, w * 4) for iz in range(iz0, iz1 + 1)]
        if any(r is None or len(r) != w * 4 for r in rows):
            raise GameGone("terrain not readable")
        arr = np.frombuffer(b"".join(rows), dtype=np.float32).reshape(-1, w)
        return ix0, iz0, arr

    @staticmethod
    def _sample(pt, x, z):
        ix0, iz0, arr = pt
        fx, fz = x + HALF - ix0, z + HALF - iz0
        i, k = int(math.floor(fx)), int(math.floor(fz))
        i = min(max(i, 0), arr.shape[1] - 2); k = min(max(k, 0), arr.shape[0] - 2)
        tx, tz = fx - i, fz - k
        a = arr[k, i] * (1 - tx) + arr[k, i + 1] * tx
        b = arr[k + 1, i] * (1 - tx) + arr[k + 1, i + 1] * tx
        return float(a * (1 - tz) + b * tz)

    def pose_on_ground(self, xs, zs, hs):
        """Arrays of y, pitch, roll for vehicle poses (one patch read for the whole path)."""
        pad = 4.0
        pt = self.patch(min(xs) - pad, min(zs) - pad, max(xs) + pad, max(zs) + pad)
        ys, ps, rs = [], [], []
        for x, z, h in zip(xs, zs, hs):
            fx, fz = math.sin(h), math.cos(h)
            rx, rz = fz, -fx
            g = self._sample
            ys.append(g(pt, x, z))
            ps.append(math.atan((g(pt, x + fx * WHEELBASE_HALF, z + fz * WHEELBASE_HALF) -
                                 g(pt, x - fx * WHEELBASE_HALF, z - fz * WHEELBASE_HALF)) / (2 * WHEELBASE_HALF)))
            rs.append(math.atan((g(pt, x + rx * TRACK_HALF, z + rz * TRACK_HALF) -
                                 g(pt, x - rx * TRACK_HALF, z - rz * TRACK_HALF)) / (2 * TRACK_HALF)))
        return np.array(ys), np.array(ps), np.array(rs)


class Machine:
    def __init__(self, proc, fuel_addr, index):
        self.p, self.F, self.index = proc, fuel_addr, index

    def f(self, off, n=1):
        v = self.p.f32(self.F + off, n)
        if v is None:
            raise GameGone("machine #%d not readable" % self.index)
        return v if n > 1 else v[0]

    def put(self, off, *vals):
        if not self.p.put_f32(self.F + off, *vals):
            raise GameGone("machine #%d not writable" % self.index)

    def pose(self):
        v = self.p.f32(self.F, 7)
        if v is None:
            raise GameGone("machine #%d not readable" % self.index)
        _, x, y, z, h, pi, ro = v
        return x, y, z, h, pi, ro

    def payload(self):
        return self.f(O_PAYLOAD)

    def write_drive(self, x, y, z, h, pitch, roll, speed, steer, wheel):
        """Pose plus the visual driving state (steering, wheel spin) for a planned path."""
        vals = (x, y, z, h, pitch, roll, speed, steer, wheel)
        if not all(math.isfinite(v) for v in vals) or abs(x) > 1000 or abs(z) > 1000 or abs(y) > 1000:
            raise ValueError("refusing to write a bad pose %r" % (vals,))
        self.put(O_CHASSIS_VEL, 0, 0, 0, 0, 0, 0)
        self.put(O_POS, x, y, z, h, pitch, roll)
        self.put(O_SPEED, speed, wheel, steer)
        self.put(O_STEER_ANGLES, steer * STEER_ANGLE_PER_STEER, steer * STEER_ANGLE_PER_STEER)
        self.put(O_QUAT, *quat(h, pitch, roll))


# ---------------------------------------------------------------- generated paths

def _hermite(p0, d0, p1, d1, step=0.25):
    """Points along a cubic Hermite curve from p0 (direction d0) to p1 (direction d1)."""
    chord = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    k = max(chord, 1.0) * 1.0
    n = max(8, int(chord * 1.6 / step))
    pts = []
    for j in range(n + 1):
        u = j / n
        h00, h10, h01, h11 = 2*u**3 - 3*u**2 + 1, u**3 - 2*u**2 + u, -2*u**3 + 3*u**2, u**3 - u**2
        pts.append((h00 * p0[0] + h10 * k * d0[0] + h01 * p1[0] + h11 * k * d1[0],
                    h00 * p0[1] + h10 * k * d0[1] + h01 * p1[1] + h11 * k * d1[1]))
    return pts


def _line(p0, p1, step=0.25):
    n = max(2, int(math.hypot(p1[0] - p0[0], p1[1] - p0[1]) / step))
    return [(p0[0] + (p1[0] - p0[0]) * j / n, p0[1] + (p1[1] - p0[1]) * j / n) for j in range(n + 1)]


def _dir(h):
    return (math.sin(h), math.cos(h))


def plan(a, b, start_gear=None, end_gear=None, cusp_m=14.0):
    """Legs [(gear, [(x, z), ...])] from pose a=(x, z, h) to pose b, never needing a sharp turn in place."""
    (ax_, az, ah), (bx, bz, bh) = a, b
    v = (bx - ax_, bz - az)
    dist = math.hypot(*v)
    if dist < 0.3:
        return []
    u = (v[0] / dist, v[1] / dist)
    da, db = _dir(ah), _dir(bh)
    dot = lambda p, q: p[0] * q[0] + p[1] * q[1]
    ok = lambda g: g is None
    # one forward or reverse curve
    for gear in (1, -1):
        if (ok(start_gear) or start_gear == gear) and (ok(end_gear) or end_gear == gear):
            ma, mb = (da, db) if gear == 1 else ((-da[0], -da[1]), (-db[0], -db[1]))
            if dot(ma, u) > 0.15 and dot(mb, u) > 0.15:
                return [(gear, _hermite((ax_, az), ma, (bx, bz), mb))]
    # drive forward past the target, then reverse straight in
    if end_gear in (None, -1):
        c = (bx + db[0] * cusp_m, bz + db[1] * cusp_m)
        legs = []
        start = (ax_, az)
        if start_gear == -1 or (start_gear is None and dot(da, (c[0] - ax_, c[1] - az)) < 0):   # back out first
            start = (ax_ - da[0] * cusp_m * 0.6, az - da[1] * cusp_m * 0.6)
            legs.append((-1, _line((ax_, az), start)))
        legs.append((1, _hermite(start, da, c, db)))
        legs.append((-1, _line(c, (bx, bz))))
        return legs
    # reverse out straight, then drive forward to the target
    c = (ax_ - da[0] * cusp_m, az - da[1] * cusp_m)
    return [(-1, _line((ax_, az), c)), (1, _hermite(c, da, (bx, bz), db))]


# ---------------------------------------------------------------- planned paths, followed by distance

def resample(pts, step=0.25):
    P = np.asarray(pts, float)
    if len(P) < 2:
        return P
    d = np.r_[0, np.cumsum(np.hypot(*np.diff(P, axis=0).T))]
    if d[-1] < 1e-6:
        return P[:1]
    ss = np.linspace(0, d[-1], max(2, int(d[-1] / step) + 1))
    return np.c_[np.interp(ss, d, P[:, 0]), np.interp(ss, d, P[:, 1])]


class Path:
    """A drive made of legs (gear, points). Arrays are indexed by travelled distance s."""

    def __init__(self, legs, v_fwd, v_rev, lat_acc=1.3, brake=1.2, ground=None, name="", tail_len=0.0):
        X, Z, Hd, G, S = [], [], [], [], []
        ends = []
        s0 = 0.0
        for gear, pts in [(g, p) for g, p in legs if len(p) > 1]:
            P = resample(pts, 0.25)
            if len(P) < 2:
                continue
            d = np.r_[0, np.cumsum(np.hypot(*np.diff(P, axis=0).T))]
            D = np.gradient(P, axis=0)
            mv = np.arctan2(D[:, 0], D[:, 1])                       # direction of travel
            hd = mv if gear == 1 else mv + math.pi
            if X:                                                   # drop the duplicate joint point
                P, d, hd = P[1:], d[1:], hd[1:]
            X.extend(P[:, 0]); Z.extend(P[:, 1]); Hd.extend(hd); G.extend([gear] * len(P)); S.extend(s0 + d)
            s0 += d[-1]
            ends.append(s0)
        self.x, self.z = np.array(X), np.array(Z)
        self.h = np.unwrap(np.array(Hd)) if Hd else np.array([])
        self.gear, self.s = np.array(G), np.array(S)
        self.leg_end = ends or [0.0]
        self.length = s0
        self.name = name
        self.tail_len = tail_len
        n = len(self.s)
        self.kappa = np.zeros(n); self.vcap = np.zeros(n); self.y = np.zeros(n)
        if n < 2:
            return
        ds = np.maximum(np.diff(self.s), 1e-4)
        k = np.r_[np.diff(self.h) / ds, 0.0]
        k = np.convolve(k, np.ones(9) / 9, mode="same")
        self.kappa = k
        vmax = np.where(self.gear == 1, v_fwd, v_rev)
        v = np.minimum(vmax, np.sqrt(lat_acc / np.maximum(np.abs(k), 1e-4)))
        if ground is not None:
            try:
                ys, ps, _ = ground.pose_on_ground(self.x[::4], self.z[::4], self.h[::4])
                self.y = np.interp(self.s, self.s[::4], ys)
                grade = np.interp(self.s, self.s[::4], np.abs(np.tan(ps)))
                v = np.minimum(v, np.maximum(2.0, vmax * (1.0 - 1.5 * np.maximum(0.0, grade - 0.08))))
            except Exception:
                pass
        v = np.maximum(v, 0.6)
        for e in self.leg_end:                                      # stop at every gear change / the end
            j = int(np.searchsorted(self.s, e - 1e-6))
            v[min(j, n - 1)] = 0.0
        for i in range(n - 2, -1, -1):                              # brake ahead of curves and stops
            v[i] = min(v[i], math.sqrt(v[i + 1] ** 2 + 2 * brake * (self.s[i + 1] - self.s[i])))
        self.vcap = v

    def pose(self, s):
        s = min(max(s, 0.0), self.length)
        i = int(np.searchsorted(self.s, s))
        i = min(max(i, 1), len(self.s) - 1)
        s0, s1 = self.s[i - 1], self.s[i]
        u = 0.0 if s1 <= s0 else (s - s0) / (s1 - s0)
        L = lambda a: float(a[i - 1] + (a[i] - a[i - 1]) * u)
        return L(self.x), L(self.z), wrap(L(self.h)), int(self.gear[i]), L(self.kappa), L(self.vcap)

    def points(self, s_from=0.0, step=2.0):
        """(x, y, z, gear) samples from s_from to the end, for drawing."""
        if len(self.s) < 2:
            return []
        i0 = int(np.searchsorted(self.s, s_from))
        k = max(1, int(step / 0.25))
        idx = list(range(i0, len(self.s), k))
        if not idx or idx[-1] != len(self.s) - 1:
            idx.append(len(self.s) - 1)
        return [(float(self.x[i]), float(self.y[i]), float(self.z[i]), int(self.gear[i])) for i in idx]

    def first_within(self, x, z, r):
        """Distance s where the path first comes within r of (x, z) (None if never)."""
        d = np.hypot(self.x - x, self.z - z)
        j = np.nonzero(d < r)[0]
        return float(self.s[j[0]]) if len(j) else None


class Follower:
    """Drives along a Path with speed control: accelerates, brakes for curves, gear changes and a
    hold distance (queue spot, traffic ahead), pauses briefly at each gear change."""

    def __init__(self, path, accel=0.9, brake=1.5, cusp_pause=0.7):
        self.p, self.accel, self.brake, self.cusp_pause = path, accel, brake, cusp_pause
        self.s, self.v, self.pause = 0.0, 0.0, 0.0
        self.leg = 0
        self.done = path.length <= 0.05

    def step(self, dt, hold=float("inf")):
        p = self.p
        if self.done:
            self.v = 0.0
            return
        if self.pause > 0:
            self.pause -= dt
            self.v = 0.0
            return
        leg_end = p.leg_end[self.leg]
        stop_at = max(self.s, min(leg_end, hold))
        cap = p.pose(self.s + min(2.0, self.v * 0.5))[5]
        vb = math.sqrt(2 * self.brake * max(0.0, stop_at - self.s))
        gap = stop_at - self.s
        if gap <= 0.03:
            self.v = 0.0
            if stop_at < leg_end - 1e-6:          # held (queue / traffic / pause)
                return
            self.s = leg_end
        else:
            vd = max(min(cap if cap > 0.05 else 0.4, vb), 0.3)
            if vd > self.v:
                self.v = min(vd, self.v + self.accel * dt)
            else:
                self.v = max(vd, self.v - 3.0 * dt)
            self.s = min(self.s + self.v * dt, stop_at)
        if self.s >= leg_end - 0.01:
            self.s = leg_end
            if self.leg + 1 < len(p.leg_end):
                self.leg += 1
                self.v = 0.0
                self.pause = self.cusp_pause
            else:
                self.done = True
                self.v = 0.0

    def remaining(self):
        return max(0.0, self.p.length - self.s)


class GroundCache:
    """Small terrain patch around one truck, re-read when the truck moves away or it gets old."""

    def __init__(self, ground, half=14.0, max_age=1.5):
        self.g, self.half, self.max_age = ground, half, max_age
        self.pt, self.cx, self.cz, self.t = None, None, None, -1e9

    def pose(self, x, z, h, now):
        if (self.pt is None or now - self.t > self.max_age or abs(x - self.cx) > self.half - 5 or
                abs(z - self.cz) > self.half - 5):
            self.pt = self.g.patch(x - self.half, z - self.half, x + self.half, z + self.half)
            self.cx, self.cz, self.t = x, z, now
        g = Ground._sample
        pt = self.pt
        fx, fz = math.sin(h), math.cos(h)
        rx, rz = fz, -fx
        y = g(pt, x, z)
        pitch = math.atan((g(pt, x + fx * WHEELBASE_HALF, z + fz * WHEELBASE_HALF) -
                           g(pt, x - fx * WHEELBASE_HALF, z - fz * WHEELBASE_HALF)) / (2 * WHEELBASE_HALF))
        roll = math.atan((g(pt, x + rx * TRACK_HALF, z + rz * TRACK_HALF) -
                          g(pt, x - rx * TRACK_HALF, z - rz * TRACK_HALF)) / (2 * TRACK_HALF))
        return y, pitch, roll
