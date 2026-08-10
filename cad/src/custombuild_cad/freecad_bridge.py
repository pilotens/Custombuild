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
import re
import shutil
import subprocess  # noqa: S404 - required for the isolated headless CAD process
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

FREECAD_BRIDGE_VERSION = "freecad-project-bridge-1.0.0"
FREECAD_PROJECT_CONTRACT_VERSION = "freecad-imported-step-contract-1.0.0"
_FREECAD_COMMANDS = ("FreeCADCmd", "freecadcmd")
_DESIGN_HASH = re.compile(r"^[a-f0-9]{64}$")


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
    bridge_version: str = FREECAD_BRIDGE_VERSION
    authoritative_geometry: bool = False

    def __post_init__(self) -> None:
        if not self.fcstd.startswith(b"PK") or not zipfile.is_zipfile(io.BytesIO(self.fcstd)):
            raise FreeCADImportError("FreeCAD did not produce a valid FCStd ZIP container")
        if not _DESIGN_HASH.fullmatch(self.source_step_sha256):
            raise FreeCADImportError("source STEP checksum is not a SHA-256 digest")
        if self.authoritative_geometry:
            raise FreeCADImportError(
                "an imported FCStd derivative cannot become authoritative design geometry"
            )


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
            script_path = directory / "import_custombuild.py"
            step_path.write_bytes(step)
            script_path.write_text(
                render_freecad_import_script(
                    input_step=step_path,
                    output_fcstd=output_path,
                    design_hash=design_hash,
                    source_step_sha256=source_checksum,
                    metadata=safe_metadata,
                ),
                encoding="utf-8",
            )
            try:
                completed = subprocess.run(  # noqa: S603 - fixed executable, no shell
                    [command, str(script_path)],
                    capture_output=True,
                    check=False,
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
            fcstd = output_path.read_bytes()

        return FreeCADProjectArtifacts(
            fcstd=fcstd,
            source_step_sha256=source_checksum,
            bridge_version=self.version,
            authoritative_geometry=False,
        )


def render_freecad_import_script(
    *,
    input_step: Path,
    output_fcstd: Path,
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
    metadata_literal = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"""# Generated by Custombuild; do not edit as production geometry.
import json
import FreeCAD as App
import Import

INPUT_STEP = {input_literal}
OUTPUT_FCSTD = {output_literal}
METADATA = json.loads({json.dumps(metadata_literal)})

document = App.newDocument("Custombuild")
Import.insert(INPUT_STEP, document.Name)
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
App.closeDocument(document.Name)
"""


def freecad_bridge_status() -> dict[str, str | bool]:
    command = _find_freecad_command()
    return {
        "available": command is not None,
        "status": "AVAILABLE" if command else "BLOCKED_UNAVAILABLE",
        "command": command or "not installed",
        "bridge_version": FREECAD_BRIDGE_VERSION,
        "contract_version": FREECAD_PROJECT_CONTRACT_VERSION,
        "authoritative_geometry": False,
    }


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
