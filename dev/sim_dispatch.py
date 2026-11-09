"""Run the dispatcher against fake game memory built from a save (no game needed).

python dev/sim_dispatch.py [trucks=3] [loads=6] [seconds to load a truck=90]

Uses the save with the most wash plants. Simulated time at 50 Hz: loaded trucks are sent, plan a route,
back into a hopper, tip (emulated), come back and queue; the next truck moves under the bucket.
Settings/state go to a temp folder, so your real config.json and state.json are not touched.
"""
import math, os, struct, sys, tempfile
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import numpy as np
import locate, motion as M, planner as P
import dispatcher as D

_TMP = tempfile.mkdtemp(prefix="tdsim")
D.DATA, D.STATE, D.CONFIG = _TMP, os.path.join(_TMP, "state.json"), os.path.join(_TMP, "config.json")
D.ROUTES, D.ROADS, D.COMMANDS = os.path.join(_TMP, "routes"), os.path.join(_TMP, "routes", "roads.json"), \
    os.path.join(_TMP, "command.txt")
D.LOGFILE = os.path.join(_TMP, "dispatcher.log")


WATCH_S = 10.0
LAYOUT = {"vec": -0x158, "rev": (-0x68, "<Q"), "cache": -0x60}      # bed record, game 0.1.3.x (Nov 2026)


class FakeProc:
    def __init__(self):
        self.mem = []                       # (base, bytearray)
        self.pid = 0

    def alive(self):
        return True

    def game_dir(self):
        return None

    def close(self):
        pass

    def alloc(self, base, size):
        self.mem.append((base, bytearray(size)))
        return base

    def _find(self, addr, n):
        for base, buf in self.mem:
            if base <= addr and addr + n <= base + len(buf):
                return buf, addr - base
        return None, None

    def read(self, addr, n):
        buf, off = self._find(addr, n)
        return None if buf is None else bytes(buf[off:off + n])

    def write(self, addr, data):
        buf, off = self._find(addr, len(data))
        if buf is None:
            raise RuntimeError("bad write %x" % addr)
        buf[off:off + len(data)] = data
        return True

    def f32(self, addr, n=1):
        b = self.read(addr, 4 * n)
        return struct.unpack("<%df" % n, b) if b else None

    def put_f32(self, addr, *v):
        return self.write(addr, struct.pack("<%df" % len(v), *v))


def pick_save():
    best = None
    for f in locate.slot_files():
        s = locate.read_save(f, log=lambda m: None)
        key = (len(s.plants), len(s.cargo.get("beds", {})))
        if best is None or key > best[0]:
            best = (key, s)
    return best[1]


def build(n_fleet=3):
    save = pick_save()
    machines = save.machines
    pr = FakeProc()
    base = pr.alloc(0x100000, len(machines) * M.STRIDE + 0x1000) + 0x600
    for i, m in enumerate(machines):
        F = base + i * M.STRIDE
        pr.put_f32(F, m.get("fuel_fraction", 0.5), *m["translation"], m["heading"], 0.0, 0.0)
        pr.put_f32(F + M.O_PAYLOAD, m.get("payload_m3", 0.0))
        pr.put_f32(F + locate.OFF_SPAWN, *m["authored_spawn_translation"])
    hb = pr.alloc(0x10000000, save.heights.nbytes)
    pr.write(hb, save.heights.astype(np.float32).tobytes())
    caps = {int(k): b["capacity_m3"] for k, b in save.cargo["beds"].items()}
    beds = {}
    nxt = 0x30000000
    for k, b in save.cargo["beds"].items():
        rec = pr.alloc(nxt, 0x400); anchor = rec + 0x200; nxt += 0x10000
        pr.put_f32(anchor - 4, b["capacity_m3"])
        for v in range(9):
            arr = pr.alloc(nxt, 400); nxt += 0x1000
            pr.write(anchor + LAYOUT["vec"] + v * 0x18, struct.pack("<QQQ", 100, arr, 100))
        beds[int(k)] = [anchor]
    d = D.Dispatcher(log=print)
    d.proc, d.save, d.fuel0 = pr, save, base
    d.ground = M.Ground(pr, hb)
    d.ids = [m["id"] for m in machines]
    d.signature = locate.spawn_signature(pr, base, len(machines))
    d.excavators = [i for i, m in enumerate(machines) if m["id"].startswith("ex-")]
    d.beds = beds
    d.bed_layout = LAYOUT
    d.trucks = []
    big = sorted(beds, key=lambda i: -caps[i])
    fleet = big[:n_fleet]
    for n, i in enumerate(sorted(beds)):
        t = D.Truck(d.machine(i), i, d.ids[i], D.COLORS[n % len(D.COLORS)])
        t.cap = caps[i]
        t.in_fleet = i in fleet
        t.state = "IDLE" if t.in_fleet else "PARKED"
        d.trucks.append(t)
    d.plants = []
    for n, pd in enumerate(save.plants):
        addr = pr.alloc(0x20000000 + n * 0x10000, 0x100) + 0x10
        d.plants.append(D.Plant(n, pd, addr))
    d.planner = P.Planner(d.ground, d.cfg)
    d.planner.static, d.planner.water = P.static_obstacles(save.doc, P.map_file_candidates(save.map_package),
                                                           log=lambda s: None)
    d.load_roads()
    d.planner.roads = d.roads
    d.ready = True
    return d, pr


