"""``cmd_flash_all`` -- build and flash all registered flashable devices.

Plus its batch-ordering/dedupe helpers. UI-free: emits via ``Emitter`` and asks
via ``DecisionProvider``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from datetime import datetime
from typing import cast

from ..blocklist import blocked_reason_for_entry, build_blocked_list
from ..build import run_build
from ..config import ConfigManager
from ..decisions import ConfirmDecision, DecisionProvider
from ..discovery import (
    extract_mcu_from_serial,
    match_device,
    match_devices,
    preflight_can_interface,
    scan_serial_devices,
)
from ..errors import ConfigError
from ..events import Emitter
from ..flash_steps import (
    SafetyGate,
    moonraker_safety_gate,
    resolve_ccache_usage,
    run_flash_sequence,
)
from ..models import BatchDeviceResult
from ..moonraker import (
    detect_firmware_flavor,
    get_host_klipper_version,
    get_mcu_canbus_map,
    get_mcu_version_for_device,
    get_mcu_versions,
    is_mcu_outdated,
)
from ..preflight import (
    check_firmware_artifact,
    get_device_flash_config_issue,
    preflight_build,
    preflight_flash,
)
from ..safety import check_dirty_repo, detect_downgrade
from ..service import (
    _is_service_active,
    acquire_sudo,
    klipper_service_stopped,
    refresh_sudo_timestamp,
    verify_passwordless_sudo,
)


def _check_duplicate_path(real_path: str, used_paths: set[str]) -> bool:
    """Check if a resolved USB path was already targeted.

    Returns True if duplicate (path already in used_paths).
    Adds path to used_paths if not a duplicate.
    """
    if real_path in used_paths:
        return True
    used_paths.add(real_path)
    return False


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


def cmd_flash_all(registry, em: Emitter, decider: DecisionProvider) -> int:
    """Build and flash firmware for all registered flashable devices.

    Orchestrates a 5-stage batch workflow:
    1. Validate all devices have cached configs
    2. Version check — prompt if all MCUs already match host
    3. Build all firmware quietly, copy to temp dir
    4. Flash all inside single klipper_service_stopped()
    5. Print summary table

    One device failure never blocks others from being processed.

    Returns:
        0 if all devices passed, 1 if any failed.
    """
    # Load registry
    data = registry.load()
    if data.global_config is None:
        em.error("Global config not set. Press A to add a device first.")
        return 1

    global_config = data.global_config
    klipper_dir = global_config.klipper_dir
    katapult_dir = global_config.katapult_dir

    # === Preflight: Build prerequisites (SAFE-01) ===
    if not preflight_build(em, klipper_dir):
        return 1

    # === Preflight: Moonraker safety check (SAFE-02) ===
    gate = moonraker_safety_gate(em=em, decider=decider, label="Flash All")
    if gate == SafetyGate.CANCELLED:
        return 0
    if gate == SafetyGate.BLOCKED:
        return 1

    em.step_divider()

    # === Stage 1: Validate cached configs ===
    em.phase("Flash All", "Validating cached configs...")

    flashable_devices = _sort_flash_all_devices(
        [e for e in data.devices.values() if e.flashable]
    )

    if not flashable_devices:
        em.error("No flashable devices registered. Press A to register a board.")
        return 1

    # Filter blocked devices
    blocked_list = build_blocked_list(data)
    blocked_devices: list[tuple] = []
    unblocked_devices: list = []
    for entry in flashable_devices:
        reason = blocked_reason_for_entry(entry, blocked_list)
        if reason:
            blocked_devices.append((entry, reason))
        else:
            unblocked_devices.append(entry)

    if blocked_devices:
        for entry, reason in blocked_devices:
            em.warn(f"Skipping {entry.name}: {reason}")

    if not unblocked_devices:
        em.error("All flashable devices are blocked. Nothing to flash.")
        return 1

    flashable_devices = unblocked_devices

    # Check cached configs exist
    missing_configs: list[str] = []
    for entry in flashable_devices:
        config_mgr = ConfigManager(entry.key, klipper_dir)
        if not config_mgr.cache_path.exists():
            missing_configs.append(entry.name)

    if missing_configs:
        em.error("The following devices lack cached configs:")
        for name in missing_configs:
            em.error(f"  - {name}")
        em.error("Flash each device individually and save config before using Flash All.")
        return 1

    # Validate MCU match for each cached config
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
        em.error("MCU type mismatch in cached configs:")
        for name, expected, actual in mcu_mismatches:
            em.error(f"  - {name}: expected {expected}, config has {actual}")
        em.error("Flash each mismatched device individually to reconfigure.")
        return 1

    # Display config ages and warn on stale configs
    for entry in flashable_devices:
        config_mgr = ConfigManager(entry.key, klipper_dir)
        age_display = config_mgr.get_cache_age_display()
        age_str = age_display or "unknown"
        em.info("", f"  {entry.name}: config cached {age_str}")
        if age_display and "Recommend Review" in age_display:
            em.warn(
                f"  {entry.name} config is very old"
                " — consider flashing individually to review config"
            )

    em.phase("Flash All", f"{len(flashable_devices)} device(s) validated")

    # Confirm before proceeding
    device_names = ", ".join(e.name for e in flashable_devices)
    if not decider.confirm(
        ConfirmDecision(
            id="flash_batch",
            message=f"Flash {len(flashable_devices)} device(s) ({device_names})?",
            default=True,
        )
    ):
        em.phase("Flash All", "Cancelled")
        return 0

    em.step_divider()

    # === Stage 2: Version check ===
    host_version = get_host_klipper_version(klipper_dir)
    mcu_versions = get_mcu_versions()
    canbus_map = get_mcu_canbus_map()

    flash_list = list(flashable_devices)

    if host_version is None or mcu_versions is None:
        em.warn("Version check unavailable -- Moonraker not reachable. Flashing all devices.")
    else:
        em.phase("Version", f"Host: {detect_firmware_flavor(host_version)} {host_version}")
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
            em.phase("Version", "All devices already match host version.")
            if not decider.confirm(
                ConfirmDecision(
                    id="flash_all_older_versions",
                    message="Flash anyway?",
                    default=False,
                )
            ):
                em.phase("Flash All", "Cancelled -- firmware already current")
                return 0
        elif current:
            # Some match, some don't
            em.phase("Version", "Outdated devices:")
            for entry in outdated:
                em.info("", f"  - {entry.name}")
            em.phase("Version", "Up-to-date devices:")
            for entry in current:
                em.info("", f"  - {entry.name}")
            if decider.confirm(
                ConfirmDecision(
                    id="flash_all_only_outdated",
                    message="Flash only outdated devices?",
                    default=True,
                )
            ):
                flash_list = outdated
            # else flash_list remains all devices

        # Handle CAN devices with unknown version (UUID not in Moonraker)
        if unknown_version:
            em.phase("Version", "CAN devices with unknown version (not in Moonraker):")
            for entry in unknown_version:
                iface = entry.canbus_interface or "can0"
                em.info("", f"  - {entry.name} ({iface}: {entry.canbus_uuid})")
            if decider.confirm(
                ConfirmDecision(
                    id="flash_all_include_unknown",
                    message="Include unknown-version devices?",
                    default=True,
                )
            ):
                flash_list = _dedupe_flash_all_devices(flash_list + unknown_version)
            else:
                unknown_keys = {entry.key for entry in unknown_version}
                flash_list = [entry for entry in flash_list if entry.key not in unknown_keys]
            # Re-sort after adding unknown devices to maintain ordering
            flash_list = _sort_flash_all_devices(flash_list)

    # === ccache Installation Check ===
    use_ccache = resolve_ccache_usage(
        registry=registry, global_config=global_config, em=em, decider=decider
    )

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
    dirty_result = check_dirty_repo(host_version)
    if dirty_result.is_dirty:
        em.phase("Safety", "Warning: Klipper repo has uncommitted changes")

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
                        em.phase(
                            "Safety",
                            f"Warning: {entry.name} MCU firmware is newer than host (downgrade)",
                        )
                except (ValueError, TypeError):
                    pass

    em.step_divider()
    em.phase("Flash All", f"Building firmware for {len(flash_list)} device(s)...")
    temp_dir = tempfile.mkdtemp(prefix="kalico-flash-")
    total = len(flash_list)
    service_restart_failed = False

    try:
        for i, (entry, result) in enumerate(zip(flash_list, results)):
            if i > 0:
                em.device_divider(i + 1, total, entry.name)
            em.info("Build", f"Building {i + 1}/{total}: {entry.name}...")
            config_mgr = ConfigManager(entry.key, klipper_dir)
            config_mgr.load_cached_config()

            build_result = run_build(klipper_dir, use_ccache=use_ccache)

            if build_result.success:
                artifact_error, artifact_warning = check_firmware_artifact(
                    build_result.firmware_path,
                    build_result.firmware_size,
                )
                if artifact_error:
                    result.error_message = f"Invalid firmware artifact: {artifact_error}"
                    em.warn(f"{entry.name} invalid firmware artifact ({i + 1}/{total})")
                    continue
                if artifact_warning:
                    em.warn(f"{entry.name}: {artifact_warning}")

                # Copy firmware to temp dir (use path from build result)
                device_fw_dir = os.path.join(temp_dir, entry.key)
                os.makedirs(device_fw_dir, exist_ok=True)
                # build succeeded, so firmware_path is populated (typed Optional)
                fw_src = cast(str, build_result.firmware_path)
                fw_name = os.path.basename(fw_src)
                fw_dst = os.path.join(device_fw_dir, fw_name)
                shutil.copy2(fw_src, fw_dst)
                result.firmware_name = fw_name
                result.build_ok = True
                if build_result.ccache_stats:
                    result.ccache_stats = build_result.ccache_stats
                    result.ccache_hit_rate = build_result.ccache_stats.hit_rate
                em.success(f"{entry.name} built ({i + 1}/{total})")
            else:
                result.error_message = build_result.error_message or "Build failed"
                result.error_output = build_result.error_output
                em.warn(f"{entry.name} build failed ({i + 1}/{total})")

        # Check if any builds succeeded
        built_results = [(e, r) for e, r in zip(flash_list, results) if r.build_ok]
        if not built_results:
            em.error("All builds failed. Nothing to flash.")
            return 1

        # === Stage 4: Flash all (inside single service stop) ===
        em.step_divider()
        em.phase("Flash All", f"Flashing {len(built_results)} device(s)...")

        # Acquire sudo credentials if service is active and passwordless sudo is missing
        if _is_service_active() and not verify_passwordless_sudo():
            em.phase("Flash All", "Sudo authentication required for service management")
            if not acquire_sudo():
                em.error("Failed to acquire sudo credentials. Cannot manage Klipper service.")
                return 1

        flash_total = len(built_results)
        used_paths: set[str] = set()
        preflight_cache: dict[str, bool] = {}
        # Per-device flash duration (device_key -> seconds), captured for devices
        # that reach run_flash_sequence. Surfaced in the summary results row so
        # the operation screen's results table can show a duration column.
        device_durations: dict[str, float] = {}

        with klipper_service_stopped(em=em) as svc_state:
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
                device_start = time.monotonic()
                # Keep sudo credentials alive: CAN retries and staggers can
                # push the batch past sudo's timestamp_timeout, which would
                # break the Klipper restart at the end of the batch.
                refresh_sudo_timestamp()
                if idx > 0:
                    em.device_divider(flash_idx, flash_total, entry.name)
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
                            em.warn(
                                f"{entry.name} CAN preflight failed"
                                f" ({flash_idx}/{flash_total})"
                            )
                            continue
                    elif not can_preflight_cache[iface]:
                        result.error_message = f"CAN interface {iface} failed preflight"
                        em.warn(
                            f"{entry.name} CAN interface unavailable"
                            f" ({flash_idx}/{flash_total})"
                        )
                        continue

                    # Validate flash config
                    issue = get_device_flash_config_issue(entry)
                    if issue is not None:
                        error_type, detail = issue
                        result.error_message = f"{error_type}: {detail}"
                        em.warn(f"{entry.name} invalid config ({flash_idx}/{flash_total})")
                        continue

                    # CAN flash method preflight
                    method = entry.flash_command
                    if method not in preflight_cache:
                        preflight_cache[method] = preflight_flash(
                            em, klipper_dir, katapult_dir, method
                        )
                    if not preflight_cache[method]:
                        result.error_message = (
                            f"Preflight failed for flash command: {method}"
                        )
                        em.warn(
                            f"{entry.name} preflight failed ({flash_idx}/{flash_total})"
                        )
                        continue

                    fw_path = os.path.join(temp_dir, entry.key, result.firmware_name)
                    step = run_flash_sequence(
                        entry=entry,
                        device_path="",  # CAN has no USB path
                        firmware_path=fw_path,
                        config=global_config,
                        klipper_dir=klipper_dir,
                        katapult_dir=katapult_dir,
                        em=em,
                        decider=decider,
                        batch=True,  # batch mode: no retry prompt
                    )
                    device_durations[entry.key] = time.monotonic() - device_start

                    result.bootloader_ok = step.bootloader_ok
                    if not step.bootloader_ok:
                        result.error_message = f"Bootloader: {step.error_message}"
                        em.warn(
                            f"{entry.name} bootloader failed ({flash_idx}/{flash_total})"
                        )
                        continue

                    result.flash_ok = step.flash_ok
                    if not step.flash_ok:
                        result.error_message = step.error_message or "Flash failed"
                        em.warn(
                            f"{entry.name} flash failed ({flash_idx}/{flash_total})"
                        )
                    elif step.verify_ok:
                        result.verify_ok = True
                        registry.update_device(
                            entry.key,
                            last_flash_timestamp=(
                                datetime.now().replace(microsecond=0).isoformat()
                            ),
                        )
                        em.success(
                            f"{entry.name} flashed and verified"
                            f" ({flash_idx}/{flash_total})"
                        )
                    else:
                        result.error_message = step.error_reason or "CAN verification failed"
                        em.warn(
                            f"{entry.name} flash OK but CAN verify failed"
                            f" ({flash_idx}/{flash_total})"
                        )

                else:
                    # === USB device flash path ===

                    # Ambiguous pattern guard
                    if entry.key in ambiguous_keys:
                        result.error_message = (
                            "Pattern matches multiple connected USB devices"
                        )
                        em.warn(f"Skipping {entry.name}: ambiguous USB pattern")
                        continue

                    # Find device
                    usb_device = match_device(entry.serial_pattern, usb_devices)
                    if usb_device is None:
                        result.error_message = "Device not found on USB"
                        em.warn(
                            f"{entry.name} not found ({flash_idx}/{flash_total})"
                        )
                        continue

                    # Duplicate USB path guard (SAFE-04)
                    real_path = os.path.realpath(usb_device.path)
                    if _check_duplicate_path(real_path, used_paths):
                        result.error_message = "USB path already targeted by prior device"
                        em.warn(f"Skipping {entry.name}: duplicate USB path")
                        continue

                    # MCU cross-check (SAFE-03)
                    usb_mcu = extract_mcu_from_serial(usb_device.filename)
                    if usb_mcu is not None and usb_mcu.lower() != entry.mcu.lower():
                        result.error_message = (
                            f"MCU mismatch: USB='{usb_mcu}', registry='{entry.mcu}'"
                        )
                        em.warn(f"Skipping {entry.name}: {result.error_message}")
                        continue

                    # Validate flash config
                    issue = get_device_flash_config_issue(entry)
                    if issue is not None:
                        error_type, detail = issue
                        result.error_message = f"{error_type}: {detail}"
                        em.warn(f"{entry.name} invalid config ({flash_idx}/{flash_total})")
                        continue

                    method = entry.flash_command
                    if method not in preflight_cache:
                        preflight_cache[method] = preflight_flash(
                            em, klipper_dir, katapult_dir, method
                        )
                    if not preflight_cache[method]:
                        result.error_message = (
                            f"Preflight failed for flash command: {method}"
                        )
                        em.warn(
                            f"{entry.name} preflight failed ({flash_idx}/{flash_total})"
                        )
                        continue

                    fw_path = os.path.join(temp_dir, entry.key, result.firmware_name)
                    step = run_flash_sequence(
                        entry=entry,
                        device_path=usb_device.path,
                        firmware_path=fw_path,
                        config=global_config,
                        klipper_dir=klipper_dir,
                        katapult_dir=katapult_dir,
                        em=em,
                        decider=decider,
                        batch=True,  # batch mode: no retry prompt
                    )
                    device_durations[entry.key] = time.monotonic() - device_start

                    result.bootloader_ok = step.bootloader_ok
                    if not step.bootloader_ok:
                        result.error_message = f"Bootloader: {step.error_message}"
                        em.warn(
                            f"{entry.name} bootloader failed"
                            f" ({flash_idx}/{flash_total})"
                        )
                        # A partial bootloader entry can change how the device
                        # enumerates; rescan so later devices don't match a
                        # stale snapshot.
                        usb_devices = scan_serial_devices()
                        continue

                    result.flash_ok = step.flash_ok
                    if not step.flash_ok:
                        result.error_message = step.error_message or "Flash failed"
                        em.warn(
                            f"{entry.name} flash failed ({flash_idx}/{flash_total})"
                        )
                    elif step.verify_ok:
                        result.verify_ok = True
                        # Record flash timestamp
                        registry.update_device(
                            entry.key,
                            last_flash_timestamp=(
                                datetime.now().replace(microsecond=0).isoformat()
                            ),
                        )
                        em.success(
                            f"{entry.name} flashed and verified"
                            f" ({flash_idx}/{flash_total})"
                        )
                    else:
                        result.error_message = step.error_reason or "Verification failed"
                        em.warn(
                            f"{entry.name} flash OK but verify failed"
                            f" ({flash_idx}/{flash_total})"
                        )

                    # Re-scan after any flash attempt: a failed flash can leave
                    # the device enumerated differently (e.g. stuck in katapult
                    # mode), which would corrupt matching for later devices.
                    usb_devices = scan_serial_devices()

        # Report actual restart outcome
        if svc_state.will_restart and svc_state.restart_succeeded:
            em.phase("Service", "Klipper restarted")
        elif svc_state.will_restart and svc_state.restart_succeeded is False:
            em.error_with_recovery(
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
    em.step_divider()
    em.phase("Flash All", "Summary:")
    em.info("", "  Device                Build   Boot    Flash   Verify  Cache (H/M%)")
    em.info("", "  " + "-" * 72)

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
        device_passed = (
            result.build_ok
            and result.bootloader_ok
            and result.flash_ok
            and result.verify_ok
        )
        # Carry a structured per-device result on this summary line so the
        # operation screen can build its results table (device_key + PASS/FAIL
        # marker + duration). The rendered CLI text is unchanged.
        em.info(
            "",
            f"{row}  {cache_str}",
            device_key=result.device_key,
            device_name=result.device_name,
            marker="PASS" if device_passed else "FAIL",
            elapsed=device_durations.get(result.device_key),
        )

        # Show build error output inline for failed builds (DBUG-01)
        if not result.build_ok and result.error_output:
            lines = result.error_output.strip().splitlines()
            tail = lines[-20:]
            em.info("", f"  Build output (last {len(tail)} lines):")
            for line in tail:
                em.info("", f"    {line}")

    passed = sum(
        1 for r in results if r.build_ok and r.bootloader_ok and r.flash_ok and r.verify_ok
    )
    failed = len(results) - passed
    em.info("", "")
    em.info("", f"  {passed} passed, {failed} failed out of {len(results)} device(s)")

    return 0 if all_passed and not service_restart_failed else 1
