from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal

from custombuild_manufacturing import MAX_ARTIFACT_BYTES, MAX_CATALOG_SOURCE_BYTES
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from .models import DesignStatus, JobStatus
from .workshop_readiness_service import WorkshopPreparationBlockerCode


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    description: str = Field(default="", max_length=4000)
    furniture_type: Literal["bookcase"] = "bookcase"


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    furniture_type: str
    current_revision: int
    archived: bool
    created_at: datetime
    updated_at: datetime


WORKSPACE_INTENT_SCHEMA_V1 = "custombuild.workspace-intent.v1"
MAX_WORKSPACE_INTENT_BYTES = 128 * 1024
MAX_WORKSPACE_CUSTOM_PART_IDS = 1_024
RATIO_COMPARISON_TOLERANCE = 1e-9

WorkspacePartId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128),
]
WorkspaceWarning = Annotated[
    str,
    StringConstraints(strict=True, max_length=500),
]
WorkspaceFingerprint = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$"),
]
WorkspacePartExtent = Annotated[float, Field(strict=True, ge=1, le=6_000)]
WorkspacePartThickness = Annotated[float, Field(strict=True, ge=1, le=100)]
WorkspacePartPositionX = Annotated[float, Field(strict=True, ge=0, le=6_000)]
WorkspacePartPositionY = Annotated[float, Field(strict=True, ge=0, le=1_200)]
WorkspacePartPositionZ = Annotated[float, Field(strict=True, ge=0, le=4_000)]


class _StrictWorkspaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class WorkspaceProductionContext(_StrictWorkspaceModel):
    stock_width_mm: float = Field(gt=0, le=10_000)
    stock_height_mm: float = Field(gt=0, le=5_000)
    stock_count: int = Field(ge=1, le=100)
    back_stock_width_mm: float = Field(gt=0, le=10_000)
    back_stock_height_mm: float = Field(gt=0, le=5_000)
    back_stock_count: int = Field(ge=1, le=100)
    machine_profile_id: Literal[
        "custombuild-router-1325-linuxcnc",
        "custombuild-router-5125-linuxcnc",
    ]


class WorkspacePartOverride(_StrictWorkspaceModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        json_schema_extra={"minProperties": 1},
    )

    width_mm: WorkspacePartExtent | SkipJsonSchema[None] = None
    depth_mm: WorkspacePartExtent | SkipJsonSchema[None] = None
    thickness_mm: WorkspacePartThickness | SkipJsonSchema[None] = None
    position_x_mm: WorkspacePartPositionX | SkipJsonSchema[None] = None
    position_y_mm: WorkspacePartPositionY | SkipJsonSchema[None] = None
    position_z_mm: WorkspacePartPositionZ | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("part override fields cannot be null")
        return value

    @model_validator(mode="after")
    def require_an_override(self) -> WorkspacePartOverride:
        if not self.model_dump(exclude_none=True):
            raise ValueError("part override must contain at least one bounded field")
        return self


class WorkspaceReferenceConfirmedInputs(_StrictWorkspaceModel):
    dimensions_measured: bool
    layout_confirmed: bool
    material_confirmed: bool
    construction_assumptions_confirmed: bool


def _default_workspace_reference_confirmations() -> WorkspaceReferenceConfirmedInputs:
    return WorkspaceReferenceConfirmedInputs(
        dimensions_measured=False,
        layout_confirmed=False,
        material_confirmed=False,
        construction_assumptions_confirmed=False,
    )


