from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from app.config import DEPENDENCY_LOCK_PATH, Settings
from pydantic import ValidationError


def production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "app_version": "1.4.0",
        "vcs_ref": "a" * 40,
        "build_date": "2026-08-11T12:00:00+02:00",
        "source_url": "https://github.com/pilotens/Custombuild",
        "source_manifest_sha256": "c" * 64,
        "dependency_lock_sha256": hashlib.sha256(DEPENDENCY_LOCK_PATH.read_bytes()).hexdigest(),
        "auth_mode": "oidc",
        "production_four_eyes_required": True,
        "database_url": (
            "postgresql+psycopg://custombuild_api:strong-api-database-password"
            "@db.internal/custombuild"
        ),
        "redis_url": "redis://:strong-api-redis-password-0001@redis.internal:6379/0",
        "oidc_issuer": "https://identity.example.test/tenant",
        "artifact_signing_secret": "strong-artifact-signing-secret-0001",
        "s3_access_key": "production-access-key",
        "s3_secret_key": "strong-object-storage-secret",
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
        "cors_origins": "https://custombuild.example.test",
        "trusted_proxy_cidrs": "172.20.0.0/24",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_complete_production_configuration_is_accepted() -> None:
    settings = production_settings()
    assert settings.auth_mode == "oidc"
    assert settings.production_four_eyes_required is True
    assert settings.allowed_origins == ["https://custombuild.example.test"]
    assert settings.build_identity == {
        "app_version": "1.4.0",
        "vcs_ref": "a" * 40,
        "build_date": "2026-08-11T12:00:00+02:00",
        "source_url": "https://github.com/pilotens/Custombuild",
        "source_manifest_sha256": "c" * 64,
        "dependency_lock_sha256": hashlib.sha256(DEPENDENCY_LOCK_PATH.read_bytes()).hexdigest(),
    }


def test_production_capacity_values_must_be_explicit_not_inherited_defaults() -> None:
    values = production_settings().model_dump()
    values.pop("storage_capacity_object_limit")

    with pytest.raises(ValidationError, match="storage_capacity_object_limit"):
        Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize("bucket", ("abc", "a.b-c", "a" * 63))
def test_production_accepts_canonical_s3_bucket_boundaries(bucket: str) -> None:
    assert production_settings(s3_bucket=bucket).s3_bucket == bucket


def test_production_lock_verification_is_independent_of_process_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert production_settings().dependency_lock_sha256


def test_compose_forwards_every_production_guard_switch_to_the_api() -> None:
    compose = Path("compose.yml").read_text(encoding="utf-8")
    environment = Path(".env.example").read_text(encoding="utf-8")
    for name in (
        "APP_ENV",
        "APP_VERSION",
        "VCS_REF",
        "BUILD_DATE",
        "SOURCE_URL",
        "SOURCE_MANIFEST_SHA256",
        "DEPENDENCY_LOCK_SHA256",
        "AUTH_MODE",
        "PRODUCTION_FOUR_EYES_REQUIRED",
        "DATABASE_URL",
        "DATABASE_STATEMENT_TIMEOUT_SECONDS",
        "DATABASE_LOCK_TIMEOUT_SECONDS",
        "READINESS_TIMEOUT_SECONDS",
        "RATE_LIMIT_REQUESTS",
        "RATE_LIMIT_WINDOW_SECONDS",
        "REQUEST_BODY_IDLE_TIMEOUT_SECONDS",
        "REQUEST_BODY_TOTAL_TIMEOUT_SECONDS",
        "TRUSTED_PROXY_CIDRS",
        "OIDC_ISSUER",
        "OIDC_AUDIENCE",
        "CORS_ORIGINS",
        "ARTIFACT_SIGNING_SECRET",
        "ARTIFACT_URL_TTL_SECONDS",
        "ARTIFACT_STREAM_TIMEOUT_SECONDS",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
    ):
        assert f"{name}: ${{{name}" in compose
        assert f"{name}=" in environment
    assert "REDIS_PASSWORD: ${REDIS_PASSWORD" in compose
    assert 'REDIS_URL: "redis://:${REDIS_PASSWORD' in compose
    assert "REDIS_PASSWORD=" in environment
    assert "REDIS_URL=" in environment
    assert "S3_BACKUP_ENDPOINT: ${S3_BACKUP_ENDPOINT" in compose
    assert "S3_BACKUP_ENDPOINT=" in environment


