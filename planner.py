"""Route planner for the haul trucks.

Searches a 2 m grid over the live terrain (slope cost, obstacles, water) with a strong preference
for roads you recorded (one-way in the direction you drove them), then fits a smooth curve and adds
the end maneuvers: leave forward (or reverse out with a turn), and arrive forward or by backing in.

World coordinates: x = ix - 392, z = iz - 392 on the 785 x 785 height grid.
"""
import json, math, os, zipfile

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

import motion as M

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_REL = os.path.join("assets", "maps", "main-map.groundwork-simulator-map")
CELL = 2                     # grid step in 1 m height points
HALF = 392
TRUCK_HALF_W = 1.8           # obstacles are grown by this much
GRADE_BLOCK = 0.45           # climb/descent grade the planner never drives off-road
SIDE_BLOCK = 0.45            # side tilt (grade across the truck) it never drives off-road
NEIGH = [(1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1),
         (2, 1), (1, 2), (-1, 2), (-2, 1), (-2, -1), (-1, -2), (1, -2), (2, -1)]


# ---------------------------------------------------------------- static world (map + save)

def map_file_candidates(map_package=None, game_dir=None):
    out = []
    if map_package:
        out.append(map_package)
    if game_dir:
        out.append(os.path.join(game_dir, MAP_REL))
    out.append(os.path.join(HERE, "..", "..", MAP_REL))      # tool kept inside the game folder
    return out


def static_obstacles(doc, map_files=(), log=print):
    """Standing trees, big rocks, buildings/fences and site equipment as (x, z, radius, label).
    Map trees that you have cleared are listed in the save's `trees` section and are skipped."""
    obs, water = [], []
    proj = None
    for path in map_files:
        try:
            with zipfile.ZipFile(path) as z:
                proj = json.loads(z.read("project.json"))
            break
        except Exception:
            continue
    if proj is None:
        log("Map file not found; routes avoid machines and terrain but not trees or water.")
        proj = {"entities": [], "waters": []}
    saved = {}
    try:
        for t in doc.get("trees")["trees"]:
            bp = t["base_position"]
            saved[(round(bp[0], 1), round(bp[2], 1))] = t
    except Exception:
        pass
    for e in proj.get("entities", []):
        a = e.get("assetId", "")
        x, _, zz = e["position"]
        if "tree" in a:
            t = saved.get((round(x, 1), round(zz, 1)))
            if t is not None and (t.get("cleared") or t.get("lifecycle") != "standing"):
                continue
            obs.append((x, zz, 1.0, "tree"))
        elif a.startswith(("village", "megacity", "watchtower", "caravan", "camping", "abandoned", "drill",
                           "story", "wooden", "steam")):
            obs.append((x, zz, 2.5, a))
        elif a.startswith("fence"):
            obs.append((x, zz, 1.2, "fence"))
    try:
        for r in doc.get("rocks")["rocks"]:
            d = r.get("dimensions") or [0, 0, 0]
            size = max(d[0], d[2])
            if size >= 1.2:
                obs.append((r["position"][0], r["position"][2], size / 2, "rock"))
    except Exception:
        pass
    for key, items, label in (("mobile_trommels", "trommels", "trommel"), ("low_loaders", "trailers", "trailer")):
        try:
            for it in doc.get(key)[items]:
                p = it.get("translation") or (it.get("deck_pose") or {}).get("translation")
                if p:
                    obs.append((p[0], p[2], 4.0, label))
        except Exception:
            pass
    for w in proj.get("waters", []):
        pts = w.get("points")
        if not pts:
            continue
        if w.get("type") == "river":
            water.append(("line", np.array(pts, float)[:, :2], float(w.get("widthM", 6.0)) / 2 + 1.0))
        elif w.get("type") in ("lake", "ocean"):
            water.append(("poly", np.array(pts, float)[:, :2], 0.0))
    return obs, water


def plant_obstacles(x, z, yaw):
    """A wash plant as three discs along its axis (the hopper end is +forward)."""
    f = (math.sin(yaw), math.cos(yaw))
    return [(x + f[0] * a, z + f[1] * a, 4.5, "plant") for a in (-12.0, -5.0, 1.0)]


