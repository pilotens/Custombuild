from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.identity import content_hash
from custombuild_manufacturing.furniture_profiles import preview_furniture
from custombuild_manufacturing.furniture_trial import furniture_trial_markdown

from tests.unit.test_furniture_families import workspace
from tests.unit.test_furniture_stock_planning import selected


def trial(document):
    preview = preview_furniture(FurnitureWorkspace.model_validate(document))
    report = preview["trial_readiness"]
    assert report == preview["workshop_handoff"]["trial_readiness"]
    assert report["design_hash"] == preview["design"]["design_hash"]
    assert report["workspace_sha256"] == content_hash(preview["workspace"])
    assert report["report_sha256"] == content_hash(
        {key: value for key, value in report.items() if key != "report_sha256"}
    )
    assert report["scope"] == "design_review_preparation"
    assert report["production_qualified"] is False
    assert report["physical_cutting_authorized"] is False
    assert len({check["code"] for check in report["checks"]}) == 10
    assert sum(report["counts"].values()) == 10
    assert report["blocker_count"] == report["counts"]["blocked"]
    assert report["evidence_required_count"] == report["counts"]["requires_evidence"]
    return report, {check["code"]: check for check in report["checks"]}


def test_customer_trial_prioritises_unresolved_dimensions_then_stock_and_construction():
    document = FurnitureWorkspace.model_validate_json(
        Path("examples/furniture/bookcase-4340x2540x280.json").read_text()
    ).model_dump(mode="json")
    document["manufacturing"] = {
        **selected()["manufacturing"],
        "stock_width_um": 1_220_000,
        "stock_height_um": 2_440_000,
    }
    report, checks = trial(document)
    assert report["state"] == "requires_design_change"
    assert checks["DIMENSION_CHAIN"]["state"] == "blocked"
    assert "TRIM_USE_REQUIRED" in checks["DIMENSION_CHAIN"]["evidence"]
    assert checks["STOCK_AND_WORK_AREA"]["state"] == "blocked"
    assert "PART_EXCEEDS_STOCK" in checks["STOCK_AND_WORK_AREA"]["evidence"]
    assert (
        "Fler hyllfack delar inte topp, botten eller sidor"
        in checks["STOCK_AND_WORK_AREA"]["action"]
    )
    assert checks["CONSTRUCTION_SCREENING"]["state"] == "blocked"
    assert report["next_action"] == checks["DIMENSION_CHAIN"]["action"]


@pytest.mark.parametrize("family", ["table", "chest_of_drawers"])
def test_layout_families_cannot_skip_missing_production_support(family):
    report, checks = trial(workspace(family).model_dump(mode="json"))
    assert checks["FAMILY_SCOPE"]["state"] == "blocked"
    assert report["next_action"] == checks["FAMILY_SCOPE"]["action"]
    assert checks["ADHESIVE_FREE_RETENTION"]["evidence"]


def test_fitting_stock_and_a_typed_batch_never_qualify_a_physical_trial():
    document = selected()
    document["design"]["material"]["batch_id"] = "customer-entered-batch"
    document["design"]["back_material"]["batch_id"] = "customer-entered-back-batch"
    report, checks = trial(document)
    assert report["state"] == "requires_workshop_evidence"
    assert checks["FAMILY_SCOPE"]["state"] == "checked"
    assert checks["STOCK_AND_WORK_AREA"]["state"] == "checked"
    for code in (
        "ACTUAL_MATERIAL",
        "ADHESIVE_FREE_RETENTION",
        "ASSEMBLY_AND_INSTALLATION",
        "AGREED_TOLERANCES",
        "WORKSHOP_CAM",
        "PHYSICAL_TRIAL",
    ):
        assert checks[code]["state"] == "requires_evidence"
    assert len(checks["ACTUAL_MATERIAL"]["evidence"]) == 2
    _, table_checks = trial(workspace("table").model_dump(mode="json"))
    assert len(table_checks["ACTUAL_MATERIAL"]["evidence"]) == 1


@pytest.mark.parametrize("change", ["revision", "material", "stock", "installation"])
def test_revision_material_stock_and_installation_changes_invalidate_the_report(change):
    document = selected()
    before, _ = trial(document)
    changed = deepcopy(document)
    if change == "revision":
        changed["design"]["revision"] += 1
    elif change == "material":
        changed["design"]["material"]["measured_thickness_um"] = 17_600
    elif change == "stock":
        changed["manufacturing"]["stock_height_um"] += 1
    else:
        intent = changed["design"]["intent"]
        changed["design"]["installation"] = {
            **{key: intent[key] for key in ("width_um", "height_um", "depth_um")},
            **{
                f"{side}_allowance_um": 0
                for side in ("left", "right", "top", "bottom", "front", "rear")
            },
        }
    after, _ = trial(changed)
    assert before["workspace_sha256"] != after["workspace_sha256"]
    assert before["report_sha256"] != after["report_sha256"]
    assert after["revision"] == changed["design"]["revision"]


def test_unknown_stock_or_installation_are_visible_and_markdown_uses_same_facts():
    document = selected()
    document["manufacturing"] = None
    report, checks = trial(document)
    assert checks["STOCK_AND_WORK_AREA"]["state"] == "requires_evidence"
    assert checks["DIMENSION_CHAIN"]["state"] == "requires_evidence"
    markdown = furniture_trial_markdown(report).decode("utf-8")
    for key in ("design_hash", "workspace_sha256", "report_sha256", "next_action"):
        assert report[key] in markdown
    for check in report["checks"]:
        assert check["title"] in markdown
        assert check["detail"] in markdown
        assert check["action"] in markdown
    assert "Inte godkännande för fysisk skärning" in markdown
