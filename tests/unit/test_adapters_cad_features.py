from __future__ import annotations

import json
from itertools import combinations
from types import SimpleNamespace

import pytest
from custombuild_cad import (
    CADAssemblyCollisionError,
    CADDependencyUnavailable,
    CADExportError,
    CadQueryAdapter,
    UnsupportedCADFeatureError,
)
from custombuild_cad.adapter import _normalise_step
from custombuild_domain import (
    BackPanelType,
    BookcaseDesignSpec,
    BookcaseParameters,
    JointType,
    ShelfMount,
    TemplateProductionLevel,
    build_bookcase,
    resolve_template_capability,
    screening_mdf_6,
    screening_mdf_18,
)
from custombuild_manufacturing import (
    DeterministicNester,
    DFMValidator,
    StockSheet,
    linuxcnc_reference_router_1325,
)
from custombuild_manufacturing.adapters import adapt_design_result, adapt_domain_part
from custombuild_manufacturing.model import FeatureKind, Side


def namespace_part(*, features=(), role: str = "shelf", grain: str = "x"):
    size = SimpleNamespace(width_um=300_000, depth_um=200_000, height_um=18_000)
    return SimpleNamespace(
        part_id="namespace-panel",
        role=role,
        instance_index=0,
        finished_size=size,
        raw_size=size,
        placement=SimpleNamespace(
            x_um=0,
            y_um=0,
            z_um=0,
            rotation_x_mdeg=0,
            rotation_y_mdeg=0,
            rotation_z_mdeg=0,
        ),
        material_id="mdf",
        material_version="v1",
        actual_thickness_um=18_000,
        grain_direction=grain,
        a_side="a",
        b_side="b",
        edge_bands=(),
        features=features,
        weight_g=1,
    )


def dimensions(**values):
    defaults = {
        "diameter_um": None,
        "depth_um": None,
        "width_um": None,
        "length_um": None,
        "radius_um": None,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def feature(
    feature_id: str,
    kind: str,
    *,
    face: str = "top",
    x_um: int = 50_000,
    y_um: int = 50_000,
    z_um: int = 18_000,
    feature_dimensions=None,
    count: int = 1,
    pitch_um: int | None = None,
    through: bool = False,
    corner_strategy: str | None = None,
    requires_square_corners: bool = False,
    open_end_reliefs: tuple[str, ...] = (),
):
    return SimpleNamespace(
        feature_id=feature_id,
        part_id="namespace-panel",
        joint_id=None,
        kind=kind,
        face=face,
        origin=SimpleNamespace(x_um=x_um, y_um=y_um, z_um=z_um),
        dimensions=feature_dimensions or dimensions(diameter_um=8_000, depth_um=5_000),
        pattern_count=count,
        pitch_um=pitch_um,
        through=through,
        corner_strategy=corner_strategy,
        requires_square_corners=requires_square_corners,
        open_end_reliefs=open_end_reliefs,
        tolerance_um=0,
        fit_clearance_um=0,
    )


def test_domain_adapter_handles_adjustable_patterns_physical_sides_and_grain_axes() -> None:
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="adapter-adjustable",
            parameters=BookcaseParameters(shelf_mount=ShelfMount.ADJUSTABLE),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )

    adapted = adapt_design_result(design)
    features = [item for part in adapted.parts for item in part.features]

    assert any(item.kind == FeatureKind.DRILL_PATTERN for item in features)
    assert {item.side for item in features} <= {Side.A, Side.B}
    assert {part.grain_direction for part in adapted.parts} <= {"X", "Y", "NONE"}
    assert adapted.engine_version == design.engine_version

    duplicate = SimpleNamespace(
        design_hash=design.design_hash,
        engine_version=design.engine_version,
        template_version=design.template_version,
        parts=(design.parts[0], design.parts[0]),
    )
    with pytest.raises(ValueError, match="duplicate part_id"):
        adapt_design_result(duplicate)


