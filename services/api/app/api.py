from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import uuid4

from custombuild_domain import (
    BookcaseDesignSpec,
    TemplateCapabilityError,
    build_bookcase,
    joint_support_payload,
    require_template_for_revision,
    resolve_template_capability,
    template_capability_registry_payload,
)
from custombuild_manufacturing import (
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
    DFM_GRAIN_BLOCKER_CODE,
    GENERATION_PLAN_ARTIFACT_PATH,
    GENERATION_PLAN_ARTIFACT_ROLE,
    MANIFEST_CONTEXT_HASH_FIELDS,
    STOCK_PROFILE_MISSING_CODE,
    ArtifactError,
    CAMStageStatus,
    DesignReviewPackageStatus,
    Severity,
    canonical_json_bytes,
    grain_control_projection,
    normalize_design_review_dfm_report,
    normalize_design_review_package_status,
    validate_design_review_status_dfm_report,
    validate_design_review_status_inventory_entries,
    validate_manifest_artifact_entries,
    validate_manifest_context_contract,
    validate_workshop_evidence_binding,
)
from custombuild_manufacturing.package import PRODUCTION_MANIFEST_SCHEMA_VERSION
from custombuild_manufacturing.production_context import (
    ProductionContextError,
    assert_frozen_design_versions,
    assert_job_matches_frozen_revision_context,
    contexts_equal,
    generation_context_hash,
    resolve_production_components,
)
from custombuild_manufacturing.readiness import (
    LEGACY_WORKSHOP_READINESS_SCHEMA_VERSION,
    WorkshopReadinessReport,
    normalize_workshop_readiness_report,
)
from custombuild_rules import RULES_VERSION
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import Principal, get_principal, require_minimum_role
from .config import get_settings
from .design_service import (
    RuleEngineUnavailable,
    assert_rule_engine_available,
    auto_fix,
    canonical_preview,
    generation_plan_snapshot_for_design,
    grain_rule_evaluation,
    preview,
    preview_grain_issues_for_design,
    stock_grain_issues_for_design,
    stock_missing_issues_for_design,
    stock_selection_snapshot_for_design,
)
from .job_policy import GENERATION_JOB_TIMEOUT
from .models import (
    Approval,
    Artifact,
    DesignStatus,
    DesignVersion,
    ExternalEvidence,
    GenerationJob,
    ImportedAsset,
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
    ExternalEvidenceRead,
    GenerationRequest,
    ImportInspection,
    JobRead,
    ProductionStateRead,
    ProjectCreate,
    ProjectDraftRead,
    ProjectDraftUpdate,
    ProjectRead,
    ReleaseCreate,
    ReleaseRead,
    RevisionProductionContext,
)
from .security import validate_upload
from .storage import (
    ArtifactIntegrityError,
    ArtifactStorageUnavailableError,
    StoredObjectExpectation,
    presigned_get,
    read_verified_stored_object,
    sign_artifact_access,
    store_evidence_object,
    store_immutable_object,
    verify_artifact_access,
    verify_stored_object,
)

router = APIRouter(prefix="/v1")
SessionDep = Annotated[Session, Depends(tenant_session)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
DesignerDep = Annotated[Principal, Depends(require_minimum_role(Role.designer))]
ReviewerDep = Annotated[Principal, Depends(require_minimum_role(Role.reviewer))]

EVIDENCE_RULE_TYPES: dict[str, str] = {
    "CB-TIP-001": "wall_anchor",
    "CB-HARDWARE-001": "hardware",
    "DFM-GRAIN-001": "material_grain",
}

_WORKSHOP_READINESS_MAX_BYTES = 64 * 1024
_DESIGN_REVIEW_PACKAGE_STATUS_MAX_BYTES = 64 * 1024
_STOCK_SELECTION_MAX_BYTES = 8 * 1024 * 1024
_GENERATION_PLAN_MAX_BYTES = 8 * 1024 * 1024
_DFM_REPORT_MAX_BYTES = 8 * 1024 * 1024
_PRODUCTION_MANIFEST_MAX_BYTES = 8 * 1024 * 1024
_WORKSHOP_READINESS_ARTIFACT_PATH = "validation/workshop-readiness.json"
_WORKSHOP_READINESS_ARTIFACT_ROLE = "WORKSHOP_READINESS_REPORT"
_DFM_REPORT_ARTIFACT_PATH = "validation/dfm-report.json"
_DFM_REPORT_ARTIFACT_ROLE = "DFM_VALIDATION_REPORT"
_STOCK_SELECTION_ARTIFACT_PATH = "validation/stock-selection.json"
_STOCK_SELECTION_ARTIFACT_ROLE = "STOCK_SELECTION_SNAPSHOT"
_GENERATION_PLAN_ARTIFACT_PATH = GENERATION_PLAN_ARTIFACT_PATH
_GENERATION_PLAN_ARTIFACT_ROLE = GENERATION_PLAN_ARTIFACT_ROLE
_MANIFEST_CHECKSUM_SCOPE = "all payload files; manifest.json excluded to avoid recursive hashing"
_MANIFEST_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        *MANIFEST_CONTEXT_HASH_FIELDS,
        "production_context_hash",
        "checksum_scope",
    }
)
_MANIFEST_ARTIFACT_ENTRY_KEYS = frozenset({"path", "media_type", "role", "size_bytes", "sha256"})
_EVIDENCE_MANIFEST_IDENTITIES: dict[str, tuple[str, str, str]] = {
    "dfm_report": (
        "validation/dfm-report.json",
        "DFM_VALIDATION_REPORT",
        "application/json",
    ),
    "design_review_package_status": (
        DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
        DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
        "application/json",
    ),
    "stock_selection": (
        _STOCK_SELECTION_ARTIFACT_PATH,
        _STOCK_SELECTION_ARTIFACT_ROLE,
        "application/json",
    ),
    "generation_plan": (
        _GENERATION_PLAN_ARTIFACT_PATH,
        _GENERATION_PLAN_ARTIFACT_ROLE,
        "application/json",
    ),
    "operations": (
        "cam/operations.json",
        "MACHINE_NEUTRAL_OPERATIONS",
        "application/json",
    ),
    "validation_backplot": (
        "cam/validation-backplot.svg",
        "VALIDATION_BACKPLOT",
        "image/svg+xml",
    ),
    "design_glb": ("model/design.glb", "WEB_PREVIEW_GLB", "model/gltf-binary"),
    "design_fcstd": (
        "model/design.fcstd",
        "NON_AUTHORITATIVE_FREECAD_PROJECT",
        "application/vnd.freecad",
    ),
    "cad_interchange_status": (
        "validation/cad-interchange-status.json",
        "CAD_INTERCHANGE_STATUS",
        "application/json",
    ),
    "source_provenance": (
        "validation/source-provenance.json",
        "SOURCE_PROVENANCE",
        "application/json",
    ),
    "workshop_readiness": (
        "validation/workshop-readiness.json",
        "WORKSHOP_READINESS_REPORT",
        "application/json",
    ),
    "assembly_readiness": (
        "assembly/assembly-readiness.json",
        "ASSEMBLY_READINESS",
        "application/json",
    ),
}
_BLOCKED_CAM_ALLOWED_EVIDENCE_KINDS = frozenset(
    {
        "production_bundle",
        "manifest",
        "dfm_report",
        "design_review_package_status",
        "stock_selection",
        "generation_plan",
        "design_glb",
        "workshop_readiness",
        "design_fcstd",
        "cad_interchange_status",
        "source_provenance",
        "assembly_readiness",
    }
)
_RULE_REPORT_DISCLAIMER = (
    "Beräkningarna är deterministisk screening och beslutsstöd, inte "
    "produktcertifiering eller garanti för säker konstruktion."
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _expire_generation_job_if_overdue(
    job: GenerationJob,
    *,
    now: datetime,
) -> bool:
    """Make the server-owned generation deadline terminal and retryable."""

    deadline_at = job.deadline_at
    if (
        job.status not in {JobStatus.queued, JobStatus.running}
        or deadline_at is None
        or _as_utc(deadline_at) > _as_utc(now)
    ):
        return False
    job.status = JobStatus.failed
    job.lease_token = None
    job.lease_expires_at = None
    job.error = "Generation job exceeded the server deadline of 120 minutes"
    job.finished_at = now
    return True


def _validation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValidationError):
        # Pydantic's default error payload can contain BaseModel instances,
        # enums and the original ValueError in `input`/`ctx`. Starlette cannot
        # JSON-encode those values, which would turn a controlled 422 into a
        # dropped HTTP connection. Keep the public error useful and strictly
        # serializable without leaking internal model state.
        errors: Any = exc.errors(
            include_url=False,
            include_input=False,
            include_context=False,
        )
    else:
        errors = [{"type": "value_error", "loc": [], "msg": str(exc)}]
    return HTTPException(
        status_code=422,
        detail={
            "code": "DESIGN_INPUT_INVALID",
            "message": "The design inputs could not be validated.",
            "solution": (
                "Use the proposed construction fix, or increase the available dimensions "
                "or reduce shelves/dividers until every part has a manufacturable size."
            ),
            "errors": errors,
        },
    )


def _rule_engine_error(exc: RuleEngineUnavailable) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "RULE_ENGINE_UNAVAILABLE",
            "message": "Construction screening is temporarily unavailable.",
            "solution": (
                "Restore the versioned custombuild-rules package and wait until /ready "
                "reports rule_engine=ok before saving or generating a revision."
            ),
        },
    )


def _require_rule_engine() -> None:
    try:
        assert_rule_engine_available()
    except RuleEngineUnavailable as exc:
        raise _rule_engine_error(exc) from exc


def _template_capability_error(exc: TemplateCapabilityError) -> HTTPException:
    status_code = 404 if exc.code == "UNKNOWN_TEMPLATE" else 409
    return HTTPException(status_code=status_code, detail=exc.as_detail())


def _evidence_snapshot(evidence: ExternalEvidence) -> dict[str, Any]:
    return {
        "evidence_id": evidence.id,
        "evidence_type": evidence.evidence_type,
        "rule_id": evidence.rule_id,
        "catalog_id": evidence.catalog_id,
        "catalog_version": evidence.catalog_version,
        "design_hash": evidence.design_hash,
        "sha256": evidence.sha256,
        "size_bytes": evidence.size_bytes,
        "content_type": evidence.content_type,
        "created_by": evidence.created_by,
        "created_at": evidence.created_at.isoformat(),
        "expires_at": evidence.expires_at.isoformat() if evidence.expires_at else None,
    }


def _import_storage_error(*, unavailable: bool) -> HTTPException:
    if unavailable:
        return HTTPException(
            status_code=503,
            detail={
                "code": "REFERENCE_ASSET_STORAGE_UNAVAILABLE",
                "message": "The reference image cannot currently be verified.",
                "solution": "Retry after object storage is healthy; the revision remains unsaved.",
            },
        )
    return HTTPException(
        status_code=409,
        detail={
            "code": "REFERENCE_ASSET_INTEGRITY_FAILED",
            "message": "The stored reference image is missing or no longer matches its checksum.",
            "solution": "Upload the source image again as a new immutable import and reconfirm it.",
        },
    )


def _verify_imported_asset_object(asset: ImportedAsset) -> None:
    try:
        verify_stored_object(
            StoredObjectExpectation(
                object_key=asset.object_key,
                sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                content_type=asset.media_type,
            ),
            stream_hash=True,
        )
    except ArtifactIntegrityError as exc:
        raise _import_storage_error(unavailable=False) from exc
    except ArtifactStorageUnavailableError as exc:
        raise _import_storage_error(unavailable=True) from exc


