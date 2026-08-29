from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from app.db import Base
from app.models import (
    DesignVersion,
    GenerationJob,
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
    GLOBAL_STORAGE_BYTE_LIMIT,
    GLOBAL_STORAGE_OBJECT_LIMIT,
    TENANT_STORAGE_BYTE_LIMIT,
    TENANT_STORAGE_OBJECT_LIMIT,
    StorageClaimConflict,
    StorageObjectClaim,
    StorageQuotaExceeded,
    StorageQuotaInvariantError,
    StorageReservation,
    StorageReservationBusy,
    commit_storage_batch,
    generation_retry_after_from_database_error,
    prepare_generation_storage_retry,
    renew_storage_batch_lease,
    reserve_storage_batch,
)
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

ORGANIZATION_ID = "00000000-0000-0000-0000-000000000001"
PROJECT_ID = "00000000-0000-0000-0000-000000000002"
LEASE_A = "00000000-0000-0000-0000-000000000003"
LEASE_B = "00000000-0000-0000-0000-000000000004"
USER_ID = "00000000-0000-0000-0000-000000000005"
VERSION_ID = "00000000-0000-0000-0000-000000000006"
GENERATION_JOB_ID = "00000000-0000-0000-0000-000000000007"
NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
MAINTENANCE_GATE_ID = "00000000-0000-0000-0000-000000000099"
CAPACITY_BUCKET = "custombuild-artifacts"


@pytest.fixture
def quota_database() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
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
        session.add(
            Organization(
                id=ORGANIZATION_ID,
                name="Quota tenant",
                slug=f"quota-{uuid.uuid4().hex}",
            )
        )
        session.add(
            Project(
                id=PROJECT_ID,
                organization_id=ORGANIZATION_ID,
                name="Quota project",
            )
        )
        session.add(
            User(
                id=USER_ID,
                oidc_sub="quota-user",
                email="quota@example.test",
                name="Quota tester",
            )
        )
        session.add(
            StorageGlobalQuota(
                id=1,
                byte_limit=GLOBAL_STORAGE_BYTE_LIMIT,
                object_limit=GLOBAL_STORAGE_OBJECT_LIMIT,
                capacity_bucket=CAPACITY_BUCKET,
            )
        )
        session.flush()
        session.add(
            DesignVersion(
                id=VERSION_ID,
                organization_id=ORGANIZATION_ID,
                project_id=PROJECT_ID,
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
    yield engine, factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def _claim(
    suffix: str = "a",
    *,
    size_bytes: int = 100,
    object_key: str | None = None,
    idempotency_key: str | None = None,
    sha256: str | None = None,
    owner_type: str = "artifact",
    owner_id: str | None = None,
) -> StorageObjectClaim:
    return StorageObjectClaim(
        project_id=PROJECT_ID,
        object_key=object_key or f"objects/{suffix}.bin",
        sha256=sha256 or hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
        size_bytes=size_bytes,
        media_type="application/octet-stream",
        owner_type=owner_type,
        owner_id=owner_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"owner:{suffix}")),
        idempotency_key=idempotency_key or f"artifact:{suffix}",
    )


def _generation_claim(suffix: str = "bundle") -> StorageObjectClaim:
    return _claim(
        suffix,
        owner_type="generation_job",
        owner_id=GENERATION_JOB_ID,
        idempotency_key=f"generation:{GENERATION_JOB_ID}:{suffix}",
    )


def _seed_generation_job(
    factory: sessionmaker[Session],
    *,
    status: JobStatus,
) -> None:
    with factory.begin() as session:
        session.add(
            GenerationJob(
                id=GENERATION_JOB_ID,
                organization_id=ORGANIZATION_ID,
                design_version_id=VERSION_ID,
                status=status,
                idempotency_key="c" * 64,
                production_context_hash="d" * 64,
                production_engine_context_json={},
                request_json={},
                attempts=1,
            )
        )


