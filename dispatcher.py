"""Truck Dispatcher engine: fleet, wash plants, route planning, queueing, traffic and emulated tipping.

One loop thread (~250 Hz) owns every game write. Route planning runs in a worker thread.
The UI talks to it with Dispatcher.command(name, *args) and reads Dispatcher.snap (a dict rebuilt
about 20 times a second).
"""
import concurrent.futures as cf
import ctypes, json, math, os, queue, struct, sys, threading, time, traceback

import numpy as np

import gwmem
import locate
import motion as M
import planner as P
from gwmem import GameGone

VERSION = "0.9.0"
HERE = os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """Settings, state and log live next to the tool, or in %APPDATA% if that folder is read-only."""
    base = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else HERE
    try:
        probe = os.path.join(base, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return base
    except OSError:
        alt = os.path.join(os.environ.get("APPDATA", base), "GroundworkTruckDispatcher")
        os.makedirs(alt, exist_ok=True)
        return alt


DATA = data_dir()
ROUTES = os.path.join(DATA, "routes")
ROADS = os.path.join(ROUTES, "roads.json")
CONFIG = os.path.join(DATA, "config.json")
STATE = os.path.join(DATA, "state.json")
COMMANDS = os.path.join(DATA, "command.txt")
LOGFILE = os.path.join(DATA, "dispatcher.log")
MIN_LOAD_M3 = 1.0
INF = float("inf")
_io_lock = threading.Lock()

DEFAULTS = {
    "hotkeys": {"menu": "F6", "send": "F7", "bucket": "F8", "pause": "F9", "record": "F10"},
    "speed_loaded_mps": 5.5,
    "speed_empty_mps": 7.0,
    "speed_reverse_mps": 2.2,
    "accel_mps2": 0.9,
    "brake_mps2": 1.5,
    "lateral_mps2": 1.3,             # slower in tight curves
    "queue_gap_m": 16.0,             # distance between trucks waiting in line
    "backin_m": 14.0,                # straight reverse into the load spot / hopper
    "next_truck_after_m": 18.0,      # next truck goes in once the full one is this far away
    "default_reach_m": 7.0,
    "swing_sign": 1,
    "wait_clearance_m": 15.0,        # waiting trucks stay at least this far from the excavator
    "full_fraction": 0.8,            # a truck this full goes to tip instead of coming to load
    "keep_right_m": 2.0,             # off-road, trucks keep this far right of the centre line
    "hopper_max_m3": 40.0,           # don't tip more than this into a hopper (bank m3)
    "dump_max_s": 270.0,             # give up waiting for a full hopper after this long
    "auto_send": True,               # send the loaded truck by itself when it is full
    "auto_send_fraction": 0.95,
    "follow_gap_m": 8.0,             # minimum gap to a truck/machine ahead
    "dump_offsets": {                # tip spot per truck model, relative to the plant: forward, right, heading
        "ah-66": [12.49, -0.27, -0.032],
        "dt-88": [11.45, -0.30, -0.032],
    },
    "default_dump_offset": [12.0, 0.0, 0.0],
    "overlay": {},                   # filled from overlay.OVERLAY_DEFAULTS
}

DRIVING = ("TO_PLANT", "RETURNING", "REPOSITION")
COLORS = ["#ffb000", "#29c5f6", "#ff5fd2", "#7dff6a", "#ff7a3d", "#b18cff", "#ffe866", "#4dffd2"]
STATE_LABEL = {
    "PARKED": "not in the fleet", "IDLE": "idle", "AT_LOAD": "loading", "PLANNING": "planning route",
    "TO_PLANT": "to plant", "DUMPING": "tipping", "RETURNING": "returning", "REPOSITION": "moving under bucket",
    "MANUAL": "you're driving it",
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if k not in cfg:
                    continue                                    # settings from older versions
                if isinstance(cfg[k], dict):
                    if isinstance(v, dict):
                        if k == "hotkeys":
                            v = {a: str(b) for a, b in v.items() if a in cfg["hotkeys"] and b}
                        cfg[k].update(v)
                elif isinstance(cfg[k], bool):
                    cfg[k] = bool(v)
                elif isinstance(cfg[k], (int, float)) and isinstance(v, (int, float)) and math.isfinite(v):
                    cfg[k] = type(cfg[k])(v)
                elif v is not None and type(v) is type(cfg[k]):
                    cfg[k] = v
        except Exception:
            try:
                os.replace(CONFIG, CONFIG + ".broken")         # keep it for a bug report, start from defaults
            except OSError:
                pass
    return cfg


def write_json(path, obj, indent=2):
    """Write a JSON file atomically (never leaves a half-written file behind)."""
    with _io_lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=indent)
        os.replace(tmp, path)


def save_config(cfg):
    write_json(CONFIG, json.loads(json.dumps(cfg)))


def dist2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def machine_radius(mid):
    return {"ex": 4.0, "dz": 3.5, "wl": 3.5, "dt": 3.0, "ah": 3.0, "dr": 3.0}.get(mid[:2], 2.5)


class Truck:
    def __init__(self, machine, index, model, color):
        self.m, self.index, self.model, self.color = machine, index, model, color
        self.name = "#%d" % index
        self.label = "%s #%d" % (model, index)
        self.in_fleet = False
        self.state = "PARKED"
        self.follower = None          # M.Follower while driving
        self.purpose = None           # "plant" | "load"
        self.plant = None             # Plant it is going to / tipping at
        self.future = None            # pending plan
        self.after_plan = None
        self.wait_s = 0.0             # where on the load path it queues (before the excavator's swing)
        self.queue_hold, self.plant_hold, self.traffic_hold = INF, INF, INF
        self.hold_reason = ""
        self.blocker, self.blocked_since, self.ignore = None, 0.0, set()
        self.last = None              # (x, z) last written
        self.wheel = 0.0
        self.gc = None
        self.dump = None
        self.full_since = None
        self.cap = 30.0
        self.plan_load = None         # load spot the current load path was planned for
        self.seen = {}                # parked machines where they were at planning time (route avoids them)
        self.plant_choice = None      # plant picked by the planner thread before the route is handed over
        self.plant_scores = ""
        self.rerouted = False         # this trip was already re-planned around trucks waiting in line
        self.note = ""

    def xz(self):
        x, _, z, h, _, _ = self.m.pose()
        return x, z, h


class Plant:
    def __init__(self, n, data, addr):
        self.n, self.addr = n, addr
        self.sid = data.get("stable_id", n)
        self.name = "Plant %d" % (n + 1)
        self.x, self.y, self.z = data["position"]
        self.yaw = data["yaw"]
        self.enabled = True
        self.rate = 0.12              # bank m3/s washed (measured live)
        self._last = None
        self.last_inflow = 0.0

    def hopper(self, proc):
        if self.addr is None:
            return 0.0
        v = proc.f32(self.addr + Dispatcher.HOP_BANK, 1)
        return v[0] if v else 0.0

    def pose_for(self, offset):
        fwd, right, relh = offset
        f = (math.sin(self.yaw), math.cos(self.yaw)); r = (f[1], -f[0])
        return (self.x + fwd * f[0] + right * r[0], self.z + fwd * f[1] + right * r[1], M.wrap(self.yaw + relh))


class Dispatcher:
    HOP_BANK, HOP_PAY, HOP_PGV, HOP_VISIBLE = 0x20, 0x28, 0x2C, 0x44
    BED_VECS = 9
    FLOW_LOOSE_M3S = 6.0

    def __init__(self, log=print):
        self._rotate_log()
        self._logfile = open(LOGFILE, "a", encoding="utf-8", buffering=1)
        self.lines = []
        self._ui_log = log
        self.version = VERSION
        self.log("Truck Dispatcher %s started." % VERSION)
        self.cfg = load_config()
        self.q = queue.Queue()
        self.paused = False
        self.trucks, self.plants = [], []
        self.save = None                      # locate.Save of the loaded game
        self.load, self.rel, self.pinned = None, None, False
        self.fleet_saved, self.plants_off = None, []
        self.rec = None
        self.roads = []
        self.ready = False
        self.connecting = False
        self.error = ""
        self.proc = None
        self.ids, self.excavators, self.beds = [], [], {}
        self.bed_layout = None
        self.exc_arm, self.exc_used = {}, {}
        self.signature = None
        self.next_connect = 0.0               # earliest time for an automatic connect attempt
        self.last_save_seen = None
        self._errors = {}
        self.snap = {"ready": False, "trucks": [], "plants": [], "log": [], "save": None}
        self.pool = cf.ThreadPoolExecutor(max_workers=1)
        self._stop = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="dispatcher")

    # ------------------------------------------------------------------ log

    @staticmethod
    def _rotate_log(limit=2_000_000):
        try:
            if os.path.getsize(LOGFILE) > limit:
                os.replace(LOGFILE, LOGFILE[:-4] + ".old.log")
        except OSError:
            pass

    def log(self, msg):
        try:
            self._ui_log(msg)
        except Exception:
            pass
        with _io_lock:
            self.lines = (self.lines + [time.strftime("%H:%M:%S ") + msg])[-200:]
        try:
            self._logfile.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except Exception:
            pass

    def log_error(self, where, e):
        """Unexpected errors: logged once per kind with the traceback (for bug reports), then counted."""
        key = (where, type(e).__name__, str(e)[:80])
        n = self._errors.get(key, 0) + 1
        self._errors[key] = n
        if n == 1:
            self.log("Problem in %s: %s: %s (details in dispatcher.log)" % (where, type(e).__name__, e))
            try:
                self._logfile.write(traceback.format_exc() + "\n")
            except Exception:
                pass
        elif n in (10, 100, 1000):
            self.log("Problem in %s happened %d times: %s" % (where, n, e))

    # ------------------------------------------------------------------ setup

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop = True
        self.thread.join(1.5)
        self.pool.shutdown(wait=False, cancel_futures=True)

    def command(self, name, *args):
        self.q.put((name, args))

    def connect(self):
        self.disconnect(None)
        self.connecting = True
        self.snap = dict(self.snap, connecting=True)
        try:
            self._connect()
            self.error = ""
        except locate.Unsupported as e:
            if str(e) != self.error:
                self.log("Not connected: %s." % e)
            self.error = str(e)
            self.next_connect = time.time() + 300
        except (GameGone, locate.NotReady) as e:
            msg = str(e)
            if msg != self.error:
                self.log("Not connected: %s." % msg)
            self.error = msg
            self.next_connect = time.time() + (20 if isinstance(e, locate.NotReady) else 5)
        except Exception as e:
            self.log_error("connect", e)
            self.error = "%s: %s" % (type(e).__name__, e)
            self.next_connect = time.time() + 60
        finally:
            if not self.ready and self.proc is not None:
                self.proc.close()
                self.proc = None
            self.connecting = False
            self.snap = dict(self.snap, ready=self.ready, connecting=False, error=self.error)

    def disconnect(self, why):
        """Stop every write at once (game closed, or it loaded another save)."""
        was = self.ready
        self.ready = False
        self.rec = None
        for t in self.trucks:
            t.follower, t.future, t.after_plan, t.dump = None, None, None, None
            if t.in_fleet:
                t.state = "IDLE"
        if was:
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.pool = cf.ThreadPoolExecutor(max_workers=1)
        if was and why:
            self.log("Disconnected: %s. Trucks stopped where they are." % why)
            self.error = why
            self.next_connect = time.time() + 5
        self.snap = dict(self.snap, ready=False, error=self.error)

    def want_connect(self):
        """Should the app try to connect now? (game running, and not too soon after a failed try
        unless a new autosave appeared since)."""
        if self.ready or self.connecting:
            return False
        newest = locate.newest_save()
        try:
            mtime = os.path.getmtime(newest) if newest else None
        except OSError:
            mtime = None
        fresh = mtime is not None and mtime != self.last_save_seen
        if time.time() < self.next_connect and not fresh:
            return False
        if not gwmem.find_pid():
            self.error = "game.exe is not running"
            self.snap = dict(self.snap, error=self.error)
            self.next_connect = time.time() + 5
            return False
        self.last_save_seen = mtime
        return True

    def _connect(self):
        self.log("Looking for the game...")
        self.proc = gwmem.Proc()
        save, hbase = locate.find_loaded_save(self.proc, log=self.log)
        self.ground = M.Ground(self.proc, hbase)
        self.fuel0 = locate.find_machine_array(self.proc, save.machines, log=self.log)
        machines = save.machines
        locate.check_layout(self.proc, self.fuel0, machines)
        self.ids = [m["id"] for m in machines]
        self.signature = locate.spawn_signature(self.proc, self.fuel0, len(machines))
        self.excavators = [i for i, m in enumerate(machines) if m["id"].startswith("ex-")]
        self.exc_arm, self.exc_used = {}, {}
        self.beds = locate.find_beds(self.proc, save.cargo, self.fuel0, log=self.log)
        self.bed_layout = locate.bed_layout(self.proc, self.beds, save.cargo, log=self.log) if self.beds else None
        if self.bed_layout is None:
            self.beds = {}
        bed_caps = {int(k): b.get("capacity_m3", 30.0) for k, b in save.cargo.get("beds", {}).items()}
        bed_caps = {i: c for i, c in bed_caps.items() if 0 <= i < len(machines)}
        same_save = self.save is not None and self.save.key == save.key
        self.save = save
        self.load_state()
        old = {t.index: t for t in self.trucks} if same_save else {}
        self.trucks = []
        for n, i in enumerate(sorted(bed_caps)):
            t = Truck(self.machine(i), i, self.ids[i], COLORS[n % len(COLORS)])
            t.cap = bed_caps[i] or 30.0
            if i in old:
                t.in_fleet = old[i].in_fleet
            else:
                t.in_fleet = self.fleet_saved is not None and i in self.fleet_saved
            t.state = "IDLE" if t.in_fleet else "PARKED"
            self.trucks.append(t)
        self.plants = []
        for n, pd in enumerate(save.plants):
            try:
                addr = locate.find_wash_plant(self.proc, pd, log=self.log)
            except Exception as e:
                self.log_error("finding wash plant %d" % (n + 1), e)
                addr = None
            p = Plant(n, pd, addr)
            p.enabled = p.sid not in self.plants_off
            self.plants.append(p)
        self.planner = P.Planner(self.ground, self.cfg)
        maps = P.map_file_candidates(save.map_package, self.proc.game_dir())
        self.planner.static, self.planner.water = P.static_obstacles(save.doc, maps, log=self.log)
        save.doc = None                                   # the parsed save is large; keep only what we use
        self.load_roads()
        self.planner.roads = self.roads
        L = self.load
        for t in self.fleet():
            if L and dist2(t.xz()[:2], L[:2]) < 3.0:
                t.state = "AT_LOAD"
                t.last = t.xz()[:2]
        self.ready = True
        self.next_connect = 0.0
        self.log("Connected to '%s' (slot %d). Fleet: %s. Plants: %s. Other trucks: %s." % (
            save.name, save.slot,
            ", ".join(t.label for t in self.fleet()) or "none yet (tick trucks in the menu)",
            ", ".join("%s%s" % (p.name, "" if p.addr else " (not found in memory)") for p in self.plants) or "none",
            ", ".join(t.label for t in self.trucks if not t.in_fleet) or "none"))
        if self.trucks and not self.beds:
            self.log("Truck beds not found: trucks can drive but not tip until the next autosave and a reconnect.")
        self.save_state()

    def check_game(self):
        """Is the memory we write to still the game's machine list? (closed game / other save loaded)."""
        if not self.proc.alive():
            raise GameGone("the game closed")
        sig = locate.spawn_signature(self.proc, self.fuel0, len(self.ids))
        if sig != self.signature:
            bad = [i for i in range(len(sig)) if sig[i] != self.signature[i]]
            try:
                self._logfile.write("signature change at %s: %s\n" % (bad[:6], [
                    (self.signature[i].hex() if self.signature[i] else None, sig[i].hex() if sig[i] else None)
                    for i in bad[:3]]))
            except Exception:
                pass
            raise GameGone("the game loaded another save (or went back to the menu)")

    # ------------------------------------------------------------------ state (per save)

    def load_state(self):
        """Load spot, fleet and disabled plants for the loaded save (state.json keeps one entry per save)."""
        self.load, self.rel, self.pinned = None, None, False
        self.fleet_saved, self.plants_off = None, []
        try:
            with open(STATE, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
        entry = (st.get("saves") or {}).get(self.save.key) if isinstance(st, dict) else None
        if entry is None and isinstance(st, dict):
            old = st.get("legacy") if "saves" in st else (st if st.get("rel") else None)
            if isinstance(old, dict):
                entry = self._old_state(old)            # state.json from before 0.9 (one save only)
        if not isinstance(entry, dict):
            return
        n = len(self.ids)
        try:
            L = entry.get("load")
            if L and len(L) == 3 and all(math.isfinite(v) for v in L):
                self.load = tuple(float(v) for v in L)
            r = entry.get("rel")
            if isinstance(r, dict) and int(r.get("exc", -1)) in self.excavators:
                self.rel = dict(r, exc=int(r["exc"]))
            self.pinned = bool(entry.get("pinned"))
            fl = entry.get("fleet")
            if isinstance(fl, list):
                self.fleet_saved = [int(i) for i in fl if isinstance(i, (int, float)) and 0 <= int(i) < n]
            self.plants_off = list(entry.get("plants_off") or [])
        except Exception as e:
            self.log_error("reading state.json", e)

    def _old_state(self, st):
        """Use an old single-save state.json only if its excavator still stands where it was."""
        try:
            r = st["rel"]
            e = int(r["exc"])
            if e not in self.excavators:
                return None
            ex, _, ez, _, _, _ = self.machine(e).pose()
            if math.hypot(ex - r["ex"], ez - r["ez"]) > 5.0:
                return None
            self.log("Kept the load spot and fleet from the previous version.")
            return st
        except Exception:
            return None

    def save_state(self):
        if self.save is None:
            return
        try:
            with open(STATE, encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st, dict) and "saves" not in st and st.get("rel"):
                st = {"saves": {}, "legacy": st}         # keep a pre-0.9 file until a save claims it
            if not isinstance(st, dict) or not isinstance(st.get("saves"), dict):
                st = {"saves": {}}
        except Exception:
            st = {"saves": {}}
        st["saves"][self.save.key] = {
            "load": list(self.load) if self.load else None, "rel": self.rel, "pinned": self.pinned,
            "fleet": [t.index for t in self.fleet()] if self.trucks else self.fleet_saved,
            "plants_off": [p.sid for p in self.plants if not p.enabled] if self.plants else self.plants_off,
            "updated": time.strftime("%Y-%m-%d %H:%M")}
        try:
            write_json(STATE, st)
        except OSError as e:
            self.log_error("saving state.json", e)

    def machine(self, i):
        return M.Machine(self.proc, self.fuel0 + i * M.STRIDE, i)

    def fleet(self):
        return [t for t in self.trucks if t.in_fleet]

    def truck(self, index):
        for t in self.trucks:
            if t.index == int(index):
                return t
        return None

    # ------------------------------------------------------------------ roads

    def load_roads(self):
        self.roads = []
        if os.path.exists(ROADS):
            try:
                with open(ROADS) as f:
                    for r in json.load(f):
                        if len(r.get("points", [])) >= 2:
                            self.roads.append(P.Road(r["points"], r.get("one_way", True), r.get("name", "road")))
            except Exception as e:
                self.log("Could not read roads.json: %s" % e)
        if hasattr(self, "planner"):
            self.planner.roads = self.roads
        self.log("Roads: %s" % (", ".join("%s (%.0f m%s)" % (r.name, r.length(), ", one-way" if r.one_way else "")
                                          for r in self.roads) or "none recorded (trucks plan over the terrain)"))

    def save_roads(self):
        os.makedirs(ROUTES, exist_ok=True)
        write_json(ROADS, [{"name": r.name, "one_way": r.one_way, "points": np.round(r.pts, 2).tolist()}
                           for r in self.roads], indent=None)

    def delete_road(self, n):
        n = int(n)
        if 0 <= n < len(self.roads):
            self.log("Deleted road '%s'." % self.roads[n].name)
            self.roads = self.roads[:n] + self.roads[n + 1:]
            self.planner.roads = self.roads
            self.save_roads()

    def rename_road(self, n, name):
        n = int(n)
        if 0 <= n < len(self.roads) and str(name).strip():
            self.roads[n].name = str(name).strip()[:40]
            self.save_roads()

    def clear_load_spot(self):
        self.load, self.rel, self.pinned = None, None, False
        self.save_state()
        self.log("Load spot cleared. Park a truck under the bucket, load it and press %s." % self.cfg["hotkeys"]["send"])

    def toggle_road_oneway(self, n):
        n = int(n)
        if 0 <= n < len(self.roads):
            r = self.roads[n]
            r.one_way = not r.one_way
            self.save_roads()
            self.log("Road '%s' is now %s." % (r.name, "one-way" if r.one_way else "two-way"))

    def record_toggle(self, *_):
        if self.rec is None:
            self.rec = {"t0": time.perf_counter(), "pts": {i: [] for i in range(len(self.ids))}}
            self.log("RECORDING a road: drive it in the direction trucks should use it, then press %s." %
                     self.cfg["hotkeys"]["record"])
            return
        r, self.rec = self.rec, None
        driven = {t.index for t in self.trucks if t.state in DRIVING}
        best, moved = None, 0.0
        for i, pts in r["pts"].items():
            if i in driven or len(pts) < 10:
                continue
            a = np.array(pts)
            d = float(np.hypot(*np.diff(a, axis=0).T).sum())
            if d > moved:
                best, moved = i, d
        if best is None or moved < 15:
            self.log("No machine drove at least 15 m during the recording - discarded.")
            return
        name = "Road %d" % (len(self.roads) + 1)
        self.roads = self.roads + [P.Road(r["pts"][best], True, name)]
        self.planner.roads = self.roads
        self.save_roads()
        self.log("Saved '%s' (%.0f m, driven with %s #%d). Trucks now prefer it, in the direction you drove." %
                 (name, moved, self.ids[best], best))

    def _record_tick(self, now):
        if now - self.rec.get("last", 0) < 0.1:
            return
        self.rec["last"] = now
        for i in range(len(self.ids)):
            x, _, z, _, _, _ = self.machine(i).pose()
            pts = self.rec["pts"][i]
            if not pts or math.hypot(x - pts[-1][0], z - pts[-1][1]) > 0.5:
                pts.append((x, z))

    # ------------------------------------------------------------------ load spot

    def watch_excavators(self, now):
        for i in self.excavators:
            arm = self.proc.read(self.machine(i).F + 0x38, 0x18)
            if self.exc_arm.get(i) is not None and arm != self.exc_arm[i]:
                self.exc_used[i] = now
            self.exc_arm[i] = arm

    def nearest_excavator(self, x, z, within=30.0):
        best = None
        for i in self.excavators:
            ex, _, ez, eh, _, _ = self.machine(i).pose()
            d = math.hypot(ex - x, ez - z)
            if d < within:
                key = (-self.exc_used.get(i, 0.0), d) if d < 20 else (0.0, d)
                if best is None or key < best[0]:
                    best = (key, i)
        return best[1] if best else None

    def learn_load_spot(self, x, z, h, excavator=None):
        self.load = (x, z, h)
        e = excavator if excavator is not None else self.nearest_excavator(x, z)
        if e is None:
            self.rel = None
            return
        mach = self.machine(e)
        ex, _, ez, eh, _, _ = mach.pose()
        swing = mach.f(M.O_SWING)
        bearing = M.heading_of(x - ex, z - ez)
        reach = math.hypot(x - ex, z - ez)
        if abs(M.wrap(swing)) > 0.3:
            ep = abs(M.wrap(eh + swing - bearing)); em = abs(M.wrap(eh - swing - bearing))
            if min(ep, em) < 0.35:
                sign = 1 if ep <= em else -1
                if sign != self.cfg["swing_sign"]:
                    self.cfg["swing_sign"] = sign; save_config(self.cfg)
        self.rel = {"exc": e, "ex": ex, "ez": ez, "eh": eh, "body": M.wrap(bearing - eh),
                    "reach": reach, "relh": M.wrap(h - bearing)}

    def set_load_spot(self, x, z, h, excavator=None):
        self.learn_load_spot(x, z, h, excavator)
        self.pinned = True
        self.save_state()
        self.log("Load spot pinned at (%.1f, %.1f)." % (x, z))

    def load_spot(self):
        r = self.rel
        if r is None or self.load is None:
            return self.load
        ex, _, ez, eh, _, _ = self.machine(r["exc"]).pose()
        if math.hypot(ex - r["ex"], ez - r["ez"]) > 1.0 or abs(M.wrap(eh - r["eh"])) > 0.1:
            b = eh + r["body"]
            self.load = (ex + r["reach"] * math.sin(b), ez + r["reach"] * math.cos(b), M.wrap(b + r["relh"]))
            r.update(ex=ex, ez=ez, eh=eh)
            self._load_moved = True
        now = time.time()
        if getattr(self, "_load_moved", False) and now - getattr(self, "_load_saved", 0.0) > 5.0:
            self._load_moved, self._load_saved = False, now
            self.save_state()
            self.log("Excavator moved: load spot follows it.")
        return self.load

    def exc_xz(self):
        if not self.rel:
            return None
        ex, _, ez, _, _, _ = self.machine(self.rel["exc"]).pose()
        return ex, ez

    def bucket_spot(self):
        """F8: put the load spot where the bucket points now."""
        ref = self.load[:2] if self.load else (self.fleet()[0].xz()[:2] if self.fleet() else (0, 0))
        e = self.rel["exc"] if self.rel else self.nearest_excavator(ref[0], ref[1], within=60)
        if self.exc_used:
            recent = max(self.exc_used, key=self.exc_used.get)
            if time.perf_counter() - self.exc_used[recent] < 120:
                e = recent
        if e is None:
            self.log("F8: no excavator found near the trucks."); return
        mach = self.machine(e)
        ex, _, ez, eh, _, _ = mach.pose()
        b = eh + self.cfg["swing_sign"] * mach.f(M.O_SWING)
        same = self.rel and self.rel["exc"] == e
        reach = self.rel["reach"] if same else self.cfg["default_reach_m"]
        relh = self.rel["relh"] if same else math.pi / 2
        self.load = (ex + reach * math.sin(b), ez + reach * math.cos(b), M.wrap(b + relh))
        self.rel = {"exc": e, "ex": ex, "ez": ez, "eh": eh, "body": M.wrap(b - eh), "reach": reach, "relh": relh}
        self.pinned = True
        self.save_state()
        self.log("Load spot set under the bucket of %s #%d (%.0f m out)." % (self.ids[e], e, reach))
        for t in self.fleet():
            if t.state == "AT_LOAD" or (t.state in ("REPOSITION",) or (t.state == "RETURNING" and t.queue_hold == INF)):
                self.go_load(t, short=True)

    # ------------------------------------------------------------------ planning

    def obstacles(self, exclude=(), parked_fleet=("DUMPING",)):
        """Machines that are not haul trucks in the fleet, the wash plants, and fleet trucks that stand
        still in a state listed in parked_fleet (tipping at a hopper, waiting in line...)."""
        fleet = {t.index: t for t in self.fleet()}
        obs = []
        self._obs_ids = set()
        for i, mid in enumerate(self.ids):
            if i in exclude:
                continue
            t = fleet.get(i)
            if t is not None and self.standing(t) not in parked_fleet:
                continue
            x, _, z, h, _, _ = self.machine(i).pose()
            self._obs_ids.add(i)
            if mid[:2] in ("dt", "ah"):                  # trucks are long: three discs along the body
                obs += [(x + math.sin(h) * a, z + math.cos(h) * a, 2.2, "%s #%d" % (mid, i)) for a in (-3.2, 0, 3.2)]
            else:
                obs.append((x, z, machine_radius(mid), "%s #%d" % (mid, i)))
        for p in self.plants:
            obs += P.plant_obstacles(p.x, p.z, p.yaw)
        return obs

    def standing(self, t):
        """How a fleet truck stands still: AT_LOAD, DUMPING, IDLE, MANUAL, WAITING (in line for the
        excavator), HOPPER (waiting for a hopper) - or None while it is moving/planning."""
        if t.state in ("AT_LOAD", "DUMPING", "IDLE", "MANUAL"):
            return t.state
        f = t.follower
        if f is None or f.v >= 0.05:
            return None
        if t.state in ("RETURNING", "REPOSITION") and t.queue_hold != INF:
            return "WAITING"
        if t.state == "TO_PLANT" and t.plant_hold != INF:
            return "HOPPER"
        return None

    def speeds(self, t, loaded):
        c = self.cfg
        return (c["speed_loaded_mps"] if loaded else c["speed_empty_mps"]), c["speed_reverse_mps"]

    def make_path(self, t, plan, loaded, name):
        vf, vr = self.speeds(t, loaded)
        return M.Path(plan.legs, vf, vr, self.cfg["lateral_mps2"], 1.2, self.ground, name, plan.tail_len)

    def request(self, t, purpose, fn):
        """Plan in the worker thread; the truck waits (state PLANNING) until the route is ready."""
        t.seen = {}                         # machines the route was planned around (see update_traffic)
        for i in getattr(self, "_obs_ids", ()):
            if i != t.index:
                x, _, z, _, _, _ = self.machine(i).pose()
                t.seen[i] = (x, z)
        t.state = "PLANNING"
        t.purpose = purpose
        t.follower = None
        t.ignore = set()                    # "drive through" decisions only last for one trip
        t.future = self.pool.submit(fn)

    def dump_offset(self, t):
        return self.cfg["dump_offsets"].get(t.model, self.cfg["default_dump_offset"])

    def go_plant(self, t, reason="", reroute=False):
        """Pick the plant with the shortest expected wait, plan there (back into the hopper)."""
        plants = [p for p in self.plants if p.enabled and p.addr]
        if not plants:
            self.log("%s: no wash plant enabled/found - can't tip." % t.label)
            t.state = "IDLE"
            return
        x, z, h = t.xz()
        start = (x, z, h)
        obs = self.obstacles(exclude=[t.index], parked_fleet=("DUMPING", "IDLE", "MANUAL", "WAITING", "HOPPER",
                                                             "AT_LOAD"))
        load_m3 = t.m.payload()
        bank = 0.846 * load_m3
        def plant_status():
            others = [o for o in self.fleet() if o is not t and o.purpose == "plant" and
                      o.state in ("TO_PLANT", "DUMPING", "PLANNING")]
            return {p.n: (p.hopper(self.proc), p.rate,
                          sum(0.846 * o.m.payload() for o in others if (o.plant or o.plant_choice) is p),
                          sum(1 for o in others if (o.plant or o.plant_choice) is p and o.state != "PLANNING"))
                    for p in plants}
        t.plant_choice = None
        vmax = self.cfg["speed_loaded_mps"]
        hmax = self.cfg["hopper_max_m3"]
        off = self.dump_offset(t)

        def work():
            best = None
            notes = {}
            status = plant_status()
            for p in plants:
                goal = p.pose_for(off)
                plan = self.planner.plan(start, goal, "backin", obs)
                if plan is None:
                    plan = P.Plan([(g, np.asarray(q)) for g, q in M.plan(start, goal, None, -1, self.cfg["backin_m"])],
                                  note="direct (no clear route found)")
                travel = plan.length() / (0.65 * vmax) + 20
                hop, rate, pending, ahead = status[p.n]
                rate = max(rate, 0.02)
                over = hop + pending + bank - rate * travel - hmax
                wait = max(0.0, over) / rate + ahead * 40.0          # truck waits: hopper full / trucks ahead
                backlog = max(0.0, (hop + pending) / rate - travel)   # plant still busy when it arrives
                score = travel + wait + 0.5 * backlog                 # spread loads over the plants
                notes[p.n] = "%s %.0f s" % (p.name, score)
                if best is None or score < best[0]:
                    best = (score, p, plan, travel, wait)
            t.plant_choice = best[1]
            t.plant_scores = ", ".join(notes[k] for k in sorted(notes))
            best = best + (status,)
            return best

        def done(res):
            score, p, plan, travel, wait, status = res
            t.plant = p
            t.follower = M.Follower(self.make_path(t, plan, True, "to " + p.name),
                                    self.cfg["accel_mps2"], self.cfg["brake_mps2"])
            t.state = "TO_PLANT"
            more = " (hoppers %s; expected time %s)" % (
                ", ".join("%s %.0f/%.0f m3" % (q.name, status[q.n][0], hmax) for q in plants),
                t.plant_scores) if len(plants) > 1 else ""
            self.log("%s: to %s with %.1f m3%s - %.0f m, about %.0f s%s, route %s.%s" % (
                t.label, p.name, load_m3, reason, plan.length(), travel,
                (", ~%.0f s wait for the hopper" % wait) if wait > 5 else "", plan.note, more))
        t.after_plan = done
        self.request(t, "plant", work)
        t.rerouted = reroute

    def go_load(self, t, short=False, reroute=False):
        """Plan to the load spot; the queue logic decides how far the truck may go."""
        L = self.load_spot()
        if L is None:
            t.state = "IDLE"
            t.note = "no load spot yet: park a truck under the bucket and press %s, or press %s" % (
                self.cfg["hotkeys"]["send"], self.cfg["hotkeys"]["bucket"])
            return
        x, z, h = t.xz()
        start = (x, z, h)
        obs = self.obstacles(exclude=[t.index], parked_fleet=("DUMPING", "IDLE", "MANUAL", "HOPPER"))
        exc = self.exc_xz()

        def work():
            plan = self.planner.plan(start, L, "any", obs)
            if plan is None:
                plan = P.Plan([(g, np.asarray(q)) for g, q in M.plan(start, L, None, None, self.cfg["backin_m"])],
                              note="direct (no clear route found)")
            return plan

        def done(plan):
            path = self.make_path(t, plan, False, "to load")
            t.follower = M.Follower(path, self.cfg["accel_mps2"], self.cfg["brake_mps2"])
            t.plan_load = L
            ws = path.first_within(exc[0], exc[1], self.cfg["wait_clearance_m"]) if exc else None
            t.wait_s = max(0.0, (ws if ws is not None else path.length - self.cfg["queue_gap_m"]) - 1.0)
            t.state = "REPOSITION" if short else "RETURNING"
            t.plant = None
        t.after_plan = done
        self.request(t, "load", work)
        t.rerouted = reroute

    def join(self, t, why="joining the cycle"):
        """Bring a fleet truck into the cycle from wherever it is."""
        if not self.ready:
            return
        x, z, h = t.xz()
        L = self.load_spot()
        t.ignore = set()
        if self.is_full(t):
            self.log("%s: %s with %.1f m3 aboard - going to tip first." % (t.label, why, t.m.payload()))
            self.go_plant(t)
        elif L and dist2((x, z), L[:2]) < 3.0:
            t.state = "AT_LOAD"
            t.last = (x, z)
            self.log("%s: %s - it's at the load spot." % (t.label, why))
        else:
            self.log("%s: %s (%.1f m3 aboard)." % (t.label, why, t.m.payload()))
            self.go_load(t)

    # ------------------------------------------------------------------ fleet and plants (menu)

    def set_fleet(self, index, on):
        t = self.truck(index)
        if t is None:
            return
        on = bool(int(on)) if not isinstance(on, bool) else on
        if on == t.in_fleet:
            return
        t.in_fleet = on
        self.save_state()
        if on:
            t.state = "IDLE"
            self.join(t, "added to the fleet")
        else:
            self.stop_truck(t)
            t.state = "PARKED"
            self.log("%s: removed from the fleet, parked where it is." % t.label)

    def set_plant(self, n, on):
        n = int(n)
        if 0 <= n < len(self.plants):
            p = self.plants[n]
            p.enabled = bool(int(on)) if not isinstance(on, bool) else on
            self.save_state()
            self.log("%s %s." % (p.name, "enabled" if p.enabled else "disabled - trucks won't go there"))

    def set_option(self, key, value):
        if key in ("auto_send",):
            value = bool(int(value)) if not isinstance(value, bool) else value
        elif isinstance(DEFAULTS.get(key), float):
            value = float(value)
        self.cfg[key] = value
        save_config(self.cfg)

    def set_overlay(self, key, value):
        self.cfg["overlay"][key] = value
        save_config(self.cfg)

    def teach_tip(self):
        """Use the truck you just backed up to a hopper as the tip spot for that truck model."""
        best = None
        for t in self.trucks:
            if t.state in DRIVING or t.state == "DUMPING":
                continue
            x, z, h = t.xz()
            for p in self.plants:
                d = math.hypot(x - p.x, z - p.z)
                if d < 25 and (best is None or d < best[0]):
                    best = (d, t, p, x, z, h)
        if best is None:
            self.log("Teach tip spot: back a truck up to a wash plant hopper first (within 25 m of the plant).")
            return
        _, t, p, x, z, h = best
        f = (math.sin(p.yaw), math.cos(p.yaw)); r = (f[1], -f[0])
        dx, dz = x - p.x, z - p.z
        off = [round(dx * f[0] + dz * f[1], 2), round(dx * r[0] + dz * r[1], 2), round(M.wrap(h - p.yaw), 3)]
        self.cfg["dump_offsets"][t.model] = off
        save_config(self.cfg)
        self.log("Tip spot for %s trucks learned from %s at %s: %.1f m in front of the plant." % (
            t.model, t.label, p.name, off[0]))

    # ------------------------------------------------------------------ hotkey actions

    def send(self):
        """F7: send the loaded truck at the excavator to tip; bring idle fleet trucks into the cycle."""
        cands = []
        for t in self.fleet():
            if t.state in ("AT_LOAD", "IDLE", "MANUAL"):
                x, z, h = t.xz()
                e = self.nearest_excavator(x, z, within=30)
                if e is not None or t.state == "AT_LOAD":
                    cands.append((t.m.payload(), t, e))
        loaded = [c for c in cands if c[0] >= MIN_LOAD_M3]
        if not loaded:
            if cands:
                self.log("%s: no truck at the excavator has a load (%s). Nothing sent." % (
                    self.cfg["hotkeys"]["send"], ", ".join("%s %.1f m3" % (c[1].label, c[0]) for c in cands)))
            else:
                self.log("%s: no fleet truck at the excavator. Park one under the bucket (or add trucks in the "
                         "menu, %s)." % (self.cfg["hotkeys"]["send"], self.cfg["hotkeys"]["menu"]))
            self._join_idle()
            return
        L0 = self.load

        def rank(c):
            pay_, tr, _ = c
            x_, z_, _ = tr.xz()
            d = dist2((x_, z_), L0[:2]) if L0 else 0.0
            return (tr.state == "AT_LOAD", -round(d / 3.0), pay_)
        pay, t, e = max(loaded, key=rank)
        x, z, h = t.xz()
        if t.state == "AT_LOAD" or self.pinned:
            self.pinned = False
        else:
            self.learn_load_spot(x, z, h, e)
            self.log("Load spot learned from where you loaded %s." % t.label)
        self.save_state()
        self.go_plant(t)
        self._join_idle(skip=t)

    def _join_idle(self, skip=None):
        for o in self.fleet():
            if o is not skip and o.state in ("IDLE", "MANUAL"):
                self.join(o)

    def send_truck(self, index):
        t = self.truck(index)
        if t and t.in_fleet:
            if t.m.payload() < MIN_LOAD_M3:
                self.log("%s is empty - not sent." % t.label); return
            self.go_plant(t)

    def rejoin(self, index):
        t = self.truck(index)
        if t:
            if not t.in_fleet:
                self.set_fleet(index, True)
            else:
                self.join(t)

    def redo_dump(self, index):
        t = self.truck(index)
        if t:
            self.go_plant(t, " (sent round again)")

    def toggle_pause(self):
        self.paused = not self.paused
        self.log("PAUSED - trucks brake and wait (%s to resume)." % self.cfg["hotkeys"]["pause"]
                 if self.paused else "Resumed.")

    def stop_truck(self, t):
        t.follower, t.future, t.after_plan, t.dump, t.last = None, None, None, None, None
        t.plant = None
        try:
            t.m.put(M.O_SPEED, 0.0)
        except GameGone:
            pass

    def stop_all(self):
        for t in self.fleet():
            self.stop_truck(t)
            t.state = "IDLE"
        self.paused = False
        self.log("Stopped. All trucks idle where they are (%s brings them back)." % self.cfg["hotkeys"]["send"])

    # ------------------------------------------------------------------ queue, plant slots, traffic

    def update_holds(self, now):
        fleet = self.fleet()
        L = self.load_spot()
        # --- load spot queue
        at_load = [t for t in fleet if t.state == "AT_LOAD" or (t.state == "REPOSITION")]
        leaving = [t for t in fleet if L and t.state in ("TO_PLANT", "PLANNING") and t.purpose == "plant" and
                   dist2(t.xz()[:2], L[:2]) < self.cfg["next_truck_after_m"]]
        busy = bool(at_load or leaving)
        self._leaving = leaving
        incoming = [t for t in fleet if t.state == "RETURNING" and t.follower]
        incoming.sort(key=lambda t: t.follower.remaining())
        for i, t in enumerate(incoming):
            slot = i + (1 if busy else 0)
            if slot == 0:
                if t.queue_hold != INF and t.follower.v < 0.05 and t.plan_load and L and \
                        dist2(t.plan_load[:2], L[:2]) > 1.5:
                    self.log("%s: load spot moved - re-planning." % t.label)
                    self.go_load(t)
                    continue
                t.queue_hold = INF
                t.hold_reason = ""
            else:
                t.queue_hold = max(0.0, t.wait_s - (slot - 1) * self.cfg["queue_gap_m"])
                t.hold_reason = "waiting in line (%s)" % ("next" if slot == 1 else "#%d" % slot)
        for t in fleet:
            if t.state != "RETURNING":
                t.queue_hold = INF
        # --- one truck at a time backs into each hopper
        for p in self.plants:
            users = [t for t in fleet if t.plant is p and t.state in ("TO_PLANT", "DUMPING")]
            occ = [t for t in users if t.state == "DUMPING" or
                   (t.follower and t.follower.s >= t.follower.p.length - t.follower.p.tail_len - 1.0)]
            rest = sorted([t for t in users if t not in occ and t.follower], key=lambda t: t.follower.remaining())
            for i, t in enumerate(rest):
                if occ or i > 0:
                    tl = t.follower.p
                    t.plant_hold = max(0.0, tl.length - tl.tail_len - 8.0 - (i if occ else i - 1) * self.cfg["queue_gap_m"])
                else:
                    t.plant_hold = INF
            for t in users:
                if t not in rest:
                    t.plant_hold = INF
        for t in fleet:
            if t.state != "TO_PLANT":
                t.plant_hold = INF
        self.update_traffic(now)

    def update_traffic(self, now):
        """Stop short of any machine on the path ahead (other trucks, you, parked machines)."""
        poses = []
        for i, mid in enumerate(self.ids):
            x, _, z, h, _, _ = self.machine(i).pose()
            poses.append((i, mid, x, z, h))
        moving = [t for t in self.fleet() if t.state in DRIVING and t.follower and not t.follower.done]
        for t in self.fleet():
            if t not in moving:
                t.traffic_hold, t.blocker = INF, None
        for t in moving:
            f, p = t.follower, t.follower.p
            look = max(14.0, f.v * f.v / (2 * self.cfg["brake_mps2"]) + 14.0)
            i0 = int(np.searchsorted(p.s, f.s + 1.5)); i1 = int(np.searchsorted(p.s, f.s + look))
            if i1 <= i0:
                t.traffic_hold, t.blocker = INF, None
                continue
            px, pz, ps = p.x[i0:i1:4], p.z[i0:i1:4], p.s[i0:i1:4]
            hit, who = INF, None
            end_x, end_z = p.x[-1], p.z[-1]
            for (i, mid, x, z, h) in poses:
                if i == t.index or i in t.ignore:
                    continue
                o = self.truck(i)
                if i in t.seen and math.hypot(x - t.seen[i][0], z - t.seen[i][1]) < 1.0:
                    continue                            # parked where it was when we planned around it
                if math.hypot(x - f.p.x[min(len(p.x) - 1, i0)], z - f.p.z[min(len(p.z) - 1, i0)]) > look + 10:
                    continue
                if mid.startswith("ex-") and self.rel and i == self.rel["exc"]:
                    rr = 3.2                            # the loading excavator: body only
                else:
                    rr = 1.9
                if mid[:2] in ("dt", "ah"):
                    circles = [(x + math.sin(h) * a, z + math.cos(h) * a) for a in (-3.0, 0.0, 3.0)]
                else:
                    circles = [(x, z)]
                # a machine parked on the very spot we're going to (e.g. tip spot) doesn't stop us
                if math.hypot(x - end_x, z - end_z) < 3.0 and (self.truck(i) is None or not self.truck(i).in_fleet):
                    continue
                for (cx, cz) in circles:
                    d = np.hypot(px - cx, pz - cz)
                    j = np.nonzero(d < rr + 1.9)[0]
                    if len(j) and ps[j[0]] < hit:
                        hit, who = float(ps[j[0]]), i
                        break
            if who is not None:
                t.traffic_hold = max(f.s, hit - 4.0 - self.cfg["follow_gap_m"] * 0.5)
                if t.blocker != who:
                    t.blocker, t.blocked_since = who, now
            else:
                t.traffic_hold, t.blocker = INF, None
        # break deadlocks: trucks waiting on each other in a circle (2, 3 or more) -> the loaded one
        # (then the lower number) goes and drives through the one in front of it
        by_index = {t.index: t for t in moving}
        leaving = [o for o in getattr(self, "_leaving", []) if o in moving]

        def waits_for(a):
            if a.blocker is not None:
                return a.blocker
            if leaving and a.state == "RETURNING" and a.queue_hold != INF and a.follower.v < 0.05:
                return leaving[0].index                 # waiting in line until the loaded truck has left
            return None
        for t in moving:
            chain, cur = [], t
            while cur is not None and waits_for(cur) is not None and cur not in chain:
                chain.append(cur)
                cur = by_index.get(waits_for(cur))
            if cur is not None and cur in chain:
                cycle = chain[chain.index(cur):]
                first = min(cycle, key=lambda a: (a.purpose != "plant", a.blocker is None, a.index))
                if first.blocker is not None:
                    first.ignore.add(first.blocker)
                first.traffic_hold, first.blocker = INF, None
        for t in moving:
            if t.blocker is not None and t.follower.v < 0.05:
                o = self.truck(t.blocker)
                busy = o is not None and o.in_fleet and o.state not in ("IDLE", "MANUAL")
                in_line = lambda a: a.state == "RETURNING" and a.queue_hold != INF
                stand = self.standing(o) if o is not None and o.in_fleet else None
                long_wait = stand in ("WAITING", "HOPPER", "DUMPING", "IDLE", "MANUAL", "AT_LOAD")
                if t.state == "RETURNING" and stand in ("WAITING", "AT_LOAD"):
                    long_wait = False                   # we are in the same line: wait our turn
                if long_wait and not t.rerouted and now - t.blocked_since > 3.0:
                    t.rerouted = True                   # a truck stands on our way for a while: go round it
                    self.log("%s: %s stands on the way (%s) - finding a way round." % (
                        t.label, o.label, {"WAITING": "waiting in line", "HOPPER": "waiting for a hopper"}.get(
                            stand, STATE_LABEL.get(o.state, o.state))))
                    if t.purpose == "plant":
                        self.go_plant(t, reroute=True)
                    else:
                        self.go_load(t, short=t.state == "REPOSITION", reroute=True)
                    continue
                if busy and ((t.state == "RETURNING" and (in_line(o) or o.state == "AT_LOAD")) or
                             (o.state in DRIVING and o.follower is not None and o.follower.v >= 0.05)):
                    t.blocked_since = now               # waiting in line / behind a moving truck: normal
                    continue
                if busy and o.state in DRIVING:
                    limit = 45.0
                else:
                    limit = 120.0 if busy else (20.0 if (o is not None and o.in_fleet) else 15.0)
                if now - t.blocked_since > limit:
                    name = o.label if o else "%s #%d" % (self.ids[t.blocker], t.blocker)
                    why = ""
                    if o is not None and o.in_fleet:
                        why = " (it is %s%s%s)" % (
                            STATE_LABEL.get(o.state, o.state),
                            (", " + o.hold_reason) if o.hold_reason and o.queue_hold != INF else "",
                            (", behind %s" % self.ids[o.blocker] + " #%d" % o.blocker) if o.blocker is not None else "")
                    self.log("%s: %s has been in the way for %.0f s%s - driving through it." % (
                        t.label, name, limit, why))
                    if os.environ.get("TD_DEBUG"):
                        for a_ in self.fleet():
                            f_ = a_.follower
                            self.log("   DBG %s %s purpose=%s v=%s s=%s/%s q=%s p=%s tr=%s pause=%s leg=%s ends=%s blk=%s" % (
                                a_.label, a_.state, a_.purpose, f_ and round(f_.v, 2), f_ and round(f_.s, 2),
                                f_ and round(f_.p.length), a_.queue_hold, a_.plant_hold, a_.traffic_hold,
                                f_ and round(f_.pause, 2), f_ and f_.leg, f_ and [round(e, 1) for e in f_.p.leg_end],
                                a_.blocker))
                    t.ignore.add(t.blocker)
                    t.traffic_hold, t.blocker = INF, None

    # ------------------------------------------------------------------ loop

    def _run(self):
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
        self.connect()
        last = time.perf_counter()
        t_slow = t_hold = t_snap = t_check = 0.0
        while not self._stop:
            now = time.perf_counter()
            dt, last = min(now - last, 0.05), now
            self._commands()
            if self.ready:
                try:
                    if now - t_check > 0.25:
                        t_check = now
                        self.check_game()
                    if now - t_slow > 0.5:
                        t_slow = now
                        self._safe("excavators", self.watch_excavators, now)
                        self._safe("command.txt", self.poll_command_file)
                        self._safe("plants", self.watch_plants, now)
                        self._safe("auto send", self.auto_send, now)
                    if now - t_hold > 0.1:
                        t_hold = now
                        self._safe("queue/traffic", self.update_holds, now)
                    if self.rec is not None:
                        self._safe("road recording", self._record_tick, now)
                    for t in self.fleet():
                        try:
                            self.tick_truck(t, now, dt)
                        except GameGone:
                            raise
                        except Exception as e:
                            self.log_error("driving " + t.label, e)
                            self.stop_truck(t)
                            t.state = "IDLE"
                            t.note = "stopped after an error - press Join to try again"
                    if now - t_snap > 0.05:
                        t_snap = now
                        self._safe("status", self._snapshot_now)
                except GameGone as e:
                    self.disconnect(str(e))
                except Exception as e:
                    self.log_error("main loop", e)
            else:
                if now - t_snap > 0.25:
                    t_snap = now
                    self.snap = dict(self.snap, ready=False, connecting=self.connecting, error=self.error,
                                     log=self.lines[-8:])
                if now - t_check > 2.0:
                    t_check = now
                    try:
                        if self.want_connect():
                            self.connect()
                    except Exception as e:
                        self.log_error("reconnect", e)
            time.sleep(0.003)
        for t in self.fleet():
            try:
                self.stop_truck(t)
            except Exception:
                pass

    def _snapshot_now(self):
        self.snap = self.snapshot()

    def _safe(self, where, fn, *args):
        try:
            fn(*args)
        except GameGone:
            raise
        except Exception as e:
            self.log_error(where, e)

    ACTIONS = {"connect", "send", "bucket_spot", "toggle_pause", "record_toggle", "teach_tip", "stop_all",
               "set_fleet", "set_plant", "send_truck", "rejoin", "redo_dump", "delete_road", "toggle_road_oneway",
               "rename_road", "set_load_spot", "clear_load_spot"}

    def _commands(self):
        while True:
            try:
                name, args = self.q.get_nowait()
            except queue.Empty:
                return
            if name not in self.ACTIONS:
                self.log("Unknown command '%s'." % name)
                continue
            if name == "connect":
                self.connect()
                continue
            if not self.ready:
                self.log("Not connected to the game yet - %s." % (self.error or "waiting"))
                continue
            try:
                getattr(self, name)(*args)
            except GameGone as e:
                self.disconnect(str(e))
            except Exception as e:
                self.log_error(name, e)

    def poll_command_file(self):
        """Lines in command.txt run as commands, e.g. "send", "set_fleet 12 1", "rejoin 18"."""
        if not os.path.exists(COMMANDS):
            return
        try:
            with open(COMMANDS, encoding="utf-8") as f:
                lines = [l.split() for l in f.read().splitlines() if l.strip()]
            os.remove(COMMANDS)
        except OSError:
            return

        def num(a):
            try:
                return float(a)
            except ValueError:
                return a
        for parts in lines:
            name, args = parts[0], [num(a) for a in parts[1:]]
            if name == "set_load_spot" and len(args) == 4:
                args[3] = int(args[3])
            self.log("Command: %s %s" % (name, " ".join(map(str, args))))
            self.q.put((name, tuple(args)))

    def watch_plants(self, now):
        dumping = {t.plant.n for t in self.fleet() if t.state == "DUMPING" and t.plant}
        for p in self.plants:
            if p.addr is None:
                continue
            v = p.hopper(self.proc)
            # wash rate measured over 20 s windows without inflow (the hopper drains in steps, so short
            # samples would overestimate it); a plant that isn't washing ends up with a very low rate
            if p.n in dumping or now - p.last_inflow < 3 or v < 0.3:
                p._last = None
            elif p._last is None:
                p._last = (now, v)
            elif now - p._last[0] >= 20.0:
                t0, v0 = p._last
                r = max(0.0, (v0 - v) / (now - t0))
                p.rate = 0.6 * p.rate + 0.4 * min(0.5, max(0.005, r))
                p._last = (now, v)

    def auto_send(self, now):
        if not self.cfg.get("auto_send"):
            return
        for t in self.fleet():
            if t.state != "AT_LOAD":
                t.full_since = None
                continue
            if t.m.payload() >= self.cfg["auto_send_fraction"] * t.cap:
                t.full_since = t.full_since or now
                if now - t.full_since > 3.0:
                    t.full_since = None
                    self.log("%s: full (%.1f m3) - auto send." % (t.label, t.m.payload()))
                    self.go_plant(t)
                    self._join_idle(skip=t)
            else:
                t.full_since = None

    def tick_truck(self, t, now, dt):
        if t.state == "PLANNING":
            if t.future is not None and t.future.done():
                fut, t.future = t.future, None
                try:
                    res = fut.result()
                    cb, t.after_plan = t.after_plan, None
                    if cb:
                        cb(res)
                except Exception as e:
                    self.log("%s: route planning failed (%s)." % (t.label, e))
                    traceback.print_exc()
                    t.state = "IDLE"
            return
        if t.state in DRIVING:
            self.drive(t, now, dt)
        elif t.state == "DUMPING":
            if not self.paused:
                self.dump_tick(t, now, dt)
        elif t.state == "IDLE":
            tip = t.m.f(M.O_TIPPER)
            if tip > 0.001:
                t.m.put(M.O_TIPPER, max(0.0, tip - 0.5 * dt))
            if self.load is not None and t.note.startswith("no load spot"):
                t.note = ""
                self.join(t)
        if t.state in ("AT_LOAD", "DUMPING") and t.last is not None:
            x, _, z, _, _, _ = t.m.pose()
            if math.hypot(x - t.last[0], z - t.last[1]) > 2.0:
                self.stop_truck(t)
                t.state = "MANUAL"
                self.log("%s: you drove it away - out of the cycle until %s (or Join in the menu)." % (
                    t.label, self.cfg["hotkeys"]["send"]))

    def drive(self, t, now, dt):
        f = t.follower
        if f is None:
            t.state = "IDLE"; return
        if len(f.p.s) < 2:                                  # already there: nothing to drive
            f.done = True
            x, z, _ = t.xz()
            self.arrive(t, now, x, z)
            return
        if t.last is not None:
            cx, _, cz, _, _, _ = t.m.pose()
            if math.hypot(cx - t.last[0], cz - t.last[1]) > 0.6:
                self.stop_truck(t)
                t.state = "MANUAL"
                self.log("%s: you took over - out of the cycle until %s (or Join in the menu)." % (
                    t.label, self.cfg["hotkeys"]["send"]))
                return
        hold = min(t.queue_hold, t.plant_hold, t.traffic_hold)
        if self.paused:
            hold = min(hold, f.s + f.v * f.v / (2 * self.cfg["brake_mps2"]) + 0.05)
        f.step(dt, hold)
        x, z, h, gear, kappa, _ = f.p.pose(f.s)
        if t.gc is None:
            t.gc = M.GroundCache(self.ground)
        y, pitch, roll = t.gc.pose(x, z, h, now)
        steer = max(-1.0, min(1.0, gear * kappa / M.KAPPA_PER_STEER)) if f.v > 0.05 or f.pause > 0 else 0.0
        t.wheel = M.wrap(t.wheel + gear * f.v * dt * M.WHEEL_RAD_PER_M)
        t.m.write_drive(x, y, z, h, pitch, roll, gear * f.v, steer, t.wheel)
        tip = t.m.f(M.O_TIPPER)
        if tip > 0:
            t.m.put(M.O_TIPPER, max(0.0, tip - 0.01))
        t.last = (x, z)
        if f.done:
            self.arrive(t, now, x, z)

    def arrive(self, t, now, x, z):
        """End of a route: start tipping, or wait under the bucket."""
        t.m.put(M.O_SPEED, 0.0)
        t.follower = None
        t.last = (x, z)
        if t.purpose == "plant":
            t.state = "DUMPING"
            t.dump = {"phase": "up", "t0": now, "tip": t.m.f(M.O_TIPPER)}
        else:
            L = self.load_spot()
            if L and dist2((x, z), L[:2]) > 1.5:
                self.go_load(t, short=True)
            else:
                t.state = "AT_LOAD"
                t.ignore = set()
                t.last = (x, z)
                self.log("%s: under the bucket, ready to load." % t.label)

    # ------------------------------------------------------------------ emulated tipping
    # The game only runs a truck's dump while you sit in it, so the tool moves the bed's cargo into
    # the hopper itself, using the game's own bed grids (measured to match a real dump: the hopper
    # gets the bed's bank, pay and pay x grade volumes).

    def is_full(self, t):
        return t.m.payload() >= self.cfg["full_fraction"] * (t.cap or 30.0)

    def bed_vecs(self, anchor):
        out = []
        for k in range(self.BED_VECS):
            raw = self.proc.read(anchor + self.bed_layout["vec"] + k * 0x18, 24)
            if not raw or len(raw) != 24:
                raise GameGone("truck bed not readable")
            cap, ptr, ln = struct.unpack("<QQQ", raw)
            if not (0 < ln <= cap <= 1_000_000) or ptr < 0x10000:
                raise RuntimeError("truck bed data looks wrong (len %d, cap %d) - not tipping" % (ln, cap))
            out.append((ptr, ln))
        return out

    def scale_bed(self, t, keep):
        removed = None
        for anchor in self.beds[t.index]:
            r = self._scale_one_bed(anchor, keep)
            removed = removed or r
        return removed

    def _scale_one_bed(self, anchor, keep):
        removed = []
        for ptr, ln in self.bed_vecs(anchor):
            arr = np.frombuffer(self.proc.read(ptr, ln * 4), dtype=np.float32)
            removed.append(float(arr.sum()) * (1 - keep))
            self.proc.write(ptr, (arr * keep if keep > 0 else np.zeros_like(arr)).astype(np.float32).tobytes())
        lay = self.bed_layout
        cache = list(self.proc.f32(anchor + lay["cache"], 20))
        if keep > 0:
            for i in (0, 1, 2, 5):                 # cached totals: loose, bank, mass, material
                cache[i] *= keep
        else:
            cache = [0.0] * 20
        self.proc.put_f32(anchor + lay["cache"], *cache)
        off, fmt = lay["rev"]                       # bump the revision: the game redraws the bed
        rev = struct.unpack(fmt, self.proc.read(anchor + off, struct.calcsize(fmt)))[0]
        self.proc.write(anchor + off, struct.pack(fmt, (rev + 1) & (0xFFFFFFFF if fmt == "<I" else 0xFFFFFFFFFFFFFFFF)))
        disc = self.proc.f32(anchor, 1)[0]
        self.proc.put_f32(anchor, disc + removed[0])
        return removed

    def dump_tick(self, t, now, dt):
        c, p = t.dump, t.plant
        c.setdefault("next_flow", now)
        if t.index not in self.beds or p is None or p.addr is None:
            if c["phase"] != "down":
                self.log("%s: can't tip - truck bed or wash plant not found in memory (wait for an autosave, "
                         "then Reconnect)." % t.label)
            c["phase"] = "down"
            c["failed"] = True
        if c["phase"] == "up":
            c["tip"] = min(1.0, c["tip"] + 0.4 * dt)
            t.m.put(M.O_TIPPER, c["tip"])
            if c["tip"] >= 0.6:
                c["phase"] = "flow"
        elif c["phase"] == "flow":
            c["tip"] = min(1.0, c["tip"] + 0.4 * dt)
            t.m.put(M.O_TIPPER, c["tip"])
            if now < c["next_flow"]:
                return
            step = now - c.get("last_flow", now - 0.05)
            c["last_flow"], c["next_flow"] = now, now + 0.05
            loose = t.m.payload()
            hop = p.hopper(self.proc)
            if loose <= 0.02:
                self.scale_bed(t, 0.0)
                t.m.put(M.O_PAYLOAD, 0.0, 0.0, 0.0, 0.0)
                c["phase"] = "down"
                return
            if hop > self.cfg["hopper_max_m3"]:
                if not c.get("full_logged"):
                    self.log("%s: %s hopper full, waiting for it to wash down." % (t.label, p.name))
                    c["full_logged"] = True
                if now - c["t0"] > self.cfg["dump_max_s"]:
                    self.log("%s: hopper stayed full, leaving with %.1f m3." % (t.label, loose)); c["phase"] = "down"
                return
            frac = min(1.0, self.FLOW_LOOSE_M3S * step / loose)
            if loose * (1 - frac) < 0.05:
                frac = 1.0
            rem = self.scale_bed(t, 1.0 - frac)
            pay_left = max(0.0, t.m.f(M.O_PAYLOAD + 4) * (1 - frac))
            t.m.put(M.O_PAYLOAD, loose * (1 - frac), pay_left)
            hb = self.proc.f32(p.addr + self.HOP_BANK, 1)[0]
            hp, hg = self.proc.f32(p.addr + self.HOP_PAY, 2)
            self.proc.put_f32(p.addr + self.HOP_BANK, hb + rem[1])
            self.proc.put_f32(p.addr + self.HOP_PAY, hp + rem[4], hg + rem[6])
            self.proc.put_f32(p.addr + self.HOP_VISIBLE, 1.5)
            p.last_inflow = now
            c["moved"] = c.get("moved", 0.0) + rem[0]
            if frac >= 1.0:
                t.m.put(M.O_PAYLOAD, 0.0, 0.0, 0.0, 0.0)
                c["phase"] = "down"
        else:
            c["tip"] = max(0.0, c["tip"] - 0.5 * dt)
            t.m.put(M.O_TIPPER, c["tip"])
            if c["tip"] <= 0.0:
                moved = c.get("moved", 0.0)
                t.dump = None
                if c.get("failed"):
                    t.state = "IDLE"
                    t.note = "couldn't tip here (bed or plant not found in memory) - tip it yourself, then Join"
                    return
                self.log("%s: tipped %.1f m3 into %s, heading back." % (t.label, moved, p.name if p else "?"))
                if self.is_full(t) and c.get("full_logged"):
                    self.go_plant(t, " (hopper was full)")
                else:
                    self.go_load(t)

    # ------------------------------------------------------------------ status for the UI

    def snapshot(self):
        hk = self.cfg["hotkeys"]
        trucks = []
        for t in self.trucks:
            x, y, z, h, _, _ = t.m.pose()
            pay = t.m.payload()
            label = STATE_LABEL.get(t.state, t.state)
            if t.state == "TO_PLANT" and t.plant:
                label = "to %s" % t.plant.name
            if t.state == "DUMPING" and t.plant:
                label = "tipping at %s" % t.plant.name
            reason = ""
            if t.state in DRIVING and t.follower:
                f = t.follower
                if f.v < 0.05 and not f.done:
                    if t.queue_hold != INF and f.s >= t.queue_hold - 0.1:
                        reason = t.hold_reason
                    elif t.plant_hold != INF and f.s >= t.plant_hold - 0.1:
                        reason = "waiting for %s hopper" % (t.plant.name if t.plant else "the")
                    elif t.traffic_hold != INF and t.blocker is not None:
                        o = self.truck(t.blocker)
                        reason = "waiting for %s" % (o.label if o else "%s #%d" % (self.ids[t.blocker], t.blocker))
                    elif self.paused:
                        reason = "paused"
            if t.state == "DUMPING" and t.dump and t.dump.get("full_logged") and t.dump.get("phase") == "flow":
                reason = "hopper full - waiting"
            if t.state == "IDLE" and t.note:
                reason = t.note
            path = []
            if t.follower is not None:
                path = t.follower.p.points(t.follower.s, 2.0)
            trucks.append({"index": t.index, "label": t.label, "name": t.name, "model": t.model, "color": t.color,
                           "fleet": t.in_fleet, "state": t.state, "text": label, "reason": reason,
                           "payload": pay, "cap": t.cap, "pos": (x, y, z), "h": h,
                           "speed": t.follower.v if t.follower else 0.0, "path": path})
        plants = []
        for p in self.plants:
            hop = p.hopper(self.proc) if p.addr else None
            plants.append({"n": p.n, "name": p.name, "enabled": p.enabled, "found": p.addr is not None,
                           "pos": (p.x, p.y, p.z), "yaw": p.yaw, "hopper": hop, "max": self.cfg["hopper_max_m3"],
                           "rate": p.rate * 60.0,
                           "tip": p.pose_for(self.cfg["default_dump_offset"] if not self.cfg["dump_offsets"] else
                                             next(iter(self.cfg["dump_offsets"].values())))})
        L = self.load
        load = None
        if L:
            try:
                y = self.ground.patch(L[0] - 1, L[1] - 1, L[0] + 1, L[1] + 1)
                y = M.Ground._sample(y, L[0], L[1])
            except Exception:
                y = 0.0
            load = (L[0], y, L[1], L[2])
        exc = None
        if self.rel:
            ex, ey, ez, eh, _, _ = self.machine(self.rel["exc"]).pose()
            exc = (ex, ey, ez, self.ids[self.rel["exc"]] + " #%d" % self.rel["exc"])
        return {"ready": self.ready, "connecting": self.connecting, "paused": self.paused,
                "save": ("%s (slot %d)" % (self.save.name, self.save.slot)) if self.save else None,
                "beds": bool(self.beds), "version": VERSION,
                "recording": self.rec is not None, "auto": bool(self.cfg.get("auto_send")),
                "trucks": trucks, "plants": plants, "load": load, "exc": exc,
                "roads": [(r.name, r.one_way, r.length(), r.pts[::3].tolist()) for r in self.roads],
                "log": self.lines[-8:], "hotkeys": hk, "error": self.error}
