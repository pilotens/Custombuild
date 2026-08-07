"""Deterministic human- and machine-readable manufacturing exports."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from html import escape

from .model import (
    FeatureKind,
    ManufacturingFeature,
    NestingLayout,
    OperationsDocument,
    PartSpec,
    Rect,
    Setup,
    Side,
    um_to_mm,
)

DXF_LAYERS = ("OUTLINE", "DRILL", "POCKET", "GROOVE", "EDGE_BAND", "LABEL")
ARTIFACT_EXPORTERS_VERSION = "manufacturing-exporters-1.0.0"


def dxf_for_part(part: PartSpec, side: Side) -> bytes:
    """Create a UTF-8 DXF with only the requested machining side's features."""

    if side not in (Side.A, Side.B):
        raise ValueError("DXF machining side must be A or B")
    side_features = tuple(
        feature
        for feature in sorted(part.features, key=lambda item: item.feature_id)
        if feature.side == side
    )
    outline_feature, outline_bounds = _outline_for_side(part, side_features)
    entities: list[str] = []
    if outline_feature is not None:
        entities.extend(("999", f"FEATURE:{_dxf_text(outline_feature.feature_id)}"))
    entities.extend(
        _dxf_rectangle(
            "OUTLINE",
            outline_bounds.x_um,
            outline_bounds.y_um,
            outline_bounds.width_um,
            outline_bounds.height_um,
        )
    )
    for edge in sorted(part.edge_bands):
        line = _edge_line(edge, part.width_um, part.height_um)
        if line:
            entities.extend(_dxf_line("EDGE_BAND", *line))

    for feature in side_features:
        if feature.kind == FeatureKind.OUTER_CONTOUR:
            continue
        entities.extend(("999", f"FEATURE:{_dxf_text(feature.feature_id)}"))
        if feature.kind in {
            FeatureKind.DRILL,
            FeatureKind.DRILL_PATTERN,
            FeatureKind.COUNTERSINK,
        }:
            radius_um = (feature.diameter_um or 0) // 2
            for point in feature.points():
                entities.extend(_dxf_circle("DRILL", point.x_um, point.y_um, radius_um))
        elif feature.kind in {FeatureKind.GROOVE, FeatureKind.RABBET}:
            bounds = feature.bounds()
            entities.extend(
                _dxf_rectangle(
                    "GROOVE", bounds.x_um, bounds.y_um, bounds.width_um, bounds.height_um
                )
            )
            for relief_x_um, relief_y_um in _corner_relief_points(feature):
                assert feature.corner_relief_radius_um is not None
                entities.extend(
                    _dxf_circle(
                        "GROOVE",
                        relief_x_um,
                        relief_y_um,
                        feature.corner_relief_radius_um,
                    )
                )
        elif feature.kind in {FeatureKind.POCKET, FeatureKind.INNER_CONTOUR}:
            bounds = feature.bounds()
            entities.extend(
                _dxf_rectangle(
                    "POCKET", bounds.x_um, bounds.y_um, bounds.width_um, bounds.height_um
                )
            )
            for relief_x_um, relief_y_um in _corner_relief_points(feature):
                assert feature.corner_relief_radius_um is not None
                entities.extend(
                    _dxf_circle(
                        "POCKET",
                        relief_x_um,
                        relief_y_um,
                        feature.corner_relief_radius_um,
                    )
                )
        else:
            entities.extend(
                _dxf_text_entity("LABEL", feature.x_um, feature.y_um, feature.feature_id, 3_000)
            )

    entities.extend(
        _dxf_text_entity(
            "LABEL",
            min(5_000, part.width_um // 20),
            min(5_000, part.height_um // 20),
            f"{part.part_id} SIDE {side.value}",
            min(5_000, max(1_000, part.height_um // 40)),
        )
    )
    tokens = [
        "0",
        "SECTION",
        "2",
        "HEADER",
        "9",
        "$ACADVER",
        "1",
        "AC1027",
        "0",
        "ENDSEC",
        "0",
        "SECTION",
        "2",
        "TABLES",
        "0",
        "TABLE",
        "2",
        "LAYER",
        "70",
        str(len(DXF_LAYERS)),
    ]
    for index, layer in enumerate(DXF_LAYERS, start=1):
        tokens.extend(("0", "LAYER", "2", layer, "70", "0", "62", str(index), "6", "CONTINUOUS"))
    tokens.extend(("0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"))
    tokens.extend(("999", f"CUSTOMBUILD_SIDE:{side.value}"))
    tokens.extend(entities)
    tokens.extend(("0", "ENDSEC", "0", "EOF"))
    return ("\n".join(tokens) + "\n").encode("utf-8")


def svg_for_part(part: PartSpec, side: Side) -> bytes:
    if side not in (Side.A, Side.B):
        raise ValueError("SVG machining side must be A or B")
    width = um_to_mm(part.width_um)
    height = um_to_mm(part.height_um)
    dimension_offset = 16
    side_features = tuple(
        feature
        for feature in sorted(part.features, key=lambda item: item.feature_id)
        if feature.side == side
    )
    outline_feature, outline_bounds = _outline_for_side(part, side_features)
    outline_identity = (
        ""
        if outline_feature is None
        else f' data-feature-id="{escape(outline_feature.feature_id, quote=True)}"'
    )
    elements = [
        f'<rect class="outline"{outline_identity} '
        f'x="{um_to_mm(outline_bounds.x_um)}" y="{um_to_mm(outline_bounds.y_um)}" '
        f'width="{um_to_mm(outline_bounds.width_um)}" '
        f'height="{um_to_mm(outline_bounds.height_um)}"/>',
    ]
    for feature in side_features:
        if feature.kind == FeatureKind.OUTER_CONTOUR:
            continue
        feature_id = escape(feature.feature_id, quote=True)
        if feature.kind in {
            FeatureKind.DRILL,
            FeatureKind.DRILL_PATTERN,
            FeatureKind.COUNTERSINK,
        }:
            radius = um_to_mm((feature.diameter_um or 0) // 2)
            for point in feature.points():
                elements.append(
                    f'<circle class="drill" data-feature-id="{feature_id}" '
                    f'cx="{um_to_mm(point.x_um)}" cy="{um_to_mm(point.y_um)}" r="{radius}"/>'
                )
        else:
            bounds = feature.bounds()
            css_class = (
                "groove" if feature.kind in {FeatureKind.GROOVE, FeatureKind.RABBET} else "pocket"
            )
            elements.append(
                f'<rect class="{css_class}" data-feature-id="{feature_id}" '
                f'x="{um_to_mm(bounds.x_um)}" y="{um_to_mm(bounds.y_um)}" '
                f'width="{um_to_mm(bounds.width_um)}" height="{um_to_mm(bounds.height_um)}"/>'
            )
            if feature.corner_relief_radius_um is not None:
                for x_um, y_um in _corner_relief_points(feature):
                    elements.append(
                        f'<circle class="{css_class} corner-relief" '
                        f'data-feature-id="{feature_id}" data-corner-strategy="dogbone-v1" '
                        f'cx="{um_to_mm(x_um)}" cy="{um_to_mm(y_um)}" '
                        f'r="{um_to_mm(feature.corner_relief_radius_um)}"/>'
                    )
    title = escape(f"{part.part_id} – sida {side.value}")
    body = "".join(elements)
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="-20 -28 {float(width) + 40:g} {float(height) + 48:g}" '
        f'width="{width}mm" height="{height}mm" data-side="{side.value}">'
        "<style>.outline{fill:none;stroke:#111;stroke-width:.5}"
        ".drill{fill:none;stroke:#06c;stroke-width:.4}"
        ".pocket{fill:#dbeafe;stroke:#2563eb;stroke-width:.4}"
        ".groove{fill:#fef3c7;stroke:#b45309;stroke-width:.4}"
        ".dim{stroke:#555;stroke-width:.25}.label{font:5px sans-serif;fill:#111}</style>"
        f'<title>{title}</title><text class="label" x="0" y="-18">{title}</text>'
        f"{body}"
        f'<line class="dim" x1="0" y1="{float(height) + dimension_offset:g}" '
        f'x2="{width}" y2="{float(height) + dimension_offset:g}"/>'
        f'<text class="label" x="{float(width) / 2:g}" '
        f'y="{float(height) + dimension_offset - 2:g}" text-anchor="middle">'
        f"{width} mm</text>"
        f'<line class="dim" x1="-12" y1="0" x2="-12" y2="{height}"/>'
        f'<text class="label" x="-15" y="{float(height) / 2:g}" '
        f'text-anchor="middle" transform="rotate(-90 -15 {float(height) / 2:g})">'
        f"{height} mm</text>"
        "</svg>"
    )
    return svg.encode("utf-8")


def _corner_relief_points(feature: ManufacturingFeature) -> tuple[tuple[int, int], ...]:
    if feature.corner_strategy is None:
        return ()
    if feature.corner_strategy != "dogbone-v1" or feature.corner_relief_radius_um is None:
        raise ValueError(
            f"feature {feature.feature_id} has an unsupported or incomplete corner strategy"
        )
    bounds = feature.bounds()
    return (
        (bounds.x_um, bounds.y_um),
        (bounds.right_um, bounds.y_um),
        (bounds.x_um, bounds.top_um),
        (bounds.right_um, bounds.top_um),
    )


def _outline_for_side(
    part: PartSpec,
    side_features: tuple[ManufacturingFeature, ...],
) -> tuple[ManufacturingFeature | None, Rect]:
    """Resolve the single authoritative closed perimeter for a side drawing.

    A semantic ``OUTER_CONTOUR`` replaces the rectangular finished-part
    fallback.  Rendering both would create two coincident tool contours for
    rectangular parts.  Multiple outer contours cannot be represented by the
    MVP's single-loop contour model and are therefore rejected rather than
    silently emitting ambiguous manufacturing geometry.
    """

    outlines = tuple(
        feature for feature in side_features if feature.kind == FeatureKind.OUTER_CONTOUR
    )
    if len(outlines) > 1:
        raise ValueError(
            f"part {part.part_id} side {outlines[0].side.value} has multiple outer contours"
        )
    if outlines:
        return outlines[0], outlines[0].bounds()
    return None, Rect(0, 0, part.width_um, part.height_um)


def bom_csv(parts: Iterable[PartSpec]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "part_id",
            "name",
            "quantity",
            "finished_width_mm",
            "finished_height_mm",
            "thickness_mm",
            "material_id",
            "material_version",
            "grain_direction",
            "edge_bands",
            "conservative_gross_weight_g_each",
        )
    )
    for part in sorted(parts, key=lambda item: item.part_id):
        writer.writerow(
            (
                part.part_id,
                part.name,
                part.quantity,
                um_to_mm(part.width_um),
                um_to_mm(part.height_um),
                um_to_mm(part.thickness_um),
                part.material_id,
                part.material_version,
                part.grain_direction,
                "|".join(sorted(part.edge_bands)),
                part.weight_g,
            )
        )
    return output.getvalue().encode("utf-8")


def cut_list_csv(parts: Iterable[PartSpec]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "instance_id",
            "part_id",
            "raw_width_mm",
            "raw_height_mm",
            "finished_width_mm",
            "finished_height_mm",
            "thickness_mm",
            "material_id",
            "material_version",
        )
    )
    for part in sorted(parts, key=lambda item: item.part_id):
        for index in range(part.quantity):
            writer.writerow(
                (
                    f"{part.part_id}:{index + 1:03d}",
                    part.part_id,
                    um_to_mm(part.blank_width_um),
                    um_to_mm(part.blank_height_um),
                    um_to_mm(part.width_um),
                    um_to_mm(part.height_um),
                    um_to_mm(part.thickness_um),
                    part.material_id,
                    part.material_version,
                )
            )
    return output.getvalue().encode("utf-8")


def material_list_csv(parts: Iterable[PartSpec]) -> bytes:
    """Aggregate deterministic material demand without hiding thickness variants."""

    totals: dict[tuple[str, str, int], list[int]] = {}
    for part in parts:
        key = (part.material_id, part.material_version, part.thickness_um)
        values = totals.setdefault(key, [0, 0, 0, 0])
        values[0] += part.quantity
        values[1] += part.width_um * part.height_um * part.quantity
        values[2] += part.blank_width_um * part.blank_height_um * part.quantity
        values[3] += part.weight_g * part.quantity

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "material_id",
            "material_version",
            "thickness_mm",
            "part_instances",
            "finished_area_m2",
            "raw_area_m2",
            "total_weight_kg",
        )
    )
    for (material_id, version, thickness_um), values in sorted(totals.items()):
        quantity, finished_area_um2, raw_area_um2, weight_g = values
        writer.writerow(
            (
                material_id,
                version,
                um_to_mm(thickness_um),
                quantity,
                _scaled_decimal(finished_area_um2, 1_000_000_000_000, 6),
                _scaled_decimal(raw_area_um2, 1_000_000_000_000, 6),
                _scaled_decimal(weight_g, 1_000, 3),
            )
        )
    return output.getvalue().encode("utf-8")


