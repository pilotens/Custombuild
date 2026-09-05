from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace
from typing import Any

import app.api as api_module
import pytest
from custombuild_manufacturing import ArtifactError, canonical_json_bytes
from custombuild_manufacturing import cam_software_provenance as provenance_module
from custombuild_manufacturing.cam_software_provenance import (
    build_cam_software_provenance,
    cam_software_provenance_sha256,
    parse_producer_build_identity,
)
from fastapi import HTTPException


def _fixture() -> tuple[SimpleNamespace, dict[str, Any], dict[str, bytes], dict[str, Any]]:
    engine_context = {
        "app_version": "1.0.0",
        "vcs_ref": "d" * 40,
        "source_manifest_sha256": "e" * 64,
        "dependency_lock_sha256": "f" * 64,
    }
    producer_build = parse_producer_build_identity(
        {
            "schema_version": "custombuild.producer-build-identity.v1",
            **engine_context,
        }
    )
    software_provenance = build_cam_software_provenance(producer_build)
    acceptance = {
        "status": "WORKSHOP_ACCEPTED",
        "evidence_id": "shop-acceptance-2026-09",
        "evidence_version": "1.0.0",
        "evidence_sha256": "1" * 64,
    }
    post_profile = {
        "profile_id": "shop-router-linuxcnc",
        "version": "2.0.0",
        "config_sha256": "2" * 64,
    }
    binding = {
        "schema_version": "custombuild.production-machine-profile.v1",
        "profile_class": "SERVER_OWNED_PRODUCTION",
        "document_sha256": "7" * 64,
        "payload_sha256": "3" * 64,
        "execution_context_sha256": "4" * 64,
        "acceptance": acceptance,
        "postprocessor_profile": post_profile,
    }
    artifacts = [
        {
            "path": "cam/toolpaths.v1.json",
            "media_type": "application/json",
            "role": "PRODUCTION_TOOLPATH_DOCUMENT",
            "size_bytes": 10,
            "sha256": "5" * 64,
        },
        {
            "path": "cam/cutting-backplot.svg",
            "media_type": "image/svg+xml",
            "role": "CUTTING_BACKPLOT",
            "size_bytes": 11,
            "sha256": "6" * 64,
        },
        {
            "path": "machine-production/production-machine-profile.v1.json",
            "media_type": "application/json",
            "role": "PRODUCTION_MACHINE_PROFILE_DOCUMENT",
            "size_bytes": 12,
            "sha256": "7" * 64,
        },
        {
            "path": "machine-production/program-index.v1.json",
            "media_type": "application/json",
            "role": "PRODUCTION_PROGRAM_INDEX",
            "size_bytes": 13,
            "sha256": "8" * 64,
        },
        {
            "path": "machine-production/linuxcnc/001.setup.tool.production.ngc",
            "media_type": "text/x-gcode",
            "role": "EXECUTABLE_CAM_CANDIDATE_PROGRAM",
            "size_bytes": 14,
            "sha256": "9" * 64,
        },
        {
            "path": "validation/cutting-program-report.json",
            "media_type": "application/json",
            "role": "CUTTING_PROGRAM_VALIDATION_REPORT",
            "size_bytes": 15,
            "sha256": "a" * 64,
        },
    ]
    manifest: dict[str, Any] = {
        "status": "CUTTING_CANDIDATE_GENERATED",
        "mode": "EXECUTABLE_CAM_CANDIDATE",
        "release_scope": "cam_candidate",
        "machine_use": "executable_cam_candidate",
        "physical_cutting_authorized": False,
        "workshop_acceptance_required": True,
        "candidate_context_hash": "b" * 64,
        "software_provenance": software_provenance,
        "base_design_review": {"bundle_sha256": "c" * 64},
        "toolpaths": {"sha256": "5" * 64},
        "postprocessor": {
            "id": software_provenance["implementations"]["postprocessor_id"],
            "version": software_provenance["implementations"]["postprocessor_version"],
        },
        "production_profile": {
            "path": "machine-production/production-machine-profile.v1.json",
            "schema_version": binding["schema_version"],
            "profile_class": binding["profile_class"],
            "payload_sha256": binding["payload_sha256"],
            "document_sha256": "7" * 64,
            "size_bytes": 12,
            "execution_context_sha256": binding["execution_context_sha256"],
            "acceptance": acceptance,
            "postprocessor_profile": {
                "path": "machine-production/linuxcnc-production-profile.v1.json",
                **post_profile,
            },
        },
        "production_machine_profile": {
            "path": "machine-production/linuxcnc-production-profile.v1.json",
            "profile_id": post_profile["profile_id"],
            "version": post_profile["version"],
            "config_sha256": post_profile["config_sha256"],
        },
        "artifacts": artifacts,
    }
    candidate_payload = b"exact-candidate-zip"
    result_evidence = [
        {
            "kind": "cam_candidate_bundle",
            "content_type": "application/zip",
            "sha256": hashlib.sha256(candidate_payload).hexdigest(),
            "size_bytes": len(candidate_payload),
        }
    ]
    kind_by_path = {
        "cam/toolpaths.v1.json": "cutting_toolpaths",
        "cam/cutting-backplot.svg": "cutting_backplot",
        "machine-production/production-machine-profile.v1.json": ("production_machine_profile"),
        "machine-production/program-index.v1.json": "machine_program_index",
        "machine-production/linuxcnc/001.setup.tool.production.ngc": ("machine_program_001"),
        "validation/cutting-program-report.json": "cutting_program_validation_report",
    }
    result_evidence.extend(
        {
            "kind": kind_by_path[entry["path"]],
            "content_type": entry["media_type"],
            "sha256": entry["sha256"],
            "size_bytes": entry["size_bytes"],
        }
        for entry in artifacts
    )
    result = {
        "bundle_sha256": "c" * 64,
        "cam_status": "CUTTING_CANDIDATE_GENERATED",
        "machine_program_mode": "EXECUTABLE_CAM_CANDIDATE",
        "production_machine_program": True,
        "physical_cutting_authorized": False,
        "workshop_acceptance_required": True,
        "evidence_artifacts": result_evidence,
        "cam_candidate": {
            "schema_version": "custombuild.cam-candidate-result.v2",
            "status": "CUTTING_CANDIDATE_GENERATED",
            "mode": "EXECUTABLE_CAM_CANDIDATE",
            "physical_cutting_authorized": False,
            "workshop_acceptance_required": True,
            "software_provenance": software_provenance,
            "software_provenance_sha256": cam_software_provenance_sha256(software_provenance),
            "production_profile_job_binding": binding,
            "production_profile_payload_sha256": binding["payload_sha256"],
            "execution_context_sha256": binding["execution_context_sha256"],
            "production_machine_profile_sha256": "7" * 64,
            "postprocessor_machine_profile_sha256": post_profile["config_sha256"],
            "base_design_review_bundle_sha256": "c" * 64,
            "bundle_sha256": hashlib.sha256(candidate_payload).hexdigest(),
            "bundle_size_bytes": len(candidate_payload),
            "manifest_sha256": hashlib.sha256(canonical_json_bytes(manifest)).hexdigest(),
            "candidate_context_hash": manifest["candidate_context_hash"],
            "toolpaths_sha256": "5" * 64,
            "program_count": 1,
            "postprocessor": manifest["postprocessor"],
        },
    }
    job = SimpleNamespace(
        request_json={
            "include_cutting_candidate": True,
            "production_machine_profile": binding,
        },
        production_engine_context_json=engine_context,
    )
    documents = {
        "production_bundle": b"exact-review-zip",
        "cam_candidate_bundle": candidate_payload,
    }
    return job, result, documents, manifest


