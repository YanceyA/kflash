"""Pilot + snapshot tests for the R5 operation screen.

Two drive styles:

* **Interaction tests** run a synthetic ``FlashEvent`` stream through the *real*
  :class:`~kflash.ui.engine_bridge.EngineBridge` (on its non-daemon worker
  thread) into an :class:`OperationScreen`, exactly as the engine would -- so
  the checklist transitions, failure hold, and batch results table are exercised
  end to end.
* **Snapshot tests** drive ``op.ingest(...)`` directly on the UI thread with a
  frozen clock, so the SVG is deterministic (no live elapsed-timer jitter).
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import pytest
from textual.app import App
from textual.widgets import DataTable, ProgressBar, Static

from kflash import events
from kflash.events import Emitter, FlashEvent
from kflash.ui import skin
from kflash.ui.engine_bridge import EngineBridge, EngineJobCompleted
from kflash.ui.screens.operation import OperationScreen, render_event

_SIZE = (80, 40)


def _run(coro) -> None:
    asyncio.run(coro)


class OpBridgeApp(App[None]):
    """Hosts one OperationScreen fed by a real EngineBridge."""

    CSS_PATH = [skin.CSS_PATH]
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, mode: str, title: str) -> None:
        super().__init__()
        self.register_theme(skin.KFLASH_THEME)
        self.theme = skin.KFLASH_THEME_NAME
        self.animation_level = "none"
        self._mode = mode
        self._title = title
        self.operation: Optional[OperationScreen] = None
        self.bridge: Optional[EngineBridge] = None

    def on_mount(self) -> None:
        operation = OperationScreen(mode=self._mode, title=self._title)
        self.operation = operation
        self.bridge = EngineBridge(self, event_target=operation)
        self.push_screen(operation)

    def on_engine_job_completed(self, message: EngineJobCompleted) -> None:
        # Re-post onto the operation screen's own pump so completion lands after
        # every engine event (mirrors the dashboard's real routing).
        assert self.operation is not None
        self.operation.post_message(
            EngineJobCompleted(
                result=message.result,
                error=message.error,
                cancelled=message.cancelled,
            )
        )


async def _drain(pilot, app, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    # Wait for the worker to finish AND its completion message to be handled
    # (the operation screen enters its held state).
    while app.bridge.is_busy or not (app.operation and app.operation._done):
        if time.monotonic() > deadline:
            raise AssertionError("engine job did not reach the held state")
        await pilot.pause()
    await pilot.pause()


def _run_stream(app: OpBridgeApp, emit_fn) -> None:
    """Start a job that plays *emit_fn(em)* on the worker thread."""
    bridge = app.bridge
    assert bridge is not None
    emitter = Emitter(bridge.events)

    def _job() -> int:
        return emit_fn(emitter)

    bridge.run_engine_job(_job)


# --------------------------------------------------------------------------- #
# render_event: the surviving event renderer must cover the closed vocabulary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", events.KINDS)
def test_render_event_handles_every_kind(kind) -> None:
    """No FlashEvent kind may be silently unrenderable (this invariant moved
    here from the retired LegacyOutputSink's every-kind test at Stage 3).
    device_line renders from its marker/name/detail payload, not message."""
    event = FlashEvent(kind, message="m", marker="REG", name="dev", detail="d")
    text = render_event(event)
    assert text.plain, f"render_event produced nothing for kind {kind!r}"


# --------------------------------------------------------------------------- #
# Interaction tests (through the real bridge)
# --------------------------------------------------------------------------- #
def test_checklist_transitions_on_success() -> None:
    """step_start/step_end + phase events drive the checklist to done."""

    def stream(em: Emitter) -> int:
        em.step_start("Config", "Loaded cached config")
        em.step_end("Config", "MCU validated: stm32h723", elapsed=0.3)
        em.step_start("Bootloader", "Entering katapult bootloader...")
        em.step_end("Bootloader", "Entered (1.2s)", elapsed=1.2)
        em.step_start("Flash", "Flashing firmware...")
        em.step_start("Verify", "Waiting for device to reappear...")
        em.success("Flashed via katapult in 9.1s")
        return 0

    async def go() -> None:
        app = OpBridgeApp("single", "Octopus Pro")
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            _run_stream(app, stream)
            await _drain(pilot, app)
            op = app.operation
            states = {p.name: p.state for p in op._phases}
            # Config/Bootloader done via step_end; Flash done when Verify began;
            # Verify finalized to done on completion.
            assert states["Config"] == "done"
            assert states["Bootloader"] == "done"
            assert states["Flash"] == "done"
            assert states["Verify"] == "done"
            # Untouched phases stay pending.
            assert states["Discovery"] == "pending"
            assert op._done is True

    _run(go())


def test_failure_hold_requires_dismissal() -> None:
    """On failure the screen holds; the return key only works once completed."""

    def stream(em: Emitter) -> int:
        em.step_start("Bootloader", "Entering katapult bootloader...")
        em.error_with_recovery(
            "Bootloader entry failed",
            "Device did not enter bootloader",
            context={"device": "octopus"},
            recovery="1. Hold BOOT\n2. Retry",
        )
        return 1

    async def go() -> None:
        app = OpBridgeApp("single", "Octopus Pro")
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            op = app.operation
            # Return is a no-op while the job is still running.
            op.action_return_dashboard()
            assert app.screen is op

            _run_stream(app, stream)
            await _drain(pilot, app)
            # Held: still on the operation screen, Bootloader marked failed.
            assert app.screen is op
            assert op._done is True
            assert op._phase("Bootloader").state == "failed"
            # Now the return key dismisses back off the operation screen.
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen is not op

    _run(go())


def test_progress_bar_ignores_non_progress_percent_text() -> None:
    """A ccache stats line ("100% hit rate") or any other message containing a
    percentage must NOT move the bar -- only a structured FlashEvent.progress
    value may. The progress panel also stays hidden."""

    def stream(em: Emitter) -> int:
        em.phase("Build", "Cache: 236 hits, 0 misses (100% hit rate)")
        em.info("Build", "Compiling foo.c... 45%")
        return 0

    async def go() -> None:
        app = OpBridgeApp("single", "Octopus Pro")
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            _run_stream(app, stream)
            await _drain(pilot, app)
            op = app.operation
            bar = op.query_one("#op-progress", ProgressBar)
            assert bar.progress == 0
            panel = op.query_one("#op-progress-panel")
            assert panel.display is False

    _run(go())


def test_progress_bar_driven_by_structured_progress_only() -> None:
    """A real FlashEvent.progress value drives the bar and reveals the panel."""

    def stream(em: Emitter) -> int:
        em.progress("Flash", "[##] 25%", 0.25)
        return 0

    async def go() -> None:
        app = OpBridgeApp("single", "Octopus Pro")
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            _run_stream(app, stream)
            await _drain(pilot, app)
            op = app.operation
            panel = op.query_one("#op-progress-panel")
            assert panel.display is True
            bar = op.query_one("#op-progress", ProgressBar)
            assert bar.progress == 25
            label = op.query_one("#op-progress-label", Static)
            assert "Flash: 25%" in label.render().plain  # type: ignore[union-attr]

    _run(go())


def test_progress_panel_resets_between_flash_all_devices() -> None:
    """Flash All: a finished device's progress bar must not linger into the
    next device's discovery/config/build phases -- device_divider resets it."""

    async def go() -> None:
        app = OpSnapshotApp("all", "3 device(s)")
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            op = pilot.app.operation
            panel = op.query_one("#op-progress-panel")
            bar = op.query_one("#op-progress", ProgressBar)

            op.ingest(FlashEvent("progress", section="Flash", message="[##] 25%", progress=0.25))
            await pilot.pause()
            assert panel.display is True
            assert bar.progress == 25

            op.ingest(FlashEvent("device_divider", index=2, total=3, name="Dev B"))
            await pilot.pause()
            assert panel.display is False
            assert bar.progress == 0

            op.ingest(FlashEvent("progress", section="Flash", message="[##] 10%", progress=0.10))
            await pilot.pause()
            assert panel.display is True
            assert bar.progress == 10

    _run(go())


def test_batch_results_table_fills() -> None:
    """Enriched per-device info rows populate the Flash All results table."""

    def stream(em: Emitter) -> int:
        em.phase("Flash All", "Flashing 2 device(s)...")
        em.device_divider(2, 2, "Dev B")
        em.info("", "  Dev A  PASS", device_key="a", device_name="Dev A",
                marker="PASS", elapsed=3.2)
        em.info("", "  Dev B  FAIL", device_key="b", device_name="Dev B",
                marker="FAIL", elapsed=1.1)
        return 1

    async def go() -> None:
        app = OpBridgeApp("all", "2 device(s)")
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            _run_stream(app, stream)
            await _drain(pilot, app)
            op = app.operation
            table = op.query_one("#op-results", DataTable)
            assert table.row_count == 2
            assert op._results_seen == {"a", "b"}
            assert op._device_total == 2
            assert op._device_index == 2

    _run(go())


# --------------------------------------------------------------------------- #
# Snapshot tests (direct ingest, frozen clock)
# --------------------------------------------------------------------------- #
class OpSnapshotApp(App[None]):
    """Pushes an OperationScreen with a frozen clock for deterministic SVGs."""

    CSS_PATH = [skin.CSS_PATH]
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, mode: str, title: str) -> None:
        super().__init__()
        self.register_theme(skin.KFLASH_THEME)
        self.theme = skin.KFLASH_THEME_NAME
        self.animation_level = "none"
        self._mode = mode
        self._title = title
        self.operation: Optional[OperationScreen] = None

    def on_mount(self) -> None:
        operation = OperationScreen(mode=self._mode, title=self._title)
        operation._clock = lambda: 1000.0
        self.operation = operation
        self.push_screen(operation)


