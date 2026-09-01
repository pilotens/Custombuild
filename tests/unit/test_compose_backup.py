import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from app.config_guards import validate_production_s3_bucket

from scripts import compose_backup
from scripts.compose_backup import (
    POSTGRES_IMAGE,
    SEAWEEDFS_IMAGE,
    TOMBSTONE_HISTORY_SCHEMA,
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
WORKER_CONTAINER_ID = "4" * 64
MAINTENANCE_WORKER_CONTAINER_ID = "5" * 64
STORAGE_REAPER_WORKER_CONTAINER_ID = "6" * 64
EMPTY_TOMBSTONE_DIGEST = hashlib.sha256(b"[]").hexdigest()


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
    if command[-1:] == ["worker"] and "ps" in command:
        return WORKER_CONTAINER_ID
    if command[-1:] == ["maintenance-worker"] and "ps" in command:
        return MAINTENANCE_WORKER_CONTAINER_ID
    if command[-1:] == ["storage-reaper-worker"] and "ps" in command:
        return STORAGE_REAPER_WORKER_CONTAINER_ID
    if "{{json .State}}" in command:
        return json.dumps({"Health": {"Status": "healthy"}, "Status": "running"})
    return IMAGE_ID if "inspect" in command else GIT_REVISION


def backup_metadata() -> dict[str, object]:
    return {
        "compose_project": "custombuild-test",
        "database_snapshot": {
            "captured_at": "2026-08-11T10:00:00+00:00",
            "wal_lsn": "0/16B6C50",
            "alembic_heads": ["0004_design_source_provenance"],
            "row_counts": {
                "alembic_version": 1,
                "projects": 1,
                "storage_object_tombstones": 0,
            },
            "tombstone_history": {
                "schema_version": TOMBSTONE_HISTORY_SCHEMA,
                "count": 0,
                "sha256": EMPTY_TOMBSTONE_DIGEST,
            },
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
            "api": {"environment": {}},
            "worker": {},
            "maintenance-worker": {},
            "storage-reaper-worker": {},
            "scheduler": {},
            "storage-recovery": {},
            "storage-capacity-attestor": {},
            "object-storage": {
                "image": SEAWEEDFS_IMAGE,
                "environment": {
                    "S3_BACKUP_ENDPOINT": "http://127.0.0.1:9200",
                    "AWS_ACCESS_KEY_ID": "test-key",
                    "AWS_SECRET_ACCESS_KEY": "test-secret",
                    "S3_BUCKET": "custombuild-artifacts",
                },
                "ports": [{"host_ip": "127.0.0.1", "published": "9200", "target": 8333}],
            },
        },
        "volumes": {"object-storage-data": {"name": "custombuild-test_object-storage-data"}},
    }


@pytest.mark.parametrize(
    ("endpoint", "ports"),
    (
        (
            "http://127.0.0.1:9999",
            [{"host_ip": "127.0.0.1", "published": "9200", "target": 8333}],
        ),
        (
            "http://127.0.0.1:9200",
            [{"host_ip": "127.0.0.1", "published": "9200", "target": 9001}],
        ),
        (
            "https://127.0.0.1:9200",
            [{"host_ip": "127.0.0.1", "published": "9200", "target": 8333}],
        ),
        (
            "\x00http://127.0.0.1:9200",
            [{"host_ip": "127.0.0.1", "published": "9200", "target": 8333}],
        ),
        (
            "\nhttp://127.0.0.1:9200",
            [{"host_ip": "127.0.0.1", "published": "9200", "target": 8333}],
        ),
        (
            "http://127.0.0.1:\t9200",
            [{"host_ip": "127.0.0.1", "published": "9200", "target": 8333}],
        ),
        (
            "http:\\//127.0.0.1:9200",
            [{"host_ip": "127.0.0.1", "published": "9200", "target": 8333}],
        ),
        (
            "http://127.0.0.1:0",
            [{"host_ip": "127.0.0.1", "published": 0, "target": 8333}],
        ),
        (
            "http://127.0.0.1:65536",
            [{"host_ip": "127.0.0.1", "published": 65_536, "target": 8333}],
        ),
        (
            "http://127.0.0.1:9200",
            [{"host_ip": "127.0.0.1", "published": 0, "target": 8333}],
        ),
        (
            "http://127.0.0.1:9200",
            [{"host_ip": "127.0.0.1", "published": 65_536, "target": 8333}],
        ),
        (
            "http://127.0.0.1:9200",
            [{"host_ip": "127.0.0.1", "published": True, "target": 8333}],
        ),
        (
            "http://127.0.0.1:9200",
            [{"host_ip": "127.0.0.1", "published": "-1", "target": 8333}],
        ),
        (
            "http://127.0.0.1:9200",
            [{"host_ip": "127.0.0.1", "published": 9200, "target": 0}],
        ),
        (
            "http://127.0.0.1:9200",
            [{"host_ip": "127.0.0.1", "published": 9200, "target": 65_536}],
        ),
        (
            "http://127.0.0.1:9200",
            [{"host_ip": "127.0.0.1", "published": 9200, "target": True}],
        ),
        (
            "http://127.0.0.1:9200",
            [{"host_ip": "127.0.0.1", "published": 9200, "target": "-1"}],
        ),
    ),
)
def test_storage_resolution_rejects_endpoint_and_published_socket_drift(
    endpoint: str,
    ports: list[dict[str, object]],
) -> None:
    config = compose_fixture()
    storage_service = cast(
        dict[str, Any],
        cast(dict[str, Any], config["services"])["object-storage"],
    )
    cast(dict[str, str], storage_service["environment"])["S3_BACKUP_ENDPOINT"] = endpoint
    storage_service["ports"] = ports

    with pytest.raises(BackupError, match="must match its loopback S3 port"):
        compose_backup._resolved_storage(cast(dict[str, Any], config))


@pytest.mark.parametrize(
    ("bucket", "valid"),
    (
        pytest.param("abc", True, id="minimum-length"),
        pytest.param("a.b-c", True, id="interior-dot-and-hyphen"),
        pytest.param("a" * 63, True, id="maximum-length"),
        pytest.param(".", False, id="single-dot"),
        pytest.param("..", False, id="double-dot"),
        pytest.param("a..b", False, id="consecutive-dots"),
        pytest.param("Production-artifacts", False, id="uppercase"),
        pytest.param("production_artifacts", False, id="underscore"),
        pytest.param("192.0.2.1", False, id="ipv4-literal"),
        pytest.param("ab", False, id="below-minimum-length"),
        pytest.param("a" * 64, False, id="above-maximum-length"),
    ),
)
def test_storage_resolution_matches_the_production_bucket_guard(
    bucket: str,
    valid: bool,
) -> None:
    config = compose_fixture()
    storage_service = cast(
        dict[str, Any],
        cast(dict[str, Any], config["services"])["object-storage"],
    )
    cast(dict[str, str], storage_service["environment"])["S3_BUCKET"] = bucket

    if valid:
        validate_production_s3_bucket(bucket)
        assert compose_backup._resolved_storage(cast(dict[str, Any], config))[-1] == bucket
        return

    with pytest.raises(ValueError, match="canonical S3 DNS name"):
        validate_production_s3_bucket(bucket)
    with pytest.raises(BackupError, match="canonical S3 DNS name"):
        compose_backup._resolved_storage(cast(dict[str, Any], config))


def test_backup_rejects_noncanonical_bucket_before_writer_or_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = compose_fixture()
    storage_service = cast(
        dict[str, Any],
        cast(dict[str, Any], config["services"])["object-storage"],
    )
    cast(dict[str, str], storage_service["environment"])["S3_BUCKET"] = "192.0.2.1"
    io_calls: list[str] = []

    def must_not_run(*_args: object, **_kwargs: object) -> None:
        io_calls.append("writer")
        raise AssertionError("writers changed before bucket validation")

    def must_not_open_provider(*_args: object, **_kwargs: object) -> None:
        io_calls.append("provider")
        raise AssertionError("provider opened before bucket validation")

    monkeypatch.setattr(compose_backup, "compose_config", lambda *_args: config)
    monkeypatch.setattr(compose_backup, "run", must_not_run)
    monkeypatch.setattr(compose_backup, "inventory_s3", must_not_open_provider)
    monkeypatch.setattr(compose_backup, "_s3_client", must_not_open_provider)

    with pytest.raises(BackupError, match="canonical S3 DNS name"):
        create_backup(tmp_path, tmp_path / "compose.yml", tmp_path / "backup")

    assert io_calls == []


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


@pytest.mark.parametrize(
    "mutation",
    ("missing", "wrong_count", "wrong_digest", "extra_field", "bool_count"),
)
def test_manifest_requires_exact_tombstone_history(
    mutation: str,
    tmp_path: Path,
) -> None:
    backup_fixture(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    snapshot = manifest["database_snapshot"]
    history = snapshot["tombstone_history"]
    if mutation == "missing":
        del snapshot["tombstone_history"]
    elif mutation == "wrong_count":
        history["count"] = 1
    elif mutation == "wrong_digest":
        history["sha256"] = "not-a-digest"
    elif mutation == "extra_field":
        history["rows"] = []
    elif mutation == "bool_count":
        history["count"] = False
    else:  # pragma: no cover - parameter list is exhaustive.
        raise AssertionError(mutation)
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="tombstone"):
        verify_manifest(tmp_path)


def test_database_snapshot_hashes_every_tombstone_identity_column(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_sql = ""

    def fake_capture(
        command: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        operation: str,
    ) -> str:
        nonlocal captured_sql
        del cwd, timeout_seconds, operation
        captured_sql = command[-1]
        return json.dumps(backup_metadata()["database_snapshot"])

    monkeypatch.setattr(compose_backup, "run_capture", fake_capture)

    snapshot = compose_backup.database_snapshot(
        ["docker", "compose"], tmp_path, "custombuild", "custombuild_migrator"
    )

    assert (
        snapshot["tombstone_history"] == backup_metadata()["database_snapshot"]["tombstone_history"]
    )
    for column in (
        "capacity_bucket",
        "object_key",
        "organization_id",
        "project_id",
        "sha256",
        "size_bytes",
        "media_type",
        "owner_type",
        "owner_id",
        "idempotency_key",
        "accounting_state",
        "claim_token",
        "retired_at",
    ):
        assert f"tombstone.{column}" in captured_sql
    assert 'ORDER BY tombstone.capacity_bucket COLLATE "C"' in captured_sql
    assert 'tombstone.object_key COLLATE "C"' in captured_sql
    assert TOMBSTONE_HISTORY_SCHEMA in captured_sql
    assert "sha256(" in captured_sql
    assert "convert_to(" in captured_sql


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


def test_active_reservations_drain_before_recovery_and_exact_capture_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def fake_run(
        _command: list[str],
        *,
        cwd: Path,
        stdout: object | None = None,
        timeout_seconds: int,
        operation: str,
    ) -> None:
        del cwd, timeout_seconds
        events.append(operation)
        if stdout is not None:
            stdout.write(b"payload")  # type: ignore[attr-defined]

    def fake_inventory(*_args: object) -> list[dict[str, object]]:
        events.append("S3 inventory")
        return [OBJECT_ENTRY]

    def fake_snapshot(*_args: object) -> object:
        events.append("PostgreSQL recovery point")
        return backup_metadata()["database_snapshot"]

    monkeypatch.setattr(compose_backup, "compose_config", lambda *_: compose_fixture())
    monkeypatch.setattr(compose_backup, "executable", lambda name: name)
    monkeypatch.setattr(compose_backup, "run", fake_run)
    monkeypatch.setattr(compose_backup, "run_capture", identity_capture)
    monkeypatch.setattr(
        compose_backup, "source_manifest_digest", lambda _repo: SOURCE_MANIFEST_SHA256
    )
    monkeypatch.setattr(compose_backup, "inventory_s3", fake_inventory)
    monkeypatch.setattr(compose_backup, "database_snapshot", fake_snapshot)
    monkeypatch.setattr(
        compose_backup,
        "wait_for_s3_readiness",
        lambda *_args, **_kwargs: events.append("S3 ready"),
    )

    create_backup(tmp_path, tmp_path / "compose.yml", tmp_path / "backup")

    worker_drain = events.index("Drain and stop worker")
    storage_recovery = events.index("Storage recovery gate")
    prepare_refresh = events.index("Record storage capacity refresh baseline")
    request_refresh = events.index("Request fresh storage capacity evidence")
    wait_refresh = events.index("Wait for fresh storage capacity evidence")
    pause_attestor = events.index("Pause storage capacity attestor")
    first_inventory = events.index("S3 inventory")
    assert worker_drain < storage_recovery
    assert storage_recovery < prepare_refresh < request_refresh < wait_refresh
    assert wait_refresh < pause_attestor < first_inventory

    refresh_waits = [
        index
        for index, event in enumerate(events)
        if event == "Wait for fresh storage capacity evidence"
    ]
    worker_start = events.index("Start worker")
    scheduler_unpause = events.index("Unpause scheduler")
    api_unpause = events.index("Unpause api")
    assert len(refresh_waits) == 2
    assert refresh_waits[-1] < worker_start < scheduler_unpause < api_unpause


@pytest.mark.parametrize(
    "failure",
    (
        BackupError("storage recovery exited unsuccessfully"),
        BackupError("Storage recovery gate timed out after 1260 seconds"),
    ),
)
def test_failed_or_unknown_storage_recovery_prevents_capture_and_writer_restart(
    failure: BackupError,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int, str]] = []
    inventories = 0

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
        if operation == "Storage recovery gate":
            raise failure

    def forbidden_inventory(*_args: object) -> list[dict[str, object]]:
        nonlocal inventories
        inventories += 1
        return [OBJECT_ENTRY]

    monkeypatch.setattr(compose_backup, "compose_config", lambda *_: compose_fixture())
    monkeypatch.setattr(compose_backup, "executable", lambda name: name)
    monkeypatch.setattr(compose_backup, "run", fake_run)
    monkeypatch.setattr(compose_backup, "run_capture", identity_capture)
    monkeypatch.setattr(compose_backup, "inventory_s3", forbidden_inventory)

    with pytest.raises(
        BackupError,
        match="writers remain stopped or paused because storage recovery did not complete",
    ):
        create_backup(tmp_path, tmp_path / "compose.yml", tmp_path / "backup")

    gate = next(item for item in calls if item[2] == "Storage recovery gate")
    assert gate[0][-6:] == [
        "run",
        "--rm",
        "--no-deps",
        "-e",
        "STORAGE_RECOVERY_TIMEOUT_SECONDS=1200",
        "storage-recovery",
    ]
    assert gate[1] == compose_backup.STORAGE_RECOVERY_COMMAND_TIMEOUT_SECONDS
    assert inventories == 0
    assert not any(
        item[2]
        in {
            "Record storage capacity refresh baseline",
            "Request fresh storage capacity evidence",
            "Wait for fresh storage capacity evidence",
            "Pause storage capacity attestor",
            "Start worker",
            "Unpause scheduler",
            "Unpause api",
        }
        for item in calls
    )


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
    worker_start = next(item for item in call_metadata if item[2] == "Start worker")
    worker_start_index = call_metadata.index(worker_start)
    assert worker_start[0] == [
        "docker",
        "container",
        "start",
        WORKER_CONTAINER_ID,
    ]
    assert worker_start[1] == compose_backup.RECOVERY_TIMEOUT_SECONDS
    assert not any(
        "compose" in command and "up" in command and command[-1:] == ["worker"]
        for command in calls
    )
    scheduler_unpause_index = next(
        index for index, command in enumerate(commands) if command.endswith("unpause scheduler")
    )
    api_unpause_index = next(
        index for index, command in enumerate(commands) if command.endswith("unpause api")
    )
    assert archive_index < restart_index < worker_start_index
    assert worker_start_index < scheduler_unpause_index < api_unpause_index
    assert any(command.endswith("unpause scheduler") for command in commands)

    worker_stop = next(item for item in call_metadata if item[2] == "Drain and stop worker")
    assert worker_stop[0][-4:] == [
        "stop",
        "--timeout",
        str(compose_backup.WORKER_DRAIN_GRACE_SECONDS),
        "worker",
    ]
    assert worker_stop[1] == compose_backup.WORKER_DRAIN_COMMAND_TIMEOUT_SECONDS
    storage_recovery = next(item for item in call_metadata if item[2] == "Storage recovery gate")
    assert storage_recovery[0][-6:] == [
        "run",
        "--rm",
        "--no-deps",
        "-e",
        "STORAGE_RECOVERY_TIMEOUT_SECONDS=1200",
        "storage-recovery",
    ]
    assert storage_recovery[1] == compose_backup.STORAGE_RECOVERY_COMMAND_TIMEOUT_SECONDS

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
    assert recovery_budgets == [compose_backup.RECOVERY_TIMEOUT_SECONDS] * 7


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
    monkeypatch.setattr(compose_backup, "run_capture", identity_capture)
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
    assert [item[0][-1] for item in unpaused] == [
        "storage-capacity-attestor",
        "scheduler",
        "api",
    ]
    assert all(item[1] == compose_backup.RECOVERY_TIMEOUT_SECONDS for item in unpaused)
    worker_start_index = next(
        index for index, item in enumerate(calls) if item[2] == "Start worker"
    )
    scheduler_unpause_index = next(
        index for index, item in enumerate(calls) if item[2] == "Unpause scheduler"
    )
    assert worker_start_index < scheduler_unpause_index


def test_capacity_refresh_failure_keeps_application_writers_paused(
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
        if operation == "PostgreSQL dump":
            raise BackupError("dump failed")
        if operation == "Wait for fresh storage capacity evidence":
            raise BackupError("capacity refresh failed")

    monkeypatch.setattr(compose_backup, "compose_config", lambda *_: compose_fixture())
    monkeypatch.setattr(compose_backup, "executable", lambda name: name)
    monkeypatch.setattr(compose_backup, "run", fake_run)
    monkeypatch.setattr(compose_backup, "run_capture", identity_capture)
    monkeypatch.setattr(compose_backup, "inventory_s3", lambda *_: [OBJECT_ENTRY])
    monkeypatch.setattr(
        compose_backup,
        "database_snapshot",
        lambda *_: backup_metadata()["database_snapshot"],
    )

    with pytest.raises(
        BackupError,
        match="writers remain stopped or paused because the pre-capture storage gate failed",
    ):
        create_backup(tmp_path, tmp_path / "compose.yml", tmp_path / "backup")

    assert any(
        operation == "Wait for fresh storage capacity evidence"
        for _command, _timeout, operation in calls
    )
    assert not any(
        command[-2:] in (["unpause", "api"], ["unpause", "worker"], ["unpause", "scheduler"])
        for command, _timeout, _operation in calls
    )


def test_post_capture_capacity_failure_prevents_every_writer_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int, str]] = []
    refresh_waits = 0

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        stdout: object | None = None,
        timeout_seconds: int,
        operation: str,
    ) -> None:
        nonlocal refresh_waits
        del cwd
        calls.append((command, timeout_seconds, operation))
        if stdout is not None:
            stdout.write(b"payload")  # type: ignore[attr-defined]
        if operation == "Wait for fresh storage capacity evidence":
            refresh_waits += 1
            if refresh_waits == 2:
                raise BackupError("post-capture capacity refresh failed")

    monkeypatch.setattr(compose_backup, "compose_config", lambda *_: compose_fixture())
    monkeypatch.setattr(compose_backup, "executable", lambda name: name)
    monkeypatch.setattr(compose_backup, "run", fake_run)
    monkeypatch.setattr(compose_backup, "run_capture", identity_capture)
    monkeypatch.setattr(
        compose_backup, "source_manifest_digest", lambda _repo: SOURCE_MANIFEST_SHA256
    )
    monkeypatch.setattr(compose_backup, "inventory_s3", lambda *_: [OBJECT_ENTRY])
    monkeypatch.setattr(compose_backup, "wait_for_s3_readiness", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        compose_backup,
        "database_snapshot",
        lambda *_: backup_metadata()["database_snapshot"],
    )

    with pytest.raises(
        BackupError,
        match="storage capacity refresh failed.*writers remain stopped or paused",
    ):
        create_backup(tmp_path, tmp_path / "compose.yml", tmp_path / "backup")

    assert refresh_waits == 2
    assert any(item[2] == "PostgreSQL dump" for item in calls)
    assert not any(
        item[2] in {"Start worker", "Unpause scheduler", "Unpause api"} for item in calls
    )


