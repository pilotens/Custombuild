from __future__ import annotations

import copy
from collections.abc import Callable

import pytest
from custombuild_manufacturing import MANIFEST_CONTEXT_HASH_FIELDS
from custombuild_manufacturing.readiness import build_workshop_readiness_report
from custombuild_manufacturing.review_status import (
    BLOCKED_CAM_REQUIRED_ACTIONS as MANUFACTURING_BLOCKED_CAM_REQUIRED_ACTIONS,
)

from scripts.live_acceptance import (
    BLOCKED_CAM_REQUIRED_ACTIONS,
    CONTEXT_HASH_FIELDS,
    DADO_RETENTION_EVIDENCE_MISSING,
    DESIGN_REVIEW_PACKAGE_STATUS_PATH,
    GENERATION_PLAN_PATH,
    GENERATION_PLAN_ROLE,
    PRODUCTION_MANIFEST_SCHEMA_VERSION,
    REQUIRED_PACKAGE_PATHS,
    REQUIRED_REVIEW_PACKAGE_PATHS,
    STOCK_SELECTION_PATH,
    STOCK_SELECTION_ROLE,
    AcceptanceFailure,
    HttpResult,
    _safe_zip_path,
    blocked_cam_artifact_violation,
    blocked_cam_evidence_kind_is_forbidden,
    manifest_context,
    verify_blocked_cam_endpoint_rejection,
    verify_design_review_dfm_report,
    verify_design_review_package_status,
    verify_explicit_two_sided_registration,
    verify_generation_context_hash,
    verify_generation_plan,
    verify_generation_result_safety,
    verify_status_readiness_alignment,
    verify_workshop_readiness,
)


def test_live_acceptance_hashes_the_exact_manifest_context_contract() -> None:
    assert CONTEXT_HASH_FIELDS == MANIFEST_CONTEXT_HASH_FIELDS
    assert PRODUCTION_MANIFEST_SCHEMA_VERSION == "custombuild.production-manifest.v4"


def test_live_acceptance_uses_the_manufacturing_blocker_actions() -> None:
    assert BLOCKED_CAM_REQUIRED_ACTIONS == MANUFACTURING_BLOCKED_CAM_REQUIRED_ACTIONS


@pytest.mark.parametrize(
    ("path", "role", "media_type"),
    (
        ("cam/rogue.ngc", "WORKER_NOTE", "application/octet-stream"),
        ("CAM/rogue.NGC", "WORKER_NOTE", "application/octet-stream"),
        ("nesting/x.svg", "WORKER_NOTE", "image/svg+xml"),
        ("machine-validation/x", "WORKER_NOTE", "application/octet-stream"),
        ("materials/stock-inventory.csv", "WORKER_NOTE", "text/csv"),
        ("placement/layout.json", "WORKER_NOTE", "application/json"),
        ("stock/profile.json", "WORKER_NOTE", "application/json"),
        ("review/x.NGC", "WORKER_NOTE", "application/octet-stream"),
        ("review/backplot.svg", "VALIDATION_BACKPLOT", "image/svg+xml"),
        ("review/note.json", "STOCK_PROFILE", "application/json"),
        ("toolpaths/finish.nc", "GCODE", "text/x-gcode"),
        ("review/operations.json", "WORKER_NOTE", "application/json"),
        ("review/tool-list.csv", "TOOLING_PLAN", "text/csv"),
        ("setups/sheet.svg", "SETUP_PLAN", "image/svg+xml"),
        (
            "quality/operation-measurements.json",
            "OPERATION_QA_PLAN",
            "application/json",
        ),
    ),
)
def test_live_acceptance_rejects_generic_blocked_cam_inventory(
    path: str,
    role: str,
    media_type: str,
) -> None:
    assert blocked_cam_artifact_violation(path, role, media_type) is True


def test_live_acceptance_allows_machine_independent_worker_document() -> None:
    assert (
        blocked_cam_artifact_violation(
            "assembly/assembly-manual.pdf",
            "ASSEMBLY_REVIEW_MANUAL",
            "application/pdf",
        )
        is False
    )


