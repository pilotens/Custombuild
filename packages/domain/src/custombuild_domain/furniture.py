"""Workshop-independent furniture intent and versioned component selections.

The v1 shelving compiler is retained. New families have explicit geometry and
qualification requirements; a shape never inherits shelving's support claim.
All dimensions here are integer micrometres, including catalogue clearances.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import FrozenModel, StableKey

Length = Annotated[int, Field(strict=True, ge=1, le=6_000_000)]
Thickness = Annotated[int, Field(strict=True, ge=1_000, le=100_000)]
Load = Annotated[int, Field(strict=True, ge=0, le=5_000)]

FURNITURE_SCHEMA_VERSION: Literal["custombuild.furniture-design.v1"] = (
    "custombuild.furniture-design.v1"
)
FURNITURE_ENGINE_VERSION = "furniture-families-1.0.0"


class MaterialSelection(FrozenModel):
    material_id: StableKey
    version: StableKey = "screening-2026.1"
    measured_thickness_um: Thickness
    batch_id: StableKey | None = None


class HardwareSelection(FrozenModel):
    catalog_id: StableKey
    version: StableKey = "layout-1.0.0"


class ShelvingIntent(FrozenModel):
    family: Literal["shelving"] = "shelving"
    width_um: Length = 900_000
    height_um: Length = 1_800_000
    depth_um: Length = 320_000
    shelf_count: Annotated[int, Field(strict=True, ge=0, le=40)] = 4
    divider_count: Annotated[int, Field(strict=True, ge=0, le=16)] = 1
    shelf_load_n: Load = 200
    shelf_height_ratios_ppm: tuple[
        Annotated[int, Field(strict=True, ge=50_000, le=950_000)], ...
    ] = Field(default=(), max_length=40)


class TableIntent(FrozenModel):
    """A panel-end table, with two removable stretchers below the top."""

    family: Literal["table"] = "table"
    width_um: Length = 1_000_000
    height_um: Length = 740_000
    depth_um: Length = 600_000
    end_inset_um: Annotated[int, Field(strict=True, ge=0, le=500_000)] = 60_000
    stretcher_height_um: Annotated[int, Field(strict=True, ge=40_000, le=300_000)] = 100_000
    top_load_n: Load = 300


class ChestIntent(FrozenModel):
    family: Literal["chest_of_drawers"] = "chest_of_drawers"
    width_um: Length = 800_000
    height_um: Length = 900_000
    depth_um: Length = 500_000
    drawer_count: Annotated[int, Field(strict=True, ge=1, le=8)] = 3
    drawer_load_n: Load = 100
    front_gap_um: Annotated[int, Field(strict=True, ge=1_000, le=10_000)] = 3_000


FurnitureIntent = Annotated[
    ShelvingIntent | TableIntent | ChestIntent, Field(discriminator="family")
]


class FurnitureDesign(FrozenModel):
    schema_version: Literal["custombuild.furniture-design.v1"] = FURNITURE_SCHEMA_VERSION
    design_id: StableKey = "furniture"
    revision: Annotated[int, Field(strict=True, ge=1)] = 1
    intent: FurnitureIntent
    material: MaterialSelection = Field(
        default_factory=lambda: MaterialSelection(
            material_id="birch-plywood",
            measured_thickness_um=18_000,
        )
    )
    back_material: MaterialSelection = Field(
        default_factory=lambda: MaterialSelection(
            material_id="birch-plywood-6",
            measured_thickness_um=6_000,
        )
    )
    hardware: HardwareSelection | None = None

    @model_validator(mode="after")
    def require_family_components(self) -> FurnitureDesign:
        if self.intent.family == "shelving" and self.hardware is not None:
            raise ValueError("shelving retention is selected through the existing signed catalogue")
        if self.intent.family != "shelving" and self.hardware is None:
            raise ValueError("table and drawer families require an explicit hardware selection")
        return self


class ManufacturingSelection(FrozenModel):
    """A separate planning profile. It grants no physical machine approval."""

    machine_profile_id: StableKey
    machine_profile_version: StableKey = "1.0.0-validation"
    stock_width_um: Length = 2_440_000
    stock_height_um: Length = 1_220_000
    stock_grain_axis: Literal["x", "y"] | None = None


class FurnitureWorkspace(FrozenModel):
    schema_version: Literal["custombuild.furniture-workspace.v1"] = (
        "custombuild.furniture-workspace.v1"
    )
    design: FurnitureDesign
    manufacturing: ManufacturingSelection | None = None


class ProfileChange(FrozenModel):
    """An explicit proposal, leaving the original workspace immutable."""

    current: FurnitureWorkspace
    proposed: FurnitureWorkspace

    @model_validator(mode="after")
    def preserve_design_intent(self) -> ProfileChange:
        if self.current.design.design_id != self.proposed.design.design_id:
            raise ValueError("a profile change cannot replace the design identity")
        if self.current.design.intent != self.proposed.design.intent:
            raise ValueError("a profile change must preserve locked design intent and dimensions")
        return self
