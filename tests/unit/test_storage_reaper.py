from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import app.storage_reaper as storage_reaper_module
import pytest
from app.db import Base
from app.models import (
    Artifact,
    DesignVersion,
    ExternalEvidence,
    GenerationJob,
    ImportedAsset,
    JobStatus,
    Organization,
    Project,
    StorageGlobalQuota,
    StorageObjectTombstone,
    StorageTenantQuota,
    StoredObject,
    StoredObjectState,
    User,
)
from app.storage_quota import (
    StorageClaimConflict,
    StorageObjectClaim,
    StorageQuotaInvariantError,
    StorageReservationBusy,
    prepare_generation_storage_retry,
    reserve_storage_batch,
)
from app.storage_reaper import (
    ReapCounterKind,
    StorageReaperInvariantError,
    StorageReapStatus,
    _claim_query,
    claim_storage_reap_batch,
    reap_storage_batch,
    reap_storage_claim,
)
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

ORGANIZATION_A = "00000000-0000-0000-0000-000000000001"
ORGANIZATION_B = "00000000-0000-0000-0000-000000000002"
PROJECT_A = "00000000-0000-0000-0000-000000000003"
PROJECT_B = "00000000-0000-0000-0000-000000000004"
USER_ID = "00000000-0000-0000-0000-000000000005"
VERSION_A = "00000000-0000-0000-0000-000000000006"
JOB_A = "00000000-0000-0000-0000-000000000007"
LEASE_ID = "00000000-0000-4000-8000-000000000008"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


class ProviderResponseError(Exception):
    def __init__(self, status: int | str | None) -> None:
        super().__init__("provider response")
        self.response: object
        if status is None:
            self.response = {}
        else:
            self.response = {
                "ResponseMetadata": {"HTTPStatusCode": status},
            }


class RecordingS3:
    def __init__(
        self,
        *,
        delete_status: int | None = 204,
        head_status: int | str | None = 404,
        delete_raises: bool = False,
        head_raises: bool = True,
        pre_head_size: int = 100,
        pre_head_sha256: str | None = None,
    ) -> None:
        self.delete_status = delete_status
        self.head_status = head_status
        self.delete_raises = delete_raises
        self.head_raises = head_raises
        self.pre_head_size = pre_head_size
        self.pre_head_sha256 = pre_head_sha256
        self.deletes: list[tuple[str, str]] = []
        self.heads: list[tuple[str, str]] = []

    @staticmethod
    def _response(status: int | str | None) -> Mapping[str, object]:
        if status is None:
            return {}
        return {"ResponseMetadata": {"HTTPStatusCode": status}}

    def delete_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]:
        self.deletes.append((Bucket, Key))
        if self.delete_raises:
            raise ProviderResponseError(503)
        return self._response(self.delete_status)

    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]:
        self.heads.append((Bucket, Key))
        if (Bucket, Key) not in self.deletes:
            suffix = Key.rsplit("/", 1)[-1].removesuffix(".bin")
            return {
                "ResponseMetadata": {"HTTPStatusCode": 200},
                "ContentLength": self.pre_head_size,
                "Metadata": {
                    "sha256": self.pre_head_sha256 or uuid.uuid5(uuid.NAMESPACE_URL, suffix).hex * 2
                },
            }
        if self.head_raises:
            raise ProviderResponseError(self.head_status)
        return self._response(self.head_status)


