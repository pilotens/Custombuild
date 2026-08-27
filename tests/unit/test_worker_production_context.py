from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import custombuild_manufacturing.pipeline as manufacturing_pipeline
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
from custombuild_manufacturing import ProductionBlockedError
from custombuild_manufacturing.production_context import (
    generation_context_hash,
    resolve_production_components,
)
from custombuild_rules import RULES_VERSION


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
        "postprocessor_id": "linuxcnc-validation-1.0.0",
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
    )
    return job, version


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

    result = worker_tasks._generate(job, version)

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

    result = worker_tasks._generate(job, version)

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

    result = worker_tasks._generate(job, version)

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
        worker_tasks._generate(job, version)

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
        worker_tasks._generate(job, version)

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
