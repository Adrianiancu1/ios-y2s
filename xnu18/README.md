# Y2S kernel, iOS 18 generation (xnu-11417.140.69, macOS 15.6)

`patches/zz-y2s-ios18.patch` is the whole Y2S delta (xnu/patches
0001-0092, written for xnu-8796) merged onto xnu-11417.140.69 with a
per-file 3-way merge (base xnu-8796.141.3). Conflicts resolved by hand:
UIO_STACKBUF backport and rtsock/vm_unix (kept upstream), pid_hibernate
(moved to kern_proc.c), trust cache, MakeInc.def, Private.arm64.exports,
files.arm64, arm_init.c, cpu.c, start.s, sleh.c, caches_asm.s,
serial_console.c, prng_random.c (Y2S PRNG kept), entropy.c (upstream
SHA512 kept - check it does not wait for corecrypto), pe_serial.c
(fbcon_probe before the serial-device early return), protos.h, task.c.
Upstream kpc.c kept (our 8796 stub dropped).

CI: .github/workflows/xnu.yml with variant=ios18 macos_version=15.6 (blacktop darwin-xnu-build, macOS 15.6,
Xcode 16). The patch is applied after blacktop's patches/15.0 set.
Known: arm_init.c now reads /chosen dram-base/dram-size (mkadt must
provide them).
