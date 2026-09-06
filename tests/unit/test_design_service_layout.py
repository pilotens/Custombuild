from __future__ import annotations

import json

import pytest
from app.design_service import (
    normalize_preview,
    preview,
    stock_configuration_for_design,
    two_sided_registration_for_design,
)
from app.schemas import BookcasePreviewInput, GenerationRequest
from custombuild_domain import PartRole
from custombuild_manufacturing.adapters import adapt_design_result
from custombuild_manufacturing.quality import manufacturing_intent_json
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


def _structured_shop_request() -> dict[str, object]:
    return {
        "stock_width_mm": 2440,
        "stock_height_mm": 1220,
        "stock_count": 4,
        "back_stock_width_mm": 2440,
        "back_stock_height_mm": 1220,
        "back_stock_count": 2,
        "stock_profiles": [
            {
                "role": "carcass",
                "declaration_authority": "CLIENT_DECLARED",
                "supplier_profile_id": "supplier-mdf-18",
                "supplier_profile_version": "batch-2026-09",
                "material_id": "mdf",
                "material_version": "screening-2026.1",
                "sheet_width_um": 2_440_000,
                "sheet_height_um": 1_220_000,
                "thickness_um": 18_000,
                "sheet_count": 4,
                "trim_margin_um": 10_000,
                "kerf_um": 6_000,
                "grain_direction": "NONE",
                "allow_rotation": True,
                "defect_zones": [],
                "fixture_keep_out_zones": [
                    {
                        "x_um": 0,
                        "y_um": 0,
                        "width_um": 40_000,
                        "height_um": 1_220_000,
                    }
                ],
            },
            {
                "role": "back",
                "declaration_authority": "CLIENT_DECLARED",
                "supplier_profile_id": "supplier-mdf-6",
                "supplier_profile_version": "batch-2026-09",
                "material_id": "mdf-6",
                "material_version": "screening-2026.1",
                "sheet_width_um": 2_440_000,
                "sheet_height_um": 1_220_000,
                "thickness_um": 6_000,
                "sheet_count": 2,
                "trim_margin_um": 10_000,
                "kerf_um": 6_000,
                "grain_direction": "NONE",
                "allow_rotation": True,
                "defect_zones": [],
                "fixture_keep_out_zones": [],
            },
        ],
        "two_sided_registrations": [
            {
                "stock_role": "carcass",
                "sheet_index": 0,
                "flip_axis": "X",
                "declaration_authority": "CLIENT_DECLARED",
                "fixture_method_id": "supplier-pin-fixture-v1",
                "fixture_method_version": "supplier-fixture-2026.1",
                "pin_diameter_um": 6_000,
                "position_tolerance_um": 500,
                "pins": [
                    {"x_um": 80_000, "y_um": 30_000},
                    {"x_um": 2_360_000, "y_um": 30_000},
                ],
            }
        ],
    }


def test_generation_request_binds_exact_structured_stock_and_registration() -> None:
    request = GenerationRequest.model_validate(_structured_shop_request())

    assert request.stock_profiles is not None
    assert request.stock_profiles[0].sheet_width_um == 2_440_000
    assert request.stock_profiles[0].fixture_keep_out_zones[0].width_um == 40_000
    assert request.two_sided_registrations is not None
    assert request.two_sided_registrations[0].flip_axis == "X"
    assert request.two_sided_registrations[0].pins[1].x_um == 2_360_000


