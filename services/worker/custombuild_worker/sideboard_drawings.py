"""S1 workshop drawings and a self-contained component viewer from one source."""

import io
import json
from html import escape
from pathlib import Path
from typing import Any

from custombuild_domain.sideboard_construction import SideboardConstruction
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

from .trestle_drawings import FACES, SixFaces, face_svg


class CabinetViews(Flowable):  # type: ignore[misc]
    def __init__(self, package: SideboardConstruction, rear: bool = False):
        super().__init__()
        self.package = package
        self.rear = rear
        self.width = 510
        self.height = 220

    def draw(self) -> None:
        c = self.canv
        p = self.package
        scale = min(490 / p.request.width_mm, 185 / p.request.height_mm)
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor("#263e35"))
        c.drawString(
            0, 207, "BAKVY - fem heltäckande fält" if self.rear else "FRAMVY - fem luckor, sex ben"
        )
        c.saveState()
        c.translate(10, 10)
        c.scale(scale, scale)
        c.setLineWidth(0.7 / scale)
        wanted = ("BACK-", "LEG-") if self.rear else ("TOP", "BOTTOM", "SIDE-", "LEG-", "DOOR-")
        for part in p.assembly.parts:
            if not part.id.startswith(wanted):
                continue
            assert part.origin_um is not None
            x, _, z = (v / 1000 for v in part.origin_um)
            w, _, h = (v / 1000 for v in part.size_um)
            c.setStrokeColor(colors.HexColor("#3d2d21"))
            c.setFillColor(colors.HexColor("#b88c68" if part.id.startswith("DOOR") else "#d9c8b6"))
            if part.profile and part.profile.kind == "convex_prism" and part.profile.axes == "xz":
                points = part.profile.points_um
                path = c.beginPath()
                path.moveTo(x + points[0][0] / 1000, z + points[0][1] / 1000)
                for a, b in points[1:]:
                    path.lineTo(x + a / 1000, z + b / 1000)
                path.close()
                c.drawPath(path, stroke=1, fill=1)
            else:
                c.rect(x, z, w, h, stroke=1, fill=1)
            if part.id.startswith(("DOOR-", "BACK-")):
                c.setFont("Helvetica", 9 / scale)
                c.setFillColor(colors.HexColor("#2f251e"))
                c.drawCentredString(x + w / 2, z + h / 2, part.id)
        c.restoreState()


