"""Optional CadQuery/OpenCascade adapter for authoritative CAD exports."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import struct
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

CADQUERY_ADAPTER_VERSION = "cadquery-adapter-2.1.0"
CAD_KERNEL_CONTRACT_VERSION = "cadquery-opencascade-contract-2.1.0"
CADQUERY_DISTRIBUTION_VERSION = "2.8.0"
OPENCASCADE_DISTRIBUTION = "cadquery-ocp"
OPENCASCADE_DISTRIBUTION_VERSION = "7.9.3.1.1"

# Manufacturing solids are checked in millimetres. Source BREP must retain the
# nominal envelope within 1 micrometre. STEP may add at most 5 micrometres in a
# standards round-trip. The preview mesh may deviate by 50 micrometres because
# it is tessellated; its reconstructed volume must remain within one percent.
CAD_SOURCE_LINEAR_TOLERANCE_MM = 0.001
CAD_STEP_LINEAR_TOLERANCE_MM = 0.005
CAD_GLB_LINEAR_TOLERANCE_M = 0.00005
CAD_STEP_VOLUME_RELATIVE_TOLERANCE = 1e-8
CAD_GLB_VOLUME_RELATIVE_TOLERANCE = 0.01
CAD_VOLUME_ABSOLUTE_TOLERANCE_MM3 = 1e-6
CAD_VOLUME_ABSOLUTE_TOLERANCE_M3 = CAD_VOLUME_ABSOLUTE_TOLERANCE_MM3 / 1_000_000_000
MM_TO_M = 0.001
MM3_TO_M3 = 0.000000001


class CADExportError(RuntimeError):
    pass


class CADDependencyUnavailable(CADExportError):
    pass


class UnsupportedCADFeatureError(CADExportError):
    pass


@dataclass(frozen=True, slots=True)
class AssemblyCollision:
    """One exact positive-volume intersection in the placed assembly."""

    part_ids: tuple[str, str]
    overlap_volume_mm3: float
    overlap_aabb_mm: tuple[float, float, float, float, float, float]
    declared_joint_ids: tuple[str, ...]
    verified_joint_ids: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "part_ids": list(self.part_ids),
            "overlap_volume_mm3": self.overlap_volume_mm3,
            "overlap_aabb_mm": list(self.overlap_aabb_mm),
            "declared_joint_ids": list(self.declared_joint_ids),
            "verified_joint_ids": list(self.verified_joint_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AssemblyCollisionReport:
    """Deterministic evidence emitted by the authoritative assembly gate."""

    checked_pair_count: int
    exact_intersection_count: int
    collisions: tuple[AssemblyCollision, ...]
    schema_version: str = "custombuild.cad-assembly-collision.v1"

    @property
    def passed(self) -> bool:
        return not self.collisions

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "passed": self.passed,
            "checked_pair_count": self.checked_pair_count,
            "exact_intersection_count": self.exact_intersection_count,
            "collision_count": len(self.collisions),
            "collisions": [collision.as_dict() for collision in self.collisions],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


class CADAssemblyCollisionError(CADExportError):
    """Raised before export when exact assembly solids occupy the same volume."""

    def __init__(self, report: AssemblyCollisionReport) -> None:
        self.report = report
        super().__init__(f"authoritative assembly collision gate failed: {report.to_json()}")


@dataclass(frozen=True, slots=True)
class _PartGeometry:
    name: str
    bounds_mm: tuple[float, float, float, float, float, float]
    volume_mm3: float


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


_SUCCESSFUL_CAD_VALIDATIONS: OrderedDict[tuple[str, str, str, str], None] = OrderedDict()
_SUCCESSFUL_CAD_VALIDATION_CACHE_SIZE = 16


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
        expected: dict[str, _PartGeometry] = {}
        placed_shapes: dict[str, Any] = {}
        for part in parts:
            name = _safe_name(str(part.part_id))
            if name in expected:
                raise CADExportError(
                    f"part names collide after STEP/GLB-safe normalisation: {name}"
                )
            shape = self._part_shape(cq, part, design)
            shape = self._apply_placement(shape, part)
            nominal = self._apply_placement(self._blank_shape(cq, part), part)
            _validate_solid(shape, f"placed part {part.part_id}")
            _assert_bounds(
                _shape_bounds(shape),
                _shape_bounds(nominal),
                CAD_SOURCE_LINEAR_TOLERANCE_MM,
                f"placed part {part.part_id}",
            )
            expected[name] = _PartGeometry(name, _shape_bounds(shape), float(shape.Volume()))
            placed_shapes[str(part.part_id)] = shape
            assembly.add(shape, name=name)

        collision_report = _assembly_collision_report(design, placed_shapes)
        if not collision_report.passed:
            raise CADAssemblyCollisionError(collision_report)

        with tempfile.TemporaryDirectory(prefix="custombuild-cad-") as temporary:
            directory = Path(temporary)
            step_path = directory / "design.step"
            glb_path = directory / "design.glb"
            try:
                assembly.export(str(step_path), exportType="STEP")
                assembly.export(str(glb_path), exportType="GLTF")
                step = _normalise_step(step_path.read_bytes())
                step_path.write_bytes(step)
                glb = _convert_glb_positions_to_metres(glb_path.read_bytes())
                self._validate_step_roundtrip(cq, step_path, expected)
                _validate_glb_semantics(glb, expected)
            except CADExportError:
                raise
            except Exception as exc:
                raise CADExportError(f"CadQuery export failed atomically: {exc}") from exc
        return CADArtifacts(
            step=step,
            glb=glb,
            kernel="CadQuery/OpenCascade",
            adapter_version=self.version,
            authoritative=True,
        )

    def validate_assembly(self, design: Any) -> AssemblyCollisionReport:
        """Run the exact placed-solid gate without creating export artifacts."""

        if not self.available():
            raise CADDependencyUnavailable(
                "CadQuery/OpenCascade is unavailable; exact assembly validation is blocked"
            )
        import cadquery as cq

        parts = tuple(
            sorted(design.parts, key=lambda item: (str(item.part_id), int(item.instance_index)))
        )
        if not parts:
            raise CADExportError("cannot validate an empty design")
        placed_shapes: dict[str, Any] = {}
        for part in parts:
            part_id = str(part.part_id)
            if part_id in placed_shapes:
                raise CADExportError(f"assembly contains duplicate part ID {part_id}")
            shape = self._apply_placement(self._part_shape(cq, part, design), part)
            _validate_solid(shape, f"placed part {part_id}")
            placed_shapes[part_id] = shape
        report = _assembly_collision_report(design, placed_shapes)
        if not report.passed:
            raise CADAssemblyCollisionError(report)
        return report

    def validate_design_artifacts(self, design: Any, artifacts: CADArtifacts) -> None:
        """Re-open STEP/GLB and bind their geometry to an authoritative design.

        Package checksums only prove that bytes are internally consistent.  This
        validator independently rebuilds the expected placed solids from the
        frozen domain design, imports the supplied STEP through OpenCascade and
        parses the GLB mesh.  A caller therefore cannot replace either CAD file
        and merely re-hash a descriptive attestation.
        """

        if not self.available():
            raise CADDependencyUnavailable(
                "CadQuery/OpenCascade is unavailable; authoritative CAD verification is blocked"
            )
        import cadquery as cq

        design_hash = str(getattr(design, "design_hash", ""))
        cache_key = (
            design_hash,
            hashlib.sha256(artifacts.step).hexdigest(),
            hashlib.sha256(artifacts.glb).hexdigest(),
            self.version,
        )
        if cache_key in _SUCCESSFUL_CAD_VALIDATIONS:
            _SUCCESSFUL_CAD_VALIDATIONS.move_to_end(cache_key)
            return

        parts = tuple(
            sorted(design.parts, key=lambda item: (str(item.part_id), int(item.instance_index)))
        )
        if not parts:
            raise CADExportError("cannot validate CAD for an empty design")

        expected: dict[str, _PartGeometry] = {}
        for part in parts:
            name = _safe_name(str(part.part_id))
            if name in expected:
                raise CADExportError(
                    f"part names collide after STEP/GLB-safe normalisation: {name}"
                )
            shape = self._apply_placement(self._part_shape(cq, part, design), part)
            nominal = self._apply_placement(self._blank_shape(cq, part), part)
            _validate_solid(shape, f"placed part {part.part_id}")
            _assert_bounds(
                _shape_bounds(shape),
                _shape_bounds(nominal),
                CAD_SOURCE_LINEAR_TOLERANCE_MM,
                f"placed part {part.part_id}",
            )
            expected[name] = _PartGeometry(name, _shape_bounds(shape), float(shape.Volume()))

        with tempfile.TemporaryDirectory(prefix="custombuild-cad-verify-") as temporary:
            step_path = Path(temporary) / "design.step"
            step_path.write_bytes(artifacts.step)
            try:
                self._validate_step_roundtrip(cq, step_path, expected)
                _validate_glb_semantics(artifacts.glb, expected)
            except CADExportError:
                raise
            except Exception as exc:
                raise CADExportError(f"authoritative CAD verification failed: {exc}") from exc
        _SUCCESSFUL_CAD_VALIDATIONS[cache_key] = None
        _SUCCESSFUL_CAD_VALIDATIONS.move_to_end(cache_key)
        while len(_SUCCESSFUL_CAD_VALIDATIONS) > _SUCCESSFUL_CAD_VALIDATION_CACHE_SIZE:
            _SUCCESSFUL_CAD_VALIDATIONS.popitem(last=False)

    def _part_shape(self, cq: Any, part: Any, design: Any | None = None) -> Any:
        blank = self._blank_shape(cq, part)
        shape = blank
        compatible_overlaps = _topology_proven_feature_overlaps(design, str(part.part_id))
        features_by_id = {str(feature.feature_id): feature for feature in part.features}
        for feature in sorted(part.features, key=lambda item: str(item.feature_id)):
            compatible_features = tuple(
                features_by_id[other_id]
                for pair in compatible_overlaps
                if str(feature.feature_id) in pair
                for other_id in pair - {str(feature.feature_id)}
                if other_id in features_by_id
            )
            shape = self._apply_feature(cq, shape, part, feature, compatible_features)
        _validate_solid(shape, f"part {part.part_id}")
        _assert_bounds(
            _shape_bounds(shape),
            _shape_bounds(blank),
            CAD_SOURCE_LINEAR_TOLERANCE_MM,
            f"part {part.part_id}",
        )
        return shape

    @staticmethod
    def _blank_shape(cq: Any, part: Any) -> Any:
        size = part.finished_size
        width = _mm(size.width_um)
        depth = _mm(size.depth_um)
        height = _mm(size.height_um)
        if min(width, depth, height) <= 0:
            raise CADExportError(f"part {part.part_id} has invalid dimensions")
        shape = cq.Workplane("XY").box(width, depth, height, centered=(False, False, False)).val()
        _validate_solid(shape, f"blank part {part.part_id}")
        return shape

    @staticmethod
    def _validate_step_roundtrip(
        cq: Any,
        step_path: Path,
        expected: dict[str, _PartGeometry],
    ) -> None:
        imported = cq.Assembly.load(str(step_path), importType="STEP")
        actual: dict[str, Any] = {}
        for name, child in imported.traverse():
            shapes = tuple(child.shapes)
            if not shapes:
                continue
            if len(shapes) != 1 or name in actual:
                raise CADExportError(
                    f"STEP round-trip has ambiguous topology for part {name}"
                )
            actual[name] = shapes[0]
        if set(actual) != set(expected):
            raise CADExportError(
                "STEP round-trip part names/count differ from the authoritative design: "
                f"expected {sorted(expected)}, got {sorted(actual)}"
            )
        for name, source in expected.items():
            shape = actual[name]
            _validate_solid(shape, f"STEP round-trip part {name}")
            _assert_bounds(
                _shape_bounds(shape),
                source.bounds_mm,
                CAD_STEP_LINEAR_TOLERANCE_MM,
                f"STEP round-trip part {name}",
            )
            _assert_volume(
                float(shape.Volume()),
                source.volume_mm3,
                CAD_STEP_VOLUME_RELATIVE_TOLERANCE,
                f"STEP round-trip part {name}",
            )

    def _apply_feature(
        self,
        cq: Any,
        shape: Any,
        part: Any,
        feature: Any,
        compatible_features: tuple[Any, ...] = (),
    ) -> Any:
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
                shape = _remove_material(
                    shape,
                    cutter,
                    part,
                    face,
                    f"feature {feature.feature_id} drill {index + 1}",
                )
            return shape

        if kind == "POCKET" and getattr(dimensions, "diameter_um", None) is not None:
            point = tuple(_mm(value) for value in start)
            cutter = cq.Solid.makeCylinder(
                _mm(int(dimensions.diameter_um)) / 2,
                depth,
                cq.Vector(*point),
                cq.Vector(*direction),
            )
            return _remove_material(
                shape,
                cutter,
                part,
                face,
                f"feature {feature.feature_id}",
            )
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
            result = _remove_material(
                shape,
                cutter,
                part,
                face,
                f"feature {feature.feature_id}",
            )
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
                open_end_reliefs = _open_end_values(feature)
                _validate_open_end_declarations(
                    part,
                    face,
                    start,
                    int(width_um),
                    int(length_um),
                    open_end_reliefs,
                    str(feature.feature_id),
                )
                result = _apply_dogbone_relief(
                    cq,
                    result,
                    part,
                    face,
                    start,
                    int(width_um),
                    int(length_um),
                    depth_um,
                    direction,
                    int(radius_um),
                    str(feature.feature_id),
                    open_end_reliefs,
                    tuple(
                        cutter
                        for compatible in compatible_features
                        if (cutter := _compatible_rectangular_cutter(cq, part, compatible))
                        is not None
                    ),
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


def _assembly_collision_report(
    design: Any,
    placed_shapes: dict[str, Any],
) -> AssemblyCollisionReport:
    """Compare every broad-phase candidate with an exact OpenCascade boolean.

    Face/edge contact has zero volume and is valid.  DADO is the only joint with
    a currently verified domain-to-CAD path; because it is represented by a
    subtractive groove and an uncut mating member, its final solids must also
    have zero overlap.  Consequently there is deliberately no generic
    positive-volume joint exemption: future joint types must provide and test
    an exact expected-overlap solid before one can be introduced.
    """

    part_ids = tuple(sorted(placed_shapes))
    declared, verified = _joint_pair_indexes(design)
    collisions: list[AssemblyCollision] = []
    exact_intersection_count = 0
    checked_pair_count = 0
    for first_index, first_id in enumerate(part_ids):
        first_shape = placed_shapes[first_id]
        first_bounds = _shape_bounds(first_shape)
        for second_id in part_ids[first_index + 1 :]:
            checked_pair_count += 1
            second_shape = placed_shapes[second_id]
            second_bounds = _shape_bounds(second_shape)
            if not _aabbs_have_positive_overlap(first_bounds, second_bounds):
                continue
            exact_intersection_count += 1
            try:
                intersection = first_shape.intersect(second_shape)
                if intersection.isNull():
                    continue
                if not intersection.isValid():
                    raise CADExportError(
                        f"assembly intersection {first_id}/{second_id} is invalid"
                    )
                overlap_volume = float(intersection.Volume())
            except CADExportError:
                raise
            except Exception as exc:
                raise CADExportError(
                    f"assembly intersection {first_id}/{second_id} could not be evaluated: {exc}"
                ) from exc
            if not math.isfinite(overlap_volume):
                raise CADExportError(
                    f"assembly intersection {first_id}/{second_id} has non-finite volume"
                )
            if overlap_volume <= CAD_VOLUME_ABSOLUTE_TOLERANCE_MM3:
                continue

            pair = frozenset((first_id, second_id))
            declared_joint_ids = declared.get(pair, ())
            verified_joint_ids = verified.get(pair, ())
            if verified_joint_ids:
                reason = "VERIFIED_JOINT_GEOMETRY_MISMATCH"
            elif declared_joint_ids:
                reason = "UNVERIFIED_JOINT_OVERLAP"
            else:
                reason = "UNDECLARED_PART_OVERLAP"
            collisions.append(
                AssemblyCollision(
                    part_ids=(first_id, second_id),
                    overlap_volume_mm3=_stable_float(overlap_volume),
                    overlap_aabb_mm=cast(
                        tuple[float, float, float, float, float, float],
                        tuple(_stable_float(value) for value in _shape_bounds(intersection)),
                    ),
                    declared_joint_ids=declared_joint_ids,
                    verified_joint_ids=verified_joint_ids,
                    reason=reason,
                )
            )

    return AssemblyCollisionReport(
        checked_pair_count=checked_pair_count,
        exact_intersection_count=exact_intersection_count,
        collisions=tuple(collisions),
    )


def _joint_pair_indexes(
    design: Any,
) -> tuple[
    dict[frozenset[str], tuple[str, ...]],
    dict[frozenset[str], tuple[str, ...]],
]:
    feature_by_id = {
        str(feature.feature_id): feature
        for part in getattr(design, "parts", ())
        for feature in getattr(part, "features", ())
    }
    declared_lists: dict[frozenset[str], list[str]] = {}
    verified_lists: dict[frozenset[str], list[str]] = {}
    for joint in sorted(getattr(design, "joints", ()), key=lambda item: str(item.joint_id)):
        members = tuple(getattr(joint, "members", ()))
        member_ids = tuple(str(member.part_id) for member in members)
        pair = frozenset(member_ids)
        if len(members) != 2 or len(pair) != 2:
            continue
        joint_id = str(joint.joint_id)
        declared_lists.setdefault(pair, []).append(joint_id)
        if _is_verified_zero_overlap_dado(joint, feature_by_id):
            verified_lists.setdefault(pair, []).append(joint_id)
    return (
        {pair: tuple(sorted(values)) for pair, values in declared_lists.items()},
        {pair: tuple(sorted(values)) for pair, values in verified_lists.items()},
    )


def _is_verified_zero_overlap_dado(joint: Any, feature_by_id: dict[str, Any]) -> bool:
    """Recognise only the versioned subtractive DADO contract, fail closed otherwise."""

    if _value(getattr(joint, "joint_type", "")) != "DADO":
        return False
    members = tuple(getattr(joint, "members", ()))
    if len(members) != 2:
        return False
    cut_member, mate_member = members
    cut_feature_ids = tuple(str(value) for value in getattr(cut_member, "feature_ids", ()))
    if len(cut_feature_ids) != 1 or tuple(getattr(mate_member, "feature_ids", ())):
        return False
    feature = feature_by_id.get(cut_feature_ids[0])
    if feature is None:
        return False
    return (
        str(getattr(feature, "part_id", "")) == str(cut_member.part_id)
        and str(getattr(feature, "joint_id", "")) == str(joint.joint_id)
        and _value(getattr(feature, "kind", "")) == "GROOVE"
        and getattr(feature, "corner_strategy", None) == "dogbone-v1"
        and bool(getattr(feature, "requires_square_corners", False))
        and int(getattr(feature, "fit_clearance_um", -1)) == int(joint.tolerance_um)
        and int(getattr(feature, "tolerance_um", 0)) * 2 < int(joint.tolerance_um)
    )


def _aabbs_have_positive_overlap(
    first: tuple[float, float, float, float, float, float],
    second: tuple[float, float, float, float, float, float],
) -> bool:
    return all(
        min(first[axis + 3], second[axis + 3])
        > max(first[axis], second[axis])
        for axis in range(3)
    )


def _stable_float(value: float) -> float:
    rounded = round(value, 6)
    return 0.0 if rounded == 0 else rounded


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
    if role in {"LEFT_SIDE", "RIGHT_SIDE", "DIVIDER", "VERTICAL_DIVIDER", "BASE_SIDE"}:
        return "LEFT" if face == "A" else "RIGHT"
    if role in {"TOP", "BOTTOM", "SHELF", "BASE_BOTTOM", "BASE_TOP"}:
        return "BOTTOM" if face == "A" else "TOP"
    if role in {"BACK", "BACK_PANEL", "PLINTH", "CABINET_FRONT"}:
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
    part: Any,
    face: str,
    start_um: tuple[int, int, int],
    width_um: int,
    length_um: int,
    depth_um: int,
    direction: tuple[int, int, int],
    radius_um: int,
    feature_id: str,
    open_end_reliefs: frozenset[str],
    compatible_cutters: tuple[Any, ...],
) -> Any:
    """Cut a versioned circular relief at each nominal rectangular corner."""

    if radius_um <= 0:
        raise UnsupportedCADFeatureError("dogbone relief radius must be positive")
    u_axis, v_axis = _face_plane_axes(face)
    result = shape
    for u_offset, v_offset, u_boundary, v_boundary in (
        (0, 0, "u_min", "v_min"),
        (width_um, 0, "u_max", "v_min"),
        (0, length_um, "u_min", "v_max"),
        (width_um, length_um, "u_max", "v_max"),
    ):
        # Where two declared cutter exits meet, the nominal rectangle has already
        # removed every in-stock quadrant. There is no internal corner to relieve.
        if {u_boundary, v_boundary} <= open_end_reliefs:
            continue
        point_um = list(start_um)
        point_um[u_axis] += u_offset
        point_um[v_axis] += v_offset
        cutter = cq.Solid.makeCylinder(
            _mm(radius_um),
            _mm(depth_um),
            cq.Vector(*(_mm(value) for value in point_um)),
            cq.Vector(*direction),
        )
        label = f"dogbone relief {feature_id} at {u_boundary}/{v_boundary}"
        intersection = result.intersect(cutter)
        if (
            not intersection.isNull()
            and intersection.isValid()
            and float(intersection.Volume()) <= CAD_VOLUME_ABSOLUTE_TOLERANCE_MM3
            and _relief_is_covered_by_compatible_cutters(
                cq,
                cutter,
                part,
                _rectangular_cutter(
                    cq,
                    face,
                    start_um,
                    width_um,
                    length_um,
                    depth_um,
                    direction,
                ),
                compatible_cutters,
            )
        ):
            # The cutter's complete in-stock volume was already removed by a
            # topology-proven crossing joint. This is not a general no-op
            # exception: the joint graph and an exact solid coverage check are
            # both required.
            continue
        result = _remove_material(
            result,
            cutter,
            part,
            face,
            label,
            open_end_reliefs,
        )
    return result


def _compatible_rectangular_cutter(cq: Any, part: Any, feature: Any) -> Any | None:
    """Build only a versioned rectangular cutter used by a proven crossing joint."""

    if _value(feature.kind) not in {"GROOVE", "RABBET"}:
        return None
    dimensions = feature.dimensions
    width_um = getattr(dimensions, "width_um", None)
    length_um = getattr(dimensions, "length_um", None)
    if width_um is None or length_um is None:
        return None
    face = _resolve_face(part, _value(feature.face))
    direction, start = _cut_axis_and_start(
        part.finished_size,
        face,
        feature.origin,
        int(dimensions.depth_um),
    )
    depth_um = int(dimensions.depth_um)
    if bool(getattr(feature, "through", False)):
        depth_um += 200
        start = (
            start[0] - direction[0] * 100,
            start[1] - direction[1] * 100,
            start[2] - direction[2] * 100,
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
    _validate_solid(cutter, f"compatible feature {feature.feature_id} cutter")
    return cutter


def _relief_is_covered_by_compatible_cutters(
    cq: Any,
    relief: Any,
    part: Any,
    nominal_cutter: Any,
    compatible_cutters: tuple[Any, ...],
) -> bool:
    """Prove that no in-stock relief volume exists outside compatible joint cuts."""

    if not compatible_cutters:
        return False
    target = CadQueryAdapter._blank_shape(cq, part).intersect(relief)
    if target.isNull() or not target.isValid():
        return False
    target_volume = float(target.Volume())
    if target_volume <= CAD_VOLUME_ABSOLUTE_TOLERANCE_MM3:
        return False
    covered = target.intersect(nominal_cutter)
    for cutter in compatible_cutters:
        covered = covered.fuse(target.intersect(cutter))
    if covered.isNull() or not covered.isValid():
        return False
    return math.isclose(
        float(covered.Volume()),
        target_volume,
        rel_tol=CAD_STEP_VOLUME_RELATIVE_TOLERANCE,
        abs_tol=CAD_VOLUME_ABSOLUTE_TOLERANCE_MM3,
    )


def _topology_proven_feature_overlaps(
    design: Any | None,
    part_id: str,
) -> frozenset[frozenset[str]]:
    """Return feature pairs backed by all three joints of a carcass corner.

    DADO crossings use the existing three-DADO proof.  A surface-mounted back
    has two RABBET edges at each rear carcass corner instead.  Those RABBETs
    participate only when the entire domain result exactly rebuilds from the
    canonical surface-back spec; arbitrary or partial RABBET declarations never
    become compatible cutters.  The caller additionally proves exact in-stock
    relief-volume containment before accepting a covered no-op.
    """

    if design is None:
        return frozenset()
    joints = tuple(getattr(design, "joints", ()))
    canonical_surface_rabbet_ids = _canonical_surface_back_rabbet_joint_ids(design)
    connections: set[frozenset[str]] = set()
    joint_data: list[tuple[frozenset[str], dict[str, tuple[str, ...]]]] = []
    for joint in joints:
        joint_type = _value(getattr(joint, "joint_type", ""))
        if joint_type != "DADO" and str(joint.joint_id) not in canonical_surface_rabbet_ids:
            continue
        by_part = {
            str(member.part_id): tuple(str(value) for value in member.feature_ids)
            for member in getattr(joint, "members", ())
        }
        members = frozenset(by_part)
        if len(members) != 2:
            continue
        connections.add(members)
        joint_data.append((members, by_part))

    owned = [item for item in joint_data if part_id in item[0]]
    proven: set[frozenset[str]] = set()
    for index, (first_members, first_features) in enumerate(owned):
        for second_members, second_features in owned[index + 1 :]:
            first_mate = first_members - {part_id}
            second_mate = second_members - {part_id}
            mate_connection = frozenset((*first_mate, *second_mate))
            if len(mate_connection) != 2 or mate_connection not in connections:
                continue
            for first_id in first_features.get(part_id, ()):
                for second_id in second_features.get(part_id, ()):
                    if first_id != second_id:
                        proven.add(frozenset((first_id, second_id)))
    return frozenset(proven)


def _canonical_surface_back_rabbet_joint_ids(design: Any) -> frozenset[str]:
    """Identify only exact compiler-owned surface-back RABBET applications."""

    try:
        from custombuild_domain import BackPanelType, BookcaseDesignSpec, build_bookcase

        spec = BookcaseDesignSpec.model_validate(getattr(design, "spec", None))
        if spec.parameters.back_panel != BackPanelType.SURFACE_MOUNTED:
            return frozenset()
        canonical = build_bookcase(spec)
    except (ImportError, TypeError, ValueError):
        return frozenset()
    if any(
        getattr(design, field, None) != getattr(canonical, field)
        for field in (
            "design_hash",
            "engine_version",
            "template_version",
            "spec",
            "parts",
            "joints",
            "assembly_graph",
            "total_weight_g",
        )
    ):
        return frozenset()
    back_part_ids = {
        str(part.part_id)
        for part in canonical.parts
        if _value(getattr(part, "role", "")) == "BACK"
    }
    return frozenset(
        str(joint.joint_id)
        for joint in canonical.joints
        if _value(getattr(joint, "joint_type", "")) == "RABBET"
        and any(
            str(getattr(member, "part_id", "")) in back_part_ids
            for member in getattr(joint, "members", ())
        )
    )


def _remove_material(
    shape: Any,
    cutter: Any,
    part: Any,
    face: str,
    label: str,
    open_end_reliefs: frozenset[str] = frozenset(),
) -> Any:
    """Apply one boolean cut only when its envelope and material effect are proven."""

    _validate_solid(shape, f"{label} input")
    _validate_solid(cutter, f"{label} cutter")
    _validate_cutter_envelope(cutter, part, face, label, open_end_reliefs)
    intersection = shape.intersect(cutter)
    if intersection.isNull() or not intersection.isValid():
        raise CADExportError(f"{label} cutter does not produce a valid stock intersection")
    intersection_volume = float(intersection.Volume())
    if intersection_volume <= CAD_VOLUME_ABSOLUTE_TOLERANCE_MM3:
        raise CADExportError(f"{label} cutter does not intersect remaining material")
    before = float(shape.Volume())
    result = shape.cut(cutter)
    _validate_solid(result, f"{label} result")
    removed = before - float(result.Volume())
    if removed <= CAD_VOLUME_ABSOLUTE_TOLERANCE_MM3:
        raise CADExportError(f"{label} removed no measurable material")
    return result


def _validate_cutter_envelope(
    cutter: Any,
    part: Any,
    face: str,
    label: str,
    open_end_reliefs: frozenset[str],
) -> None:
    bounds = _shape_bounds(cutter)
    lower = bounds[:3]
    upper = bounds[3:]
    dimensions = (
        _mm(part.finished_size.width_um),
        _mm(part.finished_size.depth_um),
        _mm(part.finished_size.height_um),
    )
    u_axis, v_axis = _face_plane_axes(face)
    boundary_names = {
        (u_axis, -1): "u_min",
        (u_axis, 1): "u_max",
        (v_axis, -1): "v_min",
        (v_axis, 1): "v_max",
    }
    for axis in (u_axis, v_axis):
        if (
            lower[axis] < -CAD_SOURCE_LINEAR_TOLERANCE_MM
            and boundary_names[(axis, -1)] not in open_end_reliefs
        ):
            raise CADExportError(f"{label} cutter envelope extends beyond the part")
        if (
            upper[axis] > dimensions[axis] + CAD_SOURCE_LINEAR_TOLERANCE_MM
            and boundary_names[(axis, 1)] not in open_end_reliefs
        ):
            raise CADExportError(f"{label} cutter envelope extends beyond the part")
    normal_axis = ({0, 1, 2} - {u_axis, v_axis}).pop()
    if (
        upper[normal_axis] <= -CAD_SOURCE_LINEAR_TOLERANCE_MM
        or lower[normal_axis] >= dimensions[normal_axis] + CAD_SOURCE_LINEAR_TOLERANCE_MM
    ):
        raise CADExportError(f"{label} cutter envelope misses the part")


def _validate_open_end_declarations(
    part: Any,
    face: str,
    start_um: tuple[int, int, int],
    width_um: int,
    length_um: int,
    declared: frozenset[str],
    feature_id: str,
) -> None:
    allowed = {"u_min", "u_max", "v_min", "v_max"}
    if not declared <= allowed:
        raise CADExportError(f"dogbone feature {feature_id} has an unknown open-end declaration")
    u_axis, v_axis = _face_plane_axes(face)
    dimensions = (
        int(part.finished_size.width_um),
        int(part.finished_size.depth_um),
        int(part.finished_size.height_um),
    )
    flush = {
        "u_min": start_um[u_axis] == 0,
        "u_max": start_um[u_axis] + width_um == dimensions[u_axis],
        "v_min": start_um[v_axis] == 0,
        "v_max": start_um[v_axis] + length_um == dimensions[v_axis],
    }
    if any(not flush[boundary] for boundary in declared):
        raise CADExportError(
            f"dogbone feature {feature_id} declares an open end that is not exactly edge-flush"
        )


def _open_end_values(feature: Any) -> frozenset[str]:
    values = tuple(getattr(feature, "open_end_reliefs", ()))
    normalised = tuple(
        str(value.value if isinstance(value, Enum) else value).lower() for value in values
    )
    if len(set(normalised)) != len(normalised):
        raise CADExportError(
            f"dogbone feature {feature.feature_id} repeats an open-end declaration"
        )
    return frozenset(normalised)


def _validate_solid(shape: Any, label: str) -> None:
    try:
        valid = not shape.isNull() and shape.isValid()
        single_solid = len(shape.Solids()) == 1
        shells = len(shape.Shells())
        volume = float(shape.Volume())
    except Exception as exc:
        raise CADExportError(f"{label} topology could not be evaluated: {exc}") from exc
    if not valid:
        raise CADExportError(f"{label} is not a valid OpenCascade shape")
    if not single_solid or shells < 1:
        raise CADExportError(f"{label} is not exactly one connected solid")
    if not math.isfinite(volume) or volume <= CAD_VOLUME_ABSOLUTE_TOLERANCE_MM3:
        raise CADExportError(f"{label} has no positive finite volume")


def _shape_bounds(shape: Any) -> tuple[float, float, float, float, float, float]:
    bounds = shape.BoundingBox()
    return (
        float(bounds.xmin),
        float(bounds.ymin),
        float(bounds.zmin),
        float(bounds.xmax),
        float(bounds.ymax),
        float(bounds.zmax),
    )


def _assert_bounds(
    actual: tuple[float, float, float, float, float, float],
    expected: tuple[float, float, float, float, float, float],
    tolerance: float,
    label: str,
    *,
    unit: str = "mm",
) -> None:
    if not all(math.isfinite(value) for value in (*actual, *expected)):
        raise CADExportError(f"{label} has non-finite bounds")
    if any(abs(left - right) > tolerance for left, right in zip(actual, expected, strict=True)):
        raise CADExportError(
            f"{label} bounds differ from the design by more than {tolerance:g} {unit}: "
            f"expected {expected}, got {actual}"
        )


def _assert_volume(
    actual: float,
    expected: float,
    relative_tolerance: float,
    label: str,
    *,
    absolute_tolerance: float = CAD_VOLUME_ABSOLUTE_TOLERANCE_MM3,
    unit: str = "mm3",
) -> None:
    if not math.isfinite(actual) or actual <= absolute_tolerance:
        raise CADExportError(f"{label} has no positive finite volume")
    if not math.isclose(
        actual,
        expected,
        rel_tol=relative_tolerance,
        abs_tol=absolute_tolerance,
    ):
        raise CADExportError(
            f"{label} volume differs from the authoritative solid: "
            f"expected {expected:g} {unit}, got {actual:g} {unit}"
        )


def _validate_glb_semantics(payload: bytes, expected: dict[str, _PartGeometry]) -> None:
    document, binary = _decode_glb(payload)
    if document.get("asset", {}).get("version") != "2.0":
        raise CADExportError("GLB does not declare glTF 2.0")
    buffers = _document_list(document, "buffers")
    if len(buffers) != 1 or not isinstance(buffers[0], dict) or "uri" in buffers[0]:
        raise CADExportError("GLB must contain one embedded binary buffer")
    byte_length = buffers[0].get("byteLength")
    if (
        type(byte_length) is not int
        or byte_length <= 0
        or not byte_length <= len(binary) <= byte_length + 3
    ):
        raise CADExportError("GLB binary buffer length is inconsistent")

    meshes = _document_list(document, "meshes")
    mesh_by_name: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, raw_mesh in enumerate(meshes):
        if not isinstance(raw_mesh, dict):
            raise CADExportError("GLB mesh entry is not an object")
        name = raw_mesh.get("name")
        if not isinstance(name, str) or not name or name in mesh_by_name:
            raise CADExportError("GLB mesh names are missing or duplicated")
        mesh_by_name[name] = (index, cast(dict[str, Any], raw_mesh))
    if set(mesh_by_name) != set(expected):
        raise CADExportError(
            "GLB part names/count differ from the authoritative design: "
            f"expected {sorted(expected)}, got {sorted(mesh_by_name)}"
        )

    referenced: dict[int, int] = {}
    for raw_node in _document_list(document, "nodes"):
        if not isinstance(raw_node, dict) or "mesh" not in raw_node:
            continue
        mesh_index = raw_node.get("mesh")
        if type(mesh_index) is not int or not 0 <= mesh_index < len(meshes):
            raise CADExportError("GLB node references an invalid mesh")
        if any(key in raw_node for key in ("matrix", "translation", "rotation", "scale")):
            raise CADExportError("GLB part node contains an unverified local transform")
        mesh_name = meshes[mesh_index].get("name")
        if raw_node.get("name") != mesh_name:
            raise CADExportError("GLB part node and mesh names differ")
        referenced[mesh_index] = referenced.get(mesh_index, 0) + 1

    for name, source in expected.items():
        mesh_index, mesh = mesh_by_name[name]
        if referenced.get(mesh_index) != 1:
            raise CADExportError(f"GLB part {name} is not referenced by exactly one node")
        bounds, volume = _mesh_geometry(document, binary, mesh, name)
        expected_bounds_m = tuple(value * MM_TO_M for value in source.bounds_mm)
        _assert_bounds(
            bounds,
            cast(tuple[float, float, float, float, float, float], expected_bounds_m),
            CAD_GLB_LINEAR_TOLERANCE_M,
            f"GLB part {name}",
            unit="m",
        )
        _assert_volume(
            volume,
            source.volume_mm3 * MM3_TO_M3,
            CAD_GLB_VOLUME_RELATIVE_TOLERANCE,
            f"GLB part {name}",
            absolute_tolerance=CAD_VOLUME_ABSOLUTE_TOLERANCE_M3,
            unit="m3",
        )


def _convert_glb_positions_to_metres(payload: bytes) -> bytes:
    """Convert CadQuery's millimetre vertex coordinates to glTF metres.

    glTF defines one linear unit as one metre. CadQuery/OpenCascade models are
    authored in millimetres, so passing the exporter bytes through unchanged
    makes every independent glTF consumer render the assembly at 1000x scale.
    The conversion is applied directly to every POSITION accessor. Accessor
    minima/maxima are rebuilt from the float32 values actually written.
    """

    document, raw_binary = _decode_glb(payload)
    meshes = _document_list(document, "meshes")
    position_accessors: set[int] = set()
    for mesh in meshes:
        if not isinstance(mesh, dict):
            raise CADExportError("GLB mesh entry is not an object")
        primitives = mesh.get("primitives")
        if not isinstance(primitives, list) or not primitives:
            raise CADExportError("GLB mesh has no primitives to convert")
        for primitive in primitives:
            if not isinstance(primitive, dict):
                raise CADExportError("GLB mesh contains an invalid primitive")
            attributes = primitive.get("attributes")
            if not isinstance(attributes, dict) or type(attributes.get("POSITION")) is not int:
                raise CADExportError("GLB primitive has no POSITION accessor to convert")
            position_accessors.add(attributes["POSITION"])
    if not position_accessors:
        raise CADExportError("GLB has no POSITION accessors to convert")

    binary = bytearray(raw_binary)
    accessors = _document_list(document, "accessors")
    for accessor_index in sorted(position_accessors):
        values = _read_accessor(document, raw_binary, accessor_index, "VEC3", {5126})
        offsets, _ = _accessor_layout(
            document,
            len(raw_binary),
            accessor_index,
            "VEC3",
            {5126},
        )
        scaled_values: list[tuple[float, float, float]] = []
        for offset, value in zip(offsets, values, strict=True):
            scaled = tuple(float(component) * MM_TO_M for component in value)
            struct.pack_into("<3f", binary, offset, *scaled)
            scaled_values.append(
                cast(
                    tuple[float, float, float],
                    struct.unpack_from("<3f", binary, offset),
                )
            )
        if not 0 <= accessor_index < len(accessors) or not isinstance(
            accessors[accessor_index], dict
        ):
            raise CADExportError("GLB POSITION accessor reference is invalid")
        accessor = accessors[accessor_index]
        accessor["min"] = [min(point[axis] for point in scaled_values) for axis in range(3)]
        accessor["max"] = [max(point[axis] for point in scaled_values) for axis in range(3)]

    return _encode_glb(document, bytes(binary))


def _encode_glb(document: dict[str, Any], binary: bytes) -> bytes:
    json_chunk = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    json_chunk += b" " * (-len(json_chunk) % 4)
    binary_chunk = binary + b"\x00" * (-len(binary) % 4)
    body = (
        struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(binary_chunk), 0x004E4942)
        + binary_chunk
    )
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


def _decode_glb(payload: bytes) -> tuple[dict[str, Any], bytes]:
    if len(payload) < 20:
        raise CADExportError("GLB is truncated")
    magic, version, declared_length = struct.unpack_from("<4sII", payload, 0)
    if magic != b"glTF" or version != 2 or declared_length != len(payload):
        raise CADExportError("GLB header or declared length is invalid")
    chunks: list[tuple[int, bytes]] = []
    offset = 12
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise CADExportError("GLB chunk header is truncated")
        chunk_length, chunk_type = struct.unpack_from("<II", payload, offset)
        offset += 8
        end = offset + chunk_length
        if end > len(payload):
            raise CADExportError("GLB chunk exceeds the declared file length")
        chunks.append((chunk_type, payload[offset:end]))
        offset = end
    if len(chunks) != 2 or chunks[0][0] != 0x4E4F534A or chunks[1][0] != 0x004E4942:
        raise CADExportError("GLB must contain one JSON chunk followed by one BIN chunk")
    try:
        decoded = json.loads(chunks[0][1].rstrip(b" \x00").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CADExportError("GLB JSON chunk is invalid") from exc
    if not isinstance(decoded, dict):
        raise CADExportError("GLB JSON root is not an object")
    return cast(dict[str, Any], decoded), chunks[1][1]


def _document_list(document: dict[str, Any], key: str) -> list[Any]:
    value = document.get(key)
    if not isinstance(value, list):
        raise CADExportError(f"GLB {key} collection is missing")
    return value


def _mesh_geometry(
    document: dict[str, Any],
    binary: bytes,
    mesh: dict[str, Any],
    name: str,
) -> tuple[tuple[float, float, float, float, float, float], float]:
    primitives = mesh.get("primitives")
    if not isinstance(primitives, list) or not primitives:
        raise CADExportError(f"GLB part {name} has no triangle primitives")
    minima = [math.inf, math.inf, math.inf]
    maxima = [-math.inf, -math.inf, -math.inf]
    signed_volume = 0.0
    triangle_count = 0
    for primitive in primitives:
        if not isinstance(primitive, dict) or primitive.get("mode", 4) != 4:
            raise CADExportError(f"GLB part {name} contains a non-triangle primitive")
        attributes = primitive.get("attributes")
        if not isinstance(attributes, dict) or type(attributes.get("POSITION")) is not int:
            raise CADExportError(f"GLB part {name} has no POSITION accessor")
        positions = _read_accessor(document, binary, attributes["POSITION"], "VEC3", {5126})
        index_accessor = primitive.get("indices")
        if type(index_accessor) is not int:
            raise CADExportError(f"GLB part {name} has no index accessor")
        raw_indices = _read_accessor(
            document, binary, index_accessor, "SCALAR", {5121, 5123, 5125}
        )
        indices = [int(value[0]) for value in raw_indices]
        if not indices or len(indices) % 3:
            raise CADExportError(f"GLB part {name} has an invalid triangle index count")
        points = [tuple(float(value) for value in point) for point in positions]
        _validate_position_accessor_bounds(document, attributes["POSITION"], points, name)
        if any(index < 0 or index >= len(points) for index in indices):
            raise CADExportError(f"GLB part {name} has an out-of-range triangle index")
        for point in points:
            for axis in range(3):
                minima[axis] = min(minima[axis], point[axis])
                maxima[axis] = max(maxima[axis], point[axis])
        for offset in range(0, len(indices), 3):
            first, second, third = (points[indices[offset + index]] for index in range(3))
            signed_volume += _signed_tetrahedron_volume(first, second, third)
            triangle_count += 1
    if triangle_count == 0:
        raise CADExportError(f"GLB part {name} contains no triangles")
    return (
        (minima[0], minima[1], minima[2], maxima[0], maxima[1], maxima[2]),
        abs(signed_volume),
    )


def _read_accessor(
    document: dict[str, Any],
    binary: bytes,
    accessor_index: int,
    expected_type: str,
    allowed_components: set[int],
) -> tuple[tuple[float | int, ...], ...]:
    offsets, unpack_format = _accessor_layout(
        document,
        len(binary),
        accessor_index,
        expected_type,
        allowed_components,
    )
    values: list[tuple[float | int, ...]] = []
    for offset in offsets:
        value = struct.unpack_from(unpack_format, binary, offset)
        if any(isinstance(item, float) and not math.isfinite(item) for item in value):
            raise CADExportError("GLB accessor contains a non-finite value")
        values.append(cast(tuple[float | int, ...], value))
    return tuple(values)


def _accessor_layout(
    document: dict[str, Any],
    binary_length: int,
    accessor_index: int,
    expected_type: str,
    allowed_components: set[int],
) -> tuple[tuple[int, ...], str]:
    accessors = _document_list(document, "accessors")
    views = _document_list(document, "bufferViews")
    if not 0 <= accessor_index < len(accessors) or not isinstance(accessors[accessor_index], dict):
        raise CADExportError("GLB accessor reference is invalid")
    accessor = accessors[accessor_index]
    if accessor.get("type") != expected_type or "sparse" in accessor:
        raise CADExportError("GLB accessor type is unsupported")
    component_type = accessor.get("componentType")
    formats = {5121: ("B", 1), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
    if type(component_type) is not int or component_type not in allowed_components:
        raise CADExportError("GLB accessor component type is unsupported")
    view_index = accessor.get("bufferView")
    count = accessor.get("count")
    if type(view_index) is not int or type(count) is not int or count <= 0:
        raise CADExportError("GLB accessor range is invalid")
    if not 0 <= view_index < len(views) or not isinstance(views[view_index], dict):
        raise CADExportError("GLB buffer-view reference is invalid")
    view = views[view_index]
    if view.get("buffer", 0) != 0:
        raise CADExportError("GLB accessor references an external buffer")
    component_format, component_size = formats[component_type]
    width = 3 if expected_type == "VEC3" else 1
    packed_size = component_size * width
    stride = view.get("byteStride", packed_size)
    view_offset = view.get("byteOffset", 0)
    accessor_offset = accessor.get("byteOffset", 0)
    view_length = view.get("byteLength")
    if (
        type(stride) is not int
        or stride < packed_size
        or type(view_offset) is not int
        or type(accessor_offset) is not int
        or type(view_length) is not int
        or min(view_offset, accessor_offset, view_length) < 0
    ):
        raise CADExportError("GLB accessor byte layout is invalid")
    start = view_offset + accessor_offset
    final_end = start + (count - 1) * stride + packed_size
    if final_end > view_offset + view_length or final_end > binary_length:
        raise CADExportError("GLB accessor exceeds its binary buffer view")
    unpack_format = f"<{width}{component_format}"
    return tuple(start + index * stride for index in range(count)), unpack_format


def _validate_position_accessor_bounds(
    document: dict[str, Any],
    accessor_index: int,
    points: list[tuple[float, ...]],
    mesh_name: str,
) -> None:
    accessors = _document_list(document, "accessors")
    if not 0 <= accessor_index < len(accessors) or not isinstance(accessors[accessor_index], dict):
        raise CADExportError("GLB POSITION accessor reference is invalid")
    accessor = accessors[accessor_index]
    declared_min = accessor.get("min")
    declared_max = accessor.get("max")
    if (
        not isinstance(declared_min, list)
        or not isinstance(declared_max, list)
        or len(declared_min) != 3
        or len(declared_max) != 3
        or not all(type(value) in {int, float} for value in (*declared_min, *declared_max))
    ):
        raise CADExportError(f"GLB part {mesh_name} POSITION accessor has no valid bounds")
    actual_min = tuple(min(point[axis] for point in points) for axis in range(3))
    actual_max = tuple(max(point[axis] for point in points) for axis in range(3))
    declared = tuple(float(value) for value in (*declared_min, *declared_max))
    actual = (*actual_min, *actual_max)
    if not all(math.isfinite(value) for value in declared) or any(
        not math.isclose(left, right, rel_tol=0.0, abs_tol=1e-7)
        for left, right in zip(declared, actual, strict=True)
    ):
        raise CADExportError(
            f"GLB part {mesh_name} POSITION accessor bounds differ from its vertex data"
        )


def _signed_tetrahedron_volume(
    first: tuple[float, ...],
    second: tuple[float, ...],
    third: tuple[float, ...],
) -> float:
    cross_x = second[1] * third[2] - second[2] * third[1]
    cross_y = second[2] * third[0] - second[0] * third[2]
    cross_z = second[0] * third[1] - second[1] * third[0]
    return (first[0] * cross_x + first[1] * cross_y + first[2] * cross_z) / 6.0


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