@pytest.fixture
def reaper_database() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as session:
        session.add_all(
            [
                Organization(id=ORGANIZATION_A, name="Tenant A", slug="reaper-a"),
                Organization(id=ORGANIZATION_B, name="Tenant B", slug="reaper-b"),
                User(
                    id=USER_ID,
                    oidc_sub="reaper-user",
                    email="reaper@example.test",
                    name="Reaper tester",
                ),
                Project(
                    id=PROJECT_A,
                    organization_id=ORGANIZATION_A,
                    name="Reaper A",
                ),
                Project(
                    id=PROJECT_B,
                    organization_id=ORGANIZATION_B,
                    name="Reaper B",
                ),
                StorageGlobalQuota(
                    id=1,
                    byte_limit=10_000,
                    object_limit=100,
                    reserved_bytes=0,
                    committed_bytes=0,
                    reserved_count=0,
                    committed_count=0,
                ),
                StorageTenantQuota(
                    organization_id=ORGANIZATION_A,
                    byte_limit=5_000,
                    object_limit=50,
                    reserved_bytes=0,
                    committed_bytes=0,
                    reserved_count=0,
                    committed_count=0,
                ),
                StorageTenantQuota(
                    organization_id=ORGANIZATION_B,
                    byte_limit=5_000,
                    object_limit=50,
                    reserved_bytes=0,
                    committed_bytes=0,
                    reserved_count=0,
                    committed_count=0,
                ),
            ]
        )
        session.flush()
        session.add(
            DesignVersion(
                id=VERSION_A,
                organization_id=ORGANIZATION_A,
                project_id=PROJECT_A,
                revision=1,
                design_hash="a" * 64,
                context_hash="b" * 64,
                spec_json={},
                result_json={},
                engine_version="test",
                template_version="test",
                created_by=USER_ID,
            )
        )
        session.flush()
        session.add(
            GenerationJob(
                id=JOB_A,
                organization_id=ORGANIZATION_A,
                design_version_id=VERSION_A,
                status=JobStatus.succeeded,
                idempotency_key="c" * 64,
                production_context_hash="d" * 64,
                production_engine_context_json={},
                request_json={},
                attempts=1,
            )
        )
    yield engine, factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def _counter_kind_for_state(state: StoredObjectState) -> ReapCounterKind:
    if state == StoredObjectState.reserved:
        return ReapCounterKind.reserved
    return ReapCounterKind.committed


def _seed_object(
    factory: sessionmaker[Session],
    suffix: str,
    *,
    organization_id: str = ORGANIZATION_A,
    project_id: str = PROJECT_A,
    state: StoredObjectState = StoredObjectState.reserved,
    size_bytes: int = 100,
    lease_expires_at: datetime | None = None,
    owner_type: str = "artifact",
    owner_id: str | None = None,
) -> str:
    object_key = f"reaper/{organization_id}/{suffix}.bin"
    counter_kind = _counter_kind_for_state(state)
    with factory.begin() as session:
        global_quota = session.get(StorageGlobalQuota, 1)
        tenant_quota = session.get(StorageTenantQuota, organization_id)
        assert global_quota is not None
        assert tenant_quota is not None
        if counter_kind == ReapCounterKind.reserved:
            global_quota.reserved_bytes += size_bytes
            global_quota.reserved_count += 1
            tenant_quota.reserved_bytes += size_bytes
            tenant_quota.reserved_count += 1
        else:
            global_quota.committed_bytes += size_bytes
            global_quota.committed_count += 1
            tenant_quota.committed_bytes += size_bytes
            tenant_quota.committed_count += 1
        session.add(
            StoredObject(
                organization_id=organization_id,
                project_id=project_id,
                object_key=object_key,
                sha256=uuid.uuid5(uuid.NAMESPACE_URL, suffix).hex * 2,
                size_bytes=size_bytes,
                media_type="application/octet-stream",
                owner_type=owner_type,
                owner_id=owner_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"owner:{suffix}")),
                idempotency_key=f"reaper:{suffix}",
                state=state,
                lease_token=(LEASE_ID if state == StoredObjectState.reserved else None),
                lease_expires_at=(
                    lease_expires_at or NOW - timedelta(seconds=1)
                    if state == StoredObjectState.reserved
                    else None
                ),
                claim_token=None,
                claim_expires_at=None,
            )
        )
    return object_key


def _stored_row(
    factory: sessionmaker[Session], organization_id: str, object_key: str
) -> StoredObject | None:
    with factory() as session:
        return session.scalar(
            select(StoredObject).where(
                StoredObject.organization_id == organization_id,
                StoredObject.object_key == object_key,
            )
        )


def _tombstone(
    factory: sessionmaker[Session], bucket: str, object_key: str
) -> StorageObjectTombstone | None:
    with factory() as session:
        return session.get(StorageObjectTombstone, (bucket, object_key))


