"""Pilot + snapshot tests for the per-device config editor.

A real :class:`~kflash.registry.Registry` over a tmp JSON file backs each test;
edits round-trip through the styled R4 modals and are asserted on disk after a
save. ``get_mcu_serial_map`` (the only engine read the editor makes, on the MCU
picker) is stubbed so no Moonraker call happens.
"""

from __future__ import annotations

import asyncio
import json
import time

from textual.app import App
from textual.widgets import DataTable, Input

import kflash.ui.screens.device_config as devcfg
from kflash.config import ConfigManager, get_defaults_dir
from kflash.registry import Registry
from kflash.ui import skin
from kflash.ui.dialogs import (
    ChoiceDialog,
    DecisionConfirmDialog,
    FlashMethodDialog,
    TextPromptDialog,
)
from kflash.ui.engine_bridge import EngineJobCompleted
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
        self._active_job_screen = None
        self.bridge = None
        self._device_key = device_key
        self.register_theme(skin.KFLASH_THEME)
        self.theme = skin.KFLASH_THEME_NAME
        self.animation_level = "none"

    def on_mount(self) -> None:
        self.push_screen(DeviceConfigScreen(self._device_key))

    def on_engine_job_completed(self, message: EngineJobCompleted) -> None:
        # Mirror KflashApp: route completion to the active job screen (the
        # device-config editor while its bridge job runs) else the dashboard.
        target = self._active_job_screen or self._dashboard
        if target is not None:
            target.handle_job_completed(message)


def _run(coro) -> None:
    asyncio.run(coro)


def _cell_text(screen, row: int) -> str:
    table = screen.query_one("#device-fields", DataTable)
    return str(table.get_row_at(row)[2])


def _identity_text(screen) -> str:
    return str(screen.query_one("#device-identity").content)


def _write_cache(key: str, content: str = 'CONFIG_MCU="stm32h723xx"\n') -> ConfigManager:
    """Write a device's cached ``.config`` directly (bypassing menuconfig)."""
    mgr = ConfigManager(key, "~/klipper")
    mgr.cache_path.parent.mkdir(parents=True, exist_ok=True)
    mgr.cache_path.write_text(content, encoding="utf-8")
    return mgr


async def _wait(pilot, predicate, what: str = "") -> None:
    deadline = time.monotonic() + 8.0
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await pilot.pause()


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


# --------------------------------------------------------------------------- #
# Save config as default (D) + copy config from another device (C)
# --------------------------------------------------------------------------- #
_PICKER_REGISTRY = {
    "global": {"klipper_dir": "~/klipper", "katapult_dir": "~/katapult"},
    "devices": {
        "octopus": {
            "name": "Octopus Pro",
            "mcu": "stm32h723",
            "serial_pattern": "usb-Klipper_stm32h723xx_ABC*",
            "flash_command": "katapult",
            "bootloader_method": "usb",
            "flashable": True,
        },
        "manta": {
            "name": "Manta M8P",
            "mcu": "stm32h723",
            "serial_pattern": "usb-Klipper_stm32h723xx_MMM*",
            "flash_command": "katapult",
            "bootloader_method": "usb",
            "flashable": True,
        },
        "spider": {
            "name": "Spider",
            "mcu": "stm32f446",
            "serial_pattern": "usb-Klipper_stm32f446xx_XYZ*",
            "flash_command": "make_flash",
            "bootloader_method": "usb",
            "flashable": True,
        },
        "nitehawk": {
            "name": "Nitehawk",
            "mcu": "rp2040",
            "serial_pattern": "usb-Klipper_rp2040_NHK*",
            "flash_command": "katapult",
            "bootloader_method": "usb",
            "flashable": True,
        },
    },
    "blocked_devices": [],
}


def _picker_registry(tmp_path) -> Registry:
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(_PICKER_REGISTRY), encoding="utf-8")
    return Registry(str(path))


