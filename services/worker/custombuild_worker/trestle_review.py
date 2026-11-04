"""Build a complete drawing package; reject failed exact CAD or STEP roundtrips."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from custombuild_cad.measured_machining import audit_machined_assembly
from custombuild_cad.trestle_checks import audit_trestle_movement
from custombuild_domain.trestle_construction import TrestleConstruction, construct_trestle
from custombuild_domain.trestle_snapshot import TrestleWorkspace

from .trestle_drawings import FACES, construction_pdf, face_dxf, face_svg


def _json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def _csv(rows: list[list[Any]]) -> bytes:
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def construction_files(package: TrestleConstruction) -> dict[str, bytes]:
    import cadquery as cq

    audit, solids = audit_machined_assembly(package.assembly)
    movement = audit_trestle_movement(package, solids)
    if not audit["geometric_checks_passed"]:
        raise ValueError("CAD-kontrollen underkände konstruktionen: " + json.dumps(audit))
    if not movement["passed"]:
        raise ValueError("Skivinfästningarnas rörelsemån underkändes: " + json.dumps(movement))
    files = {
        "construction.json": _json(package.model_dump(mode="json")),
        "assembly.json": _json(package.assembly.model_dump(mode="json")),
        "validation/cad.json": _json(audit),
        "validation/tabletop-movement.json": _json(movement),
        "documents/ritningar.pdf": construction_pdf(package, audit),
        "bom/hardware.csv": _csv(
            [["specification", "quantity"]]
            + [[h.specification, h.quantity] for h in package.hardware]
        ),
        "bom/cut-list.csv": _csv(
            [
                [
                    "part",
                    "finished_L_mm",
                    "finished_B_mm",
                    "finished_T_mm",
                    "purchase_L_mm",
                    "purchase_B_mm",
                    "purchase_T_mm",
                    "grain",
                ]
            ]
            + [
                [
                    p.part_id,
                    *(v / 1000 for v in p.finished_blank_lbt_um),
                    *(v / 1000 for v in p.purchase_min_lbt_um),
                    p.grain,
                ]
                for p in package.purchase_blanks
            ]
        ),
        "assembly/connections.csv": _csv(
            [
                [
                    "joint",
                    "part_a",
                    "operation_a",
                    "part_b",
                    "operation_b",
                    "fastener",
                    "diameter_mm",
                    "length_mm",
                    "head_recess_mm",
                    "nut_recess_mm",
                    "fittings",
                ]
            ]
            + [
                [
                    j.id,
                    j.a.part,
                    j.a.operation,
                    j.b.part,
                    j.b.operation,
                    j.article,
                    (j.diameter_um or 0) / 1000,
                    (j.length_um or 0) / 1000,
                    (j.head_recess_um or 0) / 1000,
                    (j.nut_recess_um or 0) / 1000,
                    ";".join(j.fittings),
                ]
                for j in package.assembly.joints
            ]
        ),
        "manufacturing/operations.csv": _csv(
            [
                [
                    "part",
                    "operation",
                    "shape",
                    "face",
                    "axes",
                    "U_mm",
                    "V_mm",
                    "width_mm",
                    "height_mm",
                    "depth_mm",
                    "through",
                ]
            ]
            + [
                [
                    p.id,
                    o.id,
                    o.shape,
                    o.face,
                    o.axes,
                    o.u_um / 1000,
                    o.v_um / 1000,
                    o.width_um / 1000,
                    o.height_um / 1000,
                    o.depth_um / 1000,
                    o.through,
                ]
                for p in package.assembly.parts
                for o in p.operations
            ]
        ),
    }
    roundtrips = []
    with tempfile.TemporaryDirectory(prefix="custombuild-trestle-") as temporary:
        for part in package.assembly.parts:
            original = solids[part.id]
            path = Path(temporary) / (part.id + ".step")
            cq.exporters.export(original, str(path))
            restored = cq.importers.importStep(str(path)).val()
            if not isinstance(restored, cq.Shape):
                raise ValueError(f"STEP innehåller ingen form: {part.id}")
            delta = max(
                abs(getattr(original.BoundingBox(), axis) - getattr(restored.BoundingBox(), axis))
                for axis in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")
            )
            rel_volume = abs(original.Volume() - restored.Volume()) / original.Volume()
            if (
                not restored.isValid()
                or len(restored.Solids()) != 1
                or delta >= 0.005
                or rel_volume >= 1e-8
            ):
                raise ValueError(f"STEP-återimport underkänd: {part.id}")
            roundtrips.append(
                dict(part=part.id, bbox_difference_mm=delta, relative_volume_difference=rel_volume)
            )
            files[f"parts/{part.id}/{part.id}.step"] = path.read_bytes()
            for face, axes in FACES:
                files[f"parts/{part.id}/{face}.dxf"] = face_dxf(part, face, axes)
                files[f"parts/{part.id}/{face}.svg"] = face_svg(part, face, axes)
        path = Path(temporary) / "assembly.step"
        cq.exporters.export(cq.Compound.makeCompound(list(solids.values())), str(path))
        assembly = cq.importers.importStep(str(path)).val()
        if not isinstance(assembly, cq.Shape):
            raise ValueError("STEP-monteringen innehåller ingen form.")
        if len(assembly.Solids()) != len(solids):
            raise ValueError("STEP-monteringen tappade eller duplicerade en del.")
        files["assembly/model.step"] = path.read_bytes()
    files["validation/step-roundtrips.json"] = _json(roundtrips)
    files["README.txt"] = (
        "CUSTOMBUILD / BOCKBORD B1\n\n"
        "Öppna documents/ritningar.pdf. Alla mått är mm; DXF $INSUNITS=4.\n"
        "STEP-filerna innehåller bearbetat trä, inte osynliga förenklade lådor.\n"
        "De sex delvyerna använder positiva lokala axlar, inte spegelvända vyer.\n"
        "Delvyerna visar ämneskontur och bearbetningar, inte alla dolda kanter.\n"
        "REFERENCE_EXIT-lager visar utgångshål och ska inte bearbetas en andra gång.\n"
        "DXF-lager D..._UM anger skärdjup i mikrometer; operations.csv anger mm.\n"
        "Förband, hål och beslag kan spåras via samma del- och bearbetnings-ID.\n"
        "Vald ny konstruktion: sidomonterade tvärbalkar och synliga bultar.\n"
        "Konstruktionen är inte en identifiering av fotografiets dolda anslutningar.\n"
        "Beslagsnummer är måttspecifikationer, inte bekräftade leverantörsartiklar.\n"
        "Räta urtags innerhörn behöver efterbearbetas eller få verkstadsaccepterad avlastning.\n"
        "Skivans fästen måste medge glidning; de får inte låsas med lim.\n"
        "Ingen fysisk lastklassning, toleransacceptans eller maskinkod ingår.\n\n"
        + "\n".join(package.outstanding_acceptance)
        + "\n"
    ).encode()
    return files


def build_trestle_review(workspace: TrestleWorkspace) -> bytes:
    package = construct_trestle(workspace.request)
    files = construction_files(package)
    files["design/workspace.json"] = _json(workspace.model_dump(mode="json"))
    files["manifest.json"] = _json(
        dict(
            schema_version="custombuild.furniture-review.v1",
            source_type="trestle-construction",
            design_hash=package.construction_sha256,
            assembly_sha256=package.assembly_sha256,
            revision=workspace.revision,
            production_qualified=False,
            physical_cutting_authorized=False,
            files=[
                dict(path=name, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                for name, data in sorted(files.items())
            ],
        )
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, data)
    result = output.getvalue()
    if len(result) > 16 * 1024 * 1024:
        raise ValueError("Bordets ritningspaket överstiger exportgränsen 16 MiB.")
    return result