def nesting_svg(layout: NestingLayout, sheet_index: int) -> bytes:
    if sheet_index < 0 or sheet_index >= layout.stock.quantity:
        raise ValueError("sheet index outside available stock")
    stock = layout.stock
    width = um_to_mm(stock.width_um)
    height = um_to_mm(stock.height_um)
    elements = [f'<rect class="stock" x="0" y="0" width="{width}" height="{height}"/>']
    for zone in sorted(stock.defect_zones, key=lambda item: (item.y_um, item.x_um)):
        elements.append(_svg_rect(zone, "defect"))
    for zone in sorted(stock.clamp_zones, key=lambda item: (item.y_um, item.x_um)):
        elements.append(_svg_rect(zone, "clamp"))
    for placement in sorted(
        (item for item in layout.placements if item.sheet_index == sheet_index),
        key=lambda item: (item.y_um, item.x_um, item.instance_id),
    ):
        label = escape(placement.instance_id)
        elements.append(
            f'<g data-instance-id="{escape(placement.instance_id, quote=True)}">'
            f'<rect class="part" x="{um_to_mm(placement.x_um)}" y="{um_to_mm(placement.y_um)}" '
            f'width="{um_to_mm(placement.width_um)}" height="{um_to_mm(placement.height_um)}"/>'
            f'<text x="{um_to_mm(placement.x_um + 3_000)}" '
            f'y="{um_to_mm(placement.y_um + 8_000)}">{label}'
            f"{' ↻' if placement.rotated_90 else ''}</text></g>"
        )
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'data-sheet-index="{sheet_index}"><style>'
        ".stock{fill:#f8fafc;stroke:#111;stroke-width:2}.part{fill:#dbeafe;stroke:#1d4ed8;stroke-width:1}"
        ".defect{fill:#fecaca;stroke:#b91c1c}.clamp{fill:#fef3c7;stroke:#b45309}"
        "text{font:8px sans-serif;fill:#111}</style>"
        f"{''.join(elements)}</svg>"
    )
    return svg.encode("utf-8")


