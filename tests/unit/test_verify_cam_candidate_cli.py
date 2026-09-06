from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from custombuild_manufacturing.cam_software_provenance import (
    build_cam_software_provenance,
    cam_software_provenance_sha256,
)
from custombuild_manufacturing.cam_software_provenance import (
    test_only_producer_build_identity as _test_only_producer_build_identity,
)
from custombuild_manufacturing.model import sha256_hex

from scripts import verify_cam_candidate as verifier

_SOFTWARE_PROVENANCE = build_cam_software_provenance(
    _test_only_producer_build_identity(),
    allow_test_only=True,
)
_SOFTWARE_PROVENANCE_SHA256 = cam_software_provenance_sha256(
    _SOFTWARE_PROVENANCE,
    allow_test_only=True,
)
_PRODUCER_SOURCE_MANIFEST_SHA256 = _SOFTWARE_PROVENANCE["code_root"]["sha256"]
_VERIFIER_SOURCE_MANIFEST_SHA256 = "f" * 64


def _manifest() -> dict[str, Any]:
    return {
        "artifacts": [
            {"role": "EXECUTABLE_CAM_CANDIDATE_PROGRAM"},
            {"role": "EXECUTABLE_CAM_CANDIDATE_PROGRAM"},
            {"role": "CUTTING_PROGRAM_VALIDATION_REPORT"},
        ],
        "candidate_context_hash": "1" * 64,
        "mode": "EXECUTABLE_CAM_CANDIDATE",
        "physical_cutting_authorized": False,
        "production_profile": {"payload_sha256": "2" * 64},
        "software_provenance": _SOFTWARE_PROVENANCE,
        "status": "CUTTING_CANDIDATE_GENERATED",
        "toolpaths": {"fingerprint": "3" * 64},
        "workshop_acceptance_required": True,
    }


def test_verifier_binds_expected_transfer_digest_and_base_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = b"candidate"
    design_review = b"review"
    calls: list[tuple[bytes, bytes, str, bool]] = []

    def read_candidate(
        payload: bytes,
        *,
        base_design_review_bundle: bytes,
        expected_producer_source_manifest_sha256: str,
        allow_test_only: bool,
    ) -> dict[str, Any]:
        calls.append(
            (
                payload,
                base_design_review_bundle,
                expected_producer_source_manifest_sha256,
                allow_test_only,
            )
        )
        return _manifest()

    monkeypatch.setattr(
        verifier,
        "read_and_verify_cam_candidate_package",
        read_candidate,
    )
    monkeypatch.setattr(
        verifier,
        "_current_verifier_source_manifest_sha256",
        lambda: _VERIFIER_SOURCE_MANIFEST_SHA256,
    )

    receipt = verifier.verify_cam_candidate(
        candidate,
        design_review,
        expected_candidate_sha256=sha256_hex(candidate),
        expected_producer_source_manifest_sha256=(_PRODUCER_SOURCE_MANIFEST_SHA256),
        expected_verifier_source_manifest_sha256=_VERIFIER_SOURCE_MANIFEST_SHA256,
        allow_test_only=True,
    )

    assert calls == [(candidate, design_review, _PRODUCER_SOURCE_MANIFEST_SHA256, True)]
    assert receipt.program_count == 2
    assert receipt.candidate_sha256 == sha256_hex(candidate)
    assert receipt.design_review_bundle_sha256 == sha256_hex(design_review)
    assert receipt.physical_cutting_authorized is False
    assert receipt.workshop_acceptance_required is True


def test_transfer_digest_mismatch_blocks_before_archive_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def unexpected_reader(*args: object, **kwargs: object) -> dict[str, Any]:
        nonlocal called
        called = True
        return _manifest()

    monkeypatch.setattr(verifier, "read_and_verify_cam_candidate_package", unexpected_reader)

    with pytest.raises(verifier.CAMCandidateVerificationError, match="independently supplied"):
        verifier.verify_cam_candidate(
            b"candidate-after-transfer",
            b"review",
            expected_candidate_sha256=sha256_hex(b"candidate-before-transfer"),
            expected_producer_source_manifest_sha256=(_PRODUCER_SOURCE_MANIFEST_SHA256),
            expected_verifier_source_manifest_sha256=_VERIFIER_SOURCE_MANIFEST_SHA256,
        )
    assert called is False


