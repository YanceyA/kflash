"""Snapshot + smoke tests for the kflash Textual style guide.

The snapshots (SVG, under ``tests/ui/__snapshots__/``) are the regression guard
for the visual identity: any drift in panel chrome, palette, table styling, or
the footer hint line will fail these and must be re-reviewed by eye before the
snapshot is updated with ``pytest --snapshot-update``.

16-colour degradation: Textual's snapshot harness renders through its SVG
exporter, which always emits truecolor; it exposes no per-render "constrain to
16 colours" hook (unlike a live terminal, where ``kflash.theme`` would degrade).
So we do NOT assert a 16-colour SVG here -- we only prove the truecolor design
and that a NO_COLOR/monochrome run still boots without crashing. Real 16-colour
behaviour is covered by the legacy ``kflash.theme`` tier logic, not the new UI.
"""

from __future__ import annotations

import asyncio

from textual.widgets import DataTable, ProgressBar, Static

from kflash.ui.skin import HintLine, Panel
from kflash.ui.style_guide import KConfirmScreen, StyleGuideApp

# 80 columns matches the legacy MAX_PANEL_WIDTH; height leaves room for every
# panel plus the footer hint line.
_SIZE = (80, 32)


def test_style_guide_default(snap_compare) -> None:
    """Default view: devices, status, and progress panels + hint line."""
    assert snap_compare(StyleGuideApp(), terminal_size=_SIZE)


def test_style_guide_modal(snap_compare) -> None:
    """Confirm modal open over the dimmed style-guide screen."""
    assert snap_compare(StyleGuideApp(), press=["d"], terminal_size=_SIZE)


def test_app_boots_with_widgets() -> None:
    """Pilot smoke test: the app boots and every showcased widget is present."""

    async def _run() -> None:
        app = StyleGuideApp()
        async with app.run_test(size=_SIZE) as pilot:
            assert app.theme == "kflash"
            table = app.query_one("#devices", DataTable)
            assert table.row_count == 3
            assert len(table.columns) == 4
            assert app.query_one("#flash-progress", ProgressBar).progress == 62
            assert app.query(Panel)  # at least one skinned panel
            assert app.query(HintLine)  # footer hint line
            # Modal toggles cleanly.
            await pilot.press("d")
            assert isinstance(app.screen, KConfirmScreen)
            await pilot.press("escape")
            assert not isinstance(app.screen, KConfirmScreen)

    asyncio.run(_run())


def test_app_boots_no_color(monkeypatch) -> None:
    """NO_COLOR (monochrome) run must still boot without crashing."""
    monkeypatch.setenv("NO_COLOR", "1")

    async def _run() -> None:
        app = StyleGuideApp()
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            assert app.query_one("#devices", DataTable).row_count == 3
            # Content still renders (a Static exists and produces text).
            assert app.query(Static)

    asyncio.run(_run())