def test_thin_domain_divider_is_blocked_when_opposing_dados_leave_two_mm_core() -> None:
    material = screening_mdf_6()
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="thin-opposing-wall",
            parameters=BookcaseParameters(
                nominal_thickness_um=6_000,
                actual_thickness_um=6_000,
                back_thickness_um=6_000,
                shelf_count=1,
                vertical_divider_count=1,
            ),
            material=material,
            back_material=material,
        )
    )
    adapted = adapt_design_result(design)
    stock = StockSheet(
        "thin-stock",
        material.material_id,
        material.version,
        2_440_000,
        1_220_000,
        6_000,
        quantity=4,
        grain_direction="NONE",
    )
    layout = DeterministicNester().nest(adapted.parts, stock)
    report = DFMValidator().validate(
        adapted.parts,
        layout,
        linuxcnc_reference_router_1325(),
    )

    issue = next(
        issue
        for issue in report.blocking_issues
        if issue.code == "OPPOSING_FEATURE_WALL_TOO_THIN"
    )
    divider = next(part for part in adapted.parts if part.name == "DIVIDER")
    assert issue.part_id == divider.part_id
    assert issue.inputs["remaining_um"] == 2_000


def test_domain_adapter_rejects_unknown_geometry_and_skips_noncutting_mark() -> None:
    unknown = namespace_part(features=(feature("unknown-feature", "laser_magic"),))
    with pytest.raises(ValueError, match="unsupported domain feature"):
        adapt_domain_part(unknown)

    missing_depth = namespace_part(
        features=(
            feature(
                "missing-depth",
                "drill",
                feature_dimensions=dimensions(diameter_um=8_000),
            ),
        )
    )
    with pytest.raises(ValueError, match="depth_um"):
        adapt_domain_part(missing_depth)

    marked = namespace_part(
        features=(feature("mark-feature", "mark", feature_dimensions=dimensions(width_um=1_000)),)
    )
    assert [item.kind for item in adapt_domain_part(marked).features] == [FeatureKind.OUTER_CONTOUR]

    no_thickness_axis = namespace_part(role="unknown")
    no_thickness_axis.finished_size = SimpleNamespace(
        width_um=300_000, depth_um=200_000, height_um=100_000
    )
    no_thickness_axis.raw_size = no_thickness_axis.finished_size
    with pytest.raises(ValueError, match="cannot infer"):
        adapt_domain_part(no_thickness_axis)

    thickness_grain = namespace_part(grain="z")
    with pytest.raises(ValueError, match="through panel thickness"):
        adapt_domain_part(thickness_grain)


@pytest.mark.cad
@pytest.mark.parametrize(
    "design_id",
    (
        "surface-back-authoritative-cad-a",
        "surface-back-authoritative-cad-b",
        "surface-back-authoritative-cad-c",
    ),
)
def test_surface_back_has_four_in_bounds_rabbets_and_authoritative_cad(
    design_id: str,
) -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id=design_id,
            parameters=BookcaseParameters(back_panel=BackPanelType.SURFACE_MOUNTED),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    back_part_ids = {
        part.part_id for part in design.parts if part.role.value == "back"
    }
    back_joints = tuple(
        joint
        for joint in design.joints
        if back_part_ids & {member.part_id for member in joint.members}
    )
    assert len(back_joints) == 4
    assert {joint.joint_type for joint in back_joints} == {JointType.RABBET}
    back_feature_ids = {
        feature_id
        for joint in back_joints
        for member in joint.members
        for feature_id in member.feature_ids
    }
    assert len(back_feature_ids) == 4

    adapted = adapt_design_result(design)
    adapted_by_id = {part.part_id: part for part in adapted.parts}
    rabbet_features = tuple(
        feature
        for part in adapted.parts
        for feature in part.features
        if feature.feature_id in back_feature_ids
    )
    assert len(rabbet_features) == 4
    for item in rabbet_features:
        bounds = item.machining_bounds()
        owner = adapted_by_id[item.part_id]
        assert bounds.x_um >= 0
        assert bounds.y_um >= 0
        assert bounds.x_um + bounds.width_um <= owner.width_um
        assert bounds.y_um + bounds.height_um <= owner.height_um

    # Fixed-shelf dogbones nearest the rear edge retain their complete cutter
    # radius inside each side panel; the surface-back RABBET does not act as an
    # implicit waiver for a breakthrough DADO relief.
    side_parts = tuple(
        part
        for part in design.parts
        if part.role.value in {"left_side", "right_side"}
    )
    rear_ended_dados = tuple(
        (part, feature, rabbet)
        for part in side_parts
        for rabbet in part.features
        if rabbet.kind.value == "rabbet"
        for feature in part.features
        if feature.kind.value == "groove"
        and feature.dimensions.radius_um is not None
        and feature.origin.y_um + int(feature.dimensions.width_um or 0)
        < rabbet.origin.y_um
    )
    assert rear_ended_dados
    assert all(
        feature.origin.y_um
        + int(feature.dimensions.width_um or 0)
        + int(feature.dimensions.radius_um or 0)
        <= rabbet.origin.y_um
        for _part, feature, rabbet in rear_ended_dados
    )

    artifacts = CadQueryAdapter().export_design(design)
    assert artifacts.authoritative is True
    assert artifacts.step.startswith(b"ISO-10303-21")
    assert artifacts.glb.startswith(b"glTF")


