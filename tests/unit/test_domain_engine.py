from __future__ import annotations

import json
import unittest
from decimal import Decimal
from itertools import product

from custombuild_domain import (
    BOOKCASE_JOINT_SUPPORT_MATRIX,
    BackPanelType,
    BookcaseDesignSpec,
    BookcaseParameters,
    DesignResult,
    DesignStatus,
    FaceName,
    FeatureKind,
    GrainDirection,
    JointType,
    PartRole,
    ReinforcementMode,
    ShelfMount,
    WallAnchorSpec,
    build_bookcase,
    canonical_json,
    content_hash,
    mm,
    screening_birch_plywood_6,
    screening_birch_plywood_18,
    screening_mdf_6,
    screening_mdf_18,
    stable_id,
    to_mm,
)
from pydantic import ValidationError


def make_spec(design_id: str = "unit-bookcase", **parameter_changes) -> BookcaseDesignSpec:
    base = BookcaseParameters().model_dump(mode="python")
    base.update(parameter_changes)
    parameters = BookcaseParameters.model_validate(base)
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


class ExactUnitsTests(unittest.TestCase):
    def test_exact_unit_conversion(self) -> None:
        self.assertEqual(mm("18.25"), 18_250)
        self.assertEqual(to_mm(18_250), Decimal("18.25"))

    def test_sub_micrometre_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            mm("0.0001")

    def test_geometry_rejects_floating_point_storage(self) -> None:
        payload = BookcaseParameters().model_dump(mode="python")
        payload["width_um"] = 900_000.0
        with self.assertRaises(ValidationError):
            BookcaseParameters.model_validate(payload)

    def test_stable_id_is_semantic_and_reproducible(self) -> None:
        self.assertEqual(stable_id("part", "d1", "left"), stable_id("part", "d1", "left"))
        self.assertNotEqual(stable_id("part", "d1", "left"), stable_id("part", "d1", "right"))

    def test_canonical_json_ignores_mapping_insertion_order(self) -> None:
        left = {"b": [2, 1], "a": {"x": 1}}
        right = {"a": {"x": 1}, "b": [2, 1]}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(content_hash(left), content_hash(right))


