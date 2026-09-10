"""Bounded, explicit shelf-layout proposals using the canonical screening rules.

Only the number of equal bays may change. The proposal is never saved or applied
here and cannot confer qualification on a furniture design or machine program.
"""

from __future__ import annotations

from typing import Any

from custombuild_domain.furniture import FurnitureWorkspace, ShelvingIntent
from custombuild_domain.geometry import allocate_bay_widths_um
from custombuild_rules.engine import MAX_AUTO_VERTICAL_DIVIDERS, _threshold_status
from custombuild_rules.models import RuleStatus

from .furniture_profiles import preview_furniture

SHELF_SUGGESTIONS_VERSION = "furniture-shelf-suggestions-1.0.0"
_TARGET_RULES = ("CB-DEFLECTION-001", "CB-BENDING-001", "CB-JOINT-001")
_SCOPE = (
    "Förslaget ändrar endast antalet jämnt fördelade fack. Nedböjning, böjspänning och "
    "lokal upplagsbärighet screenas med ordinarie regelmotor vid oförändrad angiven nyttig "
    "last. Egenvikten räknas om från den föreslagna geometrin. Numeriskt PASS kräver "
    "mindre än 80 % av respektive beräknad gräns, "
    "enligt regelmotorns varningsnivå; det är ingen garanti eller produktcertifiering. "
    "Långa topp- och bottenstycken delas inte. Förbandens permanenta hållning, "
    "sidostabilitet, förankring, materialbatch, listutrymme och verkstadens beredning "
    "behöver fortfarande granskas. Förslaget godkänner ingen tillverkning eller skärande CAM."
)


def _bay_dimensions(workspace: FurnitureWorkspace) -> dict[str, Any] | None:
    intent = workspace.design.intent
    if not isinstance(intent, ShelvingIntent):
        return None
    thickness = workspace.design.material.measured_thickness_um
    return {
        "divider_count": intent.divider_count,
        "bay_count": intent.divider_count + 1,
        "clear_widths_um": list(
            allocate_bay_widths_um(
                intent.width_um - 2 * thickness,
                thickness,
                intent.divider_count,
                intent.bay_width_ratios_ppm,
            )
        ),
    }


def _screening_checks(preview: dict[str, Any]) -> list[dict[str, Any]]:
    if preview["workspace"]["design"]["intent"]["family"] != "shelving":
        return []
    rules = {rule["rule_id"]: rule for rule in preview["rules"]["evaluations"]}
    checks: list[dict[str, Any]] = []
    for rule_id in _TARGET_RULES:
        rule = rules.get(rule_id)
        values = rule.get("values", {}) if rule else {}
        calculated, allowed = values.get("calculated"), values.get("allowed")
        numeric_status = "UNAVAILABLE"
        if type(calculated) is int and type(allowed) is int and calculated >= 0 and allowed > 0:
            # CB-JOINT-001 retains WARNING even at low numerical utilisation:
            # the DADO's permanent retention still needs qualification. Reuse
            # the rule engine's exact threshold classifier, never its formula.
            numeric_status = _threshold_status(calculated, allowed).value
        checks.append(
            {
                "rule_id": rule_id,
                "status": rule["status"] if rule else "UNAVAILABLE",
                "numeric_status": numeric_status,
                "calculated": calculated,
                "allowed": allowed,
                "unit": values.get("unit"),
            }
        )
    return checks


def _passes(checks: list[dict[str, Any]]) -> bool:
    return len(checks) == len(_TARGET_RULES) and all(
        check["numeric_status"] == RuleStatus.PASS.value
        and (
            check["status"] == RuleStatus.PASS.value
            or (check["rule_id"] == "CB-JOINT-001" and check["status"] == RuleStatus.WARNING.value)
        )
        for check in checks
    )


