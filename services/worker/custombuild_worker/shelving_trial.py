"""Workshop trial instructions, bound to the measured surfaces of the saved design."""

from __future__ import annotations

from math import isqrt
from typing import Any

from custombuild_domain.bookcase_angles import ANGLE_SOURCE, SCREW_SOURCE
from custombuild_domain.enums import JointType, MaterialType
from custombuild_domain.furniture_engine import FurnitureResult


def shelving_trial_protocol(result: FurnitureResult, preview: dict[str, Any]) -> bytes:
    design = result.shelving_result
    assert design is not None
    handoff = preview["workshop_handoff"]
    p = design.spec.parameters
    lines = [
        "BOKHYLLA · PROVUNDERLAG FÖR VERKSTADSGRANSKNING",
        f"Projekt {result.spec.design_id} · revision {result.spec.revision}",
        f"Design {result.design_hash}",
        "",
        "Avsett bruk i beräkningen",
        f"Stomvolym B × H × D: {p.width_um / 1000:g} × {p.height_um / 1000:g} × "
        f"{p.depth_um / 1000:g} mm. {p.shelf_count} rader, {p.vertical_divider_count + 1} fack.",
        f"Jämnt fördelad nyttolast per hel hyllrad: {p.shelf_load_n} N. Egenvikt tillkommer.",
        "",
        "Före provbeställning",
        *[v["message"] for v in handoff["dimensions"]["issues"]],
    ]
    diagonal_squared = p.height_um**2 + p.depth_um**2
    floor_diagonal = isqrt(diagonal_squared)
    tilt_clearance_mm = (floor_diagonal + (floor_diagonal**2 != diagonal_squared) + 999) // 1000
    lines.append(
        f"Om färdig stomme reses från ryggläge behövs minst {tilt_clearance_mm} mm fri "
        "takhöjd, beräknat på höjd–djup-diagonalen. Kontrollera arbetsutrymme för "
        "gavlar och topp vid stående montering. Rummets verkliga takhöjd är inte uppmätt här."
    )
    if result.spec.installation and result.spec.installation.trim_profile:
        trim = result.spec.installation.trim_profile
        lines.append(
            f"List {trim.height_um} × {trim.width_um} µm. Placering: {', '.join(trim.walls)}."
        )
        lines.append(
            "Kontrollera reserverade mått och verklig uppresnings-/monteringshöjd på plats."
        )
    for group in handoff["stock_requirements"]:
        lines.append(
            f"Bekräfta råformat och batch för {group['material_id']}, "
            f"modelltjocklek {group['measured_thickness_um'] / 1000:g} mm. "
            "Kontrollera varje ämne mot manufacturing/workshop-handoff.json."
        )
    if design.spec.material.material_type == MaterialType.SOLID_WOOD:
        # USDA Wood Handbook, table 13-5, tangential coefficient for black walnut.
        # The projection is per percentage point, not an invented site moisture range.
        movement_mm = p.depth_um / 1000 * 0.00274
        lines.append(
            f"Stommens nominella djup {p.depth_um/1000:g} mm tvärs fibrerna: "
            f"{movement_mm:.3f} mm fri tangentiell rörelse per procentenhet ändrad "
            "träfuktkvot (artmedel, giltigt inom 6–14 %). Detta är fri rörelse; "
            "differensen mellan spår, fast punkt och rygg måste kontrolleras separat."
        )
        lines.append("Källa: https://www.fpl.fs.usda.gov/documnts/fplgtr/fplgtr190/chapter_13.pdf")
        lines.append(
            "Dokumentera art, fiberriktning, uppmätt fuktkvot och förväntat intervall i bruk. "
            "Bestäm rörelsemån vid rygg, hyllspår och infästningar över detta intervall. "
            "Utan intervallet kan ingen rörelsereserv godkännas."
        )
    lines.extend(
        [
            "",
            "Provpassning och mätning",
            "1. Kontrollera millimeter vid DXF/STEP-import och jämför tre ortogonala mått "
            "mot delritningen. Sida A och B använder samma lokala U/V-datum.",
            "2. Välj provbitar ur avsedd batch för varje berörd materialparning. Dokumentera "
            "del-ID, bearbetningssida och feature-ID i inspection/first-article-checks.csv. "
            "Mät tjocklek, spårbredd och spårdjup på flera ställen. Lägg in mätosäkerhet "
            "och avtalad tolerans innan resultatet bedöms. Tom ruta är öppen.",
            "3. Jämför uppmätta värden med operationens nominella mått och deklarerade tolerans. "
            "Kräv att hela mätintervallet ryms inom den avtalade gränsen. Saknas en tolerans "
            "måste konstruktören fastställa den innan detaljen kan godkännas.",
        ]
    )
    dado = next((j for j in design.joints if j.joint_type == JointType.DADO), None)
    if dado:
        lines.append(
            f"Exempel ur revisionen: {dado.joint_id}. Nominellt totalt passningsspel "
            f"{dado.tolerance_um / 1000:g} mm. Ett passande prov styrker inte permanent låsning."
        )
    lines.extend(
        [
            "4. Gå igenom varje steg i documents/assembly.pdf med en torr provmontering. "
            "Kontrollera instick, införingsväg, åtkomst till beslag och omvänd demontering. "
            "Dokumentera varje avvikelse med steg-ID. Avbryt vid fastlåsning eller sprickbildning.",
            f"5. Modellbehov per bärande fog: {design.minimum_retention_shear_load_n} "
            "N i skjuvning. Detta är ett jämförelsevärde för verifierad brukskapacitet, inte en "
            "instruktion att lägga denna provkraft på en osäkrad möbel. Specificera provfixtur, "
            "utdragsfall, säkerhetsfaktor och tillåten deformation före mekaniskt prov.",
            "6. Kontrollera sidostabilitet, tipprisk, golvstöd och infästning i den faktiska "
            "väggen. Dokumentera lastkombination, infästning, underlag och acceptansvillkor.",
            "",
            "Öppna konstruktionskontroller",
        ]
    )
    if design.trial_angles:
        lines.extend(
            [
                f"Vald låsning för prov: {len(design.trial_angles)} GN 970-ALM-30-30-30-B "
                f"och {2 * len(design.trial_angles)} DIN 96 A4 5×16 mm.",
                "Vinkel 30×30×30 mm, 4 mm gods, Ø6,2 mm frigångshål, centrum 18 mm från böjen. "
                "Skruvhuvud Ø10 mm. Ø3,5 × 12,5 mm pilothål är konstruktörens provmått. "
                "De ska verifieras mot skruvens kärna, art, faktisk batch och fuktkvot.",
                "Provets måltolerans ±0,10 mm ger högst 0,142 mm diagonalt centrumfel i "
                "ritningsplanet, mot nominellt 0,6 mm radiespel mellan Ø5 och Ø6,2 mm. "
                "Återstoden måste rymma verkliga artikel- och montageavvikelser. "
                "Pilothålet når 0,5 mm förbi nominell spets. Verifiera innan serieorder.",
                "Mät skruvar, godstjocklek, huvudkontakt och skruvspetsens djup med provdelarna. "
                "Registrera eventuell spricka, bottenkontakt eller genomslag. Prova demontering. "
                "Mät tillgänglig plats för drivverktyg och hållande moment efter fuktcykling.",
                "design/with-mounted-angles.step visar trädelar och nominella vinklar. "
                "Skruvar finns i BOM och borrbild. Gänga och brukskapacitet är ej verifierade.",
                f"Vinkelritning: {ANGLE_SOURCE}",
                f"Skruvdata: {SCREW_SOURCE}",
            ]
        )
    for rule in preview["rules"]["evaluations"]:
        if rule["status"] == "PASS":
            continue
        values = rule["values"]
        reading = (
            f" Beräknat {values['calculated']}, gräns {values['allowed']} {values['unit']}."
            if values.get("calculated") is not None and values.get("allowed") is not None
            else ""
        )
        lines.append(f"{rule['status']} · {rule['rule_id']} · {rule['title']}.{reading}")
    lines.append(
        "Verkstaden väljer maskin, verktyg, uppspänning och CAM. "
        "Konstruktören behöver lösa öppna förband innan en tillverkningsorder läggs."
    )
    return ("\n".join(lines) + "\n").encode()