@pytest.mark.cad
def test_surface_back_dogbone_overlap_requires_complete_canonical_triangle() -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="surface-back-authoritative-cad",
            parameters=BookcaseParameters(back_panel=BackPanelType.SURFACE_MOUNTED),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    back_part_ids = {
        part.part_id for part in design.parts if part.role.value == "back"
    }
    removed_joint = next(
        joint
        for joint in design.joints
        if joint.joint_type == JointType.RABBET
        and back_part_ids & {member.part_id for member in joint.members}
    )
    incomplete = design.model_copy(
        update={
            "joints": tuple(
                joint for joint in design.joints if joint.joint_id != removed_joint.joint_id
            )
        }
    )

    with pytest.raises(CADExportError, match="does not intersect remaining material"):
        CadQueryAdapter().export_design(incomplete)


@pytest.mark.cad
def test_cadquery_applies_supported_cut_features_and_placement() -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
    import cadquery as cq

    features = (
        feature("drill", "drill", x_um=30_000, y_um=30_000),
        feature(
            "pattern",
            "drill_pattern",
            x_um=70_000,
            y_um=30_000,
            count=2,
            pitch_um=32_000,
        ),
        feature(
            "circle-pocket",
            "pocket",
            x_um=140_000,
            y_um=30_000,
            feature_dimensions=dimensions(diameter_um=15_000, depth_um=5_000),
        ),
        feature(
            "groove",
            "groove",
            x_um=20_000,
            y_um=100_000,
            feature_dimensions=dimensions(width_um=100_000, length_um=8_000, depth_um=6_000),
        ),
        feature(
            "rect-pocket",
            "pocket",
            x_um=160_000,
            y_um=100_000,
            feature_dimensions=dimensions(width_um=30_000, length_um=30_000, depth_um=5_000),
        ),
        feature("mark", "mark"),
    )
    part = namespace_part(features=features)
    part.placement.rotation_z_mdeg = 90_000
    part.placement.x_um = 10_000
    adapter = CadQueryAdapter()

    shape = adapter._part_shape(cq, part)
    placed = adapter._apply_placement(shape, part)

    assert shape.Volume() < 300 * 200 * 18
    assert placed.BoundingBox().xmax > 0


@pytest.mark.cad
def test_authoritative_cad_dado_members_have_no_remaining_solid_overlap() -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
    import cadquery as cq

    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="cad-dado-interlocks",
            parameters=BookcaseParameters(shelf_count=1, vertical_divider_count=1),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    adapter = CadQueryAdapter()
    crossing_part = next(part for part in design.parts if part.semantic_key == "left-side")
    with pytest.raises(CADExportError, match="does not intersect remaining material"):
        adapter._part_shape(cq, crossing_part)
    shapes = {
        part.part_id: adapter._apply_placement(adapter._part_shape(cq, part, design), part)
        for part in design.parts
    }
    dado_joints = [joint for joint in design.joints if joint.joint_type == JointType.DADO]

    assert dado_joints
    for joint in dado_joints:
        first, second = (shapes[member.part_id] for member in joint.members)
        assert first.intersect(second).Volume() == pytest.approx(0, abs=1e-7)
    for first_part, second_part in combinations(design.parts, 2):
        assert shapes[first_part.part_id].intersect(
            shapes[second_part.part_id]
        ).Volume() == pytest.approx(0, abs=1e-7)


