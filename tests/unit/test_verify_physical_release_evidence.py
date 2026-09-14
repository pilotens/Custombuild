from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from custombuild_manufacturing.physical_release import (
    PhysicalEvidenceKind,
    REQUIRED_PHYSICAL_EVIDENCE_KINDS,
)


def digest(char: str) -> str:
    return char * 64


DESIGN_SHA = digest("a")
GENERATION_SHA = digest("b")
MACHINE_SHA = digest("c")
MATERIAL_SHA = digest("d")


def subject_for(kind: PhysicalEvidenceKind) -> str:
    if kind in {
        PhysicalEvidenceKind.WALL_ANCHOR,
        PhysicalEvidenceKind.CABINET_HARDWARE,
        PhysicalEvidenceKind.JOINT_RETENTION_SYSTEM,
        PhysicalEvidenceKind.PROTOTYPE_BUILD,
        PhysicalEvidenceKind.LOAD_TEST,
        PhysicalEvidenceKind.CNC_OPERATOR_APPROVAL,
        PhysicalEvidenceKind.FURNITURE_CONSTRUCTOR_APPROVAL,
    }:
        return DESIGN_SHA
    if kind in {
        PhysicalEvidenceKind.JOINT_COUPONS,
        PhysicalEvidenceKind.MATERIAL_REMOVAL_COMPARISON,
        PhysicalEvidenceKind.SUPERVISED_AIR_CUT,
        PhysicalEvidenceKind.REFERENCE_PART,
    }:
        return GENERATION_SHA
    if kind in {
        PhysicalEvidenceKind.MACHINE_CALIBRATION,
        PhysicalEvidenceKind.WCS_CONVENTION,
        PhysicalEvidenceKind.MEASURED_TOOLING,
    }:
        return MACHINE_SHA
    return MATERIAL_SHA


def payload() -> dict[str, object]:
    return {
        "schema_version": "custombuild.physical-release-evidence.v1",
        "design_sha256": DESIGN_SHA,
        "generation_context_sha256": GENERATION_SHA,
        "machine_profile_sha256": MACHINE_SHA,
        "material_catalog_sha256": MATERIAL_SHA,
        "edge_band_selection_required": False,
        "records": [
            {
                "evidence_id": f"ev-{kind.value}",
                "kind": kind.value,
                "revision": "1",
                "issuer": "qualified-reviewer",
                "issued_at": "2026-08-28T08:00:00Z",
                "subject_sha256": subject_for(kind),
                "document_sha256": digest("f"),
                "notes": "",
            }
            for kind in REQUIRED_PHYSICAL_EVIDENCE_KINDS
        ],
    }


def run_cli(path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter/script; no shell execution
        [sys.executable, "scripts/verify_physical_release_evidence.py", str(path), *extra],
        check=False,
        capture_output=True,
        text=True,
    )


def test_cli_passes_complete_bound_evidence(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    source.write_text(json.dumps(payload()), encoding="utf-8")
    completed = run_cli(
        source,
        "--expect-design-sha256",
        DESIGN_SHA,
        "--expect-generation-context-sha256",
        GENERATION_SHA,
        "--expect-machine-profile-sha256",
        MACHINE_SHA,
        "--expect-material-catalog-sha256",
        MATERIAL_SHA,
    )
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["status"] == "PASS"
    assert result["physical_release_evidence_complete"] is True
    assert result["physical_cutting_authorized"] is False
    assert result["record_count"] == len(REQUIRED_PHYSICAL_EVIDENCE_KINDS)


def test_cli_blocks_wrong_design_binding(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    source.write_text(json.dumps(payload()), encoding="utf-8")
    completed = run_cli(source, "--expect-design-sha256", digest("9"))
    assert completed.returncode == 1
    result = json.loads(completed.stdout)
    assert result["status"] == "BLOCK"
    assert result["physical_release_evidence_complete"] is False
    assert result["physical_cutting_authorized"] is False


def test_cli_rejects_unexpected_json_fields(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    invalid = payload()
    invalid["browser_authorized"] = True
    source.write_text(json.dumps(invalid), encoding="utf-8")
    completed = run_cli(source)
    assert completed.returncode == 1
    assert "unexpected top-level schema" in json.loads(completed.stdout)["error"]
