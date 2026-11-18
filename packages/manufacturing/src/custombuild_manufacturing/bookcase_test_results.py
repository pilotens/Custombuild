"""Compare one trusted type test with the exact bookcase's physical demands."""

from datetime import date
from typing import Annotated, Any, Literal

from custombuild_domain.enums import GrainDirection, PartRole
from custombuild_domain.furniture import MaterialSelection
from custombuild_domain.models import (
    TRIAL_STOCK_TOLERANCE_UM,
    DesignResult,
    PartInstance,
    StableKey,
)
from pydantic import BaseModel, ConfigDict, Field

Positive = Annotated[int, Field(strict=True, gt=0)]
Nonnegative = Annotated[int, Field(strict=True, ge=0)]


class BookcaseTestLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    joint_shear_n: Positive
    row_payload_n: Nonnegative
    maximum_shelf_deflection_um: Positive
    horizontal_force_n: Nonnegative
    minimum_tip_factor_permille: Positive
    assembly_steps: tuple[Positive, ...]


def bookcase_test_limits(
    design: DesignResult, evaluations: list[dict[str, Any]]
) -> BookcaseTestLimits:
    values = {
        r["rule_id"]: r["values"]["allowed"]
        for r in evaluations
        if r["rule_id"] in {"CB-DEFLECTION-001", "CB-TIP-001"}
    }
    p = design.spec.parameters
    return BookcaseTestLimits(
        joint_shear_n=design.minimum_retention_shear_load_n,
        row_payload_n=p.shelf_load_n if p.shelf_count else 0,
        maximum_shelf_deflection_um=values["CB-DEFLECTION-001"],
        horizontal_force_n=p.assumed_horizontal_force_n,
        minimum_tip_factor_permille=values["CB-TIP-001"],
        assembly_steps=tuple(s.step_number for s in design.assembly_graph.steps),
    )


class UsableStockFormat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Guaranteed clear rectangular area after defects and edge trims. Each pair
    # describes a single actual format within this lot, never two separate maxima.
    along_grain_um: Positive
    across_grain_um: Positive


class TestedMaterialLot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    material_id: StableKey
    material_version: StableKey
    batch_id: StableKey
    usable_formats: tuple[UsableStockFormat, ...] = Field(min_length=1)
    minimum_thickness_um: Positive
    maximum_thickness_um: Positive
    uncertainty_um: Nonnegative

    def require_match(
        self, stock: MaterialSelection, label: str, parts: tuple[PartInstance, ...]
    ) -> None:
        if (self.material_id, self.material_version, self.batch_id) != (
            stock.material_id,
            stock.version,
            stock.batch_id,
        ):
            raise ValueError(f"{label}: provets material, version eller batch avviker från ordern.")
        if self.minimum_thickness_um > self.maximum_thickness_um:
            raise ValueError(f"{label}: tjocklekens mätintervall är omvänt.")
        low = self.minimum_thickness_um - self.uncertainty_um
        high = self.maximum_thickness_um + self.uncertainty_um
        target = stock.measured_thickness_um
        if low < target - TRIAL_STOCK_TOLERANCE_UM or high > target + TRIAL_STOCK_TOLERANCE_UM:
            raise ValueError(
                f"{label}: tjocklek {low}–{high} µm inklusive mätosäkerhet ligger utanför "
                f"orderns {target} ± {TRIAL_STOCK_TOLERANCE_UM} µm."
            )

        for part in parts:
            raw = part.raw_size
            # Shelving uses real broad faces. U/V are world Y/Z on sides, X/Z
            # on backs and doors, and X/Y on horizontal panels.
            dims = {"x": raw.width_um, "y": raw.depth_um, "z": raw.height_um}
            normal = {
                "left": "x",
                "right": "x",
                "bottom": "z",
                "top": "z",
                "back": "y",
                "front": "y",
            }[part.a_side.value]
            axes = [axis for axis in dims if axis != normal]
            orientations = [tuple(dims[a] for a in axes)]
            if part.grain_direction == GrainDirection.NONE:
                orientations.append(tuple(reversed(orientations[0])))
            elif axes[0] != part.grain_direction.value:
                orientations = [tuple(reversed(orientations[0]))]
            if not any(
                along <= fmt.along_grain_um and across <= fmt.across_grain_um
                for along, across in orientations
                for fmt in self.usable_formats
            ):
                raise ValueError(
                    f"{label}: inget obrutet råformat med rätt fiber för {part.semantic_key}."
                )


