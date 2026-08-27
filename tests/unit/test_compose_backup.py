import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts import compose_backup
from scripts.compose_backup import (
    POSTGRES_IMAGE,
    SEAWEEDFS_IMAGE,
    VOLUME_INIT_IMAGE,
    BackupError,
    build_manifest,
    create_backup,
    verify_manifest,
)

OBJECT_DIGEST = "0" * 64
OBJECT_ENTRY = {
    "key": "artifact.step",
    "size_bytes": 4,
    "sha256": OBJECT_DIGEST,
    "content_type": "application/step",
    "metadata": {"immutable": "true"},
}
IMAGE_ID = "sha256:" + "1" * 64
SOURCE_MANIFEST_SHA256 = "2" * 64
GIT_REVISION = "3" * 40


def test_backup_runtime_images_match_the_compose_volume_contract() -> None:
    compose = yaml.safe_load(Path("compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert services["postgres"]["image"] == POSTGRES_IMAGE
    assert (
        "./infra/postgres/init-roles.sh:/var/lib/postgres/initdb/10-init-roles.sh:ro"
        in services["postgres"]["volumes"]
    )
    assert services["object-storage-init"]["image"] == VOLUME_INIT_IMAGE
    assert services["object-storage-init"]["user"] == "0:0"


def identity_capture(command: list[str], **_kwargs: object) -> str:
    return IMAGE_ID if "inspect" in command else GIT_REVISION


def backup_metadata() -> dict[str, object]:
    return {
        "compose_project": "custombuild-test",
        "database_snapshot": {
            "captured_at": "2026-08-11T10:00:00+00:00",
            "wal_lsn": "0/16B6C50",
            "alembic_heads": ["0004_design_source_provenance"],
            "row_counts": {"alembic_version": 1, "projects": 1},
        },
        "git_revision": GIT_REVISION,
        "source_manifest_sha256": SOURCE_MANIFEST_SHA256,
        "object_store": {
            "image": SEAWEEDFS_IMAGE,
            "image_id": IMAGE_ID,
            "bucket": "custombuild-artifacts",
            "object_count": 1,
            "total_size_bytes": 4,
            "objects": [OBJECT_ENTRY],
        },
    }


def compose_fixture() -> dict[str, object]:
    return {
        "name": "custombuild-test",
        "services": {
            "postgres": {
                "environment": {
                    "POSTGRES_DB": "custombuild",
                    "POSTGRES_USER": "custombuild_bootstrap",
                }
            },
            "api": {
                "environment": {
                    "S3_PUBLIC_ENDPOINT": "http://localhost:9200",
                    "S3_ACCESS_KEY": "test-key",
                    "S3_SECRET_KEY": "test-secret",
                    "S3_BUCKET": "custombuild-artifacts",
                }
            },
            "worker": {},
            "scheduler": {},
            "object-storage": {"image": SEAWEEDFS_IMAGE},
        },
        "volumes": {"object-storage-data": {"name": "custombuild-test_object-storage-data"}},
    }


def backup_fixture(directory: Path) -> None:
    (directory / "database.dump").write_bytes(b"PGDMP-test")
    (directory / "artifacts.tar").write_bytes(b"ustar-test")
    manifest = build_manifest(directory, backup_metadata())
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("runner_name", ["run", "run_capture"])
def test_subprocess_timeout_is_bounded_and_sanitized(
    runner_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = ["docker", "compose", "--secret=must-not-leak"]
    recorded_timeout: int | None = None

    def hang(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal recorded_timeout
        timeout = kwargs["timeout"]
        assert isinstance(timeout, int)
        recorded_timeout = timeout
        raise subprocess.TimeoutExpired(
            arguments,
            recorded_timeout,
            output=b"must-not-leak",
            stderr=b"must-not-leak",
        )

    monkeypatch.setattr(subprocess, "run", hang)
    runner = getattr(compose_backup, runner_name)

    with pytest.raises(BackupError, match="Backup probe timed out") as caught:
        runner(
            command,
            cwd=tmp_path,
            timeout_seconds=17,
            operation="Backup probe",
        )

    assert recorded_timeout == 17
    assert "must-not-leak" not in str(caught.value)


@pytest.mark.parametrize("runner_name", ["run", "run_capture"])
def test_subprocess_start_failure_is_sanitized(
    runner_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise OSError("must-not-leak")

    monkeypatch.setattr(subprocess, "run", unavailable)
    runner = getattr(compose_backup, runner_name)

    with pytest.raises(BackupError, match="could not be started") as caught:
        runner(["docker", "--secret=must-not-leak"], cwd=tmp_path, operation="Recovery")

    assert "must-not-leak" not in str(caught.value)


def test_compose_config_uses_the_short_configuration_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_capture(
        command: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        operation: str,
    ) -> str:
        captured.update(
            command=command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            operation=operation,
        )
        return '{"services": {}}'

    monkeypatch.setattr(compose_backup, "executable", lambda _name: "docker")
    monkeypatch.setattr(compose_backup, "run_capture", fake_capture)

    assert compose_backup.compose_config(tmp_path, tmp_path / "compose.yml") == {"services": {}}
    assert captured["timeout_seconds"] == compose_backup.CONFIG_COMMAND_TIMEOUT_SECONDS
    assert captured["operation"] == "Compose configuration"


def test_s3_readiness_probe_uses_one_bounded_metadata_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    requests: list[dict[str, object]] = []

    class FakeClient:
        def list_objects_v2(self, **parameters: object) -> dict[str, object]:
            requests.append(parameters)
            return {"Contents": []}

    def fake_boto_client(service: str, **kwargs: object) -> FakeClient:
        captured.update(service=service, **kwargs)
        return FakeClient()

    monkeypatch.setattr("scripts.compose_backup.boto3.client", fake_boto_client)

    compose_backup.probe_s3_readiness(
        "https://objects.example.test",
        "access-key",
        "secret-key",
        "artifacts",
        io_timeout_seconds=0.75,
    )

    config = cast(Any, captured["config"])
    assert captured["service"] == "s3"
    assert config.connect_timeout == 0.75
    assert config.read_timeout == 0.75
    assert config.retries == {
        "total_max_attempts": 1,
        "mode": "standard",
    }
    assert requests == [{"Bucket": "artifacts", "MaxKeys": 1}]


def test_s3_readiness_retries_a_transient_failure_within_one_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[float] = []
    sleeps: list[float] = []

    def transient_probe(*_args: object, io_timeout_seconds: float) -> None:
        attempts.append(io_timeout_seconds)
        if len(attempts) == 1:
            raise BackupError("temporary outage")

    monkeypatch.setattr(compose_backup, "probe_s3_readiness", transient_probe)
    monkeypatch.setattr("scripts.compose_backup.time.sleep", sleeps.append)

    compose_backup.wait_for_s3_readiness(
        "https://objects.example.test",
        "access-key",
        "secret-key",
        "artifacts",
        timeout_seconds=2,
    )

    assert len(attempts) == 2
    assert all(0 < timeout <= 1 for timeout in attempts)
    assert sleeps and 0 < sleeps[0] <= 1


def test_s3_readiness_timeout_obeys_the_outer_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((10.0, 10.0, 11.0))
    attempts: list[float] = []

    def unavailable(*_args: object, io_timeout_seconds: float) -> None:
        attempts.append(io_timeout_seconds)
        raise BackupError("must-not-leak")

    monkeypatch.setattr(compose_backup, "probe_s3_readiness", unavailable)
    monkeypatch.setattr("scripts.compose_backup.time.monotonic", lambda: next(clock))

    with pytest.raises(BackupError, match="before the recovery deadline") as caught:
        compose_backup.wait_for_s3_readiness(
            "https://objects.example.test",
            "access-key",
            "secret-key",
            "artifacts",
            timeout_seconds=1,
        )

    assert attempts == [0.5]
    assert "must-not-leak" not in str(caught.value)


def test_manifest_verifies_both_coordinated_payloads(tmp_path: Path) -> None:
    backup_fixture(tmp_path)

    manifest = verify_manifest(tmp_path)

    assert manifest["order"] == ["database.dump", "artifacts.tar"]
    assert {item["path"] for item in manifest["files"]} == {"database.dump", "artifacts.tar"}


def test_manifest_detects_tampering(tmp_path: Path) -> None:
    backup_fixture(tmp_path)
    (tmp_path / "database.dump").write_bytes(b"tampered")

    with pytest.raises(BackupError, match="checksum mismatch"):
        verify_manifest(tmp_path)


def test_manifest_rejects_legacy_schema(tmp_path: Path) -> None:
    backup_fixture(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "custombuild.compose-backup.v1"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="Unsupported"):
        verify_manifest(tmp_path)


def test_manifest_requires_timezone_bound_creation_time(tmp_path: Path) -> None:
    backup_fixture(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["created_at"] = "2026-08-26T10:00:00"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="no timezone"):
        verify_manifest(tmp_path)


def test_manifest_rejects_payload_path_traversal(tmp_path: Path) -> None:
    backup_fixture(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../database.dump"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="unexpected payload path"):
        verify_manifest(tmp_path)


def test_manifest_rejects_incorrect_object_inventory_totals(tmp_path: Path) -> None:
    backup_fixture(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["object_store"]["object_count"] = 2
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="totals"):
        verify_manifest(tmp_path)


def test_manifest_requires_approved_seaweedfs_image(tmp_path: Path) -> None:
    backup_fixture(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["object_store"]["image"] = "chrislusf/seaweedfs:latest"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="approved SeaweedFS"):
        verify_manifest(tmp_path)


def test_manifest_rejects_ambiguous_length_seaweedfs_revision(tmp_path: Path) -> None:
    backup_fixture(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["object_store"]["image"] = f"custombuild-seaweedfs:{'a' * 41}"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="approved SeaweedFS"):
        verify_manifest(tmp_path)


def test_backup_quiesces_storage_and_restores_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    call_metadata: list[tuple[list[str], int, str]] = []
    inventory = [OBJECT_ENTRY]

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        stdout: object | None = None,
        timeout_seconds: int,
        operation: str,
    ) -> None:
        del cwd
        calls.append(command)
        call_metadata.append((command, timeout_seconds, operation))
        if stdout is not None:
            stdout.write(b"payload")  # type: ignore[attr-defined]

    monkeypatch.setattr(compose_backup, "compose_config", lambda *_: compose_fixture())
    monkeypatch.setattr(compose_backup, "executable", lambda name: name)
    monkeypatch.setattr(compose_backup, "run", fake_run)
    monkeypatch.setattr(compose_backup, "run_capture", identity_capture)
    monkeypatch.setattr(
        compose_backup, "source_manifest_digest", lambda _repo: SOURCE_MANIFEST_SHA256
    )
    monkeypatch.setattr(compose_backup, "inventory_s3", lambda *_: inventory)
    monkeypatch.setattr(compose_backup, "wait_for_s3_readiness", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        compose_backup,
        "database_snapshot",
        lambda *_: backup_metadata()["database_snapshot"],
    )

    create_backup(tmp_path, tmp_path / "compose.yml", tmp_path / "backup")

    commands = [" ".join(command) for command in calls]
    stop_command = (
        "docker compose --file "
        + str((tmp_path / "compose.yml").resolve())
        + " stop object-storage"
    )
    assert stop_command in commands
    assert any(command.endswith("pause scheduler") for command in commands)
    archive_index = next(
        index for index, command in enumerate(calls) if VOLUME_INIT_IMAGE in command
    )
    archive_command = calls[archive_index]
    user_index = archive_command.index("--user")
    assert archive_command[user_index + 1] == "0:0"
    restart_index = next(
        index for index, command in enumerate(commands) if command.endswith("start object-storage")
    )
    unpause_index = next(
        index for index, command in enumerate(commands) if command.endswith("unpause worker")
    )
    assert archive_index < restart_index < unpause_index
    assert any(command.endswith("unpause scheduler") for command in commands)

    pause_budgets = [
        timeout
        for command, timeout, _operation in call_metadata
        if "pause" in command and "unpause" not in command
    ]
    payload_budgets = [
        timeout
        for command, timeout, _operation in call_metadata
        if "pg_dump" in command or VOLUME_INIT_IMAGE in command
    ]
    recovery_budgets = [
        timeout
        for command, timeout, _operation in call_metadata
        if "start" in command or "unpause" in command
    ]
    assert pause_budgets == [compose_backup.SHORT_COMMAND_TIMEOUT_SECONDS] * 3
    assert payload_budgets == [compose_backup.LONG_BACKUP_COMMAND_TIMEOUT_SECONDS] * 2
    assert recovery_budgets == [compose_backup.RECOVERY_TIMEOUT_SECONDS] * 4


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_backup_outputs_remain_owner_only_with_umask_022(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = [OBJECT_ENTRY]

    def fake_run(
        _command: list[str],
        *,
        cwd: Path,
        stdout: object | None = None,
        timeout_seconds: int,
        operation: str,
    ) -> None:
        del cwd, timeout_seconds, operation
        if stdout is not None:
            stdout.write(b"private-payload")  # type: ignore[attr-defined]

    monkeypatch.setattr(compose_backup, "compose_config", lambda *_: compose_fixture())
    monkeypatch.setattr(compose_backup, "executable", lambda name: name)
    monkeypatch.setattr(compose_backup, "run", fake_run)
    monkeypatch.setattr(compose_backup, "run_capture", identity_capture)
    monkeypatch.setattr(
        compose_backup, "source_manifest_digest", lambda _repo: SOURCE_MANIFEST_SHA256
    )
    monkeypatch.setattr(compose_backup, "inventory_s3", lambda *_: inventory)
    monkeypatch.setattr(compose_backup, "wait_for_s3_readiness", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        compose_backup,
        "database_snapshot",
        lambda *_: backup_metadata()["database_snapshot"],
    )
    backup = tmp_path / "private-backup"
    previous_umask = os.umask(0o022)
    try:
        create_backup(tmp_path, tmp_path / "compose.yml", backup)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    for name in ("database.dump", "artifacts.tar", "manifest.json"):
        assert stat.S_IMODE((backup / name).stat().st_mode) == 0o600


def test_backup_bounds_dump_hang_after_pause_and_attempts_every_unpause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], int, str]] = []
    inventory = [OBJECT_ENTRY]

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        stdout: object | None = None,
        timeout_seconds: int,
        operation: str,
    ) -> None:
        del cwd, stdout
        calls.append((command, timeout_seconds, operation))
        if "pg_dump" in command:
            raise BackupError(
                f"{operation} timed out after {timeout_seconds} seconds; retry safely"
            )

    monkeypatch.setattr(compose_backup, "compose_config", lambda *_: compose_fixture())
    monkeypatch.setattr(compose_backup, "executable", lambda name: name)
    monkeypatch.setattr(compose_backup, "run", fake_run)
    monkeypatch.setattr(compose_backup, "inventory_s3", lambda *_: inventory)
    monkeypatch.setattr(
        compose_backup,
        "database_snapshot",
        lambda *_: backup_metadata()["database_snapshot"],
    )

    with pytest.raises(BackupError, match="PostgreSQL dump timed out"):
        create_backup(tmp_path, tmp_path / "compose.yml", tmp_path / "backup")

    dump = next(item for item in calls if "pg_dump" in item[0])
    assert dump[1:] == (
        compose_backup.LONG_BACKUP_COMMAND_TIMEOUT_SECONDS,
        "PostgreSQL dump",
    )
    unpaused = [item for item in calls if "unpause" in item[0]]
    assert [item[0][-1] for item in unpaused] == ["scheduler", "worker", "api"]
    assert all(item[1] == compose_backup.RECOVERY_TIMEOUT_SECONDS for item in unpaused)


def test_pause_timeout_still_attempts_bounded_unpause_for_that_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], int, str]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        stdout: object | None = None,
        timeout_seconds: int,
        operation: str,
    ) -> None:
        del cwd, stdout
        calls.append((command, timeout_seconds, operation))
        if command[-2:] == ["pause", "api"]:
            raise BackupError(
                f"{operation} timed out after {timeout_seconds} seconds; retry safely"
            )

    monkeypatch.setattr(compose_backup, "compose_config", lambda *_: compose_fixture())
    monkeypatch.setattr(compose_backup, "executable", lambda name: name)
    monkeypatch.setattr(compose_backup, "run", fake_run)

    with pytest.raises(BackupError, match="Pause api timed out"):
        create_backup(tmp_path, tmp_path / "compose.yml", tmp_path / "backup")

    assert calls[0][0][-2:] == ["pause", "api"]
    assert calls[0][1:] == (compose_backup.SHORT_COMMAND_TIMEOUT_SECONDS, "Pause api")
    assert calls[1][0][-2:] == ["unpause", "api"]
    assert calls[1][1:] == (compose_backup.RECOVERY_TIMEOUT_SECONDS, "Unpause api")
    assert len(calls) == 2


