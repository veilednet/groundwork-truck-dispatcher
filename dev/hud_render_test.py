"""Render the HUD and a world frame into images without the game (checks the GDI drawing code)."""
import ctypes, json, os, sys, time
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import numpy as np
from PIL import Image
import gamecam
gamecam.set_dpi_aware()
import gdioverlay as G
import overlay as O
import dispatcher as D


class FakeD:
    def __init__(self):
        self.cfg = json.loads(json.dumps(D.DEFAULTS))
        for k, v in O.OVERLAY_DEFAULTS.items():
            self.cfg["overlay"].setdefault(k, v)
        self.ready, self.proc, self.version = False, None, D.VERSION
        self.snap = {"ready": True, "paused": False, "auto": True, "recording": False, "hotkeys": self.cfg["hotkeys"],
                     "trucks": [
                         {"index": 15, "label": "ah-66 #15", "name": "#15", "model": "ah-66", "color": "#ffb000",
                          "fleet": True, "state": "AT_LOAD", "text": "loading", "reason": "", "payload": 21.4,
                          "cap": 30, "pos": (-306.9, 11, -342.7), "h": 1.58, "speed": 0, "path": []},
                         {"index": 18, "label": "ah-66 #18", "name": "#18", "model": "ah-66", "color": "#29c5f6",
                          "fleet": True, "state": "RETURNING", "text": "returning", "reason": "waiting in line (next)",
                          "payload": 0.0, "cap": 30, "pos": (-322.3, 14, -320.4), "h": 3.0, "speed": 0,
                          "path": [(-322.3 + i * 0.2, 14, -320.4 - i, 1) for i in range(20)]},
                         {"index": 12, "label": "dt-88 #12", "name": "#12", "model": "dt-88", "color": "#ff5fd2",
                          "fleet": True, "state": "TO_PLANT", "text": "to Plant 1", "reason": "", "payload": 22.0,
                          "cap": 22, "pos": (-300.0, 12, -310.0), "h": 0.2, "speed": 5,
                          "path": [(-300 + i * 0.3, 12, -310 + i * 2, 1) for i in range(18)]}],
                     "plants": [{"n": 0, "name": "Plant 1", "enabled": True, "found": True, "pos": (-311.7, 9.5, -274.6),
                                 "yaw": -2.71, "hopper": 23.0, "max": 40, "rate": 7.2, "tip": (-316.7, -286.0, -2.74)},
                                {"n": 1, "name": "Plant 2", "enabled": True, "found": True, "pos": (-250.0, 9.5, -260.0),
                                 "yaw": 0.0, "hopper": 38.0, "max": 40, "rate": 7.0, "tip": (-250.0, -248.0, 0.0)}],
                     "load": (-306.9, 11.0, -342.7, 1.58), "exc": (-315.5, 14.0, -336.9, "ex-94 #17"),
                     "roads": [("Road to plant", True, 134, [(-306 + i, -342 + i * 1.3) for i in range(0, 50, 3)])],
                     "log": []}


ov = G.NativeOverlay.__new__(G.NativeOverlay)
ov.d = FakeD(); ov.edit = True; ov._drag = None; ov.hud_rect = (0, 0, 0, 0)
proc = G.WNDPROC(lambda h, m, w, l: G.DefWindowProcW(h, m, w, l))
wc = G.WNDCLASSEXW(ctypes.sizeof(G.WNDCLASSEXW), 0, proc, 0, 0, G.GetModuleHandleW(None), None, None, None, None,
                   "TDTest", None)
G.RegisterClassExW(ctypes.byref(wc))
ov.hud = G.Surface("TDTest", "t")
ov.world = G.Surface("TDTest", "w")
t0 = time.perf_counter()
ov.draw_hud(ov.d.snap, (0, 0, 2560, 1440), ov.d.cfg["overlay"])
print("hud draw %.1f ms" % ((time.perf_counter() - t0) * 1000))
ov.hud.hide()


def grab(s):
    buf = (ctypes.c_ubyte * (s.w * s.h * 4)).from_address(s.bits.value)
    a = np.frombuffer(buf, dtype=np.uint8).reshape(s.h, s.w, 4)[:, :, [2, 1, 0]].copy()
    return Image.fromarray(a)


grab(ov.hud).save(os.path.join(HERE, "dev", "hud_test.png"))


class FakeCam:                     # looking north-west over the site from above the load spot
    def __init__(self):

        import math
        c = np.array([-290.0, 40.0, -360.0]); tgt = np.array([-310.0, 10.0, -315.0])
        f = tgt - c; f /= np.linalg.norm(f); r = np.cross(f, [0, 1, 0]); r /= np.linalg.norm(r); u = np.cross(r, f)
        self.c, self.f, self.r, self.u = c, f, -r, u
        self.tx, self.ty = math.tan(math.radians(51)), math.tan(math.radians(35))
        self.ok = True
    project = gamecam.Camera.project


ov.d.cfg["overlay"].update(roads=True, routes=True, labels=True, markers=True)
ov.d.machine = lambda i: (_ for _ in ()).throw(RuntimeError())
ov.world.resize(2560, 1440)
ov.world.clear()
t0 = time.perf_counter()
ov.draw_world(ov.world, FakeCam(), ov.d.snap, 2560, 1440, ov.d.cfg["overlay"])
t1 = time.perf_counter()
grab(ov.world).resize((1280, 720)).save(os.path.join(HERE, "dev", "world_test.png"))
for _ in range(20):
    ov.world.clear()
t2 = time.perf_counter()
print("world draw %.1f ms, clear %.2f ms" % ((t1 - t0) * 1000, (t2 - t1) / 20 * 1000))
