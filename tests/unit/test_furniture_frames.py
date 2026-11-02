from __future__ import annotations

import csv
import io
import json
import zipfile
from math import cos, radians, sin

import pytest
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import FurnitureResult, build_furniture
from custombuild_manufacturing.adapters import adapt_design_result
from custombuild_manufacturing.furniture_faces import drawing_faces
from custombuild_manufacturing.furniture_profiles import preview_furniture
from custombuild_worker.furniture_review import build_furniture_review


def workspace(family, **parameters):
    design = {"intent": {"family": family, **parameters}}
    if family == "chest_of_drawers":
        design["hardware"] = {"catalog_id": "hettich-ka4532-9114275"}
    return FurnitureWorkspace.model_validate({"design": design})


def world_centre(part, feature):
    p = feature.origin
    a = radians(part.placement.rotation_x_mdeg / 1000)
    return (
        part.placement.x_um + p.x_um,
        part.placement.y_um + p.y_um * cos(a) - p.z_um * sin(a),
        part.placement.z_um + p.y_um * sin(a) + p.z_um * cos(a),
    )


@pytest.mark.parametrize("family,count", [("chair", 10), ("dining_table", 9)])
def test_every_screw_binds_two_coaxial_real_bores_and_each_hole_occurs_in_one_face(family, count):
    result = build_furniture(workspace(family).design)
    assert len(result.parts) == count
    parts = {p.part_id: p for p in result.parts}
    adapted = adapt_design_result(result)
    for item in adapted.parts:
        part = parts[item.part_id]
        seen = [
            f.feature_id
            for view in drawing_faces(part, item)
            for f in view.part.features
            if f.side == view.side and f.feature_id in {a.feature_id for a in part.features}
        ]
        assert sorted(seen) == sorted(f.feature_id for f in part.features)
    for joint in result.joints:
        a, b = joint.members
        pa, pb = parts[a.part_id], parts[b.part_id]
        fa = next(f for f in pa.features if f.feature_id == a.feature_ids[0])
        fb = next(f for f in pb.features if f.feature_id == b.feature_ids[0])
        assert fa.through and not fb.through and fa.joint_id == fb.joint_id == joint.joint_id
        va, vb = world_centre(pa, fa), world_centre(pb, fb)
        axis = {"+x": 0, "-x": 0, "+y": 1, "-y": 1, "+z": 2, "-z": 2}[joint.assembly_direction]
        assert vb[axis] - va[axis] == pytest.approx(
            fa.dimensions.depth_um * (1 if joint.assembly_direction.startswith("+") else -1), abs=1
        )
        assert all(abs(vb[i] - va[i]) <= 1 for i in range(3) if i != axis)


def test_parameter_changes_move_the_connected_parts():
    def parts(family, **patch):
        return {p.semantic_key: p for p in build_furniture(workspace(family, **patch).design).parts}

    before, after = parts("dining_table"), parts("dining_table", width_um=2_000_000)
    assert (
        after["rail-front"].finished_size.width_um - before["rail-front"].finished_size.width_um
        == 200_000
    )
    before, after = parts("chair"), parts("chair", seat_depth_um=400_000, back_angle_mdeg=5_000)
    assert after["leg-front-left"].placement == before["leg-front-left"].placement
    assert after["rail-left"].finished_size == before["rail-left"].finished_size
    assert after["chair-seat"].finished_size.depth_um == 400_000
    assert after["rail-left"].features != before["rail-left"].features
    assert after["chair-back"].placement.rotation_x_mdeg == -5_000
    assert after["leg-rear-left"].features != before["leg-rear-left"].features


def test_frame_surfaces_have_flush_fasteners_and_family_specific_screening():
    for family, top in (("chair", "chair-seat"), ("dining_table", "table-top")):
        w = workspace(family)
        r = build_furniture(w.design)
        surface = next(p for p in r.parts if p.semantic_key == top)
        assert (
            sum(
                f.dimensions.diameter_um == 11_000
                and f.dimensions.depth_um == 4_000
                and not f.through
                for f in surface.features
            )
            == 4
        )
    rules = preview_furniture(workspace("chair"))["rules"]["evaluations"]
    tipping = next(r for r in rules if r["rule_id"] == "CB-CHAIR-REAR-TIP-001")
    assert tipping["values"]["estimated_force_n"] > 0
    round_rules = preview_furniture(
        workspace(
            "dining_table",
            top_shape="round",
            depth_um=1_200_000,
            width_um=1_200_000,
        )
    )["rules"]["evaluations"]
    assert any(
        r["rule_id"] == "CB-TABLE-ROUND-TOP-001" and r["status"] == "BLOCK" for r in round_rules
    )


