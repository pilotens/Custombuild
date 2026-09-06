from __future__ import annotations

import pytest
from custombuild_domain import (
    BackPanelType,
    BookcaseDesignSpec,
    BookcaseParameters,
    DesignResult,
    Joint,
    JointRetentionContract,
    JointRetentionLoadCase,
    JointRetentionLoadMode,
    JointRetentionMachiningScope,
    JointRetentionMaterialIdentity,
    JointRetentionMethod,
    JointType,
    PartInstance,
    PartRole,
    build_bookcase,
    dado_joint_geometry_fingerprint,
    mm,
    screening_mdf_6,
    screening_mdf_18,
)
from custombuild_domain.models import (
    JointRetentionApplicationClass,
    captive_inset_back_topology_is_complete,
)
from custombuild_manufacturing import (
    BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    back_panel_retention_evidence_missing,
    blocked_design_review_package_status,
    dado_retention_evidence_missing,
    retention_evidence_blocker_code,
    validate_design_review_status_retention_binding,
)
from custombuild_manufacturing.pipeline import _retention_blocker_code
from pydantic import ValidationError


def _base_spec(
    *,
    design_id: str,
    back_panel: BackPanelType = BackPanelType.INSET_GROOVE,
    vertical_divider_count: int = 0,
) -> BookcaseDesignSpec:
    return BookcaseDesignSpec(
        design_id=design_id,
        parameters=BookcaseParameters(
            back_panel=back_panel,
            vertical_divider_count=vertical_divider_count,
        ),
        material=screening_mdf_18(),
        back_material=(
            screening_mdf_6() if back_panel != BackPanelType.NONE else None
        ),
    )


def _bind_test_contract(spec: BookcaseDesignSpec) -> DesignResult:
    base = build_bookcase(spec)
    contract = JointRetentionContract(
        system_id="test-only.carcass-retention",
        system_version="v1",
        application_class=(
            JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
        ),
        method=JointRetentionMethod.MECHANICAL,
        catalog_entry_sha256="1" * 64,
        evidence_id="test-only.signed-evidence",
        evidence_sha256="2" * 64,
        installation_instruction_id="test-only.instructions",
        installation_instruction_version="v1",
        installation_instruction_sha256="3" * 64,
        machining_scope=JointRetentionMachiningScope.NO_ADDITIONAL_CNC,
        hardware_sku="test-only.fastener",
        hardware_count_per_joint=2,
        applicable_materials=(
            JointRetentionMaterialIdentity(
                material_id=spec.material.material_id,
                material_version=spec.material.version,
            ),
        ),
        joint_geometry_sha256=dado_joint_geometry_fingerprint(
            base.parts,
            base.joints,
        ),
        minimum_applicable_thickness_um=mm(17),
        maximum_applicable_thickness_um=mm(19),
        load_cases=(
            JointRetentionLoadCase(
                mode=JointRetentionLoadMode.SHEAR,
                rated_design_load_n=300,
                verified_capacity_n=600,
            ),
            JointRetentionLoadCase(
                mode=JointRetentionLoadMode.WITHDRAWAL,
                rated_design_load_n=50,
                verified_capacity_n=100,
            ),
        ),
        safety_factor_permille=1_800,
    )
    return build_bookcase(spec.model_copy(update={"joint_retention": contract}))


def _single_back_capture(
    *,
    design_id: str,
) -> tuple[DesignResult, PartInstance, Joint]:
    design = build_bookcase(_base_spec(design_id=design_id))
    back = next(part for part in design.parts if part.role == PartRole.BACK)
    joint = next(
        item
        for item in design.joints
        if item.retention_application_class
        == JointRetentionApplicationClass.CAPTIVE_INSET_BACK_GROOVE
        and back.part_id in {member.part_id for member in item.members}
    )
    return design, back, joint


def test_capture_proof_rejects_absent_back_part() -> None:
    design = build_bookcase(
        _base_spec(
            design_id="capture-without-back",
            back_panel=BackPanelType.NONE,
        )
    )

    assert not captive_inset_back_topology_is_complete(
        design.parts,
        design.joints,
        design.assembly_graph,
    )


