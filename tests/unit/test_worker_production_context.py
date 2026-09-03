from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import custombuild_manufacturing.pipeline as manufacturing_pipeline
import custombuild_manufacturing.review_status as review_status_contract
import custombuild_worker.tasks as worker_tasks
import pytest
from app.models import DesignStatus, DesignVersion, GenerationJob, JobStatus
from custombuild_domain import (
    BOOKCASE_ENGINE_VERSION,
    BOOKCASE_TEMPLATE_VERSION,
    BookcaseDesignSpec,
    BookcaseParameters,
    build_bookcase,
    mm,
    resolve_template_capability,
    screening_birch_plywood_6,
    screening_birch_plywood_18,
    screening_mdf_6,
    screening_mdf_18,
)
from custombuild_manufacturing import (
    MAX_CORE_DOCUMENT_BYTES,
    MAX_EVIDENCE_ARTIFACTS,
    MAX_EVIDENCE_TOTAL_BYTES,
    MAX_PRODUCTION_BUNDLE_BYTES,
    ArtifactFile,
    ProductionBlockedError,
)
from custombuild_manufacturing.production_context import (
    generation_context_hash,
    resolve_production_components,
)
from custombuild_rules import RULES_VERSION

TEST_STORAGE_LEASE = "77777777-7777-4777-8777-777777777777"


def _job_and_version() -> tuple[GenerationJob, DesignVersion]:
    capability = resolve_template_capability("shelving")
    version = DesignVersion(
        id="11111111-1111-1111-1111-111111111111",
        organization_id="22222222-2222-2222-2222-222222222222",
        project_id="33333333-3333-3333-3333-333333333333",
        revision=2,
        status=DesignStatus.design_validated,
        design_hash="d" * 64,
        context_hash="c" * 64,
        spec_json={},
        result_json={},
        engine_version=BOOKCASE_ENGINE_VERSION,
        template_version=f"bookcase@{BOOKCASE_TEMPLATE_VERSION}",
        template_id=capability.template_id,
        template_capability_fingerprint=capability.capability_fingerprint,
        rule_version=f"bookcase-rules@{RULES_VERSION}",
        created_by="44444444-4444-4444-4444-444444444444",
        immutable=False,
    )
    request: dict[str, Any] = {
        "stock_width_mm": 2440,
        "stock_height_mm": 1220,
        "stock_count": 4,
        "back_stock_width_mm": 2440,
        "back_stock_height_mm": 1220,
        "back_stock_count": 2,
        "machine_profile_id": "custombuild-router-1325-linuxcnc",
        "postprocessor_id": "linuxcnc-validation-1.1.0",
        "include_step": False,
        "include_freecad_project": False,
    }
    version.result_json = {
        "template_capability": capability.snapshot(),
        "production_context": {
            key: request[key]
            for key in (
                "stock_width_mm",
                "stock_height_mm",
                "stock_count",
                "back_stock_width_mm",
                "back_stock_height_mm",
                "back_stock_count",
                "machine_profile_id",
            )
        },
    }
    resolved = resolve_production_components(
        machine_profile_id=request["machine_profile_id"],
        postprocessor_id=request["postprocessor_id"],
        **worker_tasks.WORKER_SETTINGS.build_identity,
    )
    context_hash = generation_context_hash(
        design_context_hash=version.context_hash,
        design_version_id=version.id,
        revision=version.revision,
        request=request,
        production_engine_context=resolved.context,
    )
    job = GenerationJob(
        id="55555555-5555-5555-5555-555555555555",
        organization_id=version.organization_id,
        design_version_id=version.id,
        status=JobStatus.queued,
        idempotency_key="e" * 64,
        production_context_hash=context_hash,
        production_engine_context_json=resolved.context.as_dict(),
        request_json=request,
        attempts=0,
        lease_token=TEST_STORAGE_LEASE,
    )
    return job, version


def _generate_with_storage_lease(
    monkeypatch: pytest.MonkeyPatch,
    job: GenerationJob,
    version: DesignVersion,
) -> dict[str, Any]:
    lease_token = job.lease_token
    assert lease_token is not None
    monkeypatch.setattr(worker_tasks, "reserve_storage_batch", lambda *_args, **_kwargs: None)
    return worker_tasks._generate(
        job,
        version,
        lease_token=lease_token,
        lease_guard=worker_tasks._GenerationLeaseGuard(job.organization_id, lease_token),
    )


def _frozen_spec_json(**parameter_changes: object) -> dict[str, Any]:
    parameter_payload = BookcaseParameters().model_dump(mode="python")
    parameter_payload.update(parameter_changes)
    spec = BookcaseDesignSpec(
        design_id="frozen-worker-design",
        parameters=BookcaseParameters.model_validate(parameter_payload),
        material=screening_mdf_18(),
        back_material=screening_mdf_6(),
    )
    return spec.model_dump(mode="json")


