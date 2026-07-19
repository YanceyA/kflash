# Board config fragments

Minimal Kconfig fragments seeded by `kflash.boards.BoardProfile` when a curated
profile declares `config_fragment=True`. One file per profile, named
`<profile-key>.config` (matching `BoardProfile.key`).

These are **minimal** -- 2-10 `CONFIG_` lines that pin the board's identity
(architecture, MCU model, bootloader offset, clock, comms), never a full saved
`.config`. They are completed by `menuconfig` / `olddefconfig` against the
user's actual Klipper/Kalico tree, so they survive upstream Kconfig drift.

Each fragment carries a header comment: a one-line board title, one or more
`# source:` provenance URLs, and a `# checked:` line naming the Kalico version
the symbols were `olddefconfig`-validated against. Real example
(`btt-octopus-pro-h723.config`):

```
# BTT Octopus Pro v1.0/1.1 (STM32H723) -- Katapult USB
# source: https://github.com/bigtreetech/BIGTREETECH-OCTOPUS-V1.0/blob/master/Firmware/Klipper/README.md
# checked: kalico v2026.07, 2026-07-17
CONFIG_MACH_STM32=y
CONFIG_MACH_STM32H723=y
CONFIG_LOW_LEVEL_OPTIONS=y
CONFIG_STM32_FLASH_START_20000=y
CONFIG_STM32_CLOCK_REF_25M=y
CONFIG_STM32_USB_PA11_PA12=y
```

`CONFIG_LOW_LEVEL_OPTIONS=y` is present because the flash-offset, clock, and
comms sub-choices (and string options like `CONFIG_INITIAL_PINS`) are gated
behind it -- a fragment that pins any of them must enable it too.

User/community fragments live alongside user profiles in
`~/.config/kalico-flash/boards/<key>.config` instead of here.

The catalog currently ships 14 profiles (see `kflash/boards.py`
`SHIPPED_PROFILES` for the authoritative list). This directory is tracked in git
so shipped-fragment path resolution has a stable home.
