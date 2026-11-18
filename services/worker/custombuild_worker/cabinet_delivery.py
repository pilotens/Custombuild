"""Verify the delivered door set against the saved order and the J95 wood drilling diagram."""

import csv
import io
import json
from typing import Any

from custombuild_cad.bookcase_readback import verify_bookcase_steps, verify_cabinet_step
from custombuild_domain.cabinet_doors import CATALOGUE, HINGE, PLATE, SCREW, door_mounts
from custombuild_domain.furniture import ShelvingIntent
from custombuild_domain.furniture_engine import FurnitureResult
from custombuild_domain.models import PartInstance
from custombuild_manufacturing.bookcase_test_results import DoorCycleTest
from pypdf import PdfReader

from .bookcase_drawings import verify_panel_dxf
from .bookcase_joint_sheet import joint_measurement_sheet
from .bookcase_readback import (
    indexed_csv_rows,
    order_identity,
    verify_angle_register,
    verify_panel_documents,
    verify_panels_from_order,
)


def installation_register(result: FurnitureResult) -> dict[str, Any]:
    assert result.shelving_result is not None
    mounts = door_mounts(result.parts, result.shelving_result.spec.parameters)
    return {
        **order_identity(result),
        "catalogue": CATALOGUE,
        "hinge": HINGE,
        "plate": PLATE,
        "screw": SCREW,
        "pilot_scope": "Ø3,5 × 15 mm är provmått för vald skruv och valnöt.",
        "doors": [
            {
                "door": m.door.part_id,
                "support": m.support.part_id,
                "hand": "left",
                "hinges": 2,
                "plates": 2,
                "screws": 12,
                "bore_ids": [f.feature_id for f in m.holes],
            }
            for m in mounts
        ],
    }


def _check_opening(
    door: PartInstance, left: PartInstance, right: PartInstance, x: int, width: int, t: int
) -> None:
    p = door.placement
    s = door.finished_size
    if (
        (p.x_um, p.y_um, p.z_um) != (x + t + 2000, 0, t + 2000)
        or (s.width_um, s.depth_um, s.height_um) != (width - 4000, t, 700_000 - 2 * t - 4000)
        or left.placement.x_um != x
        or right.placement.x_um != x + t + width
        or s.width_um > 800_000
        or door.weight_g > 25_000
    ):
        raise ValueError(f"Lucka och gångjärnsgavel avviker från orderfacket: {door.semantic_key}")


def _verify_hinge_connection(
    row: dict[str, str],
    door: PartInstance,
    support: PartInstance,
    door_bores: set[str],
    support_bores: set[str],
) -> None:
    if (
        row["part_1"] != door.semantic_key
        or row["mating_face_1"] != "back"
        or row["part_2"] != support.semantic_key
        or row["mating_face_2"] != "right"
        or sorted(row["feature_ids_1"].split()) != sorted(door_bores)
        or sorted(row["feature_ids_2"].split()) != sorted(support_bores)
        or (len(door_bores), len(support_bores)) != (3, 4)
        or (
            row["hardware"],
            row["quantity"],
            row["plate"],
            row["plate_quantity"],
            row["screw"],
            row["screw_quantity"],
        )
        != (HINGE, "1", PLATE, "1", SCREW, "6")
    ):
        raise ValueError(f"Fel gångjärnsförbindelse i monteringslistan: {row['joint_id']}")


