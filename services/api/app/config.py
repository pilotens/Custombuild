from __future__ import annotations

import re
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
    validate_production_s3_bucket,
    validate_production_s3_credentials,
)

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
PRIVATE_PROXY_NETWORKS = tuple(
    ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
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
    database_statement_timeout_seconds: int = Field(default=60, ge=1, le=120)
    database_lock_timeout_seconds: int = Field(default=10, ge=1, le=30)
    redis_url: str = "redis://localhost:6379/0"
    readiness_timeout_seconds: int = Field(default=2, ge=1, le=10)
    rate_limit_requests: int = Field(default=180, ge=10, le=10_000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=3_600)
    request_body_idle_timeout_seconds: int = Field(default=5, ge=1, le=30)
    request_body_total_timeout_seconds: int = Field(default=30, ge=5, le=120)
    trusted_proxy_cidrs: str = ""
    oidc_issuer: str = ""
    oidc_audience: str = "custombuild-api"
    artifact_signing_secret: str = Field(
        default="development-only-signing-secret-32-bytes",
        min_length=32,
    )
    artifact_url_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    artifact_stream_timeout_seconds: int = Field(default=120, ge=30, le=600)
    joint_retention_trust_registry_json: str = Field(default="", max_length=262_144)
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "custombuild"
    s3_secret_key: str = "development-only-object-secret"  # noqa: S105
    s3_bucket: str = "custombuild-artifacts"
    storage_capacity_operator_config_sha256: str = "unverified"
    storage_capacity_volume_identity: str = "development-local-volume"
    storage_capacity_provisioned_bytes: int = Field(
        default=256 * 1024**3,
        ge=1,
        le=MAX_DATABASE_INTEGER,
    )
    storage_capacity_metadata_overhead_bytes: int = Field(
        default=1024**3,
        ge=1,
        le=MAX_DATABASE_INTEGER,
    )
    storage_capacity_emergency_reserve_bytes: int = Field(
        default=4 * 1024**3,
        ge=1,
        le=MAX_DATABASE_INTEGER,
    )
    storage_capacity_headroom_bytes: int = Field(
        default=5 * 1024**3,
        ge=1,
        le=MAX_DATABASE_INTEGER,
    )
    storage_capacity_byte_limit: int = Field(
        default=251 * 1024**3,
        ge=1,
        le=MAX_DATABASE_INTEGER,
    )
    storage_capacity_object_limit: int = Field(
        default=1_000_000,
        ge=1,
        le=MAX_DATABASE_INTEGER,
    )
    storage_capacity_deploy_descriptor_sha256: str = "unverified"
    storage_capacity_max_age_seconds: int = Field(default=600, ge=60, le=600)
    cors_origins: str = "http://localhost:3000"

    @model_validator(mode="after")
    def production_guards(self) -> Settings:
        if self.database_lock_timeout_seconds >= self.database_statement_timeout_seconds:
            raise ValueError(
                "DATABASE_LOCK_TIMEOUT_SECONDS must be shorter than the statement timeout"
            )
        if self.request_body_idle_timeout_seconds > self.request_body_total_timeout_seconds:
            raise ValueError(
                "REQUEST_BODY_IDLE_TIMEOUT_SECONDS cannot exceed the total body timeout"
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
        if self.app_env == "production" and self.auth_mode != "oidc":
            raise ValueError("production requires AUTH_MODE=oidc")
        if self.app_env == "production" and not self.production_four_eyes_required:
            raise ValueError("production requires PRODUCTION_FOUR_EYES_REQUIRED=true")
        if self.auth_mode == "oidc" and self.app_env != "production":
            raise ValueError("OIDC mode requires APP_ENV=production")
        if self.auth_mode == "oidc" and not self.oidc_issuer:
            raise ValueError("OIDC_ISSUER is required in OIDC mode")
        if self.app_env == "production":
            missing_capacity_fields = sorted(PRODUCTION_CAPACITY_FIELDS - self.model_fields_set)
            if missing_capacity_fields:
                raise ValueError(
                    "production requires explicit storage-capacity settings: "
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
            validate_production_s3_bucket(self.s3_bucket)
            if _SHA256_PATTERN.fullmatch(self.storage_capacity_operator_config_sha256) is None:
                raise ValueError("production requires STORAGE_CAPACITY_OPERATOR_CONFIG_SHA256")
            if _VOLUME_IDENTITY_PATTERN.fullmatch(self.storage_capacity_volume_identity) is None:
                raise ValueError("production requires a canonical STORAGE_CAPACITY_VOLUME_IDENTITY")
            if _SHA256_PATTERN.fullmatch(self.storage_capacity_deploy_descriptor_sha256) is None:
                raise ValueError("production requires STORAGE_CAPACITY_DEPLOY_DESCRIPTOR_SHA256")
            if self.storage_capacity_max_age_seconds != 600:
                raise ValueError("production STORAGE_CAPACITY_MAX_AGE_SECONDS must be exactly 600")
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
