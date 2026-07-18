"""The add-device wizard (dashboard action ``A``), rendered as the engine flow.

This screen is a thin view over the *real* engine command
:func:`kflash.commands.cmd_add_device`. The command is fully decider-driven --
every prompt it makes goes through the injected
:class:`~kflash.decisions.DecisionProvider` -- so the wizard IS that command run
on the :class:`~kflash.ui.engine_bridge.EngineBridge` worker thread, with its
questions serviced by the styled R4 modals (:func:`kflash.ui.dialogs.styled_modal_factory`)
and its ``Emitter`` events streamed into a :class:`~textual.widgets.RichLog`.

Two entry paths, matching the legacy ``tui._action_add_device``:

* **USB new-device pick** -- the dashboard passes the highlighted scanned "new"
  row as a :class:`~kflash.models.DiscoveredDevice`; the command skips discovery
  and jumps to naming/MCU/flash-method.
* **Fresh scan / CAN** -- no pre-selected device; the command runs full USB
  discovery, shows a device-choice modal, and (when CAN interfaces exist) offers
  CAN-bus registration -- the same "A with no new devices" path the legacy flow
  used.

menuconfig (Stage 2, UI_BRAINSTORM §6): ``cmd_add_device`` ends by offering to
run ``make menuconfig`` (``ConfirmDecision(id="run_menuconfig_now")``), a
full-screen stdio subprocess that cannot run on the bridge worker thread while
Textual owns the terminal. The worker-side prompt is therefore answered "no"
(:class:`_AddDeviceDecider`) -- but this is no longer a dead-end: on success the
wizard detects the newly-registered device and offers a real UI-driven
"Configure firmware now?" step that runs menuconfig through ``app.suspend()`` on
the main thread (the shared :mod:`kflash.ui.menuconfig` helper) plus a
config-diff receipt. So the decline defers menuconfig to the UI, it does not
drop it.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, Optional, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import RichLog, Static

from ...commands import cmd_add_device
from ...events import Emitter, FlashEvent
from ...models import DiscoveredDevice
from .. import menuconfig
from ..dialogs import DecisionConfirmDialog, styled_modal_factory
from ..engine_bridge import EngineBridge, EngineBusyError, EngineEvent, EngineJobCompleted
from ..skin import COLORS, HintLine, Panel, status_marker

if TYPE_CHECKING:
    from ..app import KflashApp

_HINTS_RUNNING: list[tuple[str, str]] = [("Esc", "Cancel step")]
_HINTS_DONE: list[tuple[str, str]] = [("Enter/Esc", "Return to dashboard")]


class _AddDeviceDecider:
    """Delegating :class:`~kflash.decisions.DecisionProvider` for the wizard.

    Forwards every decision to the bridge's real provider except the terminal
    "Run menuconfig now?" confirm, which is answered "no" on the worker: that
    engine path would launch ``make menuconfig`` (a stdio subprocess) on the
    bridge worker thread, which cannot run while Textual owns the terminal. This
    is NOT a dead-end -- the wizard offers a real UI-driven "Configure firmware
    now?" step post-add that runs menuconfig under ``app.suspend()`` (see the
    module docstring). The decline just moves menuconfig from the worker to the
    UI thread.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def confirm(self, req: Any) -> bool:
        if getattr(req, "id", None) == "run_menuconfig_now":
            return False
        return bool(self._inner.confirm(req))

    def __getattr__(self, name: str) -> Any:
        # Delegate choose_device / choose_flash_method / mcu_mismatch /
        # prompt_text / ... unchanged to the styled-modal-backed provider.
        return getattr(self._inner, name)


