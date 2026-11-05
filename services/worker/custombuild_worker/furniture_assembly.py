"""Revision-bound overall assembly view, fastener inventory and mating map."""

from __future__ import annotations

import csv
import io
from array import array
from collections import Counter
from math import ceil, cos, floor, pi, sin
from xml.sax.saxutils import escape

from custombuild_domain.furniture import ChairIntent, ChestIntent, DiningTableIntent, ShelvingIntent
from custombuild_domain.furniture_catalog import RUNNER_FIXING_SKU
from custombuild_domain.furniture_engine import FurnitureResult
from custombuild_domain.furniture_frames import rotated_yz
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Flowable,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)


def _csv(rows: list[tuple[object, ...]]) -> bytes:
    out = io.StringIO(newline="")
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue().encode("utf-8-sig")


Point = tuple[float, float, float]


def _visible_faces(
    faces: list[tuple[list[Point], bool, bool]], width: int, height: int
) -> Image.Image:
    """Depth-test faces in screen space. Long aprons can cross the top in projection."""
    picture = Image.new("RGB", (width, height), "white")
    pixels = picture.load()
    assert pixels is not None
    depths = array("d", [float("-inf")]) * (width * height)
    for vertices, timber, _ in faces:
        edges = list(zip(vertices, vertices[1:] + vertices[:1], strict=True))
        spans = [(a, b) for a, b in edges if a[1] != b[1]]
        ymin, ymax = min(v[1] for v in vertices), max(v[1] for v in vertices)
        for row in range(max(0, ceil(ymin)), min(height, ceil(ymax))):
            crossings = []
            for (ax, ay, az), (bx, by, bz) in spans:
                if min(ay, by) <= row < max(ay, by):
                    t = (row - ay) / (by - ay)
                    crossings.append((ax + t * (bx - ax), az + t * (bz - az)))
            if len(crossings) < 2:
                continue
            (left, zl), (right, zr) = min(crossings), max(crossings)
            if right <= left:
                continue
            dz = (zr - zl) / (right - left)
            for col in range(max(0, ceil(left)), min(width, floor(right) + 1)):
                z = zl + (col - left) * dz
                index = row * width + col
                if z >= depths[index]:
                    depths[index] = z
                    pixels[col, row] = (156, 187, 171) if timber else (208, 223, 214)
    # Draw only visible edges. A cylindrical side has no vertical facet seams.
    for vertices, _, curved_side in faces:
        for i, (a, b) in enumerate(zip(vertices, vertices[1:] + vertices[:1], strict=True)):
            if curved_side and i % 2:
                continue
            count = max(1, ceil(max(abs(a[0] - b[0]), abs(a[1] - b[1]))))
            for step in range(count + 1):
                t = step / count
                x, y = round(a[0] + t * (b[0] - a[0])), round(a[1] + t * (b[1] - a[1]))
                if (
                    0 <= x < width
                    and 0 <= y < height
                    and a[2] + t * (b[2] - a[2]) >= depths[y * width + x] - 2
                ):
                    pixels[x, y] = (55, 81, 71)
    return picture


