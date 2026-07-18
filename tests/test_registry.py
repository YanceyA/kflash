"""Registry tests: the menuconfig_before_flash setting + legacy-key migration.

The global setting was renamed from ``skip_menuconfig`` (default False) to the
positive ``menuconfig_before_flash`` (default True) per hardware feedback: the
flash flow prompts for menuconfig by default, and the toggle that disables the
prompt reads as "menuconfig before flash: off" rather than a double negative.
Existing devices.json files carry the old key, so load() must migrate it.
"""

from __future__ import annotations

import json

from kflash.registry import Registry


def _registry_with_global(tmp_path, global_dict: dict) -> Registry:
    path = tmp_path / "devices.json"
    path.write_text(
        json.dumps({"global": global_dict, "devices": {}, "blocked_devices": []}),
        encoding="utf-8",
    )
    return Registry(str(path))


def test_menuconfig_before_flash_defaults_on(tmp_path) -> None:
    registry = _registry_with_global(tmp_path, {})
    assert registry.load_global().menuconfig_before_flash is True


def test_legacy_skip_menuconfig_true_migrates_to_gate_off(tmp_path) -> None:
    registry = _registry_with_global(tmp_path, {"skip_menuconfig": True})
    assert registry.load_global().menuconfig_before_flash is False


def test_legacy_skip_menuconfig_false_migrates_to_gate_on(tmp_path) -> None:
    registry = _registry_with_global(tmp_path, {"skip_menuconfig": False})
    assert registry.load_global().menuconfig_before_flash is True


def test_new_key_wins_over_legacy_key(tmp_path) -> None:
    registry = _registry_with_global(
        tmp_path, {"skip_menuconfig": False, "menuconfig_before_flash": False}
    )
    assert registry.load_global().menuconfig_before_flash is False


def test_save_writes_new_key_and_drops_legacy_key(tmp_path) -> None:
    registry = _registry_with_global(tmp_path, {"skip_menuconfig": True})
    registry.save(registry.load())
    raw = json.loads((tmp_path / "devices.json").read_text(encoding="utf-8"))
    assert raw["global"]["menuconfig_before_flash"] is False
    assert "skip_menuconfig" not in raw["global"]


def test_device_board_round_trips_through_save_and_load(tmp_path) -> None:
    from kflash.models import DeviceEntry

    path = tmp_path / "devices.json"
    registry = Registry(str(path))
    registry.add(
        DeviceEntry(
            key="octopus-pro",
            name="Octopus Pro v1.1",
            mcu="stm32h723",
            board="btt-octopus-pro-v1.1",
        )
    )
    reloaded = registry.load()
    assert reloaded.devices["octopus-pro"].board == "btt-octopus-pro-v1.1"


def test_device_without_board_key_loads_as_none(tmp_path) -> None:
    path = tmp_path / "devices.json"
    path.write_text(
        json.dumps(
            {
                "global": {},
                "devices": {
                    "octopus-pro": {
                        "name": "Octopus Pro v1.1",
                        "mcu": "stm32h723",
                    }
                },
                "blocked_devices": [],
            }
        ),
        encoding="utf-8",
    )
    registry = Registry(str(path))
    reloaded = registry.load()
    assert reloaded.devices["octopus-pro"].board is None
