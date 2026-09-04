from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from threading import Lock
from typing import Annotated, Any, Literal, NoReturn
from uuid import uuid4

from custombuild_domain import (
    BackPanelType,
    BookcaseDesignSpec,
    JointRetentionContract,
    JointRetentionLoadMode,
    TemplateCapabilityError,
    build_bookcase,
    dado_joint_geometry_fingerprint,
    joint_support_payload,
    require_template_for_revision,
    resolve_template_capability,
    template_capability_registry_payload,
)
from custombuild_domain.models import (
    JointRetentionApplicationClass,
    captive_inset_back_topology_is_complete,
)
from custombuild_manufacturing import (
    BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
    DFM_GRAIN_BLOCKER_CODE,
    GENERATION_PLAN_ARTIFACT_PATH,
    GENERATION_PLAN_ARTIFACT_ROLE,
    MANIFEST_CONTEXT_HASH_FIELDS,
    MANUFACTURING_INTENT_PATH,
    MANUFACTURING_INTENT_ROLE,
    MAX_CATALOG_SOURCE_BYTES,
    MAX_CORE_DOCUMENT_BYTES,
    MAX_EVIDENCE_ARTIFACTS,
    MAX_EVIDENCE_TOTAL_BYTES,
    MAX_READINESS_STATUS_BYTES,
    STOCK_PROFILE_MISSING_CODE,
    SUPPLIER_HANDOFF_PATH,
    SUPPLIER_HANDOFF_ROLE,
    ArtifactError,
    CAMStageStatus,
    DesignReviewPackageStatus,
    Severity,
    artifact_size_limit,
    back_panel_retention_evidence_missing,
    canonical_json_bytes,
    dado_retention_evidence_missing,
    grain_control_projection,
    normalize_design_review_dfm_report,
    normalize_design_review_package_status,
    valid_artifact_size,
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
from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from .artifact_operations import (
    ArtifactOperationBusyError,
    ArtifactOperationCapacityError,
    ArtifactOperationLeaseManager,
    ArtifactOperationOwnershipLostError,
    ArtifactOperationUnavailableError,
    InMemoryArtifactOperationStore,
    RedisArtifactOperationStore,
)
from .auth import (
    Capability,
    Principal,
    capabilities_for_role,
    get_principal,
    require_capability,
)
from .config import get_settings
from .config_guards import BuildIdentityValues
from .db import get_session_factory
from .design_service import (
    RuleEngineUnavailable,
    assert_rule_engine_available,
    auto_fix,
    bind_joint_retention,
    canonical_preview,
    generation_plan_snapshot_for_design,
    generation_stock_projection_for_design,
    grain_rule_evaluation,
    preview_grain_issues_for_design,
    stock_grain_issues_for_design,
    stock_missing_issues_for_design,
    stock_selection_snapshot_for_design,
)
from .job_policy import GENERATION_JOB_TIMEOUT
from .joint_retention import (
    JOINT_GEOMETRY_FINGERPRINT_SCHEMA,
    MAX_SIGNED_EVIDENCE_BYTES,
    SIGNED_EVIDENCE_SCHEMA_VERSION,
    JointRetentionTrustError,
    resolve_joint_retention_contract,
    validate_signed_retention_evidence_structure,
)
from .joint_retention_registry import (
    JointRetentionRegistryError,
    assert_joint_retention_registry_activated,
    parse_joint_retention_registry_json,
)
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
    StoredObject,
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
    ReleaseArtifactRead,
    ReleaseCreate,
    ReleaseRead,
    RevisionProductionContext,
    WorkshopRunBlockedResponse,
    WorkshopRunBlockerDetail,
    WorkshopRunPrepare,
)
from .security import validate_upload
from .storage import (
    ArtifactIntegrityError,
    ArtifactStorageUnavailableError,
    StoredObjectExpectation,
    VerifiedStoredObject,
    open_verified_stored_object,
    read_verified_stored_object,
    reserve_transient_bytes,
    sign_artifact_access,
    storage_read_deadline,
    store_evidence_object,
    store_immutable_object,
    verify_artifact_access,
    verify_stored_object,
)
from .storage_quota import (
    MAX_GENERATION_RETRY_AFTER_SECONDS,
    StorageClaimConflict,
    StorageObjectClaim,
    StorageQuotaError,
    StorageQuotaExceeded,
    StorageQuotaInvariantError,
    StorageReservationBusy,
    commit_storage_batch_in_transaction,
    generation_retry_after_from_database_error,
    prepare_generation_storage_retry,
    require_committed_storage_binding,
    reserve_storage_batch,
    reserve_storage_batch_in_transaction,
)
from .workshop_readiness_service import (
    WorkshopPreparationBlocker,
    require_workshop_preparation_source,
)

router = APIRouter(prefix="/v1")
# The transaction must commit before FastAPI starts sending the response.  The
# default scope for a yield dependency is ``request``, whose cleanup runs after
# the response has been sent; a client can otherwise receive ``201 Created``
# and immediately fail to read the newly-created row from another connection.
SessionDep = Annotated[Session, Depends(tenant_session, scope="function")]
DownloadSessionDep = Annotated[Session, Depends(tenant_session, scope="request")]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
DesignerDep = Annotated[Principal, Depends(require_capability(Capability.DESIGN))]
GeneratorDep = Annotated[Principal, Depends(require_capability(Capability.GENERATE))]
ReviewerDep = Annotated[Principal, Depends(require_capability(Capability.REVIEW))]
JointRetentionEvidenceDownloadDep = Annotated[
    Principal,
    Depends(require_capability(Capability.JOINT_RETENTION_EVIDENCE_DOWNLOAD)),
]
WorkshopPreparerDep = Annotated[
    Principal,
    Depends(require_capability(Capability.WORKSHOP_PREPARE)),
]

EVIDENCE_RULE_TYPES: dict[str, str] = {
    "CB-TIP-001": "wall_anchor",
    "CB-HARDWARE-001": "hardware",
    "DFM-GRAIN-001": "material_grain",
    "CB-JOINT-001": "joint_retention",
}

_WORKSHOP_READINESS_MAX_BYTES = MAX_READINESS_STATUS_BYTES
_DESIGN_REVIEW_PACKAGE_STATUS_MAX_BYTES = MAX_READINESS_STATUS_BYTES
_STOCK_SELECTION_MAX_BYTES = MAX_CORE_DOCUMENT_BYTES
_GENERATION_PLAN_MAX_BYTES = MAX_CORE_DOCUMENT_BYTES
_DFM_REPORT_MAX_BYTES = MAX_CORE_DOCUMENT_BYTES
_UPLOAD_STORAGE_RESERVATION_LEASE = timedelta(minutes=15)
_PRODUCTION_MANIFEST_MAX_BYTES = MAX_CORE_DOCUMENT_BYTES
_REVIEW_STORAGE_TOTAL_SECONDS = 60.0
_REVIEW_DOCUMENT_MAX_BYTES = {
    "workshop_readiness": _WORKSHOP_READINESS_MAX_BYTES,
    "dfm_report": _DFM_REPORT_MAX_BYTES,
    "design_review_package_status": _DESIGN_REVIEW_PACKAGE_STATUS_MAX_BYTES,
    "stock_selection": _STOCK_SELECTION_MAX_BYTES,
    "generation_plan": _GENERATION_PLAN_MAX_BYTES,
    "manifest": _PRODUCTION_MANIFEST_MAX_BYTES,
}
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
    "manufacturing_intent": (
        MANUFACTURING_INTENT_PATH,
        MANUFACTURING_INTENT_ROLE,
        "application/json",
    ),
    "supplier_handoff": (
        SUPPLIER_HANDOFF_PATH,
        SUPPLIER_HANDOFF_ROLE,
        "application/json",
    ),
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
        "manufacturing_intent",
        "supplier_handoff",
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
_CANONICAL_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_CANONICAL_PROJECT_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
FOUR_EYES_APPROVER_SEPARATION_REQUIRED_CODE = "FOUR_EYES_APPROVER_SEPARATION_REQUIRED"


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
    job.next_attempt_at = None
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


_ExternalEvidenceBinding = tuple[Any, ...]
_ApprovalBinding = tuple[Any, ...]
_RetentionDownloadVersionBinding = tuple[Any, ...]


def _external_evidence_binding(evidence: ExternalEvidence) -> _ExternalEvidenceBinding:
    """Return the complete private DB identity of one verified evidence row."""

    return (
        evidence.id,
        evidence.organization_id,
        evidence.project_id,
        evidence.evidence_type,
        evidence.rule_id,
        evidence.catalog_id,
        evidence.catalog_version,
        evidence.design_hash,
        evidence.object_key,
        evidence.sha256,
        evidence.size_bytes,
        evidence.content_type,
        evidence.created_by,
        evidence.created_at,
        evidence.updated_at,
        evidence.expires_at,
        evidence.revoked_at,
    )


def _retention_download_version_binding(
    version: DesignVersion,
) -> _RetentionDownloadVersionBinding:
    """Freeze every revision field that can affect retention applicability."""

    return (
        version.id,
        version.organization_id,
        version.project_id,
        version.revision,
        version.status,
        version.design_hash,
        version.context_hash,
        canonical_hash(version.spec_json),
        canonical_hash(version.source_provenance_json),
        version.source_import_id,
        canonical_hash(version.result_json),
        version.engine_version,
        version.template_version,
        version.template_id,
        version.template_capability_fingerprint,
        version.rule_version,
        version.created_by,
        version.immutable,
        version.created_at,
        version.updated_at,
    )


def _approval_binding(approval: Approval) -> _ApprovalBinding:
    """Return an immutable comparison value for every approval-owned field."""

    return (
        approval.id,
        approval.organization_id,
        approval.design_version_id,
        approval.approval_type,
        approval.approved_by,
        approval.reason,
        approval.generation_job_id,
        approval.production_context_hash,
        approval.manifest_sha256,
        canonical_hash(approval.overrides_json),
        approval.created_at,
        approval.updated_at,
    )


def _external_evidence_not_found() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "EXTERNAL_EVIDENCE_NOT_FOUND",
            "message": "One or more evidence IDs are missing or belong to another project.",
            "solution": "Upload and select evidence for this exact project and design.",
        },
    )


def _external_evidence_stale(evidence_id: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "EXTERNAL_EVIDENCE_STALE",
            "message": "External evidence is revoked, expired or bound to another design.",
            "solution": "Upload current evidence for this exact design and control.",
            "evidence_id": evidence_id,
        },
    )


def _external_evidence_snapshot_stale() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "EXTERNAL_EVIDENCE_SNAPSHOT_STALE",
            "message": "External evidence changed while its immutable object was being verified.",
            "solution": "Review the current evidence record and retry.",
        },
    )


def _resolve_external_evidence_rows(
    session: Session,
    organization_id: str,
    project_id: str,
    design_hash: str,
    evidence_ids: list[str],
    *,
    expected_rule_id: str | None = None,
    populate_existing: bool = False,
) -> list[ExternalEvidence]:
    """Resolve and validate current evidence rows without touching object storage."""

    if not evidence_ids:
        return []
    query = select(ExternalEvidence).where(
        ExternalEvidence.organization_id == organization_id,
        ExternalEvidence.project_id == project_id,
        ExternalEvidence.id.in_(evidence_ids),
    )
    if populate_existing:
        # A normal ORM SELECT can return the already-cached Python attributes even
        # though PostgreSQL returned a newer row. Force a real final DB refresh.
        query = query.execution_options(populate_existing=True)
    evidence_rows = list(session.scalars(query))
    by_id = {item.id: item for item in evidence_rows}
    if set(by_id) != set(evidence_ids):
        raise _external_evidence_not_found()

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

    now = datetime.now(UTC)
    ordered = [by_id[evidence_id] for evidence_id in evidence_ids]
    for evidence in ordered:
        expires_at = _as_utc(evidence.expires_at) if evidence.expires_at is not None else None
        expected_type = EVIDENCE_RULE_TYPES.get(evidence.rule_id)
        if (
            evidence.revoked_at is not None
            or evidence.design_hash != design_hash
            or expected_type != evidence.evidence_type
            or (expected_rule_id is not None and evidence.rule_id != expected_rule_id)
            or (expires_at is not None and expires_at <= now)
        ):
            raise _external_evidence_stale(evidence.id)
    return ordered


def _require_external_evidence_bindings_current(
    session: Session,
    expected_bindings: Mapping[str, _ExternalEvidenceBinding],
) -> None:
    """Re-read previously verified evidence after later I/O and compare all fields."""

    if not expected_bindings:
        return
    query = (
        select(ExternalEvidence)
        .where(ExternalEvidence.id.in_(sorted(expected_bindings)))
        .execution_options(populate_existing=True)
    )
    current_rows = list(session.scalars(query))
    current = {row.id: row for row in current_rows}
    if set(current) != set(expected_bindings):
        raise _external_evidence_not_found()
    now = datetime.now(UTC)
    for evidence_id in sorted(expected_bindings):
        evidence = current[evidence_id]
        expires_at = _as_utc(evidence.expires_at) if evidence.expires_at is not None else None
        if (
            evidence.revoked_at is not None
            or (expires_at is not None and expires_at <= now)
            or _external_evidence_binding(evidence) != expected_bindings[evidence_id]
        ):
            raise _external_evidence_snapshot_stale()


def _require_approval_binding_current(
    session: Session,
    expected_binding: _ApprovalBinding,
    *,
    approval_id: str,
    organization_id: str,
    design_version_id: str,
    approval_type: str,
    detail: str | dict[str, Any],
) -> Approval:
    """Refresh one exact approval after I/O and reject deletion or any mutation."""

    approval = session.scalar(
        select(Approval)
        .where(
            Approval.id == approval_id,
            Approval.organization_id == organization_id,
            Approval.design_version_id == design_version_id,
            Approval.approval_type == approval_type,
        )
        .execution_options(populate_existing=True)
    )
    if approval is None or _approval_binding(approval) != expected_binding:
        raise HTTPException(status_code=409, detail=detail)
    return approval


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


def _quota_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, StorageQuotaExceeded):
        return HTTPException(
            status_code=507,
            detail={
                "code": "STORAGE_QUOTA_EXCEEDED",
                "message": "The verified object cannot fit inside the durable storage quota.",
                "solution": "Remove retained objects or ask an operator to raise proven capacity.",
            },
        )
    if isinstance(exc, StorageReservationBusy):
        return HTTPException(
            status_code=503,
            detail={
                "code": "STORAGE_RESERVATION_BUSY",
                "message": "The immutable storage batch is owned by another live operation.",
                "solution": "Retry after the active reservation lease expires.",
            },
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    if isinstance(exc, StorageClaimConflict):
        return HTTPException(
            status_code=409,
            detail={
                "code": "STORAGE_IDENTITY_CONFLICT",
                "message": "The immutable storage identity conflicts with an existing claim.",
                "solution": "Retry after the active upload finishes or use a new source file.",
            },
        )
    return HTTPException(
        status_code=503,
        detail={
            "code": "STORAGE_LEDGER_UNAVAILABLE",
            "message": "The durable storage ledger cannot currently prove capacity or identity.",
            "solution": "Retry after database storage accounting is healthy.",
        },
    )


def _generation_storage_retry_busy(retry_after_seconds: int) -> HTTPException:
    if (
        type(retry_after_seconds) is not int
        or retry_after_seconds < 1
        or retry_after_seconds > MAX_GENERATION_RETRY_AFTER_SECONDS
    ):
        return _generation_storage_retry_unavailable()
    return HTTPException(
        status_code=503,
        detail={
            "code": "GENERATION_STORAGE_RETRY_BUSY",
            "message": "Generation storage is owned by an active cleanup operation.",
            "solution": "Retry after the active storage claim expires.",
        },
        headers={"Retry-After": str(retry_after_seconds)},
    )


def _generation_storage_retry_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "GENERATION_STORAGE_RETRY_UNAVAILABLE",
            "message": "Generation storage cannot currently be proven safe for retry.",
            "solution": "Retry after durable storage accounting is healthy.",
        },
    )


