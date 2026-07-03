/*
 * Option B fw1 -> fw2 migrator: flash layout (single source of truth).
 *
 * This header is the canonical description of the Option B relocated two-slot
 * fw2 layout. It is consumed by:
 *   - the standalone migrator app (src/migrate.c) at compile time, and
 *   - the ESPHome build-time packaging script
 *     (xiao_ble_mcuboot_migrator.py.script), which parses the MIG_* defines
 *     below so the two never drift (see test_nrf52_migrator.py).
 *
 * Read together with BOOTLOADER_UPDATER_PLAN.md (section 4 "Option B") and the
 * memory notes nrf52-xiao-ble-mcuboot-layout / xiao-brick-root-cause.
 *
 * ┌──────────────────────────────────────────────────────────────────────────┐
 * │ Option B fw2 layout (factory nRF MBR kept at 0x0, MCUboot RELOCATED)       │
 * ├───────────┬───────────┬──────────────────────────────────────────────────┤
 * │ start     │ size      │ region                                            │
 * ├───────────┼───────────┼──────────────────────────────────────────────────┤
 * │ 0x000000  │ 0x001000  │ factory nRF MBR  (NEVER erased by the migrator)   │
 * │ 0x001000  │ 0x00C000  │ fw2 MCUboot  (relocated; NRFFW[0] -> 0x1000)      │
 * │ 0x00D000  │ 0x073000  │ mcuboot_primary   (slot 0, 460 KiB)              │
 * │ 0x080000  │ 0x073000  │ mcuboot_secondary (slot 1, equal to primary)     │
 * │ 0x0F3000  │ 0x00A800  │ settings_storage  (reclaims old fw1 BL region)    │
 * │ 0x0FD800  │ 0x002800  │ reserved: Adafruit cfg / MBR-params / settings    │
 * └───────────┴───────────┴──────────────────────────────────────────────────┘
 *
 * Why this differs from the plain (SWD) fw2 image: the plain image places
 * MCUboot's vector table at 0x0, which requires erasing page 0 (the factory
 * MBR) -- Option A's irreversible step. Option B keeps the MBR at 0x0 and only
 * retargets UICR.NRFFW[0] as the final commit, so fw1's MCUboot at 0xF4000
 * stays a live fallback (via the untouched MBR) until the very last UICR write.
 *
 * HARDWARE NOTE: these addresses + UICR targets are hardware-validated on a
 * XIAO nRF52840 (2026-06-19). The fw2 build's generated partitions.yml matches
 * every region here exactly; the relocated MCUboot vector links at 0x1000; and
 * the full end-state (factory MBR at 0x0 + NRFFW[0]=0x1000 + NRFFW[1]=0xFE000 +
 * NFCPINS=0xFFFFFFFE) was reproduced over SWD and boots the app -- proving the
 * MBR forwards to the relocated MCUboot, which boots the signed primary image.
 * Still pending: running the on-device migrator app itself (needs the prebuilt
 * base; see BOOTLOADER_UPDATER_PLAN.md section 8 / migrator/README.md).
 */

#ifndef ESPHOME_NRF52_MIGRATOR_LAYOUT_H
#define ESPHOME_NRF52_MIGRATOR_LAYOUT_H

/* nRF52840 flash + RAM geometry */
#define MIG_FLASH_PAGE_SIZE 0x1000u
#define MIG_FLASH_END 0x00100000u
#define MIG_RAM_START 0x20000000u
#define MIG_RAM_END 0x20040000u /* 256 KiB */

/* Factory MBR -- the migrator MUST NOT touch this page. */
#define MIG_MBR_START 0x00000000u
#define MIG_MBR_END 0x00001000u

/* fw2 MCUboot, relocated. NRFFW[0] is retargeted here as the final commit. */
#define MIG_FW2_BOOTLOADER_ADDR 0x00001000u
#define MIG_FW2_BOOTLOADER_SIZE 0x0000C000u /* 48 KiB */

