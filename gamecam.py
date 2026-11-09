"""The game's camera and window, so the overlay can draw on top of the 3D world.

Camera: Bevy keeps a `Frustum` per camera: six planes (unit normal xyz + distance w, 16 bytes each)
in the order left, right, bottom, top, near, far. For the perspective game camera the near and far
normals are exact opposites and every plane normal is unit length, which is rare enough to scan for.
From the planes we get the eye position, the view axes and the field of view, which is all that is
needed to project a world point to the screen.
"""
import ctypes, ctypes.wintypes as wt, threading, time

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)


class Camera:
    def __init__(self, proc, addr):
        self.p, self.addr = proc, addr
        self.ok = False
        self.read()

    def read(self):
        b = self.p.read(self.addr, 96)
        if not b or len(b) < 96:
            self.ok = False
            return False
        P = np.frombuffer(b, dtype=np.float32).reshape(6, 4).astype(np.float64)
        sol = solve_frustum(P)
        if sol is None:
            self.ok = False
            return False
        self.c, self.f, self.r, self.u, self.tx, self.ty = sol
        self.ok = True
        return True

    def project(self, pts, W, H):
        """Nx3 world points -> (sx, sy, visible) arrays in client pixels."""
        q = np.asarray(pts, float) - self.c
        d = q @ self.f
        vis = d > 0.5
        dd = np.where(vis, d, 1.0)
        x = (q @ self.r) / (dd * self.tx)
        y = (q @ self.u) / (dd * self.ty)
        return (1 + x) * 0.5 * W, (1 - y) * 0.5 * H, vis


