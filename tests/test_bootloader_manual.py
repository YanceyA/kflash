"""Tests for the manual-bootloader gate routing through the DecisionProvider.

Phase 3 eliminated the bare ``input()`` in ``_enter_manual``; the batch path now
passes a real decider with ``batch=True`` instead of ``decider=None``.
"""

from __future__ import annotations

from conftest import FakeDecisionProvider

from kflash.bootloader import enter_bootloader
from kflash.decisions import HeadlessDecisionProvider
from kflash.models import DeviceEntry


def _manual_uf2_entry():
    # uf2_mount short-circuits re-enumeration, so the gate is the whole story.
    return DeviceEntry(
        key="pico",
        name="Pico",
        mcu="rp2040",
        serial_pattern="usb-Klipper_rp2040_ABC*",
        flash_command="uf2_mount",
        bootloader_method="manual",
    )


def test_manual_batch_routes_through_decider_with_batch_flag():
    decider = FakeDecisionProvider(manual_ready=True)
    result = enter_bootloader(
        device_path="/dev/serial/by-id/usb-Klipper_rp2040_ABC-if00",
        device_entry=_manual_uf2_entry(),
        klipper_dir="/tmp/klipper",
        katapult_dir="/tmp/katapult",
        stagger_delay=0.0,
        decider=decider,
        batch=True,
    )
    assert result.success
    assert len(decider.manual_calls) == 1
    assert decider.manual_calls[0].batch is True
    assert decider.manual_calls[0].device_name == "Pico"


def test_manual_interactive_uses_batch_false():
    decider = FakeDecisionProvider(manual_ready=True)
    enter_bootloader(
        device_path="/dev/serial/by-id/usb-Klipper_rp2040_ABC-if00",
        device_entry=_manual_uf2_entry(),
        klipper_dir="/tmp/klipper",
        katapult_dir="/tmp/katapult",
        stagger_delay=0.0,
        decider=decider,
        batch=False,
    )
    assert decider.manual_calls[0].batch is False


def test_manual_declined_is_clean_failure():
    decider = FakeDecisionProvider(manual_ready=False)
    result = enter_bootloader(
        device_path="/dev/serial/by-id/usb-Klipper_rp2040_ABC-if00",
        device_entry=_manual_uf2_entry(),
        klipper_dir="/tmp/klipper",
        katapult_dir="/tmp/katapult",
        stagger_delay=0.0,
        decider=decider,
        batch=True,
    )
    assert not result.success
    assert "cancelled" in (result.error_message or "").lower()


def test_manual_without_decider_fails_without_input():
    # No decision provider on a manual gate -> clean failure, never input().
    result = enter_bootloader(
        device_path="/dev/serial/by-id/usb-Klipper_rp2040_ABC-if00",
        device_entry=_manual_uf2_entry(),
        klipper_dir="/tmp/klipper",
        katapult_dir="/tmp/katapult",
        stagger_delay=0.0,
        decider=None,
        batch=True,
    )
    assert not result.success
    assert "decision provider" in (result.error_message or "").lower()


def test_headless_manual_gate_records_failure():
    # HeadlessDecisionProvider cannot press a physical button -> False.
    decider = HeadlessDecisionProvider()
    result = enter_bootloader(
        device_path="/dev/serial/by-id/usb-Klipper_rp2040_ABC-if00",
        device_entry=_manual_uf2_entry(),
        klipper_dir="/tmp/klipper",
        katapult_dir="/tmp/katapult",
        stagger_delay=0.0,
        decider=decider,
        batch=True,
    )
    assert not result.success
