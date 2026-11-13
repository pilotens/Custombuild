"""Family-specific screening, never inherited production qualification."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from custombuild_domain.enums import MaterialType
from custombuild_domain.furniture import ChairIntent, ChestIntent, DiningTableIntent, TableIntent
from custombuild_domain.furniture_catalog import resolve_material
from custombuild_domain.furniture_dimensions import review_furniture_dimensions
from custombuild_domain.furniture_engine import FurnitureResult
from custombuild_domain.furniture_frames import rotated_yz
from custombuild_domain.models import TRIAL_ANGLE_SET_MASS_G

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
    dimensions = review_furniture_dimensions(result.spec, result.shelving_result)
    for issue in dimensions["issues"]:
        rules.append(
            _rule(
                f"CB-{issue['code'].replace('_', '-')}",
                "Plats för stående montering"
                if issue["code"].startswith("STANDING_")
                else "Kundmått och listutrymme",
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
    if result.trial_angles and result.shelving_result:
        rules.append(
            _rule(
                "CB-SHELF-ANGLE-001",
                "Mekanisk låsning för konstruktionsprov",
                "BLOCK",
                "Vinklarnas hålbild och infästningssidor är modellerade. Utdrag, skjuvning, "
                "åtdragning och fuktrörelse i träbatchen saknar ännu verifierad brukskapacitet. "
                "Tippningsscreeningen tillgodoräknar ingen stabiliserande beslagsvikt.",
                angle_count=len(result.trial_angles),
                screw_count=2 * len(result.trial_angles),
                estimated_fitting_mass_g=len(result.trial_angles) * TRIAL_ANGLE_SET_MASS_G,
                required_joint_shear_n=result.shelving_result.minimum_retention_shear_load_n,
            )
        )
    if isinstance(p, DiningTableIntent | TableIntent):
        t = result.spec.material.measured_thickness_um
        material = resolve_material(result.spec.material)
        span = p.width_um - 2 * (p.end_inset_um + t)
        if isinstance(p, DiningTableIntent):
            span = next(
                part.finished_size.width_um
                for part in result.parts
                if part.semantic_key == "rail-front"
            )
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
        round_top = isinstance(p, DiningTableIntent) and p.top_shape == "round"
        if round_top:
            rules.append(
                _rule(
                    "CB-TABLE-ROUND-TOP-001",
                    "Rund bordsskiva med överhäng",
                    "BLOCK",
                    "Kontrollera nedböjning och böjspänning för den runda skivan och "
                    "dess verkliga upplag. En rektangulär balkmodell täcker inte överhänget.",
                )
            )
        else:
            rules.extend(
                (
                    _rule(
                        "CB-TABLE-DEFLECTION-001",
                        "Bordsskivans nedböjning",
                        "BLOCK" if deflection > limit else "PASS",
                        "Enkelt upplagd skiva med jämn last och krypning. Sarghjälp räknas "
                        "inte med innan infästningarnas samverkan har verifierats.",
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
                )
            )
        rules.append(
            _rule(
                "CB-TABLE-CONNECTION-001",
                "Beninfästning och sidostabilitet",
                "BLOCK",
                "Parade skruvförband är ritade. Provmontera hörnet och verifiera "
                "sidostabilitet, kantlast och utdragskraft."
                if isinstance(p, DiningTableIntent)
                else "Välj och verifiera demonterbara beslag, borrbilder, lyft-/kantlaster och "
                "sidostabilitet för denna bordskonstruktion. Layoutprofilen saknar dessa bevis.",
            )
        )
    if isinstance(p, ChairIntent):
        # Moment about the rear feet of the empty chair, without taking credit for
        # an occupant's weight or assuming its location.
        rear_edge = max(
            part.placement.y_um + part.finished_size.depth_um
            for part in result.parts
            if part.semantic_key.startswith("leg-rear-")
        )
        restoring_nm = 0.0
        for part in result.parts:
            size, loc = part.finished_size, part.placement
            cy, _ = rotated_yz(size.depth_um / 2, size.height_um / 2, loc.rotation_x_mdeg)
            restoring_nm += part.weight_g * 9.81 / 1000 * (rear_edge - loc.y_um - cy) / 1e6
        rules.append(
            _rule(
                "CB-CHAIR-REAR-TIP-001",
                "Tom stols tippgräns bakåt",
                "WARNING",
                "Uppskattad horisontell kraft vid ryggens överkant när framfötterna "
                "börjar lyfta. Beräkningen antar plant golv och stillastående tom stol. "
                "Sittande användare, glidning och dynamiska laster kräver andra prov.",
                estimated_force_n=round(restoring_nm / (p.height_um / 1e6), 2),
                restoring_moment_nm=round(restoring_nm, 3),
            )
        )
        rules.append(
            _rule(
                "CB-CHAIR-PROTOTYPE-001",
                "Stolens hållfasthet och stabilitet",
                "BLOCK",
                (
                    "Verifiera sits, rygg, förband, tipprisk och upprepad belastning på "
                    "monterad prototyp."
                ),
                requested_seat_load_n=p.seat_load_n,
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
            weight_moment += min(Fraction(0), group.requested_load_n * load_y)
        residual = weight_moment - 50 * p.height_um
        hardware = result.hardware_profile
        documented_runner = bool(hardware and hardware.rated_pair_load_kg)
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
                    "Skenans borrbild och lådans skruvförband ingår. Provmontera och "
                    "kontrollera gång, infästning och stopp."
                    if documented_runner
                    else "Tillverkarens beslag, hålbilder, lastkapacitet, lådbotteninfästning och "
                    "utdragsstopp saknas. Layoutmått räcker för formgivning "
                    "men inte för tillverkning.",
                ),
            )
        )
        if hardware and hardware.rated_pair_load_kg:
            box_width = max(
                part.finished_size.width_um
                for part in result.parts
                if part.semantic_key.startswith("drawer-") and part.semantic_key.endswith("-bottom")
            )
            opening = box_width + 2 * hardware.side_clearance_um
            rules.append(
                _rule(
                    "CB-RUNNER-WIDTH-001",
                    "Tillverkarens rekommenderade öppningsbredd",
                    "BLOCK" if opening > 550_000 else "PASS",
                    "Hettich MS 10547 anger högst 550 mm som rekommenderad invändig bredd "
                    "per lådfack.",
                    opening_mm=opening / 1000,
                    recommended_max_mm=550,
                )
            )
            gross_mass_g = max(
                sum(parts[i].weight_g for i in g.part_ids) for g in result.moving_groups
            )
            total_n = p.drawer_load_n + gross_mass_g * 9.81 / 1000
            rules.append(
                _rule(
                    "CB-RUNNER-LOAD-001",
                    "Belastning per skenpar",
                    "BLOCK" if total_n > hardware.rated_pair_load_kg * 9.81 else "WARNING",
                    (
                        "Lådans egenvikt och önskad nyttolast jämförs med skenparets katalogvärde. "
                        "Monteringsvillkor måste verifieras."
                    ),
                    gross_load_n=round(total_n, 2),
                    rated_pair_load_kg=hardware.rated_pair_load_kg,
                )
            )
    if resolve_material(result.spec.material).material_type == MaterialType.SOLID_WOOD:
        rules.append(
            _rule(
                "CB-SOLID-WOOD-001",
                "Fuktrörelse och verkligt virke",
                "BLOCK",
                "Kontrollera art, batch, fuktkvot och fiberriktning. Verkstaden behöver "
                "granska rörelsemån tvärs fibrerna och hur förband och beslag tillåter "
                "rörelse innan möbeln kan tillverkas.",
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
