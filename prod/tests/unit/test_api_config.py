from __future__ import annotations

from pathlib import Path

import pytest
from app.config import Settings
from pydantic import ValidationError


def production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "auth_mode": "oidc",
        "database_url": (
            "postgresql+psycopg://custombuild_api:strong-db-password@db.internal/custombuild"
        ),
        "oidc_issuer": "https://identity.example.test/tenant",
        "artifact_signing_secret": "strong-artifact-signing-secret-0001",
        "s3_public_endpoint": "https://artifacts.example.test",
        "s3_access_key": "production-access-key",
        "s3_secret_key": "strong-object-storage-secret",
        "cors_origins": "https://custombuild.example.test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_complete_production_configuration_is_accepted() -> None:
    settings = production_settings()
    assert settings.auth_mode == "oidc"
    assert settings.allowed_origins == ["https://custombuild.example.test"]


def test_compose_forwards_every_production_guard_switch_to_the_api() -> None:
    compose = Path("compose.yml").read_text(encoding="utf-8")
    environment = Path(".env.example").read_text(encoding="utf-8")
    for name in (
        "APP_ENV",
        "AUTH_MODE",
        "DATABASE_URL",
        "OIDC_ISSUER",
        "OIDC_AUDIENCE",
        "CORS_ORIGINS",
        "ARTIFACT_SIGNING_SECRET",
        "ARTIFACT_URL_TTL_SECONDS",
        "S3_PUBLIC_ENDPOINT",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
    ):
        assert f"{name}: ${{{name}" in compose
        assert f"{name}=" in environment


def test_development_compose_only_publishes_services_on_loopback() -> None:
    compose = Path("compose.yml").read_text(encoding="utf-8")
    for mapping in ("9000:9000", "9001:9001", "8000:8000", "3000:3000"):
        assert f'"127.0.0.1:{mapping}"' in compose
        assert f'      - "{mapping}"' not in compose


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"auth_mode": "development"}, "AUTH_MODE=oidc"),
        ({"app_env": "development"}, "APP_ENV=production"),
        ({"oidc_issuer": "http://identity.example.test"}, "HTTPS issuer"),
        ({"oidc_issuer": "https://user@identity.example.test"}, "HTTPS issuer"),
        ({"database_url": "sqlite+pysqlite:///production.db"}, "PostgreSQL"),
        (
            {"database_url": "postgresql+psycopg://custombuild_api@db/custombuild"},
            "PostgreSQL",
        ),
        (
            {"database_url": "postgresql+psycopg://custombuild_api:change-me@db/custombuild"},
            "database password",
        ),
        ({"artifact_signing_secret": "development-signing-secret-000000"}, "signing"),
        ({"s3_access_key": "custombuild"}, "access key"),
        ({"s3_secret_key": "change-me-object-secret"}, "object-storage secret"),
        ({"s3_public_endpoint": "http://artifacts.example.test"}, "HTTPS public S3"),
        ({"s3_public_endpoint": "https://user@artifacts.example.test"}, "HTTPS public S3"),
        ({"s3_public_endpoint": "https:///missing-host"}, "HTTPS public S3"),
        ({"cors_origins": "http://custombuild.example.test"}, "HTTPS origins"),
        ({"cors_origins": "https://custombuild.example.test/path"}, "HTTPS origins"),
    ),
)
def test_insecure_production_configuration_is_rejected(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        production_settings(**overrides)
