from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from time import monotonic
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
    "readiness_probe",
]
REQUIRED_DATABASE_REVISION = "0022_durable_furniture_review_exports"
READINESS_CACHE_TTL_SECONDS = 5.0
_DEPENDENCY_CHECK_COUNT = 6


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


def _dependency_checks() -> tuple[tuple[DependencyName, Callable[[Settings], None]], ...]:
    return (
        ("database", check_database),
        ("joint_retention_registry", check_joint_retention_registry),
        ("storage_capacity", check_storage_capacity),
        ("redis", check_redis),
        ("object_storage", check_object_storage),
        ("rule_engine", check_rule_engine),
    )


def _probe_dependencies_once(
    settings: Settings,
    executor: Executor,
) -> tuple[dict[str, str], list[DependencyFailure]]:
    checks = _dependency_checks()
    statuses: dict[str, str] = {}
    failures: list[DependencyFailure] = []
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


class ReadinessProbeCoordinator:
    """Cache one bounded dependency probe and reject overlapping probe work.

    The fixed executor caps dependency work at one worker per check. If a
    provider call stalls despite its client timeout, subsequent readiness
    requests fail closed immediately instead of allocating another executor or
    waiting behind the stalled probe.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = READINESS_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = monotonic,
        executor: Executor | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("readiness cache TTL must be positive")
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._executor = executor or ThreadPoolExecutor(
            max_workers=_DEPENDENCY_CHECK_COUNT,
            thread_name_prefix="readiness",
        )
        self._lock = Lock()
        self._in_flight = False
        self._cached_settings: Settings | None = None
        self._cached_statuses: tuple[tuple[str, str], ...] | None = None
        self._cached_failures: tuple[DependencyFailure, ...] = ()
        self._expires_at = 0.0

    @staticmethod
    def _copy_result(
        statuses: tuple[tuple[str, str], ...],
        failures: tuple[DependencyFailure, ...],
    ) -> tuple[dict[str, str], list[DependencyFailure]]:
        return dict(statuses), list(failures)

    @staticmethod
    def _coordination_failure(error_type: str) -> tuple[dict[str, str], list[DependencyFailure]]:
        return (
            {"readiness_probe": "unavailable"},
            [DependencyFailure(name="readiness_probe", error_type=error_type)],
        )

    def probe(self, settings: Settings) -> tuple[dict[str, str], list[DependencyFailure]]:
        with self._lock:
            now = self._clock()
            if (
                self._cached_settings is settings
                and self._cached_statuses is not None
                and now < self._expires_at
            ):
                return self._copy_result(self._cached_statuses, self._cached_failures)
            if self._in_flight:
                return self._coordination_failure("ReadinessProbeInFlight")
            self._in_flight = True

        try:
            result = _probe_dependencies_once(settings, self._executor)
        except Exception as exc:
            result = self._coordination_failure(type(exc).__name__)
        except BaseException:
            with self._lock:
                self._in_flight = False
            raise

        statuses, failures = result
        cached_statuses = tuple(statuses.items())
        cached_failures = tuple(failures)
        with self._lock:
            self._cached_settings = settings
            self._cached_statuses = cached_statuses
            self._cached_failures = cached_failures
            self._expires_at = self._clock() + self._ttl_seconds
            self._in_flight = False
        return self._copy_result(cached_statuses, cached_failures)


_READINESS_PROBE_COORDINATOR = ReadinessProbeCoordinator()


def probe_dependencies(settings: Settings) -> tuple[dict[str, str], list[DependencyFailure]]:
    return _READINESS_PROBE_COORDINATOR.probe(settings)
