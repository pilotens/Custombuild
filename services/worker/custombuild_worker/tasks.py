from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4, uuid5

import boto3
from app.db import set_tenant_context
from app.design_service import (
    stock_configuration_for_design,
    two_sided_registration_for_design,
)
from app.job_policy import (
    GENERATION_HEARTBEAT_INTERVAL_SECONDS,
    GENERATION_JOB_TIMEOUT,
    GENERATION_LEASE_TTL,
    GENERATION_RECOVERY_INTERVAL_SECONDS,
    GENERATION_TASK_HARD_TIME_LIMIT_SECONDS,
    GENERATION_TASK_SOFT_TIME_LIMIT_SECONDS,
    LEGACY_STALE_LEASE_THRESHOLD,
)
from app.joint_retention import MAX_SIGNED_EVIDENCE_BYTES
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
    JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE,
    JOINT_RETENTION_SIGNED_EVIDENCE_PATH,
    JOINT_RETENTION_SIGNED_EVIDENCE_ROLE,
    MANUFACTURING_INTENT_PATH,
    MANUFACTURING_INTENT_ROLE,
    MAX_EVIDENCE_ARTIFACTS,
    MAX_EVIDENCE_TOTAL_BYTES,
    SUPPLIER_HANDOFF_PATH,
    SUPPLIER_HANDOFF_ROLE,
    ArtifactFile,
    CAMStageStatus,
    ManifestContext,
    ProductionBlockedError,
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
from custombuild_manufacturing.production_machine_profile import (
    LoadedProductionMachineProfile,
    ProductionMachineProfileError,
    load_production_machine_profile,
    production_machine_profile_job_binding_json,
)
from custombuild_manufacturing.readiness import ReadinessValidationError
from custombuild_rules import RuleStatus, evaluate_design
from kombu import Queue  # type: ignore[import-untyped]
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import and_, create_engine, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .cam_candidate import build_worker_cam_candidate
from .config import get_worker_settings
from .documents import (
    VerifiedRetentionTrust,
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


@dataclass(frozen=True, slots=True)
class VerifiedRetentionPackageInput:
    """Ephemeral package input produced only by the canonical retention preflight."""

    document_trust: VerifiedRetentionTrust
    signed_evidence_bytes: bytes

    def __post_init__(self) -> None:
        if type(self.signed_evidence_bytes) is not bytes:
            raise TypeError("signed retention package evidence must be exact bytes")
        if (
            not self.signed_evidence_bytes
            or len(self.signed_evidence_bytes) > MAX_SIGNED_EVIDENCE_BYTES
        ):
            raise ValueError("signed retention package evidence size is invalid")
        if sha256_hex(self.signed_evidence_bytes) != (self.document_trust.storage_evidence_sha256):
            raise ValueError("signed retention package evidence checksum mismatch")


_EVIDENCE_ARTIFACT_CONTRACTS: Mapping[str, tuple[str, str, str]] = {
    MANUFACTURING_INTENT_PATH: (
        "manufacturing_intent",
        "application/json",
        MANUFACTURING_INTENT_ROLE,
    ),
    SUPPLIER_HANDOFF_PATH: (
        "supplier_handoff",
        "application/json",
        SUPPLIER_HANDOFF_ROLE,
    ),
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
_CAM_CANDIDATE_EVIDENCE_CONTRACTS: Mapping[str, tuple[str, str]] = {
    "cam_candidate_bundle": ("application/zip", "EXECUTABLE_CAM_CANDIDATE_BUNDLE"),
    "cutting_toolpaths": ("application/json", "PRODUCTION_TOOLPATH_DOCUMENT"),
    "machine_program_index": ("application/json", "PRODUCTION_PROGRAM_INDEX"),
    "cutting_program_validation_report": (
        "application/json",
        "CUTTING_PROGRAM_VALIDATION_REPORT",
    ),
    "cutting_backplot": ("image/svg+xml", "CUTTING_BACKPLOT"),
    "production_machine_profile": (
        "application/json",
        "PRODUCTION_MACHINE_PROFILE_DOCUMENT",
    ),
}
_SETUP_EVIDENCE_PATH = re.compile(r"cam/setups/[A-Za-z0-9][A-Za-z0-9._-]*\.svg")
_MACHINE_PROGRAM_EVIDENCE_KIND = re.compile(r"machine_program_[0-9]{3}")
celery_app = Celery("custombuild-worker", broker=REDIS_URL, backend=REDIS_URL)
GENERATION_QUEUE = "generation"
MAINTENANCE_QUEUE = "maintenance"
STORAGE_REAPER_QUEUE = "storage-reaper"
UNROUTED_QUEUE = "unrouted"
BROKER_VISIBILITY_TIMEOUT_SECONDS = GENERATION_TASK_HARD_TIME_LIMIT_SECONDS + 60
OUTBOX_PUBLISH_BACKOFF_BASE_SECONDS = 2
OUTBOX_PUBLISH_BACKOFF_MAX_SECONDS = 60
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=GENERATION_TASK_SOFT_TIME_LIMIT_SECONDS,
    task_time_limit=GENERATION_TASK_HARD_TIME_LIMIT_SECONDS,
    task_queues=(
        Queue(GENERATION_QUEUE),
        Queue(MAINTENANCE_QUEUE),
        Queue(STORAGE_REAPER_QUEUE),
        Queue(UNROUTED_QUEUE),
    ),
    task_default_queue=UNROUTED_QUEUE,
    task_create_missing_queues=False,
    task_routes={
        "custombuild.generate_package": {"queue": GENERATION_QUEUE},
        "custombuild.dispatch_outbox": {"queue": MAINTENANCE_QUEUE},
        "custombuild.recover_stale_jobs": {"queue": MAINTENANCE_QUEUE},
        "custombuild.reap_abandoned_storage": {"queue": STORAGE_REAPER_QUEUE},
    },
    task_publish_retry=False,
    broker_transport_options={
        "visibility_timeout": BROKER_VISIBILITY_TIMEOUT_SECONDS,
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
        "retry_on_timeout": False,
    },
    beat_schedule={
        "dispatch-transactional-outbox": {
            "task": "custombuild.dispatch_outbox",
            "schedule": 2.0,
            "options": {"queue": MAINTENANCE_QUEUE},
        },
        "recover-stale-generation-leases": {
            "task": "custombuild.recover_stale_jobs",
            "schedule": GENERATION_RECOVERY_INTERVAL_SECONDS,
            "options": {"queue": MAINTENANCE_QUEUE},
        },
        "reap-abandoned-storage": {
            "task": "custombuild.reap_abandoned_storage",
            "schedule": 60.0,
            "options": {"queue": STORAGE_REAPER_QUEUE},
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
DEFAULT_SCHEDULER_BATCH_LIMIT = 50
DEFAULT_STORAGE_REAPER_BATCH_LIMIT = 25
STORAGE_REAPER_TENANT_BATCH_LIMIT = 10
OUTBOX_DISPATCH_CURSOR_KEY = "custombuild:scheduler:outbox-dispatch:tenant-cursor:v1"
GENERATION_RECOVERY_CURSOR_KEY = "custombuild:scheduler:generation-recovery:tenant-cursor:v1"
STORAGE_REAPER_CURSOR_KEY = "custombuild:scheduler:storage-reaper:tenant-cursor:v1"
GENERATION_DELIVERY_WATCHDOG_INTERVAL = max(
    GENERATION_LEASE_TTL,
    timedelta(seconds=2 * GENERATION_RECOVERY_INTERVAL_SECONDS),
)
GENERATION_DELIVERY_WATCHDOG_EVENT_PREFIX = "generation-delivery-watchdog"
GENERATION_DELIVERY_WATCHDOG_ERROR = "Queued generation delivery watchdog state is invalid"
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
    job.next_attempt_at = None
    job.error = GENERATION_DEADLINE_ERROR
    job.finished_at = now


def _terminalize_attempt_budget(job: GenerationJob, *, now: datetime) -> None:
    job.status = JobStatus.failed
    job.lease_token = None
    job.lease_expires_at = None
    job.next_attempt_at = None
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


def _scheduler_start_index(
    tenant_count: int,
    *,
    cursor_key: str,
    scheduler_name: str,
) -> int:
    """Advance one scheduler-specific cursor before a globally bounded tenant scan."""

    if type(tenant_count) is not int or tenant_count < 0:
        raise ValueError("tenant_count must be a non-negative integer")
    if not cursor_key or not scheduler_name:
        raise ValueError("scheduler cursor identity must be non-empty")
    if tenant_count == 0:
        return 0
    client: Redis = Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry_on_timeout=False,
    )
    try:
        cursor = client.incr(cursor_key)
    except RedisError as exc:
        raise RuntimeError(f"{scheduler_name} fairness cursor is unavailable") from exc
    finally:
        client.close()
    if type(cursor) is not int or cursor < 1:
        raise RuntimeError(f"{scheduler_name} fairness cursor is non-canonical")
    return (cursor - 1) % tenant_count


def _storage_reaper_start_index(tenant_count: int) -> int:
    """Preserve the storage task's named seam while sharing cursor validation."""

    return _scheduler_start_index(
        tenant_count,
        cursor_key=STORAGE_REAPER_CURSOR_KEY,
        scheduler_name="storage reaper",
    )


def _rotated_tenant_ids(
    organization_ids: tuple[str, ...],
    start_index: int,
) -> tuple[str, ...]:
    if not organization_ids:
        return ()
    if type(start_index) is not int or not 0 <= start_index < len(organization_ids):
        raise RuntimeError("scheduler fairness cursor is outside the tenant registry")
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


def _outbox_publish_backoff(attempts: int) -> timedelta:
    """Return a bounded deterministic delay for one failed broker publication."""

    if type(attempts) is not int or attempts < 1:
        raise ValueError("outbox attempts must be a positive integer")
    exponent = min(attempts - 1, 30)
    seconds = min(
        OUTBOX_PUBLISH_BACKOFF_BASE_SECONDS * (2**exponent),
        OUTBOX_PUBLISH_BACKOFF_MAX_SECONDS,
    )
    return timedelta(seconds=seconds)


@celery_app.task(name="custombuild.dispatch_outbox")  # type: ignore[misc]
def dispatch_outbox(limit: int = 50) -> int:
    """Publish committed outbox events. Duplicate delivery is safe by job identity."""

    global_limit = _scheduler_limit(limit)
    dispatched = 0
    handled = 0
    organization_ids = _organization_ids()
    start_index = _scheduler_start_index(
        len(organization_ids),
        cursor_key=OUTBOX_DISPATCH_CURSOR_KEY,
        scheduler_name="outbox dispatcher",
    )
    for tenant_id in _rotated_tenant_ids(organization_ids, start_index):
        remaining = global_limit - handled
        if remaining <= 0:
            break
        tenant_handled = 0
        tenant_dispatched = 0
        try:
            with _tenant_transaction(tenant_id) as session:
                now = _database_time(session)
                events = list(
                    session.scalars(
                        select(OutboxEvent)
                        .where(
                            OutboxEvent.organization_id == tenant_id,
                            OutboxEvent.dispatched_at.is_(None),
                            OutboxEvent.dead_lettered_at.is_(None),
                            OutboxEvent.available_at <= now,
                        )
                        .order_by(OutboxEvent.created_at, OutboxEvent.id)
                        .with_for_update(skip_locked=True)
                        .limit(remaining)
                    )
                )
                tenant_handled = len(events)
                tenant_dispatched = _dispatch_tenant_outbox_events(
                    events,
                    tenant_id,
                    published_at=now,
                )
        except Exception as exc:
            logger.error(
                "Tenant outbox transaction rolled back (%s); continuing with other tenants.",
                type(exc).__name__,
            )
            continue
        handled += tenant_handled
        dispatched += tenant_dispatched
    return dispatched


def _terminalize_delivery_watchdog_invariant(
    job: GenerationJob,
    *,
    now: datetime,
) -> None:
    job.status = JobStatus.failed
    job.lease_token = None
    job.lease_expires_at = None
    job.next_attempt_at = None
    job.error = GENERATION_DELIVERY_WATCHDOG_ERROR
    job.finished_at = now


def _generation_delivery_watchdog_event_key(job_id: str) -> str:
    if not _valid_event_identifier(job_id):
        raise ValueError("generation watchdog job_id must be a canonical UUID")
    return f"{GENERATION_DELIVERY_WATCHDOG_EVENT_PREFIX}:{job_id}"


def _recover_unclaimed_queued_delivery(
    session: Session,
    job: GenerationJob,
    *,
    now: datetime,
) -> OutboxEvent | None:
    """Recreate one broker delivery only after a queued job remains unclaimed."""

    if job.status != JobStatus.queued:
        raise RuntimeError("generation delivery watchdog received a non-queued job")
    if job.deadline_at is None:
        _terminalize_delivery_watchdog_invariant(job, now=now)
        return None
    if _deadline_is_expired(job, now=now):
        _terminalize_deadline(job, now=now)
        return None
    if job.attempts >= MAX_GENERATION_ATTEMPTS:
        _terminalize_attempt_budget(job, now=now)
        return None
    if job.next_attempt_at is None:
        _terminalize_delivery_watchdog_invariant(job, now=now)
        return None
    if _as_utc(job.next_attempt_at) > now:
        return None

    event_key = _generation_delivery_watchdog_event_key(job.id)
    expected_payload = {
        "job_id": job.id,
        "organization_id": job.organization_id,
    }
    watchdog = session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.organization_id == job.organization_id,
            OutboxEvent.event_key == event_key,
        )
        .with_for_update()
    )
    if watchdog is not None:
        if (
            watchdog.organization_id != job.organization_id
            or watchdog.topic != "generation.requested"
            or watchdog.payload_json != expected_payload
            or watchdog.dead_lettered_at is not None
        ):
            _terminalize_delivery_watchdog_invariant(job, now=now)
            return None
        if watchdog.dispatched_at is None:
            job.updated_at = now
            return None

    pending_event_id = session.scalar(
        select(OutboxEvent.id)
        .where(
            OutboxEvent.organization_id == job.organization_id,
            OutboxEvent.event_key != event_key,
            OutboxEvent.topic == "generation.requested",
            OutboxEvent.dispatched_at.is_(None),
            OutboxEvent.dead_lettered_at.is_(None),
            OutboxEvent.payload_json["job_id"].as_string() == job.id,
            OutboxEvent.payload_json["organization_id"].as_string() == job.organization_id,
        )
        .limit(1)
    )
    job.updated_at = now
    if pending_event_id is not None:
        return None
    if watchdog is None:
        return OutboxEvent(
            organization_id=job.organization_id,
            event_key=event_key,
            topic="generation.requested",
            payload_json=expected_payload,
            available_at=now,
        )

    watchdog.dispatched_at = None
    watchdog.available_at = now
    watchdog.last_error = (
        "Broker acknowledgement was not followed by a generation claim; "
        "a bounded delivery retry was scheduled."
    )
    return None


def _dispatch_tenant_outbox_events(
    events: list[OutboxEvent],
    organization_id: str,
    *,
    published_at: datetime | None = None,
) -> int:
    now = _as_utc(published_at or _utcnow())
    dispatched = 0
    for event in events:
        if event.organization_id != organization_id:
            raise RuntimeError("tenant-local outbox query returned a cross-tenant row")
        if _as_utc(event.available_at) > now:
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
                queue=GENERATION_QUEUE,
                retry=False,
            )
        except Exception:
            event.attempts += 1
            event.available_at = now + _outbox_publish_backoff(event.attempts)
            event.last_error = (
                "Broker publish failed; a durable bounded-backoff retry is scheduled."
            )
            continue
        event.dispatched_at = now
        event.attempts += 1
        event.last_error = None
        dispatched += 1
    return dispatched


