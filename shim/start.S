/*
 * y2s boot shim entry: arm64 Linux Image header so Samsung S-Boot treats us
 * like a kernel. Entered at EL1, MMU/D-cache off, x0 = DTB pointer.
 * text_offset/flags as in the S20 bare-metal tests that S-Boot ran.
 */
	.section .text.head, "ax"
	.globl _head
_head:
	b	entry0			/* code0 */
	.long	0			/* code1 */
	.quad	0x80000			/* text_offset */
	.quad	_end			/* image_size (build.sh rewrites it) */
	.quad	0x2			/* flags: LE, 4K pages */
	.quad	0			/* res2 */
	.quad	0			/* res3 */
	.quad	0			/* res4 */
	.ascii	"ARM\x64"		/* magic */
	.long	0			/* res5 (PE offset) */

/*
 * Boot diagnostics, laid out like the bare-metal tests S-Boot ran: code
 * right behind the header, constants built in registers (no literal pool,
 * no memory loads), before anything else. Rows of S-Boot's framebuffer
 * (0xf1000000, 1440 px, 5760 bytes per row):
 *   rows   0- 63  green   this code runs (then DECON is unmasked, see below)
 *   rows  64-127  blue    a word at the far end of the shim image reads back
 *                         right (red: S-Boot did not load/clean all of it)
 *   rows 128-191  blue    a payload follows the image (yellow: none)
 * then ~3 s pause so the bands can be seen, then the C loader.
 */
entry0:
	msr	daifset, #0xf
	mov	x19, x0			/* DTB pointer from S-Boot */
	movz	x1, #0xf100, lsl #16	/* framebuffer */
	movz	w3, #0xff00, lsl #16
	movk	w3, #0xff00		/* green */
	bl	band
	/*
	 * The S20 panel only shows framebuffer writes once DECON's hardware
	 * trigger is unmasked (bare-metal test 12 vs the same image without
	 * these writes). Exynos 9830 DECON0 (regs from Samsung cal_9830):
	 *   0x19050070 trigger control: set HW trigger enable (bit 0), clear
	 *              the trigger mask (bit 4)
	 *   0x19050060 shadow update request: global update (bit 31)
	 * Afterwards the panel follows the framebuffer, also for xnu later.
	 */
	/*
	 * Not under QEMU virt (CNTFRQ 62.5 MHz), which has no device there.
	 * Do not test for the S20's 26 MHz instead: its firmware may leave
	 * CNTFRQ unset (the live DT gives clock-frequency explicitly).
	 */
	mrs	x7, cntfrq_el0
	movz	x8, #0x3b9, lsl #16
	movk	x8, #0xaca0		/* 62500000 */
	cmp	x7, x8
	b.eq	6f
	movz	x7, #0x1905, lsl #16
	movk	x7, #0x0070
	ldr	w8, [x7]
	orr	w8, w8, #0x1
	bic	w8, w8, #0x10
	str	w8, [x7]
	movz	x7, #0x1905, lsl #16
	movk	x7, #0x0060
	ldr	w8, [x7]
	orr	w8, w8, #0x80000000
	str	w8, [x7]
	dsb	sy
6:
	adr	x4, far_probe
	ldr	w5, [x4]
	movz	w6, #0x5a5a, lsl #16
	movk	w6, #0xa5a5
	movz	w3, #0xffff, lsl #16	/* red */
	cmp	w5, w6
	b.ne	1f
	movz	w3, #0xff00, lsl #16
	movk	w3, #0x00ff		/* blue */
1:	bl	band
	adr	x4, _end
	ldr	w5, [x4]
	movz	w6, #0x5853, lsl #16
	movk	w6, #0x3259		/* "Y2SX" little endian */
	movz	w3, #0xffff, lsl #16
	movk	w3, #0xff00		/* yellow */
	cmp	w5, w6
	b.ne	2f
	movz	w3, #0xff00, lsl #16
	movk	w3, #0x00ff		/* blue */
2:	bl	band
	/* ~3 s: CNTFRQ is 26 MHz on the S20 (78M ticks) */
	mrs	x4, cntfrq_el0
	cbnz	x4, 3f
	movz	x4, #0x18c, lsl #16
	movk	x4, #0xba80