def _directional_frozen_spec_json(**parameter_changes: object) -> dict[str, Any]:
    parameter_payload = BookcaseParameters().model_dump(mode="python")
    parameter_payload.update(parameter_changes)
    carcass_material = screening_birch_plywood_18()
    back_material = screening_birch_plywood_6().model_copy(
        update={
            "material_id": carcass_material.material_id,
            "version": carcass_material.version,
        }
    )
    spec = BookcaseDesignSpec(
        design_id="frozen-directional-worker-design",
        parameters=BookcaseParameters.model_validate(parameter_payload),
        material=carcass_material,
        back_material=back_material,
    )
    return spec.model_dump(mode="json")


def _generation_ready_job_and_version() -> tuple[GenerationJob, DesignVersion]:
    job, version = _job_and_version()
    version.spec_json = _frozen_spec_json(
        width_um=mm(700),
        height_um=mm(1_000),
        shelf_count=2,
        shelf_load_n=98,
    )
    design = build_bookcase(BookcaseDesignSpec.model_validate(version.spec_json))
    version.design_hash = design.design_hash
    job.request_json = {
        **job.request_json,
        "include_step": True,
        "include_validation_program": True,
        "approved_warning_overrides": [],
    }
    resolved = resolve_production_components(
        machine_profile_id=job.request_json["machine_profile_id"],
        postprocessor_id=job.request_json["postprocessor_id"],
        **worker_tasks.WORKER_SETTINGS.build_identity,
    )
    job.production_engine_context_json = resolved.context.as_dict()
    job.production_context_hash = generation_context_hash(
        design_context_hash=version.context_hash,
        design_version_id=version.id,
        revision=version.revision,
        request=job.request_json,
        production_engine_context=resolved.context,
    )
    return job, version


_EVIDENCE_IDENTITY_CASES = (
    (
        "manufacturing/manufacturing-intent.json",
        "manufacturing_intent",
        "application/json",
        "MACHINE_NEUTRAL_MANUFACTURING_INTENT",
    ),
    (
        "shop/supplier-handoff.json",
        "supplier_handoff",
        "application/json",
        "CNC_SHOP_HANDOFF",
    ),
    (
        "validation/dfm-report.json",
        "dfm_report",
        "application/json",
        "DFM_VALIDATION_REPORT",
    ),
    (
        "validation/design-review-package-status.json",
        "design_review_package_status",
        "application/json",
        "DESIGN_REVIEW_PACKAGE_STATUS",
    ),
    (
        "validation/stock-selection.json",
        "stock_selection",
        "application/json",
        "STOCK_SELECTION_SNAPSHOT",
    ),
    (
        "validation/generation-plan.json",
        "generation_plan",
        "application/json",
        "GENERATION_PLAN",
    ),
    (
        "cam/operations.json",
        "operations",
        "application/json",
        "MACHINE_NEUTRAL_OPERATIONS",
    ),
    (
        "cam/validation-backplot.svg",
        "validation_backplot",
        "image/svg+xml",
        "VALIDATION_BACKPLOT",
    ),
    ("model/design.glb", "design_glb", "model/gltf-binary", "WEB_PREVIEW_GLB"),
    (
        "model/design.fcstd",
        "design_fcstd",
        "application/vnd.freecad",
        "NON_AUTHORITATIVE_FREECAD_PROJECT",
    ),
    (
        "validation/cad-interchange-status.json",
        "cad_interchange_status",
        "application/json",
        "CAD_INTERCHANGE_STATUS",
    ),
    (
        "validation/source-provenance.json",
        "source_provenance",
        "application/json",
        "SOURCE_PROVENANCE",
    ),
    (
        "validation/workshop-readiness.json",
        "workshop_readiness",
        "application/json",
        "WORKSHOP_READINESS_REPORT",
    ),
    (
        "assembly/assembly-readiness.json",
        "assembly_readiness",
        "application/json",
        "ASSEMBLY_READINESS",
    ),
    (
        "cam/setups/setup-001.svg",
        "setup_sheet_001",
        "image/svg+xml",
        "SETUP_SHEET",
    ),
)


def _stub_evidence_bundle(
    job: GenerationJob,
    artifacts: tuple[ArtifactFile, ...],
    context: Any,
) -> SimpleNamespace:
    return SimpleNamespace(
        zip_bytes=b"valid-bundle",
        manifest={
            "generation_context_hash": job.production_context_hash,
            "production_engine_context": context.production_engine_context,
        },
        artifacts=artifacts,
        review_status=SimpleNamespace(
            cam_status=worker_tasks.CAMStageStatus.BLOCKED,
            as_dict=lambda: {"cam_status": "BLOCKED"},
        ),
        dfm_report=SimpleNamespace(status=SimpleNamespace(value="BLOCKED")),
        layouts=(),
        workshop_readiness=SimpleNamespace(as_dict=lambda: {}),
    )


