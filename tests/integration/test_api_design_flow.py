from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import app.api as api_module
import app.artifact_operations as artifact_operations
import app.auth as auth_module
import app.design_service as design_service_module
import app.storage as storage_module
import pytest
from app.auth import DEV_ORG_NORDIC, DEV_USER_NORDIC
from app.config import get_settings
from app.db import get_session_factory
from app.design_service import canonical_preview
from app.joint_retention import (
    JOINT_GEOMETRY_FINGERPRINT_SCHEMA,
    SIGNED_EVIDENCE_SCHEMA_VERSION,
    TRUST_REGISTRY_SCHEMA_VERSION,
)
from app.main import app
from app.models import (
    Approval,
    Artifact,
    AuditEvent,
    DesignStatus,
    DesignVersion,
    ExternalEvidence,
    GenerationJob,
    ImportedAsset,
    JobStatus,
    Membership,
    OutboxEvent,
    Project,
    Release,
    Role,
    StorageGlobalQuota,
    StorageTenantQuota,
    StoredObject,
    StoredObjectState,
    User,
)
from app.schemas import BookcasePreviewInput, WorkspaceIntentV1
from app.storage_quota import (
    StorageObjectClaim,
    commit_storage_batch_in_transaction,
    reserve_storage_batch_in_transaction,
)
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from custombuild_manufacturing import (
    BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
    DFM_ENGINE_VERSION,
    DFM_GRAIN_BLOCKER_CODE,
    DFM_GRAIN_REQUIRED_ACTION,
    DFM_GRAIN_RULE_MESSAGE,
    MANIFEST_CONTEXT_HASH_FIELDS,
    MAX_CORE_DOCUMENT_BYTES,
    MAX_EVIDENCE_ARTIFACTS,
    MAX_EVIDENCE_TOTAL_BYTES,
    DFMIssue,
    DFMReport,
    Severity,
    blocked_design_review_package_status,
    canonical_json_bytes,
    generated_design_review_package_status,
    stock_profile_missing_issue,
)
from custombuild_manufacturing.package import PRODUCTION_MANIFEST_SCHEMA_VERSION
from custombuild_manufacturing.readiness import (
    LEGACY_WORKSHOP_READINESS_SCHEMA_VERSION,
    build_workshop_readiness_report,
)
from custombuild_worker import tasks as worker_tasks
from fastapi import HTTPException
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope

HEADERS = {"Authorization": "Bearer demo-nordic-owner"}
FOUR_EYES_REVIEWER_HEADERS = {"Authorization": "Bearer demo-nordic-four-eyes-reviewer"}
FOUR_EYES_REVIEWER_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
_REAL_DADO_RETENTION_GATE = api_module._require_resolved_dado_retention
_REAL_DADO_RETENTION_PROJECTION = api_module._frozen_dado_retention_is_unresolved


class _SimulatedVerifiedDownload:
    """API-boundary token; storage's sealed implementation has dedicated tests."""

    def __init__(
        self,
        expectation: storage_module.StoredObjectExpectation,
        payload: bytes | None = None,
    ) -> None:
        self.sha256 = expectation.sha256
        self.size_bytes = expectation.size_bytes
        self.content_type = expectation.content_type
        self._expectation = expectation
        self._payload = payload
        self.closed = False
        self.close_calls = 0

    def validation_bytes(self, *, max_bytes: int) -> bytes:
        assert not self.closed
        payload = self._payload or _simulated_verified_review_document(
            self._expectation,
            max_bytes=max_bytes,
        )
        assert len(payload) <= max_bytes
        return payload

    def iter_bytes(self) -> Any:
        payload = self._payload
        remaining = self.size_bytes
        offset = 0
        try:
            while remaining:
                chunk_size = min(1024 * 1024, remaining)
                remaining -= chunk_size
                if payload is None:
                    yield b"x" * chunk_size
                else:
                    yield payload[offset : offset + chunk_size]
                    offset += chunk_size
        finally:
            self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.close_calls += 1


@pytest.fixture(autouse=True)
def _avoid_external_object_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Endpoint tests opt into simulated failures; storage behavior has focused unit tests."""

    def open_verified(
        expectation: storage_module.StoredObjectExpectation,
        *,
        max_bytes: int,
    ) -> _SimulatedVerifiedDownload:
        if expectation.size_bytes > max_bytes:
            raise storage_module.ArtifactIntegrityError("size limit")
        return _SimulatedVerifiedDownload(expectation)

    monkeypatch.setattr(
        api_module,
        "verify_stored_object",
        lambda _expectation, *, stream_hash: None,
    )
    monkeypatch.setattr(
        api_module,
        "read_verified_stored_object",
        _simulated_verified_review_document,
    )
    monkeypatch.setattr(
        api_module,
        "open_verified_stored_object",
        open_verified,
    )
    monkeypatch.setattr(api_module, "store_immutable_object", lambda *_args: None)
    # Most integration cases exercise deeper approval, tamper and release
    # invariants under an explicit future structured-retention premise.  The
    # real plain-DADO gate has dedicated negative regressions below.
    monkeypatch.setattr(
        api_module,
        "_require_resolved_dado_retention",
        lambda _version: None,
    )
    monkeypatch.setattr(
        api_module,
        "_frozen_dado_retention_is_unresolved",
        lambda _version: False,
    )


def valid_spec() -> dict[str, object]:
    return {
        "width_mm": 700,
        "height_mm": 1000,
        "depth_mm": 320,
        "nominal_thickness_mm": 18,
        "measured_thickness_mm": 18,
        "shelf_count": 2,
        "load_per_shelf_kg": 10,
        "back_panel": True,
        "plinth": True,
        "divider_count": 0,
        "joint_system": "dado",
        "reinforcement_mode": "manual",
        "wall_anchor_required": False,
    }


def valid_production_context(**overrides: object) -> dict[str, object]:
    return {
        "stock_width_mm": 2440,
        "stock_height_mm": 1220,
        "stock_count": 4,
        "back_stock_width_mm": 2440,
        "back_stock_height_mm": 1220,
        "back_stock_count": 2,
        "machine_profile_id": "custombuild-router-1325-linuxcnc",
        **overrides,
    }


def stockless_production_context() -> dict[str, object]:
    return valid_production_context(
        stock_width_mm=100,
        stock_height_mm=100,
        back_stock_width_mm=100,
        back_stock_height_mm=100,
    )


def valid_workspace_intent(**overrides: object) -> dict[str, object]:
    return {
        "schema_version": "custombuild.workspace-intent.v1",
        "bay_sizing_mode": "count",
        "target_bay_width_mm": 300,
        "symmetry_locked": True,
        "production_context": valid_production_context(),
        "part_overrides": {},
        "removed_part_ids": [],
        **overrides,
    }


def valid_workshop_readiness(*, legacy: bool = False) -> dict[str, Any]:
    payload = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=True,
        operation_count=36,
        setup_count=2,
        validation_backplot=True,
        validation_program=True,
        edge_band_selection_required=True,
        material_grain_binding_required=False,
    ).as_dict()
    if legacy:
        payload["schema_version"] = LEGACY_WORKSHOP_READINESS_SCHEMA_VERSION
        del payload["release_scope"]
        del payload["machine_use"]
        del payload["edge_band_selection_required"]
    return payload


def valid_readiness_result(*, legacy: bool = False) -> dict[str, Any]:
    return {
        "authoritative_geometry": True,
        "dfm_status": "PASS",
        "workshop_readiness": valid_workshop_readiness(legacy=legacy),
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }


def valid_dfm_report(
    *,
    status: str = "PASS",
    stock_blocked: bool = False,
) -> dict[str, Any]:
    warning_issue = DFMIssue(
        "DFM-GRAIN-001",
        Severity.WARNING,
        "Material grain requires review.",
        part_id="example",
        inputs={"grain_direction": "UNVERIFIED"},
        suggestion="Verify the material batch grain direction.",
    )
    report = DFMReport(
        (
            DFMIssue(
                "STOCK_PROFILE_MISSING",
                Severity.BLOCK,
                "No selected stock profile matches the part.",
                part_id="example",
                inputs={"blank_um": (1_000_000, 400_000)},
                suggestion="Select an exact stock profile.",
            ),
        )
        if stock_blocked
        else (warning_issue,)
        if status == "WARNING"
        else (),
        engine_version=DFM_ENGINE_VERSION,
    )
    return json.loads(canonical_json_bytes(report))


_UNSAFE_GENERATION_CLAIM_PARAMS = (
    pytest.param("authoritative_geometry", None, True, id="authoritative-missing"),
    pytest.param("authoritative_geometry", False, False, id="authoritative-false"),
    pytest.param("authoritative_geometry", 1, False, id="authoritative-int"),
    pytest.param("authoritative_geometry", "true", False, id="authoritative-string"),
    pytest.param("dfm_status", None, True, id="dfm-missing"),
    pytest.param("dfm_status", 1, False, id="dfm-int"),
    pytest.param("dfm_status", [], False, id="dfm-list"),
    pytest.param("dfm_status", {}, False, id="dfm-dict"),
    pytest.param("dfm_status", "UNKNOWN", False, id="dfm-unknown"),
    pytest.param("dfm_status", "BLOCK", False, id="dfm-block"),
)


def _with_unsafe_generation_claim(
    result_json: dict[str, Any],
    field: str,
    value: object,
    remove: bool,
) -> dict[str, Any]:
    malformed = deepcopy(result_json)
    if remove:
        malformed.pop(field, None)
    else:
        malformed[field] = deepcopy(value)
    return malformed


def valid_legacy_workspace_spec(**overrides: object) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "design_id": "legacy-workspace-design",
        "revision": 0,
        "furniture_type": "bookcase",
        "width_mm": 700,
        "height_mm": 1000,
        "depth_mm": 320,
        "material_id": "mdf",
        "material_name": "MDF",
        "nominal_thickness_mm": 18,
        "measured_thickness_mm": 18,
        "shelf_count": 2,
        "fixed_shelves": True,
        "load_per_shelf_kg": 10,
        "back_panel": True,
        "plinth": True,
        "divider_count": 0,
        "bay_sizing_mode": "count",
        "target_bay_width_mm": 300,
        "bay_width_ratios": [],
        "shelf_height_ratios": [],
        "symmetry_locked": True,
        "part_overrides": {},
        "removed_part_ids": [],
        "base_cabinet_height_mm": 0,
        "base_cabinet_depth_mm": 0,
        "base_cabinet_count": 0,
        "reinforcement_mode": "manual",
        "joint_system": "dado",
        "edge_band_mm": 1,
        "wall_anchor_verified": False,
        **valid_production_context(),
        **overrides,
    }


def version_payload(
    project_id: str,
    spec: dict[str, object] | None = None,
    *,
    expected_current_revision: int = 0,
    production_context: dict[str, object] | None = None,
    source_provenance: dict[str, object] | None = None,
    template_id: str | None = None,
) -> dict[str, object]:
    requested_spec = spec or valid_spec()
    _, resolved, _ = canonical_preview(requested_spec, design_id=project_id)
    payload: dict[str, object] = {
        "template_id": template_id
        or (
            "wall-library" if requested_spec.get("furniture_type") == "wall_library" else "shelving"
        ),
        "spec": requested_spec,
        "production_context": production_context or valid_production_context(),
        "expected_design_hash": resolved.design_hash,
        "expected_current_revision": expected_current_revision,
    }
    if source_provenance is not None:
        payload["source_provenance"] = source_provenance
    return payload


def test_template_capabilities_are_server_owned_and_concepts_are_blocked() -> None:
    with TestClient(app) as client:
        capabilities = client.get("/v1/capabilities/templates", headers=HEADERS)
        assert capabilities.status_code == 200
        catalog = {item["template_id"]: item for item in capabilities.json()["templates"]}
        assert catalog["shelving"]["production_level"] == "screened"
        assert catalog["wall-library"]["production_level"] == "concept"
        assert catalog["sideboard"]["production_level"] == "concept"

        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Concept API bypass guard"}
        ).json()
        response = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"], template_id="sideboard"),
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "TEMPLATE_CAPABILITY_BLOCKED"
    assert response.json()["detail"]["solution"]


def test_wall_library_may_preview_but_cannot_create_a_server_revision() -> None:
    wall_spec = valid_spec() | {
        "furniture_type": "wall_library",
        "base_cabinet_height_mm": 400,
        "base_cabinet_depth_mm": 320,
        "base_cabinet_count": 2,
    }
    with TestClient(app) as client:
        preview_response = client.post(
            "/v1/designs/preview",
            headers=HEADERS,
            json=wall_spec,
        )
        assert preview_response.status_code == 200
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Wall library concept boundary"},
        ).json()
        revision = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(
                project["id"],
                spec=wall_spec,
                template_id="wall-library",
            ),
        )

    assert revision.status_code == 409
    assert revision.json()["detail"]["code"] == "TEMPLATE_CAPABILITY_BLOCKED"


def test_external_evidence_is_server_hashed_and_bound_to_exact_design(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: list[tuple[str, bytes, str, str]] = []
    monkeypatch.setattr(
        api_module,
        "store_evidence_object",
        lambda key, content, content_type, sha256: stored.append(
            (key, content, content_type, sha256)
        ),
    )
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Bound external evidence"}
        ).json()
        draft = client.put(
            f"/v1/projects/{project['id']}/draft",
            headers=HEADERS,
            json={
                "expected_draft_revision": 0,
                "template_id": "shelving",
                "spec": valid_spec(),
                "workspace_spec": valid_workspace_intent(),
            },
        ).json()
        document = b"\x89PNG\r\n\x1a\nserver-hashes-this-evidence"
        uploaded = client.post(
            f"/v1/projects/{project['id']}/evidence",
            headers=HEADERS,
            data={
                "evidence_type": "wall_anchor",
                "rule_id": "CB-TIP-001",
                "catalog_id": "anchor-system-approved",
                "catalog_version": "2026.1",
                "design_hash": draft["design_hash"],
            },
            files={"document": ("anchor.png", document, "image/png")},
        )
        wrong_type = client.post(
            f"/v1/projects/{project['id']}/evidence",
            headers=HEADERS,
            data={
                "evidence_type": "hardware",
                "rule_id": "CB-TIP-001",
                "catalog_id": "wrong-type",
                "catalog_version": "1",
                "design_hash": draft["design_hash"],
            },
            files={"document": ("anchor.png", document, "image/png")},
        )

    assert uploaded.status_code == 201
    assert uploaded.json()["sha256"] == hashlib.sha256(document).hexdigest()
    assert uploaded.json()["design_hash"] == draft["design_hash"]
    assert stored[0][3] == uploaded.json()["sha256"]
    assert wrong_type.status_code == 422
    assert wrong_type.json()["detail"]["code"] == "EXTERNAL_EVIDENCE_TYPE_MISMATCH"


def test_signed_retention_preview_create_and_revocation_are_bound_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    now = datetime.now(UTC)
    expiry = now + timedelta(days=365)
    registry = {
        "schema_version": TRUST_REGISTRY_SCHEMA_VERSION,
        "issuers": [
            {
                "issuer_id": "retention-lab",
                "key_id": "retention-lab-2026",
                "role": "joint_retention_certifier",
                "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                "not_before": (now - timedelta(days=1)).isoformat(),
                "not_after": (now + timedelta(days=730)).isoformat(),
                "revoked_at": None,
            }
        ],
        "revoked_statement_sha256": [],
        "revoked_system_versions": [],
    }
    monkeypatch.setattr(
        get_settings(),
        "joint_retention_trust_registry_json",
        json.dumps(registry, separators=(",", ":"), sort_keys=True),
    )
    monkeypatch.setattr(api_module, "store_evidence_object", lambda *_args: None)
    spec = valid_spec()

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Signed retention trust binding"},
        ).json()
        certification_preview = client.post(
            "/v1/designs/preview",
            headers=HEADERS,
            params={"project_id": project["id"]},
            json=spec,
        )
        assert certification_preview.status_code == 200
        request = certification_preview.json()["retention_certification_request"]
        assert request["eligible_for_current_binding"] is True
        assert request["application_class"] == "load_bearing_carcass_dado"
        assert request["excluded_applications"] == [
            {
                "application_class": "captive_inset_back_groove",
                "joint_count": 4,
                "retention_basis": "canonical_four_boundary_geometric_capture",
                "capture_proven": True,
            }
        ]
        assert request["joint_geometry_fingerprint_schema"] == (JOINT_GEOMETRY_FINGERPRINT_SCHEMA)
        draft = client.put(
            f"/v1/projects/{project['id']}/draft",
            headers=HEADERS,
            json={
                "expected_draft_revision": 0,
                "template_id": "shelving",
                "spec": spec,
                "workspace_spec": valid_workspace_intent(),
            },
        )
        assert draft.status_code == 200
        assert draft.json()["design_hash"] == request["source_design_hash"]

        report_bytes = b"independent exact-geometry retention test report"
        instruction_bytes = b"exact mechanical retention installation instruction"

        def document(document_id: str, content: bytes) -> dict[str, str]:
            return {
                "document_id": document_id,
                "document_version": "1.0.0",
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }

        safety_factor = int(request["minimum_safety_factor_permille"])
        load_cases = [
            {
                "mode": item["mode"],
                "rated_design_load_n": item["rated_design_load_n"],
                "verified_capacity_n": (int(item["rated_design_load_n"]) * safety_factor + 999)
                // 1_000,
            }
            for item in request["required_load_cases"]
        ]
        statement: dict[str, Any] = {
            "schema_version": SIGNED_EVIDENCE_SCHEMA_VERSION,
            "evidence_id": "retention-lab-report-2026-001",
            "issuer_id": "retention-lab",
            "key_id": "retention-lab-2026",
                "issued_at": now.isoformat(),
                "expires_at": expiry.isoformat(),
                "application_class": request["application_class"],
                "joint_geometry_fingerprint_schema": request["joint_geometry_fingerprint_schema"],
            "engine_version": request["engine_version"],
            "template_version": request["template_version"],
            "joint_geometry_sha256": request["joint_geometry_sha256"],
            "catalogue_entry": {
                "system_id": "mechanical-dado-lock",
                    "system_version": "1.0.0",
                    "joint_type": "dado",
                    "application_class": request["application_class"],
                    "method": "mechanical",
                "machining_scope": "no_additional_cnc",
                "hardware_sku": "SCREW-4X40-001",
                "hardware_count_per_joint": 2,
                "applicable_materials": [
                    {
                        "material_id": item["material_id"],
                        "material_version": item["material_version"],
                    }
                    for item in request["required_materials"]
                ],
                "minimum_applicable_thickness_um": min(
                    int(item["actual_thickness_um"]) for item in request["required_materials"]
                ),
                "maximum_applicable_thickness_um": max(
                    int(item["actual_thickness_um"]) for item in request["required_materials"]
                ),
                "load_cases": load_cases,
                "safety_factor_permille": safety_factor,
            },
            "test_report": document("retention-report-001", report_bytes),
            "installation_instruction": document(
                "retention-installation-001",
                instruction_bytes,
            ),
        }
        statement["signature_base64"] = base64.b64encode(
            private_key.sign(canonical_json_bytes(statement))
        ).decode("ascii")
        evidence_bytes = canonical_json_bytes(statement)
        monkeypatch.setattr(
            api_module,
            "read_verified_stored_object",
            lambda _expectation, *, max_bytes: evidence_bytes,
        )
        uploaded = client.post(
            f"/v1/projects/{project['id']}/evidence",
            headers=HEADERS,
            data={
                "evidence_type": "joint_retention",
                "rule_id": "CB-JOINT-001",
                "catalog_id": "mechanical-dado-lock",
                "catalog_version": "1.0.0",
                "design_hash": request["source_design_hash"],
                "expires_at": expiry.isoformat(),
            },
            files={
                "document": (
                    "signed-retention.json",
                    evidence_bytes,
                    "application/json",
                )
            },
        )
        assert uploaded.status_code == 201, uploaded.text

        bound_preview = client.post(
            "/v1/designs/preview",
            headers=HEADERS,
            params={
                "project_id": project["id"],
                "joint_retention_evidence_id": uploaded.json()["id"],
            },
            json=spec,
        )
        assert bound_preview.status_code == 200, bound_preview.text
        bound = bound_preview.json()
        payload = version_payload(project["id"], spec=spec)
        payload["expected_design_hash"] = bound["design_hash"]
        payload["joint_retention_evidence_id"] = uploaded.json()["id"]
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=payload,
        )
        assert version.status_code == 201, version.text
        assert version.json()["design_hash"] == bound["design_hash"]
        assert version.json()["spec_json"]["joint_retention"]["evidence_id"] == (
            "retention-lab-report-2026-001"
        )
        assert (
            version.json()["result_json"]["retention_trust"]["storage_evidence_id"]
            == uploaded.json()["id"]
        )
        base = f"/v1/projects/{project['id']}/versions/{version.json()['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200

        with get_session_factory()() as session:
            evidence = session.get(ExternalEvidence, uploaded.json()["id"])
            assert evidence is not None
            evidence.revoked_at = datetime.now(UTC)
            session.commit()

        rejected = client.post(f"{base}/validate", headers=HEADERS)

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] in {
        "EXTERNAL_EVIDENCE_STALE",
        "JOINT_RETENTION_BINDING_STALE",
    }


def _create_surface_back_signed_retention_revision(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Create one public-API revision with narrow carcass evidence and an unresolved back."""

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    now = datetime.now(UTC)
    expiry = now + timedelta(days=365)
    registry = {
        "schema_version": TRUST_REGISTRY_SCHEMA_VERSION,
        "issuers": [
            {
                "issuer_id": "surface-back-retention-lab",
                "key_id": "surface-back-retention-lab-2026",
                "role": "joint_retention_certifier",
                "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                "not_before": (now - timedelta(days=1)).isoformat(),
                "not_after": (now + timedelta(days=730)).isoformat(),
                "revoked_at": None,
            }
        ],
        "revoked_statement_sha256": [],
        "revoked_system_versions": [],
    }
    monkeypatch.setattr(
        get_settings(),
        "joint_retention_trust_registry_json",
        json.dumps(registry, separators=(",", ":"), sort_keys=True),
    )
    monkeypatch.setattr(api_module, "store_evidence_object", lambda *_args: None)
    spec = valid_spec() | {"back_panel": "surface_mounted"}
    project = client.post(
        "/v1/projects",
        headers=HEADERS,
        json={"name": "Surface-back retention application split"},
    ).json()
    certification_preview = client.post(
        "/v1/designs/preview",
        headers=HEADERS,
        params={"project_id": project["id"]},
        json=spec,
    )
    assert certification_preview.status_code == 200, certification_preview.text
    request = certification_preview.json()["retention_certification_request"]
    assert request["eligible_for_current_binding"] is True
    assert request["excluded_applications"] == [
        {
            "application_class": "surface_mounted_back",
            "joint_count": 4,
            "retention_basis": "independent_authenticated_evidence_required",
            "capture_proven": False,
        }
    ]
    draft = client.put(
        f"/v1/projects/{project['id']}/draft",
        headers=HEADERS,
        json={
            "expected_draft_revision": 0,
            "template_id": "shelving",
            "spec": spec,
            "workspace_spec": valid_workspace_intent(),
        },
    )
    assert draft.status_code == 200, draft.text
    assert draft.json()["design_hash"] == request["source_design_hash"]

    def embedded_document(document_id: str, content: bytes) -> dict[str, str]:
        return {
            "document_id": document_id,
            "document_version": "1.0.0",
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_base64": base64.b64encode(content).decode("ascii"),
        }

    safety_factor = int(request["minimum_safety_factor_permille"])
    statement: dict[str, Any] = {
        "schema_version": SIGNED_EVIDENCE_SCHEMA_VERSION,
        "evidence_id": "surface-back-carcass-report-2026-001",
        "issuer_id": "surface-back-retention-lab",
        "key_id": "surface-back-retention-lab-2026",
        "issued_at": now.isoformat(),
        "expires_at": expiry.isoformat(),
        "application_class": request["application_class"],
        "joint_geometry_fingerprint_schema": request[
            "joint_geometry_fingerprint_schema"
        ],
        "engine_version": request["engine_version"],
        "template_version": request["template_version"],
        "joint_geometry_sha256": request["joint_geometry_sha256"],
        "catalogue_entry": {
            "system_id": "surface-back-carcass-lock",
            "system_version": "1.0.0",
            "joint_type": "dado",
            "application_class": request["application_class"],
            "method": "mechanical",
            "machining_scope": "no_additional_cnc",
            "hardware_sku": "SCREW-4X40-SURFACE-BACK-001",
            "hardware_count_per_joint": 2,
            "applicable_materials": [
                {
                    "material_id": item["material_id"],
                    "material_version": item["material_version"],
                }
                for item in request["required_materials"]
            ],
            "minimum_applicable_thickness_um": min(
                int(item["actual_thickness_um"])
                for item in request["required_materials"]
            ),
            "maximum_applicable_thickness_um": max(
                int(item["actual_thickness_um"])
                for item in request["required_materials"]
            ),
            "load_cases": [
                {
                    "mode": item["mode"],
                    "rated_design_load_n": item["rated_design_load_n"],
                    "verified_capacity_n": (
                        int(item["rated_design_load_n"]) * safety_factor + 999
                    )
                    // 1_000,
                }
                for item in request["required_load_cases"]
            ],
            "safety_factor_permille": safety_factor,
        },
        "test_report": embedded_document(
            "surface-back-carcass-report",
            b"independent carcass retention report for exact surface-back design",
        ),
        "installation_instruction": embedded_document(
            "surface-back-carcass-instruction",
            b"carcass-only mechanical retention instruction; excludes back panel",
        ),
    }
    statement["signature_base64"] = base64.b64encode(
        private_key.sign(canonical_json_bytes(statement))
    ).decode("ascii")
    evidence_bytes = canonical_json_bytes(statement)
    monkeypatch.setattr(
        api_module,
        "read_verified_stored_object",
        lambda _expectation, *, max_bytes: evidence_bytes,
    )
    uploaded = client.post(
        f"/v1/projects/{project['id']}/evidence",
        headers=HEADERS,
        data={
            "evidence_type": "joint_retention",
            "rule_id": "CB-JOINT-001",
            "catalog_id": "surface-back-carcass-lock",
            "catalog_version": "1.0.0",
            "design_hash": request["source_design_hash"],
            "expires_at": expiry.isoformat(),
        },
        files={
            "document": (
                "surface-back-signed-retention.json",
                evidence_bytes,
                "application/json",
            )
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    bound_preview = client.post(
        "/v1/designs/preview",
        headers=HEADERS,
        params={
            "project_id": project["id"],
            "joint_retention_evidence_id": uploaded.json()["id"],
        },
        json=spec,
    )
    assert bound_preview.status_code == 200, bound_preview.text
    payload = version_payload(project["id"], spec=spec)
    payload["expected_design_hash"] = bound_preview.json()["design_hash"]
    payload["joint_retention_evidence_id"] = uploaded.json()["id"]
    version = client.post(
        f"/v1/projects/{project['id']}/versions",
        headers=HEADERS,
        json=payload,
    )
    assert version.status_code == 201, version.text
    with get_session_factory()() as session:
        evidence = session.get(ExternalEvidence, uploaded.json()["id"])
        assert evidence is not None
        evidence_object_key = evidence.object_key
    return SimpleNamespace(
        project=project,
        version=version.json(),
        base=(
            f"/v1/projects/{project['id']}/versions/"
            f"{version.json()['revision']}"
        ),
        evidence_bytes=evidence_bytes,
        evidence_object_key=evidence_object_key,
    )


def test_surface_back_blocked_review_package_is_downloadable_but_never_cam_approvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        fixture = _create_surface_back_signed_retention_revision(client, monkeypatch)
        assert client.post(f"{fixture.base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, fixture.base)
        generated = client.post(
            f"{fixture.base}/generate",
            headers=HEADERS,
            json=valid_production_context(),
        )
        assert generated.status_code == 202, generated.text
        documents = _complete_blocked_cam_generation(
            generated.json()["id"],
            blocker_code=BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
        )
        documents[fixture.evidence_object_key] = fixture.evidence_bytes
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        listing = client.get(
            f"/v1/jobs/{generated.json()['id']}/artifacts",
            headers=HEADERS,
        )
        assert listing.status_code == 200, listing.text
        cam = client.post(
            f"{fixture.base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Surface-back retention remains unresolved",
                "generation_job_id": generated.json()["id"],
            },
        )
        release = client.post(
            f"{fixture.base}/release",
            headers=HEADERS,
            json={"release_number": "SURFACE-BACK-BLOCKED", "confirmation": "RELEASE"},
        )

    assert cam.status_code == release.status_code == 409


def test_back_panel_blocker_must_match_the_frozen_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Forged back-panel blocker"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        documents = _complete_blocked_cam_generation(
            generated["id"],
            blocker_code=BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409


def test_generation_rejects_duplicate_evidence_type_before_creating_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_module, "store_evidence_object", lambda *_args: None)
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Duplicate external evidence type guard"},
        ).json()
        draft = client.put(
            f"/v1/projects/{project['id']}/draft",
            headers=HEADERS,
            json={
                "expected_draft_revision": 0,
                "template_id": "shelving",
                "spec": valid_spec(),
                "workspace_spec": valid_workspace_intent(),
            },
        ).json()
        uploaded_ids = []
        for index in (1, 2):
            uploaded = client.post(
                f"/v1/projects/{project['id']}/evidence",
                headers=HEADERS,
                data={
                    "evidence_type": "wall_anchor",
                    "rule_id": "CB-TIP-001",
                    "catalog_id": f"anchor-system-{index}",
                    "catalog_version": "2026.1",
                    "design_hash": draft["design_hash"],
                },
                files={
                    "document": (
                        f"anchor-{index}.png",
                        b"\x89PNG\r\n\x1a\nchecked-anchor-" + str(index).encode(),
                        "image/png",
                    )
                },
            )
            assert uploaded.status_code == 201
            uploaded_ids.append(uploaded.json()["id"])

        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        assert version["design_hash"] == draft["design_hash"]
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)

        rejected = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json=valid_production_context() | {"external_evidence_ids": uploaded_ids},
        )

    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {
        "code": "EXTERNAL_EVIDENCE_TYPE_DUPLICATE",
        "message": "Multiple selected evidence records claim the same evidence type.",
        "solution": "Select exactly one current evidence record per evidence type.",
        "evidence_types": ["wall_anchor"],
    }
    with get_session_factory()() as session:
        jobs = list(
            session.scalars(
                select(GenerationJob).where(GenerationJob.design_version_id == version["id"])
            )
        )
        approvals = list(
            session.scalars(select(Approval).where(Approval.design_version_id == version["id"]))
        )
        persisted_version = session.get(DesignVersion, version["id"])

    assert jobs == []
    assert len(approvals) == 1
    assert approvals[0].approval_type == "design"
    assert persisted_version is not None
    assert persisted_version.status == DesignStatus.design_validated


def test_revoked_external_evidence_fails_closed_before_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_module, "store_evidence_object", lambda *_args: None)
    warning_spec = valid_spec() | {"height_mm": 2000, "depth_mm": 320}
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Revoked evidence guard"}
        ).json()
        draft = client.put(
            f"/v1/projects/{project['id']}/draft",
            headers=HEADERS,
            json={
                "expected_draft_revision": 0,
                "template_id": "shelving",
                "spec": warning_spec,
                "workspace_spec": valid_workspace_intent(),
            },
        ).json()
        uploaded = client.post(
            f"/v1/projects/{project['id']}/evidence",
            headers=HEADERS,
            data={
                "evidence_type": "wall_anchor",
                "rule_id": "CB-TIP-001",
                "catalog_id": "anchor-system-approved",
                "catalog_version": "2026.1",
                "design_hash": draft["design_hash"],
            },
            files={
                "document": (
                    "anchor.png",
                    b"\x89PNG\r\n\x1a\nchecked-anchor",
                    "image/png",
                )
            },
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"], warning_spec),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        warnings = [
            item
            for item in version["result_json"]["rule_evaluations"]
            if item["status"] == "WARNING"
        ]
        assert any(item["rule_id"] == "CB-TIP-001" for item in warnings)
        approved = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "design",
                "reason": "Server-bound evidence reviewed",
                "warning_overrides": [
                    {
                        "rule_id": item["rule_id"],
                        "reason": "External control reviewed for this exact revision",
                        "evidence_ids": (
                            [uploaded["id"]] if item["rule_id"] == "CB-TIP-001" else []
                        ),
                    }
                    for item in warnings
                ],
            },
        )
        assert approved.status_code == 200
        state = client.get(f"/v1/projects/{project['id']}/production-state", headers=HEADERS).json()
        tip_override = next(
            item
            for item in state["approvals"][0]["overrides_json"]
            if item["rule_id"] == "CB-TIP-001"
        )
        assert tip_override["evidence_status"] == "verified"
        assert tip_override["external_evidence"][0]["sha256"] == uploaded["sha256"]

        with get_session_factory().begin() as session:
            record = session.get(ExternalEvidence, uploaded["id"])
            assert record is not None
            record.revoked_at = datetime.now(UTC)

        generated = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json=valid_production_context(),
        )

    assert generated.status_code == 409
    assert generated.json()["detail"]["code"] == "EXTERNAL_EVIDENCE_STALE"
    assert generated.json()["detail"]["solution"]


def design_approval_payload(
    client: TestClient,
    base: str,
    *,
    reason: str = "Design review completed",
) -> dict[str, object]:
    version = client.get(base, headers=HEADERS)
    assert version.status_code == 200
    warnings = [
        item
        for item in version.json()["result_json"]["rule_evaluations"]
        if item["status"] == "WARNING"
    ]
    return {
        "approval_type": "design",
        "reason": reason,
        "warning_overrides": [
            {
                "rule_id": item["rule_id"],
                "reason": "Current server warning reviewed for this exact revision",
            }
            for item in warnings
        ],
    }


def approve_design(client: TestClient, base: str) -> None:
    response = client.post(
        f"{base}/approve",
        headers=HEADERS,
        json=design_approval_payload(client, base),
    )
    assert response.status_code == 200


def _enable_production_four_eyes(monkeypatch: pytest.MonkeyPatch) -> None:
    configured = get_settings().model_copy(update={"production_four_eyes_required": True})
    monkeypatch.setattr(api_module, "get_settings", lambda: configured)


def _provision_four_eyes_reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    with get_session_factory().begin() as session:
        if session.get(User, FOUR_EYES_REVIEWER_ID) is None:
            session.add(
                User(
                    id=FOUR_EYES_REVIEWER_ID,
                    oidc_sub="demo:nordic-four-eyes-reviewer",
                    email="four-eyes-reviewer@nordic.example",
                    name="Nordic Four Eyes Reviewer",
                )
            )
            session.flush()
        membership = session.scalar(
            select(Membership).where(
                Membership.organization_id == DEV_ORG_NORDIC,
                Membership.user_id == FOUR_EYES_REVIEWER_ID,
            )
        )
        if membership is None:
            session.add(
                Membership(
                    organization_id=DEV_ORG_NORDIC,
                    user_id=FOUR_EYES_REVIEWER_ID,
                    role=Role.reviewer,
                )
            )
    monkeypatch.setitem(
        auth_module._DEV_TOKENS,
        "demo-nordic-four-eyes-reviewer",
        auth_module.Principal(
            user_id=FOUR_EYES_REVIEWER_ID,
            organization_id=DEV_ORG_NORDIC,
            role=Role.reviewer,
            subject="demo:nordic-four-eyes-reviewer",
            email="four-eyes-reviewer@nordic.example",
            name="Nordic Four Eyes Reviewer",
        ),
    )