def test_verifier_rejects_wrong_producer_or_local_verifier_code_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = b"candidate"
    design_review = b"review"

    def read_candidate(*args: object, **kwargs: object) -> dict[str, Any]:
        return _manifest()

    monkeypatch.setattr(verifier, "read_and_verify_cam_candidate_package", read_candidate)
    monkeypatch.setattr(
        verifier,
        "_current_verifier_source_manifest_sha256",
        lambda: _VERIFIER_SOURCE_MANIFEST_SHA256,
    )
    with pytest.raises(verifier.CAMCandidateVerificationError, match="producer source-manifest"):
        verifier.verify_cam_candidate(
            candidate,
            design_review,
            expected_candidate_sha256=sha256_hex(candidate),
            expected_producer_source_manifest_sha256="0" * 64,
            expected_verifier_source_manifest_sha256=_VERIFIER_SOURCE_MANIFEST_SHA256,
            allow_test_only=True,
        )
    with pytest.raises(verifier.CAMCandidateVerificationError, match="verifier SOURCE_MANIFEST"):
        verifier.verify_cam_candidate(
            candidate,
            design_review,
            expected_candidate_sha256=sha256_hex(candidate),
            expected_producer_source_manifest_sha256=(_PRODUCER_SOURCE_MANIFEST_SHA256),
            expected_verifier_source_manifest_sha256="0" * 64,
            allow_test_only=True,
        )


@pytest.mark.parametrize("digest", ["A" * 64, "0" * 63, "g" * 64, ""])
def test_expected_digest_must_be_canonical(digest: str) -> None:
    with pytest.raises(verifier.CAMCandidateVerificationError, match="64 lowercase"):
        verifier.verify_cam_candidate(
            b"candidate",
            b"review",
            expected_candidate_sha256=digest,
            expected_producer_source_manifest_sha256=(_PRODUCER_SOURCE_MANIFEST_SHA256),
            expected_verifier_source_manifest_sha256=_VERIFIER_SOURCE_MANIFEST_SHA256,
        )


def test_cli_defaults_to_production_reader_and_writes_only_receipt_to_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    candidate_path = tmp_path / "candidate.zip"
    review_path = tmp_path / "design-review.zip"
    candidate_path.write_bytes(b"candidate")
    review_path.write_bytes(b"review")
    digest = sha256_hex(b"candidate")
    calls: list[tuple[bytes, bytes, str, str, str, bool]] = []
    receipt = verifier.CAMCandidateVerificationReceipt(
        status="CUTTING_CANDIDATE_GENERATED",
        mode="EXECUTABLE_CAM_CANDIDATE",
        candidate_sha256=digest,
        candidate_size_bytes=len(b"candidate"),
        design_review_bundle_sha256=sha256_hex(b"review"),
        candidate_context_hash="1" * 64,
        production_profile_payload_sha256="2" * 64,
        toolpaths_sha256="3" * 64,
        program_count=2,
        software_provenance=_SOFTWARE_PROVENANCE,
        software_provenance_sha256=_SOFTWARE_PROVENANCE_SHA256,
        verifier_source_manifest_sha256=_VERIFIER_SOURCE_MANIFEST_SHA256,
        physical_cutting_authorized=False,
        workshop_acceptance_required=True,
    )

    def verify(
        candidate: bytes,
        design_review: bytes,
        *,
        expected_candidate_sha256: str,
        expected_producer_source_manifest_sha256: str,
        expected_verifier_source_manifest_sha256: str,
        allow_test_only: bool,
    ) -> verifier.CAMCandidateVerificationReceipt:
        calls.append(
            (
                candidate,
                design_review,
                expected_candidate_sha256,
                expected_producer_source_manifest_sha256,
                expected_verifier_source_manifest_sha256,
                allow_test_only,
            )
        )
        return receipt

    monkeypatch.setattr(verifier, "verify_cam_candidate", verify)
    files_before = sorted(tmp_path.iterdir())

    assert (
        verifier.main(
            (
                "--candidate",
                str(candidate_path),
                "--design-review",
                str(review_path),
                "--expect-candidate-sha256",
                digest,
                "--expect-producer-source-manifest-sha256",
                _PRODUCER_SOURCE_MANIFEST_SHA256,
                "--expect-verifier-source-manifest-sha256",
                _VERIFIER_SOURCE_MANIFEST_SHA256,
            )
        )
        == 0
    )

    assert calls == [
        (
            b"candidate",
            b"review",
            digest,
            _PRODUCER_SOURCE_MANIFEST_SHA256,
            _VERIFIER_SOURCE_MANIFEST_SHA256,
            False,
        )
    ]
    assert sorted(tmp_path.iterdir()) == files_before
    output = json.loads(capsys.readouterr().out)
    assert output["candidate_sha256"] == digest
    assert output["physical_cutting_authorized"] is False
