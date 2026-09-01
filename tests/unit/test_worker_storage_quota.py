from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import custombuild_worker.tasks as worker_tasks
import pytest
from app.db import Base
from app.job_policy import GENERATION_LEASE_TTL
from app.models import (
    Artifact,
    AuditEvent,
    DesignStatus,
    DesignVersion,
    GenerationJob,
    JobStatus,
    Organization,
    OutboxEvent,
    Project,
    StorageGlobalQuota,
    StorageTenantQuota,
    StoredObject,
    StoredObjectState,
    User,
)
from app.storage import ArtifactStorageUnavailableError
from app.storage_quota import (
    GLOBAL_STORAGE_BYTE_LIMIT,
    GLOBAL_STORAGE_OBJECT_LIMIT,
    StorageClaimConflict,
    StorageObjectClaim,
    StorageQuotaExceeded,
    StorageQuotaInvariantError,
    StorageReservationBusy,
    reserve_storage_batch,
)
from app.storage_reaper import StorageReapStatus
from custombuild_manufacturing import ArtifactFile, CAMStageStatus, ProductionBlockedError
from custombuild_rules import RuleStatus
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

ORGANIZATION_ID = "22222222-2222-4222-8222-222222222222"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
VERSION_ID = "44444444-4444-4444-8444-444444444444"
JOB_ID = "55555555-5555-4555-8555-555555555555"
USER_ID = "66666666-6666-4666-8666-666666666666"
LEASE_A = "77777777-7777-4777-8777-777777777777"
LEASE_B = "88888888-8888-4888-8888-888888888888"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def worker_quota_database(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[sessionmaker[Session]]:
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
    monkeypatch.setattr(worker_tasks, "SessionFactory", factory)
    monkeypatch.setattr(
        worker_tasks,
        "_validate_completion_evidence",
        lambda _session, _organization_id, _job: None,
    )
    monkeypatch.setattr(
        worker_tasks,
        "_validate_retention_before_generation",
        lambda _organization_id, _version, *, minimum_valid_until: None,
    )
    _seed_worker_quota_subject(factory)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed_worker_quota_subject(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        session.add(
            Organization(
                id=ORGANIZATION_ID,
                name="Worker quota tenant",
                slug=f"worker-quota-{uuid.uuid4().hex}",
            )
        )
        session.add(
            User(
                id=USER_ID,
                oidc_sub=f"worker-quota-{uuid.uuid4()}",
                email=f"worker-quota-{uuid.uuid4().hex}@example.test",
                name="Worker quota actor",
            )
        )
        session.flush()
        session.add(
            Project(
                id=PROJECT_ID,
                organization_id=ORGANIZATION_ID,
                name="Worker quota project",
            )
        )
        session.flush()
        session.add(
            DesignVersion(
                id=VERSION_ID,
                organization_id=ORGANIZATION_ID,
                project_id=PROJECT_ID,
                revision=1,
                status=DesignStatus.design_validated,
                design_hash="d" * 64,
                context_hash="c" * 64,
                spec_json={},
                source_provenance_json={},
                result_json={},
                engine_version="test-engine",
                template_version="test-template",
                template_id="shelving",
                template_capability_fingerprint="a" * 64,
                rule_version="test-rules",
                created_by=USER_ID,
            )
        )
        session.flush()
        session.add(
            GenerationJob(
                id=JOB_ID,
                organization_id=ORGANIZATION_ID,
                design_version_id=VERSION_ID,
                status=JobStatus.queued,
                idempotency_key="e" * 64,
                production_context_hash="f" * 64,
                production_engine_context_json={"schema": "test"},
                request_json={},
                attempts=0,
            )
        )
        session.add(
            StorageGlobalQuota(
                id=1,
                byte_limit=GLOBAL_STORAGE_BYTE_LIMIT,
                object_limit=GLOBAL_STORAGE_OBJECT_LIMIT,
            )
        )


def _generation_result(lease_token: str) -> dict[str, Any]:
    attempt_id = worker_tasks._generation_attempt_id(JOB_ID, lease_token)

    def object_key(kind: str, suffix: str) -> str:
        artifact_id = worker_tasks._generation_artifact_id(attempt_id, kind)
        return (
            f"{ORGANIZATION_ID}/{'d' * 64}/{'f' * 64}/attempts/{attempt_id}/"
            f"artifacts/{artifact_id}/{suffix}"
        )

    return {
        "production_engine_context_hash": worker_tasks.sha256_hex(
            worker_tasks.canonical_json_bytes({"schema": "test"})
        ),
        "bundle_object_key": object_key(
            "production_bundle",
            f"linked-v1/{'b' * 64}/{'a' * 64}/production.zip",
        ),
        "bundle_sha256": "a" * 64,
        "bundle_size_bytes": 100,
        "manifest_object_key": object_key("manifest", f"{'b' * 64}/manifest.json"),
        "manifest_sha256": "b" * 64,
        "manifest_size_bytes": 200,
        "evidence_artifacts": [
            {
                "kind": "dfm_report",
                "object_key": object_key(
                    "dfm_report",
                    f"{'c' * 64}/evidence/validation__dfm-report.json",
                ),
                "sha256": "c" * 64,
                "size_bytes": 300,
                "content_type": "application/json",
            }
        ],
    }


def _claimed_subject(
    factory: sessionmaker[Session],
) -> tuple[GenerationJob, DesignVersion, str]:
    claim = worker_tasks._claim_job(JOB_ID, ORGANIZATION_ID)
    assert claim is not None
    job, version = claim
    token = job.lease_token
    assert token is not None
    return job, version, token


def _reserve_generation_result(
    factory: sessionmaker[Session],
    job: GenerationJob,
    version: DesignVersion,
    token: str,
    result: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[StorageObjectClaim, ...]:
    claims = worker_tasks._generation_storage_claims(job, version, result, token)
    reserve_storage_batch(
        factory,
        ORGANIZATION_ID,
        claims,
        lease_token=token,
        lease_duration=GENERATION_LEASE_TTL,
        now=now,
    )
    return claims


def _fast_generation_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[GenerationJob, DesignVersion]:
    job = GenerationJob(
        id=JOB_ID,
        organization_id=ORGANIZATION_ID,
        design_version_id=VERSION_ID,
        status=JobStatus.running,
        idempotency_key="e" * 64,
        production_context_hash="f" * 64,
        production_engine_context_json={"schema": "fixture"},
        request_json={
            "stock_width_mm": 2_440,
            "stock_height_mm": 1_220,
            "stock_count": 2,
            "include_step": False,
            "include_freecad_project": False,
            "include_validation_program": False,
            "approved_warning_overrides": [],
        },
        attempts=1,
        lease_token=LEASE_A,
    )
    version = DesignVersion(
        id=VERSION_ID,
        organization_id=ORGANIZATION_ID,
        project_id=PROJECT_ID,
        revision=1,
        status=DesignStatus.design_validated,
        design_hash="d" * 64,
        context_hash="c" * 64,
        spec_json={},
        source_provenance_json={},
        result_json={},
        engine_version="test-engine",
        template_version="test-template",
        template_id="shelving",
        template_capability_fingerprint="a" * 64,
        rule_version="test-rules",
        created_by=USER_ID,
    )
    runtime_context = {"schema": "fixture"}
    resolved_context = SimpleNamespace(
        app_version="test-app",
        fingerprint="9" * 64,
        as_dict=lambda: runtime_context,
    )
    monkeypatch.setattr(
        worker_tasks,
        "_resolve_current_job_context",
        lambda _job, _version: SimpleNamespace(
            machine=SimpleNamespace(profile_id="fixture-router", version="fixture-v1"),
            postprocessor=SimpleNamespace(version="fixture-post-v1"),
            context=resolved_context,
        ),
    )
    monkeypatch.setattr(
        worker_tasks,
        "require_template_for_revision",
        lambda *_args: SimpleNamespace(),
    )
    monkeypatch.setattr(
        worker_tasks,
        "resolve_template_capability",
        lambda _template_id: SimpleNamespace(archetype="shelving"),
    )
    capability_snapshot = {
        "capability_fingerprint": version.template_capability_fingerprint,
    }
    material = SimpleNamespace(material_id="fixture-board", version="fixture-v1")
    spec = SimpleNamespace(
        material=material,
        back_material=None,
        parameters=SimpleNamespace(actual_thickness_um=18_000),
    )
    monkeypatch.setattr(
        worker_tasks,
        "_load_frozen_design_spec",
        lambda _version, _capability: (spec, capability_snapshot),
    )
    monkeypatch.setattr(
        worker_tasks,
        "build_bookcase",
        lambda _spec: SimpleNamespace(design_hash=version.design_hash),
    )
    rule_report = SimpleNamespace(
        overall_status=RuleStatus.PASS,
        evaluations=(),
        disclaimer="fixture disclaimer",
    )
    monkeypatch.setattr(worker_tasks, "evaluate_design", lambda _design: rule_report)
    for document_builder in (
        "bom_pdf",
        "hardware_csv",
        "assembly_manual_pdf",
        "assembly_readiness_json",
        "labels_pdf",
        "qa_protocol_pdf",
        "validation_report_pdf",
    ):
        monkeypatch.setattr(worker_tasks, document_builder, lambda *_args: b"document")
    monkeypatch.setattr(worker_tasks, "canonical_json_bytes", lambda _value: b"{}")
    evidence = ArtifactFile(
        "validation/dfm-report.json",
        b"evidence",
        "application/json",
        "DFM_VALIDATION_REPORT",
    )
    review_status = SimpleNamespace(
        cam_status=CAMStageStatus.BLOCKED,
        as_dict=lambda: {"cam_status": "BLOCKED"},
    )
    fake_bundle = SimpleNamespace(
        zip_bytes=b"bundle",
        manifest={
            "generation_context_hash": job.production_context_hash,
            "production_engine_context": runtime_context,
        },
        artifacts=(evidence,),
        layouts=(),
        dfm_report=SimpleNamespace(status=SimpleNamespace(value="PASS")),
        review_status=review_status,
        workshop_readiness=SimpleNamespace(as_dict=lambda: {"ready": False}),
    )
    monkeypatch.setattr(worker_tasks, "build_production_bundle", lambda *_a, **_kw: fake_bundle)
    return job, version


def test_complete_generation_batch_is_reserved_before_first_object_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _fast_generation_subject(monkeypatch)
    guard = worker_tasks._GenerationLeaseGuard(ORGANIZATION_ID, LEASE_A)
    events: list[tuple[str, tuple[str, ...] | str]] = []

    def reserve(
        _factory: object,
        organization_id: str,
        claims: tuple[StorageObjectClaim, ...],
        **kwargs: Any,
    ) -> None:
        assert organization_id == ORGANIZATION_ID
        assert kwargs == {
            "lease_token": LEASE_A,
            "lease_duration": GENERATION_LEASE_TTL,
            "capacity_settings": worker_tasks.WORKER_SETTINGS,
        }
        events.append(("reserve", tuple(claim.object_key for claim in claims)))

    def put(
        key: str,
        _payload: bytes,
        _content_type: str,
        **_kwargs: Any,
    ) -> None:
        events.append(("put", key))

    monkeypatch.setattr(worker_tasks, "reserve_storage_batch", reserve)
    monkeypatch.setattr(worker_tasks, "_put_object", put)

    result = worker_tasks._generate(
        job,
        version,
        lease_token=LEASE_A,
        lease_guard=guard,
    )

    expected_keys = {
        result["bundle_object_key"],
        result["manifest_object_key"],
        *(item["object_key"] for item in result["evidence_artifacts"]),
    }
    assert events[0][0] == "reserve"
    assert set(events[0][1]) == expected_keys
    assert [event[1] for event in events[1:]] == [
        *(item["object_key"] for item in result["evidence_artifacts"]),
        result["bundle_object_key"],
        result["manifest_object_key"],
    ]
    assert guard.storage_claims() == worker_tasks._generation_storage_claims(
        job,
        version,
        result,
        LEASE_A,
    )


@pytest.mark.parametrize(
    "guard",
    (
        worker_tasks._GenerationLeaseGuard(ORGANIZATION_ID, LEASE_B),
        worker_tasks._GenerationLeaseGuard(PROJECT_ID, LEASE_A),
    ),
    ids=("wrong-token", "wrong-tenant"),
)
def test_generation_rejects_a_mismatched_guard_before_build_or_storage(
    monkeypatch: pytest.MonkeyPatch,
    guard: worker_tasks._GenerationLeaseGuard,
) -> None:
    job, version = _fast_generation_subject(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        worker_tasks,
        "_resolve_current_job_context",
        lambda *_args: calls.append("resolve"),
    )
    monkeypatch.setattr(
        worker_tasks,
        "reserve_storage_batch",
        lambda *_args, **_kwargs: calls.append("reserve"),
    )
    monkeypatch.setattr(
        worker_tasks,
        "_put_object",
        lambda *_args, **_kwargs: calls.append("put"),
    )

    with pytest.raises(ValueError, match="exact lease guard"):
        worker_tasks._generate(
            job,
            version,
            lease_token=LEASE_A,
            lease_guard=guard,
        )

    assert calls == []


def test_generation_attempt_keys_are_disjoint_and_stale_delete_cannot_target_new_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _fast_generation_subject(monkeypatch)
    provider_keys: set[str] = set()
    monkeypatch.setattr(worker_tasks, "reserve_storage_batch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker_tasks,
        "_put_object",
        lambda key, *_args, **_kwargs: provider_keys.add(key),
    )

    def run_attempt(token: str) -> tuple[dict[str, Any], tuple[StorageObjectClaim, ...]]:
        job.lease_token = token
        guard = worker_tasks._GenerationLeaseGuard(ORGANIZATION_ID, token)
        result = worker_tasks._generate(
            job,
            version,
            lease_token=token,
            lease_guard=guard,
        )
        return result, guard.storage_claims()

    first_result, first_claims = run_attempt(LEASE_A)
    second_result, second_claims = run_attempt(LEASE_B)
    first_keys = {claim.object_key for claim in first_claims}
    second_keys = {claim.object_key for claim in second_claims}
    first_attempt_id = worker_tasks._generation_attempt_id(JOB_ID, LEASE_A)
    second_attempt_id = worker_tasks._generation_attempt_id(JOB_ID, LEASE_B)

    assert first_attempt_id != second_attempt_id
    assert first_keys.isdisjoint(second_keys)
    assert first_result["bundle_sha256"] == second_result["bundle_sha256"]
    assert first_result["manifest_sha256"] == second_result["manifest_sha256"]
    for attempt_id, claims in (
        (first_attempt_id, first_claims),
        (second_attempt_id, second_claims),
    ):
        for claim in claims:
            kind = claim.idempotency_key.split(":", maxsplit=3)[2]
            artifact_id = worker_tasks._generation_artifact_id(attempt_id, kind)
            assert f"/attempts/{attempt_id}/artifacts/{artifact_id}/" in claim.object_key
            assert claim.idempotency_key == (f"generation:{JOB_ID}:{kind}:{artifact_id}")

    # A delayed delete from the old reaper claim can address only incarnation A.
    for stale_key in first_keys:
        provider_keys.discard(stale_key)
    assert second_keys <= provider_keys


@pytest.mark.parametrize(
    "failure",
    (
        StorageQuotaExceeded("capacity exhausted"),
        StorageQuotaInvariantError("capacity has not been attested"),
    ),
    ids=("exhausted", "unverified-capacity"),
)
def test_quota_failure_blocks_generation_before_any_object_put(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    job, version = _fast_generation_subject(monkeypatch)
    guard = worker_tasks._GenerationLeaseGuard(ORGANIZATION_ID, LEASE_A)
    puts: list[str] = []

    def reject_reservation(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(worker_tasks, "reserve_storage_batch", reject_reservation)
    monkeypatch.setattr(
        worker_tasks,
        "_put_object",
        lambda key, *_args, **_kwargs: puts.append(key),
    )

    with pytest.raises(
        ProductionBlockedError,
        match="could not reserve the complete immutable batch",
    ):
        worker_tasks._generate(
            job,
            version,
            lease_token=LEASE_A,
            lease_guard=guard,
        )

    assert puts == []
    assert guard.storage_claims() == ()


def test_storage_ownership_loss_after_first_put_stops_all_remaining_puts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _fast_generation_subject(monkeypatch)
    guard = worker_tasks._GenerationLeaseGuard(ORGANIZATION_ID, LEASE_A)
    puts: list[str] = []
    monkeypatch.setattr(worker_tasks, "reserve_storage_batch", lambda *_a, **_kw: None)

    def lose_ownership_after_first_put(
        key: str,
        _payload: bytes,
        _content_type: str,
        **_kwargs: Any,
    ) -> None:
        puts.append(key)
        guard.fail(StorageClaimConflict("reservation replaced"))

    monkeypatch.setattr(worker_tasks, "_put_object", lose_ownership_after_first_put)

    with pytest.raises(worker_tasks.GenerationLeaseOwnershipLost):
        worker_tasks._generate(
            job,
            version,
            lease_token=LEASE_A,
            lease_guard=guard,
        )

    assert len(puts) == 1
    assert puts[0].endswith("validation__dfm-report.json")


def test_transient_put_failure_delays_retry_until_storage_lease_can_be_taken_over(
    worker_quota_database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = worker_quota_database
    reservation_time = datetime.now(UTC)
    claimed_storage: list[StorageObjectClaim] = []

    def fail_after_reservation(
        job: GenerationJob,
        version: DesignVersion,
        *,
        lease_token: str,
        lease_guard: worker_tasks._GenerationLeaseGuard,
    ) -> dict[str, Any]:
        claim = StorageObjectClaim(
            project_id=version.project_id,
            object_key="private/transient-upload.bin",
            sha256=hashlib.sha256(b"transient-upload").hexdigest(),
            size_bytes=len(b"transient-upload"),
            media_type="application/octet-stream",
            owner_type="generation_job",
            owner_id=job.id,
            idempotency_key=f"generation:{job.id}:transient_fixture",
        )
        reserve_storage_batch(
            factory,
            ORGANIZATION_ID,
            (claim,),
            lease_token=lease_token,
            lease_duration=GENERATION_LEASE_TTL,
            now=reservation_time,
        )
        lease_guard.bind_storage_claims((claim,))
        claimed_storage.append(claim)
        raise ArtifactStorageUnavailableError("transient PUT failed")

    monkeypatch.setattr(worker_tasks, "_generate", fail_after_reservation)
    result = worker_tasks.generate_package.run(
        job_id=JOB_ID,
        organization_id=ORGANIZATION_ID,
    )

    assert result["state"] == "retry_scheduled"
    assert result["retry_after_seconds"] >= int(GENERATION_LEASE_TTL.total_seconds())
    with factory() as session:
        failed_attempt = session.get(GenerationJob, JOB_ID)
        assert failed_attempt is not None
        assert failed_attempt.status == JobStatus.queued
        retry_event = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_key == f"generation-retry:{JOB_ID}:1")
        )
        assert retry_event is not None
        assert retry_event.available_at >= (retry_event.created_at + GENERATION_LEASE_TTL)
        assert failed_attempt.next_attempt_at == retry_event.available_at
        reserved = session.get(
            StoredObject,
            (ORGANIZATION_ID, claimed_storage[0].object_key),
        )
        assert reserved is not None
        assert reserved.state == StoredObjectState.reserved

    assert worker_tasks._claim_job(JOB_ID, ORGANIZATION_ID) is None
    retry_due = worker_tasks._as_utc(retry_event.available_at)
    monkeypatch.setattr(
        worker_tasks,
        "_database_time",
        lambda _session, override=None: retry_due,
    )
    second_claim = worker_tasks._claim_job(JOB_ID, ORGANIZATION_ID)
    assert second_claim is not None
    second_job, _second_version = second_claim
    second_token = second_job.lease_token
    assert second_token is not None
    with pytest.raises(StorageReservationBusy):
        reserve_storage_batch(
            factory,
            ORGANIZATION_ID,
            tuple(claimed_storage),
            lease_token=second_token,
            lease_duration=GENERATION_LEASE_TTL,
            now=reservation_time + GENERATION_LEASE_TTL - timedelta(microseconds=1),
        )
    takeover = reserve_storage_batch(
        factory,
        ORGANIZATION_ID,
        tuple(claimed_storage),
        lease_token=second_token,
        lease_duration=GENERATION_LEASE_TTL,
        now=reservation_time + GENERATION_LEASE_TTL,
    )
    assert takeover.newly_reserved_count == 0
    assert takeover.objects[0].lease_token == second_token


def test_completion_atomically_commits_ledger_artifacts_and_job(
    worker_quota_database: sessionmaker[Session],
) -> None:
    factory = worker_quota_database
    job, version, token = _claimed_subject(factory)
    result = _generation_result(token)
    claims = _reserve_generation_result(factory, job, version, token, result)

    assert worker_tasks._complete_job(
        JOB_ID,
        ORGANIZATION_ID,
        token,
        version,
        result,
        require_storage_reservation=True,
    )

    with factory() as session:
        stored_job = session.get(GenerationJob, JOB_ID)
        tenant_quota = session.get(StorageTenantQuota, ORGANIZATION_ID)
        global_quota = session.get(StorageGlobalQuota, 1)
        assert stored_job is not None
        assert tenant_quota is not None
        assert global_quota is not None
        assert stored_job.status == JobStatus.succeeded
        assert stored_job.lease_token is None
        assert {item.kind for item in session.scalars(select(Artifact))} == {
            "production_bundle",
            "manifest",
            "dfm_report",
        }
        rows = tuple(session.scalars(select(StoredObject).order_by(StoredObject.object_key)))
        assert {row.object_key for row in rows} == {claim.object_key for claim in claims}
        assert {row.state for row in rows} == {StoredObjectState.committed}
        total_bytes = sum(claim.size_bytes for claim in claims)
        assert (
            global_quota.reserved_bytes,
            global_quota.reserved_count,
            global_quota.committed_bytes,
            global_quota.committed_count,
        ) == (0, 0, total_bytes, len(claims))
        assert (
            tenant_quota.reserved_bytes,
            tenant_quota.reserved_count,
            tenant_quota.committed_bytes,
            tenant_quota.committed_count,
        ) == (0, 0, total_bytes, len(claims))
        assert (
            len(
                tuple(
                    session.scalars(
                        select(AuditEvent).where(
                            AuditEvent.entity_id == JOB_ID,
                            AuditEvent.action == "generation.succeeded",
                        )
                    )
                )
            )
            == 1
        )


def test_failed_completion_rolls_back_ledger_artifacts_and_job_together(
    worker_quota_database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = worker_quota_database
    job, version, token = _claimed_subject(factory)
    result = _generation_result(token)
    claims = _reserve_generation_result(factory, job, version, token, result)

    def reject_evidence(*_args: object, **_kwargs: object) -> None:
        raise ProductionBlockedError("completion evidence rejected")

    monkeypatch.setattr(worker_tasks, "_validate_completion_evidence", reject_evidence)
    with pytest.raises(ProductionBlockedError, match="completion evidence rejected"):
        worker_tasks._complete_job(
            JOB_ID,
            ORGANIZATION_ID,
            token,
            version,
            result,
            require_storage_reservation=True,
        )

    with factory() as session:
        stored_job = session.get(GenerationJob, JOB_ID)
        tenant_quota = session.get(StorageTenantQuota, ORGANIZATION_ID)
        global_quota = session.get(StorageGlobalQuota, 1)
        assert stored_job is not None
        assert tenant_quota is not None
        assert global_quota is not None
        assert stored_job.status == JobStatus.running
        assert stored_job.lease_token == token
        assert stored_job.result_json is None
        assert tuple(session.scalars(select(Artifact))) == ()
        assert {row.state for row in session.scalars(select(StoredObject))} == {
            StoredObjectState.reserved
        }
        total_bytes = sum(claim.size_bytes for claim in claims)
        assert (
            global_quota.reserved_bytes,
            global_quota.reserved_count,
            global_quota.committed_bytes,
            global_quota.committed_count,
        ) == (total_bytes, len(claims), 0, 0)
        assert (
            tenant_quota.reserved_bytes,
            tenant_quota.reserved_count,
            tenant_quota.committed_bytes,
            tenant_quota.committed_count,
        ) == (total_bytes, len(claims), 0, 0)
        assert (
            tuple(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.entity_id == JOB_ID,
                        AuditEvent.action == "generation.succeeded",
                    )
                )
            )
            == ()
        )


def test_generation_claim_rejects_an_object_key_from_another_attempt(
    worker_quota_database: sessionmaker[Session],
) -> None:
    factory = worker_quota_database
    job, version, token = _claimed_subject(factory)
    original = _generation_result(token)
    _reserve_generation_result(factory, job, version, token, original, now=NOW)
    changed = _generation_result(LEASE_B)

    with pytest.raises(ProductionBlockedError, match="exact storage attempt"):
        worker_tasks._generation_storage_claims(
            job,
            version,
            changed,
            token,
        )

    with factory() as session:
        global_quota = session.get(StorageGlobalQuota, 1)
        assert global_quota is not None
        assert (global_quota.reserved_bytes, global_quota.reserved_count) == (600, 3)


def test_completion_rejects_manipulated_artifact_incarnation_before_artifact_insert(
    worker_quota_database: sessionmaker[Session],
) -> None:
    factory = worker_quota_database
    job, version, token = _claimed_subject(factory)
    result = _generation_result(token)
    attempt_id = worker_tasks._generation_attempt_id(job.id, token)
    expected_artifact_id = worker_tasks._generation_artifact_id(
        attempt_id,
        "production_bundle",
    )
    result["bundle_object_key"] = str(result["bundle_object_key"]).replace(
        expected_artifact_id,
        "99999999-9999-4999-8999-999999999999",
    )

    with pytest.raises(ProductionBlockedError, match="exact storage attempt"):
        worker_tasks._complete_job(
            JOB_ID,
            ORGANIZATION_ID,
            token,
            version,
            result,
            require_storage_reservation=True,
        )

    with factory() as session:
        stored_job = session.get(GenerationJob, JOB_ID)
        assert stored_job is not None
        assert stored_job.status == JobStatus.running
        assert stored_job.result_json is None
        assert tuple(session.scalars(select(Artifact))) == ()


def test_job_lease_renewal_rejects_the_exact_expiry_boundary(
    worker_quota_database: sessionmaker[Session],
) -> None:
    factory = worker_quota_database
    _job, _version, token = _claimed_subject(factory)
    boundary = NOW
    with factory.begin() as session:
        stored_job = session.get(GenerationJob, JOB_ID)
        assert stored_job is not None
        stored_job.lease_expires_at = boundary
        stored_job.deadline_at = boundary + timedelta(hours=1)

    assert worker_tasks._renew_job_lease(
        JOB_ID,
        ORGANIZATION_ID,
        token,
        now=boundary - timedelta(microseconds=1),
    )
    with factory.begin() as session:
        stored_job = session.get(GenerationJob, JOB_ID)
        assert stored_job is not None
        stored_job.lease_expires_at = boundary

    assert not worker_tasks._renew_job_lease(
        JOB_ID,
        ORGANIZATION_ID,
        token,
        now=boundary,
    )
    with factory() as session:
        stored_job = session.get(GenerationJob, JOB_ID)
        assert stored_job is not None
        assert stored_job.status == JobStatus.running
        assert stored_job.lease_token == token
        assert stored_job.lease_expires_at == boundary.replace(tzinfo=None)


def test_periodic_storage_reaper_is_globally_bounded_and_counts_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []
    sentinel_client = object()
    monkeypatch.setattr(
        worker_tasks,
        "_organization_ids",
        lambda: (ORGANIZATION_ID, "99999999-9999-4999-8999-999999999999"),
    )
    monkeypatch.setattr(worker_tasks, "_storage_reaper_start_index", lambda _count: 0)
    monkeypatch.setattr(worker_tasks, "_s3_client", lambda: sentinel_client)

    def reap(
        _factory: object,
        client: object,
        _bucket: str,
        organization_id: str,
        *,
        batch_size: int,
    ) -> tuple[SimpleNamespace, ...]:
        assert client is sentinel_client
        calls.append((organization_id, batch_size))
        return tuple(SimpleNamespace(status=StorageReapStatus.deleted) for _ in range(batch_size))

    monkeypatch.setattr(worker_tasks, "reap_storage_batch", reap)

    counts = worker_tasks.reap_abandoned_storage.run(limit=12)

    assert calls == [(ORGANIZATION_ID, 10), ("99999999-9999-4999-8999-999999999999", 2)]
    assert counts[StorageReapStatus.deleted.value] == 12
    assert counts["processed"] == 12
    assert counts["tenant_failures"] == 0


@pytest.mark.parametrize(
    "status",
    [StorageReapStatus.provider_error, StorageReapStatus.identity_mismatch],
)
def test_periodic_storage_reaper_surfaces_unsafe_provider_outcome(
    monkeypatch: pytest.MonkeyPatch,
    status: StorageReapStatus,
) -> None:
    monkeypatch.setattr(worker_tasks, "_organization_ids", lambda: (ORGANIZATION_ID,))
    monkeypatch.setattr(worker_tasks, "_storage_reaper_start_index", lambda _count: 0)
    monkeypatch.setattr(worker_tasks, "_s3_client", object)
    monkeypatch.setattr(
        worker_tasks,
        "reap_storage_batch",
        lambda *_args, **_kwargs: (SimpleNamespace(status=status),),
    )

    with pytest.raises(RuntimeError, match="retained quota"):
        worker_tasks.reap_abandoned_storage.run(limit=1)


def test_periodic_storage_reaper_rotates_persistently_across_backlogged_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenants = tuple(f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 5))
    cursor = iter(range(4))
    calls_by_tick: list[tuple[str, ...]] = []
    current_tick: list[str] = []
    monkeypatch.setattr(worker_tasks, "_organization_ids", lambda: tenants)
    monkeypatch.setattr(
        worker_tasks,
        "_storage_reaper_start_index",
        lambda count: next(cursor) % count,
    )
    monkeypatch.setattr(worker_tasks, "_s3_client", object)

    def reap(
        *_args: object,
        organization_id: str,
        batch_size: int,
        **_kwargs: object,
    ) -> tuple[SimpleNamespace, ...]:
        current_tick.append(organization_id)
        return tuple(SimpleNamespace(status=StorageReapStatus.deleted) for _ in range(batch_size))

    # Positional organization_id mirrors the production helper signature.
    def positional_reap(
        _factory: object,
        _client: object,
        _bucket: str,
        organization_id: str,
        *,
        batch_size: int,
    ) -> tuple[SimpleNamespace, ...]:
        return reap(organization_id=organization_id, batch_size=batch_size)

    monkeypatch.setattr(worker_tasks, "reap_storage_batch", positional_reap)
    for _ in range(4):
        current_tick = []
        counts = worker_tasks.reap_abandoned_storage.run(limit=25)
        assert counts["processed"] == 25
        calls_by_tick.append(tuple(current_tick))

    assert calls_by_tick == [
        tenants[:3],
        (tenants[1], tenants[2], tenants[3]),
        (tenants[2], tenants[3], tenants[0]),
        (tenants[3], tenants[0], tenants[1]),
    ]
    assert set().union(*(set(tick) for tick in calls_by_tick)) == set(tenants)
