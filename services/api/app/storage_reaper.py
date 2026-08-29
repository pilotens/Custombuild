"""Fail-closed reclamation of abandoned object-storage ledger entries.

The reaper uses two short database phases around provider I/O:

* a claimant takes an expired reservation (or a deletion request) with
  ``FOR UPDATE SKIP LOCKED`` and replaces it with a short, unguessable claim;
* the owner proves the exact claim has no domain reference, binds a provider
  ``HEAD`` to the ledger SHA-256 and size, deletes the object, and requires an
  exact ``HEAD`` 404 before token-bound finalization releases the debit and
  permanently tombstones the physical bucket/key.

Deferred database constraint triggers prevent any new domain reference from
committing after the row enters ``reaping``, closing the provider-I/O race
without holding quota locks across a network call.

The database row remains charged in every ambiguous case.  A claim expiry may
only transfer deletion ownership to another reaper; it can never reactivate a
key for upload.  In particular, a
provider timeout, a non-canonical response, a still-visible object, a domain
reference, or lost claim ownership can never free quota.
"""

from __future__ import annotations

import enum
import hmac
import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import and_, delete, exists, func, or_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from .db import set_tenant_context
from .models import (
    Artifact,
    ExternalEvidence,
    GenerationJob,
    ImportedAsset,
    JobStatus,
    StorageGlobalQuota,
    StorageObjectTombstone,
    StorageTenantQuota,
    StoredObject,
    StoredObjectState,
)

DEFAULT_REAP_CLAIM_DURATION = timedelta(minutes=5)
MAX_REAP_CLAIM_DURATION = timedelta(minutes=30)
MAX_REAP_BATCH_SIZE = 100
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class StorageReaperError(RuntimeError):
    """Base class for durable storage-reaper failures."""


class StorageReaperInvariantError(StorageReaperError):
    """The ledger or its counters cannot be changed without losing accounting."""


class ReapCounterKind(str, enum.Enum):
    """Counter family originally charged by the ledger row."""

    reserved = "reserved"
    committed = "committed"


class StorageReapStatus(str, enum.Enum):
    """A bounded outcome suitable for metrics and Celery retry policy."""

    deleted = "deleted"
    provider_error = "provider_error"
    object_still_present = "object_still_present"
    identity_mismatch = "identity_mismatch"
    domain_reference = "domain_reference"
    ownership_lost = "ownership_lost"


@dataclass(frozen=True)
class StorageReapClaim:
    organization_id: str
    object_key: str
    sha256: str
    size_bytes: int
    counter_kind: ReapCounterKind
    claim_token: str
    claim_expires_at: datetime


@dataclass(frozen=True)
class StorageReapResult:
    claim: StorageReapClaim
    status: StorageReapStatus
    retryable: bool


class StorageReaperS3Client(Protocol):
    """The synchronous subset shared by boto3 S3 and the production adapter."""

    def delete_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]: ...

    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]: ...


def _canonical_text(name: str, value: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise StorageReaperInvariantError(f"{name} is not a canonical non-empty string")
    if value != value.strip() or any(
        ord(character) <= 0x20 or ord(character) == 0x7F for character in value
    ):
        raise StorageReaperInvariantError(f"{name} contains whitespace or control characters")
    if "\\" in value:
        raise StorageReaperInvariantError(f"{name} contains a non-canonical path separator")
    return value


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _database_time(session: Session, override: datetime | None) -> datetime:
    """Use wall-clock database time in PostgreSQL, including after lock acquisition."""

    if session.get_bind().dialect.name == "postgresql":
        value = session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):
            raise StorageReaperInvariantError(
                "database did not return a canonical wall-clock timestamp"
            )
    else:
        value = override or datetime.now(UTC)
    return _aware_utc(value)


def _claim_time_expression(
    session: Session, current_time: datetime
) -> ColumnElement[datetime] | datetime:
    if session.get_bind().dialect.name == "postgresql":
        return func.clock_timestamp()
    return current_time