def test_capture_proof_rejects_a_back_joint_missing_from_the_assembly_steps() -> None:
    design, _back, back_joint = _single_back_capture(
        design_id="capture-missing-assembly-step"
    )
    steps = tuple(
        step.model_copy(
            update={
                "joint_ids": tuple(
                    joint_id
                    for joint_id in step.joint_ids
                    if joint_id != back_joint.joint_id
                )
            }
        )
        for step in design.assembly_graph.steps
    )
    graph = design.assembly_graph.model_copy(update={"steps": steps})

    assert not captive_inset_back_topology_is_complete(
        design.parts,
        design.joints,
        graph,
    )


def test_capture_proof_rejects_a_cut_feature_on_the_back_member() -> None:
    design, back, back_joint = _single_back_capture(
        design_id="capture-feature-on-back"
    )
    boundary_member = next(
        member for member in back_joint.members if member.part_id != back.part_id
    )
    feature_id = boundary_member.feature_ids[0]
    members = tuple(
        member.model_copy(update={"feature_ids": (feature_id,)})
        if member.part_id == back.part_id
        else member
        for member in back_joint.members
    )
    joints = tuple(
        joint.model_copy(update={"members": members})
        if joint.joint_id == back_joint.joint_id
        else joint
        for joint in design.joints
    )

    assert not captive_inset_back_topology_is_complete(
        design.parts,
        joints,
        design.assembly_graph,
    )


def test_capture_proof_rejects_a_groove_with_unbound_fit_clearance() -> None:
    design, back, back_joint = _single_back_capture(
        design_id="capture-unbound-fit-clearance"
    )
    boundary_member = next(
        member for member in back_joint.members if member.part_id != back.part_id
    )
    feature_id = boundary_member.feature_ids[0]
    parts = tuple(
        part.model_copy(
            update={
                "features": tuple(
                    feature.model_copy(
                        update={"fit_clearance_um": feature.fit_clearance_um + 1}
                    )
                    if feature.feature_id == feature_id
                    else feature
                    for feature in part.features
                )
            }
        )
        for part in design.parts
    )

    assert not captive_inset_back_topology_is_complete(
        parts,
        design.joints,
        design.assembly_graph,
    )


def test_capture_proof_rejects_duplicate_back_boundary_faces() -> None:
    design, back, back_joint = _single_back_capture(
        design_id="capture-duplicate-boundary-face"
    )
    back_member = next(
        member for member in back_joint.members if member.part_id == back.part_id
    )
    other_back_member = next(
        member
        for joint in design.joints
        if joint.joint_id != back_joint.joint_id
        and joint.retention_application_class
        == JointRetentionApplicationClass.CAPTIVE_INSET_BACK_GROOVE
        for member in joint.members
        if member.part_id == back.part_id
        and member.mating_face != back_member.mating_face
    )
    members = tuple(
        member.model_copy(update={"mating_face": other_back_member.mating_face})
        if member.part_id == back.part_id
        else member
        for member in back_joint.members
    )
    joints = tuple(
        joint.model_copy(update={"members": members})
        if joint.joint_id == back_joint.joint_id
        else joint
        for joint in design.joints
    )

    assert not captive_inset_back_topology_is_complete(
        design.parts,
        joints,
        design.assembly_graph,
    )


def test_capture_proof_requires_top_and_bottom_boundary_roles() -> None:
    design, back, _back_joint = _single_back_capture(
        design_id="capture-missing-top-role"
    )
    boundary_ids = {
        member.part_id
        for joint in design.joints
        if joint.retention_application_class
        == JointRetentionApplicationClass.CAPTIVE_INSET_BACK_GROOVE
        and back.part_id in {item.part_id for item in joint.members}
        for member in joint.members
        if member.part_id != back.part_id
    }
    top = next(
        part
        for part in design.parts
        if part.part_id in boundary_ids and part.role == PartRole.TOP
    )
    parts = tuple(
        part.model_copy(update={"role": PartRole.LEFT_SIDE})
        if part.part_id == top.part_id
        else part
        for part in design.parts
    )

    assert not captive_inset_back_topology_is_complete(
        parts,
        design.joints,
        design.assembly_graph,
    )


