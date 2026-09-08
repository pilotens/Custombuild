"""Family-specific screening, never inherited production qualification."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from custombuild_domain.furniture import ChestIntent, TableIntent
from custombuild_domain.furniture_catalog import resolve_material
from custombuild_domain.furniture_dimensions import review_furniture_dimensions
from custombuild_domain.furniture_engine import FurnitureResult

from .engine import RuleEngine

FURNITURE_RULES_VERSION = "furniture-rules-1.1.0"


def _rule(code: str, title: str, status: str, detail: str, **values: Any) -> dict[str, Any]:
    return {
        "rule_id": code,
        "title": title,
        "status": status,
        "detail": detail,
        "values": values,
        "rules_version": FURNITURE_RULES_VERSION,
    }


def evaluate_furniture(result: FurnitureResult) -> dict[str, Any]:
    p = result.spec.intent
    rules: list[dict[str, Any]] = []
    dimensions = review_furniture_dimensions(result.spec)
    for issue in dimensions["issues"]:
        rules.append(
            _rule(
                f"CB-{issue['code'].replace('_', '-')}",
                "Kundmått och listutrymme",
                "BLOCK",
                issue["message"],
            )
        )
    if result.shelving_result is not None:
        report = RuleEngine().evaluate(result.shelving_result)
        rules.extend(
            {
                "rule_id": rule.rule_id,
                "title": rule.title,
                "status": rule.status.value,
                "detail": " ".join(rule.assumptions),
                "values": {
                    "calculated": rule.calculated_value,
                    "allowed": rule.allowed_value,
                    "unit": rule.unit,
                },
                "rules_version": rule.rule_version,
            }
            for rule in report.evaluations
        )
    if isinstance(p, TableIntent):
        t = result.spec.material.measured_thickness_um
        material = resolve_material(result.spec.material)
        span = p.width_um - 2 * (p.end_inset_um + t)
        own_top = next(part.weight_g for part in result.parts if part.semantic_key == "table-top")
        load = Fraction(p.top_load_n) + Fraction(own_top * 981, 100_000)
        # Consistent N/mm/MPa units; do not credit unqualified stretcher joints.
        length_mm, breadth_mm, thickness_mm = (
            Fraction(span, 1_000),
            Fraction(p.depth_um, 1_000),
            Fraction(t, 1_000),
        )
        inertia = breadth_mm * thickness_mm**3 / 12
        modulus = Fraction(
            material.elastic_modulus_mpa * (1_000 - material.property_uncertainty_permille), 1_000
        )
        deflection = (
            5
            * load
            * length_mm**3
            / (384 * modulus * inertia)
            * Fraction(1_000 + material.creep_factor_permille, 1_000)
        )
        limit = min(Fraction(3), length_mm / 200)
        stress = 3 * load * length_mm / (4 * breadth_mm * thickness_mm**2)
        stress_limit = Fraction(
            material.bending_strength_mpa * (1_000 - material.property_uncertainty_permille), 1_800
        )
        rules.extend(
            (
                _rule(
                    "CB-TABLE-DEFLECTION-001",
                    "Bordsskivans nedböjning",
                    "BLOCK" if deflection > limit else "PASS",
                    "Enkelt upplagd skiva med jämn last och krypning. Sarghjälp räknas inte med "
                    "innan infästningarnas samverkan har verifierats.",
                    calculated_mm=round(float(deflection), 3),
                    allowed_mm=float(limit),
                    span_um=span,
                ),
                _rule(
                    "CB-TABLE-BENDING-001",
                    "Bordsskivans böjspänning",
                    "BLOCK" if stress > stress_limit else "PASS",
                    "Jämn last inklusive skivans egenvikt; osäkerhet och säkerhetsfaktor 1,8.",
                    calculated_mpa=round(float(stress), 3),
                    allowed_mpa=float(stress_limit),
                ),
                _rule(
                    "CB-TABLE-CONNECTION-001",
                    "Beninfästning och sidostabilitet",
                    "BLOCK",
                    "Välj och verifiera demonterbara beslag, borrbilder, lyft-/kantlaster och "
                    "sidostabilitet för denna bordskonstruktion. "
                    "Layoutprofilen saknar dessa bevis.",
                ),
            )
        )
    if isinstance(p, ChestIntent):
        parts = {part.part_id: part for part in result.parts}
        # Front floor edge is y=0. Consider all drawers fully extended because
        # the layout profile provides no qualified one-drawer interlock.
        weight_moment = sum(
            Fraction(part.weight_g * 981, 100_000)
            * Fraction(2 * part.placement.y_um + part.finished_size.depth_um, 2)
            for part in result.parts
        )
        for group in result.moving_groups:
            weight_moment -= (
                Fraction(sum(parts[i].weight_g for i in group.part_ids) * 981, 100_000)
                * group.travel_um
            )
            box_bottom = next(
                parts[i] for i in group.part_ids if parts[i].semantic_key.endswith("-bottom")
            )
            load_y = (
                Fraction(2 * box_bottom.placement.y_um + box_bottom.finished_size.depth_um, 2)
                - group.travel_um
            )
            # Do not rely on stored contents to provide a restoring moment.
            weight_moment += min(Fraction(0), group.rated_load_n * load_y)
        residual = weight_moment - 50 * p.height_um
        rules.extend(
            (
                _rule(
                    "CB-DRAWER-TIP-001",
                    "Tipprisk med öppna lådor",
                    "BLOCK" if residual <= 0 else "WARNING",
                    "Alla lådor helt utdragna; angiven last där den ökar vältmomentet och "
                    "50 N horisontell kraft vid möbelns överkant. Förankring, verklig "
                    "massfördelning och prov måste verifieras separat.",
                    residual_moment_nm=round(float(residual / 1_000_000), 3),
                    open_drawers=len(result.moving_groups),
                ),
                _rule(
                    "CB-DRAWER-HARDWARE-001",
                    "Lådsystem och lådbotten",
                    "BLOCK",
                    "Tillverkarens beslag, hålbilder, lastkapacitet, lådbotteninfästning och "
                    "utdragsstopp saknas. Layoutmått räcker för formgivning "
                    "men inte för tillverkning.",
                ),
            )
        )
    rules.append(
        _rule(
            "CB-FURNITURE-MATERIAL-001",
            "Material och faktisk batch",
            "WARNING",
            "Materialprofilen innehåller indikativa värden. Materialintyg, batch och berörda "
            "förband behöver verifieras för den valda möbeln.",
        )
    )
    return {
        "rules_version": FURNITURE_RULES_VERSION,
        "design_hash": result.design_hash,
        "overall_status": "BLOCK" if any(r["status"] == "BLOCK" for r in rules) else "WARNING",
        "evaluations": rules,
        "production_qualified": False,
        "disclaimer": "Konstruktionsscreening och layoutkontroll; inte produktcertifiering.",
    }
