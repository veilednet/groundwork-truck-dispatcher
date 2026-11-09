"""Screenshots of the control panel with made-up data (no game needed): dev/ui_<tab>.png.
python dev/ui_shot.py [--setup]   (--setup: a fresh save, nothing set up yet)"""
import json, os, sys, time
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import tkinter as tk
from PIL import ImageGrab
import gamecam
gamecam.set_dpi_aware()
import dispatcher as D
import overlay as O

fresh = "--setup" in sys.argv


class FakeD:
    version = D.VERSION

    def __init__(self):
        self.cfg = json.loads(json.dumps(D.DEFAULTS))          # defaults, not your own config.json
        self.lines = ["05:49:03 Load spot learned from where you loaded ah-66 #21.",
                      "05:49:03 ah-66 #21: to Plant 2 with 30.0 m3 - 117 m, about 52 s, route forward out, back in.",
                      "05:49:52 ah-66 #20: under the bucket, ready to load.",
                      "05:51:38 ah-66 #20: full (29.0 m3) - auto send.",
                      "05:51:38 ah-66 #20: to Plant 1 with 29.0 m3 - 109 m, about 50 s, route forward out, back in."]

    def command(self, *a):
        print("command", a)


def snap():
    hk = {"menu": "F6", "send": "F7", "bucket": "F8", "pause": "F9", "record": "F10"}
    tr = [(15, "ah-66", "AT_LOAD", "loading", "", 18.4, 30, True),
          (18, "ah-66", "RETURNING", "returning", "waiting in line (next)", 0.0, 30, True),
          (20, "ah-66", "TO_PLANT", "to Plant 2", "", 29.0, 30, True),
          (21, "ah-66", "DUMPING", "tipping at Plant 1", "", 12.2, 30, True),
          (4, "dt-44", "PARKED", "not in the fleet", "", 0.0, 22, False),
          (12, "dt-88", "PARKED", "not in the fleet", "", 0.0, 22, False)]
    trucks = [{"index": i, "label": "%s #%d" % (m, i), "name": "#%d" % i, "model": m, "color": D.COLORS[n],
               "fleet": f and not fresh, "state": s if not fresh else "PARKED", "text": t if not fresh else
               "not in the fleet", "reason": r, "payload": p, "cap": c, "pos": (0, 0, 0), "h": 0, "speed": 0,
               "path": []} for n, (i, m, s, t, r, p, c, f) in enumerate(tr)]
    plants = [{"n": 0, "name": "Plant 1", "enabled": True, "found": True, "pos": (0, 0, 0), "yaw": 0, "hopper": 31.0,
               "max": 40, "rate": 7.1, "tip": (0, 0, 0)},
              {"n": 1, "name": "Plant 2", "enabled": True, "found": True, "pos": (0, 0, 0), "yaw": 0, "hopper": 6.0,
               "max": 40, "rate": 6.8, "tip": (0, 0, 0)}]
    return {"ready": True, "connecting": False, "paused": False, "save": "Spruced Up (slot 1)", "recording": False,
            "auto": True, "trucks": trucks, "plants": plants,
            "load": None if fresh else (-306.9, 11, -342.7, 1.58),
            "exc": None if fresh else (-315, 14, -336, "ex-94 #17"),
            "roads": [] if fresh else [("Road 1", True, 134.0, []), ("Road 2", False, 62.0, [])],
            "log": [] if fresh else FakeD().lines, "hotkeys": hk, "error": ""}


class FakeApp:
    def save_soon(self): pass
    def sync_panels(self): pass
    def set_hotkeys(self, k): return True


root = tk.Tk()
root.configure(bg=O.BG)
root.geometry("1180x800+40+40")
root.attributes("-topmost", True)
d = FakeD()
p = O.Panel(root, d, FakeApp(), desktop=True)
p.pack(fill="both", expand=True)
p.refresh(snap(), "Overlay: following the game camera, 144 fps")
out = os.path.join(HERE, "dev")
for tab in O.Panel.TABS:
    p.show_tab(tab)
    root.update(); time.sleep(0.4); root.update()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    img = ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height()))
    name = os.path.join(out, "ui_%s%s.png" % (tab.split()[0].lower(), "_setup" if fresh else ""))
    img.save(name); print(name)
    if fresh:
        break
root.destroy()
