from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import boto3
from botocore.config import Config
from redis import Redis
from sqlalchemy import text

from .config import Settings
from .db import get_readiness_engine
from .joint_retention_registry import (
    assert_joint_retention_registry_activated,
    joint_retention_registry_binding,
    parse_joint_retention_registry_json,
)
from .oidc_identity import oidc_issuer_sha256
from .storage_capacity import validate_storage_capacity_evidence

DependencyName = Literal[
    "database",
    "joint_retention_registry",
    "storage_capacity",
    "redis",
    "object_storage",
    "rule_engine",
]
REQUIRED_DATABASE_REVISION = "0020_release_cam_approval_identity"


@dataclass(frozen=True)
class DependencyFailure:
    name: DependencyName
    error_type: str


class LegacyUnscopedOIDCIdentityError(RuntimeError):
    """Production contains a pre-issuer-binding identity row."""


class ProductionIdentityBootstrapRequiredError(RuntimeError):
    """Production has no issuer-bound OIDC identity."""


class OIDCIssuerBindingMismatchError(RuntimeError):
    """Production contains an identity bound to another configured issuer."""


class NoCurrentJointRetentionCertifierError(RuntimeError):
    """Production has no certifier key capable of authenticating new evidence."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def check_database(settings: Settings) -> None:
    with get_readiness_engine().connect() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": f"{settings.readiness_timeout_seconds}s"},
            )
            revisions = tuple(
                str(value)
                for value in connection.execute(
                    text("SELECT version_num FROM alembic_version ORDER BY version_num")
                ).scalars()
            )
            if revisions != (REQUIRED_DATABASE_REVISION,):
                raise RuntimeError("database schema revision does not match this runtime")
            if settings.auth_mode == "oidc":
                configured_issuer_sha256 = oidc_issuer_sha256(settings.oidc_issuer)
                legacy_identity_exists = connection.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM public.users WHERE oidc_issuer_sha256 IS NULL"
                        ")"
                    )
                ).scalar_one()
                if legacy_identity_exists is not False:
                    raise LegacyUnscopedOIDCIdentityError(
                        "production contains legacy OIDC identities without issuer binding"
                    )
                other_issuer_identity_exists = connection.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM public.users "
                        "WHERE oidc_issuer_sha256 IS NOT NULL "
                        "AND oidc_issuer_sha256 <> :issuer_sha256"
                        ")"
                    ),
                    {"issuer_sha256": configured_issuer_sha256},
                ).scalar_one()
                if other_issuer_identity_exists is not False:
                    raise OIDCIssuerBindingMismatchError(
                        "production contains identities bound to another OIDC issuer"
                    )
                bound_identity_exists = connection.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM public.users "
                        "WHERE oidc_issuer_sha256 = :issuer_sha256"
                        ")"
                    ),
                    {"issuer_sha256": configured_issuer_sha256},
                ).scalar_one()
                if bound_identity_exists is not True:
                    raise ProductionIdentityBootstrapRequiredError(
                        "production has no issuer-bound OIDC identity"
                    )
        connection.execute(text("SELECT 1"))


def check_storage_capacity(settings: Settings) -> None:
    """Bind production readiness to one fresh, exact physical-capacity attest."""

    if settings.app_env != "production":
        return
    with get_readiness_engine().connect() as connection:
        if connection.dialect.name != "postgresql":
            raise RuntimeError("production storage capacity requires PostgreSQL")
        connection.execute(
            text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{settings.readiness_timeout_seconds}s"},
        )
        row = (
            connection.execute(
                text(
                    "SELECT *, clock_timestamp() AS database_now, "
                    "pg_postmaster_start_time() AS database_started_at "
                    "FROM storage_global_quotas WHERE id = 1"
                )
            )
            .mappings()
            .one_or_none()
        )
    validate_storage_capacity_evidence(settings, row)


def check_joint_retention_registry(settings: Settings) -> None:
    """Require exact activated policy and one currently usable production key."""

    if settings.app_env != "production":
        # Development has no durable activation claim and cannot satisfy this
        # production proof by creating a local SQLite row.
        return
    registry = parse_joint_retention_registry_json(settings.joint_retention_trust_registry_json)
    with get_readiness_engine().connect() as connection:
        if connection.dialect.name != "postgresql":
            raise RuntimeError("production joint-retention trust requires PostgreSQL")
        connection.execute(
            text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{settings.readiness_timeout_seconds}s"},
        )
        binding = joint_retention_registry_binding(registry)
        now = _utc_now()
        if not any(
            issuer.not_before <= now <= issuer.not_after
            and (issuer.revoked_at is None or issuer.revoked_at > now)
            for issuer in binding.registry.issuers
        ):
            raise NoCurrentJointRetentionCertifierError(
                "production has no currently valid, non-revoked joint-retention certifier key"
            )
        assert_joint_retention_registry_activated(
            connection,
            registry,
            production=True,
        )


def check_redis(settings: Settings) -> None:
    timeout = float(settings.readiness_timeout_seconds)
    client: Redis = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )
    try:
        if client.ping() is not True:
            raise RuntimeError("Redis ping did not return success")
    finally:
        client.close()


def check_object_storage(settings: Settings) -> None:
    timeout = float(settings.readiness_timeout_seconds)
    client: Any = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=timeout,
            read_timeout=timeout,
            retries={"total_max_attempts": 1, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )
    try:
        # Probe only the configured application bucket. Requiring
        # ListAllMyBuckets would defeat least-privilege S3 policies.
        client.head_bucket(Bucket=settings.s3_bucket)
    finally:
        client.close()


def check_rule_engine(_settings: Settings) -> None:
    from .design_service import assert_rule_engine_available

    assert_rule_engine_available()


def probe_dependencies(settings: Settings) -> tuple[dict[str, str], list[DependencyFailure]]:
    checks: tuple[tuple[DependencyName, Callable[[Settings], None]], ...] = (
        ("database", check_database),
        ("joint_retention_registry", check_joint_retention_registry),
        ("storage_capacity", check_storage_capacity),
        ("redis", check_redis),
        ("object_storage", check_object_storage),
        ("rule_engine", check_rule_engine),
    )
    statuses: dict[str, str] = {}
    failures: list[DependencyFailure] = []
    with ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix="readiness") as executor:
        futures = [executor.submit(check, settings) for _, check in checks]
        for (name, _), future in zip(checks, futures, strict=True):
            try:
                future.result()
            except Exception as exc:
                statuses[name] = "unavailable"
                failures.append(DependencyFailure(name=name, error_type=type(exc).__name__))
            else:
                statuses[name] = "ok"
    return statuses, failures
