"""Verify the delivered door set against the saved order and the J95 wood drilling diagram."""

import csv
import io
import json
from typing import Any

from custombuild_cad.bookcase_readback import verify_cabinet_step
from custombuild_domain.cabinet_doors import CATALOGUE, HINGE, PLATE, SCREW, door_mounts
from custombuild_domain.furniture import ShelvingIntent
from custombuild_domain.furniture_engine import FurnitureResult
from custombuild_domain.models import PartInstance
from pypdf import PdfReader

from .bookcase_drawings import verify_panel_dxf
from .bookcase_readback import order_identity, verify_panel_documents


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
    actual = {(h.part_id, h.face, h.u_um, h.v_um, h.diameter_um, h.depth_um) for h in holes}
    expected = set()
    register = json.loads(files["assembly/door-hardware.json"])
    if register != installation_register(result):
        raise ValueError("Luckornas beslagsregister avviker från beställningen.")
    manual_pages = [
        p.extract_text() for p in PdfReader(io.BytesIO(files["documents/assembly.pdf"])).pages
    ]
    manual = "\n".join(manual_pages)
    x = 0
    for i, width in enumerate(widths):
        door = by_key[f"cabinet-front-{i}"]
        left = by_key["left-side" if i == 0 else f"base-side-{i}"]
        right = by_key["right-side" if i == n - 1 else f"base-side-{i + 1}"]
        _check_opening(door, left, right, x, width, t)
        door_pages = [
            " ".join(page.split()) for page in manual_pages
            if f"IN {door.semantic_key} · {door.part_id}" in page
        ]
        if (
            len(door_pages) != 1
            or f"Luckinfästning: 2 gångjärn mellan {door.semantic_key} och {left.semantic_key}."
            not in door_pages[0]
            or "AssemblyGraph-förband i steget: 0" in door_pages[0]
        ):
            raise ValueError("Monteringsmanualen saknar rätt luckinfästning och motdel i steget.")
        leaf_width, leaf_height = width - 4000, 700_000 - 2 * t - 4000
        for z in (80_000, leaf_height - 80_000):
            expected.add((door.part_id, "back", 27_000, z, 40_000, 15_000))
            for dz in (-24_000, 24_000):
                expected.add((door.part_id, "back", 39_500, z + dz, 3500, 15_000))
            for y in (t + 42_000, t + 68_000):
                for dz in (-16_000, 16_000):
                    expected.add((left.part_id, "right", y, t + 2000 + z + dz, 3500, 15_000))
        expected.add((door.part_id, "back", leaf_width - 45_000, leaf_height - 65_000, 30_000, t))
        x += t + width
    if actual != expected or len(actual) != len(holes):
        raise ValueError("Hålbilden passar inte J95 i båda mötande delar.")
    rows = list(csv.DictReader(io.StringIO(files["bom/hardware.csv"].decode("utf-8-sig"))))
    counts = {r["item"]: int(r["quantity"]) for r in rows}
    if len(rows) != 3 or counts != {HINGE: 2 * n, PLATE: 2 * n, SCREW: 12 * n}:
        raise ValueError("Fel artikel eller antal för underskåpsluckorna.")
    if (
        manual.count(f"Beslag: 2 × {HINGE}, 2 × {PLATE}, 12 ×") != n
        or "FRONT EJ MONTERINGSBAR" in manual
        or "5x16" not in manual
    ):
        raise ValueError("Monteringsmanualen saknar rätt gångjärn, platta eller skruv.")
    verify_panel_documents(result, files)
    verify_cabinet_step(result, files["design/model.step"])
    return {
        **identity,
        "doors": n,
        "hinges": 2 * n,
        "plates": 2 * n,
        "screws": 12 * n,
        "door_and_plate_bores": len(holes),
        "step_wood_parts": len(result.parts),
        "dxf_faces": len(dxfs),
        "physical_cutting_authorized": False,
        "scope": "Closed timber, bore geometry and catalogue hinge-side reveal. "
        "No complete hardware sweep or screw withdrawal rating in this material.",
    }


def cabinet_trial_sheet(result: FurnitureResult) -> bytes:
    assert result.shelving_result is not None
    mounts = door_mounts(result.parts, result.shelving_result.spec.parameters)
    mount = max(mounts, key=lambda m: m.door.finished_size.width_um)
    door = mount.door
    return (
        f"TYPPROV J95 · revision {result.spec.revision} · {result.design_hash}\n"
        f"Montera den bredaste luckan {door.semantic_key} ({door.part_id}) med "
        f"{mount.support.semantic_key} ({mount.support.part_id}) i en komplett öppning. "
        "Ta med nästa lucka på gemensam mellanvägg och en väggmall på ritningens avstånd.\n"
        f"Luckans B × H × T är {door.finished_size.width_um / 1000:g} × 660 × 18 mm. "
        f"Beräknad ämnesvikt {door.weight_g / 1000:g} kg. Väg provluckan. Dokumentera "
        "virkesparti, fuktkvot, uppmätt tjocklek, beslagsartikel och verklig skruv.\n"
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
