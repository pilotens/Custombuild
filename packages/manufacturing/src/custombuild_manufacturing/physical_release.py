"""Fail-closed physical release evidence contracts.

The deterministic manufacturing engine may prove geometry and software-level
consistency, but it cannot infer facts about a real workshop.  This module
provides the canonical boundary for the external evidence that must exist before
physical machining can ever be authorised.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from .model import canonical_json_bytes, sha256_hex

PHYSICAL_RELEASE_EVIDENCE_SCHEMA_VERSION = "custombuild.physical-release-evidence.v1"


class PhysicalEvidenceKind(StrEnum):
    MACHINE = "machine"
    TOOLING = "tooling"
    MATERIAL_BATCH = "material_batch"
    FIXTURING = "fixturing"
    JOINT_COUPON = "joint_coupon"
    AIR_CUT = "air_cut"
    REFERENCE_PART = "reference_part"
    PROTOTYPE = "prototype"
    LOAD_TEST = "load_test"
    OPERATOR_APPROVAL = "operator_approval"
    CONSTRUCTOR_APPROVAL = "constructor_approval"


REQUIRED_PHYSICAL_EVIDENCE_KINDS = tuple(PhysicalEvidenceKind)


@dataclass(frozen=True, slots=True)
class PhysicalEvidenceRecord:
    evidence_id: str
    kind: PhysicalEvidenceKind
    revision: str
    issuer: str
    issued_at: str
    subject_sha256: str
    document_sha256: str
    notes: str = ""

    def validate(self) -> None:
        for field_name in ("evidence_id", "revision", "issuer", "issued_at"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        for field_name in ("subject_sha256", "document_sha256"):
            value = getattr(self, field_name)
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class PhysicalReleaseEvidence:
    design_sha256: str
    generation_context_sha256: str
    machine_profile_sha256: str
    material_catalog_sha256: str
    records: tuple[PhysicalEvidenceRecord, ...]
    schema_version: str = PHYSICAL_RELEASE_EVIDENCE_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != PHYSICAL_RELEASE_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported physical release evidence schema")
        for field_name in (
            "design_sha256",
            "generation_context_sha256",
            "machine_profile_sha256",
            "material_catalog_sha256",
        ):
            value = getattr(self, field_name)
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")
        if not self.records:
            raise ValueError("physical release evidence records are required")
        ids: set[str] = set()
        kinds: set[PhysicalEvidenceKind] = set()
        for record in self.records:
            record.validate()
            if record.evidence_id in ids:
                raise ValueError("duplicate physical evidence id")
            if record.kind in kinds:
                raise ValueError("duplicate physical evidence kind")
            ids.add(record.evidence_id)
            kinds.add(record.kind)
        missing = set(REQUIRED_PHYSICAL_EVIDENCE_KINDS) - kinds
        if missing:
            names = ", ".join(sorted(kind.value for kind in missing))
            raise ValueError(f"missing physical evidence kinds: {names}")

    def fingerprint(self) -> str:
        self.validate()
        return sha256_hex(canonical_json_bytes(self.as_dict()))

    def as_dict(self) -> dict[str, Any]:
        records = []
        for record in sorted(self.records, key=lambda item: item.kind.value):
            payload = asdict(record)
            payload["kind"] = record.kind.value
            records.append(payload)
        return {
            "schema_version": self.schema_version,
            "design_sha256": self.design_sha256,
            "generation_context_sha256": self.generation_context_sha256,
            "machine_profile_sha256": self.machine_profile_sha256,
            "material_catalog_sha256": self.material_catalog_sha256,
            "records": records,
        }


def physical_release_authorized(evidence: PhysicalReleaseEvidence | None) -> bool:
    """Return true only for a structurally complete, checksum-bound evidence set.

    This function deliberately does not create or infer evidence.  Authenticity,
    calibration, measurements and human competence remain external facts and the
    caller must verify the referenced documents before invoking physical release.
    """

    if evidence is None:
        return False
    try:
        evidence.validate()
    except (TypeError, ValueError):
        return False
    return True