def solve_frustum(P):
    n = P[:, :3]
    if np.any(np.abs(np.linalg.norm(n, axis=1) - 1) > 1e-3):
        return None
    f = n[4]
    if np.dot(n[4], n[5]) > -0.999:
        return None
    A, bvec = n[:4], -P[:4, 3]
    try:
        c, *_ = np.linalg.lstsq(A, bvec, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if np.abs(A @ c - bvec).max() > 0.05:                  # side planes must meet at the eye
        return None
    r = n[0] - n[1]; r -= (r @ f) * f
    u = n[2] - n[3]; u -= (u @ f) * f
    if np.linalg.norm(r) < 1e-3 or np.linalg.norm(u) < 1e-3:
        return None
    r /= np.linalg.norm(r); u /= np.linalg.norm(u)
    a0, b0 = n[0] @ f, n[0] @ r
    a2, b2 = n[2] @ f, n[2] @ u
    if b0 <= 1e-3 or b2 <= 1e-3:
        return None
    tx, ty = a0 / b0, a2 / b2
    if not (0.15 < tx < 4 and 0.15 < ty < 4):              # 17..150 degree field of view
        return None
    return c, f, r, u, tx, ty


def find_cameras(proc, near=None, within=1e9, log=print, max_hits=256):
    """Scan private memory for perspective frustums: [(addr, solved)]. Eye within `within` m of `near` if given."""
    near = None if near is None else np.asarray(near, float)
    hits = []
    with np.errstate(all="ignore"):
        for base, size in proc.regions():
            off = 0
            while off < size:
                n = min(1 << 24, size - off)
                b = proc.read(base + off, n)
                if b and len(b) >= 96:
                    a = np.frombuffer(b[:len(b) // 16 * 16], dtype=np.float32).reshape(-1, 4)
                    nn = a[:, 0] * a[:, 0] + a[:, 1] * a[:, 1] + a[:, 2] * a[:, 2]
                    unit = np.abs(nn - 1.0) < 2e-4
                    run = unit[:-5] & unit[1:-4] & unit[2:-3] & unit[3:-2] & unit[4:-1] & unit[5:]
                    idx = np.nonzero(run)[0]
                    if len(idx):
                        opp = (a[idx + 4, :3] * a[idx + 5, :3]).sum(axis=1) < -0.9999
                        for i in idx[opp]:
                            sol = solve_frustum(a[i:i + 6].astype(np.float64))
                            if sol is None:
                                continue
                            if near is None or np.linalg.norm(sol[0] - near) < within:
                                hits.append((base + off + int(i) * 16, sol))
                                if len(hits) >= max_hits:
                                    return hits
                off += n
    return hits


class CameraTracker:
    """Follows the game camera. The game keeps several copies of each camera's frustum, some of them
    stale (left where you loaded in), vehicle lights look like cameras too, and switching view can
    move the camera to new memory. So: keep every candidate with the window's aspect ratio, read them
    all each frame, use the one that changed most recently, and rescan in the background now and then."""

    def __init__(self, proc, log=print):
        self.p, self.log = proc, log
        self.cands = {}             # addr -> [bytes, last_change_time, found_time]
        self.cam = None
        self.scanning = False
        self.next_scan = 0.0
        self.last_motion = 0.0
        self.state = "searching for the game camera..."

    def _scan(self, aspect):
        try:
            hits = find_cameras(self.p)
            now = time.time()
            keep = {}
            for addr, sol in hits:
                tx, ty = sol[4], sol[5]
                if abs(tx / ty / aspect - 1) < 0.02 and 0.2 < ty < 1.5:
                    old = self.cands.get(addr)
                    keep[addr] = [self.p.read(addr, 96), old[1] if old else 0.0, old[2] if old else now]
            self.cands = keep
            if not keep:
                self.state = "game camera not found yet (status panel and mini map still work)"
        except Exception as e:
            self.state = "camera search failed: %s" % e
        finally:
            self.scanning = False

    def update(self, aspect):
        now = time.time()
        if not self.scanning and now > self.next_scan:
            stale = now - self.last_motion > 2.0
            self.scanning = True
            self.next_scan = now + (10.0 if stale else 20.0)
            threading.Thread(target=self._scan, args=(aspect,), daemon=True).start()
        best = None
        for addr, c in list(self.cands.items()):
            b = self.p.read(addr, 96)
            if not b:
                self.cands.pop(addr, None)
                continue
            if b != c[0]:
                c[0], c[1] = b, now
            key = (c[1], c[2])            # most recently changed; before any change, the newest found
            if best is None or key > best[0]:
                best = (key, addr)
        if best is None:
            self.cam = None
            return None
        addr = best[1]
        if self.cam is None or self.cam.addr != addr:
            self.cam = Camera(self.p, addr)
        else:
            self.cam.read()
        if not self.cam.ok:
            self.cands.pop(addr, None)
            self.cam = None
            return None
        if self.cands.get(addr, (None, -1e9))[1] >= now - 0.1:
            self.last_motion = now
        self.state = "following the game camera"
        return self.cam


# ---------------------------------------------------------------- game window

class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def game_window(pid):
    """Largest visible top-level window of the game process."""
    best = [None, 0]

    def cb(hwnd, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            r = RECT()
            user32.GetClientRect(hwnd, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best[1]:
                best[0], best[1] = hwnd, area
        return True
    user32.EnumWindows(EnumProc(cb), 0)
    return best[0]


def client_rect(hwnd):
    """(x, y, w, h) of the window's client area on screen, or None."""
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    r = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(r)):
        return None
    pt = POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, r.right - r.left, r.bottom - r.top


def foreground():
    return user32.GetForegroundWindow()


def is_minimized(hwnd):
    return bool(user32.IsIconic(hwnd))


def focus(hwnd):
    """Bring a window to the front (works right after our own window had focus)."""
    if not hwnd:
        return
    try:
        user32.keybd_event(0x12, 0, 0, 0)          # tap Alt: lets SetForegroundWindow through
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def set_dpi_aware():
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_TOPMOST = \
    0x20, 0x80000, 0x80, 0x08000000, 0x8
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.restype = ctypes.c_long


def set_click_through(hwnd, on):
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_LAYERED | WS_EX_TOOLWINDOW
    if on:
        ex |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
    else:
        ex &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)


def keep_on_top(hwnd):
    """Topmost without activating (the game keeps keyboard focus)."""
    user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0200)
