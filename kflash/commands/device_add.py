"""``cmd_add_device`` -- interactive wizard to register a new device.

Plus its private helpers ``_pick_mcu_name``, ``_prompt_can_uuid``,
``_prompt_required_field``. UI-free: routes all interaction through the
``DecisionProvider``.
"""

from __future__ import annotations

import fnmatch
import sys
from typing import cast

from .. import boards
from ..blocklist import blocked_reason_for_filename, build_blocked_list
from ..build import run_menuconfig
from ..config import ConfigManager
from ..decisions import (
    BoardProfileChoice,
    ChooseBoardProfileDecision,
    ChooseDeviceDecision,
    ChooseFlashMethodDecision,
    ConfirmDecision,
    DeviceChoice,
    McuMismatchDecision,
    TextPromptDecision,
)
from ..discovery import (
    extract_mcu_from_serial,
    generate_serial_pattern,
    get_can_interfaces,
    is_supported_device,
    match_devices,
    prefix_variants,
    scan_can_devices,
    scan_serial_devices,
)
from ..events import Emitter
from ..models import DeviceEntry, DiscoveredDevice, GlobalConfig
from ..moonraker import get_mcu_serial_map, match_serial_to_mcu_name
from ..validation import (
    SUB_FIELD_DEFAULTS,
    SUB_FIELD_PROMPTS,
    SUB_FIELD_VALIDATORS,
    FlashMethodPair,
    find_flash_method_pair,
    generate_device_key,
    truncate_serial,
    validate_canbus_uuid,
)
from ._common import _remove_cached_config


def _prompt_required_field(
    field_name: str, em: Emitter, decider, validator=None
) -> str | None:
    """Prompt for a required field value with optional validation.

    Unlike the deleted _prompt_conditional_field, empty input re-prompts
    rather than returning None. Only returns None on KeyboardInterrupt/EOFError
    (wizard cancellation).

    Returns:
        Validated value string, or None if user cancels (Ctrl+C/EOF)
    """
    while True:
        try:
            raw = (decider.prompt_text(TextPromptDecision(message=field_name)) or "").strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if not raw:
            em.warn(f"{field_name} is required.")
            continue

        if validator:
            is_valid, error_msg = validator(raw)
            if not is_valid:
                em.warn(error_msg)
                continue

        return raw


def _pick_mcu_name(mcu_serials: dict[str, str | None], em, decider) -> str | None:
    """Show numbered MCU name picker. Returns selected name or None."""
    # Filter to MCUs with serial paths
    mcu_with_serial = {k: v for k, v in mcu_serials.items() if v is not None}

    if not mcu_with_serial:
        em.info("MCU Name", "No MCUs with serial paths in Klipper config")
        raw = decider.prompt_text(
            TextPromptDecision(message="Enter MCU name manually (or press Enter to skip)")
        )
        return raw.strip() if raw and raw.strip() else None

    em.info("MCU Name", "Select from Klipper config MCUs:")
    names = list(mcu_with_serial.keys())
    for i, name in enumerate(names, 1):
        em.info("", f"  {i}. {name}")
    em.info("", "  0. Enter manually")
    em.info("", "  (blank). Skip - no MCU name")

    for _ in range(3):
        raw = (decider.prompt_text(TextPromptDecision(message="MCU name selection")) or "").strip()
        if not raw:
            return None
        if raw == "0":
            manual = decider.prompt_text(TextPromptDecision(message="Enter MCU name"))
            return manual.strip() if manual and manual.strip() else None
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(names):
                return names[idx]
        except ValueError:
            pass
        em.warn(f"Invalid selection '{raw}'")
    return None


def _profile_notes(profile) -> str:
    """Build the picker detail line for a board profile.

    Starts from the profile's free-form ``notes`` and appends a compact
    ``verified`` / ``checked_against`` freshness suffix when either is set, so
    the picker can show provenance without a second line.
    """
    detail_bits = []
    if profile.verified:
        detail_bits.append(f"verified: {profile.verified}")
    if profile.checked_against:
        detail_bits.append(f"checked: {profile.checked_against}")
    suffix = f" ({', '.join(detail_bits)})" if detail_bits else ""
    return f"{profile.notes}{suffix}".strip()


def _prompt_can_uuid(em, decider) -> str | None:
    """Prompt for manual CAN UUID entry with validation."""
    for _attempt in range(3):
        raw = decider.prompt_text(TextPromptDecision(message="CAN bus UUID (12 hex characters)"))
        if not raw:
            return None
        ok, err = validate_canbus_uuid(raw)
        if ok:
            return raw.lower()
        em.warn(err)
    em.error("Too many invalid inputs.")
    return None