def test_capture_proof_rejects_an_unsupported_boundary_role() -> None:
    design, back, _back_joint = _single_back_capture(
        design_id="capture-unsupported-boundary-role"
    )
    boundary_ids = {
        member.part_id
        for joint in design.joints
        if joint.retention_application_class
        == JointRetentionApplicationClass.CAPTIVE_INSET_BACK_GROOVE
        and back.part_id in {item.part_id for item in joint.members}
        for member in joint.members
        if member.part_id != back.part_id
    }
    side = next(
        part
        for part in design.parts
        if part.part_id in boundary_ids and part.role == PartRole.LEFT_SIDE
    )
    parts = tuple(
        part.model_copy(update={"role": PartRole.SHELF})
        if part.part_id == side.part_id
        else part
        for part in design.parts
    )

    assert not captive_inset_back_topology_is_complete(
        parts,
        design.joints,
        design.assembly_graph,
    )


def test_dado_joint_requires_an_explicit_retention_application_class() -> None:
    design = build_bookcase(_base_spec(design_id="unclassified-dado"))
    joint = next(
        item
        for item in design.joints
        if item.retention_application_class
        == JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
    )
    payload = joint.model_dump(mode="python")
    payload["retention_application_class"] = None

    with pytest.raises(
        ValidationError,
        match="every DADO must declare its retention application class",
    ):
        Joint.model_validate(payload)


def test_non_dado_joint_rejects_a_retention_application_class() -> None:
    design = build_bookcase(
        _base_spec(
            design_id="classified-rabbet",
            back_panel=BackPanelType.SURFACE_MOUNTED,
        )
    )
    joint = next(item for item in design.joints if item.joint_type == JointType.RABBET)
    payload = joint.model_dump(mode="python")
    payload["retention_application_class"] = (
        JointRetentionApplicationClass.CAPTIVE_INSET_BACK_GROOVE
    )

    with pytest.raises(
        ValidationError,
        match="only DADO joints may declare a retention application class",
    ):
        Joint.model_validate(payload)


def test_captive_back_joint_rejects_carcass_hardware() -> None:
    _design, _back, joint = _single_back_capture(
        design_id="capture-with-carcass-hardware"
    )
    payload = joint.model_dump(mode="python")
    payload.update(hardware_sku="test-only.intruding-fastener", hardware_count=1)

    with pytest.raises(
        ValidationError,
        match="captive inset-back groove cannot inherit carcass retention hardware",
    ):
        Joint.model_validate(payload)


def test_retention_contract_rejects_the_captive_back_application_class() -> None:
    design = _bind_test_contract(_base_spec(design_id="misclassified-contract"))
    contract = design.spec.joint_retention
    assert contract is not None
    payload = contract.model_dump(mode="python")
    payload["application_class"] = (
        JointRetentionApplicationClass.CAPTIVE_INSET_BACK_GROOVE
    )

    with pytest.raises(
        ValidationError,
        match="current retention contract only covers load-bearing carcass DADOs",
    ):
        JointRetentionContract.model_validate(payload)


def test_design_result_rechecks_missing_load_bearing_retention_after_copy() -> None:
    design = _bind_test_contract(_base_spec(design_id="removed-load-retention"))
    joint = next(
        item
        for item in design.joints
        if item.retention_application_class
        == JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
    )
    joints = tuple(
        item.model_copy(update={"retention": None})
        if item.joint_id == joint.joint_id
        else item
        for item in design.joints
    )
    tampered = design.model_copy(update={"joints": joints})

    with pytest.raises(
        ValidationError,
        match="every load-bearing carcass DADO must carry the frozen retention contract",
    ):
        DesignResult.model_validate(tampered.model_dump(mode="python"))


