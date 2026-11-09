"""Find the save that is loaded in the game, and the live data the dispatcher needs in game memory.

Nothing in game memory has a fixed address, so every connect scans for values it knows from an autosave:
  heights   : 785 x 785 f32 terrain grid (1 m cells, x = ix - 392, z = iz - 392).
  machines  : Vec of 0x5D0-byte records, same order as the save's `machines`. The game keeps mirror
              copies; only the master accepts writes.
  beds      : truck bed cargo records (for emulated tipping).
  plants    : wash plant records (hopper volumes).
The loaded save is the slot whose terrain matches the live terrain best (the newest file is not always
the one being played: the game writes other slots too, e.g. when a new game is created).
"""
import glob, os, re, shutil, struct, sys, tempfile, time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "savelib"), os.path.join(HERE, "..", "SaveEditor")):
    if os.path.isfile(os.path.join(_p, "gwsave.py")):
        sys.path.append(_p)
        break
import gwsave        # noqa: E402
import msgpack       # noqa: E402

STRIDE = 0x5D0
OFF_SPAWN = -0x538    # authored_spawn_translation, relative to the fuel field (record start)
GRID = 785
SLOTS = os.path.join(os.environ.get("APPDATA", ""), "groundwork-simulator", "groundwork-simulator",
                     "data", "saves", "slots")
DIFF_CELL_M = 0.05    # a terrain cell counts as different above this
MATCH_MAX = 0.04      # the loaded save differs from the live terrain in fewer than 4 % of cells


class NotReady(RuntimeError):
    """The game is running but no save matches it yet (main menu, loading, or no autosave yet)."""


def slot_files():
    files = [f for f in glob.glob(os.path.join(SLOTS, "slot-*.groundwork-simulator")) if ".previous." not in f]
    return sorted(files, key=os.path.getmtime, reverse=True)


def newest_save():
    files = slot_files()
    return files[0] if files else None