def tool_list_csv(document: OperationsDocument) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "setup_id",
            "tool_id",
            "tool_version",
            "nominal_diameter_mm",
            "measured_diameter_mm",
            "effective_diameter_mm",
            "runout_mm",
            "cutting_length_mm",
            "spindle_rpm",
            "feed_mm_min",
            "plunge_mm_min",
            "operation_count",
        )
    )
    tool_by_id = {tool.tool_id: tool for tool in document.tools}
    for setup in sorted(document.setups, key=lambda item: item.setup_id):
        for tool_id in sorted(setup.tool_ids):
            tool = tool_by_id.get(tool_id)
            if tool is None:
                raise ValueError(f"setup references tool absent from frozen snapshot: {tool_id}")
            count = sum(
                operation.setup_id == setup.setup_id and operation.tool_id == tool_id
                for operation in document.operations
            )
            writer.writerow(
                (
                    setup.setup_id,
                    tool_id,
                    tool.version,
                    um_to_mm(tool.diameter_um),
                    (
                        ""
                        if tool.measured_diameter_um is None
                        else um_to_mm(tool.measured_diameter_um)
                    ),
                    um_to_mm(tool.effective_diameter_um),
                    um_to_mm(tool.runout_um),
                    um_to_mm(tool.cutting_length_um),
                    tool.spindle_rpm,
                    um_to_mm(tool.feed_um_min),
                    um_to_mm(tool.plunge_um_min),
                    count,
                )
            )
    return output.getvalue().encode("utf-8")


