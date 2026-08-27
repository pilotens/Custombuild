"""Bind static, runtime, backup and restore evidence to one release source."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

try:
    from scripts.compose_backup import MANIFEST_SCHEMA, BackupError, verify_manifest
    from scripts.source_manifest import build_source_manifest
except ModuleNotFoundError:  # Direct ``python scripts/release_evidence_gate.py`` execution.
    from compose_backup import (  # type: ignore[import-not-found,no-redef]
        MANIFEST_SCHEMA,
        BackupError,
        verify_manifest,
    )
    from source_manifest import build_source_manifest  # type: ignore[import-not-found,no-redef]

STATIC_SCHEMA = "custombuild.release-readiness-static.v2"
RESTORE_SCHEMA = "custombuild.restore-drill.v3"
RUNTIME_SCHEMA = "custombuild.runtime-release-evidence.v3"
FINAL_SCHEMA = "custombuild.release-readiness-evidence.v1"
REQUIRED_IMAGES = frozenset(
    {"api", "worker", "web", "seaweedfs", "postgres", "redis", "volume-init"}
)
REQUIRED_NATIVE_PACKAGES = {
    "api": frozenset(
        {
            ("python-3.13", "3.13.15-r2"),
            ("python-3.13-base", "3.13.15-r2"),
        }
    ),
    "worker": frozenset(
        {
            ("ca-certificates-bundle", "20260611-r0"),
            ("glibc-2.44-locale-posix", "2.44-r1"),
            ("glibc-2.44", "2.44-r1"),
            ("ld-linux-2.44", "2.44-r1"),
            ("libgcc", "16.2.0-r0"),
            ("libstdc++", "16.2.0-r0"),
            ("wolfi-baselayout", "20230201-r29"),
            ("py3-pip-wheel", "26.2.1-r0"),
            ("libbz2-1", "1.0.8-r23"),
            ("libcrypto3", "3.6.4-r0"),
            ("libexpat1", "2.8.3-r0"),
            ("libffi", "3.8.0-r0"),
            ("gdbm", "1.26-r5"),
            ("xz", "5.8.3-r2"),
            ("mpdecimal", "4.0.1-r3"),
            ("ncurses-terminfo-base", "6.6.20260822-r0"),
            ("ncurses", "6.6.20260822-r0"),
            ("readline", "8.3-r2"),
            ("sqlite-libs", "3.53.4-r0"),
            ("libssl3", "3.6.4-r0"),
            ("libuuid", "2.42.2-r3"),
            ("zlib", "1.3.2-r4"),
            ("python-3.13", "3.13.15-r2"),
            ("python-3.13-base", "3.13.15-r2"),
            ("mesa-gl", "26.2.1-r0"),
            ("libx11", "1.8.13-r5"),
            ("libxau", "1.0.12-r7"),
            ("libxdmcp", "1.1.5-r9"),
            ("libbsd", "0.12.2-r7"),
            ("libmd", "1.2.0-r2"),
            ("libgomp", "16.2.0-r1"),
            ("libxcb", "1.17.0-r15"),
            ("libglvnd", "1.7.0-r10"),
            ("libxml2", "2.15.0-r0"),
            ("libLLVM-22", "22.1.8-r2"),
            ("libpciaccess", "0.19-r2"),
            ("libdrm", "2.4.134-r0"),
            ("libzstd1", "1.5.7-r8"),
            ("libelf", "0.195-r2"),
            ("libxshmfence", "1.3.3-r2"),
            ("mesa-libgallium", "26.2.1-r0"),
            ("mesa-gbm", "26.2.1-r0"),
            ("libudev", "261.2-r1"),
            ("wayland-libs-client", "1.26.0-r0"),
            ("wayland-protocols", "1.49-r0"),
            ("mesa", "26.2.1-r0"),
            ("libxext", "1.3.7-r0"),
            ("libxxf86vm", "1.1.7-r2"),
            ("mesa-glx", "26.2.1-r0"),
        }
    ),
    "web": frozenset({("nodejs-24", "24.19.0-r0")}),
}
BUILT_IMAGE_NAMES = {
    "api": "custombuild-api",
    "worker": "custombuild-worker",
    "web": "custombuild-web",
    "seaweedfs": "custombuild-seaweedfs",
}
EXTERNAL_IMAGE_REFERENCES = {
    "postgres": (
        "cgr.dev/chainguard/postgres:latest@"
        "sha256:3af67abef0353ec61f054acf649abb5eaaae9742a9c1c9125e073c7833736060"
    ),
    "redis": (
        "redis:7.2.15-alpine@"
        "sha256:05a97a479bc73de66f087dc05b569010772880f778cc8671fa6b8aadee32e5c6"
    ),
    "volume-init": (
        "cgr.dev/chainguard/busybox:latest@"
        "sha256:928939fc7f20750dea03366627d83bfa497df565fcf6b55fdddb004ecd8426d6"
    ),
}
SHA256 = re.compile(r"^[a-f0-9]{64}$")
IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
REVISION = re.compile(r"^[a-f0-9]{40}$")
SYFT_CREATOR = re.compile(r"^Tool: syft-[^\s]+$")
SYFT_PACKAGE_ID = re.compile(r"^[a-f0-9]{16}$")
GRYPE_TO_SPDX_PACKAGE_TYPES = {"UnknownPackage": "binary"}
GRYPE_VERSION = "0.110.0"


class EvidenceError(RuntimeError):
    """Raised when release evidence is incomplete or cross-source."""


def _load(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{label} is missing or invalid JSON") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _git_revision(repo: Path) -> str:
    git = shutil.which("git")
    if git is None:
        raise EvidenceError("Git CLI is unavailable")
    completed = subprocess.run(  # noqa: S603 - fixed git query and reviewed repository path.
        [git, "rev-parse", "HEAD"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    revision = completed.stdout.strip()
    if completed.returncode or not REVISION.fullmatch(revision):
        raise EvidenceError("could not resolve an exact checked Git revision")
    return revision


def _assert_clean_repository(repo: Path) -> None:
    git = shutil.which("git")
    if git is None:
        raise EvidenceError("Git CLI is unavailable")
    completed = subprocess.run(  # noqa: S603 - fixed git query and reviewed repository path.
        [git, "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode:
        raise EvidenceError("could not verify the final repository state")
    if completed.stdout:
        raise EvidenceError("repository changed after static release controls were captured")


def _expected_image_reference(component: str, revision: str) -> str:
    if component in BUILT_IMAGE_NAMES:
        return f"{BUILT_IMAGE_NAMES[component]}:{revision}"
    return EXTERNAL_IMAGE_REFERENCES[component]


def _expected_scan_input(component: str, revision: str) -> str:
    return _expected_image_reference(component, revision)


def _expected_sbom_root_identity(component: str, revision: str) -> tuple[str, str, str]:
    reference = _expected_image_reference(component, revision)
    tagged_reference, digest_separator, digest = reference.partition("@")
    repository, separator, tag = tagged_reference.rpartition(":")
    if (
        not separator
        or not repository
        or not tag
        or (digest_separator and not IMAGE_ID.fullmatch(digest))
    ):
        raise EvidenceError(f"runtime image {component} has an invalid expected reference")
    version = digest if digest_separator and tag == "latest" else tag
    return repository, version, tag


def _verify_sboms(
    directory: Path,
    images: list[dict[str, Any]],
    revision: str,
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    expected_names = {f"sbom-{component}.spdx.json" for component in REQUIRED_IMAGES}
    try:
        candidates = {
            path.name: path for path in directory.iterdir() if path.name.startswith("sbom-")
        }
    except OSError as exc:
        raise EvidenceError("runtime SBOM directory is missing or unreadable") from exc
    if set(candidates) != expected_names:
        raise EvidenceError("runtime SBOM directory does not contain the exact required set")
    runtime_by_component = {str(item["component"]): item for item in images}
    digests: dict[str, str] = {}
    identities: dict[str, dict[str, Any]] = {}
    for component in sorted(REQUIRED_IMAGES):
        path = candidates[f"sbom-{component}.spdx.json"]
        if path.is_symlink() or not path.is_file():
            raise EvidenceError(f"runtime SBOM {component} is not a regular file")
        try:
            payload = path.read_bytes()
            document = json.loads(payload)
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError(f"runtime SBOM {component} is missing or invalid JSON") from exc
        if (
            not isinstance(document, dict)
            or not re.fullmatch(r"SPDX-2\.(?:2|3)", str(document.get("spdxVersion", "")))
            or document.get("SPDXID") != "SPDXRef-DOCUMENT"
        ):
            raise EvidenceError(f"runtime SBOM {component} is not an SPDX JSON document")
        creation_info = document.get("creationInfo")
        creators = creation_info.get("creators") if isinstance(creation_info, dict) else None
        if (
            not isinstance(creators, list)
            or not creators
            or not all(isinstance(creator, str) for creator in creators)
            or not any(SYFT_CREATOR.fullmatch(creator) for creator in creators)
        ):
            raise EvidenceError(f"runtime SBOM {component} has no exact Syft creator")
        packages = document.get("packages")
        if not isinstance(packages, list) or not packages:
            raise EvidenceError(f"runtime SBOM {component} has no packages")
        package_versions: set[tuple[str, str]] = set()
        package_identities: dict[str, tuple[str, str]] = {}
        packages_by_syft_id: dict[str, tuple[str, str, str, str]] = {}
        for package in packages:
            if not isinstance(package, dict):
                raise EvidenceError(f"runtime SBOM {component} has an invalid package")
            name = package.get("name")
            version = package.get("versionInfo")
            if not isinstance(name, str) or not name:
                raise EvidenceError(f"runtime SBOM {component} has an unnamed package")
            spdx_id = package.get("SPDXID")
            if not isinstance(spdx_id, str) or not spdx_id.startswith("SPDXRef-"):
                raise EvidenceError(f"runtime SBOM {component} has a package without an SPDX ID")
            if spdx_id in package_identities:
                raise EvidenceError(f"runtime SBOM {component} has a duplicate package SPDX ID")
            package_identities[spdx_id] = (name, version if isinstance(version, str) else "")
            syft_package_id = spdx_id.rpartition("-")[2]
            if SYFT_PACKAGE_ID.fullmatch(syft_package_id):
                external_refs = package.get("externalRefs")
                package_purls = (
                    [
                        reference.get("referenceLocator")
                        for reference in external_refs
                        if isinstance(reference, dict)
                        and reference.get("referenceCategory") == "PACKAGE-MANAGER"
                        and reference.get("referenceType") == "purl"
                        and isinstance(reference.get("referenceLocator"), str)
                    ]
                    if isinstance(external_refs, list)
                    else []
                )
                if len(package_purls) != 1 or syft_package_id in packages_by_syft_id:
                    raise EvidenceError(
                        f"runtime SBOM {component} has an ambiguous Syft package identity"
                    )
                packages_by_syft_id[syft_package_id] = (
                    name,
                    version if isinstance(version, str) else "",
                    str(package_purls[0]),
                    spdx_id,
                )
            if isinstance(version, str) and version:
                package_versions.add((name, version))
        required_packages = REQUIRED_NATIVE_PACKAGES.get(component, frozenset())
        if not required_packages.issubset(package_versions):
            raise EvidenceError(
                f"runtime SBOM {component} does not contain its exact native runtime closure"
            )
        required_package_names = {name for name, _version in required_packages}
        if component in {"api", "worker"} and any(
            name.startswith("python-3.") and name not in required_package_names
            for name, _version in package_versions
        ):
            raise EvidenceError(f"runtime SBOM {component} contains another Python runtime")
        if component == "web" and any(
            name.startswith("nodejs-") and (name, version) not in required_packages
            for name, version in package_versions
        ):
            raise EvidenceError("runtime SBOM web contains another Node.js runtime")
        relationships = document.get("relationships")
        if not isinstance(relationships, list):
            raise EvidenceError(f"runtime SBOM {component} has no relationship graph")
        described_roots = [
            relationship.get("relatedSpdxElement")
            for relationship in relationships
            if isinstance(relationship, dict)
            and relationship.get("spdxElementId") == "SPDXRef-DOCUMENT"
            and relationship.get("relationshipType") == "DESCRIBES"
        ]
        if (
            len(described_roots) != 1
            or not isinstance(described_roots[0], str)
            or described_roots[0] not in package_identities
        ):
            raise EvidenceError(f"runtime SBOM {component} has no exact described image root")
        root_spdx_id = described_roots[0]
        root = next(package for package in packages if package.get("SPDXID") == root_spdx_id)
        if root.get("primaryPackagePurpose") != "CONTAINER":
            raise EvidenceError(f"runtime SBOM {component} root is not a container")
        expected_input = _expected_scan_input(component, revision)
        expected_name, expected_version, expected_tag = _expected_sbom_root_identity(
            component, revision
        )
        if root.get("name") != expected_name or root.get("versionInfo") != expected_version:
            raise EvidenceError(f"runtime SBOM {component} has another image root identity")
        checksums = root.get("checksums")
        sha256_checksums = (
            [
                checksum.get("checksumValue")
                for checksum in checksums
                if isinstance(checksum, dict) and checksum.get("algorithm") == "SHA256"
            ]
            if isinstance(checksums, list)
            else []
        )
        if (
            len(sha256_checksums) != 1
            or not isinstance(sha256_checksums[0], str)
            or not SHA256.fullmatch(sha256_checksums[0])
        ):
            raise EvidenceError(f"runtime SBOM {component} has no exact image manifest checksum")
        manifest_digest = f"sha256:{sha256_checksums[0]}"
        external_refs = root.get("externalRefs")
        oci_purls = (
            [
                reference.get("referenceLocator")
                for reference in external_refs
                if isinstance(reference, dict)
                and reference.get("referenceCategory") == "PACKAGE-MANAGER"
                and reference.get("referenceType") == "purl"
                and isinstance(reference.get("referenceLocator"), str)
                and str(reference["referenceLocator"]).startswith("pkg:oci/")
            ]
            if isinstance(external_refs, list)
            else []
        )
        if len(oci_purls) != 1:
            raise EvidenceError(f"runtime SBOM {component} has no exact OCI identity")
        decoded_purl = unquote(str(oci_purls[0]))
        parsed_purl = urlsplit(decoded_purl)
        purl_query = parse_qs(parsed_purl.query, strict_parsing=False)
        if (
            parsed_purl.scheme != "pkg"
            or parsed_purl.path != f"oci/{expected_name}@{manifest_digest}"
            or parsed_purl.netloc
            or parsed_purl.fragment
            or purl_query != {"arch": ["amd64"], "tag": [expected_tag]}
        ):
            raise EvidenceError(f"runtime SBOM {component} OCI identity differs from its root")
        if runtime_by_component[component].get("manifest_digest") != manifest_digest:
            raise EvidenceError(f"runtime SBOM {component} manifest differs from runtime evidence")
        digest = hashlib.sha256(payload).hexdigest()
        if runtime_by_component[component].get("sbom_sha256") != digest:
            raise EvidenceError(f"runtime SBOM {component} differs from runtime evidence")
        digests[component] = digest
        identities[component] = {
            "manifest_digest": manifest_digest,
            "scan_input": expected_input,
            "packages_by_syft_id": packages_by_syft_id,
        }
    return digests, identities


def _verify_scans(
    directory: Path,
    images: list[dict[str, Any]],
    sbom_identities: dict[str, dict[str, Any]],
) -> dict[str, str]:
    expected_names = {f"scan-{component}.json" for component in REQUIRED_IMAGES}
    try:
        candidates = {
            path.name: path for path in directory.iterdir() if path.name.startswith("scan-")
        }
    except OSError as exc:
        raise EvidenceError("runtime scan directory is missing or unreadable") from exc
    if set(candidates) != expected_names:
        raise EvidenceError("runtime scan directory does not contain the exact required set")
    runtime_by_component = {str(item["component"]): item for item in images}
    digests: dict[str, str] = {}
    for component in sorted(REQUIRED_IMAGES):
        path = candidates[f"scan-{component}.json"]
        if path.is_symlink() or not path.is_file():
            raise EvidenceError(f"runtime scan {component} is not a regular file")
        try:
            payload = path.read_bytes()
            document = json.loads(payload)
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError(f"runtime scan {component} is missing or invalid JSON") from exc
        if not isinstance(document, dict):
            raise EvidenceError(f"runtime scan {component} must be a JSON object")
        descriptor = document.get("descriptor")
        configuration = descriptor.get("configuration") if isinstance(descriptor, dict) else None
        database = descriptor.get("db") if isinstance(descriptor, dict) else None
        database_status = database.get("status") if isinstance(database, dict) else None
        if (
            not isinstance(descriptor, dict)
            or descriptor.get("name") != "grype"
            or descriptor.get("version") != GRYPE_VERSION
            or not isinstance(configuration, dict)
            or configuration.get("fail-on-severity") != "high"
            or configuration.get("output") != ["json"]
            or not isinstance(database_status, dict)
            or database_status.get("valid") is not True
        ):
            raise EvidenceError(f"runtime scan {component} has no valid pinned Grype proof")
        source = document.get("source")
        target = source.get("target") if isinstance(source, dict) else None
        sbom_identity = sbom_identities[component]
        if (
            not isinstance(source, dict)
            or source.get("type") != "image"
            or not isinstance(target, dict)
            or target.get("userInput") != sbom_identity["scan_input"]
            or target.get("imageID") != runtime_by_component[component].get("image_id")
            or target.get("manifestDigest") != sbom_identity["manifest_digest"]
        ):
            raise EvidenceError(f"runtime scan {component} is not bound to its exact SBOM target")
        matches = document.get("matches")
        if not isinstance(matches, list):
            raise EvidenceError(f"runtime scan {component} has no valid match list")
        for match in matches:
            vulnerability = match.get("vulnerability") if isinstance(match, dict) else None
            severity = vulnerability.get("severity") if isinstance(vulnerability, dict) else None
            if not isinstance(severity, str) or not severity:
                raise EvidenceError(f"runtime scan {component} has an invalid vulnerability")
            if severity.casefold() in {"high", "critical"}:
                raise EvidenceError(
                    f"runtime scan {component} contains a High or Critical vulnerability"
                )
            artifact = match.get("artifact") if isinstance(match, dict) else None
            artifact_id = artifact.get("id") if isinstance(artifact, dict) else None
            sbom_package = (
                sbom_identity["packages_by_syft_id"].get(artifact_id)
                if isinstance(artifact_id, str) and SYFT_PACKAGE_ID.fullmatch(artifact_id)
                else None
            )
            artifact_type = artifact.get("type") if isinstance(artifact, dict) else None
            spdx_package_type = (
                GRYPE_TO_SPDX_PACKAGE_TYPES.get(artifact_type, artifact_type)
                if isinstance(artifact_type, str)
                else None
            )
            if (
                not isinstance(sbom_package, tuple)
                or len(sbom_package) != 4
                or sbom_package[:3]
                != (artifact.get("name"), artifact.get("version"), artifact.get("purl"))
                or not isinstance(artifact_type, str)
                or not artifact_type
                or not sbom_package[3].startswith(f"SPDXRef-Package-{spdx_package_type}-")
            ):
                raise EvidenceError(f"runtime scan {component} contains a match from another SBOM")
        ignored_matches = document.get("ignoredMatches")
        if ignored_matches is not None and not isinstance(ignored_matches, list):
            raise EvidenceError(f"runtime scan {component} has invalid ignored matches")
        if isinstance(ignored_matches, list) and ignored_matches:
            raise EvidenceError(f"runtime scan {component} contains ignored vulnerabilities")
        digest = hashlib.sha256(payload).hexdigest()
        if runtime_by_component[component].get("scan_sha256") != digest:
            raise EvidenceError(f"runtime scan {component} differs from runtime evidence")
        digests[component] = digest
    return digests


def build_final_report(
    repo: Path,
    *,
    static_report_path: Path,
    backup_directory: Path,
    restore_evidence_path: Path,
    runtime_evidence_path: Path,
    sbom_directory: Path,
) -> dict[str, Any]:
    repo = repo.resolve()
    _assert_clean_repository(repo)
    revision = _git_revision(repo)
    source_manifest_sha256 = build_source_manifest(repo)[2]
    static = _load(static_report_path, "static release report")
    runtime = _load(runtime_evidence_path, "runtime image evidence")
    restore = _load(restore_evidence_path, "restore evidence")
    try:
        backup = verify_manifest(backup_directory)
    except BackupError as exc:
        raise EvidenceError(f"coordinated backup verification failed: {exc}") from exc

    if (
        static.get("schema_version") != STATIC_SCHEMA
        or static.get("static_controls_ready") is not True
        or static.get("software_release_ready") is not False
        or static.get("runtime_evidence_required") is not True
    ):
        raise EvidenceError("static release controls are not ready")
    for label, evidence in (
        ("static release report", static),
        ("coordinated backup", backup),
        ("restore evidence", restore),
        ("runtime image evidence", runtime),
    ):
        if evidence.get("git_revision") != revision:
            raise EvidenceError(f"{label} is not bound to the checked Git revision")
        if evidence.get("source_manifest_sha256") != source_manifest_sha256:
            raise EvidenceError(f"{label} is not bound to the checked source manifest")

    if backup.get("schema_version") != MANIFEST_SCHEMA:
        raise EvidenceError("coordinated backup schema is not current")
    if restore.get("schema_version") != RESTORE_SCHEMA or restore.get("status") != "PASS":
        raise EvidenceError("restore drill did not pass the current evidence contract")
    if restore.get("backup_created_at") != backup.get("created_at"):
        raise EvidenceError("restore evidence is not bound to this coordinated backup")
    object_store = backup.get("object_store")
    if not isinstance(object_store, dict):
        raise EvidenceError("coordinated backup has no object-store identity")
    if (
        restore.get("object_store_image") != object_store.get("image")
        or restore.get("object_store_image_id") != object_store.get("image_id")
        or restore.get("object_store_bucket") != object_store.get("bucket")
        or restore.get("object_store_object_count") != object_store.get("object_count")
        or restore.get("object_store_total_size_bytes") != object_store.get("total_size_bytes")
        or restore.get("database_snapshot") != backup.get("database_snapshot")
    ):
        raise EvidenceError("restore evidence differs from the coordinated backup identity")
    for key in (
        "database_exact_row_counts_verified",
        "object_store_hashes_verified",
        "object_store_metadata_verified",
        "tenant_rls_verified",
    ):
        if restore.get(key) is not True:
            raise EvidenceError(f"restore evidence did not prove {key}")
    database_snapshot = backup.get("database_snapshot")
    if not isinstance(database_snapshot, dict):
        raise EvidenceError("coordinated backup has no database snapshot")
    row_counts = database_snapshot.get("row_counts")
    if not isinstance(row_counts, dict):
        raise EvidenceError("coordinated backup has no exact database row counts")
    if (
        restore.get("database_alembic_heads") != database_snapshot.get("alembic_heads")
        or restore.get("database_project_rows") != row_counts.get("projects")
        or restore.get("tenant_acceptance_required_before_traffic") is not False
    ):
        raise EvidenceError("restore evidence did not reproduce the database recovery point")
    runtime_roles = restore.get("database_runtime_roles")
    if not isinstance(runtime_roles, dict):
        raise EvidenceError("restore evidence has no runtime-role proof")
    expected_role_flags = {
        "migrator_safe": True,
        "api_safe": True,
        "worker_safe": True,
        "memberships_absent": True,
        "all_public_objects_owned_by_migrator": True,
    }
    if runtime_roles.get("roles") != expected_role_flags:
        raise EvidenceError("restore evidence did not prove exact safe database roles")
    api_rls = runtime_roles.get("api_rls")
    worker_rls = runtime_roles.get("worker_rls")
    if (
        not isinstance(api_rls, dict)
        or api_rls.get("foreign") != 0
        or not isinstance(api_rls.get("visible"), int)
        or isinstance(api_rls.get("visible"), bool)
        or api_rls["visible"] < 1
        or worker_rls != api_rls
        or runtime_roles.get("migrator_schema_mutation_verified") is not True
    ):
        raise EvidenceError("restore evidence did not prove runtime RLS and migrator ownership")

    if runtime.get("schema_version") != RUNTIME_SCHEMA or runtime.get("status") != "PASS":
        raise EvidenceError("runtime image evidence did not pass")
    images = runtime.get("images")
    if not isinstance(images, list):
        raise EvidenceError("runtime image evidence has no image list")
    components: set[str] = set()
    for item in images:
        if not isinstance(item, dict):
            raise EvidenceError("runtime image evidence contains an invalid entry")
        component = item.get("component")
        image_id = item.get("image_id")
        image_reference = item.get("image_reference")
        manifest_digest = item.get("manifest_digest")
        scan_input = item.get("scan_input")
        scan_sha256 = item.get("scan_sha256")
        sbom_sha256 = item.get("sbom_sha256")
        if (
            not isinstance(component, str)
            or component not in REQUIRED_IMAGES
            or component in components
        ):
            raise EvidenceError("runtime image evidence has a duplicate component")
        if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
            raise EvidenceError(f"runtime image {component} has no exact image ID")
        if image_reference != _expected_image_reference(component, revision):
            raise EvidenceError(f"runtime image {component} has another image reference")
        if scan_input != _expected_scan_input(component, revision):
            raise EvidenceError(f"runtime image {component} has another scan input")
        if not isinstance(manifest_digest, str) or not IMAGE_ID.fullmatch(manifest_digest):
            raise EvidenceError(f"runtime image {component} has no exact manifest digest")
        if not isinstance(scan_sha256, str) or not SHA256.fullmatch(scan_sha256):
            raise EvidenceError(f"runtime image {component} has no scan digest")
        if not isinstance(sbom_sha256, str) or not SHA256.fullmatch(sbom_sha256):
            raise EvidenceError(f"runtime image {component} has no SBOM digest")
        if item.get("scan_status") != "PASS":
            raise EvidenceError(f"runtime image {component} did not pass vulnerability scanning")
        components.add(component)
    if components != REQUIRED_IMAGES:
        raise EvidenceError("runtime evidence does not cover the exact required image set")
    sbom_sha256, sbom_identities = _verify_sboms(sbom_directory, images, revision)
    scan_sha256 = _verify_scans(sbom_directory, images, sbom_identities)
    seaweed = next(item for item in images if item.get("component") == "seaweedfs")
    if seaweed.get("image_id") != object_store.get("image_id"):
        raise EvidenceError("scanned SeaweedFS image differs from backup and restore")

    return {
        "schema_version": FINAL_SCHEMA,
        "git_revision": revision,
        "source_manifest_sha256": source_manifest_sha256,
        "software_release_ready": True,
        "commercial_release_ready": False,
        "physical_machine_release_ready": False,
        "static_report": static,
        "backup_manifest_schema": MANIFEST_SCHEMA,
        "restore_evidence_schema": RESTORE_SCHEMA,
        "runtime_evidence_schema": RUNTIME_SCHEMA,
        "runtime_image_ids": {
            str(item["component"]): str(item["image_id"])
            for item in sorted(images, key=lambda entry: str(entry["component"]))
        },
        "runtime_image_references": {
            str(item["component"]): str(item["image_reference"])
            for item in sorted(images, key=lambda entry: str(entry["component"]))
        },
        "runtime_scan_inputs": {
            str(item["component"]): str(item["scan_input"])
            for item in sorted(images, key=lambda entry: str(entry["component"]))
        },
        "runtime_sbom_sha256": sbom_sha256,
        "runtime_scan_sha256": scan_sha256,
        "runtime_manifest_digests": {
            component: str(identity["manifest_digest"])
            for component, identity in sorted(sbom_identities.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--static-report", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--restore-evidence", type=Path, required=True)
    parser.add_argument("--runtime-evidence", type=Path, required=True)
    parser.add_argument("--sbom-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = build_final_report(
        arguments.repo,
        static_report_path=arguments.static_report,
        backup_directory=arguments.backup,
        restore_evidence_path=arguments.restore_evidence,
        runtime_evidence_path=arguments.runtime_evidence,
        sbom_directory=arguments.sbom_directory,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvidenceError as exc:
        print(f"Release evidence gate: FAIL - {exc}")
        raise SystemExit(1) from exc
