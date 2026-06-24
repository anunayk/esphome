/*
 * Option B critical section: program the relocated fw2 MCUboot and retarget
 * UICR.NRFFW[0], all from RAM with IRQs disabled.
 *
 * This routine erases and reprograms the very flash region the migrator was
 * loaded into (fw1's app slot starts at 0x1000, which is also the fw2
 * bootloader address), so it -- and the blob it programs -- must already live
 * in RAM before the first erase. main() copies the blob to RAM and marks this
 * code __ramfunc so Zephyr relocates it at boot.
 *
 * Ordering is chosen so fw1 stays bootable until the last possible instant:
 *   1. erase fw2 regions (BL + both slots + settings); MBR @0x0 and fw1 BL
 *      @0xF4000 are NOT in the window, so UICR.NRFFW[0] still forwards to fw1.
 *   2. program the relocated fw2 MCUboot; verify its vector table.
 *   3. on a bad vector table, just reset -- fw1 (via the untouched MBR/UICR)
 *      still comes up in serial recovery. No commit happened.
 *   4. only after the vector table checks out: ERASEUICR, then rewrite
 *      NRFFW[0]/NRFFW[1] + NFC-pins-as-GPIO + REGOUT0. This single commit flips
 *      the boot path from fw1 to fw2.
 *   5. reset into fw2 (empty primary slot -> recovery).
 *
 * See BOOTLOADER_UPDATER_PLAN.md sections 4-6 and migrator_layout.h.
 */

#include "migrator.h"

#include <nrf.h>
/*
 * Pulls in Zephyr's real __ramfunc (toolchain/gcc.h): when
 * CONFIG_ARCH_HAS_RAMFUNC_SUPPORT=y it places functions in the .ramfunc section
 * that the startup copies into RAM. Without this include, __ramfunc was
 * undefined here and the local empty fallback below left the entire critical
 * section in flash -- so the first erase wiped the page it was executing from
 * and the migration faulted partway (slot erased, UICR never committed).
 */
#include <zephyr/kernel.h>

/* NFCPINS.PROTECT cleared -> P0.09/P0.10 usable as GPIO (matches the fw2 app's
 * nfct-pins-as-gpios). After ERASEUICR the field reads 0xFFFFFFFF (NFC mode),
 * so the migrator must re-apply this. REGOUT0 (3.0V) is rewritten in the same
 * commit -- see MIG_UICR_REGOUT0_* in migrator_layout.h: the board will not run
 * fw2 at the erased 1.8V default, so it cannot be left to the fw2 app. */
#define MIG_UICR_NFCPINS_GPIO 0xFFFFFFFEu

/*
 * The critical section MUST run from RAM (it erases its own flash). Never define
 * __ramfunc to nothing as a fallback: a no-op silently produces flash-resident
 * critical code that self-erases mid-run and bricks the migration. If the arch
 * cannot relocate to RAM, fail the build loudly instead.
 */
#if !defined(CONFIG_ARCH_HAS_RAMFUNC_SUPPORT)
#error "migrator requires CONFIG_ARCH_HAS_RAMFUNC_SUPPORT: the critical erase/program section must execute from RAM"
#endif

static __ramfunc void mig_nvmc_wait(void) {
  while (NRF_NVMC->READY == NVMC_READY_READY_Busy) {
  }
}

static __ramfunc void mig_nvmc_config(uint32_t mode) {
  NRF_NVMC->CONFIG = mode;
  __DSB();
  mig_nvmc_wait();
}

static __ramfunc void mig_erase_range(uint32_t start, uint32_t end) {
  mig_nvmc_config(NVMC_CONFIG_WEN_Een);
  for (uint32_t page = start; page < end; page += MIG_FLASH_PAGE_SIZE) {
    NRF_NVMC->ERASEPAGE = page;
    __DSB();
    mig_nvmc_wait();
  }
  mig_nvmc_config(NVMC_CONFIG_WEN_Ren);
}

static __ramfunc void mig_program(uint32_t addr, const uint8_t *data, uint32_t len) {
  volatile uint32_t *dst = (volatile uint32_t *) addr;
  mig_nvmc_config(NVMC_CONFIG_WEN_Wen);
  for (uint32_t offset = 0; offset < len; offset += 4) {
    uint32_t word = 0xFFFFFFFFu;
    /* Little-endian assembly that tolerates a non-word-multiple tail. */
    uint8_t *wb = (uint8_t *) &word;
    for (uint32_t b = 0; b < 4 && (offset + b) < len; b++) {
      wb[b] = data[offset + b];
    }
    *dst = word;
    __DSB();
    mig_nvmc_wait();
    dst++;
  }
  mig_nvmc_config(NVMC_CONFIG_WEN_Ren);
}