def slot_index():
    """{slot number: {"save_name", "map_name", "map_package"}} from slots.ron (best effort)."""
    out = {}
    try:
        with open(os.path.join(SLOTS, "slots.ron"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return out
    for m in re.finditer(r"(\d+)\s*:\s*SaveSlotEntry\((.*?)\n\s*\)", text, re.S):
        body = m.group(2)
        e = {}
        for key in ("save_name", "map_name", "map_package"):
            k = re.search(key + r'\s*:\s*"((?:[^"\\]|\\.)*)"', body)
            if k:
                e[key] = k.group(1).replace("\\\\", "\\")
        out[int(m.group(1))] = e
    return out


def raw_section(doc, key):
    _, vs, ve = doc.idx[key]
    return msgpack.unpackb(doc.world[vs:ve], raw=False, strict_map_key=False)


def _optional(doc, key):
    """A save section that a save may not have (no wash plant yet, no trucks...)."""
    try:
        return doc.get(key)
    except (KeyError, IndexError):
        return None


class Save:
    """The parts of a save file the dispatcher uses."""

    def __init__(self, path, doc):
        self.path, self.doc = path, doc
        self.file = os.path.basename(path)
        m = re.search(r"slot-(\d+)", self.file)
        self.slot = int(m.group(1)) if m else 0
        info = slot_index().get(self.slot, {})
        self.name = info.get("save_name") or self.file
        self.map_package = info.get("map_package")
        self.machines = doc.get("machines")
        self.heights = np.asarray(raw_section(doc, "terrain")["heights"], dtype=np.float32)
        self.cargo = _optional(doc, "truck_cargo") or {"beds": {}}
        self.plants = _optional(doc, "wash_plants") or []

    @property
    def key(self):
        """Identifies the save in state.json: slot number + save name."""
        return "slot-%02d|%s" % (self.slot, self.name)


def read_save(path=None, log=print, tries=4):
    """Read a save (the newest by default). Retries: the game may be in the middle of writing it."""
    path = path or newest_save()
    if not path:
        raise NotReady("no saves found in %s" % SLOTS)
    last = None
    tmp = os.path.join(tempfile.gettempdir(), "truckdispatcher-save-%d.tmp" % os.getpid())
    for _ in range(tries):
        try:
            shutil.copyfile(path, tmp)
            save = Save(path, gwsave.SaveDoc(tmp).load())
            return save
        except Exception as e:     # half-written autosave
            last = e
            time.sleep(1.5)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    raise RuntimeError("could not read %s: %s" % (os.path.basename(path), last))


def _diff_fraction(live, heights):
    return float(np.count_nonzero(np.abs(live - heights) > DIFF_CELL_M)) / live.size


def find_loaded_save(proc, log=print, max_slots=8):
    """(Save, terrain address): the save the game has loaded, picked by comparing terrain."""
    files = slot_files()
    if not files:
        raise NotReady("no saves found - start or load a game")
    first, base = None, None
    for f in files[:3]:                        # the newest save's map, else an older one (other map)
        s = read_save(f, log=log)
        first = first or s
        base = find_heights(proc, s.heights)
        if base is not None:
            first = s
            break
    if base is None:
        raise NotReady("no map loaded in the game yet (main menu?)")
    blob = proc.read(base, GRID * GRID * 4)
    if not blob or len(blob) != GRID * GRID * 4:
        raise NotReady("terrain not readable yet")
    live = np.frombuffer(blob, dtype=np.float32)
    best = (_diff_fraction(live, first.heights), first)
    if best[0] > MATCH_MAX / 4:
        for f in files[:max_slots]:
            if f == first.path:
                continue
            try:
                s = read_save(f, log=log, tries=2)
            except Exception:
                continue
            if s.heights.shape != live.shape:
                continue
            frac = _diff_fraction(live, s.heights)
            if frac < best[0]:
                best = (frac, s)
    frac, save = best
    log("Save: '%s' (slot %d), terrain matches the game to %.1f %%." % (save.name, save.slot, 100 * (1 - frac)))
    if frac > MATCH_MAX:
        raise NotReady("the game's terrain doesn't match any autosave yet (%.0f %% different). If you just "
                       "started or loaded a game, wait for its first autosave" % (100 * frac))
    return save, base


def find_machine_array(proc, machines, log=print):
    """Address of machine 0's fuel field in the master array."""
    n = len(machines)
    if n == 0:
        raise NotReady("the save has no machines")
    spawn = [struct.pack("<3f", *m["authored_spawn_translation"]) for m in machines]
    # Rarest spawn translation gives the fewest hits.
    k = min(range(n), key=lambda i: spawn.count(spawn[i]))
    same = [j for j in range(n) if spawn[j] == spawn[k]]
    cands = set()
    for hit in proc.scan(spawn[k], limit=20000):
        for j in same:
            base = hit - OFF_SPAWN - j * STRIDE
            if base in cands:
                continue
            blob = proc.read(base + OFF_SPAWN, n * STRIDE)
            if blob and len(blob) == n * STRIDE and all(blob[i * STRIDE:i * STRIDE + 12] == spawn[i] for i in range(n)):
                cands.add(base)
    cands = sorted(cands)
    if not cands:
        raise NotReady("machine list not found in game memory (the save may be older than the game's state; "
                       "wait for the next autosave)")
    # Master = the copy whose writes stick (mirrors are re-copied from it every frame).
    probe = OFF_SPAWN + 4                      # spawn y: never used while playing
    for base in cands:
        orig = proc.read(base + probe, 4)
        if not orig:
            continue
        proc.put_f32(base + probe, struct.unpack("<f", orig)[0] + 1.0)
        time.sleep(0.15)
        stuck = proc.read(base + probe, 4) != orig
        followers = [b for b in cands if b != base and proc.read(b + probe, 4) != orig]
        proc.write(base + probe, orig)
        if stuck and (followers or len(cands) == 1):
            time.sleep(0.15)
            return base
    raise NotReady("found %d copies of the machine list but none accepted changes" % len(cands))


class Unsupported(RuntimeError):
    """The machine records don't look like the layout this tool was made for (game update?)."""


def check_layout(proc, fuel0, machines):
    """Sanity check before any write: fuel 0..1 and positions near the autosave for most machines."""
    near, n = 0, len(machines)
    for i, m in enumerate(machines):
        v = proc.f32(fuel0 + i * STRIDE, 5)
        if v is None:
            raise NotReady("machine list not readable")
        fuel, x, y, z, h = v
        if not all(np.isfinite(v)) or not (-0.01 <= fuel <= 1.01) or abs(x) > 1000 or abs(z) > 1000:
            raise Unsupported("machine #%d data doesn't look right (fuel %.3g, pos %.4g, %.4g). The game was "
                              "probably updated; this version of the tool won't write anything" % (i, fuel, x, z))
        sx, _, sz = m.get("translation", (x, y, z))
        if np.hypot(x - sx, z - sz) < 40:
            near += 1
    if near < max(1, n // 2):
        raise NotReady("most machines are far from where the autosave has them; wait for the next autosave")


def spawn_signature(proc, fuel0, n):
    """Bytes that identify the machine list (spawn points); used to notice a reload or a closed game."""
    out = []
    for i in range(n):
        b = proc.read(fuel0 + i * STRIDE + OFF_SPAWN, 12)
        out.append(b)
    return out


def find_heights(proc, heights):
    """Address of the live terrain height grid (cell 0), or None."""
    iz, ix = 700, 700                          # far corner, never dug
    i0 = iz * GRID + ix
    best = None
    for hit in proc.scan(heights[i0:i0 + 8].tobytes(), limit=200):
        base = hit - i0 * 4
        blob = proc.read(base, GRID * GRID * 4)
        if not blob or len(blob) != GRID * GRID * 4:
            continue
        diff = float(np.abs(np.frombuffer(blob, dtype=np.float32) - heights).sum())
        if best is None or diff < best[0]:
            best = (diff, base)
    return best[1] if best else None


# ---------------------------------------------------------------- truck beds and wash plant

BED_STRIDE = 0x168      # bed records sit back to back in save key order; anchor = discharged_loose_volume_m3
WASH_PROBE = 0x0C       # drag_speed_mps, 0 while set up


def find_beds(proc, truck_cargo, near, log=print):
    """{machine index: [bed anchor addresses]}: the master copy (allocated next to `near`) first."""
    keys = list(truck_cargo.get("beds", {}).keys())
    if not keys:
        return {}
    beds = [truck_cargo["beds"][k] for k in keys]
    pats = [struct.pack("<2f", b["discharged_loose_volume_m3"], b["captured_loose_volume_m3"]) for b in beds]
    caps = [struct.pack("<f", b["capacity_m3"]) for b in beds]
    cands = set()
    for j, pat in enumerate(pats):
        for hit in proc.scan(pat, limit=50):
            base = hit - j * BED_STRIDE
            ok = all(proc.read(base + i * BED_STRIDE - 4, 4) == caps[i] for i in range(len(beds)))
            if ok:
                cands.add(base)
        if cands:
            break
    if not cands:
        log("Truck beds not found: trucks can drive but not tip until the next autosave and a reconnect.")
        return {}
    base = min(cands, key=lambda b: abs(b - near))
    others = [b for b in sorted(cands) if b != base]
    return {int(k): [base + i * BED_STRIDE] + [o + i * BED_STRIDE for o in others] for i, k in enumerate(keys)}


BED_FIELDS = ["loose_volumes", "bank_volumes", "masses_kg", "moisture_volumes", "pay_bank_volumes",
              "washed_bank_volumes", "pay_grade_volumes", "material_volumes", "visual_color_volumes"]


def bed_layout(proc, beds, cargo, log=print):
    """Where the per-cell arrays and the revision counter sit in a bed record. Game updates have moved
    them, so they are found by matching a save's bed (array lengths/sums, revision) against memory.
    Returns {"vec": offset of the first (cap, ptr, len) header, "rev": (offset, struct fmt), "cache": offset}
    or None (tipping is then turned off rather than guessed)."""
    for key, b in cargo.get("beds", {}).items():
        anchors = beds.get(int(key))
        if not anchors:
            continue
        lens = [len(b.get(f) or []) for f in BED_FIELDS]
        sums = [float(np.sum(b.get(f) or [0.0])) for f in BED_FIELDS]
        if lens[0] == 0:
            continue
        a = anchors[0]
        for vec in (-0x158, -0x154, -0x15C, -0x150, -0x160):
            ok = True
            for k, (ln_save, sm) in enumerate(zip(lens, sums)):
                raw = proc.read(a + vec + k * 0x18, 24)
                if not raw:
                    ok = False; break
                cap, ptr, ln = struct.unpack("<QQQ", raw)
                if ln != ln_save or cap < ln or ptr < 0x10000 or ln > 100000:
                    ok = False; break
                if k in (0, 1, 7):                       # loose, bank, material volumes: sums match the save
                    arr = proc.read(ptr, ln * 4)
                    if not arr:
                        ok = False; break
                    live = float(np.frombuffer(arr, dtype=np.float32).sum())
                    if abs(live - sm) > 0.05 + 0.02 * abs(sm):
                        ok = False; break
            if not ok:
                continue
            rev = None
            want = int(b.get("revision", -1))
            for off, fmt in ((-0x68, "<Q"), (-0x0C, "<I"), (-0x64, "<I"), (-0x70, "<Q")):
                raw = proc.read(a + off, struct.calcsize(fmt))
                if raw and want >= 0 and 0 <= struct.unpack(fmt, raw)[0] - want < 1_000_000:
                    rev = (off, fmt)
                    break
            cache = proc.f32(a - 0x60, 2)
            if rev is None or cache is None or abs(cache[0] - sums[0]) > 0.05 + 0.02 * abs(sums[0]):
                continue
            return {"vec": vec, "rev": rev, "cache": -0x60}
    log("Truck bed layout not recognised (game update?): trucks drive but tipping is off.")
    return None


def find_wash_plant(proc, plant, log=print):
    """Address of the wash plant's yaw field (master copy), or None."""
    pat = struct.pack("<4f", *plant["position"], plant["yaw"])
    cands = [h + 12 for h in proc.scan(pat, limit=200)]
    if "delivered_power_kw" in plant:
        cands = [c for c in cands if proc.read(c + 0x18, 4) == struct.pack("<f", plant["delivered_power_kw"])] or cands
    for c in cands:
        orig = proc.read(c + WASH_PROBE, 4)
        if not orig:
            continue
        proc.put_f32(c + WASH_PROBE, struct.unpack("<f", orig)[0] + 0.001)
        time.sleep(0.15)
        stuck = proc.read(c + WASH_PROBE, 4) != orig
        proc.write(c + WASH_PROBE, orig)
        if stuck:
            return c
    return None
