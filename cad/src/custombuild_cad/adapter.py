"""Optional CadQuery/OpenCascade adapter for authoritative CAD exports."""

from __future__ import annotations

import importlib.util
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

CADQUERY_ADAPTER_VERSION = "cadquery-adapter-1.0.0"
CAD_KERNEL_CONTRACT_VERSION = "cadquery-opencascade-contract-1.0.0"
CADQUERY_DISTRIBUTION_VERSION = "2.5.2"
OPENCASCADE_DISTRIBUTION = "cadquery-ocp"
OPENCASCADE_DISTRIBUTION_VERSION = "7.7.2"


class CADExportError(RuntimeError):
    pass


class CADDependencyUnavailable(CADExportError):
    pass


class UnsupportedCADFeatureError(CADExportError):
    pass


@dataclass(frozen=True, slots=True)
class CADArtifacts:
    step: bytes
    glb: bytes
    kernel: str
    adapter_version: str
    authoritative: bool = True

    def __post_init__(self) -> None:
        if not self.step.startswith(b"ISO-10303-21"):
            raise CADExportError("CadQuery did not produce a genuine STEP file")
        if not self.glb.startswith(b"glTF"):
            raise CADExportError("CadQuery did not produce a genuine binary glTF file")


class CadQueryAdapter:
    version = CADQUERY_ADAPTER_VERSION

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("cadquery") is not None

    def export_design(self, design: Any) -> CADArtifacts:
        """Export a domain DesignResult, returning nothing on partial failure."""

        if not self.available():
            raise CADDependencyUnavailable(
                "CadQuery/OpenCascade is unavailable; STEP and GLB generation is blocked"
            )
        import cadquery as cq

        parts = tuple(
            sorted(design.parts, key=lambda item: (str(item.part_id), int(item.instance_index)))
        )
        if not parts:
            raise CADExportError("cannot export an empty design")

        assembly = cq.Assembly(name=f"custombuild-{design.design_hash}")
        for part in parts:
            shape = self._part_shape(cq, part)
            shape = self._apply_placement(shape, part)
            assembly.add(shape, name=_safe_name(str(part.part_id)))

        with tempfile.TemporaryDirectory(prefix="custombuild-cad-") as temporary:
            directory = Path(temporary)
            step_path = directory / "design.step"
            glb_path = directory / "design.glb"
            try:
                assembly.save(str(step_path), exportType="STEP")
                assembly.save(str(glb_path), exportType="GLTF")
                step = _normalise_step(step_path.read_bytes())
                glb = glb_path.read_bytes()
            except Exception as exc:
                raise CADExportError(f"CadQuery export failed atomically: {exc}") from exc
        return CADArtifacts(
            step=step,
            glb=glb,
            kernel="CadQuery/OpenCascade",
            adapter_version=self.version,
            authoritative=True,
        )

    def _part_shape(self, cq: Any, part: Any) -> Any:
        size = part.finished_size
        width = _mm(size.width_um)
        depth = _mm(size.depth_um)
        height = _mm(size.height_um)
        if min(width, depth, height) <= 0:
            raise CADExportError(f"part {part.part_id} has invalid dimensions")
        shape = cq.Workplane("XY").box(width, depth, height, centered=(False, False, False)).val()
        for feature in sorted(part.features, key=lambda item: str(item.feature_id)):
            shape = self._apply_feature(cq, shape, part, feature)
        return shape

    def _apply_feature(self, cq: Any, shape: Any, part: Any, feature: Any) -> Any:
        kind = _value(feature.kind)
        if kind == "MARK":
            # MARK is semantic labelling and does not claim material removal.
            return shape
        if kind in {"TENON", "EDGE_RELIEF"}:
            raise UnsupportedCADFeatureError(
                f"authoritative CAD for {kind} is not implemented; "
                f"feature {feature.feature_id} blocks export"
            )
        face = _resolve_face(part, _value(feature.face))
        origin = feature.origin
        dimensions = feature.dimensions
        direction, start = _cut_axis_and_start(
            part.finished_size,
            face,
            origin,
            feature.depth_um if hasattr(feature, "depth_um") else dimensions.depth_um,
        )
        depth_um = int(dimensions.depth_um)
        if bool(getattr(feature, "through", False)):
            depth_um += 200
            start = (
                start[0] - direction[0] * 100,
                start[1] - direction[1] * 100,
                start[2] - direction[2] * 100,
            )
        depth = _mm(depth_um)

        if kind == "COUNTERSINK":
            raise UnsupportedCADFeatureError(
                f"countersink angle/profile is not versioned for feature {feature.feature_id}"
            )
        if kind in {"DRILL", "DRILL_PATTERN", "SERIES_DRILL"}:
            diameter_um = getattr(dimensions, "diameter_um", None)
            if diameter_um is None:
                raise UnsupportedCADFeatureError(
                    f"drill feature {feature.feature_id} has no diameter"
                )
            count = int(getattr(feature, "pattern_count", 1))
            pitch_um = int(getattr(feature, "pitch_um", 0) or 0)
            u_axis, _ = _face_plane_axes(face)
            for index in range(count):
                offset = [0, 0, 0]
                offset[u_axis] = pitch_um * index
                point = tuple(_mm(start[axis] + offset[axis]) for axis in range(3))
                cutter = cq.Solid.makeCylinder(
                    _mm(int(diameter_um)) / 2,
                    depth,
                    cq.Vector(*point),
                    cq.Vector(*direction),
                )
                shape = shape.cut(cutter)
            return shape

        if kind == "POCKET" and getattr(dimensions, "diameter_um", None) is not None:
            point = tuple(_mm(value) for value in start)
            cutter = cq.Solid.makeCylinder(
                _mm(int(dimensions.diameter_um)) / 2,
                depth,
                cq.Vector(*point),
                cq.Vector(*direction),
            )
            return shape.cut(cutter)
        if kind in {"POCKET", "GROOVE", "RABBET", "ENGRAVE"}:
            width_um = getattr(dimensions, "width_um", None)
            length_um = getattr(dimensions, "length_um", None)
            if width_um is None or length_um is None:
                raise UnsupportedCADFeatureError(
                    f"rectangular feature {feature.feature_id} has no width/length"
                )
            cutter = _rectangular_cutter(
                cq,
                face,
                start,
                int(width_um),
                int(length_um),
                depth_um,
                direction,
            )
            result = shape.cut(cutter)
            corner_strategy = getattr(feature, "corner_strategy", None)
            requires_square = bool(getattr(feature, "requires_square_corners", False))
            if requires_square and not corner_strategy:
                raise UnsupportedCADFeatureError(
                    f"feature {feature.feature_id} requires an explicit internal-corner strategy"
                )
            if corner_strategy:
                if corner_strategy != "dogbone-v1":
                    raise UnsupportedCADFeatureError(
                        f"unsupported internal-corner strategy {corner_strategy}: "
                        f"{feature.feature_id}"
                    )
                radius_um = getattr(dimensions, "radius_um", None)
                if radius_um is None:
                    raise UnsupportedCADFeatureError(
                        f"dogbone feature {feature.feature_id} has no relief radius"
                    )
                result = _apply_dogbone_relief(
                    cq,
                    result,
                    face,
                    start,
                    int(width_um),
                    int(length_um),
                    depth_um,
                    direction,
                    int(radius_um),
                )
            return result
        raise UnsupportedCADFeatureError(
            f"unsupported authoritative CAD feature {kind}: {feature.feature_id}"
        )

    @staticmethod
    def _apply_placement(shape: Any, part: Any) -> Any:
        placement = part.placement
        result = shape
        rotations = (
            ((1, 0, 0), int(getattr(placement, "rotation_x_mdeg", 0))),
            ((0, 1, 0), int(getattr(placement, "rotation_y_mdeg", 0))),
            ((0, 0, 1), int(getattr(placement, "rotation_z_mdeg", 0))),
        )
        for axis, angle_mdeg in rotations:
            if angle_mdeg:
                result = result.rotate((0, 0, 0), axis, angle_mdeg / 1_000)
        return result.translate((_mm(placement.x_um), _mm(placement.y_um), _mm(placement.z_um)))


