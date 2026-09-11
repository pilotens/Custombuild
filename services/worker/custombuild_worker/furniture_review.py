"""Complete, non-production review exports for the furniture-family workspace."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import Any

from custombuild_cad import CadQueryAdapter
from custombuild_domain.furniture import FURNITURE_ENGINE_VERSION, FurnitureWorkspace
from custombuild_domain.furniture_engine import build_furniture
from custombuild_manufacturing.adapters import adapt_design_result
from custombuild_manufacturing.exporters import bom_csv, cut_list_csv, dxf_for_part, svg_for_part
from custombuild_manufacturing.furniture_handoff import furniture_first_article_checks
from custombuild_manufacturing.furniture_profiles import preview_furniture
from custombuild_manufacturing.model import Side

from .part_drawings import part_drawings_pdf

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
                else "Granskningsunderlag. Tillverkning kräver separat frisläppning."
            ),
        ),
        "bom/parts.csv": bom_csv(adapted.parts),
        "bom/cut-list.csv": cut_list_csv(adapted.parts),
        "assembly/moving-groups.json": _json(
            [g.model_dump(mode="json") for g in result.moving_groups]
        ),
    }
    for part in adapted.parts:
        for side in (Side.A, Side.B):
            root = f"parts/{part.part_id}/{side.value}"
            files[f"{root}.dxf"] = dxf_for_part(part, side)
            files[f"{root}.svg"] = svg_for_part(part, side)
    files["README.txt"] = (
        "CUSTOMBUILD – DESIGNUNDERLAG FÖR GRANSKNING\n\n"
        f"Familj: {workspace.design.intent.family}\n"
        f"Revision: {workspace.design.revision}\nDesign: {result.design_hash}\n\n"
        "STEP, GLB, delritningar, DXF och kaplista beskriver samma genererade delar.\n"
        "DXF använder millimeter. Delritningarna visar varje dels lokala U/V-system.\n"
        "Detta paket innehåller INGEN skärande CAM eller körklar maskinkod.\n"
        "Bordets/byråns layoutbeslag saknar tillverkarens hålbilder och verifierade\n"
        "infästningar. Saknad bearbetning ska kompletteras i modellen före tillverkning.\n"
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
    manifest = {
        "schema_version": "custombuild.furniture-review.v1",
        "engine_version": FURNITURE_ENGINE_VERSION,
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