def test_development_compose_only_publishes_services_on_loopback() -> None:
    compose = Path("compose.yml").read_text(encoding="utf-8")
    for mapping in (
        "${S3_BIND_PORT:-9000}:8333",
        "${API_BIND_PORT:-8000}:8000",
        "${WEB_BIND_PORT:-3000}:3000",
    ):
        assert f'"127.0.0.1:{mapping}"' in compose
        assert f'      - "{mapping}"' not in compose


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"app_version": "0.1.0-local"}, "APP_VERSION"),
        ({"app_version": "1.0.0+dirty"}, "APP_VERSION"),
        ({"vcs_ref": "uncommitted"}, "VCS_REF"),
        ({"vcs_ref": "a" * 39}, "VCS_REF"),
        ({"build_date": "2026-08-11T12:00:00"}, "BUILD_DATE"),
        ({"build_date": "unknown"}, "BUILD_DATE"),
        ({"source_url": "http://github.com/pilotens/Custombuild"}, "SOURCE_URL"),
        ({"source_url": "https://user@github.com/pilotens/Custombuild"}, "SOURCE_URL"),
        ({"source_manifest_sha256": "unknown"}, "SOURCE_MANIFEST_SHA256"),
        ({"source_manifest_sha256": "c" * 63}, "SOURCE_MANIFEST_SHA256"),
        ({"dependency_lock_sha256": "unknown"}, "DEPENDENCY_LOCK_SHA256"),
        ({"dependency_lock_sha256": "b" * 63}, "DEPENDENCY_LOCK_SHA256"),
        ({"dependency_lock_sha256": "b" * 64}, "does not match uv.lock"),
        ({"auth_mode": "development"}, "AUTH_MODE=oidc"),
        (
            {"production_four_eyes_required": False},
            "PRODUCTION_FOUR_EYES_REQUIRED=true",
        ),
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
        (
            {
                "database_url": (
                    "postgresql+psycopg://custombuild_worker:strong-api-database-password"
                    "@db/custombuild"
                )
            },
            "exact database role custombuild_api",
        ),
        (
            {"database_url": "postgresql+psycopg://custombuild_api:short@db/custombuild"},
            "at least 24 characters",
        ),
        (
            {
                "database_url": (
                    "postgresql+psycopg://custombuild_api:%20strong-api-database-password%20"
                    "@db/custombuild"
                )
            },
            "surrounding whitespace",
        ),
        ({"redis_url": "redis://redis.internal:6379/0"}, "Redis password"),
        (
            {"redis_url": "redis://:change-me-redis@redis.internal:6379/0"},
            "Redis password",
        ),
        ({"redis_url": "redis://:short@redis.internal:6379/0"}, "at least 24 characters"),
        (
            {"redis_url": ("redis://:%20strong-api-redis-password-0001%20@redis.internal:6379/0")},
            "surrounding whitespace",
        ),
        ({"artifact_signing_secret": "development-signing-secret-000000"}, "signing"),
        ({"s3_access_key": "custombuild"}, "access key"),
        ({"s3_access_key": " production-access-key"}, "surrounding whitespace"),
        ({"s3_access_key": "production\naccess-key"}, "control characters"),
        ({"s3_access_key": "production\x7faccess-key"}, "control characters"),
        ({"s3_access_key": "production:access-key"}, "access key"),
        ({"s3_access_key": "production/access-key"}, "access key"),
        ({"s3_secret_key": "change-me-object-secret"}, "object-storage secret"),
        ({"s3_secret_key": "short"}, "at least 24 characters"),
        (
            {"s3_secret_key": " strong-object-storage-secret "},
            "surrounding whitespace",
        ),
        (
            {"s3_secret_key": "strong-object-storage-secret\nvalue"},
            "control characters",
        ),
        (
            {"s3_secret_key": "strong-object-storage-secret\x7fvalue"},
            "control characters",
        ),
        ({"s3_bucket": " production-artifacts"}, "surrounding whitespace"),
        ({"s3_bucket": "production\nartifacts"}, "control characters"),
        ({"s3_bucket": "production\x7fartifacts"}, "control characters"),
        ({"s3_bucket": "arn:aws:s3:::production-artifacts"}, "canonical S3 DNS"),
        ({"s3_bucket": "production/artifacts"}, "canonical S3 DNS"),
        ({"s3_bucket": "Production-Artifacts"}, "canonical S3 DNS"),
        ({"s3_bucket": "production_artifacts"}, "canonical S3 DNS"),
        ({"s3_bucket": "."}, "canonical S3 DNS"),
        ({"s3_bucket": ".."}, "canonical S3 DNS"),
        ({"s3_bucket": "a..b"}, "canonical S3 DNS"),
        ({"s3_bucket": "-production"}, "canonical S3 DNS"),
        ({"s3_bucket": "production-"}, "canonical S3 DNS"),
        ({"s3_bucket": "192.0.2.1"}, "canonical S3 DNS"),
        ({"s3_bucket": "ab"}, "canonical S3 DNS"),
        ({"s3_bucket": "a" * 64}, "canonical S3 DNS"),
        ({"s3_bucket": ""}, "canonical S3 DNS"),
        (
            {"storage_capacity_operator_config_sha256": "unverified"},
            "STORAGE_CAPACITY_OPERATOR_CONFIG_SHA256",
        ),
        (
            {"storage_capacity_volume_identity": "invalid/volume"},
            "STORAGE_CAPACITY_VOLUME_IDENTITY",
        ),
        (
            {"storage_capacity_headroom_bytes": 201},
            "HEADROOM_BYTES",
        ),
        (
            {"storage_capacity_provisioned_bytes": 200},
            "PROVISIONED_BYTES",
        ),
        (
            {"storage_capacity_byte_limit": 801},
            "BYTE_LIMIT",
        ),
        (
            {"storage_capacity_deploy_descriptor_sha256": "unverified"},
            "STORAGE_CAPACITY_DEPLOY_DESCRIPTOR_SHA256",
        ),
        (
            {"storage_capacity_max_age_seconds": 599},
            "MAX_AGE_SECONDS",
        ),
        ({"cors_origins": "http://custombuild.example.test"}, "HTTPS origins"),
        ({"cors_origins": "https://custombuild.example.test/path"}, "HTTPS origins"),
        ({"trusted_proxy_cidrs": ""}, "private IP networks"),
        ({"trusted_proxy_cidrs": "0.0.0.0/0"}, "private IP networks"),
        ({"trusted_proxy_cidrs": "203.0.113.0/24"}, "private IP networks"),
        ({"trusted_proxy_cidrs": "not-a-cidr"}, "valid IP networks"),
    ),
)
def test_insecure_production_configuration_is_rejected(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        production_settings(**overrides)


def test_four_eyes_setting_defaults_off_outside_production() -> None:
    assert Settings(_env_file=None).production_four_eyes_required is False
    assert Settings(_env_file=None, app_env="test").production_four_eyes_required is False
