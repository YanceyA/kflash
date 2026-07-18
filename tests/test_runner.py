"""Tests for the runner seam: FakeRunner injection + SubprocessRunner semantics."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest
from conftest import FakeRunner

from kflash import runner
from kflash.runner import CommandResult, SubprocessRunner

# Runner isolation (save/restore of the active runner) is provided by the
# autouse ``_restore_active_runner`` fixture in conftest.py.


def test_set_runner_routes_free_functions_to_fake():
    fake = FakeRunner(default=CommandResult(returncode=7, stdout="hi"))
    runner.set_runner(fake)

    res = runner.run(["git", "describe"], timeout=5)
    assert res.returncode == 7 and res.stdout == "hi"
    assert ("run", ("git", "describe")) in fake.calls


def test_monkeypatch_active(monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(runner, "_active", fake)
    runner.run_interactive(["sudo", "-v"], timeout=5)
    runner.run_streaming_lines(
        ["make", "clean"], timeout=5, on_line=lambda line: None
    )
    assert fake.count(mode="interactive") == 1
    assert fake.count(mode="stream_lines") == 1


def test_fakerunner_rule_matching():
    fake = FakeRunner(default=CommandResult(1))
    fake.when("-q", CommandResult(0, stdout="Detected UUID: abc, Application: Klipper"))
    hit = fake.run(["python3", "flashtool.py", "-i", "can0", "-q"], timeout=5)
    miss = fake.run(["python3", "flashtool.py", "-i", "can0", "-r"], timeout=5)
    assert hit.returncode == 0 and "Klipper" in hit.stdout
    assert miss.returncode == 1


def test_subprocess_runner_captures_stdout():
    runner.set_runner(SubprocessRunner())
    res = runner.run([sys.executable, "-c", "print('marker42')"], timeout=15)
    assert res.returncode == 0
    assert "marker42" in res.stdout
    assert not res.timed_out


def test_subprocess_runner_timeout_sets_timed_out():
    runner.set_runner(SubprocessRunner())
    res = runner.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3)
    assert res.timed_out
    assert res.returncode != 0


def test_subprocess_runner_interactive_returns_code():
    runner.set_runner(SubprocessRunner())
    rc = runner.run_interactive([sys.executable, "-c", "raise SystemExit(3)"], timeout=15)
    assert rc == 3


# --- Divergent timeout contract (Runner Protocol docstring) ----------------
# run / run_streaming_lines NEVER raise TimeoutExpired -> timed_out=True.
# run_interactive DOES raise TimeoutExpired (its int return can't carry a flag).


def test_run_timeout_returns_flag_with_decoded_partial_output():
    r = SubprocessRunner()
    # -u so 'partial' reaches the captured pipe before the sleep is killed.
    res = r.run(
        [sys.executable, "-u", "-c", "print('partial'); import time; time.sleep(5)"],
        timeout=0.5,
    )
    assert res.timed_out is True
    assert isinstance(res.stdout, str)  # partial output decoded, not bytes
    assert "partial" in res.stdout


def test_run_interactive_timeout_raises():
    r = SubprocessRunner()
    with pytest.raises(subprocess.TimeoutExpired):
        r.run_interactive(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3
        )


# --- run_streaming_lines ----------------------------------------------------


def test_run_streaming_lines_delivers_lines_in_order():
    r = SubprocessRunner()
    script = "print('one'); print('two'); print('three')"
    received = []
    res = r.run_streaming_lines(
        [sys.executable, "-c", script], timeout=15, on_line=received.append
    )
    assert received == ["one", "two", "three"]
    assert "one" in res.stdout and "two" in res.stdout and "three" in res.stdout
    assert res.returncode == 0
    assert not res.timed_out


def test_run_streaming_lines_returncode_passthrough_nonzero():
    r = SubprocessRunner()
    script = "print('boom'); raise SystemExit(3)"
    received = []
    res = r.run_streaming_lines(
        [sys.executable, "-c", script], timeout=15, on_line=received.append
    )
    assert received == ["boom"]
    assert res.returncode == 3
    assert not res.timed_out


def test_run_streaming_lines_merges_stderr():
    r = SubprocessRunner()
    script = (
        "import sys; "
        "print('from-stdout'); "
        "print('from-stderr', file=sys.stderr)"
    )
    received = []
    res = r.run_streaming_lines(
        [sys.executable, "-c", script], timeout=15, on_line=received.append
    )
    assert set(received) == {"from-stdout", "from-stderr"}
    assert res.returncode == 0


def test_run_streaming_lines_timeout_returns_flag_not_raises():
    r = SubprocessRunner()
    script = (
        "print('early', flush=True); "
        "import time; time.sleep(30)"
    )
    received = []
    res = r.run_streaming_lines(
        [sys.executable, "-c", script], timeout=1, on_line=received.append
    )
    assert res.timed_out is True
    assert "early" in received
    assert "early" in res.stdout


def test_run_streaming_lines_on_line_exception_reaps_child():
    r = SubprocessRunner()
    # Long sleep after the first flushed line -- if the child weren't killed
    # promptly on the on_line exception, this test would take ~30s to return.
    script = "print('one', flush=True); import time; time.sleep(30)"

    def boom(line):
        raise RuntimeError("boom")

    start = time.monotonic()
    with pytest.raises(RuntimeError, match="boom"):
        r.run_streaming_lines([sys.executable, "-c", script], timeout=20, on_line=boom)
    elapsed = time.monotonic() - start
    assert elapsed < 10  # child killed+reaped promptly, not left running for the sleep

    # The runner is still healthy for a subsequent call (no leaked state).
    received = []
    res = r.run_streaming_lines(
        [sys.executable, "-c", "print('still-fine')"],
        timeout=15,
        on_line=received.append,
    )
    assert received == ["still-fine"]
    assert res.returncode == 0


def test_run_streaming_lines_replaces_invalid_utf8_bytes():
    r = SubprocessRunner()
    script = (
        "import sys; "
        "sys.stdout.buffer.write(b'\\xff\\xfe bad\\n'); "
        "sys.stdout.flush()"
    )
    received = []
    res = r.run_streaming_lines(
        [sys.executable, "-c", script], timeout=5, on_line=received.append
    )
    assert res.timed_out is False
    assert len(received) == 1
    assert "bad" in received[0]
    assert "�" in received[0]  # replacement char in place of the bad bytes


def test_fakerunner_when_lines_replays_through_on_line():
    fake = FakeRunner()
    fake.when_lines("flashtool.py", ["line one", "line two"])
    received = []
    res = fake.run_streaming_lines(
        ["python3", "flashtool.py", "-f"], timeout=5, on_line=received.append
    )
    assert received == ["line one", "line two"]
    assert res.returncode == 0
    assert fake.count(mode="stream_lines") == 1
