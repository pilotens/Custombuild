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
        manifest_character = "bcdef01"[index]
        image_id = SEAWEED_ID if component == "seaweedfs" else "sha256:" + digest_character * 64
        image_reference = release_evidence_gate._expected_image_reference(component, REVISION)
        scan_input = release_evidence_gate._expected_scan_input(component, REVISION)
        root_name, root_version = scan_input.rsplit(":", 1)
        root_name = root_name.rsplit("/", 1)[-1]
        root_id = f"SPDXRef-DocumentRoot-Image-{root_name}"
        manifest_digest = "sha256:" + manifest_character * 64
        sbom = sboms / f"sbom-{component}.spdx.json"
        packages = [
            {
                "name": root_name,
                "SPDXID": root_id,
                "versionInfo": root_version,
                "checksums": [
                    {
                        "algorithm": "SHA256",
                        "checksumValue": manifest_digest.removeprefix("sha256:"),
                    }
                ],
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": (
                            f"pkg:oci/{root_name}@sha256%3A"
                            f"{manifest_digest.removeprefix('sha256:')}?arch=amd64&tag={root_version}"
                        ),
                    }
                ],
                "primaryPackagePurpose": "CONTAINER",
            },
            {
                "name": "wolfi-baselayout",
                "SPDXID": "SPDXRef-Package-wolfi-baselayout",
                "versionInfo": "20230201-r29",
            },
        ]
        packages.extend(
            {
                "name": name,
                "SPDXID": f"SPDXRef-Package-native-{package_index}",
                "versionInfo": version,
            }
            for package_index, (name, version) in enumerate(
                release_evidence_gate.REQUIRED_NATIVE_PACKAGES.get(component, frozenset())
            )
        )
        _write(
            sbom,
            {
                "spdxVersion": "SPDX-2.3",
                "SPDXID": "SPDXRef-DOCUMENT",
                "name": f"custombuild-{component}",
                "creationInfo": {"creators": ["Organization: Anchore", "Tool: syft-1.42.1"]},
                "packages": packages,
                "relationships": [
                    {
                        "spdxElementId": "SPDXRef-DOCUMENT",
                        "relatedSpdxElement": root_id,
                        "relationshipType": "DESCRIBES",
                    }
                ],
            },
        )
        scan = sboms / f"scan-{component}.json"
        _write(
            scan,
            {
                "descriptor": {
                    "name": "grype",
                    "version": "0.110.0",
                    "configuration": {"output": ["json"], "fail-on-severity": "high"},
                    "db": {"status": {"schemaVersion": "v6.1.9", "valid": True}},
                },
                "source": {
                    "type": "image",
                    "target": {
                        "userInput": scan_input,
                        "imageID": root_id.removeprefix("SPDXRef-"),
                        "manifestDigest": manifest_digest,
                    },
                },
                "matches": [
                    {
                        "vulnerability": {"id": "CVE-EXAMPLE", "severity": "Medium"},
                        "artifact": {
                            "id": "Package-wolfi-baselayout",
                            "name": "wolfi-baselayout",
                            "version": "20230201-r29",
                        },
                    }
                ],
                "ignoredMatches": None,
            },
        )
        images.append(
            {
                "component": component,
                "image_id": image_id,
                "image_reference": image_reference,
                "manifest_digest": manifest_digest,
                "scan_input": scan_input,
                "sbom_sha256": hashlib.sha256(sbom.read_bytes()).hexdigest(),
                "scan_status": "PASS",
                "scan_sha256": hashlib.sha256(scan.read_bytes()).hexdigest(),
            }
        )
    _write(
        runtime,
        {
            "schema_version": "custombuild.runtime-release-evidence.v3",
            "git_revision": REVISION,
            "source_manifest_sha256": SOURCE,
            "status": "PASS",
            "images": images,
        },
    )
    return static, backup, restore, runtime, sboms


def _stub_source_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_evidence_gate, "_assert_clean_repository", lambda _repo: None)
    monkeypatch.setattr(release_evidence_gate, "_git_revision", lambda _repo: REVISION)
    monkeypatch.setattr(
        release_evidence_gate, "build_source_manifest", lambda _repo: ({}, b"", SOURCE)
    )


