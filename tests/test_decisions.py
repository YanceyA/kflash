"""Unit tests for the ``DecisionProvider`` seam.

Focuses on the ``ChooseBoardProfileDecision`` added for the board-profile wizard
step: its behaviour under both ``HeadlessDecisionProvider`` policies, the
``FakeDecisionProvider`` scripting/recording contract, and a structural
conformance check that every concrete provider implements the full protocol
surface (so a newly added protocol method can never be silently unimplemented on
one provider).
"""

from __future__ import annotations

import pytest

from kflash.decisions import (
    BoardProfileChoice,
    ChooseBoardProfileDecision,
    DecisionProvider,
    HeadlessDecisionProvider,
    HeadlessDecisionRequired,
)

# Every method on the DecisionProvider protocol, derived from the Protocol
# itself so a newly added method can never be silently missing from a
# provider (a hand-maintained set would pass vacuously for a forgotten one).
PROVIDER_METHODS = {n for n in vars(DecisionProvider) if not n.startswith("_")}


def _sample_request() -> ChooseBoardProfileDecision:
    return ChooseBoardProfileDecision(
        detected_mcu="stm32h723",
        choices=[
            BoardProfileChoice(
                key="btt-octopus-pro-h723",
                label="BTT Octopus Pro v1.0/1.1 (STM32H723)",
                notes="128KB bootloader, 25MHz crystal",
            ),
            BoardProfileChoice(
                key="btt-octopus-pro-h723-can",
                label="BTT Octopus Pro (STM32H723, CAN bridge)",
                notes="USB-to-CAN bridge",
            ),
        ],
    )


# --------------------------------------------------------------------------- #
# HeadlessDecisionProvider -- default degrades to manual setup, fail raises.
# --------------------------------------------------------------------------- #
def test_headless_default_returns_other() -> None:
    provider = HeadlessDecisionProvider(policy="default")
    # "other" == the manual wizard, exactly today's non-interactive behaviour.
    assert provider.choose_board_profile(_sample_request()) == "other"


def test_headless_fail_raises() -> None:
    provider = HeadlessDecisionProvider(policy="fail")
    with pytest.raises(HeadlessDecisionRequired):
        provider.choose_board_profile(_sample_request())


# --------------------------------------------------------------------------- #
# FakeDecisionProvider -- scripts an answer and records the request.
# --------------------------------------------------------------------------- #
def test_fake_scripts_and_records() -> None:
    from tests.conftest import FakeDecisionProvider

    provider = FakeDecisionProvider(board_profile="btt-octopus-pro-h723")
    req = _sample_request()
    assert provider.choose_board_profile(req) == "btt-octopus-pro-h723"
    assert provider.board_profile_calls == [req]


def test_fake_defaults_to_other() -> None:
    from tests.conftest import FakeDecisionProvider

    provider = FakeDecisionProvider()
    assert provider.choose_board_profile(_sample_request()) == "other"


# --------------------------------------------------------------------------- #
# Every concrete provider implements the full protocol surface.
# --------------------------------------------------------------------------- #
def test_all_providers_implement_full_protocol() -> None:
    from kflash.decisions import DecisionProvider
    from tests.conftest import FakeDecisionProvider

    # The protocol itself declares the new method.
    assert "choose_board_profile" in dir(DecisionProvider)

    for provider in (HeadlessDecisionProvider(), FakeDecisionProvider()):
        for name in PROVIDER_METHODS:
            assert callable(getattr(provider, name, None)), (
                f"{type(provider).__name__} missing protocol method {name!r}"
            )
