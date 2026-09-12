# ruff: noqa: S108 - these are assertions against fake container paths, not host files.

from __future__ import annotations

import io
import json
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import probe_scheduler_recovery as probe

CID = "a" * 64
PROJECT = "custombuild-acceptance"
OLD_START = "2026-09-12T19:00:00Z"
INJECTED = "2026-09-12T20:00:00+00:00"
RESTARTED = "2026-09-12T20:02:00Z"


def state(**changes: object) -> dict[str, Any]:
    return {
        "id": CID,
        "project": PROJECT,
        "service": "scheduler",
        "restart_count": 0,
        "started_at": OLD_START,
        "running": True,
        "health": "healthy",
        "restart_policy": "unless-stopped",
        **changes,
    }


def receipt() -> probe.ProbeReceipt:
    return probe.ProbeReceipt(PROJECT, CID, 0, OLD_START, 7, INJECTED)


class Docker:
    def __init__(self, *states: dict[str, Any]) -> None:
        self.states = list(states)
        self.commands: list[list[str]] = []

    def __call__(self, arguments: list[str]) -> str:
        self.commands.append(arguments)
        if arguments[0] == "compose":
            return CID
        if arguments[0] == "inspect":
            current = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return json.dumps(current)
        assert arguments[:4] == ["exec", CID, probe.PYTHON, "-c"]
        assert arguments[4] == probe.CONTROL_SCRIPT
        return json.dumps({"action": arguments[5], "beat_pid": 7})

    def actions(self) -> list[str]:
        return [arguments[5] for arguments in self.commands if arguments[0] == "exec"]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_probe_stops_only_scoped_child_and_requires_real_healthy_container_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    docker = Docker(
        state(),
        state(),
        state(restart_count=1, started_at=RESTARTED, health="starting"),
        state(restart_count=1, started_at=RESTARTED),
    )
    path = tmp_path / "receipt.json"
    captured = probe.begin_probe(
        ["compose.yml", "compose.ci.yml"],
        PROJECT,
        path,
        allow_fault_injection=True,
        run=docker,
    )
    assert probe.read_receipt(path) == captured
    assert docker.commands[0] == [
        "compose",
        "--project-name",
        PROJECT,
        "--file",
        "compose.yml",
        "--file",
        "compose.ci.yml",
        "ps",
        "--quiet",
        "scheduler",
    ]
    clock = Clock()
    result = probe.verify_probe(
        captured,
        run=docker,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result["healthy"] is True
    assert result["previous_restart_count"] == 0
    assert result["restart_count"] == 1
    assert result["restarted_at"] == RESTARTED
    assert docker.actions() == ["inject", "verify"]
    assert clock.now == 4


@pytest.mark.parametrize(
    "changes",
    (
        {},
        {"started_at": RESTARTED},
        {"restart_count": 1},
        {"restart_count": 1, "started_at": RESTARTED, "health": "unhealthy"},
    ),
)
def test_health_alone_or_restart_alone_never_pass_and_timeout_resumes_only_original_child(
    changes: dict[str, object],
) -> None:
    docker = Docker(state(**changes))
    clock = Clock()
    with pytest.raises(probe.SchedulerProbeError, match="restart healthy"):
        probe.verify_probe(
            receipt(),
            timeout_seconds=3,
            run=docker,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    assert docker.actions() == ["resume"]
    assert docker.commands[-1][-1] == "7"
    assert clock.now == 3


@pytest.mark.parametrize(
    "changes",
    (
        {"project": "customer-production"},
        {"service": "worker"},
        {"restart_policy": "no"},
        {"restart_count": True},
        {"health": "unhealthy"},
    ),
)
def test_wrong_service_identity_or_unhealthy_baseline_is_never_fault_injected(
    changes: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    docker = Docker(state(**changes))
    with pytest.raises(probe.SchedulerProbeError):
        probe.begin_probe(
            ["compose.yml"],
            PROJECT,
            tmp_path / "receipt.json",
            allow_fault_injection=True,
            run=docker,
        )
    assert docker.actions() == []


@pytest.mark.parametrize(
    ("github_actions", "project", "consent"),
    (
        ("false", PROJECT, True),
        ("true", "customer-production", True),
        ("true", PROJECT, False),
    ),
)
def test_explicit_ci_scope_and_fault_flag_are_required_before_any_docker_command(
    github_actions: str,
    project: str,
    consent: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", github_actions)
    docker = Docker()
    with pytest.raises(probe.SchedulerProbeError, match="explicit consent"):
        probe.begin_probe(
            ["compose.yml"],
            project,
            tmp_path / "receipt.json",
            allow_fault_injection=consent,
            run=docker,
        )
    assert docker.commands == []


def test_existing_receipt_cannot_be_overwritten_or_stop_a_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    path = tmp_path / "receipt.json"
    path.write_text("preserve")
    docker = Docker(state())
    with pytest.raises(FileExistsError):
        probe.begin_probe(["compose.yml"], PROJECT, path, allow_fault_injection=True, run=docker)
    assert path.read_text() == "preserve"
    assert docker.actions() == []


def observation(
    status: str, at: str, *, attempts: int = 0, job: str = "queued-job"
) -> dict[str, Any]:
    return {
        "observed_at": at,
        "event": {"event": "job_status", "job_id": job, "status": status, "attempts": attempts},
    }


def test_queue_proof_binds_same_unclaimed_job_before_restart_to_its_success_afterwards(
    tmp_path: Path,
) -> None:
    path = tmp_path / "live.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                observation("queued", "2026-09-12T20:00:05Z"),
                observation("running", "2026-09-12T20:02:05Z", attempts=1),
                observation("succeeded", "2026-09-12T20:02:40Z", attempts=1),
            ]
        )
    )
    result = probe.verify_probe(
        receipt(),
        acceptance_log=path,
        run=Docker(state(restart_count=1, started_at=RESTARTED)),
    )
    assert result["queued_job_id"] == "queued-job"
    assert result["queued_before_restart"] is True


@pytest.mark.parametrize(
    "events",
    (
        [
            observation("queued", "2026-09-12T20:02:01Z"),
            observation("succeeded", "2026-09-12T20:02:40Z", attempts=1),
        ],
        [
            observation("queued", "2026-09-12T20:00:05Z", attempts=1),
            observation("succeeded", "2026-09-12T20:02:40Z", attempts=1),
        ],
        [
            observation("queued", "2026-09-12T20:00:05Z"),
            observation("running", "2026-09-12T20:00:30Z", attempts=1),
            observation("succeeded", "2026-09-12T20:02:40Z", attempts=1),
        ],
        [
            observation("queued", "2026-09-12T20:00:05Z"),
            observation("succeeded", "2026-09-12T20:02:40Z", attempts=1, job="other-job"),
        ],
        [
            observation("queued", "2026-09-12T19:59:05Z"),
            observation("succeeded", "2026-09-12T20:02:40Z", attempts=1),
        ],
    ),
)
def test_queue_proof_rejects_already_dispatched_late_or_unrelated_jobs(
    events: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    path = tmp_path / "live.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events))
    with pytest.raises(probe.SchedulerProbeError, match="No job was observed queued"):
        probe.verify_probe(
            receipt(),
            acceptance_log=path,
            run=Docker(state(restart_count=1, started_at=RESTARTED)),
        )


def test_record_preserves_stdout_and_adds_timezone_bound_receipt_times(tmp_path: Path) -> None:
    line = json.dumps({"event": "job_status", "job_id": "j", "status": "queued", "attempts": 0})
    raw = f"diagnostic line\n{line}\n"
    output = io.StringIO()
    path = tmp_path / "live.jsonl"
    before = datetime.now(UTC)
    probe.record_acceptance(path, io.StringIO(raw), output)
    after = datetime.now(UTC)
    assert output.getvalue() == raw
    captured = json.loads(path.read_text())
    assert captured["event"] == json.loads(line)
    assert before <= datetime.fromisoformat(captured["observed_at"]) <= after


def test_container_fault_control_stops_exact_child_and_ages_only_beat_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []
    touched: list[str] = []
    stopped = [False]

    class FakePath:
        def __init__(self, path: str) -> None:
            self.path = path

        def read_bytes(self) -> bytes:
            return (
                b"python\0-m\0custombuild_worker.scheduler\0"
                if self.path == "/proc/1/cmdline"
                else b"celery\0-A\0custombuild_worker.tasks:celery_app\0beat\0"
            )

        def read_text(self) -> str:
            if self.path == "/proc/1/task/1/children":
                return "7"
            return "State:\tT (stopped)" if stopped[0] else "State:\tS (sleeping)"

        def glob(self, pattern: str) -> list[FakePath]:
            assert self.path == "/tmp" and pattern == "celerybeat-schedule*"
            return [FakePath("/tmp/celerybeat-schedule"), FakePath("/tmp/celerybeat-schedule.db")]

        def is_file(self) -> bool:
            return True

        def is_symlink(self) -> bool:
            return False

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_atime_ns=100, st_mtime_ns=100, st_mtime=100)

    def kill(pid: int, sig: int) -> None:
        signals.append((pid, sig))
        stopped[0] = sig == signal.SIGSTOP

    def utime(path: FakePath, times: tuple[float, float]) -> None:
        touched.append(path.path)
        assert times == (400, 400)

    with monkeypatch.context() as patch:
        patch.setattr("pathlib.Path", FakePath)
        patch.setattr(os, "kill", kill)
        patch.setattr(os, "utime", utime)
        patch.setattr("time.time", lambda: 1000.0)
        patch.setattr(sys, "argv", ["-c", "inject", "0"])
        exec(probe.CONTROL_SCRIPT, {})  # noqa: S102 - execute the fixed container probe against fake OS.
    assert signals == [(7, signal.SIGSTOP)]
    assert touched == ["/tmp/celerybeat-schedule", "/tmp/celerybeat-schedule.db"]
