from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess  # noqa: S404 - used to simulate a timeout safely
import sys
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
    if os.name == "nt":
        script = path.with_suffix(".py")
        script.write_text(f"{body}\n", encoding="utf-8")
        wrapper = path.with_suffix(".cmd")
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "%~dp0{script.name}" %*\r\nexit /b %ERRORLEVEL%\r\n',
            encoding="utf-8",
            newline="",
        )
        return str(wrapper)

    path.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    path.chmod(0o755)  # noqa: S103 - test fixture must be executable
    return str(path)


def _geometry_fingerprint(
    *,
    solid_count: int = 2,
    x_max: str = "300",
    volumes: list[str] | None = None,
) -> dict[str, object]:
    solid_volumes = volumes or ["1000", "2000"]
    return {
        "solid_count": solid_count,
        "bounds_mm": {
            "x_min": "0",
            "y_min": "0",
            "z_min": "0",
            "x_max": x_max,
            "y_max": "200",
            "z_max": "18",
        },
        "solid_volumes_mm3": solid_volumes,
        "total_volume_mm3": str(sum(float(item) for item in solid_volumes)),
    }


def _geometry_verification_bytes(
    *,
    source_step: bytes = STEP,
    source: dict[str, object] | None = None,
    reopened: dict[str, object] | None = None,
) -> bytes:
    source_geometry = source or _geometry_fingerprint()
    reopened_geometry = reopened or source_geometry
    payload = {
        "schema": "custombuild.freecad-geometry-verification.v1",
        "probe_version": "freecad-reopen-geometry-probe-1.0.0",
        "runtime_version": "0.20.2",
        "source_step_sha256": hashlib.sha256(source_step).hexdigest(),
        "geometry_verified": True,
        "tolerances": {
            "bounds_absolute_mm": "0.00001",
            "volume_absolute_mm3": "0.001",
            "volume_relative": "0.000000001",
        },
        "source_geometry": source_geometry,
        "reopened_geometry": reopened_geometry,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _fake_freecad_command(
    path: Path,
    evidence: bytes,
    *,
    vary_zip_metadata: bool = False,
) -> str:
    iteration_setup = (
        "counter = Path(sys.argv[0]).with_suffix('.count')\n"
        "iteration = int(counter.read_text() if counter.exists() else '0') + 1\n"
        "counter.write_text(str(iteration))\n"
        if vary_zip_metadata
        else "iteration = 1\n"
    )
    return _executable(
        path,
        f"""import json
import os
import re
import sys
import zipfile
from pathlib import Path
script = Path(sys.argv[1]).read_text(encoding='utf-8')
runtime_home = Path(os.environ['HOME'])
if not runtime_home.is_dir() or not os.access(runtime_home, os.W_OK):
    raise SystemExit(5)
match = re.search(r'^OUTPUT_FCSTD = (.+)$', script, re.MULTILINE)
status_match = re.search(r'^OUTPUT_STATUS = (.+)$', script, re.MULTILINE)
if match is None or status_match is None:
    raise SystemExit(4)
output = Path(json.loads(match.group(1)))
status_output = Path(json.loads(status_match.group(1)))
{iteration_setup}with zipfile.ZipFile(output, 'w') as archive:
    info = zipfile.ZipInfo('Document.xml', (2020 + iteration, 1, 1, 0, 0, 0))
    archive.writestr(
        info,
        '<Document><Property name="CreationDate"><String value="run-'
        + str(iteration)
        + '" /></Property></Document>',
    )
status_output.write_bytes({evidence!r})""",
    )


def test_import_script_marks_fcstd_as_non_authoritative() -> None:
    script = cad.render_freecad_import_script(
        input_step=Path("source.step"),
        output_fcstd=Path("project.fcstd"),
        design_hash=DESIGN_HASH,
        source_step_sha256="b" * 64,
        metadata={"template": "bookcase-1.0.0"},
    )

    compile(script, "import_custombuild.py", "exec")
    assert "Part.read" in script
    assert "Part::Feature" in script
    assert "App.openDocument" in script
    assert "geometry_fingerprint" in script
    assert "fingerprints_match" in script
    assert "bounds_absolute_mm" in script
    assert "volume_relative" in script
    assert "freecad_runtime_version" in script
    assert "CB_AuthoritativeGeometry = False" in script
    assert "do not use edits as CNC source" in script
    assert "bookcase-1.0.0" in script


def test_project_artifact_rejects_invalid_or_authoritative_payloads() -> None:
    valid = _fcstd_bytes()
    artifact = cad.FreeCADProjectArtifacts(fcstd=valid, source_step_sha256="b" * 64)
    assert artifact.authoritative_geometry is False
    assert artifact.geometry_verified is False

    with pytest.raises(cad.FreeCADImportError, match="valid FCStd"):
        cad.FreeCADProjectArtifacts(fcstd=b"not-a-zip", source_step_sha256="b" * 64)
    missing_document = io.BytesIO()
    with zipfile.ZipFile(missing_document, "w") as archive:
        archive.writestr("Other.xml", "<Other />")
    with pytest.raises(cad.FreeCADImportError, match="Document.xml"):
        cad.FreeCADProjectArtifacts(
            fcstd=missing_document.getvalue(),
            source_step_sha256="b" * 64,
        )
    unsafe_path = io.BytesIO()
    with zipfile.ZipFile(unsafe_path, "w") as archive:
        archive.writestr("Document.xml", "<Document />")
        archive.writestr("../escape", "unsafe")
    with pytest.raises(cad.FreeCADImportError, match="unsafe ZIP path"):
        cad.FreeCADProjectArtifacts(
            fcstd=unsafe_path.getvalue(),
            source_step_sha256="b" * 64,
        )
    with pytest.raises(cad.FreeCADImportError, match="SHA-256"):
        cad.FreeCADProjectArtifacts(fcstd=valid, source_step_sha256="invalid")
    with pytest.raises(cad.FreeCADImportError, match="runtime version"):
        cad.FreeCADProjectArtifacts(
            fcstd=valid,
            source_step_sha256="b" * 64,
            runtime_version="invalid version",
        )
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
    command = _fake_freecad_command(
        tmp_path / "fake-freecad",
        _geometry_verification_bytes(),
        vary_zip_metadata=True,
    )
    bridge = cad.FreeCADProjectBridge(command=command)

    result = bridge.convert_authoritative_step(
        STEP,
        DESIGN_HASH,
        metadata={"template": "bookcase", "revision": "1"},
    )
    repeated = bridge.convert_authoritative_step(
        STEP,
        DESIGN_HASH,
        metadata={"template": "bookcase", "revision": "1"},
    )

    assert result.fcstd.startswith(b"PK")
    assert result.fcstd == repeated.fcstd
    assert result.runtime_version == "0.20.2"
    assert result.source_step_sha256 == hashlib.sha256(STEP).hexdigest()
    assert result.authoritative_geometry is False
    assert result.geometry_verified is True
    assert result.geometry_verification == _geometry_verification_bytes()
    assert (
        result.geometry_verification_sha256
        == hashlib.sha256(_geometry_verification_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    ("reopened", "message"),
    [
        (_geometry_fingerprint(solid_count=1, volumes=["3000"]), "solid count"),
        (_geometry_fingerprint(x_max="300.00002"), "bounds differ"),
        (_geometry_fingerprint(volumes=["1000", "2000.01"]), "volumes differ"),
    ],
)
def test_bridge_blocks_reopened_geometry_mismatches(
    tmp_path: Path,
    reopened: dict[str, object],
    message: str,
) -> None:
    evidence = _geometry_verification_bytes(reopened=reopened)
    command = _fake_freecad_command(tmp_path / f"freecad-mismatch-{message}", evidence)

    with pytest.raises(cad.FreeCADImportError, match=message):
        cad.FreeCADProjectBridge(command=command).convert_authoritative_step(STEP, DESIGN_HASH)


def test_bridge_accepts_only_differences_inside_explicit_tolerances(tmp_path: Path) -> None:
    evidence = _geometry_verification_bytes(
        reopened=_geometry_fingerprint(
            x_max="300.000009",
            volumes=["1000", "2000.0005"],
        )
    )
    command = _fake_freecad_command(tmp_path / "freecad-within-tolerance", evidence)

    result = cad.FreeCADProjectBridge(command=command).convert_authoritative_step(
        STEP,
        DESIGN_HASH,
    )

    assert result.geometry_verified is True


def test_bridge_rejects_unbound_or_noncanonical_geometry_evidence(tmp_path: Path) -> None:
    wrong_source = _geometry_verification_bytes(source_step=b"ISO-10303-21;wrong")
    wrong_source_command = _fake_freecad_command(
        tmp_path / "freecad-wrong-source",
        wrong_source,
    )
    with pytest.raises(cad.FreeCADImportError, match="not bound"):
        cad.FreeCADProjectBridge(command=wrong_source_command).convert_authoritative_step(
            STEP,
            DESIGN_HASH,
        )

    canonical = _geometry_verification_bytes()
    noncanonical = json.dumps(json.loads(canonical), indent=2).encode()
    noncanonical_command = _fake_freecad_command(
        tmp_path / "freecad-noncanonical",
        noncanonical,
    )
    with pytest.raises(cad.FreeCADImportError, match="not canonical"):
        cad.FreeCADProjectBridge(command=noncanonical_command).convert_authoritative_step(
            STEP,
            DESIGN_HASH,
        )


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
    assert status["geometry_probe_required_when_requested"] is True

    monkeypatch.setattr("custombuild_cad.freecad_bridge.shutil.which", lambda command: None)
    assert cad.FreeCADProjectBridge.available() is False
    unavailable = cad.freecad_bridge_status()
    assert unavailable["status"] == "BLOCKED_UNAVAILABLE"
    assert unavailable["authoritative_geometry"] is False
