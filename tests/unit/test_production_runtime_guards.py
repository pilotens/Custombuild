from __future__ import annotations

import hashlib
import os
import subprocess  # noqa: S404
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.config_guards import (
    validate_production_database_url,
    validate_production_redis_url,
    validate_production_s3_credentials,
)
from custombuild_worker.config import DEPENDENCY_LOCK_PATH, WorkerSettings
from pydantic import ValidationError

import scripts.run_migrations as migration_runner
from scripts.run_migrations import validate_migration_environment


def production_worker_settings(**overrides: object) -> WorkerSettings:
    values: dict[str, object] = {
        "app_env": "production",
        "app_version": "1.4.0",
        "vcs_ref": "a" * 40,
        "build_date": "2026-08-11T12:00:00+02:00",
        "source_url": "https://github.com/pilotens/Custombuild",
        "source_manifest_sha256": "c" * 64,
        "dependency_lock_sha256": hashlib.sha256(DEPENDENCY_LOCK_PATH.read_bytes()).hexdigest(),
        "database_url": (
            "postgresql+psycopg://custombuild_worker:strong-worker-db-password"
            "@postgres:5432/custombuild"
        ),
        "redis_url": "redis://:strong-worker-redis-password@redis:6379/0",
        "s3_access_key": "production-worker-access",
        "s3_secret_key": "strong-production-object-secret",
        "s3_bucket": "production-artifacts",
        "storage_capacity_operator_config_sha256": "d" * 64,
        "storage_capacity_volume_identity": "provider-volume-0001",
        "storage_capacity_provisioned_bytes": 1_000,
        "storage_capacity_metadata_overhead_bytes": 100,
        "storage_capacity_emergency_reserve_bytes": 100,
        "storage_capacity_headroom_bytes": 200,
        "storage_capacity_byte_limit": 800,
        "storage_capacity_object_limit": 100,
        "storage_capacity_deploy_descriptor_sha256": "e" * 64,
        "storage_capacity_max_age_seconds": 600,
    }
    values.update(overrides)
    return WorkerSettings(_env_file=None, **values)  # type: ignore[arg-type]


def test_worker_accepts_secure_production_and_development_defaults() -> None:
    production = production_worker_settings()
    development = WorkerSettings(_env_file=None)

    assert production.app_env == "production"
    assert production.build_identity["vcs_ref"] == "a" * 40
    assert production.build_identity["source_manifest_sha256"] == "c" * 64
    assert (
        production.build_identity["dependency_lock_sha256"]
        == hashlib.sha256(DEPENDENCY_LOCK_PATH.read_bytes()).hexdigest()
    )
    assert development.database_url.startswith("sqlite")
    assert development.s3_access_key == "custombuild"


