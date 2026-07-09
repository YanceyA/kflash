"""The kflash dashboard: Status / Devices / Actions.

The home screen. It carries forward the legacy Status/Devices/Actions
dashboard concept (once ``kflash.screen.render_main_screen``, deleted at
Stage 3) using the Textual skin vocabulary
(``Panel``/``HintLine``/``status_marker``).

Engine boundary
---------------
Reads (registry load, USB discovery scan, Moonraker/service queries) are cheap
and non-critical, so they run on a plain Textual *thread worker* -- NOT through
the :class:`~kflash.ui.engine_bridge.EngineBridge` job runner, which is reserved
for the flash critical section (one job at a time). The read functions are
imported directly from the engine modules (``discovery``/``moonraker``/
``service``/``blocklist``); the device-list grouping lives locally in
:func:`build_dashboard_devices`.

A flash (``F``) and Flash All (``B``) are the *operations*: each goes through
the bridge on a non-daemon worker thread and pushes an
:class:`~kflash.ui.screens.operation.OperationScreen` (the R5 phase checklist +
log + progress + results table). The dashboard stays the bridge's event target
and routes the :class:`~kflash.events.FlashEvent` stream (and the completion
message) into that screen, refreshing the device list on completion.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional, cast

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Static

from ...blocklist import blocked_reason_for_filename, build_blocked_list
from ...commands import cmd_flash, cmd_flash_all, cmd_remove_device
from ...discovery import (
    extract_mcu_from_serial,
    get_can_interfaces,
    is_supported_device,
    match_devices,
    scan_can_devices,
    scan_serial_devices,
)
from ...events import Emitter
from ...moonraker import (
    detect_firmware_flavor,
    get_host_klipper_version,
    get_mcu_canbus_map,
    get_mcu_version_for_device,
    get_mcu_versions,
    is_mcu_outdated,
)
from ...service import (
    acquire_sudo,
    get_service_status,
    is_service_active,
    verify_passwordless_sudo,
)
from .. import menuconfig
from ..dialogs import ConfirmDialog, DecisionConfirmDialog
from ..engine_bridge import EngineBusyError, EngineEvent, EngineJobCompleted
from ..skin import COLORS, HintLine, Panel, status_marker
from .operation import OperationScreen

if TYPE_CHECKING:
    from ..app import KflashApp

# Two hint rows: one line no longer fits the 80-column budget (the single row
# already truncated "Q Quit" at 80 before M was added).
_HINTS_ROW1: list[tuple[str, str]] = [
    ("F", "Flash"),
    ("B", "Flash All"),
    ("A", "Add"),
    ("E", "Edit"),
    ("R", "Remove"),
]
_HINTS_ROW2: list[tuple[str, str]] = [
    ("M", "Menuconfig"),
    ("D", "Refresh"),
    ("C", "Settings"),
    ("Q", "Quit"),
]

# systemctl status -> (display text, palette role)
_SERVICE_DISPLAY: dict[str, tuple[str, str]] = {
    "active": ("Running", "green"),
    "inactive": ("Stopped", "yellow"),
    "failed": ("Failed", "red"),
    "activating": ("Starting", "yellow"),
    "deactivating": ("Stopping", "yellow"),
}

_LEVEL_ROLE: dict[str, str] = {
    "success": "green",
    "error": "red",
    "warning": "yellow",
    "info": "text",
}

# The udev-managed directory whose mtime bumps on every USB serial hotplug.
_SERIAL_BY_ID = "/dev/serial/by-id"

# --------------------------------------------------------------------------- #
# Short-lived fetch cache (mirrors the legacy tui._cached_screen_fetch pattern)
# --------------------------------------------------------------------------- #
# Live background refresh (R2) polls every 2 s (hotplug) / 5 s (status). Without
# a cache the Moonraker HTTP reads would fire on every tick; a 5 s TTL dedups
# them so rapid polls reuse one result. Keyed + locked so the (thread-worker)
# fetches never trample each other's cache slot.
_FETCH_CACHE_TTL_SECONDS = 5.0
_fetch_cache: dict[tuple, tuple[float, Any]] = {}
_fetch_cache_lock = threading.Lock()


def _cached_fetch(key: tuple, fetch: Callable[[], Any]) -> Any:
    """Return a <=5 s cached value for *key*, else call *fetch* and cache it."""
    now = time.monotonic()
    with _fetch_cache_lock:
        entry = _fetch_cache.get(key)
        if entry is not None and now - entry[0] < _FETCH_CACHE_TTL_SECONDS:
            return entry[1]
    value = fetch()
    with _fetch_cache_lock:
        _fetch_cache[key] = (now, value)
    return value


def _serial_dir_mtime() -> Optional[float]:
    """mtime of ``/dev/serial/by-id`` (a cheap hotplug signal), or ``None``.

    Returns ``None`` when the directory does not exist (no USB serial devices
    have ever been present) or cannot be stat'd -- treated as "no change".
    """
    try:
        return os.stat(_SERIAL_BY_ID).st_mtime
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None


def _scan_unregistered_can(data) -> list:
    """Discover CAN-bus devices (best-effort) for the D-refresh CAN scan.

    Reimplements the legacy ``tui._build_screen_state`` optional CAN discovery
    (NOT imported -- the frozen renderer stays untouched): probe every CAN
    interface via ``scan_can_devices`` and return ``(DiscoveredCanDevice, iface)``
    pairs. Registration/dedup filtering happens in ``build_dashboard_devices``.
    """
    results: list = []
    if data.global_config is None:
        return results
    try:
        interfaces = get_can_interfaces()
    except Exception:
        return results
    for iface in interfaces:
        try:
            for can_dev in scan_can_devices(iface, data.global_config.katapult_dir):
                results.append((can_dev, iface))
        except Exception:
            pass
    return results


# --------------------------------------------------------------------------- #
# Device rows (local reimplementation -- does NOT import screen.build_device_list)
# --------------------------------------------------------------------------- #
@dataclass
class DeviceRow:
    """One device line for the dashboard table."""

    number: int  # 1-based selection number; 0 = not selectable (blocked)
    key: str
    name: str
    mcu: str
    method: str
    serial_path: str
    version: Optional[str]
    connected: bool
    group: str  # "registered" | "new" | "blocked"
    flashable: bool = True
    is_can: bool = False
    detail: str = ""
    canbus_uuid: Optional[str] = None
    canbus_interface: Optional[str] = None

    @property
    def can_flash(self) -> bool:
        """Stage 1: only registered, included devices are flashable via F."""
        return self.group == "registered" and self.flashable


def _lookup_version(
    entry, mcu_versions: Optional[dict]
) -> Optional[str]:
    """Best-effort Moonraker version for a registered device."""
    if not mcu_versions or entry.mcu_name is None:
        return None
    try:
        return get_mcu_version_for_device(
            mcu_name=entry.mcu_name, _mcu_versions=mcu_versions
        )
    except Exception:
        return None


def build_dashboard_devices(
    data,
    usb_devices: list,
    blocked_list: list,
    mcu_versions: Optional[dict],
    can_status_map: Optional[dict] = None,
    unregistered_can: Optional[list] = None,
) -> list[DeviceRow]:
    """Group registry + scan results into ordered dashboard rows.

    Mirrors the grouping of ``kflash.screen.build_device_list`` (registered
    connected, registered disconnected, new, blocked) without importing it.
    ``unregistered_can`` (from :func:`_scan_unregistered_can`, D-refresh only)
    is a list of ``(DiscoveredCanDevice, iface)`` pairs appended to the "new"
    group after registered/dedup filtering.
    """
    registered_connected: list[DeviceRow] = []
    registered_disconnected: list[DeviceRow] = []
    new_devices: list[DeviceRow] = []
    blocked: list[DeviceRow] = []
    matched_filenames: set[str] = set()

    # Registered USB devices.
    for entry in data.devices.values():
        if entry.is_can_device:
            continue
        matches = (
            match_devices(entry.serial_pattern, usb_devices)
            if entry.serial_pattern
            else []
        )
        for device in matches:
            matched_filenames.add(device.filename)
        connected = len(matches) > 0
        serial = matches[0].filename if matches else (entry.serial_pattern or "")
        row = DeviceRow(
            number=0,
            key=entry.key,
            name=entry.name,
            mcu=entry.mcu,
            method=entry.flash_command or "-",
            serial_path=serial,
            version=_lookup_version(entry, mcu_versions),
            connected=connected,
            group="registered",
            flashable=entry.flashable,
        )
        (registered_connected if connected else registered_disconnected).append(row)

    # Registered CAN devices (no USB serial pattern).
    for entry in data.devices.values():
        if not entry.is_can_device:
            continue
        if can_status_map is not None:
            connected = entry.canbus_uuid in can_status_map
        else:
            connected = True  # Moonraker unreachable: graceful default
        row = DeviceRow(
            number=0,
            key=entry.key,
            name=entry.name,
            mcu=entry.mcu,
            method=entry.flash_command or "-",
            serial_path=f"CAN {entry.canbus_uuid}",
            version=_lookup_version(entry, mcu_versions),
            connected=connected,
            group="registered",
            flashable=entry.flashable,
            is_can=True,
            detail=f"on {entry.canbus_interface or 'can0'}",
        )
        (registered_connected if connected else registered_disconnected).append(row)

    # Unmatched USB devices -> new or blocked.
    for device in usb_devices:
        if device.filename in matched_filenames:
            continue
        reason = blocked_reason_for_filename(device.filename, blocked_list)
        if reason or not is_supported_device(device.filename):
            blocked.append(
                DeviceRow(
                    number=0,
                    key=device.filename,
                    name=device.filename,
                    mcu="",
                    method="-",
                    serial_path=device.filename,
                    version=None,
                    connected=True,
                    group="blocked",
                    flashable=False,
                    detail=reason or "Unsupported device",
                )
            )
        else:
            new_devices.append(
                DeviceRow(
                    number=0,
                    key=device.filename,
                    name=device.filename,
                    mcu=extract_mcu_from_serial(device.filename) or "unknown",
                    method="-",
                    serial_path=device.filename,
                    version=None,
                    connected=True,
                    group="new",
                    flashable=False,
                )
            )

    # Unregistered CAN devices discovered by the D-refresh CAN scan (new group).
    if unregistered_can:
        registered_uuids = {
            entry.canbus_uuid
            for entry in data.devices.values()
            if entry.canbus_uuid is not None
        }
        seen_uuids: set[str] = set()
        for can_dev, iface in unregistered_can:
            if can_dev.uuid in registered_uuids or can_dev.uuid in seen_uuids:
                continue
            seen_uuids.add(can_dev.uuid)
            new_devices.append(
                DeviceRow(
                    number=0,
                    key=f"can:{can_dev.uuid}",
                    name=f"CAN Device ({can_dev.uuid})",
                    mcu="unknown",
                    method="-",
                    serial_path="",
                    version=None,
                    connected=True,
                    group="new",
                    flashable=False,
                    is_can=True,
                    canbus_uuid=can_dev.uuid,
                    canbus_interface=iface,
                    detail=f"unregistered on {iface}",
                )
            )

    ordered = registered_connected + registered_disconnected + new_devices
    for number, row in enumerate(ordered, start=1):
        row.number = number
    return ordered + blocked


# --------------------------------------------------------------------------- #
# Fetched state
# --------------------------------------------------------------------------- #
@dataclass
class DashboardState:
    """Everything the dashboard needs to paint one refresh."""

    devices: list[DeviceRow] = field(default_factory=list)
    status_message: str = ""
    status_level: str = "info"
    klipper_status: str = "unknown"
    moonraker_status: str = "unknown"
    host_version: Optional[str] = None
    loading: bool = False


def fetch_dashboard_state(
    registry, status_message: str, status_level: str, scan_can: bool = False
) -> DashboardState:
    """Load registry + scan devices + query Moonraker/services (blocking).

    Called on a worker thread. Every engine read is best-effort so a Moonraker
    or systemctl hiccup degrades gracefully instead of blanking the screen. The
    Moonraker reads go through a 5 s cache (:func:`_cached_fetch`) so the R2
    background polls do not hammer the host. ``scan_can`` (D-refresh only, gated
    by ``can_scan_on_refresh``) additionally discovers unregistered CAN devices.
    """
    data = registry.load()
    try:
        # Best-effort like the Moonraker reads: /dev/serial/by-id can vanish
        # between the scan's is_dir() check and iterdir() on hotplug churn.
        usb_devices = scan_serial_devices()
    except OSError:
        usb_devices = []
    blocked_list = build_blocked_list(data)

    mcu_versions: Optional[dict] = None
    host_version: Optional[str] = None
    can_status_map: Optional[dict] = None
    try:
        mcu_versions = _cached_fetch(("mcu_versions",), get_mcu_versions)
    except Exception:
        pass
    try:
        if data.global_config is not None:
            klipper_dir = data.global_config.klipper_dir
            host_version = _cached_fetch(
                ("host_version", klipper_dir),
                lambda: get_host_klipper_version(klipper_dir),
            )
    except Exception:
        pass
    try:
        can_status_map = _cached_fetch(("canbus_map",), get_mcu_canbus_map)
    except Exception:
        pass

    unregistered_can: Optional[list] = None
    if scan_can:
        unregistered_can = _scan_unregistered_can(data)

    devices = build_dashboard_devices(
        data, usb_devices, blocked_list, mcu_versions, can_status_map, unregistered_can
    )

    klipper_status = "unknown"
    moonraker_status = "unknown"
    try:
        klipper_status = get_service_status("klipper")
        moonraker_status = get_service_status("moonraker")
    except Exception:
        pass

    return DashboardState(
        devices=devices,
        status_message=status_message,
        status_level=status_level,
        klipper_status=klipper_status,
        moonraker_status=moonraker_status,
        host_version=host_version,
    )


class StateReady(Message):
    """Carries a freshly fetched :class:`DashboardState` to the UI thread."""

    __slots__ = ("state",)

    def __init__(self, state: DashboardState) -> None:
        super().__init__()
        self.state = state


class FetchDone(Message):
    """Posted (always, in a ``finally``) when a fetch worker exits, so the
    dashboard clears its in-flight guard and background polling can resume."""


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #
class DashboardScreen(Screen[None]):
    """Status + device table + flash log, driven by the engine bridge."""

    DEFAULT_CSS = """
    DashboardScreen .status-line {
        height: auto;
        color: $kf-text;
    }
    """

    _COLUMNS = ("#", "Device", "MCU", "Method", "Conn", "Version")

    BINDINGS = [
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("f", "flash", "Flash"),
        ("b", "flash_all", "Flash All"),
        ("a", "add", "Add"),
        ("e", "edit", "Edit"),
        ("r", "remove", "Remove"),
        ("m", "menuconfig", "Menuconfig"),
        ("d", "refresh", "Refresh"),
        ("c", "settings", "Settings"),
        ("q", "quit", "Quit"),
    ]

    # R2 live-refresh cadences.
    _HOTPLUG_INTERVAL = 2.0  # /dev/serial/by-id mtime poll
    _STATUS_INTERVAL = 5.0  # Klipper/Moonraker status poll

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[DeviceRow] = []
        # The operation screen for the in-flight job. The dashboard stays the
        # bridge's event target and routes events/completion to it while it is
        # on top of the stack (Stage 2 replaces the Stage 1 placeholder log).
        self._operation: Optional[OperationScreen] = None
        # R2 background refresh state.
        self._fetch_in_flight = False  # a fetch worker is running (non-overlap)
        self._last_serial_mtime: Optional[float] = None  # hotplug gate baseline
        self._status_message = ""  # preserved across background polls
        self._status_level = "info"
        # An in-flight R (remove) job routed through the shared bridge. When set,
        # handle_job_completed treats the completion as a removal (not a flash).
        self._pending_remove_key: Optional[str] = None
        self._pending_remove_name: Optional[str] = None

    @property
    def kflash_app(self) -> KflashApp:
        return cast("KflashApp", self.app)

    # -- composition ----------------------------------------------------- #
    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Panel(title="status"):
                yield Static(id="status-message", classes="status-line")
                yield Static(id="status-services", classes="status-line")
                yield Static(id="status-host", classes="status-line")
            with Panel(title="devices"):
                yield DataTable(id="devices", zebra_stripes=False, cursor_type="row")
        yield HintLine(_HINTS_ROW1)
        yield HintLine(_HINTS_ROW2)

    def on_mount(self) -> None:
        table = self.query_one("#devices", DataTable)
        table.add_columns(*self._COLUMNS)
        self._apply_state(
            DashboardState(status_message="Scanning devices...", loading=True)
        )
        table.focus()
        self.refresh_devices("Select a device, then F to flash.", "info")
        # R2: live background refresh. Hotplug (2 s) rescans only when
        # /dev/serial/by-id changes; status (5 s) refreshes Klipper/Moonraker.
        # Both pause while a flash is running or a modal/operation is on top.
        self.set_interval(self._HOTPLUG_INTERVAL, self._poll_hotplug)
        self.set_interval(self._STATUS_INTERVAL, self._poll_status)

    # -- state fetch ----------------------------------------------------- #
    def refresh_devices(
        self, status_message: str, status_level: str, scan_can: bool = False
    ) -> None:
        """Kick a foreground refresh (D / completion), keeping the status line."""
        self._status_message = status_message
        self._status_level = status_level
        self._fetch_in_flight = True
        self._run_fetch(
            self.kflash_app.registry, status_message, status_level, scan_can, False
        )

    # -- R2 background polling ------------------------------------------- #
    def _polling_paused(self) -> bool:
        """True when background polling must hold off (never compete with a
        flash, and never scan under a modal / operation / suspend window)."""
        app = self.kflash_app
        bridge = app.bridge
        if bridge is not None and bridge.is_busy:
            return True
        # A modal, the operation screen, or a suspend window is on top -> the
        # dashboard is not the active screen; don't refresh under it.
        return app.screen is not self

    def _poll_hotplug(self) -> None:
        if self._polling_paused() or self._fetch_in_flight:
            return
        self._fetch_in_flight = True
        self._run_fetch(
            self.kflash_app.registry, self._status_message, self._status_level,
            False, True,
        )

    def _poll_status(self) -> None:
        if self._polling_paused() or self._fetch_in_flight:
            return
        self._fetch_in_flight = True
        self._run_fetch(
            self.kflash_app.registry, self._status_message, self._status_level,
            False, False,
        )

    @work(thread=True, group="dashboard-fetch")
    def _run_fetch(
        self,
        registry,
        status_message: str,
        status_level: str,
        scan_can: bool,
        gate_on_mtime: bool,
    ) -> None:
        """Fetch dashboard state on a worker thread; post it to the UI thread.

        ``registry`` is passed in by the main-thread caller: the worker must
        never touch ``self.app`` (``kflash_app``), which raises
        ``NoActiveAppError`` if the app is torn down mid-fetch — that was a
        real intermittent crash under test teardown. ``post_message`` on a
        closed pump is a safe no-op, so posting needs no guard.

        ``gate_on_mtime`` (hotplug poll) skips the expensive fetch when
        ``/dev/serial/by-id`` is unchanged since the last fetch. ``_FetchDone``
        is always posted so the in-flight guard clears even on error.
        """
        try:
            mtime = _serial_dir_mtime()
            if gate_on_mtime and mtime == self._last_serial_mtime:
                return
            self._last_serial_mtime = mtime
            state = fetch_dashboard_state(
                registry, status_message, status_level, scan_can
            )
            self.post_message(StateReady(state))
        finally:
            self.post_message(FetchDone())

    def on_fetch_done(self, message: FetchDone) -> None:
        self._fetch_in_flight = False

    def on_state_ready(self, message: StateReady) -> None:
        self._apply_state(message.state)

    def _apply_state(self, state: DashboardState) -> None:
        role = _LEVEL_ROLE.get(state.status_level, "text")
        # Remember the current status so background polls repaint it unchanged.
        if not state.loading:
            self._status_message = state.status_message
            self._status_level = state.status_level
        self.query_one("#status-message", Static).update(
            Text(state.status_message, style=COLORS[role])
        )
        self.query_one("#status-services", Static).update(
            self._service_line(state.klipper_status, state.moonraker_status)
        )
        host = self.query_one("#status-host", Static)
        if state.host_version:
            flavor = detect_firmware_flavor(state.host_version)
            host.update(
                Text.assemble(
                    ("Host: ", COLORS["text"]),
                    (f"{flavor} {state.host_version}", COLORS["value"]),
                )
            )
        else:
            host.update(Text("Host: version unavailable", style=COLORS["subtle"]))

        if not state.loading:
            self._populate_table(state.devices, state.host_version)

    def _service_line(self, klipper: str, moonraker: str) -> Text:
        text = Text()
        for index, (label, status) in enumerate(
            (("Klipper", klipper), ("Moonraker", moonraker))
        ):
            if index:
                text.append("    ")
            display, role = _SERVICE_DISPLAY.get(status, ("Unknown", "subtle"))
            text.append(f"{label}: ", style=COLORS["text"])
            text.append(display, style=COLORS[role])
        return text

    # -- device table ---------------------------------------------------- #
    def _populate_table(
        self, devices: list[DeviceRow], host_version: Optional[str]
    ) -> None:
        table = self.query_one("#devices", DataTable)
        # Preserve the highlighted DEVICE across a background rebuild (R2): a
        # hotplug that reorders/adds/removes rows must not yank the cursor to a
        # different device or steal focus. Anchor by device key, fall back to
        # the clamped prior index.
        prior_index = table.cursor_row
        prior_key: Optional[str] = None
        if prior_index is not None and 0 <= prior_index < len(self._rows):
            prior_key = self._rows[prior_index].key
        table.clear()
        self._rows = devices
        for row in devices:
            table.add_row(*self._row_cells(row, host_version))
        if devices:
            target = 0
            if prior_key is not None:
                for index, row in enumerate(devices):
                    if row.key == prior_key:
                        target = index
                        break
                else:
                    if prior_index is not None:
                        target = min(prior_index, len(devices) - 1)
            elif prior_index is not None:
                target = min(prior_index, len(devices) - 1)
            table.move_cursor(row=max(target, 0))

    def _row_cells(self, row: DeviceRow, host_version: Optional[str]) -> list[Text]:
        if row.group == "blocked":
            dim = f"dim {COLORS['subtle']}"
            return [
                Text("", style=dim),
                Text(row.name, style=dim),
                Text("-", style=dim),
                Text("-", style=dim),
                Text("blocked", style=dim),
                Text(row.detail or "-", style=dim),
            ]

        number = Text(str(row.number), style=COLORS["label"])
        if row.flashable and row.connected:
            name = Text(row.name, style=COLORS["text"])
        else:
            name = Text(row.name, style=COLORS["subtle"])
        mcu = Text(row.mcu or "-", style=COLORS["key_info"])
        method = Text(row.method, style=COLORS["subtle"])

        if row.group == "new":
            conn = Text("new", style=COLORS["orange"])
        elif not row.connected:
            conn = Text("offline", style=COLORS["subtle"])
        elif not row.flashable:
            conn = Text("excluded", style=COLORS["orange"])
        else:
            conn = Text("connected", style=COLORS["green"])

        version = self._version_cell(row, host_version)
        return [number, name, mcu, method, conn, version]

    def _version_cell(self, row: DeviceRow, host_version: Optional[str]) -> Text:
        if not row.version:
            return Text("-", style=COLORS["subtle"])
        text = Text(row.version, style=COLORS["subtle"])
        text.append("  ")
        if host_version:
            kind = "warn" if is_mcu_outdated(host_version, row.version) else "ok"
        else:
            kind = "caution"
        text.append_text(status_marker(kind))
        return text

    def _selected_row(self) -> Optional[DeviceRow]:
        table = self.query_one("#devices", DataTable)
        index = table.cursor_row
        if index is None or not (0 <= index < len(self._rows)):
            return None
        return self._rows[index]

    # -- navigation ------------------------------------------------------ #
    def action_cursor_down(self) -> None:
        self.query_one("#devices", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#devices", DataTable).action_cursor_up()

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key.isdigit() and event.key != "0":
            number = int(event.key)
            for index, row in enumerate(self._rows):
                if row.number == number:
                    event.stop()
                    self.query_one("#devices", DataTable).move_cursor(row=index)
                    return

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter on a row == Flash for that row (R3: device is the primary object).
        self.action_flash()

    # -- actions --------------------------------------------------------- #
    def action_flash(self) -> None:
        bridge = self.kflash_app.bridge
        if bridge is not None and bridge.is_busy:
            self._set_status("A flash is already running.", "warning")
            return
        row = self._selected_row()
        if row is None:
            self._set_status("No device selected.", "warning")
            return
        if not row.can_flash:
            self._set_status(
                f"{row.name} is not flashable (blocked/excluded/unregistered).",
                "warning",
            )
            return
        key, name = row.key, row.name

        def _after_confirm(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._menuconfig_gate(key, name)
            else:
                self._set_status(f"Flash cancelled for {name}.", "info")

        self.app.push_screen(ConfirmDialog(f"Flash firmware to '{name}' now?"), _after_confirm)

    # -- menuconfig (§6: suspend + config-diff receipt) ------------------ #
    def _global_config(self):
        """The loaded :class:`~kflash.models.GlobalConfig`, or ``None``."""
        try:
            return self.kflash_app.registry.load().global_config
        except Exception:
            return None

    def _klipper_dir(self) -> Optional[str]:
        """The configured Klipper directory, or ``None`` if no global config."""
        config = self._global_config()
        return None if config is None else config.klipper_dir

    def _menuconfig_gate(self, key: str, name: str) -> None:
        """Between the F confirm and the flash, run/offer menuconfig (§6).

        A device with a cached ``.config`` is *offered* menuconfig (declining
        flashes the cached config as before) -- unless the
        ``menuconfig_before_flash`` setting (default ON) is off, in which case
        the cached config flashes directly with no prompt. A device with no
        cached config *requires* menuconfig before its first flash regardless
        of the setting -- mirroring what ``cmd_flash`` would otherwise do on
        the worker thread (which cannot host the ncurses subprocess); MCU
        validation still happens engine-side during the flash job.
        """
        config = self._global_config()
        if config is None:
            # No global config to build against; let cmd_flash report it.
            self._start_flash(key, name)
            return
        klipper_dir = config.klipper_dir
        if menuconfig.has_cached_config(key, klipper_dir):
            if not config.menuconfig_before_flash:
                self._start_flash(key, name)
                return

            def _after_offer(edit: Optional[bool]) -> None:
                if edit:
                    self._run_menuconfig(key, name, klipper_dir, required=False)
                else:
                    self._start_flash(key, name)

            self.app.push_screen(
                DecisionConfirmDialog(
                    "Edit firmware config (menuconfig) first?",
                    default=False,
                    title="menuconfig",
                ),
                _after_offer,
            )
        else:
            self._run_menuconfig(key, name, klipper_dir, required=True)

    def action_menuconfig(self) -> None:
        """M: open menuconfig directly for the selected device -- no flash.

        Hardware feedback: reaching menuconfig only through F (flash) was not
        intuitive. This is the standalone entry: suspend, edit, save to the
        device's config cache, show the close-only diff receipt, done. The next
        flash picks the config up; nothing is built or flashed here.
        """
        bridge = self.kflash_app.bridge
        if bridge is not None and bridge.is_busy:
            self._set_status("An operation is already running.", "warning")
            return
        row = self._selected_row()
        if row is None:
            self._set_status("No device selected.", "warning")
            return
        if row.group != "registered":
            self._set_status(
                f"{row.name} is not registered; add it first (A).", "warning"
            )
            return
        klipper_dir = self._klipper_dir()
        if klipper_dir is None:
            self._set_status("Klipper directory not configured (C).", "error")
            return
        key, name = row.key, row.name
        result = menuconfig.run_menuconfig_suspended(self.app, key, klipper_dir)
        if result.cancelled:
            self._set_status(f"menuconfig cancelled for {name}.", "info")
            return
        if result.error:
            self._set_status(f"menuconfig failed: {result.error}", "error")
            return
        if result.changed:

            def _after_receipt(_val: Optional[bool]) -> None:
                self._set_status(
                    f"Config updated for {name} "
                    f"({result.lines_changed} lines changed).",
                    "success",
                )

            self.app.push_screen(
                menuconfig.ConfigDiffDialog(result, show_cancel=False),
                _after_receipt,
            )
        else:
            self._set_status(f"menuconfig saved no changes for {name}.", "info")

    def _run_menuconfig(
        self, key: str, name: str, klipper_dir: str, *, required: bool
    ) -> None:
        """Suspend, run menuconfig, then show the diff receipt or proceed."""
        result = menuconfig.run_menuconfig_suspended(self.app, key, klipper_dir)
        if result.cancelled:
            self.refresh_devices(
                f"menuconfig cancelled; flash aborted for {name}.", "warning"
            )
            return
        if result.error:
            self.refresh_devices(f"menuconfig failed: {result.error}", "error")
            return
        if required and not menuconfig.has_cached_config(key, klipper_dir):
            self.refresh_devices(
                f"No config saved for {name}; a flash needs a saved config.",
                "warning",
            )
            return
        if result.changed:

            def _after_diff(cont: Optional[bool]) -> None:
                if cont:
                    self._start_flash(key, name)
                else:
                    self.refresh_devices(f"Flash cancelled for {name}.", "info")

            # The diff is a receipt (menuconfig already saved); Y/N decides the
            # FLASH, and the dialog must say so (hardware-feedback fix).
            self.app.push_screen(
                menuconfig.ConfigDiffDialog(
                    result,
                    question=f"Flash '{name}' with this config?",
                    continue_label="Flash now",
                    cancel_label="Cancel flash",
                ),
                _after_diff,
            )
        else:
            # No net change (unchanged or exited without saving over an existing
            # cache): proceed to the flash with the current config.
            self._start_flash(key, name)

    def _start_flash(self, key: str, name: str) -> None:
        # (a) Pre-acquire sudo OUTSIDE the raw-mode app so cmd_flash's internal
        #     acquire_sudo hits cached credentials and never prompts inside the
        #     Textual event loop. Only needed when the service is up and
        #     passwordless sudo is unavailable.
        if not self._preacquire_sudo():
            self.refresh_devices(
                f"Sudo prompt cancelled; flash aborted for {name}.", "warning"
            )
            return

        # (b) Launch the flash on the bridge's non-daemon worker thread.
        bridge = self.kflash_app.bridge
        if bridge is None:
            self._set_status("Engine bridge unavailable.", "error")
            return
        emitter = Emitter(bridge.events)
        decider = bridge.decisions
        registry = self.kflash_app.registry

        def _job() -> int:
            # cmd_flash is resolved at call time so tests can stub it.
            # skip_menuconfig=True: the UI already ran menuconfig (or the user
            # declined / the gate is off) via _menuconfig_gate under
            # app.suspend() -- the worker thread cannot host ncurses.
            return cmd_flash(registry, key, emitter, decider, skip_menuconfig=True)

        self._run_operation(_job, mode="single", title=name, name="kflash-flash")

    def _start_flash_all(self, count: int) -> None:
        if not self._preacquire_sudo():
            self.refresh_devices("Sudo prompt cancelled; Flash All aborted.", "warning")
            return
        bridge = self.kflash_app.bridge
        if bridge is None:
            self._set_status("Engine bridge unavailable.", "error")
            return
        emitter = Emitter(bridge.events)
        decider = bridge.decisions
        registry = self.kflash_app.registry

        def _job() -> int:
            # cmd_flash_all resolved at call time so tests can stub it.
            return cmd_flash_all(registry, emitter, decider)

        self._run_operation(
            _job, mode="all", title=f"{count} device(s)", name="kflash-flash-all"
        )

    def _run_operation(self, job, *, mode: str, title: str, name: str) -> None:
        """Push an OperationScreen and run *job* through the bridge into it."""
        bridge = self.kflash_app.bridge
        assert bridge is not None
        operation = OperationScreen(mode=mode, title=title)
        try:
            bridge.run_engine_job(job, name=name)
        except EngineBusyError:
            self._set_status("A flash is already running.", "warning")
            return
        self._operation = operation
        self.app.push_screen(operation, self._on_operation_closed)
        self._set_status(f"Flashing {title}...", "info")

    def _on_operation_closed(self, _result) -> None:
        self._operation = None

    def _preacquire_sudo(self) -> bool:
        """Cache sudo credentials before the flash. Returns ``False`` only when
        the user pressed Ctrl+C at the sudo prompt (caller aborts the flash and
        returns to the dashboard rather than the app exiting)."""
        try:
            if not (is_service_active() and not verify_passwordless_sudo()):
                return True
        except Exception:
            return True
        # suspend() drops out of the alt-screen so the sudo password prompt is
        # visible; headless test drivers can't suspend, so fall back to a direct
        # call (guarded checks above keep tests from ever reaching here).
        from textual.app import SuspendNotSupported

        try:
            try:
                with self.app.suspend():
                    acquire_sudo()
            except SuspendNotSupported:
                acquire_sudo()
        except KeyboardInterrupt:
            # Ctrl+C during the sudo suspend window: abort the flash, don't exit.
            return False
        except Exception:
            pass
        return True

    # -- flash event stream + completion --------------------------------- #
    def on_engine_event(self, message: EngineEvent) -> None:
        # The dashboard is the bridge's event target; route the stream to the
        # operation screen that owns the in-flight job.
        if self._operation is not None:
            self._operation.ingest(message.event)

    def handle_job_completed(self, message: EngineJobCompleted) -> None:
        # An R (remove) job has no operation screen; finalize it separately.
        if self._pending_remove_name is not None:
            self._handle_remove_completed(message)
            return
        # Hand the outcome to the operation screen (it holds on failure until
        # the user returns) and refresh the device list underneath so the
        # dashboard is current when the user comes back. The completion is
        # RE-POSTED onto the operation screen's own pump (not called directly)
        # so it lands after every buffered engine event -- events flow to this
        # dashboard's pump, the completion to the app's, and a direct call could
        # finalize the checklist before the last events are processed.
        if self._operation is not None:
            self._operation.post_message(
                EngineJobCompleted(
                    result=message.result,
                    error=message.error,
                    cancelled=message.cancelled,
                )
            )
        if message.cancelled:
            self.refresh_devices("Flash cancelled.", "warning")
        elif message.ok and message.result == 0:
            self.refresh_devices("Flash completed successfully.", "success")
        else:
            self.refresh_devices("Flash failed. See the operation screen.", "error")

    # -- Flash All ------------------------------------------------------- #
    def action_flash_all(self) -> None:
        bridge = self.kflash_app.bridge
        if bridge is not None and bridge.is_busy:
            self._set_status("A flash is already running.", "warning")
            return
        count = sum(
            1
            for row in self._rows
            if row.group == "registered" and row.flashable and row.connected
        )
        if count == 0:
            self._set_status("No connected flashable devices to flash.", "warning")
            return

        def _after_confirm(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._start_flash_all(count)
            else:
                self._set_status("Flash All cancelled.", "info")

        self.app.push_screen(
            ConfirmDialog(f"Flash all {count} connected devices?"), _after_confirm
        )

    # -- stubbed / deferred actions -------------------------------------- #
    def action_add(self) -> None:
        # Add-device wizard (A). A highlighted scanned "new" USB row is passed
        # straight through as the selected device (skips discovery); otherwise
        # the wizard runs a fresh scan and offers CAN registration. Imports are
        # function-local to keep this handler's diff localized.
        from ...models import DiscoveredDevice
        from .add_device import AddDeviceScreen

        bridge = self.kflash_app.bridge
        if bridge is not None and bridge.is_busy:
            self._set_status("An operation is already running.", "warning")
            return
        row = self._selected_row()
        if row is not None and row.group == "new":
            device = DiscoveredDevice(
                path=f"/dev/serial/by-id/{row.serial_path}",
                filename=row.serial_path,
            )
            self.app.push_screen(AddDeviceScreen(selected_device=device))
        else:
            self.app.push_screen(AddDeviceScreen())

    def action_edit(self) -> None:
        # Per-device config (E). A registered row opens the config editor; a
        # scanned "new" row offers to register it instead (legacy parity);
        # blocked rows have nothing to configure. Imports are function-local to
        # keep this handler's diff localized.
        from .device_config import DeviceConfigScreen

        bridge = self.kflash_app.bridge
        if bridge is not None and bridge.is_busy:
            self._set_status("An operation is already running.", "warning")
            return
        row = self._selected_row()
        if row is None:
            self._set_status("No device selected.", "warning")
            return
        if row.group == "blocked":
            self._set_status(f"{row.name} is blocked; nothing to configure.", "warning")
            return
        if row.group == "new":
            # Unregistered: offer to add it (routes into the add-device flow).
            def _after_offer(confirmed: Optional[bool]) -> None:
                if confirmed:
                    self._open_add_for_new(row)
                else:
                    self._set_status("Configuration cancelled.", "info")

            self.app.push_screen(
                DecisionConfirmDialog(
                    "Device not registered. Add it now?",
                    default=True,
                    title="add device",
                ),
                _after_offer,
            )
            return
        self.app.push_screen(DeviceConfigScreen(row.key))

    def _open_add_for_new(self, row: DeviceRow) -> None:
        """Route a scanned "new" row into the add-device wizard (E-on-new / A)."""
        from ...models import DiscoveredDevice
        from .add_device import AddDeviceScreen

        if row.is_can:
            self.app.push_screen(AddDeviceScreen(can_only=True))
        else:
            device = DiscoveredDevice(
                path=f"/dev/serial/by-id/{row.serial_path}",
                filename=row.serial_path,
            )
            self.app.push_screen(AddDeviceScreen(selected_device=device))

    def action_remove(self) -> None:
        # Remove device (R). Only registered rows can be removed; the removal is
        # the fully decider-driven cmd_remove_device run through the shared
        # bridge, so its "Remove '<name>'?" confirm and the "remove cached
        # config?" prompt render as the styled R4 modals over the dashboard.
        bridge = self.kflash_app.bridge
        if bridge is not None and bridge.is_busy:
            self._set_status("An operation is already running.", "warning")
            return
        row = self._selected_row()
        if row is None:
            self._set_status("No device selected.", "warning")
            return
        if row.group != "registered":
            self._set_status(
                f"{row.name} is not registered; nothing to remove.", "warning"
            )
            return
        if bridge is None:
            self._set_status("Engine bridge unavailable.", "error")
            return
        key, name = row.key, row.name
        emitter = Emitter(bridge.events)
        decider = bridge.decisions
        registry = self.kflash_app.registry

        def _job() -> int:
            # cmd_remove_device resolved at call time so tests can stub it.
            return cmd_remove_device(registry, key, emitter, decider)

        self._pending_remove_key = key
        self._pending_remove_name = name
        try:
            bridge.run_engine_job(_job, name="kflash-remove-device")
        except EngineBusyError:
            self._pending_remove_key = None
            self._pending_remove_name = None
            self._set_status("An operation is already running.", "warning")
            return
        self._set_status(f"Removing {name}...", "info")

    def _handle_remove_completed(self, message: EngineJobCompleted) -> None:
        """Finalize an R removal: report the outcome and refresh the list.

        The decline-confirm and success cases both return exit code 0 from
        cmd_remove_device, so the registry is re-read to tell "removed" from
        "cancelled" and produce the right status message.
        """
        name = self._pending_remove_name or "device"
        key = self._pending_remove_key
        self._pending_remove_key = None
        self._pending_remove_name = None
        if message.cancelled:
            self.refresh_devices(f"Removal cancelled for {name}.", "warning")
            return
        if message.error is not None:
            self.refresh_devices(f"Remove failed: {message.error}", "error")
            return
        removed = False
        if key is not None:
            try:
                removed = self.kflash_app.registry.get(key) is None
            except Exception:
                removed = False
        if removed:
            self.refresh_devices(f"Removed {name}.", "success")
        else:
            self.refresh_devices(f"Removal cancelled for {name}.", "info")

    def action_settings(self) -> None:
        # Global settings editor (C). Function-local import keeps the diff local.
        from .settings import SettingsScreen

        self.app.push_screen(SettingsScreen())

    def action_refresh(self) -> None:
        # D triggers the optional unregistered-CAN scan (legacy parity), gated by
        # can_scan_on_refresh -- the 2 s/5 s poll loops never scan CAN.
        scan_can = False
        try:
            data = self.kflash_app.registry.load()
            if data.global_config is not None:
                scan_can = bool(data.global_config.can_scan_on_refresh)
        except Exception:
            pass
        message = "Scanning CAN bus + devices..." if scan_can else "Refreshing devices..."
        self.refresh_devices(message, "info", scan_can=scan_can)

    def action_quit(self) -> None:
        self.app.exit()

    # -- helpers --------------------------------------------------------- #
    def _set_status(self, message: str, level: str) -> None:
        # Track the current status so a background poll repaints it unchanged.
        self._status_message = message
        self._status_level = level
        role = _LEVEL_ROLE.get(level, "text")
        self.query_one("#status-message", Static).update(
            Text(message, style=COLORS[role])
        )