def _install_evidence_bundle(
    monkeypatch: pytest.MonkeyPatch,
    job: GenerationJob,
    artifacts: tuple[ArtifactFile, ...],
) -> None:
    def build_bundle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        return _stub_evidence_bundle(job, artifacts, kwargs["context"])

    monkeypatch.setattr(worker_tasks, "build_production_bundle", build_bundle)


def test_worker_accepts_only_the_exact_frozen_context() -> None:
    job, version = _job_and_version()

    resolved = worker_tasks._resolve_current_job_context(job, version)

    assert resolved.context.as_dict() == job.production_engine_context_json
    assert resolved.context.fingerprint
    assert resolved.context.vcs_ref == worker_tasks.WORKER_SETTINGS.vcs_ref
    assert (
        resolved.context.source_manifest_sha256
        == worker_tasks.WORKER_SETTINGS.source_manifest_sha256
    )
    assert (
        resolved.context.dependency_lock_sha256
        == worker_tasks.WORKER_SETTINGS.dependency_lock_sha256
    )


def test_worker_returns_review_package_when_two_sided_cam_registration_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exercise the deeper two-sided registration branch under an explicit
    # future structured-retention premise. The independent DADO gate has its
    # own fail-closed regression coverage and must otherwise win first.
    monkeypatch.setattr(
        manufacturing_pipeline,
        "dado_retention_evidence_missing",
        lambda _design: False,
    )
    monkeypatch.setattr(
        review_status_contract,
        "dado_retention_evidence_missing",
        lambda _design: False,
    )
    job, version = _job_and_version()
    version.spec_json = _frozen_spec_json(
        width_um=mm(700),
        height_um=mm(1_000),
        shelf_count=2,
        shelf_load_n=98,
    )
    design = build_bookcase(BookcaseDesignSpec.model_validate(version.spec_json))
    version.design_hash = design.design_hash
    job.request_json = {
        **job.request_json,
        "include_step": True,
        "include_validation_program": True,
        "approved_warning_overrides": [],
    }
    resolved = resolve_production_components(
        machine_profile_id=job.request_json["machine_profile_id"],
        postprocessor_id=job.request_json["postprocessor_id"],
        **worker_tasks.WORKER_SETTINGS.build_identity,
    )
    job.production_engine_context_json = resolved.context.as_dict()
    job.production_context_hash = generation_context_hash(
        design_context_hash=version.context_hash,
        design_version_id=version.id,
        revision=version.revision,
        request=job.request_json,
        production_engine_context=resolved.context,
    )
    writes: list[tuple[str, dict[str, str]]] = []
    preexisting_legacy_objects: dict[str, tuple[bytes, dict[str, str]]] = {}

    def record_write(
        key: str,
        payload: bytes,
        content_type: str,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        if content_type == "application/zip":
            legacy_key = (
                f"{worker_tasks.organization_id_safe(job.organization_id)}/"
                f"{version.design_hash}/{job.production_context_hash}/"
                f"{hashlib.sha256(payload).hexdigest()}/production.zip"
            )
            preexisting_legacy_objects[legacy_key] = (payload, {})
            if key == legacy_key:
                raise AssertionError("worker reused an unbound legacy bundle object")
        writes.append((key, dict(metadata or {})))

    monkeypatch.setattr(worker_tasks, "_put_object", record_write)

    result = _generate_with_storage_lease(monkeypatch, job, version)

    assert result["design_review_package_status"]["package_status"] == ("READY_FOR_DESIGN_REVIEW")
    assert result["design_review_package_status"]["cam_status"] == "BLOCKED"
    assert result["design_review_package_status"]["blocker_codes"] == [
        "TWO_SIDED_REGISTRATION_MISSING"
    ]
    assert result["machine_program_mode"] == "CAM_BLOCKED"
    assert result["production_machine_program"] is False
    assert result["nesting_utilization_ppm"] is None
    assert result["nesting_layouts"] == []
    evidence_kinds = {item["kind"] for item in result["evidence_artifacts"]}
    assert "manufacturing_intent" in evidence_kinds
    assert "supplier_handoff" in evidence_kinds
    assert "design_review_package_status" in evidence_kinds
    assert "stock_selection" in evidence_kinds
    assert "generation_plan" in evidence_kinds
    assert "operations" not in evidence_kinds
    assert "validation_backplot" not in evidence_kinds
    assert not any(kind.startswith("setup_sheet_") for kind in evidence_kinds)
    assert result["workshop_readiness"]["design_review_ready"] is False
    assert result["workshop_readiness"]["physical_cutting_authorized"] is False
    bundle_write = next(item for item in writes if item[0].endswith("production.zip"))
    assert bundle_write[0].endswith(
        f"/linked-v1/{result['manifest_sha256']}/{result['bundle_sha256']}/production.zip"
    )
    legacy_unbound_key = (
        f"{worker_tasks.organization_id_safe(job.organization_id)}/"
        f"{version.design_hash}/{job.production_context_hash}/"
        f"{result['bundle_sha256']}/production.zip"
    )
    assert legacy_unbound_key in preexisting_legacy_objects
    assert bundle_write[0] != legacy_unbound_key
    assert bundle_write[1] == {"manifest-sha256": result["manifest_sha256"]}


@pytest.mark.parametrize("oversize_object", ("bundle", "manifest"))
def test_worker_rejects_oversize_bundle_or_manifest_before_any_object_write(
    oversize_object: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _generation_ready_job_and_version()
    writes: list[str] = []

    def build_invalid_bundle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        context = kwargs["context"]
        manifest = {
            "generation_context_hash": job.production_context_hash,
            "production_engine_context": context.production_engine_context,
        }
        if oversize_object == "manifest":
            manifest["padding"] = "x" * MAX_CORE_DOCUMENT_BYTES
        zip_bytes = (
            b"x" * (MAX_PRODUCTION_BUNDLE_BYTES + 1)
            if oversize_object == "bundle"
            else b"valid-bundle"
        )
        return SimpleNamespace(zip_bytes=zip_bytes, manifest=manifest, artifacts=())

    monkeypatch.setattr(worker_tasks, "build_production_bundle", build_invalid_bundle)
    monkeypatch.setattr(
        worker_tasks,
        "_put_object",
        lambda key, _payload, _content_type, **_kwargs: writes.append(key),
    )

    with pytest.raises(ProductionBlockedError, match="canonical size limit"):
        _generate_with_storage_lease(monkeypatch, job, version)

    assert writes == []


def test_worker_rejects_late_oversize_evidence_before_any_object_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _generation_ready_job_and_version()
    writes: list[str] = []

    def build_invalid_bundle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        context = kwargs["context"]
        return SimpleNamespace(
            zip_bytes=b"valid-bundle",
            manifest={
                "generation_context_hash": job.production_context_hash,
                "production_engine_context": context.production_engine_context,
            },
            artifacts=(
                ArtifactFile(
                    "validation/dfm-report.json",
                    b"{}",
                    "application/json",
                    "DFM_VALIDATION_REPORT",
                ),
                ArtifactFile(
                    "validation/source-provenance.json",
                    b"x" * (MAX_CORE_DOCUMENT_BYTES + 1),
                    "application/json",
                    "SOURCE_PROVENANCE",
                ),
            ),
        )

    monkeypatch.setattr(worker_tasks, "build_production_bundle", build_invalid_bundle)
    monkeypatch.setattr(
        worker_tasks,
        "_put_object",
        lambda key, _payload, _content_type, **_kwargs: writes.append(key),
    )

    with pytest.raises(ProductionBlockedError, match="source_provenance artifact"):
        _generate_with_storage_lease(monkeypatch, job, version)

    assert writes == []


@pytest.mark.parametrize(
    "invalid_inventory",
    ("count", "total", "duplicate-path", "media-type"),
)
def test_worker_rejects_invalid_evidence_inventory_before_any_object_write(
    invalid_inventory: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _generation_ready_job_and_version()
    writes: list[str] = []
    if invalid_inventory == "count":
        artifacts = tuple(
            ArtifactFile(
                f"cam/setups/setup-{index:03d}.svg",
                b"x",
                "image/svg+xml",
                "SETUP_SHEET",
            )
            for index in range(MAX_EVIDENCE_ARTIFACTS + 1)
        )
        message = "file-count limit"
    elif invalid_inventory == "total":
        shared_payload = b"x" * MAX_CORE_DOCUMENT_BYTES
        artifacts = tuple(
            ArtifactFile(
                f"cam/setups/setup-{index:03d}.svg",
                shared_payload,
                "image/svg+xml",
                "SETUP_SHEET",
            )
            for index in range(MAX_EVIDENCE_TOTAL_BYTES // len(shared_payload) + 1)
        )
        message = "total size limit"
    elif invalid_inventory == "duplicate-path":
        artifacts = (
            ArtifactFile(
                "cam/setups/setup-001.svg",
                b"first",
                "image/svg+xml",
                "SETUP_SHEET",
            ),
            ArtifactFile(
                "cam/setups/setup-001.svg",
                b"second",
                "image/svg+xml",
                "SETUP_SHEET",
            ),
        )
        message = "duplicate identities"
    else:
        artifacts = (
            ArtifactFile(
                "validation/dfm-report.json",
                b"{}",
                "",
                "DFM_VALIDATION_REPORT",
            ),
        )
        message = "media type"

    def build_invalid_bundle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        context = kwargs["context"]
        return SimpleNamespace(
            zip_bytes=b"valid-bundle",
            manifest={
                "generation_context_hash": job.production_context_hash,
                "production_engine_context": context.production_engine_context,
            },
            artifacts=artifacts,
        )

    monkeypatch.setattr(worker_tasks, "build_production_bundle", build_invalid_bundle)
    monkeypatch.setattr(
        worker_tasks,
        "_put_object",
        lambda key, _payload, _content_type, **_kwargs: writes.append(key),
    )

    with pytest.raises(ProductionBlockedError, match=message):
        _generate_with_storage_lease(monkeypatch, job, version)

    assert writes == []


@pytest.mark.parametrize(
    ("path", "_kind", "_expected_media_type", "role"),
    _EVIDENCE_IDENTITY_CASES,
)
def test_worker_rejects_valid_but_wrong_evidence_media_before_any_object_write(
    path: str,
    _kind: str,
    _expected_media_type: str,
    role: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _generation_ready_job_and_version()
    writes: list[str] = []
    artifacts = (ArtifactFile(path, b"{}", "application/pdf", role),)
    _install_evidence_bundle(monkeypatch, job, artifacts)
    monkeypatch.setattr(
        worker_tasks,
        "_put_object",
        lambda key, _payload, _content_type, **_kwargs: writes.append(key),
    )

    with pytest.raises(ProductionBlockedError, match="canonical path"):
        _generate_with_storage_lease(monkeypatch, job, version)

    assert writes == []


@pytest.mark.parametrize(
    ("path", "_kind", "media_type", "_expected_role"),
    _EVIDENCE_IDENTITY_CASES,
)
def test_worker_rejects_wrong_evidence_role_before_any_object_write(
    path: str,
    _kind: str,
    media_type: str,
    _expected_role: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _generation_ready_job_and_version()
    writes: list[str] = []
    artifacts = (ArtifactFile(path, b"{}", media_type, "ATTACKER_ROLE"),)
    _install_evidence_bundle(monkeypatch, job, artifacts)
    monkeypatch.setattr(
        worker_tasks,
        "_put_object",
        lambda key, _payload, _content_type, **_kwargs: writes.append(key),
    )

    with pytest.raises(ProductionBlockedError, match="canonical path"):
        _generate_with_storage_lease(monkeypatch, job, version)

    assert writes == []


@pytest.mark.parametrize(
    "path",
    (
        "cam/setups/setup-001.json",
        "cam/setups/nested/setup-001.svg",
        "cam/setups/setup-001.svg.tmp",
    ),
)
def test_worker_rejects_noncanonical_setup_evidence_path_before_any_object_write(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _generation_ready_job_and_version()
    writes: list[str] = []
    artifacts = (ArtifactFile(path, b"<svg/>", "image/svg+xml", "SETUP_SHEET"),)
    _install_evidence_bundle(monkeypatch, job, artifacts)
    monkeypatch.setattr(
        worker_tasks,
        "_put_object",
        lambda key, _payload, _content_type, **_kwargs: writes.append(key),
    )

    with pytest.raises(ProductionBlockedError, match="setup evidence path"):
        _generate_with_storage_lease(monkeypatch, job, version)

    assert writes == []


def test_worker_accepts_only_the_canonical_evidence_path_identity_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _generation_ready_job_and_version()
    writes: list[tuple[str, str]] = []
    artifacts = tuple(
        ArtifactFile(path, b"{}", media_type, role)
        for path, _kind, media_type, role in _EVIDENCE_IDENTITY_CASES
    )
    _install_evidence_bundle(monkeypatch, job, artifacts)

    def record_write(
        key: str,
        _payload: bytes,
        content_type: str,
        **_kwargs: object,
    ) -> None:
        writes.append((key, content_type))

    monkeypatch.setattr(worker_tasks, "_put_object", record_write)

    result = _generate_with_storage_lease(monkeypatch, job, version)

    expected = {
        kind: (path, media_type) for path, kind, media_type, _role in _EVIDENCE_IDENTITY_CASES
    }
    observed = {
        item["kind"]: (
            item["object_key"].rsplit("/", maxsplit=1)[-1].replace("__", "/"),
            item["content_type"],
        )
        for item in result["evidence_artifacts"]
    }
    assert observed == expected
    assert len(writes) == len(_EVIDENCE_IDENTITY_CASES) + 2


def test_worker_returns_stockless_review_package_when_stock_profile_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _job_and_version()
    version.spec_json = _frozen_spec_json(
        width_um=mm(700),
        height_um=mm(1_000),
        shelf_count=2,
        shelf_load_n=98,
    )
    design = build_bookcase(BookcaseDesignSpec.model_validate(version.spec_json))
    version.design_hash = design.design_hash
    job.request_json = {
        **job.request_json,
        "stock_width_mm": 600,
        "stock_height_mm": 600,
        "back_stock_width_mm": 600,
        "back_stock_height_mm": 600,
        "include_step": True,
        "include_validation_program": True,
        "approved_warning_overrides": [],
    }
    version.result_json["production_context"] = {
        key: job.request_json[key]
        for key in (
            "stock_width_mm",
            "stock_height_mm",
            "stock_count",
            "back_stock_width_mm",
            "back_stock_height_mm",
            "back_stock_count",
            "machine_profile_id",
        )
    }
    resolved = resolve_production_components(
        machine_profile_id=job.request_json["machine_profile_id"],
        postprocessor_id=job.request_json["postprocessor_id"],
        **worker_tasks.WORKER_SETTINGS.build_identity,
    )
    job.production_engine_context_json = resolved.context.as_dict()
    job.production_context_hash = generation_context_hash(
        design_context_hash=version.context_hash,
        design_version_id=version.id,
        revision=version.revision,
        request=job.request_json,
        production_engine_context=resolved.context,
    )
    writes: list[tuple[str, str, dict[str, str]]] = []
    written_payloads: dict[str, bytes] = {}

    def record_write(
        key: str,
        payload: bytes,
        content_type: str,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        written_payloads[key] = payload
        writes.append((key, content_type, dict(metadata or {})))

    monkeypatch.setattr(worker_tasks, "_put_object", record_write)

    result = _generate_with_storage_lease(monkeypatch, job, version)

    status = result["design_review_package_status"]
    assert status["package_status"] == "READY_FOR_DESIGN_REVIEW"
    assert status["cam_status"] == "BLOCKED"
    assert status["blocker_codes"] == ["STOCK_PROFILE_MISSING"]
    assert result["dfm_status"] == "BLOCK"
    assert result["machine_program_mode"] == "CAM_BLOCKED"
    assert result["production_machine_program"] is False
    assert result["nesting_utilization_ppm"] is None
    assert result["used_sheet_count"] == 0
    assert result["nesting_layouts"] == []
    readiness = result["workshop_readiness"]
    software_status = {item["code"]: item["status"] for item in readiness["software_evidence"]}
    assert software_status["AUTHORITATIVE_CAD"] == "VERIFIED"
    assert software_status["DFM_SCREEN"] == "MISSING"
    assert readiness["physical_cutting_authorized"] is False
    evidence_kinds = {item["kind"] for item in result["evidence_artifacts"]}
    assert "manufacturing_intent" in evidence_kinds
    assert "supplier_handoff" in evidence_kinds
    assert "dfm_report" in evidence_kinds
    assert "design_review_package_status" in evidence_kinds
    assert "stock_selection" in evidence_kinds
    assert "generation_plan" in evidence_kinds
    assert "operations" not in evidence_kinds
    assert "validation_backplot" not in evidence_kinds
    assert "label_index" not in evidence_kinds
    assert "quality_measurement_plan" not in evidence_kinds
    assert not any(kind.startswith("setup_sheet_") for kind in evidence_kinds)
    bundle_write = next(item for item in writes if item[1] == "application/zip")
    assert "/linked-v1/" in bundle_write[0]
    assert bundle_write[2] == {"manifest-sha256": result["manifest_sha256"]}
    manifest = json.loads(written_payloads[result["manifest_object_key"]])
    assert (
        manifest["postprocessor_version"]
        == job.production_engine_context_json["postprocessor_version"]
    )


def test_worker_keeps_directional_stock_unbound_despite_opaque_grain_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _job_and_version()
    version.spec_json = _directional_frozen_spec_json(
        width_um=mm(700),
        height_um=mm(1_000),
        shelf_count=2,
        shelf_load_n=98,
    )
    design = build_bookcase(BookcaseDesignSpec.model_validate(version.spec_json))
    version.design_hash = design.design_hash
    job.request_json = {
        **job.request_json,
        "include_step": True,
        "include_validation_program": True,
        "approved_warning_overrides": [],
        "external_evidence": [
            {
                "evidence_type": "material_grain",
                "catalog_id": "supplier-sheet-document",
                "catalog_version": "opaque-v1",
                "sha256": "a" * 64,
            }
        ],
    }
    resolved = resolve_production_components(
        machine_profile_id=job.request_json["machine_profile_id"],
        postprocessor_id=job.request_json["postprocessor_id"],
        **worker_tasks.WORKER_SETTINGS.build_identity,
    )
    job.production_engine_context_json = resolved.context.as_dict()
    job.production_context_hash = generation_context_hash(
        design_context_hash=version.context_hash,
        design_version_id=version.id,
        revision=version.revision,
        request=job.request_json,
        production_engine_context=resolved.context,
    )
    written_payloads: dict[str, bytes] = {}
    captured_stock: list[Any] = []
    build_bundle = worker_tasks.build_production_bundle

    def capture_bundle(*args: Any, **kwargs: Any) -> Any:
        captured_stock.extend(kwargs["stock"])
        return build_bundle(*args, **kwargs)

    def record_write(
        key: str,
        payload: bytes,
        _content_type: str,
        **_kwargs: object,
    ) -> None:
        written_payloads[key] = payload

    monkeypatch.setattr(worker_tasks, "build_production_bundle", capture_bundle)
    monkeypatch.setattr(worker_tasks, "_put_object", record_write)

    result = _generate_with_storage_lease(monkeypatch, job, version)

    assert captured_stock
    assert {stock.grain_direction for stock in captured_stock} == {"UNBOUND"}
    assert [stock.stock_id for stock in captured_stock] == [
        "stock-carcass-birch-plywood-screening-2026.1-18000um-2440000x1220000um",
        "stock-back-birch-plywood-screening-2026.1-6000um-2440000x1220000um",
    ]
    assert len({stock.stock_id for stock in captured_stock}) == len(captured_stock)
    status = result["design_review_package_status"]
    assert status["package_status"] == "READY_FOR_DESIGN_REVIEW"
    assert status["cam_status"] == "BLOCKED"
    assert status["blocker_codes"] == ["DFM-GRAIN-001"]
    assert result["dfm_status"] == "BLOCK"
    assert result["machine_program_mode"] == "CAM_BLOCKED"
    assert result["nesting_utilization_ppm"] is None
    assert result["used_sheet_count"] == 0
    assert result["nesting_layouts"] == []
    software_status = {
        item["code"]: item["status"] for item in result["workshop_readiness"]["software_evidence"]
    }
    workshop_status = {
        item["code"]: item["status"] for item in result["workshop_readiness"]["workshop_evidence"]
    }
    assert software_status["DFM_SCREEN"] == "MISSING"
    assert workshop_status["MATERIAL_GRAIN"] == "EXTERNAL_EVIDENCE_REQUIRED"
    grain_readiness = next(
        item
        for item in result["workshop_readiness"]["workshop_evidence"]
        if item["code"] == "MATERIAL_GRAIN"
    )
    assert "sha256:" + "a" * 64 in grain_readiness["evidence"]
    assert "not a structured stock-grain axis binding" in grain_readiness["evidence"]
    dfm_artifact = next(
        item for item in result["evidence_artifacts"] if item["kind"] == "dfm_report"
    )
    dfm = json.loads(written_payloads[dfm_artifact["object_key"]])
    assert any(
        issue["code"] == "DFM-GRAIN-001" and issue["severity"] == "BLOCK" for issue in dfm["issues"]
    )
    stock_selection_artifact = next(
        item for item in result["evidence_artifacts"] if item["kind"] == "stock_selection"
    )
    stock_selection = json.loads(written_payloads[stock_selection_artifact["object_key"]])
    assert stock_selection["schema_version"] == "custombuild.stock-selection.v1"
    assert [item["stock_id"] for item in stock_selection["stocks"]] == sorted(
        stock.stock_id for stock in captured_stock
    )
    assert stock_selection["unmatched_part_ids"] == []
    generation_plan_artifact = next(
        item for item in result["evidence_artifacts"] if item["kind"] == "generation_plan"
    )
    generation_plan = json.loads(written_payloads[generation_plan_artifact["object_key"]])
    assert generation_plan["schema_version"] == "custombuild.generation-plan.v1"
    assert generation_plan["machine_profile"]["id"] == "custombuild-router-1325-linuxcnc"
    assert generation_plan["postprocessor"]["id"] == "linuxcnc-validation"
    assert (
        generation_plan["stock_profiles_fingerprint"]
        == hashlib.sha256(
            json.dumps(
                stock_selection["stocks"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )
    evidence_kinds = {item["kind"] for item in result["evidence_artifacts"]}
    assert "stock_selection" in evidence_kinds
    assert "generation_plan" in evidence_kinds
    assert "operations" not in evidence_kinds
    assert "validation_backplot" not in evidence_kinds
    assert not any(kind.startswith("setup_sheet_") for kind in evidence_kinds)
    manifest = json.loads(written_payloads[result["manifest_object_key"]])
    assert manifest["external_evidence"] == job.request_json["external_evidence"]
    assert not any("DFM-GRAIN-001" in warning for warning in manifest["warnings"])


def test_worker_blocks_outlier_frozen_spec_before_build_or_object_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _job_and_version()
    version.spec_json = _frozen_spec_json()
    version.spec_json["parameters"]["width_um"] = mm(6_000) + 1
    frozen_before = copy.deepcopy(version.spec_json)
    calls: list[str] = []
    monkeypatch.setattr(worker_tasks, "build_bookcase", lambda _spec: calls.append("build"))
    monkeypatch.setattr(
        worker_tasks,
        "_put_object",
        lambda _key, _payload, _content_type, **_kwargs: calls.append("write"),
    )

    with pytest.raises(ProductionBlockedError, match="published design envelope"):
        _generate_with_storage_lease(monkeypatch, job, version)

    assert calls == []
    assert version.spec_json == frozen_before


@pytest.mark.parametrize(
    ("template_id", "spec_json", "message"),
    (
        (
            "shelving",
            _frozen_spec_json(
                width_um=mm(2_400),
                height_um=mm(2_400),
                depth_um=mm(340),
                shelf_count=3,
                base_cabinet_height_um=mm(700),
                base_cabinet_depth_um=mm(340),
                base_cabinet_count=3,
            ),
            "furniture family",
        ),
        ("wall-library", _frozen_spec_json(), "furniture family"),
    ),
)
def test_worker_binds_frozen_spec_family_to_verified_capability_archetype(
    template_id: str,
    spec_json: dict[str, Any],
    message: str,
) -> None:
    _job, version = _job_and_version()
    capability = resolve_template_capability(template_id)
    version.template_id = template_id
    version.template_capability_fingerprint = capability.capability_fingerprint
    version.result_json = {
        **version.result_json,
        "template_capability": capability.snapshot(),
    }
    version.spec_json = copy.deepcopy(spec_json)
    frozen_before = copy.deepcopy(version.spec_json)

    with pytest.raises(ProductionBlockedError, match=message):
        worker_tasks._load_frozen_design_spec(version, capability)

    assert version.spec_json == frozen_before


def test_worker_treats_malformed_frozen_result_as_deterministic_block() -> None:
    job, version = _job_and_version()
    version.result_json = []  # type: ignore[assignment]

    with pytest.raises(ProductionBlockedError, match="production engine context drift"):
        worker_tasks._resolve_current_job_context(job, version)


@pytest.mark.parametrize(
    "drift",
    (
        "engine-context",
        "source-commit",
        "source-manifest",
        "dependency-lock",
        "generation-hash",
    ),
)
def test_worker_blocks_persisted_context_drift(drift: str) -> None:
    job, version = _job_and_version()
    if drift == "engine-context":
        job.production_engine_context_json = {
            **job.production_engine_context_json,
            "operations_engine_version": "drifted-operations-engine",
        }
    elif drift == "source-commit":
        job.production_engine_context_json = {
            **job.production_engine_context_json,
            "vcs_ref": "a" * 40,
        }
    elif drift == "dependency-lock":
        job.production_engine_context_json = {
            **job.production_engine_context_json,
            "dependency_lock_sha256": "b" * 64,
        }
    elif drift == "source-manifest":
        job.production_engine_context_json = {
            **job.production_engine_context_json,
            "source_manifest_sha256": "c" * 64,
        }
    else:
        job.production_context_hash = "0" * 64

    with pytest.raises(ProductionBlockedError, match="production engine context drift"):
        worker_tasks._resolve_current_job_context(job, version)


@pytest.mark.parametrize(
    "field_name",
    ("engine_version", "template_version", "rule_version"),
)
def test_worker_blocks_stale_frozen_design_libraries(field_name: str) -> None:
    job, version = _job_and_version()
    setattr(version, field_name, f"{getattr(version, field_name)}-stale")

    with pytest.raises(ProductionBlockedError, match="production engine context drift"):
        worker_tasks._resolve_current_job_context(job, version)


def test_worker_blocks_library_drift_before_build_or_object_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _job_and_version()
    job.production_engine_context_json = {
        **job.production_engine_context_json,
        "tool_library_fingerprint": "0" * 64,
    }
    writes: list[str] = []

    def record_write(
        key: str,
        data: bytes,
        content_type: str,
        **_kwargs: object,
    ) -> None:
        writes.append(key)

    monkeypatch.setattr(worker_tasks, "_put_object", record_write)
    with pytest.raises(ProductionBlockedError, match="production engine context drift"):
        _generate_with_storage_lease(monkeypatch, job, version)

    assert writes == []


@pytest.mark.parametrize("drift", ("legacy", "stock", "extra"))
def test_worker_blocks_jobs_not_bound_to_the_exact_revision_choices(drift: str) -> None:
    job, version = _job_and_version()
    if drift == "legacy":
        version.result_json = {}
    elif drift == "stock":
        job.request_json = {**job.request_json, "stock_width_mm": 2500}
    else:
        frozen = dict(version.result_json["production_context"])
        frozen["unexpected"] = True
        version.result_json = {"production_context": frozen}

    with pytest.raises(ProductionBlockedError, match="production engine context drift"):
        worker_tasks._resolve_current_job_context(job, version)


def test_worker_writes_checksum_metadata_with_every_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class RecordingObjectStorage:
        def head_bucket(self, **_kwargs: Any) -> None:
            return None

        def put_object(self, **kwargs: Any) -> None:
            calls.append(kwargs)

        def head_object(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "ContentLength": len(payload),
                "ContentType": "application/json",
                "Metadata": {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "immutable": "true",
                },
            }

        def get_object(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "ContentLength": len(payload),
                "ContentType": "application/json",
                "Body": BytesIO(payload),
            }

    payload = b"checksum-bound production evidence"
    monkeypatch.setattr(worker_tasks, "_s3_client", RecordingObjectStorage)
    monkeypatch.setattr(
        worker_tasks,
        "WORKER_SETTINGS",
        SimpleNamespace(s3_bucket="private-artifacts"),
    )

    worker_tasks._put_object("org/job/evidence.json", payload, "application/json")

    assert calls == [
        {
            "Bucket": "private-artifacts",
            "Key": "org/job/evidence.json",
            "Body": payload,
            "ContentLength": len(payload),
            "ContentType": "application/json",
            "Metadata": {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "immutable": "true",
            },
            "IfNoneMatch": "*",
        }
    ]
