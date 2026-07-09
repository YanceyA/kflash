"""The global settings editor (dashboard action ``C``).

Mirrors the legacy ``tui._config_screen``: the same field list, the same
validators, the same defaults, rendered as a cursor-driven table with inline
edits through the R4 modals. The field catalogue is reproduced locally (it lives
in the frozen legacy renderer ``kflash.screen.SETTINGS``, which UI modules may
not import -- see tests/test_layering.py) and kept in sync by this comment.

Edits collect into a pending dict with a visible dirty marker; ``s`` saves the
whole batch via the sanctioned :meth:`kflash.registry.Registry.save_global`, and
``Esc`` discards. On save the dashboard status is refreshed.
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

from ...validation import validate_numeric_setting, validate_path_setting
from ..dialogs import DecisionConfirmDialog, TextPromptDialog
from ..skin import COLORS, HintLine, Panel, spaced_title

if TYPE_CHECKING:
    from ..app import KflashApp

# Field catalogue -- a local copy of kflash.screen.SETTINGS (that module is the
# frozen legacy renderer and must not be imported by UI code). Keep in sync.
_SETTINGS: list[dict] = [
    {
        "key": "menuconfig_before_flash",
        "label": "Menuconfig prompt before flash",
        "type": "toggle",
    },
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


class SettingsScreen(Screen[None]):
    """Global settings editor: collect edits, save on ``s``, discard on ``Esc``."""

    _COLUMNS = ("#", "Setting", "Value")

    BINDINGS = [
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("s", "save", "Save"),
        ("escape", "cancel", "Back"),
        ("q", "cancel", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._config: Any = None
        self._pending: dict[str, Any] = {}

    @property
    def kflash_app(self) -> KflashApp:
        return cast("KflashApp", self.app)

    # -- composition ----------------------------------------------------- #
    def compose(self) -> ComposeResult:
        with VerticalScroll(), Panel(title="settings"):
            yield Static(id="settings-status", classes="status-line")
            yield DataTable(id="settings", zebra_stripes=False, cursor_type="row")
        yield HintLine(_HINTS)

    def on_mount(self) -> None:
        self._config = self.kflash_app.registry.load().global_config
        table = self.query_one("#settings", DataTable)
        table.add_columns(*self._COLUMNS)
        self._populate()
        table.focus()
        self._set_status("Select a setting, Enter to edit, S to save.", "info")

    # -- value helpers --------------------------------------------------- #
    def _current_value(self, key: str) -> Any:
        if key in self._pending:
            return self._pending[key]
        return getattr(self._config, key)

    def _value_cell(self, setting: dict) -> Text:
        key = setting["key"]
        value = self._current_value(key)
        dirty = key in self._pending
        text = Text()
        if setting["type"] == "toggle":
            text.append(
                "On" if value else "Off",
                style=COLORS["green"] if value else COLORS["subtle"],
            )
        else:
            text.append(str(value), style=COLORS["value"])
        if dirty:
            text.append("  *", style=COLORS["orange"])
        return text

    def _populate(self) -> None:
        table = self.query_one("#settings", DataTable)
        prior = table.cursor_row
        table.clear()
        for index, setting in enumerate(_SETTINGS, start=1):
            number = Text(str(index), style=COLORS["label"])
            label = Text(setting["label"], style=COLORS["text"])
            table.add_row(number, label, self._value_cell(setting))
        target = min(prior, len(_SETTINGS) - 1) if prior is not None else 0
        table.move_cursor(row=max(target, 0))
        self._update_dirty_title()

    def _update_dirty_title(self) -> None:
        panel = self.query_one(Panel)
        title = "settings *" if self._pending else "settings"
        panel.border_title = Content(spaced_title(title))

    # -- navigation ------------------------------------------------------ #
    def action_cursor_down(self) -> None:
        self.query_one("#settings", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#settings", DataTable).action_cursor_up()

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key.isdigit() and event.key != "0":
            number = int(event.key)
            if 1 <= number <= len(_SETTINGS):
                event.stop()
                self.query_one("#settings", DataTable).move_cursor(row=number - 1)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._edit_selected()

    def _selected_setting(self) -> Optional[dict]:
        index = self.query_one("#settings", DataTable).cursor_row
        if index is None or not (0 <= index < len(_SETTINGS)):
            return None
        return _SETTINGS[index]

    # -- editing --------------------------------------------------------- #
    def _edit_selected(self) -> None:
        setting = self._selected_setting()
        if setting is None:
            return
        kind = setting["type"]
        if kind == "toggle":
            self._edit_toggle(setting)
        elif kind == "numeric":
            self._edit_numeric(setting)
        elif kind == "path":
            self._edit_path(setting)

    def _edit_toggle(self, setting: dict) -> None:
        key = setting["key"]
        next_value = not self._current_value(key)
        if key == "can_scan_on_refresh" and next_value:
            # Experimental: enabling stops Klipper for CAN checks. Require an
            # explicit confirmation (default declines) before flipping it on.
            def _after(confirmed: Optional[bool]) -> None:
                if confirmed:
                    self._apply(key, True)

            self.app.push_screen(
                DecisionConfirmDialog(
                    "Enabling CAN bus scan on refresh stops Klipper for device "
                    "checks and restarts it. This can be unstable. Enable it?",
                    default=False,
                    title="experimental",
                ),
                _after,
            )
            return
        self._apply(key, next_value)

    def _edit_numeric(self, setting: dict) -> None:
        key = setting["key"]
        current = self._current_value(key)

        def _after(raw: Optional[str]) -> None:
            if raw is None or not raw.strip():
                return
            ok, value, err = validate_numeric_setting(
                raw.strip(), setting["min"], setting["max"]
            )
            if ok:
                self._apply(key, value)
            else:
                self._set_status(err, "error")

        self.app.push_screen(
            TextPromptDialog(setting["label"], default=str(current), title="edit"),
            _after,
        )

    def _edit_path(self, setting: dict) -> None:
        key = setting["key"]
        current = self._current_value(key)

        def _after(raw: Optional[str]) -> None:
            if raw is None or not raw.strip():
                return
            ok, err = validate_path_setting(raw.strip(), key)
            if ok:
                self._apply(key, raw.strip())
            else:
                self._set_status(err, "error")

        self.app.push_screen(
            TextPromptDialog(setting["label"], default=str(current), title="edit"),
            _after,
        )

    def _apply(self, key: str, value: Any) -> None:
        # Record the edit only if it actually differs from the saved value.
        if value == getattr(self._config, key):
            self._pending.pop(key, None)
        else:
            self._pending[key] = value
        self._populate()
        self._set_status(f"Edited {key} (unsaved).", "info")

    # -- save / cancel --------------------------------------------------- #
    def action_save(self) -> None:
        if not self._pending:
            self._set_status("No changes to save.", "info")
            return
        new_config = dataclasses.replace(self._config, **self._pending)
        self.kflash_app.registry.save_global(new_config)
        self._config = new_config
        self._pending.clear()
        self._populate()
        self._set_status("Settings saved.", "success")
        dashboard = self.kflash_app._dashboard
        if dashboard is not None:
            dashboard.refresh_devices("Settings saved.", "success")

    def action_cancel(self) -> None:
        if self is self.app.screen:
            self.app.pop_screen()

    # -- helpers --------------------------------------------------------- #
    def _set_status(self, message: str, level: str) -> None:
        role = _LEVEL_ROLE.get(level, "text")
        self.query_one("#settings-status", Static).update(
            Text(message, style=COLORS[role])
        )