def test_live_acceptance_allows_only_the_canonical_stock_selection_snapshot() -> None:
    assert (
        blocked_cam_artifact_violation(
            STOCK_SELECTION_PATH,
            STOCK_SELECTION_ROLE,
            "application/json",
        )
        is False
    )
    assert blocked_cam_evidence_kind_is_forbidden("stock_selection") is False
    assert (
        blocked_cam_artifact_violation(
            STOCK_SELECTION_PATH,
            "WORKER_NOTE",
            "application/json",
        )
        is True
    )


def test_live_acceptance_allows_only_the_canonical_generation_plan() -> None:
    assert (
        blocked_cam_artifact_violation(
            GENERATION_PLAN_PATH,
            GENERATION_PLAN_ROLE,
            "application/json",
        )
        is False
    )
    assert blocked_cam_evidence_kind_is_forbidden("generation_plan") is False
    assert (
        blocked_cam_artifact_violation(
            GENERATION_PLAN_PATH,
            "WORKER_NOTE",
            "application/json",
        )
        is True
    )


@pytest.mark.parametrize(
    "path",
    (
        "../cam/rogue.tap",
        "review/../cam/rogue.tap",
        "review\\cam\\rogue.tap",
        "/cam/rogue.tap",
        "C:/cam/rogue.tap",
        "file:stream",
        "review//note.txt",
        "review/./note.txt",
        "review/note\x00.txt",
    ),
)
def test_live_acceptance_rejects_unsafe_or_ambiguous_zip_path(path: str) -> None:
    with pytest.raises(AcceptanceFailure, match="ZIP path"):
        _safe_zip_path(path)


@pytest.mark.parametrize(
    "kind",
    (
        "operations",
        "validation_backplot",
        "setup_sheet_001",
        "cam_rogue",
        "nesting_rogue",
        "machine_validation_001",
        "rogue.NGC",
        "tool_list",
        "stock_purchase_schedule",
        "stock_profile",
        "placement_map",
        "placements_rogue",
        "quality_measurement_plan",
        "gcode",
        "toolpath",
        "machine_program",
        "operations_plan",
        "setup_plan",
        "tooling_plan",
    ),
)
def test_live_acceptance_rejects_generic_blocked_cam_evidence_kind(kind: str) -> None:
    assert blocked_cam_evidence_kind_is_forbidden(kind) is True


def _readiness_payload(*, edge_required: bool = False) -> dict[str, object]:
    return build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=True,
        operation_count=3,
        setup_count=1,
        validation_backplot=True,
        validation_program=True,
        edge_band_selection_required=edge_required,
    ).as_dict()


def _generation_plan_payload() -> dict[str, object]:
    return {
        "schema_version": "custombuild.generation-plan.v1",
        "pipeline_version": "production-pipeline-1.10.0",
        "nesting_algorithm": "deterministic-bottom-left-v1",
        "operations_schema_version": "custombuild.operations.v2",
        "operations_engine_version": "semantic-operations-1.2.0",
        "machine_profile": {
            "id": "custombuild-router-1325-linuxcnc",
            "version": "1.0.0",
            "fingerprint": "a" * 64,
        },
        "postprocessor": {"id": "linuxcnc-validation", "version": "1.0.0"},
        "stock_profiles_fingerprint": "a" * 64,
        "validation_program_requested": True,
        "two_sided_registrations": [
            {
                "stock_id": "stock-a",
                "sheets": [
                    {
                        "sheet_index": 0,
                        "method_id": "registration-pins",
                        "points": [
                            {"x_um": 10_000, "y_um": 20_000},
                            {"x_um": 30_000, "y_um": 40_000},
                        ],
                    }
                ],
            }
        ],
    }


def test_live_acceptance_verifies_the_generation_plan_semantics() -> None:
    payload = _generation_plan_payload()
    before = copy.deepcopy(payload)

    assert (
        verify_generation_plan(
            payload,
            label="plan",
            generated_validation_program=True,
            expected_stock_profiles_fingerprint="a" * 64,
        )
        == payload
    )
    assert payload == before

    payload["validation_program_requested"] = False
    with pytest.raises(AcceptanceFailure, match="generated status"):
        verify_generation_plan(
            payload,
            label="plan",
            generated_validation_program=True,
            expected_stock_profiles_fingerprint="a" * 64,
        )

    payload = _generation_plan_payload()
    payload["stock_profiles_fingerprint"] = "b" * 64
    with pytest.raises(AcceptanceFailure, match="stock selection"):
        verify_generation_plan(
            payload,
            label="plan",
            generated_validation_program=True,
            expected_stock_profiles_fingerprint="a" * 64,
        )


