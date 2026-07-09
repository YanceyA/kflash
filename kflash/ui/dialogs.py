"""Styled modal dialogs for the kflash Textual UI.

Two families live here:

* :class:`ConfirmDialog` -- the calm pre-flash yes/no used by the dashboard's
  flash flow (Stage 1). ``Enter`` confirms; kept unchanged so the flash flow is
  undisturbed.
* The **decision dialogs** + :func:`styled_modal_factory` (Stage 2, R4): a real,
  skin-consistent modal for every :class:`kflash.decisions.DecisionProvider`
  request. The factory has the exact same shape contract as
  :func:`kflash.ui.engine_bridge.default_modal_factory` -- each modal dismisses
  with the value the corresponding provider method returns -- so wiring it into
  an :class:`~kflash.ui.engine_bridge.EngineBridge` (as its ``modal_factory``)
  swaps the unstyled placeholders for these designed dialogs with no other
  change.

All dialogs are built from the shared skin vocabulary (:class:`~kflash.ui.skin.Panel`
rounded border + spaced-letter title, :class:`~kflash.ui.skin.HintLine` footer,
the palette :data:`~kflash.ui.skin.COLORS`) so they match the dashboard rather
than looking like stock Textual chrome. Every dialog is keyboard-first and maps
``Escape`` to the request's safe/cancel answer.
"""

from __future__ import annotations

from typing import Any, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ..decisions import (
    ChooseCcacheActionDecision,
    ChooseDeviceDecision,
    ChooseFlashMethodDecision,
    ConfirmDecision,
    ManualBootloaderReadyDecision,
    McuMismatchDecision,
    TextPromptDecision,
)
from ..validation import filter_flash_methods_for_device
from .skin import BACKGROUND, COLORS, SURFACE_LIFT, HintLine, Panel

__all__ = [
    "ConfirmDialog",
    "DecisionConfirmDialog",
    "ChoiceDialog",
    "FlashMethodDialog",
    "ManualBootloaderDialog",
    "TextPromptDialog",
    "styled_modal_factory",
]


