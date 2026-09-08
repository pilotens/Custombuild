"""Lossless adaptation of saved furniture revisions to the existing production API."""

from __future__ import annotations

from typing import Any

from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_production import (
    FurnitureProductionSource,
    assert_furniture_production_spec,
    furniture_production_source,
    shelving_production_spec,
)
from fastapi import HTTPException

from .design_service import normalize_preview
from .models import Project
from .schemas import BookcasePreviewInput


def saved_furniture_workspace(project: Project) -> FurnitureWorkspace:
    if (project.draft_spec_json or {}).get("schema_version") != "custombuild.furniture-design.v1":
        raise HTTPException(409, detail="Projektet saknar en sparad möbelrevision.")
    return FurnitureWorkspace.model_validate(
        {
            "design": project.draft_spec_json,
            "manufacturing": (project.draft_workspace_json or {}).get("manufacturing"),
        }
    )


def furniture_production_input(workspace: FurnitureWorkspace) -> BookcasePreviewInput:
    spec = shelving_production_spec(workspace)
    p = spec.parameters
    result = BookcasePreviewInput(
        width_mm=p.width_um / 1_000,
        height_mm=p.height_um / 1_000,
        depth_mm=p.depth_um / 1_000,
        material_id=spec.material.material_id,
        back_material_id=spec.back_material.material_id if spec.back_material else None,
        measured_thickness_mm=p.actual_thickness_um / 1_000,
        measured_back_thickness_mm=p.back_thickness_um / 1_000,
        shelf_count=p.shelf_count,
        divider_count=p.vertical_divider_count,
        shelf_height_ratios=[v / 1_000_000 for v in p.shelf_height_ratios_ppm],
        load_per_shelf_kg=p.shelf_load_n / 9.80665,
        back_panel="inset_groove",
        plinth=False,
        plinth_height_mm=0,
    )
    normalized = normalize_preview(
        result.model_dump(exclude_none=True), design_id=spec.design_id, revision=spec.revision
    )
    if normalized != spec:
        raise ValueError("furniture production conversion changed canonical inputs")
    return result


def bind_saved_furniture_source(
    project: Project,
    source: FurnitureProductionSource | None,
    requested: BookcasePreviewInput,
    template_id: str,
) -> dict[str, Any] | None:
    is_furniture = (project.draft_spec_json or {}).get("schema_version") == (
        "custombuild.furniture-design.v1"
    )
    if not is_furniture and source is None:
        return None
    if source is None or not is_furniture or template_id != "shelving":
        raise HTTPException(
            409, detail="Tillverkningsrevisionen måste bindas till sparat hyllsystem."
        )
    workspace = saved_furniture_workspace(project)
    current = furniture_production_source(workspace)
    if (
        source != current
        or workspace.design.design_id != project.id
        or workspace.design.revision != project.draft_revision
        or source.furniture_design_hash != project.draft_design_hash
    ):
        raise HTTPException(
            409, detail="Möbeln eller profilerna har ändrats. Öppna beredningen på nytt."
        )
    spec = normalize_preview(
        requested.model_dump(exclude_none=True),
        design_id=project.id,
        revision=project.current_revision + 1,
    )
    try:
        assert_furniture_production_spec(current, spec)
    except ValueError as exc:
        raise HTTPException(409, detail="Beredningen ändrar den sparade möbeln.") from exc
    return current.model_dump(mode="json")
