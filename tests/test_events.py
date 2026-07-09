"""Tests for the event stream: Emitter -> FlashEvent, TeeSink, NullSink.

The legacy ``Output`` protocol and its ``LegacyOutputSink``/``CliOutput``
byte-exactness proxies died with the legacy UI (Stage 3). The surviving
contract is: engine -> :class:`Emitter` -> :class:`FlashEvent` -> some
:class:`EventSink` (the Textual bridge sink in production, ``NullSink``/
``TeeSink`` for silent/fan-out composition). The every-kind-renders
invariant now lives in ``tests/ui/test_operation.py`` against the operation
screen's ``render_event``.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout

from kflash.events import Emitter, FlashEvent, NullSink, TeeSink


class RecordingSink:
    def __init__(self):
        self.events: list[FlashEvent] = []

    def emit(self, event: FlashEvent) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Emitter -> FlashEvent mapping
# ---------------------------------------------------------------------------


def test_emitter_builds_expected_events():
    rec = RecordingSink()
    em = Emitter(rec)
    em.info("Version", "hello")
    em.success("ok")
    em.warn("careful")
    em.error("bad")
    em.error_with_recovery("T", "msg", context={"a": "b"}, recovery="do x")
    em.device_line("REG", "name", "detail")
    em.phase("Flash All", "Cancelled")
    em.step_divider()
    em.device_divider(1, 3, "dev")

    kinds = [e.kind for e in rec.events]
    assert kinds == [
        "info",
        "success",
        "warn",
        "error",
        "error_recovery",
        "device_line",
        "phase",
        "step_divider",
        "device_divider",
    ]
    info = rec.events[0]
    assert info.section == "Version" and info.message == "hello"
    phase = rec.events[6]
    # section carries the original label; phase is the lowercased first word
    assert phase.section == "Flash All" and phase.phase == "flash"
    dd = rec.events[8]
    assert dd.index == 1 and dd.total == 3 and dd.name == "dev"


def test_emitter_phase_empty_label_safe():
    rec = RecordingSink()
    Emitter(rec).phase("", "msg")
    assert rec.events[0].phase == "flash"


def test_emitter_step_lifecycle_carries_device_key():
    rec = RecordingSink()
    em = Emitter(rec)
    em.step_start("Bootloader", "Entering usb bootloader...", device_key="octo")
    em.step_end("Bootloader", "Entered (1.0s)", elapsed=1.0, device_key="octo")
    start, end = rec.events
    assert start.kind == "step_start" and start.device_key == "octo"
    assert start.section == "Bootloader" and start.phase == "bootloader"
    assert end.kind == "step_end" and end.elapsed == 1.0


def test_emitter_progress_event_payload():
    rec = RecordingSink()
    Emitter(rec).progress("Verify", ".", progress=0.5, elapsed=2.0)
    ev = rec.events[0]
    assert ev.kind == "progress" and ev.phase == "verify"
    assert ev.progress == 0.5 and ev.elapsed == 2.0


# ---------------------------------------------------------------------------
# TeeSink fan-out / NullSink silence
# ---------------------------------------------------------------------------


def test_teesink_fans_out_to_all():
    a, b = RecordingSink(), RecordingSink()
    tee = TeeSink([a, b])
    ev = FlashEvent("info", section="S", message="m")
    tee.emit(ev)
    assert a.events == [ev]
    assert b.events == [ev]


def _capture(fn):
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        fn()
    return out_buf.getvalue() + err_buf.getvalue()


def test_null_sink_is_silent():
    em = Emitter(NullSink())
    assert _capture(
        lambda: (em.phase("Flash", "x"), em.error("y"), em.success("z"))
    ) == ""