def test_backup_bounds_archive_hang_after_stop_and_attempts_full_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], int, str]] = []
    recovery_readiness_budgets: list[int] = []
    full_inventory_calls = 0
    inventory = [OBJECT_ENTRY]

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        stdout: object | None = None,
        timeout_seconds: int,
        operation: str,
    ) -> None:
        del cwd
        calls.append((command, timeout_seconds, operation))
        if VOLUME_INIT_IMAGE in command:
            raise BackupError(
                f"{operation} timed out after {timeout_seconds} seconds; retry safely"
            )
        if stdout is not None:
            stdout.write(b"payload")  # type: ignore[attr-defined]

    def full_inventory(*_args: object) -> list[dict[str, object]]:
        nonlocal full_inventory_calls
        full_inventory_calls += 1
        if full_inventory_calls > 1:
            raise AssertionError("recovery must not download or hash object bodies")
        return inventory

    def recovered_readiness(*_args: object, timeout_seconds: int) -> None:
        recovery_readiness_budgets.append(timeout_seconds)

    monkeypatch.setattr(compose_backup, "compose_config", lambda *_: compose_fixture())
    monkeypatch.setattr(compose_backup, "executable", lambda name: name)
    monkeypatch.setattr(compose_backup, "run", fake_run)
    monkeypatch.setattr(compose_backup, "inventory_s3", full_inventory)
    monkeypatch.setattr(compose_backup, "wait_for_s3_readiness", recovered_readiness)
    monkeypatch.setattr(
        compose_backup,
        "database_snapshot",
        lambda *_: backup_metadata()["database_snapshot"],
    )

    with pytest.raises(BackupError, match="Object-storage archive timed out"):
        create_backup(tmp_path, tmp_path / "compose.yml", tmp_path / "backup")

    commands = [" ".join(command) for command, _timeout, _operation in calls]
    restart_index = next(
        index for index, command in enumerate(commands) if command.endswith("start object-storage")
    )
    unpause_index = next(
        index for index, command in enumerate(commands) if command.endswith("unpause worker")
    )
    assert restart_index < unpause_index
    archive = next(item for item in calls if VOLUME_INIT_IMAGE in item[0])
    assert archive[1:] == (
        compose_backup.LONG_BACKUP_COMMAND_TIMEOUT_SECONDS,
        "Object-storage archive",
    )
    recovery_commands = [item for item in calls if "start" in item[0] or "unpause" in item[0]]
    assert all(item[1] == compose_backup.RECOVERY_TIMEOUT_SECONDS for item in recovery_commands)
    assert [item[0][-1] for item in recovery_commands if "unpause" in item[0]] == [
        "scheduler",
        "worker",
        "api",
    ]
    assert full_inventory_calls == 1
    assert recovery_readiness_budgets == [compose_backup.RECOVERY_TIMEOUT_SECONDS]
