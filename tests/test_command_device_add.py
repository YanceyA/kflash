"""Engine-level tests for cmd_add_device's post-add menuconfig block.

Focus: the config-seeding hook added at the "Run menuconfig now?" step in
kflash/commands/device_add.py. When a brand-new device has no cached config
yet, the block must seed from the MCU's default config (kflash.config
ConfigManager.seed_from_default) before menuconfig launches, and the safety
invariant -- a seeded config always gets one menuconfig review before it can
be used for build/flash -- must survive every branch of the mismatch loop.

Drives the real wizard (not a hand-carved shortcut): only the OS-touching
seams (TTY check, USB scan/matching, Moonraker MCU-name lookup, menuconfig
subprocess) are stubbed, mirroring tests/ui/test_add_device.py's
`test_real_cmd_add_device_registers_usb`.
"""

from __future__ import annotations

import json
from typing import Optional

from conftest import FakeDecisionProvider, RecordingSink

from kflash.commands import device_add as dadd
from kflash.commands.device_add import cmd_add_device
from kflash.config import ConfigManager, get_defaults_dir
from kflash.events import Emitter
from kflash.models import DiscoveredDevice
from kflash.registry import Registry


class ScriptedAddDecider(FakeDecisionProvider):
    """Drives the full add-device wizard to a saved USB device.

    Subclasses conftest's FakeDecisionProvider and overrides only the two
    methods that genuinely differ: ``choose_flash_method`` (the base always
    answers ``None`` there, which cancels the wizard before a device is ever
    created) and ``mcu_mismatch`` (records call count + returns a scripted
    choice). Everything else -- confirm/prompt_text/choose_device/etc. -- is
    the base behaviour, including its call recording.
    """

    def __init__(self, prompts=None, confirms=None, mismatch_choice="k"):
        super().__init__(confirms=confirms, prompts=prompts)
        self.mismatch_choice = mismatch_choice
        self.mismatch_calls = 0

    def choose_flash_method(self, req):
        return ("usb", "katapult")

    def mcu_mismatch(self, req) -> str:
        self.mismatch_calls += 1
        return self.mismatch_choice


class ProfileAddDecider(ScriptedAddDecider):
    """ScriptedAddDecider that records choose_flash_method calls and lets the
    caller script the board-profile pick."""

    def __init__(self, *, board_profile="other", prompts=None, confirms=None):
        super().__init__(prompts=prompts, confirms=confirms)
        self.board_profile = board_profile
        self.flash_method_calls = 0

    def choose_flash_method(self, req):
        self.flash_method_calls += 1
        return ("usb", "katapult")


def _write_board(key, *, mcu="stm32h723", bootloader_method="usb",
                 flash_command="katapult", config_fragment=False,
                 fragment_text=None, sub_fields=None, role=None):
    """Drop a user board profile (+ optional fragment) into the user boards dir.

    Requires XDG_CONFIG_HOME already isolated (call after _setup()).
    """
    from kflash.boards import get_user_boards_dir

    boards_dir = get_user_boards_dir()
    boards_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "key": key,
        "name": f"Profile {key}",
        "mcu": mcu,
        "bootloader_method": bootloader_method,
        "flash_command": flash_command,
        "config_fragment": config_fragment,
    }
    if sub_fields is not None:
        data["sub_fields"] = sub_fields
    if role is not None:
        data["role"] = role
    (boards_dir / f"{key}.json").write_text(json.dumps(data), encoding="utf-8")
    if fragment_text is not None:
        (boards_dir / f"{key}.config").write_text(fragment_text, encoding="utf-8")
    return key


def _registry_with_can_seed(tmp_path, klipper_dir):
    """A registry with one pre-existing CAN device.

    Two things this buys the new USB add: (1) it isn't the "first device",
    so the wizard skips the global-config prompt and uses the klipper_dir
    set up here directly, and (2) the CAN entry has serial_pattern=None, so
    it's transparently skipped by every serial-pattern-matching loop the
    wizard runs (no extra stubbing needed for those checks).
    """
    path = tmp_path / "devices.json"
    path.write_text(
        json.dumps(
            {
                "global": {
                    "klipper_dir": str(klipper_dir),
                    "katapult_dir": str(tmp_path / "katapult"),
                },
                "devices": {
                    "toolhead": {
                        "name": "Toolhead",
                        "mcu": "stm32g0",
                        "canbus_uuid": "aabbccddeeff",
                        "canbus_interface": "can0",
                        "flash_command": "katapult_can",
                        "bootloader_method": "can",
                    }
                },
                "blocked_devices": [],
            }
        )
    )
    return Registry(str(path))


