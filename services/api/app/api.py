from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from typing import Annotated, Any

from custombuild_domain import joint_support_payload
from custombuild_manufacturing.production_context import (
    ProductionContextError,
    assert_frozen_design_versions,
    contexts_equal,
    generation_context_hash,
    resolve_production_components,
)
from custombuild_rules import RULES_VERSION
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import __version__ as APP_VERSION
from .auth import Principal, get_principal, require_minimum_role
from .config import get_settings
from .design_service import auto_fix, preview
from .models import (
    Approval,
    Artifact,
    DesignStatus,
    DesignVersion,
    GenerationJob,
    JobStatus,
    OutboxEvent,
    Project,
    Release,
    Role,
)
from .repository import audit, canonical_hash, tenant_project, tenant_session, tenant_version
from .schemas import (
    ApprovalCreate,
    ArtifactRead,
    BookcasePreviewInput,
    DesignVersionCreate,
    DesignVersionRead,
    GenerationRequest,
    ImportInspection,
    JobRead,
    ProjectCreate,
    ProjectRead,
    ReleaseCreate,
    ReleaseRead,
)
from .security import validate_upload
from .storage import presigned_get, sign_artifact_access, verify_artifact_access

router = APIRouter(prefix="/v1")
SessionDep = Annotated[Session, Depends(tenant_session)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
DesignerDep = Annotated[Principal, Depends(require_minimum_role(Role.designer))]
ReviewerDep = Annotated[Principal, Depends(require_minimum_role(Role.reviewer))]


def _validation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValidationError):
        detail: Any = exc.errors(include_url=False)
    else:
        detail = str(exc)
    return HTTPException(status_code=422, detail=detail)


def _require_current_artifacts(
    session: Session,
    principal: Principal,
    job: GenerationJob,
) -> DesignVersion:
    version = session.scalar(
        select(DesignVersion).where(
            DesignVersion.id == job.design_version_id,
            DesignVersion.organization_id == principal.organization_id,
        )
    )
    if version is None:
        raise HTTPException(status_code=404, detail="Design version not found")
    project = session.scalar(
        select(Project).where(
            Project.id == version.project_id,
            Project.organization_id == principal.organization_id,
        )
    )
    if (
        project is None
        or project.archived
        or project.current_revision != version.revision
        or version.status in {DesignStatus.superseded, DesignStatus.archived}
    ):
        raise HTTPException(
            status_code=409,
            detail="Production artifacts are stale after a design change",
        )
    _require_current_generation_context(job, version)
    return version


def _artifact_filename(kind: str, revision: int) -> str | None:
    return {
        "production_bundle": f"custombuild-rev-{revision}.zip",
        "manifest": f"custombuild-rev-{revision}-manifest.json",
    }.get(kind)


def _require_current_generation_context(
    job: GenerationJob,
    version: DesignVersion,
) -> None:
    """Reject a job frozen against any superseded production implementation."""

    try:
        assert_frozen_design_versions(
            engine_version=version.engine_version,
            template_version=version.template_version,
            rule_version=version.rule_version,
        )
        machine_profile_id = str(job.request_json["machine_profile_id"])
        postprocessor_id = str(job.request_json["postprocessor_id"])
        current = resolve_production_components(
            machine_profile_id=machine_profile_id,
            postprocessor_id=postprocessor_id,
            app_version=APP_VERSION,
        ).context
        if not contexts_equal(job.production_engine_context_json, current):
            raise ProductionContextError("production engine context has drifted")
        expected_hash = generation_context_hash(
            design_context_hash=version.context_hash,
            design_version_id=version.id,
            revision=version.revision,
            request=job.request_json,
            production_engine_context=current,
        )
        if expected_hash != job.production_context_hash:
            raise ProductionContextError("generation context hash does not match the frozen job")
    except (KeyError, ProductionContextError) as exc:
        raise HTTPException(
            status_code=409,
            detail="Production libraries or catalog data changed; generate a new job",
        ) from exc


@router.get("/me")
def me(principal: PrincipalDep) -> dict[str, str]:
    return {
        "user_id": principal.user_id,
        "organization_id": principal.organization_id,
        "role": principal.role.value,
        "name": principal.name,
        "email": principal.email,
    }


@router.get("/capabilities/joints")
def joint_capabilities(principal: PrincipalDep) -> dict[str, object]:
    """Expose the versioned support claim instead of implying enum-wide support."""

    return joint_support_payload()


