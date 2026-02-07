"""USB serial scanning and pattern matching."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from .models import DiscoveredCanDevice, DiscoveredDevice

SERIAL_BY_ID = "/dev/serial/by-id"

# Supported device prefixes for Klipper/Katapult USB IDs (case-insensitive)
SUPPORTED_PREFIXES = ("usb-klipper_", "usb-katapult_")

# sysfs path for network interface detection (monkeypatched in tests)
_SYSFS_NET = "/sys/class/net"

# CAN interface name pattern (excludes vcan, slcan, etc.)
_CAN_IFACE_RE = re.compile(r"^can\d+$")

# ARPHRD_CAN hardware type value from linux/if_arp.h
_ARPHRD_CAN = 280

# Minimum transmit queue length for CAN operations
MINIMUM_CAN_QLEN = 128

# Subprocess timeout for flashtool.py -q (seconds)
TIMEOUT_CAN_QUERY = 15

# Regex for flashtool.py -q output parsing (verified from flashtool.py source line 723)
_CAN_QUERY_RE = re.compile(r"Detected UUID:\s+([0-9a-f]{12}),\s+Application:\s+(\S+)")


def scan_serial_devices() -> list:
    """Scan /dev/serial/by-id/ and return all USB serial devices."""
    serial_dir = Path(SERIAL_BY_ID)
    if not serial_dir.is_dir():
        return []
    devices = []
    for entry in sorted(serial_dir.iterdir()):
        devices.append(
            DiscoveredDevice(
                path=str(entry),
                filename=entry.name,
            )
        )
    return devices


def is_supported_device(filename: str) -> bool:
    """Return True if filename looks like a Klipper/Katapult USB device."""
    lower = filename.lower()
    return any(lower.startswith(prefix) for prefix in SUPPORTED_PREFIXES)


def match_device(pattern: str, devices: list) -> Optional[DiscoveredDevice]:
    """Find first device whose filename matches a glob pattern."""
    matches = match_devices(pattern, devices)
    return matches[0] if matches else None


def _prefix_variants(pattern: str) -> list[str]:
    """Return pattern variants with both Klipper_ and katapult_ prefixes.

    A pattern like ``usb-katapult_rp2040_30*`` returns both itself and
    ``usb-Klipper_rp2040_30*`` so matching works regardless of which
    bootloader mode the device booted into.
    """
    lower = pattern.lower()
    klipper_prefix = "usb-klipper_"
    katapult_prefix = "usb-katapult_"
    if lower.startswith("usb-klipper_"):
        alt = katapult_prefix + pattern[len(klipper_prefix) :]
        return [pattern, alt]
    if lower.startswith("usb-katapult_"):
        alt = "usb-Klipper_" + pattern[len(katapult_prefix) :]
        return [pattern, alt]
    return [pattern]


def match_devices(pattern: str, devices: list) -> list[DiscoveredDevice]:
    """Find all devices whose filename matches a glob pattern.

    Matching is prefix-agnostic: a ``usb-katapult_*`` pattern will also
    match ``usb-Klipper_*`` filenames and vice-versa so that devices are
    found regardless of which bootloader mode they booted into.
    """
    variants = _prefix_variants(pattern)
    return [
        device for device in devices if any(fnmatch.fnmatch(device.filename, v) for v in variants)
    ]


def find_registered_devices(devices: list, registry_devices: dict) -> tuple:
    """Cross-reference discovered devices against registry.

    Returns ALL matching devices including non-flashable ones. Filtering for
    flashable devices should be done by the caller (flash module) at selection time.

    Args:
        devices: List of DiscoveredDevice from scan_serial_devices()
        registry_devices: Dict of key -> DeviceEntry from registry

    Returns:
        (matched, unmatched) where:
          matched = list of (DeviceEntry, DiscoveredDevice) tuples (includes non-flashable)
          unmatched = list of DiscoveredDevice not matching any pattern
    """
    matched = []
    unmatched_devices = list(devices)  # copy

    for entry in registry_devices.values():
        if entry.serial_pattern is None:
            continue  # CAN devices matched separately (Phase 51)
        variants = _prefix_variants(entry.serial_pattern)
        for device in devices:
            if any(fnmatch.fnmatch(device.filename, v) for v in variants):
                matched.append((entry, device))
                if device in unmatched_devices:
                    unmatched_devices.remove(device)
                break

    return matched, unmatched_devices


def extract_mcu_from_serial(filename: str) -> Optional[str]:
    """Extract MCU type from a /dev/serial/by-id/ filename.

    Examples:
        usb-Klipper_stm32h723xx_290... -> stm32h723
        usb-Klipper_rp2040_303...      -> rp2040
        usb-katapult_stm32h723xx_290.. -> stm32h723
        usb-Klipper_stm32f411xe_600... -> stm32f411
        usb-Beacon_Beacon_RevH_FC2...  -> None (not a Klipper/Katapult device)

    Returns the MCU type without variant suffix (xx, xe, etc.) or None if
    pattern does not match.
    """
    m = re.match(
        r"usb-(?:Klipper|katapult)_([a-z0-9]+?)(?:x[a-z0-9]*)?_",
        filename,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).lower()
    return None


def generate_serial_pattern(filename: str) -> str:
    """Generate a serial glob pattern from a full device filename.

    Takes the full filename up to (but not including) the interface suffix,
    then appends a wildcard.

    Example:
        usb-Klipper_stm32h723xx_29001A001151313531383332-if00
        -> usb-Klipper_stm32h723xx_29001A001151313531383332*
    """
    # Strip -ifNN suffix, add wildcard
    base = re.sub(r"-if\d+$", "", filename)
    return base + "*"


def get_can_interfaces() -> list[str]:
    """List available real CAN interfaces from sysfs.

    Scans /sys/class/net/ for interfaces matching can[0-9]+ pattern
    with ARPHRD_CAN type (280). Excludes virtual CAN (vcan*) by name
    pattern and verifies type as safety belt.

    Returns empty list silently when no CAN hardware exists or sysfs
    is unavailable (non-Linux). Callers decide presentation.
    """
    try:
        entries = os.listdir(_SYSFS_NET)
    except (FileNotFoundError, OSError):
        return []

    interfaces = []
    for name in sorted(entries):
        if not _CAN_IFACE_RE.match(name):
            continue
        type_path = os.path.join(_SYSFS_NET, name, "type")
        try:
            with open(type_path) as f:
                iface_type = int(f.read().strip())
            if iface_type == _ARPHRD_CAN:
                interfaces.append(name)
        except (FileNotFoundError, ValueError, OSError):
            continue
    return interfaces


def is_can_interface_up(interface: str) -> bool:
    """Check if a CAN interface is operationally up.

    Reads /sys/class/net/{interface}/operstate. Returns False if
    interface does not exist or state cannot be read.

    Only checks link state (UP/DOWN). Queue length (qlen) check
    lives in flash pre-flight separately.
    """
    operstate_path = os.path.join(_SYSFS_NET, interface, "operstate")
    try:
        with open(operstate_path) as f:
            return f.read().strip().lower() == "up"
    except (FileNotFoundError, OSError):
        return False


def parse_can_query_output(stdout: str) -> list[DiscoveredCanDevice]:
    """Parse flashtool.py -q stdout into DiscoveredCanDevice list.

    Extracts UUID and Application from lines matching::

        Detected UUID: 48ca7afe7a44, Application: Katapult

    Returns empty list on empty input or no matches.
    """
    results = []
    for match in _CAN_QUERY_RE.finditer(stdout):
        results.append(
            DiscoveredCanDevice(
                uuid=match.group(1),
                application=match.group(2),
            )
        )
    return results


def scan_can_devices(
    interface: str,
    katapult_dir: str,
    timeout: int = TIMEOUT_CAN_QUERY,
) -> list[DiscoveredCanDevice]:
    """Scan CAN bus for devices via flashtool.py -q.

    Args:
        interface: CAN interface name (e.g., "can0").
        katapult_dir: Path to Katapult source (for flashtool.py).
        timeout: Subprocess timeout in seconds.

    Returns:
        List of discovered CAN devices. Empty list on error.
    """
    flashtool = Path(katapult_dir).expanduser() / "scripts" / "flashtool.py"
    if not flashtool.exists():
        return []

    try:
        result = subprocess.run(
            ["python3", str(flashtool), "-i", interface, "-q"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return []
        return parse_can_query_output(result.stdout)
    except (subprocess.TimeoutExpired, OSError):
        return []


def get_can_interface_qlen(interface: str) -> int | None:
    """Read CAN interface transmit queue length from sysfs.

    Returns None if interface does not exist or qlen cannot be read.
    """
    qlen_path = os.path.join(_SYSFS_NET, interface, "tx_queue_len")
    try:
        with open(qlen_path) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def preflight_can_interface(interface: str) -> tuple[bool, str]:
    """Check CAN interface is ready for operations.

    Validates: interface exists, is UP, has qlen >= MINIMUM_CAN_QLEN.
    Returns (ok, error_message). Empty string on success.
    """
    # Check interface exists
    available = get_can_interfaces()
    if interface not in available:
        avail_str = ", ".join(available) if available else "none"
        return False, (
            f"CAN interface '{interface}' not found. "
            f"Available: {avail_str}\n"
            "1. Check USB-to-CAN adapter is connected\n"
            "2. Run: sudo ip link set can0 up type can bitrate 1000000"
        )

    # Check interface is UP
    if not is_can_interface_up(interface):
        return False, (
            f"CAN interface '{interface}' is DOWN.\n"
            f"Run: sudo ip link set {interface} up"
        )

    # Check qlen (non-blocking if unreadable)
    qlen = get_can_interface_qlen(interface)
    if qlen is not None and qlen < MINIMUM_CAN_QLEN:
        return False, (
            f"CAN interface '{interface}' has low txqueuelen ({qlen}). "
            f"Recommended >= {MINIMUM_CAN_QLEN}.\n"
            f"Run: sudo ip link set {interface} txqueuelen 1024"
        )

    return True, ""


def verify_can_device_after_flash(
    uuid: str,
    interface: str,
    katapult_dir: str,
    timeout: float = 15.0,
    poll_interval: float = 2.0,
) -> tuple[bool, str | None]:
    """Verify CAN device returned with Klipper application after flash.

    Polls flashtool.py -q until the device UUID appears with
    Application: Klipper. Returns (success, error_reason).
    """
    flashtool = Path(katapult_dir).expanduser() / "scripts" / "flashtool.py"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["python3", str(flashtool), "-i", interface, "-q"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                devices = parse_can_query_output(result.stdout)
                for dev in devices:
                    if dev.uuid == uuid and dev.application == "Klipper":
                        return True, None
        except (subprocess.TimeoutExpired, OSError):
            pass
        time.sleep(poll_interval)

    return False, f"Device {uuid} did not return as 'Application: Klipper' within {timeout}s"
