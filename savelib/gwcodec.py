"""Lossless MessagePack codec + Groundwork Simulator save container (GRNDW001) helpers."""
import struct, hashlib, io, os, sys, re

class F32(float):
    """A float that was stored as MessagePack float32."""
    __slots__ = ()

# ---------------------------------------------------------------- decoder
def decode(buf, pos=0):
    """Decode one value starting at pos -> (value, newpos). Keeps f32/f64 and arrays (never bytes)."""
    t = buf[pos]; pos += 1
    if t <= 0x7f: return t, pos
    if t >= 0xe0: return t - 256, pos
    if 0xa0 <= t <= 0xbf:
        n = t & 0x1f; return buf[pos:pos+n].decode("utf-8"), pos + n
    if 0x90 <= t <= 0x9f: return _arr(buf, pos, t & 0x0f)
    if 0x80 <= t <= 0x8f: return _map(buf, pos, t & 0x0f)
    if t == 0xc0: return None, pos
    if t == 0xc2: return False, pos
    if t == 0xc3: return True, pos
    if t == 0xca: return F32(struct.unpack_from(">f", buf, pos)[0]), pos + 4
    if t == 0xcb: return struct.unpack_from(">d", buf, pos)[0], pos + 8
    if t == 0xcc: return buf[pos], pos + 1
    if t == 0xcd: return struct.unpack_from(">H", buf, pos)[0], pos + 2
    if t == 0xce: return struct.unpack_from(">I", buf, pos)[0], pos + 4
    if t == 0xcf: return struct.unpack_from(">Q", buf, pos)[0], pos + 8
    if t == 0xd0: return struct.unpack_from(">b", buf, pos)[0], pos + 1
    if t == 0xd1: return struct.unpack_from(">h", buf, pos)[0], pos + 2
    if t == 0xd2: return struct.unpack_from(">i", buf, pos)[0], pos + 4
    if t == 0xd3: return struct.unpack_from(">q", buf, pos)[0], pos + 8
    if t == 0xd9: n = buf[pos]; pos += 1; return buf[pos:pos+n].decode("utf-8"), pos + n
    if t == 0xda: n = struct.unpack_from(">H", buf, pos)[0]; pos += 2; return buf[pos:pos+n].decode("utf-8"), pos + n
    if t == 0xdb: n = struct.unpack_from(">I", buf, pos)[0]; pos += 4; return buf[pos:pos+n].decode("utf-8"), pos + n
    if t == 0xdc: n = struct.unpack_from(">H", buf, pos)[0]; return _arr(buf, pos + 2, n)
    if t == 0xdd: n = struct.unpack_from(">I", buf, pos)[0]; return _arr(buf, pos + 4, n)
    if t == 0xde: n = struct.unpack_from(">H", buf, pos)[0]; return _map(buf, pos + 2, n)
    if t == 0xdf: n = struct.unpack_from(">I", buf, pos)[0]; return _map(buf, pos + 4, n)
    if t in (0xc4, 0xc5, 0xc6):  # bin
        if t == 0xc4: n = buf[pos]; pos += 1
        elif t == 0xc5: n = struct.unpack_from(">H", buf, pos)[0]; pos += 2
        else: n = struct.unpack_from(">I", buf, pos)[0]; pos += 4
        return bytes(buf[pos:pos+n]), pos + n
    raise ValueError("unsupported msgpack marker 0x%02x at %d" % (t, pos - 1))

def _arr(buf, pos, n):
    out = []
    for _ in range(n):
        v, pos = decode(buf, pos); out.append(v)
    return out, pos

def _map(buf, pos, n):
    out = {}
    for _ in range(n):
        k, pos = decode(buf, pos)
        v, pos = decode(buf, pos); out[k] = v
    return out, pos

# ---------------------------------------------------------------- encoder
def encode(v, out=None):
    o = out if out is not None else bytearray()
    _enc(v, o)
    return o if out is not None else bytes(o)

