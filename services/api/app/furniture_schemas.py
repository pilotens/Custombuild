"""Public furniture response contracts; reuse authoritative domain geometry."""

from datetime import datetime
from typing import Annotated, Literal

from custombuild_domain.furniture import (
    FurnitureWorkspace,
    InstallationSpace,
)
from custombuild_domain.furniture_catalog import HardwareProfile
from custombuild_domain.furniture_engine import FurnitureResult
from custombuild_domain.models import MaterialVersion
from pydantic import BaseModel, ConfigDict, Field

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Family = Literal["shelving", "table", "chest_of_drawers"]
ScreeningStatus = Literal["PASS", "WARNING", "BLOCK"]


class FurnitureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FurnitureFamilyRead(FurnitureResponse):
    id: Family
    name: str
    geometry_supported: bool
    qualification: Literal["structural_screening", "layout_only"]


class FurnitureMachineRead(FurnitureResponse):
    profile_id: str
    version: str
    name: str
    fingerprint: Digest
    work_width_um: int
    work_height_um: int
    production_qualified: Literal[False]


class FurnitureCatalogRead(FurnitureResponse):
    version: str
    fingerprint: Digest
    families: list[FurnitureFamilyRead]
    materials: list[MaterialVersion]
    hardware: list[HardwareProfile]
    machines: list[FurnitureMachineRead]


class FurnitureRuleRead(FurnitureResponse):
    rule_id: str
    title: str
    status: ScreeningStatus
    detail: str
    values: dict[str, int | float | str | None]
    rules_version: str


class FurnitureRulesRead(FurnitureResponse):
    rules_version: str
    design_hash: Digest
    overall_status: ScreeningStatus
    evaluations: list[FurnitureRuleRead]
    production_qualified: Literal[False]
    disclaimer: str


class FurnitureIssueRead(FurnitureResponse):
    code: str
    message: str
    part_id: str | None = None
    material_id: str | None = None
    material_version: str | None = None
    measured_thickness_um: int | None = None


class FurnitureBlankRead(FurnitureResponse):
    part_id: str
    semantic_key: str
    raw_width_um: int
    raw_height_um: int
    grain_direction: Literal["X", "Y", "Z", "NONE"]
    along_grain_um: int | None
    across_grain_um: int | None


class FurnitureOrientationRead(FurnitureResponse):
    rotation_deg: Literal[0, 90]
    required_stock_width_um: int
    required_stock_height_um: int
    width_shortfall_um: int
    height_shortfall_um: int
    fits_stock: bool
    fits_machine: bool


class FurnitureBlankFitRead(FurnitureBlankRead):
    fits_stock: bool | None
    orientations: list[FurnitureOrientationRead]


class FurnitureRequiredFormatRead(FurnitureResponse):
    stock_width_um: int
    stock_height_um: int
    fits_machine: bool


class FurnitureStockFormatRead(FurnitureResponse):
    stock_width_um: int
    stock_height_um: int
    stock_grain_axis: Literal["x", "y"] | None
    edge_margin_um: int


class FurnitureStockGroupRead(FurnitureResponse):
    material_id: str
    material_version: str
    measured_thickness_um: int
    selection_source: Literal["default", "material"]
    stock: FurnitureStockFormatRead
    usable_width_um: int
    usable_height_um: int
    geometry_compatible: bool
    parts: list[FurnitureBlankFitRead]
    required_formats: list[FurnitureRequiredFormatRead]
    issues: list[FurnitureIssueRead]


class FurnitureManufacturingRead(FurnitureResponse):
    state: Literal["not_selected", "requires_change", "not_qualified"]
    geometry_compatible: bool | None
    fingerprint: Digest | None
    issues: list[FurnitureIssueRead]
    production_qualified: Literal[False]
    detail: str | None = None
    stock_groups: list[FurnitureStockGroupRead] | None = None
    machine_work_area_um: dict[str, int] | None = None
    format_scope: str | None = None


class FurnitureTrimClearanceRead(FurnitureResponse):
    wall: Literal["left", "right", "rear"]
    required_clearance_um: int | None
    reserved_clearance_um: int | None
    height_um: int | None


class FurnitureDimensionsRead(FurnitureResponse):
    version: str
    state: Literal["requires_resolution", "reconciled", "not_specified"]
    carcass_dimensions_um: dict[str, int]
    required_carcass_dimensions_um: dict[str, int] | None
    room_trim_clearances: list[FurnitureTrimClearanceRead]
    installation: InstallationSpace | None
    issues: list[FurnitureIssueRead]
    production_qualified: Literal[False]