@pytest.mark.cad
def test_exact_assembly_gate_blocks_injected_undeclared_collision_before_export(
    monkeypatch,
) -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
    import cadquery as cq

    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="assembly-collision-injection",
            template_id="shelving",
            parameters=BookcaseParameters(
                width_um=700_000,
                height_um=1_000_000,
                depth_um=300_000,
                shelf_count=2,
            ),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    shelves = sorted(
        (part for part in design.parts if part.semantic_key.startswith("shelf-")),
        key=lambda part: part.semantic_key,
    )
    assert len(shelves) == 2
    moved = shelves[1].model_copy(update={"placement": shelves[0].placement})
    injected = design.model_copy(
        update={
            "parts": tuple(
                moved if part.part_id == moved.part_id else part for part in design.parts
            )
        }
    )
    monkeypatch.setattr(
        cq.Assembly,
        "export",
        lambda *args, **kwargs: pytest.fail("collision gate must run before file export"),
    )

    with pytest.raises(CADAssemblyCollisionError) as blocked:
        CadQueryAdapter().export_design(injected)

    report = blocked.value.report
    collided_shelf_ids = tuple(sorted(part.part_id for part in shelves))
    collision = next(
        item for item in report.collisions if item.part_ids == collided_shelf_ids
    )
    first = shelves[0]
    expected_bounds = (
        first.placement.x_um / 1_000,
        first.placement.y_um / 1_000,
        first.placement.z_um / 1_000,
        (first.placement.x_um + first.finished_size.width_um) / 1_000,
        (first.placement.y_um + first.finished_size.depth_um) / 1_000,
        (first.placement.z_um + first.finished_size.height_um) / 1_000,
    )
    expected_volume = (
        first.finished_size.width_um
        * first.finished_size.depth_um
        * first.finished_size.height_um
        / 1_000**3
    )
    assert collision.reason == "UNDECLARED_PART_OVERLAP"
    assert collision.declared_joint_ids == ()
    assert collision.verified_joint_ids == ()
    assert collision.overlap_volume_mm3 == pytest.approx(expected_volume)
    assert collision.overlap_aabb_mm == pytest.approx(expected_bounds)
    serialised = report.to_json()
    assert serialised == json.dumps(
        report.as_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    assert (
        f'"part_ids":["{collision.part_ids[0]}","{collision.part_ids[1]}"]'
        in serialised
    )


@pytest.mark.cad
def test_exact_assembly_gate_does_not_treat_a_declared_joint_as_a_collision_waiver() -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")

    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="declared-joint-collision-injection",
            template_id="shelving",
            parameters=BookcaseParameters(
                width_um=700_000,
                height_um=1_000_000,
                depth_um=300_000,
                shelf_count=1,
            ),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    shelf = next(part for part in design.parts if part.semantic_key.startswith("shelf-"))
    left_side = next(part for part in design.parts if part.semantic_key == "left-side")
    shifted_placement = shelf.placement.model_copy(
        update={"x_um": shelf.placement.x_um - 2_000}
    )
    shifted_shelf = shelf.model_copy(update={"placement": shifted_placement})
    injected = design.model_copy(
        update={
            "parts": tuple(
                shifted_shelf if part.part_id == shelf.part_id else part
                for part in design.parts
            )
        }
    )

    with pytest.raises(CADAssemblyCollisionError) as blocked:
        CadQueryAdapter().validate_assembly(injected)

    pair = tuple(sorted((left_side.part_id, shelf.part_id)))
    collision = next(item for item in blocked.value.report.collisions if item.part_ids == pair)
    assert collision.reason == "VERIFIED_JOINT_GEOMETRY_MISMATCH"
    assert collision.declared_joint_ids
    assert collision.verified_joint_ids == collision.declared_joint_ids
    assert collision.overlap_volume_mm3 > 0


@pytest.mark.cad
@pytest.mark.parametrize(
    ("template_id", "expected_level"),
    (
        ("shelving", TemplateProductionLevel.SCREENED),
        ("wall-library", TemplateProductionLevel.CONCEPT),
        ("sideboard", TemplateProductionLevel.CONCEPT),
        ("room-divider", TemplateProductionLevel.CONCEPT),
        ("hanging-shelf", TemplateProductionLevel.CONCEPT),
        ("cupboard", TemplateProductionLevel.CONCEPT),
    ),
)
def test_exact_assembly_geometry_covers_all_templates_without_promoting_concepts(
    template_id: str,
    expected_level: TemplateProductionLevel,
) -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")

    capability = resolve_template_capability(template_id)
    parameters = (
        BookcaseParameters(
            width_um=1_200_000,
            height_um=1_600_000,
            depth_um=340_000,
            shelf_count=2,
            vertical_divider_count=1,
            base_cabinet_height_um=450_000,
            base_cabinet_depth_um=340_000,
            base_cabinet_count=2,
        )
        if capability.archetype == "wall_library"
        else BookcaseParameters(
            width_um=700_000,
            height_um=1_000_000,
            depth_um=300_000,
            shelf_count=2,
        )
    )
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id=f"collision-gate-{template_id}",
            template_id=template_id,
            parameters=parameters,
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )

    report = CadQueryAdapter().validate_assembly(design)

    assert report.passed is True
    assert report.collisions == ()
    assert report.checked_pair_count == len(design.parts) * (len(design.parts) - 1) // 2
    assert resolve_template_capability(template_id).production_level is expected_level


