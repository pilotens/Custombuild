from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal
from importlib import import_module
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
from custombuild_manufacturing import (
    DFM_GRAIN_RULE,
    DeterministicNester,
    MachineProfile,
    Severity,
    StockSheet,
    canonical_json_bytes,
    generation_plan_artifact,
    grain_control_projection,
    stock_grain_binding_issues,
    stock_profile_missing_issue,
    stock_selection_artifact,
)
from custombuild_manufacturing.adapters import adapt_design_result, adapt_domain_part

_DEFAULT_PRODUCTION_CONTEXT: dict[str, float | int] = {
    "stock_width_mm": 2440.0,
    "stock_height_mm": 1220.0,
    "stock_count": 4,
    "back_stock_width_mm": 2440.0,
    "back_stock_height_mm": 1220.0,
    "back_stock_count": 2,
}


class RuleEngineUnavailable(RuntimeError):
    """Raised when construction screening cannot be loaded safely."""


def load_rule_engine() -> tuple[Any, Any, str]:
    """Resolve and validate the mandatory rule-engine API.

    Keeping this import late lets the health endpoint explain a broken runtime,
    while every design operation still fails closed instead of returning an
    empty evaluation list that could be mistaken for PASS.
    """

    try:
        module = import_module("custombuild_rules")
        evaluate_design = module.evaluate_design
        auto_correct_design = module.auto_correct_design
        rules_version = module.RULES_VERSION
    except (ImportError, AttributeError) as exc:
        raise RuleEngineUnavailable(
            "RULE_ENGINE_UNAVAILABLE: construction screening could not be loaded"
        ) from exc
    if not callable(evaluate_design) or not callable(auto_correct_design):
        raise RuleEngineUnavailable(
            "RULE_ENGINE_UNAVAILABLE: construction screening API is invalid"
        )
    if not isinstance(rules_version, str) or not rules_version.strip():
        raise RuleEngineUnavailable("RULE_ENGINE_UNAVAILABLE: construction rule version is missing")
    return evaluate_design, auto_correct_design, rules_version


def assert_rule_engine_available() -> None:
    load_rule_engine()


def _millimetres(value: Any) -> int:
    return mm(Decimal(str(value)))


def _load_newtons(kg: Any) -> int:
    newtons = Decimal(str(kg)) * Decimal("9.80665")
    return int(newtons.quantize(Decimal("1"), ROUND_HALF_UP))


def _ratios_ppm(values: Any) -> tuple[int, ...]:
    if not isinstance(values, list | tuple) or not values:
        return ()
    decimals = tuple(Decimal(str(value)) for value in values)
    total = sum(decimals)
    if total <= 0:
        return ()
    return tuple(
        max(1, int(((value / total) * Decimal(1_000_000)).quantize(Decimal("1"), ROUND_HALF_UP)))
        for value in decimals
    )


def _positions_ppm(values: Any) -> tuple[int, ...]:
    if not isinstance(values, list | tuple) or not values:
        return ()
    return tuple(
        int((Decimal(str(value)) * Decimal(1_000_000)).quantize(Decimal("1"), ROUND_HALF_UP))
        for value in values
    )


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
    requested_back_material = payload.get("back_material_id")
    if requested_back_material not in {None, "mdf-6", "birch-plywood-6"}:
        raise ValueError("unknown back_material_id")
    if back_panel == BackPanelType.NONE and requested_back_material is not None:
        raise ValueError("back_material_id requires an enabled back panel")
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
        bay_width_ratios_ppm=_ratios_ppm(payload.get("bay_width_ratios", ())),
        shelf_height_ratios_ppm=_positions_ppm(payload.get("shelf_height_ratios", ())),
        base_cabinet_height_um=_millimetres(payload.get("base_cabinet_height_mm", 0)),
        base_cabinet_depth_um=_millimetres(payload.get("base_cabinet_depth_mm", 0)),
        base_cabinet_count=int(payload.get("base_cabinet_count", 0)),
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
    parameters.assert_furniture_family(str(payload.get("furniture_type", "bookcase")))
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
                if requested_back_material == "birch-plywood-6"
                or (
                    requested_back_material is None
                    and material.material_id == "birch-plywood"
                )
                else screening_mdf_6()
            )
        ),
    )


def _mm_value(value_um: int) -> float:
    return value_um / 1000


def _json_value(item: Any) -> Any:
    return item.model_dump(mode="json") if hasattr(item, "model_dump") else item


def _canonical_json_value(value: Any) -> Any:
    """Return JSON-native data with the manufacturing canonicalizer as authority."""

    return json.loads(canonical_json_bytes(value))


def _production_number(
    production_context: Mapping[str, Any] | None,
    field: str,
) -> float:
    raw = (
        production_context.get(field, _DEFAULT_PRODUCTION_CONTEXT[field])
        if production_context is not None
        else _DEFAULT_PRODUCTION_CONTEXT[field]
    )
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ValueError(f"{field} must be a finite positive number")
    value = Decimal(str(raw))
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{field} must be a finite positive number")
    return float(value)


