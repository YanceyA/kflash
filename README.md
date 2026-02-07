# kflash

Interactive TUI for building and flashing [Kalico](https://docs.kalico.gg) and Klipper firmware on Raspberry Pi and similar Linux SBCs. Replaces the manual `make menuconfig` / `make` / `make flash` cycle with device profiles, cached configs, and a guided interactive flow — for both USB and CAN bus devices.

No pip dependencies. Runs on Python stdlib only.

## Requirements

- Python 3.9+
- Linux terminal session (SSH or local TTY)
- Run as a normal user (kflash exits if launched as root)
- `make` and `arm-none-eabi-gcc` (typically installed as part of Klipper setup)
- Kalico or Klipper source tree (default `~/klipper`)
- Katapult source tree for Katapult and CAN flash methods (default `~/katapult`)
- `sudo` for Klipper service stop/start during flash (passwordless recommended)
- Moonraker (recommended — enables print safety checks, firmware version display, and CAN device status)

## Install

```bash
git clone https://github.com/YanceyA/kflash.git ~/kflash
cd ~/kflash
./install.sh
kflash
```

The installer:

- Creates a `kflash` symlink in `~/.local/bin` (adds to `PATH` if needed)
- Checks for prerequisites (`python3`, `arm-none-eabi-gcc`, `dialout` group, `sudo`)
- Optionally installs `ccache` for faster rebuilds
- Accepts `./install.sh --yes` for non-interactive install
- Symlink-based — `git pull` updates take effect immediately

## Quick Start

1. Launch with `kflash`
2. Press **A** to add your first device — select a connected USB device or enter a CAN UUID
3. Press **F** to flash — kflash walks you through config, build, and flash
4. On subsequent flashes, your `.config` is cached so the process is faster

That's it. kflash handles Klipper service management, bootloader entry, and device verification automatically.

## Main Menu

| Key | Action | Description |
|-----|--------|-------------|
| `F` | Flash Device | Build and flash a single device (guided workflow) |
| `B` | Flash All | Batch-flash all connected devices using cached configs |
| `A` | Add Device | Register a new USB or CAN device |
| `E` | Config Device | Edit device name, MCU, flash method, or exclusion |
| `D` | Refresh Devices | Re-scan USB bus for connected devices |
| `R` | Remove Device | Delete a device from the registry |
| `C` | Settings | Configure global options (ccache, paths, delays) |
| `Q` | Quit | Exit kflash |

## Flash Workflow

Single-device flash (`F`) runs four phases:

1. **Discovery** — Scan USB or check CAN target, validate transport, run Moonraker print-safety check
2. **Config** — Load cached `.config`, optionally launch `menuconfig`, validate MCU type matches registry
3. **Build** — `make clean` + `make -j$(nproc)` with 300s timeout
4. **Flash** — Stop Klipper, enter bootloader, flash firmware, verify device returns, restart Klipper

**Flash All** (`B`) builds and flashes all connected, flashable devices that have a cached `.config`. Devices are flashed in role-based order for CAN safety (see [CAN Bus Support](#can-bus-support)).

### Safety Checks

Before flashing, kflash queries Moonraker to check printer state:

- **Blocked states:** `printing`, `paused`, `startup` — flashing is prevented with a clear message
- **Error state:** prompts for confirmation (flashing may be needed to recover)
- **Moonraker unreachable:** prompts for explicit confirmation before proceeding
- **Klipper service:** if active before flash, restart is guaranteed on all exit paths (success, failure, Ctrl+C)
- **Version warnings:** flags when host Klipper has uncommitted changes (`-dirty`) or when MCU firmware would be a downgrade

### Timeouts

| Operation | Default |
|-----------|---------|
| Build | 300s |
| USB flash (`katapult` / `make_flash`) | 60s |
| CAN flash (`katapult_can`) | 120s |
| USB device reappearance | 30s |
| CAN post-flash verify | 15s |

## Flash Methods

Each device stores a bootloader entry + flash command pair. The configured method runs directly with no fallback.

| Method | Bootloader + Flash | Notes |
|--------|-------------------|-------|
| Katapult USB | `usb` + `katapult` | [Katapult](https://github.com/Arksine/katapult) bootloader over USB |
| Make Flash USB | `usb` + `make_flash` | Klipper `make flash` over USB |
| Katapult Serial | `serial` + `katapult` | Requires `bootloader_baud` (default `250000`) |
| Katapult Manual | `manual` + `katapult` | User manually enters bootloader before flash |
| Make Flash Manual | `manual` + `make_flash` | Manual bootloader entry with `make flash` |
| UF2 Copy | `manual` + `uf2_mount` | Copy firmware to UF2 mount (BOOTSEL mode) |
| Make Flash Direct | `none` + `make_flash` | No bootloader step — flash directly |
| SD Card Flash | `none` + `flash_sdcard` | Requires `sdcard_board` identifier |
| Katapult CAN | `can` + `katapult_can` | Requires `canbus_uuid` + `canbus_interface` |
| Build Only | `none` + `none` | Compile firmware without flashing |

### RP2040 / RP2350 Behavior

For RP2040 and RP2350 MCUs, some flash methods that rely on USB serial re-enumeration are unreliable due to RP2 ROM boot behavior. Kflash automatically filters these from the method picker:

- **Make Flash Direct** is prioritized for RP2 devices
- **UF2 Copy** remains available for BOOTSEL workflows
- USB vs CAN transport constraints still apply

## CAN Bus Support

> **Note:** CAN flashing is currently untested, but uses known valid methods, and should be considered experimental. Use at your own risk.

CAN is supported in add, flash, and Flash All workflows. [Katapult](https://github.com/Arksine/katapult) bootloader must be flashed to the target MCU for CAN flashing.

**Prerequisites:**

- Katapult installed on host (`scripts/flashtool.py` must be available)
- CAN interface up with adequate queue length:

```bash
sudo ip link set can0 up type can bitrate 1000000
sudo ip link set can0 txqueuelen 1024
```

**Preflight checks:** kflash validates the CAN interface is UP and has `txqueuelen >= 128` before any CAN operation.

**Flash All ordering:** To prevent CAN bus loss from reflashing a bridge device before its downstream toolheads:

1. CAN toolheads first
2. USB devices and CAN devices with no assigned role
3. CAN bridges last

Device roles (`toolhead`, `bridge`) are set during device registration or via Config Device.

**CAN settings** (in Settings menu):

| Setting | Default | Description |
|---------|---------|-------------|
| CAN flash stagger delay | 5.0s | Pause between consecutive CAN flash operations |
| CAN bus scan on refresh | OFF | Scan CAN bus when refreshing devices (experimental — requires stopping Klipper) |

## Device Discovery

- **USB:** Scans `/dev/serial/by-id/` for devices matching `usb-Klipper_*` or `usb-katapult_*` (case-insensitive)
- **CAN:** Discovered via Moonraker `configfile` query or optional CAN bus scan
- **Blocked by default:** `usb-beacon_*` — Beacon probes use a separate update mechanism and are not Klipper MCUs
- **Duplicate detection:** If multiple registry entries resolve to the same physical USB device, duplicates are blocked from flash selection

## Settings

Accessible from the main menu with `C`. All settings persist in the device registry.

| Setting | Default | Description |
|---------|---------|-------------|
| Skip menuconfig | OFF | Skip menuconfig when a cached `.config` exists |
| Build acceleration (ccache) | OFF | Use ccache for faster rebuilds (prompts to install if missing) |
| Flash stagger delay | 2.0s | Pause between devices during Flash All |
| Menu return delay | 5.0s | Pause after flash output before returning to menu |
| CAN flash stagger delay | 5.0s | Pause between CAN flash operations |
| CAN bus scan on refresh | OFF | Scan CAN bus on device refresh (experimental) |
| Klipper directory | `~/klipper` | Path to Kalico/Klipper source tree |
| Katapult directory | `~/katapult` | Path to Katapult source tree |

## Data Paths

| File | Location |
|------|----------|
| Device registry | `${XDG_CONFIG_HOME:-~/.config}/kalico-flash/devices.json` |
| Per-device config cache | `${XDG_CONFIG_HOME:-~/.config}/kalico-flash/configs/<device-key>/.config` |

The registry path can be overridden with the `KALICO_REGISTRY_PATH` environment variable.

## Build Acceleration (ccache)

[ccache](https://ccache.dev/) dramatically speeds up rebuilds when flashing multiple devices or iterating on config changes.

- Controlled by the `Build acceleration (ccache)` setting
- The installer offers to install ccache during setup
- If enabled but missing at build time, kflash prompts to: install now, skip this build, or disable the setting
- Build output shows per-build cache hit/miss stats

Manual install: `sudo apt install -y ccache`

## Moonraker Update Manager

To receive kflash updates through Moonraker's update manager, add to `moonraker.conf`:

```ini
[update_manager kflash]
type: git_repo
path: ~/kflash
origin: https://github.com/YanceyA/kflash.git
primary_branch: main
is_system_service: False
```

Then restart Moonraker: `sudo systemctl restart moonraker`

## Update

```bash
cd ~/kflash
git pull
```

Changes take effect immediately (symlink-based install).

## Uninstall

```bash
cd ~/kflash
./install.sh --uninstall
```

Optional full cleanup:

```bash
rm -rf ~/kflash
rm -rf ~/.config/kalico-flash
```

## Troubleshooting

**Device not found after flash:** Wait a few seconds for USB re-enumeration. If the device does not reappear, check `ls /dev/serial/by-id/` manually. A power cycle of the MCU board may be needed.

**Permission denied on serial device:** Ensure your user is in the `dialout` group: `sudo usermod -aG dialout $USER` (log out and back in for it to take effect).

**CAN interface not found:** Verify your USB-to-CAN adapter is connected and the interface is up: `sudo ip link set can0 up type can bitrate 1000000`

**Build fails with missing toolchain:** Install the ARM cross-compiler: `sudo apt install gcc-arm-none-eabi`

**Sudo password prompts during flash:** kflash uses `sudo` to stop/start the Klipper service. For unattended use, configure passwordless sudo for `systemctl`.

## Known Working Hardware

Maintainer-tested configurations:

| Board | MCU | Transport | Katapult |
|-------|-----|-----------|----------|
| BTT Octopus Pro v1.1 | STM32H723 | USB | Yes |
| LDO Nitehawk 36 | RP2040 | USB | Yes |
| BTT HBB | RP2040 | USB | No |
| Blackpill STM32F411 | STM32F411 | USB | No |

kflash should work with any board appearing in `/dev/serial/by-id/` with a `Klipper_` or `katapult_` prefix, or any CAN device reachable via Katapult's `flashtool.py`.

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).
