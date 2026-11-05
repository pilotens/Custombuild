from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from custombuild_domain.enums import GrainDirection, MaterialType
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_catalog import resolve_material
from custombuild_domain.furniture_engine import build_furniture
from custombuild_domain.furniture_production import (
    FurnitureProductionSource,
    furniture_production_source,
)
from custombuild_domain.identity import content_hash
from custombuild_manufacturing.furniture_module_planning import (
    FurnitureModuleGrid,
    plan_furniture_modules,
)
from custombuild_manufacturing.furniture_profiles import preview_furniture

EXAMPLE = Path("examples/furniture/walnut-shelving-4340x2540x280.json")


def walnut() -> FurnitureWorkspace:
    return FurnitureWorkspace.model_validate_json(EXAMPLE.read_text())


def test_walnut_requires_explicit_matching_stock_and_preserves_production_boundary():
    source = walnut()
    material = resolve_material(source.design.material)
    assert material.material_type == MaterialType.SOLID_WOOD
    assert material.density_kg_m3 == 609 and material.elastic_modulus_mpa == 11_584
    doc = source.model_dump(mode="json")
    doc["manufacturing"]["material_stocks"] = []
    raw = preview_furniture(FurnitureWorkspace.model_validate(doc))
    group = next(
        g for g in raw["manufacturing"]["stock_groups"] if g["material_id"] == "solid-walnut"
    )
    assert group["selection_source"] == "default" and group["geometry_compatible"] is False
    assert {i["code"] for i in group["issues"]} >= {"SOLID_WOOD_BLANK_REQUIRED"}
    assert all(p["fits_stock"] is None for p in group["parts"])
    assert not group["required_formats"]
    assert any(
        r["rule_id"] == "CB-SOLID-WOOD-001" and r["status"] == "BLOCK"
        for r in raw["rules"]["evaluations"]
    )
    with pytest.raises(ValueError, match="Massivträ"):
        furniture_production_source(source)
    with pytest.raises(ValueError, match="Massivträ"):
        FurnitureProductionSource(
            workspace=source,
            workspace_sha256=content_hash(source),
            furniture_design_hash=build_furniture(source.design).design_hash,
        )
    doc["design"]["material"]["material_id"] = "unlisted-walnut-species"
    with pytest.raises(ValueError, match="unknown material"):
        preview_furniture(FurnitureWorkspace.model_validate(doc))


@pytest.mark.parametrize("family", ["dining_table", "chair", "chest_of_drawers"])
def test_walnut_part_inventory_and_direction(family):
    doc = {
        "design": {"intent": {"family": family}, "material": walnut().design.material.model_dump()}
    }
    if family == "chest_of_drawers":
        doc["design"]["hardware"] = {"catalog_id": "hettich-ka4532-9114275"}
    result = build_furniture(FurnitureWorkspace.model_validate(doc).design)
    if family == "chest_of_drawers":
        assert {
            p.material_id
            for p in result.parts
            if p.semantic_key.endswith("-bottom") and p.semantic_key.startswith("drawer-")
        } == {"birch-plywood-6"}
        assert {p.grain_direction for p in result.parts if p.role.value == "drawer_side"} == {
            GrainDirection.Y
        }
    else:
        legs = [p for p in result.parts if p.semantic_key.startswith("leg-")]
        assert len(legs) == 4 and {p.material_id for p in legs} == {"walnut-square-stock"}
        assert {p.material_id for p in result.parts if p not in legs} == {"solid-walnut"}


def test_walnut_modular_cut_list_tracks_every_piece_and_only_floors_get_plinths():
    w = walnut()
    g = FurnitureModuleGrid(
        columns=2, rows=2, gap_um=0, shelf_count_per_row=(2, 2), divider_count_per_module=2
    )
    assert "plinth_placement" not in g.model_dump()
    assert plan_furniture_modules(w, g)["code"] == "PLINTH_TRANSFORMATION_UNSUPPORTED"
    assert preview_furniture(w)["manufacturing"]["geometry_compatible"] is False
    p = plan_furniture_modules(w, g.model_copy(update={"plinth_placement": "floor_row_only"}))
    assert p["screening"]["all_stock_fit"] is True and p["screening"]["stock_issue_count"] == 0
    assert p["layout"]["column_widths_um"] == [2150000] * 2
    assert p["layout"]["row_heights_um"] == [1270000] * 2
    cut = list(csv.DictReader(io.StringIO(p["review_cut_list_csv"])))
    assert len(cut) == 62 == p["screening"]["part_count"]
    assert {r["grain_axis_uv"] for r in cut} == {"U", "V"}
    assert {r["plan_hash"] for r in cut} == {p["plan_hash"]}
    assert len({(r["row_from_floor"], r["column_from_left"], r["part_name"]) for r in cut}) == 62
    for module in p["modules"]:
        rows = [r for r in cut if r["module_id"] == module["module_id"]]
        assert {r["draft_part_id"] for r in rows} == {
            p["part_id"] for p in module["preview"]["design"]["parts"]
        }
        assert sum(r["part_name"] == "plinth" for r in rows) == int(module["row"] == 0)
        assert {r["material_id"] for r in rows} == {"solid-walnut", "birch-plywood-6"}
        assert module["workspace"]["design"]["installation"] == w.design.installation.model_dump(
            mode="json"
        )
        assert module["physical_cutting_authorized"] is False
    assert "FLOOR_PLINTH_SUPPORT_REQUIRED" in {r["code"] for r in p["requirements"]}


def test_too_narrow_or_stale_walnut_blanks_never_pass_the_module_fit_check():
    w = walnut().model_dump(mode="json")
    grid = FurnitureModuleGrid(
        columns=2,
        rows=2,
        gap_um=0,
        shelf_count_per_row=(2, 2),
        divider_count_per_module=2,
        plinth_placement="floor_row_only",
    )
    w["manufacturing"]["material_stocks"][0]["stock_height_um"] = 250_000
    too_narrow = plan_furniture_modules(FurnitureWorkspace.model_validate(w), grid)
    assert too_narrow["screening"]["all_stock_fit"] is False
    assert too_narrow["screening"]["stock_issue_count"] > 0
    w["manufacturing"]["material_stocks"][0]["measured_thickness_um"] = 17_900
    stock = preview_furniture(FurnitureWorkspace.model_validate(w))["manufacturing"]
    assert {i["code"] for i in stock["issues"]} >= {
        "MATERIAL_STOCK_NOT_USED",
        "SOLID_WOOD_BLANK_REQUIRED",
    }