def verify_cabinet_delivery(result: FurnitureResult, files: dict[str, bytes]) -> dict[str, Any]:
    intent = result.spec.intent
    assert isinstance(intent, ShelvingIntent) and intent.base_doors
    n = intent.divider_count + 1
    t = result.spec.material.measured_thickness_um
    assert t is not None
    if len([p for p in result.parts if p.role.value == "cabinet_front"]) != n:
        raise ValueError("Fel antal luckor för det sparade antalet fack.")
    weights = intent.bay_width_ratios_ppm or (1,) * n
    free = intent.width_um - (n + 1) * t
    widths = [free * w // sum(weights) for w in weights]
    # Equal bays in the canonical drawing put the remainder into the first bays.
    for i in sorted(range(n), key=lambda j: -(free * weights[j] % sum(weights)))[
        : free - sum(widths)
    ]:
        widths[i] += 1
    by_key = {p.semantic_key: p for p in result.parts}
    assert result.shelving_result is not None
    verify_panels_from_order(result)
    identity = order_identity(result)
    dxfs = {f"parts/{p.part_id}/{face}.dxf" for p in result.parts for face in ("A", "B")}
    if {name for name in files if name.endswith(".dxf")} != dxfs:
        raise ValueError("Fel antal DXF-sidor i underskåpspaketet.")
    holes = []
    for part in result.parts:
        for side in ("A", "B"):
            holes.extend(
                verify_panel_dxf(files[f"parts/{part.part_id}/{side}.dxf"], part, side, identity)
            )
    angle_bores = {fid for a in result.trial_angles for fid in (a.horizontal_hole, a.upright_hole)}
    read_angle_bores = [h for h in holes if h.feature_id in angle_bores]
    if (
        len(read_angle_bores) != len(angle_bores)
        or {h.feature_id for h in read_angle_bores} != angle_bores
    ):
        raise ValueError("Fel pilothål i vinkelregistret.")
    holes = [h for h in holes if h.feature_id not in angle_bores]
    actual = {(h.part_id, h.face, h.u_um, h.v_um, h.diameter_um, h.depth_um): h for h in holes}
    expected = set()
    connections = indexed_csv_rows(files, "assembly/connections.csv", "joint_id")
    hinge_ids = {f"J95-{bay + 1}-{pos}" for bay in range(n) for pos in (1, 2)}
    if connections.keys() != hinge_ids | {j.joint_id for j in result.joints}:
        raise ValueError("Fel förbindelser i underskåpets monteringslista.")
    register = json.loads(files["assembly/door-hardware.json"])
    if register != installation_register(result):
        raise ValueError("Luckornas beslagsregister avviker från beställningen.")
    manual_pages = [
        p.extract_text() for p in PdfReader(io.BytesIO(files["documents/assembly.pdf"])).pages
    ]
    manual = "\n".join(manual_pages)
    if result.trial_angles:
        angles = [
            i
            for i, page in enumerate(manual_pages)
            if "Fäst låsvinklarna efter stommonteringen" in page
        ]
        hanging = [
            i for i, page in enumerate(manual_pages) if "Haka på luckan i öppet läge" in page
        ]
        if (
            not hanging
            or len(angles) != (len(result.trial_angles) + 7) // 8
            or max(angles) >= min(hanging)
        ):
            raise ValueError("Monteringsmanualens vinkelblad måste föregå påhakning av luckorna.")
    samples = indexed_csv_rows(files, DOOR_TRIAL_CSV, "leaf")
    if set(samples) != {f"cabinet-front-{i}" for i in range(n)}:
        raise ValueError("Luckprovets mätblad ska innehålla varje beställt fack exakt en gång.")
    x = 0
    for i, width in enumerate(widths):
        door = by_key[f"cabinet-front-{i}"]
        left = by_key["left-side" if i == 0 else f"base-side-{i}"]
        right = by_key["right-side" if i == n - 1 else f"base-side-{i + 1}"]
        row = samples[door.semantic_key]
        nominal_sample = {
            "revision": str(result.spec.revision),
            "design_hash": result.design_hash,
            "part_id": door.part_id,
            "support_id": left.part_id,
            "width_um": str(width - 4000),
            "height_um": str(700_000 - 2 * t - 4000),
            "thickness_um": str(t),
            "minimum_test_mass_g": str(door.weight_g),
        }
        if any(row.get(k) != v for k, v in nominal_sample.items()) or any(
            row.get(k) != "" for k in DOOR_MEASUREMENTS
        ):
            raise ValueError(
                f"Mätraden för {door.semantic_key} avviker från ordern eller innehåller provvärden."
            )
        _check_opening(door, left, right, x, width, t)
        door_pages = [
            " ".join(page.split())
            for page in manual_pages
            if f"IN {door.semantic_key} · {door.part_id}" in page
        ]
        if (
            len(door_pages) != 1
            or f"Luckinfästning: 2 gångjärn mellan {door.semantic_key} och {left.semantic_key}."
            not in door_pages[0]
            or f"Beslag: 2 × {HINGE}, 2 × {PLATE}, 12 × {SCREW}." not in door_pages[0]
            or "Haka på luckan i öppet läge" not in door_pages[0]
            or any(label not in door_pages[0] for label in ("Luckans baksida", "Gavelns insida"))
            or any(f"J95-{i + 1}-{pos}" not in door_pages[0] for pos in (1, 2))
            or "Monteringsbana" in door_pages[0]
            or "AssemblyGraph-förband i steget: 0" in door_pages[0]
        ):
            raise ValueError("Monteringsmanualen saknar rätt luckinfästning och motdel i steget.")
        leaf_width, leaf_height = width - 4000, 700_000 - 2 * t - 4000
        for position, z in enumerate((80_000, leaf_height - 80_000), 1):
            door_bores = {(door.part_id, "back", 27_000, z, 40_000, 15_000)}
            for dz in (-24_000, 24_000):
                door_bores.add((door.part_id, "back", 39_500, z + dz, 3500, 15_000))
            support_bores = set()
            for y in (t + 42_000, t + 68_000):
                for dz in (-16_000, 16_000):
                    support_bores.add((left.part_id, "right", y, t + 2000 + z + dz, 3500, 15_000))
            expected.update(door_bores | support_bores)
            _verify_hinge_connection(
                connections[f"J95-{i + 1}-{position}"],
                door,
                left,
                {actual[key].feature_id for key in door_bores if key in actual},
                {actual[key].feature_id for key in support_bores if key in actual},
            )
        expected.add((door.part_id, "back", leaf_width - 45_000, leaf_height - 65_000, 30_000, t))
        x += t + width
    if actual.keys() != expected or len(actual) != len(holes):
        raise ValueError("Hålbilden passar inte J95 i båda mötande delar.")
    rows = indexed_csv_rows(files, "bom/hardware.csv", "item")
    if {k: int(r["quantity"]) for k, r in rows.items()} != {
        HINGE: 2 * n,
        PLATE: 2 * n,
        SCREW: 12 * n,
        **(
            {
                result.trial_angles[0].article: len(result.trial_angles),
                result.trial_angles[0].screw: 2 * len(result.trial_angles),
            }
            if result.trial_angles
            else {}
        ),
    }:
        raise ValueError("Fel artikel eller antal för underskåpsluckorna.")
    if (
        manual.count(f"Beslag: 2 × {HINGE}, 2 × {PLATE}, 12 ×") != n
        or "FRONT EJ MONTERINGSBAR" in manual
        or "5x16" not in manual
    ):
        raise ValueError("Monteringsmanualen saknar rätt gångjärn, platta eller skruv.")
    verify_panel_documents(result, files)
    if result.trial_angles:
        verify_angle_register(result, files)
        files["inspection/fogprov.csv"] = joint_measurement_sheet(result, read_angle_bores)
        verify_bookcase_steps(
            result, files["design/model.step"], files["design/with-mounted-angles.step"]
        )
    else:
        verify_cabinet_step(result, files["design/model.step"])
    return {
        **identity,
        "doors": n,
        "hinges": 2 * n,
        "plates": 2 * n,
        "screws": 12 * n,
        "door_and_plate_bores": len(holes),
        "hinge_connections": len(hinge_ids),
        "carcass_joints_without_retention": [
            j.joint_id for j in result.shelving_result.unretained_carcass_joints
        ],
        "trial_retention_angles": len(result.trial_angles),
        "trial_retention_pilots": len(read_angle_bores),
        "step_wood_parts": len(result.parts),
        "dxf_faces": len(dxfs),
        "physical_cutting_authorized": False,
        "scope": "Closed timber, bore geometry and catalogue hinge-side reveal. "
        "No complete hardware sweep or screw withdrawal rating in this material.",
    }


# The lab returns one DoorCycleTest per opening; blank cells are not evidence.
DOOR_MEASUREMENTS = tuple(k for k in DoorCycleTest.model_fields if k != "leaf")
DOOR_TRIAL_CSV = "inspection/luckprov-per-fack.csv"


def cabinet_trial_rows(result: FurnitureResult) -> bytes:
    assert result.shelving_result is not None
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        (
            "revision",
            "design_hash",
            "leaf",
            "part_id",
            "support_id",
            "width_um",
            "height_um",
            "thickness_um",
            "minimum_test_mass_g",
            *DOOR_MEASUREMENTS,
        )
    )
    for m in door_mounts(result.parts, result.shelving_result.spec.parameters):
        size = m.door.finished_size
        writer.writerow(
            (
                result.spec.revision,
                result.design_hash,
                m.door.semantic_key,
                m.door.part_id,
                m.support.part_id,
                size.width_um,
                size.height_um,
                m.door.actual_thickness_um,
                m.door.weight_g,
                *("" for _ in DOOR_MEASUREMENTS),
            )
        )
    return stream.getvalue().encode("utf-8-sig")


