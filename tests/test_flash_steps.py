"""Tier-2 orchestration tests for flash_steps.run_flash_sequence.

Drives the unified bootloader -> flash -> verify sequence with a FakeRunner
(subprocess seam) + a NullSink-backed Emitter + HeadlessDecisionProvider,
monkeypatching device re-enumeration where the USB verify path needs it.
"""

from __future__ import annotations

import pytest
from conftest import FakeDecisionProvider, FakeRunner

from kflash import discovery, flash_steps, runner
from kflash.decisions import HeadlessDecisionProvider
from kflash.events import Emitter, NullSink
from kflash.flash_steps import run_flash_sequence
from kflash.models import DeviceEntry, DiscoveredDevice, GlobalConfig
from kflash.runner import CommandResult

SERIAL = "usb-Klipper_stm32h723xx_ABC123-if00"
PATTERN = "usb-Klipper_stm32h723xx_ABC123*"
UUID = "112233445566"


@pytest.fixture
def em():
    return Emitter(NullSink())


@pytest.fixture
def toolchain(tmp_path):
    """Create klipper/katapult dirs with the scripts the flash funcs probe for,
    plus a firmware file. Returns a config + firmware path."""
    klipper = tmp_path / "klipper"
    katapult = tmp_path / "katapult"
    (klipper / "scripts").mkdir(parents=True)
    (katapult / "scripts").mkdir(parents=True)
    (katapult / "scripts" / "flashtool.py").write_text("# stub\n")
    sd = klipper / "scripts" / "flash-sdcard.sh"
    sd.write_text("#!/bin/sh\n")
    sd.chmod(0o755)
    fw = tmp_path / "klipper.bin"
    fw.write_bytes(b"\x00" * 4096)
    config = GlobalConfig(
        klipper_dir=str(klipper),
        katapult_dir=str(katapult),
        stagger_delay=0.0,
        can_stagger_delay=0.0,
    )
    return {"config": config, "firmware": str(fw), "katapult": str(katapult)}


def _reappear(monkeypatch, filename=SERIAL):
    """Make scan_serial_devices report a device so the USB verify path passes."""
    dev = DiscoveredDevice(path=f"/dev/serial/by-id/{filename}", filename=filename)
    monkeypatch.setattr(discovery, "scan_serial_devices", lambda: [dev])


def _usb_entry(flash_command="katapult", bootloader_method="none"):
    return DeviceEntry(
        key="octo",
        name="Octopus",
        mcu="stm32h723",
        serial_pattern=PATTERN,
        flash_command=flash_command,
        bootloader_method=bootloader_method,
    )


def _can_entry():
    return DeviceEntry(
        key="toolhead",
        name="Toolhead",
        mcu="stm32g0b1",
        serial_pattern=None,
        flash_command="katapult_can",
        bootloader_method="can",
        canbus_uuid=UUID,
        canbus_interface="can0",
    )


# ---------------------------------------------------------------------------


def test_usb_katapult_happy_path(monkeypatch, em, toolchain):
    fake = FakeRunner(default=CommandResult(0))  # flash_katapult streaming -> 0
    runner.set_runner(fake)
    _reappear(monkeypatch)

    step = run_flash_sequence(
        entry=_usb_entry(),
        device_path=f"/dev/serial/by-id/{SERIAL}",
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        klipper_dir=toolchain["config"].klipper_dir,
        katapult_dir=toolchain["katapult"],
        em=em,
        decider=HeadlessDecisionProvider(),
        verify_timeout=2.0,
    )

    assert step.bootloader_ok
    assert step.flash_ok
    assert step.verify_ok
    assert step.success
    assert step.method == "katapult"
    assert step.device_path_new == f"/dev/serial/by-id/{SERIAL}"
    # flash ran via the streaming (inherited-stdio) seam
    assert fake.count(mode="stream") == 1


def test_can_path_verifies_via_flashtool_query(monkeypatch, em, toolchain):
    fake = FakeRunner(default=CommandResult(0))  # flash_katapult_can streaming
    fake.when("-r", CommandResult(0))  # CAN bootloader entry
    fake.when(
        "-q",
        CommandResult(0, stdout=f"Detected UUID: {UUID}, Application: Klipper\n"),
    )
    runner.set_runner(fake)

    step = run_flash_sequence(
        entry=_can_entry(),
        device_path="",
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        klipper_dir=toolchain["config"].klipper_dir,
        katapult_dir=toolchain["katapult"],
        em=em,
        decider=None,  # batch
    )

    assert step.bootloader_ok
    assert step.flash_ok
    assert step.verify_ok
    assert step.method == "katapult_can"
    assert step.device_path_new is None
    assert fake.count(token="-q") >= 1  # CAN verify query happened


def test_sdcard_skips_verify(monkeypatch, em, toolchain):
    fake = FakeRunner(default=CommandResult(0))
    runner.set_runner(fake)
    # scan_serial_devices should NOT be needed; make it explode if called.
    monkeypatch.setattr(
        discovery,
        "scan_serial_devices",
        lambda: (_ for _ in ()).throw(AssertionError("verify should be skipped")),
    )

    entry = _usb_entry(flash_command="flash_sdcard")
    entry.sdcard_board = "btt-octopus-pro-h723-v1.1"
    step = run_flash_sequence(
        entry=entry,
        device_path=f"/dev/serial/by-id/{SERIAL}",
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        klipper_dir=toolchain["config"].klipper_dir,
        katapult_dir=toolchain["katapult"],
        em=em,
        decider=HeadlessDecisionProvider(),
    )

    assert step.flash_ok
    assert step.verify_ok  # verify skipped -> reported verified
    assert step.device_path_new is None


