from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import storage_recovery
from services.api.app.storage_reaper import (
    ReapCounterKind,
    StorageReapClaim,
    StorageReapResult,
    StorageReapStatus,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
TENANT_A = "00000000-0000-0000-0000-000000000001"
TENANT_B = "00000000-0000-0000-0000-000000000002"


class FakeStore:
    session_factory: Any = object()

    def __init__(self, snapshots: list[storage_recovery.RecoverySnapshot]) -> None:
        self.snapshots = snapshots
        self.events: list[str] = []
        self.finished = False

    def begin(self, token: str, bucket: str) -> int:
        assert str(uuid.UUID(token)) == token
        assert bucket == "recovery-bucket"
        self.events.append("begin")
        return 7

    def renew(self, token: str, epoch: int) -> None:
        assert str(uuid.UUID(token)) == token
        assert epoch == 7
        self.events.append("renew")

    def assert_bucket(self, token: str, epoch: int, bucket: str) -> None:
        assert str(uuid.UUID(token)) == token
        assert epoch == 7
        assert bucket == "recovery-bucket"
        self.events.append("bucket")

    def organization_ids(self, token: str, epoch: int) -> tuple[str, ...]:
        assert epoch == 7
        self.events.append("organizations")
        return (TENANT_A, TENANT_B)

    def terminalize_expired_staging_jobs(self, organization_id: str, token: str, epoch: int) -> int:
        assert epoch == 7
        self.events.append(f"terminalize:{organization_id}")
        return 1 if organization_id == TENANT_A else 0

    def snapshot(self, token: str, epoch: int) -> storage_recovery.RecoverySnapshot:
        assert epoch == 7
        self.events.append("snapshot")
        return self.snapshots.pop(0)

    def finish(self, token: str, epoch: int) -> None:
        assert epoch == 7
        self.events.append("finish")
        self.finished = True


class FakeS3:
    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        return {"ResponseMetadata": {"HTTPStatusCode": 404}}


def _snapshot(
    target_count: int,
    *,
    domain_references: int = 0,
    live_leases: int = 0,
    next_eligible_at: datetime | None = None,
) -> storage_recovery.RecoverySnapshot:
    return storage_recovery.RecoverySnapshot(
        database_now=NOW,
        target_count=target_count,
        domain_reference_count=domain_references,
        live_generation_lease_count=live_leases,
        next_eligible_at=next_eligible_at,
    )


def test_recovery_enters_gate_before_sorted_tenants_and_opens_only_at_zero() -> None:
    store = FakeStore([_snapshot(0)])
    claims: list[str] = []

    def claim_batch(
        _factory: object,
        organization_id: str,
        **_kwargs: object,
    ) -> tuple[StorageReapClaim, ...]:
        claims.append(organization_id)
        store.events.append(f"claim:{organization_id}")
        return ()

    storage_recovery.recover_storage(
        store,
        FakeS3(),
        "recovery-bucket",
        timeout_seconds=60,
        poll_seconds=1,
        claim_batch=claim_batch,
    )

    assert claims == [TENANT_A, TENANT_B]
    assert store.events.index("begin") < store.events.index("organizations")
    assert store.events.index(f"terminalize:{TENANT_A}") < store.events.index(f"claim:{TENANT_A}")
    first_claim = store.events.index(f"claim:{TENANT_A}")
    assert store.events[first_claim - 1] == "bucket"
    assert store.events[-1] == "finish"
    assert store.finished is True


def test_live_generation_lease_waits_and_is_never_reclaimed() -> None:
    store = FakeStore(
        [
            _snapshot(
                1,
                live_leases=1,
                next_eligible_at=NOW + timedelta(seconds=1),
            ),
            _snapshot(0),
        ]
    )
    clock = [0.0]
    sleeps: list[float] = []

    def advance(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    storage_recovery.recover_storage(
        store,
        FakeS3(),
        "recovery-bucket",
        timeout_seconds=60,
        poll_seconds=2,
        monotonic=lambda: clock[0],
        sleep=advance,
        claim_batch=lambda *_args, **_kwargs: (),
    )

    assert sleeps == [1.0]
    assert store.finished is True


def test_domain_reference_fails_closed_without_opening_gate() -> None:
    store = FakeStore([_snapshot(1, domain_references=1)])

    with pytest.raises(storage_recovery.StorageRecoveryError, match="domain reference"):
        storage_recovery.recover_storage(
            store,
            FakeS3(),
            "recovery-bucket",
            timeout_seconds=60,
            poll_seconds=1,
            claim_batch=lambda *_args, **_kwargs: (),
        )

    assert store.finished is False


def test_reaper_domain_reference_result_fails_before_snapshot() -> None:
    store = FakeStore([_snapshot(0)])
    claim = StorageReapClaim(
        organization_id=TENANT_A,
        object_key="staging/file.bin",
        sha256="a" * 64,
        size_bytes=1,
        counter_kind=ReapCounterKind.reserved,
        claim_token=str(uuid.UUID(int=uuid.uuid4().int, version=4)),
        claim_expires_at=NOW + timedelta(seconds=30),
    )

    def claim_batch(
        _factory: object,
        organization_id: str,
        **_kwargs: object,
    ) -> tuple[StorageReapClaim, ...]:
        return (claim,) if organization_id == TENANT_A else ()

    def reap_claim(
        _factory: object,
        _client: object,
        _bucket: str,
        value: StorageReapClaim,
    ) -> StorageReapResult:
        return StorageReapResult(
            claim=value,
            status=StorageReapStatus.domain_reference,
            retryable=False,
        )

    with pytest.raises(storage_recovery.StorageRecoveryError, match="domain reference"):
        storage_recovery.recover_storage(
            store,
            FakeS3(),
            "recovery-bucket",
            timeout_seconds=60,
            poll_seconds=1,
            claim_batch=claim_batch,
            reap_claim=reap_claim,
        )

    assert "snapshot" not in store.events
    assert store.finished is False


def test_reaper_identity_mismatch_fails_before_snapshot_or_gate_open() -> None:
    store = FakeStore([_snapshot(0)])
    claim = StorageReapClaim(
        organization_id=TENANT_A,
        object_key="staging/file.bin",
        sha256="a" * 64,
        size_bytes=1,
        counter_kind=ReapCounterKind.reserved,
        claim_token=str(uuid.UUID(int=uuid.uuid4().int, version=4)),
        claim_expires_at=NOW + timedelta(seconds=30),
    )

    def claim_batch(
        _factory: object,
        organization_id: str,
        **_kwargs: object,
    ) -> tuple[StorageReapClaim, ...]:
        return (claim,) if organization_id == TENANT_A else ()

    def reap_claim(
        _factory: object,
        _client: object,
        _bucket: str,
        value: StorageReapClaim,
    ) -> StorageReapResult:
        return StorageReapResult(
            claim=value,
            status=StorageReapStatus.identity_mismatch,
            retryable=False,
        )

    with pytest.raises(storage_recovery.StorageRecoveryError, match="identity differs"):
        storage_recovery.recover_storage(
            store,
            FakeS3(),
            "recovery-bucket",
            timeout_seconds=60,
            poll_seconds=1,
            claim_batch=claim_batch,
            reap_claim=reap_claim,
        )

    assert "snapshot" not in store.events
    assert store.finished is False


@pytest.mark.parametrize(
    "username",
    ["custombuild_api", "custombuild_worker", "custombuild_storage_attestor"],
)
def test_recovery_rejects_every_long_lived_database_role(
    monkeypatch: pytest.MonkeyPatch,
    username: str,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+psycopg://{username}:long-enough-unit-password@postgres/custombuild",
    )
    monkeypatch.setenv("S3_ENDPOINT", "http://object-storage:8333")
    monkeypatch.setenv("S3_ACCESS_KEY", "test-access")
    monkeypatch.setenv("S3_SECRET_KEY", "test-secret")
    monkeypatch.setenv("S3_BUCKET", "test-bucket")

    with pytest.raises(storage_recovery.StorageRecoveryError, match="migrator role"):
        storage_recovery.load_settings()


def test_recovery_sql_is_token_epoch_bound_and_live_lease_safe() -> None:
    source = Path(storage_recovery.__file__).read_text(encoding="utf-8")
    ledger_migration = (
        Path(__file__).parents[2] / "services/api/alembic/versions/0012_storage_quota_ledger.py"
    ).read_text(encoding="utf-8")

    assert "maintenance_token = :token" in source
    assert "maintenance_epoch = :epoch" in source
    assert "pg_postmaster_start_time() AS database_started_at" in source
    assert "maintenance_database_started_at = :database_started_at" in source
    assert "current_database_started_at == database_started_at" in source
    assert "maintenance boot ownership was lost" in source
    assert source.count("maintenance_database_started_at = ") >= 4
    assert source.count('"pg_postmaster_start_time() RETURNING id"') >= 2
    assert "maintenance_database_started_at = NULL" in source
    assert "ledger_bucket != canonical_bucket" in source
    assert "capacity_bucket = coalesce(capacity_bucket, :capacity_bucket)" in source
    assert "generation_job.lease_expires_at > :database_now" in source
    assert "owned.lease_expires_at <= :database_now" in source
    assert "status = 'failed'" in source
    assert "pg_postmaster_start_time()" in source
    assert "state IN ('reserved', 'reaping', 'delete_pending')" in source
    assert "tenant_reserved_count" in source
    assert "tenant_reserved_bytes" in source
    assert "SELECT id FROM organizations ORDER BY id" in source
    assert "stored objects exist without a tenant quota row" in source
    assert "JOIN storage_object_tombstones AS tombstone" in source
    assert "tombstone.object_key = stored.object_key" in source
    assert "OR tombstone.idempotency_key = stored.idempotency_key" in source
    assert "permanently retired storage identity" in source
    assert 'parsed_database.username != "custombuild_migrator"' in source
    assert '"maintenance_database_started_at"' in ledger_migration
    assert "maintenance_started_at >= maintenance_database_started_at" in ledger_migration


def test_migration_reserve_is_maintenance_fenced_but_attestation_remains_exact() -> None:
    migration = (
        Path(__file__).parents[2]
        / "services/api/alembic/versions/0013_storage_quota_security_functions.py"
    ).read_text(encoding="utf-8")

    reserve = migration.split("_CREATE_RESERVE =", 1)[1].split("_CREATE_RENEW =", 1)[0]
    attest = migration.split("_CREATE_ATTEST_CAPACITY =", 1)[1].split(
        "_CREATE_INVALIDATE_CAPACITY =", 1
    )[0]
    assert "STORAGE_MAINTENANCE_ACTIVE" in reserve
    assert "STORAGE_RECOVERY_REQUIRED" in reserve
    assert "ledger_object_count <> v_global.committed_count" not in reserve
    assert "ledger_bytes <> v_global.committed_bytes" not in reserve
    assert "p_inventory_object_count <> p_ledger_object_count" in attest
    assert "p_inventory_bytes <> p_ledger_bytes" in attest
    assert "p_ledger_object_count <> v_global.committed_count" in attest
    assert "p_ledger_bytes <> v_global.committed_bytes" in attest
    assert "generation_job.lease_expires_at > v_now" in migration
