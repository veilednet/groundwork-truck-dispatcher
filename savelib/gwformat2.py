"""Save format 2 (game 0.1.3.1+, header format_version 2): read it, and append a commit to it.

Layout (worked out 2026-10-30 from real saves):
  msgpack {magic: b"GRNDW001", format_version: 2, baseline: str}  then the 8 bytes b"GRNDW002"
  then an append-only log. Every autosave appends one commit:
    chunks     zstd frames (no content size, no checksum); each decodes to <= 64 KiB of the world msgpack
    envelope   msgpack {version: 2, baseline, metadata, extensions, body: [chunk refs], fine: {...}}
    trailer    96 bytes: b"GWCOMMIT", u64 envelope offset, u64 envelope length, u64 commit number,
               sha256(envelope), sha256(the first 64 trailer bytes)
  The newest trailer (at the end of the file) points at the current envelope. Its body lists the chunks whose decoded
  bytes, concatenated, are the world (the same msgpack map as format 1, minus fine_terrain). The game cuts the world
  into: the 3-byte map header, then every section (key + value) on its own, split into 64 KiB pieces.
  A chunk ref is {offset, length, decoded, encoded, raw_hash, compressed_hash, base}:
    raw_hash = sha256(decoded chunk), compressed_hash = sha256(zstd frame), encoded = size the frame decodes to.
    When base is set (another chunk ref) the frame decodes to a patch list (u32 offset, u32 length, bytes) applied
    over the base chunk, and the result is cut to `decoded` bytes (patches can shrink a chunk).
  extensions = {name: array of u8} (campaign JSON, as in format 1).
  fine = {schema, cell_size, tile_size, tile_cells, tiles: [[tileX, tileZ, revision, chunk ref], ...]}: the dug-ground
  detail that format 1 kept in the world's fine_terrain section, now one binary "GWTILE02" chunk per 12 m tile.

Writing: append_commit() adds a commit the same way the game does. Unchanged chunks keep their refs; changed ones are
written as whole frames (base nil). fine tiles are kept as they are. (Writing format 1 instead would mean converting
the GWTILE02 tiles back into fine_terrain msgpack, which is not worked out, so dug-ground detail would be lost.)
"""
import hashlib
import io
import struct

import msgpack
import zstandard

import gwcodec as C

MAGIC2 = b"GRNDW002"
TRAILER = b"GWCOMMIT"
TRAILER_LEN = 96
CHUNK = 65536


def is_v2(src):
    """True for a format 2 save (path or bytes)."""
    if isinstance(src, (bytes, bytearray, memoryview)):
        head = bytes(src[:256])
    else:
        with open(src, "rb") as f:
            head = f.read(256)
    try:
        h, p = C.decode(head, 0)
        return isinstance(h, dict) and h.get("format_version") == 2 and head[p:p + 8] == MAGIC2
    except Exception:
        return False


def find_commit(raw):
    """(trailer position, envelope offset, envelope length, commit number) of the newest whole commit."""
    c = len(raw) - TRAILER_LEN
    if c < 0 or raw[c:c + 8] != TRAILER:
        c = raw.rfind(TRAILER)                      # file still being written: fall back to the last whole commit
    while c >= 0:
        off, ln, no = struct.unpack_from("<QQQ", raw, c + 8)
        if (c + TRAILER_LEN <= len(raw) and off + ln <= c
                and hashlib.sha256(raw[c:c + 64]).digest() == raw[c + 64:c + 96]):
            return c, off, ln, no
        c = raw.rfind(TRAILER, 0, c)
    raise ValueError("no commit found in the save")


def read_envelope(raw, verify=True):
    """(header dict, envelope dict, commit number) without decoding the world (~50 ms)."""
    header, p = C.decode(raw, 0)
    if not isinstance(header, dict) or header.get("format_version") != 2 or raw[p:p + 8] != MAGIC2:
        raise ValueError("not a format 2 save")
    c, off, ln, no = find_commit(raw)
    env = raw[off:off + ln]
    if verify and hashlib.sha256(env).digest() != raw[c + 32:c + 64]:
        raise ValueError("save envelope hash mismatch")
    envelope, ep = C.decode(env, 0)
    if ep != ln:
        raise ValueError("save envelope has trailing bytes")
    return header, envelope, no


def ext_bytes(envelope):
    return {k: bytes(v) for k, v in (envelope.get("extensions") or {}).items()}


def verify_frames(raw, envelope):
    """Check every referenced zstd frame against its compressed_hash (no decompression, ~0.1 s). Returns a problem or None."""
    refs = list(envelope["body"]) + [t[3] for t in (envelope.get("fine") or {}).get("tiles", [])]
    seen = set()
    while refs:
        ch = refs.pop()
        if ch["offset"] in seen: continue
        seen.add(ch["offset"])
        if ch["offset"] + ch["length"] > len(raw):
            return "chunk at %d runs past the end of the file" % ch["offset"]
        if hashlib.sha256(raw[ch["offset"]:ch["offset"] + ch["length"]]).digest() != bytes(ch["compressed_hash"]):
            return "chunk at %d failed its hash check" % ch["offset"]
        if ch["base"] is not None: refs.append(ch["base"])
    return None


