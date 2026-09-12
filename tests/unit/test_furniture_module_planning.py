from __future__ import annotations

import json
from copy import deepcopy
from fractions import Fraction
from pathlib import Path

import pytest
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_dimensions import assert_production_dimensions
from custombuild_domain.identity import content_hash
from custombuild_manufacturing.furniture_module_planning import (
    MAX_MODULE_COUNT,
    MAX_MODULE_PARTS,
    FurnitureModuleGrid,
    plan_furniture_modules,
)
from custombuild_manufacturing.furniture_profiles import preview_furniture
from pydantic import ValidationError

from tests.unit.test_furniture_families import workspace


def grid(**changes):
    return FurnitureModuleGrid.model_validate(
        {"columns": 4, "rows": 2, "gap_um": 0, "shelf_count_per_row": [2, 2], **changes}
    )


def customer(**intent_changes):
    document = json.loads(Path("examples/furniture/bookcase-4340x2540x280.json").read_text())
    document["design"]["intent"].update(intent_changes)
    document["manufacturing"] = {
        "machine_profile_id": "custombuild-router-1325-linuxcnc",
        "stock_width_um": 2_440_000,
        "stock_height_um": 1_220_000,
        "stock_grain_axis": "x",
        "edge_margin_um": 5_000,
    }
    return FurnitureWorkspace.model_validate(document)


def test_grid_conserves_outer_dimensions_and_places_complete_separate_carcasses():
    source = customer(width_um=4_340_007, height_um=2_540_003)
    original = source.model_dump(mode="json")
    selected = grid(gap_um=7_003)
    plan = plan_furniture_modules(source, selected)
    assert plan["state"] == "available"
    assert source.model_dump(mode="json") == original
    widths, heights = plan["layout"]["column_widths_um"], plan["layout"]["row_heights_um"]
    assert sum(widths) + 3 * selected.gap_um == 4_340_007
    assert sum(heights) + selected.gap_um == 2_540_003
    assert max(widths) - min(widths) <= 1
    assert max(heights) - min(heights) <= 1
    assert len(plan["modules"]) == 8
    all_part_ids = set()
    for module in plan["modules"]:
        row, column = module["row"], module["column"]
        assert module["placement"] == {
            "x_um": sum(widths[:column]) + column * selected.gap_um,
            "y_um": 0,
            "z_um": sum(heights[:row]) + row * selected.gap_um,
        }
        draft = FurnitureWorkspace.model_validate(module["workspace"])
        assert draft.design.intent.width_um == widths[column]
        assert draft.design.intent.height_um == heights[row]
        assert draft.design.intent.depth_um == source.design.intent.depth_um
        parts = module["preview"]["design"]["parts"]
        keys = {part["semantic_key"] for part in parts}
        assert {"left-side", "right-side", "top", "bottom"} <= keys
        assert len(parts) == 7  # Four outer panels, two shelves, one back.
        ids = {part["part_id"] for part in parts}
        assert not ids & all_part_ids
        all_part_ids.update(ids)
        assert module["weight_g"] == sum(part["weight_g"] for part in parts)
        assert module["preview"] == preview_furniture(draft)
    assert plan["screening"]["part_count"] == len(all_part_ids)
    assert plan["screening"]["total_weight_g"] == sum(m["weight_g"] for m in plan["modules"])
    assert not plan["layout"]["joint_or_spacer_geometry_created"]


def test_per_row_payload_uses_exact_width_fraction_and_conservative_rounding_with_gaps():
    source = customer(width_um=4_340_003, shelf_load_n=201)
    plan = plan_furniture_modules(source, grid(gap_um=5_001))
    widths = plan["layout"]["column_widths_um"]
    expected = [Fraction(201 * width, sum(widths)).__ceil__() for width in widths]
    loads = plan["load_distribution"]
    assert loads["module_shelf_row_payload_n_by_column"] == expected
    assert sum(expected) >= 201
    assert sum(expected) < 201 + len(widths)
    assert loads["planned_total_shelf_payload_n"] >= loads["source_total_shelf_payload_n"]
    for module in plan["modules"]:
        draft = FurnitureWorkspace.model_validate(module["workspace"])
        assert draft.design.intent.resolved_shelf_load_n == expected[module["column"]]
        assert module["shelf_row_payload_n"] == draft.design.intent.resolved_shelf_load_n
        assert module["total_shelf_payload_n"] == draft.design.intent.resolved_shelf_load_n * 2
    assert not loads["stacking_loads_included_in_shelf_screening"]


