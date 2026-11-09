"""Native overlay drawn with Win32 GDI on layered, click-through windows, in its own thread.

Two windows: WORLD (covers the game's client area: route lines, rings, labels; redrawn every display
frame, synced with DwmFlush) and HUD (status panel + mini map; its own size and opacity; can be
dragged and resized while the in-game menu is open). Black (0, 0, 0) is the transparent colour.
"""
import ctypes, ctypes.wintypes as wt, math, threading, time

import numpy as np

import gamecam

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
kernel32 = ctypes.WinDLL("kernel32")

HANDLE, HWND, HDC, HGDIOBJ = ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", HANDLE),
                ("hIcon", HANDLE), ("hCursor", HANDLE), ("hbrBackground", HANDLE), ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p), ("hIconSm", HANDLE)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class LOGBRUSH(ctypes.Structure):
    _fields_ = [("lbStyle", ctypes.c_uint), ("lbColor", ctypes.c_uint32), ("lbHatch", ctypes.c_size_t)]


def _sig(dll, name, res, *args):
    f = getattr(dll, name)
    f.restype, f.argtypes = res, list(args)
    return f


RegisterClassExW = _sig(user32, "RegisterClassExW", ctypes.c_uint16, ctypes.POINTER(WNDCLASSEXW))
CreateWindowExW = _sig(user32, "CreateWindowExW", HWND, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p,
                       ctypes.c_uint32, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, HWND, HANDLE, HANDLE,
                       ctypes.c_void_p)
DefWindowProcW = _sig(user32, "DefWindowProcW", LRESULT, HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
ShowWindow = _sig(user32, "ShowWindow", ctypes.c_bool, HWND, ctypes.c_int)
UpdateLayeredWindow = _sig(user32, "UpdateLayeredWindow", ctypes.c_bool, HWND, HDC, ctypes.POINTER(POINT),
                           ctypes.POINTER(SIZE), HDC, ctypes.POINTER(POINT), ctypes.c_uint32,
                           ctypes.POINTER(BLENDFUNCTION), ctypes.c_uint32)
PeekMessageW = _sig(user32, "PeekMessageW", ctypes.c_bool, ctypes.POINTER(wt.MSG), HWND, ctypes.c_uint,
                    ctypes.c_uint, ctypes.c_uint)
TranslateMessage = _sig(user32, "TranslateMessage", ctypes.c_bool, ctypes.POINTER(wt.MSG))
DispatchMessageW = _sig(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wt.MSG))
GetDC = _sig(user32, "GetDC", HDC, HWND)
ReleaseDC = _sig(user32, "ReleaseDC", ctypes.c_int, HWND, HDC)
SetWindowPos = _sig(user32, "SetWindowPos", ctypes.c_bool, HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_uint)
GetWindowLongPtrW = _sig(user32, "GetWindowLongPtrW", ctypes.c_ssize_t, HWND, ctypes.c_int)
SetWindowLongPtrW = _sig(user32, "SetWindowLongPtrW", ctypes.c_ssize_t, HWND, ctypes.c_int, ctypes.c_ssize_t)
SetCapture = _sig(user32, "SetCapture", HWND, HWND)
ReleaseCapture = _sig(user32, "ReleaseCapture", ctypes.c_bool)
GetCursorPos = _sig(user32, "GetCursorPos", ctypes.c_bool, ctypes.POINTER(POINT))
LoadCursorW = _sig(user32, "LoadCursorW", HANDLE, HANDLE, ctypes.c_void_p)
SetCursor = _sig(user32, "SetCursor", HANDLE, HANDLE)
FillRect = _sig(user32, "FillRect", ctypes.c_int, HDC, ctypes.POINTER(RECT), HANDLE)
GetModuleHandleW = _sig(kernel32, "GetModuleHandleW", HANDLE, ctypes.c_wchar_p)

CreateCompatibleDC = _sig(gdi32, "CreateCompatibleDC", HDC, HDC)
CreateDIBSection = _sig(gdi32, "CreateDIBSection", HANDLE, HDC, ctypes.POINTER(BITMAPINFOHEADER), ctypes.c_uint,
                        ctypes.POINTER(ctypes.c_void_p), HANDLE, ctypes.c_uint32)
