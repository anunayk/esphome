/*
 * Shared declarations for the fw1 -> fw2 (Option B) migrator app.
 *
 * The embedded fw2-bootloader blob is described by mig_blob, a fixed-layout
 * structure the build-time injector (xiao_ble_mcuboot_migrator.py.script)
 * patches in place: it finds the 16-byte magic, then overwrites blob_len,
 * blob_crc32, target_addr and the data[] payload. Keeping the layout fixed and
 * little-endian lets the injector work on the raw .bin without an ELF parser.
 */

#ifndef ESPHOME_NRF52_MIGRATOR_H
#define ESPHOME_NRF52_MIGRATOR_H

#include <stdint.h>
#include "../migrator_layout.h"

/* Bumped if the on-flash blob contract changes; the injector writes the same. */
#define MIG_BLOB_FORMAT_VERSION 1u

/*
 * Fixed 32-byte header followed by the payload. The injector and migrate.c must
 * agree byte-for-byte, so the layout is asserted at compile time in blob.c.
 */
struct mig_blob {
  uint8_t magic[MIG_BLOB_MAGIC_LEN]; /* MIG_BLOB_MAGIC, no trailing NUL */
  uint32_t blob_len;                 /* fw2 bootloader byte count (<= capacity) */
  uint32_t blob_crc32;               /* crc32_ieee over data[0 .. blob_len) */
  uint32_t target_addr;              /* program destination (== fw2 BL addr) */
  uint32_t format_version;           /* MIG_BLOB_FORMAT_VERSION */
  uint8_t data[MIG_BLOB_CAPACITY];   /* fw2 bootloader bytes, 0xFF-padded */
};

/* The single embedded blob instance (src/blob.c). */
extern const struct mig_blob mig_embedded_blob;

/*
 * The RAM-resident critical section. Runs with IRQs disabled from RAM because
 * it erases and reprograms the flash region the migrator itself was loaded
 * into. Never returns on success (it resets); returns only on a pre-commit
 * sanity failure, after which fw1 (via the untouched MBR) is still bootable.
 */
void mig_run_from_ram(const struct mig_blob *blob);

#endif /* ESPHOME_NRF52_MIGRATOR_H */
