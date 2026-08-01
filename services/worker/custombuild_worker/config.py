from __future__ import annotations

from functools import lru_cache
from typing import Literal

from app.config_guards import (
    validate_production_database_url,
    validate_production_s3_credentials,
)
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite+pysqlite:///./custombuild-worker.db"
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "custombuild"
    s3_secret_key: str = "development-only-object-secret"  # noqa: S105
    s3_bucket: str = Field(default="custombuild-artifacts", min_length=1)

    @model_validator(mode="after")
    def production_guards(self) -> WorkerSettings:
        if self.app_env == "production":
            validate_production_database_url(self.database_url)
            validate_production_s3_credentials(self.s3_access_key, self.s3_secret_key)
        return self


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