def test_per_metre_density_is_preserved_with_exact_independent_ceiling_per_module():
    source = customer(
        width_um=4_340_003,
        shelf_load_basis="per_metre",
        shelf_load_per_metre_n=500,
        shelf_load_n=123,
    )
    plan = plan_furniture_modules(source, grid())
    assert plan["state"] == "available"
    assert plan["load_distribution"]["shelf_load_per_metre_n"] == 500
    expected = [
        (width * 500 + 999_999) // 1_000_000 for width in plan["layout"]["column_widths_um"]
    ]
    assert plan["load_distribution"]["module_shelf_row_payload_n_by_column"] == expected
    for module in plan["modules"]:
        intent = FurnitureWorkspace.model_validate(module["workspace"]).design.intent
        assert intent.shelf_load_basis == "per_metre"
        assert intent.shelf_load_per_metre_n == 500
        assert intent.shelf_load_n == 123  # Inactive field must not replace the effective load.
        assert intent.resolved_shelf_load_n == expected[module["column"]]
    assert sum(expected) >= source.design.intent.resolved_shelf_load_n


def test_gap_cannot_reduce_total_row_payload_when_density_must_remain_unchanged():
    source = customer(shelf_load_basis="per_metre", shelf_load_per_metre_n=500)
    plan = plan_furniture_modules(source, grid(gap_um=10_000))
    assert plan["code"] == "LOAD_REDUCTION_UNSUPPORTED"
    assert plan["modules"] == []
    assert not plan["can_export_drafts"]
    assert plan["source"]["workspace"] == source.model_dump(mode="json")


def test_zero_payload_retains_self_weight_in_real_rules():
    source = customer(shelf_load_n=0)
    plan = plan_furniture_modules(source, grid())
    assert plan["state"] == "available"
    assert plan["load_distribution"]["planned_total_shelf_payload_n"] == 0
    for module in plan["modules"]:
        rules = {rule["rule_id"]: rule for rule in module["preview"]["rules"]["evaluations"]}
        assert rules["CB-DEFLECTION-001"]["values"]["calculated"] > 0
        assert module["weight_g"] > 0


def test_real_stock_check_fits_modules_whose_source_contains_oversize_parts():
    source = customer()
    before = preview_furniture(source)
    assert not before["manufacturing"]["geometry_compatible"]
    assert any(issue["code"] == "PART_EXCEEDS_STOCK" for issue in before["manufacturing"]["issues"])
    plan = plan_furniture_modules(source, grid())
    assert plan["screening"]["all_stock_fit"]
    for module in plan["modules"]:
        stock = module["preview"]["manufacturing"]
        assert stock["geometry_compatible"]
        assert stock["issues"] == []
        stock_parts = [part for group in stock["stock_groups"] for part in group["parts"]]
        parts = module["preview"]["design"]["parts"]
        assert {part["part_id"] for part in parts} == {part["part_id"] for part in stock_parts}
        assert all(part["fits_stock"] for part in stock_parts)
    # One row leaves 2540 mm side panels intact; fitting top/bottom does not hide them.
    tall = plan_furniture_modules(source, grid(rows=1, shelf_count_per_row=[4]))
    assert tall["state"] == "available"
    assert tall["can_export_drafts"]
    assert not tall["screening"]["all_stock_fit"]
    assert tall["screening"]["stock_issue_count"] > 0


