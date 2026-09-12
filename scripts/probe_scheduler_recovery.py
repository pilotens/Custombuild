"""Fault-inject a stalled beat child in an explicitly named CI Compose stack.

Use ``begin`` before live_acceptance.py and ``verify`` afterwards to exercise
the real supervisor and Docker restart policy. ``run`` performs both phases.
The probe does not restart containers itself or claim physical CNC readiness.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

CI_PROJECTS = frozenset(
    {
        "custombuild-acceptance",
        "custombuild-prod-acceptance",
        "custombuild-cd-acceptance",
    }
)
SCHEMA_VERSION = "custombuild.scheduler-recovery-probe.v1"
CONTAINER_ID = re.compile(r"[a-f0-9]{64}")
PYTHON = "/opt/custombuild-venv/bin/python"
INSPECT_FORMAT = (
    '{"id":{{json .Id}},"restart_count":{{json .RestartCount}},'
    '"running":{{json .State.Running}},"started_at":{{json .State.StartedAt}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"missing"{{end}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"restart_policy":{{json .HostConfig.RestartPolicy.Name}}}'
)

# This code runs inside the selected scheduler container using its existing
# Python runtime. No package installation, privileged exec or shell is needed.
CONTROL_SCRIPT = r"""
import json, os, pathlib, signal, sys, time
mode = sys.argv[1]
assert mode in {"inject", "verify", "resume"}
assert b"custombuild_worker.scheduler" in pathlib.Path("/proc/1/cmdline").read_bytes().split(b"\0")
children = pathlib.Path("/proc/1/task/1/children").read_text().split()
assert len(children) == 1, "scheduler must supervise exactly one direct child"
pid = int(children[0])
assert pid > 1
command = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
assert b"beat" in command and b"custombuild_worker.tasks:celery_app" in command
state_file = pathlib.Path(f"/proc/{pid}/status")
def stopped():
    return any(line.startswith("State:") and line.split()[1] in {"T", "t"}
               for line in state_file.read_text().splitlines())
if mode == "resume":
    expected_pid = int(sys.argv[2])
    if pid == expected_pid and stopped():
        os.kill(pid, signal.SIGCONT)
else:
    paths = tuple(pathlib.Path("/tmp").glob("celerybeat-schedule*"))
    assert paths and all(path.is_file() and not path.is_symlink() for path in paths)
    if mode == "inject":
        old_times = [(path, path.stat().st_atime_ns, path.stat().st_mtime_ns) for path in paths]
        os.kill(pid, signal.SIGSTOP)
        try:
            deadline = time.monotonic() + 2
            while not stopped():
                assert time.monotonic() < deadline, "beat did not stop"
                time.sleep(0.01)
            stale_time = time.time() - 600
            for path in paths:
                os.utime(path, (stale_time, stale_time))
        except BaseException:
            try:
                for path, atime_ns, mtime_ns in old_times:
                    os.utime(path, ns=(atime_ns, mtime_ns))
            finally:
                os.kill(pid, signal.SIGCONT)
            raise
    else:
        assert not stopped(), "replacement beat is stopped"
        assert any(0 <= time.time() - path.stat().st_mtime <= 300 for path in paths)
