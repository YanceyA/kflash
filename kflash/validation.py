"""Pure validation functions for TUI settings input.

Validates paths (existence, expected files) and numeric values (range checks)
before they are saved to the global configuration.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from itertools import count
from typing import Optional

VALID_BOOTLOADER_METHODS: frozenset[str] = frozenset(
    {
        "usb",
        "serial",
        "manual",
        "none",
        "can",
    }
)

VALID_FLASH_COMMANDS: frozenset[str] = frozenset(
    {
        "katapult",
        "katapult_can",
        "make_flash",
        "flash_sdcard",
        "uf2_mount",
    }
)

VALID_BOOTLOADER_BAUDS: frozenset[int] = frozenset({250000})

COMPATIBLE_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("usb", "katapult"),
        ("usb", "make_flash"),
        ("serial", "katapult"),
        ("can", "katapult_can"),
        ("manual", "make_flash"),
        ("manual", "uf2_mount"),
        ("manual", "katapult"),
        ("none", "flash_sdcard"),
        ("none", "make_flash"),
    }
)


@dataclass(frozen=True)
class FlashMethodPair:
    """A valid bootloader_method + flash_command combination.

    Used by the paired picker UI to present all valid flash method
    combinations as a single numbered selection table.
    """

    bootloader_method: str  # "usb", "serial", "manual", "none"
    flash_command: Optional[str]  # "katapult", "make_flash", etc. or None (build-only)
    name: str  # Display name (e.g., "Katapult USB")
    description: str  # Short description
    notes: str  # Compatibility notes
    required_sub_fields: tuple[str, ...] = ()  # DeviceEntry sub-field keys this pair requires


FLASH_METHOD_TABLE: list[FlashMethodPair] = [
    # --- USB methods (most common) ---
    FlashMethodPair(
        bootloader_method="usb",
        flash_command="katapult",
        name="Katapult USB",
        description="Flash via Katapult bootloader over USB",
        notes="Most common. Requires Katapult installed on the MCU.",
    ),
    FlashMethodPair(
        bootloader_method="usb",
        flash_command="make_flash",
        name="Make Flash USB",
        description="Flash via 'make flash' over USB DFU/serial",
        notes="Uses Klipper's built-in flash. No Katapult needed.",
    ),
    # --- Serial methods ---
    FlashMethodPair(
        bootloader_method="serial",
        flash_command="katapult",
        name="Katapult Serial",
        description="Flash via Katapult bootloader over serial UART",
        notes="Requires serial connection and baud rate configuration.",
        required_sub_fields=("bootloader_baud",),
    ),
    # --- Manual methods ---
    FlashMethodPair(
        bootloader_method="manual",
        flash_command="katapult",
        name="Katapult Manual",
        description="Manually enter bootloader, flash via Katapult",
        notes="User triggers bootloader entry (button/jumper).",
    ),
    FlashMethodPair(
        bootloader_method="manual",
        flash_command="make_flash",
        name="Make Flash Manual",
        description="Manually enter bootloader, flash via 'make flash'",
        notes="User triggers bootloader entry (button/jumper).",
    ),
    FlashMethodPair(
        bootloader_method="manual",
        flash_command="uf2_mount",
        name="UF2 Copy",
        description="Copy .uf2 firmware to mounted USB drive",
        notes="RP2040 boards. Hold BOOTSEL, plug in, copy file.",
        required_sub_fields=("uf2_mount_path",),
    ),
    # --- No bootloader methods ---
    FlashMethodPair(
        bootloader_method="none",
        flash_command="make_flash",
        name="Make Flash Direct",
        description="Flash directly via 'make flash' (no bootloader step)",
        notes="Board stays in DFU mode or uses built-in USB bootloader.",
    ),
    FlashMethodPair(
        bootloader_method="none",
        flash_command="flash_sdcard",
        name="SD Card Flash",
        description="Flash via SD card update script",
        notes="Requires sdcard_board name. Used by some STM32 boards.",
        required_sub_fields=("sdcard_board",),
    ),
    # --- CAN methods ---
    FlashMethodPair(
        bootloader_method="can",
        flash_command="katapult_can",
        name="Katapult CAN",
        description="Flash via Katapult bootloader over CAN bus",
        notes="CAN bus devices. Requires CAN interface up and Katapult installed.",
        required_sub_fields=("canbus_uuid", "canbus_interface"),
    ),
    # --- Build only (special) ---
    FlashMethodPair(
        bootloader_method="none",
        flash_command=None,
        name="Build Only",
        description="Build firmware without flashing",
        notes="For devices managed externally or flashed manually.",
    ),
]


def find_flash_method_pair(
    bootloader_method: str | None,
    flash_command: str | None,
) -> FlashMethodPair | None:
    """Find the FlashMethodPair matching the given bootloader+flash values."""
    for pair in FLASH_METHOD_TABLE:
        if pair.bootloader_method == bootloader_method and pair.flash_command == flash_command:
            return pair
    return None


def _is_rp2_mcu(mcu: str) -> bool:
    """Check if MCU is an RP2040 or RP2350 (case-insensitive prefix match)."""
    return mcu.lower().startswith(("rp2040", "rp2350"))


_CAN_INTERFACE_RE = re.compile(r"^can\d+$")


_RP2_EXCLUDED_PAIRS: frozenset[tuple[str, str | None]] = frozenset(
    {
        ("usb", "make_flash"),  # bootloader entry breaks serial path
        ("serial", "katapult"),  # RP2040 is USB, not UART
        ("manual", "make_flash"),  # same serial re-enum issue
        ("manual", "katapult"),  # BOOTSEL enters ROM mode, not Katapult
        ("none", "flash_sdcard"),  # not applicable
    }
)


def filter_flash_methods_for_mcu(mcu: str | None) -> list[FlashMethodPair]:
    """Return flash method table filtered for MCU compatibility.

    For RP2040/RP2350 boards, excludes methods that rely on serial
    re-enumeration (which fails because RP2 ROM bootloader presents as
    mass storage, not a serial device). Reorders so that the recommended
    ``none`` + ``make_flash`` (picoboot) method is first.

    For all other MCUs (or when MCU is None), returns the full table.
    """
    if mcu is None or not _is_rp2_mcu(mcu):
        return list(FLASH_METHOD_TABLE)

    filtered = [
        pair
        for pair in FLASH_METHOD_TABLE
        if (pair.bootloader_method, pair.flash_command) not in _RP2_EXCLUDED_PAIRS
    ]

    # Move none+make_flash (Make Flash Direct / picoboot) to front
    reordered: list[FlashMethodPair] = []
    rest: list[FlashMethodPair] = []
    for pair in filtered:
        if pair.bootloader_method == "none" and pair.flash_command == "make_flash":
            reordered.insert(0, pair)
        elif pair.flash_command is None:
            rest.append(pair)  # Build Only goes to end
        else:
            reordered.append(pair)
    reordered.extend(rest)
    return reordered


def filter_flash_methods_for_device(
    mcu: str | None,
    is_can_device: bool,
) -> list[FlashMethodPair]:
    """Return flash methods filtered by MCU and device transport.

    CAN devices only support CAN flashing methods. USB/serial devices exclude
    CAN-only methods from the picker to prevent invalid transport pairings.
    """
    filtered = filter_flash_methods_for_mcu(mcu)
    if is_can_device:
        return [pair for pair in filtered if pair.bootloader_method == "can"]
    return [pair for pair in filtered if pair.bootloader_method != "can"]


def validate_numeric_setting(
    raw: str, min_val: float, max_val: float
) -> tuple[bool, float | None, str]:
    """Validate a numeric setting value.

    Returns:
        (is_valid, parsed_value, error_message)
    """
    try:
        val = float(raw)
    except ValueError:
        return False, None, "Not a number"

    if val < min_val or val > max_val:
        return False, None, f"Must be between {min_val} and {max_val}"

    return True, val, ""


def validate_path_setting(raw: str, setting_key: str) -> tuple[bool, str]:
    """Validate a path setting value.

    Expands ~ before checking. Returns (is_valid, error_message).
    """
    expanded = os.path.expanduser(raw)

    if not os.path.isdir(expanded):
        return False, f"Directory does not exist: {expanded}"

    if setting_key == "klipper_dir":
        makefile = os.path.join(expanded, "Makefile")
        if not os.path.isfile(makefile):
            return False, f"Missing expected file: {makefile}"

    elif setting_key == "katapult_dir":
        flashtool = os.path.join(expanded, "scripts", "flashtool.py")
        if not os.path.isfile(flashtool):
            return False, f"Missing expected file: {flashtool}"

    return True, ""


def validate_device_key(key: str, registry, current_key: str | None = None) -> tuple[bool, str]:
    """Validate a device key for registration or rename.

    Args:
        key: Proposed device key (whitespace is stripped).
        registry: Registry instance for uniqueness check.
        current_key: If renaming, the current key (self-rename is allowed).

    Returns:
        (is_valid, error_message) — empty string on success.
    """
    key = key.strip()

    if not key:
        return False, "Device key cannot be empty"

    if not re.match(r"^[a-z0-9][a-z0-9_-]*$", key):
        return False, "Key must start with a-z/0-9 and contain only a-z, 0-9, _ or -"

    if current_key is not None and key == current_key:
        return True, ""

    if registry.get(key) is not None:
        return False, f"Device '{key}' already registered"

    return True, ""


def generate_device_key(name: str, registry) -> str:
    """Generate a unique device key (slug) from a display name.

    Converts a human-readable device name into a filesystem-safe,
    lowercase slug suitable for use as a registry key. Appends a
    numeric suffix (-2, -3, ...) if the slug already exists.

    Args:
        name: Human-readable device name.
        registry: Registry instance for collision checking.

    Returns:
        A unique slug string, at most 64 characters.

    Raises:
        ValueError: If the name produces an empty slug after normalization.

    Examples:
        >>> generate_device_key("Octopus Pro v1.1", registry)
        'octopus-pro-v1-1'
        >>> generate_device_key("Cafe MCU", registry)
        'cafe-mcu'
    """
    # Unicode decomposition and ASCII folding
    slug = unicodedata.normalize("NFKD", name)
    slug = slug.encode("ascii", "ignore").decode("ascii")

    # Lowercase, replace spaces/underscores with hyphens
    slug = slug.lower()
    slug = slug.replace(" ", "-").replace("_", "-").replace(".", "-")

    # Strip everything except alphanumeric and hyphens
    slug = re.sub(r"[^a-z0-9-]", "", slug)

    # Collapse consecutive hyphens, strip leading/trailing hyphens
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")

    # Truncate to 64 chars, clean trailing hyphen from truncation
    slug = slug[:64].rstrip("-")

    if not slug:
        raise ValueError("Name produces an empty slug after normalization")

    # Check for collisions
    candidate = slug
    if registry.get(candidate) is None:
        return candidate

    for n in count(2):
        suffix = f"-{n}"
        candidate = slug[: 64 - len(suffix)] + suffix
        if registry.get(candidate) is None:
            return candidate

    raise RuntimeError("unreachable")  # count() is infinite


def validate_bootloader_flash_pair(bootloader_method: str, flash_command: str) -> tuple[bool, str]:
    """Validate that a bootloader method and flash command are compatible.

    Checks:
    1. bootloader_method is in VALID_BOOTLOADER_METHODS
    2. flash_command is in VALID_FLASH_COMMANDS
    3. The (bootloader_method, flash_command) pair is in COMPATIBLE_PAIRS

    Returns:
        (is_valid, error_message) -- empty string on success.
    """
    if bootloader_method not in VALID_BOOTLOADER_METHODS:
        valid = sorted(VALID_BOOTLOADER_METHODS)
        return False, f"Invalid bootloader method '{bootloader_method}'. Valid: {valid}"
    if flash_command not in VALID_FLASH_COMMANDS:
        valid = sorted(VALID_FLASH_COMMANDS)
        return False, f"Invalid flash command '{flash_command}'. Valid: {valid}"
    if (bootloader_method, flash_command) not in COMPATIBLE_PAIRS:
        return False, (
            f"Incompatible pair: bootloader '{bootloader_method}' + flash command '{flash_command}'"
        )
    return True, ""


def validate_canbus_uuid(uuid_str: str) -> tuple[bool, str]:
    """Validate a CAN bus UUID string (12-char lowercase hex).

    Normalizes to lowercase before checking. Accepts uppercase input.

    Returns:
        (is_valid, error_message) -- empty string on success.
    """
    if not uuid_str:
        return False, "CAN bus UUID cannot be empty"
    normalized = uuid_str.lower()
    if not re.match(r"^[0-9a-f]{12}$", normalized):
        return False, f"CAN bus UUID must be exactly 12 hex characters, got '{uuid_str}'"
    return True, ""


def validate_bootloader_baud(baud: int) -> tuple[bool, str]:
    """Validate a bootloader baud rate.

    Returns:
        (is_valid, error_message) -- empty string on success.
    """
    if baud not in VALID_BOOTLOADER_BAUDS:
        return False, f"Invalid baud rate {baud}. Valid: {sorted(VALID_BOOTLOADER_BAUDS)}"
    return True, ""


def validate_can_interface(name: str) -> tuple[bool, str]:
    """Validate a CAN interface name (can0, can1, etc.).

    Only accepts real CAN interface names (can[0-9]+).
    Rejects virtual CAN (vcan), serial-line CAN (slcan), and other variants.

    Returns:
        (is_valid, error_message) -- empty string on success.
    """
    if not name:
        return False, "CAN interface name cannot be empty"
    if not _CAN_INTERFACE_RE.match(name):
        return False, f"CAN interface must match 'can[0-9]+', got '{name}'"
    return True, ""


def validate_transport_fields(
    serial_pattern: str | None,
    canbus_uuid: str | None,
) -> tuple[bool, str]:
    """Validate USB and CAN fields are mutually exclusive.

    A device must have exactly one transport identity: either a USB serial
    pattern or a CAN bus UUID, never both and never neither.

    Returns:
        (is_valid, error_message) -- empty string on success.
    """
    if serial_pattern and canbus_uuid:
        return False, "Device cannot have both serial_pattern and canbus_uuid"
    if not serial_pattern and not canbus_uuid:
        return False, "Device must have either serial_pattern or canbus_uuid"
    return True, ""
