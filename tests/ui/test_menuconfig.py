"""Tests for the shared menuconfig helper (kflash.ui.menuconfig).

Three layers:

* pure helper units for ``_run_menuconfig_step`` (config-cache snapshot + diff)
  and ``run_menuconfig_suspended``'s Ctrl+C recovery, driving a stubbed
  ``run_menuconfig`` (no real ``make menuconfig`` subprocess, no TTY);
* a Pilot test of the add-device wizard's post-add "Configure firmware now?"
  path;
* an SVG snapshot of the config-diff receipt modal.
"""

from __future__ import annotations

import asyncio
import json

from textual.app import App

from kflash.config import ConfigManager
from kflash.registry import Registry
from kflash.ui import menuconfig, skin
from kflash.ui.menuconfig import (
    ConfigDiffDialog,
    MenuconfigResult,
    _run_menuconfig_step,
    run_menuconfig_suspended,
)

_SIZE = (80, 32)


def run_async(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# _run_menuconfig_step: snapshot cached .config, run menuconfig, diff
# --------------------------------------------------------------------------- #
def _seed_cache(monkeypatch, tmp_path, device_key: str, text: str) -> ConfigManager:
    """Point get_config_dir at tmp and seed a cached .config for *device_key*."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    klipper_dir = tmp_path / "klipper"
    klipper_dir.mkdir()
    mgr = ConfigManager(device_key, str(klipper_dir))
    mgr.cache_path.parent.mkdir(parents=True, exist_ok=True)
    mgr.cache_path.write_text(text, encoding="utf-8")
    return mgr


def test_step_diffs_cached_config_after_menuconfig(monkeypatch, tmp_path) -> None:
    mgr = _seed_cache(
        monkeypatch, tmp_path, "octopus", "CONFIG_A=y\nCONFIG_B=n\n"
    )
    klipper_dir = str(mgr.klipper_dir)

    def fake_run_menuconfig(kdir, config_path):
        # Simulate the user editing + saving in menuconfig: rewrite the klipper
        # .config (which _run_menuconfig_step loaded from the cache first).
        from pathlib import Path

        Path(config_path).write_text("CONFIG_A=y\nCONFIG_C=y\n", encoding="utf-8")
        return 0, True  # (return_code, was_saved)

    monkeypatch.setattr(menuconfig, "run_menuconfig", fake_run_menuconfig)

    result = _run_menuconfig_step("octopus", klipper_dir)

    assert result.ran is True
    assert result.saved is True
    assert result.changed is True
    # -CONFIG_B=n removed, +CONFIG_C=y added -> 2 changed lines.
    assert result.lines_changed == 2
    rendered = "\n".join(seg.plain for seg in result.diff_lines)
    assert "-CONFIG_B=n" in rendered
    assert "+CONFIG_C=y" in rendered
    # The saved config was written back to the per-device cache.
    assert "CONFIG_C=y" in mgr.cache_path.read_text(encoding="utf-8")


def test_step_reports_no_change_when_menuconfig_unsaved(monkeypatch, tmp_path) -> None:
    mgr = _seed_cache(monkeypatch, tmp_path, "octopus", "CONFIG_A=y\n")
    monkeypatch.setattr(
        menuconfig, "run_menuconfig", lambda kdir, cfg: (0, False)
    )
    result = _run_menuconfig_step("octopus", str(mgr.klipper_dir))
    assert result.ran is True
    assert result.saved is False
    assert result.changed is False
    assert result.lines_changed == 0


def test_step_reports_error_on_nonzero_exit(monkeypatch, tmp_path) -> None:
    mgr = _seed_cache(monkeypatch, tmp_path, "octopus", "CONFIG_A=y\n")
    monkeypatch.setattr(
        menuconfig, "run_menuconfig", lambda kdir, cfg: (1, False)
    )
    result = _run_menuconfig_step("octopus", str(mgr.klipper_dir))
    assert result.error is not None
    assert result.changed is False


# --------------------------------------------------------------------------- #
# run_menuconfig_suspended: Ctrl+C recovery (no app exit)
# --------------------------------------------------------------------------- #
class _FakeSuspendApp:
    """App stand-in whose suspend() is unavailable (like the headless driver)."""

    def suspend(self):
        from textual.app import SuspendNotSupported

        raise SuspendNotSupported("no tty in tests")


def test_suspended_catches_keyboardinterrupt(monkeypatch, tmp_path) -> None:
    mgr = _seed_cache(monkeypatch, tmp_path, "octopus", "CONFIG_A=y\n")

    def boom(kdir, cfg):
        raise KeyboardInterrupt

    monkeypatch.setattr(menuconfig, "run_menuconfig", boom)
    result = run_menuconfig_suspended(
        _FakeSuspendApp(), "octopus", str(mgr.klipper_dir)
    )
    assert result.cancelled is True
    assert result.ran is True


def test_suspended_reports_subprocess_launch_failure(monkeypatch, tmp_path) -> None:
    # A missing `make` / bad klipper_dir makes subprocess.run raise; that must be
    # surfaced as an error receipt, never escape into the Textual callback (which
    # would tear the whole app down). run_menuconfig_suspended promises to never
    # raise.
    mgr = _seed_cache(monkeypatch, tmp_path, "octopus", "CONFIG_A=y\n")

    def boom(kdir, cfg):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'make'")

    monkeypatch.setattr(menuconfig, "run_menuconfig", boom)
    result = run_menuconfig_suspended(
        _FakeSuspendApp(), "octopus", str(mgr.klipper_dir)
    )
    assert result.error is not None
    assert result.cancelled is False
    assert result.ran is True


# --------------------------------------------------------------------------- #
# Add-device wizard: post-add "Configure firmware now?" path
# --------------------------------------------------------------------------- #
class _AddHost(App[None]):
    CSS_PATH = [skin.CSS_PATH]
    ENABLE_COMMAND_PALETTE = False

    class _Dash:
        def __init__(self) -> None:
            self.refreshed: list = []

        def refresh_devices(self, message, level) -> None:
            self.refreshed.append((message, level))

    def __init__(self, registry, screen) -> None:
        super().__init__()
        self.registry = registry
        self._screen = screen
        self._dashboard = self._Dash()
        self._active_job_screen = None
        self.register_theme(skin.KFLASH_THEME)
        self.theme = skin.KFLASH_THEME_NAME
        self.animation_level = "none"

    def on_mount(self) -> None:
        self.push_screen(self._screen)

    def on_engine_job_completed(self, message) -> None:  # type: ignore[no-untyped-def]
        target = self._active_job_screen or self._dashboard
        if target is not None:
            target.handle_job_completed(message)


async def _wait(pilot, predicate, what: str = "") -> None:
    import time

    deadline = time.monotonic() + 8.0
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await pilot.pause()


def test_wizard_configure_now_runs_menuconfig_and_shows_receipt(
    tmp_path, monkeypatch
) -> None:
    import kflash.ui.screens.add_device as addmod
    from kflash.ui.dialogs import DecisionConfirmDialog
    from kflash.ui.screens.add_device import AddDeviceScreen

    path = tmp_path / "devices.json"
    path.write_text(
        json.dumps(
            {
                "global": {"klipper_dir": "~/klipper", "katapult_dir": "~/katapult"},
                "devices": {},
                "blocked_devices": [],
            }
        )
    )
    registry = Registry(str(path))

    def fake_add(reg, em, decider, selected_device=None, can_only=False):
        # Register a real device so the wizard detects a new key post-add.
        from kflash.models import DeviceEntry

        reg.add(DeviceEntry(key="newdev", name="New Board", mcu="rp2040"))
        # The worker-side menuconfig prompt is declined (deferred to the UI).
        from kflash.decisions import ConfirmDecision

        assert decider.confirm(
            ConfirmDecision(id="run_menuconfig_now", message="?", default=True)
        ) is False
        em.success("Device 'New Board' added successfully.")
        return 0

    monkeypatch.setattr(addmod, "cmd_add_device", fake_add)

    captured: dict = {}

    def fake_suspended(app, device_key, klipper_dir):
        captured["device_key"] = device_key
        from rich.text import Text

        return MenuconfigResult(
            ran=True,
            saved=True,
            changed=True,
            diff_lines=[Text("+CONFIG_NEW=y")],
            lines_changed=1,
        )

    monkeypatch.setattr(addmod.menuconfig, "run_menuconfig_suspended", fake_suspended)

    async def go() -> None:
        screen = AddDeviceScreen()
        app = _AddHost(registry, screen)
        async with app.run_test(size=_SIZE) as pilot:
            await _wait(pilot, lambda: screen._done, "wizard done")
            # The post-add "Configure firmware now?" offer appears.
            await _wait(
                pilot,
                lambda: isinstance(app.screen, DecisionConfirmDialog),
                "configure offer",
            )
            await pilot.press("y")  # yes, configure now
            # menuconfig ran under (stubbed) suspend for the new device...
            await _wait(
                pilot,
                lambda: isinstance(app.screen, ConfigDiffDialog),
                "diff receipt",
            )
            assert captured["device_key"] == "newdev"
            # ...and the receipt is informational (Close only).
            await pilot.press("enter")
            await pilot.pause()

    run_async(go())


def test_wizard_no_configure_offer_when_nothing_registered(
    tmp_path, monkeypatch
) -> None:
    """A stub that registers nothing -> no new key -> no configure offer."""
    import kflash.ui.screens.add_device as addmod
    from kflash.ui.dialogs import DecisionConfirmDialog
    from kflash.ui.screens.add_device import AddDeviceScreen

    path = tmp_path / "devices.json"
    path.write_text(json.dumps({"global": {}, "devices": {}, "blocked_devices": []}))
    registry = Registry(str(path))

    def fake_add(reg, em, decider, selected_device=None, can_only=False):
        em.success("nothing registered")
        return 0

    monkeypatch.setattr(addmod, "cmd_add_device", fake_add)

    async def go() -> None:
        screen = AddDeviceScreen()
        app = _AddHost(registry, screen)
        async with app.run_test(size=_SIZE) as pilot:
            await _wait(pilot, lambda: screen._done, "wizard done")
            await pilot.pause()
            assert not isinstance(app.screen, DecisionConfirmDialog)

    run_async(go())


# --------------------------------------------------------------------------- #
# Snapshot: config-diff receipt modal
# --------------------------------------------------------------------------- #
class _ModalHost(App[None]):
    CSS_PATH = [skin.CSS_PATH]
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, modal) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self._modal = modal
        self.register_theme(skin.KFLASH_THEME)
        self.theme = skin.KFLASH_THEME_NAME
        self.animation_level = "none"

    def on_mount(self) -> None:
        self.push_screen(self._modal)


def _sample_diff_result() -> MenuconfigResult:
    from rich.text import Text

    from kflash.ui.menuconfig import _render_diff

    before = ["CONFIG_MCU=stm32f103\n", "CONFIG_CLOCK=8000000\n", "CONFIG_USB=y\n"]
    after = ["CONFIG_MCU=stm32f407\n", "CONFIG_CLOCK=8000000\n", "CONFIG_CANBUS=y\n"]
    rows, changed = _render_diff(before, after)
    assert isinstance(rows[0], Text)
    return MenuconfigResult(
        ran=True, saved=True, changed=True, diff_lines=rows, lines_changed=changed
    )


def test_config_diff_receipt_snapshot(snap_compare) -> None:
    modal = ConfigDiffDialog(_sample_diff_result())
    assert snap_compare(_ModalHost(modal), terminal_size=_SIZE)


def test_config_diff_flash_question_snapshot(snap_compare) -> None:
    """The flash-flow variant: the final ask under the diff names the flash."""
    modal = ConfigDiffDialog(
        _sample_diff_result(),
        question="Flash 'Octopus Pro' with this config?",
        continue_label="Flash now",
        cancel_label="Cancel flash",
    )
    assert snap_compare(_ModalHost(modal), terminal_size=_SIZE)
