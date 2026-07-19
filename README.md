# kflash

Interactive TUI for building and flashing [Kalico](https://docs.kalico.gg) and Klipper firmware on Raspberry Pi and similar Linux SBCs. Replaces the manual `make menuconfig` / `make` / `make flash` cycle with device profiles, cached configs, a curated board-profile picker with config seeding, and a guided interactive flow — for both USB and CAN bus devices.

One pip dependency ([Textual](https://textual.textualize.io/), for the UI),
installed into a private venv by `install.sh`. The engine itself is stdlib-only.

## Requirements

- Python 3.9+
- Linux terminal session (SSH or local TTY)
- Run as a normal user (kflash exits if launched as root)
- `make` and `arm-none-eabi-gcc` (typically installed as part of Klipper setup)
- Kalico or Klipper source tree (default `~/klipper`)
- Katapult source tree for Katapult and CAN flash methods (default `~/katapult`)
- `sudo` for Klipper service stop/start during flash (scoped passwordless sudo recommended — see [Sudo Configuration](docs/configuration.md#sudo-configuration))
- When flashing over SSH, run inside `tmux` or `screen` — a dropped connection mid-flash aborts the operation (kflash restores the Klipper service on disconnect, but the flash itself is interrupted)
- Moonraker (recommended — enables print safety checks, firmware version display, and CAN device status)

## Install

```bash
git clone https://github.com/YanceyA/kflash.git ~/kflash
cd ~/kflash
./install.sh
kflash
```

The installer:

- Creates a virtualenv at `~/kflash/.venv` (override with `KFLASH_VENV=...`)
  and installs kflash there with its dependencies — the same pattern Klipper
  itself uses (`klippy-env`). Requires `python3-venv` on Pi OS / Debian.
- Creates a `kflash` symlink in `~/.local/bin` (adds to `PATH` if needed)
- Checks for prerequisites (`python3`, `arm-none-eabi-gcc`, `dialout` group, `sudo`)
- Optionally installs `ccache` for faster rebuilds
- Accepts `./install.sh --yes` for non-interactive install
- Editable install — `git pull` updates take effect immediately; re-run
  `./install.sh` after a pull that changes dependencies

Alternatively, install as a Python package into an environment of your choice
(provides the `kflash` command via a standard entry point):

```bash
git clone https://github.com/YanceyA/kflash.git ~/kflash
cd ~/kflash
pip install -e .
kflash
```

## Quick Start

1. Launch with `kflash`
2. Press **A** to add your first device — select a connected USB device or enter a CAN UUID, then pick a [board profile](docs/board-profiles.md) (or "other" for manual setup)
3. Press **F** to flash — kflash walks you through config, build, and flash. A newly added device starts from a [seeded `.config`](docs/configuration.md#config-seeding) and requires one menuconfig review before it flashes
4. On subsequent flashes, your `.config` is cached so the process is faster

That's it. kflash handles Klipper service management, bootloader entry, and device verification automatically.

### The UI

`kflash` launches a terminal UI built on
[Textual](https://textual.textualize.io/): a Status / Devices / Actions
dashboard with cursor-driven device selection (up/down or `j`/`k`,
`Enter`/`F` to flash the highlighted device), live device hotplug refresh,
and a details panel that follows the cursor. Flashes run on a dedicated
operation screen with a phase checklist, a live log of the flash tool's own
output, and a progress bar tracking real flash progress.

## Main Menu

| Key | Action | Description |
|-----|--------|-------------|
| `F` | Flash Device | Build and flash a single device (guided workflow) |
| `B` | Flash All | Batch-flash all connected devices using cached configs |
| `A` | Add Device | Register a new USB or CAN device (with board-profile picker) |
| `E` | Config Device | Edit device name, MCU, flash method, exclusion; save config as default; copy config from another device |
| `M` | Menuconfig | Edit a device's firmware config directly (no flash) |
| `D` | Refresh Devices | Re-scan USB bus for connected devices |
| `R` | Remove Device | Delete a device from the registry |
| `C` | Settings | Configure global options (ccache, paths, delays) |
| `Q` | Quit | Exit kflash |

## Flash Workflow

Single-device flash (`F`) runs four phases:

1. **Discovery** — scan USB or check CAN target, run Moonraker print-safety check
2. **Config** — seed the cache if empty, load cached `.config`, menuconfig review
3. **Build** — `make clean` + `make -j$(nproc)`, with build output captured
4. **Flash** — stop Klipper, enter bootloader, flash, verify device returns, restart Klipper

A board already sitting in the Katapult bootloader (fresh Katapult install,
no Klipper app yet) is flashed directly — bootloader entry is skipped, so
the first flash works end-to-end.

**Flash All** (`B`) builds and flashes all connected, flashable devices that
have a cached `.config`, in role-based order for CAN safety.

See [Flashing](docs/flashing.md) for phase details, safety checks, timeouts,
flash methods, and CAN bus support.

## Sudo

kflash uses `sudo` for exactly two operations: stopping and starting the
Klipper service around a flash. For unattended use, grant scoped passwordless
sudo for only those two commands — see
[Sudo Configuration](docs/configuration.md#sudo-configuration). Do not use
`NOPASSWD: ALL`.

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

## Documentation

- [Flashing](docs/flashing.md) — flash workflow details, safety checks, timeouts, flash methods, RP2040/RP2350 behavior, CAN bus support, device discovery
- [Configuration](docs/configuration.md) — config seeding, settings, data paths, ccache, sudo setup
- [Board Profiles](docs/board-profiles.md) — shipped profile catalog, community/user profiles, known working hardware
- [Troubleshooting](docs/troubleshooting.md) — common failures and fixes

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).
