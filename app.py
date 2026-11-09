"""Truck Dispatcher: desktop window, in-game overlay and menu, hotkeys.

Hotkeys only work while the game (or the tool's own menu) is the active window, so F6-F10 stay free
for other programs. F6 opens the control panel over the game.
"""
import ctypes, ctypes.wintypes as wt, os, queue, sys, threading, time, traceback
import tkinter as tk
from tkinter import messagebox


def fatal(what):
    """Show start-up errors instead of vanishing, and keep them in crash.log for a bug report."""
    text = traceback.format_exc()
    base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    path = os.path.join(base, "crash.log")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + what + "\n" + text + "\n")
    except OSError:
        path = "(could not write crash.log)"
    try:
        r = tk.Tk(); r.withdraw()
        messagebox.showerror("Truck Dispatcher", "Truck Dispatcher could not %s.\n\n%s\n\nDetails: %s" % (
            what, text.strip().splitlines()[-1], path))
        r.destroy()
    except Exception:
        pass
    sys.exit(1)


try:
    import gamecam
    gamecam.set_dpi_aware()      # before any window exists, so overlay pixels match the game's
    import dispatcher as D
    import gdioverlay
    import overlay as O
except Exception:
    fatal("start")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
WM_HOTKEY, WM_QUIT, MOD_NOREPEAT, PM_REMOVE = 0x0312, 0x0012, 0x4000, 1
ERROR_ALREADY_EXISTS = 183
ACTIONS = {"send": "send", "bucket": "bucket_spot", "pause": "toggle_pause", "record": "record_toggle"}
KEYS = {"INSERT": 0x2D, "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22, "PAUSE": 0x13,
        "SCROLLLOCK": 0x91}
ICON = os.path.join(getattr(sys, "_MEIPASS", D.HERE), "media", "icon.ico")


def vk_of(name):
    name = (name or "").upper().replace(" ", "")
    if name.startswith("F") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return 0x6F + int(name[1:])
    if name.startswith("NUMPAD") and name[6:].isdigit() and 0 <= int(name[6:]) <= 9:
        return 0x60 + int(name[6:])
    if len(name) == 1 and name.isalnum():
        return ord(name)
    return KEYS.get(name)


def exe_name_of_window(hwnd, cache={}):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    pid = pid.value
    if pid in cache:
        return pid, cache[pid]
    name = ""
    h = kernel32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            name = os.path.basename(buf.value).lower()
        kernel32.CloseHandle(h)
    if len(cache) > 200:
        cache.clear()
    cache[pid] = name
    return pid, name


