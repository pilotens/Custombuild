from __future__ import annotations

import csv
import io
from typing import Any

from reportlab.lib.colors import HexColor
from reportlab.pdfgen.canvas import Canvas

from .documents import (
    ACCENT,
    BLOCK,
    INK,
    MARGIN,
    MUTED,
    PAGE_WIDTH,
    WARNING,
    _canvas,
    _draw_exploded_parts,
    _draw_wrapped_text,
    _finish,
    _footer,
    _header,
)


def supplier_readme_pdf(design: Any, parts: tuple[Any, ...]) -> bytes:
    """Production handoff cover sheet for an external CNC/joinery supplier."""

    buffer, canvas = _canvas()
    y = _header(canvas, "TILLVERKNINGSUNDERLAG - LÄS FÖRST", f"Designhash {design.design_hash}")
    canvas.setFillColor(BLOCK)
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(MARGIN, y, "DO NOT SCALE DRAWINGS - använd endast angivna mått och koordinater")
    y -= 22
    canvas.setFillColor(INK)
    canvas.setFont("Helvetica", 8)
    intro = (
        "Detta paket är avsett för en professionell CNC-/snickerileverantör. Alla delar, hål, "
        "spår, fickor och konturer kommer från samma frysta parametriska DesignSpec. Leverantören "
        "ska skapa sin egen CAM/postprocessering för den faktiska maskinen och får inte härleda "
        "saknade mått genom att skala ritningar."
    )
    y = _draw_wrapped_text(canvas, intro, MARGIN, y, max_chars=108, leading=11) - 8
    size = design.spec.parameters
    rows = [
        ("Revision", str(design.spec.revision)),
        ("Yttermått X/Y/Z", f"{size.width_um/1000:g} × {size.depth_um/1000:g} × {size.height_um/1000:g} mm"),
        ("Primär materialtjocklek", f"{size.actual_thickness_um/1000:g} mm uppmätt"),
        ("Koordinatsystem", "Högerhänt: X=bredd, Y=djup, Z=höjd"),
        ("Delar", str(len(parts))),
        ("Enheter i DXF/PDF", "millimeter"),
    ]
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(MARGIN, y, "Projektdata")
    y -= 15
    for label, value in rows:
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawString(MARGIN, y, label)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(190, y, value)
        y -= 13
    y -= 6
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(MARGIN, y, "Obligatorisk arbetsordning för leverantören")
    y -= 15
    steps = (
        "1. Matcha varje fysisk del mot part-ID och materialversion.",
        "2. Importera respektive A/B-DXF eller STEP i egen CAM. Behåll angivet origo och enheter.",
        "3. Kontrollera alla hål/spår/fickor mot detaljritningens feature-tabell före postprocessering.",
        "4. Välj verktyg, feeds/speeds, fixturering och nollpunkt enligt den faktiska CNC-cellen.",
        "5. Tillverka en första artikel och verifiera kontrollmåtten innan serieproduktion.",
        "6. Märk varje färdig del med part-ID; blanda inte revisioner.",
    )
    canvas.setFont("Helvetica", 8)
    for line in steps:
        y = _draw_wrapped_text(canvas, line, MARGIN, y, max_chars=108, leading=11) - 3
    y -= 4
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(MARGIN, y, "Paketets källhierarki")
    y -= 14
    canvas.setFont("Helvetica", 8)
    for line in (
        "1) manifest.json + designhash/revision",
        "2) detail-drawings/*.pdf + parts/*/*.dxf",
        "3) model/design.step för sammanhang och kontroll",
        "4) cam/operations.json som semantisk operationslista - inte maskinspecifik G-code",
    ):
        canvas.drawString(MARGIN, y, line)
        y -= 12
    y -= 5
    canvas.setFillColor(WARNING)
    canvas.setFont("Helvetica-Bold", 8)
    warning = (
        "STOPP: Om material, uppmätt tjocklek, feature-position, tolerans, sida A/B eller revision "
        "är oklar ska delen inte tillverkas innan frågan är löst."
    )
    _draw_wrapped_text(canvas, warning, MARGIN, y, max_chars=105, leading=11)
    _footer(canvas, "Custombuild Supplier Manufacturing Package - deterministiskt genererat")
    return _finish(buffer, canvas)


def part_feature_csv(part: Any) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow((
        "feature_id", "side", "kind", "x_mm", "y_mm", "diameter_mm", "width_mm",
        "length_mm", "depth_mm", "pattern_count", "pitch_mm", "through",
        "tolerance_mm", "fit_clearance_mm"
    ))
    for feature in sorted(part.features, key=lambda item: item.feature_id):
        writer.writerow((
            feature.feature_id, feature.side.value, feature.kind.value, feature.x_um/1000,
            feature.y_um/1000, "" if feature.diameter_um is None else feature.diameter_um/1000,
            "" if feature.width_um is None else feature.width_um/1000,
            "" if feature.length_um is None else feature.length_um/1000, feature.depth_um/1000,
            feature.pattern_count, "" if feature.pitch_um is None else feature.pitch_um/1000,
            str(bool(feature.through)).lower(), feature.tolerance_um/1000,
            feature.fit_clearance_um/1000,
        ))
    return output.getvalue().encode("utf-8")


