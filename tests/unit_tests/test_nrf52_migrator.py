"""Tests for the nRF52 fw1 -> fw2 (Option B) migrator packaging script."""

from __future__ import annotations

import asyncio
from pathlib import Path
import runpy
import struct
import zlib

import pytest

from esphome.components import nrf52
from esphome.components.zephyr import zephyr_data
from esphome.components.zephyr.const import (
    KEY_EXTRA_BUILD_FILES,
    KEY_OVERLAY,
    KEY_PM_STATIC,
    KEY_SYSBUILD_CONF,
)
import esphome.config_validation as cv
from esphome.const import KEY_CORE
from esphome.core import CORE

NRF52_DIR = Path(__file__).parents[2] / "esphome" / "components" / "nrf52"


def _load_migrator_script() -> dict:
    return runpy.run_path(str(NRF52_DIR / "xiao_ble_mcuboot_migrator.py.script"))


def _setup_core(path: Path) -> None:
    CORE.config_path = path / "test.yaml"
    CORE.name = "test"
    CORE.build_path = path / ".esphome" / "build" / "test"
    CORE.data[KEY_CORE] = {}


def _fake_base(script: dict, *, prefix: int = 64, suffix: int = 32) -> bytes:
    """A stand-in for the prebuilt migrator base: magic + zeroed header + 0xFF."""
    header_tail = b"\x00" * 16  # blob_len/crc/target/format placeholders
    return (
        b"\xaa" * prefix
        + script["BLOB_MAGIC"]
        + header_tail
        + b"\xff" * script["BLOB_CAPACITY"]
        + b"\xbb" * suffix
    )


# --- layout / drift lock --------------------------------------------------


def test_layout_constants_match_option_b() -> None:
    """Lock the Option B addresses the migrator and pm_static agree on."""
    script = _load_migrator_script()
    assert script["FW2_BOOTLOADER_ADDR"] == 0x1000
    assert script["FW2_BOOTLOADER_SIZE"] == 0xC000
    assert script["FW2_PRIMARY_ADDR"] == 0xD000
    assert script["FW2_PRIMARY_SIZE"] == 0x73000
    assert script["FW2_SECONDARY_ADDR"] == 0x80000
    assert script["FW2_SECONDARY_SIZE"] == 0x73000
    assert script["BLOB_CAPACITY"] == 0xC000
    assert script["BLOB_MAGIC"] == b"ESPHOMENRF52BLOB"
    # the migrator pm_static must place mcuboot/secondary where the script
    # expects to extract / validate them
    assert script["EXPECTED_PARTITIONS"]["mcuboot"] == (0x1000, 0xD000)
    assert script["EXPECTED_PARTITIONS"]["mcuboot_primary"] == (0xD000, 0x80000)
    assert script["EXPECTED_PARTITIONS"]["mcuboot_secondary"] == (0x80000, 0xF3000)


# --- extract_fw2_bootloader ----------------------------------------------


def test_extract_fw2_bootloader_trims_trailing_erased() -> None:
    script = _load_migrator_script()
    data = {0x1000: 0x11, 0x1001: 0x22, 0x1002: 0xFF, 0x1003: 0xFF}
    assert script["extract_fw2_bootloader"](data) == b"\x11\x22"


def test_extract_fw2_bootloader_gap_fills_interior() -> None:
    script = _load_migrator_script()
    data = {0x1000: 0x11, 0x1005: 0x22}
    assert script["extract_fw2_bootloader"](data) == b"\x11\xff\xff\xff\xff\x22"


def test_extract_fw2_bootloader_requires_region_start() -> None:
    script = _load_migrator_script()
    with pytest.raises(script["MigratorError"], match="does not start at 0x00001000"):
        script["extract_fw2_bootloader"]({0x2000: 0x11})


def test_extract_fw2_bootloader_ignores_following_primary_slot() -> None:
    """The app in the primary slot at 0xD000 must not be pulled into the blob."""
    script = _load_migrator_script()
    data = {0x1000: 0x11, 0x1001: 0x22}
    data.update({0xD000 + i: 0xAB for i in range(64)})  # primary slot content
    assert script["extract_fw2_bootloader"](data) == b"\x11\x22"


# --- inject_blob / locate_blob -------------------------------------------