def _quota_values(
    factory: sessionmaker[Session], organization_id: str = ORGANIZATION_A
) -> tuple[int, int, int, int, int, int, int, int]:
    with factory() as session:
        global_quota = session.get(StorageGlobalQuota, 1)
        tenant_quota = session.get(StorageTenantQuota, organization_id)
        assert global_quota is not None
        assert tenant_quota is not None
        return (
            global_quota.reserved_bytes,
            global_quota.reserved_count,
            global_quota.committed_bytes,
            global_quota.committed_count,
            tenant_quota.reserved_bytes,
            tenant_quota.reserved_count,
            tenant_quota.committed_bytes,
            tenant_quota.committed_count,
        )


def test_postgresql_claim_uses_database_clock_and_skip_locked() -> None:
    statement = _claim_query(
        ORGANIZATION_A,
        batch_size=10,
        current_time=func.clock_timestamp(),
    )
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "clock_timestamp()" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert f"stored_objects.organization_id = '{ORGANIZATION_A}'" in sql


def test_claims_only_expired_reservations_and_delete_pending_for_exact_tenant(
    reaper_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = reaper_database
    expired = _seed_object(factory, "expired")
    active = _seed_object(
        factory,
        "active",
        lease_expires_at=NOW + timedelta(seconds=1),
    )
    pending = _seed_object(
        factory,
        "pending",
        state=StoredObjectState.delete_pending,
    )
    orphaned_committed = _seed_object(
        factory,
        "orphaned-committed",
        state=StoredObjectState.committed,
    )
    other_tenant = _seed_object(
        factory,
        "other",
        organization_id=ORGANIZATION_B,
        project_id=PROJECT_B,
    )

    claims = claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW)

    assert {claim.object_key for claim in claims} == {
        expired,
        pending,
        orphaned_committed,
    }
    assert {claim.counter_kind for claim in claims} == {
        ReapCounterKind.reserved,
        ReapCounterKind.committed,
    }
    for claim in claims:
        assert str(uuid.UUID(claim.claim_token)) == claim.claim_token
        assert claim.claim_expires_at == NOW + timedelta(minutes=5)
        row = _stored_row(factory, ORGANIZATION_A, claim.object_key)
        assert row is not None
        assert row.state == StoredObjectState.reaping
        assert row.lease_token is None
        assert row.claim_token == claim.claim_token
    assert _stored_row(factory, ORGANIZATION_A, active).state == StoredObjectState.reserved  # type: ignore[union-attr]
    assert _stored_row(factory, ORGANIZATION_B, other_tenant).state == StoredObjectState.reserved  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("job_status", "is_claimed"),
    [
        (JobStatus.queued, False),
        (JobStatus.running, False),
        (JobStatus.failed, True),
        (JobStatus.succeeded, True),
    ],
)
def test_generation_retry_reservation_is_not_reaped_before_job_is_terminal(
    reaper_database: tuple[Engine, sessionmaker[Session]],
    job_status: JobStatus,
    is_claimed: bool,
) -> None:
    _, factory = reaper_database
    with factory.begin() as session:
        job = session.get(GenerationJob, JOB_A)
        assert job is not None
        job.status = job_status
    object_key = _seed_object(
        factory,
        f"generation-{job_status.value}",
        owner_type="generation_job",
        owner_id=JOB_A,
    )

    claims = claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW)

    assert (object_key in {claim.object_key for claim in claims}) is is_claimed


def test_terminal_job_with_live_generation_lease_is_never_reclaimed(
    reaper_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = reaper_database
    with factory.begin() as session:
        job = session.get(GenerationJob, JOB_A)
        assert job is not None
        job.status = JobStatus.failed
        job.lease_expires_at = NOW + timedelta(minutes=1)
    object_key = _seed_object(
        factory,
        "generation-live-lease",
        owner_type="generation_job",
        owner_id=JOB_A,
    )

    assert claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW) == ()
    claims = claim_storage_reap_batch(
        factory,
        ORGANIZATION_A,
        now=NOW + timedelta(minutes=1),
    )

    assert {claim.object_key for claim in claims} == {object_key}


