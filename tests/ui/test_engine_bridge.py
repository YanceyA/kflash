"""Tests for the engine<->UI bridge (kflash.ui.engine_bridge).

Exercised through Textual's ``App.run_test()`` + Pilot, driving the bridge from
fake engine worker threads exactly as the real engine would.

The scenarios are ``async``; each is wrapped in ``asyncio.run`` from a plain
sync test so the suite needs no ``pytest-asyncio`` plugin (it is not a project
dependency).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable
from contextlib import contextmanager
from typing import Callable

from textual.app import App

from kflash.decisions import (
    ChooseDeviceDecision,
    ConfirmDecision,
    DeviceChoice,
    TextPromptDecision,
)
from kflash.events import Emitter, FlashEvent
from kflash.ui.engine_bridge import (
    ChoiceModal,
    ConfirmModal,
    DecisionCancelled,
    EngineBridge,
    EngineBusyError,
    EngineEvent,
    EngineJobCompleted,
    TextPromptModal,
    UiDecisionProvider,
    UiEventSink,
)


def run_async(coro_factory: Callable[[], Awaitable[None]]) -> None:
    """Run an async scenario without pytest-asyncio."""
    asyncio.run(coro_factory())


class BridgeTestApp(App[None]):
    """Minimal app that records the bridge messages it receives."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[FlashEvent] = []
        self.completions: list[EngineJobCompleted] = []

    def on_engine_event(self, message: EngineEvent) -> None:
        self.events.append(message.event)

    def on_engine_job_completed(self, message: EngineJobCompleted) -> None:
        self.completions.append(message)


async def _wait_for(pilot, predicate, timeout: float = 5.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or predicate}")
        await pilot.pause()


# --------------------------------------------------------------------------- #
# 1. Event adapter: order under load
# --------------------------------------------------------------------------- #
def test_events_arrive_in_order_under_load() -> None:
    async def scenario() -> None:
        app = BridgeTestApp()
        async with app.run_test() as pilot:
            sink = UiEventSink(app)
            count = 500

            def emit_many() -> None:
                emitter = Emitter(sink)
                for i in range(count):
                    emitter.info("load", str(i))

            thread = threading.Thread(target=emit_many, daemon=False)
            thread.start()
            thread.join()

            await _wait_for(
                pilot, lambda: len(app.events) == count, what="all events delivered"
            )

        assert [e.message for e in app.events] == [str(i) for i in range(count)]
        assert all(e.kind == "info" for e in app.events)

    run_async(scenario)


# --------------------------------------------------------------------------- #
# 2. Decision round-trips
# --------------------------------------------------------------------------- #
def test_confirm_round_trip() -> None:
    async def scenario() -> None:
        app = BridgeTestApp()
        async with app.run_test() as pilot:
            bridge = EngineBridge(app)

            def job() -> bool:
                return bridge.decisions.confirm(
                    ConfirmDecision(id="flash", message="Flash?", default=False)
                )

            thread = bridge.run_engine_job(job)
            await _wait_for(
                pilot,
                lambda: isinstance(app.screen, ConfirmModal),
                what="confirm modal",
            )
            await pilot.press("y")
            await _wait_for(pilot, lambda: app.completions, what="completion")

        thread.join(5)
        completed = app.completions[-1]
        assert completed.ok
        assert completed.result is True

    run_async(scenario)


def test_confirm_modal_enter_honours_request_default() -> None:
    """A ``[y/N]`` prompt (default False) must NOT confirm on Enter, and a
    ``[Y/n]`` prompt (default True) must. Regression: the modal used to ignore
    ``default`` and always answer Yes on Enter (a silent ``[y/N]``->``[Y/n]``)."""

    async def scenario(default: bool) -> bool:
        app = BridgeTestApp()
        holder: dict = {}
        async with app.run_test() as pilot:
            bridge = EngineBridge(app)
            holder["bridge"] = bridge

            def job() -> bool:
                return bridge.decisions.confirm(
                    ConfirmDecision(id="x", message="Proceed?", default=default)
                )

            thread = bridge.run_engine_job(job)
            await _wait_for(
                pilot, lambda: isinstance(app.screen, ConfirmModal), what="modal"
            )
            # User just presses Enter -> the answer must equal the request default.
            await pilot.press("enter")
            await _wait_for(pilot, lambda: app.completions, what="completion")
        thread.join(5)
        return app.completions[-1].result

    assert run_async_result(lambda: scenario(False)) is False
    assert run_async_result(lambda: scenario(True)) is True


