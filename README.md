# ESPHome [![Discord Chat](https://img.shields.io/discord/429907082951524364.svg)](https://discord.gg/KhAMKrd) [![GitHub release](https://img.shields.io/github/release/esphome/esphome.svg)](https://GitHub.com/esphome/esphome/releases/) [![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://codspeed.io/esphome/esphome)

<a href="https://esphome.io/">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://media.esphome.io/logo/logo-text-on-dark.svg">
    <img src="https://media.esphome.io/logo/logo-text-on-light.svg" alt="ESPHome Logo">
  </picture>
</a>

---

[Documentation](https://esphome.io) -- [Issues](https://github.com/esphome/esphome/issues) -- [Feature requests](https://github.com/orgs/esphome/discussions)

---

[![ESPHome - A project from the Open Home Foundation](https://www.openhomefoundation.org/badges/esphome.png)](https://www.openhomefoundation.org/)

## Unreleased

This fork adds ISC-L1512-B support to ESPHome 2026.11.0b4.
It keeps upstream Nordic fixes and carries only the remaining nRF54L15 changes.
Existing nRF52840 configurations keep their bootloader layout, NVS settings, ADC scale, and UART defaults.
Variant pin validation now rejects the nonexistent nRF52840 pin P1.16.

### ISC-L1512-B configuration

The `nrf52:` key also selects the nRF54L15.
It requires NCS 3.4.1 or newer.
Clean the build directory before upgrading an existing NCS 2.9.2 configuration.
NCS reads the HFXO startup delay from devicetree to avoid the DRGN-25262 BLE assertion.
NCS 3.4.1 uses upstream's Zephyr SDK 1.0.1 with Picolibc and full floating-point formatting.
SHA-256 uses PSA Crypto on NCS 3.4.1 or newer.
Older SDKs keep Newlib and TinyCrypt.
SHA-256 declares its crypto SDK modules, including builds without Bluetooth.
Other components can request extra SDK modules with
`esphome.components.nrf52.framework.include_west_project()` from `to_code()`.
The native build merges external `ZEPHYR_EXTRA_MODULES` with library modules.
MCUboot declares its bootloader module and its nRF52840 serial-recovery CBOR module.
The module profile disables the DK LEDs, buttons, PWM, external flash, and UART20.
Bus components supply their own pin assignments.
LFRC uses the internal low-frequency oscillator in both the application and MCUboot.
The NFC GPIO property changes the nRF54L15 NFCT pad configuration without writing UICR.

The NCS 3.4.1 DK files confirm the module pin conflicts.
LED3 uses P1.14, button 0 uses P1.13, and button 2 uses P1.08.
UART20 uses P1.04/P1.05 for TX/RX and P1.06/P1.07 for RTS/CTS.

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/anunayk/esphome
      ref: nrf54l15-integration-2026.11.0b4
    components: [nrf52, zephyr, logger, adc, spi, i2c, ble_nus, time, sha256, zephyr_ble_server, zephyr_mcumgr]

nrf52:
  board: nrf54l15dk/nrf54l15/cpuapp
  toolchain: sdk-nrf
  bootloader: mcuboot
  lfclk_source: rc
  nfct_pins_as_gpios: true
  dk_peripherals: false
  framework:
    version: 3.4.1
    libc_nano: false
    advanced:
      enable_ota_rollback: false

logger:
  hardware_uart: UART30
  baud_rate: 115200

spi:
  - id: radio_spi
    interface: spi20
    clk_pin: P1.06
    mosi_pin: P1.05
    miso_pin: P1.04
  - id: rtd_spi
    interface: spi00
    clk_pin: P2.06
    mosi_pin: P2.08
    miso_pin: P2.09

i2c:
  sda: P1.09
  scl: P1.10
  frequency: 100kHz

sensor:
  - platform: adc
    id: battery_tap
    pin: P1.11
```

UART30 uses bench TX P0.03 and RX P0.04.
The logger UART must be explicit because the stock core keeps the old nRF52 USB default.
SPI20 uses P1 pins at up to 8 MHz.
SPI00 uses P2 pins at up to 32 MHz with extra-high drive.
The radio remains limited to 8 MHz.
I2C uses TWIM21 on P1, separate from SPI20.
SPIM00 shares its peripheral address with UART00, but it has no I2C controller.
BLE NUS explicitly enables the Zephyr ring buffer on nRF54L15.
Time components use `POSIX_TIMERS` instead of the deprecated `POSIX_CLOCK` option on nRF54L15.
The ADC accepts AIN0–AIN7 or their GPIO pins.
AIN0–AIN3 map to P1.04–P1.07, and AIN4–AIN7 map to P1.11–P1.14.
Its internal reference is 0.9 V with gain 1/4.
The nominal range is 3.6 V, but inputs must also meet the chip voltage limits.
Non-raw ADC values report volts.

### RRAM and updates

Both images reclaim the unused FLPR memory.
NCS 3.4.1 supplies the devicetree partitions without Partition Manager.
The physical RRAM size is 1524 KiB, not 1536 KiB.
RAM spans `0x20000000` through exclusive end `0x20040000`.

| Region | Start | Exclusive end | Size |
| --- | --- | --- | --- |
| MCUboot | `0x000000` | `0x00F800` | 62 KiB |
| Reserved gap | `0x00F800` | `0x010000` | 2 KiB |
| Primary image | `0x010000` | `0x0C2000` | 712 KiB |
| Secondary image | `0x0C2000` | `0x174000` | 712 KiB |
| ZMS settings | `0x174000` | `0x17D000` | 36 KiB |

ZMS uses all nine storage sectors through the SDK automatic sector count.

The image header is `0x800` bytes, so application vectors start at `0x10800`.
RRAM writes use 16-byte blocks, and erase pages use 4096 bytes.
The overwrite-only trailer uses 48 bytes with the 16-byte maximum alignment.
Images use hash-only validation without a signing key.
BLE SMP writes image 0 through the secondary slot with confirm-only updates.
Buffered SMP uploads can keep 4-byte transport alignment.
There is no USB or serial recovery.
The nRF54L15 MCUboot profile replaces the SDK default configuration.
It keeps boot logging disabled without RTC timer options.
The generated MCUboot fragment still applies the selected low-frequency clock.
The first flash and recovery use SWD with J-Link.
`esphome upload --device JLINK` selects the `nRF54L15_M33` device.

Sources:

- [NCS 3.4.1 dependencies](https://github.com/nrfconnect/sdk-nrf/blob/v3.4.1/west.yml).
- [DK pins](https://github.com/nrfconnect/sdk-zephyr/blob/ncs-v3.4.1/boards/nordic/nrf54l15dk/nrf54l15dk_common.dtsi).
- [Default RRAM partitions](https://github.com/nrfconnect/sdk-zephyr/blob/ncs-v3.4.1/dts/vendor/nordic/nrf54l15_cpuapp_partition.dtsi).
- [MCUboot header padding](https://github.com/nrfconnect/sdk-zephyr/blob/ncs-v3.4.1/soc/nordic/nrf54l/Kconfig.defconfig).
- [MPSL clock startup delay](https://github.com/nrfconnect/sdk-nrf/blob/v3.4.1/subsys/mpsl/init/Kconfig).