SelectObject = _sig(gdi32, "SelectObject", HGDIOBJ, HDC, HGDIOBJ)
DeleteObject = _sig(gdi32, "DeleteObject", ctypes.c_bool, HGDIOBJ)
DeleteDC = _sig(gdi32, "DeleteDC", ctypes.c_bool, HDC)
CreatePen = _sig(gdi32, "CreatePen", HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_uint32)
ExtCreatePen = _sig(gdi32, "ExtCreatePen", HANDLE, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(LOGBRUSH),
                    ctypes.c_uint32, ctypes.c_void_p)
CreateSolidBrush = _sig(gdi32, "CreateSolidBrush", HANDLE, ctypes.c_uint32)
GetStockObject = _sig(gdi32, "GetStockObject", HGDIOBJ, ctypes.c_int)
Polyline = _sig(gdi32, "Polyline", ctypes.c_bool, HDC, ctypes.POINTER(POINT), ctypes.c_int)
Rectangle = _sig(gdi32, "Rectangle", ctypes.c_bool, HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
Ellipse = _sig(gdi32, "Ellipse", ctypes.c_bool, HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
CreateFontW = _sig(gdi32, "CreateFontW", HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                   ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                   ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_wchar_p)
SetTextColor = _sig(gdi32, "SetTextColor", ctypes.c_uint32, HDC, ctypes.c_uint32)
SetBkMode = _sig(gdi32, "SetBkMode", ctypes.c_int, HDC, ctypes.c_int)
TextOutW = _sig(gdi32, "TextOutW", ctypes.c_bool, HDC, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p, ctypes.c_int)
GetTextExtentPoint32W = _sig(gdi32, "GetTextExtentPoint32W", ctypes.c_bool, HDC, ctypes.c_wchar_p, ctypes.c_int,
                             ctypes.POINTER(SIZE))
DwmFlush = _sig(dwmapi, "DwmFlush", ctypes.c_long)

WS_POPUP = 0x80000000
WS_EX = 0x80000 | 0x80 | 0x8 | 0x08000000          # layered, toolwindow, topmost, noactivate
WS_EX_TRANSPARENT = 0x20
GWL_EXSTYLE = -20
ULW_COLORKEY, ULW_ALPHA = 1, 2
NULL_BRUSH, NULL_PEN = 5, 8
WM_NCHITTEST, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE, WM_SETCURSOR = 0x84, 0x201, 0x202, 0x200, 0x20
HTCLIENT = 1
IDC_SIZEALL, IDC_SIZENWSE = 32646, 32642


def rgb(c):
    """'#rrggbb' -> COLORREF (0x00bbggrr); pure black becomes near-black (black is transparent)."""
    c = c.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    if r == g == b == 0:
        r = g = b = 8
    return r | (g << 8) | (b << 16)


class Surface:
    """A layered window plus a 32-bit DIB to draw into."""

    def __init__(self, cls, name):
        self.hwnd = CreateWindowExW(WS_EX | WS_EX_TRANSPARENT, cls, name, WS_POPUP, 0, 0, 16, 16, None, None,
                                    GetModuleHandleW(None), None)
        self.dc = CreateCompatibleDC(None)
        self.bmp = None
        self.bits = ctypes.c_void_p()
        self.w = self.h = 0
        self.x = self.y = 0
        self.visible = False
        self.click_through = True
        SetBkMode(self.dc, 1)                     # transparent text background
        self.pens, self.brushes, self.fonts = {}, {}, {}

    def resize(self, w, h):
        w, h = max(1, int(w)), max(1, int(h))
        if (w, h) == (self.w, self.h):
            return
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bmp = CreateDIBSection(self.dc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        if not bmp or not bits.value:
            raise MemoryError("could not create a %dx%d overlay bitmap" % (w, h))
        SelectObject(self.dc, bmp)
        if self.bmp:
            DeleteObject(self.bmp)
        self.bmp, self.bits, self.w, self.h = bmp, bits, w, h

    def clear(self):
        if self.bits.value:
            ctypes.memset(self.bits, 0, self.w * self.h * 4)

    def trim_caches(self, limit=160):
        """Pens, brushes and fonts are cached; drop them all if settings changes piled up many."""
        if len(self.pens) + len(self.brushes) + len(self.fonts) > limit:
            SelectObject(self.dc, GetStockObject(NULL_PEN))
            SelectObject(self.dc, GetStockObject(NULL_BRUSH))
            SelectObject(self.dc, GetStockObject(17))         # DEFAULT_GUI_FONT
            for obj in list(self.pens.values()) + list(self.brushes.values()) + list(self.fonts.values()):
                DeleteObject(obj)
            self.pens, self.brushes, self.fonts = {}, {}, {}

    def present(self, x, y, alpha):
        self.x, self.y = int(x), int(y)
        pos, size, src = POINT(self.x, self.y), SIZE(self.w, self.h), POINT(0, 0)
        bl = BLENDFUNCTION(0, 0, max(10, min(255, int(alpha * 255))), 0)
        UpdateLayeredWindow(self.hwnd, None, ctypes.byref(pos), ctypes.byref(size), self.dc, ctypes.byref(src), 0,
                            ctypes.byref(bl), ULW_COLORKEY | ULW_ALPHA)
        if not self.visible:
            ShowWindow(self.hwnd, 4)              # SW_SHOWNOACTIVATE
            self.visible = True
        SetWindowPos(self.hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10 | 0x200)

    def hide(self):
        if self.visible:
            ShowWindow(self.hwnd, 0)
            self.visible = False

    def set_click_through(self, on):
        if on == self.click_through:
            return
        ex = GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TRANSPARENT) if on else (ex & ~WS_EX_TRANSPARENT)
        SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex)
        self.click_through = on

    # ---- drawing helpers (all coordinates in pixels of this surface)
    def pen(self, color, width=1, dash=False):
        key = (color, int(width), dash)
        p = self.pens.get(key)
        if p is None:
            lb = LOGBRUSH(0, rgb(color), 0)
            style = 0x10000 | (1 if dash else 0) | 0x0 | 0x2000   # geometric, dash/solid, round caps, round join
            p = ExtCreatePen(style, max(1, int(width)), ctypes.byref(lb), 0, None)
            self.pens[key] = p
        return p

    def brush(self, color):
        b = self.brushes.get(color)
        if b is None:
            b = CreateSolidBrush(rgb(color))
            self.brushes[color] = b
        return b

    def font(self, px, bold=False):
        key = (int(px), bold)
        f = self.fonts.get(key)
        if f is None:
            f = CreateFontW(-int(px), 0, 0, 0, 700 if bold else 400, 0, 0, 0, 1, 0, 0, 4, 0, "Segoe UI")
            self.fonts[key] = f
        return f

    def polyline(self, xs, ys, color, width, dash=False):
        n = len(xs)
        if n < 2:
            return
        pts = (POINT * n)(*[POINT(int(max(-30000, min(30000, x))), int(max(-30000, min(30000, y))))
                            for x, y in zip(xs, ys)])
        SelectObject(self.dc, self.pen(color, width, dash))
        Polyline(self.dc, pts, n)

    def rect(self, x0, y0, x1, y1, fill=None, outline=None, width=1):
        if fill:
            r = RECT(int(x0), int(y0), int(x1), int(y1))
            FillRect(self.dc, ctypes.byref(r), self.brush(fill))
        if outline:
            SelectObject(self.dc, self.pen(outline, width))
            SelectObject(self.dc, GetStockObject(NULL_BRUSH))
            Rectangle(self.dc, int(x0), int(y0), int(x1), int(y1))

    def ellipse(self, x0, y0, x1, y1, fill=None, outline=None, width=1):
        SelectObject(self.dc, self.pen(outline, width) if outline else GetStockObject(NULL_PEN))
        SelectObject(self.dc, self.brush(fill) if fill else GetStockObject(NULL_BRUSH))
        Ellipse(self.dc, int(x0), int(y0), int(x1), int(y1))

    def text_size(self, s, px, bold=False):
        SelectObject(self.dc, self.font(px, bold))
        sz = SIZE()
        GetTextExtentPoint32W(self.dc, s, len(s), ctypes.byref(sz))
        return sz.cx, sz.cy

    def text(self, x, y, s, color, px, bold=False, anchor="nw"):
        SelectObject(self.dc, self.font(px, bold))
        w, h = self.text_size(s, px, bold)
        if "e" in anchor:
            x -= w
        elif anchor in ("n", "s", "center"):
            x -= w / 2
        if "s" in anchor:
            y -= h
        elif anchor in ("w", "e", "center"):
            y -= h / 2
        SetTextColor(self.dc, rgb(color))
        TextOutW(self.dc, int(x), int(y), s, len(s))
        return w, h


# ====================================================================== the overlay thread

class NativeOverlay:
    def __init__(self, d):
        self.d = d
        self.edit = False                 # menu open: HUD can be dragged/resized
        self.menu_hwnds = set()
        self.game = None
        self.tracker = None
        self.fps = 0.0
        self.state = "waiting for the game"
        self._stop = False
        self._drag = None
        self.hud_rect = (0, 0, 0, 0)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def camera_text(self):
        t = self.tracker.state if self.tracker else "waiting for the game"
        return "Overlay: %s, %.0f fps" % (t, self.fps)

    def stop(self):
        self._stop = True

    # ---- window procedure: HUD drag / resize in edit mode
    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if self.edit and hwnd == self.hud.hwnd:
                if msg == WM_NCHITTEST:
                    return HTCLIENT
                if msg == WM_SETCURSOR:
                    p = POINT(); GetCursorPos(ctypes.byref(p))
                    x0, y0, w, h = self.hud_rect
                    corner = p.x > x0 + w - 28 and p.y > y0 + h - 28
                    SetCursor(LoadCursorW(None, ctypes.c_void_p(IDC_SIZENWSE if corner else IDC_SIZEALL)))
                    return 1
                if msg == WM_LBUTTONDOWN:
                    p = POINT(); GetCursorPos(ctypes.byref(p))
                    x0, y0, w, h = self.hud_rect
                    mode = "size" if (p.x > x0 + w - 28 and p.y > y0 + h - 28) else "move"
                    self._drag = (mode, p.x, p.y, x0, y0, w, self.d.cfg["overlay"].get("hud_scale", 1.0))
                    SetCapture(hwnd)
                    return 0
                if msg == WM_LBUTTONUP and self._drag:
                    self._drag = None
                    ReleaseCapture()
                    self._save_hud()
                    return 0
        except Exception:
            pass
        return DefWindowProcW(hwnd, msg, wp, lp)

    def _drag_tick(self, rect):
        if not self._drag:
            return
        mode, px, py, x0, y0, w0, s0 = self._drag
        p = POINT(); GetCursorPos(ctypes.byref(p))
        ov = self.d.cfg["overlay"]
        gx, gy, gw, gh = rect
        if mode == "move":
            nx, ny = x0 + p.x - px, y0 + p.y - py
            ov["hud_pos"] = [round((nx - gx) / max(1, gw), 4), round((ny - gy) / max(1, gh), 4)]
        else:
            ov["hud_scale"] = round(max(0.5, min(3.0, s0 * (w0 + p.x - px) / max(1, w0))), 3)

    def _save_hud(self):
        try:
            import dispatcher
            dispatcher.save_config(self.d.cfg)
        except Exception:
            pass

    # ---- main loop
    def _run(self):
        self._proc = WNDPROC(self._wndproc)
        wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), 0, self._proc, 0, 0, GetModuleHandleW(None), None, None,
                         None, None, "TruckDispatcherOverlay", None)
        RegisterClassExW(ctypes.byref(wc))
        self.world = Surface("TruckDispatcherOverlay", "Truck Dispatcher overlay")
        self.hud = Surface("TruckDispatcherOverlay", "Truck Dispatcher status")
        msg = wt.MSG()
        frames, t_fps = 0, time.perf_counter()
        errors = 0
        while not self._stop:
            try:
                while PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    TranslateMessage(ctypes.byref(msg))
                    DispatchMessageW(ctypes.byref(msg))
                if self._frame():
                    frames += 1
                errors = 0
            except Exception as e:
                errors += 1
                self.state = "overlay error: %s" % e
                if errors == 1:
                    self.d.log_error("overlay", e)
                try:
                    self.world.hide(); self.hud.hide()
                except Exception:
                    pass
                time.sleep(min(2.0, 0.2 * errors))
            now = time.perf_counter()
            if now - t_fps > 1.0:
                self.fps, frames, t_fps = frames / (now - t_fps), 0, now
                self.world.trim_caches(); self.hud.trim_caches()
            try:
                cap = float(self.d.cfg["overlay"].get("max_fps", 0) or 0)
            except (TypeError, ValueError):
                cap = 0.0
            t0 = time.perf_counter()
            if DwmFlush() != 0:
                time.sleep(0.008)
            if cap > 0:
                left = 1.0 / cap - (time.perf_counter() - t0)
                if left > 0:
                    time.sleep(left)
        try:
            self.world.hide(); self.hud.hide()
        except Exception:
            pass

    def _frame(self):
        d, ov = self.d, self.d.cfg["overlay"]
        now = time.time()
        if not (d.ready and d.proc):
            self.world.hide(); self.hud.hide()
            time.sleep(0.1)
            return False
        if self.game is None or now - getattr(self, "_gt", 0) > 3:
            self._gt = now
            self.game = gamecam.game_window(d.proc.pid)
        if self.tracker is None or self.tracker.p is not d.proc:
            self.tracker = gamecam.CameraTracker(d.proc, log=d.log)
        fg = gamecam.foreground()
        own = {self.world.hwnd, self.hud.hwnd}
        want = self.game and (fg == self.game or fg in self.menu_hwnds or fg in own) and \
            not gamecam.is_minimized(self.game)
        rect = gamecam.client_rect(self.game) if want else None
        if not rect or rect[2] < 50:
            self.world.hide(); self.hud.hide()
            time.sleep(0.05)
            return False
        self.hud.set_click_through(not self.edit)
        self._drag_tick(rect)
        snap = d.snap
        cam = self.tracker.update(rect[2] / max(1, rect[3]))
        self.state = self.tracker.state
        # ---- world layer
        world_on = any(ov.get(k) for k in ("routes", "labels", "markers", "roads"))
        if world_on and cam is not None:
            self.world.resize(rect[2], rect[3])
            self.world.clear()
            self.draw_world(self.world, cam, snap, rect[2], rect[3], ov)
            self.world.present(rect[0], rect[1], ov.get("world_alpha", 0.95))
        else:
            self.world.hide()
        # ---- HUD layer (10 Hz is plenty, faster while dragging)
        if ov.get("hud", True) or self.edit:
            if now - getattr(self, "_ht", 0) > (0.03 if self._drag else 0.1):
                self._ht = now
                self.draw_hud(snap, rect, ov)
        else:
            self.hud.hide()
        return True

    # ---- world drawing
    def _project(self, cam, P, W, H):
        sx, sy, vis = cam.project(P, W, H)
        return sx, sy, vis

    def _poly(self, s, cam, P, W, H, color, width, dash=False):
        if len(P) < 2:
            return
        sx, sy, vis = cam.project(P, W, H)
        ok = vis & (sx > -3 * W) & (sx < 4 * W) & (sy > -3 * H) & (sy < 4 * H)
        start = None
        for i in range(len(ok) + 1):
            if i < len(ok) and ok[i]:
                if start is None:
                    start = i
            elif start is not None:
                if i - start >= 2:
                    s.polyline(sx[start:i], sy[start:i], color, width, dash)
                start = None

    def _ring(self, x, y, z, r, n=24):
        a = np.linspace(0, 2 * math.pi, n + 1)
        return np.c_[x + r * np.sin(a), np.full(n + 1, y + 0.25), z + r * np.cos(a)]

    def _label(self, s, x, y, lines, ov, border):
        """Lines [(text, colour, bold)] in a box whose bottom centre is (x, y)."""
        px = ov.get("label_px", 15) * ov.get("label_scale", 1.0) * getattr(self, "_res", 1.0)
        sizes = [s.text_size(t, px * (1.0 if b else 0.86), b) for t, c, b in lines]
        w = max(a for a, _ in sizes) + 14
        h = sum(b for _, b in sizes) + 6
        x0, y0 = x - w / 2, y - h
        if ov.get("label_bg", True):
            s.rect(x0, y0, x0 + w, y, fill=ov.get("color_label_bg", "#15171c"), outline=border, width=1)
        yy = y0 + 3
        for (t, c, b), (_, hh) in zip(lines, sizes):
            s.text(x, yy, t, c, px * (1.0 if b else 0.86), b, anchor="n")
            yy += hh

    def draw_world(self, s, cam, snap, W, H, ov):
        self._res = max(0.7, H / 1080.0)
        lw = max(1, int(round(ov.get("line_width", 3) * self._res)))
        trucks = snap.get("trucks", [])
        live = {}
        for t in trucks:                                  # positions read now, not from the 20 Hz snapshot
            if t["fleet"]:
                try:
                    if not self.d.ready:
                        raise RuntimeError
                    x, y, z, h, _, _ = self.d.machine(t["index"]).pose()
                    live[t["index"]] = (x, y, z)
                except Exception:
                    live[t["index"]] = t["pos"]
        if ov.get("roads"):
            for name, one, length, pts in snap.get("roads", []):
                P = np.asarray(pts, float)
                if len(P) >= 2:
                    ys = self._road_y(name, P)
                    self._poly(s, cam, np.c_[P[:, 0], ys + 0.3, P[:, 1]], W, H, ov.get("color_roads", "#8a8fa0"),
                               max(1, lw - 1), dash=True)
        if ov.get("routes", True):
            for t in trucks:
                path = t.get("path") or []
                if len(path) < 2:
                    continue
                cur = live.get(t["index"], t["pos"])
                fwd = [(cur[0], cur[1] + 0.4, cur[2])] if path[0][3] == 1 else []
                fwd += [(x, y + 0.4, z) for x, y, z, g in path if g == 1]
                rev = [(x, y + 0.4, z) for x, y, z, g in path if g == -1]
                if len(fwd) >= 2:
                    self._poly(s, cam, np.array(fwd), W, H, self._truck_color(t), lw)
                if len(rev) >= 2:
                    self._poly(s, cam, np.array(rev), W, H, ov.get("color_reverse", "#ffffff"), lw, dash=True)
        if ov.get("markers", True):
            L = snap.get("load")
            if L:
                col = ov.get("color_load", "#5bd68a")
                self._poly(s, cam, self._ring(L[0], L[1], L[2], 2.5), W, H, col, lw)
                sx, sy, vis = cam.project([(L[0], L[1] + 1.2, L[2])], W, H)
                if vis[0]:
                    s.text(sx[0], sy[0], "LOAD", col, ov.get("label_px", 15) * ov.get("label_scale", 1.0) * 0.86 *
                           self._res, True, anchor="s")
            for p in snap.get("plants", []):
                if not p["enabled"]:
                    continue
                col = ov.get("color_tip", "#f2a93b")
                tx, tz, th = p["tip"]
                ty = self._road_y(("tip", p["n"]), np.array([[tx, tz]]))[0]
                self._poly(s, cam, self._ring(tx, ty, tz, 2.5), W, H, col, lw)
                px_, py_, pz_ = p["pos"]
                sx, sy, vis = cam.project([(px_, py_ + 7.0, pz_)], W, H)
                if vis[0] and 0 <= sx[0] <= W and 0 <= sy[0] <= H:
                    hop = p["hopper"]
                    full = hop is not None and hop > 0.9 * p["max"]
                    self._label(s, sx[0], sy[0], [(p["name"], col, True),
                                                  ("hopper %s / %.0f m³" % ("-" if hop is None else "%.0f" % hop,
                                                                           p["max"]),
                                                   "#f06a6a" if full else ov.get("color_text", "#eceef2"), False)],
                                ov, col)
        if ov.get("labels", True):
            for t in trucks:
                if not t["fleet"]:
                    continue
                x, y, z = live.get(t["index"], t["pos"])
                sx, sy, vis = cam.project([(x, y + 4.8, z)], W, H)
                if not vis[0] or not (-80 <= sx[0] <= W + 80 and -80 <= sy[0] <= H + 80):
                    continue
                if float(np.linalg.norm(np.array((x, y, z)) - cam.c)) > ov.get("label_max_m", 500):
                    continue
                col = self._truck_color(t)
                lines = [("%s  %s" % (t["label"], t["text"]), col, True),
                         ("%.1f / %.0f m³%s" % (t["payload"], t["cap"], ("  - " + t["reason"]) if t["reason"] else ""),
                          ov.get("color_text", "#eceef2"), False)]
                self._label(s, sx[0], sy[0], lines, ov, col)

    def _truck_color(self, t):
        return self.d.cfg["overlay"].get("truck_colors", {}).get(str(t["index"]), t["color"])

    def _road_y(self, key, P):
        cache = getattr(self, "_ycache", None)
        if cache is None:
            cache = self._ycache = {}
        k = (key, len(P))
        if k not in cache or time.time() - cache[k][0] > 10:
            try:
                g = self.d.ground
                pt = g.patch(P[:, 0].min() - 2, P[:, 1].min() - 2, P[:, 0].max() + 2, P[:, 1].max() + 2)
                ys = np.array([g._sample(pt, x, z) for x, z in P])
            except Exception:
                ys = np.zeros(len(P))
            cache[k] = (time.time(), ys)
        return cache[k][1]

    # ---- HUD
    def draw_hud(self, snap, rect, ov):
        sc = ov.get("hud_scale", 1.0) * max(0.7, rect[3] / 1080.0)
        bg, fg = ov.get("color_hud_bg", "#15171c"), ov.get("color_text", "#eceef2")
        acc, muted = ov.get("color_accent", "#f2a93b"), "#8b91a0"
        hk = snap.get("hotkeys", self.d.cfg["hotkeys"])
        trucks = [t for t in snap.get("trucks", []) if t["fleet"]]
        plants = snap.get("plants", [])
        pad, lh = int(10 * sc), int(21 * sc)
        fpx, spx = 16 * sc, 14 * sc
        w = int(420 * sc)
        mm = int(230 * sc) if ov.get("minimap", True) else 0
        rows = 2 + max(1, len(trucks)) + len(plants) + (1 if self.edit else 0)
        h = pad * 2 + rows * lh + (mm + pad if mm else 0) + 4
        s = self.hud
        s.resize(w, h)
        s.clear()
        s.rect(0, 0, w, h, fill=bg, outline="#2e333e")
        s.rect(0, 0, max(2, int(3 * sc)), h, fill=acc)
        y = pad
        tw, _ = s.text(pad, y, "TRUCK DISPATCHER", acc, fpx, True)
        s.text(pad + tw + pad * 0.6, y + fpx * 0.12, "v" + getattr(self.d, "version", ""), muted, spx * 0.9)
        tags = []
        if not snap.get("ready"):
            tags.append(("✖ not connected", "#f06a6a"))
        if snap.get("paused"):
            tags.append(("PAUSED", "#f06a6a"))
        if snap.get("auto"):
            tags.append(("AUTO", "#5bd68a"))
        if snap.get("recording"):
            tags.append(("REC", "#f06a6a"))
        xx = w - pad
        for text, col in reversed(tags):
            tw, _ = s.text(xx, y, text, col, fpx, True, anchor="ne")
            xx -= tw + pad
        y += lh + 4
        from overlay import STATE_COL
        for t in trucks:
            col = self._truck_color(t)
            s.ellipse(pad, y + lh * 0.25, pad + lh * 0.5, y + lh * 0.75, fill=col)
            txt = "%s  %s" % (t["name"], t["reason"] or t["text"])
            s.text(pad + lh * 0.8, y, txt[:40], STATE_COL.get(t["state"], fg), spx)
            s.text(w - pad, y, "%.0f/%.0f m³" % (t["payload"], t["cap"]), fg, spx, anchor="ne")
            y += lh
        if not trucks:
            s.text(pad, y, "no trucks in the fleet (%s menu)" % hk["menu"], muted, spx)
            y += lh
        for p in plants:
            frac = 0.0 if p["hopper"] is None else min(1.0, p["hopper"] / max(1.0, p["max"]))
            s.text(pad, y, p["name"] + ("" if p["enabled"] else " (off)"), fg, spx)
            bx0, bx1 = int(w * 0.30), w - pad - int(75 * sc)
            s.rect(bx0, y + lh * 0.28, bx1, y + lh * 0.72, fill="#2e333e")
            s.rect(bx0, y + lh * 0.28, bx0 + (bx1 - bx0) * frac, y + lh * 0.72, fill="#f06a6a" if frac > 0.9 else acc)
            s.text(w - pad, y, "%s m³" % ("-" if p["hopper"] is None else "%.0f" % p["hopper"]), fg, spx, anchor="ne")
            y += lh
        if mm:
            y += pad // 2
            self.draw_minimap(s, snap, pad, y, w - 2 * pad, mm, sc, ov)
            y += mm + pad
        s.text(pad, y, "%s menu   %s send   %s load spot   %s pause" % (hk["menu"], hk["send"], hk["bucket"],
                                                                         hk["pause"]), muted, spx * 0.92)
        if self.edit:
            y += lh
            s.text(pad, y, "drag to move, drag the corner to resize", acc, spx * 0.92)
            s.polyline([w - 4, w - 4, w - 22], [h - 22, h - 4, h - 4], acc, 3)
            s.rect(0, 0, w, h, outline=acc, width=2)
        gx, gy, gw, gh = rect
        pos = ov.get("hud_pos", [0.012, 0.02])
        x = gx + int(pos[0] * gw); yy = gy + int(pos[1] * gh)
        x = max(gx, min(gx + gw - 40, x)); yy = max(gy, min(gy + gh - 40, yy))
        self.hud_rect = (x, yy, w, h)
        s.present(x, yy, ov.get("hud_alpha", 0.9))

    def draw_minimap(self, s, snap, x0, y0, w, h, sc, ov):
        s.rect(x0, y0, x0 + w, y0 + h, fill="#0f1115", outline="#2e333e")
        pts = [(t["pos"][0], t["pos"][2]) for t in snap.get("trucks", []) if t["fleet"]]
        pts += [(p["pos"][0], p["pos"][2]) for p in snap.get("plants", [])]
        if snap.get("load"):
            pts.append((snap["load"][0], snap["load"][2]))
        if not pts:
            return
        P = np.array(pts)
        cx, cz = (P.min(axis=0) + P.max(axis=0)) / 2
        span = max(80.0, float((P.max(axis=0) - P.min(axis=0)).max()) * 1.25) / ov.get("minimap_zoom", 1.0)
        k = min(w, h) / span

        def tp(x, z):
            return x0 + w / 2 + (x - cx) * k, y0 + h / 2 - (z - cz) * k

        def inside(a, b):
            return x0 + 2 <= a <= x0 + w - 2 and y0 + 2 <= b <= y0 + h - 2

        def line(points, color, width):
            seg_x, seg_y = [], []
            for x, z in points:
                a, b = tp(x, z)
                if inside(a, b):
                    seg_x.append(a); seg_y.append(b)
                else:
                    if len(seg_x) >= 2:
                        s.polyline(seg_x, seg_y, color, width)
                    seg_x, seg_y = [], []
            if len(seg_x) >= 2:
                s.polyline(seg_x, seg_y, color, width)
        for name, one, length, rp in snap.get("roads", []):
            line(rp, "#3d4350", max(2, int(4 * sc)))
        for t in snap.get("trucks", []):
            if t.get("path"):
                line([(x, z) for x, y, z, g in t["path"]], self._truck_color(t), max(1, int(2 * sc)))
        r = 6 * sc
        for p in snap.get("plants", []):
            a, b = tp(p["pos"][0], p["pos"][2])
            if inside(a, b):
                s.rect(a - r, b - r, a + r, b + r, fill=ov.get("color_tip", "#f2a93b") if p["enabled"] else "#8b91a0")
        L = snap.get("load")
        if L:
            a, b = tp(L[0], L[2])
            if inside(a, b):
                s.ellipse(a - r, b - r, a + r, b + r, outline=ov.get("color_load", "#5bd68a"), width=2)
        exc = snap.get("exc")
        if exc:
            a, b = tp(exc[0], exc[2])
            if inside(a, b):
                s.polyline([a - r, a + r], [b - r, b + r], "#eceef2", 2)
                s.polyline([a - r, a + r], [b + r, b - r], "#eceef2", 2)
        for t in snap.get("trucks", []):
            if not t["fleet"]:
                continue
            a, b = tp(t["pos"][0], t["pos"][2])
            if not inside(a, b):
                continue
            col = self._truck_color(t)
            rr = 5 * sc
            s.ellipse(a - rr, b - rr, a + rr, b + rr, fill=col)
            s.polyline([a, a + math.sin(t["h"]) * rr * 2.2], [b, b - math.cos(t["h"]) * rr * 2.2], col, 2)
        s.text(x0 + w - 4, y0 + 3, "N", "#8b91a0", 12 * sc, True, anchor="ne")
