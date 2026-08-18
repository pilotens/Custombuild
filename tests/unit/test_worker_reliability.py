from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any

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
    OutboxEvent,
)
from celery.exceptions import SoftTimeLimitExceeded
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
    return factory


def _seed_generation_job(factory: sessionmaker[Session]) -> tuple[str, str]:
    organization_id = "22222222-2222-4222-8222-222222222222"
    version_id = "33333333-3333-4333-8333-333333333333"
    job_id = "55555555-5555-4555-8555-555555555555"
    with factory.begin() as session:
        session.add(
            DesignVersion(
                id=version_id,
                organization_id=organization_id,
                project_id="44444444-4444-4444-8444-444444444444",
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
        "bundle_object_key": "private/bundle.zip",
        "bundle_sha256": "a" * 64,
        "bundle_size_bytes": 100,
        "manifest_object_key": "private/manifest.json",
        "manifest_sha256": "b" * 64,
        "manifest_size_bytes": 200,
        "evidence_artifacts": [],
    }


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
    ) is False

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

    def exceed_deadline(_job: GenerationJob, _version: DesignVersion) -> dict[str, Any]:
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
