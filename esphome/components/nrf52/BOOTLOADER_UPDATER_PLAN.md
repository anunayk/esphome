# nRF52 (XIAO) no-SWD bootloader-updater — implementation plan

> Status: **design only, not implemented.** This document is for a future agent
> picking up the work. No code has been written for it yet. Read it together
> with the memory notes `nrf52-xiao-ble-mcuboot-layout`,
> `nrf52-usb-cdc-recovery-mbr-bug`, `nrf52-usb-cdc-recovery-vs-ble-ota`, and
> `xiao-brick-root-cause`.

## 1. Goal

Migrate a XIAO nRF52840 that is **already running fw1** (the single-slot
`mcuboot: usb_cdc_recovery` bootloader, `CONFIG_SINGLE_APPLICATION_SLOT=y`,
MCUboot at `0xF4000`) to **fw2** (the two-slot swap MCUboot that supports
`zephyr_mcumgr` BLE OTA) **over USB only — no SWD.**

This is the one missing no-SWD transition. Everything else already exists on
this branch:

| Transition | Path | Status |
|---|---|---|
| stock Adafruit BL → fw1 | `xiao_ble_mcuboot_updater_dfu.zip` via `adafruit-nrfutil dfu serial` | implemented |
| fw1 → new **app** | mcumgr serial recovery (single slot) | implemented |
| any → fw2 | SWD flat-flash `merged.hex` @ `0x0` | implemented (needs SWD) |
| **fw1 → fw2 (no SWD)** | **this document** | **missing** |

## 2. Why it is hard (the constraint that drives the whole design)

When fw1 was installed it **overwrote the stock Adafruit bootloader** at
`0xF4000` (the Adafruit BL self-update DFU slot was used — see
`xiao_ble_mcuboot_artifact.py.script`). So on a board running fw1:

```
0x0      factory nRF MBR (1 page)      ← reads UICR.NRFFW[0]=0xF4000, forwards there
0x1000   app slot (single, 0x78000)    ← the ONLY region fw1's USB can write
0x79000  (unused secondary region in pm_static, fw1 BL ignores it)
0xF1000  settings_storage
0xF4000  fw1 MCUboot (0x9800 budget)   ← single-slot, USB-CDC serial recovery
0xFD800+ Adafruit config / MBR-params / settings pages (kept free)
```

- The **only** USB interface left is fw1's MCUboot **serial recovery (mcumgr
  over USB-CDC)**, and it writes **only the application slot** at `0x1000`.
  It cannot write the bootloader region.
- BLE OTA needs a **two-slot swap MCUboot**, which is a *different bootloader at
  a different layout*. A two-slot BL does **not** fit the `0x9800` (38 KB)
  budget at `0xF4000` (even `BOOT_VALIDATE_SLOT0=y` overflows fw1 by design), so
  fw2's bootloader cannot live where fw1's does.

Therefore the only no-SWD lever is: **flash an "updater" application into fw1's
app slot via serial recovery, and have that application re-flash the device
into fw2 from inside.** That is the entire idea below.

## 3. Architecture overview

Three actors:

1. **fw1** — already on the device. Used once, as the loader, via its mcumgr
   serial recovery.
2. **The migrator app** — a small, self-contained Zephyr application, uploaded
   into fw1's single app slot. On boot it rewrites flash into the fw2 layout and
   resets. It embeds **only fw2's bootloader** (~48 KB), not the full app.
3. **fw2** — the two-slot swap MCUboot. After the migrator runs and resets, fw2
   comes up with an **empty primary slot** and drops into its own recovery
   (serial and/or BLE), where the real application is uploaded with the normal
   `zephyr_mcumgr` flow.

End-to-end flow:

```
fw1 (serial recovery)
   └─ upload migrator.img  ──► physical RESET ──►
migrator app (runs from 0x1000)
   ├─ erase regions fw2 expects (primary slot ⇒ empty ⇒ fw2 enters recovery)
   ├─ copy {flash routine + fw2-bootloader blob} into RAM
   ├─ [RAM] disable IRQs, feed/stop WDT, write fw2 bootloader to final addr, verify
   ├─ commit boot path (see §4 — UICR and/or page-0), verify SP in 0x2000xxxx
   └─ sys_reboot  ──►
fw2 (two-slot MCUboot), empty primary
   └─ upload real app via mcumgr (USB-CDC serial OR BLE) ──► swap ──► running fw2
```