def run_async_result(coro_factory):
    return asyncio.run(coro_factory())


def test_choice_round_trip_returns_selected_key() -> None:
    async def scenario() -> None:
        app = BridgeTestApp()
        async with app.run_test() as pilot:
            bridge = EngineBridge(app)

            def job():
                return bridge.decisions.choose_device(
                    ChooseDeviceDecision(
                        prompt="Pick",
                        choices=[
                            DeviceChoice(key="alpha", label="Alpha"),
                            DeviceChoice(key="beta", label="Beta"),
                        ],
                    )
                )

            thread = bridge.run_engine_job(job)
            await _wait_for(
                pilot, lambda: isinstance(app.screen, ChoiceModal), what="choice modal"
            )
            await pilot.press("2")
            await _wait_for(pilot, lambda: app.completions, what="completion")

        thread.join(5)
        assert app.completions[-1].result == "beta"

    run_async(scenario)


def test_decisions_work_while_events_flow() -> None:
    async def scenario() -> None:
        app = BridgeTestApp()
        async with app.run_test() as pilot:
            bridge = EngineBridge(app)

            def job():
                emitter = Emitter(bridge.events)
                for i in range(5):
                    emitter.info("pre", str(i))
                answer = bridge.decisions.confirm(
                    ConfirmDecision(id="x", message="Continue?", default=False)
                )
                for i in range(5):
                    emitter.info("post", str(i))
                return answer

            thread = bridge.run_engine_job(job)
            # Events emitted before the decision must already be flowing.
            await _wait_for(pilot, lambda: len(app.events) >= 5, what="pre events")
            await _wait_for(
                pilot, lambda: isinstance(app.screen, ConfirmModal), what="modal"
            )
            await pilot.press("y")
            await _wait_for(pilot, lambda: app.completions, what="completion")
            await _wait_for(pilot, lambda: len(app.events) == 10, what="post events")

        thread.join(5)
        assert app.completions[-1].result is True
        assert [e.section for e in app.events] == ["pre"] * 5 + ["post"] * 5

    run_async(scenario)


# --------------------------------------------------------------------------- #
# 3. Cancellation
# --------------------------------------------------------------------------- #
def test_cancellation_unblocks_worker_and_runs_finally() -> None:
    async def scenario() -> None:
        app = BridgeTestApp()
        finally_ran = threading.Event()
        captured: dict = {}
        holder: dict = {}

        def job() -> object:
            try:
                return holder["bridge"].decisions.prompt_text(
                    TextPromptDecision(message="name?", required=True)
                )
            except BaseException as exc:  # noqa: BLE001 -- record for the assertion
                captured["exc"] = exc
                raise
            finally:
                finally_ran.set()

        async with app.run_test() as pilot:
            bridge = EngineBridge(app)
            holder["bridge"] = bridge
            thread = bridge.run_engine_job(job)
            await _wait_for(
                pilot,
                lambda: isinstance(app.screen, TextPromptModal),
                what="prompt modal",
            )
            # Leave the context -> the app tears down while the worker is blocked.

        # The worker must unblock on its own (no one called shutdown()).
        thread.join(5)
        assert not thread.is_alive(), "worker stayed blocked after app teardown"
        assert finally_ran.is_set(), "job finally block did not run"
        assert isinstance(captured.get("exc"), DecisionCancelled)
        # DecisionCancelled is a KeyboardInterrupt so it unwinds the flash path.
        assert isinstance(captured.get("exc"), KeyboardInterrupt)

    run_async(scenario)


def test_shutdown_releases_blocked_worker() -> None:
    async def scenario() -> None:
        app = BridgeTestApp()
        finally_ran = threading.Event()
        holder: dict = {}

        def job() -> object:
            try:
                return holder["bridge"].decisions.confirm(
                    ConfirmDecision(id="x", message="?", default=False)
                )
            finally:
                finally_ran.set()

        async with app.run_test() as pilot:
            bridge = EngineBridge(app)
            holder["bridge"] = bridge
            thread = bridge.run_engine_job(job)
            await _wait_for(
                pilot, lambda: isinstance(app.screen, ConfirmModal), what="modal"
            )
            # Explicitly release the blocked worker while the app still runs.
            bridge.shutdown(timeout=5)
            await _wait_for(pilot, lambda: not thread.is_alive(), what="worker exit")

        assert finally_ran.is_set()

    run_async(scenario)


