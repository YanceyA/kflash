"""Engine event stream: a UI-free structured output channel.

Two responsibilities live here:

* :class:`FlashEvent` -- one frozen dataclass discriminated by ``kind`` that
  captures every message the engine wants to surface. Every ``kind`` maps to a
  real emit site that exists today (see the Phase 2 design ``B.1``).
* :class:`Emitter` -- a convenience facade whose method surface mirrors the
  non-interactive part of the retired legacy ``Output`` protocol, so the
  extracted call sites read exactly as they did before the seam existed
  (``out.phase(...)`` became ``em.phase(...)``).

The engine holds an :class:`Emitter` and the concrete rendering is done by an
:class:`EventSink` -- in production that is the Textual UI's bridge sink
(:mod:`kflash.ui.engine_bridge`), which forwards each event to the operation
screen; :class:`NullSink`/:class:`TeeSink` serve silent/fan-out composition
(e.g. a future persistent-log or headless JSON sink).

Imports only stdlib. Deliberately imports NO UI module so any engine module
may depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

# Closed phase vocabulary -- every value already appears as an out.phase(...)
# label today.
PHASES = (
    "preflight",
    "discovery",
    "safety",
    "version",
    "config",
    "build",
    "bootloader",
    "flash",
    "verify",
    "service",
    "summary",
)

# Closed kind vocabulary.
#   Tier 1 (message) kinds map 1:1 onto the retired Output protocol's methods
#   (the operation screen's render_event covers all of them).
#   Tier 2 (lifecycle) kinds carry structured step/progress data.
KINDS = (
    # Tier 1 -- message kinds
    "info",
    "success",
    "warn",
    "error",
    "error_recovery",
    "phase",
    "device_line",
    "step_divider",
    "device_divider",
    # Tier 2 -- lifecycle.
    #   "progress" IS emitted in production: wait_for_device streams inline
    #   verify dots through it (the operation screen renders them as liveness;
    #   a headless sink may ignore them).
    #   "step_start"/"step_end" carry the phase checklist: run_flash_sequence
    #   and load_and_validate_config emit them with device_key, and the
    #   operation screen drives its checklist + timers from them.
    "step_start",
    "step_end",
    "progress",
)


@dataclass(frozen=True)
class FlashEvent:
    """A single structured engine event.

    Discriminated by :attr:`kind` (+ :attr:`phase`). A single class -- rather
    than a class-per-variant -- keeps stdlib JSON serialization trivial and
    lets sinks pattern-match on ``.kind``.
    """

    kind: str
    phase: str = "flash"
    message: str = ""
    section: Optional[str] = None  # out.info section label / original phase label
    # error_recovery payload
    error_type: Optional[str] = None
    recovery: Optional[str] = None
    context: Optional[dict[str, str]] = None
    # device_line payload
    marker: Optional[str] = None
    name: Optional[str] = None
    detail: Optional[str] = None
    # device_divider payload
    index: Optional[int] = None
    total: Optional[int] = None
    # device identity / lifecycle payload.
    # FORWARD SEAM (Phase 3): these fields are populated only by the Tier-2
    # lifecycle kinds above and are currently exercised only by tests. They are
    # kept so the Phase-3 operation screen / headless JSON mode can render
    # per-device progress and timing without touching the engine.
    device_key: Optional[str] = None
    device_name: Optional[str] = None
    elapsed: Optional[float] = None  # step_end / progress
    progress: Optional[float] = None  # progress (0..1)


class EventSink(Protocol):
    """Anything that can render/consume a :class:`FlashEvent`."""

    def emit(self, event: FlashEvent) -> None: ...


class NullSink:
    """An :class:`EventSink` that discards every event.

    Used as the silent default for engine entry points that may run without a
    configured sink (so they can hold a real :class:`Emitter` instead of
    branching on ``None`` and falling back to ``print()``).
    """

    def emit(self, event: FlashEvent) -> None:
        pass


class Emitter:
    """Convenience facade the engine holds.

    The method surface mirrors the retired legacy ``Output`` protocol's
    non-interactive API so the extracted call sites read exactly as they did
    (only the variable name changed: ``out.phase(...)`` -> ``em.phase(...)``).
    Each method builds a :class:`FlashEvent` and forwards it to the wrapped
    sink.
    """

    def __init__(self, sink: EventSink) -> None:
        self._sink = sink

    # --- Tier 1: 1:1 with Output ---------------------------------------
    def info(
        self,
        section: str,
        message: str,
        *,
        device_key: Optional[str] = None,
        device_name: Optional[str] = None,
        marker: Optional[str] = None,
        elapsed: Optional[float] = None,
    ) -> None:
        # The optional device_* / marker / elapsed fields let the batch summary
        # carry a structured per-device result (device_key + PASS/FAIL marker +
        # duration) that the operation screen renders as a results-table row.
        # A plain-text sink reads only section+message, so an enriched info()
        # call renders identically to a plain one.
        self._sink.emit(
            FlashEvent(
                "info",
                section=section,
                message=message,
                device_key=device_key,
                device_name=device_name,
                marker=marker,
                elapsed=elapsed,
            )
        )

    def success(self, message: str) -> None:
        self._sink.emit(FlashEvent("success", message=message))

    def warn(self, message: str) -> None:
        self._sink.emit(FlashEvent("warn", message=message))

    def error(self, message: str) -> None:
        self._sink.emit(FlashEvent("error", message=message))

    def error_with_recovery(
        self,
        error_type: str,
        message: str,
        context: Optional[dict[str, str]] = None,
        recovery: Optional[str] = None,
    ) -> None:
        self._sink.emit(
            FlashEvent(
                "error_recovery",
                message=message,
                error_type=error_type,
                context=context,
                recovery=recovery,
            )
        )

    def device_line(self, marker: str, name: str, detail: str) -> None:
        self._sink.emit(
            FlashEvent("device_line", marker=marker, name=name, detail=detail)
        )

    def phase(self, phase_name: str, message: str) -> None:
        parts = phase_name.lower().split()
        phase = parts[0] if parts else "flash"
        self._sink.emit(
            FlashEvent("phase", phase=phase, message=message, section=phase_name)
        )

    def step_divider(self) -> None:
        self._sink.emit(FlashEvent("step_divider"))

    def device_divider(self, index: int, total: int, name: str) -> None:
        self._sink.emit(
            FlashEvent("device_divider", index=index, total=total, name=name)
        )

    # --- Tier 2: lifecycle helpers used by run_flash_sequence -----------
    def step_start(
        self, phase: str, message: str, device_key: Optional[str] = None
    ) -> None:
        self._sink.emit(
            FlashEvent(
                "step_start",
                phase=(phase.lower().split() or ["flash"])[0],
                section=phase,
                message=message,
                device_key=device_key,
            )
        )

    def step_end(
        self,
        phase: str,
        message: str,
        elapsed: Optional[float] = None,
        device_key: Optional[str] = None,
    ) -> None:
        self._sink.emit(
            FlashEvent(
                "step_end",
                phase=(phase.lower().split() or ["flash"])[0],
                section=phase,
                message=message,
                elapsed=elapsed,
                device_key=device_key,
            )
        )

    def progress(
        self,
        phase: str,
        message: str,
        progress: Optional[float] = None,
        elapsed: Optional[float] = None,
    ) -> None:
        self._sink.emit(
            FlashEvent(
                "progress",
                phase=(phase.lower().split() or ["flash"])[0],
                section=phase,
                message=message,
                progress=progress,
                elapsed=elapsed,
            )
        )


class TeeSink:
    """Fan-out sink.

    THE single attach point the future log/JSON sink plugs into: a
    ``FileLogSink`` / ``JsonlSink`` / operation-screen sink is added here
    without touching the engine.
    """

    def __init__(self, sinks: list[EventSink]) -> None:
        self._sinks = list(sinks)

    def emit(self, event: FlashEvent) -> None:
        for sink in self._sinks:
            sink.emit(event)
