"""Deterministic human- and machine-readable manufacturing exports."""

from __future__ import annotations

import csv
import io
import json
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

DXF_LAYERS = (
    "OUTLINE",
    "DRILL",
    "POCKET",
    "GROOVE",
    "RABBET",
    "INNER_CONTOUR",
    "ENGRAVE",
    "EDGE_BAND",
    "LABEL",
)
ARTIFACT_EXPORTERS_VERSION = "manufacturing-exporters-1.4.0"
PART_DRAWING_CONTRACT_VERSION = "custombuild.part-drawing.v2"
PART_DRAWING_COORDINATE_SYSTEM = "LOCAL_UV_MM_V_UP_NOT_MIRRORED"


def dxf_for_part(part: PartSpec, side: Side) -> bytes:
    """Create a UTF-8 DXF with only the requested machining side's features."""

    if side not in (Side.A, Side.B):
        raise ValueError("DXF machining side must be A or B")
    _validate_part_drawing_scope(part)
    side_features = tuple(
        feature
        for feature in sorted(part.features, key=lambda item: item.feature_id)
        if feature.side == side
    )
    outline_feature, outline_bounds = _outline_for_side(part, side_features)
    drawing_extents = _drawing_geometry_extents(part, side)
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

    for feature_index, feature in enumerate(side_features, start=1):
        if feature.kind == FeatureKind.OUTER_CONTOUR:
            continue
        entities.extend(("999", f"FEATURE:{_dxf_text(feature.feature_id)}"))
        entities.extend(
            _dxf_json_comments(
                f"CUSTOMBUILD_FEATURE_{feature_index:04d}_JSON",
                _feature_drawing_metadata(feature),
            )
        )
        if feature.kind in {FeatureKind.DRILL, FeatureKind.DRILL_PATTERN}:
            radius_um = (feature.diameter_um or 0) // 2
            for point in feature.points():
                entities.extend(_dxf_circle("DRILL", point.x_um, point.y_um, radius_um))
        elif feature.kind in {FeatureKind.GROOVE, FeatureKind.RABBET}:
            bounds = feature.bounds()
            layer = "RABBET" if feature.kind == FeatureKind.RABBET else "GROOVE"
            entities.extend(
                _dxf_rectangle(
                    layer, bounds.x_um, bounds.y_um, bounds.width_um, bounds.height_um
                )
            )
            for relief_x_um, relief_y_um in _corner_relief_points(feature):
                assert feature.corner_relief_radius_um is not None
                entities.extend(
                    _dxf_circle(
                        layer,
                        relief_x_um,
                        relief_y_um,
                        feature.corner_relief_radius_um,
                    )
                )
        elif feature.kind in {FeatureKind.POCKET, FeatureKind.INNER_CONTOUR}:
            bounds = feature.bounds()
            layer = (
                "INNER_CONTOUR"
                if feature.kind == FeatureKind.INNER_CONTOUR
                else "POCKET"
            )
            entities.extend(
                _dxf_rectangle(
                    layer, bounds.x_um, bounds.y_um, bounds.width_um, bounds.height_um
                )
            )
            for relief_x_um, relief_y_um in _corner_relief_points(feature):
                assert feature.corner_relief_radius_um is not None
                entities.extend(
                    _dxf_circle(
                        layer,
                        relief_x_um,
                        relief_y_um,
                        feature.corner_relief_radius_um,
                    )
                )
        elif feature.kind == FeatureKind.ENGRAVE:
            bounds = feature.bounds()
            entities.extend(
                _dxf_rectangle(
                    "ENGRAVE",
                    bounds.x_um,
                    bounds.y_um,
                    bounds.width_um,
                    bounds.height_um,
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
        "9",
        "$INSUNITS",
        "70",
        "4",
        "9",
        "$MEASUREMENT",
        "70",
        "1",
        "9",
        "$LUNITS",
        "70",
        "2",
        "9",
        "$LUPREC",
        "70",
        "3",
        "9",
        "$EXTMIN",
        "10",
        um_to_mm(drawing_extents.x_um),
        "20",
        um_to_mm(drawing_extents.y_um),
        "30",
        "0",
        "9",
        "$EXTMAX",
        "10",
        um_to_mm(drawing_extents.right_um),
        "20",
        um_to_mm(drawing_extents.top_um),
        "30",
        "0",
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
        "5",
        "2",
        "330",
        "0",
        "100",
        "AcDbSymbolTable",
        "70",
        str(len(DXF_LAYERS)),
    ]
    for index, layer in enumerate(DXF_LAYERS, start=1):
        tokens.extend(
            (
                "0",
                "LAYER",
                "5",
                f"{0x10 + index:X}",
                "330",
                "2",
                "100",
                "AcDbSymbolTableRecord",
                "100",
                "AcDbLayerTableRecord",
                "2",
                layer,
                "70",
                "0",
                "62",
                str(index),
                "6",
                "CONTINUOUS",
            )
        )
    tokens.extend(("0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"))
    tokens.extend(("999", f"CUSTOMBUILD_SIDE:{side.value}"))
    tokens.extend(
        _dxf_json_comments(
            "CUSTOMBUILD_DRAWING_JSON",
            _part_drawing_metadata(part, side),
        )
    )
    tokens.extend(entities)
    tokens.extend(("0", "ENDSEC", "0", "EOF"))
    return ("\n".join(tokens) + "\n").encode("utf-8")


def svg_for_part(part: PartSpec, side: Side) -> bytes:
    if side not in (Side.A, Side.B):
        raise ValueError("SVG machining side must be A or B")
    _validate_part_drawing_scope(part)
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
        ' data-outline-source="FINISHED_PART_RECTANGULAR_FALLBACK" '
        'data-tolerance-status="EXTERNAL_TOLERANCE_REQUIRED"'
        if outline_feature is None
        else (
            f' data-feature-id="{escape(outline_feature.feature_id, quote=True)}" '
            'data-outline-source="SEMANTIC_OUTER_CONTOUR" '
            f"{_svg_feature_attributes(outline_feature)}"
        )
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
        if feature.kind in {FeatureKind.DRILL, FeatureKind.DRILL_PATTERN}:
            radius = um_to_mm((feature.diameter_um or 0) // 2)
            for point in feature.points():
                elements.append(
                    f'<circle class="drill" data-feature-id="{feature_id}" '
                    f"{_svg_feature_attributes(feature)} "
                    f'cx="{um_to_mm(point.x_um)}" cy="{um_to_mm(point.y_um)}" r="{radius}"/>'
                )
        elif feature.kind == FeatureKind.LABEL:
            elements.append(
                f'<circle class="label-mark" data-feature-id="{feature_id}" '
                f"{_svg_feature_attributes(feature)} "
                f'cx="{um_to_mm(feature.x_um)}" cy="{um_to_mm(feature.y_um)}" r="1"/>'
            )
        else:
            bounds = feature.bounds()
            css_class = (
                "groove"
                if feature.kind == FeatureKind.GROOVE
                else "rabbet"
                if feature.kind == FeatureKind.RABBET
                else "inner-contour"
                if feature.kind == FeatureKind.INNER_CONTOUR
                else "engrave"
                if feature.kind == FeatureKind.ENGRAVE
                else "pocket"
            )
            elements.append(
                f'<rect class="{css_class}" data-feature-id="{feature_id}" '
                f"{_svg_feature_attributes(feature)} "
                f'x="{um_to_mm(bounds.x_um)}" y="{um_to_mm(bounds.y_um)}" '
                f'width="{um_to_mm(bounds.width_um)}" height="{um_to_mm(bounds.height_um)}"/>'
            )
            if feature.corner_relief_radius_um is not None:
                for x_um, y_um in _corner_relief_points(feature):
                    elements.append(
                        f'<circle class="{css_class} corner-relief" '
                        f'data-feature-id="{feature_id}" '
                        f'data-corner-strategy="{feature.corner_strategy}" '
                        f'cx="{um_to_mm(x_um)}" cy="{um_to_mm(y_um)}" '
                        f'r="{um_to_mm(feature.corner_relief_radius_um)}"/>'
                    )
    title = escape(f"{part.part_id} – sida {side.value}")
    drawing_metadata = escape(
        _drawing_json(_part_drawing_metadata(part, side)),
        quote=False,
    )
    physical_face = _drawing_physical_face(part, side)
    physical_face_text = escape(physical_face)
    axis_mapping = part.axis_mapping
    body = "".join(elements)
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="-20 -28 {float(width) + 40:g} {float(height) + 48:g}" '
        f'width="{width}mm" height="{height}mm" data-side="{side.value}" '
        f'data-contract-version="{PART_DRAWING_CONTRACT_VERSION}" '
        f'data-coordinate-system="{PART_DRAWING_COORDINATE_SYSTEM}" '
        f'data-part-id="{escape(part.part_id, quote=True)}" '
        f'data-physical-face="{escape(physical_face, quote=True)}" '
        f'data-material-id="{escape(part.material_id, quote=True)}" '
        f'data-material-version="{escape(part.material_version, quote=True)}" '
        f'data-grain-direction="{escape(part.grain_direction, quote=True)}" '
        f'data-u-axis="{axis_mapping.u_axis}" data-v-axis="{axis_mapping.v_axis}" '
        f'data-thickness-axis="{axis_mapping.thickness_axis}" '
        f'data-thickness-mm="{um_to_mm(part.thickness_um)}">'
        "<style>.outline{fill:none;stroke:#111;stroke-width:.5}"
        ".drill{fill:none;stroke:#06c;stroke-width:.4}"
        ".pocket{fill:#dbeafe;stroke:#2563eb;stroke-width:.4}"
        ".groove{fill:#fef3c7;stroke:#b45309;stroke-width:.4}"
        ".rabbet{fill:#ffedd5;stroke:#c2410c;stroke-width:.4}"
        ".inner-contour{fill:none;stroke:#be123c;stroke-width:.5}"
        ".engrave{fill:none;stroke:#7c3aed;stroke-width:.3}"
        ".label-mark{fill:#111}.datum{fill:#dc2626;stroke:#fff;stroke-width:.3}"
        ".dim{stroke:#555;stroke-width:.25}.label{font:5px sans-serif;fill:#111}</style>"
        f"<title>{title}</title><metadata>{drawing_metadata}</metadata>"
        f'<text class="label" x="0" y="-18">{title}</text>'
        f'<text class="label" x="0" y="-10">T={um_to_mm(part.thickness_um)} mm · '
        f"face={physical_face_text} · origin=U_MIN/V_MIN · U={axis_mapping.u_axis.upper()} · "
        f"V={axis_mapping.v_axis.upper()} · coordinates not mirrored</text>"
        f'<g class="machining-geometry" transform="translate(0 {height}) scale(1 -1)">{body}</g>'
        f'<circle class="datum" cx="0" cy="{height}" r="1.5"/>'
        f'<text class="label" x="3" y="{float(height) - 3:g}">U0/V0</text>'
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


def _validate_part_drawing_scope(part: PartSpec) -> None:
    """Fail closed when a two-face drawing would omit or misstate geometry."""

    identifiers = [feature.feature_id for feature in part.features]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"part {part.part_id} has duplicate feature IDs")
    if part.blank_width_um < part.width_um or part.blank_height_um < part.height_um:
        raise ValueError(f"part {part.part_id} raw blank is smaller than its finished perimeter")
    edge_features = tuple(
        feature.feature_id for feature in part.features if feature.side == Side.EDGE
    )
    if edge_features:
        raise ValueError(
            f"part {part.part_id} has edge-machining features that A/B drawings cannot represent: "
            f"{', '.join(sorted(edge_features))}"
        )

    rectangular = {
        FeatureKind.POCKET,
        FeatureKind.GROOVE,
        FeatureKind.RABBET,
        FeatureKind.INNER_CONTOUR,
        FeatureKind.OUTER_CONTOUR,
        FeatureKind.ENGRAVE,
    }
    part_bounds = Rect(0, 0, part.width_um, part.height_um)
    for feature in part.features:
        if feature.kind == FeatureKind.COUNTERSINK:
            raise ValueError(
                f"countersink feature {feature.feature_id} has no versioned angle/top-diameter "
                "contract and cannot enter a supplier drawing"
            )
        if feature.kind in {FeatureKind.DRILL, FeatureKind.DRILL_PATTERN}:
            if feature.diameter_um is None:
                raise ValueError(f"drill feature {feature.feature_id} has no diameter")
        elif feature.kind in rectangular and (
            feature.width_um is None or feature.length_um is None
        ):
            raise ValueError(f"rectangular feature {feature.feature_id} has no width/length")
        if feature.kind not in {
            FeatureKind.LABEL,
            FeatureKind.OUTER_CONTOUR,
        } and not part_bounds.contains(feature.bounds()):
            raise ValueError(
                f"feature {feature.feature_id} lies outside the finished-part drawing bounds"
            )
        if feature.through:
            if feature.depth_um < part.thickness_um:
                raise ValueError(
                    f"through feature {feature.feature_id} does not reach the full part thickness"
                )
        elif feature.depth_um >= part.thickness_um and feature.kind != FeatureKind.LABEL:
            raise ValueError(
                f"non-through feature {feature.feature_id} reaches or exceeds the part thickness"
            )


def _drawing_physical_face(part: PartSpec, side: Side) -> str:
    key = "domain_a_side" if side == Side.A else "domain_b_side"
    raw = part.metadata.get(key)
    return str(raw) if isinstance(raw, str) and raw else f"SIDE_{side.value}"


def _part_drawing_metadata(part: PartSpec, side: Side) -> dict[str, object]:
    extents = _drawing_geometry_extents(part, side)
    side_features = tuple(feature for feature in part.features if feature.side == side)
    outline_feature, _ = _outline_for_side(part, side_features)
    physical_face = _drawing_physical_face(part, side)
    return {
        "schema_version": PART_DRAWING_CONTRACT_VERSION,
        "part_id": part.part_id,
        "part_name": part.name,
        "quantity": part.quantity,
        "side": side.value,
        "physical_face": physical_face,
        "units": "mm",
        "coordinate_system": PART_DRAWING_COORDINATE_SYSTEM,
        "coordinates_mirrored": False,
        "origin": "FINISHED_PART_U_MIN_V_MIN",
        "datums": {
            "primary": f"{physical_face}_FINISHED_SURFACE",
            "secondary": "U_MIN_FINISHED_EDGE",
            "tertiary": "V_MIN_FINISHED_EDGE",
        },
        "axis_mapping": {
            "u": part.axis_mapping.u_axis,
            "v": part.axis_mapping.v_axis,
            "thickness": part.axis_mapping.thickness_axis,
        },
        "material": {"id": part.material_id, "version": part.material_version},
        "grain_direction": part.grain_direction,
        "allow_rotation": part.allow_rotation,
        "edge_bands": [
            {
                "edge": detail.edge,
                "thickness_mm": um_to_mm(detail.thickness_um),
                "source_face": detail.source_face,
                "catalog_id": detail.catalog_id,
                "catalog_version": detail.catalog_version,
                "attachment_method": detail.attachment_method,
                "procurement_status": detail.procurement_status,
            }
            for detail in part.edge_band_details
        ],
        "finished_size_mm": {
            "u": um_to_mm(part.width_um),
            "v": um_to_mm(part.height_um),
            "thickness": um_to_mm(part.thickness_um),
        },
        "raw_blank_size_mm": {
            "u": um_to_mm(part.blank_width_um),
            "v": um_to_mm(part.blank_height_um),
            "thickness": um_to_mm(part.thickness_um),
        },
        "emitted_geometry_extents_mm": {
            "u_min": um_to_mm(extents.x_um),
            "v_min": um_to_mm(extents.y_um),
            "u_max": um_to_mm(extents.right_um),
            "v_max": um_to_mm(extents.top_um),
        },
        "feature_count_on_side": sum(feature.side == side for feature in part.features),
        "feature_tolerances_are_individual": True,
        "finished_outline": {
            "source": (
                "SEMANTIC_OUTER_CONTOUR"
                if outline_feature is not None
                else "FINISHED_PART_RECTANGULAR_FALLBACK"
            ),
            "feature": (
                _feature_drawing_metadata(outline_feature)
                if outline_feature is not None
                else None
            ),
            "tolerance_mm": (
                um_to_mm(outline_feature.tolerance_um)
                if outline_feature is not None and outline_feature.tolerance_um > 0
                else None
            ),
            "tolerance_status": (
                "DECLARED_IN_DESIGN"
                if outline_feature is not None and outline_feature.tolerance_um > 0
                else "EXTERNAL_TOLERANCE_REQUIRED"
            ),
        },
    }


def _drawing_geometry_extents(part: PartSpec, side: Side) -> Rect:
    side_features = tuple(feature for feature in part.features if feature.side == side)
    _, outline = _outline_for_side(part, side_features)
    bounds = [
        feature.machining_bounds()
        for feature in side_features
        if feature.kind not in {FeatureKind.OUTER_CONTOUR, FeatureKind.LABEL}
    ]
    left = min((value.x_um for value in bounds), default=outline.x_um)
    bottom = min((value.y_um for value in bounds), default=outline.y_um)
    right = max((value.right_um for value in bounds), default=outline.right_um)
    top = max((value.top_um for value in bounds), default=outline.top_um)
    return Rect(
        min(outline.x_um, left),
        min(outline.y_um, bottom),
        max(outline.right_um, right) - min(outline.x_um, left),
        max(outline.top_um, top) - min(outline.y_um, bottom),
    )


def _feature_drawing_metadata(feature: ManufacturingFeature) -> dict[str, object]:
    origin_semantics = (
        "FIRST_CENTRE"
        if feature.kind in {FeatureKind.DRILL, FeatureKind.DRILL_PATTERN}
        else "LOWER_LEFT"
    )
    dimensions = {
        key: um_to_mm(value)
        for key, value in (
            ("diameter", feature.diameter_um),
            ("width", feature.width_um),
            ("length", feature.length_um),
            ("radius", feature.radius_um),
            ("pattern_pitch", feature.pitch_um),
            ("corner_relief_radius", feature.corner_relief_radius_um),
        )
        if value is not None
    }
    return {
        "feature_id": feature.feature_id,
        "kind": feature.kind.value,
        "side": feature.side.value,
        "origin_semantics": origin_semantics,
        "origin_mm": {"u": um_to_mm(feature.x_um), "v": um_to_mm(feature.y_um)},
        "depth_mm": um_to_mm(feature.depth_um),
        "dimensions_mm": dimensions,
        "pattern_count": feature.pattern_count,
        "through": feature.through,
        "tolerance_mm": (
            um_to_mm(feature.tolerance_um) if feature.tolerance_um > 0 else None
        ),
        "tolerance_status": (
            "DECLARED_IN_DESIGN"
            if feature.tolerance_um > 0
            else "EXTERNAL_TOLERANCE_REQUIRED"
        ),
        "fit_clearance_mm": um_to_mm(feature.fit_clearance_um),
        "corner_strategy": feature.corner_strategy,
        "open_end_reliefs": list(feature.open_end_reliefs),
    }


def _dxf_json_comments(label: str, payload: dict[str, object]) -> list[str]:
    """Encode canonical JSON into legal, deterministic DXF comment chunks."""

    encoded = _drawing_json(payload)
    chunk_size = 180
    chunks = tuple(
        encoded[index : index + chunk_size] for index in range(0, len(encoded), chunk_size)
    )
    tokens: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        comment = f"{label}:{index}/{len(chunks)}:{chunk}"
        if len(comment.encode("utf-8")) > 255:
            raise ValueError("DXF metadata comment exceeds the 255-byte interoperability limit")
        tokens.extend(("999", comment))
    return tokens


def _drawing_json(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _svg_feature_attributes(feature: ManufacturingFeature) -> str:
    attributes = {
        "data-kind": feature.kind.value,
        "data-origin-u-mm": um_to_mm(feature.x_um),
        "data-origin-v-mm": um_to_mm(feature.y_um),
        "data-depth-mm": um_to_mm(feature.depth_um),
        "data-through": "true" if feature.through else "false",
        "data-tolerance-status": (
            "DECLARED_IN_DESIGN"
            if feature.tolerance_um > 0
            else "EXTERNAL_TOLERANCE_REQUIRED"
        ),
        "data-fit-clearance-mm": um_to_mm(feature.fit_clearance_um),
        "data-pattern-count": str(feature.pattern_count),
    }
    optional_dimensions = {
        "data-diameter-mm": feature.diameter_um,
        "data-width-mm": feature.width_um,
        "data-length-mm": feature.length_um,
        "data-radius-mm": feature.radius_um,
        "data-pattern-pitch-mm": feature.pitch_um,
        "data-corner-relief-radius-mm": feature.corner_relief_radius_um,
    }
    attributes.update(
        {name: um_to_mm(value) for name, value in optional_dimensions.items() if value is not None}
    )
    if feature.tolerance_um > 0:
        attributes["data-tolerance-mm"] = um_to_mm(feature.tolerance_um)
    if feature.corner_strategy is not None:
        attributes["data-corner-strategy"] = feature.corner_strategy
    if feature.open_end_reliefs:
        attributes["data-open-end-reliefs"] = ",".join(feature.open_end_reliefs)
    return " ".join(f'{name}="{escape(value, quote=True)}"' for name, value in attributes.items())


def _corner_relief_points(feature: ManufacturingFeature) -> tuple[tuple[int, int], ...]:
    if feature.corner_strategy is None:
        return ()
    if (
        feature.corner_strategy not in {"dogbone-v1", "dogbone-v2"}
        or feature.corner_relief_radius_um is None
    ):
        raise ValueError(
            f"feature {feature.feature_id} has an unsupported or incomplete corner strategy"
        )
    bounds = feature.bounds()
    declared = set(feature.open_end_reliefs)
    return tuple(
        (x_um, y_um)
        for x_um, y_um, u_boundary, v_boundary in (
            (bounds.x_um, bounds.y_um, "u_min", "v_min"),
            (bounds.right_um, bounds.y_um, "u_max", "v_min"),
            (bounds.x_um, bounds.top_um, "u_min", "v_max"),
            (bounds.right_um, bounds.top_um, "u_max", "v_max"),
        )
        if not (
            {u_boundary, v_boundary} <= declared
            if feature.corner_strategy == "dogbone-v1"
            else bool({u_boundary, v_boundary} & declared)
        )
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
        outline = outlines[0]
        bounds = outline.bounds()
        expected = Rect(0, 0, part.width_um, part.height_um)
        if bounds != expected or not outline.through or outline.depth_um != part.thickness_um:
            raise ValueError(
                f"part {part.part_id} side {outline.side.value} outer contour does not exactly "
                "match the finished-part perimeter and thickness"
            )
        return outline, bounds
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
            "edge_band_thicknesses_mm",
            "edge_band_source_faces",
            "edge_band_catalog_refs",
            "edge_band_attachment_methods",
            "edge_band_procurement_statuses",
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
                "|".join(part.edge_bands),
                "|".join(um_to_mm(detail.thickness_um) for detail in part.edge_band_details),
                "|".join(detail.source_face for detail in part.edge_band_details),
                "|".join(
                    (
                        f"{detail.catalog_id}@{detail.catalog_version}"
                        if detail.catalog_id is not None
                        else "UNRESOLVED"
                    )
                    for detail in part.edge_band_details
                ),
                "|".join(detail.attachment_method for detail in part.edge_band_details),
                "|".join(detail.procurement_status for detail in part.edge_band_details),
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
    tokens = [
        "0",
        "LWPOLYLINE",
        "100",
        "AcDbEntity",
        "8",
        layer,
        "100",
        "AcDbPolyline",
        "90",
        "4",
        "70",
        "1",
    ]
    for x_value, y_value in points:
        tokens.extend(("10", um_to_mm(x_value), "20", um_to_mm(y_value)))
    return tokens


def _dxf_line(layer: str, x1_um: int, y1_um: int, x2_um: int, y2_um: int) -> list[str]:
    return [
        "0",
        "LINE",
        "100",
        "AcDbEntity",
        "8",
        layer,
        "100",
        "AcDbLine",
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
        "100",
        "AcDbEntity",
        "8",
        layer,
        "100",
        "AcDbCircle",
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
        "100",
        "AcDbEntity",
        "8",
        layer,
        "100",
        "AcDbText",
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
    if normalised in {"NORTH", "TOP", "BACK", "V_MAX"}:
        return (0, height_um, width_um, height_um)
    if normalised in {"SOUTH", "BOTTOM", "FRONT", "V_MIN"}:
        return (0, 0, width_um, 0)
    if normalised in {"WEST", "LEFT", "U_MIN"}:
        return (0, 0, 0, height_um)
    if normalised in {"EAST", "RIGHT", "U_MAX"}:
        return (width_um, 0, width_um, height_um)
    return None


def _svg_rect(rect: Rect, css_class: str) -> str:
    return (
        f'<rect class="{css_class}" x="{um_to_mm(rect.x_um)}" y="{um_to_mm(rect.y_um)}" '
        f'width="{um_to_mm(rect.width_um)}" height="{um_to_mm(rect.height_um)}"/>'
    )
