"""Shared flash-flow logic used by the single and batch flash commands.

L3 engine module. Holds the pieces that were triplicated across ``cmd_flash``
and ``cmd_flash_all``:

* :func:`moonraker_safety_gate` -- the Moonraker print-state safety check.
* :func:`load_and_validate_config` -- load cached config / menuconfig / MCU check.
* :func:`run_flash_sequence` -- the bootloader -> flash -> verify hardware
  sequence for one device (runs strictly INSIDE the caller's
  ``klipper_service_stopped`` window).
* :func:`resolve_target_mcu_version` / :func:`emit_host_and_mcu_versions` --
  the shared version-resolution/display helpers (divergent prompt flows stay
  in the commands).
* :func:`resolve_ccache_usage` -- ccache availability + install flow.

Talks to the world only through :class:`~kflash.events.Emitter` (output) and
:class:`~kflash.decisions.DecisionProvider` (input). No UI import.
"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Optional

from . import runner
from .bootloader import enter_bootloader
from .build import run_menuconfig
from .ccache import is_ccache_available
from .config import ConfigManager
from .decisions import ChooseCcacheActionDecision, ConfirmDecision, DecisionProvider
from .discovery import verify_can_device_after_flash, wait_for_device
from .errors import ERROR_TEMPLATES, ConfigError
from .events import Emitter
from .flasher import TIMEOUT_CAN_FLASH, TIMEOUT_FLASH, execute_flash
from .models import DeviceEntry, GlobalConfig
from .moonraker import detect_firmware_flavor, get_print_status
from .safety import should_block_on_printer_state


def _short_path(path_value: Optional[str]) -> str:
    """Return filename-only for /dev/serial/by-id paths."""
    try:
        return Path(path_value).name  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return path_value or ""


# ---------------------------------------------------------------------------
# Moonraker safety gate
# ---------------------------------------------------------------------------


class SafetyGate(enum.Enum):
    PROCEED = 0
    CANCELLED = 1  # caller returns 0
    BLOCKED = 2  # caller returns 1


def moonraker_safety_gate(
    *, em: Emitter, decider: DecisionProvider, label: str
) -> SafetyGate:
    """Moonraker print-state safety check.

    ``label`` is the phase label for the "Cancelled" line -- ``"Flash"`` for
    the single command, ``"Flash All"`` for the batch. That is the only
    difference between the two copies today.
    """
    print_status = get_print_status()

    if print_status is None:
        em.warn("Moonraker unreachable - print status and version check unavailable")
        if not decider.confirm(
            ConfirmDecision(
                id="no_moonraker",
                message="Continue without safety checks?",
                default=False,
            )
        ):
            em.phase(label, "Cancelled")
            return SafetyGate.CANCELLED
        return SafetyGate.PROCEED

    state = (print_status.state or "").lower()
    if state == "error":
        em.warn("Printer is in 'error' state. Flashing may be needed to recover.")
        if not decider.confirm(
            ConfirmDecision(
                id="printer_error_state",
                message="Continue flashing despite printer error state?",
                default=False,
            )
        ):
            em.phase(label, "Cancelled")
            return SafetyGate.CANCELLED
        return SafetyGate.PROCEED

    if should_block_on_printer_state(state):
        if state in ("printing", "paused"):
            progress_pct = int((print_status.progress or 0.0) * 100)
            filename = print_status.filename or "unknown"
            detail = f"Print in progress: {filename} ({progress_pct}%)"
        else:
            detail = f"Printer is in '{print_status.state}' state"
        em.error_with_recovery(
            "Printer busy",
            detail,
            recovery=(
                "1. Wait for printer to reach 'ready' state\n"
                "2. Or cancel print in Fluidd/Mainsail dashboard\n"
                "3. Then re-run flash command"
            ),
        )
        return SafetyGate.BLOCKED

    em.phase("Safety", f"Printer state: {print_status.state} - OK to flash")
    return SafetyGate.PROCEED


# ---------------------------------------------------------------------------
# Config load + validate
# ---------------------------------------------------------------------------


@dataclass
class ConfigOutcome:
    ok: bool
    exit_code: int = 0  # 0 or 1 to bubble to caller
    ran_menuconfig: bool = False


def _warn_fragment_drift(
    em: Emitter, seed_fragment: list[str], config_mgr: ConfigManager
) -> None:
    """Emit an em.warn line for each board-fragment symbol dropped on load.

    A symbol present in the recorded fragment but absent from the just-saved
    config was silently dropped by an upstream Kconfig rename (kconfiglib drops
    unknown lines on load), so a board fact reverted to the tree's default.
    No-op for non-board seeds (``seed_fragment`` empty). Never raises: a read
    failure here must not abort an otherwise-successful config phase.
    """
    if not seed_fragment:
        return
    from .boards import fragment_drift  # late import: avoids boards<->config cycle

    try:
        final = config_mgr.cache_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    drift = fragment_drift(seed_fragment, final)
    if not drift:
        return
    noun = "setting" if len(drift) == 1 else "settings"
    em.warn(
        f"{len(drift)} profile {noun} not recognized by this Kalico version -- "
        "the build will use this tree's default instead. Verify the bootloader "
        "offset / clock settings in menuconfig before flashing:"
    )
    for symbol in drift:
        em.warn(f"  dropped: {symbol}")


def load_and_validate_config(
    *,
    entry: DeviceEntry,
    device_key: str,
    klipper_dir: str,
    em: Emitter,
    decider: DecisionProvider,
    skip_menuconfig: bool,
    require_menuconfig: bool,
) -> ConfigOutcome:
    """Load cached config (or start fresh), optionally run menuconfig, save the
    cache, and validate the MCU type.

    Emits ``Config`` phase lines identical to today's ``cmd_flash`` config
    phase. ``skip_menuconfig`` + a cached config validates only;
    ``require_menuconfig`` forces the menuconfig step (cmd_build semantics).
    """
    config_mgr = ConfigManager(device_key, klipper_dir)

    # Seed-load shape shared with ui/menuconfig._run_menuconfig_step and
    # device_add's post-add block: with no cache yet, prefer the board profile's
    # fragment (guarded by entry.board), then fall back to the MCU default; read
    # the seed label ONCE (save_cached_config clears the marker later), then load
    # the cache (or start fresh) with a single load_cached_config call.
    if not config_mgr.has_cached_config():
        if entry.board is not None:
            config_mgr.seed_from_board(entry.board)
        if not config_mgr.has_cached_config():
            config_mgr.seed_from_default(entry.mcu)

    seeded_from = config_mgr.seed_source()
    # Recorded board-fragment CONFIG_ lines (empty for defaults/device copies and
    # legacy markers): read now, before save_cached_config() clears the marker,
    # so the post-menuconfig drift check can flag any dropped by an upstream rename.
    seed_fragment = config_mgr.seed_fragment_lines()

    # step_start/step_end mark the Config phase boundary for the operation
    # screen (a plain-text sink may collapse them to phase() lines).
    if config_mgr.has_cached_config():
        config_mgr.load_cached_config()
        if config_mgr.is_seeded():
            em.step_start(
                "Config",
                f"Config seeded from {seeded_from or 'unknown'} -- review required",
                device_key=device_key,
            )
        else:
            em.step_start(
                "Config", f"Loaded cached config for '{entry.name}'", device_key=device_key
            )
    else:
        config_mgr.clear_klipper_config()
        em.step_start(
            "Config", "No cached config found, starting fresh", device_key=device_key
        )

    ran_menuconfig = False

    if config_mgr.is_seeded():
        # A seeded config never flows straight to build: force one review.
        skip_menuconfig = False

    if skip_menuconfig and not require_menuconfig:
        if config_mgr.has_cached_config():
            em.phase("Config", f"Using cached config for {entry.name}")
            # Skip menuconfig but still validate MCU below.
        else:
            em.warn(f"No cached config for '{entry.name}', launching menuconfig")
            skip_menuconfig = False  # Fall through to menuconfig

    if require_menuconfig or not skip_menuconfig:
        em.phase("Config", "Launching menuconfig...")
        ret_code, was_saved = run_menuconfig(
            klipper_dir, str(config_mgr.klipper_config_path)
        )
        ran_menuconfig = True

        if ret_code != 0:
            template = ERROR_TEMPLATES["menuconfig_failed"]
            em.error_with_recovery(
                template["error_type"],
                template["message_template"],
                context={"device": device_key},
                recovery=template["recovery_template"],
            )
            return ConfigOutcome(ok=False, exit_code=1, ran_menuconfig=ran_menuconfig)

        if not was_saved:
            em.warn("Config was not saved in menuconfig")
            if not decider.confirm(
                ConfirmDecision(
                    id="build_without_save",
                    message="Continue build anyway?",
                    default=False,
                )
            ):
                em.phase("Config", "Cancelled")
                return ConfigOutcome(ok=False, exit_code=0, ran_menuconfig=ran_menuconfig)

        try:
            config_mgr.save_cached_config()
            em.phase("Config", f"Cached config for '{entry.name}'")
            _warn_fragment_drift(em, seed_fragment, config_mgr)
        except ConfigError as e:
            em.error_with_recovery(
                "Config error",
                f"Failed to cache config: {e}",
                context={"device": device_key},
                recovery=(
                    "1. Verify Klipper directory is writable\n"
                    "2. Check disk space\n"
                    "3. Re-run menuconfig"
                ),
            )
            return ConfigOutcome(ok=False, exit_code=1, ran_menuconfig=ran_menuconfig)

    try:
        is_match, actual_mcu = config_mgr.validate_mcu(entry.mcu)
        if not is_match:
            template = ERROR_TEMPLATES["mcu_mismatch"]
            shown_mcu = actual_mcu or "unknown"
            em.error_with_recovery(
                template["error_type"],
                template["message_template"].format(actual=shown_mcu, expected=entry.mcu),
                context={
                    "device": device_key,
                    "expected": entry.mcu,
                    "actual": shown_mcu,
                },
                recovery=template["recovery_template"],
            )
            return ConfigOutcome(ok=False, exit_code=1, ran_menuconfig=ran_menuconfig)
        em.step_end("Config", f"MCU validated: {actual_mcu}", device_key=device_key)
    except ConfigError as e:
        em.error_with_recovery(
            "Config error",
            f"MCU validation failed: {e}",
            context={"device": device_key},
            recovery=(
                "1. Run menuconfig and verify MCU selection\n"
                "2. Check .config file exists\n"
                "3. Ensure CONFIG_MCU is set"
            ),
        )
        return ConfigOutcome(ok=False, exit_code=1, ran_menuconfig=ran_menuconfig)

    return ConfigOutcome(ok=True, exit_code=0, ran_menuconfig=ran_menuconfig)


# ---------------------------------------------------------------------------
# Version resolution + display
# ---------------------------------------------------------------------------


def resolve_target_mcu_version(
    entry: DeviceEntry, mcu_versions: dict
) -> Optional[str]:
    """Resolve which Moonraker MCU name corresponds to ``entry``.

    When the registry records the Klipper MCU object name, the lookup is
    strict: normalize ("mcu" -> "main", "mcu nhk" -> "nhk"), match keys
    case-insensitively, and return None when that MCU is not reporting.
    Never guess another board's version -- a wrong match here feeds the
    "firmware already up-to-date" prompt with the wrong board's version.

    Legacy entries without ``mcu_name`` keep the best-effort fuzzy match on
    name/key then chip-type, but no longer fall back to ``main``.
    Returns the matched MCU name (a key of ``mcu_versions``) or None.
    """
    if entry.mcu_name is not None:
        if entry.mcu_name == "mcu":
            lookup = "main"
        elif entry.mcu_name.startswith("mcu "):
            lookup = entry.mcu_name[4:]
        else:
            lookup = entry.mcu_name
        lookup_lower = lookup.lower()
        for mcu_name in mcu_versions:
            if mcu_name.lower() == lookup_lower:
                return mcu_name
        return None

    friendly = [n for n in mcu_versions if not any(c.isdigit() for c in n)]
    chip_keys = [n for n in mcu_versions if any(c.isdigit() for c in n)]
    target_mcu: Optional[str] = None
    for candidate in (entry.name, entry.key):
        if not candidate:
            continue
        cl = candidate.lower()
        for mcu_name in friendly:
            nl = mcu_name.lower()
            if nl == cl or nl in cl or cl in nl:
                target_mcu = mcu_name
                break
        if target_mcu:
            break
    if target_mcu is None:
        for mcu_name in friendly + chip_keys:
            if (
                entry.mcu.lower() in mcu_name.lower()
                or mcu_name.lower() in entry.mcu.lower()
            ):
                target_mcu = mcu_name
                break
    return target_mcu


def emit_host_and_mcu_versions(
    em: Emitter,
    host_version: str,
    mcu_versions: dict,
    target_mcu: Optional[str],
) -> None:
    """Emit the host banner and the per-MCU ``[*]`` listing (single path)."""
    em.phase("Version", f"Host: {detect_firmware_flavor(host_version)} {host_version}")

    # Build set of friendly names (no digits = not a chip-type alias)
    friendly_names = {n for n in mcu_versions if not any(c.isdigit() for c in n)}
    # If target matched a chip-type alias, find the friendly name with the same
    # version so we can mark it with [*].
    display_target = target_mcu
    if target_mcu and target_mcu not in friendly_names:
        target_ver = mcu_versions[target_mcu]
        for fn in friendly_names:
            if mcu_versions[fn] == target_ver:
                display_target = fn
                break

    for mcu_name, mcu_version in sorted(mcu_versions.items()):
        # Skip chip-type aliases (added for matching, not display).
        if mcu_name not in friendly_names:
            continue
        marker = "*" if mcu_name == display_target else " "
        em.phase("Version", f"  [{marker}] MCU {mcu_name}: {mcu_version}")

    if target_mcu is None:
        em.warn(
            "Device firmware version not reported by Klipper"
            " - cannot compare with host"
        )


# ---------------------------------------------------------------------------
# ccache flow
# ---------------------------------------------------------------------------


def _run_ccache_install(em: Emitter) -> bool:
    """Run apt install ccache with inherited stdio. Returns True on success."""
    em.phase("Install", "Installing ccache...")
    try:
        returncode = runner.run_interactive(
            ["sudo", "apt", "install", "-y", "ccache"], timeout=120
        )
    except TimeoutExpired:
        em.error("apt install timed out")
        return False
    except Exception as e:
        em.error(f"Installation failed: {e}")
        return False

    if returncode == 0:
        em.success("ccache installed successfully")
        return True
    em.error(f"apt install failed with exit code {returncode}")
    return False


def resolve_ccache_usage(
    *,
    registry,
    global_config: GlobalConfig,
    em: Emitter,
    decider: DecisionProvider,
) -> bool:
    """Resolve whether to use ccache for this build, prompting if needed."""
    use_ccache = global_config.use_ccache
    if not use_ccache:
        return False

    if not is_ccache_available() and not global_config.ccache_install_declined:
        em.phase("Build", "ccache not found. Install ccache for faster builds?")
        choice = decider.choose_ccache_action(ChooseCcacheActionDecision())
        if choice == "install":
            if _run_ccache_install(em):
                new_gc = dataclasses.replace(
                    global_config, use_ccache=True, ccache_install_declined=False
                )
                registry.save_global(new_gc)
                use_ccache = True
            else:
                use_ccache = False  # Install failed, skip ccache for this build
        elif choice == "disable":
            new_gc = dataclasses.replace(global_config, use_ccache=False)
            registry.save_global(new_gc)
            use_ccache = False
        elif choice == "skip":
            new_gc = dataclasses.replace(global_config, ccache_install_declined=True)
            registry.save_global(new_gc)
            use_ccache = False
        em.step_divider()

    return use_ccache


# ---------------------------------------------------------------------------
# The unified bootloader -> flash -> verify sequence
# ---------------------------------------------------------------------------


@dataclass
class FlashStepResult:
    bootloader_ok: bool = False
    flash_ok: bool = False
    verify_ok: bool = False
    method: str = ""
    device_path_new: Optional[str] = None
    error_reason: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.flash_ok and self.verify_ok


def run_flash_sequence(
    *,
    entry: DeviceEntry,
    device_path: Optional[str],
    firmware_path: str,
    config: GlobalConfig,
    klipper_dir: str,
    katapult_dir: str,
    em: Emitter,
    decider: Optional[DecisionProvider],
    batch: bool = False,
    verify_timeout: float = 30.0,
) -> FlashStepResult:
    """Enter bootloader, flash, and verify a single device.

    Runs strictly INSIDE the caller's ``klipper_service_stopped`` window; it
    never touches the service lifecycle. ``batch=True`` (or, for back-compat,
    ``decider is None``) selects batch behaviour (no retry prompt, batch
    manual-bootloader instruction, terser phase lines); the interactive
    single-device path passes a real decider with ``batch=False``. Lets
    ``KeyboardInterrupt``/``SystemExit`` propagate so the caller's context
    manager can restart Klipper.
    """
    batch = batch or decider is None
    is_can = entry.is_can_device
    result = FlashStepResult()

    # === Bootloader Phase ===
    # step_start/step_end carry the device key so the operation screen can drive
    # a per-device checklist + elapsed timer (a plain-text sink may collapse
    # them to the same phase() lines emitted before the seam existed).
    dkey = entry.key
    if entry.bootloader_method == "none":
        em.step_end("Bootloader", "Skipped (method: none)", device_key=dkey)
        boot_device_path: Optional[str] = device_path
        result.bootloader_ok = True
    else:
        if batch and is_can:
            em.step_start(
                "Bootloader",
                f"Entering CAN bootloader for {entry.name}...",
                device_key=dkey,
            )
        elif batch:
            em.step_start(
                "Bootloader", f"Entering {entry.bootloader_method}...", device_key=dkey
            )
        else:
            em.step_start(
                "Bootloader",
                f"Entering {entry.bootloader_method} bootloader...",
                device_key=dkey,
            )

        bootloader_stagger = (
            config.can_stagger_delay if is_can else config.stagger_delay
        )
        boot_result = enter_bootloader(
            device_path=device_path or "",
            device_entry=entry,
            klipper_dir=klipper_dir,
            katapult_dir=katapult_dir,
            stagger_delay=bootloader_stagger,
            em=em,
            decider=decider,
            batch=batch,
        )

        if not boot_result.success:
            result.bootloader_ok = False
            result.error_message = boot_result.error_message
            return result

        result.bootloader_ok = True
        boot_device_path = boot_result.device_path
        elapsed = boot_result.elapsed_seconds
        if batch:
            em.step_end(
                "Bootloader", f"Entered ({elapsed:.1f}s)", elapsed=elapsed, device_key=dkey
            )
        elif boot_device_path:
            em.step_end(
                "Bootloader",
                f"Entered ({elapsed:.1f}s) -- {_short_path(boot_device_path)}",
                elapsed=elapsed,
                device_key=dkey,
            )
        else:
            em.step_end(
                "Bootloader", f"Entered ({elapsed:.1f}s)", elapsed=elapsed, device_key=dkey
            )

    # === Flash Phase ===
    if not batch:
        em.step_start("Flash", "Flashing firmware...", device_key=dkey)
    flash_timeout = TIMEOUT_CAN_FLASH if is_can else TIMEOUT_FLASH
    flash_result = execute_flash(
        entry=entry,
        device_path=boot_device_path or "",
        firmware_path=firmware_path,
        config=config,
        timeout=flash_timeout,
        em=em,
    )
    result.method = flash_result.method

    if not flash_result.success:
        result.flash_ok = False
        result.error_message = flash_result.error_message
        return result
    result.flash_ok = True

    # === Verify Phase ===
    if is_can:
        if batch:
            em.step_start(
                "Verify", f"Querying CAN bus for {entry.name}...", device_key=dkey
            )
        else:
            em.step_start("Verify", "Querying CAN bus for device...", device_key=dkey)
        verified, error_reason = verify_can_device_after_flash(
            uuid=entry.canbus_uuid or "",
            interface=entry.canbus_interface or "can0",
            katapult_dir=katapult_dir,
        )
        result.device_path_new = None
    elif entry.flash_command == "flash_sdcard":
        # SD-card boards need a power cycle to re-enumerate; a USB wait here
        # would misreport a good flash as a verification failure.
        if batch:
            em.step_end(
                "Verify",
                "Skipped -- SD-card flash requires a power cycle",
                device_key=dkey,
            )
        else:
            em.step_end(
                "Verify",
                "Skipped -- SD-card flash requires a power cycle; "
                "confirm the device after restarting the board",
                device_key=dkey,
            )
        verified = True
        error_reason = None
        result.device_path_new = None
    else:
        if not batch:
            em.step_start("Verify", "Waiting for device to reappear...", device_key=dkey)
        verified, device_path_new, error_reason = wait_for_device(
            entry.serial_pattern or "",
            timeout=verify_timeout,
            em=em,
        )
        result.device_path_new = device_path_new

    result.verify_ok = verified
    if not verified:
        result.error_reason = error_reason
    return result