@pytest.mark.cad
def test_cadquery_materially_cuts_versioned_dogbone_reliefs() -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
    import cadquery as cq

    rectangle = feature(
        "rectangle",
        "groove",
        x_um=40_000,
        y_um=50_000,
        feature_dimensions=dimensions(
            width_um=20_000,
            length_um=80_000,
            depth_um=6_000,
            radius_um=3_000,
        ),
    )
    dogbone = feature(
        "dogbone",
        "groove",
        x_um=40_000,
        y_um=50_000,
        feature_dimensions=dimensions(
            width_um=20_000,
            length_um=80_000,
            depth_um=6_000,
            radius_um=3_000,
        ),
        corner_strategy="dogbone-v1",
        requires_square_corners=True,
    )
    adapter = CadQueryAdapter()
    rectangular_shape = adapter._part_shape(cq, namespace_part(features=(rectangle,)))
    dogbone_shape = adapter._part_shape(cq, namespace_part(features=(dogbone,)))

    assert dogbone_shape.Volume() < rectangular_shape.Volume()


@pytest.mark.cad
def test_cadquery_blocks_noop_split_and_edge_near_cutters() -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
    import cadquery as cq

    duplicate = feature("duplicate", "drill", x_um=30_000, y_um=30_000)
    with pytest.raises(CADExportError, match="does not intersect remaining material"):
        CadQueryAdapter()._part_shape(
            cq,
            namespace_part(
                features=(
                    feature("first", "drill", x_um=30_000, y_um=30_000),
                    duplicate,
                )
            ),
        )

    splitter = feature(
        "splitter",
        "groove",
        x_um=0,
        y_um=95_000,
        through=True,
        feature_dimensions=dimensions(width_um=300_000, length_um=10_000, depth_um=18_000),
    )
    with pytest.raises(CADExportError, match="one connected solid"):
        CadQueryAdapter()._part_shape(cq, namespace_part(features=(splitter,)))

    edge_near = feature(
        "edge-near",
        "groove",
        x_um=2_000,
        y_um=50_000,
        feature_dimensions=dimensions(
            width_um=20_000,
            length_um=80_000,
            depth_um=6_000,
            radius_um=3_000,
        ),
        corner_strategy="dogbone-v1",
    )
    with pytest.raises(CADExportError, match="cutter envelope extends beyond"):
        CadQueryAdapter()._part_shape(cq, namespace_part(features=(edge_near,)))


