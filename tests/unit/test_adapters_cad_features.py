from __future__ import annotations

from itertools import combinations
from types import SimpleNamespace

import pytest
from custombuild_cad import (
    CADDependencyUnavailable,
    CADExportError,
    CadQueryAdapter,
    UnsupportedCADFeatureError,
)
from custombuild_cad.adapter import _normalise_step
from custombuild_domain import (
    BookcaseDesignSpec,
    BookcaseParameters,
    JointType,
    ShelfMount,
    build_bookcase,
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
    shapes = {
        part.part_id: adapter._apply_placement(adapter._part_shape(cq, part), part)
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
        cq.Assembly, "save", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(CADExportError, match="atomically"):
        CadQueryAdapter().export_design(
            SimpleNamespace(design_hash="c" * 64, parts=(namespace_part(),))
        )

    assert _normalise_step(b"\xff\r\n") == "ÿ\n".encode()
