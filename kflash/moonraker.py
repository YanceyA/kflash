"""Moonraker API client for print status and version detection.

Provides graceful degradation when Moonraker is unavailable - all public
functions return None on failure instead of raising exceptions. This allows
the flash workflow to continue with a warning rather than blocking.

Endpoints used:
- /printer/objects/query?print_stats&virtual_sdcard - Print status and progress
- /printer/objects/list - Discover all MCU objects
- /printer/objects/query?mcu&mcu%20nhk - MCU firmware versions
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .models import PrintStatus

# Connection settings (hardcoded per CONTEXT.md: no custom URL support)
MOONRAKER_URL = "http://localhost:7125"
TIMEOUT = 5  # seconds


def detect_firmware_flavor(version: Optional[str]) -> str:
    """Return 'Kalico' or 'Klipper' based on version string format."""
    if not version:
        return "Unknown"
    # Kalico uses date-based tags: v2025.xx, v2026.xx, etc.
    match = re.match(r"^v?(20[2-9]\d)\.", version)
    if match and int(match.group(1)) >= 2025:
        return "Kalico"
    # Klipper uses semver-style tags: v0.12.0-...
    if re.match(r"^v?\d+\.\d+\.\d+", version):
        return "Klipper"
    return "Unknown"


def get_print_status() -> Optional[PrintStatus]:
    """Query Moonraker for current print status.

    Returns:
        PrintStatus if successful, None if Moonraker unreachable or error.
    """
    try:
        url = f"{MOONRAKER_URL}/printer/objects/query?print_stats&virtual_sdcard"
        with urlopen(url, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))

        status = data["result"]["status"]
        print_stats = status.get("print_stats", {})
        virtual_sdcard = status.get("virtual_sdcard", {})

        return PrintStatus(
            state=print_stats.get("state", "standby"),
            filename=print_stats.get("filename") or None,
            progress=virtual_sdcard.get("progress", 0.0),
        )
    except (URLError, HTTPError, json.JSONDecodeError, KeyError, TimeoutError, OSError):
        return None


def get_mcu_serial_map() -> Optional[dict[str, Optional[str]]]:
    """Query Moonraker configfile for MCU name -> serial path mapping.

    Returns dict mapping MCU object name (e.g., "mcu", "mcu nhk") to serial path.
    MCUs without serial (like "mcu linux") have None value.
    Returns None if Moonraker is unreachable.
    """
    try:
        url = f"{MOONRAKER_URL}/printer/objects/query?configfile"
        with urlopen(url, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
        settings = data["result"]["status"]["configfile"]["settings"]
        result: dict[str, Optional[str]] = {}
        for key, val in settings.items():
            if key == "mcu" or key.startswith("mcu "):
                result[key] = val.get("serial")
        return result if result else None
    except (URLError, HTTPError, json.JSONDecodeError, KeyError, TimeoutError, OSError):
        return None


def get_mcu_canbus_map() -> Optional[dict[str, str]]:
    """Query Moonraker configfile for CAN UUID -> MCU object name mapping.

    Returns dict mapping canbus_uuid (e.g., "a1b2c3d4e5f6") to MCU object
    name (e.g., "mcu nhk"). Returns None if Moonraker is unreachable or no
    CAN MCUs found.
    """
    try:
        url = f"{MOONRAKER_URL}/printer/objects/query?configfile"
        with urlopen(url, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
        settings = data["result"]["status"]["configfile"]["settings"]
        result: dict[str, str] = {}
        for key, val in settings.items():
            if key == "mcu" or key.startswith("mcu "):
                uuid = val.get("canbus_uuid")
                if uuid:
                    result[uuid] = key
        return result if result else None
    except (URLError, HTTPError, json.JSONDecodeError, KeyError, TimeoutError, OSError):
        return None


def get_mcu_versions() -> Optional[dict[str, str]]:
    """Query Moonraker for all MCU firmware versions.

    Returns:
        Dict mapping MCU name to version string, None if unreachable.
        Names are normalized: "mcu" -> "main", "mcu nhk" -> "nhk".
        Example: {"main": "v0.12.0-45-g7ce409d", "nhk": "v0.12.0-45-g7ce409d"}
    """
    try:
        # First get list of all printer objects to discover MCUs
        list_url = f"{MOONRAKER_URL}/printer/objects/list"
        with urlopen(list_url, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))

        # Find all MCU objects (mcu, mcu linux, mcu nhk, etc.)
        all_objects = data["result"]["objects"]
        mcu_objects = [obj for obj in all_objects if obj == "mcu" or obj.startswith("mcu ")]

        if not mcu_objects:
            return None

        # Query MCU objects for mcu_version field
        # URL-encode spaces as %20 for query params
        query_params = "&".join(obj.replace(" ", "%20") for obj in mcu_objects)
        query_url = f"{MOONRAKER_URL}/printer/objects/query?{query_params}"

        with urlopen(query_url, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))

        versions: dict[str, str] = {}
        for mcu_name, mcu_data in data["result"]["status"].items():
            if "mcu_version" in mcu_data:
                # Normalize names: "mcu" -> "main", "mcu nhk" -> "nhk"
                if mcu_name == "mcu":
                    name = "main"
                else:
                    # Strip "mcu " prefix (4 characters)
                    name = mcu_name[4:]
                version = mcu_data["mcu_version"]
                versions[name] = version

                # Also key by chip type (e.g., "stm32h723xx") for
                # substring matching against device mcu_type fields
                mcu_constants = mcu_data.get("mcu_constants", {})
                chip_type = mcu_constants.get("MCU")
                if chip_type and chip_type not in versions:
                    versions[chip_type] = version

        return versions if versions else None

    except (URLError, HTTPError, json.JSONDecodeError, KeyError, TimeoutError, OSError):
        return None


def get_host_klipper_version(klipper_dir: str) -> Optional[str]:
    """Get host Klipper version via git describe.

    Uses --long flag to always include commit count and hash, matching
    the format used by MCU firmware (e.g., "v0.12.0-0-g7ce409d" when
    exactly at tag, "v0.12.0-45-g7ce409d" when 45 commits ahead).

    Args:
        klipper_dir: Path to Klipper source directory (supports ~ expansion).

    Returns:
        Version string like "v0.12.0-45-g7ce409d" or None if failed.
    """
    klipper_path = Path(klipper_dir).expanduser()
    try:
        result = subprocess.run(
            ["git", "describe", "--always", "--tags", "--long", "--dirty"],
            cwd=str(klipper_path),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            if version and "-g" in version:
                return version
            # Fallback: synthesize vX-Y-gHASH when git describe returns tag-only
            tag = version if version.startswith("v") else None
            if not tag:
                tag_result = subprocess.run(
                    ["git", "describe", "--tags", "--abbrev=0"],
                    cwd=str(klipper_path),
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT,
                )
                if tag_result.returncode == 0:
                    tag = tag_result.stdout.strip()
            count_result = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=str(klipper_path),
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )
            hash_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(klipper_path),
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )
            if count_result.returncode == 0 and hash_result.returncode == 0:
                count = count_result.stdout.strip()
                short_hash = hash_result.stdout.strip()
                if tag:
                    return f"{tag}-{count}-g{short_hash}"
                return f"{count}-g{short_hash}"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _parse_git_describe(version: str) -> tuple[Optional[str], Optional[int]]:
    """Parse git-describe style version strings.

    Returns (tag, commit_count). commit_count is None if not present.
    """
    v = version.strip()
    if not v:
        return None, None

    # Typical forms:
    #   v0.12.0-45-g7ce409d
    #   v0.12.0-0-g7ce409d
    #   v0.12.0-45-g7ce409d-dirty
    #   v2026.01.00
    match = re.match(
        r"^(v[0-9A-Za-z.\-_]+?)(?:-([0-9]+)-g[0-9a-fA-F]+)?(?:-dirty)?$",
        v,
    )
    if not match:
        return None, None
    tag = match.group(1)
    count = int(match.group(2)) if match.group(2) is not None else None
    return tag, count


def match_serial_to_mcu_name(pattern: str, mcu_serials: dict[str, Optional[str]]) -> Optional[str]:
    """Match a device serial pattern to a Klipper MCU object name.

    Args:
        pattern: Device serial pattern glob (e.g., "usb-Klipper_stm32h723xx_29001A*").
        mcu_serials: Dict mapping MCU object name to serial path (or None).

    Returns:
        The matching MCU object name, or None if no match.
    """
    for mcu_name, serial_path in mcu_serials.items():
        if not serial_path:
            continue
        # Extract filename from path
        filename = serial_path.rsplit("/", 1)[-1] if "/" in serial_path else serial_path
        if fnmatch.fnmatchcase(filename, pattern):
            return mcu_name
    return None


def parse_mcu_objects(response: dict) -> dict:
    """Extract chip and version from Moonraker MCU object response.

    Args:
        response: Dict mapping MCU name to MCU data from Moonraker query.

    Returns:
        Dict mapping MCU name to {"chip": ..., "version": ...}.
    """
    result: dict = {}
    for name, data in response.items():
        chip = data.get("mcu_constants", {}).get("MCU")
        version = data.get("mcu_version")
        if chip is None or version is None:
            continue
        result[name] = {"chip": chip, "version": version}
    return result


def _get_mcu_version_fuzzy(
    mcu_type: str,
    device_name: str = "",
    device_key: str = "",
    *,
    _mcu_versions: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Get MCU firmware version for a specific device.

    Tries matching by device name/key first (against Moonraker MCU names
    like "nhk", "HBB"), then falls back to chip type matching. This avoids
    returning the wrong version when multiple devices share the same MCU
    chip type (e.g., two rp2040 boards).

    Args:
        mcu_type: Device MCU type string (e.g., "stm32h723", "rp2040")
        device_name: Human-readable device name (e.g., "Nhk v1.3")
        device_key: Device registry key (e.g., "nhk-v13")

    Returns:
        Version string like "v0.12.0-45-g7ce409d" or None if unavailable.
    """
    if _mcu_versions is not None:
        mcu_versions = _mcu_versions
    else:
        mcu_versions = get_mcu_versions()
    if not mcu_versions:
        return None

    # 1. Try matching device name/key against Moonraker MCU names
    for candidate in (device_name, device_key):
        if not candidate:
            continue
        candidate_lower = candidate.lower()
        for mcu_name, version in mcu_versions.items():
            nl = mcu_name.lower()
            if nl == candidate_lower or nl in candidate_lower or candidate_lower in nl:
                return version

    # 2. Fall back to chip type matching
    mcu_lower = mcu_type.lower()
    for mcu_name, version in mcu_versions.items():
        if mcu_name.lower() == mcu_lower:
            return version

    for mcu_name, version in mcu_versions.items():
        name_lower = mcu_name.lower()
        if mcu_lower in name_lower or name_lower in mcu_lower:
            return version

    return None


