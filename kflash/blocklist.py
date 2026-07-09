"""Device block-policy helpers (pure engine, no UI).

Determines which discovered/registered devices are excluded from flashing,
combining a built-in default blocklist with user-configured patterns from the
registry. Imported by both the engine flash paths and the TUI screen builder.
"""

from __future__ import annotations

import fnmatch

from .discovery import SUPPORTED_PREFIXES

DEFAULT_BLOCKED_DEVICES = [
    ("usb-beacon_*", "Beacon probe (not a Klipper MCU)"),
]


def normalize_pattern(pattern: str) -> str:
    return pattern.strip().lower()


def build_blocked_list(registry_data) -> list[tuple[str, str | None]]:
    blocked: list[tuple[str, str | None]] = [
        (pattern, reason) for pattern, reason in DEFAULT_BLOCKED_DEVICES
    ]
    for entry in getattr(registry_data, "blocked_devices", []):
        blocked.append((entry.pattern, entry.reason))
    return blocked


def blocked_reason_for_filename(
    filename: str, blocked_list: list[tuple[str, str | None]]
) -> str | None:
    name = filename.lower()
    for pattern, reason in blocked_list:
        if fnmatch.fnmatch(name, normalize_pattern(pattern)):
            return reason or "Blocked by policy"
    return None


def blocked_reason_for_entry(entry, blocked_list: list[tuple[str, str | None]]) -> str | None:
    # CAN devices have no serial_pattern -- they cannot match USB blocked patterns
    if entry.serial_pattern is None:
        return None
    serial_pattern = entry.serial_pattern.lower()
    for pattern, reason in blocked_list:
        normalized = normalize_pattern(pattern)
        if fnmatch.fnmatch(serial_pattern, normalized) or fnmatch.fnmatch(
            normalized, serial_pattern
        ):
            return reason or "Blocked by policy"
    if not any(serial_pattern.startswith(prefix) for prefix in SUPPORTED_PREFIXES):
        return "Unsupported USB device"
    return None
