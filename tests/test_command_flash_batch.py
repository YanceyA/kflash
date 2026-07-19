"""Tier-1 tests for commands.flash_batch ordering/dedupe helpers.

Covers the pure list-shaping helpers used by ``cmd_flash_all``:
- ``_sort_flash_all_devices`` -- CAN toolheads first, USB/no-role middle,
  CAN bridges last, alphabetical by key within each group.
- ``_dedupe_flash_all_devices`` -- key-dedupe preserving first occurrence.
"""

from __future__ import annotations

import types

from conftest import FakeDecisionProvider, RecordingSink

from kflash import flash_steps
from kflash.commands import flash_batch
from kflash.commands.flash_batch import (
    _dedupe_flash_all_devices,
    _sort_flash_all_devices,
    cmd_flash_all,
)
from kflash.events import Emitter, NullSink
from kflash.models import DeviceEntry, GlobalConfig, RegistryData


def _usb(key):
    return DeviceEntry(key=key, name=key, mcu="stm32h723", serial_pattern=f"usb-{key}*")


def _can(key, role=None):
    return DeviceEntry(
        key=key,
        name=key,
        mcu="stm32h723",
        canbus_uuid="112233445566",
        canbus_interface="can0",
        role=role,
    )


def test_sort_puts_can_toolheads_first_bridges_last():
    devices = [
        _can("z-bridge", role="bridge"),
        _usb("m-usb"),
        _can("a-toolhead", role="toolhead"),
    ]
    ordered = [e.key for e in _sort_flash_all_devices(devices)]
    assert ordered == ["a-toolhead", "m-usb", "z-bridge"]


def test_sort_is_alphabetical_within_each_group():
    devices = [
        _can("t-two", role="toolhead"),
        _can("t-one", role="toolhead"),
        _usb("u-two"),
        _usb("u-one"),
        _can("b-two", role="bridge"),
        _can("b-one", role="bridge"),
    ]
    ordered = [e.key for e in _sort_flash_all_devices(devices)]
    assert ordered == ["t-one", "t-two", "u-one", "u-two", "b-one", "b-two"]


def test_sort_ignores_role_on_usb_devices():
    # A USB (non-CAN) device with a stale role must still land in the middle
    # group, never in toolheads/bridges.
    usb_with_role = _usb("u-mid")
    usb_with_role.role = "bridge"  # stale role data on a USB device
    devices = [_can("a-tool", role="toolhead"), usb_with_role, _can("z-brdg", role="bridge")]
    ordered = [e.key for e in _sort_flash_all_devices(devices)]
    assert ordered == ["a-tool", "u-mid", "z-brdg"]


def test_sort_no_role_can_device_lands_in_middle():
    devices = [_can("c-norole"), _usb("a-usb")]
    ordered = [e.key for e in _sort_flash_all_devices(devices)]
    # Both are middle-group -> alphabetical by key.
    assert ordered == ["a-usb", "c-norole"]


def test_dedupe_preserves_first_occurrence():
    first = _usb("dup")
    second = _usb("dup")  # same key
    other = _usb("other")
    result = _dedupe_flash_all_devices([first, second, other])
    assert [e.key for e in result] == ["dup", "other"]
    assert result[0] is first  # first occurrence kept


def test_dedupe_empty_list():
    assert _dedupe_flash_all_devices([]) == []


# ---------------------------------------------------------------------------
# Version-decision confirms route through the DecisionProvider (no input()).
# ---------------------------------------------------------------------------


class _FakeConfigManager:
    seeded_keys: set = set()  # tests mutate this per-case; reset in each test

    def __init__(self, key, klipper_dir):
        self.key = key
        self.cache_path = types.SimpleNamespace(exists=lambda: True)

    def is_seeded(self):
        return self.key in self.seeded_keys

    def load_cached_config(self):
        return True

    def validate_mcu(self, mcu):
        return (True, mcu)

    def get_cache_age_display(self):
        return ""


class _FakeRegistry:
    def __init__(self, data):
        self._data = data

    def load(self):
        return self._data


def _reach_version_stage(monkeypatch, *, outdated):
    """Monkeypatch flash_batch's collaborators so cmd_flash_all reaches the
    version-decision prompts with a single USB device that is either up-to-date
    (outdated=False) or outdated (outdated=True)."""
    monkeypatch.setattr(flash_batch, "preflight_build", lambda em, kd: True)
    monkeypatch.setattr(
        flash_batch, "moonraker_safety_gate", lambda **k: flash_steps.SafetyGate.PROCEED
    )
    monkeypatch.setattr(flash_batch, "build_blocked_list", lambda data: {})
    monkeypatch.setattr(flash_batch, "blocked_reason_for_entry", lambda e, bl: None)
    monkeypatch.setattr(flash_batch, "ConfigManager", _FakeConfigManager)
    monkeypatch.setattr(flash_batch, "get_host_klipper_version", lambda kd: "v0.12.0-100")
    monkeypatch.setattr(flash_batch, "get_mcu_versions", lambda: {"mcu": "v0.12.0-100"})
    monkeypatch.setattr(flash_batch, "get_mcu_canbus_map", lambda: None)
    monkeypatch.setattr(flash_batch, "detect_firmware_flavor", lambda v: "Klipper")
    monkeypatch.setattr(flash_batch, "get_mcu_version_for_device", lambda *a, **k: "v")
    monkeypatch.setattr(flash_batch, "is_mcu_outdated", lambda host, mcu: outdated)