def _rehash_runtime_file(runtime: Path, component: str, field: str, path: Path) -> None:
    payload = json.loads(runtime.read_text(encoding="utf-8"))
    next(item for item in payload["images"] if item["component"] == component)[field] = (
        hashlib.sha256(path.read_bytes()).hexdigest()
    )
    _write(runtime, payload)


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
    assert report["runtime_image_references"]["api"] == f"custombuild-api:{REVISION}"
    assert report["runtime_scan_inputs"]["postgres"] == "cgr.dev/chainguard/postgres:latest"
    assert set(report["runtime_sbom_sha256"]) == release_evidence_gate.REQUIRED_IMAGES
    assert set(report["runtime_scan_sha256"]) == release_evidence_gate.REQUIRED_IMAGES
    assert set(report["runtime_manifest_digests"]) == release_evidence_gate.REQUIRED_IMAGES


def test_runtime_evidence_v3_names_the_volume_init_image_by_its_role() -> None:
    assert release_evidence_gate.RUNTIME_SCHEMA == "custombuild.runtime-release-evidence.v3"
    assert (
        frozenset({"api", "worker", "web", "seaweedfs", "postgres", "redis", "volume-init"})
        == release_evidence_gate.REQUIRED_IMAGES
    )
    assert release_evidence_gate._expected_image_reference("postgres", REVISION).endswith(
        "@sha256:3af67abef0353ec61f054acf649abb5eaaae9742a9c1c9125e073c7833736060"
    )
    assert (
        release_evidence_gate._expected_scan_input("postgres", REVISION)
        == "cgr.dev/chainguard/postgres:latest"
    )
    assert len(release_evidence_gate.REQUIRED_NATIVE_PACKAGES["worker"]) == 49
    assert ("mesa-gl", "26.2.1-r0") in release_evidence_gate.REQUIRED_NATIVE_PACKAGES["worker"]
    assert ("libxcb", "1.17.0-r15") in release_evidence_gate.REQUIRED_NATIVE_PACKAGES["worker"]


def test_final_gate_rejects_a_scan_from_another_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static, backup, restore, runtime, sboms = _evidence(tmp_path)
    scan = sboms / "scan-seaweedfs.json"
    scan_payload = json.loads(scan.read_text(encoding="utf-8"))
    scan_payload["source"]["target"]["imageID"] = "sha256:" + "9" * 64
    _write(scan, scan_payload)
    payload = json.loads(runtime.read_text(encoding="utf-8"))
    next(item for item in payload["images"] if item["component"] == "seaweedfs")["scan_sha256"] = (
        hashlib.sha256(scan.read_bytes()).hexdigest()
    )
    _write(runtime, payload)
    monkeypatch.setattr(release_evidence_gate, "_assert_clean_repository", lambda _repo: None)
    monkeypatch.setattr(release_evidence_gate, "_git_revision", lambda _repo: REVISION)
    monkeypatch.setattr(
        release_evidence_gate, "build_source_manifest", lambda _repo: ({}, b"", SOURCE)
    )

    with pytest.raises(EvidenceError, match="not bound to its exact SBOM target"):
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
        "missing",
        "extra",
        "tampered",
        "invalid_json",
        "not_grype",
        "not_image",
        "high",
        "critical",
        "ignored",
        "invalid_ignored",
        "invalid_match",
        "fabricated",
        "wrong_manifest",
        "wrong_input",
        "foreign_match",
    ),
)
def test_final_gate_parses_and_rejects_invalid_runtime_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    static, backup, restore, runtime, sboms = _evidence(tmp_path)
    scan = sboms / "scan-api.json"
    if case == "missing":
        scan.unlink()
    elif case == "extra":
        _write(sboms / "scan-unexpected.json", {})
    elif case == "invalid_json":
        scan.write_text("not JSON", encoding="utf-8")
    else:
        payload = json.loads(scan.read_text(encoding="utf-8"))
        if case == "tampered":
            payload["unbound_mutation"] = True
        elif case == "not_grype":
            payload["descriptor"]["name"] = "another-scanner"
        elif case == "not_image":
            payload["source"]["type"] = "directory"
        elif case in {"high", "critical"}:
            payload["matches"].append({"vulnerability": {"severity": case.capitalize()}})
        elif case == "ignored":
            payload["ignoredMatches"] = [{"vulnerability": {"severity": "Medium"}}]
        elif case == "invalid_ignored":
            payload["ignoredMatches"] = {}
        elif case == "invalid_match":
            payload["matches"].append({"vulnerability": {}})
        elif case == "fabricated":
            payload["descriptor"] = {"name": "grype", "version": "0.110.0"}
            payload["matches"] = []
        elif case == "wrong_manifest":
            payload["source"]["target"]["manifestDigest"] = "sha256:" + "f" * 64
        elif case == "wrong_input":
            payload["source"]["target"]["userInput"] = "custombuild-api:another-revision"
        elif case == "foreign_match":
            payload["matches"][0]["artifact"] = {
                "id": "Package-foreign",
                "name": "foreign",
                "version": "1.0",
            }
        else:  # pragma: no cover - parameter list is exhaustive.
            raise AssertionError(case)
        _write(scan, payload)
        if case != "tampered":
            _rehash_runtime_file(runtime, "api", "scan_sha256", scan)
    _stub_source_identity(monkeypatch)

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
        "wrong_image_reference",
        "wrong_scan_input",
        "wrong_manifest_digest",
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
    payloads = {name: json.loads(path.read_text(encoding="utf-8")) for name, path in paths.items()}
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
    elif case == "wrong_image_reference":
        payloads["runtime"]["images"][0]["image_reference"] = "custombuild-api:another"
    elif case == "wrong_scan_input":
        payloads["runtime"]["images"][0]["scan_input"] = "custombuild-api:another"
    elif case == "wrong_manifest_digest":
        payloads["runtime"]["images"][0]["manifest_digest"] = "sha256:" + "f" * 64
    elif case == "restore_flag":
        payloads["restore"]["tenant_rls_verified"] = False
    elif case == "unsafe_runtime_role":
        payloads["restore"]["database_runtime_roles"]["roles"]["worker_safe"] = False
    elif case == "missing_migrator_mutation":
        payloads["restore"]["database_runtime_roles"]["migrator_schema_mutation_verified"] = False
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


