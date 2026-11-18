"""Order-bound validation of the actual deliverables before the worker may publish a ZIP."""

import csv
import io
import json
import re
from typing import Any

from custombuild_cad.bookcase_readback import (
    PLANES,
    panel_cut,
    physical_face,
    size,
    verify_bookcase_steps,
)
from custombuild_domain.enums import FeatureKind, GrainDirection, PartRole
from custombuild_domain.furniture import ShelvingIntent
from custombuild_domain.furniture_catalog import resolve_material
from custombuild_domain.furniture_engine import FurnitureResult
from custombuild_manufacturing.bookcase_qualification import READBACK_SCHEMA
from pypdf import PdfReader

from .bookcase_drawings import mm, verify_panel_dxf
from .bookcase_joint_sheet import joint_measurement_sheet


def order_identity(result: FurnitureResult) -> dict[str, object]:
    return {
        "design_id": str(result.spec.design_id),
        "revision": result.spec.revision,
        "design_hash": result.design_hash,
    }


def verify_panels_from_order(result: FurnitureResult) -> None:
    spec = result.spec
    i = spec.intent
    assert isinstance(i, ShelvingIntent)
    w, h, d = i.width_um, i.height_um, i.depth_um
    t, back = spec.material.measured_thickness_um, spec.back_material.measured_thickness_um
    if not t or not back:
        raise ValueError("Ange uppmätt tjocklek för stomme och rygg.")
    bite = t // 3
    divider_bite = min(bite, (t - 6100) // 2)
    bays = i.divider_count + 1
    ratios = i.bay_width_ratios_ppm or (1,) * bays
    total = w - (bays + 1) * t
    shares = [divmod(total * weight, sum(ratios)) for weight in ratios]
    widths = [q for q, _ in shares]
    for b in sorted(range(bays), key=lambda b: -shares[b][1])[: total - sum(widths)]:
        widths[b] += 1
    deck_top = 700_000 if i.base_doors else t
    clear_height = h - t - deck_top
    heights = i.shelf_height_ratios_ppm
    if heights:
        rows = [deck_top + (clear_height * r + 500000) // 1000000 - t // 2 for r in heights]
    else:
        opening, extra = divmod(clear_height - i.shelf_count * t, i.shelf_count + 1)
        rows = [
            deck_top + r * t + (r + 1) * opening + min(r + 1, extra) for r in range(i.shelf_count)
        ]
    inside = clear_height + 2 * bite
    shelf_depth = d - back - (16_350 if i.trial_angle else 16_000)
    # Origin and finished X/Y/Z of each blank, calculated directly from the order.
    wanted = {
        "left-side": (0, 0, 0, t, d, h),
        "right-side": (w - t, 0, 0, t, d, h),
        "bottom": (t - bite, 0, deck_top - t, w - 2 * (t - bite), d, t),
        "top": (t - bite, 0, h - t, w - 2 * (t - bite), d, t),
    }
    x = t
    for bay, clear in enumerate(widths):
        for row, z in enumerate(rows):
            wanted[f"shelf-r{row}-b{bay}"] = (x - bite, 0, z, clear + 2 * bite, shelf_depth, t)
        left = bite if bay == 0 else divider_bite
        right = bite if bay == bays - 1 else divider_bite
        name = "back" if bays == 1 else f"back-b{bay}"
        wanted[name] = (
            x - left,
            d - back - 12000,
            deck_top - bite,
            clear + left + right,
            back,
            inside,
        )
        if i.base_doors:
            wanted[f"cabinet-front-{bay}"] = (
                x + 2000,
                0,
                t + 2000,
                clear - 4000,
                t,
                deck_top - 2 * t - 4000,
            )
            wanted[f"base-bottom-{bay}"] = (x - bite, t, 0, clear + 2 * bite, d - t, t)
        x += clear
        if bay < bays - 1:
            if i.base_doors:
                wanted[f"base-side-{bay + 1}"] = (x, 0, 0, t, d, deck_top - t + bite)
            wanted[f"divider-{bay}"] = (x, 0, deck_top - bite, t, d - 12000, inside)
            x += t
    if set(wanted) != {p.semantic_key for p in result.parts}:
        raise ValueError("Delantal eller fackindelning avviker från kundens order.")
    for p in result.parts:
        loc = p.placement
        stock = spec.back_material if p.role.value == "back" else spec.material
        if wanted[p.semantic_key] != (loc.x_um, loc.y_um, loc.z_um, *size(p)) or (
            p.material_id,
            p.material_version,
        ) != (stock.material_id, stock.version):
            raise ValueError(
                f"Ämne, mått eller placering avviker från kundordern: {p.semantic_key}"
            )


def indexed_csv_rows(files: dict[str, bytes], path: str, key: str) -> dict[str, dict[str, str]]:
    records = list(csv.DictReader(io.StringIO(files[path].decode("utf-8-sig"))))
    indexed = {r[key]: r for r in records}
    if len(indexed) != len(records):
        raise ValueError(f"Dubblerade poster i {path}")
    return indexed


def verify_panel_documents(result: FurnitureResult, files: dict[str, bytes]) -> None:
    bom = indexed_csv_rows(files, "bom/parts.csv", "part_id")
    cut = indexed_csv_rows(files, "bom/cut-list.csv", "part_id")
    assembly = indexed_csv_rows(files, "assembly/parts.csv", "part_id")
    parts = {p.part_id: p for p in result.parts}
    manifest = json.loads(files["manufacturing/manufacturing-intent.json"])
    ordered_parts = manifest["parts"]
    doc = manifest["document_identity"]
    if (doc["design_hash"], doc["revision"], doc["project_id"]) != (
        result.design_hash,
        str(result.spec.revision),
        result.spec.design_id,
    ):
        raise ValueError("Bearbetningsunderlaget tillhör inte orderrevisionen.")
    operations = {row["part_id"]: row for row in ordered_parts}
    if len(ordered_parts) != len(operations) or any(
        rows.keys() != parts.keys() for rows in (bom, cut, assembly, operations)
    ):
        raise ValueError("Del-ID avviker mellan kaplista, dellista och montering.")
    drawings = PdfReader(io.BytesIO(files["documents/part-drawings.pdf"]), strict=True)
    pages: dict[tuple[str, str], list[str]] = {}
    for page in drawings.pages:
        text = page.extract_text()
        path = next(
            (
                s.removeprefix("DXF: parts/").split("/")
                for s in text.splitlines()
                if s.startswith("DXF: parts/")
            ),
            None,
        )
        if (
            not path
            or len(path) != 2
            or f"Revision {result.spec.revision} | Design {result.design_hash}" not in text
        ):
            raise ValueError("Delritningens PDF saknar rätt revision eller delhänvisning.")
        pages.setdefault((path[0], path[1].removesuffix(".dxf")), []).append(text)
    expected = {(pid, s) for pid in parts for s in ("A", "B")}
    if set(pages) != expected:
        raise ValueError("PDF-delritningarnas sidor avviker från DXF-inventariet.")
    for pid, p in parts.items():
        a, b, n, _ = PLANES[p.a_side.value]
        uv = tuple(mm(size(p)[i]) for i in (a, b, n))
        common = dict(
            zip(("finished_width_mm", "finished_height_mm", "thickness_mm"), uv, strict=True)
        )
        common.update(material_id=p.material_id, material_version=p.material_version)
        raw = size(p, raw=True)
        pos = p.placement
        if (
            any(r[k] != v for r in (bom[pid], cut[pid]) for k, v in common.items())
            or bom[pid]["quantity"] != "1"
            or cut[pid]["instance_id"] != f"{pid}:001"
            or (cut[pid]["raw_width_mm"], cut[pid]["raw_height_mm"]) != (mm(raw[a]), mm(raw[b]))
            or assembly[pid]["part_name"] != p.semantic_key
            or tuple(int(assembly[pid][k]) for k in ("x_um", "y_um", "z_um"))
            != (pos.x_um, pos.y_um, pos.z_um)
        ):
            raise ValueError(f"Kaplista eller monteringslista avviker från ordern: {pid}")
        stock = result.spec.back_material if p.role == PartRole.BACK else result.spec.material
        broad_horizontal = {PartRole.TOP, PartRole.BOTTOM, PartRole.SHELF, PartRole.BASE_BOTTOM}
        expected_grain = (
            GrainDirection.NONE
            if resolve_material(stock).grain_direction == GrainDirection.NONE
            else GrainDirection.X
            if p.role in broad_horizontal
            else GrainDirection.Z
        )
        world_grain = expected_grain.value.upper()
        local_grain = "X" if world_grain == "XYZ"[a] else "Y" if world_grain == "XYZ"[b] else "NONE"
        op = operations[pid]
        finished = size(p)
        if (
            (p.material_id, p.material_version, p.actual_thickness_um, p.grain_direction)
            != (stock.material_id, stock.version, stock.measured_thickness_um, expected_grain)
            or (
                assembly[pid]["material"],
                assembly[pid]["material_version"],
                assembly[pid]["material_batch_id"],
                assembly[pid]["grain"],
            )
            != (stock.material_id, stock.version, stock.batch_id or "", expected_grain.value)
            or bom[pid]["grain_direction"] != local_grain
            or op["material"] != {"id": stock.material_id, "version": stock.version}
            or op["grain_direction"] != local_grain
            or op["quantity"] != 1
            or op["axis_mapping"]
            != {"u_axis": "xyz"[a], "v_axis": "xyz"[b], "thickness_axis": "xyz"[n]}
            or op["finished_dimensions_um"]
            != {"u": finished[a], "v": finished[b], "thickness": finished[n]}
            or op["raw_blank_dimensions_um"] != {"u": raw[a], "v": raw[b], "thickness": raw[n]}
        ):
            raise ValueError(
                f"Material, batch, fiber eller ämnesmått avviker från ordern: {p.semantic_key}"
            )
        grain_text = (
            f"Fiber längs U (stomaxel {world_grain})"
            if world_grain == "XYZ"[a]
            else f"Fiber längs V (stomaxel {world_grain})"
            if world_grain == "XYZ"[b]
            else "Ingen angiven fiberriktning"
        )
        for side in ("A", "B"):
            content = "\n".join(pages[pid, side])
            face = (p.a_side if side == "A" else p.b_side).value.upper()
            if (
                f"Färdig U/V/T: {' / '.join(uv)} mm | Antal: 1" not in content
                or f"Fysisk yta: {face}" not in content
                or f"{p.material_id}@{p.material_version}" not in content
            ):
                raise ValueError(f"Fel delmått eller fysisk sida i PDF: {pid}/{side}")
            if any(grain_text not in sheet for sheet in pages[pid, side]):
                raise ValueError(
                    f"Fiberriktningen i delritningen avviker från modellen: {pid}/{side}"
                )
            notes: dict[str, str] = {}
            for sheet in pages[pid, side]:
                body = sheet.split("Numren identifierar", 1)[-1].split("DXF: parts/", 1)[0]
                fragments = re.split(r"(?m)^F([0-9]+)  ", body)
                for number, block in zip(fragments[1::2], fragments[2::2], strict=True):
                    if block.startswith("Fortsättning\n") and number in notes:
                        notes[number] += block
                    elif number not in notes:
                        notes[number] = block
                    else:
                        raise ValueError(f"Dubblerad PDF-bearbetning: {pid}/{side}/F{number}")
            actual = {
                m.group(1): block
                for block in notes.values()
                if (m := re.search(r"\|  (fea_[0-9a-f]+|J95-[A-Za-z0-9-]+)\n", block))
            }
            expected_ops = {
                f.feature_id: f for f in p.features if physical_face(p, f) == face.lower()
            }
            if actual.keys() != expected_ops.keys():
                raise ValueError(f"Fel bearbetnings-ID på PDF-delritningen: {pid}/{side}")
            for key, f in expected_ops.items():
                c = panel_cut(p, f)
                dimensions = (
                    f"Bredd U {mm(c.width)} | Längd V {mm(c.length)} | Radie {mm(c.radius)}"
                    if c.width
                    else f"Diameter {mm(c.radius * 2)}"
                )
                terms = [
                    f"U {mm(c.u)} / V {mm(c.v)} mm",
                    dimensions + " | Djup " + mm(c.depth) + " mm",
                    f"Tolerans: +/- {mm(f.tolerance_um)} mm",
                    f"Passningsspel: {mm(f.fit_clearance_um)} mm",
                ]
                if f.front_stop_clearance_um:
                    terms.append(f"Stoppmån Y {mm(f.front_stop_clearance_um)} mm")
                for term in terms:
                    if term not in actual[key]:
                        raise ValueError(f"PDF saknar {term} för {pid}/{key}")

    graph = json.loads(files["assembly/graph.json"])
    design = result.shelving_result
    assert design is not None
    if (
        graph["design_hash"] != result.design_hash
        or graph["revision"] != result.spec.revision
        or graph["graph"] != design.assembly_graph.model_dump(mode="json")
    ):
        raise ValueError("Monteringsgrafen tillhör inte ordern.")
    manual = "\n".join(
        p.extract_text()
        for p in PdfReader(io.BytesIO(files["documents/assembly.pdf"]), strict=True).pages
    )
    if (
        f"{result.design_hash} · rev {result.spec.revision}" not in manual
        or any(pid not in manual for pid in parts)
        or any(step.step_id not in manual for step in design.assembly_graph.steps)
    ):
        raise ValueError("Monteringsmanualen saknar rätt revision, del eller steg.")


def verify_angle_register(result: FurnitureResult, files: dict[str, bytes]) -> None:
    parts = {p.part_id: p for p in result.parts}
    fittings = indexed_csv_rows(files, "bom/hardware.csv", "item")
    joints = indexed_csv_rows(files, "assembly/connections.csv", "joint_id")
    angles = {a.joint_id: a for a in result.trial_angles}
    expected_fittings = {"GN-970-ALM-30-30-30-B": len(angles), "DIN-96-A4-5x16": len(angles) * 2}
    actual_angles = {
        key: int(fittings[key]["quantity"]) for key in expected_fittings if key in fittings
    }
    expected_joints = {j.joint_id for j in result.joints}
    intent = result.spec.intent
    if isinstance(intent, ShelvingIntent) and intent.base_doors:
        expected_joints.update(
            f"J95-{bay + 1}-{pos}" for bay in range(intent.divider_count + 1) for pos in (1, 2)
        )
    elif fittings.keys() != expected_fittings.keys():
        raise ValueError("Beslagslistan avviker från ordern.")
    if actual_angles != expected_fittings or joints.keys() != expected_joints:
        raise ValueError("Beslagslistan eller förbandsregistret avviker från ordern.")
    for joint in result.joints:
        record = joints[joint.joint_id]
        for index, member in enumerate(joint.members, 1):
            if (
                record[f"part_{index}"] != parts[member.part_id].semantic_key
                or record[f"mating_face_{index}"] != member.mating_face.value
            ):
                raise ValueError(f"Fel motdel i monteringslistan: {joint.joint_id}")
        angle = angles.get(joint.joint_id)
        if (
            record["trial_angle"],
            record["angle_quantity"],
            record["screw"],
            record["screw_quantity"],
        ) != (
            angle.article if angle else "",
            "1" if angle else "0",
            angle.screw if angle else "",
            "2" if angle else "0",
        ):
            raise ValueError(f"Fel beslagsparning: {joint.joint_id}")


def verify_bookcase_delivery(
    result: FurnitureResult, files: dict[str, bytes]
) -> tuple[dict[str, Any], bytes]:
    assert result.shelving_result is not None
    identity = order_identity(result)
    verify_panels_from_order(result)
    expected = {f"parts/{p.part_id}/{s}.dxf" for p in result.parts for s in ("A", "B")}
    if {p for p in files if p.endswith(".dxf")} != expected:
        raise ValueError("DXF-inventariet avviker från beställningen.")
    pilots = []
    for p in result.parts:
        for s in ("A", "B"):
            pilots.extend(verify_panel_dxf(files[f"parts/{p.part_id}/{s}.dxf"], p, s, identity))
    verify_panel_documents(result, files)
    verify_angle_register(result, files)
    verify_bookcase_steps(
        result, files["design/model.step"], files["design/with-mounted-angles.step"]
    )
    features = [f for p in result.parts for f in p.features]
    return {
        **identity,
        "schema": READBACK_SCHEMA,
        "units": "mm",
        "timber_solids": len(result.parts),
        "mounted_angles": len(result.trial_angles),
        "dxf_faces": len(expected),
        "pilot_holes": sum(f.kind == FeatureKind.DRILL for f in features),
        "grooves": sum(f.kind == FeatureKind.GROOVE for f in features),
        "stopped_grooves": sum(bool(f.front_stop_clearance_um) for f in features),
        "assembly_steps": len(result.shelving_result.assembly_graph.steps),
    }, joint_measurement_sheet(result, pilots)