def test_inject_blob_patches_header_and_payload() -> None:
    script = _load_migrator_script()
    base = _fake_base(script)
    fw2 = bytes(range(256)) * 4  # 1024 bytes
    out = script["inject_blob"](base, fw2)

    assert len(out) == len(base)
    assert out[:64] == b"\xaa" * 64  # prefix untouched
    assert out[-32:] == b"\xbb" * 32  # suffix untouched

    # After injection the data region is no longer blank, so locate_blob (which
    # finds the placeholder by its blank capacity run) would no longer match it;
    # the placeholder magic itself is untouched, so find it directly.
    offset = out.find(script["BLOB_MAGIC"])
    blob_len, crc, target = struct.unpack_from("<III", out, offset + 16)
    assert blob_len == len(fw2)
    assert crc == zlib.crc32(fw2) & 0xFFFFFFFF
    assert target == 0x1000
    data_start = offset + 32
    assert out[data_start : data_start + len(fw2)] == fw2
    # remainder of the capacity is 0xFF padded
    assert out[data_start + len(fw2) : data_start + script["BLOB_CAPACITY"]] == (
        b"\xff" * (script["BLOB_CAPACITY"] - len(fw2))
    )


def test_inject_blob_rejects_empty_payload() -> None:
    script = _load_migrator_script()
    with pytest.raises(script["MigratorError"], match="empty"):
        script["inject_blob"](_fake_base(script), b"")


def test_inject_blob_rejects_oversized_payload() -> None:
    script = _load_migrator_script()
    oversized = b"\x01" * (script["BLOB_CAPACITY"] + 1)
    with pytest.raises(script["MigratorError"], match="exceeds blob capacity"):
        script["inject_blob"](_fake_base(script), oversized)


def test_locate_blob_requires_magic() -> None:
    script = _load_migrator_script()
    with pytest.raises(script["MigratorError"], match="no mig_blob placeholder"):
        script["locate_blob"](b"\x00" * 256)


def test_locate_blob_rejects_ambiguous_placeholders() -> None:
    # The magic legitimately appears twice in the real base (placeholder + the
    # main.c .rodata copy); locate_blob disambiguates by the placeholder's blank
    # capacity run. Two *blank* placeholders are genuinely ambiguous and rejected.
    script = _load_migrator_script()
    one = script["BLOB_MAGIC"] + b"\x00" * 16 + b"\xff" * script["BLOB_CAPACITY"]
    with pytest.raises(script["MigratorError"], match="ambiguous"):
        script["locate_blob"](one + one)


def test_locate_blob_ignores_nonblank_magic() -> None:
    # A second magic whose capacity region is NOT blank (e.g. the .rodata copy)
    # is not a placeholder candidate, so the single blank placeholder is found.
    script = _load_migrator_script()
    placeholder = (
        script["BLOB_MAGIC"] + b"\x00" * 16 + b"\xff" * script["BLOB_CAPACITY"]
    )
    rodata_copy = script["BLOB_MAGIC"] + b"\x5a" * 16  # not followed by blank
    base = placeholder + rodata_copy
    assert script["locate_blob"](base) == 0


# --- build_migrator_image -------------------------------------------------


def test_build_migrator_image_injects_and_packages(tmp_path: Path) -> None:
    script = _load_migrator_script()
    # The base is built from source in the PlatformIO env (west build); here we
    # pass a stand-in base directly so inject + package are exercised without NCS.
    base_bytes = _fake_base(script)
    merged = tmp_path / "merged.hex"
    fw2 = bytes((i * 7) & 0xFF for i in range(2048))
    script["write_intel_hex"](merged, {0x1000 + i: b for i, b in enumerate(fw2)})

    output = tmp_path / "migrator.img"

    # Patch the signing stub (no imgtool in CI): copy the patched bytes through.
    # runpy returns a copy of the namespace, so patch the function's __globals__.
    namespace = script["build_migrator_image"].__globals__
    namespace["_imgtool_sign"] = lambda unsigned, out: out.write_bytes(
        unsigned.read_bytes()
    )

    size = script["build_migrator_image"](merged, output, base_bytes)
    assert size == output.stat().st_size
    assert size <= script["FW1_APP_SLOT_SIZE"]

    produced = output.read_bytes()
    # Injected output is no longer blank at the placeholder; find the magic.
    offset = produced.find(script["BLOB_MAGIC"])
    blob_len, crc, target = struct.unpack_from("<III", produced, offset + 16)
    assert blob_len == len(fw2)  # exact: no trailing 0xFF in this payload
    assert crc == zlib.crc32(fw2) & 0xFFFFFFFF
    assert target == 0x1000


