"""``cmd_flash`` -- build and flash firmware for a single registered device.

UI-free: emits via ``Emitter`` and asks via ``DecisionProvider``.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import cast

from ..blocklist import (
    blocked_reason_for_entry,
    blocked_reason_for_filename,
    build_blocked_list,
)
from ..build import TIMEOUT_BUILD, run_build
from ..decisions import (
    ChooseDeviceDecision,
    ConfirmDecision,
    DecisionProvider,
    DeviceChoice,
)
from ..discovery import (
    extract_mcu_from_serial,
    find_registered_devices,
    is_supported_device,
    match_device,
    match_devices,
    preflight_can_interface,
    scan_serial_devices,
)
from ..errors import ERROR_TEMPLATES, DiscoveryError, get_recovery_text
from ..events import Emitter
from ..flash_steps import (
    SafetyGate,
    emit_host_and_mcu_versions,
    load_and_validate_config,
    moonraker_safety_gate,
    resolve_ccache_usage,
    resolve_target_mcu_version,
    run_flash_sequence,
)
from ..flasher import verify_device_path
from ..moonraker import (
    detect_firmware_flavor,
    get_host_klipper_version,
    get_mcu_version_for_device,
    get_mcu_versions,
    is_mcu_outdated,
)
from ..preflight import (
    check_firmware_artifact,
    preflight_flash,
    validate_device_flash_config,
)
from ..safety import check_dirty_repo, detect_downgrade
from ..service import (
    _is_service_active,
    acquire_sudo,
    klipper_service_stopped,
    verify_passwordless_sudo,
)
from ._common import _short_path


def cmd_flash(
    registry, device_key, em: Emitter, decider: DecisionProvider, skip_menuconfig: bool = False
) -> int:
    """Build and flash firmware for a registered device.

    Orchestrates the full workflow:
    1. [Discovery] Scan USB devices, select target
    2. [Config] Load/edit menuconfig, validate MCU
    3. [Build] Compile firmware with timeout
    4. [Flash] Stop Klipper, flash device, restart Klipper

    Returns:
        0 on success, 1 on failure
    """
    # TTY check for interactive mode
    if device_key is None and not sys.stdin.isatty():
        em.error("Interactive terminal required. Run from SSH terminal.")
        return 1

    # Load registry data
    data = registry.load()
    if data.global_config is None:
        em.error("Global config not set. Press A to add a device first.")
        return 1
    blocked_list = build_blocked_list(data)

    # Fetch version information early for display in device selection
    mcu_versions = get_mcu_versions()
    host_version = get_host_klipper_version(data.global_config.klipper_dir)

    # === Phase 1: Discovery ===
    em.phase("Discovery", "Scanning for USB devices...")
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
        reason = blocked_reason_for_entry(entry, blocked_list)
        if reason:
            blocked_entries[entry.key] = reason

    if device_key is None:
        # Interactive mode: select from connected registered devices
        if not usb_devices:
            em.error("No USB devices found. Connect a board and try again.")
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
                em.error_with_recovery(
                    "Duplicate USB IDs",
                    "Registered device(s) match multiple connected USB IDs",
                    recovery=(
                        "1. Unplug duplicate devices so only one remains\n"
                        "2. Reconnect and retry\n"
                        "3. If duplicates persist, update registry to unique devices"
                    ),
                )
                em.phase("Discovery", "Blocked devices with duplicate USB IDs:")
                for key, devices in duplicate_matches.items():
                    entry = data.devices.get(key)
                    if entry is None:
                        continue
                    details = ", ".join(d.filename for d in devices)
                    em.device_line("DUP", f"{entry.name} ({entry.mcu}) [duplicate]", details)
                return 1

            if blocked_entries:
                blocked_connected = [
                    (entry, device)
                    for entry, device in find_registered_devices(usb_devices, data.devices)[0]
                    if entry.key in blocked_entries
                ]
                if blocked_connected:
                    em.error_with_recovery(
                        "Blocked devices",
                        "Connected registered devices are blocked and cannot be flashed",
                        recovery=(
                            "1. Remove blocked entries from devices.json\n"
                            "2. Or connect a flashable device"
                        ),
                    )
                    em.phase("Discovery", "Blocked registered devices:")
                    for entry, _device in blocked_connected:
                        reason = blocked_entries.get(entry.key, "Blocked by policy")
                        em.device_line("BLK", f"{entry.name} ({entry.mcu}) [blocked]", reason)
                    return 1

            recovery = (
                "1. Press D to refresh devices\n"
                "2. Check USB connections\n"
                "3. Press A to add a device"
            )
            em.error_with_recovery(
                "Device not found",
                "No registered devices connected",
                recovery=recovery,
            )
            em.phase("Discovery", "Found USB devices but none are registered:")
            for device in usb_devices:
                blocked_reason = blocked_reason_for_filename(device.filename, blocked_list)
                if blocked_reason or not is_supported_device(device.filename):
                    em.device_line(
                        "BLK",
                        device.filename,
                        blocked_reason or "Unsupported USB device",
                    )
                else:
                    em.device_line("NEW", device.filename, "Unregistered device")
            return 1

        if duplicate_matches:
            em.phase("Discovery", "Blocked devices with duplicate USB IDs:")
            for key, devices in duplicate_matches.items():
                entry = data.devices.get(key)
                if entry is None:
                    continue
                details = ", ".join(d.filename for d in devices)
                em.device_line("DUP", f"{entry.name} ({entry.mcu}) [duplicate]", details)

        # Filter to only flashable devices for selection
        flashable_matched = [(e, d) for e, d in matched if e.flashable]
        excluded_matched = [(e, d) for e, d in matched if not e.flashable]

        # Show excluded devices with note if any
        if excluded_matched:
            em.phase("Discovery", "Excluded devices (not selectable):")
            for entry, device in excluded_matched:
                em.device_line("REG", f"{entry.name} ({entry.mcu}) [excluded]", device.filename)

        if blocked_entries:
            blocked_connected = [
                (entry, device)
                for entry, device in find_registered_devices(usb_devices, data.devices)[0]
                if entry.key in blocked_entries
            ]
            if blocked_connected:
                em.phase("Discovery", "Blocked devices (not selectable):")
                for entry, _device in blocked_connected:
                    reason = blocked_entries.get(entry.key, "Blocked by policy")
                    em.device_line("BLK", f"{entry.name} ({entry.mcu}) [blocked]", reason)

        if not flashable_matched:
            template = ERROR_TEMPLATES["device_excluded"]
            em.error_with_recovery(
                template["error_type"],
                "All connected devices are excluded from flashing",
                recovery=get_recovery_text("device_excluded"),
            )
            return 1

        # Show numbered list of connected flashable devices
        em.phase("Discovery", f"Found {len(flashable_matched)} flashable device(s):")
        for i, (entry, device) in enumerate(flashable_matched):
            em.device_line(str(i + 1), f"{entry.name} ({entry.mcu})", device.filename)
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
                    em.info("", f"     MCU software version: {version}")

        # Show host Klipper version before selection
        if host_version:
            em.info("Version", f"Host: {detect_firmware_flavor(host_version)} {host_version}")

        # Single device: auto-select with confirmation
        if len(flashable_matched) == 1:
            entry, usb_device = flashable_matched[0]
            if decider.confirm(
                ConfirmDecision(
                    id="flash_single", message=f"Flash {entry.name}?", default=True
                )
            ):
                device_key = entry.key
                device_path = usb_device.path
            else:
                em.phase("Discovery", "Cancelled")
                return 0
        else:
            # Multiple devices: prompt for selection
            dev_choices = [
                DeviceChoice(key=str(i + 1), label=f"{e.name} ({e.mcu})")
                for i, (e, d) in enumerate(flashable_matched)
            ]
            choice = decider.choose_device(
                ChooseDeviceDecision(
                    prompt="Select device number (0/q to cancel): ",
                    choices=dev_choices,
                )
            )
            if choice is None:
                em.phase("Discovery", "Cancelled")
                return 0
            idx = int(choice) - 1
            entry, usb_device = flashable_matched[idx]
            device_key = entry.key
            device_path = usb_device.path
    else:
        # Verify device exists and is connected
        entry = registry.get(device_key)
        if entry is None:
            template = ERROR_TEMPLATES["device_not_registered"]
            em.error_with_recovery(
                template["error_type"],
                template["message_template"].format(device=device_key),
                context={"device": device_key},
                recovery=get_recovery_text("device_not_registered"),
            )
            return 1

        blocked_reason = blocked_reason_for_entry(entry, blocked_list)
        if blocked_reason:
            em.error_with_recovery(
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
            em.error_with_recovery(
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
                em.error_with_recovery(
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
                em.error_with_recovery(
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
                    em.device_line("DUP", device.filename, "Duplicate USB ID")
                return 1

            # Find matching USB device
            usb_device = match_device(entry.serial_pattern, usb_devices)
            if usb_device is None:
                template = ERROR_TEMPLATES["device_not_connected"]
                em.error_with_recovery(
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
            em.warn(f"MCU mismatch: USB device reports '{usb_mcu}' but registry has '{entry.mcu}'")
            if not decider.confirm(
                ConfirmDecision(
                    id="mcu_serial_mismatch",
                    message="Continue with flash anyway?",
                    default=False,
                )
            ):
                return 0

    # Load the device entry for the rest of the workflow
    entry = registry.get(device_key)
    global_config = data.global_config
    klipper_dir = global_config.klipper_dir
    katapult_dir = global_config.katapult_dir

    # Validate flash configuration (no fallback chain per user decision)
    if not validate_device_flash_config(entry, em):
        return 1

    # Preflight for selected flash command (no fallback)
    if not preflight_flash(em, klipper_dir, katapult_dir, entry.flash_command):
        return 1

    # Target display
    if entry.is_can_device:
        can_iface = entry.canbus_interface or 'can0'
        em.phase(
            "Discovery",
            f"Target: {entry.name} ({entry.mcu}) via CAN"
            f" {can_iface} [{entry.canbus_uuid}]",
        )
    else:
        em.phase("Discovery", f"Target: {entry.name} ({entry.mcu}) at {_short_path(device_path)}")

    em.step_divider()

    # === Moonraker Safety Check ===
    gate = moonraker_safety_gate(em=em, decider=decider, label="Flash")
    if gate == SafetyGate.CANCELLED:
        return 0
    if gate == SafetyGate.BLOCKED:
        return 1

    em.step_divider()

    # === Version Information ===
    # mcu_versions and host_version already fetched earlier for device selection display
    if host_version:
        if mcu_versions:
            target_mcu = resolve_target_mcu_version(entry, mcu_versions)
            emit_host_and_mcu_versions(em, host_version, mcu_versions, target_mcu)

            # Check if target MCU is outdated or already current
            if target_mcu and target_mcu in mcu_versions:
                if is_mcu_outdated(host_version, mcu_versions[target_mcu]):
                    em.warn("MCU firmware is behind host Klipper - update recommended")
                elif not decider.confirm(
                    ConfirmDecision(
                        id="reflash_up_to_date",
                        message="MCU firmware is already up-to-date. Continue anyway?",
                        default=False,
                    )
                ):
                    em.phase("Flash", "Cancelled - firmware already current")
                    return 0
        else:
            em.phase("Version", f"Host: {detect_firmware_flavor(host_version)} {host_version}")
            em.warn("MCU versions unavailable (Klipper may not be running)")
    elif mcu_versions:
        # Have MCU versions but not host version (unusual)
        em.warn("Host firmware version unavailable")
    # If neither available, skip version display silently (Moonraker down handled above)

    em.step_divider()

    # === Phase 2: Config ===
    em.phase("Config", f"Loading config for {entry.name}...")
    outcome = load_and_validate_config(
        entry=entry,
        device_key=device_key,
        klipper_dir=klipper_dir,
        em=em,
        decider=decider,
        skip_menuconfig=skip_menuconfig,
        require_menuconfig=False,
    )
    if not outcome.ok:
        return outcome.exit_code

    em.step_divider()

    # === ccache Installation Check ===
    use_ccache = resolve_ccache_usage(
        registry=registry, global_config=data.global_config, em=em, decider=decider
    )

    # === Phase 3: Build ===
    # Safety: dirty repo and downgrade warnings
    dirty_result = check_dirty_repo(host_version)
    if dirty_result.is_dirty:
        em.phase("Safety", "Warning: Klipper repo has uncommitted changes")

    if host_version and mcu_versions:
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
                    em.phase(
                        "Safety",
                        "Warning: MCU firmware is newer than host (downgrade)",
                    )
            except (ValueError, TypeError):
                pass  # Non-parseable versions, skip check

    em.phase("Build", "Running make clean + make...")
    build_result = run_build(klipper_dir, timeout=TIMEOUT_BUILD, use_ccache=use_ccache)

    if not build_result.success:
        template = ERROR_TEMPLATES["build_failed"]
        em.error_with_recovery(
            template["error_type"],
            template["message_template"].format(device=device_key),
            context={"device": device_key},
            recovery=template["recovery_template"],
        )
        return 1

    artifact_error, artifact_warning = check_firmware_artifact(
        build_result.firmware_path,
        build_result.firmware_size,
    )
    if artifact_error:
        em.error_with_recovery(
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
        em.warn(f"Build artifact warning: {artifact_warning}")

    # build succeeded, so firmware_path is populated (BuildResult types it Optional)
    firmware_path = cast(str, build_result.firmware_path)
    size_kb = build_result.firmware_size / 1024 if build_result.firmware_size else 0
    em.phase(
        "Build",
        f"Firmware ready: {size_kb:.1f} KB in {build_result.elapsed_seconds:.1f}s",
    )

    # Show ccache stats if available
    if build_result.ccache_stats:
        hits = build_result.ccache_stats.total_hits
        misses = build_result.ccache_stats.cache_miss
        rate_pct = int(build_result.ccache_stats.hit_rate * 100)
        em.phase("Build", f"Cache: {hits} hits, {misses} misses ({rate_pct}% hit rate)")

    em.step_divider()

    # === Phase 4: Flash ===
    if entry.is_can_device:
        em.phase("Flash", "CAN device -- skipping USB path verification")
    else:
        em.phase("Flash", "Verifying device connection...")
        try:
            verify_device_path(device_path)
        except DiscoveryError as e:
            em.error_with_recovery(
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
        em.phase("Flash", "Sudo authentication required for service management")
        if not acquire_sudo():
            em.error("Failed to acquire sudo credentials. Cannot manage Klipper service.")
            return 1

    if _is_service_active():
        em.phase("Flash", "Stopping Klipper...")
    flash_start = time.monotonic()
    service_restart_failed = False

    try:
        with klipper_service_stopped(em=em) as svc_state:
            step = run_flash_sequence(
                entry=entry,
                device_path=device_path,
                firmware_path=firmware_path,
                config=global_config,
                klipper_dir=klipper_dir,
                katapult_dir=katapult_dir,
                em=em,
                decider=decider,
            )
            if not step.bootloader_ok:
                em.error(f"Bootloader entry failed: {step.error_message}")
                return 1

        # Context manager exited - report actual restart outcome
        if svc_state.will_restart and svc_state.restart_succeeded:
            em.phase("Service", "Klipper restarted")
        elif svc_state.will_restart and svc_state.restart_succeeded is False:
            template = ERROR_TEMPLATES["service_start_failed"]
            em.error_with_recovery(
                template["error_type"],
                template["message_template"],
                recovery=template["recovery_template"],
            )
            service_restart_failed = True
    except Exception as e:
        template = ERROR_TEMPLATES["flash_failed"]
        em.error_with_recovery(
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

    if step.flash_ok and step.verify_ok:
        # Record flash timestamp
        registry.update_device(
            device_key,
            last_flash_timestamp=datetime.now().replace(microsecond=0).isoformat(),
        )
        em.success(f"Flashed {entry.name} via {step.method} in {flash_elapsed:.1f}s")
        if step.device_path_new:
            em.phase("Verify", f"Device confirmed at: {step.device_path_new}")
        elif entry.is_can_device:
            em.phase("Verify", f"Device confirmed on CAN bus [{entry.canbus_uuid}]")
        return 0

    elif step.flash_ok and not step.verify_ok:
        # Flash appeared to succeed but device didn't reappear correctly
        em.warn(f"Device verification failed: {step.error_reason}")
        if entry.is_can_device:
            em.error_with_recovery(
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
            if step.device_path_new:
                template = ERROR_TEMPLATES["verification_wrong_prefix"]
            else:
                template = ERROR_TEMPLATES["verification_timeout"]
            em.error_with_recovery(
                template["error_type"],
                template["message_template"],
                context={"device": device_key, "pattern": entry.serial_pattern},
                recovery=template["recovery_template"],
            )
        return 1

    else:
        # flash failed
        template = ERROR_TEMPLATES["flash_failed"]
        em.error_with_recovery(
            template["error_type"],
            template["message_template"].format(device=device_key),
            context={
                "device": device_key,
                "method": step.method,
                "error": cast(str, step.error_message),
            },
            recovery=template["recovery_template"],
        )
        return 1
