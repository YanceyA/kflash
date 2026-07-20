"""Tier-1 pure-logic tests for kflash.moonraker.

Focus (all pure, no network): ``_parse_git_describe`` version parsing,
``is_mcu_outdated`` comparison logic, ``parse_mcu_objects`` fixture handling,
``detect_firmware_flavor`` classification, ``match_serial_to_mcu_name`` glob
matching, and ``get_mcu_version_for_device`` with an injected versions dict.
"""

from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from kflash import moonraker as m

# ---------------------------------------------------------------------------
# _parse_git_describe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version, tag, count",
    [
        ("v0.12.0-45-g7ce409d", "v0.12.0", 45),
        ("v0.12.0-0-g7ce409d", "v0.12.0", 0),
        ("v0.12.0-45-g7ce409d-dirty", "v0.12.0", 45),
        ("v0.12.0", "v0.12.0", None),  # tag-only, no describe suffix
        ("v2026.01.00", "v2026.01.00", None),  # Kalico date tag
        ("v0.12.0-dirty", "v0.12.0", None),  # dirty without commit count
        ("", None, None),  # empty
        ("   ", None, None),  # whitespace only
        ("45-g7ce409d", None, None),  # synthesized no-tag form (no leading 'v')
        ("garbage", None, None),  # unparseable
    ],
)
def test_parse_git_describe(version, tag, count):
    assert m._parse_git_describe(version) == (tag, count)


# ---------------------------------------------------------------------------
# is_mcu_outdated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host, mcu, expected",
    [
        # Empty inputs -> never outdated.
        ("", "v0.12.0-1-gabc", False),
        ("v0.12.0-1-gabc", "", False),
        ("  ", "  ", False),
        # Same tag, MCU has fewer commits -> outdated.
        ("v0.12.0-45-g111", "v0.12.0-10-g222", True),
        # Same tag, MCU equal commit count -> not outdated.
        ("v0.12.0-45-g111", "v0.12.0-45-g222", False),
        # Same tag, MCU ahead -> not outdated.
        ("v0.12.0-10-g111", "v0.12.0-45-g222", False),
        # Same tag but commit counts missing -> not outdated.
        ("v0.12.0", "v0.12.0", False),
        # Unparseable on both -> raw string compare (equal).
        ("garbage", "garbage", False),
        # Unparseable and differing -> raw string compare (differs).
        ("garbageA", "garbageB", True),
    ],
)
def test_is_mcu_outdated(host, mcu, expected):
    assert m.is_mcu_outdated(host, mcu) is expected


def test_is_mcu_outdated_different_tags_always_true_even_if_mcu_newer():
    # NOTE: possible bug — see report. When the two parsed tags differ,
    # is_mcu_outdated returns True regardless of direction. Here the MCU is on
    # a NEWER tag than the host, yet it is still reported as "outdated".
    assert m.is_mcu_outdated("v0.11.0-5-g111", "v0.12.0-5-g222") is True
    # And the genuinely-behind direction is also True (correct).
    assert m.is_mcu_outdated("v0.12.0-5-g111", "v0.11.0-5-g222") is True


# ---------------------------------------------------------------------------
# parse_mcu_objects
# ---------------------------------------------------------------------------


def test_parse_mcu_objects_extracts_chip_and_version():
    response = {
        "mcu": {
            "mcu_version": "v0.12.0-45-g7ce409d",
            "mcu_constants": {"MCU": "stm32h723xx"},
        },
        "mcu nhk": {
            "mcu_version": "v0.12.0-45-g7ce409d",
            "mcu_constants": {"MCU": "stm32g0b1xx"},
        },
    }
    result = m.parse_mcu_objects(response)
    assert result == {
        "mcu": {"chip": "stm32h723xx", "version": "v0.12.0-45-g7ce409d"},
        "mcu nhk": {"chip": "stm32g0b1xx", "version": "v0.12.0-45-g7ce409d"},
    }


def test_parse_mcu_objects_skips_missing_version():
    response = {"mcu": {"mcu_constants": {"MCU": "stm32h723xx"}}}  # no mcu_version
    assert m.parse_mcu_objects(response) == {}


def test_parse_mcu_objects_skips_missing_chip():
    response = {"mcu": {"mcu_version": "v0.12.0"}}  # no mcu_constants/MCU
    assert m.parse_mcu_objects(response) == {}


def test_parse_mcu_objects_empty():
    assert m.parse_mcu_objects({}) == {}


def test_parse_mcu_objects_mixed_valid_and_invalid():
    response = {
        "mcu": {"mcu_version": "v1", "mcu_constants": {"MCU": "chipA"}},
        "mcu bad": {"mcu_version": "v2"},  # missing chip -> skipped
    }
    result = m.parse_mcu_objects(response)
    assert list(result.keys()) == ["mcu"]


