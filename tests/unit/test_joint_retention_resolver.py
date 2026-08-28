from __future__ import annotations

import hashlib
import json

import pytest

from app.joint_retention import (
    CATALOG_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    JointRetentionEvidenceError,
    resolve_joint_retention_contract,
)
from custombuild_domain import (
    JointRetentionLoadMode,
    JointRetentionMachiningScope,
    JointRetentionMethod,
)


def digest(char: str) -> str:
    return char * 64


def catalogue() -> dict[str, object]:
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "system_id": "mechanical-dado-lock",
        "system_version": "1.0.0",
        "joint_type": "dado",
        "method": "mechanical",
        "installation_instruction_id": "install-mechanical-dado-lock",
        "installation_instruction_version": "1.0.0",
        "installation_instruction_sha256": digest("a"),
        "machining_scope": "no_additional_cnc",
        "hardware_sku": "SCREW-4X40-001",
        "hardware_count_per_joint": 2,
        "minimum_applicable_thickness_um": 17_500,
        "maximum_applicable_thickness_um": 18_500,
        "safety_factor_permille": 2_000,
    }


def evidence_payload() -> dict[str, object]:
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "system_id": "mechanical-dado-lock",
        "system_version": "1.0.0",
        "joint_geometry_sha256": digest("b"),
        "material_id": "mdf-18",
        "material_version": "1.0.0",
        "tested_thickness_um": 18_000,
        "test_report_id": "retention-test-2026-001",
        "issuer": "independent-furniture-test-lab",
        "issued_at": "2026-08-28T08:00:00Z",
        "load_cases": [
            {
                "mode": "shear",
                "rated_design_load_n": 300,
                "verified_capacity_n": 900,
            },
            {
                "mode": "withdrawal",
                "rated_design_load_n": 200,
                "verified_capacity_n": 600,
            },
        ],
    }


def evidence_bytes(payload: dict[str, object] | None = None) -> bytes:
    return json.dumps(
        payload or evidence_payload(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def resolve(
    *,
    catalog: dict[str, object] | None = None,
    payload: dict[str, object] | None = None,
    expected_hash: str | None = None,
):
    raw = evidence_bytes(payload)
    return resolve_joint_retention_contract(
        catalog_entry=catalog or catalogue(),
        evidence_id="external-evidence-001",
        evidence_sha256=expected_hash or hashlib.sha256(raw).hexdigest(),
        evidence_bytes=raw,
        expected_joint_geometry_sha256=digest("b"),
        expected_material_id="mdf-18",
        expected_material_version="1.0.0",
        expected_thickness_um=18_000,
    )


def test_verified_mechanical_evidence_creates_exact_domain_contract() -> None:
    contract = resolve()
    assert contract.method is JointRetentionMethod.MECHANICAL
    assert contract.machining_scope is JointRetentionMachiningScope.NO_ADDITIONAL_CNC
    assert contract.evidence_id == "external-evidence-001"
    assert contract.joint_geometry_sha256 == digest("b")
    assert contract.load_cases[0].mode is JointRetentionLoadMode.SHEAR
    assert contract.load_cases[1].mode is JointRetentionLoadMode.WITHDRAWAL
    assert contract.applicable_materials[0].material_id == "mdf-18"
    assert contract.hardware_sku == "SCREW-4X40-001"


def test_tampered_evidence_checksum_fails_closed() -> None:
    with pytest.raises(JointRetentionEvidenceError, match="checksum mismatch"):
        resolve(expected_hash=digest("f"))


def test_evidence_for_another_geometry_fails_closed() -> None:
    payload = evidence_payload()
    payload["joint_geometry_sha256"] = digest("9")
    with pytest.raises(JointRetentionEvidenceError, match="another joint geometry"):
        resolve(payload=payload)


def test_evidence_for_another_material_or_thickness_fails_closed() -> None:
    payload = evidence_payload()
    payload["material_id"] = "birch-plywood-18"
    with pytest.raises(JointRetentionEvidenceError, match="another material"):
        resolve(payload=payload)

    payload = evidence_payload()
    payload["tested_thickness_um"] = 17_900
    with pytest.raises(JointRetentionEvidenceError, match="another measured thickness"):
        resolve(payload=payload)


def test_catalogue_and_evidence_system_must_match() -> None:
    catalog = catalogue()
    catalog["system_version"] = "2.0.0"
    with pytest.raises(JointRetentionEvidenceError, match="version does not match"):
        resolve(catalog=catalog)


def test_unknown_fields_and_adhesive_methods_cannot_cross_boundary() -> None:
    payload = evidence_payload()
    payload["browser_verified"] = True
    with pytest.raises(JointRetentionEvidenceError, match="unexpected schema"):
        resolve(payload=payload)

    catalog = catalogue()
    catalog["method"] = "adhesive"
    with pytest.raises(JointRetentionEvidenceError, match="unsupported joint-retention catalogue enum"):
        resolve(catalog=catalog)


def test_unattributed_test_evidence_fails_closed() -> None:
    for field in ("test_report_id", "issuer", "issued_at"):
        payload = evidence_payload()
        payload[field] = ""
        with pytest.raises(JointRetentionEvidenceError, match=field):
            resolve(payload=payload)


def test_capacity_below_safety_factor_is_rejected_by_domain_contract() -> None:
    payload = evidence_payload()
    load_cases = payload["load_cases"]
    assert isinstance(load_cases, list)
    shear = load_cases[0]
    assert isinstance(shear, dict)
    shear["verified_capacity_n"] = 500
    with pytest.raises(JointRetentionEvidenceError, match="failed domain validation"):
        resolve(payload=payload)


def test_feature_bound_retention_requires_generated_sorted_feature_ids() -> None:
    catalog = catalogue()
    catalog["method"] = "dry_self_locking"
    catalog["machining_scope"] = "features_bound_to_joint"
    with pytest.raises(JointRetentionEvidenceError, match="requires generated feature IDs"):
        resolve(catalog=catalog)

    raw = evidence_bytes()
    contract = resolve_joint_retention_contract(
        catalog_entry=catalog,
        evidence_id="external-evidence-001",
        evidence_sha256=hashlib.sha256(raw).hexdigest(),
        evidence_bytes=raw,
        expected_joint_geometry_sha256=digest("b"),
        expected_material_id="mdf-18",
        expected_material_version="1.0.0",
        expected_thickness_um=18_000,
        bound_feature_ids=("feature-001", "feature-002"),
    )
    assert contract.method is JointRetentionMethod.DRY_SELF_LOCKING
    assert contract.bound_feature_ids == ("feature-001", "feature-002")
