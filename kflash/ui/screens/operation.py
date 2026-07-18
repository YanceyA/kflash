"""The R5 operation screen: a dedicated view over one flash job.

Pushed by the dashboard for both a single flash (``F``) and Flash All (``B``),
this screen renders the engine's :class:`~kflash.events.FlashEvent` stream
(delivered as :class:`~kflash.ui.engine_bridge.EngineEvent` messages) into:

* a **phase checklist** (Preflight/Discovery/Safety/Config/Build/Bootloader/
  Flash/Verify/Service) with pending/active/done/failed glyphs in the skin's
  status colours and a per-phase elapsed timer that ticks (``set_interval``)
  while a phase is active. State is driven from ``step_start``/``step_end`` (the
  hardware phases, which also carry ``elapsed``) and plain ``phase`` lines;
* a **scrolling log tail** (:class:`~textual.widgets.RichLog`) of every event,
  styled by the shared :func:`render_event` (also used to render the
  ``error_recovery`` context key/values);
* a **progress bar** fed only by structured ``FlashEvent.progress`` values (the
  engine emits these from real flashtool output); the panel stays hidden until
  the first such value arrives, and re-hides on the next ``device_divider`` in
  Flash All mode so a finished device's bar doesn't linger into the next one;
* in **Flash All** mode, a device ``i/N`` header and a **results table**
  (device / result / duration / version) that fills as devices complete and is
  the final view.

Failure hold (legacy H7 fix): on job failure the screen stays put with the
sticky error + recovery steps visible; the user must press a key to return. On
success it shows a brief summary and returns on the same key. The screen never
runs engine code itself -- it only consumes messages; the dashboard owns the
:class:`~kflash.ui.engine_bridge.EngineBridge` job.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, ProgressBar, RichLog, Static

from ...events import FlashEvent
from ..engine_bridge import EngineEvent, EngineJobCompleted
from ..skin import COLORS, Panel, status_marker

# Ordered checklist phases. Incoming events are matched to one of these by their
# section label (full, case-insensitive); sections outside this set ("Version",
# "Flash All", "Install", "Summary", ...) still stream into the log but do not
# drive the checklist.
_CHECKLIST: tuple[str, ...] = (
    "Preflight",
    "Discovery",
    "Safety",
    "Config",
    "Build",
    "Bootloader",
    "Flash",
    "Verify",
    "Service",
)
_MATCH: dict[str, str] = {name.lower(): name for name in _CHECKLIST}

# "Flashing 3 device(s)" -> the batch total, for the i/N header before the first
# device_divider arrives.
_TOTAL = re.compile(r"(\d+)\s+device")


def render_event(event: FlashEvent) -> Text:
    """Render one :class:`~kflash.events.FlashEvent` as skinned Rich ``Text``.

    Shared by the operation screen's log tail (extracted from the Stage 1
    dashboard renderer so the two never drift). Unlike the Stage 1 version this
    also renders the ``error_recovery`` *context* key/values, not just the
    recovery steps.
    """
    kind = event.kind
    if kind in ("phase", "step_start", "step_end"):
        label = event.section or event.phase
        return Text(f"[{label}] {event.message}", style=COLORS["border"])
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
        head = event.error_type or event.message
        text.append(head, style=COLORS["red"])
        if kind == "error_recovery":
            if event.message and event.error_type:
                text.append(f": {event.message}", style=COLORS["red"])
            if event.context:
                for key, value in event.context.items():
                    text.append(f"\n    {key}: {value}", style=COLORS["subtle"])
            if event.recovery:
                text.append("\n")
                text.append(event.recovery, style=COLORS["subtle"])
        return text
    if kind == "device_line":
        marker = f"[{event.marker}]" if event.marker else ""
        return Text.assemble(
            (marker + " " if marker else "", COLORS["label"]),
            (event.name or "", COLORS["text"]),
            ("  " + (event.detail or "") if event.detail else "", COLORS["subtle"]),
        )
    if kind in ("step_divider", "device_divider"):
        return Text("-" * 40, style=COLORS["subtle"])
    if kind == "info":
        prefix = f"{event.section}: " if event.section else ""
        return Text(f"{prefix}{event.message}", style=COLORS["subtle"])
    # progress and anything else: show the message quietly if present.
    return Text(event.message, style=COLORS["subtle"])


@dataclass
class _Phase:
    """One checklist row's live state."""

    name: str
    state: str = "pending"  # pending | active | done | failed
    start: Optional[float] = None  # monotonic when it became active
    elapsed: Optional[float] = None  # frozen seconds once done/failed

    def display_elapsed(self, now: float) -> Optional[float]:
        if self.elapsed is not None:
            return self.elapsed
        if self.state == "active" and self.start is not None:
            return now - self.start
        return None