def _verified_reference_provenance(
    session: Session,
    principal: Principal,
    project: Project,
    source_provenance: dict[str, Any],
    *,
    design_hash: str,
) -> tuple[ImportedAsset, dict[str, Any]]:
    import_id = str(source_provenance.get("import_id", ""))
    asset = session.scalar(
        select(ImportedAsset).where(
            ImportedAsset.id == import_id,
            ImportedAsset.organization_id == principal.organization_id,
            ImportedAsset.project_id == project.id,
        )
    )
    if asset is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REFERENCE_ASSET_NOT_FOUND",
                "message": "The reference image is missing or belongs to another project.",
                "solution": (
                    "Upload the image inside this project and reconfirm the interpretation."
                ),
            },
        )
    if source_provenance.get("image_sha256") != asset.sha256:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REFERENCE_ASSET_DIGEST_MISMATCH",
                "message": "The claimed image checksum does not match the immutable import.",
                "solution": "Use the import ID and checksum returned by the latest image upload.",
            },
        )
    if source_provenance.get("verified_model_fingerprint") != design_hash:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REFERENCE_MODEL_FINGERPRINT_MISMATCH",
                "message": "The confirmed image interpretation no longer matches this model.",
                "solution": (
                    "Review the changed dimensions and layout, then reconfirm the image "
                    "interpretation."
                ),
            },
        )
    _verify_imported_asset_object(asset)
    snapshot = {
        **source_provenance,
        "import_id": asset.id,
        "image_sha256": asset.sha256,
        "file_name": asset.original_filename,
        "media_type": asset.media_type,
        "size_bytes": asset.size_bytes,
        "asset_created_at": asset.created_at.isoformat(),
        "asset_schema_version": "custombuild.reference-asset.v1",
        "verified_model_fingerprint": design_hash,
    }
    return asset, snapshot


def _verify_frozen_reference_asset(
    session: Session,
    principal: Principal,
    version: DesignVersion,
) -> None:
    provenance = version.source_provenance_json
    if not provenance:
        if version.source_import_id is not None:
            raise _import_storage_error(unavailable=False)
        return
    if provenance.get("source") != "reference_image" or version.source_import_id is None:
        raise _import_storage_error(unavailable=False)
    asset = session.scalar(
        select(ImportedAsset).where(
            ImportedAsset.id == version.source_import_id,
            ImportedAsset.organization_id == principal.organization_id,
            ImportedAsset.project_id == version.project_id,
        )
    )
    if (
        asset is None
        or provenance.get("import_id") != version.source_import_id
        or provenance.get("image_sha256") != asset.sha256
        or provenance.get("verified_model_fingerprint") != version.design_hash
    ):
        raise _import_storage_error(unavailable=False)
    _verify_imported_asset_object(asset)


def _verified_external_evidence(
    session: Session,
    organization_id: str,
    project_id: str,
    design_hash: str,
    evidence_ids: list[str],
    *,
    expected_rule_id: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve tenant records and stream-verify every claimed evidence object."""

    if not evidence_ids:
        return []
    evidence_rows = list(
        session.scalars(
            select(ExternalEvidence).where(
                ExternalEvidence.organization_id == organization_id,
                ExternalEvidence.project_id == project_id,
                ExternalEvidence.id.in_(evidence_ids),
            )
        )
    )
    by_id = {item.id: item for item in evidence_rows}
    if set(by_id) != set(evidence_ids):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXTERNAL_EVIDENCE_NOT_FOUND",
                "message": "One or more evidence IDs are missing or belong to another project.",
                "solution": "Upload and select evidence for this exact project and design.",
            },
        )
    evidence_type_counts: dict[str, int] = {}
    for evidence_id in evidence_ids:
        evidence_type = by_id[evidence_id].evidence_type
        evidence_type_counts[evidence_type] = evidence_type_counts.get(evidence_type, 0) + 1
    duplicate_types = sorted(
        evidence_type for evidence_type, count in evidence_type_counts.items() if count > 1
    )
    if duplicate_types:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXTERNAL_EVIDENCE_TYPE_DUPLICATE",
                "message": "Multiple selected evidence records claim the same evidence type.",
                "solution": "Select exactly one current evidence record per evidence type.",
                "evidence_types": duplicate_types,
            },
        )
    snapshots: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for evidence_id in evidence_ids:
        evidence = by_id[evidence_id]
        expires_at = evidence.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        expected_type = EVIDENCE_RULE_TYPES.get(evidence.rule_id)
        if (
            evidence.revoked_at is not None
            or evidence.design_hash != design_hash
            or expected_type != evidence.evidence_type
            or (expected_rule_id is not None and evidence.rule_id != expected_rule_id)
            or (expires_at is not None and expires_at <= now)
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "EXTERNAL_EVIDENCE_STALE",
                    "message": "External evidence is revoked, expired or bound to another design.",
                    "solution": "Upload current evidence for this exact design and control.",
                    "evidence_id": evidence.id,
                },
            )
        try:
            verify_stored_object(
                StoredObjectExpectation(
                    object_key=evidence.object_key,
                    sha256=evidence.sha256,
                    size_bytes=evidence.size_bytes,
                    content_type=evidence.content_type,
                ),
                stream_hash=True,
            )
        except ArtifactIntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "EXTERNAL_EVIDENCE_INTEGRITY_FAILED",
                    "message": "The evidence document no longer matches its checksum.",
                    "solution": "Upload a new immutable evidence document and review it again.",
                    "evidence_id": evidence.id,
                },
            ) from exc
        except ArtifactStorageUnavailableError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "EXTERNAL_EVIDENCE_STORAGE_UNAVAILABLE",
                    "message": "Evidence storage cannot currently be verified.",
                    "solution": "Retry after object storage is healthy; approval remains blocked.",
                },
            ) from exc
        snapshots.append(_evidence_snapshot(evidence))
    return snapshots


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
        "production_bundle": f"custombuild-design-review-rev-{revision}.zip",
        "manifest": f"custombuild-design-review-rev-{revision}-manifest.json",
    }.get(kind)


def _require_current_generation_context(
    job: GenerationJob,
    version: DesignVersion,
) -> None:
    """Reject a job frozen against any superseded production implementation."""

    try:
        _require_frozen_template_capability(version)
        result_json = version.result_json if isinstance(version.result_json, dict) else {}
        assert_job_matches_frozen_revision_context(
            result_json.get("production_context"),
            job.request_json,
        )
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
            **get_settings().build_identity,
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
            detail=(
                "The job no longer matches this revision's frozen production choices; "
                "save a new revision and generate a new job"
            ),
        ) from exc


def _require_frozen_template_capability(version: DesignVersion) -> dict[str, object]:
    """Reject legacy, concept or registry-drifted revision support claims."""

    try:
        capability = require_template_for_revision(
            version.template_id,
            resolve_template_capability(version.template_id).archetype,
        )
    except TemplateCapabilityError as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    snapshot = capability.snapshot()
    stored_snapshot = (
        version.result_json.get("template_capability")
        if isinstance(version.result_json, dict)
        else None
    )
    if (
        version.template_capability_fingerprint != capability.capability_fingerprint
        or stored_snapshot != snapshot
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TEMPLATE_CAPABILITY_STALE",
                "message": ("The revision is not bound to the current server template capability."),
                "solution": "Save and validate a new revision before generating evidence.",
                "template_id": version.template_id,
            },
        )
    return snapshot


def _require_current_design_approval(
    session: Session,
    organization_id: str,
    approval: Approval | None,
    version: DesignVersion,
) -> Approval:
    """Require an approval that covers exactly the version's current server warnings."""

    if approval is None:
        raise HTTPException(status_code=409, detail="A current design approval is required")
    if len(approval.reason.strip()) < 5:
        raise HTTPException(status_code=409, detail="The design approval reason is missing")
    warnings = {
        str(item["rule_id"]): str(item.get("rule_version", "unknown"))
        for item in version.result_json.get("rule_evaluations", [])
        if item.get("status") == "WARNING" and item.get("rule_id")
    }
    overrides = approval.overrides_json if isinstance(approval.overrides_json, list) else []
    if len(overrides) != len(warnings):
        raise HTTPException(
            status_code=409,
            detail="The design approval does not cover every current server warning",
        )
    supplied: dict[str, dict[str, Any]] = {}
    for item in overrides:
        if not isinstance(item, dict) or not isinstance(item.get("rule_id"), str):
            raise HTTPException(status_code=409, detail="The design approval is malformed")
        supplied[item["rule_id"]] = item
    if len(supplied) != len(overrides) or set(supplied) != set(warnings):
        raise HTTPException(
            status_code=409,
            detail="The design approval does not match the current server warnings",
        )
    attribution_valid = True
    for item in supplied.values():
        approved_at = item.get("approved_at")
        try:
            timestamp = datetime.fromisoformat(
                approved_at.replace("Z", "+00:00") if isinstance(approved_at, str) else ""
            )
            timestamp_valid = timestamp.tzinfo is not None and timestamp.utcoffset() is not None
        except ValueError:
            timestamp_valid = False
        attribution_valid = attribution_valid and (
            item.get("approved_by") == approval.approved_by and timestamp_valid
        )
    if not attribution_valid or any(
        str(supplied[rule_id].get("rule_version", "unknown")) != rule_version
        or not str(supplied[rule_id].get("reason", "")).strip()
        for rule_id, rule_version in warnings.items()
    ):
        raise HTTPException(
            status_code=409,
            detail="The design approval warning evidence is stale or incomplete",
        )
    for rule_id, item in supplied.items():
        evidence_snapshots = item.get("external_evidence", [])
        if not isinstance(evidence_snapshots, list):
            raise HTTPException(status_code=409, detail="Design evidence snapshot is malformed")
        if rule_id == DFM_GRAIN_BLOCKER_CODE:
            if evidence_snapshots or item.get("evidence_status") != "acknowledged_unresolved":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "DFM-GRAIN-001 is only an acknowledgement; opaque evidence "
                        "cannot verify a structured stock-grain axis"
                    ),
                )
            continue
        evidence_ids = [
            str(snapshot.get("evidence_id"))
            for snapshot in evidence_snapshots
            if isinstance(snapshot, dict) and snapshot.get("evidence_id")
        ]
        if len(evidence_ids) != len(evidence_snapshots):
            raise HTTPException(status_code=409, detail="Design evidence snapshot is malformed")
        verified = _verified_external_evidence(
            session,
            organization_id,
            version.project_id,
            version.design_hash,
            evidence_ids,
            expected_rule_id=rule_id,
        )
        if verified != evidence_snapshots:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "EXTERNAL_EVIDENCE_SNAPSHOT_STALE",
                    "message": "The approved evidence no longer matches its server record.",
                    "solution": "Review and approve the current immutable evidence again.",
                },
            )
    return approval


def _design_approval_snapshot(approval: Approval) -> dict[str, Any]:
    """Canonical review identity bound into every generation-context hash."""

    return {
        "approval_id": approval.id,
        "approved_by": approval.approved_by,
        "reason": approval.reason,
        "warning_overrides": approval.overrides_json,
    }


def _frozen_grain_contract(
    version: DesignVersion,
) -> tuple[tuple[Any, ...], tuple[Any, ...], dict[str, Any] | None] | None:
    """Re-derive preview and matched-stock grain truth from the frozen revision."""

    if not isinstance(version.spec_json, Mapping) or not isinstance(version.result_json, Mapping):
        return None
    try:
        spec = BookcaseDesignSpec.model_validate(version.spec_json)
        design = build_bookcase(spec)
        if design.design_hash != version.design_hash:
            return None
        production_context = RevisionProductionContext.model_validate(
            version.result_json.get("production_context")
        ).model_dump(mode="json")
        preview_issues = preview_grain_issues_for_design(design)
        preview_projection = grain_control_projection(preview_issues)
        expected_controls = (
            []
            if preview_projection is None
            else [json.loads(canonical_json_bytes(preview_projection))]
        )
        expected_grain_evaluations = (
            [] if preview_projection is None else [grain_rule_evaluation(preview_projection)]
        )
        raw_evaluations = version.result_json.get("rule_evaluations")
        raw_controls = version.result_json.get("manufacturing_controls")
        if not isinstance(raw_evaluations, list) or raw_controls != expected_controls:
            return None
        grain_evaluations = [
            item
            for item in raw_evaluations
            if isinstance(item, Mapping) and item.get("rule_id") == DFM_GRAIN_BLOCKER_CODE
        ]
        if canonical_json_bytes(grain_evaluations) != canonical_json_bytes(
            expected_grain_evaluations
        ):
            return None
        matched_issues = stock_grain_issues_for_design(
            design,
            production_context,
            severity=Severity.BLOCK,
        )
    except (TypeError, ValueError, ValidationError, RecursionError):
        return None
    return (
        matched_issues,
        preview_issues,
        (
            None
            if preview_projection is None
            else json.loads(canonical_json_bytes(preview_projection))
        ),
    )


