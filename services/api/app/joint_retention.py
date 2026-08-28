"""Server-side trust boundary for joint-retention evidence.

The browser is never allowed to manufacture a ``JointRetentionContract``. This
module converts a reviewed, versioned server catalogue entry plus immutable
checksum-verified test evidence into the frozen domain contract used by the
construction and CAM gates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from custombuild_domain import (
    JointRetentionContract,
    JointRetentionLoadCase,
    JointRetentionLoadMode,
    JointRetentionMachiningScope,
    JointRetentionMaterialIdentity,
    JointRetentionMethod,
    JointType,
    content_hash,
)
from pydantic import ValidationError

CATALOG_SCHEMA_VERSION = "custombuild.joint-retention-catalog.v1"
EVIDENCE_SCHEMA_VERSION = "custombuild.joint-retention-test-evidence.v1"

_CATALOG_KEYS = frozenset(
    {
        "schema_version",
        "system_id",
        "system_version",
        "joint_type",
        "method",
        "installation_instruction_id",
        "installation_instruction_version",
        "installation_instruction_sha256",
        "machining_scope",
        "hardware_sku",
        "hardware_count_per_joint",
        "minimum_applicable_thickness_um",
        "maximum_applicable_thickness_um",
        "safety_factor_permille",
    }
)
_EVIDENCE_KEYS = frozenset(
    {
        "schema_version",
        "system_id",
        "system_version",
        "joint_geometry_sha256",
        "material_id",
        "material_version",
        "tested_thickness_um",
        "test_report_id",
        "issuer",
        "issued_at",
        "load_cases",
    }
)
_LOAD_CASE_KEYS = frozenset({"mode", "rated_design_load_n", "verified_capacity_n"})


class JointRetentionEvidenceError(ValueError):
    """Raised when external retention evidence cannot cross the trust boundary."""


def _mapping(value: Any, *, keys: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise JointRetentionEvidenceError(f"{name} has an unexpected schema")
    return value


def _nonblank(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise JointRetentionEvidenceError(f"{field} must be a canonical non-blank string")
    return value


def _sha256(value: Any, field: str) -> str:
    text = _nonblank(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise JointRetentionEvidenceError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise JointRetentionEvidenceError(f"{field} must be a positive integer")
    return value


def _parse_evidence(evidence_bytes: bytes, *, expected_sha256: str) -> Mapping[str, Any]:
    if hashlib.sha256(evidence_bytes).hexdigest() != expected_sha256:
        raise JointRetentionEvidenceError("joint-retention evidence checksum mismatch")
    try:
        raw = json.loads(evidence_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JointRetentionEvidenceError("joint-retention evidence must be UTF-8 JSON") from exc
    return _mapping(raw, keys=_EVIDENCE_KEYS, name="joint-retention evidence")


def resolve_joint_retention_contract(
    *,
    catalog_entry: Mapping[str, Any],
    evidence_id: str,
    evidence_sha256: str,
    evidence_bytes: bytes,
    expected_joint_geometry_sha256: str,
    expected_material_id: str,
    expected_material_version: str,
    expected_thickness_um: int,
    bound_feature_ids: tuple[str, ...] = (),
) -> JointRetentionContract:
    """Create a frozen retention contract from server-owned, verified inputs.

    The caller must load ``catalog_entry`` from the tenant's reviewed versioned
    joint catalogue and ``evidence_bytes`` from immutable storage after tenant,
    project, design-hash, expiry and revocation checks. The function independently
    rechecks content identity and all design-specific applicability constraints.
    """

    catalog = _mapping(catalog_entry, keys=_CATALOG_KEYS, name="joint-retention catalogue")
    if catalog["schema_version"] != CATALOG_SCHEMA_VERSION:
        raise JointRetentionEvidenceError("unsupported joint-retention catalogue schema")
    if catalog["joint_type"] != JointType.DADO.value:
        raise JointRetentionEvidenceError("only DADO retention is supported by this resolver")
    evidence_id = _nonblank(evidence_id, "evidence_id")
    evidence_sha256 = _sha256(evidence_sha256, "evidence_sha256")
    expected_joint_geometry_sha256 = _sha256(
        expected_joint_geometry_sha256,
        "expected_joint_geometry_sha256",
    )
    expected_material_id = _nonblank(expected_material_id, "expected_material_id")
    expected_material_version = _nonblank(
        expected_material_version,
        "expected_material_version",
    )
    expected_thickness_um = _positive_int(expected_thickness_um, "expected_thickness_um")

    evidence = _parse_evidence(evidence_bytes, expected_sha256=evidence_sha256)
    if evidence["schema_version"] != EVIDENCE_SCHEMA_VERSION:
        raise JointRetentionEvidenceError("unsupported joint-retention evidence schema")
    _nonblank(evidence["test_report_id"], "test_report_id")
    _nonblank(evidence["issuer"], "issuer")
    _nonblank(evidence["issued_at"], "issued_at")
    if evidence["system_id"] != catalog["system_id"]:
        raise JointRetentionEvidenceError("evidence system does not match the selected catalogue entry")
    if evidence["system_version"] != catalog["system_version"]:
        raise JointRetentionEvidenceError("evidence version does not match the selected catalogue entry")
    if evidence["joint_geometry_sha256"] != expected_joint_geometry_sha256:
        raise JointRetentionEvidenceError("evidence is bound to another joint geometry")
    if evidence["material_id"] != expected_material_id:
        raise JointRetentionEvidenceError("evidence is bound to another material")
    if evidence["material_version"] != expected_material_version:
        raise JointRetentionEvidenceError("evidence is bound to another material version")
    if evidence["tested_thickness_um"] != expected_thickness_um:
        raise JointRetentionEvidenceError("evidence is bound to another measured thickness")

    minimum_thickness = _positive_int(
        catalog["minimum_applicable_thickness_um"],
        "minimum_applicable_thickness_um",
    )
    maximum_thickness = _positive_int(
        catalog["maximum_applicable_thickness_um"],
        "maximum_applicable_thickness_um",
    )
    if not minimum_thickness <= expected_thickness_um <= maximum_thickness:
        raise JointRetentionEvidenceError("measured thickness is outside the retention catalogue range")

    raw_load_cases = evidence["load_cases"]
    if not isinstance(raw_load_cases, list) or len(raw_load_cases) != 2:
        raise JointRetentionEvidenceError("retention evidence requires shear and withdrawal load cases")
    load_cases: list[JointRetentionLoadCase] = []
    for item in raw_load_cases:
        load_case = _mapping(item, keys=_LOAD_CASE_KEYS, name="joint-retention load case")
        try:
            load_cases.append(
                JointRetentionLoadCase(
                    mode=JointRetentionLoadMode(load_case["mode"]),
                    rated_design_load_n=_positive_int(
                        load_case["rated_design_load_n"],
                        "rated_design_load_n",
                    ),
                    verified_capacity_n=_positive_int(
                        load_case["verified_capacity_n"],
                        "verified_capacity_n",
                    ),
                )
            )
        except (TypeError, ValueError) as exc:
            raise JointRetentionEvidenceError("unsupported retention load mode") from exc

    try:
        method = JointRetentionMethod(catalog["method"])
        machining_scope = JointRetentionMachiningScope(catalog["machining_scope"])
    except (TypeError, ValueError) as exc:
        raise JointRetentionEvidenceError("unsupported joint-retention catalogue enum") from exc

    if machining_scope == JointRetentionMachiningScope.NO_ADDITIONAL_CNC and bound_feature_ids:
        raise JointRetentionEvidenceError("non-CNC retention must not claim manufacturing feature IDs")
    if machining_scope == JointRetentionMachiningScope.FEATURES_BOUND_TO_JOINT:
        if not bound_feature_ids:
            raise JointRetentionEvidenceError("feature-bound retention requires generated feature IDs")
        if tuple(sorted(set(bound_feature_ids))) != tuple(bound_feature_ids):
            raise JointRetentionEvidenceError("bound retention feature IDs must be sorted and unique")

    try:
        return JointRetentionContract(
            system_id=_nonblank(catalog["system_id"], "system_id"),
            system_version=_nonblank(catalog["system_version"], "system_version"),
            joint_type=JointType.DADO,
            method=method,
            catalog_entry_sha256=content_hash(catalog),
            evidence_id=evidence_id,
            evidence_sha256=evidence_sha256,
            installation_instruction_id=_nonblank(
                catalog["installation_instruction_id"],
                "installation_instruction_id",
            ),
            installation_instruction_version=_nonblank(
                catalog["installation_instruction_version"],
                "installation_instruction_version",
            ),
            installation_instruction_sha256=_sha256(
                catalog["installation_instruction_sha256"],
                "installation_instruction_sha256",
            ),
            machining_scope=machining_scope,
            hardware_sku=_nonblank(catalog["hardware_sku"], "hardware_sku"),
            hardware_count_per_joint=_positive_int(
                catalog["hardware_count_per_joint"],
                "hardware_count_per_joint",
            ),
            applicable_materials=(
                JointRetentionMaterialIdentity(
                    material_id=expected_material_id,
                    material_version=expected_material_version,
                ),
            ),
            joint_geometry_sha256=expected_joint_geometry_sha256,
            minimum_applicable_thickness_um=minimum_thickness,
            maximum_applicable_thickness_um=maximum_thickness,
            load_cases=tuple(load_cases),
            safety_factor_permille=_positive_int(
                catalog["safety_factor_permille"],
                "safety_factor_permille",
            ),
            bound_feature_ids=bound_feature_ids,
        )
    except ValidationError as exc:
        raise JointRetentionEvidenceError("joint-retention contract failed domain validation") from exc