def test_joint_rechecks_captive_retention_after_copy() -> None:
    design = _bind_test_contract(_base_spec(design_id="injected-captive-retention"))
    contract = design.spec.joint_retention
    assert contract is not None
    joint = next(
        item
        for item in design.joints
        if item.retention_application_class
        == JointRetentionApplicationClass.CAPTIVE_INSET_BACK_GROOVE
    )
    joints = tuple(
        item.model_copy(
            update={
                "retention": contract,
                "hardware_sku": contract.hardware_sku,
                "hardware_count": contract.hardware_count_per_joint,
            }
        )
        if item.joint_id == joint.joint_id
        else item
        for item in design.joints
    )
    tampered = design.model_copy(update={"joints": joints})

    with pytest.raises(
        ValidationError,
        match="joint retention must match its application class",
    ):
        DesignResult.model_validate(tampered.model_dump(mode="python"))


def test_design_result_rejects_a_reclassified_captive_joint() -> None:
    design, _back, back_joint = _single_back_capture(
        design_id="reclassified-captive-joint"
    )
    payload = design.model_dump(mode="python")
    joints = payload["joints"]
    assert isinstance(joints, tuple)
    joint_payload = next(
        item
        for item in joints
        if isinstance(item, dict) and item.get("joint_id") == back_joint.joint_id
    )
    joint_payload["retention_application_class"] = (
        JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
    )

    with pytest.raises(
        ValidationError,
        match="inset back panel is not proven captive on four independent boundaries",
    ):
        DesignResult.model_validate(payload)


def test_design_result_rejects_captive_joints_under_a_non_inset_spec() -> None:
    inset_design = build_bookcase(_base_spec(design_id="inset-joints-without-inset-spec"))
    no_back_design = build_bookcase(
        _base_spec(
            design_id="non-inset-spec-source",
            back_panel=BackPanelType.NONE,
        )
    )
    tampered = inset_design.model_copy(update={"spec": no_back_design.spec})

    with pytest.raises(
        ValidationError,
        match="captive inset-back grooves require an inset back panel",
    ):
        DesignResult.model_validate(tampered.model_dump(mode="python"))


def test_default_inset_back_uses_a_separate_proven_capture_application() -> None:
    design = _bind_test_contract(_base_spec(design_id="classified-default-back"))
    load_bearing = tuple(
        joint
        for joint in design.joints
        if joint.retention_application_class
        == JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
    )
    back_grooves = tuple(
        joint
        for joint in design.joints
        if joint.retention_application_class
        == JointRetentionApplicationClass.CAPTIVE_INSET_BACK_GROOVE
    )

    assert load_bearing
    assert len(back_grooves) == 4
    assert all(joint.retention == design.spec.joint_retention for joint in load_bearing)
    assert all(joint.retention is None for joint in back_grooves)
    assert captive_inset_back_topology_is_complete(
        design.parts,
        design.joints,
        design.assembly_graph,
    )
    assert dado_retention_evidence_missing(design) is False
    assert back_panel_retention_evidence_missing(design) is False
    assert retention_evidence_blocker_code(design) is None


def test_back_groove_changes_cannot_rewrite_the_carcass_evidence_scope() -> None:
    base = build_bookcase(_base_spec(design_id="fingerprint-scope"))
    initial = dado_joint_geometry_fingerprint(base.parts, base.joints)
    back_joint = next(
        joint
        for joint in base.joints
        if joint.retention_application_class
        == JointRetentionApplicationClass.CAPTIVE_INSET_BACK_GROOVE
    )
    changed_back = tuple(
        joint.model_copy(update={"tolerance_um": joint.tolerance_um + 1})
        if joint.joint_id == back_joint.joint_id
        else joint
        for joint in base.joints
    )
    load_joint = next(
        joint
        for joint in base.joints
        if joint.retention_application_class
        == JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
    )
    changed_load = tuple(
        joint.model_copy(update={"tolerance_um": joint.tolerance_um + 1})
        if joint.joint_id == load_joint.joint_id
        else joint
        for joint in base.joints
    )

    assert dado_joint_geometry_fingerprint(base.parts, changed_back) == initial
    assert dado_joint_geometry_fingerprint(base.parts, changed_load) != initial


