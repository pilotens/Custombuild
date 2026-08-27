from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.main import app
from app.schemas import (
    MAX_WORKSPACE_INTENT_BYTES,
    ProjectDraftUpdate,
)
from pydantic import ValidationError


def preview_spec(**overrides: Any) -> dict[str, Any]:
    return {
        "width_mm": 700,
        "height_mm": 1_000,
        "depth_mm": 320,
        "furniture_type": "bookcase",
        "material_id": "mdf",
        "nominal_thickness_mm": 18,
        "measured_thickness_mm": 18,
        "shelf_count": 2,
        "shelf_mount": "fixed",
        "load_per_shelf_kg": 10,
        "back_panel": True,
        "plinth": True,
        "divider_count": 0,
        "bay_width_ratios": [],
        "shelf_height_ratios": [],
        "base_cabinet_height_mm": 0,
        "base_cabinet_depth_mm": 0,
        "base_cabinet_count": 0,
        "edge_band_mm": 1,
        "joint_system": "dado",
        "reinforcement_mode": "manual",
        "wall_anchor_required": False,
        "wall_anchor_verified": False,
        **overrides,
    }


def production_context() -> dict[str, Any]:
    return {
        "stock_width_mm": 2_440,
        "stock_height_mm": 1_220,
        "stock_count": 4,
        "back_stock_width_mm": 2_440,
        "back_stock_height_mm": 1_220,
        "back_stock_count": 2,
        "machine_profile_id": "custombuild-router-1325-linuxcnc",
    }


def workspace_intent(**overrides: Any) -> dict[str, Any]:
    return {
        "schema_version": "custombuild.workspace-intent.v1",
        "bay_sizing_mode": "count",
        "target_bay_width_mm": 300,
        "symmetry_locked": True,
        "production_context": production_context(),
        "part_overrides": {},
        "removed_part_ids": [],
        **overrides,
    }


def legacy_workspace(**overrides: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "design_id": "legacy-design",
        "revision": 7,
        "furniture_type": "bookcase",
        "width_mm": 700,
        "height_mm": 1_000,
        "depth_mm": 320,
        "material_id": "mdf",
        "material_name": "MDF",
        "nominal_thickness_mm": 18,
        "measured_thickness_mm": 18,
        "shelf_count": 2,
        "fixed_shelves": True,
        "load_per_shelf_kg": 10,
        "back_panel": True,
        "plinth": True,
        "divider_count": 0,
        "bay_sizing_mode": "count",
        "target_bay_width_mm": 300,
        "bay_width_ratios": [],
        "shelf_height_ratios": [],
        "symmetry_locked": True,
        "part_overrides": {},
        "removed_part_ids": [],
        "base_cabinet_height_mm": 0,
        "base_cabinet_depth_mm": 0,
        "base_cabinet_count": 0,
        "reinforcement_mode": "manual",
        "joint_system": "dado",
        "edge_band_mm": 1,
        "wall_anchor_verified": False,
        **production_context(),
        **overrides,
    }


def draft_payload(workspace: dict[str, Any], **spec_overrides: Any) -> dict[str, Any]:
    return {
        "expected_draft_revision": 0,
        "template_id": "shelving",
        "spec": preview_spec(**spec_overrides),
        "workspace_spec": workspace,
    }


def test_explicit_exact_legacy_workspace_is_migrated_to_v1_only() -> None:
    parsed = ProjectDraftUpdate.model_validate(draft_payload(legacy_workspace()))

    stored = parsed.workspace_spec.model_dump(mode="json", exclude_none=True)
    assert stored == workspace_intent()
    assert "width_mm" not in stored
    assert "design_id" not in stored


def test_explicit_back_material_is_preserved_but_legacy_workspace_must_match() -> None:
    parsed = ProjectDraftUpdate.model_validate(
        draft_payload(
            workspace_intent(),
            material_id="birch-plywood",
            back_material_id="mdf-6",
        )
    )
    assert parsed.spec.material_id == "birch-plywood"
    assert parsed.spec.back_material_id == "mdf-6"

    with pytest.raises(ValidationError, match="back_panel_type"):
        ProjectDraftUpdate.model_validate(
            draft_payload(
                legacy_workspace(),
                back_panel="surface_mounted",
            )
        )

    with pytest.raises(
        ValidationError,
        match="legacy workspace production fields do not match spec: back_material_id",
    ):
        ProjectDraftUpdate.model_validate(
            draft_payload(
                legacy_workspace(
                    material_id="birch-plywood",
                    material_name="Björkplywood",
                ),
                material_id="birch-plywood",
                back_material_id="mdf-6",
            )
        )


@pytest.mark.parametrize(
    ("workspace_override", "expected_error"),
    [
        ({"shelf_count": 1_000_000_000}, "less than or equal to 40"),
        ({"width_mm": 9_999}, "less than or equal to 6000"),
        ({"width_mm": 701}, "production fields do not match spec: width_mm"),
    ],
)
def test_legacy_workspace_cannot_smuggle_mismatched_production_fields(
    workspace_override: dict[str, Any],
    expected_error: str,
) -> None:
    with pytest.raises(ValidationError, match=expected_error):
        ProjectDraftUpdate.model_validate(
            draft_payload(legacy_workspace(**workspace_override))
        )