def _stock_selection_for_design(
    result: Any,
    production_context: Mapping[str, Any] | None = None,
) -> tuple[tuple[StockSheet, ...], tuple[tuple[StockSheet, tuple[Any, ...]], ...], tuple[str, ...]]:
    """Rebuild the worker's deterministic stock candidates and part assignment."""

    parts = adapt_design_result(result).parts
    spec = result.spec
    stock_groups: list[tuple[str, str, int, str, str]] = [
        (
            spec.material.material_id,
            spec.material.version,
            spec.parameters.actual_thickness_um,
            "stock",
            "carcass",
        )
    ]
    if spec.back_material is not None:
        stock_groups.append(
            (
                spec.back_material.material_id,
                spec.back_material.version,
                spec.parameters.back_thickness_um,
                "back_stock",
                "back",
            )
        )

    stocks: list[StockSheet] = []
    for material_id, material_version, thickness_um, prefix, role in stock_groups:
        width_mm = _production_number(production_context, f"{prefix}_width_mm")
        height_mm = _production_number(production_context, f"{prefix}_height_mm")
        quantity = _production_number(production_context, f"{prefix}_count")
        if not quantity.is_integer():
            raise ValueError(f"{prefix}_count must be an integer")
        width_um = int(round(width_mm * 1000))
        height_um = int(round(height_mm * 1000))
        stocks.append(
            StockSheet(
                stock_id=(
                    f"stock-{role}-{material_id}-{material_version}-{thickness_um}um-"
                    f"{width_um}x{height_um}um"
                ),
                material_id=material_id,
                material_version=material_version,
                width_um=width_um,
                height_um=height_um,
                thickness_um=thickness_um,
                quantity=int(quantity),
                grain_direction="UNBOUND",
            )
        )
    stock_values = tuple(stocks)
    if len({stock.stock_id for stock in stock_values}) != len(stock_values):
        raise ValueError("frozen stock roles produced duplicate stock identifiers")

    grouped: dict[str, list[Any]] = {}
    stock_by_id = {stock.stock_id: stock for stock in stock_values}
    unmatched_part_ids: list[str] = []
    nester = DeterministicNester()
    for part in sorted(parts, key=lambda item: item.part_id):
        compatible = [
            stock
            for stock in stock_values
            if stock.material_id == part.material_id
            and stock.material_version == part.material_version
            and stock.thickness_um == part.thickness_um
            and nester.nest(
                (replace(part, quantity=1, grain_direction="NONE"),),
                stock,
            ).is_complete
        ]
        if not compatible:
            unmatched_part_ids.append(part.part_id)
            continue
        selected = min(
            compatible,
            key=lambda stock: (stock.width_um * stock.height_um, stock.stock_id),
        )
        grouped.setdefault(selected.stock_id, []).append(part)
    grouped_values = tuple(
        (stock_by_id[stock_id], tuple(grouped[stock_id])) for stock_id in sorted(grouped)
    )
    return stock_values, grouped_values, tuple(sorted(unmatched_part_ids))


def stock_selection_snapshot_for_design(
    result: Any,
    production_context: Mapping[str, Any] | None = None,
) -> bytes:
    """Return the canonical v1 stock-selection document for a frozen design."""

    stocks, grouped_parts, unmatched_part_ids = _stock_selection_for_design(
        result,
        production_context,
    )
    return stock_selection_artifact(
        stocks,
        grouped_parts,
        unmatched_part_ids=unmatched_part_ids,
    ).data


def generation_plan_snapshot_for_design(
    result: Any,
    production_context: Mapping[str, Any],
    *,
    machine: MachineProfile,
    validation_program_requested: bool,
) -> bytes:
    """Return the canonical worker generation plan for frozen request inputs."""

    stocks, _, _ = _stock_selection_for_design(result, production_context)
    return generation_plan_artifact(
        machine=machine,
        stocks=stocks,
        two_sided_registration_by_stock=None,
        validation_program_requested=validation_program_requested,
    ).data


def stock_missing_issues_for_design(
    result: Any,
    production_context: Mapping[str, Any] | None = None,
) -> tuple[Any, ...]:
    """Return canonical missing-stock issues from the deterministic assignment."""

    _, _, unmatched_part_ids = _stock_selection_for_design(result, production_context)
    parts_by_id = {part.part_id: part for part in adapt_design_result(result).parts}
    if any(part_id not in parts_by_id for part_id in unmatched_part_ids):
        raise ValueError("stock selection references a part outside the frozen design")
    return tuple(
        stock_profile_missing_issue(parts_by_id[part_id]) for part_id in unmatched_part_ids
    )


