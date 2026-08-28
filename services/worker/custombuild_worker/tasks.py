from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4, uuid5

import boto3
from app.db import set_tenant_context
from app.job_policy import (
    GENERATION_HEARTBEAT_INTERVAL_SECONDS,
    GENERATION_JOB_TIMEOUT,
    GENERATION_LEASE_TTL,
    GENERATION_RECOVERY_INTERVAL_SECONDS,
    GENERATION_TASK_HARD_TIME_LIMIT_SECONDS,
    GENERATION_TASK_SOFT_TIME_LIMIT_SECONDS,
    LEGACY_STALE_LEASE_THRESHOLD,
)
from app.models import (
    Artifact,
    AuditEvent,
    DesignVersion,
    GenerationJob,
    JobStatus,
    Organization,
    OutboxEvent,
)
from app.storage import (
    ArtifactIntegrityError,
    ArtifactStorageUnavailableError,
    storage_runtime,
    store_create_once_object,
)
from app.storage_quota import (
    StorageClaimConflict,
    StorageObjectClaim,
    StorageQuotaExceeded,
    StorageQuotaInvariantError,
    StorageReservationBusy,
    commit_storage_batch_in_transaction,
    renew_storage_batch_lease,
    reserve_storage_batch,
)
from app.storage_reaper import StorageReapStatus, reap_storage_batch
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from custombuild_domain import (
    BOOKCASE_JOINT_SUPPORT_VERSION,
    BookcaseDesignSpec,
    TemplateCapability,
    TemplateCapabilityError,
    build_bookcase,
    require_template_for_revision,
    resolve_template_capability,
)
from custombuild_manufacturing import (
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
    GENERATION_PLAN_ARTIFACT_PATH,
    GENERATION_PLAN_ARTIFACT_ROLE,
    MAX_EVIDENCE_ARTIFACTS,
    MAX_EVIDENCE_TOTAL_BYTES,
    ArtifactFile,
    CAMStageStatus,
    ManifestContext,
    ProductionBlockedError,
    StockSheet,
    build_production_bundle,
    canonical_json_bytes,
    sha256_hex,
    valid_artifact_size,
)
from custombuild_manufacturing.production_context import (
    ProductionContextError,
    ResolvedProductionComponents,
    assert_frozen_design_versions,
    assert_job_matches_frozen_revision_context,
    contexts_equal,
    generation_context_hash,
    resolve_production_components,
)
from custombuild_manufacturing.readiness import ReadinessValidationError
from custombuild_rules import RuleStatus, evaluate_design
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import and_, create_engine, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .config import get_worker_settings
from .documents import (
    assembly_manual_pdf,
    assembly_readiness_json,
    bom_pdf,
    hardware_csv,
    labels_pdf,
    qa_protocol_pdf,
    validation_report_pdf,
)

WORKER_SETTINGS = get_worker_settings()
logger = logging.getLogger(__name__)
REDIS_URL = WORKER_SETTINGS.redis_url
DATABASE_URL = WORKER_SETTINGS.database_url
IMMEDIATE_TERMINAL_GENERATION_ERRORS = (
    ProductionBlockedError,
    ReadinessValidationError,
)
_EVIDENCE_ARTIFACT_CONTRACTS: Mapping[str, tuple[str, str, str]] = {
    "validation/dfm-report.json": (
        "dfm_report",
        "application/json",
        "DFM_VALIDATION_REPORT",
    ),
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH: (
        "design_review_package_status",
        "application/json",
        DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
    ),
    "validation/stock-selection.json": (
        "stock_selection",
        "application/json",
        "STOCK_SELECTION_SNAPSHOT",
    ),
    GENERATION_PLAN_ARTIFACT_PATH: (
        "generation_plan",
        "application/json",
        GENERATION_PLAN_ARTIFACT_ROLE,
    ),
    "cam/operations.json": (
        "operations",
        "application/json",
        "MACHINE_NEUTRAL_OPERATIONS",
    ),
    "cam/validation-backplot.svg": (
        "validation_backplot",
        "image/svg+xml",
        "VALIDATION_BACKPLOT",
    ),
    "model/design.glb": ("design_glb", "model/gltf-binary", "WEB_PREVIEW_GLB"),
    "model/design.fcstd": (
        "design_fcstd",
        "application/vnd.freecad",
        "NON_AUTHORITATIVE_FREECAD_PROJECT",
    ),
    "validation/cad-interchange-status.json": (
        "cad_interchange_status",
        "application/json",
        "CAD_INTERCHANGE_STATUS",
    ),
    "validation/source-provenance.json": (
        "source_provenance",
        "application/json",
        "SOURCE_PROVENANCE",
    ),
    "validation/workshop-readiness.json": (
        "workshop_readiness",
        "application/json",
        "WORKSHOP_READINESS_REPORT",
    ),
    "assembly/assembly-readiness.json": (
        "assembly_readiness",
        "application/json",
        "ASSEMBLY_READINESS",
    ),
}
_SETUP_EVIDENCE_PATH = re.compile(r"cam/setups/[A-Za-z0-9][A-Za-z0-9._-]*\.svg")
celery_app = Celery("custombuild-worker", broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=GENERATION_TASK_SOFT_TIME_LIMIT_SECONDS,
    task_time_limit=GENERATION_TASK_HARD_TIME_LIMIT_SECONDS,
    beat_schedule={
        "dispatch-transactional-outbox": {
            "task": "custombuild.dispatch_outbox",
            "schedule": 2.0,
        },
        "recover-stale-generation-leases": {
            "task": "custombuild.recover_stale_jobs",
            "schedule": GENERATION_RECOVERY_INTERVAL_SECONDS,
        },
        "reap-abandoned-storage": {
            "task": "custombuild.reap_abandoned_storage",
            "schedule": 60.0,
        },
    },
)

_engine_connect_args: dict[str, object]
if DATABASE_URL.startswith("sqlite"):
    _engine_connect_args = {"check_same_thread": False}
else:
    _engine_connect_args = {
        "options": (
            "-c statement_timeout="
            f"{WORKER_SETTINGS.database_statement_timeout_seconds * 1000} "
            "-c lock_timeout="
            f"{WORKER_SETTINGS.database_lock_timeout_seconds * 1000}"
        )
    }

_engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args=_engine_connect_args,
)
SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
MAX_GENERATION_ATTEMPTS = 4
MAX_OUTBOX_PUBLISH_ATTEMPTS = 5
DEFAULT_SCHEDULER_BATCH_LIMIT = 50
DEFAULT_STORAGE_REAPER_BATCH_LIMIT = 25
STORAGE_REAPER_TENANT_BATCH_LIMIT = 10
STORAGE_REAPER_CURSOR_KEY = "custombuild:scheduler:storage-reaper:tenant-cursor:v1"
TERMINAL_JOB_STATUSES = frozenset({JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled})
GENERATION_DEADLINE_ERROR = "Generation job exceeded the server deadline of 120 minutes"
S3_CONNECT_TIMEOUT_SECONDS = 3
S3_READ_TIMEOUT_SECONDS = 30
S3_TOTAL_MAX_ATTEMPTS = 2
S3_STALLED_REQUEST_BUDGET_SECONDS = (
    S3_CONNECT_TIMEOUT_SECONDS + S3_READ_TIMEOUT_SECONDS
) * S3_TOTAL_MAX_ATTEMPTS


