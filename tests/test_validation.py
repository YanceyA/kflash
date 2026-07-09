"""Tier-1 pure-logic tests for kflash.validation.

Covers device-key slugging/validation (including adversarial path-traversal
and case inputs), CAN UUID/interface allowlists, transport-field mutual
exclusion, bootloader/flash-command pairing, numeric/path settings, and the
MCU-aware flash-method table filtering.
"""

from __future__ import annotations

import pytest
from conftest import FakeRegistry

from kflash import validation as v

# ---------------------------------------------------------------------------
# validate_device_key — adversarial + normal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, valid",
    [
        ("octopus-pro", True),
        ("mcu_1", True),
        ("a", True),
        ("0abc", True),
        ("nhk-v13", True),
        # Adversarial / malformed
        ("", False),  # empty
        ("   ", False),  # whitespace -> empty after strip
        ("../../etc", False),  # path traversal chars
        ("../evil", False),
        ("UPPER", False),  # uppercase not allowed
        ("Mixed-Case", False),
        ("-leading", False),  # must start with a-z/0-9
        ("_leading", False),
        ("has space", False),
        ("has/slash", False),
        ("dot.name", False),
        ("emoji\U0001f600", False),
    ],
)
def test_validate_device_key_patterns(key, valid):
    ok, err = v.validate_device_key(key, FakeRegistry())
    assert ok is valid
    assert (err == "") is valid


def test_validate_device_key_strips_whitespace():
    ok, err = v.validate_device_key("  good-key  ", FakeRegistry())
    assert ok is True and err == ""


def test_validate_device_key_collision():
    reg = FakeRegistry({"taken"})
    ok, err = v.validate_device_key("taken", reg)
    assert ok is False
    assert "already registered" in err


def test_validate_device_key_self_rename_allowed():
    reg = FakeRegistry({"taken"})
    ok, err = v.validate_device_key("taken", reg, current_key="taken")
    assert ok is True and err == ""


def test_validate_device_key_rename_to_other_existing_rejected():
    reg = FakeRegistry({"taken", "other"})
    ok, err = v.validate_device_key("other", reg, current_key="taken")
    assert ok is False
    assert "already registered" in err


# ---------------------------------------------------------------------------
# generate_device_key — slugging, unicode folding, traversal neutralization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Octopus Pro v1.1", "octopus-pro-v1-1"),
        ("Cafe MCU", "cafe-mcu"),
        ("Café MCU", "cafe-mcu"),  # accented -> ASCII folded
        ("  spaced  name  ", "spaced-name"),
        ("under_score", "under-score"),
        ("Multiple---Hyphens", "multiple-hyphens"),
        ("UPPER CASE", "upper-case"),
        # Path traversal in the display name is neutralized to a safe slug.
        ("../../etc", "etc"),
        ("../evil/path", "evilpath"),
    ],
)
def test_generate_device_key_slug(name, expected):
    assert v.generate_device_key(name, FakeRegistry()) == expected


@pytest.mark.parametrize("name", ["...", "///", "   ", "@#$%", "----"])
def test_generate_device_key_empty_slug_raises(name):
    with pytest.raises(ValueError):
        v.generate_device_key(name, FakeRegistry())


def test_generate_device_key_collision_suffix():
    reg = FakeRegistry({"octopus-pro-v1-1"})
    assert v.generate_device_key("Octopus Pro v1.1", reg) == "octopus-pro-v1-1-2"


def test_generate_device_key_multiple_collisions():
    reg = FakeRegistry({"board", "board-2", "board-3"})
    assert v.generate_device_key("Board", reg) == "board-4"


def test_generate_device_key_truncated_to_64():
    key = v.generate_device_key("x" * 200, FakeRegistry())
    assert len(key) <= 64
    assert key == "x" * 64