def test_writer_restart_failure_rolls_back_scheduler_api_and_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        del cwd
        calls.append((command, timeout_seconds, operation))
        if stdout is not None:
            stdout.write(b"payload")  # type: ignore[attr-defined]
        if operation == "Unpause api":
            raise BackupError("Docker client timed out with unknown API pause state")

    monkeypatch.setattr(compose_backup, "compose_config", lambda *_: compose_fixture())
    monkeypatch.setattr(compose_backup, "executable", lambda name: name)
    monkeypatch.setattr(compose_backup, "run", fake_run)
    monkeypatch.setattr(compose_backup, "run_capture", identity_capture)
    monkeypatch.setattr(
        compose_backup, "source_manifest_digest", lambda _repo: SOURCE_MANIFEST_SHA256
    )
    monkeypatch.setattr(compose_backup, "inventory_s3", lambda *_: [OBJECT_ENTRY])
    monkeypatch.setattr(compose_backup, "wait_for_s3_readiness", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        compose_backup,
        "database_snapshot",
        lambda *_: backup_metadata()["database_snapshot"],
    )

    with pytest.raises(
        BackupError,
        match="writer restart failed.*returned to a stopped or paused state",
    ):
        create_backup(tmp_path, tmp_path / "compose.yml", tmp_path / "backup")

    operations = [operation for _command, _timeout, operation in calls]
    assert operations.index("Start worker") < operations.index("Unpause scheduler")
    assert operations.index("Unpause scheduler") < operations.index("Unpause api")
    assert operations.index("Unpause api") < operations.index("Re-pause api after recovery failure")
    assert operations.index("Re-pause api after recovery failure") < operations.index(
        "Re-pause scheduler after recovery failure"
    )
    assert operations.index("Re-pause scheduler after recovery failure") < operations.index(
        "Fail-closed stop worker after recovery failure"
    )


