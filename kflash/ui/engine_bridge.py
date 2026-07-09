"""The single seam between the Textual app and the blocking, synchronous engine.

The engine (``kflash.commands.*``) is deliberately synchronous: it runs
subprocesses, sleeps, and stops/starts Klipper. It must never run on the
Textual event loop. This module provides the three adapters that let a Textual
app drive it safely from a worker thread:

* :class:`UiEventSink` -- a :class:`kflash.events.EventSink` the engine emits to
  from its worker thread. Each :class:`~kflash.events.FlashEvent` is wrapped in
  an :class:`EngineEvent` message and delivered to a Textual ``MessagePump``
  (the app or a screen) via the thread-safe
  :meth:`~textual.message_pump.MessagePump.post_message`. It does no filtering
  or rendering -- screens decide what to do with each kind.
* :class:`UiDecisionProvider` -- a :class:`kflash.decisions.DecisionProvider`
  called *on the engine worker thread*. Each request pushes a modal onto the
  app and blocks the worker until the modal is dismissed.
* :class:`EngineBridge` -- owns the event sink + decision provider and runs an
  engine callable on a non-daemon worker thread, reporting the outcome back to
  the app as an :class:`EngineJobCompleted` message. Only one job may run at a
  time.

Allowed imports: :mod:`kflash.events`, :mod:`kflash.decisions`, ``textual`` and
the standard library. This module MUST NOT import engine internals (service,
flasher, bootloader, flash_steps, discovery, ...) -- jobs arrive as callables
composed by the caller -- nor any legacy UI module (output/tui/panels/screen/
theme/ansi). No ``print()``/``input()`` anywhere.

Thread-worker daemon semantics (why we do NOT use ``@work(thread=True)``)
------------------------------------------------------------------------
Textual 8.2.x runs a threaded worker via
``loop.run_in_executor(None, runner, self._work)`` (``textual/worker.py``, the
``_ThreadPoolExecutor`` path). That means:

* the worker runs on **asyncio's default ``ThreadPoolExecutor``** -- a thread
  this code neither creates nor can assert ``daemon`` on, and whose lifetime is
  owned by the loop's executor, not by us;
* Textual cancels a worker by setting a ``threading.Event``
  (``Worker.cancel()`` -> ``_cancelled_event``). A *blocking, synchronous*
  engine call (a ``systemctl`` subprocess, a flash write) cannot observe that
  event, so app exit races the in-flight flash -- exactly the
  "joined-with-cancel-race" failure ``tests/test_worker_thread_signals.py``
  documents.

``tests/test_worker_thread_signals.py`` is the governing spec: the engine must
run on a **non-daemon** thread so interpreter shutdown *joins* it and
``service.klipper_service_stopped``'s restart ``finally`` block always runs --
a daemon thread would die with the process and leave Klipper stopped. We
therefore run engine jobs on our own ``threading.Thread(daemon=False)`` and
integrate with Textual only through messages (:meth:`post_message`) and the
thread-bridge (:meth:`~textual.app.App.call_from_thread`).

Cancellation semantics
----------------------
:class:`DecisionCancelled` subclasses :class:`KeyboardInterrupt` on purpose.
When a worker is blocked on a decision and the app tears down (or the screen
stack is dismissed), the provider raises ``DecisionCancelled`` on the worker.
Because it *is* a ``KeyboardInterrupt`` (a ``BaseException``, not an
``Exception``), it punches through every ``except Exception`` handler in the
flash path and unwinds exactly like the engine's existing Ctrl+C path:
``flash_steps.run_flash_sequence`` explicitly "lets ``KeyboardInterrupt`` /
``SystemExit`` propagate so the caller's context manager can restart Klipper",
so ``klipper_service_stopped``'s restart ``finally`` still runs. A worker never
stays blocked forever holding the Klipper-stopped window: the blocking wait
polls for app-teardown and unblocks within ~50 ms.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from ..decisions import (
    ChooseCcacheActionDecision,
    ChooseDeviceDecision,
    ChooseFlashMethodDecision,
    ConfirmDecision,
    ManualBootloaderReadyDecision,
    McuMismatchDecision,
    TextPromptDecision,
)
from ..events import FlashEvent

if TYPE_CHECKING:
    from textual.app import App
    from textual.message_pump import MessagePump

_log = logging.getLogger(__name__)


__all__ = [
    "DecisionCancelled",
    "EngineBusyError",
    "EngineEvent",
    "EngineJobCompleted",
    "UiEventSink",
    "ConfirmModal",
    "ChoiceModal",
    "TextPromptModal",
    "default_modal_factory",
    "UiDecisionProvider",
    "EngineBridge",
]


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class DecisionCancelled(KeyboardInterrupt):
    """Raised on the engine worker thread when a decision cannot be answered
    because the app is shutting down or the screen stack was torn down.

    Subclasses :class:`KeyboardInterrupt` so it unwinds the engine's flash path
    like Ctrl+C: it is *not* caught by ``except Exception`` handlers, so every
    ``finally`` (notably ``klipper_service_stopped``'s restart) runs on the way
    out. See the module docstring.
    """


class EngineBusyError(RuntimeError):
    """Raised by :meth:`EngineBridge.run_engine_job` when a job is already
    running. Flashing is a critical section: only one engine job at a time."""


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
class EngineEvent(Message):
    """A :class:`~kflash.events.FlashEvent` delivered to the UI thread.

    Dumb transport: it carries the event unchanged. Screens subscribe with an
    ``on_engine_event(self, message: EngineEvent)`` handler and decide what to
    render from ``message.event.kind``.
    """

    __slots__ = ("event",)

    def __init__(self, event: FlashEvent) -> None:
        super().__init__()
        self.event = event


class EngineJobCompleted(Message):
    """Posted when an engine job finishes (success, exception, or cancellation).

    Exactly one of the three outcomes holds:

    * success   -> :attr:`error` is ``None`` and :attr:`cancelled` is ``False``;
      :attr:`result` is the callable's return value (typically the engine's int
      exit code).
    * cancelled -> :attr:`cancelled` is ``True`` (the worker was unblocked by
      :class:`DecisionCancelled` / ``KeyboardInterrupt`` / ``SystemExit``);
      :attr:`error` holds the raised instance.
    * failure   -> :attr:`error` holds the raised ``BaseException``.
    """

    __slots__ = ("result", "error", "cancelled")

    def __init__(
        self,
        *,
        result: Any = None,
        error: Optional[BaseException] = None,
        cancelled: bool = False,
    ) -> None:
        super().__init__()
        self.result = result
        self.error = error
        self.cancelled = cancelled

    @property
    def ok(self) -> bool:
        """True only for a clean completion (no error, not cancelled)."""
        return self.error is None and not self.cancelled


# --------------------------------------------------------------------------- #
# Event adapter
# --------------------------------------------------------------------------- #
class UiEventSink:
    """A thread-safe :class:`kflash.events.EventSink` that forwards engine
    events to a Textual ``MessagePump`` as :class:`EngineEvent` messages.

    ``post_message`` is thread-safe in Textual (it hands off to the loop via
    ``call_soon_threadsafe`` when called off-thread) and preserves order, so
    hundreds of events emitted from the engine thread arrive in order. If the
    target is closing/closed, ``post_message`` drops the message and returns
    ``False`` -- emitting during teardown is safe and never blocks the engine.

    Args:
        target: the pump that should receive the events -- usually the operation
            screen (so its ``on_engine_event`` handler renders them) or the app.
    """

    def __init__(self, target: MessagePump) -> None:
        self._target = target

    def emit(self, event: FlashEvent) -> None:
        self._target.post_message(EngineEvent(event))


# --------------------------------------------------------------------------- #
# Decision adapter -- generic placeholder modals
# --------------------------------------------------------------------------- #
# These are intentionally minimal and unstyled: the dialogs.py workstream owns
# visual design and will replace them via a custom ``modal_factory``. They exist
# so the decision seam is usable and testable today.
class ConfirmModal(ModalScreen[bool]):
    """A yes/no modal. Dismisses with ``True`` (yes) or ``False`` (no).

    The request's ``default`` is honoured exactly like the legacy
    ``Output.confirm``: an empty answer (Enter) returns ``default``, and the
    suffix reads ``[Y/n]`` when the default is yes, ``[y/N]`` when it is no. A
    ``[y/N]`` prompt must never silently confirm on Enter. ``y``/``n`` are
    explicit overrides; Escape declines (the conservative answer).

    ``AUTO_FOCUS`` is disabled (empty selector, not ``None`` -- ``None`` inherits
    the app's ``"*"``) so no button grabs Enter; the key bindings own it, which
    is what makes Enter honour the default rather than pressing the first (Yes)
    button. Buttons remain mouse-clickable.
    """

    AUTO_FOCUS = ""

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("enter", "default", "Default"),
        Binding("escape", "no", "No"),
    ]

    def __init__(self, message: str, default: bool = False) -> None:
        super().__init__()
        self._message = message
        self._default = default

    def compose(self):  # type: ignore[no-untyped-def]
        suffix = " [Y/n]" if self._default else " [y/N]"
        yield Vertical(
            Label(self._message + suffix),
            Button("Yes", id="confirm-yes"),
            Button("No", id="confirm-no"),
        )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def action_default(self) -> None:
        # Empty answer == the request default (mirrors Output.confirm).
        self.dismiss(self._default)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class ChoiceModal(ModalScreen[Optional[Any]]):
    """A list-choice modal.

    ``options`` is a list of ``(value, label)`` pairs; the modal dismisses with
    the chosen ``value`` (any object), or ``None`` if cancelled (only when
    ``allow_cancel`` is set). Number keys ``1``..``9`` select the Nth option.
    """

    def __init__(
        self,
        prompt: str,
        options: list[tuple[Any, str]],
        allow_cancel: bool = True,
    ) -> None:
        super().__init__()
        self._prompt = prompt
        self._options = list(options)
        self._allow_cancel = allow_cancel

    def compose(self):  # type: ignore[no-untyped-def]
        buttons = [
            Button(f"{index + 1}. {label}", id=f"choice-{index}")
            for index, (_value, label) in enumerate(self._options)
        ]
        yield Vertical(Label(self._prompt), *buttons)

    def _choose(self, index: int) -> None:
        if 0 <= index < len(self._options):
            self.dismiss(self._options[index][0])

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key in "123456789":
            event.stop()
            self._choose(int(event.key) - 1)
        elif event.key == "escape" and self._allow_cancel:
            event.stop()
            self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("choice-"):
            self._choose(int(button_id.split("-", 1)[1]))


class TextPromptModal(ModalScreen[Optional[str]]):
    """A single-line text prompt. Dismisses with the entered string, or ``None``
    if cancelled (only allowed when the prompt is not ``required``)."""

    def __init__(
        self, message: str, default: str = "", required: bool = False
    ) -> None:
        super().__init__()
        self._message = message
        self._default = default
        self._required = required

    def compose(self):  # type: ignore[no-untyped-def]
        yield Vertical(
            Label(self._message),
            Input(value=self._default, id="prompt-input"),
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value
        if self._required and not value:
            return
        self.dismiss(value)

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape" and not self._required:
            event.stop()
            self.dismiss(None)


# The seven typed decision requests the engine can raise.
DecisionRequest = Any


def default_modal_factory(request: DecisionRequest) -> ModalScreen:
    """Map a typed decision request to a generic placeholder modal.

    The returned modal MUST dismiss with the value the corresponding
    :class:`UiDecisionProvider` method returns:

    * :class:`~kflash.decisions.ConfirmDecision`            -> ``bool``
    * :class:`~kflash.decisions.ChooseDeviceDecision`       -> ``Optional[str]``
      (a device ``key``)
    * :class:`~kflash.decisions.ChooseFlashMethodDecision`  ->
      ``Optional[tuple[str, Optional[str]]]``
    * :class:`~kflash.decisions.ManualBootloaderReadyDecision` -> ``bool``
    * :class:`~kflash.decisions.McuMismatchDecision`        -> ``"r"|"d"|"k"``
    * :class:`~kflash.decisions.ChooseCcacheActionDecision` ->
      ``"install"|"skip"|"disable"``
    * :class:`~kflash.decisions.TextPromptDecision`         -> ``Optional[str]``

    A screen may supply its own factory with the same shape contract to replace
    these with designed dialogs.
    """
    if isinstance(request, ConfirmDecision):
        return ConfirmModal(request.message, request.default)

    if isinstance(request, ChooseDeviceDecision):
        options: list[tuple[Any, str]] = [
            (choice.key, choice.label) for choice in request.choices
        ]
        return ChoiceModal(request.prompt, options, request.allow_cancel)

    if isinstance(request, ChooseFlashMethodDecision):
        method_options: list[tuple[Any, str]] = []
        if request.current_bootloader is not None:
            method_options.append(
                (
                    (request.current_bootloader, request.current_flash_command),
                    "Keep current method"
                    + f" ({request.current_bootloader})",
                )
            )
        prompt = "Select flash method"
        if request.device_name:
            prompt += f" for {request.device_name}"
        return ChoiceModal(prompt, method_options, allow_cancel=True)

    if isinstance(request, ManualBootloaderReadyDecision):
        return ConfirmModal(
            f"Put {request.device_name} into bootloader mode, then confirm.",
            default=True,
        )

    if isinstance(request, McuMismatchDecision):
        prompt = (
            f"MCU mismatch on {request.device_name}: found "
            f"{request.actual_mcu!r}, expected {request.expected_mcu!r}."
        )
        return ChoiceModal(
            prompt,
            [
                ("r", "Reflash anyway"),
                ("d", "This is a different device"),
                ("k", "Keep existing / skip"),
            ],
            allow_cancel=False,
        )

    if isinstance(request, ChooseCcacheActionDecision):
        return ChoiceModal(
            "ccache is not installed. How should the build proceed?",
            [
                ("install", "Install ccache"),
                ("skip", "Skip ccache this time"),
                ("disable", "Disable ccache permanently"),
            ],
            allow_cancel=False,
        )

    if isinstance(request, TextPromptDecision):
        return TextPromptModal(
            request.message, request.default, request.required
        )

    raise TypeError(f"No default modal for decision request: {request!r}")


@dataclass(eq=False)  # identity-hashable so instances can live in a set
class _PendingDecision:
    """Bridges one decision round-trip between the worker and the UI thread."""

    done: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    cancelled: bool = False
    # The modal pushed for this decision, so close() can abort a dialog that is
    # still on screen (set on the UI thread in _push).
    modal: Optional[ModalScreen] = None

    def set_result(self, value: Any) -> None:
        # Runs on the UI thread as the modal's dismiss callback.
        self.value = value
        self.done.set()

    def cancel(self) -> None:
        self.cancelled = True
        self.done.set()


class UiDecisionProvider:
    """A :class:`kflash.decisions.DecisionProvider` driven by modal round-trips.

    Every method is CALLED ON THE ENGINE WORKER THREAD. It pushes a modal onto
    the app (via :meth:`~textual.app.App.call_from_thread`, so the widget is
    created and mounted on the UI thread) and then blocks the worker until the
    modal is dismissed. Meanwhile the UI loop keeps flowing -- events posted by
    the sink are processed and the modal stays interactive.

    If the app tears down while a worker is blocked, or :meth:`close` /
    :meth:`cancel_pending` is called (e.g. from the app's shutdown hook), the
    blocked worker is unblocked with :class:`DecisionCancelled`.

    Args:
        app: the running Textual app.
        modal_factory: ``request -> ModalScreen``; defaults to
            :func:`default_modal_factory`. The modal must dismiss with the value
            documented on :func:`default_modal_factory`.
    """

    # How often the blocking wait re-checks for app teardown.
    _POLL_SECONDS = 0.05

    def __init__(
        self,
        app: App,
        modal_factory: Optional[Callable[[DecisionRequest], ModalScreen]] = None,
    ) -> None:
        self._app = app
        self._factory = modal_factory or default_modal_factory
        self._lock = threading.Lock()
        self._pending: set[_PendingDecision] = set()
        self._closed = False

    # -- lifecycle ------------------------------------------------------- #
    def close(self) -> None:
        """Release every blocked worker with :class:`DecisionCancelled` and
        refuse further decisions. Idempotent; safe to call from any thread.

        Besides unblocking the worker (via ``decision.cancel()``), this also
        best-effort dismisses any modal still on screen. That matters for the
        teardown race noted in BACKLOG: a worker can be blocked *inside*
        ``call_from_thread``'s successor (the pushed-but-not-drained window) --
        cancelling the pending decision plus dropping its dialog guarantees the
        worker unblocks instead of wedging on a dialog nobody will answer.
        """
        with self._lock:
            self._closed = True
            pending = list(self._pending)
        for decision in pending:
            decision.cancel()
        self._abort_modals(pending)

    # Alias matching the mission's vocabulary.
    cancel_pending = close

    def _abort_modals(self, pending: list[_PendingDecision]) -> None:
        """Dismiss any still-mounted decision modals on the UI thread.

        Runs the dismissal on the app's event loop (dialogs must be touched on
        the UI thread). Purely best-effort: if the loop is already gone there is
        nothing on screen to clean up and the worker is unblocked regardless.
        """
        loop = getattr(self._app, "_loop", None)
        if loop is None:
            return

        def _dismiss() -> None:
            for decision in pending:
                modal = decision.modal
                if modal is None:
                    continue
                try:
                    if getattr(modal, "is_running", False):
                        modal.dismiss(None)
                except Exception:  # noqa: BLE001 -- teardown hygiene only
                    pass

        try:
            loop.call_soon_threadsafe(_dismiss)
        except RuntimeError:
            # Loop already closed -- nothing to dismiss.
            pass

    # -- engine-facing round-trip --------------------------------------- #
    def _app_running(self) -> bool:
        # Private attrs, but the only reliable teardown signal: on app exit
        # Textual sets ``_running = False`` and ``_loop = None`` (verified
        # against textual 8.2 app.py). Used only to unblock a waiting worker.
        return bool(getattr(self._app, "_running", False)) and (
            getattr(self._app, "_loop", None) is not None
        )

    def _ask(self, request: DecisionRequest) -> Any:
        """Push the modal for *request* and block the worker for the answer."""
        decision = _PendingDecision()
        with self._lock:
            if self._closed:
                raise DecisionCancelled("decision provider is closed")
            self._pending.add(decision)
        try:
            self._submit_push(request, decision)

            while not decision.done.wait(self._POLL_SECONDS):
                # Unblock even if nobody calls close(): teardown is detectable.
                if not self._app_running():
                    raise DecisionCancelled("app stopped during decision")
            # done is set. A real answer (even one delivered as the app is
            # winding down) wins over a teardown race; only an explicit cancel
            # (close()/cancel_pending()) raises here.
            if decision.cancelled:
                raise DecisionCancelled("decision cancelled during teardown")
            return decision.value
        finally:
            with self._lock:
                self._pending.discard(decision)

    def _submit_push(
        self, request: DecisionRequest, decision: _PendingDecision
    ) -> None:
        """Schedule the modal push on the UI thread, tolerant of teardown.

        ``App.call_from_thread`` submits a coroutine and then blocks on
        ``future.result()`` with **no timeout**. If the event loop stops in the
        narrow window between "coroutine submitted" and "coroutine drained", that
        wait never returns and the worker wedges forever (the teardown race in
        BACKLOG). We submit the same push coroutine ourselves via the public
        ``asyncio.run_coroutine_threadsafe`` and *poll* the returned future with
        the same cadence as the answer wait below -- so a loop that stops (or a
        ``close()``) during the submit window unblocks the worker with
        :class:`DecisionCancelled` instead of hanging.
        """
        loop = getattr(self._app, "_loop", None)
        if loop is None or not self._app_running():
            raise DecisionCancelled("app is not running")
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._push_coro(request, decision), loop
            )
        except RuntimeError as exc:
            # Loop already closed/closing.
            raise DecisionCancelled(str(exc) or "app is not running") from exc
        while True:
            try:
                future.result(self._POLL_SECONDS)
                return
            except concurrent.futures.TimeoutError:
                if self._closed or not self._app_running():
                    future.cancel()
                    raise DecisionCancelled(
                        "app stopped while pushing the decision modal"
                    ) from None
            except concurrent.futures.CancelledError as exc:
                raise DecisionCancelled("decision push was cancelled") from exc

    async def _push_coro(
        self, request: DecisionRequest, decision: _PendingDecision
    ) -> None:
        # Runs on the UI loop. ``_context`` sets the active-app/message-pump
        # ContextVars exactly like ``call_from_thread`` does, so push_screen's
        # widget mounting resolves the app correctly.
        with self._app._context():
            self._push(request, decision)

    def _push(self, request: DecisionRequest, decision: _PendingDecision) -> None:
        # Runs on the UI thread: build and mount the modal, wire its dismiss
        # result back to the worker. Record the modal so close() can abort it.
        modal = self._factory(request)
        decision.modal = modal
        self._app.push_screen(modal, decision.set_result)

    # -- DecisionProvider protocol (7 methods) -------------------------- #
    def confirm(self, req: ConfirmDecision) -> bool:
        return bool(self._ask(req))

    def choose_device(self, req: ChooseDeviceDecision) -> Optional[str]:
        result = self._ask(req)
        return None if result is None else str(result)

    def choose_flash_method(
        self, req: ChooseFlashMethodDecision
    ) -> Optional[tuple[str, Optional[str]]]:
        return self._ask(req)

    def manual_bootloader_ready(self, req: ManualBootloaderReadyDecision) -> bool:
        return bool(self._ask(req))

    def mcu_mismatch(self, req: McuMismatchDecision) -> str:
        result = self._ask(req)
        return result if result in ("r", "d", "k") else "k"

    def choose_ccache_action(self, req: ChooseCcacheActionDecision) -> str:
        result = self._ask(req)
        return result if result in ("install", "skip", "disable") else "skip"

    def prompt_text(self, req: TextPromptDecision) -> Optional[str]:
        result = self._ask(req)
        return None if result is None else str(result)


# --------------------------------------------------------------------------- #
# Job runner
# --------------------------------------------------------------------------- #
class EngineBridge:
    """Owns the event/decision adapters and runs engine jobs on a worker thread.

    Typical use from a screen::

        bridge = EngineBridge(app, event_target=self)  # events -> this screen
        job = functools.partial(
            cmd_flash, registry, key,
            Emitter(bridge.events), bridge.decisions,
        )
        bridge.run_engine_job(job)          # non-blocking; returns the Thread
        # ... later, in the screen:
        def on_engine_job_completed(self, message: EngineJobCompleted): ...

    Args:
        app: the running Textual app.
        event_target: pump that should receive :class:`EngineEvent` messages
            (defaults to ``app``). Completion messages always go to ``app``.
        modal_factory: passed through to :class:`UiDecisionProvider`.
    """

    def __init__(
        self,
        app: App,
        *,
        event_target: Optional[MessagePump] = None,
        modal_factory: Optional[Callable[[DecisionRequest], ModalScreen]] = None,
    ) -> None:
        self._app = app
        self.events = UiEventSink(event_target if event_target is not None else app)
        self.decisions = UiDecisionProvider(app, modal_factory=modal_factory)
        self._job_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def is_busy(self) -> bool:
        """True while an engine job thread is alive."""
        with self._job_lock:
            return self._thread is not None and self._thread.is_alive()

    def run_engine_job(
        self,
        job: Callable[[], Any],
        *,
        name: str = "kflash-engine-job",
    ) -> threading.Thread:
        """Run *job* on a fresh non-daemon thread; report completion to the app.

        *job* is a zero-argument callable (compose it with ``functools.partial``
        over a ``kflash.commands`` entry point plus an ``Emitter(self.events)``
        and ``self.decisions``). Its return value / exception is delivered to
        the app as an :class:`EngineJobCompleted` message.

        Returns the started :class:`threading.Thread` (non-daemon, so
        interpreter shutdown joins it and the Klipper-restart ``finally`` always
        runs -- see the module docstring).

        Raises:
            EngineBusyError: if a job is already running. Flashing is a critical
                section; only one job runs at a time.
        """
        with self._job_lock:
            if self._thread is not None and self._thread.is_alive():
                raise EngineBusyError("An engine job is already running")
            thread = threading.Thread(
                target=self._run, args=(job,), name=name, daemon=False
            )
            self._thread = thread
        thread.start()
        return thread

    def _run(self, job: Callable[[], Any]) -> None:
        try:
            result = job()
        except (KeyboardInterrupt, SystemExit) as exc:
            # DecisionCancelled is a KeyboardInterrupt subclass, so cancellation
            # lands here too.
            self._post(EngineJobCompleted(cancelled=True, error=exc))
        except BaseException as exc:  # noqa: BLE001 -- report every failure
            self._post(EngineJobCompleted(error=exc))
        else:
            self._post(EngineJobCompleted(result=result))

    def _post(self, message: Message) -> None:
        # Thread-safe; returns False (dropped) if the app is already closing.
        self._app.post_message(message)

    #: Default join budget for :meth:`shutdown`. Generous on purpose: a flash is
    #: a critical section that can legitimately take minutes (build + katapult +
    #: verify + Klipper restart), and we must not tear the thread out from under
    #: it. But it is *finite* -- a wedged worker must never hang interpreter exit
    #: forever (the old ``timeout=None`` default could). On expiry we log and
    #: return; the non-daemon thread is still joined at interpreter shutdown.
    DEFAULT_SHUTDOWN_TIMEOUT = 300.0

    def shutdown(
        self, timeout: Optional[float] = DEFAULT_SHUTDOWN_TIMEOUT
    ) -> None:
        """Release any worker blocked on a decision and join the job thread.

        Call from the app's exit path (e.g. ``on_unmount``) so a torn-down
        screen stack never leaves a worker blocked, and so the non-daemon engine
        thread is joined before the process exits.

        ``timeout`` is the join budget in seconds (default
        :data:`DEFAULT_SHUTDOWN_TIMEOUT`, ~5 min -- long enough for a real
        in-flight flash to finish, short enough that a genuinely wedged worker
        cannot hang forever). Pass ``None`` to block indefinitely (not
        recommended) or a small value in tests. On timeout we log rather than
        wedge; the thread is non-daemon so interpreter shutdown still joins it.
        """
        self.decisions.close()
        with self._job_lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                _log.warning(
                    "engine job thread %r did not exit within %s s of "
                    "shutdown(); leaving it to the interpreter's non-daemon "
                    "join so the Klipper-restart finally still runs",
                    thread.name,
                    timeout,
                )