def cad_capability_status() -> dict[str, str | bool]:
    available = CadQueryAdapter.available()
    return {
        "available": available,
        "status": "AVAILABLE" if available else "BLOCKED_UNAVAILABLE",
        "kernel": "CadQuery/OpenCascade" if available else "not installed",
        "adapter_version": CadQueryAdapter.version,
    }


def _cut_axis_and_start(
    size: Any,
    face: str,
    origin: Any,
    depth_um: int,
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    point = [int(origin.x_um), int(origin.y_um), int(origin.z_um)]
    dimensions = [int(size.width_um), int(size.depth_um), int(size.height_um)]
    definitions = {
        "LEFT": (0, 1),
        "RIGHT": (0, -1),
        "FRONT": (1, 1),
        "BACK": (1, -1),
        "BOTTOM": (2, 1),
        "TOP": (2, -1),
        "X_NEGATIVE": (0, 1),
        "X_POSITIVE": (0, -1),
        "Y_NEGATIVE": (1, 1),
        "Y_POSITIVE": (1, -1),
        "Z_NEGATIVE": (2, 1),
        "Z_POSITIVE": (2, -1),
    }
    try:
        axis, sign = definitions[face]
    except KeyError as exc:
        raise UnsupportedCADFeatureError(f"unsupported feature face: {face}") from exc
    direction = [0, 0, 0]
    direction[axis] = sign
    # Domain origins are authoritative. If they lie on the opposite side of the
    # stock, anchor the cutter to the declared face instead of guessing a depth.
    point[axis] = 0 if sign > 0 else dimensions[axis]
    return (
        (direction[0], direction[1], direction[2]),
        (point[0], point[1], point[2]),
    )


def _resolve_face(part: Any, face: str) -> str:
    if face not in {"A", "B"}:
        return face
    role = _value(part.role)
    if role in {"LEFT_SIDE", "RIGHT_SIDE", "DIVIDER", "VERTICAL_DIVIDER"}:
        return "LEFT" if face == "A" else "RIGHT"
    if role in {"TOP", "BOTTOM", "SHELF"}:
        return "BOTTOM" if face == "A" else "TOP"
    if role in {"BACK", "BACK_PANEL", "PLINTH"}:
        return "FRONT" if face == "A" else "BACK"
    raise UnsupportedCADFeatureError(f"cannot resolve abstract {face}-side for part role {role}")


def _face_plane_axes(face: str) -> tuple[int, int]:
    if face in {"LEFT", "RIGHT", "X_NEGATIVE", "X_POSITIVE"}:
        return (1, 2)
    if face in {"FRONT", "BACK", "Y_NEGATIVE", "Y_POSITIVE"}:
        return (0, 2)
    if face in {"TOP", "BOTTOM", "Z_NEGATIVE", "Z_POSITIVE"}:
        return (0, 1)
    raise UnsupportedCADFeatureError(f"unsupported feature face: {face}")


def _rectangular_cutter(
    cq: Any,
    face: str,
    start_um: tuple[int, int, int],
    width_um: int,
    length_um: int,
    depth_um: int,
    direction: tuple[int, int, int],
) -> Any:
    u_axis, v_axis = _face_plane_axes(face)
    normal_axis = next(index for index, value in enumerate(direction) if value)
    sizes = [0, 0, 0]
    sizes[u_axis] = width_um
    sizes[v_axis] = length_um
    sizes[normal_axis] = depth_um
    start = list(start_um)
    if direction[normal_axis] < 0:
        start[normal_axis] -= depth_um
    return (
        cq.Workplane("XY")
        .box(
            _mm(sizes[0]),
            _mm(sizes[1]),
            _mm(sizes[2]),
            centered=(False, False, False),
        )
        .translate(tuple(_mm(value) for value in start))
        .val()
    )


def _apply_dogbone_relief(
    cq: Any,
    shape: Any,
    face: str,
    start_um: tuple[int, int, int],
    width_um: int,
    length_um: int,
    depth_um: int,
    direction: tuple[int, int, int],
    radius_um: int,
) -> Any:
    """Cut a versioned circular relief at each nominal rectangular corner."""

    if radius_um <= 0:
        raise UnsupportedCADFeatureError("dogbone relief radius must be positive")
    u_axis, v_axis = _face_plane_axes(face)
    result = shape
    for u_offset, v_offset in (
        (0, 0),
        (width_um, 0),
        (0, length_um),
        (width_um, length_um),
    ):
        point_um = list(start_um)
        point_um[u_axis] += u_offset
        point_um[v_axis] += v_offset
        cutter = cq.Solid.makeCylinder(
            _mm(radius_um),
            _mm(depth_um),
            cq.Vector(*(_mm(value) for value in point_um)),
            cq.Vector(*direction),
        )
        result = result.cut(cutter)
    return result


def _normalise_step(payload: bytes) -> bytes:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = payload.decode("latin-1")
    text = re.sub(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.+-]\d+)?",
        "1970-01-01T00:00:00",
        text,
    )
    occurrence_index = 0

    def normalise_occurrence(match: re.Match[str]) -> str:
        nonlocal occurrence_index
        occurrence_index += 1
        return f"NEXT_ASSEMBLY_USAGE_OCCURRENCE('{occurrence_index}'"

    text = re.sub(
        r"NEXT_ASSEMBLY_USAGE_OCCURRENCE\('\d+'",
        normalise_occurrence,
        text,
    )
    return text.replace("\r\n", "\n").encode("utf-8")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:80] or "part"


def _value(value: Any) -> str:
    raw = value.value if isinstance(value, Enum) else value
    return str(raw).upper()


def _mm(value_um: int) -> float:
    return int(value_um) / 1_000
