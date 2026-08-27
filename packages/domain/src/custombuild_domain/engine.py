from __future__ import annotations

from collections import defaultdict

from .enums import (
    AssemblyDirection,
    BackPanelType,
    FaceName,
    FeatureKind,
    GrainDirection,
    JointType,
    OpenEndRelief,
    PartRole,
    ShelfMount,
)
from .identity import content_hash, stable_id
from .models import (
    AssemblyEdge,
    AssemblyGraph,
    AssemblyNode,
    AssemblyStep,
    BookcaseDesignSpec,
    DesignResult,
    Dimensions3D,
    EdgeBand,
    FeatureDimensions,
    Joint,
    JointMember,
    ManufacturingFeature,
    PartInstance,
    Placement,
    Point3D,
)
from .units import mm

DADO_FIT_CLEARANCE_UM = mm("0.5")
DADO_FEATURE_TOLERANCE_UM = mm("0.05")
DADO_MAX_DEPTH_UM = mm(12)
DADO_CORNER_RELIEF_RADIUS_UM = mm(3)
# The inset back starts 12 mm from the rear edge. Four millimetres between
# shelf/divider ends and that line keeps the 3 mm dogbone envelope separated
# even after the versioned 0.05 mm machining tolerance is applied.
INSET_BACK_RELIEF_CLEARANCE_UM = mm(4)


