"""Command entry points invoked by the UI (and, in Phase 4, the CLI).

Each command is UI-free: it emits via ``Emitter`` and asks via
``DecisionProvider`` (see design §B.5). Commands never import any UI module
(enforced by tests/test_layering.py).
"""

from __future__ import annotations

from .build_cmd import cmd_build
from .device_add import cmd_add_device
from .device_manage import cmd_list_devices, cmd_remove_device
from .flash_batch import cmd_flash_all
from .flash_single import cmd_flash

__all__ = [
    "cmd_flash",
    "cmd_flash_all",
    "cmd_add_device",
    "cmd_remove_device",
    "cmd_list_devices",
    "cmd_build",
]