def test_worker_lock_verification_is_independent_of_celery_workdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert production_worker_settings().dependency_lock_sha256


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"app_version": "0.1.0-local"}, "APP_VERSION"),
        ({"vcs_ref": "uncommitted"}, "VCS_REF"),
        ({"build_date": "unknown"}, "BUILD_DATE"),
        ({"source_url": "http://github.com/pilotens/Custombuild"}, "SOURCE_URL"),
        ({"source_manifest_sha256": "unknown"}, "SOURCE_MANIFEST_SHA256"),
        ({"dependency_lock_sha256": "unknown"}, "DEPENDENCY_LOCK_SHA256"),
        ({"dependency_lock_sha256": "b" * 64}, "does not match uv.lock"),
        ({"database_url": "sqlite+pysqlite:///worker.db"}, "PostgreSQL"),
        (
            {"database_url": "postgresql+psycopg://custombuild_worker@postgres/custombuild"},
            "PostgreSQL",
        ),
        (
            {
                "database_url": (
                    "postgresql+psycopg://custombuild_worker:change-me-worker@postgres/custombuild"
                )
            },
            "database password",
        ),
        (
            {
                "database_url": (
                    "postgresql+psycopg://custombuild_api:strong-worker-db-password"
                    "@postgres/custombuild"
                )
            },
            "exact database role custombuild_worker",
        ),
        (
            {
                "database_url": (
                    "postgresql+psycopg://custombuild_worker:short@postgres/custombuild"
                )
            },
            "at least 24 characters",
        ),
        ({"redis_url": "redis://redis:6379/0"}, "Redis password"),
        ({"redis_url": "redis://:change-me-redis@redis:6379/0"}, "Redis password"),
        ({"redis_url": "redis://:short@redis:6379/0"}, "at least 24 characters"),
        ({"s3_access_key": "minioadmin"}, "access key"),
        ({"s3_access_key": "production\naccess"}, "control characters"),
        ({"s3_secret_key": "development-only-object-secret"}, "object-storage secret"),
        ({"s3_secret_key": "short"}, "at least 24 characters"),
        ({"s3_secret_key": "strong-production\x7fobject-secret"}, "control characters"),
        ({"s3_bucket": "arn:aws:s3:::production-artifacts"}, "canonical S3 DNS"),
        ({"s3_bucket": "production\nartifacts"}, "control characters"),
        ({"s3_bucket": "Production-Artifacts"}, "canonical S3 DNS"),
        ({"s3_bucket": "production_artifacts"}, "canonical S3 DNS"),
        ({"s3_bucket": ".."}, "canonical S3 DNS"),
        ({"s3_bucket": "192.0.2.1"}, "canonical S3 DNS"),
    ),
)
def test_worker_rejects_insecure_production_configuration(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        production_worker_settings(**overrides)


def test_shared_guards_reject_malformed_urls_and_blank_credentials() -> None:
    with pytest.raises(ValueError, match="MIGRATION_DATABASE_URL is invalid"):
        validate_production_database_url(
            "not a database URL",
            expected_username="custombuild_migrator",
            setting_name="MIGRATION_DATABASE_URL",
        )
    with pytest.raises(ValueError, match="access key"):
        validate_production_s3_credentials("  ", "strong-production-object-secret")
    with pytest.raises(ValueError, match="object-storage secret"):
        validate_production_s3_credentials("production-access", "  ")
    with pytest.raises(ValueError, match="REDIS_URL is invalid"):
        validate_production_redis_url("redis://:secret@redis:invalid/0")


@pytest.mark.parametrize(
    "database_url",
    (
        "sqlite+pysqlite:///migrations.db",
        "postgresql+psycopg://custombuild_migrator@postgres/custombuild",
        ("postgresql+psycopg://custombuild_migrator:change-me-migrator@postgres/custombuild"),
        ("postgresql+psycopg://custombuild_api:strong-migrator-db-password@postgres/custombuild"),
        "postgresql+psycopg://custombuild_migrator:short@postgres/custombuild",
        (
            "postgresql+psycopg://custombuild_migrator:%20strong-migrator-db-password%20"
            "@postgres/custombuild"
        ),
    ),
)
def test_migrator_rejects_insecure_production_database(database_url: str) -> None:
    with pytest.raises(ValueError):
        validate_migration_environment({"APP_ENV": "production", "DATABASE_URL": database_url})


def test_migrator_accepts_secure_production_and_development_fallback() -> None:
    validate_migration_environment(
        {
            "APP_ENV": "production",
            "DATABASE_URL": (
                "postgresql+psycopg://custombuild_migrator:strong-migrator-db-password"
                "@postgres/custombuild"
            ),
        }
    )
    validate_migration_environment({"APP_ENV": "development"})


@pytest.mark.parametrize("app_env", ("development", "test"))
def test_migrator_upgrades_before_seeding_nonproduction_database(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
) -> None:
    session = object()
    calls: list[tuple[str, object]] = []

    def fake_alembic_main(*, argv: list[str]) -> None:
        calls.append(("migration", argv))

    def fake_session_scope() -> Iterator[object]:
        calls.append(("session", session))
        yield session

    def fake_seed(received_session: object) -> None:
        calls.append(("seed", received_session))

    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("SEED_DEVELOPMENT_DATA", "true")
    monkeypatch.setattr(migration_runner, "alembic_main", fake_alembic_main)
    monkeypatch.setattr(migration_runner, "session_scope", fake_session_scope)
    monkeypatch.setattr(migration_runner, "seed_development", fake_seed)

    migration_runner.main()

    assert calls == [
        ("migration", ["-c", "services/api/alembic.ini", "upgrade", "head"]),
        ("session", session),
        ("seed", session),
    ]


def test_migrator_never_seeds_production_database(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fail_if_called() -> object:
        raise AssertionError("production migrations must not seed development data")

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SEED_DEVELOPMENT_DATA", "true")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://custombuild_migrator:strong-migrator-db-password"
        "@postgres/custombuild",
    )
    monkeypatch.setattr(
        migration_runner,
        "alembic_main",
        lambda *, argv: calls.append("migration"),
    )
    monkeypatch.setattr(migration_runner, "session_scope", fail_if_called)
    monkeypatch.setattr(migration_runner, "seed_development", fail_if_called)

    migration_runner.main()

    assert calls == ["migration"]


@pytest.mark.parametrize(
    "seed_flag",
    (None, "", "false", "1", "TRUE", " true"),
)
def test_migrator_requires_explicit_seed_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    seed_flag: str | None,
) -> None:
    calls: list[str] = []

    def fail_if_called() -> object:
        raise AssertionError("development seed requires an exact explicit opt-in")

    monkeypatch.setenv("APP_ENV", "development")
    if seed_flag is None:
        monkeypatch.delenv("SEED_DEVELOPMENT_DATA", raising=False)
    else:
        monkeypatch.setenv("SEED_DEVELOPMENT_DATA", seed_flag)
    monkeypatch.setattr(
        migration_runner,
        "alembic_main",
        lambda *, argv: calls.append("migration"),
    )
    monkeypatch.setattr(migration_runner, "session_scope", fail_if_called)
    monkeypatch.setattr(migration_runner, "seed_development", fail_if_called)

    migration_runner.main()

    assert calls == ["migration"]