# ---------------------------------------------------------------- roads

class Road:
    def __init__(self, pts, one_way=True, name=""):
        self.pts = resample(np.asarray(pts, float)[:, :2], 1.0)
        self.one_way, self.name = one_way, name

    def length(self):
        return float(np.hypot(*np.diff(self.pts, axis=0).T).sum()) if len(self.pts) > 1 else 0.0


# ---------------------------------------------------------------- helpers

def _dir(h):
    return (math.sin(h), math.cos(h))


def _right(h):
    return (math.cos(h), -math.sin(h))


def catmull_rom(pts, step=0.5):
    """Centripetal Catmull-Rom through pts (first and last are phantom tangent points)."""
    P = np.asarray(pts, float)
    out = []
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]

        def tj(ti, a, b):
            return ti + max(np.hypot(*(b - a)), 1e-3) ** 0.5
        t0 = 0.0; t1 = tj(t0, p0, p1); t2 = tj(t1, p1, p2); t3 = tj(t2, p2, p3)
        n = max(2, int(np.hypot(*(p2 - p1)) / step))
        for k in range(n):
            t = t1 + (t2 - t1) * k / n
            a1 = (t1 - t) / (t1 - t0) * p0 + (t - t0) / (t1 - t0) * p1
            a2 = (t2 - t) / (t2 - t1) * p1 + (t - t1) / (t2 - t1) * p2
            a3 = (t3 - t) / (t3 - t2) * p2 + (t - t2) / (t3 - t2) * p3
            b1 = (t2 - t) / (t2 - t0) * a1 + (t - t0) / (t2 - t0) * a2
            b2 = (t3 - t) / (t3 - t1) * a2 + (t - t1) / (t3 - t1) * a3
            out.append((t2 - t) / (t2 - t1) * b1 + (t - t1) / (t2 - t1) * b2)
    out.append(P[-2])
    return np.array(out)


def resample(pts, step=0.5):
    P = np.asarray(pts, float)
    if len(P) < 2:
        return P
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(P, axis=0).T))]
    if s[-1] < step:
        return P[[0, -1]]
    ss = np.linspace(0, s[-1], max(2, int(s[-1] / step) + 1))
    return np.c_[np.interp(ss, s, P[:, 0]), np.interp(ss, s, P[:, 1])]


def reverse_arc(x, z, h, side, radius=9.0, angle=1.2, step=0.25):
    """Reverse out while turning: points and the end heading."""
    n = max(4, int(radius * angle / step))
    ds = radius * angle / n
    pts = [(x, z)]
    for _ in range(n):
        x -= math.sin(h) * ds; z -= math.cos(h) * ds
        h += side * ds / radius
        pts.append((x, z))
    return pts, h


# ---------------------------------------------------------------- planner

class Plan:
    """legs = [(gear, Nx2 points)], plus what the planner found out."""

    def __init__(self, legs, cost=0.0, note="", grid=False, tail_len=None):
        self.legs, self.cost, self.note, self.grid = legs, cost, note, grid
        self.tail_len = self.length() if tail_len is None else tail_len   # end maneuver (turn + back in)

    def length(self):
        return sum(float(np.hypot(*np.diff(np.asarray(p), axis=0).T).sum()) for _, p in self.legs if len(p) > 1)


