"""Tier-1 pure-logic tests for kflash.discovery.

Focus: the two-phase USB glob matching (``match_devices`` / ``match_device``)
and the ``Klipper_`` <-> ``katapult_`` prefix-flip logic in
``prefix_variants``. Also covers ``find_registered_devices`` cross-referencing,
MCU extraction, serial-pattern generation, and CAN query parsing — all of which
are pure and require no real ``/dev`` access.
"""

from __future__ import annotations

import pytest

from kflash import discovery as d
from kflash.models import DeviceEntry, DiscoveredDevice


def _dev(filename: str) -> DiscoveredDevice:
    return DiscoveredDevice(path=f"/dev/serial/by-id/{filename}", filename=filename)


KLIPPER_H723 = "usb-Klipper_stm32h723xx_29001A001151313531383332-if00"
KATAPULT_H723 = "usb-katapult_stm32h723xx_29001A001151313531383332-if00"
KLIPPER_RP2040 = "usb-Klipper_rp2040_303035383039324D9B-if00"
BEACON = "usb-Beacon_Beacon_RevH_FC2A6E-if00"


# ---------------------------------------------------------------------------
# _prefix_variants — the prefix-flip core
# ---------------------------------------------------------------------------


def test_prefix_variants_klipper_generates_katapult_alt():
    variants = d.prefix_variants("usb-Klipper_stm32h723xx_29001A*")
    assert variants == [
        "usb-Klipper_stm32h723xx_29001A*",
        "usb-katapult_stm32h723xx_29001A*",
    ]


def test_prefix_variants_katapult_generates_klipper_alt():
    variants = d.prefix_variants("usb-katapult_stm32h723xx_29001A*")
    assert variants == [
        "usb-katapult_stm32h723xx_29001A*",
        "usb-Klipper_stm32h723xx_29001A*",
    ]


def test_prefix_variants_lowercase_klipper_prefix_detected():
    # Prefix detection is case-insensitive (uses .lower()).
    variants = d.prefix_variants("usb-klipper_rp2040_30*")
    assert len(variants) == 2
    assert variants[1] == "usb-katapult_rp2040_30*"


def test_prefix_variants_unknown_prefix_passthrough():
    assert d.prefix_variants("usb-Beacon_xyz*") == ["usb-Beacon_xyz*"]


# ---------------------------------------------------------------------------
# match_devices — prefix-agnostic glob matching
# ---------------------------------------------------------------------------


def test_match_devices_direct_hit():
    devices = [_dev(KLIPPER_H723)]
    matches = d.match_devices("usb-Klipper_stm32h723xx_29001A*", devices)
    assert [m.filename for m in matches] == [KLIPPER_H723]


def test_match_devices_prefix_flip_klipper_pattern_matches_katapult_device():
    # Device booted into Katapult mode but registry stores a Klipper pattern.
    devices = [_dev(KATAPULT_H723)]
    matches = d.match_devices("usb-Klipper_stm32h723xx_29001A*", devices)
    assert [m.filename for m in matches] == [KATAPULT_H723]


def test_match_devices_prefix_flip_katapult_pattern_matches_klipper_device():
    devices = [_dev(KLIPPER_H723)]
    matches = d.match_devices("usb-katapult_stm32h723xx_29001A*", devices)
    assert [m.filename for m in matches] == [KLIPPER_H723]


def test_match_devices_no_match():
    devices = [_dev(KLIPPER_H723), _dev(BEACON)]
    matches = d.match_devices("usb-Klipper_rp2040_*", devices)
    assert matches == []


def test_match_devices_multiple():
    d1 = _dev(KLIPPER_H723)
    d2 = _dev(KATAPULT_H723)
    matches = d.match_devices("usb-Klipper_stm32h723xx_29001A*", [d1, d2])
    # Both the Klipper device and (via variant) the Katapult device match.
    assert {m.filename for m in matches} == {KLIPPER_H723, KATAPULT_H723}


def test_match_device_returns_first_or_none():
    devices = [_dev(KLIPPER_H723)]
    assert d.match_device("usb-Klipper_stm32h723xx_29001A*", devices).filename == KLIPPER_H723
    assert d.match_device("usb-Klipper_rp2040_*", devices) is None


# ---------------------------------------------------------------------------
# is_supported_device
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, expected",
    [
        (KLIPPER_H723, True),
        (KATAPULT_H723, True),
        ("usb-KLIPPER_stm32_x-if00", True),  # case-insensitive
        (BEACON, False),
        ("", False),
        ("random-if00", False),
    ],
)
def test_is_supported_device(filename, expected):
    assert d.is_supported_device(filename) is expected


