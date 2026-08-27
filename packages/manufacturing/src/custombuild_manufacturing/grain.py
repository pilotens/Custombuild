"""Canonical fail-closed stock-grain policy for nesting and CAM validation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .model import DFMIssue, PartInstance, PartSpec, Severity, StockSheet, coerce_part_instances

DFM_GRAIN_BLOCKER_CODE = "DFM-GRAIN-001"
DFM_GRAIN_RULE_VERSION = "1.0.0"
DFM_GRAIN_RULE_TITLE = "Fiberriktning för skivmaterial"
DFM_GRAIN_RULE_MESSAGE = (
    "Directional sheet-material parts require an exact X or Y stock-grain axis before "
    "nesting or CAM validation."
)
DFM_GRAIN_REQUIRED_ACTION = (
    "Bind an exact, structured X or Y stock-grain axis for every directional material "
    "stock profile; opaque evidence or acknowledgement cannot resolve this blocker."
)
DFM_GRAIN_STOCK_MATCHED_PHASE = "STOCK_MATCHED"
DFM_GRAIN_STOCK_SELECTION_INCOMPLETE_PHASE = "STOCK_SELECTION_INCOMPLETE"

_DFM_GRAIN_INPUT_KEYS = frozenset(
    {
        "affected_part_ids",
        "assessment_phase",
        "binding_status",
        "material_id",
        "material_version",
        "required_part_grain_directions",
        "stock_grain_direction",
        "stock_id",
    }
)


@dataclass(frozen=True, slots=True)
class GrainRuleDefinition:
    """Stable identity and wording shared by DFM, API projection and package status."""

    rule_id: str
    rule_version: str
    title: str
    message: str
    required_action: str

    def as_dict(self) -> dict[str, str]:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "title": self.title,
            "message": self.message,
            "required_action": self.required_action,
        }


DFM_GRAIN_RULE = GrainRuleDefinition(
    rule_id=DFM_GRAIN_BLOCKER_CODE,
    rule_version=DFM_GRAIN_RULE_VERSION,
    title=DFM_GRAIN_RULE_TITLE,
    message=DFM_GRAIN_RULE_MESSAGE,
    required_action=DFM_GRAIN_REQUIRED_ACTION,
)


def _canonical_panel_grain(value: str) -> str:
    """Normalize engine-owned part axes without interpreting stock evidence."""

    upper = str(value).strip().upper()
    if upper in {"NONE", "NO_GRAIN"}:
        return "NONE"
    if upper in {"X", "Y"}:
        return upper
    return "UNKNOWN"


def _bound_stock_axis(value: str) -> str | None:
    """Accept only an exact structured sheet-frame axis for directional stock."""

    upper = str(value).strip().upper()
    return upper if upper in {"X", "Y"} else None


def stock_grain_binding_required(
    parts: Iterable[PartSpec] | Iterable[PartInstance],
) -> bool:
    """Return whether any effective part grain is directional or unresolved."""

    return any(
        _canonical_panel_grain(instance.part.grain_direction) != "NONE"
        for instance in coerce_part_instances(parts)
    )


def _strict_sorted_unique_strings(
    value: Any,
    *,
    label: str,
    allowed: frozenset[str] | None = None,
) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str | bytes)
        or not value
        or any(not isinstance(item, str) or not item or item != item.strip() for item in value)
    ):
        raise ValueError(f"canonical grain issue {label} must be non-empty strings")
    strings = tuple(value)
    if strings != tuple(sorted(set(strings))):
        raise ValueError(f"canonical grain issue {label} must be sorted and unique")
    if allowed is not None and not set(strings) <= allowed:
        raise ValueError(f"canonical grain issue {label} contains an unsupported axis")
    return strings


def validate_stock_grain_binding_issue(
    issue: DFMIssue,
    *,
    expected_severity: Severity | None = None,
    expected_phase: str | None = None,
) -> None:
    """Reject any non-canonical representation of ``DFM-GRAIN-001``.

    This validates the checksum-bound fact shape, not the external truth of a
    stock profile. Only a future structured stock-axis binding can establish
    that external truth; documentary uploads deliberately are not inputs here.
    """

    if issue.code != DFM_GRAIN_RULE.rule_id:
        raise ValueError("canonical grain issue code is invalid")
    if issue.severity not in {Severity.BLOCK, Severity.WARNING}:
        raise ValueError("canonical grain issue severity must be BLOCK or WARNING")
    if expected_severity is not None and issue.severity is not expected_severity:
        raise ValueError("canonical grain issue severity does not match its package phase")
    if issue.message != DFM_GRAIN_RULE.message:
        raise ValueError("canonical grain issue message is invalid")
    if issue.suggestion != DFM_GRAIN_RULE.required_action:
        raise ValueError("canonical grain issue required action is invalid")
    if any(value is not None for value in (issue.part_id, issue.feature_id, issue.setup_id)):
        raise ValueError("canonical grain issue must use only its grouped affected-part input")
    if frozenset(issue.inputs) != _DFM_GRAIN_INPUT_KEYS:
        raise ValueError("canonical grain issue inputs have an unexpected structure")
    if issue.inputs.get("binding_status") != "MISSING_INFORMATION":
        raise ValueError("canonical grain issue binding status is invalid")

    phase = issue.inputs.get("assessment_phase")
    allowed_phases = {
        DFM_GRAIN_STOCK_MATCHED_PHASE,
        DFM_GRAIN_STOCK_SELECTION_INCOMPLETE_PHASE,
    }
    if not isinstance(phase, str) or phase not in allowed_phases:
        raise ValueError("canonical grain issue assessment phase is invalid")
    if expected_phase is not None and phase != expected_phase:
        raise ValueError("canonical grain issue assessment phase does not match package status")

    for field in ("material_id", "material_version"):
        value = issue.inputs.get(field)
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"canonical grain issue {field} is invalid")

    stock_id = issue.inputs.get("stock_id")
    stock_axis = issue.inputs.get("stock_grain_direction")
    if phase == DFM_GRAIN_STOCK_SELECTION_INCOMPLETE_PHASE:
        if issue.severity is not Severity.WARNING:
            raise ValueError("stock-selection grain information must be a warning")
        if stock_id is not None or stock_axis != "UNAVAILABLE":
            raise ValueError("stock-selection grain warning cannot claim a matched stock")
    else:
        if not isinstance(stock_id, str) or not stock_id or stock_id != stock_id.strip():
            raise ValueError("matched-stock grain issue requires a canonical stock ID")
        if stock_axis != "UNBOUND":
            raise ValueError("matched-stock grain issue requires the canonical unbound axis")

    _strict_sorted_unique_strings(
        issue.inputs.get("required_part_grain_directions"),
        label="required part axes",
        allowed=frozenset({"X", "Y", "UNKNOWN"}),
    )
    _strict_sorted_unique_strings(
        issue.inputs.get("affected_part_ids"),
        label="affected part IDs",
    )


def stock_grain_binding_issues(
    parts: Iterable[PartSpec] | Iterable[PartInstance],
    stock: StockSheet | None,
    *,
    severity: Severity = Severity.BLOCK,
) -> tuple[DFMIssue, ...]:
    """Return one canonical blocker when directional parts lack a bound stock axis.

    ``NONE`` is meaningful only on the part side: it is the effective result of
    a versioned non-directional material catalogue entry. On stock, NONE/ANY/
    UNSPECIFIED/UNKNOWN all mean that no X/Y sheet-frame binding exists.
    """

    if severity not in {Severity.BLOCK, Severity.WARNING}:
        raise ValueError("stock-grain assessment severity must be BLOCK or WARNING")
    instances = coerce_part_instances(parts)
    directional_parts = tuple(
        sorted(
            {
                instance.part.part_id: instance.part
                for instance in instances
                if _canonical_panel_grain(instance.part.grain_direction) != "NONE"
            }.values(),
            key=lambda part: part.part_id,
        )
    )
    if not directional_parts:
        return ()
    groups: tuple[tuple[str, str, tuple[PartSpec, ...]], ...]
    if stock is not None:
        if _bound_stock_axis(stock.grain_direction) is not None:
            return ()
        groups = ((stock.material_id, stock.material_version, directional_parts),)
    else:
        by_material: dict[tuple[str, str], list[PartSpec]] = {}
        for part in directional_parts:
            by_material.setdefault((part.material_id, part.material_version), []).append(part)
        groups = tuple(
            (material_id, material_version, tuple(by_material[(material_id, material_version)]))
            for material_id, material_version in sorted(by_material)
        )

    issues: list[DFMIssue] = []
    for material_id, material_version, current_parts in groups:
        affected_part_ids = tuple(part.part_id for part in current_parts)
        required_part_axes = tuple(
            sorted({_canonical_panel_grain(part.grain_direction) for part in current_parts})
        )
        issues.append(
            DFMIssue(
                code=DFM_GRAIN_RULE.rule_id,
                severity=severity,
                message=DFM_GRAIN_RULE.message,
                inputs={
                    "binding_status": "MISSING_INFORMATION",
                    "assessment_phase": (
                        DFM_GRAIN_STOCK_MATCHED_PHASE
                        if stock is not None
                        else DFM_GRAIN_STOCK_SELECTION_INCOMPLETE_PHASE
                    ),
                    "stock_id": stock.stock_id if stock is not None else None,
                    "material_id": material_id,
                    "material_version": material_version,
                    "stock_grain_direction": (
                        "UNBOUND"
                        if stock is not None
                        else "UNAVAILABLE"
                    ),
                    "required_part_grain_directions": required_part_axes,
                    "affected_part_ids": affected_part_ids,
                },
                suggestion=DFM_GRAIN_RULE.required_action,
            )
        )
        validate_stock_grain_binding_issue(issues[-1])
    return tuple(issues)


def grain_control_projection(
    issues: Iterable[DFMIssue],
) -> Mapping[str, Any] | None:
    """Project canonical grain issues for server/UI controls without duplicating policy."""

    grain_issues = tuple(issue for issue in issues if issue.code == DFM_GRAIN_RULE.rule_id)
    if not grain_issues:
        return None
    for issue in grain_issues:
        validate_stock_grain_binding_issue(issue)
    affected_part_ids: set[str] = set()
    for issue in grain_issues:
        raw_part_ids = issue.inputs.get("affected_part_ids", ())
        if isinstance(raw_part_ids, tuple | list):
            affected_part_ids.update(str(part_id) for part_id in raw_part_ids)
    status = (
        Severity.BLOCK
        if any(issue.severity is Severity.BLOCK for issue in grain_issues)
        else Severity.WARNING
    )
    return {
        **DFM_GRAIN_RULE.as_dict(),
        "status": status.value,
        "summary": DFM_GRAIN_RULE.message,
        "calculation": "Råskiveaxel = ej strukturerat bunden",
        "assumptions": (
            "Okänd råskiveaxel får inte behandlas som riktningslöst material.",
        ),
        "affected_part_ids": tuple(sorted(affected_part_ids)),
        "issues": tuple(dict(issue.inputs) for issue in grain_issues),
    }
