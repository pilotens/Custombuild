from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from typing import Any

import app.api as api_module
import app.storage as storage_module
import custombuild_worker.tasks as worker_tasks
import pytest
from app.db import Base
from app.models import (
    Artifact,
    AuditEvent,
    DesignStatus,
    DesignVersion,
    GenerationJob,
    JobStatus,
    Organization,
    OutboxEvent,
)
from celery.exceptions import SoftTimeLimitExceeded
from custombuild_manufacturing import MAX_ARTIFACT_BYTES, MAX_CORE_DOCUMENT_BYTES
from custombuild_manufacturing.readiness import ReadinessValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def worker_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(worker_tasks, "SessionFactory", factory)
    monkeypatch.setattr(
        worker_tasks,
        "_validate_completion_evidence",
        lambda _session, _organization_id, _job: None,
    )
    return factory


def _seed_generation_job(
    factory: sessionmaker[Session],
    *,
    organization_id: str = "22222222-2222-4222-8222-222222222222",
    version_id: str = "33333333-3333-4333-8333-333333333333",
    job_id: str = "55555555-5555-4555-8555-555555555555",
    project_id: str = "44444444-4444-4444-8444-444444444444",
) -> tuple[str, str]:
    with factory.begin() as session:
        session.add(
            Organization(
                id=organization_id,
                name=f"Worker tenant {organization_id[:8]}",
                slug=f"worker-{organization_id}",
            )
        )
        session.add(
            DesignVersion(
                id=version_id,
                organization_id=organization_id,
                project_id=project_id,
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
                created_by="66666666-6666-4666-8666-666666666666",
            )
        )
        session.add(
            GenerationJob(
                id=job_id,
                organization_id=organization_id,
                design_version_id=version_id,
                status=JobStatus.queued,
                idempotency_key="e" * 64,
                production_context_hash="f" * 64,
                production_engine_context_json={"schema": "test"},
                request_json={},
                attempts=0,
            )
        )
    return job_id, organization_id


def _generation_result() -> dict[str, Any]:
    return {
        "production_engine_context_hash": worker_tasks.sha256_hex(
            worker_tasks.canonical_json_bytes({"schema": "test"})
        ),
        "bundle_object_key": "private/bundle.zip",
        "bundle_sha256": "a" * 64,
        "bundle_size_bytes": 100,
        "manifest_object_key": "private/manifest.json",
        "manifest_sha256": "b" * 64,
        "manifest_size_bytes": 200,
        "evidence_artifacts": [],
    }


def test_completion_rejects_detached_engine_context_before_state_or_artifacts(
    worker_session_factory: sessionmaker[Session],
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    claim = worker_tasks._claim_job(job_id, organization_id)
    assert claim is not None
    job, version = claim
    token = job.lease_token
    assert token is not None
    detached = _generation_result()
    detached["production_engine_context_hash"] = "0" * 64

    with pytest.raises(
        worker_tasks.ProductionBlockedError,
        match="not bound to the persisted production engine context",
    ):
        worker_tasks._complete_job(
            job_id,
            organization_id,
            token,
            version,
            detached,
        )

    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.status == JobStatus.running
        assert stored.lease_token == token
        assert stored.result_json is None
        assert list(session.scalars(select(Artifact))) == []
        assert list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.entity_id == job_id,
                    AuditEvent.action == "generation.succeeded",
                )
            )
        ) == []


