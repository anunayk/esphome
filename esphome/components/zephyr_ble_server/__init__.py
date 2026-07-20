from dataclasses import dataclass
import logging

from esphome import automation
import esphome.codegen as cg
from esphome.components.ble_device_base.server_automation import (
    register_server_apply_action,
)
from esphome.components.const import CONF_ACCEPT, CONF_ON_NUMERIC_COMPARISON_REQUEST
from esphome.components.nrf52.framework import (
    bluetooth_west_projects,
    include_west_project,
)
from esphome.components.zephyr import zephyr_add_prj_conf
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_OTA, CONF_PLATFORM, Framework
from esphome.core import CORE, CoroPriority, coroutine_with_priority
from esphome.types import ConfigType
import esphome.final_validate as fv

# BLE LE Data Length Extension maximum LL PDU payload (BLE spec)
_DLE_MAX_PDU = 251

DOMAIN = "zephyr_ble_server"


@dataclass
class _BLEServerData:
    requested_l2cap_mtu: int = 0
    mtu_emitted: bool = False
    ble_ota: bool = False


def _get_data() -> _BLEServerData:
    if DOMAIN not in CORE.data:
        CORE.data[DOMAIN] = _BLEServerData()
    return CORE.data[DOMAIN]


@coroutine_with_priority(CoroPriority.FINAL)
async def _emit_ble_mtu() -> None:
    data = _get_data()
    data.mtu_emitted = True
    mtu = data.requested_l2cap_mtu
    if mtu > 0:
        zephyr_add_prj_conf("BT_L2CAP_TX_MTU", mtu)
        zephyr_add_prj_conf("BT_BUF_ACL_TX_SIZE", min(mtu + 4, _DLE_MAX_PDU))
        zephyr_add_prj_conf("BT_BUF_ACL_RX_SIZE", mtu + 4)


def request_ble_l2cap_mtu(l2cap_mtu: int) -> None:
    """Request a minimum BLE L2CAP MTU. The maximum of all callers wins."""
    data = _get_data()
    if data.mtu_emitted:
        raise RuntimeError("BLE L2CAP MTU requested after it was written to prj.conf")
    if data.requested_l2cap_mtu == 0:
        CORE.add_job(_emit_ble_mtu)
    data.requested_l2cap_mtu = max(data.requested_l2cap_mtu, l2cap_mtu)


zephyr_ble_server_ns = cg.esphome_ns.namespace("zephyr_ble_server")
BLEServer = zephyr_ble_server_ns.class_("BLEServer", cg.Component)

_LOGGER = logging.getLogger(__name__)
CONF_MTU = "mtu"

# OTA platform whose BLE transport runs MCUmgr DFU over the Nordic UART/SMP link.
OTA_PLATFORM_ZEPHYR_MCUMGR = "zephyr_mcumgr"
CONF_TRANSPORT = "transport"
CONF_BLE = "ble"

# BLE's default ATT_MTU. At this size each notification carries 20 bytes of payload and
# Zephyr's stock (small) ACL buffers are sufficient, so the default build needs no extra
# RAM. Raising the MTU is opt-in.
DEFAULT_MTU = 23
# An L2CAP PDU adds a 4-byte basic header on top of the ATT MTU; the ACL data buffers
# must be large enough to hold it. The controller's max Data Length (and therefore the
# largest usable ACL buffer) is capped at the BLE 4.2+ maximum.
L2CAP_HEADER_SIZE = 4
MAX_CTLR_DATA_LENGTH = 251
# Enabling CONFIG_BT_SMP (done when on_numeric_comparison_request is configured) raises
# Zephyr/NCS's minimum BT_L2CAP_TX_MTU to 65 (and BT_BUF_ACL_RX_SIZE to 65 + 4 = 69) so
# pairing PDUs fit. An explicit mtu between the default and this floor would emit
# out-of-range Kconfig, so it is rejected; leaving mtu at the default lets Zephyr apply
# these SMP minimums itself.
SMP_MIN_MTU = 65




def _is_ble_ota_configured(full_config) -> bool:
    # zephyr_mcumgr OTA over BLE is the only OTA path that shares these ACL buffers.
    for ota_conf in full_config.get(CONF_OTA, []):
        if ota_conf.get(CONF_PLATFORM) != OTA_PLATFORM_ZEPHYR_MCUMGR:
            continue
        if ota_conf.get(CONF_TRANSPORT, {}).get(CONF_BLE):
            return True
    return False


