from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

import pytest
from custombuild_manufacturing.grain import DFM_GRAIN_REQUIRED_ACTION
from custombuild_manufacturing.readiness import (
    DESIGN_REVIEW_RELEASE_SCOPE,
    LEGACY_WORKSHOP_READINESS_SCHEMA_VERSION,
    VALIDATION_ONLY_MACHINE_USE,
    WORKSHOP_READINESS_SCHEMA_VERSION,
    ReadinessStatus,
    ReadinessValidationError,
    WorkshopReadinessReport,
    build_workshop_readiness_report,
    normalize_workshop_readiness_report,
)

SOFTWARE_REQUIREMENTS = [
    ("AUTHORITATIVE_CAD", "Authoritative CAD geometry"),
    ("DFM_SCREEN", "Manufacturing feasibility screen"),
    ("SEMANTIC_OPERATIONS", "Semantic machining operations"),
    ("SETUP_SHEETS", "Setup sheets"),
    ("VALIDATION_BACKPLOT", "Independent review backplot"),
    ("NON_CUTTING_PROGRAM", "Non-cutting controller validation"),
]
WORKSHOP_REQUIREMENTS = [
    ("WALL_ANCHOR", "Wall substrate and anchor system"),
    ("CABINET_HARDWARE", "Base-cabinet hardware and drill pattern"),
    ("MATERIAL_GRAIN", "Structured sheet-material grain-axis binding"),
    ("MACHINE_CALIBRATION", "Calibrated physical machine"),
    ("WCS_CONVENTION", "Verified WCS and origin convention"),
    ("MEASURED_TOOLING", "Measured tool, holder and runout"),
    ("MATERIAL_BATCH", "Verified material batch"),
    ("JOINT_COUPONS", "Joint coupon and tolerance test"),
    (
        "MATERIAL_REMOVAL_COMPARISON",
        "Independent material-removal comparison",
    ),
    ("SUPERVISED_AIR_CUT", "Supervised air cut"),
    ("REFERENCE_PART", "Measured reference part"),
    ("PROTOTYPE_BUILD", "Complete prototype furniture build"),
    ("CNC_OPERATOR_APPROVAL", "Named CNC operator approval"),
    (
        "FURNITURE_CONSTRUCTOR_APPROVAL",
        "Named furniture constructor approval",
    ),
]
EDGE_BAND_REQUIREMENT = (
    "EDGE_BAND_SYSTEM",
    "Adhesive-free mechanical edge protection and cut-size compensation",
)
REPORT_V2_KEYS = {
    "schema_version",
    "release_scope",
    "machine_use",
    "edge_band_selection_required",
    "design_review_ready",
    "physical_cutting_authorized",
    "missing_evidence_count",
    "software_evidence",
    "workshop_evidence",
}
REQUIREMENT_KEYS = {"code", "title", "status", "evidence", "required_action"}


def test_readiness_validation_error_remains_a_value_error_subclass() -> None:
    assert issubclass(ReadinessValidationError, ValueError)


def complete_report(*, edge_band: bool = False) -> dict[str, Any]:
    return build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=True,
        operation_count=36,
        setup_count=2,
        validation_backplot=True,
        validation_program=True,
        edge_band_selection_required=edge_band,
    ).as_dict()


def report_from_input_overrides(**overrides: Any) -> WorkshopReadinessReport:
    inputs = {
        "authoritative_cad": True,
        "dfm_passed": True,
        "operation_count": 1,
        "setup_count": 1,
        "validation_backplot": True,
        "validation_program": True,
        "edge_band_selection_required": False,
        "material_grain_binding_required": True,
        **overrides,
    }
    return build_workshop_readiness_report(
        authoritative_cad=inputs["authoritative_cad"],
        dfm_passed=inputs["dfm_passed"],
        operation_count=inputs["operation_count"],
        setup_count=inputs["setup_count"],
        validation_backplot=inputs["validation_backplot"],
        validation_program=inputs["validation_program"],
        edge_band_selection_required=inputs["edge_band_selection_required"],
        material_grain_binding_required=inputs["material_grain_binding_required"],
    )


def legacy_report(*, edge_band: bool = False) -> dict[str, Any]:
    payload = complete_report(edge_band=edge_band)
    payload["schema_version"] = LEGACY_WORKSHOP_READINESS_SCHEMA_VERSION
    del payload["release_scope"]
    del payload["machine_use"]
    del payload["edge_band_selection_required"]
    return payload


