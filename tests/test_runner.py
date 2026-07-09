"""Tests for the runner seam: FakeRunner injection + SubprocessRunner semantics."""

from __future__ import annotations

import subprocess
import sys

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
    runner.run_streaming(["make", "clean"], timeout=5)
    runner.run_interactive(["sudo", "-v"], timeout=5)
    assert fake.count(mode="stream") == 1
    assert fake.count(mode="interactive") == 1


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
# run / run_streaming NEVER raise TimeoutExpired -> timed_out=True.
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


def test_run_streaming_timeout_returns_flag_not_raises():
    r = SubprocessRunner()
    res = r.run_streaming(
        [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3
    )
    assert res.timed_out is True
    assert res.returncode != 0


def test_run_interactive_timeout_raises():
    r = SubprocessRunner()
    with pytest.raises(subprocess.TimeoutExpired):
        r.run_interactive(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3
        )
