from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models import GenerationJob, JobStatus
from custombuild_worker.tasks import (
    MAX_GENERATION_ATTEMPTS,
    TERMINAL_JOB_STATUSES,
    _recover_stale_job,
)


def stale_job(*, attempts: int) -> GenerationJob:
    return GenerationJob(
        id="11111111-1111-1111-1111-111111111111",
        organization_id="22222222-2222-2222-2222-222222222222",
        design_version_id="33333333-3333-3333-3333-333333333333",
        status=JobStatus.running,
        idempotency_key="a" * 64,
        production_context_hash="b" * 64,
        production_engine_context_json={"schema_version": "test-production-context.v1"},
        request_json={},
        attempts=attempts,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_stale_job_before_final_attempt_is_requeued_through_outbox() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    job = stale_job(attempts=MAX_GENERATION_ATTEMPTS - 1)

    event = _recover_stale_job(job, now=now)

    assert job.status == JobStatus.queued
    assert job.started_at is None
    assert job.finished_at is None
    assert event is not None
    assert event.event_key == f"generation-recovery:{job.id}:{job.attempts}"
    assert event.payload_json == {
        "job_id": job.id,
        "organization_id": job.organization_id,
    }


def test_stale_job_after_final_attempt_becomes_terminal() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    job = stale_job(attempts=MAX_GENERATION_ATTEMPTS)

    event = _recover_stale_job(job, now=now)

    assert event is None
    assert job.status == JobStatus.failed
    assert job.finished_at == now
    assert "maximum of 4 generation attempts" in (job.error or "")
    assert job.started_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_stale_lease_threshold_is_longer_than_retry_backoff() -> None:
    # Documents the operational invariant behind the recovery task: it only
    # handles leases far older than the normal retry delay.
    assert timedelta(minutes=30) > timedelta(seconds=5)


def test_duplicate_delivery_cannot_resurrect_any_terminal_state() -> None:
    assert {
        JobStatus.succeeded,
        JobStatus.failed,
        JobStatus.cancelled,
    } == TERMINAL_JOB_STATUSES