@router.post("/designs/preview")
def preview_design(payload: BookcasePreviewInput, principal: PrincipalDep) -> dict[str, Any]:
    try:
        _, _, presented = preview(payload.model_dump(exclude_none=True), design_id="preview")
        return presented
    except (ValueError, ValidationError) as exc:
        raise _validation_error(exc) from exc


@router.post("/designs/autofix")
def autofix_design(payload: BookcasePreviewInput, principal: DesignerDep) -> dict[str, Any]:
    try:
        _, _, presented = auto_fix(payload.model_dump(exclude_none=True), design_id="preview")
        return presented
    except (ValueError, ValidationError) as exc:
        raise _validation_error(exc) from exc


@router.get("/projects", response_model=list[ProjectRead])
def list_projects(session: SessionDep, principal: PrincipalDep) -> list[Project]:
    return list(
        session.scalars(
            select(Project)
            .where(
                Project.organization_id == principal.organization_id,
                Project.archived.is_(False),
            )
            .order_by(Project.updated_at.desc())
        )
    )


@router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    session: SessionDep,
    principal: DesignerDep,
) -> Project:
    project = Project(
        organization_id=principal.organization_id,
        name=payload.name,
        description=payload.description,
        furniture_type=payload.furniture_type,
    )
    session.add(project)
    try:
        session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="A project with this name already exists"
        ) from exc
    audit(session, principal, "project.created", "project", project.id)
    return project


@router.get("/projects/{project_id}", response_model=ProjectRead)
def get_project(project_id: str, session: SessionDep, principal: PrincipalDep) -> Project:
    return tenant_project(session, principal, project_id)


@router.get("/projects/{project_id}/versions", response_model=list[DesignVersionRead])
def list_versions(
    project_id: str,
    session: SessionDep,
    principal: PrincipalDep,
) -> list[DesignVersion]:
    project = tenant_project(session, principal, project_id)
    return list(
        session.scalars(
            select(DesignVersion)
            .where(
                DesignVersion.project_id == project.id,
                DesignVersion.organization_id == principal.organization_id,
            )
            .order_by(DesignVersion.revision.desc())
        )
    )


