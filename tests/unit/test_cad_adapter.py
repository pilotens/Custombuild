from __future__ import annotations

from types import SimpleNamespace

import pytest
from custombuild_cad import (
    CADArtifacts,
    CADDependencyUnavailable,
    CADExportError,
    CadQueryAdapter,
    cad_capability_status,
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


@pytest.mark.cad
def test_real_cadquery_export_produces_step_and_binary_glb() -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
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