def test_manual_retry_never_reactivates_expired_reaper_key(
    reaper_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = reaper_database
    with factory.begin() as session:
        job = session.get(GenerationJob, JOB_A)
        assert job is not None
        job.status = JobStatus.failed
    object_key = _seed_object(
        factory,
        "manual-generation-retry",
        owner_type="generation_job",
        owner_id=JOB_A,
    )
    reaper_claim = claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW)[0]
    with factory.begin() as session:
        job = session.get(GenerationJob, JOB_A)
        row = session.get(StoredObject, (ORGANIZATION_A, object_key))
        assert job is not None and row is not None
        job.status = JobStatus.queued
        storage_claim = StorageObjectClaim(
            project_id=row.project_id,
            object_key=row.object_key,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            media_type=row.media_type,
            owner_type=row.owner_type,
            owner_id=row.owner_id,
            idempotency_key=row.idempotency_key,
        )
    replacement_token = str(uuid.uuid4())

    with pytest.raises(StorageReservationBusy) as busy:
        reserve_storage_batch(
            factory,
            ORGANIZATION_A,
            (storage_claim,),
            lease_token=replacement_token,
            lease_duration=timedelta(minutes=2),
            now=NOW,
        )
    assert busy.value.retry_after_seconds == 305

    with pytest.raises(StorageReservationBusy) as expired_busy:
        reserve_storage_batch(
            factory,
            ORGANIZATION_A,
            (storage_claim,),
            lease_token=replacement_token,
            lease_duration=timedelta(minutes=2),
            now=reaper_claim.claim_expires_at,
        )

    assert expired_busy.value.retry_after_seconds == 5
    row = _stored_row(factory, ORGANIZATION_A, object_key)
    assert row is not None
    assert row.state == StoredObjectState.reaping
    assert row.lease_token is None
    assert row.claim_token == reaper_claim.claim_token
    assert _quota_values(factory) == (100, 1, 0, 0, 100, 1, 0, 0)


def test_generation_retry_preflight_uses_exact_active_claim_delay_then_allows_expiry(
    reaper_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = reaper_database
    with factory.begin() as session:
        job = session.get(GenerationJob, JOB_A)
        assert job is not None
        job.status = JobStatus.failed
    object_key = _seed_object(
        factory,
        "generation-retry-preflight",
        owner_type="generation_job",
        owner_id=JOB_A,
    )
    claim = claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW)[0]

    with factory.begin() as session:
        assert (
            prepare_generation_storage_retry(
                session,
                ORGANIZATION_A,
                JOB_A,
                now=NOW,
            )
            == 305
        )
    with factory.begin() as session:
        assert (
            prepare_generation_storage_retry(
                session,
                ORGANIZATION_A,
                JOB_A,
                now=claim.claim_expires_at,
            )
            == 5
        )
    row = _stored_row(factory, ORGANIZATION_A, object_key)
    assert row is not None
    assert row.state == StoredObjectState.reaping
    assert row.claim_token == claim.claim_token


