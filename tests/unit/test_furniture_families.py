from __future__ import annotations

from copy import deepcopy

import pytest
from custombuild_domain.furniture import FurnitureWorkspace, ProfileChange
from custombuild_domain.furniture_engine import build_furniture
from custombuild_manufacturing.adapters import adapt_design_result
from custombuild_manufacturing.furniture_profiles import (
    compare_furniture_profiles,
    preview_furniture,
)


def workspace(family="table", **intent):
    hardware = {
        "table": "panel-table-connectors-layout",
        "chest_of_drawers": "drawer-side-mount-450-layout",
    }.get(family)
    return FurnitureWorkspace.model_validate(
        {
            "design": {
                "intent": {"family": family, **intent},
                "hardware": {"catalog_id": hardware} if hardware else None,
            }
        }
    )


@pytest.mark.parametrize("family,count", [("table", 5), ("chest_of_drawers", 23)])
def test_each_family_has_real_distinct_parts_and_neutral_panel_mapping(family, count):
    result = build_furniture(workspace(family).design)
    assert len(result.parts) == count
    assert len({p.semantic_key for p in result.parts}) == count
    adapted = adapt_design_result(result)
    assert len(adapted.parts) == count
    assert all(p.width_um > 0 and p.height_um > 0 for p in adapted.parts)
    assert result == build_furniture(workspace(family).design)
    assert result.total_weight_g == sum(p.weight_g for p in result.parts)


def test_table_closed_parts_are_inside_envelope_and_do_not_overlap():
    result = build_furniture(workspace().design)
    boxes = [
        (
            p.placement.x_um,
            p.placement.y_um,
            p.placement.z_um,
            p.finished_size.width_um,
            p.finished_size.depth_um,
            p.finished_size.height_um,
        )
        for p in result.parts
    ]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert not all(
                min(a[j] + a[j + 3], b[j] + b[j + 3]) > max(a[j], b[j]) for j in range(3)
            )


def test_drawers_have_complete_moving_groups_with_exact_remainder_distribution():
    result = build_furniture(workspace("chest_of_drawers", height_um=900_001).design)
    assert len(result.moving_groups) == 3
    assert all(len(group.part_ids) == 6 for group in result.moving_groups)
    fronts = [
        p
        for p in result.parts
        if p.semantic_key in {"drawer-1-front", "drawer-2-front", "drawer-3-front"}
    ]
    assert sum(p.finished_size.height_um for p in fronts) == 900_001 - 36_000 - 18_000
    assert all(group.travel_um == 450_000 for group in result.moving_groups)


@pytest.mark.parametrize(
    "intent",
    [
        {"width_um": 400_000, "end_inset_um": 100_000},
        {"height_um": 310_000, "stretcher_height_um": 200_000},
    ],
)
def test_impossible_table_geometry_is_rejected(intent):
    with pytest.raises(ValueError):
        build_furniture(workspace(**intent).design)


def test_drawer_profile_that_does_not_fit_is_rejected_without_changing_intent():
    draft = workspace("chest_of_drawers", depth_um=450_000)
    with pytest.raises(ValueError, match="does not fit"):
        build_furniture(draft.design)
    assert draft.design.intent.depth_um == 450_000


def test_a_machine_swap_preserves_design_and_only_invalidates_manufacturing():
    first = workspace()
    payload = first.model_dump(mode="json")
    payload["manufacturing"] = {
        "machine_profile_id": "custombuild-router-1325-linuxcnc",
        "stock_grain_axis": "x",
    }
    first = FurnitureWorkspace.model_validate(payload)
    payload["manufacturing"]["machine_profile_id"] = "custombuild-router-5125-linuxcnc"
    second = FurnitureWorkspace.model_validate(payload)
    change = compare_furniture_profiles(ProfileChange(current=first, proposed=second))
    assert change["intent_preserved"]
    assert change["changed_dependencies"] == ["manufacturing"]
    assert change["changed_part_ids"] == []
    assert "construction" not in change["invalidated_reviews"]
    assert {"cam", "nesting", "setups", "postprocessor", "workshop"} <= set(
        change["invalidated_reviews"]
    )
    assert change["state"] == "not_qualified"
    assert not change["production_qualified"]


