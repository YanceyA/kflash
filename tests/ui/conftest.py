"""Fixtures shared by the Textual UI tests.

Textual's ``Widget.__init__`` constructs an ``asyncio.Lock`` (via its
``RLock`` helper). On Python 3.9 ``asyncio.Lock.__init__`` *eagerly* calls
``asyncio.get_event_loop()``; from 3.10 on, the lock binds lazily to the
running loop and never touches ``get_event_loop()`` at construction time.

Several UI tests build widgets/screens outside a running app -- directly (the
``FlashMethodDialog``/``ConfigDiffDialog`` filtering and snapshot tests) or to
hand to ``snap_compare``. Any such construction that runs *after* an
``asyncio.run()`` (the ``_drive`` helpers and ``App.run_test``) has executed is
the problem: ``asyncio.run`` resets the main-thread loop to ``None`` on
teardown, so the next bare ``get_event_loop()`` raises
``RuntimeError: There is no current event loop in thread 'MainThread'`` -- on
3.9 only. That is exactly the CI failure on the py3.9 matrix leg (3.10-3.13
stay green because their ``Lock()`` never calls ``get_event_loop()``).

Guaranteeing a current event loop on the main thread before each UI test closes
that gap. The fixture is a no-op on 3.10+, where the eager-loop path does not
exist.
"""

from __future__ import annotations

import asyncio
import sys

import pytest


@pytest.fixture(autouse=True)
def _ensure_main_thread_event_loop():
    """Ensure a current event loop exists on the main thread (Python 3.9 only).

    ``asyncio.run()`` in the drive/snapshot helpers leaves the main-thread loop
    set to ``None``; on 3.9 that makes a subsequent Textual widget construction
    raise. Re-establish a loop before the test body runs.
    """
    if sys.version_info >= (3, 10):
        yield
        return

    created = None
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        created = asyncio.new_event_loop()
        asyncio.set_event_loop(created)
    try:
        yield
    finally:
        if created is not None:
            created.close()
            asyncio.set_event_loop(None)
