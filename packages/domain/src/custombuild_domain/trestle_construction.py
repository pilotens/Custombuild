"""Generate every part, paired operation and fastener of a selected trestle design."""

from collections import Counter
from collections.abc import Sequence
from math import ceil, hypot
from typing import Any, Literal

from .identity import content_hash
from .measured_assembly import MeasuredAssembly, measured_assembly_hash
from .models import FrozenModel
from .trestle_planning import TrestleRequest, TrestleSizing, size_trestle

SOURCE = "Custombuild bockbord B1: vald bultad konstruktion, inte fotoavlästa förband."
MATERIAL = "Massiv amerikansk svartvalnöt (Juglans nigra), rakfibrig och torkad"


class PurchaseBlank(FrozenModel):
    part_id: str
    finished_blank_lbt_um: tuple[int, int, int]
    purchase_min_lbt_um: tuple[int, int, int]
    grain: str
    supplier_stock_confirmed: Literal[False] = False


class HardwareLine(FrozenModel):
    specification: str
    quantity: int


class TrestleConstruction(FrozenModel):
    schema_version: Literal["custombuild.trestle-construction.v1"] = (
        "custombuild.trestle-construction.v1"
    )
    request: TrestleRequest
    sizing: TrestleSizing
    assembly: MeasuredAssembly
    assembly_sha256: str
    construction_sha256: str
    purchase_blanks: tuple[PurchaseBlank, ...]
    hardware: tuple[HardwareLine, ...]
    assembly_sequence: tuple[str, ...]
    outstanding_acceptance: tuple[str, ...]
    physical_cutting_authorized: Literal[False] = False