def _require_generation_storage_retry_ready(
    session: Session,
    organization_id: str,
    generation_job_id: str,
) -> None:
    try:
        retry_after = prepare_generation_storage_retry(
            session,
            organization_id,
            generation_job_id,
        )
    except StorageReservationBusy as exc:
        raise _generation_storage_retry_busy(exc.retry_after_seconds) from exc
    except StorageQuotaError as exc:
        raise _generation_storage_retry_unavailable() from exc
    if retry_after > 0:
        raise _generation_storage_retry_busy(retry_after)


def _flush_generation_storage_retry(session: Session) -> None:
    """Force the immediate liveness trigger inside the request transaction."""

    try:
        session.flush()
    except DBAPIError as exc:
        try:
            retry_after = generation_retry_after_from_database_error(exc)
        except StorageQuotaInvariantError:
            retry_after = None
        with suppress(SQLAlchemyError):
            session.rollback()
        if retry_after is not None:
            raise _generation_storage_retry_busy(retry_after) from exc
        raise _generation_storage_retry_unavailable() from exc
    except SQLAlchemyError as exc:
        with suppress(SQLAlchemyError):
            session.rollback()
        raise _generation_storage_retry_unavailable() from exc


async def _reserve_upload_storage(
    session: Session,
    organization_id: str,
    claim: StorageObjectClaim,
) -> str:
    """Commit a durable quota claim before the first provider mutation."""

    lease_token = str(uuid4())
    try:
        if session.get_bind().dialect.name == "postgresql":
            await run_in_threadpool(
                reserve_storage_batch,
                get_session_factory(),
                organization_id,
                (claim,),
                lease_token=lease_token,
                lease_duration=_UPLOAD_STORAGE_RESERVATION_LEASE,
                capacity_settings=get_settings(),
            )
        else:
            # SQLite's in-memory StaticPool cannot own an independent
            # transaction on the same connection.  Commit the durable test/dev
            # reservation before provider I/O; production always uses the
            # independent PostgreSQL branch above.
            reserve_storage_batch_in_transaction(
                session,
                organization_id,
                (claim,),
                lease_token=lease_token,
                lease_duration=_UPLOAD_STORAGE_RESERVATION_LEASE,
            )
            session.commit()
    except (StorageClaimConflict, StorageQuotaExceeded, StorageQuotaInvariantError) as exc:
        if session.get_bind().dialect.name != "postgresql":
            session.rollback()
        raise _quota_http_error(exc) from exc
    return lease_token


def _commit_upload_storage(
    session: Session,
    organization_id: str,
    claim: StorageObjectClaim,
    lease_token: str,
) -> None:
    try:
        commit_storage_batch_in_transaction(
            session,
            organization_id,
            (claim,),
            lease_token=lease_token,
        )
    except (StorageClaimConflict, StorageQuotaExceeded, StorageQuotaInvariantError) as exc:
        raise _quota_http_error(exc) from exc


def _require_committed_domain_object(
    session: Session,
    organization_id: str,
    *,
    project_id: str,
    object_key: str,
    sha256: str,
    size_bytes: int,
    media_type: str,
    owner_type: str,
    owner_id: str,
) -> None:
    try:
        require_committed_storage_binding(
            session,
            organization_id,
            project_id=project_id,
            object_key=object_key,
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
            owner_type=owner_type,
            owner_id=owner_id,
        )
    except (StorageClaimConflict, StorageQuotaInvariantError) as exc:
        raise _quota_http_error(exc) from exc


def _verify_imported_asset_object(session: Session, asset: ImportedAsset) -> None:
    _require_committed_domain_object(
        session,
        asset.organization_id,
        project_id=asset.project_id,
        object_key=asset.object_key,
        sha256=asset.sha256,
        size_bytes=asset.size_bytes,
        media_type=asset.media_type,
        owner_type="imported_asset",
        owner_id=asset.id,
    )
    _verify_imported_asset_bytes(asset)


def _verify_imported_asset_bytes(asset: ImportedAsset) -> None:
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
    _verify_imported_asset_object(session, asset)
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
    _verify_imported_asset_object(session, asset)


def _verified_external_evidence(
    session: Session,
    organization_id: str,
    project_id: str,
    design_hash: str,
    evidence_ids: list[str],
    *,
    expected_rule_id: str | None = None,
    verified_bindings: dict[str, _ExternalEvidenceBinding] | None = None,
) -> list[dict[str, Any]]:
    """Resolve tenant records and stream-verify every claimed evidence object."""

    evidence_rows = _resolve_external_evidence_rows(
        session,
        organization_id,
        project_id,
        design_hash,
        evidence_ids,
        expected_rule_id=expected_rule_id,
    )
    initial_bindings = {item.id: _external_evidence_binding(item) for item in evidence_rows}
    for evidence in evidence_rows:
        _require_committed_domain_object(
            session,
            organization_id,
            project_id=evidence.project_id,
            object_key=evidence.object_key,
            sha256=evidence.sha256,
            size_bytes=evidence.size_bytes,
            media_type=evidence.content_type,
            owner_type="external_evidence",
            owner_id=evidence.id,
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

    # Do not repeat the object read: that only moves the race window. Refresh
    # every mutable DB field after the final storage byte was checked.
    current_rows = _resolve_external_evidence_rows(
        session,
        organization_id,
        project_id,
        design_hash,
        evidence_ids,
        expected_rule_id=expected_rule_id,
        populate_existing=True,
    )
    current_bindings = {item.id: _external_evidence_binding(item) for item in current_rows}
    if current_bindings != initial_bindings:
        raise _external_evidence_snapshot_stale()
    if verified_bindings is not None:
        for evidence_id, binding in current_bindings.items():
            previous = verified_bindings.get(evidence_id)
            if previous is not None and previous != binding:
                raise _external_evidence_snapshot_stale()
            verified_bindings[evidence_id] = binding
    return [_evidence_snapshot(evidence) for evidence in current_rows]


def _retention_trust_error(code: str, message: str, solution: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": code, "message": message, "solution": solution},
    )


@dataclass(frozen=True, slots=True)
class VerifiedCurrentRetentionEvidence:
    """Exact signed evidence released only after the complete current-binding gate.

    The bytes are immutable and deliberately excluded from every persisted job or
    Celery payload.  A generation worker may hold this value in memory long enough
    to place the certifier-signed statement in the immutable review package.
    """

    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.content) is not bytes
            or not self.content
            or len(self.content) > MAX_SIGNED_EVIDENCE_BYTES
        ):
            raise ValueError("verified retention evidence bytes are invalid")
        if (
            not isinstance(self.sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.sha256) is None
            or hashlib.sha256(self.content).hexdigest() != self.sha256
        ):
            raise ValueError("verified retention evidence SHA-256 is invalid")


def _joint_retention_trust_registry(
    encoded_override: str | None = None,
) -> Mapping[str, Any]:
    # Workers have a deliberately narrower settings model and database role.
    # Accept their already-validated deployment value explicitly instead of
    # constructing API Settings (and therefore requiring API credentials) in
    # the generation process.
    encoded = (
        get_settings().joint_retention_trust_registry_json
        if encoded_override is None
        else encoded_override
    )
    if not encoded:
        raise _retention_trust_error(
            "JOINT_RETENTION_TRUST_NOT_CONFIGURED",
            "No server-owned joint-retention trust registry is configured.",
            "Keep the design in review until approved certifier keys are deployed.",
        )
    try:
        registry = parse_joint_retention_registry_json(encoded)
    except JointRetentionRegistryError as exc:
        raise _retention_trust_error(
            "JOINT_RETENTION_TRUST_INVALID",
            "The server-owned joint-retention trust registry is malformed.",
            "Repair the deployment configuration; physical release remains blocked.",
        ) from exc
    return registry


def _verified_joint_retention_evidence_bytes(
    session: Session,
    organization_id: str,
    project_id: str,
    base_design_hash: str,
    evidence_id: str,
) -> tuple[ExternalEvidence, bytes]:
    rows = _resolve_external_evidence_rows(
        session,
        organization_id,
        project_id,
        base_design_hash,
        [evidence_id],
        expected_rule_id="CB-JOINT-001",
    )
    evidence = rows[0]
    initial_binding = _external_evidence_binding(evidence)
    _require_committed_domain_object(
        session,
        organization_id,
        project_id=evidence.project_id,
        object_key=evidence.object_key,
        sha256=evidence.sha256,
        size_bytes=evidence.size_bytes,
        media_type=evidence.content_type,
        owner_type="external_evidence",
        owner_id=evidence.id,
    )
    try:
        content = read_verified_stored_object(
            StoredObjectExpectation(
                object_key=evidence.object_key,
                sha256=evidence.sha256,
                size_bytes=evidence.size_bytes,
                content_type=evidence.content_type,
            ),
            max_bytes=MAX_SIGNED_EVIDENCE_BYTES,
        )
    except ArtifactIntegrityError as exc:
        raise _retention_trust_error(
            "JOINT_RETENTION_EVIDENCE_INTEGRITY_FAILED",
            "The signed retention statement no longer matches its immutable record.",
            "Upload and select a new signed statement; physical release remains blocked.",
        ) from exc
    except ArtifactStorageUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "JOINT_RETENTION_EVIDENCE_STORAGE_UNAVAILABLE",
                "message": "Retention evidence storage cannot currently be verified.",
                "solution": "Retry after storage is healthy; physical release remains blocked.",
            },
        ) from exc
    # Runtime roles intentionally have no UPDATE privilege on evidence rows;
    # PostgreSQL would reject SELECT FOR UPDATE.  Refresh after the object read
    # and compare every mutable binding instead.  A later revocation makes the
    # frozen revision stale at each subsequent lifecycle gate.
    current = session.scalar(
        select(ExternalEvidence)
        .where(
            ExternalEvidence.id == evidence_id,
            ExternalEvidence.organization_id == organization_id,
            ExternalEvidence.project_id == project_id,
        )
        .execution_options(populate_existing=True)
    )
    if current is None or _external_evidence_binding(current) != initial_binding:
        raise _external_evidence_snapshot_stale()
    return current, content


def _resolve_joint_retention_binding(
    session: Session,
    organization_id: str,
    project_id: str,
    base_spec: BookcaseDesignSpec,
    base_result: Any,
    evidence_id: str,
    *,
    trust_registry_json: str | None = None,
    production_mode: bool | None = None,
) -> tuple[JointRetentionContract, dict[str, Any], bytes]:
    if (
        base_spec.parameters.back_panel == BackPanelType.INSET_GROOVE
        and not captive_inset_back_topology_is_complete(
            base_result.parts,
            base_result.joints,
            base_result.assembly_graph,
        )
    ):
        raise _retention_trust_error(
            "BACK_PANEL_RETENTION_EVIDENCE_MISSING",
            "The inset back is not proven mechanically captive on four boundaries.",
            (
                "Keep this revision in design review until the canonical four-groove topology "
                "and multi-direction closing sequence are restored or independent back-panel "
                "retention evidence is implemented."
            ),
        )
    if base_spec.joint_retention is not None:
        raise _retention_trust_error(
            "JOINT_RETENTION_ALREADY_BOUND",
            "The canonical base design already contains a retention contract.",
            "Create a new unbound design revision before selecting new evidence.",
        )
    registry = _joint_retention_trust_registry(trust_registry_json)
    require_activation = (
        get_settings().app_env == "production" if production_mode is None else production_mode
    )
    try:
        assert_joint_retention_registry_activated(
            session,
            registry,
            production=require_activation,
        )
    except JointRetentionRegistryError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "JOINT_RETENTION_REGISTRY_NOT_ACTIVATED",
                "message": (
                    "The configured joint-retention trust registry does not match "
                    "the activated production high-water state."
                ),
                "solution": (
                    "Activate this exact monotonic registry with the operator CLI; "
                    "physical release remains blocked."
                ),
            },
        ) from exc
    evidence, evidence_bytes = _verified_joint_retention_evidence_bytes(
        session,
        organization_id,
        project_id,
        base_result.design_hash,
        evidence_id,
    )
    geometry_sha256 = dado_joint_geometry_fingerprint(base_result.parts, base_result.joints)
    try:
        contract = resolve_joint_retention_contract(
            trust_registry=registry,
            evidence_bytes=evidence_bytes,
            expected_application_class=(JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO),
            expected_joint_geometry_sha256=geometry_sha256,
            expected_engine_version=base_result.engine_version,
            expected_template_version=base_result.template_version,
            required_materials=((base_spec.material.material_id, base_spec.material.version),),
            required_thicknesses_um=(base_spec.parameters.actual_thickness_um,),
            required_loads_n=(
                (
                    JointRetentionLoadMode.SHEAR,
                    max(base_spec.parameters.shelf_load_n, 1),
                ),
                (
                    JointRetentionLoadMode.WITHDRAWAL,
                    base_spec.parameters.assumed_horizontal_force_n,
                ),
            ),
            minimum_safety_factor_permille=(base_spec.parameters.structural_safety_factor_permille),
        )
    except JointRetentionTrustError as exc:
        raise _retention_trust_error(
            "JOINT_RETENTION_EVIDENCE_REJECTED",
            str(exc),
            "Select current evidence signed by an approved certifier for this exact geometry.",
        ) from exc
    raw_statement = json.loads(evidence_bytes)
    if not isinstance(raw_statement, Mapping):  # guarded by the resolver; keep fail-closed
        raise _retention_trust_error(
            "JOINT_RETENTION_EVIDENCE_REJECTED",
            "The signed retention statement is malformed.",
            "Select a valid server-verifiable retention statement.",
        )
    signed_expiry_raw = raw_statement.get("expires_at")
    try:
        signed_expiry = datetime.fromisoformat(str(signed_expiry_raw)).astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise _retention_trust_error(
            "JOINT_RETENTION_EVIDENCE_REJECTED",
            "The signed retention expiry is malformed.",
            "Select a valid server-verifiable retention statement.",
        ) from exc
    row_expiry = _as_utc(evidence.expires_at) if evidence.expires_at is not None else None
    if (
        evidence.sha256 != contract.evidence_sha256
        or evidence.catalog_id != contract.system_id
        or evidence.catalog_version != contract.system_version
        or row_expiry != signed_expiry
    ):
        raise _retention_trust_error(
            "JOINT_RETENTION_EVIDENCE_METADATA_MISMATCH",
            "Stored evidence metadata does not match the authenticated statement.",
            "Upload a new statement using its signed system, version and expiry values.",
        )
    snapshot = {
        "schema_version": "custombuild.joint-retention-binding.v2",
        "application_class": (JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO.value),
        "storage_evidence_id": evidence.id,
        "storage_evidence_sha256": evidence.sha256,
        "base_design_hash": base_result.design_hash,
        "joint_geometry_sha256": geometry_sha256,
        "registry_sha256": hashlib.sha256(canonical_json_bytes(registry)).hexdigest(),
        "issuer_id": raw_statement.get("issuer_id"),
        "key_id": raw_statement.get("key_id"),
        "signed_evidence_id": contract.evidence_id,
        "signed_evidence_expires_at": raw_statement.get("expires_at"),
        "system_id": contract.system_id,
        "system_version": contract.system_version,
        "contract_sha256": canonical_hash(contract.model_dump(mode="json")),
    }
    return contract, snapshot, evidence_bytes