REFERENCE_IMAGE_BYTES = b"\x89PNG\r\n\x1a\nimmutable-reference-image"


def upload_reference(
    client: TestClient,
    project_id: str,
    *,
    filename: str = "verified-library.png",
) -> dict[str, object]:
    response = client.post(
        f"/v1/projects/{project_id}/imports/inspect",
        headers=HEADERS,
        files={"document": (filename, REFERENCE_IMAGE_BYTES, "image/png")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == project_id
    assert payload["image_sha256"] == hashlib.sha256(REFERENCE_IMAGE_BYTES).hexdigest()
    assert "object_key" not in payload
    return payload


@pytest.mark.parametrize("operation", ("reference", "evidence"))
def test_upload_storage_io_does_not_block_the_application_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    storage_started = Event()
    release_storage = Event()
    upload_responses: list[Any] = []
    health_responses: list[Any] = []

    def blocking_store(*_args: object) -> None:
        storage_started.set()
        assert release_storage.wait(timeout=10)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": f"Non-blocking {operation} storage"},
        ).json()
        if operation == "reference":
            monkeypatch.setattr(api_module, "store_immutable_object", blocking_store)

            def upload() -> None:
                upload_responses.append(
                    client.post(
                        f"/v1/projects/{project['id']}/imports/inspect",
                        headers=HEADERS,
                        files={
                            "document": (
                                "non-blocking.png",
                                REFERENCE_IMAGE_BYTES,
                                "image/png",
                            )
                        },
                    )
                )

            expected_status = 200
        else:
            version = client.post(
                f"/v1/projects/{project['id']}/versions",
                headers=HEADERS,
                json=version_payload(project["id"]),
            ).json()
            monkeypatch.setattr(api_module, "store_evidence_object", blocking_store)

            def upload() -> None:
                upload_responses.append(
                    client.post(
                        f"/v1/projects/{project['id']}/evidence",
                        headers=HEADERS,
                        data={
                            "evidence_type": "hardware",
                            "rule_id": "CB-HARDWARE-001",
                            "catalog_id": "non-blocking-hardware",
                            "catalog_version": "2026.1",
                            "design_hash": version["design_hash"],
                        },
                        files={
                            "document": (
                                "non-blocking.png",
                                REFERENCE_IMAGE_BYTES,
                                "image/png",
                            )
                        },
                    )
                )

            expected_status = 201

        upload_thread = Thread(target=upload)
        upload_thread.start()
        assert storage_started.wait(timeout=5)

        health_complete = Event()

        def check_health() -> None:
            health_responses.append(client.get("/health"))
            health_complete.set()

        health_thread = Thread(target=check_health)
        health_thread.start()
        try:
            assert health_complete.wait(timeout=2), "provider I/O blocked the event loop"
        finally:
            release_storage.set()
            health_thread.join(timeout=5)
            upload_thread.join(timeout=5)

    assert len(health_responses) == 1
    assert health_responses[0].status_code == 200
    assert len(upload_responses) == 1
    assert upload_responses[0].status_code == expected_status


def verified_reference_provenance(
    imported: dict[str, object],
    model_fingerprint: str,
) -> dict[str, object]:
    return {
        "source": "reference_image",
        "import_id": imported["import_id"],
        "image_sha256": imported["image_sha256"],
        "file_name": "verified-library.png",
        "image_width_px": 1600,
        "image_height_px": 1000,
        "confidence": 0.82,
        "detected_shelves": 4,
        "detected_dividers": 2,
        "detected_base_cabinets": True,
        "warnings": ["Djupet verifierades med verkligt mått."],
        "verification_status": "parametric_confirmed",
        "confirmed_inputs": {
            "dimensions_measured": True,
            "layout_confirmed": True,
            "material_confirmed": True,
            "construction_assumptions_confirmed": True,
        },
        "verified_model_fingerprint": model_fingerprint,
    }


def test_preview_is_reproducible() -> None:
    with TestClient(app) as client:
        first = client.post("/v1/designs/preview", headers=HEADERS, json=valid_spec())
        second = client.post("/v1/designs/preview", headers=HEADERS, json=valid_spec())
        assert first.status_code == second.status_code == 200
        assert first.json()["design_hash"] == second.json()["design_hash"]
        assert first.json()["parts"] == second.json()["parts"]
        assert first.json()["status"] == "WARNING"
        assert [
            item["rule_id"]
            for item in first.json()["rule_evaluations"]
            if item["status"] == "WARNING"
        ] == ["CB-JOINT-001"]


def test_server_preview_projects_grain_control_only_for_directional_material() -> None:
    with TestClient(app) as client:
        directional = client.post(
            "/v1/designs/preview",
            headers=HEADERS,
            json=valid_spec() | {"material_id": "birch-plywood"},
        )
        non_directional = client.post(
            "/v1/designs/preview",
            headers=HEADERS,
            json=valid_spec() | {"material_id": "mdf"},
        )

    assert directional.status_code == non_directional.status_code == 200
    directional_payload = directional.json()
    grain = next(
        item
        for item in directional_payload["rule_evaluations"]
        if item["rule_id"] == DFM_GRAIN_BLOCKER_CODE
    )
    assert grain["rule_version"] == "1.0.0"
    assert grain["status"] == "WARNING"
    assert grain["suggested_actions"] == []
    assert grain["manufacturing_control"] == directional_payload["manufacturing_controls"][0]
    assert grain["manufacturing_control"]["status"] == "WARNING"
    assert grain["manufacturing_control"]["issues"]
    assert {issue["binding_status"] for issue in grain["manufacturing_control"]["issues"]} == {
        "MISSING_INFORMATION"
    }
    assert {issue["assessment_phase"] for issue in grain["manufacturing_control"]["issues"]} == {
        "STOCK_SELECTION_INCOMPLETE"
    }
    assert all(issue["stock_id"] is None for issue in grain["manufacturing_control"]["issues"])
    assert non_directional.json()["manufacturing_controls"] == []
    assert not any(
        item["rule_id"] == DFM_GRAIN_BLOCKER_CODE
        for item in non_directional.json()["rule_evaluations"]
    )


def test_frozen_grain_contract_fails_closed_for_non_mapping_json() -> None:
    malformed = SimpleNamespace(spec_json=[], result_json=[])

    assert api_module._frozen_grain_contract(malformed) is None


def test_rule_engine_failure_blocks_preview_and_revision_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Fail closed rule engine"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()

        def missing(_name: str) -> object:
            raise ImportError("simulated rule-engine loss")

        monkeypatch.setattr(design_service_module, "import_module", missing)
        preview_response = client.post("/v1/designs/preview", headers=HEADERS, json=valid_spec())
        validation_response = client.post(
            f"/v1/projects/{project['id']}/versions/{version['revision']}/validate",
            headers=HEADERS,
        )

    assert preview_response.status_code == 503
    assert preview_response.json()["detail"]["code"] == "RULE_ENGINE_UNAVAILABLE"
    assert preview_response.json()["detail"]["solution"]
    assert validation_response.status_code == 503
    assert validation_response.json()["detail"]["code"] == "RULE_ENGINE_UNAVAILABLE"


def test_autofix_supports_a_4200_mm_loaded_shelf_row() -> None:
    payload = valid_spec() | {
        "width_mm": 4200,
        "height_mm": 2100,
        "material_id": "birch-plywood",
        "measured_thickness_mm": 17.8,
        "shelf_count": 5,
        "load_per_shelf_kg": 32,
        "divider_count": 0,
        "reinforcement_mode": "auto",
    }

    with TestClient(app) as client:
        response = client.post("/v1/designs/autofix", headers=HEADERS, json=payload)

    assert response.status_code == 200
    result = response.json()
    divider_count = result["spec"]["parameters"]["vertical_divider_count"]
    rules = {item["rule_id"]: item for item in result["rule_evaluations"]}
    assert divider_count >= 3
    assert len([part for part in result["parts"] if part["kind"] == "divider"]) == divider_count
    assert len([part for part in result["parts"] if part["kind"] == "shelf"]) == 5 * (
        divider_count + 1
    )
    assert rules["CB-DEFLECTION-001"]["status"] == "PASS"
    assert rules["CB-BENDING-001"]["status"] == "PASS"
    assert rules["CB-JOINT-001"]["status"] == "WARNING"


def test_autofix_returns_an_actionable_json_error_for_unmanufacturable_layout() -> None:
    payload = valid_spec() | {
        "width_mm": 777,
        "height_mm": 1000,
        "shelf_count": 2,
        "divider_count": 16,
        "reinforcement_mode": "auto",
    }

    with TestClient(app) as client:
        response = client.post("/v1/designs/autofix", headers=HEADERS, json=payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "DESIGN_INPUT_INVALID"
    assert detail["solution"]
    assert "manufacturable shelf width" in detail["errors"][0]["msg"]
    assert "input" not in detail["errors"][0]
    assert "ctx" not in detail["errors"][0]


def test_wall_library_generates_upper_bays_and_modular_base_parts() -> None:
    payload = valid_spec() | {
        "furniture_type": "wall_library",
        "width_mm": 4200,
        "height_mm": 2600,
        "depth_mm": 320,
        "material_id": "birch-plywood",
        "measured_thickness_mm": 17.8,
        "shelf_count": 5,
        "load_per_shelf_kg": 32,
        "divider_count": 4,
        "base_cabinet_height_mm": 720,
        "base_cabinet_depth_mm": 320,
        "base_cabinet_count": 4,
    }

    with TestClient(app) as client:
        response = client.post("/v1/designs/preview", headers=HEADERS, json=payload)

    assert response.status_code == 200
    result = response.json()
    kinds = [part["kind"] for part in result["parts"]]
    assert kinds.count("shelf") == 25
    assert kinds.count("base_side") == 3
    assert kinds.count("base_bottom") == 4
    assert kinds.count("cabinet_front") == 4
    rules = {item["rule_id"]: item for item in result["rule_evaluations"]}
    assert rules["CB-SUPPORT-001"]["status"] == "BLOCK"
    support_action = rules["CB-SUPPORT-001"]["suggested_actions"][0]
    assert support_action["action_type"] == "align_base_cabinets"
    assert support_action["changes"][0]["after"] == 5
    assert "fullhöjd underskåpssida" in support_action["description"]
    assert rules["CB-HARDWARE-001"]["status"] == "WARNING"


def test_wall_library_rejects_base_depth_outside_the_furniture_envelope() -> None:
    payload = valid_spec() | {
        "furniture_type": "wall_library",
        "width_mm": 2400,
        "height_mm": 2400,
        "depth_mm": 320,
        "divider_count": 2,
        "base_cabinet_height_mm": 680,
        "base_cabinet_depth_mm": 520,
        "base_cabinet_count": 3,
    }

    with TestClient(app) as client:
        response = client.post("/v1/designs/preview", headers=HEADERS, json=payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "DESIGN_INPUT_INVALID"
    assert any(
        "base cabinet depth must equal the furniture depth" in error["msg"]
        for error in detail["errors"]
    )


def test_joint_capability_matrix_and_preview_reject_unverified_systems() -> None:
    with TestClient(app) as client:
        capabilities = client.get("/v1/capabilities/joints", headers=HEADERS)
        assert capabilities.status_code == 200
        payload = capabilities.json()
        assert payload["version"] == "bookcase-joints-1.0.0"
        assert payload["joints"]["dado"]["status"] == "supported"
        assert payload["joints"]["shelf_pin"]["status"] == "conditional"
        assert {
            joint for joint, claim in payload["joints"].items() if claim["status"] == "blocked"
        } == {"dowel", "confirmat", "cam_dowel", "rabbet", "tenon"}

        for unsupported in (
            "dowel",
            "confirmat",
            "cam_dowel",
            "shelf_pin",
            "rabbet",
            "tenon",
        ):
            response = client.post(
                "/v1/designs/preview",
                headers=HEADERS,
                json=valid_spec() | {"joint_system": unsupported},
            )
            assert response.status_code == 422


def test_project_workspace_draft_is_server_authoritative_and_restorable() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Server draft fixture"}
        ).json()
        initial = client.get(f"/v1/projects/{project['id']}/draft", headers=HEADERS)
        assert initial.status_code == 200
        assert initial.json()["draft_revision"] == 0
        assert initial.json()["spec_json"] is None

        workspace_spec = valid_legacy_workspace_spec(
            reference_image_import={
                "source": "reference_image",
                "import_id": "11111111-1111-1111-1111-111111111111",
                "image_sha256": "a" * 64,
                "file_name": "reference.png",
                "image_width_px": 800,
                "image_height_px": 600,
                "confidence": 0.61,
                "detected_shelves": 2,
                "detected_dividers": 0,
                "detected_base_cabinets": False,
                "warnings": [],
            }
        )
        saved = client.put(
            f"/v1/projects/{project['id']}/draft",
            headers=HEADERS,
            json={
                "expected_draft_revision": 0,
                "template_id": "shelving",
                "spec": valid_spec(),
                "workspace_spec": workspace_spec,
            },
        )
        assert saved.status_code == 200
        payload = saved.json()
        assert payload["project_id"] == project["id"]
        assert payload["draft_revision"] == 1
        assert payload["template_id"] == "shelving"
        assert len(payload["design_hash"]) == 64
        assert payload["result_json"]["design_hash"] == payload["design_hash"]
        assert (
            payload["workspace_spec_json"]["reference_image_import"]["file_name"] == "reference.png"
        )
        assert (
            payload["workspace_spec_json"]["reference_image_import"]["verification_status"]
            == "concept"
        )
        assert payload["workspace_spec_json"]["reference_image_import"]["confirmed_inputs"] == {
            "dimensions_measured": False,
            "layout_confirmed": False,
            "material_confirmed": False,
            "construction_assumptions_confirmed": False,
        }
        assert payload["workspace_spec_json"]["schema_version"] == (
            "custombuild.workspace-intent.v1"
        )
        assert "width_mm" not in payload["workspace_spec_json"]
        assert payload["spec_json"] == BookcasePreviewInput.model_validate(valid_spec()).model_dump(
            mode="json", exclude_none=True
        )

        restored = client.get(f"/v1/projects/{project['id']}/draft", headers=HEADERS)
        assert restored.status_code == 200
        assert restored.json()["draft_revision"] == 1
        assert restored.json()["design_hash"] == payload["design_hash"]
        assert restored.json()["workspace_spec_json"] == payload["workspace_spec_json"]


def test_explicit_back_material_roundtrips_preview_draft_and_frozen_revision() -> None:
    requested = valid_spec() | {
        "material_id": "birch-plywood",
        "back_material_id": "mdf-6",
    }

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Explicit back material fixture"},
        ).json()
        preview_response = client.post(
            f"/v1/designs/preview?project_id={project['id']}",
            headers=HEADERS,
            json=requested,
        )
        assert preview_response.status_code == 200
        preview_payload = preview_response.json()
        assert preview_payload["spec"]["material"]["material_id"] == "birch-plywood"
        assert preview_payload["spec"]["back_material"]["material_id"] == "mdf-6"
        assert (
            next(part for part in preview_payload["parts"] if part["kind"] == "back")["material_id"]
            == "mdf-6"
        )

        saved = client.put(
            f"/v1/projects/{project['id']}/draft",
            headers=HEADERS,
            json={
                "expected_draft_revision": 0,
                "template_id": "shelving",
                "spec": requested,
                "workspace_spec": valid_workspace_intent(),
            },
        )
        assert saved.status_code == 200
        saved_payload = saved.json()
        assert saved_payload["spec_json"]["back_material_id"] == "mdf-6"
        assert saved_payload["result_json"]["spec"]["back_material"]["material_id"] == ("mdf-6")
        assert saved_payload["design_hash"] == preview_payload["design_hash"]
        restored_draft = client.get(
            f"/v1/projects/{project['id']}/draft",
            headers=HEADERS,
        )
        assert restored_draft.status_code == 200
        assert restored_draft.json()["spec_json"] == saved_payload["spec_json"]
        assert restored_draft.json()["design_hash"] == preview_payload["design_hash"]

        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"], spec=requested),
        )
        assert version.status_code == 201
        version_payload_json = version.json()
        assert version_payload_json["design_hash"] == preview_payload["design_hash"]
        assert version_payload_json["spec_json"]["back_material"]["material_id"] == ("mdf-6")
        assert version_payload_json["spec_json"]["back_material"]["version"] == ("screening-2026.1")
        assert (
            version_payload_json["result_json"]["spec"]["back_material"]["material_id"] == "mdf-6"
        )
        restored_version = client.get(
            f"/v1/projects/{project['id']}/versions/{version_payload_json['revision']}",
            headers=HEADERS,
        )
        assert restored_version.status_code == 200
        assert restored_version.json()["design_hash"] == version_payload_json["design_hash"]
        assert restored_version.json()["spec_json"] == version_payload_json["spec_json"]
        assert restored_version.json()["result_json"] == version_payload_json["result_json"]


@pytest.mark.parametrize(
    "invalid_back_fields",
    (
        {"back_material_id": "oak-6"},
        {"back_panel": False, "back_material_id": "mdf-6"},
        {"back_panel": "none", "back_material_id": "birch-plywood-6"},
    ),
)
def test_invalid_back_material_never_mutates_a_project_draft(
    invalid_back_fields: dict[str, object],
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": f"Rejected back material fixture {uuid4()}"},
        ).json()
        invalid_spec = valid_spec() | invalid_back_fields

        preview_response = client.post(
            f"/v1/designs/preview?project_id={project['id']}",
            headers=HEADERS,
            json=invalid_spec,
        )
        draft_response = client.put(
            f"/v1/projects/{project['id']}/draft",
            headers=HEADERS,
            json={
                "expected_draft_revision": 0,
                "template_id": "shelving",
                "spec": invalid_spec,
                "workspace_spec": valid_workspace_intent(),
            },
        )
        unchanged = client.get(
            f"/v1/projects/{project['id']}/draft",
            headers=HEADERS,
        )

    assert preview_response.status_code == 422
    assert draft_response.status_code == 422
    assert unchanged.status_code == 200
    assert unchanged.json()["draft_revision"] == 0
    assert unchanged.json()["spec_json"] is None


def test_existing_legacy_workspace_row_remains_directly_readable() -> None:
    legacy_workspace = valid_legacy_workspace_spec()

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Stored legacy draft fixture"}
        ).json()
        with get_session_factory().begin() as session:
            stored_project = session.get(Project, project["id"])
            assert stored_project is not None
            stored_project.draft_revision = 3
            stored_project.draft_template_id = "shelving"
            stored_project.draft_spec_json = valid_spec()
            stored_project.draft_workspace_json = legacy_workspace

        restored = client.get(f"/v1/projects/{project['id']}/draft", headers=HEADERS)

    assert restored.status_code == 200
    assert restored.json()["draft_revision"] == 3
    assert restored.json()["workspace_spec_json"] == legacy_workspace


def test_v1_workspace_roundtrip_is_normalized_and_malicious_writes_are_rejected() -> None:
    intent = valid_workspace_intent(part_overrides={"top": {"depth_mm": 300}})
    expected_intent = WorkspaceIntentV1.model_validate(intent).model_dump(
        mode="json", exclude_none=True
    )
    expected_spec = BookcasePreviewInput.model_validate(valid_spec()).model_dump(
        mode="json", exclude_none=True
    )
    oversized = valid_workspace_intent(_padding="")
    compact_size = len(
        json.dumps(oversized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    oversized["_padding"] = "x" * (128 * 1024 + 1 - compact_size)
    excessive_ids = {f"part-{index}": {"width_mm": 100} for index in range(1_024)}

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Strict workspace fixture"}
        ).json()
        endpoint = f"/v1/projects/{project['id']}/draft"
        common = {
            "expected_draft_revision": 0,
            "template_id": "shelving",
            "spec": valid_spec(),
        }
        malicious = [
            valid_workspace_intent(shelf_count=1_000_000_000),
            valid_legacy_workspace_spec(width_mm=9_999),
            oversized,
            valid_workspace_intent(
                part_overrides=excessive_ids,
                removed_part_ids=["top"],
            ),
        ]
        rejected = [
            client.put(
                endpoint,
                headers=HEADERS,
                json={**common, "workspace_spec": workspace},
            )
            for workspace in malicious
        ]
        saved = client.put(
            endpoint,
            headers=HEADERS,
            json={**common, "workspace_spec": intent},
        )
        restored = client.get(endpoint, headers=HEADERS)

    assert [response.status_code for response in rejected] == [422, 422, 422, 422]
    assert saved.status_code == 200
    assert saved.json()["workspace_spec_json"] == expected_intent
    assert saved.json()["spec_json"] == expected_spec
    assert restored.status_code == 200
    assert restored.json()["workspace_spec_json"] == expected_intent
    assert restored.json()["spec_json"] == expected_spec


def test_two_editors_cannot_silently_overwrite_the_same_project_draft() -> None:
    first_spec = valid_spec() | {"width_mm": 900}
    stale_spec = valid_spec() | {"width_mm": 1200}
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Two editor draft guard"}
        ).json()
        tab_one = client.get(f"/v1/projects/{project['id']}/draft", headers=HEADERS).json()
        tab_two = client.get(f"/v1/projects/{project['id']}/draft", headers=HEADERS).json()
        assert tab_one["draft_revision"] == tab_two["draft_revision"] == 0

        first_save = client.put(
            f"/v1/projects/{project['id']}/draft",
            headers=HEADERS,
            json={
                "expected_draft_revision": tab_one["draft_revision"],
                "template_id": "shelving",
                "spec": first_spec,
                "workspace_spec": valid_workspace_intent(),
            },
        )
        stale_save = client.put(
            f"/v1/projects/{project['id']}/draft",
            headers=HEADERS,
            json={
                "expected_draft_revision": tab_two["draft_revision"],
                "template_id": "shelving",
                "spec": stale_spec,
                "workspace_spec": valid_workspace_intent(),
            },
        )

        assert first_save.status_code == 200
        assert first_save.json()["draft_revision"] == 1
        assert stale_save.status_code == 409
        conflict = stale_save.json()["detail"]
        assert conflict == {
            "code": "DRAFT_REVISION_CONFLICT",
            "message": "The project draft was changed by another editor.",
            "solution": (
                "Reload the latest project draft, review the other editor's changes, "
                "then apply and save your changes again."
            ),
            "expected_draft_revision": 0,
            "current_draft_revision": 1,
        }
        unchanged = client.get(f"/v1/projects/{project['id']}/draft", headers=HEADERS).json()
        assert unchanged["draft_revision"] == 1
        assert unchanged["spec_json"]["width_mm"] == 900
        assert unchanged["design_hash"] == first_save.json()["design_hash"]


def test_project_preview_and_saved_revision_share_the_same_design_identity() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Project preview identity"}
        ).json()
        preview_response = client.post(
            f"/v1/designs/preview?project_id={project['id']}",
            headers=HEADERS,
            json=valid_spec(),
        )
        version_response = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        )

        assert preview_response.status_code == 200
        assert version_response.status_code == 201
        assert preview_response.json()["design_hash"] == version_response.json()["design_hash"]


def test_auto_workspace_draft_and_version_use_the_exact_autofix_identity() -> None:
    auto_spec = valid_spec() | {
        "width_mm": 4200,
        "height_mm": 2400,
        "depth_mm": 330,
        "reinforcement_mode": "auto",
        "wall_anchor_required": False,
    }
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Canonical autofix identity"}
        ).json()
        autofix = client.post(
            f"/v1/designs/autofix?project_id={project['id']}",
            headers=HEADERS,
            json=auto_spec,
        )
        assert autofix.status_code == 200
        assert autofix.json()["spec"]["parameters"]["wall_anchor"]["required"] is True

        draft = client.put(
            f"/v1/projects/{project['id']}/draft",
            headers=HEADERS,
            json={
                "expected_draft_revision": 0,
                "template_id": "shelving",
                "spec": auto_spec,
                "workspace_spec": valid_workspace_intent(),
            },
        )
        saved = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"], auto_spec),
        )

        assert draft.status_code == 200
        assert saved.status_code == 201
        assert draft.json()["design_hash"] == autofix.json()["design_hash"]
        assert saved.json()["design_hash"] == autofix.json()["design_hash"]
        assert saved.json()["result_json"]["spec"]["parameters"]["wall_anchor"]["required"] is True


def test_version_create_requires_frozen_context_and_expected_identity() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Required version context"}
        ).json()
        missing_context = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={
                "spec": valid_spec(),
                "expected_design_hash": "a" * 64,
                "expected_current_revision": 0,
            },
        )
        extra_field_payload = version_payload(project["id"])
        extra_field_payload["unexpected"] = True
        extra_field = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=extra_field_payload,
        )

        assert missing_context.status_code == 422
        assert extra_field.status_code == 422


def test_expected_design_hash_mismatch_does_not_create_or_supersede_a_revision() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Expected hash conflict"}
        ).json()
        payload = version_payload(project["id"])
        payload["expected_design_hash"] = "0" * 64
        conflict = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=payload,
        )

        assert conflict.status_code == 409
        assert "EXPECTED_DESIGN_HASH_MISMATCH" in conflict.json()["detail"]
        assert client.get(f"/v1/projects/{project['id']}/versions", headers=HEADERS).json() == []
        restored = client.get(f"/v1/projects/{project['id']}", headers=HEADERS).json()
        assert restored["current_revision"] == 0


def test_version_create_is_idempotent_only_for_the_exact_frozen_context() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Frozen context idempotency"}
        ).json()
        endpoint = f"/v1/projects/{project['id']}/versions"
        payload = version_payload(project["id"])
        first = client.post(endpoint, headers=HEADERS, json=payload)
        retry = client.post(endpoint, headers=HEADERS, json=payload)

        assert first.status_code == retry.status_code == 201
        assert retry.json()["id"] == first.json()["id"]
        assert retry.json()["context_hash"] == first.json()["context_hash"]

        large_context = valid_production_context(
            stock_width_mm=5000,
            stock_height_mm=2500,
            stock_count=3,
            back_stock_width_mm=5000,
            back_stock_height_mm=2500,
            back_stock_count=2,
            machine_profile_id="custombuild-router-5125-linuxcnc",
        )
        changed = client.post(
            endpoint,
            headers=HEADERS,
            json=version_payload(
                project["id"],
                production_context=large_context,
                expected_current_revision=1,
            ),
        )

        assert changed.status_code == 201
        assert changed.json()["revision"] == 2
        assert changed.json()["design_hash"] == first.json()["design_hash"]
        assert changed.json()["context_hash"] != first.json()["context_hash"]
        assert changed.json()["result_json"]["production_context"] == large_context


def test_concurrent_revision_conflict_never_supersedes_unreviewed_server_state() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Concurrent revision conflict"}
        ).json()
        endpoint = f"/v1/projects/{project['id']}/versions"
        first = client.post(endpoint, headers=HEADERS, json=version_payload(project["id"]))
        stale_editor = client.post(
            endpoint,
            headers=HEADERS,
            json=version_payload(
                project["id"],
                valid_spec() | {"width_mm": 720},
                expected_current_revision=0,
            ),
        )

        assert first.status_code == 201
        assert stale_editor.status_code == 409
        assert "EXPECTED_CURRENT_REVISION_MISMATCH" in stale_editor.json()["detail"]
        versions = client.get(endpoint, headers=HEADERS).json()
        assert [(item["revision"], item["status"]) for item in versions] == [(1, "draft")]


def test_verified_reference_provenance_is_frozen_on_its_own_revision() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Reference provenance"}
        ).json()
        plain = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        )
        imported = upload_reference(client, project["id"])
        payload = version_payload(project["id"], expected_current_revision=1)
        verified = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={
                **payload,
                "source_provenance": verified_reference_provenance(
                    imported,
                    str(payload["expected_design_hash"]),
                ),
            },
        )

        assert plain.status_code == verified.status_code == 201
        assert verified.json()["revision"] == plain.json()["revision"] + 1
        assert verified.json()["design_hash"] == plain.json()["design_hash"]
        assert verified.json()["context_hash"] != plain.json()["context_hash"]
        assert verified.json()["source_provenance_json"]["verification_status"] == (
            "parametric_confirmed"
        )
        assert verified.json()["source_provenance_json"]["image_sha256"] == imported["image_sha256"]
        assert verified.json()["source_import_id"] == imported["import_id"]


def test_reference_provenance_fails_closed_when_any_confirmation_is_missing() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Invalid provenance"}
        ).json()
        imported = upload_reference(client, project["id"])
        payload = version_payload(project["id"])
        provenance = verified_reference_provenance(
            imported,
            str(payload["expected_design_hash"]),
        )
        confirmed_inputs = provenance["confirmed_inputs"]
        assert isinstance(confirmed_inputs, dict)
        provenance["confirmed_inputs"] = {
            **confirmed_inputs,
            "material_confirmed": False,
        }
        response = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={**payload, "source_provenance": provenance},
        )

    assert response.status_code == 422


def test_reference_upload_and_clipboard_paste_reuse_the_same_immutable_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: list[tuple[str, bytes, str, str]] = []
    monkeypatch.setattr(
        api_module,
        "store_immutable_object",
        lambda key, content, content_type, sha256: stored.append(
            (key, content, content_type, sha256)
        ),
    )
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Idempotent source image"}
        ).json()
        uploaded = upload_reference(client, project["id"], filename="camera.png")
        pasted = upload_reference(client, project["id"], filename="urklipp.png")

    assert pasted["import_id"] == uploaded["import_id"]
    assert pasted["image_sha256"] == uploaded["image_sha256"]
    assert len(stored) == 1
    assert stored[0][1] == REFERENCE_IMAGE_BYTES
    assert stored[0][3] == uploaded["image_sha256"]
    assert stored[0][0].endswith(f"/sha256/{uploaded['image_sha256']}/{uploaded['import_id']}")
    with get_session_factory()() as session:
        assets = list(
            session.scalars(select(ImportedAsset).where(ImportedAsset.project_id == project["id"]))
        )
    assert len(assets) == 1
    assert assets[0].sha256 == uploaded["image_sha256"]


def test_reference_import_cannot_cross_project_or_organization_boundaries() -> None:
    atelier_headers = {"Authorization": "Bearer demo-atelier-owner"}
    with TestClient(app) as client:
        source_project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Reference source owner"}
        ).json()
        same_org_project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Reference other project"}
        ).json()
        other_org_project = client.post(
            "/v1/projects",
            headers=atelier_headers,
            json={"name": "Reference other tenant"},
        ).json()
        imported = upload_reference(client, source_project["id"])

        same_org_payload = version_payload(same_org_project["id"])
        same_org_payload["source_provenance"] = verified_reference_provenance(
            imported,
            str(same_org_payload["expected_design_hash"]),
        )
        same_org = client.post(
            f"/v1/projects/{same_org_project['id']}/versions",
            headers=HEADERS,
            json=same_org_payload,
        )

        other_org_payload = version_payload(other_org_project["id"])
        other_org_payload["source_provenance"] = verified_reference_provenance(
            imported,
            str(other_org_payload["expected_design_hash"]),
        )
        other_org = client.post(
            f"/v1/projects/{other_org_project['id']}/versions",
            headers=atelier_headers,
            json=other_org_payload,
        )

    assert same_org.status_code == other_org.status_code == 409
    assert same_org.json()["detail"]["code"] == "REFERENCE_ASSET_NOT_FOUND"
    assert other_org.json()["detail"]["code"] == "REFERENCE_ASSET_NOT_FOUND"
    assert "reference-imports" not in same_org.text
    assert "reference-imports" not in other_org.text


@pytest.mark.parametrize(
    ("storage_error", "expected_status", "expected_code"),
    (
        (
            storage_module.ArtifactIntegrityError("private/path/missing"),
            409,
            "REFERENCE_ASSET_INTEGRITY_FAILED",
        ),
        (
            storage_module.ArtifactStorageUnavailableError("minio.internal:9000"),
            503,
            "REFERENCE_ASSET_STORAGE_UNAVAILABLE",
        ),
    ),
)
def test_reference_revision_fails_closed_when_source_storage_cannot_be_verified(
    monkeypatch: pytest.MonkeyPatch,
    storage_error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": f"Source storage {expected_code}"}
        ).json()
        imported = upload_reference(client, project["id"])
        payload = version_payload(project["id"])
        payload["source_provenance"] = verified_reference_provenance(
            imported,
            str(payload["expected_design_hash"]),
        )

        def fail_verification(_expectation: object, *, stream_hash: bool) -> None:
            assert stream_hash is True
            raise storage_error

        monkeypatch.setattr(api_module, "verify_stored_object", fail_verification)
        response = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=payload,
        )

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code
    assert response.json()["detail"]["solution"]
    assert "private/path" not in response.text
    assert "minio.internal" not in response.text


def test_reference_confirmation_is_rejected_after_the_model_spec_changes() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Reference fingerprint drift"}
        ).json()
        imported = upload_reference(client, project["id"])
        confirmed_payload = version_payload(project["id"])
        changed_payload = version_payload(
            project["id"],
            valid_spec() | {"width_mm": 860},
        )
        changed_payload["source_provenance"] = verified_reference_provenance(
            imported,
            str(confirmed_payload["expected_design_hash"]),
        )
        response = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=changed_payload,
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REFERENCE_MODEL_FINGERPRINT_MISMATCH"
    assert response.json()["detail"]["solution"]


def test_client_cannot_invent_a_trusted_reference_model_fingerprint() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Untrusted source fingerprint"}
        ).json()
        imported = upload_reference(client, project["id"])
        payload = version_payload(project["id"])
        payload["source_provenance"] = verified_reference_provenance(
            imported,
            "f" * 64,
        )
        response = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=payload,
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REFERENCE_MODEL_FINGERPRINT_MISMATCH"


def test_reference_provenance_requires_server_issued_import_identity() -> None:
    provenance = {
        key: value
        for key, value in verified_reference_provenance(
            {"import_id": "1" * 36, "image_sha256": "2" * 64},
            "3" * 64,
        ).items()
        if key not in {"import_id", "image_sha256"}
    }
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Missing source identity"}
        ).json()
        response = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"], source_provenance=provenance),
        )

    assert response.status_code == 422


def test_generation_requires_explicit_design_approval_for_reviewed_design() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Explicit approval fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        assert version["result_json"]["status"] == "WARNING"
        warning_rule_ids = sorted(
            item["rule_id"]
            for item in version["result_json"]["rule_evaluations"]
            if item["status"] == "WARNING"
        )
        assert warning_rule_ids == ["CB-JOINT-001"]
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200

        blocked = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == (
            "Explicit design approval is required before generation"
        )

        approve_design(client, base)
        queued = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert queued.status_code == 202
        assert queued.json()["status"] == "queued"


