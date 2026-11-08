"""Compare one trusted type test with the exact bookcase's physical demands."""

from datetime import date
from typing import Annotated, Any, Literal

from custombuild_domain.models import DesignResult
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


class BookcaseTestResults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["custombuild.bookcase-physical-results.v1"]
    issuer: str = Field(min_length=1, max_length=128)
    report_reference: str = Field(min_length=1, max_length=160)
    tested_on: date
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
