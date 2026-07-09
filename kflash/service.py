"""Klipper service lifecycle management with guaranteed restart."""

from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from . import runner
from .errors import ERROR_TEMPLATES, ServiceError, format_error
from .events import Emitter, NullSink
from .safety import should_restart_service

# Default timeout for systemctl operations
TIMEOUT_SERVICE = 30


def verify_passwordless_sudo() -> bool:
    """Check if sudo can run without a password.

    Returns:
        True if passwordless sudo works, False otherwise.
    """
    try:
        result = runner.run(["sudo", "-n", "true"], timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def acquire_sudo() -> bool:
    """Prompt user for sudo credentials and verify they are cached.

    Runs ``sudo -v`` with inherited stdio so the password prompt is
    visible, then verifies with ``sudo -n true`` to confirm credentials
    are usable for non-interactive calls.

    Returns:
        True if sudo credentials are cached and usable, False otherwise.
    """
    try:
        returncode = runner.run_interactive(["sudo", "-v"], timeout=60)
        if returncode != 0:
            return False
    except Exception:
        return False

    # Verify cache is actually usable (catches strict timestamp_timeout=0)
    try:
        check = runner.run(["sudo", "-n", "true"], timeout=5, text=False)
        return check.returncode == 0
    except Exception:
        return False


def refresh_sudo_timestamp() -> None:
    """Best-effort non-interactive refresh of cached sudo credentials.

    Long Flash All batches (especially CAN retries) can outlive sudo's
    timestamp_timeout, which would make the ``sudo -n systemctl start``
    in klipper_service_stopped fail and leave Klipper stopped. Calling
    this between devices extends the timestamp while it is still valid.
    Failures are ignored: passwordless setups don't need it, and an
    already-expired timestamp is handled by the retry in _start_klipper.
    """
    try:
        runner.run(["sudo", "-n", "-v"], timeout=5, text=False)
    except Exception:
        pass


def is_service_active() -> bool:
    """Public read-only predicate: is the Klipper service currently active?

    Returns True when ``systemctl is-active klipper`` reports ``active``. On any
    ambiguity (timeout, missing systemctl) it returns True -- the safe default,
    since a caller that stops Klipper must then restart it.

    This is the public name; :func:`_is_service_active` is kept as an alias so
    the engine's existing call sites (and the Klipper-restart guarantee they
    depend on) stay untouched while UI code reads through the public predicate.
    """
    try:
        result = runner.run(["systemctl", "is-active", "klipper"], timeout=5)
        if result.timed_out:
            # If we can't check, assume it was active (safer to restart)
            return True
        return result.stdout.strip() == "active"
    except Exception:
        # If we can't check, assume it was active (safer to restart)
        return True


# Private alias retained for engine callers (klipper_service_stopped, cmd_flash,
# ...) so the restart-critical path keeps its familiar name.
_is_service_active = is_service_active


def get_service_status(service_name: str) -> str:
    """Query systemctl for a service's current status.

    Runs ``systemctl is-active <service_name>`` and returns the raw status
    string (e.g. ``"active"``, ``"inactive"``, ``"failed"``).

    Returns ``"unknown"`` on any error (timeout, missing systemctl, etc.).

    Args:
        service_name: systemd service name (e.g. ``"klipper"``).
    """
    try:
        result = runner.run(["systemctl", "is-active", service_name], timeout=5)
        if result.timed_out:
            return "unknown"
        status = result.stdout.strip()
        return status if status else "unknown"
    except (FileNotFoundError, OSError):
        return "unknown"


def _stop_klipper(timeout: int = TIMEOUT_SERVICE) -> None:
    """Stop the Klipper service.

    Args:
        timeout: Seconds to wait for stop.

    Raises:
        ServiceError: If stop fails.
    """
    result = runner.run(
        ["sudo", "-n", "systemctl", "stop", "klipper"],
        timeout=timeout,
    )
    if result.timed_out:
        template = ERROR_TEMPLATES["service_stop_failed"]
        msg = format_error(
            template["error_type"],
            f"Timeout ({timeout}s) stopping Klipper service",
            recovery=template["recovery_template"],
        )
        raise ServiceError(msg) from None
    if result.returncode != 0:
        template = ERROR_TEMPLATES["service_stop_failed"]
        msg = format_error(
            template["error_type"],
            template["message_template"],
            context={"stderr": result.stderr.strip()},
            recovery=template["recovery_template"],
        )
        raise ServiceError(msg) from None


def _start_klipper(timeout: int = TIMEOUT_SERVICE, em: Optional[Emitter] = None) -> bool:
    """Start the Klipper service.

    Does not raise on failure - used in finally block.
    Emits warning/error details and returns success state.

    Args:
        timeout: Seconds to wait for start.
        em: Event emitter for formatted errors. Defaults to a silent no-op
            emitter so callers never need to branch on ``None``.

    Returns:
        True if service started successfully, False otherwise.
    """
    if em is None:
        em = Emitter(NullSink())

    def _emit_timeout() -> None:
        template = ERROR_TEMPLATES["service_start_failed"]
        message = f"Timeout ({timeout}s) starting Klipper service"
        em.error_with_recovery(
            template["error_type"],
            message,
            recovery=template["recovery_template"],
        )

    try:
        result = runner.run(
            ["sudo", "-n", "systemctl", "start", "klipper"],
            timeout=timeout,
        )
        if result.timed_out:
            _emit_timeout()
            return False
        if (
            result.returncode != 0
            and "password is required" in result.stderr
            and sys.stdin is not None
            and sys.stdin.isatty()
        ):
            # Sudo timestamp expired during the flash window. We still have a
            # terminal, so re-prompt once rather than leaving Klipper stopped.
            em.phase("Service", "Sudo credentials expired -- please re-authenticate")
            if acquire_sudo():
                result = runner.run(
                    ["sudo", "-n", "systemctl", "start", "klipper"],
                    timeout=timeout,
                )
                if result.timed_out:
                    _emit_timeout()
                    return False
        if result.returncode != 0:
            template = ERROR_TEMPLATES["service_start_failed"]
            em.error_with_recovery(
                template["error_type"],
                template["message_template"],
                context={"stderr": result.stderr.strip()},
                recovery=template["recovery_template"],
            )
            return False
        return True
    except BaseException as exc:
        template = ERROR_TEMPLATES["service_start_failed"]
        detail = str(exc).strip() or exc.__class__.__name__
        message = f"Error starting Klipper: {detail}"
        em.error_with_recovery(
            template["error_type"],
            message,
            recovery=template["recovery_template"],
        )
        return False


@dataclass
class ServiceState:
    """State tracking for Klipper service management."""

    was_active: bool
    will_restart: bool
    restart_succeeded: bool | None = None


@contextmanager
def klipper_service_stopped(
    timeout: int = TIMEOUT_SERVICE,
    em: Optional[Emitter] = None,
) -> Generator[ServiceState, None, None]:
    """Context manager that stops Klipper and guarantees restart.

    Stops the Klipper service on entry. Restarts it on exit,
    even if an exception occurs or Ctrl+C is pressed.

    Args:
        timeout: Seconds for systemctl operations.
        em: Event emitter for formatted status/errors. Defaults to a silent
            no-op emitter so callers never need to branch on ``None``.

    Yields:
        ServiceState with was_active/will_restart and restart outcome.

    Raises:
        ServiceError: If stopping Klipper fails.

    Example:
        with klipper_service_stopped() as state:
            flash_firmware(device)
    """
    if em is None:
        em = Emitter(NullSink())

    # Check if Klipper is currently active before stopping
    was_active = _is_service_active()
    will_restart = should_restart_service(was_active)
    state = ServiceState(was_active=was_active, will_restart=will_restart)

    try:
        # The stop must happen inside the try: a Ctrl+C or signal arriving
        # while `systemctl stop` runs would otherwise escape before the
        # restart in `finally` is registered, leaving Klipper stopped.
        # (Starting an already-running service in `finally` is a no-op.)
        if was_active:
            _stop_klipper(timeout)
        yield state
    finally:
        # Conditionally restart based on prior state
        if will_restart:
            state.restart_succeeded = _start_klipper(timeout, em=em)
        else:
            em.phase("Service", "Klipper was not running -- skipping restart")
