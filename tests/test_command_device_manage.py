"""Tier-1 tests for commands.device_manage.

- ``cmd_remove_device`` against a real on-disk temp registry.
- ``cmd_list_devices`` must not crash on CAN devices (serial_pattern=None):
  the design §F guard (``if entry.serial_pattern is None: continue``) prevents
  the historical ``prefix_variants(None)`` AttributeError.
"""

from __future__ import annotations

from conftest import RecordingSink

from kflash.commands import device_manage
from kflash.commands.device_manage import (
    cmd_copy_config,
    cmd_list_devices,
    cmd_remove_device,
    cmd_save_config_as_default,
)
from kflash.config import ConfigManager, get_defaults_dir
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


def _seed_cache(key, klipper_dir, content='CONFIG_MCU="stm32h723xx"\n'):
    """Write a cached config directly (bypassing menuconfig) for test setup."""
    mgr = ConfigManager(key, str(klipper_dir))
    mgr.cache_path.parent.mkdir(parents=True, exist_ok=True)
    mgr.cache_path.write_text(content, encoding="utf-8")
    return mgr


def _registry_with_global(tmp_path):
    klipper_dir = tmp_path / "klipper"
    registry = Registry(str(tmp_path / "devices.json"))
    registry.save_global(GlobalConfig(klipper_dir=str(klipper_dir), katapult_dir=str(tmp_path)))
    return registry, klipper_dir


