from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from custombuild_worker import scheduler


class SchedulerWatchdogTests(unittest.TestCase):
    def test_probe_preserves_exact_health_age_and_backend_suffix_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "celerybeat-schedule"
            self.assertFalse(scheduler.schedule_is_fresh(base, now=1000))
            base.mkdir()
            self.assertFalse(scheduler.schedule_is_fresh(base, now=1000))
            sidecar = Path(f"{base}.db")
            sidecar.write_bytes(b"schedule")
            for mtime, expected in ((700, True), (699, False), (1000, True), (1001, False)):
                os.utime(sidecar, (mtime, mtime))
                self.assertEqual(scheduler.schedule_is_fresh(base, now=1000), expected)
            self.assertEqual(sidecar.read_bytes(), b"schedule")

    def test_missing_or_unreadable_schedule_is_not_liveness(self) -> None:
        with patch.object(Path, "glob", side_effect=PermissionError):
            self.assertFalse(scheduler.schedule_is_fresh())

    def test_stall_requires_startup_grace_and_five_consecutive_failed_probes(self) -> None:
        child = MagicMock()
        child.poll.return_value = None
        shutdown = scheduler.ShutdownRequest()
        clock = [0.0]

        def advance(seconds: float) -> bool:
            clock[0] += seconds
            return False

        with (
            patch.object(scheduler.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(shutdown, "wait", side_effect=advance),
            self.assertLogs("custombuild.scheduler", level="WARNING") as logs,
        ):
            result = scheduler.monitor_beat(child, shutdown, probe=lambda: False)
        self.assertEqual(result, 1)
        self.assertEqual(clock[0], 150)
        self.assertEqual(sum("scheduler_liveness_failed" in line for line in logs.output), 5)
        self.assertTrue(any("scheduler_stalled" in line for line in logs.output))

    def test_successful_probe_resets_failure_budget_and_shutdown_is_clean(self) -> None:
        child = MagicMock()
        child.poll.return_value = None
        shutdown = scheduler.ShutdownRequest()
        clock = [0.0]
        observed = []

        def probe() -> bool:
            observed.append(clock[0])
            return clock[0] == 150

        def advance(seconds: float) -> bool:
            clock[0] += seconds
            if clock[0] == 300:
                shutdown.set()
            return shutdown.is_set()

        with (
            patch.object(scheduler.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(shutdown, "wait", side_effect=advance),
            self.assertLogs("custombuild.scheduler", level="WARNING") as logs,
        ):
            result = scheduler.monitor_beat(child, shutdown, probe=probe)
        self.assertEqual(result, 0)
        self.assertEqual(len(observed), 10)
        self.assertFalse(any("scheduler_stalled" in line for line in logs.output))

    def test_unexpected_child_exit_is_never_reported_as_a_live_scheduler(self) -> None:
        for code, expected in ((0, 1), (7, 7), (-signal.SIGKILL, 1)):
            child = MagicMock()
            child.poll.return_value = code
            with self.assertLogs("custombuild.scheduler", level="ERROR"):
                self.assertEqual(
                    scheduler.monitor_beat(child, scheduler.ShutdownRequest()), expected
                )

    def test_shutdown_has_bounded_term_kill_and_reap(self) -> None:
        child = MagicMock(pid=1234)
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("beat", 10), 0]
        with (
            patch.object(scheduler.os, "killpg") as kill,
            self.assertLogs("custombuild.scheduler", level="ERROR"),
        ):
            scheduler.stop_beat(child)
        self.assertEqual(
            kill.call_args_list,
            [
                unittest.mock.call(1234, signal.SIGTERM),
                unittest.mock.call(1234, signal.SIGKILL),
            ],
        )
        self.assertEqual(
            child.wait.call_args_list,
            [
                unittest.mock.call(timeout=10),
                unittest.mock.call(timeout=5),
            ],
        )

    def test_uninterruptible_child_does_not_cause_an_unbounded_wait(self) -> None:
        child = MagicMock(pid=1234)
        child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired("beat", 10)
        with (
            patch.object(scheduler.os, "killpg"),
            self.assertLogs("custombuild.scheduler", level="ERROR") as logs,
        ):
            scheduler.stop_beat(child)
        self.assertEqual(child.wait.call_count, 2)
        self.assertTrue(any("container_teardown_required" in line for line in logs.output))

    def test_exit_race_still_reaps_without_signalling_an_unrelated_process(self) -> None:
        child = MagicMock(pid=1234)
        child.poll.return_value = None
        with patch.object(scheduler.os, "killpg", side_effect=ProcessLookupError):
            scheduler.stop_beat(child)
        child.wait.assert_called_once_with(timeout=10)
        child.poll.return_value = 0
        with patch.object(scheduler.os, "killpg") as kill:
            scheduler.stop_beat(child)
        kill.assert_not_called()

    def test_one_child_is_started_and_incoming_signals_are_forwarded_before_exit(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers = {}
            child = MagicMock(pid=1234)
            child.poll.return_value = None

            def register(sig: int, handler: object, handlers=handlers) -> int:
                handlers[sig] = handler
                return signal.SIG_DFL

            def monitor(
                _child: object,
                shutdown: scheduler.ShutdownRequest,
                handlers=handlers,
                signum=signum,
            ) -> int:
                handlers[signum](signum, None)
                self.assertTrue(shutdown.is_set())
                return 0

            with (
                patch.object(scheduler.signal, "signal", side_effect=register),
                patch.object(scheduler.subprocess, "Popen", return_value=child) as launch,
                patch.object(scheduler, "monitor_beat", side_effect=monitor),
                patch.object(scheduler.os, "killpg") as kill,
            ):
                self.assertEqual(scheduler.supervise(["celery", "beat"]), 0)
            launch.assert_called_once_with(["celery", "beat"], start_new_session=True)
            kill.assert_called_once_with(1234, signum)
            child.wait.assert_called_once_with(timeout=10)
            self.assertEqual(
                handlers, {signal.SIGINT: signal.SIG_DFL, signal.SIGTERM: signal.SIG_DFL}
            )

    def test_supervisor_failure_stops_child_and_restores_signal_handlers(self) -> None:
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        child = MagicMock(pid=1234)
        child.poll.return_value = None
        with (
            patch.object(scheduler.subprocess, "Popen", return_value=child),
            patch.object(scheduler, "monitor_beat", side_effect=RuntimeError("probe crashed")),
            patch.object(scheduler.os, "killpg") as kill,
            self.assertRaisesRegex(RuntimeError, "probe crashed"),
        ):
            scheduler.supervise(["celery", "beat"])
        kill.assert_called_once_with(1234, signal.SIGTERM)
        self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)

    def test_real_foreground_child_is_terminated_and_reaped(self) -> None:
        child = subprocess.Popen(  # noqa: S603 - fixed local interpreter and code
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        try:
            scheduler.stop_beat(child)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_shutdown_wait_responds_without_locking_or_full_interval_delay(self) -> None:
        shutdown = scheduler.ShutdownRequest()
        with (
            patch.object(scheduler.time, "monotonic", side_effect=[0.0, 0.0]),
            patch.object(
                scheduler.time, "sleep", side_effect=lambda _seconds: shutdown.set()
            ) as sleep,
        ):
            shutdown.wait(30)
        sleep.assert_called_once_with(1.0)


if __name__ == "__main__":
    unittest.main()
