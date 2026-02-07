#!/usr/bin/env python3
"""TUI entry point for kalico-flash.

Launches an interactive terminal menu for building and flashing Klipper
firmware to USB-connected MCU boards. Requires an interactive terminal (TTY).

Core logic lives in:
    - registry.py: Device registry persistence
    - discovery.py: USB device scanning and matching
    - output.py: Pluggable output interface (CLI, future Moonraker)
    - models.py: Dataclass contracts for cross-module data
    - errors.py: Exception hierarchy
    - config.py: Kconfig caching and MCU parsing
    - build.py: Menuconfig and firmware compilation
    - service.py: Klipper service lifecycle management
    - flasher.py: Dual-method flash operations
    - tui.py: Interactive menu
"""

from __future__ import annotations

import fnmatch
import shutil
import sys
from pathlib import Path

# Python version guard
if sys.version_info < (3, 9):
    sys.exit("Error: kalico-flash requires Python 3.9 or newer.")


VERSION = "0.1.0"
SUSPICIOUS_FIRMWARE_SIZE_BYTES = 16 * 1024

DEFAULT_BLOCKED_DEVICES = [
    ("usb-beacon_*", "Beacon probe (not a Klipper MCU)"),
]


def _check_duplicate_path(real_path: str, used_paths: set[str]) -> bool:
    """Check if a resolved USB path was already targeted.

    Returns True if duplicate (path already in used_paths).
    Adds path to used_paths if not a duplicate.
    """
    if real_path in used_paths:
        return True
    used_paths.add(real_path)
    return False


def _normalize_pattern(pattern: str) -> str:
    return pattern.strip().lower()


def _build_blocked_list(registry_data) -> list[tuple[str, str | None]]:
    blocked = [(pattern, reason) for pattern, reason in DEFAULT_BLOCKED_DEVICES]
    for entry in getattr(registry_data, "blocked_devices", []):
        blocked.append((entry.pattern, entry.reason))
    return blocked


def _blocked_reason_for_filename(
    filename: str, blocked_list: list[tuple[str, str | None]]
) -> str | None:
    name = filename.lower()
    for pattern, reason in blocked_list:
        if fnmatch.fnmatch(name, _normalize_pattern(pattern)):
            return reason or "Blocked by policy"
    return None


def _blocked_reason_for_entry(entry, blocked_list: list[tuple[str, str | None]]) -> str | None:
    # CAN devices have no serial_pattern -- they cannot match USB blocked patterns
    if entry.serial_pattern is None:
        return None
    serial_pattern = entry.serial_pattern.lower()
    for pattern, reason in blocked_list:
        normalized = _normalize_pattern(pattern)
        if fnmatch.fnmatch(serial_pattern, normalized) or fnmatch.fnmatch(
            normalized, serial_pattern
        ):
            return reason or "Blocked by policy"
    from .discovery import SUPPORTED_PREFIXES

    if not any(serial_pattern.startswith(prefix) for prefix in SUPPORTED_PREFIXES):
        return "Unsupported USB device"
    return None


def _short_path(path_value: str) -> str:
    """Return filename-only for /dev/serial/by-id paths."""
    try:
        return Path(path_value).name
    except (TypeError, ValueError):
        return path_value


def _emit_preflight(out, errors: list[str], warnings: list[str]) -> bool:
    """Emit preflight warnings/errors. Returns True if no errors."""
    for warning in warnings:
        out.warn(f"Preflight: {warning}")

    if errors:
        out.error("Preflight checks failed:")
        for err in errors:
            out.error(f"  - {err}")
        return False
    return True


def _check_firmware_artifact(
    firmware_path: str | None,
    firmware_size: int | None,
) -> tuple[str | None, str | None]:
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


def _preflight_build(out, klipper_dir: str) -> bool:
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

    return _emit_preflight(out, errors, warnings)


def _preflight_flash(
    out,
    klipper_dir: str,
    katapult_dir: str,
    flash_command: str,
) -> bool:
    """Validate flash prerequisites for the selected flash command."""
    if not _preflight_build(out, klipper_dir):
        return False

    errors: list[str] = []
    warnings: list[str] = []

    method = (flash_command or "").strip().lower()
    if not method:
        errors.append("Missing flash command")
        return _emit_preflight(out, errors, warnings)
    if method not in ("katapult", "make_flash", "flash_sdcard", "uf2_mount", "katapult_can"):
        errors.append(f"Unknown flash command: {method}")
        return _emit_preflight(out, errors, warnings)

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

    return _emit_preflight(out, errors, warnings)


def _get_device_flash_config_issue(entry) -> tuple[str, str] | None:
    """Return the first flash configuration issue for a device, or None."""
    from .validation import (
        find_flash_method_pair,
        validate_bootloader_baud,
        validate_bootloader_flash_pair,
        validate_can_interface,
        validate_canbus_uuid,
        validate_transport_fields,
    )

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


def _validate_device_flash_config(entry, out) -> bool:
    """Validate device has required flash configuration."""
    issue = _get_device_flash_config_issue(entry)
    if issue is None:
        return True

    error_type, detail = issue
    out.error_with_recovery(
        error_type,
        f"Device '{entry.name}' {detail}",
        context={"device": entry.key},
        recovery="Run Config Device (E) to fix configuration",
    )
    return False


def _remove_cached_config(
    device_key: str, out, prompt: bool = True, device_name: str | None = None
) -> None:
    """Remove cached config directory for a device key."""
    from .config import get_config_dir

    config_dir = get_config_dir(device_key)
    if not config_dir.exists():
        return

    should_remove = True
    if prompt:
        label = device_name or device_key
        should_remove = out.confirm(f"Also remove cached config for '{label}'?", default=False)

    if not should_remove:
        out.info("Registry", "Cached config kept")
        return

    try:
        shutil.rmtree(config_dir)
        out.success("Cached config removed")
    except OSError as exc:
        out.warn(f"Failed to remove cached config: {exc}")


def cmd_build(registry, device_key: str, out) -> int:
    """Build firmware for a registered device.

    Orchestrates: load cached config -> menuconfig -> save config -> MCU validation -> build
    """
    # Late imports for fast startup
    from .build import run_build, run_menuconfig
    from .config import ConfigManager
    from .errors import ERROR_TEMPLATES, ConfigError

    # Load device entry
    entry = registry.get(device_key)
    if entry is None:
        from .errors import get_recovery_text

        template = ERROR_TEMPLATES["device_not_registered"]
        out.error_with_recovery(
            template["error_type"],
            template["message_template"].format(device=device_key),
            context={"device": device_key},
            recovery=get_recovery_text("device_not_registered"),
        )
        return 1

    # Load global config for klipper_dir
    data = registry.load()
    if data.global_config is None:
        out.error("Global config not set. Press A to add a device first.")
        return 1

    klipper_dir = data.global_config.klipper_dir
    out.info("Build", f"Building firmware for {entry.name} ({entry.mcu})")

    if not _preflight_build(out, klipper_dir):
        return 1

    # Initialize config manager
    config_mgr = ConfigManager(device_key, klipper_dir)

    # Step 1: Load cached config (if exists)
    if config_mgr.load_cached_config():
        out.info("Config", f"Loaded cached config for '{entry.name}'")
    else:
        config_mgr.clear_klipper_config()
        out.info("Config", "No cached config found, starting fresh")

    # Step 2: Run menuconfig
    out.info("Config", "Launching menuconfig...")
    ret_code, was_saved = run_menuconfig(klipper_dir, str(config_mgr.klipper_config_path))

    if ret_code != 0:
        template = ERROR_TEMPLATES["menuconfig_failed"]
        out.error_with_recovery(
            template["error_type"],
            template["message_template"],
            context={"device": device_key},
            recovery=template["recovery_template"],
        )
        return 1

    if not was_saved:
        out.warn("Config was not saved in menuconfig")
        if not out.confirm("Continue build anyway?"):
            out.info("Build", "Cancelled")
            return 0

    # Step 3: Save config to cache
    try:
        config_mgr.save_cached_config()
        out.info("Config", f"Cached config for '{entry.name}'")
    except ConfigError as e:
        out.error_with_recovery(
            "Config error",
            f"Failed to cache config: {e}",
            context={"device": device_key},
            recovery=(
                "1. Verify Klipper directory is writable\n2. Check disk space\n3. Re-run menuconfig"
            ),
        )
        return 1

    # Step 4: MCU validation
    try:
        is_match, actual_mcu = config_mgr.validate_mcu(entry.mcu)
        if not is_match:
            template = ERROR_TEMPLATES["mcu_mismatch"]
            out.error_with_recovery(
                template["error_type"],
                template["message_template"].format(actual=actual_mcu, expected=entry.mcu),
                context={
                    "device": device_key,
                    "expected": entry.mcu,
                    "actual": actual_mcu,
                },
                recovery=template["recovery_template"],
            )
            return 1
        out.info("Config", f"MCU validated: {actual_mcu}")
    except ConfigError as e:
        out.error_with_recovery(
            "Config error",
            f"MCU validation failed: {e}",
            context={"device": device_key},
            recovery=(
                "1. Run menuconfig and verify MCU selection\n"
                "2. Check .config file exists\n"
                "3. Ensure CONFIG_MCU is set"
            ),
        )
        return 1

    # Step 5: Build
    out.info("Build", "Running make clean + make...")
    result = run_build(klipper_dir)

    if not result.success:
        template = ERROR_TEMPLATES["build_failed"]
        out.error_with_recovery(
            template["error_type"],
            template["message_template"].format(device=device_key),
            context={"device": device_key},
            recovery=template["recovery_template"],
        )
        return 1

    # Success
    size_kb = result.firmware_size / 1024 if result.firmware_size else 0
    out.success(
        f"Build complete: {result.firmware_path} ({size_kb:.1f} KB) "
        f"in {result.elapsed_seconds:.1f}s"
    )
    return 0


