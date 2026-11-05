import pytest
from custombuild_domain.sideboard_construction import SideboardRequest, construct_sideboard
from custombuild_domain.sideboard_motion import HingePath, HingePose
from custombuild_domain.sideboard_verification import PanelMeasurements, SideboardReviewInputs
from pydantic import ValidationError


def package(**inputs):
    return construct_sideboard(
        SideboardRequest(front_percent=(21, 17, 23, 20, 19), review=SideboardReviewInputs(**inputs))
    )


def synthetic_path(article, clash=False):
    # Synthetic verification fixture. Never ship this pivot as a Blum mechanism.
    return HingePath(
        article=article,
        plate="175H7100",
        front_mm=19,
        cup_distance_mm=7 if article == "70T3750.TL" else 6,
        source="Synthetic QA",
        poses=tuple(
            HingePose(angle_deg=a, dx_mm=0, dy_mm=80 if clash and a == 10 else 0)
            for a in range(0, 111, 5)
        ),
    )


def test_front_shares_change_the_entire_assembly_and_keep_prior_receipt():
    old = construct_sideboard(SideboardRequest())
    assert (
        old.construction_sha256
        == "f73d75197efe9ff66fedbb6f046b49aeda9885d5f9612cc731fd2e293b5e5136"
    )
    new = package()
    parts = {p.id: p for p in new.assembly.parts}
    doors = [parts[f"DOOR-{i}"] for i in range(1, 6)]
    assert [p.size_um[0] for p in doors] == [490140, 396780, 536820, 466800, 443460]
    for i, (a, b) in enumerate(zip(doors, doors[1:], strict=False), 1):
        seam = a.origin_um[0] + a.size_um[0] + 1500
        assert b.origin_um[0] - 1500 == seam
        assert parts[f"DIV-{i}"].origin_um[0] + 9000 == seam
        assert parts[f"BACK-{i}"].origin_um[0] + parts[f"BACK-{i}"].size_um[0] == seam - 250
    assert new.assembly_sha256 != old.assembly_sha256
    assert new.assessment.hinge_gaps[0].required_mm == pytest.approx(1.3)
    assert all(m.state == "missing_path" for m in new.assessment.door_motion)
    assert not new.physical_cutting_authorized


@pytest.mark.parametrize("shares", [(21, 17, 23, 21, 19), (10, 10, 10, 10, 60), (20, 20, 20, 40)])
def test_bad_layout_never_silently_normalizes(shares):
    with pytest.raises(ValidationError):
        SideboardRequest(front_percent=shares)


def test_capacity_is_specific_to_shelf_and_load_including_self_weight():
    light, heavy = (
        package(shelf_payload_kg=0),
        package(shelf_payload_kg=200, pin_working_load_n=80, pin_source="Fixture"),
    )
    a, b = light.assessment, heavy.assessment
    assert light.assembly_sha256 == heavy.assembly_sha256
    assert light.construction_sha256 != heavy.construction_sha256
    small, wide = b.shelf_screens[1:3]
    assert wide.deflection_mm > small.deflection_mm and not wide.deflection_within_limit
    assert not wide.pin_within_declared_load
    assert a.shelf_screens[2].support_demand_n > 0
    assert a.shelf_screens[2].pin_within_declared_load is None
    span, depth, t = wide.span_mm, 344, 18
    weight = (200 + 680 * span * depth * t / 1e9) * 9.81
    inertia = depth * t**3 / 12
    assert wide.deflection_mm == pytest.approx(1.5 * 5 * weight * span**3 / (384 * 5200 * inertia))
    assert wide.support_demand_n == pytest.approx(weight / 2)


def test_tolerance_stack_and_mismatched_batch_remain_visible():
    batch = PanelMeasurements(
        batch="Prov", back_mm=6, shelf_divider_mm=17.7, front_mm=19.2, carcass_mm=24
    )
    r = package(measurements=batch, edge_error_mm=1, bore_depth_error_mm=1).assessment
    assert r.batch_state == "revise_dimensions"
    assert r.minimum_divider_web_mm == pytest.approx(-0.3)
    assert r.hinge_gaps[0].required_mm == pytest.approx(1.6)
    assert all(g.state == "insufficient" for g in r.hinge_gaps)
    assert not r.physical_cutting_authorized
    batch = PanelMeasurements(
        batch="Prov", back_mm=6, shelf_divider_mm=18, front_mm=19, carcass_mm=24
    )
    assert package(measurements=batch).assessment.batch_state == "matches_nominal"
    assert package().assessment.batch_state == "missing"


def test_clear_then_colliding_motion_then_repair():
    inset = synthetic_path("70T3750.TL")
    half = synthetic_path("70T3650.TL")
    good = package(hinge_paths=(inset, half))
    assert {m.state for m in good.assessment.door_motion} == {"clear_in_model"}
    fault = package(hinge_paths=(synthetic_path("70T3750.TL", True), half))
    hit = fault.assessment.door_motion[0]
    assert hit.state == "envelope_contact" and hit.obstacle and 0 < hit.angle_deg < 10
    assert good.assembly_sha256 == fault.assembly_sha256
    assert good.construction_sha256 != fault.construction_sha256
    assert package(hinge_paths=(inset, half)) == good


