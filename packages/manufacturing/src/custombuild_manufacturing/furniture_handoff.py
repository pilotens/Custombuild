"""Measurable workshop requirements for every generated furniture family."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from typing import Any

from custombuild_domain.furniture import ShelvingIntent
from custombuild_domain.furniture_dimensions import review_furniture_dimensions
from custombuild_domain.furniture_engine import FurnitureResult

from .adapters import adapt_design_result
from .model import FeatureKind

HANDOFF_VERSION = "furniture-workshop-handoff-1.0.0"


def furniture_stock_requirements(result: FurnitureResult) -> list[dict[str, Any]]:
    """Necessary raw-blank format bounds, not a nesting yield or order quantity."""
    domain_parts = {part.part_id: part for part in result.parts}
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for part in adapt_design_result(result).parts:
        width = part.raw_width_um or part.width_um
        height = part.raw_height_um or part.height_um
        directional = part.grain_direction != "NONE"
        along = width if part.grain_direction == "X" else height
        across = height if part.grain_direction == "X" else width
        source = domain_parts[part.part_id]
        grouped[(source.material_id, source.material_version, source.actual_thickness_um)].append(
            {
                "part_id": part.part_id,
                "semantic_key": source.semantic_key,
                "raw_width_um": width,
                "raw_height_um": height,
                "grain_direction": part.grain_direction,
                "along_grain_um": along if directional else None,
                "across_grain_um": across if directional else None,
            }
        )
    return [
        {
            "material_id": material,
            "material_version": version,
            "measured_thickness_um": thickness,
            "part_count": len(parts),
            "raw_area_um2": sum(p["raw_width_um"] * p["raw_height_um"] for p in parts),
            "minimum_long_edge_um": max(max(p["raw_width_um"], p["raw_height_um"]) for p in parts),
            "minimum_short_edge_um": max(min(p["raw_width_um"], p["raw_height_um"]) for p in parts),
            "minimum_along_grain_um": max((p["along_grain_um"] or 0 for p in parts), default=0),
            "minimum_across_grain_um": max((p["across_grain_um"] or 0 for p in parts), default=0),
            "parts": sorted(parts, key=lambda p: p["semantic_key"]),
        }
        for (material, version, thickness), parts in sorted(grouped.items())
    ]


def furniture_workshop_handoff(result: FurnitureResult) -> dict[str, Any]:
    hardware = result.hardware_profile
    intent = result.spec.intent
    return {
        "schema_version": "custombuild.furniture-workshop-handoff.v1",
        "version": HANDOFF_VERSION,
        "design_hash": result.design_hash,
        "family": result.spec.intent.family,
        "dimensions": review_furniture_dimensions(result.spec),
        "stock_requirements": furniture_stock_requirements(result),
        "shelf_load": {
            "basis": intent.shelf_load_basis,
            "total_row_load_n": intent.resolved_shelf_load_n,
            "load_per_metre_n": intent.shelf_load_per_metre_n
            if intent.shelf_load_basis == "per_metre"
            else None,
            "width_um": intent.width_um,
        }
        if isinstance(intent, ShelvingIntent)
        else None,
        "hardware_requirements": list(hardware.required_evidence) if hardware else [],
        "required_workshop_inputs": [
            "material_batches_and_actual_thickness",
            "available_stock_formats_and_grain",
            "machine_axes_work_area_and_controller",
            "tools_holders_and_reach",
            "workholding_and_face_registration",
            "qualified_adhesive_free_retention",
            "postprocessor_and_simulation",
            "physical_joint_coupon_and_first_article_measurements",
        ],
        "scope": "Raw-blank dimensions exclude sheet margins, tool clearance and nesting waste. "
        "Area is not an order quantity. Oversized parts require a different stock/setup or an "
        "explicitly redesigned and validated modular assembly; never an automatic splice.",
        "production_qualified": False,
        "physical_cutting_authorized": False,
    }


def furniture_first_article_checks(result: FurnitureResult) -> bytes:
    """An unfilled measurement worksheet, not a manufactured or approved result."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "design_hash",
            "part_id",
            "part_name",
            "feature_id",
            "side",
            "origin_semantics",
            "check",
            "expected",
            "unit",
            "model_tolerance",
            "agreed_tolerance",
            "measured",
            "result",
            "inspector",
            "notes",
        )
    )
    names = {part.part_id: part.semantic_key for part in result.parts}

    def mm_text(value: int | None) -> str:
        return "" if value is None else f"{value / 1_000:.3f}"

    def row(
        part_id: str,
        feature_id: str,
        check: str,
        value: int,
        tolerance: int | None = None,
        *,
        unit: str = "mm",
        side: str = "",
        origin_semantics: str = "",
    ) -> None:
        writer.writerow(
            (
                result.design_hash,
                part_id,
                names[part_id],
                feature_id,
                side,
                origin_semantics,
                check,
                str(value) if unit == "count" else mm_text(value),
                unit,
                mm_text(tolerance) if tolerance is not None and tolerance > 0 else "",
                "",
                "",
                "",
                "",
                "",
            )
        )

    for part in adapt_design_result(result).parts:
        row(part.part_id, "", "finished_local_U", part.width_um)
        row(part.part_id, "", "finished_local_V", part.height_um)
        row(part.part_id, "", "actual_thickness", part.thickness_um)
        for feature in part.features:
            semantics = (
                "FIRST_CENTRE"
                if feature.kind in {FeatureKind.DRILL, FeatureKind.DRILL_PATTERN}
                else "LOWER_LEFT"
            )
            row(
                part.part_id,
                feature.feature_id,
                "pattern_count",
                feature.pattern_count,
                unit="count",
                side=feature.side.value,
                origin_semantics=semantics,
            )
            for check, value in (("origin_U", feature.x_um), ("origin_V", feature.y_um)):
                row(
                    part.part_id,
                    feature.feature_id,
                    check,
                    value,
                    feature.tolerance_um,
                    side=feature.side.value,
                    origin_semantics=semantics,
                )
            for field in (
                "diameter_um",
                "depth_um",
                "width_um",
                "length_um",
                "radius_um",
                "pitch_um",
            ):
                value = getattr(feature, field)
                if value is not None:
                    row(
                        part.part_id,
                        feature.feature_id,
                        field.removesuffix("_um"),
                        value,
                        feature.tolerance_um,
                        side=feature.side.value,
                        origin_semantics=semantics,
                    )
    return output.getvalue().encode("utf-8-sig")
