from __future__ import annotations

from custombuild_domain import (
    BookcaseDesignSpec,
    BookcaseParameters,
    ShelfMount,
    build_bookcase,
    screening_mdf_6,
    screening_mdf_18,
)
from custombuild_rules import evaluate_design
from custombuild_worker.documents import (
    ASSEMBLY_PARTS_PER_PAGE,
    _assembly_part_chunks,
    _assembly_step_hardware,
    _direction_vector,
    _exploded_boxes,
    _two_person_lift_parts,
    assembly_manual_pdf,
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


def test_hardware_list_is_derived_from_joint_graph() -> None:
    design = design_fixture()

    first = hardware_csv(design)
    second = hardware_csv(design)

    assert first == second
    assert first.startswith(b"hardware_sku,quantity,source_joint_ids\n")
    for joint in design.joints:
        if joint.hardware_sku:
            assert joint.hardware_sku.encode("utf-8") in first
            assert joint.joint_id.encode("utf-8") in first


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
                sum(
                    delta * component
                    for delta, component in zip(offset, direction, strict=True)
                )
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
    assert tuple(segment.value for segment in shelf_step.motion_path) == ("+y", "-z")
    shelf_part_by_id = {part.part_id: part for part in adjustable.parts}
    shelf_parts = tuple(shelf_part_by_id[part_id] for part_id in shelf_step.part_ids)
    shelf_boxes = _exploded_boxes(
        shelf_parts,
        frozenset(shelf_step.moving_part_ids),
        shelf_step.motion_path,
    )
    moving_shelf = next(
        part for part in shelf_parts if part.part_id in shelf_step.moving_part_ids
    )
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
