"""The kflash Textual application shell.

:class:`KflashApp` is the composition root for the kflash UI. It owns the
:class:`~kflash.registry.Registry` and the single
:class:`~kflash.ui.engine_bridge.EngineBridge`, registers the kflash theme,
disables the stock chrome (no ``Header``/``Footer`` -- the dashboard supplies a
:class:`~kflash.ui.skin.HintLine` instead), turns animation off, disables the
command palette, and pushes the :class:`DashboardScreen`.

It deliberately holds no engine logic: every blocking engine operation is run by
the :class:`EngineBridge` on a non-daemon worker thread (see that module's
docstring for why not ``@work(thread=True)``), and every read the dashboard
performs goes straight to the engine's own modules. This module never imports a
legacy UI module (``tui``/``screen``/``panels``/``theme``/``ansi``).
"""

from __future__ import annotations

from typing import Optional, Protocol

from textual.app import App

from kflash.registry import Registry

from . import skin
from .dialogs import styled_modal_factory
from .engine_bridge import EngineBridge, EngineJobCompleted
from .screens.dashboard import DashboardScreen


class _JobScreen(Protocol):
    """A screen that can receive engine job-completion messages."""

    def handle_job_completed(self, message: EngineJobCompleted) -> None: ...


class KflashApp(App[None]):
    """The kflash Textual app: owns the registry + engine bridge, one dashboard."""

    CSS_PATH = [skin.CSS_PATH]
    ENABLE_COMMAND_PALETTE = False
    TITLE = "kflash"

    def __init__(self, registry: Registry) -> None:
        super().__init__()
        self.registry = registry
        self.bridge: Optional[EngineBridge] = None
        self._dashboard: Optional[DashboardScreen] = None
        # A pushed screen (e.g. the add-device wizard) that owns the current
        # engine job; job-completion messages route to it instead of the
        # dashboard while set. None -> completions belong to the dashboard.
        self._active_job_screen: Optional[_JobScreen] = None
        # Register + activate the kflash theme before CSS is parsed so the
        # $kf-* variables resolve; disable animation for a calm, instant,
        # snapshot-deterministic feel (mirrors the style guide).
        self.register_theme(skin.KFLASH_THEME)
        self.theme = skin.KFLASH_THEME_NAME
        self.animation_level = "none"

    def on_mount(self) -> None:
        # The dashboard is the event target so its on_engine_event handler
        # renders the flash log directly; job-completion messages always arrive
        # at the app (see EngineBridge) and are forwarded to the dashboard.
        dashboard = DashboardScreen()
        self._dashboard = dashboard
        # Flash decisions (method picker, mcu-mismatch, ccache, manual
        # bootloader, confirms) render through the styled R4 modals.
        self.bridge = EngineBridge(
            self, event_target=dashboard, modal_factory=styled_modal_factory
        )
        self.push_screen(dashboard)

    def on_engine_job_completed(self, message: EngineJobCompleted) -> None:
        # Bridge posts completion to the app; route it to the screen that owns
        # the job (the add-device wizard, if active) else the dashboard.
        # NOTE: tests/ui/test_device_config.py's ConfigHost.on_engine_job_completed
        # mirrors this routing (active job screen else dashboard) -- keep the two
        # in sync if this dispatch changes.
        target = self._active_job_screen or self._dashboard
        if target is not None:
            target.handle_job_completed(message)

    def on_unmount(self) -> None:
        # Release any worker blocked on a decision and join the (non-daemon)
        # engine thread so a torn-down screen never strands a flash.
        if self.bridge is not None:
            self.bridge.shutdown()


def run_ui(registry: Registry) -> int:
    """Run the Textual UI to completion. Returns a process exit code."""
    KflashApp(registry).run()
    return 0