class Planner:
    def __init__(self, ground, cfg):
        self.ground, self.cfg = ground, cfg
        self.static, self.water = [], []
        self.roads = []

    # -------------------------------------------------- cost grid
    def _grid(self, x0, z0, x1, z1, obstacles, free):
        ix0, iz0, arr = self.ground.patch(x0, z0, x1, z1)
        A = arr.astype(np.float64)
        H = A[::CELL, ::CELL]                                # [row=z, col=x]
        gx = np.zeros_like(A); gz = np.zeros_like(A)         # slope over a 4 m baseline (truck size)
        gx[:, 2:-2] = (A[:, 4:] - A[:, :-4]) / 4.0
        gz[2:-2, :] = (A[4:, :] - A[:-4, :]) / 4.0
        GX, GZ = gx[::CELL, ::CELL], gz[::CELL, ::CELL]
        nz, nx = H.shape
        X = ix0 - HALF + np.arange(nx) * CELL
        Z = iz0 - HALF + np.arange(nz) * CELL
        cost = np.ones_like(H)
        blocked = np.zeros(H.shape, bool)
        near = np.zeros_like(H)
        XX, ZZ = np.meshgrid(X, Z)
        for (ox, oz, r, _) in obstacles:
            if ox < X[0] - 12 or ox > X[-1] + 12 or oz < Z[0] - 12 or oz > Z[-1] + 12:
                continue
            a0 = max(0, int((ox - r - 6 - X[0]) / CELL)); a1 = min(nx, int((ox + r + 6 - X[0]) / CELL) + 2)
            b0 = max(0, int((oz - r - 6 - Z[0]) / CELL)); b1 = min(nz, int((oz + r + 6 - Z[0]) / CELL) + 2)
            if a0 >= a1 or b0 >= b1:
                continue
            d = np.hypot(XX[b0:b1, a0:a1] - ox, ZZ[b0:b1, a0:a1] - oz)
            blocked[b0:b1, a0:a1] |= d < r + TRUCK_HALF_W
            near[b0:b1, a0:a1] = np.maximum(near[b0:b1, a0:a1], np.clip(1 - (d - r - TRUCK_HALF_W) / 4.0, 0, 1))
        cost += 1.5 * near
        for kind, pts, rad in self.water:
            bx0, bz0 = pts.min(axis=0) - rad; bx1, bz1 = pts.max(axis=0) + rad
            if bx1 < X[0] or bx0 > X[-1] or bz1 < Z[0] or bz0 > Z[-1]:
                continue
            if kind == "poly":
                blocked |= _inside(XX, ZZ, pts)
            else:
                for p, q in zip(pts[:-1], pts[1:]):
                    blocked |= _seg_dist(XX, ZZ, p, q) < rad
        # roads: preferred, drivable whatever the slope, directional when one-way
        road = np.zeros(H.shape, bool)
        rdx = np.zeros_like(H); rdz = np.zeros_like(H); one = np.zeros(H.shape, bool)
        for rd in self.roads:
            P = resample(rd.pts, 1.0)
            if len(P) < 2:
                continue
            D = np.gradient(P, axis=0); D /= np.maximum(np.hypot(D[:, 0], D[:, 1]), 1e-6)[:, None]
            for (px, pz), (dx_, dz_) in zip(P, D):
                a = int(round((px - X[0]) / CELL)); b = int(round((pz - Z[0]) / CELL))
                if a < -2 or b < -2 or a > nx + 1 or b > nz + 1:
                    continue
                sa, sb = slice(max(0, a - 1), min(nx, a + 2)), slice(max(0, b - 1), min(nz, b + 2))
                road[sb, sa] = True; rdx[sb, sa] = dx_; rdz[sb, sa] = dz_; one[sb, sa] |= rd.one_way
        cost = np.where(road, np.minimum(cost, 1.2), cost)
        for (fx, fz, fr) in free:
            blocked &= np.hypot(XX - fx, ZZ - fz) > fr
        blocked[[0, -1], :] = True; blocked[:, [0, -1]] = True
        return dict(X=X, Z=Z, H=H, GX=GX, GZ=GZ, cost=cost, blocked=blocked, road=road, rdx=rdx, rdz=rdz,
                    one=one, nx=nx, nz=nz)

    @staticmethod
    def _slope_mult(gx, gz, ux, uz, road):
        """Cost multiplier (inf = not drivable) for driving direction (ux, uz) over slope (gx, gz)."""
        along = np.abs(gx * ux + gz * uz)
        side = np.abs(gx * uz - gz * ux)
        m = 1.0 + 30.0 * np.maximum(0.0, along - 0.06) ** 2 + 30.0 * np.maximum(0.0, side - 0.10) ** 2
        bad = ((along > GRADE_BLOCK) | (side > SIDE_BLOCK)) & ~road
        return np.where(road, np.minimum(m, 1.3), np.where(bad, np.inf, m))

    def _graph(self, G):
        nx, nz = G["nx"], G["nz"]
        cost, blocked = G["cost"], G["blocked"]
        idx = np.arange(nx * nz).reshape(nz, nx)
        S, D, W = [], [], []
        for (da, db) in NEIGH:
            a0, a1 = max(0, -da), nx - max(0, da)
            b0, b1 = max(0, -db), nz - max(0, db)
            src = (slice(b0, b1), slice(a0, a1))
            dst = (slice(b0 + db, b1 + db), slice(a0 + da, a1 + da))
            ok = ~blocked[src] & ~blocked[dst]
            if abs(da) + abs(db) >= 2:                 # corners: the cells beside the move must be clear too
                mid1 = (slice(b0, b1), slice(a0 + da, a1 + da))
                mid2 = (slice(b0 + db, b1 + db), slice(a0, a1))
                ok &= ~blocked[mid1] | ~blocked[mid2]
            L = CELL * math.hypot(da, db)
            ux, uz = da / math.hypot(da, db), db / math.hypot(da, db)
            w = L * 0.5 * (cost[src] + cost[dst])
            rd = G["road"][src]
            cosang = G["rdx"][src] * ux + G["rdz"][src] * uz
            mult = np.where(rd & (cosang > 0.5), 0.3, 1.0)
            mult = np.where(rd & G["one"][src] & (cosang < -0.5), 2.2, mult)
            sm = self._slope_mult(0.5 * (G["GX"][src] + G["GX"][dst]), 0.5 * (G["GZ"][src] + G["GZ"][dst]),
                                  ux, uz, G["road"][src] | G["road"][dst])
            ok &= np.isfinite(sm)
            w = w * mult * np.where(np.isfinite(sm), sm, 1.0)
            S.append(idx[src][ok]); D.append(idx[dst][ok]); W.append(w[ok])
        S, D, W = np.concatenate(S), np.concatenate(D), np.concatenate(W)
        return csr_matrix((W, (S, D)), shape=(nx * nz, nx * nz))

    def _cell(self, G, x, z):
        a = int(round((x - G["X"][0]) / CELL)); b = int(round((z - G["Z"][0]) / CELL))
        if 0 <= a < G["nx"] and 0 <= b < G["nz"]:
            return b * G["nx"] + a
        return None

    def _xz(self, G, node):
        b, a = divmod(int(node), G["nx"])
        return float(G["X"][a]), float(G["Z"][b])

    def _blocked_at(self, G, x, z):
        c = self._cell(G, x, z)
        return True if c is None else bool(G["blocked"].flat[c])

    def _seg_cost(self, G, p, q):
        L = math.hypot(q[0] - p[0], q[1] - p[1])
        n = max(2, int(L / 1.0))
        ux, uz = (q[0] - p[0]) / max(L, 1e-6), (q[1] - p[1]) / max(L, 1e-6)
        cells = []
        for k in range(n + 1):
            c = self._cell(G, p[0] + (q[0] - p[0]) * k / n, p[1] + (q[1] - p[1]) * k / n)
            if c is None or G["blocked"].flat[c]:
                return None, 0
            cells.append(c)
        cells = np.array(cells)
        rd = G["road"].flat[cells]
        sm = self._slope_mult(G["GX"].flat[cells], G["GZ"].flat[cells], ux, uz, rd)
        if not np.isfinite(sm).all():
            return None, 0
        return float(np.mean(G["cost"].flat[cells] * sm)), float(rd.mean())

    def _path_cost(self, G, pts):
        """Mean direction-aware cost along grid points (same measure as _seg_cost)."""
        cs = []
        for p, q in zip(pts[:-1], pts[1:]):
            c, _ = self._seg_cost(G, p, q)
            cs.append(c if c is not None else 50.0)
        return float(np.mean(cs)) if cs else 1.0

    def _simplify(self, G, pts):
        """Drop grid points while the straight line is about as cheap (keeps roads as roads)."""
        out, i = [pts[0]], 0
        while i < len(pts) - 1:
            best = i + 1
            for j in range(min(len(pts) - 1, i + 30), i + 1, -1):
                c, r = self._seg_cost(G, pts[i], pts[j])
                if c is None:
                    continue
                rs = [G["road"].flat[self._cell(G, *p)] for p in pts[i:j + 1]]
                if c <= self._path_cost(G, pts[i:j + 1]) * 1.08 + 0.02 and r >= np.mean(rs) - 0.15:
                    best = j
                    break
            out.append(pts[best]); i = best
        return out

    # -------------------------------------------------- end maneuvers
    def _goal_options(self, goal, mode):
        gx, gz, gh = goal
        f, r = _dir(gh), _right(gh)
        opts = []
        if mode in ("backin", "any"):
            c = (gx + f[0] * self.cfg["backin_m"], gz + f[1] * self.cfg["backin_m"])
            R = 11.0
            for side in (1, -1):
                pre = (c[0] + side * R * r[0] - R * f[0], c[1] + side * R * r[1] - R * f[1])
                d_pre = (-side * r[0], -side * r[1])
                tail = [(1, M._hermite(pre, d_pre, c, f)), (-1, M._line(c, (gx, gz)))]
                opts.append((pre, d_pre, tail, 12.0 if mode == "any" else 0.0))
        if mode in ("forward", "any"):
            pre = (gx - f[0] * 12, gz - f[1] * 12)
            opts.append((pre, f, [(1, M._line(pre, (gx, gz)))], 0.0))
        return opts

    def _start_options(self, start, G):
        sx, sz, sh = start
        f = _dir(sh)
        opts = []
        lead = (sx + f[0] * 6, sz + f[1] * 6)
        if not any(self._blocked_at(G, sx + f[0] * d, sz + f[1] * d) for d in (3, 6, 9)):
            opts.append((lead, f, [], 0.0))
        for side in (1, -1):
            pts, h2 = reverse_arc(sx, sz, sh, side)
            if any(self._blocked_at(G, *p) for p in pts[::6]):
                continue
            e = pts[-1]; f2 = _dir(h2)
            opts.append(((e[0] + f2[0] * 5, e[1] + f2[1] * 5), f2, [(-1, pts)], 25.0, e))
        if not opts:                               # boxed in: take the forward lead anyway
            opts.append((lead, f, [], 50.0))
        return opts

    # -------------------------------------------------- main entry
    def plan(self, start, goal, mode="any", obstacles=(), log=None):
        """start/goal = (x, z, heading). mode: 'backin' | 'forward' | 'any'. Returns a Plan or None."""
        sx, sz, sh = start
        gx, gz, gh = goal
        dist = math.hypot(gx - sx, gz - sz)
        if dist < 28.0:
            legs = M.plan(start, goal, None, -1 if mode == "backin" else None, cusp_m=self.cfg["backin_m"])
            return Plan([(g, np.asarray(p)) for g, p in legs], dist, "short move")
        m = 70.0
        x0, x1 = min(sx, gx) - m, max(sx, gx) + m
        z0, z1 = min(sz, gz) - m, max(sz, gz) + m
        obs = list(self.static) + list(obstacles)
        free = [(sx, sz, 7.0), (gx, gz, 7.0)]
        goal_opts = self._goal_options(goal, mode)
        for (pre, _, _, _) in goal_opts:
            free.append((pre[0], pre[1], 3.0))
        G = self._grid(x0, z0, x1, z1, obs, free)
        A = self._graph(G)
        start_opts = self._start_options(start, G)
        goals = [self._cell(G, *o[0]) for o in goal_opts]
        starts = [self._cell(G, *o[0]) for o in start_opts]
        valid_g = [k for k, c in enumerate(goals) if c is not None]
        if not valid_g or all(c is None for c in starts):
            return None
        dist_m, pred = dijkstra(A.T.tocsr(), directed=True, indices=[goals[k] for k in valid_g],
                                return_predecessors=True)
        best = None
        for gi, k in enumerate(valid_g):
            for si, so in enumerate(start_opts):
                c = starts[si]
                if c is None or not np.isfinite(dist_m[gi, c]):
                    continue
                nodes = [c]
                while nodes[-1] != goals[k] and len(nodes) < 100000:
                    nxt = pred[gi, nodes[-1]]
                    if nxt < 0:
                        break
                    nodes.append(nxt)
                if nodes[-1] != goals[k]:
                    continue
                pts = [self._xz(G, n) for n in nodes]
                # arriving the wrong way round at the pre-point would make a loop
                pre, d_pre = goal_opts[k][0], goal_opts[k][1]
                back = pts[max(0, len(pts) - 6)]
                ax, az = pre[0] - back[0], pre[1] - back[1]
                n_ = math.hypot(ax, az)
                turn = 0.0 if n_ < 1e-3 else (ax * d_pre[0] + az * d_pre[1]) / n_
                pen = so[3] + goal_opts[k][3] + (40.0 * max(0.0, 0.2 - turn))
                total = float(dist_m[gi, c]) + pen
                if best is None or total < best[0]:
                    best = (total, si, k, pts)
        if best is None:
            return None
        total, si, k, pts = best
        so, go = start_opts[si], goal_opts[k]
        pts = self._simplify(G, pts)
        legs = list(so[2])
        if len(so) > 4:                    # reversed out first
            s_pt, s_dir = so[4], so[1]
        else:
            s_pt, s_dir = (sx, sz), so[1]
        pre, d_pre, tail = go[0], go[1], go[2]
        chain = [(s_pt[0] - s_dir[0] * 6, s_pt[1] - s_dir[1] * 6), s_pt, so[0]] + pts[1:-1] + \
                [pre, (pre[0] + d_pre[0] * 6, pre[1] + d_pre[1] * 6)]
        chain = _dedupe(chain, 1.5)
        body = catmull_rom(chain, 0.5)
        body = self._keep_right(G, body)
        fwd = np.vstack([body, np.asarray(tail[0][1])[1:]]) if tail[0][0] == 1 else body
        legs.append((1, fwd))
        for g, p in (tail[1:] if tail[0][0] == 1 else tail):
            legs.append((g, np.asarray(p)))
        note = "%s, %s" % ("forward out" if not so[2] else "reverse out", "back in" if len(tail) > 1 else "drive in")
        tail_len = sum(float(np.hypot(*np.diff(np.asarray(p), axis=0).T).sum()) for _, p in tail)
        return Plan(legs, total, note, grid=True, tail_len=tail_len)

    def _keep_right(self, G, P):
        off = float(self.cfg.get("keep_right_m", 2.0))
        if off <= 0 or len(P) < 10:
            return P
        s = np.r_[0, np.cumsum(np.hypot(*np.diff(P, axis=0).T))]
        D = np.gradient(P, axis=0); D /= np.maximum(np.hypot(D[:, 0], D[:, 1]), 1e-6)[:, None]
        R = np.c_[D[:, 1], -D[:, 0]]
        onroad = np.array([bool(G["road"].flat[c]) if (c := self._cell(G, x, z)) is not None else False for x, z in P],
                          float)
        k = 15
        onroad = np.convolve(onroad, np.ones(k) / k, mode="same")
        taper = np.clip(np.minimum(s, s[-1] - s) / 12.0, 0, 1)
        w = off * taper * (1 - np.clip(onroad * 1.5, 0, 1))
        Q = P + R * w[:, None]
        # don't push into a blocked cell
        bad = np.array([self._blocked_at(G, x, z) for x, z in Q[::4]])
        if bad.any():
            return P
        return Q


def _dedupe(pts, d):
    out = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= d:
            out.append(p)
    if out[-1] != pts[-1]:
        out[-1] = pts[-1]
    return out


def _inside(XX, ZZ, poly):
    inside = np.zeros(XX.shape, bool)
    n = len(poly)
    for i in range(n):
        x1, z1 = poly[i]; x2, z2 = poly[(i + 1) % n]
        cond = ((z1 > ZZ) != (z2 > ZZ)) & (XX < (x2 - x1) * (ZZ - z1) / ((z2 - z1) or 1e-9) + x1)
        inside ^= cond
    return inside


def _seg_dist(XX, ZZ, p, q):
    vx, vz = q[0] - p[0], q[1] - p[1]
    L2 = vx * vx + vz * vz or 1e-9
    t = np.clip(((XX - p[0]) * vx + (ZZ - p[1]) * vz) / L2, 0, 1)
    return np.hypot(XX - p[0] - t * vx, ZZ - p[1] - t * vz)
