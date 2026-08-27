from __future__ import annotations

import unittest

from custombuild_domain import (
    BackPanelType,
    BookcaseDesignSpec,
    BookcaseParameters,
    ReinforcementMode,
    ShelfMount,
    WallAnchorSpec,
    build_bookcase,
    mm,
    screening_birch_plywood_6,
    screening_birch_plywood_18,
)
from custombuild_rules import (
    ActionType,
    RuleEngine,
    RuleStatus,
    auto_correct_design,
    evaluate_design,
)


def verified_anchor() -> WallAnchorSpec:
    return WallAnchorSpec(
        required=True,
        wall_substrate="concrete",
        anchor_system_id="anchor-system-a",
        evidence_id="engineering-review-1",
        verified=True,
    )


def make_spec(design_id: str = "rules-bookcase", **parameter_changes) -> BookcaseDesignSpec:
    payload = BookcaseParameters().model_dump(mode="python")
    payload.update(parameter_changes)
    parameters = BookcaseParameters.model_validate(payload)
    return BookcaseDesignSpec(
        design_id=design_id,
        parameters=parameters,
        material=screening_birch_plywood_18(),
        back_material=(
            None
            if parameters.back_panel == BackPanelType.NONE
            else screening_birch_plywood_6()
        ),
    )


def evaluation(spec: BookcaseDesignSpec, rule_id: str):
    report = evaluate_design(build_bookcase(spec))
    return next(item for item in report.evaluations if item.rule_id == rule_id)


