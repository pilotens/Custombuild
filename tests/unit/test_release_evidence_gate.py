from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from scripts import release_evidence_gate
from scripts.compose_backup import MANIFEST_SCHEMA, SEAWEEDFS_IMAGE, build_manifest
from scripts.release_evidence_gate import EvidenceError, build_final_report

REVISION = "1" * 40
SOURCE = "2" * 64
SEAWEED_ID = "sha256:" + "3" * 64
EXTERNAL_SBOM_IDENTITIES = {
    "postgres": (
        "cgr.dev/chainguard/postgres",
        "sha256:3af67abef0353ec61f054acf649abb5eaaae9742a9c1c9125e073c7833736060",
        "latest",
    ),
    "redis": ("redis", "7.2.15-alpine", "7.2.15-alpine"),
    "volume-init": (
        "cgr.dev/chainguard/busybox",
        "sha256:928939fc7f20750dea03366627d83bfa497df565fcf6b55fdddb004ecd8426d6",
        "latest",
    ),
}
EXTERNAL_RUNTIME_IDENTITIES = {
    "postgres": (
        "sha256:090173413bfc70ef815772bbaaafcc5dd14b24fa51a96d50e9de8199cc512786",
        "sha256:4c182473bc16ff3c0d11b6c7cec116889c65f93162540f72a90ad0b1b957fba9",
    ),
    "redis": (
        "sha256:305eb88302bd3271bb2cb79f16c334da746511fb308bf5cdc36bced178d215d8",
        "sha256:86a6ce875fe0a233e015f09c2b6dacd9e30e6074499e9ee715f2dafeb902e872",
    ),
    "volume-init": (
        "sha256:93007eb2eb686f408f418ce3a2aa44a638392fd1e2b65a73082c6a81608f4e31",
        "sha256:1be7e1b6cadc639cd9652438beec21db56a402a2244f6fd1262f980530f54fb8",
    ),
}


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _evidence(
    tmp_path: Path,
    *,
    pinned_registry_inputs: bool = False,
) -> tuple[Path, Path, Path, Path, Path]:
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
                    "public_object_grants_absent": True,
                    "all_public_objects_owned_by_migrator": True,
                },
                "api_rls": {"visible": 2, "foreign": 0},
                "worker_rls": {"visible": 1, "foreign": 0},
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
            "tenant_acceptance_required_before_traffic": True,
        },
    )
    images = []
    for index, component in enumerate(sorted(release_evidence_gate.REQUIRED_IMAGES)):
        digest_character = "456789a"[index]
        manifest_character = "bcdef01"[index]
        image_id = (
            EXTERNAL_RUNTIME_IDENTITIES[component][0]
            if component in EXTERNAL_RUNTIME_IDENTITIES
            else SEAWEED_ID
            if component == "seaweedfs"
            else "sha256:" + digest_character * 64
        )
        image_reference = release_evidence_gate._expected_image_reference(component, REVISION)
        scan_input = release_evidence_gate._expected_scan_input(
            component,
            REVISION,
            pinned_registry_inputs=pinned_registry_inputs,
        )
        if component in EXTERNAL_SBOM_IDENTITIES:
            root_name, root_version, purl_tag = EXTERNAL_SBOM_IDENTITIES[component]
        else:
            root_name = release_evidence_gate.BUILT_IMAGE_NAMES[component]
            root_version = REVISION
            purl_tag = REVISION
        root_id = f"SPDXRef-DocumentRoot-Image-{root_name.replace('/', '-')}"
        manifest_digest = (
            EXTERNAL_RUNTIME_IDENTITIES[component][1]
            if component in EXTERNAL_RUNTIME_IDENTITIES
            else "sha256:" + manifest_character * 64
        )
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
                            f"pkg:oci/{quote(root_name, safe='')}@sha256%3A"
                            f"{manifest_digest.removeprefix('sha256:')}?arch=amd64&tag={purl_tag}"
                        ),
                    }
                ],
                "primaryPackagePurpose": "CONTAINER",
            },
            {
                "name": "wolfi-baselayout",
                "SPDXID": "SPDXRef-Package-apk-wolfi-baselayout-0123456789abcdef",
                "versionInfo": "20230201-r29",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": (
                            "pkg:apk/wolfi/wolfi-baselayout@20230201-r29?arch=x86_64"
                        ),
                    }
                ],
            },
            {
                "name": "python",
                "SPDXID": "SPDXRef-Package-binary-python-1111222233334444",
                "versionInfo": "3.13.15",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": "pkg:generic/python@3.13.15",
                    }
                ],
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
                        "imageID": image_id,
                        "manifestDigest": manifest_digest,
                    },
                },
                "matches": [
                    {
                        "vulnerability": {"id": "CVE-EXAMPLE", "severity": "Medium"},
                        "artifact": {
                            "id": "0123456789abcdef",
                            "name": "wolfi-baselayout",
                            "version": "20230201-r29",
                            "type": "apk",
                            "purl": "pkg:apk/wolfi/wolfi-baselayout@20230201-r29?arch=x86_64",
                        },
                    },
                    {
                        "vulnerability": {"id": "CVE-BINARY-EXAMPLE", "severity": "Low"},
                        "artifact": {
                            "id": "1111222233334444",
                            "name": "python",
                            "version": "3.13.15",
                            "type": "binary",
                            "purl": "pkg:generic/python@3.13.15",
                        },
                    },
                ],
                "ignoredMatches": None,
            },
        )
        runtime_image = {
                "component": component,
                "image_id": image_id,
                "image_reference": image_reference,
                "manifest_digest": manifest_digest,
                "scan_input": scan_input,
                "sbom_sha256": hashlib.sha256(sbom.read_bytes()).hexdigest(),
                "scan_status": "PASS",
                "scan_sha256": hashlib.sha256(scan.read_bytes()).hexdigest(),
            }
        if pinned_registry_inputs and component in EXTERNAL_RUNTIME_IDENTITIES:
            runtime_image["registry_resolution"] = {
                "deployment_reference_digest": image_reference.rsplit("@", 1)[1],
                "runtime_platform_manifest_digest": manifest_digest,
                "image_config_digest": image_id,
            }
        images.append(runtime_image)
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
    assert report["runtime_scan_inputs"]["postgres"] == (
        "cgr.dev/chainguard/postgres:latest@"
        "sha256:3af67abef0353ec61f054acf649abb5eaaae9742a9c1c9125e073c7833736060"
    )
    assert set(report["runtime_sbom_sha256"]) == release_evidence_gate.REQUIRED_IMAGES
    assert set(report["runtime_scan_sha256"]) == release_evidence_gate.REQUIRED_IMAGES
    assert set(report["runtime_manifest_digests"]) == release_evidence_gate.REQUIRED_IMAGES


