from copy import deepcopy
from importlib.util import find_spec

import pytest
from custombuild_cad.adapter import UnsupportedCADFeatureError
from custombuild_cad.measured_machining import (
    _joint_audit,
    audit_machined_assembly,
    machined_solid,
    operation_cutter,
)
from custombuild_domain.measured_assembly import MeasuredAssembly
from custombuild_domain.trestle_construction import construct_trestle
from custombuild_domain.trestle_planning import TrestleRequest

pytestmark = pytest.mark.skipif(find_spec("cadquery") is None, reason="Native CAD required")


@pytest.fixture(scope="module")
def machined_table():
    package = construct_trestle(TrestleRequest())
    audit, solids = audit_machined_assembly(package.assembly)
    return package, audit, solids


def test_every_joint_and_actual_cut_solid_passes(machined_table):
    _, audit, solids = machined_table
    assert audit["geometric_checks_passed"]
    assert audit["pairs_checked"] == 78
    assert audit["connected_parts"] == 13
    assert len(audit["joints"]) == 29
    assert not audit["wood_intersections"] and not audit["hardware_intersections"]
    assert all(s.isValid() and len(s.Solids()) == 1 for s in solids.values())
    assert not audit["load_rating_verified"] and not audit["physical_cutting_authorized"]


@pytest.mark.parametrize(
    "length,width,height,load",
    [
        (1800, 850, 700, 1500),
        (2600, 850, 700, 1500),
        (1800, 1100, 800, 100),
    ],
)
def test_dimension_boundaries_and_widest_moisture_envelope(length, width, height, load):
    from custombuild_cad.trestle_checks import audit_trestle_movement

    package = construct_trestle(
        TrestleRequest(
            length_mm=length,
            width_mm=width,
            height_mm=height,
            central_load_n=load,
            moisture_max_pct=14,
        )
    )
    audit, solids = audit_machined_assembly(package.assembly)
    assert audit["geometric_checks_passed"], audit
    assert audit_trestle_movement(package, solids)["passed"]


def test_full_depth_blind_pilot_cannot_be_exported(machined_table):
    package, _, _ = machined_table
    top = package.assembly.parts[0]
    bad = top.operations[0].model_copy(update={"depth_um": top.size_um[2]})
    with pytest.raises(UnsupportedCADFeatureError, match="blindhål"):
        operation_cutter(top, bad)


def test_one_micrometre_pilot_error_is_rejected_then_restored(machined_table):
    package, _, solids = machined_table
    bad = package.assembly.model_dump(mode="json")
    bad["parts"][0]["operations"][0]["u_um"] += 1
    mutated = MeasuredAssembly.model_validate(bad)
    joint = next(j for j in mutated.joints if j.id == "MOUNT-1")
    check, _ = _joint_audit(joint, {p.id: p for p in mutated.parts}, solids)
    assert not check["passed"] and "borrcentrum" in check["errors"][0]
    check, _ = _joint_audit(joint, {p.id: p for p in package.assembly.parts}, solids)
    assert check["passed"]


@pytest.mark.parametrize("joint_id,length", [("FRAME-L-TOP-F-1", 145000), ("MOUNT-1", 60000)])
def test_short_bolt_and_long_screw_cannot_pass(joint_id, length, machined_table):
    package, _, solids = machined_table
    parts = {p.id: p for p in package.assembly.parts}
    joint = next(j for j in package.assembly.joints if j.id == joint_id)
    bad = joint.model_copy(update={"length_um": length})
    check, _ = _joint_audit(bad, parts, solids)
    assert not check["passed"]
    assert _joint_audit(joint, parts, solids)[0]["passed"]


def test_hole_in_empty_corner_of_sloping_leg_is_not_silently_lost(machined_table):
    package, _, _ = machined_table
    part = deepcopy(package.assembly.parts[1].model_dump(mode="json"))
    part["operations"][0].update(u_um=10000, v_um=650000)
    from custombuild_domain.measured_assembly import MeasuredPart

    with pytest.raises(UnsupportedCADFeatureError, match="inget material"):
        machined_solid(MeasuredPart.model_validate(part))


def test_slots_allow_screw_and_washer_travel_at_both_endpoints(machined_table):
    import cadquery as cq

    package, _, solids = machined_table
    parts = {p.id: p for p in package.assembly.parts}
    for j in package.assembly.joints:
        if j.kind != "screw" or j.id == "MOUNT-5":
            continue
        part = parts[j.a.part]
        op = next(o for o in part.operations if o.id == j.a.operation)
        cutter = operation_cutter(part, op)
        x = (part.origin_um[0] + op.u_um) / 1000
        y = (part.origin_um[1] + op.v_um) / 1000
        z = part.origin_um[2] / 1000
        for sign in (-1, 1):
            # Check simultaneous across-grain movement and 1 mm longitudinal
            # positioning allowance plus a declared 0.0001/% axial estimate.
            dx = 1 + abs(x - package.request.length_mm / 2) * 0.0001 * 6
            dy = package.sizing.mounting_travel_each_way_mm * sign
            for x_sign in (-1, 1):
                shaft = cq.Solid.makeCylinder(2.5, 140, cq.Vector(x + dx * x_sign, y + dy, z))
                assert shaft.cut(cutter).Volume() < 1e-6
                washer = cq.Solid.makeCylinder(12, 2, cq.Vector(x + dx * x_sign, y + dy, z + 118))
                assert washer.intersect(solids[part.id]).Volume() < 1e-6