def _canonical_preview_with_optional_retention(
    session: Session,
    organization_id: str,
    project: Project | None,
    payload: BookcasePreviewInput,
    *,
    design_id: str,
    revision: int,
    evidence_id: str | None,
) -> tuple[BookcaseDesignSpec, Any, dict[str, Any], dict[str, Any] | None]:
    base_spec, base_result, presented = canonical_preview(
        payload.model_dump(exclude_none=True),
        design_id=design_id,
        revision=revision,
    )
    if project is not None:
        presented = {
            **presented,
            "retention_certification_request": _retention_certification_request(
                base_spec,
                base_result,
            ),
        }
    if evidence_id is None:
        return base_spec, base_result, presented, None
    if project is None:
        raise _retention_trust_error(
            "JOINT_RETENTION_PROJECT_REQUIRED",
            "Retention evidence can only be resolved inside an existing project.",
            "Save the project draft, then preview the signed retention selection again.",
        )
    contract, snapshot, _evidence_bytes = _resolve_joint_retention_binding(
        session,
        organization_id,
        project.id,
        base_spec,
        base_result,
        evidence_id,
    )
    spec, result, bound_presented = bind_joint_retention(base_spec, contract)
    return (
        spec,
        result,
        {
            **bound_presented,
            "retention_certification_request": presented["retention_certification_request"],
            "retention_trust": snapshot,
        },
        snapshot,
    )


def _retention_certification_request(
    spec: BookcaseDesignSpec,
    result: Any,
) -> dict[str, Any]:
    parameters = spec.parameters
    captive_back_proven = (
        parameters.back_panel == BackPanelType.INSET_GROOVE
        and captive_inset_back_topology_is_complete(
            result.parts,
            result.joints,
            result.assembly_graph,
        )
    )
    # This request covers only load-bearing carcass DADOs.  A surface-mounted
    # back remains a separate unresolved application and will keep CAM blocked,
    # but it must not prevent exact carcass evidence from crossing its own
    # narrower trust boundary.  Otherwise the truthful back-panel blocker is
    # unreachable and degrades to the higher-priority plain-DADO blocker.
    eligible = parameters.back_panel != BackPanelType.INSET_GROOVE or captive_back_proven
    required_materials = [
        {
            "material_id": spec.material.material_id,
            "material_version": spec.material.version,
            "actual_thickness_um": parameters.actual_thickness_um,
        }
    ]
    return {
        "schema_version": "custombuild.joint-retention-certification-request.v2",
        "signed_evidence_schema_version": SIGNED_EVIDENCE_SCHEMA_VERSION,
        "application_class": (JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO.value),
        "joint_geometry_fingerprint_schema": JOINT_GEOMETRY_FINGERPRINT_SCHEMA,
        "source_design_hash": result.design_hash,
        "joint_geometry_sha256": dado_joint_geometry_fingerprint(
            result.parts,
            result.joints,
        ),
        "engine_version": result.engine_version,
        "template_version": result.template_version,
        "eligible_for_current_binding": eligible,
        "blocking_issue": (None if eligible else ("back_panel_capture_not_proven")),
        "excluded_applications": (
            [
                {
                    "application_class": (
                        JointRetentionApplicationClass.CAPTIVE_INSET_BACK_GROOVE.value
                    ),
                    "joint_count": sum(
                        1
                        for joint in result.joints
                        if joint.retention_application_class
                        == JointRetentionApplicationClass.CAPTIVE_INSET_BACK_GROOVE
                    ),
                    "retention_basis": "canonical_four_boundary_geometric_capture",
                    "capture_proven": captive_back_proven,
                }
            ]
            if parameters.back_panel == BackPanelType.INSET_GROOVE
            else (
                [
                    {
                        "application_class": "surface_mounted_back",
                        "joint_count": sum(
                            1
                            for joint in result.joints
                            if joint.joint_type.value == "rabbet"
                            and any(
                                part.role.value == "back"
                                for member in joint.members
                                for part in result.parts
                                if part.part_id == member.part_id
                            )
                        ),
                        "retention_basis": "independent_authenticated_evidence_required",
                        "capture_proven": False,
                    }
                ]
                if parameters.back_panel == BackPanelType.SURFACE_MOUNTED
                else []
            )
        ),
        "required_materials": required_materials,
        "required_load_cases": [
            {
                "mode": "shear",
                "rated_design_load_n": max(parameters.shelf_load_n, 1),
            },
            {
                "mode": "withdrawal",
                "rated_design_load_n": parameters.assumed_horizontal_force_n,
            },
        ],
        "minimum_safety_factor_permille": parameters.structural_safety_factor_permille,
    }


def _require_current_retention_binding(
    session: Session,
    organization_id: str,
    version: DesignVersion,
    *,
    minimum_valid_until: datetime | None = None,
    trust_registry_json: str | None = None,
    production_mode: bool | None = None,
) -> VerifiedCurrentRetentionEvidence | None:
    try:
        bound_spec = BookcaseDesignSpec.model_validate(version.spec_json)
    except ValidationError as exc:
        raise _retention_trust_error(
            "JOINT_RETENTION_BINDING_STALE",
            "The frozen retention-bound design cannot be reconstructed.",
            "Create and review a new design revision.",
        ) from exc
    if bound_spec.joint_retention is None:
        return None
    frozen_snapshot = version.result_json.get("retention_trust")
    if not isinstance(frozen_snapshot, Mapping):
        raise _retention_trust_error(
            "JOINT_RETENTION_BINDING_STALE",
            "The frozen design has no authenticated retention trust snapshot.",
            "Create and review a new design revision with signed retention evidence.",
        )
    base_spec = bound_spec.model_copy(update={"joint_retention": None})
    try:
        base_result = build_bookcase(base_spec)
    except (TypeError, ValueError, ValidationError) as exc:
        raise _retention_trust_error(
            "JOINT_RETENTION_BINDING_STALE",
            "The retention base geometry can no longer be reconstructed.",
            "Create and review a new design revision.",
        ) from exc
    evidence_id = frozen_snapshot.get("storage_evidence_id")
    if not isinstance(evidence_id, str):
        raise _retention_trust_error(
            "JOINT_RETENTION_BINDING_STALE",
            "The frozen retention evidence identifier is malformed.",
            "Create and review a new design revision.",
        )
    contract, current_snapshot, evidence_bytes = _resolve_joint_retention_binding(
        session,
        organization_id,
        version.project_id,
        base_spec,
        base_result,
        evidence_id,
        trust_registry_json=trust_registry_json,
        production_mode=production_mode,
    )
    if minimum_valid_until is not None:
        try:
            signed_expiry = datetime.fromisoformat(
                str(current_snapshot["signed_evidence_expires_at"])
            ).astimezone(UTC)
        except (KeyError, TypeError, ValueError) as exc:
            raise _retention_trust_error(
                "JOINT_RETENTION_BINDING_STALE",
                "The frozen retention validity window is malformed.",
                "Create and review a new revision from current signed evidence.",
            ) from exc
        if not _retention_evidence_valid_beyond(
            signed_expiry,
            minimum_valid_until,
        ):
            raise _retention_trust_error(
                "JOINT_RETENTION_VALIDITY_TOO_SHORT",
                "Retention evidence may expire before the generation deadline.",
                "Select evidence valid beyond the complete generation and review window.",
            )
    if (
        contract != bound_spec.joint_retention
        or dict(frozen_snapshot) != current_snapshot
        or base_result.design_hash != frozen_snapshot.get("base_design_hash")
    ):
        raise _retention_trust_error(
            "JOINT_RETENTION_BINDING_STALE",
            "Retention evidence, registry or frozen contract changed after revision creation.",
            "Create and review a new revision from current signed evidence.",
        )
    try:
        bound_result = build_bookcase(bound_spec)
    except (TypeError, ValueError, ValidationError) as exc:
        raise _retention_trust_error(
            "JOINT_RETENTION_BINDING_STALE",
            "The frozen retention-bound design cannot be rebuilt.",
            "Create and review a new revision.",
        ) from exc
    if bound_result.design_hash != version.design_hash:
        raise _retention_trust_error(
            "JOINT_RETENTION_BINDING_STALE",
            "The frozen retention contract no longer matches the version design hash.",
            "Create and review a new revision.",
        )
    evidence_sha256 = current_snapshot.get("storage_evidence_sha256")
    if (
        not isinstance(evidence_sha256, str)
        or hashlib.sha256(evidence_bytes).hexdigest() != evidence_sha256
    ):
        raise _retention_trust_error(
            "JOINT_RETENTION_BINDING_STALE",
            "The verified retention bytes no longer match the frozen evidence digest.",
            "Create and review a new revision from current signed evidence.",
        )
    return VerifiedCurrentRetentionEvidence(
        content=evidence_bytes,
        sha256=evidence_sha256,
    )


def _retention_evidence_valid_beyond(
    signed_expiry: datetime,
    required_deadline: datetime,
) -> bool:
    """Require positive validity after the deadline, not equality at expiry."""

    return signed_expiry.astimezone(UTC) > required_deadline.astimezone(UTC)


def _require_current_artifacts(
    session: Session,
    organization_id: str,
    job: GenerationJob,
    *,
    populate_existing: bool = False,
) -> DesignVersion:
    version_query = select(DesignVersion).where(
        DesignVersion.id == job.design_version_id,
        DesignVersion.organization_id == organization_id,
    )
    if populate_existing:
        version_query = version_query.execution_options(populate_existing=True)
    version = session.scalar(version_query)
    if version is None:
        raise HTTPException(status_code=404, detail="Design version not found")
    project_query = select(Project).where(
        Project.id == version.project_id,
        Project.organization_id == organization_id,
    )
    if populate_existing:
        project_query = project_query.execution_options(populate_existing=True)
    project = session.scalar(project_query)
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


_ARTIFACT_DOWNLOAD_IDENTITIES: Mapping[str, tuple[str, str]] = {
    "production_bundle": ("design-review", "application/zip"),
    "manifest": ("design-review-manifest", "application/json"),
    "manufacturing_intent": ("manufacturing-intent", "application/json"),
    "supplier_handoff": ("cnc-shop-handoff", "application/json"),
    "dfm_report": ("dfm-report", "application/json"),
    "design_review_package_status": ("design-review-package-status", "application/json"),
    "stock_selection": ("stock-selection", "application/json"),
    "generation_plan": ("generation-plan", "application/json"),
    "operations": ("machine-neutral-operations", "application/json"),
    "validation_backplot": ("validation-backplot", "image/svg+xml"),
    "design_glb": ("design", "model/gltf-binary"),
    "design_fcstd": ("design", "application/vnd.freecad"),
    "cad_interchange_status": ("cad-interchange-status", "application/json"),
    "source_provenance": ("source-provenance", "application/json"),
    "workshop_readiness": ("workshop-readiness", "application/json"),
    "assembly_readiness": ("assembly-readiness", "application/json"),
}
_ARTIFACT_DOWNLOAD_EXTENSIONS: Mapping[str, str] = {
    "application/json": ".json",
    "application/vnd.freecad": ".FCStd",
    "application/zip": ".zip",
    "image/svg+xml": ".svg",
    "model/gltf-binary": ".glb",
}
_SETUP_SHEET_ARTIFACT_KIND_PATTERN = re.compile(r"setup_sheet_([0-9]{3})")


def _require_retention_bound_bundle_download_capability(
    principal: Principal,
    version: DesignVersion,
    artifact_kind: str,
) -> None:
    """Protect the embedded signed statement with its dedicated capability.

    Other job and release artifacts retain the ordinary READ policy.  Only a
    production ZIP whose frozen DesignSpec contains a retention contract gains
    the stricter boundary because that ZIP contains the exact signed evidence.
    """

    if artifact_kind != "production_bundle":
        return
    try:
        spec = BookcaseDesignSpec.model_validate(version.spec_json)
    except ValidationError as exc:
        raise HTTPException(
            status_code=409,
            detail="The frozen design cannot be reconstructed for bundle authorization",
        ) from exc
    if (
        spec.joint_retention is not None
        and Capability.JOINT_RETENTION_EVIDENCE_DOWNLOAD
        not in capabilities_for_role(principal.role)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(f"Capability {Capability.JOINT_RETENTION_EVIDENCE_DOWNLOAD.value} required"),
        )


def _artifact_filename(
    kind: str,
    revision: int,
    project_id: str,
    content_type: str,
) -> str:
    """Return a project-unique, header-safe name from server-owned identities."""

    if type(project_id) is not str or _CANONICAL_PROJECT_UUID_PATTERN.fullmatch(project_id) is None:
        raise ValueError("artifact project identity is invalid")
    if type(revision) is not int or revision < 1:
        raise ValueError("artifact revision is invalid")
    if type(kind) is not str or type(content_type) is not str:
        raise ValueError("artifact identity is invalid")

    identity = _ARTIFACT_DOWNLOAD_IDENTITIES.get(kind)
    if identity is None:
        setup_match = _SETUP_SHEET_ARTIFACT_KIND_PATTERN.fullmatch(kind)
        if setup_match is None:
            raise ValueError("artifact kind is invalid")
        stem = f"setup-sheet-{setup_match.group(1)}"
        expected_content_type = "image/svg+xml"
    else:
        stem, expected_content_type = identity
    if content_type != expected_content_type:
        raise ValueError("artifact content type does not match its kind")
    extension = _ARTIFACT_DOWNLOAD_EXTENSIONS.get(content_type)
    if extension is None:
        raise ValueError("artifact content type is invalid")

    filename = f"custombuild-project-{project_id}-{stem}-rev-{revision}{extension}"
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename) is None:
        raise ValueError("artifact filename is invalid")
    return filename


def _require_generation_result_context_binding(job: GenerationJob) -> None:
    """Bind a successful result to the exact persisted engine-context bytes.

    The persisted context is the server-owned fact frozen when the job was
    queued.  Recomputing only the current catalog context is insufficient: a
    later result-row edit could otherwise replace or remove the worker's hash
    while leaving every external artifact checksum intact.
    """

    engine_context = job.production_engine_context_json
    result = job.result_json
    try:
        if not isinstance(engine_context, Mapping) or not isinstance(result, Mapping):
            raise ProductionContextError("generation context binding is missing")
        expected = hashlib.sha256(canonical_json_bytes(engine_context)).hexdigest()
        actual = result.get("production_engine_context_hash")
        if actual != expected:
            raise ProductionContextError("generation result engine-context hash does not match")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProductionContextError("generation context binding is malformed") from exc


def _release_archive_error() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail="Released package failed immutable integrity verification",
    )


def _release_artifact_signature_subject(release_id: str, artifact_id: str) -> str:
    """Bind a signed historical URL to both immutable database identities."""

    if (
        _CANONICAL_UUID_PATTERN.fullmatch(release_id) is None
        or _CANONICAL_UUID_PATTERN.fullmatch(artifact_id) is None
    ):
        raise HTTPException(status_code=404, detail="Release artifact not found")
    return f"release:{release_id}:artifact:{artifact_id}"


def _release_artifact_filename(
    project_id: str,
    release_number: str,
    revision: int,
    kind: str,
    content_type: str,
) -> str:
    """Return a project-unique release name under the current artifact policy."""

    if re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,39}", release_number) is None:
        raise _release_archive_error()
    try:
        current_name = _artifact_filename(kind, revision, project_id, content_type)
    except ValueError as exc:
        raise _release_archive_error() from exc

    prefix = f"custombuild-project-{project_id}-"
    if not current_name.startswith(prefix):
        raise _release_archive_error()
    suffix = current_name.removeprefix(prefix)
    release_token = release_number
    filename = f"{prefix}release-{release_token}-{suffix}"
    if len(filename) > 128:
        release_token = (
            f"{release_number[:5]}-{hashlib.sha256(release_number.encode('ascii')).hexdigest()[:8]}"
        )
        filename = f"{prefix}release-{release_token}-{suffix}"
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename) is None:
        raise _release_archive_error()
    return filename