def test_pause_timeout_stops_every_writer_and_never_opens_the_recovery_gate(
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
    monkeypatch.setattr(compose_backup, "run_capture", identity_capture)

    with pytest.raises(BackupError, match="Pause api timed out"):
        create_backup(tmp_path, tmp_path / "compose.yml", tmp_path / "backup")

    assert calls[0][0][-2:] == ["pause", "api"]
    assert calls[0][1:] == (compose_backup.SHORT_COMMAND_TIMEOUT_SECONDS, "Pause api")
    assert calls[1][0][-4:] == [
        "stop",
        "--timeout",
        str(compose_backup.FAIL_CLOSED_STOP_GRACE_SECONDS),
        "api",
    ]
    assert calls[1][1:] == (
        compose_backup.RECOVERY_TIMEOUT_SECONDS,
        "Fail-closed stop api",
    )
    assert any(item[2] == "Pause scheduler" for item in calls)
    assert any(item[2] == "Drain and stop worker" for item in calls)
    assert not any(item[2] == "Storage recovery gate" for item in calls)
    assert not any(
        "start" in command or "unpause" in command for command, _timeout, _operation in calls
    )


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
    monkeypatch.setattr(compose_backup, "run_capture", identity_capture)
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
    worker_start_index = next(
        index for index, item in enumerate(calls) if item[2] == "Start worker"
    )
    assert restart_index < worker_start_index
    archive = next(item for item in calls if VOLUME_INIT_IMAGE in item[0])
    assert archive[1:] == (
        compose_backup.LONG_BACKUP_COMMAND_TIMEOUT_SECONDS,
        "Object-storage archive",
    )
    recovery_commands = [
        item
        for item in calls
        if "start" in item[0] or "unpause" in item[0]
    ]
    assert all(item[1] == compose_backup.RECOVERY_TIMEOUT_SECONDS for item in recovery_commands)
    assert [item[0][-1] for item in recovery_commands if "unpause" in item[0]] == [
        "storage-capacity-attestor",
        "scheduler",
        "api",
    ]
    assert [
        item[0][-1]
        for item in recovery_commands
        if "start" in item[0]
    ] == [
        "object-storage",
        WORKER_CONTAINER_ID,
        MAINTENANCE_WORKER_CONTAINER_ID,
        STORAGE_REAPER_WORKER_CONTAINER_ID,
    ]
    assert full_inventory_calls == 1
    assert recovery_readiness_budgets == [compose_backup.RECOVERY_TIMEOUT_SECONDS]

@pytest.mark.parametrize(
    "raw_container_id",
    (
        "",
        "a" * 63,
        "sha256:" + "a" * 64,
        "a" * 64 + "\n" + "b" * 64,
    ),
)
def test_worker_container_identity_fails_closed_on_ambiguous_or_invalid_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_container_id: str,
) -> None:
    monkeypatch.setattr(
        compose_backup,
        "run_capture",
        lambda *_args, **_kwargs: raw_container_id,
    )

    with pytest.raises(
        BackupError,
        match="worker container identity is missing or ambiguous",
    ):
        compose_backup._worker_container_id(["docker", "compose"], tmp_path)