def test_generation_retry_preflight_fails_closed_for_nonterminal_job(
    reaper_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = reaper_database
    with factory.begin() as session:
        job = session.get(GenerationJob, JOB_A)
        assert job is not None
        job.status = JobStatus.queued

    with (
        factory.begin() as session,
        pytest.raises(StorageQuotaInvariantError, match="exact terminal tenant job"),
    ):
        prepare_generation_storage_retry(
            session,
            ORGANIZATION_A,
            JOB_A,
            now=NOW,
        )


def test_generation_retry_preflight_allows_delete_pending_storage_without_mutating_it(
    reaper_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = reaper_database
    object_key = _seed_object(
        factory,
        "generation-delete-pending",
        state=StoredObjectState.delete_pending,
        owner_type="generation_job",
        owner_id=JOB_A,
    )

    with factory.begin() as session:
        assert (
            prepare_generation_storage_retry(
                session,
                ORGANIZATION_A,
                JOB_A,
                now=NOW,
            )
            == 0
        )

    row = _stored_row(factory, ORGANIZATION_A, object_key)
    assert row is not None
    assert row.state == StoredObjectState.delete_pending


@pytest.mark.parametrize(
    ("initial_state", "expected_counters"),
    [
        (StoredObjectState.reserved, (0, 0, 0, 0, 0, 0, 0, 0)),
        (StoredObjectState.committed, (0, 0, 0, 0, 0, 0, 0, 0)),
        (StoredObjectState.delete_pending, (0, 0, 0, 0, 0, 0, 0, 0)),
    ],
)
def test_exact_head_404_atomically_releases_correct_counter_and_ledger(
    reaper_database: tuple[Engine, sessionmaker[Session]],
    initial_state: StoredObjectState,
    expected_counters: tuple[int, int, int, int, int, int, int, int],
) -> None:
    _, factory = reaper_database
    object_key = _seed_object(factory, f"delete-{initial_state.value}", state=initial_state)
    s3 = RecordingS3()

    claim = claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW)[0]
    result = reap_storage_claim(factory, s3, "production-bucket", claim, now=NOW)

    assert result.status == StorageReapStatus.deleted
    assert result.retryable is False
    assert s3.deletes == [("production-bucket", object_key)]
    assert s3.heads == [
        ("production-bucket", object_key),
        ("production-bucket", object_key),
    ]
    assert _stored_row(factory, ORGANIZATION_A, object_key) is None
    tombstone = _tombstone(factory, "production-bucket", object_key)
    assert tombstone is not None
    assert tombstone.organization_id == ORGANIZATION_A
    assert tombstone.sha256 == claim.sha256
    assert tombstone.size_bytes == claim.size_bytes
    assert tombstone.accounting_state == claim.counter_kind.value
    assert tombstone.claim_token == claim.claim_token
    assert _quota_values(factory) == expected_counters


@pytest.mark.parametrize("collision", ["object_key", "idempotency_key"])
def test_reaper_tombstone_permanently_blocks_aba_reservation(
    reaper_database: tuple[Engine, sessionmaker[Session]],
    collision: str,
) -> None:
    _, factory = reaper_database
    object_key = _seed_object(factory, f"aba-{collision}")
    with factory.begin() as session:
        global_quota = session.get(StorageGlobalQuota, 1)
        row = session.get(StoredObject, (ORGANIZATION_A, object_key))
        assert global_quota is not None and row is not None
        global_quota.capacity_bucket = "production-bucket"
        original = StorageObjectClaim(
            project_id=row.project_id,
            object_key=row.object_key,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            media_type=row.media_type,
            owner_type=row.owner_type,
            owner_id=row.owner_id,
            idempotency_key=row.idempotency_key,
        )

    reap_claim = claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW)[0]
    result = reap_storage_claim(
        factory,
        RecordingS3(),
        "production-bucket",
        reap_claim,
        now=NOW,
    )
    assert result.status == StorageReapStatus.deleted

    replacement = replace(
        original,
        object_key=(
            original.object_key
            if collision == "object_key"
            else f"reaper/{ORGANIZATION_A}/aba-fresh.bin"
        ),
        idempotency_key=(
            original.idempotency_key
            if collision == "idempotency_key"
            else "reaper:aba-fresh"
        ),
    )
    with pytest.raises(StorageClaimConflict, match="permanently retired"):
        reserve_storage_batch(
            factory,
            ORGANIZATION_A,
            (replacement,),
            lease_token=str(uuid.uuid4()),
            lease_duration=timedelta(minutes=2),
            now=NOW,
        )

    assert _stored_row(factory, ORGANIZATION_A, replacement.object_key) is None
    assert _quota_values(factory) == (0, 0, 0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    ("s3", "expected_status"),
    [
        (RecordingS3(delete_raises=True), StorageReapStatus.provider_error),
        (
            RecordingS3(delete_status=None),
            StorageReapStatus.provider_error,
        ),
        (
            RecordingS3(head_status=200, head_raises=False),
            StorageReapStatus.object_still_present,
        ),
        (
            RecordingS3(head_status="404", head_raises=True),
            StorageReapStatus.provider_error,
        ),
        (
            RecordingS3(head_status=None, head_raises=True),
            StorageReapStatus.provider_error,
        ),
    ],
)
def test_provider_ambiguity_retains_reaping_row_and_full_debit(
    reaper_database: tuple[Engine, sessionmaker[Session]],
    s3: RecordingS3,
    expected_status: StorageReapStatus,
) -> None:
    _, factory = reaper_database
    object_key = _seed_object(factory, f"ambiguous-{expected_status.value}")
    claim = claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW)[0]

    result = reap_storage_claim(factory, s3, "production-bucket", claim, now=NOW)

    assert result.status == expected_status
    assert result.retryable is True
    row = _stored_row(factory, ORGANIZATION_A, object_key)
    assert row is not None
    assert row.state == StoredObjectState.reaping
    assert row.claim_token == claim.claim_token
    assert _quota_values(factory) == (100, 1, 0, 0, 100, 1, 0, 0)


