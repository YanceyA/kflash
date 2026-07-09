"""Tier-1 tests for commands.device_manage.

- ``cmd_remove_device`` against a real on-disk temp registry.
- ``cmd_list_devices`` must not crash on CAN devices (serial_pattern=None):
  the design §F guard (``if entry.serial_pattern is None: continue``) prevents
  the historical ``prefix_variants(None)`` AttributeError.
"""

from __future__ import annotations

from kflash.commands import device_manage
from kflash.commands.device_manage import cmd_list_devices, cmd_remove_device
from kflash.decisions import ConfirmDecision
from kflash.events import Emitter, NullSink
from kflash.models import DeviceEntry, DiscoveredDevice, GlobalConfig
from kflash.registry import Registry


class ScriptedDecider:
    """Minimal DecisionProvider stub: confirm() returns scripted answers keyed
    by ConfirmDecision.id (default True)."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.seen = []

    def confirm(self, req: ConfirmDecision) -> bool:
        self.seen.append(req.id)
        return self.answers.get(req.id, True)


def _em():
    return Emitter(NullSink())


def _usb_entry(key="octo"):
    return DeviceEntry(
        key=key,
        name="Octopus",
        mcu="stm32h723",
        serial_pattern="usb-Klipper_stm32h723xx_ABC*",
        flash_command="katapult",
        bootloader_method="usb",
    )


def _can_entry(key="toolhead"):
    return DeviceEntry(
        key=key,
        name="Toolhead",
        mcu="stm32h723",
        canbus_uuid="112233445566",
        canbus_interface="can0",
        flash_command="katapult_can",
        bootloader_method="can",
    )


def test_cmd_remove_device_removes_from_registry(tmp_path, monkeypatch):
    # Isolate cached-config lookups to a throwaway XDG dir (nothing cached).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    registry = Registry(str(tmp_path / "devices.json"))
    registry.save_global(GlobalConfig(klipper_dir=str(tmp_path), katapult_dir=str(tmp_path)))
    registry.add(_usb_entry("octo"))
    assert registry.get("octo") is not None

    decider = ScriptedDecider({"remove_device": True})
    rc = cmd_remove_device(registry, "octo", _em(), decider)

    assert rc == 0
    assert registry.get("octo") is None
    assert "remove_device" in decider.seen


def test_cmd_remove_device_cancelled_keeps_device(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    registry = Registry(str(tmp_path / "devices.json"))
    registry.save_global(GlobalConfig(klipper_dir=str(tmp_path), katapult_dir=str(tmp_path)))
    registry.add(_usb_entry("octo"))

    decider = ScriptedDecider({"remove_device": False})
    rc = cmd_remove_device(registry, "octo", _em(), decider)

    assert rc == 0
    assert registry.get("octo") is not None  # not removed


def test_cmd_remove_device_unknown_key(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    registry = Registry(str(tmp_path / "devices.json"))
    rc = cmd_remove_device(registry, "missing", _em(), ScriptedDecider())
    assert rc == 1


def test_cmd_list_devices_can_guard_no_crash(tmp_path, monkeypatch):
    """A CAN device (serial_pattern=None) must not crash the list command."""
    registry = Registry(str(tmp_path / "devices.json"))
    registry.save_global(GlobalConfig(klipper_dir=str(tmp_path), katapult_dir=str(tmp_path)))
    registry.add(_can_entry("toolhead"))
    registry.add(_usb_entry("octo"))

    # Return a live USB device so the cross-reference loop actually runs against
    # both the USB entry and the CAN entry (the CAN one has serial_pattern=None).
    usb = DiscoveredDevice(
        path="/dev/serial/by-id/usb-Klipper_stm32h723xx_ABC-if00",
        filename="usb-Klipper_stm32h723xx_ABC-if00",
    )
    monkeypatch.setattr(device_manage, "scan_serial_devices", lambda: [usb])
    monkeypatch.setattr(device_manage, "get_mcu_versions", lambda: None)
    monkeypatch.setattr(device_manage, "get_host_klipper_version", lambda _dir: None)

    # Would raise AttributeError from prefix_variants(None) without the guard.
    rc = cmd_list_devices(registry, _em())
    assert rc == 0
