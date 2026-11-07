"""Workshop-independent furniture intent and versioned component selections.

The v1 shelving compiler is retained. New families have explicit geometry and
qualification requirements; a shape never inherits shelving's support claim.
All dimensions here are integer micrometres, including catalogue clearances.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .enums import BackPanelType, ShelfMount
from .models import (
    BookcaseDepthUm,
    BookcaseHeightUm,
    BookcaseWidthUm,
    FrozenModel,
    RatioPpm,
    StableKey,
)

Length = Annotated[int, Field(strict=True, ge=1, le=6_000_000)]
Thickness = Annotated[int, Field(strict=True, ge=1_000, le=100_000)]
Load = Annotated[int, Field(strict=True, ge=0, le=5_000)]

FURNITURE_SCHEMA_VERSION: Literal["custombuild.furniture-design.v1"] = (
    "custombuild.furniture-design.v1"
)
FURNITURE_ENGINE_VERSION = "furniture-families-2.1.0"


def furniture_review_schema(family: str | None) -> str:
    if family == "shelving":
        return "custombuild.shelving-workshop-review.v6"
    return "custombuild.furniture-review.v1"


class MaterialSelection(FrozenModel):
    material_id: StableKey
    version: StableKey = "screening-2026.1"
    measured_thickness_um: Thickness
    batch_id: StableKey | None = None


class HardwareSelection(FrozenModel):
    catalog_id: StableKey
    version: StableKey = "layout-1.0.0"


class TrimProfile(FrozenModel):
    """Measured profile; use and placement are explicit customer decisions."""

    height_um: Annotated[int, Field(strict=True, ge=1, le=500_000)] | None = None
    width_um: Annotated[int, Field(strict=True, ge=1, le=100_000)] | None = None
    use: Literal["unassigned", "existing_room_trim", "furniture_trim"] = "unassigned"
    walls: tuple[Literal["left", "right", "rear"], ...] = ()

    @model_validator(mode="after")
    def validate_placement(self) -> TrimProfile:
        if len(set(self.walls)) != len(self.walls):
            raise ValueError("trim walls must be unique")
        if self.walls and self.use != "existing_room_trim":
            raise ValueError("wall placement only applies to existing room trim")
        return self


class InstallationSpace(FrozenModel):
    """Customer measurements, distinct from the generated carcass dimensions.

    Allowances reserve space for installation or separately designed trim. None
    means unmeasured, never zero. They do not create a trim part or qualify it.
    """

    width_um: Length
    height_um: Length
    depth_um: Length
    width_includes_trim: bool = False
    trim_profile: TrimProfile | None = Field(default=None, exclude_if=lambda value: value is None)
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
    trial_angle: Literal["GN-970-ALM-30-30-30-B"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    width_um: BookcaseWidthUm = 900_000
    height_um: BookcaseHeightUm = 1_800_000
    depth_um: BookcaseDepthUm = 320_000
    shelf_count: Annotated[int, Field(strict=True, ge=0, le=40)] = 4
    divider_count: Annotated[int, Field(strict=True, ge=0, le=16)] = 1
    shelf_load_n: Load = 200
    shelf_load_basis: Literal["per_row", "per_metre"] = Field(
        default="per_row", exclude_if=lambda value: value == "per_row"
    )
    shelf_load_per_metre_n: Annotated[int, Field(strict=True, ge=0, le=5_000)] = Field(
        default=0, exclude_if=lambda value: value == 0
    )
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
        if self.resolved_shelf_load_n > 5_000:
            raise ValueError("lasten per hel hyllrad överskrider modellens gräns på 5000 N")
        return self

    @property
    def resolved_shelf_load_n(self) -> int:
        if self.shelf_load_basis == "per_metre":
            # Include the full carcass width rather than subtracting dividers:
            # this does not understate a uniformly loaded row. Round upwards.
            return (self.width_um * self.shelf_load_per_metre_n + 999_999) // 1_000_000
        return self.shelf_load_n


class TableIntent(FrozenModel):
    """A panel-end table, with two removable stretchers below the top."""

    family: Literal["table"] = "table"
    width_um: Length = 1_000_000
    height_um: Length = 740_000
    depth_um: Length = 600_000
    end_inset_um: Annotated[int, Field(strict=True, ge=0, le=500_000)] = 60_000
    stretcher_height_um: Annotated[int, Field(strict=True, ge=40_000, le=300_000)] = 100_000
    top_load_n: Load = 300


class DiningTableIntent(FrozenModel):
    family: Literal["dining_table"] = "dining_table"
    width_um: Length = 1_800_000
    height_um: Length = 750_000
    depth_um: Length = 900_000
    top_shape: Literal["rectangle", "round"] = "rectangle"
    leg_section_um: Annotated[int, Field(strict=True, ge=35_000, le=80_000)] = 55_000
    end_inset_um: Annotated[int, Field(strict=True, ge=0, le=500_000)] = 90_000
    stretcher_height_um: Annotated[int, Field(strict=True, ge=65_000, le=250_000)] = 95_000
    top_load_n: Load = 300


class ChairIntent(FrozenModel):
    family: Literal["chair"] = "chair"
    width_um: Length = 460_000
    height_um: Length = 850_000
    depth_um: Length = 500_000
    leg_section_um: Annotated[int, Field(strict=True, ge=35_000, le=75_000)] = 50_000
    seat_height_um: Annotated[int, Field(strict=True, ge=250_000, le=700_000)] = 450_000
    seat_depth_um: Annotated[int, Field(strict=True, ge=200_000, le=700_000)] = 450_000
    back_angle_mdeg: Annotated[int, Field(strict=True, ge=0, le=18_000)] = 10_000
    seat_load_n: Load = 900


class ChestIntent(FrozenModel):
    family: Literal["chest_of_drawers"] = "chest_of_drawers"
    width_um: BookcaseWidthUm = 800_000
    height_um: BookcaseHeightUm = 900_000
    depth_um: BookcaseDepthUm = 500_000
    drawer_count: Annotated[int, Field(strict=True, ge=1, le=8)] = 3
    drawer_load_n: Load = 100
    front_gap_um: Annotated[int, Field(strict=True, ge=1_000, le=10_000)] = 3_000

    drawer_columns: Literal[1, 2] = Field(default=1, exclude_if=lambda v: v == 1)
    # Bottom-up front heights; the last row takes the exact remaining space.
    row_front_heights_um: tuple[Length, ...] = Field(
        default=(), max_length=7, exclude_if=lambda v: not v
    )
    gap_basis: Literal["per_edge", "clear_reveal"] = Field(
        default="per_edge", exclude_if=lambda v: v == "per_edge"
    )

    @model_validator(mode="after")
    def complete_drawer_rows(self) -> ChestIntent:
        if self.drawer_count % self.drawer_columns:
            raise ValueError("Antalet lådor måste vara delbart med antalet kolumner.")
        if len(self.row_front_heights_um) >= self.drawer_count // self.drawer_columns:
            raise ValueError("Lämna minst en lådrad för återstående höjd.")
        return self


FurnitureIntent = Annotated[
    ShelvingIntent | TableIntent | ChestIntent | DiningTableIntent | ChairIntent,
    Field(discriminator="family"),
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
        if self.intent.family in {"dining_table", "chair"} and self.hardware is not None:
            raise ValueError("Ramens skruvförband ingår i konstruktionsmodellen.")
        if self.intent.family in {"table", "chest_of_drawers"} and self.hardware is None:
            raise ValueError("table and drawer families require an explicit hardware selection")
        return self


class FurnitureStockFormat(FrozenModel):
    """Sheet axes are machine X/Y, before nesting or fixture allowance."""

    stock_width_um: Length = 2_440_000
    stock_height_um: Length = 1_220_000
    stock_grain_axis: Literal["x", "y"] | None = None
    edge_margin_um: Annotated[int, Field(strict=True, ge=0, le=100_000)] = Field(
        default=0, exclude_if=lambda value: value == 0
    )


class FurnitureMaterialStock(FurnitureStockFormat):
    material_id: StableKey
    material_version: StableKey
    measured_thickness_um: Thickness

    @property
    def material_key(self) -> tuple[str, str, int]:
        return self.material_id, self.material_version, self.measured_thickness_um


class ManufacturingSelection(FurnitureStockFormat):
    """A separate planning profile. It grants no physical machine approval."""

    machine_profile_id: StableKey
    machine_profile_version: StableKey = "1.0.0-validation"
    material_stocks: tuple[FurnitureMaterialStock, ...] = Field(
        default=(), max_length=16, exclude_if=lambda value: not value
    )

    @field_validator("material_stocks")
    @classmethod
    def canonical_material_stocks(
        cls, stocks: tuple[FurnitureMaterialStock, ...]
    ) -> tuple[FurnitureMaterialStock, ...]:
        keys = [stock.material_key for stock in stocks]
        if len(set(keys)) != len(keys):
            raise ValueError("only one stock format per material, version and thickness is allowed")
        return tuple(sorted(stocks, key=lambda stock: stock.material_key))


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
