"""Typed decision requests + the ``DecisionProvider`` seam.

Every interactive prompt on the engine flash paths is modelled as a frozen
request dataclass. The engine calls a :class:`DecisionProvider` (never
``input()`` directly), so the same flash logic can run under the Textual UI
(:class:`kflash.ui.engine_bridge.UiDecisionProvider` -- modal round-trips) or
headless (:class:`HeadlessDecisionProvider`).

Imports only stdlib. Deliberately UI-free so any engine module may depend on
it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class ConfirmDecision:
    """A yes/no confirmation. ``id`` is a stable identity so a headless policy
    can special-case individual prompts."""

    id: str
    message: str
    default: bool = False


@dataclass(frozen=True)
class DeviceChoice:
    key: str
    label: str


@dataclass(frozen=True)
class ChooseDeviceDecision:
    prompt: str
    choices: list[DeviceChoice]
    allow_cancel: bool = True


@dataclass(frozen=True)
class ChooseFlashMethodDecision:
    current_bootloader: Optional[str]
    current_flash_command: Optional[str]
    device_name: Optional[str]
    mcu: Optional[str]
    is_can_device: bool


@dataclass(frozen=True)
class ManualBootloaderReadyDecision:
    device_name: str
    # Batch runs set batch=True (flash_batch passes a real decider with
    # batch=True); the interactive single-device path uses batch=False. The
    # provider uses this to pick the prompt style (batch instruction line vs.
    # out.info) -- the engine no longer reaches this via decider=None.
    batch: bool


@dataclass(frozen=True)
class McuMismatchDecision:
    actual_mcu: str
    expected_mcu: str
    device_name: str


@dataclass(frozen=True)
class ChooseCcacheActionDecision:
    """-> "install" | "skip" | "disable"."""


@dataclass(frozen=True)
class BoardProfileChoice:
    key: str
    label: str  # "BTT Octopus Pro v1.0/1.1 (STM32H723)"
    notes: str  # picker detail line


@dataclass(frozen=True)
class ChooseBoardProfileDecision:
    """-> profile key, "other" (manual setup), or None (cancel wizard).

    "other" is a reserved sentinel: boards.py rejects user profiles that
    declare it as their key, so it can never collide with a real profile.
    """

    detected_mcu: str
    choices: list[BoardProfileChoice]


@dataclass(frozen=True)
class TextPromptDecision:
    message: str
    default: str = ""
    required: bool = False


class DecisionProvider(Protocol):
    """The single input seam the engine talks to."""

    def confirm(self, req: ConfirmDecision) -> bool: ...

    def choose_device(self, req: ChooseDeviceDecision) -> Optional[str]: ...

    def choose_flash_method(
        self, req: ChooseFlashMethodDecision
    ) -> Optional[tuple[str, Optional[str]]]: ...

    def manual_bootloader_ready(self, req: ManualBootloaderReadyDecision) -> bool: ...

    def mcu_mismatch(self, req: McuMismatchDecision) -> str: ...  # "r" | "d" | "k"

    def choose_ccache_action(self, req: ChooseCcacheActionDecision) -> str: ...

    def choose_board_profile(
        self, req: ChooseBoardProfileDecision
    ) -> Optional[str]: ...  # profile key | "other" | None

    def prompt_text(self, req: TextPromptDecision) -> Optional[str]: ...


class HeadlessDecisionRequired(Exception):
    """Raised by ``HeadlessDecisionProvider(policy="fail")`` when a decision
    cannot be made without a human, so a CLI can exit non-zero rather than
    hang."""


class HeadlessDecisionProvider:
    """A UI-free :class:`DecisionProvider` for tests / future headless CLI.

    ``policy="default"`` answers every prompt with its ``default`` (or a safe
    non-interactive fallback). ``policy="fail"`` raises
    :class:`HeadlessDecisionRequired` whenever a decision is not unambiguous.
    """

    def __init__(self, policy: str = "default") -> None:
        if policy not in ("default", "fail"):
            raise ValueError(f"Unknown policy: {policy!r}")
        self.policy = policy

    def _fail(self, what: str) -> None:
        if self.policy == "fail":
            raise HeadlessDecisionRequired(what)

    def confirm(self, req: ConfirmDecision) -> bool:
        self._fail(f"confirm:{req.id}")
        return req.default

    def choose_device(self, req: ChooseDeviceDecision) -> Optional[str]:
        if len(req.choices) == 1:
            return req.choices[0].key
        self._fail("choose_device")
        return None

    def choose_flash_method(
        self, req: ChooseFlashMethodDecision
    ) -> Optional[tuple[str, Optional[str]]]:
        self._fail("choose_flash_method")
        if req.current_bootloader is not None:
            return (req.current_bootloader, req.current_flash_command)
        return None

    def manual_bootloader_ready(self, req: ManualBootloaderReadyDecision) -> bool:
        # Cannot automate a physical button press -> record a clean failure.
        self._fail("manual_bootloader_ready")
        return False

    def mcu_mismatch(self, req: McuMismatchDecision) -> str:
        self._fail("mcu_mismatch")
        return "k"

    def choose_ccache_action(self, req: ChooseCcacheActionDecision) -> str:
        self._fail("choose_ccache_action")
        return "skip"

    def choose_board_profile(self, req: ChooseBoardProfileDecision) -> Optional[str]:
        # "default" degrades to today's manual wizard (safe); "fail" raises.
        self._fail("choose_board_profile")
        return "other"

    def prompt_text(self, req: TextPromptDecision) -> Optional[str]:
        if req.required and not req.default:
            self._fail("prompt_text")
            return None
        return req.default
