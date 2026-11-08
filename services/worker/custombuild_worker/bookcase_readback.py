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
from custombuild_domain.enums import FeatureKind
from custombuild_domain.furniture import ShelvingIntent
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


def _panels_from_order(result: FurnitureResult) -> None:
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
    heights = i.shelf_height_ratios_ppm
    if heights:
        rows = [t + ((h - 2 * t) * r + 500000) // 1000000 - t // 2 for r in heights]
    else:
        opening, extra = divmod(h - (i.shelf_count + 2) * t, i.shelf_count + 1)
        rows = [(r + 1) * (t + opening) + min(r + 1, extra) for r in range(i.shelf_count)]
    inside = h - 2 * (t - bite)
    # Origin and finished X/Y/Z of each blank, calculated directly from the order.
    wanted = {
        "left-side": (0, 0, 0, t, d, h),
        "right-side": (w - t, 0, 0, t, d, h),
        "bottom": (t - bite, 0, 0, w - 2 * (t - bite), d, t),
        "top": (t - bite, 0, h - t, w - 2 * (t - bite), d, t),
    }
    x = t
    for bay, clear in enumerate(widths):
        for row, z in enumerate(rows):
            wanted[f"shelf-r{row}-b{bay}"] = (x - bite, 0, z, clear + 2 * bite, d - back - 16350, t)
        left = bite if bay == 0 else divider_bite
        right = bite if bay == bays - 1 else divider_bite
        name = "back" if bays == 1 else f"back-b{bay}"
        wanted[name] = (x - left, d - back - 12000, t - bite, clear + left + right, back, inside)
        x += clear
        if bay < bays - 1:
            wanted[f"divider-{bay}"] = (x, 0, t - bite, t, d - 12000, inside)
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


def _rows(files: dict[str, bytes], path: str, key: str) -> dict[str, dict[str, str]]:
    records = list(csv.DictReader(io.StringIO(files[path].decode("utf-8-sig"))))
    indexed = {r[key]: r for r in records}
    if len(indexed) != len(records):
        raise ValueError(f"Dubblerade poster i {path}")
    return indexed


def _documents(result: FurnitureResult, files: dict[str, bytes]) -> None:
    bom = _rows(files, "bom/parts.csv", "part_id")
    cut = _rows(files, "bom/cut-list.csv", "part_id")
    assembly = _rows(files, "assembly/parts.csv", "part_id")
    parts = {p.part_id: p for p in result.parts}
    if any(rows.keys() != parts.keys() for rows in (bom, cut, assembly)):
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
        for side in ("A", "B"):
            content = "\n".join(pages[pid, side])
            face = (p.a_side if side == "A" else p.b_side).value.upper()
            if (
                f"Färdig U/V/T: {' / '.join(uv)} mm | Antal: 1" not in content
                or f"Fysisk yta: {face}" not in content
                or f"{p.material_id}@{p.material_version}" not in content
            ):
                raise ValueError(f"Fel delmått eller fysisk sida i PDF: {pid}/{side}")
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
                if (m := re.search(r"\|  (fea_[0-9a-f]+)\n", block))
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
    fittings = _rows(files, "bom/hardware.csv", "item")
    joints = _rows(files, "assembly/connections.csv", "joint_id")
    angles = {a.joint_id: a for a in result.trial_angles}
    expected_fittings = {"GN-970-ALM-30-30-30-B": len(angles), "DIN-96-A4-5x16": len(angles) * 2}
    if {k: int(v["quantity"]) for k, v in fittings.items()} != expected_fittings or set(joints) != {
        j.joint_id for j in result.joints
    }:
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
    _panels_from_order(result)
    expected = {f"parts/{p.part_id}/{s}.dxf" for p in result.parts for s in ("A", "B")}
    if {p for p in files if p.endswith(".dxf")} != expected:
        raise ValueError("DXF-inventariet avviker från beställningen.")
    pilots = []
    for p in result.parts:
        for s in ("A", "B"):
            pilots.extend(verify_panel_dxf(files[f"parts/{p.part_id}/{s}.dxf"], p, s, identity))
    _documents(result, files)
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
