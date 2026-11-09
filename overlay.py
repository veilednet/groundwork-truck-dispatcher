"""Control panel (tabs: Fleet, Overlay, Settings, Roads & log), used by both the in-game menu (F6)
and the desktop window. The overlay over the game itself is drawn by gdioverlay.NativeOverlay."""
import json, os
import tkinter as tk
from tkinter import colorchooser, messagebox

import gamecam

# veilednet palette (same as the Rake Mod and the Save Editor)
BG, CARD, ROW, TRACK = "#0f1115", "#15171c", "#232731", "#2e333e"
ACCENT, ACCENT_HI = "#f2a93b", "#ffbe5c"
FG, MUTED, FILL = "#eceef2", "#8b91a0", "#6b7180"
GOOD, BAD = "#5bd68a", "#f06a6a"
BG2 = ROW
STATE_COL = {"AT_LOAD": GOOD, "TO_PLANT": ACCENT, "DUMPING": "#ff9a52", "RETURNING": "#5cc8f2",
             "REPOSITION": "#5cc8f2", "PLANNING": "#b9a2ff", "IDLE": MUTED, "MANUAL": BAD, "PARKED": FILL}

OVERLAY_DEFAULTS = {
    "hud": True, "minimap": True, "routes": True, "labels": True, "markers": True, "roads": False, "label_bg": True,
    "hud_scale": 1.0, "hud_alpha": 0.92, "world_alpha": 0.95, "line_width": 3, "label_px": 15, "label_scale": 1.0,
    "minimap_zoom": 1.0, "max_fps": 0, "label_max_m": 500, "hud_pos": [0.012, 0.02],
    "color_load": GOOD, "color_tip": ACCENT, "color_reverse": "#ffffff", "color_text": FG,
    "color_accent": ACCENT, "color_hud_bg": CARD, "color_label_bg": CARD, "color_roads": "#8a8fa0",
    "truck_colors": {},
}
# colours of the first versions: replaced by the new defaults unless the user picked their own
_OLD_DEFAULTS = {"color_load": "#6fd36f", "color_tip": "#e0a030", "color_text": "#e8e8ea",
                 "color_accent": "#e0a030", "color_hud_bg": "#15171d", "color_label_bg": "#15171d"}
LIMITS = {"hud_scale": (0.5, 3.0), "hud_alpha": (0.2, 1.0), "world_alpha": (0.2, 1.0), "line_width": (1, 10),
          "label_scale": (0.5, 2.5), "minimap_zoom": (0.5, 4.0), "label_max_m": (50, 1000), "max_fps": (0, 240)}


def overlay_settings(cfg):
    """Fill in missing overlay settings and repair bad ones (hand-edited config.json)."""
    ov = cfg.setdefault("overlay", {})
    for k, v in OVERLAY_DEFAULTS.items():
        if k not in ov or type(ov[k]) is not type(v) and not (isinstance(v, (int, float)) and
                                                              isinstance(ov[k], (int, float))):
            ov[k] = json.loads(json.dumps(v))
        elif k in _OLD_DEFAULTS and ov[k] == _OLD_DEFAULTS[k]:
            ov[k] = v
    for k, (lo, hi) in LIMITS.items():
        try:
            ov[k] = max(lo, min(hi, float(ov[k])))
        except (TypeError, ValueError):
            ov[k] = OVERLAY_DEFAULTS[k]
    if not (isinstance(ov["hud_pos"], list) and len(ov["hud_pos"]) == 2):
        ov["hud_pos"] = list(OVERLAY_DEFAULTS["hud_pos"])
    return ov


def fmt(v, d=1):
    return "-" if v is None else ("%." + str(d) + "f") % v


# ====================================================================== small widgets

class Button(tk.Label):
    """Flat button: kind = primary (amber) / secondary / danger."""
    STYLES = {"primary": (ACCENT, "#15171c", ACCENT_HI), "secondary": (ROW, FG, TRACK),
              "danger": ("#3a2024", BAD, "#4a272c")}

    def __init__(self, parent, text, command, kind="secondary", font=None, **kw):
        self.bg, self.fg, self.hover = self.STYLES[kind]
        super().__init__(parent, text=text, bg=self.bg, fg=self.fg, font=font, padx=12, pady=4, cursor="hand2", **kw)
        self.command, self.enabled = command, True
        self.bind("<Enter>", lambda e: self.enabled and self.configure(bg=self.hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self.bg))
        self.bind("<Button-1>", lambda e: self.enabled and self.command())

    def set_enabled(self, on):
        if on != self.enabled:
            self.enabled = on
            self.configure(fg=self.fg if on else FILL, cursor="hand2" if on else "arrow")