def test_material_thickness_rebuilds_parts_but_keeps_outer_dimensions():
    first = workspace("chest_of_drawers")
    changed = first.model_dump(mode="json")
    changed["design"]["material"]["measured_thickness_um"] = 19_000
    second = FurnitureWorkspace.model_validate(changed)
    change = compare_furniture_profiles(ProfileChange(current=first, proposed=second))
    assert change["intent_preserved"]
    assert {"material", "geometry"} <= set(change["changed_dependencies"])
    assert "construction" in change["invalidated_reviews"]
    original = {p.semantic_key: p for p in build_furniture(first.design).parts}
    proposed = {p.semantic_key: p for p in build_furniture(second.design).parts}
    assert proposed["drawer-1-bottom"].finished_size.width_um < (
        original["drawer-1-bottom"].finished_size.width_um
    )
    assert second.design.intent == first.design.intent


def test_drawer_hardware_swap_recalculates_box_depth_and_travel():
    first = workspace("chest_of_drawers")
    changed = first.model_dump(mode="json")
    changed["design"]["hardware"]["catalog_id"] = "drawer-side-mount-400-layout"
    change = compare_furniture_profiles(
        ProfileChange(current=first, proposed=FurnitureWorkspace.model_validate(changed))
    )
    assert {"hardware", "geometry"} <= set(change["changed_dependencies"])
    assert change["proposed"]["design"]["moving_groups"][0]["travel_um"] == 400_000
    assert change["can_apply"]
    assert not change["proposed"]["production_qualified"]


def test_profile_change_never_silently_changes_locked_dimensions():
    first = workspace()
    changed = first.model_dump(mode="json")
    changed["design"]["intent"]["width_um"] = 900_000
    with pytest.raises(ValueError, match="locked design intent"):
        ProfileChange(current=first, proposed=FurnitureWorkspace.model_validate(changed))


@pytest.mark.parametrize("field,value", [("material_id", "oak"), ("version", "invented")])
def test_unknown_material_never_falls_back_to_mdf(field, value):
    first = workspace()
    changed = first.model_dump(mode="json")
    changed["design"]["material"][field] = value
    change = compare_furniture_profiles(
        ProfileChange(current=first, proposed=FurnitureWorkspace.model_validate(changed))
    )
    assert change["state"] == "requires_change"
    assert not change["can_apply"]
    assert change["proposed"] is None
    assert first.design.material.material_id == "birch-plywood"


def test_reference_profiles_never_claim_physical_qualification():
    for family in ("shelving", "table", "chest_of_drawers"):
        result = preview_furniture(workspace(family))
        assert not result["production_qualified"]
        assert not result["physical_cutting_authorized"]
        assert result["manufacturing"]["state"] == "not_selected"
        assert result["rules"]["evaluations"]


def test_narrow_stock_is_an_explicit_profile_incompatibility():
    payload = workspace().model_dump(mode="json")
    payload["manufacturing"] = {
        "machine_profile_id": "custombuild-router-1325-linuxcnc",
        "stock_width_um": 300_000,
        "stock_height_um": 300_000,
        "stock_grain_axis": "x",
    }
    result = preview_furniture(FurnitureWorkspace.model_validate(payload))
    assert result["manufacturing"]["state"] == "requires_change"
    assert any(i["code"] == "PART_EXCEEDS_STOCK" for i in result["manufacturing"]["issues"])


def test_a_blanket_production_approval_cannot_be_injected_in_a_profile():
    payload = deepcopy(workspace().model_dump(mode="json"))
    payload["design"]["hardware"]["production_qualified"] = True
    with pytest.raises(ValueError):
        FurnitureWorkspace.model_validate(payload)