def setup_sheet_svg(setup: Setup, document: OperationsDocument) -> bytes:
    """Generate a printable operator setup sheet from the frozen CAM setup."""

    operation_count = sum(operation.setup_id == setup.setup_id for operation in document.operations)
    keep_outs = (
        ", ".join(
            (
                f"x={um_to_mm(zone.x_um)}, y={um_to_mm(zone.y_um)}, "
                f"b={um_to_mm(zone.width_um)}, h={um_to_mm(zone.height_um)} mm"
            )
            for zone in setup.keep_out_zones
        )
        or "Inga deklarerade zoner"
    )
    rows = (
        ("Setup-ID", setup.setup_id),
        ("Status", "VALIDERINGSLÄGE – får inte användas för automatisk maskinstart"),
        ("Skiva", f"{setup.stock_id}, skiva {setup.sheet_index + 1}"),
        ("Material", f"{setup.material_id}@{setup.material_version}"),
        (
            "Råmått",
            f"{um_to_mm(setup.stock_width_um)} × {um_to_mm(setup.stock_height_um)} × "
            f"{um_to_mm(setup.stock_thickness_um)} mm",
        ),
        ("Sida / WCS", f"{setup.side.value} / {setup.wcs}"),
        (
            "Arbetsnolla",
            f"X={um_to_mm(setup.origin.x_um)}, Y={um_to_mm(setup.origin.y_um)} mm",
        ),
        ("Referensyta", setup.reference_surface),
        ("Orientering", setup.orientation),
        ("Fixtur", setup.fixture),
        ("Säker Z", f"{um_to_mm(setup.safe_z_um)} mm"),
        ("Nollning", setup.probe_method),
        ("Verktyg", ", ".join(setup.tool_ids) or "Inga"),
        ("Operationer", str(operation_count)),
        ("Keep-out", keep_outs),
    )
    text_rows = []
    for index, (label, value) in enumerate(rows):
        y = 72 + index * 30
        text_rows.append(
            f'<text class="label" x="44" y="{y}">{escape(label)}</text>'
            f'<text class="value" x="220" y="{y}">{escape(value)}</text>'
        )
    step_rows = []
    start_y = 72 + len(rows) * 30 + 20
    for index, step in enumerate(setup.operator_steps, start=1):
        step_rows.append(
            f'<text class="step" x="62" y="{start_y + index * 28}">{index}. {escape(step)}</text>'
        )
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1120 790">'
        "<style>"
        ".page{fill:#fff;stroke:#17211c;stroke-width:2}"
        ".title{font:700 26px sans-serif;fill:#17211c}"
        ".label{font:700 14px sans-serif;fill:#42524a}"
        ".value{font:14px sans-serif;fill:#17211c}"
        ".step{font:14px sans-serif;fill:#17211c}"
        ".warning{font:700 13px sans-serif;fill:#9a3412}"
        "</style>"
        '<rect class="page" x="12" y="12" width="1096" height="766"/>'
        '<text class="title" x="44" y="42">Custombuild setupblad</text>'
        f"{''.join(text_rows)}"
        f'<text class="warning" x="44" y="{start_y}">Operatörssteg</text>'
        f"{''.join(step_rows)}</svg>"
    )
    return svg.encode("utf-8")