def test_copy_picker_filters_and_orders(tmp_path, monkeypatch) -> None:
    """C's picker lists only OTHER devices with a cache, same-MCU first + labeled."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    registry = _picker_registry(tmp_path)
    # manta (same MCU as octopus) and spider (different MCU) both have a cache;
    # nitehawk has none (excluded); octopus is the edited device (excluded).
    _write_cache("manta")
    _write_cache("spider")

    async def go() -> None:
        app = ConfigHost(registry, device_key="octopus")
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ChoiceDialog)
            options = app.screen._options
            keys = [value for value, _label in options]
            # Only manta + spider (with caches); self + nitehawk excluded.
            assert keys == ["manta", "spider"]
            # Same-MCU device is first and labelled.
            same_label = options[0][1]
            assert "(same MCU)" in same_label
            assert "(same MCU)" not in options[1][1]

    _run(go())


def test_save_default_warns_without_cache(tmp_path, monkeypatch) -> None:
    """D with no cached config warns and never launches an engine job."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    registry = _registry(tmp_path)

    async def go() -> None:
        app = ConfigHost(registry, device_key="octopus")
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            await pilot.press("d")
            await pilot.pause()
            status = str(screen.query_one("#device-status").content)
            assert "no cached config" in status.lower()
            # No default was written and no job started.
            assert not (get_defaults_dir() / "stm32h723.config").exists()

    _run(go())


def test_save_default_happy_path(tmp_path, monkeypatch) -> None:
    """D with a cached config runs the engine command and writes the default."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    registry = _registry(tmp_path)
    _write_cache("octopus", 'CONFIG_MCU="stm32h723xx"\n')

    async def go() -> None:
        app = ConfigHost(registry, device_key="octopus")
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.press("d")
            dst = get_defaults_dir() / "stm32h723.config"
            await _wait(pilot, dst.exists, "default config written")
            assert dst.read_text(encoding="utf-8") == 'CONFIG_MCU="stm32h723xx"\n'

    _run(go())
    dst = get_defaults_dir() / "stm32h723.config"
    assert dst.exists()


def test_save_default_overwrite_confirm_surfaces_modal(tmp_path, monkeypatch) -> None:
    """D over an existing MCU default asks via DecisionConfirmDialog; Y replaces it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    registry = _registry(tmp_path)
    _write_cache("octopus", "# new content\n")
    defaults_dir = get_defaults_dir()
    defaults_dir.mkdir(parents=True, exist_ok=True)
    dst = defaults_dir / "stm32h723.config"
    dst.write_text("# old default\n", encoding="utf-8")

    async def go() -> None:
        app = ConfigHost(registry, device_key="octopus")
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.press("d")
            # The engine's overwrite_mcu_default ConfirmDecision surfaces as the
            # styled confirm modal (via styled_modal_factory on the bridge).
            await _wait(
                pilot,
                lambda: isinstance(app.screen, DecisionConfirmDialog),
                "overwrite confirm modal",
            )
            await pilot.press("y")
            await _wait(
                pilot,
                lambda: dst.read_text(encoding="utf-8") == "# new content\n",
                "default replaced after confirm",
            )

    _run(go())
    assert dst.read_text(encoding="utf-8") == "# new content\n"


def test_escape_mid_job_refuses_to_close(tmp_path, monkeypatch) -> None:
    """Esc/Q while a D job runs must not pop the screen (regression: NoMatches
    crash when the completion landed on an unmounted screen)."""
    import threading

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    registry = _registry(tmp_path)
    _write_cache("octopus")

    release = threading.Event()
    real_cmd = devcfg.cmd_save_config_as_default

    def slow_cmd(reg, key, em, dec):
        # Hold the worker until the test has pressed Escape mid-flight.
        release.wait(timeout=8.0)
        return real_cmd(reg, key, em, dec)

    monkeypatch.setattr(devcfg, "cmd_save_config_as_default", slow_cmd)

    async def go() -> None:
        app = ConfigHost(registry, device_key="octopus")
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DeviceConfigScreen)
            await pilot.press("d")
            await _wait(pilot, screen._bridge_busy, "job started")
            # Escape (and Q) while the job runs: the screen must stay put.
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is screen
            status = str(screen.query_one("#device-status").content)
            assert "wait" in status.lower()
            await pilot.press("q")
            await pilot.pause()
            assert app.screen is screen
            # Let the job finish; completion must land cleanly on the screen.
            release.set()
            dst = get_defaults_dir() / "stm32h723.config"
            await _wait(pilot, dst.exists, "default written after release")
            await _wait(
                pilot, lambda: not screen._bridge_busy(), "job thread finished"
            )
            await pilot.pause()
            # Now Escape closes normally.
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not screen

    _run(go())


