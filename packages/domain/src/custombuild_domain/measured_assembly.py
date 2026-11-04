"""Read-only review of explicitly dimensioned, orthogonal parts and two-ended joints."""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .identity import content_hash
from .measured_backing import BackCoverage, Backing, review_backing
from .measured_fasteners import FastenerInterference, Shaft, review_fasteners
from .measured_installation import (
    InstallationCheck,
    MeasuredRoom,
    StockCheck,
    review_installation,
    review_stock,
)
from .measured_profiles import Profile, check_profile_size
from .models import FrozenModel

Key = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")]
Text = Annotated[str, Field(min_length=1, max_length=600)]
Measure = Annotated[int, Field(strict=True, gt=0, le=20_000_000)]
Coord = Annotated[int, Field(strict=True, ge=-20_000_000, le=20_000_000)]
Point = tuple[Coord, Coord, Coord]
Size = tuple[Measure, Measure, Measure]
Bounds = tuple[float, float, float, float, float, float]
Face = Literal["left", "right", "front", "back", "bottom", "top"]
Shape = Literal["drill", "slot", "pocket", "tenon", "notch"]
NORMAL = {
    "left": (0, -1),
    "right": (0, 1),
    "front": (1, -1),
    "back": (1, 1),
    "bottom": (2, -1),
    "top": (2, 1),
}


class MeasuredOperation(FrozenModel):
    id: Key
    shape: Shape
    face: Face
    axes: Literal["xy", "yx", "xz", "zx", "yz", "zy"]
    u_um: Coord
    v_um: Coord
    width_um: Measure
    height_um: Measure
    depth_um: Measure
    through: bool = False
    transfer: bool = False
    provenance: Literal["listed", "drawing", "transfer_template", "edited"] = "listed"
    source: Text
    article: Key | None = None

    @model_validator(mode="after")
    def planar_axes(self) -> MeasuredOperation:
        if "xyz"[NORMAL[self.face][0]] in self.axes:
            raise ValueError("U/V måste ligga i den angivna ytan.")
        if self.shape == "drill" and self.width_um != self.height_um:
            raise ValueError("En cirkel kräver en enda diameter.")
        return self


class MeasuredPart(FrozenModel):
    id: Key
    code: Key
    name: Text
    size_um: Size
    blank_lbt_um: Size
    origin_um: Point | None = None
    frame_id: Key | None = None
    material: Text
    note: str = Field(default="", max_length=750)
    stock_lbt_um: Size | None = None
    operations: tuple[MeasuredOperation, ...] = Field(default=(), max_length=256)
    profile: Profile | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def feature_keys(self) -> MeasuredPart:
        if self.profile is not None:
            check_profile_size(self.profile, self.size_um)
        if len({o.id for o in self.operations}) != len(self.operations):
            raise ValueError("Bearbetningarnas ID måste vara unika på delen.")
        return self


class MeasuredEnd(FrozenModel):
    part: Key
    operation: Key


class MeasuredJoint(FrozenModel):
    id: Key
    kind: Literal["blind_tenon", "dowel", "screw"]
    a: MeasuredEnd
    b: MeasuredEnd
    source: Text
    article: Key | None = None
    fittings: tuple[Key, ...] = Field(default=(), max_length=10)
    interface_gap_um: int = Field(default=0, strict=True, ge=0, le=50_000)
    head_allowance_um: int = Field(default=0, strict=True, ge=0, le=50_000)
    diameter_um: Measure | None = None
    length_um: Measure | None = None


class MeasuredBearing(FrozenModel):
    id: Key
    part: Key
    support: Key
    source: Text