class OperationScreen(Screen[None]):
    """R5 flash/build screen for one job -- single flash or Flash All."""

    DEFAULT_CSS = """
    OperationScreen RichLog {
        height: 1fr;
        min-height: 6;
        background: $kf-background;
        color: $kf-text;
    }
    OperationScreen #op-checklist {
        height: auto;
        color: $kf-text;
    }
    OperationScreen #op-header, OperationScreen #op-progress-label {
        height: auto;
        color: $kf-text;
    }
    OperationScreen #op-banner {
        height: auto;
        color: $kf-text;
    }
    OperationScreen #op-footer {
        height: 1;
        color: $kf-subtle;
    }
    OperationScreen DataTable {
        max-height: 12;
    }
    OperationScreen #op-progress-panel {
        display: none;
    }
    """

    BINDINGS = [
        ("enter", "return_dashboard", "Return"),
        ("escape", "return_dashboard", "Return"),
        ("q", "return_dashboard", "Return"),
        ("space", "return_dashboard", "Return"),
    ]

    _RESULT_COLUMNS = ("Device", "Result", "Duration", "Version")

    def __init__(self, *, mode: str, title: str) -> None:
        """``mode`` is ``"single"`` or ``"all"``; ``title`` names the target."""
        super().__init__()
        self._mode = mode
        self._title = title
        self._phases: list[_Phase] = [_Phase(name) for name in _CHECKLIST]
        self._done = False  # job completed (held view shown)
        self._device_index = 0
        self._device_total = 0
        self._current_device = ""
        self._last_error: Optional[FlashEvent] = None
        self._results_seen: set[str] = set()
        self._tick_timer: Optional[Timer] = None
        # Monotonic clock seam: tests freeze it for deterministic elapsed timers.
        self._clock = time.monotonic
        # Events (and a completion) can be posted by the worker before this
        # screen finishes mounting; buffer them until on_mount, then replay.
        self._ready = False
        self._buffer: list[FlashEvent] = []
        self._pending_completion: Optional[EngineJobCompleted] = None

    # -- composition ----------------------------------------------------- #
    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Panel(title="operation"):
                yield Static(self._header_text(), id="op-header")
            with Panel(title="phases"):
                yield Static(id="op-checklist")
            with Panel(title="progress", id="op-progress-panel"):
                yield Static(Text("", style=COLORS["text"]), id="op-progress-label")
                yield ProgressBar(total=100, show_eta=False, id="op-progress")
            if self._mode == "all":
                with Panel(title="results"):
                    yield DataTable(
                        id="op-results", zebra_stripes=False, cursor_type="none"
                    )
            with Panel(title="log"):
                yield RichLog(id="op-log", highlight=False, markup=False, wrap=True)
        # Banner + footer live OUTSIDE the scroll so the sticky result/error and
        # the return hint are always visible after the job completes.
        yield Static("", id="op-banner")
        yield Static(self._footer_text(), id="op-footer")

    def on_mount(self) -> None:
        if self._mode == "all":
            table = self.query_one("#op-results", DataTable)
            table.add_columns(*self._RESULT_COLUMNS)
        self._render_checklist()
        # Tick the active phase's elapsed timer twice a second.
        self._tick_timer = self.set_interval(0.5, self._tick)
        # Replay anything that arrived before mount, then go live.
        self._ready = True
        for event in self._buffer:
            self._process(event)
        self._buffer.clear()
        if self._pending_completion is not None:
            completion, self._pending_completion = self._pending_completion, None
            self.job_completed(completion)

    # -- event ingestion ------------------------------------------------- #
    def on_engine_event(self, message: EngineEvent) -> None:
        """Direct-target path: a bridge whose ``event_target`` is this screen."""
        self.ingest(message.event)

    def on_engine_job_completed(self, message: EngineJobCompleted) -> None:
        """Completion delivered onto THIS screen's pump.

        The dashboard (or a bridge host) re-posts the bridge's completion here
        rather than calling :meth:`job_completed` directly: engine events flow
        to this screen's pump while the completion is posted to the app's pump,
        so a direct call could finalize the checklist before the last events
        drain. Re-posting puts completion at the tail of this pump's queue,
        after every event, guaranteeing correct ordering.
        """
        self.job_completed(message)

    def ingest(self, event: FlashEvent) -> None:
        """Consume one engine event (also called by the dashboard router)."""
        if not self._ready:
            self._buffer.append(event)
            return
        self._process(event)

    def _process(self, event: FlashEvent) -> None:
        self.query_one("#op-log", RichLog).write(render_event(event))
        self._drive_progress(event)
        self._drive_header(event)
        self._drive_checklist(event)
        if self._mode == "all":
            self._maybe_result_row(event)

    # -- progress bar ---------------------------------------------------- #
    def _drive_progress(self, event: FlashEvent) -> None:
        if event.progress is None:
            return
        pct = max(0.0, min(1.0, event.progress)) * 100.0
        self.query_one("#op-progress-panel").display = True
        self.query_one("#op-progress", ProgressBar).update(progress=pct)
        label = event.section or "Progress"
        self.query_one("#op-progress-label", Static).update(
            Text(f"{label}: {pct:.0f}%", style=COLORS["text"])
        )

    def _reset_progress(self) -> None:
        """Hide the progress panel and zero the bar/label for the next device."""
        self.query_one("#op-progress-panel").display = False
        self.query_one("#op-progress", ProgressBar).update(progress=0)
        self.query_one("#op-progress-label", Static).update(Text(""))

    # -- header (Flash All device i/N) ----------------------------------- #
    def _drive_header(self, event: FlashEvent) -> None:
        changed = False
        if event.kind == "device_divider":
            if event.index:
                self._device_index = event.index
            if event.total:
                self._device_total = event.total
            if event.name:
                self._current_device = event.name
            self._reset_progress()
            changed = True
        elif self._device_total == 0 and event.message:
            match = _TOTAL.search(event.message)
            if match:
                self._device_total = int(match.group(1))
                self._device_index = max(self._device_index, 1)
                changed = True
        if changed:
            self.query_one("#op-header", Static).update(self._header_text())

    # -- checklist ------------------------------------------------------- #
    def _drive_checklist(self, event: FlashEvent) -> None:
        if event.kind in ("error", "error_recovery"):
            self._last_error = event
            self._fail_active()
            self._render_checklist()
            return
        label = _MATCH.get((event.section or "").strip().lower())
        if label is None:
            return
        if event.kind == "step_end":
            self._finish(label, event.elapsed)
        else:  # step_start, phase, info, progress carrying a known section
            self._activate(label)
        self._render_checklist()

    def _phase(self, name: str) -> _Phase:
        return next(p for p in self._phases if p.name == name)

    def _activate(self, name: str) -> None:
        target = self._phase(name)
        target_idx = self._phases.index(target)
        # A later phase starting means every earlier still-active phase finished.
        for phase in self._phases[:target_idx]:
            if phase.state == "active":
                self._freeze(phase, "done")
        if target.state in ("pending", "done"):
            target.state = "active"
            target.start = self._clock()
            target.elapsed = None

    def _finish(self, name: str, elapsed: Optional[float]) -> None:
        phase = self._phase(name)
        if phase.state == "failed":
            return
        phase.state = "done"
        if elapsed is not None:
            phase.elapsed = elapsed
        elif phase.start is not None:
            phase.elapsed = self._clock() - phase.start

    def _freeze(self, phase: _Phase, state: str) -> None:
        phase.state = state
        if phase.elapsed is None and phase.start is not None:
            phase.elapsed = self._clock() - phase.start

    def _fail_active(self) -> None:
        for phase in self._phases:
            if phase.state == "active":
                self._freeze(phase, "failed")

    def _finalize_remaining(self, state: str) -> None:
        for phase in self._phases:
            if phase.state == "active":
                self._freeze(phase, state)

    def _tick(self) -> None:
        if any(p.state == "active" for p in self._phases):
            self._render_checklist()

    def _render_checklist(self) -> None:
        now = self._clock()
        text = Text()
        for index, phase in enumerate(self._phases):
            if index:
                text.append("\n")
            text.append_text(self._phase_row(phase, now))
        self.query_one("#op-checklist", Static).update(text)

    def _phase_row(self, phase: _Phase, now: float) -> Text:
        if phase.state == "done":
            row = status_marker("ok").copy()
            name_style = COLORS["text"]
        elif phase.state == "failed":
            row = status_marker("error").copy()
            name_style = COLORS["red"]
        elif phase.state == "active":
            row = Text("[>>]", style=COLORS["yellow"])
            name_style = COLORS["text"]
        else:  # pending
            row = Text("[  ]", style=COLORS["subtle"])
            name_style = COLORS["subtle"]
        row.append(" ")
        row.append(phase.name.ljust(10), style=name_style)
        secs = phase.display_elapsed(now)
        if secs is not None:
            row.append(f"  {secs:5.1f}s", style=COLORS["subtle"])
        return row

    # -- Flash All results table ----------------------------------------- #
    def _maybe_result_row(self, event: FlashEvent) -> None:
        if event.kind != "info" or not event.device_key:
            return
        if event.device_key in self._results_seen:
            return
        self._results_seen.add(event.device_key)
        passed = event.marker == "PASS"
        result_cell = (
            status_marker("ok").copy() if passed else status_marker("error").copy()
        )
        result_cell.append(" pass" if passed else " fail", style=COLORS["subtle"])
        if event.elapsed is not None:
            duration = Text(f"{event.elapsed:.1f}s", style=COLORS["subtle"])
        else:
            duration = Text("-", style=COLORS["subtle"])
        version = "-"
        if event.context:
            before = event.context.get("version_before")
            after = event.context.get("version_after")
            if before or after:
                version = f"{before or '?'} -> {after or '?'}"
        self.query_one("#op-results", DataTable).add_row(
            Text(event.device_name or event.device_key, style=COLORS["text"]),
            result_cell,
            duration,
            Text(version, style=COLORS["subtle"]),
        )

    # -- completion ------------------------------------------------------ #
    def job_completed(self, message: EngineJobCompleted) -> None:
        """Enter the held view: sticky summary/error until the user returns."""
        if not self._ready:
            self._pending_completion = message
            return
        self._done = True
        if self._tick_timer is not None:
            self._tick_timer.stop()
        banner = self.query_one("#op-banner", Static)
        if message.cancelled:
            self._finalize_remaining("failed")
            banner.update(
                Text("Flash cancelled.", style=COLORS["yellow"])
            )
        elif message.ok and (message.result in (0, None)):
            self._finalize_remaining("done")
            banner.update(self._success_banner())
        else:
            self._finalize_remaining("failed")
            banner.update(self._failure_banner(message))
        self._render_checklist()
        self.query_one("#op-footer", Static).update(
            Text("Press Enter to return to the dashboard.", style=COLORS["subtle"])
        )

    def _success_banner(self) -> Text:
        text = status_marker("ok").copy()
        text.append(" ")
        if self._mode == "all":
            text.append("Flash All complete. See the results above.", COLORS["green"])
        else:
            text.append(f"Flashed {self._title} successfully.", COLORS["green"])
        return text

    def _failure_banner(self, message: EngineJobCompleted) -> Text:
        text = status_marker("error").copy()
        text.append(" ")
        if self._last_error is not None and (
            self._last_error.error_type or self._last_error.message
        ):
            ev = self._last_error
            head = ev.error_type or "Flash failed"
            text.append(head, style=COLORS["red"])
            if ev.message:
                text.append(f": {ev.message}", style=COLORS["red"])
            if ev.context:
                for key, value in ev.context.items():
                    text.append(f"\n    {key}: {value}", style=COLORS["subtle"])
            if ev.recovery:
                text.append("\n")
                text.append(ev.recovery, style=COLORS["subtle"])
        else:
            detail = f": {message.error}" if message.error is not None else ""
            text.append(f"Flash failed{detail}", style=COLORS["red"])
        return text

    # -- return ---------------------------------------------------------- #
    def action_return_dashboard(self) -> None:
        # Failure hold: ignore the return key until the job has completed, so a
        # stray keypress never abandons a flash mid-critical-section.
        if not self._done:
            return
        self.dismiss(None)

    # -- helpers --------------------------------------------------------- #
    def _header_text(self) -> Text:
        if self._mode == "all":
            total = self._device_total or "?"
            index = self._device_index or 0
            text = Text()
            text.append("Flash All  ", style=COLORS["header"])
            text.append(f"device {index}/{total}", style=COLORS["value"])
            if self._current_device:
                text.append(f"  {self._current_device}", style=COLORS["text"])
            return text
        return Text.assemble(
            ("Flashing  ", COLORS["header"]),
            (self._title, COLORS["value"]),
        )

    def _footer_text(self) -> Text:
        return Text("Flash in progress...", style=COLORS["subtle"])
