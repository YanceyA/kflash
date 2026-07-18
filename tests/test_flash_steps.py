"""Tier-2 orchestration tests for flash_steps.run_flash_sequence.

Drives the unified bootloader -> flash -> verify sequence with a FakeRunner
(subprocess seam) + a NullSink-backed Emitter + HeadlessDecisionProvider,
monkeypatching device re-enumeration where the USB verify path needs it.
"""

from __future__ import annotations

import pytest
from conftest import FakeDecisionProvider, FakeRunner, RecordingSink

from kflash import discovery, flash_steps, runner
from kflash.decisions import HeadlessDecisionProvider
from kflash.events import Emitter, NullSink
from kflash.flash_steps import run_flash_sequence
from kflash.flasher import (
    _flash_line_emitter,
    execute_flash,
    flash_katapult,
    flash_katapult_can,
)
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
    # flash ran via the line-streaming seam (output routed through the emitter)
    assert fake.count(mode="stream_lines") == 1


# ---------------------------------------------------------------------------
# Flash output streaming through the event emitter (flasher.py)
# ---------------------------------------------------------------------------


def test_flash_katapult_streams_output_through_emitter(toolchain):
    fake = FakeRunner(default=CommandResult(0))
    fake.when_lines(
        "-f",
        [
            "Flashing 'out/klipper.bin'...",
            "[##......] 25%",
            "[########] 100%",
            "Verifying...",
        ],
    )
    runner.set_runner(fake)
    sink = RecordingSink()
    em = Emitter(sink)

    result = flash_katapult(
        device_path=f"/dev/serial/by-id/{SERIAL}",
        firmware_path=toolchain["firmware"],
        katapult_dir=toolchain["katapult"],
        klipper_dir=toolchain["config"].klipper_dir,
        em=em,
    )

    assert result.success
    events = sink.events
    assert [e.kind for e in events] == ["info", "progress", "progress", "info"]
    assert events[0].section == "Flash"
    assert events[0].message == "Flashing 'out/klipper.bin'..."
    assert events[1].progress == pytest.approx(0.25)
    assert events[1].section == "Flash"
    assert events[2].progress == pytest.approx(1.0)
    assert events[3].message == "Verifying..."
    assert all(e.section == "Flash" for e in events)
    # output was line-streamed exactly once
    assert fake.count(mode="stream_lines") == 1


def test_flash_katapult_can_streams_output_through_emitter(toolchain):
    fake = FakeRunner(default=CommandResult(0))
    fake.when_lines("-f", ["[####....] 50%", "Success"])
    runner.set_runner(fake)
    sink = RecordingSink()
    em = Emitter(sink)

    result = flash_katapult_can(
        uuid=UUID,
        interface="can0",
        firmware_path=toolchain["firmware"],
        katapult_dir=toolchain["katapult"],
        em=em,
    )

    assert result.success
    assert [e.kind for e in sink.events] == ["progress", "info"]
    assert sink.events[0].progress == pytest.approx(0.5)
    assert sink.events[1].message == "Success"
    assert fake.count(mode="stream_lines") == 1


def test_execute_flash_forwards_em_to_katapult(toolchain):
    fake = FakeRunner(default=CommandResult(0))
    fake.when_lines("-f", ["[##......] 20%"])
    runner.set_runner(fake)
    sink = RecordingSink()
    em = Emitter(sink)

    result = execute_flash(
        entry=_usb_entry(),
        device_path=f"/dev/serial/by-id/{SERIAL}",
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        em=em,
    )

    assert result.success
    progress_events = [e for e in sink.events if e.kind == "progress"]
    assert len(progress_events) == 1
    assert progress_events[0].progress == pytest.approx(0.2)


