from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from custombuild_domain import (
    AssemblyGraph,
    BackPanelType,
    BookcaseDesignSpec,
    BookcaseParameters,
    DesignResult,
    FeatureDimensions,
    Joint,
    ManufacturingFeature,
    PartInstance,
    build_bookcase,
    mm,
    screening_mdf_6,
    screening_mdf_18,
)
from pydantic import ValidationError


def _valid_result_payload() -> dict[str, Any]:
    return build_bookcase(
        BookcaseDesignSpec(
            design_id="fail-closed-invariant-fixture",
            parameters=BookcaseParameters(),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    ).model_dump(mode="python")


def _valid_part_payload() -> dict[str, Any]:
    parts = _valid_result_payload()["parts"]
    return next(part for part in parts if part["features"])


def _valid_feature_payload() -> dict[str, Any]:
    return _valid_part_payload()["features"][0]


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        (
            {"width_um": mm(250), "actual_thickness_um": mm(130)},
            "width must exceed two side thicknesses",
        ),
        (
            {"height_um": mm(300), "plinth_height_um": mm(280)},
            "height leaves no usable internal opening",
        ),
        (
            {"depth_um": mm(100), "actual_thickness_um": mm(110)},
            "depth must exceed material thickness",
        ),
        (
            {"vertical_divider_count": 1, "bay_width_ratios_ppm": (1_000_000,)},
            "bay width ratios must match",
        ),
        (
            {
                "vertical_divider_count": 1,
                "bay_width_ratios_ppm": (1, 999_999),
            },
            "every custom bay must be at least 8 percent",
        ),
        (
            {"shelf_count": 2, "shelf_height_ratios_ppm": (500_000,)},
            "shelf height ratios must match",
        ),
        (
            {"shelf_count": 2, "shelf_height_ratios_ppm": (500_000, 540_000)},
            "custom shelf centres must be ordered",
        ),
        (
            {
                "width_um": mm(250),
                "vertical_divider_count": 3,
                "shelf_side_clearance_um": mm(5),
            },
            "unmanufacturable shelf width",
        ),
        (
            {
                "base_cabinet_count": 1,
                "base_cabinet_height_um": mm(299),
                "base_cabinet_depth_um": mm(320),
            },
            "base cabinet height must be at least 300 mm",
        ),
        (
            {
                "height_um": mm(1_000),
                "shelf_count": 0,
                "base_cabinet_count": 1,
                "base_cabinet_height_um": mm(800),
                "base_cabinet_depth_um": mm(320),
            },
            "base cabinet leaves no usable upper shelving zone",
        ),
        (
            {
                "width_um": mm(450),
                "base_cabinet_count": 2,
                "base_cabinet_height_um": mm(300),
                "base_cabinet_depth_um": mm(320),
            },
            "unmanufacturable module width",
        ),
        (
            {"base_cabinet_count": 0, "base_cabinet_height_um": mm(300)},
            "base cabinet dimensions require",
        ),
        (
            {"depth_um": mm(100), "back_thickness_um": mm(90)},
            "depth is insufficient for the inset back-panel groove",
        ),
        (
            {"structural_safety_factor_permille": 999},
            "structural safety factor must be between",
        ),
    ),
)
def test_bookcase_geometry_rejects_unsafe_but_field_valid_combinations(
    changes: dict[str, object],
    message: str,
) -> None:
    payload = BookcaseParameters().model_dump(mode="python")
    payload.update(changes)

    with pytest.raises(ValidationError, match=message):
        BookcaseParameters.model_validate(payload)


@pytest.mark.parametrize(
    ("changes", "family", "message"),
    (
        (
            {
                "base_cabinet_count": 1,
                "base_cabinet_height_um": mm(300),
                "base_cabinet_depth_um": mm(320),
            },
            "bookcase",
            "bookcase furniture cannot contain base cabinets",
        ),
        (
            {
                "base_cabinet_count": 1,
                "base_cabinet_height_um": mm(299),
                "base_cabinet_depth_um": mm(320),
            },
            "wall_library",
            "base cabinet height must be at least 300 mm",
        ),
        (
            {
                "base_cabinet_count": 1,
                "base_cabinet_height_um": mm(300),
                "base_cabinet_depth_um": mm(319),
            },
            "wall_library",
            "base cabinet depth must equal furniture depth",
        ),
        (
            {
                "height_um": mm(1_000),
                "base_cabinet_count": 1,
                "base_cabinet_height_um": mm(800),
                "base_cabinet_depth_um": mm(320),
            },
            "wall_library",
            "leaves no usable upper shelving zone",
        ),
    ),
)
def test_furniture_family_gate_rechecks_invariants_even_for_internal_models(
    changes: dict[str, object],
    family: str,
    message: str,
) -> None:
    # Internal callers can receive already-constructed models. The family gate
    # remains fail-closed even when normal field validation was not the creator.
    payload = BookcaseParameters().model_dump(mode="python")
    payload.update(changes)
    parameters = BookcaseParameters.model_construct(**payload)

    with pytest.raises(ValueError, match=message):
        parameters.assert_furniture_family(family)


def test_internal_back_catalog_sentinel_cannot_disable_positive_thickness() -> None:
    payload = BookcaseParameters().model_dump(mode="python")
    payload.update(back_panel=BackPanelType.NONE, back_thickness_um=0)
    parameters = BookcaseParameters.model_construct(**payload)

    with pytest.raises(ValueError, match="back thickness must remain a positive"):
        parameters.validate_geometry()