def test_bootloader_method_none_skips_bootloader(monkeypatch, em, toolchain):
    fake = FakeRunner(default=CommandResult(0))
    runner.set_runner(fake)
    _reappear(monkeypatch)

    step = run_flash_sequence(
        entry=_usb_entry(bootloader_method="none"),
        device_path=f"/dev/serial/by-id/{SERIAL}",
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        klipper_dir=toolchain["config"].klipper_dir,
        katapult_dir=toolchain["katapult"],
        em=em,
        decider=HeadlessDecisionProvider(),
        verify_timeout=2.0,
    )

    assert step.bootloader_ok
    # No bootloader subprocess (no -r) was invoked.
    assert fake.count(token="-r") == 0
    assert step.success


def test_manual_uf2_best_effort_verify(monkeypatch, em, toolchain, tmp_path):
    # UF2 mount dir must exist for flash_uf2 to succeed quickly.
    mount = tmp_path / "RPI-RP2"
    mount.mkdir()
    fake = FakeRunner(default=CommandResult(0))
    runner.set_runner(fake)
    # manual bootloader "press Enter": batch path routes through the decider.
    decider = FakeDecisionProvider(manual_ready=True)
    _reappear(monkeypatch)

    entry = _usb_entry(flash_command="uf2_mount", bootloader_method="manual")
    entry.uf2_mount_path = str(mount)
    step = run_flash_sequence(
        entry=entry,
        device_path=f"/dev/serial/by-id/{SERIAL}",
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        klipper_dir=toolchain["config"].klipper_dir,
        katapult_dir=toolchain["katapult"],
        em=em,
        decider=decider,
        batch=True,  # batch -> manual instruction via ManualBootloaderReadyDecision
        verify_timeout=2.0,
    )

    assert step.bootloader_ok
    assert step.flash_ok
    assert step.method == "uf2_mount"
    # USB verify was still attempted (best-effort) and matched.
    assert step.verify_ok
    # The manual gate was routed through the decider with batch=True (no input()).
    assert len(decider.manual_calls) == 1
    assert decider.manual_calls[0].batch is True


def test_flash_failure_sets_error_and_skips_verify(monkeypatch, em, toolchain):
    fake = FakeRunner(default=CommandResult(1))  # flash streaming -> non-zero
    runner.set_runner(fake)
    monkeypatch.setattr(
        discovery,
        "scan_serial_devices",
        lambda: (_ for _ in ()).throw(AssertionError("verify must not run on flash fail")),
    )

    step = run_flash_sequence(
        entry=_usb_entry(),
        device_path=f"/dev/serial/by-id/{SERIAL}",
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        klipper_dir=toolchain["config"].klipper_dir,
        katapult_dir=toolchain["katapult"],
        em=em,
        decider=HeadlessDecisionProvider(),
    )

    assert not step.flash_ok
    assert not step.verify_ok
    assert not step.success
    assert step.error_message  # flash error recorded


def test_batch_bootloader_failure_no_retry(monkeypatch, em, toolchain):
    # CAN bootloader entry fails; decider=None (batch) => exactly one attempt.
    fake = FakeRunner(default=CommandResult(0))
    fake.when("-r", CommandResult(1))  # bootloader -r fails
    runner.set_runner(fake)

    step = run_flash_sequence(
        entry=_can_entry(),
        device_path="",
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        klipper_dir=toolchain["config"].klipper_dir,
        katapult_dir=toolchain["katapult"],
        em=em,
        decider=None,
    )

    assert not step.bootloader_ok
    assert not step.flash_ok
    assert fake.count(token="-r") == 1  # no retry in batch mode


@pytest.mark.parametrize("mode", ["usb_timeout", "can_not_found"])
def test_verify_failure_maps_error_reason_not_message(monkeypatch, em, toolchain, mode):
    """A flash that succeeds but fails verification populates ``error_reason``
    (the verify diagnostic) and leaves ``error_message`` None (that field is for
    bootloader/flash failures)."""
    fake = FakeRunner(default=CommandResult(0))
    fake.when("-r", CommandResult(0))  # CAN bootloader entry succeeds
    runner.set_runner(fake)

    if mode == "usb_timeout":
        # Flash succeeds; the USB device never reappears -> verify times out.
        monkeypatch.setattr(
            flash_steps,
            "wait_for_device",
            lambda *a, **k: (False, None, "Timeout after 0s waiting for device"),
        )
        entry = _usb_entry()  # bootloader_method="none"
        device_path = f"/dev/serial/by-id/{SERIAL}"
        decider = HeadlessDecisionProvider()
    else:
        # Flash succeeds; the CAN device never returns as Application: Klipper.
        monkeypatch.setattr(
            flash_steps,
            "verify_can_device_after_flash",
            lambda *a, **k: (False, f"Device {UUID} did not return as 'Klipper'"),
        )
        entry = _can_entry()
        device_path = ""
        decider = None  # batch

    step = run_flash_sequence(
        entry=entry,
        device_path=device_path,
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        klipper_dir=toolchain["config"].klipper_dir,
        katapult_dir=toolchain["katapult"],
        em=em,
        decider=decider,
        verify_timeout=0.5,
    )

    assert step.flash_ok
    assert step.verify_ok is False
    assert step.error_reason  # verify diagnostic populated
    assert step.error_message is None  # reserved for bootloader/flash failures
