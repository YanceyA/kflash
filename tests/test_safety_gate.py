"""Tier-2 tests for ``flash_steps.moonraker_safety_gate`` three-way branch.

Drives the gate with a monkeypatched ``get_print_status`` and a scripted
confirm provider (a stand-in for :class:`HeadlessDecisionProvider` where the
PROCEED path needs a ``True`` answer that the headless default cannot give).
"""

from __future__ import annotations

import pytest

from kflash import flash_steps
from kflash.decisions import HeadlessDecisionProvider
from kflash.events import Emitter, NullSink
from kflash.flash_steps import SafetyGate, moonraker_safety_gate
from kflash.models import PrintStatus


@pytest.fixture
def em():
    return Emitter(NullSink())


class _Confirmer:
    """Minimal DecisionProvider: ``confirm`` returns a fixed answer, recording
    the ids it was asked about."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.seen: list[str] = []

    def confirm(self, req) -> bool:
        self.seen.append(req.id)
        return self.answer


def _patch_status(monkeypatch, status):
    monkeypatch.setattr(flash_steps, "get_print_status", lambda: status)


@pytest.mark.parametrize(
    "proceed,expected",
    [(True, SafetyGate.PROCEED), (False, SafetyGate.CANCELLED)],
)
def test_moonraker_down_confirm_drives_outcome(monkeypatch, em, proceed, expected):
    _patch_status(monkeypatch, None)
    decider = _Confirmer(proceed)
    result = moonraker_safety_gate(em=em, decider=decider, label="Flash")
    assert result is expected
    assert "no_moonraker" in decider.seen


@pytest.mark.parametrize(
    "proceed,expected",
    [(True, SafetyGate.PROCEED), (False, SafetyGate.CANCELLED)],
)
def test_error_state_confirm_drives_outcome(monkeypatch, em, proceed, expected):
    _patch_status(
        monkeypatch, PrintStatus(state="error", filename=None, progress=0.0)
    )
    decider = _Confirmer(proceed)
    result = moonraker_safety_gate(em=em, decider=decider, label="Flash")
    assert result is expected
    assert "printer_error_state" in decider.seen


def test_printing_state_blocks_and_tolerates_none_progress(monkeypatch, em):
    # progress=None exercises the ``(print_status.progress or 0.0)`` guard --
    # it must not raise while formatting the "Print in progress" detail.
    _patch_status(
        monkeypatch,
        PrintStatus(state="printing", filename=None, progress=None),  # type: ignore[arg-type]
    )
    result = moonraker_safety_gate(
        em=em, decider=HeadlessDecisionProvider(), label="Flash All"
    )
    assert result is SafetyGate.BLOCKED


def test_safe_state_proceeds(monkeypatch, em):
    _patch_status(
        monkeypatch, PrintStatus(state="standby", filename=None, progress=0.0)
    )
    result = moonraker_safety_gate(
        em=em, decider=HeadlessDecisionProvider(), label="Flash"
    )
    assert result is SafetyGate.PROCEED
