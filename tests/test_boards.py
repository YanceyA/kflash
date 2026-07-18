"""Tests for the BoardProfile catalog and user-profile overlay.

Covers the shipped-catalog invariants (every entry forms a valid flash-method
pair; keys unique; declared fragments exist on disk) and the user-profile
overlay behaviour (JSON discovery, key-shadowing, MCU prefix matching, and
crash-proof handling of arbitrary/malformed user JSON).
"""

from __future__ import annotations

import json

import pytest

from kflash import boards
from kflash.boards import BoardProfile, fragment_drift
from kflash.validation import find_flash_method_pair


@pytest.fixture
def boards_env(tmp_path, monkeypatch):
    """Isolated XDG config home with an empty user boards dir; returns its path."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    boards_dir = boards.get_user_boards_dir()
    boards_dir.mkdir(parents=True, exist_ok=True)
    return boards_dir


def _write_json(boards_dir, stem, **overrides):
    """Write a minimal-but-valid <stem>.json into the user boards dir.

    The declared ``key`` defaults to the filename stem; pass ``key=...`` in
    overrides to create a filename/key mismatch or duplicate-key files.
    """
    data = {
        "key": stem,
        "name": f"Test {stem}",
        "mcu": "stm32h723",
        "bootloader_method": "usb",
        "flash_command": "katapult",
    }
    data.update(overrides)
    (boards_dir / f"{stem}.json").write_text(json.dumps(data), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Shipped-catalog invariants (vacuously true while empty -- Task 15 relies).
# --------------------------------------------------------------------------- #
def test_catalog_entries_are_internally_valid():
    for p in boards.SHIPPED_PROFILES:
        assert find_flash_method_pair(p.bootloader_method, p.flash_command) is not None, (
            f"shipped profile '{p.key}' does not form a valid FlashMethodPair"
        )


def test_shipped_profiles_have_unique_keys():
    keys = [p.key for p in boards.SHIPPED_PROFILES]
    assert len(keys) == len(set(keys)), "duplicate keys in SHIPPED_PROFILES"


def test_shipped_profiles_are_shipped_origin():
    for p in boards.SHIPPED_PROFILES:
        assert p.origin == "shipped"


def test_no_profile_uses_reserved_other_key():
    # "other" is the manual-setup sentinel in ChooseBoardProfileDecision.
    for p in boards.SHIPPED_PROFILES:
        assert p.key != "other", "shipped profile uses the reserved key 'other'"


def test_shipped_profiles_have_source_and_verified():
    # Provenance is mandatory: every shipped entry cites where its facts came
    # from and how thoroughly they were checked.
    for p in boards.SHIPPED_PROFILES:
        assert p.source, f"shipped profile '{p.key}' has empty source"
        assert p.verified in {"hardware", "docs"}, (
            f"shipped profile '{p.key}' has invalid verified '{p.verified}'"
        )


def test_shipped_profiles_sub_field_values_valid():
    # Guards future shipped CAN/serial boards carrying bootloader_baud (etc.)
    # against a dev typo at TEST time rather than at wizard runtime.
    for p in boards.SHIPPED_PROFILES:
        boards._validate_sub_field_values(p.sub_fields)  # raises on bad value type


def test_shipped_profiles_have_checked_against():
    # Freshness signal is mandatory for shipped profiles: every entry records the
    # Kalico/Klipper version + date its facts were last verified against.
    for p in boards.SHIPPED_PROFILES:
        assert p.checked_against, (
            f"shipped profile '{p.key}' has empty checked_against"
        )


def test_fragment_paths_exist_for_all_shipped_profiles():
    for p in boards.all_profiles():
        if p.origin == "shipped" and p.config_fragment:
            assert p.fragment_path().exists(), (
                f"shipped profile '{p.key}' declares config_fragment but "
                f"{p.fragment_path()} is missing"
            )


# --------------------------------------------------------------------------- #
# MCU prefix matching (must mirror ConfigManager.validate_mcu semantics).
# --------------------------------------------------------------------------- #
def test_profiles_for_mcu_prefix_matches(boards_env):
    _write_json(boards_env, "board-h723", mcu="stm32h723")
    # Detected exactly equal, and the longer 'xx' variant, both match.
    for detected in ("stm32h723", "stm32h723xx"):
        keys = [p.key for p in boards.profiles_for_mcu(detected)]
        assert "board-h723" in keys, detected


def test_profiles_for_mcu_shorter_detected_matches(boards_env):
    # Profile mcu is the longer string; a shorter detected value still matches
    # (bidirectional prefix rule).
    _write_json(boards_env, "board-long", mcu="stm32h723xx")
    keys = [p.key for p in boards.profiles_for_mcu("stm32h723")]
    assert "board-long" in keys


def test_profiles_for_unknown_mcu_empty(boards_env):
    _write_json(boards_env, "board-h723", mcu="stm32h723")
    # An MCU with no matching profile (shipped or user) returns empty. Use a
    # synthetic never-shippable MCU string so a future real-silicon batch (e.g.
    # SAM4E) can't silently start matching this sentinel.
    assert boards.profiles_for_mcu("nosuchmcu99") == []


# --------------------------------------------------------------------------- #
# User-profile overlay.
# --------------------------------------------------------------------------- #
def test_user_profile_json_overlay(boards_env):
    _write_json(boards_env, "my-board", config_fragment=True)
    (boards_env / "my-board.config").write_text("CONFIG_MACH_STM32=y\n", encoding="utf-8")

    profs = {p.key: p for p in boards.all_profiles()}
    assert "my-board" in profs
    p = profs["my-board"]
    assert p.origin == "user"
    # User fragment resolves into the user boards dir and exists.
    assert p.fragment_path() == boards_env / "my-board.config"
    assert p.fragment_path().exists()


def test_user_profile_shadows_shipped(boards_env, monkeypatch):
    shipped = BoardProfile(
        key="dup",
        name="Shipped Version",
        mcu="stm32h723",
        bootloader_method="usb",
        flash_command="katapult",
    )
    monkeypatch.setattr(boards, "SHIPPED_PROFILES", [shipped])
    _write_json(boards_env, "dup", name="User Override")

    p = boards.get_profile("dup")
    assert p is not None
    assert p.name == "User Override"
    assert p.origin == "user"
    # Exactly one entry for the shadowed key.
    assert [x.key for x in boards.all_profiles()].count("dup") == 1


def test_get_profile_unknown_returns_none(boards_env):
    assert boards.get_profile("does-not-exist") is None


def test_profile_display_name_resolves_and_falls_back(boards_env):
    # A known key resolves to the profile's display name; an unknown key (e.g. a
    # user profile deleted after a device recorded it) degrades to the raw key
    # so a UI surfacing a device's ``board`` never shows a blank.
    _write_json(boards_env, "known", name="Nice Board Name")
    assert boards.profile_display_name("known") == "Nice Board Name"
    assert boards.profile_display_name("gone-board") == "gone-board"


def test_profile_display_name_accepts_preloaded_catalog(boards_env):
    _write_json(boards_env, "pre", name="Preloaded Board")
    profiles, _ = boards.load_catalog()
    assert boards.profile_display_name("pre", profiles) == "Preloaded Board"


def test_flash_command_null_allowed(boards_env):
    _write_json(boards_env, "buildonly", bootloader_method="none", flash_command=None)
    p = boards.get_profile("buildonly")
    assert p is not None
    assert p.flash_command is None


def test_unknown_json_keys_ignored_and_origin_forced(boards_env):
    # Unknown keys are dropped; a user-supplied 'origin' cannot override "user".
    _write_json(boards_env, "extra", nonsense="ignored", origin="shipped")
    p = boards.get_profile("extra")
    assert p is not None
    assert p.origin == "user"


def test_sub_fields_and_optional_metadata_loaded(boards_env):
    _write_json(
        boards_env,
        "meta",
        bootloader_method="serial",
        flash_command="katapult",
        sub_fields={"bootloader_baud": 250000},
        notes="128KB bootloader",
        source="https://example.invalid/repo",
        verified="docs",
        checked_against="kalico v2026.06, 2026-07-16",
        role="toolhead",
    )
    p = boards.get_profile("meta")
    assert p is not None
    assert p.sub_fields == {"bootloader_baud": 250000}
    assert p.notes == "128KB bootloader"
    assert p.verified == "docs"
    assert p.role == "toolhead"


# --------------------------------------------------------------------------- #
# Crash-proofing against arbitrary user JSON.
# --------------------------------------------------------------------------- #
def test_malformed_user_profile_skipped_with_warning(boards_env):
    _write_json(boards_env, "good")
    (boards_env / "bad.json").write_text("{ not valid json ", encoding="utf-8")

    profiles, warnings = boards.load_user_profiles()
    keys = [p.key for p in profiles]
    assert "good" in keys
    assert not any(p.key == "bad" for p in profiles)
    assert any("bad.json" in w for w in warnings)


def test_reserved_other_key_user_profile_skipped(boards_env):
    _write_json(boards_env, "other")
    profiles, warnings = boards.load_user_profiles()
    assert profiles == []
    assert any("other" in w and "reserved" in w for w in warnings)


def test_non_dict_root_skipped(boards_env):
    (boards_env / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
    profiles, warnings = boards.load_user_profiles()
    assert profiles == []
    assert warnings


def test_missing_required_key_skipped(boards_env):
    (boards_env / "nomcu.json").write_text(
        json.dumps(
            {
                "key": "nomcu",
                "name": "No MCU",
                "bootloader_method": "usb",
                "flash_command": "katapult",
            }
        ),
        encoding="utf-8",
    )
    profiles, warnings = boards.load_user_profiles()
    assert profiles == []
    assert any("nomcu.json" in w for w in warnings)


def test_wrong_type_required_key_skipped(boards_env):
    (boards_env / "wrongtype.json").write_text(
        json.dumps(
            {
                "key": "wrongtype",
                "name": 123,
                "mcu": "stm32h723",
                "bootloader_method": "usb",
                "flash_command": "katapult",
            }
        ),
        encoding="utf-8",
    )
    profiles, warnings = boards.load_user_profiles()
    assert profiles == []
    assert warnings


def test_bad_sub_fields_type_skipped(boards_env):
    _write_json(boards_env, "badsub", sub_fields=["not", "a", "dict"])
    profiles, warnings = boards.load_user_profiles()
    assert not any(p.key == "badsub" for p in profiles)
    assert warnings


def test_sub_field_bad_baud_value_type_skipped(boards_env):
    # A malformed bootloader_baud value (non-int-like) would later crash the
    # add-device wizard at `int(pv)`; the profile is skipped at load time with a
    # warning naming the field instead.
    _write_json(
        boards_env,
        "badbaud",
        bootloader_method="serial",
        flash_command="katapult",
        sub_fields={"bootloader_baud": "fast"},
    )
    profiles, warnings = boards.load_user_profiles()
    assert not any(p.key == "badbaud" for p in profiles)
    assert any("badbaud.json" in w and "bootloader_baud" in w for w in warnings)


def test_sub_field_bool_baud_rejected(boards_env):
    # bool is an int subclass in Python but is not a valid baud; reject it.
    _write_json(
        boards_env,
        "boolbaud",
        bootloader_method="serial",
        flash_command="katapult",
        sub_fields={"bootloader_baud": True},
    )
    profiles, warnings = boards.load_user_profiles()
    assert not any(p.key == "boolbaud" for p in profiles)
    assert any("boolbaud.json" in w and "bootloader_baud" in w for w in warnings)


def test_sub_field_float_baud_rejected(boards_env):
    # A float baud (250000.0) is not an int -- reject it rather than silently
    # coercing to a value the wizard's int() path would mishandle.
    _write_json(
        boards_env,
        "floatbaud",
        bootloader_method="serial",
        flash_command="katapult",
        sub_fields={"bootloader_baud": 250000.0},
    )
    profiles, warnings = boards.load_user_profiles()
    assert not any(p.key == "floatbaud" for p in profiles)
    assert any("floatbaud.json" in w and "bootloader_baud" in w for w in warnings)


def test_sub_field_none_baud_rejected(boards_env):
    # An explicit null baud is not an int -- reject it.
    _write_json(
        boards_env,
        "nonebaud",
        bootloader_method="serial",
        flash_command="katapult",
        sub_fields={"bootloader_baud": None},
    )
    profiles, warnings = boards.load_user_profiles()
    assert not any(p.key == "nonebaud" for p in profiles)
    assert any("nonebaud.json" in w and "bootloader_baud" in w for w in warnings)


def test_sub_field_bad_string_value_type_skipped(boards_env):
    # uf2_mount_path/sdcard_board must be strings; a numeric value is rejected.
    _write_json(
        boards_env,
        "badpath",
        bootloader_method="manual",
        flash_command="uf2_mount",
        sub_fields={"uf2_mount_path": 123},
    )
    profiles, warnings = boards.load_user_profiles()
    assert not any(p.key == "badpath" for p in profiles)
    assert any("badpath.json" in w and "uf2_mount_path" in w for w in warnings)


def test_sub_field_valid_values_and_unknown_keys_allowed(boards_env):
    # Known keys with valid value types load; unknown sub_field keys are
    # permitted (ignored downstream) and do not block the profile.
    _write_json(
        boards_env,
        "goodsub",
        bootloader_method="serial",
        flash_command="katapult",
        sub_fields={"bootloader_baud": 250000, "future_field": {"nested": 1}},
    )
    p = boards.get_profile("goodsub")
    assert p is not None
    assert p.sub_fields["bootloader_baud"] == 250000
    assert p.sub_fields["future_field"] == {"nested": 1}


def test_invalid_method_pair_user_profile_skipped(boards_env):
    # usb + katapult_can is not a valid FlashMethodPair.
    _write_json(boards_env, "badpair", bootloader_method="usb", flash_command="katapult_can")
    profiles, warnings = boards.load_user_profiles()
    assert not any(p.key == "badpair" for p in profiles)
    assert any("badpair.json" in w for w in warnings)


# --------------------------------------------------------------------------- #
# load_catalog: one-pass API carrying warnings; deterministic duplicates.
# --------------------------------------------------------------------------- #
def test_load_catalog_returns_profiles_and_warnings(boards_env):
    _write_json(boards_env, "good")
    (boards_env / "bad.json").write_text("{ not valid json ", encoding="utf-8")

    profiles, warnings = boards.load_catalog()
    assert "good" in [p.key for p in profiles]
    assert any("bad.json" in w for w in warnings)


def test_load_catalog_includes_shipped_overlay(boards_env, monkeypatch):
    shipped = BoardProfile(
        key="dup",
        name="Shipped Version",
        mcu="stm32h723",
        bootloader_method="usb",
        flash_command="katapult",
    )
    monkeypatch.setattr(boards, "SHIPPED_PROFILES", [shipped])
    _write_json(boards_env, "dup", name="User Override")

    profiles, warnings = boards.load_catalog()
    by_key = {p.key: p for p in profiles}
    assert by_key["dup"].name == "User Override"
    assert warnings == []


def test_query_helpers_accept_preloaded_catalog(boards_env):
    _write_json(boards_env, "pre", mcu="stm32h723")
    profiles, _ = boards.load_catalog()
    # Remove the file to prove the helpers do NOT re-read disk when given
    # a pre-loaded catalog.
    (boards_env / "pre.json").unlink()
    # The user profile is still resolvable from the pre-loaded catalog even
    # though its file is gone (the shipped catalog may also contribute entries).
    assert "pre" in [p.key for p in boards.all_profiles(profiles)]
    assert "pre" in [p.key for p in boards.profiles_for_mcu("stm32h723xx", profiles)]
    assert boards.get_profile("pre", profiles) is not None
    # Without the pre-loaded list, the (now-empty) dir is re-read, so the
    # user-only "pre" profile is gone.
    assert boards.get_profile("pre") is None


def test_duplicate_user_keys_first_file_wins_user_only(boards_env):
    # Two files, sorted order: a.json before z.json, same declared key.
    _write_json(boards_env, "a", key="samekey", name="From A")
    _write_json(boards_env, "z", key="samekey", name="From Z")

    profiles, warnings = boards.load_user_profiles()
    same = [p for p in profiles if p.key == "samekey"]
    assert len(same) == 1
    assert same[0].name == "From A"
    assert any("duplicate key 'samekey'" in w and "z.json" in w for w in warnings)

    # User-only merge path: first file wins in the merged catalog too.
    merged, _ = boards.load_catalog()
    assert [p.name for p in merged if p.key == "samekey"] == ["From A"]


def test_duplicate_user_keys_first_file_wins_shipped_shadow(boards_env, monkeypatch):
    shipped = BoardProfile(
        key="samekey",
        name="Shipped",
        mcu="stm32h723",
        bootloader_method="usb",
        flash_command="katapult",
    )
    monkeypatch.setattr(boards, "SHIPPED_PROFILES", [shipped])
    _write_json(boards_env, "a", key="samekey", name="From A")
    _write_json(boards_env, "z", key="samekey", name="From Z")

    # Shipped-shadow merge path: the FIRST user file (not the last) shadows.
    merged, warnings = boards.load_catalog()
    assert [p.name for p in merged if p.key == "samekey"] == ["From A"]
    assert any("duplicate key" in w for w in warnings)


def test_filename_key_mismatch_warns_but_loads(boards_env):
    _write_json(boards_env, "wrongname", key="realkey")

    profiles, warnings = boards.load_user_profiles()
    assert [p.key for p in profiles] == ["realkey"]
    assert any(
        "wrongname.json" in w and "realkey" in w and "does not match" in w
        for w in warnings
    )


def test_no_user_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))
    profiles, warnings = boards.load_user_profiles()
    assert profiles == []
    assert warnings == []


# ---------------------------------------------------------------------------
# fragment_drift: dropped-symbol detection (rename-hazard hardening)
# ---------------------------------------------------------------------------


def test_fragment_drift_detects_dropped_symbol():
    fragment = ["CONFIG_MACH_STM32=y", "CONFIG_STM32_FLASH_START_20200=y"]
    final = ["CONFIG_MACH_STM32=y", "CONFIG_STM32_FLASH_START_20000=y"]  # renamed upstream
    assert fragment_drift(fragment, final) == ["CONFIG_STM32_FLASH_START_20200=y"]


def test_fragment_drift_clean_when_all_survive():
    fragment = ["CONFIG_MACH_STM32=y"]
    final = ["CONFIG_MACH_STM32=y", "CONFIG_USB=y"]
    assert fragment_drift(fragment, final) == []


def test_fragment_drift_ignores_comments_and_blanks():
    fragment = ["# BTT Octopus Pro -- source: ...", "", "CONFIG_MACH_STM32=y"]
    final = ["CONFIG_MACH_STM32=y"]
    assert fragment_drift(fragment, final) == []


def test_fragment_drift_changed_value_is_not_drift():
    # Same symbol, different value = the user's edit in review (their call),
    # NOT a dropped symbol. Only a symbol ABSENT from the final config is drift.
    fragment = ["CONFIG_FLASH_APPLICATION_ADDRESS=0x8008000"]
    final = ["CONFIG_FLASH_APPLICATION_ADDRESS=0x8000000"]
    assert fragment_drift(fragment, final) == []


def test_fragment_drift_choice_deselected_in_review_is_not_drift():
    # Flash-offset symbols are Kconfig CHOICE options: re-picking a different
    # offset in review leaves the old one as "# CONFIG_X is not set" -- still
    # RECOGNIZED by this Kalico version (the user's call), not a rename.
    fragment = ["CONFIG_STM32_FLASH_START_20200=y"]
    final = [
        "# CONFIG_STM32_FLASH_START_20200 is not set",
        "CONFIG_STM32_FLASH_START_8000=y",
    ]
    assert fragment_drift(fragment, final) == []
