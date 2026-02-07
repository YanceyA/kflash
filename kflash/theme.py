"""Centralized terminal styling with truecolor support and tier fallback.

Provides a Theme dataclass with semantic style names (e.g., theme.success not
theme.green) and automatic terminal capability detection across four tiers:
truecolor (24-bit), 256-color, ANSI 16, and no-color.

Follows the NO_COLOR standard (https://no-color.org/) for accessibility.

Usage:
    t = get_theme()
    print(f"{t.success}[OK]{t.reset} Operation complete")
"""

from __future__ import annotations

import enum
import os
import subprocess
import sys
from dataclasses import dataclass

# ANSI escape codes (legacy constants kept for any direct references)
RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"


# ---------------------------------------------------------------------------
# Color tier detection
# ---------------------------------------------------------------------------


class ColorTier(enum.Enum):
    """Terminal color capability tiers, from richest to none."""

    TRUECOLOR = "truecolor"
    ANSI256 = "256"
    ANSI16 = "16"
    NONE = "none"


def _enable_windows_vt_mode() -> bool:
    """Enable ANSI escape code processing on Windows 10+.

    Uses ctypes to call SetConsoleMode with ENABLE_VIRTUAL_TERMINAL_PROCESSING.
    Returns True if successful, False if unsupported or failed.
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        return True
    except Exception:
        return False


def detect_color_tier() -> ColorTier:
    """Detect terminal color tier from environment.

    Detection order:
    1. NO_COLOR env var set -> NONE
    2. FORCE_COLOR env var set -> TRUECOLOR
    3. stdout not a TTY -> NONE
    4. TERM == 'dumb' -> NONE
    5. Windows: enable VT mode, then check COLORTERM/TERM
    6. COLORTERM in {truecolor, 24bit} -> TRUECOLOR
    7. TERM contains '256color' -> ANSI256
    8. Otherwise -> ANSI16
    """
    if os.environ.get("NO_COLOR"):
        return ColorTier.NONE

    if os.environ.get("FORCE_COLOR"):
        return ColorTier.TRUECOLOR

    if not sys.stdout.isatty():
        return ColorTier.NONE

    if os.environ.get("TERM") == "dumb":
        return ColorTier.NONE

    if sys.platform == "win32":
        if not _enable_windows_vt_mode():
            return ColorTier.NONE

    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in ("truecolor", "24bit"):
        return ColorTier.TRUECOLOR

    term = os.environ.get("TERM", "")
    if "256color" in term:
        return ColorTier.ANSI256

    return ColorTier.ANSI16


def supports_color() -> bool:
    """Backward-compatible color check. Returns True if any color tier is active."""
    return detect_color_tier() != ColorTier.NONE


# ---------------------------------------------------------------------------
# RGB palette and conversion
# ---------------------------------------------------------------------------

PALETTE: dict[str, tuple[int, int, int]] = {
    "border": (100, 160, 180),
    "header": (130, 200, 220),
    "label": (140, 180, 160),
    "prompt": (180, 220, 200),
    "text": (200, 210, 215),
    "value": (220, 225, 230),
    "subtle": (100, 120, 130),
    "green": (80, 200, 120),
    "yellow": (220, 190, 60),
    "orange": (200, 140, 60),
    "red": (200, 80, 80),
    "key_info": (160, 150, 200),
}


def _rgb_to_256(r: int, g: int, b: int) -> int:
    """Convert RGB to closest xterm-256 color index."""
    # Check greyscale ramp (indices 232-255, 24 shades)
    if abs(r - g) < 10 and abs(g - b) < 10:
        grey = (r + g + b) // 3
        if grey < 8:
            return 16
        if grey > 248:
            return 231
        return round((grey - 8) / 247 * 23) + 232

    # 6x6x6 color cube (indices 16-231)
    ri = round(r / 255 * 5)
    gi = round(g / 255 * 5)
    bi = round(b / 255 * 5)
    return 16 + 36 * ri + 6 * gi + bi


def _rgb_to_16(r: int, g: int, b: int) -> int:
    """Convert RGB to ANSI 16-color code (30-37, 90-97)."""
    brightness = (r + g + b) / 3
    bright = brightness > 127

    # Determine dominant channel
    mx = max(r, g, b)
    threshold = mx * 0.6

    has_r = r >= threshold
    has_g = g >= threshold
    has_b = b >= threshold

    # Map to 3-bit color
    if has_r and has_g and has_b:
        base = 7  # white
    elif has_r and has_g:
        base = 3  # yellow
    elif has_r and has_b:
        base = 5  # magenta
    elif has_g and has_b:
        base = 6  # cyan
    elif has_r:
        base = 1  # red
    elif has_g:
        base = 2  # green
    elif has_b:
        base = 4  # blue
    else:
        base = 0  # black

    return (90 + base) if bright else (30 + base)


def rgb_to_ansi(r: int, g: int, b: int, tier: ColorTier, bg: bool = False) -> str:
    """Convert RGB color to ANSI escape sequence for the given tier.

    Args:
        r, g, b: Color components (0-255).
        tier: Terminal color tier.
        bg: If True, produce background color instead of foreground.

    Returns:
        ANSI escape sequence string, or empty string for NONE tier.
    """
    if tier is ColorTier.NONE:
        return ""

    if tier is ColorTier.TRUECOLOR:
        mode = 48 if bg else 38
        return f"\033[{mode};2;{r};{g};{b}m"

    if tier is ColorTier.ANSI256:
        idx = _rgb_to_256(r, g, b)
        mode = 48 if bg else 38
        return f"\033[{mode};5;{idx}m"

    # ANSI16
    code = _rgb_to_16(r, g, b)
    if bg:
        code += 10
    return f"\033[{code}m"


# ---------------------------------------------------------------------------
# Theme dataclass
# ---------------------------------------------------------------------------


@dataclass
class Theme:
    """Theme with semantic style definitions.

    All fields contain ANSI escape sequences (or empty strings for no-color mode).
    Access via get_theme() to ensure correct detection of terminal capabilities.
    """

    tier: ColorTier = ColorTier.NONE

    # Panel structure styles
    border: str = ""
    header: str = ""
    label: str = ""
    prompt: str = ""
    text: str = ""
    value: str = ""
    subtle: str = ""
    key_info: str = ""  # MCU type accent

    # Conditional field styles
    disabled: str = ""  # Dim + muted for greyed-out fields
    selected: str = ""  # Reverse video for highlighted selection

    # Semantic message styles
    success: str = ""  # [OK] messages
    warning: str = ""  # [!!] warnings
    caution: str = ""  # [~~] caution/exclusion notices
    error: str = ""  # [FAIL] errors
    info: str = ""  # [section] info
    phase: str = ""  # [Discovery], [Build], etc.

    # Backward-compat UI element styles
    menu_title: str = ""
    menu_border: str = ""

    # Device marker styles (backward compat)
    marker_reg: str = ""
    marker_new: str = ""
    marker_blk: str = ""
    marker_dup: str = ""
    marker_num: str = ""

    # Text modifiers
    bold: str = ""
    dim: str = ""

    # Reset code
    reset: str = ""


def _build_theme(tier: ColorTier) -> Theme:
    """Construct a Theme by applying the palette at the given tier."""
    if tier is ColorTier.NONE:
        return Theme(tier=tier)

    def c(name: str) -> str:
        r, g, b = PALETTE[name]
        return rgb_to_ansi(r, g, b, tier)

    bold = _BOLD
    dim = _DIM
    reset = RESET

    border = c("border")
    header = bold + c("header")
    success = c("green")
    warning = c("yellow")
    caution = c("orange")
    error = c("red")

    # Disabled: dim modifier + subtle palette color (clearly greyed out)
    disabled = dim + c("subtle")

    # Selected: reverse video + accent foreground
    # When terminal renders reverse, fg becomes bg, producing accent-colored background
    if tier is ColorTier.ANSI16:
        # Basic mode: bold instead of reverse (more readable on limited terminals)
        selected = bold
    else:
        # Truecolor and 256-color: reverse video with header accent color
        selected = "\033[7m" + c("header")

    return Theme(
        tier=tier,
        border=border,
        header=header,
        label=c("label"),
        prompt=bold + c("prompt"),
        text=c("text"),
        value=c("value"),
        subtle=c("subtle"),
        key_info=c("key_info"),
        disabled=disabled,
        selected=selected,
        success=success,
        warning=warning,
        caution=caution,
        error=error,
        info=c("header"),
        phase=border,
        menu_title=bold,
        menu_border=border,
        marker_reg=success,
        marker_new=warning,
        marker_blk=warning,
        marker_dup=warning,
        marker_num="",
        bold=bold,
        dim=dim,
        reset=reset,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_cached_theme: Theme | None = None


def get_theme() -> Theme:
    """Return appropriate theme based on terminal capabilities.

    Caches result on first call. Use reset_theme() to re-detect.
    """
    global _cached_theme
    if _cached_theme is None:
        tier = detect_color_tier()
        _cached_theme = _build_theme(tier)
    return _cached_theme


def reset_theme() -> None:
    """Clear cached theme (for testing or after env change)."""
    global _cached_theme
    _cached_theme = None


def clear_screen() -> None:
    """Clear terminal screen, preserving scrollback buffer where possible.

    Implementation:
    - Unix: clear -x if available, else ANSI fallback
    - Windows with VT: ANSI escape sequence
    - Windows without VT: cmd /c cls
    """
    if sys.platform == "win32":
        if supports_color():
            print("\033[H\033[J", end="", flush=True)
        else:
            try:
                subprocess.run(["cmd", "/c", "cls"], check=False, timeout=5)
            except (OSError, subprocess.SubprocessError):
                print("\033[H\033[J", end="", flush=True)
    else:
        try:
            result = subprocess.run(
                ["clear", "-x"],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            print("\033[H\033[J", end="", flush=True)
            return
        if result.returncode != 0:
            print("\033[H\033[J", end="", flush=True)
