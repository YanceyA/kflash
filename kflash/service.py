"""Klipper service lifecycle management with guaranteed restart."""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

from .errors import ERROR_TEMPLATES, ServiceError, format_error

# Default timeout for systemctl operations
TIMEOUT_SERVICE = 30


def verify_passwordless_sudo() -> bool:
    """Check if sudo can run without a password.

    Returns:
        True if passwordless sudo works, False otherwise.
    """
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
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
        result = subprocess.run(["sudo", "-v"], timeout=60)
        if result.returncode != 0:
            return False
    except (subprocess.TimeoutExpired, Exception):
        return False

    # Verify cache is actually usable (catches strict timestamp_timeout=0)
    try:
        check = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            timeout=5,
        )
        return check.returncode == 0
    except Exception:
        return False


def _is_service_active() -> bool:
    """Check if the Klipper service is currently active.

    Returns:
        True if active, False otherwise.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "klipper"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() == "active"
    except Exception:
        # If we can't check, assume it was active (safer to restart)
        return True


def get_service_status(service_name: str) -> str:
    """Query systemctl for a service's current status.

    Runs ``systemctl is-active <service_name>`` and returns the raw status
    string (e.g. ``"active"``, ``"inactive"``, ``"failed"``).

    Returns ``"unknown"`` on any error (timeout, missing systemctl, etc.).

    Args:
        service_name: systemd service name (e.g. ``"klipper"``).
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = result.stdout.strip()
        return status if status else "unknown"
    except subprocess.TimeoutExpired:
        return "unknown"
    except FileNotFoundError:
        return "unknown"
    except OSError:
        return "unknown"


def _stop_klipper(timeout: int = TIMEOUT_SERVICE) -> None:
    """Stop the Klipper service.

    Args:
        timeout: Seconds to wait for stop.

    Raises:
        ServiceError: If stop fails.
    """
    try:
        result = subprocess.run(
            ["sudo", "-n", "systemctl", "stop", "klipper"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            template = ERROR_TEMPLATES["service_stop_failed"]
            msg = format_error(
                template["error_type"],
                template["message_template"],
                context={"stderr": result.stderr.strip()},
                recovery=template["recovery_template"],
            )
            raise ServiceError(msg) from None
    except subprocess.TimeoutExpired as exc:
        template = ERROR_TEMPLATES["service_stop_failed"]
        msg = format_error(
            template["error_type"],
            f"Timeout ({timeout}s) stopping Klipper service",
            recovery=template["recovery_template"],
        )
        raise ServiceError(msg) from exc


def _start_klipper(timeout: int = TIMEOUT_SERVICE, out=None) -> bool:
    """Start the Klipper service.

    Does not raise on failure - used in finally block.
    Prints warning/error details and returns success state.

    Args:
        timeout: Seconds to wait for start.
        out: Optional output interface for formatted errors.

    Returns:
        True if service started successfully, False otherwise.
    """
    try:
        result = subprocess.run(
            ["sudo", "-n", "systemctl", "start", "klipper"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            template = ERROR_TEMPLATES["service_start_failed"]
            if out is not None:
                out.error_with_recovery(
                    template["error_type"],
                    template["message_template"],
                    context={"stderr": result.stderr.strip()},
                    recovery=template["recovery_template"],
                )
            else:
                print(
                    format_error(
                        template["error_type"],
                        template["message_template"],
                        context={"stderr": result.stderr.strip()},
                        recovery=template["recovery_template"],
                    )
                )
            return False
        return True
    except subprocess.TimeoutExpired:
        template = ERROR_TEMPLATES["service_start_failed"]
        message = f"Timeout ({timeout}s) starting Klipper service"
        if out is not None:
            out.error_with_recovery(
                template["error_type"],
                message,
                recovery=template["recovery_template"],
            )
        else:
            print(
                format_error(
                    template["error_type"],
                    message,
                    recovery=template["recovery_template"],
                )
            )
        return False
    except BaseException as exc:
        template = ERROR_TEMPLATES["service_start_failed"]
        detail = str(exc).strip() or exc.__class__.__name__
        message = f"Error starting Klipper: {detail}"
        if out is not None:
            out.error_with_recovery(
                template["error_type"],
                message,
                recovery=template["recovery_template"],
            )
        else:
            print(
                format_error(
                    template["error_type"],
                    message,
                    recovery=template["recovery_template"],
                )
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
    out=None,
) -> Generator[ServiceState, None, None]:
    """Context manager that stops Klipper and guarantees restart.

    Stops the Klipper service on entry. Restarts it on exit,
    even if an exception occurs or Ctrl+C is pressed.

    Args:
        timeout: Seconds for systemctl operations.
        out: Optional output interface for formatted errors.

    Yields:
        ServiceState with was_active/will_restart and restart outcome.

    Raises:
        ServiceError: If stopping Klipper fails.

    Example:
        with klipper_service_stopped() as state:
            flash_firmware(device)
        if state.will_restart:
            print("Klipper restarted")
    """
    # Check if Klipper is currently active before stopping
    from .safety import should_restart_service

    was_active = _is_service_active()
    will_restart = should_restart_service(was_active)
    state = ServiceState(was_active=was_active, will_restart=will_restart)

    if was_active:
        _stop_klipper(timeout)

    try:
        yield state
    finally:
        # Conditionally restart based on prior state
        if will_restart:
            state.restart_succeeded = _start_klipper(timeout, out=out)
        else:
            if out is not None:
                out.phase("Service", "Klipper was not running -- skipping restart")
            else:
                print("[Service] Klipper was not running -- skipping restart")