def suggest_shelf_bays(workspace: FurnitureWorkspace) -> dict[str, Any]:
    """Propose the first passing equal-bay count, without changing other inputs.

    All original/candidate geometry and rule results come from preview_furniture.
    Invalid original workspaces raise ValueError just like a normal preview. A
    candidate rejected by the canonical geometry compiler is counted and skipped;
    search is bounded by the existing rule engine's divider limit.
    """
    current = preview_furniture(workspace)
    current_checks = _screening_checks(current)
    response: dict[str, Any] = {
        "schema_version": "custombuild.furniture-shelf-suggestion.v1",
        "version": SHELF_SUGGESTIONS_VERSION,
        "state": "unavailable",
        "code": "SHELF_BAYS_UNAVAILABLE",
        "message": "Inget fackförslag finns för denna möbel.",
        "current": current,
        "proposed": None,
        "changed_fields": [],
        "current_bays": _bay_dimensions(workspace),
        "proposed_bays": None,
        "screening_checks": {"current": current_checks, "proposed": None},
        "search": {
            "max_divider_count": MAX_AUTO_VERTICAL_DIVIDERS,
            "attempted_candidate_count": 0,
            "evaluated_candidate_count": 0,
        },
        "remaining_issues": {
            "rules": [rule for rule in current["rules"]["evaluations"] if rule["status"] != "PASS"],
            "manufacturing": current["manufacturing"]["issues"],
        },
        "scope": _SCOPE,
        "can_apply": False,
        "production_qualified": False,
        "physical_cutting_authorized": False,
    }

    intent = workspace.design.intent
    if not isinstance(intent, ShelvingIntent):
        response.update(
            code="UNSUPPORTED_FAMILY",
            message="Fackförslag finns för närvarande bara för hyllsystem.",
        )
        return response
    if intent.shelf_count == 0:
        response.update(
            code="NO_SHELVES", message="Möbeln saknar hyllplan att dimensionera fack för."
        )
        return response
    if intent.bay_width_ratios_ppm:
        response.update(
            code="CUSTOM_BAY_LAYOUT",
            message="Möbeln har valda fackproportioner. Välj själv en jämn fackfördelning "
            "innan ett förslag på fler fack kan tas fram.",
        )
        return response
    if any(check["numeric_status"] == "UNAVAILABLE" for check in current_checks):
        response.update(
            code="NUMERIC_RULES_UNAVAILABLE",
            message="Beräknade gränser för hyllbärigheten är ofullständiga. "
            "Verifiera hyllornas upplag och beslag innan fackförslag kan tas fram.",
        )
        return response
    if _passes(current_checks):
        response.update(
            state="already_pass",
            code="SHELF_SCREENING_ALREADY_PASS",
            message="Nuvarande fack klarar den numeriska screeningen för hyllbärighet. "
            "Övriga granskningspunkter kvarstår.",
        )
        return response

    original_document = workspace.model_dump(mode="python")
    for divider_count in range(intent.divider_count + 1, MAX_AUTO_VERTICAL_DIVIDERS + 1):
        response["search"]["attempted_candidate_count"] += 1
        # Validation is intentional: model_copy(update=...) would bypass the
        # canonical schema, including geometry restrictions and input bounds.
        candidate_document = {
            **original_document,
            "design": {
                **original_document["design"],
                "intent": {**original_document["design"]["intent"], "divider_count": divider_count},
            },
        }
        try:
            proposed_workspace = FurnitureWorkspace.model_validate(candidate_document)
            proposed = preview_furniture(proposed_workspace)
        except ValueError:
            continue
        response["search"]["evaluated_candidate_count"] += 1
        proposed_checks = _screening_checks(proposed)
        if not _passes(proposed_checks):
            continue
        bays = _bay_dimensions(proposed_workspace)
        response.update(
            state="available",
            code="SHELF_BAYS_AVAILABLE",
            message=f"Förslag: {divider_count + 1} jämnt fördelade fack klarar den numeriska "
            "screeningen för hyllbärighet vid oförändrade yttermått, material och angiven last. "
            "Granska förslaget innan det används som nytt utkast.",
            proposed=proposed,
            changed_fields=[
                {
                    "field": "design.intent.divider_count",
                    "before": intent.divider_count,
                    "after": divider_count,
                }
            ],
            proposed_bays=bays,
            screening_checks={"current": current_checks, "proposed": proposed_checks},
            remaining_issues={
                "rules": [
                    rule for rule in proposed["rules"]["evaluations"] if rule["status"] != "PASS"
                ],
                "manufacturing": proposed["manufacturing"]["issues"],
            },
            can_apply=True,
        )
        return response

    response.update(
        code="SEARCH_EXHAUSTED",
        message=f"Inget förslag upp till {MAX_AUTO_VERTICAL_DIVIDERS + 1} jämnt fördelade fack "
        "klarar samtliga numeriska kontroller vid nuvarande mått, material och last. "
        "En annan konstruktionsändring behöver granskas.",
    )
    return response