class FurnitureStockRequirementRead(FurnitureResponse):
    material_id: str
    material_version: str
    measured_thickness_um: int
    part_count: int
    raw_area_um2: int
    minimum_long_edge_um: int
    minimum_short_edge_um: int
    minimum_along_grain_um: int
    minimum_across_grain_um: int
    parts: list[FurnitureBlankRead]


class FurnitureShelfLoadRead(FurnitureResponse):
    basis: Literal["per_row", "per_metre"]
    total_row_load_n: int
    load_per_metre_n: int | None
    width_um: int


class FurnitureHandoffRead(FurnitureResponse):
    schema_version: Literal["custombuild.furniture-workshop-handoff.v1"]
    version: str
    design_hash: Digest
    family: Family
    dimensions: FurnitureDimensionsRead
    stock_requirements: list[FurnitureStockRequirementRead]
    shelf_load: FurnitureShelfLoadRead | None
    hardware_requirements: list[str]
    required_workshop_inputs: list[str]
    scope: str
    production_qualified: Literal[False]
    physical_cutting_authorized: Literal[False]
    stock_plan: FurnitureManufacturingRead


class FurniturePreviewRead(FurnitureResponse):
    schema_version: Literal["custombuild.furniture-preview.v1"]
    workspace: FurnitureWorkspace
    design: FurnitureResult
    dependencies: dict[str, Digest]
    rules: FurnitureRulesRead
    manufacturing: FurnitureManufacturingRead
    workshop_handoff: FurnitureHandoffRead
    production_qualified: Literal[False]
    physical_cutting_authorized: Literal[False]


class FurnitureProfileChangeRead(FurnitureResponse):
    state: Literal["requires_change", "not_qualified"]
    message: str
    proposed: FurniturePreviewRead | None
    original_design_hash: Digest
    changed_dependencies: list[str]
    invalidated_reviews: list[str]
    can_apply: bool
    production_qualified: Literal[False]
    changed_part_ids: list[str] | None = None
    intent_preserved: bool | None = None


class FurnitureDraftRead(FurnitureResponse):
    project_id: str
    revision: int = Field(ge=0)
    workspace: FurnitureWorkspace | None
    preview: FurniturePreviewRead | None


class FurnitureBayDimensionsRead(FurnitureResponse):
    divider_count: int
    bay_count: int
    clear_widths_um: list[int]


class FurnitureChangedFieldRead(FurnitureResponse):
    field: str
    before: int
    after: int


class FurnitureScreeningCheckRead(FurnitureResponse):
    rule_id: str
    status: Literal["PASS", "WARNING", "BLOCK", "UNAVAILABLE"]
    numeric_status: Literal["PASS", "WARNING", "BLOCK", "UNAVAILABLE"]
    calculated: int | float | None
    allowed: int | float | None
    unit: str | None


class FurnitureScreeningComparisonRead(FurnitureResponse):
    current: list[FurnitureScreeningCheckRead]
    proposed: list[FurnitureScreeningCheckRead] | None


class FurnitureSuggestionSearchRead(FurnitureResponse):
    max_divider_count: int
    attempted_candidate_count: int
    evaluated_candidate_count: int


class FurnitureRemainingIssuesRead(FurnitureResponse):
    rules: list[FurnitureRuleRead]
    manufacturing: list[FurnitureIssueRead]


class FurnitureShelfSuggestionRead(FurnitureResponse):
    schema_version: Literal["custombuild.furniture-shelf-suggestion.v1"]
    version: str
    state: Literal["available", "already_pass", "unavailable"]
    code: str
    message: str
    current: FurniturePreviewRead
    proposed: FurniturePreviewRead | None
    changed_fields: list[FurnitureChangedFieldRead]
    current_bays: FurnitureBayDimensionsRead | None
    proposed_bays: FurnitureBayDimensionsRead | None
    screening_checks: FurnitureScreeningComparisonRead
    search: FurnitureSuggestionSearchRead
    remaining_issues: FurnitureRemainingIssuesRead
    scope: str
    can_apply: bool
    production_qualified: Literal[False]
    physical_cutting_authorized: Literal[False]


class FurnitureHistoryItemRead(FurnitureResponse):
    id: str
    created_at: datetime
    revision: int
    workspace: FurnitureWorkspace
    design_hash: Digest
    engine_version: str
    dependencies: dict[str, Digest]


class FurnitureHistoryRead(FurnitureResponse):
    items: list[FurnitureHistoryItemRead]
    next_offset: int | None
