"""Tier-1 pure-logic tests for kflash.errors.format_error.

format_error produces plain deterministic ASCII (no ANSI escapes, no theme
dependency) -- styling is the presenting sink's job.
"""

from __future__ import annotations

import pytest

from kflash import errors


def test_format_error_minimal():
    out = errors.format_error("Build failed", "compilation error")
    assert out == "[FAIL] Build failed: compilation error"


def test_format_error_no_context_no_recovery_single_line():
    out = errors.format_error("X", "y")
    assert "\n" not in out


def test_format_error_empty_context_dict_is_ignored():
    out = errors.format_error("X", "y", context={})
    assert out == "[FAIL] X: y"


def test_format_error_with_device_context():
    out = errors.format_error("Flash failed", "boom", context={"device": "octopus"})
    lines = out.split("\n")
    assert lines[0] == "[FAIL] Flash failed: boom"
    assert lines[1] == ""  # blank line separates header from context
    assert "Affected: device 'octopus'." in out


def test_format_error_context_known_keys_ordering():
    out = errors.format_error(
        "Err",
        "msg",
        context={"device": "oct", "mcu": "stm32h723", "path": "/tmp/x"},
    )
    assert "Affected: device 'oct', MCU 'stm32h723', path '/tmp/x'." in out


def test_format_error_expected_actual_pair():
    out = errors.format_error(
        "MCU mismatch",
        "no match",
        context={"expected": "stm32h723", "actual": "rp2040"},
    )
    assert "expected 'stm32h723' but found 'rp2040'." in out


def test_format_error_extra_context_keys_included():
    out = errors.format_error("Err", "msg", context={"custom": "value"})
    assert "custom 'value'" in out


def test_format_error_recovery_preserves_numbered_lines():
    recovery = "1. First step\n2. Second step\n3. Third step"
    out = errors.format_error("Err", "msg", recovery=recovery)
    assert "1. First step" in out
    assert "2. Second step" in out
    assert "3. Third step" in out


def test_format_error_recovery_preserves_blank_lines():
    recovery = "1. First\n\n2. After blank"
    out = errors.format_error("Err", "msg", recovery=recovery)
    lines = out.split("\n")
    # There should be a preserved empty line between the two numbered items.
    idx1 = lines.index("1. First")
    idx2 = lines.index("2. After blank")
    assert "" in lines[idx1 + 1 : idx2]


def test_format_error_wraps_long_lines_to_80():
    long_msg = "word " * 40  # ~200 chars of context prose
    out = errors.format_error("Err", "msg", context={"note": long_msg.strip()})
    for line in out.split("\n"):
        assert len(line) <= 80


def test_format_error_full_structure():
    out = errors.format_error(
        "Flash failed",
        "could not flash",
        context={"device": "octopus", "mcu": "stm32h723"},
        recovery="1. Power cycle\n2. Retry",
    )
    lines = out.split("\n")
    assert lines[0] == "[FAIL] Flash failed: could not flash"
    assert "Affected: device 'octopus', MCU 'stm32h723'." in out
    assert "1. Power cycle" in out
    assert "2. Retry" in out


# ---------------------------------------------------------------------------
# Template registry sanity — get_recovery_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(errors.ERROR_TEMPLATES.keys()))
def test_get_recovery_text_returns_template(key):
    text = errors.get_recovery_text(key)
    assert isinstance(text, str) and text


def test_get_recovery_text_missing_key_raises():
    with pytest.raises(KeyError):
        errors.get_recovery_text("nonexistent_template")