def test_worker_completion_gate_uses_worker_storage_and_build_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"completion":"verified"}'
    digest = hashlib.sha256(payload).hexdigest()
    build_identity = {
        "app_version": "1.4.0",
        "vcs_ref": "a" * 40,
        "build_date": "2026-08-28T00:00:00Z",
        "source_url": "https://github.com/pilotens/Custombuild",
        "source_manifest_sha256": "b" * 64,
        "dependency_lock_sha256": "c" * 64,
    }
    calls: list[dict[str, Any]] = []

    class WorkerS3Client:
        def head_object(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {
                "ContentLength": len(payload),
                "ContentType": "application/json",
                "Metadata": {"sha256": digest},
            }

    client = WorkerS3Client()
    runtime = SimpleNamespace(
        s3_bucket="production-artifacts",
        build_identity=build_identity,
    )

    def must_not_load_api_settings() -> None:
        raise AssertionError("worker completion loaded API-only settings")

    def canonical_gate(
        _session: Any,
        organization_id: str,
        _job: GenerationJob,
        **kwargs: Any,
    ) -> None:
        assert organization_id == "tenant-a"
        assert kwargs["build_identity"] == build_identity
        storage_module.verify_stored_object(
            storage_module.StoredObjectExpectation(
                object_key="tenant-a/evidence.json",
                sha256=digest,
                size_bytes=len(payload),
                content_type="application/json",
            ),
            stream_hash=False,
        )

    monkeypatch.setattr(worker_tasks, "WORKER_SETTINGS", runtime)
    monkeypatch.setattr(worker_tasks, "_s3_client", lambda: client)
    monkeypatch.setattr(api_module, "_require_review_evidence", canonical_gate)
    monkeypatch.setattr(storage_module, "get_settings", must_not_load_api_settings)

    worker_tasks._validate_completion_evidence(
        object(),
        "tenant-a",
        GenerationJob(),
    )

    assert calls == [{"Bucket": "production-artifacts", "Key": "tenant-a/evidence.json"}]


def test_replaced_lease_rejects_old_worker_completion_and_failure(
    worker_session_factory: sessionmaker[Session],
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    first_claim = worker_tasks._claim_job(job_id, organization_id)
    assert first_claim is not None
    first_job, first_version = first_claim
    first_token = first_job.lease_token
    assert first_token is not None
    assert first_job.lease_expires_at is not None
    assert first_job.started_at is not None
    assert first_job.lease_expires_at > first_job.started_at

    with worker_session_factory.begin() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        recovery = worker_tasks._recover_stale_job(
            stored,
            now=datetime.now(UTC) + worker_tasks.GENERATION_LEASE_TTL + timedelta(seconds=1),
        )
        assert recovery is not None
        session.add(recovery)
    second_claim = worker_tasks._claim_job(job_id, organization_id)
    assert second_claim is not None
    second_job, second_version = second_claim
    second_token = second_job.lease_token
    assert second_token is not None and second_token != first_token

    assert worker_tasks._complete_job(
        job_id,
        organization_id,
        first_token,
        first_version,
        _generation_result(),
    ) is False
    assert worker_tasks._record_failure(
        job_id,
        organization_id,
        first_token,
        RuntimeError("old worker must not win"),
        terminal=True,
    ) is None

    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.status == JobStatus.running
        assert stored.lease_token == second_token
        assert stored.result_json is None
        assert list(session.scalars(select(Artifact))) == []

    assert worker_tasks._complete_job(
        job_id,
        organization_id,
        second_token,
        second_version,
        _generation_result(),
    ) is True
    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.status == JobStatus.succeeded
        assert stored.lease_token is None
        assert stored.lease_expires_at is None
        assert len(list(session.scalars(select(Artifact)))) == 2
        success_audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.entity_id == job_id,
                    AuditEvent.action == "generation.succeeded",
                )
            )
        )
        assert len(success_audits) == 1


@pytest.mark.parametrize(
    "deadline_delta",
    (timedelta(0), -timedelta(seconds=1)),
    ids=("at-deadline", "after-deadline"),
)
def test_deadline_fences_owned_completion_before_artifacts_and_success_audit(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    deadline_delta: timedelta,
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    claim = worker_tasks._claim_job(job_id, organization_id)
    assert claim is not None
    job, version = claim
    token = job.lease_token
    assert token is not None
    completed_at = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    with worker_session_factory.begin() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        stored.deadline_at = completed_at + deadline_delta
        stored.lease_expires_at = completed_at + worker_tasks.GENERATION_LEASE_TTL
    monkeypatch.setattr(worker_tasks, "_utcnow", lambda: completed_at)

    assert (
        worker_tasks._complete_job(
            job_id,
            organization_id,
            token,
            version,
            _generation_result(),
        )
        is False
    )

    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.status == JobStatus.failed
        assert stored.lease_token is None
        assert stored.lease_expires_at is None
        assert stored.finished_at == completed_at.replace(tzinfo=None)
        assert stored.error == worker_tasks.GENERATION_DEADLINE_ERROR
        assert stored.result_json is None
        assert list(session.scalars(select(Artifact))) == []
        assert list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.entity_id == job_id,
                    AuditEvent.action == "generation.succeeded",
                )
            )
        ) == []


