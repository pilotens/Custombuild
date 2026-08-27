from __future__ import annotations

import json
import math
import struct
from types import SimpleNamespace

import pytest
from custombuild_cad import (
    CADArtifacts,
    CADDependencyUnavailable,
    CADExportError,
    CadQueryAdapter,
    cad_capability_status,
)
from custombuild_cad.adapter import (
    _convert_glb_positions_to_metres,
    _decode_glb,
    _document_list,
    _mesh_geometry,
    _PartGeometry,
    _read_accessor,
    _validate_glb_semantics,
)


def _glb(*chunks: tuple[int, bytes]) -> bytes:
    body = b"".join(
        struct.pack("<II", len(payload), chunk_type) + payload
        for chunk_type, payload in chunks
    )
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


def _accessor_document(
    *,
    accessor: dict[str, object] | None = None,
    view: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "accessors": [
            accessor
            or {
                "type": "VEC3",
                "componentType": 5126,
                "bufferView": 0,
                "count": 1,
            }
        ],
        "bufferViews": [view or {"buffer": 0, "byteLength": 12}],
    }


def _triangle_fixture(indices: bytes) -> tuple[dict[str, object], bytes, dict[str, object]]:
    positions = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    document = {
        "accessors": [
            {
                "type": "VEC3",
                "componentType": 5126,
                "bufferView": 0,
                "count": 3,
                "min": [0.0, 0.0, 0.0],
                "max": [1.0, 1.0, 0.0],
            },
            {
                "type": "SCALAR",
                "componentType": 5121,
                "bufferView": 1,
                "count": len(indices),
            },
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(positions)},
            {
                "buffer": 0,
                "byteOffset": len(positions),
                "byteLength": len(indices),
            },
        ],
    }
    mesh = {"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}
    return document, positions + indices, mesh


def _millimetre_triangle_glb() -> bytes:
    positions = struct.pack("<9f", 0, 0, 0, 300, 0, 0, 0, 200, 0)
    indices = bytes((0, 1, 2))
    binary = positions + indices
    document = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(positions)},
            {
                "buffer": 0,
                "byteOffset": len(positions),
                "byteLength": len(indices),
            },
        ],
        "accessors": [
            {
                "type": "VEC3",
                "componentType": 5126,
                "bufferView": 0,
                "count": 3,
                "min": [0, 0, 0],
                "max": [300, 200, 0],
            },
            {
                "type": "SCALAR",
                "componentType": 5121,
                "bufferView": 1,
                "count": 3,
            },
        ],
        "meshes": [
            {
                "name": "panel",
                "primitives": [{"attributes": {"POSITION": 0}, "indices": 1}],
            }
        ],
        "nodes": [{"name": "panel", "mesh": 0}],
    }
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * (-len(json_chunk) % 4)
    binary += b"\x00" * (-len(binary) % 4)
    return _glb((0x4E4F534A, json_chunk), (0x004E4942, binary))


def _independent_glb_document(payload: bytes) -> tuple[dict[str, object], bytes]:
    """Minimal test consumer that intentionally shares no CAD adapter parser code."""

    magic, version, total_length = struct.unpack_from("<4sII", payload)
    assert magic == b"glTF"
    assert version == 2
    assert total_length == len(payload)
    json_length, json_type = struct.unpack_from("<II", payload, 12)
    assert json_type == 0x4E4F534A
    json_start = 20
    json_end = json_start + json_length
    document = json.loads(payload[json_start:json_end].rstrip(b" \x00"))
    binary_length, binary_type = struct.unpack_from("<II", payload, json_end)
    assert binary_type == 0x004E4942
    binary_start = json_end + 8
    binary = payload[binary_start : binary_start + binary_length]
    assert isinstance(document, dict)
    return document, binary