def test_generation_rejects_stock_or_machine_choices_not_frozen_on_revision() -> None:
    frozen = valid_production_context(
        stock_width_mm=5000,
        stock_height_mm=2500,
        stock_count=3,
        back_stock_width_mm=5000,
        back_stock_height_mm=2500,
        back_stock_count=2,
        machine_profile_id="custombuild-router-5125-linuxcnc",
    )
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Frozen generation choices"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"], production_context=frozen),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)

        mismatch = client.post(f"{base}/generate", headers=HEADERS, json={})
        exact = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json={
                **frozen,
                "postprocessor_id": "linuxcnc-validation-1.1.0",
                "include_step": True,
                "include_freecad_project": False,
                "include_validation_program": True,
            },
        )

        assert mismatch.status_code == 409
        assert "FROZEN_PRODUCTION_CONTEXT_MISMATCH" in mismatch.json()["detail"]
        assert exact.status_code == 202
        with get_session_factory()() as session:
            stored = session.get(GenerationJob, exact.json()["id"])
            assert stored is not None
            assert stored.request_json["stock_width_mm"] == 5000
            assert stored.request_json["machine_profile_id"] == ("custombuild-router-5125-linuxcnc")


def test_generation_external_evidence_order_is_canonical_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_module, "store_evidence_object", lambda *_args: None)
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Canonical generation evidence order"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        uploaded_ids: list[str] = []
        for evidence_type, rule_id in (
            ("wall_anchor", "CB-TIP-001"),
            ("hardware", "CB-HARDWARE-001"),
        ):
            uploaded = client.post(
                f"/v1/projects/{project['id']}/evidence",
                headers=HEADERS,
                data={
                    "evidence_type": evidence_type,
                    "rule_id": rule_id,
                    "catalog_id": f"{evidence_type}-catalog",
                    "catalog_version": "2026.1",
                    "design_hash": version["design_hash"],
                },
                files={
                    "document": (
                        f"{evidence_type}.png",
                        b"\x89PNG\r\n\x1a\n" + evidence_type.encode("ascii"),
                        "image/png",
                    )
                },
            )
            assert uploaded.status_code == 201
            uploaded_ids.append(uploaded.json()["id"])

        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        sorted_ids = sorted(uploaded_ids)
        reversed_ids = list(reversed(sorted_ids))

        first = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json=valid_production_context() | {"external_evidence_ids": reversed_ids},
        )
        second = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json=valid_production_context() | {"external_evidence_ids": sorted_ids},
        )

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["production_context_hash"] == second.json()["production_context_hash"]
    with get_session_factory()() as session:
        jobs = list(
            session.scalars(
                select(GenerationJob).where(GenerationJob.design_version_id == version["id"])
            )
        )
        generation_events = [
            event
            for event in session.scalars(
                select(OutboxEvent).where(OutboxEvent.topic == "generation.requested")
            )
            if event.payload_json.get("job_id") == first.json()["id"]
        ]
        generation_audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "generation.queued",
                    AuditEvent.entity_id == first.json()["id"],
                )
            )
        )

    assert len(jobs) == 1
    persisted = jobs[0]
    assert persisted.request_json["external_evidence_ids"] == sorted_ids
    assert [
        snapshot["evidence_id"] for snapshot in persisted.request_json["external_evidence"]
    ] == sorted_ids
    assert len(generation_events) == 1
    assert generation_events[0].event_key == f"generation:{persisted.id}"
    assert len(generation_audits) == 1


def test_persist_validate_and_idempotently_queue_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Vertical flow fixture"}
        ).json()
        version_response = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        )
        assert version_response.status_code == 201
        version = version_response.json()
        validated = client.post(
            f"/v1/projects/{project['id']}/versions/{version['revision']}/validate",
            headers=HEADERS,
        )
        assert validated.status_code == 200
        assert validated.json()["status"] == "design_validated"

        path = f"/v1/projects/{project['id']}/versions/{version['revision']}/generate"
        approve_design(
            client,
            f"/v1/projects/{project['id']}/versions/{version['revision']}",
        )
        invalid_freecad = client.post(
            path,
            headers=HEADERS,
            json={"include_step": False, "include_freecad_project": True},
        )
        assert invalid_freecad.status_code == 422
        generation = {
            "stock_width_mm": 2440,
            "stock_height_mm": 1220,
            "stock_count": 4,
        }
        first = client.post(path, headers=HEADERS, json=generation)
        second = client.post(path, headers=HEADERS, json=generation)
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["production_context_hash"] == second.json()["production_context_hash"]
        frozen_context = first.json()["production_engine_context_json"]
        assert frozen_context["schema_version"] == "custombuild.production-engine-context.v5"
        assert {
            key: frozen_context[key] for key in get_settings().build_identity
        } == get_settings().build_identity
        assert len(frozen_context["machine_profile_fingerprint"]) == 64
        assert len(frozen_context["tool_library_fingerprint"]) == 64
        assert frozen_context["freecad_bridge_version"] == "freecad-project-bridge-1.1.0"
        assert frozen_context["freecad_project_contract_version"].startswith("freecad-part-read")

        original_resolver = api_module.resolve_production_components

        def drifted_resolver(
            *,
            machine_profile_id: str,
            postprocessor_id: str,
            app_version: str,
            vcs_ref: str,
            build_date: str,
            source_url: str,
            source_manifest_sha256: str,
            dependency_lock_sha256: str,
            require_cad_runtime: bool = False,
        ):
            resolved = original_resolver(
                machine_profile_id=machine_profile_id,
                postprocessor_id=postprocessor_id,
                app_version=app_version,
                vcs_ref=vcs_ref,
                build_date=build_date,
                source_url=source_url,
                source_manifest_sha256=source_manifest_sha256,
                dependency_lock_sha256=dependency_lock_sha256,
                require_cad_runtime=require_cad_runtime,
            )
            return replace(
                resolved,
                context=replace(
                    resolved.context,
                    operations_engine_version="semantic-operations-library-drift",
                ),
            )

        monkeypatch.setattr(api_module, "resolve_production_components", drifted_resolver)
        after_library_drift = client.post(path, headers=HEADERS, json=generation)
        assert after_library_drift.status_code == 202
        assert after_library_drift.json()["id"] != first.json()["id"]
        assert (
            after_library_drift.json()["production_context_hash"]
            != first.json()["production_context_hash"]
        )


def test_failed_generation_can_be_requeued_without_duplicate_successful_jobs() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Recover failed generation"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)

        first = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        with get_session_factory().begin() as session:
            failed = session.get(GenerationJob, first["id"])
            assert failed is not None
            failed.status = JobStatus.failed
            failed.attempts = 4
            failed.error = "STOCK_PROFILE_MISSING"
            failed.finished_at = datetime.now(UTC)

        retried = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert retried.status_code == 202
        assert retried.json()["id"] == first["id"]
        assert retried.json()["status"] == "queued"
        assert retried.json()["attempts"] == 0
        assert retried.json()["error"] is None

        repeated = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert repeated.status_code == 202
        assert repeated.json()["id"] == first["id"]
        assert repeated.json()["status"] == "queued"


def test_failed_generation_retry_waits_for_active_storage_claim_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Busy failed generation retry"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        with get_session_factory().begin() as session:
            failed = session.get(GenerationJob, generated["id"])
            assert failed is not None
            failed.status = JobStatus.failed
            failed.attempts = 4
            failed.error = "retry fixture"
            failed.finished_at = datetime.now(UTC)

        monkeypatch.setattr(
            api_module,
            "prepare_generation_storage_retry",
            lambda *_args, **_kwargs: 73,
        )
        response = client.post(f"{base}/generate", headers=HEADERS, json={})

        assert response.status_code == 503
        assert response.headers["retry-after"] == "73"
        assert response.json()["detail"]["code"] == "GENERATION_STORAGE_RETRY_BUSY"
        with get_session_factory()() as session:
            failed_after = session.get(GenerationJob, generated["id"])
            retry_events = [
                event
                for event in session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.event_key.like("generation-manual-retry:%")
                    )
                )
                if event.payload_json.get("job_id") == generated["id"]
            ]
            retry_audits = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "generation.requeued",
                        AuditEvent.entity_id == generated["id"],
                    )
                )
            )

        assert failed_after is not None
        assert failed_after.status == JobStatus.failed
        assert failed_after.attempts == 4
        assert failed_after.error == "retry fixture"
        assert retry_events == []
        assert retry_audits == []


def test_failed_generation_retry_maps_a_late_trigger_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Late retry claim race"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        with get_session_factory().begin() as session:
            failed = session.get(GenerationJob, generated["id"])
            assert failed is not None
            failed.status = JobStatus.failed
            failed.attempts = 2
            failed.error = "late race fixture"

        monkeypatch.setattr(
            api_module,
            "prepare_generation_storage_retry",
            lambda *_args, **_kwargs: 0,
        )
        original_flush = Session.flush

        def reject_late_claim(self: Session, *args: object, **kwargs: object) -> None:
            retry_job = next(
                (
                    item
                    for item in self.identity_map.values()
                    if isinstance(item, GenerationJob) and item.id == generated["id"]
                ),
                None,
            )
            if retry_job is not None and retry_job.status == JobStatus.queued:
                raise DBAPIError(
                    "UPDATE generation_jobs",
                    {},
                    RuntimeError("STORAGE_GENERATION_RETRY_BUSY:17"),
                    False,
                )
            original_flush(self, *args, **kwargs)

        monkeypatch.setattr(Session, "flush", reject_late_claim)
        response = client.post(f"{base}/generate", headers=HEADERS, json={})

        assert response.status_code == 503
        assert response.headers["retry-after"] == "17"
        assert response.json()["detail"]["code"] == "GENERATION_STORAGE_RETRY_BUSY"
        with get_session_factory()() as session:
            failed_after = session.get(GenerationJob, generated["id"])
            retry_events = [
                event
                for event in session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.event_key.like("generation-manual-retry:%")
                    )
                )
                if event.payload_json.get("job_id") == generated["id"]
            ]
            retry_audits = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "generation.requeued",
                        AuditEvent.entity_id == generated["id"],
                    )
                )
            )

        assert failed_after is not None
        assert failed_after.status == JobStatus.failed
        assert failed_after.attempts == 2
        assert failed_after.error == "late race fixture"
        assert retry_events == []
        assert retry_audits == []