def test_complete_software_evidence_never_authorizes_physical_cutting() -> None:
    report = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=True,
        operation_count=36,
        setup_count=2,
        validation_backplot=True,
        validation_program=True,
    )
    payload = report.as_dict()

    assert report.schema_version == WORKSHOP_READINESS_SCHEMA_VERSION
    assert payload.keys() == REPORT_V2_KEYS
    assert payload["release_scope"] == DESIGN_REVIEW_RELEASE_SCOPE
    assert payload["machine_use"] == VALIDATION_ONLY_MACHINE_USE
    assert payload["edge_band_selection_required"] is False
    assert report.design_review_ready is True
    assert report.physical_cutting_authorized is False
    assert all(item.status is ReadinessStatus.VERIFIED for item in report.software_evidence)
    assert all(
        item.status is ReadinessStatus.EXTERNAL_EVIDENCE_REQUIRED
        for item in report.workshop_evidence
    )
    assert report.missing_evidence_count == len(report.workshop_evidence) == 14
    assert [(item["code"], item["title"]) for item in payload["software_evidence"]] == (
        SOFTWARE_REQUIREMENTS
    )
    assert [(item["code"], item["title"]) for item in payload["workshop_evidence"]] == (
        WORKSHOP_REQUIREMENTS
    )
    assert all(item.keys() == REQUIREMENT_KEYS for item in payload["software_evidence"])
    assert all(item.keys() == REQUIREMENT_KEYS for item in payload["workshop_evidence"])
    assert payload["physical_cutting_authorized"] is False


def test_missing_software_evidence_blocks_design_review_readiness() -> None:
    report = build_workshop_readiness_report(
        authoritative_cad=False,
        dfm_passed=True,
        operation_count=0,
        setup_count=0,
        validation_backplot=False,
        validation_program=False,
    )

    missing_codes = {
        item.code for item in report.software_evidence if item.status is ReadinessStatus.MISSING
    }
    assert report.design_review_ready is False
    assert missing_codes == {
        "AUTHORITATIVE_CAD",
        "SEMANTIC_OPERATIONS",
        "SETUP_SHEETS",
        "VALIDATION_BACKPLOT",
        "NON_CUTTING_PROGRAM",
    }
    assert report.missing_evidence_count == 19


@pytest.mark.parametrize(
    "field",
    (
        "authoritative_cad",
        "dfm_passed",
        "validation_backplot",
        "validation_program",
        "edge_band_selection_required",
        "material_grain_binding_required",
    ),
)
@pytest.mark.parametrize(
    ("value", "accepted"),
    ((True, True), (False, True), (1, False), (0.5, False), (-1, False)),
)
def test_builder_requires_exact_boolean_inputs(
    field: str,
    value: object,
    accepted: bool,
) -> None:
    if accepted:
        assert isinstance(report_from_input_overrides(**{field: value}), WorkshopReadinessReport)
    else:
        with pytest.raises(
            ReadinessValidationError,
            match=rf"^{field} must be a boolean$",
        ):
            report_from_input_overrides(**{field: value})


@pytest.mark.parametrize("field", ("operation_count", "setup_count"))
@pytest.mark.parametrize(
    ("value", "accepted"),
    ((1, True), (0, True), (True, False), (0.5, False), (-1, False)),
)
def test_builder_requires_nonnegative_exact_integer_counts(
    field: str,
    value: object,
    accepted: bool,
) -> None:
    if accepted:
        assert isinstance(report_from_input_overrides(**{field: value}), WorkshopReadinessReport)
    else:
        with pytest.raises(
            ReadinessValidationError,
            match=rf"^{field} must be a non-negative integer$",
        ):
            report_from_input_overrides(**{field: value})


def test_edge_band_system_is_an_explicit_external_release_requirement() -> None:
    report = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=True,
        operation_count=1,
        setup_count=1,
        validation_backplot=True,
        validation_program=True,
        edge_band_selection_required=True,
    )

    edge_band = report.workshop_evidence[-1]
    assert (edge_band.code, edge_band.title) == EDGE_BAND_REQUIREMENT
    assert edge_band.status is ReadinessStatus.EXTERNAL_EVIDENCE_REQUIRED
    assert "SKU/version" in edge_band.required_action
    assert "mechanical retention" in edge_band.required_action
    assert "prohibited" in edge_band.required_action
    assert "cut-size compensation" in edge_band.required_action
    assert report.edge_band_selection_required is True
    assert report.design_review_ready is True
    assert report.physical_cutting_authorized is False
    assert report.missing_evidence_count == 15