@dataclass(frozen=True, slots=True)
class _ReleaseArchive:
    release: Release
    version: DesignVersion
    job: GenerationJob
    artifacts: tuple[Artifact, ...]
    stored_objects: tuple[StoredObject, ...]
    binding: tuple[Any, ...]


def _frozen_release_inventory(
    job: GenerationJob,
    artifacts: tuple[Artifact, ...],
) -> tuple[dict[str, StoredObjectExpectation], list[dict[str, Any]]]:
    """Build the canonical, row-identity-bound inventory stored on a release."""

    expectations, invalid_result = _artifact_expectations(job)
    if (
        invalid_result
        or len(artifacts) != len(expectations)
        or {item.kind for item in artifacts} != set(expectations)
        or any(_CANONICAL_UUID_PATTERN.fullmatch(item.id) is None for item in artifacts)
    ):
        raise _release_archive_error()
    inventory: list[dict[str, Any]] = []
    for artifact in artifacts:
        expectation = expectations[artifact.kind]
        if (
            artifact.organization_id != job.organization_id
            or artifact.generation_job_id != job.id
            or artifact.object_key != expectation.object_key
            or artifact.sha256 != expectation.sha256
            or artifact.size_bytes != expectation.size_bytes
            or artifact.content_type != expectation.content_type
        ):
            raise _release_archive_error()
        inventory.append(
            {
                "artifact_id": artifact.id,
                "kind": artifact.kind,
                "object_key": artifact.object_key,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "content_type": artifact.content_type,
            }
        )
    return expectations, inventory


def _release_matches_frozen_job(
    release: Release,
    job: GenerationJob,
    inventory: list[dict[str, Any]],
) -> bool:
    """Compare live job/package state with the append-only release snapshot."""

    try:
        return (
            isinstance(job.result_json, Mapping)
            and release.generation_job_id == job.id
            and release.production_context_hash == job.production_context_hash
            and release.manifest_sha256 == job.result_json.get("manifest_sha256")
            and canonical_json_bytes(release.generation_result_json)
            == canonical_json_bytes(job.result_json)
            and canonical_json_bytes(release.artifact_inventory_json)
            == canonical_json_bytes(inventory)
        )
    except (AttributeError, TypeError, ValueError, RecursionError):
        return False


def _release_bundle_sha256(release: Release) -> str:
    """Return the exact outer ZIP identity frozen by one release.

    The digest is duplicated in the frozen generation result and its immutable
    artifact-row inventory. Requiring both copies to agree prevents the release
    response from presenting an unbound value as a shop handoff identity.
    """

    result = release.generation_result_json
    inventory = release.artifact_inventory_json
    if not isinstance(result, Mapping) or not isinstance(inventory, list):
        raise _release_archive_error()
    digest = result.get("bundle_sha256")
    bundle_rows = tuple(
        item
        for item in inventory
        if isinstance(item, Mapping) and item.get("kind") == "production_bundle"
    )
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"[a-f0-9]{64}", digest) is None
        or len(bundle_rows) != 1
        or bundle_rows[0].get("sha256") != digest
        or bundle_rows[0].get("content_type") != "application/zip"
    ):
        raise _release_archive_error()
    return digest


def _release_archive_binding(
    release: Release,
    version: DesignVersion,
    job: GenerationJob,
    artifacts: tuple[Artifact, ...],
    stored_objects: tuple[StoredObject, ...],
) -> tuple[Any, ...]:
    """Freeze every database field that authorizes historical bytes."""

    try:
        return (
            (
                release.id,
                release.organization_id,
                release.design_version_id,
                release.release_number,
                release.released_by,
                release.manifest_sha256,
                release.generation_job_id,
                release.production_context_hash,
                canonical_json_bytes(release.generation_result_json),
                canonical_json_bytes(release.artifact_inventory_json),
                release.created_at,
                release.updated_at,
            ),
            (
                version.id,
                version.organization_id,
                version.project_id,
                version.revision,
                version.status.value,
                version.design_hash,
                version.context_hash,
                canonical_json_bytes(version.spec_json),
                canonical_json_bytes(version.source_provenance_json),
                version.source_import_id,
                canonical_json_bytes(version.result_json),
                version.engine_version,
                version.template_version,
                version.template_id,
                version.template_capability_fingerprint,
                version.rule_version,
                version.created_by,
                version.immutable,
                version.created_at,
                version.updated_at,
            ),
            (
                job.id,
                job.organization_id,
                job.design_version_id,
                job.status.value,
                job.idempotency_key,
                job.production_context_hash,
                canonical_json_bytes(job.production_engine_context_json),
                canonical_json_bytes(job.request_json),
                canonical_json_bytes(job.result_json),
                job.attempts,
                job.lease_token,
                job.lease_expires_at,
                job.deadline_at,
                job.error,
                job.started_at,
                job.finished_at,
                job.created_at,
                job.updated_at,
            ),
            tuple(
                (
                    artifact.id,
                    artifact.organization_id,
                    artifact.generation_job_id,
                    artifact.kind,
                    artifact.object_key,
                    artifact.sha256,
                    artifact.size_bytes,
                    artifact.content_type,
                    artifact.created_at,
                    artifact.updated_at,
                )
                for artifact in artifacts
            ),
            tuple(
                (
                    stored.organization_id,
                    stored.object_key,
                    stored.project_id,
                    stored.sha256,
                    stored.size_bytes,
                    stored.media_type,
                    stored.owner_type,
                    stored.owner_id,
                    stored.idempotency_key,
                    stored.state.value,
                    stored.lease_token,
                    stored.lease_expires_at,
                    stored.claim_token,
                    stored.claim_expires_at,
                    stored.created_at,
                    stored.updated_at,
                )
                for stored in stored_objects
            ),
        )
    except (AttributeError, TypeError, ValueError, RecursionError) as exc:
        raise _release_archive_error() from exc


def _release_build_identity(job: GenerationJob) -> BuildIdentityValues:
    context = job.production_engine_context_json
    keys = (
        "app_version",
        "vcs_ref",
        "build_date",
        "source_url",
        "source_manifest_sha256",
        "dependency_lock_sha256",
    )
    if not isinstance(context, Mapping) or any(
        not isinstance(context.get(key), str) or not context.get(key) for key in keys
    ):
        raise _release_archive_error()
    return {
        "app_version": context["app_version"],
        "vcs_ref": context["vcs_ref"],
        "build_date": context["build_date"],
        "source_url": context["source_url"],
        "source_manifest_sha256": context["source_manifest_sha256"],
        "dependency_lock_sha256": context["dependency_lock_sha256"],
    }


def _resolve_release_archive(
    session: Session,
    organization_id: str,
    release_id: str,
    *,
    populate_existing: bool = False,
) -> _ReleaseArchive:
    """Resolve one release to exactly one successful, manifest-bound job."""

    if populate_existing:
        session.expire_all()
    query_options = {"populate_existing": True} if populate_existing else {}
    release = session.scalar(
        select(Release)
        .where(
            Release.id == release_id,
            Release.organization_id == organization_id,
        )
        .execution_options(**query_options)
    )
    if release is None:
        raise HTTPException(status_code=404, detail="Release not found")
    version = session.scalar(
        select(DesignVersion)
        .where(
            DesignVersion.id == release.design_version_id,
            DesignVersion.organization_id == organization_id,
        )
        .execution_options(**query_options)
        .with_for_update()
    )
    if (
        version is None
        or _CANONICAL_UUID_PATTERN.fullmatch(release.id) is None
        or _CANONICAL_UUID_PATTERN.fullmatch(version.id) is None
        or _CANONICAL_UUID_PATTERN.fullmatch(release.generation_job_id) is None
        or version.immutable is not True
        or version.status
        not in {DesignStatus.released, DesignStatus.superseded, DesignStatus.archived}
        or re.fullmatch(r"[a-f0-9]{64}", release.manifest_sha256) is None
        or re.fullmatch(r"[a-f0-9]{64}", release.production_context_hash) is None
        or re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,39}", release.release_number) is None
        or not isinstance(release.generation_result_json, Mapping)
        or not isinstance(release.artifact_inventory_json, list)
        or not release.artifact_inventory_json
    ):
        raise _release_archive_error()

    jobs = tuple(
        session.scalars(
            select(GenerationJob)
            .where(
                GenerationJob.organization_id == organization_id,
                GenerationJob.design_version_id == version.id,
            )
            .order_by(GenerationJob.id)
            .execution_options(**query_options)
            .with_for_update()
        )
    )
    bound_jobs = tuple(job for job in jobs if job.id == release.generation_job_id)
    if len(bound_jobs) != 1:
        raise _release_archive_error()
    job = bound_jobs[0]
    matching_jobs = tuple(
        job
        for job in jobs
        if job.status == JobStatus.succeeded
        and isinstance(job.result_json, Mapping)
        and job.result_json.get("manifest_sha256") == release.manifest_sha256
    )
    if (
        job.status != JobStatus.succeeded
        or len(matching_jobs) != 1
        or matching_jobs[0].id != job.id
        or not _release_matches_frozen_job(
            release,
            job,
            release.artifact_inventory_json,
        )
    ):
        raise _release_archive_error()
    if _CANONICAL_UUID_PATTERN.fullmatch(job.id) is None:
        raise _release_archive_error()

    artifacts = tuple(
        session.scalars(
            select(Artifact)
            .where(
                Artifact.organization_id == organization_id,
                Artifact.generation_job_id == job.id,
            )
            .order_by(Artifact.kind, Artifact.id)
            .execution_options(**query_options)
        )
    )
    expectations, inventory = _frozen_release_inventory(job, artifacts)
    manifest_expectation = expectations.get("manifest")
    if (
        manifest_expectation is None
        or manifest_expectation.sha256 != release.manifest_sha256
        or not _release_matches_frozen_job(release, job, inventory)
    ):
        raise _release_archive_error()

    stored_objects: list[StoredObject] = []
    for artifact in artifacts:
        expectation = expectations[artifact.kind]
        if (
            artifact.object_key != expectation.object_key
            or artifact.sha256 != expectation.sha256
            or artifact.size_bytes != expectation.size_bytes
            or artifact.content_type != expectation.content_type
        ):
            raise _release_archive_error()
        stored = session.scalar(
            select(StoredObject)
            .where(
                StoredObject.organization_id == organization_id,
                StoredObject.object_key == artifact.object_key,
            )
            .execution_options(**query_options)
        )
        if stored is None:
            raise _release_archive_error()
        try:
            require_committed_storage_binding(
                session,
                organization_id,
                project_id=version.project_id,
                object_key=artifact.object_key,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                media_type=artifact.content_type,
                owner_type="generation_job",
                owner_id=job.id,
            )
        except (StorageClaimConflict, StorageQuotaInvariantError) as exc:
            raise _release_archive_error() from exc
        if stored.idempotency_key != (f"generation:{job.id}:{artifact.kind}:{artifact.id}"):
            raise _release_archive_error()
        stored_objects.append(stored)

    stored_tuple = tuple(stored_objects)
    return _ReleaseArchive(
        release=release,
        version=version,
        job=job,
        artifacts=artifacts,
        stored_objects=stored_tuple,
        binding=_release_archive_binding(
            release,
            version,
            job,
            artifacts,
            stored_tuple,
        ),
    )


def _verify_release_archive_owned(
    session: Session,
    organization_id: str,
    archive: _ReleaseArchive,
    *,
    verified_download_kind: str | None = None,
    verified_download: VerifiedStoredObject | None = None,
) -> _ReleaseArchive:
    """Verify frozen package semantics without consulting mutable approvals."""

    _require_current_retention_binding(session, organization_id, archive.version)
    try:
        missing, invalid, readiness_valid = _review_evidence_issues_owned(
            session,
            organization_id,
            archive.job,
            stream_hash=False,
            require_cam=True,
            bind_review_documents=True,
            verified_download_kind=verified_download_kind,
            verified_download=verified_download,
            build_identity=_release_build_identity(archive.job),
        )
    except ArtifactStorageUnavailableError as exc:
        raise _storage_unavailable() from exc
    if missing or invalid or not readiness_valid:
        raise _release_archive_error()
    current = _resolve_release_archive(
        session,
        organization_id,
        archive.release.id,
        populate_existing=True,
    )
    if current.binding != archive.binding:
        raise _release_archive_error()
    return current


def _prepare_release_artifact_download(
    session: Session,
    organization_id: str,
    archive: _ReleaseArchive,
    artifact: Artifact,
) -> tuple[_ReleaseArchive, VerifiedStoredObject]:
    """Spool and semantically verify a release target under one hard deadline."""

    expectations, invalid_result = _artifact_expectations(archive.job)
    expectation = expectations.get(artifact.kind)
    if invalid_result or expectation is None:
        raise _release_archive_error()
    verified: VerifiedStoredObject | None = None
    try:
        with storage_read_deadline(_REVIEW_STORAGE_TOTAL_SECONDS):
            try:
                verified = open_verified_stored_object(
                    expectation,
                    max_bytes=artifact_size_limit(artifact.kind),
                )
            except ArtifactIntegrityError as exc:
                raise _release_archive_error() from exc
            except ArtifactStorageUnavailableError as exc:
                raise _storage_unavailable() from exc
            current = _verify_release_archive_owned(
                session,
                organization_id,
                archive,
                verified_download_kind=artifact.kind,
                verified_download=verified,
            )
        return current, verified
    except BaseException:
        if verified is not None:
            verified.close()
        raise


def _require_current_generation_context(
    job: GenerationJob,
    version: DesignVersion,
) -> None:
    """Reject a job frozen against any superseded production implementation."""

    try:
        _require_generation_result_context_binding(job)
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
    *,
    verified_evidence_bindings: dict[str, _ExternalEvidenceBinding] | None = None,
) -> Approval:
    """Require an approval that covers exactly the version's current server warnings."""

    if approval is None:
        raise HTTPException(status_code=409, detail="A current design approval is required")
    approval_binding = _approval_binding(approval)
    evidence_bindings = verified_evidence_bindings if verified_evidence_bindings is not None else {}
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
            verified_bindings=evidence_bindings,
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
    # Earlier evidence can be revoked while a later override is being hashed.
    # Re-resolve every verified row, then the approval itself, without more I/O.
    _require_external_evidence_bindings_current(session, evidence_bindings)
    return _require_approval_binding_current(
        session,
        approval_binding,
        approval_id=approval.id,
        organization_id=organization_id,
        design_version_id=version.id,
        approval_type="design",
        detail={
            "code": "DESIGN_APPROVAL_SNAPSHOT_STALE",
            "message": "The design approval changed while its evidence was being verified.",
            "solution": "Review the current approval and generate the package again.",
        },
    )


def _require_four_eyes_approval_separation(
    design_approver_id: str,
    cam_approver_id: str,
) -> None:
    """Enforce distinct design and CAM reviewers when the deployment requires it."""

    if get_settings().production_four_eyes_required and design_approver_id == cam_approver_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": FOUR_EYES_APPROVER_SEPARATION_REQUIRED_CODE,
                "message": ("Design and CAM approval must be completed by different users."),
                "solution": (
                    "Ask a different authorized reviewer to complete CAM approval "
                    "for the current design approval."
                ),
            },
        )


