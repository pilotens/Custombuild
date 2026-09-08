"""Workshop-independent furniture intent and versioned component selections.

The v1 shelving compiler is retained. New families have explicit geometry and
qualification requirements; a shape never inherits shelving's support claim.
All dimensions here are integer micrometres, including catalogue clearances.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .enums import BackPanelType, ShelfMount
from .models import FrozenModel, RatioPpm, StableKey

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


class InstallationSpace(FrozenModel):
    """Customer measurements, distinct from the generated carcass dimensions.

    Allowances reserve space for installation or separately designed trim. None
    means unmeasured, never zero. They do not create a trim part or qualify it.
    """

    width_um: Length
    height_um: Length
    depth_um: Length
    width_includes_trim: bool = False
    left_allowance_um: Annotated[int, Field(strict=True, ge=0, le=500_000)] | None = None
    right_allowance_um: Annotated[int, Field(strict=True, ge=0, le=500_000)] | None = None
    top_allowance_um: Annotated[int, Field(strict=True, ge=0, le=500_000)] | None = None
    bottom_allowance_um: Annotated[int, Field(strict=True, ge=0, le=500_000)] | None = None
    front_allowance_um: Annotated[int, Field(strict=True, ge=0, le=500_000)] | None = None
    rear_allowance_um: Annotated[int, Field(strict=True, ge=0, le=500_000)] | None = None

    def carcass_dimensions(self) -> dict[str, int] | None:
        dimensions: dict[str, int] = {}
        for axis, sides in (
            ("width", (self.left_allowance_um, self.right_allowance_um)),
            ("height", (self.bottom_allowance_um, self.top_allowance_um)),
            ("depth", (self.front_allowance_um, self.rear_allowance_um)),
        ):
            if any(side is None for side in sides):
                return None
            dimensions[f"{axis}_um"] = getattr(self, f"{axis}_um") - sum(
                side for side in sides if side is not None
            )
        return dimensions

    @model_validator(mode="after")
    def positive_remaining_space(self) -> InstallationSpace:
        for overall, a, b in (
            (self.width_um, self.left_allowance_um, self.right_allowance_um),
            (self.height_um, self.top_allowance_um, self.bottom_allowance_um),
            (self.depth_um, self.front_allowance_um, self.rear_allowance_um),
        ):
            if (a or 0) + (b or 0) >= overall:
                raise ValueError("installation allowances consume the available dimension")
        return self


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
    bay_width_ratios_ppm: tuple[RatioPpm, ...] = Field(
        default=(), max_length=17, exclude_if=lambda value: not value
    )
    back_panel: BackPanelType = Field(
        default=BackPanelType.INSET_GROOVE,
        exclude_if=lambda value: value == BackPanelType.INSET_GROOVE,
    )
    shelf_mount: ShelfMount = Field(
        default=ShelfMount.FIXED, exclude_if=lambda value: value == ShelfMount.FIXED
    )
    plinth_height_um: Annotated[int, Field(strict=True, ge=0, le=300_000)] = Field(
        default=0, exclude_if=lambda value: value == 0
    )

    @model_validator(mode="after")
    def canonical_bay_proportions(self) -> ShelvingIntent:
        if self.bay_width_ratios_ppm and sum(self.bay_width_ratios_ppm) != 1_000_000:
            raise ValueError("bay width proportions must sum to exactly 100 percent")
        return self


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
    installation: InstallationSpace | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

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
    edge_margin_um: Annotated[int, Field(strict=True, ge=0, le=100_000)] = Field(
        default=0, exclude_if=lambda value: value == 0
    )


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
        if (
            self.current.design.intent != self.proposed.design.intent
            or self.current.design.installation != self.proposed.design.installation
        ):
            raise ValueError("a profile change must preserve locked design intent and dimensions")
        return self
