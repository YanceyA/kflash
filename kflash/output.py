"""Pluggable output interface via Protocol."""

from __future__ import annotations

import sys
from typing import Protocol

from .theme import get_theme


class Output(Protocol):
    """Pluggable output interface. Core modules call these methods.
    CLI provides CliOutput. Future Moonraker provides MoonrakerOutput."""

    def info(self, section: str, message: str) -> None: ...
    def success(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def error_with_recovery(
        self,
        error_type: str,
        message: str,
        context: dict[str, str] | None = None,
        recovery: str | None = None,
    ) -> None: ...
    def device_line(self, marker: str, name: str, detail: str) -> None: ...
    def prompt(self, message: str, default: str = "") -> str: ...
    def confirm(self, message: str, default: bool = False) -> bool: ...
    def mcu_mismatch_choice(self, actual_mcu: str, expected_mcu: str, device_name: str) -> str: ...
    def phase(self, phase_name: str, message: str) -> None: ...
    def step_divider(self) -> None: ...
    def device_divider(self, index: int, total: int, name: str) -> None: ...


class CliOutput:
    """Default CLI output with ANSI color support."""

    def __init__(self) -> None:
        self.theme = get_theme()

    def info(self, section: str, message: str) -> None:
        t = self.theme
        print(f"{t.info}[{section}]{t.reset} {message}")

    def success(self, message: str) -> None:
        t = self.theme
        print(f"{t.success}[OK]{t.reset} {message}")

    def warn(self, message: str) -> None:
        t = self.theme
        print(f"{t.warning}[!!]{t.reset} {message}")

    def error(self, message: str) -> None:
        t = self.theme
        print(f"{t.error}[FAIL]{t.reset} {message}", file=sys.stderr)

    def error_with_recovery(
        self,
        error_type: str,
        message: str,
        context: dict[str, str] | None = None,
        recovery: str | None = None,
    ) -> None:
        """Print formatted error with context and recovery guidance to stderr."""
        from .errors import format_error

        formatted = format_error(error_type, message, context, recovery)
        print(formatted, file=sys.stderr)

    def device_line(self, marker: str, name: str, detail: str) -> None:
        t = self.theme
        marker_styles = {
            "REG": t.marker_reg,
            "NEW": t.marker_new,
            "BLK": t.marker_blk,
            "DUP": t.marker_dup,
        }
        # For numbered markers (1, 2, 3...), use marker_num style
        if marker.isdigit():
            style = t.marker_num
        else:
            style = marker_styles.get(marker.upper(), "")
        print(f"  {style}[{marker}]{t.reset} {name:<24s} {detail}")

    def prompt(self, message: str, default: str = "") -> str:
        t = self.theme
        suffix = f" [{default}]" if default else ""
        response = input(f"{t.prompt}{message}{suffix}:{t.reset} ").strip()
        return response or default

    def confirm(self, message: str, default: bool = False) -> bool:
        t = self.theme
        suffix = " [Y/n]" if default else " [y/N]"
        response = input(f"{t.prompt}{message}{suffix}:{t.reset} ").strip().lower()
        if not response:
            return default
        return response in ("y", "yes")

    def mcu_mismatch_choice(self, actual_mcu: str, expected_mcu: str, device_name: str) -> str:
        """Prompt user after MCU mismatch. Returns 'r', 'd', or 'k'."""
        self.warn(
            f"MCU mismatch: config has '{actual_mcu}' "
            f"but device '{device_name}' expects '{expected_mcu}'"
        )
        while True:
            choice = (
                input("  [R]e-open menuconfig / [D]iscard config / [K]eep anyway: ").strip().lower()
            )
            if choice in ("r", "d", "k"):
                return choice

    def phase(self, phase_name: str, message: str) -> None:
        """Output a phase-labeled message."""
        t = self.theme
        print(f"{t.phase}[{phase_name}]{t.reset} {message}")

    def step_divider(self) -> None:
        """Print an unlabeled step divider line."""
        from .panels import render_action_divider

        print()
        print(render_action_divider())

    def device_divider(self, index: int, total: int, name: str) -> None:
        """Print a labeled device divider for batch operations."""
        from .panels import render_device_divider

        print()
        print(render_device_divider(index, total, name))


class NullOutput:
    """Silent output for testing or programmatic use."""

    def info(self, section: str, message: str) -> None:
        pass

    def success(self, message: str) -> None:
        pass

    def warn(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass

    def error_with_recovery(
        self,
        error_type: str,
        message: str,
        context: dict[str, str] | None = None,
        recovery: str | None = None,
    ) -> None:
        pass

    def device_line(self, marker: str, name: str, detail: str) -> None:
        pass

    def prompt(self, message: str, default: str = "") -> str:
        return default

    def confirm(self, message: str, default: bool = False) -> bool:
        return default

    def mcu_mismatch_choice(self, actual_mcu: str, expected_mcu: str, device_name: str) -> str:
        return "k"

    def phase(self, phase_name: str, message: str) -> None:
        pass

    def step_divider(self) -> None:
        pass

    def device_divider(self, index: int, total: int, name: str) -> None:
        pass
