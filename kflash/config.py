"""Config file management: caching, MCU parsing, atomic operations."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from .errors import ConfigError, format_error


def xdg_base() -> Path:
    """Resolve the XDG config base directory.

    Respects XDG_CONFIG_HOME if set and absolute, else ~/.config.
    Public: shared by boards.py for the user boards dir.
    """
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config and os.path.isabs(xdg_config):
        return Path(xdg_config)
    return Path.home() / ".config"


def get_config_dir(device_key: str) -> Path:
    """Get XDG config directory for a device.

    Returns path to ~/.config/kalico-flash/configs/{device-key}/
    Respects XDG_CONFIG_HOME if set and absolute.
    """
    return xdg_base() / "kalico-flash" / "configs" / device_key


def get_defaults_dir() -> Path:
    """XDG directory for default seed configs: ~/.config/kalico-flash/defaults/."""
    return xdg_base() / "kalico-flash" / "defaults"


def default_config_path(mcu: str) -> Path:
    """Path to an MCU's default seed config: ``defaults/<mcu>.config``.

    Single source of truth for this derivation (seed load, save-as-default,
    and the device-manage command all build the same path).
    """
    return get_defaults_dir() / f"{mcu}.config"


def rename_device_config_cache(old_key: str, new_key: str) -> bool:
    """Rename a device's config cache directory.

    Returns True if cache was moved, False if no cache existed for old_key.
    Raises FileExistsError if new_key cache already exists.
    Uses shutil.move for cross-filesystem safety.
    """
    old_dir = get_config_dir(old_key)
    new_dir = get_config_dir(new_key)

    if not old_dir.exists():
        return False

    if new_dir.exists():
        raise FileExistsError(f"Config cache for '{new_key}' already exists")

    new_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_dir), str(new_dir))
    return True


def parse_mcu_from_config(config_path: str) -> Optional[str]:
    """Extract MCU type from .config file.

    Returns e.g., 'stm32h723xx', 'rp2040', or None if not found.
    """
    path = Path(config_path)
    if not path.exists():
        return None

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    # Match: CONFIG_MCU="stm32h723xx"
    match = re.search(r'^CONFIG_MCU="([^"]+)"', content, re.MULTILINE)
    if match:
        return match.group(1)

    # Fallback: CONFIG_BOARD_DIRECTORY="rp2040" (some archs have no CONFIG_MCU)
    match = re.search(r'^CONFIG_BOARD_DIRECTORY="([^"]+)"', content, re.MULTILINE)
    return match.group(1) if match else None


def _atomic_copy(src: str, dst: str) -> None:
    """Copy file atomically: copy to temp, rename.

    Creates destination directory if needed.
    Cleans up temp file on failure.

    Note: no fsync -- on Raspberry Pi SD cards, fsync triggers an ext4
    journal commit that blocks while *all* pending dirty pages are flushed.
    After a firmware build this can stall for 30+ seconds.  The atomic
    rename already provides sufficient consistency for config-file caching.
    """
    dst_dir = os.path.dirname(os.path.abspath(dst))
    os.makedirs(dst_dir, exist_ok=True)

    with tempfile.NamedTemporaryFile(mode="wb", dir=dst_dir, delete=False, suffix=".tmp") as tf:
        tmp_path = tf.name
        try:
            with open(src, "rb") as sf:
                shutil.copyfileobj(sf, tf)
            tf.flush()
        except BaseException:
            os.unlink(tmp_path)
            raise
    os.replace(tmp_path, dst)


class ConfigManager:
    """Manage per-device Klipper .config caching.

    Handles:
    - Loading cached config to klipper directory
    - Saving klipper config to cache after menuconfig
    - Validating MCU type matches device registry
    """

    def __init__(self, device_key: str, klipper_dir: str):
        """Initialize config manager.

        Args:
            device_key: Device identifier (used for cache path)
            klipper_dir: Path to klipper source directory
        """
        self.device_key = device_key
        self.klipper_dir = Path(klipper_dir).expanduser()
        self.cache_path = get_config_dir(device_key) / ".config"
        self.klipper_config_path = self.klipper_dir / ".config"
        self.seed_marker_path = get_config_dir(device_key) / ".seeded"

    def load_cached_config(self) -> bool:
        """Load cached config to klipper directory.

        Returns True if cached config was copied.
        Returns False if no cached config exists.
        Creates klipper directory if needed.
        """
        if not self.cache_path.exists():
            return False

        # Ensure klipper directory exists
        self.klipper_dir.mkdir(parents=True, exist_ok=True)

        _atomic_copy(str(self.cache_path), str(self.klipper_config_path))
        return True

    def clear_klipper_config(self) -> bool:
        """Remove .config from klipper directory for fresh menuconfig.

        Returns True if file was removed, False if it didn't exist.
        """
        if self.klipper_config_path.exists():
            self.klipper_config_path.unlink()
            return True
        return False

    def save_cached_config(self) -> None:
        """Save klipper config to cache.

        Raises ConfigError if klipper .config doesn't exist.
        """
        if not self.klipper_config_path.exists():
            msg = format_error(
                "Config error",
                "No .config file found after menuconfig",
                context={"path": str(self.klipper_dir)},
                recovery=(
                    "1. Run make menuconfig first\n"
                    "2. Save config before exiting menuconfig\n"
                    f"3. Check path: ls {self.klipper_dir}/.config"
                ),
            )
            raise ConfigError(msg)

        _atomic_copy(str(self.klipper_config_path), str(self.cache_path))

        # A save always follows a menuconfig round-trip: the user has now
        # reviewed whatever was seeded, so the review-required marker lifts.
        if self.seed_marker_path.exists():
            try:
                self.seed_marker_path.unlink()
            except OSError:
                pass

    def is_seeded(self) -> bool:
        """True when the cached config was seeded and not yet reviewed in menuconfig."""
        return self.seed_marker_path.exists()

    def seed_source(self) -> Optional[str]:
        """The seed source label ('board:x', 'mcu-default:x', 'device:x'), or None.

        The label is the marker's FIRST line only; board seeds record the
        fragment's ``CONFIG_`` lines on subsequent lines (see
        :meth:`seed_fragment_lines`).
        """
        if not self.seed_marker_path.exists():
            return None
        try:
            lines = self.seed_marker_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        label = lines[0].strip() if lines else ""
        return label or None

    def seed_fragment_lines(self) -> list[str]:
        """The seed fragment's recorded ``CONFIG_`` lines (marker lines after the label).

        Only board seeds record these (a minimal 2-10 line board fragment); MCU
        defaults and device copies record none, and markers written by earlier
        versions hold only the label -- all three return ``[]``. Used by the
        fragment-survival drift check to detect symbols an upstream Kconfig
        rename silently dropped on load.
        """
        if not self.seed_marker_path.exists():
            return []
        try:
            lines = self.seed_marker_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [ln.strip() for ln in lines[1:] if ln.strip()]

    def _write_seed(
        self, src_path: Path, source_label: str, *, record_fragment: bool = False
    ) -> None:
        # SAFETY-CRITICAL ORDERING: write the marker BEFORE copying the cache.
        # If seeding is interrupted between the two steps, marker-first leaves
        # "marked but no cache" (harmless -- is_seeded() only matters once a
        # cache exists), whereas cache-first would leave a seeded cache with
        # no marker, silently bypassing the forced-review gate. Do not
        # reorder. The plain (non-atomic) marker write is intentional:
        # is_seeded() only checks file existence.
        marker = source_label + "\n"
        if record_fragment:
            # Record the fragment's CONFIG_ lines so a later menuconfig round-trip
            # can flag any that an upstream symbol rename dropped. Read fails
            # before either write, so the marker-first invariant is preserved.
            fragment = src_path.read_text(encoding="utf-8").splitlines()
            for line in fragment:
                if line.strip().startswith("CONFIG_"):
                    marker += line.strip() + "\n"
        self.seed_marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.seed_marker_path.write_text(marker, encoding="utf-8")
        _atomic_copy(str(src_path), str(self.cache_path))

    def seed_from_default(self, mcu: str) -> Optional[str]:
        """Seed the cache from ``defaults/<mcu>.config`` or ``defaults/default.config``.

        Returns the seed source label, or None when no default exists.
        Never overwrites an existing cache.
        """
        if self.has_cached_config():
            return None
        for candidate, label in (
            (default_config_path(mcu), f"mcu-default:{mcu}"),
            (get_defaults_dir() / "default.config", "mcu-default:default"),
        ):
            if candidate.exists():
                self._write_seed(candidate, label)
                return label
        return None

    def seed_from_board(self, profile_key: str) -> Optional[str]:
        """Seed the cache from a board profile's Kconfig fragment.

        Looks up the profile via :func:`kflash.boards.get_profile`. When the
        profile exists, declares ``config_fragment=True``, and its
        ``fragment_path()`` exists on disk, writes that fragment into the cache
        with the ``board:<profile_key>`` marker and returns the label. Returns
        None when the profile is missing, ships no fragment, or the fragment
        file is absent. Never overwrites an existing cache (same guard as
        :meth:`seed_from_default`).

        ``boards`` is imported lazily here: ``kflash.boards`` imports
        ``config.xdg_base`` at module load, so a top-level ``from . import
        boards`` would create an import cycle. Late imports are already the
        codebase norm (fast startup), so this stays consistent.
        """
        if self.has_cached_config():
            return None
        from . import boards  # late import: avoids boards<->config cycle

        profile = boards.get_profile(profile_key)
        if profile is None or not profile.config_fragment:
            return None
        fragment = profile.fragment_path()
        if not fragment.exists():
            return None
        label = f"board:{profile_key}"
        # Record the fragment's CONFIG_ lines in the marker so the post-review
        # drift check can flag any symbol an upstream rename dropped on load.
        self._write_seed(fragment, label, record_fragment=True)
        return label

    def seed_from_device(self, src_device_key: str) -> bool:
        """Seed the cache by copying another device's cached config.

        Returns False when the source has no cache. Overwrites this device's
        cache (caller confirms first).
        """
        src_cache = get_config_dir(src_device_key) / ".config"
        if not src_cache.exists():
            return False
        self._write_seed(src_cache, f"device:{src_device_key}")
        return True

    def save_cache_as_default(self, mcu: str) -> Path:
        """Copy this device's cached config to ``defaults/<mcu>.config``.

        Raises ConfigError when no cache exists. Returns the default path.
        """
        if not self.has_cached_config():
            raise ConfigError(
                format_error(
                    "Config error",
                    "No cached config to save as default",
                    context={"device": self.device_key},
                    recovery="Run menuconfig for this device first",
                )
            )
        dst = default_config_path(mcu)
        _atomic_copy(str(self.cache_path), str(dst))
        return dst

    def validate_mcu(self, expected_mcu: str) -> tuple[bool, Optional[str]]:
        """Validate MCU type in klipper .config matches expected.

        Uses prefix matching: 'stm32h723' matches 'stm32h723xx'.

        Args:
            expected_mcu: Expected MCU type from device registry

        Returns:
            (is_match, actual_mcu) tuple

        Raises:
            ConfigError: If .config doesn't exist or has no CONFIG_MCU
        """
        if not self.klipper_config_path.exists():
            msg = format_error(
                "Config error",
                "No .config file for MCU validation",
                context={"path": str(self.klipper_dir)},
                recovery=(
                    "1. Run make menuconfig to create .config\n"
                    "2. Or use --skip-menuconfig with existing cached config\n"
                    f"3. Check: ls {self.klipper_dir}/.config"
                ),
            )
            raise ConfigError(msg)

        actual_mcu = parse_mcu_from_config(str(self.klipper_config_path))
        if actual_mcu is None:
            return False, "unknown"

        # Prefix match: device registry may have 'stm32h723', config has 'stm32h723xx'
        is_match = actual_mcu.startswith(expected_mcu) or expected_mcu.startswith(actual_mcu)

        return is_match, actual_mcu

    def get_mtime(self) -> Optional[float]:
        """Get modification time of klipper .config file.

        Returns mtime in seconds since epoch, or None if file doesn't exist.
        Used to detect if menuconfig saved changes.
        """
        if not self.klipper_config_path.exists():
            return None
        return self.klipper_config_path.stat().st_mtime

    def has_cached_config(self) -> bool:
        """Check if cached config exists for this device."""
        return self.cache_path.exists()

    def get_cache_mtime(self) -> Optional[float]:
        """Get modification time of cached config.

        Returns mtime in seconds since epoch, or None if no cache exists.
        """
        if not self.cache_path.exists():
            return None
        return self.cache_path.stat().st_mtime

    def get_cache_age_display(self) -> Optional[str]:
        """Get human-readable age of cached config.

        Returns e.g. "2 hours ago", "3 days ago", "14 days ago (Recommend Review)".
        Returns None if no cached config exists.
        """
        mtime = self.get_cache_mtime()
        if mtime is None:
            return None

        age_seconds = time.time() - mtime
        if age_seconds < 0:
            age_seconds = 0

        minutes = int(age_seconds / 60)
        hours = int(age_seconds / 3600)
        days = int(age_seconds / 86400)

        if hours < 1:
            label = f"{max(minutes, 1)} minutes ago"
        elif days < 1:
            label = f"{hours} hours ago" if hours > 1 else "1 hour ago"
        else:
            label = f"{days} days ago" if days > 1 else "1 day ago"
            if days >= 90:
                label += " (Recommend Review)"

        return label