class MeasuredAssembly(FrozenModel):
    schema_version: Literal["custombuild.measured-assembly.v1"]
    title: Text
    source: Text
    backing: Backing | None = None
    room: MeasuredRoom | None = None
    parts: tuple[MeasuredPart, ...] = Field(min_length=1, max_length=512)
    joints: tuple[MeasuredJoint, ...] = Field(default=(), max_length=2048)
    bearings: tuple[MeasuredBearing, ...] = Field(default=(), max_length=2048)
    changes: tuple[Text, ...] = Field(default=(), max_length=100)
    open_items: tuple[Text, ...] = Field(default=(), max_length=40)

    @model_validator(mode="after")
    def unique_ids(self) -> MeasuredAssembly:
        if len({p.id for p in self.parts}) != len(self.parts):
            raise ValueError("Varje del behöver ett eget ID.")
        if len({j.id for j in self.joints}) != len(self.joints):
            raise ValueError("Varje förband behöver ett eget ID.")
        if len({b.id for b in self.bearings}) != len(self.bearings):
            raise ValueError("Varje upplag behöver ett eget ID.")
        if sum(len(p.operations) for p in self.parts) > 4096:
            raise ValueError("Högst 4096 bearbetningar ryms i en arbetsfil.")
        return self


class JointCheck(FrozenModel):
    id: Key
    state: Literal["aligned", "mismatch", "unresolved"]
    reason: Text
    offset_um: Point | None = None
    depth_reserve_um: int | None = None
    contact_area_um2: float | None = None
    uses_transfer_template: bool = False
    frame_id: Key | None = None


class MeasuredReport(FrozenModel):
    assembly: MeasuredAssembly
    assembly_sha256: str
    cells: dict[str, tuple[Bounds, ...]]
    issues: dict[str, tuple[str, ...]]
    joints: tuple[JointCheck, ...]
    bearings: tuple[JointCheck, ...]
    overlaps: tuple[tuple[str, str], ...]
    overlaps_truncated: bool
    back_coverage: tuple[BackCoverage, ...] = ()
    unresolved_back_parts: tuple[str, ...] = ()
    fastener_interferences: tuple[FastenerInterference, ...] = ()
    stock_checks: tuple[StockCheck, ...] = ()
    installation: InstallationCheck | None = None
    unreviewed_profile_parts: tuple[str, ...] = ()
    physical_cutting_authorized: Literal[False] = False


def _axes(op: MeasuredOperation) -> tuple[int, int, int, int]:
    axis, sign = NORMAL[op.face]
    return "xyz".index(op.axes[0]), "xyz".index(op.axes[1]), axis, sign


def _area(op: MeasuredOperation) -> tuple[float, float, float, float]:
    # Half a micrometre is exact in binary floating point throughout this model.
    return (
        op.u_um - op.width_um / 2,
        op.v_um - op.height_um / 2,
        op.u_um + op.width_um / 2,
        op.v_um + op.height_um / 2,
    )


def _invalid(part: MeasuredPart, op: MeasuredOperation) -> str | None:
    u, v, n, _ = _axes(op)
    if (
        2 * op.u_um - op.width_um < 0
        or 2 * op.v_um - op.height_um < 0
        or 2 * op.u_um + op.width_um > 2 * part.size_um[u]
        or 2 * op.v_um + op.height_um > 2 * part.size_um[v]
    ):
        return "Bearbetningen går utanför ytans kanter."
    if op.depth_um > part.size_um[n] or (op.through and op.depth_um != part.size_um[n]):
        return "Djup eller genomgång avviker från delens tjocklek i borrriktningen."
    if op.shape in ("drill", "pocket") and not op.through and op.depth_um == part.size_um[n]:
        return "Ett angivet blindhål går igenom delens motsatta yta."
    return None


def _subtract(cell: Bounds, cut: Bounds) -> list[Bounds]:
    low = [max(cell[i], cut[i]) for i in range(3)]
    high = [min(cell[i + 3], cut[i + 3]) for i in range(3)]
    if any(a >= b for a, b in zip(low, high, strict=True)):
        return [cell]
    remain = list(cell)
    fragments = []
    for i in range(3):
        if remain[i] < low[i]:
            s = remain.copy()
            s[i + 3] = low[i]
            fragments.append(tuple(s))
            remain[i] = low[i]
        if high[i] < remain[i + 3]:
            s = remain.copy()
            s[i] = high[i]
            fragments.append(tuple(s))
            remain[i + 3] = high[i]
    return fragments  # type: ignore[return-value]


