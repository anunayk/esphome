"""Regression coverage for the ISC-L1512-B integration."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from esphome.components import nrf52, spi
from esphome.components.nrf52.const import VARIANT_NRF54L15, VARIANT_NRF52840
from esphome.components.nrf52.framework import set_nrf52_variant
from esphome.components.nrf52.gpio import analog_pin_map, validate_gpio_pin
from esphome.components.zephyr import zephyr_data, zephyr_setup_preferences
from esphome.components.zephyr.const import (
    BOOTLOADER_MCUBOOT,
    KEY_EXTRA_BUILD_FILES,
    KEY_OVERLAY,
    KEY_PM_STATIC,
    KEY_PRJ_CONF,
    KEY_SYSBUILD_CONF,
    KEY_ZEPHYR,
)
import esphome.config_validation as cv
from esphome.const import (
    CONF_CLK_PIN,
    CONF_DATA_RATE,
    CONF_HARDWARE_UART,
    CONF_ID,
    CONF_INVERTED,
    CONF_MISO_PIN,
    CONF_MOSI_PIN,
    CONF_NUMBER,
    CONF_SPI_ID,
    KEY_CORE,
    KEY_FRAMEWORK_VERSION,
    KEY_TARGET_FRAMEWORK,
    KEY_TARGET_PLATFORM,
    PLATFORM_NRF52,
    Toolchain,
)
from esphome.core import CORE
import esphome.final_validate as fv


@pytest.fixture
def nrf54(setup_core: Path) -> dict:
    CORE.toolchain = Toolchain.SDK_NRF
    CORE.data[KEY_CORE] = {
        KEY_TARGET_PLATFORM: PLATFORM_NRF52,
        KEY_TARGET_FRAMEWORK: KEY_ZEPHYR,
        KEY_FRAMEWORK_VERSION: cv.Version(2, 9, 2),
    }
    config = nrf52.CONFIG_SCHEMA(
        {
            "board": "nrf54l15dk/nrf54l15/cpuapp",
            "bootloader": BOOTLOADER_MCUBOOT,
            "lfclk_source": "rc",
            "nfct_pins_as_gpios": True,
            "dk_peripherals": False,
        }
    )
    return config


def test_rram_layout_and_header(nrf54: dict) -> None:
    nrf52._mcuboot_to_code()
    sections = {section.name: section for section in zephyr_data()[KEY_PM_STATIC]}
    assert sections["mcuboot"].address == 0
    assert sections["mcuboot"].end_address == 0x10000
    assert sections["mcuboot_secondary"].address == 0xC2000
    assert sections["mcuboot_secondary"].size == 0xB2000
    assert sections["settings_storage"].address == 0x174000
    assert sections["settings_storage"].end_address == 0x17D000
    assert 0xC2000 - 0x10000 == sections["mcuboot_secondary"].size
    conf = zephyr_data()[KEY_SYSBUILD_CONF]
    assert conf["SB_CONFIG_BOOT_SIGNATURE_TYPE_NONE"] is True
    assert conf["SB_CONFIG_MCUBOOT_MODE_OVERWRITE_ONLY"] is True
    assert conf["SB_CONFIG_PM_MCUBOOT_PAD"].value == 0x800
    for image in ("", "mcuboot"):
        overlay = zephyr_data()[KEY_OVERLAY][image]
        assert "&cpuapp_rram" in overlay
        assert "/delete-node/ partitions" in overlay
        assert "partition@10000" in overlay
        assert "partition@c2000" in overlay
        assert "partition@174000" in overlay
        assert "zephyr_udc0" not in overlay
    kconfig = zephyr_data()[KEY_EXTRA_BUILD_FILES]["zephyr/Kconfig.sysbuild"]
    assert "config MCUBOOT_FPROTECT_ALLOW_COMBINED_REGIONS\n\tdefault n" in kconfig.read_text()
    fragment = zephyr_data()[KEY_EXTRA_BUILD_FILES]["zephyr/sysbuild/mcuboot/prj.conf"]
    assert "CONFIG_MCUBOOT_SERIAL=n" in fragment.read_text()
    assert "CONFIG_NCS_BOOT_BANNER=n" in fragment.read_text()
    assert "CONFIG_BOOT_MAX_IMG_SECTORS=256" in fragment.read_text()


def test_module_overlay_and_lfrc_reach_both_images(nrf54: dict) -> None:
    nrf52._nrf54l15_board_to_code(nrf54)
    for image in ("", "mcuboot"):
        overlay = zephyr_data()[KEY_OVERLAY][image]
        assert "reg = <0x0 0x17d000>" in overlay
        assert "reg = <0x20000000 0x40000>" in overlay
        assert "ranges = <0x0 0x20000000 0x40000>" in overlay
        assert '&uart20 { status = "disabled"; }' in overlay
        assert '&mx25r64 { status = "disabled"; }' in overlay
        assert '&button0 { status = "disabled"; }' in overlay
        assert '&led3 { status = "disabled"; }' in overlay
        assert "nfct-pins-as-gpios" in overlay
        assert "/delete-property/ load-capacitors" in overlay
        conf = zephyr_data()[KEY_PRJ_CONF][image]
        assert conf["CONFIG_CLOCK_CONTROL_NRF_K32SRC_RC"][0] is True
        assert conf["CONFIG_CLOCK_CONTROL_NRF_K32SRC_XTAL"][0] is False
        assert conf["CONFIG_CLOCK_CONTROL_NRF_K32SRC_RC_CALIBRATION"][0] is True
    assert '&wdt31 { status = "okay"; }' in zephyr_data()[KEY_OVERLAY][""]


@pytest.mark.parametrize(
    "variant,backend",
    [(VARIANT_NRF54L15, "ZMS"), (VARIANT_NRF52840, "NVS")],
)
def test_settings_backend(nrf54: dict, variant: str, backend: str) -> None:
    set_nrf52_variant(variant)
    with patch("esphome.components.zephyr.cg.add"):
        zephyr_setup_preferences()
    conf = zephyr_data()[KEY_PRJ_CONF][""]
    assert conf[f"CONFIG_{backend}"][0] is True
    if variant == VARIANT_NRF54L15:
        assert conf["CONFIG_SETTINGS_ZMS"][0] is True
        assert "CONFIG_NVS" not in conf
    else:
        assert "CONFIG_ZMS" not in conf


@pytest.mark.parametrize("channel,pin", list(enumerate((36, 37, 38, 39, 43, 44, 45, 46))))
def test_analog_map(nrf54: dict, channel: int, pin: int) -> None:
    assert analog_pin_map()[f"AIN{channel}"] == pin
    assert validate_gpio_pin(f"AIN{channel}") == pin


def test_nrf54_rejects_old_adc_supply_input(nrf54: dict) -> None:
    with pytest.raises(cv.Invalid, match="VDDHDIV5"):
        validate_gpio_pin("VDDHDIV5")


def test_nrf54_sdk_and_bootloader(nrf54: dict) -> None:
    assert nrf54["framework"]["version"] == "2.9.2"
    with pytest.raises(cv.Invalid, match="does not support"):
        nrf52._detect_bootloader({"board": nrf54["board"], "bootloader": "adafruit"})


def _bus(name: str, interface: str, pins: tuple[int, int, int]) -> dict:
    clk, mosi, miso = pins
    return {
        CONF_ID: name,
        "interface": interface,
        CONF_CLK_PIN: {CONF_NUMBER: clk, CONF_INVERTED: False},
        CONF_MOSI_PIN: {CONF_NUMBER: mosi, CONF_INVERTED: False},
        CONF_MISO_PIN: {CONF_NUMBER: miso, CONF_INVERTED: False},
    }


def test_two_spi_power_domains(nrf54: dict) -> None:
    buses = spi.validate_spi_config(
        [_bus("radio", "spi20", (38, 37, 36)), _bus("rtd", "spi00", (70, 72, 73))]
    )
    assert [bus[spi.CONF_INTERFACE_INDEX] for bus in buses] == [0, 1]
    assert spi.get_spi_interface(0) == "DEVICE_DT_GET(DT_NODELABEL(spi20))"
    assert spi.get_spi_interface(1) == "DEVICE_DT_GET(DT_NODELABEL(spi00))"
    assert spi._frequency_validator("32MHz") == 32e6
    token = fv.full_config.set({"sensor": [{CONF_SPI_ID: "radio", CONF_DATA_RATE: 16e6}]})
    try:
        with pytest.raises(cv.Invalid, match="power domain"):
            spi._final_validate(buses)
    finally:
        fv.full_config.reset(token)


@pytest.mark.parametrize(
    "interface,pins",
    [("spi20", (70, 72, 73)), ("spi00", (38, 37, 36)), ("spi20", (38, 72, 36))],
)
def test_spi_rejects_cross_domain_pins(
    nrf54: dict, interface: str, pins: tuple[int, int, int]
) -> None:
    with pytest.raises(cv.Invalid, match="Invalid pin"):
        spi.validate_spi_config([_bus("wrong", interface, pins)])


def test_uart30_bench_pinctrl(nrf54: dict) -> None:
    from esphome.components import logger

    config = logger.CONFIG_SCHEMA({CONF_HARDWARE_UART: "UART30", "baud_rate": 115200})
    with (
        patch.object(logger.cg, "get_variable", AsyncMock(return_value=MagicMock())),
        patch.object(logger.cg, "register_component", AsyncMock()),
        patch.object(logger.cg, "add"),
        patch.object(logger.cg, "add_build_flag"),
        patch.object(logger.cg, "add_define") as defines,
        patch.object(CORE, "add_job"),
    ):
        asyncio.run(logger._late_logger_init(config))
    overlay = zephyr_data()[KEY_OVERLAY][""]
    assert "NRF_PSEL(UART_TX, 0, 3)" in overlay
    assert "NRF_PSEL(UART_RX, 0, 4)" in overlay
    assert "&uart30" in overlay
    assert any(
        call.args[0] == "ESPHOME_LOGGER_UART0_NODE" and str(call.args[1]) == "uart30"
        for call in defines.call_args_list
    )


def test_adc_uses_nrf54_reference_and_gain(nrf54: dict) -> None:
    from esphome.components.adc import sensor as adc

    config = {
        "id": "battery",
        "pin": {CONF_NUMBER: 43},
        "raw": False,
        "samples": 1,
        "sampling_mode": "avg",
        "nrf_saadc": "battery_adc",
    }
    with (
        patch.object(adc.cg, "new_Pvariable", return_value=MagicMock()),
        patch.object(adc.cg, "register_component", AsyncMock()),
        patch.object(adc.cg, "gpio_pin_expression", AsyncMock(return_value=MagicMock())),
        patch.object(adc.cg, "add"),
        patch.object(adc.sensor, "register_sensor", AsyncMock()),
    ):
        asyncio.run(adc.to_code(config))
    overlay = zephyr_data()[KEY_OVERLAY][""]
    assert 'zephyr,gain = "ADC_GAIN_1_4"' in overlay
    assert "zephyr,vref-mv = <900>" in overlay
    assert "NRF_SAADC_AIN4" in overlay


def test_spi_codegen_uses_distinct_nodes(nrf54: dict) -> None:
    buses = spi.validate_spi_config(
        [_bus("radio", "spi20", (38, 37, 36)), _bus("rtd", "spi00", (70, 72, 73))]
    )
    with (
        patch.object(spi.cg, "new_Pvariable", return_value=MagicMock()),
        patch.object(spi.cg, "register_component", AsyncMock()),
        patch.object(spi.cg, "gpio_pin_expression", AsyncMock(return_value=MagicMock())),
        patch.object(spi.cg, "add"),
        patch.object(spi.cg, "add_global"),
    ):
        asyncio.run(spi.to_code(buses))
    overlay = zephyr_data()[KEY_OVERLAY][""]
    for node in ("spi20", "spi00"):
        assert f"&{node}" in overlay
        assert f"{node}_default: {node}_default" in overlay
    assert "NRF_PSEL(SPIM_SCK, 1, 6)" in overlay
    assert "NRF_PSEL(SPIM_SCK, 2, 6)" in overlay
    assert "NRF_DRIVE_E0E1" in overlay
    assert "&i2c00" not in overlay
    assert '&i2c20 { status = "disabled"; }' in overlay
    assert '&uart00 { status = "disabled"; }' in overlay


def test_i2c_codegen_uses_twim21(nrf54: dict) -> None:
    from esphome.components import i2c
    from esphome.config import path_context

    token = path_context.set(["i2c"])
    try:
        config = i2c.CONFIG_SCHEMA(
            {"sda": "P1.09", "scl": "P1.10", "frequency": "100kHz"}
        )
    finally:
        path_context.reset(token)
    with (
        patch.object(i2c.cg, "new_Pvariable", return_value=MagicMock()),
        patch.object(i2c.cg, "register_component", AsyncMock()),
        patch.object(i2c.cg, "add"),
        patch.object(i2c.cg, "add_global"),
    ):
        asyncio.run(i2c.to_code(config))
    overlay = zephyr_data()[KEY_OVERLAY][""]
    assert "&i2c21" in overlay
    assert "NRF_PSEL(TWIM_SDA, 1, 9)" in overlay
    assert "NRF_PSEL(TWIM_SCL, 1, 10)" in overlay


@pytest.mark.parametrize("variant", [VARIANT_NRF54L15, VARIANT_NRF52840])
def test_nus_ring_buffer_dependency(nrf54: dict, variant: str) -> None:
    from esphome.components import ble_nus

    set_nrf52_variant(variant)
    config = ble_nus.CONFIG_SCHEMA({})
    with (
        patch.object(ble_nus.cg, "new_Pvariable", return_value=MagicMock()),
        patch.object(ble_nus.cg, "register_component", AsyncMock()),
        patch.object(ble_nus.cg, "add"),
        patch.object(ble_nus.cg, "add_define"),
        patch.object(ble_nus, "request_log_listener"),
    ):
        asyncio.run(ble_nus.to_code(config))
    conf = zephyr_data()[KEY_PRJ_CONF][""]
    if variant == VARIANT_NRF54L15:
        assert conf["CONFIG_RING_BUFFER"][0] is True
    else:
        assert "CONFIG_RING_BUFFER" not in conf


@pytest.mark.parametrize(
    "variant,option",
    [(VARIANT_NRF54L15, "POSIX_TIMERS"), (VARIANT_NRF52840, "POSIX_CLOCK")],
)
def test_time_clock_option(nrf54: dict, variant: str, option: str) -> None:
    from esphome.components import time

    set_nrf52_variant(variant)
    with (
        patch.object(time.cg, "add_define"),
        patch.object(time.cg, "add_global"),
    ):
        asyncio.run(time.to_code({}))
    assert zephyr_data()[KEY_PRJ_CONF][""][f"CONFIG_{option}"][0] is True