def test_candidate_package_binding_covers_full_profile_and_every_public_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, result, documents, manifest = _fixture()
    monkeypatch.setattr(api_module, "get_settings", lambda: SimpleNamespace(app_env="production"))
    monkeypatch.setattr(
        api_module,
        "read_and_verify_cam_candidate_package",
        lambda payload,
        *,
        base_design_review_bundle,
        expected_producer_source_manifest_sha256,
        allow_test_only: manifest,
    )

    assert api_module._cam_candidate_package_binding_is_valid(
        job, result, documents, allow_test_only_profiles=False
    )
    tampered_manifest = copy.deepcopy(manifest)
    tampered_manifest["production_profile"]["acceptance"]["evidence_sha256"] = "f" * 64
    monkeypatch.setattr(
        api_module,
        "read_and_verify_cam_candidate_package",
        lambda payload,
        *,
        base_design_review_bundle,
        expected_producer_source_manifest_sha256,
        allow_test_only: tampered_manifest,
    )
    assert not api_module._cam_candidate_package_binding_is_valid(
        job, result, documents, allow_test_only_profiles=False
    )

    job, result, documents, manifest = _fixture()
    forged_binding = copy.deepcopy(job.request_json["production_machine_profile"])
    forged_binding["acceptance"]["evidence_sha256"] = "d" * 64
    job.request_json["production_machine_profile"] = forged_binding
    result["cam_candidate"]["production_profile_job_binding"] = copy.deepcopy(forged_binding)
    monkeypatch.setattr(
        api_module,
        "read_and_verify_cam_candidate_package",
        lambda payload,
        *,
        base_design_review_bundle,
        expected_producer_source_manifest_sha256,
        allow_test_only: manifest,
    )
    assert not api_module._cam_candidate_package_binding_is_valid(
        job, result, documents, allow_test_only_profiles=False
    )

    tampered_manifest = copy.deepcopy(manifest)
    program = next(
        entry
        for entry in tampered_manifest["artifacts"]
        if entry["role"] == "EXECUTABLE_CAM_CANDIDATE_PROGRAM"
    )
    program["sha256"] = "e" * 64
    monkeypatch.setattr(
        api_module,
        "read_and_verify_cam_candidate_package",
        lambda payload,
        *,
        base_design_review_bundle,
        expected_producer_source_manifest_sha256,
        allow_test_only: tampered_manifest,
    )
    assert not api_module._cam_candidate_package_binding_is_valid(
        job, result, documents, allow_test_only_profiles=False
    )


