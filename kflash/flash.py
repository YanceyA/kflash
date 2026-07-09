#!/usr/bin/env python3
"""Entry point for kalico-flash.

Launches the Textual terminal UI for building and flashing Klipper firmware
to USB/CAN-connected MCU boards. Requires an interactive terminal (TTY).

This module is the composition root and dispatch layer only. The command
bodies live in ``kflash/commands/`` (``cmd_flash``, ``cmd_flash_all``,
``cmd_add_device``, ``cmd_remove_device``, ``cmd_list_devices``, ``cmd_build``)
and are re-exported below for backward compatibility.

Core logic lives in:
    - commands/: cmd_* entry points (UI-free)
    - registry.py: Device registry persistence
    - discovery.py: USB device scanning and matching
    - events.py / decisions.py: engine event stream + decision provider
    - flash_steps.py: shared build/flash/verify orchestration
    - ui/: the Textual UI (imported lazily -- the engine stays importable
      without textual installed)
"""

from __future__ import annotations

import signal
import sys

from kflash import __version__ as VERSION  # noqa: F401  (public re-export)

# Python version guard
if sys.version_info < (3, 9):
    sys.exit("Error: kalico-flash requires Python 3.9 or newer.")

from .commands import (  # noqa: E402  (must follow the version guard)
    cmd_add_device,
    cmd_build,
    cmd_flash,
    cmd_flash_all,
    cmd_list_devices,
    cmd_remove_device,
)
from .registry import Registry
from .safety import check_not_root, resolve_registry_path

__all__ = [
    "cmd_flash",
    "cmd_flash_all",
    "cmd_add_device",
    "cmd_remove_device",
    "cmd_list_devices",
    "cmd_build",
    "main",
]


def _install_signal_handlers() -> None:
    """Convert SIGHUP/SIGTERM into exceptions so ``finally`` blocks run.

    An SSH disconnect delivers SIGHUP; the default disposition kills the
    process before klipper_service_stopped() can restart the service,
    leaving the printer down. Raising SystemExit instead unwinds the stack
    through every context manager on the way out.
    """

    def _raise_exit(signum, frame):
        raise SystemExit(128 + signum)

    for name in ("SIGHUP", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _raise_exit)
        except (ValueError, OSError):
            pass  # Non-main thread or unsupported platform


def main() -> int:
    """Main entry point -- launch the Textual UI."""
    check_not_root()
    _install_signal_handlers()

    if not sys.stdin.isatty():
        print("kalico-flash requires an interactive terminal.", file=sys.stderr)
        return 1

    # The legacy hand-rolled TUI was removed at Stage 3 (UI_BRAINSTORM §8);
    # the old flags are accepted-and-ignored so stale wrappers keep working.
    if "--legacy-ui" in sys.argv:
        print(
            "Note: the legacy UI was removed; starting the Textual UI.",
            file=sys.stderr,
        )

    # The import stays function-local (enforced by test_layering) so importing
    # kflash.flash never drags in Textual -- the engine/commands surface must
    # stay usable in environments without the UI dependencies.
    try:
        from .ui.app import run_ui
    except ImportError:
        # Reachable by a `git pull` without re-running install.sh: the UI's
        # dependencies (textual/rich) are missing from this Python.
        print(
            "kflash's UI needs the 'textual' package, which is not installed "
            "in this environment.\n"
            "Fix: re-run ./install.sh (installs into the kflash venv), or "
            "`pip install 'textual>=8.2,<9'` into the Python running kflash.",
            file=sys.stderr,
        )
        return 1

    registry_path = resolve_registry_path()
    registry = Registry(registry_path)
    return run_ui(registry)


if __name__ == "__main__":
    sys.exit(main())