class StructuralRuleTests(unittest.TestCase):
    def test_base_cabinet_validation_names_exact_missing_supports(self) -> None:
        result = evaluation(
            make_spec(
                width_um=mm(4_200),
                height_um=mm(2_600),
                vertical_divider_count=4,
                base_cabinet_count=4,
                base_cabinet_height_um=mm(720),
                base_cabinet_depth_um=mm(320),
            ),
            "CB-SUPPORT-001",
        )

        self.assertEqual(result.status, RuleStatus.BLOCK)
        self.assertGreater(result.calculated_value, 0)
        self.assertIn("ostödda_centrumlinjer", {item.name for item in result.inputs})
        action = result.suggested_actions[0]
        self.assertEqual(action.action_type, ActionType.ALIGN_BASE_CABINETS)
        self.assertEqual(action.changes[0].after, 5)
        self.assertIn("fullhöjd underskåpssida", action.description)

    def test_equal_upper_bays_and_base_modules_form_a_direct_load_path(self) -> None:
        result = evaluation(
            make_spec(
                width_um=mm(4_200),
                height_um=mm(2_600),
                vertical_divider_count=4,
                base_cabinet_count=5,
                base_cabinet_height_um=mm(720),
                base_cabinet_depth_um=mm(320),
            ),
            "CB-SUPPORT-001",
        )

        self.assertEqual(result.status, RuleStatus.PASS)
        self.assertEqual(result.calculated_value, 0)
        self.assertEqual(result.suggested_actions, ())

    def test_sixteen_dividers_recommend_and_accept_seventeen_base_modules(self) -> None:
        unsupported = evaluation(
            make_spec(
                width_um=mm(6000),
                height_um=mm(2600),
                shelf_count=1,
                vertical_divider_count=16,
                base_cabinet_count=16,
                base_cabinet_height_um=mm(720),
                base_cabinet_depth_um=mm(320),
            ),
            "CB-SUPPORT-001",
        )
        supported = evaluation(
            make_spec(
                width_um=mm(6000),
                height_um=mm(2600),
                shelf_count=1,
                vertical_divider_count=16,
                base_cabinet_count=17,
                base_cabinet_height_um=mm(720),
                base_cabinet_depth_um=mm(320),
            ),
            "CB-SUPPORT-001",
        )

        self.assertEqual(unsupported.status, RuleStatus.BLOCK)
        self.assertEqual(unsupported.suggested_actions[0].changes[0].after, 17)
        self.assertEqual(supported.status, RuleStatus.PASS)
        self.assertEqual(supported.calculated_value, 0)

    def test_base_cabinet_hardware_is_a_review_warning_for_validation_only_output(self) -> None:
        result = evaluation(
            make_spec(
                width_um=mm(4_200),
                height_um=mm(2_600),
                vertical_divider_count=4,
                base_cabinet_count=5,
                base_cabinet_height_um=mm(720),
                base_cabinet_depth_um=mm(320),
            ),
            "CB-HARDWARE-001",
        )

        self.assertEqual(result.status, RuleStatus.WARNING)
        self.assertTrue(result.suggested_actions[0].requires_user_evidence)

    def test_default_loaded_shelf_blocks_on_deflection(self) -> None:
        result = evaluation(make_spec(), "CB-DEFLECTION-001")
        self.assertEqual(result.status, RuleStatus.BLOCK)
        self.assertGreater(result.calculated_value, result.allowed_value)
        self.assertTrue(
            any(a.action_type == ActionType.ADD_VERTICAL_DIVIDER for a in result.suggested_actions)
        )

    def test_vertical_divider_reduces_deflection(self) -> None:
        unsupported = evaluation(make_spec(vertical_divider_count=0), "CB-DEFLECTION-001")
        supported = evaluation(make_spec(vertical_divider_count=1), "CB-DEFLECTION-001")
        self.assertLess(supported.calculated_value, unsupported.calculated_value)
        self.assertEqual(supported.status, RuleStatus.PASS)

    def test_wider_shelf_has_greater_deflection(self) -> None:
        narrow = evaluation(make_spec(width_um=mm(650)), "CB-DEFLECTION-001")
        wide = evaluation(make_spec(width_um=mm(1_050)), "CB-DEFLECTION-001")
        self.assertGreater(wide.calculated_value, narrow.calculated_value)

    def test_unequal_bays_assign_load_by_width_and_screen_the_widest_span(self) -> None:
        equal = evaluation(
            make_spec(width_um=mm(2000), vertical_divider_count=2),
            "CB-DEFLECTION-001",
        )
        custom = evaluation(
            make_spec(
                width_um=mm(2000),
                vertical_divider_count=2,
                bay_width_ratios_ppm=(200_000, 600_000, 200_000),
            ),
            "CB-DEFLECTION-001",
        )
        custom_inputs = {item.name: item.value for item in custom.inputs}

        self.assertGreater(custom.calculated_value, equal.calculated_value)
        self.assertGreater(custom_inputs["dimensionerande_facklast"], 100)

    def test_dado_tongues_are_excluded_from_the_free_structural_span(self) -> None:
        spec = make_spec(width_um=mm(500), shelf_count=1)
        design = build_bookcase(spec)
        shelf = next(part for part in design.parts if part.role.value == "shelf")
        result = next(
            item
            for item in evaluate_design(design).evaluations
            if item.rule_id == "CB-DEFLECTION-001"
        )
        inputs = {item.name: item.value for item in result.inputs}

        self.assertEqual(shelf.finished_size.width_um, mm(476))
        self.assertEqual(inputs["fri_spännvidd"], mm(464))

    def test_higher_load_increases_deflection_and_bending(self) -> None:
        light_spec = make_spec(shelf_load_n=100)
        heavy_spec = make_spec(shelf_load_n=600)
        for rule_id in ("CB-DEFLECTION-001", "CB-BENDING-001"):
            light = evaluation(light_spec, rule_id)
            heavy = evaluation(heavy_spec, rule_id)
            self.assertGreater(heavy.calculated_value, light.calculated_value)

    def test_stronger_elastic_modulus_reduces_deflection(self) -> None:
        base_material = screening_birch_plywood_18()
        stronger_payload = base_material.model_dump(mode="python")
        stronger_payload.update(version="stronger-test", elastic_modulus_mpa=10_000)
        stronger_material = type(base_material).model_validate(stronger_payload)
        parameters = BookcaseParameters()
        baseline = BookcaseDesignSpec(
            design_id="modulus-baseline",
            parameters=parameters,
            material=base_material,
            back_material=screening_birch_plywood_6(),
        )
        stronger = BookcaseDesignSpec(
            design_id="modulus-stronger",
            parameters=parameters,
            material=stronger_material,
            back_material=screening_birch_plywood_6(),
        )
        self.assertLess(
            evaluation(stronger, "CB-DEFLECTION-001").calculated_value,
            evaluation(baseline, "CB-DEFLECTION-001").calculated_value,
        )

    def test_zero_shelves_and_zero_load_pass_structural_rules(self) -> None:
        spec = make_spec(shelf_count=0, shelf_load_n=0)
        report = evaluate_design(build_bookcase(spec))
        by_id = {item.rule_id: item for item in report.evaluations}
        self.assertEqual(by_id["CB-DEFLECTION-001"].status, RuleStatus.PASS)
        self.assertEqual(by_id["CB-BENDING-001"].status, RuleStatus.PASS)

    def test_bending_trace_uses_kpa_and_positive_margin(self) -> None:
        result = evaluation(make_spec(shelf_load_n=300), "CB-BENDING-001")
        self.assertEqual(result.unit, "kPa")
        self.assertGreater(result.calculated_value, 1_000)
        self.assertGreater(result.safety_margin_permille, 0)
        self.assertTrue(result.trace)

    def test_material_uncertainty_is_visible_in_inputs_and_assumptions(self) -> None:
        result = evaluation(make_spec(), "CB-DEFLECTION-001")
        inputs = {item.name: item.value for item in result.inputs}
        self.assertIn("materialosäkerhet", inputs)
        self.assertTrue(any("osäkerhet" in item for item in result.assumptions))

    def test_fixed_dado_joint_capacity_uses_actual_engagement_and_versioned_shear(self) -> None:
        result = evaluation(make_spec(), "CB-JOINT-001")
        inputs = {item.name: item.value for item in result.inputs}

        self.assertEqual(result.status, RuleStatus.WARNING)
        self.assertEqual(result.calculated_value, 150)
        self.assertEqual(result.allowed_value, 1_589)
        self.assertEqual(inputs["dado_engagement"], mm(6))
        self.assertEqual(inputs["bärande_längd"], mm(298))
        self.assertEqual(inputs["bärande_area"], mm(6) * mm(298))
        self.assertEqual(inputs["skjuvhållfasthet"], 3)
        self.assertEqual(inputs["materialosäkerhet"], 200)
        self.assertEqual(inputs["strukturell_säkerhetsfaktor"], 1_800)
        self.assertEqual(inputs["materialversion"], "birch-plywood@screening-2026.1")
        self.assertEqual(inputs["materialkälla"], "screening-library@2026.1")
        self.assertIs(inputs["permanent_hållning_verifierad"], False)
        self.assertEqual(len(result.trace), 4)
        self.assertGreater(result.safety_margin_permille, 0)
        self.assertEqual(result.title, "Lokalt upplag i hyllspår och hyllbärare")
        self.assertTrue(any("inte självlåsande" in item for item in result.assumptions))
        self.assertTrue(any("adhesiv låsning är förbjuden" in item for item in result.assumptions))
        dry_action = next(
            action
            for action in result.suggested_actions
            if action.action_type == ActionType.VERIFY_DRY_JOINING_SYSTEM
        )
        self.assertTrue(dry_action.requires_user_evidence)
        self.assertEqual(dry_action.changes, ())
        self.assertIn("endast designgranskning", dry_action.description)

    def test_measured_thickness_changes_real_dado_engagement_and_capacity(self) -> None:
        thin = evaluation(make_spec(actual_thickness_um=mm(17)), "CB-JOINT-001")
        thick = evaluation(make_spec(actual_thickness_um=mm(19)), "CB-JOINT-001")
        thin_inputs = {item.name: item.value for item in thin.inputs}
        thick_inputs = {item.name: item.value for item in thick.inputs}

        self.assertEqual(thin_inputs["dado_engagement"], 5_666)
        self.assertEqual(thick_inputs["dado_engagement"], 6_333)
        self.assertLess(thin.allowed_value, thick.allowed_value)

    def test_joint_capacity_warns_then_blocks_and_proposes_divider_or_load_reduction(self) -> None:
        warning = evaluation(make_spec(shelf_load_n=3_000), "CB-JOINT-001")
        blocking = evaluation(make_spec(shelf_load_n=3_300), "CB-JOINT-001")

        self.assertEqual(warning.status, RuleStatus.WARNING)
        self.assertEqual(blocking.status, RuleStatus.BLOCK)
        self.assertEqual(blocking.calculated_value, 1_650)
        self.assertEqual(blocking.allowed_value, 1_589)
        self.assertEqual(
            {action.action_type for action in blocking.suggested_actions},
            {
                ActionType.ADD_VERTICAL_DIVIDER,
                ActionType.REDUCE_LOAD,
                ActionType.VERIFY_DRY_JOINING_SYSTEM,
            },
        )
        reduce_action = next(
            action
            for action in blocking.suggested_actions
            if action.action_type == ActionType.REDUCE_LOAD
        )
        self.assertEqual(reduce_action.changes[0].path, "parameters.shelf_load_n")
        self.assertLess(reduce_action.changes[0].after, 3_300)

    def test_divider_reduces_worst_joint_reaction_per_bay(self) -> None:
        unsupported = evaluation(
            make_spec(shelf_load_n=3_300, vertical_divider_count=0),
            "CB-JOINT-001",
        )
        supported = evaluation(
            make_spec(shelf_load_n=3_300, vertical_divider_count=1),
            "CB-JOINT-001",
        )

        self.assertEqual(unsupported.status, RuleStatus.BLOCK)
        self.assertEqual(supported.status, RuleStatus.WARNING)
        self.assertEqual(supported.calculated_value, 825)
        self.assertEqual(supported.allowed_value, unsupported.allowed_value)

    def test_adjustable_shelf_pins_block_without_versioned_hardware_capacity(self) -> None:
        result = evaluation(
            make_spec(
                shelf_count=1,
                shelf_load_n=0,
                shelf_mount=ShelfMount.ADJUSTABLE,
            ),
            "CB-JOINT-001",
        )
        inputs = {item.name: item.value for item in result.inputs}

        self.assertEqual(result.status, RuleStatus.BLOCK)
        self.assertEqual(result.allowed_value, 0)
        self.assertEqual(inputs["beslags_sku"], "shelf-pin-5")
        self.assertIsNone(inputs["beslagskapacitetsversion"])
        self.assertEqual(
            result.suggested_actions[0].action_type,
            ActionType.VERIFY_HARDWARE_CAPACITY,
        )
        self.assertTrue(result.suggested_actions[0].requires_user_evidence)
        self.assertTrue(any(step.result == "saknas" for step in result.trace))

    def test_no_adjustable_shelves_need_no_hardware_capacity_claim(self) -> None:
        result = evaluation(
            make_spec(
                shelf_count=0,
                shelf_load_n=0,
                shelf_mount=ShelfMount.ADJUSTABLE,
            ),
            "CB-JOINT-001",
        )
        self.assertEqual(result.status, RuleStatus.WARNING)
        self.assertEqual(result.calculated_value, 0)
        self.assertEqual(result.allowed_value, 0)
        self.assertEqual(result.safety_margin_permille, 0)

    def test_joint_capacity_reductions_follow_uncertainty_and_safety_factor(self) -> None:
        base_material = screening_birch_plywood_18()

        def capacity(*, uncertainty_permille: int, safety_factor_permille: int) -> int:
            material_payload = base_material.model_dump(mode="python")
            material_payload.update(
                version=f"joint-numeric-{uncertainty_permille}-{safety_factor_permille}",
                creep_factor_permille=0,
                property_uncertainty_permille=uncertainty_permille,
            )
            material = type(base_material).model_validate(material_payload)
            spec = BookcaseDesignSpec(
                design_id=f"joint-numeric-{uncertainty_permille}-{safety_factor_permille}",
                parameters=BookcaseParameters(
                    shelf_count=1,
                    structural_safety_factor_permille=safety_factor_permille,
                ),
                material=material,
                back_material=screening_birch_plywood_6(),
            )
            return evaluation(spec, "CB-JOINT-001").allowed_value

        self.assertEqual(capacity(uncertainty_permille=0, safety_factor_permille=1_000), 5_364)
        self.assertEqual(capacity(uncertainty_permille=200, safety_factor_permille=1_000), 4_291)
        self.assertEqual(capacity(uncertainty_permille=0, safety_factor_permille=2_000), 2_682)

    def test_missing_canonical_dado_geometry_blocks_without_inventing_area(self) -> None:
        design = build_bookcase(make_spec(shelf_count=1))
        malformed = design.model_copy(update={"joints": ()})
        result = RuleEngine().evaluate(malformed)
        joint = next(item for item in result.evaluations if item.rule_id == "CB-JOINT-001")

        self.assertEqual(joint.status, RuleStatus.BLOCK)
        self.assertEqual(joint.allowed_value, 0)
        self.assertEqual(
            joint.suggested_actions[0].action_type,
            ActionType.REGENERATE_JOINT_GEOMETRY,
        )
        self.assertTrue(any(step.result == "saknas" for step in joint.trace))