def test_final_gate_separates_pinned_deployment_platform_and_config_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    static, backup, restore, runtime, sboms = _evidence(
        tmp_path,
        pinned_registry_inputs=True,
    )
    _stub_source_identity(monkeypatch)

    report = build_final_report(
        tmp_path,
        static_report_path=static,
        backup_directory=backup,
        restore_evidence_path=restore,
        runtime_evidence_path=runtime,
        sbom_directory=sboms,
        pinned_registry_inputs=True,
    )

    for component, reference in release_evidence_gate.EXTERNAL_IMAGE_REFERENCES.items():
        resolution = report["runtime_registry_resolutions"][component]
        assert report["runtime_scan_inputs"][component] == reference
        assert resolution["deployment_reference_digest"] == reference.rsplit("@", 1)[1]
        assert resolution["runtime_platform_manifest_digest"] == (
            report["runtime_platform_manifest_digests"][component]
        )
        assert resolution["image_config_digest"] == (
            report["runtime_image_config_digests"][component]
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("deployment_reference_digest", f"sha256:{'1' * 64}"),
        ("runtime_platform_manifest_digest", f"sha256:{'2' * 64}"),
        ("image_config_digest", f"sha256:{'3' * 64}"),
    ),
)
def test_pinned_registry_resolution_mutations_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: str,
) -> None:
    static, backup, restore, runtime, sboms = _evidence(
        tmp_path,
        pinned_registry_inputs=True,
    )
    payload = json.loads(runtime.read_text(encoding="utf-8"))
    redis = next(item for item in payload["images"] if item["component"] == "redis")
    redis["registry_resolution"][field] = replacement
    _write(runtime, payload)
    _stub_source_identity(monkeypatch)

    with pytest.raises(EvidenceError):
        build_final_report(
            tmp_path,
            static_report_path=static,
            backup_directory=backup,
            restore_evidence_path=restore,
            runtime_evidence_path=runtime,
            sbom_directory=sboms,
            pinned_registry_inputs=True,
        )


