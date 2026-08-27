from __future__ import annotations

import csv
import io
import json

from custombuild_manufacturing import (
    DeterministicNester,
    PartSpec,
    StockSheet,
    grouped_bom_json,
    stock_purchase_csv,
)


def _parts_and_layout():
    first = PartSpec(
        "panel-a",
        "Shelf",
        300_000,
        200_000,
        18_000,
        "mdf",
        "v1",
        quantity=2,
        weight_g=500,
    )
    second = PartSpec(
        "panel-b",
        "Shelf",
        300_000,
        200_000,
        18_000,
        "mdf",
        "v1",
        quantity=1,
        weight_g=500,
    )
    third = PartSpec(
        "panel-c",
        "Side",
        200_000,
        600_000,
        18_000,
        "mdf",
        "v1",
        weight_g=900,
    )
    stock = StockSheet("sheet", "mdf", "v1", 1_000_000, 600_000, 18_000)
    layout = DeterministicNester().nest((first, second, third), stock)
    return (first, second, third), layout


def test_grouped_bom_fingerprint_preserves_exact_instance_totals() -> None:
    parts, _ = _parts_and_layout()

    first = grouped_bom_json(parts)
    second = grouped_bom_json(tuple(reversed(parts)))
    payload = json.loads(first)

    assert first == second
    assert payload["physical_release_authorized"] is False
    assert payload["part_instance_count"] == sum(part.quantity for part in parts)
    assert len(payload["group_fingerprint"]) == 64
    assert sum(group["quantity"] for group in payload["groups"]) == 4
    shelf_group = next(
        group for group in payload["groups"] if group["signature"]["name"] == "Shelf"
    )
    assert shelf_group["quantity"] == 3
    assert shelf_group["part_ids"] == ["panel-a", "panel-b"]
    assert shelf_group["conservative_total_weight_g"] == 1_500


def test_stock_purchase_uses_actual_used_sheets_and_leaves_commercial_fields_blank() -> None:
    _, layout = _parts_and_layout()

    rows = list(
        csv.DictReader(io.StringIO(stock_purchase_csv(layout).decode("utf-8")))
    )

    assert len(rows) == 1
    row = rows[0]
    expected_area_m2 = (
        layout.used_sheet_count * layout.stock.width_um * layout.stock.height_um / 1e12
    )
    placed_area_m2 = sum(
        placement.width_um * placement.height_um for placement in layout.placements
    ) / 1e12
    assert int(row["required_sheet_count"]) == layout.used_sheet_count
    assert float(row["purchased_sheet_area_m2"]) == expected_area_m2
    assert float(row["placed_blank_area_m2"]) == placed_area_m2
    assert row["supplier_sku"] == ""
    assert row["unit_cost"] == ""
    assert row["procurement_status"] == "EXTERNAL_PURCHASE_SELECTION_REQUIRED"
    assert row["physical_release_authorized"] == "false"
