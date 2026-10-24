#!/usr/bin/env python3
"""Rewrite arm64e (ARMv8.3 pointer authentication) code for ARMv8.2 cores.

  pac-strip.py DIR_OR_FILES...      patch a dyld shared cache IN PLACE
  pac-strip.py --selftest

Takes the same inputs as pac-scan.py (main cache file + subcaches; work
on a copy). Only __text-type sections (S_ATTR_PURE_INSTRUCTIONS) of the
cache images are touched. Pointers are never signed afterwards, so every
authenticate/strip becomes a no-op and every authenticated branch/load
becomes its plain form, same size:

  RETAA/RETAB              -> RET
  BRAA(Z)/BRAB(Z) Xn       -> BR Xn
  BLRAA(Z)/BLRAB(Z) Xn     -> BLR Xn
  LDRAA/LDRAB Xt,[Xn,#o]   -> LDR / LDUR / LDR pre-index (when the offset fits)
  PACIA..AUTDB (+Z), XPAC* -> NOP
  PACGA Xd,Xn,Xm           -> EOR Xd,Xn,Xm (deterministic; signer and checker agree)

Hint-space PAC instructions (PACIBSP, AUTIBSP, ...) already execute as NOPs
on ARMv8.2 and are left alone. FJCVTZS and LDRAA forms whose offset does
not fit are left for the kernel's undefined-instruction handler; their
count is printed.
"""
import importlib.util
import os
import sys

import numpy as np

NOP = 0xd503201f


def rewrite(w):
    """Rewrite a uint32 numpy array in place. Returns (counts dict, leftover dict)."""
    u = np.uint32
    m_ret = (w & u(0xfffffbff)) == u(0xd65f0bff)
    m_brz = (w & u(0xfffff81f)) == u(0xd61f081f)
    m_blrz = (w & u(0xfffff81f)) == u(0xd63f081f)
    m_br = (w & u(0xfffff800)) == u(0xd71f0800)
    m_blr = (w & u(0xfffff800)) == u(0xd73f0800)
    m_ldr = (w & u(0xff200400)) == u(0xf8200400)
    m_pac = (w & u(0xffffc000)) == u(0xdac10000)
    m_xpac = (w & u(0xfffffbe0)) == u(0xdac143e0)
    m_pacga = (w & u(0xffe0fc00)) == u(0x9ac03000)
    m_fjcvt = (w & u(0xfffffc00)) == u(0x1e7e0000)
    rn = w & u(0x3e0)

    counts = {
        "RETAA/RETAB": int(m_ret.sum()), "BRAAZ/BRABZ": int(m_brz.sum()),
        "BLRAAZ/BLRABZ": int(m_blrz.sum()), "BRAA/BRAB": int(m_br.sum()),
        "BLRAA/BLRAB": int(m_blr.sum()), "PAC*/AUT*": int(m_pac.sum()),
        "XPAC*": int(m_xpac.sum()), "PACGA": int(m_pacga.sum()),
    }
    w[m_ret] = u(0xd65f03c0)
    w[m_brz | m_br] = u(0xd61f0000) | rn[m_brz | m_br]
    w[m_blrz | m_blr] = u(0xd63f0000) | rn[m_blrz | m_blr]
    w[m_pac | m_xpac] = u(NOP)
    w[m_pacga] = u(0xca000000) | (w[m_pacga] & u(0x1f03ff))

    left = {"FJCVTZS": int(m_fjcvt.sum()), "LDRAA far": 0}
    n_ld = 0
    for i in np.nonzero(m_ldr)[0]:
        x = int(w[i])
        imm = ((x >> 22) & 1) << 9 | (x >> 12) & 0x1ff
        if imm & 0x200:
            imm -= 0x400
        off = imm * 8
        regs = x & 0x3ff
        if x & (1 << 11):                       # pre-index writeback
            if -256 <= off <= 255:
                w[i] = 0xf8400c00 | (off & 0x1ff) << 12 | regs
                n_ld += 1
                continue
        elif off >= 0:
            w[i] = 0xf9400000 | (off // 8) << 10 | regs
            n_ld += 1
            continue
        elif off >= -256:
            w[i] = 0xf8400000 | (off & 0x1ff) << 12 | regs
            n_ld += 1
            continue
        left["LDRAA far"] += 1
    counts["LDRAA/LDRAB"] = n_ld
    return counts, left


def selftest():
    # (arm64e source, expected ARMv8.2 result), encodings from llvm-mc
    cases = [
        (0xd65f0fff, 0xd65f03c0),   # retab              -> ret
        (0xd61f0a1f, 0xd61f0200),   # braaz x16          -> br x16
        (0xd63f0d1f, 0xd63f0100),   # blrabz x8          -> blr x8
        (0xd71f0a11, 0xd61f0200),   # braa x16, x17      -> br x16
        (0xd73f0d09, 0xd63f0100),   # blrab x8, x9       -> blr x8
        (0xf8201420, 0xf9400420),   # ldraa x0,[x1,#8]   -> ldr x0,[x1,#8]
        (0xf87fe420, 0xf85f0020),   # ldraa x0,[x1,#-16] -> ldur x0,[x1,#-16]
        (0xf8200e30, 0xf8400e30),   # ldraa x16,[x17,#0]! -> ldr x16,[x17,#0]!
        (0x9ac53083, 0xca050083),   # pacga x3,x4,x5     -> eor x3,x4,x5
        (0xdac10230, NOP),          # pacia x16, x17
        (0xdac13fe2, NOP),          # autdzb x2
        (0xdac143e0, NOP),          # xpaci x0
        (0xd503237f, 0xd503237f),   # pacibsp (hint) untouched
        (0x1e7e0020, 0x1e7e0020),   # fjcvtzs untouched (kernel emulates)
        (0xd65f03c0, 0xd65f03c0),   # ret untouched
        (0xf9400420, 0xf9400420),   # ldr untouched
    ]
    w = np.array([c[0] for c in cases], dtype="<u4")
    rewrite(w)
    bad = [(hex(a), hex(b), hex(int(r))) for (a, b), r in zip(cases, w) if int(r) != b]
    print("selftest:", "OK" if not bad else bad)
    return not bad


def main():
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(0 if selftest() else 1)
    if not selftest():
        sys.exit("selftest failed, not touching anything")
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("pacscan", os.path.join(here, "pac-scan.py"))
    scan = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scan)
    paths = list(scan.files(sys.argv[1:]))
    ranges = scan.text_ranges(paths)
    total, left = {}, {}
    maps = {}
    for path, foff, size in ranges:
        mm = maps.get(path)
        if mm is None:
            mm = maps[path] = np.memmap(path, dtype="u1", mode="r+")
        w = mm[foff:foff + (size & ~3)].view("<u4")
        c, l = rewrite(w)
        for k, v in c.items():
            total[k] = total.get(k, 0) + v
        for k, v in l.items():
            left[k] = left.get(k, 0) + v
    for mm in maps.values():
        mm.flush()
    print(f"code sections: {len(ranges)}")
    for k, v in total.items():
        print(f"{v:>10}  rewritten  {k}")
    for k, v in left.items():
        print(f"{v:>10}  left for kernel emulation  {k}")


if __name__ == "__main__":
    main()
