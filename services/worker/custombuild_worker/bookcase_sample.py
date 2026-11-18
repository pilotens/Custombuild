"""Revision-bound bench kit using the actual widest leaf and its mating order parts."""

import csv
import hashlib
import io
import json
from typing import Any
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from custombuild_cad.bookcase_sample import sample_step
from custombuild_domain.cabinet_doors import HINGE, MIN_DOOR_THICKNESS_UM, PLATE, SCREW, door_mounts
from custombuild_domain.furniture_catalog import resolve_material
from custombuild_domain.furniture_engine import FurnitureResult
from custombuild_manufacturing.model import um_to_mm
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from .cabinet_delivery import DOOR_TRIAL_CSV
from .part_drawings import part_drawings_pdf

SAMPLE_PATH = "inspection/fackprov.zip"


def _selected_rows(data: bytes, field: str, keys: set[str]) -> bytes:
    rows = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    assert rows.fieldnames is not None
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=rows.fieldnames, lineterminator="\n")
    writer.writeheader()
    selected = [row for row in rows if row[field] in keys]
    if not selected:
        raise ValueError(f"Provet saknar matchande rader för {field}.")
    writer.writerows(selected)
    return out.getvalue().encode("utf-8-sig")


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _protocol_pdf(instructions: str) -> bytes:
    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=A4,
        title="Prov av hyllfog, rygg och lucka",
        topMargin=44,
        bottomMargin=44,
        leftMargin=45,
        rightMargin=45,
        invariant=1,
    )
    styles = getSampleStyleSheet()
    paragraphs = instructions.split("\n\n")
    title, design_hash = paragraphs[0].splitlines()
    story = [
        Paragraph(escape(title), styles["Heading1"]),
        Paragraph(escape(design_hash), styles["Code"]),
        Spacer(1, 10),
    ]
    for body in paragraphs[1:]:
        story.extend([Paragraph(escape(body), styles["BodyText"]), Spacer(1, 7)])
    doc.build(story)
    return out.getvalue()


def _measurement_sheet(
    result: FurnitureResult,
    fits: list[dict[str, Any]],
    leaf_id: str,
) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        (
            "revision",
            "design_hash",
            "del_eller_fog",
            "kontroll",
            "enhet",
            "nedre_grans",
            "ovre_grans",
            "villkor",
            "uppmatt",
            "matosakerhet",
            "batch",
            "fuktkvot_procent",
            "temperatur_C",
            "resultat",
        )
    )

    def row(
        ref: str,
        label: str,
        unit: str,
        low: str,
        high: str = "",
        condition: str = "inklusive gräns och mätosäkerhet",
    ) -> None:
        writer.writerow(
            (
                result.spec.revision,
                result.design_hash,
                ref,
                label,
                unit,
                low,
                high,
                condition,
                "",
                "",
                "",
                "",
                "",
                "",
            )
        )

    for fit in fits:
        ref = fit["joint_id"]
        row(
            ref,
            "Spel mot spårbotten i nominellt monteringsläge",
            "mm",
            um_to_mm(fit["minimum_bottom_gap_um"]),
            um_to_mm(fit["maximum_bottom_gap_um"]),
        )
        row(
            ref,
            "Spårbredd minus delens uppmätta tjocklek",
            "mm",
            um_to_mm(fit["minimum_total_width_clearance_um"]),
        )
        if fit["minimum_front_stop_gap_um"] is not None:
            row(
                ref,
                "Spel till bakre stopp vid front i liv",
                "mm",
                um_to_mm(fit["minimum_front_stop_gap_um"]),
            )
        if fit["minimum_floating_capture_um"] is not None:
            row(
                ref,
                "Kvarvarande instick när motsatt kant bottnar",
                "mm",
                um_to_mm(fit["minimum_floating_capture_um"]),
            )
    door = next(p for p in result.parts if p.part_id == leaf_id)
    for position in ("nedre", "övre"):
        row(
            leaf_id,
            f"Färdig tjocklek vid {position} gångjärnskopp",
            "mm",
            um_to_mm(MIN_DOOR_THICKNESS_UM),
            um_to_mm(door.actual_thickness_um + door.stock_tolerance_um),
        )
    row(
        leaf_id,
        "Minsta frigång under hela lucksvepet",
        "mm",
        "0",
        condition="strikt större efter avdrag för mätosäkerhet",
    )
    row(leaf_id, "Uppnådd öppningsvinkel", "grader", "93")
    return out.getvalue().encode("utf-8-sig")


