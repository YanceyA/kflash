"""Pilot + snapshot tests for the beta Textual dashboard.

No real USB / Moonraker / systemctl calls: every engine read the dashboard makes
is monkeypatched on :mod:`kflash.ui.screens.dashboard` to return deterministic
data, and the registry is a real :class:`~kflash.registry.Registry` over a tmp
JSON file. The flash flow is driven through the *real* :class:`EngineBridge`
with a stubbed ``cmd_flash`` callable so the event stream + completion path are
exercised without touching hardware.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from textual.widgets import DataTable, RichLog, Static

import kflash.ui.screens.dashboard as dash
from kflash.events import Emitter
from kflash.models import DiscoveredDevice
from kflash.registry import Registry
from kflash.ui.app import KflashApp
from kflash.ui.dialogs import ConfirmDialog, DecisionConfirmDialog
from kflash.ui.menuconfig import ConfigDiffDialog, MenuconfigResult
from kflash.ui.screens.operation import OperationScreen

_SIZE = (80, 40)

_REGISTRY = {
    "global": {"klipper_dir": "~/klipper", "katapult_dir": "~/katapult"},
    "devices": {
        "octopus": {
            "name": "Octopus Pro",
            "mcu": "stm32h723",
            "serial_pattern": "usb-Klipper_stm32h723xx_ABC*",
            "flash_command": "katapult",
            "mcu_name": "mcu",
            "flashable": True,
        },
        "spider": {
            "name": "Spider (excluded)",
            "mcu": "stm32f446",
            "serial_pattern": "usb-Klipper_stm32f446xx_XYZ*",
            "flash_command": "make_flash",
            "mcu_name": "mcu spider",
            "flashable": False,
        },
    },
    "blocked_devices": [{"pattern": "ch340", "reason": "Serial adapter"}],
}


def _write_registry(tmp_path) -> Registry:
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(_REGISTRY), encoding="utf-8")
    return Registry(str(path))


def _fake_usb():
    return [
        # Matches "octopus" (connected, flashable).
        DiscoveredDevice(
            path="/dev/serial/by-id/usb-Klipper_stm32h723xx_ABC123-if00",
            filename="usb-Klipper_stm32h723xx_ABC123-if00",
        ),
        # Matches "spider" (excluded).
        DiscoveredDevice(
            path="/dev/serial/by-id/usb-Klipper_stm32f446xx_XYZ999-if00",
            filename="usb-Klipper_stm32f446xx_XYZ999-if00",
        ),
        # Unregistered but supported -> "new".
        DiscoveredDevice(
            path="/dev/serial/by-id/usb-Klipper_rp2040_NEW01-if00",
            filename="usb-Klipper_rp2040_NEW01-if00",
        ),
        # Blocked by pattern -> "blocked".
        DiscoveredDevice(
            path="/dev/serial/by-id/usb-ch340_serial-if00",
            filename="usb-ch340_serial-if00",
        ),
    ]


@pytest.fixture(autouse=True)
def _stub_engine_reads(monkeypatch):
    """Freeze every engine read so boot/snapshots are deterministic."""
    monkeypatch.setattr(dash, "scan_serial_devices", _fake_usb)
    monkeypatch.setattr(dash, "get_mcu_versions", lambda: {"main": "v0.12.0-1"})
    monkeypatch.setattr(dash, "get_host_klipper_version", lambda d: "v0.12.0-5")
    monkeypatch.setattr(dash, "get_mcu_canbus_map", lambda: None)
    monkeypatch.setattr(dash, "get_service_status", lambda name: "active")
    monkeypatch.setattr(dash, "is_mcu_outdated", lambda host, mcu: True)
    # Never enter the sudo/suspend path in tests.
    monkeypatch.setattr(dash, "is_service_active", lambda: False)
    monkeypatch.setattr(dash, "verify_passwordless_sudo", lambda: True)
    # Default: devices have a cached config so F offers menuconfig (never the
    # required path that would launch the real ncurses subprocess). Tests that
    # exercise menuconfig override these.
    monkeypatch.setattr(dash.menuconfig, "has_cached_config", lambda key, kdir: True)
    # Fresh fetch cache per test so the 5 s Moonraker cache never leaks values
    # across tests (each test stubs its own engine reads).
    monkeypatch.setattr(dash, "_fetch_cache", {})


def _run(coro) -> None:
    asyncio.run(coro)


def test_boot_renders_devices(tmp_path) -> None:
    """Boot loads the registry + scan and paints grouped device rows."""
    registry = _write_registry(tmp_path)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app._dashboard
            table = screen.query_one("#devices", DataTable)
            names = [r.name for r in screen._rows]
            # Registered (connected first), then new, then blocked.
            assert names == [
                "Octopus Pro",
                "Spider (excluded)",
                "usb-Klipper_rp2040_NEW01-if00",
                "usb-ch340_serial-if00",
            ]
            assert table.row_count == 4
            groups = [r.group for r in screen._rows]
            assert groups == ["registered", "registered", "new", "blocked"]
            # Numbers: three selectable, blocked stays 0.
            assert [r.number for r in screen._rows] == [1, 2, 3, 0]

    _run(go())


def test_cursor_and_jk_navigation(tmp_path) -> None:
    registry = _write_registry(tmp_path)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            table = app._dashboard.query_one("#devices", DataTable)
            assert table.cursor_row == 0
            await pilot.press("j")
            assert table.cursor_row == 1
            await pilot.press("j")
            assert table.cursor_row == 2
            await pilot.press("k")
            assert table.cursor_row == 1

    _run(go())


def test_number_key_jump(tmp_path) -> None:
    registry = _write_registry(tmp_path)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            table = app._dashboard.query_one("#devices", DataTable)
            await pilot.press("3")
            assert table.cursor_row == 2
            await pilot.press("1")
            assert table.cursor_row == 0

    _run(go())


def test_q_quits(tmp_path) -> None:
    registry = _write_registry(tmp_path)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        # run_test context exits cleanly once the app has exited.

    _run(go())


def test_blocked_and_excluded_not_flashable(tmp_path) -> None:
    """Blocked and excluded rows report not-flashable and never launch a job."""
    registry = _write_registry(tmp_path)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app._dashboard
            # Excluded (row 2) is not flashable.
            assert screen._rows[1].can_flash is False
            # Blocked (row 4) is not flashable.
            assert screen._rows[3].can_flash is False
            # Move to the excluded row and press F: no confirm dialog appears.
            await pilot.press("2")
            await pilot.press("f")
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmDialog)
            msg = screen.query_one("#status-message", Static)
            assert "not flashable" in str(msg.content)

    _run(go())


def test_flash_flow_pushes_operation_screen(tmp_path, monkeypatch) -> None:
    """F -> confirm -> real EngineBridge job -> OperationScreen shows events."""
    registry = _write_registry(tmp_path)
    calls = {}

    def fake_cmd_flash(reg, key, em: Emitter, decider, skip_menuconfig=False):
        calls["key"] = key
        calls["skip_menuconfig"] = skip_menuconfig
        em.phase("Build", "Compiling firmware")
        em.success("Flashed Octopus Pro via katapult in 12.3s")
        return 0

    # The _job closure resolves the module-global cmd_flash at call time.
    monkeypatch.setattr(dash, "cmd_flash", fake_cmd_flash)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            # Octopus (row 1) is flashable; press F, then confirm.
            await pilot.press("1")
            await pilot.press("f")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDialog)
            await pilot.press("y")
            await pilot.pause()
            # The menuconfig offer appears (cached config present); decline it so
            # the flash proceeds with the cached config (skip_menuconfig=True).
            assert isinstance(app.screen, DecisionConfirmDialog)
            await pilot.press("n")
            await pilot.pause()
            # Let the bridge worker run + messages drain.
            bridge = app.bridge
            for _ in range(50):
                await pilot.pause()
                if not bridge.is_busy:
                    break
            await pilot.pause()
            await pilot.pause()
            assert calls["key"] == "octopus"
            assert calls["skip_menuconfig"] is True
            operation = app.screen
            assert isinstance(operation, OperationScreen)
            log = operation.query_one("#op-log", RichLog)
            text = "\n".join(seg.text for line in log.lines for seg in line)
            assert "Compiling firmware" in text
            assert "Flashed Octopus Pro" in text
            # Failure hold / success hold: the screen stays until a key returns.
            await pilot.press("enter")
            await pilot.pause()
            assert app._dashboard is app.screen

    _run(go())


# --------------------------------------------------------------------------- #
# menuconfig gate (§6): suspend + config-diff receipt before the flash
# --------------------------------------------------------------------------- #
def _changed_result() -> MenuconfigResult:
    from rich.text import Text

    return MenuconfigResult(
        ran=True,
        saved=True,
        changed=True,
        diff_lines=[Text("+CONFIG_NEW=y"), Text("-CONFIG_OLD=y")],
        lines_changed=2,
    )


async def _pause_until(pilot, predicate, tries: int = 60) -> None:
    for _ in range(tries):
        await pilot.pause()
        if predicate():
            return


def test_flash_menuconfig_offer_edit_diff_continue(tmp_path, monkeypatch) -> None:
    """F -> confirm -> offer 'Edit config?' (yes) -> diff receipt -> continue -> flash."""
    registry = _write_registry(tmp_path)
    calls: dict = {}

    def fake_cmd_flash(reg, key, em, decider, skip_menuconfig=False):
        calls["key"] = key
        calls["skip"] = skip_menuconfig
        em.success("Flashed")
        return 0

    def fake_suspended(app, key, kdir):
        calls["menuconfig_key"] = key
        return _changed_result()

    monkeypatch.setattr(dash, "cmd_flash", fake_cmd_flash)
    monkeypatch.setattr(dash.menuconfig, "run_menuconfig_suspended", fake_suspended)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("1")
            await pilot.press("f")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDialog)
            await pilot.press("y")
            await pilot.pause()
            # Cached config -> the menuconfig offer appears; accept it.
            assert isinstance(app.screen, DecisionConfirmDialog)
            await pilot.press("y")
            await pilot.pause()
            # menuconfig ran (stubbed) -> the config-diff receipt shows.
            assert isinstance(app.screen, ConfigDiffDialog)
            assert calls["menuconfig_key"] == "octopus"
            await pilot.press("y")  # continue -> flash
            await _pause_until(pilot, lambda: not app.bridge.is_busy)
            await pilot.pause()
            await pilot.pause()
            assert calls["key"] == "octopus"
            assert calls["skip"] is True
            assert isinstance(app.screen, OperationScreen)

    _run(go())


def test_flash_menuconfig_diff_dialog_asks_to_proceed_with_flash(
    tmp_path, monkeypatch
) -> None:
    """Hardware feedback: the diff receipt's Y/N must clearly ask "flash or not".

    The Y/N on the config-diff dialog proceeds with (or aborts) the FLASH -- it
    is not an accept/reject of the config change (the config is already saved).
    The dialog must say so: the question names the flash and the device, and the
    hint labels say "Flash now" / "Cancel flash" rather than a bare
    Continue/Cancel.
    """
    registry = _write_registry(tmp_path)

    monkeypatch.setattr(dash, "cmd_flash", lambda *a, **k: 0)
    monkeypatch.setattr(
        dash.menuconfig, "run_menuconfig_suspended", lambda app, k, d: _changed_result()
    )

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("1")
            await pilot.press("f")
            await pilot.pause()
            await pilot.press("y")  # confirm flash
            await pilot.pause()
            await pilot.press("y")  # accept menuconfig offer
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, ConfigDiffDialog)
            body = " ".join(str(w.content) for w in dialog.query(Static))
            # The final ask is about the flash, for this device...
            assert "Flash 'Octopus Pro' with this config?" in body
            # ...and the key hints say what Y and N actually do.
            assert "Flash now" in body
            assert "Cancel flash" in body

    _run(go())


def test_flash_menuconfig_diff_cancel_aborts(tmp_path, monkeypatch) -> None:
    """Cancelling the diff receipt aborts the flash: cmd_flash never runs."""
    registry = _write_registry(tmp_path)
    calls: dict = {}

    monkeypatch.setattr(
        dash, "cmd_flash", lambda *a, **k: calls.setdefault("ran", True) or 0
    )
    monkeypatch.setattr(
        dash.menuconfig, "run_menuconfig_suspended", lambda app, k, d: _changed_result()
    )

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("1")
            await pilot.press("f")
            await pilot.pause()
            await pilot.press("y")  # confirm flash
            await pilot.pause()
            await pilot.press("y")  # accept menuconfig offer
            await pilot.pause()
            assert isinstance(app.screen, ConfigDiffDialog)
            await pilot.press("n")  # cancel the diff -> abort
            await _pause_until(pilot, lambda: app._dashboard is app.screen)
            assert app._dashboard is app.screen
            assert "ran" not in calls
            msg = app._dashboard.query_one("#status-message", Static)
            assert "cancelled" in str(msg.content).lower()

    _run(go())


def test_flash_menuconfig_gate_off_skips_the_offer(tmp_path, monkeypatch) -> None:
    """Hardware feedback: menuconfig_before_flash=False -> F flashes the cached
    config directly, with no menuconfig offer dialog in between."""
    registry_dict = json.loads(json.dumps(_REGISTRY))
    registry_dict["global"]["menuconfig_before_flash"] = False
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(registry_dict), encoding="utf-8")
    registry = Registry(str(path))
    calls: dict = {}

    def fake_cmd_flash(reg, key, em, decider, skip_menuconfig=False):
        calls["key"] = key
        calls["skip"] = skip_menuconfig
        em.success("Flashed")
        return 0

    def fail_suspended(app, key, kdir):
        raise AssertionError("menuconfig must not run when the gate is off")

    monkeypatch.setattr(dash, "cmd_flash", fake_cmd_flash)
    monkeypatch.setattr(dash.menuconfig, "run_menuconfig_suspended", fail_suspended)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("1")
            await pilot.press("f")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDialog)
            await pilot.press("y")  # confirm flash -> straight to the job
            await _pause_until(pilot, lambda: not app.bridge.is_busy)
            await pilot.pause()
            await pilot.pause()
            assert calls["key"] == "octopus"
            assert calls["skip"] is True
            assert isinstance(app.screen, OperationScreen)

    _run(go())


def test_flash_menuconfig_gate_off_still_requires_first_config(
    tmp_path, monkeypatch
) -> None:
    """Gate off + NO cached config -> menuconfig still runs (a first flash
    cannot proceed without a saved config)."""
    registry_dict = json.loads(json.dumps(_REGISTRY))
    registry_dict["global"]["menuconfig_before_flash"] = False
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(registry_dict), encoding="utf-8")
    registry = Registry(str(path))
    state = {"cached": False}
    calls: dict = {}

    def fake_cmd_flash(reg, key, em, decider, skip_menuconfig=False):
        calls["skip"] = skip_menuconfig
        em.success("Flashed")
        return 0

    def fake_suspended(app, key, kdir):
        state["cached"] = True  # menuconfig produced + cached a config
        return _changed_result()

    monkeypatch.setattr(dash, "cmd_flash", fake_cmd_flash)
    monkeypatch.setattr(dash.menuconfig, "has_cached_config", lambda k, d: state["cached"])
    monkeypatch.setattr(dash.menuconfig, "run_menuconfig_suspended", fake_suspended)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("1")
            await pilot.press("f")
            await pilot.pause()
            await pilot.press("y")  # confirm flash
            await pilot.pause()
            # Required path still applies with the gate off: the diff shows.
            assert isinstance(app.screen, ConfigDiffDialog)
            await pilot.press("y")  # Flash now
            await _pause_until(pilot, lambda: not app.bridge.is_busy)
            await pilot.pause()
            assert calls["skip"] is True
            assert isinstance(app.screen, OperationScreen)

    _run(go())


def test_flash_menuconfig_required_when_no_cache(tmp_path, monkeypatch) -> None:
    """No cached config -> menuconfig runs unconditionally (no offer) before flash."""
    registry = _write_registry(tmp_path)
    state = {"cached": False}
    calls: dict = {}

    def fake_cmd_flash(reg, key, em, decider, skip_menuconfig=False):
        calls["skip"] = skip_menuconfig
        em.success("Flashed")
        return 0

    def fake_suspended(app, key, kdir):
        state["cached"] = True  # menuconfig produced + cached a config
        return _changed_result()

    monkeypatch.setattr(dash, "cmd_flash", fake_cmd_flash)
    monkeypatch.setattr(dash.menuconfig, "has_cached_config", lambda k, d: state["cached"])
    monkeypatch.setattr(dash.menuconfig, "run_menuconfig_suspended", fake_suspended)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("1")
            await pilot.press("f")
            await pilot.pause()
            await pilot.press("y")  # confirm flash
            await pilot.pause()
            # Required path: NO offer dialog; menuconfig ran and the diff shows.
            assert isinstance(app.screen, ConfigDiffDialog)
            await pilot.press("y")  # continue -> flash
            await _pause_until(pilot, lambda: not app.bridge.is_busy)
            await pilot.pause()
            assert calls["skip"] is True
            assert isinstance(app.screen, OperationScreen)

    _run(go())


def test_flash_menuconfig_ctrlc_returns_to_dashboard(tmp_path, monkeypatch) -> None:
    """Ctrl+C during the menuconfig suspend window returns to the dashboard."""
    registry = _write_registry(tmp_path)
    calls: dict = {}

    monkeypatch.setattr(
        dash, "cmd_flash", lambda *a, **k: calls.setdefault("ran", True) or 0
    )
    monkeypatch.setattr(
        dash.menuconfig,
        "run_menuconfig_suspended",
        lambda app, k, d: MenuconfigResult(ran=True, cancelled=True),
    )

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("1")
            await pilot.press("f")
            await pilot.pause()
            await pilot.press("y")  # confirm flash
            await pilot.pause()
            await pilot.press("y")  # accept offer -> menuconfig cancelled
            await _pause_until(pilot, lambda: app._dashboard is app.screen)
            assert app._dashboard is app.screen
            assert "ran" not in calls
            msg = app._dashboard.query_one("#status-message", Static)
            assert "cancelled" in str(msg.content).lower()

    _run(go())


# --------------------------------------------------------------------------- #
# M: direct menuconfig entry for the selected device (hardware feedback)
# --------------------------------------------------------------------------- #
def test_m_opens_menuconfig_directly_for_selected_device(
    tmp_path, monkeypatch
) -> None:
    """M runs menuconfig for the highlighted device with NO flash attached:
    receipt is close-only, cmd_flash never runs, status reports the update."""
    registry = _write_registry(tmp_path)
    calls: dict = {}

    def fail_cmd_flash(*a, **k):
        raise AssertionError("M must never start a flash")

    def fake_suspended(app, key, kdir):
        calls["menuconfig_key"] = key
        return _changed_result()

    monkeypatch.setattr(dash, "cmd_flash", fail_cmd_flash)
    monkeypatch.setattr(dash.menuconfig, "run_menuconfig_suspended", fake_suspended)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("1")
            await pilot.press("m")
            await pilot.pause()
            assert calls["menuconfig_key"] == "octopus"
            # The receipt is informational: no flash question, Close only.
            dialog = app.screen
            assert isinstance(dialog, ConfigDiffDialog)
            body = " ".join(str(w.content) for w in dialog.query(Static))
            assert "Close" in body
            assert "Flash now" not in body
            await pilot.press("enter")
            await _pause_until(pilot, lambda: app._dashboard is app.screen)
            msg = app._dashboard.query_one("#status-message", Static)
            assert "Config updated for Octopus Pro" in str(msg.content)

    _run(go())


def test_m_reports_when_menuconfig_saves_no_changes(tmp_path, monkeypatch) -> None:
    registry = _write_registry(tmp_path)

    monkeypatch.setattr(
        dash.menuconfig,
        "run_menuconfig_suspended",
        lambda app, k, d: MenuconfigResult(ran=True, saved=False, changed=False),
    )

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("1")
            await pilot.press("m")
            await pilot.pause()
            # No diff -> no receipt dialog, just a status line.
            assert app._dashboard is app.screen
            msg = app._dashboard.query_one("#status-message", Static)
            assert "no changes" in str(msg.content)

    _run(go())


def test_m_on_unregistered_row_warns(tmp_path, monkeypatch) -> None:
    """M on a scanned 'new' row warns instead of launching menuconfig."""
    registry = _write_registry(tmp_path)
    calls: dict = {}

    monkeypatch.setattr(
        dash.menuconfig,
        "run_menuconfig_suspended",
        lambda app, k, d: calls.setdefault("ran", True) or _changed_result(),
    )

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("3")  # the unregistered "new" row
            await pilot.press("m")
            await pilot.pause()
            assert "ran" not in calls
            msg = app._dashboard.query_one("#status-message", Static)
            assert "not registered" in str(msg.content)

    _run(go())


def test_sudo_ctrlc_returns_to_dashboard(tmp_path, monkeypatch) -> None:
    """Ctrl+C at the sudo pre-acquire prompt aborts the flash, not the app."""
    registry = _write_registry(tmp_path)
    calls: dict = {}

    monkeypatch.setattr(
        dash, "cmd_flash", lambda *a, **k: calls.setdefault("ran", True) or 0
    )
    # Force the sudo suspend path, then interrupt it.
    monkeypatch.setattr(dash, "is_service_active", lambda: True)
    monkeypatch.setattr(dash, "verify_passwordless_sudo", lambda: False)

    def boom() -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(dash, "acquire_sudo", boom)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("1")
            await pilot.press("f")
            await pilot.pause()
            await pilot.press("y")  # confirm flash
            await pilot.pause()
            # Decline the menuconfig offer so we reach the sudo pre-acquire.
            assert isinstance(app.screen, DecisionConfirmDialog)
            await pilot.press("n")
            await _pause_until(pilot, lambda: app._dashboard is app.screen)
            assert app._dashboard is app.screen
            assert "ran" not in calls
            assert not app.bridge.is_busy
            msg = app._dashboard.query_one("#status-message", Static)
            assert "sudo" in str(msg.content).lower()

    _run(go())


# --------------------------------------------------------------------------- #
# R2: live background refresh (hotplug + status) and the D-refresh CAN scan
# --------------------------------------------------------------------------- #
def test_hotplug_poll_adds_device_without_keypress(tmp_path, monkeypatch) -> None:
    """A hotplug appears via the poll callback alone -- no user keypress."""
    registry = _write_registry(tmp_path)

    octo = DiscoveredDevice(
        path="/dev/serial/by-id/usb-Klipper_stm32h723xx_ABC123-if00",
        filename="usb-Klipper_stm32h723xx_ABC123-if00",
    )
    newdev = DiscoveredDevice(
        path="/dev/serial/by-id/usb-Klipper_rp2040_NEW01-if00",
        filename="usb-Klipper_rp2040_NEW01-if00",
    )
    usb_state = {"devices": [octo]}
    monkeypatch.setattr(dash, "scan_serial_devices", lambda: usb_state["devices"])
    mtime = {"v": 1.0}
    monkeypatch.setattr(dash, "_serial_dir_mtime", lambda: mtime["v"])

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app._dashboard
            # Let the boot fetch settle so the in-flight guard is clear.
            await _pause_until(pilot, lambda: not screen._fetch_in_flight)
            assert not any("rp2040" in r.name for r in screen._rows)
            # A device is plugged in: the scan sees it and the dir mtime bumps.
            usb_state["devices"] = [octo, newdev]
            mtime["v"] = 2.0
            # Drive the hotplug poll directly (simulating the 2 s timer tick).
            screen._poll_hotplug()
            await _pause_until(
                pilot, lambda: any("rp2040" in r.name for r in screen._rows)
            )
            assert any("rp2040" in r.name for r in screen._rows)

    _run(go())


def test_hotplug_poll_gated_on_mtime(tmp_path, monkeypatch) -> None:
    """When /dev/serial/by-id is unchanged, the hotplug poll skips the fetch."""
    registry = _write_registry(tmp_path)
    fetches = {"n": 0}
    real = dash.fetch_dashboard_state

    def counting(*a, **k):
        fetches["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(dash, "fetch_dashboard_state", counting)
    monkeypatch.setattr(dash, "_serial_dir_mtime", lambda: 7.0)  # constant

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app._dashboard
            before = fetches["n"]
            screen._poll_hotplug()  # mtime unchanged -> gated
            await pilot.pause()
            await pilot.pause()
            assert fetches["n"] == before

    _run(go())


class _BusyBridge:
    is_busy = True

    def shutdown(self, timeout=None) -> None:
        pass


def test_polling_paused_while_bridge_busy(tmp_path, monkeypatch) -> None:
    """No background fetch runs while a flash is in flight."""
    registry = _write_registry(tmp_path)
    fetches = {"n": 0}
    real = dash.fetch_dashboard_state

    def counting(*a, **k):
        fetches["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(dash, "fetch_dashboard_state", counting)
    monkeypatch.setattr(dash, "_serial_dir_mtime", lambda: time.monotonic())

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app._dashboard
            app.bridge = _BusyBridge()  # simulate an in-flight flash
            before = fetches["n"]
            screen._poll_hotplug()
            screen._poll_status()
            await pilot.pause()
            await pilot.pause()
            assert fetches["n"] == before  # both polls held off

    _run(go())


def test_d_refresh_runs_can_scan_when_enabled(tmp_path, monkeypatch) -> None:
    """D scans the CAN bus for unregistered devices when can_scan_on_refresh."""
    from kflash.models import DiscoveredCanDevice

    reg = dict(_REGISTRY)
    reg["global"] = {
        "klipper_dir": "~/klipper",
        "katapult_dir": "~/katapult",
        "can_scan_on_refresh": True,
    }
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(reg), encoding="utf-8")
    registry = Registry(str(path))

    monkeypatch.setattr(dash, "get_can_interfaces", lambda: ["can0"])
    monkeypatch.setattr(
        dash,
        "scan_can_devices",
        lambda iface, kdir: [
            DiscoveredCanDevice(uuid="112233445566", application="Katapult")
        ],
    )

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app._dashboard
            assert not any(r.is_can and r.group == "new" for r in screen._rows)
            await pilot.press("d")
            await _pause_until(
                pilot, lambda: any("112233445566" in r.name for r in screen._rows)
            )
            can_rows = [r for r in screen._rows if "112233445566" in r.name]
            assert can_rows and can_rows[0].canbus_interface == "can0"

    _run(go())


def test_flash_all_pushes_operation_screen(tmp_path, monkeypatch) -> None:
    """B -> confirm 'Flash all N connected devices?' -> cmd_flash_all in op screen."""
    registry = _write_registry(tmp_path)
    calls = {}

    def fake_cmd_flash_all(reg, em: Emitter, decider):
        calls["ran"] = True
        em.phase("Flash All", "Flashing 1 device(s)...")
        em.info("", "  Octopus Pro  PASS", device_key="octopus",
                device_name="Octopus Pro", marker="PASS", elapsed=5.0)
        return 0

    monkeypatch.setattr(dash, "cmd_flash_all", fake_cmd_flash_all)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, ConfirmDialog)
            # Only Octopus is connected + flashable -> N == 1.
            assert "Flash all 1 connected devices?" in str(dialog._message)
            await pilot.press("y")
            bridge = app.bridge
            for _ in range(50):
                await pilot.pause()
                if not bridge.is_busy:
                    break
            await pilot.pause()
            await pilot.pause()
            assert calls.get("ran") is True
            operation = app.screen
            assert isinstance(operation, OperationScreen)
            table = operation.query_one("#op-results", DataTable)
            assert table.row_count == 1

    _run(go())


def test_edit_opens_device_config_for_registered(tmp_path) -> None:
    """E on a registered row opens the per-device config editor."""
    from kflash.ui.screens.device_config import DeviceConfigScreen

    registry = _write_registry(tmp_path)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("1")  # Octopus (registered)
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, DeviceConfigScreen)
            assert app.screen._original_key == "octopus"

    _run(go())


def test_edit_on_new_row_offers_add(tmp_path) -> None:
    """E on a scanned 'new' row offers to register it (routes to add-device)."""
    from kflash.ui.screens.add_device import AddDeviceScreen

    registry = _write_registry(tmp_path)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("3")  # the rp2040 "new" row
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, DecisionConfirmDialog)
            await pilot.press("y")  # accept -> add-device wizard
            await pilot.pause()
            assert isinstance(app.screen, AddDeviceScreen)

    _run(go())


def test_remove_on_blocked_row_shows_notice(tmp_path) -> None:
    """R on a blocked/new row is a no-op notice (only registered rows remove)."""
    registry = _write_registry(tmp_path)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app._dashboard
            await pilot.press("3")  # the "new" row (number 3)
            await pilot.press("r")
            await pilot.pause()
            msg = screen.query_one("#status-message", Static)
            assert "not registered" in str(msg.content)
            assert not app.bridge.is_busy

    _run(go())


def test_remove_confirm_removes_and_refreshes(tmp_path) -> None:
    """R -> styled 'Remove ...?' confirm -> cmd_remove_device drops the device."""
    registry = _write_registry(tmp_path)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app._dashboard
            await pilot.press("1")  # Octopus (registered)
            await pilot.press("r")
            await pilot.pause()
            # The engine's ConfirmDecision renders as the styled dialog, naming
            # the device.
            assert isinstance(app.screen, DecisionConfirmDialog)
            assert "Octopus Pro" in str(app.screen._message)
            await pilot.press("y")  # confirm removal
            await _pause_until(pilot, lambda: not app.bridge.is_busy)
            await pilot.pause()
            await pilot.pause()
            # The device is gone from the registry and the refreshed list.
            assert registry.get("octopus") is None
            assert not any(r.key == "octopus" for r in screen._rows)
            msg = screen.query_one("#status-message", Static)
            assert "Removed" in str(msg.content)

    _run(go())


def test_a_opens_add_device_and_c_opens_settings(tmp_path) -> None:
    """A pushes the add-device wizard; C pushes the settings editor."""
    from kflash.ui.screens.add_device import AddDeviceScreen
    from kflash.ui.screens.settings import SettingsScreen

    registry = _write_registry(tmp_path)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            # C opens settings; Escape returns to the dashboard.
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)
            await pilot.press("escape")
            await pilot.pause()
            # A on a registered row (not "new") opens the fresh-scan wizard.
            await pilot.press("1")
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, AddDeviceScreen)

    _run(go())


def test_dashboard_snapshot(tmp_path, snap_compare) -> None:
    """Deterministic snapshot of the populated dashboard."""
    registry = _write_registry(tmp_path)
    app = KflashApp(registry)
    # Two pauses let the fetch worker land before the snapshot is taken.
    assert snap_compare(app, terminal_size=_SIZE, run_before=_settle)


async def _settle(pilot) -> None:
    await pilot.pause()
    await pilot.pause()
    await pilot.pause()