def construct_trestle(request: TrestleRequest) -> TrestleConstruction:
    sizing = size_trestle(request)
    length, width = request.length_mm, request.width_mm
    top_z = request.height_mm - sizing.top_thickness_mm
    parts: dict[str, dict[str, Any]] = {}
    joints: list[dict[str, Any]] = []
    grains: dict[str, str] = {}

    def um(value: float) -> int:
        return round(value * 1000)

    def part(
        key: str,
        name: str,
        size: tuple[float, float, float],
        origin: tuple[float, float, float],
        blank: tuple[float, float, float],
        grain: str,
        profile: dict[str, Any] | None = None,
    ) -> None:
        parts[key] = dict(
            id=key,
            code=key,
            name=name,
            size_um=tuple(map(um, size)),
            origin_um=tuple(map(um, origin)),
            blank_lbt_um=tuple(map(um, blank)),
            material=MATERIAL,
            profile=profile,
            operations=[],
            note=f"{grain} Separat ämne; inköpsmått med bearbetningsmån i kaplistan.",
        )
        grains[key] = grain

    def polygon(axes: str, points: Sequence[tuple[float, float]]) -> dict[str, Any]:
        return dict(
            kind="convex_prism",
            axes=axes,
            points_um=[tuple(map(um, p)) for p in points],
            source=SOURCE,
        )

    def operation(
        key: str,
        op_id: str,
        face: str,
        axes: str,
        world_u: float,
        world_v: float,
        w: float,
        h: float,
        depth: float,
        shape: str = "drill",
        through: bool = False,
    ) -> dict[str, str]:
        origin = parts[key]["origin_um"]
        parts[key]["operations"].append(
            dict(
                id=op_id,
                shape=shape,
                face=face,
                axes=axes,
                u_um=um(world_u) - origin["xyz".index(axes[0])],
                v_um=um(world_v) - origin["xyz".index(axes[1])],
                width_um=um(w),
                height_um=um(h),
                depth_um=um(depth),
                through=through,
                provenance="drawing",
                source=SOURCE,
            )
        )
        return dict(part=key, operation=op_id)

    def bolt(
        key: str,
        a: str,
        b: str,
        face: str,
        axes: str,
        u: float,
        v: float,
        bolt_length: int,
        recess: bool = False,
    ) -> None:
        n = next(i for i in range(3) if "xyz"[i] not in axes)
        ends = [
            operation(p, key, face, axes, u, v, 9, 9, parts[p]["size_um"][n] / 1000, through=True)
            for p in (a, b)
        ]
        if recess:
            operation(a, key + "-HEAD", "bottom", axes, u, v, 30, 30, 8)
            operation(b, key + "-NUT", "top", axes, u, v, 30, 30, 16)
        joints.append(
            dict(
                id=key,
                kind="through_bolt",
                a=ends[0],
                b=ends[1],
                source=SOURCE,
                article=f"HEX-M8x{bolt_length}-8.8",
                diameter_um=8000,
                length_um=bolt_length * 1000,
                head_allowance_um=2000,
                head_recess_um=8000 if recess else 0,
                nut_recess_um=16000 if recess else 0,
                fittings=["WASHER-8.4x24x2", "WASHER-8.4x24x2", "HEX-NUT-M8-6.5"],
            )
        )

    part(
        "TOP",
        "Bordsskiva",
        (length, width, sizing.top_thickness_mm),
        (0, 0, top_z),
        (length, width, sizing.top_thickness_mm),
        "Fibrer längs X. Limfogsskiva.",
        dict(kind="rounded_rectangle", axes="xy", radius_um=60000, source=SOURCE),
    )
    shift = top_z / 8
    leg_length = hypot(top_z, shift)
    leg_blank = (ceil(leg_length + 70 * shift / leg_length), ceil(70 * top_z / leg_length), 70)
    for end, leg_x, cross_x in (("L", 250, 320), ("R", length - 320, length - 390)):
        part(
            f"LEG-{end}-F",
            "Främre lutande ben",
            (70, 70 + shift, top_z),
            (leg_x, 105, 0),
            leg_blank,
            "Fibrer längs benets lutning, Y/Z = 1/8.",
            polygon("yz", [(0, 0), (70, 0), (70 + shift, top_z), (shift, top_z)]),
        )
        part(
            f"LEG-{end}-B",
            "Bakre lutande ben",
            (70, 70 + shift, top_z),
            (leg_x, width - 175 - shift, 0),
            leg_blank,
            "Fibrer längs benets lutning, Y/Z = −1/8.",
            polygon("yz", [(shift, 0), (70 + shift, 0), (70, top_z), (0, top_z)]),
        )
        for level, z, height in (("TOP", top_z - 140, 140), ("LOW", 160, 120)):
            cross = f"CROSS-{end}-{level}"
            part(
                cross,
                "Övre tvärbalk" if level == "TOP" else "Nedre tvärbalk",
                (70, width - 210, height),
                (cross_x, 105, z),
                (width - 210, height, 70),
                "Fibrer längs Y.",
            )
            for side in ("F", "B"):
                for index, hole_z in enumerate((z + (height - 56) / 2, z + (height + 56) / 2)):
                    y = 140 + hole_z / 8
                    if side == "B":
                        y = width - y
                    a, b = (
                        (f"LEG-{end}-{side}", cross) if end == "L" else (cross, f"LEG-{end}-{side}")
                    )
                    bolt(
                        f"FRAME-{end}-{level}-{side}-{index + 1}",
                        a,
                        b,
                        "left",
                        "yz",
                        y,
                        hole_z,
                        160,
                    )

    for level, z, height in (("TOP", top_z - 140, 140), ("LOW", 160, 120)):
        tie = f"TIE-{level}"
        part(
            tie,
            "Övre längsbalk" if level == "TOP" else "Nedre långslå",
            (length - 640, 50, height),
            (320, width / 2 - 25, z),
            (length - 640, height, 50),
            "Fibrer längs X.",
        )
        for end, x in (("L", 320), ("R", length - 390)):
            cross = f"CROSS-{end}-{level}"
            operation(cross, "LAP", "top", "xy", x + 35, width / 2, 70, 50, height / 2, "notch")
            operation(
                tie, "LAP-" + end, "bottom", "xy", x + 35, width / 2, 70, 50, height / 2, "notch"
            )
            bolt(
                f"LAP-{end}-{level}",
                cross,
                tie,
                "bottom",
                "xy",
                x + 35,
                width / 2,
                height - 10,
                recess=True,
            )

    rise, run = top_z - 160, min(350, (length - 1060) / 2)
    diagonal = hypot(rise, run)
    brace_blank = (ceil(diagonal + 80 * run / diagonal), ceil(80 * rise / diagonal), 40)
    for end, brace_x in (("L", 430), ("R", length - 510 - run)):
        points = (
            [(0, 0), (80, 0), (80 + run, rise), (run, rise)]
            if end == "L"
            else [(run, 0), (80 + run, 0), (80, rise), (0, rise)]
        )
        brace = f"BRACE-{end}"
        part(
            brace,
            "Snedsträva",
            (80 + run, 40, rise),
            (brace_x, width / 2 - 65, 160),
            brace_blank,
            "Fibrer längs strävans lutning.",
            polygon("xz", points),
        )
        for level, z in (("LOW", 230), ("TOP", top_z - 60)):
            t = (z - 160) / rise
            hole_x = brace_x + 40 + run * (t if end == "L" else 1 - t)
            bolt(f"BRACE-{end}-{level}", brace, f"TIE-{level}", "front", "xz", hole_x, z, 110)

    mounts: list[tuple[str, float, float, bool]] = [
        (f"CROSS-{end}-TOP", x, width / 2 + offset, True)
        for end, x in (("L", 355), ("R", length - 355))
        for offset in (-150, 150)
    ]
    mounts.append(("TIE-TOP", length / 2, width / 2, False))
    for index, (beam, mount_x, mount_y, sliding) in enumerate(mounts):
        key = f"MOUNT-{index + 1}"
        slot_length = sizing.mounting_slot_length_mm if sliding else 5.5
        slot_width = 9 if sliding else 5.5
        operation(
            beam,
            key + "-ACCESS",
            "bottom",
            "xy",
            mount_x,
            mount_y,
            30,
            slot_length + 25 if sliding else 30,
            120,
            "round_slot" if sliding else "drill",
        )
        a_end = operation(
            beam,
            key,
            "bottom",
            "xy",
            mount_x,
            mount_y,
            slot_width,
            slot_length,
            140,
            "round_slot" if sliding else "drill",
            True,
        )
        b_end = operation("TOP", key, "bottom", "xy", mount_x, mount_y, 3.5, 3.5, 24)
        joints.append(
            dict(
                id=key,
                kind="screw",
                a=a_end,
                b=b_end,
                source=SOURCE,
                article="WOOD-SCREW-PAN-5x45",
                fittings=["WASHER-5.5x24x2"],
                diameter_um=5000,
                length_um=45000,
                head_allowance_um=2000,
                head_recess_um=120000,
            )
        )

    outstanding = [
        "Bultförbandens bärförmåga, sidostabilitet, tippning och skruvarnas utdragning "
        "kräver konstruktionsverifiering; skivremsans nedböjning är ingen lastklassning.",
        "Verkstaden ska acceptera materialkvalitet, limfog, beslagens produktdata, "
        "toleranser, åtkomst, montage och levererbara råformat.",
    ]
    if not request.dimensions_confirmed:
        outstanding.insert(0, "Yttermåtten är valda provmått och behöver bekräftas av beställaren.")
    assembly = MeasuredAssembly.model_validate(
        dict(
            schema_version="custombuild.measured-assembly.v1",
            title=(f"Bockbord B1 · {length} × {width} × {request.height_mm} mm"),
            source=SOURCE,
            parts=list(parts.values()),
            joints=joints,
            bearings=[
                dict(id="SUPPORT-" + p, part="TOP", support=p, source=SOURCE)
                for p in ("CROSS-L-TOP", "CROSS-R-TOP", "TIE-TOP")
            ],
            open_items=outstanding,
        )
    )
    counts = Counter(
        article
        for joint in assembly.joints
        for article in (joint.article, *joint.fittings)
        if article
    )
    package = TrestleConstruction(
        request=request,
        sizing=sizing,
        assembly=assembly,
        assembly_sha256=measured_assembly_hash(assembly),
        construction_sha256="0" * 64,
        purchase_blanks=tuple(
            PurchaseBlank(
                part_id=p.id,
                finished_blank_lbt_um=p.blank_lbt_um,
                purchase_min_lbt_um=(
                    p.blank_lbt_um[0] + 20000,
                    p.blank_lbt_um[1] + 4000,
                    p.blank_lbt_um[2] + 4000,
                ),
                grain=grains[p.id],
            )
            for p in assembly.parts
        ),
        hardware=tuple(
            HardwareLine(specification=sku, quantity=count) for sku, count in sorted(counts.items())
        ),
        assembly_sequence=(
            "Märk alla ämnen med del-ID och fiberriktning. Bekräfta faktisk materialtjocklek.",
            "Bearbeta profiler, halvöverlapp, hål, slitsar och åtkomstfickor enligt delbladen. "
            "Räta urtags innerhörn kräver efterbearbetning; STEP anger slutgeometri.",
            "Montera två bockar: varje ben förbinds med övre och nedre tvärbalk med "
            "två M8×160, två brickor och en mutter per bult.",
            "Sätt längsbalkarna i tvärbalkarnas halvöverlapp. Bultskallar och muttrar "
            "ska ligga i de angivna försänkningarna. Montera de två snedsträvorna.",
            "Centrera skivan. Fäst centrumskruven och fyra skruvar i slitsarnas mitt. "
            "Limma inte skivan i ramen. Slitsinfästningarna måste kunna glida.",
            "Kontrollera planhet, full anliggning, bultåtdragning, skruvdjup och fri "
            "slitsrörelse mot verkstadens accepterade montageanvisning.",
        ),
        outstanding_acceptance=tuple(outstanding),
    )
    return package.model_copy(
        update={
            "construction_sha256": content_hash(
                package.model_dump(mode="json", exclude={"construction_sha256"})
            ),
        }
    )