@router.post(
    "/projects/{project_id}/versions",
    response_model=DesignVersionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_version(
    project_id: str,
    payload: DesignVersionCreate,
    session: SessionDep,
    principal: DesignerDep,
) -> DesignVersion:
    project = tenant_project(session, principal, project_id)
    session.refresh(project, with_for_update=True)
    revision = project.current_revision + 1
    try:
        spec, result, presented = preview(
            payload.spec.model_dump(exclude_none=True),
            design_id=project.id,
            revision=revision,
        )
    except (ValueError, ValidationError) as exc:
        raise _validation_error(exc) from exc

    existing = session.scalar(
        select(DesignVersion).where(
            DesignVersion.organization_id == principal.organization_id,
            DesignVersion.project_id == project.id,
            DesignVersion.revision == project.current_revision,
            DesignVersion.design_hash == result.design_hash,
        )
    )
    if existing is not None and not existing.immutable:
        return existing

    materials = [
        {
            "material_id": spec.material.material_id,
            "version": spec.material.version,
        }
    ]
    if spec.back_material is not None:
        materials.append(
            {
                "material_id": spec.back_material.material_id,
                "version": spec.back_material.version,
            }
        )
    context_hash = canonical_hash(
        {
            "design_hash": result.design_hash,
            "engine_version": result.engine_version,
            "template_version": result.template_version,
            "rule_version": f"bookcase-rules@{RULES_VERSION}",
            "materials": sorted(
                materials,
                key=lambda item: (item["material_id"], item["version"]),
            ),
        }
    )

    prior_versions = list(
        session.scalars(
            select(DesignVersion).where(
                DesignVersion.organization_id == principal.organization_id,
                DesignVersion.project_id == project.id,
                DesignVersion.status != DesignStatus.archived,
            )
        )
    )
    prior_ids = [item.id for item in prior_versions]
    for prior in prior_versions:
        if prior.status != DesignStatus.superseded:
            prior.status = DesignStatus.superseded
            prior.immutable = True
            audit(
                session,
                principal,
                "design_version.superseded",
                "design_version",
                prior.id,
                {"superseded_by_revision": revision},
            )
    if prior_ids:
        active_jobs = session.scalars(
            select(GenerationJob).where(
                GenerationJob.organization_id == principal.organization_id,
                GenerationJob.design_version_id.in_(prior_ids),
                GenerationJob.status.in_([JobStatus.queued, JobStatus.running]),
            )
        )
        for job in active_jobs:
            job.status = JobStatus.cancelled
            job.finished_at = datetime.now(UTC)
            job.error = "Cancelled because the design revision was superseded"
    version = DesignVersion(
        organization_id=principal.organization_id,
        project_id=project.id,
        revision=revision,
        status=DesignStatus.draft,
        design_hash=result.design_hash,
        context_hash=context_hash,
        spec_json=spec.model_dump(mode="json"),
        result_json=presented,
        engine_version=result.engine_version,
        template_version=f"bookcase@{result.template_version}",
        rule_version=f"bookcase-rules@{RULES_VERSION}",
        created_by=principal.user_id,
    )
    project.current_revision = revision
    session.add(version)
    session.flush()
    audit(
        session,
        principal,
        "design_version.created",
        "design_version",
        version.id,
        {"revision": revision, "design_hash": result.design_hash},
    )
    return version


@router.get("/projects/{project_id}/versions/{revision}", response_model=DesignVersionRead)
def get_version(
    project_id: str,
    revision: int,
    session: SessionDep,
    principal: PrincipalDep,
) -> DesignVersion:
    return tenant_version(session, principal, project_id, revision)


@router.post(
    "/projects/{project_id}/versions/{revision}/validate", response_model=DesignVersionRead
)
def validate_version(
    project_id: str,
    revision: int,
    session: SessionDep,
    principal: DesignerDep,
) -> DesignVersion:
    version = tenant_version(session, principal, project_id, revision)
    if version.immutable:
        raise HTTPException(status_code=409, detail="Immutable revisions cannot be changed")
    if version.result_json.get("status") == "BLOCK":
        raise HTTPException(status_code=409, detail="Blocking construction or DFM rules remain")
    version.status = DesignStatus.design_validated
    audit(session, principal, "design_version.validated", "design_version", version.id)
    return version


@router.post(
    "/projects/{project_id}/versions/{revision}/generate",
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_version(
    project_id: str,
    revision: int,
    payload: GenerationRequest,
    session: SessionDep,
    principal: DesignerDep,
) -> GenerationJob:
    version = tenant_version(session, principal, project_id, revision)
    if version.status not in {
        DesignStatus.design_validated,
        DesignStatus.cam_validated,
        DesignStatus.approved,
    }:
        raise HTTPException(
            status_code=409, detail="Design validation is required before generation"
        )
    request_json = payload.model_dump(mode="json")
    warning_rule_ids = {
        str(item["rule_id"])
        for item in version.result_json.get("rule_evaluations", [])
        if item.get("status") == "WARNING" and item.get("rule_id")
    }
    design_approval = session.scalar(
        select(Approval).where(
            Approval.organization_id == principal.organization_id,
            Approval.design_version_id == version.id,
            Approval.approval_type == "design",
        )
    )
    if warning_rule_ids:
        approved_rule_ids = {
            str(item.get("rule_id"))
            for item in (design_approval.overrides_json if design_approval else [])
        }
        if approved_rule_ids != warning_rule_ids:
            raise HTTPException(
                status_code=409,
                detail="Every warning requires a bound reviewer override before generation",
            )
    request_json["approved_warning_overrides"] = (
        design_approval.overrides_json if design_approval else []
    )
    try:
        assert_frozen_design_versions(
            engine_version=version.engine_version,
            template_version=version.template_version,
            rule_version=version.rule_version,
        )
    except ProductionContextError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        resolved = resolve_production_components(
            machine_profile_id=payload.machine_profile_id,
            postprocessor_id=payload.postprocessor_id,
            app_version=APP_VERSION,
        )
    except ProductionContextError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    production_context_hash = generation_context_hash(
        design_context_hash=version.context_hash,
        design_version_id=version.id,
        revision=version.revision,
        request=request_json,
        production_engine_context=resolved.context,
    )
    idempotency_key = canonical_hash(
        {"version_id": version.id, "production_context_hash": production_context_hash}
    )
    existing = session.scalar(
        select(GenerationJob).where(
            GenerationJob.organization_id == principal.organization_id,
            GenerationJob.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing
    if version.status in {DesignStatus.cam_validated, DesignStatus.approved}:
        session.execute(
            delete(Approval).where(
                Approval.organization_id == principal.organization_id,
                Approval.design_version_id == version.id,
                Approval.approval_type == "cam",
            )
        )
        version.status = DesignStatus.design_validated
    job = GenerationJob(
        organization_id=principal.organization_id,
        design_version_id=version.id,
        status=JobStatus.queued,
        idempotency_key=idempotency_key,
        production_context_hash=production_context_hash,
        production_engine_context_json=resolved.context.as_dict(),
        request_json=request_json,
    )
    session.add(job)
    session.flush()
    session.add(
        OutboxEvent(
            organization_id=principal.organization_id,
            event_key=f"generation:{job.id}",
            topic="generation.requested",
            payload_json={"job_id": job.id, "organization_id": principal.organization_id},
        )
    )
    audit(session, principal, "generation.queued", "generation_job", job.id)
    return job


@router.get("/jobs/{job_id}", response_model=JobRead)
def get_job(job_id: str, session: SessionDep, principal: PrincipalDep) -> GenerationJob:
    job = session.scalar(
        select(GenerationJob).where(
            GenerationJob.id == job_id,
            GenerationJob.organization_id == principal.organization_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Generation job not found")
    return job


@router.post("/projects/{project_id}/versions/{revision}/approve", response_model=DesignVersionRead)
def approve_version(
    project_id: str,
    revision: int,
    payload: ApprovalCreate,
    session: SessionDep,
    principal: ReviewerDep,
) -> DesignVersion:
    version = tenant_version(session, principal, project_id, revision)
    if version.immutable:
        raise HTTPException(status_code=409, detail="Immutable revisions cannot be changed")
    if payload.approval_type == "design" and version.status not in {
        DesignStatus.design_validated,
        DesignStatus.cam_validated,
        DesignStatus.approved,
    }:
        raise HTTPException(status_code=409, detail="Design validation is required")
    approved_job: GenerationJob | None = None
    approved_manifest_sha: str | None = None
    approved_overrides: list[dict[str, Any]] = []
    if payload.approval_type == "cam":
        if payload.generation_job_id is None:
            raise HTTPException(
                status_code=422, detail="generation_job_id is required for CAM approval"
            )
        approved_job = session.scalar(
            select(GenerationJob).where(
                GenerationJob.id == payload.generation_job_id,
                GenerationJob.organization_id == principal.organization_id,
                GenerationJob.design_version_id == version.id,
                GenerationJob.status == JobStatus.succeeded,
            )
        )
        if approved_job is None or not approved_job.result_json:
            raise HTTPException(
                status_code=409,
                detail="The selected successful generation job is required for CAM approval",
            )
        _require_current_generation_context(approved_job, version)
        approved_manifest_sha = str(approved_job.result_json.get("manifest_sha256", ""))
        if len(approved_manifest_sha) != 64:
            raise HTTPException(status_code=409, detail="The selected job has no checked manifest")
        version.status = DesignStatus.cam_validated
    elif payload.generation_job_id is not None:
        raise HTTPException(
            status_code=422, detail="generation_job_id is only valid for CAM approval"
        )
    if payload.approval_type == "cam" and payload.warning_overrides:
        raise HTTPException(
            status_code=422, detail="warning_overrides are only valid for design approval"
        )
    if payload.approval_type == "design":
        warnings = {
            str(item["rule_id"]): item
            for item in version.result_json.get("rule_evaluations", [])
            if item.get("status") == "WARNING" and item.get("rule_id")
        }
        supplied = {item.rule_id: item for item in payload.warning_overrides}
        if len(supplied) != len(payload.warning_overrides):
            raise HTTPException(status_code=422, detail="Duplicate warning override rule_id")
        if set(supplied) != set(warnings):
            raise HTTPException(
                status_code=422,
                detail="warning_overrides must match every and only current WARNING rule",
            )
        approved_at = datetime.now(UTC).isoformat()
        approved_overrides = [
            {
                "rule_id": rule_id,
                "rule_version": str(warnings[rule_id].get("rule_version", "unknown")),
                "reason": supplied[rule_id].reason,
                "approved_by": principal.user_id,
                "approved_at": approved_at,
            }
            for rule_id in sorted(warnings)
        ]
    approval = session.scalar(
        select(Approval).where(
            Approval.organization_id == principal.organization_id,
            Approval.design_version_id == version.id,
            Approval.approval_type == payload.approval_type,
        )
    )
    if payload.approval_type == "design" and approval is not None:
        existing_override_semantics = [
            {
                "rule_id": item.get("rule_id"),
                "rule_version": item.get("rule_version"),
                "reason": item.get("reason"),
            }
            for item in approval.overrides_json
        ]
        new_override_semantics = [
            {
                "rule_id": item.get("rule_id"),
                "rule_version": item.get("rule_version"),
                "reason": item.get("reason"),
            }
            for item in approved_overrides
        ]
        design_approval_changed = (
            approval.approved_by != principal.user_id
            or approval.reason != payload.reason
            or existing_override_semantics != new_override_semantics
        )
        if not design_approval_changed:
            approved_overrides = approval.overrides_json
        elif version.status in {DesignStatus.cam_validated, DesignStatus.approved}:
            session.execute(
                delete(Approval).where(
                    Approval.organization_id == principal.organization_id,
                    Approval.design_version_id == version.id,
                    Approval.approval_type == "cam",
                )
            )
            version.status = DesignStatus.design_validated
    if approval is None:
        session.add(
            Approval(
                organization_id=principal.organization_id,
                design_version_id=version.id,
                approval_type=payload.approval_type,
                approved_by=principal.user_id,
                reason=payload.reason,
                generation_job_id=approved_job.id if approved_job else None,
                production_context_hash=(
                    approved_job.production_context_hash if approved_job else None
                ),
                manifest_sha256=approved_manifest_sha,
                overrides_json=approved_overrides,
            )
        )
    else:
        approval.approved_by = principal.user_id
        approval.reason = payload.reason
        approval.generation_job_id = approved_job.id if approved_job else None
        approval.production_context_hash = (
            approved_job.production_context_hash if approved_job else None
        )
        approval.manifest_sha256 = approved_manifest_sha
        approval.overrides_json = approved_overrides
    approvals = set(
        session.scalars(
            select(Approval.approval_type).where(
                Approval.organization_id == principal.organization_id,
                Approval.design_version_id == version.id,
            )
        )
    )
    approvals.add(payload.approval_type)
    if approvals >= {"design", "cam"}:
        version.status = DesignStatus.approved
    audit(
        session,
        principal,
        "design_version.approved",
        "design_version",
        version.id,
        {
            "approval_type": payload.approval_type,
            "reason": payload.reason,
            "generation_job_id": approved_job.id if approved_job else None,
            "manifest_sha256": approved_manifest_sha,
            "warning_overrides": approved_overrides,
        },
    )
    return version


@router.post(
    "/projects/{project_id}/versions/{revision}/release",
    response_model=ReleaseRead,
)
def release_version(
    project_id: str,
    revision: int,
    payload: ReleaseCreate,
    session: SessionDep,
    principal: ReviewerDep,
) -> dict[str, Any]:
    version = tenant_version(session, principal, project_id, revision)
    if version.status == DesignStatus.superseded:
        raise HTTPException(status_code=409, detail="Superseded revisions cannot be released")
    if version.immutable:
        existing = session.scalar(select(Release).where(Release.design_version_id == version.id))
        if existing is None:
            raise HTTPException(status_code=409, detail="Immutable version has no release record")
        return {
            "release_id": existing.id,
            "release_number": existing.release_number,
            "status": version.status.value,
            "manifest_sha256": existing.manifest_sha256,
            "machine_use": "validation_only",
        }
    if version.status != DesignStatus.approved:
        raise HTTPException(status_code=409, detail="Design and CAM approvals are required")
    cam_approval = session.scalar(
        select(Approval).where(
            Approval.organization_id == principal.organization_id,
            Approval.design_version_id == version.id,
            Approval.approval_type == "cam",
        )
    )
    if cam_approval is None or cam_approval.generation_job_id is None:
        raise HTTPException(status_code=409, detail="A bound CAM approval is required")
    job = session.scalar(
        select(GenerationJob).where(
            GenerationJob.id == cam_approval.generation_job_id,
            GenerationJob.organization_id == principal.organization_id,
            GenerationJob.design_version_id == version.id,
            GenerationJob.status == JobStatus.succeeded,
        )
    )
    if job is None or not job.result_json:
        raise HTTPException(status_code=409, detail="Successful checked generation is required")
    _require_current_generation_context(job, version)
    if not job.result_json.get("authoritative_geometry"):
        raise HTTPException(
            status_code=409,
            detail="Release requires genuine server-generated STEP and GLB geometry",
        )
    if job.result_json.get("dfm_status") == "BLOCK":
        raise HTTPException(status_code=409, detail="Release is blocked by DFM")
    manifest_sha = str(job.result_json.get("manifest_sha256", ""))
    if len(manifest_sha) != 64:
        raise HTTPException(status_code=409, detail="Checked manifest is missing")
    if (
        cam_approval.production_context_hash != job.production_context_hash
        or cam_approval.manifest_sha256 != manifest_sha
    ):
        raise HTTPException(
            status_code=409,
            detail="CAM approval does not match the selected production context and manifest",
        )
    release = Release(
        organization_id=principal.organization_id,
        design_version_id=version.id,
        release_number=payload.release_number,
        released_by=principal.user_id,
        manifest_sha256=manifest_sha,
    )
    version.status = DesignStatus.released
    version.immutable = True
    session.add(release)
    session.flush()
    audit(
        session,
        principal,
        "design_version.released",
        "release",
        release.id,
        {"release_number": payload.release_number, "manifest_sha256": manifest_sha},
    )
    return {
        "release_id": release.id,
        "release_number": release.release_number,
        "status": version.status.value,
        "manifest_sha256": manifest_sha,
        "machine_use": "validation_only",
    }


@router.get("/jobs/{job_id}/artifacts", response_model=list[ArtifactRead])
def list_artifacts(
    job_id: str,
    session: SessionDep,
    principal: PrincipalDep,
) -> list[dict[str, Any]]:
    job = session.scalar(
        select(GenerationJob).where(
            GenerationJob.id == job_id,
            GenerationJob.organization_id == principal.organization_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Generation job not found")
    version = _require_current_artifacts(session, principal, job)
    artifacts = session.scalars(
        select(Artifact).where(
            Artifact.generation_job_id == job.id,
            Artifact.organization_id == principal.organization_id,
        )
    )
    now = int(time.time())
    expires = now + get_settings().artifact_url_ttl_seconds
    return [
        {
            "id": item.id,
            "kind": item.kind,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "content_type": item.content_type,
            "download_url": presigned_get(
                item.object_key,
                filename=_artifact_filename(item.kind, version.revision),
            ),
            "download_path": (
                f"/v1/artifacts/{item.id}/download?expires={expires}&signature="
                f"{sign_artifact_access(item.id, principal.organization_id, expires)}"
            ),
        }
        for item in artifacts
    ]


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(
    artifact_id: str,
    expires: int,
    signature: str,
    session: SessionDep,
    principal: PrincipalDep,
) -> RedirectResponse:
    verify_artifact_access(artifact_id, principal.organization_id, expires, signature)
    artifact = session.scalar(
        select(Artifact).where(
            Artifact.id == artifact_id,
            Artifact.organization_id == principal.organization_id,
        )
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    job = session.scalar(
        select(GenerationJob).where(
            GenerationJob.id == artifact.generation_job_id,
            GenerationJob.organization_id == principal.organization_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Generation job not found")
    version = _require_current_artifacts(session, principal, job)
    return RedirectResponse(
        presigned_get(
            artifact.object_key,
            filename=_artifact_filename(artifact.kind, version.revision),
        ),
        status_code=307,
    )


@router.post("/imports/inspect", response_model=ImportInspection)
async def inspect_import(
    principal: DesignerDep,
    document: Annotated[UploadFile, File()],
) -> ImportInspection:
    content = await document.read(20 * 1024 * 1024 + 1)
    try:
        validate_upload(content, document.content_type or "", document.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    digest = hashlib.sha256(content).hexdigest()
    return ImportInspection(
        import_id=f"sha256:{digest}",
        media_type=document.content_type or "application/octet-stream",
        size_bytes=len(content),
        furniture_type=None,
        furniture_type_confidence=0,
        status="needs_calibration",
        assumptions=[
            {
                "field": "furniture_type",
                "value": None,
                "confidence": 0,
                "origin": "No external AI provider configured; manual classification required",
            }
        ],
        unknown_fields=[
            "known_dimension",
            "width_mm",
            "height_mm",
            "depth_mm",
            "material",
            "load_per_shelf_kg",
            "joint_system",
            "assembly_method",
        ],
    )
