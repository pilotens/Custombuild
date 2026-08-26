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
RUNTIME_SCHEMA = "custombuild.runtime-release-evidence.v1"
FINAL_SCHEMA = "custombuild.release-readiness-evidence.v1"
REQUIRED_IMAGES = frozenset({"api", "worker", "web", "seaweedfs", "postgres", "redis", "alpine"})
SHA256 = re.compile(r"^[a-f0-9]{64}$")
IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
REVISION = re.compile(r"^[a-f0-9]{40}$")


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


def _verify_sboms(
    directory: Path,
    images: list[dict[str, Any]],
) -> dict[str, str]:
    expected_names = {f"sbom-{component}.spdx.json" for component in REQUIRED_IMAGES}
    try:
        candidates = {
            path.name: path
            for path in directory.iterdir()
            if path.name.startswith("sbom-")
        }
    except OSError as exc:
        raise EvidenceError("runtime SBOM directory is missing or unreadable") from exc
    if set(candidates) != expected_names:
        raise EvidenceError("runtime SBOM directory does not contain the exact required set")
    runtime_by_component = {str(item["component"]): item for item in images}
    digests: dict[str, str] = {}
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
        digest = hashlib.sha256(payload).hexdigest()
        if runtime_by_component[component].get("sbom_sha256") != digest:
            raise EvidenceError(f"runtime SBOM {component} differs from runtime evidence")
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
        or restore.get("object_store_total_size_bytes")
        != object_store.get("total_size_bytes")
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
        scan_sha256 = item.get("scan_sha256")
        sbom_sha256 = item.get("sbom_sha256")
        if not isinstance(component, str) or component in components:
            raise EvidenceError("runtime image evidence has a duplicate component")
        if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
            raise EvidenceError(f"runtime image {component} has no exact image ID")
        if not isinstance(scan_sha256, str) or not SHA256.fullmatch(scan_sha256):
            raise EvidenceError(f"runtime image {component} has no scan digest")
        if not isinstance(sbom_sha256, str) or not SHA256.fullmatch(sbom_sha256):
            raise EvidenceError(f"runtime image {component} has no SBOM digest")
        if item.get("scan_status") != "PASS":
            raise EvidenceError(f"runtime image {component} did not pass vulnerability scanning")
        components.add(component)
    if components != REQUIRED_IMAGES:
        raise EvidenceError("runtime evidence does not cover the exact required image set")
    sbom_sha256 = _verify_sboms(sbom_directory, images)
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
        "runtime_sbom_sha256": sbom_sha256,
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
