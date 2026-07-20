/*
 * fw1 -> fw2 (Option B) migrator entry point.
 *
 * Runs once, from fw1's single application slot. It validates the embedded
 * fw2-bootloader blob entirely from flash (safe: nothing is erased yet), copies
 * it into RAM, then hands off to the RAM-resident critical section that
 * actually rewrites flash and retargets UICR. See migrate.c for the dangerous
 * part and BOOTLOADER_UPDATER_PLAN.md for the design.
 *
 * Built with the watchdog disabled (BOOTLOADER_UPDATER_PLAN.md section 6.3):
 * the nRF52 WDT cannot be stopped once started, and the critical section runs
 * for many seconds with IRQs off, so it must never be fed/expired.
 */

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/crc.h>

#include "migrator.h"

/* RAM-resident copy of the blob (.bss). The critical section erases the flash
 * the original lives in, so it can only read this RAM copy. */
static struct mig_blob ram_blob;

/* Validation failed before anything was written: nothing is erased, so fw1 is
 * untouched. Stay parked (re-upload a fixed migrator over fw1 serial recovery)
 * rather than risk a half-baked migration. */
static void mig_park(void) {
  while (1) {
    k_msleep(1000);
  }
}

static int mig_blob_valid(const struct mig_blob *blob) {
  if (memcmp(blob->magic, MIG_BLOB_MAGIC, MIG_BLOB_MAGIC_LEN) != 0) {
    return 0;
  }
  if (blob->format_version != MIG_BLOB_FORMAT_VERSION) {
    return 0;
  }
  if (blob->blob_len == 0u || blob->blob_len > MIG_BLOB_CAPACITY) {
    return 0;
  }
  if (blob->target_addr != MIG_FW2_BOOTLOADER_ADDR) {
    return 0;
  }
  return crc32_ieee(blob->data, blob->blob_len) == blob->blob_crc32;
}

int main(void) {
  /* Let power/USB settle and give a human a beat to spot that the migrator
   * booted before the long, headless critical section begins. */
  k_msleep(500);

  if (!mig_blob_valid(&mig_embedded_blob)) {
    mig_park();
  }

  memcpy(&ram_blob, &mig_embedded_blob, sizeof(ram_blob));

  /* Re-validate the RAM copy: a bad memcpy/RAM fault must not reach flash. */
  if (!mig_blob_valid(&ram_blob)) {
    mig_park();
  }

  /* Hands off to RAM. Returns only on a pre-commit failure (fw1 still boots). */
  mig_run_from_ram(&ram_blob);

  mig_park();
  return 0;
}
