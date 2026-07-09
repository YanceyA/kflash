"""Single subprocess seam for the engine.

There is no ``subprocess.Popen`` anywhere in the codebase -- every external
command is a ``subprocess.run`` in one of three shapes:

* captured text (``run``): ``capture_output=True, text=True``;
* inherited-stdio live output (``run_streaming``): no capture, returncode only;
* interactive TTY (``run_interactive``): inherited stdin/stdout/stderr.

Engine modules call the module-level free functions (``runner.run(...)`` etc.)
which delegate to a swappable ``_active`` :class:`Runner`. Tests inject a
``FakeRunner`` via ``set_runner`` / ``monkeypatch``.

Imports only stdlib.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Optional, Protocol


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

    * :meth:`run` and :meth:`run_streaming` NEVER raise
      :class:`subprocess.TimeoutExpired`; on timeout they return a
      :class:`CommandResult` with ``timed_out=True`` (``run`` also carries any
      captured partial output, decoded to text).
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

    def run_streaming(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> CommandResult: ...

    def run_interactive(
        self,
        argv: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> int: ...


class SubprocessRunner:
    """Default :class:`Runner` backed by :mod:`subprocess`.

    Honours the divergent timeout contract documented on :class:`Runner`:
    :meth:`run` / :meth:`run_streaming` swallow
    :class:`subprocess.TimeoutExpired` and return ``timed_out=True``, while
    :meth:`run_interactive` lets it propagate (an ``int`` return can't carry a
    timeout flag).
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

    def run_streaming(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> CommandResult:
        try:
            proc = subprocess.run(
                list(argv),
                timeout=timeout,
                cwd=cwd,
                env=dict(env) if env is not None else None,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(returncode=-1, timed_out=True)
        return CommandResult(returncode=proc.returncode)

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


def run_streaming(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> CommandResult:
    return _active.run_streaming(argv, timeout=timeout, cwd=cwd, env=env)


def run_interactive(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
) -> int:
    return _active.run_interactive(argv, cwd=cwd, env=env, timeout=timeout)
