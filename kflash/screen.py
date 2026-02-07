"""Main screen data aggregation and rendering for the TUI.

Produces three panel-based sections (Status, Devices, Actions) as a single
printable string. All device data is received as parameters — no direct USB
scanning — for testability and separation of concerns.

Uses Phase 11 panel primitives from kflash.panels for bordered rendering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .ansi import get_terminal_width
from .models import DeviceEntry, GlobalConfig
from .panels import format_timestamp_relative, render_panel, render_two_column
from .theme import get_theme

# ---------------------------------------------------------------------------
# Config screen settings definition
# ---------------------------------------------------------------------------

SETTINGS: list[dict] = [
    {"key": "skip_menuconfig", "label": "Skip menuconfig", "type": "toggle"},
    {"key": "use_ccache", "label": "Build acceleration (ccache)", "type": "toggle"},
    {
        "key": "stagger_delay",
        "label": "Flash stagger delay (seconds)",
        "type": "numeric",
        "min": 0,
        "max": 30,
    },
    {
        "key": "return_delay",
        "label": "Menu return delay (seconds)",
        "type": "numeric",
        "min": 0,
        "max": 60,
    },
    {
        "key": "can_stagger_delay",
        "label": "CAN flash stagger delay (seconds)",
        "type": "numeric",
        "min": 0,
        "max": 60,
    },
    {
        "key": "can_scan_on_refresh",
        "label": "CAN bus scan on refresh [Experimental]",
        "type": "toggle",
    },
    {"key": "klipper_dir", "label": "Klipper directory", "type": "path"},
    {"key": "katapult_dir", "label": "Katapult directory", "type": "path"},
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DeviceRow:
    """A single device row for the main screen device list."""

    number: int  # Sequential number for selection (0 = not selectable)
    key: str  # Registry key or filename for unregistered
    name: str  # Display name
    mcu: str  # MCU type
    serial_path: str  # USB serial path or pattern
    version: Optional[str]  # Firmware version if known
    connected: bool  # Whether device is currently connected
    group: str  # "registered", "new", "blocked"
    flashable: bool = True  # Whether device is included in flash operations
    is_can: bool = False  # CAN bus transport indicator
    canbus_uuid: Optional[str] = None  # CAN UUID for display
    canbus_interface: Optional[str] = None  # CAN interface for display
    role: Optional[str] = None  # "toolhead" or "bridge" (CAN ordering)


@dataclass
class ScreenState:
    """Complete state needed to render the main screen."""

    devices: list[DeviceRow] = field(default_factory=list)
    host_version: Optional[str] = None
    status_message: str = "Welcome to kalico-flash. Select an action below."
    status_level: str = "info"  # "info", "success", "error", "warning"
    klipper_status: str = "unknown"
    moonraker_status: str = "unknown"


# ---------------------------------------------------------------------------
# Actions definition
# ---------------------------------------------------------------------------

ACTIONS: list[tuple[str, str]] = [
    ("F", "Flash Device"),
    ("B", "Flash All"),
    ("A", "Add Device"),
    ("E", "Config Device"),
    ("D", "Refresh Devices"),
    ("R", "Remove Device"),
    ("C", "Settings"),
    ("Q", "Quit"),
]

RESPONSIVE_PANEL_MAX_WIDTH = 120
COMPACT_LAYOUT_WIDTH = 72


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def truncate_serial(path: str, max_width: int = 40) -> str:
    """Truncate a serial path to fit within max_width visible characters.

    If the path fits, return as-is. Otherwise keep the start and end
    with ``...`` in the middle, preserving the ``-if00`` suffix when present.
    """
    if len(path) <= max_width:
        return path
    # Preserve suffix like -if00
    suffix = ""
    if path.endswith("-if00"):
        suffix = "-if00"
        body = path[: -len(suffix)]
    else:
        body = path
    available = max_width - 3 - len(suffix)  # 3 chars for "..."
    right = min(4, available // 3)
    left = available - right
    return body[:left] + "..." + body[-right:] + suffix


def _resolve_panel_max_width(panel_max_width: Optional[int] = None) -> int:
    """Resolve the target max panel width for the current render pass."""
    if panel_max_width is None:
        panel_max_width = min(get_terminal_width(), RESPONSIVE_PANEL_MAX_WIDTH)
    return max(panel_max_width, 40)


def _use_compact_layout(panel_max_width: int) -> bool:
    """Return True when the screen should use compact single-column layout."""
    return panel_max_width < COMPACT_LAYOUT_WIDTH


# ---------------------------------------------------------------------------
# Device list building
# ---------------------------------------------------------------------------


def build_device_list(
    registry_data,
    usb_devices: list,
    blocked_list: list[tuple[str, Optional[str]]],
    mcu_versions: Optional[dict[str, str]] = None,
    can_status_map: Optional[dict[str, str]] = None,
) -> list[DeviceRow]:
    """Build a numbered device list grouped by status.

    Groups (in order):
    1. Registered connected (numbered starting at 1)
    2. Registered disconnected (numbered, continuing sequence)
    3. New / unregistered (numbered, continuing sequence)
    4. Blocked (number=0, not selectable)

    Args:
        registry_data: RegistryData with devices and blocked_devices.
        usb_devices: List of DiscoveredDevice from USB scan.
        blocked_list: Pre-built blocked patterns list.
        mcu_versions: Optional MCU version map from Moonraker.
        can_status_map: Optional CAN UUID -> MCU name map from Moonraker.
            When provided, determines CAN device connected status.
            When None (Moonraker unreachable), CAN devices default to connected.

    Returns:
        Ordered list of DeviceRow.
    """
    import fnmatch

    from .discovery import extract_mcu_from_serial, is_supported_device, match_devices

    if mcu_versions is None:
        mcu_versions = {}

    # Cross-reference registry against USB
    entry_matches: dict[str, list] = {}
    device_matches: dict[str, list] = {}
    for entry in registry_data.devices.values():
        if entry.serial_pattern is None:
            continue  # CAN devices have no serial_pattern; handled in CAN loop below
        matches = match_devices(entry.serial_pattern, usb_devices)
        entry_matches[entry.key] = matches
        for device in matches:
            device_matches.setdefault(device.filename, []).append(entry)

    matched_filenames = set(device_matches.keys())
    unmatched = [d for d in usb_devices if d.filename not in matched_filenames]

    # Helper to check blocked status
    def _is_blocked(filename: str) -> tuple[bool, str]:
        name = filename.lower()
        for pattern, reason in blocked_list:
            pat = pattern.lower()
            if not pat.startswith("*"):
                pat = "*" + pat
            if not pat.endswith("*"):
                pat = pat + "*"
            if fnmatch.fnmatch(name, pat):
                return True, reason or "Blocked device"
        return False, ""

    # Build groups
    registered_connected: list[DeviceRow] = []
    registered_disconnected: list[DeviceRow] = []
    new_devices: list[DeviceRow] = []
    blocked_devices: list[DeviceRow] = []

    def _lookup_version(
        mcu_type: str,
        device_name: str = "",
        device_key: str = "",
        mcu_name: Optional[str] = None,
    ) -> Optional[str]:
        """Match device to Moonraker MCU version.

        When mcu_name is provided, does direct key lookup via moonraker.
        Returns None when mcu_name is None (no fuzzy fallback per D35-01).
        """
        from .moonraker import get_mcu_version_for_device

        return get_mcu_version_for_device(mcu_name=mcu_name)

    for entry in registry_data.devices.values():
        if entry.serial_pattern is None:
            continue  # CAN devices handled in dedicated CAN loop below
        matches = entry_matches.get(entry.key, [])
        connected = len(matches) > 0
        serial = matches[0].filename if matches else entry.serial_pattern
        version = _lookup_version(
            entry.mcu, device_name=entry.name, device_key=entry.key, mcu_name=entry.mcu_name
        )

        row = DeviceRow(
            number=0,  # assigned later
            key=entry.key,
            name=entry.name,
            mcu=entry.mcu,
            serial_path=serial,
            version=version,
            connected=connected,
            group="registered",
            flashable=entry.flashable,
        )
        if connected:
            registered_connected.append(row)
        else:
            registered_disconnected.append(row)

    # CAN devices: separate loop since they have no serial_pattern for USB matching
    for entry in registry_data.devices.values():
        if not entry.is_can_device:
            continue
        version = _lookup_version(
            entry.mcu, device_name=entry.name,
            device_key=entry.key, mcu_name=entry.mcu_name,
        )
        # Determine CAN device connected status from Moonraker UUID mapping
        if can_status_map is not None:
            connected = entry.canbus_uuid in can_status_map
        else:
            # Moonraker unreachable: default to connected (graceful fallback)
            connected = True
        row = DeviceRow(
            number=0,
            key=entry.key,
            name=entry.name,
            mcu=entry.mcu,
            serial_path="",
            version=version,
            connected=connected,
            group="registered",
            flashable=entry.flashable,
            is_can=True,
            canbus_uuid=entry.canbus_uuid,
            canbus_interface=entry.canbus_interface,
            role=entry.role,
        )
        if connected:
            registered_connected.append(row)
        else:
            registered_disconnected.append(row)

    for device in unmatched:
        blocked, reason = _is_blocked(device.filename)
        if blocked or not is_supported_device(device.filename):
            blocked_devices.append(
                DeviceRow(
                    number=0,
                    key=device.filename,
                    name=device.filename,
                    mcu="",
                    serial_path=device.filename,
                    version=None,
                    connected=True,
                    group="blocked",
                )
            )
        else:
            # Try to look up version by extracting MCU type from serial name
            guessed_mcu = extract_mcu_from_serial(device.filename)
            new_version = _lookup_version(guessed_mcu) if guessed_mcu else None
            new_devices.append(
                DeviceRow(
                    number=0,
                    key=device.filename,
                    name=device.filename,
                    mcu=guessed_mcu or "unknown",
                    serial_path=device.filename,
                    version=new_version,
                    connected=True,
                    group="new",
                )
            )

    # Assign sequential numbers
    counter = 1
    for row in registered_connected + registered_disconnected + new_devices:
        row.number = counter
        counter += 1
    # Blocked remain number=0

    return registered_connected + registered_disconnected + new_devices + blocked_devices


# ---------------------------------------------------------------------------
# Rendering functions
# ---------------------------------------------------------------------------


def render_device_rows(
    row: DeviceRow,
    host_version: Optional[str] = None,
    serial_max_width: int = 40,
    compact: bool = False,
) -> list[str]:
    """Render a single device as one or more lines.

    Layout per group:
      registered: Line 1 = icon #N  name - flavor version  status_icon
                  Line 2 =       (mcu)  serial
                  Line 3 =       Excluded from flash operations (if applicable)
      new:        Line 1 = icon #N  Unregistered Device - flavor version  status_icon
                  Line 2 =       (mcu)  serial
      blocked:    Line 1 = icon      serial (indented to align with other line-2 content)
    """
    theme = get_theme()

    # Status icon
    if row.connected:
        icon = f"{theme.success}\u25cf{theme.reset}"  # ● green
    else:
        icon = f"{theme.subtle}\u25cb{theme.reset}"  # ○ grey

    indent = "      "  # 6 spaces to align under name (past "● #N  ")

    if row.group == "blocked":
        return [
            f"{icon} {theme.text}    "
            f"{truncate_serial(row.serial_path, max_width=serial_max_width)}"
            f"{theme.reset}"
        ]

    num = f"#{row.number}" if row.number > 0 else ""

    # -- Line 1: name + firmware info + status icon --
    display_name = "Unregistered Device" if row.group == "new" else row.name

    # Build firmware portion
    if row.version:
        from .moonraker import detect_firmware_flavor

        ver_display = f"{detect_firmware_flavor(row.version)} {row.version}"
        if host_version:
            from .moonraker import is_mcu_outdated

            if is_mcu_outdated(host_version, row.version):
                status_icon = f"{theme.warning}\u25d0{theme.reset}"  # ◐ warning
            else:
                status_icon = f"{theme.success}\u2713{theme.reset}"  # ✓ good
        else:
            status_icon = f"{theme.subtle}\u25d0{theme.reset}"  # ◐ unknown
        firmware_text = ver_display
        firmware_part = f" - {theme.subtle}{ver_display}{theme.reset}  {status_icon}"
    else:
        status_icon = f"{theme.subtle}\u25d0{theme.reset}"
        firmware_text = "Firmware Unknown"
        firmware_part = f" - {theme.subtle}Firmware Unknown{theme.reset}  {status_icon}"

    parts = [icon]
    if num:
        parts.append(f" {theme.label}{num}{theme.reset}")
    parts.append(f"  {theme.text}{display_name}{theme.reset}")

    if compact:
        parts.append(f"  {status_icon}")
        firmware_display = truncate_serial(firmware_text, max_width=max(18, serial_max_width))
        lines = ["".join(parts), f"{indent}{theme.subtle}{firmware_display}{theme.reset}"]
    else:
        parts.append(firmware_part)
        lines = ["".join(parts)]

    # -- Line 2: (mcu) + transport info --
    line2_parts: list[str] = []
    if row.is_can and row.canbus_uuid:
        uuid_display = f"CAN: {row.canbus_uuid}"
        iface_display = f"on {row.canbus_interface or 'can0'}"
        if row.mcu and row.mcu != "unknown":
            line2_parts.append(f"{theme.key_info}({row.mcu}){theme.reset}")
        can_display = truncate_serial(
            f"{uuid_display} {iface_display}",
            max_width=max(18, serial_max_width),
        )
        line2_parts.append(f"{theme.text}{can_display}{theme.reset}")
    else:
        if row.mcu and row.mcu != "unknown":
            line2_parts.append(f"{theme.key_info}({row.mcu}){theme.reset}")
        if row.serial_path and (row.group == "new" or row.serial_path != row.name):
            line2_parts.append(
                f"{theme.text}"
                f"{truncate_serial(row.serial_path, max_width=serial_max_width)}"
                f"{theme.reset}"
            )
    if line2_parts:
        lines.append(f"{indent}{' '.join(line2_parts)}")

    # -- Line 3: exclusion warning --
    if row.group == "registered" and not row.flashable:
        lines.append(f"{indent}{theme.caution}Excluded from flash operations{theme.reset}")

    return lines


_SERVICE_STATUS_MAP: dict[str, tuple[str, str]] = {
    "active": ("Running", "success"),
    "inactive": ("Stopped", "warning"),
    "failed": ("Failed", "error"),
    "activating": ("Starting", "warning"),
    "deactivating": ("Stopping", "warning"),
}


def _format_service_indicator(label: str, status: str) -> str:
    """Format a single service indicator like ``Klipper: Running`` with color."""
    theme = get_theme()
    display_text, color_key = _SERVICE_STATUS_MAP.get(status, ("Unknown", "subtle"))
    color = getattr(theme, color_key, theme.subtle)
    return f"{theme.text}{label}:{theme.reset} {color}{display_text}{theme.reset}"


def _format_service_status_line(klipper_status: str, moonraker_status: str) -> str:
    """Combine Klipper and Moonraker indicators into a single line."""
    klipper = _format_service_indicator("Klipper", klipper_status)
    moonraker = _format_service_indicator("Moonraker", moonraker_status)
    return f"{klipper}    {moonraker}"


def render_status_panel(
    status_message: str = "Welcome to kalico-flash. Select an action below.",
    status_level: str = "info",
    klipper_status: str = "unknown",
    moonraker_status: str = "unknown",
    panel_max_width: Optional[int] = None,
    compact: Optional[bool] = None,
) -> str:
    """Render the Status panel with color-coded message and service status."""
    theme = get_theme()
    panel_max_width = _resolve_panel_max_width(panel_max_width)
    if compact is None:
        compact = _use_compact_layout(panel_max_width)

    level_colors = {
        "success": theme.success,
        "error": theme.error,
        "warning": theme.warning,
        "info": theme.text,
    }
    color = level_colors.get(status_level, theme.text)
    content = [f"{color}{status_message}{theme.reset}"]
    if compact:
        content.append(_format_service_indicator("Klipper", klipper_status))
        content.append(_format_service_indicator("Moonraker", moonraker_status))
    else:
        content.append(_format_service_status_line(klipper_status, moonraker_status))

    return render_panel("status", content, max_width=panel_max_width)


def render_devices_panel(
    devices: list[DeviceRow],
    host_version: Optional[str] = None,
    panel_max_width: Optional[int] = None,
) -> str:
    """Render the Devices panel with grouped device rows."""
    theme = get_theme()
    panel_max_width = _resolve_panel_max_width(panel_max_width)
    compact = _use_compact_layout(panel_max_width)
    serial_max_width = max(16, panel_max_width - 36)

    if not devices:
        content = [
            f"{theme.subtle}No devices found. Connect a board and select Refresh.{theme.reset}"
        ]
        footer = _host_version_line(host_version)
        if footer:
            content.append("")
            content.append(footer)
        return render_panel("devices", content, max_width=panel_max_width)

    content: list[str] = []

    # Group by group field, maintaining order
    groups: dict[str, list[DeviceRow]] = {}
    for row in devices:
        groups.setdefault(row.group, []).append(row)

    group_labels = {
        "registered": "Registered",
        "new": "New",
        "blocked": "Blocked",
    }

    first = True
    for group_key in ("registered", "new", "blocked"):
        rows = groups.get(group_key)
        if not rows:
            continue
        if not first:
            content.append("")
        first = False
        label = group_labels.get(group_key, group_key.title())
        content.append(f"{theme.label}{label}{theme.reset}")
        for row in rows:
            for line in render_device_rows(
                row,
                host_version,
                serial_max_width=serial_max_width,
                compact=compact,
            ):
                content.append(f"  {line}")

    # Footer with host version
    footer = _host_version_line(host_version)
    if footer:
        content.append("")
        content.append(footer)

    return render_panel("devices", content, max_width=panel_max_width)


def _host_version_line(host_version: Optional[str]) -> str:
    """Build the host version footer line."""
    theme = get_theme()
    if host_version:
        from .moonraker import detect_firmware_flavor
        from .safety import check_dirty_repo

        flavor = detect_firmware_flavor(host_version)
        dirty_result = check_dirty_repo(host_version)
        dirty_tag = f" {theme.warning}(dirty){theme.reset}" if dirty_result.is_dirty else ""
        return (
            f"{theme.text}Host Firmware:{theme.reset} "
            f"{theme.subtle}{flavor} {host_version}{theme.reset}{dirty_tag}"
        )
    return f"{theme.subtle}Host version: unavailable{theme.reset}"


def render_actions_panel(
    panel_max_width: Optional[int] = None,
    compact: Optional[bool] = None,
) -> str:
    """Render the Actions panel with two-column key layout."""
    theme = get_theme()
    panel_max_width = _resolve_panel_max_width(panel_max_width)
    if compact is None:
        compact = _use_compact_layout(panel_max_width)

    if compact:
        lines = [
            f"{theme.label}{key}{theme.reset} {theme.subtle}\u25b8{theme.reset} "
            f"{theme.text}{label}{theme.reset}"
            for key, label in ACTIONS
        ]
    else:
        # Format each action as (key, label) for render_two_column
        items: list[tuple[str, str]] = []
        for key, label in ACTIONS:
            styled_label = f"{theme.text}{label}{theme.reset}"
            items.append((key, styled_label))
        lines = render_two_column(items)

    return render_panel("actions", lines, max_width=panel_max_width)


def render_main_screen(state: ScreenState) -> str:
    """Render the complete main screen with Status, Devices, and Actions panels.

    Returns a single string ready for print().
    """
    panel_max_width = _resolve_panel_max_width()
    compact = _use_compact_layout(panel_max_width)

    panels = [
        render_status_panel(
            state.status_message,
            state.status_level,
            state.klipper_status,
            state.moonraker_status,
            panel_max_width=panel_max_width,
            compact=compact,
        ),
        render_devices_panel(state.devices, state.host_version, panel_max_width=panel_max_width),
        render_actions_panel(panel_max_width=panel_max_width, compact=compact),
    ]

    return "\n\n".join(panels)


# ---------------------------------------------------------------------------
# Config screen rendering
# ---------------------------------------------------------------------------


def render_config_screen(gc: GlobalConfig) -> str:
    """Render the config screen with status and settings panels.

    Args:
        gc: Current global configuration.

    Returns:
        Multi-line string ready for print().
    """
    theme = get_theme()
    panel_max_width = _resolve_panel_max_width()

    # Status panel
    status_content = [f"{theme.text}Press setting number to edit, Esc to return{theme.reset}"]
    status = render_panel("status", status_content, max_width=panel_max_width)

    # Settings panel
    settings_lines: list[str] = []
    for i, setting in enumerate(SETTINGS, 1):
        value = getattr(gc, setting["key"])
        if setting["type"] == "toggle":
            display = "ON" if value else "OFF"
        elif setting["type"] == "numeric":
            display = f"{value}s"
        else:
            display = str(value)
        settings_lines.append(
            f"{theme.label}{i}.{theme.reset} "
            f"{theme.text}{setting['label']}:{theme.reset} "
            f"{theme.value}{display}{theme.reset}"
        )

    settings = render_panel("settings", settings_lines, max_width=panel_max_width)

    panels = [status, settings]
    return "\n\n".join(panels)


# ---------------------------------------------------------------------------
# Device config screen settings definition
# ---------------------------------------------------------------------------

# Identity panel fields (read-only, no numbers)
DEVICE_IDENTITY_FIELDS: list[dict] = [
    {"key": "mcu", "label": "MCU Type"},
    {"key": "serial_pattern", "label": "Serial Pattern"},
    {"key": "last_flash_timestamp", "label": "Last Flashed"},
]

# Settings panel fields (numbered 1-10, editable)
DEVICE_SETTINGS_FIELDS: list[dict] = [
    {"key": "name", "label": "Display name", "type": "text"},
    {"key": "mcu_name", "label": "Klipper MCU name", "type": "picker"},
    {"key": "flash_method_pair", "label": "Flash method", "type": "picker_overlay"},
    {"key": "flashable", "label": "Include in flash", "type": "toggle"},
    {"key": "role", "label": "CAN device role", "type": "picker"},
    {"key": "canbus_uuid", "label": "CAN bus UUID", "type": "conditional_text", "sub_field": True},
    {
        "key": "canbus_interface", "label": "CAN bus interface",
        "type": "conditional_text", "sub_field": True,
    },
    {
        "key": "bootloader_baud", "label": "Bootloader baud rate",
        "type": "conditional_text", "sub_field": True,
    },
    {
        "key": "uf2_mount_path", "label": "UF2 mount path",
        "type": "conditional_text", "sub_field": True,
    },
    {
        "key": "sdcard_board", "label": "SD card board name",
        "type": "conditional_text", "sub_field": True,
    },
    {"key": "notes", "label": "Notes", "type": "text"},
]

# ---------------------------------------------------------------------------
# Device config screen helpers (pair-driven)
# ---------------------------------------------------------------------------


def _get_current_pair(entry: DeviceEntry):
    """Find the FlashMethodPair matching a device's current bootloader+flash config."""
    from .validation import find_flash_method_pair
    return find_flash_method_pair(entry.bootloader_method, entry.flash_command)


