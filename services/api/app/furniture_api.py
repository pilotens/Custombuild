"""Furniture design drafts and profile changes, separate from production release."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import uuid4

from custombuild_domain.furniture import (
    FURNITURE_ENGINE_VERSION,
    FurnitureWorkspace,
    ProfileChange,
)
from custombuild_domain.furniture_catalog import furniture_catalog
from custombuild_domain.identity import content_hash
from custombuild_manufacturing.furniture_module_planning import (
    FurnitureModuleGrid,
    plan_furniture_modules,
)
from custombuild_manufacturing.furniture_profiles import (
    compare_furniture_profiles,
    furniture_machine_catalog,
    preview_furniture,
)
from custombuild_manufacturing.furniture_suggestions import suggest_shelf_bays
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .api import (
    _ARTIFACT_STREAM_OPERATION_DEPENDENCY,
    DownloadSessionDep,
    _verified_artifact_response,
)
from .auth import Capability, Principal, get_principal, require_capability
from .furniture_schemas import (
    FurnitureCatalogRead,
    FurnitureDraftRead,
    FurnitureHistoryRead,
    FurniturePreviewRead,
    FurnitureProfileChangeRead,
    FurnitureShelfSuggestionRead,
)
from .job_policy import GENERATION_JOB_TIMEOUT
from .models import (
    Artifact,
    AuditEvent,
    DesignStatus,
    DesignVersion,
    GenerationJob,
    GenerationJobType,
    JobStatus,
    OutboxEvent,
    Project,
)
from .repository import audit, tenant_project, tenant_session
from .storage import (
    ArtifactIntegrityError,
    ArtifactStorageUnavailableError,
    StoredObjectExpectation,
    VerifiedStoredObject,
    open_verified_stored_object,
)
from .storage_quota import (
    StorageClaimConflict,
    StorageQuotaInvariantError,
    require_committed_storage_binding,
)

router = APIRouter(prefix="/v1/furniture", tags=["furniture-design"])
SessionDep = Annotated[Session, Depends(tenant_session, scope="function")]
ReaderDep = Annotated[Principal, Depends(get_principal)]
DesignerDep = Annotated[Principal, Depends(require_capability(Capability.DESIGN))]
GeneratorDep = Annotated[Principal, Depends(require_capability(Capability.GENERATE))]
_SCHEMA = "custombuild.furniture-design.v1"
_EXPORT_JOB_SCHEMA = "custombuild.furniture-review-job.v1"
_EXPORT_REVIEW_SCHEMA = "custombuild.furniture-review.v1"
_EXPORT_ARTIFACT_KIND = "furniture_review_bundle"
_EXPORT_CONTENT_TYPE = "application/zip"
_MAX_EXPORT_BYTES = 16 * 1024 * 1024
_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


class FurnitureDraftSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    workspace: FurnitureWorkspace


class FurnitureExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    expected_design_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class FurnitureExportAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=_UUID_PATTERN)
    state: Literal["queued", "running", "succeeded"]


class FurnitureExportPending(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=_UUID_PATTERN)
    state: Literal["queued", "running"]


class FurnitureExportFailed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=_UUID_PATTERN)
    state: Literal["failed", "cancelled"]
    message: str = Field(min_length=1, max_length=500)


class FurnitureExportSucceeded(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=_UUID_PATTERN)
    state: Literal["succeeded"]
    artifact_id: str = Field(pattern=_UUID_PATTERN)
    file_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    download_path: str = Field(
        pattern=r"^/v1/furniture/projects/[0-9a-f-]+/exports/[0-9a-f-]+/download$"
    )
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(strict=True, gt=0, le=_MAX_EXPORT_BYTES)
    content_type: Literal["application/zip"]
    design_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: int = Field(strict=True, ge=1)
    physical_cutting_authorized: Literal[False]


FurnitureExportStatusRead = Annotated[
    FurnitureExportPending | FurnitureExportFailed | FurnitureExportSucceeded,
    Field(discriminator="state"),
]


class FurnitureModulePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace: FurnitureWorkspace
    grid: FurnitureModuleGrid


def _workspace(project: Project) -> FurnitureWorkspace | None:
    if project.draft_spec_json is None:
        return None
    if project.draft_spec_json.get("schema_version") != _SCHEMA:
        raise HTTPException(
            409, detail="Projektet använder den tidigare studion. Skapa ett nytt projekt."
        )
    return FurnitureWorkspace.model_validate(
        {
            "design": project.draft_spec_json,
            "manufacturing": (project.draft_workspace_json or {}).get("manufacturing"),
        }
    )


def _preview(workspace: FurnitureWorkspace) -> dict[str, Any]:
    try:
        return preview_furniture(workspace)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get("/catalog", response_model=FurnitureCatalogRead)
def catalog(principal: ReaderDep, session: SessionDep) -> dict[str, Any]:
    return {**furniture_catalog(), "machines": furniture_machine_catalog()}


@router.post("/preview", response_model=FurniturePreviewRead, response_model_exclude_unset=True)
def preview(
    payload: FurnitureWorkspace, principal: ReaderDep, session: SessionDep
) -> dict[str, Any]:
    return _preview(payload)


@router.post(
    "/profile-change", response_model=FurnitureProfileChangeRead, response_model_exclude_unset=True
)
def profile_change(
    payload: ProfileChange, principal: ReaderDep, session: SessionDep
) -> dict[str, Any]:
    try:
        return compare_furniture_profiles(payload)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.post(
    "/shelf-bay-suggestion",
    response_model=FurnitureShelfSuggestionRead,
    response_model_exclude_unset=True,
)
def shelf_bay_suggestion(
    payload: FurnitureWorkspace, principal: ReaderDep, session: SessionDep
) -> dict[str, Any]:
    try:
        return suggest_shelf_bays(payload)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.post("/module-plan")
def module_plan(
    payload: FurnitureModulePlanRequest, principal: ReaderDep, session: SessionDep
) -> dict[str, Any]:
    try:
        return plan_furniture_modules(payload.workspace, payload.grid)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get(
    "/projects/{project_id}/draft",
    response_model=FurnitureDraftRead,
    response_model_exclude_unset=True,
)
def read_draft(project_id: str, principal: ReaderDep, session: SessionDep) -> dict[str, Any]:
    project = tenant_project(session, principal, project_id)
    workspace = _workspace(project)
    return {
        "project_id": project.id,
        "revision": project.draft_revision,
        "workspace": workspace.model_dump(mode="json") if workspace else None,
        "preview": _preview(workspace) if workspace else None,
    }


@router.put(
    "/projects/{project_id}/draft",
    response_model=FurnitureDraftRead,
    response_model_exclude_unset=True,
)
def save_draft(
    project_id: str,
    payload: FurnitureDraftSave,
    principal: DesignerDep,
    session: SessionDep,
) -> dict[str, Any]:
    project = tenant_project(session, principal, project_id)
    session.refresh(project, with_for_update=True)
    current = _workspace(project)  # Protect an existing legacy draft from replacement.
    if project.draft_revision != payload.expected_revision:
        raise HTTPException(
            409, detail="Utkastet har ändrats. Hämta aktuell revision före nästa sparning."
        )
    document = payload.workspace.model_dump(mode="json")
    if current is not None:
        # Project identity and revision belong to the server, including when a
        # client retries an unchanged draft with its original local identity.
        document["design"].update(design_id=project.id, revision=project.draft_revision)
        candidate = FurnitureWorkspace.model_validate(document)
        if candidate == current:
            current_preview = _preview(current)
            # Re-saving unchanged inputs must still create a new revision when
            # engine, rule or profile changes alter the committed assessment.
            if (
                current_preview["design"]["design_hash"] == project.draft_design_hash
                and current_preview == project.draft_result_json
            ):
                return {
                    "project_id": project.id,
                    "revision": project.draft_revision,
                    "workspace": current.model_dump(mode="json"),
                    "preview": current_preview,
                }
    revision = project.draft_revision + 1
    document["design"].update(design_id=project.id, revision=revision)
    workspace = FurnitureWorkspace.model_validate(document)
    preview_result = _preview(workspace)
    project.draft_revision = revision
    project.draft_template_id = workspace.design.intent.family
    project.furniture_type = workspace.design.intent.family
    project.draft_design_hash = preview_result["design"]["design_hash"]
    project.draft_spec_json = workspace.design.model_dump(mode="json")
    project.draft_workspace_json = {"manufacturing": document["manufacturing"]}
    project.draft_result_json = preview_result
    project.draft_updated_by = principal.user_id
    # A saved furniture/profile change immediately invalidates derived production
    # revisions, including batch-only changes that leave all cut geometry intact.
    derived = list(
        session.scalars(
            select(DesignVersion)
            .where(
                DesignVersion.organization_id == principal.organization_id,
                DesignVersion.project_id == project.id,
                DesignVersion.status.not_in([DesignStatus.superseded, DesignStatus.archived]),
            )
            .with_for_update()
        )
    )
    derived_ids = []
    for version in derived:
        if version.result_json.get("source_furniture") is None:
            continue
        version.status = DesignStatus.superseded
        version.immutable = True
        derived_ids.append(version.id)
        audit(
            session,
            principal,
            "design_version.superseded",
            "design_version",
            version.id,
            {"source_furniture_revision": revision, "reason": "saved_furniture_changed"},
        )
    if derived_ids:
        active_jobs = session.scalars(
            select(GenerationJob)
            .where(
                GenerationJob.organization_id == principal.organization_id,
                GenerationJob.design_version_id.in_(derived_ids),
                GenerationJob.status.in_([JobStatus.queued, JobStatus.running]),
            )
            .with_for_update()
        )
        for job in active_jobs:
            job.status = JobStatus.cancelled
            job.lease_token = None
            job.lease_expires_at = None
            job.next_attempt_at = None
            job.finished_at = datetime.now(UTC)
            job.error = "Cancelled because the saved furniture or profiles changed"
    active_review_jobs = session.scalars(
        select(GenerationJob)
        .where(
            GenerationJob.organization_id == principal.organization_id,
            GenerationJob.job_type == GenerationJobType.furniture_review,
            GenerationJob.project_id == project.id,
            GenerationJob.status.in_([JobStatus.queued, JobStatus.running]),
        )
        .with_for_update()
    )
    for job in active_review_jobs:
        job.status = JobStatus.cancelled
        job.lease_token = None
        job.lease_expires_at = None
        job.next_attempt_at = None
        job.finished_at = datetime.now(UTC)
        job.error = "Cancelled because the saved furniture or profiles changed"
    audit(
        session,
        principal,
        "furniture.draft.saved",
        "project",
        project.id,
        {
            "revision": revision,
            "workspace": workspace.model_dump(mode="json"),
            "design_hash": project.draft_design_hash,
            "engine_version": FURNITURE_ENGINE_VERSION,
            "dependencies": preview_result["dependencies"],
        },
    )
    session.flush()
    return {
        "project_id": project.id,
        "revision": revision,
        "workspace": workspace.model_dump(mode="json"),
        "preview": preview_result,
    }


@router.get(
    "/projects/{project_id}/history",
    response_model=FurnitureHistoryRead,
    response_model_exclude_unset=True,
)
def history(
    project_id: str,
    principal: ReaderDep,
    session: SessionDep,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> dict[str, Any]:
    tenant_project(session, principal, project_id)
    events = list(
        session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.organization_id == principal.organization_id,
                AuditEvent.entity_type == "project",
                AuditEvent.entity_id == project_id,
                AuditEvent.action == "furniture.draft.saved",
            )
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
            .offset(offset)
            .limit(26)
        )
    )
    return {
        "items": [{"id": e.id, "created_at": e.occurred_at, **e.payload_json} for e in events[:25]],
        "next_offset": offset + 25 if len(events) > 25 else None,
    }


def _accepted_export(job: GenerationJob) -> FurnitureExportAccepted:
    state = job.status.value
    if state not in {"queued", "running", "succeeded"}:
        raise RuntimeError("only active or successful furniture jobs can be reused")
    return FurnitureExportAccepted(job_id=job.id, state=state)


@router.post(
    "/projects/{project_id}/exports",
    status_code=202,
    response_model=FurnitureExportAccepted,
)
def request_export(
    project_id: str,
    payload: FurnitureExportRequest,
    principal: GeneratorDep,
    session: SessionDep,
) -> FurnitureExportAccepted:
    project = tenant_project(session, principal, project_id)
    session.refresh(project, with_for_update=True)
    workspace = _workspace(project)
    if (
        workspace is None
        or project.draft_revision != payload.expected_revision
        or (project.draft_design_hash != payload.expected_design_hash)
    ):
        raise HTTPException(409, detail="Spara och granska den aktuella revisionen före export.")
    preview_result = _preview(workspace)
    if len(preview_result["design"]["parts"]) > 128:
        raise HTTPException(422, detail="Granskningspaketet stöder högst 128 delar.")
    if preview_result["design"]["design_hash"] != project.draft_design_hash:
        raise HTTPException(
            409, detail="Modellmotorn har ändrats. Spara en ny revision före export."
        )
    workspace_sha256 = content_hash(workspace)
    now = datetime.now(UTC)
    engine_context = {
        "job_type": GenerationJobType.furniture_review.value,
        "furniture_engine_version": FURNITURE_ENGINE_VERSION,
        "review_schema_version": _EXPORT_REVIEW_SCHEMA,
    }
    # The project row lock serializes duplicate requests across API replicas.
    reusable_jobs = session.scalars(
        select(GenerationJob)
        .where(
            GenerationJob.organization_id == principal.organization_id,
            GenerationJob.job_type == GenerationJobType.furniture_review,
            GenerationJob.project_id == project.id,
            GenerationJob.status.in_([JobStatus.queued, JobStatus.running, JobStatus.succeeded]),
            or_(
                GenerationJob.status == JobStatus.succeeded,
                GenerationJob.deadline_at > now,
            ),
        )
        .order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc())
    )
    for recent in reusable_jobs:
        request = recent.request_json
        if (
            isinstance(request, dict)
            and request.get("schema_version") == _EXPORT_JOB_SCHEMA
            and request.get("job_type") == GenerationJobType.furniture_review.value
            and request.get("project_id") == project.id
            and request.get("revision") == project.draft_revision
            and request.get("design_hash") == project.draft_design_hash
            and request.get("workspace_sha256") == workspace_sha256
            and request.get("engine_version") == FURNITURE_ENGINE_VERSION
            and recent.production_engine_context_json == engine_context
            and content_hash(request) == recent.production_context_hash
        ):
            return _accepted_export(recent)
    job_id = str(uuid4())
    request_document = {
        "schema_version": _EXPORT_JOB_SCHEMA,
        "job_type": GenerationJobType.furniture_review.value,
        "project_id": project.id,
        "revision": project.draft_revision,
        "workspace": workspace.model_dump(mode="json"),
        "workspace_sha256": workspace_sha256,
        "design_hash": project.draft_design_hash,
        "engine_version": FURNITURE_ENGINE_VERSION,
        "requested_by": principal.user_id,
    }
    job = GenerationJob(
        id=job_id,
        organization_id=principal.organization_id,
        job_type=GenerationJobType.furniture_review,
        project_id=project.id,
        design_version_id=None,
        status=JobStatus.queued,
        idempotency_key=content_hash(
            {
                "schema_version": _EXPORT_JOB_SCHEMA,
                "job_id": job_id,
                "request": request_document,
            }
        ),
        production_context_hash=content_hash(request_document),
        production_engine_context_json=engine_context,
        request_json=request_document,
        deadline_at=now + GENERATION_JOB_TIMEOUT,
        next_attempt_at=now,
    )
    session.add(job)
    session.add(
        OutboxEvent(
            organization_id=principal.organization_id,
            event_key=f"furniture-review:{job_id}",
            topic="furniture.review.requested",
            payload_json={
                "job_id": job_id,
                "organization_id": principal.organization_id,
            },
        )
    )
    audit(
        session,
        principal,
        "furniture.export.requested",
        "project",
        project.id,
        {
            "job_id": job_id,
            "revision": project.draft_revision,
            "design_hash": project.draft_design_hash,
        },
    )
    session.flush()
    return FurnitureExportAccepted(job_id=job_id, state="queued")


def _furniture_export_artifact(
    session: Session,
    job: GenerationJob,
) -> tuple[Artifact, dict[str, Any], dict[str, Any]]:
    request = job.request_json
    result = job.result_json
    if (
        job.status != JobStatus.succeeded
        or job.project_id is None
        or not isinstance(request, dict)
        or request.get("schema_version") != _EXPORT_JOB_SCHEMA
        or request.get("project_id") != job.project_id
        or request.get("job_type") != GenerationJobType.furniture_review.value
        or type(request.get("revision")) is not int
        or request["revision"] < 1
        or content_hash(request) != job.production_context_hash
        or not isinstance(result, dict)
        or result.get("job_context_hash") != job.production_context_hash
        or result.get("design_hash") != request.get("design_hash")
        or result.get("workspace_sha256") != request.get("workspace_sha256")
        or result.get("revision") != request.get("revision")
        or result.get("file_name") != f"custombuild-design-review-{request['revision']}.zip"
        or result.get("physical_cutting_authorized") is not False
    ):
        raise HTTPException(409, detail="Exportens identitet stämmer inte med beställningen.")
    artifact = session.scalar(
        select(Artifact).where(
            Artifact.organization_id == job.organization_id,
            Artifact.generation_job_id == job.id,
            Artifact.kind == _EXPORT_ARTIFACT_KIND,
        )
    )
    if (
        artifact is None
        or artifact.id != result.get("artifact_id")
        or artifact.object_key != result.get("object_key")
        or artifact.sha256 != result.get("sha256")
        or artifact.size_bytes != result.get("size_bytes")
        or artifact.content_type != _EXPORT_CONTENT_TYPE
        or result.get("content_type") != _EXPORT_CONTENT_TYPE
    ):
        raise HTTPException(409, detail="Exportens artefaktinventering är ogiltig.")
    try:
        stored_object = require_committed_storage_binding(
            session,
            job.organization_id,
            project_id=job.project_id,
            object_key=artifact.object_key,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            media_type=artifact.content_type,
            owner_type="generation_job",
            owner_id=job.id,
        )
        if stored_object.idempotency_key != (
            f"generation:{job.id}:{_EXPORT_ARTIFACT_KIND}:{artifact.id}"
        ):
            raise StorageQuotaInvariantError(
                "furniture review artifact idempotency identity is invalid"
            )
    except (StorageClaimConflict, StorageQuotaInvariantError) as exc:
        raise HTTPException(
            409,
            detail="Exportens lagringsidentitet är inte fullständigt bekräftad.",
        ) from exc
    return artifact, result, request


@router.get(
    "/projects/{project_id}/exports/{job_id}",
    response_model=FurnitureExportStatusRead,
)
def export_result(
    project_id: str,
    job_id: str,
    principal: ReaderDep,
    session: SessionDep,
) -> FurnitureExportStatusRead:
    tenant_project(session, principal, project_id)
    job = session.scalar(
        select(GenerationJob).where(
            GenerationJob.id == job_id,
            GenerationJob.organization_id == principal.organization_id,
            GenerationJob.job_type == GenerationJobType.furniture_review,
            GenerationJob.project_id == project_id,
        )
    )
    if job is None:
        raise HTTPException(404, detail="Exporten finns inte i detta projekt.")
    deadline = job.deadline_at
    if (
        job.status in {JobStatus.queued, JobStatus.running}
        and deadline is not None
        and (deadline.replace(tzinfo=UTC) if deadline.tzinfo is None else deadline)
        <= datetime.now(UTC)
    ):
        return FurnitureExportFailed(
            job_id=job.id,
            state="failed",
            message="Exportens tidsgräns passerades. Skapa ett nytt paket.",
        )
    if job.status in {JobStatus.queued, JobStatus.running}:
        return FurnitureExportPending(job_id=job.id, state=job.status.value)
    if job.status in {JobStatus.failed, JobStatus.cancelled}:
        return FurnitureExportFailed(
            job_id=job.id,
            state=job.status.value,
            message=(
                "Exporten avbröts innan någon fil godkändes."
                if job.status == JobStatus.cancelled
                else "CAD-/dokumentkontrollen misslyckades. Ingen fil godkändes."
            ),
        )
    artifact, result, _request = _furniture_export_artifact(session, job)
    return FurnitureExportSucceeded(
        job_id=job.id,
        state="succeeded",
        artifact_id=artifact.id,
        file_name=str(result["file_name"]),
        download_path=f"/v1/furniture/projects/{project_id}/exports/{job.id}/download",
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
        content_type="application/zip",
        design_hash=str(result["design_hash"]),
        revision=int(result["revision"]),
        physical_cutting_authorized=False,
    )


@router.get(
    "/projects/{project_id}/exports/{job_id}/download",
    response_class=StreamingResponse,
    dependencies=[_ARTIFACT_STREAM_OPERATION_DEPENDENCY],
    responses={
        200: {
            "description": "Fully verified furniture review archive",
            "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
def download_export(
    project_id: str,
    job_id: str,
    principal: ReaderDep,
    session: DownloadSessionDep,
) -> StreamingResponse:
    tenant_project(session, principal, project_id)
    job = session.scalar(
        select(GenerationJob).where(
            GenerationJob.id == job_id,
            GenerationJob.organization_id == principal.organization_id,
            GenerationJob.job_type == GenerationJobType.furniture_review,
            GenerationJob.project_id == project_id,
            GenerationJob.status == JobStatus.succeeded,
        )
    )
    if job is None:
        raise HTTPException(404, detail="Exporten finns inte i detta projekt.")
    artifact, result, request = _furniture_export_artifact(session, job)
    verified: VerifiedStoredObject | None = None
    try:
        verified = open_verified_stored_object(
            StoredObjectExpectation(
                object_key=artifact.object_key,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                content_type=artifact.content_type,
                required_metadata=(
                    ("furniture-design-hash", str(request["design_hash"])),
                    ("workspace-sha256", str(request["workspace_sha256"])),
                ),
            ),
            max_bytes=_MAX_EXPORT_BYTES,
        )
        return _verified_artifact_response(
            verified,
            filename=str(result["file_name"]),
        )
    except ArtifactIntegrityError as exc:
        if verified is not None:
            verified.close()
        raise HTTPException(
            409,
            detail="Exportens lagrade bytes klarade inte integritetskontrollen.",
        ) from exc
    except ArtifactStorageUnavailableError as exc:
        if verified is not None:
            verified.close()
        raise HTTPException(503, detail="Exportlagringen kan inte verifieras just nu.") from exc
