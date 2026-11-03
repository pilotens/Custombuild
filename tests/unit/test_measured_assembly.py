from copy import deepcopy
from math import prod

import pytest
from custombuild_domain.measured_assembly import MeasuredAssembly, review_measured_assembly
from pydantic import ValidationError

from tests.product_acceptance.measured import frame_file, member


def run(data):
    return review_measured_assembly(MeasuredAssembly.model_validate(data))


def test_six_frames_have_the_same_mortise_contacts_as_their_individual_parts():
    data = frame_file(frames=6, levels=7)
    result = run(data)
    assert len(result.joints) == 84 and len(result.cells) == 54
    assert not result.issues and not result.overlaps and not result.overlaps_truncated
    assert {c.state for c in result.joints} == {"aligned"}
    assert {c.depth_reserve_um for c in result.joints} == {1125}
    assert not result.physical_cutting_authorized
    data["parts"][0]["operations"][0]["v_um"] += 1
    shifted = run(data)
    bad = [j for j in shifted.joints if j.state == "mismatch"]
    assert len(bad) == 1 and bad[0].offset_um == (0, 0, -1)
    assert shifted.overlaps and shifted.assembly_sha256 != result.assembly_sha256
    data["parts"][0]["operations"][0]["v_um"] -= 1
    assert run(data) == result


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("bottoms", "bottnar"),
        ("missing", "saknas"),
        ("self", "olika"),
        ("other-part", "existerande"),
        ("face", "riktning"),
        ("wrong-shape", "Blind tapp"),
        ("section", "tvärsnitt"),
        ("duplicate", "flera förband"),
        ("outside", "saknas"),
        ("through-pocket", "Blind tapp"),
    ],
)
def test_bad_tenons_cannot_receive_a_matching_status(fault, reason):
    d = frame_file(two_ended=False)
    post, rail = d["parts"]
    p = post["operations"][0]
    t = rail["operations"][0]
    j = d["joints"][0]
    if fault == "bottoms":
        p["depth_um"] = t["depth_um"]
    elif fault == "missing":
        j["b"]["operation"] = "none"
    elif fault == "self":
        j["a"]["part"] = post["id"]
    elif fault == "other-part":
        j["a"]["part"] = "nonexistent"
    elif fault == "face":
        t["face"] = "back"
    elif fault == "wrong-shape":
        t["shape"] = "pocket"
    elif fault == "section":
        t["height_um"] += 1
    elif fault == "duplicate":
        d["joints"].append({**deepcopy(j), "id": "reuse"})
    elif fault == "outside":
        p["u_um"] = 0
    elif fault == "through-pocket":
        p["through"] = True
        p["depth_um"] = post["size_um"][1]
    result = run(d)
    assert all(check.state == "mismatch" and reason in check.reason for check in result.joints)


def pair(kind):
    a = member("a", [40_000, 30_000, 18_000], [0, 0, 0])
    b = member("b", [40_000, 30_000, 24_000], [0, 0, 18_000])
    screw = kind == "screw"

    def bore(face, depth, diameter, through=False):
        return {
            "id": "hole",
            "face": face,
            "axes": "xy",
            "u_um": 10_000,
            "v_um": 14_000,
            "shape": "drill",
            "width_um": diameter,
            "height_um": diameter,
            "depth_um": depth,
            "through": through,
            "source": "Oberoende koordinatprov",
        }

    a["operations"] = [bore("bottom" if screw else "top", 18_000 if screw else 8_000, 6_200, screw)]
    b["operations"] = [bore("bottom", 15_000 if screw else 8_000, 3_000 if screw else 6_200)]
    joint = {
        "id": "linked",
        "a": {"part": "a", "operation": "hole"},
        "b": {"part": "b", "operation": "hole"},
        "kind": kind,
        "source": "Prov",
        "diameter_um": 4_000 if screw else 6_000,
        "length_um": 34_000 if screw else 14_000,
        "article": "v01" if screw else "d01",
        "head_allowance_um": 1000 if screw else 0,
    }
    return {
        "schema_version": "custombuild.measured-assembly.v1",
        "title": "Prov",
        "source": "Modellprov",
        "parts": [a, b],
        "joints": [joint],
        "bearings": [{"id": "rest", "part": "b", "support": "a", "source": "Kontaktprov"}],
    }


def test_screw_and_bearing_share_a_datum_and_report_real_area():
    d = pair("screw")
    r = run(d)
    assert r.joints[0].state == "aligned" and r.joints[0].depth_reserve_um == 0
    assert r.bearings[0].state == "aligned" and r.bearings[0].contact_area_um2 == 1_200_000_000
    d["parts"][1]["origin_um"][2] += 2000
    d["joints"][0].update(interface_gap_um=2000, length_um=36_000)
    r = run(d)
    assert r.joints[0].state == "aligned"
    assert r.bearings[0].state == "mismatch" and r.bearings[0].offset_um == (0, 0, 2000)
    d["joints"][0]["length_um"] += 1
    assert run(d).joints[0].state == "mismatch"
    d["joints"][0]["diameter_um"] = 7_000
    assert "diameter" in run(d).joints[0].reason


