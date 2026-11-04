"""Save a distinct construction source and enqueue the existing durable review job."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from custombuild_domain.furniture import FURNITURE_ENGINE_VERSION
from custombuild_domain.identity import content_hash
from custombuild_domain.trestle_construction import construct_trestle
from custombuild_domain.trestle_planning import TrestleRequest
from custombuild_domain.trestle_snapshot import TrestleWorkspace
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .auth import Capability, require_capability
from .furniture_api import GeneratorDep, SessionDep
from .job_policy import GENERATION_JOB_TIMEOUT
from .models import GenerationJob, GenerationJobType, JobStatus, OutboxEvent, Project
from .repository import audit

router = APIRouter(prefix="/v1/constructions")


class TrestleExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: TrestleRequest
    expected_construction_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: UUID


class TrestleExportAccepted(BaseModel):
    project_id: str
    job_id: str


@router.post(
    "/trestle-table/exports",
    response_model=TrestleExportAccepted,
    status_code=202,
    dependencies=[Depends(require_capability(Capability.DESIGN))],
)
def export_trestle(
    payload: TrestleExportRequest, principal: GeneratorDep, session: SessionDep
) -> TrestleExportAccepted:
    package = construct_trestle(payload.request)
    if package.construction_sha256 != payload.expected_construction_sha256:
        raise HTTPException(409, "Konstruktionen har ändrats. Skapa och granska den på nytt.")
    key = content_hash(dict(kind="trestle-review", key=str(payload.idempotency_key)))

    def existing() -> GenerationJob | None:
        return session.scalar(
            select(GenerationJob).where(
                GenerationJob.organization_id == principal.organization_id,
                GenerationJob.idempotency_key == key,
                GenerationJob.job_type == GenerationJobType.furniture_review,
            )
        )

    def accepted(job: GenerationJob) -> TrestleExportAccepted:
        request = job.request_json
        if (
            not isinstance(request, dict)
            or content_hash(request) != job.production_context_hash
            or request.get("requested_by") != principal.user_id
            or request.get("design_hash") != package.construction_sha256
            or not isinstance(request.get("workspace"), dict)
            or request.get("workspace", {}).get("request")
            != payload.request.model_dump(mode="json")
            or job.project_id is None
        ):
            raise HTTPException(409, "Exportnyckeln tillhör ett annat underlag.")
        return TrestleExportAccepted(project_id=job.project_id, job_id=job.id)

    previous = existing()
    if previous:
        return accepted(previous)
    now = datetime.now(UTC)
    project_id, job_id = str(uuid4()), str(uuid4())
    workspace = TrestleWorkspace(project_id=project_id, revision=1, request=payload.request)
    request = dict(
        schema_version="custombuild.furniture-review-job.v1",
        job_type=GenerationJobType.furniture_review.value,
        project_id=project_id,
        revision=1,
        workspace=workspace.model_dump(mode="json"),
        workspace_sha256=content_hash(workspace),
        design_hash=package.construction_sha256,
        engine_version=FURNITURE_ENGINE_VERSION,
        requested_by=principal.user_id,
    )
    context = dict(
        job_type=GenerationJobType.furniture_review.value,
        furniture_engine_version=FURNITURE_ENGINE_VERSION,
        review_schema_version="custombuild.furniture-review.v1",
    )
    try:
        # A duplicate concurrent request rolls back its project and outbox row too.
        with session.begin_nested():
            project = Project(
                id=project_id,
                organization_id=principal.organization_id,
                name=f"{package.assembly.title} / {project_id[:8]}",
                furniture_type="trestle_table",
                draft_revision=1,
                draft_design_hash=package.construction_sha256,
                draft_spec_json=workspace.model_dump(mode="json"),
                draft_result_json=None,
                draft_updated_by=principal.user_id,
            )
            session.add(project)
            session.flush()
            job = GenerationJob(
                id=job_id,
                organization_id=principal.organization_id,
                job_type=GenerationJobType.furniture_review,
                project_id=project_id,
                design_version_id=None,
                status=JobStatus.queued,
                idempotency_key=key,
                request_json=request,
                production_context_hash=content_hash(request),
                production_engine_context_json=context,
                deadline_at=now + GENERATION_JOB_TIMEOUT,
                next_attempt_at=now,
            )
            session.add(job)
            session.add(
                OutboxEvent(
                    organization_id=principal.organization_id,
                    event_key=f"furniture-review:{job_id}",
                    topic="furniture.review.requested",
                    payload_json=dict(job_id=job_id, organization_id=principal.organization_id),
                )
            )
            audit(
                session,
                principal,
                "trestle.export.requested",
                "project",
                project_id,
                dict(job_id=job_id, design_hash=package.construction_sha256, revision=1),
            )
            session.flush()
    except IntegrityError:
        previous = existing()
        if previous:
            return accepted(previous)
        raise
    return TrestleExportAccepted(project_id=project_id, job_id=job_id)
