"""Optional headless FreeCAD bridge for native project interchange.

Custombuild keeps the semantic design document and the deterministic domain
result as the source of truth.  This bridge imports an already-authoritative STEP
assembly produced by :mod:`custombuild_cad.adapter` into a native ``.FCStd``
container.  FreeCAD is therefore a replaceable downstream CAD implementation,
not an editable source model and never a shortcut around DFM/CAM validation.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess  # noqa: S404 - required for the isolated headless CAD process
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

FREECAD_BRIDGE_VERSION = "freecad-project-bridge-1.1.0"
FREECAD_PROJECT_CONTRACT_VERSION = "freecad-part-read-step-contract-1.2.0"
FREECAD_GEOMETRY_PROBE_VERSION = "freecad-reopen-geometry-probe-1.0.0"
FREECAD_GEOMETRY_EVIDENCE_SCHEMA = "custombuild.freecad-geometry-verification.v1"
FREECAD_BOUNDS_TOLERANCE_MM = "0.00001"
FREECAD_VOLUME_ABSOLUTE_TOLERANCE_MM3 = "0.001"
FREECAD_VOLUME_RELATIVE_TOLERANCE = "0.000000001"
_FREECAD_COMMANDS = ("FreeCADCmd", "freecadcmd")
_DESIGN_HASH = re.compile(r"^[a-f0-9]{64}$")
_RUNTIME_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")
_FCSTD_MAX_FILES = 10_000
_FCSTD_MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_STATUS_MAX_BYTES = 100_000
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_BOUND_NAMES = ("x_min", "y_min", "z_min", "x_max", "y_max", "z_max")
_DYNAMIC_DOCUMENT_METADATA = re.compile(
    rb'(<Property name="(?:CreationDate|LastModifiedDate)"[^>]*>.*?'
    rb'<String value=")[^"]*(".*?</Property>)',
    re.DOTALL,
)


class FreeCADBridgeError(RuntimeError):
    """Base error for the optional FreeCAD integration boundary."""


class FreeCADDependencyUnavailable(FreeCADBridgeError):
    """Raised when no headless FreeCAD executable can be resolved."""


class FreeCADImportError(FreeCADBridgeError):
    """Raised when FreeCAD cannot create a complete native project."""


@dataclass(frozen=True, slots=True)
class FreeCADProjectArtifacts:
    """Native FreeCAD project derived from authoritative STEP geometry."""

    fcstd: bytes
    source_step_sha256: str
    runtime_version: str = "unknown"
    bridge_version: str = FREECAD_BRIDGE_VERSION
    authoritative_geometry: bool = False
    geometry_verification: bytes | None = None

    def __post_init__(self) -> None:
        if not self.fcstd.startswith(b"PK") or not zipfile.is_zipfile(io.BytesIO(self.fcstd)):
            raise FreeCADImportError("FreeCAD did not produce a valid FCStd ZIP container")
        with zipfile.ZipFile(io.BytesIO(self.fcstd)) as archive:
            names = archive.namelist()
            if "Document.xml" not in names:
                raise FreeCADImportError("FreeCAD project is missing Document.xml")
            if len(names) != len(set(names)) or len(names) > _FCSTD_MAX_FILES:
                raise FreeCADImportError(
                    "FreeCAD project contains unsafe duplicate or excess files"
                )
            total_size = 0
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if (
                    info.is_dir()
                    or "\\" in info.filename
                    or "\x00" in info.filename
                    or path.is_absolute()
                    or ".." in path.parts
                ):
                    raise FreeCADImportError("FreeCAD project contains an unsafe ZIP path")
                total_size += info.file_size
                if total_size > _FCSTD_MAX_UNCOMPRESSED_BYTES:
                    raise FreeCADImportError("FreeCAD project exceeds the safe size limit")
        if not _DESIGN_HASH.fullmatch(self.source_step_sha256):
            raise FreeCADImportError("source STEP checksum is not a SHA-256 digest")
        if not _RUNTIME_VERSION.fullmatch(self.runtime_version):
            raise FreeCADImportError("FreeCAD runtime version is invalid")
        if self.authoritative_geometry:
            raise FreeCADImportError(
                "an imported FCStd derivative cannot become authoritative design geometry"
            )
        if self.geometry_verification is not None:
            _validate_geometry_verification(
                self.geometry_verification,
                expected_source_step_sha256=self.source_step_sha256,
                expected_runtime_version=self.runtime_version,
            )

    @property
    def geometry_verified(self) -> bool:
        """Whether the authoritative STEP and reopened FCStd passed the probe."""

        return self.geometry_verification is not None

    @property
    def geometry_verification_sha256(self) -> str | None:
        """Content identity for the canonical geometry-verification evidence."""

        if self.geometry_verification is None:
            return None
        return hashlib.sha256(self.geometry_verification).hexdigest()


@dataclass(frozen=True, slots=True)
class _FreeCADRunStatus:
    runtime_version: str
    geometry_verification: bytes


class FreeCADProjectBridge:
    """Convert Custombuild's authoritative STEP assembly to a native FCStd file."""

    version = FREECAD_BRIDGE_VERSION

    def __init__(self, command: str | None = None, *, timeout_seconds: int = 120) -> None:
        self.command = command or _find_freecad_command()
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def available() -> bool:
        return _find_freecad_command() is not None

    def convert_authoritative_step(
        self,
        step: bytes,
        design_hash: str,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> FreeCADProjectArtifacts:
        """Create a native FreeCAD project from a validated STEP assembly.

        The bridge accepts no raw furniture dimensions and creates no toolpaths.
        That prevents a FreeCAD macro or an AI-generated script from bypassing
        Custombuild's domain model, rule engine or manufacturing-operation model.
        """

        if not step.startswith(b"ISO-10303-21"):
            raise FreeCADImportError("FreeCAD bridge requires genuine STEP input")
        if not _DESIGN_HASH.fullmatch(design_hash):
            raise FreeCADImportError("design_hash must be a lowercase SHA-256 digest")
        command = self.command
        if not command:
            raise FreeCADDependencyUnavailable(
                "FreeCADCmd is unavailable; FCStd generation is optional and blocked"
            )

        source_checksum = hashlib.sha256(step).hexdigest()
        safe_metadata = _normalise_metadata(metadata or {})
        with tempfile.TemporaryDirectory(prefix="custombuild-freecad-") as temporary:
            directory = Path(temporary)
            step_path = directory / "authoritative.step"
            output_path = directory / "custombuild.fcstd"
            status_path = directory / "custombuild.status.json"
            script_path = directory / "import_custombuild.py"
            step_path.write_bytes(step)
            script_path.write_text(
                render_freecad_import_script(
                    input_step=step_path,
                    output_fcstd=output_path,
                    output_status=status_path,
                    design_hash=design_hash,
                    source_step_sha256=source_checksum,
                    metadata=safe_metadata,
                ),
                encoding="utf-8",
            )
            environment = _freecad_environment(directory)
            try:
                completed = subprocess.run(  # noqa: S603 - fixed executable, no shell
                    [command, str(script_path)],
                    capture_output=True,
                    check=False,
                    env=environment,
                    text=True,
                    timeout=self.timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise FreeCADImportError(f"headless FreeCAD invocation failed: {exc}") from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "unknown error").strip()
                raise FreeCADImportError(f"FreeCAD STEP import failed: {detail[:1_000]}")
            if not output_path.is_file():
                raise FreeCADImportError("FreeCAD exited successfully without an FCStd file")
            run_status = _read_run_status(
                status_path,
                expected_source_step_sha256=source_checksum,
            )
            fcstd = _normalise_fcstd(output_path.read_bytes())

        return FreeCADProjectArtifacts(
            fcstd=fcstd,
            source_step_sha256=source_checksum,
            runtime_version=run_status.runtime_version,
            bridge_version=self.version,
            authoritative_geometry=False,
            geometry_verification=run_status.geometry_verification,
        )


def render_freecad_import_script(
    *,
    input_step: Path,
    output_fcstd: Path,
    output_status: Path | None = None,
    design_hash: str,
    source_step_sha256: str,
    metadata: Mapping[str, str],
) -> str:
    """Render a deterministic, data-only FreeCAD import script."""

    payload = {
        "bridge_version": FREECAD_BRIDGE_VERSION,
        "contract_version": FREECAD_PROJECT_CONTRACT_VERSION,
        "design_hash": design_hash,
        "source_step_sha256": source_step_sha256,
        **metadata,
    }
    input_literal = json.dumps(str(input_step), ensure_ascii=True)
    output_literal = json.dumps(str(output_fcstd), ensure_ascii=True)
    status_literal = json.dumps(
        str(output_status or output_fcstd.with_suffix(".status.json")),
        ensure_ascii=True,
    )
    metadata_literal = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"""# Generated by Custombuild; do not edit as production geometry.
import json
import math
import FreeCAD as App
import Part

INPUT_STEP = {input_literal}
OUTPUT_FCSTD = {output_literal}
OUTPUT_STATUS = {status_literal}
METADATA = json.loads({json.dumps(metadata_literal)})
GEOMETRY_SCHEMA = {json.dumps(FREECAD_GEOMETRY_EVIDENCE_SCHEMA)}
GEOMETRY_PROBE_VERSION = {json.dumps(FREECAD_GEOMETRY_PROBE_VERSION)}
BOUNDS_TOLERANCE_MM_TEXT = {json.dumps(FREECAD_BOUNDS_TOLERANCE_MM)}
VOLUME_ABSOLUTE_TOLERANCE_MM3_TEXT = {json.dumps(FREECAD_VOLUME_ABSOLUTE_TOLERANCE_MM3)}
VOLUME_RELATIVE_TOLERANCE_TEXT = {json.dumps(FREECAD_VOLUME_RELATIVE_TOLERANCE)}
BOUNDS_TOLERANCE_MM = float(BOUNDS_TOLERANCE_MM_TEXT)
VOLUME_ABSOLUTE_TOLERANCE_MM3 = float(VOLUME_ABSOLUTE_TOLERANCE_MM3_TEXT)
VOLUME_RELATIVE_TOLERANCE = float(VOLUME_RELATIVE_TOLERANCE_TEXT)


def stable_number(value):
    numeric = float(value)
    if not math.isfinite(numeric):
        raise RuntimeError("FreeCAD geometry contains a non-finite measurement")
    return format(numeric, ".17g")


def geometry_fingerprint(shape):
    solids = list(shape.Solids)
    if not solids:
        raise RuntimeError("FreeCAD geometry contains no solids")
    bounds = shape.BoundBox
    solid_volumes = sorted(float(solid.Volume) for solid in solids)
    if any(not math.isfinite(value) or value < 0.0 for value in solid_volumes):
        raise RuntimeError("FreeCAD geometry contains an invalid solid volume")
    return {{
        "solid_count": len(solids),
        "bounds_mm": {{
            "x_min": stable_number(bounds.XMin),
            "y_min": stable_number(bounds.YMin),
            "z_min": stable_number(bounds.ZMin),
            "x_max": stable_number(bounds.XMax),
            "y_max": stable_number(bounds.YMax),
            "z_max": stable_number(bounds.ZMax),
        }},
        "solid_volumes_mm3": [stable_number(value) for value in solid_volumes],
        "total_volume_mm3": stable_number(sum(solid_volumes)),
    }}


def volume_matches(left, right):
    tolerance = max(
        VOLUME_ABSOLUTE_TOLERANCE_MM3,
        VOLUME_RELATIVE_TOLERANCE * max(abs(left), abs(right)),
    )
    return abs(left - right) <= tolerance


def fingerprints_match(source, reopened):
    if source["solid_count"] != reopened["solid_count"]:
        return False
    for name in ("x_min", "y_min", "z_min", "x_max", "y_max", "z_max"):
        bounds_delta = abs(
            float(source["bounds_mm"][name]) - float(reopened["bounds_mm"][name])
        )
        if bounds_delta > BOUNDS_TOLERANCE_MM:
            return False
    if not volume_matches(
        float(source["total_volume_mm3"]),
        float(reopened["total_volume_mm3"]),
    ):
        return False
    source_volumes = source["solid_volumes_mm3"]
    reopened_volumes = reopened["solid_volumes_mm3"]
    return len(source_volumes) == len(reopened_volumes) and all(
        volume_matches(float(left), float(right))
        for left, right in zip(source_volumes, reopened_volumes)
    )

document = App.newDocument("Custombuild")
runtime_version = ".".join(str(part) for part in App.Version()[:3])
METADATA["freecad_runtime_version"] = runtime_version
for property_name, property_value in (
    ("Label", "Custombuild"),
    ("CreatedBy", "Custombuild"),
    ("LastModifiedBy", "Custombuild"),
    ("Company", "Custombuild"),
    ("Comment", "Derived from an authoritative Custombuild STEP file."),
    ("CreationDate", "1980-01-01T00:00:00Z"),
    ("LastModifiedDate", "1980-01-01T00:00:00Z"),
):
    if hasattr(document, property_name):
        try:
            setattr(document, property_name, property_value)
        except Exception:
            pass
source_shape = Part.read(INPUT_STEP)
if source_shape.isNull():
    raise RuntimeError("FreeCAD Part.read returned empty STEP geometry")
source_fingerprint = geometry_fingerprint(source_shape)
METADATA["geometry_probe_version"] = GEOMETRY_PROBE_VERSION
METADATA["source_solid_count"] = str(source_fingerprint["solid_count"])
source_object = document.addObject("Part::Feature", "AuthoritativeSTEP")
source_object.Label = "Authoritative Custombuild STEP (non-authoritative derivative)"
source_object.Shape = source_shape
metadata = document.addObject("App::FeaturePython", "CustombuildMetadata")
for key, value in sorted(METADATA.items()):
    property_name = "CB_" + "".join(
        character if character.isalnum() else "_" for character in key
    )
    metadata.addProperty("App::PropertyString", property_name, "Custombuild")
    setattr(metadata, property_name, str(value))
metadata.addProperty("App::PropertyBool", "CB_AuthoritativeGeometry", "Custombuild")
metadata.CB_AuthoritativeGeometry = False
metadata.addProperty("App::PropertyString", "CB_Notice", "Custombuild")
metadata.CB_Notice = (
    "Derivative project. Regenerate from Custombuild DesignSpec; "
    "do not use edits as CNC source."
)
document.recompute()
document.saveAs(OUTPUT_FCSTD)
source_document_name = document.Name
App.closeDocument(source_document_name)

reopened_document = App.openDocument(OUTPUT_FCSTD)
reopened_object = reopened_document.getObject("AuthoritativeSTEP")
if reopened_object is None or reopened_object.Shape.isNull():
    raise RuntimeError("reopened FreeCAD project is missing authoritative STEP geometry")
reopened_document.recompute()
reopened_fingerprint = geometry_fingerprint(reopened_object.Shape)
if not fingerprints_match(source_fingerprint, reopened_fingerprint):
    raise RuntimeError(
        "reopened FreeCAD project geometry differs from the authoritative STEP"
    )
App.closeDocument(reopened_document.Name)

verification = {{
    "schema": GEOMETRY_SCHEMA,
    "probe_version": GEOMETRY_PROBE_VERSION,
    "runtime_version": runtime_version,
    "source_step_sha256": METADATA["source_step_sha256"],
    "geometry_verified": True,
    "tolerances": {{
        "bounds_absolute_mm": BOUNDS_TOLERANCE_MM_TEXT,
        "volume_absolute_mm3": VOLUME_ABSOLUTE_TOLERANCE_MM3_TEXT,
        "volume_relative": VOLUME_RELATIVE_TOLERANCE_TEXT,
    }},
    "source_geometry": source_fingerprint,
    "reopened_geometry": reopened_fingerprint,
}}
with open(OUTPUT_STATUS, "w", encoding="utf-8") as status_file:
    json.dump(verification, status_file, sort_keys=True, separators=(",", ":"))
"""


def freecad_bridge_status() -> dict[str, str | bool]:
    command = _find_freecad_command()
    return {
        "available": command is not None,
        "status": "AVAILABLE" if command else "BLOCKED_UNAVAILABLE",
        "command": command or "not installed",
        "bridge_version": FREECAD_BRIDGE_VERSION,
        "contract_version": FREECAD_PROJECT_CONTRACT_VERSION,
        "geometry_probe_version": FREECAD_GEOMETRY_PROBE_VERSION,
        "geometry_probe_required_when_requested": True,
        "authoritative_geometry": False,
    }


def _normalise_fcstd(payload: bytes) -> bytes:
    """Repack FreeCAD's ZIP container with stable order and metadata.

    FreeCAD owns the XML payloads, while Custombuild owns package identity. The
    repack removes ZIP timestamps, host permissions and entry-order drift before
    the derivative project enters a content-addressed review bundle.
    """

    try:
        source = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise FreeCADImportError("FreeCAD did not produce a valid FCStd ZIP container") from exc
    output = io.BytesIO()
    with (
        source,
        zipfile.ZipFile(
            output,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as target,
    ):
        infos = source.infolist()
        if "Document.xml" not in {item.filename for item in infos}:
            raise FreeCADImportError("FreeCAD project is missing Document.xml")
        if len(infos) != len({item.filename for item in infos}) or len(infos) > _FCSTD_MAX_FILES:
            raise FreeCADImportError("FreeCAD project contains unsafe duplicate or excess files")
        total_size = 0
        for item in sorted(infos, key=lambda entry: entry.filename):
            path = PurePosixPath(item.filename)
            if (
                item.is_dir()
                or "\\" in item.filename
                or "\x00" in item.filename
                or path.is_absolute()
                or ".." in path.parts
            ):
                raise FreeCADImportError("FreeCAD project contains an unsafe ZIP path")
            data = source.read(item)
            if item.filename == "Document.xml":
                data = _DYNAMIC_DOCUMENT_METADATA.sub(
                    rb"\g<1>1980-01-01T00:00:00Z\g<2>",
                    data,
                )
            total_size += len(data)
            if total_size > _FCSTD_MAX_UNCOMPRESSED_BYTES:
                raise FreeCADImportError("FreeCAD project exceeds the safe size limit")
            normalized = zipfile.ZipInfo(item.filename, date_time=_ZIP_TIMESTAMP)
            normalized.compress_type = zipfile.ZIP_DEFLATED
            normalized.create_system = 3
            normalized.external_attr = 0o100644 << 16
            target.writestr(normalized, data)
    return output.getvalue()


def _read_run_status(
    status_path: Path,
    *,
    expected_source_step_sha256: str,
) -> _FreeCADRunStatus:
    if not status_path.is_file() or status_path.stat().st_size > _STATUS_MAX_BYTES:
        raise FreeCADImportError("FreeCAD exited without valid geometry-verification evidence")
    try:
        evidence = status_path.read_bytes()
    except OSError as exc:
        raise FreeCADImportError("FreeCAD geometry-verification evidence is unreadable") from exc
    payload = _validate_geometry_verification(
        evidence,
        expected_source_step_sha256=expected_source_step_sha256,
    )
    runtime_version = payload["runtime_version"]
    if not isinstance(runtime_version, str):
        raise FreeCADImportError("FreeCAD runtime-version evidence is invalid")
    return _FreeCADRunStatus(
        runtime_version=runtime_version,
        geometry_verification=evidence,
    )


def _validate_geometry_verification(
    evidence: bytes,
    *,
    expected_source_step_sha256: str,
    expected_runtime_version: str | None = None,
) -> dict[str, object]:
    try:
        decoded = evidence.decode("utf-8")
        loaded = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FreeCADImportError("FreeCAD geometry-verification evidence is invalid") from exc
    payload = _json_mapping(loaded, "geometry-verification evidence")
    expected_keys = {
        "schema",
        "probe_version",
        "runtime_version",
        "source_step_sha256",
        "geometry_verified",
        "tolerances",
        "source_geometry",
        "reopened_geometry",
    }
    if set(payload) != expected_keys:
        raise FreeCADImportError("FreeCAD geometry-verification evidence has invalid fields")
    if payload["schema"] != FREECAD_GEOMETRY_EVIDENCE_SCHEMA:
        raise FreeCADImportError("FreeCAD geometry-verification schema is unsupported")
    if payload["probe_version"] != FREECAD_GEOMETRY_PROBE_VERSION:
        raise FreeCADImportError("FreeCAD geometry-probe version is unsupported")
    if payload["source_step_sha256"] != expected_source_step_sha256:
        raise FreeCADImportError(
            "FreeCAD geometry verification is not bound to the authoritative STEP"
        )
    if payload["geometry_verified"] is not True:
        raise FreeCADImportError("FreeCAD geometry verification did not pass")
    runtime_version = payload["runtime_version"]
    if not isinstance(runtime_version, str) or not _RUNTIME_VERSION.fullmatch(runtime_version):
        raise FreeCADImportError("FreeCAD runtime-version evidence is invalid")
    if expected_runtime_version is not None and runtime_version != expected_runtime_version:
        raise FreeCADImportError("FreeCAD geometry evidence has a runtime-version mismatch")

    tolerances = _json_mapping(payload["tolerances"], "geometry tolerances")
    expected_tolerances = {
        "bounds_absolute_mm": FREECAD_BOUNDS_TOLERANCE_MM,
        "volume_absolute_mm3": FREECAD_VOLUME_ABSOLUTE_TOLERANCE_MM3,
        "volume_relative": FREECAD_VOLUME_RELATIVE_TOLERANCE,
    }
    if tolerances != expected_tolerances:
        raise FreeCADImportError("FreeCAD geometry evidence has unexpected tolerances")

    source = _geometry_fingerprint(payload["source_geometry"], "source")
    reopened = _geometry_fingerprint(payload["reopened_geometry"], "reopened")
    _verify_matching_fingerprints(source, reopened)

    if evidence != _canonical_json_bytes(payload):
        raise FreeCADImportError("FreeCAD geometry-verification evidence is not canonical")
    return payload


def _json_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise FreeCADImportError(f"FreeCAD {label} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _geometry_fingerprint(
    value: object,
    label: str,
) -> tuple[int, tuple[Decimal, ...], tuple[Decimal, ...], Decimal]:
    payload = _json_mapping(value, f"{label} geometry fingerprint")
    if set(payload) != {
        "solid_count",
        "bounds_mm",
        "solid_volumes_mm3",
        "total_volume_mm3",
    }:
        raise FreeCADImportError(f"FreeCAD {label} geometry fingerprint has invalid fields")
    solid_count = payload["solid_count"]
    if isinstance(solid_count, bool) or not isinstance(solid_count, int) or solid_count <= 0:
        raise FreeCADImportError(f"FreeCAD {label} geometry has an invalid solid count")
    bounds_payload = _json_mapping(payload["bounds_mm"], f"{label} bounds")
    if set(bounds_payload) != set(_BOUND_NAMES):
        raise FreeCADImportError(f"FreeCAD {label} geometry bounds have invalid fields")
    bounds = tuple(
        _finite_decimal(bounds_payload[name], f"{label} bound {name}") for name in _BOUND_NAMES
    )
    if bounds[0] > bounds[3] or bounds[1] > bounds[4] or bounds[2] > bounds[5]:
        raise FreeCADImportError(f"FreeCAD {label} geometry has inverted bounds")
    volumes_payload = payload["solid_volumes_mm3"]
    if not isinstance(volumes_payload, list) or len(volumes_payload) != solid_count:
        raise FreeCADImportError(f"FreeCAD {label} geometry has invalid solid volumes")
    volumes = tuple(_finite_decimal(item, f"{label} solid volume") for item in volumes_payload)
    if any(volume < 0 for volume in volumes) or tuple(sorted(volumes)) != volumes:
        raise FreeCADImportError(f"FreeCAD {label} geometry has invalid solid volumes")
    total_volume = _finite_decimal(
        payload["total_volume_mm3"],
        f"{label} total volume",
    )
    if total_volume < 0 or not _volume_values_match(total_volume, sum(volumes, Decimal(0))):
        raise FreeCADImportError(f"FreeCAD {label} geometry has an invalid total volume")
    return solid_count, bounds, volumes, total_volume


def _finite_decimal(value: object, label: str) -> Decimal:
    if not isinstance(value, str):
        raise FreeCADImportError(f"FreeCAD {label} must be a deterministic decimal string")
    try:
        numeric = Decimal(value)
    except InvalidOperation as exc:
        raise FreeCADImportError(f"FreeCAD {label} is invalid") from exc
    if not numeric.is_finite():
        raise FreeCADImportError(f"FreeCAD {label} is not finite")
    return numeric


def _verify_matching_fingerprints(
    source: tuple[int, tuple[Decimal, ...], tuple[Decimal, ...], Decimal],
    reopened: tuple[int, tuple[Decimal, ...], tuple[Decimal, ...], Decimal],
) -> None:
    source_count, source_bounds, source_volumes, source_total = source
    reopened_count, reopened_bounds, reopened_volumes, reopened_total = reopened
    if source_count != reopened_count:
        raise FreeCADImportError(
            "reopened FreeCAD geometry has a different solid count than the authoritative STEP"
        )
    bounds_tolerance = Decimal(FREECAD_BOUNDS_TOLERANCE_MM)
    if any(
        abs(source_value - reopened_value) > bounds_tolerance
        for source_value, reopened_value in zip(source_bounds, reopened_bounds, strict=True)
    ):
        raise FreeCADImportError(
            "reopened FreeCAD geometry bounds differ from the authoritative STEP"
        )
    if not _volume_values_match(source_total, reopened_total) or any(
        not _volume_values_match(source_value, reopened_value)
        for source_value, reopened_value in zip(source_volumes, reopened_volumes, strict=True)
    ):
        raise FreeCADImportError(
            "reopened FreeCAD geometry volumes differ from the authoritative STEP"
        )


def _volume_values_match(left: Decimal, right: Decimal) -> bool:
    absolute = Decimal(FREECAD_VOLUME_ABSOLUTE_TOLERANCE_MM3)
    relative = Decimal(FREECAD_VOLUME_RELATIVE_TOLERANCE) * max(abs(left), abs(right))
    return abs(left - right) <= max(absolute, relative)


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _freecad_environment(directory: Path) -> dict[str, str]:
    """Give embedded FreeCAD/Python writable, job-local user directories."""

    environment = dict(os.environ)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    runtime_home = directory / "runtime-home"
    cache_home = runtime_home / "cache"
    config_home = runtime_home / "config"
    data_home = runtime_home / "data"
    for path in (runtime_home, cache_home, config_home, data_home):
        path.mkdir(mode=0o700)
    environment.update(
        {
            "HOME": str(runtime_home),
            "TMPDIR": str(directory),
            "XDG_CACHE_HOME": str(cache_home),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(data_home),
        }
    )
    return environment


def _find_freecad_command() -> str | None:
    for command in _FREECAD_COMMANDS:
        resolved = shutil.which(command)
        if resolved:
            return resolved
    return None


def _normalise_metadata(metadata: Mapping[str, str]) -> dict[str, str]:
    normalised: dict[str, str] = {}
    for key, value in metadata.items():
        clean_key = key.strip()
        if not clean_key or len(clean_key) > 80:
            raise FreeCADImportError("FreeCAD metadata keys must be 1-80 characters")
        if len(value) > 500:
            raise FreeCADImportError("FreeCAD metadata values must be at most 500 characters")
        normalised[clean_key] = value
    return normalised
