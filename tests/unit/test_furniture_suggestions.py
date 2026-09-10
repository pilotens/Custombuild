from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import build_furniture
from custombuild_manufacturing.furniture_profiles import preview_furniture
from custombuild_manufacturing.furniture_suggestions import suggest_shelf_bays
from custombuild_rules.engine import MAX_AUTO_VERTICAL_DIVIDERS, RuleEngine, _threshold_status
from custombuild_rules.models import RuleStatus

from tests.unit.test_furniture_families import workspace


def _customer_workspace() -> FurnitureWorkspace:
    return FurnitureWorkspace.model_validate(
        json.loads(Path("examples/furniture/bookcase-4340x2540x280.json").read_text())
    )


def test_customer_proposal_has_canonical_previews_and_preserves_all_other_inputs():
    document = _customer_workspace().model_dump(mode="json")
    document["design"]["intent"].update(
        width_um=4_340_007,
        shelf_load_basis="per_metre",
        shelf_load_per_metre_n=500,
        shelf_height_ratios_ppm=[180_000, 380_000, 580_000, 800_000],
        plinth_height_um=90_000,
    )
    document["design"]["material"]["batch_id"] = "customer-panel-batch"
    document["design"]["back_material"]["batch_id"] = "customer-back-batch"
    document["manufacturing"] = {
        "machine_profile_id": "custombuild-router-1325-linuxcnc",
        "stock_width_um": 2_440_000,
        "stock_height_um": 1_220_000,
        "stock_grain_axis": "x",
        "edge_margin_um": 5_000,
    }
    original = FurnitureWorkspace.model_validate(document)
    original_document = original.model_dump(mode="json")
    response = suggest_shelf_bays(original)
    assert response["state"] == "available"
    assert original.model_dump(mode="json") == original_document
    proposed = FurnitureWorkspace.model_validate(response["proposed"]["workspace"])
    expected_document = deepcopy(original_document)
    expected_document["design"]["intent"]["divider_count"] = proposed.design.intent.divider_count
    assert proposed.model_dump(mode="json") == expected_document
    assert response["current"] == preview_furniture(original)
    assert response["proposed"] == preview_furniture(proposed)
    assert response["changed_fields"] == [
        {
            "field": "design.intent.divider_count",
            "before": original.design.intent.divider_count,
            "after": proposed.design.intent.divider_count,
        }
    ]
    assert (
        original.design.intent.resolved_shelf_load_n == proposed.design.intent.resolved_shelf_load_n
    )
    assert (
        response["current"]["design"]["design_hash"]
        != response["proposed"]["design"]["design_hash"]
    )
    before_parts = response["current"]["design"]["parts"]
    after_parts = response["proposed"]["design"]["parts"]
    assert {p["placement"]["z_um"] for p in before_parts if p["role"] == "shelf"} == {
        p["placement"]["z_um"] for p in after_parts if p["role"] == "shelf"
    }
    assert not response["production_qualified"]
    assert not response["physical_cutting_authorized"]
    assert response["remaining_issues"]["manufacturing"]
    assert any(
        issue["code"] == "PART_EXCEEDS_STOCK"
        for issue in response["remaining_issues"]["manufacturing"]
    )
    for key in ("top", "bottom"):
        before = next(p for p in before_parts if p["semantic_key"] == key)
        after = next(p for p in after_parts if p["semantic_key"] == key)
        assert before["finished_size"] == after["finished_size"]
        assert before["raw_size"] == after["raw_size"]


def test_proposal_is_deterministic_and_uses_first_all_numeric_pass_candidate():
    original = _customer_workspace()
    response = suggest_shelf_bays(original)
    assert response == suggest_shelf_bays(original)
    proposed = FurnitureWorkspace.model_validate(response["proposed"]["workspace"])
    assert proposed.design.intent.divider_count == 4
    assert response["search"]["evaluated_candidate_count"] == 3
    target_ids = {check["rule_id"] for check in response["screening_checks"]["proposed"]}
    for divider_count in range(original.design.intent.divider_count + 1, 5):
        document = original.model_dump(mode="json")
        document["design"]["intent"]["divider_count"] = divider_count
        candidate = build_furniture(FurnitureWorkspace.model_validate(document).design)
        assert candidate.shelving_result is not None
        report = RuleEngine().evaluate(candidate.shelving_result)
        checks = [rule for rule in report.evaluations if rule.rule_id in target_ids]
        assert len(checks) == 3
        all_pass = all(
            rule.allowed_value > 0
            and _threshold_status(rule.calculated_value, rule.allowed_value) == RuleStatus.PASS
            for rule in checks
        )
        assert all_pass == (divider_count == 4)
    remaining = {rule["rule_id"]: rule for rule in response["remaining_issues"]["rules"]}
    assert remaining["CB-JOINT-001"]["status"] == "WARNING"
    assert "CB-FURNITURE-MATERIAL-001" in remaining
    assert response["proposed"]["rules"]["overall_status"] == "BLOCK"


def test_bay_widths_match_generated_joint_support_spans_with_micrometre_remainders():
    original = workspace("shelving", width_um=4_340_003, depth_um=280_000)
    response = suggest_shelf_bays(original)
    proposed = FurnitureWorkspace.model_validate(response["proposed"]["workspace"])
    result = build_furniture(proposed.design)
    shelves = sorted(
        (p for p in result.parts if p.semantic_key.startswith("shelf-r0-")),
        key=lambda part: part.placement.x_um,
    )
    actual_spans = []
    for shelf in shelves:
        support_x = [
            joint.mating_origin.x_um
            for joint in result.joints
            if any(member.part_id == shelf.part_id for member in joint.members)
        ]
        actual_spans.append(max(support_x) - min(support_x))
    assert response["proposed_bays"]["clear_widths_um"] == actual_spans
    assert max(actual_spans) - min(actual_spans) == 1