def test_completion_validation_runs_before_success_and_rolls_back_on_failure(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    claim = worker_tasks._claim_job(job_id, organization_id)
    assert claim is not None
    job, version = claim
    token = job.lease_token
    assert token is not None
    observed: list[tuple[JobStatus, bool, int]] = []

    def reject_completion(
        session: Session,
        observed_organization_id: str,
        staged_job: GenerationJob,
    ) -> None:
        observed.append(
            (
                staged_job.status,
                staged_job.result_json == _generation_result(),
                len(
                    list(
                        session.scalars(
                            select(Artifact).where(
                                Artifact.generation_job_id == staged_job.id,
                                Artifact.organization_id == observed_organization_id,
                            )
                        )
                    )
                ),
            )
        )
        raise worker_tasks.ProductionBlockedError("staged evidence rejected")

    monkeypatch.setattr(worker_tasks, "_validate_completion_evidence", reject_completion)

    with pytest.raises(worker_tasks.ProductionBlockedError, match="staged evidence rejected"):
        worker_tasks._complete_job(
            job_id,
            organization_id,
            token,
            version,
            _generation_result(),
        )

    assert observed == [(JobStatus.running, True, 2)]
    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.status == JobStatus.running
        assert stored.lease_token == token
        assert stored.result_json is None
        assert list(session.scalars(select(Artifact))) == []
        assert list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.entity_id == job_id,
                    AuditEvent.action == "generation.succeeded",
                )
            )
        ) == []


