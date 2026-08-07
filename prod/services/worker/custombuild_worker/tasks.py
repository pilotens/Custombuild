from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from app import __version__ as APP_VERSION
from app.models import (
    Artifact,
    AuditEvent,
    DesignVersion,
    GenerationJob,
    JobStatus,
    OutboxEvent,
)
from botocore.exceptions import ClientError
from celery import Celery
from custombuild_domain import (
    BOOKCASE_JOINT_SUPPORT_VERSION,
    BookcaseDesignSpec,
    build_bookcase,
)
from custombuild_manufacturing import (
    ArtifactFile,
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
    contexts_equal,
    generation_context_hash,
    resolve_production_components,
)
from custombuild_rules import RuleStatus, evaluate_design
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from .config import get_worker_settings
from .documents import (
    assembly_manual_pdf,
    bom_pdf,
    hardware_csv,
    labels_pdf,
    qa_protocol_pdf,
    validation_report_pdf,
)

WORKER_SETTINGS = get_worker_settings()
REDIS_URL = WORKER_SETTINGS.redis_url
DATABASE_URL = WORKER_SETTINGS.database_url
celery_app = Celery("custombuild-worker", broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "dispatch-transactional-outbox": {
            "task": "custombuild.dispatch_outbox",
            "schedule": 2.0,
        },
        "recover-stale-generation-leases": {
            "task": "custombuild.recover_stale_jobs",
            "schedule": 300.0,
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
TERMINAL_JOB_STATUSES = frozenset({JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled})


def _utcnow() -> datetime:
    return datetime.now(UTC)


@celery_app.task(name="custombuild.dispatch_outbox")  # type: ignore[misc]
def dispatch_outbox(limit: int = 50) -> int:
    """Publish committed outbox events. Duplicate delivery is safe by job identity."""

    dispatched = 0
    with SessionFactory.begin() as session:
        events = list(
            session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.dispatched_at.is_(None))
                .order_by(OutboxEvent.created_at)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        for event in events:
            if event.topic != "generation.requested":
                event.attempts += 1
                continue
            celery_app.send_task(
                "custombuild.generate_package",
                kwargs={
                    "job_id": str(event.payload_json["job_id"]),
                    "organization_id": str(event.payload_json["organization_id"]),
                },
            )
            event.dispatched_at = _utcnow()
            event.attempts += 1
            dispatched += 1
    return dispatched


@celery_app.task(name="custombuild.recover_stale_jobs")  # type: ignore[misc]
def recover_stale_jobs() -> int:
    threshold = _utcnow() - timedelta(minutes=30)
    handled = 0
    with SessionFactory.begin() as session:
        jobs = session.scalars(
            select(GenerationJob)
            .where(
                GenerationJob.status == JobStatus.running,
                GenerationJob.started_at < threshold,
            )
            .with_for_update(skip_locked=True)
        )
        for job in jobs:
            event = _recover_stale_job(job, now=_utcnow())
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

    if job.attempts >= MAX_GENERATION_ATTEMPTS:
        job.status = JobStatus.failed
        job.error = (
            "Stale worker lease exhausted the maximum of "
            f"{MAX_GENERATION_ATTEMPTS} generation attempts"
        )
        job.finished_at = now
        return None

    job.status = JobStatus.queued
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
    try:
        result = _generate(job, version)
        _complete_job(job_id, organization_id, version, result)
        return result
    except Exception as exc:
        terminal = self.request.retries >= self.max_retries
        _record_failure(job_id, organization_id, exc, terminal=terminal)
        if terminal or isinstance(exc, ProductionBlockedError):
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
        if (
            job.status == JobStatus.running
            and job.started_at
            and job.started_at > _utcnow() - timedelta(minutes=30)
        ):
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
        job.started_at = _utcnow()
        job.finished_at = None
        job.attempts += 1
        job.error = None
        session.flush()
        session.expunge(job)
        session.expunge(version)
        return job, version


def _generate(job: GenerationJob, version: DesignVersion) -> dict[str, Any]:
    resolved = _resolve_current_job_context(job, version)
    spec = BookcaseDesignSpec.model_validate(version.spec_json)
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
    machine = resolved.machine
    carcass_stock = StockSheet(
        stock_id=(
            f"stock-{spec.material.material_id}-{request['stock_width_mm']}x"
            f"{request['stock_height_mm']}"
        ),
        material_id=spec.material.material_id,
        material_version=spec.material.version,
        width_um=int(round(float(request["stock_width_mm"]) * 1000)),
        height_um=int(round(float(request["stock_height_mm"]) * 1000)),
        thickness_um=spec.parameters.actual_thickness_um,
        quantity=int(request["stock_count"]),
        grain_direction=spec.material.grain_direction.value,
    )
    stocks = [carcass_stock]
    if spec.back_material is not None:
        stocks.append(
            StockSheet(
                stock_id=(
                    f"stock-{spec.back_material.material_id}-"
                    f"{request['back_stock_width_mm']}x{request['back_stock_height_mm']}"
                ),
                material_id=spec.back_material.material_id,
                material_version=spec.back_material.version,
                width_um=int(round(float(request["back_stock_width_mm"]) * 1000)),
                height_um=int(round(float(request["back_stock_height_mm"]) * 1000)),
                thickness_um=spec.parameters.back_thickness_um,
                quantity=int(request["back_stock_count"]),
                grain_direction=spec.back_material.grain_direction.value,
            )
        )
    context = ManifestContext(
        project_id=version.project_id,
        revision=str(version.revision),
        design_hash=design.design_hash,
        app_version=APP_VERSION,
        engine_version=version.engine_version,
        template_version=version.template_version,
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
    )
    documents = (
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
            "ASSEMBLY_MANUAL",
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
    )
    bundle = build_production_bundle(
        design,
        stock=tuple(stocks),
        machine=machine,
        context=context,
        include_step=bool(request["include_step"]),
        include_validation_program=bool(request["include_validation_program"]),
        production_release=False,
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
    bundle_key = (
        f"{organization_id_safe(job.organization_id)}/{version.design_hash}/"
        f"{job.production_context_hash}/{bundle_sha}/production.zip"
    )
    manifest_key = (
        f"{organization_id_safe(job.organization_id)}/{version.design_hash}/"
        f"{job.production_context_hash}/{manifest_sha}/manifest.json"
    )
    _put_object(bundle_key, bundle.zip_bytes, "application/zip")
    _put_object(manifest_key, manifest_bytes, "application/json")
    return {
        "bundle_sha256": bundle_sha,
        "bundle_size_bytes": len(bundle.zip_bytes),
        "manifest_sha256": manifest_sha,
        "manifest_size_bytes": len(manifest_bytes),
        "bundle_object_key": bundle_key,
        "manifest_object_key": manifest_key,
        "artifact_count": len(bundle.artifacts) + 1,
        "generation_context_hash": job.production_context_hash,
        "production_engine_context_hash": resolved.context.fingerprint,
        "dfm_status": bundle.dfm_report.status.value,
        "nesting_utilization_ppm": (
            sum(layout.utilization_ppm for layout in bundle.layouts) // len(bundle.layouts)
        ),
        "used_sheet_count": sum(layout.used_sheet_count for layout in bundle.layouts),
        "nesting_layouts": [
            {
                "stock_id": layout.stock_id,
                "utilization_ppm": layout.utilization_ppm,
                "used_sheet_count": layout.used_sheet_count,
            }
            for layout in bundle.layouts
        ],
        "authoritative_geometry": bool(request["include_step"]),
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }


def _resolve_current_job_context(
    job: GenerationJob,
    version: DesignVersion,
) -> ResolvedProductionComponents:
    """Recompute and equality-guard code/catalog identity before generation."""

    try:
        assert_frozen_design_versions(
            engine_version=version.engine_version,
            template_version=version.template_version,
            rule_version=version.rule_version,
        )
        current = resolve_production_components(
            machine_profile_id=str(job.request_json["machine_profile_id"]),
            postprocessor_id=str(job.request_json["postprocessor_id"]),
            app_version=APP_VERSION,
            require_cad_runtime=bool(job.request_json.get("include_step", False)),
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
    version: DesignVersion,
    result: dict[str, Any],
) -> None:
    with SessionFactory.begin() as session:
        job = session.scalar(
            select(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.organization_id == organization_id,
            )
            .with_for_update()
        )
        if job is None:
            raise RuntimeError("Generation job disappeared before completion")
        if job.status == JobStatus.cancelled:
            return
        job.status = JobStatus.succeeded
        job.result_json = result
        job.finished_at = _utcnow()
        for kind, key, digest, media_type, size_bytes in (
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
        ):
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


def _record_failure(
    job_id: str,
    organization_id: str,
    exc: Exception,
    *,
    terminal: bool,
) -> None:
    with SessionFactory.begin() as session:
        job = session.scalar(
            select(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.organization_id == organization_id,
            )
            .with_for_update()
        )
        if job is None:
            return
        if job.status == JobStatus.cancelled:
            return
        job.status = (
            JobStatus.failed
            if terminal or isinstance(exc, ProductionBlockedError)
            else JobStatus.queued
        )
        job.error = f"{type(exc).__name__}: {exc}"[:4000]
        job.finished_at = _utcnow() if job.status == JobStatus.failed else None


def _put_object(key: str, payload: bytes, content_type: str) -> None:
    client = _s3_client()
    bucket = WORKER_SETTINGS.s3_bucket
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status_code != 404:
            raise
        client.create_bucket(Bucket=bucket)
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentLength=len(payload),
        ContentType=content_type,
        Metadata={"sha256": sha256_hex(payload)},
    )


def _s3_client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=WORKER_SETTINGS.s3_endpoint,
        aws_access_key_id=WORKER_SETTINGS.s3_access_key,
        aws_secret_access_key=WORKER_SETTINGS.s3_secret_key,
        region_name="us-east-1",
    )


def organization_id_safe(value: str) -> str:
    if not value or any(character not in "0123456789abcdef-" for character in value.lower()):
        raise ValueError("Invalid organization identifier for object key")
    return value.lower()