def test_numeric_warning_is_not_accepted_as_pass_and_retention_warning_remains():
    original = workspace(
        "shelving", width_um=4_340_000, divider_count=3, shelf_load_n=150, depth_um=280_000
    )
    response = suggest_shelf_bays(original)
    assert response["screening_checks"]["current"][0]["numeric_status"] == "WARNING"
    assert response["state"] == "available"
    assert response["proposed_bays"]["divider_count"] == 4
    joint = next(
        check
        for check in response["screening_checks"]["proposed"]
        if check["rule_id"] == "CB-JOINT-001"
    )
    assert joint["numeric_status"] == "PASS"
    assert joint["status"] == "WARNING"


def test_zero_payload_still_checks_shelf_self_weight():
    response = suggest_shelf_bays(
        workspace("shelving", width_um=4_340_000, depth_um=280_000, shelf_load_n=0)
    )
    assert response["state"] == "available"
    assert response["screening_checks"]["current"][0]["calculated"] > 0
    assert response["proposed"]["workspace"]["design"]["intent"]["shelf_load_n"] == 0


@pytest.mark.parametrize(
    "selected,code",
    [
        (workspace("table"), "UNSUPPORTED_FAMILY"),
        (workspace("chest_of_drawers"), "UNSUPPORTED_FAMILY"),
        (workspace("shelving", shelf_count=0), "NO_SHELVES"),
        (workspace("shelving", bay_width_ratios_ppm=[400_000, 600_000]), "CUSTOM_BAY_LAYOUT"),
        (workspace("shelving", shelf_mount="adjustable"), "NUMERIC_RULES_UNAVAILABLE"),
    ],
)
def test_unsupported_or_evidence_limited_cases_do_not_change_or_search(selected, code):
    original = selected.model_dump(mode="json")
    response = suggest_shelf_bays(selected)
    assert response["state"] == "unavailable"
    assert response["code"] == code
    assert not response["can_apply"]
    assert response["proposed"] is None
    assert response["proposed_bays"] is None
    assert response["changed_fields"] == []
    assert response["search"]["attempted_candidate_count"] == 0
    assert selected.model_dump(mode="json") == original


def test_existing_numeric_pass_is_a_no_op_without_claiming_whole_furniture_pass():
    response = suggest_shelf_bays(workspace("shelving"))
    assert response["state"] == "already_pass"
    assert response["proposed"] is None
    assert response["changed_fields"] == []
    assert response["remaining_issues"]["rules"]
    assert not response["can_apply"]
    assert not response["production_qualified"]


@pytest.mark.parametrize("width_um,evaluated", [(6_000_000, 16), (400_000, 5)])
def test_search_is_bounded_and_infeasible_geometry_never_returns_a_partial_solution(
    width_um, evaluated
):
    document = workspace(
        "shelving",
        width_um=width_um,
        depth_um=100_000,
        divider_count=0,
        shelf_load_n=5_000,
        back_panel="none",
    ).model_dump(mode="json")
    document["design"]["material"]["material_id"] = "mdf"
    response = suggest_shelf_bays(FurnitureWorkspace.model_validate(document))
    assert response["state"] == "unavailable"
    assert response["code"] == "SEARCH_EXHAUSTED"
    assert response["search"] == {
        "max_divider_count": MAX_AUTO_VERTICAL_DIVIDERS,
        "attempted_candidate_count": MAX_AUTO_VERTICAL_DIVIDERS,
        "evaluated_candidate_count": evaluated,
    }
    assert response["proposed"] is None
    assert response["changed_fields"] == []


def test_no_candidate_exists_beyond_the_current_domain_divider_limit():
    document = workspace(
        "shelving",
        width_um=6_000_000,
        depth_um=100_000,
        divider_count=MAX_AUTO_VERTICAL_DIVIDERS,
        shelf_load_n=5_000,
        back_panel="none",
    ).model_dump(mode="json")
    document["design"]["material"]["material_id"] = "mdf"
    response = suggest_shelf_bays(FurnitureWorkspace.model_validate(document))
    assert response["code"] == "SEARCH_EXHAUSTED"
    assert response["search"]["attempted_candidate_count"] == 0


@pytest.mark.parametrize("missing", ["CB-DEFLECTION-001", "CB-BENDING-001", "CB-JOINT-001"])
def test_an_incomplete_rule_report_cannot_claim_passing_screening(monkeypatch, missing):
    def incomplete_preview(selected):
        result = preview_furniture(selected)
        result["rules"]["evaluations"] = [
            rule for rule in result["rules"]["evaluations"] if rule["rule_id"] != missing
        ]
        return result

    monkeypatch.setattr(
        "custombuild_manufacturing.furniture_suggestions.preview_furniture", incomplete_preview
    )
    response = suggest_shelf_bays(workspace("shelving"))
    assert response["code"] == "NUMERIC_RULES_UNAVAILABLE"
    assert response["state"] == "unavailable"
    assert response["search"]["attempted_candidate_count"] == 0


def test_invalid_original_geometry_is_reported_without_generating_a_replacement():
    with pytest.raises(ValueError):
        suggest_shelf_bays(workspace("shelving", width_um=10_000))
