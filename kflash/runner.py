"""Single subprocess seam for the engine.

Every external command is one of three shapes:

* captured text (``run``): ``capture_output=True, text=True``, output
  available only after the process exits;
* interactive TTY (``run_interactive``): inherited stdin/stdout/stderr, for
  menuconfig's ncurses UI;
* live line-streamed (``run_streaming_lines``): captured stdout/stderr piped
  and delivered line-by-line to an ``on_line`` callback as they arrive, for
  build/flash output relayed through the engine event stream.

The single sanctioned ``subprocess.Popen`` in the codebase lives inside
``run_streaming_lines`` -- it is needed to stream output line-by-line while
the process is still running (the other two shapes remain
``subprocess.run``, which only yields output after the process exits).

Engine modules call the module-level free functions (``runner.run(...)`` etc.)
which delegate to a swappable ``_active`` :class:`Runner`. Tests inject a
``FakeRunner`` via ``set_runner`` / ``monkeypatch``.

Imports only stdlib.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable, Optional, Protocol


@dataclass
class CommandResult:
    """Outcome of a captured subprocess call."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class Runner(Protocol):
    """The subprocess seam.

    Timeout contract (deliberately divergent -- callers passing ``timeout=``
    MUST handle both shapes):

    * :meth:`run` and :meth:`run_streaming_lines` NEVER raise
      :class:`subprocess.TimeoutExpired`; on timeout they return a
      :class:`CommandResult` with ``timed_out=True`` (``run`` also carries any
      captured partial output, decoded to text; ``run_streaming_lines``
      likewise carries whatever lines were collected before the timeout).
    * :meth:`run_interactive` DOES raise :class:`subprocess.TimeoutExpired` --
      its ``int`` return value cannot carry a timeout flag, so the exception is
      the only channel. Callers that pass ``timeout=`` must catch it.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        input: Optional[str] = None,
        text: bool = True,
    ) -> CommandResult: ...

    def run_interactive(
        self,
        argv: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> int: ...

    def run_streaming_lines(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        on_line: Callable[[str], None],
    ) -> CommandResult: ...


class SubprocessRunner:
    """Default :class:`Runner` backed by :mod:`subprocess`.

    Honours the divergent timeout contract documented on :class:`Runner`:
    :meth:`run` swallows :class:`subprocess.TimeoutExpired` and returns
    ``timed_out=True``, while :meth:`run_interactive` lets it propagate (an
    ``int`` return can't carry a timeout flag).

    :meth:`run_streaming_lines` additionally guarantees the child process is
    killed and reaped (a bounded wait, never blocking indefinitely) on every
    exit path -- normal completion, timeout, or an exception raised from
    ``on_line`` (which still propagates to the caller after cleanup).
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        input: Optional[str] = None,
        text: bool = True,
    ) -> CommandResult:
        try:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                text=text,
                timeout=timeout,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                input=input,
            )
        except subprocess.TimeoutExpired as exc:
            # Decode partial output; build.py relies on this to show a tail
            # even when a build times out.
            out = _coerce_text(exc.stdout, text)
            err = _coerce_text(exc.stderr, text)
            return CommandResult(returncode=-1, stdout=out, stderr=err, timed_out=True)
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout if proc.stdout is not None else "",
            stderr=proc.stderr if proc.stderr is not None else "",
        )

    def run_interactive(
        self,
        argv: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> int:
        proc = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            timeout=timeout,
        )
        return proc.returncode

    def run_streaming_lines(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        on_line: Callable[[str], None],
    ) -> CommandResult:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=dict(env) if env is not None else None,
        )

        q: queue.Queue[Optional[str]] = queue.Queue()

        def _reader() -> None:
            # The None sentinel must always be pushed, even if iterating
            # proc.stdout raises, or the caller's loop would degrade to a
            # full-timeout wait and mask the real result.
            try:
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    q.put(raw_line)
            finally:
                q.put(None)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        collected: list[str] = []
        deadline = time.monotonic() + timeout
        eof_seen = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Final non-blocking drain so lines already buffered by
                    # the reader thread aren't dropped from the timeout result.
                    while True:
                        try:
                            item = q.get_nowait()
                        except queue.Empty:
                            break
                        if item is None:
                            continue
                        collected.append(item)
                        on_line(item.rstrip("\r\n"))
                    return CommandResult(
                        returncode=-1, stdout="".join(collected), timed_out=True
                    )
                try:
                    item = q.get(timeout=min(remaining, 0.25))
                except queue.Empty:
                    if eof_seen and proc.poll() is not None:
                        break
                    continue
                if item is None:
                    eof_seen = True
                    if proc.poll() is not None:
                        break
                    continue
                collected.append(item)
                on_line(item.rstrip("\r\n"))
        finally:
            # Guaranteed cleanup on every exit path (normal break, the
            # timeout `return` above, or an exception -- e.g. on_line
            # raising, or KeyboardInterrupt during q.get()). The wait is
            # bounded so this method never blocks indefinitely even if the
            # killed child is slow to die.
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        returncode = proc.returncode if proc.returncode is not None else -1
        return CommandResult(returncode=returncode, stdout="".join(collected))


def _coerce_text(value: object, text: bool) -> str:
    """Decode a possibly-bytes partial-output blob into text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


_active: Runner = SubprocessRunner()


def set_runner(runner: Runner) -> None:
    """Swap the active runner (tests use this or monkeypatch ``_active``)."""
    global _active
    _active = runner


def get_runner() -> Runner:
    return _active


def run(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    input: Optional[str] = None,
    text: bool = True,
) -> CommandResult:
    return _active.run(
        argv, timeout=timeout, cwd=cwd, env=env, input=input, text=text
    )


def run_interactive(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
) -> int:
    return _active.run_interactive(argv, cwd=cwd, env=env, timeout=timeout)


def run_streaming_lines(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    on_line: Callable[[str], None],
) -> CommandResult:
    return _active.run_streaming_lines(
        argv, timeout=timeout, cwd=cwd, env=env, on_line=on_line
    )
