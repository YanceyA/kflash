"""Bootloader entry dispatcher and two-phase re-enumeration polling.

Routes device bootloader entry to the correct method handler (usb, serial,
manual, none, can) based on DeviceEntry.bootloader_method.  After the
bootloader command, performs two-phase re-enumeration polling: wait for the
original device path to disappear, then scan for reappearance with a matching
device signature (MCU type + serial hex).

Public API:
    enter_bootloader()         -- dispatcher, retry logic, stagger delay
    extract_device_signature() -- extract (mcu_type, serial_hex) from filename
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .models import BootloaderResult, DeviceEntry
from .safety import discover_python_path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIMEOUT_BOOTLOADER_CMD = 10  # Seconds for subprocess calls (flashtool / flash_usb)
TIMEOUT_REENUMERATION = 10.0  # Seconds to wait for device re-enumeration
POLL_INTERVAL = 1.0  # Seconds between re-enumeration polls
SERIAL_DIR = "/dev/serial/by-id"

# Default venv paths (sibling-directory convention on Raspberry Pi)
DEFAULT_MOONRAKER_VENV = "~/moonraker-env"

# Device signature regex: matches Klipper_ or katapult_ USB serial names
_SIGNATURE_RE = re.compile(r"usb-(?:Klipper|katapult)_([a-zA-Z0-9]+)_([A-Fa-f0-9]+)")


# ---------------------------------------------------------------------------
# Public: extract_device_signature
# ---------------------------------------------------------------------------


def extract_device_signature(filename: str) -> tuple[str, str] | None:
    """Extract (mcu_type, serial_hex) from a /dev/serial/by-id/ filename.

    Parses device filenames with ``Klipper_`` or ``katapult_`` prefix and
    returns the MCU type (lowercased) and hexadecimal serial identifier.

    Args:
        filename: Device filename (basename only, not full path).

    Returns:
        Tuple of (mcu_type, serial_hex) or None if the filename does not
        match the expected Klipper/Katapult USB serial naming convention.

    Examples:
        >>> extract_device_signature("usb-Klipper_stm32h723xx_29001A001151-if00")
        ('stm32h723xx', '29001A001151')
        >>> extract_device_signature("usb-Beacon_Beacon_RevH_FC2-if00")
        None
    """
    match = _SIGNATURE_RE.search(filename)
    if not match:
        return None
    return (match.group(1).lower(), match.group(2))


# ---------------------------------------------------------------------------
# Public: enter_bootloader (dispatcher)
# ---------------------------------------------------------------------------


def enter_bootloader(
    device_path: str,
    device_entry: DeviceEntry,
    klipper_dir: str,
    katapult_dir: str = "~/katapult",
    stagger_delay: float = 2.0,
    out: Any = None,
    batch_mode: bool = False,
) -> BootloaderResult:
    """Enter bootloader mode for a device using its configured method.

    Dispatches to the method-specific handler based on
    ``device_entry.bootloader_method``.  After the handler returns, applies
    retry-once logic when re-enumeration fails and an output channel is
    available.

    Args:
        device_path: Current /dev/serial/by-id/ path to the device.
        device_entry: Registered device with bootloader_method field.
        klipper_dir: Path to the Klipper source directory.
        katapult_dir: Path to the Katapult source directory.
        stagger_delay: Seconds to wait after re-enumeration for USB to settle.
        out: Optional output object with ``confirm()`` method for retry prompts.
            When None (batch mode), no retry is offered.
        batch_mode: Reserved for future use.

    Returns:
        BootloaderResult with success status, new device path, and timing.
    """
    start = time.monotonic()

    method = device_entry.bootloader_method or "none"

    # Method dispatch table
    dispatch = {
        "usb": _enter_usb,
        "serial": _enter_serial,
        "manual": _enter_manual,
        "none": _enter_none,
        "can": _enter_can,
    }

    handler = dispatch.get(method)
    if handler is None:
        return BootloaderResult(
            success=False,
            error_message=f"Unknown bootloader method: {method}",
            elapsed_seconds=time.monotonic() - start,
        )

    # First attempt
    result = handler(device_path, device_entry, klipper_dir, katapult_dir, stagger_delay, out)

    # Retry logic: offer one retry when re-enumeration fails and out is available
    if not result.success and out is not None and method != "none":
        should_retry = out.confirm("Device not found. Try again?", default=True)
        if should_retry:
            result = handler(
                device_path,
                device_entry,
                klipper_dir,
                katapult_dir,
                stagger_delay,
                out,
            )

    result.elapsed_seconds = time.monotonic() - start
    return result


# ---------------------------------------------------------------------------
# Helper: _get_klippy_env_python
# ---------------------------------------------------------------------------


def _get_klippy_env_python(klipper_dir: str) -> str:
    """Derive klippy-env Python interpreter path from klipper_dir.

    Uses the sibling-directory convention: if klipper_dir is ``~/klipper``,
    looks for ``~/klippy-env/bin/python3``.

    Args:
        klipper_dir: Path to the Klipper source directory (may contain ~).

    Returns:
        Absolute path to the klippy-env python3 binary, or ``"python3"``
        as fallback if the venv is not found.
    """
    klipper_path = Path(klipper_dir).expanduser().resolve()
    klippy_env = klipper_path.parent / "klippy-env"
    python3 = klippy_env / "bin" / "python3"
    if python3.is_file():
        return str(python3)
    return "python3"


# ---------------------------------------------------------------------------
# Helper: _get_moonraker_env_python
# ---------------------------------------------------------------------------


def _get_moonraker_env_python(klipper_dir: str) -> str:
    """Derive Moonraker venv Python path from klipper_dir's parent.

    Serial bootloader entry uses flashtool.py which requires pyserial.
    The Moonraker venv has pyserial installed.

    Args:
        klipper_dir: Path to the Klipper source directory.

    Returns:
        Path to Moonraker venv python3, or ``"python3"`` as fallback.
    """
    klipper_path = Path(klipper_dir).expanduser().resolve()
    moonraker_env = klipper_path.parent / "moonraker-env"
    discovered = discover_python_path(str(moonraker_env))
    if discovered:
        return discovered
    # Also try the default path
    default_venv = os.path.expanduser(DEFAULT_MOONRAKER_VENV)
    discovered = discover_python_path(default_venv)
    return discovered if discovered else "python3"


# ---------------------------------------------------------------------------
# Helper: _poll_for_reenumeration
# ---------------------------------------------------------------------------


def _poll_for_reenumeration(
    original_path: str,
    serial_pattern: str,
    timeout: float = TIMEOUT_REENUMERATION,
    interval: float = POLL_INTERVAL,
    scan_fn: Any = None,
) -> str | None:
    """Two-phase polling for USB device re-enumeration after bootloader entry.

    Phase 1 (disappearance): Poll until the original device filename is no
    longer present in the device listing.  This confirms the device has
    actually rebooted into bootloader mode.

    Phase 2 (reappearance): Scan for any device whose signature (MCU type +
    serial hex) matches the original device.  The prefix may change from
    ``Klipper_`` to ``katapult_`` (or vice versa).

    Args:
        original_path: Full path (e.g. ``/dev/serial/by-id/usb-Klipper_...``).
        serial_pattern: Glob pattern from DeviceEntry (unused in signature
            matching, kept for API compatibility).
        timeout: Maximum seconds to wait across both phases.
        interval: Seconds between polls (use 0.0 in tests).
        scan_fn: Optional callable returning ``list[str]`` of filenames in
            the serial directory.  Defaults to ``os.listdir(SERIAL_DIR)``.

    Returns:
        Full device path (``SERIAL_DIR + "/" + filename``) of the re-enumerated
        device, or None on timeout.
    """
    if scan_fn is None:

        def scan_fn() -> list[str]:
            """Default scanner: list /dev/serial/by-id/ contents."""
            try:
                return os.listdir(SERIAL_DIR)
            except (FileNotFoundError, OSError):
                return []

    original_filename = os.path.basename(original_path)
    original_sig = extract_device_signature(original_filename)

    deadline = time.monotonic() + timeout

    # Phase 1: Wait for original device to disappear
    while time.monotonic() < deadline:
        filenames = scan_fn()
        if original_filename not in filenames:
            break  # Device has disappeared
        if interval > 0:
            time.sleep(interval)
    else:
        # Timeout: device never disappeared
        return None

    # Phase 2: Scan for matching device reappearance
    while time.monotonic() < deadline:
        filenames = scan_fn()
        for fname in filenames:
            sig = extract_device_signature(fname)
            if sig is not None and original_sig is not None and sig == original_sig:
                return os.path.join(SERIAL_DIR, fname)
        if interval > 0:
            time.sleep(interval)

    # Timeout: device never reappeared
    return None


# ---------------------------------------------------------------------------
# Method: _enter_none
# ---------------------------------------------------------------------------


def _enter_none(
    device_path: str,
    device_entry: DeviceEntry,
    klipper_dir: str,
    katapult_dir: str,
    stagger_delay: float,
    out: Any,
) -> BootloaderResult:
    """Handle bootloader_method='none': verify device exists, return path.

    No bootloader entry is performed.  The device is expected to already be
    in a flashable state.  No stagger delay is applied since there is no
    reboot.

    Args:
        device_path: Current device path to verify.
        device_entry: Registered device entry.
        klipper_dir: Klipper source directory (unused).
        katapult_dir: Katapult source directory (unused).
        stagger_delay: Stagger delay value (not applied for 'none').
        out: Output object (unused).

    Returns:
        BootloaderResult with success if device exists, failure otherwise.
    """
    if not os.path.exists(device_path):
        return BootloaderResult(
            success=False,
            error_message=f"Device not found: {device_path}",
        )
    return BootloaderResult(
        success=True,
        device_path=device_path,
    )


# ---------------------------------------------------------------------------
# Method: _enter_usb
# ---------------------------------------------------------------------------


def _enter_usb(
    device_path: str,
    device_entry: DeviceEntry,
    klipper_dir: str,
    katapult_dir: str,
    stagger_delay: float,
    out: Any,
) -> BootloaderResult:
    """Enter bootloader via Klipper's flash_usb.enter_bootloader().

    Calls the Klipper flash_usb script through the klippy-env Python
    interpreter to trigger a USB bootloader entry.  After the command,
    polls for device re-enumeration.

    Args:
        device_path: Current device serial path.
        device_entry: Registered device entry (serial_pattern used for polling).
        klipper_dir: Path to the Klipper source directory.
        katapult_dir: Katapult source directory (unused for USB method).
        stagger_delay: Seconds to wait after re-enumeration.
        out: Output object (unused in USB method).

    Returns:
        BootloaderResult with new device path on success.
    """
    python_path = _get_klippy_env_python(klipper_dir)

    # Build the script that imports and calls flash_usb.enter_bootloader
    klipper_path = Path(klipper_dir).expanduser().resolve()
    scripts_dir = str(klipper_path / "scripts")
    script = (
        f"import sys; sys.path.insert(0, {scripts_dir!r}); "
        f"from flash_usb import enter_bootloader; "
        f"enter_bootloader({device_path!r})"
    )

    try:
        subprocess.run(
            [python_path, "-c", script],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_BOOTLOADER_CMD,
        )
    except subprocess.TimeoutExpired:
        return BootloaderResult(
            success=False,
            error_message=f"USB bootloader entry timed out ({TIMEOUT_BOOTLOADER_CMD}s)",
        )
    except OSError as exc:
        return BootloaderResult(
            success=False,
            error_message=f"Failed to run USB bootloader entry: {exc}",
        )

    # Poll for device re-enumeration
    new_path = _poll_for_reenumeration(
        original_path=device_path,
        serial_pattern=device_entry.serial_pattern,
        timeout=TIMEOUT_REENUMERATION,
        interval=POLL_INTERVAL,
    )

    if new_path is None:
        return BootloaderResult(
            success=False,
            error_message="Device did not re-enumerate after USB bootloader entry",
        )

    # Stagger delay for USB subsystem to stabilize
    if stagger_delay > 0:
        time.sleep(stagger_delay)

    return BootloaderResult(
        success=True,
        device_path=new_path,
    )


# ---------------------------------------------------------------------------
# Method: _enter_serial
# ---------------------------------------------------------------------------


def _enter_serial(
    device_path: str,
    device_entry: DeviceEntry,
    klipper_dir: str,
    katapult_dir: str,
    stagger_delay: float,
    out: Any,
) -> BootloaderResult:
    """Enter bootloader via Katapult flashtool.py -r (serial reset).

    Uses the Moonraker venv Python (which has pyserial installed) to run
    flashtool.py with the reset flag.

    Args:
        device_path: Current device serial path.
        device_entry: Registered device entry.
        klipper_dir: Path to the Klipper source directory.
        katapult_dir: Path to the Katapult source directory.
        stagger_delay: Seconds to wait after re-enumeration.
        out: Output object (unused in serial method).

    Returns:
        BootloaderResult with new device path on success.
    """
    # Verify flashtool.py exists
    katapult_path = Path(katapult_dir).expanduser()
    flashtool = katapult_path / "scripts" / "flashtool.py"

    if not flashtool.exists():
        return BootloaderResult(
            success=False,
            error_message=f"Katapult flashtool not found: {flashtool}",
        )

    # Use Moonraker venv for pyserial access
    python_path = _get_moonraker_env_python(klipper_dir)

    try:
        subprocess.run(
            [python_path, str(flashtool), "-r", "-d", device_path],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_BOOTLOADER_CMD,
        )
    except subprocess.TimeoutExpired:
        return BootloaderResult(
            success=False,
            error_message=f"Serial bootloader entry timed out ({TIMEOUT_BOOTLOADER_CMD}s)",
        )
    except OSError as exc:
        return BootloaderResult(
            success=False,
            error_message=f"Failed to run flashtool.py: {exc}",
        )

    # Poll for device re-enumeration
    new_path = _poll_for_reenumeration(
        original_path=device_path,
        serial_pattern=device_entry.serial_pattern,
        timeout=TIMEOUT_REENUMERATION,
        interval=POLL_INTERVAL,
    )

    if new_path is None:
        return BootloaderResult(
            success=False,
            error_message="Device did not re-enumerate after serial bootloader entry",
        )

    # Stagger delay for USB subsystem to stabilize
    if stagger_delay > 0:
        time.sleep(stagger_delay)

    return BootloaderResult(
        success=True,
        device_path=new_path,
    )


# ---------------------------------------------------------------------------
# Method: _enter_manual
# ---------------------------------------------------------------------------


def _enter_manual(
    device_path: str,
    device_entry: DeviceEntry,
    klipper_dir: str,
    katapult_dir: str,
    stagger_delay: float,
    out: Any,
) -> BootloaderResult:
    """Enter bootloader via manual user action (button press, jumper, etc.).

    Prompts the user to physically put the device into bootloader mode,
    then waits for Enter key before scanning for the re-enumerated device.

    Args:
        device_path: Current device serial path.
        device_entry: Registered device entry.
        klipper_dir: Klipper source directory (unused).
        katapult_dir: Katapult source directory (unused).
        stagger_delay: Seconds to wait after re-enumeration.
        out: Output object for user prompts.

    Returns:
        BootloaderResult with new device path on success.
    """
    if out is not None:
        out.info(
            "Manual Bootloader",
            f"Put '{device_entry.name}' into bootloader mode, then press Enter.",
        )

    try:
        input()
    except (EOFError, KeyboardInterrupt):
        return BootloaderResult(
            success=False,
            error_message="Manual bootloader entry cancelled",
        )

    # UF2 flow: device enters mass storage mode, not serial.
    # Skip serial re-enumeration -- flash_uf2() uses mount path, not device_path.
    if device_entry.flash_command == "uf2_mount":
        return BootloaderResult(success=True, device_path=None)

    # Poll for device re-enumeration
    new_path = _poll_for_reenumeration(
        original_path=device_path,
        serial_pattern=device_entry.serial_pattern,
        timeout=TIMEOUT_REENUMERATION,
        interval=POLL_INTERVAL,
    )

    if new_path is None:
        return BootloaderResult(
            success=False,
            error_message="Device did not re-enumerate after manual bootloader entry",
        )

    # Stagger delay for USB subsystem to stabilize
    if stagger_delay > 0:
        time.sleep(stagger_delay)

    return BootloaderResult(
        success=True,
        device_path=new_path,
    )


# ---------------------------------------------------------------------------
# Method: _enter_can
# ---------------------------------------------------------------------------


def _enter_can(
    device_path: str,
    device_entry: DeviceEntry,
    klipper_dir: str,
    katapult_dir: str,
    stagger_delay: float,
    out: Any,
) -> BootloaderResult:
    """Enter bootloader via Katapult flashtool.py -r over CAN bus.

    Uses ``python3`` (not klippy-env) since CAN mode in flashtool.py uses
    only stdlib (raw CAN sockets via ``socket.AF_CAN``).  The device_path
    parameter is unused for CAN devices (CAN has no USB serial path).

    Args:
        device_path: Unused for CAN (CAN devices have no USB serial path).
        device_entry: Registered device with canbus_uuid and canbus_interface.
        klipper_dir: Klipper source directory (unused for CAN).
        katapult_dir: Path to the Katapult source directory.
        stagger_delay: Seconds to wait after bootloader entry.
        out: Output object (unused in CAN method).

    Returns:
        BootloaderResult with success status.  device_path is always None
        for CAN devices (no USB serial path to return).
    """
    # Verify flashtool.py exists
    katapult_path = Path(katapult_dir).expanduser()
    flashtool = katapult_path / "scripts" / "flashtool.py"

    if not flashtool.exists():
        return BootloaderResult(
            success=False,
            error_message=f"Katapult flashtool not found: {flashtool}",
        )

    interface = device_entry.canbus_interface or "can0"
    uuid = device_entry.canbus_uuid or ""

    try:
        result = subprocess.run(
            ["python3", str(flashtool), "-i", interface, "-r", "-u", uuid],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_BOOTLOADER_CMD,
        )
    except subprocess.TimeoutExpired:
        return BootloaderResult(
            success=False,
            error_message=f"CAN bootloader entry timed out ({TIMEOUT_BOOTLOADER_CMD}s)",
        )
    except OSError as exc:
        return BootloaderResult(
            success=False,
            error_message=f"Failed to run flashtool.py: {exc}",
        )

    if result.returncode != 0:
        error_detail = result.stderr.strip() or result.stdout.strip()
        return BootloaderResult(
            success=False,
            error_message=f"CAN bootloader entry failed: {error_detail}",
        )

    # Pause for device to settle in bootloader mode.
    if stagger_delay > 0:
        time.sleep(stagger_delay)

    return BootloaderResult(
        success=True,
        device_path=None,  # CAN devices have no USB serial path
    )
