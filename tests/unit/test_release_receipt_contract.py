from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from app.schemas import ReleaseRead
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _design_review_receipt() -> dict[str, object]:
    return {
        "release_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "release_number": "R8",
        "status": "released",
        "bundle_sha256": "b" * 64,
        "manifest_sha256": "d" * 64,
        "release_kind": "design_review",
        "machine_use": "validation_only",
        "physical_cutting_authorized": False,
    }


def _executable_receipt() -> dict[str, object]:
    return {
        "release_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "release_number": "R8",
        "status": "released",
        "release_kind": "executable_cam",
        "machine_use": "executable_cam_candidate",
        "design_review_bundle": {
            "bundle_sha256": "b" * 64,
            "manifest_sha256": "d" * 64,
        },
        "executable_cam": {
            "candidate_bundle_sha256": "c" * 64,
            "candidate_manifest_sha256": "e" * 64,
            "production_profile_document_sha256": "f" * 64,
            "production_profile_payload_sha256": "1" * 64,
            "program_inventory_sha256": "2" * 64,
            "cam_approval": {
                "schema_version": "custombuild.cam-approval-release-binding.v1",
                "approval_id": "33333333-3333-4333-8333-333333333333",
                "organization_id": "11111111-1111-4111-8111-111111111111",
                "design_version_id": "55555555-5555-4555-8555-555555555555",
                "approval_type": "cam",
                "approved_by": "66666666-6666-4666-8666-666666666666",
                "reason": "Verifierad CAM-kandidat för frisläppning",
                "generation_job_id": "44444444-4444-4444-8444-444444444444",
                "production_context_hash": "3" * 64,
                "manifest_sha256": "d" * 64,
                "candidate_bundle_sha256": "c" * 64,
                "overrides_json": [],
                "created_at": "2026-09-01T10:30:00.000000Z",
                "updated_at": "2026-09-01T10:30:00.000000Z",
                "binding_sha256": "4" * 64,
            },
            "workshop_acceptance_required": True,
        },
        "physical_cutting_authorized": False,
    }


@pytest.mark.parametrize("payload_factory", [_design_review_receipt, _executable_receipt])
def test_release_variants_forbid_unknown_fields_and_noncanonical_identity(
    payload_factory: Callable[[], dict[str, object]],
) -> None:
    payload = payload_factory()
    assert ReleaseRead.model_validate(payload).root.release_number == "R8"

    unexpected = copy.deepcopy(payload)
    unexpected["future_claim"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ReleaseRead.model_validate(unexpected)

    bad_uuid = copy.deepcopy(payload)
    bad_uuid["release_id"] = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        ReleaseRead.model_validate(bad_uuid)

    bad_number = copy.deepcopy(payload)
    bad_number["release_number"] = "release-8"
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        ReleaseRead.model_validate(bad_number)


def test_executable_receipt_rejects_design_overrides() -> None:
    payload = _executable_receipt()
    payload["executable_cam"]["cam_approval"]["overrides_json"] = [  # type: ignore[index]
        {"rule_id": "not-valid-for-cam"}
    ]
    with pytest.raises(ValidationError, match="too_long"):
        ReleaseRead.model_validate(payload)


def test_openapi_release_variants_preserve_closed_canonical_contract() -> None:
    schemas = json.loads((ROOT / "packages/contracts/openapi.json").read_text())["components"][
        "schemas"
    ]
    release_schema = schemas["ReleaseRead"]
    assert release_schema["discriminator"] == {
        "propertyName": "release_kind",
        "mapping": {
            "design_review": "#/components/schemas/DesignReviewReleaseRead",
            "executable_cam": "#/components/schemas/ExecutableCAMReleaseRead",
        },
    }
    for name in ("DesignReviewReleaseRead", "ExecutableCAMReleaseRead"):
        schema = schemas[name]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["release_id"]["pattern"] == UUID_PATTERN
        assert schema["properties"]["release_number"]["pattern"] == (r"^[A-Z0-9][A-Z0-9._-]{0,39}$")
    approval = schemas["CAMApprovalReleaseBinding"]
    assert approval["additionalProperties"] is False
    assert approval["properties"]["overrides_json"]["maxItems"] == 0