Embedding **only the bootloader** (not bootloader+app) is deliberate:
- The blob is ~48 KB → fits comfortably in fw1's 480 KB app slot.
- The dangerous RAM-resident write covers only the small bootloader region, not
  a large app — smaller brick window.
- Reuses fw2's own DFU for the app; no second app payload to ship or verify.

## 4. The central design decision: fw2 boot path / layout

This is the crux and **must be settled on real hardware**, because it decides
how dangerous the migration is. Two options:

### Option A — Plain `0x0` layout (matches the known-good SWD image)

fw2 = the existing plain two-slot image: **MCUboot vector table at `0x0`**,
slot0 `0xC000–0x85000`, secondary `0x85000–0xFE000`, settings `0xFE000`. This is
exactly what the SWD `merged.hex` flashes and is already validated cold-bootable
(SP `0x20004080`).

- Migrator must **erase page 0 (the factory MBR) and write MCUboot at `0x0`**,
  and clear `UICR.NRFFW` so no stale MBR-forward points at the now-erased
  `0xF4000`.
- **Brick window:** from erasing page 0 until MCUboot@`0x0` is fully written and
  valid, the device has no boot path. Power loss here = SWD-only recovery.
- Pro: target is byte-for-byte the already-trusted SWD image; no new layout to
  design/validate. Con: rewrites the page you boot from — the scariest step.

### Option B — Keep the factory MBR, retarget `UICR.NRFFW[0]` (safer, more work)

Leave the factory MBR at `0x0` untouched. Build fw2's bootloader **relocated**
to a new address (like fw1's MCUboot was relocated to `0xF4000`), place it +
its slots in a fresh full-flash layout, then **`ERASEUICR` and rewrite
`NRFFW[0]` to the new bootloader address as the very last step.**

- **Boot path is intact throughout:** until the final UICR commit, the MBR still
  forwards to fw1's MCUboot@`0xF4000` (don't erase it until the end), so a
  failure before the commit leaves fw1 bootable. The commit itself (UICR write)
  is a single small operation.
- Pro: dramatically smaller brick window; old bootloader is a live fallback
  until the last instant. Con: requires designing **and validating a brand-new
  relocated two-slot pm_static** that diverges from the SWD image; the full
  flash is still rewritten (just with the boot path preserved until commit).

**Recommendation:** prototype with **Option A** (reuses the trusted image, least
new surface) while the SWD rig is attached as a safety net; if field use without
SWD is the real goal, invest in **Option B** for the smaller brick window. The
plan below is written so the migrator's flash-write core is the same either way;
only the target addresses and the "commit boot path" step differ.

## 5. The migrator application

A standalone, fixed Zephyr app (the C/asm does **not** change per user config —
only the embedded fw2-bootloader blob does). Properties:

- **Headered for fw1's single-slot MCUboot.** fw1 is
  `CONFIG_BOOT_SIGNATURE_TYPE_NONE` + `CONFIG_BOOT_VALIDATE_SLOT0=n` (hash header
  only). The migrator just needs a valid MCUboot image header sized to the slot;
  sign with `imgtool` type `none` (reuse fw1's `root-ec-p256.pem` plumbing only
  if a key is required by the build, as the `.conf` notes).
- **Self-relocating writes via `__ramfunc`.** The migrator runs from `0x1000`,
  which in the Option A target lies *inside* the new bootloader region
  (`0x0–0xC000`). The erase/program loop **and** the bytes it programs must live
  in RAM during the critical phase. nRF52840 has 256 KB RAM at `0x20000000`;
  copying a ~48 KB blob + a tiny NVMC routine is fine.
- **Embedded payload.** The fw2-bootloader blob (final-address bytes, gap-filled
  `0xFF`) is linked in as a `const` array placed so it can be `memcpy`'d to RAM
  before the self-erase. Generated at ESPHome build time (see §7).

### Migrator boot sequence

