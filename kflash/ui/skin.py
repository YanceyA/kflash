"""kflash's visual identity, ported to Textual.

This module is the single source of taste for the new Textual UI. Every later
screen should import its vocabulary from here (the theme, the ``Panel`` widget,
the ``HintLine`` footer, the status-marker helpers) rather than re-deriving
colours or chrome. It intentionally owns no application logic -- only looks.

Design goals (see UI_BRAINSTORM.md s4 "Visual identity"): reproduce the cozy,
muted, calm, terminal-native feel of the hand-rolled TUI in ``kflash/panels.py``
and ``kflash/screen.py`` -- NOT a stock web-dashboard-in-a-terminal. Concretely:

* Rounded panels (``border: round``) in the muted teal ``border`` colour, with a
  spaced-letter bracketed title embedded in the top border
  (``[ D E V I C E S ]``) in bold ``header`` colour -- the exact chrome that
  ``render_panel`` draws.
* Flat tables: no zebra striping, no bright focus outline; the row cursor is a
  calm reverse-video block in the ``header`` colour, echoing the legacy
  ``theme.selected`` (``\\x1b[7m`` + header fg).
* An unobtrusive single-line footer of key hints -- muted text, keys accented --
  instead of Textual's stock reverse-video ``Footer`` key chips.

Palette: ``PALETTE`` below IS the kflash palette -- the exact RGB values the
legacy renderer's ``kflash.theme.PALETTE`` carried. It moved here (Stage 3)
when the legacy UI modules were deleted; this module is now the single owner
of the colour vocabulary.

------------------------------------------------------------------------------
Background decision (UI_BRAINSTORM.md s4)
------------------------------------------------------------------------------
The legacy TUI never paints a background -- it prints coloured glyphs over
whatever the user's terminal already shows (typically near-black). The obvious
way to keep that in Textual is ``App.ansi_color = True``, which drops to the
terminal's own 16-colour palette and default background. We rejected it: the
whole identity here is a *truecolor* muted-teal palette (border 100,160,180;
header 130,200,220; ...). ``ansi_color`` forces a 16-colour degrade that
collapses those distinct teals/greens into a couple of ANSI buckets and throws
the taste work away.

So we use a **designed near-black background** instead: ``BACKGROUND`` below is a
faintly cool near-black, and ``background``, ``surface`` and ``panel`` are all set
to it so panels are *flat* -- the rounded border is the only structure, exactly
like the legacy renderer drew border lines over the terminal background with no
fill. This reads as terminal-native while preserving every truecolor value.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.containers import Vertical
from textual.content import Content
from textual.theme import Theme
from textual.widgets import Static

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

#: The kflash palette -- every semantic colour role as an RGB triple. These are
#: the exact values the legacy ANSI renderer used (formerly
#: ``kflash.theme.PALETTE``); the muted teal/sea-green identity lives here now.
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


def _hex(rgb: tuple[int, int, int]) -> str:
    """Convert an ``(r, g, b)`` triple to a ``#rrggbb`` string."""
    r, g, b = rgb
    return f"#{r:02x}{g:02x}{b:02x}"


# Every semantic role as a hex string, keyed exactly like ``PALETTE``.
COLORS: dict[str, str] = {name: _hex(rgb) for name, rgb in PALETTE.items()}

# Designed near-black background (see module docstring). Faintly cool so it sits
# in the same family as the teal palette without ever competing with it.
BACKGROUND: str = "#0c1013"
# A barely-there lift for elevated surfaces (the modal dialog) -- still reads flat.
SURFACE_LIFT: str = "#12171b"
# Dark-teal selection band for highlighted list options. Dark enough that the
# palette's light per-segment foregrounds (label/text/value) stay readable on
# top of it -- Rich Text segment styles override an OptionList's highlight
# foreground, so the highlight must work WITH the existing text colours.
SELECTION: str = "#1d3a44"

# ---------------------------------------------------------------------------
# The theme
# ---------------------------------------------------------------------------

#: Custom CSS variables (referenced as ``$kf-*`` in kflash.tcss). These carry the
#: full palette into CSS so semantic roles stay named, never hard-coded.
_KF_VARIABLES: dict[str, str] = {
    "kf-border": COLORS["border"],
    "kf-header": COLORS["header"],
    "kf-label": COLORS["label"],
    "kf-prompt": COLORS["prompt"],
    "kf-text": COLORS["text"],
    "kf-value": COLORS["value"],
    "kf-subtle": COLORS["subtle"],
    "kf-green": COLORS["green"],
    "kf-yellow": COLORS["yellow"],
    "kf-orange": COLORS["orange"],
    "kf-red": COLORS["red"],
    "kf-key-info": COLORS["key_info"],
    "kf-background": BACKGROUND,
    "kf-surface": SURFACE_LIFT,
    "kf-selection": SELECTION,
}