def _require_current_bound_job_evidence(
    session: Session,
    organization_id: str,
    job: GenerationJob,
    version: DesignVersion,
    *,
    trust_registry_json: str | None = None,
    production_mode: bool | None = None,
) -> None:
    """Re-resolve mutable evidence rows before a frozen job may be trusted.

    Job and manifest snapshots prove what was reviewed at generation time.  They
    do not prove that an evidence row still exists, remains unrevoked/unexpired,
    or still points at the same immutable object.  Every artifact, CAM and
    release gate therefore repeats the server-owned evidence resolution.
    """

    request = job.request_json
    if not isinstance(request, Mapping):
        raise HTTPException(status_code=409, detail="Generation evidence binding is malformed")
    _require_current_retention_binding(
        session,
        organization_id,
        version,
        trust_registry_json=trust_registry_json,
        production_mode=production_mode,
    )
    verified_evidence_bindings: dict[str, _ExternalEvidenceBinding] = {}
    design_approval = session.scalar(
        select(Approval)
        .where(
            Approval.organization_id == organization_id,
            Approval.design_version_id == version.id,
            Approval.approval_type == "design",
        )
        .execution_options(populate_existing=True)
    )
    design_approval = _require_current_design_approval(
        session,
        organization_id,
        design_approval,
        version,
        verified_evidence_bindings=verified_evidence_bindings,
    )
    design_approval_binding = _approval_binding(design_approval)
    if request.get("approved_design_review") != _design_approval_snapshot(design_approval):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DESIGN_APPROVAL_SNAPSHOT_STALE",
                "message": "The generated package is not bound to the current design approval.",
                "solution": "Generate and review a new package from the current approval.",
            },
        )

    snapshots = request.get("external_evidence")
    if not isinstance(snapshots, list) or any(not isinstance(item, Mapping) for item in snapshots):
        raise HTTPException(status_code=409, detail="Generation evidence snapshot is malformed")
    evidence_ids = [
        str(snapshot.get("evidence_id")) for snapshot in snapshots if snapshot.get("evidence_id")
    ]
    if len(evidence_ids) != len(snapshots):
        raise HTTPException(status_code=409, detail="Generation evidence snapshot is malformed")
    current = _verified_external_evidence(
        session,
        organization_id,
        version.project_id,
        version.design_hash,
        evidence_ids,
        verified_bindings=verified_evidence_bindings,
    )
    if current != snapshots:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXTERNAL_EVIDENCE_SNAPSHOT_STALE",
                "message": "The generated package evidence no longer matches its server record.",
                "solution": "Generate and review a new package with current immutable evidence.",
            },
        )

    # The job-evidence object reads happen after the approval check.  Make the
    # final operation a DB-only re-resolution of both mutable authorities so a
    # cached identity-map row cannot authorize a link, CAM action or release.
    _require_external_evidence_bindings_current(session, verified_evidence_bindings)
    design_approval = _require_approval_binding_current(
        session,
        design_approval_binding,
        approval_id=design_approval.id,
        organization_id=organization_id,
        design_version_id=version.id,
        approval_type="design",
        detail={
            "code": "DESIGN_APPROVAL_SNAPSHOT_STALE",
            "message": "The generated package approval changed during evidence verification.",
            "solution": "Generate and review a new package from the current approval.",
        },
    )
    if request.get("approved_design_review") != _design_approval_snapshot(design_approval):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DESIGN_APPROVAL_SNAPSHOT_STALE",
                "message": "The generated package is not bound to the current design approval.",
                "solution": "Generate and review a new package from the current approval.",
            },
        )


def _design_approval_snapshot(approval: Approval) -> dict[str, Any]:
    """Canonical review identity bound into every generation-context hash."""

    return {
        "approval_id": approval.id,
        "approved_by": approval.approved_by,
        "reason": approval.reason,
        "warning_overrides": approval.overrides_json,
    }


def _frozen_dado_retention_is_unresolved(version: DesignVersion) -> bool | None:
    """Rebuild the frozen design and recheck its exact DADO retention contract."""

    if not isinstance(version.spec_json, Mapping):
        return None
    try:
        design = build_bookcase(BookcaseDesignSpec.model_validate(version.spec_json))
    except (TypeError, ValueError, ValidationError):
        return None
    if design.design_hash != version.design_hash:
        return None
    return dado_retention_evidence_missing(design)


def _frozen_back_panel_retention_is_unresolved(version: DesignVersion) -> bool | None:
    """Rebuild the frozen design and recheck its exact back-panel retention class."""

    if not isinstance(version.spec_json, Mapping):
        return None
    try:
        design = build_bookcase(BookcaseDesignSpec.model_validate(version.spec_json))
    except (TypeError, ValueError, ValidationError):
        return None
    if design.design_hash != version.design_hash:
        return None
    return back_panel_retention_evidence_missing(design)


def _require_resolved_dado_retention(version: DesignVersion) -> None:
    unresolved = _frozen_dado_retention_is_unresolved(version)
    if unresolved is None:
        raise HTTPException(
            status_code=409,
            detail="The frozen joint-retention contract cannot be reconstructed",
        )
    if unresolved:
        raise HTTPException(
            status_code=409,
            detail={
                "code": DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
                "message": (
                    "A plain DADO proves geometry and local bearing, not permanent retention."
                ),
                "solution": (
                    "Select current Ed25519-signed evidence from a server-approved certifier "
                    "for this exact geometry, material and compiler version, then create and "
                    "review a new revision."
                ),
            },
        )


def _frozen_grain_contract(
    version: DesignVersion,
    job: GenerationJob | None = None,
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
        ).model_dump(mode="json", exclude_none=True)
        if job is not None:
            if not isinstance(job.request_json, Mapping):
                return None
            assert_job_matches_frozen_revision_context(
                production_context,
                job.request_json,
            )
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
    except (ProductionContextError, TypeError, ValueError, ValidationError, RecursionError):
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


def _frozen_stock_selection_snapshot(
    version: DesignVersion,
    job: GenerationJob | None = None,
) -> bytes | None:
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
        ).model_dump(mode="json", exclude_none=True)
        if job is not None:
            if not isinstance(job.request_json, Mapping):
                return None
            assert_job_matches_frozen_revision_context(
                production_context,
                job.request_json,
            )
        return stock_selection_snapshot_for_design(design, production_context)
    except (ProductionContextError, TypeError, ValueError, ValidationError, RecursionError):
        return None


def _frozen_generation_plan_snapshot(
    job: GenerationJob,
    version: DesignVersion,
    *,
    build_identity: BuildIdentityValues | None = None,
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
        ).model_dump(mode="json", exclude_none=True)
        assert_job_matches_frozen_revision_context(
            production_context,
            job.request_json,
        )
        machine_profile_id = job.request_json.get("machine_profile_id")
        postprocessor_id = job.request_json.get("postprocessor_id")
        validation_program_requested = job.request_json.get("include_validation_program")
        if (
            not isinstance(machine_profile_id, str)
            or not isinstance(postprocessor_id, str)
            or type(validation_program_requested) is not bool
        ):
            return None
        identity = get_settings().build_identity if build_identity is None else build_identity
        resolved = resolve_production_components(
            machine_profile_id=machine_profile_id,
            postprocessor_id=postprocessor_id,
            app_version=identity["app_version"],
            vcs_ref=identity["vcs_ref"],
            build_date=identity["build_date"],
            source_url=identity["source_url"],
            source_manifest_sha256=identity["source_manifest_sha256"],
            dependency_lock_sha256=identity["dependency_lock_sha256"],
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


def _frozen_stock_missing_issues(
    version: DesignVersion,
    job: GenerationJob | None = None,
) -> tuple[Any, ...] | None:
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
        ).model_dump(mode="json", exclude_none=True)
        if job is not None:
            if not isinstance(job.request_json, Mapping):
                return None
            assert_job_matches_frozen_revision_context(
                production_context,
                job.request_json,
            )
        return stock_missing_issues_for_design(design, production_context)
    except (ProductionContextError, TypeError, ValueError, ValidationError, RecursionError):
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
            DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
            BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
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
    domain_template_version = version_result.get("template_version")
    if (
        not isinstance(capability, Mapping)
        or not isinstance(design_spec, Mapping)
        or not isinstance(domain_template_version, str)
        or not domain_template_version
        or version.template_version != f"bookcase@{domain_template_version}"
    ):
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
            "template_version": domain_template_version,
            "domain_template_version": domain_template_version,
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
    *,
    build_identity: BuildIdentityValues | None = None,
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
        expected_stock_selection = _frozen_stock_selection_snapshot(version, job)
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
        expected_generation_plan = _frozen_generation_plan_snapshot(
            job,
            version,
            build_identity=build_identity,
        )
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
        frozen_grain_contract = _frozen_grain_contract(version, job)
        if frozen_grain_contract is None:
            raise ValueError("frozen design grain projection is invalid")
        expected_grain_issues, expected_missing_stock_grain_issues, _ = frozen_grain_contract
        expected_stock_missing_issues = _frozen_stock_missing_issues(version, job)
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
            raise ValueError("schema-v5 production package status is mandatory")
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
        dado_retention_blocked = blocker_codes == (DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,)
        back_retention_blocked = blocker_codes == (
            BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
        )
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
        frozen_dado_retention = _frozen_dado_retention_is_unresolved(version)
        frozen_back_retention = _frozen_back_panel_retention_is_unresolved(version)
        if frozen_dado_retention is None or frozen_back_retention is None:
            raise ValueError("frozen retention applications cannot be reconstructed")
        if dado_retention_blocked and frozen_dado_retention is not True:
            raise ValueError("DADO retention blocker does not match the frozen design")
        if back_retention_blocked and (
            frozen_dado_retention is not False or frozen_back_retention is not True
        ):
            raise ValueError("back-panel retention blocker does not match the frozen design")
        if normalized_package_status.cam_status is CAMStageStatus.VALIDATION_GENERATED and (
            frozen_dado_retention or frozen_back_retention
        ):
            raise ValueError("generated CAM status contradicts frozen joint retention")
        if blocker_codes == ("TWO_SIDED_REGISTRATION_MISSING",) and (
            frozen_dado_retention or frozen_back_retention
        ):
            raise ValueError("registration blocker masks unresolved frozen joint retention")
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
    verified_download_kind: str | None = None,
    verified_download: VerifiedStoredObject | None = None,
    build_identity: BuildIdentityValues | None = None,
) -> tuple[list[str], list[str], bool]:
    """Serialize expensive evidence verification per tenant and always release."""

    with _artifact_operation_scope(organization_id):
        return _review_evidence_issues_owned(
            session,
            organization_id,
            job,
            stream_hash=stream_hash,
            require_cam=require_cam,
            bind_review_documents=bind_review_documents,
            verified_download_kind=verified_download_kind,
            verified_download=verified_download,
            build_identity=build_identity,
        )


def _review_evidence_issues_owned(
    session: Session,
    organization_id: str,
    job: GenerationJob,
    *,
    stream_hash: bool = False,
    require_cam: bool = True,
    bind_review_documents: bool = False,
    verified_download_kind: str | None = None,
    verified_download: VerifiedStoredObject | None = None,
    build_identity: BuildIdentityValues | None = None,
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
    if version is not None:
        for artifact in artifacts:
            try:
                require_committed_storage_binding(
                    session,
                    organization_id,
                    project_id=version.project_id,
                    object_key=artifact.object_key,
                    sha256=artifact.sha256,
                    size_bytes=artifact.size_bytes,
                    media_type=artifact.content_type,
                    owner_type="generation_job",
                    owner_id=job.id,
                )
            except (StorageClaimConflict, StorageQuotaInvariantError):
                invalid.append(artifact.kind)
    if (verified_download_kind is None) != (verified_download is None):
        invalid.append("verified_download")
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
        "manufacturing_intent",
        "supplier_handoff",
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
    consumed_verified_download = False
    retain_review_documents = stream_hash or bind_review_documents
    retained_bytes = sum(
        expectation.size_bytes
        for kind, expectation in expectations.items()
        if retain_review_documents
        and kind in _REVIEW_DOCUMENT_MAX_BYTES
        # A target document is already covered by the open verified token's
        # reservation until the response closes.
        and kind != verified_download_kind
    )
    with (
        storage_read_deadline(_REVIEW_STORAGE_TOTAL_SECONDS),
        reserve_transient_bytes(retained_bytes),
    ):
        for kind, expectation in expectations.items():
            review_artifact = by_kind.get(kind)
            if review_artifact is None:
                continue
            if (
                review_artifact.object_key != expectation.object_key
                or review_artifact.sha256 != expectation.sha256
                or review_artifact.size_bytes != expectation.size_bytes
                or review_artifact.content_type != expectation.content_type
            ):
                invalid.append(kind)
                continue
            try:
                review_limit = _REVIEW_DOCUMENT_MAX_BYTES.get(kind)
                if kind == verified_download_kind and verified_download is not None:
                    consumed_verified_download = True
                    if (
                        verified_download.sha256 != expectation.sha256
                        or verified_download.size_bytes != expectation.size_bytes
                        or verified_download.content_type != expectation.content_type
                    ):
                        invalid.append(kind)
                    elif retain_review_documents and review_limit is not None:
                        verified_documents[kind] = verified_download.validation_bytes(
                            max_bytes=review_limit
                        )
                elif retain_review_documents and review_limit is not None:
                    verified_documents[kind] = read_verified_stored_object(
                        expectation,
                        max_bytes=review_limit,
                    )
                else:
                    verify_stored_object(expectation, stream_hash=stream_hash)
            except ArtifactIntegrityError:
                invalid.append(kind)
        if verified_download is not None and not consumed_verified_download:
            invalid.append("verified_download")
        if retain_review_documents and version is not None:
            invalid.extend(
                _review_document_binding_issues(
                    job,
                    version,
                    result_json,
                    expectations,
                    verified_documents,
                    build_identity=build_identity,
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
    object_keys: set[str] = set()
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
            or object_key in object_keys
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or not valid_artifact_size(kind_value, size_bytes)
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
        assert type(size_bytes) is int
        normalized_kinds.add(normalized_kind)
        object_keys.add(object_key)
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
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE_ARTIFACTS:
        invalid.append("evidence_artifacts")
    else:
        evidence_total_bytes = 0
        for item in evidence:
            if not isinstance(item, dict):
                invalid.append("evidence_artifacts")
                continue
            raw_size = item.get("size_bytes")
            if type(raw_size) is int and raw_size > 0:
                evidence_total_bytes += raw_size
                if evidence_total_bytes > MAX_EVIDENCE_TOTAL_BYTES:
                    invalid.append("evidence_artifacts")
                    break
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
    build_identity: BuildIdentityValues | None = None,
    trust_registry_json: str | None = None,
    production_mode: bool | None = None,
) -> None:
    """Require persisted, checksum-addressed evidence for the exact successful job."""

    with _artifact_operation_scope(organization_id):
        try:
            # Own the shared tenant/global slot until artifact verification,
            # external-evidence I/O and their final DB refresh all complete.
            missing, invalid, readiness_valid = _review_evidence_issues_owned(
                session,
                organization_id,
                job,
                stream_hash=stream_hash,
                require_cam=require_cam,
                bind_review_documents=bind_review_documents,
                build_identity=build_identity,
            )
        except ArtifactStorageUnavailableError as exc:
            raise _storage_unavailable() from exc
        if missing or invalid or not readiness_valid:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Production evidence failed integrity verification; regenerate the package"
                ),
            )
        version = session.scalar(
            select(DesignVersion).where(
                DesignVersion.organization_id == organization_id,
                DesignVersion.id == job.design_version_id,
            )
        )
        if version is None:
            raise HTTPException(status_code=409, detail="Generation design version is missing")
        _require_current_bound_job_evidence(
            session,
            organization_id,
            job,
            version,
            trust_registry_json=trust_registry_json,
            production_mode=production_mode,
        )