def test_candidate_package_binding_rejects_document_hash_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, result, documents, manifest = _fixture()
    job.request_json["production_machine_profile"]["document_sha256"] = "d" * 64
    result["cam_candidate"]["production_profile_job_binding"]["document_sha256"] = "d" * 64
    monkeypatch.setattr(
        api_module,
        "read_and_verify_cam_candidate_package",
        lambda payload,
        *,
        base_design_review_bundle,
        expected_producer_source_manifest_sha256,
        allow_test_only: manifest,
    )

    assert not api_module._cam_candidate_package_binding_is_valid(
        job, result, documents, allow_test_only_profiles=False
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("app_version", "2.0.0"),
        ("vcs_ref", "a" * 40),
        ("source_manifest_sha256", "0" * 64),
        ("dependency_lock_sha256", "1" * 64),
    ),
)
def test_candidate_result_and_package_bind_full_frozen_producer_identity(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: str,
) -> None:
    job, result, documents, manifest = _fixture()
    expectations = {entry["kind"]: object() for entry in result["evidence_artifacts"]}
    monkeypatch.setattr(
        api_module,
        "read_and_verify_cam_candidate_package",
        lambda payload,
        *,
        base_design_review_bundle,
        expected_producer_source_manifest_sha256,
        allow_test_only: manifest,
    )

    assert api_module._cam_candidate_job_binding_is_valid(job, result, expectations)
    assert api_module._cam_candidate_package_binding_is_valid(
        job, result, documents, allow_test_only_profiles=False
    )

    job.production_engine_context_json[field] = replacement
    assert not api_module._cam_candidate_job_binding_is_valid(job, result, expectations)
    assert not api_module._cam_candidate_package_binding_is_valid(
        job, result, documents, allow_test_only_profiles=False
    )


