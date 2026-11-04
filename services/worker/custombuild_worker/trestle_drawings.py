"""Six face drawings and operation schedules from the exact construction source."""

from __future__ import annotations

import io
from html import escape
from typing import Any

from custombuild_domain.measured_assembly import MeasuredPart
from custombuild_domain.measured_profiles import ConvexProfile, RoundedProfile
from custombuild_domain.trestle_construction import TrestleConstruction
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import (
    Flowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

FACES = (
    ("left", "yz"),
    ("right", "yz"),
    ("front", "xz"),
    ("back", "xz"),
    ("bottom", "xy"),
    ("top", "xy"),
)
OPPOSITE = {
    "left": "right",
    "right": "left",
    "front": "back",
    "back": "front",
    "bottom": "top",
    "top": "bottom",
}


def face_shapes(part: MeasuredPart, face: str, axes: str) -> list[dict[str, Any]]:
    u, v = ("xyz".index(a) for a in axes)
    profile = part.profile
    outline: dict[str, Any] = dict(
        kind="rect", x=0, y=0, w=part.size_um[u] / 1000, h=part.size_um[v] / 1000, r=0
    )
    if profile and profile.axes == axes:
        if isinstance(profile, ConvexProfile):
            outline = dict(
                kind="polygon", points=[(a / 1000, b / 1000) for a, b in profile.points_um]
            )
        elif isinstance(profile, RoundedProfile):
            outline["r"] = profile.radius_um / 1000
    outline.update(layer="CONTOUR", label=part.id)
    result = [outline]
    for op in part.operations:
        exit_view = op.through and OPPOSITE[op.face] == face
        if op.face != face and not exit_view:
            continue
        coordinates = {op.axes[0]: (op.u_um, op.width_um), op.axes[1]: (op.v_um, op.height_um)}
        a, w = (n / 1000 for n in coordinates[axes[0]])
        b, h = (n / 1000 for n in coordinates[axes[1]])
        layer = ("REFERENCE_EXIT_" if exit_view else "") + f"{op.shape.upper()}_D{op.depth_um}_UM"
        if op.shape == "drill":
            item = dict(kind="circle", x=a, y=b, r=w / 2)
        else:
            item = dict(
                kind="rect",
                x=a - w / 2,
                y=b - h / 2,
                w=w,
                h=h,
                r=min(w, h) / 2 if op.shape == "round_slot" else 0,
            )
        item.update(layer=layer, label=op.id)
        result.append(item)
    return result


def _rect_edges(s: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    x, y, w, h, r = (s[k] for k in ("x", "y", "w", "h", "r"))
    lines = [
        ((x + r, y), (x + w - r, y)),
        ((x + w, y + r), (x + w, y + h - r)),
        ((x + w - r, y + h), (x + r, y + h)),
        ((x, y + h - r), (x, y + r)),
    ]
    arcs = (
        [
            ((x + w - r, y + r), r, 270, 360),
            ((x + w - r, y + h - r), r, 0, 90),
            ((x + r, y + h - r), r, 90, 180),
            ((x + r, y + r), r, 180, 270),
        ]
        if r
        else []
    )
    return [(a, b) for a, b in lines if a != b], arcs


def face_dxf(part: MeasuredPart, face: str, axes: str) -> bytes:
    from ezdxf.filemanagement import new

    document = new("R2010")
    document.units = 4  # millimetres, not an inferred drawing scale
    space = document.modelspace()
    for item in face_shapes(part, face, axes):
        layer = item["layer"]
        if layer not in document.layers:
            document.layers.new(layer)
        attrs = {"layer": layer}
        if item["kind"] == "polygon":
            space.add_lwpolyline(item["points"], close=True, dxfattribs=attrs)
        elif item["kind"] == "circle":
            space.add_circle((item["x"], item["y"]), item["r"], dxfattribs=attrs)
        else:
            lines, arcs = _rect_edges(item)
            for a, b in lines:
                space.add_line(a, b, dxfattribs=attrs)
            for center, radius, start, end in arcs:
                space.add_arc(center, radius, start, end, dxfattribs=attrs)
    output = io.StringIO()
    document.write(output)
    return output.getvalue().encode("utf-8")


def face_svg(part: MeasuredPart, face: str, axes: str) -> bytes:
    w, h = (part.size_um["xyz".index(a)] / 1000 for a in axes)
    pad = max(w, h) * 0.035
    paths = []
    for item in face_shapes(part, face, axes):
        title = f"<title>{escape(item['label'])} | {escape(item['layer'])}</title>"
        attrs = f'fill="none" stroke="{"#273e35" if item["layer"] == "CONTOUR" else "#a45622"}"'
        if item["kind"] == "polygon":
            points = " ".join(f"{a},{b}" for a, b in item["points"])
            paths.append(f'<polygon points="{points}" {attrs}>{title}</polygon>')
        elif item["kind"] == "circle":
            paths.append(
                f'<circle cx="{item["x"]}" cy="{item["y"]}" r="{item["r"]}" '
                f"{attrs}>{title}</circle>"
            )
        else:
            paths.append(
                f'<rect x="{item["x"]}" y="{item["y"]}" width="{item["w"]}" '
                f'height="{item["h"]}" rx="{item["r"]}" {attrs}>{title}</rect>'
            )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{-pad} {-pad} {w + 2 * pad} {h + 2 * pad}">'
        f"<title>{part.id} / {face} / +{axes[0]},+{axes[1]} / mm</title>"
        f'<g transform="translate(0 {h}) scale(1 -1)" stroke-width="{max(w, h) / 1000}">'
        + "".join(paths)
        + "</g></svg>"
    ).encode()


class SixFaces(Flowable):  # type: ignore[misc]  # ReportLab has no class stubs.
    def __init__(self, part: MeasuredPart) -> None:
        super().__init__()
        self.part, self.width, self.height = part, 510, 570

    def draw(self) -> None:
        c = self.canv
        for index, (face, axes) in enumerate(FACES):
            x, y = (index % 2) * 255, 570 - (index // 2 + 1) * 190
            w, h = (self.part.size_um["xyz".index(a)] / 1000 for a in axes)
            scale = min(225 / w, 145 / h)
            c.setFont("Helvetica", 9)
            c.setFillColor(colors.HexColor("#243c33"))
            c.drawString(x, y + 178, f"{face} | +{axes[0]},+{axes[1]} | {w:g} x {h:g} mm")
            c.saveState()
            c.translate(x + 10 + (225 - w * scale) / 2, y + 12 + (145 - h * scale) / 2)
            c.scale(scale, scale)
            c.setLineWidth(0.55 / scale)
            for s in face_shapes(self.part, face, axes):
                c.setStrokeColor(
                    colors.HexColor("#243c33" if s["layer"] == "CONTOUR" else "#a45622")
                )
                if s["kind"] == "polygon":
                    p = c.beginPath()
                    p.moveTo(*s["points"][0])
                    for point in s["points"][1:]:
                        p.lineTo(*point)
                    p.close()
                    c.drawPath(p)
                elif s["kind"] == "circle":
                    c.circle(s["x"], s["y"], s["r"])
                else:
                    c.roundRect(s["x"], s["y"], s["w"], s["h"], s["r"])
            c.restoreState()


def construction_pdf(package: TrestleConstruction, audit: dict[str, Any]) -> bytes:
    out = io.BytesIO()
    styles = getSampleStyleSheet()
    styles["BodyText"].fontSize = 9
    styles["BodyText"].leading = 13
    story: list[Any] = []

    def paragraph(text: str, style: str = "BodyText") -> Any:
        return Paragraph(escape(text.replace("−", "-")).replace("\n", "<br/>"), styles[style])

    def table(rows: list[list[str]], widths: list[int]) -> Any:
        item = Table(
            [[paragraph(cell) for cell in row] for row in rows],
            colWidths=widths,
            repeatRows=1,
            hAlign="LEFT",
        )
        item.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e7ede8")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#bcc8bf")),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        return item

    story += [
        paragraph("CUSTOMBUILD / KONSTRUKTION B1", "Title"),
        paragraph(package.assembly.title, "Heading1"),
        paragraph("Bearbetade delar, förbandsritningar och kapunderlag.", "Heading2"),
        paragraph(
            f"CAD: {audit['parts']} delar, {audit['operations']} bearbetningar, "
            f"{len(audit['joints'])} kontrollerade förband och {audit['pairs_checked']} delpar."
        ),
        paragraph(
            "Geometrikontrollen är godkänd. Detta är inte ett intyg om bärförmåga "
            "eller verkstadens frisläppning för tillverkning."
        ),
        Spacer(1, 12),
    ]
    for item in package.outstanding_acceptance:
        story.append(paragraph(item))
    story += [Spacer(1, 12), paragraph("Vald dimensionering", "Heading2")]
    s = package.sizing
    story.append(
        paragraph(
            f"Skiva {s.top_thickness_mm} mm. Centrisk provlast "
            f"{package.request.central_load_n} N på en 300 mm skivremsa. "
            f"Beräknad nedböjning {s.predicted_deflection_mm:.3f} mm; "
            f"mål {s.deflection_target_mm:.3f} mm. Böjspänning "
            f"{s.bending_stress_mpa:.3f} MPa, ingen fastställd tillåten spänning."
        )
    )
    for item in s.assumptions:
        story.append(paragraph(item))
    story += [paragraph("Montering", "Heading2")]
    for i, item in enumerate(package.assembly_sequence, 1):
        story.append(paragraph(f"{i}. {item}"))
    story += [
        PageBreak(),
        paragraph("Kap- och beslagslista", "Heading1"),
        paragraph(
            "Minsta inköpsämne i fiberriktning L/B/T. Mån: +20/+4/+4 mm. "
            "Limfogsskivan beställs separat. Lagerlängder och leverantör bekräftas vid inköp."
        ),
        table(
            [["Del", "Färdigt ämne (mm)", "Minsta inköpsmått (mm)"]]
            + [
                [
                    b.part_id,
                    " x ".join(f"{v / 1000:g}" for v in b.finished_blank_lbt_um),
                    " x ".join(f"{v / 1000:g}" for v in b.purchase_min_lbt_um),
                ]
                for b in package.purchase_blanks
            ],
            [120, 185, 205],
        ),
        Spacer(1, 12),
        table(
            [["Beslagsspecifikation", "Antal"]]
            + [[h.specification, str(h.quantity)] for h in package.hardware],
            [430, 80],
        ),
        paragraph(
            "Beslagen ovan är dimensionskrav, inte verifierade leverantörsartiklar. "
            "Bult: M8, klass 8.8. Mutterhöjd 6,5 mm. Ingen åtdragningsnivå eller "
            "bärförmåga är fastställd. En fysisk artikel måste uppfylla måtten."
        ),
    ]
    for part in package.assembly.parts:
        story += [
            PageBreak(),
            paragraph(part.id + " / " + part.name, "Heading1"),
            paragraph(part.note),
            paragraph(
                "Vyer i delens positiva axlar, i mm. "
                "Inte spegelvända operatörsvyer. Ämneskontur och bearbetningar per ingångsyta; "
                "genomgångars utgångar visas som referens. Färdig geometri finns i STEP. "
                "Figurerna är inte i utskriftsskala 1:1."
            ),
            SixFaces(part),
            PageBreak(),
            paragraph(part.id + " / bearbetningar och förband", "Heading1"),
        ]
        rows = [["ID", "Yta / axlar / form", "U / V (mm)", "B / H / djup (mm)"]]
        for op in part.operations:
            rows.append(
                [
                    op.id,
                    f"{op.face} / {op.axes}\n{op.shape}",
                    f"{op.u_um / 1000:g} / {op.v_um / 1000:g}",
                    f"{op.width_um / 1000:g} / {op.height_um / 1000:g} / {op.depth_um / 1000:g}"
                    + ("\ngenomgående" if op.through else ""),
                ]
            )
        story += [table(rows, [130, 140, 105, 135]), Spacer(1, 10)]
        for joint in package.assembly.joints:
            if part.id in (joint.a.part, joint.b.part):
                story.append(
                    paragraph(
                        f"{joint.id}: {joint.a.part}/{joint.a.operation} "
                        f"till {joint.b.part}/{joint.b.operation}. {joint.article}. "
                        f"Beslag: {', '.join(joint.fittings)}."
                    )
                )
        story.append(
            paragraph(
                "Urtag anger färdig geometri med räta innerhörn. "
                "Verkstaden väljer verktyg/efterbearbetning och accepterar toleranser "
                "före tillverkning."
            )
        )
    story += [
        PageBreak(),
        paragraph("Underlag och spårbarhet", "Heading1"),
        paragraph("Modellens SHA-256:\n" + package.assembly_sha256),
        paragraph("Hela konstruktionsunderlagets SHA-256:\n" + package.construction_sha256),
        paragraph("Källa: " + package.assembly.source),
    ]
    for source in s.sources:
        story.append(paragraph(source))
    doc = SimpleDocTemplate(
        out,
        pagesize=A4,
        rightMargin=42,
        leftMargin=42,
        topMargin=35,
        bottomMargin=40,
        title=package.assembly.title,
    )

    def footer(canvas: Any, document: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawString(
            42, 22, f"B1 | {package.assembly_sha256[:16]} | tillverkningsacceptans återstår"
        )
        canvas.drawRightString(A4[0] - 42, 22, str(document.page))
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()
