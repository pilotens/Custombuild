from copy import deepcopy

import pytest
from custombuild_domain.measured_fasteners import Shaft, review_fasteners

from tests.product_acceptance.measured import frame_file
from tests.unit.test_measured_assembly import pair, run


def shaft(name, axis, center, limits, diameter=4000, certain=None, frame=None):
    return Shaft(name, frame, axis, center, diameter, certain or limits, limits)


def test_joint_reservation_uses_physical_volume_across_distinct_operation_ids():
    work = pair("screw")
    for part in work["parts"]:
        part["operations"].append({**part["operations"][0], "id": "duplicate"})
    extra = deepcopy(work["joints"][0])
    extra["id"] = "new-screw"
    extra["a"]["operation"] = extra["b"]["operation"] = "duplicate"
    work["joints"].append(extra)
    report = run(work)
    assert [(c.joint_a, c.joint_b, c.state) for c in report.fastener_interferences] == [
        ("linked", "new-screw", "mismatch")
    ]
    assert all(c.state == "mismatch" for c in report.joints)
    for part in work["parts"]:
        part["operations"][1]["u_um"] += 4_000
    assert not run(work).fastener_interferences
    assert all(c.state == "aligned" for c in run(work).joints)
    for part in work["parts"]:
        part["operations"][1]["u_um"] -= 1
    assert len(run(work).fastener_interferences) == 1


@pytest.mark.parametrize("axis", [0, 1, 2])
def test_finite_perpendicular_shafts_intersect_only_with_positive_volume(axis):
    other = (axis + 1) % 3
    free = 3 - axis - other
    base = shaft("a", axis, (0, 0, 0), (0, 10_000), 2000)
    p = [0, 0, 0]
    p[axis] = 10_000
    p[free] = 2_000

    def crossed():
        return review_fasteners([base, shaft("b", other, tuple(p), (-5_000, 5_000), 2000)])

    assert not crossed()
    p[free] -= 1
    assert crossed()[0].state == "mismatch"
    p[axis] += 1_000
    assert not crossed()


def test_partial_dowel_reservations_and_separate_frames_are_distinguished():
    peg = shaft("peg", 0, (0, 0, 0), (-10_000, 10_000), certain=(-6_000, 6_000))
    end = shaft("end", 1, (9_000, 0, 0), (-4_000, 4_000), 2000)
    assert review_fasteners([peg, end])[0].state == "unresolved"
    inside = shaft("end", 1, (3_000, 0, 0), (-4_000, 4_000), 2000)
    assert review_fasteners([peg, inside])[0].state == "mismatch"
    assert not review_fasteners([peg, shaft("end", 1, (0, 0, 0), (-4_000, 4_000), frame="door")])
    assert not review_fasteners([peg, shaft("outside", 0, (0, 0, 0), (10_000, 15_000))])


def test_excess_conflicts_reject_the_report_without_truncating_it_to_success():
    rods = [shaft(f"p{i}", 0, (0, 0, 0), (0, 10_000)) for i in range(65)]
    with pytest.raises(ValueError, match="överlappande beslag"):
        review_fasteners(rods)


@pytest.mark.parametrize("end,target", [(0, "bottom"), (1, "tenon"), (1, "shoulder")])
def test_lost_mortise_bottom_tongue_or_shoulder_invalidates_the_joint(end, target):
    work = frame_file(two_ended=False)
    part = work["parts"][end]
    op = part["operations"][0]
    cut = {**op, "shape": "notch", "id": "removed"}
    if target == "bottom":
        cut.update(depth_um=part["size_um"][1], through=True)
    if target == "shoulder":
        # Cut behind, but not into, the nominal tongue from the opposite end.
        cut.update(face="back", depth_um=part["size_um"][1] - op["depth_um"])
    part["operations"].append(cut)
    result = run(work)
    assert result.joints[0].state == "mismatch"
    assert any("Tapp" in s or "tapphål" in s for s in result.issues[part["id"]])
    part["operations"].pop()
    assert run(work).joints[0].state == "aligned"


def test_source_article_on_pilot_must_correspond_to_the_fastener_or_a_declared_fitting():
    work = pair("screw")
    work["parts"][1]["operations"][0]["article"] = "M-other"
    assert run(work).joints[0].state == "unresolved"
    work["joints"][0]["fittings"] = ["M-other"]
    assert run(work).joints[0].state == "aligned"
