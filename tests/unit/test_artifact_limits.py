from __future__ import annotations

from pathlib import Path

import pytest
from app.schemas import ArtifactRead
from custombuild_manufacturing.artifact_limits import (
    MAX_API_TRANSIENT_BYTES,
    MAX_ARTIFACT_BYTES,
    MAX_CATALOG_SOURCE_BYTES,
    MAX_CORE_DOCUMENT_BYTES,
    MAX_EVIDENCE_ARTIFACTS,
    MAX_EVIDENCE_TOTAL_BYTES,
    MAX_HTTP_REQUEST_BYTES,
    MAX_READINESS_STATUS_BYTES,
    artifact_size_limit,
    valid_artifact_size,
)
from pydantic import ValidationError

MIB = 1024 * 1024


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("production_bundle", MAX_ARTIFACT_BYTES),
        ("design_glb", MAX_ARTIFACT_BYTES),
        ("catalog_source", MAX_CATALOG_SOURCE_BYTES),
        ("workshop_readiness", MAX_READINESS_STATUS_BYTES),
        ("design_review_package_status", MAX_READINESS_STATUS_BYTES),
        ("manifest", MAX_CORE_DOCUMENT_BYTES),
        ("dfm_report", MAX_CORE_DOCUMENT_BYTES),
        ("manufacturing_intent", MAX_CORE_DOCUMENT_BYTES),
        ("supplier_handoff", MAX_CORE_DOCUMENT_BYTES),
        ("setup_sheet_001", MAX_CORE_DOCUMENT_BYTES),
    ),
)
def test_artifact_size_limit_is_shared_by_kind(kind: str, expected: int) -> None:
    assert artifact_size_limit(kind) == expected


@pytest.mark.parametrize("invalid", (True, False, 0, -1, 1.0, "1", None))
def test_artifact_size_validation_rejects_coerced_or_nonpositive_values(invalid: object) -> None:
    assert valid_artifact_size("manifest", invalid) is False


def test_artifact_size_validation_accepts_only_through_the_exact_ceiling() -> None:
    assert valid_artifact_size("manifest", MAX_CORE_DOCUMENT_BYTES) is True
    assert valid_artifact_size("manifest", MAX_CORE_DOCUMENT_BYTES + 1) is False


def test_verified_spool_budget_fits_the_api_tmpfs_with_a_hard_safety_margin() -> None:
    compose = Path("compose.yml").read_text(encoding="utf-8")

    assert "- /tmp:size=128m,mode=1777" in compose
    assert MAX_ARTIFACT_BYTES == 32 * MIB
    assert MAX_API_TRANSIENT_BYTES == 96 * MIB
    assert MAX_HTTP_REQUEST_BYTES == 21 * MIB
    assert MAX_ARTIFACT_BYTES <= MAX_API_TRANSIENT_BYTES
    assert MAX_API_TRANSIENT_BYTES <= 128 * MIB - 32 * MIB


def test_evidence_inventory_has_shared_bounded_count_and_total_size() -> None:
    assert MAX_EVIDENCE_ARTIFACTS == 512
    assert MAX_EVIDENCE_TOTAL_BYTES == 96 * MIB
    assert MAX_EVIDENCE_TOTAL_BYTES == MAX_API_TRANSIENT_BYTES


@pytest.mark.parametrize("invalid_size", (True, False, 0, -1, 1.0, MAX_ARTIFACT_BYTES + 1))
def test_artifact_response_schema_rejects_coerced_empty_or_oversize_claims(
    invalid_size: object,
) -> None:
    with pytest.raises(ValidationError):
        ArtifactRead.model_validate(
            {
                "id": "artifact-1",
                "kind": "production_bundle",
                "sha256": "a" * 64,
                "size_bytes": invalid_size,
                "content_type": "application/zip",
                "download_url": "/v1/artifacts/artifact-1/download?signature=x",
                "download_path": "/v1/artifacts/artifact-1/download?signature=x",
            }
        )