@pytest.mark.parametrize(
    "invalid_selection",
    (
        {"machine_profile_id": "unknown-machine"},
        {"postprocessor_id": "unknown-postprocessor"},
    ),
)
def test_generation_rejects_unversioned_catalog_selection(
    invalid_selection: dict[str, str],
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": f"Invalid catalog {invalid_selection}"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)

        response = client.post(f"{base}/generate", headers=HEADERS, json=invalid_selection)

        expected_status = 409 if "machine_profile_id" in invalid_selection else 422
        assert response.status_code == expected_status
        expected_detail = (
            "FROZEN_PRODUCTION_CONTEXT_MISMATCH"
            if expected_status == 409
            else "unknown or unverified"
        )
        assert expected_detail in str(response.json()["detail"])


def test_generation_requires_a_new_revision_after_design_library_drift() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Stale domain library fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        with get_session_factory().begin() as session:
            stored = session.get(DesignVersion, version["id"])
            assert stored is not None
            stored.engine_version = f"{stored.engine_version}-stale"

        response = client.post(f"{base}/generate", headers=HEADERS, json={})

        assert response.status_code == 409
        assert "frozen design libraries are stale" in str(response.json()["detail"])


def test_client_cannot_self_assert_wall_anchor_evidence() -> None:
    with TestClient(app) as client:
        payload = valid_spec() | {"wall_anchor_verified": True}
        response = client.post("/v1/designs/preview", headers=HEADERS, json=payload)
        assert response.status_code == 422
        assert "not accepted" in str(response.json()["detail"])


def test_measured_thickness_cannot_expand_the_versioned_material_range() -> None:
    with TestClient(app) as client:
        for unsupported_thickness in (16.9, 19.1):
            response = client.post(
                "/v1/designs/preview",
                headers=HEADERS,
                json=valid_spec() | {"measured_thickness_mm": unsupported_thickness},
            )
            assert response.status_code == 422


def test_api_accepts_only_complete_safe_v2_readiness_and_strict_legacy_v1() -> None:
    assert api_module._workshop_readiness_is_valid(valid_readiness_result()) is True

    legacy_without_historic_program_fields = {
        "authoritative_geometry": True,
        "dfm_status": "PASS",
        "workshop_readiness": valid_workshop_readiness(legacy=True),
    }
    assert api_module._workshop_readiness_is_valid(legacy_without_historic_program_fields) is True
    assert api_module._workshop_readiness_is_valid(valid_readiness_result(legacy=True)) is True

    incomplete_legacy = deepcopy(legacy_without_historic_program_fields)
    incomplete_legacy["workshop_readiness"]["software_evidence"] = []
    assert api_module._workshop_readiness_is_valid(incomplete_legacy) is False


def test_api_rejects_generated_status_that_omits_required_validation_program() -> None:
    result = valid_readiness_result()
    result["design_review_package_status"] = generated_design_review_package_status(
        validation_program_included=False
    ).as_dict()

    assert api_module._design_review_package_is_valid(result, require_cam=False) is False


def test_api_cross_checks_readiness_against_non_cutting_machine_program_fields() -> None:
    missing_both = {"workshop_readiness": valid_workshop_readiness()}
    only_mode = missing_both | {"machine_program_mode": "VALIDATION_DRY_RUN"}
    only_flag = missing_both | {"production_machine_program": False}
    cutting_mode = valid_readiness_result() | {"machine_program_mode": "PRODUCTION"}
    cutting_flag = valid_readiness_result() | {"production_machine_program": True}
    legacy_only_mode = {
        "workshop_readiness": valid_workshop_readiness(legacy=True),
        "machine_program_mode": "VALIDATION_DRY_RUN",
    }

    for unsafe in (
        missing_both,
        only_mode,
        only_flag,
        cutting_mode,
        cutting_flag,
        legacy_only_mode,
    ):
        assert api_module._workshop_readiness_is_valid(unsafe) is False


def test_api_rejects_structurally_ambiguous_readiness_even_with_safe_program_fields() -> None:
    unexpected_top_level = valid_readiness_result()
    unexpected_top_level["workshop_readiness"]["untrusted_scope"] = "design_review"
    wrong_count = valid_readiness_result()
    wrong_count["workshop_readiness"]["missing_evidence_count"] = 0
    authorized = valid_readiness_result()
    authorized["workshop_readiness"]["physical_cutting_authorized"] = True
    noncanonical_order = valid_readiness_result()
    software = noncanonical_order["workshop_readiness"]["software_evidence"]
    software[0], software[1] = software[1], software[0]

    for malformed in (unexpected_top_level, wrong_count, authorized, noncanonical_order):
        assert api_module._workshop_readiness_is_valid(malformed) is False


@pytest.mark.parametrize(
    ("field", "value", "remove"),
    _UNSAFE_GENERATION_CLAIM_PARAMS,
)
def test_api_readiness_rejects_unsafe_generation_result_claims(
    field: str,
    value: object,
    remove: bool,
) -> None:
    malformed = _with_unsafe_generation_claim(
        valid_readiness_result(),
        field,
        value,
        remove,
    )

    assert api_module._generation_result_claims_are_safe(malformed) is False
    assert api_module._workshop_readiness_is_valid(malformed) is False


@pytest.mark.parametrize("dfm_status", ("PASS", "WARNING"))
def test_api_readiness_accepts_canonical_nonblocking_generation_claims(
    dfm_status: str,
) -> None:
    result = valid_readiness_result()
    result["dfm_status"] = dfm_status

    assert api_module._generation_result_claims_are_safe(result) is True
    assert api_module._workshop_readiness_is_valid(result) is True


def _bounded_artifact_result(
    evidence_artifacts: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "bundle_object_key": "private/production.zip",
        "bundle_sha256": "a" * 64,
        "bundle_size_bytes": 1,
        "manifest_object_key": "private/manifest.json",
        "manifest_sha256": "b" * 64,
        "manifest_size_bytes": 1,
        "evidence_artifacts": evidence_artifacts,
    }


def test_artifact_expectations_reject_inventory_count_before_iterating_entries() -> None:
    evidence = [
        {
            "kind": f"extra_{index:03d}",
            "object_key": f"private/evidence/{index:03d}",
            "sha256": "c" * 64,
            "size_bytes": 1,
            "content_type": "application/octet-stream",
        }
        for index in range(MAX_EVIDENCE_ARTIFACTS + 1)
    ]
    job = SimpleNamespace(result_json=_bounded_artifact_result(evidence))

    expectations, invalid = api_module._artifact_expectations(job)

    assert set(expectations) == {"production_bundle", "manifest"}
    assert invalid == ["evidence_artifacts"]


def test_artifact_expectations_reject_total_bytes_and_duplicate_object_keys() -> None:
    item_size = 3 * 1024 * 1024
    evidence = [
        {
            "kind": f"extra_{index:03d}",
            "object_key": f"private/evidence/{index:03d}",
            "sha256": "c" * 64,
            "size_bytes": item_size,
            "content_type": "application/octet-stream",
        }
        for index in range(MAX_EVIDENCE_TOTAL_BYTES // item_size + 1)
    ]
    job = SimpleNamespace(result_json=_bounded_artifact_result(evidence))

    _expectations, invalid = api_module._artifact_expectations(job)

    assert invalid == ["evidence_artifacts"]

    duplicate_key = _bounded_artifact_result(
        [
            {
                "kind": "dfm_report",
                "object_key": "private/manifest.json",
                "sha256": "d" * 64,
                "size_bytes": 1,
                "content_type": "application/json",
            }
        ]
    )
    duplicate_expectations, duplicate_invalid = api_module._artifact_expectations(
        SimpleNamespace(result_json=duplicate_key)
    )
    assert "dfm_report" not in duplicate_expectations
    assert duplicate_invalid == ["dfm_report"]


def _manifest_document_for_job(
    job: GenerationJob,
    readiness_expectation: storage_module.StoredObjectExpectation,
    dfm_expectation: storage_module.StoredObjectExpectation,
    stock_selection_expectation: storage_module.StoredObjectExpectation,
    generation_plan_expectation: storage_module.StoredObjectExpectation,
    package_status_expectation: storage_module.StoredObjectExpectation,
    *,
    cam_blocked: bool = False,
    evidence_artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    artifact_entries = [
        {
            "path": path,
            "media_type": media_type,
            "role": role,
            "size_bytes": 128,
            "sha256": "f" * 64,
        }
        for path, media_type, role in (
            ("bom/bom.csv", "text/csv", "BOM"),
            ("bom/grouped-bom.json", "application/json", "GROUPED_BOM"),
            ("cut-list/cut-list.csv", "text/csv", "CUT_LIST"),
            ("design/design-spec.json", "application/json", "FROZEN_DESIGN_SPEC"),
            ("design/result-summary.json", "application/json", "DESIGN_RESULT_SUMMARY"),
            ("drawings/example/A.svg", "image/svg+xml", "PART_DRAWING"),
            ("materials/material-list.csv", "text/csv", "MATERIAL_LIST"),
            ("model/design.glb", "model/gltf-binary", "WEB_PREVIEW_GLB"),
            ("model/design.step", "model/step", "AUTHORITATIVE_STEP"),
            ("parts/example/A.dxf", "image/vnd.dxf", "PART_DXF"),
            (
                "validation/cad-interchange-status.json",
                "application/json",
                "CAD_INTERCHANGE_STATUS",
            ),
        )
    ] + [
        {
            "path": "validation/dfm-report.json",
            "media_type": dfm_expectation.content_type,
            "role": "DFM_VALIDATION_REPORT",
            "size_bytes": dfm_expectation.size_bytes,
            "sha256": dfm_expectation.sha256,
        },
        {
            "path": "validation/workshop-readiness.json",
            "media_type": readiness_expectation.content_type,
            "role": "WORKSHOP_READINESS_REPORT",
            "size_bytes": readiness_expectation.size_bytes,
            "sha256": readiness_expectation.sha256,
        },
        {
            "path": "validation/stock-selection.json",
            "media_type": stock_selection_expectation.content_type,
            "role": "STOCK_SELECTION_SNAPSHOT",
            "size_bytes": stock_selection_expectation.size_bytes,
            "sha256": stock_selection_expectation.sha256,
        },
        {
            "path": "validation/generation-plan.json",
            "media_type": generation_plan_expectation.content_type,
            "role": "GENERATION_PLAN",
            "size_bytes": generation_plan_expectation.size_bytes,
            "sha256": generation_plan_expectation.sha256,
        },
        {
            "path": DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
            "media_type": package_status_expectation.content_type,
            "role": DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
            "size_bytes": package_status_expectation.size_bytes,
            "sha256": package_status_expectation.sha256,
        },
    ]
    if not cam_blocked:
        artifact_entries.extend(
            {
                "path": path,
                "media_type": media_type,
                "role": role,
                "size_bytes": 128,
                "sha256": "e" * 64,
            }
            for path, media_type, role in (
                ("cam/operations.json", "application/json", "MACHINE_NEUTRAL_OPERATIONS"),
                ("cam/setups/setup-001.svg", "image/svg+xml", "SETUP_SHEET"),
                ("cam/validation-backplot.svg", "image/svg+xml", "VALIDATION_BACKPLOT"),
                (
                    "machine-validation/setup-001.validation.ngc",
                    "text/x-gcode",
                    "NON_CUTTING_VALIDATION_PROGRAM",
                ),
                ("nesting/stock/sheet-001.svg", "image/svg+xml", "NESTING_MAP"),
            )
        )
    evidence_values = (
        evidence_artifacts
        if evidence_artifacts is not None
        else (
            list(job.result_json.get("evidence_artifacts", []))
            if isinstance(job.result_json, dict)
            else []
        )
    )
    static_identities = {
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
            "validation/stock-selection.json",
            "STOCK_SELECTION_SNAPSHOT",
            "application/json",
        ),
        "generation_plan": (
            "validation/generation-plan.json",
            "GENERATION_PLAN",
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
    entries_by_path = {str(entry["path"]): entry for entry in artifact_entries}
    for item in evidence_values:
        kind = item.get("kind")
        identity = static_identities.get(str(kind))
        if identity is None and isinstance(kind, str) and kind.startswith("setup_sheet_"):
            index = int(kind.removeprefix("setup_sheet_"))
            identity = (
                f"cam/setups/setup-{index:03d}.svg",
                "SETUP_SHEET",
                "image/svg+xml",
            )
        if identity is None:
            continue
        path, role, media_type = identity
        entries_by_path[path] = {
            "path": path,
            "media_type": media_type,
            "role": role,
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
        }
    artifact_entries = list(entries_by_path.values())
    artifact_entries.sort(key=lambda item: str(item["path"]))

    with get_session_factory()() as fixture_session:
        version = fixture_session.get(DesignVersion, job.design_version_id)
        assert version is not None
        assert isinstance(version.result_json, dict)
        capability = version.result_json["template_capability"]
        design_spec = version.result_json["spec"]
    engine_context = job.production_engine_context_json
    material_versions = sorted(
        {
            f"{material['material_id']}@{material['version']}"
            for material in (design_spec["material"], design_spec.get("back_material"))
            if material is not None
        }
    )
    external_evidence = list(job.request_json.get("external_evidence", []))
    warnings = [
        "Beräkningarna är deterministisk screening och beslutsstöd, inte "
        "produktcertifiering eller garanti för säker konstruktion."
    ]
    warnings.extend(
        f"{item['rule_id']}@{item['rule_version']}: {item['title']}"
        for item in version.result_json.get("rule_evaluations", [])
        if item.get("status") == "WARNING" and item.get("rule_id") != "DFM-GRAIN-001"
    )
    context: dict[str, Any] = {field: None for field in MANIFEST_CONTEXT_HASH_FIELDS}
    context.update(
        {
            "project_id": version.project_id,
            "revision": str(version.revision),
            "design_hash": version.design_hash,
            "app_version": engine_context["app_version"],
            "engine_version": version.engine_version,
            "template_version": version.result_json["template_version"],
            "domain_template_version": version.result_json["template_version"],
            "template_capability_version": capability["template_version"],
            "template_capability_registry_version": engine_context[
                "template_capability_registry_version"
            ],
            "template_id": version.template_id,
            "template_capability_fingerprint": version.template_capability_fingerprint,
            "template_capability": capability,
            "rule_version": version.rule_version,
            "material_versions": material_versions,
            "joint_version": engine_context["joint_support_version"],
            "machine_profile": {
                "id": engine_context["machine_profile_id"],
                "version": engine_context["machine_profile_version"],
            },
            "postprocessor_version": engine_context["postprocessor_version"],
            "generation_context_hash": job.production_context_hash,
            "production_engine_context": deepcopy(job.production_engine_context_json),
            "artifact_schema_version": engine_context["artifact_schema_version"],
            "cad_status": "GENERATED",
            "release_scope": "design_review",
            "machine_use": "validation_only",
            "physical_cutting_authorized": False,
            "approved_assumptions": [],
            "warnings": sorted(warnings),
            "overrides": deepcopy(job.request_json.get("approved_warning_overrides", [])),
            "external_evidence": external_evidence,
            "source_provenance": version.source_provenance_json or None,
            "artifacts": artifact_entries,
        }
    )
    return {
        "schema_version": PRODUCTION_MANIFEST_SCHEMA_VERSION,
        **context,
        "production_context_hash": hashlib.sha256(canonical_json_bytes(context)).hexdigest(),
        "checksum_scope": ("all payload files; manifest.json excluded to avoid recursive hashing"),
    }


def _simulated_verified_review_document(
    expectation: storage_module.StoredObjectExpectation,
    *,
    max_bytes: int,
) -> bytes:
    """Return fixture bytes; storage verification itself has focused unit coverage."""

    with get_session_factory()() as session:
        artifact = session.scalar(
            select(Artifact).where(Artifact.object_key == expectation.object_key)
        )
        assert artifact is not None
        job = session.get(GenerationJob, artifact.generation_job_id)
        assert job is not None
        assert isinstance(job.result_json, dict)
        expectations, invalid = api_module._artifact_expectations(job)
        assert not invalid
        if artifact.kind == "workshop_readiness":
            payload = canonical_json_bytes(job.result_json["workshop_readiness"])
        elif artifact.kind == "dfm_report":
            package_status = api_module._design_review_package_status(job.result_json)
            if package_status is not None and package_status.blocker_codes == (
                "STOCK_PROFILE_MISSING",
            ):
                payload = canonical_json_bytes(
                    _stock_blocked_dfm_report_for_version(job.design_version_id)
                )
            else:
                payload = canonical_json_bytes(
                    valid_dfm_report(status=str(job.result_json.get("dfm_status", "PASS")))
                )
        elif artifact.kind == "design_review_package_status":
            payload = canonical_json_bytes(job.result_json["design_review_package_status"])
        elif artifact.kind == "stock_selection":
            version = session.get(DesignVersion, job.design_version_id)
            assert version is not None
            payload = api_module._frozen_stock_selection_snapshot(version)
            assert payload is not None
        elif artifact.kind == "generation_plan":
            version = session.get(DesignVersion, job.design_version_id)
            assert version is not None
            payload = api_module._frozen_generation_plan_snapshot(job, version)
            assert payload is not None
        elif artifact.kind == "manifest":
            package_status = api_module._design_review_package_status(job.result_json)
            assert package_status is not None
            payload = canonical_json_bytes(
                _manifest_document_for_job(
                    job,
                    expectations["workshop_readiness"],
                    expectations["dfm_report"],
                    expectations["stock_selection"],
                    expectations["generation_plan"],
                    expectations["design_review_package_status"],
                    cam_blocked=(package_status.cam_status is api_module.CAMStageStatus.BLOCKED),
                )
            )
        else:
            raise AssertionError(f"unexpected semantic document kind: {artifact.kind}")
    assert len(payload) <= max_bytes
    return payload


def _persist_committed_artifact_records(
    session: Session,
    job: GenerationJob,
    records: list[dict[str, Any]],
) -> None:
    """Persist worker-shaped artifact rows with their exact committed ledger batch."""

    version = session.get(DesignVersion, job.design_version_id)
    assert version is not None
    lease_token = str(uuid4())
    attempt_id = worker_tasks._generation_attempt_id(job.id, lease_token)
    claims = tuple(
        StorageObjectClaim(
            project_id=version.project_id,
            object_key=str(record["object_key"]),
            sha256=str(record["sha256"]),
            size_bytes=int(record["size_bytes"]),
            media_type=str(record["content_type"]),
            owner_type="generation_job",
            owner_id=job.id,
            idempotency_key=(
                f"generation:{job.id}:{record['kind']}:"
                f"{worker_tasks._generation_artifact_id(attempt_id, str(record['kind']))}"
            ),
        )
        for record in records
    )
    reserve_storage_batch_in_transaction(
        session,
        job.organization_id,
        claims,
        lease_token=lease_token,
        lease_duration=timedelta(minutes=15),
    )
    commit_storage_batch_in_transaction(
        session,
        job.organization_id,
        claims,
        lease_token=lease_token,
    )
    for record in records:
        artifact_id = worker_tasks._generation_artifact_id(
            attempt_id,
            str(record["kind"]),
        )
        session.add(
            Artifact(
                id=artifact_id,
                organization_id=job.organization_id,
                generation_job_id=job.id,
                kind=str(record["kind"]),
                object_key=str(record["object_key"]),
                sha256=str(record["sha256"]),
                size_bytes=int(record["size_bytes"]),
                content_type=str(record["content_type"]),
            )
        )


def _rewrite_committed_artifact_identity(
    session: Session,
    job: GenerationJob,
    artifact: Artifact,
    *,
    sha256: str,
    size_bytes: int,
    content_type: str,
) -> None:
    """Keep a checksum-consistent fixture rewrite exact in ledger and quota counters."""

    version = session.get(DesignVersion, job.design_version_id)
    stored = session.get(StoredObject, (job.organization_id, artifact.object_key))
    global_quota = session.get(StorageGlobalQuota, 1)
    tenant_quota = session.get(StorageTenantQuota, job.organization_id)
    assert version is not None
    assert stored is not None
    assert stored.state == StoredObjectState.committed
    assert stored.project_id == version.project_id
    assert stored.owner_type == "generation_job"
    assert stored.owner_id == job.id
    assert stored.idempotency_key == (f"generation:{job.id}:{artifact.kind}:{artifact.id}")
    assert global_quota is not None
    assert tenant_quota is not None

    byte_delta = size_bytes - stored.size_bytes
    global_quota.committed_bytes += byte_delta
    tenant_quota.committed_bytes += byte_delta
    assert global_quota.committed_bytes >= 0
    assert tenant_quota.committed_bytes >= 0
    stored.sha256 = sha256
    stored.size_bytes = size_bytes
    stored.media_type = content_type
    artifact.sha256 = sha256
    artifact.size_bytes = size_bytes
    artifact.content_type = content_type


def _complete_generation(
    job_id: str,
    _manifest_sha_seed: str,
    *,
    dfm_status: str = "PASS",
) -> None:
    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None
        version = session.get(DesignVersion, job.design_version_id)
        assert version is not None
        job.status = JobStatus.succeeded
        job.finished_at = datetime.now(UTC)
        readiness = valid_workshop_readiness()
        readiness_payload = canonical_json_bytes(readiness)
        dfm_payload = canonical_json_bytes(valid_dfm_report(status=dfm_status))
        stock_selection_payload = api_module._frozen_stock_selection_snapshot(version)
        assert stock_selection_payload is not None
        generation_plan_payload = api_module._frozen_generation_plan_snapshot(job, version)
        assert generation_plan_payload is not None
        package_status = generated_design_review_package_status(
            validation_program_included=True
        ).as_dict()
        package_status_payload = canonical_json_bytes(package_status)
        readiness_expectation = storage_module.StoredObjectExpectation(
            object_key=f"evidence/{job.id}/workshop_readiness",
            sha256=hashlib.sha256(readiness_payload).hexdigest(),
            size_bytes=len(readiness_payload),
            content_type="application/json",
        )
        dfm_expectation = storage_module.StoredObjectExpectation(
            object_key=f"evidence/{job.id}/dfm_report",
            sha256=hashlib.sha256(dfm_payload).hexdigest(),
            size_bytes=len(dfm_payload),
            content_type="application/json",
        )
        stock_selection_expectation = storage_module.StoredObjectExpectation(
            object_key=f"evidence/{job.id}/stock_selection",
            sha256=hashlib.sha256(stock_selection_payload).hexdigest(),
            size_bytes=len(stock_selection_payload),
            content_type="application/json",
        )
        generation_plan_expectation = storage_module.StoredObjectExpectation(
            object_key=f"evidence/{job.id}/generation_plan",
            sha256=hashlib.sha256(generation_plan_payload).hexdigest(),
            size_bytes=len(generation_plan_payload),
            content_type="application/json",
        )
        package_status_expectation = storage_module.StoredObjectExpectation(
            object_key=f"evidence/{job.id}/design_review_package_status",
            sha256=hashlib.sha256(package_status_payload).hexdigest(),
            size_bytes=len(package_status_payload),
            content_type="application/json",
        )
        evidence_artifacts = [
            {
                "kind": kind,
                "object_key": f"evidence/{job.id}/{kind}",
                "sha256": sha256,
                "size_bytes": size_bytes,
                "content_type": content_type,
            }
            for kind, content_type, sha256, size_bytes in (
                (
                    "dfm_report",
                    "application/json",
                    dfm_expectation.sha256,
                    dfm_expectation.size_bytes,
                ),
                ("operations", "application/json", "a" * 64, 128),
                ("validation_backplot", "image/svg+xml", "a" * 64, 128),
                ("design_glb", "model/gltf-binary", "a" * 64, 128),
                ("setup_sheet_001", "image/svg+xml", "a" * 64, 128),
                (
                    "workshop_readiness",
                    "application/json",
                    readiness_expectation.sha256,
                    readiness_expectation.size_bytes,
                ),
                (
                    "stock_selection",
                    "application/json",
                    stock_selection_expectation.sha256,
                    stock_selection_expectation.size_bytes,
                ),
                (
                    "generation_plan",
                    "application/json",
                    generation_plan_expectation.sha256,
                    generation_plan_expectation.size_bytes,
                ),
                (
                    "design_review_package_status",
                    "application/json",
                    package_status_expectation.sha256,
                    package_status_expectation.size_bytes,
                ),
            )
        ]
        manifest_payload = canonical_json_bytes(
            _manifest_document_for_job(
                job,
                readiness_expectation,
                dfm_expectation,
                stock_selection_expectation,
                generation_plan_expectation,
                package_status_expectation,
                evidence_artifacts=evidence_artifacts,
            )
        )
        manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
        job.result_json = {
            "production_engine_context_hash": hashlib.sha256(
                canonical_json_bytes(job.production_engine_context_json)
            ).hexdigest(),
            "authoritative_geometry": True,
            "dfm_status": dfm_status,
            "design_review_package_status": package_status,
            "machine_program_mode": "VALIDATION_DRY_RUN",
            "production_machine_program": False,
            "bundle_object_key": f"evidence/{job.id}/production_bundle",
            "bundle_sha256": "b" * 64,
            "bundle_size_bytes": 128,
            "manifest_object_key": f"evidence/{job.id}/manifest",
            "manifest_sha256": manifest_sha,
            "manifest_size_bytes": len(manifest_payload),
            "evidence_artifacts": evidence_artifacts,
            "workshop_readiness": readiness,
        }
        records = [
            {
                "kind": "production_bundle",
                "object_key": f"evidence/{job.id}/production_bundle",
                "sha256": "b" * 64,
                "size_bytes": 128,
                "content_type": "application/zip",
            },
            {
                "kind": "manifest",
                "object_key": f"evidence/{job.id}/manifest",
                "sha256": manifest_sha,
                "size_bytes": len(manifest_payload),
                "content_type": "application/json",
            },
            *evidence_artifacts,
        ]
        _persist_committed_artifact_records(session, job, records)


def _complete_blocked_cam_generation(
    job_id: str,
    *,
    blocker_code: str = "TWO_SIDED_REGISTRATION_MISSING",
    dfm_report_payload: dict[str, Any] | None = None,
    dfm_passed_override: bool | None = None,
    readiness_mutator: Callable[[dict[str, Any]], None] | None = None,
    manifest_mutator: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, bytes]:
    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None
        version = session.get(DesignVersion, job.design_version_id)
        assert version is not None
        grain_contract = api_module._frozen_grain_contract(version)
        assert grain_contract is not None
        matched_grain_issues, missing_stock_grain_issues, _ = grain_contract
        job.status = JobStatus.succeeded
        job.finished_at = datetime.now(UTC)
        stock_blocked = blocker_code == "STOCK_PROFILE_MISSING"
        grain_blocked = blocker_code == DFM_GRAIN_BLOCKER_CODE
        dfm_blocked = stock_blocked or grain_blocked
        readiness = build_workshop_readiness_report(
            authoritative_cad=True,
            dfm_passed=(not dfm_blocked if dfm_passed_override is None else dfm_passed_override),
            operation_count=0,
            setup_count=0,
            validation_backplot=False,
            validation_program=False,
            edge_band_selection_required=True,
            material_grain_binding_required=bool(missing_stock_grain_issues),
            external_evidence=tuple(job.request_json.get("external_evidence", ())),
        ).as_dict()
        if readiness_mutator is not None:
            readiness_mutator(readiness)
        package_status = blocked_design_review_package_status((blocker_code,)).as_dict()
        if dfm_report_payload is None and grain_blocked:
            assert matched_grain_issues
            dfm_report_payload = json.loads(
                canonical_json_bytes(
                    DFMReport(
                        matched_grain_issues,
                        engine_version=DFM_ENGINE_VERSION,
                    )
                )
            )
        elif dfm_report_payload is None and stock_blocked:
            dfm_report_payload = _stock_blocked_dfm_report_for_version(version.id)
        dfm_payload = canonical_json_bytes(
            dfm_report_payload if dfm_report_payload is not None else valid_dfm_report()
        )
        readiness_payload = canonical_json_bytes(readiness)
        package_status_payload = canonical_json_bytes(package_status)
        stock_selection_payload = api_module._frozen_stock_selection_snapshot(version)
        assert stock_selection_payload is not None
        generation_plan_payload = api_module._frozen_generation_plan_snapshot(job, version)
        assert generation_plan_payload is not None
        readiness_expectation = storage_module.StoredObjectExpectation(
            object_key=f"evidence/{job.id}/workshop_readiness",
            sha256=hashlib.sha256(readiness_payload).hexdigest(),
            size_bytes=len(readiness_payload),
            content_type="application/json",
        )
        package_status_expectation = storage_module.StoredObjectExpectation(
            object_key=f"evidence/{job.id}/design_review_package_status",
            sha256=hashlib.sha256(package_status_payload).hexdigest(),
            size_bytes=len(package_status_payload),
            content_type="application/json",
        )
        dfm_expectation = storage_module.StoredObjectExpectation(
            object_key=f"evidence/{job.id}/dfm_report",
            sha256=hashlib.sha256(dfm_payload).hexdigest(),
            size_bytes=len(dfm_payload),
            content_type="application/json",
        )
        stock_selection_expectation = storage_module.StoredObjectExpectation(
            object_key=f"evidence/{job.id}/stock_selection",
            sha256=hashlib.sha256(stock_selection_payload).hexdigest(),
            size_bytes=len(stock_selection_payload),
            content_type="application/json",
        )
        generation_plan_expectation = storage_module.StoredObjectExpectation(
            object_key=f"evidence/{job.id}/generation_plan",
            sha256=hashlib.sha256(generation_plan_payload).hexdigest(),
            size_bytes=len(generation_plan_payload),
            content_type="application/json",
        )
        evidence_artifacts = [
            {
                "kind": "dfm_report",
                "object_key": dfm_expectation.object_key,
                "sha256": dfm_expectation.sha256,
                "size_bytes": dfm_expectation.size_bytes,
                "content_type": dfm_expectation.content_type,
            },
            {
                "kind": "design_glb",
                "object_key": f"evidence/{job.id}/design_glb",
                "sha256": "c" * 64,
                "size_bytes": 128,
                "content_type": "model/gltf-binary",
            },
            {
                "kind": "workshop_readiness",
                "object_key": readiness_expectation.object_key,
                "sha256": readiness_expectation.sha256,
                "size_bytes": readiness_expectation.size_bytes,
                "content_type": readiness_expectation.content_type,
            },
            {
                "kind": "design_review_package_status",
                "object_key": package_status_expectation.object_key,
                "sha256": package_status_expectation.sha256,
                "size_bytes": package_status_expectation.size_bytes,
                "content_type": package_status_expectation.content_type,
            },
            {
                "kind": "stock_selection",
                "object_key": stock_selection_expectation.object_key,
                "sha256": stock_selection_expectation.sha256,
                "size_bytes": stock_selection_expectation.size_bytes,
                "content_type": stock_selection_expectation.content_type,
            },
            {
                "kind": "generation_plan",
                "object_key": generation_plan_expectation.object_key,
                "sha256": generation_plan_expectation.sha256,
                "size_bytes": generation_plan_expectation.size_bytes,
                "content_type": generation_plan_expectation.content_type,
            },
        ]
        manifest = _manifest_document_for_job(
            job,
            readiness_expectation,
            dfm_expectation,
            stock_selection_expectation,
            generation_plan_expectation,
            package_status_expectation,
            cam_blocked=True,
            evidence_artifacts=evidence_artifacts,
        )
        if manifest_mutator is not None:
            manifest_mutator(manifest)
            context = {field: manifest[field] for field in MANIFEST_CONTEXT_HASH_FIELDS}
            manifest["production_context_hash"] = hashlib.sha256(
                canonical_json_bytes(context)
            ).hexdigest()
        manifest_payload = canonical_json_bytes(manifest)
        manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
        job.result_json = {
            "production_engine_context_hash": hashlib.sha256(
                canonical_json_bytes(job.production_engine_context_json)
            ).hexdigest(),
            "authoritative_geometry": True,
            "dfm_status": "BLOCK" if dfm_blocked else "PASS",
            "design_review_package_status": package_status,
            "machine_program_mode": "CAM_BLOCKED",
            "production_machine_program": False,
            "nesting_utilization_ppm": None,
            "used_sheet_count": 0,
            "nesting_layouts": [],
            "bundle_object_key": f"evidence/{job.id}/production_bundle",
            "bundle_sha256": "b" * 64,
            "bundle_size_bytes": 128,
            "manifest_object_key": f"evidence/{job.id}/manifest",
            "manifest_sha256": manifest_sha,
            "manifest_size_bytes": len(manifest_payload),
            "evidence_artifacts": evidence_artifacts,
            "workshop_readiness": readiness,
        }
        records = [
            {
                "kind": "production_bundle",
                "object_key": f"evidence/{job.id}/production_bundle",
                "sha256": "b" * 64,
                "size_bytes": 128,
                "content_type": "application/zip",
            },
            {
                "kind": "manifest",
                "object_key": f"evidence/{job.id}/manifest",
                "sha256": manifest_sha,
                "size_bytes": len(manifest_payload),
                "content_type": "application/json",
            },
            *evidence_artifacts,
        ]
        _persist_committed_artifact_records(session, job, records)
        return {
            readiness_expectation.object_key: readiness_payload,
            dfm_expectation.object_key: dfm_payload,
            stock_selection_expectation.object_key: stock_selection_payload,
            generation_plan_expectation.object_key: generation_plan_payload,
            package_status_expectation.object_key: package_status_payload,
            f"evidence/{job.id}/manifest": manifest_payload,
        }


def _stock_blocked_dfm_report_for_version(
    version_id: str,
    *,
    grain_mutator: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    with get_session_factory()() as session:
        version = session.get(DesignVersion, version_id)
        assert version is not None
        grain_contract = api_module._frozen_grain_contract(version)
        assert grain_contract is not None
        _, missing_stock_grain_issues, _ = grain_contract
        stock_missing_issues = api_module._frozen_stock_missing_issues(version)
        assert stock_missing_issues is not None
        if not stock_missing_issues:
            from custombuild_domain import BookcaseDesignSpec, build_bookcase
            from custombuild_manufacturing.adapters import adapt_design_result

            spec = BookcaseDesignSpec.model_validate(version.spec_json)
            first_part = min(
                adapt_design_result(build_bookcase(spec)).parts,
                key=lambda part: part.part_id,
            )
            stock_missing_issues = (stock_profile_missing_issue(first_part),)
    grain_values = [json.loads(canonical_json_bytes(issue)) for issue in missing_stock_grain_issues]
    if grain_mutator is not None:
        grain_mutator(grain_values)
    issues = [
        *stock_missing_issues,
        *(
            DFMIssue(
                code=str(item["code"]),
                severity=Severity(str(item["severity"])),
                message=str(item["message"]),
                part_id=item.get("part_id"),
                feature_id=item.get("feature_id"),
                setup_id=item.get("setup_id"),
                inputs=item["inputs"],
                suggestion=item.get("suggestion"),
            )
            for item in grain_values
        ),
    ]
    return json.loads(
        canonical_json_bytes(DFMReport(tuple(issues), engine_version=DFM_ENGINE_VERSION))
    )


def _convert_to_generated_status_with_missing_manifest_cam(
    job_id: str,
) -> dict[str, bytes]:
    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None and isinstance(job.result_json, dict)
        result = deepcopy(job.result_json)
        readiness = valid_workshop_readiness()
        package_status = generated_design_review_package_status(
            validation_program_included=True
        ).as_dict()
        readiness_payload = canonical_json_bytes(readiness)
        package_status_payload = canonical_json_bytes(package_status)
        readiness_item = next(
            item for item in result["evidence_artifacts"] if item["kind"] == "workshop_readiness"
        )
        status_item = next(
            item
            for item in result["evidence_artifacts"]
            if item["kind"] == "design_review_package_status"
        )
        dfm_item = next(
            item for item in result["evidence_artifacts"] if item["kind"] == "dfm_report"
        )
        stock_selection_item = next(
            item for item in result["evidence_artifacts"] if item["kind"] == "stock_selection"
        )
        generation_plan_item = next(
            item for item in result["evidence_artifacts"] if item["kind"] == "generation_plan"
        )
        readiness_item.update(
            sha256=hashlib.sha256(readiness_payload).hexdigest(),
            size_bytes=len(readiness_payload),
        )
        status_item.update(
            sha256=hashlib.sha256(package_status_payload).hexdigest(),
            size_bytes=len(package_status_payload),
        )
        for item, payload in (
            (readiness_item, readiness_payload),
            (status_item, package_status_payload),
        ):
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == job.id,
                    Artifact.kind == item["kind"],
                )
            )
            assert artifact is not None
            _rewrite_committed_artifact_identity(
                session,
                job,
                artifact,
                sha256=str(item["sha256"]),
                size_bytes=len(payload),
                content_type=artifact.content_type,
            )

        added_records: list[dict[str, Any]] = []
        for kind, content_type in (
            ("operations", "application/json"),
            ("validation_backplot", "image/svg+xml"),
            ("setup_sheet_001", "image/svg+xml"),
        ):
            item = {
                "kind": kind,
                "object_key": f"evidence/{job.id}/{kind}",
                "sha256": "a" * 64,
                "size_bytes": 128,
                "content_type": content_type,
            }
            result["evidence_artifacts"].append(item)
            added_records.append(item)
        _persist_committed_artifact_records(session, job, added_records)

        readiness_expectation = storage_module.StoredObjectExpectation(
            object_key=str(readiness_item["object_key"]),
            sha256=str(readiness_item["sha256"]),
            size_bytes=int(readiness_item["size_bytes"]),
            content_type="application/json",
        )
        status_expectation = storage_module.StoredObjectExpectation(
            object_key=str(status_item["object_key"]),
            sha256=str(status_item["sha256"]),
            size_bytes=int(status_item["size_bytes"]),
            content_type="application/json",
        )
        dfm_expectation = storage_module.StoredObjectExpectation(
            object_key=str(dfm_item["object_key"]),
            sha256=str(dfm_item["sha256"]),
            size_bytes=int(dfm_item["size_bytes"]),
            content_type="application/json",
        )
        stock_selection_expectation = storage_module.StoredObjectExpectation(
            object_key=str(stock_selection_item["object_key"]),
            sha256=str(stock_selection_item["sha256"]),
            size_bytes=int(stock_selection_item["size_bytes"]),
            content_type="application/json",
        )
        generation_plan_expectation = storage_module.StoredObjectExpectation(
            object_key=str(generation_plan_item["object_key"]),
            sha256=str(generation_plan_item["sha256"]),
            size_bytes=int(generation_plan_item["size_bytes"]),
            content_type="application/json",
        )
        version = session.get(DesignVersion, job.design_version_id)
        assert version is not None
        stock_selection_payload = api_module._frozen_stock_selection_snapshot(version)
        assert stock_selection_payload is not None
        generation_plan_payload = api_module._frozen_generation_plan_snapshot(job, version)
        assert generation_plan_payload is not None
        # Intentionally keep the manifest review-only while the authenticated
        # status/result claim generated CAM. The endpoint must reject this split.
        manifest_payload = canonical_json_bytes(
            _manifest_document_for_job(
                job,
                readiness_expectation,
                dfm_expectation,
                stock_selection_expectation,
                generation_plan_expectation,
                status_expectation,
                cam_blocked=True,
                evidence_artifacts=result["evidence_artifacts"],
            )
        )
        manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
        manifest_artifact = session.scalar(
            select(Artifact).where(
                Artifact.generation_job_id == job.id,
                Artifact.kind == "manifest",
            )
        )
        assert manifest_artifact is not None
        _rewrite_committed_artifact_identity(
            session,
            job,
            manifest_artifact,
            sha256=manifest_sha,
            size_bytes=len(manifest_payload),
            content_type=manifest_artifact.content_type,
        )
        result.update(
            design_review_package_status=package_status,
            workshop_readiness=readiness,
            machine_program_mode="VALIDATION_DRY_RUN",
            manifest_sha256=manifest_sha,
            manifest_size_bytes=len(manifest_payload),
        )
        job.result_json = result
        flag_modified(job, "result_json")
        return {
            readiness_expectation.object_key: readiness_payload,
            dfm_expectation.object_key: canonical_json_bytes(valid_dfm_report()),
            stock_selection_expectation.object_key: stock_selection_payload,
            generation_plan_expectation.object_key: generation_plan_payload,
            status_expectation.object_key: package_status_payload,
            str(result["manifest_object_key"]): manifest_payload,
        }


def _persist_strict_review_documents(
    job_id: str,
    *,
    readiness: dict[str, Any] | None = None,
    omit_program_fields: bool = False,
    readiness_content_type: str = "application/json",
    manifest_mutator: Callable[[dict[str, Any]], None] | None = None,
    rehash_manifest_context: bool = True,
) -> dict[str, bytes]:
    """Persist checksum-consistent fixture metadata and return its immutable bytes."""

    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None
        assert isinstance(job.result_json, dict)
        result = deepcopy(job.result_json)
        if readiness is not None:
            result["workshop_readiness"] = deepcopy(readiness)
        if omit_program_fields:
            result.pop("machine_program_mode", None)
            result.pop("production_machine_program", None)

        readiness_payload = canonical_json_bytes(result["workshop_readiness"])
        readiness_sha = hashlib.sha256(readiness_payload).hexdigest()
        readiness_item = next(
            item for item in result["evidence_artifacts"] if item["kind"] == "workshop_readiness"
        )
        readiness_item["sha256"] = readiness_sha
        readiness_item["size_bytes"] = len(readiness_payload)
        readiness_item["content_type"] = readiness_content_type
        readiness_artifact = session.scalar(
            select(Artifact).where(
                Artifact.generation_job_id == job.id,
                Artifact.kind == "workshop_readiness",
            )
        )
        assert readiness_artifact is not None
        _rewrite_committed_artifact_identity(
            session,
            job,
            readiness_artifact,
            sha256=readiness_sha,
            size_bytes=len(readiness_payload),
            content_type=readiness_content_type,
        )

        readiness_expectation = storage_module.StoredObjectExpectation(
            object_key=str(readiness_item["object_key"]),
            sha256=readiness_sha,
            size_bytes=len(readiness_payload),
            content_type=readiness_content_type,
        )
        dfm_item = next(
            item for item in result["evidence_artifacts"] if item["kind"] == "dfm_report"
        )
        dfm_expectation = storage_module.StoredObjectExpectation(
            object_key=str(dfm_item["object_key"]),
            sha256=str(dfm_item["sha256"]),
            size_bytes=int(dfm_item["size_bytes"]),
            content_type=str(dfm_item["content_type"]),
        )
        dfm_payload = canonical_json_bytes(valid_dfm_report())
        stock_selection_item = next(
            item for item in result["evidence_artifacts"] if item["kind"] == "stock_selection"
        )
        stock_selection_expectation = storage_module.StoredObjectExpectation(
            object_key=str(stock_selection_item["object_key"]),
            sha256=str(stock_selection_item["sha256"]),
            size_bytes=int(stock_selection_item["size_bytes"]),
            content_type=str(stock_selection_item["content_type"]),
        )
        generation_plan_item = next(
            item for item in result["evidence_artifacts"] if item["kind"] == "generation_plan"
        )
        generation_plan_expectation = storage_module.StoredObjectExpectation(
            object_key=str(generation_plan_item["object_key"]),
            sha256=str(generation_plan_item["sha256"]),
            size_bytes=int(generation_plan_item["size_bytes"]),
            content_type=str(generation_plan_item["content_type"]),
        )
        version = session.get(DesignVersion, job.design_version_id)
        assert version is not None
        stock_selection_payload = api_module._frozen_stock_selection_snapshot(version)
        assert stock_selection_payload is not None
        generation_plan_payload = api_module._frozen_generation_plan_snapshot(job, version)
        assert generation_plan_payload is not None
        status_item = next(
            item
            for item in result["evidence_artifacts"]
            if item["kind"] == "design_review_package_status"
        )
        status_expectation = storage_module.StoredObjectExpectation(
            object_key=str(status_item["object_key"]),
            sha256=str(status_item["sha256"]),
            size_bytes=int(status_item["size_bytes"]),
            content_type=str(status_item["content_type"]),
        )
        status_payload = canonical_json_bytes(result["design_review_package_status"])
        manifest = _manifest_document_for_job(
            job,
            readiness_expectation,
            dfm_expectation,
            stock_selection_expectation,
            generation_plan_expectation,
            status_expectation,
            evidence_artifacts=result["evidence_artifacts"],
        )
        if manifest_mutator is not None:
            manifest_mutator(manifest)
        if rehash_manifest_context:
            context = {field: manifest[field] for field in MANIFEST_CONTEXT_HASH_FIELDS}
            manifest["production_context_hash"] = hashlib.sha256(
                canonical_json_bytes(context)
            ).hexdigest()
        manifest_payload = canonical_json_bytes(manifest)
        manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
        result["manifest_sha256"] = manifest_sha
        result["manifest_size_bytes"] = len(manifest_payload)
        manifest_artifact = session.scalar(
            select(Artifact).where(
                Artifact.generation_job_id == job.id,
                Artifact.kind == "manifest",
            )
        )
        assert manifest_artifact is not None
        _rewrite_committed_artifact_identity(
            session,
            job,
            manifest_artifact,
            sha256=manifest_sha,
            size_bytes=len(manifest_payload),
            content_type=manifest_artifact.content_type,
        )
        job.result_json = result
        flag_modified(job, "result_json")
        return {
            readiness_expectation.object_key: readiness_payload,
            dfm_expectation.object_key: dfm_payload,
            stock_selection_expectation.object_key: stock_selection_payload,
            generation_plan_expectation.object_key: generation_plan_payload,
            status_expectation.object_key: status_payload,
            str(result["manifest_object_key"]): manifest_payload,
        }


def _install_strict_review_reader(
    monkeypatch: pytest.MonkeyPatch,
    documents: dict[str, bytes],
    calls: list[tuple[str, int]],
) -> None:
    def read_verified(
        expectation: storage_module.StoredObjectExpectation,
        *,
        max_bytes: int,
    ) -> bytes:
        calls.append((expectation.object_key, max_bytes))
        payload = documents[expectation.object_key]
        assert len(payload) <= max_bytes
        assert len(payload) == expectation.size_bytes
        assert hashlib.sha256(payload).hexdigest() == expectation.sha256
        return payload

    monkeypatch.setattr(api_module, "read_verified_stored_object", read_verified)


def _rewrite_bound_review_document(
    job_id: str,
    documents: dict[str, bytes],
    *,
    kind: str,
    payload: bytes,
    content_type: str = "application/json",
) -> None:
    """Coordinate stored metadata and manifest hashes around an untrusted document."""

    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None and isinstance(job.result_json, dict)
        result = deepcopy(job.result_json)
        item = next(entry for entry in result["evidence_artifacts"] if entry["kind"] == kind)
        object_key = str(item["object_key"])
        digest = hashlib.sha256(payload).hexdigest()
        item.update(
            sha256=digest,
            size_bytes=len(payload),
            content_type=content_type,
        )
        artifact = session.scalar(
            select(Artifact).where(
                Artifact.generation_job_id == job.id,
                Artifact.kind == kind,
            )
        )
        assert artifact is not None
        _rewrite_committed_artifact_identity(
            session,
            job,
            artifact,
            sha256=digest,
            size_bytes=len(payload),
            content_type=content_type,
        )

        manifest_key = str(result["manifest_object_key"])
        manifest = json.loads(documents[manifest_key])
        path, _role, _media_type = api_module._EVIDENCE_MANIFEST_IDENTITIES[kind]
        manifest_entry = next(entry for entry in manifest["artifacts"] if entry["path"] == path)
        manifest_entry.update(
            sha256=digest,
            size_bytes=len(payload),
            media_type=content_type,
        )
        context = {field: manifest[field] for field in MANIFEST_CONTEXT_HASH_FIELDS}
        manifest["production_context_hash"] = hashlib.sha256(
            canonical_json_bytes(context)
        ).hexdigest()
        manifest_payload = canonical_json_bytes(manifest)
        manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
        result["manifest_sha256"] = manifest_sha
        result["manifest_size_bytes"] = len(manifest_payload)
        manifest_artifact = session.scalar(
            select(Artifact).where(
                Artifact.generation_job_id == job.id,
                Artifact.kind == "manifest",
            )
        )
        assert manifest_artifact is not None
        _rewrite_committed_artifact_identity(
            session,
            job,
            manifest_artifact,
            sha256=manifest_sha,
            size_bytes=len(manifest_payload),
            content_type=manifest_artifact.content_type,
        )
        job.result_json = result
        flag_modified(job, "result_json")
        documents[object_key] = payload
        documents[manifest_key] = manifest_payload


def _remove_bound_review_document(
    job_id: str,
    documents: dict[str, bytes],
    *,
    kind: str,
) -> None:
    """Remove one review document coherently; API policy must still require it."""

    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None and isinstance(job.result_json, dict)
        result = deepcopy(job.result_json)
        item = next(entry for entry in result["evidence_artifacts"] if entry["kind"] == kind)
        result["evidence_artifacts"] = [
            entry for entry in result["evidence_artifacts"] if entry["kind"] != kind
        ]
        if kind == "design_review_package_status":
            result.pop("design_review_package_status", None)
        artifact = session.scalar(
            select(Artifact).where(
                Artifact.generation_job_id == job.id,
                Artifact.kind == kind,
            )
        )
        assert artifact is not None
        session.delete(artifact)

        manifest_key = str(result["manifest_object_key"])
        manifest = json.loads(documents[manifest_key])
        path, _role, _media_type = api_module._EVIDENCE_MANIFEST_IDENTITIES[kind]
        manifest["artifacts"] = [entry for entry in manifest["artifacts"] if entry["path"] != path]
        context = {field: manifest[field] for field in MANIFEST_CONTEXT_HASH_FIELDS}
        manifest["production_context_hash"] = hashlib.sha256(
            canonical_json_bytes(context)
        ).hexdigest()
        manifest_payload = canonical_json_bytes(manifest)
        manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
        result["manifest_sha256"] = manifest_sha
        result["manifest_size_bytes"] = len(manifest_payload)
        manifest_artifact = session.scalar(
            select(Artifact).where(
                Artifact.generation_job_id == job.id,
                Artifact.kind == "manifest",
            )
        )
        assert manifest_artifact is not None
        _rewrite_committed_artifact_identity(
            session,
            job,
            manifest_artifact,
            sha256=manifest_sha,
            size_bytes=len(manifest_payload),
            content_type=manifest_artifact.content_type,
        )
        job.result_json = result
        flag_modified(job, "result_json")
        documents.pop(str(item["object_key"]))
        documents[manifest_key] = manifest_payload


def _create_strict_review_job(
    client: TestClient,
    *,
    name: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    project = client.post(
        "/v1/projects",
        headers=HEADERS,
        json={"name": name},
    ).json()
    version = client.post(
        f"/v1/projects/{project['id']}/versions",
        headers=HEADERS,
        json=version_payload(project["id"]),
    ).json()
    base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
    assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
    approve_design(client, base)
    generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
    _complete_generation(generated["id"], "7" * 64)
    return version, generated, base


def _create_strict_review_job_with_bound_evidence(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
) -> tuple[dict[str, Any], dict[str, Any], str, str, str, dict[str, bytes]]:
    """Create a checked job whose request is bound to one mutable evidence row."""

    monkeypatch.setattr(api_module, "store_evidence_object", lambda *_args: None)
    project = client.post(
        "/v1/projects",
        headers=HEADERS,
        json={"name": name},
    ).json()
    version = client.post(
        f"/v1/projects/{project['id']}/versions",
        headers=HEADERS,
        json=version_payload(project["id"]),
    ).json()
    uploaded = client.post(
        f"/v1/projects/{project['id']}/evidence",
        headers=HEADERS,
        data={
            "evidence_type": "hardware",
            "rule_id": "CB-HARDWARE-001",
            "catalog_id": "hardware-current-at-generation",
            "catalog_version": "2026.1",
            "design_hash": version["design_hash"],
        },
        files={
            "document": (
                "hardware.png",
                b"\x89PNG\r\n\x1a\nbound-evidence-race",
                "image/png",
            )
        },
    )
    assert uploaded.status_code == 201
    evidence_id = str(uploaded.json()["id"])
    with get_session_factory()() as session:
        evidence = session.get(ExternalEvidence, evidence_id)
        assert evidence is not None
        evidence_object_key = evidence.object_key

    base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
    assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
    approve_design(client, base)
    generated_response = client.post(
        f"{base}/generate",
        headers=HEADERS,
        json=valid_production_context() | {"external_evidence_ids": [evidence_id]},
    )
    assert generated_response.status_code == 202
    generated = generated_response.json()
    _complete_generation(generated["id"], "7" * 64)
    with get_session_factory()() as session:
        stored_job = session.get(GenerationJob, generated["id"])
        assert stored_job is not None
        readiness = build_workshop_readiness_report(
            authoritative_cad=True,
            dfm_passed=True,
            operation_count=36,
            setup_count=2,
            validation_backplot=True,
            validation_program=True,
            edge_band_selection_required=True,
            material_grain_binding_required=False,
            external_evidence=tuple(stored_job.request_json["external_evidence"]),
        ).as_dict()
    documents = _persist_strict_review_documents(generated["id"], readiness=readiness)
    return version, generated, base, evidence_id, evidence_object_key, documents


def _review_mutation_snapshot(version_id: str) -> tuple[Any, ...]:
    with get_session_factory()() as session:
        version = session.get(DesignVersion, version_id)
        assert version is not None
        approvals = tuple(
            sorted(
                (
                    approval.id,
                    approval.approval_type,
                    approval.reason,
                    approval.generation_job_id,
                    approval.production_context_hash,
                    approval.manifest_sha256,
                )
                for approval in session.scalars(
                    select(Approval).where(Approval.design_version_id == version_id)
                )
            )
        )
        releases = tuple(
            sorted(
                (release.id, release.release_number, release.manifest_sha256)
                for release in session.scalars(
                    select(Release).where(Release.design_version_id == version_id)
                )
            )
        )
        return approvals, releases, version.status, version.immutable


def test_four_eyes_rejects_same_user_cam_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        version, generated, base = _create_strict_review_job(
            client,
            name="Four eyes same reviewer CAM guard",
        )
        before = _review_mutation_snapshot(version["id"])
        _enable_production_four_eyes(monkeypatch)

        rejected = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "The design reviewer must not self-approve CAM",
                "generation_job_id": generated["id"],
            },
        )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == (
        api_module.FOUR_EYES_APPROVER_SEPARATION_REQUIRED_CODE
    )
    assert _review_mutation_snapshot(version["id"]) == before


def test_four_eyes_accepts_two_users_and_rechecks_manipulated_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        version, generated, base = _create_strict_review_job(
            client,
            name="Four eyes distinct reviewer release guard",
        )
        _provision_four_eyes_reviewer(monkeypatch)
        _enable_production_four_eyes(monkeypatch)

        cam = client.post(
            f"{base}/approve",
            headers=FOUR_EYES_REVIEWER_HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Independent CAM review",
                "generation_job_id": generated["id"],
            },
        )
        released = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "FOUR-EYES-R1", "confirmation": "RELEASE"},
        )

        assert cam.status_code == 200, cam.json()
        assert released.status_code == 200, released.json()
        with get_session_factory().begin() as session:
            approvals = {
                approval.approval_type: approval
                for approval in session.scalars(
                    select(Approval).where(Approval.design_version_id == version["id"])
                )
            }
            assert approvals["design"].approved_by == DEV_USER_NORDIC
            assert approvals["cam"].approved_by == FOUR_EYES_REVIEWER_ID
            # Simulate a pre-policy or directly manipulated row after release.
            approvals["cam"].approved_by = DEV_USER_NORDIC

        replay = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "IGNORED-R2", "confirmation": "RELEASE"},
        )

    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == (
        api_module.FOUR_EYES_APPROVER_SEPARATION_REQUIRED_CODE
    )
    with get_session_factory()() as session:
        persisted = session.get(DesignVersion, version["id"])
        releases = list(
            session.scalars(select(Release).where(Release.design_version_id == version["id"]))
        )
    assert persisted is not None
    assert persisted.status == DesignStatus.released
    assert persisted.immutable is True
    assert len(releases) == 1


@pytest.mark.parametrize(
    ("approval_type", "changed_values", "expected_code"),
    (
        (
            "design",
            {"reason": "Design approval changed during evidence verification"},
            "DESIGN_APPROVAL_SNAPSHOT_STALE",
        ),
        (
            "cam",
            {"manifest_sha256": "f" * 64},
            "CAM_APPROVAL_SNAPSHOT_STALE",
        ),
    ),
)
def test_release_reloads_approvals_changed_during_review_storage_io(
    monkeypatch: pytest.MonkeyPatch,
    approval_type: str,
    changed_values: dict[str, object],
    expected_code: str,
) -> None:
    """A cached design or CAM approval must never survive the final object read."""

    changed = False
    with TestClient(app) as client:
        version, generated, base = _create_strict_review_job(
            client,
            name=f"Release approval race {approval_type}",
        )
        documents = _persist_strict_review_documents(generated["id"])
        _install_strict_review_reader(monkeypatch, documents, [])
        cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "CAM evidence reviewed before concurrent mutation",
                "generation_job_id": generated["id"],
            },
        )
        assert cam.status_code == 200, cam.json()

        def read_then_change_approval(
            expectation: storage_module.StoredObjectExpectation,
            *,
            max_bytes: int,
        ) -> bytes:
            nonlocal changed
            payload = documents[expectation.object_key]
            assert len(payload) <= max_bytes
            assert len(payload) == expectation.size_bytes
            assert hashlib.sha256(payload).hexdigest() == expectation.sha256
            if not changed:
                changed = True
                with get_session_factory().begin() as racing_session:
                    racing_session.execute(
                        update(Approval)
                        .where(
                            Approval.design_version_id == version["id"],
                            Approval.approval_type == approval_type,
                        )
                        .values(**changed_values)
                        .execution_options(synchronize_session=False)
                    )
            return payload

        monkeypatch.setattr(
            api_module,
            "read_verified_stored_object",
            read_then_change_approval,
        )
        released = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={
                "release_number": f"RACE-{approval_type.upper()}",
                "confirmation": "RELEASE",
            },
        )

    assert changed is True
    assert released.status_code == 409
    assert released.json()["detail"]["code"] == expected_code
    with get_session_factory()() as session:
        persisted_version = session.get(DesignVersion, version["id"])
        releases = list(
            session.scalars(select(Release).where(Release.design_version_id == version["id"]))
        )
    assert persisted_version is not None
    assert persisted_version.status == DesignStatus.approved
    assert persisted_version.immutable is False
    assert releases == []


def test_manifest_context_uses_unprefixed_domain_template_version() -> None:
    with TestClient(app) as client:
        version, generated, _base = _create_strict_review_job(
            client,
            name="Domain template version manifest binding",
        )
        documents = _persist_strict_review_documents(generated["id"])

    with get_session_factory()() as session:
        job = session.get(GenerationJob, generated["id"])
        stored_version = session.get(DesignVersion, version["id"])
        assert job is not None and isinstance(job.result_json, dict)
        assert stored_version is not None and isinstance(stored_version.result_json, dict)
        manifest = json.loads(documents[str(job.result_json["manifest_object_key"])])
        domain_version = stored_version.result_json["template_version"]

        assert stored_version.template_version == f"bookcase@{domain_version}"
        assert manifest["template_version"] == domain_version
        assert manifest["domain_template_version"] == domain_version
        assert api_module._manifest_context_matches_frozen_job(
            manifest,
            job,
            stored_version,
        )

        prefixed_manifest = deepcopy(manifest)
        prefixed_manifest["template_version"] = stored_version.template_version
        prefixed_manifest["domain_template_version"] = stored_version.template_version
        assert not api_module._manifest_context_matches_frozen_job(
            prefixed_manifest,
            job,
            stored_version,
        )


