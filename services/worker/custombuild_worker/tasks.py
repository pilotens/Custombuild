from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Any
from uuid import UUID, uuid4

import boto3
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
    OutboxEvent,
)
from app.storage import (
    ArtifactIntegrityError,
    ArtifactStorageUnavailableError,
    store_create_once_object,
)
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
    ArtifactFile,
    CAMStageStatus,
    ManifestContext,
    ProductionBlockedError,
    StockSheet,
    build_production_bundle,
    canonical_json_bytes,
    sha256_hex,
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
from sqlalchemy import and_, create_engine, or_, select
from sqlalchemy.orm import sessionmaker

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
    },
)

_engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
MAX_GENERATION_ATTEMPTS = 4
MAX_OUTBOX_PUBLISH_ATTEMPTS = 5
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


def _utcnow() -> datetime:
    return datetime.now(UTC)


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


def _dead_letter(event: OutboxEvent, reason: str, *, increment_attempt: bool = True) -> None:
    if increment_attempt:
        event.attempts += 1
    event.dead_lettered_at = _utcnow()
    event.last_error = reason[:500]


@celery_app.task(name="custombuild.dispatch_outbox")  # type: ignore[misc]
def dispatch_outbox(limit: int = 50) -> int:
    """Publish committed outbox events. Duplicate delivery is safe by job identity."""

    dispatched = 0
    with SessionFactory.begin() as session:
        events = list(
            session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.dispatched_at.is_(None),
                    OutboxEvent.dead_lettered_at.is_(None),
                )
                .order_by(OutboxEvent.created_at)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        for event in events:
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
            organization_id = payload.get("organization_id")
            if not _valid_event_identifier(job_id) or not _valid_event_identifier(organization_id):
                _dead_letter(
                    event,
                    "Malformed generation request payload; event was dead-lettered.",
                )
                continue
            try:
                celery_app.send_task(
                    "custombuild.generate_package",
                    kwargs={
                        "job_id": job_id,
                        "organization_id": organization_id,
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
def recover_stale_jobs() -> int:
    now = _utcnow()
    legacy_threshold = now - LEGACY_STALE_LEASE_THRESHOLD
    handled = 0
    with SessionFactory.begin() as session:
        jobs = session.scalars(
            select(GenerationJob)
            .where(
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
            .with_for_update(skip_locked=True)
        )
        for job in jobs:
            event = _recover_stale_job(job, now=now)
            if event is not None:
                session.add(event)
            handled += 1
    return handled


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
    try:
        with _maintain_generation_lease(job_id, organization_id, lease_token):
            result = _generate(job, version)
        _complete_job(job_id, organization_id, lease_token, version, result)
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
            _deadline_is_expired(job, now=failure_at)
            or job.attempts >= MAX_GENERATION_ATTEMPTS
            or self.request.retries >= self.max_retries
            or isinstance(exc, IMMEDIATE_TERMINAL_GENERATION_ERRORS)
        )
        _record_failure(
            job_id,
            organization_id,
            lease_token,
            exc,
            terminal=terminal,
            recorded_at=failure_at,
        )
        if terminal:
            raise
        raise self.retry(exc=exc) from exc


def _claim_job(job_id: str, organization_id: str) -> tuple[GenerationJob, DesignVersion] | None:
    with SessionFactory.begin() as session:
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
        now = _utcnow()
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

    renewed_at = now or _utcnow()
    with SessionFactory.begin() as session:
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
) -> Iterator[None]:
    """Renew a generation lease in the background for the duration of work."""

    stopped = Event()

    def heartbeat() -> None:
        while not stopped.wait(interval_seconds):
            try:
                if not _renew_job_lease(job_id, organization_id, lease_token):
                    return
            except Exception as exc:  # A later heartbeat can heal transient DB outages.
                logger.warning(
                    "Generation lease heartbeat failed (%s); retrying while the lease is valid.",
                    type(exc).__name__,
                )

    thread = Thread(
        target=heartbeat,
        name=f"generation-lease-heartbeat-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=5.0)


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


def _generate(job: GenerationJob, version: DesignVersion) -> dict[str, Any]:
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
    bundle_sha = sha256_hex(bundle.zip_bytes)
    manifest_bytes = canonical_json_bytes(bundle.manifest)
    manifest_sha = sha256_hex(manifest_bytes)
    evidence_candidates = [
        artifact
        for artifact in bundle.artifacts
        if artifact.path
        in {
            "validation/dfm-report.json",
            DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
            "validation/stock-selection.json",
            "validation/generation-plan.json",
            "cam/operations.json",
            "cam/validation-backplot.svg",
            "model/design.glb",
            "model/design.fcstd",
            "validation/cad-interchange-status.json",
            "validation/source-provenance.json",
            "validation/workshop-readiness.json",
            "assembly/assembly-readiness.json",
        }
        or artifact.path.startswith("cam/setups/")
    ]
    evidence_artifacts: list[dict[str, Any]] = []
    setup_index = 0
    for artifact in sorted(evidence_candidates, key=lambda item: item.path):
        kind = {
            "validation/dfm-report.json": "dfm_report",
            DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH: "design_review_package_status",
            "validation/stock-selection.json": "stock_selection",
            "validation/generation-plan.json": "generation_plan",
            "cam/operations.json": "operations",
            "cam/validation-backplot.svg": "validation_backplot",
            "model/design.glb": "design_glb",
            "model/design.fcstd": "design_fcstd",
            "validation/cad-interchange-status.json": "cad_interchange_status",
            "validation/source-provenance.json": "source_provenance",
            "validation/workshop-readiness.json": "workshop_readiness",
            "assembly/assembly-readiness.json": "assembly_readiness",
        }.get(artifact.path)
        if kind is None:
            setup_index += 1
            kind = f"setup_sheet_{setup_index:03d}"
        digest = sha256_hex(artifact.data)
        evidence_key = (
            f"{organization_id_safe(job.organization_id)}/{version.design_hash}/"
            f"{job.production_context_hash}/{digest}/evidence/"
            f"{artifact.path.replace('/', '__')}"
        )
        _put_object(evidence_key, artifact.data, artifact.media_type)
        evidence_artifacts.append(
            {
                "kind": kind,
                "object_key": evidence_key,
                "sha256": digest,
                "size_bytes": len(artifact.data),
                "content_type": artifact.media_type,
            }
        )
    bundle_key = (
        f"{organization_id_safe(job.organization_id)}/{version.design_hash}/"
        f"{job.production_context_hash}/linked-v1/{manifest_sha}/"
        f"{bundle_sha}/production.zip"
    )
    manifest_key = (
        f"{organization_id_safe(job.organization_id)}/{version.design_hash}/"
        f"{job.production_context_hash}/{manifest_sha}/manifest.json"
    )
    _put_object(
        bundle_key,
        bundle.zip_bytes,
        "application/zip",
        metadata={"manifest-sha256": manifest_sha},
    )
    _put_object(manifest_key, manifest_bytes, "application/json")
    cam_blocked = bundle.review_status.cam_status is CAMStageStatus.BLOCKED
    return {
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
) -> bool:
    with SessionFactory.begin() as session:
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
        completed_at = _utcnow()
        if _deadline_is_expired(job, now=completed_at):
            _terminalize_deadline(job, now=completed_at)
            return False
        if job.attempts > MAX_GENERATION_ATTEMPTS:
            _terminalize_attempt_budget(job, now=completed_at)
            return False
        job.status = JobStatus.succeeded
        job.lease_token = None
        job.lease_expires_at = None
        job.result_json = result
        job.finished_at = completed_at
        artifact_records = [
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
        artifact_records.extend(
            (
                str(item["kind"]),
                str(item["object_key"]),
                str(item["sha256"]),
                str(item["content_type"]),
                int(item["size_bytes"]),
            )
            for item in result.get("evidence_artifacts", [])
        )
        for kind, key, digest, media_type, size_bytes in artifact_records:
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
                        organization_id=organization_id,
                        generation_job_id=job.id,
                        kind=kind,
                        object_key=key,
                        sha256=digest,
                        size_bytes=size_bytes,
                        content_type=media_type,
                    )
                )
            elif existing.sha256 != digest or existing.object_key != key:
                raise RuntimeError(
                    f"non-deterministic {kind} artifact detected for generation job {job.id}"
                )
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


def _record_failure(
    job_id: str,
    organization_id: str,
    lease_token: str,
    exc: Exception,
    *,
    terminal: bool,
    recorded_at: datetime | None = None,
) -> bool:
    with SessionFactory.begin() as session:
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
        failed_at = recorded_at or _utcnow()
        if _deadline_is_expired(job, now=failed_at):
            _terminalize_deadline(job, now=failed_at)
            return True
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
        return True


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
    except ClientError as exc:
        status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status_code != 404:
            raise ArtifactStorageUnavailableError(
                "artifact storage is temporarily unavailable; verify the object store and retry"
            ) from None
        try:
            client.create_bucket(Bucket=bucket)
        except (BotoCoreError, ClientError, OSError) as create_error:
            raise ArtifactStorageUnavailableError(
                "artifact storage is temporarily unavailable; verify the object store and retry"
            ) from create_error
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
