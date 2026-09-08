from __future__ import annotations

import pytest
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import build_furniture
from custombuild_manufacturing.adapters import adapt_design_result
from custombuild_manufacturing.furniture_handoff import furniture_workshop_handoff
from custombuild_manufacturing.furniture_profiles import preview_furniture

from tests.unit.test_furniture_families import workspace


@pytest.mark.parametrize("family", ["shelving", "table", "chest_of_drawers"])
def test_every_blank_is_in_one_material_group_with_the_exact_cad_dimensions(family):
    result = build_furniture(workspace(family).design)
    handoff = furniture_workshop_handoff(result)
    rows = {p["part_id"]: p for group in handoff["stock_requirements"] for p in group["parts"]}
    assert len(rows) == len(result.parts)
    for part in adapt_design_result(result).parts:
        assert rows[part.part_id]["raw_width_um"] == (part.raw_width_um or part.width_um)
        assert rows[part.part_id]["raw_height_um"] == (part.raw_height_um or part.height_um)
    for group in handoff["stock_requirements"]:
        assert group["raw_area_um2"] == sum(
            p["raw_width_um"] * p["raw_height_um"] for p in group["parts"]
        )
    assert handoff["design_hash"] == result.design_hash
    assert not handoff["physical_cutting_authorized"]


def test_the_large_customer_design_reports_stock_and_grain_constraints_per_part():
    value = workspace(
        "shelving",
        width_um=4_340_000,
        height_um=2_540_000,
        depth_um=280_000,
        divider_count=4,
        shelf_count=8,
    ).model_dump(mode="json")
    value["manufacturing"] = {
        "machine_profile_id": "custombuild-router-1325-linuxcnc",
        "stock_width_um": 1_220_000,
        "stock_height_um": 2_440_000,
        "stock_grain_axis": "y",
        "edge_margin_um": 10_000,
    }
    preview = preview_furniture(FurnitureWorkspace.model_validate(value))
    issues = preview["manufacturing"]["issues"]
    assert any(p["code"] == "PART_EXCEEDS_STOCK" and "2420" in p["message"] for p in issues)
    requirement = next(
        g
        for g in preview["workshop_handoff"]["stock_requirements"]
        if g["material_id"] == "birch-plywood"
    )
    assert requirement["minimum_along_grain_um"] > 4_000_000
    assert preview["manufacturing"]["geometry_compatible"] is False
    assert all(
        "skarvas inte automatiskt" in p["message"]
        for p in issues
        if p["code"] == "PART_EXCEEDS_STOCK"
    )


def test_margins_can_make_a_nominally_fitting_board_unusable():
    value = workspace().model_dump(mode="json")
    value["manufacturing"] = {
        "machine_profile_id": "custombuild-router-1325-linuxcnc",
        "stock_width_um": 1_000_000,
        "stock_height_um": 800_000,
        "stock_grain_axis": "x",
    }
    assert preview_furniture(FurnitureWorkspace.model_validate(value))["manufacturing"][
        "geometry_compatible"
    ]
    value["manufacturing"]["edge_margin_um"] = 1
    issues = preview_furniture(FurnitureWorkspace.model_validate(value))["manufacturing"]["issues"]
    assert any(p["code"] == "PART_EXCEEDS_STOCK" and "table-top" in p["message"] for p in issues)
    value["manufacturing"].update(stock_width_um=100_000, edge_margin_um=100_000)
    issues = preview_furniture(FurnitureWorkspace.model_validate(value))["manufacturing"]["issues"]
    assert any(p["code"] == "STOCK_MARGIN_CONSUMES_SHEET" for p in issues)


def test_format_error_text_preserves_micrometres_even_for_long_parts():
    value = workspace(width_um=4_340_007).model_dump(mode="json")
    value["manufacturing"] = {
        "machine_profile_id": "custombuild-router-5125-linuxcnc",
        "stock_width_um": 4_340_006,
        "stock_height_um": 1_220_000,
        "stock_grain_axis": "x",
    }
    preview = preview_furniture(FurnitureWorkspace.model_validate(value))
    assert any(
        "4340.007" in issue["message"] and "4340.006" in issue["message"]
        for issue in preview["manufacturing"]["issues"]
    )