def _is_sub_field_applicable(entry: DeviceEntry, field_key: str) -> bool:
    """Check if a conditional sub-field applies for the current flash method pair.

    Pair-driven: each FlashMethodPair declares which sub-fields it needs via required_sub_fields.
    If no matching pair is found, the sub-field is not applicable.
    """
    pair = _get_current_pair(entry)
    if pair is None:
        return False
    return field_key in pair.required_sub_fields


def _is_role_applicable(entry: DeviceEntry) -> bool:
    """Role is only applicable to CAN devices (toolhead/bridge ordering)."""
    return entry.is_can_device


# ---------------------------------------------------------------------------
# Device config screen rendering
# ---------------------------------------------------------------------------

DEVICE_CONFIG_PANEL_WIDTH = 72


def _compute_device_config_col_width() -> int:
    """Compute the widest label width across identity and settings fields.

    Considers identity labels (plain), settings labels ("N. Label:"),
    and the menuconfig line ("M. Edit firmware config:").
    """
    widths: list[int] = []

    for f in DEVICE_IDENTITY_FIELDS:
        # Identity labels: "Label:"
        widths.append(len(f["label"]) + 1)  # +1 for ":"

    for i, f in enumerate(DEVICE_SETTINGS_FIELDS, 1):
        # Settings labels: "N. Label:" (field 10 displayed as "0.", field 11 as "N.")
        display_num = "0" if i == 10 else ("N" if i == 11 else str(i))
        prefix = f"{display_num}. "
        widths.append(len(prefix) + len(f["label"]) + 1)  # +1 for ":"

    # Menuconfig line: "M. Edit firmware config:"
    widths.append(len("M. Edit firmware config:"))

    return max(widths)


