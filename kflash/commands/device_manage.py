"""Device registry management commands: ``cmd_remove_device``, ``cmd_list_devices``."""

from __future__ import annotations

from ..blocklist import (
    blocked_reason_for_entry,
    blocked_reason_for_filename,
    build_blocked_list,
)
from ..config import ConfigManager, default_config_path
from ..decisions import ConfirmDecision, DecisionProvider
from ..discovery import is_supported_device, match_devices, scan_serial_devices
from ..errors import ERROR_TEMPLATES, ConfigError
from ..events import Emitter
from ..moonraker import (
    detect_firmware_flavor,
    get_host_klipper_version,
    get_mcu_version_for_device,
    get_mcu_versions,
)
from ._common import _remove_cached_config


def _emit_device_not_registered(em: Emitter, device_key: str) -> None:
    template = ERROR_TEMPLATES["device_not_registered"]
    em.error_with_recovery(
        template["error_type"],
        template["message_template"].format(device=device_key),
        context={"device": device_key},
        recovery=template["recovery_template"],
    )


def cmd_remove_device(
    registry, device_key: str, em: Emitter, decider: DecisionProvider
) -> int:
    """Remove a device from the registry with optional config cleanup."""
    entry = registry.get(device_key)
    if entry is None:
        _emit_device_not_registered(em, device_key)
        return 1

    em.step_divider()

    if not decider.confirm(
        ConfirmDecision(id="remove_device", message=f"Remove '{entry.name}'?", default=False)
    ):
        em.info("Registry", "Removal cancelled")
        return 0

    registry.remove(device_key)
    em.success(f"Removed '{entry.name}'")

    em.step_divider()

    _remove_cached_config(device_key, em, decider, prompt=True, device_name=entry.name)

    return 0


def cmd_list_devices(registry, em: Emitter) -> int:
    """List all registered devices with connection status.

    Cross-references registered devices against live USB scan to show:
    - [REG] Connected devices with their USB filename
    - [REG] Disconnected devices (registered but not currently connected)
    - [NEW] Unknown USB devices (connected but not registered)

    Args:
        registry: Registry instance for device lookup.
        em: Emitter for user messages.
    """
    # Load registry and scan USB devices
    data = registry.load()
    usb_devices = scan_serial_devices()
    blocked_list = build_blocked_list(data)

    # Fetch version information
    mcu_versions = get_mcu_versions()
    host_version = None
    if data.global_config:
        host_version = get_host_klipper_version(data.global_config.klipper_dir)

    # Cross-reference registered vs discovered
    entry_matches: dict[str, list] = {}
    device_matches: dict[str, list] = {}
    for entry in data.devices.values():
        if entry.serial_pattern is None:
            continue  # CAN devices have no USB serial pattern
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
        blocked_reason = blocked_reason_for_filename(device.filename, blocked_list)
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
        em.info("Devices", "No registered devices and no USB devices found.")
        return 0

    # Handle: no registered devices BUT USB devices exist (first-run UX)
    if not data.devices and usb_devices:
        summary = (
            f"{len(usb_devices)} USB devices found: {registered_connected} registered, "
            f"{new_count} new, {blocked_count} blocked, {duplicate_count} duplicate"
        )
        em.info("Devices", f"No registered devices. {summary}.")
        for device in usb_devices:
            blocked_reason = blocked_reason_for_filename(device.filename, blocked_list)
            if blocked_reason or not is_supported_device(device.filename):
                marker = "BLK"
                detail = blocked_reason or "Unsupported USB device"
            else:
                marker = "NEW"
                detail = "Unregistered device"
            em.device_line(marker, device.filename, detail)
        em.info("Devices", "Press A to register a board.")
        return 0

    # Normal display: show registered devices with connection status
    summary = (
        f"{len(usb_devices)} USB devices found: {registered_connected} registered, "
        f"{new_count} new, {blocked_count} blocked, {duplicate_count} duplicate"
    )
    em.info("Devices", f"{len(data.devices)} registered. {summary}.")

    for key in sorted(data.devices.keys()):
        entry = data.devices[key]
        # Build name with optional [excluded] marker
        name_str = f"{entry.name} ({entry.mcu})"
        if not entry.flashable:
            name_str += " [excluded]"
        blocked_reason = blocked_reason_for_entry(entry, blocked_list)
        if blocked_reason:
            name_str += " [blocked]"

        if blocked_reason:
            matches = entry_matches.get(key, [])
            if matches:
                detail = f"{blocked_reason} [{matches[0].filename}]"
            else:
                detail = blocked_reason
            em.device_line("BLK", name_str, detail)
        elif key in duplicate_entry_keys:
            devices = entry_matches.get(key, [])
            detail = ", ".join(d.filename for d in devices)
            em.device_line("DUP", name_str, detail)
        elif entry_matches.get(key):
            device = entry_matches[key][0]
            em.device_line("REG", name_str, device.filename)
        else:
            em.device_line("REG", name_str, "(disconnected)")

        # Show MCU software version if available
        if mcu_versions:
            version = get_mcu_version_for_device(
                entry.mcu, device_name=entry.name, device_key=entry.key, mcu_name=entry.mcu_name
            )
            if version:
                em.info("", f"       MCU software version: {version}")

    # Show unmatched (unknown/blocked) USB devices if any
    if unmatched:
        # Separate blocked from new devices
        blocked_unmatched = []
        new_unmatched = []
        for device in unmatched:
            blocked_reason = blocked_reason_for_filename(device.filename, blocked_list)
            if blocked_reason or not is_supported_device(device.filename):
                blocked_unmatched.append((device, blocked_reason or "Unsupported USB device"))
            else:
                new_unmatched.append(device)

        # Show new (unregistered) devices
        if new_unmatched:
            em.info("", "")  # blank line for separation
            for device in new_unmatched:
                em.device_line("NEW", device.filename, "Unregistered device")

        # Show blocked devices with label
        if blocked_unmatched:
            em.info("", "")  # blank line for separation
            em.info("Blocked devices", "")
            for device, reason in blocked_unmatched:
                em.device_line("BLK", device.filename, reason)

        # Show hint if there are new unregistered devices
        if new_unmatched:
            em.info("Devices", "Press A to register unknown devices.")

    # Show host Klipper version at the end
    if host_version:
        em.info("", "")  # blank line for separation
        em.info("Version", f"Host: {detect_firmware_flavor(host_version)} {host_version}")

    return 0