def _setup(monkeypatch, tmp_path, default_config_text: Optional[str]):
    """Common wizard scaffolding: registry, tmp klipper dir, discovery
    stubs, and an isolated XDG config home.

    ``default_config_text`` seeds ``defaults/stm32h723.config`` when a string;
    pass ``None`` to skip the write entirely (no defaults dir at all), so
    ``seed_from_default()`` finds nothing and the fresh-config path runs.

    Returns (registry, klipper_dir, selected_device).
    """
    klipper_dir = tmp_path / "klipper"
    klipper_dir.mkdir()
    registry = _registry_with_can_seed(tmp_path, klipper_dir)

    selected = DiscoveredDevice(
        path="/dev/serial/by-id/usb-Klipper_stm32h723xx_TEST01-if00",
        filename="usb-Klipper_stm32h723xx_TEST01-if00",
    )

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    if default_config_text is not None:
        defaults = get_defaults_dir()
        defaults.mkdir(parents=True, exist_ok=True)
        (defaults / "stm32h723.config").write_text(default_config_text, encoding="utf-8")

    class _Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(dadd.sys, "stdin", _Tty())
    monkeypatch.setattr(dadd, "scan_serial_devices", lambda: [selected])
    monkeypatch.setattr(dadd, "match_devices", lambda pattern, devices: [selected])
    monkeypatch.setattr(dadd, "extract_mcu_from_serial", lambda name: "stm32h723")
    monkeypatch.setattr(
        dadd, "generate_serial_pattern", lambda name: "usb-Klipper_stm32h723xx_TEST01*"
    )
    monkeypatch.setattr(dadd, "get_mcu_serial_map", lambda: None)
    monkeypatch.setattr(dadd, "prefix_variants", lambda pattern: [pattern])

    return registry, klipper_dir, selected


def test_add_device_seeds_config_from_mcu_default_before_menuconfig(monkeypatch, tmp_path):
    registry, klipper_dir, selected = _setup(
        monkeypatch, tmp_path, 'CONFIG_MCU="stm32h723xx"\n'
    )

    launched = {"n": 0}

    def fake_menuconfig(kdir, cfg_path):
        launched["n"] += 1
        # The stub "saves": the .config already on disk (copied there by
        # load_cached_config() right after seeding) is what save_cached_config()
        # picks up below -- it never touches the filesystem itself.
        return (0, True)

    monkeypatch.setattr(dadd, "run_menuconfig", fake_menuconfig)

    sink = RecordingSink()
    em = Emitter(sink)
    decider = ScriptedAddDecider(
        prompts={"Display name (e.g., 'Octopus Pro v1.1')": "Test Board"}
    )

    rc = cmd_add_device(registry, em, decider, selected_device=selected)

    assert rc == 0
    assert launched["n"] == 1  # menuconfig ran exactly once

    data = registry.load()
    added = next(e for e in data.devices.values() if e.name == "Test Board")
    assert added.mcu == "stm32h723"

    config_mgr = ConfigManager(added.key, str(klipper_dir))
    assert config_mgr.has_cached_config()

    text = sink.text()
    assert "Config seeded from" in text  # Config phase announced the seed

    # menuconfig "saved" -> validate_mcu matched -> save_cached_config() ran
    # -> the review marker clears (safety invariant satisfied for this run).
    assert not config_mgr.is_seeded()


