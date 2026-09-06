"""Standalone, standard-library verifier for treating v5 ZIPs as untrusted data.

Distribute this file through a trusted channel outside the package being
verified.  It deliberately has no Custombuild imports and never imports or
executes content from the received ZIP.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, TextIO

VERIFIER_VERSION = "custombuild-offline-package-verifier-1.2.0"
REPORT_SCHEMA_VERSION = "custombuild.offline-package-verification.v1"
MANIFEST_SCHEMA_VERSION = "custombuild.production-manifest.v5"
ARTIFACT_SCHEMA_VERSION = "custombuild.production-artifacts.v1"
MANIFEST_PATH = "manifest.json"
FROZEN_DESIGN_SPEC_PATH = "design/design-spec.json"
FROZEN_DESIGN_SPEC_ROLE = "FROZEN_DESIGN_SPEC"
FROZEN_DESIGN_SPEC_MEDIA_TYPE = "application/json"
FROZEN_DESIGN_SPEC_SCHEMA_VERSION = "custombuild.frozen-design-spec.v1"
JOINT_RETENTION_SIGNED_EVIDENCE_PATH = "evidence/joint-retention/signed-evidence.json"
JOINT_RETENTION_SIGNED_EVIDENCE_ROLE = "JOINT_RETENTION_SIGNED_EVIDENCE"
JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE = "application/json"
MANIFEST_CHECKSUM_SCOPE = "all payload files; manifest.json excluded to avoid recursive hashing"
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 3 * 1024 * 1024
MAX_ENTRY_BYTES = 32 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 10_000
MAX_COMPRESSION_RATIO = 1_000
_DIGEST = re.compile(r"[a-f0-9]{64}")
_MANIFEST_CONTEXT_FIELDS = (
    "project_id",
    "revision",
    "design_hash",
    "app_version",
    "engine_version",
    "template_version",
    "domain_template_version",
    "template_capability_version",
    "template_capability_registry_version",
    "template_id",
    "template_capability_fingerprint",
    "template_capability",
    "rule_version",
    "material_versions",
    "joint_version",
    "machine_profile",
    "postprocessor_version",
    "generation_context_hash",
    "production_engine_context",
    "artifact_schema_version",
    "cad_status",
    "release_scope",
    "machine_use",
    "physical_cutting_authorized",
    "approved_assumptions",
    "warnings",
    "overrides",
    "external_evidence",
    "source_provenance",
    "artifacts",
)
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        *_MANIFEST_CONTEXT_FIELDS,
        "production_context_hash",
        "checksum_scope",
    }
)
_ARTIFACT_KEYS = frozenset({"path", "media_type", "role", "size_bytes", "sha256"})
_BOUNDARY_WARNINGS = (
    "Artifact SHA-256 values verify internal consistency relative to this unsigned manifest; "
    "they do not authenticate the publisher or issuer.",
    "The required expected bundle SHA-256 must come from an authenticated, out-of-band source "
    "independent of the received ZIP; a digest delivered with the same untrusted ZIP does not "
    "authenticate it.",
    "A matching external bundle digest detects byte changes, including a coordinated internal "
    "rewrite, relative to that supplied digest; the match is not a publisher signature.",
    "Expected project, revision and design-hash options compare unsigned manifest claims only; "
    "they do not independently reconstruct design semantics or establish authenticity.",
    "A PASS does not authorize physical cutting, machining, or assembly.",
    "This verifier does not establish current revocation or expiry status for external "
    "signed evidence.",
    "No registry embedded in a ZIP is trusted; use the authenticated server to recheck "
    "the certifier, signature, registry high-water, revocation and expiry before use.",
)


class VerificationFailure(Exception):
    """A deterministic, recipient-actionable verification failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StructuredArgumentParser(argparse.ArgumentParser):
    """Convert argparse failures into the verifier's JSON report contract."""

    def error(self, message: str) -> NoReturn:
        raise VerificationFailure("INVALID_ARGUMENTS", message)


def _fail(code: str, message: str) -> NoReturn:
    raise VerificationFailure(code, message)