class AssemblyView(Flowable):  # type: ignore[misc]  # ReportLab lacks class stubs.
    def __init__(self, result: FurnitureResult):
        super().__init__()
        self.result = result
        self.width, self.height = 480, 218

    def draw(self) -> None:
        faces = []
        for part in self.result.parts:
            size = part.finished_size
            w, d, h = (size.width_um, size.depth_um, size.height_um)
            base = (
                [
                    (w * (1 + cos(i * pi / 48)) / 2, d * (1 + sin(i * pi / 48)) / 2)
                    for i in range(96)
                ]
                if part.circular_outline
                else [(0, 0), (w, 0), (w, d), (0, d)]
            )
            lo = [(*pt, 0) for pt in base]
            hi = [(*pt, h) for pt in base]
            for face_index, poly in enumerate(
                [hi, *[[lo[i - 1], lo[i], hi[i], hi[i - 1]] for i in range(len(lo))]]
            ):
                vertices = []
                for x, y, z in poly:
                    ry, rz = rotated_yz(y, z, part.placement.rotation_x_mdeg)
                    vertices.append(
                        (
                            x + part.placement.x_um,
                            ry + part.placement.y_um,
                            rz + part.placement.z_um,
                        )
                    )
                projected = [(x + 0.48 * y, z + 0.26 * y - 0.11 * x) for x, y, z in vertices]
                area = sum(
                    a[0] * b[1] - b[0] * a[1]
                    for a, b in zip(projected, projected[1:] + projected[:1], strict=True)
                )
                if area <= 0:
                    continue
                face = [
                    (u, v, 0.48 * x - y + 0.3128 * z)
                    for (u, v), (x, y, z) in zip(projected, vertices, strict=True)
                ]
                faces.append(
                    (
                        face,
                        part.semantic_key.startswith("leg-"),
                        bool(part.circular_outline and face_index),
                    )
                )
        if not faces:
            return
        xs = [x for face, _, _ in faces for x, _, _ in face]
        ys = [y for face, _, _ in faces for _, y, _ in face]
        scale = min(
            (self.width - 14) / (max(xs) - min(xs)), (self.height - 10) / (max(ys) - min(ys))
        )
        ox = (self.width - scale * (max(xs) + min(xs))) / 2
        oy = (self.height - scale * (max(ys) + min(ys))) / 2
        resolution = 3
        screen = [
            (
                [
                    (
                        resolution * (ox + x * scale),
                        resolution * (self.height - oy - y * scale),
                        resolution * z * scale,
                    )
                    for x, y, z in face
                ],
                timber,
                curved,
            )
            for face, timber, curved in faces
        ]
        img = _visible_faces(screen, resolution * self.width, resolution * self.height)
        cv = self.canv
        cv.drawImage(ImageReader(img), 0, 0, self.width, self.height)
        cv.setLineWidth(0.5)

        # Key the overall view to the parts table. Show the first drawer as the
        # reference group; the part map lists all repeated instances.
        positions: list[tuple[float, float]] = []
        for i, part in enumerate(self.result.parts, 1):
            if part.semantic_key.startswith("drawer-") and part.semantic_key != "drawer-1-front":
                continue
            size, loc = part.finished_size, part.placement
            ry, rz = rotated_yz(size.depth_um / 2, size.height_um / 2, loc.rotation_x_mdeg)
            cx, cy, cz = loc.x_um + size.width_um / 2, loc.y_um + ry, loc.z_um + rz
            px = ox + (cx + 0.48 * cy) * scale
            py = oy + (cz + 0.26 * cy - 0.11 * cx) * scale
            lx, ly = px, py
            while any((lx - u) ** 2 + (ly - v) ** 2 < 14**2 for u, v in positions):
                ly += 14
            positions.append((lx, ly))
            cv.setStrokeColor(colors.HexColor("#334d45"))
            cv.line(px, py, lx, ly)
            cv.setFillColor(colors.white)
            cv.circle(lx, ly, 6, fill=1, stroke=1)
            cv.setFillColor(colors.black)
            cv.setFont("Helvetica", 7)
            cv.drawCentredString(lx, ly - 2.5, str(i))


