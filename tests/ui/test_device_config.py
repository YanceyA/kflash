"""Pilot + snapshot tests for the per-device config editor.

A real :class:`~kflash.registry.Registry` over a tmp JSON file backs each test;
edits round-trip through the styled R4 modals and are asserted on disk after a
save. ``get_mcu_serial_map`` (the only engine read the editor makes, on the MCU
picker) is stubbed so no Moonraker call happens.
"""

from __future__ import annotations

import asyncio
import json

from textual.app import App
from textual.widgets import DataTable, Input

import kflash.ui.screens.device_config as devcfg
from kflash.registry import Registry
from kflash.ui import skin
from kflash.ui.dialogs import DecisionConfirmDialog, FlashMethodDialog, TextPromptDialog
from kflash.ui.screens.device_config import DeviceConfigScreen

_SIZE = (80, 32)

_REGISTRY = {
    "global": {"klipper_dir": "~/klipper", "katapult_dir": "~/katapult"},
    "devices": {
        "octopus": {
            "name": "Octopus Pro",
            "mcu": "stm32h723",
            "serial_pattern": "usb-Klipper_stm32h723xx_ABC*",
            "flash_command": "katapult",
            "bootloader_method": "usb",
            "mcu_name": "mcu",
            "flashable": True,
            "last_flash_timestamp": "2026-02-06T14:30:00",
        },
        "spider": {
            "name": "Spider",
            "mcu": "stm32f446",
            "serial_pattern": "usb-Klipper_stm32f446xx_XYZ*",
            "flash_command": "make_flash",
            "bootloader_method": "usb",
            "flashable": False,
        },
    },
    "blocked_devices": [],
}


def _registry(tmp_path) -> Registry:
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(_REGISTRY), encoding="utf-8")
    return Registry(str(path))


class ConfigHost(App[None]):
    """Minimal themed host: provides ``registry`` + ``_dashboard`` and the screen."""

    CSS_PATH = [skin.CSS_PATH]
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, registry: Registry, device_key: str = "octopus") -> None:
        super().__init__()
        self.registry = registry
        self._dashboard = None
        self._device_key = device_key
        self.register_theme(skin.KFLASH_THEME)
        self.theme = skin.KFLASH_THEME_NAME
        self.animation_level = "none"

    def on_mount(self) -> None:
        self.push_screen(DeviceConfigScreen(self._device_key))


def _run(coro) -> None:
    asyncio.run(coro)


def _cell_text(screen, row: int) -> str:
    table = screen.query_one("#device-fields", DataTable)
    return str(table.get_row_at(row)[2])


def test_boot_shows_field_values(tmp_path, monkeypatch) -> None:
    """E opens with the device's current values populated from the registry."""
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    registry = _registry(tmp_path)

    async def go() -> None:
        app = ConfigHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            table = screen.query_one("#device-fields", DataTable)
            assert table.row_count == 11
            # Name row (0) and flash-method row (2) reflect the registry.
            assert "Octopus Pro" in _cell_text(screen, 0)
            assert "Katapult USB" in _cell_text(screen, 2)
            # Identity panel shows MCU type + last-flash.
            identity = str(screen.query_one("#device-identity").content)
            assert "stm32h723" in identity
            assert "2026-02-06" in identity

    _run(go())


def test_edit_name_and_method_and_save_persists(tmp_path, monkeypatch) -> None:
    """Edit name + flash method, save, and assert the registry JSON changed."""
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    registry = _registry(tmp_path)

    async def go() -> None:
        app = ConfigHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            # Edit the display name (field 1).
            await pilot.press("1")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, TextPromptDialog)
            app.screen.query_one("#prompt-input", Input).value = "Renamed Board"
            await pilot.press("enter")
            await pilot.pause()
            assert screen._pending.get("name") == "Renamed Board"
            # Edit the flash method (field 3): pick option 2 (Make Flash USB).
            await pilot.press("3")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, FlashMethodDialog)
            await pilot.press("2")
            await pilot.press("enter")
            await pilot.pause()
            assert screen._pending.get("flash_command") == "make_flash"
            # Save the batch.
            await pilot.press("s")
            await pilot.pause()
            assert not screen._pending

    _run(go())

    # Persisted: name + flash_command changed, key ("octopus") unchanged.
    saved = json.loads((tmp_path / "devices.json").read_text())
    assert "octopus" in saved["devices"]
    assert "renamed-board" not in saved["devices"]  # key never regenerated
    assert saved["devices"]["octopus"]["name"] == "Renamed Board"
    assert saved["devices"]["octopus"]["flash_command"] == "make_flash"


def test_toggle_flashable(tmp_path, monkeypatch) -> None:
    """The Include-in-flash toggle flips immediately and saves."""
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    registry = _registry(tmp_path)

    async def go() -> None:
        app = ConfigHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            await pilot.press("4")  # Include in flash (toggle)
            await pilot.press("enter")
            await pilot.pause()
            assert screen._pending.get("flashable") is False
            await pilot.press("s")
            await pilot.pause()

    _run(go())
    assert registry.get("octopus").flashable is False


def test_escape_with_dirty_asks(tmp_path, monkeypatch) -> None:
    """Escaping with unsaved edits prompts to discard rather than dropping them."""
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    registry = _registry(tmp_path)

    async def go() -> None:
        app = ConfigHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            # Make one edit (flip the flashable toggle).
            await pilot.press("4")
            await pilot.press("enter")
            await pilot.pause()
            assert screen._pending
            # Escape -> discard confirmation appears.
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DecisionConfirmDialog)

    _run(go())
    # Nothing was written: the toggle stays at its original True.
    assert registry.get("octopus").flashable is True


def test_escape_clean_returns(tmp_path, monkeypatch) -> None:
    """Escaping with no edits pops the screen without a discard prompt."""
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    registry = _registry(tmp_path)

    async def go() -> None:
        app = ConfigHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, DeviceConfigScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, DeviceConfigScreen)

    _run(go())


def test_device_config_snapshot(tmp_path, monkeypatch, snap_compare) -> None:
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    registry = _registry(tmp_path)

    async def _settle(pilot) -> None:
        await pilot.pause()
        await pilot.pause()

    assert snap_compare(ConfigHost(registry), terminal_size=_SIZE, run_before=_settle)