class StabilityAndTipTests(unittest.TestCase):
    def test_tall_case_without_back_panel_blocks_lateral_stability(self) -> None:
        spec = make_spec(
            width_um=mm(650),
            height_um=mm(1_500),
            depth_um=mm(450),
            shelf_count=0,
            shelf_load_n=0,
            back_panel=BackPanelType.NONE,
        )
        result = evaluation(spec, "CB-STABILITY-001")
        self.assertEqual(result.status, RuleStatus.BLOCK)
        self.assertEqual(result.suggested_actions[0].action_type, ActionType.ADD_BACK_PANEL)

    def test_inset_back_improves_stability_limit(self) -> None:
        no_back = evaluation(
            make_spec(width_um=mm(650), height_um=mm(1_500), back_panel=BackPanelType.NONE),
            "CB-STABILITY-001",
        )
        with_back = evaluation(
            make_spec(width_um=mm(650), height_um=mm(1_500), back_panel=BackPanelType.INSET_GROOVE),
            "CB-STABILITY-001",
        )
        self.assertEqual(no_back.status, RuleStatus.BLOCK)
        self.assertNotEqual(with_back.status, RuleStatus.BLOCK)

    def test_tall_shallow_case_requires_verified_anchor(self) -> None:
        result = evaluation(make_spec(wall_anchor=WallAnchorSpec(required=False)), "CB-TIP-001")
        self.assertEqual(result.status, RuleStatus.WARNING)
        self.assertTrue(result.suggested_actions[0].requires_user_evidence)

    def test_verified_anchor_resolves_geometric_anchor_gate(self) -> None:
        result = evaluation(make_spec(wall_anchor=verified_anchor()), "CB-TIP-001")
        self.assertEqual(result.status, RuleStatus.PASS)
        self.assertEqual(result.suggested_actions, ())

    def test_short_deep_case_can_pass_without_anchor(self) -> None:
        spec = make_spec(
            width_um=mm(900),
            height_um=mm(900),
            depth_um=mm(500),
            shelf_count=0,
            shelf_load_n=0,
            wall_anchor=WallAnchorSpec(required=False),
        )
        result = evaluation(spec, "CB-TIP-001")
        self.assertNotEqual(result.status, RuleStatus.BLOCK)

    def test_rule_report_exposes_version_trace_and_disclaimer(self) -> None:
        report = evaluate_design(build_bookcase(make_spec()))
        self.assertEqual(report.rules_version, "1.3.0")
        self.assertIn("inte produktcertifiering", report.disclaimer)
        self.assertTrue(
            all(item.trace and item.inputs and item.assumptions for item in report.evaluations)
        )
        self.assertEqual(report.overall_status, RuleStatus.BLOCK)