def _independent_accessor(
    document: dict[str, object],
    binary: bytes,
    accessor_index: int,
) -> tuple[tuple[float | int, ...], ...]:
    accessors = document["accessors"]
    views = document["bufferViews"]
    assert isinstance(accessors, list)
    assert isinstance(views, list)
    accessor = accessors[accessor_index]
    assert isinstance(accessor, dict)
    view = views[accessor["bufferView"]]
    assert isinstance(view, dict)
    formats = {5121: ("B", 1), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
    widths = {"SCALAR": 1, "VEC3": 3}
    component_format, component_size = formats[accessor["componentType"]]
    width = widths[accessor["type"]]
    stride = view.get("byteStride", width * component_size)
    start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    return tuple(
        struct.unpack_from(f"<{width}{component_format}", binary, start + index * stride)
        for index in range(accessor["count"])
    )


def _independent_mesh_geometry(
    payload: bytes,
    mesh_name: str,
) -> tuple[tuple[float, float, float, float, float, float], float]:
    document, binary = _independent_glb_document(payload)
    meshes = document["meshes"]
    assert isinstance(meshes, list)
    mesh = next(item for item in meshes if isinstance(item, dict) and item.get("name") == mesh_name)
    minima = [math.inf, math.inf, math.inf]
    maxima = [-math.inf, -math.inf, -math.inf]
    volume = 0.0
    primitives = mesh["primitives"]
    assert isinstance(primitives, list)
    for primitive in primitives:
        assert isinstance(primitive, dict)
        attributes = primitive["attributes"]
        assert isinstance(attributes, dict)
        positions = _independent_accessor(document, binary, attributes["POSITION"])
        indices = _independent_accessor(document, binary, primitive["indices"])
        points = [tuple(float(value) for value in point) for point in positions]
        for point in points:
            for axis in range(3):
                minima[axis] = min(minima[axis], point[axis])
                maxima[axis] = max(maxima[axis], point[axis])
        flat_indices = [int(value[0]) for value in indices]
        for offset in range(0, len(flat_indices), 3):
            first, second, third = (
                points[flat_indices[offset + local_index]] for local_index in range(3)
            )
            cross_x = second[1] * third[2] - second[2] * third[1]
            cross_y = second[2] * third[0] - second[0] * third[2]
            cross_z = second[0] * third[1] - second[1] * third[0]
            volume += (
                first[0] * cross_x + first[1] * cross_y + first[2] * cross_z
            ) / 6.0
    return (
        (minima[0], minima[1], minima[2], maxima[0], maxima[1], maxima[2]),
        abs(volume),
    )


def test_cad_unavailability_is_explicit_and_never_creates_placeholder_files() -> None:
    status = cad_capability_status()
    if CadQueryAdapter.available():
        assert status["status"] == "AVAILABLE"
        pytest.skip("CadQuery is installed; real export is covered by the CAD marker suite")

    with pytest.raises(CADDependencyUnavailable, match="generation is blocked"):
        CadQueryAdapter().export_design(object())
    assert status["status"] == "BLOCKED_UNAVAILABLE"


def test_artifact_type_rejects_fake_step_and_glb_payloads() -> None:
    with pytest.raises(CADExportError, match="genuine STEP"):
        CADArtifacts(b"placeholder", b"glTF", "test", "test")

    with pytest.raises(CADExportError, match="binary glTF"):
        CADArtifacts(b"ISO-10303-21;", b"placeholder", "test", "test")


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (b"", "truncated"),
        (struct.pack("<4sII", b"BAD!", 2, 20) + b"\0" * 8, "header"),
        (
            struct.pack("<4sII", b"glTF", 2, 24)
            + struct.pack("<II", 0, 0x4E4F534A)
            + b"tail",
            "chunk header",
        ),
        (
            struct.pack("<4sII", b"glTF", 2, 20)
            + struct.pack("<II", 16, 0x4E4F534A),
            "chunk exceeds",
        ),
        (_glb((0x4E4F534A, b"{}  ")), "one JSON chunk"),
        (_glb((0x4E4F534A, b"nope"), (0x004E4942, b"\0" * 4)), "JSON chunk"),
        (_glb((0x4E4F534A, b"[]  "), (0x004E4942, b"\0" * 4)), "root"),
    ),
)
def test_glb_container_parser_rejects_malformed_structure(payload: bytes, message: str) -> None:
    with pytest.raises(CADExportError, match=message):
        _decode_glb(payload)


def test_glb_accessor_parser_rejects_untrusted_layouts_and_values() -> None:
    binary = struct.pack("<3f", 1, 2, 3)
    assert _read_accessor(_accessor_document(), binary, 0, "VEC3", {5126}) == (
        (1.0, 2.0, 3.0),
    )
    with pytest.raises(CADExportError, match="collection is missing"):
        _document_list({}, "meshes")
    with pytest.raises(CADExportError, match="accessor reference"):
        _read_accessor(_accessor_document(), binary, 1, "VEC3", {5126})
    with pytest.raises(CADExportError, match="accessor type"):
        _read_accessor(
            _accessor_document(
                accessor={
                    "type": "SCALAR",
                    "componentType": 5126,
                    "bufferView": 0,
                    "count": 1,
                }
            ),
            binary,
            0,
            "VEC3",
            {5126},
        )
    with pytest.raises(CADExportError, match="component type"):
        _read_accessor(
            _accessor_document(
                accessor={
                    "type": "VEC3",
                    "componentType": 5123,
                    "bufferView": 0,
                    "count": 1,
                }
            ),
            binary,
            0,
            "VEC3",
            {5126},
        )
    with pytest.raises(CADExportError, match="accessor range"):
        _read_accessor(
            _accessor_document(
                accessor={
                    "type": "VEC3",
                    "componentType": 5126,
                    "bufferView": 0,
                    "count": 0,
                }
            ),
            binary,
            0,
            "VEC3",
            {5126},
        )
    with pytest.raises(CADExportError, match="buffer-view reference"):
        _read_accessor(
            _accessor_document(
                accessor={
                    "type": "VEC3",
                    "componentType": 5126,
                    "bufferView": 1,
                    "count": 1,
                }
            ),
            binary,
            0,
            "VEC3",
            {5126},
        )
    with pytest.raises(CADExportError, match="external buffer"):
        _read_accessor(
            _accessor_document(view={"buffer": 1, "byteLength": 12}),
            binary,
            0,
            "VEC3",
            {5126},
        )
    with pytest.raises(CADExportError, match="byte layout"):
        _read_accessor(
            _accessor_document(view={"buffer": 0, "byteLength": 12, "byteStride": 1}),
            binary,
            0,
            "VEC3",
            {5126},
        )
    with pytest.raises(CADExportError, match="exceeds its binary buffer view"):
        _read_accessor(
            _accessor_document(view={"buffer": 0, "byteLength": 4}),
            binary,
            0,
            "VEC3",
            {5126},
        )
    with pytest.raises(CADExportError, match="non-finite"):
        _read_accessor(
            _accessor_document(),
            struct.pack("<3f", math.nan, 0, 0),
            0,
            "VEC3",
            {5126},
        )


