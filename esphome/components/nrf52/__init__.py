from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
import shutil
import subprocess

from esphome import pins
import esphome.codegen as cg
from esphome.components.zephyr import (
    Section,
    add_extra_build_file,
    add_extra_script,
    copy_files as zephyr_copy_files,
    zephyr_add_overlay,
    zephyr_add_pm_static,
    zephyr_add_prj_conf,
    zephyr_add_sysbuild_conf,
    zephyr_data,
    zephyr_set_core_data,
    zephyr_setup_preferences,
    zephyr_to_code,
)
from esphome.components.zephyr.const import (
    BOOTLOADER_MCUBOOT,
    CONF_CDC_ACM,
    KEY_BOARD,
    KEY_BOOTLOADER,
    KEY_ZEPHYR,
    CdcAcm,
)
import esphome.config_validation as cv
from esphome.const import (
    CONF_ADVANCED,
    CONF_BOARD,
    CONF_DISABLED,
    CONF_ENABLE_OTA_ROLLBACK,
    CONF_FRAMEWORK,
    CONF_ID,
    CONF_OTA,
    CONF_PLATFORM,
    CONF_RESET_PIN,
    CONF_SAFE_MODE,
    CONF_TOOLCHAIN,
    CONF_VERSION,
    CONF_VOLTAGE,
    KEY_CORE,
    KEY_FRAMEWORK_VERSION,
    KEY_TARGET_FRAMEWORK,
    KEY_TARGET_PLATFORM,
    PLATFORM_NRF52,
    ThreadModel,
    Toolchain,
)
from esphome.core import CORE, CoroPriority, EsphomeError, coroutine_with_priority
from esphome.core.config import BOARD_MAX_LENGTH
import esphome.final_validate as fv
from esphome.framework_helpers import (
    get_project_compile_flags,
    get_project_link_flags,
    run_command_ok,
)
from esphome.helpers import rmtree, write_file_if_changed
from esphome.storage_json import StorageJSON
from esphome.types import ConfigType

from .boards import BOARDS_ZEPHYR, BOOTLOADER_CONFIG
from .const import (
    BOOTLOADER_ADAFRUIT,
    BOOTLOADER_ADAFRUIT_NRF52_SD132,
    BOOTLOADER_ADAFRUIT_NRF52_SD140_V6,
    BOOTLOADER_ADAFRUIT_NRF52_SD140_V7,
)
from .framework import (
    check_and_install,
    get_build_env,
    get_build_paths,
    setup_platformio_python_env,
)

# force import gpio to register pin schema
from .gpio import nrf52_pin_to_code  # noqa: F401

CODEOWNERS = ["@tomaszduda23"]
AUTO_LOAD = ["zephyr", "preferences"]
IS_TARGET_PLATFORM = True
_LOGGER = logging.getLogger(__name__)

# Default framework versions per toolchain. The sdk-nrf one also keys the CI
# sdk-nrf install cache and pins the clang-tidy project's SDK.
RECOMMENDED_PLATFORMIO_VERSION = "2.6.1-b"
RECOMMENDED_SDK_NRF_VERSION = "2.9.2"

FAKE_BOARD_MANIFEST = """
{
    "frameworks": [
        "zephyr"
    ],
    "name": "esphome nrf52",
    "upload": {
        "maximum_ram_size": 248832,
        "maximum_size": 815104,
        "speed": 115200
    },
    "url": "https://esphome.io/",
    "vendor": "esphome",
    "build": {
        "bsp": {
            "name": "adafruit"
        },
        "softdevice": {
            "sd_fwid": "0x00B6"
        }
    }
}
"""


def set_core_data(config: ConfigType) -> ConfigType:
    zephyr_set_core_data(config)
    CORE.data[KEY_CORE][KEY_TARGET_PLATFORM] = PLATFORM_NRF52
    CORE.data[KEY_CORE][KEY_TARGET_FRAMEWORK] = KEY_ZEPHYR

    if config[KEY_BOOTLOADER] in BOOTLOADER_CONFIG:
        zephyr_add_pm_static(BOOTLOADER_CONFIG[config[KEY_BOOTLOADER]])

    return config


def _resolve_toolchain(config: ConfigType) -> ConfigType:
    if CORE.toolchain is None:
        CORE.toolchain = config.get(CONF_TOOLCHAIN, Toolchain.SDK_NRF)
    return config


def set_framework(config: ConfigType) -> ConfigType:
    if CONF_VERSION not in config[CONF_FRAMEWORK]:
        default_version = (
            RECOMMENDED_PLATFORMIO_VERSION
            if CORE.using_toolchain_platformio
            else RECOMMENDED_SDK_NRF_VERSION
        )
        config = {
            **config,
            CONF_FRAMEWORK: {**config[CONF_FRAMEWORK], CONF_VERSION: default_version},
        }
    framework_ver = cv.Version.parse(
        cv.version_number(config[CONF_FRAMEWORK][CONF_VERSION])
    )
    CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION] = framework_ver
    if not CORE.using_toolchain_platformio:
        return config
    if framework_ver < cv.Version(2, 9, 2):
        return cv.require_framework_version(
            nrf52_zephyr=cv.Version(2, 6, 1, "a"),
        )(config)
    if framework_ver < cv.Version(3, 2, 0):
        return cv.require_framework_version(
            nrf52_zephyr=cv.Version(2, 9, 2, "2"),
        )(config)
    return cv.require_framework_version(
        nrf52_zephyr=cv.Version(3, 2, 0, "1"),
    )(config)


BOOTLOADERS = [
    BOOTLOADER_ADAFRUIT,
    BOOTLOADER_ADAFRUIT_NRF52_SD132,
    BOOTLOADER_ADAFRUIT_NRF52_SD140_V6,
    BOOTLOADER_ADAFRUIT_NRF52_SD140_V7,
    BOOTLOADER_MCUBOOT,
]


def _validate_toolchain(value) -> Toolchain:
    return Toolchain(
        cv.one_of(Toolchain.PLATFORMIO, Toolchain.SDK_NRF, lower=True)(value)
    )


def _detect_bootloader(config: ConfigType) -> ConfigType:
    """Detect the bootloader for the given board."""
    config = config.copy()
    bootloaders: list[str] = []
    board = config[CONF_BOARD]

    if board in BOARDS_ZEPHYR and KEY_BOOTLOADER in BOARDS_ZEPHYR[board]:
        # this board have bootloaders config available
        bootloaders = BOARDS_ZEPHYR[board][KEY_BOOTLOADER]

    if KEY_BOOTLOADER not in config:
        if bootloaders:
            # there is no bootloader in config -> take first one
            config[KEY_BOOTLOADER] = bootloaders[0]
        else:
            # make mcuboot as default if there is no configuration for that board
            config[KEY_BOOTLOADER] = BOOTLOADER_MCUBOOT
    elif bootloaders and config[KEY_BOOTLOADER] not in bootloaders:
        raise cv.Invalid(
            f"{board} does not support {config[KEY_BOOTLOADER]}, select one of: {', '.join(bootloaders)}"
        )
    return config


