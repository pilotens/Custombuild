from copy import deepcopy

import pytest
from custombuild_domain.measured_assembly import MeasuredAssembly, review_measured_assembly
from custombuild_domain.measured_backing import Backing
from pydantic import ValidationError

from tests.product_acceptance.measured import member


def backing_file():
    parts = [
        member("left", [60_000, 6_000, 50_000], [0, 10_000, 0]),
        member("right", [45_000, 6_000, 50_000], [55_000, 10_000, 0]),
    ]
    # Overlapping projections in distinct depth layers, as for a rebated rear.
    parts[1]["origin_um"][1] = 16_000
    return {
        "schema_version": "custombuild.measured-assembly.v1",
        "title": "Bakvägg",
        "source": "Prov",
        "parts": parts,
        "backing": {
            "part_ids": ["left", "right"],
            "openings": [
                {
                    "id": "low",
                    "label": "Nedre rygg",
                    "target": "covered",
                    "low_um": [0, 10_000, 0],
                    "high_um": [100_000, 22_000, 50_000],
                },
                {
                    "id": "upper",
                    "label": "Övre rygg",
                    "target": "open",
                    "low_um": [0, 10_000, 50_000],
                    "high_um": [100_000, 22_000, 100_000],
                },
            ],
        },
    }


def review(data):
    return review_measured_assembly(MeasuredAssembly.model_validate(data))


def test_partial_lapped_sheets_do_not_double_count_and_loss_only_exposes_unique_material():
    data = backing_file()
    low, high = review(data).back_coverage
    assert low.state == high.state == "met"
    assert low.covered_area_um2 == low.required_area_um2 == 5_000_000_000
    assert low.exposure_on_removal_um2 == {"left": 2_750_000_000, "right": 2_000_000_000}
    assert high.covered_area_um2 == 0
    data["parts"][1]["origin_um"][2] += 10_000
    low, high = review(data).back_coverage
    assert low.state == high.state == "mismatch"
    assert low.covered_area_um2 == 4_600_000_000 and high.covered_area_um2 == 450_000_000


def test_notch_changes_net_back_coverage_and_panel_outside_depth_does_not_cover():
    data = backing_file()
    part = data["parts"][0]
    part["operations"] = [
        dict(
            id="opening",
            shape="notch",
            face="back",
            axes="xz",
            u_um=20_000,
            v_um=20_000,
            width_um=10_000,
            height_um=20_000,
            depth_um=6_000,
            through=True,
            source="Prov",
        )
    ]
    before = review(data)
    assert before.back_coverage[0].covered_area_um2 == 4_800_000_000
    data["parts"][1]["origin_um"][1] = 22_000
    shifted = review(data)
    assert shifted.back_coverage[0].covered_area_um2 == 2_800_000_000
    data["parts"][1]["origin_um"][1] = 16_000
    assert review(data) == before


@pytest.mark.parametrize("fault", ["missing", "isolated", "local"])
def test_unplaced_or_removed_back_is_unresolved_without_inventing_coverage(fault):
    data = backing_file()
    if fault == "missing":
        data["parts"].pop()
    elif fault == "isolated":
        data["parts"][1]["origin_um"] = None
    else:
        data["parts"][1]["frame_id"] = "door"
    r = review(data)
    assert r.unresolved_back_parts == ("right",)
    assert (
        r.back_coverage[0].state == "unresolved"
        and r.back_coverage[0].covered_area_um2 == 3_000_000_000
    )


def test_invalid_opening_and_duplicate_members_are_rejected_and_legacy_identity_is_stable():
    data = backing_file()
    good = deepcopy(data["backing"])
    for change in ("depth", "duplicate", "float"):
        obj = deepcopy(good)
        if change == "depth":
            obj["openings"][0]["high_um"][1] = 1
        elif change == "duplicate":
            obj["part_ids"].append("left")
        else:
            obj["openings"][0]["high_um"][0] = 123.1
        with pytest.raises(ValidationError):
            Backing.model_validate(obj)
    del data["backing"]
    old = review(data)
    data["backing"] = None
    assert review(data) == old and not old.back_coverage