@celery_app.task(name="custombuild.recover_stale_jobs")  # type: ignore[misc]
def recover_stale_jobs(limit: int = DEFAULT_SCHEDULER_BATCH_LIMIT) -> int:
    global_limit = _scheduler_limit(limit)
    handled = 0
    organization_ids = _organization_ids()
    start_index = _scheduler_start_index(
        len(organization_ids),
        cursor_key=GENERATION_RECOVERY_CURSOR_KEY,
        scheduler_name="generation recovery",
    )
    for tenant_id in _rotated_tenant_ids(organization_ids, start_index):
        remaining = global_limit - handled
        if remaining <= 0:
            break
        tenant_handled = 0
        try:
            with _tenant_transaction(tenant_id) as session:
                now = _database_time(session)
                legacy_threshold = now - LEGACY_STALE_LEASE_THRESHOLD
                queued_delivery_threshold = now - GENERATION_DELIVERY_WATCHDOG_INTERVAL
                jobs = list(
                    session.scalars(
                        select(GenerationJob)
                        .where(
                            GenerationJob.organization_id == tenant_id,
                            GenerationJob.status.in_([JobStatus.queued, JobStatus.running]),
                            or_(
                                GenerationJob.deadline_at <= now,
                                and_(
                                    GenerationJob.status == JobStatus.queued,
                                    or_(
                                        GenerationJob.attempts >= MAX_GENERATION_ATTEMPTS,
                                        GenerationJob.deadline_at.is_(None),
                                        GenerationJob.next_attempt_at.is_(None),
                                        and_(
                                            GenerationJob.next_attempt_at <= now,
                                            GenerationJob.updated_at <= queued_delivery_threshold,
                                        ),
                                    ),
                                ),
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
                        .order_by(
                            GenerationJob.updated_at,
                            GenerationJob.created_at,
                            GenerationJob.id,
                        )
                        .with_for_update(skip_locked=True)
                        .limit(remaining)
                    )
                )
                for job in jobs:
                    if job.organization_id != tenant_id:
                        raise RuntimeError(
                            "tenant-local recovery query returned a cross-tenant row"
                        )
                    if job.status == JobStatus.queued:
                        event = _recover_unclaimed_queued_delivery(
                            session,
                            job,
                            now=now,
                        )
                    else:
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
        job.next_attempt_at = None
        job.error = (
            "Stale worker lease exhausted the maximum of "
            f"{MAX_GENERATION_ATTEMPTS} generation attempts"
        )
        job.finished_at = now
        return None

    job.status = JobStatus.queued
    job.lease_token = None
    job.lease_expires_at = None
    job.next_attempt_at = now
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
        available_at=now,
    )


@celery_app.task(name="custombuild.generate_package")  # type: ignore[misc]
def generate_package(*, job_id: str, organization_id: str) -> dict[str, Any]:
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
            verified_retention_input = _validate_retention_before_generation(
                organization_id,
                version,
                minimum_valid_until=job.deadline_at,
            )
            lease_guard.check()
            result = _generate(
                job,
                version,
                lease_token=lease_token,
                lease_guard=lease_guard,
                verified_retention_input=verified_retention_input,
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
        terminal = job.attempts >= MAX_GENERATION_ATTEMPTS or isinstance(
            exc,
            (GenerationDeadlineExceeded, *IMMEDIATE_TERMINAL_GENERATION_ERRORS),
        )
        if isinstance(exc, GenerationStorageReservationBusy):
            retry_delay = exc.retry_after_seconds
        elif lease_guard is not None and bool(lease_guard.storage_claims()):
            retry_delay = int(GENERATION_LEASE_TTL.total_seconds()) + 5
        else:
            retry_delay = 5
        recorded_status = _record_failure(
            job_id,
            organization_id,
            lease_token,
            exc,
            terminal=terminal,
            recorded_at=failure_at,
            retry_after_seconds=retry_delay,
        )
        if recorded_status is not JobStatus.queued:
            raise
        return {
            "job_id": job_id,
            "state": "retry_scheduled",
            "retry_after_seconds": retry_delay,
        }


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
        if job.next_attempt_at is not None and _as_utc(job.next_attempt_at) > now:
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
        job.next_attempt_at = None
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


def _configured_cutting_candidate_profile(
    request: Mapping[str, Any],
) -> LoadedProductionMachineProfile | None:
    """Resolve only the server-owned profile bound by the API at enqueue time."""

    requested = request.get("include_cutting_candidate", False)
    persisted_binding = request.get("production_machine_profile")
    if type(requested) is not bool:
        raise ProductionBlockedError("cutting candidate opt-in must be a boolean")
    if not requested:
        if persisted_binding is not None:
            raise ProductionBlockedError(
                "production machine profile binding exists without cutting-candidate opt-in"
            )
        return None
    if request.get("include_validation_program") is not True:
        raise ProductionBlockedError(
            "cutting candidate requires the complete validation-program review package"
        )
    if not isinstance(persisted_binding, Mapping):
        raise ProductionBlockedError(
            "cutting candidate has no server-owned production machine profile binding"
        )
    try:
        profile = load_production_machine_profile(
            WORKER_SETTINGS.production_cam_profile_source,
            allow_test_only=WORKER_SETTINGS.app_env == "test",
        )
        if canonical_json_bytes(persisted_binding) != (
            production_machine_profile_job_binding_json(profile)
        ):
            raise ProductionBlockedError(
                "production machine profile changed after the generation job was queued"
            )
    except ProductionBlockedError:
        raise
    except (ProductionMachineProfileError, TypeError, ValueError) as exc:
        raise ProductionBlockedError(
            "configured production machine profile is unavailable or invalid"
        ) from exc
    return profile


def _generate(
    job: GenerationJob,
    version: DesignVersion,
    *,
    lease_token: str,
    lease_guard: _GenerationLeaseGuard,
    verified_retention_input: VerifiedRetentionPackageInput | None = None,
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
    cutting_profile = _configured_cutting_candidate_profile(job.request_json)
    if cutting_profile is not None:
        source_profile = cutting_profile.execution_context
        if (
            source_profile.source_machine_profile_id != resolved.machine.profile_id
            or source_profile.source_machine_profile_version != resolved.machine.version
            or source_profile.source_machine_profile_fingerprint
            != resolved.context.machine_profile_fingerprint
        ):
            raise ProductionBlockedError(
                "production CAM profile is detached from the frozen validation machine"
            )
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
    retention_contract = design.spec.joint_retention
    if retention_contract is None:
        if verified_retention_input is not None:
            raise ProductionBlockedError(
                "verified retention package input contradicts the unbound frozen design"
            )
        verified_retention_trust = None
        signed_retention_artifacts: tuple[ArtifactFile, ...] = ()
    else:
        if verified_retention_input is None:
            raise ProductionBlockedError(
                "retention-bound generation has no canonically verified signed evidence bytes"
            )
        verified_retention_trust = verified_retention_input.document_trust
        if not verified_retention_trust.matches_contract(retention_contract):
            raise ProductionBlockedError(
                "verified retention package input is detached from the frozen contract"
            )
        signed_retention_artifacts = (
            ArtifactFile(
                JOINT_RETENTION_SIGNED_EVIDENCE_PATH,
                verified_retention_input.signed_evidence_bytes,
                JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE,
                JOINT_RETENTION_SIGNED_EVIDENCE_ROLE,
            ),
        )
    rule_report = evaluate_design(design)
    if rule_report.overall_status == RuleStatus.BLOCK:
        blockers = ", ".join(
            item.rule_id for item in rule_report.evaluations if item.status == RuleStatus.BLOCK
        )
        raise ProductionBlockedError(f"generation blocked by construction rules: {blockers}")

    request = job.request_json
    external_evidence = tuple(request.get("external_evidence", ()))
    machine = resolved.machine
    try:
        stocks = stock_configuration_for_design(design, request)
        registrations = two_sided_registration_for_design(
            design,
            request,
            stocks=stocks,
        )
    except (TypeError, ValueError) as exc:
        raise ProductionBlockedError(
            f"frozen workshop stock or two-sided registration is invalid: {exc}"
        ) from exc
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
        ArtifactFile(
            "bom/bom.pdf",
            bom_pdf(design, verified_retention_trust),
            "application/pdf",
            "BOM_PDF",
        ),
        ArtifactFile(
            "bom/hardware-list.csv",
            hardware_csv(design, verified_retention_trust),
            "text/csv",
            "HARDWARE_LIST",
        ),
        ArtifactFile(
            "assembly/assembly-manual.pdf",
            assembly_manual_pdf(design, verified_retention_trust),
            "application/pdf",
            "ASSEMBLY_REVIEW_MANUAL",
        ),
        ArtifactFile(
            "assembly/assembly-readiness.json",
            assembly_readiness_json(design, verified_retention_trust),
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
    documents = (*document_files, *signed_retention_artifacts)
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
        two_sided_registration_by_stock=registrations,
        additional_artifacts=documents,
    )
    cutting_candidate = (
        build_worker_cam_candidate(bundle, cutting_profile, resolved.context)
        if cutting_profile is not None
        else None
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
    candidate_kind_by_path: dict[str, str] = {}
    if cutting_candidate is not None:
        for item in cutting_candidate.evidence:
            if item.artifact.path in candidate_kind_by_path:
                raise ProductionBlockedError(
                    "cutting candidate contains duplicate persisted evidence paths"
                )
            candidate_kind_by_path[item.artifact.path] = item.kind
            evidence_candidates.append(item.artifact)
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
        candidate_kind = candidate_kind_by_path.get(artifact.path)
        if contract is not None:
            kind, expected_media_type, expected_role = contract
        elif candidate_kind is not None:
            candidate_contract = _CAM_CANDIDATE_EVIDENCE_CONTRACTS.get(candidate_kind)
            if candidate_contract is None:
                if _MACHINE_PROGRAM_EVIDENCE_KIND.fullmatch(candidate_kind) is None:
                    raise ProductionBlockedError(
                        "generated cutting candidate evidence kind is invalid"
                    )
                candidate_contract = (
                    "text/x-gcode",
                    "EXECUTABLE_CAM_CANDIDATE_PROGRAM",
                )
            kind = candidate_kind
            expected_media_type, expected_role = candidate_contract
        else:
            setup_index += 1
            kind = f"setup_sheet_{setup_index:03d}"
            expected_media_type = "image/svg+xml"
            expected_role = "SETUP_SHEET"
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
        "artifact_count": (
            len(bundle.artifacts)
            + 1
            + (0 if cutting_candidate is None else len(cutting_candidate.bundle.artifacts) + 1)
        ),
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
        "machine_program_mode": (
            "EXECUTABLE_CAM_CANDIDATE"
            if cutting_candidate is not None
            else ("CAM_BLOCKED" if cam_blocked else "VALIDATION_DRY_RUN")
        ),
        "production_machine_program": cutting_candidate is not None,
        "workshop_readiness": bundle.workshop_readiness.as_dict(),
        **(
            {
                "cam_status": "CUTTING_CANDIDATE_GENERATED",
                "physical_cutting_authorized": False,
                "workshop_acceptance_required": True,
                "cam_candidate": cutting_candidate.result_claims,
            }
            if cutting_candidate is not None
            else {}
        ),
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
        # The evidence gate performs remote object-store reads while this
        # transaction is open. Re-read the database clock immediately before
        # success so a deadline or lease that elapsed during those reads can
        # never commit artifacts, quota or a success audit. Raising rolls the
        # entire completion transaction back; the failure transaction then
        # terminalizes or durably requeues the exact still-owned attempt.
        final_fence_at = _database_time(session)
        if _deadline_is_expired(job, now=final_fence_at):
            raise GenerationDeadlineExceeded(
                "Generation completion crossed the server execution deadline"
            )
        if not _lease_is_live(job, now=final_fence_at):
            raise GenerationLeaseOwnershipLost(
                "generation completion lease expired before the success commit"
            )
        # Success is the final state transition.  The exact API download gate
        # above must pass while the job is still lease-owned and ``running``;
        # any exception rolls this transaction back without a success audit.
        job.status = JobStatus.succeeded
        job.lease_token = None
        job.lease_expires_at = None
        job.next_attempt_at = None
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
                trust_registry_json=WORKER_SETTINGS.joint_retention_trust_registry_json,
                production_mode=WORKER_SETTINGS.app_env == "production",
                allow_test_only_profiles=WORKER_SETTINGS.app_env == "test",
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


def _validate_retention_before_generation(
    organization_id: str,
    claimed_version: DesignVersion,
    *,
    minimum_valid_until: datetime | None,
) -> VerifiedRetentionPackageInput | None:
    """Revalidate mutable retention trust before spending a generation attempt.

    The API validates the binding when it queues the job and the completion
    evidence gate validates it again before success.  This additional boundary
    prevents a revoked or expired statement from consuming a full CAM/package
    run after a queued job has waited for capacity.
    """

    from app.api import _require_current_retention_binding
    from fastapi import HTTPException

    try:
        client = _s3_client()
        with (
            _tenant_transaction(organization_id) as session,
            storage_runtime(client=client, bucket=WORKER_SETTINGS.s3_bucket),
        ):
            version = session.scalar(
                select(DesignVersion).where(
                    DesignVersion.id == claimed_version.id,
                    DesignVersion.organization_id == organization_id,
                )
            )
            if version is None:
                raise ProductionBlockedError(
                    "generation retention preflight references a missing design version"
                )
            current_evidence = _require_current_retention_binding(
                session,
                organization_id,
                version,
                minimum_valid_until=minimum_valid_until,
                trust_registry_json=WORKER_SETTINGS.joint_retention_trust_registry_json,
                production_mode=WORKER_SETTINGS.app_env == "production",
            )
            try:
                bound_spec = BookcaseDesignSpec.model_validate(version.spec_json)
            except (TypeError, ValueError) as exc:
                raise ProductionBlockedError(
                    "generation retention preflight could not reconstruct the frozen design"
                ) from exc
            if bound_spec.joint_retention is None:
                if current_evidence is not None:
                    raise ProductionBlockedError(
                        "unbound generation unexpectedly returned retention evidence"
                    )
                return None
            if current_evidence is None:
                raise ProductionBlockedError(
                    "retention-bound generation did not receive verified evidence bytes"
                )
            result_json = version.result_json
            if not isinstance(result_json, Mapping):
                raise ProductionBlockedError(
                    "generation retention preflight has no frozen trust snapshot"
                )
            try:
                verified = VerifiedRetentionTrust.from_verified_snapshot(
                    result_json.get("retention_trust")
                )
            except (TypeError, ValueError) as exc:
                raise ProductionBlockedError(
                    "generation retention preflight trust snapshot is malformed"
                ) from exc
            if not verified.matches_contract(bound_spec.joint_retention):
                raise ProductionBlockedError(
                    "generation retention preflight trust snapshot is detached from the contract"
                )
            try:
                evidence_bytes = current_evidence.content
                evidence_sha256 = current_evidence.sha256
            except AttributeError as exc:
                raise ProductionBlockedError(
                    "canonical retention gate returned malformed evidence"
                ) from exc
            if (
                type(evidence_bytes) is not bytes
                or evidence_sha256 != verified.storage_evidence_sha256
            ):
                raise ProductionBlockedError(
                    "canonical retention evidence is detached from the frozen trust snapshot"
                )
            try:
                return VerifiedRetentionPackageInput(
                    document_trust=verified,
                    signed_evidence_bytes=evidence_bytes,
                )
            except (TypeError, ValueError) as exc:
                raise ProductionBlockedError(
                    "canonical retention evidence is invalid for package generation"
                ) from exc
    except (BotoCoreError, ClientError, OSError) as exc:
        raise ArtifactStorageUnavailableError(
            "retention evidence storage is temporarily unavailable"
        ) from exc
    except HTTPException as exc:
        if exc.status_code == 503:
            raise ArtifactStorageUnavailableError(
                "retention evidence storage is temporarily unavailable"
            ) from exc
        raise ProductionBlockedError(
            "retention evidence failed the canonical generation preflight"
        ) from exc


def _record_failure(
    job_id: str,
    organization_id: str,
    lease_token: str,
    exc: Exception,
    *,
    terminal: bool,
    recorded_at: datetime | None = None,
    retry_after_seconds: int = 0,
) -> JobStatus | None:
    if type(retry_after_seconds) is not int or retry_after_seconds < 0:
        raise ValueError("retry_after_seconds must be a non-negative integer")
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
        if job.status == JobStatus.queued:
            retry_at = failed_at + timedelta(seconds=retry_after_seconds)
            job.next_attempt_at = retry_at
            session.add(
                OutboxEvent(
                    organization_id=organization_id,
                    event_key=f"generation-retry:{job.id}:{job.attempts}",
                    topic="generation.requested",
                    payload_json={
                        "job_id": job.id,
                        "organization_id": organization_id,
                    },
                    available_at=retry_at,
                )
            )
        else:
            job.next_attempt_at = None
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