def assembly_documents(result: FurnitureResult) -> dict[str, bytes]:
    intent = result.spec.intent
    hardware = result.hardware_profile
    names = {p.part_id: p.semantic_key for p in result.parts}
    counts: Counter[str] = Counter()
    joint_rows: list[tuple[object, ...]] = [
        (
            "joint_id",
            "part_1",
            "mating_face_1",
            "feature_ids_1",
            "part_2",
            "mating_face_2",
            "feature_ids_2",
            "hardware",
            "quantity",
        )
    ]
    for joint in result.joints:
        a, b = joint.members
        if joint.hardware_sku:
            counts[joint.hardware_sku] += joint.hardware_count
        joint_rows.append(
            (
                joint.joint_id,
                names[a.part_id],
                a.mating_face.value,
                " ".join(a.feature_ids),
                names[b.part_id],
                b.mating_face.value,
                " ".join(b.feature_ids),
                joint.hardware_sku or "",
                joint.hardware_count,
            )
        )
    instructions = []
    if isinstance(intent, ChairIntent | DiningTableIntent):
        leg_wood = (
            "valnötsämne" if result.spec.material.material_id == "solid-walnut" else "björkämne"
        )
        instructions = [
            (
                f"Kapa fyra ben ur separat hyvlat {leg_wood} och märk varje ben och sarg "
                "enligt dellistan."
            ),
            (
                "Borra varje angiven fysisk sida. Förborra sargarnas ändträ enligt "
                "kantvyerna. Provpassa en hörnförbindning innan övriga förband monteras."
            ),
            (
                "Skruva ihop främre och bakre ramparen. Förbind dem med de två sidosargarna "
                "och kontrollera diagonalerna."
            ),
            (
                "Skruva fast sitsen eller bordsskivan i sidosargarnas överkanter. "
                "Kontrollera att valt skruvhuvud ryms i planförsänkningen Ø11 × 4 mm "
                "och hamnar under ytan. Kontrollera planhet och samtliga skruvförband."
            ),
        ]
        if isinstance(intent, ChairIntent):
            instructions.append(
                "Montera ryggstödet mellan bakbenen med "
                f"{intent.back_angle_mdeg / 1000:g}° lutning bakåt. "
                "Provbelasta enligt överenskommen provplan."
            )
    elif isinstance(intent, ChestIntent):
        instructions = [
            (
                "Montera och rikta stommen, eventuellt mittstycke samt bakstycket. "
                "Verifiera stommens foglåsning."
            ),
            (
                "Montera varje låda enligt dess separata delgrupp. Numreringen går från "
                "vänster till höger, nedifrån och upp."
            ),
        ]
        if hardware and hardware.drawer_holes_um:
            sku = hardware.catalog_id.split("-")[-1]
            n = intent.drawer_count
            counts["Hettich KA 4532 Silent System " + sku + " (par)"] = n
            counts[RUNNER_FIXING_SKU] += 2 * n * len(hardware.cabinet_holes_um)
            counts[RUNNER_FIXING_SKU] += 2 * n * len(hardware.drawer_holes_um)
            instructions.extend(
                [
                    (
                        "Skruva ihop lådsidorna med lådans fram- och bakstycke. Fäst botten under "
                        "väggarna och fäst den synliga fronten inifrån."
                    ),
                    f"Montera Hettich {sku} på stomme och låda med "
                    "Ø4 × 14 mm skruv. Borrmallens referensplan "
                    "för den infällda lådan är lådkroppens framkant.",
                    (
                        "Pilotdiameter 2,5 mm och pilotdjup 12 mm är konstruktionsval. "
                        "Kontrollera dem i materialprov mot faktisk skruv och skena. Kontrollera "
                        "rörelse, stopp och tipprisk."
                    ),
                ]
            )
        else:
            instructions.append(
                "Layoutbeslaget saknar verifierad borrbild. Fastställ skena, infästningar "
                "och hålmönster före tillverkning."
            )
    elif isinstance(intent, ShelvingIntent):
        instructions = [
            (
                "Identifiera stommens sidor, hyllplan och rygg. Följ den separata "
                "produktionsberedningen för fogarnas monteringsordning och låsning."
            )
        ]
    else:
        instructions = [
            (
                "Placera de två gavlarna och de två sargarna under skivan. Infästningar för "
                "detta äldre gavelbord behöver specificeras före montering."
            )
        ]
    if result.spec.material.material_id == "solid-walnut":
        instructions.append(
            "Mät virkesfukten. Kontrollera fiberriktningen på varje ämne och verifiera "
            "att förband och infästningar tillåter fuktrörelse tvärs fibrerna."
        )
    styles = getSampleStyleSheet()

    def para(t: str, style: str = "BodyText") -> Paragraph:
        return Paragraph(escape(t), styles[style])

    header = f"Revision {result.spec.revision} · {intent.family} · {len(names)} delar"
    story = [
        para("Monteringsöversikt", "Title"),
        para(header),
        para("Design " + result.design_hash),
        para(
            f"Yttermått B × D × H: {intent.width_um / 1000:g} × "
            f"{intent.depth_um / 1000:g} × {intent.height_um / 1000:g} mm."
        ),
        AssemblyView(result),
        para("Monteringsföljd för provpassning", "Heading2"),
    ]
    story.extend(para(f"{i}. {t}") for i, t in enumerate(instructions, 1))
    story.extend(
        [
            Spacer(1, 8),
            para(
                "Granskningsunderlag. Kontrollera råmaterial, passning, stabilitet och "
                "bärighet. Kaplistan anger nominella mått. Borr- och måttoleranser för "
                "slutkontroll avtalas med verkstaden."
            ),
        ]
    )
    if hardware and hardware.drawer_holes_um:
        story.append(para("Skenritning: " + hardware.source))
    story.extend([PageBreak(), para("Delar, material och beslag", "Heading1")])

    def table(rows: list[list[str]], widths: list[int]) -> LongTable:
        tb = LongTable(
            [[para(v, "Normal") for v in row] for row in rows],
            colWidths=widths,
            repeatRows=1,
            hAlign="LEFT",
        )
        tb.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d1e0d8")),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.grey),
                ]
            )
        )
        return tb

    rows = [["Del", "Material", "X × Y × Z (mm)"]]
    for idx, p in enumerate(result.parts, 1):
        size = p.finished_size
        rows.append(
            [
                f"{idx}. {p.semantic_key}",
                p.material_id,
                f"{size.width_um / 1000:g} × {size.depth_um / 1000:g} × {size.height_um / 1000:g}",
            ]
        )
    story.append(table(rows, [165, 110, 201]))
    if counts:
        story.extend(
            [
                para("Beslagslista", "Heading2"),
                table(
                    [
                        ["Artikel / specifikation", "Antal"],
                        *[[k, str(v)] for k, v in sorted(counts.items())],
                    ],
                    [385, 91],
                ),
            ]
        )
    if result.joints:
        story.extend(
            [
                para("Förband per delpar", "Heading2"),
                para(
                    "Enskilda hål-ID, fysiska ytor och kopplingar finns i "
                    "assembly/connections.csv och i delritningarna."
                ),
            ]
        )
        pairs = Counter(
            (
                names[j.members[0].part_id],
                names[j.members[1].part_id],
                j.hardware_sku or j.joint_type.value,
            )
            for j in result.joints
        )
        story.append(
            table(
                [
                    ["Delpar", "Beslag / fog", "Antal"],
                    *[[f"{a} / {b}", k, str(n)] for (a, b, k), n in pairs.items()],
                ],
                [225, 190, 61],
            )
        )
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=42, rightMargin=42, topMargin=36, bottomMargin=38, invariant=1
    )
    doc.build(story)
    return {
        "documents/assembly.pdf": buf.getvalue(),
        "assembly/connections.csv": _csv(joint_rows),
        "assembly/parts.csv": _csv(
            [
                (
                    "part_id",
                    "part_name",
                    "material",
                    "material_version",
                    "grain",
                    "x_um",
                    "y_um",
                    "z_um",
                    "rotation_x_mdeg",
                ),
                *(
                    (
                        p.part_id,
                        p.semantic_key,
                        p.material_id,
                        p.material_version,
                        p.grain_direction.value,
                        p.placement.x_um,
                        p.placement.y_um,
                        p.placement.z_um,
                        p.placement.rotation_x_mdeg,
                    )
                    for p in result.parts
                ),
            ]
        ),
        "bom/hardware.csv": _csv([("item", "quantity"), *sorted(counts.items())]),
    }