def stock_grain_issues_for_design(
    result: Any,
    production_context: Mapping[str, Any] | None = None,
    *,
    severity: Severity = Severity.WARNING,
) -> tuple[Any, ...]:
    """Assess grain binding using the exact deterministic stock assignment."""

    _, grouped_parts, _ = _stock_selection_for_design(result, production_context)
    return tuple(
        issue
        for stock, current_parts in grouped_parts
        for issue in stock_grain_binding_issues(
            current_parts,
            stock,
            severity=severity,
        )
    )


def preview_grain_issues_for_design(result: Any) -> tuple[Any, ...]:
    """Project missing information before any stock-match claim has been made."""

    parts = tuple(adapt_domain_part(part) for part in result.parts)
    return stock_grain_binding_issues(
        parts,
        None,
        severity=Severity.WARNING,
    )


def grain_rule_evaluation(projection: Mapping[str, Any]) -> dict[str, Any]:
    affected_part_ids = tuple(str(item) for item in projection["affected_part_ids"])
    raw_issues = projection.get("issues", ())
    stock_ids = tuple(
        sorted(
            str(item.get("stock_id"))
            for item in raw_issues
            if isinstance(item, Mapping) and item.get("stock_id") is not None
        )
    )
    return {
        "rule_id": DFM_GRAIN_RULE.rule_id,
        "rule_version": DFM_GRAIN_RULE.rule_version,
        "title": DFM_GRAIN_RULE.title,
        "status": str(projection["status"]),
        "applies_to_part_ids": list(affected_part_ids),
        "inputs": [
            {"name": "binding_status", "value": "MISSING_INFORMATION", "unit": None},
            {
                "name": "affected_part_count",
                "value": len(affected_part_ids),
                "unit": "parts",
            },
            {
                "name": "stock_ids",
                "value": ", ".join(stock_ids),
                "unit": None,
            },
        ],
        "assumptions": list(projection["assumptions"]),
        "trace": [
            {
                "expression": "structured stock-grain axis",
                "result": "MISSING_INFORMATION",
                "unit": None,
            }
        ],
        "calculated_value": len(affected_part_ids),
        "allowed_value": 0,
        "unit": "parts",
        "safety_margin_permille": -1000,
        "suggested_actions": [],
        "manufacturing_control": _canonical_json_value(projection),
    }


def _present_part(part: Any) -> dict[str, Any]:
    size = part.finished_size
    placement = part.placement
    if part.role in {
        PartRole.LEFT_SIDE,
        PartRole.RIGHT_SIDE,
        PartRole.DIVIDER,
        PartRole.BASE_SIDE,
    }:
        orientation = "YZ"
        width_um, depth_um, thickness_um = size.height_um, size.depth_um, size.width_um
        kind = "side" if part.role in {PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE} else part.role.value
    elif part.role in {PartRole.BACK, PartRole.PLINTH, PartRole.CABINET_FRONT}:
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
    grain_issues = preview_grain_issues_for_design(result)
    grain_projection = grain_control_projection(grain_issues)
    if grain_projection is not None:
        evaluation_json.append(grain_rule_evaluation(grain_projection))
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
        "manufacturing_controls": (
            [] if grain_projection is None else [_canonical_json_value(grain_projection)]
        ),
        "status": overall,
        "change_diff": [_json_value(item) for item in change_diff],
    }


def preview(
    payload: dict[str, Any], *, design_id: str = "preview", revision: int = 1
) -> tuple[BookcaseDesignSpec, Any, dict[str, Any]]:
    spec = normalize_preview(payload, design_id=design_id, revision=revision)
    result = build_bookcase(spec)
    evaluate_design, _, _ = load_rule_engine()
    evaluations = evaluate_design(result).evaluations
    return spec, result, present_design(result, evaluations)


def auto_fix(
    payload: dict[str, Any], *, design_id: str = "preview", revision: int = 1
) -> tuple[BookcaseDesignSpec, Any, dict[str, Any]]:
    spec = normalize_preview(payload, design_id=design_id, revision=revision)
    _, auto_correct_design, _ = load_rule_engine()
    correction = auto_correct_design(spec)
    correction.corrected_spec.parameters.assert_furniture_family(
        str(payload.get("furniture_type", "bookcase"))
    )
    return (
        correction.corrected_spec,
        correction.corrected_design,
        present_design(
            correction.corrected_design,
            correction.final_report.evaluations,
            correction.diffs,
        ),
    )


def canonical_preview(
    payload: dict[str, Any], *, design_id: str = "preview", revision: int = 1
) -> tuple[BookcaseDesignSpec, Any, dict[str, Any]]:
    """Resolve the exact canonical model used by the active workspace mode."""

    resolver = auto_fix if payload.get("reinforcement_mode") == "auto" else preview
    return resolver(payload, design_id=design_id, revision=revision)