class WorkspaceReferenceImageImport(_StrictWorkspaceModel):
    source: Literal["reference_image"]
    import_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_name: str = Field(min_length=1, max_length=120)
    image_width_px: int = Field(ge=160, le=20_000)
    image_height_px: int = Field(ge=160, le=20_000)
    confidence: float = Field(ge=0, le=1)
    detected_shelves: int = Field(ge=0, le=40)
    detected_dividers: int = Field(ge=0, le=16)
    detected_base_cabinets: bool
    warnings: list[WorkspaceWarning] = Field(max_length=20)
    verification_status: Literal["concept", "parametric_confirmed"] = "concept"
    confirmed_inputs: WorkspaceReferenceConfirmedInputs = Field(
        default_factory=_default_workspace_reference_confirmations
    )
    verified_model_fingerprint: WorkspaceFingerprint | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_fingerprint_null(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("verified_model_fingerprint", ...) is None:
            raise ValueError("verified_model_fingerprint cannot be null")
        return value


def _validate_workspace_ratios(
    *,
    divider_count: int,
    shelf_count: int,
    bay_width_ratios: list[float],
    shelf_height_ratios: list[float],
) -> None:
    if bay_width_ratios:
        if len(bay_width_ratios) != divider_count + 1:
            raise ValueError("bay_width_ratios must match divider_count + 1")
        total = sum(bay_width_ratios)
        if total <= 0 or any(value <= 0 or value / total < 0.08 for value in bay_width_ratios):
            raise ValueError("every custom bay must be at least 8 percent of the inner width")
    if shelf_height_ratios:
        if len(shelf_height_ratios) != shelf_count:
            raise ValueError("shelf_height_ratios must match shelf_count")
        if any(
            value < 0.05 - RATIO_COMPARISON_TOLERANCE
            or value > 0.95 + RATIO_COMPARISON_TOLERANCE
            or (
                index > 0
                and value - shelf_height_ratios[index - 1]
                < 0.05 - RATIO_COMPARISON_TOLERANCE
            )
            for index, value in enumerate(shelf_height_ratios)
        ):
            raise ValueError(
                "custom shelf levels must be ordered and separated by at least 5 percent"
            )


class WorkspaceTopologyBaseline(_StrictWorkspaceModel):
    divider_count: int = Field(ge=0, le=16)
    shelf_count: int = Field(ge=0, le=40)
    base_cabinet_count: int = Field(ge=0, le=17)
    bay_width_ratios: list[float] = Field(max_length=17)
    shelf_height_ratios: list[float] = Field(max_length=40)
    reinforcement_mode: Literal["manual", "auto"]

    @model_validator(mode="after")
    def validate_layout(self) -> WorkspaceTopologyBaseline:
        _validate_workspace_ratios(
            divider_count=self.divider_count,
            shelf_count=self.shelf_count,
            bay_width_ratios=self.bay_width_ratios,
            shelf_height_ratios=self.shelf_height_ratios,
        )
        return self


class WorkspaceIntentV1(_StrictWorkspaceModel):
    schema_version: Literal["custombuild.workspace-intent.v1"]
    bay_sizing_mode: Literal["count", "target_width"]
    target_bay_width_mm: float = Field(ge=50, le=2_000)
    symmetry_locked: bool
    production_context: WorkspaceProductionContext
    part_overrides: dict[WorkspacePartId, WorkspacePartOverride] = Field(max_length=1_024)
    removed_part_ids: list[WorkspacePartId] = Field(max_length=1_024)
    reference_image_import: WorkspaceReferenceImageImport | SkipJsonSchema[None] = None
    topology_baseline: WorkspaceTopologyBaseline | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_optional_nulls(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(
            value.get(field, ...) is None
            for field in ("reference_image_import", "topology_baseline")
        ):
            raise ValueError("optional workspace intent objects cannot be null")
        return value

    @model_validator(mode="after")
    def validate_custom_part_resources(self) -> WorkspaceIntentV1:
        if len(self.part_overrides) + len(self.removed_part_ids) > MAX_WORKSPACE_CUSTOM_PART_IDS:
            raise ValueError("workspace intent exceeds the 1024 custom-part resource limit")
        if len(self.removed_part_ids) != len(set(self.removed_part_ids)):
            raise ValueError("removed_part_ids must be unique")
        return self


class BookcasePreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width_mm: float = Field(default=900, ge=250, le=6000)
    height_mm: float = Field(default=2000, ge=300, le=4000)
    depth_mm: float = Field(default=320, ge=100, le=1200)
    furniture_type: Literal["bookcase", "wall_library"] = "bookcase"
    material_id: Literal["mdf", "birch-plywood"] = "mdf"
    back_material_id: Literal["mdf-6", "birch-plywood-6"] | None = Field(
        default=None,
        description=(
            "Optional exact 6 mm back-panel material. When omitted, legacy clients "
            "retain the matching MDF/birch-plywood derivation from material_id."
        ),
    )
    nominal_thickness_mm: float = Field(
        default=18,
        ge=18,
        le=18,
        description="The production MVP catalogue contains nominal 18 mm carcass sheets.",
    )
    measured_thickness_mm: float = Field(
        default=18,
        ge=17,
        le=19,
        description="MVP catalogue range for nominal 18 mm carcass sheet material.",
    )
    shelf_count: int = Field(default=5, ge=0, le=40)
    shelf_mount: Literal["fixed", "adjustable"] = "fixed"
    load_per_shelf_kg: float = Field(default=30, ge=0, le=500)
    back_panel: bool | Literal["none", "surface_mounted", "inset_groove"] = True
    plinth: bool = True
    plinth_height_mm: float | None = Field(default=None, ge=0, le=500)
    divider_count: int = Field(default=0, ge=0, le=16)
    bay_width_ratios: list[float] = Field(default_factory=list, max_length=17)
    shelf_height_ratios: list[float] = Field(default_factory=list, max_length=40)
    base_cabinet_height_mm: float = Field(default=0, ge=0, le=2000)
    base_cabinet_depth_mm: float = Field(default=0, ge=0, le=1200)
    base_cabinet_count: int = Field(default=0, ge=0, le=17)
    edge_band_mm: float = Field(default=1, ge=0, le=5)
    joint_system: Literal["dado"] = Field(
        default="dado",
        description=(
            "Only DADO has an implemented deterministic design-review path in the MVP. "
            "CAM remains conditional on authenticated retention evidence and all other "
            "manufacturing gates; other joint types are capability-blocked."
        ),
    )
    reinforcement_mode: Literal["manual", "auto"] = "manual"
    wall_anchor_required: bool = False
    wall_anchor_verified: bool = False

    @model_validator(mode="after")
    def validate_custom_layout(self) -> BookcasePreviewInput:
        if not _legacy_back_panel_enabled(self.back_panel) and self.back_material_id is not None:
            raise ValueError("back_material_id requires an enabled back panel")
        if self.plinth_height_mm is not None and (
            self.plinth != (self.plinth_height_mm > 0)
        ):
            raise ValueError(
                "plinth must be true exactly when explicit plinth_height_mm is greater than zero"
            )
        if self.bay_width_ratios:
            if len(self.bay_width_ratios) != self.divider_count + 1:
                raise ValueError("bay_width_ratios must match divider_count + 1")
            total = sum(self.bay_width_ratios)
            if total <= 0 or any(
                value <= 0 or value / total < 0.08 for value in self.bay_width_ratios
            ):
                raise ValueError("every custom bay must be at least 8 percent of the inner width")
        if self.shelf_height_ratios:
            if len(self.shelf_height_ratios) != self.shelf_count:
                raise ValueError("shelf_height_ratios must match shelf_count")
            if any(
                value < 0.05 - RATIO_COMPARISON_TOLERANCE
                or value > 0.95 + RATIO_COMPARISON_TOLERANCE
                or (
                    index > 0
                    and value - self.shelf_height_ratios[index - 1]
                    < 0.05 - RATIO_COMPARISON_TOLERANCE
                )
                for index, value in enumerate(self.shelf_height_ratios)
            ):
                raise ValueError(
                    "custom shelf levels must be ordered and separated by at least 5 percent"
                )
        return self


class _LegacyWorkspaceSpec(_StrictWorkspaceModel):
    """Internal, one-way migration parser for the former full DesignSpec draft."""

    schema_version: Literal["1.0"]
    design_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=0, le=1_000_000_000)
    furniture_type: Literal["bookcase", "wall_library"]
    width_mm: float = Field(ge=250, le=6_000)
    height_mm: float = Field(ge=300, le=4_000)
    depth_mm: float = Field(ge=100, le=1_200)
    material_id: Literal["mdf", "birch-plywood"]
    material_name: str = Field(min_length=1, max_length=160)
    nominal_thickness_mm: float = Field(ge=18, le=18)
    measured_thickness_mm: float = Field(ge=17, le=19)
    shelf_count: int = Field(ge=0, le=40)
    fixed_shelves: bool
    load_per_shelf_kg: float = Field(ge=0, le=500)
    back_panel: bool
    plinth: bool
    divider_count: int = Field(ge=0, le=16)
    bay_sizing_mode: Literal["count", "target_width"]
    target_bay_width_mm: float = Field(ge=50, le=2_000)
    bay_width_ratios: list[float] = Field(max_length=17)
    shelf_height_ratios: list[float] = Field(max_length=40)
    symmetry_locked: bool
    reference_image_import: WorkspaceReferenceImageImport | None = None
    part_overrides: dict[WorkspacePartId, WorkspacePartOverride] = Field(max_length=1_024)
    removed_part_ids: list[WorkspacePartId] = Field(max_length=1_024)
    topology_baseline: WorkspaceTopologyBaseline | None = None
    base_cabinet_height_mm: float = Field(ge=0, le=2_000)
    base_cabinet_depth_mm: float = Field(ge=0, le=1_200)
    base_cabinet_count: int = Field(ge=0, le=17)
    reinforcement_mode: Literal["manual", "auto"]
    joint_system: Literal["dado"]
    edge_band_mm: float = Field(ge=0, le=5)
    wall_anchor_verified: bool
    stock_width_mm: float = Field(gt=0, le=10_000)
    stock_height_mm: float = Field(gt=0, le=5_000)
    stock_count: int = Field(ge=1, le=100)
    back_stock_width_mm: float = Field(gt=0, le=10_000)
    back_stock_height_mm: float = Field(gt=0, le=5_000)
    back_stock_count: int = Field(ge=1, le=100)
    machine_profile_id: Literal[
        "custombuild-router-1325-linuxcnc",
        "custombuild-router-5125-linuxcnc",
    ]

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_optional_nulls(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(
            value.get(field, ...) is None
            for field in ("reference_image_import", "topology_baseline")
        ):
            raise ValueError("optional legacy workspace objects cannot be null")
        return value

    @model_validator(mode="after")
    def validate_legacy_intent(self) -> _LegacyWorkspaceSpec:
        _validate_workspace_ratios(
            divider_count=self.divider_count,
            shelf_count=self.shelf_count,
            bay_width_ratios=self.bay_width_ratios,
            shelf_height_ratios=self.shelf_height_ratios,
        )
        if len(self.part_overrides) + len(self.removed_part_ids) > MAX_WORKSPACE_CUSTOM_PART_IDS:
            raise ValueError("legacy workspace exceeds the 1024 custom-part resource limit")
        if len(self.removed_part_ids) != len(set(self.removed_part_ids)):
            raise ValueError("legacy removed_part_ids must be unique")
        return self


def _workspace_json_size(value: Any) -> int:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("workspace_spec must be finite JSON data") from exc
    return len(encoded.encode("utf-8"))


def _legacy_back_panel_enabled(
    value: bool | Literal["none", "surface_mounted", "inset_groove"],
) -> bool:
    return value is True or value in {"surface_mounted", "inset_groove"}


def _canonical_back_panel_type(
    value: bool | Literal["none", "surface_mounted", "inset_groove"],
) -> Literal["none", "surface_mounted", "inset_groove"]:
    if value is True:
        return "inset_groove"
    if value is False:
        return "none"
    return value


def _legacy_production_mismatches(
    legacy: _LegacyWorkspaceSpec,
    spec: BookcasePreviewInput,
) -> list[str]:
    comparisons: dict[str, tuple[Any, Any]] = {
        "furniture_type": (legacy.furniture_type, spec.furniture_type),
        "width_mm": (legacy.width_mm, spec.width_mm),
        "height_mm": (legacy.height_mm, spec.height_mm),
        "depth_mm": (legacy.depth_mm, spec.depth_mm),
        "material_id": (legacy.material_id, spec.material_id),
        "back_material_id": (
            (
                "birch-plywood-6"
                if legacy.back_panel and legacy.material_id == "birch-plywood"
                else "mdf-6"
                if legacy.back_panel
                else None
            ),
            (
                spec.back_material_id
                or (
                    "birch-plywood-6"
                    if _legacy_back_panel_enabled(spec.back_panel)
                    and spec.material_id == "birch-plywood"
                    else "mdf-6"
                    if _legacy_back_panel_enabled(spec.back_panel)
                    else None
                )
            ),
        ),
        "nominal_thickness_mm": (legacy.nominal_thickness_mm, spec.nominal_thickness_mm),
        "measured_thickness_mm": (legacy.measured_thickness_mm, spec.measured_thickness_mm),
        "shelf_count": (legacy.shelf_count, spec.shelf_count),
        "shelf_mount": (
            "fixed" if legacy.fixed_shelves else "adjustable",
            spec.shelf_mount,
        ),
        "load_per_shelf_kg": (legacy.load_per_shelf_kg, spec.load_per_shelf_kg),
        "back_panel": (legacy.back_panel, _legacy_back_panel_enabled(spec.back_panel)),
        "back_panel_type": (
            "inset_groove" if legacy.back_panel else "none",
            _canonical_back_panel_type(spec.back_panel),
        ),
        "plinth": (legacy.plinth, spec.plinth),
        "divider_count": (legacy.divider_count, spec.divider_count),
        "bay_width_ratios": (legacy.bay_width_ratios, spec.bay_width_ratios),
        "shelf_height_ratios": (legacy.shelf_height_ratios, spec.shelf_height_ratios),
        "base_cabinet_height_mm": (
            legacy.base_cabinet_height_mm,
            spec.base_cabinet_height_mm,
        ),
        "base_cabinet_depth_mm": (legacy.base_cabinet_depth_mm, spec.base_cabinet_depth_mm),
        "base_cabinet_count": (legacy.base_cabinet_count, spec.base_cabinet_count),
        "edge_band_mm": (legacy.edge_band_mm, spec.edge_band_mm),
        "joint_system": (legacy.joint_system, spec.joint_system),
        "reinforcement_mode": (legacy.reinforcement_mode, spec.reinforcement_mode),
        "wall_anchor_verified": (legacy.wall_anchor_verified, spec.wall_anchor_verified),
        "plinth_height_mm": (None, spec.plinth_height_mm),
        "wall_anchor_required": (False, spec.wall_anchor_required),
    }
    return [
        name
        for name, (legacy_value, spec_value) in comparisons.items()
        if legacy_value != spec_value
    ]


def _migrate_legacy_workspace(legacy: _LegacyWorkspaceSpec) -> dict[str, Any]:
    migrated: dict[str, Any] = {
        "schema_version": WORKSPACE_INTENT_SCHEMA_V1,
        "bay_sizing_mode": legacy.bay_sizing_mode,
        "target_bay_width_mm": legacy.target_bay_width_mm,
        "symmetry_locked": legacy.symmetry_locked,
        "production_context": {
            "stock_width_mm": legacy.stock_width_mm,
            "stock_height_mm": legacy.stock_height_mm,
            "stock_count": legacy.stock_count,
            "back_stock_width_mm": legacy.back_stock_width_mm,
            "back_stock_height_mm": legacy.back_stock_height_mm,
            "back_stock_count": legacy.back_stock_count,
            "machine_profile_id": legacy.machine_profile_id,
        },
        "part_overrides": {
            part_id: override.model_dump(mode="json", exclude_none=True)
            for part_id, override in legacy.part_overrides.items()
        },
        "removed_part_ids": legacy.removed_part_ids,
    }
    if legacy.reference_image_import is not None:
        migrated["reference_image_import"] = legacy.reference_image_import.model_dump(
            mode="json", exclude_none=True
        )
    if legacy.topology_baseline is not None:
        migrated["topology_baseline"] = legacy.topology_baseline.model_dump(mode="json")
    return migrated


def _generated_workspace_part_ids(spec: BookcasePreviewInput) -> set[str]:
    part_ids = {"side-left", "side-right", "bottom", "top"}
    part_ids.update(f"divider-{divider}" for divider in range(1, spec.divider_count + 1))
    part_ids.update(
        f"shelf-{shelf}-bay-{bay}"
        for shelf in range(1, spec.shelf_count + 1)
        for bay in range(1, spec.divider_count + 2)
    )
    if spec.furniture_type == "wall_library":
        part_ids.update(
            f"base-side-{boundary}" for boundary in range(2, spec.base_cabinet_count + 1)
        )
        for cabinet in range(1, spec.base_cabinet_count + 1):
            part_ids.add(f"base-bottom-{cabinet}")
            part_ids.add(f"cabinet-front-{cabinet}")
    if _legacy_back_panel_enabled(spec.back_panel):
        if spec.back_panel != "surface_mounted" and spec.divider_count > 0:
            part_ids.update(f"back-panel-bay-{bay}" for bay in range(1, spec.divider_count + 2))
        else:
            part_ids.add("back-panel")
    if spec.plinth:
        part_ids.add("plinth-front")
    return part_ids


class ProjectDraftUpdate(BaseModel):
    """A mutable workspace draft with a bounded, versioned UI-intent envelope."""

    model_config = ConfigDict(extra="forbid")

    expected_draft_revision: int = Field(ge=0)
    template_id: str = Field(min_length=1, max_length=80)
    spec: BookcasePreviewInput
    workspace_spec: WorkspaceIntentV1

    @model_validator(mode="before")
    @classmethod
    def migrate_explicit_legacy_workspace(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        workspace = value.get("workspace_spec")
        if _workspace_json_size(workspace) > MAX_WORKSPACE_INTENT_BYTES:
            raise ValueError("workspace_spec exceeds the 128 KiB draft limit")
        if not isinstance(workspace, dict):
            return value
        schema_version = workspace.get("schema_version")
        if schema_version == WORKSPACE_INTENT_SCHEMA_V1:
            return value
        if schema_version != "1.0":
            raise ValueError("workspace_spec requires a known, explicit schema_version")
        spec = BookcasePreviewInput.model_validate(value.get("spec"))
        legacy = _LegacyWorkspaceSpec.model_validate(workspace)
        mismatches = _legacy_production_mismatches(legacy, spec)
        if mismatches:
            raise ValueError(
                "legacy workspace production fields do not match spec: " + ", ".join(mismatches)
            )
        normalized = dict(value)
        normalized["workspace_spec"] = _migrate_legacy_workspace(legacy)
        return normalized

    @model_validator(mode="after")
    def validate_normalized_workspace(self) -> ProjectDraftUpdate:
        normalized = self.workspace_spec.model_dump(mode="json", exclude_none=True)
        if _workspace_json_size(normalized) > MAX_WORKSPACE_INTENT_BYTES:
            raise ValueError("normalized workspace_spec exceeds the 128 KiB draft limit")
        allowed_part_ids = _generated_workspace_part_ids(self.spec)
        requested_part_ids = {
            *self.workspace_spec.part_overrides,
            *self.workspace_spec.removed_part_ids,
        }
        if not requested_part_ids.issubset(allowed_part_ids):
            raise ValueError("workspace intent references an unknown generated part ID")
        return self


class ProjectDraftRead(BaseModel):
    project_id: str
    draft_revision: int
    template_id: str | None
    design_hash: str | None
    spec_json: dict[str, Any] | None
    workspace_spec_json: dict[str, Any] | None
    result_json: dict[str, Any] | None
    updated_at: datetime


class ReferenceImageConfirmedInputs(BaseModel):
    dimensions_measured: Literal[True]
    layout_confirmed: Literal[True]
    material_confirmed: Literal[True]
    construction_assumptions_confirmed: Literal[True]


class ReferenceImageSourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["reference_image"]
    import_id: str = Field(min_length=36, max_length=36)
    image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_name: str = Field(min_length=1, max_length=120)
    image_width_px: int = Field(ge=160, le=20_000)
    image_height_px: int = Field(ge=160, le=20_000)
    confidence: float = Field(ge=0, le=1)
    detected_shelves: int = Field(ge=0, le=40)
    detected_dividers: int = Field(ge=0, le=16)
    detected_base_cabinets: bool
    warnings: list[str] = Field(default_factory=list, max_length=20)
    verification_status: Literal["parametric_confirmed"]
    confirmed_inputs: ReferenceImageConfirmedInputs
    verified_model_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_warning_size(self) -> ReferenceImageSourceProvenance:
        if any(len(warning) > 500 for warning in self.warnings):
            raise ValueError("reference-image warnings must be at most 500 characters")
        return self


class RevisionProductionContext(BaseModel):
    """Production-affecting choices frozen when a design revision is created."""

    model_config = ConfigDict(extra="forbid")

    stock_width_mm: float = Field(gt=0, le=10_000)
    stock_height_mm: float = Field(gt=0, le=5_000)
    stock_count: int = Field(ge=1, le=100)
    back_stock_width_mm: float = Field(gt=0, le=10_000)
    back_stock_height_mm: float = Field(gt=0, le=5_000)
    back_stock_count: int = Field(ge=1, le=100)
    machine_profile_id: str = Field(min_length=1, max_length=160)


class DesignVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1, max_length=80)
    spec: BookcasePreviewInput
    production_context: RevisionProductionContext
    expected_design_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_current_revision: int = Field(ge=0)
    joint_retention_evidence_id: str | None = Field(
        default=None,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
        description=(
            "Optional immutable signed retention statement. The server verifies and "
            "injects the resulting contract; clients never submit contract fields."
        ),
    )
    source_provenance: ReferenceImageSourceProvenance | None = None


class DesignVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    revision: int
    status: DesignStatus
    design_hash: str
    context_hash: str
    spec_json: dict[str, Any]
    source_provenance_json: dict[str, Any]
    source_import_id: str | None
    result_json: dict[str, Any]
    engine_version: str
    template_version: str
    rule_version: str
    template_id: str
    template_capability_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    immutable: bool
    created_at: datetime


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stock_width_mm: float = Field(default=2440, gt=0, le=10_000)
    stock_height_mm: float = Field(default=1220, gt=0, le=5_000)
    stock_count: int = Field(default=4, ge=1, le=100)
    back_stock_width_mm: float = Field(default=2440, gt=0, le=10_000)
    back_stock_height_mm: float = Field(default=1220, gt=0, le=5_000)
    back_stock_count: int = Field(default=2, ge=1, le=100)
    machine_profile_id: Literal[
        "custombuild-router-1325-linuxcnc",
        "custombuild-router-5125-linuxcnc",
    ] = "custombuild-router-1325-linuxcnc"
    postprocessor_id: Literal["linuxcnc-validation-1.1.0"] = "linuxcnc-validation-1.1.0"
    include_step: bool = True
    include_freecad_project: bool = False
    include_validation_program: bool = True
    external_evidence_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("external_evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("external_evidence_ids must be unique")
        if any(len(item) != 36 for item in value):
            raise ValueError("external evidence IDs must be UUID strings")
        return sorted(value)

    @model_validator(mode="after")
    def validate_cad_dependencies(self) -> GenerationRequest:
        if self.include_freecad_project and not self.include_step:
            raise ValueError("include_freecad_project requires include_step")
        return self


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    design_version_id: str
    status: JobStatus
    production_context_hash: str
    production_engine_context_json: dict[str, Any]
    attempts: int
    error: str | None
    result_json: dict[str, Any] | None
    started_at: datetime | None
    lease_expires_at: datetime | None
    deadline_at: datetime | None
    next_attempt_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ArtifactRead(BaseModel):
    id: str
    kind: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(strict=True, gt=0, le=MAX_ARTIFACT_BYTES)
    content_type: str
    download_url: str
    download_path: str


class ReleaseArtifactRead(ArtifactRead):
    """One immutable artifact resolved through its historical release."""

    release_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    release_number: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,39}$")
    revision: int = Field(strict=True, gt=0)


class WarningOverrideCreate(BaseModel):
    rule_id: str = Field(pattern=r"^(?:CB-[A-Z]+-[0-9]{3}|DFM-GRAIN-001)$")
    reason: str = Field(min_length=10, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 10:
            raise ValueError("warning override reason must contain at least 10 characters")
        return stripped

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("evidence_ids must be unique")
        if any(len(item) != 36 for item in value):
            raise ValueError("evidence IDs must be UUID strings")
        return value


class ExternalEvidenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    evidence_type: Literal[
        "wall_anchor",
        "hardware",
        "material_grain",
        "joint_retention",
    ]
    rule_id: str = Field(pattern=r"^(CB|DFM)-[A-Z]+-[0-9]{3}$")
    catalog_id: str
    catalog_version: str
    design_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(strict=True, gt=0, le=MAX_CATALOG_SOURCE_BYTES)
    content_type: str
    created_by: str
    expires_at: datetime | None
    created_at: datetime


class ApprovalCreate(BaseModel):
    approval_type: Literal["design", "cam"]
    reason: str = Field(min_length=5, max_length=2000)
    generation_job_id: str | None = Field(default=None, min_length=36, max_length=36)
    warning_overrides: list[WarningOverrideCreate] = Field(default_factory=list, max_length=20)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 5:
            raise ValueError("approval reason must contain at least 5 characters")
        return stripped


class ReleaseCreate(BaseModel):
    release_number: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,39}$")
    confirmation: Literal["RELEASE"]


class WorkshopRunPrepare(BaseModel):
    """Non-authoritative request to prepare one server-derived workshop run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    generation_job_id: str = Field(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    )
    confirmation: Literal["PREPARE_WORKSHOP_RUN"]


class WorkshopRunBlockerDetail(BaseModel):
    """Truthful current-state result for the blocker-only workshop endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: WorkshopPreparationBlockerCode
    message: str = Field(min_length=1, max_length=500)
    solution: str = Field(min_length=1, max_length=500)
    workshop_status: Literal["BLOCKED"]
    release_review_eligible: Literal[False]
    cutting_blocker_codes: tuple[WorkshopPreparationBlockerCode] = Field(
        min_length=1,
        max_length=1,
    )
    physical_cutting_authorized: Literal[False]

    @model_validator(mode="after")
    def blocker_code_is_canonical(self) -> WorkshopRunBlockerDetail:
        if self.cutting_blocker_codes != (self.code,):
            raise ValueError("cutting blocker codes must contain the exact primary blocker")
        return self


class WorkshopRunBlockedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    detail: WorkshopRunBlockerDetail


class ReleaseRead(BaseModel):
    release_id: str
    release_number: str
    status: Literal["released"]
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    release_kind: Literal["design_review"]
    machine_use: Literal["validation_only"]


class ApprovalRead(BaseModel):
    """Server-owned review state for restoring the production workflow."""

    model_config = ConfigDict(from_attributes=True)

    approval_type: Literal["design", "cam"]
    approved_by: str
    reason: str
    generation_job_id: str | None
    production_context_hash: str | None
    manifest_sha256: str | None
    overrides_json: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class ProductionStateRead(BaseModel):
    """Complete recoverable state for the current project revision."""

    project_id: str
    version: DesignVersionRead | None
    approvals: list[ApprovalRead]
    latest_job: JobRead | None
    release: ReleaseRead | None


class ImportInspection(BaseModel):
    import_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    project_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: str
    size_bytes: int = Field(strict=True, gt=0, le=MAX_CATALOG_SOURCE_BYTES)
    furniture_type: Literal["bookcase"] | None
    furniture_type_confidence: float
    status: Literal["needs_calibration"]
    assumptions: list[dict[str, Any]]
    unknown_fields: list[str]
