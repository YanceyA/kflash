"""Tests for ConfigManager seeding, markers, and defaults resolution."""

from __future__ import annotations

import pytest

from kflash.config import ConfigManager, get_defaults_dir


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated XDG config dir + klipper dir; returns (make_mgr, tmp_path)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    klipper = tmp_path / "klipper"
    klipper.mkdir()

    def make_mgr(key="octopus-pro"):
        return ConfigManager(key, str(klipper))

    return make_mgr, tmp_path


def _write_default(tmp_path, name, content="CONFIG_MACH_STM32=y\n"):
    d = get_defaults_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(content, encoding="utf-8")


class TestSeedFromDefault:
    def test_no_default_returns_none(self, env):
        make_mgr, _ = env
        mgr = make_mgr()
        assert mgr.seed_from_default("stm32h723") is None
        assert not mgr.has_cached_config()

    def test_mcu_default_seeds_cache_and_marks(self, env):
        make_mgr, tmp_path = env
        _write_default(tmp_path, "stm32h723.config")
        mgr = make_mgr()
        assert mgr.seed_from_default("stm32h723") == "mcu-default:stm32h723"
        assert mgr.has_cached_config()
        assert mgr.is_seeded()
        assert "CONFIG_MACH_STM32" in mgr.cache_path.read_text(encoding="utf-8")

    def test_global_default_fallback(self, env):
        make_mgr, tmp_path = env
        _write_default(tmp_path, "default.config")
        mgr = make_mgr()
        assert mgr.seed_from_default("rp2040") == "mcu-default:default"
        assert mgr.is_seeded()

    def test_mcu_default_wins_over_global(self, env):
        make_mgr, tmp_path = env
        _write_default(tmp_path, "default.config", "# global\n")
        _write_default(tmp_path, "rp2040.config", "# rp2040\n")
        mgr = make_mgr()
        assert mgr.seed_from_default("rp2040") == "mcu-default:rp2040"
        assert "rp2040" in mgr.cache_path.read_text(encoding="utf-8")

    def test_existing_cache_never_overwritten(self, env):
        make_mgr, tmp_path = env
        _write_default(tmp_path, "rp2040.config", "# seed content\n")
        mgr = make_mgr()
        mgr.cache_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.cache_path.write_text("# user's existing config\n", encoding="utf-8")
        assert mgr.seed_from_default("rp2040") is None
        assert mgr.cache_path.read_text(encoding="utf-8") == "# user's existing config\n"
        assert not mgr.is_seeded()

    def test_interrupted_seed_never_leaves_unmarked_cache(self, env, monkeypatch):
        """Failure invariant: an interrupted seed must never leave a seeded
        cache that is_seeded() doesn't flag -- that would silently bypass the
        forced-review gate. Marker-before-cache ordering makes it fail safe."""
        import kflash.config as config_mod

        make_mgr, tmp_path = env
        _write_default(tmp_path, "rp2040.config")
        mgr = make_mgr()

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(config_mod, "_atomic_copy", boom)
        with pytest.raises(OSError):
            mgr.seed_from_default("rp2040")

        # The invariant: never "cache seeded but not marked".
        assert not (mgr.has_cached_config() and not mgr.is_seeded())
        # With marker-first ordering the marker survives the failed copy.
        assert mgr.is_seeded()
        assert not mgr.has_cached_config()


class TestSeedMarker:
    def test_save_cached_config_clears_marker(self, env):
        make_mgr, tmp_path = env
        _write_default(tmp_path, "default.config")
        mgr = make_mgr()
        mgr.seed_from_default("rp2040")
        # simulate a menuconfig save: klipper .config exists, then cache save
        mgr.klipper_config_path.write_text("CONFIG_MCU=\"rp2040\"\n", encoding="utf-8")
        mgr.save_cached_config()
        assert not mgr.is_seeded()

    def test_seed_source_readable(self, env):
        make_mgr, tmp_path = env
        _write_default(tmp_path, "default.config")
        mgr = make_mgr()
        mgr.seed_from_default("rp2040")
        assert mgr.seed_source() == "mcu-default:default"

    def test_unseeded_manager_reports_not_seeded(self, env):
        make_mgr, _ = env
        assert not make_mgr().is_seeded()
        assert make_mgr().seed_source() is None

    def test_seed_source_none_for_empty_marker(self, env):
        make_mgr, _ = env
        mgr = make_mgr()
        mgr.seed_marker_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.seed_marker_path.write_text("", encoding="utf-8")
        assert mgr.seed_source() is None
        mgr.seed_marker_path.write_text("  \n\t\n", encoding="utf-8")
        assert mgr.seed_source() is None