def load_spot_near_plants(d):
    """A spot 9 m in front of the excavator closest to the first plant."""
    p = d.plants[0]
    e = min(d.excavators, key=lambda i: math.hypot(d.machine(i).pose()[0] - p.x, d.machine(i).pose()[2] - p.z))
    ex, _, ez, eh, _, _ = d.machine(e).pose()
    return (ex + 9 * math.sin(eh), ez + 9 * math.cos(eh), M.wrap(eh + math.pi / 2)), e


def fill(d, t, m3):
    """Load a truck as if the excavator filled it (bed grids + payload)."""
    for anchor in d.beds[t.index]:
        for k, (ptr, ln) in enumerate(d.bed_vecs(anchor)):
            share = {0: m3, 1: 0.846 * m3, 4: 0.6 * m3, 6: 4.0 * m3}.get(k, 0.0)
            d.proc.write(ptr, np.full(ln, share / ln, np.float32).tobytes())
    t.m.put(M.O_PAYLOAD, m3, 0.6 * m3)


def run(d, secs, dt=0.02, t0=[0.0], watch=None, until=None, drain=0.12):
    now = t0[0]
    end = now + secs
    while now < end:
        now += dt
        k = int(round(now / dt))
        if k % 5 == 0:
            d.update_holds(now)
        for t in d.fleet():
            d.tick_truck(t, now, dt)
            if t.state == "PLANNING" and t.future is not None:
                t.future.result()             # let the planner finish within the same simulated tick
        if k % 25 == 0:
            d.watch_plants(now)
            d.auto_send(now)
            for p in d.plants:                # the plants wash their hoppers down
                v = p.hopper(d.proc)
                d.proc.put_f32(p.addr + D.Dispatcher.HOP_BANK, max(0.0, v - drain * 25 * dt))
        if watch and k % int(WATCH_S / dt) == 0:
            watch(now)
        if until and until():
            break
    t0[0] = now


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    loads = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    LOAD_S = float(sys.argv[3]) if len(sys.argv) > 3 else 90.0
    d, pr = build(n)
    print("save %s, fleet %s, plants %d" % (d.save.key, [t.label for t in d.fleet()], len(d.plants)))
    L, e = load_spot_near_plants(d)
    first = d.fleet()[0]
    first.m.put(M.O_POS, L[0], d.ground.patch(L[0] - 1, L[1] - 1, L[0] + 1, L[1] + 1)[2].mean(), L[1], L[2])
    d.learn_load_spot(L[0], L[1], L[2], e)

    def show(now):
        print("  t=%5.0f  " % now + " | ".join("%s %s%s %.0fm3%s" % (
            t.name, t.state, (" " + t.plant.name) if t.plant else "", t.m.payload(),
            (" [" + t.hold_reason + "]") if t.hold_reason and t.state == "RETURNING" and t.follower and
            t.follower.v < 0.05 else "") for t in d.fleet()) + "  hoppers " +
            " / ".join("%.0f" % p.hopper(pr) for p in d.plants))
    sent = 0
    first.state = "AT_LOAD"; first.last = (L[0], L[1])
    while sent < loads:
        at = [t for t in d.fleet() if t.state == "AT_LOAD"]
        if not at:
            run(d, 5, watch=show)
            if d.lines and "error" in d.lines[-1].lower():
                break
            continue
        run(d, LOAD_S, watch=show)              # the excavator needs a while to fill a truck
        fill(d, at[0], at[0].cap)               # auto-send takes it from here
        sent += 1
        run(d, 30, watch=show, until=lambda: not any(t.state == "AT_LOAD" and t.m.payload() > 1 for t in d.fleet()))
    run(d, 240, watch=show)
    for t in d.fleet():
        f = t.follower
        print("DBG", t.label, t.state, "s=%.1f/%.1f v=%.2f" % (f.s, f.p.length, f.v) if f else "-",
              "holds q=%.1f p=%.1f t=%.1f" % (t.queue_hold, t.plant_hold, t.traffic_hold), "blocker", t.blocker,
              "ignore", t.ignore, "reason", t.hold_reason)
    print("\n".join(l for l in d.lines if "to Plant" in l or "tipped" in l or "way" in l or "Problem" in l
                    or "error" in l.lower()))
    tipped = sum(1 for l in d.lines if "tipped" in l)
    print("RESULT: %d loads sent, %d tipped, plants used: %s" % (
        sent, tipped, sorted({l.split(" into ")[1].split(",")[0] for l in d.lines if "tipped" in l})))