# --- _locate_base_bin (sysbuild output layout) ----------------------------


def test_locate_base_bin_resolves_sysbuild_domain(tmp_path: Path) -> None:
    """NCS 2.9 sysbuild puts the base in <build>/<domain>/zephyr/zephyr.bin."""
    script = _load_migrator_script()
    domain_bin = tmp_path / "migrator" / "zephyr" / "zephyr.bin"
    domain_bin.parent.mkdir(parents=True)
    domain_bin.write_bytes(b"\x01\x02")
    (tmp_path / "domains.yaml").write_text(
        "default: migrator\n"
        "domains:\n"
        "  - name: migrator\n"
        f"    build_dir: {tmp_path / 'migrator'}\n",
        encoding="utf-8",
    )
    assert script["_locate_base_bin"](tmp_path) == domain_bin


def test_locate_base_bin_falls_back_to_legacy_layout(tmp_path: Path) -> None:
    """Pre-2.9 / non-sysbuild builds keep the base at <build>/zephyr/zephyr.bin."""
    script = _load_migrator_script()
    legacy_bin = tmp_path / "zephyr" / "zephyr.bin"
    legacy_bin.parent.mkdir(parents=True)
    legacy_bin.write_bytes(b"\x03\x04")
    assert script["_locate_base_bin"](tmp_path) == legacy_bin


def test_locate_base_bin_raises_when_absent(tmp_path: Path) -> None:
    script = _load_migrator_script()
    with pytest.raises(script["MigratorError"], match="zephyr.bin not produced"):
        script["_locate_base_bin"](tmp_path)


# --- _validate_partitions -------------------------------------------------


def _partitions_yaml(script: dict) -> str:
    mcuboot = script["EXPECTED_PARTITIONS"]["mcuboot"]
    primary = script["EXPECTED_PARTITIONS"]["mcuboot_primary"]
    secondary = script["EXPECTED_PARTITIONS"]["mcuboot_secondary"]
    return (
        f"mcuboot:\n  address: {mcuboot[0]}\n  end_address: {mcuboot[1]}\n"
        f"mcuboot_primary:\n  address: {primary[0]}\n  end_address: {primary[1]}\n"
        f"mcuboot_secondary:\n  address: {secondary[0]}\n  end_address: {secondary[1]}\n"
    )


def test_validate_partitions_accepts_option_b_layout(tmp_path: Path) -> None:
    script = _load_migrator_script()
    path = tmp_path / "partitions.yml"
    path.write_text(_partitions_yaml(script), encoding="utf-8")
    script["_validate_partitions"](path)  # no raise