def _reserve(
    factory: sessionmaker[Session],
    claims: tuple[StorageObjectClaim, ...],
    *,
    token: str = LEASE_A,
    now: datetime = NOW,
    lifetime: timedelta = timedelta(hours=2),
) -> StorageReservation:
    return reserve_storage_batch(
        factory,
        ORGANIZATION_ID,
        claims,
        lease_token=token,
        lease_expires_at=now + lifetime,
        now=now,
    )


def test_generation_retry_trigger_delay_parser_requires_a_bounded_positive_integer() -> None:
    exact = DBAPIError(
        "UPDATE generation_jobs",
        {},
        RuntimeError("STORAGE_GENERATION_RETRY_BUSY:17"),
        False,
    )
    unrelated = DBAPIError(
        "UPDATE generation_jobs",
        {},
        RuntimeError("STORAGE_GENERATION_LIVENESS_INVALID"),
        False,
    )
    with_context = DBAPIError(
        "UPDATE generation_jobs",
        {},
        RuntimeError("ERROR: STORAGE_GENERATION_RETRY_BUSY:19\nCONTEXT: trigger"),
        False,
    )

    assert generation_retry_after_from_database_error(exact) == 17
    assert generation_retry_after_from_database_error(with_context) == 19
    assert generation_retry_after_from_database_error(unrelated) is None
    for malformed_marker in (
        "STORAGE_GENERATION_RETRY_BUSY:17evil",
        "xSTORAGE_GENERATION_RETRY_BUSY:17",
        "STORAGE_GENERATION_RETRY_BUSY:+17",
    ):
        malformed = DBAPIError(
            "UPDATE generation_jobs",
            {},
            RuntimeError(malformed_marker),
            False,
        )
        assert generation_retry_after_from_database_error(malformed) is None
    for invalid_value in ("0", "017", "3606"):
        invalid = DBAPIError(
            "UPDATE generation_jobs",
            {},
            RuntimeError(f"STORAGE_GENERATION_RETRY_BUSY:{invalid_value}"),
            False,
        )
        with pytest.raises(StorageQuotaInvariantError, match="non-canonical|non-positive"):
            generation_retry_after_from_database_error(invalid)