def _registry_one_usb():
    entry = DeviceEntry(
        key="octo", name="Octopus", mcu="stm32h723", serial_pattern="usb-Klipper_x*"
    )
    data = RegistryData(
        global_config=GlobalConfig(klipper_dir="/tmp/k", katapult_dir="/tmp/kt"),
        devices={"octo": entry},
    )
    return _FakeRegistry(data)


def test_all_up_to_date_asks_older_versions_confirm_and_cancels(monkeypatch):
    _reach_version_stage(monkeypatch, outdated=False)
    em = Emitter(NullSink())
    # Proceed past the "Flash N device(s)?" gate, then decline "Flash anyway?".
    decider = FakeDecisionProvider(
        confirms={"flash_batch": True, "flash_all_older_versions": False}
    )

    rc = cmd_flash_all(_registry_one_usb(), em, decider)

    assert rc == 0  # cancelled -- firmware already current
    ids = [c.id for c in decider.confirm_calls]
    assert "flash_all_older_versions" in ids
    older = next(c for c in decider.confirm_calls if c.id == "flash_all_older_versions")
    assert older.default is False  # matches the old [y/N] default
    assert older.message == "Flash anyway?"


def test_all_up_to_date_confirm_true_proceeds_past_version_gate(monkeypatch):
    _reach_version_stage(monkeypatch, outdated=False)
    em = Emitter(NullSink())
    # Say yes to "Flash anyway?"; stop the run right after by failing the build.
    monkeypatch.setattr(
        flash_batch,
        "run_build",
        lambda *a, **k: types.SimpleNamespace(
            success=False, error_message="stop", error_output="", firmware_path=None
        ),
    )
    monkeypatch.setattr(flash_batch, "resolve_ccache_usage", lambda **k: False)
    monkeypatch.setattr(
        flash_batch, "check_dirty_repo", lambda v: types.SimpleNamespace(is_dirty=False)
    )
    decider = FakeDecisionProvider(
        confirms={"flash_batch": True, "flash_all_older_versions": True}
    )

    rc = cmd_flash_all(_registry_one_usb(), em, decider)

    # Build failed for the only device -> "All builds failed" -> rc 1, but we
    # got PAST the version gate, proving confirm(True) proceeds.
    assert rc == 1
    ids = [c.id for c in decider.confirm_calls]
    assert "flash_all_older_versions" in ids


# ---------------------------------------------------------------------------
# Seeded-but-unreviewed configs are skipped by Flash All (review gate parity)
# ---------------------------------------------------------------------------


def _registry_two_usb():
    a = DeviceEntry(key="octo", name="Octopus", mcu="stm32h723",
                    serial_pattern="usb-Klipper_x*")
    b = DeviceEntry(key="nite", name="Nitehawk", mcu="rp2040",
                    serial_pattern="usb-Klipper_y*")
    data = RegistryData(
        global_config=GlobalConfig(klipper_dir="/tmp/k", katapult_dir="/tmp/kt"),
        devices={"octo": a, "nite": b},
    )
    return _FakeRegistry(data)


def test_flash_all_skips_seeded_device_and_continues(monkeypatch):
    _reach_version_stage(monkeypatch, outdated=True)
    monkeypatch.setattr(_FakeConfigManager, "seeded_keys", {"nite"})
    sink = RecordingSink()
    em = Emitter(sink)
    # Cancel at the "Flash N device(s)?" confirm -- Stage 1 is what's under test.
    decider = FakeDecisionProvider(confirms={"flash_batch": False})

    rc = cmd_flash_all(_registry_two_usb(), em, decider)

    assert rc == 0  # cancelled at the batch confirm, not an error
    text = sink.text()
    assert "Skipping Nitehawk" in text
    assert "not reviewed" in text
    # The surviving device count excludes the seeded one.
    assert "1 device(s) validated" in text


def test_flash_all_all_seeded_errors(monkeypatch):
    _reach_version_stage(monkeypatch, outdated=True)
    monkeypatch.setattr(_FakeConfigManager, "seeded_keys", {"octo"})
    sink = RecordingSink()
    em = Emitter(sink)

    rc = cmd_flash_all(_registry_one_usb(), em, FakeDecisionProvider())

    assert rc == 1
    assert "Skipping Octopus" in sink.text()


def test_flash_all_unseeded_devices_unaffected(monkeypatch):
    # Regression guard: with no seeded keys, Stage 1 validates both devices.
    _reach_version_stage(monkeypatch, outdated=True)
    monkeypatch.setattr(_FakeConfigManager, "seeded_keys", set())
    sink = RecordingSink()
    em = Emitter(sink)
    decider = FakeDecisionProvider(confirms={"flash_batch": False})

    rc = cmd_flash_all(_registry_two_usb(), em, decider)

    assert rc == 0
    assert "Skipping" not in sink.text()
    assert "2 device(s) validated" in sink.text()
