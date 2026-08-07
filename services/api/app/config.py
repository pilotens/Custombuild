from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .config_guards import (
    is_insecure_secret,
    validate_production_database_url,
    validate_production_s3_credentials,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    auth_mode: Literal["development", "oidc"] = "development"
    database_url: str = "sqlite+pysqlite:///./custombuild.db"
    redis_url: str = "redis://localhost:6379/0"
    oidc_issuer: str = ""
    oidc_audience: str = "custombuild-api"
    artifact_signing_secret: str = Field(
        default="development-only-signing-secret-32-bytes",
        min_length=32,
    )
    artifact_url_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    s3_endpoint: str = "http://localhost:9000"
    s3_public_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "custombuild"
    s3_secret_key: str = "development-only-object-secret"  # noqa: S105
    s3_bucket: str = "custombuild-artifacts"
    cors_origins: str = "http://localhost:3000"

    @model_validator(mode="after")
    def production_guards(self) -> Settings:
        if self.app_env == "production" and self.auth_mode != "oidc":
            raise ValueError("production requires AUTH_MODE=oidc")
        if self.auth_mode == "oidc" and self.app_env != "production":
            raise ValueError("OIDC mode requires APP_ENV=production")
        if self.auth_mode == "oidc" and not self.oidc_issuer:
            raise ValueError("OIDC_ISSUER is required in OIDC mode")
        if self.app_env == "production":
            issuer = urlparse(self.oidc_issuer)
            if (
                issuer.scheme != "https"
                or not issuer.hostname
                or issuer.username is not None
                or issuer.password is not None
                or issuer.query
                or issuer.fragment
            ):
                raise ValueError("production OIDC_ISSUER must be an HTTPS issuer URL")
            validate_production_database_url(self.database_url)
            for origin in self.allowed_origins:
                parsed_origin = urlparse(origin)
                if (
                    parsed_origin.scheme != "https"
                    or not parsed_origin.hostname
                    or parsed_origin.username is not None
                    or parsed_origin.password is not None
                    or parsed_origin.path not in {"", "/"}
                    or parsed_origin.params
                    or parsed_origin.query
                    or parsed_origin.fragment
                ):
                    raise ValueError("production CORS origins must be HTTPS origins")
        if self.app_env == "production" and is_insecure_secret(self.artifact_signing_secret):
            raise ValueError("production artifact signing secret must be replaced")
        if self.app_env == "production":
            validate_production_s3_credentials(self.s3_access_key, self.s3_secret_key)
            public_s3 = urlparse(self.s3_public_endpoint)
            if (
                public_s3.scheme != "https"
                or not public_s3.hostname
                or public_s3.username is not None
                or public_s3.password is not None
                or public_s3.params
                or public_s3.query
                or public_s3.fragment
            ):
                raise ValueError("production artifact links require an HTTPS public S3 endpoint")
        return self

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
