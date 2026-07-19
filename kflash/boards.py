"""Curated board profile catalog.

A BoardProfile pre-fills the add-device wizard (flash method, sub-fields)
and optionally seeds the device's first .config from a minimal Kconfig
fragment (kflash/board_configs/<key>.config). Fragments are MINIMAL --
2-10 CONFIG_ lines, never a full saved .config -- and are completed by
menuconfig/olddefconfig against the user's actual Klipper/Kalico tree,
so they survive upstream Kconfig drift.

Fragment-scope policy (curation batches follow this): a fragment asserts only
board-INVARIANT PHYSICAL facts -- MACH family + chip sub-choice, bootloader/flash
start offset, clock reference, and the communication-interface choice (plus the
LOW_LEVEL_OPTIONS=y that gates those sub-choices). A required pin-state without
which the chosen comms interface cannot enumerate qualifies too and IS included:
it is required-for-comms, the same category as the USB-pins choice (e.g. the SKR
Mini E3 V2 needs CONFIG_INITIAL_PINS="!PA14" or its USB never enumerates). Such
string-valued CONFIG_ lines are allowed when they meet that bar; feature-level
preferences (pin maps for peripherals, thermistor types, etc.) stay OUT and are
left to the mandatory menuconfig review. That forced review is invariant -- a
board-seeded config always requires one menuconfig pass before build/flash
(skip_menuconfig cannot bypass it) -- so shipping a required line is about the
seeded DEFAULT being correct, not about flashing anything unreviewed.

User/community profiles: drop <key>.json (+ optional <key>.config) into
~/.config/kalico-flash/boards/. Same-key user profiles shadow shipped ones.

Shipped vs user provenance is tracked on ``BoardProfile.origin`` ("shipped"
default, "user" for anything parsed from the user boards dir). That single
field drives ``fragment_path()`` resolution: shipped fragments live beside
this module in ``kflash/board_configs/``; user fragments live in the user
boards dir. ``load_user_profiles`` is the only producer of "user" profiles,
so a user JSON can never forge an "origin" of "shipped".

API shape: ``load_catalog()`` is the primary entry point -- ONE disk pass
returning the merged catalog plus any load warnings. Pickers/UIs should call
it once, surface the warnings, and pass the profile list through to
``profiles_for_mcu(mcu, profiles=...)`` / ``get_profile(key, profiles=...)``.
Calling those helpers without ``profiles`` re-reads the user dir and discards
warnings (convenience path only).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import xdg_base
from .validation import find_flash_method_pair


@dataclass(frozen=True)
class BoardProfile:
    """A curated (or user-supplied) board profile.

    Frozen for value semantics, but never hashed: ``sub_fields`` is a dict, so
    ``hash(profile)`` raises ``TypeError``. Profiles are only ever compared and
    stored in lists/dicts-by-key, never used as set members or dict keys.
    Note that frozen is SHALLOW -- ``sub_fields`` itself remains a mutable
    dict; treat it as read-only (copy before modifying).
    """

    key: str  # "btt-octopus-pro-h723" (slug, unique)
    name: str  # "BTT Octopus Pro v1.0/1.1 (STM32H723)"
    mcu: str  # "stm32h723" -- matched against detected MCU (prefix)
    bootloader_method: str  # must form a valid FlashMethodPair
    flash_command: Optional[str]
    config_fragment: bool = False  # ships a board_configs/<key>.config
    sub_fields: dict = field(default_factory=dict)  # e.g. {"bootloader_baud": 250000}
    notes: str = ""  # shown in the picker (bootloader size, quirks)
    source: str = ""  # provenance URL (manufacturer repo / docs)
    verified: str = ""  # "hardware" | "docs" | "" (unverified)
    checked_against: str = ""  # Kalico/Klipper version or date last verified
    # (e.g. "kalico v2026.06, 2026-07-16"). Informational freshness signal
    # ONLY -- shown in the picker detail line; profiles are NEVER version-locked.
    role: Optional[str] = None  # default Flash All role ("toolhead" for CAN toolboards)
    origin: str = "shipped"  # "shipped" (bundled) or "user" (loaded from user dir)

    def fragment_path(self) -> Path:
        """Resolve this profile's Kconfig fragment path.

        Shipped profiles resolve beside this module in ``board_configs/``;
        user profiles resolve in the user boards dir. Existence is NOT checked
        here (a profile may declare no fragment, or the fragment may be absent);
        callers test ``.exists()``.
        """
        if self.origin == "user":
            return get_user_boards_dir() / f"{self.key}.config"
        return Path(__file__).parent / "board_configs" / f"{self.key}.config"


def _config_symbol(line: str) -> Optional[str]:
    """Extract the ``CONFIG_NAME`` key from a ``.config`` assignment line.

    Returns the symbol name for a ``CONFIG_X=value`` line and ``None`` for
    everything else -- blanks, and comments (including the ``# CONFIG_X is not
    set`` disabled-option form, which starts with ``#``).
    """
    stripped = line.strip()
    if not stripped.startswith("CONFIG_"):
        return None
    name, sep, _value = stripped.partition("=")
    return name if sep else None


def _recognized_symbol(line: str) -> Optional[str]:
    """The ``CONFIG_NAME`` a FINAL-config line proves is recognized, or ``None``.

    Broader than :func:`_config_symbol`: also matches the ``# CONFIG_X is not
    set`` disabled-option form. A Kconfig *choice* (e.g. a flash offset) that the
    user re-picks in review leaves the deselected member as ``# CONFIG_X is not
    set`` -- the symbol is still recognized by this Kalico version (the user's
    deliberate change), NOT a rename. Only symbols entirely absent from the final
    config are drift, so both forms count as "present".
    """
    stripped = line.strip()
    if stripped.startswith("# CONFIG_") and stripped.endswith(" is not set"):
        return stripped[2 : -len(" is not set")]
    return _config_symbol(line)


def fragment_drift(
    fragment_lines: list[str], final_config_lines: list[str]
) -> list[str]:
    """Fragment ``CONFIG_`` symbols that vanished from the final ``.config``.

    A fragment line "survives" when its ``CONFIG_`` symbol name is still
    recognized by the final config -- present with the same value (unchanged),
    a different value (the user edited it in review), or the ``# CONFIG_X is not
    set`` disabled form (a choice the user re-picked). Only a symbol that is
    entirely ABSENT from the final config counts as drift: that is the signature
    of an upstream Kconfig symbol *rename*, which kconfiglib silently drops on
    load so a board fact (e.g. a bootloader offset encoded in the symbol name)
    quietly reverts to the tree's default. Comment/blank fragment lines are
    ignored.

    Returns the offending fragment lines (stripped, in fragment order).
    """
    present = {
        sym
        for line in final_config_lines
        if (sym := _recognized_symbol(line)) is not None
    }
    drift: list[str] = []
    for line in fragment_lines:
        sym = _config_symbol(line)
        if sym is not None and sym not in present:
            drift.append(line.strip())
    return drift


# Bundled catalog. Curated incrementally (Task 15, per-vendor batches).
# Each entry must satisfy find_flash_method_pair(bootloader_method, flash_command),
# and any config_fragment=True entry must ship board_configs/<key>.config.
# Every fact (MCU variant, crystal, bootloader offset, comms) is re-verified
# against the authoritative source in ``source`` and validated via
# ``make olddefconfig`` on a real Kalico tree before shipping.
# The BIGTREETECH-OCTOPUS-V1.0 repo's firmware README ("Octopus and Octopus Pro
# Klipper Setup Summary") is the OFFICIAL source for BOTH the Octopus and the
# Octopus Pro families -- the separate BIGTREETECH-OCTOPUS-Pro repo ships no
# Firmware/ docs at all, so this URL is authoritative for the Pro too.
_BTT_OCTOPUS_SOURCE = (
    "https://github.com/bigtreetech/BIGTREETECH-OCTOPUS-V1.0/blob/master/"
    "Firmware/Klipper/README.md"
)
# The BTT SKR-mini-E3 repo ships a Klipper README only for V3.x -- its V2.0
# folder is Marlin-only. The canonical, upstream-maintained menuconfig
# reference for the V2.0 board is Klipper's own generic board config header
# (STM32F103, "28KiB bootloader", USB, "Enable extra low-level configuration
# options", startup pin "!PA14", SD-card flash). It is the authoritative Klipper
# build source for this board. That header does NOT state the crystal speed, so
# the 8MHz clock-reference fact is cited separately to Voron's SKR mini E3 V2.0
# Klipper build guide, which states verbatim "Ensure that the Clock Reference is
# set to '8 Mhz'" (recorded in the fragment header's second source line):
#   https://docs.vorondesign.com/build/software/miniE3_v20_klipper.html
_BTT_SKR_MINI_E3_V2_SOURCE = (
    "https://github.com/Klipper3d/klipper/blob/master/config/"
    "generic-bigtreetech-skr-mini-e3-v2.0.cfg"
)
_BTT_SKR_MINI_E3_V3_SOURCE = (
    "https://github.com/bigtreetech/BIGTREETECH-SKR-mini-E3/blob/master/"
    "firmware/V3.0/Klipper/README.md"
)
# The Manta M8P V2.0 repo ships no Klipper README; its generic printer.cfg
# header is the OFFICIAL firmware-build reference (STM32H723, "128KiB
# bootloader", "25 MHz crystal", "USB (on PA11/PA12)"). BTT flashes a 128KiB
# Katapult bootloader (M8P_V2_H723_bootloader.bin) via DFU at 0x8000000, so the
# Klipper app sits at the 128KiB offset and is flashed over USB with Katapult.
_BTT_MANTA_M8P_SOURCE = (
    "https://github.com/bigtreetech/Manta-M8P/blob/master/V2.0/Firmware/"
    "generic-bigtreetech-manta-m8p-V2_0.cfg"
)
# The BTT EBB repo's root README is the OFFICIAL firmware-build reference for the
# EBB36 and EBB42 CAN toolheads (STM32G0B1, "8 MHz crystal", "8KiB bootloader"
# with CanBoot/Katapult, "CAN bus (on PB0/PB1)", CAN speed 1M). EBB36 and EBB42
# are the same electronics in two board sizes; v1.1 and v1.2 differ only in the
# hotend MOSFET pin (PA2 vs PB13) -- not a firmware-build fact -- so a single
# profile covers all four.
_BTT_EBB36_42_SOURCE = (
    "https://github.com/bigtreetech/EBB/blob/master/README.md"
)
# The BTT EBB SB2240/2209 CAN boards share ONE BTT directory, one readme, and one
# sample config; the SB2240 and SB2209 differ only in the onboard stepper driver
# (TMC2240 vs TMC2209), not the firmware build. The combined sample config header
# is authoritative for MCU/clock/CAN pins ("STM32G0B1", "8 MHz crystal", "CAN bus
# (on PB0/PB1)"); the BTT docs page supplies the 8KiB Katapult offset fact, so
# both are cited (Batch B dual-source pattern). NOTE: entirely distinct from the
# RP2040-based "EBB SB2209 CAN (RP2040)" and "EBB SB2209 USB" toolboards, which
# are NOT this profile.
_BTT_SB2209_2240_CAN_SOURCE = (
    "https://github.com/bigtreetech/EBB/blob/master/EBB%20SB2240_2209%20CAN/"
    "sample-bigtreetech-ebb-sb-canbus-v1.0.cfg"
)
# The BTT HBB repo ships a User Manual and a sample Klipper config. The manual's
# section 4.1 "Compiling the Firmware" is the OFFICIAL firmware-build reference
# (RP2040, "Bootloader offset (No bootloader)", "Flash chip (W25Q080 with CLKDIV
# 2)", "Communication interface (USB)", and an EMPTY "GPIO pins to set at
# micro-controller startup" -- no required startup pin); the sample cfg confirms
# the USB serial identity (Klipper_rp2040_hbb). The HBB is a Klipper macro keypad
# (7 keys + 7 WS2812B RGB), NOT a toolboard -- USB only, no CAN, no Katapult
# (matching kflash's Known Working Hardware entry: "BTT HBB | RP2040 | USB | No").
# HBB and HBB Fe are the same electronics (the Fe lacks silk screening), so one
# profile covers both. This is the same bare-RP2040 build/flash shape as the
# generic Raspberry Pi Pico -- the sample cfg is cited as primary source, with the
# manual's menuconfig section named in the fragment header (dual-source pattern).
_BTT_HBB_SOURCE = (
    "https://github.com/bigtreetech/HBB/blob/master/sample-bigtreetech-hbb.cfg"
)
# Optional Katapult variant of the HBB. BTT ships the HBB as a no-bootloader USB
# board (the stock btt-hbb profile above); installing Katapult is a community/DIY
# add-on NOT documented by BTT. Katapult supports the RP2040 over USB and is
# deployed the same way the stock board is flashed -- via the RP2040 ROM
# bootloader (hold BOOT, then `make flash`, or drag-drop katapult.uf2). The Klipper
# app then moves to the 16KiB offset (FLASH_START_4000) and is flashed over USB
# through Katapult without pressing BOOT again. The 16KiB app offset is the
# established RP2040-Katapult convention -- identical to the docs-verified in-repo
# ldo-nitehawk-36 (RP2040, Katapult USB) profile; Katapult's own repo documents the
# RP2040 USB support and BOOT-button deploy flow (github.com/Arksine/katapult, cited
# in the fragment header). ``source`` carries the HBB board provenance either way.
_BTT_CHECKED = "kalico v2026.07, 2026-07-17"

# --- Batch D sources (LDO / Mellow / Fysetc / Raspberry Pi) ---
# LDO's official Nitehawk-36 toolboard docs are the firmware-build reference
# (RP2040, "16KiB bootloader" -- LDO warns explicitly that not setting this
# offset ERASES the pre-installed Katapult bootloader -- USBSERIAL over USB,
# flashed with `make flash FLASH_DEVICE=`). The board ships with Katapult at the
# 16KiB offset, so kflash flashes it the Katapult-over-USB way (same as the
# Octopus Pro). gpio8 on this board is the PCB activity LED (ACT_LED), NOT a
# comms-required pin, so it is intentionally left OUT of the fragment (a startup
# pin ships only when the comms interface cannot enumerate without it).
_LDO_NITEHAWK_36_SOURCE = (
    "https://docs.ldomotors.com/en/Toolboard/nitehawk-36"
)
# Mellow's official SHT36 v2 CAN/CanBoot build page is authoritative for the
# STM32F072 variant (v1 and v2 share the same STM32F072 build: "8KiB bootloader",
# "8 MHz crystal", "CAN bus (on PB8/PB9)", CAN speed 500000). meteyou's SHT-36 v1
# Klipper guide confirms the identical v1 settings (Batch B/C dual-source pattern).
# NOTE: SHT36 v2 units shipped before 2022-10-18 use a GD32F103/STM32F103 MCU --
# this profile is scoped to STM32F072 (MCU prefix match), so those older F103
# boards are simply not offered it (no misseed).
_MELLOW_SHT36_V1_V2_SOURCE = (
    "https://mellow-3d.github.io/fly-sht36_v2_canboot_can.html"
)
_MELLOW_SHT36_V1_SOURCE = (
    "https://docs.meteyou.wtf/mellow-fly-sht-v1/klipper/"
)
# Mellow's official SHT36 v3 CAN firmware-compilation page is authoritative for
# the RP2040 variant ("16KiB bootloader" Katapult pre-installed, CAN bus with
# RX=gpio1 / TX=gpio0, CAN speed 1M, and the startup pin "!gpio5", which Mellow's
# CAN build instructions REQUIRE be set at micro-controller startup -- so it ships
# in the fragment as CONFIG_INITIAL_PINS the same category as the SKR mini E3 V2's
# !PA14 does for USB). (Mellow does not state what gpio5 drives; community reports
# describe it as the onboard CAN transceiver enable -- noted here as inference, not
# a sourced fact.) The Mellow-3D/klipper-docs flash.md repeats the same list.
_MELLOW_SHT36_V3_SOURCE = (
    "https://mellow.klipper.cn/en/docs/ProductDoc/ToolBoard/fly-sht36/"
    "sht36_v3/flash/can/"
)
# Voron's official Spider Klipper build guide + Fysetc's own SPIDER repo README
# both state the stock v2.x factory config: STM32F446, "32KiB bootloader"
# (0x8008000 -- the board ships with a bootloader from the factory), "12 MHz
# crystal", USB on PA11/PA12. Bootloader entry is via the BOOT0 jumper (manual),
# then `make flash FLASH_DEVICE=`; an SD-card firmware.bin path also exists. (A
# community "wipe the bootloader, go bare at 0x8000000" USB path exists but is
# non-stock and is deliberately not modeled here.)
_FYSETC_SPIDER_SOURCE = (
    "https://docs.vorondesign.com/build/software/spider_klipper.html"
)
_FYSETC_SPIDER_REPO_SOURCE = (
    "https://github.com/FYSETC/FYSETC-SPIDER/blob/main/README.md"
)
# Klipper is authoritative for the generic Raspberry Pi Pico (Batch B dual-source
# split). Board identity (RP2040 arch, USBSERIAL comms) comes from Klipper's own
# rp2040 Kconfig; the flash procedure (hold BOOTSEL for the RP2040's built-in USB
# mass-storage bootloader, then `make flash FLASH_DEVICE=first`) is documented in
# Klipper's Measuring_Resonances guide. That flash step is exactly the none +
# make_flash ("built-in USB bootloader") pair, with no fragile uf2-mount-path
# sub-field required. (Klipper ships no generic-raspberry-pi-pico.cfg -- the
# config/ dir has only generic-bigtreetech-skr-pico-v1.0.cfg -- so the rp2040
# Kconfig is the identity source, as the plan's "Kconfig defaults" option allows.)
_RPI_PICO_SOURCE = (
    "https://github.com/Klipper3d/klipper/blob/master/src/rp2040/Kconfig"
)
_RPI_PICO_FLASH_SOURCE = (
    "https://www.klipper3d.org/Measuring_Resonances.html"
)
_BATCH_D_CHECKED = "kalico v2026.07, 2026-07-17"

SHIPPED_PROFILES: list[BoardProfile] = [
    BoardProfile(
        key="btt-octopus-pro-h723",
        name="BTT Octopus Pro v1.0/1.1 (STM32H723)",
        mcu="stm32h723",
        bootloader_method="usb",
        flash_command="katapult",
        config_fragment=True,
        notes=(
            "128KiB Katapult bootloader offset (0x8020000), 25MHz crystal, "
            "USB on PA11/PA12. Requires Katapult installed on the board."
        ),
        source=_BTT_OCTOPUS_SOURCE,
        verified="hardware",
        checked_against="kalico v2026.07, 2026-07-18",
    ),
    BoardProfile(
        key="btt-octopus-pro-f446",
        name="BTT Octopus Pro v1.0/1.1 (STM32F446)",
        mcu="stm32f446",
        bootloader_method="usb",
        flash_command="katapult",
        config_fragment=True,
        notes=(
            "32KiB Katapult bootloader offset (0x8008000), 12MHz crystal, "
            "USB on PA11/PA12. Requires Katapult installed on the board."
        ),
        source=_BTT_OCTOPUS_SOURCE,
        verified="docs",
        checked_against=_BTT_CHECKED,
    ),
    BoardProfile(
        key="btt-octopus-f446",
        name="BTT Octopus v1.0/1.1 (STM32F446)",
        mcu="stm32f446",
        bootloader_method="usb",
        flash_command="katapult",
        config_fragment=True,
        notes=(
            "32KiB Katapult bootloader offset (0x8008000), 12MHz crystal, "
            "USB on PA11/PA12. Requires Katapult installed on the board."
        ),
        source=_BTT_OCTOPUS_SOURCE,
        verified="docs",
        checked_against=_BTT_CHECKED,
    ),
    BoardProfile(
        key="btt-skr-mini-e3-v2",
        name="BTT SKR Mini E3 V2.0 (STM32F103)",
        mcu="stm32f103",
        bootloader_method="none",
        flash_command="flash_sdcard",
        config_fragment=True,
        sub_fields={"sdcard_board": "btt-skr-mini-e3-v2"},
        notes=(
            "28KiB onboard bootloader offset (0x8007000), 8MHz crystal, "
            "USB on PA11/PA12. Stock SD-card flash ('make flash' does not work); "
            "Klipper's flash_sdcard board name is 'btt-skr-mini-e3-v2'. "
            "INITIAL_PINS pre-set to !PA14 (required -- USB does not enumerate "
            "without it)."
        ),
        source=_BTT_SKR_MINI_E3_V2_SOURCE,
        verified="docs",
        checked_against=_BTT_CHECKED,
    ),
    BoardProfile(
        key="btt-skr-mini-e3-v3",
        name="BTT SKR Mini E3 V3.0 (STM32G0B1)",
        mcu="stm32g0b1",
        bootloader_method="none",
        flash_command="flash_sdcard",
        config_fragment=True,
        sub_fields={"sdcard_board": "btt-skr-mini-e3-v3"},
        notes=(
            "8KiB onboard bootloader offset (0x8002000), 8MHz crystal, "
            "USB on PA11/PA12. Stock SD-card flash ('make flash' does not work); "
            "Klipper's flash_sdcard board name is 'btt-skr-mini-e3-v3'."
        ),
        source=_BTT_SKR_MINI_E3_V3_SOURCE,
        verified="docs",
        checked_against=_BTT_CHECKED,
    ),
    BoardProfile(
        key="btt-manta-m8p-h723",
        name="BTT Manta M8P V2.0 (STM32H723)",
        mcu="stm32h723",
        bootloader_method="usb",
        flash_command="katapult",
        config_fragment=True,
        notes=(
            "128KiB Katapult bootloader offset (0x8020000), 25MHz crystal, "
            "USB on PA11/PA12. Requires Katapult installed on the board -- BTT "
            "ships a Katapult bootloader flashed via DFU (dfu-util) at 0x8000000; "
            "also flashable over CAN (PD0/PD1) or SD card."
        ),
        source=_BTT_MANTA_M8P_SOURCE,
        verified="docs",
        checked_against=_BTT_CHECKED,
    ),
    BoardProfile(
        key="btt-ebb36-42-can",
        name="BTT EBB36/42 CAN v1.1/v1.2 (STM32G0B1)",
        mcu="stm32g0b1",
        bootloader_method="can",
        flash_command="katapult_can",
        config_fragment=True,
        notes=(
            "8KiB Katapult bootloader offset (0x8002000), 8MHz crystal, CAN on "
            "PB0/PB1. EBB36 and EBB42 share identical electronics/firmware "
            "(board size differs); v1.1 vs v1.2 differ only in the hotend MOSFET "
            "pin, not the build. Requires Katapult over CAN. CAN bitrate (1M "
            "typical) is a bus-wide setting picked in menuconfig, not seeded here."
        ),
        source=_BTT_EBB36_42_SOURCE,
        verified="docs",
        checked_against=_BTT_CHECKED,
        role="toolhead",
    ),
    BoardProfile(
        key="btt-sb2209-sb2240-can",
        name="BTT EBB SB2209/SB2240 CAN V1.0 (STM32G0B1)",
        mcu="stm32g0b1",
        bootloader_method="can",
        flash_command="katapult_can",
        config_fragment=True,
        notes=(
            "8KiB Katapult bootloader offset (0x8002000), 8MHz crystal, CAN on "
            "PB0/PB1. SB2209 and SB2240 CAN V1.0 share one BTT firmware (differ "
            "only in stepper driver: TMC2209 vs TMC2240). NOT the RP2040-based "
            "'SB2209 CAN' or 'SB2209 USB' toolboards -- those are different chips. "
            "Requires Katapult over CAN. CAN bitrate (1M typical) is a bus-wide "
            "setting picked in menuconfig, not seeded here."
        ),
        source=_BTT_SB2209_2240_CAN_SOURCE,
        verified="docs",
        checked_against=_BTT_CHECKED,
        role="toolhead",
    ),
    BoardProfile(
        key="btt-hbb",
        name="BTT HBB / HBB Fe V1.0 (RP2040)",
        mcu="rp2040",
        bootloader_method="none",
        flash_command="make_flash",
        config_fragment=True,
        notes=(
            "Klipper macro keypad (7 keys + 7 WS2812B RGB), USB (NOT a CAN "
            "toolboard). No bootloader (FLASH_START 0x100, RP2040 boot2 "
            "reserve), W25Q080 flash, USB. Hold the BOOT button while "
            "connecting USB to enter the RP2040's built-in DFU bootloader, then "
            "'make flash FLASH_DEVICE=2e8a:0003'; after the first flash, "
            "'make flash FLASH_DEVICE=/dev/serial/by-id/usb-Klipper_rp2040_hbb-if00' "
            "over USB. HBB and HBB Fe share the same build. No Katapult."
        ),
        source=_BTT_HBB_SOURCE,
        verified="hardware",
        checked_against="kalico v2026.07, 2026-07-19",
    ),
    BoardProfile(
        key="btt-hbb-katapult",
        name="BTT HBB / HBB Fe V1.0 (RP2040, Katapult)",
        mcu="rp2040",
        bootloader_method="usb",
        flash_command="katapult",
        config_fragment=True,
        notes=(
            "COMMUNITY/DIY variant -- NOT BTT-documented. Same HBB keypad as the "
            "stock 'btt-hbb' profile, but with Katapult installed for button-free "
            "USB flashing. 16KiB Katapult bootloader offset (0x4000), USB. Requires "
            "Katapult installed first: hold BOOT while connecting USB, flash "
            "katapult.uf2 via the RP2040 ROM bootloader ('make flash "
            "FLASH_DEVICE=2e8a:0003' or drag-drop the .uf2), then build Klipper at "
            "the 16KiB offset and flash it over USB through Katapult. Use the stock "
            "'btt-hbb' profile if you have NOT installed Katapult."
        ),
        source=_BTT_HBB_SOURCE,
        verified="docs",
        checked_against="kalico v2026.07, 2026-07-19",
    ),
    BoardProfile(
        key="ldo-nitehawk-36",
        name="LDO Nitehawk-36 (RP2040)",
        mcu="rp2040",
        bootloader_method="usb",
        flash_command="katapult",
        config_fragment=True,
        notes=(
            "16KiB Katapult bootloader offset (0x4000), USB toolboard (NOT CAN). "
            "Ships with Katapult pre-installed -- setting the wrong bootloader "
            "offset erases it. LDO documents 'make flash FLASH_DEVICE=' as an "
            "alternative; flash method is editable after add. gpio8 is the PCB "
            "activity LED, not a firmware-build fact."
        ),
        source=_LDO_NITEHAWK_36_SOURCE,
        verified="hardware",
        checked_against="kalico v2026.07, 2026-07-18",
    ),
    BoardProfile(
        key="mellow-fly-sht36-v1-v2",
        name="Mellow Fly SHT36 v1/v2 (STM32F072)",
        mcu="stm32f072",
        bootloader_method="can",
        flash_command="katapult_can",
        config_fragment=True,
        notes=(
            "8KiB Katapult/CanBoot bootloader offset (0x8002000), 8MHz crystal, "
            "CAN on PB8/PB9. v1 and v2 share the STM32F072 build. Requires "
            "Katapult over CAN. CAN bitrate (500k typical) is a bus-wide setting "
            "picked in menuconfig, not seeded here. NOTE: SHT36 v2 units shipped "
            "before 2022-10-18 use a GD32F103/STM32F103 MCU -- not covered by "
            "this profile (use manual setup for those)."
        ),
        source=_MELLOW_SHT36_V1_V2_SOURCE,
        verified="docs",
        checked_against=_BATCH_D_CHECKED,
        role="toolhead",
    ),
    BoardProfile(
        key="mellow-fly-sht36-v3",
        name="Mellow Fly SHT36 v3 (RP2040)",
        mcu="rp2040",
        bootloader_method="can",
        flash_command="katapult_can",
        config_fragment=True,
        notes=(
            "16KiB Katapult bootloader offset (0x4000), CAN with RX=gpio1 / "
            "TX=gpio0. INITIAL_PINS pre-set to !gpio5 (required at startup by "
            "Mellow's CAN build instructions). Ships with Katapult pre-installed. "
            "Requires Katapult over CAN. CAN bitrate (1M typical) is a bus-wide "
            "setting picked in menuconfig, not seeded here."
        ),
        source=_MELLOW_SHT36_V3_SOURCE,
        verified="docs",
        checked_against=_BATCH_D_CHECKED,
        role="toolhead",
    ),
    BoardProfile(
        key="fysetc-spider-v2",
        name="Fysetc Spider v2.x (STM32F446)",
        mcu="stm32f446",
        bootloader_method="manual",
        flash_command="make_flash",
        config_fragment=True,
        notes=(
            "32KiB factory bootloader offset (0x8008000), 12MHz crystal, USB on "
            "PA11/PA12. Ships with a bootloader from the factory. Enter the "
            "bootloader via the BOOT0 jumper, then 'make flash'; an SD-card "
            "firmware.bin path also works. No Katapult."
        ),
        source=_FYSETC_SPIDER_SOURCE,
        verified="docs",
        checked_against=_BATCH_D_CHECKED,
    ),
    BoardProfile(
        key="raspberry-pi-pico",
        name="Raspberry Pi Pico (RP2040)",
        mcu="rp2040",
        bootloader_method="none",
        flash_command="make_flash",
        config_fragment=True,
        notes=(
            "Bare RP2040, no bootloader (FLASH_START 0x100, RP2040 boot2 "
            "reserve). Hold BOOTSEL while connecting USB to enter the RP2040's "
            "built-in USB bootloader, then 'make flash FLASH_DEVICE=first'. "
            "USBSERIAL. Generic Pico -- carrier boards with their own "
            "bootloader/wiring may need manual setup."
        ),
        source=_RPI_PICO_SOURCE,
        verified="docs",
        checked_against=_BATCH_D_CHECKED,
    ),
]


def get_user_boards_dir() -> Path:
    """XDG directory for user/community board profiles: ~/.config/kalico-flash/boards/."""
    return xdg_base() / "kalico-flash" / "boards"


# Known sub_field keys and the JSON value type the wizard requires. Only these
# keys are read downstream (commands/device_add.py), where a wrong value type
# would otherwise crash mid-wizard (e.g. int("fast") on a bad bootloader_baud).
# bool is excluded from the int check even though it subclasses int -- True/False
# is never a valid baud. Unknown sub_field keys are NOT type-checked: they are
# permitted to load and simply ignored downstream, so a forward-compatible
# profile carrying a future field still parses on an older kflash.
def _validate_sub_field_values(sub_fields: dict) -> None:
    """Raise ValueError if a KNOWN sub_field key carries a wrong-typed value.

    ``bootloader_baud`` must be an int (bool rejected); ``uf2_mount_path`` and
    ``sdcard_board`` must be strings. Unknown keys pass unchecked.
    """
    baud = sub_fields.get("bootloader_baud")
    if "bootloader_baud" in sub_fields and (
        not isinstance(baud, int) or isinstance(baud, bool)
    ):
        raise ValueError(
            f"sub_field 'bootloader_baud' must be an integer, got {type(baud).__name__}"
        )
    for str_key in ("uf2_mount_path", "sdcard_board"):
        if str_key in sub_fields and not isinstance(sub_fields[str_key], str):
            raise ValueError(
                f"sub_field '{str_key}' must be a string, "
                f"got {type(sub_fields[str_key]).__name__}"
            )


def _profile_from_dict(data: object) -> BoardProfile:
    """Build a user BoardProfile from parsed JSON, or raise ValueError.

    Enforces the required-string keys, the null-or-string flash_command, a
    valid flash-method pair, dict-typed sub_fields, and the value types of the
    KNOWN sub_field keys (see :func:`_validate_sub_field_values`). Unknown keys
    (including a user-supplied ``origin``) are ignored; ``origin`` is forced to
    "user".
    """
    if not isinstance(data, dict):
        raise ValueError("root is not a JSON object")

    def req_str(field_name: str) -> str:
        value = data.get(field_name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"missing or non-string required field '{field_name}'")
        return value

    def opt_str(field_name: str) -> str:
        value = data.get(field_name, "")
        if not isinstance(value, str):
            raise ValueError(f"field '{field_name}' must be a string")
        return value

    key = req_str("key")
    if key == "other":
        # "other" is the reserved manual-setup sentinel in
        # ChooseBoardProfileDecision's return contract; a profile with this
        # key would be silently treated as "manual setup" by the wizard and
        # render a duplicate row in the picker.
        raise ValueError("profile key 'other' is reserved for manual setup")
    name = req_str("name")
    mcu = req_str("mcu")
    bootloader_method = req_str("bootloader_method")

    flash_command = data.get("flash_command")
    if flash_command is not None and not isinstance(flash_command, str):
        raise ValueError("field 'flash_command' must be a string or null")

    if find_flash_method_pair(bootloader_method, flash_command) is None:
        raise ValueError(
            f"invalid flash-method pair: bootloader '{bootloader_method}' "
            f"+ flash command '{flash_command}'"
        )

    sub_fields = data.get("sub_fields", {})
    if not isinstance(sub_fields, dict):
        raise ValueError("field 'sub_fields' must be a JSON object")
    _validate_sub_field_values(sub_fields)

    role = data.get("role")
    if role is not None and not isinstance(role, str):
        raise ValueError("field 'role' must be a string or null")

    return BoardProfile(
        key=key,
        name=name,
        mcu=mcu,
        bootloader_method=bootloader_method,
        flash_command=flash_command,
        config_fragment=bool(data.get("config_fragment", False)),
        sub_fields=dict(sub_fields),
        notes=opt_str("notes"),
        source=opt_str("source"),
        verified=opt_str("verified"),
        checked_against=opt_str("checked_against"),
        role=role,
        origin="user",
    )


def load_user_profiles() -> tuple[list[BoardProfile], list[str]]:
    """Load every ``*.json`` profile from the user boards dir.

    Returns ``(profiles, warnings)``. Malformed files (bad JSON, wrong root
    type, missing/mistyped required keys, invalid flash-method pair) are
    SKIPPED and a human-readable warning string is collected -- this never
    raises on arbitrary user data. Unknown JSON keys are ignored.

    Determinism rules (files processed in sorted filename order):

    * A file whose declared ``key`` differs from its filename stem loads, but
      warns -- docs/board-profiles.md documents ``<key>.json``, and the fragment lookup
      (``<key>.config``) keys off the DECLARED key, not the filename.
    * Duplicate declared keys across files: the FIRST file (sorted order)
      wins; later duplicates are skipped with a warning. Returned keys are
      therefore unique, so both merge paths in ``load_catalog`` resolve the
      same profile.
    """
    boards_dir = get_user_boards_dir()
    profiles: list[BoardProfile] = []
    warnings: list[str] = []
    seen_keys: dict[str, str] = {}  # declared key -> filename that claimed it

    if not boards_dir.is_dir():
        return profiles, warnings

    for path in sorted(boards_dir.glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            warnings.append(f"Skipping board profile '{path.name}': cannot read ({exc})")
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            warnings.append(f"Skipping board profile '{path.name}': invalid JSON ({exc})")
            continue
        try:
            profile = _profile_from_dict(data)
        except ValueError as exc:
            warnings.append(f"Skipping board profile '{path.name}': {exc}")
            continue

        if profile.key != path.stem:
            warnings.append(
                f"Board profile '{path.name}': declared key '{profile.key}' does not "
                f"match filename stem '{path.stem}' (expected '{profile.key}.json')"
            )
        if profile.key in seen_keys:
            warnings.append(
                f"Skipping board profile '{path.name}': duplicate key '{profile.key}' "
                f"(already defined by '{seen_keys[profile.key]}')"
            )
            continue
        seen_keys[profile.key] = path.name
        profiles.append(profile)

    return profiles, warnings


def load_catalog() -> tuple[list[BoardProfile], list[str]]:
    """Load the full catalog in ONE disk pass: shipped + user overlay + warnings.

    Primary API for pickers/UIs: call once, surface the warnings, and pass the
    profile list through to ``profiles_for_mcu`` / ``get_profile`` via their
    ``profiles`` parameter (avoids re-reading the user dir and re-discarding
    warnings).

    Merge: user profiles shadow shipped profiles sharing the same key. Order
    is shipped-first, with each shipped entry replaced in place by its user
    override, then any user-only profiles appended (sorted filename order).
    """
    user_profiles, warnings = load_user_profiles()
    # Keys are unique within user_profiles (load_user_profiles keeps the first
    # sorted-filename claimant), so this dict resolves identically to the
    # ordered scan below -- both merge paths are first-wins.
    user_by_key = {p.key: p for p in user_profiles}

    merged: list[BoardProfile] = []
    seen: set[str] = set()
    for shipped in SHIPPED_PROFILES:
        merged.append(user_by_key.get(shipped.key, shipped))
        seen.add(shipped.key)
    for user in user_profiles:
        if user.key not in seen:
            merged.append(user)
            seen.add(user.key)
    return merged, warnings


def all_profiles(profiles: Optional[list[BoardProfile]] = None) -> list[BoardProfile]:
    """The merged catalog (shipped + user overlay; user wins on key collision).

    Pass ``profiles`` (from ``load_catalog()``) to reuse an already-loaded
    catalog; with ``None`` this loads internally and DISCARDS load warnings --
    convenience path only.
    """
    if profiles is not None:
        return list(profiles)
    merged, _warnings = load_catalog()
    return merged


def profiles_for_mcu(
    mcu: str, profiles: Optional[list[BoardProfile]] = None
) -> list[BoardProfile]:
    """Profiles whose MCU matches ``mcu`` by bidirectional prefix.

    Mirrors ``ConfigManager.validate_mcu`` exactly (case-sensitive): a profile
    matches when either string is a prefix of the other, so a profile for
    ``stm32h723`` matches a detected ``stm32h723xx`` and vice versa. Inherited
    contract: an EMPTY detected ``mcu`` is a prefix of everything and matches
    ALL profiles (callers gate on a non-empty detection if that is unwanted).

    Pass ``profiles`` (from ``load_catalog()``) to reuse an already-loaded
    catalog; ``None`` loads internally, discarding warnings.
    """
    return [
        p
        for p in all_profiles(profiles)
        if mcu.startswith(p.mcu) or p.mcu.startswith(mcu)
    ]


def get_profile(
    key: str, profiles: Optional[list[BoardProfile]] = None
) -> Optional[BoardProfile]:
    """The profile registered under ``key`` (user overlay applied), or None.

    Pass ``profiles`` (from ``load_catalog()``) to reuse an already-loaded
    catalog; ``None`` loads internally, discarding warnings.
    """
    for p in all_profiles(profiles):
        if p.key == key:
            return p
    return None


def profile_display_name(
    key: str, profiles: Optional[list[BoardProfile]] = None
) -> str:
    """The display name for ``key``, or ``key`` itself when it does not resolve.

    Convenience for UIs surfacing a device's stored ``board`` key: a user
    profile may have been deleted after a device recorded it, so an unresolved
    key degrades to the raw key rather than vanishing from the view.

    Pass ``profiles`` (from ``load_catalog()``) to reuse an already-loaded
    catalog; ``None`` loads internally, discarding warnings.
    """
    profile = get_profile(key, profiles)
    return profile.name if profile is not None else key
