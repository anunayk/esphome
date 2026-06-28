# XIAO nRF52840 fw1 → fw2 migrator (Option B)

A tiny standalone Zephyr app that migrates a XIAO nRF52840 already running
**fw1** (the single-slot `mcuboot: usb_cdc_recovery` bootloader at `0xF4000`) to
**fw2** (a relocated two-slot swap MCUboot that supports BLE OTA) **over USB
only — no SWD.**

This implements **Option B** of `../BOOTLOADER_UPDATER_PLAN.md`: the factory nRF
MBR at `0x0` is kept, fw2's MCUboot is relocated to `0x1000`, and the boot path
is committed by retargeting `UICR.NRFFW[0]` as the very last step — so fw1 stays
a live fallback until that single commit.

> **Hardware-validated on a XIAO nRF52840.** The flash addresses and target
> UICR values in `migrator_layout.h` match the fw2 build's `partitions.yml`
> exactly, and the full Option B end-state (factory MBR at `0x0` +
> `NRFFW[0]=0x1000` + `NRFFW[1]=0xFE000` + `NFCPINS=0xFFFFFFFE`) was reproduced
> over SWD and **boots the app**; the `src/migrate.c` routine has been run
> end-to-end on-device. That routine is intrinsically brick-capable, so keep an
> SWD rig attached as a recovery net for its first run on any new board.

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

## Building the base (automatic, from source)

The ESPHome build compiles this app **from source** during a firmware compile —
no binary is committed in-tree. Building the installer is opt-in and separate
from building the app in the fw2 two-slot layout: `nrf52: mcuboot: two_slot:
true` selects the relocated fw2 partition layout for the running app, while
`nrf52: mcuboot: migrator_image: true` (which requires `two_slot: true`)
additionally produces this one-time installer. Every normal build/flash/OTA of
the app needs only `two_slot: true`; the installer is needed once, to flash the
two-slot bootloader onto a board still running fw1. When `migrator_image: true`
is set, `to_code` copies this whole app tree into the build's project dir and
the post-build script (`../xiao_ble_mcuboot_migrator.py.script`) runs:

```sh
west build -b xiao_ble -d <build>/migrator_base \
  esphome/components/nrf52/migrator
```

against the `framework-zephyr` NCS workspace and the `gnuarmemb` toolchain the
nrf52 platform already provides (`ZEPHYR_BASE` / `GNUARMEMB_TOOLCHAIN_PATH` are
set from the resolved PlatformIO package dirs). The resulting **unsigned**
`build/zephyr/zephyr.bin` is the base: patching the blob changes the image
bytes, so signing happens *after* injection. The script then overwrites the
`mig_blob` placeholder with this build's relocated fw2 MCUboot and runs
`imgtool sign` (signature type *none*, fw1's single-slot size) to produce
`xiao_ble_mcuboot_migrator.img`. Because the base is rebuilt on every compile,
it always matches `src/` — there is nothing to regenerate by hand.

To build the base standalone (e.g. to inspect it), run the same `west build`
command above with `ZEPHYR_BASE` pointed at your NCS checkout.