def _feature_box(part: MeasuredPart, op: MeasuredOperation) -> Bounds:
    u, v, axis, sign = _axes(op)
    u1, v1, u2, v2 = _area(op)
    box = list((0.0, 0.0, 0.0, *part.size_um))
    box[u], box[v], box[u + 3], box[v + 3] = u1, v1, u2, v2
    if sign < 0:
        box[axis + 3] = op.depth_um
    else:
        box[axis] = part.size_um[axis] - op.depth_um
    return tuple(box)  # type: ignore[return-value]


def _cuts(part: MeasuredPart, op: MeasuredOperation) -> list[Bounds]:
    if op.shape not in ("notch", "pocket", "tenon"):
        return []
    feature = _feature_box(part, op)
    if op.shape != "tenon":
        return [feature]
    _, _, axis, _ = _axes(op)
    slab = list((0.0, 0.0, 0.0, *part.size_um))
    slab[axis], slab[axis + 3] = feature[axis], feature[axis + 3]
    return _subtract(tuple(slab), feature)  # type: ignore[arg-type]


def _missing_wall(
    part: MeasuredPart, box: Bounds, axis: int, sign: int, removed: list[Bounds]
) -> bool:
    plane = box[axis + (3 if sign > 0 else 0)]
    if plane == (part.size_um[axis] if sign > 0 else 0):
        return True
    others = [n for n in range(3) if n != axis]
    return any(
        (cut[axis] <= plane < cut[axis + 3] if sign > 0 else cut[axis] < plane <= cut[axis + 3])
        and all(cut[n] < box[n + 3] and box[n] < cut[n + 3] for n in others)
        for cut in removed
    )


def _lost_joinery(part: MeasuredPart, op: MeasuredOperation, removed: list[Bounds]) -> str | None:
    u, v, axis, sign = _axes(op)
    box = _feature_box(part, op)
    if op.shape == "tenon":
        if any(_hit(box, cut) for cut in removed) or _missing_wall(part, box, axis, -sign, removed):
            return (
                "Tappens angivna material eller infästningen i skuldran saknas efter bearbetning."
            )
        return None
    if any(
        _missing_wall(part, box, n, direction, removed)
        for n, direction in ((axis, -sign), (u, -1), (u, 1), (v, -1), (v, 1))
    ):
        return "Det blinda tapphålets botten eller sidovägg är öppen efter bearbetning."
    return None


def _breaks_into_cut(part: MeasuredPart, op: MeasuredOperation, removed: list[Bounds]) -> bool:
    u, v, axis, sign = _axes(op)
    start = 0 if sign < 0 else part.size_um[axis] - op.depth_um
    end = start + op.depth_um
    for box in removed:
        if max(start, box[axis]) >= min(end, box[axis + 3]):
            continue
        du = max(box[u] - op.u_um, op.u_um - box[u + 3], 0)
        dv = max(box[v] - op.v_um, op.v_um - box[v + 3], 0)
        if op.shape == "drill":
            if 4 * (du * du + dv * dv) < op.width_um * op.width_um:
                return True
        elif du * 2 < op.width_um and dv * 2 < op.height_um:
            return True
    return False


def _point(part: MeasuredPart, op: MeasuredOperation, shift: int) -> list[int]:
    assert part.origin_um is not None
    u, v, n, sign = _axes(op)
    local = [0, 0, 0]
    local[u] = op.u_um
    local[v] = op.v_um
    local[n] = (part.size_um[n] if sign > 0 else 0) - sign * shift
    return [a + b for a, b in zip(part.origin_um, local, strict=True)]