class ConfirmDialog(ModalScreen[bool]):
    """A centred yes/no confirmation. Dismisses ``True`` (yes) or ``False``.

    Used by the dashboard's pre-flash prompt where ``Enter`` == confirm. Left
    unchanged from Stage 1 so the flash flow is undisturbed; decision-seam
    confirmations use :class:`DecisionConfirmDialog` (which honours request
    default polarity) instead.
    """

    DEFAULT_CSS = f"""
    ConfirmDialog {{
        align: center middle;
        background: {BACKGROUND} 70%;
    }}
    ConfirmDialog > .dialog {{
        width: 56;
        max-width: 56;
        height: auto;
        background: {SURFACE_LIFT};
        padding: 1 2;
        margin: 0;
    }}
    ConfirmDialog .dialog-message {{
        color: {COLORS['text']};
        padding: 0 0 1 0;
    }}
    """

    BINDINGS = [
        ("y", "confirm", "Confirm"),
        ("enter", "confirm", "Confirm"),
        ("n", "cancel", "Cancel"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, message: str, *, title: str = "confirm") -> None:
        super().__init__()
        self._message = message
        self._title = title

    def compose(self) -> ComposeResult:
        with Panel(title=self._title, classes="dialog"):
            yield Static(
                Text(self._message, style=COLORS["text"]),
                classes="dialog-message",
            )
            yield HintLine([("Y/Enter", "Confirm"), ("N/Esc", "Cancel")])

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class DecisionConfirmDialog(ModalScreen[bool]):
    """Styled :class:`~kflash.decisions.ConfirmDecision` modal -> ``bool``.

    Preserves exactly the default-polarity semantics of the placeholder
    :class:`~kflash.ui.engine_bridge.ConfirmModal` (mirroring the legacy
    ``Output.confirm``): an empty answer (``Enter``) returns the request
    ``default``; the suffix reads ``[Y/n]`` when the default is yes, ``[y/N]``
    when it is no; a ``[y/N]`` prompt must never silently confirm on ``Enter``.
    ``y``/``n`` are explicit overrides; ``Escape`` declines (the conservative
    answer). ``AUTO_FOCUS`` is disabled so no widget steals ``Enter`` from the
    bindings.
    """

    AUTO_FOCUS = ""

    BINDINGS = [
        ("y", "yes", "Yes"),
        ("n", "no", "No"),
        ("enter", "default", "Default"),
        ("escape", "no", "Cancel"),
    ]

    def __init__(
        self, message: str, default: bool = False, *, title: str = "confirm"
    ) -> None:
        super().__init__(classes="kf-modal")
        self._message = message
        self._default = default
        self._title = title

    def compose(self) -> ComposeResult:
        suffix = " [Y/n]" if self._default else " [y/N]"
        default_key = "Y/Enter" if self._default else "Enter"
        with Panel(title=self._title, classes="dialog"):
            yield Static(
                Text(self._message + suffix, style=COLORS["text"]),
                classes="dialog-message",
            )
            yield HintLine(
                [("Y", "Yes"), ("N", "No"), (default_key, "Default"), ("Esc", "Cancel")]
            )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def action_default(self) -> None:
        # Empty answer == the request default (mirrors Output.confirm).
        self.dismiss(self._default)


class ChoiceDialog(ModalScreen[Optional[Any]]):
    """A cursor-driven list-choice modal with number-jump.

    ``options`` is a list of ``(value, label)`` pairs; the modal dismisses with
    the chosen ``value``. Up/down move the cursor, ``Enter`` selects the
    highlighted option, number keys ``1``..``9`` jump the cursor to the Nth
    option, and ``Escape`` dismisses with ``escape_value`` (the request's
    safe/cancel answer).

    ``current_index`` pre-highlights an option (e.g. the current selection).
    ``details`` supplies an optional muted sub-line rendered under the
    highlighted option (used by the method picker for the method description).
    """

    def __init__(
        self,
        prompt: str,
        options: list[tuple[Any, str]],
        *,
        allow_cancel: bool = True,
        escape_value: Any = None,
        current_index: Optional[int] = None,
        title: str = "select",
        details: Optional[list[str]] = None,
    ) -> None:
        super().__init__(classes="kf-modal")
        self._prompt = prompt
        self._options = list(options)
        self._allow_cancel = allow_cancel
        self._escape_value = escape_value
        self._current_index = current_index
        self._title = title
        self._details = details or []

    def compose(self) -> ComposeResult:
        with Panel(title=self._title, classes="dialog"):
            yield Static(
                Text(self._prompt, style=COLORS["prompt"]),
                classes="dialog-message",
            )
            option_widgets = [
                Option(self._option_label(index, label))
                for index, (_value, label) in enumerate(self._options)
            ]
            yield OptionList(*option_widgets, id="choice-list")
            yield Static("", id="choice-detail", classes="dialog-detail")
            hints: list[tuple[str, str]] = [
                ("Up/Dn", "Move"),
                ("1-9", "Jump"),
                ("Enter", "Select"),
            ]
            if self._allow_cancel or self._escape_value is not None:
                hints.append(("Esc", "Cancel"))
            yield HintLine(hints)

    def _option_label(self, index: int, label: str) -> Text:
        text = Text()
        text.append(f"{index + 1}. ", style=COLORS["label"])
        text.append(label, style=COLORS["text"])
        return text

    def on_mount(self) -> None:
        option_list = self.query_one("#choice-list", OptionList)
        if (
            self._current_index is not None
            and 0 <= self._current_index < len(self._options)
        ):
            option_list.highlighted = self._current_index
        elif self._options:
            option_list.highlighted = 0
        option_list.focus()
        self._update_detail(option_list.highlighted)

    def _update_detail(self, index: Optional[int]) -> None:
        detail = self.query_one("#choice-detail", Static)
        if index is not None and 0 <= index < len(self._details):
            detail.update(Text(self._details[index], style=COLORS["subtle"]))
        else:
            detail.update("")

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        self._update_detail(event.option_index)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        index = event.option_index
        if 0 <= index < len(self._options):
            self.dismiss(self._options[index][0])

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key in "123456789":
            number = int(event.key) - 1
            if 0 <= number < len(self._options):
                event.stop()
                self.query_one("#choice-list", OptionList).highlighted = number
        elif event.key == "escape":
            event.stop()
            self.dismiss(self._escape_value)


class FlashMethodDialog(ChoiceDialog):
    """The real flash-method picker -> ``(bootloader_method, flash_command)`` or ``None``.

    Reuses the engine-side catalogue and filter
    (:func:`kflash.validation.filter_flash_methods_for_device`) -- the SAME
    logic the legacy ``ui_input._flash_method_picker_overlay`` used -- so the
    RP2040/RP2350 exclusions, the picoboot reorder, and the CAN-only transport
    filter are identical and never copied. Methods are filtered per MCU +
    transport, the current method is pre-highlighted, and each row's description
    shows as a muted detail line.
    """

    def __init__(self, request: ChooseFlashMethodDecision) -> None:
        methods = filter_flash_methods_for_device(
            mcu=request.mcu, is_can_device=request.is_can_device
        )
        options: list[tuple[Any, str]] = [
            ((pair.bootloader_method, pair.flash_command), pair.name)
            for pair in methods
        ]
        details = [f"{pair.description} - {pair.notes}" for pair in methods]
        current_index: Optional[int] = None
        for index, pair in enumerate(methods):
            if (
                pair.bootloader_method == request.current_bootloader
                and pair.flash_command == request.current_flash_command
            ):
                current_index = index
                break
        if request.device_name:
            prompt = f"Select flash method for {request.device_name}"
        else:
            prompt = "Select flash method"
        super().__init__(
            prompt,
            options,
            allow_cancel=True,
            escape_value=None,
            current_index=current_index,
            title="flash method",
            details=details,
        )


class ManualBootloaderDialog(ModalScreen[bool]):
    """Styled :class:`~kflash.decisions.ManualBootloaderReadyDecision` -> ``bool``.

    Shows the bootloader-entry instruction and waits for the user to confirm the
    device is ready (``Enter``/``r``) or cancel (``Escape``/``c``). Cancel is the
    safe answer (aborts the flash rather than writing to an unready board).
    """

    AUTO_FOCUS = ""

    BINDINGS = [
        ("r", "ready", "Ready"),
        ("enter", "ready", "Ready"),
        ("c", "cancel", "Cancel"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, device_name: str, batch: bool = False) -> None:
        super().__init__(classes="kf-modal")
        self._device_name = device_name
        self._batch = batch

    def compose(self) -> ComposeResult:
        with Panel(title="manual bootloader", classes="dialog"):
            yield Static(
                Text(
                    f"Put '{self._device_name}' into bootloader mode "
                    "(button/jumper), then confirm.",
                    style=COLORS["text"],
                ),
                classes="dialog-message",
            )
            yield HintLine([("R/Enter", "Ready"), ("C/Esc", "Cancel")])

    def action_ready(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class TextPromptDialog(ModalScreen[Optional[str]]):
    """Styled :class:`~kflash.decisions.TextPromptDecision` -> ``str`` or ``None``.

    A single-line input pre-filled with the request ``default``. ``Enter``
    submits (a ``required`` prompt refuses an empty submit and stays open);
    ``Escape`` dismisses with ``None`` (cancel), which lets a wizard step abort.
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        message: str,
        default: str = "",
        required: bool = False,
        *,
        title: str = "input",
    ) -> None:
        super().__init__(classes="kf-modal")
        self._message = message
        self._default = default
        self._required = required
        self._title = title

    def compose(self) -> ComposeResult:
        with Panel(title=self._title, classes="dialog"):
            yield Static(
                Text(self._message, style=COLORS["prompt"]),
                classes="dialog-message",
            )
            yield Input(value=self._default, id="prompt-input")
            hint = "required" if self._required else "optional"
            yield HintLine([("Enter", f"Submit ({hint})"), ("Esc", "Cancel")])

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value
        if self._required and not value.strip():
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


def styled_modal_factory(request: Any) -> ModalScreen:
    """Map a typed decision request to its designed dialog.

    Same shape contract as
    :func:`kflash.ui.engine_bridge.default_modal_factory` (each modal dismisses
    with the value the matching provider method returns); pass this as an
    :class:`~kflash.ui.engine_bridge.EngineBridge` ``modal_factory`` to replace
    the unstyled placeholder modals.
    """
    if isinstance(request, ConfirmDecision):
        return DecisionConfirmDialog(request.message, request.default)

    if isinstance(request, ChooseDeviceDecision):
        options: list[tuple[Any, str]] = [
            (choice.key, choice.label) for choice in request.choices
        ]
        return ChoiceDialog(
            request.prompt,
            options,
            allow_cancel=request.allow_cancel,
            escape_value=None,
            title="select device",
        )

    if isinstance(request, ChooseFlashMethodDecision):
        return FlashMethodDialog(request)

    if isinstance(request, ManualBootloaderReadyDecision):
        return ManualBootloaderDialog(request.device_name, request.batch)

    if isinstance(request, McuMismatchDecision):
        prompt = (
            f"MCU mismatch on {request.device_name}: built config targets "
            f"{request.actual_mcu!r}, device expects {request.expected_mcu!r}."
        )
        # Escape -> "k" (keep existing / skip): the conservative answer.
        return ChoiceDialog(
            prompt,
            [
                ("r", "Re-open menuconfig"),
                ("d", "Discard config (different device)"),
                ("k", "Keep mismatched config"),
            ],
            allow_cancel=False,
            escape_value="k",
            title="mcu mismatch",
        )

    if isinstance(request, ChooseCcacheActionDecision):
        # Escape -> "skip": the conservative answer (build once without ccache).
        return ChoiceDialog(
            "ccache is not installed. How should the build proceed?",
            [
                ("install", "Install ccache now (requires sudo)"),
                ("skip", "Skip ccache for this build"),
                ("disable", "Disable ccache permanently"),
            ],
            allow_cancel=False,
            escape_value="skip",
            title="ccache",
        )

    if isinstance(request, TextPromptDecision):
        return TextPromptDialog(request.message, request.default, request.required)

    raise TypeError(f"No styled modal for decision request: {request!r}")
