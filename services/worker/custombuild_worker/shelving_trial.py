"""Workshop trial instructions, bound to the measured surfaces of the saved design."""

from __future__ import annotations

from math import isqrt
from typing import Any

from custombuild_domain.bookcase_angles import ANGLE_SOURCE, SCREW_SOURCE
from custombuild_domain.bookcase_motion import AssemblySpace
from custombuild_domain.enums import JointType, MaterialType
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.furniture_engine import FurnitureResult
from custombuild_manufacturing.bookcase_qualification import bookcase_scope
from custombuild_manufacturing.model import um_to_mm


def shelving_trial_protocol(
    result: FurnitureResult, preview: dict[str, Any], space: AssemblySpace | None = None
) -> bytes:
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
        f"Stomvolym B × H × D: {um_to_mm(p.width_um)} × {um_to_mm(p.height_um)} × "
        f"{um_to_mm(p.depth_um)} mm. {p.shelf_count} rader, {p.vertical_divider_count + 1} fack.",
        f"Jämnt fördelad nyttolast per hel hyllrad: {p.shelf_load_n} N. Egenvikt tillkommer.",
        "",
        "Före provbeställning",
        *[v["message"] for v in handoff["dimensions"]["issues"]],
    ]
    diagonal_squared = p.height_um**2 + p.depth_um**2
    floor_diagonal = isqrt(diagonal_squared)
    tilt_clearance_mm = (floor_diagonal + (floor_diagonal**2 != diagonal_squared) + 999) // 1000
    ceiling = (
        result.spec.installation.measured_ceiling_height_um if result.spec.installation else None
    )
    lines.append(
        f"Om färdig stomme reses från ryggläge behövs minst {tilt_clearance_mm} mm fri "
        "takhöjd, beräknat på höjd–djup-diagonalen. Kontrollera arbetsutrymme för "
        "gavlar och topp vid stående montering. "
        + (
            f"Angiven fri takhöjd: {um_to_mm(ceiling)} mm."
            if ceiling is not None
            else "Rummets verkliga takhöjd är inte uppmätt här."
        )
    )
    if space:
        lines.append(
            "Stöd botten på plan yta. Håll varje ryggfält, avdelare och gavel lodrätt med "
            "separata stöd "
            "mot tippning tills stommen är säkrad. Avsätt plats utanför införingsbanorna för "
            "fixturer och lyfthjälp. "
            "Provpassa frontöppningen i varje frontmatat spår. Med framkanterna inriktade "
            "(±0,10 mm) ska "
            "hyllan eller avdelaren ha minst 0,05 mm kvar till det bakre stoppet. Mät mot "
            "inspection/groove-fits.json."
        )
        lines.extend(
            [
                space.description(),
                "Mät det tillgängliga utrymmet mot assembly/required-space.json före provmontage.",
            ]
        )
    if result.spec.installation and result.spec.installation.trim_profile:
        trim = result.spec.installation.trim_profile
        wall_names = {"left": "vänster vägg", "right": "höger vägg", "rear": "bakvägg"}
        walls = ", ".join(wall_names[w] for w in trim.walls) or "ej angivet"
        height = f"{um_to_mm(trim.height_um)} mm" if trim.height_um is not None else "ej angiven"
        projection = f"{um_to_mm(trim.width_um)} mm" if trim.width_um is not None else "ej angivet"
        lines.append(f"Listens höjd {height}, utstick {projection}. Placering: {walls}.")
        lines.append(
            "Kontrollera reserverade mått och verklig uppresnings-/monteringshöjd på plats."
        )
    for group in handoff["stock_requirements"]:
        lines.append(
            f"Bekräfta råformat och batch för {group['material_id']}, "
            f"modelltjocklek {um_to_mm(group['measured_thickness_um'])} mm. "
            "Kontrollera varje ämne mot manufacturing/workshop-handoff.json."
        )
    if design.spec.material.material_type == MaterialType.SOLID_WOOD:
        # USDA Wood Handbook, table 13-5, tangential coefficient for black walnut.
        # The projection is per percentage point, not an invented site moisture range.
        movement_mm = p.depth_um / 1000 * 0.00274
        lines.append(
            f"Stommens nominella djup {um_to_mm(p.depth_um)} mm tvärs fibrerna: "
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
            f"{um_to_mm(dado.tolerance_um)} mm. Ett passande prov styrker inte permanent låsning."
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
                "Spårprov: jämför varje fog med inspection/groove-fits.json. "
                "Mät skuren spårbotten mot ingångsytan (±0,05 mm); mät motdelens ände och "
                "ingångsytans läge från monteringsdatum (vardera ±0,10 mm). Målen ger "
                "0,30 mm nominell bottenmån och minst 0,05 mm vid kombinerade avvikelser. "
                "Det gäller delarna på sina monteringsdatum. Den lösa plywoodryggen kan sedan "
                "ligga mot en spårbotten. För den åt båda håll och kontrollera minsta kvarvarande "
                "instick i motsatt spår enligt minimum_floating_capture_um. "
                "Upprepa vid avtalad bruksfukt. "
                "Uppmätt lokal tjocklek ska ligga inom ±0,10 mm av modellens tjocklek. "
                "Nominellt 0,50 mm totalt breddspel lämnar minst 0,35 mm under dessa provmål. "
                "Styrk verklig processduglighet innan ett intervall godkänns.",
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
                "inspection/fogprov.csv parar ihop vinkelns två pilothål och visar respektive "
                "DXF, fysisk yta, lokalt centrum U/V och centrum i monterad position. Mät från "
                "färdigdelens U0/V0 på angiven sida. Fyll i uppmätt U, V, diameter, djup och "
                "mätosäkerhet för båda delarna av samma joint_id. Kolumnerna measured_* "
                "är tomma tills provbitarna faktiskt har mätts.",
                f"Vinkelritning: {ANGLE_SOURCE}",
                f"Skruvdata: {SCREW_SOURCE}",
            ]
        )
    if design.trial_angles:
        rules = {r["rule_id"]: r for r in preview["rules"]["evaluations"]}
        deflection = rules["CB-DEFLECTION-001"]["values"]
        lines.extend(
            [
                "",
                "Provgräns för denna konstruktion",
                "inspection/kvalificeringsprov.json anger orderns jämförelsevärden och vilka "
                "provresultat som krävs. Kapaciteter avser dimensionerande brukskapacitet efter "
                "säkerhetsfaktorer. Utdragskravet hämtas från förbandsprovets lastfall. "
                "Vid ny order jämför appen typunderlagets resultat med orderns krav.",
                f"Omfattnings-ID {bookcase_scope(FurnitureWorkspace(design=result.spec))}.",
                "Registrera verklig stom- och ryggbatch, fuktkvot, temperatur, fiberriktning, "
                "vägguppbyggnad, infästning, golv, jiggar, lyftmetod och uppmätta "
                "platsmått i första "
                "provordern. Byt exempelvärden mot mätningar och generera om innan ett "
                "intyg binds.",
                f"Belastningsfall: {p.shelf_load_n} N jämnt fördelat över varje hel "
                "rad, plus egenvikt. "
                f"Mät nedböjning mitt i det längsta fria spannet från olastat läge. "
                f"Regelgräns {um_to_mm(deflection['allowed'])} mm. Uppmätt värde "
                "inklusive mätosäkerhet "
                "ska ligga inom gränsen vid avtalad konditionering och belastningstid. "
                "Prova i säkrad provfixtur enligt provledarens fastställda metod.",
                f"Förband: dimensionerande brukskapacitet i skjuvning minst "
                f"{design.minimum_retention_shear_load_n} N per bärande fog enligt aktuell regel. "
                "Använd verifierad brukskapacitet efter säkerhetsfaktorer. För utdrag "
                "ska provledaren "
                "härleda kravet från belastningens riktning och excentricitet och "
                "dokumentera marginal. "
                "En enstaka brottlast eller en katalogritning ger inget kapacitetsintyg.",
                "Stabilitet och montering: prova den avsedda golv- och vägginfästningen "
                "med avtalade "
                "statiska och horisontella lastfall. Dokumentera uppmätt rörelse, "
                "skada, tippmarginal, "
                "hand- och fixturutrymme i varje steg, samt slutlig åtdragning. Ett godkänt utfall "
                "kräver uppfyllda gränser från provledarens förhandsfastställda provplan.",
                "Kvalificering avser exakt provad geometri, batcher, last, plats och regelversion. "
                "Ett annat kund-ID eller revisionsnummer kan återanvända samma prov. Ändrade mått, "
                "batcher, lastsättning, vägg, eller regelsystem behöver ny täckning. "
                "Genomfört passningsprov ensamt kvalificerar ingen order för bruk.",
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