def _validated_claim_duration(duration: timedelta) -> timedelta:
    if not isinstance(duration, timedelta):
        raise StorageReaperInvariantError("claim duration must be a timedelta")
    if duration <= timedelta(0) or duration > MAX_REAP_CLAIM_DURATION:
        raise StorageReaperInvariantError("claim duration is outside the safe window")
    return duration


def _counter_kind_from_token(token: str) -> ReapCounterKind:
    """Recover accounting origin when an expired reaping claim is taken over.

    The low UUID bit is an authenticated-by-the-ledger type marker.  The other
    121 random UUIDv4 bits keep the token unguessable.  This lets a crashed
    reaper be safely resumed without adding another lifecycle column.
    """

    try:
        parsed = uuid.UUID(token)
    except (AttributeError, TypeError, ValueError) as exc:
        raise StorageReaperInvariantError("stored reaper claim token is not a UUID") from exc
    if str(parsed) != token:
        raise StorageReaperInvariantError("stored reaper claim token is not canonical")
    if parsed.version == 5:
        return ReapCounterKind.committed
    if parsed.version == 4:
        return ReapCounterKind.reserved
    raise StorageReaperInvariantError("stored reaper claim token has no counter marker")


def _new_claim_token(counter_kind: ReapCounterKind) -> str:
    random_uuid = uuid.uuid4()
    version = 5 if counter_kind == ReapCounterKind.committed else 4
    marked = uuid.UUID(int=random_uuid.int, version=version)
    return str(marked)


