from __future__ import annotations

import hashlib
import io
import subprocess  # noqa: S404 - used to simulate a timeout safely
import zipfile
from pathlib import Path

import custombuild_cad as cad
import pytest

STEP = b"ISO-10303-21;\nEND-ISO-10303-21;"
DESIGN_HASH = "a" * 64


def _fcstd_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("Document.xml", "<Document SchemaVersion='4' />")
    return stream.getvalue()


def _executable(path: Path, body: str) -> str:
    path.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    path.chmod(0o755)  # noqa: S103 - test fixture must be executable
    return str(path)


def test_import_script_marks_fcstd_as_non_authoritative() -> None:
    script = cad.render_freecad_import_script(
        input_step=Path("source.step"),
        output_fcstd=Path("project.fcstd"),
        design_hash=DESIGN_HASH,
        source_step_sha256="b" * 64,
        metadata={"template": "bookcase-1.0.0"},
    )

    assert "Import.insert" in script
    assert "CB_AuthoritativeGeometry = False" in script
    assert "do not use edits as CNC source" in script
    assert "bookcase-1.0.0" in script


def test_project_artifact_rejects_invalid_or_authoritative_payloads() -> None:
    valid = _fcstd_bytes()
    artifact = cad.FreeCADProjectArtifacts(fcstd=valid, source_step_sha256="b" * 64)
    assert artifact.authoritative_geometry is False

    with pytest.raises(cad.FreeCADImportError, match="valid FCStd"):
        cad.FreeCADProjectArtifacts(fcstd=b"not-a-zip", source_step_sha256="b" * 64)
    with pytest.raises(cad.FreeCADImportError, match="SHA-256"):
        cad.FreeCADProjectArtifacts(fcstd=valid, source_step_sha256="invalid")
    with pytest.raises(cad.FreeCADImportError, match="cannot become authoritative"):
        cad.FreeCADProjectArtifacts(
            fcstd=valid,
            source_step_sha256="b" * 64,
            authoritative_geometry=True,
        )


def test_bridge_rejects_invalid_input_before_invoking_freecad() -> None:
    bridge = cad.FreeCADProjectBridge(command="/definitely/missing/FreeCADCmd")

    with pytest.raises(cad.FreeCADImportError, match="genuine STEP"):
        bridge.convert_authoritative_step(b"not-step", DESIGN_HASH)
    with pytest.raises(cad.FreeCADImportError, match="design_hash"):
        bridge.convert_authoritative_step(STEP, "not-a-hash")


def test_bridge_fails_closed_when_freecad_is_unavailable() -> None:
    bridge = cad.FreeCADProjectBridge(command=None)
    bridge.command = None

    with pytest.raises(cad.FreeCADDependencyUnavailable):
        bridge.convert_authoritative_step(STEP, DESIGN_HASH)


def test_bridge_creates_fcstd_with_fake_headless_freecad(tmp_path: Path) -> None:
    command = _executable(
        tmp_path / "fake-freecad",
        """import json
import re
import sys
import zipfile
from pathlib import Path
script = Path(sys.argv[1]).read_text(encoding='utf-8')
match = re.search(r'^OUTPUT_FCSTD = (.+)$', script, re.MULTILINE)
if match is None:
    raise SystemExit(4)
output = Path(json.loads(match.group(1)))
with zipfile.ZipFile(output, 'w') as archive:
    archive.writestr('Document.xml', '<Document />')""",
    )
    bridge = cad.FreeCADProjectBridge(command=command)

    result = bridge.convert_authoritative_step(
        STEP,
        DESIGN_HASH,
        metadata={"template": "bookcase", "revision": "1"},
    )

    assert result.fcstd.startswith(b"PK")
    assert result.source_step_sha256 == hashlib.sha256(STEP).hexdigest()
    assert result.authoritative_geometry is False


def test_bridge_reports_process_failure_and_missing_output(tmp_path: Path) -> None:
    failing = _executable(
        tmp_path / "freecad-fail",
        "import sys\nsys.stderr.write('import failed')\nraise SystemExit(3)",
    )
    with pytest.raises(cad.FreeCADImportError, match="import failed"):
        cad.FreeCADProjectBridge(command=failing).convert_authoritative_step(STEP, DESIGN_HASH)

    no_output = _executable(tmp_path / "freecad-no-output", "raise SystemExit(0)")
    with pytest.raises(cad.FreeCADImportError, match="without an FCStd"):
        cad.FreeCADProjectBridge(command=no_output).convert_authoritative_step(STEP, DESIGN_HASH)


def test_bridge_wraps_invocation_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="FreeCADCmd", timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)
    bridge = cad.FreeCADProjectBridge(command="/usr/bin/FreeCADCmd", timeout_seconds=1)

    with pytest.raises(cad.FreeCADImportError, match="invocation failed"):
        bridge.convert_authoritative_step(STEP, DESIGN_HASH)


def test_bridge_validates_metadata_before_process_execution() -> None:
    bridge = cad.FreeCADProjectBridge(command="/bin/true")
    with pytest.raises(cad.FreeCADImportError, match="keys"):
        bridge.convert_authoritative_step(STEP, DESIGN_HASH, metadata={" ": "value"})
    with pytest.raises(cad.FreeCADImportError, match="values"):
        bridge.convert_authoritative_step(STEP, DESIGN_HASH, metadata={"note": "x" * 501})


def test_availability_and_status_follow_resolved_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custombuild_cad.freecad_bridge.shutil.which",
        lambda command: "/opt/freecad/FreeCADCmd" if command == "FreeCADCmd" else None,
    )
    assert cad.FreeCADProjectBridge.available() is True
    status = cad.freecad_bridge_status()
    assert status["available"] is True
    assert status["command"] == "/opt/freecad/FreeCADCmd"

    monkeypatch.setattr("custombuild_cad.freecad_bridge.shutil.which", lambda command: None)
    assert cad.FreeCADProjectBridge.available() is False
    assert cad.freecad_bridge_status()["status"] == "BLOCKED_UNAVAILABLE"
