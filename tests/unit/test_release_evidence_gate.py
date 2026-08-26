from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import release_evidence_gate
from scripts.compose_backup import MANIFEST_SCHEMA, SEAWEEDFS_IMAGE, build_manifest
from scripts.release_evidence_gate import EvidenceError, build_final_report

REVISION = "1" * 40
SOURCE = "2" * 64
SEAWEED_ID = "sha256:" + "3" * 64


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _evidence(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    static = tmp_path / "static.json"
    restore = tmp_path / "restore.json"
    runtime = tmp_path / "runtime.json"
    backup = tmp_path / "backup"
    backup.mkdir()
    sboms = tmp_path / "sboms"
    sboms.mkdir()
    (backup / "database.dump").write_bytes(b"database")
    (backup / "artifacts.tar").write_bytes(b"objects")
    database_snapshot = {
        "captured_at": "2026-08-26T10:00:00+00:00",
        "wal_lsn": "0/16B6C50",
        "alembic_heads": ["0010_tenant_fk"],
        "row_counts": {"alembic_version": 1, "projects": 2},
    }
    manifest = build_manifest(
        backup,
        {
            "git_revision": REVISION,
            "source_manifest_sha256": SOURCE,
            "database_snapshot": database_snapshot,
            "object_store": {
                "image": SEAWEEDFS_IMAGE,
                "image_id": SEAWEED_ID,
                "bucket": "artifacts",
                "object_count": 0,
                "total_size_bytes": 0,
                "objects": [],
            },
        },
    )
    _write(backup / "manifest.json", manifest)
    _write(
        static,
        {
            "schema_version": "custombuild.release-readiness-static.v2",
            "git_revision": REVISION,
            "source_manifest_sha256": SOURCE,
            "static_controls_ready": True,
            "software_release_ready": False,
            "runtime_evidence_required": True,
        },
    )
    _write(
        restore,
        {
            "schema_version": "custombuild.restore-drill.v3",
            "status": "PASS",
            "git_revision": REVISION,
            "source_manifest_sha256": SOURCE,
            "backup_created_at": manifest["created_at"],
            "database_snapshot": database_snapshot,
            "database_alembic_heads": database_snapshot["alembic_heads"],
            "database_project_rows": database_snapshot["row_counts"]["projects"],
            "database_exact_row_counts_verified": True,
            "database_runtime_roles": {
                "roles": {
                    "migrator_safe": True,
                    "api_safe": True,
                    "worker_safe": True,
                    "memberships_absent": True,
                    "all_public_objects_owned_by_migrator": True,
                },
                "api_rls": {"visible": 2, "foreign": 0},
                "worker_rls": {"visible": 2, "foreign": 0},
                "migrator_schema_mutation_verified": True,
            },
            "tenant_rls_verified": True,
            "object_store_image": SEAWEEDFS_IMAGE,
            "object_store_image_id": SEAWEED_ID,
            "object_store_bucket": "artifacts",
            "object_store_object_count": 0,
            "object_store_total_size_bytes": 0,
            "object_store_hashes_verified": True,
            "object_store_metadata_verified": True,
            "tenant_acceptance_required_before_traffic": False,
        },
    )
    images = []
    for index, component in enumerate(sorted(release_evidence_gate.REQUIRED_IMAGES)):
        digest_character = "456789a"[index]
        sbom = sboms / f"sbom-{component}.spdx.json"
        _write(
            sbom,
            {
                "spdxVersion": "SPDX-2.3",
                "SPDXID": "SPDXRef-DOCUMENT",
                "name": f"custombuild-{component}",
            },
        )
        images.append(
            {
                "component": component,
                "image_id": (
                    SEAWEED_ID
                    if component == "seaweedfs"
                    else "sha256:" + digest_character * 64
                ),
                "sbom_sha256": hashlib.sha256(sbom.read_bytes()).hexdigest(),
                "scan_status": "PASS",
                "scan_sha256": digest_character * 64,
            }
        )
    _write(
        runtime,
        {
            "schema_version": "custombuild.runtime-release-evidence.v1",
            "git_revision": REVISION,
            "source_manifest_sha256": SOURCE,
            "status": "PASS",
            "images": images,
        },
    )
    return static, backup, restore, runtime, sboms


def test_final_gate_binds_all_release_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static, backup, restore, runtime, sboms = _evidence(tmp_path)
    monkeypatch.setattr(release_evidence_gate, "_assert_clean_repository", lambda _repo: None)
    monkeypatch.setattr(release_evidence_gate, "_git_revision", lambda _repo: REVISION)
    monkeypatch.setattr(
        release_evidence_gate, "build_source_manifest", lambda _repo: ({}, b"", SOURCE)
    )

    report = build_final_report(
        tmp_path,
        static_report_path=static,
        backup_directory=backup,
        restore_evidence_path=restore,
        runtime_evidence_path=runtime,
        sbom_directory=sboms,
    )

    assert report["software_release_ready"] is True
    assert report["backup_manifest_schema"] == MANIFEST_SCHEMA
    assert report["runtime_image_ids"]["seaweedfs"] == SEAWEED_ID
    assert set(report["runtime_sbom_sha256"]) == release_evidence_gate.REQUIRED_IMAGES


def test_final_gate_rejects_a_scan_from_another_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static, backup, restore, runtime, sboms = _evidence(tmp_path)
    payload = json.loads(runtime.read_text(encoding="utf-8"))
    next(item for item in payload["images"] if item["component"] == "seaweedfs")[
        "image_id"
    ] = "sha256:" + "9" * 64
    _write(runtime, payload)
    monkeypatch.setattr(release_evidence_gate, "_assert_clean_repository", lambda _repo: None)
    monkeypatch.setattr(release_evidence_gate, "_git_revision", lambda _repo: REVISION)
    monkeypatch.setattr(
        release_evidence_gate, "build_source_manifest", lambda _repo: ({}, b"", SOURCE)
    )

    with pytest.raises(EvidenceError, match="SeaweedFS image differs"):
        build_final_report(
            tmp_path,
            static_report_path=static,
            backup_directory=backup,
            restore_evidence_path=restore,
            runtime_evidence_path=runtime,
            sbom_directory=sboms,
        )


@pytest.mark.parametrize(
    "case",
    (
        "static_revision",
        "static_source",
        "backup_revision",
        "backup_source",
        "restore_revision",
        "restore_source",
        "runtime_revision",
        "runtime_source",
        "missing_image",
        "duplicate_image",
        "extra_image",
        "failed_scan",
        "invalid_scan_hash",
        "invalid_sbom_hash",
        "restore_flag",
        "unsafe_runtime_role",
        "missing_migrator_mutation",
        "worker_rls_mismatch",
        "snapshot_mismatch",
        "created_at_mismatch",
    ),
)
def test_final_gate_rejects_cross_source_or_incomplete_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    static, backup, restore, runtime, sboms = _evidence(tmp_path)
    paths = {
        "static": static,
        "backup": backup / "manifest.json",
        "restore": restore,
        "runtime": runtime,
    }
    payloads = {
        name: json.loads(path.read_text(encoding="utf-8"))
        for name, path in paths.items()
    }
    if case.endswith("_revision"):
        payloads[case.removesuffix("_revision")]["git_revision"] = "f" * 40
    elif case.endswith("_source"):
        payloads[case.removesuffix("_source")]["source_manifest_sha256"] = "f" * 64
    elif case == "missing_image":
        payloads["runtime"]["images"].pop()
    elif case == "duplicate_image":
        payloads["runtime"]["images"].append(dict(payloads["runtime"]["images"][0]))
    elif case == "extra_image":
        payloads["runtime"]["images"].append(
            {
                "component": "unexpected",
                "image_id": "sha256:" + "b" * 64,
                "scan_status": "PASS",
                "scan_sha256": "c" * 64,
            }
        )
    elif case == "failed_scan":
        payloads["runtime"]["images"][0]["scan_status"] = "FAIL"
    elif case == "invalid_scan_hash":
        payloads["runtime"]["images"][0]["scan_sha256"] = "not-a-digest"
    elif case == "invalid_sbom_hash":
        payloads["runtime"]["images"][0]["sbom_sha256"] = "not-a-digest"
    elif case == "restore_flag":
        payloads["restore"]["tenant_rls_verified"] = False
    elif case == "unsafe_runtime_role":
        payloads["restore"]["database_runtime_roles"]["roles"]["worker_safe"] = False
    elif case == "missing_migrator_mutation":
        payloads["restore"]["database_runtime_roles"][
            "migrator_schema_mutation_verified"
        ] = False
    elif case == "worker_rls_mismatch":
        payloads["restore"]["database_runtime_roles"]["worker_rls"]["foreign"] = 1
    elif case == "snapshot_mismatch":
        payloads["restore"]["database_snapshot"]["row_counts"]["projects"] = 99
    elif case == "created_at_mismatch":
        payloads["restore"]["backup_created_at"] = "2026-08-26T11:00:00+00:00"
    else:  # pragma: no cover - parameter list is exhaustive.
        raise AssertionError(case)
    for name, path in paths.items():
        _write(path, payloads[name])
    monkeypatch.setattr(release_evidence_gate, "_assert_clean_repository", lambda _repo: None)
    monkeypatch.setattr(release_evidence_gate, "_git_revision", lambda _repo: REVISION)
    monkeypatch.setattr(
        release_evidence_gate, "build_source_manifest", lambda _repo: ({}, b"", SOURCE)
    )

    with pytest.raises(EvidenceError):
        build_final_report(
            tmp_path,
            static_report_path=static,
            backup_directory=backup,
            restore_evidence_path=restore,
            runtime_evidence_path=runtime,
            sbom_directory=sboms,
        )


@pytest.mark.parametrize(
    "status",
    (" M .github/workflows/prod-ci.yml\n", "?? .github/workflows/unreviewed.yml\n"),
)
def test_final_gate_rejects_late_tracked_or_untracked_source_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    monkeypatch.setattr(release_evidence_gate.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(
        release_evidence_gate.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout=status,
            stderr="",
        ),
    )

    with pytest.raises(EvidenceError, match="changed after static"):
        release_evidence_gate._assert_clean_repository(tmp_path)


@pytest.mark.parametrize("case", ("missing", "extra", "tampered", "invalid_spdx"))
def test_final_gate_rejects_incomplete_or_tampered_sbom_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    static, backup, restore, runtime, sboms = _evidence(tmp_path)
    api_sbom = sboms / "sbom-api.spdx.json"
    if case == "missing":
        api_sbom.unlink()
    elif case == "extra":
        _write(
            sboms / "sbom-unexpected.spdx.json",
            {"spdxVersion": "SPDX-2.3", "SPDXID": "SPDXRef-DOCUMENT"},
        )
    elif case == "tampered":
        payload = json.loads(api_sbom.read_text(encoding="utf-8"))
        payload["tampered"] = True
        _write(api_sbom, payload)
    elif case == "invalid_spdx":
        _write(api_sbom, {"spdxVersion": "not-spdx", "SPDXID": "wrong"})
        runtime_payload = json.loads(runtime.read_text(encoding="utf-8"))
        api = next(item for item in runtime_payload["images"] if item["component"] == "api")
        api["sbom_sha256"] = hashlib.sha256(api_sbom.read_bytes()).hexdigest()
        _write(runtime, runtime_payload)
    else:  # pragma: no cover - parameter list is exhaustive.
        raise AssertionError(case)
    monkeypatch.setattr(release_evidence_gate, "_assert_clean_repository", lambda _repo: None)
    monkeypatch.setattr(release_evidence_gate, "_git_revision", lambda _repo: REVISION)
    monkeypatch.setattr(
        release_evidence_gate, "build_source_manifest", lambda _repo: ({}, b"", SOURCE)
    )

    with pytest.raises(EvidenceError):
        build_final_report(
            tmp_path,
            static_report_path=static,
            backup_directory=backup,
            restore_evidence_path=restore,
            runtime_evidence_path=runtime,
            sbom_directory=sboms,
        )


def test_release_evidence_cli_help_smoke() -> None:
    completed = subprocess.run(  # noqa: S603 - fixed local interpreter and script path.
        [sys.executable, "scripts/release_evidence_gate.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0
    assert "--sbom-directory" in completed.stdout


def test_workflow_keeps_generated_evidence_outside_the_clean_source_tree() -> None:
    workflow = Path(".github/workflows/prod-ci.yml").read_text(encoding="utf-8")
    gitignore = Path(".gitignore").read_text(encoding="utf-8").splitlines()

    assert "artifacts/" in gitignore
    assert "RELEASE_EVIDENCE_DIR: artifacts/release-evidence" in workflow
    assert workflow.count("uses: anchore/sbom-action@") == len(
        release_evidence_gate.REQUIRED_IMAGES
    )
    assert workflow.count("output-file: artifacts/release-evidence/sbom-") == len(
        release_evidence_gate.REQUIRED_IMAGES
    )
    assert '--static-report "$RELEASE_EVIDENCE_DIR/static-release-readiness.json"' in workflow
    assert '--runtime-evidence "$RELEASE_EVIDENCE_DIR/runtime-evidence.json"' in workflow
    assert '--sbom-directory "$RELEASE_EVIDENCE_DIR"' in workflow
    assert '--output "$RELEASE_EVIDENCE_DIR/release-readiness.json"' in workflow