def test_checksum_bound_external_evidence_is_canonical_and_deterministic() -> None:
    canonical_digest = "a1" * 32
    external_evidence = [
        {
            "evidence_type": "wall_anchor",
            "catalog_id": "anchor-catalog",
            "catalog_version": "2026.08",
            "sha256": canonical_digest,
            "unrelated_server_metadata": "preserved outside readiness",
        }
    ]
    untouched = deepcopy(external_evidence)
    report = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=True,
        operation_count=1,
        setup_count=1,
        validation_backplot=True,
        validation_program=True,
        external_evidence=external_evidence,
    )

    wall_anchor = report.workshop_evidence[0]
    assert wall_anchor.status is ReadinessStatus.VERIFIED
    assert wall_anchor.evidence == (
        f"Server-bound anchor-catalog@2026.08 / sha256:{canonical_digest}"
    )
    assert report.missing_evidence_count == 13
    assert external_evidence == untouched
    assert normalize_workshop_readiness_report(report.as_dict()).as_dict() == report.as_dict()


def test_opaque_material_grain_upload_never_verifies_structured_axis_binding() -> None:
    digest = "b2" * 32
    report = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=True,
        operation_count=1,
        setup_count=1,
        validation_backplot=True,
        validation_program=True,
        external_evidence=(
            {
                "evidence_type": "material_grain",
                "catalog_id": "grain-note",
                "catalog_version": "2026.08",
                "sha256": digest,
            },
        ),
    )

    material_grain = next(
        item for item in report.workshop_evidence if item.code == "MATERIAL_GRAIN"
    )
    assert material_grain.status is ReadinessStatus.EXTERNAL_EVIDENCE_REQUIRED
    assert f"grain-note@2026.08 / sha256:{digest}" in material_grain.evidence
    assert "not a structured stock-grain axis binding" in material_grain.evidence
    assert material_grain.required_action == DFM_GRAIN_REQUIRED_ACTION
    assert report.missing_evidence_count == 14


def test_catalog_nondirectional_design_marks_grain_binding_not_applicable() -> None:
    report = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=True,
        operation_count=1,
        setup_count=1,
        validation_backplot=True,
        validation_program=True,
        material_grain_binding_required=False,
        external_evidence=(
            {
                "evidence_type": "material_grain",
                "catalog_id": "irrelevant-upload",
                "catalog_version": "1.0.0",
                "sha256": "c" * 64,
            },
        ),
    )

    material_grain = next(
        item for item in report.workshop_evidence if item.code == "MATERIAL_GRAIN"
    )
    assert material_grain.status is ReadinessStatus.VERIFIED
    assert "catalog-declared non-directional material" in material_grain.evidence
    assert material_grain.required_action == "None for this design."
    assert report.missing_evidence_count == 13


@pytest.mark.parametrize(
    "external_evidence",
    [
        ["not-a-mapping"],
        [
            {
                "evidence_type": "edge_band",
                "catalog_id": "edge",
                "catalog_version": "1",
                "sha256": "a" * 64,
            }
        ],
        [
            {
                "evidence_type": "wall_anchor",
                "catalog_id": "anchor",
                "catalog_version": "1",
                "sha256": "a" * 64,
            },
            {
                "evidence_type": "wall_anchor",
                "catalog_id": "anchor-duplicate",
                "catalog_version": "2",
                "sha256": "b" * 64,
            },
        ],
        [
            {
                "evidence_type": "hardware",
                "catalog_id": " ",
                "catalog_version": "1",
                "sha256": "a" * 64,
            }
        ],
        [
            {
                "evidence_type": "material_grain",
                "catalog_id": "batch",
                "catalog_version": "1",
                "sha256": "not-a-structural-sha256",
            }
        ],
        [
            {
                "evidence_type": " wall_anchor",
                "catalog_id": "anchor",
                "catalog_version": "1",
                "sha256": "a" * 64,
            }
        ],
        [
            {
                "evidence_type": "wall_anchor",
                "catalog_id": "anchor ",
                "catalog_version": "1",
                "sha256": "a" * 64,
            }
        ],
        [
            {
                "evidence_type": "wall_anchor",
                "catalog_id": "anchor",
                "catalog_version": " 1",
                "sha256": "a" * 64,
            }
        ],
        [
            {
                "evidence_type": "wall_anchor",
                "catalog_id": "anchor",
                "catalog_version": "1",
                "sha256": "A" * 64,
            }
        ],
        [
            {
                "evidence_type": "wall_anchor",
                "catalog_id": "anchor",
                "catalog_version": "1",
                "sha256": f"{'a' * 64} ",
            }
        ],
    ],
    ids=(
        "non-mapping",
        "unknown-type",
        "duplicate",
        "blank-catalog",
        "bad-digest",
        "evidence-type-whitespace",
        "catalog-id-whitespace",
        "catalog-version-whitespace",
        "uppercase-digest",
        "digest-whitespace",
    ),
)
def test_external_evidence_rejects_malformed_unknown_and_duplicate_records(
    external_evidence: list[Any],
) -> None:
    with pytest.raises(ReadinessValidationError):
        build_workshop_readiness_report(
            authoritative_cad=True,
            dfm_passed=True,
            operation_count=1,
            setup_count=1,
            validation_backplot=True,
            validation_program=True,
            external_evidence=external_evidence,
        )