@pytest.mark.parametrize(
    "payload",
    (
        {"stock_width_mm": 2440.0001},
        {"stock_profiles": [], "two_sided_registrations": []},
        {
            **_structured_shop_request(),
            "stock_profiles": [*_structured_shop_request()["stock_profiles"]][1:],  # type: ignore[index]
        },
        {
            **_structured_shop_request(),
            "two_sided_registrations": [
                {
                    "stock_role": "carcass",
                    "sheet_index": 4,
                    "flip_axis": "X",
                    "declaration_authority": "CLIENT_DECLARED",
                    "fixture_method_id": "supplier-pin-fixture-v1",
                    "fixture_method_version": "supplier-fixture-2026.1",
                    "pin_diameter_um": 6_000,
                    "position_tolerance_um": 500,
                    "pins": [
                        {"x_um": 80_000, "y_um": 30_000},
                        {"x_um": 2_360_000, "y_um": 30_000},
                    ],
                }
            ],
        },
    ),
)
def test_generation_request_rejects_lossy_or_incomplete_shop_context(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        GenerationRequest.model_validate(payload)


def test_design_service_resolves_server_owned_stock_ids_and_registration() -> None:
    _, result, _ = preview(BookcasePreviewInput().model_dump(exclude_none=True))
    request = GenerationRequest.model_validate(_structured_shop_request()).model_dump(
        mode="json",
        exclude_none=True,
    )

    stocks = stock_configuration_for_design(result, request)
    registrations = two_sided_registration_for_design(result, request, stocks=stocks)

    assert len(stocks) == 2
    stock_roles = zip(stocks, ("carcass", "back"), strict=True)
    assert all(
        stock.stock_id.startswith(f"stock-{role}-supplier-")
        for stock, role in stock_roles
    )
    carcass = next(stock for stock in stocks if stock.thickness_um == 18_000)
    assert carcass.grain_direction == "NONE"
    assert carcass.margin_um == 10_000
    assert carcass.kerf_um == 6_000
    assert carcass.clamp_zones[0].width_um == 40_000
    plan = registrations[carcass.stock_id][0]
    assert plan.method_id == "supplier-pin-fixture-v1"
    assert [(point.x_um, point.y_um) for point in plan.points] == [
        (80_000, 30_000),
        (2_360_000, 30_000),
    ]


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


def test_measured_back_thickness_crosses_api_boundary_as_exact_geometry() -> None:
    payload = BookcasePreviewInput(measured_back_thickness_mm=5.8)

    spec, result, presented = preview(payload.model_dump(exclude_none=True))
    back = next(part for part in result.parts if part.role == PartRole.BACK)

    assert spec.parameters.back_thickness_um == 5_800
    assert back.actual_thickness_um == 5_800
    assert presented["spec"]["parameters"]["back_thickness_um"] == 5_800
    assert next(part for part in presented["parts"] if part["kind"] == "back")[
        "thickness_mm"
    ] == 5.8


def test_measured_back_thickness_rejects_rounding_and_catalogue_overreach() -> None:
    with pytest.raises(ValidationError, match="at most 0.001 mm precision"):
        BookcasePreviewInput(measured_back_thickness_mm=5.8005)

    with pytest.raises(ValueError, match="whole micrometres"):
        normalize_preview({"measured_back_thickness_mm": 5.8005})

    for unsupported in (5.499, 6.501):
        with pytest.raises(ValidationError):
            BookcasePreviewInput(measured_back_thickness_mm=unsupported)


@pytest.mark.parametrize(
    ("edge_band_mm", "expected_um"),
    ((0, 0), (1, 1_000), (1.2, 1_200)),
)
def test_edge_band_input_controls_exact_front_edge_geometry(
    edge_band_mm: float,
    expected_um: int,
) -> None:
    payload = BookcasePreviewInput(edge_band_mm=edge_band_mm)

    spec, result, presented = preview(payload.model_dump(exclude_none=True))

    assert spec.parameters.edge_band_thickness_um == expected_um
    assert presented["spec"]["parameters"]["edge_band_thickness_um"] == expected_um
    edge_bands = [band for part in result.parts for band in part.edge_bands]
    if expected_um == 0:
        assert edge_bands == []
    else:
        assert edge_bands
        assert {band.thickness_um for band in edge_bands} == {expected_um}


def test_default_design_has_no_unresolved_edge_band_but_explicit_one_mm_does() -> None:
    def unresolved_edge_applications(payload: BookcasePreviewInput) -> list[str]:
        _spec, result, _presented = preview(payload.model_dump(exclude_none=True))
        parts = adapt_design_result(result).parts
        intent = json.loads(
            manufacturing_intent_json(
                parts=parts,
                project_id="edge-band-default-regression",
                revision="1",
                design_hash=result.design_hash,
            )
        )
        return list(intent["external_decisions"]["unresolved_edge_application_ids"])

    default_payload = BookcasePreviewInput()
    explicit_payload = BookcasePreviewInput(edge_band_mm=1)

    assert default_payload.edge_band_mm == 0
    assert normalize_preview({}).parameters.edge_band_thickness_um == 0
    assert unresolved_edge_applications(default_payload) == []
    assert explicit_payload.edge_band_mm == 1
    assert unresolved_edge_applications(explicit_payload)


def test_edge_band_input_rejects_submicron_and_out_of_range_values() -> None:
    for unsupported in (1.0005, -0.001, 5.001):
        with pytest.raises(ValidationError):
            BookcasePreviewInput(edge_band_mm=unsupported)

    with pytest.raises(ValueError, match="whole micrometres"):
        normalize_preview({"edge_band_mm": 1.0005})


def test_structured_back_stock_must_match_measured_back_thickness() -> None:
    payload = BookcasePreviewInput(measured_back_thickness_mm=5.8)
    _, result, _ = preview(payload.model_dump(exclude_none=True))
    request = GenerationRequest.model_validate(_structured_shop_request()).model_dump(
        mode="json",
        exclude_none=True,
    )

    with pytest.raises(ValueError, match="structured back profile does not match"):
        stock_configuration_for_design(result, request)

    profiles = request["stock_profiles"]
    assert isinstance(profiles, list)
    profiles[1]["thickness_um"] = 5_800
    stocks = stock_configuration_for_design(result, request)
    assert next(stock for stock in stocks if stock.material_id == "mdf-6").thickness_um == 5_800


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