# ---------------------------------------------------------------------------
# find_registered_devices — cross-reference with registry
# ---------------------------------------------------------------------------


def test_find_registered_devices_matches_and_leaves_unmatched():
    entry = DeviceEntry(
        key="octopus",
        name="Octopus",
        mcu="stm32h723",
        serial_pattern="usb-Klipper_stm32h723xx_29001A*",
    )
    devices = [_dev(KLIPPER_H723), _dev(BEACON)]
    matched, unmatched = d.find_registered_devices(devices, {"octopus": entry})
    assert len(matched) == 1
    assert matched[0][0] is entry
    assert matched[0][1].filename == KLIPPER_H723
    assert [u.filename for u in unmatched] == [BEACON]


def test_find_registered_devices_skips_can_entries():
    can_entry = DeviceEntry(
        key="nhk",
        name="Nhk",
        mcu="stm32g0b1",
        serial_pattern=None,  # CAN device — matched separately
        canbus_uuid="48ca7afe7a44",
    )
    devices = [_dev(KLIPPER_H723)]
    matched, unmatched = d.find_registered_devices(devices, {"nhk": can_entry})
    assert matched == []
    assert [u.filename for u in unmatched] == [KLIPPER_H723]


def test_find_registered_devices_prefix_flip():
    entry = DeviceEntry(
        key="octopus",
        name="Octopus",
        mcu="stm32h723",
        serial_pattern="usb-Klipper_stm32h723xx_29001A*",
    )
    # Device present only in katapult mode still matches the Klipper pattern.
    matched, unmatched = d.find_registered_devices([_dev(KATAPULT_H723)], {"octopus": entry})
    assert len(matched) == 1
    assert unmatched == []


# ---------------------------------------------------------------------------
# extract_mcu_from_serial
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, expected",
    [
        (KLIPPER_H723, "stm32h723"),
        (KATAPULT_H723, "stm32h723"),
        (KLIPPER_RP2040, "rp2040"),
        ("usb-Klipper_stm32f411xe_600-if00", "stm32f411"),
        (BEACON, None),
        ("garbage", None),
        ("", None),
    ],
)
def test_extract_mcu_from_serial(filename, expected):
    assert d.extract_mcu_from_serial(filename) == expected


# ---------------------------------------------------------------------------
# generate_serial_pattern
# ---------------------------------------------------------------------------


def test_generate_serial_pattern_strips_if_suffix():
    assert d.generate_serial_pattern(KLIPPER_H723) == (
        "usb-Klipper_stm32h723xx_29001A001151313531383332*"
    )


def test_generate_serial_pattern_no_suffix_still_appends_wildcard():
    assert d.generate_serial_pattern("usb-Klipper_rp2040_ABC") == "usb-Klipper_rp2040_ABC*"


# ---------------------------------------------------------------------------
# parse_can_query_output
# ---------------------------------------------------------------------------


def test_parse_can_query_output_single():
    out = "Detected UUID: 48ca7afe7a44, Application: Katapult\n"
    result = d.parse_can_query_output(out)
    assert len(result) == 1
    assert result[0].uuid == "48ca7afe7a44"
    assert result[0].application == "Katapult"


def test_parse_can_query_output_multiple():
    out = (
        "Query Complete\n"
        "Detected UUID: 48ca7afe7a44, Application: Katapult\n"
        "Detected UUID: aabbccddeeff, Application: Klipper\n"
    )
    result = d.parse_can_query_output(out)
    assert [(r.uuid, r.application) for r in result] == [
        ("48ca7afe7a44", "Katapult"),
        ("aabbccddeeff", "Klipper"),
    ]


@pytest.mark.parametrize("out", ["", "no matches here", "Detected UUID: ZZZ, Application: X"])
def test_parse_can_query_output_no_matches(out):
    assert d.parse_can_query_output(out) == []


# ---------------------------------------------------------------------------
# is_katapult_device
# ---------------------------------------------------------------------------


def test_is_katapult_device_true_for_katapult_prefix():
    assert d.is_katapult_device("usb-katapult_rp2040_45474E621A858C5A-if00")


def test_is_katapult_device_case_insensitive():
    assert d.is_katapult_device("usb-Katapult_rp2040_ABC123-if00")


def test_is_katapult_device_false_for_klipper_and_foreign_devices():
    assert not d.is_katapult_device("usb-Klipper_rp2040_ABC123-if00")
    assert not d.is_katapult_device("usb-Beacon_Beacon_RevH_FC2-if00")
    assert not d.is_katapult_device("")