def _frozen_stock_selection_snapshot(version: DesignVersion) -> bytes | None:
    """Rebuild the exact worker stock-selection document from frozen inputs."""

    if not isinstance(version.spec_json, Mapping) or not isinstance(version.result_json, Mapping):
        return None
    try:
        spec = BookcaseDesignSpec.model_validate(version.spec_json)
        design = build_bookcase(spec)
        if design.design_hash != version.design_hash:
            return None
        production_context = RevisionProductionContext.model_validate(
            version.result_json.get("production_context")
        ).model_dump(mode="json")
        return stock_selection_snapshot_for_design(design, production_context)
    except (TypeError, ValueError, ValidationError, RecursionError):
        return None


def _frozen_generation_plan_snapshot(
    job: GenerationJob,
    version: DesignVersion,
) -> bytes | None:
    """Rebuild the worker's exact generation plan from frozen request inputs."""

    if (
        not isinstance(version.spec_json, Mapping)
        or not isinstance(version.result_json, Mapping)
        or not isinstance(job.request_json, Mapping)
    ):
        return None
    try:
        spec = BookcaseDesignSpec.model_validate(version.spec_json)
        design = build_bookcase(spec)
        if design.design_hash != version.design_hash:
            return None
        production_context = RevisionProductionContext.model_validate(
            version.result_json.get("production_context")
        ).model_dump(mode="json")
        request_context = {
            field: job.request_json.get(field)
            for field in (
                "stock_width_mm",
                "stock_height_mm",
                "stock_count",
                "back_stock_width_mm",
                "back_stock_height_mm",
                "back_stock_count",
                "machine_profile_id",
            )
        }
        if request_context != production_context:
            return None
        machine_profile_id = job.request_json.get("machine_profile_id")
        postprocessor_id = job.request_json.get("postprocessor_id")
        validation_program_requested = job.request_json.get("include_validation_program")
        if (
            not isinstance(machine_profile_id, str)
            or not isinstance(postprocessor_id, str)
            or type(validation_program_requested) is not bool
        ):
            return None
        resolved = resolve_production_components(
            machine_profile_id=machine_profile_id,
            postprocessor_id=postprocessor_id,
            **get_settings().build_identity,
        )
        if not contexts_equal(job.production_engine_context_json, resolved.context):
            return None
        return generation_plan_snapshot_for_design(
            design,
            production_context,
            machine=resolved.machine,
            validation_program_requested=validation_program_requested,
        )
    except (
        KeyError,
        ProductionContextError,
        TypeError,
        ValueError,
        ValidationError,
        RecursionError,
    ):
        return None


def _frozen_stock_missing_issues(version: DesignVersion) -> tuple[Any, ...] | None:
    """Rebuild canonical missing-stock facts from the frozen assignment."""

    if not isinstance(version.spec_json, Mapping) or not isinstance(version.result_json, Mapping):
        return None
    try:
        spec = BookcaseDesignSpec.model_validate(version.spec_json)
        design = build_bookcase(spec)
        if design.design_hash != version.design_hash:
            return None
        production_context = RevisionProductionContext.model_validate(
            version.result_json.get("production_context")
        ).model_dump(mode="json")
        return stock_missing_issues_for_design(design, production_context)
    except (TypeError, ValueError, ValidationError, RecursionError):
        return None


def _grain_report_matches_frozen_version(
    report: Any,
    expected_issues: tuple[Any, ...],
) -> bool:
    actual = tuple(issue for issue in report.issues if issue.code == DFM_GRAIN_BLOCKER_CODE)
    try:
        return canonical_json_bytes(actual) == canonical_json_bytes(expected_issues)
    except (TypeError, ValueError, RecursionError):
        return False


def _stock_report_matches_frozen_version(
    report: Any,
    expected_issues: tuple[Any, ...],
) -> bool:
    actual = tuple(issue for issue in report.issues if issue.code == STOCK_PROFILE_MISSING_CODE)
    try:
        return canonical_json_bytes(actual) == canonical_json_bytes(expected_issues)
    except (TypeError, ValueError, RecursionError):
        return False


def _generation_result_claims_are_safe(result_json: Mapping[str, Any]) -> bool:
    """Require canonical, non-blocking claims for the generated review package."""

    dfm_status = result_json.get("dfm_status")
    return (
        result_json.get("authoritative_geometry") is True
        and isinstance(dfm_status, str)
        and dfm_status in ("PASS", "WARNING")
    )


def _workshop_readiness_is_valid(result_json: Mapping[str, Any]) -> bool:
    """Validate the report structure and its separate machine-program safety claims."""

    if not _generation_result_claims_are_safe(result_json):
        return False
    readiness = result_json.get("workshop_readiness")
    if not isinstance(readiness, Mapping):
        return False
    source_schema_version = readiness.get("schema_version")
    try:
        normalized = normalize_workshop_readiness_report(readiness)
    except ValueError:
        return False

    program_fields = {"machine_program_mode", "production_machine_program"}
    fields_present = program_fields.intersection(result_json)
    if source_schema_version != LEGACY_WORKSHOP_READINESS_SCHEMA_VERSION or fields_present:
        if fields_present != program_fields:
            return False
        if (
            result_json.get("machine_program_mode") != "VALIDATION_DRY_RUN"
            or result_json.get("production_machine_program") is not False
        ):
            return False

    return normalized.design_review_ready and normalized.missing_evidence_count > 0


def _design_review_package_status(
    result_json: Mapping[str, Any],
) -> DesignReviewPackageStatus | None:
    raw = result_json.get("design_review_package_status")
    if not isinstance(raw, Mapping):
        return None
    try:
        return normalize_design_review_package_status(raw)
    except ValueError:
        return None


def _blocked_cam_review_package_is_valid(result_json: Mapping[str, Any]) -> bool:
    """Accept a complete review package without calling its CAM stage ready."""

    package_status = _design_review_package_status(result_json)
    if (
        package_status is None
        or package_status.cam_status is not CAMStageStatus.BLOCKED
        or len(package_status.blocker_codes) != 1
        or package_status.blocker_codes[0]
        not in {
            STOCK_PROFILE_MISSING_CODE,
            DFM_GRAIN_BLOCKER_CODE,
            "TWO_SIDED_REGISTRATION_MISSING",
        }
        or result_json.get("authoritative_geometry") is not True
        or result_json.get("machine_program_mode") != "CAM_BLOCKED"
        or result_json.get("production_machine_program") is not False
    ):
        return False
    dfm_blocked = package_status.blocker_codes[0] in {
        STOCK_PROFILE_MISSING_CODE,
        DFM_GRAIN_BLOCKER_CODE,
    }
    grain_blocked = package_status.blocker_codes == (DFM_GRAIN_BLOCKER_CODE,)
    dfm_status = result_json.get("dfm_status")
    if (dfm_blocked and dfm_status != "BLOCK") or (
        not dfm_blocked and dfm_status not in {"PASS", "WARNING"}
    ):
        return False
    raw_readiness = result_json.get("workshop_readiness")
    if not isinstance(raw_readiness, Mapping):
        return False
    try:
        readiness = normalize_workshop_readiness_report(raw_readiness)
    except ValueError:
        return False
    software_status = {item.code: item.status.value for item in readiness.software_evidence}
    workshop_status = {item.code: item.status.value for item in readiness.workshop_evidence}
    return (
        readiness.design_review_ready is False
        and readiness.physical_cutting_authorized is False
        and readiness.missing_evidence_count > 0
        and "nesting_utilization_ppm" in result_json
        and result_json["nesting_utilization_ppm"] is None
        and type(result_json.get("used_sheet_count")) is int
        and result_json.get("used_sheet_count") == 0
        and result_json.get("nesting_layouts") == []
        and software_status
        == {
            "AUTHORITATIVE_CAD": "VERIFIED",
            "DFM_SCREEN": "MISSING" if dfm_blocked else "VERIFIED",
            "SEMANTIC_OPERATIONS": "MISSING",
            "SETUP_SHEETS": "MISSING",
            "VALIDATION_BACKPLOT": "MISSING",
            "NON_CUTTING_PROGRAM": "MISSING",
        }
        and (
            not grain_blocked
            or workshop_status.get("MATERIAL_GRAIN") == "EXTERNAL_EVIDENCE_REQUIRED"
        )
    )


def _design_review_package_is_valid(result_json: Mapping[str, Any], *, require_cam: bool) -> bool:
    raw_status = result_json.get("design_review_package_status")
    package_status = _design_review_package_status(result_json)
    if raw_status is None or package_status is None:
        return False
    if package_status.cam_status is CAMStageStatus.BLOCKED:
        return not require_cam and _blocked_cam_review_package_is_valid(result_json)
    if package_status.validation_program_included is not True:
        return False
    return _workshop_readiness_is_valid(result_json)


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _strict_canonical_json_object(payload: bytes) -> dict[str, Any]:
    """Parse one UTF-8 canonical JSON object without accepting ambiguous encodings."""

    if not isinstance(payload, bytes) or payload.startswith(b"\xef\xbb\xbf"):
        raise ArtifactIntegrityError("review evidence is not canonical UTF-8 JSON")
    try:
        decoded = payload.decode("utf-8", errors="strict")
        parsed = json.loads(decoded, parse_constant=_reject_nonfinite_json)
        if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != payload:
            raise ValueError("review evidence is not a canonical JSON object")
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise ArtifactIntegrityError("review evidence is not canonical JSON") from exc
    return parsed


def _manifest_context_matches_frozen_job(
    manifest: Mapping[str, Any],
    job: GenerationJob,
    version: DesignVersion,
) -> bool:
    """Bind every non-inventory manifest claim to the frozen server records."""

    engine_context = job.production_engine_context_json
    version_result = version.result_json
    request = job.request_json
    if (
        not isinstance(engine_context, Mapping)
        or not isinstance(version_result, Mapping)
        or not isinstance(request, Mapping)
    ):
        return False
    capability = version_result.get("template_capability")
    design_spec = version_result.get("spec")
    if not isinstance(capability, Mapping) or not isinstance(design_spec, Mapping):
        return False

    material_versions: set[str] = set()
    for field in ("material", "back_material"):
        material = design_spec.get(field)
        if material is None and field == "back_material":
            continue
        if not isinstance(material, Mapping):
            return False
        material_id = material.get("material_id")
        material_version = material.get("version")
        if not isinstance(material_id, str) or not isinstance(material_version, str):
            return False
        material_versions.add(f"{material_id}@{material_version}")

    external_evidence = request.get("external_evidence", [])
    overrides = request.get("approved_warning_overrides", [])
    rule_evaluations = version_result.get("rule_evaluations", [])
    if (
        not isinstance(external_evidence, list)
        or any(not isinstance(item, Mapping) for item in external_evidence)
        or not isinstance(overrides, list)
        or any(not isinstance(item, Mapping) for item in overrides)
        or not isinstance(rule_evaluations, list)
        or any(not isinstance(item, Mapping) for item in rule_evaluations)
    ):
        return False

    warnings = [_RULE_REPORT_DISCLAIMER]
    for evaluation in rule_evaluations:
        if (
            evaluation.get("status") != "WARNING"
            or evaluation.get("rule_id") == DFM_GRAIN_BLOCKER_CODE
        ):
            continue
        rule_id = evaluation.get("rule_id")
        rule_version = evaluation.get("rule_version")
        title = evaluation.get("title")
        if not all(isinstance(value, str) for value in (rule_id, rule_version, title)):
            return False
        warnings.append(f"{rule_id}@{rule_version}: {title}")

    try:
        expected = {
            "project_id": version.project_id,
            "revision": str(version.revision),
            "design_hash": version.design_hash,
            "app_version": engine_context["app_version"],
            "engine_version": version.engine_version,
            "template_version": version.template_version,
            "domain_template_version": version.template_version,
            "template_capability_version": capability["template_version"],
            "template_capability_registry_version": engine_context[
                "template_capability_registry_version"
            ],
            "template_id": version.template_id,
            "template_capability_fingerprint": version.template_capability_fingerprint,
            "template_capability": capability,
            "rule_version": version.rule_version,
            "material_versions": sorted(material_versions),
            "joint_version": engine_context["joint_support_version"],
            "machine_profile": {
                "id": engine_context["machine_profile_id"],
                "version": engine_context["machine_profile_version"],
            },
            "postprocessor_version": engine_context["postprocessor_version"],
            "generation_context_hash": job.production_context_hash,
            "production_engine_context": engine_context,
            "artifact_schema_version": engine_context["artifact_schema_version"],
            "cad_status": "GENERATED",
            "release_scope": "design_review",
            "machine_use": "validation_only",
            "physical_cutting_authorized": False,
            "approved_assumptions": [],
            "warnings": sorted(warnings),
            "overrides": overrides,
            "external_evidence": external_evidence,
            "source_provenance": version.source_provenance_json or None,
        }
    except KeyError:
        return False
    actual = {field: manifest.get(field) for field in expected}
    try:
        return canonical_json_bytes(actual) == canonical_json_bytes(expected)
    except (TypeError, ValueError, RecursionError):
        return False