def test_v2_normalization_is_canonical_and_does_not_mutate_input() -> None:
    payload = complete_report(edge_band=True)
    untouched = deepcopy(payload)

    normalized = normalize_workshop_readiness_report(payload)

    assert payload == untouched
    assert normalized.as_dict() == untouched
    assert isinstance(normalized.software_evidence, tuple)
    assert isinstance(normalized.workshop_evidence, tuple)


@pytest.mark.parametrize("edge_band", [False, True])
def test_complete_legacy_v1_normalizes_to_safe_scoped_v2(edge_band: bool) -> None:
    payload = legacy_report(edge_band=edge_band)
    untouched = deepcopy(payload)

    normalized = normalize_workshop_readiness_report(payload)
    canonical = normalized.as_dict()

    assert payload == untouched
    assert canonical["schema_version"] == WORKSHOP_READINESS_SCHEMA_VERSION
    assert canonical["release_scope"] == DESIGN_REVIEW_RELEASE_SCOPE
    assert canonical["machine_use"] == VALIDATION_ONLY_MACHINE_USE
    assert canonical["edge_band_selection_required"] is edge_band
    assert canonical["physical_cutting_authorized"] is False


def _add_unknown_key(payload: dict[str, Any]) -> None:
    payload["unexpected"] = "unsafe ambiguity"


def _remove_release_scope(payload: dict[str, Any]) -> None:
    del payload["release_scope"]


def _unsafe_release_scope(payload: dict[str, Any]) -> None:
    payload["release_scope"] = "physical_release"


def _unsafe_machine_use(payload: dict[str, Any]) -> None:
    payload["machine_use"] = "production"


def _non_boolean_edge_flag(payload: dict[str, Any]) -> None:
    payload["edge_band_selection_required"] = 0


def _edge_flag_array_mismatch(payload: dict[str, Any]) -> None:
    payload["edge_band_selection_required"] = True


def _authorize_cutting(payload: dict[str, Any]) -> None:
    payload["physical_cutting_authorized"] = True


def _boolean_missing_count(payload: dict[str, Any]) -> None:
    payload["missing_evidence_count"] = True


def _wrong_missing_count(payload: dict[str, Any]) -> None:
    payload["missing_evidence_count"] += 1


def _wrong_ready_flag(payload: dict[str, Any]) -> None:
    payload["design_review_ready"] = False


def _reorder_software(payload: dict[str, Any]) -> None:
    payload["software_evidence"][0], payload["software_evidence"][1] = (
        payload["software_evidence"][1],
        payload["software_evidence"][0],
    )


def _blank_title(payload: dict[str, Any]) -> None:
    payload["workshop_evidence"][0]["title"] = " "


def _software_external_status(payload: dict[str, Any]) -> None:
    payload["software_evidence"][0]["status"] = "EXTERNAL_EVIDENCE_REQUIRED"


def _workshop_missing_status(payload: dict[str, Any]) -> None:
    payload["workshop_evidence"][0]["status"] = "MISSING"


def _tuple_array(payload: dict[str, Any]) -> None:
    payload["software_evidence"] = tuple(payload["software_evidence"])


@pytest.mark.parametrize(
    "mutator",
    [
        _add_unknown_key,
        _remove_release_scope,
        _unsafe_release_scope,
        _unsafe_machine_use,
        _non_boolean_edge_flag,
        _edge_flag_array_mismatch,
        _authorize_cutting,
        _boolean_missing_count,
        _wrong_missing_count,
        _wrong_ready_flag,
        _reorder_software,
        _blank_title,
        _software_external_status,
        _workshop_missing_status,
        _tuple_array,
    ],
)
def test_v2_normalization_rejects_noncanonical_or_unsafe_payloads(
    mutator: Callable[[dict[str, Any]], None],
) -> None:
    payload = complete_report()
    mutator(payload)

    with pytest.raises(ReadinessValidationError):
        normalize_workshop_readiness_report(payload)


def test_legacy_v1_requires_complete_arrays_and_exact_old_top_level_keys() -> None:
    incomplete = legacy_report()
    incomplete["software_evidence"] = []
    with pytest.raises(ReadinessValidationError):
        normalize_workshop_readiness_report(incomplete)

    invented_scope = legacy_report()
    invented_scope["release_scope"] = DESIGN_REVIEW_RELEASE_SCOPE
    with pytest.raises(ReadinessValidationError):
        normalize_workshop_readiness_report(invented_scope)