def _blocked_readiness_payload(*, dfm_blocked: bool = False) -> dict[str, object]:
    return build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=not dfm_blocked,
        operation_count=0,
        setup_count=0,
        validation_backplot=False,
        validation_program=False,
        edge_band_selection_required=False,
    ).as_dict()


def _package_status(*, blocked: bool = False) -> dict[str, object]:
    return {
        "schema_version": "custombuild.design-review-package-status.v1",
        "package_status": "READY_FOR_DESIGN_REVIEW",
        "cam_status": "BLOCKED" if blocked else "VALIDATION_GENERATED",
        "blocker_codes": ["TWO_SIDED_REGISTRATION_MISSING"] if blocked else [],
        "operations_included": not blocked,
        "setup_sheets_included": not blocked,
        "nesting_included": not blocked,
        "validation_backplot_included": not blocked,
        "validation_program_included": not blocked,
        "physical_cutting_authorized": False,
        "required_action": (
            "Bind an externally specified two-sided registration and fixture plan; "
            "do not infer WCS, pins, fixtures or registration coordinates."
            if blocked
            else "None for design review; physical workshop evidence remains required."
        ),
    }


def _stock_package_status() -> dict[str, object]:
    payload = _package_status(blocked=True)
    payload["blocker_codes"] = ["STOCK_PROFILE_MISSING"]
    payload["required_action"] = (
        "Select and server-bind an exact stock profile for every part material, version, "
        "thickness, blank size and quantity; do not infer sheet size, stock identity or "
        "machine capacity."
    )
    return payload


def _grain_package_status() -> dict[str, object]:
    payload = _package_status(blocked=True)
    payload["blocker_codes"] = ["DFM-GRAIN-001"]
    payload["required_action"] = (
        "Bind an exact, structured X or Y stock-grain axis for every directional material "
        "stock profile; opaque evidence or acknowledgement cannot resolve this blocker."
    )
    return payload


def _retention_package_status() -> dict[str, object]:
    payload = _package_status(blocked=True)
    payload["blocker_codes"] = [DADO_RETENTION_EVIDENCE_MISSING]
    payload["required_action"] = BLOCKED_CAM_REQUIRED_ACTIONS[
        DADO_RETENTION_EVIDENCE_MISSING
    ]
    return payload


def test_live_acceptance_requires_and_verifies_the_v2_readiness_artifact() -> None:
    assert "validation/workshop-readiness.json" in REQUIRED_PACKAGE_PATHS
    assert DESIGN_REVIEW_PACKAGE_STATUS_PATH in REQUIRED_PACKAGE_PATHS
    assert DESIGN_REVIEW_PACKAGE_STATUS_PATH in REQUIRED_REVIEW_PACKAGE_PATHS
    assert STOCK_SELECTION_PATH in REQUIRED_PACKAGE_PATHS
    assert STOCK_SELECTION_PATH in REQUIRED_REVIEW_PACKAGE_PATHS
    assert GENERATION_PLAN_PATH in REQUIRED_PACKAGE_PATHS
    assert GENERATION_PLAN_PATH in REQUIRED_REVIEW_PACKAGE_PATHS
    for edge_required in (False, True):
        payload = _readiness_payload(edge_required=edge_required)
        before = copy.deepcopy(payload)

        assert verify_workshop_readiness(payload, label="readiness") == payload
        assert payload == before


