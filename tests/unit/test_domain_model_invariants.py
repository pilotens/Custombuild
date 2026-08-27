from __future__ import annotations

import pytest
from custombuild_domain import (
    AssemblyGraph,
    BackPanelType,
    BookcaseDesignSpec,
    BookcaseParameters,
    DesignResult,
    MaterialVersion,
    build_bookcase,
    screening_birch_plywood_18,
    screening_mdf_6,
    screening_mdf_18,
)
from pydantic import ValidationError


@pytest.mark.parametrize(
    "changes, message",
    [
        (
            {"min_supported_thickness_um": 20_000, "max_supported_thickness_um": 10_000},
            "range is inverted",
        ),
        (
            {"nominal_thickness_um": 5_000},
            "nominal thickness is outside",
        ),
        (
            {"property_uncertainty_permille": 901},
            "uncertainty must be at most",
        ),
    ],
)
def test_material_catalog_rejects_invalid_screening_ranges(
    changes: dict[str, int],
    message: str,
) -> None:
    payload = screening_birch_plywood_18().model_dump(mode="python")
    payload.update(changes)

    with pytest.raises(ValidationError, match=message):
        MaterialVersion.model_validate(payload)


def assembly_graph_payload() -> dict[str, object]:
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="assembly-invariant-fixture",
            parameters=BookcaseParameters(),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    return design.assembly_graph.model_dump(mode="python")


def test_design_without_back_panel_rejects_a_back_material() -> None:
    with pytest.raises(ValidationError, match="back material is forbidden"):
        BookcaseDesignSpec(
            design_id="no-back-material-invariant",
            parameters=BookcaseParameters(back_panel=BackPanelType.NONE),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (("engine_version", "0.6.0"), ("template_version", "1.1.0")),
)
def test_design_spec_rejects_retired_compiler_versions(field: str, value: str) -> None:
    payload = {
        "design_id": "retired-compiler-invariant",
        "parameters": BookcaseParameters(),
        "material": screening_mdf_18(),
        "back_material": screening_mdf_6(),
        field: value,
    }

    with pytest.raises(ValidationError, match="retired; create new revision"):
        BookcaseDesignSpec.model_validate(payload)


def design_result_payload() -> dict[str, object]:
    return build_bookcase(
        BookcaseDesignSpec(
            design_id="design-result-invariant-fixture",
            parameters=BookcaseParameters(),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    ).model_dump(mode="python")


def test_design_result_binds_joint_features_to_their_declared_member() -> None:
    payload = design_result_payload()
    joints = payload["joints"]
    parts = payload["parts"]
    assert isinstance(joints, tuple)
    assert isinstance(parts, tuple)
    first_joint = joints[0]
    cut_member = first_joint["members"][0]
    foreign_feature = next(
        feature
        for part in parts
        if part["part_id"] != cut_member["part_id"]
        for feature in part["features"]
    )
    cut_member["feature_ids"] = (foreign_feature["feature_id"],)

    with pytest.raises(ValidationError, match="another part's feature"):
        DesignResult.model_validate(payload)


def test_design_result_rejects_orphaned_joint_owned_feature() -> None:
    payload = design_result_payload()
    joints = payload["joints"]
    assert isinstance(joints, tuple)
    first_joint = joints[0]
    cut_member = first_joint["members"][0]
    assert cut_member["feature_ids"]
    cut_member["feature_ids"] = ()

    with pytest.raises(ValidationError, match="absent from its joint"):
        DesignResult.model_validate(payload)


def test_assembly_graph_rejects_a_joint_installed_twice() -> None:
    payload = assembly_graph_payload()
    steps = payload["steps"]
    assert isinstance(steps, tuple)
    first_joint_id = steps[0]["joint_ids"][0]
    steps[1]["joint_ids"] = (*steps[1]["joint_ids"], first_joint_id)

    with pytest.raises(ValidationError, match="exactly once"):
        AssemblyGraph.model_validate(payload)


def test_assembly_graph_rejects_an_uninstalled_joint() -> None:
    payload = assembly_graph_payload()
    steps = payload["steps"]
    assert isinstance(steps, tuple)
    steps[0]["joint_ids"] = ()

    with pytest.raises(ValidationError, match="install every joint"):
        AssemblyGraph.model_validate(payload)


def test_assembly_graph_rejects_a_bom_part_missing_from_all_steps() -> None:
    payload = assembly_graph_payload()
    steps = payload["steps"]
    nodes = payload["nodes"]
    assert isinstance(steps, tuple)
    assert isinstance(nodes, tuple)
    moving_part_ids = {
        part_id for step in steps for part_id in step["moving_part_ids"]
    }
    omitted_part_id = next(
        node["part_id"] for node in nodes if node["part_id"] not in moving_part_ids
    )
    for step in steps:
        step["part_ids"] = tuple(
            part_id for part_id in step["part_ids"] if part_id != omitted_part_id
        )

    with pytest.raises(ValidationError, match="every part"):
        AssemblyGraph.model_validate(payload)


def test_assembly_step_rejects_duplicate_or_external_moving_parts() -> None:
    duplicate_payload = assembly_graph_payload()
    duplicate_steps = duplicate_payload["steps"]
    assert isinstance(duplicate_steps, tuple)
    moving_id = duplicate_steps[0]["moving_part_ids"][0]
    duplicate_steps[0]["moving_part_ids"] = (moving_id, moving_id)
    with pytest.raises(ValidationError, match="duplicate parts"):
        AssemblyGraph.model_validate(duplicate_payload)

    external_payload = assembly_graph_payload()
    external_steps = external_payload["steps"]
    assert isinstance(external_steps, tuple)
    external_steps[0]["moving_part_ids"] = ("par_not-in-this-step",)
    with pytest.raises(ValidationError, match="contained in the step"):
        AssemblyGraph.model_validate(external_payload)


def test_assembly_step_final_direction_must_match_motion_path() -> None:
    payload = assembly_graph_payload()
    steps = payload["steps"]
    assert isinstance(steps, tuple)
    steps[0]["motion_path"] = ("+x",)

    with pytest.raises(ValidationError, match="final motion-path"):
        AssemblyGraph.model_validate(payload)