def test_runtime_evidence_v3_names_the_volume_init_image_by_its_role() -> None:
    assert release_evidence_gate.RUNTIME_SCHEMA == "custombuild.runtime-release-evidence.v3"
    assert (
        frozenset({"api", "worker", "web", "seaweedfs", "postgres", "redis", "volume-init"})
        == release_evidence_gate.REQUIRED_IMAGES
    )
    assert release_evidence_gate._expected_image_reference("postgres", REVISION).endswith(
        "@sha256:3af67abef0353ec61f054acf649abb5eaaae9742a9c1c9125e073c7833736060"
    )
    assert release_evidence_gate._expected_scan_input(
        "postgres", REVISION
    ) == release_evidence_gate._expected_image_reference("postgres", REVISION)
    assert release_evidence_gate._expected_scan_input(
        "postgres", REVISION, pinned_registry_inputs=True
    ) == release_evidence_gate._expected_image_reference("postgres", REVISION)
    for component in ("postgres", "redis", "volume-init"):
        assert "@sha256:" in release_evidence_gate._expected_scan_input(component, REVISION)
    assert len(release_evidence_gate.REQUIRED_NATIVE_PACKAGES["worker"]) == 49
    assert ("mesa-gl", "26.2.1-r0") in release_evidence_gate.REQUIRED_NATIVE_PACKAGES["worker"]
    assert ("libxcb", "1.17.0-r15") in release_evidence_gate.REQUIRED_NATIVE_PACKAGES["worker"]


@pytest.mark.parametrize("component", ("postgres", "redis", "volume-init"))
def test_final_gate_rejects_tag_only_external_scan_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    static, backup, restore, runtime, sboms = _evidence(tmp_path)
    payload = json.loads(runtime.read_text(encoding="utf-8"))
    image = next(item for item in payload["images"] if item["component"] == component)
    image["scan_input"] = str(image["scan_input"]).partition("@")[0]
    _write(runtime, payload)
    _stub_source_identity(monkeypatch)

    with pytest.raises(
        EvidenceError,
        match=rf"runtime image {component} has another scan input",
    ):
        build_final_report(
            tmp_path,
            static_report_path=static,
            backup_directory=backup,
            restore_evidence_path=restore,
            runtime_evidence_path=runtime,
            sbom_directory=sboms,
        )


@pytest.mark.parametrize("component", ("postgres", "redis", "volume-init"))
def test_final_gate_rejects_registry_selector_as_persisted_scan_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    static, backup, restore, runtime, sboms = _evidence(
        tmp_path,
        pinned_registry_inputs=True,
    )
    payload = json.loads(runtime.read_text(encoding="utf-8"))
    image = next(item for item in payload["images"] if item["component"] == component)
    image["scan_input"] = f"registry:{image['scan_input']}"
    _write(runtime, payload)
    _stub_source_identity(monkeypatch)

    with pytest.raises(
        EvidenceError,
        match=rf"runtime image {component} has another scan input",
    ):
        build_final_report(
            tmp_path,
            static_report_path=static,
            backup_directory=backup,
            restore_evidence_path=restore,
            runtime_evidence_path=runtime,
            sbom_directory=sboms,
            pinned_registry_inputs=True,
        )


