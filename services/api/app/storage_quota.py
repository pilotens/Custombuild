"""Durable, cross-replica storage quota reservations.

The public helpers deliberately own a short database transaction.  Callers
must commit a reservation before uploading bytes, then commit the object only
after the uploaded bytes have been verified against the reserved identity.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from .db import set_tenant_context
from .models import (
    GenerationJob,
    JobStatus,
    StorageGlobalQuota,
    StorageObjectTombstone,
    StorageTenantQuota,
    StoredObject,
    StoredObjectState,
)
from .storage_capacity import (
    StorageCapacitySettings,
    validate_storage_capacity_evidence,
)

GLOBAL_STORAGE_BYTE_LIMIT = 256 * 1024**3
GLOBAL_STORAGE_OBJECT_LIMIT = 1_000_000
TENANT_STORAGE_BYTE_LIMIT = 10 * 1024**3
TENANT_STORAGE_OBJECT_LIMIT = 100_000
MAX_RESERVATION_LEASE = timedelta(hours=3)
DEFAULT_STORAGE_BUSY_RETRY_SECONDS = 125
REAPER_CLAIM_RETRY_SECONDS = 305
MAX_GENERATION_RETRY_AFTER_SECONDS = 3605

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_GENERATION_RETRY_BUSY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])STORAGE_GENERATION_RETRY_BUSY:([0-9]{1,5})(?=\s|$)"
)


class StorageQuotaError(RuntimeError):
    """Base class for fail-closed ledger failures."""


class StorageQuotaExceeded(StorageQuotaError):
    """The complete batch cannot fit inside a durable quota."""


class StorageClaimConflict(StorageQuotaError):
    """A stable key was previously bound to a different object identity."""


class StorageReservationBusy(StorageClaimConflict):
    """An exact immutable reservation is still owned by another live lease."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int = DEFAULT_STORAGE_BUSY_RETRY_SECONDS,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class StorageQuotaInvariantError(StorageQuotaError):
    """The durable ledger is missing or internally inconsistent."""


@dataclass(frozen=True)
class StorageObjectClaim:
    project_id: str
    object_key: str
    sha256: str
    size_bytes: int
    media_type: str
    owner_type: str
    owner_id: str
    idempotency_key: str


@dataclass(frozen=True)
class StorageObjectReservation:
    object_key: str
    state: StoredObjectState
    lease_token: str | None
    newly_reserved: bool


@dataclass(frozen=True)
class StorageReservation:
    objects: tuple[StorageObjectReservation, ...]
    newly_reserved_bytes: int
    newly_reserved_count: int


