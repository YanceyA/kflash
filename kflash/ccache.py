"""ccache integration for accelerated firmware builds.

Provides ccache detection, symlink management, environment setup,
configuration, and statistics retrieval.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from kflash.models import CcacheStats


def is_ccache_available() -> bool:
    """Check if ccache is installed and available in PATH.

    Returns:
        True if ccache executable found, False otherwise.
    """
    return shutil.which("ccache") is not None


def get_ccache_bin_dir() -> Path:
    """Get the path to the ccache symlink directory.

    Uses XDG_DATA_HOME if set and absolute, otherwise ~/.local/share.
    Creates path: {base}/kalico-flash/ccache-bin/

    Returns:
        Path to the ccache symlink directory.
    """
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "")
    if xdg_data_home and os.path.isabs(xdg_data_home):
        base = Path(xdg_data_home)
    else:
        base = Path.home() / ".local" / "share"
    return base / "kalico-flash" / "ccache-bin"


def setup_ccache_symlinks() -> bool:
    """Create symlinks for ARM cross-compilers pointing to ccache.

    Creates arm-none-eabi-gcc and arm-none-eabi-g++ symlinks in the
    ccache bin directory. These symlinks allow ccache to intercept
    compiler invocations when the directory is prepended to PATH.

    Returns:
        True if symlinks created/verified successfully, False if ccache unavailable.
    """
    ccache_path = shutil.which("ccache")
    if ccache_path is None:
        return False

    try:
        bin_dir = get_ccache_bin_dir()
        bin_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    compilers = ["arm-none-eabi-gcc", "arm-none-eabi-g++"]
    for compiler in compilers:
        symlink_path = bin_dir / compiler
        try:
            if symlink_path.is_symlink():
                # Check if symlink points to correct target
                if os.readlink(symlink_path) == ccache_path:
                    continue  # Already correct, skip
                # Wrong target, remove and recreate
                symlink_path.unlink()
            elif symlink_path.exists():
                # Regular file exists, remove it
                symlink_path.unlink()

            # Create symlink pointing to ccache
            symlink_path.symlink_to(ccache_path)
        except OSError:
            return False

    return True


def get_ccache_env() -> dict[str, str]:
    """Get environment with ccache bin directory prepended to PATH.

    Returns:
        Copy of current environment with modified PATH.
    """
    env = os.environ.copy()
    bin_dir = str(get_ccache_bin_dir())
    current_path = env.get("PATH", "")

    if current_path:
        env["PATH"] = f"{bin_dir}{os.pathsep}{current_path}"
    else:
        env["PATH"] = bin_dir

    return env


def get_ccache_config_commands() -> list[tuple[str, str]]:
    """Get ccache configuration commands for Klipper builds.

    Returns:
        List of (option, value) tuples for ccache --set-config.
    """
    return [
        ("sloppiness", "time_macros"),  # Ignore __DATE__ and __TIME__ macros
        ("max_size", "2G"),  # 2GB cache size
        ("compression", "true"),  # Enable compression
    ]


def get_build_env(use_ccache: bool) -> Optional[dict[str, str]]:
    """Get environment for firmware build, optionally with ccache.

    This is the main entry point for build.py integration. It handles
    all ccache setup (symlinks, environment) and returns None if
    ccache should not be used.

    Args:
        use_ccache: Whether ccache should be enabled for this build.

    Returns:
        Modified environment dict if ccache enabled and available,
        None if disabled or unavailable.
    """
    if not use_ccache:
        return None

    if not is_ccache_available():
        return None

    if not setup_ccache_symlinks():
        return None

    return get_ccache_env()


def configure_ccache() -> bool:
    """Configure ccache with optimal settings for Klipper builds.

    Runs ccache --set-config for each configuration option.

    Returns:
        True if all configuration commands succeeded, False otherwise.
    """
    if not is_ccache_available():
        return False

    for option, value in get_ccache_config_commands():
        try:
            result = subprocess.run(
                ["ccache", "--set-config", f"{option}={value}"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                return False
        except (subprocess.TimeoutExpired, OSError):
            return False

    return True


def get_ccache_stats() -> Optional[CcacheStats]:
    """Get current ccache statistics.

    Runs ccache --print-stats and parses the output.

    Returns:
        CcacheStats object with current statistics, or None on failure.
    """
    if not is_ccache_available():
        return None

    try:
        result = subprocess.run(
            ["ccache", "--print-stats"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None

        stats = _parse_ccache_stats(result.stdout)
        if stats and stats.total_calls == 0 and result.stdout.strip():
            # Fallback: some versions format stats differently
            show = subprocess.run(
                ["ccache", "--show-stats"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if show.returncode == 0:
                fallback = _parse_ccache_stats(show.stdout)
                if fallback and fallback.total_calls > 0:
                    return fallback

        return stats
    except (subprocess.TimeoutExpired, OSError):
        return None


def _parse_ccache_stats(output: str) -> Optional[CcacheStats]:
    """Parse ccache --print-stats output into CcacheStats.

    Handles both old and new ccache output formats.

    Args:
        output: Raw output from ccache --print-stats.

    Returns:
        CcacheStats object or None if parsing fails.
    """
    stats = CcacheStats()

    total_miss_value: Optional[int] = None
    direct_miss_value = 0
    preprocessed_miss_value = 0

    def _apply_kv(key: str, value: str) -> bool:
        nonlocal total_miss_value, direct_miss_value, preprocessed_miss_value
        key = key.strip().replace("-", "_")
        if not key:
            return False
        try:
            if key in {"cache_hit_direct", "direct_cache_hit"}:
                stats.cache_hit_direct = int(value)
                return True
            if key in {"cache_hit_preprocessed", "preprocessed_cache_hit"}:
                stats.cache_hit_preprocessed = int(value)
                return True
            if key == "cache_miss":
                total_miss_value = int(value)
                stats.cache_miss = total_miss_value
                return True
            if key == "direct_cache_miss":
                direct_miss_value = int(value)
                return True
            if key == "preprocessed_cache_miss":
                preprocessed_miss_value = int(value)
                return True
            if key in {"cache_size_kibibyte", "cache_size_kib", "cache_size_kibibytes"}:
                stats.cache_size_bytes = int(value) * 1024
                return True
            if key in {
                "max_cache_size_kibibyte",
                "max_cache_size_kib",
                "max_cache_size_kibibytes",
            }:
                stats.cache_max_bytes = int(value) * 1024
                return True
            if key in {"cache_size_bytes", "cache_size"}:
                stats.cache_size_bytes = int(value)
                return True
            if key in {"max_cache_size_bytes", "max_cache_size"}:
                stats.cache_max_bytes = int(value)
                return True
        except ValueError:
            return False
        return False

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        # Prefer machine-readable --print-stats output (tab-separated key/value)
        if "\t" in line:
            key, value = line.split("\t", 1)
            if _apply_kv(key, value):
                continue

        # Space/colon-separated key/value (varies by ccache version)
        tokens = line.replace(":", " ").split()
        if len(tokens) >= 2 and _apply_kv(tokens[0], tokens[1]):
            continue

        # Fallback: human-readable output parsing
        lower = line.lower()
        # Parse direct cache hits
        if "cache hit (direct)" in lower or "direct cache hit" in lower:
            stats.cache_hit_direct = _extract_number(line)
        # Parse preprocessed cache hits
        elif "cache hit (preprocessed)" in lower or "preprocessed cache hit" in lower:
            stats.cache_hit_preprocessed = _extract_number(line)
        # Parse cache misses
        elif "cache miss" in lower:
            stats.cache_miss = _extract_number(line)
        # Parse cache size (bytes)
        elif "cache size" in lower:
            stats.cache_size_bytes = _extract_size_bytes(line)
        # Parse max cache size
        elif "max cache size" in lower or "maximum cache size" in lower:
            stats.cache_max_bytes = _extract_size_bytes(line)

    if total_miss_value is None and (direct_miss_value or preprocessed_miss_value):
        stats.cache_miss = direct_miss_value + preprocessed_miss_value

    return stats


def _extract_number(line: str) -> int:
    """Extract first integer from a line."""
    import re

    match = re.search(r"\d+", line)
    if match:
        return int(match.group())
    return 0


def _extract_size_bytes(line: str) -> int:
    """Extract size in bytes from a line with optional units.

    Handles formats like:
    - "123456789" (bytes)
    - "1.5 GB"
    - "45 MB"
    - "2.0 GiB"
    """
    import re

    # Try to find number with unit
    match = re.search(r"([\d.]+)\s*([KMGT]i?B?)?", line, re.IGNORECASE)
    if not match:
        return 0

    value = float(match.group(1))
    unit = (match.group(2) or "").upper()

    multipliers = {
        "": 1,
        "B": 1,
        "K": 1024,
        "KB": 1024,
        "KIB": 1024,
        "M": 1024**2,
        "MB": 1024**2,
        "MIB": 1024**2,
        "G": 1024**3,
        "GB": 1024**3,
        "GIB": 1024**3,
        "T": 1024**4,
        "TB": 1024**4,
        "TIB": 1024**4,
    }

    return int(value * multipliers.get(unit, 1))
