"""S1: five flush walnut-veneered fronts, explicit joinery and catalogued hinges.

This is a selected design inspired by a photograph, not recovered hidden joinery.
All coordinates are millimetres at the builder boundary and integer µm in the model.
"""

from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from .identity import content_hash
from .measured_assembly import MeasuredAssembly, MeasuredFitting, measured_assembly_hash
from .models import FrozenModel
from .sideboard_verification import SideboardAssessment, SideboardReviewInputs, assess_sideboard

SOURCE = "Custombuild S1: vald skänkkonstruktion efter foto IMG_8289; inga fotoavlästa mått."
SOURCES = (
    "https://publications.blum.com/2024/catalogue/en/76/",
    "https://publications.blum.com/2024/catalogue/en/77/",
    "https://publications.blum.com/2024/catalogue/en/153/",
    "https://publications.blum.com/2024/catalogue/en/192/",
)


class SideboardRequest(FrozenModel):
    width_mm: int = Field(default=2400, strict=True, ge=1800, le=2600)
    depth_mm: int = Field(default=450, strict=True, ge=400, le=550)
    height_mm: int = Field(default=750, strict=True, ge=650, le=850)
    leg_height_mm: int = Field(default=180, strict=True, ge=140, le=220)
    door_count: Literal[5] = 5
    material: Literal["walnut-veneered-plywood"] = "walnut-veneered-plywood"
    dimensions_confirmed: bool = False
    front_percent: tuple[Annotated[int, Field(strict=True, ge=10, le=32)], ...] | None = Field(
        default=None, min_length=5, max_length=5, exclude_if=lambda v: v is None
    )
    review: SideboardReviewInputs | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def useful_case(self) -> "SideboardRequest":
        if self.height_mm - self.leg_height_mm < 450:
            raise ValueError("Stommen behöver minst 450 mm höjd för denna beslagssättning.")
        if self.front_percent is not None and sum(self.front_percent) != 100:
            raise ValueError("De fem luckandelarna måste summera till 100 %.")
        return self


class SideboardBlank(FrozenModel):
    part_id: str
    finished_lbt_um: tuple[int, int, int]
    purchase_min_lbt_um: tuple[int, int, int]
    grain_axis: Literal["x", "y", "z"]
    supplier_stock_confirmed: Literal[False] = False


class SideboardConstruction(FrozenModel):
    schema_version: Literal["custombuild.sideboard-construction.v1"] = (
        "custombuild.sideboard-construction.v1"
    )
    request: SideboardRequest
    assembly: MeasuredAssembly
    assembly_sha256: str
    construction_sha256: str
    fittings: tuple[MeasuredFitting, ...]
    hardware: dict[str, int]
    purchase_blanks: tuple[SideboardBlank, ...]
    assembly_sequence: tuple[str, ...]
    assumptions: tuple[str, ...]
    outstanding_acceptance: tuple[str, ...]
    assessment: SideboardAssessment | None = Field(default=None, exclude_if=lambda v: v is None)
    sources: tuple[str, ...] = SOURCES
    physical_cutting_authorized: Literal[False] = False


