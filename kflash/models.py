"""Dataclass contracts for cross-module data exchange."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GlobalConfig:
    """Global settings shared across all devices."""

    klipper_dir: str = "~/klipper"
    katapult_dir: str = "~/katapult"
    skip_menuconfig: bool = False
    stagger_delay: float = 2.0
    return_delay: float = 5.0
    use_ccache: bool = False  # Enable ccache build acceleration
    ccache_install_declined: bool = False  # User declined ccache installation prompt
    can_stagger_delay: float = 5.0  # CAN-specific stagger delay for Flash All
    can_scan_on_refresh: bool = False  # CAN bus scan on TUI startup/refresh (default OFF)


@dataclass
class DeviceEntry:
    """A registered device in the registry."""

    key: str  # "octopus-pro" (auto-generated slug from display name)
    name: str  # "Octopus Pro v1.1" (display name)
    mcu: str  # "stm32h723" (extracted from serial path)
    serial_pattern: Optional[str] = None  # "usb-Klipper_stm32h723xx_29001A*" (None for CAN devices)
    flash_command: Optional[str] = None  # "katapult", "make_flash", "flash_sdcard", "uf2_mount"
    bootloader_method: Optional[str] = None  # "usb", "serial", "manual", "none", "can"
    canbus_uuid: Optional[str] = None  # 12-char hex CAN bus UUID
    canbus_interface: Optional[str] = None  # CAN interface name (e.g., "can0")
    bootloader_baud: Optional[int] = None  # Baud rate for serial bootloader entry
    uf2_mount_path: Optional[str] = None  # Mount path for UF2 firmware upload
    sdcard_board: Optional[str] = None  # Board name for flash_sdcard command
    mcu_name: Optional[str] = None  # Klipper MCU object name (e.g., "mcu", "mcu nhk")
    flashable: bool = True  # Non-flashable devices excluded from flash selection
    notes: Optional[str] = None  # Free-form user notes
    role: Optional[str] = None  # "toolhead" or "bridge" (CAN Flash All ordering)
    last_flash_timestamp: Optional[str] = None  # ISO format: "2026-02-06T14:30:00"

    @property
    def is_can_device(self) -> bool:
        """True if this device uses CAN bus transport."""
        return self.canbus_uuid is not None


@dataclass
class BlockedDevice:
    """A device pattern that should be blocked from add/flash flows."""

    pattern: str
    reason: Optional[str] = None


@dataclass
class DiscoveredDevice:
    """A USB serial device found during scanning."""

    path: str  # "/dev/serial/by-id/usb-Klipper_stm32h723xx_..."
    filename: str  # "usb-Klipper_stm32h723xx_29001A001151313531383332-if00"


@dataclass
class DiscoveredCanDevice:
    """A CAN bus device found during scanning."""

    uuid: str  # 12-char hex CAN bus UUID
    application: str  # Raw application string: "Klipper", "Katapult", "Unknown"


@dataclass
class RegistryData:
    """Complete registry file contents."""

    global_config: GlobalConfig = field(default_factory=GlobalConfig)
    devices: dict = field(default_factory=dict)  # key -> DeviceEntry
    blocked_devices: list[BlockedDevice] = field(default_factory=list)


@dataclass
class BuildResult:
    """Result of a firmware build."""

    success: bool
    firmware_path: Optional[str] = None  # Path to klipper.bin if success
    firmware_size: int = 0  # Size in bytes if success
    elapsed_seconds: float = 0.0  # Build duration
    error_message: Optional[str] = None  # Error details if failed
    error_output: Optional[str] = None  # Captured build output on failure
    ccache_stats: Optional[CcacheStats] = None  # Stats if ccache was used


@dataclass
class FlashResult:
    """Result of a flash operation."""

    success: bool
    method: str  # "katapult" or "make_flash"
    elapsed_seconds: float = 0.0
    error_message: Optional[str] = None


@dataclass
class BatchDeviceResult:
    """Per-device result tracking for Flash All batch operations."""

    device_key: str
    device_name: str
    config_ok: bool = False
    build_ok: bool = False
    bootloader_ok: bool = False  # Track bootloader phase status
    flash_ok: bool = False
    verify_ok: bool = False
    error_message: Optional[str] = None
    error_output: Optional[str] = None  # Captured build output on failure
    skipped: bool = False  # User chose to skip (version match)
    firmware_name: str = "klipper.bin"  # Firmware filename (klipper.bin or klipper.uf2)
    ccache_hit_rate: Optional[float] = None  # Cache hit rate if ccache was used
    ccache_stats: Optional[CcacheStats] = None  # Per-build cache stats if available


@dataclass
class PrintStatus:
    """Current print job status from Moonraker."""

    state: str  # standby, printing, paused, complete, error, cancelled
    filename: Optional[str]  # None if no file loaded
    progress: float  # 0.0 to 1.0


@dataclass
class KatapultCheckResult:
    """Result of a Katapult bootloader detection check.

    has_katapult is tri-state:
      True  - Katapult bootloader detected (katapult_ device appeared)
      False - No Katapult (device entered DFU/BOOTSEL, recovered via USB reset)
      None  - Inconclusive (error during check, device state unknown)
    """

    has_katapult: Optional[bool]  # True/False/None tri-state
    error_message: Optional[str] = None  # Details when None or False
    elapsed_seconds: float = 0.0


@dataclass
class BootloaderResult:
    """Result of bootloader entry operation."""

    success: bool
    device_path: Optional[str] = None
    error_message: Optional[str] = None
    elapsed_seconds: float = 0.0


@dataclass
class CcacheStats:
    """ccache statistics after a build."""

    cache_hit_direct: int = 0
    cache_hit_preprocessed: int = 0
    cache_miss: int = 0
    cache_size_bytes: int = 0
    cache_max_bytes: int = 0

    @property
    def total_hits(self) -> int:
        """Total cache hits (direct + preprocessed)."""
        return self.cache_hit_direct + self.cache_hit_preprocessed

    @property
    def total_calls(self) -> int:
        """Total compilation calls (hits + misses)."""
        return self.total_hits + self.cache_miss

    @property
    def hit_rate(self) -> float:
        """Cache hit rate as a fraction (0.0 to 1.0)."""
        if self.total_calls == 0:
            return 0.0
        return self.total_hits / self.total_calls

    def format_line(self) -> str:
        """Format stats as single line for build output.

        Example: "ccache: 142 hits, 3 misses (98% hit rate), cache: 45MB / 5.0GB max"
        """
        hits = self.total_hits
        misses = self.cache_miss
        rate_pct = int(self.hit_rate * 100)
        size_mb = self.cache_size_bytes / (1024 * 1024)
        max_gb = self.cache_max_bytes / (1024 * 1024 * 1024)
        stats = f"ccache: {hits} hits, {misses} misses ({rate_pct}% hit rate)"
        return f"{stats}, cache: {size_mb:.0f}MB / {max_gb:.1f}GB max"