def test_add_device_no_default_seed_falls_back_to_fresh_message(monkeypatch, tmp_path):
    """Cached/fresh-path messages must be byte-identical to before this change
    when there is no default to seed from."""
    # default_config_text=None -> no defaults dir, so seed_from_default() is a
    # no-op and the fresh-config path runs.
    registry, klipper_dir, selected = _setup(monkeypatch, tmp_path, None)

    launched = {"n": 0}

    def fake_menuconfig(kdir, cfg_path):
        launched["n"] += 1
        return (0, False)  # exited without saving -- simplest terminal branch

    monkeypatch.setattr(dadd, "run_menuconfig", fake_menuconfig)

    sink = RecordingSink()
    em = Emitter(sink)
    decider = ScriptedAddDecider(
        prompts={"Display name (e.g., 'Octopus Pro v1.1')": "Test Board 2"}
    )

    rc = cmd_add_device(registry, em, decider, selected_device=selected)

    assert rc == 0
    assert launched["n"] == 1
    text = sink.text()
    assert "No cached config found, starting fresh" in text
    assert "Seeded" not in text


def test_add_device_discard_after_mismatch_keeps_seed_marker(monkeypatch, tmp_path):
    """Regression for the mismatch-loop 'd' (discard) branch: it must never
    clear the seed marker. A seeded-but-unreviewed cache has to keep forcing
    a menuconfig review on every subsequent flash attempt, even when the
    very first review is abandoned via discard.
    """
    # Default seed reports an MCU that will NOT match the device's registered
    # MCU ('stm32h723'), forcing the mismatch loop.
    registry, klipper_dir, selected = _setup(monkeypatch, tmp_path, 'CONFIG_MCU="rp2040"\n')

    launched = {"n": 0}

    def fake_menuconfig(kdir, cfg_path):
        launched["n"] += 1
        return (0, True)  # "saved" -- the mismatched seeded content stands

    monkeypatch.setattr(dadd, "run_menuconfig", fake_menuconfig)

    sink = RecordingSink()
    em = Emitter(sink)
    decider = ScriptedAddDecider(
        prompts={"Display name (e.g., 'Octopus Pro v1.1')": "Test Board 3"},
        mismatch_choice="d",
    )

    rc = cmd_add_device(registry, em, decider, selected_device=selected)

    assert rc == 0
    assert launched["n"] == 1
    assert decider.mismatch_calls == 1  # the mismatch loop ran, and chose 'd'

    data = registry.load()
    added = next(e for e in data.devices.values() if e.name == "Test Board 3")
    config_mgr = ConfigManager(added.key, str(klipper_dir))

    # Discard never calls save_cached_config() -> the seed marker (written
    # when seed_from_default() populated the cache) is untouched.
    assert config_mgr.has_cached_config()
    assert config_mgr.is_seeded()
    text = sink.text()
    assert "Config seeded from" in text
    # The 'd' branch checks has_cached_config() at discard time: the fresh seed
    # counts, so the cache is RESTORED (not cleared as "no previous cache").
    assert "Restored cached config" in text


# --------------------------------------------------------------------------- #
# Board profile picker integration
# --------------------------------------------------------------------------- #
def test_empty_catalog_never_invokes_picker_and_board_is_none(monkeypatch, tmp_path):
    """Zero-profiles regression: with no board profiles on disk the wizard runs
    exactly as before -- the picker is never asked and the device gets no board."""
    from kflash import boards

    registry, klipper_dir, selected = _setup(monkeypatch, tmp_path, None)
    # Force a genuinely empty catalog (XDG isolation covers user profiles; the
    # shipped catalog is non-empty once boards are curated).
    monkeypatch.setattr(boards, "SHIPPED_PROFILES", [])
    monkeypatch.setattr(dadd, "run_menuconfig", lambda kdir, cfg: (0, False))

    sink = RecordingSink()
    em = Emitter(sink)
    decider = ProfileAddDecider(
        prompts={"Display name (e.g., 'Octopus Pro v1.1')": "Empty Cat"}
    )

    rc = cmd_add_device(registry, em, decider, selected_device=selected)

    assert rc == 0
    assert decider.board_profile_calls == []  # picker never invoked
    assert decider.flash_method_calls == 1  # normal manual flash-method path
    added = next(e for e in registry.load().devices.values() if e.name == "Empty Cat")
    assert added.board is None


