"""UKAM safety functions for kalico-flash."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass
class DirtyResult:
    """Result of dirty repository check."""

    is_dirty: bool
    version: str | None = None


@dataclass
class DowngradeResult:
    """Result of firmware downgrade detection."""

    is_downgrade: bool
    host_version: str | None = None
    mcu_version: str | None = None


def check_not_root() -> None:
    """Exit if running as root on Linux. No-op on Windows."""
    if os.name != "posix" or not hasattr(os, "geteuid"):
        return
    if os.geteuid() == 0:
        raise SystemExit("Error: Running as root can damage system files. Run as your normal user.")


def check_dirty_repo(version: str | None) -> DirtyResult:
    """Check if the Klipper repo has uncommitted changes."""
    if version is None:
        return DirtyResult(is_dirty=False)
    return DirtyResult(is_dirty=version.endswith("-dirty"), version=version)


def should_restart_service(was_active: bool) -> bool:
    """Determine whether to restart the Klipper service."""
    return was_active


def should_block_on_printer_state(state: str) -> bool:
    """Determine whether the printer state should block flashing."""
    return state in {"startup", "printing", "paused"}


def detect_downgrade(host_version: str, mcu_version: str) -> DowngradeResult:
    """Detect if flashing would downgrade MCU firmware."""

    def _parse(v: str) -> tuple[int, ...]:
        # Supports both Kalico date tags and Klipper semver tags with optional
        # git-describe suffix, e.g.:
        #   v2025.01.15-45-g7ce409d
        #   v0.12.0-45-g7ce409d-dirty
        #   v2025.01.15
        text = v.strip()
        match = re.match(
            r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9]+)-g[0-9a-fA-F]+)?(?:-dirty)?$",
            text,
        )
        if not match:
            raise ValueError(f"Unsupported version format: {v}")
        major = int(match.group(1))
        minor = int(match.group(2))
        patch = int(match.group(3))
        commit_count = int(match.group(4)) if match.group(4) is not None else 0
        return (major, minor, patch, commit_count)

    host_tuple = _parse(host_version)
    mcu_tuple = _parse(mcu_version)
    return DowngradeResult(
        is_downgrade=mcu_tuple > host_tuple,
        host_version=host_version,
        mcu_version=mcu_version,
    )


def discover_python_path(venv_dir: str) -> str | None:
    """Find python binary in a virtualenv's bin directory."""
    candidate = os.path.join(venv_dir, "bin", "python3")
    if os.path.isfile(candidate):
        return candidate
    return None


def resolve_registry_path() -> str:
    """Resolve XDG-compliant path for the device registry."""
    env_path = os.environ.get("KALICO_REGISTRY_PATH")
    if env_path:
        return env_path
    config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(config_home, "kalico-flash", "devices.json")