def test_historical_candidate_binding_uses_supported_frozen_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, result, documents, manifest = _fixture()
    expectations = {entry["kind"]: object() for entry in result["evidence_artifacts"]}
    observed_current_requirements: list[bool] = []

    def candidate_reader(
        payload: bytes,
        *,
        base_design_review_bundle: bytes,
        expected_producer_source_manifest_sha256: str,
        allow_test_only: bool,
        require_current_implementations: bool = True,
    ) -> dict[str, Any]:
        observed_current_requirements.append(require_current_implementations)
        return manifest

    monkeypatch.setattr(api_module, "read_and_verify_cam_candidate_package", candidate_reader)
    monkeypatch.setattr(
        provenance_module,
        "PRODUCTION_TOOLPATH_ENGINE_VERSION",
        "production-toolpaths-future",
    )

    assert not api_module._cam_candidate_job_binding_is_valid(job, result, expectations)
    assert not api_module._cam_candidate_package_binding_is_valid(
        job,
        result,
        documents,
        allow_test_only_profiles=False,
    )
    assert api_module._cam_candidate_job_binding_is_valid(
        job,
        result,
        expectations,
        require_current_implementations=False,
    )
    assert api_module._cam_candidate_package_binding_is_valid(
        job,
        result,
        documents,
        allow_test_only_profiles=False,
        require_current_implementations=False,
    )
    assert observed_current_requirements == [True, False]


def test_historical_release_archive_disables_only_the_current_implementation_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = SimpleNamespace(
        release=SimpleNamespace(id="release-id"),
        version=object(),
        job=object(),
        binding=("frozen", "archive"),
    )
    observed: dict[str, Any] = {}

    monkeypatch.setattr(api_module, "_require_current_retention_binding", lambda *_args: None)
    monkeypatch.setattr(api_module, "_release_build_identity", lambda _job: object())
    monkeypatch.setattr(
        api_module,
        "get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )

    def review_issues(
        *_args: Any,
        **kwargs: Any,
    ) -> tuple[list[str], list[str], bool]:
        observed.update(kwargs)
        return [], [], True

    monkeypatch.setattr(api_module, "_review_evidence_issues_owned", review_issues)
    monkeypatch.setattr(api_module, "_resolve_release_archive", lambda *_args, **_kw: archive)

    assert api_module._verify_release_archive_owned(object(), "org-id", archive) is archive
    assert observed["require_current_cam_implementations"] is False
    assert observed["allow_test_only_profiles"] is False


def test_production_never_enables_test_only_candidate_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, result, documents, manifest = _fixture()
    job.request_json["production_machine_profile"]["profile_class"] = "TEST_ONLY"
    result["cam_candidate"]["production_profile_job_binding"]["profile_class"] = "TEST_ONLY"
    manifest["production_profile"]["profile_class"] = "TEST_ONLY"
    observed: list[bool] = []

    def test_only_reader(
        payload: bytes,
        *,
        base_design_review_bundle: bytes,
        expected_producer_source_manifest_sha256: str,
        allow_test_only: bool,
    ) -> dict[str, Any]:
        observed.append(allow_test_only)
        if not allow_test_only:
            raise ArtifactError("test-only profile is forbidden")
        return manifest

    monkeypatch.setattr(api_module, "read_and_verify_cam_candidate_package", test_only_reader)

    assert not api_module._cam_candidate_package_binding_is_valid(
        job, result, documents, allow_test_only_profiles=False
    )
    assert observed == [False]


def test_candidate_approval_digest_distinguishes_absence_from_malformed_identity() -> None:
    with pytest.raises(HTTPException, match="requires a verified executable cutting candidate"):
        api_module._cam_candidate_bundle_sha256({})
    assert api_module._cam_candidate_bundle_sha256(
        {"cam_candidate": {"bundle_sha256": "a" * 64}}
    ) == ("a" * 64)

    with pytest.raises(HTTPException, match="CAM candidate binding is malformed"):
        api_module._cam_candidate_bundle_sha256({"cam_candidate": {"bundle_sha256": "A" * 64}})


@pytest.mark.parametrize(
    ("request_class", "request_status", "candidate_class", "candidate_status"),
    (
        ("TEST_ONLY", "TEST_ONLY", "TEST_ONLY", "TEST_ONLY"),
        (
            "SERVER_OWNED_PRODUCTION",
            "WORKSHOP_ACCEPTED",
            "TEST_ONLY",
            "TEST_ONLY",
        ),
        (
            "TEST_ONLY",
            "TEST_ONLY",
            "SERVER_OWNED_PRODUCTION",
            "WORKSHOP_ACCEPTED",
        ),
    ),
)
def test_cam_promotion_never_accepts_a_test_only_profile_binding(
    request_class: str,
    request_status: str,
    candidate_class: str,
    candidate_status: str,
) -> None:
    request_binding = {
        "profile_class": request_class,
        "acceptance": {"status": request_status},
    }
    candidate_binding = {
        "profile_class": candidate_class,
        "acceptance": {"status": candidate_status},
    }
    job: Any = SimpleNamespace(
        request_json={"production_machine_profile": request_binding},
        result_json={
            "cam_candidate": {
                "production_profile_job_binding": candidate_binding,
            }
        },
    )

    with pytest.raises(
        HTTPException,
        match="CAM promotion requires a workshop-accepted production profile",
    ):
        api_module._require_promotable_cam_candidate_profile(job)


def test_cam_promotion_accepts_only_matching_current_workshop_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = {
        "profile_class": "SERVER_OWNED_PRODUCTION",
        "acceptance": {"status": "WORKSHOP_ACCEPTED"},
    }
    job: Any = SimpleNamespace(
        request_json={"production_machine_profile": copy.deepcopy(binding)},
        result_json={
            "cam_candidate": {
                "production_profile_job_binding": copy.deepcopy(binding),
            }
        },
    )

    current_profile = object()
    monkeypatch.setattr(
        api_module,
        "get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            production_cam_profile_source=b"current-protected-profile",
        ),
    )
    monkeypatch.setattr(
        api_module,
        "load_production_machine_profile",
        lambda source, *, allow_test_only: current_profile,
    )
    monkeypatch.setattr(
        api_module,
        "production_machine_profile_job_binding",
        lambda profile: copy.deepcopy(binding),
    )

    api_module._require_promotable_cam_candidate_profile(job)
    no_candidate_job: Any = SimpleNamespace(request_json={}, result_json={})
    with pytest.raises(HTTPException, match="requires a verified executable cutting candidate"):
        api_module._require_promotable_cam_candidate_profile(no_candidate_job)