class SaveV2:
    """A format 2 save. .world is the newest commit's world msgpack (~240 MB, ~0.5 s)."""

    def __init__(self, path=None, raw=None):
        self.path = path
        if raw is None:
            with open(path, "rb") as f:
                raw = f.read()
        self.raw = raw
        self.header, self.envelope, self.commit = read_envelope(raw)
        self.metadata = self.envelope.get("metadata", {})
        self._z = zstandard.ZstdDecompressor()
        self._cache = {}
        self._world = None

    def chunk(self, ch):
        key = ch["offset"]
        if key in self._cache: return self._cache[key]
        d = self._z.decompress(self.raw[ch["offset"]:ch["offset"] + ch["length"]], max_output_size=ch["encoded"])
        if ch["base"] is not None:
            out = bytearray(self.chunk(ch["base"]))
            if len(out) < ch["decoded"]:             # chunk grew past its base: game zero-fills
                out += bytes(ch["decoded"] - len(out))
            i = 0
            while i < len(d):
                o, n = struct.unpack_from("<II", d, i)
                i += 8
                out[o:o + n] = d[i:i + n]
                i += n
            d = bytes(out[:ch["decoded"]])
        if len(d) != ch["decoded"] or hashlib.sha256(d).digest() != bytes(ch["raw_hash"]):
            raise ValueError("save chunk at %d failed its hash check" % ch["offset"])
        if ch["decoded"] <= CHUNK: self._cache[key] = d      # patch bases are reused; keep them
        return d

    _chunk = chunk                                   # old name (MapMaker)

    @property
    def world(self):
        if self._world is None:
            self._world = b"".join(self.chunk(ch) for ch in self.envelope["body"])
            self._cache.clear()
        return self._world

    def section(self, name):
        """Unpack one top-level world entry (e.g. "terrain", "authored_map")."""
        u = msgpack.Unpacker(io.BytesIO(self.world), raw=False, strict_map_key=False, max_buffer_size=2 ** 31)
        for _ in range(u.read_map_header()):
            k = u.unpack()
            if k == name:
                return u.unpack()
            u.skip()
        raise KeyError(name)

    def authored_head(self, keys=("map_id", "map_name", "content_hash")):
        """The first entries of authored_map without unpacking its large grids."""
        u = msgpack.Unpacker(io.BytesIO(self.world), raw=False, strict_map_key=False, max_buffer_size=2 ** 31)
        for _ in range(u.read_map_header()):
            k = u.unpack()
            if k != "authored_map":
                u.skip()
                continue
            head = {}
            for _ in range(u.read_map_header()):
                kk = u.unpack()
                if kk not in keys:
                    break
                head[kk] = u.unpack()
            return head
        raise KeyError("authored_map")


def split_world(world, idx, order):
    """The game's chunking: (start, end) of the map header, then each section cut into 64 KiB pieces."""
    first = idx[order[0]][0] if order else len(world)
    spans = [(0, first)]
    for k in order:
        s, _, e = idx[k]
        while s < e:
            spans.append((s, min(s + CHUNK, e)))
            s += CHUNK
    return spans


def append_commit(raw, envelope, world, idx, order, metadata=None, extensions=None, level=3):
    """File bytes = raw + one new commit holding `world` (indexed by idx/order as in SaveDoc).

    A chunk whose bytes equal any chunk of the current commit (same raw_hash) keeps that ref; everything else becomes
    a new whole zstd frame. metadata / extensions (name -> bytes) replace the envelope's."""
    c, _, _, no = find_commit(raw)
    end = c + TRAILER_LEN                            # drops a half-written commit after the newest whole one
    old = {(bytes(ch["raw_hash"]), ch["decoded"]): ch for ch in envelope["body"]}
    zc = zstandard.ZstdCompressor(level=level, write_content_size=False, write_checksum=False)
    out = bytearray()
    body = []
    for s, e in split_world(world, idx, order):
        piece = world[s:e]
        h = hashlib.sha256(piece).digest()
        ref = old.get((h, e - s))
        if ref is not None:
            body.append(ref)
            continue
        frame = zc.compress(piece)
        body.append({"offset": end + len(out), "length": len(frame), "decoded": len(piece), "encoded": len(piece),
                     "raw_hash": list(h), "compressed_hash": list(hashlib.sha256(frame).digest()), "base": None})
        out += frame
    env = dict(envelope)                             # same key order as the game's envelope
    env["body"] = body
    if metadata is not None: env["metadata"] = metadata
    if extensions is not None:
        exts = dict(env.get("extensions") or {})
        for k in exts:
            if k not in extensions: del exts[k]
        for k, v in extensions.items(): exts[k] = list(v)
        env["extensions"] = exts
    eb = bytes(C.encode(env))
    env_off = end + len(out)
    out += eb
    tr = TRAILER + struct.pack("<QQQ", env_off, len(eb), no + 1) + hashlib.sha256(eb).digest()
    out += tr + hashlib.sha256(tr).digest()
    return bytes(raw[:end]) + bytes(out)
