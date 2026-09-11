from __future__ import annotations

from copy import deepcopy

import pytest
from custombuild_domain.furniture import FurnitureWorkspace, ManufacturingSelection, ProfileChange
from custombuild_manufacturing.furniture_profiles import (
    compare_furniture_profiles,
    preview_furniture,
)
from pydantic import ValidationError

from tests.unit.test_furniture_families import workspace


def selected():
    value = workspace("shelving", width_um=600_000, height_um=800_000, depth_um=300_000)
    document = value.model_dump(mode="json")
    document["manufacturing"] = {
        "machine_profile_id": "custombuild-router-1325-linuxcnc",
        "stock_width_um": 310_000,
        "stock_height_um": 810_000,
        "stock_grain_axis": "y",
        "edge_margin_um": 5_000,
    }
    return document


def back_stock(**changes):
    return {
        "material_id": "birch-plywood-6",
        "material_version": "screening-2026.1",
        "measured_thickness_um": 6_000,
        "stock_width_um": 786_000,
        "stock_height_um": 294_950,
        "stock_grain_axis": "x",
        "edge_margin_um": 5_000,
        **changes,
    }


def preview(document):
    return preview_furniture(FurnitureWorkspace.model_validate(document))


def test_different_back_grain_and_format_fit_without_changing_any_furniture_parts():
    document = selected()
    before = preview(document)
    original = deepcopy(document)
    document["manufacturing"]["material_stocks"] = [back_stock()]
    after = preview(document)
    assert before["manufacturing"]["geometry_compatible"]
    assert after["manufacturing"]["geometry_compatible"]
    assert after["design"] == before["design"]
    groups = after["manufacturing"]["stock_groups"]
    assert [g["selection_source"] for g in groups] == ["default", "material"]
    assert groups[1]["required_formats"] == [
        {"stock_width_um": 786_000, "stock_height_um": 294_950, "fits_machine": True}
    ]
    assert all(p["orientations"][0]["rotation_deg"] == 90 for p in groups[1]["parts"])
    assert after["workshop_handoff"]["stock_plan"] == after["manufacturing"]
    comparison = compare_furniture_profiles(
        ProfileChange(
            current=FurnitureWorkspace.model_validate(original),
            proposed=FurnitureWorkspace.model_validate(document),
        )
    )
    assert comparison["changed_dependencies"] == ["manufacturing"]
    assert comparison["changed_part_ids"] == []
    assert "cam" in comparison["invalidated_reviews"]
    assert "construction" not in comparison["invalidated_reviews"]


@pytest.mark.parametrize("field", ["stock_width_um", "stock_height_um"])
def test_one_micrometre_missing_on_either_axis_is_reported_exactly(field):
    document = selected()
    stock = back_stock()
    stock[field] -= 1
    document["manufacturing"]["material_stocks"] = [stock]
    report = preview(document)["manufacturing"]
    group = report["stock_groups"][1]
    assert not report["geometry_compatible"]
    assert not group["geometry_compatible"]
    assert all(p["fits_stock"] is False for p in group["parts"])
    shortfall = "width_shortfall_um" if field == "stock_width_um" else "height_shortfall_um"
    assert all(p["orientations"][0][shortfall] == 1 for p in group["parts"])
    assert {i["material_id"] for i in report["issues"]} == {"birch-plywood-6"}


def test_unknown_grain_in_an_override_never_inherits_the_common_grain():
    document = selected()
    document["manufacturing"]["material_stocks"] = [back_stock(stock_grain_axis=None)]
    group = preview(document)["manufacturing"]["stock_groups"][1]
    assert group["required_formats"] == []
    assert all(p["fits_stock"] is None and p["orientations"] == [] for p in group["parts"])
    assert {i["code"] for i in group["issues"]} == {"GRAIN_AXIS_REQUIRED"}


@pytest.mark.parametrize(
    "change",
    [
        {"material_id": "mdf-6"},
        {"material_version": "retired"},
        {"measured_thickness_um": 6_001},
    ],
)
def test_stale_material_bindings_are_visible_and_never_reused(change):
    document = selected()
    document["manufacturing"]["material_stocks"] = [back_stock(**change)]
    report = preview(document)["manufacturing"]
    assert not report["geometry_compatible"]
    assert report["issues"][0]["code"] == "MATERIAL_STOCK_NOT_USED"
    assert all(g["selection_source"] == "default" for g in report["stock_groups"])


