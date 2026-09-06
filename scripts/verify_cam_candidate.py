"""Read-only intake verifier for a transferred CAM-candidate sidecar."""

# Repository-path bootstrapping intentionally precedes local package imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
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

from custombuild_manufacturing.artifact_limits import MAX_ARTIFACT_BYTES
from custombuild_manufacturing.cam_candidate_package import (
    read_and_verify_cam_candidate_package,
)
from custombuild_manufacturing.cam_software_provenance import (
    cam_software_provenance_sha256,
    validate_cam_software_provenance,
)
from custombuild_manufacturing.errors import ManufacturingError
from custombuild_manufacturing.model import canonical_json_bytes, sha256_hex

from scripts.compile_cam_candidate import CAMCandidateCompileError, _read_bounded_regular_file
from scripts.source_manifest import SourceManifestError, build_source_manifest

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class CAMCandidateVerificationError(RuntimeError):
    """A transferred candidate does not match its expected immutable identity."""


@dataclass(frozen=True, slots=True)
class CAMCandidateVerificationReceipt:
    status: str
    mode: str
    candidate_sha256: str
    candidate_size_bytes: int
    design_review_bundle_sha256: str
    candidate_context_hash: str
    production_profile_payload_sha256: str
    toolpaths_sha256: str
    program_count: int
    software_provenance: dict[str, Any]
    software_provenance_sha256: str
    verifier_source_manifest_sha256: str
    physical_cutting_authorized: bool
    workshop_acceptance_required: bool

    def to_json(self) -> bytes:
        return canonical_json_bytes(self)


def verify_cam_candidate(
    candidate_bundle: bytes,
    design_review_bundle: bytes,
    *,
    expected_candidate_sha256: str,
    expected_producer_source_manifest_sha256: str,
    expected_verifier_source_manifest_sha256: str,
    allow_test_only: bool = False,
) -> CAMCandidateVerificationReceipt:
    """Verify transfer identity, base binding and every candidate payload."""

    if _SHA256_PATTERN.fullmatch(expected_candidate_sha256) is None:
        raise CAMCandidateVerificationError(
            "expected candidate SHA-256 must be 64 lowercase hexadecimal characters"
        )
    if _SHA256_PATTERN.fullmatch(expected_producer_source_manifest_sha256) is None:
        raise CAMCandidateVerificationError(
            "expected producer source-manifest SHA-256 must be 64 lowercase hexadecimal characters"
        )
    if _SHA256_PATTERN.fullmatch(expected_verifier_source_manifest_sha256) is None:
        raise CAMCandidateVerificationError(
            "expected verifier source-manifest SHA-256 must be 64 lowercase hexadecimal characters"
        )
    if not candidate_bundle or len(candidate_bundle) > MAX_ARTIFACT_BYTES:
        raise CAMCandidateVerificationError("CAM candidate has an invalid byte size")
    if not design_review_bundle or len(design_review_bundle) > MAX_ARTIFACT_BYTES:
        raise CAMCandidateVerificationError("design-review bundle has an invalid byte size")
    actual_candidate_sha256 = sha256_hex(candidate_bundle)
    if actual_candidate_sha256 != expected_candidate_sha256:
        raise CAMCandidateVerificationError(
            "CAM candidate SHA-256 differs from the independently supplied expected digest"
        )
    verifier_source_manifest_sha256 = _current_verifier_source_manifest_sha256()
    if verifier_source_manifest_sha256 != expected_verifier_source_manifest_sha256:
        raise CAMCandidateVerificationError(
            "verifier SOURCE_MANIFEST_SHA256 differs from the independently supplied code root"
        )

    manifest = read_and_verify_cam_candidate_package(
        candidate_bundle,
        base_design_review_bundle=design_review_bundle,
        expected_producer_source_manifest_sha256=(expected_producer_source_manifest_sha256),
        allow_test_only=allow_test_only,
    )
    software_provenance = manifest["software_provenance"]
    producer_build = validate_cam_software_provenance(
        software_provenance,
        allow_test_only=allow_test_only,
    )
    if producer_build.source_manifest_sha256 != expected_producer_source_manifest_sha256:
        raise CAMCandidateVerificationError(
            "CAM candidate producer source-manifest SHA-256 differs from the expected code root"
        )
    artifacts = manifest["artifacts"]
    program_count = sum(entry["role"] == "EXECUTABLE_CAM_CANDIDATE_PROGRAM" for entry in artifacts)
    production_profile = manifest["production_profile"]
    toolpaths = manifest["toolpaths"]
    return CAMCandidateVerificationReceipt(
        status=str(manifest["status"]),
        mode=str(manifest["mode"]),
        candidate_sha256=actual_candidate_sha256,
        candidate_size_bytes=len(candidate_bundle),
        design_review_bundle_sha256=sha256_hex(design_review_bundle),
        candidate_context_hash=str(manifest["candidate_context_hash"]),
        production_profile_payload_sha256=str(production_profile["payload_sha256"]),
        toolpaths_sha256=str(toolpaths["fingerprint"]),
        program_count=program_count,
        software_provenance=software_provenance,
        software_provenance_sha256=cam_software_provenance_sha256(
            software_provenance,
            allow_test_only=allow_test_only,
        ),
        verifier_source_manifest_sha256=verifier_source_manifest_sha256,
        physical_cutting_authorized=False,
        workshop_acceptance_required=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Strictly verify a transferred CAM-candidate ZIP against its immutable "
            "design-review ZIP and an independently supplied candidate SHA-256."
        )
    )
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--design-review", required=True, type=Path)
    parser.add_argument("--expect-candidate-sha256", required=True)
    parser.add_argument("--expect-producer-source-manifest-sha256", required=True)
    parser.add_argument("--expect-verifier-source-manifest-sha256", required=True)
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
        candidate = _read_bounded_regular_file(
            arguments.candidate,
            label="CAM candidate",
            limit=MAX_ARTIFACT_BYTES,
        )
        design_review = _read_bounded_regular_file(
            arguments.design_review,
            label="design-review bundle",
            limit=MAX_ARTIFACT_BYTES,
        )
        receipt = verify_cam_candidate(
            candidate,
            design_review,
            expected_candidate_sha256=arguments.expect_candidate_sha256,
            expected_producer_source_manifest_sha256=(
                arguments.expect_producer_source_manifest_sha256
            ),
            expected_verifier_source_manifest_sha256=(
                arguments.expect_verifier_source_manifest_sha256
            ),
            allow_test_only=arguments.allow_test_only,
        )
    except (
        CAMCandidateCompileError,
        CAMCandidateVerificationError,
        ManufacturingError,
        TypeError,
        ValueError,
    ) as exc:
        parser.exit(2, f"CAM candidate verification blocked: {exc}\n")
    sys.stdout.buffer.write(receipt.to_json() + b"\n")
    return 0


def _current_verifier_source_manifest_sha256() -> str:
    try:
        return build_source_manifest(_REPOSITORY_ROOT)[2]
    except (OSError, SourceManifestError) as exc:
        raise CAMCandidateVerificationError(
            "cannot establish the local verifier SOURCE_MANIFEST_SHA256"
        ) from exc


if __name__ == "__main__":
    raise SystemExit(main())
