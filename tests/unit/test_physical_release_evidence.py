from __future__ import annotations

import pytest

from custombuild_manufacturing.physical_release import (
    REQUIRED_PHYSICAL_EVIDENCE_KINDS,
    PhysicalEvidenceKind,
    PhysicalEvidenceRecord,
    PhysicalReleaseEvidence,
    physical_release_evidence_complete,
)


def digest(char: str) -> str:
    return char * 64


def complete_evidence() -> PhysicalReleaseEvidence:
    records = tuple(
        PhysicalEvidenceRecord(
            evidence_id=f"ev-{kind.value}",
            kind=kind,
            revision="1",
            issuer="qualified-reviewer",
            issued_at="2026-08-28T08:00:00Z",
            subject_sha256=digest("a"),
            document_sha256=digest("b"),
        )
        for kind in REQUIRED_PHYSICAL_EVIDENCE_KINDS
    )
    return PhysicalReleaseEvidence(
        design_sha256=digest("c"),
        generation_context_sha256=digest("d"),
        machine_profile_sha256=digest("e"),
        material_catalog_sha256=digest("f"),
        records=records,
    )


def test_complete_physical_evidence_is_deterministic_and_complete() -> None:
    evidence = complete_evidence()
    assert physical_release_evidence_complete(evidence) is True
    assert evidence.fingerprint() == evidence.fingerprint()
    assert [item["kind"] for item in evidence.as_dict()["records"]] == sorted(
        kind.value for kind in REQUIRED_PHYSICAL_EVIDENCE_KINDS
    )


def test_missing_physical_evidence_fails_closed() -> None:
    evidence = complete_evidence()
    incomplete = PhysicalReleaseEvidence(
        design_sha256=evidence.design_sha256,
        generation_context_sha256=evidence.generation_context_sha256,
        machine_profile_sha256=evidence.machine_profile_sha256,
        material_catalog_sha256=evidence.material_catalog_sha256,
        records=tuple(
            record for record in evidence.records if record.kind is not PhysicalEvidenceKind.LOAD_TEST
        ),
    )
    assert physical_release_evidence_complete(incomplete) is False
    with pytest.raises(ValueError, match="load_test"):
        incomplete.validate()


def test_duplicate_kind_and_bad_hash_are_rejected() -> None:
    evidence = complete_evidence()
    duplicate = PhysicalReleaseEvidence(
        design_sha256=evidence.design_sha256,
        generation_context_sha256=evidence.generation_context_sha256,
        machine_profile_sha256=evidence.machine_profile_sha256,
        material_catalog_sha256=evidence.material_catalog_sha256,
        records=evidence.records + (evidence.records[0],),
    )
    with pytest.raises(ValueError, match="duplicate physical evidence id"):
        duplicate.validate()

    bad = PhysicalReleaseEvidence(
        design_sha256="not-a-digest",
        generation_context_sha256=evidence.generation_context_sha256,
        machine_profile_sha256=evidence.machine_profile_sha256,
        material_catalog_sha256=evidence.material_catalog_sha256,
        records=evidence.records,
    )
    assert physical_release_evidence_complete(bad) is False


def test_none_never_counts_as_complete_physical_evidence() -> None:
    assert physical_release_evidence_complete(None) is False