# ---------------------------------------------------------------------------
# detect_firmware_flavor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version, expected",
    [
        (None, "Unknown"),
        ("", "Unknown"),
        ("v2025.01.15", "Kalico"),
        ("v2026.02.01", "Kalico"),
        ("2025.01.15", "Kalico"),  # 'v' optional
        ("v0.12.0-45-g7ce409d", "Klipper"),
        ("v0.11.0", "Klipper"),
        ("garbage", "Unknown"),
    ],
)
def test_detect_firmware_flavor(version, expected):
    assert m.detect_firmware_flavor(version) == expected


# ---------------------------------------------------------------------------
# get_klippy_state — server/info query
# ---------------------------------------------------------------------------


def test_get_klippy_state_parses_server_info(monkeypatch):
    payload = json.dumps({"result": {"klippy_state": "startup"}}).encode()

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return payload

    monkeypatch.setattr(m, "urlopen", lambda url, timeout: _Resp())
    assert m.get_klippy_state() == "startup"


def test_get_klippy_state_none_when_unreachable(monkeypatch):
    def _raise(url, timeout):
        raise URLError("down")

    monkeypatch.setattr(m, "urlopen", _raise)
    assert m.get_klippy_state() is None


# ---------------------------------------------------------------------------
# match_serial_to_mcu_name
# ---------------------------------------------------------------------------


def test_match_serial_to_mcu_name_hit():
    mcu_serials = {
        "mcu": "/dev/serial/by-id/usb-Klipper_stm32h723xx_29001A001151313531383332-if00",
        "mcu linux": None,
    }
    pattern = "usb-Klipper_stm32h723xx_29001A*"
    assert m.match_serial_to_mcu_name(pattern, mcu_serials) == "mcu"


def test_match_serial_to_mcu_name_skips_none_serial():
    mcu_serials = {"mcu linux": None}
    assert m.match_serial_to_mcu_name("usb-Klipper_*", mcu_serials) is None


def test_match_serial_to_mcu_name_no_match():
    mcu_serials = {"mcu": "/dev/serial/by-id/usb-Klipper_rp2040_ABC-if00"}
    assert m.match_serial_to_mcu_name("usb-Klipper_stm32h723*", mcu_serials) is None


def test_match_serial_to_mcu_name_matches_across_prefixes():
    # Device registered while in Katapult mode: pattern has the katapult
    # prefix, printer.cfg records the Klipper serial path.
    mcu_serials = {
        "mcu": "/dev/serial/by-id/usb-Klipper_rp2040_45474E621A858C5A-if00",
    }
    assert (
        m.match_serial_to_mcu_name("usb-katapult_rp2040_45474E621A858C5A*", mcu_serials)
        == "mcu"
    )


def test_match_serial_to_mcu_name_reverse_prefix_direction():
    mcu_serials = {
        "mcu hbb": "/dev/serial/by-id/usb-katapult_rp2040_ABC-if00",
    }
    assert (
        m.match_serial_to_mcu_name("usb-Klipper_rp2040_ABC*", mcu_serials)
        == "mcu hbb"
    )


# ---------------------------------------------------------------------------
# get_mcu_version_for_device — direct lookup with injected versions
# ---------------------------------------------------------------------------


def test_get_mcu_version_direct_lookup_main():
    versions = {"main": "v0.12.0-45-gaaa", "nhk": "v0.12.0-45-gbbb"}
    assert (
        m.get_mcu_version_for_device(mcu_name="mcu", _mcu_versions=versions) == "v0.12.0-45-gaaa"
    )


def test_get_mcu_version_direct_lookup_named_strips_prefix():
    versions = {"main": "v0.12.0-45-gaaa", "nhk": "v0.12.0-45-gbbb"}
    assert m.get_mcu_version_for_device(mcu_name="mcu nhk", _mcu_versions=versions) == (
        "v0.12.0-45-gbbb"
    )


def test_get_mcu_version_direct_lookup_case_insensitive():
    versions = {"hbb": "v0.12.0-45-gccc"}
    assert m.get_mcu_version_for_device(mcu_name="mcu HBB", _mcu_versions=versions) == (
        "v0.12.0-45-gccc"
    )


def test_get_mcu_version_no_name_returns_none_without_fallback():
    versions = {"main": "v0.12.0"}
    assert m.get_mcu_version_for_device(mcu_name=None, _mcu_versions=versions) is None


def test_get_mcu_version_fuzzy_fallback_by_name():
    versions = {"nhk": "v0.12.0-45-gbbb", "main": "v0.12.0-45-gaaa"}
    result = m.get_mcu_version_for_device(
        device_name="Nhk v1.3",
        mcu_name=None,
        _mcu_versions=versions,
        allow_fuzzy_fallback=True,
    )
    assert result == "v0.12.0-45-gbbb"


def test_get_mcu_version_direct_lookup_miss_returns_none():
    versions = {"main": "v0.12.0"}
    assert m.get_mcu_version_for_device(mcu_name="mcu nope", _mcu_versions=versions) is None