def _write_user_board(key, *, config_fragment, fragment_text=None, sub_fields=None):
    """Drop a user board profile (and optional fragment) into the user dir.

    Returns the profile key. Requires XDG_CONFIG_HOME already isolated.
    """
    import json

    from kflash.boards import get_user_boards_dir

    boards_dir = get_user_boards_dir()
    boards_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "key": key,
        "name": f"Test {key}",
        "mcu": "stm32h723",
        "bootloader_method": "usb",
        "flash_command": "katapult",
        "config_fragment": config_fragment,
    }
    if sub_fields is not None:
        data["sub_fields"] = sub_fields
    (boards_dir / f"{key}.json").write_text(json.dumps(data), encoding="utf-8")
    if fragment_text is not None:
        (boards_dir / f"{key}.config").write_text(fragment_text, encoding="utf-8")
    return key


class TestSeedFromBoard:
    def test_board_fragment_seeds_cache_and_marks(self, env):
        make_mgr, _ = env
        _write_user_board(
            "btt-x", config_fragment=True, fragment_text="CONFIG_BOARD_X=y\n"
        )
        mgr = make_mgr()
        assert mgr.seed_from_board("btt-x") == "board:btt-x"
        assert mgr.has_cached_config()
        assert mgr.is_seeded()
        assert mgr.seed_source() == "board:btt-x"
        assert "CONFIG_BOARD_X" in mgr.cache_path.read_text(encoding="utf-8")

    def test_missing_profile_returns_none(self, env):
        make_mgr, _ = env
        mgr = make_mgr()
        assert mgr.seed_from_board("no-such-board") is None
        assert not mgr.has_cached_config()
        assert not mgr.is_seeded()

    def test_profile_without_fragment_flag_returns_none(self, env):
        make_mgr, _ = env
        # config_fragment=False, even though a .config file is present on disk.
        _write_user_board(
            "no-frag", config_fragment=False, fragment_text="CONFIG_X=y\n"
        )
        mgr = make_mgr()
        assert mgr.seed_from_board("no-frag") is None
        assert not mgr.has_cached_config()

    def test_fragment_flag_but_file_absent_returns_none(self, env):
        make_mgr, _ = env
        # Declares a fragment but ships no <key>.config file.
        _write_user_board("declared", config_fragment=True, fragment_text=None)
        mgr = make_mgr()
        assert mgr.seed_from_board("declared") is None
        assert not mgr.has_cached_config()

    def test_existing_cache_never_overwritten(self, env):
        make_mgr, _ = env
        _write_user_board(
            "btt-x", config_fragment=True, fragment_text="# seed content\n"
        )
        mgr = make_mgr()
        mgr.cache_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.cache_path.write_text("# user's existing config\n", encoding="utf-8")
        assert mgr.seed_from_board("btt-x") is None
        assert (
            mgr.cache_path.read_text(encoding="utf-8") == "# user's existing config\n"
        )
        assert not mgr.is_seeded()


