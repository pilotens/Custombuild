from copy import deepcopy

import pytest
from custombuild_domain.measured_assembly import MeasuredAssembly, review_measured_assembly
from custombuild_domain.measured_profiles import ConvexProfile
from custombuild_domain.trestle_example import trestle_example
from pydantic import ValidationError

from tests.unit.test_measured_assembly import pair


@pytest.mark.parametrize(
    "points",
    [
        [[0, 0], [20, 20], [0, 20], [20, 0]],
        [[0, 0], [20, 0], [10, 10], [20, 20], [0, 20]],
        [[0, 0], [10, 0], [20, 0]],
        [[0, 0], [20, 0], [20, 20], [0, 0]],
        [[0, 0], [20.1, 0], [0, 20]],
        [[0, 0], [True, 0], [0, 20]],
    ],
)
def test_invalid_profiles_are_rejected(points):
    with pytest.raises(ValidationError):
        ConvexProfile(axes="xy", points_um=points, source="Prov")


def test_profile_bounds_and_radius_are_validated_without_clamping():
    base = trestle_example().model_dump(mode="json")
    for value in (475001, 0, -1, 60000.1, True):
        work = deepcopy(base)
        work["parts"][0]["profile"]["radius_um"] = value
        with pytest.raises(ValidationError):
            MeasuredAssembly.model_validate(work)
    base["parts"][1]["profile"]["points_um"][2][0] += 1
    with pytest.raises(ValidationError, match="omslutande"):
        MeasuredAssembly.model_validate(base)


def test_profile_joint_cannot_inherit_orthogonal_success_and_room_stays_incomplete():
    work = pair("screw")
    assert (
        review_measured_assembly(MeasuredAssembly.model_validate(work)).joints[0].state == "aligned"
    )
    work["parts"][1]["profile"] = dict(
        kind="rounded_rectangle", axes="xy", radius_um=3000, source="Prov"
    )
    work["room"] = dict(size_um=[100000, 100000, 100000], source="Prov")
    result = review_measured_assembly(MeasuredAssembly.model_validate(work))
    assert result.joints[0].state == result.bearings[0].state == "unresolved"
    assert result.installation.state == "unresolved"
    assert result.unreviewed_profile_parts == ("b",)
    assert result.installation.unresolved_parts == ("b",)


def test_trestle_has_thirteen_independent_parts_and_no_invented_drillings():
    work = trestle_example()
    report = review_measured_assembly(work)
    assert len(work.parts) == len({p.id for p in work.parts}) == 13
    assert len(report.unreviewed_profile_parts) == 9
    assert len(report.issues) == 13
    assert not work.joints and not work.bearings and not any(p.operations for p in work.parts)
    assert {s.state for s in report.stock_checks} == {"unresolved"}
    assert not report.physical_cutting_authorized


def test_legacy_receipt_does_not_change_when_profile_is_absent():
    data = pair("screw")
    a = review_measured_assembly(MeasuredAssembly.model_validate(data))
    for part in data["parts"]:
        part["profile"] = None
    assert review_measured_assembly(MeasuredAssembly.model_validate(data)) == a