3:	add	x4, x4, x4, lsl #1
	isb
	mrs	x5, cntpct_el0
	add	x4, x4, x5
4:	isb
	mrs	x5, cntpct_el0
	cmp	x5, x4
	b.lo	4b
	b	_start

/* Fill 64 rows at x1 with w3; x1 advances past them. */
band:
	movz	x2, #0x1, lsl #16
	movk	x2, #0x6800		/* 1440 * 64 pixels */
5:	str	w3, [x1], #4
	subs	x2, x2, #1
	b.ne	5b
	dsb	sy
	ret

	.text
	.globl _start
_start:
	msr	spsel, #1
	adr	x1, _stack_top
	mov	sp, x1
	adr	x1, vectors
	msr	vbar_el1, x1
	isb
	/* zero .bss */
	adr	x1, _bss_start
	adr	x2, _bss_end
1:	cmp	x1, x2
	b.hs	2f
	str	xzr, [x1], #8
	b	1b
2:	mov	x0, x19
	bl	shim_main
3:	wfe
	b	3b

/* PSCI CPU_ON target. x0 = context id = cpu index (1..7). */
	.globl secondary_entry
secondary_entry:
	msr	daifset, #0xf
	msr	spsel, #1
	adr	x1, _sec_stacks
	add	x1, x1, x0, lsl #12
	add	x1, x1, #0x1000
	mov	sp, x1
	adr	x1, vectors
	msr	vbar_el1, x1
	isb
	bl	secondary_main
4:	wfe
	b	4b

/*
 * Exception vectors. Each slot stashes x29/x30, loads its slot number
 * and jumps to the common path, which saves x0-x28 and calls
 * exc_handler(slot, regs). IRQs return; everything else ends in the
 * on-screen panic.
 */
.macro vent slot
	.balign	0x80
	stp	x29, x30, [sp, #-16]!
	mov	x29, #\slot
	b	exc_common
.endm

	.balign	0x800
	.globl vectors
vectors:
	vent 0
	vent 1
	vent 2
	vent 3
	vent 4
	vent 5
	vent 6
	vent 7
	vent 8
	vent 9
	vent 10
	vent 11
	vent 12
	vent 13
	vent 14
	vent 15

exc_common:
	sub	sp, sp, #(30 * 8)
	stp	x0, x1, [sp, #0]
	stp	x2, x3, [sp, #16]
	stp	x4, x5, [sp, #32]
	stp	x6, x7, [sp, #48]
	stp	x8, x9, [sp, #64]
	stp	x10, x11, [sp, #80]
	stp	x12, x13, [sp, #96]
	stp	x14, x15, [sp, #112]
	stp	x16, x17, [sp, #128]
	stp	x18, x19, [sp, #144]
	stp	x20, x21, [sp, #160]
	stp	x22, x23, [sp, #176]
	stp	x24, x25, [sp, #192]
	stp	x26, x27, [sp, #208]
	str	x28, [sp, #224]
	mov	x0, x29
	mov	x1, sp
	bl	exc_handler
	ldp	x0, x1, [sp, #0]
	ldp	x2, x3, [sp, #16]
	ldp	x4, x5, [sp, #32]
	ldp	x6, x7, [sp, #48]
	ldp	x8, x9, [sp, #64]
	ldp	x10, x11, [sp, #80]
	ldp	x12, x13, [sp, #96]
	ldp	x14, x15, [sp, #112]
	ldp	x16, x17, [sp, #128]
	ldp	x18, x19, [sp, #144]
	ldp	x20, x21, [sp, #160]
	ldp	x22, x23, [sp, #176]
	ldp	x24, x25, [sp, #192]
	ldp	x26, x27, [sp, #208]
	ldr	x28, [sp, #224]
	add	sp, sp, #(30 * 8)
	ldp	x29, x30, [sp], #16
	eret

/* Last initialized word of the image (.data is placed last): see entry0. */
	.data
	.balign	4
far_probe:
	.word	0x5a5aa5a5
