/*
 * y2s boot shim for the Galaxy S20+ (SM-G986B, Exynos 990).
 *
 * S-Boot (unchanged) loads boot.img like an Android kernel: this image is
 * the "kernel", the boot.img ramdisk is the xnu payload (tools/mkpayload.py).
 * Entry at EL1 (Samsung's hypervisor owns EL2), MMU and D-cache off,
 * x0 = DTB with /chosen linux,initrd-start/end. loader.c then acts as iBoot.
 *
 * On any error the reason stays on screen for 60 s, then the phone resets.
 * An unexpected exception paints a blue panic screen.
 */
#include <stdint.h>
#include "console.h"
#include "loader.h"

#define PSCI_SYSTEM_RESET	0x84000009UL
/* PMU SWRESET (Exynos 990 PMU 0x15860000 + 0x3a00, bit 1) */
#define PMU_SWRESET		0x15863a00UL

#define rsr(r) ({ uint64_t _v; __asm__ volatile("mrs %0, " r : "=r"(_v)); _v; })

static const char *stage;	/* set at runtime: no absolute pointers in data */

static uint64_t smc(uint64_t fn, uint64_t a1, uint64_t a2, uint64_t a3)
{
	register uint64_t x0 __asm__("x0") = fn;
	register uint64_t x1 __asm__("x1") = a1;
	register uint64_t x2 __asm__("x2") = a2;
	register uint64_t x3 __asm__("x3") = a3;
	__asm__ volatile("smc #0"
			 : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x3)
			 :
			 : "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11",
			   "x12", "x13", "x14", "x15", "x16", "x17", "memory");
	return x0;
}

static uint64_t now(void)
{
	__asm__ volatile("isb" ::: "memory");
	return rsr("cntpct_el0");
}

static void delay_s(unsigned s)
{
	uint64_t f = rsr("cntfrq_el0");
	if (f == 0)
		f = 26000000;	/* S20 timer clock (live.dts) */
	uint64_t end = now() + f * s;
	while (now() < end)
		;
}

static void __attribute__((noreturn)) reset(unsigned after_s)
{
	con_skip();
	con_str("RESET", after_s >= 60 ? "in 60s" : "in 20s");
	delay_s(after_s);
	smc(PSCI_SYSTEM_RESET, 0, 0, 0);
	/* PSCI returned: direct PMU software reset. */
	volatile uint32_t *sw = (volatile uint32_t *)PMU_SWRESET;
	*sw |= 2;
	__asm__ volatile("dsb sy" ::: "memory");
	for (;;)
		__asm__ volatile("wfe");
}

void exc_handler(uint64_t slot, uint64_t *regs)
{
	con_clear(BLUE);
	/* One string + offset: a switch can become a table of absolute pointers. */
	static const char kinds[] = "SYNC\0\0\0IRQ\0\0\0\0FIQ\0\0\0\0SERROR";
	con_str("PANIC", kinds + 7 * (slot & 3));
	con_hex2("SLOT", "n=", slot, "cpu=", rsr("mpidr_el1"));
	con_str("STAGE", stage);
	con_hex("ESR", rsr("esr_el1"));
	con_hex("ELR", rsr("elr_el1"));
	con_hex("FAR", rsr("far_el1"));
	con_hex("SPSR", rsr("spsr_el1"));
	con_hex("X0", regs[0]);
	con_hex("X1", regs[1]);
	con_hex("X30", regs[29]);	/* stashed lr is just above the frame */
	reset(60);
}

static uint64_t hvc(uint64_t fn, uint64_t a1, uint64_t a2, uint64_t a3)
{
	register uint64_t x0 __asm__("x0") = fn;
	register uint64_t x1 __asm__("x1") = a1;
	register uint64_t x2 __asm__("x2") = a2;
	register uint64_t x3 __asm__("x3") = a3;
	__asm__ volatile("hvc #0"
			 : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x3)
			 :
			 : "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11",
			   "x12", "x13", "x14", "x15", "x16", "x17", "memory");
	return x0;
}

/*
 * SMP probe (boot-arg-free: built in, runs before the loader). A started
 * core records its MPIDR and exception level, then switches itself off
 * again through the same conduit, so xnu can CPU_ON it later.
 */
#define PSCI_CPU_ON	0xc4000003UL
#define PSCI_CPU_OFF	0x84000002UL
extern char secondary_entry[];
static volatile uint64_t sec_seen[8], sec_el[8], sec_conduit;

