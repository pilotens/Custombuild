"""Supervise one Celery beat process without manufacturing liveness evidence.

The schedule-file probe and failure budget match the Compose health check.
A stalled beat is stopped before this process exits, allowing the container's
existing restart policy to recover it. This supervisor never starts a second
beat itself and never dispatches jobs, changes leases or touches the schedule.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from types import FrameType

logger = logging.getLogger("custombuild.scheduler")
# Existing container-owned beat path; the supervisor only reads its metadata.
SCHEDULE_PATH = Path("/tmp/celerybeat-schedule")  # noqa: S108
MAX_SCHEDULE_AGE_SECONDS = 300
STARTUP_GRACE_SECONDS = 30
CHECK_INTERVAL_SECONDS = 30
MAX_CONSECUTIVE_FAILURES = 5
TERMINATE_GRACE_SECONDS = 10
KILL_GRACE_SECONDS = 5


class ShutdownRequest:
    """Signal-safe flag; avoid taking a threading lock from a signal handler."""

    def __init__(self) -> None:
        self.requested = False

    def is_set(self) -> bool:
        return self.requested

    def set(self) -> None:
        self.requested = True

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            # A signal returns control within one second, without calling
            # Event.set/logging locks in its handler or busy-polling the child.
            time.sleep(min(1.0, remaining))


def schedule_is_fresh(path: Path = SCHEDULE_PATH, *, now: float | None = None) -> bool:
    """Read the same bounded file-age evidence as the existing health check."""

    current = time.time() if now is None else now
    try:
        for candidate in path.parent.glob(f"{path.name}*"):
            try:
                if candidate.is_file() and 0 <= current - candidate.stat().st_mtime <= (
                    MAX_SCHEDULE_AGE_SECONDS
                ):
                    return True
            except OSError:
                # A disappearing backend sidecar must not hide another live one.
                continue
    except OSError:
        return False
    return False


def monitor_beat(
    process: subprocess.Popen[bytes],
    shutdown: ShutdownRequest,
    *,
    probe: Callable[[], bool] = schedule_is_fresh,
) -> int:
    """Return only after exit, requested shutdown, or sustained lost liveness."""

    started = time.monotonic()
    failures = 0
    while not shutdown.is_set():
        returncode = process.poll()
        if returncode is not None:
            logger.error("scheduler_child_exited returncode=%s", returncode)
            # Even an unrequested exit(0) has stopped the scheduling service.
            return returncode if returncode > 0 else 1
        if probe():
            failures = 0
        elif time.monotonic() - started >= STARTUP_GRACE_SECONDS:
            failures += 1
            logger.warning(
                "scheduler_liveness_failed count=%s limit=%s maximum_file_age_seconds=%s",
                failures,
                MAX_CONSECUTIVE_FAILURES,
                MAX_SCHEDULE_AGE_SECONDS,
            )
            if failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error("scheduler_stalled stopping_child_before_container_restart")
                return 1
        shutdown.wait(CHECK_INTERVAL_SECONDS)
    return 0


def stop_beat(process: subprocess.Popen[bytes], *, initial_signal: int = signal.SIGTERM) -> None:
    """Terminate the isolated process group and reap the child before returning."""

    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, initial_signal)
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        logger.error("scheduler_terminate_timeout escalating_to_sigkill")
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        # Exiting the container still tears down its cgroup. Do not relaunch a
        # second process beside an uninterruptible child inside this container.
        logger.critical("scheduler_kill_timeout container_teardown_required")


def supervise(command: Sequence[str]) -> int:
    if not command:
        raise ValueError("a foreground Celery beat command is required")
    shutdown = ShutdownRequest()
    shutdown_signal: int = signal.SIGTERM

    def request_shutdown(signum: int, _frame: FrameType | None) -> None:
        nonlocal shutdown_signal
        shutdown_signal = signum
        shutdown.set()

    previous = {
        signum: signal.signal(signum, request_shutdown)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    process: subprocess.Popen[bytes] | None = None
    try:
        if shutdown.is_set():
            return 0
        # This argv comes from the fixed Compose command, never an HTTP request.
        process = subprocess.Popen(list(command), start_new_session=True)  # noqa: S603
        logger.info("scheduler_child_started pid=%s", process.pid)
        result = monitor_beat(process, shutdown)
        if shutdown.is_set():
            logger.info("scheduler_shutdown_requested signal=%s", shutdown_signal)
        return result
    finally:
        if process is not None:
            stop_beat(process, initial_signal=shutdown_signal)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    command = sys.argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    try:
        raise SystemExit(supervise(command))
    except (OSError, ValueError):
        logger.exception("scheduler_supervisor_failed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