def test_duplicate_material_formats_are_rejected_and_legacy_documents_keep_their_shape():
    document = selected()["manufacturing"]
    parsed = ManufacturingSelection.model_validate(document)
    assert "material_stocks" not in parsed.model_dump(mode="json")
    with pytest.raises(ValidationError, match="only one stock format"):
        ManufacturingSelection.model_validate(
            {**document, "material_stocks": [back_stock(), back_stock()]}
        )
    with pytest.raises(ValidationError):
        ManufacturingSelection.model_validate(
            {**document, "material_stocks": [back_stock(stock_width_um=True)]}
        )


def test_unused_common_sheet_does_not_block_explicit_formats_for_every_material():
    document = selected()
    common = deepcopy(document["manufacturing"])
    document["manufacturing"].update(stock_width_um=6_000_000, stock_grain_axis=None)
    document["manufacturing"]["material_stocks"] = [
        back_stock(),
        {
            **{
                key: common[key]
                for key in (
                    "stock_width_um",
                    "stock_height_um",
                    "stock_grain_axis",
                    "edge_margin_um",
                )
            },
            "material_id": "birch-plywood",
            "material_version": "screening-2026.1",
            "measured_thickness_um": 18_000,
        },
    ]
    assert preview(document)["manufacturing"]["geometry_compatible"]


def test_a_sheet_larger_than_the_machine_blocks_even_when_every_part_fits():
    document = selected()
    document["manufacturing"]["material_stocks"] = [back_stock(stock_width_um=6_000_000)]
    group = preview(document)["manufacturing"]["stock_groups"][1]
    assert all(p["fits_stock"] for p in group["parts"])
    assert not group["geometry_compatible"]
    assert {i["code"] for i in group["issues"]} == {"STOCK_EXCEEDS_MACHINE"}


def test_long_customer_parts_report_required_machine_area_without_any_automatic_splice():
    document = selected()
    document["design"]["intent"].update(width_um=4_340_000, height_um=2_540_000, depth_um=280_000)
    result = preview(document)
    groups = result["manufacturing"]["stock_groups"]
    assert all(not g["geometry_compatible"] for g in groups)
    assert groups[0]["required_formats"][0]["stock_height_um"] == 4_326_000
    assert not groups[0]["required_formats"][0]["fits_machine"]
    assert result["workspace"]["design"] == FurnitureWorkspace.model_validate(
        document
    ).design.model_dump(mode="json")
    assert not result["physical_cutting_authorized"]


@pytest.mark.parametrize("family", ["shelving", "table", "chest_of_drawers"])
def test_all_families_export_one_exact_plan_row_for_each_cad_part(family):
    document = workspace(family).model_dump(mode="json")
    document["manufacturing"] = selected()["manufacturing"]
    result = preview(document)
    rows = [p for g in result["manufacturing"]["stock_groups"] for p in g["parts"]]
    assert {r["part_id"] for r in rows} == {p["part_id"] for p in result["design"]["parts"]}
    assert len(rows) == len(result["design"]["parts"])


def test_non_directional_material_reports_both_minimal_axis_envelopes():
    document = workspace("table", width_um=1_000_000, depth_um=600_000).model_dump(mode="json")
    document["design"]["material"]["material_id"] = "mdf"
    document["manufacturing"] = selected()["manufacturing"]
    document["manufacturing"]["stock_grain_axis"] = None
    group = preview(document)["manufacturing"]["stock_groups"][0]
    assert len(group["required_formats"]) == 2
    for required in group["required_formats"]:
        assert all(
            any(
                option["required_stock_width_um"] <= required["stock_width_um"]
                and option["required_stock_height_um"] <= required["stock_height_um"]
                for option in part["orientations"]
            )
            for part in group["parts"]
        )


def test_material_format_order_does_not_change_revision_or_planning_identity():
    document = selected()
    document["manufacturing"]["material_stocks"] = [
        back_stock(),
        {
            **back_stock(),
            "material_id": "birch-plywood",
            "measured_thickness_um": 18_000,
        },
    ]
    original = preview(document)
    document["manufacturing"]["material_stocks"].reverse()
    assert preview(document) == original