class Hotkeys:
    """RegisterHotKey in its own thread, registered only while the game or this tool is in front."""

    def __init__(self, out, log):
        self.out, self.log = out, log
        self.tid = None
        self.thread = None
        self._stop = False
        self.keys = {}

    def start(self, keys):
        self.stop()
        self.keys = dict(keys)
        self._stop = False
        ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(ready,), daemon=True, name="hotkeys")
        self.thread.start()
        ready.wait(2)

    def _active(self):
        fg = user32.GetForegroundWindow()
        if not fg:
            return False
        pid, name = exe_name_of_window(fg)
        return pid == os.getpid() or name == "game.exe"

    def _run(self, ready):
        self.tid = kernel32.GetCurrentThreadId()
        ids, failed_logged = {}, set()
        registered = False
        msg = wt.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)      # create the message queue
        ready.set()
        while not self._stop:
            active = self._active()
            if active and not registered:
                for n, (action, key) in enumerate(self.keys.items(), start=1):
                    vk = vk_of(key)
                    if vk and user32.RegisterHotKey(None, n, MOD_NOREPEAT, vk):
                        ids[n] = action
                    elif key not in failed_logged:
                        failed_logged.add(key)
                        self.log("Hotkey %s (%s) can't be used: unknown key or another program uses it. "
                                 "Pick another one in the menu, Settings tab." % (key, action))
                registered = True
            elif not active and registered:
                for n in ids:
                    user32.UnregisterHotKey(None, n)
                ids, registered = {}, False
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    self._stop = True
                elif msg.message == WM_HOTKEY and msg.wParam in ids:
                    self.out.put(ids[msg.wParam])
            time.sleep(0.02)
        for n in ids:
            user32.UnregisterHotKey(None, n)

    def stop(self):
        self._stop = True
        if self.thread and self.tid:
            user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
            self.thread.join(1)
        self.thread, self.tid = None, None


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Truck Dispatcher %s - Groundwork Simulator" % D.VERSION)
        self.root.configure(bg=O.BG)
        try:
            self.root.iconbitmap(default=ICON)
        except tk.TclError:
            pass
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(1180, sw - 80), min(800, sh - 120)
        self.root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, max(20, (sh - h) // 3)))
        self.root.minsize(min(900, w), min(600, h))
        self.root.report_callback_exception = self._tk_error
        self.keyq = queue.Queue()
        self.d = D.Dispatcher(log=lambda s: None)
        self.panel = O.Panel(self.root, self.d, self, desktop=True)
        self.panel.pack(fill="both", expand=True)
        self.overlay = gdioverlay.NativeOverlay(self.d)
        self.menu = O.Menu(self.root, self.d, self, self.overlay)
        self.hotkeys = Hotkeys(self.keyq, self.d.log)
        self.hotkeys.start(self.d.cfg["hotkeys"])
        self.d.start()
        self.t_panel = 0.0
        self._save_job = None
        self.root.after(100, self.loop)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    def _tk_error(self, exc, val, tb):
        try:
            raise val
        except Exception as e:
            self.d.log_error("window", e)

    # ---- used by the panels
    def save_soon(self):
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(600, self._save_now)

    def _save_now(self):
        self._save_job = None
        try:
            D.save_config(self.d.cfg)
        except OSError as e:
            self.d.log_error("saving settings", e)

    def sync_panels(self):
        self.panel.sync()
        self.menu.panel.sync()

    def set_hotkeys(self, keys):
        bad = [k for k, v in keys.items() if not vk_of(v)]
        if bad or len(set(keys.values())) < len(keys):
            self.d.log("Hotkeys not changed: %s." % ("unknown key for " + ", ".join(bad) if bad else
                                                     "two actions use the same key"))
            return False
        self.d.cfg["hotkeys"].update(keys)
        D.save_config(self.d.cfg)
        self.hotkeys.start(self.d.cfg["hotkeys"])
        self.d.log("Hotkeys: %s." % ", ".join("%s %s" % (v, k) for k, v in self.d.cfg["hotkeys"].items()))
        self.sync_panels()
        return True

    def loop(self):
        try:
            while not self.keyq.empty():
                a = self.keyq.get()
                if a == "menu":
                    self.menu.toggle()
                elif a in ACTIONS:
                    self.d.command(ACTIONS[a])
            now = time.time()
            if now - self.t_panel > 0.25:
                self.t_panel = now
                snap = self.d.snap
                self.panel.refresh(snap, self.overlay.camera_text())
                self.menu.refresh(snap)
        except Exception as e:
            self.d.log_error("window", e)
        finally:
            self.root.after(30, self.loop)

    def quit(self):
        try:
            self.d.command("stop_all")
            time.sleep(0.1)
            self.d.stop()
            self.overlay.stop()
            self.hotkeys.stop()
            if self._save_job:
                self._save_now()
        finally:
            self.root.destroy()


def single_instance():
    """Two copies would both drive the same trucks; only one may run."""
    h = kernel32.CreateMutexW(None, False, "Local\\GroundworkTruckDispatcher")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        r = tk.Tk(); r.withdraw()
        messagebox.showinfo("Truck Dispatcher", "Truck Dispatcher is already running.\n\n"
                            "Look for its window on the taskbar, or press F6 in the game.")
        r.destroy()
        sys.exit(0)
    return h


if __name__ == "__main__":
    _mutex = single_instance()
    try:
        app = App()
    except Exception:
        fatal("start")
    app.root.mainloop()