def sideboard_pdf(package: SideboardConstruction, audit: dict[str, Any]) -> bytes:
    styles = getSampleStyleSheet()
    styles["BodyText"].fontSize = 9
    styles["BodyText"].leading = 12
    out = io.BytesIO()
    story = []

    def p(text: str, style: str = "BodyText") -> Any:
        return Paragraph(escape(text).replace("\n", "<br/>"), styles[style])

    def table(rows: list[list[str]], widths: list[int]) -> Any:
        t = Table(
            [[p(str(v)) for v in row] for row in rows],
            colWidths=widths,
            repeatRows=1,
            hAlign="LEFT",
        )
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e7ede8")),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c2cdc5")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        return t

    story += [
        p("CUSTOMBUILD / SKÄNK S1", "Title"),
        p(package.assembly.title, "Heading1"),
        p(
            "Vald konstruktion efter fotografi. Alla mått i millimeter. Provmåtten är"
            " inte uppmätta från bilden."
        ),
        CabinetViews(package),
        p("Vad kontrollen omfattar", "Heading2"),
        p(
            f"{audit['parts']} bearbetade trädelar, {audit['operations']} operationer "
            f"och {audit['pairs_checked']} träpar. "
            f"{len(audit['joints'])} plugg-/skruvförband och "
            f"{len(audit['fittings'])} beslagsposter med kontrollerade borrbilder. "
            + (
                "Samtliga fem baköppningar är täckta. Luckornas 2 mm intryckning är fri "
                "från träkollisioner."
            )
        ),
        p(
            (
                "Gångjärnens fulla öppningsrörelse och beslagens kompletta kroppar ingår "
                "inte i denna kontroll. "
            )
            + (
                "CAD-paketet visar träet och dess bearbetningar. Ingen lastklassning "
                "eller fysisk tillverkningsfrigivning följer av resultatet."
            )
        ),
        p("Konstruktion och material", "Heading2"),
    ]
    story += [p(a) for a in package.assumptions]
    assessment = package.assessment
    if assessment is not None and package.request.review is not None:
        review = package.request.review
        story += [
            PageBreak(),
            p("Konstruktionsprov", "Heading1"),
            p(
                f"Jämnt fördelad nyttolast {review.shelf_payload_kg:g} kg/hylla. "
                f"Materialunderlag {assessment.material_profile}."
            ),
            table(
                [["Hyllplan", "Spann mm", "Nedböjning / gräns mm", "Behov per bärare N"]]
                + [
                    [
                        r.part_id,
                        f"{r.span_mm:.2f}",
                        f"{r.deflection_mm:.3f} / {r.limit_mm:.2f}",
                        f"{r.support_demand_n:.1f}",
                    ]
                    for r in assessment.shelf_screens
                ],
                [110, 90, 180, 130],
            ),
            p("Luckornas rörelse", "Heading2"),
            table(
                [["Lucka", "Springa / katalogkrav mm", "Bana", "Hinder / vinkel"]]
                + [
                    [
                        r.part_id,
                        f"{r.available_mm:.2f} / "
                        f"{r.required_mm if r.required_mm is not None else '—'}",
                        {
                            "clear_in_model": "Fri i modellen",
                            "missing_path": "Bana saknas",
                            "envelope_contact": "Kontakt i modellen",
                            "inconclusive": "Osäker marginal",
                        }[m.state],
                        f"{m.obstacle or '—'} / {m.angle_deg if m.angle_deg is not None else '—'}",
                    ]
                    for r, m in zip(assessment.hinge_gaps, assessment.door_motion, strict=True)
                ],
                [80, 150, 145, 135],
            ),
            p(
                "Banan avser en luckas träkontur mot övriga stillastående trädelar. "
                "Katalog: Blum 2024/25 s.77, R1 och MD0."
            ),
            p(
                "Skivbatch: "
                + (review.measurements.batch if review.measurements else "ej registrerad")
                + ". "
                + f"Minsta trä bakom hyllbärarhål {assessment.minimum_divider_web_mm:.2f} mm. "
                f"återstående frontskikt {assessment.minimum_front_skin_mm:.2f} mm."
            ),
        ]
        story += [
            p(
                f"Toleranser: {review.edge_error_mm:g} mm per kant, "
                f"{review.bore_depth_error_mm:g} mm i borrdjup."
            ),
            table(
                [["Hyllplan", "Böjspänning MPa", "Tillåten MPa", "Bärarkapacitet N"]]
                + [
                    [
                        r.part_id,
                        f"{r.bending_stress_mpa:.3f}",
                        f"{review.shelf_material.allowable_bending_mpa:g}"
                        if review.shelf_material
                        else "saknas",
                        (
                            "Se gräns för fyra"
                            if r.support_set
                            else f"{review.pin_working_load_n:g}"
                            if review.pin_working_load_n
                            else "saknas"
                        ),
                    ]
                    for r in assessment.shelf_screens
                ],
                [120, 125, 125, 140],
            ),
        ]
        if package.request.shelf_pin_article:
            story += [
                p(
                    "Häfele 282.43.905. Jämnt fördelad last inklusive hyllans egenvikt. "
                    "Den lägre publicerade uppgiften, 75 lb, används för hela uppsättningen."
                ),
                table(
                    [["Hyllplan", "Total massa kg", "Gräns för fyra kg", "Utfall"]]
                    + [
                        [
                            row.part_id,
                            f"{row.support_set.total_shelf_mass_kg:.2f}",
                            f"{row.support_set.set_limit_kg:.2f}",
                            "Inom kataloggräns"
                            if row.support_set.within_set_limit
                            else "Överskriden",
                        ]
                        for row in assessment.shelf_screens
                        if row.support_set
                    ],
                    [120, 125, 125, 140],
                ),
            ]
        if review.pin_source:
            story.append(p("Hyllbärarens artikel/källa: " + review.pin_source))
        if review.measurements:
            batch = review.measurements
            story.append(
                p(
                    f"Uppmätt rygg/hylla/front/stomme: {batch.back_mm:g} / "
                    f"{batch.shelf_divider_mm:g} / {batch.front_mm:g} / {batch.carcass_mm:g} mm."
                )
            )
        for profile in review.hinge_paths:
            story.append(p(profile.article + " / rörelsekälla: " + profile.source))
        stability = audit["closed_stability"]
        story += [
            p("Statisk tippberäkning", "Heading2"),
            p(stability["basis"]),
            p(
                f"Trämassa {stability['wood_mass_kg']:.1f} kg, "
                f"topplast {review.top_payload_kg:g} kg, sidokraft {review.horizontal_force_n:g} N."
            ),
            table(
                [["Tippkant", "Minsta momentreserv Nm"]]
                + [[r["edge"], f"{r['minimum_reserve_nm']:.2f}"] for r in stability["edges"]],
                [255, 255],
            ),
            p(
                f"Total vertikallast {stability['total_vertical_n']:.1f} N. "
                + {
                    "missing": "Underredets kombinerade provfall saknas.",
                    "wrong_assembly": "Underredets protokoll gäller annan montering.",
                    "wrong_batch": "Protokollet saknar matchande uppmätt skivbatch.",
                    "outside_recorded_case": "Valda laster överskrider underredets protokoll.",
                    "within_recorded_case": "Valda laster ryms i angivet kombinerat provfall.",
                }[stability["base_test_comparison"]]
            ),
            p("Öppna punkter", "Heading2"),
            *[p(f) for f in assessment.findings],
        ]

    story += [
        PageBreak(),
        p("Rygg, montage och kvarvarande acceptans", "Heading1"),
        CabinetViews(package, True),
        table(
            [["Baköppning", "Krav mm²", "Täckt mm²"]]
            + [
                [x["id"], f"{x['required_mm2']:.3f}", f"{x['covered_mm2']:.3f}"]
                for x in audit["back_coverage"]
            ],
            [170, 170, 170],
        ),
        p("Monteringsordning", "Heading2"),
    ]
    story += [p(f"{i}. {a}") for i, a in enumerate(package.assembly_sequence, 1)]
    story += [
        p("Kvar att acceptera", "Heading2"),
        *[p(a) for a in package.outstanding_acceptance],
        PageBreak(),
        p("Kap- och inköpsbehov", "Heading1"),
        p(
            (
                "Färdiga mått inkluderar kantfaner. Plywoodens slutliga tjocklek ska "
                "mätas; ingen hyvling av fanerskiktet förutsätts. "
            )
            + (
                "Skivor har 10 mm kapmån i längd/bredd. Massiva ämnen har +20/+4/+4 mm. "
                "Bekräfta lagerformat och fanerföljd före beställning."
            )
        ),
        table(
            [["Del", "Färdigt L/B/T mm", "Minsta inköpsämne mm", "Fiber"]]
            + [
                [
                    b.part_id,
                    " × ".join(f"{v / 1000:g}" for v in b.finished_lbt_um),
                    " × ".join(f"{v / 1000:g}" for v in b.purchase_min_lbt_um),
                    b.grain_axis.upper(),
                ]
                for b in package.purchase_blanks
            ],
            [115, 165, 180, 50],
        ),
        PageBreak(),
        p("Beslag och förbrukning", "Heading1"),
        table(
            [["Artikel / dimensionskrav", "Antal"]]
            + [[a, str(n)] for a, n in package.hardware.items()],
            [420, 90],
        ),
        p(
            (
                "Blum-artiklar följer angivna katalogsidor. PAN-SCREW, DOWEL och "
                "SHELF-PIN är måttspecifikationer, inte leverantörsartikelnummer. "
            )
            + (
                "Lim, finish, kantfaner och vägganpassad tippsäkring väljs separat. Inga "
                "skruvar räknas en andra gång genom att de syns på två delblad."
            )
        ),
        p("Luckornas anslutningar", "Heading2"),
        p(
            (
                "Lucka 1: två fjäderlösa 70T3750.TL (inset), TB7. Lucka 2-5: två "
                "fjäderlösa 70T3650.TL per lucka (halvöverliggande), TB6. "
            )
            + (
                "Alla har 175H7100, MD0. Två hål med 32 mm delning på globalt Y=57,5 mm. "
                "Koppar Ø35 x 13 mm; fästskruvar med 45 mm delning och 9,5 mm offset mot "
                "kanten. "
            )
            + (
                "Fem långa TIP-ON 956A1004 med skruvat slutbleck används med fjäderlösa "
                "gångjärn. Detta är inte en BLUMOTION-kombination."
            )
        ),
    ]
    for part in package.assembly.parts:
        story += [
            PageBreak(),
            p(part.id + " / " + part.name, "Heading1"),
            p(part.material),
            p(part.note),
            p(
                "Sex vyer i positiva lokala axlar. Inte i utskriftsskala 1:1. Kontur "
                "och bearbetningar per ingångsyta; utgångshål är referenser. STEP "
                "innehåller bearbetad trävolym."
            ),
            SixFaces(part),
            PageBreak(),
            p(part.id + " / operationer och anslutningar", "Heading1"),
        ]
        rows = [["ID", "Yta / axlar", "U / V mm", "Ø / djup mm"]]
        rows += [
            [
                o.id,
                f"{o.face} / {o.axes}",
                f"{o.u_um / 1000:g} / {o.v_um / 1000:g}",
                f"{o.width_um / 1000:g} / {o.depth_um / 1000:g}"
                + (" genom" if o.through else " blind"),
            ]
            for o in part.operations
        ]
        story += [
            table(rows, [145, 110, 125, 130])
            if part.operations
            else p(
                "Inga borrningar krävs på detta lösa hyllplan. Det vilar på fyra "
                "separata hyllbärare."
            ),
            Spacer(1, 8),
        ]
        for j in package.assembly.joints:
            if part.id in (j.a.part, j.b.part):
                story.append(
                    p(
                        f"{j.id}: {j.a.part}/{j.a.operation} <-> "
                        f"{j.b.part}/{j.b.operation}. {j.article}."
                    )
                )
        for f in package.fittings:
            if part.id in f.connects:
                story.append(
                    p(f"{f.id}: {' <-> '.join(f.connects)}. {f.article}. {f.specification}")
                )
                story.append(
                    p("Borrningar: " + ", ".join(m.part + "/" + m.operation for m in f.mounts))
                )
    story += [
        PageBreak(),
        p("Spårbarhet och underlag", "Heading1"),
        p("Modell SHA-256: " + package.assembly_sha256),
        p("Konstruktionsunderlag SHA-256: " + package.construction_sha256),
        p(package.assembly.source),
    ]
    story += [p(s) for s in package.sources]

    def footer(c: Any, doc: Any) -> None:
        c.saveState()
        c.setFont("Helvetica", 8)
        c.drawString(
            42,
            22,
            "S1 | "
            + package.assembly_sha256[:16]
            + " | nominell geometri, ej tillverkningsfrigivning",
        )
        c.drawRightString(A4[0] - 42, 22, str(doc.page))
        c.restoreState()

    SimpleDocTemplate(
        out,
        pagesize=A4,
        leftMargin=42,
        rightMargin=42,
        topMargin=35,
        bottomMargin=40,
        title=package.assembly.title,
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()


def sideboard_viewer(package: SideboardConstruction, audit: dict[str, Any]) -> bytes:
    data = package.model_dump(mode="json")
    data["audit"] = audit
    data["views"] = {
        p.id: [face_svg(p, face, axes).decode() for face, axes in FACES]
        for p in package.assembly.parts
    }
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    template = Path(__file__).with_name("sideboard_viewer.html").read_text(encoding="utf-8")
    sorter = (Path(__file__).parents[3] / "packages/scene-geometry/occlusion.mjs").read_text(
        encoding="utf-8"
    )
    return (
        template.replace("__OCCLUSION__", sorter.replace("export function", "function"))
        .replace("__SIDEB0ARD_PAYLOAD__", payload)
        .encode()
    )
