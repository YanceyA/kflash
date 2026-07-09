"""Pilot tests for the add-device wizard (kflash.ui.screens.add_device).

The wizard drives the real ``cmd_add_device`` through the engine bridge. Two
levels of coverage:

* **Stubbed** -- ``add_device.cmd_add_device`` is replaced with a fake that
  emits a couple of events, asks one prompt, and asserts the menuconfig confirm
  is auto-declined; proves the screen wiring (bridge + styled modals + completion
  routing + return-to-dashboard refresh).
* **Real** -- the genuine ``cmd_add_device`` runs against a tmp registry with the
  discovery seams monkeypatched, driven modal-by-modal to a saved device.

The host app replicates :class:`~kflash.ui.app.KflashApp`'s completion routing
(active-job-screen -> dashboard) so the flow is faithful without booting the
dashboard's engine reads.
"""

from __future__ import annotations

import asyncio
import json
import time

from textual.app import App
from textual.css.query import NoMatches
from textual.widgets import Input, RichLog

import kflash.commands.device_add as dadd
import kflash.ui.screens.add_device as addmod
from kflash.decisions import ConfirmDecision, TextPromptDecision
from kflash.models import DiscoveredDevice
from kflash.registry import Registry
from kflash.ui import skin
from kflash.ui.dialogs import (
    ChoiceDialog,
    DecisionConfirmDialog,
    FlashMethodDialog,
    TextPromptDialog,
)
from kflash.ui.screens.add_device import AddDeviceScreen

_SIZE = (80, 32)


class _DashStub:
    def __init__(self) -> None:
        self.refreshed: list = []

    def refresh_devices(self, message: str, level: str) -> None:
        self.refreshed.append((message, level))

    def handle_job_completed(self, message) -> None:  # type: ignore[no-untyped-def]
        pass


class AddHost(App[None]):
    """Minimal host mirroring KflashApp's job-completion routing."""

    CSS_PATH = [skin.CSS_PATH]
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, registry: Registry, screen: AddDeviceScreen) -> None:
        super().__init__()
        self.registry = registry
        self._dashboard = _DashStub()
        self._active_job_screen = None
        self.bridge = None
        self._to_push = screen
        self.register_theme(skin.KFLASH_THEME)
        self.theme = skin.KFLASH_THEME_NAME
        self.animation_level = "none"

    def on_mount(self) -> None:
        self.push_screen(self._to_push)

    def on_engine_job_completed(self, message) -> None:  # type: ignore[no-untyped-def]
        target = self._active_job_screen or self._dashboard
        target.handle_job_completed(message)


def _run(coro) -> None:
    asyncio.run(coro)


async def _wait(pilot, predicate, what: str = "") -> None:
    deadline = time.monotonic() + 8.0
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await pilot.pause()


def _log_text(screen: AddDeviceScreen) -> str:
    log = screen.query_one("#add-log", RichLog)
    return "\n".join(seg.text for line in log.lines for seg in line)


# --------------------------------------------------------------------------- #
# Stubbed happy path
# --------------------------------------------------------------------------- #
def test_stubbed_wizard_happy_path(tmp_path, monkeypatch) -> None:
    path = tmp_path / "devices.json"
    path.write_text(json.dumps({"global": {}, "devices": {}, "blocked_devices": []}))
    registry = Registry(str(path))
    captured: dict = {}

    def fake_add(reg, em, decider, selected_device=None, can_only=False):
        captured["selected"] = selected_device
        captured["can_only"] = can_only
        em.info("Discovery", "Scanning for USB serial devices...")
        name = decider.prompt_text(
            TextPromptDecision(message="Display name (e.g., 'Octopus Pro v1.1')")
        )
        captured["name"] = name
        # The wizard must auto-decline this (menuconfig can't run under Textual).
        captured["menuconfig"] = decider.confirm(
            ConfirmDecision(
                id="run_menuconfig_now", message="Run menuconfig now?", default=True
            )
        )
        em.success(f"Device '{name}' added successfully.")
        return 0

    monkeypatch.setattr(addmod, "cmd_add_device", fake_add)

    async def go() -> None:
        screen = AddDeviceScreen()
        app = AddHost(registry, screen)
        async with app.run_test(size=_SIZE) as pilot:
            await _wait(pilot, lambda: isinstance(app.screen, TextPromptDialog), "prompt")
            app.screen.query_one("#prompt-input", Input).value = "Test Board"
            await pilot.press("enter")
            await _wait(pilot, lambda: screen._done, "completion")
            assert captured["name"] == "Test Board"
            assert captured["menuconfig"] is False  # auto-declined, no modal shown
            assert captured["selected"] is None  # fresh-scan path
            assert "added successfully" in _log_text(screen)
            # Enter returns to the dashboard and refreshes it.
            await pilot.press("enter")
            await pilot.pause()
            assert app._dashboard.refreshed
            assert app._dashboard.refreshed[-1][1] == "success"

    _run(go())