@pytest.mark.parametrize(
    ("location", "value"),
    [
        (("schema_version",), "custombuild.workshop-readiness.v1"),
        (("release_scope",), "production"),
        (("machine_use",), "cutting"),
        (("physical_cutting_authorized",), True),
        (("missing_evidence_count",), True),
        (("design_review_ready",), False),
        (("software_evidence", 0, "code"), "UNKNOWN"),
        (("software_evidence", 0, "status"), "EXTERNAL_EVIDENCE_REQUIRED"),
        (("workshop_evidence", 0, "status"), "MISSING"),
    ],
)
def test_live_acceptance_rejects_noncanonical_readiness(
    location: tuple[str | int, ...],
    value: object,
) -> None:
    payload = _readiness_payload()
    target: object = payload
    for key in location[:-1]:
        if isinstance(target, list):
            assert isinstance(key, int)
            target = target[key]
        else:
            assert isinstance(target, dict)
            target = target[str(key)]
    final_key = location[-1]
    if isinstance(target, list):
        assert isinstance(final_key, int)
        target[final_key] = value
    else:
        assert isinstance(target, dict)
        target[str(final_key)] = value

    with pytest.raises(AcceptanceFailure):
        verify_workshop_readiness(payload, label="readiness")


def test_live_acceptance_rejects_edge_flag_without_edge_requirement() -> None:
    payload = _readiness_payload()
    payload["edge_band_selection_required"] = True

    with pytest.raises(AcceptanceFailure):
        verify_workshop_readiness(payload, label="readiness")


@pytest.mark.parametrize(
    "payload",
    (
        _package_status(),
        _package_status(blocked=True),
        _stock_package_status(),
        _retention_package_status(),
    ),
)
def test_live_acceptance_verifies_canonical_package_statuses(
    payload: dict[str, object],
) -> None:
    before = copy.deepcopy(payload)

    assert verify_design_review_package_status(payload, label="package status") == payload
    assert payload == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("physical_cutting_authorized", True),
        ("operations_included", True),
        ("blocker_codes", []),
        ("blocker_codes", ["UNSUPPORTED_BLOCKER"]),
        ("required_action", "  "),
    ],
)
def test_live_acceptance_rejects_unsafe_blocked_package_status(
    field: str,
    value: object,
) -> None:
    payload = _package_status(blocked=True)
    payload[field] = value

    with pytest.raises(AcceptanceFailure):
        verify_design_review_package_status(payload, label="package status")


@pytest.mark.parametrize("dfm_status", [None, 1, "UNKNOWN", "BLOCK"])
def test_live_acceptance_rejects_nonpassing_dfm_status(dfm_status: object) -> None:
    job_result = {
        "authoritative_geometry": True,
        "dfm_status": dfm_status,
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
        "design_review_package_status": _package_status(),
        "workshop_readiness": _readiness_payload(),
    }

    with pytest.raises(AcceptanceFailure):
        verify_generation_result_safety(job_result)


@pytest.mark.parametrize("dfm_status", ["PASS", "WARNING"])
def test_live_acceptance_accepts_canonical_nonblocking_dfm_status(dfm_status: str) -> None:
    readiness = _readiness_payload()

    assert (
        verify_generation_result_safety(
            {
                "authoritative_geometry": True,
                "dfm_status": dfm_status,
                "machine_program_mode": "VALIDATION_DRY_RUN",
                "production_machine_program": False,
                "design_review_package_status": _package_status(),
                "workshop_readiness": readiness,
            }
        )
        == readiness
    )


def test_live_acceptance_accepts_review_package_with_truthful_cam_block() -> None:
    readiness = _blocked_readiness_payload()

    assert (
        verify_generation_result_safety(
            {
                "authoritative_geometry": True,
                "dfm_status": "PASS",
                "machine_program_mode": "CAM_BLOCKED",
                "production_machine_program": False,
                "nesting_utilization_ppm": None,
                "used_sheet_count": 0,
                "nesting_layouts": [],
                "design_review_package_status": _package_status(blocked=True),
                "workshop_readiness": readiness,
            }
        )
        == readiness
    )


def test_live_acceptance_accepts_truthful_stockless_review_package() -> None:
    readiness = _blocked_readiness_payload(dfm_blocked=True)

    assert (
        verify_generation_result_safety(
            {
                "authoritative_geometry": True,
                "dfm_status": "BLOCK",
                "machine_program_mode": "CAM_BLOCKED",
                "production_machine_program": False,
                "nesting_utilization_ppm": None,
                "used_sheet_count": 0,
                "nesting_layouts": [],
                "design_review_package_status": _stock_package_status(),
                "workshop_readiness": readiness,
            }
        )
        == readiness
    )


