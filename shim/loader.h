#pragma once
#include <stdint.h>

/* Boot the Y2SXNU64 payload found in the boot.img ramdisk; returns only on error. */
const char *loader_boot(uintptr_t dtb);