def _prepare_review_artifact_download(
    session: Session,
    organization_id: str,
    job: GenerationJob,
    version: DesignVersion,
    artifact_kind: str,
) -> VerifiedStoredObject:
    """Verify the exact target once and fail closed on every sibling artifact."""

    with storage_read_deadline(_REVIEW_STORAGE_TOTAL_SECONDS):
        return _prepare_review_artifact_download_within_deadline(
            session,
            organization_id,
            job,
            version,
            artifact_kind,
        )


def _prepare_review_artifact_download_within_deadline(
    session: Session,
    organization_id: str,
    job: GenerationJob,
    version: DesignVersion,
    artifact_kind: str,
) -> VerifiedStoredObject:
    """Run one complete download review within its inherited absolute deadline."""

    expectations, result_invalid = _artifact_expectations(job)
    expectation = expectations.get(artifact_kind)
    if result_invalid or expectation is None:
        raise HTTPException(
            status_code=409,
            detail="Production evidence failed integrity verification; regenerate the package",
        )
    artifact_row = session.scalar(
        select(Artifact).where(
            Artifact.organization_id == organization_id,
            Artifact.generation_job_id == job.id,
            Artifact.kind == artifact_kind,
        )
    )
    if artifact_row is None:
        raise HTTPException(
            status_code=409,
            detail="Production evidence failed integrity verification; regenerate the package",
        )
    try:
        require_committed_storage_binding(
            session,
            organization_id,
            project_id=version.project_id,
            object_key=artifact_row.object_key,
            sha256=artifact_row.sha256,
            size_bytes=artifact_row.size_bytes,
            media_type=artifact_row.content_type,
            owner_type="generation_job",
            owner_id=job.id,
        )
    except (StorageClaimConflict, StorageQuotaInvariantError) as exc:
        raise HTTPException(
            status_code=409,
            detail="Production evidence failed integrity verification; regenerate the package",
        ) from exc
    try:
        verified = open_verified_stored_object(
            expectation,
            max_bytes=artifact_size_limit(artifact_kind),
        )
    except ArtifactIntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="Production evidence failed integrity verification; regenerate the package",
        ) from exc
    except ArtifactStorageUnavailableError as exc:
        raise _storage_unavailable() from exc

    try:
        try:
            # The enclosing authenticated download already owns the shared
            # tenant/global artifact-operation slot.  Re-entering the public
            # limiter here would double-count one request and self-reject.
            missing, invalid, readiness_valid = _review_evidence_issues_owned(
                session,
                organization_id,
                job,
                stream_hash=False,
                require_cam=False,
                bind_review_documents=True,
                verified_download_kind=artifact_kind,
                verified_download=verified,
            )
        except ArtifactStorageUnavailableError as exc:
            raise _storage_unavailable() from exc
        if missing or invalid or not readiness_valid:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Production evidence failed integrity verification; regenerate the package"
                ),
            )
        _require_current_bound_job_evidence(
            session,
            organization_id,
            job,
            version,
        )
        # Object and bound-evidence verification can involve bounded network and
        # spool I/O.  A concurrent revision may supersede/archive this version
        # while either operation is in flight, so refresh mutable state only
        # after all of that I/O and immediately before response ownership.
        fresh_version = _require_current_artifacts(
            session,
            organization_id,
            job,
            populate_existing=True,
        )
        if fresh_version.id != version.id:
            raise HTTPException(
                status_code=409,
                detail="Production artifacts are stale after a design change",
            )
    except BaseException:
        try:
            verified.close()
        except ArtifactStorageUnavailableError as exc:
            raise _storage_unavailable() from exc
        raise
    return verified


class _VerifiedArtifactStreamingResponse(StreamingResponse):
    """Own and close a verified spool across every ASGI exit path."""

    def __init__(
        self,
        verified: VerifiedStoredObject,
        *,
        headers: Mapping[str, str],
        tenant_lease: _TenantDownloadLease | None,
        stream_timeout_seconds: float,
    ) -> None:
        if stream_timeout_seconds <= 0:
            raise ValueError("artifact stream timeout must be positive")
        self._verified = verified
        self._tenant_lease = tenant_lease
        self._stream_timeout_seconds = stream_timeout_seconds
        super().__init__(
            verified.iter_bytes(),
            status_code=200,
            media_type=None,
            headers=headers,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with asyncio.timeout(self._stream_timeout_seconds):
                await super().__call__(scope, receive, send)
        finally:
            # Starlette does not run a BackgroundTask when ASGI send() fails,
            # including before response.start.  Close the storage-owned token
            # around the complete response call so disconnects cannot retain a
            # potentially large verified temporary file.
            try:
                self._verified.close()
            finally:
                if self._tenant_lease is not None:
                    self._tenant_lease.close()


class _TenantDownloadLease:
    """Release one tenant's single in-flight artifact transfer exactly once."""

    __slots__ = ("_limiter", "_lock", "_organization_id")

    def __init__(self, limiter: _TenantDownloadLimiter, organization_id: str) -> None:
        self._limiter: _TenantDownloadLimiter | None = limiter
        self._organization_id = organization_id
        self._lock = Lock()

    def close(self) -> None:
        with self._lock:
            limiter = self._limiter
            self._limiter = None
        if limiter is not None:
            limiter._release(self._organization_id)


class _TenantDownloadLimiter:
    """Bound artifact work globally and to one in-flight operation per tenant."""

    __slots__ = ("_active", "_lock", "_max_active")

    def __init__(self, max_active: int = 8) -> None:
        if type(max_active) is not int or max_active <= 0:
            raise ValueError("artifact operation capacity must be a positive integer")
        self._active: set[str] = set()
        self._lock = Lock()
        self._max_active = max_active

    def acquire(self, organization_id: str) -> _TenantDownloadLease:
        with self._lock:
            if organization_id in self._active:
                raise HTTPException(
                    status_code=429,
                    detail="Another verified artifact operation is already active for this tenant",
                    headers={"Retry-After": "5"},
                )
            if len(self._active) >= self._max_active:
                raise HTTPException(
                    status_code=503,
                    detail="Verified artifact operation capacity is temporarily exhausted",
                    headers={"Retry-After": "5"},
                )
            self._active.add(organization_id)
        return _TenantDownloadLease(self, organization_id)

    def _release(self, organization_id: str) -> None:
        with self._lock:
            self._active.discard(organization_id)


_ARTIFACT_OPERATION_LIMITER = _TenantDownloadLimiter()
_TENANT_DOWNLOAD_LIMITER = _ARTIFACT_OPERATION_LIMITER
_TENANT_REVIEW_LIMITER = _ARTIFACT_OPERATION_LIMITER
_ACTIVE_ARTIFACT_OPERATION_ORGANIZATION: ContextVar[str | None] = ContextVar(
    "active_artifact_operation_organization",
    default=None,
)
_IN_MEMORY_ARTIFACT_OPERATION_STORE = InMemoryArtifactOperationStore()
_ARTIFACT_OPERATION_TENANT_LOCK_NAMESPACE = 1_129_659_476
_ARTIFACT_OPERATION_GLOBAL_LOCK_NAMESPACE = 1_129_659_463
_ARTIFACT_OPERATION_GLOBAL_LOCK_COUNT = 8


@lru_cache(maxsize=1)
def _artifact_operation_lease_manager() -> ArtifactOperationLeaseManager:
    """Build one process-local adapter around the production Redis lease store."""

    settings = get_settings()
    store = (
        RedisArtifactOperationStore(settings.redis_url)
        if settings.app_env == "production"
        else _IN_MEMORY_ARTIFACT_OPERATION_STORE
    )
    return ArtifactOperationLeaseManager(store)


def _artifact_operation_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ArtifactOperationBusyError):
        return HTTPException(
            status_code=429,
            detail="Another verified artifact operation is already active for this tenant",
            headers={"Retry-After": "5"},
        )
    if isinstance(exc, ArtifactOperationCapacityError):
        return HTTPException(
            status_code=503,
            detail="Verified artifact operation capacity is temporarily exhausted",
            headers={"Retry-After": "5"},
        )
    return HTTPException(
        status_code=503,
        detail="Verified artifact operation coordination is temporarily unavailable",
        headers={"Retry-After": "5"},
    )


def _acquire_database_artifact_operation_locks(
    session: Session,
    organization_id: str,
) -> None:
    """Fence one tenant and one of eight global slots in the request transaction."""

    if session.get_bind().dialect.name != "postgresql":
        return
    tenant_key = int.from_bytes(
        hashlib.sha256(organization_id.encode("utf-8")).digest()[:4],
        byteorder="big",
        signed=True,
    )
    try:
        tenant_acquired = session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:namespace, :tenant_key)"),
            {
                "namespace": _ARTIFACT_OPERATION_TENANT_LOCK_NAMESPACE,
                "tenant_key": tenant_key,
            },
        )
    except SQLAlchemyError as exc:
        raise ArtifactOperationUnavailableError(
            "database artifact-operation coordination is unavailable"
        ) from exc
    if tenant_acquired is not True:
        raise ArtifactOperationBusyError()
    for slot in range(_ARTIFACT_OPERATION_GLOBAL_LOCK_COUNT):
        try:
            acquired = session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:namespace, :slot)"),
                {
                    "namespace": _ARTIFACT_OPERATION_GLOBAL_LOCK_NAMESPACE,
                    "slot": slot,
                },
            )
        except SQLAlchemyError as exc:
            raise ArtifactOperationUnavailableError(
                "database artifact-operation coordination is unavailable"
            ) from exc
        if acquired is True:
            return
    raise ArtifactOperationCapacityError()


@contextmanager
def _artifact_operation_scope(organization_id: str) -> Iterator[None]:
    """Own one re-entrant tenant/global slot across a complete storage operation."""

    active_organization_id = _ACTIVE_ARTIFACT_OPERATION_ORGANIZATION.get()
    if active_organization_id is not None:
        if active_organization_id != organization_id:
            raise RuntimeError("artifact operation scope cannot switch tenant")
        yield
        return

    tenant_lease = _TENANT_REVIEW_LIMITER.acquire(organization_id)
    token = _ACTIVE_ARTIFACT_OPERATION_ORGANIZATION.set(organization_id)
    try:
        with storage_read_deadline(_REVIEW_STORAGE_TOTAL_SECONDS):
            yield
    finally:
        _ACTIVE_ARTIFACT_OPERATION_ORGANIZATION.reset(token)
        tenant_lease.close()


async def _artifact_operation_dependency(
    principal: PrincipalDep,
    session: SessionDep,
) -> AsyncIterator[None]:
    """Fence a non-streaming mutation in Redis, PostgreSQL and this process."""

    local_lease: _TenantDownloadLease | None = None
    try:
        async with _artifact_operation_lease_manager().lease(principal.organization_id):
            _acquire_database_artifact_operation_locks(
                session,
                principal.organization_id,
            )
            local_lease = _TENANT_REVIEW_LIMITER.acquire(principal.organization_id)
            _ACTIVE_ARTIFACT_OPERATION_ORGANIZATION.set(principal.organization_id)
            with storage_read_deadline(_REVIEW_STORAGE_TOTAL_SECONDS):
                yield
    except (
        ArtifactOperationBusyError,
        ArtifactOperationCapacityError,
        ArtifactOperationOwnershipLostError,
        ArtifactOperationUnavailableError,
    ) as exc:
        raise _artifact_operation_http_error(exc) from exc
    finally:
        _ACTIVE_ARTIFACT_OPERATION_ORGANIZATION.set(None)
        if local_lease is not None:
            local_lease.close()


async def _artifact_stream_operation_dependency(
    principal: PrincipalDep,
    session: DownloadSessionDep,
) -> AsyncIterator[None]:
    """Keep explicit leases and DB locks alive through the complete body stream."""

    local_lease: _TenantDownloadLease | None = None
    try:
        async with _artifact_operation_lease_manager().lease(principal.organization_id):
            _acquire_database_artifact_operation_locks(
                session,
                principal.organization_id,
            )
            local_lease = _TENANT_DOWNLOAD_LIMITER.acquire(principal.organization_id)
            yield
    except (
        ArtifactOperationBusyError,
        ArtifactOperationCapacityError,
        ArtifactOperationOwnershipLostError,
        ArtifactOperationUnavailableError,
    ) as exc:
        raise _artifact_operation_http_error(exc) from exc
    finally:
        if local_lease is not None:
            local_lease.close()


_ARTIFACT_OPERATION_DEPENDENCY = Depends(
    _artifact_operation_dependency,
    scope="function",
)
_ARTIFACT_STREAM_OPERATION_DEPENDENCY = Depends(
    _artifact_stream_operation_dependency,
    scope="request",
)