def test_no_machine_selection_never_claims_stock_fit_and_unknown_grain_stays_unknown():
    source = workspace("shelving", width_um=1_800_000)
    plan = plan_furniture_modules(source, grid(columns=2, rows=1, shelf_count_per_row=[4]))
    assert plan["screening"]["all_stock_fit"] is None
    document = customer().model_dump(mode="json")
    document["manufacturing"]["stock_grain_axis"] = None
    unknown = plan_furniture_modules(FurnitureWorkspace.model_validate(document), grid())
    assert unknown["screening"]["all_stock_fit"] is False
    assert any(
        requirement["code"] == "GRAIN_AXIS_REQUIRED"
        for module in unknown["modules"]
        for requirement in module["requirements"]
    )


def test_hashes_bind_entire_source_and_choices_and_each_draft_has_its_own_identity():
    source = customer()
    first = plan_furniture_modules(source, grid())
    assert first == plan_furniture_modules(source, grid())
    assert first["source"]["workspace_hash"] == content_hash(source.model_dump(mode="json"))
    assert first["source_design_hash"] == preview_furniture(source)["design"]["design_hash"]
    module_hashes = set()
    for module in first["modules"]:
        assert module["workspace_hash"] == content_hash(module["workspace"])
        assert module["workspace"]["design"]["design_id"] == module["module_id"]
        assert module["workspace"]["design"]["revision"] == 1
        assert module["workspace"]["design"]["design_id"] != source.design.design_id
        assert module["design_hash"] != first["source_design_hash"]
        assert module["source_design_hash"] == first["source_design_hash"]
        assert module["plan_hash"] == first["plan_hash"]
        module_hashes.add(module["design_hash"])
    assert len(module_hashes) == len(first["modules"])
    variants = []
    for field, value in [("revision", 2), ("design_id", "another-customer")]:
        changed = source.model_dump(mode="json")
        changed["design"][field] = value
        variants.append(changed)
    changed = source.model_dump(mode="json")
    changed["design"]["installation"]["trim_profile"]["width_um"] += 1
    variants.append(changed)
    changed = source.model_dump(mode="json")
    changed["design"]["material"]["batch_id"] = "new-batch"
    variants.append(changed)
    changed = source.model_dump(mode="json")
    changed["manufacturing"]["edge_margin_um"] += 1
    variants.append(changed)
    for changed in variants:
        plan = plan_furniture_modules(FurnitureWorkspace.model_validate(changed), grid())
        assert plan["plan_hash"] != first["plan_hash"]
        assert plan["modules"][0]["module_id"] != first["modules"][0]["module_id"]
    for choices in [
        grid(gap_um=1),
        grid(shelf_count_per_row=[3, 2]),
        grid(divider_count_per_module=1),
    ]:
        assert plan_furniture_modules(source, choices)["plan_hash"] != first["plan_hash"]


def test_unresolved_customer_installation_survives_every_export_and_blocks_production():
    source = customer()
    plan = plan_furniture_modules(source, grid())
    assert plan["state"] == "available"
    assert plan["review_status"] == "requires_review"
    assert plan["can_export_drafts"]
    assert plan["source"]["installation"] == source.design.installation.model_dump(mode="json")
    for module in plan["modules"]:
        draft = FurnitureWorkspace.model_validate(module["workspace"])
        assert draft.design.installation == source.design.installation
        assert draft.design.material == source.design.material
        assert draft.design.back_material == source.design.back_material
        assert draft.manufacturing == source.manufacturing
        assert module["review_status"] == "requires_review"
        codes = {requirement["code"] for requirement in module["requirements"]}
        assert "SOURCE_INSTALLATION_ALLOWANCES_MISSING" in codes
        assert "SOURCE_TRIM_USE_REQUIRED" in codes
        with pytest.raises(ValueError, match="list"):
            assert_production_dimensions(draft.design)
        assert not module["production_qualified"]
        assert not module["physical_cutting_authorized"]
        assert not module["preview"]["production_qualified"]
    assert not plan["production_qualified"]
    assert not plan["physical_cutting_authorized"]
    assert not plan["can_apply"]


