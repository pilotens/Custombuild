"""Readable part drawings from the same local geometry as the SVG/DXF exports.

This is a drawing and feature schedule, not a CAM setup or machining order.
All coordinates stay in the declared part-local U/V frame on both faces.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

from custombuild_manufacturing import FeatureKind, ManufacturingFeature, PartSpec, Side
from custombuild_manufacturing.adapters import adapt_design_result
from custombuild_manufacturing.exporters import svg_for_part
from custombuild_manufacturing.model import um_to_mm
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

PAGE_WIDTH, PAGE_HEIGHT = float(A4[0]), float(A4[1])
MARGIN = 36.0
NOTE_WIDTH = PAGE_WIDTH - 2 * MARGIN
NOTE_LINES_PER_PAGE = 25
SVG_NS = "{http://www.w3.org/2000/svg}"
INK = HexColor("#17211c")
MUTED = HexColor("#68746d")
ACCENT = HexColor("#26754f")

_KINDS = {
    FeatureKind.DRILL: "Hål",
    FeatureKind.DRILL_PATTERN: "Hålmönster",
    FeatureKind.POCKET: "Ficka",
    FeatureKind.GROOVE: "Spår",
    FeatureKind.RABBET: "Fals",
    FeatureKind.INNER_CONTOUR: "Innerkontur",
    FeatureKind.OUTER_CONTOUR: "Ytterkontur",
    FeatureKind.ENGRAVE: "Gravering",
    FeatureKind.LABEL: "Märkning",
}
_COLORS = {
    "outline": ("#111111", None),
    "drill": ("#0066cc", None),
    "pocket": ("#2563eb", "#dbeafe"),
    "groove": ("#b45309", "#fef3c7"),
    "rabbet": ("#c2410c", "#ffedd5"),
    "inner-contour": ("#be123c", None),
    "engrave": ("#7c3aed", None),
    "label-mark": ("#111111", "#111111"),
}


@dataclass(frozen=True)
class FeatureNote:
    number: int
    feature: ManufacturingFeature
    lines: tuple[str, ...]


def _wrap(text: str, *, size: float = 8, width: float = NOTE_WIDTH) -> tuple[str, ...]:
    """Wrap by measured width, including long unbroken feature identities."""

    lines: list[str] = []
    current = ""
    for character in text:
        if current and stringWidth(current + character, "Helvetica", size) > width:
            boundary = current.rfind(" ")
            if boundary > len(current) // 2:
                lines.append(current[:boundary])
                current = current[boundary + 1 :] + character
            else:
                lines.append(current)
                current = character
        else:
            current += character
    if current:
        lines.append(current)
    return tuple(lines)


def feature_note_lines(feature: ManufacturingFeature) -> tuple[str, ...]:
    """Exact dimensions, tolerances and every drilling centre, in millimetres."""

    centre = feature.kind in {FeatureKind.DRILL, FeatureKind.DRILL_PATTERN}
    lines = [
        f"U {um_to_mm(feature.x_um)} / V {um_to_mm(feature.y_um)} mm"
        + (" - första hålets centrum" if centre else " - nedre vänstra hörnet"),
    ]
    dimensions = [
        f"{label} {um_to_mm(value)}"
        for label, value in (
            ("Diameter", feature.diameter_um),
            ("Bredd U", feature.width_um),
            ("Längd V", feature.length_um),
            ("Radie", feature.radius_um),
        )
        if value is not None
    ]
    dimensions.append(f"Djup {um_to_mm(feature.depth_um)}")
    lines.append(" | ".join(dimensions) + " mm" + (" | Genomgående" if feature.through else ""))
    tolerance = (
        f"+/- {um_to_mm(feature.tolerance_um)} mm"
        if feature.tolerance_um > 0
        else "EJ ANGIVEN - ska fastställas"
    )
    lines.append(f"Tolerans: {tolerance} | Passningsspel: {um_to_mm(feature.fit_clearance_um)} mm")
    if feature.corner_strategy:
        lines.append(
            f"Hörnstrategi: {feature.corner_strategy} | Avlastningsradie: "
            f"{um_to_mm(feature.corner_relief_radius_um or 0)} mm | Öppna ändar: "
            + (", ".join(feature.open_end_reliefs) or "inga")
        )
    if centre:
        if feature.kind == FeatureKind.DRILL_PATTERN:
            lines.append(
                f"Antal hål: {feature.pattern_count} | Delning: "
                f"{um_to_mm(feature.pitch_um or 0)} mm"
            )
        lines.append(
            "Hålcentrum U/V (mm): "
            + "; ".join(f"{um_to_mm(p.x_um)}/{um_to_mm(p.y_um)}" for p in feature.points())
        )
    return tuple(wrapped for line in lines for wrapped in _wrap(line))


def _note_pages(features: tuple[ManufacturingFeature, ...]) -> tuple[tuple[FeatureNote, ...], ...]:
    pages: list[tuple[FeatureNote, ...]] = []
    current: list[FeatureNote] = []
    used = 0
    for number, feature in enumerate(features, start=1):
        heading = _wrap(f"F{number:02d}  {_KINDS[feature.kind]}  |  {feature.feature_id}")
        remaining = (*heading, *feature_note_lines(feature))
        continued = False
        while remaining:
            title = (f"F{number:02d}  Fortsättning",) if continued else ()
            capacity = NOTE_LINES_PER_PAGE - used - len(title) - 1
            if capacity < min(4, len(remaining)):
                pages.append(tuple(current))
                current, used = [], 0
                continue
            body, remaining = remaining[:capacity], remaining[capacity:]
            fragment = FeatureNote(number, feature, (*title, *body))
            current.append(fragment)
            used += len(fragment.lines) + 1
            continued = True
    if current or not pages:
        pages.append(tuple(current))
    return tuple(pages)


def _text(canvas: Canvas, x: float, y: float, value: str, *, size: float = 8) -> None:
    canvas.setFont("Helvetica", size)
    canvas.drawString(x, y, value)


def _geometry(
    canvas: Canvas,
    part: PartSpec,
    root: ElementTree.Element,
    metadata: dict[str, Any],
    notes: tuple[FeatureNote, ...],
) -> None:
    """Render the already validated SVG primitives in their unmirrored U/V frame."""

    bounds = metadata["emitted_geometry_extents_mm"]
    low_u, low_v = float(bounds["u_min"]), float(bounds["v_min"])
    high_u, high_v = float(bounds["u_max"]), float(bounds["v_max"])
    scale = min(405 / (high_u - low_u), 285 / (high_v - low_v))
    x0 = MARGIN + 80 + (405 - (high_u - low_u) * scale) / 2 - low_u * scale
    y0 = 414 + (285 - (high_v - low_v) * scale) / 2 - low_v * scale
    geometry = root.find(f"{SVG_NS}g")
    if geometry is None:
        raise ValueError("generated drawing has no machining geometry")
    for item in geometry:
        stroke, fill = _COLORS[item.attrib["class"].split()[0]]
        canvas.setStrokeColor(HexColor(stroke))
        canvas.setLineWidth(0.6)
        if fill:
            canvas.setFillColor(HexColor(fill))
        if item.tag == f"{SVG_NS}rect":
            canvas.rect(
                x0 + float(item.attrib["x"]) * scale,
                y0 + float(item.attrib["y"]) * scale,
                float(item.attrib["width"]) * scale,
                float(item.attrib["height"]) * scale,
                fill=int(fill is not None),
                stroke=1,
            )
        elif item.tag == f"{SVG_NS}circle":
            canvas.circle(
                x0 + float(item.attrib["cx"]) * scale,
                y0 + float(item.attrib["cy"]) * scale,
                float(item.attrib["r"]) * scale,
                fill=int(fill is not None),
                stroke=1,
            )
        else:
            raise ValueError("generated drawing contains an unsupported primitive")

    width, height = part.width_um / 1000, part.height_um / 1000
    canvas.setFillColor(MUTED)
    canvas.setStrokeColor(MUTED)
    canvas.setLineWidth(0.3)
    canvas.line(x0, y0 - 12, x0 + width * scale, y0 - 12)
    canvas.setFont("Helvetica", 8)
    canvas.drawCentredString(x0 + width * scale / 2, y0 - 23, f"U {um_to_mm(part.width_um)} mm")
    canvas.line(x0 - 12, y0, x0 - 12, y0 + height * scale)
    canvas.saveState()
    canvas.translate(x0 - 18, y0 + height * scale / 2)
    canvas.rotate(90)
    canvas.drawCentredString(0, 0, f"V {um_to_mm(part.height_um)} mm")
    canvas.restoreState()
    canvas.setFillColor(ACCENT)
    canvas.circle(x0, y0, 2, fill=1, stroke=0)
    _text(canvas, x0 + 4, y0 + 4, "U0/V0", size=7)
    for index, note in enumerate(notes):
        feature = note.feature
        bounds_um = feature.bounds()
        point_u = (
            feature.x_um
            if feature.kind in {FeatureKind.DRILL, FeatureKind.DRILL_PATTERN}
            else (bounds_um.x_um + bounds_um.width_um / 2)
        )
        point_v = (
            feature.y_um
            if feature.kind in {FeatureKind.DRILL, FeatureKind.DRILL_PATTERN}
            else (bounds_um.y_um + bounds_um.height_um / 2)
        )
        label_x, label_y = MARGIN + 19, 680 - index * 25
        canvas.setStrokeColor(MUTED)
        canvas.line(label_x + 15, label_y, x0 + point_u / 1000 * scale, y0 + point_v / 1000 * scale)
        canvas.setFillColor(INK)
        _text(canvas, MARGIN, label_y - 3, f"F{note.number:02d}", size=9)


def part_drawings_pdf(design: Any) -> bytes:
    """Draw every part and face, retaining the exact SVG geometry and feature data."""

    adapted = adapt_design_result(design)
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=A4, invariant=1, pageCompression=1)
    canvas.setTitle("Custombuild - delritningar för verkstadsgranskning")
    canvas.setAuthor("Custombuild deterministic document engine")
    page_number = 0
    for part in sorted(adapted.parts, key=lambda value: value.part_id):
        for side in (Side.A, Side.B):
            # Only our own validated, escaped exporter output is parsed here.
            root = ElementTree.fromstring(svg_for_part(part, side))  # noqa: S314
            metadata_node = root.find(f"{SVG_NS}metadata")
            if metadata_node is None or not metadata_node.text:
                raise ValueError("generated drawing has no part metadata")
            metadata = json.loads(metadata_node.text)
            features = tuple(
                sorted(
                    (feature for feature in part.features if feature.side == side),
                    key=lambda feature: feature.feature_id,
                )
            )
            pages = _note_pages(features)
            for sheet_index, notes in enumerate(pages, start=1):
                page_number += 1
                canvas.setFillColor(INK)
                canvas.setFont("Helvetica-Bold", 17)
                canvas.drawString(MARGIN, 806, f"Delritning - sida {side.value}")
                canvas.setFont("Helvetica", 8)
                canvas.drawRightString(PAGE_WIDTH - MARGIN, 808, f"Blad {sheet_index}/{len(pages)}")
                _text(canvas, MARGIN, 788, f"{part.name} | {part.part_id}")
                _text(
                    canvas,
                    MARGIN,
                    774,
                    f"Revision {design.spec.revision} | Design {design.design_hash}",
                    size=7,
                )
                canvas.setStrokeColor(ACCENT)
                canvas.line(MARGIN, 765, PAGE_WIDTH - MARGIN, 765)
                canvas.setFillColor(MUTED)
                _text(
                    canvas,
                    MARGIN,
                    752,
                    f"Färdig U/V/T: {um_to_mm(part.width_um)} / {um_to_mm(part.height_um)} / "
                    f"{um_to_mm(part.thickness_um)} mm | Antal: {part.quantity}",
                )
                _text(
                    canvas,
                    MARGIN,
                    739,
                    f"Råämne U/V: {um_to_mm(part.blank_width_um)} / "
                    f"{um_to_mm(part.blank_height_um)} mm | "
                    f"Material: {part.material_id}@{part.material_version}",
                    size=7.5,
                )
                _text(
                    canvas,
                    MARGIN,
                    726,
                    f"Fysisk yta: {metadata['physical_face']} | "
                    f"U={part.axis_mapping.u_axis.upper()}, "
                    f"V={part.axis_mapping.v_axis.upper()} | Delfiber: {part.grain_direction}",
                )
                _text(
                    canvas,
                    MARGIN,
                    713,
                    "U0/V0 vid färdigdelens nedre vänstra hörn. V uppåt. Sida B är INTE speglad.",
                )
                _geometry(canvas, part, root, metadata, notes)
                canvas.setFillColor(MUTED)
                _text(
                    canvas,
                    MARGIN,
                    370,
                    "Mått i mm. Skissen är skalad till sidan; mät inte på utskriften.",
                )
                _text(
                    canvas,
                    MARGIN,
                    357,
                    "Numren identifierar detaljer i ritningen. De anger ingen bearbetningsordning.",
                    size=7.5,
                )
                canvas.setStrokeColor(ACCENT)
                canvas.line(MARGIN, 347, PAGE_WIDTH - MARGIN, 347)
                y = 330.0
                if not notes:
                    canvas.setFillColor(INK)
                    _text(
                        canvas, MARGIN, y, "Inga bearbetningsdetaljer är deklarerade på denna sida."
                    )
                    _text(
                        canvas,
                        MARGIN,
                        y - 13,
                        "Konturlinjen visar färdigdelens form och är ingen extra skäroperation.",
                    )
                for note in notes:
                    for line_index, line in enumerate(note.lines):
                        canvas.setFillColor(INK if line_index == 0 else MUTED)
                        _text(canvas, MARGIN, y, line)
                        y -= 10
                    y -= 10
                canvas.setFillColor(MUTED)
                _text(canvas, MARGIN, 52, f"DXF: parts/{part.part_id}/{side.value}.dxf", size=7)
                _text(
                    canvas,
                    MARGIN,
                    40,
                    "Verkstadsgranskning. Material, förband, toleranser och CAM "
                    "måste accepteras inför tillverkning.",
                    size=7,
                )
                canvas.setFont("Helvetica", 7)
                canvas.drawRightString(PAGE_WIDTH - MARGIN, 24, f"Sida {page_number}")
                _text(
                    canvas,
                    MARGIN,
                    24,
                    "Granskningsunderlag. Tillverkning kräver separat frisläppning.",
                    size=7,
                )
                canvas.showPage()
    canvas.save()
    return buffer.getvalue()