nrf52_ns = cg.esphome_ns.namespace("nrf52")
DeviceFirmwareUpdate = nrf52_ns.class_("DeviceFirmwareUpdate", cg.Component)

CONF_DFU = "dfu"
CONF_DCDC = "dcdc"
CONF_LIBC_NANO = "libc_nano"
CONF_MCUBOOT = "mcuboot"
CONF_REG0 = "reg0"
CONF_UICR_ERASE = "uicr_erase"
CONF_USB_CDC_RECOVERY = "usb_cdc_recovery"
CONF_TWO_SLOT = "two_slot"
CONF_MIGRATOR_IMAGE = "migrator_image"

VOLTAGE_LEVELS = [1.8, 2.1, 2.4, 2.7, 3.0, 3.3]


_DFU_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(DeviceFirmwareUpdate),
        cv.Optional(CONF_RESET_PIN): pins.gpio_output_pin_schema,
    }
)


def _dfu_schema(value: bool | ConfigType) -> ConfigType:
    if isinstance(value, bool):
        if not value:
            raise cv.Invalid("Use 'dfu: true' or specify a configuration dict")
        return _DFU_SCHEMA({})
    return _DFU_SCHEMA(value)


def _validate_mcuboot(config: ConfigType) -> ConfigType:
    if CONF_MCUBOOT not in config:
        return config
    if config[KEY_BOOTLOADER] != BOOTLOADER_MCUBOOT:
        raise cv.Invalid("mcuboot: is only valid with bootloader: mcuboot")
    if config[CONF_MCUBOOT][CONF_USB_CDC_RECOVERY] and config[CONF_BOARD] != "xiao_ble":
        raise cv.Invalid("mcuboot.usb_cdc_recovery is only supported on xiao_ble")
    if config[CONF_MCUBOOT][CONF_TWO_SLOT] and config[CONF_BOARD] != "xiao_ble":
        raise cv.Invalid("mcuboot.two_slot is only supported on xiao_ble")
    if (
        config[CONF_MCUBOOT][CONF_TWO_SLOT]
        and config[CONF_MCUBOOT][CONF_USB_CDC_RECOVERY]
    ):
        # usb_cdc_recovery is the single-slot fw1 bootloader; two_slot builds the
        # relocated two-slot fw2 that fw1 is migrated *to*. They are different
        # flash layouts and cannot be built into one image.
        raise cv.Invalid(
            "mcuboot.two_slot and mcuboot.usb_cdc_recovery are mutually "
            "exclusive: usb_cdc_recovery builds the single-slot fw1 bootloader, "
            "two_slot builds the relocated two-slot fw2 that fw1 migrates to."
        )
    if (
        config[CONF_MCUBOOT][CONF_MIGRATOR_IMAGE]
        and not config[CONF_MCUBOOT][CONF_TWO_SLOT]
    ):
        # two_slot builds the app in the fw2 two-slot layout; migrator_image is
        # the one-time fw1 -> fw2 installer that embeds *this build's* relocated
        # fw2 bootloader, so it can only be produced from a two_slot build.
        raise cv.Invalid(
            "mcuboot.migrator_image requires mcuboot.two_slot: the installer "
            "image embeds the relocated fw2 bootloader that the two_slot layout "
            "produces."
        )
    return config


def _mcuboot_usb_cdc_recovery_enabled(config: ConfigType) -> bool:
    mcuboot_config = config.get(CONF_MCUBOOT, {})
    return mcuboot_config.get(CONF_USB_CDC_RECOVERY, False)


def _mcuboot_two_slot_enabled(config: ConfigType) -> bool:
    mcuboot_config = config.get(CONF_MCUBOOT, {})
    return mcuboot_config.get(CONF_TWO_SLOT, False)


def _validate_usb_cdc_recovery_ota(config: ConfigType, full_config: ConfigType) -> None:
    """Reject usb_cdc_recovery combined with a runtime zephyr_mcumgr OTA.

    usb_cdc_recovery builds a single-application-slot MCUboot
    (CONFIG_SINGLE_APPLICATION_SLOT=y) that only updates through its own
    USB-CDC serial recovery mode. It has no secondary slot and never swaps, so
    a zephyr_mcumgr OTA (e.g. over BLE) silently writes an image into a slot the
    bootloader ignores: the upload "succeeds" but the device keeps booting the
    old firmware. These two cannot coexist, so fail loudly instead.
    """
    if not _mcuboot_usb_cdc_recovery_enabled(config):
        return
    for ota_conf in full_config.get(CONF_OTA, []):
        if ota_conf.get(CONF_PLATFORM) == "zephyr_mcumgr":
            raise cv.Invalid(
                "mcuboot.usb_cdc_recovery builds a single-slot MCUboot that has no "
                "secondary slot and can only be updated through its USB-CDC serial "
                "recovery mode. A 'zephyr_mcumgr' OTA (e.g. over BLE) would upload "
                "an image the bootloader never swaps in, so the device keeps "
                "booting the old firmware. Remove 'usb_cdc_recovery: true' to build "
                "the two-slot swap bootloader that supports OTA, or drop the 'ota:' "
                "block and update via USB-CDC serial recovery."
            )


CONFIG_SCHEMA = cv.All(
    _detect_bootloader,
    set_core_data,
    cv.Schema(
        {
            cv.Required(CONF_BOARD): cv.All(
                cv.string_strict, cv.ByteLength(max=BOARD_MAX_LENGTH)
            ),
            cv.Optional(KEY_BOOTLOADER): cv.one_of(*BOOTLOADERS, lower=True),
            cv.Optional(CONF_DFU): _dfu_schema,
            cv.Optional(CONF_MCUBOOT): cv.Schema(
                {
                    cv.Optional(CONF_USB_CDC_RECOVERY, default=False): cv.boolean,
                    cv.Optional(CONF_TWO_SLOT, default=False): cv.boolean,
                    cv.Optional(CONF_MIGRATOR_IMAGE, default=False): cv.boolean,
                }
            ),
            cv.Optional(CONF_DCDC): cv.boolean,
            cv.Optional(CONF_REG0): cv.Schema(
                {
                    cv.Required(CONF_VOLTAGE): cv.All(
                        cv.voltage,
                        cv.one_of(*VOLTAGE_LEVELS, float=True),
                    ),
                    cv.Optional(CONF_UICR_ERASE, default=False): cv.boolean,
                }
            ),
            cv.Optional(
                CONF_FRAMEWORK,
                default={},
            ): cv.Schema(
                {
                    cv.Optional(CONF_VERSION): cv.string_strict,
                    cv.Optional(CONF_LIBC_NANO, default=True): cv.boolean,
                    cv.Optional(
                        CONF_ADVANCED, default={}, visibility=cv.Visibility.YAML_ONLY
                    ): cv.Schema(
                        {
                            cv.Optional(
                                CONF_ENABLE_OTA_ROLLBACK, default=True
                            ): cv.boolean,
                        }
                    ),
                }
            ),
            cv.Optional(CONF_TOOLCHAIN): _validate_toolchain,
            cv.GenerateID(CONF_CDC_ACM): cv.declare_id(CdcAcm),
        }
    ),
    _resolve_toolchain,
    _validate_mcuboot,
    set_framework,
)