def test_reservation_charges_whole_batch_once_and_same_lease_is_idempotent(
    quota_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = quota_database
    claims = (_claim("a", size_bytes=40), _claim("b", size_bytes=60))

    first = _reserve(factory, claims)
    retry = _reserve(
        factory,
        claims,
        now=NOW + timedelta(minutes=10),
        lifetime=timedelta(hours=2, minutes=10),
    )

    assert first.newly_reserved_bytes == 100
    assert first.newly_reserved_count == 2
    assert retry.newly_reserved_bytes == 0
    assert retry.newly_reserved_count == 0
    with factory() as session:
        global_quota = session.get(StorageGlobalQuota, 1)
        tenant_quota = session.get(StorageTenantQuota, ORGANIZATION_ID)
        assert global_quota is not None
        assert tenant_quota is not None
        assert (global_quota.reserved_bytes, global_quota.reserved_count) == (100, 2)
        assert (tenant_quota.reserved_bytes, tenant_quota.reserved_count) == (100, 2)
        rows = tuple(session.scalars(select(StoredObject).order_by(StoredObject.object_key)))
        assert len(rows) == 2
        assert all(row.lease_expires_at is not None for row in rows)
        assert all(
            row.lease_expires_at.replace(tzinfo=UTC) >= NOW + timedelta(hours=2)
            for row in rows
            if row.lease_expires_at is not None
        )


def test_maintenance_gate_rejects_new_reservation_without_mutation(
    quota_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = quota_database
    with factory.begin() as session:
        quota = session.get(StorageGlobalQuota, 1)
        assert quota is not None
        quota.maintenance_token = MAINTENANCE_GATE_ID
        quota.maintenance_database_started_at = NOW - timedelta(minutes=1)
        quota.maintenance_started_at = NOW
        quota.maintenance_owner_expires_at = NOW + timedelta(minutes=2)

    with pytest.raises(StorageQuotaInvariantError, match="maintenance is active"):
        _reserve(factory, (_claim(),))

    with factory() as session:
        quota = session.get(StorageGlobalQuota, 1)
        assert quota is not None
        assert quota.reserved_count == 0
        assert session.get(StorageTenantQuota, ORGANIZATION_ID) is None
        assert session.scalar(select(StoredObject)) is None


def test_active_different_lease_is_rejected_without_double_charge(
    quota_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = quota_database
    claim = _claim()
    _reserve(factory, (claim,))

    with pytest.raises(StorageClaimConflict, match="active different lease"):
        _reserve(factory, (claim,), token=LEASE_B, now=NOW + timedelta(minutes=5))

    with factory() as session:
        quota = session.get(StorageTenantQuota, ORGANIZATION_ID)
        row = session.get(StoredObject, (ORGANIZATION_ID, claim.object_key))
        assert quota is not None
        assert row is not None
        assert (quota.reserved_bytes, quota.reserved_count) == (100, 1)
        assert row.lease_token == LEASE_A


def test_expired_reservation_can_be_atomically_taken_over_without_recharging(
    quota_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = quota_database
    claim = _claim()
    _reserve(factory, (claim,), lifetime=timedelta(minutes=30))

    takeover = _reserve(
        factory,
        (claim,),
        token=LEASE_B,
        now=NOW + timedelta(hours=1),
    )

    assert takeover.newly_reserved_count == 0
    assert takeover.objects[0].lease_token == LEASE_B
    with factory() as session:
        quota = session.get(StorageTenantQuota, ORGANIZATION_ID)
        assert quota is not None
        assert (quota.reserved_bytes, quota.reserved_count) == (100, 1)


@pytest.mark.parametrize(
    ("claim_expiry", "reservation_time", "expected_retry_after"),
    [
        (NOW + timedelta(seconds=17), NOW, 22),
        (NOW + timedelta(minutes=30), NOW + timedelta(hours=1), 5),
    ],
)
def test_reaping_claim_is_never_rebound_as_a_reservation(
    quota_database: tuple[Engine, sessionmaker[Session]],
    claim_expiry: datetime,
    reservation_time: datetime,
    expected_retry_after: int,
) -> None:
    _, factory = quota_database
    _seed_generation_job(factory, status=JobStatus.queued)
    claim = _generation_claim()
    _reserve(factory, (claim,), lifetime=timedelta(minutes=30))
    reaper_token = str(uuid.uuid4())
    with factory.begin() as session:
        row = session.get(StoredObject, (ORGANIZATION_ID, claim.object_key))
        assert row is not None
        row.state = StoredObjectState.reaping
        row.lease_token = None
        row.lease_expires_at = None
        row.claim_token = reaper_token
        row.claim_expires_at = claim_expiry

    with pytest.raises(StorageReservationBusy, match="storage reaper claim") as exc_info:
        _reserve(
            factory,
            (claim,),
            token=LEASE_B,
            now=reservation_time,
        )

    assert exc_info.value.retry_after_seconds == expected_retry_after
    with factory() as session:
        row = session.get(StoredObject, (ORGANIZATION_ID, claim.object_key))
        quota = session.get(StorageTenantQuota, ORGANIZATION_ID)
        assert row is not None
        assert quota is not None
        assert row.state == StoredObjectState.reaping
        assert row.claim_token == reaper_token
        assert row.lease_token is None
        assert (quota.reserved_bytes, quota.reserved_count) == (100, 1)


def test_reaping_claim_with_noncanonical_token_fails_closed(
    quota_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = quota_database
    claim = _claim()
    _reserve(factory, (claim,))
    with factory.begin() as session:
        row = session.get(StoredObject, (ORGANIZATION_ID, claim.object_key))
        assert row is not None
        row.state = StoredObjectState.reaping
        row.lease_token = None
        row.lease_expires_at = None
        row.claim_token = LEASE_A
        row.claim_expires_at = NOW + timedelta(minutes=1)

    with pytest.raises(StorageClaimConflict, match="invalid storage reaper claim"):
        _reserve(factory, (claim,), token=LEASE_B, now=NOW)

    with factory() as session:
        row = session.get(StoredObject, (ORGANIZATION_ID, claim.object_key))
        assert row is not None
        assert row.state == StoredObjectState.reaping
        assert row.claim_token == LEASE_A


@pytest.mark.parametrize(
    ("claim_expiry", "expected_retry_after"),
    [
        (NOW + timedelta(seconds=17), 22),
        (NOW - timedelta(seconds=1), 5),
    ],
)
def test_generation_retry_preflight_blocks_every_reaping_claim(
    quota_database: tuple[Engine, sessionmaker[Session]],
    claim_expiry: datetime,
    expected_retry_after: int,
) -> None:
    _, factory = quota_database
    _seed_generation_job(factory, status=JobStatus.failed)
    claim = _generation_claim()
    _reserve(factory, (claim,))
    reaper_token = str(uuid.uuid4())
    with factory.begin() as session:
        row = session.get(StoredObject, (ORGANIZATION_ID, claim.object_key))
        assert row is not None
        row.state = StoredObjectState.reaping
        row.lease_token = None
        row.lease_expires_at = None
        row.claim_token = reaper_token
        row.claim_expires_at = claim_expiry

    with factory.begin() as session:
        retry_after = prepare_generation_storage_retry(
            session,
            ORGANIZATION_ID,
            GENERATION_JOB_ID,
            now=NOW,
        )

    assert retry_after == expected_retry_after
    with factory() as session:
        row = session.get(StoredObject, (ORGANIZATION_ID, claim.object_key))
        assert row is not None
        assert row.state == StoredObjectState.reaping
        assert row.claim_token == reaper_token


def test_generation_retry_preflight_accepts_safe_states_without_mutation(
    quota_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = quota_database
    _seed_generation_job(factory, status=JobStatus.succeeded)
    claims = (
        _generation_claim("bundle"),
        _generation_claim("manifest"),
        _generation_claim("reserved"),
    )
    _reserve(factory, claims)
    commit_storage_batch(
        factory,
        ORGANIZATION_ID,
        claims[:2],
        lease_token=LEASE_A,
        now=NOW + timedelta(minutes=1),
    )
    with factory.begin() as session:
        delete_pending = session.get(StoredObject, (ORGANIZATION_ID, claims[0].object_key))
        assert delete_pending is not None
        delete_pending.state = StoredObjectState.delete_pending

    with factory.begin() as session:
        assert (
            prepare_generation_storage_retry(
                session,
                ORGANIZATION_ID,
                GENERATION_JOB_ID,
                now=NOW + timedelta(minutes=2),
            )
            == 0
        )

    with factory() as session:
        assert {
            row.object_key: row.state
            for row in session.scalars(
                select(StoredObject).where(
                    StoredObject.organization_id == ORGANIZATION_ID,
                    StoredObject.owner_id == GENERATION_JOB_ID,
                )
            )
        } == {
            claims[0].object_key: StoredObjectState.delete_pending,
            claims[1].object_key: StoredObjectState.committed,
            claims[2].object_key: StoredObjectState.reserved,
        }


@pytest.mark.parametrize("collision", ["object_key", "idempotency_key"])
def test_retired_storage_identity_cannot_be_reserved_again(
    quota_database: tuple[Engine, sessionmaker[Session]],
    collision: str,
) -> None:
    _, factory = quota_database
    claim = _claim()
    tombstone_object_key = (
        claim.object_key if collision == "object_key" else "objects/retired-other.bin"
    )
    tombstone_idempotency_key = (
        claim.idempotency_key if collision == "idempotency_key" else "artifact:retired-other"
    )
    with factory.begin() as session:
        session.add(
            StorageObjectTombstone(
                capacity_bucket=CAPACITY_BUCKET,
                object_key=tombstone_object_key,
                organization_id=ORGANIZATION_ID,
                project_id=PROJECT_ID,
                sha256="e" * 64,
                size_bytes=100,
                media_type="application/octet-stream",
                owner_type="artifact",
                owner_id=str(uuid.uuid4()),
                idempotency_key=tombstone_idempotency_key,
                accounting_state="committed",
                claim_token=str(uuid.uuid4()),
                retired_at=NOW,
            )
        )

    with pytest.raises(StorageClaimConflict, match="permanently retired"):
        _reserve(factory, (claim,))

    with factory() as session:
        global_quota = session.get(StorageGlobalQuota, 1)
        assert global_quota is not None
        assert (global_quota.reserved_bytes, global_quota.reserved_count) == (0, 0)
        assert session.get(StorageTenantQuota, ORGANIZATION_ID) is None
        assert session.scalar(select(StoredObject)) is None


def test_object_or_idempotency_identity_mismatch_fails_closed(
    quota_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = quota_database
    original = _claim()
    _reserve(factory, (original,))

    with pytest.raises(StorageClaimConflict, match="different immutable identity"):
        _reserve(factory, (_claim(sha256="b" * 64),))
    with pytest.raises(StorageClaimConflict, match="different immutable identity"):
        _reserve(
            factory,
            (_claim("other", idempotency_key=original.idempotency_key),),
        )

    with factory() as session:
        quota = session.get(StorageTenantQuota, ORGANIZATION_ID)
        assert quota is not None
        assert (quota.reserved_bytes, quota.reserved_count) == (100, 1)
        assert (
            session.scalar(
                select(StoredObject).where(StoredObject.object_key == "objects/other.bin")
            )
            is None
        )


def test_tenant_quota_rejects_entire_batch_and_rolls_back_global_charge(
    quota_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = quota_database
    with factory.begin() as session:
        session.add(
            StorageTenantQuota(
                organization_id=ORGANIZATION_ID,
                byte_limit=100,
                object_limit=10,
            )
        )

    with pytest.raises(StorageQuotaExceeded, match="tenant storage quota"):
        _reserve(
            factory,
            (_claim("a", size_bytes=60), _claim("b", size_bytes=60)),
        )

    with factory() as session:
        global_quota = session.get(StorageGlobalQuota, 1)
        tenant_quota = session.get(StorageTenantQuota, ORGANIZATION_ID)
        assert global_quota is not None
        assert tenant_quota is not None
        assert (global_quota.reserved_bytes, global_quota.reserved_count) == (0, 0)
        assert (tenant_quota.reserved_bytes, tenant_quota.reserved_count) == (0, 0)
        assert session.scalar(select(StoredObject)) is None


def test_global_quota_rejects_batch_before_tenant_counter_update(
    quota_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = quota_database
    with factory.begin() as session:
        quota = session.get(StorageGlobalQuota, 1)
        assert quota is not None
        quota.byte_limit = 50

    with pytest.raises(StorageQuotaExceeded, match="global storage quota"):
        _reserve(factory, (_claim(size_bytes=51),))

    with factory() as session:
        quota = session.get(StorageGlobalQuota, 1)
        assert quota is not None
        assert quota.reserved_bytes == 0
        assert session.get(StorageTenantQuota, ORGANIZATION_ID) is None
        assert session.scalar(select(StoredObject)) is None


def test_commit_moves_reserved_counters_exactly_once_and_checks_lease(
    quota_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = quota_database
    claims = (_claim("a", size_bytes=40), _claim("b", size_bytes=60))
    _reserve(factory, claims)

    with pytest.raises(StorageClaimConflict, match="different lease"):
        commit_storage_batch(
            factory,
            ORGANIZATION_ID,
            claims,
            lease_token=LEASE_B,
            now=NOW + timedelta(minutes=1),
        )
    commit_storage_batch(
        factory,
        ORGANIZATION_ID,
        claims,
        lease_token=LEASE_A,
        now=NOW + timedelta(minutes=1),
    )
    commit_storage_batch(
        factory,
        ORGANIZATION_ID,
        claims,
        lease_token=LEASE_B,
        now=NOW + timedelta(minutes=2),
    )

    with factory() as session:
        global_quota = session.get(StorageGlobalQuota, 1)
        tenant_quota = session.get(StorageTenantQuota, ORGANIZATION_ID)
        assert global_quota is not None
        assert tenant_quota is not None
        assert (
            global_quota.reserved_bytes,
            global_quota.committed_bytes,
            global_quota.reserved_count,
            global_quota.committed_count,
        ) == (0, 100, 0, 2)
        assert (
            tenant_quota.reserved_bytes,
            tenant_quota.committed_bytes,
            tenant_quota.reserved_count,
            tenant_quota.committed_count,
        ) == (0, 100, 0, 2)
        assert set(session.scalars(select(StoredObject.state))) == {StoredObjectState.committed}


def test_lease_heartbeat_requires_exact_live_owner_and_commit_rechecks_expiry(
    quota_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = quota_database
    claim = _claim()
    _reserve(factory, (claim,), lifetime=timedelta(minutes=30))

    with pytest.raises(StorageClaimConflict, match="ownership was lost"):
        renew_storage_batch_lease(
            factory,
            ORGANIZATION_ID,
            (claim,),
            lease_token=LEASE_B,
            lease_expires_at=NOW + timedelta(hours=2),
            now=NOW + timedelta(minutes=10),
        )
    renew_storage_batch_lease(
        factory,
        ORGANIZATION_ID,
        (claim,),
        lease_token=LEASE_A,
        lease_expires_at=NOW + timedelta(hours=2),
        now=NOW + timedelta(minutes=10),
    )
    with pytest.raises(StorageClaimConflict, match="different lease"):
        commit_storage_batch(
            factory,
            ORGANIZATION_ID,
            (claim,),
            lease_token=LEASE_A,
            now=NOW + timedelta(hours=2, minutes=1),
        )


def test_reservation_updates_global_then_tenant_in_fixed_order(
    quota_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    engine, factory = quota_database
    updates: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture_updates(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if statement.lstrip().upper().startswith("UPDATE STORAGE_"):
            updates.append(statement)

    _reserve(factory, (_claim(),))

    assert len(updates) == 2
    assert "storage_global_quotas" in updates[0]
    assert "storage_tenant_quotas" in updates[1]


@pytest.mark.parametrize(
    "claim",
    [
        _claim(size_bytes=True),
        _claim(sha256="A" * 64),
        _claim(object_key=" objects/a.bin"),
        _claim(idempotency_key="artifact:a\n"),
    ],
)
def test_noncanonical_claims_are_rejected_before_database_mutation(
    quota_database: tuple[Engine, sessionmaker[Session]],
    claim: StorageObjectClaim,
) -> None:
    _, factory = quota_database

    with pytest.raises(StorageClaimConflict):
        _reserve(factory, (claim,))

    with factory() as session:
        global_quota = session.get(StorageGlobalQuota, 1)
        assert global_quota is not None
        assert global_quota.reserved_count == 0
        assert session.get(StorageTenantQuota, ORGANIZATION_ID) is None
        assert session.scalar(select(StoredObject)) is None


def test_canonical_limits_are_finite_and_nested() -> None:
    assert 0 < TENANT_STORAGE_BYTE_LIMIT < GLOBAL_STORAGE_BYTE_LIMIT < 2**63
    assert 0 < TENANT_STORAGE_OBJECT_LIMIT < GLOBAL_STORAGE_OBJECT_LIMIT < 2**63