def cmd_save_config_as_default(
    registry, device_key: str, em: Emitter, decider: DecisionProvider
) -> int:
    """Promote a device's cached config to the MCU-wide default seed.

    The saved config lives at ``defaults/<mcu>.config`` (see
    ``ConfigManager.save_cache_as_default``) and is offered by
    ``seed_from_default`` to any future device of the same MCU that has no
    cache of its own yet.
    """
    entry = registry.get(device_key)
    if entry is None:
        _emit_device_not_registered(em, device_key)
        return 1

    data = registry.load()
    if data.global_config is None:
        em.error_with_recovery(
            "Config error",
            "Global config not set",
            context={"device": device_key},
            recovery="Configure the Klipper directory from Settings first",
        )
        return 1

    config_mgr = ConfigManager(device_key, data.global_config.klipper_dir)
    if not config_mgr.has_cached_config():
        em.error_with_recovery(
            "Config error",
            f"No cached config for '{entry.name}'",
            context={"device": device_key},
            recovery="Run menuconfig for this device first",
        )
        return 1

    dst = default_config_path(entry.mcu)
    if dst.exists() and not decider.confirm(
        ConfirmDecision(
            id="overwrite_mcu_default",
            message=f"Overwrite existing default config for MCU '{entry.mcu}'?",
            default=False,
        )
    ):
        em.info("Config", "Save as default cancelled")
        return 0

    try:
        dst_path = config_mgr.save_cache_as_default(entry.mcu)
    except ConfigError as exc:
        em.error_with_recovery(
            "Config error",
            f"Failed to save default config: {exc}",
            context={"device": device_key},
            recovery="Run menuconfig for this device first",
        )
        return 1

    em.success(f"Saved '{entry.name}' config as default for MCU '{entry.mcu}' ({dst_path})")
    return 0


def cmd_copy_config(
    registry, src_key: str, dst_key: str, em: Emitter, decider: DecisionProvider
) -> int:
    """Copy one device's cached config onto another device.

    The destination cache is marked seeded (``device:<src_key>``) so the
    normal seed-review gate forces a menuconfig pass before it is used to
    build/flash -- copying between devices is a starting point, not a
    guaranteed-correct config for the destination's board.
    """
    if src_key == dst_key:
        em.error("Cannot copy a device's config onto itself")
        return 1

    src_entry = registry.get(src_key)
    if src_entry is None:
        _emit_device_not_registered(em, src_key)
        return 1

    dst_entry = registry.get(dst_key)
    if dst_entry is None:
        _emit_device_not_registered(em, dst_key)
        return 1

    data = registry.load()
    if data.global_config is None:
        em.error_with_recovery(
            "Config error",
            "Global config not set",
            context={"device": dst_key},
            recovery="Configure the Klipper directory from Settings first",
        )
        return 1

    klipper_dir = data.global_config.klipper_dir
    src_mgr = ConfigManager(src_key, klipper_dir)
    if not src_mgr.has_cached_config():
        em.error_with_recovery(
            "Config error",
            f"No cached config for '{src_entry.name}' to copy",
            context={"device": src_key},
            recovery="Run menuconfig for the source device first",
        )
        return 1

    dst_mgr = ConfigManager(dst_key, klipper_dir)
    if dst_mgr.has_cached_config() and not decider.confirm(
        ConfirmDecision(
            id="overwrite_config_copy",
            message=(
                f"Overwrite cached config for '{dst_entry.name}' with "
                f"the config from '{src_entry.name}'?"
            ),
            default=False,
        )
    ):
        em.info("Config", "Copy cancelled")
        return 0

    if not dst_mgr.seed_from_device(src_key):
        # Race: source cache vanished between the check above and now.
        em.error_with_recovery(
            "Config error",
            f"No cached config for '{src_entry.name}' to copy",
            context={"device": src_key},
            recovery="Run menuconfig for the source device first",
        )
        return 1

    em.success(
        f"Copied config from '{src_entry.name}' to '{dst_entry.name}' -- "
        "run menuconfig to review before the next flash"
    )
    return 0
