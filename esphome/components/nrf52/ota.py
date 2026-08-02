import asyncio
from dataclasses import asdict
import json
import logging
from pathlib import Path

from bleak import BleakScanner
from bleak.exc import BleakDBusError, BleakDeviceNotFoundError
from smp.error import MGMT_ERR
from smp.exceptions import SMPBadStartDelimiter
from smp.image_management import IMG_MGMT_ERR
from smpclient import SMPClient
from smpclient.generics import error, success
from smpclient.mcuboot import IMAGE_TLV, ImageInfo, MCUBootImageError, TLVNotFound
from smpclient.requests.image_management import (
    ImageErase,
    ImageStatesRead,
    ImageStatesWrite,
)
from smpclient.requests.os_management import ResetWrite
from smpclient.transport import SMPTransportDisconnected
from smpclient.transport.ble import (
    SMPBLETransport,
    SMPBLETransportDeviceNotFound,
    SMPBLETransportException,
)
from smpclient.transport.serial import SMPSerialTransport

from esphome.core import EsphomeError
from esphome.espota2 import ProgressBar
from esphome.upload_targets import PortType, get_port_type

SMP_SERVICE_UUID = "8D53DC1D-1DB7-4CD3-868B-8A527460AA84"
BLE_SCAN_TIMEOUT = 10.0  # seconds
RESET_DELAY = 2.0  # seconds to wait before reset, allows on_end action to execute
ERASE_TIMEOUT = 30.0  # seconds; flash-erasing the secondary slot can take a while

_LOGGER = logging.getLogger(__name__)


def _json_state(o: object) -> object:
    """JSON serializer for SMP image state objects."""
    if isinstance(o, (bytes, bytearray)):
        return o.hex()
    if hasattr(o, "hex"):
        return o.hex()
    if hasattr(o, "__dict__"):
        return vars(o)
    return str(o)


def _is_no_free_slot(response: object) -> bool:
    """True if an SMP image-management error response is NO_FREE_SLOT."""
    # SMP v2 image-management errors carry an `err` whose `rc` is an
    # IMG_MGMT_ERR; for other groups the enum differs so the comparison is False.
    err = getattr(response, "err", None)
    return err is not None and getattr(err, "rc", None) == IMG_MGMT_ERR.NO_FREE_SLOT


async def smpmgr_scan(name: str) -> str:
    _LOGGER.info("Scanning bluetooth for %s...", name)
    # Do NOT pass service_uuids= to the scanner. The SMP/OTA service UUID is
    # carried in the BLE *scan response*, not the primary advertisement: a
    # 128-bit UUID (18 bytes) plus the complete device name does not fit in the
    # 31-byte primary advertising payload (see zephyr_ble_server's AD vs SD).
    # macOS/CoreBluetooth's service-UUID scan filter only matches UUIDs in the
    # primary advertisement, so filtering on it hides the device entirely even
    # though it is plainly discoverable by name. Match by name instead (the same
    # way ble_logger does); the SMP GATT service is verified on connect.
    smp_uuid = SMP_SERVICE_UUID.lower()
    fallback: str | None = None
    devices = await BleakScanner.discover(timeout=BLE_SCAN_TIMEOUT, return_adv=True)
    for device, adv in devices.values():
        # Match the live advertised name (local_name) as well as device.name.
        # On macOS, device.name is the cached GAP "Device Name" from a previous
        # connection, which goes stale after the firmware's name changes; the
        # current name is in the advertisement's local_name. `name` is the base
        # esphome name; with name_add_mac_suffix the device advertises
        # "<name>-<mac>", so accept that prefix too (an exact match would miss it).
        adv_name = adv.local_name or device.name
        if not (adv_name and (adv_name == name or adv_name.startswith(f"{name}-"))):
            continue
        # Prefer a device that actually advertises the OTA service (reliable on
        # backends like BlueZ that report scan-response UUIDs), but fall back to
        # the name match when the UUID is not visible (e.g. macOS).
        if smp_uuid in (uuid.lower() for uuid in adv.service_uuids):
            return device.address
        fallback = device.address
    if fallback is not None:
        return fallback
    raise EsphomeError(f"BLE device {name} with OTA service not found")


