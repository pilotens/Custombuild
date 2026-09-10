#!/usr/bin/env python3
"""Verify checksum-bound workshop evidence without authorizing machine motion.

This command is intentionally an evidence verifier, not a postprocessor or CNC
launcher. It gives operators a deterministic, auditable preflight for the
external facts Custombuild cannot infer from CAD/CAM alone.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from custombuild_manufacturing.physical_release import (
    PHYSICAL_RELEASE_EVIDENCE_SCHEMA_VERSION,
    PhysicalEvidenceKind,
    PhysicalEvidenceRecord,
    PhysicalReleaseEvidence,
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "design_sha256",
        "generation_context_sha256",
        "machine_profile_sha256",
        "material_catalog_sha256",
        "edge_band_selection_required",
        "records",
    }
)
_RECORD_KEYS = frozenset(
    {
        "evidence_id",
        "kind",
        "revision",
        "issuer",
        "issued_at",
        "subject_sha256",
        "document_sha256",
        "notes",
    }
)


def _load(path: Path) -> PhysicalReleaseEvidence:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_KEYS:
        raise ValueError("physical release evidence has an unexpected top-level schema")
    if payload["schema_version"] != PHYSICAL_RELEASE_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("unsupported physical release evidence schema")
    raw_records = payload["records"]
    if not isinstance(raw_records, list):
        raise ValueError("records must be a JSON array")
    records: list[PhysicalEvidenceRecord] = []
    for item in raw_records:
        if not isinstance(item, dict) or set(item) != _RECORD_KEYS:
            raise ValueError("physical release evidence record has an unexpected schema")
        records.append(
            PhysicalEvidenceRecord(
                evidence_id=item["evidence_id"],
                kind=PhysicalEvidenceKind(item["kind"]),
                revision=item["revision"],
                issuer=item["issuer"],
                issued_at=item["issued_at"],
                subject_sha256=item["subject_sha256"],
                document_sha256=item["document_sha256"],
                notes=item["notes"],
            )
        )
    return PhysicalReleaseEvidence(
        schema_version=payload["schema_version"],
        design_sha256=payload["design_sha256"],
        generation_context_sha256=payload["generation_context_sha256"],
        machine_profile_sha256=payload["machine_profile_sha256"],
        material_catalog_sha256=payload["material_catalog_sha256"],
        edge_band_selection_required=payload["edge_band_selection_required"],
        records=tuple(records),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--expect-design-sha256")
    parser.add_argument("--expect-generation-context-sha256")
    parser.add_argument("--expect-machine-profile-sha256")
    parser.add_argument("--expect-material-catalog-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        evidence = _load(args.evidence)
        evidence.validate()
        expected = {
            "design_sha256": args.expect_design_sha256,
            "generation_context_sha256": args.expect_generation_context_sha256,
            "machine_profile_sha256": args.expect_machine_profile_sha256,
            "material_catalog_sha256": args.expect_material_catalog_sha256,
        }
        for field, expected_digest in expected.items():
            if expected_digest and getattr(evidence, field) != expected_digest:
                raise ValueError(f"{field} does not match the expected release subject")
        result = {
            "status": "PASS",
            "schema_version": evidence.schema_version,
            "evidence_fingerprint": evidence.fingerprint(),
            "design_sha256": evidence.design_sha256,
            "generation_context_sha256": evidence.generation_context_sha256,
            "machine_profile_sha256": evidence.machine_profile_sha256,
            "material_catalog_sha256": evidence.material_catalog_sha256,
            "edge_band_selection_required": evidence.edge_band_selection_required,
            "record_count": len(evidence.records),
            "physical_release_evidence_complete": True,
            "physical_cutting_authorized": False,
            "note": (
                "Structural evidence completeness passed. Physical cutting remains subject "
                "to independent authenticity, competence, calibration and release approval."
            ),
        }
        exit_code = 0
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "status": "BLOCK",
            "physical_release_evidence_complete": False,
            "physical_cutting_authorized": False,
            "error": str(exc),
        }
        exit_code = 1

    rendered = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
