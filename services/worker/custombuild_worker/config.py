from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from app.config_guards import (
    BuildIdentityValues,
    read_production_cam_profile_source,
    validate_production_build_identity,
    validate_production_database_url,
    validate_production_redis_url,
    validate_production_s3_bucket,
    validate_production_s3_credentials,
)
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEPENDENCY_LOCK_PATH = Path(__file__).resolve().parents[3] / "uv.lock"
MAX_DATABASE_INTEGER = 2**63 - 1
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_VOLUME_IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,254}\Z")
PRODUCTION_CAPACITY_FIELDS = frozenset(
    {
        "s3_bucket",
        "storage_capacity_operator_config_sha256",
        "storage_capacity_volume_identity",
        "storage_capacity_provisioned_bytes",
        "storage_capacity_metadata_overhead_bytes",
        "storage_capacity_emergency_reserve_bytes",
        "storage_capacity_headroom_bytes",
        "storage_capacity_byte_limit",
        "storage_capacity_object_limit",
        "storage_capacity_deploy_descriptor_sha256",
        "storage_capacity_max_age_seconds",
    }
)


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    app_version: str = "0.1.0-local"
    vcs_ref: str = "uncommitted"
    build_date: str = "unknown"
    source_url: str = "unknown"
    source_manifest_sha256: str = "unknown"
    dependency_lock_sha256: str = "unknown"
    database_url: str = "sqlite+pysqlite:///./custombuild-worker.db"
    database_statement_timeout_seconds: int = Field(default=60, ge=1, le=120)
    database_lock_timeout_seconds: int = Field(default=10, ge=1, le=30)
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "custombuild"
    s3_secret_key: str = "development-only-object-secret"  # noqa: S105
    s3_bucket: str = Field(default="custombuild-artifacts", min_length=1)
    joint_retention_trust_registry_json: str = Field(default="", max_length=262_144)
    production_cam_profile_path: str = Field(default="", max_length=4096)
    production_cam_profile_json: str = Field(default="", max_length=1_048_576)
    production_cam_profile_sha256: str = Field(default="", max_length=64)
    storage_capacity_operator_config_sha256: str = "unverified"
    storage_capacity_volume_identity: str = "development-local-volume"
    storage_capacity_provisioned_bytes: int = Field(
        default=256 * 1024**3, ge=1, le=MAX_DATABASE_INTEGER
    )
    storage_capacity_metadata_overhead_bytes: int = Field(
        default=1024**3, ge=1, le=MAX_DATABASE_INTEGER
    )
    storage_capacity_emergency_reserve_bytes: int = Field(
        default=4 * 1024**3, ge=1, le=MAX_DATABASE_INTEGER
    )
    storage_capacity_headroom_bytes: int = Field(default=5 * 1024**3, ge=1, le=MAX_DATABASE_INTEGER)
    storage_capacity_byte_limit: int = Field(default=251 * 1024**3, ge=1, le=MAX_DATABASE_INTEGER)
    storage_capacity_object_limit: int = Field(default=1_000_000, ge=1, le=MAX_DATABASE_INTEGER)
    storage_capacity_deploy_descriptor_sha256: str = "unverified"
    storage_capacity_max_age_seconds: int = Field(default=600, ge=60, le=600)

    @model_validator(mode="after")
    def production_guards(self) -> WorkerSettings:
        if (
            self.production_cam_profile_path
            or self.production_cam_profile_json
            or self.production_cam_profile_sha256
        ):
            read_production_cam_profile_source(
                profile_path=self.production_cam_profile_path,
                profile_json=self.production_cam_profile_json,
                profile_sha256=self.production_cam_profile_sha256,
                production=self.app_env == "production",
            )
        if self.database_lock_timeout_seconds >= self.database_statement_timeout_seconds:
            raise ValueError(
                "DATABASE_LOCK_TIMEOUT_SECONDS must be shorter than the statement timeout"
            )
        if (
            self.storage_capacity_headroom_bytes
            != self.storage_capacity_metadata_overhead_bytes
            + self.storage_capacity_emergency_reserve_bytes
        ):
            raise ValueError(
                "STORAGE_CAPACITY_HEADROOM_BYTES must equal metadata overhead plus "
                "emergency reserve"
            )
        if self.storage_capacity_headroom_bytes >= self.storage_capacity_provisioned_bytes:
            raise ValueError("STORAGE_CAPACITY_PROVISIONED_BYTES must exceed capacity headroom")
        if self.storage_capacity_byte_limit > (
            self.storage_capacity_provisioned_bytes - self.storage_capacity_headroom_bytes
        ):
            raise ValueError("STORAGE_CAPACITY_BYTE_LIMIT exceeds attested usable storage")
        if self.app_env == "production":
            missing_capacity_fields = sorted(PRODUCTION_CAPACITY_FIELDS - self.model_fields_set)
            if missing_capacity_fields:
                raise ValueError(
                    "production worker requires explicit storage-capacity settings: "
                    + ", ".join(missing_capacity_fields)
                )
            validate_production_build_identity(
                app_version=self.app_version,
                vcs_ref=self.vcs_ref,
                build_date=self.build_date,
                source_url=self.source_url,
                source_manifest_sha256=self.source_manifest_sha256,
                dependency_lock_sha256=self.dependency_lock_sha256,
                dependency_lock_path=DEPENDENCY_LOCK_PATH,
            )
            validate_production_database_url(
                self.database_url,
                expected_username="custombuild_worker",
            )
            validate_production_redis_url(self.redis_url)
            validate_production_s3_credentials(self.s3_access_key, self.s3_secret_key)
            validate_production_s3_bucket(self.s3_bucket)
            if _SHA256_PATTERN.fullmatch(self.storage_capacity_operator_config_sha256) is None:
                raise ValueError(
                    "production worker requires STORAGE_CAPACITY_OPERATOR_CONFIG_SHA256"
                )
            if _VOLUME_IDENTITY_PATTERN.fullmatch(self.storage_capacity_volume_identity) is None:
                raise ValueError("production worker requires a canonical storage volume identity")
            if _SHA256_PATTERN.fullmatch(self.storage_capacity_deploy_descriptor_sha256) is None:
                raise ValueError(
                    "production worker requires STORAGE_CAPACITY_DEPLOY_DESCRIPTOR_SHA256"
                )
            if self.storage_capacity_max_age_seconds != 600:
                raise ValueError("production worker STORAGE_CAPACITY_MAX_AGE_SECONDS must be 600")
        return self

    @property
    def build_identity(self) -> BuildIdentityValues:
        return {
            "app_version": self.app_version,
            "vcs_ref": self.vcs_ref,
            "build_date": self.build_date,
            "source_url": self.source_url,
            "source_manifest_sha256": self.source_manifest_sha256,
            "dependency_lock_sha256": self.dependency_lock_sha256,
        }

    @property
    def production_cam_profile_source(self) -> bytes | str:
        return read_production_cam_profile_source(
            profile_path=self.production_cam_profile_path,
            profile_json=self.production_cam_profile_json,
            profile_sha256=self.production_cam_profile_sha256,
            production=self.app_env == "production",
        )


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