def _joint(
    joint: MeasuredJoint, parts: dict[str, MeasuredPart], bad: set[tuple[str, str]]
) -> JointCheck:
    def result(
        state: Literal["aligned", "mismatch", "unresolved"],
        why: str,
        offset: Point | None = None,
        reserve: int | None = None,
    ) -> JointCheck:
        return JointCheck(
            id=joint.id, state=state, reason=why, offset_um=offset, depth_reserve_um=reserve
        )

    a, b = parts.get(joint.a.part), parts.get(joint.b.part)
    if a is None or b is None or a.id == b.id:
        return result("mismatch", "Två olika existerande motdelar krävs.")
    left = next((o for o in a.operations if o.id == joint.a.operation), None)
    right = next((o for o in b.operations if o.id == joint.b.operation), None)
    if not left or not right or (a.id, left.id) in bad or (b.id, right.id) in bad:
        return result(
            "mismatch", "Förbandets bearbetningar saknas eller är ogiltiga. Se respektive del."
        )
    if a.frame_id != b.frame_id:
        return result("unresolved", "Motdelarna saknar en gemensam monteringsreferens.")
    if a.origin_um is None or b.origin_um is None:
        return result("unresolved", "Minst en del saknar monteringsläge.")
    ua, va, na, sa = _axes(left)
    ub, vb, nb, sb = _axes(right)
    if na != nb or ((sa == sb) != (joint.kind == "screw")):
        return result("mismatch", "Förbandets ingångsytor har fel riktning.")
    shift_left = 0
    reserve = None
    if joint.kind == "blind_tenon":
        if left.shape != "tenon" or right.shape != "pocket" or right.through:
            return result("mismatch", "Blind tapp måste möta ett blint tapphål.")
        tenon_section = {ua: left.width_um, va: left.height_um}
        hole_section = {ub: right.width_um, vb: right.height_um}
        if tenon_section != hole_section:
            return result("mismatch", "Tapp och tapphål har olika nominellt tvärsnitt.")
        shift_left = left.depth_um
        reserve = right.depth_um - left.depth_um
        if reserve <= 0:
            return result(
                "mismatch", "Tappen bottnar eller går djupare än tapphålet.", reserve=reserve
            )
    elif joint.kind == "screw":
        if (
            left.shape not in ("drill", "slot")
            or not left.through
            or right.shape != "drill"
            or right.through
        ):
            return result("mismatch", "Genomgången måste möta en blind pilotborrning.")
        if min(left.width_um, left.height_um) < right.width_um:
            return result("mismatch", "Genomgångens minsta bredd understiger pilotdiametern.")
        shift_left = left.depth_um + joint.interface_gap_um
        if joint.diameter_um is not None and joint.diameter_um > min(left.width_um, left.height_um):
            return result("mismatch", "Skruvens diameter överstiger genomgångens bredd.")
        if joint.length_um is not None:
            penetration = joint.length_um - shift_left - joint.head_allowance_um
            reserve = right.depth_um - penetration
            if penetration <= 0 or reserve < 0:
                return result(
                    "mismatch",
                    "Skruvens inträngning ryms inte i mottagarens pilot.",
                    reserve=reserve,
                )
    elif (
        left.shape != "drill"
        or right.shape != "drill"
        or left.through
        or right.through
        or left.width_um != right.width_um
    ):
        return result(
            "mismatch", "Styrtappen behöver två motstående blindhål med samma nominella diameter."
        )
    if joint.kind == "dowel":
        if joint.diameter_um is not None and joint.diameter_um > left.width_um:
            return result("mismatch", "Styrtappens diameter överstiger hålens diameter.")
        if joint.length_um is not None:
            reserve = left.depth_um + right.depth_um - joint.length_um
            if reserve < 0 or joint.length_um <= max(left.depth_um, right.depth_um):
                return result(
                    "mismatch", "Styrtappens längd passar inte båda delarna.", reserve=reserve
                )
    p, q = _point(a, left, shift_left), _point(b, right, 0)
    delta: Point = (p[0] - q[0], p[1] - q[1], p[2] - q[2])
    if any(delta):
        return result(
            "mismatch",
            "Bearbetningarnas nominella centrum möts inte i monteringen.",
            delta,
            reserve,
        )
    if joint.kind != "blind_tenon" and not (
        joint.article and joint.length_um and joint.diameter_um
    ):
        return result(
            "unresolved", "Artikel, diameter och längd behövs för att verifiera beslaget.", delta
        )
    if joint.kind != "blind_tenon" and any(
        op.article and op.article not in (joint.article, *joint.fittings) for op in (left, right)
    ):
        return result(
            "unresolved",
            "Bearbetningens angivna artikel saknas bland förbandets beslag.",
            delta,
            reserve,
        )
    if (
        joint.kind == "screw"
        and joint.diameter_um is not None
        and right.width_um >= joint.diameter_um
    ):
        return result(
            "unresolved",
            "Piloten är minst lika bred som skruven. Infästningen måste utredas.",
            delta,
        )
    return result(
        "aligned",
        "Centrum och ingångsytor sammanfaller. Nominell geometri kontrollerad.",
        delta,
        reserve,
    )