/* fw2 slots + settings (the regions the migrator erases so fw2 boots empty). */
#define MIG_FW2_PRIMARY_ADDR 0x0000D000u
#define MIG_FW2_PRIMARY_SIZE 0x00073000u
#define MIG_FW2_SECONDARY_ADDR 0x00080000u
#define MIG_FW2_SECONDARY_SIZE 0x00073000u
#define MIG_FW2_SETTINGS_ADDR 0x000F3000u
#define MIG_FW2_SETTINGS_SIZE 0x0000A800u

/*
 * Inclusive erase window for the RAM routine. This must NOT reach fw1's MCUboot
 * at 0xF4000: that bootloader (plus the untouched factory MBR @0x0 and
 * UICR.NRFFW[0]=0xF4000) is the migrator's only fallback. If the migrator erased
 * it before committing fw2, any failure in the program/verify steps below would
 * be a hard brick instead of a reset back into fw1's serial recovery.
 *
 * So the window covers exactly the relocated fw2 bootloader + both fw2 slots and
 * stops at the start of settings_storage (0xF3000) -- i.e. just below fw1's BL.
 * The factory MBR (below MIG_ERASE_START) and everything at/after MIG_ERASE_END
 * (settings_storage, which overlaps fw1's BL region) are left intact. fw2's app
 * reclaims/erases settings_storage at runtime after the migration commits, when
 * fw1's bootloader is no longer needed and fw2's own MCUboot is in control.
 */
#define MIG_ERASE_START MIG_FW2_BOOTLOADER_ADDR /* 0x001000 */
#define MIG_ERASE_END (MIG_FW2_SECONDARY_ADDR + MIG_FW2_SECONDARY_SIZE) /* 0x0F3000 (stops below fw1 BL @0xF4000) */

/*
 * Target UICR contents written after ERASEUICR (the all-or-nothing commit, see
 * BOOTLOADER_UPDATER_PLAN.md section 6.1). NRFFW[0] forwards the MBR to the
 * relocated MCUboot; NRFFW[1] is the MBR params page (kept in the reserved top
 * region). NFCT-pins-as-GPIO and REGOUT0 must be re-applied here too because
 * ERASEUICR wipes them -- the migrator restores them from these defines.
 */
#define MIG_UICR_NRFFW0_ADDR 0x10001014u
#define MIG_UICR_NRFFW1_ADDR 0x10001018u
#define MIG_UICR_NRFFW0_VALUE MIG_FW2_BOOTLOADER_ADDR /* 0x001000 */
#define MIG_UICR_NRFFW1_VALUE 0x000FE000u

/*
 * REGOUT0 (GPIO/VDD regulator output). The XIAO nRF52840 powers the SoC in
 * high-voltage mode (VDDH from USB), where the internal regulator defaults to
 * 1.8V. At 1.8V this board does not run -- it never enumerates USB and the CPU
 * does not execute the app (hardware-verified: a wiped REGOUT0 leaves the board
 * dead until 3.0V is restored over SWD). ERASEUICR clears REGOUT0, so the
 * migrator MUST rewrite it here; fw2 (whose MCUboot runs first, before any app
 * board-init that might set it) would otherwise boot at 1.8V and hang. 3.0V is
 * the Adafruit XIAO default: REGOUT0.VOUT field = 4 -> low three bits 0b100.
 */
#define MIG_UICR_REGOUT0_ADDR 0x10001304u
#define MIG_UICR_REGOUT0_VALUE 0xFFFFFFFCu /* VOUT=4 (3.0V); all other bits left erased */

/*
 * Embedded fw2-bootloader blob. The placeholder array in src/blob.c reserves
 * MIG_BLOB_CAPACITY bytes (>= MIG_FW2_BOOTLOADER_SIZE); the build-time injector
 * locates it by MIG_BLOB_MAGIC and overwrites the header + data in place.
 */
#define MIG_BLOB_MAGIC "ESPHOMENRF52BLOB" /* exactly 16 bytes, located verbatim in the .bin */
#define MIG_BLOB_MAGIC_LEN 16u
#define MIG_BLOB_CAPACITY MIG_FW2_BOOTLOADER_SIZE /* 0xC000 */

#endif /* ESPHOME_NRF52_MIGRATOR_LAYOUT_H */
