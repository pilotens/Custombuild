"""Duck-typed boundary between the furniture domain and manufacturing engine."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .model import (
    EdgeBandSpec,
    FeatureKind,
    ManufacturingFeature,
    PanelAxisMapping,
    PartSpec,
    Side,
)

MANUFACTURING_ADAPTER_VERSION = "domain-to-manufacturing-adapter-1.3.0"


@runtime_checkable
class DesignResultLike(Protocol):
    design_hash: str
    parts: Iterable[Any]


@dataclass(frozen=True, slots=True)
class AdaptedDesign:
    design_hash: str
    engine_version: str
    template_version: str
    parts: tuple[PartSpec, ...]


_ROLE_AXES: dict[str, PanelAxisMapping] = {
    "LEFT_SIDE": PanelAxisMapping("y", "z", "x"),
    "RIGHT_SIDE": PanelAxisMapping("y", "z", "x"),
    "SIDE_LEFT": PanelAxisMapping("y", "z", "x"),
    "SIDE_RIGHT": PanelAxisMapping("y", "z", "x"),
    "VERTICAL_DIVIDER": PanelAxisMapping("y", "z", "x"),
    "DIVIDER": PanelAxisMapping("y", "z", "x"),
    "BASE_SIDE": PanelAxisMapping("y", "z", "x"),
    "TOP": PanelAxisMapping("x", "y", "z"),
    "BOTTOM": PanelAxisMapping("x", "y", "z"),
    "SHELF": PanelAxisMapping("x", "y", "z"),
    "BASE_BOTTOM": PanelAxisMapping("x", "y", "z"),
    "BASE_TOP": PanelAxisMapping("x", "y", "z"),
    "PLINTH": PanelAxisMapping("x", "z", "y"),
    "BASE": PanelAxisMapping("x", "y", "z"),
    "BACK": PanelAxisMapping("x", "z", "y"),
    "BACK_PANEL": PanelAxisMapping("x", "z", "y"),
    "CABINET_FRONT": PanelAxisMapping("x", "z", "y"),
}


_FEATURE_ALIASES: dict[str, FeatureKind] = {
    "DRILL": FeatureKind.DRILL,
    "BORE": FeatureKind.DRILL,
    "DRILL_PATTERN": FeatureKind.DRILL_PATTERN,
    "SERIES_DRILL": FeatureKind.DRILL_PATTERN,
    "LINE_BORE": FeatureKind.DRILL_PATTERN,
    "COUNTERSINK": FeatureKind.COUNTERSINK,
    "POCKET": FeatureKind.POCKET,
    "GROOVE": FeatureKind.GROOVE,
    "DADO": FeatureKind.GROOVE,
    "SLOT": FeatureKind.GROOVE,
    "RABBET": FeatureKind.RABBET,
    "REBATE": FeatureKind.RABBET,
    "INNER_CONTOUR": FeatureKind.INNER_CONTOUR,
    "OUTER_CONTOUR": FeatureKind.OUTER_CONTOUR,
    "TENON": FeatureKind.OUTER_CONTOUR,
    "EDGE_RELIEF": FeatureKind.RABBET,
    "CONTOUR": FeatureKind.OUTER_CONTOUR,
    "ENGRAVE": FeatureKind.ENGRAVE,
    "LABEL": FeatureKind.LABEL,
    "MARK": FeatureKind.LABEL,
}


def adapt_design_result(design: DesignResultLike) -> AdaptedDesign:
    parts = tuple(adapt_domain_part(part) for part in design.parts)
    parts = _annotate_compatible_corner_overlaps(parts, getattr(design, "joints", ()))
    identifiers = [part.part_id for part in parts]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("domain DesignResult contains duplicate part_id values")
    return AdaptedDesign(
        design_hash=str(design.design_hash),
        engine_version=str(getattr(design, "engine_version", "unknown")),
        template_version=str(getattr(design, "template_version", "unknown")),
        parts=parts,
    )


def _annotate_compatible_corner_overlaps(
    parts: tuple[PartSpec, ...],
    domain_joints: Iterable[Any],
) -> tuple[PartSpec, ...]:
    """Allow only topology-proven crossing grooves at three-member corners.

    A side's carcass dado and back-panel dado intentionally cross where the
    bottom/top and back also have their own joint. DFM must not treat that
    three-way corner as an accidental collision, but it must continue blocking
    arbitrary overlapping features. The complete joint triangle is therefore
    required before either feature receives an overlap exception.
    """

    joint_members: dict[str, frozenset[str]] = {}
    connections: set[frozenset[str]] = set()
    for joint in domain_joints:
        if _enum_value(getattr(joint, "joint_type", "")) != "DADO":
            continue
        members = frozenset(str(member.part_id) for member in getattr(joint, "members", ()))
        joint_id = str(getattr(joint, "joint_id", ""))
        if joint_id and len(members) == 2:
            joint_members[joint_id] = members
            connections.add(members)
    if not joint_members:
        return parts

    updated_parts: list[PartSpec] = []
    cut_kinds = {FeatureKind.GROOVE, FeatureKind.RABBET}
    for part in parts:
        allowed: dict[str, set[str]] = {feature.feature_id: set() for feature in part.features}
        ordered = sorted(part.features, key=lambda item: item.feature_id)
        for index, first in enumerate(ordered):
            for second in ordered[index + 1 :]:
                if (
                    first.side != second.side
                    or first.kind not in cut_kinds
                    or second.kind not in cut_kinds
                    or not first.bounds().intersects(second.bounds())
                ):
                    continue
                first_joint = str(first.metadata.get("joint_id") or "")
                second_joint = str(second.metadata.get("joint_id") or "")
                first_members = joint_members.get(first_joint, frozenset())
                second_members = joint_members.get(second_joint, frozenset())
                first_mates = first_members - {part.part_id}
                second_mates = second_members - {part.part_id}
                if len(first_mates) != 1 or len(second_mates) != 1:
                    continue
                mate_connection = frozenset((*first_mates, *second_mates))
                if len(mate_connection) == 2 and mate_connection in connections:
                    allowed[first.feature_id].add(second.feature_id)
                    allowed[second.feature_id].add(first.feature_id)

        features = tuple(
            replace(
                feature,
                metadata={
                    **dict(feature.metadata),
                    "allow_overlap_with": tuple(sorted(allowed[feature.feature_id])),
                },
            )
            if allowed[feature.feature_id]
            else feature
            for feature in part.features
        )
        updated_parts.append(replace(part, features=features))
    return tuple(updated_parts)


def adapt_domain_part(part: Any) -> PartSpec:
    finished = _dimensions(part.finished_size)
    raw = _dimensions(getattr(part, "raw_size", part.finished_size))
    thickness_um = int(part.actual_thickness_um)
    mapping = _axis_mapping(part, finished, thickness_um)
    width_um = finished[mapping.u_axis]
    height_um = finished[mapping.v_axis]
    raw_width_um = raw[mapping.u_axis]
    raw_height_um = raw[mapping.v_axis]

    a_side = _enum_value(getattr(part, "a_side", ""))
    b_side = _enum_value(getattr(part, "b_side", ""))
    source_features = tuple(
        adapt_domain_feature(feature, mapping=mapping, a_side=a_side, b_side=b_side)
        for feature in getattr(part, "features", ())
        if _enum_value(feature.kind) != "MARK"
    )
    outline = ManufacturingFeature(
        feature_id=f"outline:{part.part_id}",
        part_id=str(part.part_id),
        kind=FeatureKind.OUTER_CONTOUR,
        side=Side.A,
        x_um=0,
        y_um=0,
        depth_um=thickness_um,
        width_um=width_um,
        length_um=height_um,
        through=True,
        metadata={
            "derived": True,
            "is_part_outline": True,
            "compensation": "OUTSIDE",
            "holding_strategy": "TABS_OR_ONION_SKIN_REQUIRES_SETUP_APPROVAL",
        },
    )
    if any(feature.feature_id == outline.feature_id for feature in source_features):
        raise ValueError(f"domain feature collides with derived outline ID for {part.part_id}")
    features = (*source_features, outline)
    edge_band_details = tuple(
        sorted(
            (
                _adapt_edge_band(edge_band, mapping=mapping, part_id=str(part.part_id))
                for edge_band in getattr(part, "edge_bands", ())
            ),
            key=lambda item: item.edge,
        )
    )
    edge_bands = tuple(detail.edge for detail in edge_band_details)
    return PartSpec(
        part_id=str(part.part_id),
        name=_enum_value(getattr(part, "role", part.part_id)),
        width_um=width_um,
        height_um=height_um,
        thickness_um=thickness_um,
        material_id=str(part.material_id),
        material_version=str(part.material_version),
        features=tuple(features),
        grain_direction=_panel_grain_direction(
            _enum_value(getattr(part, "grain_direction", "NONE")), mapping
        ),
        allow_rotation=bool(getattr(part, "allow_rotation", True)),
        quantity=1,
        weight_g=int(getattr(part, "weight_g", 0)),
        raw_width_um=raw_width_um,
        raw_height_um=raw_height_um,
        axis_mapping=mapping,
        edge_bands=edge_bands,
        edge_band_details=edge_band_details,
        metadata={
            "domain_instance_index": int(getattr(part, "instance_index", 0)),
            "domain_a_side": a_side,
            "domain_b_side": b_side,
            "finished_xyz_um": finished,
            "raw_xyz_um": raw,
            "weight_basis": "conservative-finished-blank-before-machining-v1",
        },
    )


def adapt_domain_feature(
    feature: Any,
    *,
    mapping: PanelAxisMapping,
    a_side: str,
    b_side: str,
) -> ManufacturingFeature:
    kind_name = _enum_value(feature.kind)
    try:
        kind = _FEATURE_ALIASES[kind_name]
    except KeyError as exc:
        raise ValueError(f"unsupported domain feature kind: {kind_name}") from exc

    face = _enum_value(feature.face)
    if face in {"A", a_side}:
        side = Side.A
    elif face in {"B", b_side}:
        side = Side.B
    else:
        side = _side_from_face(face, mapping)

    origin = feature.origin
    coordinate = {
        "x": int(origin.x_um),
        "y": int(origin.y_um),
        "z": int(origin.z_um),
    }
    dimensions = feature.dimensions
    return ManufacturingFeature(
        feature_id=str(feature.feature_id),
        part_id=str(feature.part_id),
        kind=kind,
        side=side,
        x_um=coordinate[mapping.u_axis],
        y_um=coordinate[mapping.v_axis],
        depth_um=int(_required_dimension(dimensions, "depth_um")),
        diameter_um=_optional_dimension(dimensions, "diameter_um"),
        width_um=_optional_dimension(dimensions, "width_um"),
        length_um=_optional_dimension(dimensions, "length_um"),
        radius_um=_optional_dimension(dimensions, "radius_um"),
        pattern_count=int(getattr(feature, "pattern_count", 1)),
        pitch_um=_optional_int(getattr(feature, "pitch_um", None)),
        through=bool(getattr(feature, "through", False)),
        corner_strategy=getattr(feature, "corner_strategy", None),
        corner_relief_radius_um=(
            _optional_dimension(dimensions, "radius_um")
            if getattr(feature, "corner_strategy", None)
            else None
        ),
        open_end_reliefs=tuple(
            _enum_value(value).lower() for value in getattr(feature, "open_end_reliefs", ())
        ),
        tolerance_um=int(getattr(feature, "tolerance_um", 0)),
        fit_clearance_um=int(getattr(feature, "fit_clearance_um", 0)),
        metadata={
            "domain_face": face,
            "joint_id": getattr(feature, "joint_id", None),
            "requires_square_corners": bool(getattr(feature, "requires_square_corners", False)),
        },
    )


def _dimensions(value: Any) -> dict[str, int]:
    return {
        "x": int(value.width_um),
        "y": int(value.depth_um),
        "z": int(value.height_um),
    }


def _axis_mapping(
    part: Any,
    dimensions: dict[str, int],
    thickness_um: int,
) -> PanelAxisMapping:
    role = _enum_value(getattr(part, "role", ""))
    if role in _ROLE_AXES:
        mapping = _ROLE_AXES[role]
        axis_dimension = dimensions[mapping.thickness_axis]
        if abs(axis_dimension - thickness_um) <= max(500, thickness_um // 20):
            return mapping

    differences = sorted(
        (
            (abs(value - thickness_um), {"z": 0, "y": 1, "x": 2}[axis], axis)
            for axis, value in dimensions.items()
        )
    )
    thickness_axis = differences[0][2]
    if differences[0][0] > max(500, thickness_um // 20):
        raise ValueError(
            f"cannot infer panel thickness axis for part {part.part_id}: "
            f"no dimension matches actual thickness {thickness_um} µm"
        )
    remaining = [axis for axis in ("x", "y", "z") if axis != thickness_axis]
    return PanelAxisMapping(remaining[0], remaining[1], thickness_axis)


def _adapt_edge_band(
    edge_band: Any,
    *,
    mapping: PanelAxisMapping,
    part_id: str,
) -> EdgeBandSpec:
    source_face = _enum_value(getattr(edge_band, "edge", edge_band))
    axis_and_boundary = {
        "LEFT": ("x", "MIN"),
        "RIGHT": ("x", "MAX"),
        "FRONT": ("y", "MIN"),
        "BACK": ("y", "MAX"),
        "BOTTOM": ("z", "MIN"),
        "TOP": ("z", "MAX"),
    }
    try:
        source_axis, boundary = axis_and_boundary[source_face]
    except KeyError as exc:
        raise ValueError(f"part {part_id} has unsupported edge-band face {source_face}") from exc
    if source_axis == mapping.thickness_axis:
        raise ValueError(
            f"part {part_id} edge-band face {source_face} is normal to panel thickness"
        )
    if source_axis == mapping.u_axis:
        local_edge = f"U_{boundary}"
    elif source_axis == mapping.v_axis:
        local_edge = f"V_{boundary}"
    else:
        raise ValueError(
            f"part {part_id} edge-band face {source_face} does not map to a panel boundary"
        )
    thickness = getattr(edge_band, "thickness_um", None)
    if thickness is None:
        raise ValueError(f"part {part_id} edge-band face {source_face} has no thickness")
    return EdgeBandSpec(
        edge=local_edge,
        thickness_um=int(thickness),
        source_face=source_face,
    )


def _side_from_face(face: str, mapping: PanelAxisMapping) -> Side:
    normal_faces = {
        "x": {
            "LEFT": Side.A,
            "X_NEGATIVE": Side.A,
            "RIGHT": Side.B,
            "X_POSITIVE": Side.B,
        },
        "y": {
            "FRONT": Side.A,
            "Y_NEGATIVE": Side.A,
            "BACK": Side.B,
            "Y_POSITIVE": Side.B,
        },
        "z": {
            "BOTTOM": Side.A,
            "Z_NEGATIVE": Side.A,
            "TOP": Side.B,
            "Z_POSITIVE": Side.B,
        },
    }
    return normal_faces[mapping.thickness_axis].get(face, Side.EDGE)


def _panel_grain_direction(value: str, mapping: PanelAxisMapping) -> str:
    normalised = value.lower()
    if normalised in {"none", "no_grain", "any", "unspecified"}:
        return "NONE"
    if normalised == mapping.u_axis:
        return "X"
    if normalised == mapping.v_axis:
        return "Y"
    if normalised == mapping.thickness_axis:
        raise ValueError("grain direction cannot run through panel thickness")
    raise ValueError(f"unsupported domain grain direction: {value}")


def _enum_value(value: Any) -> str:
    raw = value.value if isinstance(value, Enum) else value
    return str(raw).upper()


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_dimension(dimensions: Any, name: str) -> int | None:
    return _optional_int(getattr(dimensions, name, None))


def _required_dimension(dimensions: Any, name: str) -> int:
    value = _optional_dimension(dimensions, name)
    if value is None:
        raise ValueError(f"domain feature is missing required {name}")
    return value