@pytest.mark.parametrize(
    ("location", "invalid_size"),
    (
        ("bundle_size_bytes", True),
        ("bundle_size_bytes", 0),
        ("bundle_size_bytes", MAX_ARTIFACT_BYTES + 1),
        ("manifest_size_bytes", MAX_CORE_DOCUMENT_BYTES + 1),
        ("evidence", False),
        ("evidence", MAX_CORE_DOCUMENT_BYTES + 1),
    ),
)
def test_completion_rejects_coerced_empty_or_oversize_artifact_claims(
    worker_session_factory: sessionmaker[Session],
    location: str,
    invalid_size: object,
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    claim = worker_tasks._claim_job(job_id, organization_id)
    assert claim is not None
    job, version = claim
    token = job.lease_token
    assert token is not None
    result = _generation_result()
    if location == "evidence":
        result["evidence_artifacts"] = [
            {
                "kind": "dfm_report",
                "object_key": "private/dfm-report.json",
                "sha256": "c" * 64,
                "size_bytes": invalid_size,
                "content_type": "application/json",
            }
        ]
    else:
        result[location] = invalid_size

    with pytest.raises(worker_tasks.ProductionBlockedError, match="inventory"):
        worker_tasks._complete_job(
            job_id,
            organization_id,
            token,
            version,
            result,
        )

    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.status == JobStatus.running
        assert stored.result_json is None
        assert list(session.scalars(select(Artifact))) == []


def test_lease_heartbeat_extends_only_the_current_tenant_owner(
    worker_session_factory: sessionmaker[Session],
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    claim = worker_tasks._claim_job(job_id, organization_id)
    assert claim is not None
    job, _version = claim
    token = job.lease_token
    assert token is not None

    renewed_at = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    assert worker_tasks._renew_job_lease(
        job_id,
        organization_id,
        token,
        now=renewed_at,
    ) is True
    assert worker_tasks._renew_job_lease(
        job_id,
        organization_id,
        "00000000-0000-4000-8000-000000000000",
        now=renewed_at,
    ) is False
    assert worker_tasks._renew_job_lease(
        job_id,
        "99999999-9999-4999-8999-999999999999",
        token,
        now=renewed_at,
    ) is False

    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.lease_expires_at is not None
        expires_at = stored.lease_expires_at.replace(tzinfo=UTC)
        assert expires_at == renewed_at + worker_tasks.GENERATION_LEASE_TTL


def test_every_generation_job_transaction_binds_the_requested_tenant(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    contexts: list[str] = []
    monkeypatch.setattr(
        worker_tasks,
        "set_tenant_context",
        lambda _session, observed_id: contexts.append(observed_id),
    )

    first_claim = worker_tasks._claim_job(job_id, organization_id)
    assert first_claim is not None
    first_job, _first_version = first_claim
    first_token = first_job.lease_token
    assert first_token is not None
    assert worker_tasks._renew_job_lease(job_id, organization_id, first_token)
    assert worker_tasks._record_failure(
        job_id,
        organization_id,
        first_token,
        RuntimeError("retryable"),
        terminal=False,
    )

    second_claim = worker_tasks._claim_job(job_id, organization_id)
    assert second_claim is not None
    second_job, second_version = second_claim
    second_token = second_job.lease_token
    assert second_token is not None
    assert worker_tasks._complete_job(
        job_id,
        organization_id,
        second_token,
        second_version,
        _generation_result(),
    )

    assert contexts == [organization_id] * 5


def test_background_heartbeat_stops_when_lease_ownership_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []
    lease_checked = Event()

    def lost_lease(job_id: str, organization_id: str, lease_token: str) -> bool:
        calls.append((job_id, organization_id, lease_token))
        lease_checked.set()
        return False

    monkeypatch.setattr(worker_tasks, "_renew_job_lease", lost_lease)
    with worker_tasks._maintain_generation_lease(
        "job-id",
        "organization-id",
        "lease-token",
        interval_seconds=0.001,
    ):
        assert lease_checked.wait(timeout=1.0)

    assert calls == [("job-id", "organization-id", "lease-token")]


def test_celery_generation_limits_are_bound_to_the_server_job_policy() -> None:
    assert int(worker_tasks.GENERATION_JOB_TIMEOUT.total_seconds()) == (
        worker_tasks.GENERATION_TASK_SOFT_TIME_LIMIT_SECONDS
    )
    assert worker_tasks.GENERATION_TASK_HARD_TIME_LIMIT_SECONDS == (
        worker_tasks.GENERATION_TASK_SOFT_TIME_LIMIT_SECONDS
        + 60
    )
    assert worker_tasks.celery_app.conf.task_soft_time_limit == (
        worker_tasks.GENERATION_TASK_SOFT_TIME_LIMIT_SECONDS
    )
    assert worker_tasks.celery_app.conf.task_time_limit == (
        worker_tasks.GENERATION_TASK_HARD_TIME_LIMIT_SECONDS
    )


def test_soft_time_limit_terminalizes_the_owned_job_without_retry(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)

    def exceed_deadline(
        _job: GenerationJob,
        _version: DesignVersion,
        **_kwargs: object,
    ) -> dict[str, Any]:
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(worker_tasks, "_generate", exceed_deadline)

    with pytest.raises(SoftTimeLimitExceeded):
        worker_tasks.generate_package.run(job_id=job_id, organization_id=organization_id)

    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.status == JobStatus.failed
        assert stored.lease_token is None
        assert stored.lease_expires_at is None
        assert stored.finished_at is not None
        assert stored.error == (
            "GenerationDeadlineExceeded: Generation task exceeded the server execution deadline"
        )


def test_readiness_validation_failure_is_terminal_without_retry(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    retry_calls: list[Exception] = []

    def reject_readiness(
        _job: GenerationJob,
        _version: DesignVersion,
        **_kwargs: object,
    ) -> dict[str, Any]:
        raise ReadinessValidationError("canonical readiness invalid")

    def unexpected_retry(*_args: object, **kwargs: Any) -> None:
        retry_calls.append(kwargs["exc"])
        raise AssertionError("Readiness validation must not be retried")

    monkeypatch.setattr(worker_tasks, "_generate", reject_readiness)
    monkeypatch.setattr(worker_tasks.generate_package, "retry", unexpected_retry)

    with pytest.raises(
        ReadinessValidationError,
        match="^canonical readiness invalid$",
    ):
        worker_tasks.generate_package.run(
            job_id=job_id,
            organization_id=organization_id,
        )

    assert retry_calls == []
    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.attempts == 1
        assert stored.status == JobStatus.failed
        assert stored.lease_token is None
        assert stored.lease_expires_at is None
        assert stored.finished_at is not None
        assert stored.error == ("ReadinessValidationError: canonical readiness invalid")


def test_runtime_error_remains_retryable_and_requeues_owned_job(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    retry_calls: list[Exception] = []

    class RetryRequested(Exception):
        pass

    def fail_transiently(
        _job: GenerationJob,
        _version: DesignVersion,
        **_kwargs: object,
    ) -> dict[str, Any]:
        raise RuntimeError("temporary dependency failure")

    def capture_retry(*_args: object, **kwargs: Any) -> None:
        retry_calls.append(kwargs["exc"])
        raise RetryRequested()

    monkeypatch.setattr(worker_tasks, "_generate", fail_transiently)
    monkeypatch.setattr(worker_tasks.generate_package, "retry", capture_retry)

    with pytest.raises(RetryRequested):
        worker_tasks.generate_package.run(
            job_id=job_id,
            organization_id=organization_id,
        )

    assert len(retry_calls) == 1
    assert isinstance(retry_calls[0], RuntimeError)
    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.attempts == 1
        assert stored.status == JobStatus.queued
        assert stored.lease_token is None
        assert stored.lease_expires_at is None
        assert stored.started_at is None
        assert stored.finished_at is None
        assert stored.error == "RuntimeError: temporary dependency failure"


def test_transient_failure_after_server_deadline_is_terminal_without_retry(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    claim_at = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    failure_at = claim_at + timedelta(seconds=2)
    with worker_session_factory.begin() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        stored.deadline_at = claim_at + timedelta(seconds=1)
    clock = iter((claim_at, failure_at))
    retry_calls: list[Exception] = []

    def fail_after_deadline(
        _job: GenerationJob,
        _version: DesignVersion,
        **_kwargs: object,
    ) -> dict[str, Any]:
        raise RuntimeError("late transient failure")

    def unexpected_retry(*_args: object, **kwargs: Any) -> None:
        retry_calls.append(kwargs["exc"])
        raise AssertionError("A deadline-expired job must not be retried")

    monkeypatch.setattr(worker_tasks, "_utcnow", lambda: next(clock))
    monkeypatch.setattr(worker_tasks, "_generate", fail_after_deadline)
    monkeypatch.setattr(worker_tasks.generate_package, "retry", unexpected_retry)

    with pytest.raises(RuntimeError, match="^late transient failure$"):
        worker_tasks.generate_package.run(
            job_id=job_id,
            organization_id=organization_id,
        )

    assert retry_calls == []
    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.attempts == 1
        assert stored.status == JobStatus.failed
        assert stored.lease_token is None
        assert stored.lease_expires_at is None
        assert stored.finished_at == failure_at.replace(tzinfo=None)
        assert stored.error == worker_tasks.GENERATION_DEADLINE_ERROR


def test_global_attempt_budget_terminalizes_fourth_transient_failure(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    with worker_session_factory.begin() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        stored.attempts = worker_tasks.MAX_GENERATION_ATTEMPTS - 1
    retry_calls: list[Exception] = []

    def fail_on_final_attempt(
        _job: GenerationJob,
        _version: DesignVersion,
        **_kwargs: object,
    ) -> dict[str, Any]:
        raise RuntimeError("fourth transient failure")

    def unexpected_retry(*_args: object, **kwargs: Any) -> None:
        retry_calls.append(kwargs["exc"])
        raise AssertionError("The global attempt budget must not start attempt five")

    monkeypatch.setattr(worker_tasks, "_generate", fail_on_final_attempt)
    monkeypatch.setattr(worker_tasks.generate_package, "retry", unexpected_retry)

    with pytest.raises(RuntimeError, match="^fourth transient failure$"):
        worker_tasks.generate_package.run(
            job_id=job_id,
            organization_id=organization_id,
        )

    assert retry_calls == []
    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.attempts == worker_tasks.MAX_GENERATION_ATTEMPTS
        assert stored.status == JobStatus.failed
        assert stored.lease_token is None
        assert stored.lease_expires_at is None
        assert stored.finished_at is not None
        assert stored.error == "RuntimeError: fourth transient failure"


def test_queued_job_at_global_attempt_budget_is_not_claimed_again(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    with worker_session_factory.begin() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        stored.attempts = worker_tasks.MAX_GENERATION_ATTEMPTS

    def unexpected_generate(
        _job: GenerationJob,
        _version: DesignVersion,
    ) -> dict[str, Any]:
        raise AssertionError("An exhausted job must never enter generation")

    monkeypatch.setattr(worker_tasks, "_generate", unexpected_generate)

    result = worker_tasks.generate_package.run(
        job_id=job_id,
        organization_id=organization_id,
    )

    assert result == {"job_id": job_id, "state": "already_running_or_complete"}
    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.attempts == worker_tasks.MAX_GENERATION_ATTEMPTS
        assert stored.status == JobStatus.failed
        assert stored.lease_token is None
        assert stored.lease_expires_at is None
        assert stored.finished_at is not None
        assert stored.error == (
            "Generation job exhausted the maximum of 4 attempts"
        )


def test_recovery_task_ignores_live_lease_then_requeues_expired_lease(
    worker_session_factory: sessionmaker[Session],
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    claim = worker_tasks._claim_job(job_id, organization_id)
    assert claim is not None

    assert worker_tasks.recover_stale_jobs.run() == 0
    with worker_session_factory.begin() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.status == JobStatus.running
        stored.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    assert worker_tasks.recover_stale_jobs.run() == 1
    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.status == JobStatus.queued
        assert stored.lease_token is None
        assert stored.lease_expires_at is None
        recovery_events = list(
            session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_key == f"generation-recovery:{job_id}:1"
                )
            )
        )
        assert len(recovery_events) == 1


def test_recovery_task_deadline_overrides_a_still_live_lease(
    worker_session_factory: sessionmaker[Session],
) -> None:
    job_id, organization_id = _seed_generation_job(worker_session_factory)
    claim = worker_tasks._claim_job(job_id, organization_id)
    assert claim is not None
    now = datetime.now(UTC)
    with worker_session_factory.begin() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        stored.deadline_at = now - timedelta(seconds=1)
        stored.lease_expires_at = now + worker_tasks.GENERATION_LEASE_TTL

    assert worker_tasks.recover_stale_jobs.run() == 1

    with worker_session_factory() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None
        assert stored.status == JobStatus.failed
        assert stored.lease_token is None
        assert stored.lease_expires_at is None
        assert stored.finished_at is not None
        assert stored.error == worker_tasks.GENERATION_DEADLINE_ERROR
        recovery_events = list(
            session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_key.like(f"generation-recovery:{job_id}:%")
                )
            )
        )
        assert recovery_events == []


def test_poison_outbox_event_is_dead_lettered_without_blocking_the_next_event(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    job_id = "11111111-1111-4111-8111-111111111111"
    organization_id = "22222222-2222-4222-8222-222222222222"
    with worker_session_factory.begin() as session:
        session.add(
            Organization(
                id=organization_id,
                name="Outbox tenant",
                slug="outbox-poison-tenant",
            )
        )
        session.add_all(
            [
                OutboxEvent(
                    organization_id=organization_id,
                    event_key="poison-topic",
                    topic="unknown.secret.topic",
                    payload_json={"secret": "must-not-leak"},
                    created_at=now,
                ),
                OutboxEvent(
                    organization_id=organization_id,
                    event_key="valid-after-poison",
                    topic="generation.requested",
                    payload_json={
                        "job_id": job_id,
                        "organization_id": organization_id,
                    },
                    created_at=now + timedelta(seconds=1),
                ),
            ]
        )
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(
        worker_tasks.celery_app,
        "send_task",
        lambda _name, **kwargs: published.append(kwargs),
    )

    assert worker_tasks.dispatch_outbox.run(limit=10) == 1

    with worker_session_factory() as session:
        poison = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_key == "poison-topic")
        )
        valid = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_key == "valid-after-poison")
        )
        assert poison is not None and poison.dead_lettered_at is not None
        assert poison.dispatched_at is None
        assert poison.last_error == "Unsupported outbox topic; event was dead-lettered."
        assert "must-not-leak" not in poison.last_error
        assert valid is not None and valid.dispatched_at is not None
        assert valid.dead_lettered_at is None
    assert published == [
        {
            "kwargs": {
                "job_id": job_id,
                "organization_id": organization_id,
            }
        }
    ]


def test_transient_outbox_failure_is_bounded_and_sanitized(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = "22222222-2222-4222-8222-222222222222"
    with worker_session_factory.begin() as session:
        session.add(
            Organization(
                id=organization_id,
                name="Transient outbox tenant",
                slug="outbox-transient-tenant",
            )
        )
        session.add(
            OutboxEvent(
                organization_id=organization_id,
                event_key="bounded-transient",
                topic="generation.requested",
                payload_json={
                    "job_id": "11111111-1111-4111-8111-111111111111",
                    "organization_id": organization_id,
                    "secret": "payload-secret",
                },
            )
        )
    attempts = 0

    def fail_publish(*_args: object, **_kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("broker-secret-password")

    monkeypatch.setattr(worker_tasks.celery_app, "send_task", fail_publish)
    for _ in range(worker_tasks.MAX_OUTBOX_PUBLISH_ATTEMPTS + 1):
        worker_tasks.dispatch_outbox.run(limit=10)

    with worker_session_factory() as session:
        event = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_key == "bounded-transient")
        )
        assert event is not None
        assert event.attempts == worker_tasks.MAX_OUTBOX_PUBLISH_ATTEMPTS
        assert event.dead_lettered_at is not None
        assert event.dispatched_at is None
        assert event.last_error is not None
        assert "broker-secret-password" not in event.last_error
        assert "payload-secret" not in event.last_error
    assert attempts == worker_tasks.MAX_OUTBOX_PUBLISH_ATTEMPTS


def test_outbox_dispatch_is_tenant_local_payload_bound_and_globally_limited(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_a = "11111111-1111-4111-8111-111111111111"
    organization_b = "22222222-2222-4222-8222-222222222222"
    job_a = "33333333-3333-4333-8333-333333333333"
    job_b = "44444444-4444-4444-8444-444444444444"
    now = datetime.now(UTC)
    with worker_session_factory.begin() as session:
        session.add_all(
            [
                Organization(id=organization_a, name="Tenant A", slug="dispatcher-a"),
                Organization(id=organization_b, name="Tenant B", slug="dispatcher-b"),
                OutboxEvent(
                    organization_id=organization_a,
                    event_key="tenant-a-valid",
                    topic="generation.requested",
                    payload_json={
                        "job_id": job_a,
                        "organization_id": organization_a,
                    },
                    created_at=now,
                ),
                OutboxEvent(
                    organization_id=organization_a,
                    event_key="tenant-a-forged-payload",
                    topic="generation.requested",
                    payload_json={
                        "job_id": job_b,
                        "organization_id": organization_b,
                        "secret": "must-not-leak",
                    },
                    created_at=now + timedelta(seconds=1),
                ),
                OutboxEvent(
                    organization_id=organization_b,
                    event_key="tenant-b-valid",
                    topic="generation.requested",
                    payload_json={
                        "job_id": job_b,
                        "organization_id": organization_b,
                    },
                    created_at=now,
                ),
            ]
        )

    contexts: list[str] = []
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(
        worker_tasks,
        "set_tenant_context",
        lambda _session, organization_id: contexts.append(organization_id),
    )
    monkeypatch.setattr(
        worker_tasks.celery_app,
        "send_task",
        lambda _name, **kwargs: published.append(kwargs),
    )

    assert worker_tasks.dispatch_outbox.run(limit=1) == 1
    assert published == [
        {"kwargs": {"job_id": job_a, "organization_id": organization_a}}
    ]
    assert worker_tasks.dispatch_outbox.run(limit=10) == 1

    assert contexts == [organization_a, organization_a, organization_b]
    assert published == [
        {"kwargs": {"job_id": job_a, "organization_id": organization_a}},
        {"kwargs": {"job_id": job_b, "organization_id": organization_b}},
    ]
    with worker_session_factory() as session:
        forged = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_key == "tenant-a-forged-payload"
            )
        )
        assert forged is not None
        assert forged.dispatched_at is None
        assert forged.dead_lettered_at is not None
        assert forged.last_error is not None
        assert "must-not-leak" not in forged.last_error
        assert organization_a not in forged.last_error
        assert organization_b not in forged.last_error


def test_stale_recovery_runs_one_tenant_transaction_per_organization(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_a = "11111111-1111-4111-8111-111111111111"
    organization_b = "22222222-2222-4222-8222-222222222222"
    job_a, _ = _seed_generation_job(
        worker_session_factory,
        organization_id=organization_a,
        version_id="33333333-3333-4333-8333-333333333333",
        job_id="55555555-5555-4555-8555-555555555555",
        project_id="77777777-7777-4777-8777-777777777777",
    )
    job_b, _ = _seed_generation_job(
        worker_session_factory,
        organization_id=organization_b,
        version_id="44444444-4444-4444-8444-444444444444",
        job_id="66666666-6666-4666-8666-666666666666",
        project_id="88888888-8888-4888-8888-888888888888",
    )
    assert worker_tasks._claim_job(job_a, organization_a) is not None
    assert worker_tasks._claim_job(job_b, organization_b) is not None
    with worker_session_factory.begin() as session:
        for job_id in (job_a, job_b):
            job = session.get(GenerationJob, job_id)
            assert job is not None
            job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    tenant_sessions: list[tuple[str, Session]] = []
    monkeypatch.setattr(
        worker_tasks,
        "set_tenant_context",
        lambda session, organization_id: tenant_sessions.append(
            (organization_id, session)
        ),
    )

    assert worker_tasks.recover_stale_jobs.run(limit=2) == 2
    assert [item[0] for item in tenant_sessions] == [organization_a, organization_b]
    assert tenant_sessions[0][1] is not tenant_sessions[1][1]
    with worker_session_factory() as session:
        jobs = {
            job.id: job
            for job in session.scalars(
                select(GenerationJob).where(GenerationJob.id.in_([job_a, job_b]))
            )
        }
        assert jobs[job_a].status == JobStatus.queued
        assert jobs[job_b].status == JobStatus.queued
        recovery_events = list(
            session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.event_key.like("generation-recovery:%"))
                .order_by(OutboxEvent.organization_id)
            )
        )
        assert [event.organization_id for event in recovery_events] == [
            organization_a,
            organization_b,
        ]