def test_flash_katapult_without_emitter_does_not_crash(toolchain):
    fake = FakeRunner(default=CommandResult(0))
    fake.when_lines("-f", ["[##......] 20%", "done"])
    runner.set_runner(fake)

    result = flash_katapult(
        device_path=f"/dev/serial/by-id/{SERIAL}",
        firmware_path=toolchain["firmware"],
        katapult_dir=toolchain["katapult"],
        klipper_dir=toolchain["config"].klipper_dir,
        em=None,
    )

    assert result.success


@pytest.mark.parametrize(
    "line,expected_kind,expected_progress",
    [
        # Two percentages in one line -> the first match wins.
        ("[####....] 40% (est 80% remaining)", "progress", 0.40),
        # Out-of-range values are not treated as progress.
        ("150% complete", "info", None),
        ("100.5% complete", "info", None),
        # A percentage sliced out of a longer digit run would be spurious
        # (e.g. "100" out of "5100%", or "000" out of "1000%") -- must not
        # be reported as progress at all.
        ("5100% overflow", "info", None),
        ("1000% overflow", "info", None),
        # A negative number must not be read as an in-range percentage.
        ("-100% delta", "info", None),
    ],
)
def test_flash_line_emitter_percent_boundary_cases(
    line, expected_kind, expected_progress
):
    sink = RecordingSink()
    em = Emitter(sink)
    on_line = _flash_line_emitter(em)

    on_line(line)

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.kind == expected_kind
    assert event.section == "Flash"
    if expected_progress is not None:
        assert event.progress == pytest.approx(expected_progress)
    else:
        assert event.progress is None


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


# ---------------------------------------------------------------------------
# load_and_validate_config: seeding + forced-review invariant
# ---------------------------------------------------------------------------


