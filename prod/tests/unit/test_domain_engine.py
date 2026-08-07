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
    FeatureKind,
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
        back_material=screening_birch_plywood_6(),
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
                    joint_id
                    for step in result.assembly_graph.steps
                    for joint_id in step.joint_ids
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
                step.moving_part_ids
                and set(step.moving_part_ids) <= set(step.part_ids)
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

    def test_surface_back_is_one_incoming_group_after_carcass_closure(self) -> None:
        result = build_bookcase(make_spec(back_panel=BackPanelType.SURFACE_MOUNTED))
        back = next(part for part in result.parts if part.role == PartRole.BACK)
        back_joint_ids = {
            joint.joint_id
            for joint in result.joints
            if back.part_id in {member.part_id for member in joint.members}
        }
        step = next(
            item
            for item in result.assembly_graph.steps
            if set(item.joint_ids) == back_joint_ids
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
                {by_id[part_id].role for part_id in step.moving_part_ids}
                == {PartRole.SHELF}
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
            part.part_id
            for part in result.parts
            if part.role in {PartRole.DIVIDER, PartRole.SHELF}
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

            self.assertEqual(feature.kind, FeatureKind.GROOVE)
            self.assertEqual(feature.dimensions.depth_um, mm(6))
            self.assertEqual(joint.tolerance_um, mm("0.5"))
            self.assertEqual(feature.tolerance_um, mm("0.05"))
            self.assertEqual(feature.fit_clearance_um, mm("0.5"))
            self.assertEqual(feature.corner_strategy, "dogbone-v1")
            self.assertEqual(feature.dimensions.radius_um, mm(3))

            normal_axis, face_sign = physical_face(owner, feature.face)
            owner_normal = interval(owner, normal_axis)
            mate_normal = interval(mate, normal_axis)
            actual_interlock = (
                max(owner_normal[0], mate_normal[0]),
                min(owner_normal[1], mate_normal[1]),
            )
            expected_interlock = (
                (owner_normal[0], owner_normal[0] + mm(6))
                if face_sign < 0
                else (owner_normal[1] - mm(6), owner_normal[1])
            )
            self.assertEqual(actual_interlock, expected_interlock)

            owner_panel_axes = panel_axes(owner)
            mate_thickness_axis = ({"x", "y", "z"} - set(panel_axes(mate))).pop()
            feature_lengths = (
                feature.dimensions.width_um,
                feature.dimensions.length_um,
            )
            for axis, feature_length in zip(owner_panel_axes, feature_lengths, strict=True):
                self.assertIsNotNone(feature_length)
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
                self.assertTrue(
                    all(
                        feature.dimensions.depth_um == depth_um
                        for part in result.parts
                        for feature in part.features
                        if feature.kind == FeatureKind.GROOVE
                    )
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