def _draw_part_plan(canvas: Canvas, part: Any, side_value: str, x: float, y_top: float, box_w: float, box_h: float) -> None:
    """Draw a scaled, dimensionally faithful 2D machining-face preview."""
    margin = 14.0
    usable_w = box_w - 2 * margin
    usable_h = box_h - 2 * margin - 16
    width_mm = part.width_um / 1000
    height_mm = part.height_um / 1000
    scale = min(usable_w / max(width_mm, 1e-9), usable_h / max(height_mm, 1e-9))
    draw_w = width_mm * scale
    draw_h = height_mm * scale
    ox = x + (box_w - draw_w) / 2
    oy = y_top - 20 - draw_h
    canvas.setStrokeColor(HexColor("#9aa59f"))
    canvas.setLineWidth(0.6)
    canvas.rect(x, y_top - box_h, box_w, box_h, stroke=1, fill=0)
    canvas.setFillColor(INK)
    canvas.setFont("Helvetica-Bold", 7)
    canvas.drawString(x + 6, y_top - 11, f"SIDA {side_value} - lokal U/V-geometri")
    canvas.setStrokeColor(INK)
    canvas.setLineWidth(0.8)
    canvas.rect(ox, oy, draw_w, draw_h, stroke=1, fill=0)
    for feature in sorted(part.features, key=lambda item: item.feature_id):
        if feature.side.value != side_value or feature.kind.value == "OUTER_CONTOUR":
            continue
        fx = ox + (feature.x_um / 1000) * scale
        fy = oy + (feature.y_um / 1000) * scale
        if feature.diameter_um is not None:
            radius = max(1.2, (feature.diameter_um / 2000) * scale)
            canvas.setStrokeColor(HexColor("#2563eb"))
            for index in range(feature.pattern_count):
                px = fx + index * ((feature.pitch_um or 0) / 1000) * scale
                canvas.circle(px, fy, radius, stroke=1, fill=0)
        elif feature.width_um is not None:
            fw = max(1.2, (feature.width_um / 1000) * scale)
            fl = max(1.2, ((feature.length_um or feature.width_um) / 1000) * scale)
            canvas.setStrokeColor(HexColor("#b45309" if feature.kind.value in {"GROOVE", "RABBET"} else "#2563eb"))
            canvas.rect(fx, fy, fw, fl, stroke=1, fill=0)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 5.8)
    canvas.drawCentredString(ox + draw_w/2, oy - 8, f"U {width_mm:g} mm")
    canvas.saveState()
    canvas.translate(ox - 8, oy + draw_h/2)
    canvas.rotate(90)
    canvas.drawCentredString(0, 0, f"V {height_mm:g} mm")
    canvas.restoreState()


