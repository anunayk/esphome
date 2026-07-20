"""Tests for nRF52 per-board bootloader selection and validation."""

import pytest

from esphome.components import nrf52
from esphome.components.zephyr.const import BOOTLOADER_MCUBOOT, KEY_BOOTLOADER
import esphome.config_validation as cv
from esphome.const import CONF_BOARD


def test_xiao_ble_accepts_mcuboot_bootloader() -> None:
    """xiao_ble lists mcuboot as a supported two-slot bootloader."""
    config = nrf52._detect_bootloader(
        {CONF_BOARD: "xiao_ble", KEY_BOOTLOADER: BOOTLOADER_MCUBOOT}
    )

    assert config[KEY_BOOTLOADER] == BOOTLOADER_MCUBOOT


def test_unsupported_board_rejects_mcuboot_bootloader() -> None:
    """Boards that do not list mcuboot still reject an explicit selection."""
    with pytest.raises(cv.Invalid, match="does not support"):
        nrf52._detect_bootloader(
            {
                CONF_BOARD: "adafruit_itsybitsy_nrf52840",
                KEY_BOOTLOADER: BOOTLOADER_MCUBOOT,
            }
        )


def test_xiao_ble_default_bootloader_is_not_mcuboot() -> None:
    """Without an explicit selection xiao_ble keeps its first (Adafruit) default."""
    config = nrf52._detect_bootloader({CONF_BOARD: "xiao_ble"})

    assert config[KEY_BOOTLOADER] != BOOTLOADER_MCUBOOT