/*
 * Reset from RAM. NVIC_SystemReset() is reached through a flash veneer
 * (____NVIC_SystemReset_veneer: `ldr pc,[pc]`) that lives in the region this
 * routine just erased -- jumping there faults. Inline the AIRCR write in a
 * __ramfunc so the reset never touches flash.
 */
static __ramfunc void mig_reset(void) {
  __DSB();
  SCB->AIRCR =
      (uint32_t) ((0x5FAUL << SCB_AIRCR_VECTKEY_Pos) | (SCB->AIRCR & SCB_AIRCR_PRIGROUP_Msk) | SCB_AIRCR_SYSRESETREQ_Msk);
  __DSB();
  for (;;) {
  }
}

static __ramfunc void mig_uicr_write(uint32_t addr, uint32_t value) {
  mig_nvmc_config(NVMC_CONFIG_WEN_Wen);
  *(volatile uint32_t *) addr = value;
  __DSB();
  mig_nvmc_wait();
  mig_nvmc_config(NVMC_CONFIG_WEN_Ren);
}

static __ramfunc void mig_uicr_erase(void) {
  mig_nvmc_config(NVMC_CONFIG_WEN_Een);
  NRF_NVMC->ERASEUICR = NVMC_ERASEUICR_ERASEUICR_Erase;
  __DSB();
  mig_nvmc_wait();
  mig_nvmc_config(NVMC_CONFIG_WEN_Ren);
}

/* A valid Cortex-M vector table starts with the initial stack pointer (must
 * point into RAM) followed by the reset vector (a thumb address in flash). */
static __ramfunc int mig_vector_table_ok(uint32_t addr) {
  const uint32_t *vt = (const uint32_t *) addr;
  uint32_t sp = vt[0];
  uint32_t reset = vt[1];
  if (sp < MIG_RAM_START || sp > MIG_RAM_END) {
    return 0;
  }
  if ((reset & 1u) == 0u) { /* reset vector must be a thumb (odd) address */
    return 0;
  }
  if (reset < MIG_ERASE_START || reset >= MIG_FLASH_END) {
    return 0;
  }
  return 1;
}

void __ramfunc mig_run_from_ram(const struct mig_blob *blob) {
  __disable_irq();

  /* Disable the ARM MPU and the NVMC instruction cache before touching flash.
   * Programming nRF flash is a CPU store straight to the flash address; if the
   * MPU's code-region attributes forbid that store in our context it faults, and
   * because we run from RAM with our own vector table about to be erased, that
   * fault escalates to an unrecoverable lockup. Disabling the MPU here keeps the
   * direct flash stores legal; clearing ICACHE keeps post-write readbacks
   * (mig_vector_table_ok) coherent with what we just programmed. Both are safe:
   * IRQs are off and we execute entirely from RAM. */
  MPU->CTRL = 0u;
  __DSB();
  __ISB();
  NRF_NVMC->ICACHECNF = 0u;
  __DSB();

  /* 1. Erase the relocated fw2 bootloader + both fw2 slots. The window stops
   *    below fw1's MCUboot @0xF4000 (see MIG_ERASE_END), so until the UICR
   *    commit below fw1 stays the bootable fallback via the untouched MBR. */
  mig_erase_range(MIG_ERASE_START, MIG_ERASE_END);

  /* 2. Program the relocated fw2 MCUboot from the RAM-resident blob. */
  mig_program(blob->target_addr, blob->data, blob->blob_len);

  /* 3. Pre-commit safety gate: if the new bootloader's vector table is bad,
   *    do NOT touch UICR -- reset back into the still-bootable fw1. */
  if (!mig_vector_table_ok(blob->target_addr)) {
    mig_reset(); /* fw1 (untouched MBR/UICR/BL) still comes up in serial recovery */
  }

  /* 4. Commit the boot path: ERASEUICR wipes all of UICR, so every word fw2
   *    needs must be rewritten here (NRFFW[0/1] + NFC-pins-as-GPIO + REGOUT0).
   *    REGOUT0 is critical: without it the board boots fw2 at 1.8V and hangs. */
  mig_uicr_erase();
  mig_uicr_write(MIG_UICR_NRFFW0_ADDR, MIG_UICR_NRFFW0_VALUE);
  mig_uicr_write(MIG_UICR_NRFFW1_ADDR, MIG_UICR_NRFFW1_VALUE);
  mig_uicr_write((uint32_t) &NRF_UICR->NFCPINS, MIG_UICR_NFCPINS_GPIO);
  mig_uicr_write(MIG_UICR_REGOUT0_ADDR, MIG_UICR_REGOUT0_VALUE);

  /* 5. Reset into fw2. */
  mig_reset();
}
