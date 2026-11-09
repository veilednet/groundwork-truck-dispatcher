"""Groundwork Simulator save container: read / modify / verify / write.

Format 1 (game <= 0.1.2.1, first byte 0x87): one msgpack envelope with a zstd world and a checksum.
Format 2 (game 0.1.3.1+, header format_version 2): an append-only chunk log, see gwformat2.py. Saving a format 2
file appends a commit, as the game does; format 1 files are still written as format 1.
"""
import os, re, io, json, struct, hashlib, shutil, time, subprocess, glob
import zstandard, msgpack
import gwcodec as C
import gwformat2 as F2

SAVE_DIR = os.path.join(os.environ.get("APPDATA", ""), "groundwork-simulator", "groundwork-simulator", "data", "saves")
SLOT_DIR = os.path.join(SAVE_DIR, "slots")
BACKUP_DIR = os.path.join(SLOT_DIR, "editor-backups")
EXT_CAMPAIGN = "groundwork-simulator-campaign-v1"
BIG_LIMIT = 8_000_000        # world sections larger than this are not decoded (terrain/water/etc.)



def enc_u8_array(data):
    """Serde-style Vec<u8>: a MessagePack array with one integer per byte."""
    return msgpack.packb(list(data))


def dec_u8_array(buf):
    return bytes(msgpack.unpackb(buf, raw=False))


def zstd_decompress(payload):
    r = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(payload), read_size=1 << 24)
    parts = []
    while True:
        c = r.read(1 << 26)
        if not c: break
        parts.append(c)
    return b"".join(parts)


def compute_checksum(payload, ext):
    """SHA-256 over the compressed world, then each extension entry as len(key) key len(value) value (u64 LE lengths)."""
    h = hashlib.sha256(payload)
    for k in sorted(ext):
        kb = k.encode("utf-8")
        h.update(struct.pack("<Q", len(kb))); h.update(kb)
        h.update(struct.pack("<Q", len(ext[k]))); h.update(ext[k])
    return h.digest()


def read_head(raw, verify=True):
    """Header-level read of a save file's bytes (either format) without decompressing the world (~50-300 ms).
    -> {"format", "metadata", "ext_bytes", "checksum_ok"}; checksum_ok is None when verify is False.
    Raises ValueError if the bytes are not a whole save."""
    if raw[:1] == b"\x87":
        pos, fields = 1, {}
        while True:
            k, pos = C.decode(raw, pos)
            if k == "compressed_world_payload": break
            v, pos = C.decode(raw, pos); fields[k] = v
        if raw[pos] != 0xdd: raise ValueError("unexpected payload encoding")
        n = struct.unpack_from(">I", raw, pos + 1)[0]
        tail = raw.rfind(b"\xb2extension_payloads")
        if tail < pos: raise ValueError("file is incomplete (no extension block)")
        _, tp = C.decode(raw, tail)
        ext_raw, tp = C.decode(raw, tp)
        if tp != len(raw): raise ValueError("file is incomplete or has trailing bytes")
        ext = {k: bytes(v) for k, v in ext_raw.items()}
        ok = None
        if verify:
            payload = dec_u8_array(raw[pos:tail])
            ok = len(payload) == n and compute_checksum(payload, ext) == bytes(fields["checksum_sha256"])
        return {"format": 1, "metadata": fields.get("metadata") or {}, "ext_bytes": ext, "checksum_ok": ok,
                "fields": fields, "payload_span": (pos + 5, tail, n)}
    if F2.is_v2(raw):
        _, env, _ = F2.read_envelope(raw, verify=verify)
        ok = (F2.verify_frames(raw, env) is None) if verify else None
        return {"format": 2, "metadata": env.get("metadata") or {}, "ext_bytes": F2.ext_bytes(env), "checksum_ok": ok}
    raise ValueError("Not a Groundwork save (bad envelope header)")


def game_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq game.exe", "/NH"], capture_output=True, text=True,
                             creationflags=0x08000000).stdout
        return "game.exe" in out.lower()
    except Exception:
        return False


def list_slots():
    res = []
    for p in sorted(glob.glob(os.path.join(SLOT_DIR, "slot-*.groundwork-simulator"))):
        m = re.match(r"slot-(\d+)\.groundwork-simulator$", os.path.basename(p))
        if m: res.append((int(m.group(1)), p))
    return res


def read_slot_index():
    try: return open(os.path.join(SLOT_DIR, "slots.ron"), encoding="utf-8").read()
    except OSError: return ""


