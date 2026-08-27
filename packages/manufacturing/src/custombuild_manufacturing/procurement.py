"""Deterministic grouped demand and stock-purchase review documents."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from typing import Any

from .model import (
    NestingLayout,
    PartSpec,
    canonical_json_bytes,
    sha256_hex,
    um_to_mm,
)

GROUPED_BOM_SCHEMA_VERSION = "custombuild.grouped-bom.v1"
STOCK_PURCHASE_SCHEMA_VERSION = "custombuild.stock-purchase.v1"


def grouped_bom_json(parts: Iterable[PartSpec]) -> bytes:
    """Group identical blanks without losing instance-level traceability."""

    part_values = tuple(parts)
    groups: dict[bytes, dict[str, Any]] = {}
    for part in sorted(part_values, key=lambda item: item.part_id):
        signature = {
            "name": part.name,
            "finished_um": {
                "width": part.width_um,
                "height": part.height_um,
                "thickness": part.thickness_um,
            },
            "raw_um": {
                "width": part.blank_width_um,
                "height": part.blank_height_um,
                "thickness": part.thickness_um,
            },
            "material_id": part.material_id,
            "material_version": part.material_version,
            "grain_direction": part.grain_direction,
            "edge_bands": part.edge_band_details,
        }
        signature_bytes = canonical_json_bytes(signature)
        group = groups.setdefault(
            signature_bytes,
            {
                "group_id": f"bom-group:{sha256_hex(signature_bytes)[:16]}",
                "signature": signature,
                "part_ids": [],
                "quantity": 0,
                "conservative_total_weight_g": 0,
                "finished_area_um2": 0,
                "raw_area_um2": 0,
            },
        )
        group["part_ids"].append(part.part_id)
        group["quantity"] += part.quantity
        group["conservative_total_weight_g"] += part.weight_g * part.quantity
        group["finished_area_um2"] += part.width_um * part.height_um * part.quantity
        group["raw_area_um2"] += part.blank_width_um * part.blank_height_um * part.quantity

    rows = tuple(sorted(groups.values(), key=lambda item: item["group_id"]))
    for row in rows:
        row["part_ids"] = tuple(sorted(row["part_ids"]))
    fingerprint = sha256_hex(canonical_json_bytes(rows))
    payload = {
        "schema_version": GROUPED_BOM_SCHEMA_VERSION,
        "group_fingerprint": fingerprint,
        "release_scope": "DESIGN_REVIEW",
        "physical_release_authorized": False,
        "group_count": len(rows),
        "part_instance_count": sum(part.quantity for part in part_values),
        "groups": rows,
    }
    return canonical_json_bytes(payload)


def stock_purchase_csv(
    layouts: NestingLayout | Iterable[NestingLayout],
) -> bytes:
    """Describe computed sheet demand without inventing supplier or cost data."""

    layout_values = (layouts,) if isinstance(layouts, NestingLayout) else tuple(layouts)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "schema_version",
            "stock_id",
            "material_id",
            "material_version",
            "thickness_mm",
            "sheet_width_mm",
            "sheet_height_mm",
            "required_sheet_count",
            "purchased_sheet_area_m2",
            "placed_blank_area_m2",
            "computed_waste_area_m2",
            "utilization_percent",
            "unplaced_instance_count",
            "supplier_sku",
            "unit_cost",
            "procurement_status",
            "physical_release_authorized",
        )
    )
    for layout in sorted(layout_values, key=lambda item: item.stock.stock_id):
        stock = layout.stock
        sheet_area_um2 = stock.width_um * stock.height_um
        purchased_area_um2 = sheet_area_um2 * layout.used_sheet_count
        placed_area_um2 = sum(
            placement.width_um * placement.height_um for placement in layout.placements
        )
        waste_area_um2 = max(0, purchased_area_um2 - placed_area_um2)
        writer.writerow(
            (
                STOCK_PURCHASE_SCHEMA_VERSION,
                stock.stock_id,
                stock.material_id,
                stock.material_version,
                um_to_mm(stock.thickness_um),
                um_to_mm(stock.width_um),
                um_to_mm(stock.height_um),
                layout.used_sheet_count,
                _area_m2(purchased_area_um2),
                _area_m2(placed_area_um2),
                _area_m2(waste_area_um2),
                f"{layout.utilization_ppm / 10_000:.2f}",
                len(layout.unplaced_instance_ids),
                "",
                "",
                "EXTERNAL_PURCHASE_SELECTION_REQUIRED",
                "false",
            )
        )
    return output.getvalue().encode("utf-8")


def _area_m2(area_um2: int) -> str:
    return f"{area_um2 / 1_000_000_000_000:.6f}"
