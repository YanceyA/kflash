"""Tier-1 pure-logic tests for kflash.safety.

Focus: version parsing/comparison in ``detect_downgrade`` (including the
Kalico-date-tag vs Klipper-semver edge cases the project review calls out),
plus the small pure predicates.
"""

from __future__ import annotations

import pytest

from kflash import safety

# ---------------------------------------------------------------------------
# check_dirty_repo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version, expected_dirty",
    [
        (None, False),
        ("v0.12.0-45-g7ce409d", False),
        ("v0.12.0-45-g7ce409d-dirty", True),
        ("v2025.01.15-dirty", True),
        ("", False),
    ],
)
def test_check_dirty_repo(version, expected_dirty):
    result = safety.check_dirty_repo(version)
    assert result.is_dirty is expected_dirty
    if version is None:
        assert result.version is None
    else:
        assert result.version == version


# ---------------------------------------------------------------------------
# should_restart_service / should_block_on_printer_state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("was_active", [True, False])
def test_should_restart_service(was_active):
    assert safety.should_restart_service(was_active) is was_active


@pytest.mark.parametrize(
    "state, expected",
    [
        ("startup", True),
        ("printing", True),
        ("paused", True),
        ("standby", False),
        ("complete", False),
        ("error", False),
        ("cancelled", False),
        ("", False),
        ("PRINTING", False),  # case-sensitive membership check
    ],
)
def test_should_block_on_printer_state(state, expected):
    assert safety.should_block_on_printer_state(state) is expected


# ---------------------------------------------------------------------------
# detect_downgrade — happy path / semver comparison
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host, mcu, is_downgrade",
    [
        # Identical versions: not a downgrade.
        ("v0.12.0", "v0.12.0", False),
        ("v0.12.0-45-g7ce409d", "v0.12.0-45-g7ce409d", False),
        # MCU behind host (fewer commits): flashing host is an upgrade.
        ("v0.12.0-45-g7ce409d", "v0.12.0-10-g7ce409d", False),
        # MCU ahead of host (more commits on same tag): flashing would downgrade.
        ("v0.12.0-10-g7ce409d", "v0.12.0-45-g7ce409d", True),
        # Higher patch on MCU -> downgrade.
        ("v0.12.0", "v0.12.5", True),
        # Higher patch on host -> not a downgrade.
        ("v0.12.5", "v0.12.0", False),
        # Leading 'v' optional and -dirty suffix tolerated.
        ("0.12.0", "v0.12.0-dirty", False),
    ],
)
def test_detect_downgrade_semver(host, mcu, is_downgrade):
    result = safety.detect_downgrade(host, mcu)
    assert result.is_downgrade is is_downgrade
    assert result.host_version == host
    assert result.mcu_version == mcu


def test_detect_downgrade_kalico_date_tag_parses():
    # Kalico date tags with three dotted components DO parse (major=year).
    result = safety.detect_downgrade("v2025.01.15", "v2025.01.15")
    assert result.is_downgrade is False


def test_detect_downgrade_cross_scheme_numeric_quirk():
    # Kalico date tag (year as "major") compares numerically greater than any
    # Klipper semver. detect_downgrade does not detect that the schemes are
    # incompatible; it simply compares the parsed integer tuples. Documenting
    # current behavior: MCU on Kalico v2025.* vs host on Klipper v0.12.0 is
    # reported as a "downgrade" because (2025, ...) > (0, 12, 0, 0).
    result = safety.detect_downgrade("v0.12.0", "v2025.01.15")
    assert result.is_downgrade is True


@pytest.mark.parametrize(
    "bad_version",
    [
        "garbage",
        "v2025.01",  # only two components — real risk for short Kalico tags
        "1.2",
        "",
        "v1.2.3.4",  # four components
        "not-a-version",
    ],
)
def test_detect_downgrade_raises_on_unparseable(bad_version):
    # Callers wrap detect_downgrade in try/except; unparseable input raises
    # ValueError rather than returning a "can't compare" sentinel.
    with pytest.raises(ValueError):
        safety.detect_downgrade("v0.12.0", bad_version)
    with pytest.raises(ValueError):
        safety.detect_downgrade(bad_version, "v0.12.0")


# ---------------------------------------------------------------------------
# discover_python_path / resolve_registry_path
# ---------------------------------------------------------------------------


def test_discover_python_path_found(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").write_text("#!/bin/sh\n")
    assert safety.discover_python_path(str(tmp_path)) == str(bindir / "python3")


def test_discover_python_path_missing(tmp_path):
    assert safety.discover_python_path(str(tmp_path)) is None


def test_resolve_registry_path_env_override(monkeypatch):
    monkeypatch.setenv("KALICO_REGISTRY_PATH", "/custom/reg.json")
    assert safety.resolve_registry_path() == "/custom/reg.json"


def test_resolve_registry_path_xdg(monkeypatch):
    monkeypatch.delenv("KALICO_REGISTRY_PATH", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    assert safety.resolve_registry_path() == "/xdg/kalico-flash/devices.json"


def test_resolve_registry_path_default_home(monkeypatch):
    monkeypatch.delenv("KALICO_REGISTRY_PATH", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/tester")
    path = safety.resolve_registry_path()
    assert path == "/home/tester/.config/kalico-flash/devices.json"
