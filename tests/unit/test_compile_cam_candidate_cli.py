from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from custombuild_manufacturing.cam_software_provenance import (
    build_cam_software_provenance,
    cam_software_provenance_sha256,
    parse_producer_build_identity,
)
from custombuild_manufacturing.cam_software_provenance import (
    test_only_producer_build_identity as _test_only_producer_build_identity,
)
from custombuild_manufacturing.model import canonical_json_bytes, sha256_hex

from scripts import compile_cam_candidate as compiler

_SOFTWARE_PROVENANCE = build_cam_software_provenance(
    _test_only_producer_build_identity(),
    allow_test_only=True,
)
_SOFTWARE_PROVENANCE_SHA256 = cam_software_provenance_sha256(
    _SOFTWARE_PROVENANCE,
    allow_test_only=True,
)


def test_bounded_input_rejects_symlink_and_exclusive_output_preserves_existing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    symlink = tmp_path / "source-link.bin"
    symlink.symlink_to(source)

    assert compiler._read_bounded_regular_file(source, label="source", limit=6) == b"source"
    with pytest.raises(compiler.CAMCandidateCompileError, match="non-symlink"):
        compiler._read_bounded_regular_file(symlink, label="source", limit=100)

    output = tmp_path / "candidate.zip"
    compiler._write_new_file(output, b"candidate")
    assert output.read_bytes() == b"candidate"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(compiler.CAMCandidateCompileError, match="cannot create output"):
        compiler._write_new_file(output, b"replacement")
    assert output.read_bytes() == b"candidate"


def test_producer_identity_document_is_closed_canonical_and_bound_to_local_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_sha = "a" * 64
    lock_sha = sha256_hex((compiler._REPOSITORY_ROOT / "uv.lock").read_bytes())
    value = {
        "schema_version": "custombuild.producer-build-identity.v1",
        "app_version": "1.0.0",
        "vcs_ref": "b" * 40,
        "source_manifest_sha256": source_sha,
        "dependency_lock_sha256": lock_sha,
    }
    identity = compiler._parse_producer_build_identity_document(canonical_json_bytes(value))
    monkeypatch.setattr(
        compiler,
        "build_source_manifest",
        lambda _root: (
            {
                "entries": [
                    {
                        "path": "uv.lock",
                        "type": "file",
                        "sha256": lock_sha,
                    }
                ]
            },
            b"manifest",
            source_sha,
        ),
    )
    compiler._require_identity_matches_local_source(identity)

    with pytest.raises(compiler.CAMCandidateCompileError, match="byte-canonical"):
        compiler._parse_producer_build_identity_document(
            json.dumps(value, indent=2).encode("utf-8")
        )

    changed_source = parse_producer_build_identity({**value, "source_manifest_sha256": "c" * 64})
    with pytest.raises(compiler.CAMCandidateCompileError, match="local compiler code root"):
        compiler._require_identity_matches_local_source(changed_source)

    changed_lock = parse_producer_build_identity({**value, "dependency_lock_sha256": "d" * 64})
    with pytest.raises(compiler.CAMCandidateCompileError, match="local compiler lock"):
        compiler._require_identity_matches_local_source(changed_lock)

    monkeypatch.setattr(
        compiler,
        "build_source_manifest",
        lambda _root: ({"entries": []}, b"manifest", source_sha),
    )
    with pytest.raises(compiler.CAMCandidateCompileError, match="no unique dependency-lock"):
        compiler._require_identity_matches_local_source(identity)


