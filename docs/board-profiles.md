# Board Profiles

Part of the kflash docs — back to the [README](../README.md).

When you add a device (`A`), kflash detects its MCU and then offers a curated
list of **board profiles** for that MCU. Picking one pre-fills the wizard —
flash method, bootloader sub-fields (e.g. bootloader baud, SD-card board name),
Flash All role, and the display name — and seeds the device's first `.config`
from a minimal board fragment. The wizard order is **MCU → board profile →
display name** (the name is pre-filled from the profile, editable). Choose
**other** to skip the catalog and configure the device manually.

A profile is optional and **informational after add**: it drives
[config seeding](configuration.md#config-seeding) and how the device is
labeled in the UI, but it never locks later edits — you can freely change the
MCU, flash method, or sub-fields afterward.

## Shipped profiles

kflash ships **13** curated profiles. Each fact (MCU variant, crystal,
bootloader offset, comms interface) is verified against the manufacturer's
authoritative source and validated with `make olddefconfig` on a real Kalico
tree before shipping. Profiles are **never version-locked** — the fragments are
minimal and completed against your own tree, so they survive upstream Kconfig
changes.

| Board | MCU | Flash | Verified |
|-------|-----|-------|----------|
| BTT Octopus Pro v1.0/1.1 | STM32H723 | Katapult USB | Hardware |
| BTT Octopus Pro v1.0/1.1 | STM32F446 | Katapult USB | Docs |
| BTT Octopus v1.0/1.1 | STM32F446 | Katapult USB | Docs |
| BTT SKR Mini E3 V2.0 | STM32F103 | SD card | Docs |
| BTT SKR Mini E3 V3.0 | STM32G0B1 | SD card | Docs |
| BTT Manta M8P V2.0 | STM32H723 | Katapult USB | Docs |
| BTT EBB36/42 CAN v1.1/v1.2 | STM32G0B1 | Katapult CAN | Docs |
| BTT EBB SB2209/SB2240 CAN V1.0 | STM32G0B1 | Katapult CAN | Docs |
| LDO Nitehawk-36 | RP2040 | Katapult USB | Hardware |
| Mellow Fly SHT36 v1/v2 | STM32F072 | Katapult CAN | Docs |
| Mellow Fly SHT36 v3 | RP2040 | Katapult CAN | Docs |
| Fysetc Spider v2.x | STM32F446 | Make Flash (BOOT0) | Docs |
| Raspberry Pi Pico | RP2040 | Make Flash (BOOTSEL) | Docs |

**Verified** distinguishes **Hardware** (flashed successfully on a real board)
from **Docs** (built and validated against the manufacturer's documentation,
not yet flashed on hardware). Both are safe to use — the distinction is only
about how far the profile has been proven.

## Community / user profiles

Add your own board by dropping a `<key>.json` file (and an optional
`<key>.config` fragment) into `~/.config/kalico-flash/boards/`. User profiles
appear in the picker alongside the shipped ones and **shadow** a shipped
profile that uses the same key. Malformed files are skipped with a warning,
never fatal.

Minimal `<key>.json`:

```json
{
  "key": "my-custom-board",
  "name": "My Custom Board (STM32H723)",
  "mcu": "stm32h723",
  "bootloader_method": "usb",
  "flash_command": "katapult",
  "config_fragment": true,
  "sub_fields": { "bootloader_baud": 250000 },
  "role": "toolhead",
  "notes": "…",
  "source": "https://…",
  "verified": "docs",
  "checked_against": "kalico v2026.07, 2026-07-17"
}
```

- **Required:** `key`, `name`, `mcu`, `bootloader_method` (non-empty strings).
- `flash_command`: string or `null`; together with `bootloader_method` it must
  form a valid flash-method pair (see [Flash Methods](flashing.md#flash-methods)).
- `config_fragment`: `true` only if you ship a matching `<key>.config` beside
  the JSON — see the
  [fragment format](../kflash/board_configs/README.md) for how fragments are
  structured.
- `sub_fields`: known keys are type-checked (`bootloader_baud` int,
  `uf2_mount_path` / `sdcard_board` strings); unknown keys are ignored.
- The filename should match the declared `key`; `key: "other"` is reserved.
- Unknown top-level keys are ignored, so profiles written for a newer kflash
  still load on an older one.

## Known Working Hardware

Maintainer-tested configurations:

| Board | MCU | Transport | Katapult |
|-------|-----|-----------|----------|
| BTT Octopus Pro v1.1 | STM32H723 | USB | Yes |
| LDO Nitehawk 36 | RP2040 | USB | Yes |
| BTT HBB | RP2040 | USB | No |
| Blackpill STM32F411 | STM32F411 | USB | No |

kflash should work with any board appearing in `/dev/serial/by-id/` with a `Klipper_` or `katapult_` prefix, or any CAN device reachable via Katapult's `flashtool.py`.
