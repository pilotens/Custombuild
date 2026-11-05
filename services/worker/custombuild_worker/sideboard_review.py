"""S1 exports through the existing immutable furniture-review artifact contract."""

import hashlib
import io
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from custombuild_cad.sideboard_checks import audit_sideboard
from custombuild_cad.sideboard_stability import closed_stability
from custombuild_domain.sideboard_construction import SideboardConstruction, construct_sideboard
from custombuild_domain.sideboard_snapshot import SideboardWorkspace

from .sideboard_drawings import sideboard_pdf, sideboard_viewer
from .trestle_drawings import FACES, face_dxf, face_svg
from .trestle_review import _csv, _json


def sideboard_files(package: SideboardConstruction) -> dict[str, bytes]:
    import cadquery as cq

    audit, solids = audit_sideboard(package)
    if not audit["wood_and_boring_checks_passed"]:
        raise ValueError("Skänkens trä-/borrkontroll underkändes: " + str(audit))
    if package.request.review:
        audit["closed_stability"] = closed_stability(package, solids)
    files = {
        "construction.json": _json(package.model_dump(mode="json")),
        "assembly.json": _json(package.assembly.model_dump(mode="json")),
        "validation/sideboard.json": _json(audit),
        "documents/ritningar.pdf": sideboard_pdf(package, audit),
        "viewer.html": sideboard_viewer(package, audit),
        "bom/hardware.csv": _csv(
            [["article_or_specification", "quantity"]]
            + [[a, n] for a, n in package.hardware.items()]
        ),
        "bom/cut-list.csv": _csv(
            [
                [
                    "part",
                    "material",
                    "finished_L_mm",
                    "finished_B_mm",
                    "finished_T_mm",
                    "purchase_L_mm",
                    "purchase_B_mm",
                    "purchase_T_mm",
                    "grain_axis",
                ]
            ]
            + [
                [
                    b.part_id,
                    next(p.material for p in package.assembly.parts if p.id == b.part_id),
                    *(v / 1000 for v in b.finished_lbt_um),
                    *(v / 1000 for v in b.purchase_min_lbt_um),
                    b.grain_axis,
                ]
                for b in package.purchase_blanks
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
                    "diameter_mm",
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
                    o.depth_um / 1000,
                    o.through,
                ]
                for p in package.assembly.parts
                for o in p.operations
            ]
        ),
        "assembly/connections.csv": _csv(
            [
                [
                    "connection",
                    "part_a",
                    "operation_a",
                    "part_b",
                    "operation_b",
                    "article",
                    "diameter_mm",
                    "length_mm",
                    "head_recess_mm",
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
                ]
                for j in package.assembly.joints
            ]
        ),
        "assembly/fittings.json": _json([f.model_dump(mode="json") for f in package.fittings]),
    }
    if package.assessment:
        files["validation/planning.json"] = _json(
            dict(
                construction_sha256=package.construction_sha256,
                assessment=package.assessment.model_dump(mode="json"),
                closed_stability=audit["closed_stability"],
            )
        )
    roundtrips = []
    with tempfile.TemporaryDirectory(prefix="custombuild-sideboard-") as temporary:
        for part in package.assembly.parts:
            original = solids[part.id]
            path = Path(temporary) / (part.id + ".step")
            cq.exporters.export(original, str(path))
            restored = cq.importers.importStep(str(path)).val()
            if not isinstance(restored, cq.Shape):
                raise ValueError("STEP saknar form: " + part.id)
            delta = max(
                abs(getattr(original.BoundingBox(), axis) - getattr(restored.BoundingBox(), axis))
                for axis in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")
            )
            relative = abs(original.Volume() - restored.Volume()) / original.Volume()
            if (
                not restored.isValid()
                or len(restored.Solids()) != 1
                or delta >= 0.005
                or relative >= 1e-8
            ):
                raise ValueError("STEP-återimport underkänd: " + part.id)
            roundtrips.append(
                dict(part=part.id, bbox_difference_mm=delta, relative_volume_difference=relative)
            )
            files[f"parts/{part.id}/{part.id}.step"] = path.read_bytes()
            for face, axes in FACES:
                files[f"parts/{part.id}/{face}.dxf"] = face_dxf(part, face, axes)
                files[f"parts/{part.id}/{face}.svg"] = face_svg(part, face, axes)
        assembled = Path(temporary) / "assembly.step"
        cq.exporters.export(cq.Compound.makeCompound(list(solids.values())), str(assembled))
        back = cq.importers.importStep(str(assembled)).val()
        if not isinstance(back, cq.Shape) or len(back.Solids()) != len(solids):
            raise ValueError("STEP-monteringen har tappat en trädel.")
        files["assembly/model.step"] = assembled.read_bytes()
    files["validation/step-roundtrips.json"] = _json(roundtrips)
    files["README.txt"] = (
        "CUSTOMBUILD / SKÄNK S1\n\nÖppna viewer.html eller documents/ritningar.pdf.\n"
        "Alla mått är mm. DXF INSUNITS=4. D..._UM anger borrdjup i mikrometer.\n"
        "Vald konstruktion efter foto, inte uppmätta originalmått.\n"
        "38 STEP-delar visar bearbetat trä. Köpta beslag är poster med borrbilder,\n"
        "inte kompletta 3D-beslag. 10 gångjärn, 20 hyllbärare, 5 trycköppnare;\n"
        "montageplattor och skruvar finns i BOM. 90 träförband i nominell geometri.\n"
        "Limfogar och förband är inte lastklassade. Fem baköppningar är täckta.\n"
        "Luckornas 2 mm intryckning är kontrollerad mot träet. Det bevisar inte\n"
        "ett 110-graders gångjärns verkliga rörelse. REFERENCE_EXIT är en referens,\n"
        "ingen andra bearbetning. Inga lagerlängder, materialbatcher,\n"
        "leverantörsgodkännanden eller maskinprogram är antagna.\n\n"
        + "\n".join(package.outstanding_acceptance)
        + "\n"
    ).encode()
    return files


def build_sideboard_review(workspace: SideboardWorkspace) -> bytes:
    package = construct_sideboard(workspace.request)
    files = sideboard_files(package)
    files["design/workspace.json"] = _json(workspace.model_dump(mode="json"))
    manifest: dict[str, Any] = dict(
        schema_version="custombuild.furniture-review.v1",
        source_type="sideboard-construction",
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
    files["manifest.json"] = _json(manifest)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, data)
    payload = out.getvalue()
    if len(payload) > 16 * 1024 * 1024:
        raise ValueError("Skänkens paket överstiger 16 MiB.")
    return payload