def _reject_json_constant(value: str) -> NoReturn:
    _fail("INVALID_MANIFEST_JSON", f"manifest contains unsupported JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail("DUPLICATE_MANIFEST_KEY", f"manifest contains duplicate JSON key: {key}")
        value[key] = item
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        _fail("NON_CANONICAL_MANIFEST", f"manifest cannot be encoded canonically: {exc}")


def _parse_manifest(payload: bytes) -> dict[str, Any]:
    try:
        decoded = payload.decode("utf-8")
        parsed = json.loads(
            decoded,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except VerificationFailure:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        _fail("INVALID_MANIFEST_JSON", f"manifest.json is not strict UTF-8 JSON: {exc}")
    if not isinstance(parsed, dict):
        _fail("INVALID_MANIFEST_STRUCTURE", "manifest.json must contain one JSON object")
    if frozenset(parsed) != _MANIFEST_KEYS:
        _fail("INVALID_MANIFEST_STRUCTURE", "manifest.json has unexpected top-level fields")
    if _canonical_json_bytes(parsed) != payload:
        _fail("NON_CANONICAL_MANIFEST", "manifest.json is not canonical UTF-8 JSON")
    return parsed


def _validate_path(path: Any) -> str:
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path or ":" in path:
        _fail("UNSAFE_ZIP_PATH", f"ZIP contains an invalid path: {path!r}")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        _fail("UNSAFE_ZIP_PATH", f"ZIP contains an unsafe path: {path}")
    if PurePosixPath(path).is_absolute():
        _fail("UNSAFE_ZIP_PATH", f"ZIP contains an absolute path: {path}")
    return path


def _sha256_stream(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    expected_size: int,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with archive.open(info, mode="r") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > expected_size or size > MAX_ENTRY_BYTES:
                    _fail(
                        "ARTIFACT_SIZE_MISMATCH",
                        f"artifact exceeds declared size: {info.filename}",
                    )
                digest.update(chunk)
    except VerificationFailure:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        _fail("UNREADABLE_ARTIFACT", f"cannot read ZIP artifact {info.filename}: {exc}")
    return size, digest.hexdigest()


def _validate_zip_metadata(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_FILES:
        _fail("TOO_MANY_ZIP_ENTRIES", "ZIP contains too many entries")
    by_name: dict[str, zipfile.ZipInfo] = {}
    casefolded: set[str] = set()
    total_size = 0
    for info in infos:
        name = _validate_path(info.filename)
        if name.casefold() == "__main__.py":
            _fail(
                "EXECUTABLE_ZIP_ENTRY_FORBIDDEN",
                "received packages must not contain executable __main__.py content",
            )
        folded = name.casefold()
        if name in by_name or folded in casefolded:
            _fail("DUPLICATE_ZIP_PATH", f"ZIP contains a duplicate or case-alias path: {name}")
        if info.is_dir() or info.flag_bits & 0x1:
            _fail("UNSAFE_ZIP_ENTRY", f"ZIP contains a directory or encrypted entry: {name}")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if info.create_system != 3 or unix_mode != 0o100644:
            _fail("UNSAFE_ZIP_ENTRY", f"ZIP contains a non-regular entry: {name}")
        if info.file_size < 0 or info.file_size > MAX_ENTRY_BYTES:
            _fail("ZIP_ENTRY_TOO_LARGE", f"ZIP entry exceeds the safety limit: {name}")
        total_size += info.file_size
        if total_size > MAX_UNCOMPRESSED_BYTES:
            _fail("ZIP_TOO_LARGE", "ZIP uncompressed size exceeds the safety limit")
        if info.file_size and (
            info.compress_size == 0 or info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        ):
            _fail("UNSAFE_COMPRESSION_RATIO", f"ZIP entry has an unsafe compression ratio: {name}")
        by_name[name] = info
        casefolded.add(folded)
    return by_name


def _manifest_entries(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw_entries = manifest.get("artifacts")
    if not isinstance(raw_entries, list):
        _fail("INVALID_ARTIFACT_INVENTORY", "manifest artifacts must be an array")
    entries: list[dict[str, Any]] = []
    paths: list[str] = []
    casefolded: set[str] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict) or frozenset(raw_entry) != _ARTIFACT_KEYS:
            _fail(
                "INVALID_ARTIFACT_INVENTORY",
                "manifest artifact entries must use the exact v5 field set",
            )
        path = _validate_path(raw_entry["path"])
        if path == MANIFEST_PATH:
            _fail("INVALID_ARTIFACT_INVENTORY", "manifest.json cannot inventory itself")
        media_type = raw_entry["media_type"]
        role = raw_entry["role"]
        size = raw_entry["size_bytes"]
        digest = raw_entry["sha256"]
        if not isinstance(media_type, str) or not media_type.strip():
            _fail("INVALID_ARTIFACT_INVENTORY", f"artifact has invalid media type: {path}")
        if not isinstance(role, str) or not role.strip():
            _fail("INVALID_ARTIFACT_INVENTORY", f"artifact has invalid role: {path}")
        if type(size) is not int or size < 0 or size > MAX_ENTRY_BYTES:
            _fail("INVALID_ARTIFACT_INVENTORY", f"artifact has invalid byte size: {path}")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            _fail("INVALID_ARTIFACT_INVENTORY", f"artifact has invalid SHA-256: {path}")
        if path in paths or path.casefold() in casefolded:
            _fail("DUPLICATE_MANIFEST_PATH", f"manifest repeats an artifact path: {path}")
        paths.append(path)
        casefolded.add(path.casefold())
        entries.append(dict(raw_entry))
    if paths != sorted(paths):
        _fail("NON_CANONICAL_ARTIFACT_INVENTORY", "manifest artifact paths are not sorted")
    return tuple(entries)


def _parse_canonical_artifact_object(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload)
    except (UnicodeError, ValueError, RecursionError) as exc:
        _fail("INVALID_ARTIFACT_JSON", f"{label} is not strict UTF-8 JSON: {exc}")
    if not isinstance(parsed, dict):
        _fail("INVALID_ARTIFACT_JSON", f"{label} must contain one JSON object")
    if _canonical_json_bytes(parsed) != payload:
        _fail("NON_CANONICAL_ARTIFACT_JSON", f"{label} is not canonical UTF-8 JSON")
    return parsed


def _validate_joint_retention_sidecar(
    archive: zipfile.ZipFile,
    entries: Sequence[Mapping[str, Any]],
) -> None:
    """Bind historical evidence bytes without claiming current authenticity."""

    design_entries = [
        entry
        for entry in entries
        if str(entry["path"]).casefold() == FROZEN_DESIGN_SPEC_PATH.casefold()
        or str(entry["role"]).casefold() == FROZEN_DESIGN_SPEC_ROLE.casefold()
    ]
    if len(design_entries) != 1 or (
        design_entries[0]["path"],
        design_entries[0]["role"],
        design_entries[0]["media_type"],
    ) != (
        FROZEN_DESIGN_SPEC_PATH,
        FROZEN_DESIGN_SPEC_ROLE,
        FROZEN_DESIGN_SPEC_MEDIA_TYPE,
    ):
        _fail(
            "INVALID_FROZEN_DESIGN_BINDING",
            "manifest must bind one canonical frozen DesignSpec",
        )
    try:
        frozen_payload = archive.read(FROZEN_DESIGN_SPEC_PATH)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        _fail("INVALID_FROZEN_DESIGN_BINDING", f"cannot read frozen DesignSpec: {exc}")
    frozen = _parse_canonical_artifact_object(
        frozen_payload,
        label="frozen DesignSpec",
    )
    spec = frozen.get("spec")
    if (
        frozenset(frozen) != {"schema_version", "spec"}
        or frozen.get("schema_version") != FROZEN_DESIGN_SPEC_SCHEMA_VERSION
        or not isinstance(spec, Mapping)
    ):
        _fail(
            "INVALID_FROZEN_DESIGN_BINDING",
            "frozen DesignSpec has an unsupported structure",
        )
    retention = spec.get("joint_retention")
    evidence_entries = [
        entry
        for entry in entries
        if str(entry["path"]).casefold() == JOINT_RETENTION_SIGNED_EVIDENCE_PATH.casefold()
        or str(entry["role"]).casefold() == JOINT_RETENTION_SIGNED_EVIDENCE_ROLE.casefold()
    ]
    if retention is None:
        if evidence_entries:
            _fail(
                "UNEXPECTED_RETENTION_EVIDENCE",
                "unbound frozen DesignSpec cannot contain signed retention evidence",
            )
        return
    if not isinstance(retention, Mapping):
        _fail(
            "INVALID_RETENTION_CONTRACT",
            "frozen joint-retention contract must be an object",
        )
    expected_sha256 = retention.get("evidence_sha256")
    if not isinstance(expected_sha256, str) or _DIGEST.fullmatch(expected_sha256) is None:
        _fail(
            "INVALID_RETENTION_CONTRACT",
            "frozen joint-retention evidence SHA-256 is invalid",
        )
    if len(evidence_entries) != 1 or (
        evidence_entries[0]["path"],
        evidence_entries[0]["role"],
        evidence_entries[0]["media_type"],
    ) != (
        JOINT_RETENTION_SIGNED_EVIDENCE_PATH,
        JOINT_RETENTION_SIGNED_EVIDENCE_ROLE,
        JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE,
    ):
        _fail(
            "INVALID_RETENTION_EVIDENCE_BINDING",
            "retention-bound DesignSpec requires one canonical signed evidence artifact",
        )
    evidence_entry = evidence_entries[0]
    if evidence_entry["sha256"] != expected_sha256:
        _fail(
            "RETENTION_EVIDENCE_SHA256_MISMATCH",
            "signed retention evidence differs from the frozen retention contract",
        )
    try:
        evidence_bytes = archive.read(JOINT_RETENTION_SIGNED_EVIDENCE_PATH)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        _fail("INVALID_RETENTION_EVIDENCE_BINDING", f"cannot read signed evidence: {exc}")
    if hashlib.sha256(evidence_bytes).hexdigest() != expected_sha256:
        _fail(
            "RETENTION_EVIDENCE_SHA256_MISMATCH",
            "signed retention evidence differs from the frozen retention contract",
        )


def _validate_manifest_claims(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        _fail("UNSUPPORTED_MANIFEST_SCHEMA", "only custombuild.production-manifest.v5 is supported")
    if (
        manifest.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION
        or manifest.get("release_scope") != "design_review"
        or manifest.get("machine_use") != "validation_only"
        or manifest.get("physical_cutting_authorized") is not False
        or manifest.get("checksum_scope") != MANIFEST_CHECKSUM_SCOPE
    ):
        _fail("UNSAFE_MANIFEST_CLAIM", "manifest contains unsafe or unsupported package claims")
    if not isinstance(manifest.get("project_id"), str) or not manifest["project_id"]:
        _fail("INVALID_PACKAGE_IDENTITY", "manifest project_id must be a non-empty string")
    if not isinstance(manifest.get("revision"), str) or not manifest["revision"]:
        _fail("INVALID_PACKAGE_IDENTITY", "manifest revision must be a non-empty string")
    design_hash = manifest.get("design_hash")
    if not isinstance(design_hash, str) or _DIGEST.fullmatch(design_hash) is None:
        _fail("INVALID_PACKAGE_IDENTITY", "manifest design_hash must be a lowercase SHA-256")
    production_hash = manifest.get("production_context_hash")
    if not isinstance(production_hash, str) or _DIGEST.fullmatch(production_hash) is None:
        _fail("INVALID_CONTEXT_HASH", "manifest production_context_hash is invalid")
    try:
        context = {field: manifest[field] for field in _MANIFEST_CONTEXT_FIELDS}
    except KeyError as exc:
        _fail("INVALID_MANIFEST_STRUCTURE", f"manifest context field is missing: {exc.args[0]}")
    expected_hash = hashlib.sha256(_canonical_json_bytes(context)).hexdigest()
    if production_hash != expected_hash:
        _fail(
            "CONTEXT_HASH_MISMATCH",
            "manifest production_context_hash does not match its context",
        )


def _validate_expected_identity(
    manifest: Mapping[str, Any],
    *,
    project_id: str | None,
    revision: str | None,
    design_hash: str | None,
) -> None:
    expected = {
        "project_id": project_id,
        "revision": revision,
        "design_hash": design_hash,
    }
    for field, value in expected.items():
        if value is not None and manifest.get(field) != value:
            _fail(
                "EXPECTED_IDENTITY_MISMATCH",
                f"expected {field}={value!r}, manifest contains {manifest.get(field)!r}",
            )


def verify_package(
    package_path: Path,
    *,
    expected_bundle_sha256: str,
    expected_project_id: str | None = None,
    expected_revision: str | None = None,
    expected_design_hash: str | None = None,
) -> dict[str, Any]:
    """Verify one v5 ZIP without extracting it or importing project code."""

    if (
        not isinstance(expected_bundle_sha256, str)
        or _DIGEST.fullmatch(expected_bundle_sha256) is None
    ):
        _fail("INVALID_ARGUMENTS", "expected bundle hash must be a lowercase SHA-256")
    try:
        with package_path.open("rb") as package_stream:
            package_bytes = package_stream.read(MAX_PACKAGE_BYTES)
            exceeds_size_limit = bool(package_stream.read(1))
    except OSError as exc:
        _fail("PACKAGE_IO_ERROR", f"cannot read package: {exc}")
    if not package_bytes or exceeds_size_limit:
        _fail("PACKAGE_SIZE_INVALID", "package is empty or exceeds the 32 MiB safety limit")

    bundle_sha256 = hashlib.sha256(package_bytes).hexdigest()
    if bundle_sha256 != expected_bundle_sha256:
        _fail(
            "BUNDLE_SHA256_MISMATCH",
            "package ZIP SHA-256 differs from the independently supplied expected bundle hash",
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(package_bytes), mode="r")
    except (OSError, zipfile.BadZipFile) as exc:
        _fail("INVALID_ZIP", f"package is not a readable ZIP: {exc}")
    with archive:
        files = _validate_zip_metadata(archive)
        manifest_info = files.get(MANIFEST_PATH)
        if manifest_info is None:
            _fail("MISSING_MANIFEST", "ZIP does not contain manifest.json")
        if manifest_info.file_size > MAX_MANIFEST_BYTES:
            _fail("MANIFEST_TOO_LARGE", "manifest.json exceeds the safety limit")
        try:
            manifest_bytes = archive.read(manifest_info)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            _fail("UNREADABLE_MANIFEST", f"cannot read manifest.json: {exc}")
        manifest = _parse_manifest(manifest_bytes)
        _validate_manifest_claims(manifest)
        _validate_expected_identity(
            manifest,
            project_id=expected_project_id,
            revision=expected_revision,
            design_hash=expected_design_hash,
        )
        entries = _manifest_entries(manifest)
        expected_paths = {MANIFEST_PATH, *(entry["path"] for entry in entries)}
        actual_paths = set(files)
        missing = sorted(expected_paths - actual_paths)
        extra = sorted(actual_paths - expected_paths)
        if missing:
            _fail("MISSING_ARTIFACT", f"ZIP is missing manifest artifact: {missing[0]}")
        if extra:
            _fail("UNLISTED_ARTIFACT", f"ZIP contains an unlisted artifact: {extra[0]}")
        for entry in entries:
            path = entry["path"]
            info = files[path]
            if info.file_size != entry["size_bytes"]:
                _fail("ARTIFACT_SIZE_MISMATCH", f"artifact byte size differs: {path}")
            actual_size, actual_digest = _sha256_stream(
                archive,
                info,
                expected_size=entry["size_bytes"],
            )
            if actual_size != entry["size_bytes"]:
                _fail("ARTIFACT_SIZE_MISMATCH", f"artifact byte size differs: {path}")
            if actual_digest != entry["sha256"]:
                _fail("ARTIFACT_SHA256_MISMATCH", f"artifact SHA-256 differs: {path}")
        _validate_joint_retention_sidecar(archive, entries)

    return {
        "bundle_sha256": bundle_sha256,
        "external_bundle_sha256_match": True,
        "identity": {
            "design_hash": manifest["design_hash"],
            "project_id": manifest["project_id"],
            "revision": manifest["revision"],
        },
        "manifest_schema_version": manifest["schema_version"],
        "verified_artifact_count": len(entries),
    }


def _report(
    *,
    status: str,
    exit_code: int,
    package_name: str,
    error_code: str | None = None,
    message: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "authenticity": "NOT_AUTHENTICATED",
        "checksums_verified": status == "PASS",
        "details": None if details is None else dict(details),
        "error": (
            None
            if error_code is None
            else {
                "code": error_code,
                "message": message,
            }
        ),
        "exit_code": exit_code,
        "package": package_name,
        "physical_cutting_authorized": False,
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "verifier_version": VERIFIER_VERSION,
        "warnings": list(_BOUNDARY_WARNINGS),
    }


def _write_report(report: Mapping[str, Any], stream: TextIO) -> None:
    stream.write(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def _parser() -> StructuredArgumentParser:
    parser = StructuredArgumentParser(
        prog="python3 -I /trusted/verify_production_package.py <custombuild-package>.zip",
        description=(
            "Verify a Custombuild production-manifest v5 ZIP without extracting it. "
            "The ZIP is untrusted data: a PASS proves internal consistency only, not "
            "publisher authenticity or physical-cutting authorization."
        ),
    )
    parser.add_argument(
        "package",
        help="untrusted package ZIP to verify as data",
    )
    parser.add_argument(
        "--expect-bundle-sha256",
        dest="expected_bundle_sha256",
        required=True,
        help=(
            "lowercase SHA-256 of the exact ZIP obtained from an authenticated, out-of-band source"
        ),
    )
    parser.add_argument("--expect-project-id", dest="expected_project_id")
    parser.add_argument("--expect-revision", dest="expected_revision")
    parser.add_argument("--expect-design-hash", dest="expected_design_hash")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
) -> int:
    """Run the structured command-line verifier and return its stable exit code."""

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    package_name = ""
    try:
        args = _parser().parse_args(arguments)
        package_path = Path(args.package)
        package_name = package_path.name
        if _DIGEST.fullmatch(args.expected_bundle_sha256) is None:
            _fail("INVALID_ARGUMENTS", "expected bundle hash must be a lowercase SHA-256")
        for name, value in (
            ("project ID", args.expected_project_id),
            ("revision", args.expected_revision),
        ):
            if value is not None and not value:
                _fail("INVALID_ARGUMENTS", f"expected {name} cannot be empty")
        if (
            args.expected_design_hash is not None
            and _DIGEST.fullmatch(args.expected_design_hash) is None
        ):
            _fail("INVALID_ARGUMENTS", "expected design hash must be a lowercase SHA-256")
        details = verify_package(
            package_path,
            expected_bundle_sha256=args.expected_bundle_sha256,
            expected_project_id=args.expected_project_id,
            expected_revision=args.expected_revision,
            expected_design_hash=args.expected_design_hash,
        )
    except VerificationFailure as exc:
        exit_code = 2 if exc.code == "INVALID_ARGUMENTS" else 3
        _write_report(
            _report(
                status="FAIL",
                exit_code=exit_code,
                package_name=package_name,
                error_code=exc.code,
                message=str(exc),
            ),
            stdout,
        )
        return exit_code
    except (OSError, RuntimeError, zipfile.BadZipFile):
        _write_report(
            _report(
                status="FAIL",
                exit_code=4,
                package_name=package_name,
                error_code="UNEXPECTED_IO_ERROR",
                message="package verification failed because the ZIP could not be read",
            ),
            stdout,
        )
        return 4

    _write_report(
        _report(
            status="PASS",
            exit_code=0,
            package_name=package_name,
            details=details,
        ),
        stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