def test_glb_mesh_parser_rejects_invalid_triangle_topology() -> None:
    with pytest.raises(CADExportError, match="no triangle primitives"):
        _mesh_geometry({}, b"", {}, "panel")
    with pytest.raises(CADExportError, match="non-triangle primitive"):
        _mesh_geometry({}, b"", {"primitives": [None]}, "panel")
    with pytest.raises(CADExportError, match="no POSITION accessor"):
        _mesh_geometry({}, b"", {"primitives": [{"attributes": {}}]}, "panel")

    document, binary, mesh = _triangle_fixture(bytes((0, 1, 2)))
    with pytest.raises(CADExportError, match="no index accessor"):
        _mesh_geometry(
            document,
            binary,
            {"primitives": [{"attributes": {"POSITION": 0}}]},
            "panel",
        )
    invalid_count = _triangle_fixture(bytes((0, 1)))
    with pytest.raises(CADExportError, match="triangle index count"):
        _mesh_geometry(*invalid_count, "panel")
    out_of_range = _triangle_fixture(bytes((0, 1, 3)))
    with pytest.raises(CADExportError, match="out-of-range"):
        _mesh_geometry(*out_of_range, "panel")
    bounds, volume = _mesh_geometry(document, binary, mesh, "panel")
    assert bounds == (0.0, 0.0, 0.0, 1.0, 1.0, 0.0)
    assert volume == 0.0

    accessors = document["accessors"]
    assert isinstance(accessors, list)
    position_accessor = accessors[0]
    assert isinstance(position_accessor, dict)
    position_accessor["max"] = [1.0, 2.0, 0.0]
    with pytest.raises(CADExportError, match="accessor bounds differ"):
        _mesh_geometry(document, binary, mesh, "panel")


def test_glb_conversion_scales_positions_and_declared_bounds_to_metres() -> None:
    converted = _convert_glb_positions_to_metres(_millimetre_triangle_glb())
    document, binary = _independent_glb_document(converted)
    positions = _independent_accessor(document, binary, 0)
    accessors = document["accessors"]
    assert isinstance(accessors, list)
    position_accessor = accessors[0]
    assert isinstance(position_accessor, dict)

    assert tuple(value for point in positions for value in point) == pytest.approx(
        (0.0, 0.0, 0.0, 0.300, 0.0, 0.0, 0.0, 0.200, 0.0)
    )
    assert position_accessor["min"] == pytest.approx([0.0, 0.0, 0.0])
    assert position_accessor["max"] == pytest.approx([0.300, 0.200, 0.0])