def cabinet_trial_sheet(result: FurnitureResult) -> bytes:
    return (
        f"TYPPROV J95 · revision {result.spec.revision} · {result.design_hash}\n"
        f"Använd {DOOR_TRIAL_CSV}. Prova varje angivet fack med rätt lucka, gavel och "
        "grannluckor på delad mellanvägg. Ta med väggmall där väggen gränsar till facket. "
        "Dokumentera om grannluckan är öppen eller stängd och registrera sämsta fallet.\n"
        "Raden anger ordermått i µm och minsta provmassa i gram. Väg varje provlucka och "
        "redovisa faktisk massa under provet. Ange batch, fuktkvot och beslag i rapporten. "
        "Fastställ brukskategori, erforderliga cykler och skruvkapacitet före provet. "
        "Registrera uppnådd öppningsvinkel och osäkerhet i tusendels grad, minsta frigång "
        "och osäkerhet i µm, genomförda cykler och kvarvarande skruvkapacitet i N. "
        "Krav per fack: provmassa minst modellmassan, vinkel minus osäkerhet minst 93°, "
        "frigång minus osäkerhet större än noll, samt cykler och skruvkapacitet minst "
        "provspecifikationens krav. Tomma mätfält ska fyllas från verkligt prov.\n"
        f"Beslag per lucka: 2 × {HINGE}, 2 × {PLATE}, 12 × {SCREW}.\n"
        "1. Mät hålcentrum på båda bearbetningssidorna från respektive U/V-datum. "
        "Jämför med PDF och DXF. Hålens projekterade läges- och måttolerans är ±0,10 mm. "
        "Kontrollera Ø40 × 15 kopp, Ø3,5 × 15 pilot och Ø30 genomgående grepp. "
        "Mät faktiskt kvarvarande trä på provbit och kontrollera att varken skruv "
        "eller borr bryter igenom baksidan på något blindhål.\n"
        "2. Skruva och snäpp fast som i manualsidan för luckans del-ID. "
        "Kontrollera plant anliggande metall och att båda plattorna låser. "
        "Justera till nominellt 2 mm spel runt luckan. Mät minsta fria avstånd "
        "mot båda gavlarna, angränsande lucka och vägg under hela öppningen till 93° "
        "och tillbaka. Prova även med grannluckan öppen. "
        "Godkänt geometriprov kräver positiv frigång efter avdrag för mätosäkerhet, "
        "nådd ändvinkel, stängd front i liv och ingen genomträngning eller spricka.\n"
        "3. Dokumentera nedhängning, skruvlossning och sprickor före och efter belastning. "
        "En separat fastställd last- och cykelprovning samt fuktrörelsebedömning behövs "
        "för att styrka beständighet och utdragning. Katalogens beslagstal avser inte "
        "denna skruv i det använda virkespartiet.\n"
        "Ett godkänt geometriprov styrker endast detta utförande och provade mått, "
        "beslag, skruv och material. Övriga lastfall, andra orderbredder, stomlåsning, "
        "tippskydd och platsmontering omfattas inte.\n"
        f"Katalogritning: {CATALOGUE}\n"
    ).encode()
