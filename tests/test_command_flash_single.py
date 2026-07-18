"""Focused test for cmd_flash's build-failure path.

Drives ``cmd_flash`` with device_key pre-selected (skips the interactive USB
picker) and every collaborator up to the build step monkeypatched to a
trivial success, so the run lands squarely on ``run_build`` returning a
failure with ``error_output``. Asserts the captured tail is surfaced as
``info`` events (section "Build") in the log before the sticky
``error_recovery`` event -- the behavior added when ``run_build`` stopped
streaming raw output to inherited stdio (which would overdraw the TUI).
"""

from __future__ import annotations

import types

from conftest import FakeDecisionProvider, RecordingSink

from kflash import flash_steps
from kflash.commands import flash_single
from kflash.commands.flash_single import cmd_flash
from kflash.events import Emitter
from kflash.models import DeviceEntry, DiscoveredDevice, GlobalConfig, RegistryData


class _FakeRegistry:
    def __init__(self, data):
        self._data = data

    def load(self):
        return self._data

    def get(self, key):
        return self._data.devices.get(key)


def _entry():
    return DeviceEntry(
        key="octo",
        name="Octopus",
        mcu="stm32h723",
        serial_pattern="usb-Klipper_stm32h723xx_ABC*",
        flash_command="katapult",
        bootloader_method="usb",
        flashable=True,
    )


def _reach_build_stage(monkeypatch, build_result):
    """Monkeypatch flash_single's collaborators so cmd_flash reaches the
    build step with a single connected, unambiguous USB device."""
    device = DiscoveredDevice(
        path="/dev/serial/by-id/usb-Klipper_stm32h723xx_ABC123-if00",
        filename="usb-Klipper_stm32h723xx_ABC123-if00",
    )
    monkeypatch.setattr(flash_single, "scan_serial_devices", lambda: [device])
    monkeypatch.setattr(flash_single, "match_devices", lambda pattern, devices: [])
    monkeypatch.setattr(flash_single, "match_device", lambda pattern, devices: device)
    monkeypatch.setattr(flash_single, "extract_mcu_from_serial", lambda filename: None)
    monkeypatch.setattr(flash_single, "build_blocked_list", lambda data: {})
    monkeypatch.setattr(flash_single, "blocked_reason_for_entry", lambda e, bl: None)
    monkeypatch.setattr(flash_single, "get_mcu_versions", lambda: {})
    monkeypatch.setattr(flash_single, "get_host_klipper_version", lambda kd: None)
    monkeypatch.setattr(flash_single, "validate_device_flash_config", lambda entry, em: True)
    monkeypatch.setattr(
        flash_single, "preflight_flash", lambda em, kd, katd, cmd: True
    )
    monkeypatch.setattr(
        flash_single, "moonraker_safety_gate", lambda **k: flash_steps.SafetyGate.PROCEED
    )
    monkeypatch.setattr(
        flash_single,
        "load_and_validate_config",
        lambda **k: types.SimpleNamespace(ok=True, exit_code=0),
    )
    monkeypatch.setattr(flash_single, "resolve_ccache_usage", lambda **k: False)
    monkeypatch.setattr(
        flash_single, "check_dirty_repo", lambda v: types.SimpleNamespace(is_dirty=False)
    )
    monkeypatch.setattr(flash_single, "run_build", lambda *a, **k: build_result)


def _registry_one_usb(entry):
    data = RegistryData(
        global_config=GlobalConfig(klipper_dir="/tmp/k", katapult_dir="/tmp/kt"),
        devices={entry.key: entry},
    )
    return _FakeRegistry(data)


def test_build_failure_tail_surfaces_as_info_events_before_error_recovery(monkeypatch):
    entry = _entry()
    build_result = types.SimpleNamespace(
        success=False,
        error_message="make failed with exit code 2",
        error_output="\n".join(f"line{i}" for i in range(1, 26)),  # 25 lines
        firmware_path=None,
        firmware_size=0,
    )
    _reach_build_stage(monkeypatch, build_result)

    sink = RecordingSink()
    em = Emitter(sink)
    decider = FakeDecisionProvider()

    rc = cmd_flash(_registry_one_usb(entry), entry.key, em, decider)

    assert rc == 1

    kinds = [e.kind for e in sink.events]
    assert "error_recovery" in kinds
    error_idx = kinds.index("error_recovery")

    build_info_events = [
        e for e in sink.events[:error_idx] if e.kind == "info" and e.section == "Build"
    ]
    # Only the last 20 lines are surfaced.
    assert [e.message for e in build_info_events] == [f"line{i}" for i in range(6, 26)]


def test_build_success_emits_no_tail_lines(monkeypatch):
    entry = _entry()
    # Reaching flash-sequence territory is heavier to fake; just confirm that
    # a *successful* build result does not spuriously emit tail lines (the
    # tail emission is gated on `not build_result.success`).
    build_result = types.SimpleNamespace(
        success=True,
        error_message=None,
        error_output=None,
        firmware_path="/tmp/k/out/klipper.bin",
        firmware_size=0,
    )
    _reach_build_stage(monkeypatch, build_result)
    # check_firmware_artifact will run for real on a nonexistent path and
    # fail closed -- that's fine, we only care no "Build" info tail leaked.
    sink = RecordingSink()
    em = Emitter(sink)
    decider = FakeDecisionProvider()

    cmd_flash(_registry_one_usb(entry), entry.key, em, decider)

    build_info_events = [e for e in sink.events if e.kind == "info" and e.section == "Build"]
    assert build_info_events == []