def test_artifact_access_revalidates_current_bound_external_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_module, "store_evidence_object", lambda *_args: None)
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Current artifact evidence binding"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        uploaded = client.post(
            f"/v1/projects/{project['id']}/evidence",
            headers=HEADERS,
            data={
                "evidence_type": "hardware",
                "rule_id": "CB-HARDWARE-001",
                "catalog_id": "hardware-current-at-generation",
                "catalog_version": "2026.1",
                "design_hash": version["design_hash"],
            },
            files={
                "document": (
                    "hardware.png",
                    b"\x89PNG\r\n\x1a\ncurrent-bound-evidence",
                    "image/png",
                )
            },
        )
        assert uploaded.status_code == 201
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json=valid_production_context() | {"external_evidence_ids": [uploaded.json()["id"]]},
        )
        assert generated.status_code == 202
        job = generated.json()
        _complete_generation(job["id"], "7" * 64)
        with get_session_factory()() as session:
            stored_job = session.get(GenerationJob, job["id"])
            assert stored_job is not None
            readiness = build_workshop_readiness_report(
                authoritative_cad=True,
                dfm_passed=True,
                operation_count=36,
                setup_count=2,
                validation_backplot=True,
                validation_program=True,
                edge_band_selection_required=True,
                material_grain_binding_required=False,
                external_evidence=tuple(stored_job.request_json["external_evidence"]),
            ).as_dict()
        documents = _persist_strict_review_documents(job["id"], readiness=readiness)
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        current = client.get(f"/v1/jobs/{job['id']}/artifacts", headers=HEADERS)
        assert current.status_code == 200, current.json()

        with get_session_factory().begin() as session:
            evidence = session.get(ExternalEvidence, uploaded.json()["id"])
            assert evidence is not None
            evidence.revoked_at = datetime.now(UTC)

        stale = client.get(f"/v1/jobs/{job['id']}/artifacts", headers=HEADERS)

    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "EXTERNAL_EVIDENCE_STALE"
    assert calls


def test_artifact_listing_rejects_evidence_revoked_during_storage_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limiter and DB refresh must cover the final external-object read."""

    limiter_attempts: list[int] = []
    mutated = False
    with TestClient(app) as client:
        (
            _version,
            generated,
            _base,
            evidence_id,
            evidence_object_key,
            documents,
        ) = _create_strict_review_job_with_bound_evidence(
            client,
            monkeypatch,
            name="Evidence revoked during artifact listing",
        )
        _install_strict_review_reader(monkeypatch, documents, [])

        def verify_then_revoke(
            expectation: storage_module.StoredObjectExpectation,
            *,
            stream_hash: bool,
        ) -> None:
            nonlocal mutated
            if expectation.object_key != evidence_object_key or not stream_hash or mutated:
                return
            mutated = True
            try:
                unexpected_lease = api_module._TENANT_REVIEW_LIMITER.acquire(DEV_ORG_NORDIC)
            except HTTPException as exc:
                limiter_attempts.append(exc.status_code)
            else:
                limiter_attempts.append(200)
                unexpected_lease.close()
            with get_session_factory().begin() as racing_session:
                racing_session.execute(
                    update(ExternalEvidence)
                    .where(ExternalEvidence.id == evidence_id)
                    .values(revoked_at=datetime.now(UTC))
                    .execution_options(synchronize_session=False)
                )

        signed = False

        def must_not_sign(_artifact_id: str, _organization_id: str, _expires: int) -> str:
            nonlocal signed
            signed = True
            raise AssertionError("revoked evidence received a signed artifact link")

        monkeypatch.setattr(api_module, "verify_stored_object", verify_then_revoke)
        monkeypatch.setattr(api_module, "sign_artifact_access", must_not_sign)
        response = client.get(
            f"/v1/jobs/{generated['id']}/artifacts",
            headers=HEADERS,
        )

    assert mutated is True
    assert limiter_attempts == [429]
    assert response.status_code == 409
    assert response.json()["detail"]["code"] in {
        "EXTERNAL_EVIDENCE_STALE",
        "EXTERNAL_EVIDENCE_SNAPSHOT_STALE",
    }
    assert signed is False


def test_stock_selection_and_generation_plan_are_exact_downloadable_review_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        version, generated, _base = _create_strict_review_job(
            client,
            name="Canonical stock and generation plan review evidence",
        )
        documents = _persist_strict_review_documents(generated["id"])
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)
        with get_session_factory()() as session:
            job = session.get(GenerationJob, generated["id"])
            stored_version = session.get(DesignVersion, version["id"])
            assert job is not None and stored_version is not None
            assert isinstance(job.result_json, dict)
            expected_plan = api_module._frozen_generation_plan_snapshot(job, stored_version)
            assert expected_plan is not None
            plan_item = next(
                item
                for item in job.result_json["evidence_artifacts"]
                if item["kind"] == "generation_plan"
            )
            assert documents[str(plan_item["object_key"])] == expected_plan
            parsed_plan = json.loads(expected_plan)
            assert parsed_plan["schema_version"] == "custombuild.generation-plan.v1"
            assert parsed_plan["validation_program_requested"] is True
            assert len(parsed_plan["stock_profiles_fingerprint"]) == 64

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)
        assert listing.status_code == 200
        kinds = {item["kind"] for item in listing.json()}
        assert {"stock_selection", "generation_plan"} <= kinds
        expected_filenames = {
            "stock_selection": (f"custombuild-stock-selection-rev-{version['revision']}.json"),
            "generation_plan": (f"custombuild-generation-plan-rev-{version['revision']}.json"),
        }
        for kind, expected_filename in expected_filenames.items():
            artifact = next(item for item in listing.json() if item["kind"] == kind)
            download = client.get(
                artifact["download_path"],
                headers=HEADERS,
                follow_redirects=False,
            )
            assert download.status_code == 200
            assert len(download.content) == artifact["size_bytes"]
            assert download.headers["content-type"] == "application/json"
            assert download.headers["content-length"] == str(artifact["size_bytes"])
            assert download.headers["etag"] == f'"{artifact["sha256"]}"'
            assert download.headers["digest"] == (
                "sha-256=" + base64.b64encode(bytes.fromhex(artifact["sha256"])).decode("ascii")
            )
            assert download.headers["content-disposition"] == (
                f'attachment; filename="{expected_filename}"'
            )
            assert download.headers.get_list("cache-control") == [
                "private, no-store, no-transform, max-age=0"
            ]
            assert "location" not in download.headers

    plan_reads = [key for key, _max_bytes in calls if key == str(plan_item["object_key"])]
    assert len(plan_reads) == 2


def test_download_reuses_the_verified_target_spool_and_checks_every_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        _version, generated, _base = _create_strict_review_job(
            client,
            name="One GET verified manifest download",
        )
        documents = _persist_strict_review_documents(generated["id"])
        reads: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, reads)
        with get_session_factory()() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            expectations, invalid = api_module._artifact_expectations(job)
            assert not invalid
            expectation = expectations["manifest"]
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == job.id,
                    Artifact.kind == "manifest",
                )
            )
            assert artifact is not None

        opened: list[_SimulatedVerifiedDownload] = []

        def open_manifest(
            observed: storage_module.StoredObjectExpectation,
            *,
            max_bytes: int,
        ) -> _SimulatedVerifiedDownload:
            assert observed == expectation
            assert max_bytes == MAX_CORE_DOCUMENT_BYTES
            token = _SimulatedVerifiedDownload(observed, documents[observed.object_key])
            opened.append(token)
            return token

        monkeypatch.setattr(api_module, "open_verified_stored_object", open_manifest)
        expires = int(datetime.now(UTC).timestamp()) + 60
        signature = api_module.sign_artifact_access(
            artifact.id,
            DEV_ORG_NORDIC,
            expires,
        )
        response = client.get(
            f"/v1/artifacts/{artifact.id}/download?expires={expires}&signature={signature}",
            headers=HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 200
    assert response.content == documents[expectation.object_key]
    assert len(opened) == 1
    assert opened[0].closed is True
    assert opened[0].close_calls == 1
    assert expectation.object_key not in {key for key, _limit in reads}
    assert len(reads) == 5


def test_download_closes_the_verified_target_when_a_sibling_fails_integrity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        _version, generated, _base = _create_strict_review_job(
            client,
            name="Sibling evidence blocks download",
        )
        documents = _persist_strict_review_documents(generated["id"])
        _install_strict_review_reader(monkeypatch, documents, [])
        with get_session_factory()() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            expectations, invalid = api_module._artifact_expectations(job)
            assert not invalid
            target = expectations["production_bundle"]
            failed_sibling_key = expectations["design_glb"].object_key
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == job.id,
                    Artifact.kind == "production_bundle",
                )
            )
            assert artifact is not None

        opened: list[_SimulatedVerifiedDownload] = []

        def open_bundle(
            expectation: storage_module.StoredObjectExpectation,
            *,
            max_bytes: int,
        ) -> _SimulatedVerifiedDownload:
            assert expectation == target
            assert expectation.size_bytes <= max_bytes
            token = _SimulatedVerifiedDownload(expectation)
            opened.append(token)
            return token

        def reject_sibling(
            expectation: storage_module.StoredObjectExpectation,
            *,
            stream_hash: bool,
        ) -> None:
            assert stream_hash is False
            if expectation.object_key == failed_sibling_key:
                raise api_module.ArtifactIntegrityError("private sibling corruption")

        monkeypatch.setattr(api_module, "open_verified_stored_object", open_bundle)
        monkeypatch.setattr(api_module, "verify_stored_object", reject_sibling)
        expires = int(datetime.now(UTC).timestamp()) + 60
        signature = api_module.sign_artifact_access(
            artifact.id,
            DEV_ORG_NORDIC,
            expires,
        )
        response = client.get(
            f"/v1/artifacts/{artifact.id}/download?expires={expires}&signature={signature}",
            headers=HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 409
    assert "private sibling corruption" not in response.text
    assert len(opened) == 1
    assert opened[0].close_calls == 1


@pytest.mark.parametrize(
    "invalidation",
    ("new_revision", "archived_project", "superseded_version"),
)
def test_download_rechecks_current_revision_after_storage_verification(
    monkeypatch: pytest.MonkeyPatch,
    invalidation: str,
) -> None:
    """A verified spool must not escape after concurrent design invalidation."""

    with TestClient(app) as client:
        _version, generated, _base = _create_strict_review_job(
            client,
            name=f"Download current-version race {invalidation}",
        )
        documents = _persist_strict_review_documents(generated["id"])
        _install_strict_review_reader(monkeypatch, documents, [])
        with get_session_factory()() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            stored_version = session.get(DesignVersion, job.design_version_id)
            assert stored_version is not None
            expectations, invalid = api_module._artifact_expectations(job)
            assert not invalid
            target = expectations["production_bundle"]
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == job.id,
                    Artifact.kind == "production_bundle",
                )
            )
            assert artifact is not None
            artifact_id = artifact.id
            project_id = stored_version.project_id
            revision = stored_version.revision
            version_id = stored_version.id

        opened: list[_SimulatedVerifiedDownload] = []

        def open_then_invalidate(
            expectation: storage_module.StoredObjectExpectation,
            *,
            max_bytes: int,
        ) -> _SimulatedVerifiedDownload:
            assert expectation == target
            assert expectation.size_bytes <= max_bytes
            with get_session_factory().begin() as racing_session:
                if invalidation == "new_revision":
                    racing_session.execute(
                        update(Project)
                        .where(Project.id == project_id)
                        .values(current_revision=revision + 1)
                        .execution_options(synchronize_session=False)
                    )
                elif invalidation == "archived_project":
                    racing_session.execute(
                        update(Project)
                        .where(Project.id == project_id)
                        .values(archived=True)
                        .execution_options(synchronize_session=False)
                    )
                else:
                    racing_session.execute(
                        update(DesignVersion)
                        .where(DesignVersion.id == version_id)
                        .values(status=DesignStatus.superseded)
                        .execution_options(synchronize_session=False)
                    )
            token = _SimulatedVerifiedDownload(expectation)
            opened.append(token)
            return token

        monkeypatch.setattr(
            api_module,
            "open_verified_stored_object",
            open_then_invalidate,
        )
        expires = int(datetime.now(UTC).timestamp()) + 60
        signature = api_module.sign_artifact_access(
            artifact_id,
            DEV_ORG_NORDIC,
            expires,
        )
        response = client.get(
            f"/v1/artifacts/{artifact_id}/download?expires={expires}&signature={signature}",
            headers=HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Production artifacts are stale after a design change"
    assert len(opened) == 1
    assert opened[0].close_calls == 1


def test_download_rechecks_current_revision_after_bound_evidence_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last potentially remote evidence check must precede the final refresh."""

    real_bound_check = api_module._require_current_bound_job_evidence
    with TestClient(app) as client:
        _version, generated, _base = _create_strict_review_job(
            client,
            name="Download bound-evidence current-version race",
        )
        documents = _persist_strict_review_documents(generated["id"])
        _install_strict_review_reader(monkeypatch, documents, [])
        with get_session_factory()() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            stored_version = session.get(DesignVersion, job.design_version_id)
            assert stored_version is not None
            expectations, invalid = api_module._artifact_expectations(job)
            assert not invalid
            target = expectations["production_bundle"]
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == job.id,
                    Artifact.kind == "production_bundle",
                )
            )
            assert artifact is not None
            artifact_id = artifact.id
            project_id = stored_version.project_id
            revision = stored_version.revision

        opened: list[_SimulatedVerifiedDownload] = []

        def open_bundle(
            expectation: storage_module.StoredObjectExpectation,
            *,
            max_bytes: int,
        ) -> _SimulatedVerifiedDownload:
            assert expectation == target
            assert expectation.size_bytes <= max_bytes
            token = _SimulatedVerifiedDownload(expectation)
            opened.append(token)
            return token

        def check_then_supersede(
            session: Session,
            organization_id: str,
            job: GenerationJob,
            version: DesignVersion,
        ) -> None:
            real_bound_check(session, organization_id, job, version)
            with get_session_factory().begin() as racing_session:
                racing_session.execute(
                    update(Project)
                    .where(Project.id == project_id)
                    .values(current_revision=revision + 1)
                    .execution_options(synchronize_session=False)
                )

        monkeypatch.setattr(api_module, "open_verified_stored_object", open_bundle)
        monkeypatch.setattr(
            api_module,
            "_require_current_bound_job_evidence",
            check_then_supersede,
        )
        expires = int(datetime.now(UTC).timestamp()) + 60
        signature = api_module.sign_artifact_access(
            artifact_id,
            DEV_ORG_NORDIC,
            expires,
        )
        response = client.get(
            f"/v1/artifacts/{artifact_id}/download?expires={expires}&signature={signature}",
            headers=HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Production artifacts are stale after a design change"
    assert len(opened) == 1
    assert opened[0].close_calls == 1


def test_artifact_listing_rechecks_current_revision_after_review_storage_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listing must not mint signed links after concurrent supersession."""

    real_review_check = api_module._require_review_evidence
    with TestClient(app) as client:
        _version, generated, _base = _create_strict_review_job(
            client,
            name="Artifact listing current-version race",
        )
        documents = _persist_strict_review_documents(generated["id"])
        _install_strict_review_reader(monkeypatch, documents, [])
        with get_session_factory()() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            stored_version = session.get(DesignVersion, job.design_version_id)
            assert stored_version is not None
            project_id = stored_version.project_id
            revision = stored_version.revision

        def check_then_supersede(
            session: Session,
            organization_id: str,
            job: GenerationJob,
            *,
            stream_hash: bool,
            require_cam: bool,
            bind_review_documents: bool,
        ) -> None:
            real_review_check(
                session,
                organization_id,
                job,
                stream_hash=stream_hash,
                require_cam=require_cam,
                bind_review_documents=bind_review_documents,
            )
            with get_session_factory().begin() as racing_session:
                racing_session.execute(
                    update(Project)
                    .where(Project.id == project_id)
                    .values(current_revision=revision + 1)
                    .execution_options(synchronize_session=False)
                )

        signed = False

        def must_not_sign(_artifact_id: str, _organization_id: str, _expires: int) -> str:
            nonlocal signed
            signed = True
            raise AssertionError("stale artifact link was signed")

        monkeypatch.setattr(api_module, "_require_review_evidence", check_then_supersede)
        monkeypatch.setattr(api_module, "sign_artifact_access", must_not_sign)
        response = client.get(
            f"/v1/jobs/{generated['id']}/artifacts",
            headers=HEADERS,
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Production artifacts are stale after a design change"
    assert signed is False


@pytest.mark.parametrize("fail_after_messages", (0, 1), ids=("response-start", "first-body"))
def test_download_response_closes_on_real_asgi_disconnect(
    fail_after_messages: int,
) -> None:
    payload = b"never requested because the client disconnected"
    expectation = storage_module.StoredObjectExpectation(
        object_key="private/pre-first-byte.zip",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        content_type="application/zip",
    )
    verified = _SimulatedVerifiedDownload(expectation, payload)

    response = api_module._verified_artifact_response(
        verified,
        filename="custombuild-pre-first-byte.zip",
    )

    assert verified.closed is False
    assert verified.close_calls == 0

    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if len(sent) == fail_after_messages:
            raise OSError("simulated disconnected client")
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/v1/artifacts/example/download",
        "raw_path": b"/v1/artifacts/example/download",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }

    with pytest.raises(ClientDisconnect):
        asyncio.run(response(scope, receive, send))

    assert verified.closed is True
    assert verified.close_calls == 1


def test_download_stream_deadline_closes_bytes_and_releases_the_tenant_slot() -> None:
    payload = b"slow consumer bytes"
    expectation = storage_module.StoredObjectExpectation(
        object_key="private/slow-consumer.zip",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        content_type="application/zip",
    )
    verified = _SimulatedVerifiedDownload(expectation, payload)
    limiter = api_module._TenantDownloadLimiter()
    lease = limiter.acquire(DEV_ORG_NORDIC)
    response = api_module._verified_artifact_response(
        verified,
        filename="custombuild-slow-consumer.zip",
        tenant_lease=lease,
        stream_timeout_seconds=0.01,
    )

    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/v1/artifacts/example/download",
        "raw_path": b"/v1/artifacts/example/download",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }

    with pytest.raises(TimeoutError):
        asyncio.run(response(scope, receive, send))

    assert verified.closed is True
    assert verified.close_calls == 1
    replacement = limiter.acquire(DEV_ORG_NORDIC)
    replacement.close()


def test_download_request_scope_closes_before_dependency_teardown_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dependency cleanup cannot strand response-owned bytes before __call__."""

    with TestClient(app) as client:
        _version, generated, _base = _create_strict_review_job(
            client,
            name="Download dependency teardown ownership",
        )
        documents = _persist_strict_review_documents(generated["id"])
        _install_strict_review_reader(monkeypatch, documents, [])
        with get_session_factory()() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            expectations, invalid = api_module._artifact_expectations(job)
            assert not invalid
            target = expectations["production_bundle"]
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == job.id,
                    Artifact.kind == "production_bundle",
                )
            )
            assert artifact is not None
            artifact_id = artifact.id

        opened: list[_SimulatedVerifiedDownload] = []

        def open_bundle(
            expectation: storage_module.StoredObjectExpectation,
            *,
            max_bytes: int,
        ) -> _SimulatedVerifiedDownload:
            assert expectation == target
            assert expectation.size_bytes <= max_bytes
            token = _SimulatedVerifiedDownload(expectation)
            opened.append(token)
            return token

        def failing_tenant_session() -> Any:
            session = get_session_factory()()
            try:
                yield session
            finally:
                session.close()
                raise RuntimeError("simulated dependency teardown failure")

        monkeypatch.setattr(api_module, "open_verified_stored_object", open_bundle)
        app.dependency_overrides[api_module.tenant_session] = failing_tenant_session
        expires = int(datetime.now(UTC).timestamp()) + 60
        signature = api_module.sign_artifact_access(
            artifact_id,
            DEV_ORG_NORDIC,
            expires,
        )
        try:
            with pytest.raises(RuntimeError, match="dependency teardown failure"):
                client.get(
                    f"/v1/artifacts/{artifact_id}/download?expires={expires}&signature={signature}",
                    headers=HEADERS,
                    follow_redirects=False,
                )
        finally:
            app.dependency_overrides.pop(api_module.tenant_session, None)

    assert len(opened) == 1
    assert opened[0].closed is True
    assert opened[0].close_calls == 1
    replacement = api_module._TENANT_DOWNLOAD_LIMITER.acquire(DEV_ORG_NORDIC)
    replacement.close()


def test_tenant_download_limiter_is_fair_and_idempotently_released() -> None:
    limiter = api_module._TenantDownloadLimiter()
    nordic = limiter.acquire(DEV_ORG_NORDIC)
    atelier = limiter.acquire("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    with pytest.raises(HTTPException) as rejected:
        limiter.acquire(DEV_ORG_NORDIC)

    assert rejected.value.status_code == 429
    assert rejected.value.headers == {"Retry-After": "5"}
    nordic.close()
    nordic.close()
    replacement = limiter.acquire(DEV_ORG_NORDIC)
    replacement.close()
    atelier.close()


def test_artifact_operation_limiter_enforces_global_capacity_and_releases() -> None:
    limiter = api_module._TenantDownloadLimiter(max_active=2)
    first = limiter.acquire("tenant-a")
    second = limiter.acquire("tenant-b")

    with pytest.raises(HTTPException) as rejected:
        limiter.acquire("tenant-c")

    assert rejected.value.status_code == 503
    assert rejected.value.headers == {"Retry-After": "5"}
    first.close()
    replacement = limiter.acquire("tenant-c")
    replacement.close()
    second.close()


def test_download_and_review_share_one_global_operation_limiter() -> None:
    assert api_module._TENANT_DOWNLOAD_LIMITER is api_module._TENANT_REVIEW_LIMITER


def test_postgres_advisory_lock_failure_maps_to_coordination_unavailable() -> None:
    class FailingPostgresSession:
        @staticmethod
        def get_bind() -> Any:
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        @staticmethod
        def scalar(*_args: object, **_kwargs: object) -> object:
            raise SQLAlchemyError("simulated advisory lock dependency failure")

    with pytest.raises(artifact_operations.ArtifactOperationUnavailableError):
        api_module._acquire_database_artifact_operation_locks(
            FailingPostgresSession(),  # type: ignore[arg-type]
            DEV_ORG_NORDIC,
        )


def test_generate_and_approve_reject_before_handler_io_when_tenant_operation_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        _version, _generated, base = _create_strict_review_job(
            client,
            name="Generate and approve global artifact-operation gate",
        )
        approval_payload = design_approval_payload(client, base)
        handler_calls: list[tuple[object, ...]] = []
        provider_calls: list[tuple[object, ...]] = []

        def must_not_enter_handler(*args: object, **_kwargs: object) -> None:
            handler_calls.append(args)
            raise AssertionError("artifact-operation gate allowed handler I/O")

        def must_not_enter_provider(*args: object, **_kwargs: object) -> None:
            provider_calls.append(args)
            raise AssertionError("artifact-operation gate allowed provider I/O")

        monkeypatch.setattr(api_module, "tenant_version", must_not_enter_handler)
        monkeypatch.setattr(api_module, "verify_stored_object", must_not_enter_provider)
        monkeypatch.setattr(api_module, "read_verified_stored_object", must_not_enter_provider)
        monkeypatch.setattr(api_module, "open_verified_stored_object", must_not_enter_provider)

        lease = api_module._TENANT_DOWNLOAD_LIMITER.acquire(DEV_ORG_NORDIC)
        try:
            responses = (
                client.post(f"{base}/generate", headers=HEADERS, json={}),
                client.post(
                    f"{base}/approve",
                    headers=HEADERS,
                    json=approval_payload,
                ),
            )
        finally:
            lease.close()

    assert handler_calls == []
    assert provider_calls == []
    for response in responses:
        assert response.status_code == 429
        assert response.headers["retry-after"] == "5"
        assert response.json()["detail"] == (
            "Another verified artifact operation is already active for this tenant"
        )
    replacement = api_module._TENANT_REVIEW_LIMITER.acquire(DEV_ORG_NORDIC)
    replacement.close()


def test_shared_replica_lease_rejects_generate_and_approve_before_handler_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease owned by replica A must gate replica B before SQL or storage I/O."""

    shared_store = artifact_operations.InMemoryArtifactOperationStore()
    replica_a = artifact_operations.ArtifactOperationLeaseManager(shared_store)
    replica_b = artifact_operations.ArtifactOperationLeaseManager(shared_store)

    with TestClient(app) as client:
        _version, _generated, base = _create_strict_review_job(
            client,
            name="Cross-replica artifact-operation gate",
        )
        approval_payload = design_approval_payload(client, base)
        active = asyncio.run(replica_a.acquire(DEV_ORG_NORDIC))
        handler_calls: list[tuple[object, ...]] = []
        provider_calls: list[tuple[object, ...]] = []

        def must_not_enter_handler(*args: object, **_kwargs: object) -> None:
            handler_calls.append(args)
            raise AssertionError("shared lease allowed handler I/O on replica B")

        def must_not_enter_provider(*args: object, **_kwargs: object) -> None:
            provider_calls.append(args)
            raise AssertionError("shared lease allowed provider I/O on replica B")

        monkeypatch.setattr(api_module, "_artifact_operation_lease_manager", lambda: replica_b)
        monkeypatch.setattr(api_module, "tenant_version", must_not_enter_handler)
        monkeypatch.setattr(api_module, "verify_stored_object", must_not_enter_provider)
        monkeypatch.setattr(api_module, "read_verified_stored_object", must_not_enter_provider)
        monkeypatch.setattr(api_module, "open_verified_stored_object", must_not_enter_provider)
        try:
            responses = (
                client.post(f"{base}/generate", headers=HEADERS, json={}),
                client.post(
                    f"{base}/approve",
                    headers=HEADERS,
                    json=approval_payload,
                ),
            )
        finally:
            assert asyncio.run(replica_a.release(active)) is True

    assert handler_calls == []
    assert provider_calls == []
    for response in responses:
        assert response.status_code == 429
        assert response.headers["retry-after"] == "5"
        assert response.json()["detail"] == (
            "Another verified artifact operation is already active for this tenant"
        )
    replacement = asyncio.run(replica_b.acquire(DEV_ORG_NORDIC))
    assert asyncio.run(replica_b.release(replacement)) is True


@pytest.mark.parametrize("failure", ("redis_unavailable", "ownership_lost"))
def test_shared_lease_failure_is_503_before_handler_or_storage_io(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Coordination ambiguity must fail closed on mutating and streaming routes."""

    with TestClient(app) as client:
        _version, generated, base = _create_strict_review_job(
            client,
            name=f"Fail-closed shared lease {failure}",
        )
        approval_payload = design_approval_payload(client, base)
        with get_session_factory()() as session:
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == generated["id"],
                    Artifact.kind == "production_bundle",
                )
            )
            assert artifact is not None
            artifact_id = artifact.id
        expires = int(datetime.now(UTC).timestamp()) + 60
        signature = storage_module.sign_artifact_access(
            artifact_id,
            DEV_ORG_NORDIC,
            expires,
        )

        if failure == "redis_unavailable":

            class UnavailableRedisClient:
                async def eval(
                    self,
                    _script: str,
                    _numkeys: int,
                    *_keys_and_args: object,
                ) -> object:
                    raise RedisError("simulated Redis outage")

            manager: Any = artifact_operations.ArtifactOperationLeaseManager(
                artifact_operations.RedisArtifactOperationStore(
                    "redis://unused",
                    operation_timeout_seconds=0.1,
                    client=UnavailableRedisClient(),
                )
            )
        else:

            @asynccontextmanager
            async def ownership_lost(_tenant_id: str) -> AsyncIterator[None]:
                raise artifact_operations.ArtifactOperationOwnershipLostError(
                    "simulated ownership loss"
                )
                yield  # pragma: no cover - marks this as an async context manager

            manager = SimpleNamespace(lease=ownership_lost)

        handler_calls: list[tuple[object, ...]] = []
        provider_calls: list[tuple[object, ...]] = []

        def must_not_enter_handler(*args: object, **_kwargs: object) -> None:
            handler_calls.append(args)
            raise AssertionError("coordination failure allowed handler I/O")

        def must_not_enter_provider(*args: object, **_kwargs: object) -> None:
            provider_calls.append(args)
            raise AssertionError("coordination failure allowed provider I/O")

        monkeypatch.setattr(api_module, "_artifact_operation_lease_manager", lambda: manager)
        monkeypatch.setattr(api_module, "tenant_version", must_not_enter_handler)
        monkeypatch.setattr(api_module, "verify_stored_object", must_not_enter_provider)
        monkeypatch.setattr(api_module, "read_verified_stored_object", must_not_enter_provider)
        monkeypatch.setattr(api_module, "open_verified_stored_object", must_not_enter_provider)

        responses = (
            client.post(f"{base}/generate", headers=HEADERS, json={}),
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json=approval_payload,
            ),
            client.get(
                f"/v1/artifacts/{artifact_id}/download?expires={expires}&signature={signature}",
                headers=HEADERS,
                follow_redirects=False,
            ),
        )

    assert handler_calls == []
    assert provider_calls == []
    for response in responses:
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"
        assert response.json()["detail"] == (
            "Verified artifact operation coordination is temporarily unavailable"
        )


def test_download_holds_shared_lease_through_stream_and_releases_on_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response-owned spool keeps both replica and local gates until ASGI exits."""

    shared_store = artifact_operations.InMemoryArtifactOperationStore()
    download_replica = artifact_operations.ArtifactOperationLeaseManager(shared_store)
    competing_replica = artifact_operations.ArtifactOperationLeaseManager(shared_store)
    stream_started = Event()
    disconnect_client = Event()
    stream_errors: list[BaseException] = []
    sent: list[Message] = []
    download_thread: Thread | None = None

    with TestClient(app) as client:
        _version, generated, base = _create_strict_review_job(
            client,
            name="Cross-replica streaming lease lifetime",
        )
        approval_payload = design_approval_payload(client, base)
        documents = _persist_strict_review_documents(generated["id"])
        provider_calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, provider_calls)
        with get_session_factory()() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            expectations, invalid = api_module._artifact_expectations(job)
            assert not invalid
            target = expectations["manifest"]
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == job.id,
                    Artifact.kind == "manifest",
                )
            )
            assert artifact is not None
            artifact_id = artifact.id

        opened: list[_SimulatedVerifiedDownload] = []

        def open_manifest(
            expectation: storage_module.StoredObjectExpectation,
            *,
            max_bytes: int,
        ) -> _SimulatedVerifiedDownload:
            assert expectation == target
            assert max_bytes == MAX_CORE_DOCUMENT_BYTES
            verified = _SimulatedVerifiedDownload(
                expectation,
                documents[expectation.object_key],
            )
            opened.append(verified)
            return verified

        def select_replica() -> artifact_operations.ArtifactOperationLeaseManager:
            assert download_thread is not None
            return download_replica if current_thread() is download_thread else competing_replica

        handler_calls: list[tuple[object, ...]] = []

        def must_not_enter_handler(*args: object, **_kwargs: object) -> None:
            handler_calls.append(args)
            raise AssertionError("active download allowed competing handler I/O")

        monkeypatch.setattr(api_module, "open_verified_stored_object", open_manifest)
        monkeypatch.setattr(
            api_module,
            "require_committed_storage_binding",
            lambda *_args, **_kwargs: object(),
        )
        monkeypatch.setattr(api_module, "_artifact_operation_lease_manager", select_replica)
        monkeypatch.setattr(api_module, "tenant_version", must_not_enter_handler)

        expires = int(datetime.now(UTC).timestamp()) + 60
        signature = storage_module.sign_artifact_access(
            artifact_id,
            DEV_ORG_NORDIC,
            expires,
        )
        path = f"/v1/artifacts/{artifact_id}/download"
        query = f"expires={expires}&signature={signature}".encode("ascii")
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query,
            "headers": [
                (b"authorization", HEADERS["Authorization"].encode("ascii")),
                (b"content-length", b"0"),
            ],
            "client": ("127.0.0.44", 43001),
            "server": ("testserver", 443),
        }
        request_received = False

        async def receive() -> Message:
            nonlocal request_received
            if not request_received:
                request_received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def send(message: Message) -> None:
            sent.append(message)
            if message["type"] == "http.response.start":
                stream_started.set()
                await asyncio.to_thread(disconnect_client.wait)
                raise OSError("simulated client disconnect during response start")

        def run_download() -> None:
            try:
                asyncio.run(app(scope, receive, send))
            except BaseException as exc:
                stream_errors.append(exc)

        download_thread = Thread(target=run_download, name="replica-a-download")
        download_thread.start()
        try:
            assert stream_started.wait(timeout=10), stream_errors
            provider_count = len(provider_calls)
            responses = (
                client.post(f"{base}/generate", headers=HEADERS, json={}),
                client.post(
                    f"{base}/approve",
                    headers=HEADERS,
                    json=approval_payload,
                ),
            )
            assert handler_calls == []
            assert len(provider_calls) == provider_count
            for response in responses:
                assert response.status_code == 429
                assert response.headers["retry-after"] == "5"
        finally:
            disconnect_client.set()
            download_thread.join(timeout=10)

    assert download_thread.is_alive() is False
    assert [message["status"] for message in sent if message["type"] == "http.response.start"] == [
        200
    ]
    assert stream_errors
    assert len(opened) == 1
    assert opened[0].closed is True
    assert opened[0].close_calls == 1
    replacement = asyncio.run(competing_replica.acquire(DEV_ORG_NORDIC))
    assert asyncio.run(competing_replica.release(replacement)) is True
    try:
        local_replacement = api_module._TENANT_DOWNLOAD_LIMITER.acquire(DEV_ORG_NORDIC)
    except HTTPException:
        # Keep a failed regression isolated: the assertion still fails, but a
        # leaked process-local slot must not cascade into unrelated tests.
        api_module._TENANT_DOWNLOAD_LIMITER._release(DEV_ORG_NORDIC)
        raise
    local_replacement.close()


@pytest.mark.parametrize("failure", ("redis_unavailable", "ownership_lost"))
def test_stream_stops_and_closes_when_shared_lease_heartbeat_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A post-start coordination failure must cancel bytes and release both gates."""

    body_started = Event()
    renewal_attempted = Event()

    class RenewalFailureStore(artifact_operations.InMemoryArtifactOperationStore):
        async def renew(
            self,
            tenant_key_hash: str,
            owner_token: str,
            ttl_seconds: int,
        ) -> artifact_operations.ArtifactOperationRenewResult:
            while not body_started.is_set():
                await asyncio.sleep(0.001)
            renewal_attempted.set()
            if failure == "redis_unavailable":
                raise artifact_operations.ArtifactOperationUnavailableError(
                    "simulated Redis heartbeat outage"
                )
            return artifact_operations.ArtifactOperationRenewResult(
                renewed=False,
                server_time_ms=0,
                expires_at_ms=0,
            )

    store = RenewalFailureStore()
    manager = artifact_operations.ArtifactOperationLeaseManager(
        store,
        heartbeat_interval_seconds=0.001,
    )
    response_started = Event()
    stream_errors: list[BaseException] = []
    sent: list[Message] = []

    with TestClient(app) as client:
        _version, generated, _base = _create_strict_review_job(
            client,
            name=f"Streaming heartbeat failure {failure}",
        )
        documents = _persist_strict_review_documents(generated["id"])
        _install_strict_review_reader(monkeypatch, documents, [])
        with get_session_factory()() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            expectations, invalid = api_module._artifact_expectations(job)
            assert not invalid
            target = expectations["manifest"]
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == job.id,
                    Artifact.kind == "manifest",
                )
            )
            assert artifact is not None
            artifact_id = artifact.id

        opened: list[_SimulatedVerifiedDownload] = []

        def open_manifest(
            expectation: storage_module.StoredObjectExpectation,
            *,
            max_bytes: int,
        ) -> _SimulatedVerifiedDownload:
            assert expectation == target
            assert max_bytes == MAX_CORE_DOCUMENT_BYTES
            verified = _SimulatedVerifiedDownload(
                expectation,
                documents[expectation.object_key],
            )
            opened.append(verified)
            return verified

        monkeypatch.setattr(api_module, "open_verified_stored_object", open_manifest)
        monkeypatch.setattr(
            api_module,
            "require_committed_storage_binding",
            lambda *_args, **_kwargs: object(),
        )
        monkeypatch.setattr(api_module, "_artifact_operation_lease_manager", lambda: manager)
        expires = int(datetime.now(UTC).timestamp()) + 60
        signature = storage_module.sign_artifact_access(
            artifact_id,
            DEV_ORG_NORDIC,
            expires,
        )
        path = f"/v1/artifacts/{artifact_id}/download"
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": (f"expires={expires}&signature={signature}".encode("ascii")),
            "headers": [
                (b"authorization", HEADERS["Authorization"].encode("ascii")),
                (b"content-length", b"0"),
            ],
            "client": ("127.0.0.45", 43002),
            "server": ("testserver", 443),
        }
        request_received = False

        async def receive() -> Message:
            nonlocal request_received
            if not request_received:
                request_received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def send(message: Message) -> None:
            sent.append(message)
            if message["type"] == "http.response.start":
                response_started.set()
                return
            if message["type"] == "http.response.body" and message.get("body"):
                body_started.set()
                await asyncio.Event().wait()

        def run_download() -> None:
            try:
                asyncio.run(app(scope, receive, send))
            except BaseException as exc:
                stream_errors.append(exc)

        stream_thread = Thread(target=run_download, name=f"heartbeat-{failure}")
        stream_thread.start()
        stream_thread.join(timeout=10)

    assert response_started.is_set()
    assert body_started.is_set()
    assert renewal_attempted.is_set()
    assert stream_thread.is_alive() is False
    assert stream_errors
    assert [message["status"] for message in sent if message["type"] == "http.response.start"] == [
        200
    ]
    assert len(opened) == 1
    assert opened[0].closed is True
    assert opened[0].close_calls == 1
    replacement_manager = artifact_operations.ArtifactOperationLeaseManager(store)
    replacement = asyncio.run(replacement_manager.acquire(DEV_ORG_NORDIC))
    assert asyncio.run(replacement_manager.release(replacement)) is True
    local_replacement = api_module._TENANT_DOWNLOAD_LIMITER.acquire(DEV_ORG_NORDIC)
    local_replacement.close()