def _shaft(joint: MeasuredJoint, parts: dict[str, MeasuredPart]) -> Shaft:
    first = parts[joint.a.part]
    a = next(o for o in first.operations if o.id == joint.a.operation)
    other = parts[joint.b.part]
    b = next(o for o in other.operations if o.id == joint.b.operation)
    _, _, axis, sign = _axes(a)
    center = tuple(_point(first, a, 0))
    assert joint.length_um and joint.diameter_um
    datum = center[axis]

    def interval(near: int, far: int) -> tuple[int, int]:
        return min(near, far), max(near, far)

    if joint.kind == "screw":
        head = datum + sign * joint.head_allowance_um
        certain = possible = interval(head, head - sign * joint.length_um)
    else:
        # A floating dowel can slide between the two blind bottoms.
        possible = interval(datum - sign * a.depth_um, datum + sign * b.depth_um)
        certain = interval(
            datum - sign * (joint.length_um - b.depth_um),
            datum + sign * (joint.length_um - a.depth_um),
        )
    return Shaft(joint.id, first.frame_id, axis, center, joint.diameter_um, certain, possible)  # type: ignore[arg-type]


def _hit(a: Bounds, b: Bounds) -> bool:
    return all(a[i] < b[i + 3] and b[i] < a[i + 3] for i in range(3))


def _world(box: Bounds, origin: Point) -> Bounds:
    return (
        box[0] + origin[0],
        box[1] + origin[1],
        box[2] + origin[2],
        box[3] + origin[0],
        box[4] + origin[1],
        box[5] + origin[2],
    )


def _bearing(
    b: MeasuredBearing,
    parts: dict[str, MeasuredPart],
    world: dict[str, tuple[Bounds, tuple[Bounds, ...]]],
) -> JointCheck:
    if b.part not in parts or b.support not in parts or b.part == b.support:
        return JointCheck(
            id=b.id, state="mismatch", reason="Upplaget kräver två olika existerande delar."
        )
    if (
        parts[b.part].frame_id != parts[b.support].frame_id
        or b.part not in world
        or b.support not in world
    ):
        return JointCheck(
            id=b.id, state="unresolved", reason="Monteringsläget för upplaget saknas."
        )
    top, material = world[b.part]
    support, below = world[b.support]
    dz = int(top[2] - support[5])
    area = 0.0
    if dz == 0:
        for first in material:
            if first[2] != top[2]:
                continue
            for other in below:
                if other[5] == support[5]:
                    dx = max(0, min(first[3], other[3]) - max(first[0], other[0]))
                    dy = max(0, min(first[4], other[4]) - max(first[1], other[1]))
                    area += dx * dy
    return JointCheck(
        id=b.id,
        state="aligned" if area > 0 else "mismatch",
        reason="Undersidan ligger an mot upplagets ovansida."
        if area > 0
        else "Inget gemensamt horisontellt upplag på den angivna höjden.",
        offset_um=(0, 0, dz),
        contact_area_um2=area,
        frame_id=parts[b.part].frame_id,
    )


