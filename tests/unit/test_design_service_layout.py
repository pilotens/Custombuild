from __future__ import annotations

import pytest
from app.design_service import normalize_preview, preview
from app.schemas import BookcasePreviewInput, GenerationRequest
from custombuild_domain import PartRole
from pydantic import ValidationError


def test_custom_layout_crosses_api_boundary_into_domain_geometry() -> None:
    payload = BookcasePreviewInput(
        width_mm=2000,
        height_mm=2200,
        divider_count=2,
        shelf_count=3,
        bay_width_ratios=[0.2, 0.6, 0.2],
        shelf_height_ratios=[0.2, 0.5, 0.8],
    )

    spec, result, presented = preview(payload.model_dump(exclude_none=True))
    first_row = sorted(
        (
            part
            for part in result.parts
            if part.role == PartRole.SHELF and part.instance_index < 3
        ),
        key=lambda part: part.placement.x_um,
    )

    assert spec.parameters.bay_width_ratios_ppm == (200_000, 600_000, 200_000)
    assert spec.parameters.shelf_height_ratios_ppm == (200_000, 500_000, 800_000)
    assert first_row[1].finished_size.width_um > first_row[0].finished_size.width_um
    assert presented["spec"]["parameters"]["bay_width_ratios_ppm"] == [
        200_000,
        600_000,
        200_000,
    ]


def test_api_accepts_binary_float_five_percent_shelf_spacing() -> None:
    payload = BookcasePreviewInput(
        shelf_count=2,
        shelf_height_ratios=[0.1, 0.15],
    )

    spec = normalize_preview(payload.model_dump(exclude_none=True))

    assert spec.parameters.shelf_height_ratios_ppm == (100_000, 150_000)


@pytest.mark.parametrize(
    ("bay_ratios", "shelf_ratios"),
    (
        ([0.5, 0.5], []),
        ([], [0.5, 0.4, 0.8]),
        ([0.01, 0.49, 0.5], []),
    ),
)
def test_api_rejects_incoherent_custom_layouts(
    bay_ratios: list[float], shelf_ratios: list[float]
) -> None:
    with pytest.raises(ValidationError):
        BookcasePreviewInput(
            divider_count=2,
            shelf_count=3,
            bay_width_ratios=bay_ratios,
            shelf_height_ratios=shelf_ratios,
        )


@pytest.mark.parametrize(
    ("payload", "error_type"),
    (
        ({"machine_profile_id": "unknown-machine"}, "literal_error"),
        ({"postprocessor_id": "unknown-postprocessor"}, "literal_error"),
        ({"unexpected": True}, "extra_forbidden"),
    ),
)
def test_generation_request_rejects_values_outside_its_runtime_contract(
    payload: dict[str, object], error_type: str
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        GenerationRequest.model_validate(payload)

    assert {error["type"] for error in exc_info.value.errors()} == {error_type}


def test_generation_request_accepts_the_large_format_runtime_profile() -> None:
    request = GenerationRequest(machine_profile_id="custombuild-router-5125-linuxcnc")

    assert request.machine_profile_id == "custombuild-router-5125-linuxcnc"


def test_api_contract_allows_seventeen_base_cabinets_but_only_sixteen_dividers() -> None:
    accepted = BookcasePreviewInput(
        furniture_type="wall_library",
        divider_count=16,
        base_cabinet_count=17,
    )
    assert accepted.base_cabinet_count == 17

    with pytest.raises(ValidationError):
        BookcasePreviewInput(
            furniture_type="wall_library",
            divider_count=17,
            base_cabinet_count=17,
        )

    with pytest.raises(ValidationError):
        BookcasePreviewInput(
            furniture_type="wall_library",
            divider_count=16,
            base_cabinet_count=18,
        )


def test_design_service_rejects_base_cabinets_for_bookcase_family() -> None:
    payload = BookcasePreviewInput(
        furniture_type="bookcase",
        width_mm=2400,
        height_mm=2400,
        depth_mm=340,
        base_cabinet_height_mm=700,
        base_cabinet_depth_mm=340,
        base_cabinet_count=3,
    )

    with pytest.raises(ValueError, match="bookcase furniture cannot contain base cabinets"):
        preview(payload.model_dump(exclude_none=True))


def test_design_service_rejects_wall_library_without_active_base() -> None:
    payload = BookcasePreviewInput(furniture_type="wall_library")

    with pytest.raises(ValueError, match="requires at least one base cabinet"):
        preview(payload.model_dump(exclude_none=True))


def test_max_public_load_keeps_the_existing_kg_to_newton_conversion() -> None:
    payload = BookcasePreviewInput(load_per_shelf_kg=500)

    spec = normalize_preview(payload.model_dump(exclude_none=True))

    assert spec.parameters.shelf_load_n == 4_903


@pytest.mark.parametrize(
    ("material_id", "back_material_id"),
    (("birch-plywood", "mdf-6"), ("mdf", "birch-plywood-6")),
)
def test_explicit_back_material_crosses_api_boundary_into_exact_back_parts(
    material_id: str,
    back_material_id: str,
) -> None:
    payload = BookcasePreviewInput(
        material_id=material_id,
        back_material_id=back_material_id,
    )

    spec, result, presented = preview(payload.model_dump(exclude_none=True))
    back = next(part for part in result.parts if part.role == PartRole.BACK)

    assert spec.material.material_id == material_id
    assert spec.back_material is not None
    assert spec.back_material.material_id == back_material_id
    assert back.material_id == back_material_id
    assert next(part for part in presented["parts"] if part["kind"] == "back")[
        "material_id"
    ] == back_material_id


@pytest.mark.parametrize(
    ("material_id", "expected_back_material_id"),
    (("mdf", "mdf-6"), ("birch-plywood", "birch-plywood-6")),
)
def test_legacy_preview_without_back_material_keeps_matching_derivation(
    material_id: str,
    expected_back_material_id: str,
) -> None:
    request = BookcasePreviewInput(material_id=material_id, back_panel=True).model_dump(
        exclude_none=True
    )
    assert "back_material_id" not in request

    spec = normalize_preview(request)

    assert spec.back_material is not None
    assert spec.back_material.material_id == expected_back_material_id


@pytest.mark.parametrize("disabled_back", (False, "none"))
def test_back_material_rejects_unknown_and_backless_conflicts(
    disabled_back: bool | str,
) -> None:
    with pytest.raises(ValidationError, match="back_material_id"):
        BookcasePreviewInput.model_validate({"back_material_id": "oak-6"})

    with pytest.raises(ValidationError, match="requires an enabled back panel"):
        BookcasePreviewInput.model_validate(
            {"back_panel": disabled_back, "back_material_id": "mdf-6"}
        )

    with pytest.raises(ValueError, match="unknown back_material_id"):
        normalize_preview({"back_material_id": "oak-6"})

    with pytest.raises(ValueError, match="requires an enabled back panel"):
        normalize_preview(
            {"back_panel": disabled_back, "back_material_id": "mdf-6"}
        )