def test_artifact_operation_scope_is_reentrant_only_for_the_same_tenant() -> None:
    other_organization_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    assert api_module._ACTIVE_ARTIFACT_OPERATION_ORGANIZATION.get() is None

    with api_module._artifact_operation_scope(DEV_ORG_NORDIC):
        assert api_module._ACTIVE_ARTIFACT_OPERATION_ORGANIZATION.get() == DEV_ORG_NORDIC
        with api_module._artifact_operation_scope(DEV_ORG_NORDIC):
            assert api_module._ACTIVE_ARTIFACT_OPERATION_ORGANIZATION.get() == DEV_ORG_NORDIC
        with (
            pytest.raises(RuntimeError, match="cannot switch tenant"),
            api_module._artifact_operation_scope(other_organization_id),
        ):
            raise AssertionError("cross-tenant scope unexpectedly opened")
        with pytest.raises(HTTPException) as still_owned:
            api_module._TENANT_REVIEW_LIMITER.acquire(DEV_ORG_NORDIC)
        assert still_owned.value.status_code == 429

    assert api_module._ACTIVE_ARTIFACT_OPERATION_ORGANIZATION.get() is None
    replacement = api_module._TENANT_REVIEW_LIMITER.acquire(DEV_ORG_NORDIC)
    replacement.close()


def test_worker_completion_gate_does_not_require_api_only_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custombuild_worker import tasks as worker_tasks

    with TestClient(app) as client:
        _version, generated, _base = _create_strict_review_job(
            client,
            name="Worker-owned completion runtime",
        )
        documents = _persist_strict_review_documents(generated["id"])
        _install_strict_review_reader(monkeypatch, documents, [])

    build_identity = dict(get_settings().build_identity)
    monkeypatch.setattr(
        worker_tasks,
        "WORKER_SETTINGS",
        SimpleNamespace(
            s3_bucket="production-artifacts",
            build_identity=build_identity,
            joint_retention_trust_registry_json=None,
        ),
    )
    monkeypatch.setattr(worker_tasks, "_s3_client", object)

    def must_not_load_api_settings() -> None:
        raise AssertionError("worker completion loaded API-only OIDC/runtime settings")

    monkeypatch.setattr(api_module, "get_settings", must_not_load_api_settings)
    with get_session_factory()() as session:
        job = session.get(GenerationJob, generated["id"])
        assert job is not None
        worker_tasks._validate_completion_evidence(
            session,
            DEV_ORG_NORDIC,
            job,
        )


def test_artifact_listing_is_wired_to_the_per_tenant_review_limiter() -> None:
    with TestClient(app) as client:
        _version, generated, _base = _create_strict_review_job(
            client,
            name="Tenant review limiter wiring",
        )
        lease = api_module._TENANT_REVIEW_LIMITER.acquire(DEV_ORG_NORDIC)
        try:
            rejected = client.get(
                f"/v1/jobs/{generated['id']}/artifacts",
                headers=HEADERS,
            )
        finally:
            lease.close()

    assert rejected.status_code == 429
    assert rejected.headers["retry-after"] == "5"
    replacement = api_module._TENANT_REVIEW_LIMITER.acquire(DEV_ORG_NORDIC)
    replacement.close()


@pytest.mark.parametrize("invalid_size", (True, 0, 32 * 1024 * 1024 + 1))
def test_download_rejects_invalid_result_size_before_opening_storage(
    monkeypatch: pytest.MonkeyPatch,
    invalid_size: object,
) -> None:
    with TestClient(app) as client:
        _version, generated, _base = _create_strict_review_job(
            client,
            name=f"Invalid download size claim {invalid_size!r}",
        )
        _persist_strict_review_documents(generated["id"])
        with get_session_factory().begin() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None and isinstance(job.result_json, dict)
            result = deepcopy(job.result_json)
            result["bundle_size_bytes"] = invalid_size
            job.result_json = result
            flag_modified(job, "result_json")
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == job.id,
                    Artifact.kind == "production_bundle",
                )
            )
            assert artifact is not None
            artifact_id = artifact.id

        opened = False

        def must_not_open(
            _expectation: storage_module.StoredObjectExpectation,
            *,
            max_bytes: int,
        ) -> _SimulatedVerifiedDownload:
            nonlocal opened
            opened = True
            raise AssertionError(f"storage opened with cap {max_bytes}")

        monkeypatch.setattr(api_module, "open_verified_stored_object", must_not_open)
        expires = int(datetime.now(UTC).timestamp()) + 60
        signature = api_module.sign_artifact_access(
            artifact_id,
            DEV_ORG_NORDIC,
            expires,
        )
        response = client.get(
            f"/v1/artifacts/{artifact_id}/download?expires={expires}&signature={signature}",
            headers=HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 409
    assert opened is False


def test_generated_status_cannot_claim_a_validation_program_the_plan_did_not_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Generation-plan validation-program mismatch"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json={"include_validation_program": False},
        ).json()
        # This fixture deliberately claims a generated validation program while
        # its exact frozen generation plan records that none was requested.
        _complete_generation(generated["id"], "7" * 64)
        documents = _persist_strict_review_documents(generated["id"])
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)
        before = _review_mutation_snapshot(version["id"])

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    assert _review_mutation_snapshot(version["id"]) == before


@pytest.mark.parametrize("kind", ("stock_selection", "generation_plan"))
@pytest.mark.parametrize("tamper", ("canonical", "noncanonical", "media_type"))
def test_review_rejects_coordinated_standalone_input_tamper_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    tamper: str,
) -> None:
    with TestClient(app) as client:
        version, generated, base = _create_strict_review_job(
            client,
            name=f"Coordinated {kind} {tamper} tamper",
        )
        documents = _persist_strict_review_documents(generated["id"])
        with get_session_factory()() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None and isinstance(job.result_json, dict)
            item = next(
                entry for entry in job.result_json["evidence_artifacts"] if entry["kind"] == kind
            )
            original = documents[str(item["object_key"])]
        content_type = "application/json"
        if tamper == "canonical":
            value = json.loads(original)
            if kind == "stock_selection":
                value["stocks"][0]["quantity"] += 1
            else:
                value["stock_profiles_fingerprint"] = "0" * 64
            payload = canonical_json_bytes(value)
        elif tamper == "noncanonical":
            payload = original + b"\n"
        else:
            payload = original
            content_type = "text/plain"
        _rewrite_bound_review_document(
            generated["id"],
            documents,
            kind=kind,
            payload=payload,
            content_type=content_type,
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)
        before = _review_mutation_snapshot(version["id"])

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)
        rejected_cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Coordinated standalone input tamper must fail",
                "generation_job_id": generated["id"],
            },
        )

    assert listing.status_code == 409
    assert rejected_cam.status_code == 409
    assert _review_mutation_snapshot(version["id"]) == before


@pytest.mark.parametrize(
    "kind",
    ("stock_selection", "generation_plan", "design_review_package_status"),
)
def test_review_requires_every_v4_standalone_document_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    with TestClient(app) as client:
        version, generated, _base = _create_strict_review_job(
            client,
            name=f"Missing mandatory v4 {kind}",
        )
        documents = _persist_strict_review_documents(generated["id"])
        _remove_bound_review_document(generated["id"], documents, kind=kind)
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)
        before = _review_mutation_snapshot(version["id"])

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    assert _review_mutation_snapshot(version["id"]) == before


def _substitute_generation_bundle(job_id: str) -> tuple[str, str, str]:
    """Coordinate result/row substitution while leaving the reviewed manifest intact."""

    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None and isinstance(job.result_json, dict)
        result = deepcopy(job.result_json)
        manifest_sha = str(result["manifest_sha256"])
        rogue_key = f"evidence/{job.id}/substituted-production-bundle"
        rogue_sha = "c" * 64
        result.update(
            bundle_object_key=rogue_key,
            bundle_sha256=rogue_sha,
            bundle_size_bytes=256,
        )
        job.result_json = result
        flag_modified(job, "result_json")
        artifact = session.scalar(
            select(Artifact).where(
                Artifact.generation_job_id == job.id,
                Artifact.kind == "production_bundle",
            )
        )
        assert artifact is not None
        artifact.object_key = rogue_key
        artifact.sha256 = rogue_sha
        artifact.size_bytes = 256
        return manifest_sha, rogue_key, artifact.id


@pytest.mark.parametrize(
    ("endpoint", "actual_manifest_binding"),
    (
        ("listing", None),
        ("download", "e" * 64),
        ("cam_approval", None),
        ("release", "e" * 64),
    ),
)
def test_review_endpoints_reject_coordinated_bundle_substitution(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    actual_manifest_binding: str | None,
) -> None:
    with TestClient(app) as client:
        version, generated, base = _create_strict_review_job(
            client,
            name=f"Bundle substitution {endpoint}",
        )
        if endpoint == "release":
            with get_session_factory()() as session:
                stored_job = session.get(GenerationJob, generated["id"])
                assert stored_job is not None
                review_state = api_module._review_evidence_issues(
                    session,
                    DEV_ORG_NORDIC,
                    stored_job,
                    stream_hash=True,
                    require_cam=True,
                )
            assert review_state == ([], [], True), review_state
            approved = client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={
                    "approval_type": "cam",
                    "reason": "Reviewed before coordinated bundle substitution",
                    "generation_job_id": generated["id"],
                },
            )
            assert approved.status_code == 200, approved.json()
        before = _review_mutation_snapshot(version["id"])
        manifest_sha, rogue_key, bundle_artifact_id = _substitute_generation_bundle(generated["id"])
        bundle_verifications: list[tuple[dict[str, str], bool]] = []

        def verify(
            expectation: storage_module.StoredObjectExpectation,
            *,
            stream_hash: bool,
        ) -> None:
            if expectation.object_key != rogue_key:
                return
            required = dict(expectation.required_metadata)
            bundle_verifications.append((required, stream_hash))
            if required.get("manifest-sha256") != actual_manifest_binding:
                raise api_module.ArtifactIntegrityError("bundle manifest binding does not match")

        monkeypatch.setattr(api_module, "verify_stored_object", verify)
        if endpoint == "download":

            def open_substituted_bundle(
                expectation: storage_module.StoredObjectExpectation,
                *,
                max_bytes: int,
            ) -> _SimulatedVerifiedDownload:
                assert expectation.size_bytes <= max_bytes
                required = dict(expectation.required_metadata)
                bundle_verifications.append((required, False))
                if required.get("manifest-sha256") != actual_manifest_binding:
                    raise api_module.ArtifactIntegrityError(
                        "bundle manifest binding does not match"
                    )
                return _SimulatedVerifiedDownload(expectation)

            monkeypatch.setattr(
                api_module,
                "open_verified_stored_object",
                open_substituted_bundle,
            )
        if endpoint == "listing":
            response = client.get(
                f"/v1/jobs/{generated['id']}/artifacts",
                headers=HEADERS,
            )
        elif endpoint == "download":
            expires = int(datetime.now(UTC).timestamp()) + 60
            signature = api_module.sign_artifact_access(
                bundle_artifact_id,
                DEV_ORG_NORDIC,
                expires,
            )
            response = client.get(
                f"/v1/artifacts/{bundle_artifact_id}/download"
                f"?expires={expires}&signature={signature}",
                headers=HEADERS,
                follow_redirects=False,
            )
        elif endpoint == "cam_approval":
            response = client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={
                    "approval_type": "cam",
                    "reason": "Substituted bundle must not be approved",
                    "generation_job_id": generated["id"],
                },
            )
        else:
            response = client.post(
                f"{base}/release",
                headers=HEADERS,
                json={"release_number": "SWAPPED-BUNDLE", "confirmation": "RELEASE"},
            )

    assert response.status_code == 409
    assert _review_mutation_snapshot(version["id"]) == before
    # Download checks the immutable ledger before provider I/O.  The broader
    # review routes stream-check the claimed bundle first and reject its retained
    # manifest binding, still before any state mutation.
    expected_verifications = (
        []
        if endpoint == "download"
        else [({"manifest-sha256": manifest_sha}, endpoint in {"cam_approval", "release"})]
    )
    assert bundle_verifications == expected_verifications


def test_bundle_binding_storage_outage_is_503_and_nonmutating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        version, generated, _base = _create_strict_review_job(
            client,
            name="Bundle binding storage outage",
        )
        before = _review_mutation_snapshot(version["id"])

        def unavailable(
            expectation: storage_module.StoredObjectExpectation,
            *,
            stream_hash: bool,
        ) -> None:
            if expectation.content_type == "application/zip":
                raise api_module.ArtifactStorageUnavailableError("private provider details")

        monkeypatch.setattr(api_module, "verify_stored_object", unavailable)
        response = client.get(
            f"/v1/jobs/{generated['id']}/artifacts",
            headers=HEADERS,
        )

    assert response.status_code == 503
    assert "private provider" not in str(response.json())
    assert _review_mutation_snapshot(version["id"]) == before


@pytest.mark.parametrize(
    "blocker_code",
    ("TWO_SIDED_REGISTRATION_MISSING", "STOCK_PROFILE_MISSING"),
)
def test_blocked_cam_review_package_is_downloadable_but_cannot_be_cam_approved(
    blocker_code: str,
) -> None:
    production_context = (
        stockless_production_context()
        if blocker_code == "STOCK_PROFILE_MISSING"
        else valid_production_context()
    )
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": f"Blocked CAM review package {blocker_code}"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"], production_context=production_context),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json=production_context,
        ).json()
        documents = _complete_blocked_cam_generation(
            generated["id"],
            blocker_code=blocker_code,
        )

        with get_session_factory()() as session:
            completed = session.get(GenerationJob, generated["id"])
            assert completed is not None and isinstance(completed.result_json, dict)
            result = completed.result_json
            assert result["design_review_package_status"]["blocker_codes"] == [blocker_code]
            assert result["dfm_status"] == (
                "BLOCK" if blocker_code == "STOCK_PROFILE_MISSING" else "PASS"
            )
            assert result["nesting_utilization_ppm"] is None
            assert result["used_sheet_count"] == 0
            assert result["nesting_layouts"] == []
            expectations, expectation_errors = api_module._artifact_expectations(completed)
            assert not expectation_errors
            stored_dfm = api_module.normalize_design_review_dfm_report(
                json.loads(documents[expectations["dfm_report"].object_key])
            )
            stored_status = api_module._design_review_package_status(result)
            assert stored_status is not None
            api_module.validate_design_review_status_dfm_report(stored_status, stored_dfm)
            stored_version = session.get(DesignVersion, completed.design_version_id)
            assert stored_version is not None
            stored_manifest = json.loads(documents[expectations["manifest"].object_key])
            api_module.validate_manifest_context_contract(stored_manifest)
            assert api_module._manifest_context_matches_frozen_job(
                stored_manifest,
                completed,
                stored_version,
            )
            manifest_inventory = api_module.validate_manifest_artifact_entries(
                stored_manifest["artifacts"]
            )
            assert api_module._manifest_evidence_matches_expectations(
                list(manifest_inventory),
                expectations,
            )
            api_module.validate_design_review_status_inventory_entries(
                stored_status,
                manifest_inventory,
            )
            expected_stock_issues = api_module._frozen_stock_missing_issues(stored_version)
            assert expected_stock_issues is not None
            assert api_module._stock_report_matches_frozen_version(
                stored_dfm,
                expected_stock_issues,
            )
            frozen_grain_contract = api_module._frozen_grain_contract(stored_version)
            assert frozen_grain_contract is not None
            expected_grain_issues, expected_missing_grain_issues, _ = frozen_grain_contract
            assert not expected_grain_issues
            assert api_module._grain_report_matches_frozen_version(
                stored_dfm,
                expected_missing_grain_issues,
            )
            stored_readiness = api_module.normalize_workshop_readiness_report(
                result["workshop_readiness"]
            )
            expected_edge_band = api_module._frozen_edge_band_selection_required(stored_version)
            assert expected_edge_band is not None
            api_module.validate_workshop_evidence_binding(
                stored_readiness,
                expected_edge_band_selection_required=expected_edge_band,
                expected_material_grain_binding_required=bool(expected_missing_grain_issues),
                external_evidence=stored_manifest["external_evidence"],
            )
            review_state = api_module._review_evidence_issues(
                session,
                DEV_ORG_NORDIC,
                completed,
                stream_hash=True,
                require_cam=False,
            )
            assert review_state == ([], [], True), review_state

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)
        assert listing.status_code == 200
        artifacts = listing.json()
        kinds = {item["kind"] for item in artifacts}
        assert kinds == {
            "production_bundle",
            "manifest",
            "dfm_report",
            "stock_selection",
            "generation_plan",
            "design_glb",
            "workshop_readiness",
            "design_review_package_status",
        }
        assert "operations" not in kinds
        assert "validation_backplot" not in kinds
        assert not any(kind.startswith("setup_sheet_") for kind in kinds)
        bundle = next(item for item in artifacts if item["kind"] == "production_bundle")
        download = client.get(
            bundle["download_path"],
            headers=HEADERS,
            follow_redirects=False,
        )
        assert download.status_code == 200

        cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "This review package must remain CAM blocked",
                "generation_job_id": generated["id"],
            },
        )
        assert cam.status_code == 409
        release = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={
                "release_number": f"BLOCKED-{blocker_code[:12]}",
                "confirmation": "RELEASE",
            },
        )
        assert release.status_code == 409


@pytest.mark.parametrize("endpoint", ("cam", "release"))
def test_plain_dado_retention_is_a_non_overridable_cam_and_release_gate(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    with TestClient(app) as client:
        version, generated, base = _create_strict_review_job(
            client,
            name=f"Plain DADO retention gate {endpoint}",
        )
        if endpoint == "release":
            approved = client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={
                    "approval_type": "cam",
                    "reason": "Legacy fixture approval before enforcing retention",
                    "generation_job_id": generated["id"],
                },
            )
            assert approved.status_code == 200, approved.json()

        monkeypatch.setattr(
            api_module,
            "_require_resolved_dado_retention",
            _REAL_DADO_RETENTION_GATE,
        )
        monkeypatch.setattr(
            api_module,
            "_frozen_dado_retention_is_unresolved",
            _REAL_DADO_RETENTION_PROJECTION,
        )
        before = _review_mutation_snapshot(version["id"])
        if endpoint == "cam":
            rejected = client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={
                    "approval_type": "cam",
                    "reason": "A review reason cannot provide physical retention",
                    "generation_job_id": generated["id"],
                },
            )
        else:
            rejected = client.post(
                f"{base}/release",
                headers=HEADERS,
                json={"release_number": "NO-RETENTION", "confirmation": "RELEASE"},
            )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE
    assert _review_mutation_snapshot(version["id"]) == before


def test_grain_review_package_records_opaque_evidence_but_remains_cam_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_module, "store_evidence_object", lambda *_args: None)
    directional_spec = valid_spec() | {"material_id": "birch-plywood"}
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Directional material review"},
        ).json()
        draft = client.put(
            f"/v1/projects/{project['id']}/draft",
            headers=HEADERS,
            json={
                "expected_draft_revision": 0,
                "template_id": "shelving",
                "spec": directional_spec,
                "workspace_spec": valid_workspace_intent(),
            },
        ).json()
        uploaded = client.post(
            f"/v1/projects/{project['id']}/evidence",
            headers=HEADERS,
            data={
                "evidence_type": "material_grain",
                "rule_id": DFM_GRAIN_BLOCKER_CODE,
                "catalog_id": "supplier-grain-note",
                "catalog_version": "2026.1",
                "design_hash": draft["design_hash"],
            },
            files={
                "document": (
                    "grain-note.png",
                    b"\x89PNG\r\n\x1a\nopaque-grain-document",
                    "image/png",
                )
            },
        )
        assert uploaded.status_code == 201

        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"], directional_spec),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200

        invalid_approval = design_approval_payload(client, base)
        grain_override = next(
            item
            for item in invalid_approval["warning_overrides"]
            if item["rule_id"] == DFM_GRAIN_BLOCKER_CODE
        )
        grain_override["evidence_ids"] = [uploaded.json()["id"]]
        rejected = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json=invalid_approval,
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "DFM_GRAIN_STRUCTURED_BINDING_REQUIRED"

        approve_design(client, base)
        state = client.get(
            f"/v1/projects/{project['id']}/production-state",
            headers=HEADERS,
        ).json()
        stored_grain_override = next(
            item
            for item in state["approvals"][0]["overrides_json"]
            if item["rule_id"] == DFM_GRAIN_BLOCKER_CODE
        )
        assert stored_grain_override["evidence_status"] == "acknowledged_unresolved"
        assert stored_grain_override["external_evidence"] == []

        generated = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json=valid_production_context() | {"external_evidence_ids": [uploaded.json()["id"]]},
        )
        assert generated.status_code == 202
        documents = _complete_blocked_cam_generation(
            generated.json()["id"],
            blocker_code=DFM_GRAIN_BLOCKER_CODE,
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        listing = client.get(
            f"/v1/jobs/{generated.json()['id']}/artifacts",
            headers=HEADERS,
        )
        assert listing.status_code == 200
        bundle = next(item for item in listing.json() if item["kind"] == "production_bundle")
        download = client.get(
            bundle["download_path"],
            headers=HEADERS,
            follow_redirects=False,
        )
        assert download.status_code == 200
        assert len(calls) == 12
        cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Opaque evidence must not unlock CAM",
                "generation_job_id": generated.json()["id"],
            },
        )
        release = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "GRAIN-BLOCKED", "confirmation": "RELEASE"},
        )

    assert cam.status_code == release.status_code == 409
    with get_session_factory()() as session:
        job = session.get(GenerationJob, generated.json()["id"])
        assert job is not None and isinstance(job.result_json, dict)
        assert job.result_json["dfm_status"] == "BLOCK"
        assert job.result_json["design_review_package_status"]["blocker_codes"] == [
            DFM_GRAIN_BLOCKER_CODE
        ]
        assert job.request_json["external_evidence"][0]["evidence_type"] == "material_grain"
        material_grain = next(
            item
            for item in job.result_json["workshop_readiness"]["workshop_evidence"]
            if item["code"] == "MATERIAL_GRAIN"
        )
        assert material_grain["status"] == "EXTERNAL_EVIDENCE_REQUIRED"
        assert "not a structured stock-grain axis" in material_grain["evidence"]


@pytest.mark.parametrize(
    ("material_id", "blocker_code", "forged_binding_required", "forged_status"),
    (
        (
            "birch-plywood",
            DFM_GRAIN_BLOCKER_CODE,
            False,
            "VERIFIED",
        ),
        (
            "mdf",
            "STOCK_PROFILE_MISSING",
            True,
            "EXTERNAL_EVIDENCE_REQUIRED",
        ),
    ),
    ids=("directional-suppression", "non-directional-fabrication"),
)
def test_review_listing_rejects_checksum_bound_material_grain_readiness_fabrication(
    monkeypatch: pytest.MonkeyPatch,
    material_id: str,
    blocker_code: str,
    forged_binding_required: bool,
    forged_status: str,
) -> None:
    def forge_material_grain_requirement(readiness: dict[str, Any]) -> None:
        forged = build_workshop_readiness_report(
            authoritative_cad=True,
            dfm_passed=False,
            operation_count=0,
            setup_count=0,
            validation_backplot=False,
            validation_program=False,
            edge_band_selection_required=True,
            material_grain_binding_required=forged_binding_required,
        ).as_dict()
        material_grain = next(
            item for item in forged["workshop_evidence"] if item["code"] == "MATERIAL_GRAIN"
        )
        assert material_grain["status"] == forged_status
        readiness.clear()
        readiness.update(forged)

    spec = valid_spec() | {"material_id": material_id}
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": f"Material-grain readiness fabrication {material_id}"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"], spec),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        documents = _complete_blocked_cam_generation(
            generated["id"],
            blocker_code=blocker_code,
            readiness_mutator=forge_material_grain_requirement,
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    assert len(calls) == 6


def test_worker_stock_grain_projection_matches_frozen_api_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custombuild_domain import BookcaseDesignSpec
    from custombuild_manufacturing import (
        generation_plan_artifact,
        stock_grain_binding_issues,
        stock_selection_artifact,
    )
    from custombuild_manufacturing.adapters import adapt_design_result
    from custombuild_manufacturing.pipeline import _assign_parts_to_stock
    from custombuild_worker import tasks as worker_tasks

    class WorkerStocksCaptured(RuntimeError):
        pass

    captured: dict[str, Any] = {}

    def capture_worker_stocks(
        design: Any,
        *,
        stock: Any,
        **_kwargs: Any,
    ) -> Any:
        stocks = tuple(stock)
        adapted = adapt_design_result(design)
        grouped_parts, selection_issues = _assign_parts_to_stock(adapted.parts, stocks)
        assert not selection_issues
        captured["stock_ids"] = tuple(item.stock_id for item in stocks)
        captured["issues"] = tuple(
            issue
            for selected_stock, selected_parts in grouped_parts
            for issue in stock_grain_binding_issues(selected_parts, selected_stock)
        )
        captured["stock_selection"] = stock_selection_artifact(
            stocks,
            grouped_parts,
            unmatched_part_ids=(),
        ).data
        captured["generation_plan"] = generation_plan_artifact(
            machine=_kwargs["machine"],
            stocks=stocks,
            two_sided_registration_by_stock=None,
            validation_program_requested=bool(_kwargs["include_validation_program"]),
        ).data
        raise WorkerStocksCaptured

    monkeypatch.setattr(worker_tasks, "build_production_bundle", capture_worker_stocks)
    for document_builder in (
        "assembly_manual_pdf",
        "assembly_readiness_json",
        "bom_pdf",
        "hardware_csv",
        "labels_pdf",
        "qa_protocol_pdf",
        "validation_report_pdf",
    ):
        monkeypatch.setattr(worker_tasks, document_builder, lambda *_args: b"fixture")

    directional_spec = valid_spec() | {"material_id": "birch-plywood"}
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Worker and API grain identity"},
        ).json()
        version_payload_value = version_payload(project["id"], directional_spec)
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload_value,
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json=valid_production_context(),
        ).json()

    with get_session_factory()() as session:
        job = session.get(GenerationJob, generated["id"])
        stored_version = session.get(DesignVersion, version["id"])
        assert job is not None and stored_version is not None
        frozen_contract = api_module._frozen_grain_contract(stored_version)
        assert frozen_contract is not None
        expected_issues, _, _ = frozen_contract
        expected_stock_selection = api_module._frozen_stock_selection_snapshot(stored_version)
        expected_generation_plan = api_module._frozen_generation_plan_snapshot(job, stored_version)
        assert expected_stock_selection is not None
        assert expected_generation_plan is not None
        spec = BookcaseDesignSpec.model_validate(stored_version.spec_json)
        request = job.request_json
        expected_stock_ids = (
            (
                f"stock-carcass-{spec.material.material_id}-{spec.material.version}-"
                f"{spec.parameters.actual_thickness_um}um-"
                f"{int(round(float(request['stock_width_mm']) * 1000))}x"
                f"{int(round(float(request['stock_height_mm']) * 1000))}um"
            ),
            (
                f"stock-back-{spec.back_material.material_id}-{spec.back_material.version}-"
                f"{spec.parameters.back_thickness_um}um-"
                f"{int(round(float(request['back_stock_width_mm']) * 1000))}x"
                f"{int(round(float(request['back_stock_height_mm']) * 1000))}um"
            ),
        )
        storage_lease_token = str(uuid4())
        job.lease_token = storage_lease_token
        with pytest.raises(WorkerStocksCaptured):
            worker_tasks._generate(
                job,
                stored_version,
                lease_token=storage_lease_token,
                lease_guard=worker_tasks._GenerationLeaseGuard(
                    job.organization_id,
                    storage_lease_token,
                ),
            )

    assert captured["stock_ids"] == expected_stock_ids
    assert len(set(captured["stock_ids"])) == 2
    assert canonical_json_bytes(captured["issues"]) == canonical_json_bytes(expected_issues)
    assert captured["stock_selection"] == expected_stock_selection
    assert captured["generation_plan"] == expected_generation_plan


def test_stockless_directional_review_binds_the_exact_missing_grain_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directional_spec = valid_spec() | {"material_id": "birch-plywood"}
    production_context = stockless_production_context()
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Stockless directional review"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(
                project["id"],
                directional_spec,
                production_context=production_context,
            ),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json=production_context,
        ).json()
        documents = _complete_blocked_cam_generation(
            generated["id"],
            blocker_code="STOCK_PROFILE_MISSING",
            dfm_report_payload=_stock_blocked_dfm_report_for_version(version["id"]),
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 200
    assert len(calls) == 6


@pytest.mark.parametrize("mutation", ("omit", "affected-parts"))
def test_stockless_directional_review_rejects_rehashed_grain_warning_tampering(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    def mutate(grain_issues: list[dict[str, Any]]) -> None:
        assert grain_issues
        if mutation == "omit":
            grain_issues.clear()
        else:
            grain_issues[0]["inputs"]["affected_part_ids"] = ["invented-part"]

    directional_spec = valid_spec() | {"material_id": "birch-plywood"}
    production_context = stockless_production_context()
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": f"Stockless grain tamper {mutation}"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(
                project["id"],
                directional_spec,
                production_context=production_context,
            ),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json=production_context,
        ).json()
        documents = _complete_blocked_cam_generation(
            generated["id"],
            blocker_code="STOCK_PROFILE_MISSING",
            dfm_report_payload=_stock_blocked_dfm_report_for_version(
                version["id"],
                grain_mutator=mutate,
            ),
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    assert len(calls) == 6


def test_grain_review_rejects_rehashed_affected_part_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directional_spec = valid_spec() | {"material_id": "birch-plywood"}
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Grain affected-part substitution"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"], directional_spec),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        with get_session_factory()() as session:
            stored_version = session.get(DesignVersion, version["id"])
            assert stored_version is not None
            grain_contract = api_module._frozen_grain_contract(stored_version)
            assert grain_contract is not None
            matched_issues, _, _ = grain_contract
        tampered_report = json.loads(
            canonical_json_bytes(DFMReport(matched_issues, engine_version=DFM_ENGINE_VERSION))
        )
        tampered_report["issues"][0]["inputs"]["affected_part_ids"] = ["invented-part"]
        documents = _complete_blocked_cam_generation(
            generated["id"],
            blocker_code=DFM_GRAIN_BLOCKER_CODE,
            dfm_report_payload=tampered_report,
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    assert len(calls) == 6


def test_non_directional_material_rejects_coordinated_grain_blocker_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fabricated = json.loads(
        canonical_json_bytes(
            DFMReport(
                (
                    DFMIssue(
                        DFM_GRAIN_BLOCKER_CODE,
                        Severity.BLOCK,
                        DFM_GRAIN_RULE_MESSAGE,
                        inputs={
                            "binding_status": "MISSING_INFORMATION",
                            "assessment_phase": "STOCK_MATCHED",
                            "stock_id": "stock-mdf-2440.0x1220.0",
                            "material_id": "mdf",
                            "material_version": "screening-2026.1",
                            "stock_grain_direction": "UNBOUND",
                            "required_part_grain_directions": ("X",),
                            "affected_part_ids": ("invented-directional-part",),
                        },
                        suggestion=DFM_GRAIN_REQUIRED_ACTION,
                    ),
                ),
                engine_version=DFM_ENGINE_VERSION,
            )
        )
    )
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Non-directional grain substitution"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        documents = _complete_blocked_cam_generation(
            generated["id"],
            blocker_code=DFM_GRAIN_BLOCKER_CODE,
            dfm_report_payload=fabricated,
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    assert len(calls) == 6


@pytest.mark.parametrize(
    "dfm_report_payload",
    (
        valid_dfm_report(),
        json.loads(
            canonical_json_bytes(
                DFMReport(
                    (
                        DFMIssue(
                            "OTHER_BLOCKER",
                            Severity.BLOCK,
                            "A different DFM blocker.",
                        ),
                    ),
                    engine_version=DFM_ENGINE_VERSION,
                )
            )
        ),
    ),
)
def test_stockless_review_listing_rejects_checksum_bound_dfm_substitution(
    monkeypatch: pytest.MonkeyPatch,
    dfm_report_payload: dict[str, Any],
) -> None:
    with TestClient(app) as client:
        issue_code = (
            dfm_report_payload["issues"][0]["code"] if dfm_report_payload["issues"] else "EMPTY"
        )
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": f"Stockless DFM substitution {issue_code}"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        documents = _complete_blocked_cam_generation(
            generated["id"],
            blocker_code="STOCK_PROFILE_MISSING",
            dfm_report_payload=dfm_report_payload,
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    assert len(calls) == 6


def test_stockless_review_listing_rejects_coordinated_glb_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Bound GLB substitution"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        documents = _complete_blocked_cam_generation(
            generated["id"],
            blocker_code="STOCK_PROFILE_MISSING",
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        with get_session_factory().begin() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None and isinstance(job.result_json, dict)
            result = deepcopy(job.result_json)
            glb = next(
                item for item in result["evidence_artifacts"] if item["kind"] == "design_glb"
            )
            glb["sha256"] = "9" * 64
            glb["size_bytes"] = 129
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == job.id,
                    Artifact.kind == "design_glb",
                )
            )
            assert artifact is not None
            artifact.sha256 = "9" * 64
            artifact.size_bytes = 129
            job.result_json = result
            flag_modified(job, "result_json")

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    assert len(calls) == 6