class AutoCorrectionTests(unittest.TestCase):
    def test_max_divider_design_suppresses_invalid_action_and_stays_unresolved(self) -> None:
        spec = make_spec(
            width_um=mm(6_000),
            height_um=mm(2_600),
            shelf_count=1,
            shelf_load_n=5_000,
            vertical_divider_count=16,
            max_deflection_um=1,
            reinforcement_mode=ReinforcementMode.AUTO,
            wall_anchor=verified_anchor(),
        )
        initial_report = evaluate_design(build_bookcase(spec))
        initial = next(
            item for item in initial_report.evaluations if item.rule_id == "CB-DEFLECTION-001"
        )
        all_actions = tuple(
            action for item in initial_report.evaluations for action in item.suggested_actions
        )

        self.assertEqual(initial.status, RuleStatus.BLOCK)
        self.assertNotIn(
            ActionType.ADD_VERTICAL_DIVIDER,
            {action.action_type for action in all_actions},
        )
        self.assertTrue(
            all(
                change.after <= 16
                for action in all_actions
                for change in action.changes
                if change.path == "parameters.vertical_divider_count"
            )
        )

        result = auto_correct_design(spec)

        self.assertFalse(result.resolved)
        self.assertEqual(result.corrected_spec, spec)
        self.assertEqual(result.corrected_design, result.original_design)
        self.assertEqual(result.diffs, ())
        self.assertEqual(result.corrected_spec.parameters.vertical_divider_count, 16)

    @staticmethod
    def _joint_limited_spec(*, shelf_mount: ShelfMount = ShelfMount.FIXED) -> BookcaseDesignSpec:
        material = screening_birch_plywood_18()
        payload = material.model_dump(mode="python")
        payload.update(
            version="joint-limited-test",
            elastic_modulus_mpa=1_000_000,
            bending_strength_mpa=1_000,
            shear_strength_mpa=1,
            property_uncertainty_permille=0,
            creep_factor_permille=0,
        )
        return BookcaseDesignSpec(
            design_id=f"joint-autofix-{shelf_mount.value}",
            parameters=BookcaseParameters(
                width_um=mm(900),
                height_um=mm(1_000),
                shelf_count=1,
                shelf_load_n=2_500,
                shelf_mount=shelf_mount,
                reinforcement_mode=ReinforcementMode.AUTO,
                wall_anchor=verified_anchor(),
            ),
            material=type(material).model_validate(payload),
            back_material=screening_birch_plywood_6(),
        )

    def test_manual_mode_never_mutates_design(self) -> None:
        spec = make_spec(reinforcement_mode=ReinforcementMode.MANUAL)
        result = auto_correct_design(spec)
        self.assertEqual(result.diffs, ())
        self.assertEqual(result.original_design, result.corrected_design)
        self.assertEqual(result.corrected_spec, spec)

    def test_auto_mode_adds_divider_and_resolves_overloaded_shelf(self) -> None:
        spec = make_spec(
            reinforcement_mode=ReinforcementMode.AUTO,
            wall_anchor=verified_anchor(),
        )
        result = auto_correct_design(spec)
        self.assertTrue(result.resolved)
        self.assertEqual(result.diffs[0].action_type, ActionType.ADD_VERTICAL_DIVIDER)
        self.assertEqual(result.corrected_spec.parameters.vertical_divider_count, 1)
        self.assertEqual(result.final_report.overall_status, RuleStatus.WARNING)

    def test_auto_divider_updates_parts_joints_and_assembly(self) -> None:
        spec = make_spec(
            reinforcement_mode=ReinforcementMode.AUTO,
            wall_anchor=verified_anchor(),
        )
        result = auto_correct_design(spec)
        self.assertGreater(len(result.corrected_design.parts), len(result.original_design.parts))
        self.assertGreater(len(result.corrected_design.joints), len(result.original_design.joints))
        self.assertGreater(
            len(result.corrected_design.assembly_graph.steps),
            len(result.original_design.assembly_graph.steps),
        )
        self.assertNotEqual(result.original_design.design_hash, result.corrected_design.design_hash)

    def test_auto_mode_adds_divider_when_joint_capacity_is_the_only_structural_block(self) -> None:
        result = auto_correct_design(self._joint_limited_spec())
        initial = {item.rule_id: item for item in result.initial_report.evaluations}

        self.assertEqual(initial["CB-JOINT-001"].status, RuleStatus.BLOCK)
        self.assertNotEqual(initial["CB-DEFLECTION-001"].status, RuleStatus.BLOCK)
        self.assertNotEqual(initial["CB-BENDING-001"].status, RuleStatus.BLOCK)
        self.assertTrue(result.resolved)
        self.assertEqual(result.diffs[0].reason_rule_id, "CB-JOINT-001")
        self.assertEqual(result.diffs[0].action_type, ActionType.ADD_VERTICAL_DIVIDER)
        self.assertEqual(result.corrected_spec.parameters.vertical_divider_count, 1)
        final = {item.rule_id: item for item in result.final_report.evaluations}
        self.assertEqual(final["CB-JOINT-001"].status, RuleStatus.WARNING)

    def test_auto_mode_does_not_add_dividers_to_invent_shelf_pin_evidence(self) -> None:
        spec = make_spec(
            width_um=mm(500),
            height_um=mm(800),
            shelf_count=1,
            shelf_load_n=0,
            shelf_mount=ShelfMount.ADJUSTABLE,
            reinforcement_mode=ReinforcementMode.AUTO,
            wall_anchor=verified_anchor(),
        )
        result = auto_correct_design(spec)

        self.assertEqual(result.diffs, ())
        self.assertFalse(result.resolved)
        joint = next(
            item for item in result.final_report.evaluations if item.rule_id == "CB-JOINT-001"
        )
        self.assertEqual(joint.status, RuleStatus.BLOCK)

    def test_auto_mode_exposes_back_panel_action_but_requires_material_selection(self) -> None:
        spec = make_spec(
            width_um=mm(650),
            height_um=mm(1_500),
            depth_um=mm(450),
            shelf_count=0,
            shelf_load_n=0,
            back_panel=BackPanelType.NONE,
            reinforcement_mode=ReinforcementMode.AUTO,
        )
        report = evaluate_design(build_bookcase(spec))
        back_actions = tuple(
            action
            for evaluation_item in report.evaluations
            for action in evaluation_item.suggested_actions
            if action.action_type == ActionType.ADD_BACK_PANEL
        )
        self.assertTrue(back_actions)
        self.assertTrue(all(action.requires_user_evidence for action in back_actions))

        result = auto_correct_design(spec)
        self.assertFalse(
            any(diff.action_type == ActionType.ADD_BACK_PANEL for diff in result.diffs)
        )
        self.assertEqual(result.corrected_spec.parameters.back_panel, BackPanelType.NONE)

    def test_auto_mode_does_not_invent_missing_back_material(self) -> None:
        parameters = BookcaseParameters(
            width_um=mm(650),
            height_um=mm(1_500),
            depth_um=mm(450),
            shelf_count=0,
            shelf_load_n=0,
            back_panel=BackPanelType.NONE,
            reinforcement_mode=ReinforcementMode.AUTO,
        )
        spec = BookcaseDesignSpec(
            design_id="missing-auto-back-material",
            parameters=parameters,
            material=screening_birch_plywood_18(),
            back_material=None,
        )
        result = auto_correct_design(spec)
        self.assertFalse(
            any(diff.action_type == ActionType.ADD_BACK_PANEL for diff in result.diffs)
        )
        self.assertEqual(result.corrected_spec.parameters.back_panel, BackPanelType.NONE)
        self.assertFalse(result.resolved)

    def test_auto_mode_marks_anchor_requirement_but_does_not_invent_evidence(self) -> None:
        spec = make_spec(
            shelf_count=0,
            shelf_load_n=0,
            reinforcement_mode=ReinforcementMode.AUTO,
            wall_anchor=WallAnchorSpec(required=False),
        )
        result = auto_correct_design(spec)
        anchor_diffs = [
            diff for diff in result.diffs if diff.action_type == ActionType.VERIFY_WALL_ANCHOR
        ]
        self.assertEqual(len(anchor_diffs), 1)
        self.assertTrue(result.corrected_spec.parameters.wall_anchor.required)
        self.assertFalse(result.corrected_spec.parameters.wall_anchor.verified)
        self.assertTrue(result.resolved)
        self.assertEqual(
            evaluation(result.corrected_spec, "CB-TIP-001").status,
            RuleStatus.WARNING,
        )

    def test_auto_correction_is_deterministic(self) -> None:
        spec = make_spec(
            reinforcement_mode=ReinforcementMode.AUTO,
            wall_anchor=verified_anchor(),
        )
        first = auto_correct_design(spec)
        second = auto_correct_design(spec)
        self.assertEqual(first, second)
        self.assertEqual(first.corrected_design.design_hash, second.corrected_design.design_hash)

    def test_invalid_iteration_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RuleEngine().auto_correct(make_spec(), max_iterations=0)
        with self.assertRaises(ValueError):
            RuleEngine().auto_correct(make_spec(), max_iterations=33)


if __name__ == "__main__":
    unittest.main()