@pytest.mark.parametrize(
    "s3",
    [
        RecordingS3(pre_head_size=101),
        RecordingS3(pre_head_sha256="f" * 64),
    ],
)
def test_provider_identity_mismatch_never_deletes_or_debits(
    reaper_database: tuple[Engine, sessionmaker[Session]],
    s3: RecordingS3,
) -> None:
    _, factory = reaper_database
    object_key = _seed_object(factory, "identity-mismatch")
    claim = claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW)[0]

    result = reap_storage_claim(factory, s3, "production-bucket", claim, now=NOW)

    assert result.status == StorageReapStatus.identity_mismatch
    assert result.retryable is False
    assert s3.deletes == []
    assert s3.heads == [("production-bucket", object_key)]
    row = _stored_row(factory, ORGANIZATION_A, object_key)
    assert row is not None
    assert row.state == StoredObjectState.reaping
    assert _quota_values(factory) == (100, 1, 0, 0, 100, 1, 0, 0)


@pytest.mark.parametrize("reference_kind", ["artifact", "import", "evidence"])
def test_every_domain_reference_blocks_provider_delete_and_retains_debit(
    reaper_database: tuple[Engine, sessionmaker[Session]],
    reference_kind: str,
) -> None:
    _, factory = reaper_database
    object_key = _seed_object(
        factory,
        f"referenced-{reference_kind}",
        state=StoredObjectState.committed,
    )
    with factory.begin() as session:
        if reference_kind == "artifact":
            session.add(
                Artifact(
                    id=str(uuid.uuid4()),
                    organization_id=ORGANIZATION_A,
                    generation_job_id=JOB_A,
                    kind="bundle",
                    object_key=object_key,
                    sha256="e" * 64,
                    size_bytes=100,
                    content_type="application/octet-stream",
                )
            )
        elif reference_kind == "import":
            session.add(
                ImportedAsset(
                    id=str(uuid.uuid4()),
                    organization_id=ORGANIZATION_A,
                    project_id=PROJECT_A,
                    object_key=object_key,
                    sha256="e" * 64,
                    size_bytes=100,
                    media_type="application/octet-stream",
                    original_filename="input.bin",
                    created_by=USER_ID,
                )
            )
        else:
            session.add(
                ExternalEvidence(
                    id=str(uuid.uuid4()),
                    organization_id=ORGANIZATION_A,
                    project_id=PROJECT_A,
                    evidence_type="certificate",
                    rule_id="rule",
                    catalog_id="catalog",
                    catalog_version="1",
                    design_hash="a" * 64,
                    object_key=object_key,
                    sha256="e" * 64,
                    size_bytes=100,
                    content_type="application/octet-stream",
                    created_by=USER_ID,
                )
            )
    s3 = RecordingS3()

    claims = claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW)

    assert claims == ()
    assert s3.deletes == []
    assert s3.heads == []
    assert _stored_row(factory, ORGANIZATION_A, object_key) is not None
    assert _quota_values(factory) == (0, 0, 100, 1, 0, 0, 100, 1)


def test_wrong_or_expired_claim_never_calls_provider(
    reaper_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = reaper_database
    object_key = _seed_object(factory, "lost")
    claim = claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW)[0]
    replacement_token = str(uuid.UUID(int=uuid.uuid4().int & ~1, version=4))
    stale_copy = replace(claim, claim_token=replacement_token)
    s3 = RecordingS3()

    wrong_token_result = reap_storage_claim(
        factory,
        s3,
        "production-bucket",
        stale_copy,
        now=NOW,
    )
    expired_result = reap_storage_claim(
        factory,
        s3,
        "production-bucket",
        claim,
        now=claim.claim_expires_at,
    )

    assert wrong_token_result.status == StorageReapStatus.ownership_lost
    assert expired_result.status == StorageReapStatus.ownership_lost
    assert s3.deletes == []
    assert _stored_row(factory, ORGANIZATION_A, object_key) is not None
    assert _quota_values(factory) == (100, 1, 0, 0, 100, 1, 0, 0)