def _seed_env(monkeypatch, tmp_path):
    """Isolated XDG config dir + klipper dir. Returns the klipper path."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    klipper = tmp_path / "klipper"
    klipper.mkdir()
    return klipper


def test_no_cache_seeds_from_default_and_forces_menuconfig(monkeypatch, tmp_path):
    from kflash.config import ConfigManager, get_defaults_dir

    klipper = _seed_env(monkeypatch, tmp_path)
    # A default seed for this MCU exists on disk.
    defaults = get_defaults_dir()
    defaults.mkdir(parents=True, exist_ok=True)
    (defaults / "stm32h723.config").write_text(
        'CONFIG_MCU="stm32h723xx"\n', encoding="utf-8"
    )

    sink = RecordingSink()
    em = Emitter(sink)

    launched = {"n": 0}

    def fake_menuconfig(kdir, cfg_path):
        # Menuconfig "saves": the klipper .config is already present (loaded from
        # the just-seeded cache), so save_cached_config() below clears the marker.
        launched["n"] += 1
        return (0, True)

    monkeypatch.setattr(flash_steps, "run_menuconfig", fake_menuconfig)

    outcome = flash_steps.load_and_validate_config(
        entry=_usb_entry(),
        device_key="octo",
        klipper_dir=str(klipper),
        em=em,
        decider=HeadlessDecisionProvider(),
        skip_menuconfig=True,  # would normally skip -- seeding must override
        require_menuconfig=False,
    )

    assert outcome.ok
    assert outcome.ran_menuconfig
    assert launched["n"] == 1  # menuconfig forced despite skip_menuconfig=True
    text = sink.text()
    assert "Config seeded from" in text  # Config phase announced the seed
    assert "review required" in text
    # After the stubbed menuconfig save, the review marker is cleared.
    assert not ConfigManager("octo", str(klipper)).is_seeded()


def test_no_cache_prefers_board_fragment_over_mcu_default(monkeypatch, tmp_path):
    import json

    from kflash.boards import get_user_boards_dir
    from kflash.config import get_defaults_dir

    klipper = _seed_env(monkeypatch, tmp_path)

    # Both a board fragment AND an MCU default exist; the board fragment wins.
    defaults = get_defaults_dir()
    defaults.mkdir(parents=True, exist_ok=True)
    (defaults / "stm32h723.config").write_text(
        'CONFIG_MCU="stm32h723xx"\nCONFIG_FROM_DEFAULT=y\n', encoding="utf-8"
    )
    boards_dir = get_user_boards_dir()
    boards_dir.mkdir(parents=True, exist_ok=True)
    (boards_dir / "btt-x.json").write_text(
        json.dumps(
            {
                "key": "btt-x",
                "name": "BTT X",
                "mcu": "stm32h723",
                "bootloader_method": "usb",
                "flash_command": "katapult",
                "config_fragment": True,
            }
        ),
        encoding="utf-8",
    )
    (boards_dir / "btt-x.config").write_text(
        'CONFIG_MCU="stm32h723xx"\nCONFIG_FROM_BOARD=y\n', encoding="utf-8"
    )

    sink = RecordingSink()
    em = Emitter(sink)

    def fake_menuconfig(kdir, cfg_path):
        # "saves": the klipper .config (loaded from the just-seeded board
        # fragment) stands, so save_cached_config() clears the marker after.
        return (0, True)

    monkeypatch.setattr(flash_steps, "run_menuconfig", fake_menuconfig)

    entry = _usb_entry()
    entry.board = "btt-x"

    outcome = flash_steps.load_and_validate_config(
        entry=entry,
        device_key="octo",
        klipper_dir=str(klipper),
        em=em,
        decider=HeadlessDecisionProvider(),
        skip_menuconfig=True,
        require_menuconfig=False,
    )

    assert outcome.ok
    from kflash.config import ConfigManager

    mgr = ConfigManager("octo", str(klipper))
    # The board fragment (not the MCU default) is what got seeded/cached.
    cached = mgr.cache_path.read_text(encoding="utf-8")
    assert "CONFIG_FROM_BOARD" in cached
    assert "CONFIG_FROM_DEFAULT" not in cached
    # The forced-review announcement named the board seed source.
    assert "Config seeded from board:btt-x" in sink.text()


def test_board_fragment_drift_emits_warning(monkeypatch, tmp_path):
    """When menuconfig drops a board-fragment symbol (upstream Kconfig rename),
    load_and_validate_config surfaces it as an em.warn line, not silently."""
    import json

    from kflash.boards import get_user_boards_dir

    klipper = _seed_env(monkeypatch, tmp_path)

    boards_dir = get_user_boards_dir()
    boards_dir.mkdir(parents=True, exist_ok=True)
    (boards_dir / "btt-x.json").write_text(
        json.dumps(
            {
                "key": "btt-x",
                "name": "BTT X",
                "mcu": "stm32h723",
                "bootloader_method": "usb",
                "flash_command": "katapult",
                "config_fragment": True,
            }
        ),
        encoding="utf-8",
    )
    (boards_dir / "btt-x.config").write_text(
        'CONFIG_MCU="stm32h723xx"\nCONFIG_STM32_FLASH_START_20200=y\n',
        encoding="utf-8",
    )

    sink = RecordingSink()
    em = Emitter(sink)

    def fake_menuconfig(kdir, cfg_path):
        # kconfiglib drops the renamed offset symbol on load and re-saves.
        from pathlib import Path

        Path(cfg_path).write_text(
            'CONFIG_MCU="stm32h723xx"\nCONFIG_STM32_FLASH_START_20000=y\n',
            encoding="utf-8",
        )
        return (0, True)

    monkeypatch.setattr(flash_steps, "run_menuconfig", fake_menuconfig)

    entry = _usb_entry()
    entry.board = "btt-x"

    outcome = flash_steps.load_and_validate_config(
        entry=entry,
        device_key="octo",
        klipper_dir=str(klipper),
        em=em,
        decider=HeadlessDecisionProvider(),
        skip_menuconfig=True,
        require_menuconfig=False,
    )

    assert outcome.ok
    warns = [e.message for e in sink.events if e.kind == "warn"]
    assert any("CONFIG_STM32_FLASH_START_20200=y" in w for w in warns)
    assert any("not recognized" in w for w in warns)


def test_board_fragment_no_drift_no_warning(monkeypatch, tmp_path):
    """A board fragment whose symbols all survive emits no drift warning."""
    import json

    from kflash.boards import get_user_boards_dir

    klipper = _seed_env(monkeypatch, tmp_path)

    boards_dir = get_user_boards_dir()
    boards_dir.mkdir(parents=True, exist_ok=True)
    (boards_dir / "btt-x.json").write_text(
        json.dumps(
            {
                "key": "btt-x",
                "name": "BTT X",
                "mcu": "stm32h723",
                "bootloader_method": "usb",
                "flash_command": "katapult",
                "config_fragment": True,
            }
        ),
        encoding="utf-8",
    )
    (boards_dir / "btt-x.config").write_text(
        'CONFIG_MCU="stm32h723xx"\n', encoding="utf-8"
    )

    sink = RecordingSink()
    em = Emitter(sink)

    def fake_menuconfig(kdir, cfg_path):
        from pathlib import Path

        Path(cfg_path).write_text(
            'CONFIG_MCU="stm32h723xx"\nCONFIG_USB=y\n', encoding="utf-8"
        )
        return (0, True)

    monkeypatch.setattr(flash_steps, "run_menuconfig", fake_menuconfig)

    entry = _usb_entry()
    entry.board = "btt-x"

    flash_steps.load_and_validate_config(
        entry=entry,
        device_key="octo",
        klipper_dir=str(klipper),
        em=em,
        decider=HeadlessDecisionProvider(),
        skip_menuconfig=True,
        require_menuconfig=False,
    )

    warns = [e.message for e in sink.events if e.kind == "warn"]
    assert not any("not recognized" in w for w in warns)


def test_seeded_cache_overrides_skip_menuconfig(monkeypatch, tmp_path):
    from kflash.config import ConfigManager

    klipper = _seed_env(monkeypatch, tmp_path)
    # A cache exists but is still flagged as seeded/unreviewed.
    mgr = ConfigManager("octo", str(klipper))
    mgr.cache_path.parent.mkdir(parents=True, exist_ok=True)
    mgr.cache_path.write_text('CONFIG_MCU="stm32h723xx"\n', encoding="utf-8")
    mgr.seed_marker_path.write_text("mcu-default:stm32h723\n", encoding="utf-8")
    assert mgr.is_seeded()

    sink = RecordingSink()
    em = Emitter(sink)

    launched = {"n": 0}

    def fake_menuconfig(kdir, cfg_path):
        launched["n"] += 1
        return (0, True)

    monkeypatch.setattr(flash_steps, "run_menuconfig", fake_menuconfig)

    outcome = flash_steps.load_and_validate_config(
        entry=_usb_entry(),
        device_key="octo",
        klipper_dir=str(klipper),
        em=em,
        decider=HeadlessDecisionProvider(),
        skip_menuconfig=True,  # ignored because the cache is seeded
        require_menuconfig=False,
    )

    assert outcome.ok
    assert launched["n"] == 1  # menuconfig launched anyway
    assert "review required" in sink.text()
    # Review satisfied -> marker cleared.
    assert not ConfigManager("octo", str(klipper)).is_seeded()


# ---------------------------------------------------------------------------
# resolve_target_mcu_version / emit_host_and_mcu_versions
# ---------------------------------------------------------------------------

HOST_VER = "v2026.07.00-2-g888f2672"
NHK_VER = "v2026.04.00-11-g90510d7a"


def _nhk_entry(mcu_name="mcu nhk"):
    return DeviceEntry(
        key="ldo-nitehawk-36",
        name="LDO Nitehawk-36",
        mcu="rp2040",
        serial_pattern="usb-Klipper_rp2040_ABC123*",
        flash_command="katapult",
        bootloader_method="usb",
        mcu_name=mcu_name,
    )


class TestResolveTargetMcuVersion:
    def test_strict_lookup_by_mcu_name(self):
        versions = {
            "main": HOST_VER,
            "stm32h723xx": HOST_VER,
            "nhk": NHK_VER,
            "rp2040": NHK_VER,
        }
        assert flash_steps.resolve_target_mcu_version(_nhk_entry(), versions) == "nhk"

    def test_mcu_name_mcu_maps_to_main(self):
        entry = DeviceEntry(
            key="octo",
            name="Octopus",
            mcu="stm32h723",
            serial_pattern=PATTERN,
            mcu_name="mcu",
        )
        versions = {"main": HOST_VER, "stm32h723xx": HOST_VER}
        assert flash_steps.resolve_target_mcu_version(entry, versions) == "main"

    def test_not_reporting_returns_none_not_main(self):
        # Regression: klippy in error state -> only the main MCU reports.
        # The Nitehawk must NOT resolve to "main" (that fed a false
        # "firmware already up-to-date" prompt with the wrong board's version).
        versions = {"main": HOST_VER, "stm32h723xx": HOST_VER}
        assert flash_steps.resolve_target_mcu_version(_nhk_entry(), versions) is None

    def test_not_reporting_ignores_chip_alias_of_other_board(self):
        # Another rp2040 board's chip-type alias must not be borrowed when
        # the device's own MCU object is not reporting.
        versions = {"main": HOST_VER, "hbb": "v2026.01.00-0-gaaaaaaa",
                    "rp2040": "v2026.01.00-0-gaaaaaaa"}
        assert flash_steps.resolve_target_mcu_version(_nhk_entry(), versions) is None

    def test_mcu_name_lookup_is_case_insensitive(self):
        entry = DeviceEntry(
            key="huvud",
            name="Huvud",
            mcu="rp2040",
            serial_pattern="usb-Klipper_rp2040_DEF456*",
            mcu_name="mcu HBB",
        )
        versions = {"hbb": NHK_VER}
        assert flash_steps.resolve_target_mcu_version(entry, versions) == "hbb"

    def test_legacy_fuzzy_name_match_without_mcu_name(self):
        entry = DeviceEntry(
            key="nhk-v13",
            name="Nhk v1.3",
            mcu="rp2040",
            serial_pattern="usb-Klipper_rp2040_ABC123*",
            mcu_name=None,
        )
        versions = {"main": HOST_VER, "nhk": NHK_VER}
        assert flash_steps.resolve_target_mcu_version(entry, versions) == "nhk"

    def test_legacy_chip_match_without_mcu_name(self):
        entry = DeviceEntry(
            key="toolhead",
            name="Toolhead",
            mcu="rp2040",
            serial_pattern="usb-Klipper_rp2040_ABC123*",
            mcu_name=None,
        )
        versions = {"main": HOST_VER, "rp2040": NHK_VER}
        assert flash_steps.resolve_target_mcu_version(entry, versions) == "rp2040"

    def test_legacy_no_match_returns_none_not_main(self):
        entry = DeviceEntry(
            key="toolhead",
            name="Toolhead",
            mcu="rp2040",
            serial_pattern="usb-Klipper_rp2040_ABC123*",
            mcu_name=None,
        )
        versions = {"main": HOST_VER, "stm32h723xx": HOST_VER}
        assert flash_steps.resolve_target_mcu_version(entry, versions) is None


class TestEmitHostAndMcuVersions:
    def test_warns_when_target_unknown(self):
        sink = RecordingSink()
        em = Emitter(sink)
        flash_steps.emit_host_and_mcu_versions(em, HOST_VER, {"main": HOST_VER}, None)
        assert "not reported" in sink.text()

    def test_no_warning_when_target_known(self):
        sink = RecordingSink()
        em = Emitter(sink)
        flash_steps.emit_host_and_mcu_versions(em, HOST_VER, {"main": HOST_VER}, "main")
        assert "not reported" not in sink.text()