class DomainValidationTests(unittest.TestCase):
    def test_public_design_envelope_rejects_values_outside_static_caps(self) -> None:
        cases = (
            ("width_um", mm(250) - 1),
            ("width_um", mm(6_000) + 1),
            ("height_um", mm(300) - 1),
            ("height_um", mm(4_000) + 1),
            ("depth_um", mm(100) - 1),
            ("depth_um", mm(1_200) + 1),
            ("shelf_count", 41),
            ("shelf_load_n", 5_001),
            ("vertical_divider_count", 17),
            ("base_cabinet_height_um", mm(2_000) + 1),
            ("base_cabinet_depth_um", mm(1_200) + 1),
            ("base_cabinet_count", 18),
        )

        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                payload = BookcaseParameters().model_dump(mode="python")
                payload[field] = value
                BookcaseParameters.model_validate(payload)

    def test_furniture_family_assertion_is_strict_and_non_mutating(self) -> None:
        bookcase = BookcaseParameters()
        bookcase_before = bookcase.model_dump(mode="python")
        self.assertIs(bookcase.assert_furniture_family("bookcase"), bookcase)
        self.assertEqual(bookcase.model_dump(mode="python"), bookcase_before)

        wall_library = BookcaseParameters(
            width_um=mm(2_400),
            height_um=mm(2_400),
            depth_um=mm(340),
            shelf_count=3,
            base_cabinet_height_um=mm(700),
            base_cabinet_depth_um=mm(340),
            base_cabinet_count=3,
        )
        wall_library_before = wall_library.model_dump(mode="python")
        self.assertIs(
            wall_library.assert_furniture_family("wall_library"),
            wall_library,
        )
        self.assertEqual(wall_library.model_dump(mode="python"), wall_library_before)

        with self.assertRaisesRegex(ValueError, "bookcase furniture cannot contain"):
            wall_library.assert_furniture_family("bookcase")
        with self.assertRaisesRegex(ValueError, "requires at least one base cabinet"):
            bookcase.assert_furniture_family("wall_library")
        with self.assertRaisesRegex(ValueError, "unsupported furniture family"):
            bookcase.assert_furniture_family("sideboard")

    def test_base_cabinet_depth_must_match_the_furniture_depth(self) -> None:
        matching = BookcaseParameters(
            depth_um=mm(320),
            base_cabinet_height_um=mm(600),
            base_cabinet_depth_um=mm(320),
            base_cabinet_count=1,
        )
        self.assertEqual(matching.base_cabinet_depth_um, matching.depth_um)

        for mismatched_depth_um in (mm(300), mm(520)):
            with (
                self.subTest(mismatched_depth_um=mismatched_depth_um),
                self.assertRaisesRegex(
                    ValidationError,
                    "base cabinet depth must equal the furniture depth",
                ),
            ):
                BookcaseParameters(
                    depth_um=mm(320),
                    base_cabinet_height_um=mm(600),
                    base_cabinet_depth_um=mismatched_depth_um,
                    base_cabinet_count=1,
                )

    def test_every_base_part_stays_inside_the_furniture_envelope(self) -> None:
        cases = (
            (900, 2000, 280, 0, 1),
            (2400, 2400, 350, 2, 3),
            (4200, 2600, 400, 4, 5),
            (6000, 2600, 550, 16, 17),
        )
        base_roles = {
            PartRole.BASE_SIDE,
            PartRole.BASE_BOTTOM,
            PartRole.CABINET_FRONT,
            PartRole.PLINTH,
        }

        for width_mm, height_mm, depth_mm, divider_count, cabinet_count in cases:
            with self.subTest(
                width_mm=width_mm,
                height_mm=height_mm,
                depth_mm=depth_mm,
                cabinet_count=cabinet_count,
            ):
                result = build_bookcase(
                    make_spec(
                        design_id=(
                            f"bounded-base-{width_mm}-{height_mm}-{depth_mm}-{cabinet_count}"
                        ),
                        width_um=mm(width_mm),
                        height_um=mm(height_mm),
                        depth_um=mm(depth_mm),
                        shelf_count=1,
                        vertical_divider_count=divider_count,
                        base_cabinet_height_um=mm(600),
                        base_cabinet_depth_um=mm(depth_mm),
                        base_cabinet_count=cabinet_count,
                    )
                )
                limits = {
                    "x": result.spec.parameters.width_um,
                    "y": result.spec.parameters.depth_um,
                    "z": result.spec.parameters.height_um,
                }
                base_parts = [part for part in result.parts if part.role in base_roles]
                self.assertTrue(base_parts)
                for part in base_parts:
                    dimensions = {
                        "x": part.finished_size.width_um,
                        "y": part.finished_size.depth_um,
                        "z": part.finished_size.height_um,
                    }
                    for axis, limit in limits.items():
                        start = getattr(part.placement, f"{axis}_um")
                        self.assertGreaterEqual(start, 0, (part.semantic_key, axis))
                        self.assertLessEqual(
                            start + dimensions[axis],
                            limit,
                            (part.semantic_key, axis),
                        )

                base_bottoms = [part for part in result.parts if part.role == PartRole.BASE_BOTTOM]
                self.assertTrue(base_bottoms)
                for base_bottom in base_bottoms:
                    self.assertEqual(
                        base_bottom.placement.y_um,
                        result.spec.parameters.actual_thickness_um,
                    )
                    self.assertEqual(
                        base_bottom.finished_size.depth_um,
                        result.spec.parameters.depth_um
                        - result.spec.parameters.actual_thickness_um,
                    )

                plinths = [part for part in result.parts if part.role == PartRole.PLINTH]
                self.assertEqual(len(plinths), 1)
                self.assertEqual(
                    plinths[0].finished_size.height_um,
                    result.spec.parameters.plinth_height_um,
                )

                joint_pairs = {
                    frozenset(member.part_id for member in joint.members) for joint in result.joints
                }
                unexpected_overlaps: list[tuple[str, str]] = []
                for first_index, first in enumerate(result.parts):
                    for second in result.parts[first_index + 1 :]:
                        overlaps = all(
                            min(
                                getattr(first.placement, f"{axis}_um")
                                + getattr(first.finished_size, dimension),
                                getattr(second.placement, f"{axis}_um")
                                + getattr(second.finished_size, dimension),
                            )
                            > max(
                                getattr(first.placement, f"{axis}_um"),
                                getattr(second.placement, f"{axis}_um"),
                            )
                            for axis, dimension in (
                                ("x", "width_um"),
                                ("y", "depth_um"),
                                ("z", "height_um"),
                            )
                        )
                        if (
                            overlaps
                            and frozenset((first.part_id, second.part_id)) not in joint_pairs
                        ):
                            unexpected_overlaps.append((first.semantic_key, second.semantic_key))
                self.assertEqual(unexpected_overlaps, [])

    def test_wall_library_parts_are_complete_assembly_graph_nodes(self) -> None:
        result = build_bookcase(
            make_spec(
                design_id="wall-library",
                width_um=mm(4200),
                height_um=mm(2600),
                depth_um=mm(320),
                shelf_count=5,
                vertical_divider_count=4,
                base_cabinet_height_um=mm(720),
                base_cabinet_depth_um=mm(320),
                base_cabinet_count=4,
            )
        )

        roles = [part.role for part in result.parts]
        self.assertEqual(roles.count(PartRole.BASE_SIDE), 3)
        self.assertEqual(roles.count(PartRole.BASE_BOTTOM), 4)
        self.assertEqual(roles.count(PartRole.CABINET_FRONT), 4)
        self.assertEqual(roles.count(PartRole.PLINTH), 1)
        self.assertEqual(roles.count(PartRole.SHELF), 25)
        self.assertTrue(
            all(
                part.placement.y_um == 0
                for part in result.parts
                if part.role == PartRole.CABINET_FRONT
            )
        )
        used_part_ids = {
            part_id for step in result.assembly_graph.steps for part_id in step.part_ids
        }
        self.assertEqual(used_part_ids, {part.part_id for part in result.parts})

    def test_custom_upper_bays_drive_the_aligned_base_module_grid(self) -> None:
        result = build_bookcase(
            make_spec(
                design_id="aligned-custom-wall-library",
                width_um=mm(2400),
                height_um=mm(2400),
                depth_um=mm(320),
                vertical_divider_count=2,
                bay_width_ratios_ppm=(250_000, 500_000, 250_000),
                base_cabinet_height_um=mm(720),
                base_cabinet_depth_um=mm(320),
                base_cabinet_count=3,
            )
        )

        dividers = sorted(
            (part for part in result.parts if part.role == PartRole.DIVIDER),
            key=lambda part: part.placement.x_um,
        )
        internal_base_sides = sorted(
            (part for part in result.parts if part.role == PartRole.BASE_SIDE),
            key=lambda part: part.placement.x_um,
        )
        self.assertEqual(
            [part.placement.x_um for part in internal_base_sides],
            [part.placement.x_um for part in dividers],
        )

    def test_base_structure_has_verified_joints_load_paths_and_assembly_steps(self) -> None:
        result = build_bookcase(
            make_spec(
                design_id="jointed-wall-library-base",
                width_um=mm(4200),
                height_um=mm(2600),
                depth_um=mm(320),
                shelf_count=2,
                vertical_divider_count=4,
                base_cabinet_height_um=mm(720),
                base_cabinet_depth_um=mm(320),
                base_cabinet_count=5,
            )
        )

        structural_parts = {
            part.part_id: part
            for part in result.parts
            if part.role in {PartRole.BASE_SIDE, PartRole.BASE_BOTTOM}
        }
        joint_part_ids = {member.part_id for joint in result.joints for member in joint.members}
        step_part_ids = {
            part_id for step in result.assembly_graph.steps for part_id in step.part_ids
        }
        self.assertEqual(
            len([part for part in structural_parts.values() if part.role == PartRole.BASE_SIDE]),
            4,
        )
        self.assertEqual(
            len([part for part in structural_parts.values() if part.role == PartRole.BASE_BOTTOM]),
            5,
        )
        self.assertTrue(set(structural_parts) <= joint_part_ids)
        self.assertTrue(set(structural_parts) <= step_part_ids)

        upper_bottom = next(part for part in result.parts if part.role == PartRole.BOTTOM)
        for base_side in (
            part for part in structural_parts.values() if part.role == PartRole.BASE_SIDE
        ):
            self.assertTrue(
                any(
                    {base_side.part_id, upper_bottom.part_id}
                    == {member.part_id for member in joint.members}
                    and joint.joint_type == JointType.DADO
                    for joint in result.joints
                )
            )
        for base_bottom in (
            part for part in structural_parts.values() if part.role == PartRole.BASE_BOTTOM
        ):
            support_joints = [
                joint
                for joint in result.joints
                if base_bottom.part_id in {member.part_id for member in joint.members}
                and joint.joint_type == JointType.DADO
            ]
            self.assertEqual(len(support_joints), 2)

    def test_sixteen_dividers_support_seventeen_base_modules(self) -> None:
        result = build_bookcase(
            make_spec(
                design_id="seventeen-base-modules",
                width_um=mm(6000),
                height_um=mm(2600),
                depth_um=mm(320),
                shelf_count=1,
                vertical_divider_count=16,
                base_cabinet_height_um=mm(720),
                base_cabinet_depth_um=mm(320),
                base_cabinet_count=17,
            )
        )

        self.assertEqual(
            len([part for part in result.parts if part.role == PartRole.BASE_SIDE]),
            16,
        )
        self.assertEqual(
            len([part for part in result.parts if part.role == PartRole.BASE_BOTTOM]),
            17,
        )
        installed_joint_ids = {
            joint_id for step in result.assembly_graph.steps for joint_id in step.joint_ids
        }
        self.assertEqual(installed_joint_ids, {joint.joint_id for joint in result.joints})

    def test_joint_support_matrix_blocks_unverified_primary_systems(self) -> None:
        self.assertEqual(set(BOOKCASE_JOINT_SUPPORT_MATRIX), set(JointType))
        self.assertEqual(BOOKCASE_JOINT_SUPPORT_MATRIX[JointType.DADO]["status"], "supported")
        self.assertEqual(
            BOOKCASE_JOINT_SUPPORT_MATRIX[JointType.SHELF_PIN]["status"], "conditional"
        )
        for joint in JointType:
            with self.subTest(joint=joint):
                if joint == JointType.DADO:
                    self.assertEqual(BookcaseParameters(joint_system=joint).joint_system, joint)
                else:
                    with self.assertRaises(ValidationError):
                        BookcaseParameters(joint_system=joint)

    def test_supported_assembly_matrix_has_complete_verified_motion_paths(self) -> None:
        cases = product(
            tuple(BackPanelType),
            tuple(ShelfMount),
            (0, 1, 2),
            (0, 2),
            (0, 80_000),
        )
        for index, (back, mount, dividers, shelves, plinth_um) in enumerate(cases):
            with self.subTest(
                back=back,
                mount=mount,
                dividers=dividers,
                shelves=shelves,
                plinth_um=plinth_um,
            ):
                result = build_bookcase(
                    make_spec(
                        design_id=f"assembly-matrix-{index}",
                        back_panel=back,
                        shelf_mount=mount,
                        vertical_divider_count=dividers,
                        shelf_count=shelves,
                        plinth_height_um=plinth_um,
                    )
                )
                installed = [
                    joint_id for step in result.assembly_graph.steps for joint_id in step.joint_ids
                ]
                self.assertCountEqual(installed, [joint.joint_id for joint in result.joints])
                self.assertEqual(len(installed), len(set(installed)))
                for step in result.assembly_graph.steps:
                    self.assertTrue(set(step.moving_part_ids) <= set(step.part_ids))
                    self.assertEqual(step.motion_path[-1], step.direction)

    def test_invalid_outer_geometry_is_rejected(self) -> None:
        payload = BookcaseParameters().model_dump(mode="python")
        payload["width_um"] = mm(30)
        with self.assertRaises(ValidationError):
            BookcaseParameters.model_validate(payload)

    def test_shelves_must_leave_manufacturable_openings(self) -> None:
        payload = BookcaseParameters().model_dump(mode="python")
        payload.update(height_um=mm(600), shelf_count=12, plinth_height_um=0)
        with self.assertRaises(ValidationError):
            BookcaseParameters.model_validate(payload)

    def test_excessive_dividers_are_rejected(self) -> None:
        payload = BookcaseParameters().model_dump(mode="python")
        payload["vertical_divider_count"] = 30
        with self.assertRaises(ValidationError):
            BookcaseParameters.model_validate(payload)

    def test_verified_anchor_requires_complete_evidence(self) -> None:
        with self.assertRaises(ValidationError):
            WallAnchorSpec(required=True, verified=True)
        verified = WallAnchorSpec(
            required=True,
            wall_substrate="concrete",
            anchor_system_id="approved-system",
            evidence_id="calibration-42",
            verified=True,
        )
        self.assertTrue(verified.verified)

    def test_material_nominal_thickness_must_match_design(self) -> None:
        parameters = BookcaseParameters(nominal_thickness_um=mm(19), actual_thickness_um=mm(18))
        with self.assertRaises(ValidationError):
            BookcaseDesignSpec(
                design_id="bad-material",
                parameters=parameters,
                material=screening_birch_plywood_18(),
                back_material=screening_birch_plywood_6(),
            )

    def test_back_panel_requires_explicit_versioned_back_material(self) -> None:
        with self.assertRaises(ValidationError):
            BookcaseDesignSpec(
                design_id="missing-back-material",
                parameters=BookcaseParameters(),
                material=screening_birch_plywood_18(),
            )

    def test_back_material_must_cover_actual_back_thickness(self) -> None:
        with self.assertRaises(ValidationError):
            BookcaseDesignSpec(
                design_id="wrong-back-material",
                parameters=BookcaseParameters(),
                material=screening_birch_plywood_18(),
                back_material=screening_birch_plywood_18(),
            )


