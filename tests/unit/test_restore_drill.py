import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from scripts import restore_drill
from scripts.compose_backup import TOMBSTONE_HISTORY_SCHEMA, BackupError
from scripts.postgres_runtime_privileges import runtime_privileges_sql
from scripts.restore_drill import current_alembic_heads, validate_temporary_name


def test_disposable_restore_name_is_narrowly_scoped() -> None:
    validate_temporary_name("custombuild-restore-deadbeef")
    validate_temporary_name("custombuild-restore-deadbeef-postgres")
    validate_temporary_name("custombuild-restore-deadbeef-seaweed")
    validate_temporary_name("custombuild-restore-deadbeef-extract")


@pytest.mark.parametrize(
    "name",
    [
        "custombuild-prod",
        "custombuild-restore-",
        "custombuild-restore-../../",
        "custombuild-restore-deadbeef-other",
        "restore-deadbeef",
    ],
)
def test_broad_or_unsafe_restore_names_are_rejected(name: str) -> None:
    with pytest.raises(BackupError, match="Unsafe"):
        validate_temporary_name(name)


def test_restore_drill_resolves_current_repository_alembic_head() -> None:
    assert current_alembic_heads(Path.cwd()) == ["0020_release_cam_approval_identity"]


def test_restore_prepares_schema_ownership_and_restores_as_migrator() -> None:
    source = Path("scripts/restore_drill.py").read_text(encoding="utf-8")

    assert "ALTER SCHEMA public OWNER TO custombuild_migrator" in source
    assert '"--username",\n            "custombuild_migrator"' in source
    assert '"PGPASSWORD={RESTORE_MIGRATOR_PASSWORD}"' in source


def test_restore_rebuilds_the_canonical_runtime_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, **_kwargs: object) -> str:
        calls.append(arguments)
        return ""

    monkeypatch.setattr(restore_drill, "docker", fake_docker)

    restore_drill._restore_runtime_privileges("custombuild-restore-deadbeef-postgres")

    assert len(calls) == 1
    sql = calls[0][-1]
    assert sql.endswith(runtime_privileges_sql())
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES" not in sql
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES" not in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES" not in sql
    assert "GRANT USAGE, SELECT ON SEQUENCES" not in sql


def test_restore_probe_recomputes_the_canonical_full_tombstone_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_sql = ""
    history = {
        "schema_version": TOMBSTONE_HISTORY_SCHEMA,
        "count": 0,
        "sha256": hashlib.sha256(b"[]").hexdigest(),
    }

    def fake_docker(*arguments: str, **_kwargs: object) -> str:
        nonlocal captured_sql
        captured_sql = arguments[-1]
        return json.dumps(
            {
                "alembic_heads": ["0017_oidc_issuer_binding"],
                "row_counts": {"storage_object_tombstones": 0},
                "tombstone_history": history,
            }
        )

    monkeypatch.setattr(restore_drill, "docker", fake_docker)

    probe = restore_drill._restored_database_probe("custombuild-restore-deadbeef-postgres")

    assert probe["tombstone_history"] == history
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


def test_restore_rejects_count_preserving_tombstone_identity_substitution() -> None:
    expected = {
        "tombstone_history": {
            "schema_version": TOMBSTONE_HISTORY_SCHEMA,
            "count": 1,
            "sha256": "a" * 64,
        }
    }
    restored = {
        "tombstone_history": {
            "schema_version": TOMBSTONE_HISTORY_SCHEMA,
            "count": 1,
            "sha256": "b" * 64,
        }
    }

    with pytest.raises(BackupError, match="tombstone history"):
        restore_drill._verified_restored_tombstone_history(expected, restored)


def test_restore_proves_exact_tombstone_acl_and_v4_evidence() -> None:
    source = Path("scripts/restore_drill.py").read_text(encoding="utf-8")

    assert 'RESTORE_SCHEMA = "custombuild.restore-drill.v4"' in source
    for role in ("custombuild_api", "custombuild_worker", "custombuild_storage_attestor"):
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
            "MAINTAIN",
        ):
            pattern = (
                r"has_table_privilege\(\s*'"
                + role
                + r"',\s*'public\.storage_object_tombstones',\s*'"
                + privilege
                + r"'\s*\)"
            )
            assert re.search(pattern, source) is not None
    assert "CROSS JOIN LATERAL aclexplode(attribute.attacl)" in source
    assert '"tombstone_column_grants_absent": True' in source
    assert '"database_tombstone_history_verified": True' in source


def test_restore_worker_probe_uses_only_a_worker_allowlisted_table() -> None:
    source = Path("scripts/restore_drill.py").read_text(encoding="utf-8")
    worker_probe = source.split("worker_query =", maxsplit=1)[1].split("worker_raw =", maxsplit=1)[
        0
    ]

    assert "FROM outbox_events" in worker_probe
    assert "FROM projects" not in worker_probe
    assert "public_object_grants_absent" in source
    assert "FROM pg_auth_members" in source


