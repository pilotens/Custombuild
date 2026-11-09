"""Verify the actual millimetre DXF entities using the saved design's physical panel axes."""

import io
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from custombuild_cad.bookcase_readback import PLANES, panel_cut, physical_face, size
from custombuild_domain.models import PartInstance
from ezdxf.filemanagement import read


def mm(um: int) -> str:
    return format(Decimal(um) / 1000, "f")


def _metadata(text: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    lines = text.splitlines()
    for code, value in zip(lines[::2], lines[1::2], strict=True):
        if code.strip() == "999" and "_JSON:" in value and value.startswith("CUSTOMBUILD_"):
            label, order, chunk = value.split(":", 2)
            i, n = map(int, order.split("/"))
            groups[label].append((i, n, chunk))
    result = {}
    for label, chunks in groups.items():
        if any((i, n) != (index, len(chunks)) for index, (i, n, _) in enumerate(chunks, 1)):
            raise ValueError(f"Ofullständig eller dubblerad DXF-identitet: {label}")
        result[label] = json.loads("".join(c for _, _, c in chunks))
    return result


def _rect(layer: str, x: int, y: int, w: int, h: int) -> tuple[object, ...]:
    vertices = ((x, y), (x + w, y), (x + w, y + h), (x, y + h))
    return layer, "rect", vertices


def _um(value: float) -> int:
    scaled = Decimal(str(value)) * 1000
    if not scaled.is_finite() or scaled != scaled.to_integral_value():
        raise ValueError("DXF-koordinaten ligger utanför ritningens mikrometerraster.")
    return int(scaled)


def _entity(entity: Any) -> tuple[object, ...]:
    layer = entity.dxf.layer
    if (
        tuple(entity.dxf.get("extrusion", (0, 0, 1))) != (0, 0, 1)
        or entity.dxf.get("thickness", 0) != 0
    ):
        raise ValueError("DXF har en oväntad bearbetningsriktning.")
    if entity.dxftype() == "LWPOLYLINE":
        pts = list(entity.get_points("xyseb"))
        if (
            not entity.closed
            or len(pts) != 4
            or entity.dxf.get("elevation", 0) != 0
            or entity.dxf.get("const_width", 0)
            or any(any(p[2:]) for p in pts)
        ):
            raise ValueError("DXF innehåller en öppen, böjd eller tredimensionell kontur.")
        coords = tuple((_um(p[0]), _um(p[1])) for p in pts)
        return layer, "rect", coords
    if entity.dxftype() == "CIRCLE":
        x, y, z = entity.dxf.center
        if z or entity.dxf.radius <= 0:
            raise ValueError("DXF har ogiltig cirkel.")
        return layer, "circle", _um(x), _um(y), _um(entity.dxf.radius)
    if entity.dxftype() == "TEXT" and layer == "LABEL":
        return layer, entity.dxf.text
    raise ValueError(f"Oväntad DXF-geometri: {entity.dxftype()} på {layer}")


@dataclass(frozen=True)
class ReadbackPilot:
    feature_id: str
    part_id: str
    side: str
    face: str
    u_um: int
    v_um: int
    diameter_um: int
    depth_um: int
    tolerance_um: int


def verify_panel_dxf(
    raw: bytes, part: PartInstance, side: str, identity: dict[str, object]
) -> list[ReadbackPilot]:
    text = raw.decode("utf-8")
    meta = _metadata(text)
    if meta.get("CUSTOMBUILD_ORDER_JSON") != identity:
        raise ValueError(f"DXF tillhör fel order eller revision: {part.part_id}/{side}")
    doc = read(io.StringIO(text))
    face = part.a_side if side == "A" else part.b_side
    a, b, normal, _ = PLANES[face.value]
    stock = size(part)
    drawing = meta["CUSTOMBUILD_DRAWING_JSON"]
    if (
        doc.units != 4
        or drawing["units"] != "mm"
        or drawing["part_id"] != part.part_id
        or drawing["physical_face"] != face.value.upper()
        or drawing["side"] != side
        or drawing["coordinate_system"] != "LOCAL_UV_MM_V_UP_NOT_MIRRORED"
        or drawing["coordinates_mirrored"] is not False
        or drawing["axis_mapping"] != {"u": "xyz"[a], "v": "xyz"[b], "thickness": "xyz"[normal]}
        or drawing["finished_size_mm"]
        != {k: mm(stock[i]) for k, i in (("u", a), ("v", b), ("thickness", normal))}
    ):
        raise ValueError(f"Fel DXF-sida, enhet eller ämnesmått: {part.part_id}/{side}")
    expected = [
        _rect("OUTLINE", 0, 0, stock[a], stock[b]),
        ("LABEL", f"{part.part_id} SIDE {side}"),
    ]
    features = {f.feature_id: f for f in part.features if physical_face(part, f) == face.value}
    records = [v for k, v in meta.items() if k.startswith("CUSTOMBUILD_FEATURE_")]
    if len(records) != len(features) or {r["feature_id"] for r in records} != set(features):
        raise ValueError(f"DXF saknar rätt bearbetningar: {part.part_id}/{side}")
    pilots = []
    for record in records:
        f = features[record["feature_id"]]
        cut = panel_cut(part, f)
        dims = (
            {
                "width": mm(cut.width),
                "length": mm(cut.length),
                "radius": mm(cut.radius),
                "corner_relief_radius": mm(cut.radius),
            }
            if cut.width
            else {"diameter": mm(2 * cut.radius)}
        )
        if (
            record["side"] != side
            or record["through"] is not f.through
            or record["kind"] != f.kind.value.upper()
            or record["depth_mm"] != mm(cut.depth)
            or record["origin_mm"] != {"u": mm(cut.u), "v": mm(cut.v)}
            or record["dimensions_mm"] != dims
            or record["tolerance_mm"] != mm(f.tolerance_um)
            or record["fit_clearance_mm"] != mm(f.fit_clearance_um)
            or set(record["open_end_reliefs"]) != set(f.open_end_reliefs)
        ):
            raise ValueError(f"Fel DXF-mått, djup eller spårstopp: {f.feature_id}")
        if cut.width:
            expected.append(_rect("GROOVE", cut.u, cut.v, cut.width, cut.length))
            expected.extend(("GROOVE", "circle", x, y, cut.radius) for x, y in cut.corners)
        else:
            expected.append(("DRILL", "circle", cut.u, cut.v, cut.radius))
            pilots.append(
                ReadbackPilot(
                    f.feature_id,
                    part.part_id,
                    side,
                    face.value,
                    cut.u,
                    cut.v,
                    2 * cut.radius,
                    cut.depth,
                    f.tolerance_um,
                )
            )
    actual = Counter(_entity(e) for e in doc.modelspace())
    if actual != Counter(expected):
        raise ValueError(
            f"DXF-geometrin avviker från orderns hål eller kontur: {part.part_id}/{side}"
        )
    return pilots