class TestSeedFragmentRecording:
    def test_board_seed_records_fragment_config_lines(self, env):
        make_mgr, _ = env
        _write_user_board(
            "btt-x",
            config_fragment=True,
            fragment_text="# BTT X\nCONFIG_MACH_STM32=y\nCONFIG_STM32_FLASH_START_20200=y\n",
        )
        mgr = make_mgr()
        assert mgr.seed_from_board("btt-x") == "board:btt-x"
        # The label is still exactly the first line...
        assert mgr.seed_source() == "board:btt-x"
        # ...and the fragment's CONFIG_ lines are recorded alongside it (the
        # comment line is not).
        assert mgr.seed_fragment_lines() == [
            "CONFIG_MACH_STM32=y",
            "CONFIG_STM32_FLASH_START_20200=y",
        ]

    def test_default_seed_records_no_fragment_lines(self, env):
        # Full-config defaults are NOT drift-checked (a user disabling an option
        # in review would look like a dropped symbol), so no lines are recorded.
        make_mgr, tmp_path = env
        _write_default(tmp_path, "stm32h723.config", "CONFIG_MACH_STM32=y\n")
        mgr = make_mgr()
        mgr.seed_from_default("stm32h723")
        assert mgr.seed_source() == "mcu-default:stm32h723"
        assert mgr.seed_fragment_lines() == []

    def test_device_seed_records_no_fragment_lines(self, env):
        make_mgr, _ = env
        src = make_mgr("src-dev")
        src.cache_path.parent.mkdir(parents=True, exist_ok=True)
        src.cache_path.write_text("CONFIG_MACH_STM32=y\n", encoding="utf-8")
        dst = make_mgr("dst-dev")
        dst.seed_from_device("src-dev")
        assert dst.seed_fragment_lines() == []

    def test_legacy_label_only_marker_has_no_fragment_lines(self, env):
        # Backward compatibility: a marker written by an earlier version holds
        # only the label with no fragment lines -> no drift ever reported.
        make_mgr, _ = env
        mgr = make_mgr()
        mgr.seed_marker_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.seed_marker_path.write_text("board:legacy\n", encoding="utf-8")
        assert mgr.seed_source() == "board:legacy"
        assert mgr.seed_fragment_lines() == []

    def test_no_marker_has_no_fragment_lines(self, env):
        make_mgr, _ = env
        assert make_mgr().seed_fragment_lines() == []


class TestCopyFromDevice:
    def test_copy_seeds_and_marks(self, env):
        make_mgr, _ = env
        src = make_mgr("nitehawk-a")
        src.cache_path.parent.mkdir(parents=True, exist_ok=True)
        src.cache_path.write_text("CONFIG_MCU=\"rp2040\"\n", encoding="utf-8")
        dst = make_mgr("nitehawk-b")
        assert dst.seed_from_device("nitehawk-a") is True
        assert dst.has_cached_config()
        assert dst.seed_source() == "device:nitehawk-a"

    def test_copy_missing_source_returns_false(self, env):
        make_mgr, _ = env
        assert make_mgr("b").seed_from_device("no-such") is False


class TestRemoveCachedConfigClearsMarker:
    def test_remove_cached_config_deletes_seeded_marker(self, env):
        """_remove_cached_config (kflash/commands/_common.py) removes the whole
        per-device config dir, which must take the .seeded marker with it."""
        from kflash.commands._common import _remove_cached_config
        from kflash.decisions import ConfirmDecision
        from kflash.events import Emitter, NullSink

        class _AlwaysConfirm:
            def confirm(self, req: ConfirmDecision) -> bool:
                return True

        make_mgr, _ = env
        mgr = make_mgr("octopus-pro")
        mgr.cache_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.cache_path.write_text("CONFIG_MCU=\"stm32h723\"\n", encoding="utf-8")
        mgr.seed_marker_path.write_text("mcu-default:stm32h723\n", encoding="utf-8")
        assert mgr.is_seeded()

        _remove_cached_config(
            "octopus-pro", Emitter(NullSink()), _AlwaysConfirm(), prompt=False
        )

        assert not mgr.cache_path.exists()
        assert not mgr.seed_marker_path.exists()


class TestSaveAsDefault:
    def test_save_as_default_writes_mcu_default(self, env):
        make_mgr, _ = env
        mgr = make_mgr()
        mgr.cache_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.cache_path.write_text("CONFIG_MCU=\"stm32h723\"\n", encoding="utf-8")
        result = mgr.save_cache_as_default("stm32h723")
        assert (get_defaults_dir() / "stm32h723.config").exists()
        assert result == get_defaults_dir() / "stm32h723.config"

    def test_save_as_default_without_cache_raises(self, env):
        make_mgr, _ = env
        from kflash.errors import ConfigError
        with pytest.raises(ConfigError):
            make_mgr().save_cache_as_default("stm32h723")