@pytest.mark.cad
def test_real_cadquery_export_produces_step_and_binary_glb() -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
    import cadquery as cq

    size = SimpleNamespace(width_um=300_000, depth_um=200_000, height_um=18_000)
    placement = SimpleNamespace(
        x_um=0,
        y_um=0,
        z_um=0,
        rotation_x_mdeg=0,
        rotation_y_mdeg=0,
        rotation_z_mdeg=0,
    )
    part = SimpleNamespace(
        part_id="cad-panel",
        instance_index=0,
        finished_size=size,
        placement=placement,
        features=(),
    )
    design = SimpleNamespace(design_hash="e" * 64, parts=(part,))

    first = CadQueryAdapter().export_design(design)
    second = CadQueryAdapter().export_design(design)

    assert first.step.startswith(b"ISO-10303-21")
    assert first.glb.startswith(b"glTF")
    assert len(first.step) > 1_000
    assert len(first.glb) > 1_000
    assert first.step == second.step
    assert first.glb == second.glb

    independent_bounds, independent_volume = _independent_mesh_geometry(
        first.glb,
        "cad-panel",
    )
    assert independent_bounds == pytest.approx(
        (0.0, 0.0, 0.0, 0.300, 0.200, 0.018),
        abs=0.00005,
    )
    assert independent_volume == pytest.approx(300 * 200 * 18 / 1_000_000_000, rel=0.01)

    independent_document, independent_binary = _independent_glb_document(first.glb)
    independent_meshes = independent_document["meshes"]
    independent_accessors = independent_document["accessors"]
    assert isinstance(independent_meshes, list)
    assert isinstance(independent_accessors, list)
    independent_mesh = next(
        item
        for item in independent_meshes
        if isinstance(item, dict) and item.get("name") == "cad-panel"
    )
    independent_primitives = independent_mesh["primitives"]
    assert isinstance(independent_primitives, list)
    for primitive in independent_primitives:
        assert isinstance(primitive, dict)
        attributes = primitive["attributes"]
        assert isinstance(attributes, dict)
        accessor = independent_accessors[attributes["POSITION"]]
        assert isinstance(accessor, dict)
        accessor_positions = _independent_accessor(
            independent_document,
            independent_binary,
            attributes["POSITION"],
        )
        expected_min = [min(point[axis] for point in accessor_positions) for axis in range(3)]
        expected_max = [max(point[axis] for point in accessor_positions) for axis in range(3)]
        assert accessor["min"] == pytest.approx(expected_min, abs=0.0000001)
        assert accessor["max"] == pytest.approx(expected_max, abs=0.0000001)

    source = CadQueryAdapter()._part_shape(cq, part, design)
    bounds = source.BoundingBox()
    expected = {
        "cad-panel": _PartGeometry(
            "cad-panel",
            (bounds.xmin, bounds.ymin, bounds.zmin, bounds.xmax, bounds.ymax, bounds.zmax),
            source.Volume(),
        )
    }
    tampered = first.glb.replace(b"cad-panel", b"wrongpart")
    assert tampered != first.glb
    with pytest.raises(CADExportError, match="GLB part names/count"):
        _validate_glb_semantics(tampered, expected)


@pytest.mark.cad
def test_real_cadquery_export_rejects_a_step_roundtrip_missing_parts(monkeypatch) -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
    import cadquery as cq

    size = SimpleNamespace(width_um=300_000, depth_um=200_000, height_um=18_000)
    part = SimpleNamespace(
        part_id="cad-panel",
        instance_index=0,
        finished_size=size,
        placement=SimpleNamespace(
            x_um=0,
            y_um=0,
            z_um=0,
            rotation_x_mdeg=0,
            rotation_y_mdeg=0,
            rotation_z_mdeg=0,
        ),
        features=(),
    )
    monkeypatch.setattr(cq.Assembly, "load", lambda *args, **kwargs: cq.Assembly(name="empty"))

    with pytest.raises(CADExportError, match="STEP round-trip part names/count"):
        CadQueryAdapter().export_design(
            SimpleNamespace(design_hash="f" * 64, parts=(part,))
        )


@pytest.mark.cad
def test_real_cadquery_export_rejects_step_roundtrip_geometry_drift(monkeypatch) -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
    import cadquery as cq

    size = SimpleNamespace(width_um=300_000, depth_um=200_000, height_um=18_000)
    part = SimpleNamespace(
        part_id="cad-panel",
        instance_index=0,
        finished_size=size,
        placement=SimpleNamespace(
            x_um=0,
            y_um=0,
            z_um=0,
            rotation_x_mdeg=0,
            rotation_y_mdeg=0,
            rotation_z_mdeg=0,
        ),
        features=(),
    )
    design = SimpleNamespace(design_hash="a" * 64, parts=(part,))

    wrong_bounds = cq.Assembly(name="roundtrip")
    wrong_bounds.add(
        cq.Workplane("XY").box(301, 200, 18, centered=(False, False, False)).val(),
        name="cad-panel",
    )
    monkeypatch.setattr(cq.Assembly, "load", lambda *args, **kwargs: wrong_bounds)
    with pytest.raises(CADExportError, match="bounds differ"):
        CadQueryAdapter().export_design(design)

    wrong_volume = cq.Assembly(name="roundtrip")
    hollowed = (
        cq.Workplane("XY")
        .box(300, 200, 18, centered=(False, False, False))
        .cut(
            cq.Workplane("XY")
            .box(10, 10, 10, centered=(False, False, False))
            .translate((100, 100, 0))
        )
        .val()
    )
    wrong_volume.add(hollowed, name="cad-panel")
    monkeypatch.setattr(cq.Assembly, "load", lambda *args, **kwargs: wrong_volume)
    with pytest.raises(CADExportError, match="volume differs"):
        CadQueryAdapter().export_design(design)