# ---------------------------------------------------------------------------
# validate_canbus_uuid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uuid_str, valid",
    [
        ("48ca7afe7a44", True),
        ("000000000000", True),
        ("ffffffffffff", True),
        ("48CA7AFE7A44", True),  # uppercase normalized to lowercase
        ("48Ca7aFe7A44", True),  # mixed case
        ("", False),  # empty
        ("48ca7afe7a4", False),  # 11 chars
        ("48ca7afe7a444", False),  # 13 chars
        ("48ca7afe7a4g", False),  # 'g' not hex
        ("48ca7afe7a4z", False),
        ("48ca 7afe7a44", False),  # space
        (" 48ca7afe7a44", False),  # leading space not stripped
        ("0x48ca7afe74", False),  # prefix
    ],
)
def test_validate_canbus_uuid(uuid_str, valid):
    ok, err = v.validate_canbus_uuid(uuid_str)
    assert ok is valid
    assert (err == "") is valid


# ---------------------------------------------------------------------------
# validate_can_interface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, valid",
    [
        ("can0", True),
        ("can1", True),
        ("can10", True),
        ("", False),  # empty
        ("can", False),  # no digit
        ("vcan0", False),  # virtual CAN rejected
        ("slcan0", False),  # serial-line CAN rejected
        ("CAN0", False),  # uppercase rejected
        ("can0x", False),  # trailing junk
        ("acan0", False),  # prefix junk
        ("can 0", False),
    ],
)
def test_validate_can_interface(name, valid):
    ok, err = v.validate_can_interface(name)
    assert ok is valid
    assert (err == "") is valid


# ---------------------------------------------------------------------------
# validate_bootloader_baud
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "baud, valid", [(250000, True), (115200, False), (0, False), (250001, False)]
)
def test_validate_bootloader_baud(baud, valid):
    ok, err = v.validate_bootloader_baud(baud)
    assert ok is valid
    assert (err == "") is valid


# ---------------------------------------------------------------------------
# validate_transport_fields — mutual exclusion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "serial, uuid, valid",
    [
        ("usb-Klipper_stm32*", None, True),  # USB only
        (None, "48ca7afe7a44", True),  # CAN only
        ("usb-Klipper_stm32*", "48ca7afe7a44", False),  # both
        (None, None, False),  # neither
        ("", "", False),  # both empty -> neither
        ("", "48ca7afe7a44", True),  # empty serial treated as absent
    ],
)
def test_validate_transport_fields(serial, uuid, valid):
    ok, err = v.validate_transport_fields(serial, uuid)
    assert ok is valid
    assert (err == "") is valid


# ---------------------------------------------------------------------------
# validate_bootloader_flash_pair
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bl, fc", sorted(v.COMPATIBLE_PAIRS))
def test_validate_bootloader_flash_pair_all_compatible(bl, fc):
    ok, err = v.validate_bootloader_flash_pair(bl, fc)
    assert ok is True and err == ""


@pytest.mark.parametrize(
    "bl, fc, err_contains",
    [
        ("bogus", "katapult", "Invalid bootloader method"),
        ("usb", "bogus", "Invalid flash command"),
        ("usb", "katapult_can", "Incompatible pair"),  # valid pieces, bad combo
        ("can", "make_flash", "Incompatible pair"),
        ("serial", "make_flash", "Incompatible pair"),
    ],
)
def test_validate_bootloader_flash_pair_invalid(bl, fc, err_contains):
    ok, err = v.validate_bootloader_flash_pair(bl, fc)
    assert ok is False
    assert err_contains in err


# ---------------------------------------------------------------------------
# validate_numeric_setting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, lo, hi, valid, value",
    [
        ("5", 0, 10, True, 5.0),
        ("0", 0, 10, True, 0.0),  # inclusive lower bound
        ("10", 0, 10, True, 10.0),  # inclusive upper bound
        ("2.5", 0, 10, True, 2.5),
        ("-1", 0, 10, False, None),  # below range
        ("11", 0, 10, False, None),  # above range
        ("abc", 0, 10, False, None),  # not a number
        ("", 0, 10, False, None),  # empty
    ],
)
def test_validate_numeric_setting(raw, lo, hi, valid, value):
    ok, parsed, err = v.validate_numeric_setting(raw, lo, hi)
    assert ok is valid
    assert parsed == value
    assert (err == "") is valid


# ---------------------------------------------------------------------------
# validate_path_setting
# ---------------------------------------------------------------------------