@pytest.mark.parametrize(
    "case",
    (
        "missing",
        "extra",
        "tampered",
        "invalid_spdx",
        "missing_creator",
        "not_syft",
        "empty_packages",
        "missing_root",
        "not_container",
        "wrong_root_identity",
        "wrong_manifest_checksum",
        "wrong_oci_purl",
    ),
)
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
    elif case in {
        "missing_creator",
        "not_syft",
        "empty_packages",
        "missing_root",
        "not_container",
        "wrong_root_identity",
        "wrong_manifest_checksum",
        "wrong_oci_purl",
    }:
        payload = json.loads(api_sbom.read_text(encoding="utf-8"))
        root_id = payload["relationships"][0]["relatedSpdxElement"]
        root = next(package for package in payload["packages"] if package["SPDXID"] == root_id)
        if case == "missing_creator":
            payload.pop("creationInfo")
        elif case == "not_syft":
            payload["creationInfo"]["creators"] = ["Tool: another-scanner-1.0"]
        elif case == "empty_packages":
            payload["packages"] = []
        elif case == "missing_root":
            payload["relationships"] = []
        elif case == "not_container":
            root["primaryPackagePurpose"] = "APPLICATION"
        elif case == "wrong_root_identity":
            root["name"] = "another-image"
        elif case == "wrong_manifest_checksum":
            root["checksums"][0]["checksumValue"] = "f" * 64
        else:
            root["externalRefs"][0]["referenceLocator"] = (
                "pkg:oci/another-image@sha256%3A"
                + root["checksums"][0]["checksumValue"]
                + f"?tag={REVISION}"
            )
        _write(api_sbom, payload)
        _rehash_runtime_file(runtime, "api", "sbom_sha256", api_sbom)
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


@pytest.mark.parametrize(
    ("component", "package_name"),
    (
        ("api", "python-3.13"),
        ("worker", "python-3.13"),
        ("web", "nodejs-24-minimal"),
    ),
)
def test_final_gate_rejects_wrong_native_interpreter_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
    package_name: str,
) -> None:
    static, backup, restore, runtime, sboms = _evidence(tmp_path)
    sbom = sboms / f"sbom-{component}.spdx.json"
    payload = json.loads(sbom.read_text(encoding="utf-8"))
    package = next(item for item in payload["packages"] if item["name"] == package_name)
    package["versionInfo"] = "0.0.0-r0"
    _write(sbom, payload)
    _rehash_runtime_file(runtime, component, "sbom_sha256", sbom)
    _stub_source_identity(monkeypatch)

    with pytest.raises(EvidenceError, match="exact native runtime closure"):
        build_final_report(
            tmp_path,
            static_report_path=static,
            backup_directory=backup,
            restore_evidence_path=restore,
            runtime_evidence_path=runtime,
            sbom_directory=sboms,
        )


