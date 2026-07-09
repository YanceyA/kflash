"""The per-device config editor (dashboard action ``E``).

Ports the legacy per-device config screen (``kflash.tui._device_config_screen`` +
``_save_device_edits``) to the Textual skin, using the settings-editor grammar
(``kflash.ui.screens.settings``): a cursor-driven table of editable fields, inline
edits through the R4 modals, edits collected into a pending dict with a visible
dirty marker, ``S`` to save the whole batch, ``Esc`` to cancel (confirming first
when there are unsaved changes). On save the dashboard is refreshed.

Field catalogue and applicability rules are a local copy of the frozen legacy
renderer's ``kflash.screen.DEVICE_SETTINGS_FIELDS`` / ``_is_sub_field_applicable``
(that module is the ANSI renderer and may not be imported by UI code -- see
tests/test_layering.py). Kept in sync by this module.

Rename / key semantics (faithful port)
--------------------------------------
Legacy ``_save_device_edits`` saves via ``Registry.update_device(original_key,
**pending)``. The registry ``key`` is NOT in the updatable field set, so renaming
a device's display ``name`` never regenerates its key: the key is the immutable
slug minted once at add time (``generate_device_key``) and the config-cache
directory (``config.get_config_dir(key)``) stays put across renames. This editor
preserves that exactly -- it edits ``name`` and never touches the key, so no
``rename_device_config_cache`` move is needed (legacy makes none either).
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Optional, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.content import Content
from textual.screen import Screen
from textual.widgets import DataTable, Static

from ...decisions import ChooseFlashMethodDecision
from ...models import DeviceEntry
from ...moonraker import get_mcu_serial_map
from ...validation import (
    find_flash_method_pair,
    validate_bootloader_baud,
    validate_can_interface,
    validate_canbus_uuid,
)
from ..dialogs import (
    ChoiceDialog,
    DecisionConfirmDialog,
    FlashMethodDialog,
    TextPromptDialog,
)
from ..skin import COLORS, HintLine, Panel, spaced_title

if TYPE_CHECKING:
    from ..app import KflashApp

# Field catalogue -- a local copy of kflash.screen.DEVICE_SETTINGS_FIELDS (that
# module is the frozen legacy renderer and must not be imported by UI code).
# Keep in sync. ``role`` applies only to CAN devices; the five ``sub_text``
# fields apply only when the current flash-method pair requires them.
_FIELDS: list[dict] = [
    {"key": "name", "label": "Display name", "type": "text"},
    {"key": "mcu_name", "label": "Klipper MCU name", "type": "mcu"},
    {"key": "flash_method_pair", "label": "Flash method", "type": "method"},
    {"key": "flashable", "label": "Include in flash", "type": "toggle"},
    {"key": "role", "label": "CAN device role", "type": "role"},
    {"key": "canbus_uuid", "label": "CAN bus UUID", "type": "sub_text"},
    {"key": "canbus_interface", "label": "CAN bus interface", "type": "sub_text"},
    {"key": "bootloader_baud", "label": "Bootloader baud rate", "type": "sub_text"},
    {"key": "uf2_mount_path", "label": "UF2 mount path", "type": "sub_text"},
    {"key": "sdcard_board", "label": "SD card board name", "type": "sub_text"},
    {"key": "notes", "label": "Notes", "type": "text"},
]

_HINTS: list[tuple[str, str]] = [
    ("Up/Dn", "Move"),
    ("Enter", "Edit"),
    ("S", "Save"),
    ("Esc", "Back"),
]

_LEVEL_ROLE: dict[str, str] = {
    "success": "green",
    "error": "red",
    "warning": "yellow",
    "info": "text",
}

# Sentinels for the MCU-name picker choices (distinct from any real MCU name).
_MCU_MANUAL = object()
_MCU_CLEAR = object()

# CAN role choices: values are strings so ``None`` stays free to mean "escape /
# no change" (the "(none)" role maps the string "none" -> ``None`` on apply).
_ROLE_CHOICES: list[tuple[str, str]] = [
    ("none", "(none)"),
    ("toolhead", "toolhead"),
    ("bridge", "bridge"),
]


def _is_sub_field_applicable(entry: DeviceEntry, field_key: str) -> bool:
    """Whether a conditional sub-field applies for *entry*'s current flash pair.

    Local reimplementation of ``kflash.screen._is_sub_field_applicable`` (pair
    driven): each :class:`~kflash.validation.FlashMethodPair` declares the
    sub-fields it requires; if no pair matches the current bootloader+flash
    values, no sub-field applies.
    """
    pair = find_flash_method_pair(entry.bootloader_method, entry.flash_command)
    if pair is None:
        return False
    return field_key in pair.required_sub_fields


def _format_last_flash(iso_str: Optional[str]) -> str:
    """Format an ISO timestamp as ``YYYY-MM-DD HH:MM (X ago)`` for display.

    Local reimplementation of ``kflash.panels.format_timestamp_relative`` (that
    renderer is off-limits to UI code). Returns the raw string if unparseable.
    """
    if not iso_str:
        return "No recorded flash"
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return iso_str
    delta = datetime.now() - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        relative = "in the future"
    elif seconds < 60:
        relative = "just now"
    elif seconds < 3600:
        minutes = seconds // 60
        relative = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    elif seconds < 86400:
        hours = seconds // 3600
        relative = f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        days = seconds // 86400
        relative = f"{days} day{'s' if days != 1 else ''} ago"
    return f"{dt.strftime('%Y-%m-%d %H:%M')} ({relative})"


class DeviceConfigScreen(Screen[None]):
    """Per-device config editor: collect edits, save on ``s``, discard on ``Esc``."""

    DEFAULT_CSS = """
    DeviceConfigScreen .status-line {
        height: auto;
        color: $kf-text;
    }
    """

    _COLUMNS = ("#", "Setting", "Value")

    BINDINGS = [
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("s", "save", "Save"),
        ("escape", "cancel", "Back"),
        ("q", "cancel", "Back"),
    ]

    def __init__(self, device_key: str) -> None:
        super().__init__()
        self._original_key = device_key
        self._entry: Optional[DeviceEntry] = None
        self._pending: dict[str, Any] = {}

    @property
    def kflash_app(self) -> KflashApp:
        return cast("KflashApp", self.app)

    # -- composition ----------------------------------------------------- #
    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Panel(title="device identity"):
                yield Static(id="device-identity", classes="status-line")
            with Panel(title="device config", id="config-panel"):
                yield Static(id="device-status", classes="status-line")
                yield DataTable(id="device-fields", zebra_stripes=False, cursor_type="row")
        yield HintLine(_HINTS)

    def on_mount(self) -> None:
        self._entry = self.kflash_app.registry.get(self._original_key)
        table = self.query_one("#device-fields", DataTable)
        table.add_columns(*self._COLUMNS)
        if self._entry is None:
            self._set_status("Device not found; nothing to edit.", "error")
            return
        self._render_identity()
        self._populate()
        table.focus()
        self._set_status("Select a field, Enter to edit, S to save.", "info")

    # -- working copy ---------------------------------------------------- #
    def _working(self) -> DeviceEntry:
        """The saved entry with the pending edits overlaid (legacy ``working``)."""
        assert self._entry is not None
        return dataclasses.replace(self._entry, **self._pending)

    # -- identity (read-only) -------------------------------------------- #
    def _render_identity(self) -> None:
        entry = self._entry
        assert entry is not None
        text = Text()
        text.append("MCU Type: ", style=COLORS["text"])
        text.append(f"{entry.mcu or '-'}\n", style=COLORS["value"])
        text.append("Serial Pattern: ", style=COLORS["text"])
        if entry.serial_pattern:
            text.append(f"{entry.serial_pattern}\n", style=COLORS["value"])
        else:
            text.append("CAN device (no serial path)\n", style=COLORS["subtle"])
        text.append("Last Flashed: ", style=COLORS["text"])
        last = _format_last_flash(entry.last_flash_timestamp)
        role = "value" if entry.last_flash_timestamp else "subtle"
        text.append(last, style=COLORS[role])
        self.query_one("#device-identity", Static).update(text)

    # -- table ----------------------------------------------------------- #
    def _field_state(self, field: dict, working: DeviceEntry) -> tuple[Text, bool]:
        """Return (value cell, applicable) for *field* against the working copy."""
        key = field["key"]
        ftype = field["type"]

        if ftype == "toggle":
            value = getattr(working, key)
            cell = Text(
                "On" if value else "Off",
                style=COLORS["green"] if value else COLORS["subtle"],
            )
            return cell, True

        if ftype == "method":
            pair = find_flash_method_pair(
                working.bootloader_method, working.flash_command
            )
            display = pair.name if pair is not None else "(not set)"
            return Text(display, style=COLORS["value"]), True

        if ftype == "mcu":
            display = working.mcu_name or "(not set)"
            return Text(display, style=COLORS["value"]), True

        if ftype == "role":
            applicable = working.is_can_device
            if not applicable:
                return Text("(CAN only)", style=COLORS["subtle"]), False
            return Text(working.role or "(none)", style=COLORS["value"]), True

        if ftype == "sub_text":
            applicable = _is_sub_field_applicable(working, key)
            value = getattr(working, key)
            if not applicable:
                display = str(value) if value else "(n/a)"
                return Text(display, style=COLORS["subtle"]), False
            if value is None or value == "":
                return Text("(required)", style=COLORS["yellow"]), True
            return Text(str(value), style=COLORS["value"]), True

        # plain text (name, notes)
        value = getattr(working, key)
        return Text(str(value) if value else "", style=COLORS["value"]), True

    def _value_cell(self, field: dict, working: DeviceEntry) -> Text:
        cell, applicable = self._field_state(field, working)
        if not applicable:
            return cell
        if self._is_dirty(field):
            cell = cell.copy()
            cell.append("  *", style=COLORS["orange"])
        return cell

    def _is_dirty(self, field: dict) -> bool:
        if field["type"] == "method":
            return "bootloader_method" in self._pending or "flash_command" in self._pending
        return field["key"] in self._pending

    def _populate(self) -> None:
        table = self.query_one("#device-fields", DataTable)
        working = self._working()
        prior = table.cursor_row
        table.clear()
        for index, field in enumerate(_FIELDS, start=1):
            cell, applicable = self._field_state(field, working)
            number_role = "label" if applicable else "subtle"
            label_role = "text" if applicable else "subtle"
            number = Text(str(index), style=COLORS[number_role])
            label = Text(field["label"], style=COLORS[label_role])
            table.add_row(number, label, self._value_cell(field, working))
        target = min(prior, len(_FIELDS) - 1) if prior is not None else 0
        table.move_cursor(row=max(target, 0))
        self._update_dirty_title()

    def _update_dirty_title(self) -> None:
        panel = self.query_one("#config-panel", Panel)
        title = "device config *" if self._pending else "device config"
        panel.border_title = Content(spaced_title(title))

    # -- navigation ------------------------------------------------------ #
    def action_cursor_down(self) -> None:
        self.query_one("#device-fields", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#device-fields", DataTable).action_cursor_up()

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key.isdigit() and event.key != "0":
            number = int(event.key)
            if 1 <= number <= len(_FIELDS):
                event.stop()
                self.query_one("#device-fields", DataTable).move_cursor(row=number - 1)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._edit_selected()

    def _selected_field(self) -> Optional[dict]:
        index = self.query_one("#device-fields", DataTable).cursor_row
        if index is None or not (0 <= index < len(_FIELDS)):
            return None
        return _FIELDS[index]

    # -- editing --------------------------------------------------------- #
    def _edit_selected(self) -> None:
        if self._entry is None:
            return
        field = self._selected_field()
        if field is None:
            return
        ftype = field["type"]
        if ftype == "toggle":
            self._edit_toggle(field)
        elif ftype == "text":
            self._edit_text(field)
        elif ftype == "mcu":
            self._edit_mcu_name()
        elif ftype == "method":
            self._edit_method()
        elif ftype == "role":
            self._edit_role()
        elif ftype == "sub_text":
            self._edit_sub_text(field)

    def _edit_toggle(self, field: dict) -> None:
        key = field["key"]
        self._apply(key, not getattr(self._working(), key))

    def _edit_text(self, field: dict) -> None:
        key = field["key"]
        current = getattr(self._working(), key) or ""

        def _after(raw: Optional[str]) -> None:
            if raw is None:  # Escape -> no change
                return
            value = raw.strip()
            if key == "name":
                self._apply_name(value)
            elif key == "notes":
                # Empty clears notes to None (legacy notes semantics).
                self._apply(key, value if value else None)
            elif value:
                self._apply(key, value)

        self.app.push_screen(
            TextPromptDialog(field["label"], default=str(current), title="edit"),
            _after,
        )

    def _apply_name(self, value: str) -> None:
        if not value:  # empty -> no change (legacy)
            return
        existing = {
            entry.name.casefold()
            for entry in self.kflash_app.registry.load().devices.values()
            if entry.key != self._original_key
        }
        if value.casefold() in existing:
            self._set_status("A different device already uses that name.", "error")
            return
        self._apply("name", value)

    def _edit_mcu_name(self) -> None:
        """MCU-name picker: Moonraker MCU list (+ manual / clear) or a text prompt.

        Ports ``_device_config_screen``'s key-2 flow: query Moonraker for the
        MCU-object -> serial map; when MCUs with serial paths exist, offer them
        as a choice list (plus manual entry and clear); otherwise fall back to a
        free-text prompt (blank clears).
        """
        try:
            mcu_serials = get_mcu_serial_map()
        except Exception:
            mcu_serials = None
        names: list[str] = []
        if mcu_serials:
            names = [name for name, serial in mcu_serials.items() if serial is not None]
        if names:
            options: list[tuple[Any, str]] = [(name, name) for name in names]
            options.append((_MCU_MANUAL, "Enter manually"))
            options.append((_MCU_CLEAR, "Clear MCU name"))
            current_index: Optional[int] = None
            working = self._working()
            if working.mcu_name in names:
                current_index = names.index(working.mcu_name)

            def _after_choice(choice: Any) -> None:
                if choice is None:  # Escape
                    return
                if choice is _MCU_CLEAR:
                    self._apply("mcu_name", None)
                elif choice is _MCU_MANUAL:
                    self._prompt_manual_mcu_name()
                else:
                    self._apply("mcu_name", choice)

            self.app.push_screen(
                ChoiceDialog(
                    "Select Klipper MCU name",
                    options,
                    escape_value=None,
                    current_index=current_index,
                    title="mcu name",
                ),
                _after_choice,
            )
        else:
            self._prompt_manual_mcu_name()

    def _prompt_manual_mcu_name(self) -> None:
        current = self._working().mcu_name or ""

        def _after(raw: Optional[str]) -> None:
            if raw is None:  # Escape -> no change
                return
            value = raw.strip()
            self._apply("mcu_name", value if value else None)

        self.app.push_screen(
            TextPromptDialog(
                "Klipper MCU name (blank to clear)", default=str(current), title="mcu name"
            ),
            _after,
        )

    def _edit_method(self) -> None:
        working = self._working()
        request = ChooseFlashMethodDecision(
            current_bootloader=working.bootloader_method,
            current_flash_command=working.flash_command,
            device_name=working.name,
            mcu=working.mcu,
            is_can_device=working.is_can_device,
        )

        def _after(result: Optional[tuple]) -> None:
            if result is None:  # Escape / cancel -> no change
                return
            bootloader, flash_command = result
            self._stage("bootloader_method", bootloader)
            self._stage("flash_command", flash_command)
            self._populate()
            self._set_status("Edited flash method (unsaved).", "info")

        self.app.push_screen(FlashMethodDialog(request), _after)

    def _edit_role(self) -> None:
        working = self._working()
        if not working.is_can_device:
            self._set_status("CAN device role applies to CAN devices only.", "warning")
            return
        current_index = 0
        for index, (value, _label) in enumerate(_ROLE_CHOICES):
            if (value == "none" and working.role is None) or value == working.role:
                current_index = index
                break

        def _after(choice: Optional[str]) -> None:
            if choice is None:  # Escape -> no change
                return
            self._apply("role", None if choice == "none" else choice)

        self.app.push_screen(
            ChoiceDialog(
                "Select CAN device role",
                list(_ROLE_CHOICES),
                escape_value=None,
                current_index=current_index,
                title="role",
            ),
            _after,
        )

    def _edit_sub_text(self, field: dict) -> None:
        key = field["key"]
        working = self._working()
        if not _is_sub_field_applicable(working, key):
            self._set_status(
                f"{field['label']} does not apply to the current flash method.",
                "warning",
            )
            return
        current = getattr(working, key)
        default = "" if current is None else str(current)

        def _after(raw: Optional[str]) -> None:
            if raw is None:  # Escape -> no change
                return
            value = raw.strip()
            self._apply_sub_text(key, value)

        self.app.push_screen(
            TextPromptDialog(field["label"], default=default, title="edit"),
            _after,
        )

    def _apply_sub_text(self, key: str, value: str) -> None:
        if key == "canbus_uuid":
            if not value:
                return
            normalized = value.lower()
            ok, err = validate_canbus_uuid(normalized)
            if ok:
                self._apply(key, normalized)
            else:
                self._set_status(err, "error")
        elif key == "canbus_interface":
            if not value:
                return
            ok, err = validate_can_interface(value)
            if ok:
                self._apply(key, value)
            else:
                self._set_status(err, "error")
        elif key == "bootloader_baud":
            if not value:
                # Legacy: an empty baud entry applies the default (250000).
                default = getattr(self._working(), key) or 250000
                self._apply(key, default)
                return
            try:
                baud = int(value)
            except ValueError:
                self._set_status("Invalid baud rate.", "error")
                return
            ok, err = validate_bootloader_baud(baud)
            if ok:
                self._apply(key, baud)
            else:
                self._set_status(err, "error")
        else:  # uf2_mount_path, sdcard_board -- free text, empty is no change
            if value:
                self._apply(key, value)

    # -- pending bookkeeping --------------------------------------------- #
    def _stage(self, key: str, value: Any) -> None:
        """Record an edit only if it differs from the saved value."""
        assert self._entry is not None
        if value == getattr(self._entry, key):
            self._pending.pop(key, None)
        else:
            self._pending[key] = value

    def _apply(self, key: str, value: Any) -> None:
        self._stage(key, value)
        self._populate()
        self._set_status(f"Edited {key} (unsaved).", "info")

    # -- save / cancel --------------------------------------------------- #
    def action_save(self) -> None:
        if self._entry is None:
            return
        if not self._pending:
            self._set_status("No changes to save.", "info")
            return
        # Faithful port: save via update_device(original_key, ...). The key is
        # never regenerated on rename, so the config-cache dir stays put.
        self.kflash_app.registry.update_device(self._original_key, **self._pending)
        self._entry = self.kflash_app.registry.get(self._original_key) or self._entry
        self._pending.clear()
        self._render_identity()
        self._populate()
        self._set_status("Device saved.", "success")
        dashboard = self.kflash_app._dashboard
        if dashboard is not None:
            dashboard.refresh_devices(f"Saved {self._entry.name}.", "success")

    def action_cancel(self) -> None:
        if self._pending:

            def _after(discard: Optional[bool]) -> None:
                if discard:
                    self._pending.clear()
                    self._close()

            self.app.push_screen(
                DecisionConfirmDialog(
                    "Discard unsaved changes?", default=False, title="discard"
                ),
                _after,
            )
            return
        self._close()

    def _close(self) -> None:
        if self is self.app.screen:
            self.app.pop_screen()

    # -- helpers --------------------------------------------------------- #
    def _set_status(self, message: str, level: str) -> None:
        role = _LEVEL_ROLE.get(level, "text")
        self.query_one("#device-status", Static).update(
            Text(message, style=COLORS[role])
        )