def _enc(v, o):
    if v is None: o.append(0xc0)
    elif v is True: o.append(0xc3)
    elif v is False: o.append(0xc2)
    elif isinstance(v, F32): o.append(0xca); o += struct.pack(">f", v)
    elif isinstance(v, float): o.append(0xcb); o += struct.pack(">d", v)
    elif isinstance(v, int):
        if v >= 0:
            if v < 0x80: o.append(v)
            elif v < 0x100: o += b"\xcc" + bytes([v])
            elif v < 0x10000: o += b"\xcd" + struct.pack(">H", v)
            elif v < 0x100000000: o += b"\xce" + struct.pack(">I", v)
            else: o += b"\xcf" + struct.pack(">Q", v)
        else:
            if v >= -32: o += struct.pack(">b", v)
            elif v >= -128: o += b"\xd0" + struct.pack(">b", v)
            elif v >= -32768: o += b"\xd1" + struct.pack(">h", v)
            elif v >= -2**31: o += b"\xd2" + struct.pack(">i", v)
            else: o += b"\xd3" + struct.pack(">q", v)
    elif isinstance(v, str):
        b = v.encode("utf-8"); n = len(b)
        if n < 32: o.append(0xa0 | n)
        elif n < 256: o += b"\xd9" + bytes([n])
        elif n < 65536: o += b"\xda" + struct.pack(">H", n)
        else: o += b"\xdb" + struct.pack(">I", n)
        o += b
    elif isinstance(v, (bytes, bytearray)):
        n = len(v)
        if n < 256: o += b"\xc4" + bytes([n])
        elif n < 65536: o += b"\xc5" + struct.pack(">H", n)
        else: o += b"\xc6" + struct.pack(">I", n)
        o += v
    elif isinstance(v, (list, tuple)):
        n = len(v)
        if n < 16: o.append(0x90 | n)
        elif n < 65536: o += b"\xdc" + struct.pack(">H", n)
        else: o += b"\xdd" + struct.pack(">I", n)
        for x in v: _enc(x, o)
    elif isinstance(v, dict):
        n = len(v)
        if n < 16: o.append(0x80 | n)
        elif n < 65536: o += b"\xde" + struct.pack(">H", n)
        else: o += b"\xdf" + struct.pack(">I", n)
        for k, x in v.items(): _enc(k, o); _enc(x, o)
    else:
        raise TypeError("cannot encode %r" % type(v))

# ---------------------------------------------------------------- skipping (index big maps)
def skip(buf, pos):
    """Return end position of the value at pos without building it (fast for containers of scalars)."""
    t = buf[pos]; pos += 1
    if t <= 0x7f or t >= 0xe0 or t in (0xc0, 0xc2, 0xc3): return pos
    if 0xa0 <= t <= 0xbf: return pos + (t & 0x1f)
    if 0x90 <= t <= 0x9f: n = t & 0x0f; return _skipn(buf, pos, n)
    if 0x80 <= t <= 0x8f: n = t & 0x0f; return _skipn(buf, pos, n * 2)
    sizes = {0xca: 4, 0xcb: 8, 0xcc: 1, 0xcd: 2, 0xce: 4, 0xcf: 8, 0xd0: 1, 0xd1: 2, 0xd2: 4, 0xd3: 8}
    if t in sizes: return pos + sizes[t]
    if t == 0xd9: return pos + 1 + buf[pos]
    if t == 0xda: return pos + 2 + struct.unpack_from(">H", buf, pos)[0]
    if t == 0xdb: return pos + 4 + struct.unpack_from(">I", buf, pos)[0]
    if t == 0xc4: return pos + 1 + buf[pos]
    if t == 0xc5: return pos + 2 + struct.unpack_from(">H", buf, pos)[0]
    if t == 0xc6: return pos + 4 + struct.unpack_from(">I", buf, pos)[0]
    if t == 0xdc: return _skipn(buf, pos + 2, struct.unpack_from(">H", buf, pos)[0])
    if t == 0xdd: return _skipn(buf, pos + 4, struct.unpack_from(">I", buf, pos)[0])
    if t == 0xde: return _skipn(buf, pos + 2, struct.unpack_from(">H", buf, pos)[0] * 2)
    if t == 0xdf: return _skipn(buf, pos + 4, struct.unpack_from(">I", buf, pos)[0] * 2)
    raise ValueError("skip: bad marker 0x%02x" % t)

def _skipn(buf, pos, n):
    for _ in range(n): pos = skip(buf, pos)
    return pos