async def smpmgr_upload(device: str, firmware: Path) -> None:
    try:
        await _smpmgr_upload(device, firmware)
    except SMPTransportDisconnected as exc:
        raise EsphomeError(f"{device} was disconnected.") from exc
    except SMPBLETransportDeviceNotFound as exc:
        raise EsphomeError(f"{device} was not found.") from exc


def _get_image_tlv_sha256(file: Path) -> bytes:
    _LOGGER.info("Checking image: %s", str(file))
    try:
        image_info = ImageInfo.load_file(str(file))
        _LOGGER.info(
            "Image header:\n%s", json.dumps(asdict(image_info.header), indent=2)
        )
        _LOGGER.debug(str(image_info))
    except MCUBootImageError as exc:
        raise EsphomeError("Inspection of FW image failed") from exc
    except FileNotFoundError as exc:
        raise EsphomeError(
            f"Firmware image file not found: {file}. Build with zephyr_mcumgr enabled"
        ) from exc

    try:
        image_tlv_sha256 = image_info.get_tlv(IMAGE_TLV.SHA256)
        _LOGGER.info("Image tlv sha256: %s", image_tlv_sha256)
    except TLVNotFound as exc:
        raise EsphomeError("Could not find IMAGE_TLV_SHA256 in image.") from exc
    return image_tlv_sha256.value


async def _smpmgr_upload(device: str, firmware: Path) -> None:
    image_tlv_sha256 = _get_image_tlv_sha256(firmware)

    # Serial ports are filesystem paths (/dev/..., COMx); anything else is a
    # BLE peripheral. Don't gate on is_mac_address() here: macOS/CoreBluetooth
    # identifies BLE devices by UUID (not MAC), so a scanned BLE address would
    # otherwise be misrouted to the serial transport and time out.
    if get_port_type(device) == PortType.SERIAL:
        smp_client = SMPClient(SMPSerialTransport(), device)
    else:
        smp_client = SMPClient(SMPBLETransport(), device)

    _LOGGER.info("Connecting %s...", device)
    try:
        await smp_client.connect()
    except BleakDeviceNotFoundError as exc:
        raise EsphomeError(f"Device {device} not found") from exc
    except BleakDBusError as exc:
        if "NotPermitted" in exc.dbus_error:
            raise EsphomeError(
                f"Cannot connect to {device}: Make sure the device is paired."
            ) from exc
        raise EsphomeError(f"BLE error connecting to {device}: {exc}") from exc
    except SMPBLETransportException as exc:
        raise EsphomeError(f"Connection error with {device}") from exc

    _LOGGER.info("Connected %s...", device)
    try:
        await _smpmgr_upload_connected(smp_client, device, firmware, image_tlv_sha256)
    finally:
        await smp_client.disconnect()