def test_exact_worker_restart_rejects_container_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[list[str]] = []
    monkeypatch.setattr(
        compose_backup,
        "_worker_container_id",
        lambda *_args: "5" * 64,
    )
    monkeypatch.setattr(
        compose_backup,
        "run",
        lambda command, **_kwargs: starts.append(command),
    )

    with pytest.raises(BackupError, match="identity changed"):
        compose_backup._start_exact_worker(
            "docker",
            ["docker", "compose"],
            tmp_path,
            WORKER_CONTAINER_ID,
        )

    assert starts == []


def test_worker_health_wait_has_one_bounded_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    inspect_budgets: list[int] = []
    monkeypatch.setattr(compose_backup.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        compose_backup.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    def starting_state(
        _docker: str,
        _repo: Path,
        _container_id: str,
        *,
        timeout_seconds: int,
    ) -> tuple[str, str]:
        inspect_budgets.append(timeout_seconds)
        return "running", "starting"

    monkeypatch.setattr(compose_backup, "_worker_runtime_state", starting_state)

    with pytest.raises(BackupError, match="did not become healthy"):
        compose_backup._wait_for_worker_health(
            "docker",
            tmp_path,
            WORKER_CONTAINER_ID,
            timeout_seconds=2,
        )

    assert inspect_budgets == [2, 1]
    assert now == [2.0]