class SaveDoc:
    def __init__(self, path):
        self.path = path
        self.dirty_world = set()
        self.ext_dirty = False
        self.cache = {}          # world section -> decoded object
        self.readonly = {}       # world section -> reason

    # ---------------------------------------------------------------- loading
    def load(self, progress=lambda s: None):
        progress("Reading file...")
        raw = open(self.path, "rb").read()
        self.raw = raw
        if F2.is_v2(raw): return self._load_v2(progress)
        if raw[:1] != b"\x87": raise ValueError("Not a Groundwork save (bad envelope header)")
        self.format = 1
        pos = 1
        fields = {}
        while True:
            k, pos = C.decode(raw, pos)
            if k == "compressed_world_payload": break
            v, pos = C.decode(raw, pos); fields[k] = v
        if raw[pos] != 0xdd: raise ValueError("unexpected payload encoding")
        n = struct.unpack_from(">I", raw, pos + 1)[0]; body0 = pos + 5
        tail = raw.rfind(b"\xb2extension_payloads")
        progress("Unpacking payload...")
        payload = dec_u8_array(raw[pos:tail])
        if len(payload) != n: raise ValueError("payload length mismatch (%d vs %d)" % (len(payload), n))
        _, tp = C.decode(raw, tail)
        ext_raw, tp = C.decode(raw, tp)
        if tp != len(raw): raise ValueError("trailing bytes after extension payloads")
        self.magic = bytes(fields["magic"]); self.format_version = fields["format_version"]
        self.baseline = fields["baseline"]; self.stored_checksum = bytes(fields["checksum_sha256"])
        self.metadata = fields["metadata"]
        self.payload = payload
        self.ext_bytes = {k2: bytes(v) for k2, v in ext_raw.items()}
        self.checksum_ok = compute_checksum(payload, self.ext_bytes) == self.stored_checksum
        self.ext_json = {}
        for k2, b in self.ext_bytes.items():
            try: self.ext_json[k2] = json.loads(b)
            except Exception: pass
        progress("Decompressing world...")
        self.world = zstd_decompress(payload)
        progress("Indexing world...")
        self._index_world()
        return self

    def _load_v2(self, progress):
        progress("Reading commit...")
        v2 = F2.SaveV2(self.path, raw=self.raw)
        self.format = 2; self.v2 = v2; self.commit = v2.commit
        self.magic = bytes(v2.header["magic"]); self.format_version = v2.header["format_version"]
        self.baseline = v2.header["baseline"]
        c, _, _, _ = F2.find_commit(self.raw)
        self.stored_checksum = self.raw[c + 32:c + 64]        # sha256 of the envelope (checked while reading)
        self.metadata = v2.metadata
        self.payload = None
        self.ext_bytes = F2.ext_bytes(v2.envelope)
        self.ext_json = {}
        for k2, b in self.ext_bytes.items():
            try: self.ext_json[k2] = json.loads(b)
            except Exception: pass
        progress("Decompressing world...")
        self.world = v2.world                                  # checks every chunk's raw_hash
        self.checksum_ok = F2.verify_frames(self.raw, v2.envelope) is None
        progress("Indexing world...")
        self._index_world()
        return self

    @staticmethod
    def index_world(w):
        """(map header bytes, section order, {section: (key start, value start, value end)}) of a world."""
        u = msgpack.Unpacker(io.BytesIO(w), raw=False, max_buffer_size=len(w) + 10, strict_map_key=False)
        n = u.read_map_header(); order = []; idx = {}
        header = w[:u.tell()]
        for _ in range(n):
            ks = u.tell(); k = u.unpack(); vs = u.tell(); u.skip(); ve = u.tell()
            order.append(k); idx[k] = (ks, vs, ve)
        return header, order, idx

    def _index_world(self):
        self.header, self.order, self.idx = self.index_world(self.world)

    # ---------------------------------------------------------------- world sections
    def section_size(self, key):
        _, s, e = self.idx[key]
        return e - s

    def section_editable(self, key):
        return self.section_size(key) <= BIG_LIMIT

    def get(self, key, default=KeyError):
        """Decoded (mutable) world section; call touch(key) after modifying it.
        Sections appear only once used (a fresh game has no wash_plants / bore_logs): pass default to allow that."""
        if key in self.cache: return self.cache[key]
        if key not in self.idx and default is not KeyError: return default
        ks, vs, ve = self.idx[key]
        if ve - vs > BIG_LIMIT:
            raise ValueError("%s is too large to edit (%.1f MB)" % (key, (ve - vs) / 1e6))
        v, p = C.decode(self.world, vs)
        if p != ve: raise ValueError("decode length mismatch in %s" % key)
        if bytes(C.encode(v)) != self.world[vs:ve]:
            self.readonly[key] = "re-encoding would not be byte-exact"
        self.cache[key] = v
        return v

    def touch(self, key):
        if key in self.readonly: raise ValueError("Section %s is read-only: %s" % (key, self.readonly[key]))
        self.dirty_world.add(key)

    def touch_ext(self):
        self.ext_dirty = True

    @property
    def dirty(self):
        return bool(self.dirty_world or self.ext_dirty or self.meta_changed)

    meta_changed = False

    # ---------------------------------------------------------------- campaign (funds) helpers
    @property
    def campaign(self):
        return self.ext_json.get(EXT_CAMPAIGN)

    def balance(self):
        f = self.campaign["funds"]
        return f["opening_balance_eur"] + sum(p["amount_eur"] for p in f["ledger_postings"])

    def set_balance(self, target):
        f = self.campaign["funds"]
        f["opening_balance_eur"] += int(target) - self.balance()
        self.touch_ext()

    # ---------------------------------------------------------------- assembling
    def build_world(self, progress=lambda s: None):
        """The world msgpack with the dirty sections re-encoded."""
        if not self.dirty_world: return self.world
        progress("Re-encoding world...")
        parts = [self.header]; w = self.world
        for k in self.order:
            ks, vs, ve = self.idx[k]
            parts.append(w[ks:vs])
            parts.append(bytes(C.encode(self.cache[k])) if k in self.dirty_world else w[vs:ve])
        return b"".join(parts)

    def build_world_payload(self, progress=lambda s: None):
        if not self.dirty_world and self.payload is not None: return self.payload
        newworld = self.build_world(progress)
        progress("Compressing world (zstd)...")
        return zstandard.ZstdCompressor(level=3, threads=-1).compress(newworld)

    def assemble(self, progress=lambda s: None, world=None):
        """-> (file bytes, compressed payload (None for format 2), extensions). world= replaces the whole world
        (same msgpack map, e.g. GoldEditor's in-place array edits) instead of re-encoding the dirty sections."""
        ext = dict(self.ext_bytes)
        if self.ext_dirty:
            for k, obj in self.ext_json.items():
                ext[k] = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if self.format == 2:
            w = world if world is not None else self.build_world(progress)
            if w is self.world: order, idx = self.order, self.idx
            else:
                progress("Indexing world...")
                _, order, idx = self.index_world(w)
            progress("Compressing changed chunks (zstd)...")
            data = F2.append_commit(self.raw, self.v2.envelope, w, idx, order, metadata=self.metadata, extensions=ext)
            return data, None, ext
        if world is not None:
            progress("Compressing world (zstd)...")
            payload = zstandard.ZstdCompressor(level=3, threads=-1).compress(world)
        else:
            payload = self.build_world_payload(progress)
        ck = compute_checksum(payload, ext)
        out = bytearray(b"\x87")
        out += C.encode("magic") + C.encode(list(self.magic))
        out += C.encode("format_version") + C.encode(self.format_version)
        out += C.encode("baseline") + C.encode(self.baseline)
        out += C.encode("checksum_sha256") + C.encode(list(ck))
        out += C.encode("metadata") + C.encode(self.metadata)
        out += C.encode("compressed_world_payload") + enc_u8_array(payload)
        out += C.encode("extension_payloads")
        n = len(ext)
        out += bytes([0x80 | n]) if n < 16 else b"\xde" + struct.pack(">H", n)
        for k in sorted(ext):
            out += C.encode(k) + enc_u8_array(ext[k])
        return bytes(out), payload, ext

    # ---------------------------------------------------------------- saving
    def backup(self):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(BACKUP_DIR, "%s.%s.bak" % (os.path.basename(self.path), stamp))
        shutil.copy2(self.path, dst)
        ri = os.path.join(SLOT_DIR, "slots.ron")
        if os.path.exists(ri): shutil.copy2(ri, os.path.join(BACKUP_DIR, "slots.ron.%s.bak" % stamp))
        return dst

    def save(self, progress=lambda s: None, check_game=True):
        if check_game and game_running():
            raise RuntimeError("Groundwork Simulator is running. Quit the game first, otherwise it will overwrite your edits.")
        data, payload, ext = self.assemble(progress)
        progress("Backing up original...")
        bak = self.backup()
        tmp = self.path + ".editor-tmp"
        progress("Writing...")
        with open(tmp, "wb") as f: f.write(data)
        progress("Verifying written file...")
        try: chk = SaveDoc(tmp).load()
        except Exception as e:
            os.remove(tmp)
            raise RuntimeError("Verification failed (%s). The original save was left untouched." % e)
        if not chk.checksum_ok or chk.ext_bytes != ext:
            os.remove(tmp)
            raise RuntimeError("Verification failed: checksum mismatch. The original save was left untouched.")
        os.replace(tmp, self.path)
        return bak

    # ---------------------------------------------------------------- slots.ron
    @staticmethod
    def update_slot_index(slot_no, save_name=None, difficulty=None, play_time=None):
        p = os.path.join(SLOT_DIR, "slots.ron")
        if not os.path.exists(p): return
        txt = open(p, encoding="utf-8").read()
        m = re.search(r"\n\s*%d: SaveSlotEntry\(.*?\n\s*\)," % slot_no, txt, re.S)
        if not m: return
        blk = m.group(0)
        if save_name is not None:
            blk = re.sub(r'save_name: ".*?",', lambda _: 'save_name: "%s",' % save_name.replace('"', "'"), blk)
        if difficulty is not None:
            blk = re.sub(r"difficulty: Some\(\w+\)", "difficulty: Some(%s)" % difficulty, blk)
        if play_time is not None:
            blk = re.sub(r"play_time_seconds: \d+", "play_time_seconds: %d" % int(play_time), blk)
        with open(p, "w", encoding="utf-8", newline="\n") as f: f.write(txt.replace(m.group(0), blk))
