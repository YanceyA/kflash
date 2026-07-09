"""Preflight validation for build/flash prerequisites.

Pure engine module (L2): validates the environment and per-device flash
configuration before any hardware operation. Emits advisory/blocking messages
through an :class:`~kflash.events.Emitter` -- never touches the UI layer.

Moved verbatim out of ``flash.py`` (Phase 2 Step B) with public names.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from .events import Emitter
from .models import DeviceEntry
from .validation import (
    SUB_FIELD_PROMPTS,
    find_flash_method_pair,
    validate_bootloader_baud,
    validate_bootloader_flash_pair,
    validate_can_interface,
    validate_canbus_uuid,
    validate_transport_fields,
)

SUSPICIOUS_FIRMWARE_SIZE_BYTES = 16 * 1024


def emit_preflight(em: Emitter, errors: list[str], warnings: list[str]) -> bool:
    """Emit preflight warnings/errors. Returns True if no errors."""
    for warning in warnings:
        em.warn(f"Preflight: {warning}")

    if errors:
        em.error("Preflight checks failed:")
        for err in errors:
            em.error(f"  - {err}")
        return False
    return True


def check_firmware_artifact(
    firmware_path: Optional[str],
    firmware_size: Optional[int],
) -> tuple[Optional[str], Optional[str]]:
    """Validate built firmware artifact path/size.

    Returns:
        (error, warning) tuple. error is fatal; warning is advisory.
    """
    if not firmware_path:
        return ("build returned no firmware path", None)

    path = Path(firmware_path)
    if not path.is_file():
        return (f"firmware file not found: {path}", None)

    size = firmware_size if firmware_size is not None else path.stat().st_size
    if size <= 0:
        return (f"firmware file is empty: {path}", None)

    if size < SUSPICIOUS_FIRMWARE_SIZE_BYTES:
        return (
            None,
            (
                f"firmware file is unusually small ({size} bytes): {path}. "
                "Proceed only if this is expected for your target."
            ),
        )

    return (None, None)


def preflight_build(em: Emitter, klipper_dir: str) -> bool:
    """Validate build prerequisites and Klipper directory."""
    errors: list[str] = []
    warnings: list[str] = []

    klipper_path = Path(klipper_dir).expanduser()
    if not klipper_path.is_dir():
        errors.append(f"Klipper directory not found: {klipper_path}")
    elif not (klipper_path / "Makefile").is_file():
        errors.append(f"Klipper Makefile not found in: {klipper_path}")

    if shutil.which("make") is None:
        errors.append("`make` not found in PATH")
    if shutil.which("arm-none-eabi-gcc") is None:
        errors.append(
            "`arm-none-eabi-gcc` not found in PATH "
            "(install: sudo apt install gcc-arm-none-eabi)"
        )

    return emit_preflight(em, errors, warnings)


def preflight_flash(
    em: Emitter,
    klipper_dir: str,
    katapult_dir: str,
    flash_command: str,
) -> bool:
    """Validate flash prerequisites for the selected flash command."""
    if not preflight_build(em, klipper_dir):
        return False

    errors: list[str] = []
    warnings: list[str] = []

    method = (flash_command or "").strip().lower()
    if not method:
        errors.append("Missing flash command")
        return emit_preflight(em, errors, warnings)
    if method not in ("katapult", "make_flash", "flash_sdcard", "uf2_mount", "katapult_can"):
        errors.append(f"Unknown flash command: {method}")
        return emit_preflight(em, errors, warnings)

    if method == "katapult":
        flashtool = Path(katapult_dir).expanduser() / "scripts" / "flashtool.py"
        if not flashtool.is_file():
            errors.append(f"Katapult flashtool not found at {flashtool}")
        if shutil.which("python3") is None:
            errors.append("`python3` not found in PATH (required for Katapult)")

    if method == "katapult_can":
        flashtool = Path(katapult_dir).expanduser() / "scripts" / "flashtool.py"
        if not flashtool.is_file():
            errors.append(f"Katapult flashtool not found at {flashtool}")
        if shutil.which("python3") is None:
            errors.append("`python3` not found in PATH (required for CAN flash)")

    if method == "flash_sdcard":
        script = Path(klipper_dir).expanduser() / "scripts" / "flash-sdcard.sh"
        if not script.is_file():
            errors.append(f"flash-sdcard.sh not found at {script}")

    if shutil.which("sudo") is None:
        warnings.append("`sudo` not found; Klipper service control may fail")
    if shutil.which("systemctl") is None:
        warnings.append("`systemctl` not found; Klipper service control may fail")

    return emit_preflight(em, errors, warnings)


def get_device_flash_config_issue(entry: DeviceEntry) -> Optional[tuple[str, str]]:
    """Return the first flash configuration issue for a device, or None."""
    if entry.bootloader_method is None:
        return ("Missing configuration", "has no bootloader_method configured")

    if entry.flash_command is None:
        return ("Missing configuration", "has no flash_command configured")

    is_valid, error_msg = validate_bootloader_flash_pair(
        entry.bootloader_method, entry.flash_command
    )
    if not is_valid:
        return ("Invalid configuration", error_msg)

    # Enforce transport identity contract (USB XOR CAN).
    valid_transport, transport_err = validate_transport_fields(
        entry.serial_pattern,
        entry.canbus_uuid,
    )
    if not valid_transport:
        return ("Invalid configuration", transport_err)

    if entry.is_can_device and (
        entry.bootloader_method != "can" or entry.flash_command != "katapult_can"
    ):
        return (
            "Invalid configuration",
            "CAN devices must use bootloader 'can' and flash command 'katapult_can'",
        )

    if (not entry.is_can_device) and (
        entry.bootloader_method == "can" or entry.flash_command == "katapult_can"
    ):
        return (
            "Invalid configuration",
            "USB/serial devices cannot use CAN-only flash methods",
        )

    pair = find_flash_method_pair(entry.bootloader_method, entry.flash_command)
    if pair is not None:
        for field_key in pair.required_sub_fields:
            value = getattr(entry, field_key, None)
            if value is None or (isinstance(value, str) and not value.strip()):
                label = SUB_FIELD_PROMPTS.get(field_key, field_key)
                return (
                    "Missing configuration",
                    f"is missing required field '{label}' for flash method '{pair.name}'",
                )

    if entry.canbus_uuid is not None:
        ok, err = validate_canbus_uuid(entry.canbus_uuid.strip())
        if not ok:
            return ("Invalid configuration", f"has invalid CAN bus UUID: {err}")

    if entry.canbus_interface is not None:
        ok, err = validate_can_interface(entry.canbus_interface.strip())
        if not ok:
            return ("Invalid configuration", f"has invalid CAN interface: {err}")

    if entry.bootloader_baud is not None:
        ok, err = validate_bootloader_baud(entry.bootloader_baud)
        if not ok:
            return ("Invalid configuration", f"has invalid bootloader baud: {err}")

    return None


def validate_device_flash_config(entry: DeviceEntry, em: Emitter) -> bool:
    """Validate device has required flash configuration."""
    issue = get_device_flash_config_issue(entry)
    if issue is None:
        return True

    error_type, detail = issue
    em.error_with_recovery(
        error_type,
        f"Device '{entry.name}' {detail}",
        context={"device": entry.key},
        recovery="Run Config Device (E) to fix configuration",
    )
    return False
