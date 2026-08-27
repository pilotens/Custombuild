from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.api import _expire_generation_job_if_overdue
from app.models import GenerationJob, JobStatus


def generation_job(status: JobStatus, *, deadline_at: datetime) -> GenerationJob:
    return GenerationJob(
        id="11111111-1111-4111-8111-111111111111",
        organization_id="22222222-2222-4222-8222-222222222222",
        design_version_id="33333333-3333-4333-8333-333333333333",
        status=status,
        idempotency_key="a" * 64,
        production_context_hash="b" * 64,
        production_engine_context_json={"schema": "test"},
        request_json={},
        attempts=1,
        lease_token=str(uuid4()),
        lease_expires_at=deadline_at + timedelta(minutes=1),
        deadline_at=deadline_at,
    )


def test_overdue_active_job_becomes_terminal_and_retryable() -> None:
    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    job = generation_job(JobStatus.running, deadline_at=now)

    assert _expire_generation_job_if_overdue(job, now=now) is True

    assert job.status == JobStatus.failed
    assert job.lease_token is None
    assert job.lease_expires_at is None
    assert job.finished_at == now
    assert "server deadline of 120 minutes" in (job.error or "")


def test_live_or_terminal_job_is_not_changed_by_deadline_check() -> None:
    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    live = generation_job(JobStatus.queued, deadline_at=now + timedelta(seconds=1))
    succeeded = generation_job(JobStatus.succeeded, deadline_at=now)

    assert _expire_generation_job_if_overdue(live, now=now) is False
    assert live.status == JobStatus.queued
    assert _expire_generation_job_if_overdue(succeeded, now=now) is False
    assert succeeded.status == JobStatus.succeeded