def _validate_mcumgr(config):
    bootloader = zephyr_data()[KEY_BOOTLOADER]
    if bootloader == BOOTLOADER_MCUBOOT:
        raise cv.Invalid(f"'{bootloader}' bootloader does not support DFU")


def _final_validate(config):

    if CONF_DFU in config:
        _validate_mcumgr(config)
    if config[KEY_BOOTLOADER] == BOOTLOADER_ADAFRUIT:
        _LOGGER.warning(
            "Selected generic Adafruit bootloader. The board might crash. Consider settings `bootloader:`"
        )
    full_config = fv.full_config.get()
    _validate_usb_cdc_recovery_ota(config, full_config)
    conf = config[CONF_FRAMEWORK]
    advanced = conf[CONF_ADVANCED]

    if conf[CONF_LIBC_NANO] and "logger" in CORE.loaded_integrations:
        _LOGGER.warning(
            "Logger is enabled with newlib-nano (libc_nano: true). Some format specifiers "
            "such as %%zu are not supported and will print incorrectly. "
            "Set 'libc_nano: false' under 'framework:' to use the full newlib."
        )

    if advanced[CONF_ENABLE_OTA_ROLLBACK]:
        # "disabled: false" means safe mode *is* enabled.
        safe_mode_config = full_config.get(CONF_SAFE_MODE, {CONF_DISABLED: True})
        safe_mode_enabled = not safe_mode_config[CONF_DISABLED]
        ota_enabled = CONF_OTA in full_config
        # Both need to be enabled for rollback to work
        if not (ota_enabled and safe_mode_enabled):
            # But only warn if ota is even possible
            if ota_enabled:
                _LOGGER.warning(
                    "OTA rollback requires safe_mode, disabling rollback support"
                )
            # disable the rollback feature anyway since it can't be used.
            advanced[CONF_ENABLE_OTA_ROLLBACK] = False


FINAL_VALIDATE_SCHEMA = _final_validate


