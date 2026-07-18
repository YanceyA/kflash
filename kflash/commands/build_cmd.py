"""``cmd_build`` -- build firmware for a registered device.

No TUI caller yet -- this is the CLI ``kflash build <name>`` front door (Phase 4).
Retained (not dead) per design §F.
"""

from __future__ import annotations

from ..build import run_build
from ..decisions import DecisionProvider
from ..errors import ERROR_TEMPLATES, get_recovery_text
from ..events import Emitter
from ..flash_steps import load_and_validate_config
from ..preflight import preflight_build
from ..registry import Registry
from ._common import emit_output_tail


def cmd_build(
    registry: Registry, device_key: str, em: Emitter, decider: DecisionProvider
) -> int:
    """Build firmware for a registered device.

    Orchestrates: load cached config -> menuconfig -> save config -> MCU validation -> build

    No TUI caller yet -- this is the CLI ``kflash build <name>`` front door (Phase 4).
    """
    # Load device entry
    entry = registry.get(device_key)
    if entry is None:
        template = ERROR_TEMPLATES["device_not_registered"]
        em.error_with_recovery(
            template["error_type"],
            template["message_template"].format(device=device_key),
            context={"device": device_key},
            recovery=get_recovery_text("device_not_registered"),
        )
        return 1

    # Load global config for klipper_dir
    data = registry.load()
    if data.global_config is None:
        em.error("Global config not set. Press A to add a device first.")
        return 1

    klipper_dir = data.global_config.klipper_dir
    em.info("Build", f"Building firmware for {entry.name} ({entry.mcu})")

    if not preflight_build(em, klipper_dir):
        return 1

    outcome = load_and_validate_config(
        entry=entry,
        device_key=device_key,
        klipper_dir=klipper_dir,
        em=em,
        decider=decider,
        skip_menuconfig=False,
        require_menuconfig=True,
    )
    if not outcome.ok:
        return outcome.exit_code

    # Build
    em.info("Build", "Running make clean + make...")
    result = run_build(klipper_dir)

    if not result.success:
        emit_output_tail(em, result.error_output)
        template = ERROR_TEMPLATES["build_failed"]
        em.error_with_recovery(
            template["error_type"],
            template["message_template"].format(device=device_key),
            context={"device": device_key},
            recovery=template["recovery_template"],
        )
        return 1

    # Success
    size_kb = result.firmware_size / 1024 if result.firmware_size else 0
    em.success(
        f"Build complete: {result.firmware_path} ({size_kb:.1f} KB) "
        f"in {result.elapsed_seconds:.1f}s"
    )
    return 0
