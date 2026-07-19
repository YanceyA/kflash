# Flashing

Part of the kflash docs — back to the [README](../README.md).

## Flash Workflow

**Scope:** kflash flashes boards that enumerate in `/dev/serial/by-id/` as `usb-Klipper_*` or `usb-katapult_*` USB devices, or as Katapult CAN nodes. A board already in the Katapult bootloader is flashed directly. Installing Katapult itself for the first time, or recovering a board with neither firmware (raw DFU, RP2 BOOTSEL), is a manual step outside kflash.

Single-device flash (`F`) runs four phases:

1. **Discovery** — Scan USB or check CAN target, validate transport, run Moonraker print-safety check
2. **Config** — [Seed the cache](configuration.md#config-seeding) if empty, load cached `.config`, launch `menuconfig` (always forced for a seeded config), validate MCU type matches registry
3. **Build** — `make clean` + `make -j$(nproc)` with 300s timeout; build output is captured (not dumped to the terminal), and on failure the tail of the log is shown inline
4. **Flash** — Stop Klipper, enter bootloader, flash firmware (output streams live into the log with a real-progress bar), verify device returns, restart Klipper

**Flash All** (`B`) builds and flashes all connected, flashable devices that have a cached `.config`. Devices are flashed in role-based order for CAN safety (see [CAN Bus Support](#can-bus-support)).

## Safety Checks

Before flashing, kflash queries Moonraker to check printer state:

- **Blocked states:** `printing`, `paused`, `startup` — flashing is prevented with a clear message
- **Error state:** prompts for confirmation (flashing may be needed to recover)
- **Moonraker unreachable:** prompts for explicit confirmation before proceeding
- **Klipper service:** if active before flash, restart is guaranteed on all exit paths (success, failure, Ctrl+C, SIGTERM, SSH disconnect/SIGHUP)
- **Version warnings:** flags when host Klipper has uncommitted changes (`-dirty`) or when MCU firmware would be a downgrade

## Timeouts

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
