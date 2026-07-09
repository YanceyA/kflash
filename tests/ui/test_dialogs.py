"""Tests for the styled R4 decision dialogs (kflash.ui.dialogs).

Each dialog is exercised the way the engine drives it: a fake engine job on a
bridge worker thread calls a ``DecisionProvider`` method, the *real*
:class:`~kflash.ui.engine_bridge.UiDecisionProvider` (wired with
:func:`~kflash.ui.dialogs.styled_modal_factory`) pushes the styled modal, and the
test presses keys and asserts the round-tripped value -- extending the
``test_engine_bridge`` patterns to the designed dialogs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Callable

from textual.app import App

from kflash.decisions import (
    ChooseCcacheActionDecision,
    ChooseDeviceDecision,
    ChooseFlashMethodDecision,
    ConfirmDecision,
    DeviceChoice,
    ManualBootloaderReadyDecision,
    McuMismatchDecision,
    TextPromptDecision,
)
from kflash.ui import skin
from kflash.ui.dialogs import (
    ChoiceDialog,
    DecisionConfirmDialog,
    FlashMethodDialog,
    ManualBootloaderDialog,
    TextPromptDialog,
    styled_modal_factory,
)
from kflash.ui.engine_bridge import EngineBridge

_SIZE = (80, 32)


def run_async(coro_factory: Callable[[], Awaitable[None]]):
    return asyncio.run(coro_factory())


class DialogApp(App[None]):
    """Minimal themed app that services decisions with the styled factory."""

    CSS_PATH = [skin.CSS_PATH]
    ENABLE_COMMAND_PALETTE = False

    def __init__(self) -> None:
        super().__init__()
        self.completions: list = []
        self.register_theme(skin.KFLASH_THEME)
        self.theme = skin.KFLASH_THEME_NAME
        self.animation_level = "none"

    def on_engine_job_completed(self, message) -> None:  # type: ignore[no-untyped-def]
        self.completions.append(message)


async def _wait(pilot, predicate, what: str = "") -> None:
    import time

    deadline = time.monotonic() + 5.0
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await pilot.pause()


def _drive(job_factory, presses, dialog_type):
    """Run *job* on a bridge, wait for *dialog_type*, apply *presses*, return result."""

    async def scenario() -> None:
        app = DialogApp()
        async with app.run_test(size=_SIZE) as pilot:
            bridge = EngineBridge(app, modal_factory=styled_modal_factory)
            thread = bridge.run_engine_job(job_factory(bridge))
            await _wait(pilot, lambda: isinstance(app.screen, dialog_type), "dialog")
            for key in presses:
                await pilot.press(key)
            await _wait(pilot, lambda: app.completions, "completion")
        thread.join(5)
        scenario.result = app.completions[-1].result  # type: ignore[attr-defined]

    run_async(scenario)
    return scenario.result  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# ConfirmDecision -> DecisionConfirmDialog (default-polarity preserved)
# --------------------------------------------------------------------------- #
def test_confirm_enter_honours_default_true() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.confirm(
            ConfirmDecision(id="x", message="Proceed?", default=True)
        )),
        ["enter"],
        DecisionConfirmDialog,
    )
    assert result is True


def test_confirm_enter_honours_default_false() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.confirm(
            ConfirmDecision(id="x", message="Proceed?", default=False)
        )),
        ["enter"],
        DecisionConfirmDialog,
    )
    assert result is False


def test_confirm_explicit_yes() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.confirm(
            ConfirmDecision(id="x", message="?", default=False)
        )),
        ["y"],
        DecisionConfirmDialog,
    )
    assert result is True


def test_confirm_escape_declines() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.confirm(
            ConfirmDecision(id="x", message="?", default=True)
        )),
        ["escape"],
        DecisionConfirmDialog,
    )
    assert result is False


# --------------------------------------------------------------------------- #
# ChooseDeviceDecision -> ChoiceDialog (cursor + number jump)
# --------------------------------------------------------------------------- #
def test_choose_device_number_jump_then_select() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.choose_device(
            ChooseDeviceDecision(
                prompt="Pick",
                choices=[
                    DeviceChoice(key="alpha", label="Alpha"),
                    DeviceChoice(key="beta", label="Beta"),
                    DeviceChoice(key="gamma", label="Gamma"),
                ],
            )
        )),
        ["3", "enter"],
        ChoiceDialog,
    )
    assert result == "gamma"


def test_choose_device_escape_cancels() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.choose_device(
            ChooseDeviceDecision(
                prompt="Pick",
                choices=[DeviceChoice(key="alpha", label="Alpha")],
                allow_cancel=True,
            )
        )),
        ["escape"],
        ChoiceDialog,
    )
    assert result is None


# --------------------------------------------------------------------------- #
# McuMismatch / ccache -> ChoiceDialog with safe escape value
# --------------------------------------------------------------------------- #
def test_mcu_mismatch_escape_keeps() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.mcu_mismatch(
            McuMismatchDecision(actual_mcu="rp2040", expected_mcu="stm32", device_name="B")
        )),
        ["escape"],
        ChoiceDialog,
    )
    assert result == "k"


def test_ccache_escape_skips() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.choose_ccache_action(ChooseCcacheActionDecision())),
        ["escape"],
        ChoiceDialog,
    )
    assert result == "skip"


def test_ccache_select_install() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.choose_ccache_action(ChooseCcacheActionDecision())),
        ["1", "enter"],
        ChoiceDialog,
    )
    assert result == "install"


# --------------------------------------------------------------------------- #
# ManualBootloader -> ManualBootloaderDialog
# --------------------------------------------------------------------------- #
def test_manual_bootloader_ready() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.manual_bootloader_ready(
            ManualBootloaderReadyDecision(device_name="Board", batch=False)
        )),
        ["r"],
        ManualBootloaderDialog,
    )
    assert result is True


def test_manual_bootloader_escape_cancels() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.manual_bootloader_ready(
            ManualBootloaderReadyDecision(device_name="Board", batch=False)
        )),
        ["escape"],
        ManualBootloaderDialog,
    )
    assert result is False


# --------------------------------------------------------------------------- #
# TextPrompt -> TextPromptDialog
# --------------------------------------------------------------------------- #
def test_text_prompt_returns_default_on_enter() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.prompt_text(
            TextPromptDecision(message="MCU name", default="rp2040")
        )),
        ["enter"],
        TextPromptDialog,
    )
    assert result == "rp2040"


def test_text_prompt_escape_returns_none() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.prompt_text(
            TextPromptDecision(message="Optional", default="")
        )),
        ["escape"],
        TextPromptDialog,
    )
    assert result is None


# --------------------------------------------------------------------------- #
# Flash-method picker: filtering reuses the engine catalogue (no copy)
# --------------------------------------------------------------------------- #
def test_flash_method_dialog_filters_rp2040() -> None:
    dialog = FlashMethodDialog(
        ChooseFlashMethodDecision(
            current_bootloader=None,
            current_flash_command=None,
            device_name="Pico",
            mcu="rp2040",
            is_can_device=False,
        )
    )
    labels = [label for _value, label in dialog._options]
    # RP2 excludes serial re-enumeration methods; picoboot (Make Flash Direct)
    # is reordered to the front; no CAN methods on a USB device.
    assert labels[0] == "Make Flash Direct"
    assert "Katapult Serial" not in labels
    assert "Make Flash USB" not in labels
    assert all("CAN" not in label for label in labels)


def test_flash_method_dialog_can_only_shows_can() -> None:
    dialog = FlashMethodDialog(
        ChooseFlashMethodDecision(
            current_bootloader=None,
            current_flash_command=None,
            device_name="Toolhead",
            mcu="stm32g0",
            is_can_device=True,
        )
    )
    labels = [label for _value, label in dialog._options]
    assert labels == ["Katapult CAN"]


def test_flash_method_dialog_highlights_current() -> None:
    dialog = FlashMethodDialog(
        ChooseFlashMethodDecision(
            current_bootloader="usb",
            current_flash_command="make_flash",
            device_name="Board",
            mcu="stm32h7",
            is_can_device=False,
        )
    )
    # The current (usb, make_flash) pair is pre-highlighted.
    assert dialog._options[dialog._current_index][0] == ("usb", "make_flash")


def test_flash_method_round_trip_selects_pair() -> None:
    result = _drive(
        lambda b: (lambda: b.decisions.choose_flash_method(
            ChooseFlashMethodDecision(
                current_bootloader=None,
                current_flash_command=None,
                device_name="Pico",
                mcu="rp2040",
                is_can_device=False,
            )
        )),
        ["1", "enter"],
        FlashMethodDialog,
    )
    # First option for rp2040 is picoboot: (none, make_flash).
    assert result == ("none", "make_flash")


def test_styled_factory_covers_every_request() -> None:
    from kflash.ui.dialogs import ManualBootloaderDialog as _MBD

    reqs = [
        ConfirmDecision(id="x", message="?", default=True),
        ChooseDeviceDecision(prompt="p", choices=[DeviceChoice("a", "A")]),
        ChooseFlashMethodDecision(None, None, "B", "rp2040", False),
        ManualBootloaderReadyDecision(device_name="B", batch=False),
        McuMismatchDecision("a", "b", "B"),
        ChooseCcacheActionDecision(),
        TextPromptDecision(message="m"),
    ]
    types = {DecisionConfirmDialog, ChoiceDialog, FlashMethodDialog, _MBD, TextPromptDialog}
    produced = {type(styled_modal_factory(r)) for r in reqs}
    # FlashMethodDialog subclasses ChoiceDialog; every request maps to a dialog.
    assert produced <= types | {ChoiceDialog}
    assert all(styled_modal_factory(r) is not None for r in reqs)


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
class _ModalHost(App[None]):
    CSS_PATH = [skin.CSS_PATH]
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, modal) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self._modal = modal
        self.register_theme(skin.KFLASH_THEME)
        self.theme = skin.KFLASH_THEME_NAME
        self.animation_level = "none"

    def on_mount(self) -> None:
        self.push_screen(self._modal)


def test_flash_method_picker_snapshot(snap_compare) -> None:
    modal = FlashMethodDialog(
        ChooseFlashMethodDecision(
            current_bootloader="usb",
            current_flash_command="make_flash",
            device_name="Octopus Pro",
            mcu="stm32h723",
            is_can_device=False,
        )
    )
    assert snap_compare(_ModalHost(modal), terminal_size=_SIZE)


def test_text_prompt_step_snapshot(snap_compare) -> None:
    # Represents one add-device wizard step (the display-name prompt).
    modal = TextPromptDialog("Display name (e.g., 'Octopus Pro v1.1')", default="")
    assert snap_compare(_ModalHost(modal), terminal_size=_SIZE)
