from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import build_furniture
from custombuild_manufacturing.adapters import adapt_design_result
from custombuild_manufacturing.furniture_handoff import (
    furniture_assembly_checks,
    furniture_first_article_checks,
    furniture_workshop_handoff,
)
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


@pytest.mark.parametrize("family", ["shelving", "table", "chest_of_drawers"])
def test_measurement_worksheet_uses_the_same_local_axes_sides_and_features_as_dxf(family):
    result = build_furniture(workspace(family).design)
    raw = furniture_first_article_checks(result)
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert raw.startswith(b"\xef\xbb\xbf")
    assert {r["design_hash"] for r in rows} == {result.design_hash}
    assert {r["part_id"] for r in rows} == {p.part_id for p in result.parts}
    assert all(
        not r[k]
        for r in rows
        for k in ("agreed_tolerance", "measured", "result", "inspector", "notes")
    )
    indexed = {(r["part_id"], r["feature_id"], r["check"]): r for r in rows}
    for part in adapt_design_result(result).parts:
        for check, expected in (
            ("finished_local_U", part.width_um),
            ("finished_local_V", part.height_um),
            ("actual_thickness", part.thickness_um),
        ):
            row = indexed[(part.part_id, "", check)]
            assert Decimal(row["expected"]) * 1000 == expected
            assert row["unit"] == "mm"
            assert row["model_tolerance"] == ""
        for feature in part.features:
            for check, expected in (("origin_U", feature.x_um), ("origin_V", feature.y_um)):
                row = indexed[(part.part_id, feature.feature_id, check)]
                assert Decimal(row["expected"]) * 1000 == expected
                assert row["side"] == feature.side.value
                assert row["model_tolerance"] == (
                    f"{feature.tolerance_um / 1000:.3f}" if feature.tolerance_um > 0 else ""
                )
            count = indexed[(part.part_id, feature.feature_id, "pattern_count")]
            assert count["unit"] == "count"
            assert int(count["expected"]) == feature.pattern_count
            if feature.pitch_um is not None:
                assert (
                    Decimal(indexed[(part.part_id, feature.feature_id, "pitch")]["expected"]) * 1000
                    == feature.pitch_um
                )


def test_handoff_distinguishes_external_row_payload_from_shelf_self_weight():
    selected = workspace(
        "shelving", width_um=4_340_000, shelf_load_basis="per_metre", shelf_load_per_metre_n=300
    )
    assert furniture_workshop_handoff(build_furniture(selected.design))["shelf_load"] == {
        "basis": "per_metre",
        "total_row_load_n": 1302,
        "load_per_metre_n": 300,
        "width_um": 4_340_000,
    }


def test_assembly_worksheet_preserves_exact_dimensions_and_leaves_acceptance_unagreed():
    result = build_furniture(workspace("shelving", width_um=4_340_007).design)
    raw = furniture_assembly_checks(result)
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    indexed = {row["check"]: row for row in rows}
    assert indexed["carcass_width"]["expected"] == "4340.007"
    assert indexed["carcass_width"]["scope"] == "carcass_excluding_trim_and_installation_allowances"
    assert {row["design_hash"] for row in rows} == {result.design_hash}
    assert all(
        not row[key]
        for row in rows
        for key in ("agreed_acceptance_criterion", "measured", "result", "inspector", "notes")
    )
    assert {
        "diagonal_difference",
        "flatness",
        "joint_fit_and_retention",
        "assembly_sequence",
        "transport_and_raising",
        "anchoring_and_stability",
        "load_test",
        "customer_dimensions_and_trim",
    } <= indexed.keys()
    assert all(not row["expected"] for row in rows if row["scope"] == "complete_assembly")
