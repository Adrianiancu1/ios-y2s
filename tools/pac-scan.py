#!/usr/bin/env python3
"""Count ARMv8.3+ instructions in the executable mappings of a dyld shared cache.

  pac-scan.py DIR_OR_FILES...

Takes the main cache file and its subcaches (dyld_shared_cache_arm64e,
.01, .02, ...; .symbols is skipped). Only mappings with execute
permission are scanned. Galaxy S20+ cores (Cortex-A55/A76, Exynos M5)
are ARMv8.2: "hint" PAC instructions run as NOPs there, everything in
the other groups raises an undefined-instruction exception.
"""
import os
import struct
import sys

import numpy as np

# (name, mask, value, runs on ARMv8.2 as NOP?)
PATTERNS = [
    ("PACIASP/PACIBSP/AUTIASP/AUTIBSP/..Z/1716 (hint)", 0xfffffd1f, 0xd503211f, True),
    ("XPACLRI (hint)", 0xffffffff, 0xd50320ff, True),
    ("RETAA/RETAB", 0xfffffbff, 0xd65f0bff, False),
    ("BRAAZ/BRABZ", 0xfffff81f, 0xd61f081f, False),
    ("BLRAAZ/BLRABZ", 0xfffff81f, 0xd63f081f, False),
    ("BRAA/BRAB", 0xfffff800, 0xd71f0800, False),
    ("BLRAA/BLRAB", 0xfffff800, 0xd73f0800, False),
    ("LDRAA/LDRAB", 0xff200400, 0xf8200400, False),
    ("PACIA/PACIB/PACDA/PACDB/AUT* (+Z)", 0xffffc000, 0xdac10000, False),
    ("XPACI/XPACD", 0xfffffbe0, 0xdac143e0, False),
    ("PACGA", 0xffe0fc00, 0x9ac03000, False),
    ("FJCVTZS (v8.3 JSCVT)", 0xfffffc00, 0x1e7e0000, False),
    ("FCMLA vector (v8.3 FCMA)", 0xbf20e400, 0x2e00c400, False),
]


def cache_maps(paths):
    """All mappings of the main cache + subcaches: (addr, size, foff, prot, path)."""
    out = []
    for path in paths:
        with open(path, "rb") as f:
            hdr = f.read(0x200)
        if not hdr.startswith(b"dyld_v1"):
            continue
        moff, mcount = struct.unpack_from("<II", hdr, 0x10)
        with open(path, "rb") as f:
            f.seek(moff)
            raw = f.read(32 * mcount)
        for i in range(mcount):
            a, size, foff, _m, initp = struct.unpack_from("<QQQII", raw, 32 * i)
            out.append((a, size, foff, initp, path))
    return out


def text_ranges(paths):
    """(path, file offset, size) of every __text-like section (S_ATTR_PURE_INSTRUCTIONS)."""
    maps = cache_maps(paths)

    def where(addr):
        for a, size, foff, _p, path in maps:
            if a <= addr < a + size:
                return path, foff + addr - a
        return None, None

    main = paths[0]
    with open(main, "rb") as f:
        hdr = f.read(0x200)
    ioff, icount = struct.unpack_from("<II", hdr, 0x1c0)
    if ioff == 0:
        ioff, icount = struct.unpack_from("<II", hdr, 0x18)
    with open(main, "rb") as f:
        f.seek(ioff)
        imgs = f.read(32 * icount)
    out, handles = [], {}
    for i in range(icount):
        addr = struct.unpack_from("<Q", imgs, 32 * i)[0]
        path, off = where(addr)
        if path is None:
            continue
        fh = handles.setdefault(path, open(path, "rb"))
        fh.seek(off)
        mh = fh.read(32)
        magic, _ct, _st, _ft, ncmds, sizeofcmds = struct.unpack_from("<IiiIII", mh, 0)
        if magic != 0xfeedfacf:
            continue
        cmds = fh.read(sizeofcmds)
        p = 0
        for _ in range(ncmds):
            cmd, size = struct.unpack_from("<II", cmds, p)
            if cmd == 0x19:  # LC_SEGMENT_64
                nsects = struct.unpack_from("<I", cmds, p + 64)[0]
                for k in range(nsects):
                    sp = p + 72 + 80 * k
                    saddr, ssize = struct.unpack_from("<QQ", cmds, sp + 32)
                    flags = struct.unpack_from("<I", cmds, sp + 64)[0]
                    if flags & 0x80000000 and ssize:
                        spath, soff = where(saddr)
                        if spath:
                            out.append((spath, soff, ssize))
            p += size
    for fh in handles.values():
        fh.close()
    return out


def exec_ranges(path):
    with open(path, "rb") as f:
        hdr = f.read(0x20)
    if not hdr.startswith(b"dyld_v1"):
        return []
    moff, mcount = struct.unpack_from("<II", hdr, 0x10)
    with open(path, "rb") as f:
        f.seek(moff)
        raw = f.read(32 * mcount)
    out = []
    for i in range(mcount):
        _addr, size, foff, _maxp, initp = struct.unpack_from("<QQQII", raw, 32 * i)
        if initp & 4 and size:
            out.append((foff, size))
    return out


def files(args):
    for a in args:
        if os.path.isdir(a):
            for n in sorted(os.listdir(a)):
                if n.startswith("dyld_shared_cache_") and not n.endswith((".symbols", ".map", ".atlas")):
                    yield os.path.join(a, n)
        else:
            yield a


def ldraa_class(w):
    """LDRAA/LDRAB: can a plain load of the same size replace it?"""
    imm = ((w >> 22) & 1) << 9 | (w >> 12) & 0x1ff
    if imm & 0x200:
        imm -= 0x400
    off = imm * 8
    if w & (1 << 11):  # pre-index writeback: LDR Xt, [Xn, #simm9]!
        return "writeback ok" if -256 <= off <= 255 else "writeback FAR"
    if off >= 0:
        return "LDR ok"
    return "LDUR ok" if off >= -256 else "negative FAR"


def main():
    args = sys.argv[1:]
    paths = list(files(args))
    ranges = text_ranges(paths)
    counts = {p[0]: 0 for p in PATTERNS}
    ld = {}
    total = 0
    handles = {}
    for path, foff, size in ranges:
        fh = handles.setdefault(path, open(path, "rb"))
        fh.seek(foff)
        chunk = fh.read(size & ~3)
        w = np.frombuffer(chunk[: len(chunk) & ~3], dtype="<u4")
        total += w.size
        for name, mask, val, _hint in PATTERNS:
            hits = (w & np.uint32(mask)) == np.uint32(val)
            n = int(np.count_nonzero(hits))
            counts[name] += n
            if n and name.startswith("LDRAA"):
                for x in w[hits]:
                    c = ldraa_class(int(x))
                    ld[c] = ld.get(c, 0) + 1
    print(f"code sections: {len(ranges)}, instructions: {total}")
    bad = 0
    for name, _m, _v, hint in PATTERNS:
        print(f"{counts[name]:>10}  {'nop ' if hint else 'TRAP'}  {name}")
        if not hint:
            bad += counts[name]
    print(f"{bad:>10}  TRAP  total")
    for c, n in sorted(ld.items()):
        print(f"{n:>10}  LDRAA {c}")


if __name__ == "__main__":
    main()
