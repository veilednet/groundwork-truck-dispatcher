"""Minimal Windows process-memory access for game.exe (read, write, scan)."""
import ctypes, ctypes.wintypes as wt, os, struct, subprocess

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_ACCESS = 0x0438 | 0x1000 | 0x00100000   # VM_READ|VM_WRITE|VM_OPERATION|QUERY_INFORMATION, QUERY_LIMITED, SYNCHRONIZE
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_RW = (0x04, 0x40)  # PAGE_READWRITE, PAGE_EXECUTE_READWRITE
STILL_ACTIVE = 259


class GameGone(RuntimeError):
    """game.exe closed, or memory the tool was using is no longer readable."""


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
                ("AllocationProtect", wt.DWORD), ("PartitionId", wt.WORD), ("_p", wt.WORD),
                ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD), ("Protect", wt.DWORD),
                ("Type", wt.DWORD), ("_p2", wt.DWORD)]


k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_uint64, ctypes.POINTER(MBI), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.argtypes = k32.ReadProcessMemory.argtypes


def find_pids(name="game.exe"):
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + name, "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == name.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def _is_groundwork(pid):
    """game.exe is a common name: the Groundwork one has assets/maps next to it."""
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return False
        return os.path.isdir(os.path.join(os.path.dirname(buf.value), "assets", "maps"))
    finally:
        k32.CloseHandle(h)


def find_pid(name="game.exe"):
    pids = find_pids(name)
    if len(pids) > 1:
        pids = [p for p in pids if _is_groundwork(p)] or pids
    return pids[0] if pids else None


class Proc:
    def __init__(self, pid=None):
        self.pid = pid or find_pid()
        self.h = None
        if not self.pid:
            raise GameGone("game.exe is not running")
        self.h = k32.OpenProcess(PROCESS_ACCESS, False, self.pid)
        if not self.h:
            raise RuntimeError("could not open game.exe (%s)" % ctypes.WinError(ctypes.get_last_error()).strerror)

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def alive(self):
        if not self.h:
            return False
        code = wt.DWORD()
        return bool(k32.GetExitCodeProcess(self.h, ctypes.byref(code))) and code.value == STILL_ACTIVE

    def exe_path(self):
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(len(buf))
        if self.h and k32.QueryFullProcessImageNameW(self.h, 0, buf, ctypes.byref(n)):
            return buf.value
        return None

    def game_dir(self):
        p = self.exe_path()
        return os.path.dirname(p) if p else None

    def regions(self, max_size=1 << 31):
        addr, mbi = 0, MBI()
        while k32.VirtualQueryEx(self.h, addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
            if (mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE and mbi.Protect in PAGE_RW
                    and mbi.RegionSize <= max_size):
                yield mbi.BaseAddress, mbi.RegionSize
            addr = mbi.BaseAddress + mbi.RegionSize
            if addr >= 1 << 47:
                break

    def read(self, addr, n):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t()
        if not k32.ReadProcessMemory(self.h, addr, buf, n, ctypes.byref(got)):
            return None
        return buf.raw[:got.value]

    def write(self, addr, data):
        got = ctypes.c_size_t()
        return bool(k32.WriteProcessMemory(self.h, addr, data, len(data), ctypes.byref(got)))

    def f32(self, addr, n=1):
        b = self.read(addr, 4 * n)
        return struct.unpack("<%df" % n, b) if b and len(b) == 4 * n else None

    def put_f32(self, addr, *vals):
        return self.write(addr, struct.pack("<%df" % len(vals), *vals))

    def scan(self, pattern, step=1 << 24, limit=1000):
        """Addresses of `pattern` in private RW memory."""
        hits = []
        for base, size in self.regions():
            off = 0
            while off < size:
                n = min(step, size - off)
                b = self.read(base + off, n + len(pattern) - 1 if off + n < size else n)
                if b:
                    i = b.find(pattern)
                    while i >= 0:
                        hits.append(base + off + i)
                        if len(hits) >= limit:
                            return hits
                        i = b.find(pattern, i + 1)
                off += n
        return hits
