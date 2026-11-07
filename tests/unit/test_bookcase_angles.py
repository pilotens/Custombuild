"""Geometric limits and the fitting-to-bore contract of the bookcase compiler."""

import pytest
from custombuild_domain.bookcase_angles import screw_access
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import build_furniture
from custombuild_domain.models import DesignResult

ARTICLE = "GN-970-ALM-30-30-30-B"


def trial(*, material="birch-plywood", thickness=18_000, **layout):
    return FurnitureWorkspace.model_validate(
        {
            "design": {
                "intent": {
                    "family": "shelving",
                    "width_um": 900_000,
                    "height_um": 1_200_000,
                    "depth_um": 280_000,
                    "trial_angle": ARTICLE,
                    **layout,
                },
                "material": {"material_id": material, "measured_thickness_um": thickness},
            }
        }
    )


@pytest.mark.parametrize(
    ("material", "thickness", "layout", "count"),
    [
        ("birch-plywood", 17_000, {"divider_count": 0, "shelf_count": 0}, 4),
        ("mdf", 19_000, {"divider_count": 0, "shelf_count": 2, "depth_um": 100_000}, 8),
        ("solid-walnut", 18_000, {"divider_count": 1, "shelf_count": 2}, 14),
        (
            "solid-walnut",
            18_000,
            {
                "divider_count": 2,
                "shelf_count": 2,
                "bay_width_ratios_ppm": [250000, 450000, 300000],
            },
            20,
        ),
    ],
)
def test_selected_profile_pairs_every_angle_with_two_distinct_pilots(
    material, thickness, layout, count
):
    result = build_furniture(trial(material=material, thickness=thickness, **layout).design)
    assert len(result.trial_angles) == count
    ids = [fid for a in result.trial_angles for fid in (a.upright_hole, a.horizontal_hole)]
    assert len(set(ids)) == 2 * count
    assert {f.feature_id for p in result.parts for f in p.features if f.joint_id is None} == set(
        ids
    )
    assert result.shelving_result
    doc = result.shelving_result.model_dump(mode="json")
    doc["trial_angles"].pop()
    with pytest.raises(ValueError, match="varje bärande fog"):
        DesignResult.model_validate(doc)


@pytest.mark.parametrize(
    ("layout", "message"),
    [
        ({"width_um": 249999}, "greater than or equal"),
        ({"height_um": 4000001}, "less than or equal"),
        ({"depth_um": 99999}, "greater than or equal"),
        ({"shelf_load_n": 5001}, "less than or equal"),
        ({"shelf_mount": "adjustable"}, "fasta hyllor"),
        ({"plinth_height_um": 90000}, "utan sockel"),
        ({"back_panel": "surface_mounted"}, "spårmonterad rygg"),
    ],
)
def test_orders_outside_the_profile_get_specific_rejections(layout, message):
    with pytest.raises(ValueError, match=message):
        build_furniture(trial(**layout).design)


@pytest.mark.parametrize("change", ["other-owner", "moved-pilot", "unassigned-pilot"])
def test_invalid_fitting_or_pilot_inventory_cannot_pass_model_readback(change):
    result = build_furniture(trial(divider_count=0, shelf_count=1).design).shelving_result
    assert result is not None
    doc = result.model_dump(mode="json")
    angle = doc["trial_angles"][0]
    part = next(p for p in doc["parts"] if p["part_id"] == angle["upright_id"])
    hole = next(h for h in part["features"] if h["feature_id"] == angle["upright_hole"])
    if change == "other-owner":
        angle["upright_id"] = next(p["part_id"] for p in doc["parts"] if p["role"] == "back")
        reason = "delar avviker"
    elif change == "moved-pilot":
        hole["origin"]["y_um"] += 1000
        reason = "pilothål avviker"
    else:
        part["features"].append({**hole, "feature_id": "fea_unassigned_pilot"})
        reason = "obunden borrning"
    with pytest.raises(ValueError, match=reason):
        DesignResult.model_validate(doc)


def test_tool_space_checks_real_axial_obstructions():
    result = build_furniture(trial(divider_count=0, shelf_count=0).design)
    angle = next(a for a in result.trial_angles if a.x_sign == 1 and a.z_sign == 1)
    spaces = screw_access(result.parts, result.trial_angles)
    # Between the two inside faces of the 900 mm carcass there are 864 mm.
    # This trajectory begins at the outside of a 4 mm bracket plate.
    # Other brackets can reduce clearance further.
    assert 0 < spaces[angle.upright_hole] <= 860000
    # Introduce an obstruction 15 mm away from the screw seat without altering the
    # nominal bracket. The trial must be rejected before a shop is asked to assemble it.
    part = next(p for p in result.parts if p.part_id == angle.upright_id)
    stop = part.model_copy(
        update={
            "part_id": "obstructing-panel",
            "placement": part.placement.model_copy(update={"x_um": angle.corner.x_um + 19000}),
        }
    )
    with pytest.raises(ValueError, match="införingsväg"):
        screw_access((*result.parts, stop), result.trial_angles)


@pytest.mark.cad
def test_cad_gate_rejects_a_purchased_body_inside_timber(tmp_path):
    cq = pytest.importorskip("cadquery")
    from custombuild_cad.adapter import CADExportError
    from custombuild_cad.bookcase_angles import mounted_trial_angles

    result = build_furniture(trial(divider_count=0, shelf_count=0).design)
    # A disconnected timber file that fills the angle's occupied corner.
    wood = cq.Assembly(name="wrong-timber")
    wood.add(cq.Solid.makeBox(900, 280, 1200), name="blocked-opening")
    path = tmp_path / "wrong.step"
    wood.export(str(path), exportType="STEP")
    with pytest.raises(CADExportError, match="intersects"):
        mounted_trial_angles(path.read_bytes(), result.trial_angles[:1])


def test_trial_hardware_loads_are_counted_without_granting_a_rating():
    from custombuild_manufacturing.furniture_profiles import preview_furniture

    w = trial(material="solid-walnut", divider_count=0, shelf_count=2, shelf_load_n=300)
    preview = preview_furniture(w)
    assert preview["manufacturing"]["state"] == "not_selected"
    rules = {r["rule_id"]: r for r in preview["rules"]["evaluations"]}
    retained = rules["CB-SHELF-ANGLE-001"]
    assert retained["status"] == "BLOCK" and rules["CB-SOLID-WOOD-001"]["status"] == "BLOCK"
    # Four outer corners plus four shelf ends, with one aluminium angle and two screws each.
    mass = sum(p["weight_g"] for p in preview["design"]["parts"])
    own_n = ((mass + 8 * 25) * 981 + 99999) // 100000
    assert retained["values"] == {
        "angle_count": 8,
        "screw_count": 16,
        "estimated_fitting_mass_g": 200,
        "required_joint_shear_n": own_n + 600,
    }
    assert not preview["production_qualified"]