def test_outbox_tenant_rollback_does_not_block_other_tenants(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_a = "11111111-1111-4111-8111-111111111111"
    organization_b = "22222222-2222-4222-8222-222222222222"
    job_a = "33333333-3333-4333-8333-333333333333"
    job_b = "44444444-4444-4444-8444-444444444444"
    with worker_session_factory.begin() as session:
        session.add_all(
            [
                Organization(id=organization_a, name="Tenant A", slug="rollback-a"),
                Organization(id=organization_b, name="Tenant B", slug="rollback-b"),
                OutboxEvent(
                    organization_id=organization_a,
                    event_key="rollback-tenant-a",
                    topic="generation.requested",
                    payload_json={
                        "job_id": job_a,
                        "organization_id": organization_a,
                    },
                ),
                OutboxEvent(
                    organization_id=organization_b,
                    event_key="commit-tenant-b",
                    topic="generation.requested",
                    payload_json={
                        "job_id": job_b,
                        "organization_id": organization_b,
                    },
                ),
            ]
        )

    original_dispatch = worker_tasks._dispatch_tenant_outbox_events

    def fail_tenant_a(events: list[OutboxEvent], organization_id: str) -> int:
        if organization_id == organization_a:
            events[0].last_error = "must roll back"
            raise RuntimeError("isolated tenant failure")
        return original_dispatch(events, organization_id)

    published: list[dict[str, Any]] = []
    monkeypatch.setattr(worker_tasks, "_dispatch_tenant_outbox_events", fail_tenant_a)
    monkeypatch.setattr(
        worker_tasks.celery_app,
        "send_task",
        lambda _name, **kwargs: published.append(kwargs),
    )

    assert worker_tasks.dispatch_outbox.run(limit=1) == 1
    assert published == [
        {"kwargs": {"job_id": job_b, "organization_id": organization_b}}
    ]
    with worker_session_factory() as session:
        event_a = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_key == "rollback-tenant-a")
        )
        event_b = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_key == "commit-tenant-b")
        )
        assert event_a is not None
        assert event_a.last_error is None
        assert event_a.dispatched_at is None
        assert event_b is not None
        assert event_b.dispatched_at is not None


