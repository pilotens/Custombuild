"""Fail-closed cold-start recovery for the durable storage ledger.

This process is deliberately one-shot.  It runs with the short-lived migrator
database login before the storage attestor and every writer.  A token/epoch
maintenance gate prevents new reservations while expired staging objects are
terminalized, deleted from the provider, and finalized through the existing
token-bound reaper contract.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from sqlalchemy import Connection, Engine, RowMapping, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from services.api.app.config_guards import (
    validate_production_database_url,
    validate_production_s3_bucket,
    validate_production_s3_credentials,
)
from services.api.app.storage_reaper import (
    StorageReapClaim,
    StorageReaperS3Client,
    StorageReapResult,
    StorageReapStatus,
    claim_storage_reap_batch,
    reap_storage_claim,
)

DEFAULT_TIMEOUT_SECONDS = 10_860
MAX_TIMEOUT_SECONDS = 14_400
DEFAULT_POLL_SECONDS = 2.0
MAINTENANCE_LEASE_SECONDS = 300
REAP_CLAIM_SECONDS = 180
REAP_BATCH_SIZE = 50
_CANONICAL_UUID_LENGTH = 36


class StorageRecoveryError(RuntimeError):
    """The recovery proof could not be completed safely."""


@dataclass(frozen=True)
class RecoverySettings:
    app_env: str
    database_url: str
    statement_timeout_seconds: int
    lock_timeout_seconds: int
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    timeout_seconds: int
    poll_seconds: float


@dataclass(frozen=True)
class RecoverySnapshot:
    database_now: datetime
    target_count: int
    domain_reference_count: int
    live_generation_lease_count: int
    next_eligible_at: datetime | None


class RecoveryStore(Protocol):
    session_factory: sessionmaker[Session]

    def begin(self, token: str, bucket: str) -> int: ...

    def renew(self, token: str, epoch: int) -> None: ...

    def assert_bucket(self, token: str, epoch: int, bucket: str) -> None: ...

    def organization_ids(self, token: str, epoch: int) -> tuple[str, ...]: ...

    def terminalize_expired_staging_jobs(
        self, organization_id: str, token: str, epoch: int
    ) -> int: ...

    def snapshot(self, token: str, epoch: int) -> RecoverySnapshot: ...

    def finish(self, token: str, epoch: int) -> None: ...


ClaimBatch = Callable[..., tuple[StorageReapClaim, ...]]
ReapClaim = Callable[..., StorageReapResult]


def _integer_environment(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise StorageRecoveryError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum or str(value) != raw:
        raise StorageRecoveryError(f"{name} must be a canonical integer in {minimum}..{maximum}")
    return value


def _float_environment(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise StorageRecoveryError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise StorageRecoveryError(f"{name} is outside the safe range")
    return value


def load_settings() -> RecoverySettings:
    """Load only the credentials and bounds needed by the one-shot process."""

    app_env = os.environ.get("APP_ENV", "development")
    if app_env not in {"development", "test", "production"}:
        raise StorageRecoveryError("APP_ENV is not canonical")
    database_url = os.environ.get("DATABASE_URL", "")
    try:
        parsed_database = make_url(database_url)
    except Exception as exc:
        raise StorageRecoveryError("DATABASE_URL is invalid") from exc
    if (
        parsed_database.get_backend_name() != "postgresql"
        or parsed_database.username != "custombuild_migrator"
        or not parsed_database.password
    ):
        raise StorageRecoveryError(
            "DATABASE_URL must use the password-authenticated custombuild_migrator role"
        )
    if app_env == "production":
        try:
            validate_production_database_url(
                database_url,
                expected_username="custombuild_migrator",
            )
        except ValueError as exc:
            raise StorageRecoveryError(str(exc)) from exc

    s3_endpoint = os.environ.get("S3_ENDPOINT", "")
    endpoint = urlparse(s3_endpoint)
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise StorageRecoveryError("S3_ENDPOINT is invalid")
    if app_env == "production" and s3_endpoint != "http://object-storage:8333":
        raise StorageRecoveryError(
            "production recovery must use the internal object-storage endpoint"
        )
    s3_access_key = os.environ.get("S3_ACCESS_KEY", "")
    s3_secret_key = os.environ.get("S3_SECRET_KEY", "")
    s3_bucket = os.environ.get("S3_BUCKET", "")
    if app_env == "production":
        try:
            validate_production_s3_credentials(s3_access_key, s3_secret_key)
            validate_production_s3_bucket(s3_bucket)
        except ValueError as exc:
            raise StorageRecoveryError(str(exc)) from exc
    elif not s3_access_key or not s3_secret_key or not s3_bucket:
        raise StorageRecoveryError("S3 credentials and bucket are required")

    return RecoverySettings(
        app_env=app_env,
        database_url=database_url,
        statement_timeout_seconds=_integer_environment(
            "DATABASE_STATEMENT_TIMEOUT_SECONDS", 60, minimum=1, maximum=120
        ),
        lock_timeout_seconds=_integer_environment(
            "DATABASE_LOCK_TIMEOUT_SECONDS", 10, minimum=1, maximum=30
        ),
        s3_endpoint=s3_endpoint,
        s3_access_key=s3_access_key,
        s3_secret_key=s3_secret_key,
        s3_bucket=s3_bucket,
        timeout_seconds=_integer_environment(
            "STORAGE_RECOVERY_TIMEOUT_SECONDS",
            DEFAULT_TIMEOUT_SECONDS,
            minimum=60,
            maximum=MAX_TIMEOUT_SECONDS,
        ),
        poll_seconds=_float_environment(
            "STORAGE_RECOVERY_POLL_SECONDS",
            DEFAULT_POLL_SECONDS,
            minimum=0.1,
            maximum=10.0,
        ),
    )


def _aware_utc(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise StorageRecoveryError(f"database returned invalid {name}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_uuid(value: str, *, name: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise StorageRecoveryError(f"{name} is not a UUID") from exc
    if len(value) != _CANONICAL_UUID_LENGTH or str(parsed) != value:
        raise StorageRecoveryError(f"{name} is not a canonical lowercase UUID")
    return value


def _canonical_bucket(value: str) -> str:
    try:
        validate_production_s3_bucket(value)
    except ValueError as exc:
        raise StorageRecoveryError(str(exc)) from exc
    return value


class PostgresRecoveryStore:
    """Small migrator-only database adapter for the recovery state machine."""

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise StorageRecoveryError("storage recovery requires PostgreSQL")
        self.engine = engine
        self.session_factory = sessionmaker(
            bind=engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @staticmethod
    def _set_tenant(connection: Connection, organization_id: str) -> None:
        connection.execute(
            text("SELECT set_config('app.current_organization_id', :organization_id, true)"),
            {"organization_id": organization_id},
        )

    @staticmethod
    def _locked_gate(connection: Connection, token: str, epoch: int) -> RowMapping:
        row = (
            connection.execute(
                text(
                    "SELECT *, clock_timestamp() AS database_now, "
                    "pg_postmaster_start_time() AS database_started_at "
                    "FROM storage_global_quotas WHERE id = 1 FOR UPDATE"
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise StorageRecoveryError("global storage quota singleton is missing")
        if row["maintenance_token"] != token or row["maintenance_epoch"] != epoch:
            raise StorageRecoveryError("storage recovery maintenance ownership was lost")
        database_now = _aware_utc(row["database_now"], name="database clock")
        database_started_at = _aware_utc(
            row["database_started_at"],
            name="database start time",
        )
        maintenance_database_started_at = _aware_utc(
            row["maintenance_database_started_at"],
            name="maintenance database start time",
        )
        if maintenance_database_started_at != database_started_at:
            raise StorageRecoveryError("storage recovery maintenance boot ownership was lost")
        owner_expiry = _aware_utc(
            row["maintenance_owner_expires_at"],
            name="maintenance owner expiry",
        )
        if owner_expiry <= database_now:
            raise StorageRecoveryError("storage recovery maintenance ownership expired")
        return row

    @classmethod
    def _target_count(cls, connection: Connection) -> int:
        organization_ids = tuple(
            str(value)
            for value in connection.execute(
                text("SELECT id FROM organizations ORDER BY id")
            ).scalars()
        )
        count = 0
        for organization_id in organization_ids:
            _canonical_uuid(organization_id, name="organization id")
            cls._set_tenant(connection, organization_id)
            count += int(
                connection.execute(
                    text(
                        "SELECT count(*) FROM stored_objects"
                        " WHERE organization_id = :organization_id"
                        " AND state IN ('reserved', 'reaping', 'delete_pending')"
                    ),
                    {"organization_id": organization_id},
                ).scalar_one()
            )
        return count

    @classmethod
    def _assert_no_tombstone_overlap(
        cls,
        connection: Connection,
        capacity_bucket: str,
    ) -> None:
        """Prove no live ledger row reuses a retired physical or logical identity."""

        organization_ids = tuple(
            str(value)
            for value in connection.execute(
                text("SELECT id FROM organizations ORDER BY id")
            ).scalars()
        )
        for organization_id in organization_ids:
            _canonical_uuid(organization_id, name="organization id")
            cls._set_tenant(connection, organization_id)
            overlap = connection.execute(
                text(
                    "SELECT stored.object_key FROM stored_objects AS stored"
                    " JOIN storage_object_tombstones AS tombstone"
                    "   ON tombstone.capacity_bucket = :capacity_bucket"
                    "  AND (tombstone.object_key = stored.object_key"
                    "       OR tombstone.idempotency_key = stored.idempotency_key)"
                    " WHERE stored.organization_id = :organization_id LIMIT 1"
                ),
                {
                    "capacity_bucket": capacity_bucket,
                    "organization_id": organization_id,
                },
            ).scalar_one_or_none()
            if overlap is not None:
                raise StorageRecoveryError(
                    "live storage ledger overlaps a permanently retired storage identity"
                )

    def begin(self, token: str, bucket: str) -> int:
        _canonical_uuid(token, name="maintenance token")
        canonical_bucket = _canonical_bucket(bucket)
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT *, clock_timestamp() AS database_now, "
                        "pg_postmaster_start_time() AS database_started_at "
                        "FROM storage_global_quotas WHERE id = 1 FOR UPDATE"
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise StorageRecoveryError("global storage quota singleton is missing")
            database_now = _aware_utc(row["database_now"], name="database clock")
            database_started_at = _aware_utc(
                row["database_started_at"],
                name="database start time",
            )
            ledger_bucket = row["capacity_bucket"]
            if ledger_bucket is None:
                if (
                    int(row["reserved_count"]) != 0
                    or int(row["reserved_bytes"]) != 0
                    or self._target_count(connection) != 0
                ):
                    raise StorageRecoveryError(
                        "an unbound non-empty storage ledger cannot be recovered"
                    )
            elif ledger_bucket != canonical_bucket:
                raise StorageRecoveryError("configured S3 bucket does not match the storage ledger")
            self._assert_no_tombstone_overlap(connection, canonical_bucket)
            current_token = row["maintenance_token"]
            if current_token is not None:
                current_expiry = _aware_utc(
                    row["maintenance_owner_expires_at"],
                    name="maintenance owner expiry",
                )
                current_database_started_at = _aware_utc(
                    row["maintenance_database_started_at"],
                    name="maintenance database start time",
                )
                if (
                    current_database_started_at == database_started_at
                    and current_expiry > database_now
                ):
                    raise StorageRecoveryError("another storage recovery owns maintenance")
            epoch = int(row["maintenance_epoch"]) + 1
            connection.execute(
                text(
                    "UPDATE storage_global_quotas SET "
                    "maintenance_token = :token, maintenance_epoch = :epoch, "
                    "maintenance_started_at = :database_now, "
                    "maintenance_database_started_at = :database_started_at, "
                    "maintenance_owner_expires_at = :database_now + "
                    "make_interval(secs => :lease_seconds), "
                    "capacity_bucket = coalesce(capacity_bucket, :capacity_bucket), "
                    "capacity_verified = false, capacity_verified_at = :database_now, "
                    "recovery_database_started_at = NULL, recovery_completed_at = NULL, "
                    "updated_at = :database_now WHERE id = 1"
                ),
                {
                    "token": token,
                    "epoch": epoch,
                    "database_now": database_now,
                    "database_started_at": database_started_at,
                    "lease_seconds": MAINTENANCE_LEASE_SECONDS,
                    "capacity_bucket": canonical_bucket,
                },
            )
        return epoch

    def renew(self, token: str, epoch: int) -> None:
        with self.engine.begin() as connection:
            self._locked_gate(connection, token, epoch)
            updated = connection.execute(
                text(
                    "UPDATE storage_global_quotas SET "
                    "maintenance_owner_expires_at = clock_timestamp() + "
                    "make_interval(secs => :lease_seconds), "
                    "updated_at = clock_timestamp() "
                    "WHERE id = 1 AND maintenance_token = :token "
                    "AND maintenance_epoch = :epoch "
                    "AND maintenance_database_started_at = "
                    "pg_postmaster_start_time() RETURNING id"
                ),
                {
                    "token": token,
                    "epoch": epoch,
                    "lease_seconds": MAINTENANCE_LEASE_SECONDS,
                },
            ).scalar_one_or_none()
            if updated != 1:
                raise StorageRecoveryError("storage recovery maintenance renewal failed")

    def assert_bucket(self, token: str, epoch: int, bucket: str) -> None:
        canonical_bucket = _canonical_bucket(bucket)
        with self.engine.begin() as connection:
            row = self._locked_gate(connection, token, epoch)
            if row["capacity_bucket"] != canonical_bucket:
                raise StorageRecoveryError("configured S3 bucket does not match the storage ledger")

    def organization_ids(self, token: str, epoch: int) -> tuple[str, ...]:
        with self.engine.begin() as connection:
            self._locked_gate(connection, token, epoch)
            values = tuple(
                str(value)
                for value in connection.execute(
                    text("SELECT id FROM organizations ORDER BY id")
                ).scalars()
            )
        for value in values:
            _canonical_uuid(value, name="organization id")
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise StorageRecoveryError("organization order is not canonical")
        return values

    def terminalize_expired_staging_jobs(self, organization_id: str, token: str, epoch: int) -> int:
        _canonical_uuid(organization_id, name="organization id")
        with self.engine.begin() as connection:
            gate = self._locked_gate(connection, token, epoch)
            database_now = _aware_utc(gate["database_now"], name="database clock")
            self._set_tenant(connection, organization_id)
            rows = (
                connection.execute(
                    text(
                        "WITH eligible AS ("
                        " SELECT generation_job.id"
                        " FROM generation_jobs AS generation_job"
                        " WHERE generation_job.organization_id = :organization_id"
                        "   AND generation_job.status IN ('queued', 'running')"
                        "   AND (generation_job.lease_expires_at IS NULL"
                        "        OR generation_job.lease_expires_at <= :database_now)"
                        "   AND EXISTS ("
                        "       SELECT 1 FROM stored_objects AS owned"
                        "       WHERE owned.organization_id = generation_job.organization_id"
                        "         AND owned.owner_type = 'generation_job'"
                        "         AND owned.owner_id = generation_job.id"
                        "         AND ((owned.state = 'reserved'"
                        "               AND owned.lease_expires_at <= :database_now)"
                        "              OR (owned.state = 'reaping'"
                        "                  AND owned.claim_expires_at <= :database_now"
                        "                  AND pg_catalog.substr(owned.claim_token, 15, 1) = '4'))"
                        "   )"
                        "   AND NOT EXISTS ("
                        "       SELECT 1 FROM stored_objects AS live_owned"
                        "       WHERE live_owned.organization_id = generation_job.organization_id"
                        "         AND live_owned.owner_type = 'generation_job'"
                        "         AND live_owned.owner_id = generation_job.id"
                        "         AND ((live_owned.state = 'reserved'"
                        "               AND live_owned.lease_expires_at > :database_now)"
                        "              OR (live_owned.state = 'reaping'"
                        "                  AND live_owned.claim_expires_at > :database_now))"
                        "   )"
                        " FOR UPDATE OF generation_job"
                        ") UPDATE generation_jobs AS generation_job SET"
                        " status = 'failed', lease_token = NULL, lease_expires_at = NULL,"
                        " error = 'storage recovery reclaimed expired staging reservation',"
                        " finished_at = :database_now, updated_at = :database_now"
                        " FROM eligible WHERE generation_job.organization_id = :organization_id"
                        " AND generation_job.id = eligible.id RETURNING generation_job.id"
                    ),
                    {
                        "organization_id": organization_id,
                        "database_now": database_now,
                    },
                )
                .scalars()
                .all()
            )
        return len(rows)

    def snapshot(self, token: str, epoch: int) -> RecoverySnapshot:
        with self.engine.begin() as connection:
            gate = self._locked_gate(connection, token, epoch)
            capacity_bucket = _canonical_bucket(str(gate["capacity_bucket"]))
            self._assert_no_tombstone_overlap(connection, capacity_bucket)
            database_now = _aware_utc(gate["database_now"], name="database clock")
            organization_ids = tuple(
                str(value)
                for value in connection.execute(
                    text("SELECT id FROM organizations ORDER BY id")
                ).scalars()
            )
            target_count = 0
            domain_reference_count = 0
            live_generation_lease_count = 0
            next_eligible: datetime | None = None
            for organization_id in organization_ids:
                _canonical_uuid(organization_id, name="organization id")
                self._set_tenant(connection, organization_id)
                row = (
                    connection.execute(
                        text(
                            "SELECT"
                            " count(*) FILTER (WHERE stored.state IN "
                            "('reserved', 'reaping', 'delete_pending')) AS target_count,"
                            " count(*) FILTER (WHERE stored.state IN "
                            "('reserved', 'reaping', 'delete_pending') AND ("
                            "   EXISTS (SELECT 1 FROM imported_assets AS imported"
                            "           WHERE imported.organization_id = stored.organization_id"
                            "             AND imported.object_key = stored.object_key)"
                            "   OR EXISTS (SELECT 1 FROM external_evidence AS evidence"
                            "              WHERE evidence.organization_id = stored.organization_id"
                            "                AND evidence.object_key = stored.object_key)"
                            "   OR EXISTS (SELECT 1 FROM artifacts AS artifact"
                            "              WHERE artifact.organization_id = stored.organization_id"
                            "                AND artifact.object_key = stored.object_key)"
                            " )) AS domain_reference_count,"
                            " count(*) FILTER (WHERE stored.state IN "
                            "('reserved', 'reaping', 'delete_pending')"
                            " AND stored.owner_type = 'generation_job'"
                            " AND EXISTS (SELECT 1 FROM generation_jobs AS generation_job"
                            "             WHERE generation_job.organization_id"
                            " = stored.organization_id"
                            "               AND generation_job.id = stored.owner_id"
                            "               AND generation_job.lease_expires_at > :database_now))"
                            " AS live_generation_lease_count,"
                            " min(CASE"
                            "   WHEN stored.state = 'reserved' THEN stored.lease_expires_at"
                            "   WHEN stored.state = 'reaping' THEN stored.claim_expires_at"
                            "   ELSE NULL END) FILTER (WHERE"
                            "      (stored.state = 'reserved'"
                            "       AND stored.lease_expires_at > :database_now)"
                            "   OR (stored.state = 'reaping'"
                            "       AND stored.claim_expires_at > :database_now))"
                            " AS next_eligible_at"
                            " FROM stored_objects AS stored"
                            " WHERE stored.organization_id = :organization_id"
                        ),
                        {
                            "database_now": database_now,
                            "organization_id": organization_id,
                        },
                    )
                    .mappings()
                    .one()
                )
                target_count += int(row["target_count"])
                domain_reference_count += int(row["domain_reference_count"])
                live_generation_lease_count += int(row["live_generation_lease_count"])
                raw_eligible = row["next_eligible_at"]
                if raw_eligible is not None:
                    candidate = _aware_utc(
                        raw_eligible,
                        name="next recovery eligibility",
                    )
                    if next_eligible is None or candidate < next_eligible:
                        next_eligible = candidate
        return RecoverySnapshot(
            database_now=database_now,
            target_count=target_count,
            domain_reference_count=domain_reference_count,
            live_generation_lease_count=live_generation_lease_count,
            next_eligible_at=next_eligible,
        )

    def finish(self, token: str, epoch: int) -> None:
        with self.engine.begin() as connection:
            gate = self._locked_gate(connection, token, epoch)
            capacity_bucket = _canonical_bucket(str(gate["capacity_bucket"]))
            self._assert_no_tombstone_overlap(connection, capacity_bucket)
            database_now = _aware_utc(gate["database_now"], name="database clock")
            global_residual = (
                connection.execute(
                    text(
                        "SELECT reserved_count AS global_reserved_count,"
                        " reserved_bytes AS global_reserved_bytes"
                        " FROM storage_global_quotas WHERE id = 1"
                    )
                )
                .mappings()
                .one()
            )
            organization_ids = tuple(
                str(value)
                for value in connection.execute(
                    text("SELECT id FROM organizations ORDER BY id")
                ).scalars()
            )
            # Count lifecycle targets independently from the quota table. A
            # missing quota row must never make a stored object disappear from
            # the proof that opens the maintenance gate.
            target_count = self._target_count(connection)
            tenant_reserved_count = 0
            tenant_reserved_bytes = 0
            for organization_id in organization_ids:
                _canonical_uuid(organization_id, name="organization id")
                self._set_tenant(connection, organization_id)
                object_count = int(
                    connection.execute(
                        text(
                            "SELECT count(*) FROM stored_objects"
                            " WHERE organization_id = :organization_id"
                        ),
                        {"organization_id": organization_id},
                    ).scalar_one()
                )
                quota = (
                    connection.execute(
                        text(
                            "SELECT reserved_count, reserved_bytes"
                            " FROM storage_tenant_quotas"
                            " WHERE organization_id = :organization_id"
                        ),
                        {"organization_id": organization_id},
                    )
                    .mappings()
                    .one_or_none()
                )
                if object_count and quota is None:
                    raise StorageRecoveryError("stored objects exist without a tenant quota row")
                if quota is not None:
                    tenant_reserved_count += int(quota["reserved_count"])
                    tenant_reserved_bytes += int(quota["reserved_bytes"])
            if (
                target_count != 0
                or tenant_reserved_count != 0
                or tenant_reserved_bytes != 0
                or int(global_residual["global_reserved_count"]) != 0
                or int(global_residual["global_reserved_bytes"]) != 0
            ):
                raise StorageRecoveryError(
                    "storage recovery cannot open with residual reservations"
                )
            updated = connection.execute(
                text(
                    "UPDATE storage_global_quotas SET"
                    " maintenance_token = NULL, maintenance_started_at = NULL,"
                    " maintenance_owner_expires_at = NULL,"
                    " maintenance_database_started_at = NULL,"
                    " recovery_database_started_at = pg_postmaster_start_time(),"
                    " recovery_completed_at = :database_now,"
                    " capacity_verified = false, updated_at = :database_now"
                    " WHERE id = 1 AND maintenance_token = :token"
                    " AND maintenance_epoch = :epoch"
                    " AND maintenance_database_started_at = "
                    "pg_postmaster_start_time() RETURNING id"
                ),
                {
                    "token": token,
                    "epoch": epoch,
                    "database_now": database_now,
                },
            ).scalar_one_or_none()
            if updated != 1:
                raise StorageRecoveryError("storage recovery could not open maintenance")


def _bounded_sleep_seconds(
    snapshot: RecoverySnapshot,
    *,
    poll_seconds: float,
) -> float:
    if snapshot.next_eligible_at is None:
        return poll_seconds
    until_eligible = (snapshot.next_eligible_at - snapshot.database_now).total_seconds()
    return max(0.05, min(poll_seconds, until_eligible))


def recover_storage(
    store: RecoveryStore,
    s3_client: StorageReaperS3Client,
    bucket: str,
    *,
    timeout_seconds: int,
    poll_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    claim_batch: ClaimBatch = claim_storage_reap_batch,
    reap_claim: ReapClaim = reap_storage_claim,
) -> None:
    """Recover every tenant in stable order and open only from a zero ledger."""

    token = str(uuid.uuid4())
    deadline = monotonic() + timeout_seconds
    canonical_bucket = _canonical_bucket(bucket)
    epoch = store.begin(token, canonical_bucket)
    while True:
        if monotonic() >= deadline:
            raise StorageRecoveryError("storage recovery exceeded its bounded timeout")
        store.renew(token, epoch)
        store.assert_bucket(token, epoch, canonical_bucket)
        organizations = store.organization_ids(token, epoch)
        if organizations != tuple(sorted(organizations)):
            raise StorageRecoveryError("storage recovery tenant order drifted")
        for organization_id in organizations:
            if monotonic() >= deadline:
                raise StorageRecoveryError("storage recovery exceeded its bounded timeout")
            store.renew(token, epoch)
            store.assert_bucket(token, epoch, canonical_bucket)
            store.terminalize_expired_staging_jobs(organization_id, token, epoch)
            store.assert_bucket(token, epoch, canonical_bucket)
            claims = claim_batch(
                store.session_factory,
                organization_id,
                batch_size=REAP_BATCH_SIZE,
                claim_duration=timedelta(seconds=REAP_CLAIM_SECONDS),
            )
            for claim in claims:
                store.renew(token, epoch)
                store.assert_bucket(token, epoch, canonical_bucket)
                result = reap_claim(
                    store.session_factory,
                    s3_client,
                    bucket,
                    claim,
                )
                if result.status == StorageReapStatus.domain_reference:
                    raise StorageRecoveryError("storage recovery is blocked by a domain reference")
                if result.status == StorageReapStatus.identity_mismatch:
                    raise StorageRecoveryError(
                        "storage recovery object identity differs from the ledger claim"
                    )

        snapshot = store.snapshot(token, epoch)
        if snapshot.target_count == 0:
            store.finish(token, epoch)
            return
        if snapshot.domain_reference_count:
            raise StorageRecoveryError("storage recovery is blocked by a domain reference")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise StorageRecoveryError("storage recovery exceeded its bounded timeout")
        sleep(min(remaining, _bounded_sleep_seconds(snapshot, poll_seconds=poll_seconds)))


def _engine(settings: RecoverySettings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        connect_args={
            "connect_timeout": min(settings.statement_timeout_seconds, 10),
            "options": (
                f"-c statement_timeout={settings.statement_timeout_seconds * 1000} "
                f"-c lock_timeout={settings.lock_timeout_seconds * 1000}"
            ),
        },
    )


def _s3_client(settings: RecoverySettings) -> Any:
    timeout = min(float(settings.statement_timeout_seconds), 30.0)
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=timeout,
            read_timeout=timeout,
            retries={"total_max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def main() -> int:
    engine: Engine | None = None
    s3_client: Any | None = None
    try:
        settings = load_settings()
        engine = _engine(settings)
        store = PostgresRecoveryStore(engine)
        s3_client = _s3_client(settings)
        s3_client.head_bucket(Bucket=settings.s3_bucket)
        recover_storage(
            store,
            s3_client,
            settings.s3_bucket,
            timeout_seconds=settings.timeout_seconds,
            poll_seconds=settings.poll_seconds,
        )
    except Exception as exc:
        print(f"storage recovery failed closed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if s3_client is not None:
            close = getattr(s3_client, "close", None)
            if callable(close):
                close()
        if engine is not None:
            engine.dispose()
    print("storage recovery completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