class TestCmdSaveConfigAsDefault:
    def test_happy_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        registry, klipper_dir = _registry_with_global(tmp_path)
        registry.add(_usb_entry("octo"))
        _seed_cache("octo", klipper_dir, 'CONFIG_MCU="stm32h723xx"\n')

        sink = RecordingSink()
        em = Emitter(sink)
        rc = cmd_save_config_as_default(registry, "octo", em, ScriptedDecider())

        assert rc == 0
        dst = get_defaults_dir() / "stm32h723.config"
        assert dst.exists()
        assert dst.read_text(encoding="utf-8") == 'CONFIG_MCU="stm32h723xx"\n'
        assert any(e.kind == "success" for e in sink.events)

    def test_no_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        registry, _klipper_dir = _registry_with_global(tmp_path)
        registry.add(_usb_entry("octo"))

        sink = RecordingSink()
        em = Emitter(sink)
        rc = cmd_save_config_as_default(registry, "octo", em, ScriptedDecider())

        assert rc == 1
        assert any(e.kind == "error_recovery" for e in sink.events)
        error_event = next(e for e in sink.events if e.kind == "error_recovery")
        assert "menuconfig" in (error_event.recovery or "").lower()

    def test_unknown_device(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        registry, _klipper_dir = _registry_with_global(tmp_path)

        rc = cmd_save_config_as_default(registry, "missing", _em(), ScriptedDecider())
        assert rc == 1

    def test_existing_default_requires_confirm(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        registry, klipper_dir = _registry_with_global(tmp_path)
        registry.add(_usb_entry("octo"))
        _seed_cache("octo", klipper_dir, "# new content\n")

        defaults_dir = get_defaults_dir()
        defaults_dir.mkdir(parents=True, exist_ok=True)
        (defaults_dir / "stm32h723.config").write_text("# old default\n", encoding="utf-8")

        decider = ScriptedDecider({"overwrite_mcu_default": False})
        rc = cmd_save_config_as_default(registry, "octo", _em(), decider)

        assert rc == 0
        assert "overwrite_mcu_default" in decider.seen
        assert (defaults_dir / "stm32h723.config").read_text(encoding="utf-8") == "# old default\n"

    def test_confirmed_overwrite_replaces_existing_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        registry, klipper_dir = _registry_with_global(tmp_path)
        registry.add(_usb_entry("octo"))
        _seed_cache("octo", klipper_dir, "# new content\n")

        defaults_dir = get_defaults_dir()
        defaults_dir.mkdir(parents=True, exist_ok=True)
        (defaults_dir / "stm32h723.config").write_text("# old default\n", encoding="utf-8")

        decider = ScriptedDecider({"overwrite_mcu_default": True})
        rc = cmd_save_config_as_default(registry, "octo", _em(), decider)

        assert rc == 0
        assert "overwrite_mcu_default" in decider.seen
        assert (defaults_dir / "stm32h723.config").read_text(encoding="utf-8") == "# new content\n"

    def test_no_global_config(self, tmp_path, monkeypatch):
        """Fresh-install state: registry has the device but no global config."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        registry, klipper_dir = _registry_with_global(tmp_path)
        registry.add(_usb_entry("octo"))
        _seed_cache("octo", klipper_dir)

        data = registry.load()
        data.global_config = None
        monkeypatch.setattr(registry, "load", lambda: data)

        sink = RecordingSink()
        rc = cmd_save_config_as_default(registry, "octo", Emitter(sink), ScriptedDecider())

        assert rc == 1
        assert any(e.kind == "error_recovery" for e in sink.events)


class TestCmdCopyConfig:
    def test_overwrite_requires_confirm(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        registry, klipper_dir = _registry_with_global(tmp_path)
        registry.add(_usb_entry("src"))
        registry.add(_usb_entry("dst"))
        _seed_cache("src", klipper_dir, "# src config\n")
        dst_mgr = _seed_cache("dst", klipper_dir, "# dst config\n")

        decider = ScriptedDecider({"overwrite_config_copy": False})
        rc = cmd_copy_config(registry, "src", "dst", _em(), decider)

        assert rc == 0
        assert "overwrite_config_copy" in decider.seen
        # Declined -- destination cache must be untouched, not marked seeded.
        assert dst_mgr.cache_path.read_text(encoding="utf-8") == "# dst config\n"
        assert not dst_mgr.is_seeded()

    def test_marks_seeded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        registry, klipper_dir = _registry_with_global(tmp_path)
        registry.add(_usb_entry("src"))
        registry.add(_usb_entry("dst"))
        _seed_cache("src", klipper_dir, "# src config\n")

        decider = ScriptedDecider()
        rc = cmd_copy_config(registry, "src", "dst", _em(), decider)

        assert rc == 0
        dst_mgr = ConfigManager("dst", str(klipper_dir))
        assert dst_mgr.is_seeded()
        assert dst_mgr.seed_source() == "device:src"
        assert dst_mgr.cache_path.read_text(encoding="utf-8") == "# src config\n"

    def test_no_source_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        registry, _klipper_dir = _registry_with_global(tmp_path)
        registry.add(_usb_entry("src"))
        registry.add(_usb_entry("dst"))

        sink = RecordingSink()
        em = Emitter(sink)
        rc = cmd_copy_config(registry, "src", "dst", em, ScriptedDecider())

        assert rc == 1
        assert any(e.kind == "error_recovery" for e in sink.events)

    def test_self_copy_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        registry, klipper_dir = _registry_with_global(tmp_path)
        registry.add(_usb_entry("octo"))
        mgr = _seed_cache("octo", klipper_dir, "# octo config\n")

        sink = RecordingSink()
        em = Emitter(sink)
        decider = ScriptedDecider()
        rc = cmd_copy_config(registry, "octo", "octo", em, decider)

        assert rc == 1
        assert any(e.kind == "error" for e in sink.events)
        # No confirm asked, cache untouched, not re-marked seeded.
        assert decider.seen == []
        assert mgr.cache_path.read_text(encoding="utf-8") == "# octo config\n"
        assert not mgr.is_seeded()

    def test_no_global_config(self, tmp_path, monkeypatch):
        """Fresh-install state: registry has both devices but no global config."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        registry, klipper_dir = _registry_with_global(tmp_path)
        registry.add(_usb_entry("src"))
        registry.add(_usb_entry("dst"))
        _seed_cache("src", klipper_dir)

        data = registry.load()
        data.global_config = None
        monkeypatch.setattr(registry, "load", lambda: data)

        sink = RecordingSink()
        rc = cmd_copy_config(registry, "src", "dst", Emitter(sink), ScriptedDecider())

        assert rc == 1
        assert any(e.kind == "error_recovery" for e in sink.events)

    def test_unknown_devices(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        registry, _klipper_dir = _registry_with_global(tmp_path)
        registry.add(_usb_entry("src"))

        rc = cmd_copy_config(registry, "src", "missing", _em(), ScriptedDecider())
        assert rc == 1

        rc2 = cmd_copy_config(registry, "missing", "src", _em(), ScriptedDecider())
        assert rc2 == 1
