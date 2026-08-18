from __future__ import annotations

from types import SimpleNamespace

import pytest
from custombuild_domain import (
    BookcaseDesignSpec,
    BookcaseParameters,
    build_bookcase,
    screening_mdf_6,
    screening_mdf_18,
)
from custombuild_manufacturing.adapters import adapt_design_result, adapt_domain_part


def _part(role: str, *, edge: str, size: tuple[int, int, int]):
    width_um, depth_um, height_um = size
    dimensions = SimpleNamespace(
        width_um=width_um,
        depth_um=depth_um,
        height_um=height_um,
    )
    return SimpleNamespace(
        part_id=f"part-{role.lower()}",
        role=role,
        instance_index=0,
        finished_size=dimensions,
        raw_size=dimensions,
        material_id="mdf",
        material_version="v1",
        actual_thickness_um=18_000,
        grain_direction="none",
        a_side="a",
        b_side="b",
        edge_bands=(SimpleNamespace(edge=edge, thickness_um=1_000),),
        features=(),
        weight_g=1,
    )


@pytest.mark.parametrize(
    ("role", "size", "expected_edge"),
    (
        ("LEFT_SIDE", (18_000, 320_000, 2_000_000), "U_MIN"),
        ("TOP", (1_000_000, 320_000, 18_000), "V_MIN"),
    ),
)
def test_global_front_edge_maps_to_the_correct_local_panel_boundary(
    role: str,
    size: tuple[int, int, int],
    expected_edge: str,
) -> None:
    adapted = adapt_domain_part(_part(role, edge="front", size=size))

    assert adapted.edge_bands == (expected_edge,)
    assert adapted.edge_band_details[0].edge == expected_edge
    assert adapted.edge_band_details[0].source_face == "FRONT"
    assert adapted.edge_band_details[0].thickness_um == 1_000
    assert adapted.edge_band_details[0].procurement_status == "EXTERNAL_SELECTION_REQUIRED"


def test_edge_band_normal_to_panel_thickness_is_rejected() -> None:
    cabinet_front = _part(
        "CABINET_FRONT",
        edge="front",
        size=(600_000, 18_000, 700_000),
    )

    with pytest.raises(ValueError, match="normal to panel thickness"):
        adapt_domain_part(cabinet_front)


def test_edge_band_without_thickness_is_rejected() -> None:
    part = _part("TOP", edge="front", size=(1_000_000, 320_000, 18_000))
    part.edge_bands = (SimpleNamespace(edge="front"),)

    with pytest.raises(ValueError, match="has no thickness"):
        adapt_domain_part(part)


def test_generated_base_cabinet_has_only_physically_bandable_boundaries() -> None:
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="edge-band-base-cabinet",
            parameters=BookcaseParameters(
                width_um=2_400_000,
                depth_um=340_000,
                height_um=2_400_000,
                vertical_divider_count=2,
                shelf_count=3,
                base_cabinet_count=3,
                base_cabinet_height_um=680_000,
                base_cabinet_depth_um=340_000,
            ),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )

    adapted = adapt_design_result(design)
    fronts = tuple(part for part in adapted.parts if part.name == "CABINET_FRONT")

    assert fronts
    assert all(not part.edge_bands for part in fronts)
    assert all(
        detail.edge in {"U_MIN", "U_MAX", "V_MIN", "V_MAX"}
        for part in adapted.parts
        for detail in part.edge_band_details
    )