def test_stockless_review_listing_rejects_fabricated_workshop_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fabricate_wall_anchor(readiness: dict[str, Any]) -> None:
        fabricated = build_workshop_readiness_report(
            authoritative_cad=True,
            dfm_passed=False,
            operation_count=0,
            setup_count=0,
            validation_backplot=False,
            validation_program=False,
            edge_band_selection_required=True,
            external_evidence=(
                {
                    "evidence_type": "wall_anchor",
                    "catalog_id": "invented-anchor",
                    "catalog_version": "1.0.0",
                    "sha256": "9" * 64,
                },
            ),
        ).as_dict()
        readiness.clear()
        readiness.update(fabricated)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Fabricated workshop verification"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        documents = _complete_blocked_cam_generation(
            generated["id"],
            blocker_code="STOCK_PROFILE_MISSING",
            readiness_mutator=fabricate_wall_anchor,
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    assert len(calls) == 6


def test_stockless_review_listing_rejects_suppressed_edge_band_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def suppress_edge_band_requirement(readiness: dict[str, Any]) -> None:
        suppressed = build_workshop_readiness_report(
            authoritative_cad=True,
            dfm_passed=False,
            operation_count=0,
            setup_count=0,
            validation_backplot=False,
            validation_program=False,
            edge_band_selection_required=False,
        ).as_dict()
        readiness.clear()
        readiness.update(suppressed)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Suppressed edge-band requirement"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        documents = _complete_blocked_cam_generation(
            generated["id"],
            blocker_code="STOCK_PROFILE_MISSING",
            readiness_mutator=suppress_edge_band_requirement,
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    assert len(calls) == 6


def test_stockless_result_rejects_verified_dfm_or_nonzero_nesting_claims() -> None:
    status = blocked_design_review_package_status(("STOCK_PROFILE_MISSING",)).as_dict()
    base_result = {
        "authoritative_geometry": True,
        "dfm_status": "BLOCK",
        "design_review_package_status": status,
        "machine_program_mode": "CAM_BLOCKED",
        "production_machine_program": False,
        "nesting_utilization_ppm": None,
        "used_sheet_count": 0,
        "nesting_layouts": [],
        "workshop_readiness": build_workshop_readiness_report(
            authoritative_cad=True,
            dfm_passed=False,
            operation_count=0,
            setup_count=0,
            validation_backplot=False,
            validation_program=False,
        ).as_dict(),
    }
    assert api_module._blocked_cam_review_package_is_valid(base_result) is True

    verified_dfm = deepcopy(base_result)
    verified_dfm["workshop_readiness"] = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=True,
        operation_count=0,
        setup_count=0,
        validation_backplot=False,
        validation_program=False,
    ).as_dict()
    utilization = deepcopy(base_result)
    utilization["nesting_utilization_ppm"] = 1
    missing_utilization = deepcopy(base_result)
    del missing_utilization["nesting_utilization_ppm"]
    sheets = deepcopy(base_result)
    sheets["used_sheet_count"] = 1
    boolean_sheets = deepcopy(base_result)
    boolean_sheets["used_sheet_count"] = False
    float_sheets = deepcopy(base_result)
    float_sheets["used_sheet_count"] = 0.0
    layouts = deepcopy(base_result)
    layouts["nesting_layouts"] = [{"stock_id": "invented"}]
    wrong_dfm_status = deepcopy(base_result)
    wrong_dfm_status["dfm_status"] = "PASS"

    for unsafe in (
        verified_dfm,
        utilization,
        missing_utilization,
        sheets,
        boolean_sheets,
        float_sheets,
        layouts,
        wrong_dfm_status,
    ):
        assert api_module._blocked_cam_review_package_is_valid(unsafe) is False


@pytest.mark.parametrize(
    ("forbidden_kind", "content_type"),
    (
        ("operations", "application/json"),
        ("validation_backplot", "image/svg+xml"),
        ("setup_sheet_001", "image/svg+xml"),
        ("cam_rogue", "application/octet-stream"),
        ("nesting_rogue", "image/svg+xml"),
        ("machine_validation_001", "text/x-gcode"),
        ("rogue.NGC", "text/x-gcode"),
        ("tool_list", "text/csv"),
        ("stock_purchase_schedule", "text/csv"),
        ("quality_measurement_plan", "application/json"),
        ("gcode", "text/x-gcode"),
        ("toolpath", "application/octet-stream"),
        ("machine_program", "text/x-gcode"),
        ("operations_plan", "application/json"),
        ("setup_plan", "image/svg+xml"),
        ("tooling_plan", "text/csv"),
    ),
)
def test_blocked_cam_review_package_rejects_checksum_bound_cam_evidence(
    forbidden_kind: str,
    content_type: str,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": f"Blocked CAM evidence contradiction {forbidden_kind}"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_blocked_cam_generation(generated["id"])

        with get_session_factory().begin() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            assert isinstance(job.result_json, dict)
            result = deepcopy(job.result_json)
            contradictory = {
                "kind": forbidden_kind,
                "object_key": f"evidence/{job.id}/{forbidden_kind}",
                "sha256": "e" * 64,
                "size_bytes": 128,
                "content_type": content_type,
            }
            result["evidence_artifacts"].append(contradictory)
            job.result_json = result
            flag_modified(job, "result_json")
            session.add(
                Artifact(
                    organization_id=DEV_ORG_NORDIC,
                    generation_job_id=job.id,
                    **contradictory,
                )
            )

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    assert listing.json()["detail"] == (
        "Production evidence failed integrity verification; regenerate the package"
    )


@pytest.mark.parametrize(
    "aliased_kind",
    ("Design_Review_Package_Status", "Workshop_Readiness", "Dfm_Report"),
)
def test_review_listing_rejects_case_aliased_semantic_evidence_kind(
    aliased_kind: str,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": f"Aliased semantic evidence {aliased_kind}"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_blocked_cam_generation(generated["id"])

        with get_session_factory().begin() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            assert isinstance(job.result_json, dict)
            result = deepcopy(job.result_json)
            aliased = {
                "kind": aliased_kind,
                "object_key": f"evidence/{job.id}/{aliased_kind}",
                "sha256": "e" * 64,
                "size_bytes": 128,
                "content_type": "application/json",
            }
            result["evidence_artifacts"].append(aliased)
            job.result_json = result
            flag_modified(job, "result_json")
            session.add(
                Artifact(
                    organization_id=DEV_ORG_NORDIC,
                    generation_job_id=job.id,
                    **aliased,
                )
            )

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    assert listing.json()["detail"] == (
        "Production evidence failed integrity verification; regenerate the package"
    )


@pytest.mark.parametrize(
    ("forbidden_path", "forbidden_role"),
    (
        ("machine-validation/injected.validation.ngc", "WORKER_NOTE"),
        ("cam/rogue.ngc", "WORKER_NOTE"),
        ("CAM/rogue.NGC", "WORKER_NOTE"),
        ("nesting/x.svg", "WORKER_NOTE"),
        ("review/x.ngc", "WORKER_NOTE"),
        ("review/backplot.svg", "VALIDATION_BACKPLOT"),
        ("review/../cam/rogue.tap", "WORKER_NOTE"),
        ("review\\cam\\rogue.tap", "WORKER_NOTE"),
        ("/cam/rogue.tap", "WORKER_NOTE"),
        ("C:/cam/rogue.tap", "WORKER_NOTE"),
    ),
)
def test_blocked_cam_review_package_rejects_cam_path_in_bound_manifest(
    monkeypatch: pytest.MonkeyPatch,
    forbidden_path: str,
    forbidden_role: str,
) -> None:
    def add_operations_entry(manifest: dict[str, Any]) -> None:
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, list)
        artifacts.append(
            {
                "path": forbidden_path,
                "media_type": "application/octet-stream",
                "role": forbidden_role,
                "size_bytes": 128,
                "sha256": "e" * 64,
            }
        )
        artifacts.sort(key=lambda item: str(item["path"]))

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={
                "name": (
                    "Blocked CAM manifest contradiction "
                    + hashlib.sha256(forbidden_path.encode()).hexdigest()[:12]
                )
            },
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        documents = _complete_blocked_cam_generation(
            generated["id"],
            manifest_mutator=add_operations_entry,
        )
        calls: list[tuple[str, int]] = []
        metadata_calls: list[str] = []
        _install_strict_review_reader(monkeypatch, documents, calls)
        monkeypatch.setattr(
            api_module,
            "verify_stored_object",
            lambda expectation, *, stream_hash: metadata_calls.append(expectation.object_key),
        )
        with get_session_factory()() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            bundle_artifact = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == generated["id"],
                    Artifact.kind == "production_bundle",
                )
            )
            assert bundle_artifact is not None

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)
        first_reads = tuple(key for key, _ in calls)
        first_metadata = tuple(metadata_calls)
        expires = int(datetime.now(UTC).timestamp()) + 60
        signature = api_module.sign_artifact_access(
            bundle_artifact.id,
            DEV_ORG_NORDIC,
            expires,
        )
        download = client.get(
            f"/v1/artifacts/{bundle_artifact.id}/download?expires={expires}&signature={signature}",
            headers=HEADERS,
            follow_redirects=False,
        )

    assert listing.status_code == 409
    assert download.status_code == 409
    assert len(first_reads) == 6
    assert len(set(first_reads)) == 6
    assert not (set(first_reads) & set(first_metadata))
    assert len(calls) == 12
    assert tuple(key for key, _ in calls[6:]) == first_reads


@pytest.mark.parametrize(
    ("rogue_path", "rogue_role"),
    (
        ("review/rogue-readiness.json", "WORKSHOP_READINESS_REPORT"),
        ("Validation/Workshop-Readiness.JSON", "WORKER_NOTE"),
    ),
)
def test_blocked_review_listing_rejects_duplicate_or_aliased_readiness_entry(
    monkeypatch: pytest.MonkeyPatch,
    rogue_path: str,
    rogue_role: str,
) -> None:
    def add_rogue_readiness(manifest: dict[str, Any]) -> None:
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, list)
        artifacts.append(
            {
                "path": rogue_path,
                "media_type": "application/json",
                "role": rogue_role,
                "size_bytes": 64,
                "sha256": "e" * 64,
            }
        )
        artifacts.sort(key=lambda item: str(item["path"]))

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": f"Duplicate readiness {rogue_path}"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        documents = _complete_blocked_cam_generation(
            generated["id"],
            manifest_mutator=add_rogue_readiness,
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    assert len(calls) == 6


def test_listing_rejects_generated_status_when_manifest_omits_claimed_cam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Generated status missing manifest CAM"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_blocked_cam_generation(generated["id"])
        documents = _convert_to_generated_status_with_missing_manifest_cam(generated["id"])
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)

        listing = client.get(f"/v1/jobs/{generated['id']}/artifacts", headers=HEADERS)

    assert listing.status_code == 409
    # Fail as soon as the four documents needed to disprove the generated CAM
    # claim have been checked; no unrelated provider reads should follow.
    assert len(calls) == 4


@pytest.mark.parametrize("endpoint", ("listing", "download"))
def test_blocked_review_semantic_storage_failure_is_503_and_nonmutating(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": f"Blocked review storage failure {endpoint}"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_blocked_cam_generation(generated["id"])
        with get_session_factory()() as session:
            bundle = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == generated["id"],
                    Artifact.kind == "production_bundle",
                )
            )
            assert bundle is not None
        before = _review_mutation_snapshot(version["id"])

        def unavailable(
            _expectation: storage_module.StoredObjectExpectation,
            *,
            max_bytes: int,
        ) -> bytes:
            assert max_bytes in (64 * 1024, MAX_CORE_DOCUMENT_BYTES)
            raise storage_module.ArtifactStorageUnavailableError(
                "private-provider-name unavailable"
            )

        monkeypatch.setattr(api_module, "read_verified_stored_object", unavailable)
        if endpoint == "listing":
            response = client.get(
                f"/v1/jobs/{generated['id']}/artifacts",
                headers=HEADERS,
            )
        else:
            expires = int(datetime.now(UTC).timestamp()) + 60
            signature = api_module.sign_artifact_access(
                bundle.id,
                DEV_ORG_NORDIC,
                expires,
            )
            response = client.get(
                f"/v1/artifacts/{bundle.id}/download?expires={expires}&signature={signature}",
                headers=HEADERS,
                follow_redirects=False,
            )

    assert response.status_code == 503
    assert "private-provider-name" not in str(response.json())
    assert _review_mutation_snapshot(version["id"]) == before


@pytest.mark.parametrize(
    "payload",
    (
        b' {"valid":true}',
        b'\xef\xbb\xbf{"valid":true}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"duplicate":1,"duplicate":1}',
        b"[]",
        b'\xff{"valid":true}',
        b'{"value":' + (b"[" * 1100) + b"0" + (b"]" * 1100) + b"}",
    ),
)
def test_review_json_parser_rejects_ambiguous_or_unsafe_payloads(
    payload: bytes,
) -> None:
    with pytest.raises(storage_module.ArtifactIntegrityError):
        api_module._strict_canonical_json_object(payload)


@pytest.mark.parametrize("legacy", (False, True), ids=("v2", "full-legacy-v1"))
def test_cam_and_release_accept_exact_checksum_bound_readiness_documents(
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
) -> None:
    with TestClient(app) as client:
        version, generated, base = _create_strict_review_job(
            client,
            name=f"Strict readiness document {'v1' if legacy else 'v2'}",
        )
        documents = _persist_strict_review_documents(
            generated["id"],
            readiness=valid_workshop_readiness(legacy=legacy),
            omit_program_fields=legacy,
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)
        separately_verified: list[str] = []

        def record_other_verification(
            expectation: storage_module.StoredObjectExpectation,
            *,
            stream_hash: bool,
        ) -> None:
            assert stream_hash is True
            separately_verified.append(expectation.object_key)

        monkeypatch.setattr(
            api_module,
            "verify_stored_object",
            record_other_verification,
        )

        cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Exact checksum-bound review evidence accepted",
                "generation_job_id": generated["id"],
            },
        )
        assert cam.status_code == 200
        released = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "STRICT-1", "confirmation": "RELEASE"},
        )

    assert released.status_code == 200
    assert released.json()["status"] == "released"
    assert len(calls) == 12
    assert [max_bytes for _key, max_bytes in calls].count(64 * 1024) == 4
    assert [max_bytes for _key, max_bytes in calls].count(MAX_CORE_DOCUMENT_BYTES) == 8
    assert set(documents).isdisjoint(separately_verified)
    with get_session_factory()() as session:
        persisted = session.get(DesignVersion, version["id"])
        assert persisted is not None
        assert persisted.status == DesignStatus.released


def test_cam_and_release_reject_result_readiness_tamper_with_objects_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        version, generated, base = _create_strict_review_job(
            client,
            name="Tampered result readiness binding",
        )
        documents = _persist_strict_review_documents(generated["id"])
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)
        with get_session_factory().begin() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            assert isinstance(job.result_json, dict)
            valid_result = deepcopy(job.result_json)
            tampered_result = deepcopy(valid_result)
            tampered_result["workshop_readiness"]["software_evidence"][0]["evidence"] += (
                " tampered after storage"
            )
            assert api_module._workshop_readiness_is_valid(tampered_result) is True
            job.result_json = tampered_result
            flag_modified(job, "result_json")

        before_cam = _review_mutation_snapshot(version["id"])
        rejected_cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Tampered job result must fail",
                "generation_job_id": generated["id"],
            },
        )
        assert rejected_cam.status_code == 409
        assert _review_mutation_snapshot(version["id"]) == before_cam

        with get_session_factory().begin() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            job.result_json = deepcopy(valid_result)
            flag_modified(job, "result_json")
        approved_cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Original immutable readiness reviewed",
                "generation_job_id": generated["id"],
            },
        )
        assert approved_cam.status_code == 200

        with get_session_factory().begin() as session:
            job = session.get(GenerationJob, generated["id"])
            assert job is not None
            job.result_json = deepcopy(tampered_result)
            flag_modified(job, "result_json")
        before_release = _review_mutation_snapshot(version["id"])
        rejected_release = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "TAMPERED", "confirmation": "RELEASE"},
        )
        assert rejected_release.status_code == 409
        assert _review_mutation_snapshot(version["id"]) == before_release

    assert len(calls) == 18
    assert [max_bytes for _key, max_bytes in calls].count(64 * 1024) == 6
    assert [max_bytes for _key, max_bytes in calls].count(MAX_CORE_DOCUMENT_BYTES) == 12


@pytest.mark.parametrize(
    "malformation",
    (
        "missing",
        "duplicate",
        "sha-mismatch",
        "top-level-extra",
        "top-level-missing",
        "schema-version",
        "generation-context",
        "production-context-hash",
        "release-scope",
        "machine-use",
        "physical-flag-type",
        "cad-status",
        "checksum-scope",
        "project-id",
        "revision",
        "design-hash",
        "engine-version",
        "production-engine-context",
        "entry-extra-key",
        "entry-missing-key",
        "entry-media-type",
        "entry-role",
        "entry-size-type",
    ),
)
def test_cam_rejects_checksum_valid_manifest_contract_malformation(
    monkeypatch: pytest.MonkeyPatch,
    malformation: str,
) -> None:
    def mutate_manifest(manifest: dict[str, Any]) -> None:
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, list)
        target = next(
            item for item in artifacts if item["path"] == "validation/workshop-readiness.json"
        )
        if malformation == "missing":
            artifacts.clear()
        elif malformation == "duplicate":
            artifacts.append(deepcopy(target))
        elif malformation == "sha-mismatch":
            target["sha256"] = "e" * 64
        elif malformation == "top-level-extra":
            manifest["unexpected"] = "unsafe"
        elif malformation == "top-level-missing":
            manifest.pop("checksum_scope")
        elif malformation == "schema-version":
            manifest["schema_version"] = "custombuild.production-manifest.v3"
        elif malformation == "generation-context":
            manifest["generation_context_hash"] = "e" * 64
        elif malformation == "production-context-hash":
            manifest["production_context_hash"] = "e" * 64
        elif malformation == "release-scope":
            manifest["release_scope"] = "production"
        elif malformation == "machine-use":
            manifest["machine_use"] = "physical_cutting"
        elif malformation == "physical-flag-type":
            manifest["physical_cutting_authorized"] = 0
        elif malformation == "cad-status":
            manifest["cad_status"] = "FAILED"
        elif malformation == "checksum-scope":
            manifest["checksum_scope"] = "payload files"
        elif malformation == "project-id":
            manifest["project_id"] = "other-project"
        elif malformation == "revision":
            manifest["revision"] = "999"
        elif malformation == "design-hash":
            manifest["design_hash"] = "0" * 64
        elif malformation == "engine-version":
            manifest["engine_version"] = "unsafe-engine"
        elif malformation == "production-engine-context":
            manifest["production_engine_context"] = {
                **manifest["production_engine_context"],
                "app_version": "substituted-app",
            }
        elif malformation == "entry-extra-key":
            target["unexpected"] = "unsafe"
        elif malformation == "entry-missing-key":
            target.pop("role")
        elif malformation == "entry-media-type":
            target["media_type"] = "text/plain"
        elif malformation == "entry-role":
            target["role"] = "OTHER_REPORT"
        elif malformation == "entry-size-type":
            target["size_bytes"] = True
        else:
            raise AssertionError(f"unknown manifest malformation: {malformation}")

    with TestClient(app) as client:
        version, generated, base = _create_strict_review_job(
            client,
            name=f"Manifest readiness inventory {malformation}",
        )
        documents = _persist_strict_review_documents(
            generated["id"],
            manifest_mutator=mutate_manifest,
            rehash_manifest_context=malformation != "production-context-hash",
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)
        before = _review_mutation_snapshot(version["id"])

        rejected = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Malformed manifest inventory must fail",
                "generation_job_id": generated["id"],
            },
        )

    assert rejected.status_code == 409
    assert _review_mutation_snapshot(version["id"]) == before
    assert len(calls) == 6
    assert sorted(max_bytes for _key, max_bytes in calls) == [
        64 * 1024,
        64 * 1024,
        MAX_CORE_DOCUMENT_BYTES,
        MAX_CORE_DOCUMENT_BYTES,
        MAX_CORE_DOCUMENT_BYTES,
        MAX_CORE_DOCUMENT_BYTES,
    ]


def test_cam_rejects_coordinated_noncanonical_readiness_media_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        version, generated, base = _create_strict_review_job(
            client,
            name="Coordinated readiness media-type tamper",
        )
        documents = _persist_strict_review_documents(
            generated["id"],
            readiness_content_type="text/plain",
        )
        calls: list[tuple[str, int]] = []
        _install_strict_review_reader(monkeypatch, documents, calls)
        before = _review_mutation_snapshot(version["id"])

        rejected = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Noncanonical readiness media type must fail",
                "generation_job_id": generated["id"],
            },
        )

    assert rejected.status_code == 409
    assert _review_mutation_snapshot(version["id"]) == before
    assert calls == []


@pytest.mark.parametrize(
    ("storage_error", "expected_status"),
    (
        pytest.param(
            storage_module.ArtifactIntegrityError("private/missing/readiness-object"),
            409,
            id="integrity",
        ),
        pytest.param(
            storage_module.ArtifactStorageUnavailableError("private-provider-endpoint unavailable"),
            503,
            id="availability",
        ),
    ),
)
def test_semantic_document_read_failures_are_generic_and_do_not_mutate_review(
    monkeypatch: pytest.MonkeyPatch,
    storage_error: Exception,
    expected_status: int,
) -> None:
    def fail_read(
        _expectation: storage_module.StoredObjectExpectation,
        *,
        max_bytes: int,
    ) -> bytes:
        assert max_bytes in (64 * 1024, MAX_CORE_DOCUMENT_BYTES)
        raise storage_error

    monkeypatch.setattr(api_module, "read_verified_stored_object", fail_read)
    with TestClient(app) as client:
        version, generated, base = _create_strict_review_job(
            client,
            name=f"Semantic document storage failure {expected_status}",
        )
        before = _review_mutation_snapshot(version["id"])
        rejected = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Storage failure must fail closed",
                "generation_job_id": generated["id"],
            },
        )

    assert rejected.status_code == expected_status
    assert "private" not in str(rejected.json())
    assert _review_mutation_snapshot(version["id"]) == before


@pytest.mark.parametrize("malformation", ("empty_arrays", "partial_arrays"))
def test_cam_endpoint_rejects_malformed_readiness_without_mutation(
    malformation: str,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": f"Malformed readiness endpoint {malformation}"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(generated["id"], "7" * 64)

        with get_session_factory().begin() as session:
            persisted_job = session.get(GenerationJob, generated["id"])
            assert persisted_job is not None
            corrupted_result = deepcopy(persisted_job.result_json)
            readiness = corrupted_result["workshop_readiness"]
            if malformation == "empty_arrays":
                readiness["software_evidence"] = []
                readiness["workshop_evidence"] = []
            else:
                readiness["software_evidence"] = readiness["software_evidence"][:-1]
                readiness["workshop_evidence"] = readiness["workshop_evidence"][:-1]
            persisted_job.result_json = corrupted_result

            approval_snapshot = [
                (
                    approval.id,
                    approval.approval_type,
                    approval.reason,
                    approval.generation_job_id,
                )
                for approval in session.scalars(
                    select(Approval).where(Approval.design_version_id == version["id"])
                )
            ]
            artifact_snapshot = sorted(
                artifact.id
                for artifact in session.scalars(
                    select(Artifact).where(Artifact.generation_job_id == generated["id"])
                )
            )
            release_snapshot = list(
                session.scalars(select(Release).where(Release.design_version_id == version["id"]))
            )

        rejected = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Malformed readiness must fail closed",
                "generation_job_id": generated["id"],
            },
        )

        assert rejected.status_code == 409
        assert rejected.json()["detail"] == (
            "Production evidence failed integrity verification; regenerate the package"
        )
        with get_session_factory()() as session:
            approvals_after = [
                (
                    approval.id,
                    approval.approval_type,
                    approval.reason,
                    approval.generation_job_id,
                )
                for approval in session.scalars(
                    select(Approval).where(Approval.design_version_id == version["id"])
                )
            ]
            artifacts_after = sorted(
                artifact.id
                for artifact in session.scalars(
                    select(Artifact).where(Artifact.generation_job_id == generated["id"])
                )
            )
            releases_after = list(
                session.scalars(select(Release).where(Release.design_version_id == version["id"]))
            )
            version_after = session.get(DesignVersion, version["id"])
            job_after = session.get(GenerationJob, generated["id"])

        assert approval_snapshot == approvals_after
        assert len(approval_snapshot) == 1
        assert approval_snapshot[0][1] == "design"
        assert artifact_snapshot == artifacts_after
        assert len(artifact_snapshot) == 11
        assert release_snapshot == releases_after == []
        assert version_after is not None
        assert version_after.status == DesignStatus.design_validated
        assert job_after is not None
        assert job_after.status == JobStatus.succeeded


@pytest.mark.parametrize(
    ("field", "value", "remove"),
    _UNSAFE_GENERATION_CLAIM_PARAMS,
)
def test_cam_and_release_endpoints_reject_unsafe_generation_claims_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    remove: bool,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": f"Unsafe generated claim {uuid4()}"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(generated["id"], "6" * 64)

        with get_session_factory().begin() as session:
            persisted_job = session.get(GenerationJob, generated["id"])
            assert persisted_job is not None
            assert isinstance(persisted_job.result_json, dict)
            valid_result = deepcopy(persisted_job.result_json)
            persisted_job.result_json = _with_unsafe_generation_claim(
                valid_result,
                field,
                value,
                remove,
            )
            flag_modified(persisted_job, "result_json")
        with get_session_factory()() as session:
            approval_snapshot = sorted(
                (
                    approval.id,
                    approval.approval_type,
                    approval.reason,
                    approval.generation_job_id,
                )
                for approval in session.scalars(
                    select(Approval).where(Approval.design_version_id == version["id"])
                )
            )
            release_snapshot = list(
                session.scalars(select(Release).where(Release.design_version_id == version["id"]))
            )

        rejected_cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Unsafe generation claims must fail closed",
                "generation_job_id": generated["id"],
            },
        )

        assert rejected_cam.status_code == 409
        with get_session_factory()() as session:
            approvals_after_cam = sorted(
                (
                    approval.id,
                    approval.approval_type,
                    approval.reason,
                    approval.generation_job_id,
                )
                for approval in session.scalars(
                    select(Approval).where(Approval.design_version_id == version["id"])
                )
            )
            releases_after_cam = list(
                session.scalars(select(Release).where(Release.design_version_id == version["id"]))
            )
            version_after_cam = session.get(DesignVersion, version["id"])
        assert approvals_after_cam == approval_snapshot
        assert release_snapshot == releases_after_cam == []
        assert version_after_cam is not None
        assert version_after_cam.status == DesignStatus.design_validated

        with get_session_factory().begin() as session:
            persisted_job = session.get(GenerationJob, generated["id"])
            assert persisted_job is not None
            persisted_job.result_json = deepcopy(valid_result)
            flag_modified(persisted_job, "result_json")
        approved_cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Canonical generation claims reviewed",
                "generation_job_id": generated["id"],
            },
        )
        assert approved_cam.status_code == 200
        assert approved_cam.json()["status"] == "approved"

        with get_session_factory().begin() as session:
            persisted_job = session.get(GenerationJob, generated["id"])
            assert persisted_job is not None
            persisted_job.result_json = _with_unsafe_generation_claim(
                valid_result,
                field,
                value,
                remove,
            )
            flag_modified(persisted_job, "result_json")
        with get_session_factory()() as session:
            approvals_before_release = sorted(
                (
                    approval.id,
                    approval.approval_type,
                    approval.reason,
                    approval.generation_job_id,
                )
                for approval in session.scalars(
                    select(Approval).where(Approval.design_version_id == version["id"])
                )
            )

        rejected_release = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "UNSAFE-CLAIM", "confirmation": "RELEASE"},
        )
        assert rejected_release.status_code == 409

        monkeypatch.setattr(
            api_module,
            "_require_review_evidence",
            lambda *_args, **_kwargs: None,
        )
        defense_in_depth = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "UNSAFE-DEFENSE", "confirmation": "RELEASE"},
        )
        assert defense_in_depth.status_code == 409

        with get_session_factory()() as session:
            approvals_after_release = sorted(
                (
                    approval.id,
                    approval.approval_type,
                    approval.reason,
                    approval.generation_job_id,
                )
                for approval in session.scalars(
                    select(Approval).where(Approval.design_version_id == version["id"])
                )
            )
            releases_after_release = list(
                session.scalars(select(Release).where(Release.design_version_id == version["id"]))
            )
            version_after_release = session.get(DesignVersion, version["id"])
            job_after_release = session.get(GenerationJob, generated["id"])
        assert approvals_after_release == approvals_before_release
        assert releases_after_release == []
        assert version_after_release is not None
        assert version_after_release.status == DesignStatus.approved
        assert job_after_release is not None
        assert job_after_release.status == JobStatus.succeeded


@pytest.mark.parametrize("dfm_status", ("PASS", "WARNING"))
def test_cam_and_release_endpoints_accept_canonical_nonblocking_dfm_status(
    dfm_status: str,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": f"Canonical generated claim {dfm_status}"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(
            generated["id"],
            "5" * 64,
            dfm_status=dfm_status,
        )

        cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Canonical non-blocking DFM reviewed",
                "generation_job_id": generated["id"],
            },
        )
        assert cam.status_code == 200
        released = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={
                "release_number": f"SAFE-{dfm_status}",
                "confirmation": "RELEASE",
            },
        )

    assert released.status_code == 200
    assert released.json()["status"] == "released"
    with get_session_factory()() as session:
        persisted_version = session.get(DesignVersion, version["id"])
        releases = list(
            session.scalars(select(Release).where(Release.design_version_id == version["id"]))
        )
    assert persisted_version is not None
    assert persisted_version.status == DesignStatus.released
    assert len(releases) == 1


def test_design_review_snapshot_is_exactly_bound_to_generation_cam_and_release() -> None:
    first_reason = "Design review. Local checks: DFM-GRAIN-001."
    changed_reason = "Design review. Local checks: DFM-GRAIN-002."
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Exact design review binding"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        assert (
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json=design_approval_payload(client, base, reason=first_reason),
            ).status_code
            == 200
        )
        first_job = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(first_job["id"], "1" * 64)

        unchanged = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json=design_approval_payload(client, base, reason=first_reason),
        )
        idempotent_job = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert unchanged.status_code == 200
        assert idempotent_job.status_code == 202
        assert idempotent_job.json()["id"] == first_job["id"]
        assert (
            idempotent_job.json()["production_context_hash"]
            == (first_job["production_context_hash"])
        )

        cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Exact job reviewed",
                "generation_job_id": first_job["id"],
            },
        )
        assert cam.status_code == 200
        assert cam.json()["status"] == "approved"
        revalidated = client.post(f"{base}/validate", headers=HEADERS)
        assert revalidated.status_code == 200
        assert revalidated.json()["status"] == "approved"

        changed = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json=design_approval_payload(client, base, reason=changed_reason),
        )
        assert changed.status_code == 200
        assert changed.json()["status"] == "design_validated"
        stale_cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Attempt to reuse old review",
                "generation_job_id": first_job["id"],
            },
        )
        stale_release = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "STALE-REVIEW", "confirmation": "RELEASE"},
        )
        replacement_job = client.post(f"{base}/generate", headers=HEADERS, json={})

        assert stale_cam.status_code == 409
        assert "not bound to the current design-warning approval" in stale_cam.json()["detail"]
        assert stale_release.status_code == 409
        assert replacement_job.status_code == 202
        assert replacement_job.json()["id"] != first_job["id"]
        assert (
            replacement_job.json()["production_context_hash"]
            != (first_job["production_context_hash"])
        )


def test_new_design_approval_removes_orphan_cam_without_promoting_version() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Orphan CAM approval"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        with get_session_factory().begin() as session:
            stored = session.get(DesignVersion, version["id"])
            assert stored is not None
            stored.status = DesignStatus.cam_validated
            session.add(
                Approval(
                    organization_id=DEV_ORG_NORDIC,
                    design_version_id=stored.id,
                    approval_type="cam",
                    approved_by=DEV_USER_NORDIC,
                    reason="Legacy orphan CAM review",
                    generation_job_id=None,
                    production_context_hash=None,
                    manifest_sha256=None,
                    overrides_json=[],
                )
            )

        approved = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json=design_approval_payload(client, base, reason="Current design review"),
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "design_validated"
        with get_session_factory()() as session:
            cam_rows = list(
                session.scalars(
                    select(Approval).where(
                        Approval.design_version_id == version["id"],
                        Approval.approval_type == "cam",
                    )
                )
            )
        assert cam_rows == []


