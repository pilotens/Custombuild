"""Offline compiler for one checksum-bound executable CAM candidate.

The input design-review archive remains immutable.  This command combines its
strictly verified machine-neutral operations with one separately supplied,
server/workshop-owned production profile and writes a sidecar ZIP.  Successful
compilation does not authorize starting a physical machine.
"""

# Repository-path bootstrapping intentionally precedes local package imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for _source_root in (
    _REPOSITORY_ROOT,
    _REPOSITORY_ROOT / "packages/domain/src",
    _REPOSITORY_ROOT / "packages/rule-engine/src",
    _REPOSITORY_ROOT / "packages/manufacturing/src",
    _REPOSITORY_ROOT / "packages/template-sdk/src",
    _REPOSITORY_ROOT / "cad/src",
    _REPOSITORY_ROOT / "cam/src",
    _REPOSITORY_ROOT / "postprocessors/src",
):
    _source_path = str(_source_root)
    if _source_path not in sys.path:
        sys.path.insert(0, _source_path)

from custombuild_cam import generate_production_toolpaths
from custombuild_manufacturing.artifact_limits import MAX_ARTIFACT_BYTES
from custombuild_manufacturing.cam_candidate_package import (
    CAMCandidateBundle,
    build_cam_candidate_bundle,
    read_and_verify_cam_candidate_package,
    read_operations_document_from_design_review_bundle,
)
from custombuild_manufacturing.cam_software_provenance import (
    CAMSoftwareProvenanceError,
    ProducerBuildIdentity,
    cam_software_provenance_sha256,
    parse_producer_build_identity,
    validate_cam_software_provenance,
)
from custombuild_manufacturing.errors import ManufacturingError
from custombuild_manufacturing.model import canonical_json_bytes, sha256_hex
from custombuild_manufacturing.production_machine_profile import (
    MAX_PRODUCTION_MACHINE_PROFILE_BYTES,
    TEST_ONLY_PROFILE,
    load_production_machine_profile,
)
from custombuild_postprocessors import LinuxCNCProductionPostprocessor

from scripts.source_manifest import SourceManifestError, build_source_manifest


class CAMCandidateCompileError(RuntimeError):
    """The offline compiler could not produce a fully verified candidate."""


@dataclass(frozen=True, slots=True)
class CAMCandidateCompileReceipt:
    """Small immutable receipt suitable for an operator log."""

    status: str
    mode: str
    candidate_context_hash: str
    design_review_bundle_sha256: str
    production_profile_payload_sha256: str
    toolpaths_sha256: str
    bundle_sha256: str
    bundle_size_bytes: int
    program_count: int
    software_provenance: dict[str, Any]
    software_provenance_sha256: str
    physical_cutting_authorized: bool
    workshop_acceptance_required: bool

    def to_json(self) -> bytes:
        return canonical_json_bytes(self)


def compile_cam_candidate(
    design_review_bundle: bytes,
    production_profile_document: bytes,
    *,
    producer_build_identity: ProducerBuildIdentity | Mapping[str, Any] | None = None,
    allow_test_only: bool = False,
) -> tuple[CAMCandidateBundle, CAMCandidateCompileReceipt]:
    """Compile and round-trip verify one design-review sidecar.

    ``allow_test_only`` is intentionally explicit and defaults to false.  It is
    useful for simulation/CI fixtures but must never be enabled by a production
    deployment.
    """

    if not design_review_bundle or len(design_review_bundle) > MAX_ARTIFACT_BYTES:
        raise CAMCandidateCompileError("design-review bundle has an invalid byte size")
    if (
        not production_profile_document
        or len(production_profile_document) > MAX_PRODUCTION_MACHINE_PROFILE_BYTES
    ):
        raise CAMCandidateCompileError("production profile has an invalid byte size")

    source = read_operations_document_from_design_review_bundle(design_review_bundle)
    profile = load_production_machine_profile(
        production_profile_document,
        allow_test_only=allow_test_only,
    )
    test_only_profile = profile.profile_class == TEST_ONLY_PROFILE
    if producer_build_identity is None:
        if not (allow_test_only and test_only_profile):
            raise CAMCandidateCompileError(
                "production CAM requires an independently supplied producer build identity"
            )
        parsed_producer_build_identity = None
    else:
        try:
            parsed_producer_build_identity = parse_producer_build_identity(
                (
                    producer_build_identity.as_dict()
                    if isinstance(producer_build_identity, ProducerBuildIdentity)
                    else producer_build_identity
                ),
                allow_test_only=test_only_profile,
            )
        except CAMSoftwareProvenanceError as exc:
            raise CAMCandidateCompileError("producer build identity is invalid") from exc
        if not test_only_profile:
            _require_identity_matches_local_source(parsed_producer_build_identity)
    toolpaths = generate_production_toolpaths(source, profile.execution_context)
    programs = LinuxCNCProductionPostprocessor(profile.postprocessor_profile).generate(toolpaths)
    candidate = build_cam_candidate_bundle(
        design_review_bundle,
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
        producer_build_identity=parsed_producer_build_identity,
    )
    software_provenance = candidate.manifest["software_provenance"]
    embedded_producer_build = validate_cam_software_provenance(
        software_provenance,
        allow_test_only=allow_test_only,
    )
    verified_manifest = read_and_verify_cam_candidate_package(
        candidate.zip_bytes,
        base_design_review_bundle=design_review_bundle,
        expected_producer_source_manifest_sha256=(embedded_producer_build.source_manifest_sha256),
        allow_test_only=allow_test_only,
    )
    if verified_manifest != candidate.manifest:
        raise CAMCandidateCompileError(
            "CAM candidate round-trip manifest differs from the generated manifest"
        )
    manifest = candidate.manifest
    receipt = CAMCandidateCompileReceipt(
        status=str(manifest["status"]),
        mode=str(manifest["mode"]),
        candidate_context_hash=str(manifest["candidate_context_hash"]),
        design_review_bundle_sha256=sha256_hex(design_review_bundle),
        production_profile_payload_sha256=profile.payload_sha256,
        toolpaths_sha256=toolpaths.fingerprint,
        bundle_sha256=sha256_hex(candidate.zip_bytes),
        bundle_size_bytes=len(candidate.zip_bytes),
        program_count=len(programs),
        software_provenance=software_provenance,
        software_provenance_sha256=cam_software_provenance_sha256(
            software_provenance,
            allow_test_only=allow_test_only,
        ),
        physical_cutting_authorized=False,
        workshop_acceptance_required=True,
    )
    return candidate, receipt


