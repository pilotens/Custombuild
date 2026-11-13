from pathlib import Path

import pytest
from custombuild_domain.cabinet_doors import door_mounts
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import build_furniture
from custombuild_domain.models import DesignResult

EXAMPLE = Path("examples/furniture/bookcase-walnut-base-700.json")


def test_every_inset_leaf_matches_its_support_on_both_drill_faces():
    design = FurnitureWorkspace.model_validate_json(EXAMPLE.read_bytes()).design
    result = build_furniture(design)
    carcass = result.shelving_result
    mounts = door_mounts(result.parts, carcass.spec.parameters)
    assert [m.support.semantic_key for m in mounts] == [
        "left-side",
        "base-side-1",
        "base-side-2",
        "base-side-3",
        "base-side-4",
        "base-side-5",
    ]
    assert {m.door.finished_size.height_um for m in mounts} == {660000}
    assert {m.door.finished_size.width_um for m in mounts} == {688333, 688334}
    assert all(len(m.holes) == 15 for m in mounts)
    data = carcass.model_dump(mode="json")
    front = next(p for p in data["parts"] if p["semantic_key"] == "cabinet-front-0")
    front["features"].pop()
    with pytest.raises(ValueError, match="hål eller monteringsplatta"):
        DesignResult.model_validate(data)


@pytest.mark.parametrize(("width", "passes"), [(840_000, True), (840_001, False)])
def test_single_bay_800_mm_width_limit(width, passes):
    design = FurnitureWorkspace.model_validate_json(EXAMPLE.read_bytes()).design
    intent = design.intent.model_copy(update={"width_um": width, "divider_count": 0})
    spec = design.model_copy(update={"intent": intent, "installation": None})
    if passes:
        assert (
            len(
                door_mounts(
                    build_furniture(spec).parts,
                    build_furniture(spec).shelving_result.spec.parameters,
                )
            )
            == 1
        )
    else:
        with pytest.raises(ValueError, match="800 mm"):
            build_furniture(spec)


def test_unsupported_stock_and_door_system_are_rejected():
    data = FurnitureWorkspace.model_validate_json(EXAMPLE.read_bytes()).model_dump(mode="json")
    data["design"]["material"]["measured_thickness_um"] = 17999
    with pytest.raises(ValueError, match="18 mm"):
        build_furniture(FurnitureWorkspace.model_validate(data).design)
    data["design"]["intent"]["base_doors"] = "sliding"
    with pytest.raises(ValueError, match="J95"):
        FurnitureWorkspace.model_validate(data)
