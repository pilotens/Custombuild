from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import custombuild_worker.tasks as worker_tasks
import pytest
from app.models import (
    DesignStatus,
    DesignVersion,
    GenerationJob,
    JobStatus,
    Organization,
    OutboxEvent,
    Project,
    User,
)
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker


@pytest.mark.postgres
def test_worker_role_is_rls_scoped_and_schedulers_process_two_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    privileged_url = os.getenv("TENANT_GRAPH_DATABASE_URL")
    worker_url = os.getenv("WORKER_RLS_DATABASE_URL")
    if not privileged_url or not worker_url:
        pytest.skip(
            "Worker RLS probe requires TENANT_GRAPH_DATABASE_URL and "
            "WORKER_RLS_DATABASE_URL"
        )

    suffix = uuid.uuid4().hex[:10]
    organization_a = str(uuid.uuid4())
    organization_b = str(uuid.uuid4())
    user_a = str(uuid.uuid4())
    user_b = str(uuid.uuid4())
    project_a = str(uuid.uuid4())
    project_b = str(uuid.uuid4())
    version_a = str(uuid.uuid4())
    version_b = str(uuid.uuid4())
    job_a = str(uuid.uuid4())
    job_b = str(uuid.uuid4())
    privileged_engine = create_engine(privileged_url, pool_pre_ping=True)
    worker_engine = create_engine(worker_url, pool_pre_ping=True)
    privileged_factory = sessionmaker(bind=privileged_engine, expire_on_commit=False)
    worker_factory = sessionmaker(bind=worker_engine, expire_on_commit=False)
    now = datetime.now(UTC)

    try:
        with privileged_factory.begin() as session:
            session.add_all(
                [
                    Organization(
                        id=organization_a,
                        name="Worker RLS tenant A",
                        slug=f"worker-rls-a-{suffix}",
                    ),
                    Organization(
                        id=organization_b,
                        name="Worker RLS tenant B",
                        slug=f"worker-rls-b-{suffix}",
                    ),
                    User(
                        id=user_a,
                        oidc_sub=f"worker-rls-user-a-{suffix}",
                        email=f"worker-rls-a-{suffix}@example.test",
                        name="Worker RLS user A",
                    ),
                    User(
                        id=user_b,
                        oidc_sub=f"worker-rls-user-b-{suffix}",
                        email=f"worker-rls-b-{suffix}@example.test",
                        name="Worker RLS user B",
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    Project(
                        id=project_a,
                        organization_id=organization_a,
                        name="Worker RLS project A",
                    ),
                    Project(
                        id=project_b,
                        organization_id=organization_b,
                        name="Worker RLS project B",
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    DesignVersion(
                        id=version_a,
                        organization_id=organization_a,
                        project_id=project_a,
                        revision=1,
                        status=DesignStatus.design_validated,
                        design_hash="a" * 64,
                        context_hash="b" * 64,
                        spec_json={},
                        source_provenance_json={},
                        result_json={},
                        engine_version="test-engine",
                        template_version="bookcase@1.1.0",
                        template_id="shelving",
                        template_capability_fingerprint="c" * 64,
                        rule_version="test-rules",
                        created_by=user_a,
                    ),
                    DesignVersion(
                        id=version_b,
                        organization_id=organization_b,
                        project_id=project_b,
                        revision=1,
                        status=DesignStatus.design_validated,
                        design_hash="d" * 64,
                        context_hash="e" * 64,
                        spec_json={},
                        source_provenance_json={},
                        result_json={},
                        engine_version="test-engine",
                        template_version="bookcase@1.1.0",
                        template_id="shelving",
                        template_capability_fingerprint="f" * 64,
                        rule_version="test-rules",
                        created_by=user_b,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    GenerationJob(
                        id=job_a,
                        organization_id=organization_a,
                        design_version_id=version_a,
                        status=JobStatus.queued,
                        idempotency_key="1" * 64,
                        production_context_hash="2" * 64,
                        production_engine_context_json={"schema": "worker-rls-test"},
                        request_json={},
                        attempts=0,
                    ),
                    GenerationJob(
                        id=job_b,
                        organization_id=organization_b,
                        design_version_id=version_b,
                        status=JobStatus.queued,
                        idempotency_key="3" * 64,
                        production_context_hash="4" * 64,
                        production_engine_context_json={"schema": "worker-rls-test"},
                        request_json={},
                        attempts=0,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    OutboxEvent(
                        organization_id=organization_a,
                        event_key=f"worker-rls-valid-a-{suffix}",
                        topic="generation.requested",
                        payload_json={
                            "job_id": job_a,
                            "organization_id": organization_a,
                        },
                        created_at=now,
                    ),
                    OutboxEvent(
                        organization_id=organization_b,
                        event_key=f"worker-rls-valid-b-{suffix}",
                        topic="generation.requested",
                        payload_json={
                            "job_id": job_b,
                            "organization_id": organization_b,
                        },
                        created_at=now,
                    ),
                    OutboxEvent(
                        organization_id=organization_a,
                        event_key=f"worker-rls-forged-{suffix}",
                        topic="generation.requested",
                        payload_json={
                            "job_id": job_b,
                            "organization_id": organization_b,
                        },
                        created_at=now + timedelta(seconds=1),
                    ),
                ]
            )

        with worker_engine.begin() as connection:
            role = connection.execute(
                text(
                    "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole, "
                    "rolinherit, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            ).one()
            assert role == (
                "custombuild_worker",
                False,
                False,
                False,
                False,
                False,
                False,
            )
            organization_ids = connection.execute(
                text(
                    "SELECT id FROM organizations WHERE id IN (:a, :b) ORDER BY id"
                ),
                {"a": organization_a, "b": organization_b},
            ).scalars().all()
            assert organization_ids == sorted([organization_a, organization_b])
            assert connection.execute(
                text("SELECT id FROM generation_jobs WHERE id IN (:a, :b)"),
                {"a": job_a, "b": job_b},
            ).scalars().all() == []
            connection.execute(
                text("SELECT set_config('app.current_organization_id', :tenant, true)"),
                {"tenant": organization_a},
            )
            assert connection.execute(
                text("SELECT id FROM generation_jobs WHERE id IN (:a, :b) ORDER BY id"),
                {"a": job_a, "b": job_b},
            ).scalars().all() == [job_a]

        monkeypatch.setattr(worker_tasks, "SessionFactory", worker_factory)
        monkeypatch.setattr(
            worker_tasks,
            "_scheduler_start_index",
            lambda _tenant_count, *, cursor_key, scheduler_name: 0,
        )
        assert {organization_a, organization_b}.issubset(
            set(worker_tasks._organization_ids())
        )
        monkeypatch.setattr(
            worker_tasks,
            "_organization_ids",
            lambda: tuple(sorted([organization_a, organization_b])),
        )

        claim_a = worker_tasks._claim_job(job_a, organization_a)
        claim_b = worker_tasks._claim_job(job_b, organization_b)
        assert claim_a is not None
        assert claim_b is not None
        with privileged_factory.begin() as session:
            for job_id in (job_a, job_b):
                job = session.get(GenerationJob, job_id)
                assert job is not None
                job.lease_expires_at = now - timedelta(seconds=1)

        published: list[dict[str, Any]] = []
        monkeypatch.setattr(
            worker_tasks.celery_app,
            "send_task",
            lambda _name, **kwargs: published.append(kwargs),
        )
        assert worker_tasks.dispatch_outbox.run(limit=3) == 2
        assert worker_tasks.recover_stale_jobs.run(limit=2) == 2
        assert {item["kwargs"]["organization_id"] for item in published} == {
            organization_a,
            organization_b,
        }
        assert all(item["queue"] == worker_tasks.GENERATION_QUEUE for item in published)
        assert all(item["retry"] is False for item in published)

        with privileged_factory() as session:
            jobs = {
                job.id: job
                for job in session.scalars(
                    select(GenerationJob).where(
                        GenerationJob.id.in_([job_a, job_b])
                    )
                )
            }
            assert jobs[job_a].status == JobStatus.queued
            assert jobs[job_b].status == JobStatus.queued
            forged = session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.event_key == f"worker-rls-forged-{suffix}"
                )
            )
            assert forged is not None
            assert forged.dispatched_at is None
            assert forged.dead_lettered_at is not None
            assert forged.last_error is not None
            assert organization_a not in forged.last_error
            assert organization_b not in forged.last_error
    finally:
        with privileged_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b)"),
                {"a": organization_a, "b": organization_b},
            )
            connection.execute(
                text("DELETE FROM users WHERE id IN (:a, :b)"),
                {"a": user_a, "b": user_b},
            )
        worker_engine.dispose()
        privileged_engine.dispose()