KFLASH_THEME_NAME = "kflash"

#: The kflash Textual theme, built from the exact legacy palette.
KFLASH_THEME = Theme(
    name=KFLASH_THEME_NAME,
    primary=COLORS["header"],      # accents / cursor -> header teal
    secondary=COLORS["label"],
    accent=COLORS["key_info"],
    warning=COLORS["yellow"],
    error=COLORS["red"],
    success=COLORS["green"],
    foreground=COLORS["text"],
    background=BACKGROUND,
    surface=BACKGROUND,            # flat: surface == background
    panel=BACKGROUND,              # flat: panels are border-only chrome
    dark=True,
    variables=_KF_VARIABLES,
)

#: Path to the shared stylesheet. Apps set ``CSS_PATH = [skin.CSS_PATH]``.
CSS_PATH: Path = Path(__file__).with_name("kflash.tcss")


# ---------------------------------------------------------------------------
# Spaced-letter title helper
# ---------------------------------------------------------------------------


def spaced_title(text: str) -> str:
    """Transform ``"devices"`` into ``"[ D E V I C E S ]"``.

    Mirrors ``kflash.panels._spaced_header`` so Textual ``border_title``s read
    identically to the legacy panel headers.
    """
    spaced = " ".join(text.upper())
    return f"[ {spaced} ]"


# ---------------------------------------------------------------------------
# Status markers
# ---------------------------------------------------------------------------

#: Maps a semantic status kind to its (marker text, palette-role) pair.
STATUS_MARKERS: dict[str, tuple[str, str]] = {
    "ok": ("[OK]", "green"),
    "warn": ("[!!]", "yellow"),
    "caution": ("[~~]", "orange"),
    "error": ("[FAIL]", "red"),
}


def status_marker(kind: str) -> Text:
    """Return a Rich ``Text`` status marker (e.g. green ``[OK]``).

    Suitable as a ``DataTable`` cell or inline in a ``Static``. ``kind`` is one
    of ``ok``, ``warn``, ``caution``, ``error``.
    """
    marker, role = STATUS_MARKERS[kind]
    return Text(marker, style=COLORS[role])


def phase_line(text: str) -> Text:
    """Return a phase line (e.g. ``[Discovery] scanning...``) in border colour."""
    return Text(text, style=COLORS["border"])


# ---------------------------------------------------------------------------
# Reusable widgets
# ---------------------------------------------------------------------------


class Panel(Vertical):
    """A rounded, border-titled container -- the Textual port of ``render_panel``.

    The ``title`` is rendered as a spaced-letter bracketed header embedded in the
    top border, in bold header colour, exactly like the legacy panels. Chrome is
    defined in ``kflash.tcss`` (``.panel``); this class only wires up the title.
    """

    def __init__(self, *children, title: str, **kwargs) -> None:
        classes = kwargs.pop("classes", "")
        kwargs["classes"] = f"panel {classes}".strip()
        super().__init__(*children, **kwargs)
        self._panel_title = title

    def on_mount(self) -> None:
        # Wrap in Content so the ``[`` in the spaced title is rendered literally
        # rather than parsed as Textual console markup (which would blank it out).
        self.border_title = Content(spaced_title(self._panel_title))


class HintLine(Static):
    """A muted single-line footer of key hints (keys accented, labels subtle).

    Deliberately NOT Textual's stock ``Footer``: no reverse-video key chips, just
    one calm terminal-native line, e.g. ``F Flash   B Build   A Add   Q Quit``.
    """

    def __init__(self, hints: list[tuple[str, str]], **kwargs) -> None:
        classes = kwargs.pop("classes", "")
        kwargs["classes"] = f"hint-line {classes}".strip()
        super().__init__(self._render_hints(hints), **kwargs)

    @staticmethod
    def _render_hints(hints: list[tuple[str, str]]) -> Text:
        text = Text(no_wrap=True, overflow="ellipsis")
        for index, (key, label) in enumerate(hints):
            if index:
                text.append("   ", style=COLORS["subtle"])
            text.append(key, style=f"bold {COLORS['header']}")
            text.append(" ", style=COLORS["subtle"])
            text.append(label, style=COLORS["subtle"])
        return text