def test_claim_expiring_during_provider_io_retains_ledger_and_full_debit(
    reaper_database: tuple[Engine, sessionmaker[Session]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = reaper_database
    object_key = _seed_object(factory, "expires-during-provider")
    claim = claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW)[0]
    observed_times = iter((NOW, claim.claim_expires_at))
    monkeypatch.setattr(
        storage_reaper_module,
        "_database_time",
        lambda _session, _override: next(observed_times),
    )
    s3 = RecordingS3()

    result = reap_storage_claim(factory, s3, "production-bucket", claim, now=NOW)

    assert result.status == StorageReapStatus.ownership_lost
    assert s3.deletes == [("production-bucket", object_key)]
    assert s3.heads == [
        ("production-bucket", object_key),
        ("production-bucket", object_key),
    ]
    row = _stored_row(factory, ORGANIZATION_A, object_key)
    assert row is not None
    assert row.state == StoredObjectState.reaping
    assert _quota_values(factory) == (100, 1, 0, 0, 100, 1, 0, 0)


def test_expired_reaping_claim_can_be_taken_over_without_losing_counter_origin(
    reaper_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = reaper_database
    _seed_object(factory, "reserved-retry")
    _seed_object(
        factory,
        "committed-retry",
        state=StoredObjectState.delete_pending,
    )
    original = claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW)

    replacement = claim_storage_reap_batch(
        factory,
        ORGANIZATION_A,
        now=NOW + timedelta(minutes=5, microseconds=1),
    )

    assert len(original) == len(replacement) == 2
    assert {item.counter_kind for item in replacement} == {
        ReapCounterKind.reserved,
        ReapCounterKind.committed,
    }
    assert {item.claim_token for item in original}.isdisjoint(
        {item.claim_token for item in replacement}
    )


def test_counter_underflow_rolls_back_all_database_finalization(
    reaper_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = reaper_database
    object_key = _seed_object(factory, "underflow")
    claim = claim_storage_reap_batch(factory, ORGANIZATION_A, now=NOW)[0]
    with factory.begin() as session:
        tenant_quota = session.get(StorageTenantQuota, ORGANIZATION_A)
        assert tenant_quota is not None
        tenant_quota.reserved_bytes = 0
        tenant_quota.reserved_count = 0
    s3 = RecordingS3()

    with pytest.raises(StorageReaperInvariantError, match="tenant reserved counters"):
        reap_storage_claim(factory, s3, "production-bucket", claim, now=NOW)

    row = _stored_row(factory, ORGANIZATION_A, object_key)
    assert row is not None
    assert row.state == StoredObjectState.reaping
    assert _quota_values(factory) == (100, 1, 0, 0, 0, 0, 0, 0)


def test_bounded_batch_orchestrator_claims_and_deletes_only_requested_count(
    reaper_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = reaper_database
    keys = {_seed_object(factory, f"batch-{index}") for index in range(3)}
    s3 = RecordingS3()

    results = reap_storage_batch(
        factory,
        s3,
        "production-bucket",
        ORGANIZATION_A,
        batch_size=2,
        now=NOW,
    )

    assert len(results) == 2
    assert {result.status for result in results} == {StorageReapStatus.deleted}
    deleted_keys = {key for _, key in s3.deletes}
    assert deleted_keys < keys
    assert _quota_values(factory) == (100, 1, 0, 0, 100, 1, 0, 0)


@pytest.mark.parametrize("batch_size", [0, 101, True])
def test_invalid_batch_size_fails_before_opening_claims(
    reaper_database: tuple[Engine, sessionmaker[Session]],
    batch_size: Any,
) -> None:
    _, factory = reaper_database
    _seed_object(factory, "invalid-batch")

    with pytest.raises(StorageReaperInvariantError, match="batch_size"):
        claim_storage_reap_batch(
            factory,
            ORGANIZATION_A,
            batch_size=batch_size,
            now=NOW,
        )

    assert _quota_values(factory) == (100, 1, 0, 0, 100, 1, 0, 0)
