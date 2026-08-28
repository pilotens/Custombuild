from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from custombuild_manufacturing.physical_release import REQUIRED_PHYSICAL_EVIDENCE_KINDS


def digest(char: str) -> str:
    return char * 64


def payload() -> dict[str, object]:
    return {
        "schema_version": "custombuild.physical-release-evidence.v1",
        "design_sha256": digest("a"),
        "generation_context_sha256": digest("b"),
        "machine_profile_sha256": digest("c"),
        "material_catalog_sha256": digest("d"),
        "records": [
            {
                "evidence_id": f"ev-{kind.value}",
                "kind": kind.value,
                "revision": "1",
                "issuer": "qualified-reviewer",
                "issued_at": "2026-08-28T08:00:00Z",
                "subject_sha256": digest("e"),
                "document_sha256": digest("f"),
                "notes": "",
            }
            for kind in REQUIRED_PHYSICAL_EVIDENCE_KINDS
        ],
    }


def run_cli(path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
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
        digest("a"),
        "--expect-generation-context-sha256",
        digest("b"),
    )
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["status"] == "PASS"
    assert result["physical_cutting_authorized"] is False
    assert result["record_count"] == len(REQUIRED_PHYSICAL_EVIDENCE_KINDS)


def test_cli_blocks_wrong_design_binding(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    source.write_text(json.dumps(payload()), encoding="utf-8")
    completed = run_cli(source, "--expect-design-sha256", digest("9"))
    assert completed.returncode == 1
    result = json.loads(completed.stdout)
    assert result["status"] == "BLOCK"
    assert result["physical_cutting_authorized"] is False
