from __future__ import annotations

from copy import deepcopy

import pytest
from custombuild_domain.furniture import FurnitureWorkspace, InstallationSpace, ProfileChange
from custombuild_domain.furniture_dimensions import review_furniture_dimensions
from custombuild_domain.furniture_engine import build_furniture
from custombuild_domain.furniture_production import (
    FurnitureProductionSource,
    furniture_production_source,
)
from custombuild_manufacturing.furniture_profiles import preview_furniture

from tests.unit.test_furniture_families import workspace


def installed(family="shelving", *, trim=False, allowances=0):
    value = workspace(family).model_dump(mode="json")
    intent = value["design"]["intent"]
    value["design"]["installation"] = {
        **{key: intent[key] for key in ("width_um", "height_um", "depth_um")},
        "width_includes_trim": trim,
        **{
            f"{side}_allowance_um": allowances
            for side in ("left", "right", "top", "bottom", "front", "rear")
        },
    }
    return FurnitureWorkspace.model_validate(value)


@pytest.mark.parametrize("family", ["shelving", "table", "chest_of_drawers"])
def test_customer_measurements_are_saved_and_change_intent_without_inventing_parts(family):
    original = workspace(family)
    selected = installed(family)
    first, second = build_furniture(original.design), build_furniture(selected.design)
    assert first.parts == second.parts
    assert first.intent_hash != second.intent_hash
    assert first.design_hash != second.design_hash
    assert review_furniture_dimensions(selected.design)["state"] == "reconciled"
    assert FurnitureWorkspace.model_validate_json(selected.model_dump_json()) == selected


@pytest.mark.parametrize("allowances", [None, 10_000])
def test_unknown_or_unreconciled_allowances_cannot_create_a_production_source(allowances):
    selected = installed(allowances=allowances)
    with pytest.raises(ValueError):
        furniture_production_source(selected)
    review = review_furniture_dimensions(selected.design)
    assert review["state"] == "requires_resolution"
    if allowances is None:
        assert review["required_carcass_dimensions_um"] is None
    else:
        assert review["required_carcass_dimensions_um"]["width_um"] == 880_000


def test_zero_allowances_do_not_silently_qualify_unmodelled_trim():
    selected = installed(trim=True)
    with pytest.raises(ValueError, match="list"):
        furniture_production_source(selected)
    preview = preview_furniture(selected)
    assert any(
        "TRIM-DESIGN-REQUIRED" in rule["rule_id"] for rule in preview["rules"]["evaluations"]
    )
    assert preview["rules"]["overall_status"] == "BLOCK"
    assert not preview["physical_cutting_authorized"]


def test_a_direct_source_payload_cannot_bypass_unresolved_customer_dimensions():
    from custombuild_domain.identity import content_hash

    source = furniture_production_source(workspace("shelving")).model_dump(mode="json")
    candidate = installed(trim=True)
    source.update(
        workspace=candidate.model_dump(mode="json"),
        workspace_sha256=content_hash(candidate),
        furniture_design_hash=build_furniture(candidate.design).design_hash,
    )
    with pytest.raises(ValueError, match="list"):
        FurnitureProductionSource.model_validate(source)


def test_profile_swap_cannot_change_customer_measurements():
    current = installed()
    changed = current.model_dump(mode="json")
    changed["design"]["installation"]["width_um"] += 1
    with pytest.raises(ValueError, match="locked design intent"):
        ProfileChange(current=current, proposed=FurnitureWorkspace.model_validate(changed))


@pytest.mark.parametrize("value", [-1, True, 0.1, 500_001])
def test_installation_measurements_require_exact_nonnegative_micrometres(value):
    with pytest.raises(ValueError):
        InstallationSpace(
            width_um=4_340_000, height_um=2_540_000, depth_um=280_000, left_allowance_um=value
        )


def test_an_allowance_cannot_consume_a_dimension_even_when_another_axis_is_unknown():
    with pytest.raises(ValueError, match="consume"):
        InstallationSpace(
            width_um=4_340_000, height_um=2_540_000, depth_um=280_000, front_allowance_um=280_000
        )


def test_old_workspaces_keep_their_canonical_schema_and_hash_when_extensions_are_unused():
    import json
    from pathlib import Path

    fixture = json.loads(
        Path("apps/web/components/fixtures/furniture-production-preview.json").read_text()
    )
    source = fixture["source_furniture"]
    assert FurnitureProductionSource.model_validate(source).model_dump(mode="json") == source