def _canonical_text(name: str, value: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise StorageClaimConflict(f"{name} is not a canonical non-empty string")
    has_control = any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
    if value != value.strip() or has_control:
        raise StorageClaimConflict(f"{name} contains whitespace or control characters")
    if "\\" in value:
        raise StorageClaimConflict(f"{name} contains a non-canonical path separator")
    return value


def _canonical_uuid(name: str, value: str) -> str:
    canonical_value = _canonical_text(name, value, maximum=36)
    try:
        parsed_value = str(uuid.UUID(canonical_value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise StorageClaimConflict(f"{name} must be a canonical UUID") from exc
    if parsed_value != canonical_value:
        raise StorageClaimConflict(f"{name} must be a canonical UUID")
    return parsed_value


def _validated_claim(claim: StorageObjectClaim) -> StorageObjectClaim:
    if not isinstance(claim, StorageObjectClaim):
        raise StorageClaimConflict("storage claims must use StorageObjectClaim")
    if isinstance(claim.size_bytes, bool) or not isinstance(claim.size_bytes, int):
        raise StorageClaimConflict("size_bytes must be an integer")
    if claim.size_bytes <= 0 or claim.size_bytes > TENANT_STORAGE_BYTE_LIMIT:
        raise StorageClaimConflict("size_bytes is outside the canonical tenant limit")
    if _SHA256_PATTERN.fullmatch(claim.sha256) is None:
        raise StorageClaimConflict("sha256 must be exactly 64 lowercase hexadecimal characters")
    return StorageObjectClaim(
        project_id=_canonical_text("project_id", claim.project_id, maximum=36),
        object_key=_canonical_text("object_key", claim.object_key, maximum=512),
        sha256=claim.sha256,
        size_bytes=claim.size_bytes,
        media_type=_canonical_text("media_type", claim.media_type, maximum=160),
        owner_type=_canonical_text("owner_type", claim.owner_type, maximum=40),
        owner_id=_canonical_text("owner_id", claim.owner_id, maximum=36),
        idempotency_key=_canonical_text("idempotency_key", claim.idempotency_key, maximum=512),
    )


def _normalized_claims(claims: Iterable[StorageObjectClaim]) -> tuple[StorageObjectClaim, ...]:
    by_object_key: dict[str, StorageObjectClaim] = {}
    by_idempotency_key: dict[str, StorageObjectClaim] = {}
    for unvalidated_claim in claims:
        claim = _validated_claim(unvalidated_claim)
        object_collision = by_object_key.get(claim.object_key)
        idempotency_collision = by_idempotency_key.get(claim.idempotency_key)
        if object_collision is not None and object_collision != claim:
            raise StorageClaimConflict("object_key is reused by a different batch identity")
        if idempotency_collision is not None and idempotency_collision != claim:
            raise StorageClaimConflict("idempotency_key is reused by a different batch identity")
        by_object_key[claim.object_key] = claim
        by_idempotency_key[claim.idempotency_key] = claim
    if not by_object_key:
        raise StorageClaimConflict("a storage reservation batch must not be empty")
    return tuple(sorted(by_object_key.values(), key=lambda item: item.object_key))


def _validated_lease(
    lease_token: str,
    lease_expires_at: datetime | None,
    lease_duration: timedelta | None,
    *,
    now: datetime,
) -> tuple[str, datetime]:
    try:
        canonical_token = str(uuid.UUID(lease_token))
    except (AttributeError, TypeError, ValueError) as exc:
        raise StorageClaimConflict("lease_token must be a canonical UUID") from exc
    if canonical_token != lease_token:
        raise StorageClaimConflict("lease_token must be a canonical UUID")
    if (lease_expires_at is None) == (lease_duration is None):
        raise StorageClaimConflict("provide exactly one storage lease expiry or duration")
    if lease_duration is not None:
        if lease_duration <= timedelta(0) or lease_duration > MAX_RESERVATION_LEASE:
            raise StorageClaimConflict("lease duration is outside the canonical lease window")
        normalized_expiry = now + lease_duration
    else:
        assert lease_expires_at is not None
        if lease_expires_at.tzinfo is None or lease_expires_at.utcoffset() is None:
            raise StorageClaimConflict("lease_expires_at must be timezone-aware")
        normalized_expiry = lease_expires_at.astimezone(UTC)
    if normalized_expiry <= now or normalized_expiry > now + MAX_RESERVATION_LEASE:
        raise StorageClaimConflict("lease_expires_at is outside the canonical lease window")
    return canonical_token, normalized_expiry


def _transaction_time(session: Session, override: datetime | None) -> datetime:
    """Use database time in PostgreSQL so replica clock skew cannot steal a lease."""

    if session.get_bind().dialect.name == "postgresql":
        value = session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):
            raise StorageQuotaInvariantError("database did not return a canonical timestamp")
    else:
        value = override or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _claims_json(claims: tuple[StorageObjectClaim, ...]) -> str:
    return json.dumps(
        [
            {
                "project_id": claim.project_id,
                "object_key": claim.object_key,
                "sha256": claim.sha256,
                "size_bytes": claim.size_bytes,
                "media_type": claim.media_type,
                "owner_type": claim.owner_type,
                "owner_id": claim.owner_id,
                "idempotency_key": claim.idempotency_key,
            }
            for claim in claims
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _postgresql_error_message(exc: DBAPIError) -> str:
    original = getattr(exc, "orig", None)
    return str(original if original is not None else exc)


def _validated_generation_retry_after(value: object) -> int:
    if type(value) is not int or value < 0 or value > MAX_GENERATION_RETRY_AFTER_SECONDS:
        raise StorageQuotaInvariantError(
            "generation retry preflight returned a non-canonical delay"
        )
    return value


def generation_retry_after_from_database_error(exc: DBAPIError) -> int | None:
    """Extract a bounded retry delay from the immediate liveness trigger."""

    match = _GENERATION_RETRY_BUSY_PATTERN.search(_postgresql_error_message(exc))
    if match is None:
        return None
    raw_retry_after = match.group(1)
    retry_after = _validated_generation_retry_after(int(raw_retry_after))
    if raw_retry_after != str(retry_after):
        raise StorageQuotaInvariantError("generation retry trigger returned a non-canonical delay")
    if retry_after == 0:
        raise StorageQuotaInvariantError("generation retry trigger returned a non-positive delay")
    return retry_after


def _raise_postgresql_quota_error(exc: DBAPIError) -> NoReturn:
    message = _postgresql_error_message(exc)
    generation_retry_after = generation_retry_after_from_database_error(exc)
    if generation_retry_after is not None:
        raise StorageReservationBusy(
            "generation storage is owned by an active reaper claim",
            retry_after_seconds=generation_retry_after,
        ) from exc
    if "STORAGE_QUOTA_EXCEEDED:" in message:
        raise StorageQuotaExceeded("storage quota is exceeded") from exc
    if "STORAGE_RESERVATION_BUSY:" in message:
        retry_after = (
            REAPER_CLAIM_RETRY_SECONDS
            if "reaper claim" in message
            else DEFAULT_STORAGE_BUSY_RETRY_SECONDS
        )
        raise StorageReservationBusy(
            "stored object has an active different lease",
            retry_after_seconds=retry_after,
        ) from exc
    if "STORAGE_CLAIM_CONFLICT:" in message:
        raise StorageClaimConflict("storage claim conflicts with durable identity") from exc
    if "STORAGE_CLAIM_INVALID:" in message:
        raise StorageClaimConflict("storage claim is not canonical") from exc
    if (
        "STORAGE_QUOTA_INVARIANT:" in message
        or "STORAGE_CAPACITY_UNVERIFIED:" in message
        or "STORAGE_MAINTENANCE_ACTIVE:" in message
        or "STORAGE_RECOVERY_REQUIRED:" in message
        or "STORAGE_TENANT_CONTEXT_MISMATCH" in message
    ):
        raise StorageQuotaInvariantError(
            "storage quota database rejected an unsafe mutation"
        ) from exc
    raise StorageQuotaInvariantError("storage quota database mutation failed") from exc


def prepare_generation_storage_retry(
    session: Session,
    organization_id: str,
    generation_job_id: str,
    *,
    now: datetime | None = None,
) -> int:
    """Lock and prove that terminal generation storage can safely be retried.

    PostgreSQL delegates the race boundary to a SECURITY DEFINER function so
    the API role never needs write access to the storage ledger. SQLite keeps
    the same status/row-lock ordering for deterministic unit and development
    behavior. A positive result is the exact bounded delay before re-checking
    any reaper claim, including the five-second handoff delay after expiry;
    zero is the only success value.
    """

    canonical_organization_id = _canonical_uuid("organization_id", organization_id)
    canonical_job_id = _canonical_uuid("generation_job_id", generation_job_id)
    set_tenant_context(session, canonical_organization_id)
    if session.get_bind().dialect.name == "postgresql":
        try:
            raw_result = session.scalar(
                text(
                    "SELECT public.custombuild_storage_prepare_generation_retry("
                    ":organization_id, :generation_job_id)"
                ),
                {
                    "organization_id": canonical_organization_id,
                    "generation_job_id": canonical_job_id,
                },
            )
        except DBAPIError as exc:
            _raise_postgresql_quota_error(exc)
        return _validated_generation_retry_after(raw_result)

    try:
        job = session.scalar(
            select(GenerationJob)
            .where(
                GenerationJob.organization_id == canonical_organization_id,
                GenerationJob.id == canonical_job_id,
            )
            .with_for_update()
        )
        if job is None or job.status not in {JobStatus.failed, JobStatus.succeeded}:
            raise StorageQuotaInvariantError(
                "generation retry requires the exact terminal tenant job"
            )
        rows = tuple(
            session.scalars(
                select(StoredObject)
                .where(
                    StoredObject.organization_id == canonical_organization_id,
                    StoredObject.owner_type == "generation_job",
                    StoredObject.owner_id == canonical_job_id,
                )
                .order_by(StoredObject.object_key)
                .with_for_update()
            )
        )
        current_time = _transaction_time(session, now)
    except DBAPIError as exc:
        raise StorageQuotaInvariantError("generation retry storage preflight failed") from exc

    retry_after = 0
    for row in rows:
        if row.state == StoredObjectState.reaping:
            claim_expiry = row.claim_expires_at
            claim_token = row.claim_token
            try:
                parsed_claim_token = uuid.UUID(claim_token) if claim_token is not None else None
            except (AttributeError, TypeError, ValueError) as exc:
                raise StorageQuotaInvariantError(
                    "generation retry storage has an invalid reaper claim"
                ) from exc
            if (
                claim_expiry is None
                or claim_token is None
                or parsed_claim_token is None
                or str(parsed_claim_token) != claim_token
                or parsed_claim_token.version not in {4, 5}
            ):
                raise StorageQuotaInvariantError(
                    "generation retry storage has an invalid reaper claim"
                )
            if claim_expiry.tzinfo is None:
                claim_expiry = claim_expiry.replace(tzinfo=UTC)
            else:
                claim_expiry = claim_expiry.astimezone(UTC)
            candidate = (
                math.ceil((claim_expiry - current_time).total_seconds()) + 5
                if claim_expiry > current_time
                else 5
            )
            retry_after = max(
                retry_after,
                _validated_generation_retry_after(candidate),
            )
        elif row.state not in {
            StoredObjectState.reserved,
            StoredObjectState.committed,
            StoredObjectState.delete_pending,
        }:
            raise StorageQuotaInvariantError(
                "generation retry storage has an unknown lifecycle state"
            )
    return _validated_generation_retry_after(retry_after)


def _postgresql_lease_seconds(now: datetime, expires_at: datetime) -> int:
    seconds = math.ceil((expires_at - now).total_seconds())
    if seconds < 1 or seconds > int(MAX_RESERVATION_LEASE.total_seconds()):
        raise StorageClaimConflict("lease duration is outside the canonical lease window")
    return seconds


def _postgresql_reservation_result(
    raw_result: object,
    claims: tuple[StorageObjectClaim, ...],
) -> StorageReservation:
    if isinstance(raw_result, str):
        try:
            raw_result = json.loads(raw_result)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise StorageQuotaInvariantError(
                "storage reserve function returned malformed JSON"
            ) from exc
    if not isinstance(raw_result, Mapping) or frozenset(raw_result) != {
        "objects",
        "newly_reserved_bytes",
        "newly_reserved_count",
    }:
        raise StorageQuotaInvariantError("storage reserve function returned a non-canonical result")
    raw_objects = raw_result["objects"]
    raw_bytes = raw_result["newly_reserved_bytes"]
    raw_count = raw_result["newly_reserved_count"]
    if (
        not isinstance(raw_objects, list)
        or type(raw_bytes) is not int
        or type(raw_count) is not int
        or raw_bytes < 0
        or raw_count < 0
        or raw_count > len(claims)
    ):
        raise StorageQuotaInvariantError("storage reserve function returned invalid counters")
    expected_keys = {claim.object_key for claim in claims}
    reservations: list[StorageObjectReservation] = []
    returned_keys: set[str] = set()
    for raw_object in raw_objects:
        if not isinstance(raw_object, Mapping) or frozenset(raw_object) != {
            "object_key",
            "state",
            "lease_token",
            "newly_reserved",
        }:
            raise StorageQuotaInvariantError("storage reserve function returned an invalid object")
        object_key = raw_object["object_key"]
        state_value = raw_object["state"]
        lease_token = raw_object["lease_token"]
        newly_reserved = raw_object["newly_reserved"]
        if (
            not isinstance(object_key, str)
            or object_key not in expected_keys
            or object_key in returned_keys
            or state_value
            not in {
                StoredObjectState.reserved.value,
                StoredObjectState.committed.value,
            }
            or (lease_token is not None and not isinstance(lease_token, str))
            or type(newly_reserved) is not bool
        ):
            raise StorageQuotaInvariantError(
                "storage reserve function returned inconsistent object identity"
            )
        state = StoredObjectState(state_value)
        if (state == StoredObjectState.committed and lease_token is not None) or (
            state == StoredObjectState.reserved and lease_token is None
        ):
            raise StorageQuotaInvariantError(
                "storage reserve function returned inconsistent lease state"
            )
        returned_keys.add(object_key)
        reservations.append(
            StorageObjectReservation(
                object_key=object_key,
                state=state,
                lease_token=lease_token,
                newly_reserved=newly_reserved,
            )
        )
    if returned_keys != expected_keys or len(raw_objects) != len(claims):
        raise StorageQuotaInvariantError(
            "storage reserve function did not return the complete batch"
        )
    expected_new_count = sum(item.newly_reserved for item in reservations)
    expected_new_bytes = sum(
        claim.size_bytes
        for claim in claims
        if next(item for item in reservations if item.object_key == claim.object_key).newly_reserved
    )
    if raw_count != expected_new_count or raw_bytes != expected_new_bytes:
        raise StorageQuotaInvariantError(
            "storage reserve function counters do not match returned objects"
        )
    return StorageReservation(
        objects=tuple(reservations),
        newly_reserved_bytes=raw_bytes,
        newly_reserved_count=raw_count,
    )


def _require_postgresql_capacity_binding(
    session: Session,
    settings: StorageCapacitySettings | None,
) -> None:
    if settings is None:
        raise StorageQuotaInvariantError(
            "PostgreSQL reservation requires explicit runtime capacity settings"
        )
    # The database function always requires a fresh, internally consistent
    # attestation. Production additionally binds it to this exact runtime.
    if settings.app_env != "production":
        return
    row = (
        session.execute(
            text(
                "SELECT *, clock_timestamp() AS database_now, "
                "pg_postmaster_start_time() AS database_started_at "
                "FROM storage_global_quotas WHERE id = 1"
            )
        )
        .mappings()
        .one_or_none()
    )
    try:
        validate_storage_capacity_evidence(settings, row)
    except RuntimeError as exc:
        raise StorageQuotaInvariantError(
            "storage capacity evidence does not match this runtime"
        ) from exc


def _insert_tenant_quota_if_missing(
    session: Session,
    organization_id: str,
    *,
    now: datetime,
) -> None:
    values = {
        "organization_id": organization_id,
        "byte_limit": TENANT_STORAGE_BYTE_LIMIT,
        "object_limit": TENANT_STORAGE_OBJECT_LIMIT,
        "reserved_bytes": 0,
        "committed_bytes": 0,
        "reserved_count": 0,
        "committed_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        session.execute(
            postgresql_insert(StorageTenantQuota)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["organization_id"])
        )
        return
    if dialect_name == "sqlite":
        session.execute(
            sqlite_insert(StorageTenantQuota)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["organization_id"])
        )
        return
    raise StorageQuotaInvariantError(f"unsupported quota database dialect: {dialect_name}")


def initialize_development_storage_quota(session: Session) -> None:
    """Seed only SQLite's non-production singleton used by local/test schemas."""

    if session.get_bind().dialect.name != "sqlite":
        raise StorageQuotaInvariantError(
            "runtime quota bootstrap is forbidden outside the SQLite development database"
        )
    session.execute(
        sqlite_insert(StorageGlobalQuota)
        .values(
            id=1,
            byte_limit=GLOBAL_STORAGE_BYTE_LIMIT,
            object_limit=GLOBAL_STORAGE_OBJECT_LIMIT,
            reserved_bytes=0,
            committed_bytes=0,
            reserved_count=0,
            committed_count=0,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )


def _insert_stored_object(
    session: Session,
    organization_id: str,
    claim: StorageObjectClaim,
    *,
    lease_token: str,
    lease_expires_at: datetime,
    now: datetime,
) -> bool:
    values = {
        "organization_id": organization_id,
        "object_key": claim.object_key,
        "project_id": claim.project_id,
        "sha256": claim.sha256,
        "size_bytes": claim.size_bytes,
        "media_type": claim.media_type,
        "owner_type": claim.owner_type,
        "owner_id": claim.owner_id,
        "idempotency_key": claim.idempotency_key,
        "state": StoredObjectState.reserved,
        "lease_token": lease_token,
        "lease_expires_at": lease_expires_at,
        "claim_token": None,
        "claim_expires_at": None,
        "created_at": now,
        "updated_at": now,
    }
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        inserted_key = session.execute(
            postgresql_insert(StoredObject)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(StoredObject.object_key)
        ).scalar_one_or_none()
    elif dialect_name == "sqlite":
        inserted_key = session.execute(
            sqlite_insert(StoredObject)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(StoredObject.object_key)
        ).scalar_one_or_none()
    else:
        raise StorageQuotaInvariantError(f"unsupported quota database dialect: {dialect_name}")
    return inserted_key is not None


def _identity_matches(row: StoredObject, claim: StorageObjectClaim) -> bool:
    return (
        row.object_key == claim.object_key
        and row.project_id == claim.project_id
        and row.sha256 == claim.sha256
        and row.size_bytes == claim.size_bytes
        and row.media_type == claim.media_type
        and row.owner_type == claim.owner_type
        and row.owner_id == claim.owner_id
        and row.idempotency_key == claim.idempotency_key
    )


def _locked_claim_rows(
    session: Session,
    organization_id: str,
    claims: tuple[StorageObjectClaim, ...],
) -> tuple[StoredObject, ...]:
    object_keys = tuple(claim.object_key for claim in claims)
    idempotency_keys = tuple(claim.idempotency_key for claim in claims)
    rows = tuple(
        session.scalars(
            select(StoredObject)
            .where(
                StoredObject.organization_id == organization_id,
                or_(
                    StoredObject.object_key.in_(object_keys),
                    StoredObject.idempotency_key.in_(idempotency_keys),
                ),
            )
            .order_by(StoredObject.object_key)
            .with_for_update()
        )
    )
    for claim in claims:
        matches = tuple(
            row
            for row in rows
            if row.object_key == claim.object_key or row.idempotency_key == claim.idempotency_key
        )
        if len(matches) != 1 or not _identity_matches(matches[0], claim):
            raise StorageClaimConflict(
                "stored object key or idempotency key has a different immutable identity"
            )
    return rows


def _reserve_counters(
    session: Session,
    organization_id: str,
    *,
    byte_delta: int,
    count_delta: int,
    now: datetime,
) -> None:
    if count_delta == 0:
        return
    global_row = session.execute(
        update(StorageGlobalQuota)
        .where(
            StorageGlobalQuota.id == 1,
            StorageGlobalQuota.reserved_bytes
            <= StorageGlobalQuota.byte_limit - StorageGlobalQuota.committed_bytes - byte_delta,
            StorageGlobalQuota.reserved_count
            <= StorageGlobalQuota.object_limit - StorageGlobalQuota.committed_count - count_delta,
        )
        .values(
            reserved_bytes=StorageGlobalQuota.reserved_bytes + byte_delta,
            reserved_count=StorageGlobalQuota.reserved_count + count_delta,
            updated_at=now,
        )
        .returning(StorageGlobalQuota.id)
    ).scalar_one_or_none()
    if global_row is None:
        raise StorageQuotaExceeded("global storage quota is missing or exceeded")

    tenant_row = session.execute(
        update(StorageTenantQuota)
        .where(
            StorageTenantQuota.organization_id == organization_id,
            StorageTenantQuota.reserved_bytes
            <= StorageTenantQuota.byte_limit - StorageTenantQuota.committed_bytes - byte_delta,
            StorageTenantQuota.reserved_count
            <= StorageTenantQuota.object_limit - StorageTenantQuota.committed_count - count_delta,
        )
        .values(
            reserved_bytes=StorageTenantQuota.reserved_bytes + byte_delta,
            reserved_count=StorageTenantQuota.reserved_count + count_delta,
            updated_at=now,
        )
        .returning(StorageTenantQuota.organization_id)
    ).scalar_one_or_none()
    if tenant_row is None:
        raise StorageQuotaExceeded("tenant storage quota is missing or exceeded")


def reserve_storage_batch_in_transaction(
    session: Session,
    organization_id: str,
    claims: Iterable[StorageObjectClaim],
    *,
    lease_token: str,
    lease_expires_at: datetime | None = None,
    lease_duration: timedelta | None = None,
    now: datetime | None = None,
    capacity_settings: StorageCapacitySettings | None = None,
) -> StorageReservation:
    """Atomically reserve one whole batch inside the caller's transaction."""

    canonical_organization_id = _canonical_text("organization_id", organization_id, maximum=36)
    canonical_claims = _normalized_claims(claims)
    current_time = _transaction_time(session, now)
    canonical_token, canonical_expiry = _validated_lease(
        lease_token,
        lease_expires_at,
        lease_duration,
        now=current_time,
    )
    set_tenant_context(session, canonical_organization_id)
    if session.get_bind().dialect.name == "postgresql":
        _require_postgresql_capacity_binding(session, capacity_settings)
        try:
            raw_result = session.scalar(
                text(
                    "SELECT public.custombuild_storage_reserve_batch("
                    ":organization_id, CAST(:claims AS jsonb), :lease_token, "
                    ":lease_duration_seconds)"
                ),
                {
                    "organization_id": canonical_organization_id,
                    "claims": _claims_json(canonical_claims),
                    "lease_token": canonical_token,
                    "lease_duration_seconds": _postgresql_lease_seconds(
                        current_time, canonical_expiry
                    ),
                },
            )
        except DBAPIError as exc:
            _raise_postgresql_quota_error(exc)
        return _postgresql_reservation_result(raw_result, canonical_claims)
    global_quota = session.scalar(
        select(StorageGlobalQuota).where(StorageGlobalQuota.id == 1).with_for_update()
    )
    if global_quota is None:
        raise StorageQuotaInvariantError("global storage quota is missing")
    if global_quota.maintenance_token is not None:
        raise StorageQuotaInvariantError(
            "storage maintenance is active; new reservations are fenced"
        )
    retired_identity = session.scalar(
        select(StorageObjectTombstone.object_key)
        .where(
            StorageObjectTombstone.capacity_bucket == global_quota.capacity_bucket,
            or_(
                StorageObjectTombstone.object_key.in_(
                    tuple(claim.object_key for claim in canonical_claims)
                ),
                StorageObjectTombstone.idempotency_key.in_(
                    tuple(claim.idempotency_key for claim in canonical_claims)
                ),
            ),
        )
        .limit(1)
    )
    if retired_identity is not None:
        raise StorageClaimConflict(
            "stored object key or idempotency key is permanently retired"
        )
    _insert_tenant_quota_if_missing(session, canonical_organization_id, now=current_time)

    inserted_keys = {
        claim.object_key
        for claim in canonical_claims
        if _insert_stored_object(
            session,
            canonical_organization_id,
            claim,
            lease_token=canonical_token,
            lease_expires_at=canonical_expiry,
            now=current_time,
        )
    }
    rows = _locked_claim_rows(session, canonical_organization_id, canonical_claims)
    current_time = _transaction_time(session, now)
    canonical_token, canonical_expiry = _validated_lease(
        lease_token,
        lease_expires_at,
        lease_duration,
        now=current_time,
    )
    rows_by_key = {row.object_key: row for row in rows}
    for claim in canonical_claims:
        row = rows_by_key[claim.object_key]
        if claim.object_key in inserted_keys:
            # The insert may itself have waited behind a conflicting transaction.
            # Rebind its lease to fresh database time after every identity row is
            # locked so a new reservation never starts with an already-stale TTL.
            row.lease_expires_at = canonical_expiry
            row.updated_at = current_time
            continue
        if row.state == StoredObjectState.committed:
            continue
        if row.state == StoredObjectState.reaping:
            stored_claim_expiry = row.claim_expires_at
            stored_claim_token = row.claim_token
            try:
                parsed_claim_token = (
                    uuid.UUID(stored_claim_token) if stored_claim_token is not None else None
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise StorageClaimConflict(
                    "stored object has an invalid storage reaper claim"
                ) from exc
            if (
                stored_claim_expiry is None
                or stored_claim_token is None
                or parsed_claim_token is None
                or str(parsed_claim_token) != stored_claim_token
                or parsed_claim_token.version not in {4, 5}
            ):
                raise StorageClaimConflict(
                    "stored object has an invalid storage reaper claim"
                )
            if stored_claim_expiry is not None and stored_claim_expiry.tzinfo is None:
                stored_claim_expiry = stored_claim_expiry.replace(tzinfo=UTC)
            elif stored_claim_expiry is not None:
                stored_claim_expiry = stored_claim_expiry.astimezone(UTC)
            retry_after = (
                math.ceil((stored_claim_expiry - current_time).total_seconds()) + 5
                if stored_claim_expiry > current_time
                else 5
            )
            raise StorageReservationBusy(
                "stored object is fenced by a storage reaper claim",
                retry_after_seconds=_validated_generation_retry_after(retry_after),
            )
        if row.state != StoredObjectState.reserved or row.lease_expires_at is None:
            raise StorageClaimConflict("stored object is being deleted or reaped")
        stored_expiry = row.lease_expires_at
        if stored_expiry.tzinfo is None:
            stored_expiry = stored_expiry.replace(tzinfo=UTC)
        else:
            stored_expiry = stored_expiry.astimezone(UTC)
        if stored_expiry <= current_time:
            row.lease_token = canonical_token
            row.lease_expires_at = canonical_expiry
            row.updated_at = current_time
        elif row.lease_token != canonical_token:
            retry_after = max(
                1,
                math.ceil((stored_expiry - current_time).total_seconds()) + 5,
            )
            raise StorageReservationBusy(
                "stored object has an active different lease",
                retry_after_seconds=retry_after,
            )
        elif canonical_expiry > stored_expiry:
            row.lease_expires_at = canonical_expiry
            row.updated_at = current_time

    byte_delta = sum(
        claim.size_bytes for claim in canonical_claims if claim.object_key in inserted_keys
    )
    _reserve_counters(
        session,
        canonical_organization_id,
        byte_delta=byte_delta,
        count_delta=len(inserted_keys),
        now=current_time,
    )
    session.flush()
    return StorageReservation(
        objects=tuple(
            StorageObjectReservation(
                object_key=claim.object_key,
                state=rows_by_key[claim.object_key].state,
                lease_token=rows_by_key[claim.object_key].lease_token,
                newly_reserved=claim.object_key in inserted_keys,
            )
            for claim in canonical_claims
        ),
        newly_reserved_bytes=byte_delta,
        newly_reserved_count=len(inserted_keys),
    )


def reserve_storage_batch(
    session_factory: sessionmaker[Session],
    organization_id: str,
    claims: Iterable[StorageObjectClaim],
    *,
    lease_token: str,
    lease_expires_at: datetime | None = None,
    lease_duration: timedelta | None = None,
    now: datetime | None = None,
    capacity_settings: StorageCapacitySettings | None = None,
) -> StorageReservation:
    """Commit a durable reservation before any object-store PUT starts."""

    with session_factory.begin() as session:
        return reserve_storage_batch_in_transaction(
            session,
            organization_id,
            claims,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            lease_duration=lease_duration,
            now=now,
            capacity_settings=capacity_settings,
        )


def renew_storage_batch_lease_in_transaction(
    session: Session,
    organization_id: str,
    claims: Iterable[StorageObjectClaim],
    *,
    lease_token: str,
    lease_expires_at: datetime | None = None,
    lease_duration: timedelta | None = None,
    now: datetime | None = None,
) -> None:
    """Extend an unexpired lease while its exact ownership is still locked."""

    canonical_organization_id = _canonical_text("organization_id", organization_id, maximum=36)
    canonical_claims = _normalized_claims(claims)
    current_time = _transaction_time(session, now)
    canonical_token, canonical_expiry = _validated_lease(
        lease_token,
        lease_expires_at,
        lease_duration,
        now=current_time,
    )
    set_tenant_context(session, canonical_organization_id)
    if session.get_bind().dialect.name == "postgresql":
        try:
            session.execute(
                text(
                    "SELECT public.custombuild_storage_renew_batch("
                    ":organization_id, CAST(:claims AS jsonb), :lease_token, "
                    ":lease_duration_seconds)"
                ),
                {
                    "organization_id": canonical_organization_id,
                    "claims": _claims_json(canonical_claims),
                    "lease_token": canonical_token,
                    "lease_duration_seconds": _postgresql_lease_seconds(
                        current_time, canonical_expiry
                    ),
                },
            )
        except DBAPIError as exc:
            _raise_postgresql_quota_error(exc)
        return
    rows = _locked_claim_rows(session, canonical_organization_id, canonical_claims)
    current_time = _transaction_time(session, now)
    canonical_token, canonical_expiry = _validated_lease(
        lease_token,
        lease_expires_at,
        lease_duration,
        now=current_time,
    )
    for row in rows:
        stored_expiry = row.lease_expires_at
        if stored_expiry is not None and stored_expiry.tzinfo is None:
            stored_expiry = stored_expiry.replace(tzinfo=UTC)
        elif stored_expiry is not None:
            stored_expiry = stored_expiry.astimezone(UTC)
        if (
            row.state != StoredObjectState.reserved
            or row.lease_token != canonical_token
            or stored_expiry is None
            or stored_expiry <= current_time
        ):
            raise StorageClaimConflict("storage reservation lease ownership was lost")
        if canonical_expiry > stored_expiry:
            row.lease_expires_at = canonical_expiry
            row.updated_at = current_time
    session.flush()


def renew_storage_batch_lease(
    session_factory: sessionmaker[Session],
    organization_id: str,
    claims: Iterable[StorageObjectClaim],
    *,
    lease_token: str,
    lease_expires_at: datetime | None = None,
    lease_duration: timedelta | None = None,
    now: datetime | None = None,
) -> None:
    """Heartbeat a batch in an independent durable transaction."""

    with session_factory.begin() as session:
        renew_storage_batch_lease_in_transaction(
            session,
            organization_id,
            claims,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            lease_duration=lease_duration,
            now=now,
        )


def _commit_counters(
    session: Session,
    organization_id: str,
    *,
    byte_delta: int,
    count_delta: int,
    now: datetime,
) -> None:
    if count_delta == 0:
        return
    global_row = session.execute(
        update(StorageGlobalQuota)
        .where(
            StorageGlobalQuota.id == 1,
            StorageGlobalQuota.reserved_bytes >= byte_delta,
            StorageGlobalQuota.reserved_count >= count_delta,
        )
        .values(
            reserved_bytes=StorageGlobalQuota.reserved_bytes - byte_delta,
            committed_bytes=StorageGlobalQuota.committed_bytes + byte_delta,
            reserved_count=StorageGlobalQuota.reserved_count - count_delta,
            committed_count=StorageGlobalQuota.committed_count + count_delta,
            updated_at=now,
        )
        .returning(StorageGlobalQuota.id)
    ).scalar_one_or_none()
    if global_row is None:
        raise StorageQuotaInvariantError("global reserved counters cannot be committed")

    tenant_row = session.execute(
        update(StorageTenantQuota)
        .where(
            StorageTenantQuota.organization_id == organization_id,
            StorageTenantQuota.reserved_bytes >= byte_delta,
            StorageTenantQuota.reserved_count >= count_delta,
        )
        .values(
            reserved_bytes=StorageTenantQuota.reserved_bytes - byte_delta,
            committed_bytes=StorageTenantQuota.committed_bytes + byte_delta,
            reserved_count=StorageTenantQuota.reserved_count - count_delta,
            committed_count=StorageTenantQuota.committed_count + count_delta,
            updated_at=now,
        )
        .returning(StorageTenantQuota.organization_id)
    ).scalar_one_or_none()
    if tenant_row is None:
        raise StorageQuotaInvariantError("tenant reserved counters cannot be committed")


def commit_storage_batch_in_transaction(
    session: Session,
    organization_id: str,
    claims: Iterable[StorageObjectClaim],
    *,
    lease_token: str,
    now: datetime | None = None,
) -> None:
    """Move an exactly verified reserved batch to committed counters."""

    canonical_organization_id = _canonical_text("organization_id", organization_id, maximum=36)
    canonical_claims = _normalized_claims(claims)
    try:
        canonical_token = str(uuid.UUID(lease_token))
    except (AttributeError, TypeError, ValueError) as exc:
        raise StorageClaimConflict("lease_token must be a canonical UUID") from exc
    if canonical_token != lease_token:
        raise StorageClaimConflict("lease_token must be a canonical UUID")
    set_tenant_context(session, canonical_organization_id)
    if session.get_bind().dialect.name == "postgresql":
        try:
            session.execute(
                text(
                    "SELECT public.custombuild_storage_commit_batch("
                    ":organization_id, CAST(:claims AS jsonb), :lease_token)"
                ),
                {
                    "organization_id": canonical_organization_id,
                    "claims": _claims_json(canonical_claims),
                    "lease_token": canonical_token,
                },
            )
        except DBAPIError as exc:
            _raise_postgresql_quota_error(exc)
        return
    rows = _locked_claim_rows(session, canonical_organization_id, canonical_claims)
    current_time = _transaction_time(session, now)
    rows_by_key = {row.object_key: row for row in rows}
    reserved_rows: list[StoredObject] = []
    for claim in canonical_claims:
        row = rows_by_key[claim.object_key]
        if row.state == StoredObjectState.committed:
            continue
        stored_expiry = row.lease_expires_at
        if stored_expiry is not None and stored_expiry.tzinfo is None:
            stored_expiry = stored_expiry.replace(tzinfo=UTC)
        elif stored_expiry is not None:
            stored_expiry = stored_expiry.astimezone(UTC)
        if (
            row.state != StoredObjectState.reserved
            or row.lease_token != canonical_token
            or stored_expiry is None
            or stored_expiry <= current_time
        ):
            raise StorageClaimConflict("reserved object is owned by a different lease")
        reserved_rows.append(row)

    byte_delta = sum(row.size_bytes for row in reserved_rows)
    _commit_counters(
        session,
        canonical_organization_id,
        byte_delta=byte_delta,
        count_delta=len(reserved_rows),
        now=current_time,
    )
    for row in reserved_rows:
        row.state = StoredObjectState.committed
        row.lease_token = None
        row.lease_expires_at = None
        row.updated_at = current_time
    session.flush()


def commit_storage_batch(
    session_factory: sessionmaker[Session],
    organization_id: str,
    claims: Iterable[StorageObjectClaim],
    *,
    lease_token: str,
    now: datetime | None = None,
) -> None:
    """Commit a verified batch in an independent durable transaction."""

    with session_factory.begin() as session:
        commit_storage_batch_in_transaction(
            session,
            organization_id,
            claims,
            lease_token=lease_token,
            now=now,
        )


def require_committed_storage_binding(
    session: Session,
    organization_id: str,
    *,
    project_id: str,
    object_key: str,
    sha256: str,
    size_bytes: int,
    media_type: str,
    owner_type: str,
    owner_id: str,
) -> StoredObject:
    """Fail closed unless a domain row has one exact committed ledger binding."""

    claim = _validated_claim(
        StorageObjectClaim(
            project_id=project_id,
            object_key=object_key,
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
            owner_type=owner_type,
            owner_id=owner_id,
            idempotency_key=object_key,
        )
    )
    canonical_organization_id = _canonical_text("organization_id", organization_id, maximum=36)
    set_tenant_context(session, canonical_organization_id)
    row = session.scalar(
        select(StoredObject).where(
            StoredObject.organization_id == canonical_organization_id,
            StoredObject.object_key == claim.object_key,
        )
    )
    if (
        row is None
        or row.state != StoredObjectState.committed
        or row.project_id != claim.project_id
        or row.sha256 != claim.sha256
        or row.size_bytes != claim.size_bytes
        or row.media_type != claim.media_type
        or row.owner_type != claim.owner_type
        or row.owner_id != claim.owner_id
    ):
        raise StorageQuotaInvariantError(
            "domain object is not bound to one exact committed storage identity"
        )
    return row