def test_wrong_article_configuration_duplicate_broken_or_partial_path_rejected():
    original = synthetic_path("70T3750.TL").model_dump()
    for fields in (
        {"cup_distance_mm": 6},
        {"front_mm": 18},
        {"plate": "other"},
        {"poses": original["poses"][:-1]},
        {"poses": original["poses"][:1] + original["poses"][2:]},
    ):
        with pytest.raises(ValidationError):
            HingePath.model_validate({**original, **fields})
    with pytest.raises(ValidationError):
        package(hinge_paths=(HingePath.model_validate(original),) * 2)
    with pytest.raises(ValidationError):
        package(pin_working_load_n=100)


def test_material_source_changes_stress_and_deflection_checks_without_changing_wood():
    props = dict(
        source="Fiktivt provvärde",
        density_kg_m3=800,
        service_modulus_mpa=2000,
        allowable_bending_mpa=0.1,
    )
    a = package(shelf_material=props)
    b = package(shelf_material={**props, "allowable_bending_mpa": 20, "service_modulus_mpa": 4000})
    assert a.assembly_sha256 == b.assembly_sha256 and a.construction_sha256 != b.construction_sha256
    assert a.assessment.shelf_screens[2].bending_within_declared_limit is False
    assert b.assessment.shelf_screens[2].bending_within_declared_limit is True
    assert a.assessment.shelf_screens[2].deflection_mm == pytest.approx(
        2 * b.assessment.shelf_screens[2].deflection_mm
    )


def test_continuous_screen_catches_a_collision_between_clear_recorded_poses():
    from math import cos, radians, sin

    from custombuild_domain.sideboard_motion import _separate, door_motion

    c = package()
    door = next(p for p in c.assembly.parts if p.id == "DOOR-1")
    x, y, z = (v / 1000 for v in door.origin_um)
    w, t, h = (v / 1000 for v in door.size_um)
    a = radians(42.5)
    cx = x + (w - 0.6) * cos(a) + t / 2 * sin(a)
    cy = y - (w - 0.6) * sin(a) + t / 2 * cos(a)
    obstacles = [("test stop", cx, cy, 0.3, 0.3)]
    for degrees in (40, 45):
        assert _separate(x, y, w, t, obstacles, degrees, 0, 0)[0] > 0.4
    parts = tuple(
        p.model_copy(
            update={
                "origin_um": (
                    round((cx - 0.3) * 1000),
                    round((cy - 0.3) * 1000),
                    round((z + h / 2) * 1000),
                ),
                "size_um": (600, 600, 1000),
            }
        )
        if p.id == "LATCH-BLOCK-5"
        else p
        for p in c.assembly.parts
    )
    work = c.assembly.model_copy(update={"parts": parts})
    hit = door_motion(work, (synthetic_path("70T3750.TL"),), 0.4)[0]
    assert hit.state == "envelope_contact" and hit.obstacle == "LATCH-BLOCK-5"
    assert 40 < hit.angle_deg < 45


def test_static_push_reserve_and_combined_base_record_are_bound_to_the_measured_case():
    import cadquery as cq
    from custombuild_cad.sideboard_stability import closed_stability
    from custombuild_domain.sideboard_stability_report import BaseTestRecord, ClosedStability

    batch = PanelMeasurements(
        batch="fixture", back_mm=6, shelf_divider_mm=18, front_mm=19, carcass_mm=24
    )
    original = package(measurements=batch)
    solids = {
        p.id: cq.Solid.makeBox(
            *(v / 1000 for v in p.size_um), cq.Vector(*(v / 1000 for v in p.origin_um))
        )
        for p in original.assembly.parts
    }

    def screen(**inputs):
        return ClosedStability.model_validate(
            closed_stability(package(measurements=batch, **inputs), solids)
        )

    a, b = screen(horizontal_force_n=0), screen(horizontal_force_n=1000)
    assert a.positive_reserve and not b.positive_reserve
    assert all(
        i.minimum_reserve_nm - j.minimum_reserve_nm == pytest.approx(750)
        for i, j in zip(a.edges, b.edges, strict=True)
    )
    good = BaseTestRecord(
        case="S1-closed-combined-v1",
        assembly_sha256=original.assembly_sha256,
        batch="fixture",
        source="Synthetic QA",
        total_vertical_n=10000,
        horizontal_n=200,
    )
    assert screen(base_test=good).base_test_comparison == "within_recorded_case"
    assert (
        screen(base_test=good, horizontal_force_n=201).base_test_comparison
        == "outside_recorded_case"
    )
    assert (
        screen(base_test=good.model_copy(update={"total_vertical_n": 100})).base_test_comparison
        == "outside_recorded_case"
    )
    assert (
        screen(base_test=good.model_copy(update={"assembly_sha256": "0" * 64})).base_test_comparison
        == "wrong_assembly"
    )
    assert (
        screen(base_test=good.model_copy(update={"batch": "other"})).base_test_comparison
        == "wrong_batch"
    )
    assert screen().base_test_comparison == "missing"
