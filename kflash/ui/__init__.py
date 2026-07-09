"""Textual UI for kflash (the UI_BRAINSTORM Track C rebuild).

This package is the ONLY place Textual is imported. Nothing in the engine
(``kflash/`` outside this package) may import from here, and modules here
must talk to the engine exclusively through the Phase 2 contract:
:mod:`kflash.events` (Emitter/EventSink), :mod:`kflash.decisions`
(DecisionProvider), and the ``kflash.commands`` entry points.

Import Textual lazily from the entry point (``kflash.flash.main``) so the
engine/commands surface stays importable in environments without the
dependency installed (a clear install hint is printed instead).

Re-exports the visual vocabulary from :mod:`kflash.ui.skin` so screens can
import it from one place, e.g. ``from kflash.ui import Panel, HintLine``.
"""

from kflash.ui.skin import (
    BACKGROUND,
    COLORS,
    CSS_PATH,
    KFLASH_THEME,
    KFLASH_THEME_NAME,
    HintLine,
    Panel,
    phase_line,
    spaced_title,
    status_marker,
)

__all__ = [
    "BACKGROUND",
    "COLORS",
    "CSS_PATH",
    "KFLASH_THEME",
    "KFLASH_THEME_NAME",
    "HintLine",
    "Panel",
    "phase_line",
    "spaced_title",
    "status_marker",
]
