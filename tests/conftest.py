"""Shared pytest configuration for the kflash Tier-1 pure-logic test suite.

Ensures the repository root is importable so ``import kflash`` works when
pytest is invoked from anywhere, and provides a tiny in-memory registry
stub used by the validation tests (the real Registry does disk I/O we do
not want in Tier-1 pure-logic tests).
"""

from __future__ import annotations

import os
import sys

import pytest

# Make the repo root importable regardless of pytest's rootdir handling.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture(autouse=True)
def _restore_active_runner():
    """Save/restore ``kflash.runner``'s active runner around every test.

    Centralizes the subprocess-seam isolation that each test file used to roll
    itself: a test may ``set_runner(FakeRunner())`` freely and the global is
    reset afterwards, so no injected runner leaks across tests.
    """
    from kflash import runner

    original = runner.get_runner()
    try:
        yield
    finally:
        runner.set_runner(original)


class FakeRegistry:
    """Minimal registry stand-in exposing only ``.get(key)``.

    ``validate_device_key`` and ``generate_device_key`` only ever call
    ``registry.get(key)`` and treat a non-None result as "already taken".
    """

    def __init__(self, existing_keys=()):
        self._keys = set(existing_keys)

    def get(self, key):
        # Return a truthy sentinel (mimicking a DeviceEntry) for known keys.
        return object() if key in self._keys else None


class FakeRunner:
    """In-memory :class:`kflash.runner.Runner` for Tier-2 orchestration tests.

    Records every call and returns scripted results. Rules are matched by a
    substring test against the joined argv, checked in registration order; the
    first match wins, else :attr:`default` is returned.
    """

    def __init__(self, default=None):
        from kflash.runner import CommandResult

        self.calls = []  # list of (mode, argv-tuple)
        self.rules = []  # list of (substr, result)
        self.default = default if default is not None else CommandResult(0)

    def when(self, token, result):
        """Register a rule: argv containing exact token *token* -> *result*."""
        self.rules.append((token, result))
        return self

    def _result(self, argv):
        from kflash.runner import CommandResult

        tokens = [str(a) for a in argv]
        for token, res in self.rules:
            if token in tokens:
                return res if isinstance(res, CommandResult) else CommandResult(int(res))
        res = self.default
        return res if isinstance(res, CommandResult) else CommandResult(int(res))

    def count(self, mode=None, token=None):
        """Count recorded calls, optionally filtered by mode and/or exact token."""
        total = 0
        for m, argv in self.calls:
            if mode is not None and m != mode:
                continue
            if token is not None and token not in argv:
                continue
            total += 1
        return total

    def run(self, argv, *, timeout, cwd=None, env=None, input=None, text=True):
        self.calls.append(("run", tuple(str(a) for a in argv)))
        return self._result(argv)

    def run_streaming(self, argv, *, timeout, cwd=None, env=None):
        self.calls.append(("stream", tuple(str(a) for a in argv)))
        return self._result(argv)

    def run_interactive(self, argv, *, cwd=None, env=None, timeout=None):
        self.calls.append(("interactive", tuple(str(a) for a in argv)))
        return self._result(argv).returncode


class FakeDecisionProvider:
    """Recording, scriptable :class:`kflash.decisions.DecisionProvider`.

    Answers each request from a scripted map keyed by the request's stable
    identity and records every call so tests can assert the round-trip:

    * ``confirms``     -- ``{ConfirmDecision.id: bool}`` (falls back to req.default)
    * ``prompts``      -- ``{TextPromptDecision.message: str}`` (falls back to default)
    * ``manual_ready`` -- answer for ``manual_bootloader_ready`` (default True)

    Recorded requests are exposed on ``confirm_calls`` / ``prompt_calls`` /
    ``manual_calls`` for assertions.
    """

    def __init__(self, confirms=None, prompts=None, manual_ready=True):
        self.confirms = dict(confirms or {})
        self.prompts = dict(prompts or {})
        self.manual_ready = manual_ready
        self.confirm_calls = []
        self.prompt_calls = []
        self.manual_calls = []

    def confirm(self, req):
        self.confirm_calls.append(req)
        return self.confirms.get(req.id, req.default)

    def choose_device(self, req):
        return req.choices[0].key if req.choices else None

    def choose_flash_method(self, req):
        return None

    def manual_bootloader_ready(self, req):
        self.manual_calls.append(req)
        return self.manual_ready

    def mcu_mismatch(self, req):
        return "k"

    def choose_ccache_action(self, req):
        return "skip"

    def prompt_text(self, req):
        self.prompt_calls.append(req)
        return self.prompts.get(req.message, req.default)