@coroutine_with_priority(CoroPriority.PLATFORM)
async def to_code(config: ConfigType) -> None:
    """Convert the configuration to code."""
    cg.add_build_flag("-DUSE_NRF52")
    cg.add_define("ESPHOME_BOARD", config[CONF_BOARD])
    cg.add_define("ESPHOME_VARIANT", "NRF52")
    # nRF52 processors are single-core
    cg.add_define(ThreadModel.SINGLE)
    if CORE.using_toolchain_platformio:
        cg.add_platformio_option("board", config[CONF_BOARD])
        cg.add_platformio_option(
            CONF_FRAMEWORK, CORE.data[KEY_CORE][KEY_TARGET_FRAMEWORK]
        )
        cg.add_platformio_option(
            "platform",
            "https://github.com/tomaszduda23/platform-nordicnrf52/archive/refs/tags/v10.3.0-5.zip",
        )
        cg.add_platformio_option(
            "platform_packages",
            [
                f"platformio/framework-zephyr@https://github.com/tomaszduda23/framework-sdk-nrf/archive/refs/tags/v{CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION]}.zip",
            ],
        )
        if config[KEY_BOOTLOADER] != BOOTLOADER_MCUBOOT:
            # make sure that firmware.zip is created
            # for Adafruit_nRF52_Bootloader
            cg.add_platformio_option("board_upload.protocol", "nrfutil")
            cg.add_platformio_option("board_upload.use_1200bps_touch", "true")
            cg.add_platformio_option("board_upload.require_upload_port", "true")
            cg.add_platformio_option("board_upload.wait_for_upload_port", "true")

        add_extra_script(
            "pre",
            "pre_build.py",
            Path(__file__).parent / "pre_build.py.script",
        )
        # build is done by west so bypass board checking in platformio
        cg.add_platformio_option("boards_dir", CORE.relative_build_path("boards"))

    if config[KEY_BOOTLOADER] == BOOTLOADER_MCUBOOT:
        cg.add_define("USE_BOOTLOADER_MCUBOOT")
        # Build the app as an MCUboot image (signed, linked into the primary
        # slot) and pull in the MCUboot child image + merged.hex. This must be
        # set by the bootloader selection itself, not by the OTA component: a
        # usb_cdc_recovery fw1 config has no zephyr_mcumgr OTA (the two are
        # mutually exclusive), and without this the app would link as a plain
        # SoftDevice app at 0x27000, MCUboot would never build, and the
        # USB-CDC updater package could not be generated.
        zephyr_add_prj_conf("BOOTLOADER_MCUBOOT", True)
        if config[CONF_BOARD] == "xiao_ble":
            # slot0_partition / slot1_partition devicetree nodes for the MCUboot
            # sysbuild image. Under sysbuild, bootloader/mcuboot/boot/zephyr/
            # CMakeLists.txt reads erase-block-size / write-block-size (and the
            # auto sector count) from these labels at configure time; the xiao_ble
            # board DTS ships the Adafruit softdevice layout and defines no such
            # labels, so we add them here. The real placement is owned by the
            # Partition Manager static layout (pm_static.yml); these addresses
            # only match the PM mcuboot_primary/mcuboot_secondary slots so the
            # configure-time sector maths is correct.
            mcuboot_slots = ""
            if _mcuboot_usb_cdc_recovery_enabled(config):
                mcuboot_conf = "xiao_ble_mcuboot_usb_cdc_recovery.conf"
                mcuboot_overlay = "xiao_ble_mcuboot_usb_cdc_recovery.overlay"
                # Single-application-slot recovery bootloader: only slot0 is read
                # by MCUboot. mcuboot_primary fills 0x1000..0x79000 (0x78000).
                mcuboot_slots = """
                    &flash0 {
                        partitions {
                            slot0_partition: partition@1000 {
                                label = "image-0";
                                reg = <0x00001000 0x00078000>;
                            };
                        };
                    };
                """
                add_extra_script(
                    "post",
                    "xiao_ble_mcuboot_artifact.py",
                    Path(__file__).parent / "xiao_ble_mcuboot_artifact.py.script",
                )
                # MCUboot must fit the executable region in the stock
                # Adafruit bootloader flash map. 0xFD800..0xFDFFF is the
                # Adafruit bootloader config page and 0xFE000..0xFEFFF is
                # the MBR params page used during the bootloader swap; keep
                # both free so a failed update cannot corrupt swap state.
                #
                # Reserve the nRF MBR at 0x0 (0x1000 bytes). The board reaches
                # MCUboot (at 0xF4000) via this MBR + UICR.NRFFW[0]=0xF4000, so
                # the application slot must NOT start at 0x0: if it does, the
                # partition manager places mcuboot_primary over the MBR, MCUboot
                # reads the MBR as a bogus primary image, and the first app
                # upload overwrites the MBR -- bricking the boot path (no way
                # back to MCUboot without SWD). With the MBR reserved here,
                # mcuboot_primary auto-fills 0x1000..0x79000 (0x78000);
                # mcuboot_secondary is sized to match (MCUboot requires equal
                # primary/secondary slots), which shifts settings_storage to
                # 0xF1000..0xF4000. The mcuboot / config / mbr-params / settings
                # pages below are unchanged.
                zephyr_add_pm_static(
                    [
                        Section("mbr", 0x0, 0x1000, "flash_primary"),
                        Section("mcuboot_secondary", 0x79000, 0x78000, "flash_primary"),
                        Section("settings_storage", 0xF1000, 0x3000, "flash_primary"),
                        Section("mcuboot", 0xF4000, 0x9800, "flash_primary"),
                        Section(
                            "empty_adafruit_bl_config_page",
                            0xFD800,
                            0x800,
                            "flash_primary",
                        ),
                        Section(
                            "empty_mbr_params_page", 0xFE000, 0x1000, "flash_primary"
                        ),
                        Section(
                            "empty_adafruit_bl_settings_page",
                            0xFF000,
                            0x1000,
                            "flash_primary",
                        ),
                    ]
                )
            elif _mcuboot_two_slot_enabled(config):
                # Option B fw2: the relocated two-slot swap MCUboot that fw1 is
                # migrated to over USB (no SWD). This is the steady-state layout
                # every normal build of the running app uses. The factory nRF MBR
                # stays at 0x0; MCUboot is relocated to 0x1000 and reached via
                # UICR.NRFFW[0]=0x1000, which the migrator app commits at
                # migration time. mcuboot_primary auto-fills the gap
                # 0xD000..0x80000 between the relocated bootloader and the
                # equal-sized secondary slot. Producing the one-time fw1 -> fw2
                # installer that embeds this relocated bootloader is a separate
                # opt-in below (migrator_image). See BOOTLOADER_UPDATER_PLAN.md
                # (Option B) and migrator/migrator_layout.h (the shared source of
                # truth for these addresses).
                mcuboot_conf = "xiao_ble_mcuboot_migrator.conf"
                mcuboot_overlay = "xiao_ble_mcuboot_migrator.overlay"
                # Two-slot swap bootloader: MCUboot reads both slots. The
                # dynamic mcuboot_primary fills 0xD000..0x80000 (0x73000) and
                # mcuboot_secondary matches it at 0x80000 (0x73000).
                mcuboot_slots = """
                    &flash0 {
                        partitions {
                            slot0_partition: partition@d000 {
                                label = "image-0";
                                reg = <0x0000d000 0x00073000>;
                            };
                            slot1_partition: partition@80000 {
                                label = "image-1";
                                reg = <0x00080000 0x00073000>;
                            };
                        };
                    };
                """
                if config[CONF_MCUBOOT][CONF_MIGRATOR_IMAGE]:
                    # Producing the one-time fw1 -> fw2 installer image is a
                    # separate, opt-in concern from building the app in the fw2
                    # layout above. It compiles a standalone migrator Zephyr app
                    # (its own west build) and is only needed once, to flash the
                    # two-slot bootloader onto a board still running fw1; every
                    # normal build/flash/OTA of the running app needs only the
                    # fw2 layout. Gating it here keeps that orthogonal west build
                    # out of the normal build path. See migrator/README.md.
                    add_extra_script(
                        "post",
                        "xiao_ble_mcuboot_migrator.py",
                        Path(__file__).parent / "xiao_ble_mcuboot_migrator.py.script",
                    )
                    # The post-build script runs in a separate PlatformIO/SCons
                    # process with no access to the component source tree, so
                    # copy the whole standalone migrator Zephyr app into the
                    # build's project dir under migrator/. The post-build step
                    # compiles it from source (west build) to produce the
                    # unsigned base image, then injects this build's relocated
                    # fw2 bootloader blob and signs it -- so no prebuilt binary
                    # is committed in-tree.
                    migrator_dir = Path(__file__).parent / "migrator"
                    for src in sorted(migrator_dir.rglob("*")):
                        if not src.is_file():
                            continue
                        rel = src.relative_to(migrator_dir)
                        # README is docs only; nothing else is excluded.
                        if rel.parts[0] == "README.md":
                            continue
                        add_extra_build_file(f"migrator/{rel.as_posix()}", src)
                # The Nordic Partition Manager requires the static layout to
                # leave exactly one gap (for the dynamic mcuboot_primary/app at
                # 0xD000..0x80000). The reserved top region above settings holds
                # the factory MBR-params page (UICR.NRFFW[1]=0xFE000) and the
                # Adafruit bootloader pages, which Option B preserves -- it must
                # be declared statically too, or the PM sees two gaps and fails.
                zephyr_add_pm_static(
                    [
                        Section("mbr", 0x0, 0x1000, "flash_primary"),
                        Section("mcuboot", 0x1000, 0xC000, "flash_primary"),
                        Section("mcuboot_secondary", 0x80000, 0x73000, "flash_primary"),
                        Section("settings_storage", 0xF3000, 0xA800, "flash_primary"),
                        Section("mbr_params", 0xFD800, 0x2800, "flash_primary"),
                    ]
                )
            else:
                mcuboot_conf = "xiao_ble_mcuboot.conf"
                mcuboot_overlay = "xiao_ble_mcuboot.overlay"
                # Default two-slot swap layout (no custom pm_static); give
                # MCUboot slot labels so the configure-time sector maths works.
                mcuboot_slots = """
                    &flash0 {
                        partitions {
                            slot0_partition: partition@d000 {
                                label = "image-0";
                                reg = <0x0000d000 0x00073000>;
                            };
                            slot1_partition: partition@80000 {
                                label = "image-1";
                                reg = <0x00080000 0x00073000>;
                            };
                        };
                    };
                """
            # Deliver the MCUboot Kconfig fragment and DT overlay to the MCUboot
            # *sysbuild* image (NCS >= 2.9.2 ignores the old zephyr/child_image/
            # mechanism). Sysbuild reads these from ${APP_DIR}/sysbuild/, i.e.
            # zephyr/sysbuild/<image>.{conf,overlay}. The board overlay and the
            # slot0/slot1 partition nodes are both appended to the same MCUboot
            # image overlay.
            # The recovery (fw1) and two-slot (fw2) bootloaders are hash-only
            # (signature type "none") to fit the tight Adafruit/relocated flash
            # budget -- see the CONFIG_BOOT_SIGNATURE_TYPE_NONE lines in their
            # .conf. Under sysbuild the signature type is chosen at the sysbuild
            # level (the mcuboot image inherits it and the app is signed to
            # match), and the nRF52840 sysbuild default is ECDSA-P256, which
            # would override the image-level request and push MCUboot over its
            # budget. Force it to "none" here so both hold.
            if _mcuboot_usb_cdc_recovery_enabled(config) or _mcuboot_two_slot_enabled(
                config
            ):
                zephyr_add_sysbuild_conf("BOOT_SIGNATURE_TYPE_NONE", True)
            add_extra_build_file(
                "zephyr/sysbuild/mcuboot.conf",
                Path(__file__).parent / mcuboot_conf,
            )
            zephyr_add_overlay(
                (Path(__file__).parent / mcuboot_overlay).read_text(), image="mcuboot"
            )
            if mcuboot_slots:
                zephyr_add_overlay(mcuboot_slots, image="mcuboot")
                # The application image also needs the slot0/slot1 labels: NCS's
                # sysbuild image-signing step (image_signing.cmake) reads
                # slot0_partition (REQUIRED) to size the signed app image. Add
                # the same nodes to the app's own devicetree overlay.
                zephyr_add_overlay(mcuboot_slots, image="")
    elif "_sd" in config[KEY_BOOTLOADER]:
        bootloader = config[KEY_BOOTLOADER].split("_")
        sd_id = bootloader[2][2:]
        cg.add_define("USE_SOFTDEVICE_ID", int(sd_id))
        if (len(bootloader)) > 3:
            sd_version = bootloader[3][1:]
            cg.add_define("USE_SOFTDEVICE_VERSION", int(sd_version))

    zephyr_setup_preferences()
    zephyr_to_code(config)

    if dfu_config := config.get(CONF_DFU):
        CORE.add_job(_dfu_to_code, dfu_config)
    framework_ver: cv.Version = CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION]
    if CONF_DCDC in config:
        if framework_ver < cv.Version(2, 9, 2):
            zephyr_add_prj_conf("BOARD_ENABLE_DCDC", config[CONF_DCDC])
        else:
            zephyr_add_overlay(
                f"""
                    &reg1 {{
                        regulator-initial-mode = <{"NRF5X_REG_MODE_DCDC" if config[CONF_DCDC] else "NRF5X_REG_MODE_LDO"}>;
                    }};
                """
            )

    if reg0_config := config.get(CONF_REG0):
        value = VOLTAGE_LEVELS.index(reg0_config[CONF_VOLTAGE])
        cg.add_define("USE_NRF52_REG0_VOUT", value)
        if reg0_config[CONF_UICR_ERASE]:
            cg.add_define("USE_NRF52_UICR_ERASE")

    conf = config[CONF_FRAMEWORK]
    advanced = conf[CONF_ADVANCED]
    # Enable OTA rollback support
    if advanced[CONF_ENABLE_OTA_ROLLBACK]:
        cg.add_define("USE_OTA_ROLLBACK")
    zephyr_add_prj_conf("NEWLIB_LIBC", True)
    zephyr_add_prj_conf("NEWLIB_LIBC_FLOAT_PRINTF", True)
    zephyr_add_prj_conf("NEWLIB_LIBC_NANO", conf[CONF_LIBC_NANO])
    # c++ support
    if framework_ver < cv.Version(2, 9, 2):
        zephyr_add_prj_conf("CPLUSPLUS", True)
        zephyr_add_prj_conf("LIB_CPLUSPLUS", True)
    else:
        zephyr_add_prj_conf("CPP", True)
        zephyr_add_prj_conf("REQUIRES_FULL_LIBCPP", True)
    # watchdog
    zephyr_add_prj_conf("WATCHDOG", True)
    zephyr_add_prj_conf("WDT_DISABLE_AT_BOOT", False)
    # disable console
    zephyr_add_prj_conf("UART_CONSOLE", False)
    zephyr_add_prj_conf("CONSOLE", False, False)
    # Disable the hardware UARTE (uart0) in the APPLICATION overlay. Everything on
    # this board runs over USB-CDC (console, shell, mcumgr), so nothing uses uart0,
    # but the Zephyr UARTE shim still STARTRXes it at boot (~0.5-1 mA), and its RX
    # pad (P1.12 on xiao_ble) collides with the I2C SCL some nodes wire on D7. The
    # mcuboot child-image overlays already disable it; the running app did not.
    # Unconditional: the `status = "disabled"` overlay is identical on NCS <2.9.2
    # and >=2.9.2 (unlike the DCDC/NFC nodes below). ESPHome has no nRF52/Zephyr
    # hardware-UART backend, so this can never conflict with a `uart:` bus.
    zephyr_add_overlay(
        """
            &uart0 {
                status = "disabled";
            };
        """
    )
    # use NFC pins as GPIO
    if framework_ver < cv.Version(2, 9, 2):
        zephyr_add_prj_conf("NFCT_PINS_AS_GPIOS", True)
    else:
        zephyr_add_overlay(
            """
                &uicr {
                    nfct-pins-as-gpios;
                };
            """
        )
    zephyr_add_prj_conf("REBOOT", True)