print(json.dumps({"beat_pid": pid, "action": mode}, sort_keys=True))
"""


class SchedulerProbeError(RuntimeError):
    """The CI fault injection or automatic recovery could not be proven."""


@dataclass(frozen=True)
class ProbeReceipt:
    project_name: str
    container_id: str
    restart_count: int
    started_at: str
    beat_pid: int
    injected_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            self.schema_version != SCHEMA_VERSION
            or self.project_name not in CI_PROJECTS
            or not isinstance(self.container_id, str)
            or CONTAINER_ID.fullmatch(self.container_id) is None
            or type(self.restart_count) is not int
            or self.restart_count < 0
            or not isinstance(self.started_at, str)
            or not self.started_at
            or type(self.beat_pid) is not int
            or self.beat_pid <= 1
        ):
            raise SchedulerProbeError("Invalid scheduler recovery receipt")
        _timestamp(self.injected_at)


Runner = Callable[[list[str]], str]


def _run(arguments: list[str]) -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise SchedulerProbeError("Docker is required for the scheduler recovery probe")
    try:
        result = subprocess.run(  # noqa: S603 - resolved Docker and fixed, scoped argv.
            [docker, *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Docker stderr or a complete inspect object may contain credentials.
        raise SchedulerProbeError("Scoped scheduler Docker command failed") from exc
    return result.stdout.strip()


def _snapshot(container_id: str, project_name: str, run: Runner) -> dict[str, Any]:
    try:
        state = json.loads(run(["inspect", "--format", INSPECT_FORMAT, container_id]))
    except (ValueError, TypeError) as exc:
        raise SchedulerProbeError("Scheduler inspect returned invalid JSON") from exc
    if (
        not isinstance(state, dict)
        or state.get("id") != container_id
        or state.get("project") != project_name
        or state.get("service") != "scheduler"
        or state.get("restart_policy") != "unless-stopped"
        or type(state.get("restart_count")) is not int
        or state["restart_count"] < 0
        or not isinstance(state.get("started_at"), str)
        or not state["started_at"]
        or type(state.get("running")) is not bool
    ):
        raise SchedulerProbeError("Scheduler identity, restart policy or state differs")
    return state


def _control(container_id: str, action: str, run: Runner, beat_pid: int = 0) -> int:
    try:
        result = json.loads(
            run(
                [
                    "exec",
                    container_id,
                    PYTHON,
                    "-c",
                    CONTROL_SCRIPT,
                    action,
                    str(beat_pid),
                ]
            )
        )
    except (ValueError, TypeError) as exc:
        raise SchedulerProbeError("Scheduler control returned invalid JSON") from exc
    if (
        not isinstance(result, dict)
        or result.get("action") != action
        or type(result.get("beat_pid")) is not int
        or result["beat_pid"] <= 1
    ):
        raise SchedulerProbeError("Scheduler control did not identify its beat child")
    return int(result["beat_pid"])


def begin_probe(
    compose_files: Sequence[str],
    project_name: str,
    receipt_path: Path,
    *,
    allow_fault_injection: bool = False,
    run: Runner = _run,
) -> ProbeReceipt:
    if (
        allow_fault_injection is not True
        or project_name not in CI_PROJECTS
        or os.environ.get("GITHUB_ACTIONS") != "true"
    ):
        raise SchedulerProbeError(
            "Fault injection requires explicit consent and a named CI project"
        )
    if not compose_files:
        raise SchedulerProbeError("At least one explicit Compose file is required")
    arguments = ["compose", "--project-name", project_name]
    for path in compose_files:
        arguments.extend(["--file", str(path)])
    container_id = run([*arguments, "ps", "--quiet", "scheduler"]).strip()
    if CONTAINER_ID.fullmatch(container_id) is None:
        raise SchedulerProbeError("Expected exactly one running scheduler container")
    before = _snapshot(container_id, project_name, run)
    if before["running"] is not True or before.get("health") != "healthy":
        raise SchedulerProbeError("Scheduler must be healthy before fault injection")
    # Refuse an existing receipt before stopping any process.
    with receipt_path.open("x", encoding="utf-8") as destination:
        beat_pid = _control(container_id, "inject", run)
        receipt = ProbeReceipt(
            project_name,
            container_id,
            before["restart_count"],
            before["started_at"],
            beat_pid,
            datetime.now(UTC).isoformat(),
        )
        try:
            destination.write(json.dumps(asdict(receipt), sort_keys=True) + "\n")
            destination.flush()
        except BaseException:
            _control(container_id, "resume", run, beat_pid)
            raise
    return receipt


def _timestamp(value: object) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError("missing timestamp")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("timestamp requires a timezone")
        return parsed
    except ValueError as exc:
        raise SchedulerProbeError("Invalid scheduler probe timestamp") from exc


def record_acceptance(log_path: Path, source: TextIO, output: TextIO) -> None:
    """Timestamp the existing acceptance script's flushed JSON events."""
    with log_path.open("x", encoding="utf-8") as destination:
        for line in source:
            output.write(line)
            output.flush()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == "job_status":
                destination.write(
                    json.dumps(
                        {
                            "observed_at": datetime.now(UTC).isoformat(),
                            "event": event,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                destination.flush()


def _queued_job_proof(log_path: Path, receipt: ProbeReceipt, restarted_at: str) -> str:
    try:
        if log_path.stat().st_size > 1_000_000:
            raise SchedulerProbeError("Acceptance event log is oversized")
        observations = [json.loads(line) for line in log_path.read_text().splitlines() if line]
        injected = _timestamp(receipt.injected_at)
        restarted = _timestamp(restarted_at)
        events_by_job: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
        for observation in observations:
            observed = _timestamp(observation["observed_at"])
            event = observation["event"]
            job_id = event["job_id"]
            if event.get("event") != "job_status" or not isinstance(job_id, str) or not job_id:
                continue
            events_by_job.setdefault(job_id, []).append((observed, event))
        for job_id, events in events_by_job.items():
            first_at, first_event = events[0]
            if (
                first_event.get("status") == "queued"
                and type(first_event.get("attempts")) is int
                and first_event["attempts"] == 0
                and injected <= first_at < restarted
                and not any(
                    observed < restarted and event.get("status") != "queued"
                    for observed, event in events
                )
                and any(
                    event.get("status") == "succeeded"
                    and type(event.get("attempts")) is int
                    and event["attempts"] >= 1
                    and observed >= restarted
                    for observed, event in events
                )
            ):
                return job_id
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SchedulerProbeError("Could not read valid acceptance job observations") from exc
    raise SchedulerProbeError(
        "No job was observed queued during the stall and succeeded after restart"
    )


def read_receipt(path: Path) -> ProbeReceipt:
    try:
        raw = path.read_bytes()
        if len(raw) > 4096:
            raise ValueError("oversized receipt")
        payload = json.loads(raw)
        return ProbeReceipt(**payload)
    except (OSError, TypeError, ValueError) as exc:
        raise SchedulerProbeError("Could not read a valid scheduler recovery receipt") from exc


def verify_probe(
    receipt: ProbeReceipt,
    *,
    timeout_seconds: float = 300,
    poll_seconds: float = 2,
    acceptance_log: Path | None = None,
    run: Runner = _run,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if not (
        math.isfinite(timeout_seconds)
        and 0 < timeout_seconds <= 600
        and math.isfinite(poll_seconds)
        and 0 < poll_seconds <= timeout_seconds
    ):
        raise SchedulerProbeError("Invalid scheduler recovery timeout or polling interval")
    deadline = monotonic() + timeout_seconds
    try:
        while True:
            current = _snapshot(receipt.container_id, receipt.project_name, run)
            if current["restart_count"] < receipt.restart_count:
                raise SchedulerProbeError("Scheduler restart count moved backwards")
            if (
                current["restart_count"] > receipt.restart_count
                and current["started_at"] != receipt.started_at
                and current["running"] is True
                and current.get("health") == "healthy"
            ):
                beat_pid = _control(receipt.container_id, "verify", run)
                evidence = {
                    "event": "scheduler_recovery_verified",
                    "container_id": receipt.container_id,
                    "previous_restart_count": receipt.restart_count,
                    "restart_count": current["restart_count"],
                    "restarted_at": current["started_at"],
                    "beat_pid": beat_pid,
                    "healthy": True,
                }
                if acceptance_log is not None:
                    evidence["queued_job_id"] = _queued_job_proof(
                        acceptance_log,
                        receipt,
                        current["started_at"],
                    )
                    evidence["queued_before_restart"] = True
                return evidence
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise SchedulerProbeError("Scheduler did not automatically restart healthy in time")
            sleep(min(poll_seconds, remaining))
    except BaseException:
        # Avoid leaving a stopped child behind when a diagnostic deadline fails.
        # Only the original beat PID in the same labelled container may resume.
        with suppress(SchedulerProbeError):
            _control(receipt.container_id, "resume", run, receipt.beat_pid)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("begin", "verify", "run", "record"))
    parser.add_argument("--project-name", choices=sorted(CI_PROJECTS))
    parser.add_argument("--compose-file", action="append", default=[])
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--acceptance-log", type=Path)
    parser.add_argument("--allow-fault-injection", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args(argv)
    try:
        if args.action == "record":
            if args.acceptance_log is None:
                parser.error("record requires --acceptance-log")
            record_acceptance(args.acceptance_log, sys.stdin, sys.stdout)
            return 0
        if args.receipt is None:
            parser.error("begin, verify and run require --receipt")
        if args.action in {"begin", "run"}:
            receipt = begin_probe(
                args.compose_file,
                args.project_name,
                args.receipt,
                allow_fault_injection=args.allow_fault_injection,
            )
            print(
                json.dumps({"event": "scheduler_stall_injected", **asdict(receipt)}, sort_keys=True)
            )
        else:
            receipt = read_receipt(args.receipt)
        if args.action in {"verify", "run"}:
            print(
                json.dumps(
                    verify_probe(
                        receipt,
                        timeout_seconds=args.timeout_seconds,
                        acceptance_log=args.acceptance_log,
                    ),
                    sort_keys=True,
                )
            )
    except (SchedulerProbeError, OSError) as exc:
        print(json.dumps({"event": "scheduler_recovery_failed", "error": str(exc)}, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