def get_mcu_version_for_device(
    mcu_type: str = "",
    device_name: str = "",
    device_key: str = "",
    *,
    mcu_name: Optional[str] = None,
    _mcu_versions: Optional[dict[str, str]] = None,
    allow_fuzzy_fallback: bool = False,
) -> Optional[str]:
    """Get MCU firmware version for a specific device.

    When mcu_name is provided, does a direct key lookup against the MCU
    versions dict. When mcu_name is None, returns None by default, or can
    optionally fall back to fuzzy matching for legacy/partially configured devices.

    Args:
        mcu_type: Unused (kept for backward compatibility).
        device_name: Unused (kept for backward compatibility).
        device_key: Unused (kept for backward compatibility).
        mcu_name: Klipper MCU object name for direct lookup.
        _mcu_versions: Injected versions dict for testing. If None, queries Moonraker.
        allow_fuzzy_fallback: If True and mcu_name is None, use legacy fuzzy lookup.

    Returns:
        Version string or None.
    """
    if _mcu_versions is not None:
        versions = _mcu_versions
    else:
        versions = get_mcu_versions()
        if versions is None:
            return None

    if mcu_name is None:
        if not allow_fuzzy_fallback:
            return None
        return _get_mcu_version_fuzzy(
            mcu_type,
            device_name=device_name,
            device_key=device_key,
            _mcu_versions=versions,
        )

    # Normalize mcu_name to match get_mcu_versions() key format:
    # "mcu" -> "main", "mcu hbb" -> "hbb"
    if mcu_name == "mcu":
        lookup_key = "main"
    elif mcu_name.startswith("mcu "):
        lookup_key = mcu_name[4:]  # Strip "mcu " prefix
    else:
        lookup_key = mcu_name

    # Case-insensitive lookup: Moonraker configfile returns lowercase keys,
    # but object names preserve case from printer.cfg (e.g., "hbb" vs "HBB")
    lookup_lower = lookup_key.lower()
    for key, value in versions.items():
        if key.lower() == lookup_lower:
            return value
    return None


def is_mcu_outdated(host_version: str, mcu_version: str) -> bool:
    """Check if MCU firmware appears behind host Klipper.

    Compares tag + commit count when available. Falls back to
    tag-only comparison or raw string comparison if parsing fails.
    This is informational only - never blocks flash.
    """
    host = host_version.strip()
    mcu = mcu_version.strip()
    if not host or not mcu:
        return False

    host_tag, host_count = _parse_git_describe(host)
    mcu_tag, mcu_count = _parse_git_describe(mcu)

    if host_tag and mcu_tag:
        if host_tag != mcu_tag:
            return True
        if host_count is not None and mcu_count is not None:
            return mcu_count < host_count
        # If commit counts are missing, don't warn on equal tags
        return False

    return host != mcu
