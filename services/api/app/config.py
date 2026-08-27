from __future__ import annotations

from functools import lru_cache
from ipaddress import ip_network
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .config_guards import (
    BuildIdentityValues,
    is_insecure_secret,
    validate_production_build_identity,
    validate_production_database_url,
    validate_production_redis_url,
    validate_production_s3_credentials,
)

DEPENDENCY_LOCK_PATH = Path(__file__).resolve().parents[3] / "uv.lock"
PRIVATE_PROXY_NETWORKS = tuple(
    ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    app_version: str = "0.1.0-local"
    vcs_ref: str = "uncommitted"
    build_date: str = "unknown"
    source_url: str = "unknown"
    source_manifest_sha256: str = "unknown"
    dependency_lock_sha256: str = "unknown"
    auth_mode: Literal["development", "oidc"] = "development"
    production_four_eyes_required: bool = False
    database_url: str = "sqlite+pysqlite:///./custombuild.db"
    redis_url: str = "redis://localhost:6379/0"
    readiness_timeout_seconds: int = Field(default=2, ge=1, le=10)
    rate_limit_requests: int = Field(default=180, ge=10, le=10_000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=3_600)
    trusted_proxy_cidrs: str = ""
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
        if self.app_env == "production" and not self.production_four_eyes_required:
            raise ValueError(
                "production requires PRODUCTION_FOUR_EYES_REQUIRED=true"
            )
        if self.auth_mode == "oidc" and self.app_env != "production":
            raise ValueError("OIDC mode requires APP_ENV=production")
        if self.auth_mode == "oidc" and not self.oidc_issuer:
            raise ValueError("OIDC_ISSUER is required in OIDC mode")
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
            validate_production_database_url(
                self.database_url,
                expected_username="custombuild_api",
            )
            validate_production_redis_url(self.redis_url)
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
            try:
                proxy_networks = [ip_network(value) for value in self.trusted_proxy_networks]
            except ValueError as exc:
                raise ValueError(
                    "production TRUSTED_PROXY_CIDRS must contain valid IP networks"
                ) from exc
            if not proxy_networks or any(
                not any(
                    network.version == allowed.version
                    and network.network_address in allowed
                    and network.broadcast_address in allowed
                    for allowed in PRIVATE_PROXY_NETWORKS
                )
                for network in proxy_networks
            ):
                raise ValueError(
                    "production TRUSTED_PROXY_CIDRS must contain only private IP networks"
                )
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

    @property
    def trusted_proxy_networks(self) -> list[str]:
        values = [value.strip() for value in self.trusted_proxy_cidrs.split(",") if value.strip()]
        return [str(ip_network(value, strict=False)) for value in values]

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
def get_settings() -> Settings:
    return Settings()