```
1. Bring-up: minimal clocks, NO watchdog start (see §6.3). Optional brief delay
   / LED blink so a human can see it started.
2. Erase the regions fw2 expects to find empty:
     - fw2 primary slot  → empty  (so fw2 boots into recovery, not a garbage app)
     - fw2 secondary slot, scratch, settings → erased
   These are large but NOT brick-risk (boot path still intact).
3. Verify embedded blob hash (sanity) BEFORE touching the boot path.
4. memcpy(flash routine, fw2-bootloader blob) → RAM.
5. Jump to the RAM routine:
     a. __disable_irq()
     b. WDT handling per §6.3
     c. Option A: erase 0x0..0xC000 (MBR + new BL region); program fw2 BL from RAM
        Option B: program fw2 BL at its relocated address (MBR/0x0 untouched)
     d. verify: first word (SP) of the new BL is in 0x2000xxxx; second word
        (reset vector) is sane
     e. commit boot path:
        Option A: clear UICR.NRFFW (ERASEUICR + rewrite the UICR words the build
                  still needs — NFC-pins-as-gpio, REGOUT0 if used)
        Option B: ERASEUICR + write NRFFW[0]=<new BL addr>, NRFFW[1]=<params>,
                  + restore the same NFC/REGOUT0 words
     f. NVIC_SystemReset()
6. (unreachable) — device resets into fw2.
```

## 6. Sharp edges (each has bricked or can brick this board)

### 6.1 UICR is all-or-nothing
`NRFFW[0..1]` cannot be changed from one set value to another without an
`ERASEUICR`, which wipes **all** of UICR. The plain build relies on UICR for
`nfct-pins-as-gpios` (newer NCS sets this via the `&uicr` overlay) and possibly
`REGOUT0`. The migrator **must re-write every UICR word fw2 needs** after the
erase, not just `NRFFW`. Derive the exact target UICR contents from a real
post-SWD device (see §8) — do not guess. The existing artifact script already
encodes `UICR_EXPECTED = {0x10001014: 0xF4000, 0x10001018: 0xFE000}` for fw1;
fw2's expected UICR is different and must be captured.

### 6.2 Page-0 / MBR (Option A only)
Erasing page 0 removes the factory MBR. This is the irreversible-without-SWD
step. Keep the RAM routine minimal, IRQs off, and verify the new vector table
before reset. If Option B is chosen, page 0 is never touched.

### 6.3 Watchdog
The nRF52 `WDT`, once started, **cannot be stopped** until reset. The nrf52
component forces `WATCHDOG=y` / `WDT_DISABLE_AT_BOOT=n` for the *app* build, but
the migrator controls its own prj.conf — it should **not start the WDT at all**,
or if Zephyr starts one, the RAM routine must feed it (hard, since IRQs are off
and the routine must stay short). Simplest: build the migrator with the WDT
disabled.

### 6.4 Serial-recovery reset quirk
Per `nrf52-usb-cdc-recovery-mbr-bug`: an SMP serial *reset* does **not** actually
reboot fw1's MCUboot — a **physical RESET** is required after uploading the
migrator. The host flow (and any docs/UX) must tell the user to press reset.

### 6.5 fw1 single-slot upload semantics
fw1 has no test/confirm/swap: "mark for test" returns `ENOTSUP`, reset replies
error-typed with `rc=EOK`. `ota.py` already tolerates this (commit `a5959b6b5`),
so uploading the migrator over serial reuses that path unchanged. Verify the
migrator `.img` size ≤ the single app slot (`0x78000`).

### 6.6 Flash budget for the embedded blob
fw2-bootloader blob (~48 KB) + migrator code/RAM tables must fit fw1's app slot
(480 KB) — fine. But the blob must also fit in RAM alongside the routine during
the copy (256 KB RAM) — also fine. Add an explicit build-time assertion.

## 7. ESPHome integration points (concrete)

Mirror the existing `usb_cdc_recovery` artifact machinery:

- **`esphome/components/nrf52/__init__.py`**
  - `get_download_types()` (around line 522): add a new entry, e.g.
    `"MCUboot two-slot migrator (no SWD)"` pointing at a generated
    `zephyr/xiao_ble_mcuboot_migrator.img`, gated on the file existing — same
    pattern as the `MCUBOOT_UPDATER_DFU_PATH` block at line 551.
  - When building the **two-slot** fw2 (the `else` branch at line 422, NOT
    `usb_cdc_recovery`), register a new post-build script via `add_extra_script`,
    analogous to `xiao_ble_mcuboot_artifact.py.script`.
  - Note the existing `_validate_usb_cdc_recovery_ota` guard
    (line 205) is unrelated and stays; the migrator is built for the two-slot
    config, which is allowed to have a `zephyr_mcumgr` OTA.

