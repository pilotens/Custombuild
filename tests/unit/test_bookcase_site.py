"""Room dimensions are independent measurements; the part trajectory determines the reservation."""

import pytest
from custombuild_domain.furniture import InstallationSpace
from custombuild_domain.furniture_dimensions import review_furniture_dimensions
from custombuild_domain.furniture_engine import build_furniture
from custombuild_domain.furniture_production import furniture_production_source
from custombuild_manufacturing.furniture_profiles import preview_furniture

from tests.unit.test_bookcase_angles import trial


def installed_trial(left=20_000, right=20_000, floor=0, ceiling=None, front=None, **profile):
    sample = trial(shelf_count=2, divider_count=0)
    p = sample.design.intent
    site = InstallationSpace(
        width_um=p.width_um + left + right,
        height_um=p.height_um + (floor or 0),
        depth_um=p.depth_um + 20_000,
        left_allowance_um=left,
        right_allowance_um=right,
        rear_allowance_um=20_000,
        front_allowance_um=0,
        top_allowance_um=0,
        bottom_allowance_um=floor,
        measured_ceiling_height_um=ceiling,
        measured_front_space_um=front,
        trim_profile={
            "height_um": 90_000,
            "width_um": 20_000,
            "use": "existing_room_trim",
            "walls": ["left", "right", "rear"],
            **profile,
        },
    )
    return sample.model_copy(
        update={"design": sample.design.model_copy(update={"installation": site})}
    )


def test_reference_strokes_inside_finished_width_still_conflict_with_skirting():
    w = installed_trial()
    before = w.model_dump(mode="json")
    report = preview_furniture(w)
    dims = report["workshop_handoff"]["dimensions"]
    site = dims["standing_assembly"]
    # The back and bottom project 6 mm into each cheek. The last 18 mm cap
    # is lowered from above the cheeks. No CAD calculator supplies these oracles.
    assert [(wall["required_reservation_um"], wall["margin_um"]) for wall in site["walls"]] == [
        (26_000, -6_000),
        (26_000, -6_000),
        (20_000, 0),
    ]
    assert site["minimum_height_from_base_um"] == 1_218_000
    assert site["minimum_ceiling_height_um"] == 1_218_000
    assert site["measured_ceiling_height_um"] is None and site["state"] == "blocked"
    assert dims["state"] == "requires_resolution"
    assert {r["code"] for r in site["issues"]} == {
        "STANDING_LEFT_CLEARANCE",
        "STANDING_RIGHT_CLEARANCE",
        "STANDING_HEIGHT_UNMEASURED",
        "STANDING_FRONT_UNMEASURED",
    }
    assert all(
        "CB-" + r["code"].replace("_", "-")
        in {rule["rule_id"] for rule in report["rules"]["evaluations"] if rule["status"] == "BLOCK"}
        for r in site["issues"]
    )
    assert w.model_dump(mode="json") == before
    with pytest.raises(ValueError, match="Monteringssteg"):
        furniture_production_source(w)


@pytest.mark.parametrize(
    ("left", "ceiling", "state"),
    [
        (25_999, 1_218_001, "blocked"),
        (26_000, 1_218_001, "blocked"),
        (26_001, 1_217_999, "blocked"),
        (26_001, 1_218_000, "blocked"),
        (26_001, 1_218_001, "nominal_clear"),
        (26_001, None, "requires_measurement"),
    ],
)
def test_positive_nominal_margin_is_separate_from_physical_qualification(left, ceiling, state):
    w = installed_trial(left, 27_000, ceiling=ceiling, front=1_000_000)
    review = review_furniture_dimensions(w.design)
    assert review["standing_assembly"]["state"] == state and not review["production_qualified"]


def test_above_skirt_needs_only_the_bare_wall_stroke_and_floor_offset_counts_for_ceiling():
    w = installed_trial(20_000, 20_000, floor=90_000, ceiling=1_300_000)
    site = review_furniture_dimensions(w.design)["standing_assembly"]
    assert [v["required_reservation_um"] for v in site["walls"]] == [6000, 6000, 0]
    assert site["minimum_ceiling_height_um"] == 1_308_000 and site["state"] == "blocked"
    w = installed_trial(floor=None, ceiling=1_500_000, width_um=None)
    site = review_furniture_dimensions(w.design)["standing_assembly"]
    assert site["state"] == "requires_measurement" and site["minimum_ceiling_height_um"] is None
    assert all(wall["margin_um"] is None for wall in site["walls"])


def test_ceiling_measurement_changes_revision_identity_without_moving_timber():
    old = installed_trial()
    corrected = installed_trial(ceiling=1_300_000)
    assert "measured_ceiling_height_um" not in old.design.installation.model_dump()
    a, b = (build_furniture(w.design) for w in (old, corrected))
    assert a.parts == b.parts and a.design_hash != b.design_hash


def test_front_insertion_clearance_is_independent_of_customer_depth_and_trim():
    w = installed_trial(27_000, 27_000, ceiling=1_300_000, front=0)
    site = review_furniture_dimensions(w.design)["standing_assembly"]
    assert site["state"] == "blocked" and site["minimum_front_space_um"] > 0
    assert [i["code"] for i in site["issues"]] == ["STANDING_FRONT_CLEARANCE"]
    w = installed_trial(27_000, 27_000, ceiling=1_300_000, front=site["minimum_front_space_um"])
    assert review_furniture_dimensions(w.design)["standing_assembly"]["state"] == "blocked"