def _prompt_ccache_install(out) -> str:
    """Prompt for ccache installation choice.

    Returns: "install", "skip", or "disable"
    """
    out.info("", "  1. Install ccache now (requires sudo)")
    out.info("", "  2. Skip for this build")
    out.info("", "  3. Disable ccache setting")

    for _ in range(3):
        try:
            choice = input("  Choice [1/2/3]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return "skip"

        choice_map = {"1": "install", "2": "skip", "3": "disable"}
        if choice in choice_map:
            return choice_map[choice]
        out.warn("Invalid choice. Enter 1, 2, or 3.")

    return "skip"  # Default after max attempts


def _run_ccache_install(out) -> bool:
    """Run apt install ccache with inherited stdio.

    Returns True if install succeeded, False otherwise.
    """
    import subprocess as _subprocess

    out.phase("Install", "Installing ccache...")
    try:
        result = _subprocess.run(
            ["sudo", "apt", "install", "-y", "ccache"],
            timeout=120,
        )
        if result.returncode == 0:
            out.success("ccache installed successfully")
            return True
        else:
            out.error(f"apt install failed with exit code {result.returncode}")
            return False
    except _subprocess.TimeoutExpired:
        out.error("apt install timed out")
        return False
    except Exception as e:
        out.error(f"Installation failed: {e}")
        return False


def _resolve_ccache_usage(
    out,
    registry,
    global_config,
    *,
    is_available_fn=None,
    prompt_fn=None,
    install_fn=None,
) -> bool:
    """Resolve whether to use ccache for this build, prompting if needed."""
    import dataclasses

    use_ccache = global_config.use_ccache
    if not use_ccache:
        return False

    if is_available_fn is None:
        from .ccache import is_ccache_available as is_available_fn
    if prompt_fn is None:
        prompt_fn = _prompt_ccache_install
    if install_fn is None:
        install_fn = _run_ccache_install

    if use_ccache and not is_available_fn() and not global_config.ccache_install_declined:
        out.phase("Build", "ccache not found. Install ccache for faster builds?")
        choice = prompt_fn(out)
        if choice == "install":
            if install_fn(out):
                new_gc = dataclasses.replace(
                    global_config, use_ccache=True, ccache_install_declined=False
                )
                registry.save_global(new_gc)
                use_ccache = True
            else:
                use_ccache = False  # Install failed, skip ccache for this build
        elif choice == "disable":
            new_gc = dataclasses.replace(global_config, use_ccache=False)
            registry.save_global(new_gc)
            use_ccache = False
        elif choice == "skip":
            new_gc = dataclasses.replace(global_config, ccache_install_declined=True)
            registry.save_global(new_gc)
            use_ccache = False
        out.step_divider()

    return use_ccache


# --- Sub-field configuration for paired flash method picker ---

SUB_FIELD_DEFAULTS: dict[str, object] = {
    "bootloader_baud": 250000,
}

SUB_FIELD_PROMPTS: dict[str, str] = {
    "bootloader_baud": "Bootloader baud rate",
    "uf2_mount_path": "UF2 mount path (e.g., /media/RPI-RP2)",
    "sdcard_board": "SD card board name (from flash-sdcard.sh supported boards)",
    "canbus_uuid": "CAN bus UUID (12-char hex)",
    "canbus_interface": "CAN interface name (e.g., can0)",
}


def _validate_bootloader_baud(value: str) -> tuple[bool, str]:
    """Validate bootloader baud rate string."""
    if not value.isdigit():
        return (False, "Must be a number")
    from .validation import validate_bootloader_baud

    return validate_bootloader_baud(int(value))


def _validate_canbus_uuid(value: str) -> tuple[bool, str]:
    """Validate CAN bus UUID string."""
    from .validation import validate_canbus_uuid

    return validate_canbus_uuid(value)


def _validate_canbus_interface(value: str) -> tuple[bool, str]:
    """Validate CAN interface name string."""
    from .validation import validate_can_interface

    return validate_can_interface(value)


SUB_FIELD_VALIDATORS: dict[str, object] = {
    "bootloader_baud": _validate_bootloader_baud,
    "canbus_uuid": _validate_canbus_uuid,
    "canbus_interface": _validate_canbus_interface,
}


def _prompt_required_field(field_name: str, out, validator=None) -> str | None:
    """Prompt for a required field value with optional validation.

    Unlike the deleted _prompt_conditional_field, empty input re-prompts
    rather than returning None. Only returns None on KeyboardInterrupt/EOFError
    (wizard cancellation).

    Args:
        field_name: Human-readable field name for prompt
        out: Output interface
        validator: Optional callable(value) -> (bool, error_msg)

    Returns:
        Validated value string, or None if user cancels (Ctrl+C/EOF)
    """
    while True:
        try:
            raw = out.prompt(field_name).strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if not raw:
            out.warn(f"{field_name} is required.")
            continue

        if validator:
            is_valid, error_msg = validator(raw)
            if not is_valid:
                out.warn(error_msg)
                continue

        return raw


def cmd_flash(registry, device_key, out, skip_menuconfig: bool = False) -> int:
    """Build and flash firmware for a registered device.

    Orchestrates the full workflow:
    1. [Discovery] Scan USB devices, select target
    2. [Config] Load/edit menuconfig, validate MCU
    3. [Build] Compile firmware with timeout
    4. [Flash] Stop Klipper, flash device, restart Klipper

    Args:
        registry: Registry instance for device lookup
        device_key: Device key to flash (None for interactive selection)
        out: Output interface for user messages
        skip_menuconfig: If True, skip menuconfig when cached config exists

    Returns:
        0 on success, 1 on failure
    """
    import time

    from .build import TIMEOUT_BUILD, run_build, run_menuconfig
    from .config import ConfigManager

    # Late imports for fast startup
    from .discovery import (
        extract_mcu_from_serial,
        find_registered_devices,
        is_supported_device,
        match_device,
        match_devices,
        preflight_can_interface,
        scan_serial_devices,
        verify_can_device_after_flash,
    )
    from .errors import ERROR_TEMPLATES, ConfigError, DiscoveryError
    from .flasher import TIMEOUT_CAN_FLASH, TIMEOUT_FLASH, execute_flash, verify_device_path
    from .service import (
        _is_service_active,
        acquire_sudo,
        klipper_service_stopped,
        verify_passwordless_sudo,
    )
    from .tui import _get_menu_choice, wait_for_device

    # TTY check for interactive mode
    if device_key is None and not sys.stdin.isatty():
        out.error("Interactive terminal required. Run from SSH terminal.")
        return 1

    # Load registry data
    data = registry.load()
    if data.global_config is None:
        out.error("Global config not set. Press A to add a device first.")
        return 1
    blocked_list = _build_blocked_list(data)

    # Fetch version information early for display in device selection
    from .moonraker import (
        get_host_klipper_version,
        get_mcu_version_for_device,
        get_mcu_versions,
        get_print_status,
        is_mcu_outdated,
    )

    mcu_versions = get_mcu_versions()
    host_version = get_host_klipper_version(data.global_config.klipper_dir)

    # === Phase 1: Discovery ===
    out.phase("Discovery", "Scanning for USB devices...")
    usb_devices = scan_serial_devices()
    duplicate_matches: dict[str, list] = {}
    for entry in data.devices.values():
        if entry.serial_pattern is None:
            continue  # CAN devices have no USB serial pattern
        matches = match_devices(entry.serial_pattern, usb_devices)
        if len(matches) > 1:
            duplicate_matches[entry.key] = matches
    blocked_entries: dict[str, str] = {}
    for entry in data.devices.values():
        reason = _blocked_reason_for_entry(entry, blocked_list)
        if reason:
            blocked_entries[entry.key] = reason

    if device_key is None:
        # Interactive mode: select from connected registered devices
        if not usb_devices:
            out.error("No USB devices found. Connect a board and try again.")
            return 1

        # Cross-reference with registry
        matched, unmatched = find_registered_devices(usb_devices, data.devices)

        # Remove any entries with duplicate USB IDs or blocked status from selectable list
        if duplicate_matches:
            matched = [(e, d) for e, d in matched if e.key not in duplicate_matches]
        if blocked_entries:
            matched = [(e, d) for e, d in matched if e.key not in blocked_entries]

        if not matched:
            if duplicate_matches:
                out.error_with_recovery(
                    "Duplicate USB IDs",
                    "Registered device(s) match multiple connected USB IDs",
                    recovery=(
                        "1. Unplug duplicate devices so only one remains\n"
                        "2. Reconnect and retry\n"
                        "3. If duplicates persist, update registry to unique devices"
                    ),
                )
                out.phase("Discovery", "Blocked devices with duplicate USB IDs:")
                for key, devices in duplicate_matches.items():
                    entry = data.devices.get(key)
                    if entry is None:
                        continue
                    details = ", ".join(d.filename for d in devices)
                    out.device_line("DUP", f"{entry.name} ({entry.mcu}) [duplicate]", details)
                return 1

            if blocked_entries:
                blocked_connected = [
                    (entry, device)
                    for entry, device in find_registered_devices(usb_devices, data.devices)[0]
                    if entry.key in blocked_entries
                ]
                if blocked_connected:
                    out.error_with_recovery(
                        "Blocked devices",
                        "Connected registered devices are blocked and cannot be flashed",
                        recovery=(
                            "1. Remove blocked entries from devices.json\n"
                            "2. Or connect a flashable device"
                        ),
                    )
                    out.phase("Discovery", "Blocked registered devices:")
                    for entry, _device in blocked_connected:
                        reason = blocked_entries.get(entry.key, "Blocked by policy")
                        out.device_line("BLK", f"{entry.name} ({entry.mcu}) [blocked]", reason)
                    return 1

            recovery = (
                "1. Press D to refresh devices\n"
                "2. Check USB connections\n"
                "3. Press A to add a device"
            )
            out.error_with_recovery(
                "Device not found",
                "No registered devices connected",
                recovery=recovery,
            )
            out.phase("Discovery", "Found USB devices but none are registered:")
            for device in usb_devices:
                blocked_reason = _blocked_reason_for_filename(device.filename, blocked_list)
                if blocked_reason or not is_supported_device(device.filename):
                    out.device_line(
                        "BLK",
                        device.filename,
                        blocked_reason or "Unsupported USB device",
                    )
                else:
                    out.device_line("NEW", device.filename, "Unregistered device")
            return 1

        if duplicate_matches:
            out.phase("Discovery", "Blocked devices with duplicate USB IDs:")
            for key, devices in duplicate_matches.items():
                entry = data.devices.get(key)
                if entry is None:
                    continue
                details = ", ".join(d.filename for d in devices)
                out.device_line("DUP", f"{entry.name} ({entry.mcu}) [duplicate]", details)

        # Filter to only flashable devices for selection
        flashable_matched = [(e, d) for e, d in matched if e.flashable]
        excluded_matched = [(e, d) for e, d in matched if not e.flashable]

        # Show excluded devices with note if any
        if excluded_matched:
            out.phase("Discovery", "Excluded devices (not selectable):")
            for entry, device in excluded_matched:
                out.device_line("REG", f"{entry.name} ({entry.mcu}) [excluded]", device.filename)

        if blocked_entries:
            blocked_connected = [
                (entry, device)
                for entry, device in find_registered_devices(usb_devices, data.devices)[0]
                if entry.key in blocked_entries
            ]
            if blocked_connected:
                out.phase("Discovery", "Blocked devices (not selectable):")
                for entry, _device in blocked_connected:
                    reason = blocked_entries.get(entry.key, "Blocked by policy")
                    out.device_line("BLK", f"{entry.name} ({entry.mcu}) [blocked]", reason)

        if not flashable_matched:
            from .errors import get_recovery_text

            template = ERROR_TEMPLATES["device_excluded"]
            out.error_with_recovery(
                template["error_type"],
                "All connected devices are excluded from flashing",
                recovery=get_recovery_text("device_excluded"),
            )
            return 1

        # Show numbered list of connected flashable devices
        out.phase("Discovery", f"Found {len(flashable_matched)} flashable device(s):")
        for i, (entry, device) in enumerate(flashable_matched):
            out.device_line(str(i + 1), f"{entry.name} ({entry.mcu})", device.filename)
            # Show MCU software version if available
            if mcu_versions:
                version = get_mcu_version_for_device(
                    entry.mcu,
                    device_name=entry.name,
                    device_key=entry.key,
                    mcu_name=entry.mcu_name,
                    allow_fuzzy_fallback=True,
                )
                if version:
                    out.info("", f"     MCU software version: {version}")

        # Show host Klipper version before selection
        if host_version:
            from .moonraker import detect_firmware_flavor

            out.info("Version", f"Host: {detect_firmware_flavor(host_version)} {host_version}")

        # Single device: auto-select with confirmation
        if len(flashable_matched) == 1:
            entry, usb_device = flashable_matched[0]
            if out.confirm(f"Flash {entry.name}?", default=True):
                device_key = entry.key
                device_path = usb_device.path
            else:
                out.phase("Discovery", "Cancelled")
                return 0
        else:
            # Multiple devices: prompt for selection
            choices = ["0"] + [str(i) for i in range(1, len(flashable_matched) + 1)]
            choice = _get_menu_choice(
                choices,
                out,
                max_attempts=3,
                prompt="Select device number (0/q to cancel): ",
            )
            if choice is None or choice == "0":
                out.phase("Discovery", "Cancelled")
                return 0
            idx = int(choice) - 1
            entry, usb_device = flashable_matched[idx]
            device_key = entry.key
            device_path = usb_device.path
    else:
        # Verify device exists and is connected
        entry = registry.get(device_key)
        if entry is None:
            from .errors import get_recovery_text

            template = ERROR_TEMPLATES["device_not_registered"]
            out.error_with_recovery(
                template["error_type"],
                template["message_template"].format(device=device_key),
                context={"device": device_key},
                recovery=get_recovery_text("device_not_registered"),
            )
            return 1

        blocked_reason = _blocked_reason_for_entry(entry, blocked_list)
        if blocked_reason:
            out.error_with_recovery(
                "Device blocked",
                f"Device '{entry.name}' is blocked: {blocked_reason}",
                context={"device": device_key},
                recovery=(
                    "1. Remove the device from blocked_devices in devices.json\n"
                    "2. Or update the device serial pattern to a supported target"
                ),
            )
            return 1

        # Check if device is excluded from flashing
        if not entry.flashable:
            recovery_msg = (
                f"The device '{entry.name}' is excluded from flashing. "
                "Re-include it from the device list."
            )
            out.error_with_recovery(
                "Device excluded",
                device_key,
                {"device": device_key, "name": entry.name},
                recovery_msg,
            )
            return 1

        # Find device path (USB) or validate CAN interface (CAN)
        if entry.is_can_device:
            # CAN pre-flight check replaces USB device matching
            ok, error = preflight_can_interface(entry.canbus_interface or "can0")
            if not ok:
                out.error_with_recovery(
                    "CAN interface error",
                    error,
                    context={"device": device_key, "interface": entry.canbus_interface},
                    recovery="Check CAN adapter connection and interface configuration",
                )
                return 1
            usb_device = None  # CAN has no USB device
            device_path = None  # CAN has no device_path
        else:
            # USB device matching
            # Block if this device matches multiple USB IDs
            if device_key in duplicate_matches:
                out.error_with_recovery(
                    "Duplicate USB IDs",
                    f"Device '{entry.name}' matches multiple connected USB IDs",
                    context={"device": device_key},
                    recovery=(
                        "1. Unplug duplicate devices so only one remains\n"
                        "2. Reconnect and retry\n"
                        "3. If duplicates persist, update registry to unique devices"
                    ),
                )
                for device in duplicate_matches[device_key]:
                    out.device_line("DUP", device.filename, "Duplicate USB ID")
                return 1

            # Find matching USB device
            usb_device = match_device(entry.serial_pattern, usb_devices)
            if usb_device is None:
                from .errors import get_recovery_text

                template = ERROR_TEMPLATES["device_not_connected"]
                out.error_with_recovery(
                    template["error_type"],
                    template["message_template"].format(device=device_key),
                    context={"device": device_key},
                    recovery=get_recovery_text("device_not_connected"),
                )
                return 1

            device_path = usb_device.path

    # === MCU Cross-Check (SAFE-03) ===
    if not entry.is_can_device:
        usb_mcu = extract_mcu_from_serial(usb_device.filename)
        if usb_mcu is not None and usb_mcu.lower() != entry.mcu.lower():
            out.warn(f"MCU mismatch: USB device reports '{usb_mcu}' but registry has '{entry.mcu}'")
            if not out.confirm("Continue with flash anyway?", default=False):
                return 0

    # Load the device entry for the rest of the workflow
    entry = registry.get(device_key)
    global_config = data.global_config
    klipper_dir = global_config.klipper_dir
    katapult_dir = global_config.katapult_dir

    # Validate flash configuration (no fallback chain per user decision)
    if not _validate_device_flash_config(entry, out):
        return 1

    # Preflight for selected flash command (no fallback)
    if not _preflight_flash(out, klipper_dir, katapult_dir, entry.flash_command):
        return 1

    # Target display
    if entry.is_can_device:
        can_iface = entry.canbus_interface or 'can0'
        out.phase(
            "Discovery",
            f"Target: {entry.name} ({entry.mcu}) via CAN"
            f" {can_iface} [{entry.canbus_uuid}]",
        )
    else:
        out.phase("Discovery", f"Target: {entry.name} ({entry.mcu}) at {_short_path(device_path)}")

    out.step_divider()

    # === Moonraker Safety Check ===
    from .safety import should_block_on_printer_state

    print_status = get_print_status()

    if print_status is None:
        # Moonraker unreachable - warn and require confirmation
        out.warn("Moonraker unreachable - print status and version check unavailable")
        if not out.confirm("Continue without safety checks?", default=False):
            out.phase("Flash", "Cancelled")
            return 0
    else:
        state = (print_status.state or "").lower()
        if state == "error":
            out.warn("Printer is in 'error' state. Flashing may be needed to recover.")
            if not out.confirm("Continue flashing despite printer error state?", default=False):
                out.phase("Flash", "Cancelled")
                return 0
        elif should_block_on_printer_state(state):
            # Block flash during unsafe printer states
            if state in ("printing", "paused"):
                progress_pct = int(print_status.progress * 100)
                filename = print_status.filename or "unknown"
                detail = f"Print in progress: {filename} ({progress_pct}%)"
            else:
                detail = f"Printer is in '{print_status.state}' state"
            out.error_with_recovery(
                "Printer busy",
                detail,
                recovery=(
                    "1. Wait for printer to reach 'ready' state\n"
                    "2. Or cancel print in Fluidd/Mainsail dashboard\n"
                    "3. Then re-run flash command"
                ),
            )
            return 1
        else:
            # Safe state - show status and continue
            out.phase("Safety", f"Printer state: {print_status.state} - OK to flash")

    out.step_divider()

    # === Version Information ===
    # mcu_versions and host_version already fetched earlier for device selection display

    if host_version:
        from .moonraker import detect_firmware_flavor

        out.phase("Version", f"Host: {detect_firmware_flavor(host_version)} {host_version}")

        if mcu_versions:
            # Display all MCU versions, mark target with asterisk
            # Map device MCU type to Moonraker MCU name by checking mcu_constants
            target_mcu = None
            # Check device name/key against Moonraker MCU names first
            friendly = [n for n in mcu_versions if not any(c.isdigit() for c in n)]
            chip_keys = [n for n in mcu_versions if any(c.isdigit() for c in n)]
            for candidate in (entry.name, entry.key):
                if not candidate:
                    continue
                cl = candidate.lower()
                for mcu_name in friendly:
                    nl = mcu_name.lower()
                    if nl == cl or nl in cl or cl in nl:
                        target_mcu = mcu_name
                        break
                if target_mcu:
                    break
            # Fall back to chip-type matching
            if target_mcu is None:
                for mcu_name in friendly + chip_keys:
                    if (
                        entry.mcu.lower() in mcu_name.lower()
                        or mcu_name.lower() in entry.mcu.lower()
                    ):
                        target_mcu = mcu_name
                        break
            # If no match found by name, use "main" as default for primary MCU
            if target_mcu is None and "main" in mcu_versions:
                target_mcu = "main"

            # Build set of friendly names (no digits = not a chip-type alias)
            friendly_names = {n for n in mcu_versions if not any(c.isdigit() for c in n)}
            # If target matched a chip-type alias, find the friendly name
            # with the same version so we can mark it with [*]
            display_target = target_mcu
            if target_mcu and target_mcu not in friendly_names:
                target_ver = mcu_versions[target_mcu]
                for fn in friendly_names:
                    if mcu_versions[fn] == target_ver:
                        display_target = fn
                        break

            for mcu_name, mcu_version in sorted(mcu_versions.items()):
                # Skip chip-type aliases (e.g. "stm32h723xx", "rp2040") —
                # these are added for matching, not display
                if mcu_name not in friendly_names:
                    continue
                marker = "*" if mcu_name == display_target else " "
                out.phase("Version", f"  [{marker}] MCU {mcu_name}: {mcu_version}")

            # Check if target MCU is outdated or already current
            if target_mcu and target_mcu in mcu_versions:
                if is_mcu_outdated(host_version, mcu_versions[target_mcu]):
                    out.warn("MCU firmware is behind host Klipper - update recommended")
                else:
                    # MCU firmware matches host - confirm user wants to reflash
                    if not out.confirm(
                        "MCU firmware is already up-to-date. Continue anyway?",
                        default=False,
                    ):
                        out.phase("Flash", "Cancelled - firmware already current")
                        return 0
        else:
            out.warn("MCU versions unavailable (Klipper may not be running)")
    elif mcu_versions:
        # Have MCU versions but not host version (unusual)
        out.warn("Host firmware version unavailable")
    # If neither available, skip version display silently (Moonraker down case handled above)

    out.step_divider()

    # === Phase 2: Config ===
    out.phase("Config", f"Loading config for {entry.name}...")
    config_mgr = ConfigManager(device_key, klipper_dir)

    if config_mgr.load_cached_config():
        out.phase("Config", f"Loaded cached config for '{entry.name}'")
    else:
        config_mgr.clear_klipper_config()
        out.phase("Config", "No cached config found, starting fresh")

    # Skip menuconfig if flag is set AND cached config exists
    if skip_menuconfig:
        if config_mgr.has_cached_config():
            out.phase("Config", f"Using cached config for {entry.name}")
            # Skip menuconfig but still validate MCU
        else:
            # Per CONTEXT.md: warn and launch menuconfig anyway
            out.warn(f"No cached config for '{entry.name}', launching menuconfig")
            skip_menuconfig = False  # Fall through to menuconfig

    if not skip_menuconfig:
        out.phase("Config", "Launching menuconfig...")
        ret_code, was_saved = run_menuconfig(klipper_dir, str(config_mgr.klipper_config_path))

        if ret_code != 0:
            template = ERROR_TEMPLATES["menuconfig_failed"]
            out.error_with_recovery(
                template["error_type"],
                template["message_template"],
                context={"device": device_key},
                recovery=template["recovery_template"],
            )
            return 1

        if not was_saved:
            out.warn("Config was not saved in menuconfig")
            if not out.confirm("Continue build anyway?"):
                out.phase("Config", "Cancelled")
                return 0

        # Save config to cache
        try:
            config_mgr.save_cached_config()
            out.phase("Config", f"Cached config for '{entry.name}'")
        except ConfigError as e:
            out.error_with_recovery(
                "Config error",
                f"Failed to cache config: {e}",
                context={"device": device_key},
                recovery=(
                    "1. Verify Klipper directory is writable\n"
                    "2. Check disk space\n"
                    "3. Re-run menuconfig"
                ),
            )
            return 1

    # MCU validation (always runs, even with skip_menuconfig)
    try:
        is_match, actual_mcu = config_mgr.validate_mcu(entry.mcu)
        if not is_match:
            template = ERROR_TEMPLATES["mcu_mismatch"]
            out.error_with_recovery(
                template["error_type"],
                template["message_template"].format(actual=actual_mcu, expected=entry.mcu),
                context={
                    "device": device_key,
                    "expected": entry.mcu,
                    "actual": actual_mcu,
                },
                recovery=template["recovery_template"],
            )
            return 1
        out.phase("Config", f"MCU validated: {actual_mcu}")
    except ConfigError as e:
        out.error_with_recovery(
            "Config error",
            f"MCU validation failed: {e}",
            context={"device": device_key},
            recovery=(
                "1. Run menuconfig and verify MCU selection\n"
                "2. Check .config file exists\n"
                "3. Ensure CONFIG_MCU is set"
            ),
        )
        return 1

    out.step_divider()

    # === ccache Installation Check ===
    use_ccache = _resolve_ccache_usage(out, registry, data.global_config)

    # === Phase 3: Build ===
    # Safety: dirty repo and downgrade warnings
    from .safety import check_dirty_repo, detect_downgrade

    dirty_result = check_dirty_repo(host_version)
    if dirty_result.is_dirty:
        out.phase("Safety", "Warning: Klipper repo has uncommitted changes")

    if host_version and mcu_versions:
        from .moonraker import get_mcu_version_for_device

        target_mcu_ver = get_mcu_version_for_device(
            entry.mcu,
            device_name=entry.name,
            device_key=entry.key,
            mcu_name=entry.mcu_name,
            allow_fuzzy_fallback=True,
        )
        if target_mcu_ver:
            try:
                downgrade = detect_downgrade(host_version, target_mcu_ver)
                if downgrade.is_downgrade:
                    out.phase(
                        "Safety",
                        "Warning: MCU firmware is newer than host (downgrade)",
                    )
            except (ValueError, TypeError):
                pass  # Non-parseable versions, skip check

    out.phase("Build", "Running make clean + make...")
    build_result = run_build(klipper_dir, timeout=TIMEOUT_BUILD, use_ccache=use_ccache)

    if not build_result.success:
        template = ERROR_TEMPLATES["build_failed"]
        out.error_with_recovery(
            template["error_type"],
            template["message_template"].format(device=device_key),
            context={"device": device_key},
            recovery=template["recovery_template"],
        )
        return 1

    artifact_error, artifact_warning = _check_firmware_artifact(
        build_result.firmware_path,
        build_result.firmware_size,
    )
    if artifact_error:
        out.error_with_recovery(
            "Build artifact invalid",
            f"Firmware output sanity check failed: {artifact_error}",
            context={"device": entry.key},
            recovery=(
                "1. Verify Klipper build output for errors\n"
                "2. Re-run build after `make clean`\n"
                "3. Check storage and filesystem health on the host"
            ),
        )
        return 1
    if artifact_warning:
        out.warn(f"Build artifact warning: {artifact_warning}")

    firmware_path = build_result.firmware_path
    size_kb = build_result.firmware_size / 1024 if build_result.firmware_size else 0
    out.phase(
        "Build",
        f"Firmware ready: {size_kb:.1f} KB in {build_result.elapsed_seconds:.1f}s",
    )

    # Show ccache stats if available
    if build_result.ccache_stats:
        hits = build_result.ccache_stats.total_hits
        misses = build_result.ccache_stats.cache_miss
        rate_pct = int(build_result.ccache_stats.hit_rate * 100)
        out.phase("Build", f"Cache: {hits} hits, {misses} misses ({rate_pct}% hit rate)")

    out.step_divider()

    # === Phase 4: Flash ===
    if entry.is_can_device:
        out.phase("Flash", "CAN device -- skipping USB path verification")
    else:
        out.phase("Flash", "Verifying device connection...")
        try:
            verify_device_path(device_path)
        except DiscoveryError as e:
            out.error_with_recovery(
                "Device disconnected",
                str(e),
                context={"device": device_key, "path": device_path},
                recovery=(
                    "1. Check USB connection and board power\n"
                    "2. List devices: ls /dev/serial/by-id/\n"
                    "3. Reconnect and retry flash"
                ),
            )
            return 1

    # Acquire sudo credentials if service is active and passwordless sudo is missing
    if _is_service_active() and not verify_passwordless_sudo():
        out.phase("Flash", "Sudo authentication required for service management")
        if not acquire_sudo():
            out.error("Failed to acquire sudo credentials. Cannot manage Klipper service.")
            return 1

    if _is_service_active():
        out.phase("Flash", "Stopping Klipper...")
    flash_start = time.monotonic()
    service_restart_failed = False

    try:
        from .bootloader import enter_bootloader

        with klipper_service_stopped(out=out) as svc_state:
            # === Bootloader Phase ===
            if entry.bootloader_method == "none":
                out.phase("Bootloader", "Skipped (method: none)")
                boot_device_path = device_path  # Use original path
            else:
                out.phase("Bootloader", f"Entering {entry.bootloader_method} bootloader...")
                bootloader_stagger = (
                    global_config.can_stagger_delay
                    if entry.is_can_device
                    else global_config.stagger_delay
                )
                boot_result = enter_bootloader(
                    device_path=device_path,
                    device_entry=entry,
                    klipper_dir=klipper_dir,
                    katapult_dir=katapult_dir,
                    stagger_delay=bootloader_stagger,
                    out=out,
                    batch_mode=False,
                )

                if not boot_result.success:
                    out.error(f"Bootloader entry failed: {boot_result.error_message}")
                    return 1

                boot_device_path = boot_result.device_path
                elapsed = boot_result.elapsed_seconds
                if boot_device_path:
                    short_path = _short_path(boot_device_path)
                    out.phase("Bootloader", f"Entered ({elapsed:.1f}s) -- {short_path}")
                else:
                    out.phase("Bootloader", f"Entered ({elapsed:.1f}s)")

            # === Flash Phase (uses bootloader result path) ===
            out.phase("Flash", "Flashing firmware...")
            flash_timeout = TIMEOUT_CAN_FLASH if entry.is_can_device else TIMEOUT_FLASH

            flash_result = execute_flash(
                entry=entry,
                device_path=boot_device_path or "",  # Use path from bootloader
                firmware_path=firmware_path,
                config=global_config,
                timeout=flash_timeout,
            )

            if flash_result.success:
                if entry.is_can_device:
                    # CAN verification: query bus for device with Klipper app
                    out.phase("Verify", "Querying CAN bus for device...")
                    verified, error_reason = verify_can_device_after_flash(
                        uuid=entry.canbus_uuid,
                        interface=entry.canbus_interface or "can0",
                        katapult_dir=katapult_dir,
                    )
                    device_path_new = None  # CAN has no device_path
                else:
                    # USB verification: poll serial directory
                    out.phase("Verify", "Waiting for device to reappear...")
                    verified, device_path_new, error_reason = wait_for_device(
                        entry.serial_pattern,
                        timeout=30.0,
                        out=out,
                    )
            else:
                verified = False
                device_path_new = None
                error_reason = flash_result.error_message

        # Context manager exited - report actual restart outcome
        if svc_state.will_restart and svc_state.restart_succeeded:
            out.phase("Service", "Klipper restarted")
        elif svc_state.will_restart and svc_state.restart_succeeded is False:
            template = ERROR_TEMPLATES["service_start_failed"]
            out.error_with_recovery(
                template["error_type"],
                template["message_template"],
                recovery=template["recovery_template"],
            )
            service_restart_failed = True
    except Exception as e:
        template = ERROR_TEMPLATES["flash_failed"]
        out.error_with_recovery(
            template["error_type"],
            f"Flash operation error: {e}",
            context={"device": device_key},
            recovery=template["recovery_template"],
        )
        return 1

    flash_elapsed = time.monotonic() - flash_start

    # === Summary ===
    if service_restart_failed:
        return 1

    if flash_result.success and verified:
        # Record flash timestamp
        from datetime import datetime

        registry.update_device(
            device_key,
            last_flash_timestamp=datetime.now().replace(microsecond=0).isoformat(),
        )
        out.success(f"Flashed {entry.name} via {flash_result.method} in {flash_elapsed:.1f}s")
        if device_path_new:
            out.phase("Verify", f"Device confirmed at: {device_path_new}")
        elif entry.is_can_device:
            out.phase("Verify", f"Device confirmed on CAN bus [{entry.canbus_uuid}]")
        return 0

    elif flash_result.success and not verified:
        # Flash appeared to succeed but device didn't reappear correctly
        out.warn(f"Device verification failed: {error_reason}")
        if entry.is_can_device:
            out.error_with_recovery(
                "CAN verification timeout",
                f"Device {entry.canbus_uuid} did not return on CAN bus",
                context={"device": device_key, "uuid": entry.canbus_uuid},
                recovery=(
                    "1. Check CAN wiring and termination\n"
                    "2. Verify CAN interface is still up: ip link show can0\n"
                    "3. Query manually: python3 ~/katapult/scripts/flashtool.py -q"
                ),
            )
        else:
            if device_path_new:
                template = ERROR_TEMPLATES["verification_wrong_prefix"]
            else:
                template = ERROR_TEMPLATES["verification_timeout"]
            out.error_with_recovery(
                template["error_type"],
                template["message_template"],
                context={"device": device_key, "pattern": entry.serial_pattern},
                recovery=template["recovery_template"],
            )
        return 1

    else:
        # flash_result.success was False
        template = ERROR_TEMPLATES["flash_failed"]
        out.error_with_recovery(
            template["error_type"],
            template["message_template"].format(device=device_key),
            context={
                "device": device_key,
                "method": flash_result.method,
                "error": flash_result.error_message,
            },
            recovery=template["recovery_template"],
        )
        return 1


def _sort_flash_all_devices(devices: list) -> list:
    """Sort devices for Flash All ordering: CAN toolheads -> USB/no-role -> bridges.

    Within each group, devices are sorted alphabetically by key.
    Role field values: "toolhead", "bridge", or None.
    Role is only honored for CAN devices; USB-only devices always land in
    the middle group even if stale role data exists.
    """
    toolheads: list = []
    middle: list = []
    bridges: list = []
    for entry in devices:
        is_can = bool(getattr(entry, "is_can_device", False))
        role = getattr(entry, "role", None) if is_can else None
        if role == "toolhead":
            toolheads.append(entry)
        elif role == "bridge":
            bridges.append(entry)
        else:
            middle.append(entry)
    toolheads.sort(key=lambda e: e.key)
    middle.sort(key=lambda e: e.key)
    bridges.sort(key=lambda e: e.key)
    return toolheads + middle + bridges


def _dedupe_flash_all_devices(devices: list) -> list:
    """Return devices deduplicated by registry key, preserving first occurrence."""
    seen: set[str] = set()
    deduped: list = []
    for entry in devices:
        if entry.key in seen:
            continue
        seen.add(entry.key)
        deduped.append(entry)
    return deduped


def _should_apply_flash_all_stagger(previous_result) -> bool:
    """Return True when the previous flash-all device touched hardware."""
    if previous_result is None:
        return False
    if previous_result.bootloader_ok or previous_result.flash_ok:
        return True
    msg = (previous_result.error_message or "").strip().lower()
    return msg.startswith("bootloader:")


def cmd_flash_all(registry, out) -> int:
    """Build and flash firmware for all registered flashable devices.

    Orchestrates a 5-stage batch workflow:
    1. Validate all devices have cached configs
    2. Version check — prompt if all MCUs already match host
    3. Build all firmware quietly, copy to temp dir
    4. Flash all inside single klipper_service_stopped()
    5. Print summary table

    One device failure never blocks others from being processed.

    Args:
        registry: Registry instance for device lookup.
        out: Output interface for user messages.

    Returns:
        0 if all devices passed, 1 if any failed.
    """
    import os
    import shutil
    import tempfile
    import time

    from .build import run_build

    # Late imports
    from .config import ConfigManager
    from .discovery import (
        extract_mcu_from_serial,
        match_device,
        match_devices,
        preflight_can_interface,
        scan_serial_devices,
        verify_can_device_after_flash,
    )
    from .flasher import TIMEOUT_CAN_FLASH, TIMEOUT_FLASH, execute_flash
    from .models import BatchDeviceResult
    from .moonraker import (
        get_host_klipper_version,
        get_mcu_canbus_map,
        get_mcu_version_for_device,
        get_mcu_versions,
        get_print_status,
        is_mcu_outdated,
    )
    from .service import (
        _is_service_active,
        acquire_sudo,
        klipper_service_stopped,
        verify_passwordless_sudo,
    )
    from .tui import wait_for_device

    # Load registry
    data = registry.load()
    if data.global_config is None:
        out.error("Global config not set. Press A to add a device first.")
        return 1

    global_config = data.global_config
    klipper_dir = global_config.klipper_dir
    katapult_dir = global_config.katapult_dir

    # === Preflight: Build prerequisites (SAFE-01) ===
    if not _preflight_build(out, klipper_dir):
        return 1

    # === Preflight: Moonraker safety check (SAFE-02) ===
    from .safety import should_block_on_printer_state

    print_status = get_print_status()

    if print_status is None:
        out.warn("Moonraker unreachable - print status and version check unavailable")
        if not out.confirm("Continue without safety checks?", default=False):
            out.phase("Flash All", "Cancelled")
            return 0
    else:
        state = (print_status.state or "").lower()
        if state == "error":
            out.warn("Printer is in 'error' state. Flashing may be needed to recover.")
            if not out.confirm("Continue flashing despite printer error state?", default=False):
                out.phase("Flash All", "Cancelled")
                return 0
        elif should_block_on_printer_state(state):
            if state in ("printing", "paused"):
                progress_pct = int(print_status.progress * 100)
                filename = print_status.filename or "unknown"
                detail = f"Print in progress: {filename} ({progress_pct}%)"
            else:
                detail = f"Printer is in '{print_status.state}' state"
            out.error_with_recovery(
                "Printer busy",
                detail,
                recovery=(
                    "1. Wait for printer to reach 'ready' state\n"
                    "2. Or cancel print in Fluidd/Mainsail dashboard\n"
                    "3. Then re-run flash command"
                ),
            )
            return 1
        else:
            out.phase("Safety", f"Printer state: {print_status.state} - OK to flash")

    out.step_divider()

    # === Stage 1: Validate cached configs ===
    out.phase("Flash All", "Validating cached configs...")

    flashable_devices = _sort_flash_all_devices(
        [e for e in data.devices.values() if e.flashable]
    )

    if not flashable_devices:
        out.error("No flashable devices registered. Press A to register a board.")
        return 1

    # Filter blocked devices
    blocked_list = _build_blocked_list(data)
    blocked_devices: list[tuple] = []
    unblocked_devices: list = []
    for entry in flashable_devices:
        reason = _blocked_reason_for_entry(entry, blocked_list)
        if reason:
            blocked_devices.append((entry, reason))
        else:
            unblocked_devices.append(entry)

    if blocked_devices:
        for entry, reason in blocked_devices:
            out.warn(f"Skipping {entry.name}: {reason}")

    if not unblocked_devices:
        out.error("All flashable devices are blocked. Nothing to flash.")
        return 1

    flashable_devices = unblocked_devices

    # Check cached configs exist
    missing_configs: list[str] = []
    for entry in flashable_devices:
        config_mgr = ConfigManager(entry.key, klipper_dir)
        if not config_mgr.cache_path.exists():
            missing_configs.append(entry.name)

    if missing_configs:
        out.error("The following devices lack cached configs:")
        for name in missing_configs:
            out.error(f"  - {name}")
        out.error("Flash each device individually and save config before using Flash All.")
        return 1

    # Validate MCU match for each cached config
    from .errors import ConfigError

    mcu_mismatches: list[tuple[str, str, str]] = []
    for entry in flashable_devices:
        config_mgr = ConfigManager(entry.key, klipper_dir)
        try:
            config_mgr.load_cached_config()
            is_match, actual_mcu = config_mgr.validate_mcu(entry.mcu)
            if not is_match:
                mcu_mismatches.append((entry.name, entry.mcu, actual_mcu or "unknown"))
        except ConfigError:
            mcu_mismatches.append((entry.name, entry.mcu, "corrupt/unreadable"))

    if mcu_mismatches:
        out.error("MCU type mismatch in cached configs:")
        for name, expected, actual in mcu_mismatches:
            out.error(f"  - {name}: expected {expected}, config has {actual}")
        out.error("Flash each mismatched device individually to reconfigure.")
        return 1

    # Display config ages and warn on stale configs
    for entry in flashable_devices:
        config_mgr = ConfigManager(entry.key, klipper_dir)
        age_display = config_mgr.get_cache_age_display()
        age_str = age_display or "unknown"
        out.info("", f"  {entry.name}: config cached {age_str}")
        if age_display and "Recommend Review" in age_display:
            out.warn(
                f"  {entry.name} config is very old"
                " — consider flashing individually to review config"
            )

    out.phase("Flash All", f"{len(flashable_devices)} device(s) validated")

    # Confirm before proceeding
    device_names = ", ".join(e.name for e in flashable_devices)
    if not out.confirm(f"Flash {len(flashable_devices)} device(s) ({device_names})?", default=True):
        out.phase("Flash All", "Cancelled")
        return 0

    out.step_divider()

    # === Stage 2: Version check ===
    host_version = get_host_klipper_version(klipper_dir)
    mcu_versions = get_mcu_versions()
    canbus_map = get_mcu_canbus_map()

    flash_list = list(flashable_devices)

    if host_version is None or mcu_versions is None:
        out.warn("Version check unavailable -- Moonraker not reachable. Flashing all devices.")
    else:
        from .moonraker import detect_firmware_flavor

        out.phase("Version", f"Host: {detect_firmware_flavor(host_version)} {host_version}")
        outdated: list = []
        current: list = []
        unknown_version: list = []

        for entry in flashable_devices:
            if entry.is_can_device and canbus_map is not None:
                # CAN version check: use UUID-to-MCU mapping
                can_mcu_name = canbus_map.get(entry.canbus_uuid or "")
                if can_mcu_name:
                    mcu_ver = get_mcu_version_for_device(mcu_name=can_mcu_name)
                else:
                    mcu_ver = None  # UUID not in Moonraker -- unknown version
            else:
                mcu_ver = get_mcu_version_for_device(
                    entry.mcu,
                    device_name=entry.name,
                    device_key=entry.key,
                    mcu_name=entry.mcu_name,
                    allow_fuzzy_fallback=True,
                )

            if entry.is_can_device and mcu_ver is None and canbus_map is not None:
                # Moonraker reachable but UUID not found -- unknown version
                unknown_version.append(entry)
            elif mcu_ver and not is_mcu_outdated(host_version, mcu_ver):
                current.append(entry)
            else:
                outdated.append(entry)

        if not outdated and not unknown_version:
            # All match
            out.phase("Version", "All devices already match host version.")
            try:
                answer = input("  Flash anyway? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer != "y":
                out.phase("Flash All", "Cancelled -- firmware already current")
                return 0
        elif current:
            # Some match, some don't
            out.phase("Version", "Outdated devices:")
            for entry in outdated:
                out.info("", f"  - {entry.name}")
            out.phase("Version", "Up-to-date devices:")
            for entry in current:
                out.info("", f"  - {entry.name}")
            try:
                answer = input("  Flash only outdated devices? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "y"
            if answer != "n":
                flash_list = outdated
            # else flash_list remains all devices

        # Handle CAN devices with unknown version (UUID not in Moonraker)
        if unknown_version:
            out.phase("Version", "CAN devices with unknown version (not in Moonraker):")
            for entry in unknown_version:
                iface = entry.canbus_interface or "can0"
                out.info("", f"  - {entry.name} ({iface}: {entry.canbus_uuid})")
            try:
                answer = input("  Include unknown-version devices? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "y"
            if answer != "n":
                flash_list = _dedupe_flash_all_devices(flash_list + unknown_version)
            else:
                unknown_keys = {entry.key for entry in unknown_version}
                flash_list = [entry for entry in flash_list if entry.key not in unknown_keys]
            # Re-sort after adding unknown devices to maintain ordering
            flash_list = _sort_flash_all_devices(flash_list)

    # === ccache Installation Check ===
    use_ccache = _resolve_ccache_usage(out, registry, global_config)

    # Initialize results tracking
    results: list[BatchDeviceResult] = []
    for entry in flash_list:
        results.append(
            BatchDeviceResult(
                device_key=entry.key,
                device_name=entry.name,
                config_ok=True,  # Already validated in Stage 1
            )
        )

    # === Stage 3: Build all firmware ===
    # Safety: dirty repo and downgrade warnings (once for batch)
    from .safety import check_dirty_repo, detect_downgrade

    dirty_result = check_dirty_repo(host_version)
    if dirty_result.is_dirty:
        out.phase("Safety", "Warning: Klipper repo has uncommitted changes")

    if host_version and mcu_versions:
        for entry in flash_list:
            if entry.is_can_device and canbus_map:
                can_mcu_name = canbus_map.get(entry.canbus_uuid or "")
                if can_mcu_name:
                    mcu_ver = get_mcu_version_for_device(mcu_name=can_mcu_name)
                else:
                    mcu_ver = None
            else:
                mcu_ver = get_mcu_version_for_device(
                    entry.mcu,
                    device_name=entry.name,
                    device_key=entry.key,
                    mcu_name=entry.mcu_name,
                    allow_fuzzy_fallback=True,
                )
            if mcu_ver:
                try:
                    downgrade = detect_downgrade(host_version, mcu_ver)
                    if downgrade.is_downgrade:
                        out.phase(
                            "Safety",
                            f"Warning: {entry.name} MCU firmware is newer than host (downgrade)",
                        )
                except (ValueError, TypeError):
                    pass

    out.step_divider()
    out.phase("Flash All", f"Building firmware for {len(flash_list)} device(s)...")
    temp_dir = tempfile.mkdtemp(prefix="kalico-flash-")
    total = len(flash_list)
    service_restart_failed = False

    try:
        for i, (entry, result) in enumerate(zip(flash_list, results)):
            if i > 0:
                out.device_divider(i + 1, total, entry.name)
            print(f"  Building {i + 1}/{total}: {entry.name}...")
            config_mgr = ConfigManager(entry.key, klipper_dir)
            config_mgr.load_cached_config()

            build_result = run_build(klipper_dir, quiet=True, use_ccache=use_ccache)

            if build_result.success:
                artifact_error, artifact_warning = _check_firmware_artifact(
                    build_result.firmware_path,
                    build_result.firmware_size,
                )
                if artifact_error:
                    result.error_message = f"Invalid firmware artifact: {artifact_error}"
                    print(f"  X {entry.name} invalid firmware artifact ({i + 1}/{total})")
                    continue
                if artifact_warning:
                    out.warn(f"{entry.name}: {artifact_warning}")

                # Copy firmware to temp dir (use path from build result)
                device_fw_dir = os.path.join(temp_dir, entry.key)
                os.makedirs(device_fw_dir, exist_ok=True)
                fw_src = build_result.firmware_path
                fw_name = os.path.basename(fw_src)
                fw_dst = os.path.join(device_fw_dir, fw_name)
                shutil.copy2(fw_src, fw_dst)
                result.firmware_name = fw_name
                result.build_ok = True
                if build_result.ccache_stats:
                    result.ccache_stats = build_result.ccache_stats
                    result.ccache_hit_rate = build_result.ccache_stats.hit_rate
                print(f"  \u2713 {entry.name} built ({i + 1}/{total})")
            else:
                result.error_message = build_result.error_message or "Build failed"
                result.error_output = build_result.error_output
                print(f"  \u2717 {entry.name} build failed ({i + 1}/{total})")

        # Check if any builds succeeded
        built_results = [(e, r) for e, r in zip(flash_list, results) if r.build_ok]
        if not built_results:
            out.error("All builds failed. Nothing to flash.")
            return 1

        # === Stage 4: Flash all (inside single service stop) ===
        from .bootloader import enter_bootloader
        out.step_divider()
        out.phase("Flash All", f"Flashing {len(built_results)} device(s)...")

        # Acquire sudo credentials if service is active and passwordless sudo is missing
        if _is_service_active() and not verify_passwordless_sudo():
            out.phase("Flash All", "Sudo authentication required for service management")
            if not acquire_sudo():
                out.error("Failed to acquire sudo credentials. Cannot manage Klipper service.")
                return 1

        flash_total = len(built_results)
        used_paths: set[str] = set()
        preflight_cache: dict[str, bool] = {}

        with klipper_service_stopped(out=out) as svc_state:
            # Re-scan USB after Klipper stop
            usb_devices = scan_serial_devices()

            # Duplicate USB match detection (mirrors cmd_flash logic)
            # CAN devices have no serial_pattern so skip them
            ambiguous_keys: set[str] = set()
            for entry, _ in built_results:
                if entry.serial_pattern is not None:
                    matches = match_devices(entry.serial_pattern, usb_devices)
                    if len(matches) > 1:
                        ambiguous_keys.add(entry.key)

            # CAN interface preflight cache (keyed by interface name)
            can_preflight_cache: dict[str, bool] = {}

            for idx, (entry, result) in enumerate(built_results):
                flash_idx = idx + 1
                if idx > 0:
                    out.device_divider(flash_idx, flash_total, entry.name)
                    previous_result = built_results[idx - 1][1]
                    if _should_apply_flash_all_stagger(previous_result):
                        # Use CAN or USB stagger delay based on upcoming device transport.
                        delay = (
                            global_config.can_stagger_delay
                            if entry.is_can_device
                            else global_config.stagger_delay
                        )
                        time.sleep(delay)

                if entry.is_can_device:
                    # === CAN device flash path ===
                    iface = entry.canbus_interface or "can0"

                    # CAN interface preflight (cached per interface)
                    if iface not in can_preflight_cache:
                        ok, error = preflight_can_interface(iface)
                        can_preflight_cache[iface] = ok
                        if not ok:
                            result.error_message = f"CAN preflight failed: {error}"
                            print(
                                f"  X {entry.name} CAN preflight failed"
                                f" ({flash_idx}/{flash_total})"
                            )
                            continue
                    elif not can_preflight_cache[iface]:
                        result.error_message = f"CAN interface {iface} failed preflight"
                        print(
                            f"  X {entry.name} CAN interface unavailable"
                            f" ({flash_idx}/{flash_total})"
                        )
                        continue

                    # Validate flash config
                    issue = _get_device_flash_config_issue(entry)
                    if issue is not None:
                        error_type, detail = issue
                        result.error_message = f"{error_type}: {detail}"
                        print(f"  X {entry.name} invalid config ({flash_idx}/{flash_total})")
                        continue

                    # CAN flash method preflight
                    method = entry.flash_command
                    if method not in preflight_cache:
                        preflight_cache[method] = _preflight_flash(
                            out, klipper_dir, katapult_dir, method
                        )
                    if not preflight_cache[method]:
                        result.error_message = (
                            f"Preflight failed for flash command: {method}"
                        )
                        print(
                            f"  X {entry.name} preflight failed ({flash_idx}/{flash_total})"
                        )
                        continue

                    # CAN bootloader entry
                    out.phase("Bootloader", f"Entering CAN bootloader for {entry.name}...")
                    boot_result = enter_bootloader(
                        device_path="",  # CAN has no USB path
                        device_entry=entry,
                        klipper_dir=klipper_dir,
                        katapult_dir=katapult_dir,
                        stagger_delay=global_config.can_stagger_delay,
                        out=None,  # batch mode: no retry prompt
                        batch_mode=True,
                    )

                    if not boot_result.success:
                        result.bootloader_ok = False
                        result.error_message = f"Bootloader: {boot_result.error_message}"
                        print(
                            f"  X {entry.name} bootloader failed ({flash_idx}/{flash_total})"
                        )
                        continue

                    result.bootloader_ok = True
                    out.phase(
                        "Bootloader", f"Entered ({boot_result.elapsed_seconds:.1f}s)"
                    )

                    # CAN flash
                    fw_path = os.path.join(temp_dir, entry.key, result.firmware_name)
                    flash_result = execute_flash(
                        entry=entry,
                        device_path="",  # CAN uses UUID, not USB path
                        firmware_path=fw_path,
                        config=global_config,
                        timeout=TIMEOUT_CAN_FLASH,
                    )

                    if flash_result.success:
                        result.flash_ok = True
                        # CAN post-flash verification
                        out.phase("Verify", f"Querying CAN bus for {entry.name}...")
                        verified, error_reason = verify_can_device_after_flash(
                            entry.canbus_uuid or "",
                            iface,
                            katapult_dir,
                        )
                        if verified:
                            result.verify_ok = True
                            from datetime import datetime

                            registry.update_device(
                                entry.key,
                                last_flash_timestamp=(
                                    datetime.now().replace(microsecond=0).isoformat()
                                ),
                            )
                            print(
                                f"  \u2713 {entry.name} flashed and verified"
                                f" ({flash_idx}/{flash_total})"
                            )
                        else:
                            result.error_message = (
                                error_reason or "CAN verification failed"
                            )
                            print(
                                f"  \u2717 {entry.name} flash OK but CAN verify failed"
                                f" ({flash_idx}/{flash_total})"
                            )
                    else:
                        result.error_message = (
                            flash_result.error_message or "Flash failed"
                        )
                        print(
                            f"  \u2717 {entry.name} flash failed ({flash_idx}/{flash_total})"
                        )

                else:
                    # === USB device flash path (existing code) ===

                    # Ambiguous pattern guard
                    if entry.key in ambiguous_keys:
                        result.error_message = (
                            "Pattern matches multiple connected USB devices"
                        )
                        out.warn(f"Skipping {entry.name}: ambiguous USB pattern")
                        continue

                    # Find device
                    usb_device = match_device(entry.serial_pattern, usb_devices)
                    if usb_device is None:
                        result.error_message = "Device not found on USB"
                        print(
                            f"  \u2717 {entry.name} not found ({flash_idx}/{flash_total})"
                        )
                        continue

                    # Duplicate USB path guard (SAFE-04)
                    real_path = os.path.realpath(usb_device.path)
                    if _check_duplicate_path(real_path, used_paths):
                        result.error_message = "USB path already targeted by prior device"
                        out.warn(f"Skipping {entry.name}: duplicate USB path")
                        continue

                    # MCU cross-check (SAFE-03)
                    usb_mcu = extract_mcu_from_serial(usb_device.filename)
                    if usb_mcu is not None and usb_mcu.lower() != entry.mcu.lower():
                        result.error_message = (
                            f"MCU mismatch: USB='{usb_mcu}', registry='{entry.mcu}'"
                        )
                        out.warn(f"Skipping {entry.name}: {result.error_message}")
                        continue

                    # Validate flash config
                    issue = _get_device_flash_config_issue(entry)
                    if issue is not None:
                        error_type, detail = issue
                        result.error_message = f"{error_type}: {detail}"
                        print(f"  X {entry.name} invalid config ({flash_idx}/{flash_total})")
                        continue

                    method = entry.flash_command
                    if method not in preflight_cache:
                        preflight_cache[method] = _preflight_flash(
                            out, klipper_dir, katapult_dir, method
                        )
                    if not preflight_cache[method]:
                        result.error_message = (
                            f"Preflight failed for flash command: {method}"
                        )
                        print(
                            f"  X {entry.name} preflight failed ({flash_idx}/{flash_total})"
                        )
                        continue

                    # === Bootloader Phase ===
                    if entry.bootloader_method == "none":
                        out.phase("Bootloader", "Skipped (method: none)")
                        boot_device_path = usb_device.path
                        result.bootloader_ok = True
                    else:
                        out.phase(
                            "Bootloader", f"Entering {entry.bootloader_method}..."
                        )
                        boot_result = enter_bootloader(
                            device_path=usb_device.path,
                            device_entry=entry,
                            klipper_dir=klipper_dir,
                            katapult_dir=katapult_dir,
                            stagger_delay=global_config.stagger_delay,
                            out=None,  # batch mode: no retry prompt
                            batch_mode=True,
                        )

                        if not boot_result.success:
                            result.bootloader_ok = False
                            result.error_message = (
                                f"Bootloader: {boot_result.error_message}"
                            )
                            print(
                                f"  X {entry.name} bootloader failed"
                                f" ({flash_idx}/{flash_total})"
                            )
                            continue

                        result.bootloader_ok = True
                        boot_device_path = boot_result.device_path or ""
                        out.phase(
                            "Bootloader",
                            f"Entered ({boot_result.elapsed_seconds:.1f}s)",
                        )

                    fw_path = os.path.join(temp_dir, entry.key, result.firmware_name)
                    flash_result = execute_flash(
                        entry=entry,
                        device_path=boot_device_path,
                        firmware_path=fw_path,
                        config=global_config,
                        timeout=TIMEOUT_FLASH,
                    )

                    if flash_result.success:
                        result.flash_ok = True
                        # Post-flash verification
                        verified, _, error_reason = wait_for_device(
                            entry.serial_pattern,
                            timeout=30.0,
                            out=out,
                        )
                        if verified:
                            result.verify_ok = True
                            # Record flash timestamp
                            from datetime import datetime

                            registry.update_device(
                                entry.key,
                                last_flash_timestamp=(
                                    datetime.now().replace(microsecond=0).isoformat()
                                ),
                            )
                            print(
                                f"  \u2713 {entry.name} flashed and verified"
                                f" ({flash_idx}/{flash_total})"
                            )
                        else:
                            result.error_message = (
                                error_reason or "Verification failed"
                            )
                            print(
                                f"  \u2717 {entry.name} flash OK but verify failed"
                                f" ({flash_idx}/{flash_total})"
                            )

                        # Re-scan after flash for next device
                        usb_devices = scan_serial_devices()
                    else:
                        result.error_message = (
                            flash_result.error_message or "Flash failed"
                        )
                        print(
                            f"  \u2717 {entry.name} flash failed ({flash_idx}/{flash_total})"
                        )

        # Report actual restart outcome
        if svc_state.will_restart and svc_state.restart_succeeded:
            out.phase("Service", "Klipper restarted")
        elif svc_state.will_restart and svc_state.restart_succeeded is False:
            out.error_with_recovery(
                "Service error",
                "Failed to restart Klipper service after flash",
                recovery=(
                    "1. Start manually: `sudo systemctl start klipper`\n"
                    "2. Check logs: `sudo journalctl -u klipper -n 50`\n"
                    "3. Firmware was flashed - issue is service, not board"
                ),
            )
            service_restart_failed = True

    finally:
        # Clean up temp dir
        shutil.rmtree(temp_dir, ignore_errors=True)

    # === Stage 5: Summary table ===
    out.step_divider()
    out.phase("Flash All", "Summary:")
    out.info("", "  Device                Build   Boot    Flash   Verify  Cache (H/M%)")
    out.info("", "  " + "-" * 72)

    all_passed = True
    for result in results:
        build_str = "PASS" if result.build_ok else "FAIL"
        if result.build_ok:
            boot_str = "PASS" if result.bootloader_ok else "FAIL"
        else:
            boot_str = "SKIP"
        if result.bootloader_ok:
            flash_str = "PASS" if result.flash_ok else "FAIL"
        else:
            flash_str = "SKIP"
        if result.flash_ok:
            verify_str = "PASS" if result.verify_ok else "FAIL"
        else:
            verify_str = "SKIP"

        if not (result.build_ok and result.bootloader_ok and result.flash_ok and result.verify_ok):
            all_passed = False

        if result.ccache_stats is not None:
            hits = result.ccache_stats.total_hits
            misses = result.ccache_stats.cache_miss
            rate_pct = int(result.ccache_stats.hit_rate * 100)
            cache_str = f"{hits}/{misses} {rate_pct}%"
        elif result.ccache_hit_rate is not None:
            cache_str = f"{int(result.ccache_hit_rate * 100)}%"
        else:
            cache_str = "-"

        name = result.device_name[:20].ljust(20)
        row = f"  {name}  {build_str:6s}  {boot_str:6s}  {flash_str:6s}  {verify_str:6s}"
        out.info("", f"{row}  {cache_str}")

        # Show build error output inline for failed builds (DBUG-01)
        if not result.build_ok and result.error_output:
            lines = result.error_output.strip().splitlines()
            tail = lines[-20:]
            out.info("", f"  Build output (last {len(tail)} lines):")
            for line in tail:
                out.info("", f"    {line}")

    passed = sum(
        1 for r in results if r.build_ok and r.bootloader_ok and r.flash_ok and r.verify_ok
    )
    failed = len(results) - passed
    out.info("", "")
    out.info("", f"  {passed} passed, {failed} failed out of {len(results)} device(s)")

    return 0 if all_passed and not service_restart_failed else 1


def cmd_remove_device(registry, device_key: str, out) -> int:
    """Remove a device from the registry with optional config cleanup."""
    from .errors import ERROR_TEMPLATES

    entry = registry.get(device_key)
    if entry is None:
        template = ERROR_TEMPLATES["device_not_registered"]
        out.error_with_recovery(
            template["error_type"],
            template["message_template"].format(device=device_key),
            context={"device": device_key},
            recovery=template["recovery_template"],
        )
        return 1

    out.step_divider()

    if not out.confirm(f"Remove '{entry.name}'?"):
        out.info("Registry", "Removal cancelled")
        return 0

    registry.remove(device_key)
    out.success(f"Removed '{entry.name}'")

    out.step_divider()

    _remove_cached_config(device_key, out, prompt=True, device_name=entry.name)

    return 0


def cmd_list_devices(registry, out) -> int:
    """List all registered devices with connection status.

    Cross-references registered devices against live USB scan to show:
    - [REG] Connected devices with their USB filename
    - [REG] Disconnected devices (registered but not currently connected)
    - [NEW] Unknown USB devices (connected but not registered)

    Args:
        registry: Registry instance for device lookup.
        out: Output interface for user messages.
    """
    from .discovery import is_supported_device, match_devices, scan_serial_devices
    from .moonraker import get_host_klipper_version, get_mcu_version_for_device, get_mcu_versions

    # Load registry and scan USB devices
    data = registry.load()
    usb_devices = scan_serial_devices()
    blocked_list = _build_blocked_list(data)

    # Fetch version information
    mcu_versions = get_mcu_versions()
    host_version = None
    if data.global_config:
        host_version = get_host_klipper_version(data.global_config.klipper_dir)

    # Cross-reference registered vs discovered
    entry_matches: dict[str, list] = {}
    device_matches: dict[str, list] = {}
    for entry in data.devices.values():
        matches = match_devices(entry.serial_pattern, usb_devices)
        entry_matches[entry.key] = matches
        for device in matches:
            device_matches.setdefault(device.filename, []).append(entry)

    matched_filenames = set(device_matches.keys())
    unmatched = [device for device in usb_devices if device.filename not in matched_filenames]

    duplicate_entry_keys = {key for key, matches in entry_matches.items() if len(matches) > 1}
    duplicate_devices = {
        filename
        for filename, entries in device_matches.items()
        if len(entries) > 1 or any(entry.key in duplicate_entry_keys for entry in entries)
    }

    registered_connected = 0
    new_count = 0
    blocked_count = 0
    duplicate_count = 0
    for device in usb_devices:
        blocked_reason = _blocked_reason_for_filename(device.filename, blocked_list)
        if blocked_reason or not is_supported_device(device.filename):
            blocked_count += 1
            continue
        entries = device_matches.get(device.filename, [])
        if entries:
            if device.filename in duplicate_devices:
                duplicate_count += 1
            else:
                registered_connected += 1
        else:
            new_count += 1

    # Handle: no registered devices AND no USB devices
    if not data.devices and not usb_devices:
        out.info("Devices", "No registered devices and no USB devices found.")
        return 0

    # Handle: no registered devices BUT USB devices exist (first-run UX)
    if not data.devices and usb_devices:
        summary = (
            f"{len(usb_devices)} USB devices found: {registered_connected} registered, "
            f"{new_count} new, {blocked_count} blocked, {duplicate_count} duplicate"
        )
        out.info("Devices", f"No registered devices. {summary}.")
        for device in usb_devices:
            blocked_reason = _blocked_reason_for_filename(device.filename, blocked_list)
            if blocked_reason or not is_supported_device(device.filename):
                marker = "BLK"
                detail = blocked_reason or "Unsupported USB device"
            else:
                marker = "NEW"
                detail = "Unregistered device"
            out.device_line(marker, device.filename, detail)
        out.info("Devices", "Press A to register a board.")
        return 0

    # Normal display: show registered devices with connection status
    summary = (
        f"{len(usb_devices)} USB devices found: {registered_connected} registered, "
        f"{new_count} new, {blocked_count} blocked, {duplicate_count} duplicate"
    )
    out.info("Devices", f"{len(data.devices)} registered. {summary}.")

    for key in sorted(data.devices.keys()):
        entry = data.devices[key]
        # Build name with optional [excluded] marker
        name_str = f"{entry.name} ({entry.mcu})"
        if not entry.flashable:
            name_str += " [excluded]"
        blocked_reason = _blocked_reason_for_entry(entry, blocked_list)
        if blocked_reason:
            name_str += " [blocked]"

        if blocked_reason:
            matches = entry_matches.get(key, [])
            if matches:
                detail = f"{blocked_reason} [{matches[0].filename}]"
            else:
                detail = blocked_reason
            out.device_line("BLK", name_str, detail)
        elif key in duplicate_entry_keys:
            devices = entry_matches.get(key, [])
            detail = ", ".join(d.filename for d in devices)
            out.device_line("DUP", name_str, detail)
        elif entry_matches.get(key):
            device = entry_matches[key][0]
            out.device_line("REG", name_str, device.filename)
        else:
            out.device_line("REG", name_str, "(disconnected)")

        # Show MCU software version if available
        if mcu_versions:
            version = get_mcu_version_for_device(
                entry.mcu, device_name=entry.name, device_key=entry.key, mcu_name=entry.mcu_name
            )
            if version:
                out.info("", f"       MCU software version: {version}")

    # Show unmatched (unknown/blocked) USB devices if any
    if unmatched:
        # Separate blocked from new devices
        blocked_unmatched = []
        new_unmatched = []
        for device in unmatched:
            blocked_reason = _blocked_reason_for_filename(device.filename, blocked_list)
            if blocked_reason or not is_supported_device(device.filename):
                blocked_unmatched.append((device, blocked_reason or "Unsupported USB device"))
            else:
                new_unmatched.append(device)

        # Show new (unregistered) devices
        if new_unmatched:
            out.info("", "")  # blank line for separation
            for device in new_unmatched:
                out.device_line("NEW", device.filename, "Unregistered device")

        # Show blocked devices with label
        if blocked_unmatched:
            out.info("", "")  # blank line for separation
            out.info("Blocked devices", "")
            for device, reason in blocked_unmatched:
                out.device_line("BLK", device.filename, reason)

        # Show hint if there are new unregistered devices
        if new_unmatched:
            out.info("Devices", "Press A to register unknown devices.")

    # Show host Klipper version at the end
    if host_version:
        out.info("", "")  # blank line for separation
        from .moonraker import detect_firmware_flavor

        out.info("Version", f"Host: {detect_firmware_flavor(host_version)} {host_version}")

    return 0


def _pick_mcu_name(mcu_serials: dict[str, str | None], out) -> str | None:
    """Show numbered MCU name picker. Returns selected name or None."""
    # Filter to MCUs with serial paths
    mcu_with_serial = {k: v for k, v in mcu_serials.items() if v is not None}

    if not mcu_with_serial:
        out.info("MCU Name", "No MCUs with serial paths in Klipper config")
        raw = out.prompt("Enter MCU name manually (or press Enter to skip)")
        return raw.strip() if raw and raw.strip() else None

    out.info("MCU Name", "Select from Klipper config MCUs:")
    names = list(mcu_with_serial.keys())
    for i, name in enumerate(names, 1):
        out.info("", f"  {i}. {name}")
    out.info("", "  0. Enter manually")
    out.info("", "  (blank). Skip - no MCU name")

    for _ in range(3):
        raw = out.prompt("MCU name selection").strip()
        if not raw:
            return None
        if raw == "0":
            manual = out.prompt("Enter MCU name")
            return manual.strip() if manual and manual.strip() else None
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(names):
                return names[idx]
        except ValueError:
            pass
        out.warn(f"Invalid selection '{raw}'")
    return None


def _prompt_can_uuid(out) -> str | None:
    """Prompt for manual CAN UUID entry with validation."""
    from .validation import validate_canbus_uuid

    for _attempt in range(3):
        raw = out.prompt("CAN bus UUID (12 hex characters)")
        if not raw:
            return None
        ok, err = validate_canbus_uuid(raw)
        if ok:
            return raw.lower()
        out.warn(err)
    out.error("Too many invalid inputs.")
    return None


def cmd_add_device(
    registry, out, selected_device=None, can_uuid=None, can_interface=None, can_only=False
) -> int:
    """Interactive wizard to register a new device.

    Args:
        registry: Registry instance for device persistence.
        out: Output interface for user messages.
        selected_device: Optional pre-selected DiscoveredDevice (from TUI).
            When provided, skips discovery scan, listing, and selection prompt.
        can_uuid: Optional pre-filled CAN UUID (from TUI CAN discovery).
            When provided, skips all discovery/selection and enters CAN path.
        can_interface: Optional CAN interface name (e.g., "can0").
            Used with can_uuid for pre-filled CAN device registration.
        can_only: When True, skip USB discovery and go straight to CAN
            interface selection and CAN bus scan.
    """
    # Import discovery functions for USB scanning
    from .discovery import (
        extract_mcu_from_serial,
        generate_serial_pattern,
        is_supported_device,
        match_devices,
        scan_serial_devices,
    )
    from .models import DeviceEntry, GlobalConfig
    from .tui import _get_menu_choice

    # TTY check: wizard requires interactive terminal
    if not sys.stdin.isatty():
        out.error("Interactive terminal required. Run from SSH terminal.")
        return 1

    # CAN transport state -- initialized here to prevent NameError in any branch
    is_can_transport = False
    canbus_uuid_value = None
    canbus_interface_value = None
    replacing_existing_device = False

    if can_uuid is not None:
        # Pre-filled CAN path (from TUI CAN discovery via "A" on CAN device)
        is_can_transport = True
        canbus_uuid_value = can_uuid
        canbus_interface_value = can_interface or "can0"
        serial_pattern = None
        selected = None
        existing_entry = None
        registry_data = registry.load()
        # Skip to common wizard steps (display name, MCU, etc.)
    elif selected_device is not None:
        # TUI path: device already selected, skip discovery/listing/selection
        selected = selected_device
        from .screen import truncate_serial

        out.info("Selected", truncate_serial(selected.filename))

        # Determine if this device is already registered
        registry_data = registry.load()
        devices = scan_serial_devices()
        existing_entry = None
        for entry in registry_data.devices.values():
            if entry.serial_pattern is None:
                continue  # CAN devices have no serial_pattern
            matches = match_devices(entry.serial_pattern, devices)
            for matched_dev in matches:
                if matched_dev.filename == selected.filename:
                    existing_entry = entry
                    break
            if existing_entry is not None:
                break
    else:
        # Full discovery scan and selection
        if can_only:
            # CAN-only shortcut: skip USB discovery entirely
            from .discovery import get_can_interfaces

            can_interfaces = get_can_interfaces()
            if not can_interfaces:
                out.error("No CAN interfaces found.")
                return 1

            registry_data = registry.load()
            # Jump straight to CAN interface selection (same as choice=="c" below)
            choice = "c"
        else:
            # Step 1: Scan USB devices
            out.info("Discovery", "Scanning for USB serial devices...")
            devices = scan_serial_devices()
            if not devices:
                out.error("No USB devices found. Plug in a board and try again.")
                return 1

            registry_data = registry.load()
            blocked_list = _build_blocked_list(registry_data)

            entry_matches: dict[str, list] = {}
            device_matches: dict[str, list] = {}
            for entry in registry_data.devices.values():
                if entry.serial_pattern is None:
                    continue  # CAN devices have no serial_pattern
                matches = match_devices(entry.serial_pattern, devices)
                entry_matches[entry.key] = matches
                for device in matches:
                    device_matches.setdefault(device.filename, []).append(entry)

            duplicate_entry_keys = {key for key, matches in entry_matches.items() if len(matches) > 1}

            registered_devices: list[tuple[object, object]] = []
            new_devices: list = []
            blocked_devices: list[tuple[object, str]] = []
            duplicate_devices: list[tuple[object, list]] = []

            for device in devices:
                blocked_reason = _blocked_reason_for_filename(device.filename, blocked_list)
                if blocked_reason or not is_supported_device(device.filename):
                    blocked_devices.append((device, blocked_reason or "Unsupported USB device"))
                    continue

                entries = device_matches.get(device.filename, [])
                if entries and (
                    len(entries) > 1 or any(entry.key in duplicate_entry_keys for entry in entries)
                ):
                    duplicate_devices.append((device, entries))
                    continue

                if entries:
                    registered_devices.append((device, entries[0]))
                else:
                    new_devices.append(device)

            summary = (
                f"{len(devices)} USB devices found: {len(registered_devices)} registered, "
                f"{len(new_devices)} new, {len(blocked_devices)} blocked, "
                f"{len(duplicate_devices)} duplicate"
            )
            out.info("Discovery", summary)

            selectable: list[tuple[object, object | None]] = []
            if registered_devices:
                out.info("Discovery", f"Registered devices ({len(registered_devices)}):")
                for device, entry in registered_devices:
                    idx = len(selectable) + 1
                    label = f"{idx}. {device.filename}"
                    detail = f"{entry.name} ({entry.mcu})"
                    out.device_line("REG", label, detail)
                    selectable.append((device, entry))

            if new_devices:
                out.info("Discovery", f"New devices ({len(new_devices)}):")
                for device in new_devices:
                    idx = len(selectable) + 1
                    label = f"{idx}. {device.filename}"
                    out.device_line("NEW", label, "Unregistered device")
                    selectable.append((device, None))

            if duplicate_devices:
                out.info("Discovery", f"Duplicate devices (not eligible) ({len(duplicate_devices)}):")
                for device, entries in duplicate_devices:
                    names = ", ".join(entry.name for entry in entries)
                    out.device_line("DUP", device.filename, f"Matches: {names}")

            if blocked_devices:
                out.info("Discovery", f"Blocked devices (not eligible) ({len(blocked_devices)}):")
                for device, reason in blocked_devices:
                    out.device_line("BLK", device.filename, reason)

            # Check for CAN interfaces to offer CAN path
            from .discovery import get_can_interfaces

            can_interfaces = get_can_interfaces()

            if not selectable and not can_interfaces:
                out.error("No eligible devices available to add.")
                return 1

            choices = ["0"] + [str(i) for i in range(1, len(selectable) + 1)]
            if can_interfaces:
                out.info("Discovery", f"CAN interfaces detected: {', '.join(can_interfaces)}")
                out.info("Discovery", "  C. Add CAN bus device")
                choices.append("c")

            if not selectable:
                # Only CAN option available
                out.info("Discovery", "No USB devices eligible. Use C for CAN device.")

            choice = _get_menu_choice(
                choices,
                out,
                max_attempts=3,
                prompt="Select device number (0/q to cancel): ",
            )
            if choice is None or choice == "0":
                out.info("Registry", "Add device cancelled")
                return 0

        if choice == "c":
            # CAN transport path
            # a. Select CAN interface
            if len(can_interfaces) == 1:
                can_iface = can_interfaces[0]
                out.info("CAN", f"Using interface: {can_iface}")
            else:
                for i, iface in enumerate(can_interfaces, 1):
                    out.info("CAN", f"  {i}. {iface}")
                iface_choice = out.prompt(f"Select interface (1-{len(can_interfaces)})")
                try:
                    can_iface = can_interfaces[int(iface_choice) - 1]
                except (ValueError, IndexError):
                    out.error("Invalid interface selection")
                    return 1

            # b. Scan CAN bus
            from .discovery import scan_can_devices

            gc = registry.load().global_config
            out.info("CAN", f"Scanning CAN bus on {can_iface}...")
            can_devices = scan_can_devices(can_iface, gc.katapult_dir)

            # c. Show discovered UUIDs and let user select or enter manually
            # Filter to only unregistered UUIDs
            registry_data = registry.load()
            registered_uuids = {
                e.canbus_uuid
                for e in registry_data.devices.values()
                if e.canbus_uuid is not None
            }
            new_can = [d for d in can_devices if d.uuid not in registered_uuids]

            if new_can:
                out.info("CAN", f"Found {len(new_can)} unregistered CAN device(s):")
                for i, dev in enumerate(new_can, 1):
                    out.info("CAN", f"  {i}. UUID: {dev.uuid}  Application: {dev.application}")
                out.info("CAN", "  0. Enter UUID manually")
                uuid_choice = out.prompt(f"Select device (1-{len(new_can)}, 0 for manual)")
                if uuid_choice == "0":
                    can_uuid_val = _prompt_can_uuid(out)
                    if can_uuid_val is None:
                        return 1
                else:
                    try:
                        can_uuid_val = new_can[int(uuid_choice) - 1].uuid
                    except (ValueError, IndexError):
                        out.error("Invalid selection")
                        return 1
            else:
                if can_devices:
                    out.info("CAN", "All discovered CAN devices are already registered")
                else:
                    out.info("CAN", "No CAN devices found on bus")
                out.info("CAN", "You can enter a UUID manually")
                can_uuid_val = _prompt_can_uuid(out)
                if can_uuid_val is None:
                    return 1

            # Set CAN-specific state and continue to common wizard steps
            selected = None  # No USB DiscoveredDevice for CAN
            existing_entry = None
            serial_pattern = None
            canbus_uuid_value = can_uuid_val
            canbus_interface_value = can_iface
            is_can_transport = True
        else:
            selected, existing_entry = selectable[int(choice) - 1]
            from .screen import truncate_serial

            out.info("Selected", truncate_serial(selected.filename))

    # Check if selected device is already registered
    if existing_entry is not None:
        existing = existing_entry
        if not out.confirm(
            f"Device already registered as '{existing.name}'. Remove and re-add this device?",
            default=False,
        ):
            out.info("Registry", "Add device cancelled")
            return 0
        registry.remove(existing.key)
        out.success(f"Removed existing device '{existing.name}'")
        _remove_cached_config(existing.key, out, prompt=True, device_name=existing.name)
        registry_data = registry.load()
        replacing_existing_device = True

    out.step_divider()

    # Step 3: Global config (first run only)
    if not registry_data.devices and not replacing_existing_device:
        out.info("Setup", "First device registration - configuring global paths...")
        klipper_dir = out.prompt("Klipper source directory", default="~/klipper")
        katapult_dir = out.prompt("Katapult source directory", default="~/katapult")
        registry.save_global(
            GlobalConfig(
                klipper_dir=klipper_dir,
                katapult_dir=katapult_dir,
            )
        )
        out.success("Global configuration saved")
    elif not registry_data.devices and replacing_existing_device:
        out.info("Setup", "Keeping existing global configuration")

    out.step_divider()

    # Step 4: Display name (device key is auto-generated)
    registry_data = registry.load()
    existing_names = {e.name.lower() for e in registry_data.devices.values()}
    display_name = None
    for _attempt in range(3):
        name_input = out.prompt("Display name (e.g., 'Octopus Pro v1.1')")
        if not name_input:
            out.warn("Display name cannot be empty.")
            continue
        if name_input.lower() in existing_names:
            out.warn(f"You already have a device named '{name_input}'. Enter a different name.")
            continue
        display_name = name_input
        break
    if display_name is None:
        out.error("Too many invalid inputs.")
        return 1

    # Auto-generate device key from display name
    from .validation import generate_device_key

    try:
        device_key = generate_device_key(display_name, registry)
    except ValueError:
        out.error("Display name must contain at least one letter or number.")
        return 1

    out.step_divider()

    # Step 6: MCU type
    if is_can_transport:
        out.info("CAN", "CAN devices require manual MCU type entry")
        mcu = out.prompt("MCU type (e.g., stm32h723, rp2040)")
    else:
        detected_mcu = extract_mcu_from_serial(selected.filename)
        if detected_mcu:
            if out.confirm(f"Detected MCU: {detected_mcu}. Correct?", default=True):
                mcu = detected_mcu
            else:
                mcu = out.prompt("Enter MCU type")
        else:
            out.info("Discovery", "Could not auto-detect MCU from device name.")
            mcu = out.prompt("MCU type (e.g., stm32h723, rp2040)")

    if not mcu:
        out.error("MCU type is required.")
        return 1

    # Step 7: Serial pattern (USB only)
    if not is_can_transport:
        serial_pattern = generate_serial_pattern(selected.filename)
        out.info("Registry", f"Serial pattern: {serial_pattern}")

        # Check if pattern matches multiple connected devices (duplicate USB IDs)
        pattern_matches = match_devices(serial_pattern, devices)
        if len(pattern_matches) > 1:
            out.error(
                "Serial pattern matches multiple connected devices."
                " Unplug duplicates and retry."
            )
            for device in pattern_matches:
                out.device_line("DUP", device.filename, "Duplicate USB ID")
            return 1

        # Check for pattern overlap with existing devices
        for existing_key, existing_entry in registry_data.devices.items():
            if existing_entry.serial_pattern is None:
                continue  # CAN devices have no serial_pattern
            if existing_entry.serial_pattern == serial_pattern:
                out.error(
                    f"Serial pattern already registered to '{existing_key}'. "
                    "Remove it first or choose a different device."
                )
                return 1
            from .discovery import _prefix_variants

            existing_variants = _prefix_variants(existing_entry.serial_pattern)
            if any(fnmatch.fnmatch(selected.filename, v) for v in existing_variants):
                out.error(
                    f"Selected device matches existing entry '{existing_key}'. "
                    "Remove it first or replace it."
                )
                return 1

    out.step_divider()

    # Step 7.5: MCU name selection
    from .moonraker import get_mcu_serial_map, match_serial_to_mcu_name

    mcu_name = None
    mcu_serials = get_mcu_serial_map()

    if is_can_transport:
        # CAN devices: skip serial-based auto-match, go directly to picker
        if mcu_serials is not None:
            mcu_name = _pick_mcu_name(mcu_serials, out)
        else:
            raw = out.prompt("Klipper MCU name (optional, press Enter to skip)")
            mcu_name = raw.strip() if raw and raw.strip() else None
    elif mcu_serials is not None:
        # USB path: try auto-match first
        auto_match = match_serial_to_mcu_name(serial_pattern, mcu_serials)
        if auto_match:
            if out.confirm(f"Detected Klipper MCU name: '{auto_match}'. Correct?", default=True):
                mcu_name = auto_match
            else:
                mcu_name = _pick_mcu_name(mcu_serials, out)
        else:
            out.info("MCU Name", "Could not auto-detect MCU name from serial path")
            mcu_name = _pick_mcu_name(mcu_serials, out)
    else:
        out.warn("Moonraker unreachable - cannot auto-detect MCU name")
        raw = out.prompt("Klipper MCU name (optional, press Enter to skip)")
        mcu_name = raw.strip() if raw and raw.strip() else None

    out.step_divider()

    # Step 8: Flash method
    from .validation import find_flash_method_pair

    if is_can_transport:
        # CAN devices: auto-select Katapult CAN
        bootloader_method = "can"
        flash_command = "katapult_can"
        pair = find_flash_method_pair(bootloader_method, flash_command)
        out.info("Flash Method", f"Auto-selected: {pair.name}")

        # CAN devices: prompt for device role (affects Flash All ordering)
        print("\n  Device role (affects Flash All ordering):")
        print("    1) Toolhead")
        print("    2) Bridge")
        print("    3) Skip (no role)")
        try:
            role_choice = input("  Select role [1]: ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            role_choice = "3"
        role_map = {"1": "toolhead", "2": "bridge", "3": None}
        device_role = role_map.get(role_choice, "toolhead")
    else:
        from .tui import _flash_method_picker_overlay

        result = _flash_method_picker_overlay(
            None,
            None,
            device_name=display_name,
            mcu=mcu,
            is_can_device=False,
        )
        if result is None:
            out.info("Registry", "Add device cancelled")
            return 1

        bootloader_method, flash_command = result
        pair = find_flash_method_pair(bootloader_method, flash_command)
        out.info("Flash Method", pair.name)
        device_role = None  # USB devices: no role by default

    # Step 8a: Sub-field prompts (driven by pair.required_sub_fields)
    bootloader_baud = None
    uf2_mount_path = None
    sdcard_board = None
    canbus_uuid = None

    for field_key in pair.required_sub_fields:
        # CAN transport: skip fields already set from CAN flow
        if is_can_transport and field_key == "canbus_uuid":
            canbus_uuid = canbus_uuid_value
            continue
        if is_can_transport and field_key == "canbus_interface":
            continue  # canbus_interface_value already set from CAN flow

        default = SUB_FIELD_DEFAULTS.get(field_key)
        if default is not None:
            # Auto-accept default, echo
            if field_key == "bootloader_baud":
                bootloader_baud = default
            out.info("Flash Method", f"Using default {SUB_FIELD_PROMPTS[field_key]}: {default}")
            continue

        # Required: prompt until value or cancel
        value = _prompt_required_field(
            SUB_FIELD_PROMPTS[field_key], out,
            validator=SUB_FIELD_VALIDATORS.get(field_key),
        )
        if value is None:
            out.info("Registry", "Add device cancelled")
            return 1
        if field_key == "uf2_mount_path":
            uf2_mount_path = value
        elif field_key == "sdcard_board":
            sdcard_board = value
        elif field_key == "canbus_uuid":
            canbus_uuid = value
        elif field_key == "canbus_interface":
            canbus_interface_value = value
        elif field_key == "bootloader_baud":
            bootloader_baud = int(value)

    out.step_divider()

    # Step 9: Flashable toggle
    if flash_command is None:
        # Build Only -- auto-set non-flashable
        is_flashable = False
        out.info("Flash Method", "Build Only selected -- device excluded from flash operations")
    else:
        exclude_from_flash = out.confirm(
            "Exclude this device from flashing?", default=False
        )
        is_flashable = not exclude_from_flash

    out.step_divider()

    # Step 11: Create and save
    entry = DeviceEntry(
        key=device_key,
        name=display_name,
        mcu=mcu,
        serial_pattern=serial_pattern,
        bootloader_method=bootloader_method,
        flash_command=flash_command,
        bootloader_baud=bootloader_baud,
        uf2_mount_path=uf2_mount_path,
        sdcard_board=sdcard_board,
        canbus_uuid=canbus_uuid if not is_can_transport else canbus_uuid_value,
        canbus_interface=canbus_interface_value if is_can_transport else None,
        flashable=is_flashable,
        mcu_name=mcu_name,
        role=device_role,
    )
    registry.add(entry)
    out.success(f"Device '{display_name}' added successfully.")

    # Offer to run menuconfig for the newly registered device
    out.step_divider()
    if out.confirm("Run menuconfig now to configure firmware?", default=True):
        try:
            from .build import run_menuconfig
            from .config import ConfigManager

            data = registry.load()
            if data.global_config is None:
                out.warn("Cannot run menuconfig: global config not set")
                return 0

            klipper_dir = data.global_config.klipper_dir
            config_mgr = ConfigManager(device_key, klipper_dir)
            had_cache = config_mgr.has_cached_config()

            # Load or start fresh config
            if config_mgr.load_cached_config():
                out.info("Config", f"Loaded cached config for '{entry.name}'")
            else:
                config_mgr.clear_klipper_config()
                out.info("Config", "No cached config found, starting fresh")

            out.info("Config", "Launching menuconfig...")
            ret_code, was_saved = run_menuconfig(klipper_dir, str(config_mgr.klipper_config_path))

            if ret_code != 0:
                out.warn("menuconfig exited with errors, config not saved")
            elif was_saved:
                # DON'T save to cache yet -- need to validate MCU first
                try:
                    is_match, actual_mcu = config_mgr.validate_mcu(entry.mcu)
                    while not is_match:
                        choice = out.mcu_mismatch_choice(actual_mcu, entry.mcu, entry.name)
                        if choice == "r":
                            out.info("Config", "Re-launching menuconfig...")
                            ret_code2, was_saved2 = run_menuconfig(
                                klipper_dir, str(config_mgr.klipper_config_path)
                            )
                            if was_saved2:
                                is_match, actual_mcu = config_mgr.validate_mcu(entry.mcu)
                            else:
                                out.info("Config", "menuconfig exited without saving")
                                break
                        elif choice == "d":
                            # Restore old cache or delete klipper .config
                            if had_cache:
                                config_mgr.load_cached_config()
                                out.info("Config", "Restored previous cached config")
                            else:
                                config_mgr.clear_klipper_config()
                                out.info("Config", "Discarded config (no previous cache)")
                            break
                        else:  # 'k'
                            config_mgr.save_cached_config()
                            out.info("Config", "Keeping mismatched config")
                            break
                    else:
                        # MCU matched (while condition became False) -- save now
                        config_mgr.save_cached_config()
                        out.success(f"Config saved for '{entry.name}'")
                except Exception:
                    pass  # Non-blocking
            else:
                out.info("Config", "menuconfig exited without saving")
        except Exception as exc:
            out.warn(f"menuconfig failed: {exc}")

    return 0


def _warn_startup_klipper_dir(registry, out) -> None:
    """Emit startup warning if configured klipper_dir is invalid."""
    try:
        data = registry.load()
    except Exception:
        return

    global_config = getattr(data, "global_config", None)
    if global_config is None:
        return

    klipper_dir = getattr(global_config, "klipper_dir", None)
    if not klipper_dir:
        return

    klipper_path = Path(klipper_dir).expanduser()
    if not klipper_path.is_dir():
        out.warn(f"Startup: configured klipper_dir not found: {klipper_path}")
        out.warn("Use Settings (C) to update paths before building or flashing.")
        return

    if not (klipper_path / "Makefile").is_file():
        out.warn(f"Startup: Klipper Makefile missing in configured path: {klipper_path}")
        out.warn("Use Settings (C) to point to a valid Kalico/Klipper source directory.")


def main() -> int:
    """Main entry point — launch TUI."""
    from .safety import check_not_root

    check_not_root()

    if not sys.stdin.isatty():
        print("kalico-flash requires an interactive terminal.", file=sys.stderr)
        return 1

    from .errors import KlipperFlashError
    from .output import CliOutput
    from .registry import Registry
    from .safety import resolve_registry_path

    out = CliOutput()
    registry_path = resolve_registry_path()
    registry = Registry(registry_path)
    _warn_startup_klipper_dir(registry, out)

    try:
        from .tui import run_menu

        return run_menu(registry, out)
    except KeyboardInterrupt:
        out.warn("Aborted.")
        return 130
    except KlipperFlashError as e:
        out.error(str(e))
        return 1
    except Exception as e:
        out.error(f"Unexpected error: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