def test_compile_wires_verified_source_profile_toolpaths_post_and_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []
    source = object()
    context = object()
    post_profile = object()
    profile = SimpleNamespace(
        execution_context=context,
        postprocessor_profile=post_profile,
        payload_sha256="1" * 64,
        profile_class="TEST_ONLY",
    )
    toolpaths = SimpleNamespace(fingerprint="2" * 64)
    programs = (object(), object())
    manifest = {
        "status": "CUTTING_CANDIDATE_GENERATED",
        "mode": "EXECUTABLE_CAM_CANDIDATE",
        "candidate_context_hash": "3" * 64,
        "software_provenance": _SOFTWARE_PROVENANCE,
    }
    candidate = SimpleNamespace(zip_bytes=b"candidate-zip", manifest=manifest)

    def read_source(payload: bytes) -> object:
        calls.append(("source", payload))
        return source

    def load_profile(payload: bytes, *, allow_test_only: bool) -> SimpleNamespace:
        calls.append(("profile", (payload, allow_test_only)))
        return profile

    def generate_toolpaths(actual_source: object, actual_context: object) -> SimpleNamespace:
        calls.append(("toolpaths", (actual_source, actual_context)))
        return toolpaths

    monkeypatch.setattr(compiler, "read_operations_document_from_design_review_bundle", read_source)
    monkeypatch.setattr(compiler, "load_production_machine_profile", load_profile)
    monkeypatch.setattr(compiler, "generate_production_toolpaths", generate_toolpaths)

    class FakePostprocessor:
        def __init__(self, actual_profile: object) -> None:
            calls.append(("post_init", actual_profile))

        def generate(self, actual_toolpaths: object) -> tuple[object, ...]:
            calls.append(("post_generate", actual_toolpaths))
            return programs

    monkeypatch.setattr(compiler, "LinuxCNCProductionPostprocessor", FakePostprocessor)

    def build_bundle(payload: bytes, **kwargs: object) -> SimpleNamespace:
        calls.append(("bundle", (payload, kwargs)))
        return candidate

    def read_candidate(
        payload: bytes,
        *,
        base_design_review_bundle: bytes,
        expected_producer_source_manifest_sha256: str,
        allow_test_only: bool,
    ) -> dict[str, str]:
        calls.append(
            (
                "round_trip",
                (
                    payload,
                    base_design_review_bundle,
                    expected_producer_source_manifest_sha256,
                    allow_test_only,
                ),
            )
        )
        return manifest

    monkeypatch.setattr(compiler, "build_cam_candidate_bundle", build_bundle)
    monkeypatch.setattr(compiler, "read_and_verify_cam_candidate_package", read_candidate)

    built, receipt = compiler.compile_cam_candidate(
        b"design-review",
        b"production-profile",
        allow_test_only=True,
    )

    assert built is candidate
    assert receipt.status == "CUTTING_CANDIDATE_GENERATED"
    assert receipt.mode == "EXECUTABLE_CAM_CANDIDATE"
    assert receipt.program_count == 2
    assert receipt.physical_cutting_authorized is False
    assert receipt.workshop_acceptance_required is True
    assert [name for name, _value in calls] == [
        "source",
        "profile",
        "toolpaths",
        "post_init",
        "post_generate",
        "bundle",
        "round_trip",
    ]


def test_cli_emits_receipt_and_requires_new_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    design_review = tmp_path / "review.zip"
    profile = tmp_path / "profile.json"
    output = tmp_path / "candidate.zip"
    design_review.write_bytes(b"review")
    profile.write_bytes(b"profile")
    candidate = SimpleNamespace(zip_bytes=b"candidate")
    receipt = compiler.CAMCandidateCompileReceipt(
        status="CUTTING_CANDIDATE_GENERATED",
        mode="EXECUTABLE_CAM_CANDIDATE",
        candidate_context_hash="3" * 64,
        design_review_bundle_sha256="4" * 64,
        production_profile_payload_sha256="5" * 64,
        toolpaths_sha256="6" * 64,
        bundle_sha256="7" * 64,
        bundle_size_bytes=9,
        program_count=2,
        software_provenance=_SOFTWARE_PROVENANCE,
        software_provenance_sha256=_SOFTWARE_PROVENANCE_SHA256,
        physical_cutting_authorized=False,
        workshop_acceptance_required=True,
    )
    monkeypatch.setattr(
        compiler,
        "compile_cam_candidate",
        lambda *args, **kwargs: (candidate, receipt),
    )

    assert (
        compiler.main(
            (
                "--design-review",
                str(design_review),
                "--production-profile",
                str(profile),
                "--output",
                str(output),
                "--allow-test-only",
            )
        )
        == 0
    )
    assert output.read_bytes() == b"candidate"
    summary = json.loads(capsys.readouterr().out)
    assert summary["output"] == str(output)
    assert summary["physical_cutting_authorized"] is False

    with pytest.raises(SystemExit) as exc:
        compiler.main(
            (
                "--design-review",
                str(design_review),
                "--production-profile",
                str(profile),
                "--output",
                str(output),
            )
        )
    assert exc.value.code == 2
    assert output.read_bytes() == b"candidate"
