"""Manifest, checksums and byte-reproducible production ZIP packages."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .errors import ArtifactError, ProductionBlockedError
from .exporters import (
    bom_csv,
    cut_list_csv,
    dxf_for_part,
    material_list_csv,
    nesting_svg,
    setup_sheet_svg,
    svg_for_part,
    tool_list_csv,
)
from .model import (
    NestingLayout,
    OperationsDocument,
    PartSpec,
    Side,
    canonical_json_bytes,
    sha256_hex,
)

MAX_PACKAGE_FILES = 10_000
MAX_ARTIFACT_SIZE_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1_000
PACKAGE_BUILDER_VERSION = "deterministic-package-1.0.0"
PRODUCTION_MANIFEST_SCHEMA_VERSION = "custombuild.production-manifest.v1"
ARTIFACT_SCHEMA_VERSION = "custombuild.production-artifacts.v1"
MANIFEST_CONTEXT_HASH_FIELDS = (
    "project_id",
    "revision",
    "design_hash",
    "app_version",
    "engine_version",
    "template_version",
    "rule_version",
    "material_versions",
    "joint_version",
    "machine_profile",
    "postprocessor_version",
    "generation_context_hash",
    "production_engine_context",
    "artifact_schema_version",
    "cad_status",
    "approved_assumptions",
    "warnings",
    "overrides",
    "artifacts",
)


@dataclass(frozen=True, slots=True)
class ArtifactFile:
    path: str
    data: bytes
    media_type: str
    role: str

    def __post_init__(self) -> None:
        _validate_artifact_path(self.path)
        if not isinstance(self.data, bytes):
            raise TypeError("artifact data must be bytes")


@dataclass(frozen=True, slots=True)
class ManifestContext:
    project_id: str
    revision: str
    design_hash: str
    app_version: str
    engine_version: str
    template_version: str
    rule_version: str
    material_versions: tuple[str, ...]
    joint_version: str
    machine_profile_id: str
    machine_profile_version: str
    postprocessor_version: str
    cad_status: str
    generation_context_hash: str
    production_engine_context: Mapping[str, Any]
    approved_assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    overrides: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if len(self.generation_context_hash) != 64:
            raise ValueError("manifest requires a 64-character generation context hash")
        if not self.production_engine_context:
            raise ValueError("manifest requires the frozen production engine context")


def default_artifacts(
    *,
    parts: Iterable[PartSpec],
    layout: NestingLayout | Iterable[NestingLayout],
    operations: OperationsDocument,
    additional: Iterable[ArtifactFile] = (),
) -> tuple[ArtifactFile, ...]:
    part_values = tuple(parts)
    layouts = (layout,) if isinstance(layout, NestingLayout) else tuple(layout)
    files: list[ArtifactFile] = [
        ArtifactFile("bom/bom.csv", bom_csv(part_values), "text/csv", "BOM"),
        ArtifactFile("cut-list/cut-list.csv", cut_list_csv(part_values), "text/csv", "CUT_LIST"),
        ArtifactFile(
            "materials/material-list.csv",
            material_list_csv(part_values),
            "text/csv",
            "MATERIAL_LIST",
        ),
        ArtifactFile(
            "cam/operations.json",
            operations.to_json(),
            "application/json",
            "MACHINE_NEUTRAL_OPERATIONS",
        ),
        ArtifactFile("cam/tool-list.csv", tool_list_csv(operations), "text/csv", "TOOL_LIST"),
    ]
    for setup in sorted(operations.setups, key=lambda item: item.setup_id):
        files.append(
            ArtifactFile(
                f"cam/setups/{safe_component(setup.setup_id)}.svg",
                setup_sheet_svg(setup, operations),
                "image/svg+xml",
                "SETUP_SHEET",
            )
        )
    for part in sorted(part_values, key=lambda item: item.part_id):
        component = safe_component(part.part_id)
        for side in (Side.A, Side.B):
            files.append(
                ArtifactFile(
                    f"parts/{component}/{side.value}.dxf",
                    dxf_for_part(part, side),
                    "image/vnd.dxf",
                    "PART_DXF",
                )
            )
            files.append(
                ArtifactFile(
                    f"drawings/{component}/{side.value}.svg",
                    svg_for_part(part, side),
                    "image/svg+xml",
                    "PART_DRAWING",
                )
            )
    for current_layout in sorted(layouts, key=lambda item: item.stock.stock_id):
        stock_component = safe_component(current_layout.stock.stock_id)
        for sheet_index in range(current_layout.used_sheet_count):
            files.append(
                ArtifactFile(
                    f"nesting/{stock_component}/sheet-{sheet_index + 1:03d}.svg",
                    nesting_svg(current_layout, sheet_index),
                    "image/svg+xml",
                    "NESTING_MAP",
                )
            )
    files.extend(additional)
    return tuple(sorted(files, key=lambda item: item.path))


def build_manifest(
    context: ManifestContext,
    artifacts: Iterable[ArtifactFile],
) -> bytes:
    files = tuple(sorted(artifacts, key=lambda item: item.path))
    _validate_unique_paths(files)
    artifact_entries = [
        {
            "path": artifact.path,
            "media_type": artifact.media_type,
            "role": artifact.role,
            "size_bytes": len(artifact.data),
            "sha256": sha256_hex(artifact.data),
        }
        for artifact in files
    ]
    production_context = {
        "project_id": context.project_id,
        "revision": context.revision,
        "design_hash": context.design_hash,
        "app_version": context.app_version,
        "engine_version": context.engine_version,
        "template_version": context.template_version,
        "rule_version": context.rule_version,
        "material_versions": sorted(context.material_versions),
        "joint_version": context.joint_version,
        "machine_profile": {
            "id": context.machine_profile_id,
            "version": context.machine_profile_version,
        },
        "postprocessor_version": context.postprocessor_version,
        "generation_context_hash": context.generation_context_hash,
        "production_engine_context": context.production_engine_context,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "cad_status": context.cad_status,
        "approved_assumptions": sorted(context.approved_assumptions),
        "warnings": sorted(context.warnings),
        "overrides": list(context.overrides),
        "artifacts": artifact_entries,
    }
    manifest = {
        "schema_version": PRODUCTION_MANIFEST_SCHEMA_VERSION,
        **production_context,
        "production_context_hash": sha256_hex(canonical_json_bytes(production_context)),
        "checksum_scope": "all payload files; manifest.json excluded to avoid recursive hashing",
    }
    return canonical_json_bytes(manifest)


def build_deterministic_zip(
    context: ManifestContext,
    artifacts: Iterable[ArtifactFile],
    *,
    production_release: bool = False,
) -> bytes:
    files = tuple(sorted(artifacts, key=lambda item: item.path))
    _validate_unique_paths(files)
    if production_release:
        _validate_release_artifacts(context, files)
    manifest = build_manifest(context, files)

    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for path, payload in [
            ("manifest.json", manifest),
            *((item.path, item.data) for item in files),
        ]:
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits = 0x800
            archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buffer.getvalue()


def read_and_verify_package(payload: bytes) -> dict[str, Any]:
    """Re-parse a package, reject unsafe paths and verify every manifest hash."""

    try:
        archive = zipfile.ZipFile(io.BytesIO(payload), mode="r")
    except zipfile.BadZipFile as exc:
        raise ArtifactError("invalid production ZIP") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_PACKAGE_FILES:
            raise ArtifactError("production ZIP contains too many files")
        total_size = 0
        for info in infos:
            if info.is_dir() or info.flag_bits & 0x1:
                raise ArtifactError("production ZIP contains a directory or encrypted entry")
            if info.file_size > MAX_ARTIFACT_SIZE_BYTES:
                raise ArtifactError(f"production ZIP entry is too large: {info.filename}")
            total_size += info.file_size
            if total_size > MAX_PACKAGE_UNCOMPRESSED_BYTES:
                raise ArtifactError("production ZIP uncompressed size exceeds the safety limit")
            if info.file_size and (
                info.compress_size == 0
                or info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
            ):
                raise ArtifactError(f"unsafe compression ratio: {info.filename}")

        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ArtifactError("production ZIP contains duplicate paths")
        for name in names:
            _validate_artifact_path(name)
        try:
            manifest_value = json.loads(archive.read("manifest.json"))
        except (KeyError, json.JSONDecodeError) as exc:
            raise ArtifactError("package has no valid manifest.json") from exc
        if not isinstance(manifest_value, dict):
            raise ArtifactError("package manifest must be a JSON object")
        manifest: dict[str, Any] = {str(key): value for key, value in manifest_value.items()}
        if manifest.get("schema_version") != PRODUCTION_MANIFEST_SCHEMA_VERSION:
            raise ArtifactError("unsupported production manifest schema")
        entries = manifest.get("artifacts")
        if not isinstance(entries, list):
            raise ArtifactError("manifest artifacts must be an array")
        artifact_paths: list[str] = []
        for raw_entry in entries:
            if not isinstance(raw_entry, dict):
                raise ArtifactError("manifest artifact entry must be an object")
            entry = {str(key): value for key, value in raw_entry.items()}
            path = entry.get("path")
            if not isinstance(path, str):
                raise ArtifactError("manifest artifact path must be a string")
            artifact_paths.append(path)
            try:
                data = archive.read(path)
            except KeyError as exc:
                raise ArtifactError(f"manifest artifact missing from ZIP: {path}") from exc
            if len(data) != entry.get("size_bytes") or sha256_hex(data) != entry.get("sha256"):
                raise ArtifactError(f"artifact checksum mismatch: {path}")
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ArtifactError("manifest contains duplicate artifact paths")
        if set(names) != {"manifest.json", *artifact_paths}:
            raise ArtifactError("production ZIP contains files outside the manifest")
        try:
            context_payload = {
                field: manifest[field] for field in MANIFEST_CONTEXT_HASH_FIELDS
            }
        except KeyError as exc:
            raise ArtifactError(f"manifest context field missing: {exc.args[0]}") from exc
        expected_context_hash = sha256_hex(canonical_json_bytes(context_payload))
        if manifest.get("production_context_hash") != expected_context_hash:
            raise ArtifactError("manifest production_context_hash mismatch")
        return manifest


def safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "part"
    if cleaned == value and len(cleaned) <= 80:
        return cleaned
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:64]}-{digest}"


def _validate_release_artifacts(
    context: ManifestContext,
    files: tuple[ArtifactFile, ...],
) -> None:
    paths = {item.path for item in files}
    required = {"model/design.step", "model/design.glb", "cam/operations.json"}
    missing = sorted(required - paths)
    if context.cad_status != "GENERATED" or missing:
        raise ProductionBlockedError(
            "production release requires genuine STEP and GLB CAD artifacts; "
            f"cad_status={context.cad_status}, missing={missing}"
        )
    raise ProductionBlockedError(
        "production machine release is disabled until a server-bound calibration and "
        "operator-approval catalogue exists; client-supplied evidence is not accepted"
    )


def _validate_unique_paths(files: tuple[ArtifactFile, ...]) -> None:
    paths = [item.path for item in files]
    if len(paths) != len(set(paths)):
        raise ArtifactError("duplicate artifact paths")


def _validate_artifact_path(path: str) -> None:
    if not path or "\\" in path or "\x00" in path:
        raise ArtifactError("invalid artifact path")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ArtifactError(f"unsafe artifact path: {path}")
    candidate = PurePosixPath(path)
    if candidate.is_absolute():
        raise ArtifactError(f"unsafe artifact path: {path}")