@coroutine_with_priority(CoroPriority.DIAGNOSTICS)
async def _dfu_to_code(dfu_config):
    cg.add_define("USE_NRF52_DFU")
    var = cg.new_Pvariable(dfu_config[CONF_ID])
    if CONF_RESET_PIN in dfu_config:
        pin = await cg.gpio_pin_expression(dfu_config[CONF_RESET_PIN])
        cg.add(var.set_reset_pin(pin))
    zephyr_add_prj_conf("CDC_ACM_DTE_RATE_CALLBACK_SUPPORT", True)
    await cg.register_component(var, dfu_config)


def copy_files() -> None:
    """Copy files to the build directory."""

    # Library conversion to Zephyr modules is wired into the sdk-nrf
    # CMakeLists only; the PlatformIO toolchain's forked platform package
    # cannot compile external libraries at all, so the build would fail at
    # link time anyway. Fail fast with a clear message instead.
    if CORE.using_toolchain_platformio and CORE.platformio_libraries:
        raise EsphomeError(
            f"Libraries ({', '.join(sorted(CORE.platformio_libraries))}) are "
            "not supported on the nRF52 'platformio' toolchain; use toolchain "
            "'sdk-nrf' to build them as Zephyr modules."
        )

    if CORE.using_toolchain_platformio and (
        zephyr_data()[KEY_BOOTLOADER] == BOOTLOADER_MCUBOOT
        or zephyr_data()[KEY_BOARD] == "xiao_ble"
    ):
        write_file_if_changed(
            CORE.relative_build_path(f"boards/{zephyr_data()[KEY_BOARD]}.json"),
            FAKE_BOARD_MANIFEST,
        )

    zephyr_copy_files()