def test_restore_evidence_requires_acceptance_before_traffic() -> None:
    source = Path("scripts/restore_drill.py").read_text(encoding="utf-8")

    assert '"tenant_acceptance_required_before_traffic": True' in source


def test_postgres_wait_ignores_temporary_initialization_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    log_attempts = 0

    def fake_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal log_attempts
        assert 0 < float(kwargs["timeout"]) <= restore_drill.DOCKER_PROBE_TIMEOUT_SECONDS
        calls.append(arguments)
        if arguments[1] == "logs":
            log_attempts += 1
            stderr = (
                "database system is ready to accept connections"
                if log_attempts == 1
                else "PostgreSQL init process complete; ready for start up."
            )
            return subprocess.CompletedProcess(arguments, 0, stdout="", stderr=stderr)
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr(restore_drill, "docker_executable", lambda: "docker")
    monkeypatch.setattr(restore_drill.subprocess, "run", fake_run)
    monkeypatch.setattr(restore_drill.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(restore_drill.time, "sleep", lambda _seconds: None)

    restore_drill.wait_for_postgres("custombuild-restore-deadbeef-postgres")

    assert [arguments[1] for arguments in calls] == ["logs", "logs", "exec"]


def test_docker_timeout_is_bounded_actionable_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_timeout: object = None

    def hang(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal captured_timeout
        captured_timeout = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(
            arguments,
            timeout=float(captured_timeout),
            output="must-not-leak",
            stderr="must-not-leak",
        )

    monkeypatch.setattr(restore_drill, "docker_executable", lambda: "docker")
    monkeypatch.setattr(restore_drill.subprocess, "run", hang)

    with pytest.raises(BackupError, match="timed out after 17 seconds") as caught:
        restore_drill.docker("inspect", "--secret=must-not-leak", timeout_seconds=17)

    assert captured_timeout == 17
    assert "must-not-leak" not in str(caught.value)
    assert "retry" in str(caught.value)


def test_resource_inspection_timeout_is_bounded_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def hang(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["timeout"] == restore_drill.DOCKER_INSPECT_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(
            arguments,
            timeout=restore_drill.DOCKER_INSPECT_TIMEOUT_SECONDS,
            output=b"must-not-leak",
            stderr=b"must-not-leak",
        )

    monkeypatch.setattr(restore_drill, "docker_executable", lambda: "docker")
    monkeypatch.setattr(restore_drill.subprocess, "run", hang)

    with pytest.raises(BackupError, match="resource inspection timed out") as caught:
        restore_drill.docker_resource_exists(
            "container",
            "inspect",
            "custombuild-restore-deadbeef-postgres",
        )

    assert "must-not-leak" not in str(caught.value)


def test_postgres_log_hang_fails_within_the_probe_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_timeout = 0.0

    def hang(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal captured_timeout
        captured_timeout = float(kwargs["timeout"])
        raise subprocess.TimeoutExpired(arguments, timeout=captured_timeout)

    monkeypatch.setattr(restore_drill, "docker_executable", lambda: "docker")
    monkeypatch.setattr(restore_drill.subprocess, "run", hang)
    monkeypatch.setattr(restore_drill.time, "monotonic", lambda: 0.0)

    with pytest.raises(BackupError, match="log probe timed out") as caught:
        restore_drill.wait_for_postgres(
            "custombuild-restore-deadbeef-postgres",
            timeout_seconds=60,
        )

    assert 0 < captured_timeout <= restore_drill.DOCKER_PROBE_TIMEOUT_SECONDS
    assert "retry" in str(caught.value)


def test_cleanup_attempts_bounded_removal_after_each_inspection_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspections: list[tuple[tuple[str, ...], int]] = []
    removals: list[tuple[tuple[str, ...], int]] = []

    def hung_inspection(*arguments: str, timeout_seconds: int) -> bool:
        inspections.append((arguments, timeout_seconds))
        raise BackupError("injected inspection timeout")

    def hung_removal(
        *arguments: str,
        capture: bool = False,
        timeout_seconds: int,
    ) -> str:
        del capture
        removals.append((arguments, timeout_seconds))
        raise BackupError("injected removal timeout")

    monkeypatch.setattr(restore_drill, "docker_resource_exists", hung_inspection)
    monkeypatch.setattr(restore_drill, "docker", hung_removal)
    errors: list[str] = []
    resources = (
        ("container", "custombuild-restore-deadbeef-seaweed"),
        ("container", "custombuild-restore-deadbeef-postgres"),
        ("container", "custombuild-restore-deadbeef-extract"),
        ("volume", "custombuild-restore-deadbeef"),
    )

    for kind, name in resources:
        restore_drill._cleanup_resource(kind, name, errors)

    assert len(inspections) == len(resources)
    assert len(removals) == len(resources)
    assert all(
        timeout == restore_drill.DOCKER_INSPECT_TIMEOUT_SECONDS
        for _arguments, timeout in inspections
    )
    assert all(
        timeout == restore_drill.DOCKER_CLEANUP_TIMEOUT_SECONDS for _arguments, timeout in removals
    )
    assert len(errors) == len(resources) * 2