def test_resolved_source_installation_is_not_falsely_rebound_to_smaller_modules():
    document = customer().model_dump(mode="json")
    installation = document["design"]["installation"]
    installation.update(width_includes_trim=False)
    installation.pop("trim_profile")
    for side in ("left", "right", "top", "bottom", "front", "rear"):
        installation[f"{side}_allowance_um"] = 0
    source = FurnitureWorkspace.model_validate(document)
    assert_production_dimensions(source.design)
    plan = plan_furniture_modules(source, grid())
    assert plan["can_export_drafts"]
    for module in plan["modules"]:
        draft = FurnitureWorkspace.model_validate(module["workspace"])
        assert draft.design.installation == source.design.installation
        assert any(
            requirement["code"] == "INSTALLATION_DIMENSIONS_DIFFER"
            and requirement["status"] == "blocked"
            for requirement in module["requirements"]
        )
        with pytest.raises(ValueError, match="Stommåtten"):
            assert_production_dimensions(draft.design)


def test_unknown_whole_assembly_requirements_remain_even_if_local_numeric_rules_pass():
    source = workspace("shelving", width_um=1_200_000, height_um=1_200_000, shelf_load_n=50)
    plan = plan_furniture_modules(source, grid(columns=2, rows=2, gap_um=5_000))
    assert plan["state"] == "available"
    assert plan["screening"]["all_shelf_numeric_pass"]
    codes = {requirement["code"] for requirement in plan["requirements"]}
    assert {
        "MODULE_CONNECTIONS_REQUIRED",
        "WALL_ANCHORAGE_REQUIRED",
        "STACKING_LOAD_PATH_REQUIRED",
        "VERTICAL_GAP_SUPPORT_REQUIRED",
        "INSTALLATION_MEASUREMENTS_REQUIRED",
        "MATERIAL_AND_JOINT_QUALIFICATION_REQUIRED",
        "WORKSHOP_QUALIFICATION_REQUIRED",
    } <= codes
    assert plan["screening"]["stacking_load_path"] == "unknown"
    assert not plan["screening"]["assembly_qualified"]
    assert not plan["production_qualified"]


@pytest.mark.parametrize(
    "source,selected,code",
    [
        (workspace("table"), grid(), "UNSUPPORTED_FAMILY"),
        (workspace("chest_of_drawers"), grid(), "UNSUPPORTED_FAMILY"),
        (customer(bay_width_ratios_ppm=[400_000, 600_000]), grid(), "CUSTOM_BAY_LAYOUT"),
        (
            customer(shelf_height_ratios_ppm=[180_000, 380_000, 580_000, 800_000]),
            grid(),
            "CUSTOM_SHELF_LAYOUT",
        ),
        (customer(plinth_height_um=90_000), grid(), "PLINTH_TRANSFORMATION_UNSUPPORTED"),
        (customer(), grid(shelf_count_per_row=[1, 2]), "SHELF_CAPACITY_REDUCTION_UNSUPPORTED"),
        (
            customer(),
            grid(columns=8, shelf_count_per_row=[40, 40], divider_count_per_module=16),
            "MODULE_COMPLEXITY_LIMIT",
        ),
        (
            workspace("shelving", width_um=250_000, divider_count=0),
            grid(columns=4, rows=1, gap_um=100_000, shelf_count_per_row=[4]),
            "MODULE_GAPS_CONSUME_SPACE",
        ),
        (
            customer(),
            grid(columns=8, rows=2, shelf_count_per_row=[40, 40]),
            "MODULE_COMPLEXITY_LIMIT",
        ),
        (
            workspace("shelving", width_um=300_000, divider_count=0),
            grid(columns=2, rows=1, shelf_count_per_row=[4]),
            "MODULE_GEOMETRY_UNSUPPORTED",
        ),
    ],
)
def test_unsupported_transformations_never_return_partial_drafts(source, selected, code):
    before = deepcopy(source.model_dump(mode="json"))
    plan = plan_furniture_modules(source, selected)
    assert plan["state"] == "unavailable"
    assert plan["review_status"] == "blocked"
    assert plan["code"] == code
    assert plan["modules"] == []
    assert plan["layout"] is None
    assert not plan["can_export_drafts"]
    assert plan["source"]["workspace"] == before
    assert source.model_dump(mode="json") == before
    assert not plan["production_qualified"]


