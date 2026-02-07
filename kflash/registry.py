"""Device registry backed by devices.json."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import fields
from pathlib import Path
from typing import Optional

from .errors import RegistryError
from .models import BlockedDevice, DeviceEntry, GlobalConfig, RegistryData

_UPDATABLE_DEVICE_FIELDS: frozenset[str] = frozenset(
    field.name for field in fields(DeviceEntry) if field.name != "key"
)


class Registry:
    """Device registry with JSON CRUD and atomic writes."""

    def __init__(self, registry_path: str):
        self.path = registry_path

    def load(self) -> RegistryData:
        """Load registry from disk. Returns default if file missing."""
        p = Path(self.path)
        if not p.exists():
            return RegistryData()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise RegistryError(f"Corrupt registry file: {e}") from e

        global_raw = raw.get("global", {})
        global_config = GlobalConfig(
            klipper_dir=global_raw.get("klipper_dir", "~/klipper"),
            katapult_dir=global_raw.get("katapult_dir", "~/katapult"),
            skip_menuconfig=global_raw.get("skip_menuconfig", False),
            stagger_delay=global_raw.get("stagger_delay", 2.0),
            return_delay=global_raw.get("return_delay", 5.0),
            use_ccache=global_raw.get("use_ccache", False),
            ccache_install_declined=global_raw.get("ccache_install_declined", False),
            can_stagger_delay=global_raw.get("can_stagger_delay", 5.0),
            can_scan_on_refresh=global_raw.get("can_scan_on_refresh", False),
        )
        devices: dict[str, DeviceEntry] = {}
        for key, data in raw.get("devices", {}).items():
            if not isinstance(data, dict):
                raise RegistryError(f"Device '{key}' entry must be a JSON object")
            try:
                devices[key] = DeviceEntry(
                    key=key,
                    name=data["name"],
                    mcu=data["mcu"],
                    serial_pattern=data.get("serial_pattern"),
                    flash_command=data.get("flash_command") or data.get("flash_method"),
                    bootloader_method=data.get("bootloader_method"),
                    canbus_uuid=data.get("canbus_uuid"),
                    canbus_interface=data.get("canbus_interface"),
                    bootloader_baud=data.get("bootloader_baud"),
                    uf2_mount_path=data.get("uf2_mount_path"),
                    sdcard_board=data.get("sdcard_board"),
                    mcu_name=data.get("mcu_name"),
                    flashable=data.get("flashable", True),  # Default to True if missing
                    notes=data.get("notes"),
                    role=data.get("role"),
                    last_flash_timestamp=data.get("last_flash_timestamp"),
                )
            except KeyError as exc:
                missing_field = exc.args[0]
                raise RegistryError(
                    f"Device '{key}' missing required field: '{missing_field}'"
                ) from exc

        blocked_devices: list[BlockedDevice] = []
        for item in raw.get("blocked_devices", []):
            if isinstance(item, str):
                blocked_devices.append(BlockedDevice(pattern=item))
                continue
            if isinstance(item, dict):
                pattern = item.get("pattern") or item.get("serial_pattern")
                if pattern:
                    blocked_devices.append(
                        BlockedDevice(
                            pattern=pattern,
                            reason=item.get("reason"),
                        )
                    )

        return RegistryData(
            global_config=global_config,
            devices=devices,
            blocked_devices=blocked_devices,
        )

    def save(self, registry: RegistryData) -> None:
        """Save registry to disk atomically."""
        data = {
            "global": {
                "klipper_dir": registry.global_config.klipper_dir,
                "katapult_dir": registry.global_config.katapult_dir,
                "skip_menuconfig": registry.global_config.skip_menuconfig,
                "stagger_delay": registry.global_config.stagger_delay,
                "return_delay": registry.global_config.return_delay,
                "use_ccache": registry.global_config.use_ccache,
                "ccache_install_declined": registry.global_config.ccache_install_declined,
                "can_stagger_delay": registry.global_config.can_stagger_delay,
                "can_scan_on_refresh": registry.global_config.can_scan_on_refresh,
            },
            "devices": {},
            "blocked_devices": [],
        }
        for key, device in sorted(registry.devices.items()):
            data["devices"][key] = {
                "name": device.name,
                "mcu": device.mcu,
                "serial_pattern": device.serial_pattern,
                "flash_command": device.flash_command,
                "bootloader_method": device.bootloader_method,
                "canbus_uuid": device.canbus_uuid,
                "canbus_interface": device.canbus_interface,
                "bootloader_baud": device.bootloader_baud,
                "uf2_mount_path": device.uf2_mount_path,
                "sdcard_board": device.sdcard_board,
                "flashable": device.flashable,
            }
            if device.mcu_name is not None:
                data["devices"][key]["mcu_name"] = device.mcu_name
            if device.notes is not None:
                data["devices"][key]["notes"] = device.notes
            if device.role is not None:
                data["devices"][key]["role"] = device.role
            if device.last_flash_timestamp is not None:
                data["devices"][key]["last_flash_timestamp"] = device.last_flash_timestamp
        for blocked in registry.blocked_devices:
            entry = {"pattern": blocked.pattern}
            if blocked.reason:
                entry["reason"] = blocked.reason
            data["blocked_devices"].append(entry)
        _atomic_write_json(self.path, data)

    def add(self, entry: DeviceEntry) -> None:
        """Add a device to the registry. Raises RegistryError if key exists."""
        registry = self.load()
        if entry.key in registry.devices:
            raise RegistryError(f"Device '{entry.key}' already registered")
        registry.devices[entry.key] = entry
        self.save(registry)

    def remove(self, key: str) -> bool:
        """Remove a device from the registry. Returns False if not found."""
        registry = self.load()
        if key not in registry.devices:
            return False
        del registry.devices[key]
        self.save(registry)
        return True

    def get(self, key: str) -> Optional[DeviceEntry]:
        """Get a device by key. Returns None if not found."""
        registry = self.load()
        return registry.devices.get(key)

    def list_all(self) -> list:
        """List all registered devices."""
        registry = self.load()
        return list(registry.devices.values())

    def load_global(self) -> GlobalConfig:
        """Load global configuration."""
        registry = self.load()
        return registry.global_config

    def save_global(self, config: GlobalConfig) -> None:
        """Update global configuration."""
        registry = self.load()
        registry.global_config = config
        self.save(registry)

    def update_device(self, key: str, **updates) -> bool:
        """Update fields on a registered device. Returns False if key not found.

        Uses load-modify-save pattern for atomic persistence.
        Valid fields: all DeviceEntry fields except key.
        """
        registry = self.load()
        if key not in registry.devices:
            return False

        invalid_fields = sorted(field for field in updates if field not in _UPDATABLE_DEVICE_FIELDS)
        if invalid_fields:
            raise RegistryError(
                "Invalid device field(s): " + ", ".join(invalid_fields)
            )

        device = registry.devices[key]
        for field, value in updates.items():
            setattr(device, field, value)
        self.save(registry)
        return True

    def set_flashable(self, key: str, flashable: bool) -> bool:
        """Set flashable status for a device. Returns False if device not found."""
        registry = self.load()
        if key not in registry.devices:
            return False
        registry.devices[key].flashable = flashable
        self.save(registry)
        return True


def _atomic_write_json(path: str, data: dict) -> None:
    """Write JSON atomically: write to temp file, rename.

    No fsync -- on Raspberry Pi SD cards, fsync triggers an ext4 journal
    commit that can stall for 30+ seconds after heavy I/O (firmware builds).
    The atomic rename provides sufficient consistency for registry data.
    """
    dir_name = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_name, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=dir_name, delete=False, suffix=".tmp", encoding="utf-8"
    ) as tf:
        tmp_path = tf.name
        try:
            json.dump(data, tf, indent=2, sort_keys=True)
            tf.write("\n")
            tf.flush()
        except BaseException:
            os.unlink(tmp_path)
            raise
    os.replace(tmp_path, path)