def get_download_types(storage_json: StorageJSON) -> list[dict[str, str]]:
    """Get the download types for the firmware."""
    types = []
    UF2_PATH = "zephyr/zephyr.uf2"
    DFU_PATH = "firmware.zip"
    HEX_PATH = "zephyr/zephyr.hex"  # SDK 2.6.1, only generated when OTA is disabled
    HEX_MERGED_PATH = "zephyr/merged.hex"  # SDK 2.9.2, always generated
    APP_IMAGE_PATH = "zephyr/app_update.bin"
    MCUBOOT_UPDATER_DFU_PATH = "zephyr/xiao_ble_mcuboot_updater_dfu.zip"
    MCUBOOT_MIGRATOR_PATH = "zephyr/xiao_ble_mcuboot_migrator.img"
    build_dir = Path(storage_json.firmware_bin_path).parent
    if (build_dir / APP_IMAGE_PATH).is_file():
        types = [
            {
                "title": "HEX package",
                "description": "For initial flashing via pyocd using SWD.",
                "file": (
                    HEX_MERGED_PATH
                    if (build_dir / HEX_MERGED_PATH).is_file()
                    else HEX_PATH
                ),
                "download": f"{storage_json.name}.hex",
            },
            {
                "title": "App update package",
                "description": "For flashing via mcumgr-web using BLE or smpclient using USB CDC.",
                "file": APP_IMAGE_PATH,
                "download": f"app-{storage_json.name}.img",
            },
        ]
        if (build_dir / MCUBOOT_UPDATER_DFU_PATH).is_file():
            types.append(
                {
                    "title": "MCUboot bootloader update package",
                    "description": "One-time MCUboot install through the stock "
                    "Adafruit bootloader via adafruit-nrfutil using USB CDC. "
                    "No SWD debugger needed.",
                    "file": MCUBOOT_UPDATER_DFU_PATH,
                    "download": f"mcuboot-updater-{storage_json.name}.zip",
                }
            )
        if (build_dir / MCUBOOT_MIGRATOR_PATH).is_file():
            types.append(
                {
                    "title": "MCUboot two-slot migrator (no SWD)",
                    "description": "One-time fw1 -> fw2 migration. Upload to the "
                    "single-slot usb_cdc_recovery bootloader via USB-CDC serial "
                    "recovery, then physically reset: it installs this two-slot "
                    "swap MCUboot in place. No SWD debugger needed.",
                    "file": MCUBOOT_MIGRATOR_PATH,
                    "download": f"mcuboot-migrator-{storage_json.name}.img",
                }
            )
    elif (build_dir / UF2_PATH).is_file():
        types = [
            {
                "title": "UF2 package (recommended)",
                "description": "For flashing via Adafruit nRF52 Bootloader as a flash drive.",
                "file": UF2_PATH,
                "download": f"{storage_json.name}.uf2",
            },
            {
                "title": "DFU package",
                "description": "For flashing via adafruit-nrfutil using USB CDC.",
                "file": DFU_PATH,
                "download": f"dfu-{storage_json.name}.zip",
            },
        ]
    else:
        types = [
            {
                "title": "HEX package",
                "description": "For flashing via pyocd using SWD.",
                "file": (
                    HEX_MERGED_PATH
                    if (build_dir / HEX_MERGED_PATH).is_file()
                    else HEX_PATH
                ),
                "download": f"{storage_json.name}.hex",
            },
        ]

    return types


def _upload_using_platformio(
    config: ConfigType, port: str, upload_args: list[str]
) -> int | str:
    from esphome.platformio import toolchain

    setup_platformio_python_env()
    if port is not None:
        upload_args += ["--upload-port", port]
    return toolchain.run_platformio_cli_run(config, CORE.verbose, *upload_args)


