"""kflash Textual style guide -- the visual identity, proven in one screen.

Run it manually::

    python -m kflash.ui.style_guide

It renders, in a single view, every primitive later screens will reuse:

* a skinned ``devices`` panel wrapping a flat ``DataTable`` (registered ``[OK]``,
  outdated ``[!!]``, and a dim blocked row);
* a ``status`` panel with phase lines and all four status markers;
* a palette-coloured progress bar;
* a muted footer hint line (custom -- not the stock reverse-video Footer);
* a confirm-style modal dialog (rounded, centred), toggled with ``d``.

Nothing here does real work; it exists to lock in taste before real screens do.

Animations: this app sets ``animation_level = "none"`` (see ``__init__``). The
style guide has no animated widgets, and disabling animation keeps the
snapshot-tested output deterministic and the feel calm/terminal-native. The
command palette is disabled and there is no stock ``Header``.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, ProgressBar, Static

from kflash.ui import skin
from kflash.ui.skin import COLORS, HintLine, Panel

# Sample device rows: (name, mcu, method, status-kind, blocked?)
_DEVICES: list[tuple[str, str, str, str, bool]] = [
    ("mainboard", "stm32f446", "katapult_usb", "ok", False),
    ("toolhead", "rp2040", "katapult_can", "warn", False),
    ("/dev/ttyUSB0", "-", "unsupported", "error", True),
]

_HINTS: list[tuple[str, str]] = [
    ("F", "Flash"),
    ("B", "Build"),
    ("A", "Add"),
    ("D", "Dialog"),
    ("Q", "Quit"),
]


def _device_cells(
    name: str, mcu: str, method: str, kind: str, blocked: bool
) -> list[Text]:
    """Build the four Rich-Text cells for one device row."""
    if blocked:
        # Blocked rows read as fully dim/subtle -- including the marker -- so the
        # "disabled row" state is visually distinct from the four live status
        # markers showcased in the status panel legend.
        dim = f"dim {COLORS['subtle']}"
        return [
            Text(name, style=dim),
            Text(mcu, style=dim),
            Text(method, style=dim),
            Text("[--]", style=dim),
        ]
    return [
        Text(name, style=COLORS["text"]),
        Text(mcu, style=COLORS["key_info"]),
        Text(method, style=COLORS["subtle"]),
        skin.status_marker(kind),
    ]


def _status_legend() -> Text:
    """A single line showing every status marker with its meaning."""
    text = Text()
    pairs = [
        ("ok", "registered"),
        ("warn", "outdated"),
        ("caution", "excluded"),
        ("error", "error"),
    ]
    for index, (kind, meaning) in enumerate(pairs):
        if index:
            text.append("   ")
        text.append_text(skin.status_marker(kind))
        text.append(f" {meaning}", style=COLORS["subtle"])
    return text


class KConfirmScreen(ModalScreen[bool]):
    """A calm, centred confirm dialog -- rounded panel, key-hint footer."""

    BINDINGS = [
        ("enter", "confirm", "Confirm"),
        ("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Panel(title="confirm", classes="dialog"):
            yield Static(
                Text(
                    "Flash firmware to 'mainboard' now?",
                    style=COLORS["text"],
                ),
                classes="dialog-message",
            )
            yield HintLine([("Enter", "Confirm"), ("Esc", "Cancel")])

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class StyleGuideApp(App[None]):
    """Single-screen showcase of the kflash Textual skin."""

    CSS_PATH = [skin.CSS_PATH]
    ENABLE_COMMAND_PALETTE = False
    AUTO_FOCUS = "#devices"
    TITLE = "kflash style guide"

    BINDINGS = [
        ("d", "show_confirm", "Dialog"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        # Register + activate the kflash theme before CSS is parsed so the
        # $kf-* variables resolve; also disable animation (see module docstring).
        self.register_theme(skin.KFLASH_THEME)
        self.theme = skin.KFLASH_THEME_NAME
        self.animation_level = "none"

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Panel(title="devices"):
                yield DataTable(id="devices", zebra_stripes=False, cursor_type="row")
            with Panel(title="status"):
                yield Static(skin.phase_line("[Discovery] scanning USB + CAN buses"))
                yield Static(skin.phase_line("[Build]     firmware compiled (kalico v0.12)"))
                yield Static(_status_legend())
            with Panel(title="progress"):
                yield Static(Text("Flashing mainboard", style=COLORS["text"]))
                yield ProgressBar(total=100, show_eta=False, id="flash-progress")
        yield HintLine(_HINTS)

    def on_mount(self) -> None:
        table = self.query_one("#devices", DataTable)
        table.add_columns("Device", "MCU", "Method", "Status")
        for name, mcu, method, kind, blocked in _DEVICES:
            table.add_row(*_device_cells(name, mcu, method, kind, blocked))
        self.query_one("#flash-progress", ProgressBar).update(progress=62)

    def action_show_confirm(self) -> None:
        self.push_screen(KConfirmScreen())


def main() -> None:
    StyleGuideApp().run()


if __name__ == "__main__":
    main()