def test_generate_keeps_complete_succeeded_job_and_cam_approval_idempotent() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Complete evidence idempotency"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(generated["id"], "9" * 64)
        cam_approval = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Complete evidence reviewed",
                "generation_job_id": generated["id"],
            },
        )
        assert cam_approval.status_code == 200
        assert cam_approval.json()["status"] == "approved"

        repeated = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert repeated.status_code == 202
        assert repeated.json()["id"] == generated["id"]
        assert repeated.json()["status"] == "succeeded"

        with get_session_factory()() as session:
            persisted = session.get(GenerationJob, generated["id"])
            persisted_version = session.get(DesignVersion, version["id"])
            approval = session.scalar(
                select(Approval).where(
                    Approval.design_version_id == version["id"],
                    Approval.approval_type == "cam",
                )
            )
            artifacts = list(
                session.scalars(
                    select(Artifact).where(Artifact.generation_job_id == generated["id"])
                )
            )
            repair_events = list(
                session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.event_key.like(
                            f"generation-evidence-repair:{generated['id']}:%"
                        )
                    )
                )
            )

        assert persisted is not None and persisted.status == JobStatus.succeeded
        assert persisted_version is not None
        assert persisted_version.status == DesignStatus.approved
        assert approval is not None and approval.generation_job_id == generated["id"]
        assert len(artifacts) == 11
        assert repair_events == []


def test_generate_repairs_incomplete_succeeded_job_and_invalidates_cam() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Incomplete evidence repair"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(generated["id"], "8" * 64)
        cam_approval = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Evidence reviewed before corruption",
                "generation_job_id": generated["id"],
            },
        )
        assert cam_approval.status_code == 200
        assert cam_approval.json()["status"] == "approved"

        with get_session_factory().begin() as session:
            persisted = session.get(GenerationJob, generated["id"])
            assert persisted is not None
            persisted.attempts = 3
            persisted.started_at = datetime.now(UTC)
            session.execute(
                delete(Artifact).where(
                    Artifact.generation_job_id == generated["id"],
                    Artifact.kind == "validation_backplot",
                )
            )

        repaired = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert repaired.status_code == 202
        assert repaired.json()["id"] == generated["id"]
        assert repaired.json()["status"] == "queued"
        assert repaired.json()["attempts"] == 0
        assert repaired.json()["result_json"] is None
        assert repaired.json()["error"] is None

        with get_session_factory()() as session:
            persisted = session.get(GenerationJob, generated["id"])
            persisted_version = session.get(DesignVersion, version["id"])
            stale_cam_approval = session.scalar(
                select(Approval).where(
                    Approval.design_version_id == version["id"],
                    Approval.approval_type == "cam",
                )
            )
            artifacts = list(
                session.scalars(
                    select(Artifact).where(Artifact.generation_job_id == generated["id"])
                )
            )
            repair_events = list(
                session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.event_key.like(
                            f"generation-evidence-repair:{generated['id']}:%"
                        )
                    )
                )
            )
            repair_audits = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "generation.evidence_repair_queued",
                        AuditEvent.entity_id == generated["id"],
                    )
                )
            )

        assert persisted is not None
        assert persisted.status == JobStatus.queued
        assert persisted.attempts == 0
        assert persisted.error is None
        assert persisted.result_json is None
        assert persisted.started_at is None
        assert persisted.finished_at is None
        assert persisted_version is not None
        assert persisted_version.status == DesignStatus.design_validated
        assert stale_cam_approval is None
        assert artifacts == []
        assert len(repair_events) == 1
        assert repair_events[0].topic == "generation.requested"
        assert repair_events[0].payload_json == {
            "job_id": generated["id"],
            "organization_id": DEV_ORG_NORDIC,
            "reason": "incomplete_review_evidence",
        }
        assert len(repair_audits) == 1
        assert repair_audits[0].payload_json["missing"] == ["validation_backplot"]
        assert repair_audits[0].payload_json["cam_approval_invalidated"] is True
        assert repair_audits[0].payload_json["cam_approvals_invalidated"] == 1
        assert repair_audits[0].payload_json["cam_approvals_remaining"] == 0


def test_succeeded_evidence_repair_waits_for_active_storage_claim_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        version, generated, base = _create_strict_review_job(
            client,
            name="Busy succeeded evidence repair",
        )
        cam_approval = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Reviewed before the active cleanup claim",
                "generation_job_id": generated["id"],
            },
        )
        assert cam_approval.status_code == 200
        with get_session_factory().begin() as session:
            session.execute(
                delete(Artifact).where(
                    Artifact.generation_job_id == generated["id"],
                    Artifact.kind == "validation_backplot",
                )
            )

        monkeypatch.setattr(
            api_module,
            "prepare_generation_storage_retry",
            lambda *_args, **_kwargs: 41,
        )
        response = client.post(f"{base}/generate", headers=HEADERS, json={})

        assert response.status_code == 503
        assert response.headers["retry-after"] == "41"
        assert response.json()["detail"]["code"] == "GENERATION_STORAGE_RETRY_BUSY"
        with get_session_factory()() as session:
            job_after = session.get(GenerationJob, generated["id"])
            version_after = session.get(DesignVersion, version["id"])
            artifacts_after = list(
                session.scalars(
                    select(Artifact).where(Artifact.generation_job_id == generated["id"])
                )
            )
            approval_after = session.scalar(
                select(Approval).where(
                    Approval.design_version_id == version["id"],
                    Approval.approval_type == "cam",
                )
            )
            repair_events = list(
                session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.event_key.like(
                            f"generation-evidence-repair:{generated['id']}:%"
                        )
                    )
                )
            )
            repair_audits = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "generation.evidence_repair_queued",
                        AuditEvent.entity_id == generated["id"],
                    )
                )
            )

        assert job_after is not None and job_after.status == JobStatus.succeeded
        assert job_after.result_json is not None
        assert version_after is not None and version_after.status == DesignStatus.approved
        assert len(artifacts_after) == 10
        assert approval_after is not None
        assert approval_after.generation_job_id == generated["id"]
        assert repair_events == []
        assert repair_audits == []


def test_artifact_listing_rejects_a_database_row_not_bound_to_job_result() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Artifact binding fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        job = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(job["id"], "4" * 64)
        with get_session_factory().begin() as session:
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.generation_job_id == job["id"],
                    Artifact.kind == "operations",
                )
            )
            assert artifact is not None
            artifact.object_key = "private/secret/wrong-object.json"

        response = client.get(f"/v1/jobs/{job['id']}/artifacts", headers=HEADERS)

        assert response.status_code == 409
        assert response.json()["detail"] == (
            "Production evidence failed integrity verification; regenerate the package"
        )
        assert "private/secret" not in str(response.json())


def test_artifact_listing_turns_a_missing_object_into_a_generic_repairable_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_object(*_args: object, **_kwargs: object) -> None:
        raise api_module.ArtifactIntegrityError("private/key/from-provider")

    monkeypatch.setattr(api_module, "verify_stored_object", missing_object)
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Missing artifact fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        job = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(job["id"], "3" * 64)

        response = client.get(f"/v1/jobs/{job['id']}/artifacts", headers=HEADERS)

        assert response.status_code == 409
        assert response.json()["detail"] == (
            "Production evidence failed integrity verification; regenerate the package"
        )
        assert "private/key" not in str(response.json())


def test_missing_bucket_does_not_repair_or_mutate_reviewed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Transient storage fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        job = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(job["id"], "2" * 64)
        assert (
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={
                    "approval_type": "cam",
                    "reason": "Reviewed before transient storage failure",
                    "generation_job_id": job["id"],
                },
            ).status_code
            == 200
        )

        class MissingBucketStorage:
            def head_object(self, **_kwargs: object) -> dict[str, object]:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "NoSuchBucket",
                            "Message": "private-bucket-name does not exist",
                        },
                        "ResponseMetadata": {"HTTPStatusCode": 404},
                    },
                    "HeadObject",
                )

        monkeypatch.setattr(
            storage_module,
            "internal_s3_client",
            lambda: MissingBucketStorage(),
        )
        monkeypatch.setattr(
            storage_module,
            "get_settings",
            lambda: SimpleNamespace(s3_bucket="private-bucket-name"),
        )
        monkeypatch.setattr(
            api_module,
            "verify_stored_object",
            storage_module.verify_stored_object,
        )
        response = client.post(f"{base}/generate", headers=HEADERS, json={})

        assert response.status_code == 503
        assert response.json()["detail"] == (
            "Production evidence storage is temporarily unavailable; try again later"
        )
        assert "private-bucket-name" not in str(response.json())
        with get_session_factory()() as session:
            persisted_job = session.get(GenerationJob, job["id"])
            persisted_version = session.get(DesignVersion, version["id"])
            artifacts = list(
                session.scalars(select(Artifact).where(Artifact.generation_job_id == job["id"]))
            )
            cam_approval = session.scalar(
                select(Approval).where(
                    Approval.design_version_id == version["id"],
                    Approval.approval_type == "cam",
                )
            )
            repairs = list(
                session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.event_key.like(f"generation-evidence-repair:{job['id']}:%")
                    )
                )
            )

        assert persisted_job is not None and persisted_job.status == JobStatus.succeeded
        assert persisted_job.result_json is not None
        assert persisted_version is not None
        assert persisted_version.status == DesignStatus.approved
        assert len(artifacts) == 11
        assert cam_approval is not None and cam_approval.generation_job_id == job["id"]
        assert repairs == []


def test_body_integrity_failure_can_be_repaired_after_cam_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verification_modes: list[bool] = []

    def corrupt_when_streamed(
        _expectation: object,
        *,
        stream_hash: bool,
    ) -> None:
        verification_modes.append(stream_hash)
        if stream_hash:
            raise api_module.ArtifactIntegrityError("corrupt body")

    monkeypatch.setattr(api_module, "verify_stored_object", corrupt_when_streamed)
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Corrupt body repair fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        job = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(job["id"], "1" * 64)

        listed = client.get(f"/v1/jobs/{job['id']}/artifacts", headers=HEADERS)
        assert listed.status_code == 200
        rejected = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Attempt to review corrupted evidence",
                "generation_job_id": job["id"],
            },
        )
        assert rejected.status_code == 409
        repaired = client.post(f"{base}/generate", headers=HEADERS, json={})

        assert repaired.status_code == 202
        assert repaired.json()["status"] == "queued"
        assert False in verification_modes
        assert True in verification_modes


def test_reuse_rejects_truthy_non_mapping_success_result_without_mutation() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Malformed result fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        job = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(job["id"], "0" * 64)
        with get_session_factory().begin() as session:
            persisted = session.get(GenerationJob, job["id"])
            assert persisted is not None
            persisted.result_json = ["invalid"]  # type: ignore[assignment]

        rejected = client.post(f"{base}/generate", headers=HEADERS, json={})

        assert rejected.status_code == 409
        with get_session_factory()() as session:
            persisted = session.get(GenerationJob, job["id"])
            assert persisted is not None
            assert persisted.status == JobStatus.succeeded
            assert persisted.result_json == ["invalid"]


def test_evidence_repair_downgrades_legacy_approved_status_without_cam_row() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Legacy evidence repair"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(generated["id"], "7" * 64)

        with get_session_factory().begin() as session:
            persisted_version = session.get(DesignVersion, version["id"])
            assert persisted_version is not None
            persisted_version.status = DesignStatus.approved
            session.execute(
                delete(Approval).where(
                    Approval.design_version_id == version["id"],
                    Approval.approval_type == "cam",
                )
            )
            session.execute(
                delete(Artifact).where(
                    Artifact.generation_job_id == generated["id"],
                    Artifact.kind == "manifest",
                )
            )

        repaired = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert repaired.status_code == 202
        assert repaired.json()["status"] == "queued"

        with get_session_factory()() as session:
            persisted_version = session.get(DesignVersion, version["id"])
            repair_audit = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "generation.evidence_repair_queued",
                    AuditEvent.entity_id == generated["id"],
                )
            )

        assert persisted_version is not None
        assert persisted_version.status == DesignStatus.design_validated
        assert repair_audit is not None
        assert repair_audit.payload_json["cam_approval_invalidated"] is False
        assert repair_audit.payload_json["cam_approvals_invalidated"] == 0
        assert repair_audit.payload_json["cam_approvals_remaining"] == 0


def test_repair_rejects_older_job_and_preserves_latest_cam_production_state() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Multi-job evidence repair"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)

        older = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(older["id"], "6" * 64)
        frozen_mismatch = client.post(
            f"{base}/generate", headers=HEADERS, json={"stock_width_mm": 2500}
        )
        assert frozen_mismatch.status_code == 409
        newer = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json={"include_step": False},
        ).json()
        assert newer["id"] != older["id"]
        _complete_generation(newer["id"], "5" * 64)
        cam_approval = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Newer complete job reviewed",
                "generation_job_id": newer["id"],
            },
        )
        assert cam_approval.status_code == 200
        assert cam_approval.json()["status"] == "approved"

        with get_session_factory().begin() as session:
            session.execute(
                delete(Artifact).where(
                    Artifact.generation_job_id == older["id"],
                    Artifact.kind == "operations",
                )
            )

        repaired = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert repaired.status_code == 409
        assert repaired.json()["detail"] == (
            "Only the latest generation job can be repaired; restore the current production state"
        )

        state = client.get(
            f"/v1/projects/{project['id']}/production-state",
            headers=HEADERS,
        )
        assert state.status_code == 200
        assert state.json()["version"]["status"] == "approved"
        assert state.json()["latest_job"]["id"] == newer["id"]
        assert state.json()["latest_job"]["status"] == "succeeded"
        cam_approvals = [
            approval for approval in state.json()["approvals"] if approval["approval_type"] == "cam"
        ]
        assert len(cam_approvals) == 1
        assert cam_approvals[0]["generation_job_id"] == newer["id"]

        with get_session_factory()() as session:
            persisted_older = session.get(GenerationJob, older["id"])
            older_artifacts = list(
                session.scalars(select(Artifact).where(Artifact.generation_job_id == older["id"]))
            )
            newer_artifacts = list(
                session.scalars(select(Artifact).where(Artifact.generation_job_id == newer["id"]))
            )
            repair_audit = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "generation.evidence_repair_queued",
                    AuditEvent.entity_id == older["id"],
                )
            )
            repair_outbox = list(
                session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.event_key.like(f"generation-evidence-repair:{older['id']}:%")
                    )
                )
            )

        assert persisted_older is not None
        assert persisted_older.status == JobStatus.succeeded
        assert len(older_artifacts) == 10
        assert len(newer_artifacts) == 11
        assert repair_audit is None
        assert repair_outbox == []


def test_initial_generate_integrity_conflict_returns_the_winning_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Concurrent generation fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        first = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert first.status_code == 202
        winner = first.json()

        original_scalar = Session.scalar
        missed_existing_row = False

        def miss_existing_generation_once(self, statement, *args, **kwargs):
            nonlocal missed_existing_row
            entities = {
                description.get("entity")
                for description in getattr(statement, "column_descriptions", [])
            }
            if (
                not missed_existing_row
                and GenerationJob in entities
                and "idempotency_key" in str(statement)
            ):
                missed_existing_row = True
                return None
            return original_scalar(self, statement, *args, **kwargs)

        monkeypatch.setattr(Session, "scalar", miss_existing_generation_once)
        conflicted = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert conflicted.status_code == 202
        assert missed_existing_row is True
        assert conflicted.json()["id"] == winner["id"]
        assert conflicted.json()["production_context_hash"] == winner["production_context_hash"]

        with get_session_factory()() as session:
            jobs = list(
                session.scalars(
                    select(GenerationJob).where(
                        GenerationJob.design_version_id == version["id"],
                        GenerationJob.production_context_hash == winner["production_context_hash"],
                    )
                )
            )
            outbox_events = list(
                session.scalars(
                    select(OutboxEvent).where(OutboxEvent.event_key == f"generation:{winner['id']}")
                )
            )
            queue_audits = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "generation.queued",
                        AuditEvent.entity_id == winner["id"],
                    )
                )
            )

        assert len(jobs) == 1
        assert len(outbox_events) == 1
        assert len(queue_audits) == 1


def test_cam_approval_and_release_reject_an_older_job_when_a_newer_job_exists() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Latest job CAM binding"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        older = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(older["id"], "3" * 64)
        initial_cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Older job reviewed before regeneration",
                "generation_job_id": older["id"],
            },
        )
        assert initial_cam.status_code == 200
        assert initial_cam.json()["status"] == "approved"

        frozen_mismatch = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json={"stock_width_mm": 2500},
        )
        assert frozen_mismatch.status_code == 409
        newer = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json={"include_validation_program": False},
        )
        assert newer.status_code == 202
        assert newer.json()["status"] == "queued"

        stale_cam = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Attempted stale CAM approval",
                "generation_job_id": older["id"],
            },
        )
        assert stale_cam.status_code == 409
        assert stale_cam.json()["detail"] == "CAM approval requires the latest generation job"

        with get_session_factory().begin() as session:
            older_job = session.get(GenerationJob, older["id"])
            persisted_version = session.get(DesignVersion, version["id"])
            assert older_job is not None and older_job.result_json is not None
            assert persisted_version is not None
            session.add(
                Approval(
                    organization_id=DEV_ORG_NORDIC,
                    design_version_id=version["id"],
                    approval_type="cam",
                    approved_by=DEV_USER_NORDIC,
                    reason="Legacy stale CAM approval",
                    generation_job_id=older_job.id,
                    production_context_hash=older_job.production_context_hash,
                    manifest_sha256=str(older_job.result_json["manifest_sha256"]),
                    overrides_json=[],
                )
            )
            persisted_version.status = DesignStatus.approved

        stale_release = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "STALE-R1", "confirmation": "RELEASE"},
        )
        assert stale_release.status_code == 409
        assert stale_release.json()["detail"] == (
            "CAM approval is stale because a newer generation job exists"
        )
        with get_session_factory()() as session:
            persisted_version = session.get(DesignVersion, version["id"])
        assert persisted_version is not None
        assert persisted_version.immutable is False


def test_generate_approve_and_release_lock_the_design_version_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Version row lock fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200

        original_refresh = Session.refresh
        locked_version_ids: list[str] = []

        def track_version_lock(self, instance, *args, **kwargs):
            if isinstance(instance, DesignVersion) and kwargs.get("with_for_update") is True:
                locked_version_ids.append(instance.id)
            return original_refresh(self, instance, *args, **kwargs)

        monkeypatch.setattr(Session, "refresh", track_version_lock)
        approve_design(client, base)
        generated = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(generated["id"], "2" * 64)
        approved = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Latest checked job approved",
                "generation_job_id": generated["id"],
            },
        )
        assert approved.status_code == 200
        released = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "LOCK-R1", "confirmation": "RELEASE"},
        )
        assert released.status_code == 200

        assert locked_version_ids == [version["id"]] * 4


def test_freecad_cam_approval_requires_both_persisted_evidence_artifacts() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "FreeCAD evidence fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        job = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json={"include_freecad_project": True},
        ).json()
        _complete_generation(job["id"], "f" * 64)
        with get_session_factory().begin() as session:
            generation = session.get(GenerationJob, job["id"])
            assert generation is not None and generation.result_json is not None
            generation.result_json = {
                **generation.result_json,
                "freecad_project_requested": True,
                "freecad_project_generated": True,
            }
        missing = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "FreeCAD evidence reviewed",
                "generation_job_id": job["id"],
            },
        )
        assert missing.status_code == 409
        assert "interchange-status evidence" in missing.json()["detail"]

        with get_session_factory().begin() as session:
            generation = session.get(GenerationJob, job["id"])
            assert generation is not None and generation.result_json is not None
            freecad_artifacts = []
            for kind, content_type in (
                ("design_fcstd", "application/vnd.freecad"),
                ("cad_interchange_status", "application/json"),
            ):
                freecad_artifacts.append(
                    {
                        "kind": kind,
                        "object_key": f"evidence/{job['id']}/{kind}",
                        "sha256": "e" * 64,
                        "size_bytes": 128,
                        "content_type": content_type,
                    }
                )
            _persist_committed_artifact_records(session, generation, freecad_artifacts)
            generation.result_json = {
                **generation.result_json,
                "evidence_artifacts": [
                    *generation.result_json["evidence_artifacts"],
                    *freecad_artifacts,
                ],
            }

        approved = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "FreeCAD evidence reviewed",
                "generation_job_id": job["id"],
            },
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"


def test_cam_approval_is_bound_to_exact_job_context_and_manifest() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Approval binding fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)

        first = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(first["id"], "a" * 64)
        assert (
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json=design_approval_payload(client, base),
            ).status_code
            == 200
        )
        missing_job = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={"approval_type": "cam", "reason": "CAM review completed"},
        )
        assert missing_job.status_code == 422
        approved = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "CAM review completed",
                "generation_job_id": first["id"],
            },
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        frozen_mismatch = client.post(
            f"{base}/generate", headers=HEADERS, json={"stock_width_mm": 2500}
        )
        assert frozen_mismatch.status_code == 409
        second = client.post(
            f"{base}/generate",
            headers=HEADERS,
            json={"include_step": False},
        )
        assert second.status_code == 202
        stale_release = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "R1", "confirmation": "RELEASE"},
        )
        assert stale_release.status_code == 409

        second_job = second.json()
        _complete_generation(second_job["id"], "b" * 64)
        rebound = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Rechecked changed stock context",
                "generation_job_id": second_job["id"],
            },
        )
        assert rebound.status_code == 200
        released = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "R1", "confirmation": "RELEASE"},
        )
        assert released.status_code == 200
        with get_session_factory()() as session:
            persisted_second = session.get(GenerationJob, second_job["id"])
            assert persisted_second is not None
            expected_manifest_sha = persisted_second.result_json["manifest_sha256"]
        assert released.json()["manifest_sha256"] == expected_manifest_sha
        repeated = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "IGNORED-R2", "confirmation": "RELEASE"},
        )
        assert repeated.status_code == 200
        assert repeated.json() == released.json()


def test_production_state_restores_current_revision_approvals_job_and_release() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Recoverable production state"}
        ).json()
        empty = client.get(f"/v1/projects/{project['id']}/production-state", headers=HEADERS)
        assert empty.status_code == 200
        assert empty.json() == {
            "project_id": project["id"],
            "version": None,
            "approvals": [],
            "latest_job": None,
            "release": None,
        }

        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        design_reason = "Verified dimensions, joints and construction assumptions"
        assert (
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json=design_approval_payload(client, base, reason=design_reason),
            ).status_code
            == 200
        )
        job = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(job["id"], "d" * 64)
        cam_reason = "Verified setup, operation order and validation backplot"
        assert (
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={
                    "approval_type": "cam",
                    "reason": cam_reason,
                    "generation_job_id": job["id"],
                },
            ).status_code
            == 200
        )
        released = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "R7", "confirmation": "RELEASE"},
        ).json()

        restored = client.get(f"/v1/projects/{project['id']}/production-state", headers=HEADERS)
        assert restored.status_code == 200
        state = restored.json()
        assert state["version"]["id"] == version["id"]
        assert state["version"]["status"] == "released"
        assert state["latest_job"]["id"] == job["id"]
        assert state["latest_job"]["status"] == "succeeded"
        assert {approval["approval_type"] for approval in state["approvals"]} == {
            "design",
            "cam",
        }
        assert (
            next(item for item in state["approvals"] if item["approval_type"] == "design")["reason"]
            == design_reason
        )
        assert (
            next(item for item in state["approvals"] if item["approval_type"] == "cam")["reason"]
            == cam_reason
        )
        assert state["release"]["release_id"] == released["release_id"]
        assert state["release"]["release_number"] == "R7"


def test_warning_requires_attributed_override_and_is_frozen_into_job_context() -> None:
    warning_spec = valid_spec() | {
        "width_mm": 500,
        "height_mm": 700,
        "depth_mm": 400,
        "shelf_count": 2,
        "load_per_shelf_kg": 39,
    }
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Warning override fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"], warning_spec),
        ).json()
        assert version["result_json"]["status"] == "WARNING"
        warning_rule_ids = sorted(
            item["rule_id"]
            for item in version["result_json"]["rule_evaluations"]
            if item["status"] == "WARNING"
        )
        assert warning_rule_ids == ["CB-DEFLECTION-001", "CB-JOINT-001"]
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        blocked = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert blocked.status_code == 409

        missing_override = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={"approval_type": "design", "reason": "Reviewed warning"},
        )
        assert missing_override.status_code == 422
        blank_reason = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={"approval_type": "design", "reason": "     "},
        )
        assert blank_reason.status_code == 422
        approved = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "design",
                "reason": "Reviewed construction warning",
                "warning_overrides": [
                    {
                        "rule_id": rule_id,
                        "reason": (
                            "Accepted for this screening fixture after documented review"
                            if rule_id == "CB-DEFLECTION-001"
                            else "Dry mechanical retention reviewed for this exact revision"
                        ),
                    }
                    for rule_id in warning_rule_ids
                ],
            },
        )
        assert approved.status_code == 200
        with get_session_factory().begin() as session:
            design_approval = session.scalar(
                select(Approval).where(
                    Approval.design_version_id == version["id"],
                    Approval.approval_type == "design",
                )
            )
            assert design_approval is not None
            original_overrides = [dict(item) for item in design_approval.overrides_json]
            design_approval.overrides_json = [
                {key: value for key, value in item.items() if key != "approved_at"}
                for item in original_overrides
            ]
        malformed_attribution = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert malformed_attribution.status_code == 409
        assert "stale or incomplete" in malformed_attribution.json()["detail"]
        with get_session_factory().begin() as session:
            design_approval = session.scalar(
                select(Approval).where(
                    Approval.design_version_id == version["id"],
                    Approval.approval_type == "design",
                )
            )
            assert design_approval is not None
            design_approval.overrides_json = original_overrides
        job_response = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert job_response.status_code == 202

        with get_session_factory()() as session:
            job = session.get(GenerationJob, job_response.json()["id"])
            assert job is not None
            overrides = job.request_json["approved_warning_overrides"]
            assert {item["rule_id"] for item in overrides} == set(warning_rule_ids)
            deflection_override = next(
                item for item in overrides if item["rule_id"] == "CB-DEFLECTION-001"
            )
            assert deflection_override["approved_by"]
            assert deflection_override["approved_at"]
            assert "documented review" in deflection_override["reason"]

        _complete_generation(job_response.json()["id"], "c" * 64)
        assert (
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={
                    "approval_type": "cam",
                    "reason": "Reviewed generated CAM context",
                    "generation_job_id": job_response.json()["id"],
                },
            ).status_code
            == 200
        )
        changed_override = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "design",
                "reason": "Construction warning reviewed again",
                "warning_overrides": [
                    {
                        "rule_id": rule_id,
                        "reason": (
                            "Changed justification requires a new production generation"
                            if rule_id == "CB-DEFLECTION-001"
                            else "Dry mechanical retention reviewed for this exact revision"
                        ),
                    }
                    for rule_id in warning_rule_ids
                ],
            },
        )
        assert changed_override.status_code == 200
        assert changed_override.json()["status"] == "design_validated"
        release = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "WARN-R1", "confirmation": "RELEASE"},
        )
        assert release.status_code == 409


def test_new_design_revision_supersedes_release_and_blocks_stale_artifacts() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Supersession fixture"}
        ).json()
        first = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{first['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        job = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(job["id"], "d" * 64)
        assert (
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json=design_approval_payload(client, base),
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={
                    "approval_type": "cam",
                    "reason": "CAM review completed",
                    "generation_job_id": job["id"],
                },
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/release",
                headers=HEADERS,
                json={"release_number": "STALE-R1", "confirmation": "RELEASE"},
            ).status_code
            == 200
        )

        artifact_listing = client.get(f"/v1/jobs/{job['id']}/artifacts", headers=HEADERS)
        assert artifact_listing.status_code == 200
        first_artifact = artifact_listing.json()[0]
        assert first_artifact["download_url"] == first_artifact["download_path"]
        assert first_artifact["download_path"].startswith("/v1/artifacts/")
        stale_download_path = artifact_listing.json()[0]["download_path"]

        changed_spec = valid_spec() | {"width_mm": 710}
        second = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"], changed_spec, expected_current_revision=1),
        )
        assert second.status_code == 201
        assert second.json()["revision"] == 2

        versions = client.get(f"/v1/projects/{project['id']}/versions", headers=HEADERS).json()
        assert [(item["revision"], item["status"]) for item in versions] == [
            (2, "draft"),
            (1, "superseded"),
        ]
        assert versions[1]["immutable"] is True
        assert client.get(f"/v1/jobs/{job['id']}/artifacts", headers=HEADERS).status_code == 409
        assert client.get(stale_download_path, headers=HEADERS).status_code == 409
        assert (
            client.post(
                f"{base}/release",
                headers=HEADERS,
                json={"release_number": "STALE-R1", "confirmation": "RELEASE"},
            ).status_code
            == 409
        )


def test_result_context_hash_tamper_blocks_every_promotion_without_mutation() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            headers=HEADERS,
            json={"name": "Result engine-context binding"},
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        job = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(job["id"], "d" * 64)

        artifact_listing = client.get(f"/v1/jobs/{job['id']}/artifacts", headers=HEADERS)
        assert artifact_listing.status_code == 200
        signed_download_path = artifact_listing.json()[0]["download_path"]
        assert (
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={
                    "approval_type": "cam",
                    "reason": "Initial bound CAM review",
                    "generation_job_id": job["id"],
                },
            ).status_code
            == 200
        )

        with get_session_factory().begin() as session:
            persisted = session.get(GenerationJob, job["id"])
            assert persisted is not None and isinstance(persisted.result_json, dict)
            tampered = deepcopy(persisted.result_json)
            tampered["production_engine_context_hash"] = "0" * 64
            persisted.result_json = tampered
            flag_modified(persisted, "result_json")
            before = {
                "status": persisted.status,
                "result": deepcopy(tampered),
                "artifacts": session.scalar(
                    select(func.count(Artifact.id)).where(
                        Artifact.generation_job_id == persisted.id
                    )
                ),
                "approvals": session.scalar(
                    select(func.count(Approval.id)).where(
                        Approval.design_version_id == persisted.design_version_id
                    )
                ),
                "outbox": session.scalar(select(func.count(OutboxEvent.id))),
            }

        # Read-only recovery surfaces deliberately remain available so an
        # operator can diagnose the detached legacy or tampered job.
        readable_job = client.get(f"/v1/jobs/{job['id']}", headers=HEADERS)
        readable_state = client.get(
            f"/v1/projects/{project['id']}/production-state", headers=HEADERS
        )
        assert readable_job.status_code == readable_state.status_code == 200
        assert readable_job.json()["result_json"]["production_engine_context_hash"] == "0" * 64

        attempts = (
            client.get(f"/v1/jobs/{job['id']}/artifacts", headers=HEADERS),
            client.get(signed_download_path, headers=HEADERS),
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={
                    "approval_type": "cam",
                    "reason": "Detached CAM evidence must not be reapproved",
                    "generation_job_id": job["id"],
                },
            ),
            client.post(
                f"{base}/release",
                headers=HEADERS,
                json={"release_number": "DETACHED-R1", "confirmation": "RELEASE"},
            ),
            client.post(f"{base}/generate", headers=HEADERS, json={}),
        )
        assert [response.status_code for response in attempts] == [409, 409, 409, 409, 409]

        with get_session_factory()() as session:
            persisted = session.get(GenerationJob, job["id"])
            assert persisted is not None
            after = {
                "status": persisted.status,
                "result": persisted.result_json,
                "artifacts": session.scalar(
                    select(func.count(Artifact.id)).where(
                        Artifact.generation_job_id == persisted.id
                    )
                ),
                "approvals": session.scalar(
                    select(func.count(Approval.id)).where(
                        Approval.design_version_id == persisted.design_version_id
                    )
                ),
                "outbox": session.scalar(select(func.count(OutboxEvent.id))),
            }
        assert after == before

        # 0002 truthfully represented unknown historical context as canonical
        # `{}`.  Its honest hash keeps the row inspectable but cannot confer
        # current production authority.
        with get_session_factory().begin() as session:
            persisted = session.get(GenerationJob, job["id"])
            assert persisted is not None and isinstance(persisted.result_json, dict)
            persisted.production_engine_context_json = {}
            legacy_result = deepcopy(persisted.result_json)
            legacy_result["production_engine_context_hash"] = hashlib.sha256(b"{}").hexdigest()
            persisted.result_json = legacy_result
            flag_modified(persisted, "production_engine_context_json")
            flag_modified(persisted, "result_json")

        assert client.get(f"/v1/jobs/{job['id']}", headers=HEADERS).status_code == 200
        assert (
            client.get(
                f"/v1/projects/{project['id']}/production-state", headers=HEADERS
            ).status_code
            == 200
        )
        assert client.get(f"/v1/jobs/{job['id']}/artifacts", headers=HEADERS).status_code == 409
        assert client.get(signed_download_path, headers=HEADERS).status_code == 409
        assert client.post(f"{base}/generate", headers=HEADERS, json={}).status_code == 409
        assert (
            client.post(
                f"{base}/release",
                headers=HEADERS,
                json={"release_number": "LEGACY-R1", "confirmation": "RELEASE"},
            ).status_code
            == 409
        )


def test_new_design_revision_cancels_unfinished_generation() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Cancellation fixture"}
        ).json()
        first = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{first['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, base)
        queued = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        assert queued["status"] == "queued"
        with get_session_factory().begin() as session:
            leased = session.get(GenerationJob, queued["id"])
            assert leased is not None
            leased.status = JobStatus.running
            leased.lease_token = str(uuid4())
            leased.started_at = datetime.now(UTC)

        changed = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(
                project["id"],
                valid_spec() | {"depth_mm": 330},
                expected_current_revision=1,
            ),
        )
        assert changed.status_code == 201
        cancelled = client.get(f"/v1/jobs/{queued['id']}", headers=HEADERS)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert "superseded" in cancelled.json()["error"]
        with get_session_factory()() as session:
            persisted = session.get(GenerationJob, queued["id"])
            assert persisted is not None
            assert persisted.lease_token is None


def test_reverting_to_an_older_design_creates_a_new_revision() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Revert fixture"}
        ).json()
        first = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"]),
        ).json()
        first_base = f"/v1/projects/{project['id']}/versions/{first['revision']}"
        assert client.post(f"{first_base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, first_base)
        first_job = client.post(f"{first_base}/generate", headers=HEADERS, json={}).json()
        second = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(
                project["id"],
                valid_spec() | {"width_mm": 720},
                expected_current_revision=1,
            ),
        ).json()
        reverted = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json=version_payload(project["id"], expected_current_revision=2),
        )

        assert reverted.status_code == 201
        assert (first["revision"], second["revision"], reverted.json()["revision"]) == (
            1,
            2,
            3,
        )
        assert reverted.json()["design_hash"] == first["design_hash"]
        reverted_base = f"/v1/projects/{project['id']}/versions/{reverted.json()['revision']}"
        assert client.post(f"{reverted_base}/validate", headers=HEADERS).status_code == 200
        approve_design(client, reverted_base)
        reverted_job = client.post(f"{reverted_base}/generate", headers=HEADERS, json={}).json()
        assert reverted_job["production_context_hash"] != first_job["production_context_hash"]
        versions = client.get(f"/v1/projects/{project['id']}/versions", headers=HEADERS).json()
        assert [item["status"] for item in versions] == [
            "design_validated",
            "superseded",
            "superseded",
        ]
