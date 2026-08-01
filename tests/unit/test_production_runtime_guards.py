from __future__ import annotations

import os
import subprocess  # noqa: S404
from pathlib import Path

import pytest
from app.config_guards import (
    validate_production_database_url,
    validate_production_s3_credentials,
)
from custombuild_worker.config import WorkerSettings
from pydantic import ValidationError

from scripts.run_migrations import validate_migration_environment


def production_worker_settings(**overrides: object) -> WorkerSettings:
    values: dict[str, object] = {
        "app_env": "production",
        "database_url": (
            "postgresql+psycopg://custombuild_worker:strong-worker-db-password"
            "@postgres:5432/custombuild"
        ),
        "s3_access_key": "production-worker-access",
        "s3_secret_key": "strong-production-object-secret",
    }
    values.update(overrides)
    return WorkerSettings(_env_file=None, **values)  # type: ignore[arg-type]


def test_worker_accepts_secure_production_and_development_defaults() -> None:
    production = production_worker_settings()
    development = WorkerSettings(_env_file=None)

    assert production.app_env == "production"
    assert development.database_url.startswith("sqlite")
    assert development.s3_access_key == "custombuild"


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
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
        ({"s3_access_key": "minioadmin"}, "access key"),
        ({"s3_secret_key": "development-only-object-secret"}, "object-storage secret"),
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
            setting_name="MIGRATION_DATABASE_URL",
        )
    with pytest.raises(ValueError, match="access key"):
        validate_production_s3_credentials("  ", "strong-production-object-secret")
    with pytest.raises(ValueError, match="object-storage secret"):
        validate_production_s3_credentials("production-access", "  ")


@pytest.mark.parametrize(
    "database_url",
    (
        "sqlite+pysqlite:///migrations.db",
        "postgresql+psycopg://custombuild_migrator@postgres/custombuild",
        ("postgresql+psycopg://custombuild_migrator:change-me-migrator@postgres/custombuild"),
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
        "POSTGRES_USER": "custombuild_migrator",
        "POSTGRES_PASSWORD": "change-me-migrator",
        "API_DATABASE_PASSWORD": "change-me-api",
        "WORKER_DATABASE_PASSWORD": "change-me-worker",
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


@pytest.mark.parametrize(
    ("variable", "value"),
    (
        ("POSTGRES_PASSWORD", "change-me-migrator"),
        ("API_DATABASE_PASSWORD", "development-api-password"),
        ("WORKER_DATABASE_PASSWORD", "password"),
        ("WORKER_DATABASE_PASSWORD", "   "),
    ),
)
def test_postgres_init_rejects_insecure_production_passwords(
    tmp_path: Path, variable: str, value: str
) -> None:
    completed = _run_postgres_init(
        tmp_path,
        app_env="production",
        overrides={
            "POSTGRES_PASSWORD": "strong-migrator-db-password",
            "API_DATABASE_PASSWORD": "strong-api-db-password",
            "WORKER_DATABASE_PASSWORD": "strong-worker-db-password",
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
            "POSTGRES_PASSWORD": "strong-migrator-db-password",
            "API_DATABASE_PASSWORD": "strong-api-db-password",
            "WORKER_DATABASE_PASSWORD": "strong-worker-db-password",
        },
    )
    assert completed.returncode == 0, completed.stderr


def test_compose_routes_production_mode_through_every_startup_guard() -> None:
    compose = Path("compose.yml").read_text(encoding="utf-8")

    assert compose.count("APP_ENV: ${APP_ENV:-development}") == 4
    assert 'command: ["python", "-m", "scripts.run_migrations"]' in compose