@pytest.mark.parametrize("component", ("postgres", "redis", "volume-init"))
def test_final_gate_rejects_tag_only_external_image_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    static, backup, restore, runtime, sboms = _evidence(tmp_path)
    payload = json.loads(runtime.read_text(encoding="utf-8"))
    image = next(item for item in payload["images"] if item["component"] == component)
    image["image_reference"] = str(image["image_reference"]).partition("@")[0]
    _write(runtime, payload)
    _stub_source_identity(monkeypatch)

    with pytest.raises(
        EvidenceError,
        match=rf"runtime image {component} has another image reference",
    ):
        build_final_report(
            tmp_path,
            static_report_path=static,
            backup_directory=backup,
            restore_evidence_path=restore,
            runtime_evidence_path=runtime,
            sbom_directory=sboms,
        )


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
        "coordinated_unpinned_input",
        "scan_unpinned_input",
        "coordinated_wrong_input_digest",
        "root_basename",
        "root_version",
        "purl_repository",
        "purl_manifest",
        "purl_tag",
        "purl_arch",
        "scan_image_id",
        "scan_manifest",
    ),
)
def test_final_gate_rejects_mutated_external_image_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    static, backup, restore, runtime, sboms = _evidence(tmp_path)
    runtime_payload = json.loads(runtime.read_text(encoding="utf-8"))
    postgres = next(item for item in runtime_payload["images"] if item["component"] == "postgres")
    scan_path = sboms / "scan-postgres.json"
    scan_payload = json.loads(scan_path.read_text(encoding="utf-8"))
    sbom_path = sboms / "sbom-postgres.spdx.json"
    sbom_payload = json.loads(sbom_path.read_text(encoding="utf-8"))
    root_id = sbom_payload["relationships"][0]["relatedSpdxElement"]
    root = next(item for item in sbom_payload["packages"] if item["SPDXID"] == root_id)
    expected_input = release_evidence_gate.EXTERNAL_IMAGE_REFERENCES["postgres"]
    tag_only_input = expected_input.partition("@")[0]
    wrong_digest_input = tag_only_input + "@sha256:" + "f" * 64

    if case == "coordinated_unpinned_input":
        postgres["scan_input"] = tag_only_input
        scan_payload["source"]["target"]["userInput"] = tag_only_input
    elif case == "scan_unpinned_input":
        scan_payload["source"]["target"]["userInput"] = tag_only_input
    elif case == "coordinated_wrong_input_digest":
        postgres["scan_input"] = wrong_digest_input
        scan_payload["source"]["target"]["userInput"] = wrong_digest_input
    elif case == "root_basename":
        root["name"] = "postgres"
    elif case == "root_version":
        root["versionInfo"] = "latest"
    elif case == "purl_repository":
        root["externalRefs"][0]["referenceLocator"] = (
            "pkg:oci/postgres@sha256%3A"
            + root["checksums"][0]["checksumValue"]
            + "?arch=amd64&tag=latest"
        )
    elif case == "purl_manifest":
        root["externalRefs"][0]["referenceLocator"] = (
            "pkg:oci/cgr.dev%2Fchainguard%2Fpostgres@sha256%3A"
            + "f" * 64
            + "?arch=amd64&tag=latest"
        )
    elif case == "purl_tag":
        root["externalRefs"][0]["referenceLocator"] = root["externalRefs"][0][
            "referenceLocator"
        ].replace("tag=latest", "tag=stable")
    elif case == "purl_arch":
        root["externalRefs"][0]["referenceLocator"] = root["externalRefs"][0][
            "referenceLocator"
        ].replace("arch=amd64", "arch=arm64")
    elif case == "scan_image_id":
        scan_payload["source"]["target"]["imageID"] = "sha256:" + "f" * 64
    elif case == "scan_manifest":
        scan_payload["source"]["target"]["manifestDigest"] = "sha256:" + "f" * 64
    else:  # pragma: no cover - parameter list is exhaustive.
        raise AssertionError(case)

    if scan_payload != json.loads(scan_path.read_text(encoding="utf-8")):
        _write(scan_path, scan_payload)
        postgres["scan_sha256"] = hashlib.sha256(scan_path.read_bytes()).hexdigest()
    if sbom_payload != json.loads(sbom_path.read_text(encoding="utf-8")):
        _write(sbom_path, sbom_payload)
        postgres["sbom_sha256"] = hashlib.sha256(sbom_path.read_bytes()).hexdigest()
    _write(runtime, runtime_payload)
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
        "wrong_package_id",
        "wrong_package_purl",
        "wrong_package_type",
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
                "id": "fedcba9876543210",
                "name": "foreign",
                "version": "1.0",
                "type": "apk",
                "purl": "pkg:apk/wolfi/foreign@1.0?arch=x86_64",
            }
        elif case == "wrong_package_id":
            payload["matches"][0]["artifact"]["id"] = "fedcba9876543210"
        elif case == "wrong_package_purl":
            payload["matches"][0]["artifact"]["purl"] = "pkg:apk/wolfi/foreign@1.0"
        elif case == "wrong_package_type":
            payload["matches"][0]["artifact"]["type"] = "deb"
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
        "pretraffic_acceptance_not_required",
        "unsafe_runtime_role",
        "missing_public_grant_proof",
        "missing_migrator_mutation",
        "api_rls_bool_visible",
        "worker_rls_foreign",
        "worker_rls_zero_visible",
        "worker_rls_extra_field",
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
    elif case == "pretraffic_acceptance_not_required":
        payloads["restore"]["tenant_acceptance_required_before_traffic"] = False
    elif case == "unsafe_runtime_role":
        payloads["restore"]["database_runtime_roles"]["roles"]["worker_safe"] = False
    elif case == "missing_public_grant_proof":
        del payloads["restore"]["database_runtime_roles"]["roles"][
            "public_object_grants_absent"
        ]
    elif case == "missing_migrator_mutation":
        payloads["restore"]["database_runtime_roles"]["migrator_schema_mutation_verified"] = False
    elif case == "api_rls_bool_visible":
        payloads["restore"]["database_runtime_roles"]["api_rls"]["visible"] = True
    elif case == "worker_rls_foreign":
        payloads["restore"]["database_runtime_roles"]["worker_rls"]["foreign"] = 1
    elif case == "worker_rls_zero_visible":
        payloads["restore"]["database_runtime_roles"]["worker_rls"]["visible"] = 0
    elif case == "worker_rls_extra_field":
        payloads["restore"]["database_runtime_roles"]["worker_rls"]["unexpected"] = True
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
        "ambiguous_syft_id",
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
        "ambiguous_syft_id",
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
        elif case == "ambiguous_syft_id":
            payload["packages"].append(
                {
                    "name": "another-package",
                    "SPDXID": "SPDXRef-Package-apk-another-package-0123456789abcdef",
                    "versionInfo": "1.0-r0",
                    "externalRefs": [
                        {
                            "referenceCategory": "PACKAGE-MANAGER",
                            "referenceType": "purl",
                            "referenceLocator": "pkg:apk/wolfi/another-package@1.0-r0",
                        }
                    ],
                }
            )
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
        ("web", "nodejs-24"),
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
    assert workflow.count("$(scan_image_id ") == len(release_evidence_gate.REQUIRED_IMAGES)
    for component, image_reference in release_evidence_gate.EXTERNAL_IMAGE_REFERENCES.items():
        assert image_reference in compose
        assert workflow.count(f"image: {image_reference}") == (3 if component == "postgres" else 2)
    assert '--static-report "$RELEASE_EVIDENCE_DIR/static-release-readiness.json"' in workflow
    assert '--runtime-evidence "$RELEASE_EVIDENCE_DIR/runtime-evidence.json"' in workflow
    assert '--sbom-directory "$RELEASE_EVIDENCE_DIR"' in workflow
    assert '--output "$RELEASE_EVIDENCE_DIR/release-readiness.json"' in workflow
