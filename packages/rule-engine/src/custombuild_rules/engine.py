from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from custombuild_domain import (
    BackPanelType,
    BookcaseDesignSpec,
    BookcaseParameters,
    DesignResult,
    FeatureKind,
    JointType,
    PartInstance,
    PartRole,
    ReinforcementMode,
    ShelfMount,
    WallAnchorSpec,
    build_bookcase,
    stable_id,
)

from .models import (
    ActionType,
    AutoCorrectionResult,
    CalculationStep,
    DesignDiff,
    ParameterChange,
    RuleDatum,
    RuleEvaluation,
    RuleReport,
    RuleStatus,
    SuggestedAction,
    aggregate_status,
)

RULES_VERSION = "1.3.0"
MAX_AUTO_VERTICAL_DIVIDERS = 16


@dataclass(frozen=True, slots=True)
class _DadoSupportCase:
    joint_id: str
    shelf_part_id: str
    support_part_id: str
    engagement_um: int
    bearing_length_um: int
    bearing_area_um2: int
    allowed_load_n: int


def _ceil(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _margin_permille(calculated: int, allowed: int) -> int:
    if allowed <= 0:
        return -1_000_000
    return ((allowed - calculated) * 1_000) // allowed


def _threshold_status(calculated: int, allowed: int, warning_permille: int = 800) -> RuleStatus:
    if calculated > allowed:
        return RuleStatus.BLOCK
    if calculated * 1_000 >= allowed * warning_permille:
        return RuleStatus.WARNING
    return RuleStatus.PASS


class RuleEngine:
    """Versioned deterministic structural screening for the bookcase template."""

    version = RULES_VERSION

    def evaluate(self, design: DesignResult) -> RuleReport:
        evaluations: tuple[RuleEvaluation, ...] = (
            self._shelf_deflection(design),
            self._shelf_bending(design),
            self._joint_capacity(design),
            self._lateral_stability(design),
            self._tip_risk(design),
        )
        cabinet_parts = tuple(
            part
            for part in design.parts
            if part.role
            in {
                PartRole.BASE_SIDE,
                PartRole.BASE_BOTTOM,
                PartRole.BASE_TOP,
                PartRole.CABINET_FRONT,
            }
        )
        if cabinet_parts:
            evaluations += (
                self._base_support_alignment(design),
                self._base_cabinet_hardware(cabinet_parts),
            )
        return RuleReport(
            design_hash=design.design_hash,
            rules_version=self.version,
            overall_status=aggregate_status(item.status for item in evaluations),
            evaluations=evaluations,
        )

    def _base_support_alignment(self, design: DesignResult) -> RuleEvaluation:
        """Require a direct vertical load path below every upper divider."""

        dividers = tuple(
            sorted(
                (part for part in design.parts if part.role == PartRole.DIVIDER),
                key=lambda part: part.placement.x_um,
            )
        )
        base_sides = tuple(
            sorted(
                (part for part in design.parts if part.role == PartRole.BASE_SIDE),
                key=lambda part: part.placement.x_um,
            )
        )
        # Full-height carcass sides are the two outer supports.  Every
        # BASE_SIDE is therefore a real internal module boundary.
        internal_base_sides = base_sides
        tolerance_um = 1_000
        unsupported = tuple(
            divider
            for divider in dividers
            if not any(
                abs(support.placement.x_um - divider.placement.x_um) <= tolerance_um
                for support in internal_base_sides
            )
        )
        target_modules = len(dividers) + 1
        position_text = ", ".join(
            f"{round(part.placement.x_um / 1_000)} mm" for part in unsupported
        )
        status = RuleStatus.PASS if not unsupported else RuleStatus.BLOCK
        actions: tuple[SuggestedAction, ...] = ()
        if unsupported:
            actions = (
                SuggestedAction(
                    action_id=stable_id("rule-action", design.design_hash, "align-base-cabinets"),
                    action_type=ActionType.ALIGN_BASE_CABINETS,
                    description=(
                        f"Skapa {target_modules} lika breda underskåpsmoduler och rikta en "
                        "fullhöjd underskåpssida under varje övre avdelare. Nödvändiga "
                        f"centrumlinjer från vänster ytterkant: {position_text}."
                    ),
                    changes=(
                        ParameterChange(
                            path="parameters.base_cabinet_count",
                            before=design.spec.parameters.base_cabinet_count,
                            after=target_modules,
                        ),
                    ),
                ),
            )
        bottom_ids = tuple(part.part_id for part in design.parts if part.role == PartRole.BOTTOM)
        return RuleEvaluation(
            rule_id="CB-SUPPORT-001",
            rule_version=self.version,
            title="Lodrät lastväg genom underskåpen",
            status=status,
            applies_to_part_ids=bottom_ids
            + tuple(part.part_id for part in (unsupported or dividers)),
            inputs=(
                RuleDatum(name="övre_avdelare", value=len(dividers), unit="st"),
                RuleDatum(
                    name="inre_underskåpssidor",
                    value=len(internal_base_sides),
                    unit="st",
                ),
                RuleDatum(
                    name="ostödda_centrumlinjer",
                    value=position_text or "inga",
                ),
            ),
            assumptions=(
                "Varje bärande övre avdelare ska ha en genomgående underskåpssida "
                "på samma centrumlinje.",
                "Mellanbottnen är inte verifierad som en fritt spännande balk för "
                "koncentrerade laster.",
            ),
            trace=(
                CalculationStep(
                    expression="|x_övre avdelare - x_underskåpssida| ≤ 1 mm",
                    result=str(len(unsupported)),
                    unit="ostödda avdelare",
                ),
            ),
            calculated_value=len(unsupported),
            allowed_value=0,
            unit="ostödda avdelare",
            safety_margin_permille=1_000 if not unsupported else -1_000_000,
            suggested_actions=actions,
        )

    def _base_cabinet_hardware(self, cabinet_parts: tuple[PartInstance, ...]) -> RuleEvaluation:
        part_ids = tuple(part.part_id for part in cabinet_parts)
        front_count = sum(1 for part in cabinet_parts if part.role == PartRole.CABINET_FRONT)
        return RuleEvaluation(
            rule_id="CB-HARDWARE-001",
            rule_version=self.version,
            title="Beslag och borrbild för underskåp",
            status=RuleStatus.WARNING,
            applies_to_part_ids=part_ids,
            inputs=(RuleDatum(name="frontantal", value=front_count, unit="st"),),
            assumptions=(
                "Stom- och frontgeometri ingår i dellistan.",
                "Gångjärn, öppningsvinkel, frontspel och infästning är ännu "
                "inte valda från ett versionslåst beslagssystem.",
            ),
            trace=(
                CalculationStep(
                    expression="verifierat beslagssystem = valt och versionslåst",
                    result="nej",
                ),
            ),
            calculated_value=1,
            allowed_value=0,
            unit="obekräftade system",
            safety_margin_permille=-1_000,
            suggested_actions=(
                SuggestedAction(
                    action_id=stable_id("rule-action", "base-cabinet-hardware"),
                    action_type=ActionType.VERIFY_HARDWARE_CAPACITY,
                    description=(
                        "Välj och verifiera gångjärn, montageplatta, frontspel "
                        "och borrbild innan produktionsrelease."
                    ),
                    requires_user_evidence=True,
                ),
            ),
        )

    def _shelf_geometry(self, design: DesignResult) -> tuple[int, int, int, int, tuple[str, ...]]:
        shelves = tuple(part for part in design.parts if part.role == PartRole.SHELF)
        if not shelves:
            return 0, 1, design.spec.parameters.actual_thickness_um, 0, ()
        spans = {
            part.part_id: self._shelf_clear_span(design, part.part_id, part.finished_size.width_um)
            for part in shelves
        }
        worst_span = max(spans.values())
        shelf_depth = min(part.finished_size.depth_um for part in shelves)
        thickness = design.spec.parameters.actual_thickness_um
        affected = tuple(part.part_id for part in shelves if spans[part.part_id] == worst_span)
        bay_count = design.spec.parameters.vertical_divider_count + 1
        representative_row = sorted(shelves, key=lambda part: part.instance_index)[:bay_count]
        row_width = sum(part.finished_size.width_um for part in representative_row)
        affected_width = max(
            part.finished_size.width_um for part in shelves if part.part_id in affected
        )
        design_load_n = (
            _ceil(Fraction(design.spec.parameters.shelf_load_n * affected_width, row_width))
            if row_width
            else 0
        )
        return worst_span, shelf_depth, thickness, design_load_n, affected

    @staticmethod
    def _shelf_clear_span(
        design: DesignResult,
        shelf_part_id: str,
        fallback_width_um: int,
    ) -> int:
        """Use support-face joint coordinates, excluding dado insertion tongues."""

        support_x = tuple(
            joint.mating_origin.x_um
            for joint in design.joints
            if any(member.part_id == shelf_part_id for member in joint.members)
        )
        if len(support_x) >= 2 and max(support_x) > min(support_x):
            return max(support_x) - min(support_x)
        return fallback_width_um

    def _divider_actions(
        self, design: DesignResult, action_type: ActionType = ActionType.ADD_VERTICAL_DIVIDER
    ) -> tuple[SuggestedAction, ...]:
        current = design.spec.parameters.vertical_divider_count
        if current >= MAX_AUTO_VERTICAL_DIVIDERS:
            return ()
        return (
            SuggestedAction(
                action_id=stable_id("rule-action", design.design_hash, action_type.value),
                action_type=action_type,
                description=(
                    "Lägg till en genomgående vertikal avdelare för att korta fri spännvidd; "
                    "räkna därefter om delar, fogar, BOM, nesting och montering."
                ),
                changes=(
                    ParameterChange(
                        path="parameters.vertical_divider_count",
                        before=current,
                        after=current + 1,
                    ),
                ),
            ),
        )

    def _shelf_deflection(self, design: DesignResult) -> RuleEvaluation:
        p, material = design.spec.parameters, design.spec.material
        span, depth, thickness, design_load_n, affected = self._shelf_geometry(design)
        if not affected or p.shelf_load_n == 0:
            calculated = 0
        else:
            # δ = 5*W*L³/(32*E*b*t³), using conservative E and creep.
            calculated = _ceil(
                Fraction(
                    5
                    * design_load_n
                    * span**3
                    * 1_000_000
                    * (1_000 + material.creep_factor_permille),
                    32
                    * material.elastic_modulus_mpa
                    * depth
                    * thickness**3
                    * (1_000 - material.property_uncertainty_permille),
                )
            )
        allowed = (
            min(p.max_deflection_um, span // p.deflection_span_ratio)
            if span
            else p.max_deflection_um
        )
        status = _threshold_status(calculated, allowed)
        actions = (
            ()
            if status == RuleStatus.PASS
            else (
                *self._divider_actions(design),
                SuggestedAction(
                    action_id=stable_id(
                        "rule-action", design.design_hash, "stronger-material-deflection"
                    ),
                    action_type=ActionType.USE_STRONGER_MATERIAL,
                    description=(
                        "Välj en versionshanterad materialpost med högre "
                        "verifierad elasticitetsmodul."
                    ),
                ),
            )
        )
        return RuleEvaluation(
            rule_id="CB-DEFLECTION-001",
            rule_version=self.version,
            title="Långtidsnedböjning för hylla",
            status=status,
            applies_to_part_ids=affected,
            inputs=(
                RuleDatum(name="fri_spännvidd", value=span, unit="µm"),
                RuleDatum(name="hylllast_per_rad", value=p.shelf_load_n, unit="N"),
                RuleDatum(name="dimensionerande_facklast", value=design_load_n, unit="N"),
                RuleDatum(name="elasticitetsmodul", value=material.elastic_modulus_mpa, unit="MPa"),
                RuleDatum(
                    name="materialosäkerhet", value=material.property_uncertainty_permille, unit="‰"
                ),
                RuleDatum(name="krypfaktor", value=material.creep_factor_permille, unit="‰"),
            ),
            assumptions=(
                "Hyllan screenas som enkelt upplagd balk med jämnt fördelad total last.",
                "Angiven radlast fördelas lika mellan facken.",
                "Elasticitetsmodulen reduceras med katalogpostens dokumenterade osäkerhet.",
            ),
            trace=(
                CalculationStep(
                    expression="δ = 5·W·L³/(32·E_eff·b·t³) · (1 + creep)",
                    result=str(calculated),
                    unit="µm",
                ),
                CalculationStep(
                    expression="δ_allow = min(max_deflection, L/span_ratio)",
                    result=str(allowed),
                    unit="µm",
                ),
            ),
            calculated_value=calculated,
            allowed_value=allowed,
            unit="µm",
            safety_margin_permille=_margin_permille(calculated, allowed),
            suggested_actions=actions,
        )

    def _joint_capacity(self, design: DesignResult) -> RuleEvaluation:
        """Screen local support without claiming that a plain groove locks permanently."""

        p, material = design.spec.parameters, design.spec.material
        shelves = tuple(part for part in design.parts if part.role == PartRole.SHELF)
        bay_count = p.vertical_divider_count + 1
        _, _, _, design_load_n, _ = self._shelf_geometry(design)
        demand_n = _ceil(Fraction(design_load_n, 2)) if shelves else 0

        if p.shelf_mount == ShelfMount.ADJUSTABLE and shelves:
            shelf_ids = {part.part_id for part in shelves}
            shelf_pin_joints = tuple(
                joint
                for joint in design.joints
                if joint.joint_type == JointType.SHELF_PIN
                and any(member.part_id in shelf_ids for member in joint.members)
            )
            affected_pin_part_ids = tuple(
                sorted({member.part_id for joint in shelf_pin_joints for member in joint.members})
            )
            hardware_skus = tuple(
                sorted(
                    {
                        joint.hardware_sku
                        for joint in shelf_pin_joints
                        if joint.hardware_sku is not None
                    }
                )
            )
            return RuleEvaluation(
                rule_id="CB-JOINT-001",
                rule_version=self.version,
                title="Lokalt upplag i hyllspår och hyllbärare",
                status=RuleStatus.BLOCK,
                applies_to_part_ids=affected_pin_part_ids,
                inputs=(
                    RuleDatum(name="hylltyp", value=ShelfMount.ADJUSTABLE.value),
                    RuleDatum(name="hylllast_per_rad", value=p.shelf_load_n, unit="N"),
                    RuleDatum(name="antal_fack", value=bay_count, unit="fack"),
                    RuleDatum(name="dimensionerande_last_per_stöd", value=demand_n, unit="N"),
                    RuleDatum(
                        name="beslags_sku",
                        value="|".join(hardware_skus) if hardware_skus else None,
                    ),
                    RuleDatum(name="beslagskapacitetsversion", value=None),
                ),
                assumptions=(
                    "Radlasten fördelas efter respektive facks bredd och reaktionen lika mellan "
                    "två stöd per hyllsegment.",
                    "Ingen leverantörskapacitet, materialkompatibilitet eller borrbild för "
                    "hyllbäraren finns i den versionshanterade katalogen.",
                    "Systemet antar därför inte ett beslagstal, inte ens vid noll angiven last.",
                    "Lim, fogmassa, epoxy och annan adhesiv låsning är förbjuden enligt "
                    "produktpolicyn.",
                ),
                trace=(
                    CalculationStep(
                        expression="R_stöd = ceil(W_rad·fackandel/2)",
                        result=str(demand_n),
                        unit="N",
                    ),
                    CalculationStep(
                        expression="F_tillåten = versionshanterad leverantörskapacitet",
                        result="saknas",
                        unit="N",
                    ),
                ),
                calculated_value=demand_n,
                allowed_value=0,
                unit="N",
                safety_margin_permille=-1_000_000,
                suggested_actions=(
                    SuggestedAction(
                        action_id=stable_id(
                            "rule-action", design.design_hash, "verify-shelf-pin-capacity"
                        ),
                        action_type=ActionType.VERIFY_HARDWARE_CAPACITY,
                        description=(
                            "Registrera versionshanterad tillverkardata för exakt hyllbärare, "
                            "borrbild, material och uppmätt tjocklek innan designen godkänns."
                        ),
                        requires_user_evidence=True,
                    ),
                    self._dry_joining_action(design),
                ),
            )

        cases = self._dado_support_cases(design)
        if not shelves:
            engagement_um = bearing_length_um = bearing_area_um2 = 0
            allowed_n = 0
            affected: tuple[str, ...] = ()
            worst_joint_id: str | None = None
        elif not cases:
            affected = tuple(sorted(part.part_id for part in shelves))
            return RuleEvaluation(
                rule_id="CB-JOINT-001",
                rule_version=self.version,
                title="Lokalt upplag i hyllspår och hyllbärare",
                status=RuleStatus.BLOCK,
                applies_to_part_ids=affected,
                inputs=(
                    RuleDatum(name="hylltyp", value=p.shelf_mount.value),
                    RuleDatum(name="hylllast_per_rad", value=p.shelf_load_n, unit="N"),
                    RuleDatum(name="antal_fack", value=bay_count, unit="fack"),
                    RuleDatum(name="dimensionerande_last_per_stöd", value=demand_n, unit="N"),
                    RuleDatum(name="matchande_dado_stödfogar", value=0, unit="st"),
                    RuleDatum(
                        name="materialversion",
                        value=f"{material.material_id}@{material.version}",
                    ),
                    RuleDatum(
                        name="materialkälla",
                        value=f"{material.source.source_id}@{material.source.revision}",
                    ),
                ),
                assumptions=(
                    "En fast hylla måste ha en matchande kanonisk DADO-stödfog innan lokal "
                    "bärförmåga kan beräknas.",
                    "Ingen fogarea eller tillåten last antas när den genererade geometrin saknas.",
                    "Ett vanligt spår är endast geometriskt upplag och får inte beskrivas som "
                    "permanent hållning utan verifierad självlåsning eller mekanisk säkring.",
                ),
                trace=(
                    CalculationStep(
                        expression="A_bär = verkligt dado-insticksdjup · bärande hyllängd",
                        result="saknas",
                        unit="µm²",
                    ),
                    CalculationStep(
                        expression="F_tillåten = f_v,eff·A_bär",
                        result="ej beräknad",
                        unit="N",
                    ),
                ),
                calculated_value=demand_n,
                allowed_value=0,
                unit="N",
                safety_margin_permille=-1_000_000,
                suggested_actions=(
                    SuggestedAction(
                        action_id=stable_id(
                            "rule-action", design.design_hash, "regenerate-joint-geometry"
                        ),
                        action_type=ActionType.REGENERATE_JOINT_GEOMETRY,
                        description=(
                            "Regenerera designen från den versionsatta bokhyllemallen och "
                            "utred varför fast hylla saknar en matchande DADO-stödfog."
                        ),
                    ),
                    self._dry_joining_action(design),
                ),
            )
        else:
            worst = min(cases, key=lambda item: (item.allowed_load_n, item.joint_id))
            engagement_um = worst.engagement_um
            bearing_length_um = worst.bearing_length_um
            bearing_area_um2 = worst.bearing_area_um2
            allowed_n = worst.allowed_load_n
            affected = tuple(sorted((worst.shelf_part_id, worst.support_part_id)))
            worst_joint_id = worst.joint_id

        capacity_status = RuleStatus.PASS if not shelves else _threshold_status(demand_n, allowed_n)
        # The current DADO contract proves geometry and local bearing only. It
        # has no versioned retention evidence and must therefore remain a
        # review decision even when its numeric capacity screen passes.
        status = RuleStatus.BLOCK if capacity_status == RuleStatus.BLOCK else RuleStatus.WARNING
        actions: tuple[SuggestedAction, ...] = (self._dry_joining_action(design),)
        if capacity_status != RuleStatus.PASS:
            pass_demand_n = max(0, (allowed_n * 800 - 1) // 1_000)
            recommended_row_load_n = min(
                p.shelf_load_n,
                (
                    2 * pass_demand_n * p.shelf_load_n // design_load_n
                    if design_load_n
                    else p.shelf_load_n
                ),
            )
            actions = (
                *self._divider_actions(design),
                SuggestedAction(
                    action_id=stable_id("rule-action", design.design_hash, "reduce-joint-load"),
                    action_type=ActionType.REDUCE_LOAD,
                    description=(
                        "Sänk angiven radlast till den deterministiskt beräknade nivån och "
                        "validera en ny designrevision."
                    ),
                    changes=(
                        ParameterChange(
                            path="parameters.shelf_load_n",
                            before=p.shelf_load_n,
                            after=recommended_row_load_n,
                        ),
                    ),
                ),
                self._dry_joining_action(design),
            )

        effective_shear_kpa = (
            material.shear_strength_mpa
            * 1_000
            * (1_000 - material.property_uncertainty_permille)
            * 1_000
            // ((1_000 + material.creep_factor_permille) * p.structural_safety_factor_permille)
        )
        return RuleEvaluation(
            rule_id="CB-JOINT-001",
            rule_version=self.version,
            title="Lokalt upplag i hyllspår och hyllbärare",
            status=status,
            applies_to_part_ids=affected,
            inputs=(
                RuleDatum(name="hylltyp", value=p.shelf_mount.value),
                RuleDatum(name="hylllast_per_rad", value=p.shelf_load_n, unit="N"),
                RuleDatum(name="antal_fack", value=bay_count, unit="fack"),
                RuleDatum(name="dimensionerande_last_per_stöd", value=demand_n, unit="N"),
                RuleDatum(name="dimensionerande_fog_id", value=worst_joint_id),
                RuleDatum(name="permanent_hållning_verifierad", value=False),
                RuleDatum(name="dado_engagement", value=engagement_um, unit="µm"),
                RuleDatum(name="bärande_längd", value=bearing_length_um, unit="µm"),
                RuleDatum(name="bärande_area", value=bearing_area_um2, unit="µm²"),
                RuleDatum(name="skjuvhållfasthet", value=material.shear_strength_mpa, unit="MPa"),
                RuleDatum(
                    name="materialosäkerhet",
                    value=material.property_uncertainty_permille,
                    unit="‰",
                ),
                RuleDatum(name="krypfaktor", value=material.creep_factor_permille, unit="‰"),
                RuleDatum(
                    name="strukturell_säkerhetsfaktor",
                    value=p.structural_safety_factor_permille,
                    unit="‰",
                ),
                RuleDatum(
                    name="materialversion",
                    value=f"{material.material_id}@{material.version}",
                ),
                RuleDatum(
                    name="materialkälla",
                    value=f"{material.source.source_id}@{material.source.revision}",
                ),
                RuleDatum(name="materialkällans_titel", value=material.source.title),
            ),
            assumptions=(
                "Radlasten fördelas efter fackbredd och ger halva "
                "facklasten som reaktion i vardera stödfogen.",
                "Bärande area härleds från den kanoniska fogens verkliga spårdjup gånger "
                "hyllans överlappande djup; nominell materialtjocklek används inte som instick.",
                "Versionspostens skjuvhållfasthet används konservativt som screening för "
                "lokal bärning/skjuvning och reduceras för osäkerhet, krypning och "
                "strukturell säkerhetsfaktor.",
                "Screeningen verifierar endast lokalt upplag; ett vanligt DADO-spår är inte "
                "självlåsande och verifierar inte permanent hållning.",
                "Fysisk montering kräver ett verifierat självlåsande torrförband eller en "
                "versionslåst mekanisk säkring som hindrar isärdragning i omvänd "
                "monteringsriktning.",
                "Lim, fogmassa, epoxy och annan adhesiv låsning är förbjuden och ingår aldrig "
                "som bärande antagande.",
                "Screeningen verifierar inte kantutbrott, slaglast, utmattning eller "
                "materialsats och är inte en certifiering av hela förbandet.",
            ),
            trace=(
                CalculationStep(
                    expression="R_stöd = ceil(W_rad·fackandel/2)",
                    result=str(demand_n),
                    unit="N",
                ),
                CalculationStep(
                    expression="A_bär = verkligt dado-insticksdjup · bärande hyllängd",
                    result=str(bearing_area_um2),
                    unit="µm²",
                ),
                CalculationStep(
                    expression="f_v,eff = f_v·(1-u)/(1+creep)/säkerhetsfaktor",
                    result=str(effective_shear_kpa),
                    unit="kPa",
                ),
                CalculationStep(
                    expression="F_tillåten = f_v,eff·A_bär",
                    result=str(allowed_n),
                    unit="N",
                ),
            ),
            calculated_value=demand_n,
            allowed_value=allowed_n,
            unit="N",
            safety_margin_permille=(0 if not shelves else _margin_permille(demand_n, allowed_n)),
            suggested_actions=actions,
        )

    @staticmethod
    def _dry_joining_action(design: DesignResult) -> SuggestedAction:
        return SuggestedAction(
            action_id=stable_id("rule-action", design.design_hash, "verify-dry-joining-system"),
            action_type=ActionType.VERIFY_DRY_JOINING_SYSTEM,
            description=(
                "Välj och versionslås ett självlåsande torrförband eller en demonterbar "
                "mekanisk säkring som hindrar isärdragning; adhesiver är förbjudna. "
                "Beslutet är endast designgranskning och ger inte fysisk frisläppning."
            ),
            requires_user_evidence=True,
        )

    @staticmethod
    def _dado_support_cases(design: DesignResult) -> tuple[_DadoSupportCase, ...]:
        p, material = design.spec.parameters, design.spec.material
        by_id = {part.part_id: part for part in design.parts}
        feature_by_id = {
            feature.feature_id: feature for part in design.parts for feature in part.features
        }
        cases: list[_DadoSupportCase] = []
        for joint in sorted(design.joints, key=lambda item: item.joint_id):
            if joint.joint_type != JointType.DADO:
                continue
            shelf_members = tuple(
                member for member in joint.members if by_id[member.part_id].role == PartRole.SHELF
            )
            cut_members = tuple(member for member in joint.members if member.feature_ids)
            if len(shelf_members) != 1 or len(cut_members) != 1:
                continue
            shelf_member, cut_member = shelf_members[0], cut_members[0]
            if len(cut_member.feature_ids) != 1:
                continue
            feature = feature_by_id.get(cut_member.feature_ids[0])
            shelf = by_id[shelf_member.part_id]
            support = by_id[cut_member.part_id]
            if (
                feature is None
                or feature.kind != FeatureKind.GROOVE
                or feature.dimensions.depth_um is None
                or support.material_id != material.material_id
                or support.material_version != material.version
            ):
                continue
            in_plane_lengths = tuple(
                value
                for value in (
                    feature.dimensions.width_um,
                    feature.dimensions.length_um,
                )
                if value is not None
            )
            if not in_plane_lengths:
                continue
            engagement_um = feature.dimensions.depth_um
            bearing_length_um = min(
                shelf.finished_size.depth_um,
                max(in_plane_lengths),
            )
            bearing_area_um2 = engagement_um * bearing_length_um
            allowed_load_n = (
                material.shear_strength_mpa
                * bearing_area_um2
                * (1_000 - material.property_uncertainty_permille)
                // (
                    1_000
                    * (1_000 + material.creep_factor_permille)
                    * p.structural_safety_factor_permille
                )
            )
            cases.append(
                _DadoSupportCase(
                    joint_id=joint.joint_id,
                    shelf_part_id=shelf.part_id,
                    support_part_id=support.part_id,
                    engagement_um=engagement_um,
                    bearing_length_um=bearing_length_um,
                    bearing_area_um2=bearing_area_um2,
                    allowed_load_n=allowed_load_n,
                )
            )
        return tuple(cases)

    def _shelf_bending(self, design: DesignResult) -> RuleEvaluation:
        p, material = design.spec.parameters, design.spec.material
        span, depth, thickness, design_load_n, affected = self._shelf_geometry(design)
        if not affected or p.shelf_load_n == 0:
            calculated_kpa = 0
        else:
            # sigma = 3*W*L/(2*b*t²); factor 1e9 converts the µm expression to kPa.
            calculated_kpa = _ceil(
                Fraction(
                    3 * design_load_n * span * 1_000_000_000,
                    2 * depth * thickness**2,
                )
            )
        allowed_kpa = (
            material.bending_strength_mpa
            * (1_000 - material.property_uncertainty_permille)
            * 1_000
            // p.structural_safety_factor_permille
        )
        status = _threshold_status(calculated_kpa, allowed_kpa)
        actions = (
            ()
            if status == RuleStatus.PASS
            else (
                *self._divider_actions(design),
                SuggestedAction(
                    action_id=stable_id(
                        "rule-action", design.design_hash, "increase-thickness-bending"
                    ),
                    action_type=ActionType.INCREASE_THICKNESS,
                    description=(
                        "Välj ett tjockare kompatibelt material och generera en ny designrevision."
                    ),
                ),
            )
        )
        return RuleEvaluation(
            rule_id="CB-BENDING-001",
            rule_version=self.version,
            title="Böjspänning i hylla",
            status=status,
            applies_to_part_ids=affected,
            inputs=(
                RuleDatum(name="fri_spännvidd", value=span, unit="µm"),
                RuleDatum(name="hyllbredd_i_böjning", value=depth, unit="µm"),
                RuleDatum(name="uppmätt_tjocklek", value=thickness, unit="µm"),
                RuleDatum(
                    name="säkerhetsfaktor", value=p.structural_safety_factor_permille, unit="‰"
                ),
            ),
            assumptions=(
                "Jämnt fördelad total last och enkelt upplagd hylla.",
                "Tillåten spänning reduceras för materialosäkerhet och säkerhetsfaktor.",
            ),
            trace=(
                CalculationStep(
                    expression="σ_max = 3·W·L/(2·b·t²)",
                    result=str(calculated_kpa),
                    unit="kPa",
                ),
                CalculationStep(
                    expression="σ_allow = f_b·(1-uncertainty)/safety_factor",
                    result=str(allowed_kpa),
                    unit="kPa",
                ),
            ),
            calculated_value=calculated_kpa,
            allowed_value=allowed_kpa,
            unit="kPa",
            safety_margin_permille=_margin_permille(calculated_kpa, allowed_kpa),
            suggested_actions=actions,
        )

    def _lateral_stability(self, design: DesignResult) -> RuleEvaluation:
        p = design.spec.parameters
        slenderness = p.height_um * 1_000 // p.width_um
        has_back = p.back_panel != BackPanelType.NONE
        allowed = 4_000 if has_back else 1_800
        status = _threshold_status(slenderness, allowed, warning_permille=750)
        if not has_back and p.height_um > 1_200_000:
            status = RuleStatus.BLOCK
        back_ids = tuple(part.part_id for part in design.parts if part.role == PartRole.BACK)
        actions: tuple[SuggestedAction, ...] = ()
        if status != RuleStatus.PASS:
            if not has_back:
                actions = (
                    SuggestedAction(
                        action_id=stable_id("rule-action", design.design_hash, "back-panel"),
                        action_type=ActionType.ADD_BACK_PANEL,
                        description=(
                            "Lägg till ett infällt ryggstycke som genererar fogar mot stommen."
                        ),
                        changes=(
                            ParameterChange(
                                path="parameters.back_panel",
                                before=BackPanelType.NONE.value,
                                after=BackPanelType.INSET_GROOVE.value,
                            ),
                        ),
                        requires_user_evidence=design.spec.back_material is None,
                    ),
                )
            else:
                actions = self._divider_actions(design)
        return RuleEvaluation(
            rule_id="CB-STABILITY-001",
            rule_version=self.version,
            title="Sidostabilitet och skevning",
            status=status,
            applies_to_part_ids=back_ids
            or tuple(
                part.part_id
                for part in design.parts
                if part.role in {PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE}
            ),
            inputs=(
                RuleDatum(name="höjd", value=p.height_um, unit="µm"),
                RuleDatum(name="bredd", value=p.width_um, unit="µm"),
                RuleDatum(name="ryggstycke", value=has_back),
            ),
            assumptions=(
                "Regeln är en konservativ slankhetskontroll för MVP-stommen.",
                "Ryggstyckets stabiliserande verkan förutsätter att alla "
                "genererade ryggfogar utförs.",
            ),
            trace=(
                CalculationStep(
                    expression="slankhetsindex = höjd/bredd · 1000",
                    result=str(slenderness),
                    unit="‰",
                ),
            ),
            calculated_value=slenderness,
            allowed_value=allowed,
            unit="‰",
            safety_margin_permille=_margin_permille(slenderness, allowed),
            suggested_actions=actions,
        )

    def _tip_risk(self, design: DesignResult) -> RuleEvaluation:
        p = design.spec.parameters
        mass_g = design.total_weight_g
        if mass_g:
            product_cg_z = Fraction(
                sum(
                    part.weight_g * (2 * part.placement.z_um + part.finished_size.height_um)
                    for part in design.parts
                ),
                2 * mass_g,
            )
        else:
            product_cg_z = Fraction(p.height_um, 2)
        shelf_rows = p.shelf_count
        load_force_n = p.shelf_load_n * shelf_rows
        load_cg_z = Fraction(p.plinth_height_um + p.height_um, 2)
        product_weight_n = Fraction(mass_g * 981, 100_000)
        total_vertical_n = product_weight_n + load_force_n
        combined_cg_z = (
            (product_weight_n * product_cg_z + load_force_n * load_cg_z) / total_vertical_n
            if total_vertical_n
            else Fraction(p.height_um, 2)
        )
        resisting = total_vertical_n * Fraction(p.depth_um, 2)
        overturning = p.assumed_horizontal_force_n * combined_cg_z
        factor_permille = _ceil(resisting * 1_000 / overturning) if overturning else 1_000_000
        geometrically_requires_anchor = p.height_um >= 4 * p.depth_um
        anchor_required = p.wall_anchor.required or geometrically_requires_anchor
        if anchor_required and not p.wall_anchor.verified:
            status = RuleStatus.WARNING
        elif anchor_required and p.wall_anchor.verified:
            status = RuleStatus.PASS
        elif factor_permille < 1_500:
            status = RuleStatus.BLOCK
        elif factor_permille < 2_000:
            status = RuleStatus.WARNING
        else:
            status = RuleStatus.PASS
        actions: tuple[SuggestedAction, ...] = ()
        if status != RuleStatus.PASS and not p.wall_anchor.verified:
            changes = (
                ()
                if p.wall_anchor.required
                else (
                    ParameterChange(
                        path="parameters.wall_anchor.required",
                        before=False,
                        after=True,
                    ),
                )
            )
            actions = (
                SuggestedAction(
                    action_id=stable_id("rule-action", design.design_hash, "verify-wall-anchor"),
                    action_type=ActionType.VERIFY_WALL_ANCHOR,
                    description=(
                        "Välj inte infästning förrän väggunderlag och godkänt "
                        "förankringssystem är kända; "
                        "registrera därefter serverbunden verifiering."
                    ),
                    changes=changes,
                    requires_user_evidence=True,
                ),
            )
        return RuleEvaluation(
            rule_id="CB-TIP-001",
            rule_version=self.version,
            title="Tipprisk och krav på väggförankring",
            status=status,
            applies_to_part_ids=tuple(part.part_id for part in design.parts),
            inputs=(
                RuleDatum(name="produktvikt", value=mass_g, unit="g"),
                RuleDatum(name="horisontalkraft", value=p.assumed_horizontal_force_n, unit="N"),
                RuleDatum(name="kombinerad_tyngdpunkthöjd", value=_ceil(combined_cg_z), unit="µm"),
                RuleDatum(name="väggförankring_krävs", value=anchor_required),
                RuleDatum(name="väggförankring_verifierad", value=p.wall_anchor.verified),
            ),
            assumptions=(
                "Last per hyllrad antas verka vid stommens genomsnittliga lastnivå.",
                "Produktmassan är konservativ bruttomassa för färdigämnen före spår, "
                "hål och annan materialavverkning.",
                "Tippscreening använder angiven horisontalkraft och halv basdjup som hävarm.",
                "Geometri med höjd minst fyra gånger djup kräver verifierad väggförankring.",
            ),
            trace=(
                CalculationStep(
                    expression="SF_tip = stabiliserande moment / vältande moment",
                    result=str(factor_permille),
                    unit="‰",
                ),
            ),
            calculated_value=factor_permille,
            allowed_value=1_500,
            unit="‰ safety factor",
            safety_margin_permille=(
                -1_000
                if anchor_required and not p.wall_anchor.verified
                else 1_000
                if anchor_required and p.wall_anchor.verified
                else (factor_permille - 1_500) * 1_000 // 1_500
            ),
            suggested_actions=actions,
        )

    def auto_correct(
        self,
        spec: BookcaseDesignSpec,
        *,
        max_iterations: int = 8,
    ) -> AutoCorrectionResult:
        if max_iterations < 1 or max_iterations > 32:
            raise ValueError("max_iterations must be between 1 and 32")
        original = build_bookcase(spec)
        initial_report = self.evaluate(original)
        current_spec, current_design, current_report = spec, original, initial_report
        diffs: list[DesignDiff] = []
        if spec.parameters.reinforcement_mode != ReinforcementMode.AUTO:
            return AutoCorrectionResult(
                original_design=original,
                corrected_spec=spec,
                corrected_design=original,
                initial_report=initial_report,
                final_report=initial_report,
                diffs=(),
                resolved=initial_report.overall_status != RuleStatus.BLOCK,
            )

        for _ in range(max_iterations):
            by_id = {evaluation.rule_id: evaluation for evaluation in current_report.evaluations}
            structural_failure = any(
                by_id[rule_id].status == RuleStatus.BLOCK
                for rule_id in ("CB-DEFLECTION-001", "CB-BENDING-001")
            ) or (
                current_spec.parameters.shelf_mount == ShelfMount.FIXED
                and by_id["CB-JOINT-001"].status == RuleStatus.BLOCK
            )
            if structural_failure:
                old = current_spec.parameters.vertical_divider_count
                if old >= MAX_AUTO_VERTICAL_DIVIDERS:
                    break
                candidate_parameters = self._replace_parameters(
                    current_spec.parameters,
                    vertical_divider_count=old + 1,
                    bay_width_ratios_ppm=(),
                )
                candidate_spec = self._replace_spec(current_spec, candidate_parameters)
                candidate_design = build_bookcase(candidate_spec)
                candidate_report = self.evaluate(candidate_design)
                if self._structural_score(candidate_report) >= self._structural_score(
                    current_report
                ):
                    break
                changes = (
                    ParameterChange(
                        path="parameters.vertical_divider_count",
                        before=old,
                        after=old + 1,
                    ),
                )
                diffs.append(
                    DesignDiff(
                        sequence=len(diffs) + 1,
                        action_type=ActionType.ADD_VERTICAL_DIVIDER,
                        reason_rule_id=(
                            "CB-DEFLECTION-001"
                            if by_id["CB-DEFLECTION-001"].status == RuleStatus.BLOCK
                            else "CB-BENDING-001"
                            if by_id["CB-BENDING-001"].status == RuleStatus.BLOCK
                            else "CB-JOINT-001"
                        ),
                        changes=changes,
                        design_hash_before=current_design.design_hash,
                        design_hash_after=candidate_design.design_hash,
                        explanation=(
                            "En vertikal avdelare lades till explicit; hyllor delades per fack och "
                            "assembly, features och vikt räknades om från samma DesignSpec."
                        ),
                    )
                )
                current_spec, current_design, current_report = (
                    candidate_spec,
                    candidate_design,
                    candidate_report,
                )
                continue

            stability = by_id["CB-STABILITY-001"]
            if (
                stability.status == RuleStatus.BLOCK
                and current_spec.parameters.back_panel == BackPanelType.NONE
            ):
                if current_spec.back_material is None:
                    break
                candidate_parameters = self._replace_parameters(
                    current_spec.parameters,
                    back_panel=BackPanelType.INSET_GROOVE,
                )
                candidate_spec = self._replace_spec(current_spec, candidate_parameters)
                candidate_design = build_bookcase(candidate_spec)
                candidate_report = self.evaluate(candidate_design)
                diffs.append(
                    DesignDiff(
                        sequence=len(diffs) + 1,
                        action_type=ActionType.ADD_BACK_PANEL,
                        reason_rule_id=stability.rule_id,
                        changes=(
                            ParameterChange(
                                path="parameters.back_panel",
                                before=BackPanelType.NONE.value,
                                after=BackPanelType.INSET_GROOVE.value,
                            ),
                        ),
                        design_hash_before=current_design.design_hash,
                        design_hash_after=candidate_design.design_hash,
                        explanation=(
                            "Ett infällt ryggstycke och dess matchande stomfogar lades till."
                        ),
                    )
                )
                current_spec, current_design, current_report = (
                    candidate_spec,
                    candidate_design,
                    candidate_report,
                )
                continue

            tip = by_id["CB-TIP-001"]
            if tip.status != RuleStatus.PASS and not current_spec.parameters.wall_anchor.required:
                old_anchor = current_spec.parameters.wall_anchor
                new_anchor = WallAnchorSpec(
                    required=True,
                    wall_substrate=old_anchor.wall_substrate,
                    anchor_system_id=old_anchor.anchor_system_id,
                    evidence_id=old_anchor.evidence_id,
                    verified=False,
                )
                candidate_parameters = self._replace_parameters(
                    current_spec.parameters, wall_anchor=new_anchor
                )
                candidate_spec = self._replace_spec(current_spec, candidate_parameters)
                candidate_design = build_bookcase(candidate_spec)
                candidate_report = self.evaluate(candidate_design)
                diffs.append(
                    DesignDiff(
                        sequence=len(diffs) + 1,
                        action_type=ActionType.VERIFY_WALL_ANCHOR,
                        reason_rule_id=tip.rule_id,
                        changes=(
                            ParameterChange(
                                path="parameters.wall_anchor.required",
                                before=False,
                                after=True,
                            ),
                        ),
                        design_hash_before=current_design.design_hash,
                        design_hash_after=candidate_design.design_hash,
                        explanation=(
                            "Krav på väggförankring registrerades, men ingen "
                            "infästning hittades på. "
                            "Blockeringen kvarstår tills underlag, system och evidens verifierats."
                        ),
                    )
                )
                current_spec, current_design, current_report = (
                    candidate_spec,
                    candidate_design,
                    candidate_report,
                )
            break

        return AutoCorrectionResult(
            original_design=original,
            corrected_spec=current_spec,
            corrected_design=current_design,
            initial_report=initial_report,
            final_report=current_report,
            diffs=tuple(diffs),
            resolved=current_report.overall_status != RuleStatus.BLOCK,
        )

    @staticmethod
    def _replace_parameters(
        parameters: BookcaseParameters, **changes: object
    ) -> BookcaseParameters:
        payload = parameters.model_dump(mode="python")
        payload.update(changes)
        return BookcaseParameters.model_validate(payload)

    @staticmethod
    def _replace_spec(
        spec: BookcaseDesignSpec, parameters: BookcaseParameters
    ) -> BookcaseDesignSpec:
        payload = spec.model_dump(mode="python")
        payload["parameters"] = parameters
        return BookcaseDesignSpec.model_validate(payload)

    @staticmethod
    def _structural_score(report: RuleReport) -> tuple[int, int]:
        rank = {RuleStatus.PASS: 0, RuleStatus.WARNING: 1, RuleStatus.BLOCK: 2}
        selected = tuple(
            evaluation
            for evaluation in report.evaluations
            if evaluation.rule_id in {"CB-DEFLECTION-001", "CB-BENDING-001", "CB-JOINT-001"}
        )
        return (
            sum(rank[item.status] for item in selected),
            sum(max(0, -item.safety_margin_permille) for item in selected),
        )


def evaluate_design(design: DesignResult) -> RuleReport:
    return RuleEngine().evaluate(design)


def auto_correct_design(
    spec: BookcaseDesignSpec,
    *,
    max_iterations: int = 8,
) -> AutoCorrectionResult:
    return RuleEngine().auto_correct(spec, max_iterations=max_iterations)