@pytest.mark.parametrize("kind", ["screw", "dowel"])
def test_missing_absolute_pose_keeps_relative_door_geometry_separate(kind):
    d = pair(kind)
    assert run(d).joints[0].state == "aligned"
    for p in d["parts"]:
        p["frame_id"] = "door-1"
    r = run(d)
    assert r.joints[0].state == "aligned" and r.joints[0].frame_id == "door-1"
    d["parts"][0]["frame_id"] = "door-2"
    r = run(d)
    assert r.joints[0].state == r.bearings[0].state == "unresolved" and not r.overlaps
    del d["parts"][0]["frame_id"]
    del d["parts"][1]["frame_id"]
    d["parts"][1]["origin_um"] = None
    r = run(d)
    assert r.joints[0].state == "unresolved" and r.bearings[0].state == "unresolved"
    assert d["parts"][1]["id"] in r.issues


def test_dowel_uses_both_depths_and_preserves_the_bore_diameter():
    d = pair("dowel")
    r = run(d)
    assert r.joints[0].depth_reserve_um == 2000
    d["joints"][0]["diameter_um"] = 6201
    assert run(d).joints[0].state == "mismatch"
    d["joints"][0]["diameter_um"] = 6000
    d["joints"][0]["length_um"] = 16_001
    assert run(d).joints[0].depth_reserve_um == -1
    d["parts"][1]["operations"][0]["width_um"] = 6100
    d["parts"][1]["operations"][0]["height_um"] = 6100
    assert "blindhål" in run(d).joints[0].reason


def test_an_odd_rectangular_cut_has_exact_half_micrometre_edges_and_reports_excess_stock():
    p = member("odd", [81_000, 35_000, 12_000], [0, 0, 0])
    p["stock_lbt_um"] = [80_000, 35_000, 12_000]
    p["operations"] = [
        {
            "id": "notch",
            "shape": "notch",
            "face": "top",
            "axes": "yx",
            "u_um": 12_000,
            "v_um": 30_000,
            "width_um": 5_501,
            "height_um": 4_003,
            "depth_um": 12_000,
            "through": True,
            "source": "Prov",
        }
    ]
    d = {
        "schema_version": "custombuild.measured-assembly.v1",
        "title": "Ansats",
        "source": "Prov",
        "parts": [p],
    }
    r = run(d)
    assert len(r.issues[p["id"]]) == 1
    assert any(value % 1 == 0.5 for b in r.cells["odd"] for value in b)
    actual = sum(prod(b[i + 3] - b[i] for i in range(3)) for b in r.cells["odd"])
    assert actual == prod(p["size_um"]) - 5_501 * 4_003 * 12_000
    p["operations"][0]["depth_um"] += 1
    p["blank_lbt_um"][0] += 100
    assert len(run(d).issues["odd"]) == 3


def test_invalid_face_axes_duplicate_instances_and_duplicate_operations_are_rejected():
    d = pair("screw")
    a = d["parts"][0]
    for patch in ({"axes": "xz"}, {"width_um": 5_500}):
        old = a["operations"][0]
        a["operations"][0] = old | patch
        with pytest.raises(ValidationError):
            run(d)
        a["operations"][0] = old
    a["operations"].append(a["operations"][0])
    with pytest.raises(ValidationError):
        run(d)
    a["operations"].pop()
    for key in ("joints", "parts", "bearings"):
        d[key].append(d[key][0])
        with pytest.raises(ValidationError):
            run(d)
        d[key].pop()
    d["bearings"][0]["support"] = "not-present"
    assert run(d).bearings[0].state == "mismatch"


def test_transferred_receiver_is_identified_even_if_the_nominal_coordinates_match():
    data = pair("screw")
    receiver = data["parts"][1]["operations"][0]
    receiver["provenance"] = "transfer_template"
    result = run(data)
    assert result.joints[0].state == "aligned"
    assert result.joints[0].uses_transfer_template
    receiver["v_um"] += 1
    incorrect = run(data).joints[0]
    assert incorrect.state == "mismatch"
    assert incorrect.uses_transfer_template


@pytest.mark.parametrize("kind", ["screw", "dowel"])
def test_missing_fastener_dimensions_cannot_verify_even_matching_hole_centres(kind):
    model = pair(kind)
    del model["joints"][0]["diameter_um"]
    assert run(model).joints[0].state == "unresolved"


def test_a_pilot_that_hits_a_notch_loses_its_connection_and_drill_tangency_is_allowed():
    data = pair("screw")
    part = data["parts"][1]
    bore = part["operations"][0]
    notch = {**bore, "id": "notch", "shape": "notch", "width_um": 4_000, "height_um": 14_000}
    part["operations"].append(notch)
    notch["u_um"] += 3500
    assert run(data).joints[0].state == "aligned"
    notch["u_um"] -= 1
    result = run(data)
    assert result.joints[0].state == "mismatch" and "utskuret område" in result.issues["b"][0]
    part["operations"].pop()
    bore["width_um"] = bore["height_um"] = 4000
    assert run(data).joints[0].state == "unresolved"
