from __future__ import annotations

import pytest

from custombuild_manufacturing.physical_release import (
    PhysicalEvidenceKind,
    PhysicalEvidenceRecord,
    PhysicalReleaseEvidence,
    REQUIRED_PHYSICAL_EVIDENCE_KINDS,
    physical_release_evidence_complete,
)


def digest(char: str) -> str:
    return char * 64


DESIGN_SHA = digest("c")
GENERATION_SHA = digest("d")
MACHINE_SHA = digest("e")
MATERIAL_SHA = digest("f")


def subject_for(kind: PhysicalEvidenceKind) -> str:
    if kind in {
        PhysicalEvidenceKind.WALL_ANCHOR,
        PhysicalEvidenceKind.CABINET_HARDWARE,
        PhysicalEvidenceKind.JOINT_RETENTION_SYSTEM,
        PhysicalEvidenceKind.PROTOTYPE_BUILD,
        PhysicalEvidenceKind.LOAD_TEST,
        PhysicalEvidenceKind.CNC_OPERATOR_APPROVAL,
        PhysicalEvidenceKind.FURNITURE_CONSTRUCTOR_APPROVAL,
    }:
        return DESIGN_SHA
    if kind in {
        PhysicalEvidenceKind.JOINT_COUPONS,
        PhysicalEvidenceKind.MATERIAL_REMOVAL_COMPARISON,
        PhysicalEvidenceKind.SUPERVISED_AIR_CUT,
        PhysicalEvidenceKind.REFERENCE_PART,
    }:
        return GENERATION_SHA
    if kind in {
        PhysicalEvidenceKind.MACHINE_CALIBRATION,
        PhysicalEvidenceKind.WCS_CONVENTION,
        PhysicalEvidenceKind.MEASURED_TOOLING,
    }:
        return MACHINE_SHA
    return MATERIAL_SHA


def record_for(kind: PhysicalEvidenceKind) -> PhysicalEvidenceRecord:
    return PhysicalEvidenceRecord(
        evidence_id=f"ev-{kind.value}",
        kind=kind,
        revision="1",
        issuer="qualified-reviewer",
        issued_at="2026-08-28T08:00:00Z",
        subject_sha256=subject_for(kind),
        document_sha256=digest("b"),
    )


def complete_evidence(*, edge_band: bool = False) -> PhysicalReleaseEvidence:
    kinds = list(REQUIRED_PHYSICAL_EVIDENCE_KINDS)
    if edge_band:
        kinds.append(PhysicalEvidenceKind.EDGE_BAND_SYSTEM)
    return PhysicalReleaseEvidence(
        design_sha256=DESIGN_SHA,
        generation_context_sha256=GENERATION_SHA,
        machine_profile_sha256=MACHINE_SHA,
        material_catalog_sha256=MATERIAL_SHA,
        edge_band_selection_required=edge_band,
        records=tuple(record_for(kind) for kind in kinds),
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
            record
            for record in evidence.records
            if record.kind is not PhysicalEvidenceKind.LOAD_TEST
        ),
    )
    assert physical_release_evidence_complete(incomplete) is False
    with pytest.raises(ValueError, match="load_test"):
        incomplete.validate()


def test_wrong_release_subject_is_rejected() -> None:
    evidence = complete_evidence()
    wrong = PhysicalEvidenceRecord(
        evidence_id="ev-machine_calibration",
        kind=PhysicalEvidenceKind.MACHINE_CALIBRATION,
        revision="1",
        issuer="qualified-reviewer",
        issued_at="2026-08-28T08:00:00Z",
        subject_sha256=DESIGN_SHA,
        document_sha256=digest("b"),
    )
    records = tuple(
        wrong if record.kind is PhysicalEvidenceKind.MACHINE_CALIBRATION else record
        for record in evidence.records
    )
    invalid = PhysicalReleaseEvidence(
        design_sha256=evidence.design_sha256,
        generation_context_sha256=evidence.generation_context_sha256,
        machine_profile_sha256=evidence.machine_profile_sha256,
        material_catalog_sha256=evidence.material_catalog_sha256,
        records=records,
    )
    with pytest.raises(ValueError, match="wrong release subject"):
        invalid.validate()


def test_edge_band_evidence_is_required_only_for_matching_design() -> None:
    assert physical_release_evidence_complete(complete_evidence(edge_band=True)) is True
    unexpected_edge = PhysicalReleaseEvidence(
        design_sha256=DESIGN_SHA,
        generation_context_sha256=GENERATION_SHA,
        machine_profile_sha256=MACHINE_SHA,
        material_catalog_sha256=MATERIAL_SHA,
        records=complete_evidence(edge_band=True).records,
    )
    with pytest.raises(ValueError, match="not required"):
        unexpected_edge.validate()


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