def _final_validate(config):
    if not _is_ble_ota_configured(fv.full_config.get()):
        return
    _get_data().ble_ota = True
    if config[CONF_MTU] > DEFAULT_MTU:
        _LOGGER.info(
            "'%s': mtu is %d and zephyr_mcumgr BLE OTA is configured, so Zephyr's "
            "default ACL buffer counts are kept (not trimmed). This preserves OTA "
            "throughput (NCS_SAMPLE_MCUMGR_BT_OTA_DFU_SPEEDUP relies on them) at the "
            "cost of a little extra RAM.",
            DOMAIN,
            config[CONF_MTU],
        )


def _validate_mtu_for_smp(config):
    # SMP is enabled whenever numeric comparison pairing is configured (see to_code).
    if config.get(CONF_ON_NUMERIC_COMPARISON_REQUEST) is None:
        return config
    if DEFAULT_MTU < config[CONF_MTU] < SMP_MIN_MTU:
        raise cv.Invalid(
            f"'{CONF_MTU}' must be at least {SMP_MIN_MTU} when "
            f"'{CONF_ON_NUMERIC_COMPARISON_REQUEST}' is set, because enabling Bluetooth "
            f"pairing (SMP) raises the minimum supported MTU to {SMP_MIN_MTU}. Use the "
            f"default ({DEFAULT_MTU}) or a value of at least {SMP_MIN_MTU}.",
            [CONF_MTU],
        )
    return config


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(BLEServer),
            cv.Optional(
                CONF_ON_NUMERIC_COMPARISON_REQUEST
            ): automation.validate_automation({}),
            cv.Optional(CONF_MTU, default=DEFAULT_MTU): cv.int_range(
                min=DEFAULT_MTU, max=MAX_CTLR_DATA_LENGTH - L2CAP_HEADER_SIZE
            ),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    _validate_mtu_for_smp,
    cv.only_with_framework(Framework.ZEPHYR),
)

FINAL_VALIDATE_SCHEMA = _final_validate

_CALLBACK_AUTOMATIONS = (
    automation.CallbackAutomation(
        CONF_ON_NUMERIC_COMPARISON_REQUEST,
        "add_passkey_callback",
        [(cg.uint32, "passkey")],
    ),
)