def test_cam_promotion_rejects_profile_rotation_and_unavailable_current_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_a = {
        "profile_class": "SERVER_OWNED_PRODUCTION",
        "acceptance": {"status": "WORKSHOP_ACCEPTED"},
        "document_sha256": "a" * 64,
    }
    profile_b = copy.deepcopy(profile_a)
    profile_b["document_sha256"] = "b" * 64
    job: Any = SimpleNamespace(
        request_json={"production_machine_profile": copy.deepcopy(profile_a)},
        result_json={
            "cam_candidate": {
                "production_profile_job_binding": copy.deepcopy(profile_a),
            }
        },
    )
    monkeypatch.setattr(
        api_module,
        "get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            production_cam_profile_source=b"rotated-protected-profile",
        ),
    )
    monkeypatch.setattr(
        api_module,
        "load_production_machine_profile",
        lambda source, *, allow_test_only: object(),
    )
    monkeypatch.setattr(
        api_module,
        "production_machine_profile_job_binding",
        lambda profile: copy.deepcopy(profile_b),
    )

    with pytest.raises(HTTPException, match="stale production machine profile") as stale:
        api_module._require_promotable_cam_candidate_profile(job)
    assert stale.value.status_code == 409

    def unavailable(source: object, *, allow_test_only: bool) -> object:
        raise OSError("protected profile disappeared")

    monkeypatch.setattr(api_module, "load_production_machine_profile", unavailable)
    with pytest.raises(HTTPException, match="cannot verify the current") as unavailable_error:
        api_module._require_promotable_cam_candidate_profile(job)
    assert unavailable_error.value.status_code == 409
