"""Offline check on the newest autosave: routes from the excavator area to every wash plant and back,
no recorded roads. Draws dev/plan_two_plants.png."""
import math, os, sys, time
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import numpy as np
from PIL import Image, ImageDraw
import motion as M, planner as P


class ArrayGround:
    def __init__(self, h): self.h = h.reshape(785, 785)

    def patch(self, x0, z0, x1, z1):
        ix0 = max(0, int(math.floor(min(x0, x1) + 392)) - 1); ix1 = min(784, int(math.floor(max(x0, x1) + 392)) + 2)
        iz0 = max(0, int(math.floor(min(z0, z1) + 392)) - 1); iz1 = min(784, int(math.floor(max(z0, z1) + 392)) + 2)
        return ix0, iz0, self.h[iz0:iz1 + 1, ix0:ix1 + 1]


sys.path.insert(0, os.path.join(HERE, "dev"))
import sim_dispatch
save = sim_dispatch.pick_save()
machines, heights, plants = save.machines, save.heights, save.plants
pl = P.Planner(ArrayGround(np.asarray(heights, np.float32)), {"backin_m": 14.0, "keep_right_m": 2.0})
pl.static, pl.water = P.static_obstacles(save.doc, P.map_file_candidates(save.map_package), log=lambda s: None)
beds = sorted(int(k) for k in save.cargo.get("beds", {}))
start_i = int(sys.argv[1]) if len(sys.argv) > 1 else beds[0]
fleet = {start_i}
m0 = machines[start_i]
start = (m0["translation"][0], m0["translation"][2], m0["heading"])
obs = [(m["translation"][0], m["translation"][2], 4.0 if m["id"].startswith("ex") else 3.0, m["id"])
       for i, m in enumerate(machines) if i not in fleet]
for p in plants:
    obs += P.plant_obstacles(p["position"][0], p["position"][2], p["yaw"])
off = (12.49, -0.27, -0.032)
plans, marks = [], [(start, "START #%d" % start_i)]
for n, p in enumerate(plants):
    px, _, pz = p["position"]; yaw = p["yaw"]
    f = (math.sin(yaw), math.cos(yaw)); r = (f[1], -f[0])
    dump = (px + off[0] * f[0] + off[1] * r[0], pz + off[0] * f[1] + off[1] * r[1], M.wrap(yaw + off[2]))
    marks.append((dump, "TIP %d" % (n + 1)))
    for name, a, b, mode in (("to plant %d" % (n + 1), start, dump, "backin"), ("back from %d" % (n + 1), dump, start, "any")):
        t0 = time.time(); q = pl.plan(a, b, mode, obs); dt = time.time() - t0
        if q is None:
            print("%-14s FAILED (%.2fs)" % (name, dt)); continue
        path = M.Path(q.legs, 6.0, 2.2, tail_len=q.tail_len)
        fo = M.Follower(path); t = 0.0
        while not fo.done and t < 1000:
            fo.step(0.01); t += 0.01
        print("%-14s %4.0f m  drive %4.0f s  %s, legs %s  (%.2fs)" % (
            name, q.length(), t, q.note, [("F" if g == 1 else "R", len(w)) for g, w in q.legs], dt))
        plans.append(q)
xs = [w[0] for q in plans for _, l in q.legs for w in l] + [k[0][0] for k in marks]
zs = [w[1] for q in plans for _, l in q.legs for w in l] + [k[0][1] for k in marks]
x0, x1, z0, z1 = min(xs) - 25, max(xs) + 25, min(zs) - 25, max(zs) + 25
S = 5
W, Hh = int((x1 - x0) * S), int((z1 - z0) * S)
h = np.asarray(heights, np.float32).reshape(785, 785)
sub = h[int(z0 + 392):int(z1 + 392), int(x0 + 392):int(x1 + 392)]
img = np.clip(80 + (sub - sub.mean()) * 4, 0, 255)
im = Image.fromarray(np.stack([img] * 3, -1).astype(np.uint8)).resize((W, Hh), Image.NEAREST).transpose(Image.FLIP_TOP_BOTTOM)
dr = ImageDraw.Draw(im)
def tp(x, z): return ((x - x0) * S, Hh - (z - z0) * S)
for (ox, oz, rr, lab) in list(pl.static) + obs:
    if x0 < ox < x1 and z0 < oz < z1:
        a, b = tp(ox, oz); c = (30, 140, 30) if lab == "tree" else (200, 200, 60)
        dr.ellipse([a - rr * S, b - rr * S, a + rr * S, b + rr * S], outline=c)
cols = [(255, 140, 0), (0, 220, 255), (255, 0, 200), (120, 255, 120)]
for q, c in zip(plans, cols):
    for g, w in q.legs:
        dr.line([tp(*v) for v in w], fill=c if g == 1 else (255, 255, 255), width=3)
for (x, z, hh), lab in marks:
    a, b = tp(x, z); dr.ellipse([a - 7, b - 7, a + 7, b + 7], outline=(255, 255, 0), width=2)
    dr.line([a, b, a + 14 * math.sin(hh), b - 14 * math.cos(hh)], fill=(255, 255, 0), width=2)
    dr.text((a + 9, b), lab, fill=(255, 255, 0))
out = os.path.join(HERE, "dev", "plan_two_plants.png"); im.save(out); print(out)
