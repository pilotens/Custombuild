from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.verify_production_platform_evidence import REQUIRED_CONTROLS, validate


def digest(char: str) -> str:
    return char * 64


def evidence_payload() -> dict[str, object]:
    return {
        "schema_version": "custombuild.production-platform-evidence.v1",
        "environment_id": "prod-eu-1",
        "git_revision": digest("a"),
        "deploy_descriptor_sha256": digest("b"),
        "verified_at": "2026-08-28T09:00:00Z",
        "verified_by": "production-owner",
        "controls": [
            {
                "code": code,
                "evidence_id": f"evidence-{index}",
                "subject_sha256": digest("c"),
                "document_sha256": f"{index:064x}",
                "issuer": "platform-control",
                "issued_at": "2026-08-28T09:00:00Z",
            }
            for index, code in enumerate(REQUIRED_CONTROLS, start=1)
        ],
    }


def run_cli(path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter/script; no shell execution
        [sys.executable, "scripts/verify_production_platform_evidence.py", str(path), *extra],
        check=False,
        capture_output=True,
        text=True,
    )


def test_complete_platform_evidence_passes_deterministically() -> None:
    first = validate(evidence_payload())
    second = validate(evidence_payload())
    assert first == second
    assert first["status"] == "PASS"
    assert first["internet_production_evidence_complete"] is True
    assert first["deployment_performed"] is False
    assert first["control_count"] == len(REQUIRED_CONTROLS)


def test_missing_platform_control_blocks() -> None:
    payload = evidence_payload()
    controls = payload["controls"]
    assert isinstance(controls, list)
    payload["controls"] = controls[:-1]
    try:
        validate(payload)
    except ValueError as exc:
        assert "missing production platform controls" in str(exc)
    else:
        raise AssertionError("missing production control unexpectedly passed")


def test_duplicate_evidence_id_blocks() -> None:
    payload = evidence_payload()
    controls = payload["controls"]
    assert isinstance(controls, list)
    first = controls[0]
    second = controls[1]
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    second["evidence_id"] = first["evidence_id"]
    try:
        validate(payload)
    except ValueError as exc:
        assert "evidence IDs must be unique" in str(exc)
    else:
        raise AssertionError("duplicate evidence ID unexpectedly passed")


def test_cli_binds_evidence_to_exact_release(tmp_path: Path) -> None:
    path = tmp_path / "platform.json"
    path.write_text(json.dumps(evidence_payload()), encoding="utf-8")
    completed = run_cli(
        path,
        "--expect-git-revision",
        digest("a"),
        "--expect-deploy-descriptor-sha256",
        digest("b"),
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["status"] == "PASS"


def test_cli_blocks_other_release_binding(tmp_path: Path) -> None:
    path = tmp_path / "platform.json"
    path.write_text(json.dumps(evidence_payload()), encoding="utf-8")
    completed = run_cli(path, "--expect-git-revision", digest("f"))
    assert completed.returncode == 1
    result = json.loads(completed.stdout)
    assert result["status"] == "BLOCK"
    assert result["internet_production_evidence_complete"] is False