# Above safe_mode, so Bluetooth OTA can still recover a device stuck in a boot loop
@coroutine_with_priority(CoroPriority.COMMUNICATION)
async def to_code(config: ConfigType) -> None:
    var = cg.new_Pvariable(config[CONF_ID])
    zephyr_add_prj_conf("BT", True)
    for project in bluetooth_west_projects():
        include_west_project(project)
    zephyr_add_prj_conf("BT_PERIPHERAL", True)
    # Enable the BT settings subsystem unconditionally. Beyond persisting bonds, it
    # is what makes the host load/generate a valid identity (device) address after
    # bt_enable() via settings_load(). Without it the nRF SoftDevice Controller comes
    # up with an all-zero identity address and bt_le_adv_start() fails with -EINVAL,
    # so the node never advertises (no BLE logs/OTA). Previously this was only set
    # inside the numeric-comparison/SMP branch below, which left non-SMP nodes (the
    # soil sensor) unable to advertise.
    zephyr_add_prj_conf("BT_SETTINGS", True)
    zephyr_add_prj_conf("BT_RX_STACK_SIZE", 1536)
    zephyr_add_prj_conf("BT_DEVICE_NAME", CORE.name)
    # The advertised/GAP name is set at runtime from App.get_name() (see
    # ble_server.cpp) so name_add_mac_suffix yields a per-device name; that needs
    # a writable name buffer. Size it for the base name plus the "-<mac>" tail.
    zephyr_add_prj_conf("BT_DEVICE_NAME_DYNAMIC", True)
    zephyr_add_prj_conf("BT_DEVICE_NAME_MAX", 28)
    if (mtu := config[CONF_MTU]) > DEFAULT_MTU:
        # Raise the ATT MTU and enable LL Data Length Extension so each notification can carry
        # up to (mtu - 3) bytes of payload instead of the default 20. The central (e.g. macOS)
        # requests a large MTU on connect; the peripheral must advertise a matching
        # BT_L2CAP_TX_MTU and provide ACL buffers large enough to hold the L2CAP PDU
        # (MTU + 4-byte header) for the negotiation to take. This is the single biggest
        # throughput win for BLE log streaming and OTA, shrinking a burst from dozens of
        # round-trips to a handful.
        acl_size = mtu + L2CAP_HEADER_SIZE
        request_ble_l2cap_mtu(mtu)
        zephyr_add_prj_conf("BT_CTLR_DATA_LENGTH_MAX", acl_size)
        zephyr_add_prj_conf("BT_USER_DATA_LEN_UPDATE", True)
        zephyr_add_prj_conf("BT_AUTO_DATA_LEN_UPDATE", True)
        # The enlarged ACL buffers are allocated as size x count pools, so the larger size
        # multiplies against the buffer counts. Zephyr's defaults (TX 3, RX 6) are sized for a
        # host juggling several connections; this peripheral serves a single connection, so trim
        # the counts to claw back most of the RAM the bigger buffers would otherwise cost.
        # BT_L2CAP_TX_BUF_COUNT defaults to BT_BUF_ACL_TX_COUNT, so it shrinks for free too.
        #
        # Exception: zephyr_mcumgr BLE OTA turns on NCS_SAMPLE_MCUMGR_BT_OTA_DFU_SPEEDUP, which
        # leans on those default counts for throughput. Trimming them would silently slow OTA, so
        # keep Zephyr's stock counts when BLE OTA is configured (see _final_validate, which also
        # logs that this RAM trade-off is being made).
        if not _get_data().ble_ota:
            zephyr_add_prj_conf("BT_BUF_ACL_TX_COUNT", 2)
            zephyr_add_prj_conf("BT_BUF_ACL_RX_COUNT", 3)
    await cg.register_component(var, config)
    if config.get(CONF_ON_NUMERIC_COMPARISON_REQUEST):
        zephyr_add_prj_conf("BT_SMP", True)
        zephyr_add_prj_conf("BT_SETTINGS", True)
        zephyr_add_prj_conf("BT_SMP_SC_ONLY", True)
        zephyr_add_prj_conf("BT_KEYS_OVERWRITE_OLDEST", True)
        request_ble_l2cap_mtu(65)  # BT_SMP Kconfig default and range minimum
    # Pairing confirmation is the user's automation, so in safe mode new pairings
    # are rejected (ble_server.cpp) and only bonded hosts can connect
    await cg.past_safe_mode()
    await automation.build_callback_automations(var, config, _CALLBACK_AUTOMATIONS)


BLE_NUMERIC_COMPARISON_REPLY_ACTION_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_ID): cv.use_id(BLEServer),
        cv.Required(CONF_ACCEPT): cv.templatable(cv.boolean),
    }
)


register_server_apply_action(
    "ble_server.numeric_comparison_reply",
    lambda: CORE.using_zephyr,
    BLE_NUMERIC_COMPARISON_REPLY_ACTION_SCHEMA,
    automation.ApplyField(CONF_ACCEPT, "numeric_comparison_reply", cg.bool_),
)

# Link-gated advertising control (the BLE analogue of wifi.enable/disable). node-soil's
# ble_policy calls these off the hub link: stop advertising while hub-linked, start it
# again for a bounded operator "wake" window. stop_advertising also drops an active
# central (disconnect-on-stop), except during an OTA.
BLEStartAdvertisingAction = zephyr_ble_server_ns.class_(
    "BLEStartAdvertisingAction", automation.Action
)
BLEStopAdvertisingAction = zephyr_ble_server_ns.class_(
    "BLEStopAdvertisingAction", automation.Action
)

BLE_ADVERTISING_ACTION_SCHEMA = automation.maybe_simple_id(
    {
        cv.GenerateID(): cv.use_id(BLEServer),
    }
)


# synchronous=True: play() calls set_advertising_enabled() and returns before
# play_next_() runs (no deferral to a callback/timer/loop), like numeric_comparison_reply.
@automation.register_action(
    "ble_server.start_advertising",
    BLEStartAdvertisingAction,
    BLE_ADVERTISING_ACTION_SCHEMA,
    synchronous=True,
)
async def start_advertising_to_code(config, action_id, template_arg, args):
    parent = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_arg, parent)


@automation.register_action(
    "ble_server.stop_advertising",
    BLEStopAdvertisingAction,
    BLE_ADVERTISING_ACTION_SCHEMA,
    synchronous=True,
)
async def stop_advertising_to_code(config, action_id, template_arg, args):
    parent = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_arg, parent)