def _scaled_decimal(value: int, divisor: int, decimals: int) -> str:
    whole, remainder = divmod(value, divisor)
    scale = 10**decimals
    fraction = remainder * scale // divisor
    return f"{whole}.{fraction:0{decimals}d}"


def _dxf_rectangle(layer: str, x_um: int, y_um: int, width_um: int, height_um: int) -> list[str]:
    points = (
        (x_um, y_um),
        (x_um + width_um, y_um),
        (x_um + width_um, y_um + height_um),
        (x_um, y_um + height_um),
    )
    tokens = ["0", "LWPOLYLINE", "8", layer, "90", "4", "70", "1"]
    for x_value, y_value in points:
        tokens.extend(("10", um_to_mm(x_value), "20", um_to_mm(y_value)))
    return tokens


def _dxf_line(layer: str, x1_um: int, y1_um: int, x2_um: int, y2_um: int) -> list[str]:
    return [
        "0",
        "LINE",
        "8",
        layer,
        "10",
        um_to_mm(x1_um),
        "20",
        um_to_mm(y1_um),
        "11",
        um_to_mm(x2_um),
        "21",
        um_to_mm(y2_um),
    ]


def _dxf_circle(layer: str, x_um: int, y_um: int, radius_um: int) -> list[str]:
    return [
        "0",
        "CIRCLE",
        "8",
        layer,
        "10",
        um_to_mm(x_um),
        "20",
        um_to_mm(y_um),
        "40",
        um_to_mm(radius_um),
    ]


def _dxf_text_entity(layer: str, x_um: int, y_um: int, value: str, height_um: int) -> list[str]:
    return [
        "0",
        "TEXT",
        "8",
        layer,
        "10",
        um_to_mm(x_um),
        "20",
        um_to_mm(y_um),
        "40",
        um_to_mm(height_um),
        "1",
        _dxf_text(value),
    ]


def _dxf_text(value: str) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def _edge_line(edge: str, width_um: int, height_um: int) -> tuple[int, int, int, int] | None:
    normalised = edge.upper()
    if normalised in {"NORTH", "TOP", "BACK"}:
        return (0, height_um, width_um, height_um)
    if normalised in {"SOUTH", "BOTTOM", "FRONT"}:
        return (0, 0, width_um, 0)
    if normalised in {"WEST", "LEFT"}:
        return (0, 0, 0, height_um)
    if normalised in {"EAST", "RIGHT"}:
        return (width_um, 0, width_um, height_um)
    return None


def _svg_rect(rect: Rect, css_class: str) -> str:
    return (
        f'<rect class="{css_class}" x="{um_to_mm(rect.x_um)}" y="{um_to_mm(rect.y_um)}" '
        f'width="{um_to_mm(rect.width_um)}" height="{um_to_mm(rect.height_um)}"/>'
    )