def bay_sample(result: FurnitureResult, files: dict[str, bytes]) -> bytes:
    shelving = result.shelving_result
    assert shelving is not None
    mounts = door_mounts(result.parts, shelving.spec.parameters)
    # For equal leaf widths prefer the middle bay, with shared divider grooves when available.
    mount = max(
        mounts,
        key=lambda m: (
            m.door.finished_size.width_um,
            -abs(2 * m.door.instance_index - len(mounts) + 1),
        ),
    )
    bay = mount.door.instance_index
    by_name = {p.semantic_key: p for p in result.parts}
    by_id = {p.part_id: p for p in result.parts}
    shelf, back, floor = (
        by_name[key] for key in (f"shelf-r0-b{bay}", f"back-b{bay}", f"base-bottom-{bay}")
    )
    fits = json.loads(files["inspection/groove-fits.json"])["joints"]

    def hosts(mate_id: str) -> list[str]:
        found = [
            f["host_id"]
            for f in fits
            if f["inserted_part_id"] == mate_id and f["normal_axis"] == "x"
        ]
        if len(set(found)) != 2 or len(found) != 2:
            raise ValueError("Provets inskjutna del behöver två motstående spår.")
        return sorted(found, key=lambda pid: by_id[pid].placement.x_um)

    upper, lower = hosts(shelf.part_id), hosts(floor.part_id)
    if hosts(back.part_id) != upper or mount.support.part_id != lower[0]:
        raise ValueError("Spår och gångjärnsplatta måste höra till samma provfack.")
    clear = by_id[upper[1]].placement.x_um - (
        by_id[upper[0]].placement.x_um + by_id[upper[0]].finished_size.width_um
    )
    ids = {*upper, *lower, mount.door.part_id, shelf.part_id, back.part_id}
    selected_fits = [
        f
        for f in fits
        if f["host_id"] in upper
        and f["inserted_part_id"] in {shelf.part_id, back.part_id}
        and f["normal_axis"] == "x"
    ]
    angles = [
        a for a in result.trial_angles if a.horizontal_id == shelf.part_id and a.upright_id in upper
    ]
    if len(angles) != 2:
        raise ValueError("Provhyllan saknar ett par GN 970-vinklar.")
    joint_ids = {a.joint_id for a in angles}
    identity = dict(design_hash=result.design_hash, revision=result.spec.revision)
    output = {
        "design/fack.step": sample_step(files["design/model.step"], ids),
        "ritningar.pdf": part_drawings_pdf(
            result, part_ids=ids, qualification_note="Fackprov på fixtur. Se LAS-MIG.txt."
        ),
        "kaplista.csv": _selected_rows(files["bom/cut-list.csv"], "part_id", ids),
        "placering.csv": _selected_rows(files["assembly/parts.csv"], "part_id", ids),
        "kontrollmatt.csv": _selected_rows(
            files["inspection/first-article-checks.csv"], "part_id", ids
        ),
        "vinkelprov.csv": _selected_rows(files["inspection/fogprov.csv"], "joint_id", joint_ids),
        "luckprov.csv": _selected_rows(files[DOOR_TRIAL_CSV], "part_id", {mount.door.part_id}),
        "sparpassning.json": _json({**identity, "units": "micrometres", "joints": selected_fits}),
    }
    intent = json.loads(files["manufacturing/manufacturing-intent.json"])
    output["bearbetning.json"] = _json(
        {
            **identity,
            "units": intent["units"],
            "coordinate_contract": intent["coordinate_contract"],
            "parts": [p for p in intent["parts"] if p["part_id"] in ids],
        }
    )
    for pid in ids:
        for side in ("A", "B"):
            path = f"parts/{pid}/{side}.dxf"
            output[path] = files[path]

    def mm(um: int) -> str:
        return um_to_mm(um).replace(".", ",")

    stock = mount.door.actual_thickness_um
    min_gap = min(f["minimum_bottom_gap_um"] for f in selected_fits)
    reveal = (clear - mount.door.finished_size.width_um) // 2
    output["LAS-MIG.txt"] = (
        f"FACKPROV I VALNÖT | Fack {bay + 1} av {len(mounts)} | Revision "
        f"{result.spec.revision}\n{result.design_hash}\n\n"
        "Prova en hyllfog, ryggens infångning i sidospåren och en fullstor lucka. "
        f"Kaplista och A/B-DXF omfattar {len(ids)} trädelar från ordern. Alla längder "
        "i ritningarna är i mm. "
        "Öppna ritningar.pdf och design/fack.step. placering.csv anger monteringslägen i µm.\n\n"
        f"Material enligt kaplistan: {mm(stock)} mm massiv valnöt och "
        f"rygg av {resolve_material(result.spec.back_material).name}, "
        f"verklig måltjocklek {mm(back.actual_thickness_um)} mm. "
        f"Beslag för provet: 2 × {HINGE}, 2 × {PLATE}, 12 × {SCREW}, 2 × "
        f"{angles[0].article}, 4 × {angles[0].screw}. "
        "Ange verklig batch, fuktkvot, ytbehandling och mätosäkerhet för varje material.\n\n"
        f"1. Fixera övre sidor {by_id[upper[0]].semantic_key} och {by_id[upper[1]].semantic_key} "
        f"i en styv fixtur med {mm(clear)} mm mellan insidorna. Använd gemensam "
        "framkant och nivån i placering.csv. "
        "Stötta delarna i fixturen under hela provet. Kontrollera faktisk spårbredd, "
        "djup och stoppläge före montering. "
        f"Skjut in {shelf.semantic_key} framifrån. Mät spårdjup och effektivt instick "
        "från samma referensyta. "
        "Spårdjup minus instick, efter avdrag för mätosäkerheterna, ska vara minst "
        f"{mm(min_gap)} mm. "
        "Kontrollera också breddspel och stoppmån mot respektive rad i "
        "sparpassning.json. Montera hyllans två vinklar enligt vinkelprov.csv.\n\n"
        f"2. För in {back.semantic_key} i båda vertikala spåren med fixturen öppen upptill. "
        "Förskjut ryggen försiktigt till vardera spårbotten. Motsatt kant ska vara "
        "kvar i sitt spår med minst "
        "minimum_floating_capture_um enligt sparpassning.json, inklusive "
        "mätosäkerhet. Provet omfattar sidospåren. "
        "Infångning upptill och nedtill i den kompletta möbeln provas separat.\n\n"
        f"3. Fixera nedre sidor {by_id[lower[0]].semantic_key} och "
        f"{by_id[lower[1]].semantic_key} i orderns läge. "
        f"Montera {mount.door.semantic_key} med plattorna i den vänstra gavelns ritade hål. "
        f"Luckans fria sidospringa i stängt läge är nominellt {mm(reveal)} mm per sida. "
        "Kontrollera färdig tjocklek vid båda gångjärnskopparna, minst 18,00 mm "
        "efter avdrag för mätosäkerhet. "
        "Prova hela svepet till minst 93 grader inklusive vinkelosäkerhet. Mät "
        "minsta frigång under rörelsen efter ytbehandling. "
        "Frigång minus mätosäkerhet ska vara större än noll. För ändlägen med vägg "
        "eller grannlucka krävs motsvarande hinder i fixturen.\n\n"
        "Fyll i provmatning.csv och luckprov.csv. Kontrollera delarna med "
        "kontrollmatt.csv och vinkelprov.csv. Registrera temperatur, fuktkvot "
        "och provade klimatlägen. Saknade värden är öppna provpunkter. Vid "
        "avvikelse: stoppa, dokumentera del-ID och mätvärde. "
        "Delprovet prövar passning och montering i ett fack. Last, tippsäkring, "
        "klimatintervall, långtidsprov, samtliga luckors svep "
        "och de långa delarnas bearbetning behöver underlag för den kompletta "
        "ordern. Verkstaden väljer maskin och CAM."
    ).encode()
    output["Provprotokoll.pdf"] = _protocol_pdf(output["LAS-MIG.txt"].decode())
    output["provmatning.csv"] = _measurement_sheet(result, selected_fits, mount.door.part_id)
    output["manifest.json"] = _json(
        {
            **identity,
            "schema_version": "custombuild.bay-sample.v1",
            "bay": bay + 1,
            "bay_count": len(mounts),
            "clear_width_um": clear,
            "part_ids": sorted(ids),
            "production_qualified": False,
            "physical_cutting_authorized": False,
            "files": {k: hashlib.sha256(v).hexdigest() for k, v in sorted(output.items())},
        }
    )
    archive = io.BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        for name, data in sorted(output.items()):
            bundle.writestr(ZipInfo(name), data, compress_type=ZIP_DEFLATED)
    return archive.getvalue()