class GenerationDeadlineExceeded(RuntimeError):
    """The worker reached the server-owned bounded execution window."""


class GenerationLeaseOwnershipLost(RuntimeError):
    """The worker can no longer prove ownership of its generation side effects."""


class GenerationStorageReservationBusy(RuntimeError):
    """A prior attempt still owns this job's exact immutable storage batch."""

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class _GenerationLeaseGuard:
    """Share fail-closed job/quota lease state with the heartbeat thread."""

    def __init__(self, organization_id: str, lease_token: str) -> None:
        self.organization_id = organization_id
        self.lease_token = lease_token
        self._claims: tuple[StorageObjectClaim, ...] = ()
        self._failure: Exception | None = None
        self._lock = Lock()

    def bind_storage_claims(self, claims: tuple[StorageObjectClaim, ...]) -> None:
        with self._lock:
            if self._failure is not None:
                raise GenerationLeaseOwnershipLost(str(self._failure)) from self._failure
            if self._claims and self._claims != claims:
                raise RuntimeError("generation storage lease cannot change its claim batch")
            self._claims = claims

    def storage_claims(self) -> tuple[StorageObjectClaim, ...]:
        with self._lock:
            return self._claims

    def fail(self, exc: Exception) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = exc

    def check(self) -> None:
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise GenerationLeaseOwnershipLost(
                "generation or storage reservation lease ownership was lost"
            ) from failure


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _database_time(session: Session, override: datetime | None = None) -> datetime:
    """Use PostgreSQL's clock for lease decisions shared across replicas."""

    if session.get_bind().dialect.name == "postgresql":
        value = session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):
            raise RuntimeError("database did not return a canonical lease timestamp")
    else:
        value = override or _utcnow()
    return _as_utc(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _lease_is_live(job: GenerationJob, *, now: datetime) -> bool:
    expires_at = job.lease_expires_at
    return expires_at is not None and _as_utc(expires_at) > _as_utc(now)


def _deadline_is_expired(job: GenerationJob, *, now: datetime) -> bool:
    deadline_at = job.deadline_at
    return deadline_at is not None and _as_utc(deadline_at) <= _as_utc(now)


def _terminalize_deadline(job: GenerationJob, *, now: datetime) -> None:
    job.status = JobStatus.failed
    job.lease_token = None
    job.lease_expires_at = None
    job.error = GENERATION_DEADLINE_ERROR
    job.finished_at = now


def _terminalize_attempt_budget(job: GenerationJob, *, now: datetime) -> None:
    job.status = JobStatus.failed
    job.lease_token = None
    job.lease_expires_at = None
    job.error = f"Generation job exhausted the maximum of {MAX_GENERATION_ATTEMPTS} attempts"
    job.finished_at = now


def _valid_event_identifier(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def _scheduler_limit(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("scheduler limit must be a positive integer")
    return value


def _organization_ids() -> tuple[str, ...]:
    """Enumerate only the non-RLS organization root table.

    This transaction must never inspect tenant-owned rows. Every subsequent
    outbox, recovery or generation query uses a fresh tenant-local transaction.
    """

    with SessionFactory.begin() as session:
        organization_ids = tuple(session.scalars(select(Organization.id).order_by(Organization.id)))
    if any(not _valid_event_identifier(item) for item in organization_ids):
        raise RuntimeError("organization registry contains a non-canonical identifier")
    return organization_ids


def _storage_reaper_start_index(tenant_count: int) -> int:
    """Advance one durable Redis cursor so bounded scans cannot starve tenants."""

    if tenant_count <= 0:
        return 0
    client: Redis = Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry_on_timeout=False,
    )
    try:
        cursor = client.incr(STORAGE_REAPER_CURSOR_KEY)
    except RedisError as exc:
        raise RuntimeError("storage reaper fairness cursor is unavailable") from exc
    finally:
        client.close()
    if type(cursor) is not int or cursor < 1:
        raise RuntimeError("storage reaper fairness cursor is non-canonical")
    return (cursor - 1) % tenant_count


def _rotated_tenant_ids(
    organization_ids: tuple[str, ...],
    start_index: int,
) -> tuple[str, ...]:
    if not organization_ids:
        return ()
    if type(start_index) is not int or not 0 <= start_index < len(organization_ids):
        raise RuntimeError("storage reaper fairness cursor is outside the tenant registry")
    return organization_ids[start_index:] + organization_ids[:start_index]


@contextmanager
def _tenant_transaction(organization_id: str) -> Iterator[Session]:
    """Open one transaction whose PostgreSQL RLS context is the exact tenant."""

    if not _valid_event_identifier(organization_id):
        raise ValueError("organization_id must be a canonical UUID")
    with SessionFactory.begin() as session:
        set_tenant_context(session, organization_id)
        yield session


def _dead_letter(event: OutboxEvent, reason: str, *, increment_attempt: bool = True) -> None:
    if increment_attempt:
        event.attempts += 1
    event.dead_lettered_at = _utcnow()
    event.last_error = reason[:500]


@celery_app.task(name="custombuild.dispatch_outbox")  # type: ignore[misc]
def dispatch_outbox(limit: int = 50) -> int:
    """Publish committed outbox events. Duplicate delivery is safe by job identity."""

    global_limit = _scheduler_limit(limit)
    dispatched = 0
    handled = 0
    for tenant_id in _organization_ids():
        remaining = global_limit - handled
        if remaining <= 0:
            break
        tenant_handled = 0
        tenant_dispatched = 0
        try:
            with _tenant_transaction(tenant_id) as session:
                events = list(
                    session.scalars(
                        select(OutboxEvent)
                        .where(
                            OutboxEvent.organization_id == tenant_id,
                            OutboxEvent.dispatched_at.is_(None),
                            OutboxEvent.dead_lettered_at.is_(None),
                        )
                        .order_by(OutboxEvent.created_at, OutboxEvent.id)
                        .with_for_update(skip_locked=True)
                        .limit(remaining)
                    )
                )
                tenant_handled = len(events)
                tenant_dispatched = _dispatch_tenant_outbox_events(events, tenant_id)
        except Exception as exc:
            logger.error(
                "Tenant outbox transaction rolled back (%s); continuing with other tenants.",
                type(exc).__name__,
            )
            continue
        handled += tenant_handled
        dispatched += tenant_dispatched
    return dispatched


def _dispatch_tenant_outbox_events(
    events: list[OutboxEvent],
    organization_id: str,
) -> int:
    dispatched = 0
    for event in events:
        if event.organization_id != organization_id:
            raise RuntimeError("tenant-local outbox query returned a cross-tenant row")
        if event.attempts >= MAX_OUTBOX_PUBLISH_ATTEMPTS:
            _dead_letter(
                event,
                "Broker publish retry limit exceeded; event was dead-lettered.",
                increment_attempt=False,
            )
            continue
        if event.topic != "generation.requested":
            _dead_letter(event, "Unsupported outbox topic; event was dead-lettered.")
            continue
        payload = event.payload_json if isinstance(event.payload_json, dict) else {}
        job_id = payload.get("job_id")
        payload_organization_id = payload.get("organization_id")
        if (
            not _valid_event_identifier(job_id)
            or not _valid_event_identifier(payload_organization_id)
            or payload_organization_id != event.organization_id
        ):
            _dead_letter(
                event,
                "Malformed or cross-tenant generation request payload; event was dead-lettered.",
            )
            continue
        try:
            celery_app.send_task(
                "custombuild.generate_package",
                kwargs={
                    "job_id": job_id,
                    "organization_id": payload_organization_id,
                },
            )
        except Exception:
            event.attempts += 1
            if event.attempts >= MAX_OUTBOX_PUBLISH_ATTEMPTS:
                event.dead_lettered_at = _utcnow()
                event.last_error = (
                    "Broker publish failed at the retry limit; event was dead-lettered."
                )
            else:
                event.last_error = "Broker publish failed; a bounded retry is scheduled."
            continue
        event.dispatched_at = _utcnow()
        event.attempts += 1
        event.last_error = None
        dispatched += 1
    return dispatched


@celery_app.task(name="custombuild.recover_stale_jobs")  # type: ignore[misc]
def recover_stale_jobs(limit: int = DEFAULT_SCHEDULER_BATCH_LIMIT) -> int:
    global_limit = _scheduler_limit(limit)
    handled = 0
    for tenant_id in _organization_ids():
        remaining = global_limit - handled
        if remaining <= 0:
            break
        tenant_handled = 0
        try:
            with _tenant_transaction(tenant_id) as session:
                now = _database_time(session)
                legacy_threshold = now - LEGACY_STALE_LEASE_THRESHOLD
                jobs = list(
                    session.scalars(
                        select(GenerationJob)
                        .where(
                            GenerationJob.organization_id == tenant_id,
                            GenerationJob.status.in_([JobStatus.queued, JobStatus.running]),
                            or_(
                                GenerationJob.deadline_at <= now,
                                and_(
                                    GenerationJob.status == JobStatus.running,
                                    or_(
                                        GenerationJob.lease_expires_at <= now,
                                        and_(
                                            GenerationJob.lease_expires_at.is_(None),
                                            GenerationJob.started_at < legacy_threshold,
                                        ),
                                    ),
                                ),
                            ),
                        )
                        .order_by(GenerationJob.created_at, GenerationJob.id)
                        .with_for_update(skip_locked=True)
                        .limit(remaining)
                    )
                )
                for job in jobs:
                    if job.organization_id != tenant_id:
                        raise RuntimeError(
                            "tenant-local recovery query returned a cross-tenant row"
                        )
                    event = _recover_stale_job(job, now=now)
                    if event is not None:
                        session.add(event)
                    tenant_handled += 1
        except Exception as exc:
            logger.error(
                "Tenant recovery transaction rolled back (%s); continuing with other tenants.",
                type(exc).__name__,
            )
            continue
        handled += tenant_handled
    return handled


@celery_app.task(name="custombuild.reap_abandoned_storage")  # type: ignore[misc]
def reap_abandoned_storage(limit: int = DEFAULT_STORAGE_REAPER_BATCH_LIMIT) -> dict[str, int]:
    """Reclaim bounded abandoned objects across tenants and expose retryable drift."""

    global_limit = _scheduler_limit(limit)
    client = _s3_client()
    counts = {status.value: 0 for status in StorageReapStatus}
    processed = 0
    tenant_failures = 0
    organization_ids = _organization_ids()
    start_index = _storage_reaper_start_index(len(organization_ids))
    for tenant_id in _rotated_tenant_ids(organization_ids, start_index):
        remaining = global_limit - processed
        if remaining <= 0:
            break
        try:
            results = reap_storage_batch(
                SessionFactory,
                client,
                WORKER_SETTINGS.s3_bucket,
                tenant_id,
                batch_size=min(remaining, STORAGE_REAPER_TENANT_BATCH_LIMIT),
            )
        except Exception as exc:
            tenant_failures += 1
            logger.error(
                "Tenant storage reaper failed closed (%s); continuing with other tenants.",
                type(exc).__name__,
            )
            continue
        processed += len(results)
        for result in results:
            counts[result.status.value] += 1
    counts["processed"] = processed
    counts["tenant_failures"] = tenant_failures
    if tenant_failures or any(
        result_count
        for name, result_count in counts.items()
        if name
        in {
            StorageReapStatus.provider_error.value,
            StorageReapStatus.object_still_present.value,
            StorageReapStatus.identity_mismatch.value,
        }
    ):
        raise RuntimeError("storage reaper retained quota after a retryable failure")
    return counts


def _recover_stale_job(job: GenerationJob, *, now: datetime) -> OutboxEvent | None:
    """Move a stale lease to one deterministic terminal or retry state.

    A worker can disappear after claiming its final Celery attempt.  Such a job
    must become terminal instead of remaining ``running`` forever.  Earlier
    attempts are re-enqueued through the transactional outbox.
    """

    if _deadline_is_expired(job, now=now):
        _terminalize_deadline(job, now=now)
        return None

    if _lease_is_live(job, now=now):
        return None

    if job.attempts >= MAX_GENERATION_ATTEMPTS:
        job.status = JobStatus.failed
        job.lease_token = None
        job.lease_expires_at = None
        job.error = (
            "Stale worker lease exhausted the maximum of "
            f"{MAX_GENERATION_ATTEMPTS} generation attempts"
        )
        job.finished_at = now
        return None

    job.status = JobStatus.queued
    job.lease_token = None
    job.lease_expires_at = None
    job.error = "Recovered after stale worker lease"
    job.started_at = None
    job.finished_at = None
    return OutboxEvent(
        organization_id=job.organization_id,
        event_key=f"generation-recovery:{job.id}:{job.attempts}",
        topic="generation.requested",
        payload_json={
            "job_id": job.id,
            "organization_id": job.organization_id,
        },
    )


@celery_app.task(  # type: ignore[misc]
    bind=True,
    name="custombuild.generate_package",
    max_retries=MAX_GENERATION_ATTEMPTS - 1,
    default_retry_delay=5,
)
def generate_package(self: Any, *, job_id: str, organization_id: str) -> dict[str, Any]:
    claim = _claim_job(job_id, organization_id)
    if claim is None:
        return {"job_id": job_id, "state": "already_running_or_complete"}

    job, version = claim
    lease_token = job.lease_token
    if lease_token is None:
        raise RuntimeError("Generation claim did not receive a lease token")
    lease_guard: _GenerationLeaseGuard | None = None
    try:
        with _maintain_generation_lease(
            job_id,
            organization_id,
            lease_token,
        ) as lease_guard:
            result = _generate(
                job,
                version,
                lease_token=lease_token,
                lease_guard=lease_guard,
            )
            lease_guard.check()
            completed = _complete_job(
                job_id,
                organization_id,
                lease_token,
                version,
                result,
                require_storage_reservation=True,
            )
            if not completed:
                raise GenerationLeaseOwnershipLost(
                    "generation completion lost its exact job or storage lease"
                )
        return result
    except SoftTimeLimitExceeded:
        _record_failure(
            job_id,
            organization_id,
            lease_token,
            GenerationDeadlineExceeded("Generation task exceeded the server execution deadline"),
            terminal=True,
        )
        raise
    except Exception as exc:
        failure_at = _utcnow()
        terminal = (
            job.attempts >= MAX_GENERATION_ATTEMPTS
            or self.request.retries >= self.max_retries
            or isinstance(exc, IMMEDIATE_TERMINAL_GENERATION_ERRORS)
        )
        recorded_status = _record_failure(
            job_id,
            organization_id,
            lease_token,
            exc,
            terminal=terminal,
            recorded_at=failure_at,
        )
        if recorded_status is not JobStatus.queued:
            raise
        if isinstance(exc, GenerationStorageReservationBusy):
            retry_delay = exc.retry_after_seconds
        elif lease_guard is not None and bool(lease_guard.storage_claims()):
            retry_delay = int(GENERATION_LEASE_TTL.total_seconds()) + 5
        else:
            retry_delay = 5
        raise self.retry(exc=exc, countdown=retry_delay) from exc


def _claim_job(job_id: str, organization_id: str) -> tuple[GenerationJob, DesignVersion] | None:
    with _tenant_transaction(organization_id) as session:
        job = session.scalar(
            select(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.organization_id == organization_id,
            )
            .with_for_update()
        )
        if job is None or job.status in TERMINAL_JOB_STATUSES:
            return None
        # Only the recovery task may transition an expired running lease back
        # to queued. Duplicate broker deliveries must never steal live work.
        if job.status != JobStatus.queued:
            return None
        now = _database_time(session)
        if _deadline_is_expired(job, now=now):
            _terminalize_deadline(job, now=now)
            return None
        if job.attempts >= MAX_GENERATION_ATTEMPTS:
            _terminalize_attempt_budget(job, now=now)
            return None
        version = session.scalar(
            select(DesignVersion).where(
                DesignVersion.id == job.design_version_id,
                DesignVersion.organization_id == organization_id,
            )
        )
        if version is None:
            raise RuntimeError("Generation job references a missing tenant design version")
        job.status = JobStatus.running
        job.lease_token = str(uuid4())
        job.started_at = now
        job.lease_expires_at = now + GENERATION_LEASE_TTL
        job.deadline_at = job.deadline_at or now + GENERATION_JOB_TIMEOUT
        job.finished_at = None
        job.attempts += 1
        job.error = None
        session.flush()
        session.expunge(job)
        session.expunge(version)
        return job, version


def _renew_job_lease(
    job_id: str,
    organization_id: str,
    lease_token: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Extend a live lease only when the same tenant worker still owns it."""

    with _tenant_transaction(organization_id) as session:
        job = session.scalar(
            select(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.organization_id == organization_id,
                GenerationJob.status == JobStatus.running,
                GenerationJob.lease_token == lease_token,
            )
            .with_for_update()
        )
        if job is None:
            return False
        renewed_at = _database_time(session, now)
        if not _lease_is_live(job, now=renewed_at):
            return False
        if _deadline_is_expired(job, now=renewed_at):
            return False
        job.lease_expires_at = renewed_at + GENERATION_LEASE_TTL
        return True


@contextmanager
def _maintain_generation_lease(
    job_id: str,
    organization_id: str,
    lease_token: str,
    *,
    interval_seconds: float = GENERATION_HEARTBEAT_INTERVAL_SECONDS,
) -> Iterator[_GenerationLeaseGuard]:
    """Renew a generation lease in the background for the duration of work."""

    stopped = Event()
    guard = _GenerationLeaseGuard(organization_id, lease_token)

    def heartbeat() -> None:
        while not stopped.wait(interval_seconds):
            try:
                if not _renew_job_lease(job_id, organization_id, lease_token):
                    guard.fail(
                        GenerationLeaseOwnershipLost(
                            "generation job lease is no longer owned by this worker"
                        )
                    )
                    return
                claims = guard.storage_claims()
                if claims:
                    renew_storage_batch_lease(
                        SessionFactory,
                        organization_id,
                        claims,
                        lease_token=lease_token,
                        lease_duration=GENERATION_LEASE_TTL,
                    )
            except Exception as exc:
                logger.error(
                    "Generation lease heartbeat failed closed (%s).",
                    type(exc).__name__,
                )
                guard.fail(exc)
                return

    thread = Thread(
        target=heartbeat,
        name=f"generation-lease-heartbeat-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    try:
        yield guard
    finally:
        stopped.set()
        thread.join(timeout=5.0)
        if thread.is_alive():
            guard.fail(
                GenerationLeaseOwnershipLost(
                    "generation lease heartbeat did not stop within its deadline"
                )
            )


def _stock_id(
    *,
    role: str,
    material_id: str,
    material_version: str,
    thickness_um: int,
    width_um: int,
    height_um: int,
) -> str:
    """Build a stable stock identity without conflating carcass and back sheets."""

    return (
        f"stock-{role}-{material_id}-{material_version}-{thickness_um}um-{width_um}x{height_um}um"
    )


def _generation_attempt_id(job_id: str, lease_token: str) -> str:
    """Derive the immutable physical incarnation for one claimed job attempt."""

    try:
        job_uuid = UUID(job_id)
        canonical_job_id = str(job_uuid)
        canonical_lease_token = str(UUID(lease_token))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProductionBlockedError("generation storage attempt identity is invalid") from exc
    if canonical_job_id != job_id or canonical_lease_token != lease_token:
        raise ProductionBlockedError("generation storage attempt identity is not canonical")
    return str(uuid5(job_uuid, f"storage-attempt:{canonical_lease_token}"))


def _generation_artifact_id(attempt_id: str, kind: str) -> str:
    """Derive one physical artifact row/object identity inside an attempt."""

    if type(kind) is not str or not kind or kind != kind.strip():
        raise ProductionBlockedError("generation artifact kind is not canonical")
    try:
        attempt_uuid = UUID(attempt_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProductionBlockedError("generation storage attempt identity is invalid") from exc
    if str(attempt_uuid) != attempt_id:
        raise ProductionBlockedError("generation storage attempt identity is not canonical")
    return str(uuid5(attempt_uuid, kind))


def _generation_storage_claims(
    job: GenerationJob,
    version: DesignVersion,
    result: Mapping[str, Any],
    lease_token: str,
) -> tuple[StorageObjectClaim, ...]:
    if job.lease_token != lease_token:
        raise ProductionBlockedError(
            "generation storage lease token does not match the claimed job"
        )
    raw_evidence = result.get("evidence_artifacts")
    if not isinstance(raw_evidence, list):
        raise ProductionBlockedError("generation storage inventory is invalid")

    def storage_record(
        kind: object,
        object_key: object,
        digest: object,
        size_bytes: object,
        media_type: object,
    ) -> tuple[str, str, str, int, str]:
        if type(size_bytes) is not int:
            raise ProductionBlockedError("generation storage inventory is invalid")
        return (
            str(kind),
            str(object_key),
            str(digest),
            size_bytes,
            str(media_type),
        )

    records: list[tuple[str, str, str, int, str]] = [
        storage_record(
            "production_bundle",
            result.get("bundle_object_key", ""),
            result.get("bundle_sha256", ""),
            result.get("bundle_size_bytes"),
            "application/zip",
        ),
        storage_record(
            "manifest",
            result.get("manifest_object_key", ""),
            result.get("manifest_sha256", ""),
            result.get("manifest_size_bytes"),
            "application/json",
        ),
    ]
    for item in raw_evidence:
        if not isinstance(item, Mapping):
            raise ProductionBlockedError("generation storage inventory is invalid")
        records.append(
            storage_record(
                item.get("kind", ""),
                item.get("object_key", ""),
                item.get("sha256", ""),
                item.get("size_bytes"),
                item.get("content_type", ""),
            )
        )
    attempt_id = _generation_attempt_id(job.id, lease_token)
    expected_prefix = (
        f"{organization_id_safe(job.organization_id)}/{version.design_hash}/"
        f"{job.production_context_hash}/attempts/{attempt_id}/artifacts/"
    )
    try:
        claims: list[StorageObjectClaim] = []
        for kind, object_key, digest, size_bytes, media_type in records:
            artifact_id = _generation_artifact_id(attempt_id, kind)
            if (
                not object_key.startswith(f"{expected_prefix}{artifact_id}/")
                or object_key.count("/attempts/") != 1
                or object_key.count("/artifacts/") != 1
            ):
                raise ProductionBlockedError(
                    "generation object key does not match its exact storage attempt"
                )
            claims.append(
                StorageObjectClaim(
                    project_id=version.project_id,
                    object_key=object_key,
                    sha256=digest,
                    size_bytes=size_bytes,
                    media_type=media_type,
                    owner_type="generation_job",
                    owner_id=job.id,
                    idempotency_key=f"generation:{job.id}:{kind}:{artifact_id}",
                )
            )
        return tuple(claims)
    except (TypeError, ValueError) as exc:
        raise ProductionBlockedError("generation storage inventory is invalid") from exc


def _generate(
    job: GenerationJob,
    version: DesignVersion,
    *,
    lease_token: str,
    lease_guard: _GenerationLeaseGuard,
) -> dict[str, Any]:
    if (
        not isinstance(lease_guard, _GenerationLeaseGuard)
        or lease_guard.organization_id != job.organization_id
        or lease_guard.lease_token != lease_token
    ):
        raise ValueError("generation storage requires its exact lease guard")
    if job.lease_token != lease_token:
        raise ValueError("generation storage lease token does not match the claimed job")
    lease_guard.check()
    attempt_id = _generation_attempt_id(job.id, lease_token)
    resolved = _resolve_current_job_context(job, version)
    try:
        capability = require_template_for_revision(
            version.template_id,
            resolve_template_capability(version.template_id).archetype,
        )
    except TemplateCapabilityError as exc:
        raise ProductionBlockedError(
            f"template capability blocked: {exc.code}: {exc.message}"
        ) from exc
    spec, capability_snapshot = _load_frozen_design_spec(version, capability)
    design = build_bookcase(spec)
    if design.design_hash != version.design_hash:
        raise ProductionBlockedError("Frozen DesignSpec no longer matches persisted design hash")
    rule_report = evaluate_design(design)
    if rule_report.overall_status == RuleStatus.BLOCK:
        blockers = ", ".join(
            item.rule_id for item in rule_report.evaluations if item.status == RuleStatus.BLOCK
        )
        raise ProductionBlockedError(f"generation blocked by construction rules: {blockers}")

    request = job.request_json
    external_evidence = tuple(request.get("external_evidence", ()))
    machine = resolved.machine
    carcass_width_um = int(round(float(request["stock_width_mm"]) * 1000))
    carcass_height_um = int(round(float(request["stock_height_mm"]) * 1000))
    carcass_stock = StockSheet(
        stock_id=_stock_id(
            role="carcass",
            material_id=spec.material.material_id,
            material_version=spec.material.version,
            thickness_um=spec.parameters.actual_thickness_um,
            width_um=carcass_width_um,
            height_um=carcass_height_um,
        ),
        material_id=spec.material.material_id,
        material_version=spec.material.version,
        width_um=carcass_width_um,
        height_um=carcass_height_um,
        thickness_um=spec.parameters.actual_thickness_um,
        quantity=int(request["stock_count"]),
        # The request carries dimensions and quantity, not a supplier-sheet axis.
        # Keep stock orientation unbound until a structured stock profile owns it.
        grain_direction="UNBOUND",
    )
    stocks = [carcass_stock]
    if spec.back_material is not None:
        back_width_um = int(round(float(request["back_stock_width_mm"]) * 1000))
        back_height_um = int(round(float(request["back_stock_height_mm"]) * 1000))
        stocks.append(
            StockSheet(
                stock_id=_stock_id(
                    role="back",
                    material_id=spec.back_material.material_id,
                    material_version=spec.back_material.version,
                    thickness_um=spec.parameters.back_thickness_um,
                    width_um=back_width_um,
                    height_um=back_height_um,
                ),
                material_id=spec.back_material.material_id,
                material_version=spec.back_material.version,
                width_um=back_width_um,
                height_um=back_height_um,
                thickness_um=spec.parameters.back_thickness_um,
                quantity=int(request["back_stock_count"]),
                # The validation request does not yet carry a verified supplier-sheet
                # orientation. Keep it explicitly unspecified and record that fact in
                # the signed manifest instead of inventing a grain match.
                grain_direction="UNBOUND",
            )
        )
    context = ManifestContext(
        project_id=version.project_id,
        revision=str(version.revision),
        design_hash=design.design_hash,
        app_version=resolved.context.app_version,
        engine_version=version.engine_version,
        template_version=version.template_version,
        template_id=version.template_id,
        template_capability_fingerprint=version.template_capability_fingerprint,
        template_capability=capability_snapshot,
        rule_version=version.rule_version,
        material_versions=tuple(
            sorted(
                {
                    f"{item.material_id}@{item.version}"
                    for item in (spec.material, spec.back_material)
                    if item is not None
                }
            )
        ),
        joint_version=BOOKCASE_JOINT_SUPPORT_VERSION,
        machine_profile_id=machine.profile_id,
        machine_profile_version=machine.version,
        postprocessor_version=resolved.postprocessor.version,
        cad_status="PENDING",
        generation_context_hash=job.production_context_hash,
        production_engine_context=resolved.context.as_dict(),
        warnings=tuple(
            [rule_report.disclaimer]
            + [
                f"{item.rule_id}@{item.rule_version}: {item.title}"
                for item in rule_report.evaluations
                if item.status == RuleStatus.WARNING
            ]
        ),
        overrides=tuple(request.get("approved_warning_overrides", ())),
        external_evidence=external_evidence,
        source_provenance=version.source_provenance_json or None,
    )
    document_files = [
        ArtifactFile("bom/bom.pdf", bom_pdf(design), "application/pdf", "BOM_PDF"),
        ArtifactFile(
            "bom/hardware-list.csv",
            hardware_csv(design),
            "text/csv",
            "HARDWARE_LIST",
        ),
        ArtifactFile(
            "assembly/assembly-manual.pdf",
            assembly_manual_pdf(design),
            "application/pdf",
            "ASSEMBLY_REVIEW_MANUAL",
        ),
        ArtifactFile(
            "assembly/assembly-readiness.json",
            assembly_readiness_json(design),
            "application/json",
            "ASSEMBLY_READINESS",
        ),
        ArtifactFile(
            "labels/part-labels.pdf",
            labels_pdf(design),
            "application/pdf",
            "PART_LABELS",
        ),
        ArtifactFile(
            "qa/measurement-protocol.pdf",
            qa_protocol_pdf(design),
            "application/pdf",
            "QA_PROTOCOL",
        ),
        ArtifactFile(
            "validation/construction-report.pdf",
            validation_report_pdf(rule_report),
            "application/pdf",
            "CONSTRUCTION_VALIDATION_REPORT",
        ),
        ArtifactFile(
            "validation/construction-report.json",
            canonical_json_bytes(rule_report),
            "application/json",
            "CONSTRUCTION_VALIDATION_REPORT",
        ),
    ]
    if version.source_provenance_json:
        document_files.append(
            ArtifactFile(
                "validation/source-provenance.json",
                canonical_json_bytes(version.source_provenance_json),
                "application/json",
                "SOURCE_PROVENANCE",
            )
        )
    documents = tuple(document_files)
    bundle = build_production_bundle(
        design,
        stock=tuple(stocks),
        machine=machine,
        context=context,
        include_step=bool(request["include_step"]),
        include_freecad_project=bool(request.get("include_freecad_project", False)),
        include_validation_program=bool(request["include_validation_program"]),
        production_release=False,
        allow_blocked_cam=True,
        additional_artifacts=documents,
    )
    if bundle.manifest.get("generation_context_hash") != job.production_context_hash:
        raise ProductionBlockedError("manifest generation context does not match frozen job")
    if canonical_json_bytes(bundle.manifest.get("production_engine_context")) != (
        canonical_json_bytes(resolved.context.as_dict())
    ):
        raise ProductionBlockedError("manifest production engine context drifted")
    if type(bundle.zip_bytes) is not bytes or not valid_artifact_size(
        "production_bundle", len(bundle.zip_bytes)
    ):
        raise ProductionBlockedError(
            "generated production bundle is empty or exceeds its canonical size limit"
        )
    manifest_bytes = canonical_json_bytes(bundle.manifest)
    if not valid_artifact_size("manifest", len(manifest_bytes)):
        raise ProductionBlockedError(
            "generated manifest is empty or exceeds its canonical size limit"
        )
    bundle_sha = sha256_hex(bundle.zip_bytes)
    manifest_sha = sha256_hex(manifest_bytes)
    for artifact in bundle.artifacts:
        if not isinstance(artifact, ArtifactFile):
            raise ProductionBlockedError("generated artifact inventory is invalid")
        path = artifact.path
        if type(path) is not str or not path:
            raise ProductionBlockedError("generated artifact path is invalid")
        if path.startswith("cam/setups/") and _SETUP_EVIDENCE_PATH.fullmatch(path) is None:
            raise ProductionBlockedError("generated setup evidence path is invalid")
    evidence_candidates = [
        artifact
        for artifact in bundle.artifacts
        if artifact.path in _EVIDENCE_ARTIFACT_CONTRACTS or artifact.path.startswith("cam/setups/")
    ]
    if len(evidence_candidates) > MAX_EVIDENCE_ARTIFACTS:
        raise ProductionBlockedError("generated evidence inventory exceeds its file-count limit")
    prepared_evidence: list[tuple[ArtifactFile, str, str, str]] = []
    evidence_kinds: set[str] = set()
    evidence_keys: set[str] = set()
    retained_paths: set[str] = set()
    evidence_total_bytes = 0
    setup_index = 0
    for artifact in sorted(evidence_candidates, key=lambda item: item.path):
        contract = _EVIDENCE_ARTIFACT_CONTRACTS.get(artifact.path)
        if contract is None:
            setup_index += 1
            kind = f"setup_sheet_{setup_index:03d}"
            expected_media_type = "image/svg+xml"
            expected_role = "SETUP_SHEET"
        else:
            kind, expected_media_type, expected_role = contract
        if artifact.media_type != expected_media_type:
            raise ProductionBlockedError(
                "generated evidence media type does not match its canonical path"
            )
        if artifact.role != expected_role:
            raise ProductionBlockedError(
                "generated evidence role does not match its canonical path"
            )
        if not valid_artifact_size(kind, len(artifact.data)):
            raise ProductionBlockedError(
                f"generated {kind} artifact is empty or exceeds its canonical size limit"
            )
        evidence_total_bytes += len(artifact.data)
        if evidence_total_bytes > MAX_EVIDENCE_TOTAL_BYTES:
            raise ProductionBlockedError(
                "generated evidence inventory exceeds its total size limit"
            )
        digest = sha256_hex(artifact.data)
        artifact_id = _generation_artifact_id(attempt_id, kind)
        evidence_key = (
            f"{organization_id_safe(job.organization_id)}/{version.design_hash}/"
            f"{job.production_context_hash}/attempts/{attempt_id}/"
            f"artifacts/{artifact_id}/{digest}/evidence/"
            f"{artifact.path.replace('/', '__')}"
        )
        if type(evidence_key) is not str or not evidence_key:
            raise ProductionBlockedError("generated evidence object key is invalid")
        if (
            kind in evidence_kinds
            or evidence_key in evidence_keys
            or artifact.path in retained_paths
        ):
            raise ProductionBlockedError(
                "generated evidence inventory contains duplicate identities"
            )
        evidence_kinds.add(kind)
        evidence_keys.add(evidence_key)
        retained_paths.add(artifact.path)
        prepared_evidence.append((artifact, kind, digest, evidence_key))

    bundle_artifact_id = _generation_artifact_id(attempt_id, "production_bundle")
    manifest_artifact_id = _generation_artifact_id(attempt_id, "manifest")
    bundle_key = (
        f"{organization_id_safe(job.organization_id)}/{version.design_hash}/"
        f"{job.production_context_hash}/attempts/{attempt_id}/"
        f"artifacts/{bundle_artifact_id}/linked-v1/{manifest_sha}/"
        f"{bundle_sha}/production.zip"
    )
    manifest_key = (
        f"{organization_id_safe(job.organization_id)}/{version.design_hash}/"
        f"{job.production_context_hash}/attempts/{attempt_id}/"
        f"artifacts/{manifest_artifact_id}/{manifest_sha}/manifest.json"
    )
    if (
        type(bundle_key) is not str
        or not bundle_key
        or type(manifest_key) is not str
        or not manifest_key
        or bundle_key == manifest_key
        or bundle_key in evidence_keys
        or manifest_key in evidence_keys
    ):
        raise ProductionBlockedError("generated object-key inventory is invalid")

    # No object-store mutation is allowed until every generated object and the
    # complete evidence inventory have passed their canonical limits and
    # identity checks.
    evidence_artifacts: list[dict[str, Any]] = [
        {
            "kind": kind,
            "object_key": evidence_key,
            "sha256": digest,
            "size_bytes": len(artifact.data),
            "content_type": artifact.media_type,
        }
        for artifact, kind, digest, evidence_key in prepared_evidence
    ]
    cam_blocked = bundle.review_status.cam_status is CAMStageStatus.BLOCKED
    result = {
        "bundle_sha256": bundle_sha,
        "bundle_size_bytes": len(bundle.zip_bytes),
        "manifest_sha256": manifest_sha,
        "manifest_size_bytes": len(manifest_bytes),
        "bundle_object_key": bundle_key,
        "manifest_object_key": manifest_key,
        "evidence_artifacts": evidence_artifacts,
        "artifact_count": len(bundle.artifacts) + 1,
        "generation_context_hash": job.production_context_hash,
        "production_engine_context_hash": resolved.context.fingerprint,
        "dfm_status": bundle.dfm_report.status.value,
        "design_review_package_status": bundle.review_status.as_dict(),
        "nesting_utilization_ppm": (
            None
            if cam_blocked
            else sum(layout.utilization_ppm for layout in bundle.layouts) // len(bundle.layouts)
        ),
        "used_sheet_count": (
            0 if cam_blocked else sum(layout.used_sheet_count for layout in bundle.layouts)
        ),
        "nesting_layouts": (
            []
            if cam_blocked
            else [
                {
                    "stock_id": layout.stock_id,
                    "utilization_ppm": layout.utilization_ppm,
                    "used_sheet_count": layout.used_sheet_count,
                }
                for layout in bundle.layouts
            ]
        ),
        "authoritative_geometry": bool(request["include_step"]),
        "freecad_project_requested": bool(request.get("include_freecad_project", False)),
        "freecad_project_generated": any(
            artifact.path == "model/design.fcstd" for artifact in bundle.artifacts
        ),
        "machine_program_mode": "CAM_BLOCKED" if cam_blocked else "VALIDATION_DRY_RUN",
        "production_machine_program": False,
        "workshop_readiness": bundle.workshop_readiness.as_dict(),
    }
    lease_guard.check()
    frozen_result = MappingProxyType(result)
    claims = _generation_storage_claims(
        job,
        version,
        frozen_result,
        lease_token,
    )
    try:
        reserve_storage_batch(
            SessionFactory,
            job.organization_id,
            claims,
            lease_token=lease_token,
            lease_duration=GENERATION_LEASE_TTL,
            capacity_settings=WORKER_SETTINGS,
        )
    except StorageReservationBusy as exc:
        raise GenerationStorageReservationBusy(
            "a previous generation attempt still owns the immutable storage batch",
            retry_after_seconds=exc.retry_after_seconds,
        ) from exc
    except (StorageClaimConflict, StorageQuotaExceeded, StorageQuotaInvariantError) as exc:
        raise ProductionBlockedError(
            "generation storage quota could not reserve the complete immutable batch"
        ) from exc
    lease_guard.bind_storage_claims(claims)
    lease_guard.check()

    for artifact, _kind, _digest, evidence_key in prepared_evidence:
        lease_guard.check()
        _put_object(evidence_key, artifact.data, artifact.media_type)
    lease_guard.check()
    _put_object(
        bundle_key,
        bundle.zip_bytes,
        "application/zip",
        metadata={"manifest-sha256": manifest_sha},
    )
    lease_guard.check()
    _put_object(manifest_key, manifest_bytes, "application/json")
    lease_guard.check()
    return result


def _load_frozen_design_spec(
    version: DesignVersion,
    capability: TemplateCapability,
) -> tuple[BookcaseDesignSpec, dict[str, object]]:
    """Validate persisted immutable design data without normalizing it."""

    capability_snapshot = capability.snapshot()
    result_json = version.result_json
    if not isinstance(result_json, dict):
        raise ProductionBlockedError(
            "frozen design result is malformed; save and validate a new revision"
        )
    if (
        version.template_capability_fingerprint != capability.capability_fingerprint
        or result_json.get("template_capability") != capability_snapshot
    ):
        raise ProductionBlockedError(
            "frozen template capability is stale; save and validate a new revision"
        )
    try:
        spec = BookcaseDesignSpec.model_validate(version.spec_json)
        spec.parameters.assert_furniture_family(capability.archetype)
    except (TypeError, ValueError) as exc:
        raise ProductionBlockedError(
            "frozen DesignSpec violates the published design envelope or furniture family"
        ) from exc
    return spec, capability_snapshot


def _resolve_current_job_context(
    job: GenerationJob,
    version: DesignVersion,
) -> ResolvedProductionComponents:
    """Recompute and equality-guard code/catalog identity before generation."""

    try:
        if not isinstance(version.result_json, dict):
            raise ProductionContextError("frozen design result is not an object")
        result_json = version.result_json
        assert_job_matches_frozen_revision_context(
            result_json.get("production_context"),
            job.request_json,
        )
        assert_frozen_design_versions(
            engine_version=version.engine_version,
            template_version=version.template_version,
            rule_version=version.rule_version,
        )
        current = resolve_production_components(
            machine_profile_id=str(job.request_json["machine_profile_id"]),
            postprocessor_id=str(job.request_json["postprocessor_id"]),
            **WORKER_SETTINGS.build_identity,
            require_cad_runtime=bool(
                job.request_json.get("include_step", False)
                or job.request_json.get("include_freecad_project", False)
            ),
        )
        if not contexts_equal(job.production_engine_context_json, current.context):
            raise ProductionContextError("production engine context has drifted")
        expected_hash = generation_context_hash(
            design_context_hash=version.context_hash,
            design_version_id=version.id,
            revision=version.revision,
            request=job.request_json,
            production_engine_context=current.context,
        )
        if expected_hash != job.production_context_hash:
            raise ProductionContextError("generation context hash does not match the frozen job")
        return current
    except (KeyError, ProductionContextError) as exc:
        raise ProductionBlockedError(f"production engine context drift: {exc}") from exc


def _complete_job(
    job_id: str,
    organization_id: str,
    lease_token: str,
    version: DesignVersion,
    result: dict[str, Any],
    *,
    require_storage_reservation: bool = False,
) -> bool:
    with _tenant_transaction(organization_id) as session:
        job = session.scalar(
            select(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.organization_id == organization_id,
                GenerationJob.status == JobStatus.running,
                GenerationJob.lease_token == lease_token,
            )
            .with_for_update()
        )
        if job is None:
            return False
        completed_at = _database_time(session)
        if _deadline_is_expired(job, now=completed_at):
            _terminalize_deadline(job, now=completed_at)
            return False
        if not _lease_is_live(job, now=completed_at):
            return False
        if job.attempts > MAX_GENERATION_ATTEMPTS:
            _terminalize_attempt_budget(job, now=completed_at)
            return False
        expected_engine_context_hash = sha256_hex(
            canonical_json_bytes(job.production_engine_context_json)
        )
        if result.get("production_engine_context_hash") != expected_engine_context_hash:
            raise ProductionBlockedError(
                "generation result is not bound to the persisted production engine context"
            )
        artifact_records: list[tuple[str, str, str, str, int]] = [
            (
                "production_bundle",
                result["bundle_object_key"],
                result["bundle_sha256"],
                "application/zip",
                result["bundle_size_bytes"],
            ),
            (
                "manifest",
                result["manifest_object_key"],
                result["manifest_sha256"],
                "application/json",
                result["manifest_size_bytes"],
            ),
        ]
        if any(
            type(kind) is not str
            or not kind
            or type(key) is not str
            or not key
            or type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or type(media_type) is not str
            or not media_type
            or not valid_artifact_size(kind, size_bytes)
            for kind, key, digest, media_type, size_bytes in artifact_records
        ):
            raise ProductionBlockedError("generation result artifact inventory is invalid")
        raw_evidence = result.get("evidence_artifacts")
        if not isinstance(raw_evidence, list):
            raise ProductionBlockedError("generation result evidence inventory is invalid")
        if len(raw_evidence) > MAX_EVIDENCE_ARTIFACTS:
            raise ProductionBlockedError("generation result evidence inventory is invalid")
        evidence_kinds = {record[0] for record in artifact_records}
        evidence_keys = {record[1] for record in artifact_records}
        if len(evidence_kinds) != len(artifact_records) or len(evidence_keys) != len(
            artifact_records
        ):
            raise ProductionBlockedError("generation result artifact inventory is invalid")
        evidence_total_bytes = 0
        for item in raw_evidence:
            if not isinstance(item, Mapping):
                raise ProductionBlockedError("generation result evidence inventory is invalid")
            values = (
                item.get("kind"),
                item.get("object_key"),
                item.get("sha256"),
                item.get("content_type"),
                item.get("size_bytes"),
            )
            kind, key, digest, media_type, size_bytes = values
            if (
                type(kind) is not str
                or not kind
                or type(key) is not str
                or not key
                or type(digest) is not str
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or type(media_type) is not str
                or not media_type
                or not valid_artifact_size(kind, size_bytes)
            ):
                raise ProductionBlockedError("generation result evidence inventory is invalid")
            assert type(size_bytes) is int
            if kind in evidence_kinds or key in evidence_keys:
                raise ProductionBlockedError("generation result evidence inventory is invalid")
            evidence_total_bytes += size_bytes
            if evidence_total_bytes > MAX_EVIDENCE_TOTAL_BYTES:
                raise ProductionBlockedError("generation result evidence inventory is invalid")
            evidence_kinds.add(kind)
            evidence_keys.add(key)
            artifact_records.append((kind, key, digest, media_type, size_bytes))
        attempt_id = _generation_attempt_id(job.id, lease_token)
        storage_claims = (
            _generation_storage_claims(job, version, result, lease_token)
            if require_storage_reservation
            else ()
        )
        job.result_json = result
        for kind, key, digest, media_type, size_bytes in artifact_records:
            artifact_id = _generation_artifact_id(attempt_id, kind)
            existing = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == job.id,
                    Artifact.organization_id == organization_id,
                    Artifact.kind == kind,
                )
            )
            if existing is None:
                session.add(
                    Artifact(
                        id=artifact_id,
                        organization_id=organization_id,
                        generation_job_id=job.id,
                        kind=kind,
                        object_key=key,
                        sha256=digest,
                        size_bytes=size_bytes,
                        content_type=media_type,
                    )
                )
            elif (
                existing.id != artifact_id
                or existing.sha256 != digest
                or existing.object_key != key
            ):
                raise RuntimeError(
                    f"non-deterministic {kind} artifact detected for generation job {job.id}"
                )
        if require_storage_reservation:
            try:
                commit_storage_batch_in_transaction(
                    session,
                    organization_id,
                    storage_claims,
                    lease_token=lease_token,
                )
            except (
                StorageClaimConflict,
                StorageQuotaExceeded,
                StorageQuotaInvariantError,
            ) as exc:
                raise ProductionBlockedError(
                    "generation storage reservation could not be committed exactly"
                ) from exc
        session.flush()
        _validate_completion_evidence(session, organization_id, job)
        # Success is the final state transition.  The exact API download gate
        # above must pass while the job is still lease-owned and ``running``;
        # any exception rolls this transaction back without a success audit.
        job.status = JobStatus.succeeded
        job.lease_token = None
        job.lease_expires_at = None
        job.finished_at = completed_at
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_id=version.created_by,
                action="generation.succeeded",
                entity_type="generation_job",
                entity_id=job.id,
                payload_json={
                    "bundle_sha256": result["bundle_sha256"],
                    "manifest_sha256": result["manifest_sha256"],
                },
            )
        )
        return True


def _validate_completion_evidence(
    session: Any,
    organization_id: str,
    job: GenerationJob,
) -> None:
    """Run the API's artifact/download evidence gate before committing success."""

    # The worker already shares the API's models and storage adapter.  Import
    # lazily to avoid initializing the HTTP router during worker module import,
    # while still using one canonical validation implementation.
    from app.api import _require_review_evidence
    from fastapi import HTTPException

    try:
        client = _s3_client()
        with storage_runtime(client=client, bucket=WORKER_SETTINGS.s3_bucket):
            _require_review_evidence(
                session,
                organization_id,
                job,
                stream_hash=False,
                require_cam=False,
                bind_review_documents=True,
                build_identity=WORKER_SETTINGS.build_identity,
            )
    except (BotoCoreError, ClientError, OSError) as exc:
        raise ArtifactStorageUnavailableError(
            "completion evidence storage is temporarily unavailable"
        ) from exc
    except HTTPException as exc:
        if exc.status_code == 503:
            raise ArtifactStorageUnavailableError(
                "completion evidence storage is temporarily unavailable"
            ) from exc
        raise ProductionBlockedError(
            "generated artifact evidence failed the canonical completion gate"
        ) from exc


def _record_failure(
    job_id: str,
    organization_id: str,
    lease_token: str,
    exc: Exception,
    *,
    terminal: bool,
    recorded_at: datetime | None = None,
) -> JobStatus | None:
    with _tenant_transaction(organization_id) as session:
        job = session.scalar(
            select(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.organization_id == organization_id,
                GenerationJob.status == JobStatus.running,
                GenerationJob.lease_token == lease_token,
            )
            .with_for_update()
        )
        if job is None:
            return None
        failed_at = _database_time(session, recorded_at)
        if _deadline_is_expired(job, now=failed_at):
            _terminalize_deadline(job, now=failed_at)
            return job.status
        job.status = (
            JobStatus.failed
            if (
                terminal
                or job.attempts >= MAX_GENERATION_ATTEMPTS
                or isinstance(exc, IMMEDIATE_TERMINAL_GENERATION_ERRORS)
            )
            else JobStatus.queued
        )
        job.lease_token = None
        job.lease_expires_at = None
        job.error = f"{type(exc).__name__}: {exc}"[:4000]
        job.finished_at = failed_at if job.status == JobStatus.failed else None
        job.started_at = None if job.status == JobStatus.queued else job.started_at
        return job.status


def _put_object(
    key: str,
    payload: bytes,
    content_type: str,
    *,
    metadata: Mapping[str, str] | None = None,
) -> None:
    try:
        client = _s3_client()
    except (BotoCoreError, ClientError, OSError) as exc:
        raise ArtifactStorageUnavailableError(
            "artifact storage is temporarily unavailable; verify the object store and retry"
        ) from exc
    bucket = WORKER_SETTINGS.s3_bucket
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        raise ArtifactStorageUnavailableError(
            "artifact storage is temporarily unavailable; verify the object store and retry"
        ) from None
    except (BotoCoreError, OSError) as exc:
        raise ArtifactStorageUnavailableError(
            "artifact storage is temporarily unavailable; verify the object store and retry"
        ) from exc
    try:
        store_create_once_object(
            client,
            bucket=bucket,
            object_key=key,
            content=payload,
            content_type=content_type,
            sha256=sha256_hex(payload),
            metadata=metadata,
        )
    except ArtifactIntegrityError as exc:
        raise ProductionBlockedError(
            "non-deterministic content-addressed artifact collision; "
            "the existing object does not match the generated bytes"
        ) from exc


def _s3_client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=WORKER_SETTINGS.s3_endpoint,
        aws_access_key_id=WORKER_SETTINGS.s3_access_key,
        aws_secret_access_key=WORKER_SETTINGS.s3_secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=S3_CONNECT_TIMEOUT_SECONDS,
            read_timeout=S3_READ_TIMEOUT_SECONDS,
            retries={
                "mode": "standard",
                "total_max_attempts": S3_TOTAL_MAX_ATTEMPTS,
            },
            s3={"addressing_style": "path"},
        ),
    )


def organization_id_safe(value: str) -> str:
    if not value or any(character not in "0123456789abcdef-" for character in value.lower()):
        raise ValueError("Invalid organization identifier for object key")
    return value.lower()