@pytest.mark.parametrize(
    "changes",
    [
        {"columns": True},
        {"columns": 0},
        {"rows": 1.5},
        {"rows": 4, "columns": 8, "shelf_count_per_row": [1, 1, 1, 1]},
        {"rows": 1, "columns": 1, "shelf_count_per_row": [4]},
        {"shelf_count_per_row": [4]},
        {"shelf_count_per_row": [True, 4]},
        {"gap_um": -1},
        {"gap_um": 100_001},
        {"gap_um": False},
        {"divider_count_per_module": 17},
        {"hidden_splice": True},
    ],
)
def test_grid_validates_bounds_and_does_not_accept_implicit_or_approximate_inputs(changes):
    with pytest.raises(ValidationError):
        grid(**changes)
    assert MAX_MODULE_COUNT == 16
    assert MAX_MODULE_PARTS == 512


def test_gap_and_shelf_distribution_must_be_explicit():
    with pytest.raises(ValidationError):
        FurnitureModuleGrid.model_validate({"columns": 4, "rows": 2})


def test_planner_revalidates_bypassed_models():
    forged = grid().model_copy(update={"columns": 10_000})
    with pytest.raises(ValidationError):
        plan_furniture_modules(customer(), forged)


def test_complexity_limit_is_checked_before_compiling_any_candidate(monkeypatch):
    calls = []

    def record_preview(selected):
        calls.append(selected)
        return preview_furniture(selected)

    monkeypatch.setattr(
        "custombuild_manufacturing.furniture_module_planning.preview_furniture", record_preview
    )
    source = customer()
    plan = plan_furniture_modules(source, grid(columns=8, shelf_count_per_row=[40, 40]))
    assert plan["code"] == "MODULE_COMPLEXITY_LIMIT"
    assert calls == [source]


def test_single_row_preserves_selected_plinth_and_does_not_invent_stacking():
    source = customer(plinth_height_um=90_000)
    plan = plan_furniture_modules(source, grid(rows=1, shelf_count_per_row=[4]))
    assert plan["state"] == "available"
    assert plan["screening"]["stacking_load_path"] == "not_applicable"
    assert all(
        m["workspace"]["design"]["intent"]["plinth_height_um"] == 90_000 for m in plan["modules"]
    )
    assert "STACKING_LOAD_PATH_REQUIRED" not in {r["code"] for r in plan["requirements"]}


def test_late_geometry_failure_discards_already_computed_modules(monkeypatch):
    # The bottom four 300 mm carcasses are valid, but the upper row cannot fit
    # forty shelves. No caller may receive the successful subset as a plan.
    source = customer(height_um=600_001)
    original = source.model_dump(mode="json")
    calls = []

    def record_preview(selected):
        calls.append(selected)
        return preview_furniture(selected)

    monkeypatch.setattr(
        "custombuild_manufacturing.furniture_module_planning.preview_furniture", record_preview
    )
    plan = plan_furniture_modules(source, grid(shelf_count_per_row=[0, 40]))
    assert len(calls) == 6  # Source, four valid modules and the rejected module.
    assert plan["state"] == "unavailable"
    assert plan["code"] == "MODULE_GEOMETRY_UNSUPPORTED"
    assert plan["modules"] == []
    assert plan["layout"] is None
    assert plan["screening"] is None
    assert plan["load_distribution"] is None
    assert not plan["can_export_drafts"]
    assert source.model_dump(mode="json") == original


def test_maximum_module_count_remains_available_with_valid_geometry():
    source = customer()
    selected = grid(columns=4, rows=4, gap_um=1, shelf_count_per_row=[1, 1, 1, 1])
    plan = plan_furniture_modules(source, selected)
    assert plan["state"] == "available"
    assert len(plan["modules"]) == MAX_MODULE_COUNT
    assert plan["screening"]["part_count"] == 96
    assert plan["screening"]["part_count"] <= MAX_MODULE_PARTS
    assert sum(plan["layout"]["column_widths_um"]) + 3 == source.design.intent.width_um
    assert sum(plan["layout"]["row_heights_um"]) + 3 == source.design.intent.height_um
    assert len({module["workspace_hash"] for module in plan["modules"]}) == MAX_MODULE_COUNT