class AddDeviceScreen(Screen[None]):
    """Runs ``cmd_add_device`` on the bridge and renders it as modals + a log."""

    BINDINGS = [
        ("escape", "return_home", "Return"),
        ("enter", "return_home", "Return"),
        ("q", "return_home", "Return"),
    ]

    def __init__(
        self,
        *,
        selected_device: Optional[DiscoveredDevice] = None,
        can_only: bool = False,
    ) -> None:
        super().__init__()
        self._selected_device = selected_device
        self._can_only = can_only
        self._bridge: Optional[EngineBridge] = None
        self._done = False
        self._result_status = ""
        self._result_level = "info"
        # Registry device keys before the job, so a post-add success can detect
        # which device was registered and offer to configure it.
        self._keys_before: set[str] = set()

    @property
    def kflash_app(self) -> KflashApp:
        return cast("KflashApp", self.app)

    def compose(self) -> ComposeResult:
        with VerticalScroll(), Panel(title="add device"):
            yield Static(id="add-status", classes="status-line")
            yield RichLog(id="add-log", highlight=False, markup=False, wrap=True)
        yield HintLine(_HINTS_RUNNING, id="add-hints")

    def on_mount(self) -> None:
        self._set_status("Starting add-device wizard...", "info")
        # Dedicated bridge: events target THIS screen's log, and every decision
        # is serviced by the styled R4 modals. Completion is routed back here by
        # the app (which tracks the active job screen).
        self._bridge = EngineBridge(
            self.app, event_target=self, modal_factory=styled_modal_factory
        )
        self.kflash_app._active_job_screen = self
        emitter = Emitter(self._bridge.events)
        decider = _AddDeviceDecider(self._bridge.decisions)
        registry = self.kflash_app.registry
        try:
            self._keys_before = set(registry.load().devices.keys())
        except Exception:
            self._keys_before = set()
        selected = self._selected_device
        can_only = self._can_only

        job = functools.partial(
            _run_add_device, registry, emitter, decider, selected, can_only
        )
        try:
            self._bridge.run_engine_job(job, name="kflash-add-device")
        except EngineBusyError:
            self._finish(1, cancelled=False, error=None)

    def on_unmount(self) -> None:
        # Release any worker blocked on a modal and join the non-daemon thread.
        if self._bridge is not None:
            self._bridge.shutdown(timeout=5)

    # -- event stream ---------------------------------------------------- #
    def on_engine_event(self, message: EngineEvent) -> None:
        self.query_one("#add-log", RichLog).write(self._render_event(message.event))

    def _render_event(self, event: FlashEvent) -> Text:
        kind = event.kind
        if kind == "success":
            text = status_marker("ok").copy()
            text.append(" ")
            text.append(event.message, style=COLORS["green"])
            return text
        if kind == "warn":
            text = status_marker("warn").copy()
            text.append(" ")
            text.append(event.message, style=COLORS["yellow"])
            return text
        if kind in ("error", "error_recovery"):
            text = status_marker("error").copy()
            text.append(" ")
            text.append(event.error_type or event.message, style=COLORS["red"])
            return text
        if kind == "device_line":
            marker = f"[{event.marker}] " if event.marker else ""
            return Text.assemble(
                (marker, COLORS["label"]),
                (event.name or "", COLORS["text"]),
                ("  " + (event.detail or "") if event.detail else "", COLORS["subtle"]),
            )
        if kind == "step_divider":
            return Text("-" * 40, style=COLORS["subtle"])
        if kind == "info":
            prefix = f"{event.section}: " if event.section else ""
            return Text(f"{prefix}{event.message}", style=COLORS["text"])
        return Text(event.message, style=COLORS["subtle"])

    # -- completion ------------------------------------------------------ #
    def handle_job_completed(self, message: EngineJobCompleted) -> None:
        self._finish(message.result, message.cancelled, message.error)

    def _finish(
        self, result: Any, cancelled: bool, error: Optional[BaseException]
    ) -> None:
        log = self.query_one("#add-log", RichLog)
        if cancelled:
            log.write(Text("Add device cancelled.", style=COLORS["yellow"]))
            self._result_status, self._result_level = ("Add device cancelled.", "warning")
        elif error is not None:
            log.write(Text(f"Add device failed: {error}", style=COLORS["red"]))
            self._result_status, self._result_level = (f"Add device failed: {error}", "error")
        elif result == 0:
            log.write(Text("Done. Press Enter to return.", style=COLORS["green"]))
            self._result_status, self._result_level = ("Device added.", "success")
        else:
            log.write(Text("Add device did not complete.", style=COLORS["yellow"]))
            self._result_status, self._result_level = (
                "Add device cancelled or failed.",
                "warning",
            )
        self._done = True
        self._set_status(self._result_status, self._result_level)
        self.query_one("#add-hints", HintLine).update(HintLine._render_hints(_HINTS_DONE))
        if not cancelled and error is None and result == 0:
            self._offer_configure()

    # -- post-add configure (menuconfig under suspend + diff receipt) ----- #
    def _new_device(
        self,
    ) -> Optional[tuple[str, str, Optional[str], Optional[str]]]:
        """Return ``(device_key, klipper_dir, mcu, board)`` for the just-added device.

        Finds the single new registry key vs the pre-job snapshot; returns
        ``None`` if zero or several appeared (ambiguous) or there is no global
        config to build against. ``mcu`` and ``board`` (from the new registry
        row) are threaded into the menuconfig helper so a first-flash config can
        be seeded (board fragment preferred over MCU default).
        """
        try:
            data = self.kflash_app.registry.load()
        except Exception:
            return None
        if data.global_config is None:
            return None
        new_keys = set(data.devices.keys()) - self._keys_before
        if len(new_keys) != 1:
            return None
        key = next(iter(new_keys))
        entry = data.devices.get(key)
        mcu = entry.mcu if entry is not None else None
        board = entry.board if entry is not None else None
        return key, data.global_config.klipper_dir, mcu, board

    def _offer_configure(self) -> None:
        """Offer to run menuconfig for the newly-added device (post-add)."""
        target = self._new_device()
        if target is None:
            return
        device_key, klipper_dir, mcu, board = target

        def _after_offer(configure: Optional[bool]) -> None:
            if configure:
                self._run_configure(device_key, klipper_dir, mcu, board)

        self.app.push_screen(
            DecisionConfirmDialog(
                "Configure firmware now?", default=False, title="menuconfig"
            ),
            _after_offer,
        )

    def _run_configure(
        self,
        device_key: str,
        klipper_dir: str,
        mcu: Optional[str] = None,
        board: Optional[str] = None,
    ) -> None:
        """Suspend, run menuconfig, then show the diff receipt (informational)."""
        result = menuconfig.run_menuconfig_suspended(
            self.app, device_key, klipper_dir, mcu, board
        )
        log = self.query_one("#add-log", RichLog)
        if result.cancelled:
            log.write(Text("menuconfig cancelled.", style=COLORS["yellow"]))
            self._set_status("Device added; menuconfig cancelled.", "warning")
            return
        if result.error:
            log.write(Text(f"menuconfig: {result.error}", style=COLORS["red"]))
            self._set_status(f"Device added; menuconfig: {result.error}", "warning")
            return
        if result.changed:
            self._set_status(
                f"Device added; config saved ({result.lines_changed} lines changed).",
                "success",
            )
            # Informational receipt: the device is already registered, so the
            # diff has nothing to cancel into (Close only).
            self.app.push_screen(
                menuconfig.ConfigDiffDialog(result, show_cancel=False)
            )
        else:
            self._set_status("Device added; menuconfig saved no changes.", "info")

    # -- navigation ------------------------------------------------------ #
    def action_return_home(self) -> None:
        # Only leave once the wizard has finished (while running, the modals own
        # the keyboard so this is unreachable mid-flow).
        if not self._done:
            return
        app = self.kflash_app
        if app._active_job_screen is self:
            app._active_job_screen = None
        if self is app.screen:
            app.pop_screen()
        dashboard = app._dashboard
        if dashboard is not None:
            dashboard.refresh_devices(self._result_status, self._result_level)

    # -- helpers --------------------------------------------------------- #
    def _set_status(self, message: str, level: str) -> None:
        role = {
            "success": "green",
            "error": "red",
            "warning": "yellow",
            "info": "text",
        }.get(level, "text")
        self.query_one("#add-status", Static).update(Text(message, style=COLORS[role]))


def _run_add_device(
    registry: Any,
    emitter: Emitter,
    decider: Any,
    selected_device: Optional[DiscoveredDevice],
    can_only: bool,
) -> int:
    """Worker-thread entry: dispatch to ``cmd_add_device`` with the right path.

    The module-global ``cmd_add_device`` is resolved at call time, so a test can
    stub ``add_device.cmd_add_device`` (matching the dashboard's flash pattern).
    """
    if selected_device is not None:
        return cmd_add_device(registry, emitter, decider, selected_device=selected_device)
    return cmd_add_device(registry, emitter, decider, can_only=can_only)