def test_recovery_tenant_rollback_does_not_block_other_tenants(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_a = "11111111-1111-4111-8111-111111111111"
    organization_b = "22222222-2222-4222-8222-222222222222"
    job_a, _ = _seed_generation_job(
        worker_session_factory,
        organization_id=organization_a,
        version_id="33333333-3333-4333-8333-333333333333",
        job_id="55555555-5555-4555-8555-555555555555",
        project_id="77777777-7777-4777-8777-777777777777",
    )
    job_b, _ = _seed_generation_job(
        worker_session_factory,
        organization_id=organization_b,
        version_id="44444444-4444-4444-8444-444444444444",
        job_id="66666666-6666-4666-8666-666666666666",
        project_id="88888888-8888-4888-8888-888888888888",
    )
    assert worker_tasks._claim_job(job_a, organization_a) is not None
    assert worker_tasks._claim_job(job_b, organization_b) is not None
    with worker_session_factory.begin() as session:
        for job_id in (job_a, job_b):
            job = session.get(GenerationJob, job_id)
            assert job is not None
            job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    original_recovery = worker_tasks._recover_stale_job

    def fail_tenant_a(job: GenerationJob, *, now: datetime) -> OutboxEvent | None:
        if job.organization_id == organization_a:
            job.error = "must roll back"
            raise RuntimeError("isolated tenant failure")
        return original_recovery(job, now=now)

    monkeypatch.setattr(worker_tasks, "_recover_stale_job", fail_tenant_a)

    assert worker_tasks.recover_stale_jobs.run(limit=1) == 1
    with worker_session_factory() as session:
        stored_a = session.get(GenerationJob, job_a)
        stored_b = session.get(GenerationJob, job_b)
        assert stored_a is not None
        assert stored_a.status == JobStatus.running
        assert stored_a.error is None
        assert stored_b is not None
        assert stored_b.status == JobStatus.queued
        events = list(session.scalars(select(OutboxEvent)))
        assert [event.organization_id for event in events] == [organization_b]


@pytest.mark.parametrize("invalid_limit", (0, -1, True, 1.5, "1", None))
def test_scheduler_tasks_reject_invalid_global_limits(
    invalid_limit: object,
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        worker_tasks.dispatch_outbox.run(limit=invalid_limit)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive integer"):
        worker_tasks.recover_stale_jobs.run(limit=invalid_limit)  # type: ignore[arg-type]