def test_drawer_columns_cut_selected_reveals_and_use_the_documented_runner():
    w = workspace(
        "chest_of_drawers",
        width_um=1_000_000,
        drawer_count=4,
        drawer_columns=2,
        gap_basis="clear_reveal",
        row_front_heights_um=[250_000],
    )
    result = build_furniture(w.design)
    parts = {p.semantic_key: p for p in result.parts}
    assert len(parts) == 31
    first, third = parts["drawer-1-front"], parts["drawer-3-front"]
    assert first.finished_size.height_um == 250_000
    assert third.placement.z_um - first.placement.z_um - first.finished_size.height_um == 3_000
    assert len(result.moving_groups) == 4
    assert result.moving_groups[0].requested_load_n == 100
    assert result.moving_groups[0].rated_pair_load_kg == 35
    for sku, drawer_holes, cabinet_last in (
        ("9114273", {37_000, 257_000}, 229_000),
        ("9114275", {37_000, 165_000, 331_000}, 261_000),
    ):
        doc = w.model_dump(mode="json")
        doc["design"]["hardware"]["catalog_id"] = "hettich-ka4532-" + sku
        r = build_furniture(FurnitureWorkspace.model_validate(doc).design)
        side = next(p for p in r.parts if p.semantic_key == "drawer-1-left")
        assert {f.origin.y_um for f in side.features if f.joint_id is None} == drawer_holes
        divider = next(p for p in r.parts if p.role.value == "divider")
        bores = [f for f in divider.features if f.feature_id.startswith("run_")]
        assert len(bores) == 12 and {f.origin.y_um - 18_000 for f in bores} == {
            37_000,
            165_000,
            cabinet_last,
        }
        left = {f.origin.z_um for f in bores if f.face.value == "left"}
        right = {f.origin.z_um for f in bores if f.face.value == "right"}
        assert min(abs(a - b) for a in left for b in right) >= 12_000
    assert preview_furniture(w)["physical_cutting_authorized"] is False


@pytest.mark.cad
@pytest.mark.parametrize(
    "family,patch",
    [
        ("dining_table", {"width_um": 1_200_001, "depth_um": 1_200_001, "top_shape": "round"}),
        ("chair", {"depth_um": 562_037, "seat_depth_um": 414_521, "back_angle_mdeg": 7000}),
        ("chest_of_drawers", {"drawer_count": 4, "drawer_columns": 2}),
    ],
)
def test_actual_package_includes_assembly_and_all_physical_faces(family, patch, tmp_path):
    cq = pytest.importorskip("cadquery")
    w = workspace(family, **patch)
    result = build_furniture(w.design)
    with zipfile.ZipFile(io.BytesIO(build_furniture_review(w))) as z:
        manifest = json.loads(z.read("manifest.json"))
        assert (
            not manifest["production_qualified"] and manifest["design_hash"] == result.design_hash
        )
        step = tmp_path / "assembly.step"
        step.write_bytes(z.read("design/model.step"))
        bounds = cq.importers.importStep(str(step)).val().BoundingBox()
        intent = w.design.intent
        assert (bounds.xlen, bounds.ylen, bounds.zlen) == pytest.approx(
            (intent.width_um / 1000, intent.depth_um / 1000, intent.height_um / 1000), abs=0.002
        )
        assert z.read("documents/assembly.pdf").startswith(b"%PDF")
        assert len(
            list(csv.DictReader(io.StringIO(z.read("assembly/parts.csv").decode("utf-8-sig"))))
        ) == len(result.parts)
        assert len(
            list(
                csv.DictReader(io.StringIO(z.read("assembly/connections.csv").decode("utf-8-sig")))
            )
        ) == len(result.joints)
        for p in result.parts:
            for f in p.features:
                face = (
                    "A"
                    if f.face == p.a_side
                    else "B"
                    if f.face == p.b_side
                    else f.face.value.upper()
                )
                assert f.feature_id.encode() in z.read(f"parts/{p.part_id}/{face}.dxf")
        if family == "dining_table":
            top = next(p for p in result.parts if p.semantic_key == "table-top")
            data = z.read(f"parts/{top.part_id}/A.svg").decode()
            assert '<circle class="outline"' in data and 'r="600.0005"' in data
            assert b"600.0005" in z.read(f"parts/{top.part_id}/A.dxf")


def test_chair_envelope_and_seat_are_independent_physical_dimensions():
    for depth, seat in ((562_037, 414_521), (591_117, 427_117)):
        design = workspace(
            "chair",
            width_um=488_505,
            depth_um=depth,
            height_um=929_219,
            seat_depth_um=seat,
            leg_section_um=65_001,
            back_angle_mdeg=14_333,
        )
        parts = {p.semantic_key: p for p in build_furniture(design.design).parts}
        front = parts["chair-seat"]
        back = parts["leg-rear-right"]
        assert front.placement.y_um == 0
        assert back.placement.y_um + back.finished_size.depth_um == depth
        assert front.finished_size.depth_um == seat
        detached = build_furniture(design.design).model_dump(mode="json")
        detached["spec"]["intent"]["depth_um"] += 1_000
        with pytest.raises(ValueError, match="verkliga yttermått"):
            FurnitureResult.model_validate(detached)


@pytest.mark.parametrize("depth", [390_000, 391_000, 619_219])
def test_compact_table_recesses_leave_material_between_screw_heads(depth):
    table = workspace(
        "dining_table",
        width_um=722_000,
        depth_um=depth,
        height_um=437_001,
        end_inset_um=90_000,
        leg_section_um=55_000,
        stretcher_height_um=65_000,
    )
    top = next(p for p in build_furniture(table.design).parts if p.semantic_key == "table-top")
    bores = [f for f in top.features if f.dimensions.diameter_um == 11_000]
    assert len(bores) == 4
    for i, a in enumerate(bores):
        for b in bores[i + 1 :]:
            dx, dy = a.origin.x_um - b.origin.x_um, a.origin.y_um - b.origin.y_um
            assert dx**2 + dy**2 >= 16_000**2


def test_hettich_450_drawer_member_has_all_three_documented_holes():
    w = workspace("chest_of_drawers", width_um=1_111_731, drawer_count=6, drawer_columns=2)
    r = build_furniture(w.design)
    for n in range(1, 7):
        for end in ("left", "right"):
            part = next(p for p in r.parts if p.semantic_key == f"drawer-{n}-{end}")
            assert sorted(f.origin.y_um for f in part.features if f.joint_id is None) == [
                37_000,
                165_000,
                331_000,
            ]