@pytest.mark.cad
def test_cadquery_accepts_only_exact_declared_open_end_relief() -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
    import cadquery as cq

    declared_but_offset = feature(
        "offset-open-end",
        "groove",
        x_um=2_000,
        y_um=50_000,
        feature_dimensions=dimensions(
            width_um=20_000,
            length_um=80_000,
            depth_um=6_000,
            radius_um=3_000,
        ),
        corner_strategy="dogbone-v1",
        open_end_reliefs=("u_min",),
    )
    with pytest.raises(CADExportError, match="not exactly edge-flush"):
        CadQueryAdapter()._part_shape(cq, namespace_part(features=(declared_but_offset,)))

    exact_open_end = feature(
        "exact-open-end",
        "groove",
        x_um=0,
        y_um=50_000,
        feature_dimensions=dimensions(
            width_um=20_000,
            length_um=80_000,
            depth_um=6_000,
            radius_um=3_000,
        ),
        corner_strategy="dogbone-v1",
        open_end_reliefs=("u_min",),
    )
    result = CadQueryAdapter()._part_shape(cq, namespace_part(features=(exact_open_end,)))
    assert result.isValid()
    assert result.Volume() > 0


@pytest.mark.cad
@pytest.mark.parametrize(
    ("bad_feature", "message"),
    (
        (feature("tenon", "tenon"), "not implemented"),
        (feature("counter", "countersink"), "not versioned"),
        (feature("unknown", "magic"), "unsupported authoritative"),
        (
            feature(
                "rect-missing",
                "groove",
                feature_dimensions=dimensions(width_um=10_000, depth_um=5_000),
            ),
            "no width/length",
        ),
        (feature("bad-face", "drill", face="diagonal"), "unsupported feature face"),
        (
            feature(
                "missing-corner-strategy",
                "groove",
                feature_dimensions=dimensions(width_um=20_000, length_um=20_000, depth_um=5_000),
                requires_square_corners=True,
            ),
            "requires an explicit",
        ),
        (
            feature(
                "unknown-corner-strategy",
                "groove",
                feature_dimensions=dimensions(
                    width_um=20_000,
                    length_um=20_000,
                    depth_um=5_000,
                    radius_um=3_000,
                ),
                corner_strategy="magic-v1",
            ),
            "unsupported internal-corner strategy",
        ),
        (
            feature(
                "missing-relief-radius",
                "groove",
                feature_dimensions=dimensions(width_um=20_000, length_um=20_000, depth_um=5_000),
                corner_strategy="dogbone-v1",
            ),
            "no relief radius",
        ),
    ),
)
def test_cadquery_blocks_unversioned_or_unsupported_features(bad_feature, message: str) -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
    import cadquery as cq

    with pytest.raises(UnsupportedCADFeatureError, match=message):
        CadQueryAdapter()._part_shape(cq, namespace_part(features=(bad_feature,)))


@pytest.mark.cad
def test_cadquery_dependency_empty_invalid_and_atomic_export_failures(monkeypatch) -> None:
    adapter = CadQueryAdapter()
    monkeypatch.setattr(adapter, "available", lambda: False)
    with pytest.raises(CADDependencyUnavailable):
        adapter.export_design(object())
    monkeypatch.undo()
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")

    with pytest.raises(CADExportError, match="empty design"):
        CadQueryAdapter().export_design(SimpleNamespace(design_hash="a" * 64, parts=()))

    invalid = namespace_part()
    invalid.finished_size = SimpleNamespace(width_um=0, depth_um=1, height_um=1)
    with pytest.raises(CADExportError, match="invalid dimensions"):
        CadQueryAdapter().export_design(SimpleNamespace(design_hash="b" * 64, parts=(invalid,)))

    import cadquery as cq

    monkeypatch.setattr(
        cq.Assembly, "export", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(CADExportError, match="atomically"):
        CadQueryAdapter().export_design(
            SimpleNamespace(design_hash="c" * 64, parts=(namespace_part(),))
        )

    assert _normalise_step(b"\xff\r\n") == "ÿ\n".encode()