def test_validate_partitions_rejects_wrong_bootloader_address(tmp_path: Path) -> None:
    script = _load_migrator_script()
    path = tmp_path / "partitions.yml"
    text = _partitions_yaml(script).replace(
        "mcuboot:\n  address: 4096", "mcuboot:\n  address: 0"
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(script["MigratorError"], match="Option B relocated layout"):
        script["_validate_partitions"](path)


# --- config wiring --------------------------------------------------------


def test_two_slot_registers_sysbuild_images_and_pm_static(setup_core: Path) -> None:
    _setup_core(setup_core)
    config = nrf52.CONFIG_SCHEMA(
        {
            "board": "xiao_ble",
            "bootloader": "mcuboot",
            "mcuboot": {"two_slot": True},
        }
    )

    asyncio.run(nrf52.to_code(config))
    CORE.flush_tasks()

    extra_build_files = zephyr_data()[KEY_EXTRA_BUILD_FILES]
    # NCS >= 2.9.2 sysbuild: the MCUboot Kconfig fragment goes to the mcuboot
    # sysbuild image (zephyr/sysbuild/mcuboot.conf), not zephyr/child_image/.
    assert (
        extra_build_files["zephyr/sysbuild/mcuboot.conf"].name
        == "xiao_ble_mcuboot_migrator.conf"
    )
    assert "zephyr/child_image/mcuboot.conf" not in extra_build_files
    assert "zephyr/child_image/mcuboot/boards/xiao_ble.overlay" not in extra_build_files
    # The mcuboot DT overlay carries the migrator board overlay plus the
    # slot0/slot1 partition labels the sysbuild image needs; the app image gets
    # them too. Hash-only signing is forced at the sysbuild level.
    mcuboot_overlay = zephyr_data()[KEY_OVERLAY]["mcuboot"]
    assert "slot0_partition: partition@d000" in mcuboot_overlay
    assert "slot1_partition: partition@80000" in mcuboot_overlay
    assert "slot0_partition: partition@d000" in zephyr_data()[KEY_OVERLAY][""]
    assert (
        zephyr_data()[KEY_SYSBUILD_CONF]["SB_CONFIG_BOOT_SIGNATURE_TYPE_NONE"] is True
    )
    # The fw2 layout alone does NOT build the one-time migrator installer: the
    # standalone migrator app is not copied into the build and the post-build
    # packaging script is not registered (that is gated behind migrator_image).
    assert "migrator/CMakeLists.txt" not in extra_build_files
    assert "migrator/prj.conf" not in extra_build_files
    assert "post:xiao_ble_mcuboot_migrator.py" not in CORE.platformio_options.get(
        "extra_scripts", []
    )
    assert [
        (section.name, section.address, section.size)
        for section in zephyr_data()[KEY_PM_STATIC]
    ] == [
        ("mbr", 0x0, 0x1000),
        ("mcuboot", 0x1000, 0xC000),
        ("mcuboot_secondary", 0x80000, 0x73000),
        ("settings_storage", 0xF3000, 0xA800),
        ("mbr_params", 0xFD800, 0x2800),
    ]


def test_migrator_image_registers_installer_packaging(setup_core: Path) -> None:
    _setup_core(setup_core)
    config = nrf52.CONFIG_SCHEMA(
        {
            "board": "xiao_ble",
            "bootloader": "mcuboot",
            "mcuboot": {"two_slot": True, "migrator_image": True},
        }
    )

    asyncio.run(nrf52.to_code(config))
    CORE.flush_tasks()

    extra_build_files = zephyr_data()[KEY_EXTRA_BUILD_FILES]
    # The standalone migrator Zephyr app is copied into the build so the
    # post-build step can compile the base from source (no committed binary).
    assert "migrator/CMakeLists.txt" in extra_build_files
    assert "migrator/prj.conf" in extra_build_files
    assert "migrator/src/main.c" in extra_build_files
    assert "migrator/migrator_layout.h" in extra_build_files
    # The README is docs only and must not be shipped into the build.
    assert "migrator/README.md" not in extra_build_files
    assert (
        "post:xiao_ble_mcuboot_migrator.py" in CORE.platformio_options["extra_scripts"]
    )


def test_migrator_image_requires_two_slot(setup_core: Path) -> None:
    _setup_core(setup_core)
    with pytest.raises(cv.Invalid, match="migrator_image requires mcuboot.two_slot"):
        nrf52.CONFIG_SCHEMA(
            {
                "board": "xiao_ble",
                "bootloader": "mcuboot",
                "mcuboot": {"migrator_image": True},
            }
        )


def test_two_slot_rejects_usb_cdc_recovery_combo(setup_core: Path) -> None:
    _setup_core(setup_core)
    with pytest.raises(cv.Invalid, match="mutually"):
        nrf52.CONFIG_SCHEMA(
            {
                "board": "xiao_ble",
                "bootloader": "mcuboot",
                "mcuboot": {"two_slot": True, "usb_cdc_recovery": True},
            }
        )


def test_two_slot_rejects_unsupported_board(setup_core: Path) -> None:
    _setup_core(setup_core)
    with pytest.raises(cv.Invalid, match="only supported on xiao_ble"):
        nrf52.CONFIG_SCHEMA(
            {
                "board": "adafruit_feather_nrf52840",
                "bootloader": "mcuboot",
                "mcuboot": {"two_slot": True},
            }
        )
