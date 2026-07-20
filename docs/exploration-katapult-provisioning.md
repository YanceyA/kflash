# Exploration: Flashing Katapult to a new (unprovisioned) device

Status: **exploration / design notes** — not implemented. Back to the
[README](../README.md).

> Scenario from the request: a brand-new PCB with **no bootloader installed
> yet**. The user puts it into its chip ROM boot mode with a **manual BOOT +
> RESET button press on the board**, and kflash installs the
> [Katapult](https://github.com/Arksine/katapult) bootloader onto it. After
> that, the existing kflash flow takes over to build and flash Klipper/Kalico.

This document maps what that would take, what we can reuse, and the pros, cons,
and risks. It is deliberately concrete about which modules change so the effort
is estimable.

---

## 1. TL;DR

- **What it is:** a new *provisioning* (a.k.a. "day-zero" / "first flash") path
  that installs Katapult onto a bare MCU via its **built-in ROM bootloader**,
  as opposed to today's paths, which all assume the MCU **already runs
  Katapult or a factory bootloader**.
- **Feasibility:** moderate. The three hard pieces (build Katapult, target a
  raw ROM-bootloader device, write the bootloader binary) each map onto code we
  already have — but the "raw device" has no serial identity, which is an
  assumption baked into discovery, the registry, and MCU auto-detection.
- **Cleanest scope for a first pass:** the two boot modes the request's
  "manual button" assumption actually produces over USB —
  **STM32 USB-DFU** (`0483:df11`) and **RP2040/RP2350 BOOTSEL** (`2e8a:0003` /
  `2e8a:000f`). Defer UART/serial ROM bootloaders (they need wiring, not a
  button) and exotic MCUs.
- **Biggest reuse win:** the board-profile catalog already encodes the exact
  per-board facts you need to build Katapult correctly (MCU variant, crystal,
  comms interface, flash offset). The manual-bootloader "press the button" gate
  and the UF2 copier already exist.
- **Biggest risk:** no serial identity on a raw device means **no cross-check
  that the user picked the right board profile** before writing to flash.

---

## 2. How kflash works today (the relevant baseline)

kflash is built end-to-end around a device that **already enumerates as a
serial device**: `usb-Klipper_*` (running app) or `usb-katapult_*` (already in
the Katapult bootloader), or a CAN UUID.

| Stage | Module | What it assumes |
|-------|--------|-----------------|
| Discovery | `discovery.py` | Scans `/dev/serial/by-id/` for `usb-Klipper_*` / `usb-katapult_*` (`SUPPORTED_PREFIXES`, `scan_serial_devices`) |
| Registration | `commands/device_add.py` | MCU is auto-detected **from the serial filename** (`extract_mcu_from_serial`); a `DeviceEntry` must have a `serial_pattern` **or** a `canbus_uuid` (`validation.validate_transport_fields`) |
| Build | `build.py` `run_build` | Runs `make` in **`klipper_dir`**, looks for **`out/klipper.bin`** (or `.uf2`) |
| Bootloader entry | `bootloader.py` | Dispatch table `{usb, serial, manual, none, can}`; then polls `/dev/serial/by-id/` for the device to **re-enumerate with the same MCU+serial signature** |
| Flash | `flasher.py` `execute_flash` | Dispatch on `flash_command` `{katapult, katapult_can, make_flash, flash_sdcard, uf2_mount}` — all upload **Klipper** onto an **existing** bootloader/app |
| Verify | `discovery.py` `wait_for_device` | Success = the device reappears as `usb-Klipper_*` |

`katapult_dir` is used **only** to locate `scripts/flashtool.py` for *uploading
Klipper over an existing Katapult bootloader* — nothing today builds or installs
Katapult itself. (`grep` confirms: no `make` ever runs in `katapult_dir`, no
`out/katapult.*` is ever produced, no `dfu-util`/`stm32flash`/`picotool`
invocation exists.)

The board-profile catalog (`boards.py`, `board_configs/*.config`) already
carries the hardware facts we would need — e.g. the Octopus Pro H723 profile
records "128KiB Katapult bootloader offset (0x8020000), 25MHz crystal, USB on
PA11/PA12," and the Manta profile even documents "BTT ships a Katapult
bootloader flashed via DFU (dfu-util) at 0x8000000." That knowledge is
currently **descriptive** (notes + a Klipper Kconfig fragment); provisioning
would make part of it **executable**.

---

## 3. The core gap

Everything above keys off a **serial identity that a bare board does not
have**. When you hold BOOT + tap RESET on a new PCB, it comes up in the chip's
**ROM bootloader**, which presents as:

- **STM32 (with USB DFU support):** a USB DFU device `0483:df11` — **not** in
  `/dev/serial/by-id/`.
- **RP2040 / RP2350:** a USB **mass-storage** disk labelled `RPI-RP2`
  (`2e8a:0003` / `2e8a:000f`) — again not a serial device.

So the device is invisible to `scan_serial_devices()`, has no MCU string to
parse, can't be registered (no `serial_pattern`), and can't be Moonraker-cross-
checked. Interestingly the code already *knows this mode exists* — `flasher.py`
`check_katapult` explicitly recovers a device that "entered DFU/BOOTSEL," and
`flash_uf2` already copies a `.uf2` to the `RPI-RP2` mount — but there is no
first-class notion of an **unprovisioned / raw** device.

That's the whole feature in one sentence: **teach kflash to build, target, and
verify a device that has no serial identity yet, then hand it back to the
existing flow once it does.**

---

## 4. The ROM-bootloader landscape vs. the "manual button" assumption

The request assumes "manual button reset puts it in boot mode, consistent
across most devices." That's true, but *what mode you land in* is MCU-family
specific, and that determines the flashing tool:

| MCU family | Button action | Presents as | Flash tool | Katapult artifact |
|-----------|---------------|-------------|-----------|-------------------|
| STM32 F4/F7/H7/G0/G4 (USB DFU) | BOOT0 high + RESET | USB DFU `0483:df11` | `dfu-util` (or `make flash`) | `out/katapult.bin` @ `0x08000000` |
| RP2040 / RP2350 | hold BOOTSEL + power | USB mass storage `RPI-RP2` | UF2 copy / `rp2040_flash` | `out/katapult.uf2` |
| STM32 F103 & other no-USB-DFU parts | BOOT0 + RESET | **UART** ROM bootloader | `stm32flash` over a wired UART | `out/katapult.bin` |
| SAM / LPC / HC32 / … | varies (often SD-card) | varies | varies | varies |

**Takeaway:** the "button → USB device" assumption holds cleanly for **STM32
USB-DFU** and **RP2040/RP2350**, which conveniently are the two the codebase
already half-supports. The **UART** path (notably the very common SKR Mini E3 /
STM32F103) needs physical TX/RX/GND wiring to the host — that is *not* "just a
button," so it should be explicitly out of scope for v1. Everything else is
long-tail.

This maps well onto the existing `manual` bootloader method (`bootloader.py`
`_enter_manual`), which already prompts "physically put the device into
bootloader mode, then press Enter."

---

## 5. What would be required

Three technical pieces + orchestration + UI. File-level detail so effort is
estimable.

### Piece 1 — Build Katapult (not Klipper)

Katapult uses the **same Kconfig/Make build system** as Klipper (`make
menuconfig` → `make` → `out/katapult.bin` or `out/katapult.uf2`).

- `build.py` `run_build` is hardcoded to `klipper_dir` and to the
  `out/klipper.*` artifact names. Generalize it to take a **source dir** and an
  **artifact basename** (or add a thin `run_katapult_build`). Small change —
  `run_menuconfig` already takes the dir as a parameter, so it's reusable as-is
  pointed at `katapult_dir`.
- **Katapult needs its own `.config` cache**, separate from the device's
  Klipper `.config` (`ConfigManager` today namespaces the cache per device key
  against the Klipper tree). Add a parallel cache slot for the Katapult config.
- **Katapult's Kconfig is not identical to Klipper's.** It shares
  architecture/MCU/clock/comms symbols, but adds bootloader-specific ones
  (application start offset, "enter bootloader on double-tap of reset," status
  LED pin, comms selection) and **drops** the Klipper app-offset symbols. So
  today's `board_configs/*.config` fragments — which are *Klipper* fragments —
  cannot be reused verbatim. We'd either:
  - **(a)** ship a second, small set of **Katapult** fragments (a new
    `katapult_config_fragment` flag + `katapult_configs/<key>.config`), or
  - **(b)** rely on Katapult's own menuconfig defaults + a forced review
    (simpler, less to maintain, but the seeded default is less correct).

### Piece 2 — Detect / target a raw ROM-bootloader device

New discovery for devices that are **not** serial:

- **STM32 DFU:** scan for USB VID:PID `0483:df11` (via `lsusb`, or
  `/sys/bus/usb/devices/*/{idVendor,idProduct}`). Reading the DFU **descriptor**
  also yields the target/alt-setting, which is our only chance to sanity-check
  the chip (see Risks).
- **RP2040/RP2350:** detect the `RPI-RP2` mount — `flasher._find_uf2_mount`
  already does this — and/or the `2e8a:0003`/`2e8a:000f` VID:PID to tell RP2040
  from RP2350.
- Add a `RawDevice`/`DfuDevice` model (`models.py`) distinct from
  `DiscoveredDevice`. It has an MCU *family guess* at best, no serial pattern.

The "wait for the button press" step is the existing `manual` gate; what
changes is **what we poll for afterward** — a DFU/mass-storage device instead of
a serial re-enumeration.

### Piece 3 — Write the bootloader binary to the base address

- **STM32 USB DFU:** `dfu-util -a 0 -D out/katapult.bin -s 0x08000000:leave`.
  The lowest-effort, most-reusable option is to lean on **Katapult's own
  Makefile flash target** (`make flash FLASH_DEVICE=0483:df11` run in
  `katapult_dir`) so the arch-specific tool invocation stays upstream rather
  than reimplemented in kflash — this mirrors what `flasher.flash_make` already
  does for Klipper. *(Open item: confirm Katapult's per-arch `make flash`
  coverage; fall back to a direct `dfu-util` call where it's absent.)*
- **RP2040/RP2350:** copy `out/katapult.uf2` to the `RPI-RP2` mount — **reuse
  `flasher.flash_uf2` verbatim**, just pointed at the Katapult artifact — or
  `make flash FLASH_DEVICE=2e8a:0003`.
- Add a small dispatcher (`flash_bootloader`) parallel to `execute_flash`, or a
  new provisioning method table. This is a *separate axis* from the existing
  `bootloader_method`/`flash_command` pair (which describes how to flash the
  *app*), so it should not overload `validation.FLASH_METHOD_TABLE`.

### Orchestration — a new command

New `commands/provision.py` `cmd_provision_katapult(...)`, UI-free (Emitter +
DecisionProvider), mirroring `cmd_flash`'s structure:

1. **Select target** — no serial to auto-detect from, so the user **picks a
   board profile / MCU manually** (`choose_board_profile`, already exists). This
   is where the profile's offset/clock/comms facts drive the Katapult build.
2. **Build Katapult** — seed the Katapult `.config`, forced menuconfig review,
   `make` in `katapult_dir` (Piece 1).
3. **Enter boot mode** — reuse `_enter_manual`'s "hold BOOT, tap RESET, connect
   USB, press Enter" gate.
4. **Detect** the raw device (Piece 2).
5. **Flash** `katapult.bin`/`.uf2` to base (Piece 3).
6. **Verify** — poll `/dev/serial/by-id/` for the device to reappear as
   **`usb-katapult_*`** (reuse the existing polling in `bootloader.py` /
   `discovery.wait_for_device`, inverting the success prefix from `Klipper_` to
   `katapult_`).
7. **Hand off** — now the device *has* a serial identity, so offer to run the
   normal add-device wizard (`cmd_add_device(selected_device=...)`) and go
   straight into a Klipper flash.

**Design choice worth calling out:** do **not** create a registry entry for a
raw device. Register only *after* Katapult is installed and the device
enumerates as `usb-katapult_*`. That keeps the invariant "every `DeviceEntry`
has a serial_pattern or CAN UUID" intact and avoids threading a half-real
device through the whole registry/flash stack.

### UI

- New dashboard binding/action, e.g. **`K` — Install Katapult (new device)**,
  in `ui/screens/dashboard.py` (`BINDINGS` + `action_*`), following the exact
  pattern `action_add`/`action_flash` use: push a screen that runs the command
  on the `EngineBridge` worker thread and renders decisions as modals.
- Reuse the `operation.py` phase-checklist screen for the build → boot → flash →
  verify phases.
- Possibly a new "provisioning" screen or a mode flag on `AddDeviceScreen`.

### Docs & tests

- New `docs/provisioning.md`; cross-links from `flashing.md` and
  `board-profiles.md`.
- Tests: Katapult build (mocked `make`), DFU/RP2 detection (mocked sysfs/mount),
  the flash dispatch, and the raw→serial verify transition. Non-trivial new
  surface.

### Rough effort

| Area | Size |
|------|------|
| `build.py` generalization + Katapult config cache | S |
| Raw-device detection (DFU + RP2) in `discovery.py` + `models.py` | M |
| Bootloader-flash dispatch (`dfu-util` / reuse UF2 / `make flash`) | M |
| `cmd_provision_katapult` orchestration | M |
| Katapult board fragments (option **a**) | M (ongoing curation) |
| UI screen + binding | M |
| Docs + tests | M |

No single giant piece — the cost is in the **breadth** of touch points and the
**new "device without an identity" concept**, not algorithmic difficulty.

---

## 6. Recommended MVP

1. **USB only, two families:** STM32 USB-DFU (`0483:df11`) and RP2040/RP2350
   BOOTSEL. Explicitly refuse/omit UART and no-USB-DFU parts (e.g. F103) with a
   clear "wire it up and use the shell / not yet supported" message.
2. **Flash via Katapult's own `make flash`** where it exists (DFU + RP2),
   falling back to `dfu-util` / UF2 copy. Least new code, arch quirks stay
   upstream.
3. **Config seeding option (b)** to start (menuconfig defaults + forced review),
   then add Katapult fragments (option **a**) for the highest-traffic boards.
4. **Register-after-provision** hand-off, reusing `cmd_add_device`.
5. Gate on a preflight that verifies `katapult_dir` is a real Katapult tree and
   the flash tool (`dfu-util` / `make flash`) is available before touching
   hardware.

This delivers the headline value — **bare PCB → running Klipper MCU without
dropping to a shell** — while containing scope.

---

## 7. Pros

- **Closes the lifecycle gap.** Installing Katapult the first time (dfu-util /
  uf2 copy from the shell) is the single biggest manual step kflash doesn't
  cover today. This makes kflash a one-stop tool from unboxing to a running MCU.
- **High reuse.** Board profiles, `run_menuconfig`, the manual-bootloader gate,
  `flash_uf2`, serial re-enumeration polling, service management, the operation
  screen, and the add-device wizard are all reusable largely as-is.
- **Conceptually consistent** with the request's assumption — the existing
  `manual` bootloader method already models "user presses a button."
- **The catalog is already 80% of the data work.** Offsets, crystals, and comms
  interfaces are already curated per board; provisioning turns that from prose
  into build inputs.
- **RP2040/RP2350 is nearly free** and unbrickable — a low-risk beachhead to
  ship and validate the flow before tackling STM32 DFU.

## 8. Cons

- **Introduces a device with no identity**, which contradicts assumptions spread
  across discovery, the registry (`validate_transport_fields`), MCU
  auto-detection, and Moonraker cross-checks. Expect a lot of "serial_pattern is
  None / no MCU string yet" special-casing.
- **A second build target to maintain.** Katapult's Kconfig differs from
  Klipper's, so correct seeding means a *second* fragment set with its own
  upstream-drift maintenance — the same curation burden the Klipper fragments
  already carry, doubled for provisioned boards.
- **New external dependency** (`dfu-util`) for the STM32 path, plus udev/perms
  considerations the current serial-only flow avoids.
- **Weaker verification.** You can't Moonraker-verify a bootloader; the best
  success signal is "the device now enumerates as `usb-katapult_*`."
- **Scope-creep magnet** (UART provisioning, F103, HC32, read-protection
  removal, option-byte handling). Needs a firm line.

## 9. Risks & mitigations

| Risk | Severity | Mitigation |
|------|----------|-----------|
| **Wrong board profile → wrong-chip Katapult build flashed** (no serial identity to validate against before writing) | High | Read the DFU/USB **descriptor** and cross-check MCU family; for RP2040 vs RP2350 use the VID:PID; require an explicit "I confirm this is an `<MCU>`" step; refuse if the descriptor contradicts the picked profile |
| **STM32 brick / recoverability scare** — wrong offset, or overwriting a needed factory bootloader | Medium | Katapult flashes at `0x08000000` (base); document that BOOT0 re-entry recovers it; warn before overwriting; keep RP2040 (unbrickable) as the safe first target |
| **DFU enumeration flakiness** (dfu-util needs udev rules/dialout, sometimes root; `:leave` behavior varies; clone quirks) | Medium | Preflight the tool + permissions; clear recovery text; prefer Katapult's `make flash` which already handles these; provide a retry (the `manual` gate already retries once) |
| **Multiple raw devices attached at once** — two `0483:df11` devices are indistinguishable (no serial at all) | Medium | Detect >1 raw device and refuse ("provision one at a time"); this is stricter than today's duplicate-USB-ID block, which at least had serials |
| **UART-only / no-USB-DFU parts** (F103, etc.) — user expects "just a button" but the chip needs wiring | Medium | Detect the family and clearly state it's out of scope for the button-only flow; point to the shell/UART path |
| **Permissions** — `dfu-util` may want root while kflash runs as non-root with narrowly-scoped sudo | Low/Med | Prefer udev-rule-based access (document it); avoid broad sudo; fail loudly with the fix rather than escalating silently |
| **Curation drift** on Katapult fragments | Low | Start with menuconfig-defaults (option b), add fragments only for high-traffic boards, reuse the existing `fragment_drift` freshness machinery |

## 10. Open questions / decisions needed

1. **Config seeding:** ship Katapult Kconfig fragments (more correct, more
   maintenance) or lean on menuconfig defaults + forced review (simpler)?
2. **Flash mechanism:** commit to Katapult's `make flash`, or call `dfu-util` /
   copy UF2 directly from kflash? (Depends on Katapult's per-arch flash-target
   coverage — needs verifying against the Katapult tree.)
3. **Scope line for v1:** confirm UART/serial provisioning and no-USB-DFU STM32
   parts are out.
4. **UX placement:** a dedicated "Install Katapult" action vs. folding it into
   the add-device wizard when a raw device is detected.
5. **MCU cross-check strength:** how hard do we gate on the DFU descriptor
   matching the picked profile before we're willing to write flash?

---

*Companion reading: [Flashing](flashing.md) for the current flash-method table
and phases, [Board Profiles](board-profiles.md) for the catalog that already
encodes most of the per-board facts provisioning needs.*
