# Configuration

Part of the kflash docs — back to the [README](../README.md).

## Config Seeding

New devices start from a sensible `.config` instead of a blank `menuconfig`.
When a device has **no cached `.config`**, the config step seeds one before
menuconfig, using the first available source:

1. **Board fragment** — the device's [board profile](board-profiles.md) fragment (if it ships one)
2. **`~/.config/kalico-flash/defaults/<mcu>.config`** — your saved default for that MCU
3. **`~/.config/kalico-flash/defaults/default.config`** — a catch-all default
4. **Fresh** — no seed available; menuconfig starts cleared

Seeding never overwrites an existing cache (except the explicit copy-config,
which you confirm).

### Forced review of seeded configs

An auto-seeded `.config` must pass through **one menuconfig review** before it
can build or flash. Until you review it, the dashboard row carries an orange
`[review]` tag. This gate holds even when **Menuconfig prompt before flash** is
OFF — a seeded config never reaches build/flash unreviewed. Saving the config
out of menuconfig clears the tag; declining leaves it in place.

After a seeded menuconfig round-trip, the config-diff receipt also runs a
**drift check**: if a newer Kalico tree no longer recognizes one of the
profile's settings (an upstream symbol rename), the receipt warns that *N
profile settings were not recognized* and names them, so a board fact can't
silently revert to a tree default.

### Saving and copying configs

From **Config Device** (`E`):

- **Save config as default** writes the device's current `.config` to
  `~/.config/kalico-flash/defaults/<mcu>.config`, so future devices with that
  MCU seed from it.
- **Copy config from another device** copies one device's cached `.config` onto
  another (confirmed, since it overwrites).

## Settings

Accessible from the main menu with `C`. All settings persist in the device registry.

| Setting | Default | Description |
|---------|---------|-------------|
| Menuconfig prompt before flash | ON | Offer menuconfig before each flash; when OFF, a cached `.config` flashes directly (a first flash still requires menuconfig) |
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
| MCU-default seed configs | `${XDG_CONFIG_HOME:-~/.config}/kalico-flash/defaults/<mcu>.config`, `defaults/default.config` |
| User/community board profiles | `${XDG_CONFIG_HOME:-~/.config}/kalico-flash/boards/<key>.json` (+ optional `<key>.config`) |

The registry path can be overridden with the `KALICO_REGISTRY_PATH` environment variable. All paths respect `XDG_CONFIG_HOME` (defaulting to `~/.config`).

## Build Acceleration (ccache)

[ccache](https://ccache.dev/) dramatically speeds up rebuilds when flashing multiple devices or iterating on config changes.

- Controlled by the `Build acceleration (ccache)` setting
- The installer offers to install ccache during setup
- If enabled but missing at build time, kflash prompts to: install now, skip this build, or disable the setting
- Build output shows per-build cache hit/miss stats

Manual install: `sudo apt install -y ccache`

## Sudo Configuration

kflash uses `sudo` for two operations: stopping and starting the Klipper service around a flash. For unattended use (no password prompts), grant passwordless sudo for **only those two commands**:

```bash
sudo visudo -f /etc/sudoers.d/kflash
```

```
yourusername ALL=(root) NOPASSWD: /usr/bin/systemctl stop klipper, /usr/bin/systemctl start klipper
```

(Use `command -v systemctl` if your distribution puts systemctl somewhere other than `/usr/bin`.)

**Do not use `NOPASSWD: ALL`**, and do not add `tee` to sudoers — passwordless `tee` can write any file on the system as root and is effectively full root access. The optional USB re-enumeration check may invoke `sudo tee` on a sysfs path; it is expected to prompt for a password (or be skipped) on a scoped setup — that is by design.

If you skip this, kflash prompts for your sudo password at the start of a flash and caches it for the run. If credentials expire during a long batch, kflash re-prompts before restarting Klipper rather than leaving it stopped.
