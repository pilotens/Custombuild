"""Read-only inspection of a single component of a hash-bound server preview."""

from typing import Annotated

from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import FurnitureResult, build_furniture
from custombuild_domain.models import DesignResult
from custombuild_domain.rear_coverage import ReferenceConstruction
from custombuild_manufacturing.adapters import adapt_domain_part
from custombuild_manufacturing.furniture_profiles import furniture_stock_assessment
from custombuild_manufacturing.nesting import stock_orientations
from custombuild_manufacturing.part_inspection import InspectionStock, PartInspection, inspect_part
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .api import (
    PrincipalDep,
    SessionDep,
    _canonical_preview_with_optional_retention,
    _rule_engine_error,
    _validation_error,
)
from .design_service import RuleEngineUnavailable, generation_stock_projection_for_design
from .repository import tenant_project
from .schemas import BookcasePreviewInput, RevisionProductionContext

router = APIRouter(prefix="/v1")
PartId = Annotated[str, Field(min_length=8, max_length=64)]
DesignHash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class InspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_id: PartId
    expected_design_hash: DesignHash


class InspectFurniture(InspectionRequest):
    workspace: FurnitureWorkspace


class InspectBookcase(InspectionRequest):
    construction: ReferenceConstruction | None = None
    production_context: RevisionProductionContext | None = None
    spec: BookcasePreviewInput


def _selected(result: DesignResult | FurnitureResult, request: InspectionRequest) -> PartInspection:
    if result.design_hash != request.expected_design_hash:
        raise HTTPException(409, "Modellen har ändrats. Öppna delen från den aktuella modellen.")
    part = next((part for part in result.parts if part.part_id == request.part_id), None)
    if part is None:
        raise HTTPException(404, "Delen finns inte i den här modellen.")
    stock: list[InspectionStock] = []
    if isinstance(request, InspectFurniture) and isinstance(result, FurnitureResult):
        plan = furniture_stock_assessment(request.workspace, result)
        for group in plan.get("stock_groups", []):
            blank = next((p for p in group["parts"] if p["part_id"] == part.part_id), None)
            if blank:
                fmt = group["stock"]
                stock.append(
                    InspectionStock(
                        source=group["selection_source"],
                        width_um=fmt["stock_width_um"],
                        height_um=fmt["stock_height_um"],
                        margin_um=fmt["edge_margin_um"],
                        grain=fmt["stock_grain_axis"],
                        fits=blank["fits_stock"],
                    )
                )
    if isinstance(request, InspectBookcase) and request.production_context is not None:
        sheets, _ = generation_stock_projection_for_design(
            result, request.production_context.model_dump(mode="json", exclude_none=True)
        )
        blank_part = adapt_domain_part(part)
        for sheet in sheets:
            if (sheet.material_id, sheet.material_version, sheet.thickness_um) != (
                part.material_id,
                part.material_version,
                part.actual_thickness_um,
            ):
                continue
            usable = sheet.usable_rect
            stock.append(
                InspectionStock(
                    source=sheet.declaration_authority,
                    width_um=sheet.width_um,
                    height_um=sheet.height_um,
                    margin_um=sheet.margin_um,
                    grain=sheet.grain_direction,
                    fits=any(
                        width <= usable.width_um and height <= usable.height_um
                        for _, width, height in stock_orientations(blank_part, sheet)
                    ),
                )
            )
    return inspect_part(
        result,
        part,
        request.construction if isinstance(request, InspectBookcase) else None,
        stock=tuple(stock),
    )


@router.post("/furniture/inspect-part", response_model=PartInspection)
def inspect_furniture(payload: InspectFurniture, principal: PrincipalDep) -> PartInspection:
    try:
        return _selected(build_furniture(payload.workspace.design), payload)
    except ValueError as exc:
        raise _validation_error(exc) from exc


@router.post("/designs/inspect-part", response_model=PartInspection)
def inspect_bookcase(
    payload: InspectBookcase,
    session: SessionDep,
    principal: PrincipalDep,
    project_id: str | None = None,
    joint_retention_evidence_id: str | None = None,
) -> PartInspection:
    project = tenant_project(session, principal, project_id) if project_id else None
    try:
        _, result, _, _ = _canonical_preview_with_optional_retention(
            session,
            principal.organization_id,
            project,
            payload.spec,
            design_id=project.id if project else "preview",
            revision=project.current_revision + 1 if project else 1,
            evidence_id=joint_retention_evidence_id,
        )
        return _selected(result, payload)
    except ValueError as exc:
        raise _validation_error(exc) from exc
    except RuleEngineUnavailable as exc:
        raise _rule_engine_error(exc) from exc
