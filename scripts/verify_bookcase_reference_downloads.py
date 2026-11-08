"""Independent acceptance measurements for the two full-size customer downloads.

Run with the original ZIP followed by its amended revision. All geometry is in mm.
This executable fixture imports only file readers, independent of Custombuild.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import cadquery as cq
import ezdxf
from pypdf import PdfReader

# Local U, V, stock normal and inward sign in assembled XYZ.
FACE_AXES = {
    "left": (1, 2, 0, 1),
    "right": (1, 2, 0, -1),
    "front": (0, 2, 1, 1),
    "back": (0, 2, 1, -1),
    "bottom": (0, 1, 2, 1),
    "top": (0, 1, 2, -1),
}


def reference_parts(width, height):
    """Derive all 57 stock envelopes and placements from reference dimensions."""
    parts = {
        "left-side": ((0, 0, 0), (18, 260, height)),
        "right-side": ((width - 18, 0, 0), (18, 260, height)),
        "bottom": ((12, 0, 0), (width - 24, 260, 18)),
        "top": ((12, 0, height - 18), (width - 24, 260, 18)),
    }
    # Six bays share the available width after seven 18 mm uprights.
    # Remainder micrometres are distributed left to right.
    quotient, remainder = divmod(width * 1000 - 7 * 18_000, 6)
    left = 18.0
    for bay in range(6):
        opening = (quotient + (bay < remainder)) / 1000
        right = left + opening
        if bay < 5:
            parts[f"divider-{bay}"] = ((right, 0, 12), (18, 248, height - 24))
        # Keep 0.05 mm from each intermediate divider datum on a floating back.
        inset_left = 0.05 if bay else 0
        inset_right = 0.05 if bay < 5 else 0
        parts[f"back-b{bay}"] = (
            (left - 6 + inset_left, 242, 12),
            (opening + 12 - inset_left - inset_right, 6, height - 24),
        )
        for row in range(7):
            shelf_z = (row + 1) * (height - 18) / 8
            parts[f"shelf-r{row}-b{bay}"] = (
                (left - 6, 0, shelf_z),
                (opening + 12, 237.65, 18),
            )
        left = right + 18
    return parts


def bounds(solid):
    box = solid.BoundingBox()
    return tuple(
        tuple(round(getattr(box, axis + field), 6) for axis in "xyz") for field in ("min", "len")
    )


def rows(archive, path):
    return list(csv.DictReader(StringIO(archive.read(path).decode("utf-8-sig"))))


def read_step(archive, root, name):
    path = root / name
    path.write_bytes(archive.read(f"design/{name}"))
    return cq.Assembly.load(str(path))


def point_on_face(origin, size, face, u, v, depth):
    along_u, along_v, normal, sign = FACE_AXES[face]
    point = list(origin)
    point[along_u] += u
    point[along_v] += v
    point[normal] += (size[normal] if sign < 0 else 0) + sign * depth
    return tuple(round(coordinate, 6) for coordinate in point)


def inspect_face(archive, panel, solid, face, label, pair_by_id, counts, design_hash):
    part_id = panel["part_id"]
    dxf_bytes = archive.read(f"parts/{part_id}/{label}.dxf")
    drawing = ezdxf.read(StringIO(dxf_bytes.decode()))
    assert drawing.units == 4 and design_hash.encode() in dxf_bytes, (part_id, label)
    circles = [e for e in drawing.modelspace().query("CIRCLE") if e.dxf.layer == "DRILL"]
    grooves = [e for e in drawing.modelspace().query("LWPOLYLINE") if e.dxf.layer == "GROOVE"]
    side_names = {"a": panel["a_side"], "b": panel["b_side"]}
    features = [f for f in panel["features"] if side_names.get(f["face"], f["face"]) == face]
    assert len(circles) == sum(f["kind"] == "drill" for f in features)
    assert len(grooves) == sum(f["kind"] == "groove" for f in features)
    origin, size = bounds(solid)
    u_axis, v_axis, _, _ = FACE_AXES[face]
    counts["faces"] += 1

    def inside(u, v, depth):
        return solid.isInside(point_on_face(origin, size, face, u, v, depth), 1e-6)

    for feature in features:
        coordinates = [feature["origin"][axis + "_um"] / 1000 for axis in "xyz"]
        u, v = coordinates[u_axis], coordinates[v_axis]
        dims = feature["dimensions"]
        depth = dims["depth_um"] / 1000
        if feature["kind"] == "drill":
            assert depth == 12.5 and dims["diameter_um"] == 3500
            assert any(
                abs(e.dxf.center[0] - u) < 1e-7
                and abs(e.dxf.center[1] - v) < 1e-7
                and abs(e.dxf.radius - 1.75) < 1e-7
                for e in circles
            ), feature["feature_id"]
            assert not inside(u, v, 12.48) and inside(u, v, 12.52)
            assert not inside(u + 1.73, v, 6) and inside(u + 1.77, v, 6)
            row = pair_by_id[feature["feature_id"]]
            assert (row["part_id"], row["physical_face"], row["side"]) == (part_id, face, label)
            assert tuple(float(row[k]) for k in ("U_mm", "V_mm", "depth_mm", "diameter_mm")) == (
                u,
                v,
                12.5,
                3.5,
            )
            centre = point_on_face(origin, size, face, u, v, 0)
            assert centre == tuple(float(row[f"world_{axis}_mm"]) for axis in "XYZ")
            counts["pilots"] += 1
            continue
        assert feature["kind"] == "groove"
        width, length = dims["width_um"] / 1000, dims["length_um"] / 1000
        polygon = [(u, v), (u + width, v), (u + width, v + length), (u, v + length)]
        assert any(
            e.closed
            and [(round(x, 6), round(y, 6)) for x, y in e.get_points("xy")]
            == [(round(x, 6), round(y, 6)) for x, y in polygon]
            for e in grooves
        ), feature["feature_id"]
        assert any(
            not inside(u + width * x, v + length * y, depth - 0.02)
            and inside(u + width * x, v + length * y, depth + 0.02)
            for x in (0.5, 0.35, 0.65)
            for y in (0.5, 0.35, 0.65)
        ), feature["feature_id"]
        counts["grooves"] += 1
        if feature.get("front_stop_clearance_um"):
            assert feature["front_stop_clearance_um"] == 350
            end = (u + width, v + length / 2) if u_axis == 1 else (u + width / 2, v + length)
            du, dv = (0.02, 0) if u_axis == 1 else (0, 0.02)
            assert not inside(end[0] - du, end[1] - dv, depth / 2)
            assert inside(end[0] + du, end[1] + dv, depth / 2), feature["feature_id"]
            counts["stops"] += 1


def inspect_files(path, revision):
    with ZipFile(path) as archive, TemporaryDirectory() as directory:

        def document(name):
            return json.loads(archive.read(name))

        root = Path(directory)
        order = document("design/workspace.json")["design"]
        manifest = document("manifest.json")
        design_hash = manifest["design_hash"]
        assert order["revision"] == manifest["revision"] == revision
        assert manifest["schema_version"] == "custombuild.shelving-workshop-review.v16"
        assert manifest["production_qualified"] is False
        intent = order["intent"]
        assert (
            intent["divider_count"],
            intent["shelf_count"],
            intent["shelf_load_per_metre_n"],
        ) == (5, 7, 294)
        site = document("assembly/required-space.json")["standing_site"]
        installation = order["installation"]
        assert [installation[f"{k}_um"] for k in ("width", "height", "depth")] == [
            4340000,
            2540000,
            280000,
        ]
        assert tuple(installation["trim_profile"][f"{k}_um"] for k in ("height", "width")) == (
            90000,
            20000,
        )
        margins = [-6000, -6000, 0] if revision == 1 else [4000, 4000, 0]
        assert [w["margin_um"] for w in site["walls"]] == margins
        assert [w["measured_margin_um"] for w in site["walls"]] == [None] * 3
        assert [w["measured_distance_um"] for w in site["walls"]] == [None] * 3
        assert site["state"] == ("blocked" if revision == 1 else "requires_measurement")
        for field in ("left_wall", "right_wall", "rear_wall", "front_space", "ceiling_height"):
            assert installation.get(f"measured_{field}_um") is None
        manual = PdfReader(BytesIO(archive.read("documents/assembly.pdf")))
        place = manual.pages[1].extract_text()
        assert (
            f"Marginal inom kundens angivna höjdmått: {(-18 if revision == 1 else 22)} mm" in place
        )
        assert "Uppmätt takhöjd: uppgift saknas" in place
        assert "Uppmätt fritt utrymme framför stommen: uppgift saknas" in place
        physical = document("inspection/kvalificeringsprov.json")
        assert physical["type_test_results"] is None and physical["design_hash"] == design_hash
        limits = physical["order_requirements"]
        assert limits["joint_shear_n"] == (10784 if revision == 1 else 10720)
        assert limits["row_payload_n"] == (1265 if revision == 1 else 1259)
        assert (
            limits["maximum_shelf_deflection_um"] == 3000
            and limits["minimum_tip_factor_permille"] == 1500
        )
        assert limits["assembly_steps"] == list(range(1, 57))
        timber = read_step(archive, root, "model.step")
        mounted = read_step(archive, root, "with-mounted-angles.step")
        assert len(timber.toCompound().Solids()) == 57 and len(mounted.toCompound().Solids()) == 155
        reference = reference_parts(4300, 2540) if revision == 1 else reference_parts(4280, 2500)
        panels = {p["semantic_key"]: p for p in document("design/resolved.json")["parts"]}
        assert panels.keys() == reference.keys()
        ids = {p["part_id"] for p in panels.values()}
        bom_rows = rows(archive, "bom/parts.csv")
        cut_rows = rows(archive, "bom/cut-list.csv")
        assert len(bom_rows) == len(cut_rows) == 57
        bom = {r["part_id"]: r for r in bom_rows}
        cutlist = {r["part_id"]: r for r in cut_rows}
        assert len(ids) == len(reference) == 57 and bom.keys() == cutlist.keys() == ids
        pairs = rows(archive, "inspection/fogprov.csv")
        pair_by_id = {row["pilot_id"]: row for row in pairs}
        assert len(pair_by_id) == len(pairs) == 196
        joint_counts = Counter(row["joint_id"] for row in pairs)
        assert len(joint_counts) == 98 and set(joint_counts.values()) == {2}
        assert all(
            row["design_hash"] == design_hash
            and int(row["revision"]) == revision
            and not row["inspection_result"]
            and not any(v for k, v in row.items() if k.startswith("measured_"))
            for row in pairs
        )
        counts = Counter()
        for key, (origin, size) in reference.items():
            panel = panels[key]
            part_id = panel["part_id"]
            solid = timber.objects[part_id].toCompound().Solids()[0]
            assert bounds(solid) == tuple(
                tuple(round(c, 6) for c in triple) for triple in (origin, size)
            ), key
            if key.startswith("back-"):
                faces = ("front", "back")
                stock = (size[0], size[2], size[1])
                material = "birch-plywood-6"
            elif key.endswith("-side") or key.startswith("divider-"):
                faces = ("left", "right")
                stock = (size[1], size[2], size[0])
                material = "solid-walnut"
            else:
                faces = ("bottom", "top")
                stock = (size[0], size[1], size[2])
                material = "solid-walnut"
            assert (panel["a_side"], panel["b_side"]) == faces
            assert bom[part_id]["quantity"] == "1"
            for row in (cutlist[part_id], bom[part_id]):
                assert row["material_id"] == material
                assert tuple(
                    float(row[field]) for field in ("finished_width_mm", "finished_height_mm")
                ) == tuple(round(c, 6) for c in stock[:2])
                assert float(row["thickness_mm"]) == stock[2]
            for face, label in zip(faces, ("A", "B"), strict=True):
                inspect_face(archive, panel, solid, face, label, pair_by_id, counts, design_hash)
        assert counts == {"faces": 114, "pilots": 196, "grooves": 122, "stops": 94}
        hardware = {r["item"]: int(r["quantity"]) for r in rows(archive, "bom/hardware.csv")}
        assert hardware == {"GN-970-ALM-30-30-30-B": 98, "DIN-96-A4-5x16": 196}
        angle = next(
            a
            for a in document("assembly/trial-angles.json")["angles"]
            if a["upright_id"] == panels["left-side"]["part_id"]
            and a["horizontal_id"] == panels["bottom"]["part_id"]
        )
        bracket = mounted.objects["angle-" + angle["joint_id"]].toCompound().Solids()[0]
        # Catalogue bores have a 3.1 mm radius on both legs of the mounting angle.
        assert not bracket.isInside((20, 23.08, 36), 1e-6) and bracket.isInside(
            (20, 23.12, 36), 1e-6
        )
        assert not bracket.isInside((36, 23.08, 20), 1e-6) and bracket.isInside(
            (36, 23.12, 20), 1e-6
        )
        joint_rows = [r for r in pairs if r["joint_id"] == angle["joint_id"]]
        assert {tuple(float(r[f"world_{a}_mm"]) for a in "XYZ") for r in joint_rows} == {
            (18, 20, 36),
            (36, 20, 18),
        }
        steps = document("assembly/graph.json")["graph"]["steps"]
        assert len(steps) == 56
        for name in ("assembly", "part-drawings"):
            pdf = PdfReader(BytesIO(archive.read(f"documents/{name}.pdf")))
            sheets = [p.extract_text() for p in pdf.pages]
            text = "\n".join(sheets)
            if name == "part-drawings":
                for key, panel in panels.items():
                    upright = key.endswith("-side") or key.startswith(("divider-", "back-"))
                    label = (
                        "Fiber längs V (stomaxel Z)" if upright else "Fiber längs U (stomaxel X)"
                    )
                    drawings = [
                        sheet for sheet in sheets if f"DXF: parts/{panel['part_id']}/" in sheet
                    ]
                    assert drawings and all(label in sheet for sheet in drawings), key
            assert all(part in text for part in ids) and design_hash in text
            if name == "assembly":
                assert all(s["step_id"] in text for s in steps)
        return {
            "file": path.name,
            "project": order["design_id"],
            "revision": revision,
            "design_hash": design_hash,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "site": site["state"],
            "timber": len(ids),
            "mounted": 155,
            "steps": len(steps),
            "joint": angle["joint_id"],
            **counts,
        }


def main():
    if not __debug__:
        raise RuntimeError("Run the reference checks with Python assertions enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path)
    parser.add_argument("revised", type=Path)
    args = parser.parse_args()
    original = inspect_files(args.original, 1)
    revised = inspect_files(args.revised, 2)
    assert (
        original["project"] == revised["project"]
        and original["design_hash"] != revised["design_hash"]
    )
    for receipt in (original, revised):
        print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