async def _smpmgr_upload_connected(
    smp_client: SMPClient, device: str, firmware: Path, image_tlv_sha256: bytes
) -> None:
    try:
        image_state = await smp_client.request(ImageStatesRead(), 2.5)
    except (SMPBadStartDelimiter, TimeoutError) as exc:
        raise EsphomeError(
            f"mcumgr is not responding on {device}. If this board still runs "
            "the stock Adafruit bootloader, install MCUboot first. If MCUboot "
            "is already installed, reset the board into USB CDC recovery mode "
            "and retry after the serial port appears."
        ) from exc

    already_uploaded = False
    stale_slot: int | None = None

    if error(image_state):
        raise EsphomeError(f"Failed to read image state from {device}: {image_state}")
    if success(image_state):
        if len(image_state.images) == 0:
            _LOGGER.warning("No images on device!")
        for image in image_state.images:
            _LOGGER.info(
                "Image state:\n%s",
                json.dumps(image, indent=2, default=_json_state),
            )
            if image.active and not image.confirmed:
                raise EsphomeError("No free slot. Testing mode but not confirmed yet.")
            if image.hash == image_tlv_sha256:
                if already_uploaded:
                    raise EsphomeError("Both slots have the same image already")
                if image.confirmed:
                    raise EsphomeError("The same image already confirmed")
                _LOGGER.warning("The same image already uploaded")
                already_uploaded = True
            elif image.pending and not image.active:
                # A leftover image from a previous OTA is sitting in the
                # secondary slot marked "pending" (queued for the next swap but
                # never booted/confirmed). MCUboot reserves that slot for the
                # pending swap and refuses a new upload with NO_FREE_SLOT, so it
                # must be erased before a different image can be written.
                stale_slot = image.slot

    if stale_slot is not None and not already_uploaded:
        _LOGGER.info(
            "Erasing stale pending image in slot %d to free the secondary slot",
            stale_slot,
        )
        r = await smp_client.request(ImageErase(slot=stale_slot), ERASE_TIMEOUT)
        if error(r) and _is_no_free_slot(r):
            # The running firmware was built without
            # CONFIG_MCUMGR_GRP_IMG_ALLOW_ERASE_PENDING, so mcumgr refuses to
            # erase the pending slot and would also refuse the upload below with
            # the same NO_FREE_SLOT error. There is no remote way out of this:
            # the pending swap must be cleared via serial recovery.
            raise EsphomeError(
                f"Slot {stale_slot} on {device} holds a pending image from a "
                "previous OTA that this firmware will not let mcumgr erase, so a "
                "new image cannot be uploaded over BLE. Recover by resetting the "
                "board into USB CDC MCUboot serial recovery and re-uploading over "
                "serial; the resulting firmware allows erasing a pending slot, so "
                "later BLE OTAs will work."
            )
        if error(r):
            # Other errors (e.g. single-slot MCUboot reporting ENOTSUP) are not
            # fatal -- let the upload below surface a genuine error if the slot
            # really is not free.
            _LOGGER.warning("Image erase of slot %d reported: %s", stale_slot, r)

    if not already_uploaded:
        with firmware.open("rb") as file:
            image = file.read()
            upload_size = len(image)
            progress = ProgressBar("Uploading")
            progress.update(0)
            try:
                async for offset in smp_client.upload(image):
                    progress.update(offset / upload_size)
            finally:
                progress.done()

    _LOGGER.info("Mark image for testing")
    r = await smp_client.request(ImageStatesWrite(hash=image_tlv_sha256), 1.0)

    if error(r):
        # MCUboot serial recovery only implements "set image state" when it is
        # built with CONFIG_BOOT_SERIAL_IMG_GRP_IMAGE_STATE; without it the
        # bootloader returns ENOTSUP. The image is already written to the
        # primary slot and boots directly on reset, so ENOTSUP here is expected,
        # not fatal.
        if getattr(r, "rc", None) == MGMT_ERR.ENOTSUP:
            _LOGGER.info(
                "Set-image-state not supported in MCUboot serial recovery; "
                "the uploaded image will boot on reset"
            )
        else:
            raise EsphomeError(f"Failed to mark image for testing on {device}: {r}")

    await asyncio.sleep(RESET_DELAY)
    _LOGGER.info("Reset")
    try:
        r = await smp_client.request(ResetWrite(), 1.0)
    except TimeoutError:
        # The device may reset before it can reply; that is still a success.
        r = None

    # Single-slot MCUboot serial recovery answers reset with an error-typed
    # frame carrying rc=EOK (and may not actually reboot until a physical reset);
    # treat EOK/ENOTSUP as success so a completed upload is not reported failed.
    if (
        r is not None
        and error(r)
        and getattr(r, "rc", None)
        not in (
            MGMT_ERR.EOK,
            MGMT_ERR.ENOTSUP,
        )
    ):
        raise EsphomeError(f"Failed to reset {device}: {r}")