def test_v1_rejects_unknown_root_and_nested_keys() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProjectDraftUpdate.model_validate(
            draft_payload(workspace_intent(shelf_count=1_000_000_000))
        )

    nested = production_context() | {"postprocessor_id": "untrusted"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProjectDraftUpdate.model_validate(
            draft_payload(workspace_intent(production_context=nested))
        )


@pytest.mark.parametrize(
    "non_finite",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_v1_rejects_non_finite_json_numbers(non_finite: float) -> None:
    with pytest.raises(ValidationError, match="workspace_spec must be finite JSON data"):
        ProjectDraftUpdate.model_validate(
            draft_payload(workspace_intent(target_bay_width_mm=non_finite))
        )


@pytest.mark.parametrize(
    "intent",
    [
        pytest.param(workspace_intent(target_bay_width_mm="300"), id="numeric-string"),
        pytest.param(workspace_intent(symmetry_locked=1), id="integer-as-boolean"),
        pytest.param(
            workspace_intent(
                production_context=production_context() | {"stock_count": 4.0}
            ),
            id="float-as-integer",
        ),
        pytest.param(
            workspace_intent(part_overrides={"top": {"width_mm": "100"}}),
            id="nested-numeric-string",
        ),
    ],
)
def test_v1_rejects_type_coercion(intent: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProjectDraftUpdate.model_validate(draft_payload(intent))


@pytest.mark.parametrize("field", ["reference_image_import", "topology_baseline"])
def test_v1_rejects_explicit_null_for_optional_objects(field: str) -> None:
    intent = workspace_intent()
    intent[field] = None

    with pytest.raises(
        ValidationError,
        match="optional workspace intent objects cannot be null",
    ):
        ProjectDraftUpdate.model_validate(draft_payload(intent))


def test_v1_rejects_one_unknown_generated_part_id() -> None:
    intent = workspace_intent(
        part_overrides={"not-a-generated-part": {"width_mm": 100.0}}
    )

    with pytest.raises(
        ValidationError,
        match="workspace intent references an unknown generated part ID",
    ):
        ProjectDraftUpdate.model_validate(draft_payload(intent))


def test_explicit_legacy_workspace_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProjectDraftUpdate.model_validate(
            draft_payload(legacy_workspace(untrusted_legacy_field=True))
        )


def test_checked_in_openapi_contract_matches_runtime_schema() -> None:
    contract_path = Path(__file__).resolve().parents[2] / "packages" / "contracts" / "openapi.json"
    checked_in = json.loads(contract_path.read_text(encoding="utf-8"))

    assert checked_in == app.openapi()


def test_workspace_size_is_rejected_before_nested_storage_validation() -> None:
    oversized = workspace_intent(_padding="")
    compact_size = len(
        json.dumps(oversized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    oversized["_padding"] = "x" * (MAX_WORKSPACE_INTENT_BYTES + 1 - compact_size)
    assert (
        len(json.dumps(oversized, separators=(",", ":")).encode("utf-8"))
        == MAX_WORKSPACE_INTENT_BYTES + 1
    )

    with pytest.raises(ValidationError, match="128 KiB"):
        ProjectDraftUpdate.model_validate(draft_payload(oversized))


def test_normalization_cannot_expand_a_valid_request_beyond_the_storage_limit() -> None:
    removed_part_ids = []
    for index in range(1_024):
        prefix = f"p{index:04d}-"
        removed_part_ids.append(prefix + "x" * (124 - len(prefix)))
    reference = {
        "source": "reference_image",
        "import_id": "11111111-1111-1111-1111-111111111111",
        "image_sha256": "a" * 64,
        "file_name": "reference.png",
        "image_width_px": 800,
        "image_height_px": 600,
        "confidence": 0.5,
        "detected_shelves": 2,
        "detected_dividers": 0,
        "detected_base_cabinets": False,
        "warnings": ["w" * 120],
    }
    raw = workspace_intent(
        removed_part_ids=removed_part_ids,
        reference_image_import=reference,
    )
    assert (
        len(json.dumps(raw, separators=(",", ":")).encode("utf-8"))
        <= MAX_WORKSPACE_INTENT_BYTES
    )

    with pytest.raises(ValidationError, match="normalized workspace_spec"):
        ProjectDraftUpdate.model_validate(draft_payload(raw))


def test_workspace_rejects_more_than_1024_custom_part_ids() -> None:
    overrides = {f"part-{index}": {"width_mm": 100} for index in range(1_024)}
    with pytest.raises(ValidationError, match="1024 custom-part resource limit"):
        ProjectDraftUpdate.model_validate(
            draft_payload(
                workspace_intent(part_overrides=overrides, removed_part_ids=["top"])
            )
        )