class BookcaseEngineTests(unittest.TestCase):
    def test_generated_parts_bind_a_and_b_to_physical_assembly_faces(self) -> None:
        result = build_bookcase(
            make_spec(
                vertical_divider_count=1,
                base_cabinet_height_um=mm(300),
                base_cabinet_depth_um=BookcaseParameters().depth_um,
                base_cabinet_count=2,
            )
        )
        expected = {
            PartRole.LEFT_SIDE: (FaceName.LEFT, FaceName.RIGHT),
            PartRole.RIGHT_SIDE: (FaceName.LEFT, FaceName.RIGHT),
            PartRole.DIVIDER: (FaceName.LEFT, FaceName.RIGHT),
            PartRole.BASE_SIDE: (FaceName.LEFT, FaceName.RIGHT),
            PartRole.TOP: (FaceName.BOTTOM, FaceName.TOP),
            PartRole.BOTTOM: (FaceName.BOTTOM, FaceName.TOP),
            PartRole.SHELF: (FaceName.BOTTOM, FaceName.TOP),
            PartRole.BASE_BOTTOM: (FaceName.BOTTOM, FaceName.TOP),
            PartRole.BACK: (FaceName.FRONT, FaceName.BACK),
            PartRole.PLINTH: (FaceName.FRONT, FaceName.BACK),
            PartRole.CABINET_FRONT: (FaceName.FRONT, FaceName.BACK),
        }

        self.assertTrue(result.parts)
        for part in result.parts:
            with self.subTest(part=part.semantic_key, role=part.role):
                self.assertEqual((part.a_side, part.b_side), expected[part.role])

    def test_catalog_directionality_controls_effective_part_grain(self) -> None:
        mdf_design = build_bookcase(
            BookcaseDesignSpec(
                design_id="catalog-nondirectional-grain",
                parameters=BookcaseParameters(),
                material=screening_mdf_18(),
                back_material=screening_mdf_6(),
            )
        )
        self.assertTrue(mdf_design.parts)
        self.assertEqual(
            {part.grain_direction for part in mdf_design.parts},
            {GrainDirection.NONE},
        )

        directional_design = build_bookcase(make_spec("catalog-directional-grain"))
        back_parts = tuple(part for part in directional_design.parts if part.role == PartRole.BACK)
        self.assertTrue(back_parts)
        self.assertEqual(
            {part.grain_direction for part in back_parts},
            {GrainDirection.Z},
        )

    def test_default_build_has_all_carcass_parts(self) -> None:
        result = build_bookcase(make_spec())
        roles = {part.role for part in result.parts}
        self.assertTrue(
            {
                PartRole.LEFT_SIDE,
                PartRole.RIGHT_SIDE,
                PartRole.TOP,
                PartRole.BOTTOM,
                PartRole.SHELF,
                PartRole.BACK,
                PartRole.PLINTH,
            }
            <= roles
        )

    def test_identical_input_is_byte_deterministic(self) -> None:
        spec = make_spec()
        first = build_bookcase(spec)
        second = build_bookcase(spec)
        self.assertEqual(first.design_hash, second.design_hash)
        self.assertEqual(
            first.model_dump_json(exclude_none=False),
            second.model_dump_json(exclude_none=False),
        )

    def test_dimension_change_changes_hash_but_preserves_semantic_part_ids(self) -> None:
        first = build_bookcase(make_spec(width_um=mm(800)))
        second = build_bookcase(make_spec(width_um=mm(1_000)))
        self.assertNotEqual(first.design_hash, second.design_hash)
        first_ids = {part.semantic_key: part.part_id for part in first.parts}
        second_ids = {part.semantic_key: part.part_id for part in second.parts}
        self.assertEqual(first_ids["left-side"], second_ids["left-side"])
        self.assertEqual(first_ids["shelf-r0-b0"], second_ids["shelf-r0-b0"])

    def test_revision_preserves_part_identity(self) -> None:
        spec = make_spec()
        revised = BookcaseDesignSpec.model_validate(
            {
                **spec.model_dump(mode="python"),
                "revision": 2,
                "status": DesignStatus.APPROVED,
            }
        )
        first = build_bookcase(spec)
        second = build_bookcase(revised)
        self.assertEqual(
            {part.semantic_key: part.part_id for part in first.parts},
            {part.semantic_key: part.part_id for part in second.parts},
        )
        self.assertTrue(all(part.revision == 2 for part in second.parts))
        self.assertEqual(first.design_hash, second.design_hash)
        self.assertEqual(
            tuple(part.model_dump(mode="python", exclude={"revision"}) for part in first.parts),
            tuple(part.model_dump(mode="python", exclude={"revision"}) for part in second.parts),
        )

    def test_vertical_divider_splits_each_shelf_row_into_bays(self) -> None:
        result = build_bookcase(make_spec(vertical_divider_count=1, shelf_count=3))
        shelves = [part for part in result.parts if part.role == PartRole.SHELF]
        dividers = [part for part in result.parts if part.role == PartRole.DIVIDER]
        self.assertEqual(len(shelves), 6)
        self.assertEqual(len(dividers), 1)
        inner_width = (
            result.spec.parameters.width_um - 2 * result.spec.parameters.actual_thickness_um
        )
        first_row = shelves[:2]
        dado_depth = mm(6)
        self.assertEqual(
            sum(part.finished_size.width_um - 2 * dado_depth for part in first_row)
            + result.spec.parameters.actual_thickness_um,
            inner_width,
        )

    def test_shelf_bays_do_not_overlap(self) -> None:
        result = build_bookcase(make_spec(vertical_divider_count=2, shelf_count=1))
        shelves = sorted(
            (part for part in result.parts if part.role == PartRole.SHELF),
            key=lambda item: item.placement.x_um,
        )
        for current, following in zip(shelves, shelves[1:], strict=False):
            self.assertLessEqual(
                current.placement.x_um + current.finished_size.width_um,
                following.placement.x_um,
            )

    def test_shelf_positions_are_inside_carcass_and_strictly_ordered(self) -> None:
        result = build_bookcase(make_spec(shelf_count=7))
        shelves = [part for part in result.parts if part.role == PartRole.SHELF]
        z_values = [part.placement.z_um for part in shelves]
        self.assertEqual(z_values, sorted(set(z_values)))
        self.assertGreater(min(z_values), result.spec.parameters.plinth_height_um)
        self.assertLess(
            max(z_values) + result.spec.parameters.actual_thickness_um,
            result.spec.parameters.height_um,
        )

    def test_custom_bay_and_shelf_ratios_drive_production_geometry(self) -> None:
        result = build_bookcase(
            make_spec(
                width_um=mm(2000),
                height_um=mm(2200),
                vertical_divider_count=2,
                shelf_count=3,
                bay_width_ratios_ppm=(200_000, 600_000, 200_000),
                shelf_height_ratios_ppm=(200_000, 500_000, 800_000),
            )
        )
        shelves = [part for part in result.parts if part.role == PartRole.SHELF]
        first_row = sorted(
            (part for part in shelves if part.instance_index < 3),
            key=lambda part: part.placement.x_um,
        )
        row_levels = sorted({part.placement.z_um for part in shelves})

        self.assertEqual(len(first_row), 3)
        self.assertGreater(first_row[1].finished_size.width_um, first_row[0].finished_size.width_um)
        self.assertEqual(first_row[0].finished_size.width_um, first_row[2].finished_size.width_um)
        self.assertEqual(len(row_levels), 3)
        self.assertGreater(row_levels[1] - row_levels[0], mm(400))

    def test_inset_back_is_segmented_per_bay_with_four_sided_capture(self) -> None:
        result = build_bookcase(
            make_spec(
                design_id="segmented-inset-back",
                width_um=mm(2_400),
                height_um=mm(2_400),
                depth_um=mm(340),
                vertical_divider_count=2,
                shelf_count=1,
                bay_width_ratios_ppm=(250_000, 500_000, 250_000),
            )
        )
        by_id = {part.part_id: part for part in result.parts}
        backs = sorted(
            (part for part in result.parts if part.role == PartRole.BACK),
            key=lambda part: part.instance_index,
        )
        shelves = sorted(
            (part for part in result.parts if part.role == PartRole.SHELF),
            key=lambda part: part.instance_index,
        )
        left_side = next(part for part in result.parts if part.role == PartRole.LEFT_SIDE)
        right_side = next(part for part in result.parts if part.role == PartRole.RIGHT_SIDE)
        dividers = sorted(
            (part for part in result.parts if part.role == PartRole.DIVIDER),
            key=lambda part: part.instance_index,
        )
        supports = (left_side, *dividers, right_side)
        dado_depth_um = mm(6)
        divider_capture_um = mm("5.95")

        self.assertEqual([part.semantic_key for part in backs], ["back-b0", "back-b1", "back-b2"])
        self.assertEqual(
            [part.finished_size.width_um for part in backs],
            [
                shelves[0].finished_size.width_um - mm("0.05"),
                shelves[1].finished_size.width_um - mm("0.1"),
                shelves[2].finished_size.width_um - mm("0.05"),
            ],
        )
        self.assertLess(
            max(part.finished_size.width_um for part in backs),
            result.spec.parameters.width_um - 2 * result.spec.parameters.actual_thickness_um,
        )

        for bay_index, back in enumerate(backs):
            left_capture_um = dado_depth_um if bay_index == 0 else divider_capture_um
            right_capture_um = (
                dado_depth_um if bay_index == len(backs) - 1 else divider_capture_um
            )
            back_joints = [
                joint
                for joint in result.joints
                if back.part_id in {member.part_id for member in joint.members}
            ]
            connected_ids = {
                member.part_id
                for joint in back_joints
                for member in joint.members
                if member.part_id != back.part_id
            }
            self.assertEqual(len(back_joints), 4)
            self.assertEqual({joint.joint_type for joint in back_joints}, {JointType.DADO})
            self.assertEqual(
                connected_ids,
                {
                    supports[bay_index].part_id,
                    supports[bay_index + 1].part_id,
                    next(part for part in result.parts if part.role == PartRole.TOP).part_id,
                    next(part for part in result.parts if part.role == PartRole.BOTTOM).part_id,
                },
            )
            self.assertEqual(
                back.placement.x_um,
                supports[bay_index].placement.x_um
                + supports[bay_index].finished_size.width_um
                - left_capture_um,
            )
            self.assertEqual(
                back.placement.x_um + back.finished_size.width_um,
                supports[bay_index + 1].placement.x_um + right_capture_um,
            )

        self.assertEqual(
            [
                backs[index + 1].placement.x_um
                - (backs[index].placement.x_um + backs[index].finished_size.width_um)
                for index in range(len(backs) - 1)
            ],
            [mm("6.1"), mm("6.1")],
        )

        for divider in dividers:
            back_faces = {
                member.mating_face.value
                for joint in result.joints
                if any(by_id[item.part_id].role == PartRole.BACK for item in joint.members)
                for member in joint.members
                if member.part_id == divider.part_id
            }
            self.assertEqual(back_faces, {"a", "b"})

    def test_assembly_graph_references_every_part_and_joint_once(self) -> None:
        result = build_bookcase(make_spec(vertical_divider_count=1))
        self.assertEqual(
            {part.part_id for part in result.parts},
            {node.part_id for node in result.assembly_graph.nodes},
        )
        step_joint_ids = [
            joint_id for step in result.assembly_graph.steps for joint_id in step.joint_ids
        ]
        self.assertCountEqual(step_joint_ids, [joint.joint_id for joint in result.joints])
        self.assertEqual(len(step_joint_ids), len(set(step_joint_ids)))
        self.assertTrue(
            all(
                step.moving_part_ids and set(step.moving_part_ids) <= set(step.part_ids)
                for step in result.assembly_graph.steps
            )
        )

    def test_captured_panels_are_installed_before_carcass_side_closure(self) -> None:
        result = build_bookcase(make_spec(vertical_divider_count=2, shelf_count=2))
        by_id = {part.part_id: part for part in result.parts}
        step_by_joint = {
            joint_id: step.step_number
            for step in result.assembly_graph.steps
            for joint_id in step.joint_ids
        }
        side_joint_steps = [
            step_by_joint[joint.joint_id]
            for joint in result.joints
            if any(
                by_id[member.part_id].role in {PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE}
                for member in joint.members
            )
        ]
        captured_steps = [
            step_by_joint[joint.joint_id]
            for joint in result.joints
            if all(
                by_id[member.part_id].role not in {PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE}
                for member in joint.members
            )
        ]
        self.assertLess(max(captured_steps), min(side_joint_steps))
        self.assertGreater(
            max(len(step.joint_ids) for step in result.assembly_graph.steps),
            1,
        )
        self.assertEqual(
            result.assembly_graph.steps[-2].direction.value,
            "-x",
        )
        self.assertEqual(result.assembly_graph.steps[-1].direction.value, "+x")

    def test_each_inset_back_field_slides_down_before_top_and_outer_sides_close(self) -> None:
        result = build_bookcase(make_spec(vertical_divider_count=2, shelf_count=2))
        by_id = {part.part_id: part for part in result.parts}
        step_by_joint_id = {
            joint_id: step for step in result.assembly_graph.steps for joint_id in step.joint_ids
        }
        backs = sorted(
            (part for part in result.parts if part.role == PartRole.BACK),
            key=lambda part: part.instance_index,
        )

        self.assertEqual(len(backs), 3)
        for back in backs:
            insertion_step = next(
                step
                for step in result.assembly_graph.steps
                if step.moving_part_ids == (back.part_id,)
            )
            insertion_joints = [
                joint for joint in result.joints if joint.joint_id in insertion_step.joint_ids
            ]
            inserted_against = {
                by_id[member.part_id].role
                for joint in insertion_joints
                for member in joint.members
                if member.part_id != back.part_id
            }
            self.assertEqual(insertion_step.direction.value, "-z")
            self.assertIn(PartRole.BOTTOM, inserted_against)
            self.assertTrue(inserted_against <= {PartRole.BOTTOM, PartRole.DIVIDER})

            closing_joints = [
                joint
                for joint in result.joints
                if back.part_id in {member.part_id for member in joint.members}
                and any(
                    by_id[member.part_id].role
                    in {PartRole.TOP, PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE}
                    for member in joint.members
                )
            ]
            self.assertTrue(closing_joints)
            self.assertTrue(
                all(
                    step_by_joint_id[joint.joint_id].step_number > insertion_step.step_number
                    for joint in closing_joints
                )
            )

    def test_side_explosion_declares_only_the_incoming_gable(self) -> None:
        result = build_bookcase(make_spec())
        by_id = {part.part_id: part for part in result.parts}
        right_side = next(part for part in result.parts if part.role == PartRole.RIGHT_SIDE)
        step = next(
            item
            for item in result.assembly_graph.steps
            if right_side.part_id in item.moving_part_ids
        )

        self.assertEqual(step.direction.value, "-x")
        self.assertEqual(tuple(item.value for item in step.motion_path), ("-x",))
        self.assertEqual(step.moving_part_ids, (right_side.part_id,))
        self.assertIn("panel-positioning-jig", step.tool_ids)
        self.assertTrue(
            any(
                by_id[part_id].role in {PartRole.TOP, PartRole.BOTTOM, PartRole.SHELF}
                for part_id in step.part_ids
            )
        )

        left_side = next(part for part in result.parts if part.role == PartRole.LEFT_SIDE)
        closing_step = next(
            item
            for item in result.assembly_graph.steps
            if left_side.part_id in item.moving_part_ids
        )
        self.assertIn("spåranslutning", closing_step.checkpoint)
        self.assertIn("verifierar inte permanent hållning", closing_step.checkpoint)

    def test_cabinet_front_is_explicitly_not_mountable_without_hardware(self) -> None:
        result = build_bookcase(
            make_spec(
                width_um=mm(2_400),
                height_um=mm(2_400),
                depth_um=mm(340),
                vertical_divider_count=2,
                base_cabinet_height_um=mm(600),
                base_cabinet_depth_um=mm(340),
                base_cabinet_count=3,
            )
        )
        by_id = {part.part_id: part for part in result.parts}
        front_steps = [
            step
            for step in result.assembly_graph.steps
            if any(by_id[part_id].role == PartRole.CABINET_FRONT for part_id in step.part_ids)
        ]

        self.assertEqual(len(front_steps), 3)
        self.assertTrue(all("FRONT EJ MONTERINGSBAR" in step.checkpoint for step in front_steps))

    def test_surface_back_is_one_incoming_group_after_carcass_closure(self) -> None:
        result = build_bookcase(
            make_spec(
                back_panel=BackPanelType.SURFACE_MOUNTED,
                vertical_divider_count=2,
            )
        )
        backs = tuple(part for part in result.parts if part.role == PartRole.BACK)
        self.assertEqual(len(backs), 1)
        back = backs[0]
        back_joint_ids = {
            joint.joint_id
            for joint in result.joints
            if back.part_id in {member.part_id for member in joint.members}
        }
        self.assertEqual(len(back_joint_ids), 4)
        self.assertEqual(
            {joint.joint_type for joint in result.joints if joint.joint_id in back_joint_ids},
            {JointType.RABBET},
        )
        step = next(
            item for item in result.assembly_graph.steps if set(item.joint_ids) == back_joint_ids
        )

        self.assertEqual(step.direction.value, "-y")
        self.assertEqual(step.moving_part_ids, (back.part_id,))
        self.assertEqual(step, result.assembly_graph.steps[-1])

    def test_adjustable_shelves_are_not_part_of_the_divider_module_move(self) -> None:
        result = build_bookcase(
            make_spec(vertical_divider_count=1, shelf_mount=ShelfMount.ADJUSTABLE)
        )
        by_id = {part.part_id: part for part in result.parts}
        module_step = next(
            step
            for step in result.assembly_graph.steps
            if any(
                by_id[member.part_id].role == PartRole.DIVIDER
                and by_id[member.part_id].part_id in step.moving_part_ids
                for joint in result.joints
                if joint.joint_id in step.joint_ids
                for member in joint.members
            )
        )
        self.assertEqual(
            {by_id[part_id].role for part_id in module_step.moving_part_ids},
            {PartRole.DIVIDER},
        )
        shelf_steps = [
            step
            for step in result.assembly_graph.steps
            if any(by_id[part_id].role == PartRole.SHELF for part_id in step.moving_part_ids)
        ]
        self.assertEqual(len(shelf_steps), result.spec.parameters.shelf_count * 2)
        self.assertTrue(
            all(
                {by_id[part_id].role for part_id in step.moving_part_ids} == {PartRole.SHELF}
                and tuple(item.value for item in step.motion_path) == ("+y", "-z")
                for step in shelf_steps
            )
        )

    def test_fixed_divider_module_preserves_all_connected_shelves_as_one_group(self) -> None:
        result = build_bookcase(make_spec(vertical_divider_count=2, shelf_count=2))
        by_id = {part.part_id: part for part in result.parts}
        module_step = next(
            step
            for step in result.assembly_graph.steps
            if step.direction.value == "-z"
            and {by_id[part_id].role for part_id in step.moving_part_ids}
            == {PartRole.DIVIDER, PartRole.SHELF}
        )
        expected = {
            part.part_id for part in result.parts if part.role in {PartRole.DIVIDER, PartRole.SHELF}
        }
        self.assertEqual(set(module_step.moving_part_ids), expected)

    def test_joint_features_are_owned_and_raw_mating_edges_are_explicit(self) -> None:
        result = build_bookcase(make_spec(vertical_divider_count=1))
        features = {
            feature.feature_id: feature for part in result.parts for feature in part.features
        }
        for joint in result.joints:
            self.assertEqual(len(joint.members), 2)
            self.assertTrue(any(member.feature_ids for member in joint.members))
            for member in joint.members:
                if not member.feature_ids:
                    self.assertIn(
                        joint.joint_type,
                        {JointType.DADO, JointType.RABBET, JointType.SHELF_PIN},
                    )
                for feature_id in member.feature_ids:
                    self.assertEqual(features[feature_id].part_id, member.part_id)
                    self.assertEqual(features[feature_id].joint_id, joint.joint_id)

    def test_default_dado_has_one_groove_and_one_raw_mating_edge(self) -> None:
        result = build_bookcase(make_spec(shelf_count=1))
        for joint in (item for item in result.joints if item.joint_type == JointType.DADO):
            feature_counts = tuple(len(member.feature_ids) for member in joint.members)
            self.assertEqual(feature_counts, (1, 0))
            feature_id = joint.members[0].feature_ids[0]
            feature = next(
                item
                for part in result.parts
                for item in part.features
                if item.feature_id == feature_id
            )
            self.assertEqual(feature.kind, FeatureKind.GROOVE)

    def test_back_grooves_are_on_panel_broad_faces(self) -> None:
        result = build_bookcase(make_spec())
        by_id = {part.part_id: part for part in result.parts}
        back_joints = [
            joint
            for joint in result.joints
            if any(by_id[member.part_id].role == PartRole.BACK for member in joint.members)
        ]
        self.assertEqual(len(back_joints), 4)
        for joint in back_joints:
            cut_member = joint.members[0]
            feature = next(
                item
                for item in by_id[cut_member.part_id].features
                if item.feature_id == cut_member.feature_ids[0]
            )
            self.assertIn(feature.face.value, {"a", "b"})
            self.assertEqual(joint.members[1].feature_ids, ())

    def test_plinth_uses_reachable_bottom_broadside_groove(self) -> None:
        result = build_bookcase(make_spec())
        by_id = {part.part_id: part for part in result.parts}
        plinth_joint = next(
            joint
            for joint in result.joints
            if any(by_id[member.part_id].role == PartRole.PLINTH for member in joint.members)
        )
        self.assertEqual(plinth_joint.joint_type, JointType.DADO)
        bottom_member = plinth_joint.members[0]
        feature = next(
            item
            for item in by_id[bottom_member.part_id].features
            if item.feature_id == bottom_member.feature_ids[0]
        )
        self.assertEqual(feature.face.value, "a")
        self.assertEqual(feature.kind, FeatureKind.GROOVE)
        self.assertEqual(plinth_joint.members[1].feature_ids, ())

    def test_non_dado_cut_feature_origins_match_joint_global_reference(self) -> None:
        result = build_bookcase(make_spec(vertical_divider_count=1))
        by_id = {part.part_id: part for part in result.parts}
        for joint in result.joints:
            if joint.joint_type == JointType.DADO:
                continue
            cut_member = joint.members[0]
            part = by_id[cut_member.part_id]
            feature = next(
                item for item in part.features if item.feature_id == cut_member.feature_ids[0]
            )
            if part.role in {PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE, PartRole.DIVIDER}:
                axes = ("y", "z")
            elif part.role in {PartRole.BACK, PartRole.PLINTH}:
                axes = ("x", "z")
            else:
                axes = ("x", "y")
            for axis in axes:
                local = getattr(feature.origin, f"{axis}_um")
                placement = getattr(part.placement, f"{axis}_um")
                global_reference = getattr(joint.mating_origin, f"{axis}_um")
                self.assertEqual(local + placement, global_reference)

    def test_18mm_dado_members_extend_exactly_six_mm_into_their_grooves(self) -> None:
        result = build_bookcase(make_spec(vertical_divider_count=1, shelf_count=1))
        by_key = {part.semantic_key: part for part in result.parts}

        for key in ("bottom", "top"):
            part = by_key[key]
            self.assertEqual(part.placement.x_um, mm(12))
            self.assertEqual(part.finished_size.width_um, mm(876))

        divider = by_key["divider-0"]
        self.assertEqual(divider.placement.z_um, mm(92))
        self.assertEqual(divider.finished_size.height_um, mm(1_896))

        left_shelf = by_key["shelf-r0-b0"]
        right_shelf = by_key["shelf-r0-b1"]
        self.assertEqual(
            (left_shelf.placement.x_um, left_shelf.finished_size.width_um),
            (mm(12), mm(435)),
        )
        self.assertEqual(
            (right_shelf.placement.x_um, right_shelf.finished_size.width_um),
            (mm(453), mm(435)),
        )

        plinth = by_key["plinth"]
        self.assertEqual(plinth.finished_size.height_um, mm(86))

    def test_inset_back_keeps_dogbone_clearance_with_divider_capture_and_joint_depth(self) -> None:
        result = build_bookcase(make_spec(vertical_divider_count=1, shelf_count=1))
        by_key = {part.semantic_key: part for part in result.parts}
        by_id = {part.part_id: part for part in result.parts}
        shelf = by_key["shelf-r0-b0"]
        divider = by_key["divider-0"]
        left_side = by_key["left-side"]
        back = by_key["back-b0"]

        self.assertEqual(shelf.finished_size.depth_um, mm(298))
        self.assertEqual(divider.finished_size.depth_um, mm(308))
        self.assertEqual((shelf.placement.y_um, divider.placement.y_um), (0, 0))

        def groove_for(first_part_id: str, second_part_id: str):
            joint = next(
                item
                for item in result.joints
                if {member.part_id for member in item.members} == {first_part_id, second_part_id}
            )
            cut_member = next(member for member in joint.members if member.feature_ids)
            owner = by_id[cut_member.part_id]
            return next(
                feature
                for feature in owner.features
                if feature.feature_id == cut_member.feature_ids[0]
            )

        shelf_groove = groove_for(left_side.part_id, shelf.part_id)
        back_groove = groove_for(left_side.part_id, back.part_id)
        shelf_end_um = shelf_groove.origin.y_um + (shelf_groove.dimensions.width_um or 0)
        tolerance_adjusted_clearance_um = (
            back_groove.origin.y_um
            - shelf_end_um
            - shelf_groove.tolerance_um
            - back_groove.tolerance_um
        )

        self.assertGreater(tolerance_adjusted_clearance_um, mm(3))
        self.assertEqual(shelf_groove.dimensions.depth_um, mm(6))

    def test_every_dado_groove_contains_the_mate_with_defined_depth_and_fit(self) -> None:
        result = build_bookcase(make_spec(vertical_divider_count=1, shelf_count=1))
        by_id = {part.part_id: part for part in result.parts}
        feature_by_id = {
            feature.feature_id: feature for part in result.parts for feature in part.features
        }
        dado_mate_roles: set[PartRole] = set()

        def dimensions(part) -> dict[str, int]:
            return {
                "x": part.finished_size.width_um,
                "y": part.finished_size.depth_um,
                "z": part.finished_size.height_um,
            }

        def placement(part, axis: str) -> int:
            return getattr(part.placement, f"{axis}_um")

        def interval(part, axis: str) -> tuple[int, int]:
            start = placement(part, axis)
            return start, start + dimensions(part)[axis]

        def panel_axes(part) -> tuple[str, str]:
            if part.role in {PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE, PartRole.DIVIDER}:
                return ("y", "z")
            if part.role in {PartRole.BACK, PartRole.PLINTH}:
                return ("x", "z")
            return ("x", "y")

        def physical_face(part, face) -> tuple[str, int]:
            if face.value == "a":
                if part.role in {PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE, PartRole.DIVIDER}:
                    face = type(face).LEFT
                elif part.role in {PartRole.TOP, PartRole.BOTTOM, PartRole.SHELF}:
                    face = type(face).BOTTOM
                else:
                    face = type(face).FRONT
            elif face.value == "b":
                if part.role in {PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE, PartRole.DIVIDER}:
                    face = type(face).RIGHT
                elif part.role in {PartRole.TOP, PartRole.BOTTOM, PartRole.SHELF}:
                    face = type(face).TOP
                else:
                    face = type(face).BACK
            return {
                "left": ("x", -1),
                "right": ("x", 1),
                "front": ("y", -1),
                "back": ("y", 1),
                "bottom": ("z", -1),
                "top": ("z", 1),
            }[face.value]

        for joint in result.joints:
            if joint.joint_type != JointType.DADO:
                continue
            cut_member, mate_member = joint.members
            owner = by_id[cut_member.part_id]
            mate = by_id[mate_member.part_id]
            feature = feature_by_id[cut_member.feature_ids[0]]
            dado_mate_roles.add(mate.role)
            expected_depth = (
                mm("5.95")
                if owner.role == PartRole.DIVIDER and mate.role == PartRole.BACK
                else mm(6)
            )

            self.assertEqual(feature.kind, FeatureKind.GROOVE)
            self.assertEqual(feature.dimensions.depth_um, expected_depth)
            self.assertEqual(joint.tolerance_um, mm("0.5"))
            self.assertEqual(feature.tolerance_um, mm("0.05"))
            self.assertEqual(feature.fit_clearance_um, mm("0.5"))
            self.assertEqual(feature.corner_strategy, "dogbone-v2")
            self.assertEqual(feature.dimensions.radius_um, mm(3))

            normal_axis, face_sign = physical_face(owner, feature.face)
            owner_normal = interval(owner, normal_axis)
            mate_normal = interval(mate, normal_axis)
            actual_interlock = (
                max(owner_normal[0], mate_normal[0]),
                min(owner_normal[1], mate_normal[1]),
            )
            expected_interlock = (
                (owner_normal[0], owner_normal[0] + expected_depth)
                if face_sign < 0
                else (owner_normal[1] - expected_depth, owner_normal[1])
            )
            self.assertEqual(actual_interlock, expected_interlock)

            owner_panel_axes = panel_axes(owner)
            mate_thickness_axis = ({"x", "y", "z"} - set(panel_axes(mate))).pop()
            feature_lengths = (
                feature.dimensions.width_um,
                feature.dimensions.length_um,
            )
            expected_open_ends: list[str] = []
            open_end_labels = (("u_min", "u_max"), ("v_min", "v_max"))
            for index, (axis, feature_length) in enumerate(
                zip(owner_panel_axes, feature_lengths, strict=True)
            ):
                self.assertIsNotNone(feature_length)
                local_start = getattr(feature.origin, f"{axis}_um")
                if local_start == 0:
                    expected_open_ends.append(open_end_labels[index][0])
                if local_start + int(feature_length) == dimensions(owner)[axis]:
                    expected_open_ends.append(open_end_labels[index][1])
                groove_start = placement(owner, axis) + getattr(feature.origin, f"{axis}_um")
                groove_interval = (groove_start, groove_start + int(feature_length))
                mate_interval = interval(mate, axis)
                if axis == mate_thickness_axis:
                    self.assertLessEqual(groove_interval[0], mate_interval[0])
                    self.assertGreaterEqual(groove_interval[1], mate_interval[1])
                    self.assertEqual(
                        groove_interval[1]
                        - groove_interval[0]
                        - (mate_interval[1] - mate_interval[0]),
                        mm("0.5"),
                    )
                else:
                    self.assertEqual(
                        groove_interval,
                        (
                            max(interval(owner, axis)[0], mate_interval[0]),
                            min(interval(owner, axis)[1], mate_interval[1]),
                        ),
                    )
            self.assertEqual(
                tuple(item.value for item in feature.open_end_reliefs),
                tuple(expected_open_ends),
            )

        self.assertTrue(
            {
                PartRole.BOTTOM,
                PartRole.TOP,
                PartRole.SHELF,
                PartRole.DIVIDER,
                PartRole.BACK,
                PartRole.PLINTH,
            }
            <= dado_mate_roles
        )

    def test_dado_depth_tracks_measured_material_thickness_deterministically(self) -> None:
        for thickness_um, depth_um in ((mm(17), 5_666), (mm(18), 6_000), (mm(19), 6_333)):
            with self.subTest(thickness_um=thickness_um):
                result = build_bookcase(
                    make_spec(
                        actual_thickness_um=thickness_um,
                        shelf_count=1,
                        vertical_divider_count=1,
                    )
                )
                by_key = {part.semantic_key: part for part in result.parts}
                bottom = by_key["bottom"]
                divider = by_key["divider-0"]

                self.assertEqual(bottom.placement.x_um, thickness_um - depth_um)
                self.assertEqual(
                    bottom.finished_size.width_um,
                    result.spec.parameters.width_um - 2 * thickness_um + 2 * depth_um,
                )
                self.assertEqual(
                    divider.placement.z_um,
                    result.spec.parameters.plinth_height_um + thickness_um - depth_um,
                )
                by_id = {part.part_id: part for part in result.parts}
                feature_by_id = {
                    feature.feature_id: feature
                    for part in result.parts
                    for feature in part.features
                }
                divider_capture = min(depth_um, (thickness_um - 6_100) // 2)
                for joint in result.joints:
                    if joint.joint_type != JointType.DADO:
                        continue
                    cut_member, mate_member = joint.members
                    owner = by_id[cut_member.part_id]
                    mate = by_id[mate_member.part_id]
                    expected_depth = (
                        divider_capture
                        if owner.role == PartRole.DIVIDER and mate.role == PartRole.BACK
                        else depth_um
                    )
                    self.assertEqual(
                        feature_by_id[cut_member.feature_ids[0]].dimensions.depth_um,
                        expected_depth,
                    )

    def test_all_default_features_are_inside_local_panel_bounds(self) -> None:
        result = build_bookcase(make_spec(vertical_divider_count=2))
        for part in result.parts:
            if part.role in {PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE, PartRole.DIVIDER}:
                axes = ("y", "z")
            elif part.role in {PartRole.BACK, PartRole.PLINTH}:
                axes = ("x", "z")
            else:
                axes = ("x", "y")
            panel_dimensions = {
                "x": part.finished_size.width_um,
                "y": part.finished_size.depth_um,
                "z": part.finished_size.height_um,
            }
            for feature in part.features:
                u_origin = getattr(feature.origin, f"{axes[0]}_um")
                v_origin = getattr(feature.origin, f"{axes[1]}_um")
                width = feature.dimensions.width_um or feature.dimensions.diameter_um or 0
                length = feature.dimensions.length_um or width
                self.assertGreaterEqual(u_origin, 0)
                self.assertGreaterEqual(v_origin, 0)
                self.assertLessEqual(u_origin + width, panel_dimensions[axes[0]])
                self.assertLessEqual(v_origin + length, panel_dimensions[axes[1]])

    def test_adjustable_shelves_generate_pin_patterns(self) -> None:
        result = build_bookcase(make_spec(shelf_mount=ShelfMount.ADJUSTABLE, shelf_count=2))
        shelf_pin_joints = [
            joint for joint in result.joints if joint.joint_type == JointType.SHELF_PIN
        ]
        self.assertEqual(len(shelf_pin_joints), 4)
        self.assertTrue(
            any(
                feature.kind == FeatureKind.SERIES_DRILL and feature.pattern_count == 2
                for part in result.parts
                for feature in part.features
            )
        )

    def test_no_back_panel_produces_no_back_part_or_joint(self) -> None:
        result = build_bookcase(make_spec(back_panel=BackPanelType.NONE))
        self.assertFalse(any(part.role == PartRole.BACK for part in result.parts))
        self.assertFalse(any("back" in part.semantic_key for part in result.parts))
        part_keys = {part.part_id: part.semantic_key for part in result.parts}
        self.assertFalse(
            any(
                any(part_keys[member.part_id] == "back" for member in joint.members)
                for joint in result.joints
            )
        )

    def test_surface_back_stays_inside_requested_outer_depth(self) -> None:
        result = build_bookcase(make_spec(back_panel=BackPanelType.SURFACE_MOUNTED))
        back = next(part for part in result.parts if part.role == PartRole.BACK)
        self.assertEqual(
            back.placement.y_um + back.finished_size.depth_um,
            result.spec.parameters.depth_um,
        )

    def test_back_part_uses_its_own_material_version_and_density(self) -> None:
        result = build_bookcase(make_spec())
        back = next(part for part in result.parts if part.role == PartRole.BACK)
        back_material = screening_birch_plywood_6()
        self.assertEqual(back.material_id, back_material.material_id)
        self.assertEqual(back.material_version, back_material.version)
        expected_weight = (
            back.finished_size.width_um
            * back.finished_size.depth_um
            * back.finished_size.height_um
            * back_material.density_kg_m3
            * 1_000
            + 1_000_000**3
            - 1
        ) // (1_000_000**3)
        self.assertEqual(back.weight_g, expected_weight)

    def test_inset_back_joint_faces_match_the_actual_boundary_and_back_edges(self) -> None:
        result = build_bookcase(make_spec())
        by_id = {part.part_id: part for part in result.parts}
        expected = {
            PartRole.LEFT_SIDE: ("b", "left"),
            PartRole.RIGHT_SIDE: ("a", "right"),
            PartRole.TOP: ("a", "top"),
            PartRole.BOTTOM: ("b", "bottom"),
        }
        for joint in result.joints:
            members = tuple(by_id[member.part_id] for member in joint.members)
            if not any(part.role == PartRole.BACK for part in members):
                continue
            boundary_index = 0 if members[1].role == PartRole.BACK else 1
            boundary = members[boundary_index]
            back_index = 1 - boundary_index
            self.assertEqual(
                (
                    joint.members[boundary_index].mating_face.value,
                    joint.members[back_index].mating_face.value,
                ),
                expected[boundary.role],
            )

    def test_part_weights_sum_exactly_to_result_weight(self) -> None:
        result = build_bookcase(make_spec())
        self.assertEqual(result.total_weight_g, sum(part.weight_g for part in result.parts))
        self.assertGreater(result.total_weight_g, 0)

    def test_raw_dimensions_include_catalogue_machining_allowance(self) -> None:
        material_payload = screening_birch_plywood_18().model_dump(mode="python")
        material_payload["machining_allowance_um"] = mm(1)
        material = type(screening_birch_plywood_18()).model_validate(material_payload)
        spec = BookcaseDesignSpec(
            design_id="allowance",
            parameters=BookcaseParameters(),
            material=material,
            back_material=screening_birch_plywood_6(),
        )
        side = next(part for part in build_bookcase(spec).parts if part.role == PartRole.LEFT_SIDE)
        self.assertEqual(side.raw_size.width_um, side.finished_size.width_um)
        self.assertEqual(side.raw_size.depth_um, side.finished_size.depth_um + mm(2))
        self.assertEqual(side.raw_size.height_um, side.finished_size.height_um + mm(2))

    def test_result_round_trips_through_json_without_loss(self) -> None:
        result = build_bookcase(make_spec(vertical_divider_count=1))
        restored = DesignResult.model_validate(json.loads(result.model_dump_json()))
        self.assertEqual(restored, result)
        self.assertEqual(restored.design_hash, result.design_hash)

    def test_reinforcement_mode_is_part_of_design_hash(self) -> None:
        manual = build_bookcase(make_spec(reinforcement_mode=ReinforcementMode.MANUAL))
        automatic = build_bookcase(make_spec(reinforcement_mode=ReinforcementMode.AUTO))
        self.assertNotEqual(manual.design_hash, automatic.design_hash)


if __name__ == "__main__":
    unittest.main()
