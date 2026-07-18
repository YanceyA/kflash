"""Helpers shared across two or more command modules.

Kept UI-free per the engine layering rule (§B.5): commands talk to the world
only through ``Emitter`` (output) and ``DecisionProvider`` (input).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from ..config import get_config_dir
from ..decisions import ConfirmDecision, DecisionProvider
from ..events import Emitter


def _short_path(path_value: str) -> str:
    """Return filename-only for /dev/serial/by-id paths."""
    try:
        return Path(path_value).name
    except (TypeError, ValueError):
        return path_value


def emit_output_tail(
    em: Emitter, output: Optional[str], *, section: str = "Build", limit: int = 20
) -> None:
    """Emit the last *limit* lines of captured subprocess *output*.

    No-ops on falsy output. Strips trailing whitespace before splitting so a
    trailing-newline-only final line doesn't consume one of the *limit* slots.
    """
    if not output:
        return
    for line in output.strip().splitlines()[-limit:]:
        em.info(section, line)


def _remove_cached_config(
    device_key: str,
    em: Emitter,
    decider: DecisionProvider,
    prompt: bool = True,
    device_name: str | None = None,
) -> None:
    """Remove cached config directory for a device key."""
    config_dir = get_config_dir(device_key)
    if not config_dir.exists():
        return

    should_remove = True
    if prompt:
        label = device_name or device_key
        should_remove = decider.confirm(
            ConfirmDecision(
                id="remove_cached_config",
                message=f"Also remove cached config for '{label}'?",
                default=False,
            )
        )

    if not should_remove:
        em.info("Registry", "Cached config kept")
        return

    try:
        shutil.rmtree(config_dir)
        em.success("Cached config removed")
    except OSError as exc:
        em.warn(f"Failed to remove cached config: {exc}")
