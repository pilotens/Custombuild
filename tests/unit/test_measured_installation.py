import pytest
from custombuild_domain.identity import content_hash
from pydantic import ValidationError

from tests.product_acceptance.measured import installation_file, member
from tests.unit.test_measured_assembly import pair, run


def test_final_fit_does_not_imply_clearance_to_raise_a_whole_carcase():
    work = installation_file()
    expected = run(work).installation
    assert expected.raise_height_um == (5_000_000, 5_000_000)
    assert expected.raise_states == ("fits", "fits")
    work["room"]["size_um"][2] -= 1
    report = run(work)
    assert report.installation.state == "fits"
    assert report.installation.raise_states == ("blocked", "blocked")
    assert report.installation.margin_xyz_um == (0, 0, 999_999)
    assert report.physical_cutting_authorized is False
    work["room"]["size_um"][0] -= 1
    assert run(work).installation.state == "blocked"


def test_removed_corner_changes_the_two_tipping_directions():
    work = installation_file()
    # A solid L section has a 3-4-5 diagonal in only one tipping direction.
    work["parts"] = [
        member("bottom", [900_000, 3_000_000, 1_000_000], [0, 0, 0]),
        member("post", [900_000, 1_000_000, 3_000_000], [0, 0, 1_000_000]),
    ]
    result = run(work).installation
    assert sorted(result.raise_height_um) == [4_123_106, 5_000_000]
    for part in work["parts"]:
        part["origin_um"] = [v + 170_000 for v in part["origin_um"]]
    assert run(work).installation == result


def test_missing_and_local_parts_cannot_make_room_checks_complete():
    work = installation_file()
    work["parts"].append(member("door", [500_000, 20_000, 700_000], None))
    review = run(work).installation
    assert review.state == "unresolved" and review.raise_states == ("unresolved", "unresolved")
    assert review.known_parts == 1 and review.unresolved_parts == ("door",)
    work["parts"][1].update(origin_um=[0, 0, 0], frame_id="door-frame")
    assert run(work).installation.state == "unresolved"
    work["parts"][0]["origin_um"] = None
    result = run(work).installation
    assert result.extent_xyz_um is None and result.raise_height_um is None


def test_legacy_identity_and_explicit_room_reset_are_preserved():
    work = installation_file()
    del work["room"]
    report = run(work)
    old = report.assembly.model_dump(mode="json", exclude={"room", "backing"})
    assert report.assembly_sha256 == content_hash(old)
    work["room"] = None
    assert run(work).assembly_sha256 == report.assembly_sha256
    assert report.installation is None


def test_stock_is_unknown_until_supplied_and_respects_long_grain_direction():
    work = installation_file(height=18_000, depth=200_000)
    assert run(work).stock_checks[0].state == "unresolved"
    part = work["parts"][0]
    part["stock_lbt_um"] = [900_000, 200_000, 18_000]
    assert run(work).stock_checks[0].margin_lbt_um == (0, 0, 0)
    assert run(work).stock_checks[0].state == "fits"
    part["stock_lbt_um"] = [200_000, 900_000, 18_000]
    assert run(work).stock_checks[0].state == "blocked"
    part["stock_lbt_um"] = [900_000, 200_000, 17_999]
    assert run(work).stock_checks[0].state == "blocked"


@pytest.mark.parametrize("fault", [0, -1, 20_000_001, 5_000_000.1, True])
def test_room_dimensions_require_bounded_integer_micrometres(fault):
    work = installation_file()
    work["room"]["size_um"][2] = fault
    with pytest.raises(ValidationError):
        run(work)


def test_rear_screw_insertion_and_missing_tool_dimensions_are_reported():
    work = pair("screw")
    # Turn the vertical screw pair onto its back face.
    for part in work["parts"]:
        x, y, z = part["origin_um"]
        dx, dy, dz = part["size_um"]
        part["origin_um"] = [x, -z - dz, y]
        part["size_um"] = [dx, dz, dy]
        for op in part["operations"]:
            op["face"] = {"bottom": "back", "top": "front"}[op["face"]]
            op["axes"] = "xz"
    work["bearings"] = []
    baseline = run(work)
    assert baseline.joints[0].state == "aligned"
    ys = [(p["origin_um"][1], p["origin_um"][1] + p["size_um"][1]) for p in work["parts"]]
    total_depth = max(b for a, b in ys) - min(a for a, b in ys)
    length = work["joints"][0]["length_um"]
    work["room"] = {
        "size_um": [
            1_000_000,
            total_depth + length + work["joints"][0]["head_allowance_um"] - 1,
            1_000_000,
        ],
        "source": "Prov",
    }
    access = run(work).installation.rear_access[0]
    assert access.state == "blocked" and access.gap_um == length - 1
    work["room"]["size_um"][1] += 1
    assert run(work).installation.rear_access[0].state == "unresolved"
    work["room"]["rear_tool_depth_um"] = 80_000
    assert run(work).installation.rear_access[0].state == "blocked"
    work["room"]["size_um"][1] += 80_000
    assert run(work).installation.rear_access[0].state == "fits"
