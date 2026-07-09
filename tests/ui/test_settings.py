"""Pilot + snapshot tests for the global settings editor (kflash.ui.screens.settings).

A real :class:`~kflash.registry.Registry` over a tmp JSON file backs each test;
edits round-trip through the styled R4 modals and are asserted on disk after a
save. No engine reads are involved (settings only touch the registry).
"""

from __future__ import annotations

import asyncio
import json

from textual.app import App
from textual.widgets import DataTable, Input

from kflash.registry import Registry
from kflash.ui import skin
from kflash.ui.dialogs import DecisionConfirmDialog, TextPromptDialog
from kflash.ui.screens.settings import SettingsScreen

_SIZE = (80, 32)

_REGISTRY = {
    "global": {
        "klipper_dir": "~/klipper",
        "katapult_dir": "~/katapult",
        "menuconfig_before_flash": True,
        "stagger_delay": 2.0,
        "use_ccache": False,
    },
    "devices": {},
    "blocked_devices": [],
}


def _registry(tmp_path) -> Registry:
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(_REGISTRY), encoding="utf-8")
    return Registry(str(path))


class SettingsHost(App[None]):
    """Minimal themed host: provides ``registry`` + ``_dashboard`` and the screen."""

    CSS_PATH = [skin.CSS_PATH]
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, registry: Registry) -> None:
        super().__init__()
        self.registry = registry
        self._dashboard = None
        self.register_theme(skin.KFLASH_THEME)
        self.theme = skin.KFLASH_THEME_NAME
        self.animation_level = "none"

    def on_mount(self) -> None:
        self.push_screen(SettingsScreen())


def _run(coro) -> None:
    asyncio.run(coro)


def test_boot_lists_all_settings(tmp_path) -> None:
    registry = _registry(tmp_path)

    async def go() -> None:
        app = SettingsHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#settings", DataTable)
            assert table.row_count == 8

    _run(go())


def test_toggle_edit_and_save(tmp_path) -> None:
    registry = _registry(tmp_path)

    async def go() -> None:
        app = SettingsHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            # Row 1 = menuconfig_before_flash (toggle, default On). Jump +
            # Enter flips it immediately.
            await pilot.press("1")
            await pilot.press("enter")
            assert screen._pending.get("menuconfig_before_flash") is False
            # Save the batch.
            await pilot.press("s")
            await pilot.pause()
            assert not screen._pending

    _run(go())
    assert registry.load_global().menuconfig_before_flash is False


def test_numeric_edit_valid(tmp_path) -> None:
    registry = _registry(tmp_path)

    async def go() -> None:
        app = SettingsHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            # Row 3 = stagger_delay (numeric, 0..30).
            await pilot.press("3")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, TextPromptDialog)
            app.screen.query_one("#prompt-input", Input).value = "10"
            await pilot.press("enter")
            await pilot.pause()
            assert screen._pending.get("stagger_delay") == 10.0
            await pilot.press("s")
            await pilot.pause()

    _run(go())
    assert registry.load_global().stagger_delay == 10.0


def test_numeric_edit_invalid_rejected(tmp_path) -> None:
    registry = _registry(tmp_path)

    async def go() -> None:
        app = SettingsHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            await pilot.press("3")
            await pilot.press("enter")
            await pilot.pause()
            # 999 exceeds the max (30): rejected, nothing pending.
            app.screen.query_one("#prompt-input", Input).value = "999"
            await pilot.press("enter")
            await pilot.pause()
            assert "stagger_delay" not in screen._pending

    _run(go())
    assert registry.load_global().stagger_delay == 2.0


def test_cancel_discards_edits(tmp_path) -> None:
    registry = _registry(tmp_path)

    async def go() -> None:
        app = SettingsHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            await pilot.press("1")
            await pilot.press("enter")
            assert screen._pending
            # Escape leaves without saving.
            await pilot.press("escape")
            await pilot.pause()

    _run(go())
    # Nothing was written: the toggle stays at its original True (default On).
    assert registry.load_global().menuconfig_before_flash is True


def test_experimental_toggle_requires_confirm(tmp_path) -> None:
    registry = _registry(tmp_path)

    async def go() -> None:
        app = SettingsHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            # Row 6 = can_scan_on_refresh (experimental toggle).
            await pilot.press("6")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, DecisionConfirmDialog)
            # Explicit yes enables it (default is decline).
            await pilot.press("y")
            await pilot.pause()
            assert screen._pending.get("can_scan_on_refresh") is True

    _run(go())


def test_experimental_toggle_declined_by_default(tmp_path) -> None:
    registry = _registry(tmp_path)

    async def go() -> None:
        app = SettingsHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            await pilot.press("6")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, DecisionConfirmDialog)
            # Enter == the default (False) -> the toggle is NOT enabled.
            await pilot.press("enter")
            await pilot.pause()
            assert "can_scan_on_refresh" not in screen._pending

    _run(go())


def test_settings_snapshot(tmp_path, snap_compare) -> None:
    registry = _registry(tmp_path)

    async def _settle(pilot) -> None:
        await pilot.pause()
        await pilot.pause()

    assert snap_compare(SettingsHost(registry), terminal_size=_SIZE, run_before=_settle)