def cmd_add_device(
    registry, em, decider, selected_device=None, can_uuid=None,
    can_interface=None, can_only=False,
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
    # TTY check: wizard requires interactive terminal
    if not sys.stdin.isatty():
        em.error("Interactive terminal required. Run from SSH terminal.")
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
        em.info("Selected", truncate_serial(selected.filename))

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
            can_interfaces = get_can_interfaces()
            if not can_interfaces:
                em.error("No CAN interfaces found.")
                return 1

            registry_data = registry.load()
            # Jump straight to CAN interface selection (same as choice=="c" below)
            choice = "c"
        else:
            # Step 1: Scan USB devices
            em.info("Discovery", "Scanning for USB serial devices...")
            devices = scan_serial_devices()
            if not devices:
                em.error("No USB devices found. Plug in a board and try again.")
                return 1

            registry_data = registry.load()
            blocked_list = build_blocked_list(registry_data)

            entry_matches: dict[str, list] = {}
            device_matches: dict[str, list] = {}
            for entry in registry_data.devices.values():
                if entry.serial_pattern is None:
                    continue  # CAN devices have no serial_pattern
                matches = match_devices(entry.serial_pattern, devices)
                entry_matches[entry.key] = matches
                for device in matches:
                    device_matches.setdefault(device.filename, []).append(entry)

            duplicate_entry_keys = {
                key for key, matches in entry_matches.items() if len(matches) > 1
            }

            registered_devices: list[tuple[DiscoveredDevice, DeviceEntry]] = []
            new_devices: list = []
            blocked_devices: list[tuple[DiscoveredDevice, str]] = []
            duplicate_devices: list[tuple[DiscoveredDevice, list]] = []

            for device in devices:
                blocked_reason = blocked_reason_for_filename(device.filename, blocked_list)
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
            em.info("Discovery", summary)

            selectable: list[tuple[DiscoveredDevice, DeviceEntry | None]] = []
            if registered_devices:
                em.info("Discovery", f"Registered devices ({len(registered_devices)}):")
                for device, entry in registered_devices:
                    idx = len(selectable) + 1
                    label = f"{idx}. {device.filename}"
                    detail = f"{entry.name} ({entry.mcu})"
                    em.device_line("REG", label, detail)
                    selectable.append((device, entry))

            if new_devices:
                em.info("Discovery", f"New devices ({len(new_devices)}):")
                for device in new_devices:
                    idx = len(selectable) + 1
                    label = f"{idx}. {device.filename}"
                    em.device_line("NEW", label, "Unregistered device")
                    selectable.append((device, None))

            if duplicate_devices:
                em.info(
                    "Discovery",
                    f"Duplicate devices (not eligible) ({len(duplicate_devices)}):",
                )
                for device, entries in duplicate_devices:
                    names = ", ".join(entry.name for entry in entries)
                    em.device_line("DUP", device.filename, f"Matches: {names}")

            if blocked_devices:
                em.info("Discovery", f"Blocked devices (not eligible) ({len(blocked_devices)}):")
                for device, reason in blocked_devices:
                    em.device_line("BLK", device.filename, reason)

            # Check for CAN interfaces to offer CAN path
            can_interfaces = get_can_interfaces()

            if not selectable and not can_interfaces:
                em.error("No eligible devices available to add.")
                return 1

            choices = ["0"] + [str(i) for i in range(1, len(selectable) + 1)]
            if can_interfaces:
                em.info("Discovery", f"CAN interfaces detected: {', '.join(can_interfaces)}")
                em.info("Discovery", "  C. Add CAN bus device")
                choices.append("c")

            if not selectable:
                # Only CAN option available
                em.info("Discovery", "No USB devices eligible. Use C for CAN device.")

            choice = decider.choose_device(
                ChooseDeviceDecision(
                    prompt="Select device number (0/q to cancel): ",
                    choices=[DeviceChoice(key=c, label=c) for c in choices if c != "0"],
                )
            )
            if choice is None or choice == "0":
                em.info("Registry", "Add device cancelled")
                return 0

        if choice == "c":
            # CAN transport path
            # a. Select CAN interface
            if len(can_interfaces) == 1:
                can_iface = can_interfaces[0]
                em.info("CAN", f"Using interface: {can_iface}")
            else:
                for i, iface in enumerate(can_interfaces, 1):
                    em.info("CAN", f"  {i}. {iface}")
                iface_choice = decider.prompt_text(
                    TextPromptDecision(message=f"Select interface (1-{len(can_interfaces)})")
                )
                try:
                    can_iface = can_interfaces[int(iface_choice) - 1]
                except (ValueError, IndexError):
                    em.error("Invalid interface selection")
                    return 1

            # b. Scan CAN bus
            gc = registry.load().global_config
            em.info("CAN", f"Scanning CAN bus on {can_iface}...")
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
                em.info("CAN", f"Found {len(new_can)} unregistered CAN device(s):")
                for i, dev in enumerate(new_can, 1):
                    em.info("CAN", f"  {i}. UUID: {dev.uuid}  Application: {dev.application}")
                em.info("CAN", "  0. Enter UUID manually")
                uuid_choice = decider.prompt_text(
                    TextPromptDecision(
                        message=f"Select device (1-{len(new_can)}, 0 for manual)"
                    )
                )
                if uuid_choice == "0":
                    can_uuid_val = _prompt_can_uuid(em, decider)
                    if can_uuid_val is None:
                        return 1
                else:
                    try:
                        can_uuid_val = new_can[int(uuid_choice) - 1].uuid
                    except (ValueError, IndexError):
                        em.error("Invalid selection")
                        return 1
            else:
                if can_devices:
                    em.info("CAN", "All discovered CAN devices are already registered")
                else:
                    em.info("CAN", "No CAN devices found on bus")
                em.info("CAN", "You can enter a UUID manually")
                can_uuid_val = _prompt_can_uuid(em, decider)
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
            em.info("Selected", truncate_serial(selected.filename))

    # Check if selected device is already registered
    if existing_entry is not None:
        existing = existing_entry
        if not decider.confirm(
            ConfirmDecision(
                id="readd_device",
                message=(
                    f"Device already registered as '{existing.name}'. "
                    "Remove and re-add this device?"
                ),
                default=False,
            )
        ):
            em.info("Registry", "Add device cancelled")
            return 0
        registry.remove(existing.key)
        em.success(f"Removed existing device '{existing.name}'")
        _remove_cached_config(existing.key, em, decider, prompt=True, device_name=existing.name)
        registry_data = registry.load()
        replacing_existing_device = True

    em.step_divider()

    # Step 3: Global config (first run only)
    if not registry_data.devices and not replacing_existing_device:
        em.info("Setup", "First device registration - configuring global paths...")
        klipper_dir = decider.prompt_text(
            TextPromptDecision(message="Klipper source directory", default="~/klipper")
        )
        katapult_dir = decider.prompt_text(
            TextPromptDecision(message="Katapult source directory", default="~/katapult")
        )
        registry.save_global(
            GlobalConfig(
                klipper_dir=klipper_dir,
                katapult_dir=katapult_dir,
            )
        )
        em.success("Global configuration saved")
    elif not registry_data.devices and replacing_existing_device:
        em.info("Setup", "Keeping existing global configuration")

    em.step_divider()

    # Step 6: MCU type
    if is_can_transport:
        em.info("CAN", "CAN devices require manual MCU type entry")
        mcu = decider.prompt_text(TextPromptDecision(message="MCU type (e.g., stm32h723, rp2040)"))
    else:
        detected_mcu = extract_mcu_from_serial(cast(DiscoveredDevice, selected).filename)
        if detected_mcu:
            if decider.confirm(
                ConfirmDecision(
                    id="confirm_detected_mcu",
                    message=f"Detected MCU: {detected_mcu}. Correct?",
                    default=True,
                )
            ):
                mcu = detected_mcu
            else:
                mcu = decider.prompt_text(TextPromptDecision(message="Enter MCU type"))
        else:
            em.info("Discovery", "Could not auto-detect MCU from device name.")
            mcu = decider.prompt_text(
                TextPromptDecision(message="MCU type (e.g., stm32h723, rp2040)")
            )

    if not mcu:
        em.error("MCU type is required.")
        return 1

    # Step 6.5: Board profile picker.
    #
    # Load the catalog once, surface any load warnings, then offer the profiles
    # whose MCU matches AND whose transport matches this add (CAN adds show only
    # can profiles; USB/serial adds hide can-only profiles -- mirrors the spirit
    # of filter_flash_methods_for_device). An empty candidate list means the
    # wizard proceeds exactly as before (zero behaviour change).
    selected_profile = None
    profiles, board_warnings = boards.load_catalog()
    for warning in board_warnings:
        em.warn(warning)

    candidates = boards.profiles_for_mcu(mcu, profiles=profiles)
    if is_can_transport:
        candidates = [p for p in candidates if p.bootloader_method == "can"]
    else:
        candidates = [p for p in candidates if p.bootloader_method != "can"]

    if candidates:
        profile_choices = [
            BoardProfileChoice(key=p.key, label=p.name, notes=_profile_notes(p))
            for p in candidates
        ]
        picked = decider.choose_board_profile(
            ChooseBoardProfileDecision(detected_mcu=mcu, choices=profile_choices)
        )
        if picked is None:
            em.info("Registry", "Add device cancelled")
            return 0
        if picked != "other":
            selected_profile = boards.get_profile(picked, profiles=profiles)
            if selected_profile is not None:
                em.info("Board", f"Using profile: {selected_profile.name}")
            # A stale/unknown key falls through to the manual path unchanged.

    em.step_divider()

    # Display name (device key is auto-generated). Asked AFTER the MCU and
    # board-profile steps so the prompt can suggest a good name: a picked
    # profile pre-fills its display name (Enter accepts it); the manual path
    # keeps the static example hint.
    registry_data = registry.load()
    existing_names = {e.name.lower() for e in registry_data.devices.values()}
    if selected_profile is not None:
        name_decision = TextPromptDecision(
            message="Display name", default=selected_profile.name
        )
    else:
        name_decision = TextPromptDecision(
            message="Display name (e.g., 'Octopus Pro v1.1')"
        )
    display_name = None
    for _attempt in range(3):
        name_input = decider.prompt_text(name_decision)
        if not name_input:
            em.warn("Display name cannot be empty.")
            continue
        if name_input.lower() in existing_names:
            em.warn(f"You already have a device named '{name_input}'. Enter a different name.")
            continue
        display_name = name_input
        break
    if display_name is None:
        em.error("Too many invalid inputs.")
        return 1

    # Auto-generate device key from display name
    try:
        device_key = generate_device_key(display_name, registry)
    except ValueError:
        em.error("Display name must contain at least one letter or number.")
        return 1

    em.step_divider()

    # Step 7: Serial pattern (USB only)
    if not is_can_transport:
        selected_usb = cast(DiscoveredDevice, selected)
        serial_pattern = generate_serial_pattern(selected_usb.filename)
        em.info("Registry", f"Serial pattern: {serial_pattern}")

        # Check if pattern matches multiple connected devices (duplicate USB IDs)
        pattern_matches = match_devices(serial_pattern, devices)
        if len(pattern_matches) > 1:
            em.error(
                "Serial pattern matches multiple connected devices."
                " Unplug duplicates and retry."
            )
            for device in pattern_matches:
                em.device_line("DUP", device.filename, "Duplicate USB ID")
            return 1

        # Check for pattern overlap with existing devices
        for existing_key, existing_entry in registry_data.devices.items():
            if existing_entry.serial_pattern is None:
                continue  # CAN devices have no serial_pattern
            if existing_entry.serial_pattern == serial_pattern:
                em.error(
                    f"Serial pattern already registered to '{existing_key}'. "
                    "Remove it first or choose a different device."
                )
                return 1
            existing_variants = prefix_variants(existing_entry.serial_pattern)
            if any(fnmatch.fnmatch(selected_usb.filename, v) for v in existing_variants):
                em.error(
                    f"Selected device matches existing entry '{existing_key}'. "
                    "Remove it first or replace it."
                )
                return 1

    em.step_divider()

    # Step 7.5: MCU name selection
    mcu_name = None
    mcu_serials = get_mcu_serial_map()

    if is_can_transport:
        # CAN devices: skip serial-based auto-match, go directly to picker
        if mcu_serials is not None:
            mcu_name = _pick_mcu_name(mcu_serials, em, decider)
        else:
            raw = decider.prompt_text(
                TextPromptDecision(message="Klipper MCU name (optional, press Enter to skip)")
            )
            mcu_name = raw.strip() if raw and raw.strip() else None
    elif mcu_serials is not None:
        # USB path: try auto-match first
        auto_match = match_serial_to_mcu_name(cast(str, serial_pattern), mcu_serials)
        if auto_match:
            if decider.confirm(
                ConfirmDecision(
                    id="confirm_mcu_name",
                    message=f"Detected Klipper MCU name: '{auto_match}'. Correct?",
                    default=True,
                )
            ):
                mcu_name = auto_match
            else:
                mcu_name = _pick_mcu_name(mcu_serials, em, decider)
        else:
            em.info("MCU Name", "Could not auto-detect MCU name from serial path")
            mcu_name = _pick_mcu_name(mcu_serials, em, decider)
    else:
        em.warn("Moonraker unreachable - cannot auto-detect MCU name")
        raw = decider.prompt_text(
            TextPromptDecision(message="Klipper MCU name (optional, press Enter to skip)")
        )
        mcu_name = raw.strip() if raw and raw.strip() else None

    em.step_divider()

    # Step 8: Flash method
    #
    # A board profile collapses this step: bootloader_method/flash_command come
    # straight from the profile and choose_flash_method is NOT asked. CAN adds
    # still auto-select Katapult CAN; USB adds without a profile use the picker.
    if selected_profile is not None:
        bootloader_method = selected_profile.bootloader_method
        flash_command = selected_profile.flash_command
        pair = cast(FlashMethodPair, find_flash_method_pair(bootloader_method, flash_command))
        em.info("Flash Method", f"{pair.name} (board profile: {selected_profile.name})")
    elif is_can_transport:
        # CAN devices: auto-select Katapult CAN
        bootloader_method = "can"
        flash_command = "katapult_can"
        pair = cast(FlashMethodPair, find_flash_method_pair(bootloader_method, flash_command))
        em.info("Flash Method", f"Auto-selected: {pair.name}")
    else:
        result = decider.choose_flash_method(
            ChooseFlashMethodDecision(
                current_bootloader=None,
                current_flash_command=None,
                device_name=display_name,
                mcu=mcu,
                is_can_device=False,
            )
        )
        if result is None:
            em.info("Registry", "Add device cancelled")
            return 1

        bootloader_method, flash_command = result
        pair = cast(FlashMethodPair, find_flash_method_pair(bootloader_method, flash_command))
        em.info("Flash Method", pair.name)

    # Device role -- CAN only (affects Flash All ordering). The prompt default
    # comes from the board profile's role when a profile was picked; otherwise
    # it stays the legacy "toolhead" default. USB devices get no role.
    if is_can_transport:
        if selected_profile is not None:
            default_role_choice = {"toolhead": "1", "bridge": "2"}.get(
                selected_profile.role or "", "3"
            )
        else:
            default_role_choice = "1"
        em.info("", "")
        em.info("Device role", "affects Flash All ordering:")
        em.info("", "  1) Toolhead")
        em.info("", "  2) Bridge")
        em.info("", "  3) Skip (no role)")
        try:
            role_choice = decider.prompt_text(
                TextPromptDecision(message="Select role", default=default_role_choice)
            ) or default_role_choice
        except (EOFError, KeyboardInterrupt):
            role_choice = "3"
        role_map = {"1": "toolhead", "2": "bridge", "3": None}
        device_role = role_map.get(role_choice, "toolhead")
    else:
        device_role = None  # USB devices: no role by default

    # Step 8a: Sub-field prompts (driven by pair.required_sub_fields)
    bootloader_baud: int | None = None
    uf2_mount_path = None
    sdcard_board = None
    canbus_uuid = None
    profile_sub_fields = selected_profile.sub_fields if selected_profile is not None else {}

    for field_key in pair.required_sub_fields:
        # CAN transport: skip fields already set from CAN flow. CAN identity
        # fields (uuid/interface) ALWAYS come from the CAN flow, never a profile.
        if is_can_transport and field_key == "canbus_uuid":
            canbus_uuid = canbus_uuid_value
            continue
        if is_can_transport and field_key == "canbus_interface":
            continue  # canbus_interface_value already set from CAN flow

        # A board profile's sub_fields pre-fill BEFORE SUB_FIELD_DEFAULTS.
        if field_key in profile_sub_fields:
            pv = profile_sub_fields[field_key]
            if field_key == "bootloader_baud":
                bootloader_baud = int(pv)
            elif field_key == "uf2_mount_path":
                uf2_mount_path = str(pv)
            elif field_key == "sdcard_board":
                sdcard_board = str(pv)
            em.info(
                "Flash Method",
                f"Using board profile {SUB_FIELD_PROMPTS[field_key]}: {pv}",
            )
            continue

        default = SUB_FIELD_DEFAULTS.get(field_key)
        if default is not None:
            # Auto-accept default, echo
            if field_key == "bootloader_baud":
                bootloader_baud = cast(int, default)
            em.info("Flash Method", f"Using default {SUB_FIELD_PROMPTS[field_key]}: {default}")
            continue

        # Required: prompt until value or cancel
        value = _prompt_required_field(
            SUB_FIELD_PROMPTS[field_key], em, decider,
            validator=SUB_FIELD_VALIDATORS.get(field_key),
        )
        if value is None:
            em.info("Registry", "Add device cancelled")
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

    em.step_divider()

    # Step 9: Flashable toggle
    if flash_command is None:
        # Build Only -- auto-set non-flashable
        is_flashable = False
        em.info("Flash Method", "Build Only selected -- device excluded from flash operations")
    else:
        exclude_from_flash = decider.confirm(
            ConfirmDecision(
                id="exclude_from_flash",
                message="Exclude this device from flashing?",
                default=False,
            )
        )
        is_flashable = not exclude_from_flash

    em.step_divider()

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
        board=selected_profile.key if selected_profile is not None else None,
    )
    registry.add(entry)
    em.success(f"Device '{display_name}' added successfully.")

    # Offer to run menuconfig for the newly registered device
    em.step_divider()
    if decider.confirm(
        ConfirmDecision(
            id="run_menuconfig_now",
            message="Run menuconfig now to configure firmware?",
            default=True,
        )
    ):
        try:
            data = registry.load()
            if data.global_config is None:
                em.warn("Cannot run menuconfig: global config not set")
                return 0

            klipper_dir = data.global_config.klipper_dir
            config_mgr = ConfigManager(device_key, klipper_dir)

            # Seed-load shape shared with flash_steps.load_and_validate_config and
            # ui/menuconfig._run_menuconfig_step: with no cache yet, prefer the
            # board profile fragment (guarded by entry.board), then fall back to
            # the MCU default; read the seed label ONCE, then load the cache
            # (or start fresh) with a single load_cached_config call.
            if not config_mgr.has_cached_config():
                if entry.board is not None:
                    config_mgr.seed_from_board(entry.board)
                if not config_mgr.has_cached_config():
                    config_mgr.seed_from_default(entry.mcu)

            seeded_from = config_mgr.seed_source()

            if config_mgr.has_cached_config():
                config_mgr.load_cached_config()
                if config_mgr.is_seeded():
                    em.info(
                        "Config",
                        f"Config seeded from {seeded_from or 'unknown'} -- review required",
                    )
                else:
                    em.info("Config", f"Loaded cached config for '{entry.name}'")
            else:
                config_mgr.clear_klipper_config()
                em.info("Config", "No cached config found, starting fresh")

            em.info("Config", "Launching menuconfig...")
            ret_code, was_saved = run_menuconfig(klipper_dir, str(config_mgr.klipper_config_path))

            if ret_code != 0:
                em.warn("menuconfig exited with errors, config not saved")
            elif was_saved:
                # DON'T save to cache yet -- need to validate MCU first
                try:
                    is_match, actual_mcu = config_mgr.validate_mcu(entry.mcu)
                    while not is_match:
                        choice = decider.mcu_mismatch(
                            McuMismatchDecision(
                                actual_mcu=cast(str, actual_mcu),
                                expected_mcu=entry.mcu,
                                device_name=entry.name,
                            )
                        )
                        if choice == "r":
                            em.info("Config", "Re-launching menuconfig...")
                            ret_code2, was_saved2 = run_menuconfig(
                                klipper_dir, str(config_mgr.klipper_config_path)
                            )
                            if was_saved2:
                                is_match, actual_mcu = config_mgr.validate_mcu(entry.mcu)
                            else:
                                em.info("Config", "menuconfig exited without saving")
                                break
                        elif choice == "d":
                            # Restore the cached config if one exists NOW -- a
                            # fresh seed counts (has_cached_config is checked at
                            # discard time, not from a stale pre-seed snapshot).
                            # The .seeded marker is left untouched so the seeded
                            # cache still forces a review on the next flash.
                            if config_mgr.has_cached_config():
                                config_mgr.load_cached_config()
                                em.info("Config", "Restored cached config")
                            else:
                                config_mgr.clear_klipper_config()
                                em.info("Config", "Discarded config (no cache)")
                            break
                        else:  # 'k'
                            config_mgr.save_cached_config()
                            em.info("Config", "Keeping mismatched config")
                            break
                    else:
                        # MCU matched (while condition became False) -- save now
                        config_mgr.save_cached_config()
                        em.success(f"Config saved for '{entry.name}'")
                except Exception:
                    pass  # Non-blocking
            else:
                em.info("Config", "menuconfig exited without saving")
        except Exception as exc:
            em.warn(f"menuconfig failed: {exc}")

    return 0