def test_live_acceptance_accepts_truthful_grain_blocked_review_package() -> None:
    readiness = _blocked_readiness_payload(dfm_blocked=True)

    assert (
        verify_generation_result_safety(
            {
                "authoritative_geometry": True,
                "dfm_status": "BLOCK",
                "machine_program_mode": "CAM_BLOCKED",
                "production_machine_program": False,
                "nesting_utilization_ppm": None,
                "used_sheet_count": 0,
                "nesting_layouts": [],
                "design_review_package_status": _grain_package_status(),
                "workshop_readiness": readiness,
            }
        )
        == readiness
    )


@pytest.mark.parametrize("dfm_status", ("PASS", "WARNING"))
def test_live_acceptance_accepts_truthful_dado_retention_block(
    dfm_status: str,
) -> None:
    readiness = _blocked_readiness_payload()

    assert (
        verify_generation_result_safety(
            {
                "authoritative_geometry": True,
                "dfm_status": dfm_status,
                "machine_program_mode": "CAM_BLOCKED",
                "production_machine_program": False,
                "nesting_utilization_ppm": None,
                "used_sheet_count": 0,
                "nesting_layouts": [],
                "design_review_package_status": _retention_package_status(),
                "workshop_readiness": readiness,
            }
        )
        == readiness
    )


def test_live_acceptance_rejects_grain_blocked_readiness_claiming_bound_grain() -> None:
    readiness = _blocked_readiness_payload(dfm_blocked=True)
    grain = next(
        item for item in readiness["workshop_evidence"] if item["code"] == "MATERIAL_GRAIN"
    )
    grain["status"] = "VERIFIED"
    readiness["missing_evidence_count"] -= 1
    result = {
        "authoritative_geometry": True,
        "dfm_status": "BLOCK",
        "machine_program_mode": "CAM_BLOCKED",
        "production_machine_program": False,
        "nesting_utilization_ppm": None,
        "used_sheet_count": 0,
        "nesting_layouts": [],
        "design_review_package_status": _grain_package_status(),
        "workshop_readiness": readiness,
    }

    with pytest.raises(AcceptanceFailure, match="MATERIAL_GRAIN unresolved"):
        verify_generation_result_safety(result)

    with pytest.raises(AcceptanceFailure, match="MATERIAL_GRAIN readiness disagree"):
        verify_status_readiness_alignment(
            _grain_package_status(),
            readiness,
            label="grain-blocked review contract",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("authoritative_geometry", False),
        ("authoritative_geometry", None),
        ("nesting_utilization_ppm", 0),
        ("used_sheet_count", False),
        ("used_sheet_count", 0.0),
        ("used_sheet_count", 1),
        ("nesting_layouts", [{}]),
        ("nesting_layouts", ()),
    ),
)
def test_live_acceptance_rejects_blocked_result_nesting_or_geometry_tamper(
    field: str,
    value: object,
) -> None:
    result: dict[str, object] = {
        "authoritative_geometry": True,
        "dfm_status": "BLOCK",
        "machine_program_mode": "CAM_BLOCKED",
        "production_machine_program": False,
        "nesting_utilization_ppm": None,
        "used_sheet_count": 0,
        "nesting_layouts": [],
        "design_review_package_status": _stock_package_status(),
        "workshop_readiness": _blocked_readiness_payload(dfm_blocked=True),
    }
    result[field] = value

    with pytest.raises(AcceptanceFailure):
        verify_generation_result_safety(result)


def test_live_acceptance_rejects_missing_blocked_nesting_utilization_claim() -> None:
    result: dict[str, object] = {
        "authoritative_geometry": True,
        "dfm_status": "BLOCK",
        "machine_program_mode": "CAM_BLOCKED",
        "production_machine_program": False,
        "used_sheet_count": 0,
        "nesting_layouts": [],
        "design_review_package_status": _stock_package_status(),
        "workshop_readiness": _blocked_readiness_payload(dfm_blocked=True),
    }

    with pytest.raises(AcceptanceFailure):
        verify_generation_result_safety(result)