def _format_aligned_line(
    styled_label: str,
    label_plain_width: int,
    styled_value: str,
    col_width: int,
    gap: int = 2,
) -> str:
    """Format a line with label and value aligned at a fixed column.

    Args:
        styled_label: Label with ANSI styling applied.
        label_plain_width: Visible width of the label (without ANSI).
        styled_value: Value with ANSI styling applied.
        col_width: Column width for label alignment.
        gap: Number of spaces between label column and value.
    """
    padding = " " * (col_width - label_plain_width + gap)
    return f"{styled_label}{padding}{styled_value}"


def render_device_config_screen(device_entry: DeviceEntry) -> str:
    """Render the device config screen with status, identity, and settings panels.

    Two-panel layout:
    - Identity panel: read-only device info (MCU type, serial pattern, last flash)
    - Settings panel: numbered fields 1-10 plus M for menuconfig

    Args:
        device_entry: The device to render config for.

    Returns:
        Multi-line string ready for print().
    """
    theme = get_theme()
    panel_max_width = _resolve_panel_max_width()
    col_w = _compute_device_config_col_width()
    panel_min_width = min(DEVICE_CONFIG_PANEL_WIDTH - 2, panel_max_width - 2)

    # Status panel
    status_content = [
        f"{theme.text}Press field key to edit, M for menuconfig, Esc to save & return{theme.reset}"
    ]
    status = render_panel("status", status_content, max_width=panel_max_width)

    # Identity panel (read-only, aligned)
    identity_lines: list[str] = []
    for field_def in DEVICE_IDENTITY_FIELDS:
        key = field_def["key"]
        label_text = f"{field_def['label']}:"
        label_plain_w = len(label_text)
        styled_label = f"{theme.text}{label_text}{theme.reset}"

        if key == "last_flash_timestamp":
            ts = device_entry.last_flash_timestamp
            if ts is not None:
                formatted = format_timestamp_relative(ts)
                styled_value = f"{theme.value}{formatted}{theme.reset}"
            else:
                styled_value = f"{theme.disabled}No recorded flash{theme.reset}"
        elif key == "serial_pattern" and getattr(device_entry, key) is None:
            styled_value = f"{theme.disabled}CAN device (no serial path){theme.reset}"
        else:
            value = getattr(device_entry, key)
            styled_value = f"{theme.value}{value}{theme.reset}"

        identity_lines.append(
            _format_aligned_line(styled_label, label_plain_w, styled_value, col_w)
        )

    identity = render_panel(
        "device identity",
        identity_lines,
        max_width=panel_max_width,
        min_width=panel_min_width,
    )

    # Settings panel (numbered 1-9, 0 for 10th, N for 11th, editable, aligned)
    settings_lines: list[str] = []
    for i, setting in enumerate(DEVICE_SETTINGS_FIELDS, 1):
        num = "0" if i == 10 else ("N" if i == 11 else str(i))
        key = setting["key"]
        label = setting["label"]
        field_type = setting["type"]
        label_text = f"{num}. {label}:"
        label_plain_w = len(label_text)

        if field_type == "text":
            value = getattr(device_entry, key, None)
            display = str(value) if value else ""
            styled_label = (
                f"{theme.label}{num}.{theme.reset} "
                f"{theme.text}{label}:{theme.reset}"
            )
            styled_value = f"{theme.value}{display}{theme.reset}"
            settings_lines.append(
                _format_aligned_line(styled_label, label_plain_w, styled_value, col_w)
            )

        elif field_type == "picker":
            value = getattr(device_entry, key, None)
            display = str(value) if value else "(not set)"
            if key == "role" and not _is_role_applicable(device_entry):
                plain_label = f"{num}. {label}:"
                pad = " " * (col_w - len(plain_label) + 2)
                settings_lines.append(
                    f"{theme.disabled}{plain_label}{pad}(CAN only){theme.reset}"
                )
            else:
                styled_label = (
                    f"{theme.label}{num}.{theme.reset} "
                    f"{theme.text}{label}:{theme.reset}"
                )
                styled_value = f"{theme.value}{display}{theme.reset}"
                settings_lines.append(
                    _format_aligned_line(styled_label, label_plain_w, styled_value, col_w)
                )

        elif field_type == "picker_overlay":
            pair = _get_current_pair(device_entry)
            display = pair.name if pair is not None else "(not set)"
            styled_label = (
                f"{theme.label}{num}.{theme.reset} "
                f"{theme.text}{label}:{theme.reset}"
            )
            styled_value = f"{theme.value}{display}{theme.reset}"
            settings_lines.append(
                _format_aligned_line(styled_label, label_plain_w, styled_value, col_w)
            )

        elif field_type == "toggle":
            value = getattr(device_entry, key, False)
            display = "ON" if value else "OFF"
            styled_label = (
                f"{theme.label}{num}.{theme.reset} "
                f"{theme.text}{label}:{theme.reset}"
            )
            styled_value = f"{theme.value}{display}{theme.reset}"
            settings_lines.append(
                _format_aligned_line(styled_label, label_plain_w, styled_value, col_w)
            )

        elif field_type == "conditional_text":
            value = getattr(device_entry, key, None)
            applicable = _is_sub_field_applicable(device_entry, key)

            if not applicable:
                # Not applicable: render entire line dimmed with column alignment
                val_str = str(value) if value else ""
                plain_label = f"{num}. {label}:"
                pad = " " * (col_w - len(plain_label) + 2)
                settings_lines.append(
                    f"{theme.disabled}{plain_label}{pad}{val_str}{theme.reset}"
                )
            elif value is None or value == "":
                # Applicable but empty: show (required) tag
                styled_label = (
                    f"{theme.label}{num}.{theme.reset} "
                    f"{theme.text}{label}:{theme.reset}"
                )
                styled_value = f"{theme.warning}(required){theme.reset}"
                settings_lines.append(
                    _format_aligned_line(styled_label, label_plain_w, styled_value, col_w)
                )
            else:
                # Applicable with value: show normally
                styled_label = (
                    f"{theme.label}{num}.{theme.reset} "
                    f"{theme.text}{label}:{theme.reset}"
                )
                styled_value = f"{theme.value}{value}{theme.reset}"
                settings_lines.append(
                    _format_aligned_line(styled_label, label_plain_w, styled_value, col_w)
                )

    # Menuconfig line with M prefix (aligned)
    arrow = "\u25b6"  # ▶
    menu_label_text = "M. Edit firmware config:"
    menu_label_plain_w = len(menu_label_text)
    styled_menu_label = (
        f"{theme.label}M.{theme.reset} "
        f"{theme.text}Edit firmware config:{theme.reset}"
    )
    styled_menu_value = f"{theme.subtle}{arrow}{theme.reset}"
    settings_lines.append(
        _format_aligned_line(styled_menu_label, menu_label_plain_w, styled_menu_value, col_w)
    )

    settings = render_panel(
        "settings",
        settings_lines,
        max_width=panel_max_width,
        min_width=panel_min_width,
    )

    return "\n\n".join([status, identity, settings])
