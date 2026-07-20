/*
 * Placeholder for the embedded fw2-bootloader blob.
 *
 * This object reserves space and a locatable magic marker; it carries no real
 * bootloader bytes. The ESPHome build-time injector
 * (xiao_ble_mcuboot_migrator.py.script) finds MIG_BLOB_MAGIC in the compiled
 * .bin and overwrites blob_len / blob_crc32 / target_addr / data[] with the
 * actual relocated fw2 MCUboot extracted from that build's merged.hex.
 *
 * It lives in .rodata (a non-zero initializer keeps it in the loadable image
 * rather than .bss) and is referenced by main(), so it is emitted verbatim
 * into the .bin and the injector can find it by scanning for MIG_BLOB_MAGIC.
 */

#include "migrator.h"

#ifdef __ZEPHYR__
#include <zephyr/toolchain.h>
#include <zephyr/sys/util.h>
#endif

#include <stddef.h>

/* The header must stay a fixed 32 bytes so the injector can patch by offset. */
#ifdef BUILD_ASSERT
BUILD_ASSERT(offsetof(struct mig_blob, data) == 32,
             "mig_blob header must be exactly 32 bytes");
BUILD_ASSERT(MIG_BLOB_CAPACITY >= MIG_FW2_BOOTLOADER_SIZE,
             "blob capacity too small for the fw2 bootloader region");
#endif

/* data[] must be non-zero so the whole object stays in the loadable image; the
 * 0xFF fill also matches erased flash, which the injector pads short blobs with
 * (GCC range designator -- the migrator is GCC-only under Zephyr). */
const struct mig_blob mig_embedded_blob __attribute__((used)) = {
    .magic = MIG_BLOB_MAGIC,
    .blob_len = 0u,             /* patched by the injector */
    .blob_crc32 = 0u,           /* patched by the injector */
    .target_addr = 0xFFFFFFFFu, /* patched by the injector */
    .format_version = MIG_BLOB_FORMAT_VERSION,
    .data = {[0 ... MIG_BLOB_CAPACITY - 1] = 0xFF},
};