def test_new_customer_dimensions_replace_the_old_dimensions_without_selecting_trim_width():
    import json
    from pathlib import Path

    selected = FurnitureWorkspace.model_validate(
        json.loads(Path("examples/furniture/bookcase-4340x2540x280.json").read_text())
    )
    result = preview_furniture(selected)
    measurement = result["workshop_handoff"]["dimensions"]
    assert measurement["installation"]["width_um"] == 4_340_000
    assert measurement["installation"]["height_um"] == 2_540_000
    assert measurement["installation"]["depth_um"] == 280_000
    assert measurement["installation"]["width_includes_trim"]
    assert measurement["installation"]["trim_profile"] == {
        "height_um": 90_000,
        "width_um": 20_000,
        "use": "unassigned",
        "walls": [],
    }
    assert measurement["required_carcass_dimensions_um"] is None
    assert result["manufacturing"]["state"] == "not_selected"
    with pytest.raises(ValueError):
        furniture_production_source(selected)
    # Another customer's dimensions produce another source, without editing code.
    different = deepcopy(selected.model_dump(mode="json"))
    different["design"]["intent"]["width_um"] = 3_210_007
    different["design"]["installation"]["width_um"] = 3_210_007
    assert (
        build_furniture(FurnitureWorkspace.model_validate(different).design).design_hash
        != (result["design"]["design_hash"])
    )


@pytest.mark.parametrize("height,width", [(90_000, 20_000), (73_001, 12_003)])
def test_existing_room_trim_uses_explicit_clearance_without_creating_or_subtracting_parts_twice(
    height, width
):
    value = installed(trim=True).model_dump(mode="json")
    installation = value["design"]["installation"]
    installation["trim_profile"] = {
        "height_um": height,
        "width_um": width,
        "use": "existing_room_trim",
        "walls": ["left", "rear"],
    }
    selected = FurnitureWorkspace.model_validate(value)
    assert "ROOM_TRIM_CLEARANCE_REQUIRED" in {
        v["code"] for v in review_furniture_dimensions(selected.design)["issues"]
    }
    with pytest.raises(ValueError):
        furniture_production_source(selected)
    installation.update(left_allowance_um=width, rear_allowance_um=width)
    value["design"]["intent"]["width_um"] -= width
    value["design"]["intent"]["depth_um"] -= width
    selected = FurnitureWorkspace.model_validate(value)
    review = review_furniture_dimensions(selected.design)
    assert review["state"] == "reconciled"
    source = furniture_production_source(selected)
    assert source.workspace.design.intent.width_um == 900_000 - width
    assert source.workspace.design.intent.depth_um == 320_000 - width
    assert source.workspace.design.intent.height_um == 1_800_000
    assert len(build_furniture(selected.design).parts) == len(
        build_furniture(installed().design).parts
    )


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"use": "unassigned"}, "TRIM_USE_REQUIRED"),
        ({"use": "furniture_trim"}, "TRIM_DESIGN_REQUIRED"),
        ({"use": "existing_room_trim"}, "ROOM_TRIM_LOCATION_REQUIRED"),
        ({"height_um": None}, "TRIM_DIMENSIONS_REQUIRED"),
        ({"width_um": None}, "TRIM_DIMENSIONS_REQUIRED"),
    ],
)
def test_profile_measurements_never_guess_function_location_or_missing_dimensions(patch, code):
    value = installed(trim=True).model_dump(mode="json")
    value["design"]["installation"]["trim_profile"] = {
        "height_um": 90_000,
        "width_um": 20_000,
        "use": "unassigned",
        "walls": [],
        **patch,
    }
    selected = FurnitureWorkspace.model_validate(value)
    assert code in {v["code"] for v in review_furniture_dimensions(selected.design)["issues"]}
    with pytest.raises(ValueError):
        furniture_production_source(selected)


@pytest.mark.parametrize(
    "patch",
    [
        {"height_um": True},
        {"width_um": 20.001},
        {"height_um": 0},
        {"use": "existing_room_trim", "walls": ["rear", "rear"]},
        {"use": "furniture_trim", "walls": ["left"]},
    ],
)
def test_trim_schema_rejects_inexact_or_inconsistent_measurements(patch):
    from custombuild_domain.furniture import TrimProfile

    with pytest.raises(ValueError):
        TrimProfile.model_validate({"height_um": 90_000, "width_um": 20_000, **patch})