def test_picker_cancel_registers_no_device(monkeypatch, tmp_path):
    """Picker returning None cancels the wizard cleanly: exit 0, no device, and
    the flash-method step is never reached."""
    registry, klipper_dir, selected = _setup(monkeypatch, tmp_path, None)
    _write_board("btt-x")
    monkeypatch.setattr(dadd, "run_menuconfig", lambda kdir, cfg: (0, False))

    sink = RecordingSink()
    em = Emitter(sink)
    decider = ProfileAddDecider(
        board_profile=None,
        prompts={"Display name (e.g., 'Octopus Pro v1.1')": "Cancelled Board"},
    )

    rc = cmd_add_device(registry, em, decider, selected_device=selected)

    assert rc == 0
    assert len(decider.board_profile_calls) == 1
    assert decider.flash_method_calls == 0  # never reached the flash-method step
    names = {e.name for e in registry.load().devices.values()}
    assert "Cancelled Board" not in names
    assert "Add device cancelled" in sink.text()


def test_profile_pick_collapses_flash_method_and_sets_board(monkeypatch, tmp_path):
    """A picked profile pre-fills the flash method (choose_flash_method NOT
    asked) and stamps entry.board."""
    registry, klipper_dir, selected = _setup(monkeypatch, tmp_path, None)
    _write_board("btt-x", bootloader_method="usb", flash_command="katapult")
    monkeypatch.setattr(dadd, "run_menuconfig", lambda kdir, cfg: (0, False))

    sink = RecordingSink()
    em = Emitter(sink)
    decider = ProfileAddDecider(
        board_profile="btt-x",
        prompts={"Display name": "Profiled Board"},
    )

    rc = cmd_add_device(registry, em, decider, selected_device=selected)

    assert rc == 0
    assert decider.flash_method_calls == 0  # profile skipped the flash-method picker
    req = decider.board_profile_calls[0]
    assert req.detected_mcu == "stm32h723"
    assert any(c.key == "btt-x" for c in req.choices)
    added = next(e for e in registry.load().devices.values() if e.name == "Profiled Board")
    assert added.board == "btt-x"
    assert added.bootloader_method == "usb"
    assert added.flash_command == "katapult"


def test_profile_pick_prefills_display_name(monkeypatch, tmp_path):
    """With a board profile picked, the display-name prompt comes AFTER the
    profile step, carries the profile's display name as its default, and
    Enter-accepting it names the device after the profile."""
    registry, klipper_dir, selected = _setup(monkeypatch, tmp_path, None)
    _write_board("prof-board", mcu="stm32h723")

    sink = RecordingSink()
    em = Emitter(sink)
    # No scripted name prompt: prompt_text falls back to the request default.
    decider = ProfileAddDecider(
        board_profile="prof-board", confirms={"run_menuconfig_now": False}
    )

    rc = cmd_add_device(registry, em, decider, selected_device=selected)

    assert rc == 0
    name_req = next(r for r in decider.prompt_calls if r.message == "Display name")
    assert name_req.default == "Profile prof-board"
    added = next(
        e for e in registry.load().devices.values() if e.name == "Profile prof-board"
    )
    assert added.board == "prof-board"


def test_usb_add_hides_can_only_profiles_in_mixed_catalog(monkeypatch, tmp_path):
    """Transport filter: with both a USB and a CAN profile matching the MCU, a
    USB add offers only the USB profile in the picker choices."""
    registry, klipper_dir, selected = _setup(monkeypatch, tmp_path, None)
    _write_board("usb-board", bootloader_method="usb", flash_command="katapult")
    _write_board("can-board", bootloader_method="can", flash_command="katapult_can")
    monkeypatch.setattr(dadd, "run_menuconfig", lambda kdir, cfg: (0, False))

    sink = RecordingSink()
    em = Emitter(sink)
    decider = ProfileAddDecider(
        board_profile="usb-board",
        prompts={"Display name": "Mixed Cat"},
    )

    rc = cmd_add_device(registry, em, decider, selected_device=selected)

    assert rc == 0
    req = decider.board_profile_calls[0]
    keys = {c.key for c in req.choices}
    assert "usb-board" in keys
    assert "can-board" not in keys