def _verified_artifact_response(
    verified: VerifiedStoredObject,
    *,
    filename: str,
    tenant_lease: _TenantDownloadLease | None = None,
    stream_timeout_seconds: float | None = None,
) -> StreamingResponse:
    """Transfer ownership of a verified spool to response and background cleanup."""

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename) is None:
        try:
            verified.close()
        except ArtifactStorageUnavailableError as exc:
            raise _storage_unavailable() from exc
        raise HTTPException(status_code=409, detail="Artifact filename is invalid")
    digest = base64.b64encode(bytes.fromhex(verified.sha256)).decode("ascii")
    headers = {
        "Cache-Control": "private, no-store, no-transform, max-age=0",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(verified.size_bytes),
        "Content-Type": verified.content_type,
        "Digest": f"sha-256={digest}",
        "ETag": f'"{verified.sha256}"',
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }
    try:
        return _VerifiedArtifactStreamingResponse(
            verified,
            headers=headers,
            tenant_lease=tenant_lease,
            stream_timeout_seconds=(
                float(get_settings().artifact_stream_timeout_seconds)
                if stream_timeout_seconds is None
                else stream_timeout_seconds
            ),
        )
    except Exception:
        try:
            verified.close()
        except ArtifactStorageUnavailableError as exc:
            raise _storage_unavailable() from exc
        raise


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
    joint_retention_evidence_id: str | None = None,
) -> dict[str, Any]:
    project = tenant_project(session, principal, project_id) if project_id else None
    try:
        _, _, presented, _ = _canonical_preview_with_optional_retention(
            session,
            principal.organization_id,
            project,
            payload,
            design_id=project.id if project else "preview",
            revision=project.current_revision + 1 if project else 1,
            evidence_id=joint_retention_evidence_id,
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
    rows = list(
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
    for evidence in rows:
        _require_committed_domain_object(
            session,
            principal.organization_id,
            project_id=project.id,
            object_key=evidence.object_key,
            sha256=evidence.sha256,
            size_bytes=evidence.size_bytes,
            media_type=evidence.content_type,
            owner_type="external_evidence",
            owner_id=evidence.id,
        )
    return rows


def _current_joint_retention_download_version(
    session: Session,
    organization_id: str,
    project: Project,
    evidence: ExternalEvidence,
) -> DesignVersion:
    """Require the evidence to be named by the project's current revision."""

    version = session.scalar(
        select(DesignVersion).where(
            DesignVersion.organization_id == organization_id,
            DesignVersion.project_id == project.id,
            DesignVersion.revision == project.current_revision,
        )
    )
    frozen_snapshot = (
        version.result_json.get("retention_trust")
        if version is not None and isinstance(version.result_json, Mapping)
        else None
    )
    try:
        bound_spec = (
            BookcaseDesignSpec.model_validate(version.spec_json) if version is not None else None
        )
    except ValidationError as exc:
        raise _retention_trust_error(
            "JOINT_RETENTION_BINDING_STALE",
            "The current retention-bound revision cannot be reconstructed.",
            "Create and review a new revision from current signed evidence.",
        ) from exc
    if (
        version is None
        or bound_spec is None
        or bound_spec.joint_retention is None
        or not isinstance(frozen_snapshot, Mapping)
        or frozen_snapshot.get("storage_evidence_id") != evidence.id
        or frozen_snapshot.get("storage_evidence_sha256") != evidence.sha256
        or frozen_snapshot.get("base_design_hash") != evidence.design_hash
    ):
        raise _retention_trust_error(
            "JOINT_RETENTION_BINDING_STALE",
            "The signed retention evidence is not bound to the project's current revision.",
            "Select the current revision's authenticated evidence before downloading it.",
        )
    return version


@router.get(
    "/projects/{project_id}/evidence/{evidence_id}/download",
    response_class=StreamingResponse,
    dependencies=[_ARTIFACT_STREAM_OPERATION_DEPENDENCY],
    responses={
        200: {
            "description": (
                "Exact certifier-signed joint-retention JSON bytes after current-revision, "
                "Ed25519, activated-registry/high-water, tenant, ledger, revocation, expiry "
                "and SHA-256 verification"
            ),
            "headers": {
                "Cache-Control": {"schema": {"type": "string"}},
                "Content-Disposition": {"schema": {"type": "string"}},
                "Content-Length": {"schema": {"type": "string"}},
                "Digest": {"schema": {"type": "string"}},
                "ETag": {"schema": {"type": "string"}},
                "Pragma": {"schema": {"type": "string"}},
                "X-Content-Type-Options": {"schema": {"type": "string"}},
            },
            "content": {"application/json": {"schema": {"type": "string", "format": "binary"}}},
        },
        401: {"description": "Authentication required."},
        403: {"description": "The active role may not download signed retention evidence."},
        404: {"description": "The tenant-scoped project or signed evidence was not found."},
        409: {"description": "The evidence is stale, revoked, expired or unverifiable."},
        422: {"description": "A path identifier is not a canonical UUID."},
        429: {"description": "The bounded download channel is at capacity."},
        503: {"description": "The evidence ledger or immutable object is unavailable."},
    },
)
def download_joint_retention_evidence(
    project_id: Annotated[
        str,
        Path(pattern=rf"^{_CANONICAL_PROJECT_UUID_PATTERN.pattern}$"),
    ],
    evidence_id: Annotated[
        str,
        Path(pattern=rf"^{_CANONICAL_PROJECT_UUID_PATTERN.pattern}$"),
    ],
    session: DownloadSessionDep,
    principal: JointRetentionEvidenceDownloadDep,
) -> StreamingResponse:
    """Stream only the exact currently valid signed JSON held by this tenant/project."""

    project = tenant_project(session, principal, project_id)
    evidence = session.scalar(
        select(ExternalEvidence).where(
            ExternalEvidence.id == evidence_id,
            ExternalEvidence.organization_id == principal.organization_id,
            ExternalEvidence.project_id == project.id,
            ExternalEvidence.evidence_type == "joint_retention",
            ExternalEvidence.rule_id == "CB-JOINT-001",
        )
    )
    if evidence is None:
        raise HTTPException(status_code=404, detail="Signed retention evidence not found")
    expires_at = _as_utc(evidence.expires_at) if evidence.expires_at is not None else None
    if evidence.revoked_at is not None or (
        expires_at is not None and expires_at <= datetime.now(UTC)
    ):
        raise _external_evidence_stale(evidence.id)
    if (
        evidence.content_type != "application/json"
        or type(evidence.size_bytes) is not int
        or evidence.size_bytes <= 0
        or evidence.size_bytes > MAX_SIGNED_EVIDENCE_BYTES
        or re.fullmatch(r"[a-f0-9]{64}", evidence.sha256) is None
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "JOINT_RETENTION_EVIDENCE_INTEGRITY_FAILED",
                "message": "The signed retention evidence record is not a canonical JSON object.",
                "solution": "Register a new immutable certifier-signed JSON statement.",
            },
        )

    version = _current_joint_retention_download_version(
        session,
        principal.organization_id,
        project,
        evidence,
    )
    initial_binding = _external_evidence_binding(evidence)
    initial_version_binding = _retention_download_version_binding(version)
    _require_committed_domain_object(
        session,
        principal.organization_id,
        project_id=project.id,
        object_key=evidence.object_key,
        sha256=evidence.sha256,
        size_bytes=evidence.size_bytes,
        media_type=evidence.content_type,
        owner_type="external_evidence",
        owner_id=evidence.id,
    )
    verified: VerifiedStoredObject | None = None
    try:
        try:
            with storage_read_deadline(_REVIEW_STORAGE_TOTAL_SECONDS):
                # Use the same fail-closed resolver as validation, generation
                # and release inside this request's absolute storage deadline.
                # It rechecks Ed25519 authenticity, registry/high-water,
                # revocations, exact design applicability and the complete
                # frozen revision snapshot before any bytes can be streamed.
                _require_current_retention_binding(
                    session,
                    principal.organization_id,
                    version,
                )
                verified = open_verified_stored_object(
                    StoredObjectExpectation(
                        object_key=evidence.object_key,
                        sha256=evidence.sha256,
                        size_bytes=evidence.size_bytes,
                        content_type=evidence.content_type,
                        required_metadata=(("immutable", "true"),),
                    ),
                    max_bytes=MAX_SIGNED_EVIDENCE_BYTES,
                )
        except ArtifactIntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "JOINT_RETENTION_EVIDENCE_INTEGRITY_FAILED",
                    "message": "The signed retention evidence no longer matches its record.",
                    "solution": "Register and review a new immutable signed JSON statement.",
                },
            ) from exc
        except ArtifactStorageUnavailableError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "JOINT_RETENTION_EVIDENCE_STORAGE_UNAVAILABLE",
                    "message": "Signed retention evidence storage cannot currently be verified.",
                    "solution": "Retry after immutable object storage is healthy.",
                },
            ) from exc

        current = session.scalar(
            select(ExternalEvidence)
            .where(
                ExternalEvidence.id == evidence_id,
                ExternalEvidence.organization_id == principal.organization_id,
                ExternalEvidence.project_id == project.id,
            )
            .execution_options(populate_existing=True)
        )
        if current is None:
            raise _external_evidence_snapshot_stale()
        current_expiry = _as_utc(current.expires_at) if current.expires_at is not None else None
        if current.revoked_at is not None or (
            current_expiry is not None and current_expiry <= datetime.now(UTC)
        ):
            raise _external_evidence_stale(current.id)
        if (
            current.evidence_type != "joint_retention"
            or current.rule_id != "CB-JOINT-001"
            or current.content_type != "application/json"
            or _external_evidence_binding(current) != initial_binding
        ):
            raise _external_evidence_snapshot_stale()
        current_project = session.scalar(
            select(Project)
            .where(
                Project.id == project.id,
                Project.organization_id == principal.organization_id,
            )
            .execution_options(populate_existing=True)
        )
        current_version = session.scalar(
            select(DesignVersion)
            .where(
                DesignVersion.id == version.id,
                DesignVersion.organization_id == principal.organization_id,
                DesignVersion.project_id == project.id,
            )
            .execution_options(populate_existing=True)
        )
        if (
            current_project is None
            or current_version is None
            or current_project.current_revision != version.revision
            or _retention_download_version_binding(current_version) != initial_version_binding
        ):
            raise _retention_trust_error(
                "JOINT_RETENTION_BINDING_STALE",
                "The current retention-bound revision changed during verification.",
                "Refresh the project and retry against its current revision.",
            )
        return _verified_artifact_response(
            verified,
            filename=f"custombuild-joint-retention-{current.id}.json",
        )
    except BaseException:
        if verified is not None:
            verified.close()
        raise


