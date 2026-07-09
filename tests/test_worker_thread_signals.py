"""Service-restart guarantee with the engine on a worker thread (§7 blocker 4).

The Textual UI drives the engine from a worker thread while signals
(SIGHUP from an SSH drop, SIGTERM, Ctrl+C's SIGINT) are delivered to the
*main* thread. These tests prove, in a real subprocess, that the
``klipper_service_stopped`` restart ``finally`` still runs in that model:

* main thread receives the signal and unwinds (``flash.py``'s handlers
  convert SIGHUP/SIGTERM to ``SystemExit``);
* interpreter shutdown joins **non-daemon** worker threads, so the
  in-flight flash completes and the restart executes.

The daemon-thread scenario is asserted as a *failure* on purpose: it
documents why the UI's engine bridge MUST run engine jobs on non-daemon
threads (or explicitly join them before exiting). If the engine bridge is
ever switched to daemon threads without a join, this file is the spec that
change violates.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Child process: fakes runner.run to record systemctl calls, installs the
# real production signal handlers, runs klipper_service_stopped in a worker
# thread, and idles in the main thread like a UI event loop would.
CHILD_SCRIPT = """
import sys, threading, time

log_path, flash_seconds, thread_mode = sys.argv[1], float(sys.argv[2]), sys.argv[3]

from kflash import runner, service
from kflash.flash import _install_signal_handlers

def log(line):
    with open(log_path, "a") as fh:
        fh.write(line + "\\n")

class _FakeResult:
    returncode = 0
    stdout = "active\\n"
    stderr = ""
    timed_out = False

def fake_run(argv, timeout=None, **kw):
    log("RUN " + " ".join(argv))
    return _FakeResult()

runner.run = fake_run  # service.py holds the module, so the attr patch takes

_install_signal_handlers()  # the real production SIGHUP/SIGTERM handlers

def worker():
    with service.klipper_service_stopped(timeout=5):
        log("CRITICAL-SECTION-ENTERED")
        time.sleep(flash_seconds)
        log("FLASH-DONE")

t = threading.Thread(target=worker, daemon=(thread_mode == "daemon"))
t.start()
while t.is_alive():
    time.sleep(0.05)
"""

START_CMD = "RUN sudo -n systemctl start klipper"


def _run_scenario(
    tmp_path: Path, sig: signal.Signals, thread_mode: str
) -> tuple[int, str]:
    log_path = tmp_path / "calls.log"
    child = tmp_path / "child.py"
    child.write_text(CHILD_SCRIPT)
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    proc = subprocess.Popen(
        [sys.executable, str(child), str(log_path), "1.5", thread_mode],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if log_path.exists() and "CRITICAL-SECTION-ENTERED" in log_path.read_text():
                break
            time.sleep(0.05)
        else:
            pytest.fail("child never entered the critical section")
        time.sleep(0.2)  # ensure the signal lands mid-flash
        proc.send_signal(sig)
        rc = proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return rc, log_path.read_text()


@pytest.mark.parametrize(
    "sig", [signal.SIGHUP, signal.SIGTERM, signal.SIGINT], ids=lambda s: s.name
)
def test_restart_runs_when_signal_hits_main_thread_nondaemon_worker(tmp_path, sig):
    rc, log = _run_scenario(tmp_path, sig, "nondaemon")
    assert START_CMD in log, (
        f"Klipper restart did not run after {sig.name} with engine on a "
        f"non-daemon worker thread:\n{log}"
    )
    # The in-flight flash must have been allowed to finish, not aborted
    # mid-write (interpreter shutdown joins the worker first).
    assert "FLASH-DONE" in log
    assert rc != 0  # the signal still terminates the process


def test_daemon_worker_breaks_the_guarantee(tmp_path):
    """Negative spec: a daemon engine thread dies with the process and the
    restart never runs. This is the failure mode the engine bridge must
    avoid — engine jobs go on non-daemon threads (or are joined on exit)."""
    rc, log = _run_scenario(tmp_path, signal.SIGHUP, "daemon")
    assert START_CMD not in log
    assert "FLASH-DONE" not in log