void secondary_main(uint64_t idx)
{
	idx &= 7;
	sec_el[idx] = rsr("CurrentEL") >> 2;
	sec_seen[idx] = rsr("mpidr_el1") | (1ULL << 63);
	__asm__ volatile("dsb sy" ::: "memory");
	if (sec_conduit)
		hvc(PSCI_CPU_OFF, 0, 0, 0);
	else
		smc(PSCI_CPU_OFF, 0, 0, 0);
	for (;;)
		__asm__ volatile("wfe");
}

static void wait_ms(unsigned ms)
{
	uint64_t f = rsr("cntfrq_el0");
	if (f == 0)
		f = 26000000;
	uint64_t end = now() + f / 1000 * ms;
	while (now() < end)
		;
}

/* Samsung RKP (include/linux/rkp.h struct rkp_init, non-FASTUH IDs). */
static struct {
	uint32_t magic;
	uint64_t vmalloc_start, vmalloc_end, init_mm_pgd, id_map_pgd, zero_pg_addr;
	uint64_t rkp_pgt_bitmap, rkp_dbl_bitmap;
	uint32_t rkp_bitmap_size, no_fimc_verify;
	uint64_t fimc_phys_addr, _text, _etext, extra_memory_addr;
	uint32_t extra_memory_size;
	uint64_t physmap_addr, _srodata, _erodata;
	uint32_t large_memory;
	uint64_t tramp_pgd, tramp_valias;
} rkp_init_data;
static uint8_t rkp_pgd[4096] __attribute__((aligned(4096)));
static uint8_t rkp_idmap[4096] __attribute__((aligned(4096)));
static uint8_t rkp_zero[4096] __attribute__((aligned(4096)));
static uint8_t rkp_text[4096] __attribute__((aligned(4096)));
static uint8_t rkp_ro[4096] __attribute__((aligned(4096)));

static unsigned uhlog_end(const volatile char *log)
{
	unsigned last = 0;
	for (unsigned i = 0; i < 0x40000; i++)
		if (log[i])
			last = i + 1;
	return last;
}

static void __attribute__((unused)) smp_probe(void)
{
	uintptr_t entry = (uintptr_t)secondary_entry;
	con_hex("SEC ENTRY", entry);
	/* Real PSCI 1.0 answers 0x10000; a stub that swallows calls answers 0. */
	con_hex2("PSCI VER", "hvc=", (uint32_t)hvc(0x84000000UL, 0, 0, 0),
		 "smc=", (uint32_t)smc(0x84000000UL, 0, 0, 0));
	con_hex2("CPU_ON F", "hvc=", (uint32_t)hvc(0x8400000aUL, PSCI_CPU_ON, 0, 0),
		 "smc=", (uint32_t)smc(0x8400000aUL, PSCI_CPU_ON, 0, 0));
	con_hex2("SMCCC", "hvc=", (uint32_t)hvc(0x80000000UL, 0, 0, 0),
		 "smc=", (uint32_t)smc(0x80000000UL, 0, 0, 0));
	/*
	 * Boot MPIDR is 0x81000000: MT=1, so the core number is in Aff1. Try
	 * the DT-style target (core in Aff0) and the MT-style one (Aff1), each
	 * through hvc (the DT's PSCI conduit) and smc.
	 */
	/* Probe 7: RKP_START (as Samsung's start_kernel does) before CPU_ON. */
	const volatile char *log = (const volatile char *)0xC1500000UL;
	unsigned end0 = uhlog_end(log);
	static uint64_t ro_bm, dbl_bm;
	uint32_t r1 = 0xdead, r2 = 0xdead;
	rkp_init_data.magic = 0x5afe0001;
	rkp_init_data.vmalloc_start = 0xffffff8008000000UL;
	rkp_init_data.vmalloc_end = 0xffffffbebfff0000UL;
	rkp_init_data.init_mm_pgd = (uint64_t)rkp_pgd;
	rkp_init_data.id_map_pgd = (uint64_t)rkp_idmap;
	rkp_init_data.zero_pg_addr = (uint64_t)rkp_zero;
	rkp_init_data.no_fimc_verify = 1;
	rkp_init_data._text = (uint64_t)rkp_text;
	rkp_init_data._etext = (uint64_t)rkp_text + 4096;
	rkp_init_data._srodata = (uint64_t)rkp_ro;
	rkp_init_data._erodata = (uint64_t)rkp_ro + 4096;
	__asm__ volatile("dsb sy" ::: "memory");
	con_hex("RKP 3 strt", (uint64_t)&rkp_init_data);
	uint32_t r3 = (uint32_t)hvc(0x83000201UL, (uint64_t)&rkp_init_data, 0, 0);
	con_hex2("RKP bm", "ro=", r1, "dbl=", r2);
	con_hex2("RKP ptr", "ro=", (uint32_t)ro_bm, "dbl=", (uint32_t)dbl_bm);
	con_hex2("RKP STRT", "rc=", r3, "log+=", uhlog_end(log) - end0);
	uint32_t a0 = (uint32_t)hvc(0x84000004UL, 1, 0, 0);
	uint32_t rc = (uint32_t)hvc(PSCI_CPU_ON, 1, entry, 1);
	wait_ms(1000);
	uint32_t a1 = (uint32_t)hvc(0x84000004UL, 1, 0, 0);
	con_hex2("CPU1 on", "rc=", rc, "seen=", (uint32_t)sec_seen[1]);
	con_hex2("CPU1 aff", "pre=", a0, "1s=", a1);
	unsigned end1 = uhlog_end(log);
	con_hex2("UHLOG", "before=", end0, "after=", end1);
	unsigned from = end1 - end0 > 1600 ? end1 - 1600 : end0;
	con_text(log + from, end1 - from, 34);
	reset(60);
}