def part_drawing_pdf(part: Any) -> bytes:
    """Human-readable manufacturing drawing with complete feature coordinate table."""

    buffer, canvas = _canvas()
    title = f"DETALJRITNING - {part.name}"
    y = _header(canvas, title, f"Part-ID {part.part_id}")
    canvas.setFillColor(BLOCK)
    canvas.setFont("Helvetica-Bold", 8.5)
    canvas.drawString(MARGIN, y, "DO NOT SCALE DRAWING")
    y -= 17
    canvas.setFillColor(INK)
    canvas.setFont("Helvetica", 8)
    metadata = [
        ("Färdigmått U × V × T", f"{part.width_um/1000:g} × {part.height_um/1000:g} × {part.thickness_um/1000:g} mm"),
        ("Råmått U × V", f"{part.blank_width_um/1000:g} × {part.blank_height_um/1000:g} mm"),
        ("Material", f"{part.material_id} @ {part.material_version}"),
        ("Fiberriktning", str(part.grain_direction)),
        ("A/B-orientering", f"A={part.metadata.get('domain_a_side','-')} · B={part.metadata.get('domain_b_side','-')}"),
        ("Kantlist", ", ".join(part.edge_bands) if part.edge_bands else "Ingen"),
        ("Origo", "X0/Y0 = nedre vänster hörn i respektive A/B-DXF"),
    ]
    for label, value in metadata:
        canvas.setFont("Helvetica-Bold", 7.5)
        canvas.drawString(MARGIN, y, label)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(175, y, str(value))
        y -= 11
    y -= 5
    drawing_top = y
    gap = 10
    drawing_w = (PAGE_WIDTH - 2*MARGIN - gap) / 2
    _draw_part_plan(canvas, part, "A", MARGIN, drawing_top, drawing_w, 205)
    _draw_part_plan(canvas, part, "B", MARGIN + drawing_w + gap, drawing_top, drawing_w, 205)
    y -= 218
    canvas.setFont("Helvetica-Bold", 8)
    canvas.setFillColor(INK)
    canvas.drawString(MARGIN, y, "Bearbetningsfeatures - exakta koordinater i mm")
    y -= 13
    headers = ("ID", "Sida", "Typ", "X", "Y", "Ø/B×L", "Djup", "Tol.")
    xs = (MARGIN, 175, 205, 267, 309, 351, 431, 486)
    canvas.setFont("Helvetica-Bold", 6.5)
    for x, header in zip(xs, headers, strict=True):
        canvas.drawString(x, y, header)
    y -= 10
    canvas.setFont("Helvetica", 5.9)
    for feature in sorted(part.features, key=lambda item: (item.side.value, item.kind.value, item.feature_id)):
        if y < 58:
            _footer(canvas, f"Part-ID {part.part_id} - fortsättning")
            canvas.showPage()
            y = _header(canvas, "DETALJRITNING - feature-tabell forts.", f"Part-ID {part.part_id}")
            canvas.setFont("Helvetica-Bold", 6.5)
            for x, header in zip(xs, headers, strict=True):
                canvas.drawString(x, y, header)
            y -= 10
            canvas.setFont("Helvetica", 5.9)
        size_text = (
            f"Ø{feature.diameter_um/1000:g}" if feature.diameter_um is not None else
            f"{(feature.width_um or 0)/1000:g}×{(feature.length_um or feature.width_um or 0)/1000:g}"
        )
        values = (
            feature.feature_id[:22], feature.side.value, feature.kind.value,
            f"{feature.x_um/1000:g}", f"{feature.y_um/1000:g}", size_text,
            f"{feature.depth_um/1000:g}", f"±{feature.tolerance_um/1000:g}",
        )
        for x, value in zip(xs, values, strict=True):
            canvas.drawString(x, y, str(value))
        y -= 9
        if feature.pattern_count > 1:
            canvas.setFillColor(MUTED)
            canvas.drawString(205, y, f"Mönster: {feature.pattern_count} st · pitch {feature.pitch_um/1000:g} mm")
            canvas.setFillColor(INK)
            y -= 8
    y -= 5
    canvas.setFont("Helvetica-Bold", 7.5)
    canvas.drawString(MARGIN, y, "Kontroll före frisläppning")
    y -= 11
    canvas.setFont("Helvetica", 7)
    for line in (
        "Kontrollera färdigmått, material/tjocklek och A/B-sida mot denna ritning.",
        "DXF och feature-tabell ska överensstämma; avvikelse blockerar tillverkning.",
        "Maskinens verktygskompensering, fixturering och feeds/speeds bestäms av leverantören.",
    ):
        y = _draw_wrapped_text(canvas, line, MARGIN, y, max_chars=112, leading=9) - 1
    _footer(canvas, f"Part-ID {part.part_id} - deterministisk detaljritning")
    return _finish(buffer, canvas)


def assembly_overview_pdf(design: Any) -> bytes:
    """Compact supplier overview of the complete finished assembly."""

    buffer, canvas = _canvas()
    p = design.spec.parameters
    y = _header(canvas, "SAMMANSTÄLLNINGSRITNING", f"Designhash {design.design_hash}")
    canvas.setFillColor(INK)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(MARGIN, y, f"Yttermått: {p.width_um/1000:g} × {p.depth_um/1000:g} × {p.height_um/1000:g} mm (X/Y/Z)")
    y -= 18
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(MARGIN, y, f"Revision {design.spec.revision} · {len(design.parts)} delar · {len(design.joints)} fogar")
    y -= 18
    _draw_exploded_parts(
        canvas, tuple(sorted(design.parts, key=lambda item: item.semantic_key)), frozenset(), ("+x",),
        top=y, height=360,
    )
    y -= 374
    canvas.setFont("Helvetica-Bold", 8)
    canvas.drawString(MARGIN, y, "Part-ID och roll")
    y -= 12
    canvas.setFont("Helvetica", 6.4)
    for part in sorted(design.parts, key=lambda item: item.semantic_key):
        if y < 48:
            _footer(canvas, "Sammanställningsritning - fortsättning")
            canvas.showPage()
            y = _header(canvas, "SAMMANSTÄLLNINGSRITNING - forts.", f"Designhash {design.design_hash}")
            canvas.setFont("Helvetica", 6.4)
        canvas.drawString(MARGIN, y, f"{part.semantic_key} · {part.part_id}")
        y -= 9
    _footer(canvas, "Översikt - detaljmått och bearbetning finns på respektive detaljritning")
    return _finish(buffer, canvas)