def upload_program(config: ConfigType, args, host: str) -> bool:
    from esphome.__main__ import check_permissions
    from esphome.upload_targets import PortType, get_port_type

    if KEY_ZEPHYR not in CORE.data:
        platform_config = config.get(CORE.target_platform)
        if not platform_config:
            raise EsphomeError(
                "nRF52 platform configuration is missing; "
                "please re-validate and recompile."
            )
        set_core_data(platform_config)
        set_framework(platform_config)

    mcumgr_device: str | None = None

    if get_port_type(host) == PortType.SERIAL:
        check_permissions(host)
        if zephyr_data()[KEY_BOOTLOADER] == BOOTLOADER_MCUBOOT:
            mcumgr_device = host
        else:
            if not CORE.using_toolchain_platformio:
                bootloader = zephyr_data()[KEY_BOOTLOADER]
                if bootloader not in (
                    BOOTLOADER_ADAFRUIT,
                    BOOTLOADER_ADAFRUIT_NRF52_SD132,
                    BOOTLOADER_ADAFRUIT_NRF52_SD140_V6,
                    BOOTLOADER_ADAFRUIT_NRF52_SD140_V7,
                ):
                    raise EsphomeError("Not implemented yet")
                check_and_install()
                paths = get_build_paths()
                env = get_build_env()
                build_dir = CORE.relative_pioenvs_path(CORE.name)
                dfu_package = build_dir / "firmware.zip"
                if not dfu_package.is_file():
                    raise EsphomeError("Firmware not found. Please compile first.")
                import time as _time

                import serial as _serial
                import serial.tools.list_ports as _list_ports

                try:
                    ser = _serial.Serial(host, baudrate=1200, timeout=1)
                    ser.close()
                except _serial.SerialException as err:
                    raise EsphomeError(f"Failed to open {host}: {err}") from err

                # Wait for device to reset (port disappears)
                deadline = _time.monotonic() + 5
                while _time.monotonic() < deadline:
                    _time.sleep(0.1)
                    if host not in {p.device for p in _list_ports.comports()}:
                        break
                else:
                    _LOGGER.warning(
                        "Device did not leave %s within 5 s; "
                        "it may not have entered bootloader mode",
                        host,
                    )

                # Wait for DFU port to reappear
                deadline = _time.monotonic() + 10
                while _time.monotonic() < deadline:
                    _time.sleep(0.1)
                    if host in {p.device for p in _list_ports.comports()}:
                        break
                else:
                    raise EsphomeError(
                        f"DFU port {host!r} did not reappear within 10 s. "
                        "Check that the device entered DFU mode."
                    )

                # Wait for udev to finish setting up device permissions
                deadline = _time.monotonic() + 5
                while _time.monotonic() < deadline:
                    try:
                        check_permissions(host)
                        break
                    except EsphomeError:
                        _time.sleep(0.05)
                else:
                    check_permissions(host)  # raises with helpful message

                python = str(paths["python_executable"])
                if not run_command_ok(
                    [
                        python,
                        "-m",
                        "nordicsemi.__main__",
                        "dfu",
                        "serial",
                        "-pkg",
                        str(dfu_package),
                        "-p",
                        host,
                        "-b",
                        "115200",
                        "--singlebank",
                    ],
                    env=env,
                    stream_output=True,
                ):
                    raise EsphomeError("nRF52 serial DFU upload failed")
            else:
                result = _upload_using_platformio(config, host, ["-t", "upload"])
                if result != 0:
                    raise EsphomeError(f"Upload failed with result: {result}")
            return True  # Handled: serial upload

    if host == "PYOCD":
        if not CORE.using_toolchain_platformio:
            check_and_install()
            paths = get_build_paths()
            env = get_build_env()
            build_dir = CORE.relative_pioenvs_path(CORE.name)
            west_cmd = [
                str(paths["python_executable"]),
                "-m",
                "west",
                "flash",
                "--runner",
                "pyocd",
                "-d",
                str(build_dir),
            ]
            if not run_command_ok(
                west_cmd,
                env=env,
                stream_output=True,
                cwd=str(paths["framework_path"]),
            ):
                raise EsphomeError("nRF52 pyocd flash failed")
        else:
            result = _upload_using_platformio(config, host, ["-t", "flash_pyocd"])
            if result != 0:
                raise EsphomeError(f"Upload failed with result: {result}")
        return True  # Handled: PYOCD upload

    # Deferred imports: bleak/smpclient are heavy, only load for BLE/mcumgr paths
    from .ble_logger import is_mac_address
    from .ota import smpmgr_scan, smpmgr_upload

    if host == "BLE":
        mcumgr_device = asyncio.run(smpmgr_scan(CORE.name))

    if is_mac_address(host):
        mcumgr_device = host

    if mcumgr_device:
        firmware = Path(
            CORE.relative_pioenvs_path(CORE.name, "zephyr", "app_update.bin")
        ).resolve()
        asyncio.run(smpmgr_upload(mcumgr_device, firmware))
        return True  # Handled: mcumgr OTA upload

    return False  # Not handled: let caller try default upload methods


def show_logs(config: ConfigType, args, devices: list[str]) -> bool:
    address = devices[0]
    from .ble_logger import is_mac_address, logger_connect, logger_scan

    if devices[0] == "BLE":
        ble_device = asyncio.run(logger_scan(CORE.name))
        if ble_device:
            # ble_device.address is a BLE handle (a MAC on Linux/BlueZ, a
            # CoreBluetooth UUID on macOS); connect to it directly rather than
            # gating on is_mac_address(), which is False for macOS UUIDs.
            asyncio.run(logger_connect(ble_device.address))
        return True

    if is_mac_address(address):
        asyncio.run(logger_connect(address))
        return True
    return False