def test_live_acceptance_binds_package_status_to_readiness_matrix() -> None:
    verify_status_readiness_alignment(
        _package_status(blocked=True),
        _blocked_readiness_payload(),
        label="review contract",
    )
    with pytest.raises(AcceptanceFailure, match="status and readiness disagree"):
        verify_status_readiness_alignment(
            _package_status(blocked=True),
            _readiness_payload(),
            label="review contract",
        )
    verify_status_readiness_alignment(
        _stock_package_status(),
        _blocked_readiness_payload(dfm_blocked=True),
        label="stockless review contract",
    )
    verify_status_readiness_alignment(
        _grain_package_status(),
        _blocked_readiness_payload(dfm_blocked=True),
        label="grain-blocked review contract",
    )
    verify_status_readiness_alignment(
        _retention_package_status(),
        _blocked_readiness_payload(),
        label="DADO-retention review contract",
    )
    with pytest.raises(AcceptanceFailure, match="status and readiness disagree"):
        verify_status_readiness_alignment(
            _stock_package_status(),
            _blocked_readiness_payload(),
            label="stockless review contract",
        )


def _dfm_issue(code: str, severity: str) -> dict[str, object]:
    return {
        "code": code,
        "severity": severity,
        "message": "A canonical manufacturing decision is unresolved.",
        "part_id": "side-left" if code == "STOCK_PROFILE_MISSING" else None,
        "feature_id": None,
        "setup_id": None,
        "inputs": {"binding_status": "MISSING_INFORMATION"},
        "suggestion": "Bind exact structured input.",
    }


def _dfm_report(
    *,
    stock_blocked: bool = False,
    grain_severity: str | None = None,
) -> dict[str, object]:
    issues = []
    if stock_blocked:
        issues.append(_dfm_issue("STOCK_PROFILE_MISSING", "BLOCK"))
    if grain_severity is not None:
        issues.append(_dfm_issue("DFM-GRAIN-001", grain_severity))
    return {
        "engine_version": "dfm-1.3.0",
        "issues": issues,
    }


def test_live_acceptance_binds_stock_status_to_raw_dfm_report() -> None:
    payload = _dfm_report(stock_blocked=True, grain_severity="WARNING")

    assert (
        verify_design_review_dfm_report(
            payload,
            package_status=_stock_package_status(),
            expected_status="BLOCK",
            label="DFM report",
        )
        == payload
    )


def test_live_acceptance_binds_grain_status_to_raw_dfm_report() -> None:
    payload = _dfm_report(grain_severity="BLOCK")

    assert (
        verify_design_review_dfm_report(
            payload,
            package_status=_grain_package_status(),
            expected_status="BLOCK",
            label="DFM report",
        )
        == payload
    )


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    ((_dfm_report(), "PASS"), (_dfm_report(grain_severity="WARNING"), "WARNING")),
)
def test_live_acceptance_keeps_dado_retention_separate_from_dfm(
    payload: dict[str, object],
    expected_status: str,
) -> None:
    assert (
        verify_design_review_dfm_report(
            payload,
            package_status=_retention_package_status(),
            expected_status=expected_status,
            label="DFM report",
        )
        == payload
    )


def test_live_acceptance_requires_exact_dado_cam_and_release_rejection() -> None:
    canonical = HttpResult(
        409,
        "http://api.test/review",
        {"content-type": "application/json"},
        b'{"detail":{"code":"DADO_RETENTION_EVIDENCE_MISSING"}}',
    )
    verify_blocked_cam_endpoint_rejection(
        canonical,
        [DADO_RETENTION_EVIDENCE_MISSING],
        label="CAM approval",
    )

    generic = HttpResult(
        409,
        "http://api.test/review",
        {"content-type": "application/json"},
        b'{"detail":"Production evidence is blocked"}',
    )
    with pytest.raises(AcceptanceFailure, match="detail"):
        verify_blocked_cam_endpoint_rejection(
            generic,
            [DADO_RETENTION_EVIDENCE_MISSING],
            label="release",
        )