@router.post(
    "/projects/{project_id}/evidence",
    response_model=ExternalEvidenceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_ARTIFACT_OPERATION_DEPENDENCY],
)
async def upload_external_evidence(
    project_id: str,
    session: SessionDep,
    principal: ReviewerDep,
    document: Annotated[UploadFile, File()],
    evidence_type: Annotated[
        Literal["wall_anchor", "hardware", "material_grain", "joint_retention"],
        Form(),
    ],
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
    normalized_expiry: datetime | None = None
    if expires_at is not None:
        normalized_expiry = (
            expires_at.replace(tzinfo=UTC) if expires_at.tzinfo is None else expires_at
        ).astimezone(UTC)
        if normalized_expiry <= datetime.now(UTC):
            raise HTTPException(status_code=422, detail="Evidence expiry must be in the future")
    content = await document.read(MAX_CATALOG_SOURCE_BYTES + 1)
    try:
        if evidence_type == "joint_retention":
            if (document.content_type or "").split(";", 1)[
                0
            ].strip().lower() != "application/json" or not (
                document.filename or ""
            ).lower().endswith(".json"):
                raise ValueError("signed joint-retention evidence must be an application/json file")
            validate_signed_retention_evidence_structure(content)
            statement = json.loads(content)
            entry = statement.get("catalogue_entry") if isinstance(statement, Mapping) else None
            signed_expiry_raw = (
                statement.get("expires_at") if isinstance(statement, Mapping) else None
            )
            if not isinstance(entry, Mapping):
                raise ValueError("signed joint-retention catalogue entry is missing")
            try:
                signed_expiry = datetime.fromisoformat(str(signed_expiry_raw)).astimezone(UTC)
            except (TypeError, ValueError) as exc:
                raise ValueError("signed joint-retention expiry is malformed") from exc
            if (
                catalog_id.strip() != entry.get("system_id")
                or catalog_version.strip() != entry.get("system_version")
                or normalized_expiry != signed_expiry
            ):
                raise ValueError(
                    "upload metadata must match signed retention system, version and expiry"
                )
        else:
            validate_upload(content, document.content_type or "", document.filename or "")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "EXTERNAL_EVIDENCE_INVALID",
                "message": str(exc),
                "solution": (
                    "Choose a supported image, PDF, DXF or signed retention JSON no larger "
                    "than 20 MiB and retry."
                ),
            },
        ) from exc
    evidence_id = str(uuid4())
    digest = hashlib.sha256(content).hexdigest()
    media_type = document.content_type or "application/octet-stream"
    extension = (document.filename or "evidence").rpartition(".")[2].lower()
    object_key = (
        f"{principal.organization_id}/projects/{project.id}/external-evidence/"
        f"sha256/{digest}/{evidence_id}/document.{extension}"
    )
    claim = StorageObjectClaim(
        project_id=project.id,
        object_key=object_key,
        sha256=digest,
        size_bytes=len(content),
        media_type=media_type,
        owner_type="external_evidence",
        owner_id=evidence_id,
        idempotency_key=f"external-evidence:{evidence_id}",
    )
    storage_lease_token = await _reserve_upload_storage(
        session,
        principal.organization_id,
        claim,
    )
    try:
        await run_in_threadpool(
            store_evidence_object,
            object_key,
            content,
            media_type,
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
        content_type=media_type,
        created_by=principal.user_id,
        expires_at=normalized_expiry,
    )
    session.add(evidence)
    session.flush()
    _commit_upload_storage(
        session,
        principal.organization_id,
        claim,
        storage_lease_token,
    )
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
    dependencies=[_ARTIFACT_OPERATION_DEPENDENCY],
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
    production_context = payload.production_context.model_dump(mode="json", exclude_none=True)
    try:
        template_capability = require_template_for_revision(
            payload.template_id, payload.spec.furniture_type
        )
    except TemplateCapabilityError as exc:
        raise _template_capability_error(exc) from exc
    try:
        spec, result, presented, retention_trust = _canonical_preview_with_optional_retention(
            session,
            principal.organization_id,
            project,
            payload.spec,
            design_id=project.id,
            revision=revision,
            evidence_id=payload.joint_retention_evidence_id,
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

    try:
        generation_stock_projection_for_design(
            result,
            production_context,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "WORKSHOP_PRODUCTION_CONTEXT_INVALID",
                "message": (
                    "The workshop production context does not match the server-built design."
                ),
                "solution": (
                    "Correct the exact stock material, version, thickness, coverage or "
                    "two-sided registration before creating a design revision."
                ),
                "errors": [
                    {
                        "type": "value_error",
                        "loc": ["body", "production_context"],
                        "msg": str(exc),
                    }
                ],
            },
        ) from exc

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
    context_payload = {
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
    if retention_trust is not None:
        context_payload["retention_trust"] = retention_trust
    context_hash = canonical_hash(context_payload)

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
            job.next_attempt_at = None
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
            "retention_trust": retention_trust,
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
        bundle_sha256 = _release_bundle_sha256(release)
        release_payload = {
            "release_id": release.id,
            "release_number": release.release_number,
            "status": "released",
            "bundle_sha256": bundle_sha256,
            "manifest_sha256": release.manifest_sha256,
            "release_kind": "design_review",
            "machine_use": "validation_only",
            "physical_cutting_authorized": False,
        }
    return {
        "project_id": project.id,
        "version": version,
        "approvals": approvals,
        "latest_job": latest_job,
        "release": release_payload,
    }


@router.post(
    "/projects/{project_id}/versions/{revision}/validate",
    response_model=DesignVersionRead,
    dependencies=[_ARTIFACT_OPERATION_DEPENDENCY],
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
    _require_current_retention_binding(session, principal.organization_id, version)
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
    dependencies=[_ARTIFACT_OPERATION_DEPENDENCY],
)
def generate_version(
    project_id: str,
    revision: int,
    payload: GenerationRequest,
    session: SessionDep,
    principal: GeneratorDep,
) -> GenerationJob:
    version = tenant_version(session, principal, project_id, revision)
    session.refresh(version, with_for_update=True)
    _require_rule_engine()
    _require_frozen_template_capability(version)
    _verify_frozen_reference_asset(session, principal, version)
    _require_current_retention_binding(
        session,
        principal.organization_id,
        version,
        minimum_valid_until=(datetime.now(UTC) + GENERATION_JOB_TIMEOUT + timedelta(minutes=5)),
    )
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
        ).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "FROZEN_PRODUCTION_CONTEXT_MISSING: save a new design revision "
                "before generating production evidence"
            ),
        ) from exc
    requested_production_context: dict[str, Any] = {
        "stock_width_mm": payload.stock_width_mm,
        "stock_height_mm": payload.stock_height_mm,
        "stock_count": payload.stock_count,
        "back_stock_width_mm": payload.back_stock_width_mm,
        "back_stock_height_mm": payload.back_stock_height_mm,
        "back_stock_count": payload.back_stock_count,
        "machine_profile_id": payload.machine_profile_id,
    }
    if payload.stock_profiles is not None:
        requested_production_context["stock_profiles"] = [
            profile.model_dump(mode="json") for profile in payload.stock_profiles
        ]
    if payload.two_sided_registrations is not None:
        requested_production_context["two_sided_registrations"] = [
            registration.model_dump(mode="json") for registration in payload.two_sided_registrations
        ]
    if requested_production_context != frozen_production_context:
        raise HTTPException(
            status_code=409,
            detail=(
                "FROZEN_PRODUCTION_CONTEXT_MISMATCH: stock format, stock count, "
                "back-stock format or machine profile changed; save a new design "
                "revision before generating production evidence"
            ),
        )
    request_json = payload.model_dump(mode="json", exclude_none=True)
    verified_evidence_bindings: dict[str, _ExternalEvidenceBinding] = {}
    request_json["external_evidence"] = _verified_external_evidence(
        session,
        principal.organization_id,
        version.project_id,
        version.design_hash,
        payload.external_evidence_ids,
        verified_bindings=verified_evidence_bindings,
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
        session,
        principal.organization_id,
        design_approval,
        version,
        verified_evidence_bindings=verified_evidence_bindings,
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
        generation_requeued = False
        if existing.status == JobStatus.failed:
            _require_generation_storage_retry_ready(
                session,
                principal.organization_id,
                existing.id,
            )
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
            existing.next_attempt_at = retry_started_at
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
            generation_requeued = True
        elif existing.status == JobStatus.succeeded:
            # A succeeded row is immutable evidence.  Reject a detached result
            # before the repair/reuse branch can delete artifacts, approvals or
            # otherwise mutate production state.
            _require_current_generation_context(existing, version)
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
                _require_generation_storage_retry_ready(
                    session,
                    principal.organization_id,
                    existing.id,
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
                existing.next_attempt_at = retry_started_at
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
                generation_requeued = True
        if generation_requeued:
            _flush_generation_storage_retry(session)
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
    queued_at = datetime.now(UTC)
    job = GenerationJob(
        organization_id=principal.organization_id,
        design_version_id=version.id,
        status=JobStatus.queued,
        idempotency_key=idempotency_key,
        production_context_hash=production_context_hash,
        production_engine_context_json=resolved.context.as_dict(),
        request_json=request_json,
        deadline_at=queued_at + GENERATION_JOB_TIMEOUT,
        next_attempt_at=queued_at,
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


@router.post(
    "/projects/{project_id}/versions/{revision}/approve",
    response_model=DesignVersionRead,
    dependencies=[_ARTIFACT_OPERATION_DEPENDENCY],
)
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
    _require_current_retention_binding(session, principal.organization_id, version)
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
        # Retention is a frozen design invariant, not review evidence.  Surface
        # its canonical blocker before looking for CAM artifacts so a truthful
        # CAM-blocked package cannot degrade into a generic missing-file error.
        _require_resolved_dado_retention(version)
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
        _require_four_eyes_approval_separation(
            design_approval.approved_by,
            principal.user_id,
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
        verified_evidence_bindings: dict[str, _ExternalEvidenceBinding] = {}
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
                    verified_bindings=verified_evidence_bindings,
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
        _require_external_evidence_bindings_current(session, verified_evidence_bindings)
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
    "/projects/{project_id}/versions/{revision}/workshop-runs",
    status_code=status.HTTP_409_CONFLICT,
    response_model=WorkshopRunBlockedResponse,
    response_description=(
        "The server-owned generation/release is not an immutable executable workshop package."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": "The active role lacks workshop preparation capability."
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "The tenant-scoped project or revision was not found."
        },
    },
)
def prepare_workshop_run(
    project_id: Annotated[
        str,
        Path(pattern=rf"^{_CANONICAL_UUID_PATTERN.pattern}$"),
    ],
    revision: Annotated[int, Path(ge=1)],
    payload: WorkshopRunPrepare,
    session: SessionDep,
    principal: WorkshopPreparerDep,
) -> NoReturn:
    """Prepare no physical state until a real executable package exists.

    Clients can name only a generation job plus an explicit confirmation. The
    tenant revision, job result and release binding are resolved by the server;
    hashes, policies, machine identity and evidence cannot be supplied here.
    """

    version = tenant_version(session, principal, project_id, revision)
    try:
        require_workshop_preparation_source(
            session,
            organization_id=principal.organization_id,
            version=version,
            generation_job_id=payload.generation_job_id,
        )
        # The helper is intentionally ``NoReturn`` while executable package
        # persistence does not exist.  Keep the HTTP boundary fail-closed even
        # if a future refactor accidentally changes that internal contract.
        raise WorkshopPreparationBlocker(
            code="WORKSHOP_EXECUTABLE_PACKAGE_MISSING",
            message=(
                "Workshop preparation returned without creating a verified immutable "
                "executable package."
            ),
            solution=(
                "Keep preparation blocked until the server can persist and reread an "
                "exact executable program inventory."
            ),
        )
    except WorkshopPreparationBlocker as exc:
        # HTTPException payloads bypass FastAPI response-model validation.
        # Validate explicitly so a future internal blocker cannot drift from
        # the public enum/envelope while still returning a plausible 409.
        detail = WorkshopRunBlockerDetail.model_validate(exc.as_detail()).model_dump(mode="json")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from exc


@router.post(
    "/projects/{project_id}/versions/{revision}/release",
    response_model=ReleaseRead,
    dependencies=[_ARTIFACT_OPERATION_DEPENDENCY],
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
    _require_frozen_template_capability(version)
    # A missing CAM approval must never mask an unresolved physical-retention
    # invariant.  Release therefore returns the same canonical blocker as CAM.
    _require_resolved_dado_retention(version)
    _require_current_retention_binding(session, principal.organization_id, version)
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
    cam_approval_binding = _approval_binding(cam_approval)
    _require_four_eyes_approval_separation(
        design_approval.approved_by,
        cam_approval.approved_by,
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
    cam_approval = _require_approval_binding_current(
        session,
        cam_approval_binding,
        approval_id=cam_approval.id,
        organization_id=principal.organization_id,
        design_version_id=version.id,
        approval_type="cam",
        detail={
            "code": "CAM_APPROVAL_SNAPSHOT_STALE",
            "message": "The CAM approval changed while production evidence was being verified.",
            "solution": "Review and approve the current generated package again.",
        },
    )
    _require_four_eyes_approval_separation(
        design_approval.approved_by,
        cam_approval.approved_by,
    )
    if cam_approval.generation_job_id != job.id:
        raise HTTPException(status_code=409, detail="The bound CAM approval changed generation job")
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
    release_artifacts = tuple(
        session.scalars(
            select(Artifact)
            .where(
                Artifact.organization_id == principal.organization_id,
                Artifact.generation_job_id == job.id,
            )
            .order_by(Artifact.kind, Artifact.id)
        )
    )
    _expectations, release_inventory = _frozen_release_inventory(job, release_artifacts)
    if not isinstance(job.result_json, dict):
        raise HTTPException(status_code=409, detail="Checked generation result is malformed")
    frozen_generation_result = json.loads(canonical_json_bytes(job.result_json))
    if not isinstance(frozen_generation_result, dict):
        raise HTTPException(status_code=409, detail="Checked generation result is malformed")
    if existing_release is not None:
        if not _release_matches_frozen_job(existing_release, job, release_inventory):
            raise HTTPException(
                status_code=409,
                detail="The stored release does not match the checked immutable package",
            )
        bundle_sha256 = _release_bundle_sha256(existing_release)
        return {
            "release_id": existing_release.id,
            "release_number": existing_release.release_number,
            "status": version.status.value,
            "bundle_sha256": bundle_sha256,
            "manifest_sha256": existing_release.manifest_sha256,
            "release_kind": "design_review",
            "machine_use": "validation_only",
            "physical_cutting_authorized": False,
        }
    release = Release(
        organization_id=principal.organization_id,
        design_version_id=version.id,
        generation_job_id=job.id,
        release_number=payload.release_number,
        released_by=principal.user_id,
        manifest_sha256=manifest_sha,
        production_context_hash=job.production_context_hash,
        generation_result_json=frozen_generation_result,
        artifact_inventory_json=release_inventory,
    )
    version.status = DesignStatus.released
    version.immutable = True
    session.add(release)
    session.flush()
    bundle_sha256 = _release_bundle_sha256(release)
    audit(
        session,
        principal,
        "design_version.released",
        "release",
        release.id,
        {
            "release_number": payload.release_number,
            "bundle_sha256": bundle_sha256,
            "manifest_sha256": manifest_sha,
            "generation_job_id": job.id,
            "production_context_hash": job.production_context_hash,
            "release_scope": "design_review",
            "machine_use": "validation_only",
            "physical_cutting_authorized": False,
            "artifact_inventory": release_inventory,
        },
    )
    return {
        "release_id": release.id,
        "release_number": release.release_number,
        "status": version.status.value,
        "bundle_sha256": bundle_sha256,
        "manifest_sha256": manifest_sha,
        "release_kind": "design_review",
        "machine_use": "validation_only",
        "physical_cutting_authorized": False,
    }


@router.get(
    "/releases/{release_id}/artifacts",
    response_model=list[ReleaseArtifactRead],
    dependencies=[_ARTIFACT_OPERATION_DEPENDENCY],
)
def list_release_artifacts(
    release_id: str,
    session: SessionDep,
    principal: PrincipalDep,
) -> list[dict[str, Any]]:
    """List the exact package inventory frozen by a historical release."""

    archive = _resolve_release_archive(session, principal.organization_id, release_id)
    archive = _verify_release_archive_owned(
        session,
        principal.organization_id,
        archive,
    )
    expires = int(time.time()) + get_settings().artifact_url_ttl_seconds
    result: list[dict[str, Any]] = []
    for item in archive.artifacts:
        signature_subject = _release_artifact_signature_subject(release_id, item.id)
        download_path = (
            f"/v1/releases/{release_id}/artifacts/{item.id}/download"
            f"?expires={expires}&signature="
            f"{sign_artifact_access(signature_subject, principal.organization_id, expires)}"
        )
        result.append(
            {
                "release_id": archive.release.id,
                "release_number": archive.release.release_number,
                "revision": archive.version.revision,
                "id": item.id,
                "kind": item.kind,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "content_type": item.content_type,
                "download_url": download_path,
                "download_path": download_path,
            }
        )
    return result


@router.get(
    "/releases/{release_id}/artifacts/{artifact_id}/download",
    response_class=StreamingResponse,
    dependencies=[_ARTIFACT_STREAM_OPERATION_DEPENDENCY],
    responses={
        200: {
            "description": "Fully verified immutable release artifact bytes",
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
def download_release_artifact(
    release_id: str,
    artifact_id: str,
    expires: int,
    signature: str,
    session: DownloadSessionDep,
    principal: PrincipalDep,
) -> StreamingResponse:
    """Stream an archived release without requiring it to be the current revision."""

    signature_subject = _release_artifact_signature_subject(release_id, artifact_id)
    verify_artifact_access(
        signature_subject,
        principal.organization_id,
        expires,
        signature,
    )
    archive = _resolve_release_archive(session, principal.organization_id, release_id)
    artifact = next((item for item in archive.artifacts if item.id == artifact_id), None)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Release artifact not found")
    _require_retention_bound_bundle_download_capability(
        principal,
        archive.version,
        artifact.kind,
    )

    verified: VerifiedStoredObject | None = None
    try:
        archive, verified = _prepare_release_artifact_download(
            session,
            principal.organization_id,
            archive,
            artifact,
        )
        current_artifact = next(
            (item for item in archive.artifacts if item.id == artifact_id),
            None,
        )
        if current_artifact is None:
            raise _release_archive_error()
        filename = _release_artifact_filename(
            archive.version.project_id,
            archive.release.release_number,
            archive.version.revision,
            current_artifact.kind,
            current_artifact.content_type,
        )
        return _verified_artifact_response(verified, filename=filename)
    except BaseException:
        if verified is not None:
            verified.close()
        raise


@router.get(
    "/jobs/{job_id}/artifacts",
    response_model=list[ArtifactRead],
    dependencies=[_ARTIFACT_OPERATION_DEPENDENCY],
)
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
    _require_current_artifacts(session, principal.organization_id, job)
    _require_review_evidence(
        session,
        principal.organization_id,
        job,
        stream_hash=False,
        require_cam=False,
        bind_review_documents=True,
    )
    # Review verification can perform bounded object-store I/O.  Refresh the
    # mutable project/version state after that I/O so a concurrent revision can
    # never receive newly signed links for stale artifacts.
    _require_current_artifacts(
        session,
        principal.organization_id,
        job,
        populate_existing=True,
    )
    artifacts = session.scalars(
        select(Artifact).where(
            Artifact.generation_job_id == job.id,
            Artifact.organization_id == principal.organization_id,
        )
    )
    now = int(time.time())
    expires = now + get_settings().artifact_url_ttl_seconds
    result: list[dict[str, Any]] = []
    for item in artifacts:
        download_path = (
            f"/v1/artifacts/{item.id}/download?expires={expires}&signature="
            f"{sign_artifact_access(item.id, principal.organization_id, expires)}"
        )
        result.append(
            {
                "id": item.id,
                "kind": item.kind,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "content_type": item.content_type,
                # Kept for one schema transition; both values are now the same
                # authenticated API path and never expose an object-store URL.
                "download_url": download_path,
                "download_path": download_path,
            }
        )
    return result


@router.get(
    "/artifacts/{artifact_id}/download",
    response_class=StreamingResponse,
    dependencies=[_ARTIFACT_STREAM_OPERATION_DEPENDENCY],
    responses={
        200: {
            "description": "Fully verified artifact bytes",
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
def download_artifact(
    artifact_id: str,
    expires: int,
    signature: str,
    session: DownloadSessionDep,
    principal: PrincipalDep,
) -> StreamingResponse:
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
    version = _require_current_artifacts(session, principal.organization_id, job)
    _require_retention_bound_bundle_download_capability(
        principal,
        version,
        artifact.kind,
    )
    verified: VerifiedStoredObject | None = None
    try:
        verified = _prepare_review_artifact_download(
            session,
            principal.organization_id,
            job,
            version,
            artifact.kind,
        )
        try:
            filename = _artifact_filename(
                artifact.kind,
                version.revision,
                version.project_id,
                artifact.content_type,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="Artifact download identity is invalid",
            ) from exc
        # The request-scoped session deliberately retains its PostgreSQL
        # transaction-level tenant/global advisory locks until streaming ends.
        # Capacity is bounded to eight transfers and the response has a hard
        # deadline, so a second replica cannot overlap this tenant even if
        # Redis restarts while bytes are in flight.
        return _verified_artifact_response(
            verified,
            filename=filename,
        )
    except BaseException:
        if verified is not None:
            verified.close()
        raise


@router.post(
    "/projects/{project_id}/imports/inspect",
    response_model=ImportInspection,
    dependencies=[_ARTIFACT_OPERATION_DEPENDENCY],
)
async def inspect_import(
    project_id: str,
    session: SessionDep,
    principal: DesignerDep,
    document: Annotated[UploadFile, File()],
) -> ImportInspection:
    project = tenant_project(session, principal, project_id)
    content = await document.read(MAX_CATALOG_SOURCE_BYTES + 1)
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
        _require_committed_domain_object(
            session,
            principal.organization_id,
            project_id=existing.project_id,
            object_key=existing.object_key,
            sha256=existing.sha256,
            size_bytes=existing.size_bytes,
            media_type=existing.media_type,
            owner_type="imported_asset",
            owner_id=existing.id,
        )
        await run_in_threadpool(_verify_imported_asset_bytes, existing)
        asset = existing
    else:
        asset_id = str(uuid4())
        object_key = (
            f"{principal.organization_id}/projects/{project.id}/reference-imports/"
            f"sha256/{digest}/{asset_id}"
        )
        claim = StorageObjectClaim(
            project_id=project.id,
            object_key=object_key,
            sha256=digest,
            size_bytes=len(content),
            media_type=media_type,
            owner_type="imported_asset",
            owner_id=asset_id,
            idempotency_key=f"imported:{asset_id}",
        )
        storage_lease_token = await _reserve_upload_storage(
            session,
            principal.organization_id,
            claim,
        )
        try:
            await run_in_threadpool(
                store_immutable_object,
                object_key,
                content,
                media_type,
                digest,
            )
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
            _require_committed_domain_object(
                session,
                principal.organization_id,
                project_id=raced.project_id,
                object_key=raced.object_key,
                sha256=raced.sha256,
                size_bytes=raced.size_bytes,
                media_type=raced.media_type,
                owner_type="imported_asset",
                owner_id=raced.id,
            )
            await run_in_threadpool(_verify_imported_asset_bytes, raced)
            asset = raced
        else:
            _commit_upload_storage(
                session,
                principal.organization_id,
                claim,
                storage_lease_token,
            )
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