def test_validate_path_setting_missing_dir():
    ok, err = v.validate_path_setting("/no/such/dir/here", "klipper_dir")
    assert ok is False
    assert "does not exist" in err


def test_validate_path_setting_klipper_dir_requires_makefile(tmp_path):
    ok, err = v.validate_path_setting(str(tmp_path), "klipper_dir")
    assert ok is False
    assert "Makefile" in err
    (tmp_path / "Makefile").write_text("all:\n")
    ok, err = v.validate_path_setting(str(tmp_path), "klipper_dir")
    assert ok is True and err == ""


def test_validate_path_setting_katapult_dir_requires_flashtool(tmp_path):
    ok, err = v.validate_path_setting(str(tmp_path), "katapult_dir")
    assert ok is False
    assert "flashtool.py" in err
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "flashtool.py").write_text("")
    ok, err = v.validate_path_setting(str(tmp_path), "katapult_dir")
    assert ok is True and err == ""


def test_validate_path_setting_other_key_only_checks_dir(tmp_path):
    ok, err = v.validate_path_setting(str(tmp_path), "some_other_key")
    assert ok is True and err == ""


# ---------------------------------------------------------------------------
# find_flash_method_pair
# ---------------------------------------------------------------------------


def test_find_flash_method_pair_hit():
    pair = v.find_flash_method_pair("usb", "katapult")
    assert pair is not None
    assert pair.name == "Katapult USB"


def test_find_flash_method_pair_build_only_none_command():
    pair = v.find_flash_method_pair("none", None)
    assert pair is not None
    assert pair.name == "Build Only"


def test_find_flash_method_pair_miss():
    assert v.find_flash_method_pair("usb", "flash_sdcard") is None


# ---------------------------------------------------------------------------
# _is_rp2_mcu
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mcu, expected",
    [
        ("rp2040", True),
        ("RP2040", True),
        ("rp2350", True),
        ("rp2040-something", True),
        ("stm32h723", False),
        ("", False),
    ],
)
def test_is_rp2_mcu(mcu, expected):
    assert v._is_rp2_mcu(mcu) is expected


# ---------------------------------------------------------------------------
# filter_flash_methods_for_mcu
# ---------------------------------------------------------------------------


def test_filter_flash_methods_none_mcu_returns_full_table():
    result = v.filter_flash_methods_for_mcu(None)
    assert len(result) == len(v.FLASH_METHOD_TABLE)


def test_filter_flash_methods_non_rp2_returns_full_table():
    result = v.filter_flash_methods_for_mcu("stm32h723")
    assert len(result) == len(v.FLASH_METHOD_TABLE)


def test_filter_flash_methods_rp2_excludes_serial_reenum_pairs():
    result = v.filter_flash_methods_for_mcu("rp2040")
    present = {(p.bootloader_method, p.flash_command) for p in result}
    for excluded in v._RP2_EXCLUDED_PAIRS:
        assert excluded not in present


def test_filter_flash_methods_rp2_reorders_picoboot_first():
    result = v.filter_flash_methods_for_mcu("rp2040")
    assert (result[0].bootloader_method, result[0].flash_command) == ("none", "make_flash")


def test_filter_flash_methods_rp2_build_only_last():
    result = v.filter_flash_methods_for_mcu("rp2040")
    assert result[-1].flash_command is None


# ---------------------------------------------------------------------------
# filter_flash_methods_for_device — transport filtering
# ---------------------------------------------------------------------------


def test_filter_flash_methods_for_device_can_only():
    result = v.filter_flash_methods_for_device("stm32h723", is_can_device=True)
    assert result
    assert all(p.bootloader_method == "can" for p in result)


def test_filter_flash_methods_for_device_usb_excludes_can():
    result = v.filter_flash_methods_for_device("stm32h723", is_can_device=False)
    assert result
    assert all(p.bootloader_method != "can" for p in result)


def test_filter_flash_methods_for_device_rp2_can_yields_only_katapult_can():
    result = v.filter_flash_methods_for_device("rp2040", is_can_device=True)
    assert [(p.bootloader_method, p.flash_command) for p in result] == [("can", "katapult_can")]
