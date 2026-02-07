"""ANSI-aware string utilities for panel rendering.

Provides functions to strip escape sequences, measure visible width
(including CJK wide characters), pad strings to exact display width,
and detect terminal dimensions.
"""

from __future__ import annotations

import re
import shutil
import sys
import unicodedata

_ANSI_RE = re.compile(r"\033\[[0-9;]*[A-Za-z]")


def strip_ansi(s: str) -> str:
    """Remove all CSI escape sequences from *s*."""
    return _ANSI_RE.sub("", s)


def display_width(s: str) -> int:
    """Return the visible character count of *s*, ignoring ANSI codes.

    CJK wide characters (East Asian Width 'W' or 'F') count as 2 columns.
    """
    total = 0
    for ch in strip_ansi(s):
        eaw = unicodedata.east_asian_width(ch)
        total += 2 if eaw in ("W", "F") else 1
    return total


def pad_to_width(s: str, target_width: int, fill: str = " ") -> str:
    """Pad *s* with *fill* characters to reach *target_width* visible columns.

    If *s* already meets or exceeds *target_width*, it is returned unchanged.
    """
    current = display_width(s)
    if current >= target_width:
        return s
    return s + fill * (target_width - current)


def get_terminal_width(default: int = 80, minimum: int = 40) -> int:
    """Return the current terminal width in columns.

    Falls back to *default* when width cannot be determined (e.g., piped
    output) and clamps the result to at least *minimum*.
    """
    cols = shutil.get_terminal_size((default, 24)).columns
    return max(cols, minimum)


def supports_unicode() -> bool:
    """Check if stdout encoding supports Unicode box-drawing characters."""
    encoding = getattr(sys.stdout, "encoding", "") or ""
    return "utf" in encoding.lower()
