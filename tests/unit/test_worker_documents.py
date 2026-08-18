from __future__ import annotations

import csv
import io
import json
from types import SimpleNamespace

import pytest
from custombuild_domain import (
    BookcaseDesignSpec,
    BookcaseParameters,
    ShelfMount,
    build_bookcase,
    mm,
    screening_mdf_6,
    screening_mdf_18,
)
from custombuild_manufacturing import canonical_json_bytes
from custombuild_rules import evaluate_design
from custombuild_worker.documents import (
    ASSEMBLY_PARTS_PER_PAGE,
    _assembly_group_plan,
    _assembly_manual_plan,
    _assembly_part_chunks,
    _assembly_step_hardware,
    _assembly_step_hardware_text,
    _direction_vector,
    _exploded_boxes,
    _label_qr_payload,
    _rule_threshold_label,
    _two_person_lift_parts,
    _unresolved_front_hardware_part_ids,
    assembly_manual_pdf,
    assembly_readiness_json,
    bom_pdf,
    hardware_csv,
    labels_pdf,
    qa_protocol_pdf,
    validation_report_pdf,
)


def design_fixture():
    return build_bookcase(
        BookcaseDesignSpec(
            design_id="pdf-fixture",
            parameters=BookcaseParameters(),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )


def wall_library_fixture():
    return build_bookcase(
        BookcaseDesignSpec(
            design_id="wall-library-pdf-fixture",
            parameters=BookcaseParameters(
                width_um=mm(2_400),
                height_um=mm(2_400),
                depth_um=mm(340),
                vertical_divider_count=2,
                base_cabinet_height_um=mm(600),
                base_cabinet_depth_um=mm(340),
                base_cabinet_count=3,
            ),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )


def test_production_pdfs_are_real_and_deterministic() -> None:
    design = design_fixture()
    documents = (
        bom_pdf(design),
        assembly_manual_pdf(design),
        labels_pdf(design),
        qa_protocol_pdf(design),
        validation_report_pdf(evaluate_design(design)),
    )
    repeated = (
        bom_pdf(design),
        assembly_manual_pdf(design),
        labels_pdf(design),
        qa_protocol_pdf(design),
        validation_report_pdf(evaluate_design(design)),
    )
    assert documents == repeated
    assert all(payload.startswith(b"%PDF-") and len(payload) > 1_000 for payload in documents)


def test_construction_report_is_canonical_json_serializable() -> None:
    report = evaluate_design(design_fixture())

    first = canonical_json_bytes(report)
    second = canonical_json_bytes(report)
    decoded = json.loads(first)

    assert first == second
    assert decoded["design_hash"] == report.design_hash
    assert decoded["overall_status"] == report.overall_status.value
    assert decoded["evaluations"][0]["rule_id"].startswith("CB-")


def test_label_qr_payload_binds_design_and_exact_instance() -> None:
    assert _label_qr_payload("d" * 64, "part-123") == (f"custombuild:part:{'d' * 64}:part-123:001")


def test_hardware_list_is_derived_from_joint_graph() -> None:
    design = design_fixture()

    first = hardware_csv(design)
    second = hardware_csv(design)

    assert first == second
    assert first.startswith(
        b"hardware_sku,quantity,source_joint_ids,affected_part_ids,"
        b"selection_status,required_action\n"
    )
    for joint in design.joints:
        if joint.hardware_sku:
            assert joint.hardware_sku.encode("utf-8") in first
            assert joint.joint_id.encode("utf-8") in first
    assert b"CATALOG_IDENTIFIED" not in first


def test_hardware_list_marks_fronts_without_hardware_as_not_mountable() -> None:
    design = wall_library_fixture()
    payload = hardware_csv(design).decode("utf-8")

    assert "EXTERNAL_SELECTION_REQUIRED" in payload
    assert "fronts are not mountable until then" in payload
    for part in design.parts:
        if str(getattr(part.role, "value", part.role)) == "cabinet_front":
            assert part.part_id in payload


@pytest.mark.parametrize("sku", ("construction-adhesive", "hinge-unversioned"))
def test_free_form_hardware_identifier_never_makes_a_front_mountable(sku: str) -> None:
    front_id = "cabinet-front-1"
    joint_id = "unverified-front-joint"
    design = SimpleNamespace(
        parts=(SimpleNamespace(part_id=front_id, role="cabinet_front"),),
        joints=(
            SimpleNamespace(
                joint_id=joint_id,
                hardware_sku=sku,
                hardware_count=2,
                members=(
                    SimpleNamespace(part_id=front_id),
                    SimpleNamespace(part_id="side-1"),
                ),
            ),
        ),
    )
    step = SimpleNamespace(joint_ids=(joint_id,), part_ids=(front_id, "side-1"))

    rows = tuple(csv.DictReader(io.StringIO(hardware_csv(design).decode("utf-8"))))
    identifier_row = next(row for row in rows if row["hardware_sku"] == sku)
    unresolved_row = next(row for row in rows if not row["hardware_sku"])

    assert identifier_row["selection_status"] == "UNVERIFIED_IDENTIFIER"
    assert "adhesives are prohibited" in identifier_row["required_action"]
    assert unresolved_row["selection_status"] == "EXTERNAL_SELECTION_REQUIRED"
    assert front_id in _unresolved_front_hardware_part_ids(design)
    assert _assembly_step_hardware_text(design, step).startswith("FRONT EJ MONTERINGSBAR")


def test_manual_hardware_text_never_claims_unsecured_geometry_is_complete() -> None:
    design = wall_library_fixture()
    by_id = {part.part_id: part for part in design.parts}
    front_step = next(
        step
        for step in design.assembly_graph.steps
        if any(
            str(getattr(by_id[part_id].role, "value", by_id[part_id].role)) == "cabinet_front"
            for part_id in step.part_ids
        )
    )
    dado_step = next(step for step in design.assembly_graph.steps if step.joint_ids)

    assert _assembly_step_hardware_text(design, front_step).startswith("FRONT EJ MONTERINGSBAR")
    assert "inte verifierad som permanent låsning" in _assembly_step_hardware_text(
        design, dado_step
    )


def test_validation_report_threshold_labels_have_real_direction() -> None:
    evaluations = {item.rule_id: item for item in evaluate_design(design_fixture()).evaluations}

    assert _rule_threshold_label(evaluations["CB-DEFLECTION-001"]) == "högsta tillåtna"
    assert _rule_threshold_label(evaluations["CB-TIP-001"]) == "minimikrav"
    assert _rule_threshold_label(evaluations["CB-JOINT-001"]) == ("verifierad lokal kapacitet")


def test_manual_geometry_preserves_real_sizes_and_uses_step_direction() -> None:
    design = design_fixture()
    part_by_id = {part.part_id: part for part in design.parts}
    step = next(
        step
        for step in design.assembly_graph.steps
        if any(part_by_id[part_id].semantic_key == "right-side" for part_id in step.moving_part_ids)
    )
    parts = tuple(part_by_id[part_id] for part_id in step.part_ids)

    boxes = _exploded_boxes(parts, frozenset(step.moving_part_ids), step.motion_path)

    assert len(boxes) == len(parts)
    offsets: list[tuple[int, int, int]] = []
    direction = _direction_vector(step.direction)
    for box, part in zip(boxes, parts, strict=True):
        x0, y0, z0, x1, y1, z1 = box.bounds_um
        assert (x1 - x0, y1 - y0, z1 - z0) == (
            part.finished_size.width_um,
            part.finished_size.depth_um,
            part.finished_size.height_um,
        )
        offset = (
            x0 - part.placement.x_um,
            y0 - part.placement.y_um,
            z0 - part.placement.z_um,
        )
        if part.part_id in step.moving_part_ids:
            offsets.append(offset)
            assert (
                sum(delta * component for delta, component in zip(offset, direction, strict=True))
                < 0
            )
        else:
            assert offset == (0, 0, 0)
    assert len(set(offsets)) == 1

    right_side = next(part for part in parts if part.semantic_key == "right-side")
    right_box = next(box for box in boxes if box.part_id == right_side.part_id)
    assert step.direction.value == "-x"
    assert right_box.bounds_um[0] > right_side.placement.x_um


def test_manual_step_hardware_and_lift_warnings_are_graph_derived() -> None:
    adjustable = build_bookcase(
        BookcaseDesignSpec(
            design_id="manual-hardware-fixture",
            parameters=BookcaseParameters(shelf_mount=ShelfMount.ADJUSTABLE),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    shelf_step = next(
        step
        for step in adjustable.assembly_graph.steps
        if any(
            joint.joint_id in step.joint_ids and joint.hardware_sku == "shelf-pin-5"
            for joint in adjustable.joints
        )
    )

    assert _assembly_step_hardware(adjustable, shelf_step) == (("shelf-pin-5", 4),)
    assert _assembly_step_hardware_text(adjustable, shelf_step).startswith("EJ VERIFIERAT BESLAG")
    assert tuple(segment.value for segment in shelf_step.motion_path) == ("+y", "-z")
    shelf_part_by_id = {part.part_id: part for part in adjustable.parts}
    shelf_parts = tuple(shelf_part_by_id[part_id] for part_id in shelf_step.part_ids)
    shelf_boxes = _exploded_boxes(
        shelf_parts,
        frozenset(shelf_step.moving_part_ids),
        shelf_step.motion_path,
    )
    moving_shelf = next(part for part in shelf_parts if part.part_id in shelf_step.moving_part_ids)
    moving_box = next(box for box in shelf_boxes if box.part_id == moving_shelf.part_id)
    assert moving_box.bounds_um[1] < moving_shelf.placement.y_um
    assert moving_box.bounds_um[2] > moving_shelf.placement.z_um
    by_id = {part.part_id: part for part in adjustable.parts}
    side_step = next(
        step
        for step in adjustable.assembly_graph.steps
        if any(by_id[part_id].semantic_key == "left-side" for part_id in step.part_ids)
    )
    assert any(
        by_id[part_id].semantic_key == "left-side"
        for part_id in _two_person_lift_parts(
            tuple(by_id[part_id] for part_id in side_step.part_ids)
        )
    )


def test_manual_plans_exact_moving_group_mass_extent_people_and_unknown_tools() -> None:
    design = design_fixture()
    by_id = {part.part_id: part for part in design.parts}
    plans = {
        step.step_id: _assembly_group_plan(by_id, step) for step in design.assembly_graph.steps
    }

    assert set(plans) == {step.step_id for step in design.assembly_graph.steps}
    for step in design.assembly_graph.steps:
        plan = plans[step.step_id]
        moving = tuple(by_id[part_id] for part_id in step.moving_part_ids)
        assert plan.moving_part_ids == tuple(sorted(step.moving_part_ids))
        assert plan.weight_g == sum(part.weight_g for part in moving)
        x0, y0, z0, x1, y1, z1 = plan.bounds_um
        assert plan.max_dimension_um == max(x1 - x0, y1 - y0, z1 - z0)
        assert plan.minimum_people_floor in {1, 2}
        assert plan.requires_external_lift_plan == (
            plan.weight_g >= 20_000 or plan.max_dimension_um >= 1_800_000
        )

    manual = _assembly_manual_plan(design)
    assert manual.groups == tuple(plans[step.step_id] for step in design.assembly_graph.steps)
    assert set(manual.tool_ids) == {
        tool_id for step in design.assembly_graph.steps for tool_id in step.tool_ids
    }
    assert "panel-positioning-jig" in manual.unresolved_tool_ids
    assert manual.requires_external_work_preparation is True


def test_assembly_readiness_is_machine_readable_and_fail_closed() -> None:
    design = design_fixture()

    payload = json.loads(assembly_readiness_json(design))

    assert payload["schema_version"] == "custombuild.assembly-readiness.v1"
    assert payload["design_hash"] == design.design_hash
    assert payload["release_scope"] == "design_review"
    assert payload["customer_assembly_authorized"] is False
    assert payload["physical_assembly_authorized"] is False
    assert payload["requires_external_work_preparation"] is True
    assert "named_assembly_safety_approver" in payload["missing_requirements"]
    assert "verified_adhesive_free_joint_retention" in payload["missing_requirements"]
    assert len(payload["groups"]) == len(design.assembly_graph.steps)
    assert any(item.startswith("approved_lift_plan:") for item in payload["missing_requirements"])
    assert any(
        tool["tool_id"] == "panel-positioning-jig"
        and tool["specification_status"] == "EXTERNAL_SPECIFICATION_REQUIRED"
        for tool in payload["tools"]
    )


def test_large_assembly_steps_are_paginated_without_losing_parts() -> None:
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="manual-pagination-fixture",
            parameters=BookcaseParameters(
                width_um=2_400_000,
                height_um=1_200_000,
                depth_um=400_000,
                shelf_count=8,
                vertical_divider_count=7,
            ),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    largest_step = max(design.assembly_graph.steps, key=lambda item: len(item.part_ids))

    chunks = _assembly_part_chunks(largest_step.part_ids)
    payload = assembly_manual_pdf(design)

    assert len(largest_step.part_ids) > ASSEMBLY_PARTS_PER_PAGE
    assert all(0 < len(chunk) <= ASSEMBLY_PARTS_PER_PAGE for chunk in chunks)
    assert tuple(part_id for chunk in chunks for part_id in chunk) == largest_step.part_ids
    assert payload.startswith(b"%PDF-")
    assert len(payload) > 10_000