def _addr2line(addr2line: str, elf: Path, addr: str) -> str:
    try:
        result = subprocess.run(
            [addr2line, "-e", elf, addr],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip().splitlines()[0]
    except Exception as err:  # noqa: BLE001  # pylint: disable=broad-except
        _LOGGER.error("Running command failed: %s", err)
    return ""


def process_stacktrace(config: ConfigType, line: str, backtrace_state: bool) -> bool:
    if "Last crash:" in line:
        return True
    if backtrace_state:
        match = re.search(r"PC=(0x[0-9a-fA-F]+)\s+LR=(0x[0-9a-fA-F]+)", line)
        if match:
            pc = match.group(1)
            lr = match.group(2)
            from esphome.analyze_memory.toolchain import find_tool

            addr2line = find_tool("addr2line")
            if addr2line is None:
                return False

            candidates = [
                CORE.relative_pioenvs_path(CORE.name, "zephyr", "zephyr", "zephyr.elf"),
                CORE.relative_pioenvs_path(CORE.name, "zephyr", "zephyr.elf"),
                CORE.relative_pioenvs_path(CORE.name, "firmware.elf"),
            ]

            elf = next((path for path in candidates if path.exists()), None)

            if elf is None:
                _LOGGER.warning(
                    "None of the expected ELF files exist:\n%s",
                    "\n".join(str(p) for p in candidates),
                )
                return False

            _LOGGER.error("=== CRASH ===")
            _LOGGER.error("PC: %s", _addr2line(addr2line, elf, pc))
            _LOGGER.error("LR: %s", _addr2line(addr2line, elf, lr))

    return False


def _generate_cmake_lists() -> tuple[bool, list[Path]]:
    """Write the project CMakeLists.txt.

    Returns ``(changed, module_dirs)`` where ``changed`` is True if the file
    changed on disk and ``module_dirs`` is the list of Zephyr module directories
    converted from ``cg.add_library()`` libraries. The caller wires ``module_dirs``
    into the build via the environment (see ``run_compile``) rather than a plain
    ``set(EXTRA_ZEPHYR_MODULES ...)`` here: Zephyr resolves the module list with
    ``zephyr_get()``, which returns the first *scope* that defines the variable and
    never merges across scopes, so a local ``set()`` in this CMakeLists is silently
    dropped whenever a higher-priority scope already supplies modules (an external
    ``ZEPHYR_EXTRA_MODULES`` in the environment, or -- under sysbuild -- the module
    list the sysbuild top level pushes into every image).
    """
    compile_flags = get_project_compile_flags()
    link_flags = get_project_link_flags()

    # Convert any PlatformIO libraries added via cg.add_library() into Zephyr
    # modules. Only framework-agnostic libraries actually compile under Zephyr.
    from esphome.components.zephyr.library import generate_zephyr_modules

    module_dirs = generate_zephyr_modules(list(CORE.platformio_libraries.values()))

    lines = [
        "cmake_minimum_required(VERSION 3.20.0)",
        "",
        'set(Zephyr_DIR "$ENV{ZEPHYR_BASE}/share/zephyr-package/cmake/")',
        "",
        "find_package(Zephyr REQUIRED)",
        "",
        f"project({CORE.name})",
        "",
        'file(GLOB_RECURSE APP_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_LIST_DIR}/../src/*.cpp" "${CMAKE_CURRENT_LIST_DIR}/../src/*.c")',
        "",
        "target_sources(app PRIVATE ${APP_SOURCES})",
        'target_include_directories(app PRIVATE "${CMAKE_CURRENT_LIST_DIR}/../src")',
    ]

    if compile_flags:
        lines += [
            "",
            "target_compile_options(app PRIVATE",
            *[f'  "{flag}"' for flag in compile_flags],
            ")",
        ]

    if link_flags:
        lines += [
            "",
            "zephyr_ld_options(",
            *[f'  "{flag}"' for flag in link_flags],
            ")",
        ]

    changed = write_file_if_changed(
        CORE.relative_build_path("zephyr", "CMakeLists.txt"),
        "\n".join(lines) + "\n",
    )
    return changed, module_dirs


def _merge_zephyr_extra_modules(env: dict, module_dirs: list[Path]) -> None:
    """Publish converted-library module dirs to the Zephyr build environment.

    Zephyr's ``zephyr_get()`` resolves the module list (``EXTRA_ZEPHYR_MODULES`` /
    ``ZEPHYR_EXTRA_MODULES``) by returning the first scope that defines it without
    merging across scopes. A local ``set()`` in the app CMakeLists therefore loses
    to any value provided at a higher-priority scope -- an external
    ``ZEPHYR_EXTRA_MODULES`` in the environment (e.g. a link-only prebuilt-archive
    module), or, under sysbuild, the module list the sysbuild top level reads once
    and pushes into every image. Merge our module dirs into those same environment
    variables (preserving anything already there) so every converted library
    survives in both sysbuild and non-sysbuild builds. Setting both aliases to the
    identical merged value keeps whichever name ``zephyr_get()`` happens to pick
    complete.
    """
    if not module_dirs:
        return
    merged: list[str] = []
    for name in ("EXTRA_ZEPHYR_MODULES", "ZEPHYR_EXTRA_MODULES"):
        for path in env.get(name, "").split(";"):
            if path and path not in merged:
                merged.append(path)
    for module_dir in module_dirs:
        path = str(module_dir).replace("\\", "/")
        if path and path not in merged:
            merged.append(path)
    value = ";".join(merged)
    env["EXTRA_ZEPHYR_MODULES"] = value
    env["ZEPHYR_EXTRA_MODULES"] = value


def _copy_if_exists(src: Path, dst: Path) -> None:
    if src.is_file():
        shutil.copy2(src, dst)


def run_compile(args, config: ConfigType) -> bool:
    if CORE.using_toolchain_platformio:
        # The actual build is done by PlatformIO (the caller falls through to
        # it when this returns False); prepare the Python environment its
        # Zephyr build script expects first.
        setup_platformio_python_env()
        return False
    if not CORE.using_toolchain_sdk_nrf:
        raise EsphomeError(
            "Unsupported toolchain for nRF52. "
            "Supported toolchains are 'platformio' and 'sdk-nrf'."
        )
    check_and_install()

    paths = get_build_paths()
    env = get_build_env()

    cmake_lists_changed, module_dirs = _generate_cmake_lists()
    _merge_zephyr_extra_modules(env, module_dirs)

    board = zephyr_data()[KEY_BOARD]
    build_dir = CORE.relative_pioenvs_path(CORE.name)
    source_dir = CORE.relative_build_path("zephyr")

    # A missing CMake cache (dropped by zephyr's copy_files() on config
    # change) or a changed CMakeLists.txt requires a pristine build: Zephyr
    # caches Kconfig/devicetree state that survives a plain cmake re-run.
    # West can't do the wipe — its pristine modes only recognize a build dir
    # by reading ZEPHYR_BASE from the very cache that was dropped.
    if (
        cmake_lists_changed or not (build_dir / "CMakeCache.txt").is_file()
    ) and build_dir.is_dir():
        _LOGGER.info("Build inputs changed, cleaning %s", build_dir)
        rmtree(build_dir)

    west_cmd = [
        str(paths["python_executable"]),
        "-m",
        "west",
        "build",
        "--pristine=auto",
        "-b",
        board,
        "-d",
        str(build_dir),
        str(source_dir),
    ]

    if not run_command_ok(
        west_cmd,
        env=env,
        stream_output=True,
        cwd=str(paths["framework_path"]),
    ):
        raise EsphomeError("nRF52 native build failed")

    zephyr_dir = build_dir / "zephyr"
    framework_ver = CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION]
    # SDK < 2.9.2 places artifacts directly in build_dir/zephyr/.
    # SDK >= 2.9.2 nests them one level deeper (build_dir/zephyr/zephyr/);
    # copy files to match get_download_types layout.
    if framework_ver < cv.Version(2, 9, 2):
        west_out = zephyr_dir
    else:
        west_out = zephyr_dir / "zephyr"
        _copy_if_exists(west_out / "zephyr.uf2", zephyr_dir / "zephyr.uf2")
        _copy_if_exists(west_out / "zephyr.signed.bin", zephyr_dir / "app_update.bin")
        _copy_if_exists(build_dir / "merged.hex", zephyr_dir / "merged.hex")

    # (dev_type, sd_req) per bootloader — values from Nordic SoftDevice release notes
    _GENPKG_PARAMS = {
        BOOTLOADER_ADAFRUIT_NRF52_SD132: ("0x0051", "0x009D"),
        BOOTLOADER_ADAFRUIT_NRF52_SD140_V6: ("0x0052", "0x00B6"),
        BOOTLOADER_ADAFRUIT_NRF52_SD140_V7: ("0x0052", "0x00CA"),
    }
    bootloader = zephyr_data()[KEY_BOOTLOADER]
    if bootloader in (
        BOOTLOADER_ADAFRUIT,
        BOOTLOADER_ADAFRUIT_NRF52_SD132,
        BOOTLOADER_ADAFRUIT_NRF52_SD140_V6,
        BOOTLOADER_ADAFRUIT_NRF52_SD140_V7,
    ):
        hex_file = west_out / "zephyr.hex"
        dfu_package = build_dir / "firmware.zip"
        genpkg_cmd = [
            str(paths["python_executable"]),
            "-m",
            "nordicsemi.__main__",
            "dfu",
            "genpkg",
        ]
        if bootloader in _GENPKG_PARAMS:
            dev_type, sd_req = _GENPKG_PARAMS[bootloader]
            genpkg_cmd += ["--dev-type", dev_type, "--sd-req", sd_req]
        genpkg_cmd += ["--application", str(hex_file), str(dfu_package)]
        if not run_command_ok(genpkg_cmd, env=env, stream_output=True):
            raise EsphomeError("Failed to create adafruit DFU package")

    return True