def test_feature_dimension_and_pattern_contracts_fail_closed() -> None:
    with pytest.raises(ValidationError, match="must have a dimension"):
        FeatureDimensions()

    missing_pitch = _valid_feature_payload()
    missing_pitch.update(pattern_count=2, pitch_um=None)
    with pytest.raises(ValidationError, match="multiple items needs a pitch"):
        ManufacturingFeature.model_validate(missing_pitch)

    duplicate_reliefs = _valid_feature_payload()
    duplicate_reliefs.update(
        open_end_reliefs=("u_min", "u_min"),
        corner_strategy="dogbone-v1",
    )
    with pytest.raises(ValidationError, match="relief declarations must be unique"):
        ManufacturingFeature.model_validate(duplicate_reliefs)

    unversioned_relief = _valid_feature_payload()
    unversioned_relief.update(open_end_reliefs=("u_min",), corner_strategy=None)
    with pytest.raises(ValidationError, match="require the versioned dogbone-v1"):
        ManufacturingFeature.model_validate(unversioned_relief)


def test_part_and_joint_ownership_invariants_reject_corrupt_collections() -> None:
    foreign_feature = _valid_part_payload()
    foreign_feature["features"][0]["part_id"] = "par_foreign-owner"
    with pytest.raises(ValidationError, match="feature belonging to another part"):
        PartInstance.model_validate(foreign_feature)

    duplicate_feature = _valid_part_payload()
    duplicate_feature["features"] = (
        duplicate_feature["features"][0],
        duplicate_feature["features"][0],
    )
    with pytest.raises(ValidationError, match="duplicate feature IDs"):
        PartInstance.model_validate(duplicate_feature)

    joint_payload = _valid_result_payload()["joints"][0]
    joint_payload["members"][1]["part_id"] = joint_payload["members"][0]["part_id"]
    with pytest.raises(ValidationError, match="connect two distinct part"):
        Joint.model_validate(joint_payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda payload: payload.__setitem__(
                "nodes", (payload["nodes"][0], payload["nodes"][0], *payload["nodes"][2:])
            ),
            "duplicate nodes",
        ),
        (
            lambda payload: payload.__setitem__(
                "edges", (payload["edges"][0], payload["edges"][0], *payload["edges"][2:])
            ),
            "duplicate joint edges",
        ),
        (
            lambda payload: payload["edges"][0].__setitem__(
                "from_part_id", "par_missing-node"
            ),
            "edge references a missing part",
        ),
        (
            lambda payload: payload["steps"][0].__setitem__("step_number", 2),
            "steps must be sequential and one-based",
        ),
        (
            lambda payload: payload["steps"][0].__setitem__(
                "part_ids", (*payload["steps"][0]["part_ids"], "par_missing-step")
            ),
            "step references a missing part",
        ),
        (
            lambda payload: payload["steps"][0].__setitem__(
                "joint_ids", ("jnt_missing-edge",)
            ),
            "step references a missing joint",
        ),
    ),
)
def test_assembly_graph_rejects_corrupt_identity_and_sequence_references(
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    payload = _valid_result_payload()["assembly_graph"]
    mutate(payload)

    with pytest.raises(ValidationError, match=message):
        AssemblyGraph.model_validate(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda payload: payload.__setitem__(
                "parts", (payload["parts"][0], payload["parts"][0], *payload["parts"][2:])
            ),
            "duplicate part IDs",
        ),
        (
            lambda payload: payload.__setitem__(
                "joints",
                (payload["joints"][0], payload["joints"][0], *payload["joints"][2:]),
            ),
            "duplicate joint IDs",
        ),
        (
            lambda payload: payload.__setitem__(
                "parts", payload["parts"][:-1]
            ),
            "assembly graph and part collection differ",
        ),
        (
            lambda payload: payload.__setitem__(
                "joints", payload["joints"][:-1]
            ),
            "assembly graph and joint collection differ",
        ),
        (
            lambda payload: payload["joints"][0]["members"][0].__setitem__(
                "part_id", "par_joint-missing"
            ),
            "joint references a missing part",
        ),
        (
            lambda payload: payload["joints"][0]["members"][0].__setitem__(
                "feature_ids", ("fea_missing-feature",)
            ),
            "joint references a missing manufacturing feature",
        ),
        (
            lambda payload: payload.__setitem__(
                "total_weight_g", payload["total_weight_g"] + 1
            ),
            "total design weight does not equal",
        ),
    ),
)
def test_design_result_rejects_corrupt_top_level_identity_bindings(
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    payload = _valid_result_payload()
    mutate(payload)

    with pytest.raises(ValidationError, match=message):
        DesignResult.model_validate(payload)


def test_design_result_rejects_duplicate_and_unknown_feature_ownership() -> None:
    duplicate_payload = _valid_result_payload()
    featured_parts = [part for part in duplicate_payload["parts"] if part["features"]]
    first_feature = featured_parts[0]["features"][0]
    second_feature = featured_parts[1]["features"][0]
    second_feature["feature_id"] = first_feature["feature_id"]
    with pytest.raises(ValidationError, match="duplicate manufacturing feature IDs"):
        DesignResult.model_validate(duplicate_payload)

    unknown_joint_payload = _valid_result_payload()
    part = next(part for part in unknown_joint_payload["parts"] if part["features"])
    unreferenced_feature = {
        **part["features"][0],
        "feature_id": "fea_unknown-joint",
        "joint_id": "jnt_unknown-joint",
    }
    part["features"] = (*part["features"], unreferenced_feature)
    with pytest.raises(ValidationError, match="feature references a missing joint"):
        DesignResult.model_validate(unknown_joint_payload)
