"""Run the engine's menuconfig under ``app.suspend()`` + a config-diff receipt.

Shared by the two UI flows that need firmware configuration (UI_BRAINSTORM §6):

* the dashboard flash flow (``F``): offer/require menuconfig before a flash;
* the add-device wizard (``A``): a post-add "Configure firmware now?" step.

Both call :func:`run_menuconfig_suspended` (the suspend + engine-menuconfig +
diff snapshot) and render its :class:`MenuconfigResult` with
:class:`ConfigDiffDialog`.

------------------------------------------------------------------------------
SANCTIONED LAYERING EXCEPTION (note for the layering reviewers)
------------------------------------------------------------------------------
This module calls the engine's real menuconfig entry point
(:func:`kflash.build.run_menuconfig` -- a full-screen ``make menuconfig`` stdio
subprocess) **directly on the Textual main thread** inside
``with app.suspend():``. It deliberately does NOT go through the
:class:`~kflash.ui.engine_bridge.EngineBridge` worker + ``DecisionProvider``
seam every other engine call in the UI uses.

This is intentional and mirrors the dashboard's ``acquire_sudo()`` pre-flash
exception: menuconfig inherits the terminal's stdio for its ncurses UI, which a
bridge worker thread cannot host while Textual owns the screen. ``suspend()``
hands the real TTY to menuconfig and repaints on resume. The engine calls here
(``run_menuconfig`` + :class:`~kflash.config.ConfigManager`) are quick,
non-critical config-cache operations -- there is no Klipper-stopped window and
no long-running critical section, so the bridge's "one job, non-daemon thread"
machinery buys nothing. Every *other* engine call in the UI still goes through
the bridge.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ..boards import fragment_drift
from ..build import run_menuconfig
from ..config import ConfigManager
from .skin import COLORS, HintLine, Panel

if TYPE_CHECKING:
    from textual.app import App

__all__ = [
    "MenuconfigResult",
    "has_cached_config",
    "needs_review",
    "is_seeded",
    "run_menuconfig_suspended",
    "ConfigDiffDialog",
]


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
@dataclass
class MenuconfigResult:
    """Outcome of one :func:`run_menuconfig_suspended` round-trip."""

    ran: bool = False  # menuconfig actually launched
    saved: bool = False  # menuconfig saved (its .config mtime changed)
    changed: bool = False  # the cached .config content differs before -> after
    diff_lines: list[Text] = field(default_factory=list)  # rendered diff rows
    lines_changed: int = 0  # count of +/- lines (excludes ---/+++ headers)
    cancelled: bool = False  # Ctrl+C during the suspend window
    error: Optional[str] = None  # engine-side failure (non-zero exit, etc.)
    seeded_from: Optional[str] = None  # seed source label if the step started
    #                                    from a seeded-but-unreviewed cache
    drift_warnings: list[str] = field(default_factory=list)  # board-fragment
    #   CONFIG_ symbols an upstream Kconfig rename dropped on load (empty unless
    #   the step started from a board seed whose fragment was recorded)


# --------------------------------------------------------------------------- #
# Engine step (runs inside the suspend window)
# --------------------------------------------------------------------------- #
def has_cached_config(device_key: str, klipper_dir: str) -> bool:
    """True when a cached ``.config`` already exists for *device_key*."""
    return ConfigManager(device_key, klipper_dir).has_cached_config()


def needs_review(device_key: str, klipper_dir: str) -> bool:
    """True when the device has no cache OR its cache is seeded-but-unreviewed.

    The dashboard flash gate uses this (not ``has_cached_config``) so a seeded
    config never reaches build/flash without one menuconfig review: seeding
    makes ``has_cached_config`` true, which would otherwise silently drop the
    forced first review when ``menuconfig_before_flash`` is off.
    """
    mgr = ConfigManager(device_key, klipper_dir)
    return not mgr.has_cached_config() or mgr.is_seeded()


def is_seeded(device_key: str, klipper_dir: str) -> bool:
    """True when *device_key*'s cached ``.config`` was seeded and not yet
    reviewed in menuconfig (i.e. the ``.seeded`` marker is still present).

    Distinct from :func:`needs_review`: that also returns True when there is
    no cache at all, which is not "seeded" -- this is the dashboard-display
    read (a device row wants to say "seeded", not "no config yet").
    """
    return ConfigManager(device_key, klipper_dir).is_seeded()


def seed_source(device_key: str, klipper_dir: str) -> Optional[str]:
    """The seed label (``board:x`` / ``mcu-default:x`` / ``device:x``) for
    *device_key*'s cached config, or ``None`` when the cache is absent or
    already reviewed (the marker clears on a menuconfig save). Dashboard-display
    read for the details panel."""
    return ConfigManager(device_key, klipper_dir).seed_source()


def _humanize_seed(label: str) -> str:
    """Turn a seed source label into receipt-friendly prose.

    ``mcu-default:stm32h723`` -> ``stm32h723 default``; ``mcu-default:default``
    -> ``default``; ``device:octopus`` -> ``device octopus``; ``board:foo`` ->
    ``board foo``. Unknown shapes pass through unchanged.
    """
    kind, _, value = label.partition(":")
    if kind == "mcu-default":
        return "default" if value == "default" else f"{value} default"
    if kind == "device":
        return f"device {value}"
    if kind == "board":
        return f"board {value}"
    return label


def _read_config(path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []


def _render_diff(before: list[str], after: list[str]) -> tuple[list[Text], int]:
    """Return (rendered unified-diff rows, count of changed lines).

    Additions render green, removals red, hunk headers in the accent colour,
    context muted -- the skin's palette, no external theme.
    """
    rows: list[Text] = []
    changed = 0
    diff = difflib.unified_diff(
        before, after, fromfile="cached .config", tofile="new .config", lineterm=""
    )
    for line in diff:
        if line.startswith("+++") or line.startswith("---"):
            rows.append(Text(line, style=COLORS["subtle"]))
        elif line.startswith("+"):
            rows.append(Text(line, style=COLORS["green"]))
            changed += 1
        elif line.startswith("-"):
            rows.append(Text(line, style=COLORS["red"]))
            changed += 1
        elif line.startswith("@@"):
            rows.append(Text(line, style=COLORS["key_info"]))
        else:
            rows.append(Text(line, style=COLORS["subtle"]))
    return rows, changed


def _run_menuconfig_step(
    device_key: str,
    klipper_dir: str,
    mcu: Optional[str] = None,
    board: Optional[str] = None,
) -> MenuconfigResult:
    """Snapshot the cached ``.config``, run menuconfig, save, and diff.

    Mirrors ``flash_steps.load_and_validate_config``'s config step: the cached
    config (if any) is loaded into the klipper tree before launching menuconfig
    so the user edits the device's real config; a fresh device starts from a
    cleared ``.config``. On save the result is written back to the per-device
    cache, so a subsequent ``cmd_flash(skip_menuconfig=True)`` picks it up and
    still runs MCU validation engine-side. The diff is cached-before vs
    cached-after (the net change actually persisted).

    First-flash seeding: when the device has no cache, the cache is seeded so
    the user edits a sensible starting point -- preferring the board profile's
    fragment (when *board* is known) over the MCU default (when *mcu* is known).
    The ``before`` snapshot is taken AFTER seeding, so the diff shows only the
    user's edits on top of the seed -- not the whole seed as spurious additions.
    ``save_cached_config`` clears the ``.seeded`` marker once the user reviews it
    here.
    """
    config_mgr = ConfigManager(device_key, klipper_dir)

    if not config_mgr.has_cached_config():
        if board:
            config_mgr.seed_from_board(board)
        if not config_mgr.has_cached_config() and mcu:
            config_mgr.seed_from_default(mcu)

    # Read the seed label + recorded fragment now: save_cached_config() clears
    # the marker below, so this is the only point at which the "was seeded" fact
    # (and the fragment lines the drift check needs) is still available.
    seeded_from = config_mgr.seed_source()
    seed_fragment = config_mgr.seed_fragment_lines()

    before = _read_config(config_mgr.cache_path)

    if config_mgr.has_cached_config():
        config_mgr.load_cached_config()
    else:
        config_mgr.clear_klipper_config()

    ret_code, was_saved = run_menuconfig(
        klipper_dir, str(config_mgr.klipper_config_path)
    )
    if ret_code != 0:
        return MenuconfigResult(
            ran=True,
            error=f"menuconfig exited with code {ret_code}",
            seeded_from=seeded_from,
        )

    if was_saved:
        try:
            config_mgr.save_cached_config()
        except Exception as exc:  # noqa: BLE001 -- surface as a receipt error
            return MenuconfigResult(
                ran=True,
                saved=True,
                error=f"failed to cache config: {exc}",
                seeded_from=seeded_from,
            )

    after = _read_config(config_mgr.cache_path)
    rows, changed = _render_diff(before, after)
    # Drift check: a board fragment symbol absent from the saved config was
    # dropped on load (upstream Kconfig rename). Empty unless a board fragment
    # was recorded; unsaved round-trips leave the seed intact -> no drift.
    drift = fragment_drift(seed_fragment, after)
    return MenuconfigResult(
        ran=True,
        saved=was_saved,
        changed=changed > 0,
        diff_lines=rows,
        lines_changed=changed,
        seeded_from=seeded_from,
        drift_warnings=drift,
    )


# --------------------------------------------------------------------------- #
# Suspend orchestrator (runs on the Textual main thread)
# --------------------------------------------------------------------------- #
def _guarded_step(
    device_key: str,
    klipper_dir: str,
    mcu: Optional[str] = None,
    board: Optional[str] = None,
) -> MenuconfigResult:
    """Run :func:`_run_menuconfig_step`, translating every failure to a result.

    This is the ``never raises`` guarantee's teeth: neither a Ctrl+C nor an
    engine-side subprocess failure may unwind past
    :func:`run_menuconfig_suspended` into the Textual callback that invoked it
    (which would tear the whole app down). Ctrl+C in the suspend window is benign
    -- no Klipper-stopped window is open before a flash starts -- so it is
    reported as ``cancelled``; any other failure (a missing ``make``, a bad
    ``klipper_dir``, an ``OSError`` launching the subprocess) is surfaced as an
    ``error`` receipt the caller renders before returning to the dashboard.
    """
    try:
        return _run_menuconfig_step(device_key, klipper_dir, mcu, board)
    except KeyboardInterrupt:
        return MenuconfigResult(ran=True, cancelled=True)
    except Exception as exc:  # noqa: BLE001 -- surface, never crash the app
        return MenuconfigResult(ran=True, error=f"menuconfig failed: {exc}")


def run_menuconfig_suspended(
    app: App,
    device_key: str,
    klipper_dir: str,
    mcu: Optional[str] = None,
    board: Optional[str] = None,
) -> MenuconfigResult:
    """Run menuconfig under ``app.suspend()`` on the main thread; never raises.

    Must be called on the Textual main thread (a modal callback / action). Ctrl+C
    during the suspend window raises ``KeyboardInterrupt`` on the main thread
    *before* any flash starts (no Klipper-stopped window is open); it is caught
    and reported as ``cancelled`` so the caller can return to the dashboard
    instead of the app exiting. A subprocess-launch failure (missing ``make``,
    bad ``klipper_dir``, ``OSError``) is likewise caught and reported as an
    ``error`` receipt rather than escaping into the callback and crashing the app.
    """
    from textual.app import SuspendNotSupported

    try:
        with app.suspend():
            return _guarded_step(device_key, klipper_dir, mcu, board)
    except SuspendNotSupported:
        # Headless / test driver: suspend() is unavailable. Run the step
        # directly (tests stub run_menuconfig on this module).
        return _guarded_step(device_key, klipper_dir, mcu, board)


# --------------------------------------------------------------------------- #
# Config-diff receipt modal
# --------------------------------------------------------------------------- #
class ConfigDiffDialog(ModalScreen[bool]):
    """A scrollable config-diff receipt with a "N lines changed" summary.

    Dismisses ``True`` to continue or ``False`` to cancel. The diff itself is a
    *receipt* -- menuconfig already saved the config; Y/N never accepts or
    rejects the change. What Y/N actually decides is the caller's next action
    (e.g. "proceed with the flash?"), so a caller with a follow-up action MUST
    say so explicitly via ``question`` (rendered as the final ask under the
    diff) and ``continue_label``/``cancel_label`` (the key-hint labels, e.g.
    ``Flash now`` / ``Cancel flash``) -- hardware feedback showed a bare
    Continue/Cancel reads as accept/reject-the-diff.

    When ``show_cancel`` is ``False`` (the add-device receipt, where the device
    is already registered and there is nothing to abort) only a Close/Continue
    affordance is shown and it always dismisses ``True``.
    """

    DEFAULT_CSS = """
    ConfigDiffDialog .diff-scroll {
        height: auto;
        max-height: 20;
        background: $kf-surface;
        margin: 0 0 1 0;
    }
    ConfigDiffDialog #diff-body {
        height: auto;
        color: $kf-text;
    }
    """

    def __init__(
        self,
        result: MenuconfigResult,
        *,
        show_cancel: bool = True,
        title: str = "config diff",
        question: Optional[str] = None,
        continue_label: str = "Continue",
        cancel_label: str = "Cancel",
    ) -> None:
        super().__init__(classes="kf-modal")
        self._result = result
        self._show_cancel = show_cancel
        self._title = title
        self._question = question
        self._continue_label = continue_label
        self._cancel_label = cancel_label

    BINDINGS = [
        ("y", "cont", "Continue"),
        ("enter", "cont", "Continue"),
        ("n", "cancel", "Cancel"),
        ("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Panel(title=self._title, classes="dialog"):
            yield Static(self._summary(), classes="dialog-message")
            drift = self._drift_note()
            if drift is not None:
                yield Static(drift, id="diff-drift", classes="dialog-message")
            with VerticalScroll(classes="diff-scroll"):
                yield Static(self._diff_body(), id="diff-body")
            if self._question:
                yield Static(
                    Text(self._question, style=COLORS["prompt"]),
                    id="diff-question",
                    classes="dialog-message",
                )
            if self._show_cancel:
                yield HintLine(
                    [
                        ("Y/Enter", self._continue_label),
                        ("N/Esc", self._cancel_label),
                    ]
                )
            else:
                yield HintLine([("Enter/Esc", "Close")])

    def _summary(self) -> Text:
        n = self._result.lines_changed
        seed = self._seed_note()
        if not self._result.changed:
            text = Text("menuconfig saved no changes.", style=COLORS["subtle"])
            if seed is not None:
                text.append("\n")
                text.append_text(seed)
            return text
        plural = "line" if n == 1 else "lines"
        text = Text()
        text.append(f"{n} {plural} changed", style=COLORS["value"])
        text.append("  (", style=COLORS["subtle"])
        text.append("+ added", style=COLORS["green"])
        text.append(" / ", style=COLORS["subtle"])
        text.append("- removed", style=COLORS["red"])
        text.append(")", style=COLORS["subtle"])
        if seed is not None:
            text.append("\n")
            text.append_text(seed)
        return text

    def _seed_note(self) -> Optional[Text]:
        """A "seeded from ..." note when the config started from a seed."""
        label = self._result.seeded_from
        if not label:
            return None
        return Text(f"seeded from {_humanize_seed(label)}", style=COLORS["subtle"])

    def _drift_note(self) -> Optional[Text]:
        """A warning block when board-fragment symbols were dropped on load.

        None when the config carried no drift. Names each dropped symbol so the
        user can re-check the bootloader-offset / clock settings menuconfig fell
        back to a default for.
        """
        warnings = self._result.drift_warnings
        if not warnings:
            return None
        n = len(warnings)
        noun = "setting" if n == 1 else "settings"
        text = Text()
        text.append(
            f"⚠ {n} profile {noun} not recognized by this Kalico version:",
            style=COLORS["yellow"],
        )
        for symbol in warnings:
            text.append("\n    ")
            text.append(symbol, style=COLORS["yellow"])
        text.append(
            "\n  The build will use this tree's default instead. Verify the "
            "bootloader\n  offset / clock settings in menuconfig before flashing.",
            style=COLORS["subtle"],
        )
        return text

    def _diff_body(self) -> Text:
        if not self._result.diff_lines:
            return Text("(no differences)", style=COLORS["subtle"])
        body = Text()
        for index, row in enumerate(self._result.diff_lines):
            if index:
                body.append("\n")
            body.append_text(row)
        return body

    def action_cont(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        # With no cancel affordance (add-device receipt), Escape just closes
        # (nothing to abort), so it dismisses True like Continue.
        self.dismiss(not self._show_cancel)