def test_partial_or_single_movement_back_capture_fails_closed() -> None:
    design = build_bookcase(_base_spec(design_id="tampered-back-capture"))
    back_joint_ids = {
        joint.joint_id
        for joint in design.joints
        if joint.retention_application_class
        == JointRetentionApplicationClass.CAPTIVE_INSET_BACK_GROOVE
    }
    partial = tuple(
        joint for joint in design.joints if joint.joint_id != min(back_joint_ids)
    )
    assert not captive_inset_back_topology_is_complete(
        design.parts,
        partial,
        design.assembly_graph,
    )

    back_part_id = next(part.part_id for part in design.parts if part.role.value == "back")
    single_movement_steps = tuple(
        step.model_copy(update={"moving_part_ids": (back_part_id,)})
        if back_joint_ids & set(step.joint_ids)
        else step
        for step in design.assembly_graph.steps
    )
    single_movement_graph = design.assembly_graph.model_copy(
        update={"steps": single_movement_steps}
    )
    assert not captive_inset_back_topology_is_complete(
        design.parts,
        design.joints,
        single_movement_graph,
    )


def test_middle_inset_back_accepts_later_same_axis_top_closure() -> None:
    design = build_bookcase(
        _base_spec(
            design_id="same-axis-middle-back-capture",
            vertical_divider_count=2,
        )
    )
    backs = sorted(
        (part for part in design.parts if part.role.value == "back"),
        key=lambda part: part.instance_index,
    )
    middle_back = backs[1]
    middle_joint_ids = {
        joint.joint_id
        for joint in design.joints
        if middle_back.part_id in {member.part_id for member in joint.members}
    }
    middle_steps = tuple(
        step
        for step in design.assembly_graph.steps
        if middle_joint_ids & set(step.joint_ids)
    )

    assert {step.direction.value for step in middle_steps} == {"-z"}
    assert captive_inset_back_topology_is_complete(
        design.parts,
        design.joints,
        design.assembly_graph,
    )


def test_inset_back_closure_before_insertion_fails_closed() -> None:
    design = build_bookcase(
        _base_spec(
            design_id="premature-middle-back-closure",
            vertical_divider_count=2,
        )
    )
    backs = sorted(
        (part for part in design.parts if part.role.value == "back"),
        key=lambda part: part.instance_index,
    )
    middle_back = backs[1]
    middle_joint_ids = {
        joint.joint_id
        for joint in design.joints
        if middle_back.part_id in {member.part_id for member in joint.members}
    }
    steps = list(design.assembly_graph.steps)
    insertion_index = next(
        index
        for index, step in enumerate(steps)
        if middle_back.part_id in step.moving_part_ids
        and middle_joint_ids & set(step.joint_ids)
    )
    closure_index = next(
        index
        for index, step in enumerate(steps)
        if middle_back.part_id not in step.moving_part_ids
        and middle_joint_ids & set(step.joint_ids)
    )
    steps[insertion_index], steps[closure_index] = (
        steps[closure_index],
        steps[insertion_index],
    )
    reordered_steps = tuple(
        step.model_copy(update={"step_number": index})
        for index, step in enumerate(steps, start=1)
    )
    reordered_graph = type(design.assembly_graph)(
        nodes=design.assembly_graph.nodes,
        edges=design.assembly_graph.edges,
        steps=reordered_steps,
    )

    assert not captive_inset_back_topology_is_complete(
        design.parts,
        design.joints,
        reordered_graph,
    )


def test_surface_back_remains_a_distinct_retention_blocker() -> None:
    design = _bind_test_contract(
        _base_spec(
            design_id="surface-back-blocker",
            back_panel=BackPanelType.SURFACE_MOUNTED,
        )
    )

    assert dado_retention_evidence_missing(design) is False
    assert back_panel_retention_evidence_missing(design) is True
    assert (
        retention_evidence_blocker_code(design)
        == BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE
    )
    assert _retention_blocker_code(design) == (
        BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE
    )
    status = blocked_design_review_package_status(
        (BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,)
    )
    validate_design_review_status_retention_binding(status, design)