@pytest.mark.parametrize(
    ("payload", "package_status", "expected_status"),
    (
        (_dfm_report(), _stock_package_status(), "PASS"),
        (_dfm_report(stock_blocked=True), _package_status(blocked=True), "BLOCK"),
        (_dfm_report(stock_blocked=True), _stock_package_status(), "PASS"),
        (_dfm_report(grain_severity="WARNING"), _grain_package_status(), "WARNING"),
        (_dfm_report(grain_severity="BLOCK"), _stock_package_status(), "BLOCK"),
        (_dfm_report(stock_blocked=True), _retention_package_status(), "BLOCK"),
    ),
)
def test_live_acceptance_rejects_dfm_status_or_blocker_drift(
    payload: dict[str, object],
    package_status: dict[str, object],
    expected_status: str,
) -> None:
    with pytest.raises(AcceptanceFailure):
        verify_design_review_dfm_report(
            payload,
            package_status=package_status,
            expected_status=expected_status,
            label="DFM report",
        )


@pytest.mark.parametrize(
    ("blocked", "machine_mode", "readiness_factory"),
    [
        (True, "VALIDATION_DRY_RUN", _blocked_readiness_payload),
        (False, "CAM_BLOCKED", _readiness_payload),
        (True, "CAM_BLOCKED", _readiness_payload),
        (False, "VALIDATION_DRY_RUN", _blocked_readiness_payload),
    ],
)
def test_live_acceptance_rejects_crossed_cam_status_and_readiness_claims(
    blocked: bool,
    machine_mode: str,
    readiness_factory: Callable[[], dict[str, object]],
) -> None:
    with pytest.raises(AcceptanceFailure):
        verify_generation_result_safety(
            {
                "authoritative_geometry": True,
                "dfm_status": "PASS",
                "machine_program_mode": machine_mode,
                "production_machine_program": False,
                "design_review_package_status": _package_status(blocked=blocked),
                "workshop_readiness": readiness_factory(),
            }
        )


def _registered_operations() -> dict[str, object]:
    common = {
        "stock_id": "sheet-stock",
        "sheet_index": 0,
        "fixture": "EXTERNAL_FIXTURE_PLAN_REQUIRED; DECLARED_KEEP_OUT_ZONES_ONLY",
        "probe_method": (
            "DECLARED_COORDINATE_REGISTRATION;METHOD=fixture-registration-v1;"
            "STOCK_XY_UM=20000,20000|900000,20000;"
            "EXTERNAL_SETUP_VERIFICATION_REQUIRED"
        ),
    }
    return {
        "setups": [
            {**common, "side": "B"},
            {**common, "side": "A"},
        ]
    }


def test_live_acceptance_full_cam_requires_explicit_two_sided_registration() -> None:
    verify_explicit_two_sided_registration(_registered_operations())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("probe_method", "EXTERNAL_COORDINATE_REGISTRATION_REQUIRED"),
        (
            "probe_method",
            "DECLARED_COORDINATE_REGISTRATION;METHOD=fixture-registration-v1;"
            "STOCK_XY_UM=20000,20000|20000,20000;"
            "EXTERNAL_SETUP_VERIFICATION_REQUIRED",
        ),
        ("fixture", "VACUUM_FIXTURE_ASSUMED"),
    ],
)
def test_live_acceptance_rejects_unbound_two_sided_registration(
    field: str,
    value: str,
) -> None:
    operations = _registered_operations()
    setups = operations["setups"]
    assert isinstance(setups, list)
    assert isinstance(setups[0], dict)
    setups[0][field] = value

    with pytest.raises(AcceptanceFailure):
        verify_explicit_two_sided_registration(operations)


def test_live_acceptance_rejects_missing_manifest_context_field() -> None:
    manifest = {field: None for field in CONTEXT_HASH_FIELDS}
    manifest.pop("domain_template_version")

    with pytest.raises(AcceptanceFailure):
        manifest_context(manifest)


def test_live_acceptance_binds_job_and_result_generation_context() -> None:
    context_hash = "a" * 64
    assert (
        verify_generation_context_hash(
            {"production_context_hash": context_hash},
            {"generation_context_hash": context_hash},
        )
        == context_hash
    )

    with pytest.raises(AcceptanceFailure):
        verify_generation_context_hash(
            {"production_context_hash": context_hash},
            {"generation_context_hash": "b" * 64},
        )