def _claim_query(
    organization_id: str,
    *,
    batch_size: int,
    current_time: ColumnElement[datetime] | datetime,
) -> Select[tuple[StoredObject]]:
    return (
        select(StoredObject)
        .where(
            StoredObject.organization_id == organization_id,
            or_(
                and_(
                    StoredObject.state == StoredObjectState.reserved,
                    StoredObject.lease_expires_at.is_not(None),
                    StoredObject.lease_expires_at <= current_time,
                ),
                StoredObject.state == StoredObjectState.committed,
                StoredObject.state == StoredObjectState.delete_pending,
                and_(
                    StoredObject.state == StoredObjectState.reaping,
                    StoredObject.claim_expires_at.is_not(None),
                    StoredObject.claim_expires_at <= current_time,
                ),
            ),
            ~exists(
                select(Artifact.id).where(
                    Artifact.organization_id == organization_id,
                    Artifact.object_key == StoredObject.object_key,
                )
            ),
            ~exists(
                select(ImportedAsset.id).where(
                    ImportedAsset.organization_id == organization_id,
                    ImportedAsset.object_key == StoredObject.object_key,
                )
            ),
            ~exists(
                select(ExternalEvidence.id).where(
                    ExternalEvidence.organization_id == organization_id,
                    ExternalEvidence.object_key == StoredObject.object_key,
                )
            ),
            or_(
                StoredObject.owner_type != "generation_job",
                ~exists(
                    select(GenerationJob.id).where(
                        GenerationJob.organization_id == organization_id,
                        GenerationJob.id == StoredObject.owner_id,
                        or_(
                            GenerationJob.status.in_((JobStatus.queued, JobStatus.running)),
                            GenerationJob.lease_expires_at > current_time,
                        ),
                    )
                ),
            ),
        )
        .order_by(StoredObject.organization_id, StoredObject.object_key)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


def _postgresql_reaper_message(exc: DBAPIError) -> str:
    original = getattr(exc, "orig", None)
    return str(original if original is not None else exc)


def _postgresql_reaper_error(exc: DBAPIError) -> StorageReaperInvariantError:
    message = _postgresql_reaper_message(exc)
    if "STORAGE_BUCKET_MISMATCH:" in message:
        return StorageReaperInvariantError(
            "storage provider bucket does not match the attested ledger bucket"
        )
    if "STORAGE_REAP_BLOCKED:" in message:
        return StorageReaperInvariantError("storage object still has a domain reference")
    if "STORAGE_CLAIM_CONFLICT:" in message:
        return StorageReaperInvariantError("storage reaper claim ownership was lost")
    return StorageReaperInvariantError("storage reaper database mutation failed")


def _claim_expiry(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StorageReaperInvariantError(
                "storage reaper function returned an invalid expiry"
            ) from exc
    else:
        raise StorageReaperInvariantError("storage reaper function returned a non-canonical expiry")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StorageReaperInvariantError("storage reaper function returned a naive expiry")
    return parsed.astimezone(UTC)


def _postgresql_reap_claims(
    raw_result: object,
    organization_id: str,
) -> tuple[StorageReapClaim, ...]:
    if isinstance(raw_result, str):
        try:
            raw_result = json.loads(raw_result)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise StorageReaperInvariantError(
                "storage reaper function returned malformed JSON"
            ) from exc
    if not isinstance(raw_result, list):
        raise StorageReaperInvariantError("storage reaper function returned a non-canonical batch")
    expected_fields = {
        "organization_id",
        "project_id",
        "object_key",
        "sha256",
        "size_bytes",
        "media_type",
        "owner_type",
        "owner_id",
        "claim_token",
        "claim_expires_at",
        "accounting_state",
    }
    claims: list[StorageReapClaim] = []
    object_keys: set[str] = set()
    for item in raw_result:
        if not isinstance(item, Mapping) or frozenset(item) != expected_fields:
            raise StorageReaperInvariantError("storage reaper function returned an invalid claim")
        object_key = item["object_key"]
        sha256 = item["sha256"]
        size_bytes = item["size_bytes"]
        claim_token = item["claim_token"]
        accounting_state = item["accounting_state"]
        if (
            item["organization_id"] != organization_id
            or not isinstance(object_key, str)
            or object_key in object_keys
            or not isinstance(sha256, str)
            or _SHA256_PATTERN.fullmatch(sha256) is None
            or type(size_bytes) is not int
            or size_bytes < 1
            or not isinstance(claim_token, str)
            or accounting_state
            not in {
                ReapCounterKind.reserved.value,
                ReapCounterKind.committed.value,
            }
        ):
            raise StorageReaperInvariantError(
                "storage reaper function returned inconsistent identity"
            )
        _canonical_text("object_key", object_key, maximum=512)
        counter_kind = ReapCounterKind(accounting_state)
        if _counter_kind_from_token(claim_token) != counter_kind:
            raise StorageReaperInvariantError(
                "storage reaper function returned inconsistent counter provenance"
            )
        object_keys.add(object_key)
        claims.append(
            StorageReapClaim(
                organization_id=organization_id,
                object_key=object_key,
                sha256=sha256,
                size_bytes=size_bytes,
                counter_kind=counter_kind,
                claim_token=claim_token,
                claim_expires_at=_claim_expiry(item["claim_expires_at"]),
            )
        )
    return tuple(claims)


def _claim_postgresql_reap_kind(
    session: Session,
    organization_id: str,
    *,
    function_name: str,
    batch_size: int,
    claim_duration_seconds: int,
) -> tuple[StorageReapClaim, ...]:
    if function_name not in {
        "custombuild_storage_claim_expired_reservations",
        "custombuild_storage_claim_delete_pending",
    }:
        raise StorageReaperInvariantError("unknown storage reaper entry point")
    if batch_size <= 0:
        return ()
    try:
        raw_result = session.scalar(
            text(
                f"SELECT public.{function_name}("  # noqa: S608 - closed allow-list above
                ":organization_id, :claim_token, :duration_seconds, :batch_size)"
            ),
            {
                "organization_id": organization_id,
                "claim_token": str(uuid.uuid4()),
                "duration_seconds": claim_duration_seconds,
                "batch_size": batch_size,
            },
        )
    except DBAPIError as exc:
        raise _postgresql_reaper_error(exc) from exc
    return _postgresql_reap_claims(raw_result, organization_id)


def claim_storage_reap_batch(
    session_factory: sessionmaker[Session],
    organization_id: str,
    *,
    batch_size: int = 25,
    claim_duration: timedelta = DEFAULT_REAP_CLAIM_DURATION,
    now: datetime | None = None,
) -> tuple[StorageReapClaim, ...]:
    """Claim eligible rows for one exact tenant in a durable transaction."""

    canonical_organization_id = _canonical_text("organization_id", organization_id, maximum=36)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise StorageReaperInvariantError("batch_size must be an integer")
    if batch_size <= 0 or batch_size > MAX_REAP_BATCH_SIZE:
        raise StorageReaperInvariantError("batch_size is outside the safe range")
    canonical_duration = _validated_claim_duration(claim_duration)

    with session_factory.begin() as session:
        set_tenant_context(session, canonical_organization_id)
        if session.get_bind().dialect.name == "postgresql":
            duration_seconds = math.ceil(canonical_duration.total_seconds())
            reserved_target = max(1, (batch_size + 1) // 2)
            pending_target = batch_size - reserved_target
            postgres_claims = list(
                _claim_postgresql_reap_kind(
                    session,
                    canonical_organization_id,
                    function_name="custombuild_storage_claim_expired_reservations",
                    batch_size=reserved_target,
                    claim_duration_seconds=duration_seconds,
                )
            )
            if pending_target:
                postgres_claims.extend(
                    _claim_postgresql_reap_kind(
                        session,
                        canonical_organization_id,
                        function_name="custombuild_storage_claim_delete_pending",
                        batch_size=pending_target,
                        claim_duration_seconds=duration_seconds,
                    )
                )
            remaining = batch_size - len(postgres_claims)
            if remaining:
                postgres_claims.extend(
                    _claim_postgresql_reap_kind(
                        session,
                        canonical_organization_id,
                        function_name="custombuild_storage_claim_expired_reservations",
                        batch_size=remaining,
                        claim_duration_seconds=duration_seconds,
                    )
                )
            remaining = batch_size - len(postgres_claims)
            if remaining:
                postgres_claims.extend(
                    _claim_postgresql_reap_kind(
                        session,
                        canonical_organization_id,
                        function_name="custombuild_storage_claim_delete_pending",
                        batch_size=remaining,
                        claim_duration_seconds=duration_seconds,
                    )
                )
            if len(postgres_claims) > batch_size or len(
                {claim.object_key for claim in postgres_claims}
            ) != len(postgres_claims):
                raise StorageReaperInvariantError(
                    "storage reaper functions returned an invalid combined batch"
                )
            return tuple(postgres_claims)
        initial_time = _database_time(session, now)
        rows = tuple(
            session.scalars(
                _claim_query(
                    canonical_organization_id,
                    batch_size=batch_size,
                    current_time=_claim_time_expression(session, initial_time),
                )
            )
        )
        claim_time = _database_time(session, now)
        claim_expires_at = claim_time + canonical_duration
        claims: list[StorageReapClaim] = []
        for row in rows:
            if row.state == StoredObjectState.reserved:
                counter_kind = ReapCounterKind.reserved
            elif row.state in {
                StoredObjectState.committed,
                StoredObjectState.delete_pending,
            }:
                counter_kind = ReapCounterKind.committed
            elif row.state == StoredObjectState.reaping and row.claim_token is not None:
                counter_kind = _counter_kind_from_token(row.claim_token)
            else:
                raise StorageReaperInvariantError(
                    "claimed row has an impossible storage lifecycle state"
                )
            token = _new_claim_token(counter_kind)
            row.state = StoredObjectState.reaping
            row.lease_token = None
            row.lease_expires_at = None
            row.claim_token = token
            row.claim_expires_at = claim_expires_at
            row.updated_at = claim_time
            claims.append(
                StorageReapClaim(
                    organization_id=canonical_organization_id,
                    object_key=row.object_key,
                    sha256=row.sha256,
                    size_bytes=row.size_bytes,
                    counter_kind=counter_kind,
                    claim_token=token,
                    claim_expires_at=claim_expires_at,
                )
            )
        session.flush()
        return tuple(claims)


def _has_domain_reference(
    session: Session,
    claim: StorageReapClaim,
    *,
    current_time: datetime,
) -> bool:
    predicates = (
        (Artifact, Artifact.object_key),
        (ImportedAsset, ImportedAsset.object_key),
        (ExternalEvidence, ExternalEvidence.object_key),
    )
    for model, object_key_column in predicates:
        reference = session.scalar(
            select(model.id)
            .where(
                model.organization_id == claim.organization_id,
                object_key_column == claim.object_key,
            )
            .limit(1)
        )
        if reference is not None:
            return True
    active_generation = session.scalar(
        select(GenerationJob.id)
        .join(
            StoredObject,
            and_(
                StoredObject.organization_id == GenerationJob.organization_id,
                StoredObject.owner_id == GenerationJob.id,
            ),
        )
        .where(
            GenerationJob.organization_id == claim.organization_id,
            StoredObject.object_key == claim.object_key,
            StoredObject.owner_type == "generation_job",
            or_(
                GenerationJob.status.in_((JobStatus.queued, JobStatus.running)),
                GenerationJob.lease_expires_at > current_time,
            ),
        )
        .limit(1)
    )
    return active_generation is not None


def _http_status(response: object) -> int | None:
    if not isinstance(response, Mapping):
        return None
    metadata = response.get("ResponseMetadata")
    if not isinstance(metadata, Mapping):
        return None
    status = metadata.get("HTTPStatusCode")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return status


def _exception_http_status(exc: Exception) -> int | None:
    return _http_status(getattr(exc, "response", None))


def _delete_and_confirm_missing(
    s3_client: StorageReaperS3Client,
    bucket: str,
    claim: StorageReapClaim,
) -> StorageReapStatus | None:
    # Bind provider identity before the destructive call. S3 user metadata is
    # the immutable upload digest contract; an absent or different digest/size
    # is never safe to delete under this ledger claim.
    try:
        before_response = s3_client.head_object(Bucket=bucket, Key=claim.object_key)
    except Exception as exc:
        before_status = _exception_http_status(exc)
        if before_status == 404:
            return None
        return StorageReapStatus.provider_error
    before_status = _http_status(before_response)
    if before_status is None or not 200 <= before_status < 300:
        return StorageReapStatus.provider_error
    provider_size = before_response.get("ContentLength")
    provider_metadata = before_response.get("Metadata")
    provider_sha256 = (
        provider_metadata.get("sha256") if isinstance(provider_metadata, Mapping) else None
    )
    if (
        isinstance(provider_size, bool)
        or not isinstance(provider_size, int)
        or provider_size != claim.size_bytes
        or not isinstance(provider_sha256, str)
        or not hmac.compare_digest(provider_sha256, claim.sha256)
    ):
        return StorageReapStatus.identity_mismatch

    try:
        delete_response = s3_client.delete_object(Bucket=bucket, Key=claim.object_key)
    except Exception as exc:  # provider SDK exceptions are intentionally normalized
        _exception_http_status(exc)
        return StorageReapStatus.provider_error
    delete_status = _http_status(delete_response)
    if delete_status is None or not 200 <= delete_status < 300:
        return StorageReapStatus.provider_error

    try:
        head_response = s3_client.head_object(Bucket=bucket, Key=claim.object_key)
    except Exception as exc:  # boto3 reports a missing HEAD as ClientError(404)
        head_status = _exception_http_status(exc)
    else:
        head_status = _http_status(head_response)
    if head_status == 404:
        return None
    if head_status is not None and 200 <= head_status < 300:
        return StorageReapStatus.object_still_present
    return StorageReapStatus.provider_error


def _decrement_quota_counters(
    session: Session,
    claim: StorageReapClaim,
    *,
    now: datetime,
) -> None:
    if claim.counter_kind == ReapCounterKind.reserved:
        global_byte_counter = StorageGlobalQuota.reserved_bytes
        global_count_counter = StorageGlobalQuota.reserved_count
        tenant_byte_counter = StorageTenantQuota.reserved_bytes
        tenant_count_counter = StorageTenantQuota.reserved_count
    else:
        global_byte_counter = StorageGlobalQuota.committed_bytes
        global_count_counter = StorageGlobalQuota.committed_count
        tenant_byte_counter = StorageTenantQuota.committed_bytes
        tenant_count_counter = StorageTenantQuota.committed_count

    global_values = {
        global_byte_counter.key: global_byte_counter - claim.size_bytes,
        global_count_counter.key: global_count_counter - 1,
        "updated_at": now,
    }
    global_updated = session.execute(
        update(StorageGlobalQuota)
        .where(
            StorageGlobalQuota.id == 1,
            global_byte_counter >= claim.size_bytes,
            global_count_counter >= 1,
        )
        .values(**global_values)
        .returning(StorageGlobalQuota.id)
    ).scalar_one_or_none()
    if global_updated is None:
        raise StorageReaperInvariantError(
            f"global {claim.counter_kind.value} counters would underflow"
        )

    tenant_values = {
        tenant_byte_counter.key: tenant_byte_counter - claim.size_bytes,
        tenant_count_counter.key: tenant_count_counter - 1,
        "updated_at": now,
    }
    tenant_updated = session.execute(
        update(StorageTenantQuota)
        .where(
            StorageTenantQuota.organization_id == claim.organization_id,
            tenant_byte_counter >= claim.size_bytes,
            tenant_count_counter >= 1,
        )
        .values(**tenant_values)
        .returning(StorageTenantQuota.organization_id)
    ).scalar_one_or_none()
    if tenant_updated is None:
        raise StorageReaperInvariantError(
            f"tenant {claim.counter_kind.value} counters would underflow"
        )


def _claim_identity_is_canonical(claim: StorageReapClaim) -> bool:
    if not isinstance(claim, StorageReapClaim):
        return False
    try:
        canonical_token = str(uuid.UUID(claim.claim_token))
    except (AttributeError, TypeError, ValueError):
        return False
    return (
        canonical_token == claim.claim_token
        and _counter_kind_from_token(claim.claim_token) == claim.counter_kind
        and isinstance(claim.sha256, str)
        and _SHA256_PATTERN.fullmatch(claim.sha256) is not None
        and claim.size_bytes > 0
    )


def _postgresql_reap_preflight(
    session_factory: sessionmaker[Session],
    claim: StorageReapClaim,
    bucket: str,
) -> StorageReapStatus | None:
    with session_factory.begin() as session:
        set_tenant_context(session, claim.organization_id)
        try:
            bucket_matches = session.scalar(
                text("SELECT public.custombuild_storage_assert_reap_bucket(:capacity_bucket)"),
                {"capacity_bucket": bucket},
            )
        except DBAPIError as exc:
            raise _postgresql_reaper_error(exc) from exc
        if bucket_matches is not True:
            raise StorageReaperInvariantError(
                "storage bucket assertion returned a non-canonical result"
            )
        row = session.scalar(
            select(StoredObject).where(
                StoredObject.organization_id == claim.organization_id,
                StoredObject.object_key == claim.object_key,
            )
        )
        current_time = _database_time(session, None)
        stored_claim_expiry = (
            _aware_utc(row.claim_expires_at)
            if row is not None and row.claim_expires_at is not None
            else None
        )
        if (
            row is None
            or row.state != StoredObjectState.reaping
            or row.claim_token != claim.claim_token
            or row.sha256 != claim.sha256
            or row.size_bytes != claim.size_bytes
            or stored_claim_expiry is None
            or stored_claim_expiry <= current_time
        ):
            return StorageReapStatus.ownership_lost
        if _has_domain_reference(session, claim, current_time=current_time):
            return StorageReapStatus.domain_reference
    return None


def _finalize_postgresql_reap(
    session_factory: sessionmaker[Session],
    claim: StorageReapClaim,
    bucket: str,
) -> StorageReapStatus:
    try:
        with session_factory.begin() as session:
            set_tenant_context(session, claim.organization_id)
            finalized = session.scalar(
                text(
                    "SELECT public.custombuild_storage_finalize_reap("
                    ":organization_id, :object_key, :sha256, :size_bytes, :claim_token, "
                    ":capacity_bucket)"
                ),
                {
                    "organization_id": claim.organization_id,
                    "object_key": claim.object_key,
                    "sha256": claim.sha256,
                    "size_bytes": claim.size_bytes,
                    "claim_token": claim.claim_token,
                    "capacity_bucket": bucket,
                },
            )
            if finalized is not True:
                raise StorageReaperInvariantError(
                    "storage reaper finalize function returned a non-canonical result"
                )
    except DBAPIError as exc:
        message = _postgresql_reaper_message(exc)
        if "STORAGE_REAP_BLOCKED:" in message:
            return StorageReapStatus.domain_reference
        if "STORAGE_CLAIM_CONFLICT:" in message:
            return StorageReapStatus.ownership_lost
        raise _postgresql_reaper_error(exc) from exc
    return StorageReapStatus.deleted


def reap_storage_claim(
    session_factory: sessionmaker[Session],
    s3_client: StorageReaperS3Client,
    bucket: str,
    claim: StorageReapClaim,
    *,
    now: datetime | None = None,
) -> StorageReapResult:
    """Delete and finalize one exactly owned claim, retaining debit on ambiguity."""

    canonical_bucket = _canonical_text("bucket", bucket, maximum=63)
    if not _claim_identity_is_canonical(claim):
        raise StorageReaperInvariantError("reaper claim identity is not canonical")
    _canonical_text("organization_id", claim.organization_id, maximum=36)
    _canonical_text("object_key", claim.object_key, maximum=512)

    with session_factory() as dialect_session:
        is_postgresql = dialect_session.get_bind().dialect.name == "postgresql"
    if is_postgresql:
        preflight_status = _postgresql_reap_preflight(
            session_factory,
            claim,
            canonical_bucket,
        )
        if preflight_status is not None:
            return StorageReapResult(
                claim=claim,
                status=preflight_status,
                retryable=False,
            )
        provider_status = _delete_and_confirm_missing(
            s3_client,
            canonical_bucket,
            claim,
        )
        if provider_status is not None:
            return StorageReapResult(
                claim=claim,
                status=provider_status,
                retryable=(provider_status != StorageReapStatus.identity_mismatch),
            )
        final_status = _finalize_postgresql_reap(
            session_factory,
            claim,
            canonical_bucket,
        )
        return StorageReapResult(
            claim=claim,
            status=final_status,
            retryable=False,
        )

    # SQLite is used only for local parity tests, but it keeps the production
    # two-phase contract: prove ownership in a short transaction, close it,
    # perform provider I/O, then re-prove ownership in a new finalizer
    # transaction.  No database transaction is held across the network call.
    with session_factory.begin() as session:
        set_tenant_context(session, claim.organization_id)
        row = session.scalar(
            select(StoredObject)
            .where(
                StoredObject.organization_id == claim.organization_id,
                StoredObject.object_key == claim.object_key,
            )
            .with_for_update()
        )
        current_time = _database_time(session, now)
        stored_claim_expiry = (
            _aware_utc(row.claim_expires_at)
            if row is not None and row.claim_expires_at is not None
            else None
        )
        if (
            row is None
            or row.state != StoredObjectState.reaping
            or row.claim_token != claim.claim_token
            or row.sha256 != claim.sha256
            or row.size_bytes != claim.size_bytes
            or stored_claim_expiry is None
            or stored_claim_expiry <= current_time
        ):
            return StorageReapResult(
                claim=claim,
                status=StorageReapStatus.ownership_lost,
                retryable=False,
            )
        if _has_domain_reference(session, claim, current_time=current_time):
            return StorageReapResult(
                claim=claim,
                status=StorageReapStatus.domain_reference,
                retryable=False,
            )

    provider_status = _delete_and_confirm_missing(
        s3_client,
        canonical_bucket,
        claim,
    )
    if provider_status is not None:
        return StorageReapResult(
            claim=claim,
            status=provider_status,
            retryable=(provider_status != StorageReapStatus.identity_mismatch),
        )

    with session_factory.begin() as session:
        set_tenant_context(session, claim.organization_id)
        row = session.scalar(
            select(StoredObject)
            .where(
                StoredObject.organization_id == claim.organization_id,
                StoredObject.object_key == claim.object_key,
            )
            .with_for_update()
        )
        # Provider I/O may have consumed most of the claim window.  Re-read the
        # exact row and database clock after the confirmed delete. Retain the
        # full debit if identity/ownership changed or expired while in flight.
        current_time = _database_time(session, now)
        stored_claim_expiry = (
            _aware_utc(row.claim_expires_at)
            if row is not None and row.claim_expires_at is not None
            else None
        )
        if (
            row is None
            or row.state != StoredObjectState.reaping
            or row.claim_token != claim.claim_token
            or row.sha256 != claim.sha256
            or row.size_bytes != claim.size_bytes
            or stored_claim_expiry is None
            or stored_claim_expiry <= current_time
        ):
            return StorageReapResult(
                claim=claim,
                status=StorageReapStatus.ownership_lost,
                retryable=False,
            )
        if _has_domain_reference(session, claim, current_time=current_time):
            return StorageReapResult(
                claim=claim,
                status=StorageReapStatus.domain_reference,
                retryable=False,
            )

        retired_identity = session.scalar(
            select(StorageObjectTombstone).where(
                StorageObjectTombstone.capacity_bucket == canonical_bucket,
                or_(
                    StorageObjectTombstone.object_key == row.object_key,
                    StorageObjectTombstone.idempotency_key == row.idempotency_key,
                ),
            )
        )
        if retired_identity is not None:
            raise StorageReaperInvariantError(
                "storage key or idempotency identity was already retired"
            )
        session.add(
            StorageObjectTombstone(
                capacity_bucket=canonical_bucket,
                object_key=row.object_key,
                organization_id=row.organization_id,
                project_id=row.project_id,
                sha256=row.sha256,
                size_bytes=row.size_bytes,
                media_type=row.media_type,
                owner_type=row.owner_type,
                owner_id=row.owner_id,
                idempotency_key=row.idempotency_key,
                accounting_state=claim.counter_kind.value,
                claim_token=claim.claim_token,
                retired_at=current_time,
            )
        )
        _decrement_quota_counters(session, claim, now=current_time)
        deleted_key = session.execute(
            delete(StoredObject)
            .where(
                StoredObject.organization_id == claim.organization_id,
                StoredObject.object_key == claim.object_key,
                StoredObject.state == StoredObjectState.reaping,
                StoredObject.claim_token == claim.claim_token,
            )
            .returning(StoredObject.object_key)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        if deleted_key != claim.object_key:
            raise StorageReaperInvariantError("exact reaper claim disappeared during finalization")
        session.flush()
        return StorageReapResult(
            claim=claim,
            status=StorageReapStatus.deleted,
            retryable=False,
        )


def reap_storage_batch(
    session_factory: sessionmaker[Session],
    s3_client: StorageReaperS3Client,
    bucket: str,
    organization_id: str,
    *,
    batch_size: int = 25,
    claim_duration: timedelta = DEFAULT_REAP_CLAIM_DURATION,
    now: datetime | None = None,
) -> tuple[StorageReapResult, ...]:
    """Claim and process one bounded tenant batch; directly usable by Celery."""

    claims = claim_storage_reap_batch(
        session_factory,
        organization_id,
        batch_size=batch_size,
        claim_duration=claim_duration,
        now=now,
    )
    return tuple(
        reap_storage_claim(
            session_factory,
            s3_client,
            bucket,
            claim,
            now=now,
        )
        for claim in claims
    )