def test_stubbed_wizard_usb_pick_passes_selected_device(tmp_path, monkeypatch) -> None:
    path = tmp_path / "devices.json"
    path.write_text(json.dumps({"global": {}, "devices": {}, "blocked_devices": []}))
    registry = Registry(str(path))
    captured: dict = {}

    def fake_add(reg, em, decider, selected_device=None, can_only=False):
        captured["selected"] = selected_device
        em.success("Device 'Picked' added successfully.")
        return 0

    monkeypatch.setattr(addmod, "cmd_add_device", fake_add)
    device = DiscoveredDevice(
        path="/dev/serial/by-id/usb-Klipper_rp2040_NEW01-if00",
        filename="usb-Klipper_rp2040_NEW01-if00",
    )

    async def go() -> None:
        screen = AddDeviceScreen(selected_device=device)
        app = AddHost(registry, screen)
        async with app.run_test(size=_SIZE) as pilot:
            await _wait(pilot, lambda: screen._done, "completion")
            assert captured["selected"] is device

    _run(go())


# --------------------------------------------------------------------------- #
# Real cmd_add_device against a tmp registry
# --------------------------------------------------------------------------- #
def test_real_cmd_add_device_registers_usb(tmp_path, monkeypatch) -> None:
    # Registry seeded with a CAN device so it is non-empty (skips the first-run
    # global-config prompt) and has no serial pattern to overlap the USB add.
    path = tmp_path / "devices.json"
    path.write_text(
        json.dumps(
            {
                "global": {"klipper_dir": "~/klipper", "katapult_dir": "~/katapult"},
                "devices": {
                    "toolhead": {
                        "name": "Toolhead",
                        "mcu": "stm32g0",
                        "canbus_uuid": "aabbccddeeff",
                        "canbus_interface": "can0",
                        "flash_command": "katapult_can",
                        "bootloader_method": "can",
                    }
                },
                "blocked_devices": [],
            }
        )
    )
    registry = Registry(str(path))

    selected = DiscoveredDevice(
        path="/dev/serial/by-id/usb-Klipper_rp2040_NEW01-if00",
        filename="usb-Klipper_rp2040_NEW01-if00",
    )

    # cmd_add_device requires an interactive tty; under a live Textual app over
    # SSH that holds, but pytest's stdin is not a tty, so fake it.
    class _Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(dadd.sys, "stdin", _Tty())
    # Monkeypatch the discovery seams inside the real command's module.
    monkeypatch.setattr(dadd, "scan_serial_devices", lambda: [selected])
    monkeypatch.setattr(dadd, "match_devices", lambda pattern, devices: [selected])
    monkeypatch.setattr(dadd, "extract_mcu_from_serial", lambda name: "rp2040")
    monkeypatch.setattr(
        dadd, "generate_serial_pattern", lambda name: "usb-Klipper_rp2040_NEW01*"
    )
    monkeypatch.setattr(dadd, "get_mcu_serial_map", lambda: None)
    monkeypatch.setattr(dadd, "prefix_variants", lambda pattern: [pattern])

    async def answer_modals(pilot, app, screen) -> None:
        # Drive whatever modal is up until the wizard completes. The poll can
        # observe a modal after push_screen but before its children mount, so
        # queries may raise NoMatches for a tick -- retry until the deadline.
        deadline = time.monotonic() + 8.0
        while not screen._done:
            if time.monotonic() > deadline:
                raise AssertionError(f"stuck; last log:\n{_log_text(screen)}")
            current = app.screen
            try:
                if isinstance(current, TextPromptDialog):
                    if "Display name" in current._message:
                        current.query_one("#prompt-input", Input).value = "Test Board"
                    await pilot.press("enter")
                elif isinstance(current, (FlashMethodDialog, ChoiceDialog)):
                    await pilot.press("enter")
                elif isinstance(current, DecisionConfirmDialog):
                    await pilot.press("enter")  # accept each default
            except NoMatches:
                pass  # modal not fully mounted yet; poll again
            await pilot.pause()

    async def go() -> None:
        screen = AddDeviceScreen(selected_device=selected)
        app = AddHost(registry, screen)
        async with app.run_test(size=_SIZE) as pilot:
            await answer_modals(pilot, app, screen)

    _run(go())

    data = registry.load()
    names = {e.name: e for e in data.devices.values()}
    assert "Test Board" in names
    added = names["Test Board"]
    assert added.mcu == "rp2040"
    # rp2040 first method is picoboot: none + make_flash.
    assert added.bootloader_method == "none"
    assert added.flash_command == "make_flash"
    assert added.serial_pattern == "usb-Klipper_rp2040_NEW01*"