@pytest.mark.parametrize("app_env", ("staging", "prod", "", "DEVELOPMENT"))
def test_migrator_does_not_seed_unknown_environments(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
) -> None:
    calls: list[str] = []

    def fail_if_called() -> object:
        raise AssertionError("development seed is restricted to known local environments")

    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("SEED_DEVELOPMENT_DATA", "true")
    monkeypatch.setattr(
        migration_runner,
        "alembic_main",
        lambda *, argv: calls.append("migration"),
    )
    monkeypatch.setattr(migration_runner, "session_scope", fail_if_called)
    monkeypatch.setattr(migration_runner, "seed_development", fail_if_called)

    migration_runner.main()

    assert calls == ["migration"]


def _run_postgres_init(
    tmp_path: Path,
    *,
    app_env: str,
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_psql = tmp_path / "psql"
    fake_psql.write_text("#!/bin/sh\ncat >/dev/null\n", encoding="utf-8")
    fake_psql.chmod(0o700)
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "APP_ENV": app_env,
        "POSTGRES_DB": "custombuild",
        "POSTGRES_USER": "custombuild_bootstrap",
        "POSTGRES_PASSWORD": "change-me-bootstrap",
        "MIGRATOR_DATABASE_USER": "custombuild_migrator",
        "MIGRATOR_DATABASE_PASSWORD": "change-me-migrator",
        "API_DATABASE_PASSWORD": "change-me-api",
        "WORKER_DATABASE_PASSWORD": "change-me-worker",
        "CAPACITY_ATTESTOR_DATABASE_USER": "custombuild_storage_attestor",
        "CAPACITY_ATTESTOR_DATABASE_PASSWORD": "change-me-capacity-attestor",
    }
    environment.update(overrides or {})
    return subprocess.run(  # noqa: S603
        ["/bin/sh", "infra/postgres/init-roles.sh"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_postgres_init_keeps_development_defaults_working(tmp_path: Path) -> None:
    completed = _run_postgres_init(tmp_path, app_env="development")
    assert completed.returncode == 0, completed.stderr


def test_postgres_init_provisions_and_isolates_the_capacity_attestor() -> None:
    source = Path("infra/postgres/init-roles.sh").read_text(encoding="utf-8")

    assert "CREATE ROLE custombuild_storage_attestor LOGIN PASSWORD %L" in source
    assert "ALTER ROLE custombuild_storage_attestor WITH LOGIN PASSWORD %L" in source
    assert "NOINHERIT NOREPLICATION NOBYPASSRLS" in source
    assert "member.rolname = 'custombuild_storage_attestor'" in source
    assert "granted.rolname = 'custombuild_storage_attestor'" in source
    assert "FROM custombuild_api, custombuild_worker, custombuild_storage_attestor" in source


@pytest.mark.parametrize(
    ("variable", "value"),
    (
        ("POSTGRES_PASSWORD", "change-me-migrator"),
        ("MIGRATOR_DATABASE_PASSWORD", "change-me-migrator"),
        ("API_DATABASE_PASSWORD", "development-api-password"),
        ("API_DATABASE_PASSWORD", "too-short"),
        ("WORKER_DATABASE_PASSWORD", "password"),
        ("WORKER_DATABASE_PASSWORD", "   "),
        ("CAPACITY_ATTESTOR_DATABASE_PASSWORD", "change-me-capacity-attestor"),
    ),
)
def test_postgres_init_rejects_insecure_production_passwords(
    tmp_path: Path, variable: str, value: str
) -> None:
    completed = _run_postgres_init(
        tmp_path,
        app_env="production",
        overrides={
            "POSTGRES_PASSWORD": "strong-bootstrap-db-password",
            "MIGRATOR_DATABASE_PASSWORD": "strong-migrator-db-password",
            "API_DATABASE_PASSWORD": "strong-api-database-password",
            "WORKER_DATABASE_PASSWORD": "strong-worker-db-password",
            "CAPACITY_ATTESTOR_DATABASE_PASSWORD": ("strong-capacity-attestor-db-password"),
            variable: value,
        },
    )

    assert completed.returncode == 64
    assert variable in completed.stderr


def test_postgres_init_accepts_replaced_production_passwords(tmp_path: Path) -> None:
    completed = _run_postgres_init(
        tmp_path,
        app_env="production",
        overrides={
            "POSTGRES_PASSWORD": "strong-bootstrap-db-password",
            "MIGRATOR_DATABASE_PASSWORD": "strong-migrator-db-password",
            "API_DATABASE_PASSWORD": "strong-api-database-password",
            "WORKER_DATABASE_PASSWORD": "strong-worker-db-password",
            "CAPACITY_ATTESTOR_DATABASE_PASSWORD": ("strong-capacity-attestor-db-password"),
        },
    )
    assert completed.returncode == 0, completed.stderr


def test_postgres_init_rejects_reused_capacity_attestor_password(tmp_path: Path) -> None:
    shared = "strong-but-shared-database-password"
    completed = _run_postgres_init(
        tmp_path,
        app_env="production",
        overrides={
            "POSTGRES_PASSWORD": "strong-bootstrap-db-password",
            "MIGRATOR_DATABASE_PASSWORD": shared,
            "API_DATABASE_PASSWORD": "strong-api-database-password",
            "WORKER_DATABASE_PASSWORD": "strong-worker-db-password",
            "CAPACITY_ATTESTOR_DATABASE_PASSWORD": shared,
        },
    )

    assert completed.returncode == 64
    assert "must be unique" in completed.stderr


@pytest.mark.parametrize(
    ("variable", "value"),
    (
        ("POSTGRES_USER", "custombuild_migrator"),
        ("MIGRATOR_DATABASE_USER", "custombuild_bootstrap"),
        ("CAPACITY_ATTESTOR_DATABASE_USER", "custombuild_worker"),
    ),
)
def test_postgres_init_rejects_role_name_substitution(
    tmp_path: Path, variable: str, value: str
) -> None:
    completed = _run_postgres_init(
        tmp_path,
        app_env="production",
        overrides={
            "POSTGRES_PASSWORD": "strong-bootstrap-db-password",
            "MIGRATOR_DATABASE_PASSWORD": "strong-migrator-db-password",
            "API_DATABASE_PASSWORD": "strong-api-database-password",
            "WORKER_DATABASE_PASSWORD": "strong-worker-database-password",
            "CAPACITY_ATTESTOR_DATABASE_PASSWORD": ("strong-capacity-attestor-database-password"),
            variable: value,
        },
    )

    assert completed.returncode == 64
    assert variable in completed.stderr


def test_compose_routes_production_mode_through_every_startup_guard() -> None:
    compose = Path("compose.yml").read_text(encoding="utf-8")

    # PostgreSQL, migration, one-shot storage recovery, the local-only capacity
    # attestor, API, all three workers and scheduler retain their local default.
    # Web distinguishes unset from explicitly blank so blank cannot silently
    # weaken a production server.
    assert compose.count("APP_ENV: ${APP_ENV:-development}") == 9
    assert compose.count("APP_ENV: ${APP_ENV-development}") == 1
    assert compose.count('SEED_DEVELOPMENT_DATA: "true"') == 1
    assert 'command: ["python", "-m", "scripts.run_migrations"]' in compose
    assert 'command: ["python", "-m", "scripts.storage_recovery"]' in compose