def test_snapshot_single_mid_build(snap_compare) -> None:
    """Config done, Build active, progress bar mid-flight."""

    async def run_before(pilot) -> None:
        await pilot.pause()
        op = pilot.app.operation
        op.ingest(FlashEvent("step_start", section="Config", message="Loaded cached config"))
        op.ingest(FlashEvent("step_end", section="Config",
                             message="MCU validated: stm32h723", elapsed=0.3))
        op.ingest(FlashEvent("phase", section="Build", message="Running make clean + make..."))
        op.ingest(FlashEvent("progress", section="Build",
                             message="Compiling... 45%", progress=0.45))
        await pilot.pause()

    assert snap_compare(
        OpSnapshotApp("single", "Octopus Pro"),
        terminal_size=_SIZE,
        run_before=run_before,
    )


def test_snapshot_single_failed_sticky(snap_compare) -> None:
    """Failed Bootloader + sticky error banner (failure hold)."""

    async def run_before(pilot) -> None:
        await pilot.pause()
        op = pilot.app.operation
        op.ingest(FlashEvent("step_start", section="Config", message="Loaded cached config"))
        op.ingest(FlashEvent("step_end", section="Config",
                             message="MCU validated: stm32h723", elapsed=0.3))
        op.ingest(FlashEvent("step_start", section="Bootloader",
                             message="Entering katapult bootloader..."))
        op.ingest(FlashEvent(
            "error_recovery",
            error_type="Bootloader entry failed",
            message="Device did not enter bootloader",
            context={"device": "octopus"},
            recovery="1. Hold BOOT while replugging\n2. Retry the flash",
        ))
        op.job_completed(EngineJobCompleted(result=1))
        await pilot.pause()

    assert snap_compare(
        OpSnapshotApp("single", "Octopus Pro"),
        terminal_size=_SIZE,
        run_before=run_before,
    )


def test_snapshot_flash_all_results(snap_compare) -> None:
    """Flash All results table as the final view."""

    async def run_before(pilot) -> None:
        await pilot.pause()
        op = pilot.app.operation
        op.ingest(FlashEvent("phase", section="Flash All", message="Flashing 2 device(s)..."))
        op.ingest(FlashEvent("device_divider", index=2, total=2, name="Toolhead"))
        op.ingest(FlashEvent("info", message="  Mainboard  PASS",
                             device_key="mainboard", device_name="Mainboard",
                             marker="PASS", elapsed=8.4))
        op.ingest(FlashEvent("info", message="  Toolhead  FAIL",
                             device_key="toolhead", device_name="Toolhead",
                             marker="FAIL", elapsed=2.1))
        op.job_completed(EngineJobCompleted(result=1))
        await pilot.pause()

    assert snap_compare(
        OpSnapshotApp("all", "2 device(s)"),
        terminal_size=_SIZE,
        run_before=run_before,
    )