def test_unavailable_rear_space_blocks_even_if_that_wall_has_no_outward_stroke():
    w = installed_trial(27_000, 27_000, ceiling=1_300_000, front=600_000)
    changed = w.design.installation.model_copy(update={"rear_allowance_um": 19_999})
    result = review_furniture_dimensions(w.design.model_copy(update={"installation": changed}))
    assert result["standing_assembly"]["state"] == "blocked"
    assert "STANDING_REAR_CLEARANCE" in {i["code"] for i in result["issues"]}


@pytest.mark.parametrize(
    ("survey", "left_gap", "state"),
    [
        (None, 0, "requires_measurement"),
        ((), 0, "nominal_clear"),
        (("left",), 0, "blocked"),
        (("left",), 6000, "blocked"),
        (("left",), 6001, "nominal_clear"),
    ],
)
def test_bare_wall_and_unsurveyed_space_are_distinct_from_an_open_side(survey, left_gap, state):
    w = installed_trial(left=left_gap, ceiling=7_000_000, front=10_000_000)
    site = w.design.installation.model_dump()
    site.update(trim_profile=None, additional_bounded_walls=survey)
    selected = InstallationSpace.model_validate(site)
    check = review_furniture_dimensions(w.design.model_copy(update={"installation": selected}))
    assert check["standing_assembly"]["state"] == state
    if survey:
        assert check["standing_assembly"]["walls"][0]["required_reservation_um"] == 6000
    if survey is None:
        assert "STANDING_WALL_LAYOUT_UNMEASURED" in {i["code"] for i in check["issues"]}


def test_bare_wall_next_to_a_trimmed_wall_does_not_inherit_its_projection():
    w = installed_trial(27_000, 7000, ceiling=1_300_000, front=600_000, walls=["left"])
    site = w.design.installation.model_dump()
    site["additional_bounded_walls"] = ["right"]
    selected = InstallationSpace.model_validate(site)
    check = review_furniture_dimensions(w.design.model_copy(update={"installation": selected}))
    report = check["standing_assembly"]
    assert report["state"] == "nominal_clear"
    assert [(v["wall"], v["required_reservation_um"]) for v in report["walls"]] == [
        ("left", 26_000),
        ("right", 6000),
    ]
    with pytest.raises(ValueError):
        InstallationSpace.model_validate({**site, "additional_bounded_walls": ["right", "right"]})


def test_removing_all_site_measurements_keeps_the_required_timber_travel():
    from custombuild_domain.identity import content_hash

    installed = installed_trial(27_000, 27_000, ceiling=1_400_000, front=600_000)
    measured = review_furniture_dimensions(installed.design)["standing_assembly"]
    removed = installed.design.model_copy(update={"installation": None})
    geometry = build_furniture(removed)
    review = review_furniture_dimensions(removed, geometry.shelving_result)
    assert review["state"] == "requires_resolution" and review["installation"] is None
    assert review["required_carcass_dimensions_um"] is None
    site = review["standing_assembly"]
    assert site["state"] == "requires_measurement" and site["walls"] == []
    assert site["minimum_height_from_base_um"] == measured["minimum_height_from_base_um"]
    assert site["minimum_front_space_um"] == measured["minimum_front_space_um"]
    assert site["minimum_ceiling_height_um"] is None
    assert site["issues"] == review["issues"]
    assert [issue["code"] for issue in site["issues"]] == ["STANDING_SITE_UNMEASURED"]
    assert content_hash(removed) != content_hash(installed.design)


def test_known_bare_wall_is_checked_while_other_sides_are_unsurveyed():
    w = installed_trial(5_000, 27_000, ceiling=1_400_000, front=600_000, walls=["right"])
    site = InstallationSpace.model_validate(
        {
            **w.design.installation.model_dump(),
            "additional_bounded_walls": ["left"],
            "remaining_walls_unmeasured": True,
        }
    )
    report = review_furniture_dimensions(w.design.model_copy(update={"installation": site}))
    assembly = report["standing_assembly"]
    assert assembly["state"] == "blocked"
    assert {wall["wall"] for wall in assembly["walls"]} == {"left", "right"}
    issues = {issue["code"]: issue["message"] for issue in assembly["issues"]}
    assert set(issues) == {"STANDING_LEFT_CLEARANCE", "STANDING_WALL_LAYOUT_UNMEASURED"}
    assert "bakväggen" in issues["STANDING_WALL_LAYOUT_UNMEASURED"]
    assert "vänster vägg" not in issues["STANDING_WALL_LAYOUT_UNMEASURED"]
    assert site == InstallationSpace.model_validate_json(site.model_dump_json())


def test_site_message_preserves_one_micrometre_of_insufficient_ceiling_height():
    # 1200 mm carcass + 18 mm top insertion + 1 µm offset from the floor.
    w = installed_trial(27_000, 27_000, floor=1, ceiling=1_218_000, front=600_000)
    site = review_furniture_dimensions(w.design)["standing_assembly"]
    assert site["state"] == "blocked"
    ceiling = next(i for i in site["issues"] if i["code"] == "STANDING_CEILING_CLEARANCE")
    assert "1218.001 mm" in ceiling["message"] and "Uppmätt 1218 mm" in ceiling["message"]