class Check(tk.Canvas):
    """Square check box in the house style."""

    def __init__(self, parent, value, command, size=18, bg=CARD):
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0, cursor="hand2")
        self.size, self.value, self.command = size, bool(value), command
        self.bind("<Button-1>", lambda e: self.toggle())
        self.draw()

    def draw(self):
        s = self.size
        self.delete("all")
        self.create_rectangle(1, 1, s - 1, s - 1, fill=ACCENT if self.value else TRACK, width=0)
        if self.value:
            self.create_line(s * 0.24, s * 0.52, s * 0.43, s * 0.72, s * 0.78, s * 0.3, fill="#15171c",
                             width=max(2, s // 8), capstyle="round", joinstyle="round")

    def set(self, v):
        if bool(v) != self.value:
            self.value = bool(v)
            self.draw()

    def toggle(self):
        self.set(not self.value)
        self.command(self.value)


class Slider(tk.Canvas):
    """Flat slider: track, amber fill, round knob, value on the right. get()/set() like a tk variable."""

    def __init__(self, parent, lo, hi, res, value, command, width=230, font=None, bg=CARD):
        self.h = 22
        super().__init__(parent, width=width + 60, height=self.h, bg=bg, highlightthickness=0, cursor="hand2")
        self.lo, self.hi, self.res, self.command, self.font = lo, hi, res, command, font
        self.value = self._snap(value)
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<MouseWheel>", lambda e: self._step(1 if e.delta > 0 else -1))

    def _snap(self, v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = self.lo
        v = round((v - self.lo) / self.res) * self.res + self.lo
        return max(self.lo, min(self.hi, round(v, 6)))

    def _geom(self):
        w = max(40, self.winfo_width() - 60)
        return 8, w - 8

    def draw(self):
        self.delete("all")
        x0, x1 = self._geom()
        u = (self.value - self.lo) / ((self.hi - self.lo) or 1)
        xk, y = x0 + (x1 - x0) * u, self.h / 2
        self.create_line(x0, y, x1, y, fill=TRACK, width=4, capstyle="round")
        self.create_line(x0, y, xk, y, fill=ACCENT, width=4, capstyle="round")
        self.create_oval(xk - 7, y - 7, xk + 7, y + 7, fill=FG, outline=ACCENT, width=2)
        digits = 0 if self.res >= 1 else (1 if self.res >= 0.1 else 2)
        self.create_text(x1 + 14, y, text=("%." + str(digits) + "f") % self.value, fill=FG, anchor="w",
                         font=self.font)

    def _set_from_ui(self, v):
        v = self._snap(v)
        if v != self.value:
            self.value = v
            self.draw()
            self.command(v)

    def _drag(self, e):
        x0, x1 = self._geom()
        self._set_from_ui(self.lo + (self.hi - self.lo) * (e.x - x0) / max(1, x1 - x0))

    def _step(self, n):
        self._set_from_ui(self.value + n * self.res)

    def get(self):
        return self.value

    def set(self, v):
        v = self._snap(v)
        if v != self.value:
            self.value = v
            self.draw()


def divider(parent, bg=TRACK, **pack):
    f = tk.Frame(parent, bg=bg, height=1)
    f.pack(fill="x", **pack)
    return f


# ====================================================================== the panel

class Panel(tk.Frame):
    TABS = ("Fleet", "Overlay", "Settings", "Roads & log")

    def __init__(self, master, d, app, big=False, on_close=None, desktop=False):
        super().__init__(master, bg=BG)
        self.d, self.app, self.big = d, app, big
        fs = 12 if big else 10
        self.fs = fs
        self.f = ("Segoe UI", fs)
        self.fb = ("Segoe UI", fs, "bold")
        self.fh = ("Segoe UI", fs + 1, "bold")
        self.fm = ("Segoe UI", fs - 1)
        self.truck_rows, self.truck_key = [], None
        self.plant_rows, self.plant_key = [], None
        self.road_key = None
        self.syncers = []                 # (var, getter) pairs re-read from the config on sync()
        self.hk_buttons = []              # (button, template, hotkey name): labels follow hotkey changes
        overlay_settings(d.cfg)

        top = tk.Frame(self, bg=BG); top.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(top, text="TRUCK DISPATCHER", font=("Segoe UI", fs + 5, "bold"), fg=ACCENT, bg=BG).pack(side="left")
        tk.Label(top, text="v" + getattr(d, "version", "") if getattr(d, "version", "") else "",
                 font=self.fm, fg=MUTED, bg=BG).pack(side="left", padx=(8, 0), pady=(6, 0))
        if on_close:
            self.close_btn = Button(top, "Close", on_close, font=self.f)
            self.close_btn.pack(side="right")
            self.hk_buttons.append((self.close_btn, "Close   %s / Esc", "menu"))
        self.status = tk.Label(top, text="", font=self.fb, fg=MUTED, bg=BG)
        self.status.pack(side="right", padx=12)

        nav = tk.Frame(self, bg=BG); nav.pack(fill="x", padx=14)
        self.tabs, self.tab_btns = {}, {}
        foot = tk.Frame(self, bg=BG); foot.pack(fill="x", side="bottom", padx=14, pady=(0, 8))
        divider(foot, pady=(0, 6))
        self.keyhint = tk.Label(foot, text="", font=self.fm, fg=MUTED, bg=BG, anchor="w")
        self.keyhint.pack(side="left")
        tk.Label(foot, text="veilednet", font=self.fm, fg=FILL, bg=BG).pack(side="right")
        body = tk.Frame(self, bg=BG); body.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        for name in self.TABS:
            cell = tk.Frame(nav, bg=BG); cell.pack(side="left", padx=(0, 18))
            lab = tk.Label(cell, text=name, font=self.fb, fg=MUTED, bg=BG, cursor="hand2", pady=6)
            lab.pack()
            bar = tk.Frame(cell, height=3, bg=BG); bar.pack(fill="x")
            lab.bind("<Button-1>", lambda e, n=name: self.show_tab(n))
            self.tab_btns[name] = (lab, bar)
            self.tabs[name] = tk.Frame(body, bg=BG)
        divider(self, before=body)

        self.build_fleet(self.tabs["Fleet"])
        self.build_overlay(self.tabs["Overlay"])
        self.build_settings(self.tabs["Settings"])
        self.build_roads(self.tabs["Roads & log"])
        self.show_tab("Fleet")
        self.sync_hotkey_labels()

    # ------------------------------------------------------------------ helpers
    def show_tab(self, name):
        for n, fr in self.tabs.items():
            fr.pack_forget()
            lab, bar = self.tab_btns[n]
            lab.configure(fg=FG if n == name else MUTED)
            bar.configure(bg=ACCENT if n == name else BG)
        self.tabs[name].pack(fill="both", expand=True, pady=(8, 0))

    def card(self, parent, title, note="", side=None, expand=False):
        outer = tk.Frame(parent, bg=CARD)
        if side:
            outer.pack(side=side, fill="both", expand=True, padx=(0, 10) if side == "left" else 0, pady=(0, 10))
        else:
            outer.pack(fill="both" if expand else "x", expand=expand, pady=(0, 10))
        head = tk.Frame(outer, bg=CARD); head.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(head, text=title, font=self.fh, fg=ACCENT, bg=CARD).pack(side="left")
        if note:
            tk.Label(head, text=note, font=self.fm, fg=MUTED, bg=CARD).pack(side="left", padx=(10, 0), pady=(2, 0))
        inner = tk.Frame(outer, bg=CARD); inner.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        return inner

    def subhead(self, parent, text):
        tk.Label(parent, text=text, font=self.fb, fg=FG, bg=CARD, anchor="w").pack(fill="x", pady=(8, 2))

    def btn(self, parent, text, cmd, kind="secondary", hotkey=None):
        b = Button(parent, text, cmd, kind=kind, font=self.f)
        if hotkey:
            self.hk_buttons.append((b, text, hotkey))
        return b

    def check(self, parent, text, get, set_, bg=CARD):
        row = tk.Frame(parent, bg=bg)
        c = Check(row, get(), set_, size=self.fs + 8, bg=bg)
        c.pack(side="left")
        lab = tk.Label(row, text=text, font=self.f, fg=FG, bg=bg, cursor="hand2")
        lab.pack(side="left", padx=(8, 0))
        lab.bind("<Button-1>", lambda e: c.toggle())
        self.syncers.append((c, get))
        return row

    def ov_check(self, parent, key, text):
        def set_(v):
            self.d.cfg["overlay"][key] = bool(v); self.app.save_soon(); self.app.sync_panels()
        return self.check(parent, text, lambda: bool(self.d.cfg["overlay"].get(key)), set_)

    def slider(self, parent, text, lo, hi, res, get, set_, width=220):
        row = tk.Frame(parent, bg=CARD); row.pack(fill="x", pady=3)
        tk.Label(row, text=text, font=self.f, fg=FG, bg=CARD, width=30, anchor="w").pack(side="left")
        s = Slider(row, lo, hi, res, get(), set_, width=width, font=self.fm)
        s.pack(side="left", fill="x", expand=True)
        self.syncers.append((s, get))
        return s

    def ov_slider(self, parent, text, key, lo, hi, res):
        def set_(val):
            self.d.cfg["overlay"][key] = val; self.app.save_soon()
        return self.slider(parent, text, lo, hi, res, lambda: float(self.d.cfg["overlay"].get(key, 0)), set_)

    def opt_slider(self, parent, text, key, lo, hi, res):
        def set_(val):
            self.d.cfg[key] = float(val); self.app.save_soon()
        return self.slider(parent, text, lo, hi, res, lambda: float(self.d.cfg.get(key, 0)), set_)

    def color_btn(self, parent, text, key):
        row = tk.Frame(parent, bg=CARD); row.pack(fill="x", pady=2)
        sw = tk.Label(row, text="", width=4, cursor="hand2", bg=self.d.cfg["overlay"].get(key, "#ffffff"),
                      highlightthickness=1, highlightbackground=FILL)
        sw.pack(side="left")
        tk.Label(row, text=text, font=self.f, fg=FG, bg=CARD, anchor="w").pack(side="left", padx=(10, 0))

        def pick(_e=None):
            cur = self.d.cfg["overlay"].get(key, "#ffffff")
            c = colorchooser.askcolor(color=cur, title=text, parent=self.winfo_toplevel())[1]
            if c:
                self.d.cfg["overlay"][key] = c; self.app.save_soon(); self.app.sync_panels()
        sw.bind("<Button-1>", pick)
        self.syncers.append((None, lambda: sw.configure(bg=self.d.cfg["overlay"].get(key, "#ffffff"))))

    def sync(self):
        """Re-read every control from the config (the other panel may have changed it)."""
        for v, get in self.syncers:
            try:
                if v is None:
                    get()
                elif isinstance(v, Check):
                    v.set(get())
                elif v.get() != get():
                    v.set(get())
            except Exception:
                pass
        self.sync_hotkey_labels()

    def sync_hotkey_labels(self):
        hk = self.d.cfg["hotkeys"]
        for b, template, name in self.hk_buttons:
            key = hk.get(name, "?")
            b.configure(text=(template % key) if "%s" in template else "%s   %s" % (template, key))
        self.keyhint.configure(text="%s menu   ·   %s send loaded truck   ·   %s load spot at bucket   ·   "
                                    "%s pause   ·   %s record road" % (hk["menu"], hk["send"], hk["bucket"],
                                                                       hk["pause"], hk["record"]))

    # ------------------------------------------------------------------ Fleet tab
    def build_fleet(self, tab):
        self.setup = self.card(tab, "GET STARTED", "four steps, done once per save")
        self.setup_head = self.setup.master.winfo_children()[0]
        self.setup_note = self.setup_head.winfo_children()[1]
        self.setup_rows = []
        self.setup_done = None
        for i in range(4):
            r = tk.Frame(self.setup, bg=CARD); r.pack(fill="x", pady=1)
            icon = tk.Label(r, text="", font=self.fb, bg=CARD, width=2)
            icon.pack(side="left")
            title = tk.Label(r, text="", font=self.fb, fg=FG, bg=CARD, width=22, anchor="w")
            title.pack(side="left")
            detail = tk.Label(r, text="", font=self.f, fg=MUTED, bg=CARD, anchor="w")
            detail.pack(side="left", fill="x")
            self.setup_rows.append((icon, title, detail))

        cols = tk.Frame(tab, bg=BG); cols.pack(fill="x")
        self.trucks = self.card(cols, "TRUCKS", "tick the trucks the dispatcher drives · click a dot for its colour")
        self.plants = self.card(tab, "WASH PLANTS", "loaded trucks go to the ticked plant with the shortest wait")

        acts = self.card(tab, "ACTIONS")
        g = tk.Frame(acts, bg=CARD); g.pack(fill="x")
        row1 = [("Send loaded truck", "send", "primary", "send"),
                ("Load spot at bucket", "bucket_spot", "secondary", "bucket"),
                ("Pause / resume", "toggle_pause", "secondary", "pause"),
                ("Record a road", "record_toggle", "secondary", "record")]
        row2 = [("Teach tip spot", "teach_tip", "secondary", None),
                ("Clear load spot", "clear_load_spot", "secondary", None),
                ("Reconnect to game", "connect", "secondary", None),
                ("Stop all trucks", "stop_all", "danger", None)]
        for r, row in enumerate((row1, row2)):
            for c, (text, cmd, kind, hk) in enumerate(row):
                self.btn(g, text, lambda c_=cmd: self.d.command(c_), kind, hk).grid(
                    row=r, column=c, sticky="ew", padx=(0, 6), pady=(0, 6))
        for c in range(4):
            g.grid_columnconfigure(c, weight=1, uniform="a")
        self.loadinfo = tk.Label(acts, text="", font=self.f, fg=MUTED, bg=CARD, anchor="w", justify="left")
        self.loadinfo.pack(fill="x")

        logc = self.card(tab, "RECENT", expand=True)
        self.lastlog = tk.Label(logc, text="", font=("Consolas", self.fs - 1), fg=MUTED, bg=CARD, anchor="nw",
                                justify="left")
        self.lastlog.pack(fill="both", expand=True)
        self.lastlog.bind("<Configure>", lambda e: self.lastlog.configure(wraplength=max(200, e.width - 10)))

    # ------------------------------------------------------------------ Overlay tab
    def build_overlay(self, tab):
        cols = tk.Frame(tab, bg=BG); cols.pack(fill="both", expand=True)
        a = self.card(cols, "SHOW", side="left")
        g = tk.Frame(a, bg=CARD); g.pack(fill="x")
        for i, (k, t) in enumerate([("hud", "Status panel"), ("minimap", "Mini map in the panel"),
                                    ("routes", "Route lines"), ("labels", "Truck labels"),
                                    ("markers", "Load and tip markers"), ("roads", "Recorded roads"),
                                    ("label_bg", "Boxes behind labels")]):
            self.ov_check(g, k, t).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 18), pady=3)
        self.subhead(a, "Size and opacity")
        self.ov_slider(a, "Status panel size", "hud_scale", 0.5, 3.0, 0.05)
        self.ov_slider(a, "Status panel opacity", "hud_alpha", 0.2, 1.0, 0.05)
        self.ov_slider(a, "Route and label opacity", "world_alpha", 0.2, 1.0, 0.05)
        self.ov_slider(a, "Line width", "line_width", 1, 10, 1)
        self.ov_slider(a, "Label size", "label_scale", 0.5, 2.5, 0.05)
        self.ov_slider(a, "Mini map zoom", "minimap_zoom", 0.5, 4.0, 0.1)
        self.ov_slider(a, "Hide labels further than (m)", "label_max_m", 50, 1000, 10)
        self.ov_slider(a, "Max fps (0 = screen refresh)", "max_fps", 0, 240, 10)
        tk.Label(a, text="While the in-game menu is open, drag the status panel anywhere on the game screen, "
                         "and drag its bottom-right corner to resize it.", font=self.fm, fg=MUTED, bg=CARD,
                 wraplength=480, justify="left").pack(anchor="w", pady=(8, 0))
        b = self.card(cols, "COLOURS", "truck colours: click the dot on the Fleet tab", side="left")
        for k, t in [("color_load", "Load spot marker"), ("color_tip", "Tip spot marker"),
                     ("color_reverse", "Reversing part of a route"), ("color_text", "Text"),
                     ("color_accent", "Panel accent"), ("color_hud_bg", "Panel background"),
                     ("color_label_bg", "Label background"), ("color_roads", "Recorded roads")]:
            self.color_btn(b, t, k)
        r = tk.Frame(b, bg=CARD); r.pack(fill="x", pady=(12, 0))
        self.btn(r, "Reset panel position", self.reset_hud_pos).pack(side="left", padx=(0, 6))
        self.btn(r, "Reset all overlay settings", self.reset_overlay, "danger").pack(side="left")
        self.cam_label = tk.Label(b, text="", font=self.fm, fg=MUTED, bg=CARD, anchor="w", justify="left",
                                  wraplength=420)
        self.cam_label.pack(fill="x", pady=(12, 0))

    def reset_hud_pos(self):
        self.d.cfg["overlay"]["hud_pos"] = list(OVERLAY_DEFAULTS["hud_pos"])
        self.d.cfg["overlay"]["hud_scale"] = 1.0
        self.app.save_soon(); self.app.sync_panels()

    def reset_overlay(self):
        if not messagebox.askyesno("Truck Dispatcher", "Put every overlay setting and colour back to the default?",
                                   parent=self.winfo_toplevel()):
            return
        self.d.cfg["overlay"].clear()
        self.d.cfg["overlay"].update(json.loads(json.dumps(OVERLAY_DEFAULTS)))
        self.app.save_soon()
        self.app.sync_panels()

    # ------------------------------------------------------------------ Settings tab
    def build_settings(self, tab):
        cols = tk.Frame(tab, bg=BG); cols.pack(fill="both", expand=True)
        a = self.card(cols, "DRIVING", "applies from each truck's next trip", side="left")
        self.opt_slider(a, "Speed loaded (m/s)", "speed_loaded_mps", 2, 12, 0.5)
        self.opt_slider(a, "Speed empty (m/s)", "speed_empty_mps", 2, 14, 0.5)
        self.opt_slider(a, "Speed reversing (m/s)", "speed_reverse_mps", 1, 5, 0.1)
        self.opt_slider(a, "Acceleration (m/s²)", "accel_mps2", 0.3, 3, 0.1)
        self.opt_slider(a, "Cornering (higher = faster turns)", "lateral_mps2", 0.5, 4, 0.1)
        self.opt_slider(a, "Keep right of centre (m)", "keep_right_m", 0, 5, 0.25)
        self.opt_slider(a, "Gap to the truck ahead (m)", "follow_gap_m", 3, 30, 1)
        self.subhead(a, "Loading and tipping")
        self.opt_slider(a, "Wait this far from excavator (m)", "wait_clearance_m", 6, 40, 1)
        self.opt_slider(a, "Gap between waiting trucks (m)", "queue_gap_m", 8, 40, 1)
        self.opt_slider(a, "Reverse into spots (m)", "backin_m", 6, 30, 1)
        self.opt_slider(a, "Hopper limit (bank m³)", "hopper_max_m3", 10, 100, 1)
        self.opt_slider(a, "'Full' = this much of the bed", "full_fraction", 0.3, 1.0, 0.05)

        def set_auto(v):
            self.d.cfg["auto_send"] = bool(v); self.app.save_soon(); self.app.sync_panels()
        self.check(a, "Send the loaded truck by itself when it's full",
                   lambda: bool(self.d.cfg.get("auto_send")), set_auto).pack(anchor="w", pady=(8, 2))
        self.opt_slider(a, "Auto-send at (share of the bed)", "auto_send_fraction", 0.5, 1.0, 0.01)

        b = self.card(cols, "HOTKEYS", "only active while the game is in front", side="left")
        tk.Label(b, text="F1-F24, NUMPAD0-9, INSERT, HOME, END, PAGEUP, PAGEDOWN, PAUSE, SCROLLLOCK, A-Z, 0-9",
                 font=self.fm, fg=MUTED, bg=CARD, anchor="w", wraplength=420, justify="left").pack(fill="x")
        self.hk_vars = {}
        for key, text in [("menu", "Open the in-game menu"), ("send", "Send loaded truck"),
                          ("bucket", "Load spot at bucket"), ("pause", "Pause / resume"), ("record", "Record a road")]:
            row = tk.Frame(b, bg=CARD); row.pack(fill="x", pady=2)
            tk.Label(row, text=text, font=self.f, fg=FG, bg=CARD, width=24, anchor="w").pack(side="left")
            v = tk.StringVar(value=self.d.cfg["hotkeys"].get(key, ""))
            tk.Entry(row, textvariable=v, font=self.f, width=12, bg=ROW, fg=FG, insertbackground=FG,
                     relief="flat", highlightthickness=1, highlightbackground=TRACK,
                     highlightcolor=ACCENT).pack(side="left", ipady=3)
            self.hk_vars[key] = v
            self.syncers.append((v, lambda k=key: self.d.cfg["hotkeys"].get(k, "")))
        r = tk.Frame(b, bg=CARD); r.pack(fill="x", pady=(8, 0))
        self.btn(r, "Apply hotkeys", self.apply_hotkeys, "primary").pack(side="left")
        self.hk_msg = tk.Label(r, text="", font=self.f, fg=MUTED, bg=CARD, anchor="w")
        self.hk_msg.pack(side="left", padx=10)

        self.subhead(b, "Files")
        tk.Label(b, text="Settings, the load spot per save, roads and dispatcher.log (attach it to bug reports).",
                 font=self.fm, fg=MUTED, bg=CARD, anchor="w", wraplength=420, justify="left").pack(fill="x")
        r = tk.Frame(b, bg=CARD); r.pack(fill="x", pady=(6, 0))
        import dispatcher as D
        self.btn(r, "Open data folder", lambda: os.startfile(D.DATA)).pack(side="left", padx=(0, 6))
        self.btn(r, "Open log", lambda: os.path.exists(D.LOGFILE) and os.startfile(D.LOGFILE)).pack(side="left")

    def apply_hotkeys(self):
        new = {k: v.get().strip().upper().replace(" ", "") for k, v in self.hk_vars.items()}
        ok = self.app.set_hotkeys(new)
        self.hk_msg.configure(text="✔ Hotkeys applied" if ok else "✖ Not applied - see Roads & log",
                              fg=GOOD if ok else BAD)

    # ------------------------------------------------------------------ Roads & log tab
    def build_roads(self, tab):
        r = self.card(tab, "RECORDED ROADS", "optional: trucks prefer these, in the direction you drove them")
        self.roads = tk.Frame(r, bg=CARD); self.roads.pack(fill="x")
        self.road_help = tk.Label(r, text="", font=self.fm, fg=MUTED, bg=CARD, anchor="w", justify="left")
        self.road_help.pack(fill="x", pady=(6, 0))
        lc = self.card(tab, "LOG", "the full log is dispatcher.log (Settings tab, Open log)", expand=True)
        self.logbox = tk.Text(lc, font=("Consolas", self.fs - 1), fg=FG, bg=CARD, relief="flat", height=12,
                              wrap="word", highlightthickness=0, insertbackground=FG, selectbackground=TRACK)
        self.logbox.pack(fill="both", expand=True)
        self.logbox.configure(state="disabled")
        self._log_shown = None

    # ------------------------------------------------------------------ refresh
    def refresh(self, snap, cam_text=""):
        hk = snap.get("hotkeys", self.d.cfg["hotkeys"])
        if snap.get("connecting"):
            st, col = "…  Connecting to the game", ACCENT
        elif not snap.get("ready"):
            err = snap.get("error") or "start the game and load your save"
            st, col = "✖  Not connected - %s" % err[:90], BAD
        else:
            st, col = "✔  Connected · %s" % (snap.get("save") or "game"), GOOD
            if snap.get("paused"):
                st, col = "❚❚  PAUSED (%s resumes) · %s" % (hk["pause"], snap.get("save") or ""), ACCENT
        self.status.configure(text=st, fg=col)
        self._setup_steps(snap, hk)
        self._trucks(snap.get("trucks", []))
        self._ready = bool(snap.get("ready"))
        self._plants(snap.get("plants", []))
        self._roads(snap.get("roads", []), hk)
        L, exc = snap.get("load"), snap.get("exc")
        if L:
            self.loadinfo.configure(text="Load spot at (%.0f, %.0f)%s. It follows the excavator; %s moves it to "
                                         "where the bucket points." % (L[0], L[2], (" next to %s" % exc[3]) if exc
                                                                       else "", hk["bucket"]))
        else:
            self.loadinfo.configure(text="No load spot yet: park a truck under the bucket, load it and press %s "
                                         "(or point the bucket where trucks should stop and press %s)." %
                                         (hk["send"], hk["bucket"]))
        if snap.get("recording"):
            self.loadinfo.configure(text="●  RECORDING a road - drive it in the direction trucks should use it, "
                                         "then press %s" % hk["record"], fg=BAD)
        else:
            self.loadinfo.configure(fg=MUTED)
        lines = snap.get("log", [])
        self.lastlog.configure(text="\n".join(lines[-5:]))
        full = list(getattr(self.d, "lines", []))[-60:]
        if full != self._log_shown:
            self._log_shown = full
            self.logbox.configure(state="normal")
            self.logbox.delete("1.0", "end")
            self.logbox.insert("end", "\n".join(full))
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.cam_label.configure(text=cam_text)

    def _setup_steps(self, snap, hk):
        ready = bool(snap.get("ready"))
        fleet = [t for t in snap.get("trucks", []) if t["fleet"]]
        plants = [p for p in snap.get("plants", []) if p["found"]]
        steps = [
            (ready, "Game connected",
             snap.get("save") if ready else "start the game and load your save (borderless or windowed); "
                                            "it connects by itself"),
            (ready and bool(fleet), "Trucks ticked",
             ("%d in the fleet: %s" % (len(fleet), ", ".join(t["label"] for t in fleet[:5]))) if fleet else
             "tick the trucks the dispatcher should drive below"),
            (ready and bool(plants), "Wash plant found",
             ("%d found" % len(plants)) if plants else "set up a wash plant in the game, then Reconnect"),
            (ready and bool(snap.get("load")), "Load spot set",
             "done - load a truck and press %s (or let auto-send do it)" % hk["send"] if snap.get("load") else
             "park a truck under the bucket, load it, press %s" % hk["send"]),
        ]
        nxt = next((i for i, s in enumerate(steps) if not s[0]), None)
        done = nxt is None
        if done != self.setup_done:
            self.setup_done = done
            if done:
                self.setup.pack_forget()
                self.setup_note.configure(text="✔  all set: load a truck and press %s (or let auto-send do it)"
                                          % hk["send"], fg=GOOD)
                self.setup_head.pack_configure(pady=(10, 10))
            else:
                self.setup.pack(fill="both", expand=True, padx=12, pady=(0, 10))
                self.setup_note.configure(text="four steps, done once per save", fg=MUTED)
                self.setup_head.pack_configure(pady=(10, 6))
        if done:
            self.setup_note.configure(text="✔  all set: load a truck and press %s (or let auto-send do it)"
                                      % hk["send"])
        for i, ((ok, title, detail), (icon, tl, dl)) in enumerate(zip(steps, self.setup_rows)):
            icon.configure(text="✔" if ok else ("●" if i == nxt else "○"),
                           fg=GOOD if ok else (ACCENT if i == nxt else FILL))
            tl.configure(fg=FG if ok or i == nxt else MUTED, text=title)
            dl.configure(text=detail or "", fg=MUTED if ok else (FG if i == nxt else FILL))

    def _trucks(self, trucks):
        key = tuple(t["index"] for t in trucks)
        if key != self.truck_key:
            for w in self.trucks.winfo_children():
                w.destroy()
            self.truck_rows = []
            if not trucks:
                tk.Label(self.trucks, text="No trucks yet - they appear here once the game is connected.",
                         font=self.f, fg=MUTED, bg=CARD).pack(anchor="w")
            for t in trucks:
                row = tk.Frame(self.trucks, bg=ROW); row.pack(fill="x", pady=1)
                bar = tk.Frame(row, width=3, bg=ROW); bar.pack(side="left", fill="y")
                chk = Check(row, t["fleet"], lambda v, i=t["index"]: self.d.command("set_fleet", i, int(v)),
                            size=self.fs + 8, bg=ROW)
                chk.pack(side="left", padx=(8, 6), pady=3)
                dot = tk.Label(row, text="●", font=self.fb, bg=ROW, cursor="hand2")
                dot.pack(side="left")
                dot.bind("<Button-1>", lambda e, i=t["index"], b=dot: self.pick_truck_color(i, b))
                name = tk.Label(row, text=t["label"], font=self.fb, fg=FG, bg=ROW, width=12, anchor="w")
                name.pack(side="left", padx=(4, 0))
                b2 = self.btn(row, "Join", lambda i=t["index"]: self.d.command("rejoin", i))
                b2.configure(bg=TRACK); b2.bg = TRACK; b2.hover = FILL
                b2.pack(side="right", padx=(0, 6), pady=2)
                b1 = self.btn(row, "Send", lambda i=t["index"]: self.d.command("send_truck", i))
                b1.configure(bg=TRACK); b1.bg = TRACK; b1.hover = FILL
                b1.pack(side="right", padx=(0, 6), pady=2)
                load = tk.Label(row, text="", font=self.f, fg=FG, bg=ROW, width=13, anchor="e")
                load.pack(side="right", padx=(0, 10))
                state = tk.Label(row, text="", font=self.f, fg=FG, bg=ROW, anchor="w")
                state.pack(side="left", fill="x", expand=True, padx=(6, 0))
                self.truck_rows.append((row, bar, chk, dot, name, state, load, b1, b2))
            self.truck_key = key
        tc = self.d.cfg["overlay"].get("truck_colors", {})
        for (row, bar, chk, dot, name, state, load, b1, b2), t in zip(self.truck_rows, trucks):
            chk.set(t["fleet"])
            col = tc.get(str(t["index"]), t["color"])
            dot.configure(fg=col if t["fleet"] else FILL)
            bar.configure(bg=col if t["fleet"] else ROW)
            name.configure(fg=FG if t["fleet"] else MUTED)
            text = t["text"] + (("  ·  " + t["reason"]) if t["reason"] else "")
            state.configure(text=text[:90], fg=STATE_COL.get(t["state"], FG))
            load.configure(text="%s / %.0f m³" % (fmt(t["payload"]), t["cap"]))
            b1.set_enabled(t["fleet"] and t["payload"] >= 1 and t["state"] in ("AT_LOAD", "IDLE", "MANUAL"))
            b2.set_enabled(t["state"] in ("IDLE", "MANUAL") or not t["fleet"])
            b2.configure(text="Join" if t["fleet"] else "Add")

    def pick_truck_color(self, index, button):
        tc = self.d.cfg["overlay"].setdefault("truck_colors", {})
        cur = tc.get(str(index), button.cget("fg"))
        c = colorchooser.askcolor(color=cur, title="Colour for #%d" % index, parent=self.winfo_toplevel())[1]
        if c:
            tc[str(index)] = c
            self.app.save_soon()

    def _plants(self, plants):
        key = (getattr(self, "_ready", False),) + tuple(p["n"] for p in plants)
        if key != self.plant_key:
            for w in self.plants.winfo_children():
                w.destroy()
            self.plant_rows = []
            if not plants:
                tk.Label(self.plants, text="No wash plant in this save yet. Set one up in the game, then press "
                                           "Reconnect." if getattr(self, "_ready", False) else
                         "Wash plants appear here once the game is connected.",
                         font=self.f, fg=MUTED, bg=CARD).pack(anchor="w")
            for p in plants:
                row = tk.Frame(self.plants, bg=ROW); row.pack(fill="x", pady=1)
                chk = Check(row, p["enabled"], lambda v, n=p["n"]: self.d.command("set_plant", n, int(v)),
                            size=self.fs + 8, bg=ROW)
                chk.pack(side="left", padx=(11, 6), pady=5)
                tk.Label(row, text=p["name"], font=self.fb, fg=FG, bg=ROW, width=9, anchor="w").pack(side="left")
                bar = tk.Canvas(row, width=200, height=12, bg=TRACK, highlightthickness=0)
                bar.pack(side="left", padx=(4, 10))
                info = tk.Label(row, text="", font=self.f, fg=FG, bg=ROW, anchor="w")
                info.pack(side="left", fill="x", expand=True)
                self.plant_rows.append((chk, bar, info))
            self.plant_key = key
        for (chk, bar, info), p in zip(self.plant_rows, plants):
            chk.set(p["enabled"])
            frac = 0.0 if p["hopper"] is None else max(0.0, min(1.0, p["hopper"] / max(1.0, p["max"])))
            bar.delete("all")
            bar.create_rectangle(0, 0, 200 * frac, 12, fill=BAD if frac > 0.9 else ACCENT, width=0)
            if not p["found"]:
                info.configure(text="not found in game memory - wait for an autosave, then Reconnect", fg=BAD)
            else:
                info.configure(text="hopper %s / %.0f m³  ·  washes %.1f m³/min%s" % (
                    fmt(p["hopper"], 0), p["max"], p["rate"], "" if p["enabled"] else "  ·  not used"),
                    fg=FG if p["enabled"] else MUTED)

    def _roads(self, roads, hk):
        key = tuple((r[0], r[1], round(r[2])) for r in roads)
        self.road_help.configure(text="To add one: press %s, drive the road in any machine in the direction "
                                      "trucks should use it, press %s again." % (hk["record"], hk["record"]))
        if key == self.road_key:
            return
        for w in self.roads.winfo_children():
            w.destroy()
        if not roads:
            tk.Label(self.roads, text="None. Trucks plan their own way over the terrain.", font=self.f, fg=MUTED,
                     bg=CARD).pack(anchor="w")
        for r, (name, one, length, _) in enumerate(roads):
            row = tk.Frame(self.roads, bg=ROW); row.pack(fill="x", pady=1)
            tk.Label(row, text="%s   (%.0f m)" % (name, length), font=self.f, fg=FG, bg=ROW, anchor="w").pack(
                side="left", padx=10, pady=4)
            d = self.btn(row, "Delete", lambda n=r, nm=name: self.delete_road(n, nm), "danger")
            d.pack(side="right", padx=(0, 6), pady=3)
            o = self.btn(row, "one-way" if one else "two-way", lambda n=r: self.d.command("toggle_road_oneway", n))
            o.configure(bg=TRACK); o.bg = TRACK; o.hover = FILL
            o.pack(side="right", padx=(0, 6), pady=3)
        self.road_key = key

    def delete_road(self, n, name):
        if messagebox.askyesno("Truck Dispatcher", "Delete the road '%s'?" % name, parent=self.winfo_toplevel()):
            self.d.command("delete_road", n)


class Menu:
    """The panel as a borderless window over the game; the menu hotkey or Esc toggles it."""

    def __init__(self, root, d, app, overlay):
        self.root, self.d, self.ov = root, d, overlay
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", 0.97)
        self.top.configure(bg=TRACK)
        inner = tk.Frame(self.top, bg=BG)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        big = root.winfo_screenheight() >= 1300
        self.panel = Panel(inner, d, app, big=big, on_close=self.hide)
        self.panel.pack(fill="both", expand=True, padx=4, pady=4)
        self.top.bind("<Escape>", lambda e: self.hide())
        self.top.withdraw()
        self.visible = False
        self.top.update_idletasks()
        self.hwnd = int(self.top.wm_frame(), 16)
        overlay.menu_hwnds.add(self.hwnd)

    def toggle(self):
        self.hide() if self.visible else self.show()

    def show(self):
        self.panel.sync()
        game = self.ov.game
        rect = gamecam.client_rect(game) if game and not gamecam.is_minimized(game) else None
        if not rect or rect[2] < 400 or rect[3] < 300:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            rect = (0, 0, sw, sh)
        x, y, W, H = rect
        self.top.update_idletasks()
        want_w = 1240 if self.panel.big else 1060
        want_h = 900 if self.panel.big else 760
        w = max(320, min(want_w, W - 40))
        h = max(240, min(max(want_h, self.top.winfo_reqheight()), H - 40))
        # leave the left side free so the status panel can be dragged around while the menu is open
        self.top.geometry("%dx%d+%d+%d" % (w, h, x + max(20, W - w - 30), y + max(20, (H - h) // 2)))
        self.top.deiconify()
        self.top.lift()
        self.top.attributes("-topmost", True)
        self.visible = True
        self.ov.edit = True
        self.top.after(30, lambda: (gamecam.focus(self.hwnd), self.top.focus_force()))

    def hide(self):
        if not self.visible:
            return
        self.top.withdraw()
        self.visible = False
        self.ov.edit = False
        if self.ov.game:
            gamecam.focus(self.ov.game)

    def refresh(self, snap):
        if self.visible:
            self.panel.refresh(snap, self.ov.camera_text())