class WoodMovementTest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Moisture content is reported in thousandths of dry wood mass.
    use_min_permille: Nonnegative
    use_max_permille: Positive
    tested_dry_permille: Nonnegative
    tested_wet_permille: Positive
    moisture_uncertainty_permille: Nonnegative
    minimum_remaining_movement_um: Positive
    clearance_uncertainty_um: Nonnegative

    def require_clearance(self) -> None:
        if (
            self.use_min_permille >= self.use_max_permille
            or self.tested_dry_permille + self.moisture_uncertainty_permille > self.use_min_permille
            or self.tested_wet_permille - self.moisture_uncertainty_permille < self.use_max_permille
            or self.minimum_remaining_movement_um <= self.clearance_uncertainty_um
        ):
            raise ValueError("Fuktprovet täcker inte bruksmiljön med kvarvarande rörelsemån.")


class DoorCycleTest(BaseModel):
    """Measured result for one opening, with both neighbours and the installed wall present."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    leaf: StableKey
    tested_mass_g: Positive
    achieved_opening_mdeg: Positive
    angle_uncertainty_mdeg: Nonnegative
    minimum_clearance_um: Positive
    clearance_uncertainty_um: Nonnegative
    cycles_completed: Positive
    retained_screw_capacity_n: Positive


class DoorServiceTest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # The project's trusted service/test specification supplies the duty and
    # screw actions. Neither may be taken from an order or inferred from wood density.
    duty_specification: StableKey
    required_cycles: Positive
    required_screw_capacity_n: Positive
    tested_openings: tuple[DoorCycleTest, ...] = Field(min_length=1)

    def require_openings(self, design: DesignResult) -> None:
        leaves = {
            p.semantic_key: p.weight_g for p in design.parts if p.role == PartRole.CABINET_FRONT
        }
        if (
            len(self.tested_openings) != len(leaves)
            or {r.leaf for r in self.tested_openings} != leaves.keys()
        ):
            raise ValueError("Luckprovet måste omfatta varje enskilt orderfack exakt en gång.")
        for row in self.tested_openings:
            if (
                row.tested_mass_g < leaves[row.leaf]
                or row.achieved_opening_mdeg - row.angle_uncertainty_mdeg < 93_000
                or row.minimum_clearance_um <= row.clearance_uncertainty_um
                or row.cycles_completed < self.required_cycles
                or row.retained_screw_capacity_n < self.required_screw_capacity_n
            ):
                raise ValueError(
                    f"{row.leaf}: luckprovet klarar inte last, rörelse, cykler eller skruvfäste."
                )


class BookcaseTestResults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["custombuild.bookcase-physical-results.v2"]
    issuer: str = Field(min_length=1, max_length=128)
    report_reference: str = Field(min_length=1, max_length=160)
    tested_on: date
    carcass_lot: TestedMaterialLot
    back_lot: TestedMaterialLot
    wood_movement: WoodMovementTest | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    door_service: DoorServiceTest | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    joint_design_shear_capacity_n: Positive
    joint_withdrawal_demand_n: Positive
    joint_design_withdrawal_capacity_n: Positive
    verified_row_payload_n: Nonnegative
    measured_shelf_deflection_um: Nonnegative
    deflection_uncertainty_um: Nonnegative
    tested_horizontal_force_n: Nonnegative
    verified_tip_factor_permille: Positive
    completed_assembly_steps: tuple[Positive, ...]

    def require_capacity(self, limits: BookcaseTestLimits) -> None:
        checks = (
            ("Fogskjuvning", self.joint_design_shear_capacity_n, limits.joint_shear_n, "N"),
            (
                "Fogutdrag",
                self.joint_design_withdrawal_capacity_n,
                self.joint_withdrawal_demand_n,
                "N",
            ),
            (
                "Jämnt fördelad brukslast per hel rad",
                self.verified_row_payload_n,
                limits.row_payload_n,
                "N",
            ),
            (
                "Horisontalkraft för stabilitetsprovet",
                self.tested_horizontal_force_n,
                limits.horizontal_force_n,
                "N",
            ),
            (
                "Verifierad tippmarginal",
                self.verified_tip_factor_permille,
                limits.minimum_tip_factor_permille,
                "‰",
            ),
        )
        for name, measured, required, unit in checks:
            if measured < required:
                raise ValueError(
                    f"{name}: provunderlaget anger {measured} {unit}. "
                    f"Denna order kräver minst {required} {unit}."
                )
        upper = self.measured_shelf_deflection_um + self.deflection_uncertainty_um
        if upper > limits.maximum_shelf_deflection_um:
            raise ValueError(
                f"Nedböjning med mätosäkerhet är {upper} µm. "
                f"Orderns högsta tillåtna värde är {limits.maximum_shelf_deflection_um} µm."
            )
        if self.completed_assembly_steps != limits.assembly_steps:
            raise ValueError(
                "Provprotokollets monteringssteg avviker från orderns steg och ordningsföljd."
            )