@pytest.mark.parametrize(
    "package_name",
    (
        "mesa-gl",
        "libx11",
        "libxau",
        "libxdmcp",
        "libbsd",
        "libmd",
        "libgomp",
        "libxcb",
        "libLLVM-22",
        "wayland-libs-client",
    ),
)
def test_final_gate_rejects_missing_worker_native_cad_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package_name: str,
) -> None:
    static, backup, restore, runtime, sboms = _evidence(tmp_path)
    sbom = sboms / "sbom-worker.spdx.json"
    payload = json.loads(sbom.read_text(encoding="utf-8"))
    payload["packages"] = [
        package for package in payload["packages"] if package["name"] != package_name
    ]
    _write(sbom, payload)
    _rehash_runtime_file(runtime, "worker", "sbom_sha256", sbom)
    _stub_source_identity(monkeypatch)

    with pytest.raises(EvidenceError, match="exact native runtime closure"):
        build_final_report(
            tmp_path,
            static_report_path=static,
            backup_directory=backup,
            restore_evidence_path=restore,
            runtime_evidence_path=runtime,
            sbom_directory=sboms,
        )


@pytest.mark.parametrize(
    ("component", "foreign_package", "foreign_version"),
    (
        ("api", "python-3.14", "3.14.2-r0"),
        ("worker", "python-3.14-base", "3.14.2-r0"),
        ("web", "nodejs-26-minimal", "26.0.0-r0"),
    ),
)
def test_final_gate_rejects_another_native_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
    foreign_package: str,
    foreign_version: str,
) -> None:
    static, backup, restore, runtime, sboms = _evidence(tmp_path)
    sbom = sboms / f"sbom-{component}.spdx.json"
    payload = json.loads(sbom.read_text(encoding="utf-8"))
    payload["packages"].append(
        {
            "name": foreign_package,
            "SPDXID": "SPDXRef-Package-foreign-runtime",
            "versionInfo": foreign_version,
        }
    )
    _write(sbom, payload)
    _rehash_runtime_file(runtime, component, "sbom_sha256", sbom)
    _stub_source_identity(monkeypatch)

    with pytest.raises(EvidenceError, match="another (?:Python|Node.js) runtime"):
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
    compose = Path("compose.yml").read_text(encoding="utf-8")
    gitignore = Path(".gitignore").read_text(encoding="utf-8").splitlines()

    assert "artifacts/" in gitignore
    assert "RELEASE_EVIDENCE_DIR: artifacts/release-evidence" in workflow
    assert workflow.count("uses: anchore/sbom-action@") == len(
        release_evidence_gate.REQUIRED_IMAGES
    )
    assert workflow.count("output-file: artifacts/release-evidence/sbom-") == len(
        release_evidence_gate.REQUIRED_IMAGES
    )
    assert '--arg schema_version "custombuild.runtime-release-evidence.v3"' in workflow
    assert workflow.count("image_reference:$") == len(release_evidence_gate.REQUIRED_IMAGES)
    assert workflow.count("scan_input:$") == len(release_evidence_gate.REQUIRED_IMAGES)
    assert workflow.count("manifest_digest:$") == len(release_evidence_gate.REQUIRED_IMAGES)
    for image_reference in release_evidence_gate.EXTERNAL_IMAGE_REFERENCES.values():
        assert image_reference in compose
        assert image_reference in workflow
    assert '--static-report "$RELEASE_EVIDENCE_DIR/static-release-readiness.json"' in workflow
    assert '--runtime-evidence "$RELEASE_EVIDENCE_DIR/runtime-evidence.json"' in workflow
    assert '--sbom-directory "$RELEASE_EVIDENCE_DIR"' in workflow
    assert '--output "$RELEASE_EVIDENCE_DIR/release-readiness.json"' in workflow