def test_profile_sub_fields_prefill_beats_default(monkeypatch, tmp_path):
    """A profile's sub_fields value pre-fills before SUB_FIELD_DEFAULTS: the
    Katapult-Serial baud is taken from the profile (announced as such), not the
    auto-accepted default, and no prompt is issued for it."""
    registry, klipper_dir, selected = _setup(monkeypatch, tmp_path, None)
    _write_board(
        "serialboard",
        bootloader_method="serial",
        flash_command="katapult",
        sub_fields={"bootloader_baud": 250000},
    )
    monkeypatch.setattr(dadd, "run_menuconfig", lambda kdir, cfg: (0, False))

    sink = RecordingSink()
    em = Emitter(sink)
    decider = ProfileAddDecider(
        board_profile="serialboard",
        prompts={"Display name": "Serial Board"},
    )

    rc = cmd_add_device(registry, em, decider, selected_device=selected)

    assert rc == 0
    added = next(e for e in registry.load().devices.values() if e.name == "Serial Board")
    assert added.bootloader_method == "serial"
    assert added.bootloader_baud == 250000
    # Announced as a board-profile pre-fill, not the auto-accepted default.
    assert "Using board profile Bootloader baud rate" in sink.text()
    assert "Using default Bootloader baud rate" not in sink.text()
    # No interactive prompt was issued for the baud sub-field.
    assert all(
        p.message != "Bootloader baud rate" for p in decider.prompt_calls
    )


def test_profile_board_first_seed_marks_board_source(monkeypatch, tmp_path):
    """Post-add seeding prefers the board fragment; the .seeded marker records
    board:<key> and survives a menuconfig that exits without saving."""
    registry, klipper_dir, selected = _setup(
        monkeypatch, tmp_path, 'CONFIG_MCU="stm32h723"\nCONFIG_FROM_DEFAULT=y\n'
    )
    _write_board(
        "btt-x",
        config_fragment=True,
        fragment_text='CONFIG_MCU="stm32h723"\nCONFIG_FROM_BOARD=y\n',
    )
    monkeypatch.setattr(dadd, "run_menuconfig", lambda kdir, cfg: (0, False))

    sink = RecordingSink()
    em = Emitter(sink)
    decider = ProfileAddDecider(
        board_profile="btt-x",
        prompts={"Display name": "Seed Board"},
    )

    rc = cmd_add_device(registry, em, decider, selected_device=selected)

    assert rc == 0
    added = next(e for e in registry.load().devices.values() if e.name == "Seed Board")
    config_mgr = ConfigManager(added.key, str(klipper_dir))
    assert config_mgr.seed_source() == "board:btt-x"
    assert config_mgr.is_seeded()
    cached = config_mgr.cache_path.read_text(encoding="utf-8")
    assert "CONFIG_FROM_BOARD" in cached
    assert "CONFIG_FROM_DEFAULT" not in cached


def test_can_profile_keeps_flow_uuid_and_uses_profile_role(monkeypatch, tmp_path):
    """A CAN add with a picked profile: transport identity (uuid/interface) comes
    from the CAN flow, never the profile; the role default comes from the
    profile; entry.board is stamped."""
    registry, klipper_dir, selected = _setup(monkeypatch, tmp_path, None)
    _write_board(
        "can-tool",
        mcu="stm32g0",
        bootloader_method="can",
        flash_command="katapult_can",
        role="toolhead",
        sub_fields={"canbus_uuid": "ffffffffffff"},  # must be ignored
    )
    monkeypatch.setattr(dadd, "run_menuconfig", lambda kdir, cfg: (0, False))

    sink = RecordingSink()
    em = Emitter(sink)
    decider = ProfileAddDecider(
        board_profile="can-tool",
        prompts={
            "Display name": "CAN Tool",
            "MCU type (e.g., stm32h723, rp2040)": "stm32g0",
            "Klipper MCU name (optional, press Enter to skip)": "",
        },
    )

    rc = cmd_add_device(
        registry, em, decider, can_uuid="112233445566", can_interface="can0"
    )

    assert rc == 0
    assert decider.flash_method_calls == 0
    added = next(e for e in registry.load().devices.values() if e.name == "CAN Tool")
    assert added.board == "can-tool"
    assert added.canbus_uuid == "112233445566"  # from the CAN flow
    assert added.canbus_interface == "can0"
    assert added.role == "toolhead"  # default derived from profile.role
