# XIAO nRF52840 fw1 → fw2 migrator (Option B)

A tiny standalone Zephyr app that migrates a XIAO nRF52840 already running
**fw1** (the single-slot `mcuboot: usb_cdc_recovery` bootloader at `0xF4000`) to
**fw2** (a relocated two-slot swap MCUboot that supports BLE OTA) **over USB
only — no SWD.**

This implements **Option B** of `../BOOTLOADER_UPDATER_PLAN.md`: the factory nRF
MBR at `0x0` is kept, fw2's MCUboot is relocated to `0x1000`, and the boot path
is committed by retargeting `UICR.NRFFW[0]` as the very last step — so fw1 stays
a live fallback until that single commit.

> **Layout hardware-validated (2026-06-19); on-device migrator still pending.**
> The flash addresses and target UICR values in `migrator_layout.h` are
> hardware-validated on a XIAO nRF52840: the fw2 build's `partitions.yml`
> matches them exactly, and the full Option B end-state (factory MBR at `0x0` +
> `NRFFW[0]=0x1000` + `NRFFW[1]=0xFE000` + `NFCPINS=0xFFFFFFFE`) was reproduced
> over SWD and **boots the app**. What is *not* yet validated is running the
> `src/migrate.c` routine itself on-device (it needs the prebuilt base, below).
> That routine is intrinsically brick-capable, so keep the SWD rig attached for
> its first real run.

## How it works

1. fw1's USB-CDC serial recovery uploads `xiao_ble_mcuboot_migrator.img` into
   fw1's single app slot (`0x1000`). A **physical reset** boots it
   (`../BOOTLOADER_UPDATER_PLAN.md` §6.4 — an SMP serial reset does not reboot
   fw1).
2. `src/main.c` validates the embedded fw2-bootloader blob (magic + CRC) from
   flash, copies it into RAM, and calls the RAM-resident routine.
3. `src/migrate.c` (`__ramfunc`, IRQs off) erases the fw2 region, programs the
   relocated fw2 MCUboot, verifies its vector table, then `ERASEUICR`s and
   rewrites `NRFFW[0]/NRFFW[1]` + NFC-pins-as-GPIO, and resets.
4. fw2 comes up with an empty primary slot → its own recovery, where the real
   app is uploaded with the normal `zephyr_mcumgr` USB-CDC or BLE flow.

## The embedded blob contract

`src/blob.c` reserves a fixed-layout `struct mig_blob` (see `src/migrator.h`):
a 16-byte magic `ESPHOMENRF52BLOB`, a 32-byte header
(`blob_len`, `blob_crc32`, `target_addr`, `format_version`), then a
`MIG_BLOB_CAPACITY` (`0xC000`) `0xFF`-filled payload. The ESPHome build-time
injector (`../xiao_ble_mcuboot_migrator.py.script`) finds the magic in the
compiled `.bin` and overwrites the header + payload with the relocated fw2
MCUboot extracted from that build's `merged.hex`. The C app and the injector
share the constants in `migrator_layout.h`.

## Building the prebuilt base (offline, once)

The ESPHome build does **not** compile this app; it patches a committed prebuilt
base binary. The base must be the **unsigned** `zephyr.bin` — patching the blob
changes the image bytes, so signing has to happen *after* injection. Regenerate
the base with NCS (matching the framework version the nrf52 component pins) when
`src/` changes:

```sh
west build -b xiao_ble -d build esphome/components/nrf52/migrator \
  -- -DEXTRA_CONF_FILE=prj.conf

# UNSIGNED image (MCUboot header padded, not yet signed):
cp build/zephyr/zephyr.bin \
   esphome/components/nrf52/migrator/prebuilt/xiao_ble_mcuboot_migrator_base.bin
```

The injector (`../xiao_ble_mcuboot_migrator.py.script`) reads
`prebuilt/xiao_ble_mcuboot_migrator_base.bin`, overwrites the blob, then runs
`imgtool sign` (signature type *none*, fw1's single-slot size) to produce
`xiao_ble_mcuboot_migrator.img`. If the prebuilt is absent, the ESPHome
post-build step logs a warning and skips the migrator artifact (the rest of the
build is unaffected).
