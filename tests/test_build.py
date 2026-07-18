"""Tests for kflash.build.run_build: always-captured output.

``run_build`` used to stream ``make`` output to inherited stdio unless a
``quiet=True`` flag was passed (which drew raw compiler output over the
Textual UI). It now always captures via ``runner.run`` -- these tests pin
that behavior and the failure-tail surfaced in ``BuildResult.error_output``.
"""

from __future__ import annotations

from conftest import FakeRunner

from kflash import build, runner
from kflash.runner import CommandResult


def test_run_build_only_uses_captured_run_calls(tmp_path):
    fake = FakeRunner(default=CommandResult(0))
    runner.set_runner(fake)

    klipper = tmp_path / "klipper"
    (klipper / "out").mkdir(parents=True)
    (klipper / "out" / "klipper.bin").write_bytes(b"\x00" * 128)

    result = build.run_build(str(klipper))

    assert result.success
    # Every make invocation went through captured "run" -- never streaming.
    assert fake.count(mode="stream_lines") == 0
    assert fake.count(mode="run", token="clean") == 1
    assert fake.count(mode="run") >= 2  # clean + make -jN


def test_run_build_failure_returns_captured_tail(tmp_path):
    fake = FakeRunner()
    # "clean" must be registered before "make" since both tokens appear in
    # argv for every make invocation (argv[0] is always "make"); first match
    # wins in FakeRunner, so "clean" -> success, "make" -> failure.
    fake.when("clean", CommandResult(0))
    fake.when(
        "make",
        CommandResult(2, stdout="compiling foo.c\n", stderr="foo.c:1: error: bad\n"),
    )
    runner.set_runner(fake)

    klipper = tmp_path / "klipper"
    klipper.mkdir()

    result = build.run_build(str(klipper))

    assert result.success is False
    assert result.error_output is not None
    assert "compiling foo.c" in result.error_output
    assert "foo.c:1: error: bad" in result.error_output
    # No streaming calls were made for the failing make either.
    assert fake.count(mode="stream_lines") == 0


def test_run_build_has_no_quiet_parameter(tmp_path):
    import inspect

    sig = inspect.signature(build.run_build)
    assert "quiet" not in sig.parameters