void shim_main(uint64_t dtb)
{
	stage = "loader";
	con_clear(RED);
	con_str("SHIM", "y2s (Galaxy S20+): xnu loader");
	con_hex2("EL", "cur=", rsr("CurrentEL") >> 2, "mpidr=", rsr("mpidr_el1"));
	con_hex2("DTB", "at=", dtb, "magic=", dtb ? __builtin_bswap32(*(volatile uint32_t *)dtb) : 0);
	con_hex2("TIMER", "frq=", rsr("cntfrq_el0"), 0, 0);
	/*
	 * Touch I2C lines (gpp5-6 SCL / gpp5-7 SDA, PERIC1 pinctrl 0x10730000)
	 * as S-Boot left them, then as plain inputs without and with pull-ups.
	 * Under xnu the touch IC holds both low; 1 = idle bus.
	 */
	if (rsr("cntfrq_el0") != 62500000) {
		volatile uint32_t *con = (volatile uint32_t *)0x10730000UL;
		volatile uint32_t *dat = (volatile uint32_t *)0x10730004UL;
		volatile uint32_t *pud = (volatile uint32_t *)0x10730008UL;
		uint32_t c0 = *con, p0 = *pud, d0 = (*dat >> 6) & 3;
		*con = c0 & 0x00ffffffu;
		*pud = p0 & 0x00ffffffu;
		__asm__ volatile("dsb sy" ::: "memory");
		wait_ms(1);
		uint32_t d1 = (*dat >> 6) & 3;
		*pud = (p0 & 0x00ffffffu) | 0x33000000u;
		__asm__ volatile("dsb sy" ::: "memory");
		wait_ms(1);
		uint32_t d2 = (*dat >> 6) & 3;
		*con = c0;
		*pud = p0;
		__asm__ volatile("dsb sy" ::: "memory");
		con_hex2("TSP CON", "con=", c0, "pud=", p0);
		con_hex2("TSP LINES", "asis/nopull=", d0 << 4 | d1, "pullup=", d2);

		/* Boot CPU clock: PMU cycle counter over 100 ms of the 26 MHz timer. */
		uint64_t pmcr;
		__asm__ volatile("mrs %0, pmcr_el0" : "=r"(pmcr));
		__asm__ volatile("msr pmcr_el0, %0" :: "r"(pmcr | 1));             /* E */
		__asm__ volatile("msr pmcntenset_el0, %0" :: "r"(1UL << 31));       /* C */
		__asm__ volatile("isb");
		uint64_t k0, k1, t0, t1;
		__asm__ volatile("mrs %0, cntpct_el0" : "=r"(t0));
		__asm__ volatile("mrs %0, pmccntr_el0" : "=r"(k0));
		do {
			__asm__ volatile("mrs %0, cntpct_el0" : "=r"(t1));
		} while (t1 - t0 < 2600000);
		__asm__ volatile("mrs %0, pmccntr_el0" : "=r"(k1));
		con_hex2("CPU MHZ", "hex=", (uint32_t)((k1 - k0) / 100000), "cyc=", (uint32_t)(k1 - k0));
	}
	con_skip();
	stage = "smp probe";
	/* smp_probe(); SMP blocked by uH, see docs/S20.md */
	con_skip();
	stage = "loader";
	const char *why = loader_boot(dtb);
	/* Only reached on error. */
	con_str("LOADER", why);
	stage = "done";
	reset(60);
}