def test_copy_config_marks_seeded_and_shows_state(tmp_path, monkeypatch) -> None:
    """Selecting a source device copies its cache, marks dst seeded, shows state."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    registry = _picker_registry(tmp_path)
    _write_cache("manta", "# manta config\n")  # source has a cache; octopus does not

    async def go() -> None:
        app = ConfigHost(registry, device_key="octopus")
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ChoiceDialog)
            # manta is the only candidate; select it.
            await pilot.press("enter")
            octo = ConfigManager("octopus", "~/klipper")
            await _wait(pilot, octo.is_seeded, "octopus cache seeded")
            # Screen reflects the new seeded state and forced review.
            assert octo.seed_source() == "device:manta"
            assert octo.cache_path.read_text(encoding="utf-8") == "# manta config\n"
            await _wait(
                pilot, lambda: "seeded" in _identity_text(screen), "seeded state shown"
            )

    _run(go())


def _write_board_profile(key: str, name: str) -> None:
    """Drop a user board profile JSON under the (XDG-isolated) boards dir."""
    from kflash.boards import get_user_boards_dir

    boards_dir = get_user_boards_dir()
    boards_dir.mkdir(parents=True, exist_ok=True)
    (boards_dir / f"{key}.json").write_text(
        json.dumps(
            {
                "key": key,
                "name": name,
                "mcu": "stm32h723",
                "bootloader_method": "usb",
                "flash_command": "katapult",
            }
        ),
        encoding="utf-8",
    )


def _registry_with_board(tmp_path, board_key: str) -> Registry:
    reg = json.loads(json.dumps(_REGISTRY))
    reg["devices"]["octopus"]["board"] = board_key
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(reg), encoding="utf-8")
    return Registry(str(path))


def test_identity_shows_board_profile_when_set(tmp_path, monkeypatch) -> None:
    """The identity panel surfaces the board profile's display name when the
    device has a ``board`` key; it is absent when the device has none (Task 13)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    _write_board_profile("btt-x", "BTT Octopus Pro (H723)")
    registry = _registry_with_board(tmp_path, "btt-x")

    async def go() -> None:
        app = ConfigHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            identity = _identity_text(app.screen)
            assert "Board:" in identity
            assert "BTT Octopus Pro (H723)" in identity

    _run(go())

    # A device without a board profile shows no Board line.
    plain = _registry(tmp_path)

    async def go_plain() -> None:
        app = ConfigHost(plain)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            assert "Board:" not in _identity_text(app.screen)

    _run(go_plain())


def test_board_does_not_constrain_method_editing(tmp_path, monkeypatch) -> None:
    """``board`` is informational after add: changing the flash method away from
    the profile's bootloader/flash pair still works and never clears ``board``."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    _write_board_profile("btt-x", "BTT Octopus Pro (H723)")
    registry = _registry_with_board(tmp_path, "btt-x")

    async def go() -> None:
        app = ConfigHost(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            screen = app.screen
            # The seeded profile pair is usb + katapult ("Katapult USB"); change
            # the flash method to Make Flash USB (option 2) -- away from the pair.
            await pilot.press("3")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, FlashMethodDialog)
            await pilot.press("2")
            await pilot.press("enter")
            await pilot.pause()
            assert screen._pending.get("flash_command") == "make_flash"
            await pilot.press("s")
            await pilot.pause()
            assert not screen._pending

    _run(go())

    # The method changed and the board key was preserved untouched.
    entry = registry.get("octopus")
    assert entry.flash_command == "make_flash"
    assert entry.board == "btt-x"


def test_device_config_snapshot(tmp_path, monkeypatch, snap_compare) -> None:
    # Isolate config-state lookups to an empty XDG dir so the identity panel's
    # "Config:" line is deterministically "no cached config" on any machine.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(devcfg, "get_mcu_serial_map", lambda: None)
    # The screen renders "Last Flashed: <ts> (N days ago)" relative to
    # datetime.now() (see devcfg._format_last_flash), so the "(N days ago)" part
    # drifts every day and the snapshot only matches on the date it was
    # generated. Pin the formatted string so the snapshot is deterministic on
    # any run date; the frozen value matches the committed baseline exactly.
    monkeypatch.setattr(
        devcfg, "_format_last_flash", lambda _iso: "2026-02-06 14:30 (152 days ago)"
    )
    registry = _registry(tmp_path)

    async def _settle(pilot) -> None:
        await pilot.pause()
        await pilot.pause()

    assert snap_compare(ConfigHost(registry), terminal_size=_SIZE, run_before=_settle)
