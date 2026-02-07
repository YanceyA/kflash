"""Interactive TUI menu for kalico-flash.

Provides a panel-based main screen with single-keypress navigation and a
settings submenu when running without arguments.  Handles Unicode/ASCII
terminal detection, non-TTY fallback, invalid-input retry logic, and
error-resilient action dispatch.

Exports:
    run_menu: Main menu loop entry point.
    wait_for_device: Post-flash device verification with polling.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable

from .theme import clear_screen, get_theme

# ---------------------------------------------------------------------------
# Unicode / ASCII box-drawing detection
# ---------------------------------------------------------------------------

UNICODE_BOX: dict[str, str] = {
    "tl": "\u250c",  # top-left corner
    "tr": "\u2510",  # top-right corner
    "bl": "\u2514",  # bottom-left corner
    "br": "\u2518",  # bottom-right corner
    "h": "\u2500",  # horizontal line
    "v": "\u2502",  # vertical line
}

ASCII_BOX: dict[str, str] = {
    "tl": "+",
    "tr": "+",
    "bl": "+",
    "br": "+",
    "h": "-",
    "v": "|",
}


def _supports_unicode() -> bool:
    """Check if terminal supports Unicode box drawing."""
    lang = os.environ.get("LANG", "").upper()
    lc_all = os.environ.get("LC_ALL", "").upper()
    return "UTF-8" in lang or "UTF-8" in lc_all or "UTF8" in lang or "UTF8" in lc_all


def _get_box_chars() -> dict[str, str]:
    """Return the appropriate box-drawing character set for this terminal."""
    return UNICODE_BOX if _supports_unicode() else ASCII_BOX


# ---------------------------------------------------------------------------
# Menu rendering (kept for settings submenu)
# ---------------------------------------------------------------------------

MENU_OPTIONS: list[tuple[str, str]] = [
    ("1", "Add Device"),
    ("2", "List Devices"),
    ("3", "Flash Device"),
    ("4", "Remove Device"),
    ("5", "Settings"),
    ("0", "Exit"),
]


def _render_menu(options: list[tuple[str, str]], box: dict[str, str]) -> str:
    """Render a numbered menu with box-drawing characters.

    Used by the settings submenu. The main menu now uses panel rendering.
    """
    theme = get_theme()

    inner_items = [f" {num}) {label} " for num, label in options]
    inner_width = max(len(item) for item in inner_items)

    title_plain = "kalico-flash"
    title_width = len(title_plain) + 2
    inner_width = max(inner_width, title_width)

    title_display = f" {theme.menu_title}{title_plain}{theme.reset} "

    lines: list[str] = []

    pad_total = inner_width - title_width
    pad_left = pad_total // 2
    pad_right = pad_total - pad_left
    lines.append(box["tl"] + box["h"] * pad_left + title_display + box["h"] * pad_right + box["tr"])

    lines.append(box["v"] + box["h"] * inner_width + box["v"])

    for item in inner_items:
        padded = item.ljust(inner_width)
        lines.append(box["v"] + padded + box["v"])

    lines.append(box["bl"] + box["h"] * inner_width + box["br"])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Terminal cursor helpers
# ---------------------------------------------------------------------------

_CURSOR_HIDE = "\x1b[?25l"
_CURSOR_SHOW = "\x1b[?25h"


def _set_cursor_visible(visible: bool) -> None:
    """Best-effort cursor visibility toggle."""
    if not sys.stdout.isatty():
        return
    print(_CURSOR_SHOW if visible else _CURSOR_HIDE, end="", flush=True)


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def _consume_unix_escape_sequence(
    read_char: Callable[[], str],
    has_input: Callable[[float], bool],
) -> bool:
    """Consume a Unix ANSI escape sequence after leading ESC.

    Returns ``True`` when additional bytes were consumed (escape sequence),
    ``False`` when ESC was a standalone keypress.
    """
    if not has_input(0.005):
        return False

    first = read_char()

    # CSI/SS3 sequences (arrow keys, function keys, etc.)
    if first in ("[", "O"):
        while has_input(0.001):
            ch = read_char()
            if "@" <= ch <= "~":  # ANSI final byte
                break
        return True

    # Alt/meta key combination: consume any immediately available trailing bytes.
    while has_input(0):
        read_char()
    return True


def _getch() -> str:
    """Read a single keypress without requiring Enter.

    Returns lowercase character. Standalone ``Esc`` returns ``"\\x1b"``.
    Multi-byte escape sequences (arrow/function keys) are fully consumed and
    return ``""`` so they do not trigger phantom actions.
    """
    try:
        # Windows
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            # Extended key prefix (arrows/function keys) -- consume trailing byte.
            msvcrt.getwch()
            return ""
        return ch.lower()
    except ImportError:
        pass

    # Unix / Linux
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            is_sequence = _consume_unix_escape_sequence(
                read_char=lambda: sys.stdin.read(1),
                has_input=lambda timeout: bool(select.select([sys.stdin], [], [], timeout)[0]),
            )
            if is_sequence:
                return ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch.lower()


def _wait_for_key(timeout: float = 1.0) -> bool:
    """Wait for a keypress up to *timeout* seconds.

    Returns ``True`` if a key was pressed, ``False`` if the timeout expired.
    Cross-platform: uses ``msvcrt`` on Windows and ``select`` on Unix.
    """
    try:
        import msvcrt
        import time

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if msvcrt.kbhit():
                msvcrt.getwch()  # consume
                return True
            time.sleep(0.05)
        return False
    except ImportError:
        pass

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if ready:
            sys.stdin.read(1)  # consume
            return True
        return False
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _countdown_return(seconds: float) -> None:
    """Display a countdown timer; any keypress skips immediately.

    If *seconds* is zero or negative the function returns instantly.
    Uses ``_wait_for_key`` for cross-platform non-blocking detection.
    """
    if seconds <= 0:
        return

    theme = get_theme()
    remaining = int(seconds)
    while remaining > 0:
        print(
            f"\r  {theme.subtle}Returning to menu in {remaining}s... "
            f"(press any key){theme.reset}  ",
            end="",
            flush=True,
        )
        if _wait_for_key(timeout=1.0):
            break
        remaining -= 1
    # Clear the countdown line
    print("\r" + " " * 60 + "\r", end="", flush=True)


def _get_menu_choice(
    valid_choices: list[str],
    out,
    max_attempts: int = 3,
    prompt: str = "Select option: ",
) -> str | None:
    """Get a valid menu choice with retry logic.

    Prompts the user for input, validates against ``valid_choices``, and
    retries up to ``max_attempts`` times on invalid input.  Typing ``q``
    is normalised to ``"0"`` (exit / back).
    """
    for attempt in range(max_attempts):
        try:
            choice = input(prompt).strip().lower()
        except EOFError:
            return "0"

        if choice in ("q",):
            return "0"

        if choice in valid_choices:
            return choice

        remaining = max_attempts - attempt - 1
        if remaining > 0:
            out.warn(f"Invalid option '{choice}'. {remaining} attempts remaining.")
        else:
            out.warn(f"Invalid option '{choice}'. Too many invalid attempts.")

    return None


# ---------------------------------------------------------------------------
# Screen state building
# ---------------------------------------------------------------------------

_SCREEN_FETCH_CACHE_TTL_SECONDS = 5.0
_SCREEN_FETCH_CACHE: dict[tuple[str, str], tuple[float, object]] = {}


def _cached_screen_fetch(cache_key: tuple[str, str], fetch: Callable[[], object]) -> object:
    """Return a short-lived cached value for screen refresh fetches."""
    now = time.monotonic()
    cached = _SCREEN_FETCH_CACHE.get(cache_key)
    if cached is not None:
        cached_at, value = cached
        if now - cached_at < _SCREEN_FETCH_CACHE_TTL_SECONDS:
            return value
    value = fetch()
    _SCREEN_FETCH_CACHE[cache_key] = (now, value)
    return value


def _build_screen_state(registry, status_message: str, status_level: str, scan_can: bool = False):
    """Build a ScreenState by loading registry data and scanning USB devices.

    Args:
        registry: Registry instance.
        status_message: Current status bar message.
        status_level: Current status bar level.
        scan_can: When True, also scan CAN bus for unregistered devices.

    Returns (ScreenState, device_map) where device_map maps device number
    to DeviceRow for device-targeting actions.
    """
    from .discovery import scan_serial_devices
    from .flash import _build_blocked_list
    from .screen import ScreenState, build_device_list

    data = registry.load()
    usb_devices = scan_serial_devices()
    blocked_list = _build_blocked_list(data)

    # Fetch version info (best effort)
    mcu_versions = None
    host_version = None
    try:
        from .moonraker import get_host_klipper_version, get_mcu_versions

        mcu_versions = _cached_screen_fetch(("moonraker", "mcu_versions"), get_mcu_versions)
        if data.global_config:
            host_version = _cached_screen_fetch(
                ("moonraker", f"host_version:{data.global_config.klipper_dir}"),
                lambda: get_host_klipper_version(data.global_config.klipper_dir),
            )
    except Exception:
        pass

    # Fetch CAN device status from Moonraker (best effort)
    can_status_map = None
    try:
        from .moonraker import get_mcu_canbus_map

        can_status_map = _cached_screen_fetch(("moonraker", "canbus_map"), get_mcu_canbus_map)
    except Exception:
        pass

    devices = build_device_list(
        data, usb_devices, blocked_list, mcu_versions,
        can_status_map=can_status_map,
    )

    # Scan CAN bus for discovered-but-unregistered devices (best effort)
    if scan_can:
        try:
            from .discovery import get_can_interfaces, scan_can_devices
            from .screen import DeviceRow

            can_interfaces = get_can_interfaces()
            if can_interfaces and data.global_config:
                registered_uuids = {
                    e.canbus_uuid
                    for e in data.devices.values()
                    if e.canbus_uuid is not None
                }
                can_counter = max((d.number for d in devices), default=0)
                for iface in can_interfaces:
                    try:
                        found = scan_can_devices(iface, data.global_config.katapult_dir)
                        for can_dev in found:
                            if can_dev.uuid in registered_uuids:
                                continue
                            can_counter += 1
                            devices.append(
                                DeviceRow(
                                    number=can_counter,
                                    key=f"can:{can_dev.uuid}",
                                    name=f"CAN Device ({can_dev.uuid})",
                                    mcu="unknown",
                                    serial_path="",
                                    version=None,
                                    connected=True,
                                    group="new",
                                    flashable=True,
                                    is_can=True,
                                    canbus_uuid=can_dev.uuid,
                                    canbus_interface=iface,
                                )
                            )
                            registered_uuids.add(can_dev.uuid)  # Deduplicate across interfaces
                    except Exception:
                        pass
        except Exception:
            pass

    # Fetch service status (best effort)
    klipper_status = "unknown"
    moonraker_status = "unknown"
    try:
        from .service import get_service_status

        klipper_status = get_service_status("klipper")
        moonraker_status = get_service_status("moonraker")
    except Exception:
        pass

    state = ScreenState(
        devices=devices,
        host_version=host_version,
        status_message=status_message,
        status_level=status_level,
        klipper_status=klipper_status,
        moonraker_status=moonraker_status,
    )

    # Build device_map: number -> DeviceRow (only numbered devices)
    device_map: dict[int, object] = {}
    for row in devices:
        if row.number > 0:
            device_map[row.number] = row

    return state, device_map


# ---------------------------------------------------------------------------
# Device number prompting
# ---------------------------------------------------------------------------


def _prompt_device_number(device_map: dict, out) -> str | None:
    """Prompt the user for a device number and return the device key.

    If only one device exists, auto-selects it. Allows up to 3 attempts.
    Returns the device key string or None if cancelled/invalid.
    """
    if not device_map:
        out.warn("No devices available.")
        return None

    # Auto-select if only one device
    if len(device_map) == 1:
        row = next(iter(device_map.values()))
        return row.key

    for attempt in range(3):
        try:
            num_str = input("  Device #: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if not num_str or num_str.lower() in ("q", "0"):
            return None

        try:
            num = int(num_str)
        except ValueError:
            remaining = 2 - attempt
            if remaining > 0:
                out.warn(f"Invalid number '{num_str}'. {remaining} attempts remaining.")
            continue

        if num in device_map:
            return device_map[num].key

        remaining = 2 - attempt
        if remaining > 0:
            out.warn(f"No device #{num}. {remaining} attempts remaining.")

    return None


# ---------------------------------------------------------------------------
# Action handlers (return status message tuples)
# ---------------------------------------------------------------------------


def _action_flash_device(registry, out, device_key: str) -> tuple[str, str]:
    """Flash a specific device. Returns (message, level)."""
    from .flash import cmd_flash

    try:
        skip = registry.load().global_config.skip_menuconfig
        result = cmd_flash(registry, device_key, out, skip_menuconfig=skip)
        if result == 0:
            entry = registry.get(device_key)
            name = entry.name if entry else device_key
            return (f"Flash: {name} flashed successfully", "success")
        else:
            entry = registry.get(device_key)
            name = entry.name if entry else device_key
            return (f"Flash: failed for {name}", "error")
    except KeyboardInterrupt:
        return ("Flash: cancelled", "warning")
    except Exception as exc:
        return (f"Flash: {exc}", "error")


def _prompt_new_device_number(device_map: dict, out):
    """Prompt for a device number, filtered to only new (unregistered) devices.

    Returns (key, DeviceRow) or (None, None) if cancelled or no new devices.
    """
    # Filter to new devices only
    new_map = {num: row for num, row in device_map.items() if row.group == "new"}

    if not new_map:
        out.warn("No new devices to add.")
        return (None, None)

    # Auto-select if only one new device
    if len(new_map) == 1:
        num, row = next(iter(new_map.items()))
        return (row.key, row)

    for attempt in range(3):
        try:
            num_str = input("  Device #: ").strip()
        except (EOFError, KeyboardInterrupt):
            return (None, None)

        if not num_str or num_str.lower() in ("q", "0"):
            return (None, None)

        try:
            num = int(num_str)
        except ValueError:
            remaining = 2 - attempt
            if remaining > 0:
                out.warn(f"Invalid number '{num_str}'. {remaining} attempts remaining.")
            continue

        if num in new_map:
            return (new_map[num].key, new_map[num])

        remaining = 2 - attempt
        if remaining > 0:
            out.warn(f"No new device #{num}. {remaining} attempts remaining.")

    return (None, None)


def _action_add_device(registry, out, device_row=None, can_only=False) -> tuple[str, str]:
    """Launch the add-device wizard. Returns (message, level).

    Args:
        registry: Registry instance.
        out: Output interface.
        device_row: Optional DeviceRow from TUI prompt. When provided,
            finds the matching DiscoveredDevice and passes it to
            cmd_add_device to skip discovery output. CAN DeviceRows
            pass can_uuid/can_interface directly.
        can_only: When True, skip USB discovery and go straight to CAN
            registration wizard.
    """
    from .flash import cmd_add_device

    try:
        if device_row is not None and getattr(device_row, "is_can", False):
            # CAN device from discovery -- pass CAN info directly
            result = cmd_add_device(
                registry,
                out,
                can_uuid=device_row.canbus_uuid,
                can_interface=device_row.canbus_interface,
            )
        elif device_row is not None:
            # USB device from TUI (existing path)
            from .discovery import scan_serial_devices

            usb_devices = scan_serial_devices()
            matched_device = None
            for dev in usb_devices:
                if dev.filename == device_row.serial_path:
                    matched_device = dev
                    break
            if matched_device is None:
                return ("Add device: device no longer connected", "error")
            result = cmd_add_device(registry, out, selected_device=matched_device)
        else:
            result = cmd_add_device(registry, out, can_only=can_only)
        if result == 0:
            return ("Device added successfully", "success")
        else:
            return ("Add device: cancelled or failed", "warning")
    except KeyboardInterrupt:
        return ("Add device: cancelled", "warning")
    except Exception as exc:
        return (f"Add device: {exc}", "error")


def _action_remove_device(registry, out, device_key: str) -> tuple[str, str]:
    """Remove a specific device. Returns (message, level)."""
    from .flash import cmd_remove_device

    try:
        entry = registry.get(device_key)
        name = entry.name if entry else device_key
        result = cmd_remove_device(registry, device_key, out)
        if result == 0:
            return (f"Removed device '{name}'", "success")
        else:
            return (f"Remove: cancelled or failed for {name}", "warning")
    except KeyboardInterrupt:
        return ("Remove: cancelled", "warning")
    except Exception as exc:
        return (f"Remove: {exc}", "error")


# ---------------------------------------------------------------------------
# Main menu loop (panel-based)
# ---------------------------------------------------------------------------


def run_menu(registry, out) -> int:
    """Main interactive menu loop with panel-based screen.

    Displays a panel-based main screen with Status, Devices, and Actions
    panels. Single keypress selects actions. Device-targeting actions
    prompt for device number. Screen refreshes after every command.

    Returns 0 on normal exit.
    """
    # Non-TTY guard
    if not sys.stdin.isatty():
        print("kalico-flash: interactive menu requires a terminal.")
        print("Run 'kflash' to launch the interactive menu.")
        return 0

    from .panels import render_action_divider
    from .screen import render_main_screen

    theme = get_theme()
    status_message = "Welcome to kalico-flash. Select an action below."
    status_level = "info"
    first_render = True
    gc = registry.load().global_config
    scan_can_next = bool(gc.can_scan_on_refresh) if gc is not None else False

    try:
        while True:
            try:
                # Build screen state (scans USB, loads registry; optionally scans CAN)
                state, device_map = _build_screen_state(
                    registry, status_message, status_level, scan_can=scan_can_next
                )
                if scan_can_next:
                    status_message = "Devices refreshed (CAN bus scanned)"
                    status_level = "info"
                    state.status_message = status_message
                    state.status_level = status_level
                scan_can_next = False

                # Render and display
                _set_cursor_visible(False)
                clear_screen()
                if not first_render:
                    print()
                    print(render_action_divider())
                first_render = False
                print()
                print(render_main_screen(state))
                print()
                print(f"  {theme.prompt}Press action key:{theme.reset} ", end="", flush=True)
                _set_cursor_visible(True)

                # Read single keypress
                try:
                    key = _getch()
                except (EOFError, OSError):
                    return 0

                # Handle Ctrl+C (comes as \x03 in raw mode)
                if key == "\x03":
                    print()
                    return 0

                # Dispatch
                if key == "q":
                    return 0

                elif key == "f":
                    print(key)
                    print()
                    device_key = _prompt_device_number(device_map, out)
                    if device_key:
                        status_message, status_level = _action_flash_device(
                            registry, out, device_key,
                        )
                        print()
                        _countdown_return(registry.load().global_config.return_delay)
                    else:
                        status_message = "Flash: no device selected"
                        status_level = "warning"

                elif key == "a":
                    print(key)
                    print()
                    has_new_rows = any(row.group == "new" for row in device_map.values())
                    if not has_new_rows:
                        try:
                            prompt_msg = (
                                f"  {theme.prompt}No new USB devices found. "
                                f"Register a CAN device now? [y/N]:{theme.reset} "
                            )
                            answer = input(prompt_msg).strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            answer = "n"

                        if answer in ("y", "yes"):
                            status_message, status_level = _action_add_device(
                                registry, out, can_only=True
                            )
                            print()
                            _countdown_return(registry.load().global_config.return_delay)
                        else:
                            status_message = "Add: cancelled"
                            status_level = "info"
                    else:
                        device_key, device_row = _prompt_new_device_number(device_map, out)
                        if device_row:
                            status_message, status_level = _action_add_device(
                                registry, out, device_row
                            )
                            print()
                            _countdown_return(registry.load().global_config.return_delay)
                        else:
                            status_message = "Add: no device selected"
                            status_level = "warning"

                elif key == "r":
                    print(key)
                    print()
                    device_key = _prompt_device_number(device_map, out)
                    if device_key:
                        status_message, status_level = _action_remove_device(
                            registry, out, device_key,
                        )
                        print()
                        _countdown_return(registry.load().global_config.return_delay)
                    else:
                        status_message = "Remove: no device selected"
                        status_level = "warning"

                elif key == "e":
                    print(key)
                    print()
                    if not device_map:
                        status_message = "No devices registered. Use Add Device first."
                        status_level = "warning"
                    else:
                        device_key = _prompt_device_number(device_map, out)
                        if device_key:
                            # Check if selected device is unregistered
                            selected_row = next(
                                (r for r in device_map.values() if r.key == device_key), None
                            )
                            if selected_row and selected_row.group == "new":
                                try:
                                    prompt_msg = (
                                        f"  {theme.prompt}Device not registered. "
                                        f"Add it now? (y/n):{theme.reset} "
                                    )
                                    answer = input(prompt_msg).strip().lower()
                                except (EOFError, KeyboardInterrupt):
                                    answer = "n"
                                if answer in ("y", "yes"):
                                    status_message, status_level = _action_add_device(
                                        registry, out, device_row=selected_row
                                    )
                                else:
                                    status_message = "Config: cancelled"
                                    status_level = "info"
                            else:
                                _device_config_screen(device_key, registry, out)
                                print()
                                status_message = "Returned from device config"
                                status_level = "info"
                                _countdown_return(registry.load().global_config.return_delay)
                        else:
                            status_message = "Config: no device selected"
                            status_level = "warning"

                elif key == "d":
                    print(key)
                    gc = registry.load().global_config
                    scan_can_next = bool(gc.can_scan_on_refresh) if gc is not None else False
                    status_message = (
                        "Scanning devices and CAN bus..."
                        if scan_can_next
                        else "Scanning devices..."
                    )
                    status_level = "info"

                elif key == "c":
                    print(key)
                    _config_screen(registry, out)
                    status_message = "Returned from settings"
                    status_level = "info"

                elif key == "b":
                    print(key)
                    print()
                    connected_flashable = sum(
                        1
                        for row in state.devices
                        if row.group == "registered" and row.connected and row.flashable
                    )
                    try:
                        answer = input(
                            f"  {theme.prompt}Flash all {connected_flashable} connected device(s)? "
                            f"[y/N]:{theme.reset} "
                        ).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        answer = "n"

                    if answer not in ("y", "yes"):
                        status_message = "Flash All: cancelled"
                        status_level = "info"
                        continue

                    from .flash import cmd_flash_all

                    result = cmd_flash_all(registry, out)
                    if result == 0:
                        status_message = "Flash All: completed successfully"
                        status_level = "success"
                    else:
                        status_message = "Flash All: completed with errors"
                        status_level = "error"
                    print()
                    _countdown_return(registry.load().global_config.return_delay)

                else:
                    # Echo the key and show warning
                    if key.isprintable():
                        print(key)
                        status_message = f"Unknown key '{key}'. Use F/B/A/E/R/C/Q."
                    else:
                        print()
                        status_message = "Unknown key. Use F/B/A/E/R/C/Q."
                    status_level = "warning"

            except KeyboardInterrupt:
                print()
                return 0
    finally:
        _set_cursor_visible(True)


# ---------------------------------------------------------------------------
# Settings submenu (unchanged)
# ---------------------------------------------------------------------------


def _config_screen(registry, out) -> None:
    """Config screen with panel-based settings display and inline editing.

    Renders a status panel with instructions and a settings panel with 6
    numbered rows. Single keypress selects a setting to edit. Toggle settings
    flip immediately; numeric and path settings prompt for typed input.
    """
    import dataclasses

    from .panels import render_action_divider
    from .screen import SETTINGS, render_config_screen

    theme = get_theme()

    try:
        while True:
            data = registry.load()
            gc = data.global_config

            _set_cursor_visible(False)
            clear_screen()
            print()
            print(render_action_divider())
            print()
            print(render_config_screen(gc))
            print()
            print(
                f"  {theme.prompt}Setting # (or Esc/B to return):{theme.reset} ",
                end="",
                flush=True,
            )
            _set_cursor_visible(True)

            try:
                key = _getch()
            except (EOFError, OSError):
                return

            # Ctrl+C
            if key == "\x03":
                return

            # Escape or B to return
            if key == "\x1b" or key == "b":
                return

            # Check for valid setting number (8 settings: 1-8)
            if key in ("1", "2", "3", "4", "5", "6", "7", "8"):
                idx = int(key) - 1
                setting = SETTINGS[idx]
                field_key = setting["key"]
                current = getattr(gc, field_key)

                if setting["type"] == "toggle":
                    next_value = not current
                    if field_key == "can_scan_on_refresh" and next_value:
                        print(key)
                        print(
                            f"  {theme.warning}[Experimental]{theme.reset} "
                            "Enabling CAN bus scan on refresh will stop Klipper for device checks "
                            "and then restart it."
                        )
                        print(
                            f"  {theme.warning}This can be unstable on some systems.{theme.reset}"
                        )
                        try:
                            answer = input(
                                f"  {theme.prompt}Enable this setting? (y/n):{theme.reset} "
                            ).strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            answer = "n"
                        if answer not in ("y", "yes"):
                            continue

                    # Flip immediately
                    new_gc = dataclasses.replace(gc, **{field_key: next_value})
                    registry.save_global(new_gc)
                    success_color = getattr(theme, "success", "")
                    print(f"  {success_color}Saved {setting['label']}{theme.reset}")

                elif setting["type"] == "numeric":
                    from .validation import validate_numeric_setting

                    print(key)
                    while True:
                        try:
                            raw = input(f"  {setting['label']} [{current}]: ").strip()
                        except (EOFError, KeyboardInterrupt):
                            raw = ""
                            break
                        if not raw:
                            break
                        ok, val, err = validate_numeric_setting(raw, setting["min"], setting["max"])
                        if ok:
                            new_gc = dataclasses.replace(gc, **{field_key: val})
                            registry.save_global(new_gc)
                            success_color = getattr(theme, "success", "")
                            print(f"  {success_color}Saved {setting['label']}{theme.reset}")
                            break
                        print(f"  {theme.error}{err}{theme.reset}")
                    continue

                elif setting["type"] == "path":
                    from .validation import validate_path_setting

                    print(key)
                    while True:
                        try:
                            raw = input(f"  {setting['label']} [{current}]: ").strip()
                        except (EOFError, KeyboardInterrupt):
                            raw = ""
                            break
                        if not raw:
                            break
                        ok, err = validate_path_setting(raw, field_key)
                        if ok:
                            new_gc = dataclasses.replace(gc, **{field_key: raw})
                            registry.save_global(new_gc)
                            success_color = getattr(theme, "success", "")
                            print(f"  {success_color}Saved {setting['label']}{theme.reset}")
                            break
                        print(f"  {theme.error}{err}{theme.reset}")
                    continue
    finally:
        _set_cursor_visible(True)


# ---------------------------------------------------------------------------
# Device config screen
# ---------------------------------------------------------------------------


def _flash_method_picker_overlay(
    current_bootloader: str | None,
    current_flash_command: str | None,
    device_name: str | None = None,
    mcu: str | None = None,
    is_can_device: bool = False,
) -> tuple[str, str | None] | None:
    """Full-screen flash method picker overlay.

    Clears screen, renders a numbered flash method table with the current
    selection highlighted. Single keypress (1-9) selects a pair instantly.
    Esc/Ctrl+C cancels without change.

    When *mcu* is provided, the table is filtered to exclude methods
    incompatible with that MCU (e.g., serial re-enumeration methods for
    RP2040/RP2350 boards).

    Args:
        current_bootloader: Current bootloader_method value.
        current_flash_command: Current flash_command value.
        device_name: Optional device name for header context (e.g., "Octopus Pro").
        mcu: Optional MCU type for filtering incompatible methods.
        is_can_device: Whether the target device uses CAN transport.

    Returns:
        Tuple of (bootloader_method, flash_command) if a selection was made,
        or None if cancelled.
    """
    from .panels import render_table_picker
    from .validation import filter_flash_methods_for_device

    theme = get_theme()
    filtered_table = filter_flash_methods_for_device(
        mcu=mcu,
        is_can_device=is_can_device,
    )

    # Find current selection index
    selected_index: int | None = None
    for i, pair in enumerate(filtered_table):
        if (
            pair.bootloader_method == current_bootloader
            and pair.flash_command == current_flash_command
        ):
            selected_index = i
            break

    _set_cursor_visible(False)
    clear_screen()
    print()
    if device_name:
        header = f"Select flash method for {device_name} (1-{len(filtered_table)}, Esc to cancel):"
    else:
        header = f"Select flash method (1-{len(filtered_table)}, Esc to cancel):"
    print(f"  {theme.prompt}{header}{theme.reset}")
    print()
    for line in render_table_picker(filtered_table, selected_index=selected_index):
        print(f"  {line}")
    print()
    print(f"  {theme.prompt}Selection: {theme.reset}", end="", flush=True)
    _set_cursor_visible(True)

    while True:
        try:
            key = _getch()
        except (EOFError, OSError):
            return None

        if key in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
            pair_index = int(key) - 1
            if pair_index < len(filtered_table):
                pair = filtered_table[pair_index]
                return (pair.bootloader_method, pair.flash_command)
            # Out of range -- silently ignore
            continue

        if key == "\x1b" or key == "\x03":
            return None

        # Any other key: silently ignore
        continue


def _save_device_edits(original_key: str, pending: dict, registry) -> None:
    """Save accumulated device edits atomically.

    Applies pending field updates via ``Registry.update_device``.
    """
    if not pending:
        return

    registry.update_device(original_key, **pending)


def _device_config_screen(device_key: str, registry, out) -> None:
    """Device config screen with collect-then-save editing.

    Renders device identity (read-only) and numbered settings (1-10).
    Single keypress selects a setting to edit. Changes accumulate in a
    pending dict and are saved atomically on Esc exit. Ctrl+C discards.

    Field 3 (flash method) opens a full-screen picker overlay.
    Greyed-out CAN-only fields are silently ignored when inapplicable.
    Key "0" selects field 10 (notes). M key opens menuconfig with MCU
    mismatch validation loop.
    """
    import dataclasses

    from .panels import render_action_divider
    from .screen import (
        DEVICE_SETTINGS_FIELDS,
        _is_sub_field_applicable,
        render_device_config_screen,
    )

    theme = get_theme()
    original_key = device_key
    original = registry.get(device_key)
    if original is None:
        return
    pending: dict[str, object] = {}

    try:
        while True:
            # Build working copy with pending overlaid
            working = dataclasses.replace(original, **pending)

            _set_cursor_visible(False)
            clear_screen()
            print()
            print(render_action_divider())
            print()
            print(render_device_config_screen(working))
            print()
            print(
                f"  {theme.prompt}Setting #, N, or M (Esc to save & return):{theme.reset} ",
                end="",
                flush=True,
            )
            _set_cursor_visible(True)

            try:
                key = _getch()
            except (EOFError, OSError):
                _save_device_edits(original_key, pending, registry)
                return

            # Ctrl+C - discard pending
            if key == "\x03":
                return

            # Esc - save and return
            if key == "\x1b":
                _save_device_edits(original_key, pending, registry)
                return

            # --- Numbered fields 1-9, 0 for field 10, N for field 11 (notes) ---
            if key in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "n"):
                if key == "0":
                    idx = 9
                elif key == "n":
                    idx = 10
                else:
                    idx = int(key) - 1
                if idx >= len(DEVICE_SETTINGS_FIELDS):
                    continue
                setting = DEVICE_SETTINGS_FIELDS[idx]

                # Key 1: Display name (text edit)
                if setting["key"] == "name":
                    print(key)
                    try:
                        raw = input(f"  {setting['label']} [{working.name}]: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        continue
                    if raw:
                        existing_names = {
                            entry.name.casefold()
                            for entry in registry.load().devices.values()
                            if entry.key != original_key
                        }
                        if raw.casefold() in existing_names:
                            print(
                                f"  {theme.error}A different device already"
                                f" uses that name{theme.reset}"
                            )
                            continue
                        pending["name"] = raw

                # Key 2: MCU name picker with Moonraker query
                elif setting["key"] == "mcu_name":
                    print(key)
                    try:
                        from .moonraker import get_mcu_serial_map

                        mcu_serials = get_mcu_serial_map()
                        if mcu_serials is not None:
                            mcu_with_serial = {
                                k: v for k, v in mcu_serials.items()
                                if v is not None
                            }
                            if mcu_with_serial:
                                print(f"  {theme.text}Select Klipper MCU name:{theme.reset}")
                                names = list(mcu_with_serial.keys())
                                for i, name in enumerate(names, 1):
                                    print(f"    {i}. {name}")
                                print("    0. Enter manually")
                                print("    (blank). Clear MCU name")
                                raw = input("  MCU name selection: ").strip()
                                if not raw:
                                    pending["mcu_name"] = None
                                elif raw == "0":
                                    manual = input("  Enter MCU name: ").strip()
                                    pending["mcu_name"] = manual if manual else None
                                else:
                                    try:
                                        sel_idx = int(raw) - 1
                                        if 0 <= sel_idx < len(names):
                                            pending["mcu_name"] = names[sel_idx]
                                    except ValueError:
                                        print(f"  {theme.warning}Invalid selection{theme.reset}")
                            else:
                                print(
                                    f"  {theme.subtle}No MCUs with serial paths"
                                    f" in config{theme.reset}"
                                )
                                raw = input("  Enter MCU name (blank to clear): ").strip()
                                pending["mcu_name"] = raw if raw else None
                        else:
                            print(f"  {theme.warning}Moonraker unreachable{theme.reset}")
                            raw = input("  Enter MCU name (blank to clear): ").strip()
                            pending["mcu_name"] = raw if raw else None
                    except (EOFError, KeyboardInterrupt):
                        continue

                # Key 3: Flash method pair picker overlay
                elif setting["key"] == "flash_method_pair":
                    result = _flash_method_picker_overlay(
                        working.bootloader_method,
                        working.flash_command,
                        mcu=working.mcu,
                        is_can_device=working.is_can_device,
                    )
                    if result is not None:
                        pending["bootloader_method"] = result[0]
                        pending["flash_command"] = result[1]
                    continue

                # Key 4: Flashable toggle
                elif setting["key"] == "flashable":
                    pending["flashable"] = not working.flashable

                # Key 5: CAN device role picker (toolhead/bridge/none)
                elif setting["key"] == "role":
                    if not working.is_can_device:
                        continue
                    print(key)
                    try:
                        print(f"  {theme.text}Select CAN device role:{theme.reset}")
                        role_options = ["(none)", "toolhead", "bridge"]
                        for i, opt in enumerate(role_options, 1):
                            marker = (
                                " *"
                                if (
                                    (opt == "(none)" and working.role is None)
                                    or opt == working.role
                                )
                                else ""
                            )
                            print(f"    {i}. {opt}{marker}")
                        raw = input("  Role selection: ").strip()
                        if raw == "1":
                            pending["role"] = None
                        elif raw == "2":
                            pending["role"] = "toolhead"
                        elif raw == "3":
                            pending["role"] = "bridge"
                        # else: invalid or blank, no change
                    except (EOFError, KeyboardInterrupt):
                        pass
                    continue

                # Keys 6-10: Conditional sub-fields (silently ignored when inapplicable)
                elif setting["key"] == "canbus_uuid":
                    if not _is_sub_field_applicable(working, "canbus_uuid"):
                        continue
                    print(key)
                    try:
                        raw = input(f"  CAN bus UUID [{working.canbus_uuid or ''}]: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        continue
                    if raw:
                        from .validation import validate_canbus_uuid

                        normalized = raw.lower()
                        ok, err = validate_canbus_uuid(normalized)
                        if ok:
                            pending["canbus_uuid"] = normalized
                        else:
                            print(f"  {theme.error}{err}{theme.reset}")

                elif setting["key"] == "canbus_interface":
                    if not _is_sub_field_applicable(working, "canbus_interface"):
                        continue
                    print(key)
                    try:
                        from .discovery import get_can_interfaces

                        interfaces = get_can_interfaces()
                        if interfaces:
                            print(f"  Available: {', '.join(interfaces)}")
                        raw = input(
                            f"  CAN interface [{working.canbus_interface or 'can0'}]: "
                        ).strip()
                    except (EOFError, KeyboardInterrupt):
                        continue
                    if raw:
                        from .validation import validate_can_interface

                        ok, err = validate_can_interface(raw)
                        if ok:
                            pending["canbus_interface"] = raw
                        else:
                            print(f"  {theme.error}{err}{theme.reset}")

                elif setting["key"] == "bootloader_baud":
                    if not _is_sub_field_applicable(working, "bootloader_baud"):
                        continue
                    print(key)
                    try:
                        default = working.bootloader_baud or 250000
                        raw = input(f"  Bootloader baud rate [{default}]: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        continue
                    if raw:
                        from .validation import validate_bootloader_baud

                        try:
                            baud = int(raw)
                            ok, err = validate_bootloader_baud(baud)
                            if ok:
                                pending["bootloader_baud"] = baud
                            else:
                                print(f"  {theme.error}{err}{theme.reset}")
                        except ValueError:
                            print(f"  {theme.error}Invalid baud rate{theme.reset}")
                    else:
                        pending["bootloader_baud"] = default

                elif setting["key"] == "uf2_mount_path":
                    if not _is_sub_field_applicable(working, "uf2_mount_path"):
                        continue
                    print(key)
                    try:
                        raw = input(f"  UF2 mount path [{working.uf2_mount_path or ''}]: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        continue
                    if raw:
                        pending["uf2_mount_path"] = raw

                elif setting["key"] == "sdcard_board":
                    if not _is_sub_field_applicable(working, "sdcard_board"):
                        continue
                    print(key)
                    try:
                        default = working.sdcard_board or ""
                        raw = input(f"  SD card board name [{default}]: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        continue
                    if raw:
                        pending["sdcard_board"] = raw

                # Key N: Notes (single-line inline input, field 11)
                elif setting["key"] == "notes":
                    print(key)
                    try:
                        raw = input("  Notes: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        continue
                    pending["notes"] = raw if raw else None

            # --- M key: menuconfig ---
            elif key == "m":
                print(key)
                try:
                    from .build import run_menuconfig
                    from .config import ConfigManager

                    gc = registry.load_global()
                    cm = ConfigManager(original_key, gc.klipper_dir)
                    had_cache = cm.has_cached_config()
                    cm.load_cached_config()
                    config_path = str(cm.klipper_config_path)
                    ret_code, was_saved = run_menuconfig(gc.klipper_dir, config_path)
                    if was_saved:
                        # DON'T save to cache yet -- validate MCU first
                        try:
                            entry = registry.load().devices.get(original_key)
                            if entry:
                                is_match, actual_mcu = cm.validate_mcu(entry.mcu)
                                while not is_match:
                                    print(
                                        f"  {theme.warning}MCU mismatch: "
                                        f"config has '{actual_mcu}' but "
                                        f"device '{entry.name}' expects "
                                        f"'{entry.mcu}'{theme.reset}"
                                    )
                                    choice = (
                                        input(
                                            "  [R]e-open menuconfig / "
                                            "[D]iscard config / "
                                            "[K]eep anyway: "
                                        )
                                        .strip()
                                        .lower()
                                    )
                                    if choice not in ("r", "d", "k"):
                                        continue
                                    if choice == "r":
                                        ret2, saved2 = run_menuconfig(gc.klipper_dir, config_path)
                                        if saved2:
                                            is_match, actual_mcu = cm.validate_mcu(entry.mcu)
                                        else:
                                            print(
                                                f"  {theme.info}menuconfig exited "
                                                f"without saving{theme.reset}"
                                            )
                                            break
                                    elif choice == "d":
                                        if had_cache:
                                            cm.load_cached_config()
                                            print(
                                                f"  {theme.info}Restored previous "
                                                f"config{theme.reset}"
                                            )
                                        else:
                                            cm.clear_klipper_config()
                                            print(f"  {theme.info}Discarded config{theme.reset}")
                                        break
                                    else:  # 'k'
                                        cm.save_cached_config()
                                        print(
                                            f"  {theme.info}Keeping mismatched config{theme.reset}"
                                        )
                                        break
                                else:
                                    # MCU matched -- save now
                                    cm.save_cached_config()
                        except Exception:
                            pass
                except Exception as exc:
                    print(f"  {theme.error}{exc}{theme.reset}")

            # Any other key: silently ignore
    finally:
        _set_cursor_visible(True)


# ---------------------------------------------------------------------------
# Post-flash device verification
# ---------------------------------------------------------------------------


def wait_for_device(
    serial_pattern: str,
    timeout: float = 30.0,
    interval: float = 0.5,
    out=None,
) -> tuple[bool, str | None, str | None]:
    """Poll for device to reappear after flash.

    Prints progress dots every 2 seconds when ``out`` is None. Checks both
    device existence AND prefix (``Klipper_`` expected, ``katapult_`` means failure).

    After a successful flash, the MCU reboots from bootloader to Klipper mode.
    If we detect the device still in katapult mode, we continue polling to
    allow time for the reboot to complete.

    Returns:
        A 3-tuple ``(success, device_path, error_reason)``.
    """
    import fnmatch
    import time

    from .discovery import scan_serial_devices

    start = time.monotonic()
    last_dot_time = start
    last_katapult_path = None  # Track if we've seen katapult device

    if out is None:
        print("Verifying", end="", flush=True)

    while time.monotonic() - start < timeout:
        now = time.monotonic()
        if out is None and now - last_dot_time >= 2.0:
            print(".", end="", flush=True)
            last_dot_time = now

        devices = scan_serial_devices()
        found_katapult = False

        for device in devices:
            from .discovery import _prefix_variants

            variants = _prefix_variants(serial_pattern)
            if any(fnmatch.fnmatch(device.filename, v) for v in variants):
                filename_lower = device.filename.lower()
                if filename_lower.startswith("usb-klipper_"):
                    if out is None:
                        print()
                    return (True, device.path, None)
                elif filename_lower.startswith("usb-katapult_"):
                    # Device still in bootloader - continue polling to allow reboot
                    found_katapult = True
                    last_katapult_path = device.path
                else:
                    if out is None:
                        print()
                    return (
                        False,
                        device.path,
                        f"Unexpected device prefix: {device.filename}",
                    )

        # If we saw katapult but it's now gone, device is rebooting - keep polling
        if not found_katapult and last_katapult_path:
            last_katapult_path = None  # Reset, device disappeared

        time.sleep(interval)

    if out is None:
        print()

    # Timeout reached - provide helpful error based on what we observed
    if last_katapult_path:
        return (
            False,
            last_katapult_path,
            "Device in bootloader mode (katapult)",
        )
    return (False, None, f"Timeout after {int(timeout)}s waiting for device")
