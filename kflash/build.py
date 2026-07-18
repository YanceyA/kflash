"""Build operations: menuconfig TUI passthrough and firmware compilation."""

from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path
from typing import Optional

from . import runner
from .ccache import configure_ccache, get_build_env, get_ccache_stats
from .models import BuildResult, CcacheStats

# Default timeout for build operations (5 minutes)
TIMEOUT_BUILD = 300


def run_menuconfig(klipper_dir: str, config_path: str) -> tuple[int, bool]:
    """Run make menuconfig with inherited stdio for ncurses TUI.

    Sets KCONFIG_CONFIG to the absolute path of config_path so menuconfig
    reads/writes the specified file instead of .config in klipper_dir.

    Args:
        klipper_dir: Path to klipper source directory (supports ~)
        config_path: Path to .config file to use

    Returns:
        (return_code, was_saved) tuple:
        - return_code: Exit code from menuconfig
        - was_saved: True if config file was modified (mtime changed)
    """
    klipper_path = Path(klipper_dir).expanduser()
    config_abs = Path(config_path).expanduser().absolute()

    # Record mtime before (None if file doesn't exist yet)
    mtime_before: Optional[float] = None
    if config_abs.exists():
        mtime_before = config_abs.stat().st_mtime

    # Set up environment with KCONFIG_CONFIG pointing to absolute path
    env = os.environ.copy()
    env["KCONFIG_CONFIG"] = str(config_abs)

    # Run menuconfig with inherited stdio (no PIPE) for ncurses TUI
    # User can navigate, edit, save with normal keyboard controls
    returncode = runner.run_interactive(
        ["make", "menuconfig"],
        cwd=str(klipper_path),
        env=env,
    )

    # Check if config was saved (mtime changed or file created)
    was_saved = False
    if config_abs.exists():
        mtime_after = config_abs.stat().st_mtime
        if mtime_before is None or mtime_after > mtime_before:
            was_saved = True

    return returncode, was_saved


def _captured_tail(result: runner.CommandResult) -> Optional[str]:
    """Return the last 200 lines of the captured build output."""
    raw = (result.stdout or "") + (result.stderr or "")
    lines = raw.splitlines()
    return "\n".join(lines[-200:])


def run_build(
    klipper_dir: str,
    timeout: int = TIMEOUT_BUILD,
    use_ccache: bool = False,
) -> BuildResult:
    """Run make clean + make -j with captured output.

    Executes build in klipper directory, capturing stdout/stderr instead of
    inheriting the terminal (a build's high-volume output would otherwise
    overdraw the TUI). Uses all available CPU cores for parallel compilation.
    On failure, the last 200 captured lines are returned in
    ``BuildResult.error_output``.

    Args:
        klipper_dir: Path to klipper source directory (supports ~)
        timeout: Seconds before timeout (default: TIMEOUT_BUILD)
        use_ccache: Enable ccache build acceleration if available

    Returns:
        BuildResult with success status, firmware path/size, elapsed time
    """
    klipper_path = Path(klipper_dir).expanduser()
    start_time = time.monotonic()

    # Get ccache environment if enabled
    build_env = get_build_env(use_ccache)
    pre_stats: Optional[CcacheStats] = None
    if build_env is not None:
        # Configure ccache on first use (idempotent)
        configure_ccache()
        pre_stats = get_ccache_stats()

    # Run make clean with captured output
    clean_result = runner.run(
        ["make", "clean"],
        cwd=str(klipper_path),
        timeout=timeout,
        env=build_env,  # None uses default environment
    )

    if clean_result.timed_out:
        return BuildResult(
            success=False,
            elapsed_seconds=time.monotonic() - start_time,
            error_message=f"make clean timed out after {timeout}s",
            error_output=_captured_tail(clean_result),
        )

    if clean_result.returncode != 0:
        elapsed = time.monotonic() - start_time
        return BuildResult(
            success=False,
            elapsed_seconds=elapsed,
            error_message=f"make clean failed with exit code {clean_result.returncode}",
            error_output=_captured_tail(clean_result),
        )

    # Run make -j with all available cores, captured output
    nproc = multiprocessing.cpu_count()
    build_result = runner.run(
        ["make", f"-j{nproc}"],
        cwd=str(klipper_path),
        timeout=timeout,
        env=build_env,  # None uses default environment
    )

    if build_result.timed_out:
        return BuildResult(
            success=False,
            elapsed_seconds=time.monotonic() - start_time,
            error_message=f"Build timed out after {timeout}s",
            error_output=_captured_tail(build_result),
        )

    elapsed = time.monotonic() - start_time

    if build_result.returncode != 0:
        return BuildResult(
            success=False,
            elapsed_seconds=elapsed,
            error_message=f"make failed with exit code {build_result.returncode}",
            error_output=_captured_tail(build_result),
        )

    # Check for firmware output (.bin preferred, .uf2 for RP2040)
    firmware_path = klipper_path / "out" / "klipper.bin"
    if not firmware_path.exists():
        firmware_path = klipper_path / "out" / "klipper.uf2"
    if not firmware_path.exists():
        return BuildResult(
            success=False,
            elapsed_seconds=elapsed,
            error_message=f"Build succeeded but firmware not found in {klipper_path / 'out'}",
        )

    firmware_size = firmware_path.stat().st_size

    # Get ccache stats if ccache was used (per-build delta when possible)
    ccache_stats = None
    if use_ccache and build_env is not None:
        post_stats = get_ccache_stats()
        if pre_stats and post_stats:
            ccache_stats = _delta_ccache_stats(pre_stats, post_stats)
        else:
            ccache_stats = post_stats

    return BuildResult(
        success=True,
        firmware_path=str(firmware_path),
        firmware_size=firmware_size,
        elapsed_seconds=elapsed,
        ccache_stats=ccache_stats,
    )


def _delta_ccache_stats(before: CcacheStats, after: CcacheStats) -> CcacheStats:
    """Compute per-build ccache stats from two snapshots."""
    return CcacheStats(
        cache_hit_direct=max(0, after.cache_hit_direct - before.cache_hit_direct),
        cache_hit_preprocessed=max(0, after.cache_hit_preprocessed - before.cache_hit_preprocessed),
        cache_miss=max(0, after.cache_miss - before.cache_miss),
        cache_size_bytes=after.cache_size_bytes,
        cache_max_bytes=after.cache_max_bytes or before.cache_max_bytes,
    )