def construct_sideboard(request: SideboardRequest) -> SideboardConstruction:
    w, d, h, leg = request.width_mm, request.depth_mm, request.height_mm, request.leg_height_mm
    t, divider, door_t, back_t, gap = 24, 18, 19, 6, 3
    body_d, clear_h = d - back_t, h - leg - 2 * t
    bay = (w - 2 * t - 4 * divider) / 5
    parts: dict[str, dict[str, Any]] = {}
    joints: list[dict[str, Any]] = []
    fittings: list[dict[str, Any]] = []
    blanks: list[SideboardBlank] = []
    hardware: Counter[str] = Counter()

    def um(v: float) -> int:
        return round(v * 1000)

    def part(
        key: str,
        name: str,
        size: tuple[float, float, float],
        origin: tuple[float, float, float],
        grain: str = "x",
        solid: bool = False,
        profile: dict[str, Any] | None = None,
        note: str = "",
    ) -> None:
        material = (
            "Massiv valnöt"
            if solid
            else "Valnötsfanerad björkplywood, balanserad fanering på båda sidor"
        )
        parts[key] = dict(
            id=key,
            code=key,
            name=name,
            size_um=tuple(map(um, size)),
            origin_um=tuple(map(um, origin)),
            operations=[],
            material=material,
            blank_lbt_um=tuple(map(um, sorted(size, reverse=True))),
            profile=profile,
            note=note
            or f"Synlig fiberriktning {grain.upper()}. Alla mått inkluderar färdig kantfaner.",
        )
        axis = "xyz".index(grain)
        other = sorted((size[i] for i in range(3) if i != axis), reverse=True)
        lbt = (um(size[axis]), um(other[0]), um(other[1]))
        parts[key]["blank_lbt_um"] = lbt
        # Do not add a thickness allowance to factory-finished veneer panels.
        allowances = (20_000, 4_000, 4_000) if solid else (10_000, 10_000, 0)
        blanks.append(
            SideboardBlank(
                part_id=key,
                finished_lbt_um=lbt,
                purchase_min_lbt_um=tuple(a + b for a, b in zip(lbt, allowances, strict=True)),
                grain_axis=grain,
            )
        )

    def bore(
        key: str,
        op: str,
        face: str,
        axes: str,
        a: float,
        b: float,
        diameter: float,
        depth: float,
        through: bool = False,
        article: str | None = None,
    ) -> dict[str, str]:
        origin = parts[key]["origin_um"]
        parts[key]["operations"].append(
            dict(
                id=op,
                shape="drill",
                face=face,
                axes=axes,
                u_um=um(a) - origin["xyz".index(axes[0])],
                v_um=um(b) - origin["xyz".index(axes[1])],
                width_um=um(diameter),
                height_um=um(diameter),
                depth_um=um(depth),
                through=through,
                source=SOURCE,
                provenance="drawing",
                article=article,
            )
        )
        return dict(part=key, operation=op)

    def dowel(
        a: str,
        b: str,
        face_a: str,
        face_b: str,
        axes: str,
        u: float,
        v: float,
        da: float = 13,
        db: float = 21,
    ) -> None:
        key = f"D-{len(joints) + 1:03}"
        ends = [
            bore(p, key, f, axes, u, v, 8, depth)
            for p, f, depth in ((a, face_a, da), (b, face_b, db))
        ]
        length = 30 if (da, db) == (13, 21) else 40
        article = f"DOWEL-8x{length:g}"
        joints.append(
            dict(
                id=key,
                kind="dowel",
                a=ends[0],
                b=ends[1],
                diameter_um=8000,
                length_um=um(length),
                article=article,
                source=SOURCE,
            )
        )
        hardware[article] += 1

    def screw(
        a: str,
        b: str,
        face: str,
        axes: str,
        u: float,
        v: float,
        through: float,
        diameter: float,
        length: float,
        pilot: float,
        depth: float,
        recess: float = 0,
    ) -> None:
        key = f"S-{len(joints) + 1:03}"
        ends = [
            bore(a, key, face, axes, u, v, diameter + 0.5, through, True),
            bore(b, key, face, axes, u, v, pilot, depth),
        ]
        if recess:
            bore(a, key + "-HEAD", face, axes, u, v, diameter * 2, recess)
        article = f"PAN-SCREW-{diameter:g}x{length:g}"
        joints.append(
            dict(
                id=key,
                kind="screw",
                a=ends[0],
                b=ends[1],
                diameter_um=um(diameter),
                length_um=um(length),
                article=article,
                source=SOURCE,
                **({"head_recess_um": um(recess)} if recess else {}),
            )
        )
        hardware[article] += 1

    part("TOP", "Överskiva", (w, body_d, t), (0, 0, h - t))
    part("BOTTOM", "Botten", (w, body_d, t), (0, 0, leg))
    part("SIDE-L", "Vänster gavel", (t, body_d, clear_h), (0, 0, leg + t), "z")
    part("SIDE-R", "Höger gavel", (t, body_d, clear_h), (w - t, 0, leg + t), "z")
    partitions = [t + (i + 1) * bay + i * divider for i in range(4)]
    if request.front_percent is not None:
        # Shares refer to finished fronts, excluding all six gaps.
        wood_width = w - 2 * t - 6 * gap
        partitions = [
            t
            + gap
            + wood_width * sum(request.front_percent[:i]) / 100
            + (i - 0.5) * gap
            - divider / 2
            for i in range(1, 5)
        ]
    bays = list(zip([t, *(x + divider for x in partitions)], [*partitions, w - t], strict=True))
    for i, x in enumerate(partitions, 1):
        part(
            f"DIV-{i}",
            f"Mellanvägg {i}",
            (divider, body_d - 22.5, clear_h),
            (x, 22.5, leg + t),
            "z",
        )
    for key in ("SIDE-L", "SIDE-R", *(f"DIV-{i}" for i in range(1, 5))):
        p = parts[key]
        x = (p["origin_um"][0] + p["size_um"][0] / 2) / 1000
        for y in (85, body_d - 45):
            dowel("BOTTOM", key, "top", "bottom", "xy", x, y, 13, 21)
            dowel("TOP", key, "bottom", "top", "xy", x, y, 13, 21)

    # Six legs: the central pair supports the long base and is set back from sight.
    leg_x = (80, w / 2 - 25, w - 130)
    for ix, x in enumerate(leg_x):
        for end, y in (("F", 50), ("B", d - 100)):
            key = f"LEG-{ix + 1}-{end}"
            profile = dict(
                kind="convex_prism",
                axes="xz",
                points_um=[
                    (um(10), 0),
                    (um(40), 0),
                    (um(50), um(leg - 70)),
                    (um(50), um(leg)),
                    (0, um(leg)),
                    (0, um(leg - 70)),
                ],
                source=SOURCE,
            )
            part(
                key,
                "Ben i massiv valnöt",
                (50, 50, leg),
                (x, y, 0),
                "z",
                True,
                profile,
                (
                    "Ensidigt avsmalnande profil. Mittparet är ett tillagt stöd, inte "
                    "avläst från fotot."
                ),
            )
            # Two top-down screws avoid the horizontal rail dowels.
            for dy in (16, 34):
                screw("BOTTOM", key, "top", "xy", x + 25, y + dy, t, 5, 60, 3.5, 40, 3)
    for row, y in (("F", 55), ("B", d - 95)):
        for i in range(2):
            x = leg_x[i] + 50
            length = leg_x[i + 1] - x
            key = f"RAIL-{row}-{i + 1}"
            part(key, "Bärande längdslå", (length, 40, 70), (x, y, leg - 70), "x", True)
            for z in (leg - 52, leg - 20):
                dowel(f"LEG-{i + 1}-{row}", key, "right", "left", "yz", y + 20, z, 21, 21)
                dowel(f"LEG-{i + 2}-{row}", key, "left", "right", "yz", y + 20, z, 21, 21)
            for px in (x + length / 3, x + 2 * length / 3):
                screw("BOTTOM", key, "top", "xy", px, y + 20, t, 5, 60, 3.5, 40, 3)

    rear_edges = [0, *(x + divider / 2 for x in partitions), w]
    for i, (left, right) in enumerate(zip(rear_edges, rear_edges[1:], strict=False), 1):
        # Half-millimetre expansion joint lies over the supporting divider.
        x0 = left + (0.25 if i > 1 else 0)
        x1 = right - (0.25 if i < 5 else 0)
        key = f"BACK-{i}"
        part(key, "Heltäckande ryggfält", (x1 - x0, back_t, h - leg), (x0, body_d, leg), "x")
        for x in (x0 + 10, x1 - 10):
            for z, support in ((leg + 12, "BOTTOM"), (h - 12, "TOP")):
                screw(key, support, "back", "xz", x, z, back_t, 3, 16, 2, 14, 2.5)

    for i, (left, right) in enumerate(bays):
        bay = right - left
        left_part = "SIDE-L" if i == 0 else f"DIV-{i}"
        right_part = "SIDE-R" if i == 4 else f"DIV-{i + 1}"
        door_left = t + gap if i == 0 else left - divider / 2 + gap / 2
        door_right = w - t - gap if i == 4 else right + divider / 2 - gap / 2
        door = f"DOOR-{i + 1}"
        part(
            door,
            f"Lucka {i + 1}",
            (door_right - door_left, door_t, clear_h - 2 * gap),
            (door_left, 0, leg + t + gap),
            "x",
            profile=dict(kind="rounded_rectangle", axes="xy", radius_um=1000, source=SOURCE),
            note=(
                "19 mm balanserad valnötsfanerad plywood. R1 på lodräta kanter. Matcha "
                "fanerföljden över alla fem fronter."
            ),
        )
        # Catalogue factory geometry: first inset TB7; other doors half-overlay TB6.
        hinge = "70T3750.TL" if i == 0 else "70T3650.TL"
        tb = 7 if i == 0 else 6
        for n, z in enumerate((leg + t + gap + 80, h - t - gap - 80), 1):
            key = f"HINGE-{i + 1}-{n}"
            cup_x = door_left + tb + 17.5
            mounts = [bore(door, key + "-CUP", "back", "xz", cup_x, z, 35, 13, article=hinge)]
            mounts += [
                bore(
                    door,
                    key + f"-CUP-{j}",
                    "back",
                    "xz",
                    cup_x - 9.5,
                    z + dz,
                    2.5,
                    15,
                    article="609.1500",
                )
                for j, dz in enumerate((-22.5, 22.5), 1)
            ]
            mounts += [
                bore(
                    left_part,
                    key + f"-PLATE-{j}",
                    "right",
                    "yz",
                    57.5,
                    z + dz,
                    2.5,
                    16,
                    article="609.1700",
                )
                for j, dz in enumerate((-16, 16), 1)
            ]
            fittings.append(
                dict(
                    id=key,
                    article=hinge,
                    kind="hinge",
                    connects=(door, left_part),
                    mounts=mounts,
                    accessories=("175H7100", "609.1500", "609.1500", "609.1700", "609.1700"),
                    specification=(
                        f"Blum fjäderlöst, 110 grader. TB {tb} mm; 175H7100, MD0; "
                        "borrning Y=57,5 mm från möbelns front."
                    ),
                    source=SOURCES[1],
                )
            )
            hardware[hinge] += 1
            hardware["175H7100"] += 1
            hardware["609.1500"] += 2
            hardware["609.1700"] += 2
        shelf = f"SHELF-{i + 1}"
        shelf_z = leg + t + clear_h / 2
        part(
            shelf,
            f"Löst hyllplan {i + 1}",
            (bay - 2, body_d - 100, 18),
            (left + 1, 90, shelf_z),
            "x",
        )
        for side, support, face in (("L", left_part, "right"), ("R", right_part, "left")):
            for n, y in enumerate((110, body_d - 30), 1):
                key = f"PIN-{i + 1}-{side}-{n}"
                pin_mount = bore(support, key, face, "yz", y, shelf_z - 2.5, 5, 8)
                fittings.append(
                    dict(
                        id=key,
                        article="SHELF-PIN-5x12",
                        kind="shelf_pin",
                        connects=(shelf, support),
                        mounts=(pin_mount,),
                        specification=(
                            "Cylindrisk hyllbärare Ø5; 8 mm i blindhålet och 4 mm "
                            "utstick. Ej lastklassad artikel."
                        ),
                        source=SOURCE,
                    )
                )
                hardware["SHELF-PIN-5x12"] += 1
        block = f"LATCH-BLOCK-{i + 1}"
        cx = door_right - 35
        z = h - t - 12
        part(
            block,
            "Fästkloss för trycköppnare",
            (44, 100, 24),
            (cx - 22, 22.1, h - t - 24),
            "y",
            True,
        )
        for dx in (-14, 14):
            screw(block, "TOP", "bottom", "xy", cx + dx, 55, 24, 4, 40, 2.5, 20, 2.5)
        key = f"LATCH-{i + 1}"
        latch_mounts = (
            bore(block, key + "-BODY", "front", "xz", cx, z, 10, 76, article="956A1004"),
            bore(door, key + "-CATCH", "back", "xz", cx, z, 2.5, 15, article="609.1500"),
        )
        fittings.append(
            dict(
                id=key,
                article="956A1004",
                kind="push_latch",
                connects=(door, block),
                mounts=latch_mounts,
                accessories=("609.1500",),
                specification=(
                    "Blum lång TIP-ON med magnet och skruvad slutblecksplatta, fjäderlösa"
                    " gångjärn. 3,1 mm nominell bakre frontspalt vid klossen."
                ),
                source=SOURCES[3],
            )
        )
        hardware["956A1004"] += 1
        hardware["609.1500"] += 1

    openings = [
        dict(
            id=f"REAR-{i + 1}",
            label=f"Baköppning {i + 1}",
            target="covered",
            low_um=(um(left), um(body_d), um(leg + t)),
            high_um=(um(right), um(d), um(h - t)),
        )
        for i, (left, right) in enumerate(bays)
    ]
    outstanding = (
        *(
            ("Yttermåtten är valda provmått och behöver bekräftas av beställaren.",)
            if not request.dimensions_confirmed
            else ()
        ),
        (
            "Hela möbelns bärförmåga, stommens limfogar, benförband och tippsäkring "
            "behöver kvalificeras."
        ),
        (
            "Gångjärnens borrbild följer katalogen; full rörelse med leverantörens "
            "3D-beslag samt provpassning återstår."
        ),
        (
            "Kontrollera faktisk skivtjocklek, pilotdiametrar, kantfaner, "
            "gångjärnskantens R1, beslagstoleranser och levererbara format före "
            "tillverkning."
        ),
    )
    assembly = MeasuredAssembly.model_validate(
        dict(
            schema_version="custombuild.measured-assembly.v1",
            title=f"Skänk S1 · {w} × {d} × {h} mm · fem luckor",
            source=SOURCE,
            parts=list(parts.values()),
            joints=joints,
            fittings=fittings,
            backing=dict(part_ids=[f"BACK-{i}" for i in range(1, 6)], openings=openings),
            open_items=outstanding,
        )
    )
    package = SideboardConstruction(
        request=request,
        assessment=(
            assess_sideboard(assembly, request.review, request.dimensions_confirmed)
            if request.review is not None
            else None
        ),
        assembly=assembly,
        assembly_sha256=measured_assembly_hash(assembly),
        construction_sha256="",
        fittings=assembly.fittings,
        hardware=dict(sorted(hardware.items())),
        purchase_blanks=tuple(blanks),
        outstanding_acceptance=outstanding,
        assumptions=(
            (
                "Fem fronter med 3 mm fria springor. 24 mm stomme, 18 mm "
                "mellanväggar/hyllor, 19 mm luckor och 6 mm rygg."
            ),
            "Skivor har valnötsfaner över plywoodkärna; detta är inte en massivträstomme.",
            (
                "Fyra hörnben och två extra mittben. Alla sex är massiva; mittstöden är "
                "ett konstruktionsval."
            ),
            (
                "Ingen standardlängd antas. För skivor läggs 10 mm kapmån i längd/bredd; "
                "tjocklek är färdig skivtjocklek."
            ),
            (
                "Y ökar bakåt, Z uppåt. Luckornas framkant är Y=0. Mellanväggarna börjar "
                "22,5 mm bakom fronten."
            ),
            (
                "Luckorna trycks nominellt 2 mm inåt. Katalogens fulla 110-graders "
                "mekanism är inte ersatt med en förenklad pivotaxel."
            ),
            (
                "Överlagring på mellanvägg: 7,5 mm med TB6 och MD0. Inset vid vänster "
                "gavel: 3 mm spel med TB7 och MD0."
            ),
        ),
        assembly_sequence=(
            "Kontrollera skivtjocklekar och fanerföljd. Märk varje del med sitt ID innan kapning.",
            (
                "Borra alla parade pluggförband från angivna referensytor. Torrmontera "
                "stomme och underrede; limsystem väljs för skivkärnan."
            ),
            "Montera sex ben och fyra längsslåar. Skruva botten ovanifrån i ben och längsslåar.",
            (
                "Montera gavlar och mellanväggar mellan botten och topp. Kontrollera "
                "diagonalmått innan limmet härdar."
            ),
            (
                "Montera fem ryggfält med 0,5 mm fog över mellanväggarna; kontrollera att"
                " samtliga fem baköppningar är täckta."
            ),
            (
                "Montera TIP-ON-klossarna under toppen och de katalogbundna "
                "gångjärnsplattorna. Sätt in de 20 hyllbärarna och fem hyllplan."
            ),
            (
                "Montera två gångjärn per lucka och skruvade slutbleck. Justera springor,"
                " tryckfunktion och öppning. Kontrollera tillverkarens anvisningar."
            ),
            "Verifiera stabilitet och välj vägganpassad tippsäkring före användning.",
        ),
    )
    return package.model_copy(
        update={
            "construction_sha256": content_hash(
                package.model_dump(mode="json", exclude={"construction_sha256"})
            )
        }
    )
