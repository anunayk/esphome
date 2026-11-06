"""UART selection for Nordic variants."""

import pytest

@pytest.mark.parametrize(
    ("variant", "label"),
    [("NRF54L15", "uart20"), ("NRF54LM20", "uart20"), ("NRF52840", "uart0")],
)
def test_zephyr_uart0_label(variant: str, label: str) -> None:
    from esphome.components.logger import _zephyr_uart0_label
    from esphome.components.nrf52.framework import set_nrf52_variant

    set_nrf52_variant(variant)
    assert _zephyr_uart0_label() == label


def test_nrf54l15_rejects_usb_cdc() -> None:
    from esphome.components.logger import uart_selection
    from esphome.components.nrf52.const import VARIANT_NRF54L15
    from esphome.components.nrf52.framework import set_nrf52_variant
    import esphome.config_validation as cv
    from esphome.const import KEY_CORE, KEY_TARGET_PLATFORM, PLATFORM_NRF52
    from esphome.core import CORE

    CORE.data.setdefault(KEY_CORE, {})[KEY_TARGET_PLATFORM] = PLATFORM_NRF52
    set_nrf52_variant(VARIANT_NRF54L15)
    assert uart_selection("uart0") == "UART0"
    with pytest.raises(cv.Invalid):
        uart_selection("USB_CDC")
