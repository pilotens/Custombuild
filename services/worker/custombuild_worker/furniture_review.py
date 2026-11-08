"""Complete, non-production review exports for the furniture-family workspace."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import asdict
from typing import Any

from custombuild_cad import CadQueryAdapter
from custombuild_cad.bookcase_motion import verify_assembly_space
from custombuild_domain.bookcase_angles import screw_access
from custombuild_domain.bookcase_fit import trial_groove_fits
from custombuild_domain.bookcase_site import StandingSiteReview
from custombuild_domain.furniture import FurnitureWorkspace, furniture_review_schema
from custombuild_domain.furniture_engine import build_furniture
from custombuild_manufacturing.adapters import adapt_design_result
from custombuild_manufacturing.exporters import bom_csv, cut_list_csv, dxf_for_part, svg_for_part
from custombuild_manufacturing.furniture_faces import drawing_faces
from custombuild_manufacturing.furniture_handoff import furniture_first_article_checks
from custombuild_manufacturing.furniture_profiles import preview_furniture
from custombuild_manufacturing.quality import (
    MANUFACTURING_INTENT_PATH,
    manufacturing_intent_json,
    manufacturing_intent_json_schema,
)

from .documents import assembly_manual_pdf
from .furniture_assembly import assembly_documents
from .part_drawings import part_drawings_pdf
from .shelving_trial import shelving_trial_protocol

MAX_FURNITURE_REVIEW_PARTS = 128
MAX_FURNITURE_REVIEW_BYTES = 16 * 1024 * 1024


def _json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def build_furniture_review(workspace: FurnitureWorkspace) -> bytes:
    result = build_furniture(workspace.design)
    if len(result.parts) > MAX_FURNITURE_REVIEW_PARTS:
        raise ValueError("furniture review export is limited to 128 parts")
    preview = preview_furniture(workspace)
    adapted = adapt_design_result(result)
    # Use the same exact CAD kernel/roundtrip and solid-collision gates as the
    # established pipeline. Missing CAD cannot produce placeholder interchange.
    cad = CadQueryAdapter().export_design(result)
    files = {
        "design/workspace.json": _json(workspace.model_dump(mode="json")),
        "design/resolved.json": _json(result.model_dump(mode="json")),
        "design/model.step": cad.step,
        "design/model.glb": cad.glb,
        "validation/review.json": _json(preview),
        "manufacturing/workshop-handoff.json": _json(preview["workshop_handoff"]),
        "inspection/first-article-checks.csv": furniture_first_article_checks(result),
        "documents/part-drawings.pdf": part_drawings_pdf(
            result,
            qualification_note=(
                "PRELIMINÄRT: Kundmått/list/montage återstår. Inga listdelar ingår."
                if preview["workshop_handoff"]["dimensions"]["issues"]
                else "KONCEPT: Beslag, hålbilder och infästningar är inte verifierade."
                if result.hardware_profile is not None
                and not result.hardware_profile.drawer_holes_um
                else "Granskningsunderlag. Tillverkning kräver separat frisläppning."
            ),
        ),
        "bom/parts.csv": bom_csv(adapted.parts),
        "bom/cut-list.csv": cut_list_csv(adapted.parts),
        "assembly/moving-groups.json": _json(
            [g.model_dump(mode="json") for g in result.moving_groups]
        ),
    }
    files.update(assembly_documents(result))
    shelving = result.shelving_result
    if shelving is not None:
        space = verify_assembly_space(shelving, cad.step) if shelving.trial_angles else None
        site_data = preview["workshop_handoff"]["dimensions"]["standing_assembly"]
        site = StandingSiteReview.model_validate(site_data) if site_data else None
        if space:
            if site and (
                site.minimum_height_from_base_um != space.world_max_um[2]
                or site.minimum_front_space_um != max(0, -space.world_min_um[1])
            ):
                raise ValueError("Platskontrollen och CAD använder olika monteringsutrymme.")
            files["assembly/required-space.json"] = _json(
                {
                    "design_hash": result.design_hash,
                    "revision": workspace.design.revision,
                    "units": "integer_micrometres",
                    "scope": "Rigid timber, straight translations, no fixtures or human clearance.",
                    **asdict(space),
                    "standing_site": site_data,
                }
            )
        files["inspection/provprotokoll.txt"] = shelving_trial_protocol(result, preview, space)
        files["documents/assembly-overview.pdf"] = files["documents/assembly.pdf"]
        files["documents/assembly.pdf"] = assembly_manual_pdf(
            shelving,
            package_design_hash=result.design_hash,
            assembly_space=space,
            standing_site=site,
        )
        files["assembly/graph.json"] = _json(
            {
                "design_hash": result.design_hash,
                "revision": workspace.design.revision,
                "graph": shelving.assembly_graph.model_dump(mode="json"),
            }
        )
        files[MANUFACTURING_INTENT_PATH] = manufacturing_intent_json(
            parts=adapted.parts,
            project_id=workspace.design.design_id,
            revision=str(workspace.design.revision),
            design_hash=result.design_hash,
        )
        files["schemas/manufacturing-intent.schema.json"] = manufacturing_intent_json_schema()
        if shelving.trial_angles:
            from custombuild_cad.bookcase_angles import mounted_trial_angles

            files["design/with-mounted-angles.step"] = mounted_trial_angles(
                cad.step, shelving.trial_angles
            )
            files["inspection/groove-fits.json"] = _json(
                {
                    "design_hash": result.design_hash,
                    "revision": workspace.design.revision,
                    "unit": "integer_micrometres",
                    "qualification": "trial_targets_only",
                    "joints": [asdict(f) for f in trial_groove_fits(result.parts, result.joints)],
                }
            )
            files["assembly/trial-angles.json"] = _json(
                {
                    "design_hash": result.design_hash,
                    "revision": workspace.design.revision,
                    "units": "micrometres",
                    "corners": "World X/Y/Z at outer bend, Y at bracket centreline.",
                    "angles": [a.model_dump(mode="json") for a in shelving.trial_angles],
                    "straight_access_um_by_pilot": screw_access(
                        result.parts, shelving.trial_angles
                    ),
                    "access_scope": "10 mm square axial envelope from screw seat, in final "
                    "assembly; null means open in this model. Verify actual handle and hands.",
                    "qualification": "geometry_trial_only",
                }
            )
    sources = {p.part_id: p for p in result.parts}
    for original in adapted.parts:
        for view in drawing_faces(sources[original.part_id], original):
            part, side = view.part, view.side
            root = f"parts/{part.part_id}/{view.name}"
            files[f"{root}.dxf"] = dxf_for_part(part, side)
            files[f"{root}.svg"] = svg_for_part(part, side)
    files["README.txt"] = (
        "CUSTOMBUILD – DESIGNUNDERLAG FÖR GRANSKNING\n\n"
        f"Familj: {workspace.design.intent.family}\n"
        f"Revision: {workspace.design.revision}\nDesign: {result.design_hash}\n\n"
        "STEP, GLB, delritningar, DXF och kaplista beskriver samma genererade delar.\n"
        "DXF använder millimeter. Delritningarna visar varje dels lokala U/V-system.\n"
        "Detta paket innehåller INGEN skärande CAM eller körklar maskinkod.\n"
        "Öppna documents/assembly.pdf, bom/hardware.csv och assembly/connections.csv.\n"
        "Kantvyer har fysiska filnamn och referenskontur på lagret REFERENCE.\n"
        "Material, förband, stabilitet, lådbottnar, rörelse och montering behöver\n"
        "kvalificeras inom den valda möbelfamiljen. Se validation/review.json.\n"
        "En ny material-, beslags- eller maskinprofil kräver omräkning och ny granskning.\n"
        "Kundens yttermått, utrymme för list/montage och exakta råformat per material\n"
        "finns i manufacturing/workshop-handoff.json. Reserverat listutrymme skapar\n"
        "inga listdelar; ofullständiga listuppgifter blockerar tillverkningsberedning.\n"
        "stock_plan visar valda råformat per material/tjocklek, tillåtna rotationer\n"
        "och minimiformat inklusive kantmarginal. Varje del ska rymmas enskilt;\n"
        "planen anger inte nesting, beställningsantal eller godkänd uppspänning.\n"
        "inspection/first-article-checks.csv anger CAD-delarnas kontrollmått och\n"
        "modellens featuretoleranser. Verkstaden fastställer avtalade toleranser och\n"
        "fyller i mätning och granskare. Tomma resultat är inte godkända mätningar.\n"
    ).encode()
    if shelving is not None:
        files["README.txt"] += (
            "\nBOKHYLLA – börja med documents/assembly.pdf för numrerade steg och del-ID.\n"
            "manufacturing/manufacturing-intent.json anger bearbetningar med sida, datum, "
            "speldimension och tolerans. Maskinval krävs inte för detta underlag.\n"
        ).encode()
    manifest = {
        "schema_version": furniture_review_schema(workspace.design.intent.family),
        "engine_version": result.engine_version,
        "design_hash": result.design_hash,
        "revision": workspace.design.revision,
        "dependencies": preview["dependencies"],
        "production_qualified": False,
        "physical_cutting_authorized": False,
        "files": [
            {"path": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(files.items())
        ],
    }
    files["manifest.json"] = _json(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, data)
    payload = buffer.getvalue()
    if len(payload) > MAX_FURNITURE_REVIEW_BYTES:
        raise ValueError("furniture review export exceeds its 16 MiB limit")
    return payload
