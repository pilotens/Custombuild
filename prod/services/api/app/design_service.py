from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from custombuild_domain import (
    WEIGHT_BASIS,
    BackPanelType,
    BookcaseDesignSpec,
    BookcaseParameters,
    JointType,
    PartRole,
    ReinforcementMode,
    ShelfMount,
    WallAnchorSpec,
    build_bookcase,
    mm,
    screening_birch_plywood_6,
    screening_birch_plywood_18,
    screening_mdf_6,
    screening_mdf_18,
)


def _millimetres(value: Any) -> int:
    return mm(Decimal(str(value)))


def _load_newtons(kg: Any) -> int:
    newtons = Decimal(str(kg)) * Decimal("9.80665")
    return int(newtons.quantize(Decimal("1"), ROUND_HALF_UP))


def _back_panel(value: Any) -> BackPanelType:
    if value is True:
        return BackPanelType.INSET_GROOVE
    if value is False or value is None:
        return BackPanelType.NONE
    return BackPanelType(str(value))


def normalize_preview(
    payload: dict[str, Any], *, design_id: str = "preview", revision: int = 1
) -> BookcaseDesignSpec:
    if payload.get("wall_anchor_verified"):
        raise ValueError(
            "Client-supplied wall-anchor verification is not accepted; "
            "use a server-bound approval catalogue"
        )

    material_name = str(payload.get("material_id", payload.get("material", "mdf"))).lower()
    material = (
        screening_birch_plywood_18()
        if material_name in {"birch", "birch-plywood", "plywood", "bjorkplywood", "björkplywood"}
        else screening_mdf_18()
    )
    nominal_um = _millimetres(payload.get("nominal_thickness_mm", 18))
    actual_um = _millimetres(
        payload.get("measured_thickness_mm", payload.get("material_thickness_mm", 18))
    )
    if nominal_um != material.nominal_thickness_um:
        raise ValueError("MVP material catalogue currently supports nominal 18 mm sheet material")

    back_panel = _back_panel(payload.get("back_panel", True))
    parameters = BookcaseParameters(
        width_um=_millimetres(payload.get("width_mm", 900)),
        height_um=_millimetres(payload.get("height_mm", 2000)),
        depth_um=_millimetres(payload.get("depth_mm", 320)),
        nominal_thickness_um=nominal_um,
        actual_thickness_um=actual_um,
        shelf_count=int(payload.get("shelf_count", 5)),
        shelf_mount=ShelfMount(str(payload.get("shelf_mount", "fixed"))),
        shelf_load_n=_load_newtons(payload.get("load_per_shelf_kg", 30)),
        vertical_divider_count=int(
            payload.get("divider_count", payload.get("vertical_divider_count", 0))
        ),
        back_panel=back_panel,
        plinth_height_um=_millimetres(
            payload.get("plinth_height_mm", 80 if payload.get("plinth", True) else 0)
        ),
        edge_band_thickness_um=_millimetres(payload.get("edge_band_mm", 1)),
        joint_system=JointType(str(payload.get("joint_system", "dado"))),
        reinforcement_mode=ReinforcementMode(str(payload.get("reinforcement_mode", "manual"))),
        wall_anchor=WallAnchorSpec(
            required=bool(payload.get("wall_anchor_required", False)), verified=False
        ),
    )
    return BookcaseDesignSpec(
        design_id=design_id,
        revision=revision,
        parameters=parameters,
        material=material,
        back_material=(
            None
            if back_panel == BackPanelType.NONE
            else (
                screening_birch_plywood_6()
                if material.material_id == "birch-plywood"
                else screening_mdf_6()
            )
        ),
    )


def _mm_value(value_um: int) -> float:
    return value_um / 1000


def _json_value(item: Any) -> Any:
    return item.model_dump(mode="json") if hasattr(item, "model_dump") else item


def _present_part(part: Any) -> dict[str, Any]:
    size = part.finished_size
    placement = part.placement
    if part.role in {PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE, PartRole.DIVIDER}:
        orientation = "YZ"
        width_um, depth_um, thickness_um = size.height_um, size.depth_um, size.width_um
        kind = "side" if part.role in {PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE} else "divider"
    elif part.role in {PartRole.BACK, PartRole.PLINTH}:
        orientation = "XZ"
        width_um, depth_um, thickness_um = size.width_um, size.height_um, size.depth_um
        kind = part.role.value
    else:
        orientation = "XY"
        width_um, depth_um, thickness_um = size.width_um, size.depth_um, size.height_um
        kind = part.role.value
    return {
        "part_id": part.part_id,
        "name": part.semantic_key,
        "kind": kind,
        "width_mm": _mm_value(width_um),
        "depth_mm": _mm_value(depth_um),
        "thickness_mm": _mm_value(thickness_um),
        "position_mm": {
            "x": _mm_value(placement.x_um + size.width_um // 2),
            "y": _mm_value(placement.y_um + size.depth_um // 2),
            "z": _mm_value(placement.z_um + size.height_um // 2),
        },
        "orientation": orientation,
        "material_id": part.material_id,
        "size_um": size.model_dump(mode="json"),
        "placement_um": placement.model_dump(mode="json"),
        "weight_g": part.weight_g,
        "weight_kg": part.weight_g / 1000,
        "weight_basis": WEIGHT_BASIS,
        "features": [feature.model_dump(mode="json") for feature in part.features],
    }


def present_design(
    result: Any,
    evaluations: list[Any] | tuple[Any, ...] = (),
    change_diff: list[Any] | tuple[Any, ...] = (),
) -> dict[str, Any]:
    parts = [_present_part(part) for part in result.parts]
    evaluation_json = [_json_value(item) for item in evaluations]
    statuses = [str(item.get("status", "PASS")).upper() for item in evaluation_json]
    overall = "BLOCK" if "BLOCK" in statuses else "WARNING" if "WARNING" in statuses else "PASS"
    return {
        "design_hash": result.design_hash,
        "engine_version": result.engine_version,
        "template_version": result.template_version,
        "spec": result.spec.model_dump(mode="json"),
        "parts": parts,
        "joints": [joint.model_dump(mode="json") for joint in result.joints],
        "assembly_steps": [step.model_dump(mode="json") for step in result.assembly_graph.steps],
        "total_weight_kg": result.total_weight_g / 1000,
        "weight_basis": WEIGHT_BASIS,
        "rule_evaluations": evaluation_json,
        "status": overall,
        "change_diff": [_json_value(item) for item in change_diff],
    }


def preview(
    payload: dict[str, Any], *, design_id: str = "preview", revision: int = 1
) -> tuple[BookcaseDesignSpec, Any, dict[str, Any]]:
    spec = normalize_preview(payload, design_id=design_id, revision=revision)
    result = build_bookcase(spec)
    try:
        from custombuild_rules import evaluate_design

        evaluations = evaluate_design(result).evaluations
    except ImportError:
        evaluations = ()
    return spec, result, present_design(result, evaluations)


def auto_fix(
    payload: dict[str, Any], *, design_id: str = "preview", revision: int = 1
) -> tuple[BookcaseDesignSpec, Any, dict[str, Any]]:
    spec = normalize_preview(payload, design_id=design_id, revision=revision)
    try:
        from custombuild_rules import auto_correct_design

        correction = auto_correct_design(spec)
        return (
            correction.corrected_spec,
            correction.corrected_design,
            present_design(
                correction.corrected_design,
                correction.final_report.evaluations,
                correction.diffs,
            ),
        )
    except ImportError:
        result = build_bookcase(spec)
        return spec, result, present_design(result)