def _read_bounded_regular_file(path: Path, *, label: str, limit: int) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise CAMCandidateCompileError(f"{label} must be a regular, non-symlink file")
        if not 1 <= before.st_size <= limit:
            raise CAMCandidateCompileError(f"{label} has an invalid byte size")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            payload = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise CAMCandidateCompileError(
            f"{label} must be a readable regular, non-symlink file: {exc}"
        ) from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_after != identity_before or len(payload) != before.st_size:
        raise CAMCandidateCompileError(f"{label} changed while it was being read")
    return payload


def _write_new_file(path: Path, payload: bytes) -> None:
    """Write only to a previously absent regular path with owner-only mode."""

    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            with suppress(OSError):
                path.unlink()
        raise CAMCandidateCompileError(f"cannot create output file: {exc}") from exc


def _parse_producer_build_identity_document(payload: bytes) -> ProducerBuildIdentity:
    """Parse one byte-canonical, closed identity file supplied outside the review ZIP."""

    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CAMCandidateCompileError("producer build identity is not canonical JSON") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        raise CAMCandidateCompileError("producer build identity JSON is not byte-canonical")
    try:
        return parse_producer_build_identity(value)
    except CAMSoftwareProvenanceError as exc:
        raise CAMCandidateCompileError("producer build identity is invalid") from exc


def _require_identity_matches_local_source(identity: ProducerBuildIdentity) -> None:
    """Bind a production compiler claim to the exact source tree executing it."""

    try:
        source_manifest, _source_manifest_bytes, source_manifest_sha256 = build_source_manifest(
            _REPOSITORY_ROOT
        )
    except (OSError, SourceManifestError) as exc:
        raise CAMCandidateCompileError(
            "cannot establish the local compiler SOURCE_MANIFEST_SHA256"
        ) from exc
    if identity.source_manifest_sha256 != source_manifest_sha256:
        raise CAMCandidateCompileError(
            "producer SOURCE_MANIFEST_SHA256 differs from the local compiler code root"
        )
    lock_entries = [
        entry
        for entry in source_manifest.get("entries", ())
        if isinstance(entry, Mapping) and entry.get("path") == "uv.lock"
    ]
    if (
        len(lock_entries) != 1
        or lock_entries[0].get("type") != "file"
        or not isinstance(lock_entries[0].get("sha256"), str)
    ):
        raise CAMCandidateCompileError(
            "local compiler source manifest has no unique dependency-lock binding"
        )
    dependency_lock_sha256 = lock_entries[0]["sha256"]
    if identity.dependency_lock_sha256 != dependency_lock_sha256:
        raise CAMCandidateCompileError(
            "producer dependency-lock SHA-256 differs from the local compiler lock"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compile a verified design-review ZIP and an exact production profile "
            "into a separate executable CAM-candidate ZIP."
        )
    )
    parser.add_argument("--design-review", required=True, type=Path)
    parser.add_argument("--production-profile", required=True, type=Path)
    parser.add_argument(
        "--producer-build-identity",
        type=Path,
        help=("canonical producer-build identity JSON; required outside explicit TEST_ONLY runs"),
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--allow-test-only",
        action="store_true",
        help="accept an explicitly TEST_ONLY profile for CI/simulation; never use in production",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        design_review = _read_bounded_regular_file(
            arguments.design_review,
            label="design-review bundle",
            limit=MAX_ARTIFACT_BYTES,
        )
        production_profile = _read_bounded_regular_file(
            arguments.production_profile,
            label="production profile",
            limit=MAX_PRODUCTION_MACHINE_PROFILE_BYTES,
        )
        if arguments.producer_build_identity is None:
            if not arguments.allow_test_only:
                raise CAMCandidateCompileError(
                    "--producer-build-identity is required outside explicit TEST_ONLY runs"
                )
            producer_build_identity = None
        else:
            identity_document = _read_bounded_regular_file(
                arguments.producer_build_identity,
                label="producer build identity",
                limit=16 * 1024,
            )
            producer_build_identity = _parse_producer_build_identity_document(identity_document)
        candidate, receipt = compile_cam_candidate(
            design_review,
            production_profile,
            producer_build_identity=producer_build_identity,
            allow_test_only=arguments.allow_test_only,
        )
        _write_new_file(arguments.output, candidate.zip_bytes)
    except (CAMCandidateCompileError, ManufacturingError, TypeError, ValueError) as exc:
        parser.exit(2, f"CAM candidate compilation blocked: {exc}\n")
    summary = json.loads(receipt.to_json())
    summary["output"] = str(arguments.output)
    sys.stdout.buffer.write(canonical_json_bytes(summary) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