class BookcaseEngine:
    """Pure deterministic compiler from a BookcaseDesignSpec to product model."""

    def build(self, spec: BookcaseDesignSpec) -> DesignResult:
        parts = self._build_parts(spec)
        parts, joints = self._build_joints(spec, parts)
        graph = self._build_assembly_graph(parts, joints)
        total_weight = sum(part.weight_g for part in parts)
        hash_payload = {
            "spec": spec.model_dump(
                mode="json",
                exclude={"revision", "status"},
                exclude_none=False,
            ),
            "parts": tuple(part.model_dump(mode="python", exclude={"revision"}) for part in parts),
            "joints": joints,
            "assembly_graph": graph,
        }
        return DesignResult(
            design_hash=content_hash(hash_payload),
            engine_version=spec.engine_version,
            template_version=spec.template_version,
            spec=spec,
            parts=parts,
            joints=joints,
            assembly_graph=graph,
            total_weight_g=total_weight,
        )

    def _build_parts(self, spec: BookcaseDesignSpec) -> tuple[PartInstance, ...]:
        p = spec.parameters
        t = p.actual_thickness_um
        case_depth = p.depth_um - (
            p.back_thickness_um if p.back_panel == BackPanelType.SURFACE_MOUNTED else 0
        )
        inner_width = p.width_um - 2 * t
        shelf_zone_bottom = (
            p.plinth_height_um + p.base_cabinet_height_um
            if p.base_cabinet_count
            else p.plinth_height_um + t
        )
        bottom_z = shelf_zone_bottom - t
        inner_height = p.height_um - shelf_zone_bottom - t
        dado_depth = self._dado_depth(t)
        carcass_side_inset = dado_depth if p.joint_system == JointType.DADO else 0
        fixed_shelf_inset = (
            dado_depth
            if p.shelf_mount == ShelfMount.FIXED and p.joint_system == JointType.DADO
            else 0
        )
        shelf_depth = (
            p.depth_um - mm(12) - p.back_thickness_um - INSET_BACK_RELIEF_CLEARANCE_UM
            if p.back_panel == BackPanelType.INSET_GROOVE
            else case_depth - mm(2)
        )
        parts: list[PartInstance] = []

        def add(
            key: str,
            role: PartRole,
            index: int,
            size: Dimensions3D,
            placement: Placement,
            *,
            grain: GrainDirection,
            visible: tuple[FaceName, ...] = (),
            band_front: bool = False,
        ) -> None:
            part_id = stable_id("part", spec.design_id, key)
            part_material = (
                spec.back_material
                if role == PartRole.BACK and spec.back_material is not None
                else spec.material
            )
            # Part orientation is a design intent, while material directionality
            # is a versioned catalogue property. A catalogue-declared
            # non-directional sheet must not inherit an artificial X/Y panel
            # constraint merely from its placement in the furniture assembly.
            # Conversely, directional materials retain the role-specific axis
            # so manufacturing can bind it to an exact stock-sheet axis.
            effective_grain = (
                GrainDirection.NONE
                if part_material.grain_direction == GrainDirection.NONE
                else grain
            )
            allowance = part_material.machining_allowance_um
            part_thickness = p.back_thickness_um if role == PartRole.BACK else t
            raw = self._raw_size(size, part_thickness, allowance)
            bands = (
                (EdgeBand(edge=FaceName.FRONT, thickness_um=p.edge_band_thickness_um),)
                if band_front and p.edge_band_thickness_um
                else ()
            )
            parts.append(
                PartInstance(
                    part_id=part_id,
                    semantic_key=key,
                    role=role,
                    instance_index=index,
                    revision=spec.revision,
                    finished_size=size,
                    raw_size=raw,
                    placement=placement,
                    material_id=part_material.material_id,
                    material_version=part_material.version,
                    actual_thickness_um=t if role != PartRole.BACK else p.back_thickness_um,
                    grain_direction=effective_grain,
                    visible_faces=visible,
                    edge_bands=bands,
                    weight_g=self._weight_g(size, part_material.density_kg_m3),
                )
            )

        add(
            "left-side",
            PartRole.LEFT_SIDE,
            0,
            Dimensions3D(width_um=t, depth_um=case_depth, height_um=p.height_um),
            Placement(),
            grain=GrainDirection.Z,
            visible=(FaceName.LEFT, FaceName.FRONT),
            band_front=True,
        )
        add(
            "right-side",
            PartRole.RIGHT_SIDE,
            0,
            Dimensions3D(width_um=t, depth_um=case_depth, height_um=p.height_um),
            Placement(x_um=p.width_um - t),
            grain=GrainDirection.Z,
            visible=(FaceName.RIGHT, FaceName.FRONT),
            band_front=True,
        )
        add(
            "bottom",
            PartRole.BOTTOM,
            0,
            Dimensions3D(
                width_um=inner_width + 2 * carcass_side_inset,
                depth_um=case_depth,
                height_um=t,
            ),
            Placement(x_um=t - carcass_side_inset, z_um=bottom_z),
            grain=GrainDirection.X,
            visible=(FaceName.FRONT,),
            band_front=True,
        )
        add(
            "top",
            PartRole.TOP,
            0,
            Dimensions3D(
                width_um=inner_width + 2 * carcass_side_inset,
                depth_um=case_depth,
                height_um=t,
            ),
            Placement(x_um=t - carcass_side_inset, z_um=p.height_um - t),
            grain=GrainDirection.X,
            visible=(FaceName.TOP, FaceName.FRONT),
            band_front=True,
        )

        bay_widths = self._bay_widths(
            inner_width,
            t,
            p.vertical_divider_count,
            p.bay_width_ratios_ppm,
        )
        bay_starts: list[int] = []
        cursor = t
        for bay_index, width in enumerate(bay_widths):
            bay_starts.append(cursor)
            cursor += width
            if bay_index < p.vertical_divider_count:
                divider_index = bay_index
                add(
                    f"divider-{divider_index}",
                    PartRole.DIVIDER,
                    divider_index,
                    Dimensions3D(
                        width_um=t,
                        depth_um=shelf_depth,
                        height_um=inner_height + 2 * dado_depth,
                    ),
                    Placement(x_um=cursor, z_um=shelf_zone_bottom - dado_depth),
                    grain=GrainDirection.Z,
                    visible=(FaceName.FRONT,),
                    band_front=True,
                )
                cursor += t

        shelf_z_values = self._shelf_positions(
            shelf_zone_bottom,
            inner_height,
            t,
            p.shelf_count,
            p.shelf_height_ratios_ppm,
        )
        shelf_instance_index = 0
        for row, z_um in enumerate(shelf_z_values):
            for bay, (bay_start, bay_width) in enumerate(zip(bay_starts, bay_widths, strict=True)):
                clearance = (
                    p.shelf_side_clearance_um if p.shelf_mount == ShelfMount.ADJUSTABLE else 0
                )
                add(
                    f"shelf-r{row}-b{bay}",
                    PartRole.SHELF,
                    shelf_instance_index,
                    Dimensions3D(
                        width_um=bay_width - 2 * clearance + 2 * fixed_shelf_inset,
                        depth_um=shelf_depth,
                        height_um=t,
                    ),
                    Placement(
                        x_um=bay_start + clearance - fixed_shelf_inset,
                        z_um=z_um,
                    ),
                    grain=GrainDirection.X,
                    visible=(FaceName.A, FaceName.FRONT),
                    band_front=True,
                )
                shelf_instance_index += 1

        if p.base_cabinet_count:
            # When the lower modules and upper bays have the same count, they
            # share one structural grid. This preserves custom bay ratios and
            # puts every upper divider directly on a full-height base side.
            base_openings = (
                bay_widths
                if p.base_cabinet_count == len(bay_widths)
                else self._bay_widths(
                    p.width_um - 2 * t,
                    t,
                    p.base_cabinet_count - 1,
                )
            )
            # The full-height carcass sides are the two outer supports.  Only
            # internal cabinet boundaries need separate BASE_SIDE parts; the
            # previous N+1 layout duplicated both outer carcass sides as
            # coincident solids and provided no physical load-path joints.
            base_boundary_positions = [0]
            cursor = 0
            for opening_width in base_openings:
                cursor += t + opening_width
                base_boundary_positions.append(cursor)

            base_side_height = p.base_cabinet_height_um - t + dado_depth
            for side_index, x_um in enumerate(base_boundary_positions[1:-1], start=1):
                add(
                    f"base-side-{side_index}",
                    PartRole.BASE_SIDE,
                    side_index,
                    Dimensions3D(
                        width_um=t,
                        depth_um=p.base_cabinet_depth_um,
                        # The top edge enters a verified dado in the upper
                        # bottom.  That makes each internal side a direct
                        # vertical load path instead of a merely coincident
                        # visual panel.
                        height_um=base_side_height,
                    ),
                    Placement(x_um=x_um, z_um=p.plinth_height_um),
                    grain=GrainDirection.Z,
                    visible=(FaceName.FRONT,),
                    band_front=True,
                )

            front_gap = mm(2)
            front_z = p.plinth_height_um + t + front_gap
            front_height = p.base_cabinet_height_um - 2 * t - 2 * front_gap
            for cabinet_index, opening_width in enumerate(base_openings):
                opening_x = base_boundary_positions[cabinet_index] + t
                add(
                    f"base-bottom-{cabinet_index}",
                    PartRole.BASE_BOTTOM,
                    cabinet_index,
                    Dimensions3D(
                        # Each end enters the adjacent full-height or internal
                        # support by the canonical dado depth.
                        width_um=opening_width + 2 * dado_depth,
                        # The lower bottom starts behind the front plane. Its
                        # requested depth therefore includes the inset-front
                        # thickness without creating a coincident solid.
                        depth_um=p.base_cabinet_depth_um - t,
                        height_um=t,
                    ),
                    Placement(
                        x_um=opening_x - dado_depth,
                        y_um=t,
                        z_um=p.plinth_height_um,
                    ),
                    grain=GrainDirection.X,
                    visible=(FaceName.A, FaceName.FRONT),
                    band_front=True,
                )
                add(
                    f"cabinet-front-{cabinet_index}",
                    PartRole.CABINET_FRONT,
                    cabinet_index,
                    Dimensions3D(
                        width_um=opening_width - 2 * front_gap,
                        depth_um=t,
                        # The front is an inset panel in the clear opening. It
                        # must not occupy the base-bottom or upper-bottom
                        # solids that frame that opening.
                        height_um=front_height,
                    ),
                    Placement(
                        x_um=opening_x + front_gap,
                        z_um=front_z,
                    ),
                    grain=GrainDirection.Z,
                    visible=(FaceName.FRONT,),
                    # FRONT is the panel face (its thickness normal), not one
                    # of the four bandable panel boundaries.  A supplier-
                    # backed perimeter-band specification is still required
                    # before physical manufacture and must not be invented by
                    # the geometry generator.
                    band_front=False,
                )

        if p.back_panel == BackPanelType.SURFACE_MOUNTED:
            add(
                "back",
                PartRole.BACK,
                0,
                Dimensions3D(
                    width_um=p.width_um, depth_um=p.back_thickness_um, height_um=p.height_um
                ),
                Placement(y_um=p.depth_um - p.back_thickness_um),
                grain=GrainDirection.Z,
                visible=(FaceName.BACK,),
            )
        elif p.back_panel == BackPanelType.INSET_GROOVE:
            add(
                "back",
                PartRole.BACK,
                0,
                Dimensions3D(
                    width_um=inner_width + 2 * dado_depth,
                    depth_um=p.back_thickness_um,
                    height_um=inner_height + 2 * dado_depth,
                ),
                Placement(
                    x_um=t - dado_depth,
                    y_um=p.depth_um - mm(12) - p.back_thickness_um,
                    z_um=shelf_zone_bottom - dado_depth,
                ),
                grain=GrainDirection.Z,
                visible=(FaceName.BACK,),
            )

        if p.plinth_height_um:
            add(
                "plinth",
                PartRole.PLINTH,
                0,
                Dimensions3D(
                    width_um=inner_width,
                    depth_um=t,
                    # With base cabinets the plinth ends exactly at the
                    # lower-panel datum. The no-base carcass retains its dado
                    # tongue into the main bottom.
                    height_um=(
                        p.plinth_height_um
                        if p.base_cabinet_count
                        else p.plinth_height_um + dado_depth
                    ),
                ),
                Placement(x_um=t),
                grain=GrainDirection.X,
                visible=(FaceName.FRONT,),
            )
        return tuple(parts)

    @staticmethod
    def _dado_depth(thickness_um: int) -> int:
        """Versioned MVP dado depth: one third of stock, bounded to 1–12 mm."""

        return max(mm(1), min(DADO_MAX_DEPTH_UM, thickness_um // 3))

    @staticmethod
    def _raw_size(size: Dimensions3D, thickness_um: int, allowance_um: int) -> Dimensions3D:
        if not allowance_um:
            return size

        def expanded(dimension: int) -> int:
            return dimension if dimension == thickness_um else dimension + 2 * allowance_um

        return Dimensions3D(
            width_um=expanded(size.width_um),
            depth_um=expanded(size.depth_um),
            height_um=expanded(size.height_um),
        )

    @staticmethod
    def _weight_g(size: Dimensions3D, density_kg_m3: int) -> int:
        numerator = size.width_um * size.depth_um * size.height_um * density_kg_m3 * 1_000
        denominator = 1_000_000**3
        return max(1, (numerator + denominator - 1) // denominator)

    @staticmethod
    def _bay_widths(
        inner_width_um: int,
        thickness_um: int,
        divider_count: int,
        ratios_ppm: tuple[int, ...] = (),
    ) -> tuple[int, ...]:
        bay_count = divider_count + 1
        available = inner_width_um - divider_count * thickness_um
        if ratios_ppm and len(ratios_ppm) == bay_count:
            total = sum(ratios_ppm)
            widths = [(available * ratio) // total for ratio in ratios_ppm]
            remainder = available - sum(widths)
            order = sorted(
                range(bay_count),
                key=lambda index: (-(available * ratios_ppm[index] % total), index),
            )
            for index in order[:remainder]:
                widths[index] += 1
            return tuple(widths)
        base, remainder = divmod(available, bay_count)
        return tuple(base + (1 if index < remainder else 0) for index in range(bay_count))

    @staticmethod
    def _shelf_positions(
        bottom_surface_z_um: int,
        inner_height_um: int,
        thickness_um: int,
        shelf_count: int,
        ratios_ppm: tuple[int, ...] = (),
    ) -> tuple[int, ...]:
        if shelf_count == 0:
            return ()
        if ratios_ppm and len(ratios_ppm) == shelf_count:
            return tuple(
                bottom_surface_z_um
                + (inner_height_um * ratio + 500_000) // 1_000_000
                - thickness_um // 2
                for ratio in ratios_ppm
            )
        clear_total = inner_height_um - shelf_count * thickness_um
        opening, remainder = divmod(clear_total, shelf_count + 1)
        cursor = bottom_surface_z_um
        positions: list[int] = []
        for row in range(shelf_count):
            cursor += opening + (1 if row < remainder else 0)
            positions.append(cursor)
            cursor += thickness_um
        return tuple(positions)

    def _build_joints(
        self,
        spec: BookcaseDesignSpec,
        parts: tuple[PartInstance, ...],
    ) -> tuple[tuple[PartInstance, ...], tuple[Joint, ...]]:
        p = spec.parameters
        inner_width = p.width_um - 2 * p.actual_thickness_um
        shelf_zone_bottom = (
            p.plinth_height_um + p.base_cabinet_height_um
            if p.base_cabinet_count
            else p.plinth_height_um + p.actual_thickness_um
        )
        inner_height = p.height_um - shelf_zone_bottom - p.actual_thickness_um
        by_key = {part.semantic_key: part for part in parts}
        feature_map: dict[str, list[ManufacturingFeature]] = defaultdict(list)
        joints: list[Joint] = []

        def join(
            key: str,
            first_key: str,
            second_key: str,
            kind: JointType,
            first_face: FaceName,
            second_face: FaceName,
            direction: AssemblyDirection,
            mating: Point3D,
            *,
            first_feature_face: FaceName | None = None,
            second_feature_face: FaceName | None = None,
            first_feature_dimensions: FeatureDimensions | None = None,
        ) -> None:
            first, second = by_key[first_key], by_key[second_key]
            joint_id = stable_id("joint", spec.design_id, key)
            first_feature, second_feature = self._feature_pair(
                joint_id,
                first,
                second,
                kind,
                first_feature_face or first_face,
                second_feature_face or second_face,
                p.actual_thickness_um,
                mating,
                first_feature_dimensions,
            )
            feature_map[first.part_id].append(first_feature)
            if second_feature is not None:
                feature_map[second.part_id].append(second_feature)
            hardware_sku, hardware_count = self._hardware(kind)
            joints.append(
                Joint(
                    joint_id=joint_id,
                    joint_type=kind,
                    members=(
                        JointMember(
                            part_id=first.part_id,
                            mating_face=first_face,
                            feature_ids=(first_feature.feature_id,),
                        ),
                        JointMember(
                            part_id=second.part_id,
                            mating_face=second_face,
                            feature_ids=(second_feature.feature_id,) if second_feature else (),
                        ),
                    ),
                    mating_origin=mating,
                    assembly_direction=direction,
                    hardware_sku=hardware_sku,
                    hardware_count=hardware_count,
                    tolerance_um=(DADO_FIT_CLEARANCE_UM if kind == JointType.DADO else mm("0.2")),
                )
            )

        left, right = by_key["left-side"], by_key["right-side"]
        bottom, top = by_key["bottom"], by_key["top"]
        join(
            "bottom-left",
            "left-side",
            "bottom",
            p.joint_system,
            FaceName.RIGHT,
            FaceName.LEFT,
            AssemblyDirection.POS_X,
            Point3D(x_um=p.actual_thickness_um, z_um=bottom.placement.z_um),
        )
        join(
            "bottom-right",
            "right-side",
            "bottom",
            p.joint_system,
            FaceName.LEFT,
            FaceName.RIGHT,
            AssemblyDirection.NEG_X,
            Point3D(x_um=p.width_um - p.actual_thickness_um, z_um=bottom.placement.z_um),
        )
        join(
            "top-left",
            "left-side",
            "top",
            p.joint_system,
            FaceName.RIGHT,
            FaceName.LEFT,
            AssemblyDirection.POS_X,
            Point3D(x_um=p.actual_thickness_um, z_um=p.height_um - p.actual_thickness_um),
        )
        join(
            "top-right",
            "right-side",
            "top",
            p.joint_system,
            FaceName.LEFT,
            FaceName.RIGHT,
            AssemblyDirection.NEG_X,
            Point3D(
                x_um=p.width_um - p.actual_thickness_um, z_um=p.height_um - p.actual_thickness_um
            ),
        )

        dividers = sorted(
            (part for part in parts if part.role == PartRole.DIVIDER),
            key=lambda item: item.instance_index,
        )
        for divider in dividers:
            join(
                f"divider-{divider.instance_index}-bottom",
                "bottom",
                divider.semantic_key,
                JointType.DADO,
                FaceName.TOP,
                FaceName.BOTTOM,
                AssemblyDirection.POS_Z,
                Point3D(
                    x_um=divider.placement.x_um,
                    z_um=bottom.placement.z_um + bottom.finished_size.height_um,
                ),
            )
            join(
                f"divider-{divider.instance_index}-top",
                "top",
                divider.semantic_key,
                JointType.DADO,
                FaceName.BOTTOM,
                FaceName.TOP,
                AssemblyDirection.NEG_Z,
                Point3D(x_um=divider.placement.x_um, z_um=top.placement.z_um),
            )

        supports: list[PartInstance] = [left, *dividers, right]
        shelves = sorted(
            (part for part in parts if part.role == PartRole.SHELF),
            key=lambda item: item.instance_index,
        )
        bay_count = p.vertical_divider_count + 1
        for shelf in shelves:
            row, bay = divmod(shelf.instance_index, bay_count)
            shelf_joint_type = (
                JointType.SHELF_PIN if p.shelf_mount == ShelfMount.ADJUSTABLE else p.joint_system
            )
            shelf_inset = (
                self._dado_depth(p.actual_thickness_um) if shelf_joint_type == JointType.DADO else 0
            )
            join(
                f"shelf-r{row}-b{bay}-left",
                supports[bay].semantic_key,
                shelf.semantic_key,
                shelf_joint_type,
                FaceName.RIGHT,
                FaceName.LEFT,
                AssemblyDirection.POS_X,
                Point3D(
                    x_um=shelf.placement.x_um + shelf_inset,
                    z_um=shelf.placement.z_um,
                ),
            )
            join(
                f"shelf-r{row}-b{bay}-right",
                supports[bay + 1].semantic_key,
                shelf.semantic_key,
                shelf_joint_type,
                FaceName.LEFT,
                FaceName.RIGHT,
                AssemblyDirection.NEG_X,
                Point3D(
                    x_um=(shelf.placement.x_um + shelf.finished_size.width_um - shelf_inset),
                    z_um=shelf.placement.z_um,
                ),
            )

        base_bottoms = tuple(
            sorted(
                (part for part in parts if part.role == PartRole.BASE_BOTTOM),
                key=lambda item: item.instance_index,
            )
        )
        if base_bottoms:
            for cabinet_index, base_bottom in enumerate(base_bottoms):
                left_support = left if cabinet_index == 0 else by_key[f"base-side-{cabinet_index}"]
                right_support = (
                    right
                    if cabinet_index == len(base_bottoms) - 1
                    else by_key[f"base-side-{cabinet_index + 1}"]
                )
                join(
                    f"base-bottom-{cabinet_index}-left-support",
                    left_support.semantic_key,
                    base_bottom.semantic_key,
                    JointType.DADO,
                    FaceName.RIGHT,
                    FaceName.LEFT,
                    AssemblyDirection.POS_X,
                    Point3D(
                        x_um=left_support.placement.x_um + left_support.finished_size.width_um,
                        z_um=base_bottom.placement.z_um,
                    ),
                )
                join(
                    f"base-bottom-{cabinet_index}-right-support",
                    right_support.semantic_key,
                    base_bottom.semantic_key,
                    JointType.DADO,
                    FaceName.LEFT,
                    FaceName.RIGHT,
                    AssemblyDirection.NEG_X,
                    Point3D(
                        x_um=right_support.placement.x_um,
                        z_um=base_bottom.placement.z_um,
                    ),
                )

            for base_side in sorted(
                (part for part in parts if part.role == PartRole.BASE_SIDE),
                key=lambda item: item.instance_index,
            ):
                join(
                    f"{base_side.semantic_key}-upper-bottom",
                    "bottom",
                    base_side.semantic_key,
                    JointType.DADO,
                    FaceName.BOTTOM,
                    FaceName.TOP,
                    AssemblyDirection.POS_Z,
                    Point3D(
                        x_um=base_side.placement.x_um,
                        z_um=bottom.placement.z_um,
                    ),
                )

        if "back" in by_key:
            back_kind = (
                JointType.DADO if p.back_panel == BackPanelType.INSET_GROOVE else JointType.RABBET
            )
            back = by_key["back"]
            back_feature_faces = {
                "left-side": FaceName.B,
                "right-side": FaceName.A,
                "top": FaceName.A,
                "bottom": FaceName.B,
            }
            back_mating_faces = {
                "left-side": (FaceName.B, FaceName.LEFT),
                "right-side": (FaceName.A, FaceName.RIGHT),
                "top": (FaceName.A, FaceName.TOP),
                "bottom": (FaceName.B, FaceName.BOTTOM),
            }
            if back_kind == JointType.DADO:
                back_origins = {
                    "left-side": Point3D(
                        x_um=p.actual_thickness_um,
                        y_um=back.placement.y_um,
                        z_um=back.placement.z_um,
                    ),
                    "right-side": Point3D(
                        x_um=p.width_um - p.actual_thickness_um,
                        y_um=back.placement.y_um,
                        z_um=back.placement.z_um,
                    ),
                    "top": Point3D(
                        x_um=max(top.placement.x_um, back.placement.x_um),
                        y_um=back.placement.y_um,
                        z_um=top.placement.z_um,
                    ),
                    "bottom": Point3D(
                        x_um=max(bottom.placement.x_um, back.placement.x_um),
                        y_um=back.placement.y_um,
                        z_um=bottom.placement.z_um + bottom.finished_size.height_um,
                    ),
                }
            else:
                back_origins = {
                    "left-side": Point3D(
                        x_um=p.actual_thickness_um,
                        y_um=back.placement.y_um,
                        z_um=bottom.placement.z_um + bottom.finished_size.height_um,
                    ),
                    "right-side": Point3D(
                        x_um=p.width_um - p.actual_thickness_um,
                        y_um=back.placement.y_um,
                        z_um=bottom.placement.z_um + bottom.finished_size.height_um,
                    ),
                    "top": Point3D(
                        x_um=p.actual_thickness_um,
                        y_um=back.placement.y_um,
                        z_um=top.placement.z_um,
                    ),
                    "bottom": Point3D(
                        x_um=p.actual_thickness_um,
                        y_um=back.placement.y_um,
                        z_um=bottom.placement.z_um,
                    ),
                }
            groove_depth = max(mm(1), min(mm(12), p.actual_thickness_um // 3))
            back_feature_dimensions = {
                "left-side": FeatureDimensions(
                    width_um=p.back_thickness_um,
                    depth_um=groove_depth,
                    length_um=inner_height,
                ),
                "right-side": FeatureDimensions(
                    width_um=p.back_thickness_um,
                    depth_um=groove_depth,
                    length_um=inner_height,
                ),
                "top": FeatureDimensions(
                    width_um=inner_width,
                    depth_um=groove_depth,
                    length_um=p.back_thickness_um,
                ),
                "bottom": FeatureDimensions(
                    width_um=inner_width,
                    depth_um=groove_depth,
                    length_um=p.back_thickness_um,
                ),
            }
            for boundary in ("left-side", "right-side", "top", "bottom"):
                feature_dimensions = (
                    None if back_kind == JointType.DADO else back_feature_dimensions[boundary]
                )
                join(
                    f"back-{boundary}",
                    boundary,
                    "back",
                    back_kind,
                    (
                        back_mating_faces[boundary][0]
                        if back_kind == JointType.DADO
                        else FaceName.BACK
                    ),
                    (back_mating_faces[boundary][1] if back_kind == JointType.DADO else FaceName.A),
                    AssemblyDirection.NEG_Y,
                    back_origins[boundary],
                    first_feature_face=back_feature_faces[boundary],
                    first_feature_dimensions=feature_dimensions,
                )

        if "plinth" in by_key and not p.base_cabinet_count:
            join(
                "plinth-bottom",
                "bottom",
                "plinth",
                JointType.DADO,
                FaceName.BOTTOM,
                FaceName.TOP,
                AssemblyDirection.NEG_Z,
                Point3D(
                    x_um=p.actual_thickness_um,
                    y_um=0,
                    z_um=p.plinth_height_um,
                ),
                first_feature_face=FaceName.A,
            )

        updated_parts = tuple(
            part.model_copy(update={"features": tuple(feature_map.get(part.part_id, ()))})
            for part in parts
        )
        self._validate_dado_interlocks(updated_parts, tuple(joints))
        return updated_parts, tuple(joints)

    @staticmethod
    def _feature_pair(
        joint_id: str,
        first: PartInstance,
        second: PartInstance,
        kind: JointType,
        first_face: FaceName,
        second_face: FaceName,
        thickness_um: int,
        mating: Point3D,
        first_dimensions_override: FeatureDimensions | None = None,
    ) -> tuple[ManufacturingFeature, ManufacturingFeature | None]:
        depth = max(mm(1), min(mm(12), thickness_um // 3))
        feature_kind: dict[JointType, tuple[FeatureKind, FeatureKind | None]] = {
            JointType.DOWEL: (FeatureKind.DRILL, FeatureKind.DRILL),
            JointType.CONFIRMAT: (FeatureKind.COUNTERSINK, FeatureKind.DRILL),
            JointType.CAM_DOWEL: (FeatureKind.POCKET, FeatureKind.DRILL),
            JointType.SHELF_PIN: (FeatureKind.SERIES_DRILL, None),
            JointType.RABBET: (FeatureKind.RABBET, None),
            JointType.DADO: (FeatureKind.GROOVE, None),
            JointType.TENON: (FeatureKind.POCKET, FeatureKind.OUTER_CONTOUR),
        }
        first_kind, second_kind = feature_kind[kind]
        dado_geometry = (
            BookcaseEngine._dado_feature_geometry(
                owner=first,
                mate=second,
                owner_face=first_face,
                depth_um=depth,
                fit_clearance_um=DADO_FIT_CLEARANCE_UM,
            )
            if kind == JointType.DADO
            else None
        )

        def dimensions(
            feature: FeatureKind,
            owner: PartInstance,
            mate: PartInstance,
        ) -> FeatureDimensions:
            if feature in {
                FeatureKind.DRILL,
                FeatureKind.SERIES_DRILL,
                FeatureKind.COUNTERSINK,
                FeatureKind.POCKET,
            }:
                diameter = (
                    mm(15)
                    if feature == FeatureKind.POCKET and kind == JointType.CAM_DOWEL
                    else mm(5 if kind == JointType.SHELF_PIN else 8)
                )
                return FeatureDimensions(diameter_um=diameter, depth_um=depth)
            owner_axes = BookcaseEngine._panel_axes(owner)
            owner_dimensions = BookcaseEngine._axis_dimensions(owner)
            mate_dimensions = BookcaseEngine._axis_dimensions(mate)
            return FeatureDimensions(
                width_um=min(owner_dimensions[owner_axes[0]], mate_dimensions[owner_axes[0]]),
                depth_um=depth,
                length_um=min(owner_dimensions[owner_axes[1]], mate_dimensions[owner_axes[1]]),
            )

        first_id = stable_id("feature", joint_id, first.part_id)
        second_id = stable_id("feature", joint_id, second.part_id)
        first_pattern = (
            2 if first_kind == FeatureKind.SERIES_DRILL or kind == JointType.DOWEL else 1
        )
        second_pattern = 2 if kind == JointType.DOWEL else 1
        first_dimensions = (
            dado_geometry[0]
            if dado_geometry is not None
            else first_dimensions_override or dimensions(first_kind, first, second)
        )
        first_pitch = mm(32) if first_pattern > 1 else None
        second_pitch = mm(32) if second_pattern > 1 else None
        first_origin = (
            dado_geometry[1]
            if dado_geometry is not None
            else BookcaseEngine._feature_origin(
                first,
                first_kind,
                first_dimensions,
                mating,
                first_pattern,
                first_pitch,
            )
        )
        first_open_end_reliefs = (
            BookcaseEngine._open_end_reliefs(first, first_dimensions, first_origin)
            if kind == JointType.DADO
            else ()
        )
        second_dimensions = (
            dimensions(second_kind, second, first) if second_kind is not None else None
        )
        return (
            ManufacturingFeature(
                feature_id=first_id,
                part_id=first.part_id,
                joint_id=joint_id,
                kind=first_kind,
                face=first_face,
                origin=first_origin,
                dimensions=first_dimensions,
                pattern_count=first_pattern,
                pitch_um=first_pitch,
                tolerance_um=(DADO_FEATURE_TOLERANCE_UM if kind == JointType.DADO else mm("0.15")),
                fit_clearance_um=(DADO_FIT_CLEARANCE_UM if kind == JointType.DADO else 0),
                corner_strategy="dogbone-v1" if kind == JointType.DADO else None,
                requires_square_corners=kind == JointType.DADO,
                open_end_reliefs=first_open_end_reliefs,
            ),
            ManufacturingFeature(
                feature_id=second_id,
                part_id=second.part_id,
                joint_id=joint_id,
                kind=second_kind,
                face=second_face,
                origin=BookcaseEngine._feature_origin(
                    second,
                    second_kind,
                    second_dimensions,
                    mating,
                    second_pattern,
                    second_pitch,
                ),
                dimensions=second_dimensions,
                pattern_count=second_pattern,
                pitch_um=second_pitch,
            )
            if second_kind is not None and second_dimensions is not None
            else None,
        )

    @staticmethod
    def _feature_origin(
        part: PartInstance,
        kind: FeatureKind,
        dimensions: FeatureDimensions,
        mating: Point3D,
        pattern_count: int,
        pitch_um: int | None,
    ) -> Point3D:
        """Project a joint reference into a safe, part-local machining origin.

        Drill coordinates represent centres. Rectangular operations represent
        their lower-left corner. The calculation uses the same panel-axis
        convention as the manufacturing adapter and never silently places a
        cutter centre on a finished edge.
        """

        coordinates = {
            "x": mating.x_um - part.placement.x_um,
            "y": mating.y_um - part.placement.y_um,
            "z": mating.z_um - part.placement.z_um,
        }
        dimensions_by_axis = BookcaseEngine._axis_dimensions(part)
        panel_axes = BookcaseEngine._panel_axes(part)

        hole_kinds = {
            FeatureKind.DRILL,
            FeatureKind.SERIES_DRILL,
            FeatureKind.COUNTERSINK,
        }
        if kind in hole_kinds:
            radius = (dimensions.diameter_um or 0) // 2
            margin = radius + mm(2)
            pitch_extent = (pattern_count - 1) * (pitch_um or 0)
            extents = (margin + pitch_extent, margin)
            lower_bounds = (margin, margin)
        else:
            width = dimensions.width_um or dimensions.diameter_um or mm(1)
            length = dimensions.length_um or width
            extents = (width, length)
            lower_bounds = (0, 0)

        for index, axis in enumerate(panel_axes):
            lower = lower_bounds[index]
            upper = dimensions_by_axis[axis] - extents[index]
            if upper < lower:
                raise ValueError(
                    f"feature {kind.value} does not fit part {part.semantic_key} on {axis}-axis"
                )
            coordinates[axis] = min(max(coordinates[axis], lower), upper)

        thickness_axis = ({"x", "y", "z"} - set(panel_axes)).pop()
        coordinates[thickness_axis] = 0
        return Point3D(
            x_um=coordinates["x"],
            y_um=coordinates["y"],
            z_um=coordinates["z"],
        )

    @staticmethod
    def _dado_feature_geometry(
        *,
        owner: PartInstance,
        mate: PartInstance,
        owner_face: FaceName,
        depth_um: int,
        fit_clearance_um: int,
    ) -> tuple[FeatureDimensions, Point3D]:
        """Derive a groove from the actual global intersection of both members."""

        if any(
            angle
            for part in (owner, mate)
            for angle in (
                part.placement.rotation_x_mdeg,
                part.placement.rotation_y_mdeg,
                part.placement.rotation_z_mdeg,
            )
        ):
            raise ValueError("dado geometry currently requires axis-aligned part placements")

        normal_axis, face_sign = BookcaseEngine._physical_face(owner, owner_face)
        panel_axes = BookcaseEngine._panel_axes(owner)
        if normal_axis in panel_axes:
            raise ValueError(
                f"dado face {owner_face.value} is not a broad face of {owner.semantic_key}"
            )

        mate_panel_axes = BookcaseEngine._panel_axes(mate)
        mate_thickness_axis = ({"x", "y", "z"} - set(mate_panel_axes)).pop()
        if mate_thickness_axis not in panel_axes:
            raise ValueError(f"mating thickness of {mate.semantic_key} is not in the groove plane")

        owner_normal = BookcaseEngine._global_interval(owner, normal_axis)
        mate_normal = BookcaseEngine._global_interval(mate, normal_axis)
        expected_normal = (
            (owner_normal[0], owner_normal[0] + depth_um)
            if face_sign < 0
            else (owner_normal[1] - depth_um, owner_normal[1])
        )

        actual_normal = (
            max(owner_normal[0], mate_normal[0]),
            min(owner_normal[1], mate_normal[1]),
        )
        if actual_normal != expected_normal:
            raise ValueError(
                f"dado {owner.semantic_key}/{mate.semantic_key} interlock is "
                f"{max(0, actual_normal[1] - actual_normal[0])} µm; expected {depth_um} µm"
            )

        global_starts: dict[str, int] = {}
        global_lengths: dict[str, int] = {}
        for axis in panel_axes:
            owner_interval = BookcaseEngine._global_interval(owner, axis)
            mate_interval = BookcaseEngine._global_interval(mate, axis)
            if axis == mate_thickness_axis:
                start, end = BookcaseEngine._fit_interval(
                    owner_interval,
                    mate_interval,
                    fit_clearance_um,
                )
            else:
                start = max(owner_interval[0], mate_interval[0])
                end = min(owner_interval[1], mate_interval[1])
                if end <= start:
                    raise ValueError(
                        f"dado members {owner.semantic_key}/{mate.semantic_key} "
                        f"do not overlap on {axis}"
                    )
            global_starts[axis] = start
            global_lengths[axis] = end - start

        local_coordinates = {"x": 0, "y": 0, "z": 0}
        for axis in panel_axes:
            local_coordinates[axis] = global_starts[axis] - BookcaseEngine._placement_axis(
                owner, axis
            )
        return (
            FeatureDimensions(
                width_um=global_lengths[panel_axes[0]],
                depth_um=depth_um,
                length_um=global_lengths[panel_axes[1]],
                radius_um=DADO_CORNER_RELIEF_RADIUS_UM,
            ),
            Point3D(
                x_um=local_coordinates["x"],
                y_um=local_coordinates["y"],
                z_um=local_coordinates["z"],
            ),
        )

    @staticmethod
    def _open_end_reliefs(
        owner: PartInstance,
        dimensions: FeatureDimensions,
        origin: Point3D,
    ) -> tuple[OpenEndRelief, ...]:
        """Declare only dado exits whose nominal rectangle is exactly edge-flush."""

        panel_axes = BookcaseEngine._panel_axes(owner)
        owner_dimensions = BookcaseEngine._axis_dimensions(owner)
        starts = {axis: int(getattr(origin, f"{axis}_um")) for axis in panel_axes}
        extents = {
            panel_axes[0]: dimensions.width_um,
            panel_axes[1]: dimensions.length_um,
        }
        if any(extent is None for extent in extents.values()):
            raise ValueError("open-end relief declaration requires rectangular dado dimensions")

        declarations: list[OpenEndRelief] = []
        labels = (
            (OpenEndRelief.U_MIN, OpenEndRelief.U_MAX),
            (OpenEndRelief.V_MIN, OpenEndRelief.V_MAX),
        )
        for index, axis in enumerate(panel_axes):
            extent = extents[axis]
            assert extent is not None
            start = starts[axis]
            if start == 0:
                declarations.append(labels[index][0])
            if start + extent == owner_dimensions[axis]:
                declarations.append(labels[index][1])
        return tuple(declarations)

    @staticmethod
    def _fit_interval(
        owner_interval: tuple[int, int],
        mate_interval: tuple[int, int],
        clearance_um: int,
    ) -> tuple[int, int]:
        """Add total nominal fit clearance, shifting it inward at stock edges."""

        owner_start, owner_end = owner_interval
        mate_start, mate_end = mate_interval
        if mate_start < owner_start or mate_end > owner_end or mate_end <= mate_start:
            raise ValueError("dado mating thickness lies outside the grooved panel face")
        target_width = mate_end - mate_start + clearance_um
        if target_width > owner_end - owner_start:
            raise ValueError("dado fit clearance does not fit the grooved panel face")
        start = mate_start - clearance_um // 2
        end = start + target_width
        if start < owner_start:
            end += owner_start - start
            start = owner_start
        if end > owner_end:
            start -= end - owner_end
            end = owner_end
        if start < owner_start or end > owner_end:
            raise ValueError("dado fit interval could not be contained in the grooved panel")
        return start, end

    @staticmethod
    def _physical_face(part: PartInstance, face: FaceName) -> tuple[str, int]:
        if face == FaceName.A:
            if part.role in {
                PartRole.LEFT_SIDE,
                PartRole.RIGHT_SIDE,
                PartRole.DIVIDER,
                PartRole.BASE_SIDE,
            }:
                face = FaceName.LEFT
            elif part.role in {
                PartRole.TOP,
                PartRole.BOTTOM,
                PartRole.SHELF,
                PartRole.BASE_BOTTOM,
                PartRole.BASE_TOP,
            }:
                face = FaceName.BOTTOM
            elif part.role in {PartRole.BACK, PartRole.PLINTH, PartRole.CABINET_FRONT}:
                face = FaceName.FRONT
        elif face == FaceName.B:
            if part.role in {
                PartRole.LEFT_SIDE,
                PartRole.RIGHT_SIDE,
                PartRole.DIVIDER,
                PartRole.BASE_SIDE,
            }:
                face = FaceName.RIGHT
            elif part.role in {
                PartRole.TOP,
                PartRole.BOTTOM,
                PartRole.SHELF,
                PartRole.BASE_BOTTOM,
                PartRole.BASE_TOP,
            }:
                face = FaceName.TOP
            elif part.role in {PartRole.BACK, PartRole.PLINTH, PartRole.CABINET_FRONT}:
                face = FaceName.BACK
        try:
            return {
                FaceName.LEFT: ("x", -1),
                FaceName.RIGHT: ("x", 1),
                FaceName.FRONT: ("y", -1),
                FaceName.BACK: ("y", 1),
                FaceName.BOTTOM: ("z", -1),
                FaceName.TOP: ("z", 1),
            }[face]
        except KeyError as exc:
            raise ValueError(f"cannot resolve physical face {face.value}") from exc

    @staticmethod
    def _placement_axis(part: PartInstance, axis: str) -> int:
        return int(getattr(part.placement, f"{axis}_um"))

    @staticmethod
    def _global_interval(part: PartInstance, axis: str) -> tuple[int, int]:
        start = BookcaseEngine._placement_axis(part, axis)
        return start, start + BookcaseEngine._axis_dimensions(part)[axis]

    @staticmethod
    def _validate_dado_interlocks(
        parts: tuple[PartInstance, ...],
        joints: tuple[Joint, ...],
    ) -> None:
        """Fail compilation if any dado feature diverges from its mating solid."""

        by_id = {part.part_id: part for part in parts}
        feature_by_id = {feature.feature_id: feature for part in parts for feature in part.features}
        for joint in joints:
            if joint.joint_type != JointType.DADO:
                continue
            cut_member, mate_member = joint.members
            if len(cut_member.feature_ids) != 1 or mate_member.feature_ids:
                raise ValueError("a dado must have one groove and one uncut mating edge")
            feature = feature_by_id[cut_member.feature_ids[0]]
            if feature.kind != FeatureKind.GROOVE or feature.dimensions.depth_um is None:
                raise ValueError("a dado cut member must reference one depth-defined groove")
            expected_dimensions, expected_origin = BookcaseEngine._dado_feature_geometry(
                owner=by_id[cut_member.part_id],
                mate=by_id[mate_member.part_id],
                owner_face=feature.face,
                depth_um=feature.dimensions.depth_um,
                fit_clearance_um=joint.tolerance_um,
            )
            if feature.dimensions != expected_dimensions or feature.origin != expected_origin:
                raise ValueError(f"dado feature geometry diverges for joint {joint.joint_id}")
            expected_open_ends = BookcaseEngine._open_end_reliefs(
                by_id[cut_member.part_id], expected_dimensions, expected_origin
            )
            if feature.open_end_reliefs != expected_open_ends:
                raise ValueError(
                    f"dado open-end relief declaration diverges for joint {joint.joint_id}"
                )
            if feature.tolerance_um * 2 >= joint.tolerance_um:
                raise ValueError(
                    f"dado machining tolerance consumes fit for joint {joint.joint_id}"
                )

    @staticmethod
    def _axis_dimensions(part: PartInstance) -> dict[str, int]:
        return {
            "x": part.finished_size.width_um,
            "y": part.finished_size.depth_um,
            "z": part.finished_size.height_um,
        }

    @staticmethod
    def _panel_axes(part: PartInstance) -> tuple[str, str]:
        if part.role in {
            PartRole.LEFT_SIDE,
            PartRole.RIGHT_SIDE,
            PartRole.DIVIDER,
            PartRole.BASE_SIDE,
        }:
            return ("y", "z")
        if part.role in {PartRole.BACK, PartRole.PLINTH, PartRole.CABINET_FRONT}:
            return ("x", "z")
        return ("x", "y")

    @staticmethod
    def _hardware(kind: JointType) -> tuple[str | None, int]:
        return {
            JointType.DOWEL: ("dowel-8x30", 2),
            JointType.CONFIRMAT: ("confirmat-7x50", 2),
            JointType.CAM_DOWEL: ("cam-15-with-dowel", 2),
            JointType.SHELF_PIN: ("shelf-pin-5", 2),
            JointType.RABBET: (None, 0),
            JointType.DADO: (None, 0),
            JointType.TENON: (None, 0),
        }[kind]

    @staticmethod
    def _build_assembly_graph(
        parts: tuple[PartInstance, ...],
        joints: tuple[Joint, ...],
    ) -> AssemblyGraph:
        nodes = tuple(
            AssemblyNode(part_id=part.part_id, semantic_key=part.semantic_key) for part in parts
        )
        edges = tuple(
            AssemblyEdge(
                joint_id=joint.joint_id,
                from_part_id=joint.members[0].part_id,
                to_part_id=joint.members[1].part_id,
            )
            for joint in joints
        )
        tool_for = {
            JointType.DOWEL: ("mallet",),
            JointType.CONFIRMAT: ("hex-key-4mm",),
            JointType.SHELF_PIN: (),
            JointType.RABBET: (),
            JointType.DADO: (),
            JointType.TENON: ("mallet",),
        }
        by_id = {part.part_id: part for part in parts}
        by_key = {part.semantic_key: part for part in parts}
        used: set[str] = set()
        steps_list: list[AssemblyStep] = []

        def names(joint: Joint) -> frozenset[str]:
            return frozenset(by_id[member.part_id].semantic_key for member in joint.members)

        def add_step(
            selected: tuple[Joint, ...],
            direction: AssemblyDirection,
            checkpoint: str,
            *,
            moving_parts: tuple[PartInstance, ...],
            motion_path: tuple[AssemblyDirection, ...] | None = None,
            required_tools: tuple[str, ...] = (),
        ) -> None:
            current = tuple(
                sorted(
                    (joint for joint in selected if joint.joint_id not in used),
                    key=lambda item: item.joint_id,
                )
            )
            if not current:
                return
            joint_ids = tuple(joint.joint_id for joint in current)
            moving_part_ids = tuple(sorted({part.part_id for part in moving_parts}))
            joint_part_ids = {member.part_id for joint in current for member in joint.members}
            if not moving_part_ids or not set(moving_part_ids) <= set(by_id):
                raise ValueError("assembly step moving group must belong to the design")
            part_ids = tuple(sorted(joint_part_ids | set(moving_part_ids)))
            tool_ids = tuple(
                sorted(
                    {
                        *required_tools,
                        *(tool for joint in current for tool in tool_for[joint.joint_type]),
                    }
                )
            )
            steps_list.append(
                AssemblyStep(
                    step_number=len(steps_list) + 1,
                    step_id=stable_id("assembly-step", *joint_ids),
                    part_ids=part_ids,
                    moving_part_ids=moving_part_ids,
                    joint_ids=joint_ids,
                    direction=direction,
                    motion_path=motion_path or (direction,),
                    tool_ids=tool_ids,
                    checkpoint=checkpoint,
                )
            )
            used.update(joint_ids)

        # Build the internal lower-cabinet bank from left to right while both
        # full-height outer sides remain open.  Each internal side captures the
        # left bottom first; the next bottom then enters its open right groove.
        # The existing carcass-side steps close the two remaining outer joints.
        base_sides = tuple(
            sorted(
                (part for part in parts if part.role == PartRole.BASE_SIDE),
                key=lambda item: item.instance_index,
            )
        )
        for base_side in base_sides:
            side_index = base_side.instance_index
            left_bottom = by_key[f"base-bottom-{side_index - 1}"]
            right_bottom = by_key[f"base-bottom-{side_index}"]
            add_step(
                tuple(
                    joint
                    for joint in joints
                    if names(joint) == {base_side.semantic_key, left_bottom.semantic_key}
                ),
                AssemblyDirection.NEG_X,
                (
                    "För underskåpssidan från höger över vänster bottenkant och "
                    "kontrollera fullt spårinstick."
                ),
                moving_parts=(base_side,),
            )
            add_step(
                tuple(
                    joint
                    for joint in joints
                    if names(joint) == {base_side.semantic_key, right_bottom.semantic_key}
                ),
                AssemblyDirection.NEG_X,
                "För nästa underskåpsbotten från höger in i sidans öppna spår.",
                moving_parts=(right_bottom,),
            )

        add_step(
            tuple(
                joint
                for joint in joints
                if "bottom" in names(joint)
                and any(
                    by_id[member.part_id].role == PartRole.BASE_SIDE for member in joint.members
                )
            ),
            AssemblyDirection.NEG_Z,
            (
                "Sänk överdelens botten över samtliga interna underskåpssidor och "
                "kontrollera den sammanhängande lodräta lastvägen."
            ),
            moving_parts=(by_key["bottom"],),
        )

        # Build the captured-panel subassembly while both carcass sides remain
        # open.  This prevents a fixed shelf or inset back from being trapped by
        # a closed frame.  Several joints intentionally belong to one movement.
        add_step(
            tuple(
                joint
                for joint in joints
                if names(joint) == {"bottom", "plinth"} and joint.joint_type == JointType.DADO
            ),
            AssemblyDirection.NEG_Z,
            "Sänk botten över sockelns övre tappkant och kontrollera full anliggning.",
            moving_parts=(by_key["bottom"],),
        )

        dividers = tuple(
            sorted(
                (part for part in parts if part.role == PartRole.DIVIDER),
                key=lambda item: item.instance_index,
            )
        )
        if dividers:
            for divider_index, divider in enumerate(dividers):
                shelf_joints = tuple(
                    joint
                    for joint in joints
                    if joint.joint_type == JointType.DADO
                    and divider.part_id in {member.part_id for member in joint.members}
                    and any(
                        by_id[member.part_id].role == PartRole.SHELF for member in joint.members
                    )
                )
                left_joints = tuple(
                    joint
                    for joint in shelf_joints
                    if any(
                        member.part_id == divider.part_id and member.mating_face == FaceName.LEFT
                        for member in joint.members
                    )
                )
                right_joints = tuple(
                    joint
                    for joint in shelf_joints
                    if any(
                        member.part_id == divider.part_id and member.mating_face == FaceName.RIGHT
                        for member in joint.members
                    )
                )

                def shelves_for(selected: tuple[Joint, ...]) -> tuple[PartInstance, ...]:
                    return tuple(
                        by_id[part_id]
                        for part_id in sorted(
                            {
                                member.part_id
                                for joint in selected
                                for member in joint.members
                                if by_id[member.part_id].role == PartRole.SHELF
                            }
                        )
                    )

                if divider_index == 0:
                    add_step(
                        left_joints,
                        AssemblyDirection.POS_X,
                        (
                            "För in vänsterfackets hyllkanter i avdelarens öppna spår; "
                            "kontrollera spårbotten och del-ID före nästa grupp."
                        ),
                        moving_parts=shelves_for(left_joints),
                    )
                else:
                    add_step(
                        left_joints,
                        AssemblyDirection.NEG_X,
                        ("För in avdelaren från höger över föregående facks exponerade hylländar."),
                        moving_parts=(divider,),
                    )
                add_step(
                    right_joints,
                    AssemblyDirection.NEG_X,
                    (
                        "För in nästa facks hyllkanter från höger i avdelarens öppna spår; "
                        "kontrollera spårbotten och del-ID före nästa avdelare."
                    ),
                    moving_parts=shelves_for(right_joints),
                )

            divider_module_part_ids = {divider.part_id for divider in dividers}
            divider_module_part_ids.update(
                member.part_id
                for joint in joints
                if joint.joint_id in used
                and joint.joint_type == JointType.DADO
                and any(by_id[item.part_id].role == PartRole.DIVIDER for item in joint.members)
                for member in joint.members
                if by_id[member.part_id].role == PartRole.SHELF
            )
            add_step(
                tuple(
                    joint
                    for joint in joints
                    if joint.joint_type == JointType.DADO
                    and "bottom" in names(joint)
                    and any(
                        by_id[member.part_id].role == PartRole.DIVIDER for member in joint.members
                    )
                ),
                AssemblyDirection.NEG_Z,
                "Sänk avdelar-/hyllmodulen i bottens samtliga spår samtidigt.",
                moving_parts=tuple(by_id[part_id] for part_id in sorted(divider_module_part_ids)),
            )

        back = by_key.get("back")
        back_joints = tuple(
            joint
            for joint in joints
            if back is not None and back.part_id in {member.part_id for member in joint.members}
        )
        captured_back_joints = tuple(
            joint for joint in back_joints if joint.joint_type == JointType.DADO
        )
        surface_back_joints = tuple(
            joint for joint in back_joints if joint.joint_type == JointType.RABBET
        )
        add_step(
            tuple(joint for joint in captured_back_joints if names(joint) == {"back", "bottom"}),
            AssemblyDirection.NEG_Z,
            "Sänk ryggens nederkant i bottenspåret innan topp eller gavlar monteras.",
            moving_parts=(back,) if back is not None else (),
        )
        add_step(
            tuple(
                joint
                for joint in joints
                if "top" in names(joint)
                and (
                    joint in captured_back_joints
                    or any(
                        by_id[member.part_id].role == PartRole.DIVIDER for member in joint.members
                    )
                )
            ),
            AssemblyDirection.NEG_Z,
            "Sänk toppen över rygg och avdelare och kontrollera alla samtidiga instick.",
            moving_parts=(by_key["top"],),
        )

        for side_key, direction, checkpoint in (
            (
                "right-side",
                AssemblyDirection.NEG_X,
                (
                    "Säkra samtliga FIX-delar i panelpositioneringsjiggen och för därefter "
                    "in höger gavel från höger över alla exponerade tappkanter samtidigt."
                ),
            ),
            (
                "left-side",
                AssemblyDirection.POS_X,
                (
                    "Stäng stommen med vänster gavel och kontrollera varje spåranslutning "
                    "visuellt. Kontroll av instick verifierar inte permanent hållning."
                ),
            ),
        ):
            add_step(
                tuple(
                    joint
                    for joint in joints
                    if side_key in names(joint) and joint.joint_type == JointType.DADO
                ),
                direction,
                checkpoint,
                moving_parts=(by_key[side_key],),
                required_tools=("panel-positioning-jig",) if side_key == "right-side" else (),
            )

        add_step(
            surface_back_joints,
            AssemblyDirection.NEG_Y,
            (
                "För ryggstycket från baksidan mot den slutna stommen och kontrollera "
                "full anliggning längs samtliga fyra kanter."
            ),
            moving_parts=(back,) if back is not None else (),
        )

        # Adjustable shelves are installed after the carcass has closed.  Both
        # pin joints for one shelf form one operator action.
        adjustable_shelves = tuple(
            sorted(
                (part for part in parts if part.role == PartRole.SHELF),
                key=lambda item: item.instance_index,
            )
        )
        for shelf in adjustable_shelves:
            add_step(
                tuple(
                    joint
                    for joint in joints
                    if joint.joint_type == JointType.SHELF_PIN
                    and shelf.part_id in {member.part_id for member in joint.members}
                ),
                AssemblyDirection.NEG_Z,
                (
                    "Montera rätt hyllbärare, för in den märkta hyllan från framkanten "
                    "ovanför bärarna och sänk den därefter på samtliga bärare."
                ),
                moving_parts=(shelf,),
                motion_path=(AssemblyDirection.POS_Y, AssemblyDirection.NEG_Z),
            )

        unused_joint_ids = {joint.joint_id for joint in joints} - used
        if unused_joint_ids:
            raise ValueError(
                "no verified assembly sequence exists for joints: "
                + ", ".join(sorted(unused_joint_ids))
            )

        covered_part_ids = {part_id for step in steps_list for part_id in step.part_ids}
        for part in sorted(parts, key=lambda item: item.semantic_key):
            if part.part_id in covered_part_ids:
                continue
            is_front = part.role == PartRole.CABINET_FRONT
            steps_list.append(
                AssemblyStep(
                    step_number=len(steps_list) + 1,
                    step_id=stable_id("assembly-step", part.part_id, "hardware-pending"),
                    part_ids=(part.part_id,),
                    moving_part_ids=(part.part_id,),
                    joint_ids=(),
                    direction=(AssemblyDirection.NEG_Y if is_front else AssemblyDirection.NEG_Z),
                    motion_path=(AssemblyDirection.NEG_Y if is_front else AssemblyDirection.NEG_Z,),
                    tool_ids=(),
                    checkpoint=(
                        "FRONT EJ MONTERINGSBAR: montera inte förrän mekaniskt beslag, spel "
                        "och borrbild har verifierats."
                        if is_front
                        else (
                            "Placera moduldelen enligt ritning; infästningen inväntar "
                            "verifierat beslagssystem."
                        )
                    ),
                )
            )

        steps = tuple(steps_list)
        return AssemblyGraph(nodes=nodes, edges=edges, steps=steps)


def build_bookcase(spec: BookcaseDesignSpec) -> DesignResult:
    return BookcaseEngine().build(spec)