def test_vertical_only_plan_preserves_each_row_load_without_inventing_horizontal_gaps():
    source = workspace("shelving", width_um=900_003, height_um=1_200_003, shelf_load_n=501)
    plan = plan_furniture_modules(
        source, grid(columns=1, rows=2, gap_um=1_001, shelf_count_per_row=[1, 3])
    )
    assert plan["state"] == "available"
    assert plan["layout"]["column_widths_um"] == [900_003]
    assert plan["layout"]["row_heights_um"] == [599_501, 599_501]
    assert plan["load_distribution"]["module_shelf_row_payload_n_by_column"] == [501]
    assert plan["load_distribution"]["planned_total_shelf_payload_n"] == 2_004
    assert [module["total_shelf_payload_n"] for module in plan["modules"]] == [501, 1_503]
    assert all(module["placement"]["x_um"] == 0 for module in plan["modules"])


@pytest.mark.parametrize("back_panel", ["none", "surface_mounted"])
def test_nondefault_component_and_per_material_stock_selections_are_preserved(back_panel):
    source = customer(back_panel=back_panel, shelf_mount="adjustable")
    document = source.model_dump(mode="json")
    document["manufacturing"]["material_stocks"] = [
        {
            "material_id": "birch-plywood-6",
            "material_version": "screening-2026.1",
            "measured_thickness_um": 6_000,
            "stock_width_um": 1_000_000,
            "stock_height_um": 1_000_000,
            "stock_grain_axis": "y",
        }
    ]
    source = FurnitureWorkspace.model_validate(document)
    plan = plan_furniture_modules(source, grid())
    assert plan["state"] == "available"
    for module in plan["modules"]:
        draft = FurnitureWorkspace.model_validate(module["workspace"])
        assert draft.design.intent.back_panel == source.design.intent.back_panel
        assert draft.design.intent.shelf_mount == source.design.intent.shelf_mount
        assert draft.manufacturing == source.manufacturing
        assert module["preview"] == preview_furniture(draft)
    # An unused override remains an explicit issue, rather than being dropped.
    # A present back must be checked against the undersized override itself.
    assert not plan["screening"]["all_stock_fit"]
    expected = "MATERIAL_STOCK_NOT_USED" if back_panel == "none" else "PART_EXCEEDS_STOCK"
    for module in plan["modules"]:
        assert any(
            issue["code"] == expected and issue["material_id"] == "birch-plywood-6"
            for issue in module["preview"]["manufacturing"]["issues"]
        )


@pytest.mark.parametrize(
    "back_panel,counts", [("inset_groove", [5, 5, 5, 6]), ("surface_mounted", [6, 6, 6, 6])]
)
def test_part_limit_accepts_exactly_512_parts_for_each_back_construction(back_panel, counts):
    source = customer(back_panel=back_panel)
    selected = grid(columns=4, rows=4, shelf_count_per_row=counts, divider_count_per_module=3)
    plan = plan_furniture_modules(source, selected)
    assert plan["state"] == "available"
    assert plan["screening"]["part_count"] == MAX_MODULE_PARTS
    assert sum(len(module["preview"]["design"]["parts"]) for module in plan["modules"]) == 512
    # One extra shelf row creates sixteen more panels, before any machining.
    too_many = [counts[0] + 1, *counts[1:]]
    rejected = plan_furniture_modules(
        source, grid(columns=4, rows=4, shelf_count_per_row=too_many, divider_count_per_module=3)
    )
    assert rejected["code"] == "MODULE_COMPLEXITY_LIMIT"
    assert rejected["modules"] == []


def test_planner_revalidates_bypassed_nested_workspace_models():
    source = customer()
    forged_intent = source.design.intent.model_copy(update={"shelf_load_n": True})
    forged_design = source.design.model_copy(update={"intent": forged_intent})
    forged_workspace = source.model_copy(update={"design": forged_design})
    with pytest.raises(ValidationError):
        plan_furniture_modules(forged_workspace, grid())
