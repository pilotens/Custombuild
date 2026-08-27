from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from app.config_guards import (
    BuildIdentityValues,
    validate_production_build_identity,
    validate_production_database_url,
    validate_production_redis_url,
    validate_production_s3_credentials,
)
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEPENDENCY_LOCK_PATH = Path(__file__).resolve().parents[3] / "uv.lock"


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
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "custombuild"
    s3_secret_key: str = "development-only-object-secret"  # noqa: S105
    s3_bucket: str = Field(default="custombuild-artifacts", min_length=1)

    @model_validator(mode="after")
    def production_guards(self) -> WorkerSettings:
        if self.app_env == "production":
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


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