def _frozen_edge_band_selection_required(version: DesignVersion) -> bool | None:
    result = version.result_json
    if not isinstance(result, Mapping):
        return None
    spec = result.get("spec")
    if not isinstance(spec, Mapping):
        return None
    parameters = spec.get("parameters")
    if not isinstance(parameters, Mapping):
        return None
    thickness_um = parameters.get("edge_band_thickness_um")
    if type(thickness_um) is not int or thickness_um < 0:
        return None
    return thickness_um > 0


def _manifest_evidence_entry(
    kind: str,
    inventory: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve one result evidence kind to one canonical manifest entry."""

    identity = _EVIDENCE_MANIFEST_IDENTITIES.get(kind)
    if identity is not None:
        path, role, media_type = identity
        candidates = [
            entry
            for entry in inventory
            if entry["path"].casefold() == path.casefold()
            or entry["role"].casefold() == role.casefold()
        ]
        if len(candidates) != 1:
            return None
        entry = candidates[0]
        if entry["path"] != path or entry["role"] != role or entry["media_type"] != media_type:
            return None
        return entry

    if not kind.startswith("setup_sheet_") or len(kind) != len("setup_sheet_000"):
        return None
    suffix = kind.removeprefix("setup_sheet_")
    if not suffix.isascii() or not suffix.isdigit() or int(suffix) <= 0:
        return None
    setup_entries = sorted(
        (
            entry
            for entry in inventory
            if entry["path"].casefold().startswith("cam/setups/")
            or entry["role"].casefold() == "setup_sheet"
        ),
        key=lambda entry: entry["path"],
    )
    index = int(suffix) - 1
    if index >= len(setup_entries):
        return None
    entry = setup_entries[index]
    if (
        not entry["path"].startswith("cam/setups/")
        or entry["role"] != "SETUP_SHEET"
        or entry["media_type"] != "image/svg+xml"
    ):
        return None
    return entry


def _manifest_evidence_matches_expectations(
    inventory: list[dict[str, Any]],
    expectations: Mapping[str, StoredObjectExpectation],
) -> bool:
    for kind, expectation in expectations.items():
        if kind in {"production_bundle", "manifest"}:
            continue
        entry = _manifest_evidence_entry(kind, inventory)
        if (
            entry is None
            or frozenset(entry) != _MANIFEST_ARTIFACT_ENTRY_KEYS
            or type(entry.get("size_bytes")) is not int
            or entry.get("size_bytes") != expectation.size_bytes
            or entry.get("sha256") != expectation.sha256
            or entry.get("media_type") != expectation.content_type
        ):
            return False
    return True


def _review_document_binding_issues(
    job: GenerationJob,
    version: DesignVersion,
    result_json: Mapping[str, Any],
    expectations: Mapping[str, StoredObjectExpectation],
    verified_documents: Mapping[str, bytes],
) -> list[str]:
    """Bind the successful job claims to their exact readiness and manifest bytes."""

    invalid: list[str] = []
    stored_dfm_report = None
    stored_readiness: WorkshopReadinessReport | None = None
    stored_package_status: DesignReviewPackageStatus | None = None
    readiness_expectation = expectations.get("workshop_readiness")
    readiness_payload = verified_documents.get("workshop_readiness")
    readiness = result_json.get("workshop_readiness")
    try:
        if (
            readiness_expectation is None
            or readiness_payload is None
            or not isinstance(readiness, Mapping)
            or readiness_expectation.content_type != "application/json"
        ):
            raise ValueError("workshop readiness evidence is missing")
        result_readiness_bytes = canonical_json_bytes(readiness)
        if (
            result_readiness_bytes != readiness_payload
            or hashlib.sha256(result_readiness_bytes).hexdigest() != readiness_expectation.sha256
        ):
            raise ValueError("workshop readiness evidence does not match the job")
        stored_readiness_payload = _strict_canonical_json_object(readiness_payload)
        # Validate each source schema in place. Legacy v1 must remain byte-identical v1;
        # normalization is deliberately used only as a strict schema validator.
        normalize_workshop_readiness_report(readiness)
        stored_readiness = normalize_workshop_readiness_report(stored_readiness_payload)
    except (
        ArtifactError,
        ArtifactIntegrityError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        invalid.append("workshop_readiness")

    dfm_expectation = expectations.get("dfm_report")
    dfm_payload = verified_documents.get("dfm_report")
    try:
        if (
            dfm_expectation is None
            or dfm_payload is None
            or dfm_expectation.content_type != "application/json"
        ):
            raise ValueError("DFM report evidence is missing")
        stored_dfm_report = normalize_design_review_dfm_report(
            _strict_canonical_json_object(dfm_payload)
        )
        if result_json.get("dfm_status") != stored_dfm_report.status.value:
            raise ValueError("DFM report evidence does not match the job status")
    except (
        ArtifactIntegrityError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        invalid.append("dfm_report")

    stock_selection_expectation = expectations.get("stock_selection")
    stock_selection_payload = verified_documents.get("stock_selection")
    try:
        if (
            stock_selection_expectation is None
            or stock_selection_payload is None
            or stock_selection_expectation.content_type != "application/json"
        ):
            raise ValueError("stock-selection evidence is missing")
        _strict_canonical_json_object(stock_selection_payload)
        expected_stock_selection = _frozen_stock_selection_snapshot(version)
        if (
            expected_stock_selection is None
            or stock_selection_payload != expected_stock_selection
            or hashlib.sha256(stock_selection_payload).hexdigest()
            != stock_selection_expectation.sha256
        ):
            raise ValueError("stock-selection evidence does not match the frozen job")
    except (
        ArtifactIntegrityError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        invalid.append("stock_selection")

    generation_plan_expectation = expectations.get("generation_plan")
    generation_plan_payload = verified_documents.get("generation_plan")
    try:
        if (
            generation_plan_expectation is None
            or generation_plan_payload is None
            or generation_plan_expectation.content_type != "application/json"
        ):
            raise ValueError("generation-plan evidence is missing")
        _strict_canonical_json_object(generation_plan_payload)
        expected_generation_plan = _frozen_generation_plan_snapshot(job, version)
        if (
            expected_generation_plan is None
            or generation_plan_payload != expected_generation_plan
            or hashlib.sha256(generation_plan_payload).hexdigest()
            != generation_plan_expectation.sha256
        ):
            raise ValueError("generation-plan evidence does not match the frozen job")
    except (
        ArtifactIntegrityError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        invalid.append("generation_plan")

    package_status_expectation = expectations.get("design_review_package_status")
    package_status_payload = verified_documents.get("design_review_package_status")
    raw_package_status = result_json.get("design_review_package_status")
    try:
        if (
            package_status_expectation is None
            or package_status_payload is None
            or not isinstance(raw_package_status, Mapping)
            or package_status_expectation.content_type != "application/json"
        ):
            raise ValueError("design-review package status evidence is missing")
        result_status_bytes = canonical_json_bytes(raw_package_status)
        if (
            result_status_bytes != package_status_payload
            or hashlib.sha256(result_status_bytes).hexdigest() != package_status_expectation.sha256
        ):
            raise ValueError("design-review package status evidence does not match the job")
        stored_status = _strict_canonical_json_object(package_status_payload)
        normalized_result_status = normalize_design_review_package_status(raw_package_status)
        stored_package_status = normalize_design_review_package_status(stored_status)
        frozen_request = job.request_json if isinstance(job.request_json, Mapping) else {}
        if (
            normalized_result_status.cam_status is CAMStageStatus.VALIDATION_GENERATED
            and normalized_result_status.validation_program_included
            is not frozen_request.get("include_validation_program")
        ):
            raise ValueError("generated status does not match generation-plan request")
    except (
        ArtifactIntegrityError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        invalid.append("design_review_package_status")

    manifest_expectation = expectations.get("manifest")
    manifest_payload = verified_documents.get("manifest")
    try:
        if (
            manifest_expectation is None
            or manifest_payload is None
            or readiness_expectation is None
            or dfm_expectation is None
            or stock_selection_expectation is None
            or generation_plan_expectation is None
            or package_status_expectation is None
        ):
            raise ValueError("production manifest evidence is missing")
        manifest = _strict_canonical_json_object(manifest_payload)
        if frozenset(manifest) != _MANIFEST_TOP_LEVEL_KEYS:
            raise ValueError("production manifest has an unexpected structure")
        validate_manifest_context_contract(manifest)
        if (
            manifest.get("schema_version") != PRODUCTION_MANIFEST_SCHEMA_VERSION
            or manifest.get("generation_context_hash") != job.production_context_hash
            or manifest.get("release_scope") != "design_review"
            or manifest.get("machine_use") != "validation_only"
            or manifest.get("physical_cutting_authorized") is not False
            or manifest.get("cad_status") != "GENERATED"
            or manifest.get("checksum_scope") != _MANIFEST_CHECKSUM_SCOPE
        ):
            raise ValueError("production manifest has unsafe or stale claims")

        manifest_context = {field: manifest[field] for field in MANIFEST_CONTEXT_HASH_FIELDS}
        expected_context_hash = hashlib.sha256(canonical_json_bytes(manifest_context)).hexdigest()
        if manifest.get("production_context_hash") != expected_context_hash:
            raise ValueError("production manifest context hash does not match")
        if not _manifest_context_matches_frozen_job(manifest, job, version):
            raise ValueError("production manifest does not match the frozen job")
        if stored_readiness is None:
            raise ValueError("production manifest has no valid workshop readiness evidence")
        frozen_grain_contract = _frozen_grain_contract(version)
        if frozen_grain_contract is None:
            raise ValueError("frozen design grain projection is invalid")
        expected_grain_issues, expected_missing_stock_grain_issues, _ = frozen_grain_contract
        expected_stock_missing_issues = _frozen_stock_missing_issues(version)
        if expected_stock_missing_issues is None:
            raise ValueError("frozen design stock-selection projection is invalid")
        expected_edge_band_selection_required = _frozen_edge_band_selection_required(version)
        if expected_edge_band_selection_required is None:
            raise ValueError("frozen design has no valid edge-band requirement")
        validate_workshop_evidence_binding(
            stored_readiness,
            expected_edge_band_selection_required=expected_edge_band_selection_required,
            expected_material_grain_binding_required=bool(expected_missing_stock_grain_issues),
            external_evidence=manifest["external_evidence"],
        )

        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("production manifest artifact inventory is invalid")
        manifest_inventory = list(validate_manifest_artifact_entries(artifacts))
        if not _manifest_evidence_matches_expectations(manifest_inventory, expectations):
            raise ValueError("production manifest does not bind every evidence artifact")
        readiness_entries: list[dict[str, Any]] = []
        dfm_entries: list[dict[str, Any]] = []
        stock_selection_entries: list[dict[str, Any]] = []
        generation_plan_entries: list[dict[str, Any]] = []
        package_status_entries: list[dict[str, Any]] = []
        for raw_entry in manifest_inventory:
            path = raw_entry["path"]
            role = raw_entry["role"]
            if (
                path.casefold() == _WORKSHOP_READINESS_ARTIFACT_PATH.casefold()
                or role.casefold() == _WORKSHOP_READINESS_ARTIFACT_ROLE.casefold()
            ):
                readiness_entries.append(raw_entry)
            if (
                path.casefold() == _DFM_REPORT_ARTIFACT_PATH.casefold()
                or role.casefold() == _DFM_REPORT_ARTIFACT_ROLE.casefold()
            ):
                dfm_entries.append(raw_entry)
            if (
                path.casefold() == _STOCK_SELECTION_ARTIFACT_PATH.casefold()
                or role.casefold() == _STOCK_SELECTION_ARTIFACT_ROLE.casefold()
            ):
                stock_selection_entries.append(raw_entry)
            if (
                path.casefold() == _GENERATION_PLAN_ARTIFACT_PATH.casefold()
                or role.casefold() == _GENERATION_PLAN_ARTIFACT_ROLE.casefold()
            ):
                generation_plan_entries.append(raw_entry)
            if (
                path.casefold() == DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH.casefold()
                or role.casefold() == DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE.casefold()
            ):
                package_status_entries.append(raw_entry)
        if len(readiness_entries) != 1:
            raise ValueError("production manifest readiness entry is not unique")
        readiness_entry = readiness_entries[0]
        if (
            frozenset(readiness_entry) != {"path", "media_type", "role", "size_bytes", "sha256"}
            or readiness_entry.get("path") != _WORKSHOP_READINESS_ARTIFACT_PATH
            or readiness_entry.get("media_type") != "application/json"
            or readiness_entry.get("role") != _WORKSHOP_READINESS_ARTIFACT_ROLE
            or type(readiness_entry.get("size_bytes")) is not int
            or readiness_entry.get("size_bytes") != readiness_expectation.size_bytes
            or readiness_entry.get("sha256") != readiness_expectation.sha256
        ):
            raise ValueError("production manifest readiness entry does not match")
        if dfm_expectation is None or len(dfm_entries) != 1:
            raise ValueError("production manifest DFM report entry is not unique")
        dfm_entry = dfm_entries[0]
        if (
            frozenset(dfm_entry) != {"path", "media_type", "role", "size_bytes", "sha256"}
            or dfm_entry.get("path") != _DFM_REPORT_ARTIFACT_PATH
            or dfm_entry.get("media_type") != "application/json"
            or dfm_entry.get("role") != _DFM_REPORT_ARTIFACT_ROLE
            or type(dfm_entry.get("size_bytes")) is not int
            or dfm_entry.get("size_bytes") != dfm_expectation.size_bytes
            or dfm_entry.get("sha256") != dfm_expectation.sha256
        ):
            raise ValueError("production manifest DFM report entry does not match")
        if len(stock_selection_entries) != 1:
            raise ValueError("production manifest stock-selection entry is not unique")
        stock_selection_entry = stock_selection_entries[0]
        if (
            frozenset(stock_selection_entry)
            != {"path", "media_type", "role", "size_bytes", "sha256"}
            or stock_selection_entry.get("path") != _STOCK_SELECTION_ARTIFACT_PATH
            or stock_selection_entry.get("media_type") != "application/json"
            or stock_selection_entry.get("role") != _STOCK_SELECTION_ARTIFACT_ROLE
            or type(stock_selection_entry.get("size_bytes")) is not int
            or stock_selection_entry.get("size_bytes") != stock_selection_expectation.size_bytes
            or stock_selection_entry.get("sha256") != stock_selection_expectation.sha256
        ):
            raise ValueError("production manifest stock-selection entry does not match")
        if len(generation_plan_entries) != 1:
            raise ValueError("production manifest generation-plan entry is not unique")
        generation_plan_entry = generation_plan_entries[0]
        if (
            frozenset(generation_plan_entry)
            != {"path", "media_type", "role", "size_bytes", "sha256"}
            or generation_plan_entry.get("path") != _GENERATION_PLAN_ARTIFACT_PATH
            or generation_plan_entry.get("media_type") != "application/json"
            or generation_plan_entry.get("role") != _GENERATION_PLAN_ARTIFACT_ROLE
            or type(generation_plan_entry.get("size_bytes")) is not int
            or generation_plan_entry.get("size_bytes") != generation_plan_expectation.size_bytes
            or generation_plan_entry.get("sha256") != generation_plan_expectation.sha256
        ):
            raise ValueError("production manifest generation-plan entry does not match")
        if len(package_status_entries) != 1:
            raise ValueError("production manifest package-status entry is not unique")
        package_status_entry = package_status_entries[0]
        if (
            frozenset(package_status_entry)
            != {"path", "media_type", "role", "size_bytes", "sha256"}
            or package_status_entry.get("path") != DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH
            or package_status_entry.get("media_type") != "application/json"
            or package_status_entry.get("role") != DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE
            or type(package_status_entry.get("size_bytes")) is not int
            or package_status_entry.get("size_bytes") != package_status_expectation.size_bytes
            or package_status_entry.get("sha256") != package_status_expectation.sha256
        ):
            raise ValueError("production manifest package-status entry does not match")
        normalized_package_status = _design_review_package_status(result_json)
        if normalized_package_status is None or stored_package_status is None:
            raise ValueError("schema-v4 production package status is mandatory")
        validate_design_review_status_inventory_entries(
            normalized_package_status,
            manifest_inventory,
        )
        if stored_dfm_report is None:
            raise ValueError("production manifest has no valid DFM report evidence")
        validate_design_review_status_dfm_report(
            normalized_package_status,
            stored_dfm_report,
        )

        blocker_codes = normalized_package_status.blocker_codes
        grain_blocked = blocker_codes == (DFM_GRAIN_BLOCKER_CODE,)
        stock_blocked = blocker_codes == (STOCK_PROFILE_MISSING_CODE,)
        if stored_dfm_report is None or not _stock_report_matches_frozen_version(
            stored_dfm_report,
            expected_stock_missing_issues,
        ):
            raise ValueError("stock blockers do not match the frozen stock selection")
        if bool(expected_stock_missing_issues) is not stock_blocked:
            raise ValueError("package status does not match the frozen stock selection")
        if grain_blocked:
            if (
                stored_dfm_report is None
                or not expected_grain_issues
                or not _grain_report_matches_frozen_version(
                    stored_dfm_report,
                    expected_grain_issues,
                )
            ):
                raise ValueError("grain blocker does not match the frozen design and stock")
        elif stock_blocked:
            if stored_dfm_report is None or not _grain_report_matches_frozen_version(
                stored_dfm_report,
                expected_missing_stock_grain_issues,
            ):
                raise ValueError("stock blocker grain warning does not match the frozen design")
        elif expected_grain_issues:
            raise ValueError("unbound directional stock is not represented by the package status")
    except (
        ArtifactError,
        ArtifactIntegrityError,
        KeyError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        invalid.append("manifest")

    return sorted(set(invalid))


def _review_evidence_issues(
    session: Session,
    organization_id: str,
    job: GenerationJob,
    *,
    stream_hash: bool = False,
    require_cam: bool = True,
    bind_review_documents: bool = False,
) -> tuple[list[str], list[str], bool]:
    """Return missing/invalid evidence without changing the reviewed job."""

    version = session.scalar(
        select(DesignVersion).where(
            DesignVersion.organization_id == organization_id,
            DesignVersion.id == job.design_version_id,
        )
    )
    artifacts = tuple(
        session.scalars(
            select(Artifact).where(
                Artifact.organization_id == organization_id,
                Artifact.generation_job_id == job.id,
            )
        )
    )
    by_kind = {artifact.kind: artifact for artifact in artifacts}
    expectations, result_invalid = _artifact_expectations(job)
    invalid = list(result_invalid)
    if version is None:
        invalid.append("design_version")
    result_json = job.result_json if isinstance(job.result_json, dict) else {}
    raw_package_status = result_json.get("design_review_package_status")
    package_status = _design_review_package_status(result_json)
    if raw_package_status is None or package_status is None:
        invalid.append("design_review_package_status")
    cam_blocked = package_status is not None and package_status.cam_status is CAMStageStatus.BLOCKED
    required = {
        "production_bundle",
        "manifest",
        "dfm_report",
        "stock_selection",
        "generation_plan",
        "design_review_package_status",
        "design_glb",
        "workshop_readiness",
    }
    if not cam_blocked or require_cam:
        required.update({"operations", "validation_backplot"})
    missing = sorted((required | set(expectations)) - set(by_kind))
    if (not cam_blocked or require_cam) and not any(
        kind.startswith("setup_sheet_") for kind in by_kind
    ):
        missing.append("setup_sheet_001")
    if cam_blocked:
        forbidden_kinds = {
            kind
            for kind in {*by_kind, *expectations}
            if _blocked_cam_evidence_kind_is_forbidden(kind)
        }
        invalid.extend(sorted(forbidden_kinds))
    invalid.extend(sorted(set(by_kind) - set(expectations)))
    readiness_valid = _design_review_package_is_valid(result_json, require_cam=require_cam)
    if result_invalid:
        # Result metadata is the authority for the exact storage inventory.  If
        # it is ambiguous, fail closed before reading any caller-selected key.
        return sorted(set(missing)), sorted(set(invalid)), readiness_valid
    verified_documents: dict[str, bytes] = {}
    for kind, expectation in expectations.items():
        artifact = by_kind.get(kind)
        if artifact is None:
            continue
        if (
            artifact.object_key != expectation.object_key
            or artifact.sha256 != expectation.sha256
            or artifact.size_bytes != expectation.size_bytes
            or artifact.content_type != expectation.content_type
        ):
            invalid.append(kind)
            continue
        try:
            if (stream_hash or bind_review_documents) and kind == "workshop_readiness":
                verified_documents[kind] = read_verified_stored_object(
                    expectation,
                    max_bytes=_WORKSHOP_READINESS_MAX_BYTES,
                )
            elif (stream_hash or bind_review_documents) and kind == "dfm_report":
                verified_documents[kind] = read_verified_stored_object(
                    expectation,
                    max_bytes=_DFM_REPORT_MAX_BYTES,
                )
            elif (stream_hash or bind_review_documents) and kind == "design_review_package_status":
                verified_documents[kind] = read_verified_stored_object(
                    expectation,
                    max_bytes=_DESIGN_REVIEW_PACKAGE_STATUS_MAX_BYTES,
                )
            elif (stream_hash or bind_review_documents) and kind == "stock_selection":
                verified_documents[kind] = read_verified_stored_object(
                    expectation,
                    max_bytes=_STOCK_SELECTION_MAX_BYTES,
                )
            elif (stream_hash or bind_review_documents) and kind == "generation_plan":
                verified_documents[kind] = read_verified_stored_object(
                    expectation,
                    max_bytes=_GENERATION_PLAN_MAX_BYTES,
                )
            elif (stream_hash or bind_review_documents) and kind == "manifest":
                verified_documents[kind] = read_verified_stored_object(
                    expectation,
                    max_bytes=_PRODUCTION_MANIFEST_MAX_BYTES,
                )
            else:
                verify_stored_object(expectation, stream_hash=stream_hash)
        except ArtifactIntegrityError:
            invalid.append(kind)
    if (stream_hash or bind_review_documents) and version is not None:
        invalid.extend(
            _review_document_binding_issues(
                job,
                version,
                result_json,
                expectations,
                verified_documents,
            )
        )
    return sorted(set(missing)), sorted(set(invalid)), readiness_valid


def _blocked_cam_evidence_kind_is_forbidden(kind: str) -> bool:
    return kind not in _BLOCKED_CAM_ALLOWED_EVIDENCE_KINDS


def _artifact_expectations(
    job: GenerationJob,
) -> tuple[dict[str, StoredObjectExpectation], list[str]]:
    """Build the one exact artifact set committed by the successful job result."""

    result = job.result_json
    if not isinstance(result, dict):
        return {}, ["generation_result"]
    expectations: dict[str, StoredObjectExpectation] = {}
    normalized_kinds: set[str] = set()
    invalid: list[str] = []

    def add(
        kind: object,
        object_key: object,
        sha256: object,
        size_bytes: object,
        content_type: object,
        *,
        required_metadata: Mapping[str, object] | None = None,
    ) -> None:
        kind_value = kind if isinstance(kind, str) else ""
        normalized_kind = kind_value.casefold()
        metadata_items = tuple(sorted((required_metadata or {}).items()))
        if (
            not kind_value
            or kind_value in expectations
            or normalized_kind in normalized_kinds
            or (
                normalized_kind
                in {
                    "dfm_report",
                    "workshop_readiness",
                    "design_review_package_status",
                    "stock_selection",
                    "generation_plan",
                }
                and kind_value != normalized_kind
            )
            or not isinstance(object_key, str)
            or not object_key
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes <= 0
            or not isinstance(content_type, str)
            or not content_type
            or any(
                not isinstance(key, str)
                or not key
                or key != key.casefold()
                or not isinstance(value, str)
                or not value
                or (
                    key == "manifest-sha256"
                    and (
                        len(value) != 64
                        or any(character not in "0123456789abcdef" for character in value)
                    )
                )
                for key, value in metadata_items
            )
            or (
                kind_value
                in {
                    "dfm_report",
                    "workshop_readiness",
                    "design_review_package_status",
                    "stock_selection",
                    "generation_plan",
                }
                and content_type != "application/json"
            )
        ):
            invalid.append(kind_value or "generation_result")
            return
        normalized_kinds.add(normalized_kind)
        expectations[kind_value] = StoredObjectExpectation(
            object_key=object_key,
            sha256=sha256,
            size_bytes=size_bytes,
            content_type=content_type,
            required_metadata=tuple((str(key), str(value)) for key, value in metadata_items),
        )

    add(
        "production_bundle",
        result.get("bundle_object_key"),
        result.get("bundle_sha256"),
        result.get("bundle_size_bytes"),
        "application/zip",
        required_metadata={"manifest-sha256": result.get("manifest_sha256")},
    )
    add(
        "manifest",
        result.get("manifest_object_key"),
        result.get("manifest_sha256"),
        result.get("manifest_size_bytes"),
        "application/json",
    )
    evidence = result.get("evidence_artifacts")
    if not isinstance(evidence, list):
        invalid.append("evidence_artifacts")
    else:
        for item in evidence:
            if not isinstance(item, dict):
                invalid.append("evidence_artifacts")
                continue
            add(
                item.get("kind"),
                item.get("object_key"),
                item.get("sha256"),
                item.get("size_bytes"),
                item.get("content_type"),
            )
    return expectations, sorted(set(invalid))


def _storage_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Production evidence storage is temporarily unavailable; try again later",
    )


def _require_review_evidence(
    session: Session,
    organization_id: str,
    job: GenerationJob,
    *,
    stream_hash: bool,
    require_cam: bool = True,
    bind_review_documents: bool = False,
) -> None:
    """Require persisted, checksum-addressed evidence for the exact successful job."""

    try:
        missing, invalid, readiness_valid = _review_evidence_issues(
            session,
            organization_id,
            job,
            stream_hash=stream_hash,
            require_cam=require_cam,
            bind_review_documents=bind_review_documents,
        )
    except ArtifactStorageUnavailableError as exc:
        raise _storage_unavailable() from exc
    if missing or invalid or not readiness_valid:
        raise HTTPException(
            status_code=409,
            detail="Production evidence failed integrity verification; regenerate the package",
        )


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


@router.get("/capabilities/templates")
def template_capabilities(principal: PrincipalDep) -> dict[str, object]:
    """Return server-owned support claims; client assertions are never trusted."""

    return template_capability_registry_payload()


@router.post("/designs/preview")
def preview_design(
    payload: BookcasePreviewInput,
    session: SessionDep,
    principal: PrincipalDep,
    project_id: str | None = None,
) -> dict[str, Any]:
    project = tenant_project(session, principal, project_id) if project_id else None
    try:
        _, _, presented = preview(
            payload.model_dump(exclude_none=True),
            design_id=project.id if project else "preview",
            revision=project.current_revision + 1 if project else 1,
        )
        return presented
    except (ValueError, ValidationError) as exc:
        raise _validation_error(exc) from exc
    except RuleEngineUnavailable as exc:
        raise _rule_engine_error(exc) from exc


@router.post("/designs/autofix")
def autofix_design(
    payload: BookcasePreviewInput,
    session: SessionDep,
    principal: DesignerDep,
    project_id: str | None = None,
) -> dict[str, Any]:
    project = tenant_project(session, principal, project_id) if project_id else None
    try:
        _, _, presented = auto_fix(
            payload.model_dump(exclude_none=True),
            design_id=project.id if project else "preview",
            revision=project.current_revision + 1 if project else 1,
        )
        return presented
    except (ValueError, ValidationError) as exc:
        raise _validation_error(exc) from exc
    except RuleEngineUnavailable as exc:
        raise _rule_engine_error(exc) from exc


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


@router.get(
    "/projects/{project_id}/evidence",
    response_model=list[ExternalEvidenceRead],
)
def list_external_evidence(
    project_id: str,
    session: SessionDep,
    principal: PrincipalDep,
) -> list[ExternalEvidence]:
    project = tenant_project(session, principal, project_id)
    return list(
        session.scalars(
            select(ExternalEvidence)
            .where(
                ExternalEvidence.organization_id == principal.organization_id,
                ExternalEvidence.project_id == project.id,
                ExternalEvidence.revoked_at.is_(None),
            )
            .order_by(ExternalEvidence.created_at.desc())
        )
    )


@router.post(
    "/projects/{project_id}/evidence",
    response_model=ExternalEvidenceRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_external_evidence(
    project_id: str,
    session: SessionDep,
    principal: ReviewerDep,
    document: Annotated[UploadFile, File()],
    evidence_type: Annotated[Literal["wall_anchor", "hardware", "material_grain"], Form()],
    rule_id: Annotated[str, Form(pattern=r"^(CB|DFM)-[A-Z]+-[0-9]{3}$")],
    catalog_id: Annotated[str, Form(min_length=1, max_length=160)],
    catalog_version: Annotated[str, Form(min_length=1, max_length=80)],
    design_hash: Annotated[str, Form(pattern=r"^[a-f0-9]{64}$")],
    expires_at: Annotated[datetime | None, Form()] = None,
) -> ExternalEvidence:
    project = tenant_project(session, principal, project_id)
    expected_type = EVIDENCE_RULE_TYPES.get(rule_id)
    if expected_type != evidence_type:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "EXTERNAL_EVIDENCE_TYPE_MISMATCH",
                "message": f"{rule_id} requires evidence_type={expected_type!r}.",
                "solution": "Choose the control type shown by the server validation result.",
            },
        )
    belongs_to_project = project.draft_design_hash == design_hash or session.scalar(
        select(DesignVersion.id).where(
            DesignVersion.organization_id == principal.organization_id,
            DesignVersion.project_id == project.id,
            DesignVersion.design_hash == design_hash,
        )
    )
    if not belongs_to_project:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXTERNAL_EVIDENCE_DESIGN_MISMATCH",
                "message": "Evidence must be bound to a design hash in this project.",
                "solution": "Save the current draft and upload evidence against its shown hash.",
            },
        )
    if expires_at is not None:
        normalized_expiry = (
            expires_at.replace(tzinfo=UTC) if expires_at.tzinfo is None else expires_at
        )
        if normalized_expiry <= datetime.now(UTC):
            raise HTTPException(status_code=422, detail="Evidence expiry must be in the future")
    content = await document.read(20 * 1024 * 1024 + 1)
    try:
        validate_upload(content, document.content_type or "", document.filename or "")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "EXTERNAL_EVIDENCE_INVALID",
                "message": str(exc),
                "solution": (
                    "Choose a supported image, PDF or DXF no larger than 20 MiB and retry."
                ),
            },
        ) from exc
    evidence_id = str(uuid4())
    digest = hashlib.sha256(content).hexdigest()
    extension = (document.filename or "evidence").rpartition(".")[2].lower()
    object_key = (
        f"{principal.organization_id}/projects/{project.id}/external-evidence/"
        f"{evidence_id}/document.{extension}"
    )
    try:
        store_evidence_object(
            object_key,
            content,
            document.content_type or "application/octet-stream",
            digest,
        )
    except ArtifactIntegrityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ArtifactStorageUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "EXTERNAL_EVIDENCE_STORAGE_UNAVAILABLE",
                "message": "The evidence document could not be stored safely.",
                "solution": "Retry after object storage is healthy.",
            },
        ) from exc
    evidence = ExternalEvidence(
        id=evidence_id,
        organization_id=principal.organization_id,
        project_id=project.id,
        evidence_type=evidence_type,
        rule_id=rule_id,
        catalog_id=catalog_id.strip(),
        catalog_version=catalog_version.strip(),
        design_hash=design_hash,
        object_key=object_key,
        sha256=digest,
        size_bytes=len(content),
        content_type=document.content_type or "application/octet-stream",
        created_by=principal.user_id,
        expires_at=expires_at,
    )
    session.add(evidence)
    session.flush()
    audit(
        session,
        principal,
        "external_evidence.created",
        "external_evidence",
        evidence.id,
        {
            "evidence_type": evidence.evidence_type,
            "rule_id": evidence.rule_id,
            "catalog_id": evidence.catalog_id,
            "catalog_version": evidence.catalog_version,
            "design_hash": evidence.design_hash,
            "sha256": evidence.sha256,
        },
    )
    return evidence


@router.get("/projects/{project_id}/draft", response_model=ProjectDraftRead)
def get_project_draft(
    project_id: str,
    session: SessionDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    project = tenant_project(session, principal, project_id)
    return {
        "project_id": project.id,
        "draft_revision": project.draft_revision,
        "template_id": project.draft_template_id,
        "design_hash": project.draft_design_hash,
        "spec_json": project.draft_spec_json,
        "workspace_spec_json": project.draft_workspace_json,
        "result_json": project.draft_result_json,
        "updated_at": project.updated_at,
    }


@router.put("/projects/{project_id}/draft", response_model=ProjectDraftRead)
def update_project_draft(
    project_id: str,
    payload: ProjectDraftUpdate,
    session: SessionDep,
    principal: DesignerDep,
) -> dict[str, Any]:
    project = tenant_project(session, principal, project_id)
    session.refresh(project, with_for_update=True)
    if payload.expected_draft_revision != project.draft_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DRAFT_REVISION_CONFLICT",
                "message": "The project draft was changed by another editor.",
                "solution": (
                    "Reload the latest project draft, review the other editor's changes, "
                    "then apply and save your changes again."
                ),
                "expected_draft_revision": payload.expected_draft_revision,
                "current_draft_revision": project.draft_revision,
            },
        )
    try:
        template_capability = resolve_template_capability(payload.template_id)
        if payload.spec.furniture_type not in template_capability.allowed_furniture_types:
            raise TemplateCapabilityError(
                "TEMPLATE_FURNITURE_TYPE_MISMATCH",
                (
                    f"Template {payload.template_id!r} cannot represent furniture_type "
                    f"{payload.spec.furniture_type!r}."
                ),
                "Restore the selected template defaults before saving the draft.",
                payload.template_id,
            )
    except TemplateCapabilityError as exc:
        raise _template_capability_error(exc) from exc
    try:
        _, result, presented = canonical_preview(
            payload.spec.model_dump(exclude_none=True),
            design_id=project.id,
            revision=project.current_revision + 1,
        )
    except (ValueError, ValidationError) as exc:
        raise _validation_error(exc) from exc
    except RuleEngineUnavailable as exc:
        raise _rule_engine_error(exc) from exc

    project.draft_template_id = payload.template_id
    project.draft_design_hash = result.design_hash
    project.draft_spec_json = payload.spec.model_dump(exclude_none=True)
    project.draft_workspace_json = payload.workspace_spec.model_dump(mode="json", exclude_none=True)
    project.draft_result_json = {
        **presented,
        "template_capability": template_capability.snapshot(),
    }
    project.draft_updated_by = principal.user_id
    project.furniture_type = payload.spec.furniture_type
    previous_draft_revision = project.draft_revision
    project.draft_revision += 1
    session.flush()
    audit(
        session,
        principal,
        "project.draft.updated",
        "project",
        project.id,
        {
            "design_hash": result.design_hash,
            "template_id": payload.template_id,
            "previous_draft_revision": previous_draft_revision,
            "draft_revision": project.draft_revision,
        },
    )
    return {
        "project_id": project.id,
        "draft_revision": project.draft_revision,
        "template_id": project.draft_template_id,
        "design_hash": project.draft_design_hash,
        "spec_json": project.draft_spec_json,
        "workspace_spec_json": project.draft_workspace_json,
        "result_json": project.draft_result_json,
        "updated_at": project.updated_at,
    }


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
    requested_source_provenance = (
        payload.source_provenance.model_dump(mode="json")
        if payload.source_provenance is not None
        else {}
    )
    source_provenance: dict[str, Any] = {}
    source_import: ImportedAsset | None = None
    production_context = payload.production_context.model_dump(mode="json")
    try:
        template_capability = require_template_for_revision(
            payload.template_id, payload.spec.furniture_type
        )
    except TemplateCapabilityError as exc:
        raise _template_capability_error(exc) from exc
    try:
        spec, result, presented = canonical_preview(
            payload.spec.model_dump(exclude_none=True),
            design_id=project.id,
            revision=revision,
        )
    except (ValueError, ValidationError) as exc:
        raise _validation_error(exc) from exc
    except RuleEngineUnavailable as exc:
        raise _rule_engine_error(exc) from exc
    if result.design_hash != payload.expected_design_hash:
        raise HTTPException(
            status_code=409,
            detail=(
                "EXPECTED_DESIGN_HASH_MISMATCH: the active server preview is no longer "
                "current; wait for preview synchronization and save again"
            ),
        )

    if requested_source_provenance:
        source_import, source_provenance = _verified_reference_provenance(
            session,
            principal,
            project,
            requested_source_provenance,
            design_hash=result.design_hash,
        )

    presented = {
        **presented,
        "production_context": production_context,
        "template_capability": template_capability.snapshot(),
    }

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
            "source_provenance": source_provenance,
            "production_context": production_context,
            "template_capability": template_capability.snapshot(),
            "materials": sorted(
                materials,
                key=lambda item: (item["material_id"], item["version"]),
            ),
        }
    )

    existing = session.scalar(
        select(DesignVersion).where(
            DesignVersion.organization_id == principal.organization_id,
            DesignVersion.project_id == project.id,
            DesignVersion.revision == project.current_revision,
            DesignVersion.design_hash == result.design_hash,
        )
    )
    if (
        existing is not None
        and not existing.immutable
        and existing.status not in {DesignStatus.superseded, DesignStatus.archived}
        and existing.context_hash == context_hash
        and existing.source_provenance_json == source_provenance
        and existing.result_json.get("production_context") == production_context
        and existing.template_id == template_capability.template_id
        and existing.template_capability_fingerprint == template_capability.capability_fingerprint
    ):
        return existing
    if project.current_revision != payload.expected_current_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "EXPECTED_CURRENT_REVISION_MISMATCH: another session created a newer "
                "revision; fetch current production state and review it before saving"
            ),
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
            job.lease_token = None
            job.lease_expires_at = None
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
        source_provenance_json=source_provenance,
        source_import_id=source_import.id if source_import is not None else None,
        result_json=presented,
        engine_version=result.engine_version,
        template_version=f"bookcase@{result.template_version}",
        template_id=template_capability.template_id,
        template_capability_fingerprint=template_capability.capability_fingerprint,
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
        {
            "revision": revision,
            "design_hash": result.design_hash,
            "source": source_provenance.get("source", "parametric_template"),
            "production_context": production_context,
            "template_capability": template_capability.snapshot(),
        },
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


@router.get(
    "/projects/{project_id}/production-state",
    response_model=ProductionStateRead,
)
def get_production_state(
    project_id: str,
    session: SessionDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    """Return server-authoritative state for the project's current revision.

    The web client may cache this response for responsiveness, but must use this
    endpoint to restore approvals, jobs and releases after reload or device
    changes. Returning an empty state for a project without revisions keeps the
    first production visit read-only and side-effect free.
    """

    project = tenant_project(session, principal, project_id)
    if project.current_revision < 1:
        return {
            "project_id": project.id,
            "version": None,
            "approvals": [],
            "latest_job": None,
            "release": None,
        }

    version = tenant_version(session, principal, project.id, project.current_revision)
    approvals = list(
        session.scalars(
            select(Approval)
            .where(
                Approval.organization_id == principal.organization_id,
                Approval.design_version_id == version.id,
            )
            .order_by(Approval.created_at.asc())
        )
    )
    latest_job = session.scalar(
        select(GenerationJob)
        .where(
            GenerationJob.organization_id == principal.organization_id,
            GenerationJob.design_version_id == version.id,
        )
        .order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc())
        .limit(1)
    )
    release = session.scalar(
        select(Release).where(
            Release.organization_id == principal.organization_id,
            Release.design_version_id == version.id,
        )
    )
    release_payload: dict[str, Any] | None = None
    if release is not None:
        release_payload = {
            "release_id": release.id,
            "release_number": release.release_number,
            "status": "released",
            "manifest_sha256": release.manifest_sha256,
            "release_kind": "design_review",
            "machine_use": "validation_only",
        }
    return {
        "project_id": project.id,
        "version": version,
        "approvals": approvals,
        "latest_job": latest_job,
        "release": release_payload,
    }


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
    _require_rule_engine()
    _require_frozen_template_capability(version)
    _verify_frozen_reference_asset(session, principal, version)
    if _frozen_grain_contract(version) is None:
        raise HTTPException(
            status_code=409,
            detail="The frozen design grain projection is missing or stale",
        )
    if version.immutable:
        raise HTTPException(status_code=409, detail="Immutable revisions cannot be changed")
    if version.result_json.get("status") == "BLOCK":
        raise HTTPException(status_code=409, detail="Blocking construction or DFM rules remain")
    if version.status == DesignStatus.draft:
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
    session.refresh(version, with_for_update=True)
    _require_rule_engine()
    _require_frozen_template_capability(version)
    _verify_frozen_reference_asset(session, principal, version)
    if _frozen_grain_contract(version) is None:
        raise HTTPException(
            status_code=409,
            detail="The frozen design grain projection is missing or stale",
        )
    if version.status not in {
        DesignStatus.design_validated,
        DesignStatus.cam_validated,
        DesignStatus.approved,
    }:
        raise HTTPException(
            status_code=409, detail="Design validation is required before generation"
        )
    try:
        frozen_production_context = RevisionProductionContext.model_validate(
            version.result_json.get("production_context")
        ).model_dump(mode="json")
    except ValidationError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "FROZEN_PRODUCTION_CONTEXT_MISSING: save a new design revision "
                "before generating production evidence"
            ),
        ) from exc
    requested_production_context = {
        "stock_width_mm": payload.stock_width_mm,
        "stock_height_mm": payload.stock_height_mm,
        "stock_count": payload.stock_count,
        "back_stock_width_mm": payload.back_stock_width_mm,
        "back_stock_height_mm": payload.back_stock_height_mm,
        "back_stock_count": payload.back_stock_count,
        "machine_profile_id": payload.machine_profile_id,
    }
    if requested_production_context != frozen_production_context:
        raise HTTPException(
            status_code=409,
            detail=(
                "FROZEN_PRODUCTION_CONTEXT_MISMATCH: stock format, stock count, "
                "back-stock format or machine profile changed; save a new design "
                "revision before generating production evidence"
            ),
        )
    request_json = payload.model_dump(mode="json")
    request_json["external_evidence"] = _verified_external_evidence(
        session,
        principal.organization_id,
        version.project_id,
        version.design_hash,
        payload.external_evidence_ids,
    )
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
    if design_approval is None:
        raise HTTPException(
            status_code=409,
            detail="Explicit design approval is required before generation",
        )
    design_approval = _require_current_design_approval(
        session, principal.organization_id, design_approval, version
    )
    if warning_rule_ids:
        approved_rule_ids = {str(item.get("rule_id")) for item in design_approval.overrides_json}
        if approved_rule_ids != warning_rule_ids:
            raise HTTPException(
                status_code=409,
                detail="Every warning requires a bound reviewer override before generation",
            )
    request_json["approved_warning_overrides"] = design_approval.overrides_json
    request_json["approved_design_review"] = _design_approval_snapshot(design_approval)
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
            **get_settings().build_identity,
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
        select(GenerationJob)
        .where(
            GenerationJob.organization_id == principal.organization_id,
            GenerationJob.idempotency_key == idempotency_key,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.status == JobStatus.failed:
            previous_error = existing.error
            retry_started_at = datetime.now(UTC)
            session.execute(
                delete(Artifact).where(
                    Artifact.organization_id == principal.organization_id,
                    Artifact.generation_job_id == existing.id,
                )
            )
            existing.status = JobStatus.queued
            existing.attempts = 0
            existing.lease_token = None
            existing.lease_expires_at = None
            existing.deadline_at = retry_started_at + GENERATION_JOB_TIMEOUT
            existing.error = None
            existing.result_json = None
            existing.started_at = None
            existing.finished_at = None
            session.add(
                OutboxEvent(
                    organization_id=principal.organization_id,
                    event_key=f"generation-manual-retry:{uuid4()}",
                    topic="generation.requested",
                    payload_json={
                        "job_id": existing.id,
                        "organization_id": principal.organization_id,
                    },
                )
            )
            audit(
                session,
                principal,
                "generation.requeued",
                "generation_job",
                existing.id,
                {"previous_error": previous_error},
            )
        elif existing.status == JobStatus.succeeded:
            try:
                missing, invalid, readiness_valid = _review_evidence_issues(
                    session,
                    principal.organization_id,
                    existing,
                    stream_hash=True,
                    require_cam=False,
                )
            except ArtifactStorageUnavailableError as exc:
                raise _storage_unavailable() from exc
            if missing or invalid or not readiness_valid:
                latest_job = session.scalar(
                    select(GenerationJob)
                    .where(
                        GenerationJob.organization_id == principal.organization_id,
                        GenerationJob.design_version_id == version.id,
                    )
                    .order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc())
                    .limit(1)
                    .with_for_update()
                )
                if latest_job is None or latest_job.id != existing.id:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Only the latest generation job can be repaired; "
                            "restore the current production state"
                        ),
                    )
                removed_artifact_kinds = list(
                    session.scalars(
                        select(Artifact.kind).where(
                            Artifact.organization_id == principal.organization_id,
                            Artifact.generation_job_id == existing.id,
                        )
                    )
                )
                session.execute(
                    delete(Artifact).where(
                        Artifact.organization_id == principal.organization_id,
                        Artifact.generation_job_id == existing.id,
                    )
                )
                repaired_cam_approval_ids = list(
                    session.scalars(
                        select(Approval.id).where(
                            Approval.organization_id == principal.organization_id,
                            Approval.design_version_id == version.id,
                            Approval.approval_type == "cam",
                            Approval.generation_job_id == existing.id,
                        )
                    )
                )
                session.execute(
                    delete(Approval).where(
                        Approval.organization_id == principal.organization_id,
                        Approval.design_version_id == version.id,
                        Approval.approval_type == "cam",
                        Approval.generation_job_id == existing.id,
                    )
                )
                remaining_cam_approval_ids = list(
                    session.scalars(
                        select(Approval.id).where(
                            Approval.organization_id == principal.organization_id,
                            Approval.design_version_id == version.id,
                            Approval.approval_type == "cam",
                        )
                    )
                )
                if not remaining_cam_approval_ids and version.status in {
                    DesignStatus.cam_validated,
                    DesignStatus.approved,
                }:
                    version.status = DesignStatus.design_validated
                existing.status = JobStatus.queued
                retry_started_at = datetime.now(UTC)
                existing.attempts = 0
                existing.lease_token = None
                existing.lease_expires_at = None
                existing.deadline_at = retry_started_at + GENERATION_JOB_TIMEOUT
                existing.error = None
                existing.result_json = None
                existing.started_at = None
                existing.finished_at = None
                session.add(
                    OutboxEvent(
                        organization_id=principal.organization_id,
                        event_key=f"generation-evidence-repair:{existing.id}:{uuid4()}",
                        topic="generation.requested",
                        payload_json={
                            "job_id": existing.id,
                            "organization_id": principal.organization_id,
                            "reason": "incomplete_review_evidence",
                        },
                    )
                )
                audit(
                    session,
                    principal,
                    "generation.evidence_repair_queued",
                    "generation_job",
                    existing.id,
                    {
                        "missing": missing,
                        "invalid": invalid,
                        "readiness_report": readiness_valid,
                        "removed_artifact_kinds": sorted(removed_artifact_kinds),
                        "cam_approval_invalidated": bool(repaired_cam_approval_ids),
                        "cam_approvals_invalidated": len(repaired_cam_approval_ids),
                        "cam_approvals_remaining": len(remaining_cam_approval_ids),
                    },
                )
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
        deadline_at=datetime.now(UTC) + GENERATION_JOB_TIMEOUT,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        winner = session.scalar(
            select(GenerationJob)
            .where(
                GenerationJob.organization_id == principal.organization_id,
                GenerationJob.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        if winner is None:
            raise
        return winner
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
        select(GenerationJob)
        .where(
            GenerationJob.id == job_id,
            GenerationJob.organization_id == principal.organization_id,
        )
        .with_for_update()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Generation job not found")
    if _expire_generation_job_if_overdue(job, now=datetime.now(UTC)):
        audit(
            session,
            principal,
            "generation.deadline_exceeded",
            "generation_job",
            job.id,
            {"deadline_at": job.deadline_at.isoformat() if job.deadline_at else None},
        )
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
    session.refresh(version, with_for_update=True)
    _require_frozen_template_capability(version)
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
        design_approval = session.scalar(
            select(Approval).where(
                Approval.organization_id == principal.organization_id,
                Approval.design_version_id == version.id,
                Approval.approval_type == "design",
            )
        )
        design_approval = _require_current_design_approval(
            session, principal.organization_id, design_approval, version
        )
        if payload.generation_job_id is None:
            raise HTTPException(
                status_code=422, detail="generation_job_id is required for CAM approval"
            )
        latest_job = session.scalar(
            select(GenerationJob)
            .where(
                GenerationJob.organization_id == principal.organization_id,
                GenerationJob.design_version_id == version.id,
            )
            .order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc())
            .limit(1)
            .with_for_update()
        )
        if latest_job is None or latest_job.id != payload.generation_job_id:
            raise HTTPException(
                status_code=409,
                detail="CAM approval requires the latest generation job",
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
        if approved_job.request_json.get("approved_design_review") != (
            _design_approval_snapshot(design_approval)
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "The generation job is not bound to the current design-warning "
                    "approval; generate a new job"
                ),
            )
        approved_manifest_sha = str(approved_job.result_json.get("manifest_sha256", ""))
        if len(approved_manifest_sha) != 64:
            raise HTTPException(status_code=409, detail="The selected job has no checked manifest")
        _require_review_evidence(
            session,
            principal.organization_id,
            approved_job,
            stream_hash=True,
        )
        if approved_job.request_json.get("include_freecad_project") is True:
            freecad_evidence = {
                artifact.kind: artifact
                for artifact in session.scalars(
                    select(Artifact).where(
                        Artifact.organization_id == principal.organization_id,
                        Artifact.generation_job_id == approved_job.id,
                        Artifact.kind.in_({"design_fcstd", "cad_interchange_status"}),
                    )
                )
            }
            valid_freecad_evidence = set(freecad_evidence) == {
                "design_fcstd",
                "cad_interchange_status",
            } and all(
                artifact.size_bytes > 0
                and len(artifact.sha256) == 64
                and all(character in "0123456789abcdef" for character in artifact.sha256)
                for artifact in freecad_evidence.values()
            )
            if (
                approved_job.result_json.get("freecad_project_generated") is not True
                or not valid_freecad_evidence
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The selected job requested FreeCAD but lacks the verified FCStd "
                        "artifact or its interchange-status evidence"
                    ),
                )
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
        approved_overrides = []
        for rule_id in sorted(warnings):
            if rule_id == DFM_GRAIN_BLOCKER_CODE:
                if supplied[rule_id].evidence_ids:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "DFM_GRAIN_STRUCTURED_BINDING_REQUIRED",
                            "message": (
                                "An opaque material-grain document cannot verify the "
                                "stock profile's X/Y axis."
                            ),
                            "solution": (
                                "Acknowledge the review warning without evidence; CAM remains "
                                "blocked until a structured stock profile binds an exact axis."
                            ),
                        },
                    )
                evidence_snapshots: list[dict[str, Any]] = []
                evidence_status = "acknowledged_unresolved"
            else:
                evidence_snapshots = _verified_external_evidence(
                    session,
                    principal.organization_id,
                    version.project_id,
                    version.design_hash,
                    supplied[rule_id].evidence_ids,
                    expected_rule_id=rule_id,
                )
                evidence_status = "verified" if evidence_snapshots else "missing"
            approved_overrides.append(
                {
                    "rule_id": rule_id,
                    "rule_version": str(warnings[rule_id].get("rule_version", "unknown")),
                    "reason": supplied[rule_id].reason,
                    "approved_by": principal.user_id,
                    "approved_at": approved_at,
                    "evidence_status": evidence_status,
                    "external_evidence": evidence_snapshots,
                }
            )
    approval = session.scalar(
        select(Approval).where(
            Approval.organization_id == principal.organization_id,
            Approval.design_version_id == version.id,
            Approval.approval_type == payload.approval_type,
        )
    )
    design_approval_changed = False
    if payload.approval_type == "design":
        if approval is None:
            design_approval_changed = True
        else:
            existing_override_semantics = [
                {
                    "rule_id": item.get("rule_id"),
                    "rule_version": item.get("rule_version"),
                    "reason": item.get("reason"),
                    "external_evidence": item.get("external_evidence", []),
                }
                for item in approval.overrides_json
            ]
            new_override_semantics = [
                {
                    "rule_id": item.get("rule_id"),
                    "rule_version": item.get("rule_version"),
                    "reason": item.get("reason"),
                    "external_evidence": item.get("external_evidence", []),
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
        if design_approval_changed:
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
    # Only a CAM action that passed the exact job/context/evidence checks above
    # may promote a version. Merely finding two historical approval rows is unsafe.
    if payload.approval_type == "cam":
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
    session.refresh(version, with_for_update=True)
    if version.status == DesignStatus.superseded:
        raise HTTPException(status_code=409, detail="Superseded revisions cannot be released")
    existing_release: Release | None = None
    if version.immutable:
        existing_release = session.scalar(
            select(Release).where(Release.design_version_id == version.id)
        )
        if version.status != DesignStatus.released or existing_release is None:
            raise HTTPException(status_code=409, detail="Immutable version has no release record")
    elif version.status != DesignStatus.approved:
        raise HTTPException(status_code=409, detail="Design and CAM approvals are required")
    design_approval = session.scalar(
        select(Approval).where(
            Approval.organization_id == principal.organization_id,
            Approval.design_version_id == version.id,
            Approval.approval_type == "design",
        )
    )
    design_approval = _require_current_design_approval(
        session, principal.organization_id, design_approval, version
    )
    cam_approval = session.scalar(
        select(Approval).where(
            Approval.organization_id == principal.organization_id,
            Approval.design_version_id == version.id,
            Approval.approval_type == "cam",
        )
    )
    if cam_approval is None or cam_approval.generation_job_id is None:
        raise HTTPException(status_code=409, detail="A bound CAM approval is required")
    latest_job = session.scalar(
        select(GenerationJob)
        .where(
            GenerationJob.organization_id == principal.organization_id,
            GenerationJob.design_version_id == version.id,
        )
        .order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc())
        .limit(1)
        .with_for_update()
    )
    if latest_job is None or latest_job.id != cam_approval.generation_job_id:
        raise HTTPException(
            status_code=409,
            detail="CAM approval is stale because a newer generation job exists",
        )
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
    if job.request_json.get("approved_design_review") != _design_approval_snapshot(design_approval):
        raise HTTPException(
            status_code=409,
            detail=(
                "The checked job is not bound to the current design-warning approval; "
                "generate and review a new job"
            ),
        )
    _require_review_evidence(
        session,
        principal.organization_id,
        job,
        stream_hash=True,
    )
    if job.result_json.get("authoritative_geometry") is not True:
        raise HTTPException(
            status_code=409,
            detail="Release requires genuine server-generated STEP and GLB geometry",
        )
    if not _generation_result_claims_are_safe(job.result_json):
        raise HTTPException(
            status_code=409,
            detail="Release requires a canonical non-blocking DFM status",
        )
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
    if existing_release is not None:
        if existing_release.manifest_sha256 != manifest_sha:
            raise HTTPException(
                status_code=409,
                detail="The stored release does not match the checked production manifest",
            )
        return {
            "release_id": existing_release.id,
            "release_number": existing_release.release_number,
            "status": version.status.value,
            "manifest_sha256": existing_release.manifest_sha256,
            "release_kind": "design_review",
            "machine_use": "validation_only",
        }
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
        "release_kind": "design_review",
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
    _require_review_evidence(
        session,
        principal.organization_id,
        job,
        stream_hash=False,
        require_cam=False,
        bind_review_documents=True,
    )
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
    _require_review_evidence(
        session,
        principal.organization_id,
        job,
        stream_hash=False,
        require_cam=False,
        bind_review_documents=True,
    )
    return RedirectResponse(
        presigned_get(
            artifact.object_key,
            filename=_artifact_filename(artifact.kind, version.revision),
        ),
        status_code=307,
    )


@router.post(
    "/projects/{project_id}/imports/inspect",
    response_model=ImportInspection,
)
async def inspect_import(
    project_id: str,
    session: SessionDep,
    principal: DesignerDep,
    document: Annotated[UploadFile, File()],
) -> ImportInspection:
    project = tenant_project(session, principal, project_id)
    content = await document.read(20 * 1024 * 1024 + 1)
    try:
        validate_upload(content, document.content_type or "", document.filename or "")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "REFERENCE_IMAGE_INVALID",
                "message": str(exc),
                "solution": (
                    "Choose a safe JPG, PNG or WebP file no larger than 20 MiB and retry."
                ),
            },
        ) from exc
    media_type = document.content_type or "application/octet-stream"
    if media_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "REFERENCE_IMAGE_TYPE_UNSUPPORTED",
                "message": "Reference imports accept JPG, PNG or WebP images only.",
                "solution": "Export the screenshot as JPG, PNG or WebP and upload it again.",
            },
        )
    digest = hashlib.sha256(content).hexdigest()
    existing = session.scalar(
        select(ImportedAsset).where(
            ImportedAsset.organization_id == principal.organization_id,
            ImportedAsset.project_id == project.id,
            ImportedAsset.sha256 == digest,
        )
    )
    if existing is not None:
        _verify_imported_asset_object(existing)
        asset = existing
    else:
        asset_id = str(uuid4())
        object_key = (
            f"{principal.organization_id}/projects/{project.id}/reference-imports/sha256/{digest}"
        )
        try:
            store_immutable_object(object_key, content, media_type, digest)
        except ArtifactIntegrityError as exc:
            raise _import_storage_error(unavailable=False) from exc
        except ArtifactStorageUnavailableError as exc:
            raise _import_storage_error(unavailable=True) from exc
        asset = ImportedAsset(
            id=asset_id,
            organization_id=principal.organization_id,
            project_id=project.id,
            sha256=digest,
            object_key=object_key,
            size_bytes=len(content),
            media_type=media_type,
            original_filename=document.filename or "reference-image",
            created_by=principal.user_id,
        )
        try:
            with session.begin_nested():
                session.add(asset)
                session.flush()
        except IntegrityError:
            raced = session.scalar(
                select(ImportedAsset).where(
                    ImportedAsset.organization_id == principal.organization_id,
                    ImportedAsset.project_id == project.id,
                    ImportedAsset.sha256 == digest,
                )
            )
            if raced is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "REFERENCE_ASSET_CONFLICT",
                        "message": "The immutable image record changed during upload.",
                        "solution": "Retry the upload after the current project has refreshed.",
                    },
                ) from None
            _verify_imported_asset_object(raced)
            asset = raced
        else:
            audit(
                session,
                principal,
                "reference_asset.created",
                "imported_asset",
                asset.id,
                {
                    "project_id": project.id,
                    "sha256": asset.sha256,
                    "size_bytes": asset.size_bytes,
                    "media_type": asset.media_type,
                },
            )
    return ImportInspection(
        import_id=asset.id,
        project_id=project.id,
        image_sha256=asset.sha256,
        media_type=asset.media_type,
        size_bytes=asset.size_bytes,
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