- **New `esphome/components/nrf52/xiao_ble_mcuboot_migrator.py.script`**
  (post-build, PlatformIO `AddPostAction`): reuse the Intel-HEX reader and
  `_bootloader_region_to_bin`-style flattening from
  `xiao_ble_mcuboot_artifact.py.script` to extract **fw2's bootloader bytes**
  from `merged.hex`/`zephyr.hex`, patch them into the prebuilt migrator ELF/bin
  as the embedded blob, then `imgtool`-header the result for fw1's single slot →
  emit `xiao_ble_mcuboot_migrator.img`.

- **New prebuilt migrator sources** under
  `esphome/components/nrf52/migrator/` (a tiny standalone Zephyr app + linker
  bits for the `__ramfunc` routine and the blob placeholder section). Kept fixed;
  only the blob is patched per build.

- **Host-side flow** — likely a small helper (or documented manual steps): upload
  `migrator.img` to fw1 over USB-CDC (reusing `ota.py`'s serial path) → prompt
  physical reset → wait for fw2 recovery to enumerate → upload the real app
  (`app_update.bin`) via the normal `zephyr_mcumgr` USB-CDC or BLE flow. Reuses
  the single-slot tolerance already in `ota.py`.

## 8. Verification & testing

CI cannot exercise the hardware path, so split it:

- **Unit-testable (CI):** the blob-extraction + `.img` packaging script. Follow
  the style of any tests for `xiao_ble_mcuboot_artifact.py.script` — feed a
  synthetic `merged.hex`, assert the extracted bootloader bytes, header, and size
  bound. Add a config-compile test that the migrator app builds.
- **Hardware bring-up (SWD rig attached as the safety net, `10.0.10.72`):**
  1. Flash fw1 via SWD; confirm fw1 serial recovery.
  2. Upload `migrator.img`; physical reset; confirm migrator runs (LED/RTT).
  3. After migration, **dump full flash + UICR over SWD and diff against the
     known-good post-SWD fw2 image** — this is the acceptance test. The
     `arm-none-eabi-objcopy` + word-0 SP check from
     `nrf52-xiao-ble-mcuboot-layout` is the cold-bootability gate.
  4. Confirm fw2 enters recovery, app uploads, BLE OTA then works.
  - Capture the exact fw2 UICR contents here and bake them into the migrator
    (see §6.1).
- Keep the SWD rig connected through all bring-up; it is the recovery net for the
  inevitable mid-write bricks.

## 9. Risks / honest assessment

- **Brick risk is real and intrinsic** to Option A's page-0 rewrite; Option B
  shrinks but does not eliminate it. This feature trades "needs SWD once" for
  "small chance of needing SWD to un-brick." Only worth it if no-SWD field
  migration is a hard requirement.
- Significant new surface: a standalone Zephyr app, a `__ramfunc` flash routine,
  UICR reconstruction, a build-time blob-injection script, and a multi-step host
  flow — each independently testable, but the integration only proves out on
  hardware.
- If the goal is simply "get this board onto BLE OTA," the **one-time SWD
  flat-flash of `merged.hex` is far cheaper and safer** and is already supported.
  This updater only earns its keep for fleets with no SWD access.

## 10. Suggested implementation order (for the future agent)

1. On a real board, SWD-flash fw2; **capture the reference flash image + UICR**.
   Decide Option A vs B from what you observe (does anything still read UICR at
   `0x0`? is there an MBR in the fw2 image or is `0x0` MCUboot directly?).
2. Write the standalone migrator app (no WDT, RTT/LED heartbeat), with a stubbed
   blob, and a `__ramfunc` erase/program/verify/reset routine. Prove it on
   hardware writing a *harmless* region first.
3. Add the build-time blob-injection + `.img` packaging script; unit-test it.
4. Wire `get_download_types()` + post-build script into `nrf52/__init__.py`.
5. Add the host upload flow (migrator → reset prompt → fw2 recovery → app).
6. Full hardware acceptance: flash-diff vs the reference; confirm BLE OTA.
7. Document in `XIAO-GUIDE.md`; add a memory note recording the chosen option and
   the captured fw2 UICR values.
```
