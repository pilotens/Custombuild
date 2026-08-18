from __future__ import annotations

import csv
import io
import json

import pytest
from custombuild_manufacturing import (
    DeterministicNester,
    FeatureKind,
    ManufacturingFeature,
    PartSpec,
    Side,
    StockSheet,
    generate_operations_document,
    label_index_csv,
    linuxcnc_reference_router_1325,
    quality_measurement_plan_json,
)
from custombuild_manufacturing.package import default_artifacts


def _quality_values():
    part = PartSpec(
        "panel",
        "Panel",
        300_000,
        200_000,
        18_000,
        "mdf",
        "v1",
        quantity=2,
        features=(
            ManufacturingFeature(
                "hole",
                "panel",
                FeatureKind.DRILL,
                Side.A,
                50_000,
                50_000,
                10_000,
                diameter_um=8_000,
                tolerance_um=150,
            ),
        ),
    )
    stock = StockSheet("sheet", "mdf", "v1", 1_000_000, 600_000, 18_000)
    layout = DeterministicNester().nest((part,), stock)
    operations = generate_operations_document(
        design_hash="d" * 64,
        parts=(part,),
        layout=layout,
        machine=linuxcnc_reference_router_1325(),
    )
    return part, layout, operations


def test_label_index_covers_every_placement_once_with_non_authorizing_qr() -> None:
    part, layout, operations = _quality_values()

    first = label_index_csv(parts=(part,), layouts=layout, operations=operations)
    second = label_index_csv(parts=(part,), layouts=(layout,), operations=operations)
    rows = list(csv.DictReader(io.StringIO(first.decode("utf-8"))))

    assert first == second
    assert {row["instance_id"] for row in rows} == {
        placement.instance_id for placement in layout.placements
    }
    assert len(rows) == len(layout.placements)
    assert len({row["qr_payload"] for row in rows}) == len(rows)
    assert all(row["qr_payload"].startswith(f"custombuild:part:{'d' * 64}:") for row in rows)
    assert all(row["physical_release_authorized"] == "false" for row in rows)


def test_measurement_plan_covers_every_part_dimension_and_operation_without_pass() -> None:
    part, layout, operations = _quality_values()

    payload = json.loads(
        quality_measurement_plan_json(
            parts=(part,),
            layouts=layout,
            operations=operations,
        )
    )

    instance_ids = {placement.instance_id for placement in layout.placements}
    assert payload["physical_release_authorized"] is False
    assert payload["approval_state"] == "PENDING_EXTERNAL_MEASUREMENT"
    assert payload["coverage"]["part_instance_count"] == 2
    assert payload["coverage"]["dimension_check_count"] == 6
    assert payload["coverage"]["operation_check_count"] == len(operations.operations)
    assert {item["instance_id"] for item in payload["dimension_checks"]} == instance_ids
    assert {item["operation_id"] for item in payload["operation_checks"]} == {
        operation.operation_id for operation in operations.operations
    }
    assert all(item["measured_um"] is None for item in payload["dimension_checks"])
    assert all(item["result"] is None for item in payload["dimension_checks"])
    assert all(item["measured"] is None for item in payload["operation_checks"])
    assert all(item["result"] is None for item in payload["operation_checks"])
    assert any(
        item["tolerance_um"] == 150
        and item["tolerance_status"] == "DECLARED_IN_DESIGN"
        for item in payload["operation_checks"]
    )
    assert b'"result":"PASS"' not in quality_measurement_plan_json(
        parts=(part,), layouts=layout, operations=operations
    )


def test_label_index_rejects_duplicate_instance_placement() -> None:
    part, layout, operations = _quality_values()
    duplicate_layout = type(layout)(
        layout.stock,
        (*layout.placements, layout.placements[0]),
        layout.unplaced_instance_ids,
        layout.used_sheet_count,
        layout.utilization_ppm,
        layout.algorithm,
    )

    with pytest.raises(ValueError, match="placed more than once"):
        label_index_csv(parts=(part,), layouts=duplicate_layout, operations=operations)


def test_default_package_includes_traceability_quality_and_procurement_documents() -> None:
    part, layout, operations = _quality_values()

    artifacts = {
        artifact.path: artifact
        for artifact in default_artifacts(
            parts=(part,),
            layout=layout,
            operations=operations,
        )
    }

    assert artifacts["labels/label-index.csv"].role == "LABEL_INDEX"
    assert artifacts["quality/measurement-plan.json"].role == "QUALITY_MEASUREMENT_PLAN"
    assert artifacts["bom/grouped-bom.json"].role == "GROUPED_BOM"
    assert artifacts["materials/stock-purchase.csv"].role == "STOCK_PURCHASE_SCHEDULE"
