from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated

from custombuild_domain.models import BookcaseDesignSpec, DesignResult, FrozenModel
from pydantic import Field, model_validator


class RuleStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - a rule result, not a credential
    WARNING = "WARNING"
    BLOCK = "BLOCK"


class ActionType(StrEnum):
    ADD_VERTICAL_DIVIDER = "add_vertical_divider"
    ADD_BACK_PANEL = "add_back_panel"
    VERIFY_WALL_ANCHOR = "verify_wall_anchor"
    USE_STRONGER_MATERIAL = "use_stronger_material"
    INCREASE_THICKNESS = "increase_thickness"
    REDUCE_LOAD = "reduce_load"
    VERIFY_HARDWARE_CAPACITY = "verify_hardware_capacity"
    REGENERATE_JOINT_GEOMETRY = "regenerate_joint_geometry"


Scalar = int | str | bool | None


class RuleDatum(FrozenModel):
    name: str = Field(min_length=1, max_length=100)
    value: Scalar
    unit: str | None = Field(default=None, max_length=32)


class CalculationStep(FrozenModel):
    expression: str = Field(min_length=1, max_length=500)
    result: str = Field(min_length=1, max_length=100)
    unit: str | None = Field(default=None, max_length=32)


class ParameterChange(FrozenModel):
    path: str = Field(pattern=r"^[a-z][a-z0-9_.]+$")
    before: Scalar
    after: Scalar


class SuggestedAction(FrozenModel):
    action_id: str = Field(min_length=8, max_length=64)
    action_type: ActionType
    description: str = Field(min_length=1, max_length=500)
    changes: tuple[ParameterChange, ...] = ()
    requires_user_evidence: bool = False


class RuleEvaluation(FrozenModel):
    rule_id: str = Field(pattern=r"^CB-[A-Z]+-[0-9]{3}$")
    rule_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    title: str = Field(min_length=1, max_length=160)
    status: RuleStatus
    applies_to_part_ids: tuple[str, ...]
    inputs: tuple[RuleDatum, ...]
    assumptions: tuple[str, ...]
    trace: tuple[CalculationStep, ...]
    calculated_value: int
    allowed_value: int
    unit: str
    safety_margin_permille: int
    suggested_actions: tuple[SuggestedAction, ...] = ()


class RuleReport(FrozenModel):
    design_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    rules_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    overall_status: RuleStatus
    evaluations: tuple[RuleEvaluation, ...]
    disclaimer: str = (
        "Beräkningarna är deterministisk screening och beslutsstöd, inte "
        "produktcertifiering eller garanti för säker konstruktion."
    )

    @model_validator(mode="after")
    def overall_matches_evaluations(self) -> RuleReport:
        expected = aggregate_status(evaluation.status for evaluation in self.evaluations)
        if expected != self.overall_status:
            raise ValueError("overall rule status does not match the evaluations")
        return self


class DesignDiff(FrozenModel):
    sequence: Annotated[int, Field(strict=True, gt=0)]
    action_type: ActionType
    reason_rule_id: str
    changes: tuple[ParameterChange, ...]
    design_hash_before: str = Field(pattern=r"^[a-f0-9]{64}$")
    design_hash_after: str = Field(pattern=r"^[a-f0-9]{64}$")
    explanation: str


class AutoCorrectionResult(FrozenModel):
    original_design: DesignResult
    corrected_spec: BookcaseDesignSpec
    corrected_design: DesignResult
    initial_report: RuleReport
    final_report: RuleReport
    diffs: tuple[DesignDiff, ...]
    resolved: bool


def aggregate_status(statuses: Iterable[RuleStatus]) -> RuleStatus:
    rank = {RuleStatus.PASS: 0, RuleStatus.WARNING: 1, RuleStatus.BLOCK: 2}
    return max(tuple(statuses), key=lambda status: rank[status], default=RuleStatus.PASS)
