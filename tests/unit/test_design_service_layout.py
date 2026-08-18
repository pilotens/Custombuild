from __future__ import annotations

import pytest
from app.design_service import normalize_preview, preview
from app.schemas import BookcasePreviewInput
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