def review_measured_assembly(assembly: MeasuredAssembly) -> MeasuredReport:
    parts = {p.id: p for p in assembly.parts}
    issues: dict[str, list[str]] = {p.id: [] for p in assembly.parts}
    cells: dict[str, tuple[Bounds, ...]] = {}
    world: dict[str, tuple[Bounds, tuple[Bounds, ...]]] = {}
    bad: set[tuple[str, str]] = set()
    socket_ends = {(j.b.part, j.b.operation) for j in assembly.joints if j.kind == "blind_tenon"}
    for p in assembly.parts:
        blank: Bounds = (0, 0, 0, *p.size_um)
        pieces = [blank]
        if p.profile is None and sorted(p.size_um) != sorted(p.blank_lbt_um):
            issues[p.id].append(
                "Råmått och modellens omslutande mått avviker. Kontrollera orienteringen."
            )
        if p.stock_lbt_um and any(
            x > y for x, y in zip(p.blank_lbt_um, p.stock_lbt_um, strict=True)
        ):
            issues[p.id].append("Råämnet ryms inte i angivet inköpsformat i ådringens riktning.")
        if p.origin_um is None:
            issues[p.id].append("Monteringsläge saknas. Delen kan granskas isolerad.")
        if p.profile is not None:
            issues[p.id].append(
                "Formprofilen visas, men den rätvinkliga förbands- och kollisionskontrollen "
                "omfattar inte denna del. Profilens bearbetningar och anslutningar "
                "är ej verifierade."
            )
        removed: list[Bounds] = []
        for op in p.operations:
            failure = _invalid(p, op)
            if failure:
                bad.add((p.id, op.id))
                issues[p.id].append(op.id + " · " + failure)
                continue
            for cut in _cuts(p, op):
                removed.append(cut)
                pieces = [s for cell in pieces for s in _subtract(cell, cut)]
            if len(pieces) > 2048:
                raise ValueError("Bearbetningarna överstiger granskarens gräns för en del.")
        for op in p.operations:
            if (
                op.shape == "tenon"
                or (op.shape == "pocket" and not op.through and (p.id, op.id) in socket_ends)
            ) and (p.id, op.id) not in bad:
                failure = _lost_joinery(p, op, removed)
                if failure:
                    issues[p.id].append(op.id + " · " + failure)
                    bad.add((p.id, op.id))
            if (
                op.shape in ("drill", "slot")
                and (p.id, op.id) not in bad
                and _breaks_into_cut(p, op, removed)
            ):
                bad.add((p.id, op.id))
                issues[p.id].append(op.id + " · Borrning eller slits går in i ett utskuret område.")
        if not pieces:
            issues[p.id].append("Bearbetningarna tar bort hela delens material.")
        cells[p.id] = tuple(pieces)
        if sum(map(len, cells.values())) > 20_000:
            raise ValueError("Arbetsritningen överskrider gränsen för geometrisk granskning.")
        if p.origin_um is not None and p.profile is None:
            world[p.id] = (
                _world(blank, p.origin_um),
                tuple(_world(c, p.origin_um) for c in pieces),
            )
    overlaps: list[tuple[str, str]] = []
    truncated = False
    budget = 3_000_000
    for a, b in combinations(world, 2):
        abox, av = world[a]
        bbox, bv = world[b]
        if parts[a].frame_id != parts[b].frame_id or not _hit(abox, bbox):
            continue
        budget -= len(av) * len(bv)
        if budget < 0:
            truncated = True
            break
        if any(_hit(m, n) for m in av for n in bv):
            if len(overlaps) >= 2048:
                truncated = True
                break
            overlaps.append((a, b))
    used = Counter((end.part, end.operation) for j in assembly.joints for end in (j.a, j.b))
    checks = []
    for j in assembly.joints:
        if any(used[(e.part, e.operation)] > 1 for e in (j.a, j.b)):
            checks.append(
                JointCheck(
                    id=j.id,
                    state="mismatch",
                    reason="Samma bearbetning är tilldelad flera förband.",
                )
            )
        else:
            check = _joint(j, parts, bad)
            if any(parts[e.part].profile is not None for e in (j.a, j.b) if e.part in parts):
                check = JointCheck(
                    id=j.id,
                    state="unresolved",
                    reason="Förband mot en formprofil är inte verifierat "
                    "av den rätvinkliga granskaren.",
                )
            transfer = any(
                op.provenance == "transfer_template"
                for end in (j.a, j.b)
                if end.part in parts
                for op in parts[end.part].operations
                if op.id == end.operation
            )
            if transfer:
                check = check.model_copy(update={"uses_transfer_template": True})
            if (
                j.a.part in parts
                and j.b.part in parts
                and parts[j.a.part].frame_id == parts[j.b.part].frame_id
            ):
                check = check.model_copy(update={"frame_id": parts[j.a.part].frame_id})
            checks.append(check)
    shafts = [
        _shaft(joint, parts)
        for joint, check in zip(assembly.joints, checks, strict=True)
        if joint.kind != "blind_tenon" and check.state == "aligned"
    ]
    interferences = review_fasteners(shafts)
    by_joint: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for collision in interferences:
        by_joint[collision.joint_a].append((collision.joint_b, collision.state))
        by_joint[collision.joint_b].append((collision.joint_a, collision.state))
    for index, check in enumerate(checks):
        if hits := by_joint.get(check.id):
            certain_hits = [other for other, state in hits if state == "mismatch"]
            reason = (
                "Beslagets nominella volym överlappar "
                if certain_hits
                else "Styrtappens sättdjup kan ge överlapp med "
            )
            examples = certain_hits or [other for other, _ in hits]
            reason += ", ".join(examples[:3]) + (" m.fl." if len(examples) > 3 else ".")
            checks[index] = check.model_copy(
                update={"state": "mismatch" if certain_hits else "unresolved", "reason": reason}
            )
    bearing_budget = sum(
        len(world[b.part][1]) * len(world[b.support][1])
        for b in assembly.bearings
        if b.part in world and b.support in world
    )
    if bearing_budget > 3_000_000:
        raise ValueError("Upplagens geometri överstiger granskarens omfattning.")
    bearings = tuple(_bearing(b, parts, world) for b in assembly.bearings)
    joined = {
        end.part
        for j, c in zip(assembly.joints, checks, strict=True)
        if c.state == "aligned"
        for end in (j.a, j.b)
    }
    resting = {
        key
        for b, c in zip(assembly.bearings, bearings, strict=True)
        if c.state == "aligned"
        for key in (b.part, b.support)
    }
    if len(parts) > 1:
        for key, part in parts.items():
            if part.origin_um is not None and key not in joined:
                issues[key].append(
                    "Endast ett nominellt upplag förbinder delen med monteringen."
                    if key in resting
                    else "Delen saknar passande registrerade förband och upplag till andra delar."
                )
    canonical = assembly.model_dump(mode="json")
    if assembly.room is None:
        # Roomless files retain their previous identities and printed receipts.
        canonical.pop("room")
    back_checks: tuple[BackCoverage, ...] = ()
    unknown: set[str] = set()
    if assembly.backing:
        backs = {
            p: world[p][1]
            for p in assembly.backing.part_ids
            if p in world and not parts[p].frame_id
        }
        unknown = set(assembly.backing.part_ids).difference(backs)
        back_checks = review_backing(assembly.backing, backs, unknown)
    else:
        # Keep receipts from files predating optional rear coverage reproducible.
        canonical.pop("backing")
    return MeasuredReport(
        assembly=assembly,
        assembly_sha256=content_hash(canonical),
        back_coverage=back_checks,
        fastener_interferences=interferences,
        stock_checks=tuple(review_stock(p) for p in assembly.parts),
        installation=review_installation(assembly, world),
        unreviewed_profile_parts=tuple(p.id for p in assembly.parts if p.profile is not None),
        unresolved_back_parts=tuple(sorted(unknown)),
        cells=cells,
        issues={k: tuple(v) for k, v in issues.items() if v},
        joints=tuple(checks),
        bearings=bearings,
        overlaps=tuple(overlaps),
        overlaps_truncated=truncated,
    )