# --------------------------------------------------------------------------- #
# 3b. Teardown race: worker wedged in the submit->drain window (BACKLOG)
# --------------------------------------------------------------------------- #
class _DummyModal:
    """Stands in for a pushed ModalScreen; never dismisses on its own."""

    is_running = True

    def dismiss(self, _value=None) -> None:  # pragma: no cover - abort hygiene
        pass


def test_close_unblocks_worker_wedged_in_submit_window() -> None:
    """Regression for the narrow teardown race.

    The old provider pushed the modal with ``call_from_thread``, which blocks on
    ``future.result()`` with no timeout. If the loop stops (or the modal push
    stalls) in the window after the coroutine is submitted but before it drains,
    the worker wedged forever. The hardened provider polls the submit future and
    unblocks on ``close()``.

    Simulated closely: a real loop on its own thread, a modal factory that holds
    the loop *inside* the push (the submit->drain window is open), and a
    ``close()`` from a different thread that must release the worker with
    ``DecisionCancelled`` -- without waiting for the push to finish.
    """
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    factory_entered = threading.Event()
    factory_gate = threading.Event()

    def blocking_factory(_request):
        factory_entered.set()
        factory_gate.wait(5)  # hold the loop inside the push
        return _DummyModal()

    @contextmanager
    def _noop_context():
        yield

    class FakeApp:
        _running = True
        _loop = loop

        def _context(self):
            return _noop_context()

        def push_screen(self, screen, callback=None):
            # Never invokes the callback -> the decision stays pending, so the
            # only way the worker can unblock is the submit-window poll.
            return None

    provider = UiDecisionProvider(FakeApp(), modal_factory=blocking_factory)
    outcome: dict = {}

    def worker() -> None:
        try:
            provider.confirm(ConfirmDecision(id="x", message="?", default=False))
        except BaseException as exc:  # noqa: BLE001 -- record for the assertion
            outcome["exc"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert factory_entered.wait(5), "push coroutine never reached the loop"
    # The worker is now blocked in the submit->drain window. Close from here
    # (a different thread from the loop) -- it must unblock the worker.
    provider.close()
    thread.join(5)
    assert not thread.is_alive(), "worker stayed wedged in the submit window"
    assert isinstance(outcome.get("exc"), DecisionCancelled)
    assert isinstance(outcome.get("exc"), KeyboardInterrupt)

    # Cleanup: release the loop-held factory and stop the loop.
    factory_gate.set()
    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(5)
    loop.close()


def test_shutdown_join_timeout_is_finite_and_reported(caplog) -> None:
    """A worker that never exits must not hang shutdown() forever: the finite
    default join budget returns, logs, and leaves the (non-daemon) thread to the
    interpreter's join."""

    async def scenario() -> None:
        app = BridgeTestApp()
        release = threading.Event()
        async with app.run_test() as pilot:
            bridge = EngineBridge(app)
            thread = bridge.run_engine_job(lambda: release.wait(30))
            await _wait_for(pilot, lambda: bridge.is_busy, what="busy")
            # Tiny finite timeout: shutdown must give up quickly, not block 30s.
            start = time.monotonic()
            with caplog.at_level("WARNING"):
                bridge.shutdown(timeout=0.2)
            elapsed = time.monotonic() - start
            assert elapsed < 5.0, "shutdown() did not honour the finite timeout"
            assert thread.is_alive(), "job should still be running after timeout"
            assert any(
                "did not exit within" in rec.getMessage() for rec in caplog.records
            )
            release.set()
            await _wait_for(pilot, lambda: not thread.is_alive(), what="exit")

    run_async(scenario)


# --------------------------------------------------------------------------- #
# 4. Completion + exception paths
# --------------------------------------------------------------------------- #
def test_job_completion_message() -> None:
    async def scenario() -> None:
        app = BridgeTestApp()
        async with app.run_test() as pilot:
            bridge = EngineBridge(app)
            thread = bridge.run_engine_job(lambda: 0)
            await _wait_for(pilot, lambda: app.completions, what="completion")

        thread.join(5)
        completed = app.completions[-1]
        assert completed.ok
        assert completed.result == 0
        assert completed.error is None
        assert completed.cancelled is False

    run_async(scenario)


def test_job_exception_message() -> None:
    async def scenario() -> None:
        app = BridgeTestApp()

        def boom() -> int:
            raise ValueError("kaboom")

        async with app.run_test() as pilot:
            bridge = EngineBridge(app)
            thread = bridge.run_engine_job(boom)
            await _wait_for(pilot, lambda: app.completions, what="completion")

        thread.join(5)
        completed = app.completions[-1]
        assert not completed.ok
        assert completed.cancelled is False
        assert isinstance(completed.error, ValueError)
        assert str(completed.error) == "kaboom"

    run_async(scenario)


# --------------------------------------------------------------------------- #
# 5. One job at a time
# --------------------------------------------------------------------------- #
def test_second_job_while_running_is_rejected() -> None:
    async def scenario() -> None:
        app = BridgeTestApp()
        release = threading.Event()
        async with app.run_test() as pilot:
            bridge = EngineBridge(app)

            def slow_job() -> int:
                release.wait(5)
                return 0

            thread = bridge.run_engine_job(slow_job)
            await _wait_for(pilot, lambda: bridge.is_busy, what="busy")

            raised = False
            try:
                bridge.run_engine_job(lambda: 1)
            except EngineBusyError:
                raised = True
            assert raised, "second concurrent job was not rejected"

            release.set()
            await _wait_for(pilot, lambda: app.completions, what="completion")

        thread.join(5)
        assert not bridge.is_busy
        # Exactly one job ran to completion.
        assert len(app.completions) == 1
        assert app.completions[-1].result == 0

    run_async(scenario)


# --------------------------------------------------------------------------- #
# 6. Printer safety: non-daemon thread + finally on teardown mid-job
# --------------------------------------------------------------------------- #
def test_engine_thread_is_non_daemon() -> None:
    async def scenario() -> None:
        app = BridgeTestApp()
        release = threading.Event()
        async with app.run_test() as pilot:
            bridge = EngineBridge(app)
            thread = bridge.run_engine_job(lambda: release.wait(5))
            # Governing spec (tests/test_worker_thread_signals.py): a daemon
            # engine thread dies with the process and the Klipper restart never
            # runs. The bridge MUST use a non-daemon thread.
            assert thread.daemon is False
            release.set()
            await _wait_for(pilot, lambda: app.completions, what="completion")
        thread.join(5)

    run_async(scenario)


def test_job_finally_runs_when_app_torn_down_mid_job() -> None:
    async def scenario() -> None:
        app = BridgeTestApp()
        finally_ran = threading.Event()
        in_critical_section = threading.Event()
        release = threading.Event()

        def job() -> int:
            try:
                # Emulate an engine job mid-flash (inside klipper_service_stopped):
                # it holds a critical section its finally must clean up.
                in_critical_section.set()
                release.wait(5)
                return 0
            finally:
                finally_ran.set()

        async with app.run_test() as pilot:
            bridge = EngineBridge(app)
            thread = bridge.run_engine_job(job)
            await _wait_for(
                pilot, lambda: in_critical_section.is_set(), what="critical section"
            )
            # App tears down while the job is still running.

        # The non-daemon thread outlives the app; let it finish, prove finally ran.
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert finally_ran.is_set()

    run_async(scenario)


# --------------------------------------------------------------------------- #
# 7. Sanity: adapters satisfy the engine Protocols
# --------------------------------------------------------------------------- #
def test_adapters_satisfy_engine_protocols() -> None:
    from kflash.decisions import DecisionProvider
    from kflash.events import EventSink

    async def scenario() -> None:
        app = BridgeTestApp()
        async with app.run_test():
            bridge = EngineBridge(app)
            sink: EventSink = bridge.events
            provider: DecisionProvider = bridge.decisions
            assert sink is not None and provider is not None

    run_async(scenario)


if __name__ == "__main__":  # pragma: no cover
    pass
