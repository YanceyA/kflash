"""Dual-method flash operations: Katapult-first with make-flash fallback.

Public API (Phase 45+):
    execute_flash()         -- dispatcher, routes to correct flash function
    flash_katapult()        -- flash via Katapult flashtool.py (USB serial)
    flash_katapult_can()    -- flash via Katapult flashtool.py (CAN bus)
    flash_make()            -- flash via make flash
    flash_sdcard()          -- flash via flash-sdcard.sh script
    flash_uf2()             -- flash via UF2 mount copy

DEPRECATED (legacy API, retained for backward compatibility):
    flash_device()      -- original dual-method with fallback
    _try_katapult_flash -- original captured-output katapult
    _try_make_flash     -- original captured-output make flash
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from .bootloader import _get_klippy_env_python
from .errors import DiscoveryError, format_error
from .models import DeviceEntry, FlashResult, GlobalConfig, KatapultCheckResult
from .safety import discover_python_path

# Default timeout for flash operations (from CONTEXT.md)
TIMEOUT_FLASH = 60

# CAN flash constants: CAN is slower (shared 1Mbps bus) and less reliable
TIMEOUT_CAN_FLASH = 120  # CAN flash is slower (shared 1Mbps bus)
CAN_RETRY_ATTEMPTS = 3  # Retry on transient CAN bus errors

# Default Moonraker virtualenv path
DEFAULT_MOONRAKER_VENV = "~/moonraker-env"


def _get_python_path() -> str:
    """Get Python interpreter path, preferring Moonraker venv.

    Returns path to Python from Moonraker venv if available,
    otherwise falls back to system 'python3'.
    """
    venv_path = os.path.expanduser(DEFAULT_MOONRAKER_VENV)
    discovered = discover_python_path(venv_path)
    return discovered if discovered else "python3"


# Katapult detection timing (from Phase 21 hardware research)
BOOTLOADER_ENTRY_TIMEOUT = 5.0  # Max wait for flashtool.py -r
USB_RESET_SLEEP = 0.5  # Pause between deauthorize/reauthorize
POLL_INTERVAL = 0.25  # Serial device polling interval
POLL_TIMEOUT = 5.0  # Max wait for device reappearance


def verify_device_path(device_path: str) -> None:
    """Verify the device is still connected.

    Args:
        device_path: Path to the USB serial device.

    Raises:
        DiscoveryError: If the device is not found.
    """
    if not Path(device_path).exists():
        msg = format_error(
            "Device disconnected",
            "Device no longer connected after build",
            context={"path": device_path},
            recovery=(
                "1. Check USB cable connection\n"
                "2. Verify board power LED is on\n"
                "3. List devices: ls /dev/serial/by-id/\n"
                "4. Reconnect and retry flash"
            ),
        )
        raise DiscoveryError(msg)


# DEPRECATED: Legacy function retained for backward compatibility with flash.py
def _try_katapult_flash(
    device_path: str,
    firmware_path: str,
    katapult_dir: str,
    timeout: int,
) -> FlashResult:
    """Attempt to flash using Katapult flashtool.py.

    Args:
        device_path: Path to the USB serial device.
        firmware_path: Path to the firmware binary (klipper.bin).
        katapult_dir: Path to the Katapult directory.
        timeout: Seconds before timeout.

    Returns:
        FlashResult with success status and details.
    """
    start = time.monotonic()

    # Build path to flashtool.py
    flashtool = Path(katapult_dir).expanduser() / "scripts" / "flashtool.py"

    if not flashtool.exists():
        return FlashResult(
            success=False,
            method="katapult",
            elapsed_seconds=time.monotonic() - start,
            error_message=f"Katapult flashtool not found: {flashtool}",
        )

    try:
        python_path = _get_python_path()
        result = subprocess.run(
            [python_path, str(flashtool), "-d", device_path, "-f", firmware_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - start

        if result.returncode == 0:
            return FlashResult(
                success=True,
                method="katapult",
                elapsed_seconds=elapsed,
            )
        else:
            return FlashResult(
                success=False,
                method="katapult",
                elapsed_seconds=elapsed,
                error_message=result.stderr.strip() or result.stdout.strip(),
            )

    except subprocess.TimeoutExpired:
        return FlashResult(
            success=False,
            method="katapult",
            elapsed_seconds=timeout,
            error_message=f"Flash timeout ({timeout}s) - device may need manual recovery",
        )


# DEPRECATED: Legacy function retained for backward compatibility with flash.py
def _try_make_flash(
    device_path: str,
    klipper_dir: str,
    timeout: int,
) -> FlashResult:
    """Attempt to flash using make flash.

    Args:
        device_path: Path to the USB serial device.
        klipper_dir: Path to the Klipper directory.
        timeout: Seconds before timeout.

    Returns:
        FlashResult with success status and details.
    """
    start = time.monotonic()
    klipper_path = Path(klipper_dir).expanduser()

    try:
        result = subprocess.run(
            ["make", f"FLASH_DEVICE={device_path}", "flash"],
            cwd=str(klipper_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - start

        if result.returncode == 0:
            return FlashResult(
                success=True,
                method="make_flash",
                elapsed_seconds=elapsed,
            )
        else:
            return FlashResult(
                success=False,
                method="make_flash",
                elapsed_seconds=elapsed,
                error_message=result.stderr.strip() or result.stdout.strip(),
            )

    except subprocess.TimeoutExpired:
        return FlashResult(
            success=False,
            method="make_flash",
            elapsed_seconds=timeout,
            error_message=f"Flash timeout ({timeout}s) - device may need manual recovery",
        )


def _resolve_usb_sysfs_path(serial_path: str) -> str:
    """Resolve /dev/serial/by-id/ symlink to sysfs USB authorized file path."""
    real_dev = os.path.realpath(serial_path)
    tty_name = os.path.basename(real_dev)
    sysfs_device = f"/sys/class/tty/{tty_name}/device"
    if not os.path.exists(sysfs_device):
        raise DiscoveryError(f"sysfs path not found: {sysfs_device}")
    iface_path = os.path.realpath(sysfs_device)
    usb_dev_path = os.path.dirname(iface_path)
    authorized = os.path.join(usb_dev_path, "authorized")
    if not os.path.exists(authorized):
        raise DiscoveryError(f"USB authorized file not found: {authorized}")
    return authorized


def _usb_sysfs_reset(authorized_path: str) -> None:
    """Toggle USB device authorized flag to force re-enumeration."""
    for value in ("0", "1"):
        result = subprocess.run(
            ["sudo", "-n", "tee", authorized_path],
            input=value,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise DiscoveryError(
                f"Failed to write '{value}' to {authorized_path}: {result.stderr.strip()}"
            )
        if value == "0":
            time.sleep(USB_RESET_SLEEP)


def _poll_for_serial_device(
    pattern: str,
    timeout: float = POLL_TIMEOUT,
) -> Optional[str]:
    """Poll /dev/serial/by-id/ for device matching glob pattern."""
    serial_dir = "/dev/serial/by-id"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            from .discovery import _prefix_variants

            variants = _prefix_variants(pattern)
            for name in os.listdir(serial_dir):
                if any(fnmatch.fnmatch(name, v) for v in variants):
                    return os.path.join(serial_dir, name)
        except FileNotFoundError:
            pass  # Directory may vanish briefly during USB reset
        time.sleep(POLL_INTERVAL)
    return None


def check_katapult(
    device_path: str,
    serial_pattern: str,
    katapult_dir: str,
    log: Optional[Callable[[str], None]] = None,
) -> KatapultCheckResult:
    """Check whether a device has Katapult bootloader installed.

    Sends flashtool.py -r to enter bootloader mode, then polls for a
    katapult_ device. If none appears, performs USB sysfs reset to
    recover the device back to Klipper_ mode.

    Args:
        device_path: Current /dev/serial/by-id/ path (Klipper_ device).
        serial_pattern: Glob pattern from DeviceEntry.serial_pattern.
        katapult_dir: Path to Katapult source (for flashtool.py).
        log: Optional callback for progress messages.

    Returns:
        KatapultCheckResult with tri-state has_katapult.
    """
    start = time.monotonic()

    # Extract hex serial identifier from device path
    match = re.search(
        r"usb-(?:Klipper|katapult)_[a-zA-Z0-9]+_([A-Fa-f0-9]+)",
        os.path.basename(device_path),
    )
    if not match:
        return KatapultCheckResult(
            has_katapult=None,
            error_message="Could not extract serial from device path",
            elapsed_seconds=time.monotonic() - start,
        )
    serial_hex = match.group(1)

    # Resolve sysfs path for USB reset recovery
    try:
        authorized_path = _resolve_usb_sysfs_path(device_path)
    except (DiscoveryError, OSError) as exc:
        return KatapultCheckResult(
            has_katapult=None,
            error_message=f"Failed to resolve sysfs path: {exc}",
            elapsed_seconds=time.monotonic() - start,
        )

    # Verify flashtool.py exists
    flashtool = Path(katapult_dir).expanduser() / "scripts" / "flashtool.py"
    if not flashtool.exists():
        return KatapultCheckResult(
            has_katapult=None,
            error_message=f"Katapult flashtool not found: {flashtool}",
            elapsed_seconds=time.monotonic() - start,
        )

    # Enter bootloader mode
    if log:
        log("Entering bootloader mode...")
    try:
        python_path = _get_python_path()
        result = subprocess.run(
            [python_path, str(flashtool), "-r", "-d", device_path],
            capture_output=True,
            text=True,
            timeout=BOOTLOADER_ENTRY_TIMEOUT,
        )
        if result.returncode != 0:
            return KatapultCheckResult(
                has_katapult=None,
                error_message=result.stderr.strip() or result.stdout.strip(),
                elapsed_seconds=time.monotonic() - start,
            )
    except subprocess.TimeoutExpired:
        return KatapultCheckResult(
            has_katapult=None,
            error_message=f"flashtool.py -r timed out ({BOOTLOADER_ENTRY_TIMEOUT}s)",
            elapsed_seconds=time.monotonic() - start,
        )
    except OSError as exc:
        return KatapultCheckResult(
            has_katapult=None,
            error_message=f"Failed to run flashtool.py: {exc}",
            elapsed_seconds=time.monotonic() - start,
        )

    # Poll for Katapult device
    katapult_pattern = f"usb-katapult_*_{serial_hex}*"
    if log:
        log("Polling for Katapult device...")
    found = _poll_for_serial_device(katapult_pattern)

    if found:
        return KatapultCheckResult(
            has_katapult=True,
            elapsed_seconds=time.monotonic() - start,
        )

    # No Katapult detected -- recover device via USB reset
    if log:
        log("No Katapult detected, recovering device...")
    try:
        _usb_sysfs_reset(authorized_path)
    except (DiscoveryError, OSError) as exc:
        return KatapultCheckResult(
            has_katapult=None,
            error_message=f"USB reset failed: {exc}",
            elapsed_seconds=time.monotonic() - start,
        )

    # Poll for Klipper device return
    recovered = _poll_for_serial_device(serial_pattern)
    if recovered:
        return KatapultCheckResult(
            has_katapult=False,
            elapsed_seconds=time.monotonic() - start,
        )

    return KatapultCheckResult(
        has_katapult=None,
        error_message="Device did not recover after USB reset",
        elapsed_seconds=time.monotonic() - start,
    )


# DEPRECATED: Legacy function retained for backward compatibility with flash.py
def flash_device(
    device_path: str,
    firmware_path: str,
    katapult_dir: str,
    klipper_dir: str,
    timeout: int = TIMEOUT_FLASH,
    preferred_method: str = "katapult",
    allow_fallback: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> FlashResult:
    """Flash firmware to device using Katapult or make flash.

    Tries Katapult flashtool.py first. If that fails, falls back to
    make flash automatically.

    Args:
        device_path: Path to the USB serial device.
        firmware_path: Path to the firmware binary (klipper.bin).
        katapult_dir: Path to the Katapult directory.
        klipper_dir: Path to the Klipper directory.
        timeout: Seconds per flash attempt (applies to each method).
        preferred_method: "katapult" or "make_flash" (default: "katapult").
        allow_fallback: If True, attempt the other method on failure.
        log: Optional callback for progress messages.

    Returns:
        FlashResult with success status, method used, and timing.
    """
    start = time.monotonic()

    method = (preferred_method or "katapult").strip().lower()
    if method not in ("katapult", "make_flash"):
        return FlashResult(
            success=False,
            method=method,
            elapsed_seconds=0.0,
            error_message=f"Unknown flash method: {method}",
        )

    methods = [method]
    if allow_fallback:
        methods.append("make_flash" if method == "katapult" else "katapult")

    last_result: Optional[FlashResult] = None
    for current in methods:
        if current == "katapult":
            result = _try_katapult_flash(device_path, firmware_path, katapult_dir, timeout)
        else:
            result = _try_make_flash(device_path, klipper_dir, timeout)

        last_result = result
        if result.success:
            return result

        # If no fallback, return immediately
        if not allow_fallback or current == methods[-1]:
            break

        if log is not None:
            log(f"{current} failed: {result.error_message}")
            log("Trying fallback method...")

    # If all methods failed, return last result with total elapsed time
    if last_result is None:
        return FlashResult(
            success=False,
            method=method,
            elapsed_seconds=time.monotonic() - start,
            error_message="No flash methods attempted",
        )

    last_result.elapsed_seconds = time.monotonic() - start
    return last_result


# ---------------------------------------------------------------------------
# Phase 45+ Public API: Flash command functions with inherited stdio
# ---------------------------------------------------------------------------


def flash_katapult(
    device_path: str,
    firmware_path: str,
    katapult_dir: str,
    klipper_dir: str = "~/klipper",
    timeout: int = TIMEOUT_FLASH,
) -> FlashResult:
    """Flash firmware using Katapult flashtool.py with inherited stdio.

    Uses klippy-env Python interpreter for pyserial access. Output streams
    to terminal in real-time (no capture).

    Args:
        device_path: Path to the USB serial device.
        firmware_path: Path to the firmware binary (klipper.bin).
        katapult_dir: Path to the Katapult directory.
        klipper_dir: Path to the Klipper directory (for klippy-env python).
        timeout: Seconds before timeout.

    Returns:
        FlashResult with success status and method='katapult'.
    """
    start = time.monotonic()

    # Build path to flashtool.py
    flashtool = Path(katapult_dir).expanduser() / "scripts" / "flashtool.py"

    if not flashtool.exists():
        return FlashResult(
            success=False,
            method="katapult",
            elapsed_seconds=time.monotonic() - start,
            error_message=f"Katapult flashtool not found: {flashtool}",
        )

    try:
        python_path = _get_klippy_env_python(klipper_dir)
        result = subprocess.run(
            [python_path, str(flashtool), "-d", device_path, "-f", firmware_path],
            timeout=timeout,
        )
        elapsed = time.monotonic() - start

        if result.returncode == 0:
            return FlashResult(
                success=True,
                method="katapult",
                elapsed_seconds=elapsed,
            )
        else:
            return FlashResult(
                success=False,
                method="katapult",
                elapsed_seconds=elapsed,
                error_message="flashtool.py returned non-zero exit code",
            )

    except subprocess.TimeoutExpired:
        return FlashResult(
            success=False,
            method="katapult",
            elapsed_seconds=timeout,
            error_message=f"Flash timeout ({timeout}s)",
        )
    except OSError as exc:
        return FlashResult(
            success=False,
            method="katapult",
            elapsed_seconds=time.monotonic() - start,
            error_message=f"Failed to run flashtool.py: {exc}",
        )


def flash_make(
    device_path: str,
    klipper_dir: str,
    timeout: int = TIMEOUT_FLASH,
) -> FlashResult:
    """Flash firmware using make flash with inherited stdio.

    Runs 'make FLASH_DEVICE={device_path} flash' with cwd=klipper_dir.
    Output streams to terminal in real-time (no capture).

    Args:
        device_path: Path to the USB serial device.
        klipper_dir: Path to the Klipper directory.
        timeout: Seconds before timeout.

    Returns:
        FlashResult with success status and method='make_flash'.
    """
    start = time.monotonic()
    klipper_path = Path(klipper_dir).expanduser()

    try:
        result = subprocess.run(
            ["make", f"FLASH_DEVICE={device_path}", "flash"],
            cwd=str(klipper_path),
            timeout=timeout,
        )
        elapsed = time.monotonic() - start

        if result.returncode == 0:
            return FlashResult(
                success=True,
                method="make_flash",
                elapsed_seconds=elapsed,
            )
        else:
            return FlashResult(
                success=False,
                method="make_flash",
                elapsed_seconds=elapsed,
                error_message="make flash returned non-zero exit code",
            )

    except subprocess.TimeoutExpired:
        return FlashResult(
            success=False,
            method="make_flash",
            elapsed_seconds=timeout,
            error_message=f"Flash timeout ({timeout}s)",
        )
    except OSError as exc:
        return FlashResult(
            success=False,
            method="make_flash",
            elapsed_seconds=time.monotonic() - start,
            error_message=f"Failed to run make flash: {exc}",
        )


def flash_sdcard(
    device_path: str,
    firmware_path: str,
    klipper_dir: str,
    board_name: str,
    timeout: int = 120,
) -> FlashResult:
    """Flash firmware using Klipper's flash-sdcard.sh script.

    Runs 'scripts/flash-sdcard.sh {device_path} {board_name}' with cwd=klipper_dir.
    Output streams to terminal in real-time (no capture).

    Args:
        device_path: Path to the USB serial device.
        firmware_path: Path to the firmware binary (unused, script locates it).
        klipper_dir: Path to the Klipper directory.
        board_name: Board identifier for flash-sdcard.sh (e.g., 'btt-octopus-pro-h723-v1.1').
        timeout: Seconds before timeout.

    Returns:
        FlashResult with success status and method='flash_sdcard'.
    """
    start = time.monotonic()

    # Validate board_name
    if not board_name:
        return FlashResult(
            success=False,
            method="flash_sdcard",
            elapsed_seconds=time.monotonic() - start,
            error_message="flash_sdcard requires board_name (sdcard_board not configured)",
        )

    # Build script path
    klipper_path = Path(klipper_dir).expanduser()
    script = klipper_path / "scripts" / "flash-sdcard.sh"

    if not script.exists():
        return FlashResult(
            success=False,
            method="flash_sdcard",
            elapsed_seconds=time.monotonic() - start,
            error_message=f"flash-sdcard.sh not found: {script}",
        )

    try:
        result = subprocess.run(
            [str(script), device_path, board_name],
            cwd=str(klipper_path),
            timeout=timeout,
        )
        elapsed = time.monotonic() - start

        if result.returncode == 0:
            return FlashResult(
                success=True,
                method="flash_sdcard",
                elapsed_seconds=elapsed,
            )
        else:
            err_msg = f"flash_sdcard failed for board {board_name}: non-zero exit code"
            return FlashResult(
                success=False,
                method="flash_sdcard",
                elapsed_seconds=elapsed,
                error_message=err_msg,
            )

    except subprocess.TimeoutExpired:
        return FlashResult(
            success=False,
            method="flash_sdcard",
            elapsed_seconds=timeout,
            error_message=f"flash_sdcard failed for board {board_name}: timeout ({timeout}s)",
        )
    except OSError as exc:
        return FlashResult(
            success=False,
            method="flash_sdcard",
            elapsed_seconds=time.monotonic() - start,
            error_message=f"flash_sdcard failed for board {board_name}: {exc}",
        )


def _find_uf2_mount(uf2_mount_path: Optional[str], username: str) -> Optional[str]:
    """Find UF2 mass storage mount point.

    If uf2_mount_path is specified and exists, returns it directly.
    Otherwise scans common mount locations for RPI-RP2 or RP2040 labels.

    Args:
        uf2_mount_path: Explicit mount path from device config (may be None).
        username: Current username for /media/{user}/ paths.

    Returns:
        Path to UF2 mount point if found, None otherwise.
    """
    # Use explicit path if provided and exists
    if uf2_mount_path and os.path.isdir(uf2_mount_path):
        return uf2_mount_path

    # Common mount point patterns for RPI-RP2 and RP2040 labels
    labels = ["RPI-RP2", "RP2040"]
    paths = []
    for label in labels:
        paths.extend(
            [
                f"/media/{username}/{label}",
                f"/media/{label}",
                f"/run/media/{username}/{label}",
                f"/mnt/{label}",
            ]
        )

    for path in paths:
        if os.path.isdir(path):
            return path

    return None


def flash_uf2(
    firmware_path: str,
    mount_path: Optional[str],
    timeout: int = 15,
) -> FlashResult:
    """Flash firmware by copying UF2 file to mass storage mount.

    Polls for UF2 mount point, then copies firmware file. RP2040 auto-reboots
    on valid UF2 write, so no explicit unmount is performed.

    Args:
        firmware_path: Path to the firmware file (klipper.uf2).
        mount_path: Explicit mount path from device config (may be None for auto-detect).
        timeout: Seconds to poll for mount point (default 15).

    Returns:
        FlashResult with success status and method='uf2_mount'.
    """
    import getpass
    import shutil

    start = time.monotonic()
    username = getpass.getuser()

    # Poll for UF2 mount point
    deadline = time.monotonic() + max(0, float(timeout))
    uf2_mount = None
    while time.monotonic() < deadline:
        uf2_mount = _find_uf2_mount(mount_path, username)
        if uf2_mount:
            break
        time.sleep(0.5)

    if not uf2_mount:
        return FlashResult(
            success=False,
            method="uf2_mount",
            elapsed_seconds=time.monotonic() - start,
            error_message="UF2 mass storage not found (RPI-RP2 not mounted)",
        )

    # Verify firmware file exists
    if not os.path.isfile(firmware_path):
        return FlashResult(
            success=False,
            method="uf2_mount",
            elapsed_seconds=time.monotonic() - start,
            error_message=f"Firmware file not found: {firmware_path}",
        )

    # Copy firmware to mount point
    try:
        filename = os.path.basename(firmware_path)
        dest_path = os.path.join(uf2_mount, filename)
        shutil.copy(firmware_path, dest_path)
        print(f"Copied {filename} to {uf2_mount}")
        elapsed = time.monotonic() - start

        return FlashResult(
            success=True,
            method="uf2_mount",
            elapsed_seconds=elapsed,
        )

    except OSError as exc:
        return FlashResult(
            success=False,
            method="uf2_mount",
            elapsed_seconds=time.monotonic() - start,
            error_message=f"Failed to copy firmware to UF2 mount: {exc}",
        )


def flash_katapult_can(
    uuid: str,
    interface: str,
    firmware_path: str,
    katapult_dir: str,
    timeout: int = TIMEOUT_CAN_FLASH,
    max_retries: int = CAN_RETRY_ATTEMPTS,
) -> FlashResult:
    """Flash firmware using Katapult flashtool.py over CAN bus with retry logic.

    Uses ``python3`` (not klippy-env) since CAN mode in flashtool.py uses
    only stdlib (raw CAN sockets).  Retries on non-zero exit codes (transient
    CAN bus errors) but breaks immediately on OSError (not transient).

    Args:
        uuid: CAN bus UUID (12-char hex) of the target device.
        interface: CAN interface name (e.g., ``"can0"``).
        firmware_path: Path to the firmware binary (klipper.bin).
        katapult_dir: Path to the Katapult directory.
        timeout: Seconds before timeout per attempt.
        max_retries: Maximum number of flash attempts.

    Returns:
        FlashResult with success status and method='katapult_can'.
    """
    start = time.monotonic()

    # Build path to flashtool.py
    flashtool = Path(katapult_dir).expanduser() / "scripts" / "flashtool.py"

    if not flashtool.exists():
        return FlashResult(
            success=False,
            method="katapult_can",
            elapsed_seconds=time.monotonic() - start,
            error_message=f"Katapult flashtool not found: {flashtool}",
        )

    last_error = ""
    for attempt in range(1, max_retries + 1):
        try:
            result = subprocess.run(
                [
                    "python3",
                    str(flashtool),
                    "-i",
                    interface,
                    "-u",
                    uuid,
                    "-f",
                    firmware_path,
                ],
                timeout=timeout,
            )

            if result.returncode == 0:
                return FlashResult(
                    success=True,
                    method="katapult_can",
                    elapsed_seconds=time.monotonic() - start,
                )

            last_error = "flashtool.py returned non-zero exit code"

        except subprocess.TimeoutExpired:
            last_error = f"CAN flash timeout ({timeout}s)"

        except OSError as exc:
            last_error = f"Failed to run flashtool.py: {exc}"
            break  # OSError is not transient -- don't retry

        # Pause between retries
        if attempt < max_retries:
            time.sleep(2.0)

    return FlashResult(
        success=False,
        method="katapult_can",
        elapsed_seconds=time.monotonic() - start,
        error_message=f"{last_error} (after {max_retries} attempts)",
    )


def execute_flash(
    entry: DeviceEntry,
    device_path: str,
    firmware_path: str,
    config: GlobalConfig,
    timeout: int = TIMEOUT_FLASH,
) -> FlashResult:
    """Dispatch flash operation to the correct method based on entry.flash_command.

    Routes to flash_katapult, flash_katapult_can, flash_make, flash_sdcard, or
    flash_uf2 based on the device's configured flash_command. No fallback chain
    -- flash_command must be explicitly set per device.

    Args:
        entry: DeviceEntry with flash_command and optional method-specific fields.
        device_path: Path to the USB serial device.
        firmware_path: Path to the firmware file.
        config: GlobalConfig with klipper_dir, katapult_dir paths.
        timeout: Seconds before timeout (applies to katapult and make_flash).

    Returns:
        FlashResult with success status and method name.
    """
    flash_command = entry.flash_command

    # No flash_command configured
    if flash_command is None:
        return FlashResult(
            success=False,
            method="unknown",
            elapsed_seconds=0.0,
            error_message="No flash_command configured for device",
        )

    # Dispatch to appropriate flash function
    if flash_command == "katapult":
        return flash_katapult(
            device_path=device_path,
            firmware_path=firmware_path,
            katapult_dir=config.katapult_dir,
            klipper_dir=config.klipper_dir,
            timeout=timeout,
        )

    if flash_command == "make_flash":
        if entry.is_can_device:
            return FlashResult(
                success=False,
                method="make_flash",
                elapsed_seconds=0.0,
                error_message="CAN devices cannot use make_flash; configure Katapult CAN",
            )
        return flash_make(
            device_path=device_path,
            klipper_dir=config.klipper_dir,
            timeout=timeout,
        )

    if flash_command == "flash_sdcard":
        return flash_sdcard(
            device_path=device_path,
            firmware_path=firmware_path,
            klipper_dir=config.klipper_dir,
            board_name=entry.sdcard_board or "",
            timeout=120,
        )

    if flash_command == "uf2_mount":
        return flash_uf2(
            firmware_path=firmware_path,
            mount_path=entry.uf2_mount_path,
            timeout=15,
        )

    if flash_command == "katapult_can":
        if not entry.is_can_device:
            return FlashResult(
                success=False,
                method="katapult_can",
                elapsed_seconds=0.0,
                error_message="katapult_can requires a CAN device UUID configuration",
            )
        uuid = (entry.canbus_uuid or "").strip()
        if not uuid:
            return FlashResult(
                success=False,
                method="katapult_can",
                elapsed_seconds=0.0,
                error_message="katapult_can requires a non-empty CAN UUID",
            )
        return flash_katapult_can(
            uuid=uuid,
            interface=entry.canbus_interface or "can0",
            firmware_path=firmware_path,
            katapult_dir=config.katapult_dir,
            timeout=TIMEOUT_CAN_FLASH,
        )

    # Unknown flash command
    return FlashResult(
        success=False,
        method=flash_command,
        elapsed_seconds=0.0,
        error_message=f"Unknown flash command: {flash_command}",
    )
