from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.workshop_trust import (
    EXECUTABLE_MACHINE_PROGRAM_KIND,
    SIDECAR_EVIDENCE_PLACEMENT,
    SIGNED_WORKSHOP_ATTESTATION_SCHEMA_VERSION,
    VALIDATION_ONLY_MACHINE_PROGRAM_KIND,
    WORKSHOP_ATTESTATION_STATEMENT_SCHEMA_VERSION,
    WORKSHOP_CHECKER_ROLE,
    WORKSHOP_EVIDENCE_MEDIA_TYPE,
    WORKSHOP_EVIDENCE_RECORD_SCHEMA_VERSION,
    WORKSHOP_MAKER_ROLE,
    WORKSHOP_TRUST_REGISTRY_SCHEMA_VERSION,
    WORKSHOP_VERIFICATION_POLICY_SCHEMA_VERSION,
    VerifiedWorkshopChain,
    WorkshopRun,
    WorkshopTrustError,
    WorkshopVerificationPolicy,
    canonical_json_bytes,
    validate_signed_workshop_attestation_structure,
    verify_workshop_attestation_chain,
    workshop_evidence_claim_sha256,
    workshop_policy_sha256,
    workshop_run_sha256,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
STAGES = ("PRE_CUT", "REFERENCE_PART", "FINAL_WORKSHOP")
NONCES = {
    "PRE_CUT": "nonce-pre-cut-00000000000000000001",
    "REFERENCE_PART": "nonce-reference-000000000000000001",
    "FINAL_WORKSHOP": "nonce-final-workshop-0000000000001",
}


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _reference(
    label: str,
    *,
    status: str = "VERIFIED",
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    if status == "VERIFIED":
        return {
            "status": status,
            "evidence_id": label,
            "evidence_version": "1.0.0",
            "claim_type": None,
            "claim_sha256": None,
            "sha256": None,
            "size_bytes": None,
            "media_type": None,
            "observed_at": _utc(observed_at or NOW - timedelta(days=10)),
            "reason": None,
        }
    return {
        "status": status,
        "evidence_id": None,
        "evidence_version": None,
        "claim_type": None,
        "claim_sha256": None,
        "sha256": None,
        "size_bytes": None,
        "media_type": None,
        "observed_at": None,
        "reason": f"{label} was explicitly unavailable",
    }


def _run(*, kind: str = EXECUTABLE_MACHINE_PROGRAM_KIND) -> dict[str, Any]:
    policy_hash = workshop_policy_sha256(WorkshopVerificationPolicy.model_validate(_policy()))
    return {
        "organization": "org-1",
        "project": "project-1",
        "design_version": "design-v7",
        "design_hash": _sha("design"),
        "generation_job": "generation-job-42",
        "production_context_hash": _sha("production-context"),
        "manifest_sha256": _sha("base-manifest"),
        "bundle_sha256": _sha("base-bundle"),
        "operations_sha256": _sha("operations"),
        "generation_plan_sha256": _sha("generation-plan"),
        "workshop_policy_sha256": policy_hash,
        "machine_program_kind": kind,
        "machine_program_set_sha256": _sha("executable-machine-program-set"),
        "postprocessor_id": "linuxcnc-production",
        "postprocessor_version": "2.0.0",
        "postprocessor_binary_sha256": _sha("postprocessor-binary"),
        "postprocessor_config_sha256": _sha("postprocessor-config"),
    }


def _measurement(measurement_id: str) -> dict[str, Any]:
    return {
        "measurement_id": measurement_id,
        "nominal_um": 10_000,
        "measured_um": 10_010,
        "minimum_acceptable_um": 9_950,
        "maximum_acceptable_um": 10_050,
    }


def _setup() -> dict[str, Any]:
    return {
        "machine": {
            "machine_id": "cnc-01",
            "manufacturer": "Acme",
            "model": "Router-X",
            "serial_number": "SN-10001",
            "controller_id": "LinuxCNC",
            "controller_version": "2.9.4",
            "profile_id": "cnc-01-oak",
            "profile_version": "4.2.0",
            "profile_sha256": _sha("cnc-01-oak-profile"),
            "calibration_id": "cal-2026-08",
            "calibrated_at": _utc(NOW - timedelta(days=7)),
            "calibration_expires_at": _utc(NOW + timedelta(days=30)),
            "calibration_evidence": _reference("machine-calibration"),
        },
        "wcs": {
            "wcs_id": "G54",
            "convention_version": "1.0.0",
            "origin_x_um": 0,
            "origin_y_um": 0,
            "origin_z_um": 18_000,
            "axes_definition_sha256": _sha("axes-definition"),
            "verification_evidence": _reference("wcs-verification"),
        },
        "fixture": {
            "fixture_id": "vacuum-bed",
            "fixture_version": "3.0.0",
            "serial_number": "FIX-008",
            "setup_sha256": _sha("fixture-setup"),
            "clamping_plan_sha256": _sha("clamping-plan"),
            "verification_evidence": _reference("fixture-verification"),
        },
        "keepouts": {
            "volumes": [
                {
                    "keepout_id": "clamp-01",
                    "minimum_x_um": 0,
                    "minimum_y_um": 0,
                    "minimum_z_um": 0,
                    "maximum_x_um": 50_000,
                    "maximum_y_um": 20_000,
                    "maximum_z_um": 30_000,
                }
            ],
            "review_evidence": _reference("keepout-review"),
        },
        "tools": [
            {
                "tool_id": "endmill-6mm",
                "tool_version": "1.0.0",
                "serial_number": "TOOL-100",
                "holder_id": "ER32-01",
                "pocket_number": 1,
                "measured_diameter_um": 5_998,
                "measured_length_offset_um": 52_010,
                "measured_runout_um": 8,
                "measured_stickout_um": 30_000,
                "measured_usable_flute_length_um": 22_000,
                "measurement_evidence": _reference("tool-measurement"),
            }
        ],
        "stock": [
            {
                "stock_id": "sheet-001",
                "material_id": "oak-plywood",
                "material_version": "supplier-2026.1",
                "supplier_batch_id": "BATCH-08-31",
                "supplier_lot_id": "LOT-552",
                "grain_orientation": "X",
                "dimensions": {
                    "length_um": 2_500_000,
                    "width_um": 1_250_000,
                    "thickness_um": 18_020,
                },
                "moisture_content_ppm": 82_000,
                "material_certificate_evidence": _reference("material-certificate"),
                "measurement_evidence": _reference("stock-measurement"),
            }
        ],
    }


def _policy() -> dict[str, Any]:
    setup = _setup()
    keepout_digest = hashlib.sha256(
        canonical_json_bytes({"volumes": setup["keepouts"]["volumes"]})
    ).hexdigest()
    measurement = _measurement("dado-width")
    reference_measurement = _measurement("reference-width")
    return {
        "schema_version": WORKSHOP_VERIFICATION_POLICY_SCHEMA_VERSION,
        "policy_id": "oak-bookcase-workshop",
        "policy_version": "1.0.0",
        "machine": {
            "machine_id": "cnc-01",
            "manufacturer": "Acme",
            "model": "Router-X",
            "serial_number": "SN-10001",
            "controller_id": "LinuxCNC",
            "controller_version": "2.9.4",
            "profile_id": "cnc-01-oak",
            "profile_version": "4.2.0",
            "profile_sha256": _sha("cnc-01-oak-profile"),
        },
        "wcs": {
            "wcs_id": "G54",
            "convention_version": "1.0.0",
            "origin_x_um": 0,
            "origin_y_um": 0,
            "origin_z_um": 18_000,
            "axes_definition_sha256": _sha("axes-definition"),
        },
        "fixture": {
            "fixture_id": "vacuum-bed",
            "fixture_version": "3.0.0",
            "serial_number": "FIX-008",
            "setup_sha256": _sha("fixture-setup"),
            "clamping_plan_sha256": _sha("clamping-plan"),
            "keepout_volumes_sha256": keepout_digest,
        },
        "tools": [
            {
                "tool_id": "endmill-6mm",
                "tool_version": "1.0.0",
                "serial_number": "TOOL-100",
                "holder_id": "ER32-01",
                "pocket_number": 1,
                "minimum_diameter_um": 5_990,
                "maximum_diameter_um": 6_010,
                "minimum_length_offset_um": 51_900,
                "maximum_length_offset_um": 52_100,
                "maximum_runout_um": 10,
                "minimum_stickout_um": 29_000,
                "maximum_stickout_um": 31_000,
                "minimum_usable_flute_length_um": 20_000,
            }
        ],
        "stock": [
            {
                "stock_id": "sheet-001",
                "material_id": "oak-plywood",
                "material_version": "supplier-2026.1",
                "supplier_batch_id": "BATCH-08-31",
                "supplier_lot_id": "LOT-552",
                "grain_orientation": "X",
                "minimum_length_um": 2_499_000,
                "maximum_length_um": 2_501_000,
                "minimum_width_um": 1_249_000,
                "maximum_width_um": 1_251_000,
                "minimum_thickness_um": 17_900,
                "maximum_thickness_um": 18_100,
                "minimum_moisture_content_ppm": 60_000,
                "maximum_moisture_content_ppm": 100_000,
            }
        ],
        "coupon_material_batch_ids": ["BATCH-08-31"],
        "coupon_specification_sha256": _sha("coupon-specification"),
        "coupon_measurements": [
            {
                key: measurement[key]
                for key in (
                    "measurement_id",
                    "nominal_um",
                    "minimum_acceptable_um",
                    "maximum_acceptable_um",
                )
            }
        ],
        "independent_engine": {
            "engine_id": "independent-backplot",
            "engine_version": "5.0.0",
            "binary_sha256": _sha("independent-backplot-binary"),
            "config_sha256": _sha("independent-backplot-config"),
        },
        "expected_removal_sha256": _sha("expected-removal"),
        "maximum_removal_deviation_um": 50,
        "minimum_air_cut_clearance_um": 20_000,
        "reference_part_program_sha256": _sha("reference-part-program"),
        "reference_part_measurements": [
            {
                key: reference_measurement[key]
                for key in (
                    "measurement_id",
                    "nominal_um",
                    "minimum_acceptable_um",
                    "maximum_acceptable_um",
                )
            }
        ],
        "load_test_plan_sha256": _sha("load-test-plan"),
        "minimum_applied_load_n": 7_500,
        "minimum_load_duration_seconds": 80_000,
        "maximum_deflection_um": 2_000,
        "maximum_residual_deflection_um": 250,
    }


def _bind_reference(
    reference: dict[str, Any],
    *,
    claim_type: str,
    payload: dict[str, Any],
) -> None:
    if reference["status"] != "VERIFIED":
        return
    claim_digest = workshop_evidence_claim_sha256(
        claim_type=claim_type,
        payload=payload,
    )
    record = {
        "schema_version": WORKSHOP_EVIDENCE_RECORD_SCHEMA_VERSION,
        "evidence_id": reference["evidence_id"],
        "evidence_version": reference["evidence_version"],
        "claim_type": claim_type,
        "claim_sha256": claim_digest,
    }
    record_bytes = canonical_json_bytes(record)
    reference.update(
        {
            "claim_type": claim_type,
            "claim_sha256": claim_digest,
            "sha256": hashlib.sha256(record_bytes).hexdigest(),
            "size_bytes": len(record_bytes),
            "media_type": WORKSHOP_EVIDENCE_MEDIA_TYPE,
        }
    )


def _without(value: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: deepcopy(item) for key, item in value.items() if key not in keys}


def _bind_evidence_claims(statements: list[dict[str, Any]]) -> None:
    for statement in statements:
        setup = statement["setup"]
        machine = setup["machine"]
        _bind_reference(
            machine["calibration_evidence"],
            claim_type="machine-calibration",
            payload=_without(machine, "calibration_evidence"),
        )
        wcs = setup["wcs"]
        _bind_reference(
            wcs["verification_evidence"],
            claim_type="wcs-verification",
            payload=_without(wcs, "verification_evidence"),
        )
        fixture = setup["fixture"]
        _bind_reference(
            fixture["verification_evidence"],
            claim_type="fixture-verification",
            payload=_without(fixture, "verification_evidence"),
        )
        keepouts = setup["keepouts"]
        _bind_reference(
            keepouts["review_evidence"],
            claim_type="keepout-review",
            payload={"volumes": deepcopy(keepouts["volumes"])},
        )
        for tool in setup["tools"]:
            _bind_reference(
                tool["measurement_evidence"],
                claim_type="tool-measurement",
                payload=_without(tool, "measurement_evidence"),
            )
        for stock in setup["stock"]:
            _bind_reference(
                stock["material_certificate_evidence"],
                claim_type="material-certificate",
                payload={
                    key: stock[key]
                    for key in (
                        "stock_id",
                        "material_id",
                        "material_version",
                        "supplier_batch_id",
                        "supplier_lot_id",
                        "grain_orientation",
                    )
                },
            )
            _bind_reference(
                stock["measurement_evidence"],
                claim_type="stock-measurement",
                payload=_without(
                    stock,
                    "material_certificate_evidence",
                    "measurement_evidence",
                ),
            )
        if statement["pre_cut"] is not None:
            pre_cut = statement["pre_cut"]
            coupons = pre_cut["coupons"]
            _bind_reference(
                coupons["evidence"],
                claim_type="coupon-qualification",
                payload=_without(coupons, "evidence"),
            )
            comparison = pre_cut["independent_removal_comparison"]
            _bind_reference(
                comparison["evidence"],
                claim_type="independent-removal-comparison",
                payload=_without(comparison, "evidence"),
            )
            air_cut = pre_cut["supervised_air_cut"]
            _bind_reference(
                air_cut["evidence"],
                claim_type="supervised-air-cut",
                payload=_without(air_cut, "evidence"),
            )
        elif statement["reference_part"] is not None:
            reference_part = statement["reference_part"]
            _bind_reference(
                reference_part["evidence"],
                claim_type="reference-part-metrology",
                payload=_without(reference_part, "evidence"),
            )
        else:
            final = statement["final_workshop"]
            prototype = final["prototype_build"]
            _bind_reference(
                prototype["evidence"],
                claim_type="prototype-build",
                payload=_without(prototype, "evidence"),
            )
            load_test = final["load_test"]
            _bind_reference(
                load_test["evidence"],
                claim_type="prototype-load-test",
                payload=_without(load_test, "evidence"),
            )


def _pre_cut(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "coupons": {
            "evidence": _reference(
                "coupon-report",
                observed_at=NOW - timedelta(days=6, hours=1),
            ),
            "coupons": [
                {
                    "coupon_id": "coupon-001",
                    "material_batch_id": "BATCH-08-31",
                    "specification_sha256": _sha("coupon-specification"),
                    "measurements": [_measurement("dado-width")],
                    "outcome": "PASS",
                }
            ],
        },
        "independent_removal_comparison": {
            "evidence": _reference(
                "removal-comparison",
                observed_at=NOW - timedelta(days=6, hours=1),
            ),
            "comparison_engine_id": "independent-backplot",
            "comparison_engine_version": "5.0.0",
            "comparison_engine_binary_sha256": _sha("independent-backplot-binary"),
            "comparison_engine_config_sha256": _sha("independent-backplot-config"),
            "expected_removal_sha256": _sha("expected-removal"),
            "observed_removal_sha256": _sha("observed-removal"),
            "maximum_deviation_um": 10,
            "allowed_deviation_um": 50,
            "outcome": "PASS",
        },
        "supervised_air_cut": {
            "evidence": _reference(
                "supervised-air-cut",
                observed_at=NOW - timedelta(days=6, hours=1),
            ),
            "machine_program_set_sha256": run["machine_program_set_sha256"],
            "supervisor_id": "operator-supervisor-1",
            "minimum_clearance_um": 25_000,
            "outcome": "PASS",
        },
    }


def _reference_part() -> dict[str, Any]:
    return {
        "evidence": _reference(
            "reference-part-metrology",
            observed_at=NOW - timedelta(days=5, hours=1),
        ),
        "part_id": "reference-part-001",
        "machine_program_sha256": _sha("reference-part-program"),
        "metrology": [_measurement("reference-width")],
        "outcome": "PASS",
    }


def _final_workshop(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "prototype_build": {
            "evidence": _reference(
                "prototype-inspection",
                observed_at=NOW - timedelta(days=4),
            ),
            "prototype_id": "prototype-001",
            "build_manifest_sha256": run["manifest_sha256"],
            "inspection_sha256": _sha("prototype-inspection-record"),
            "outcome": "PASS",
        },
        "load_test": {
            "evidence": _reference(
                "prototype-load-test",
                observed_at=NOW - timedelta(days=2, hours=23),
            ),
            "test_plan_sha256": _sha("load-test-plan"),
            "started_at": _utc(NOW - timedelta(days=3, hours=23)),
            "completed_at": _utc(NOW - timedelta(days=2, hours=23)),
            "applied_load_n": 8_000,
            "duration_seconds": 86_400,
            "maximum_deflection_um": 1_200,
            "allowed_deflection_um": 2_000,
            "residual_deflection_um": 120,
            "allowed_residual_deflection_um": 250,
            "outcome": "PASS",
        },
    }


def _statements(run: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    selected_run = deepcopy(run or _run())
    setup = _setup()
    results: list[dict[str, Any]] = []
    issued_times = (
        NOW - timedelta(days=6),
        NOW - timedelta(days=5),
        NOW - timedelta(days=1),
    )
    for index, stage in enumerate(STAGES):
        results.append(
            {
                "schema_version": WORKSHOP_ATTESTATION_STATEMENT_SCHEMA_VERSION,
                "attestation_id": f"attestation-{index + 1}",
                "stage": stage,
                "run": deepcopy(selected_run),
                "evidence_placement": SIDECAR_EVIDENCE_PLACEMENT,
                "previous_attestation_sha256": None,
                "server_nonce": NONCES[stage],
                "issued_at": _utc(issued_times[index]),
                "expires_at": _utc(NOW + timedelta(days=7)),
                "maker": {"principal_id": "maker-1", "key_id": "maker-key-1"},
                "checker": {"principal_id": "checker-1", "key_id": "checker-key-1"},
                "setup": deepcopy(setup),
                "pre_cut": _pre_cut(selected_run) if stage == "PRE_CUT" else None,
                "reference_part": _reference_part() if stage == "REFERENCE_PART" else None,
                "final_workshop": _final_workshop(selected_run)
                if stage == "FINAL_WORKSHOP"
                else None,
            }
        )
    _bind_evidence_claims(results)
    return results


def _keys() -> dict[str, Ed25519PrivateKey]:
    return {
        "maker-1": Ed25519PrivateKey.generate(),
        "checker-1": Ed25519PrivateKey.generate(),
    }


def _signed_chain(
    statements: list[dict[str, Any]],
    keys: dict[str, Ed25519PrivateKey],
) -> tuple[bytes, bytes, bytes]:
    chain: list[bytes] = []
    previous: str | None = None
    for statement in statements:
        statement["previous_attestation_sha256"] = previous
        statement_bytes = canonical_json_bytes(statement)
        envelope = {
            "schema_version": SIGNED_WORKSHOP_ATTESTATION_SCHEMA_VERSION,
            "statement": statement,
            "maker_signature_base64": base64.b64encode(
                keys[statement["maker"]["principal_id"]].sign(statement_bytes)
            ).decode("ascii"),
            "checker_signature_base64": base64.b64encode(
                keys[statement["checker"]["principal_id"]].sign(statement_bytes)
            ).decode("ascii"),
        }
        encoded = canonical_json_bytes(envelope)
        chain.append(encoded)
        previous = hashlib.sha256(encoded).hexdigest()
    return (chain[0], chain[1], chain[2])


def _public_key(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _registry(
    keys: dict[str, Ed25519PrivateKey],
    *,
    revoked_statement_sha256: tuple[str, ...] = (),
    revoked_run_sha256: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "schema_version": WORKSHOP_TRUST_REGISTRY_SCHEMA_VERSION,
        "issuers": [
            {
                "organization": "org-1",
                "principal_id": "checker-1",
                "key_id": "checker-key-1",
                "role": WORKSHOP_CHECKER_ROLE,
                "qualified_stages": list(STAGES),
                "public_key_base64": _public_key(keys["checker-1"]),
                "not_before": _utc(NOW - timedelta(days=30)),
                "not_after": _utc(NOW + timedelta(days=365)),
                "revoked_at": None,
            },
            {
                "organization": "org-1",
                "principal_id": "maker-1",
                "key_id": "maker-key-1",
                "role": WORKSHOP_MAKER_ROLE,
                "qualified_stages": list(STAGES),
                "public_key_base64": _public_key(keys["maker-1"]),
                "not_before": _utc(NOW - timedelta(days=30)),
                "not_after": _utc(NOW + timedelta(days=365)),
                "revoked_at": None,
            },
        ],
        "revoked_statement_sha256": list(revoked_statement_sha256),
        "revoked_run_sha256": list(revoked_run_sha256),
    }


def _valid_fixture() -> tuple[
    dict[str, Any],
    dict[str, Ed25519PrivateKey],
    dict[str, Any],
    tuple[bytes, bytes, bytes],
]:
    run = _run()
    keys = _keys()
    return run, keys, _registry(keys), _signed_chain(_statements(run), keys)


def _evidence_objects(chain: Sequence[bytes]) -> dict[tuple[str, str], bytes]:
    objects: dict[tuple[str, str], bytes] = {}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("status") == "VERIFIED" and "evidence_id" in value:
                evidence_id = value["evidence_id"]
                evidence_version = value["evidence_version"]
                assert isinstance(evidence_id, str)
                assert isinstance(evidence_version, str)
                objects[(evidence_id, evidence_version)] = canonical_json_bytes(
                    {
                        "schema_version": WORKSHOP_EVIDENCE_RECORD_SCHEMA_VERSION,
                        "evidence_id": evidence_id,
                        "evidence_version": evidence_version,
                        "claim_type": value["claim_type"],
                        "claim_sha256": value["claim_sha256"],
                    }
                )
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for encoded in chain:
        visit(json.loads(encoded))
    return objects


def _verify(
    run: dict[str, Any],
    registry: dict[str, Any],
    chain: tuple[bytes, bytes, bytes],
    *,
    policy: dict[str, Any] | None = None,
    evidence_objects: dict[tuple[str, str], bytes] | None = None,
) -> VerifiedWorkshopChain:
    return verify_workshop_attestation_chain(
        trust_registry=registry,
        attestation_bytes=chain,
        expected_run=run,
        expected_policy=policy or _policy(),
        expected_server_nonces=NONCES,
        evidence_objects=(
            _evidence_objects(chain) if evidence_objects is None else evidence_objects
        ),
        now=NOW,
    )


def test_complete_three_stage_chain_derives_review_eligibility_but_never_cutting() -> None:
    run, _keys_by_principal, registry, chain = _valid_fixture()

    result = _verify(run, registry, chain)

    assert result.final_eligibility == "VERIFIED_FOR_RELEASE_REVIEW"
    assert result.physical_cutting_authorized is False
    assert result.run_sha256 == workshop_run_sha256(WorkshopRun.model_validate(run))
    assert result.attestation_sha256 == tuple(hashlib.sha256(item).hexdigest() for item in chain)
    assert result.final_attestation_id == "attestation-3"
    assert run["bundle_sha256"] not in result.attestation_sha256
    for item in chain:
        validate_signed_workshop_attestation_structure(item)
    replace_any: Any = replace
    with pytest.raises(TypeError):
        replace_any(result, physical_cutting_authorized=True)


def test_evidence_objects_are_reread_and_checksum_verified() -> None:
    run, _keys_by_principal, registry, chain = _valid_fixture()
    objects = _evidence_objects(chain)
    objects.pop(("coupon-report", "1.0.0"))
    with pytest.raises(WorkshopTrustError, match="evidence object is unavailable"):
        _verify(run, registry, chain, evidence_objects=objects)

    corrupted = _evidence_objects(chain)
    corrupted[("coupon-report", "1.0.0")] = b"corrupt evidence bytes"
    with pytest.raises(WorkshopTrustError, match="does not match its signed identity"):
        _verify(run, registry, chain, evidence_objects=corrupted)


def test_server_policy_digest_is_part_of_the_immutable_run() -> None:
    run, _keys_by_principal, registry, chain = _valid_fixture()
    substituted_policy = _policy()
    substituted_policy["minimum_applied_load_n"] = 1

    with pytest.raises(WorkshopTrustError, match="policy does not match the immutable run"):
        _verify(run, registry, chain, policy=substituted_policy)


def test_tampering_and_wrong_signing_key_fail_closed() -> None:
    run, keys, registry, chain = _valid_fixture()
    tampered = json.loads(chain[0])
    tampered["statement"]["setup"]["tools"][0]["measured_runout_um"] = 9
    tampered_chain = (canonical_json_bytes(tampered), chain[1], chain[2])

    with pytest.raises(WorkshopTrustError, match="maker signature is invalid"):
        _verify(run, registry, tampered_chain)

    wrong_registry = _registry({**keys, "maker-1": Ed25519PrivateKey.generate()})
    with pytest.raises(WorkshopTrustError, match="maker signature is invalid"):
        _verify(run, wrong_registry, chain)


def test_noncanonical_attestation_and_timestamp_bytes_fail_closed() -> None:
    run, keys, registry, chain = _valid_fixture()
    pretty = json.dumps(json.loads(chain[0]), indent=2).encode()
    with pytest.raises(WorkshopTrustError, match="canonical JSON bytes"):
        _verify(run, registry, (pretty, chain[1], chain[2]))

    statements = _statements(run)
    statements[0]["issued_at"] = "2026-09-01T11:30:00+00:00"
    malformed = _signed_chain(statements, keys)
    with pytest.raises(WorkshopTrustError, match="invalid schema"):
        _verify(run, registry, malformed)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("organization", "another-org"),
        ("project", "another-project"),
        ("design_hash", "b" * 64),
        ("manifest_sha256", "c" * 64),
        ("bundle_sha256", "d" * 64),
        ("machine_program_set_sha256", "e" * 64),
        ("postprocessor_binary_sha256", "f" * 64),
    ),
)
def test_chain_cannot_replay_across_run_design_manifest_or_executable_identity(
    field: str,
    replacement: str,
) -> None:
    run, _keys_by_principal, registry, chain = _valid_fixture()
    another_expected_run = deepcopy(run)
    another_expected_run[field] = replacement

    with pytest.raises(WorkshopTrustError, match="another immutable run"):
        _verify(another_expected_run, registry, chain)


def test_server_nonce_replay_and_nonce_aliasing_fail_closed() -> None:
    run, _keys_by_principal, registry, chain = _valid_fixture()
    replay_nonces = dict(NONCES)
    replay_nonces["PRE_CUT"] = "new-server-nonce-00000000000000000001"
    with pytest.raises(WorkshopTrustError, match="server nonce does not match"):
        verify_workshop_attestation_chain(
            trust_registry=registry,
            attestation_bytes=chain,
            expected_run=run,
            expected_policy=_policy(),
            expected_server_nonces=replay_nonces,
            evidence_objects=_evidence_objects(chain),
            now=NOW,
        )

    aliased_nonces = dict(NONCES)
    aliased_nonces["REFERENCE_PART"] = aliased_nonces["PRE_CUT"]
    with pytest.raises(WorkshopTrustError, match="nonces must be unique"):
        verify_workshop_attestation_chain(
            trust_registry=registry,
            attestation_bytes=chain,
            expected_run=run,
            expected_policy=_policy(),
            expected_server_nonces=aliased_nonces,
            evidence_objects=_evidence_objects(chain),
            now=NOW,
        )


def test_role_confusion_and_same_maker_checker_identity_fail_closed() -> None:
    run = _run()
    keys = _keys()
    statements = _statements(run)
    statements[0]["maker"], statements[0]["checker"] = (
        statements[0]["checker"],
        statements[0]["maker"],
    )
    confused_chain = _signed_chain(statements, keys)
    with pytest.raises(WorkshopTrustError, match="not trusted for its signer role"):
        _verify(run, _registry(keys), confused_chain)

    same_identity = _statements(run)
    same_identity[0]["checker"] = deepcopy(same_identity[0]["maker"])
    same_chain = _signed_chain(same_identity, keys)
    with pytest.raises(WorkshopTrustError, match="invalid schema"):
        _verify(run, _registry(keys), same_chain)


def test_unqualified_checker_and_duplicate_public_key_alias_fail_closed() -> None:
    run, keys, registry, chain = _valid_fixture()
    registry["issuers"][0]["qualified_stages"] = ["PRE_CUT", "REFERENCE_PART"]
    with pytest.raises(WorkshopTrustError, match="not qualified for FINAL_WORKSHOP"):
        _verify(run, registry, chain)

    aliased_registry = _registry(keys)
    aliased_registry["issuers"][0]["public_key_base64"] = aliased_registry["issuers"][1][
        "public_key_base64"
    ]
    with pytest.raises(WorkshopTrustError, match="public keys must be unique"):
        _verify(run, aliased_registry, chain)


def test_signer_authority_is_scoped_to_the_run_organization() -> None:
    run = _run()
    run["organization"] = "org-2"
    keys = _keys()
    chain = _signed_chain(_statements(run), keys)

    with pytest.raises(WorkshopTrustError, match="not trusted for this organization"):
        _verify(run, _registry(keys), chain)


@pytest.mark.parametrize("condition", ("expired", "revoked"))
def test_expired_or_revoked_issuer_key_fails_closed(condition: str) -> None:
    run, _keys_by_principal, registry, chain = _valid_fixture()
    maker = registry["issuers"][1]
    if condition == "expired":
        maker["not_after"] = _utc(NOW - timedelta(minutes=1))
    else:
        maker["revoked_at"] = _utc(NOW - timedelta(minutes=1))

    with pytest.raises(WorkshopTrustError, match="not currently valid|key is revoked"):
        _verify(run, registry, chain)


def test_revoked_run_and_statement_fail_closed() -> None:
    run, keys, _registry_value, chain = _valid_fixture()
    run_digest = workshop_run_sha256(WorkshopRun.model_validate(run))
    with pytest.raises(WorkshopTrustError, match="run is revoked"):
        _verify(run, _registry(keys, revoked_run_sha256=(run_digest,)), chain)

    statement = json.loads(chain[1])["statement"]
    statement_digest = hashlib.sha256(canonical_json_bytes(statement)).hexdigest()
    with pytest.raises(WorkshopTrustError, match="statement is revoked"):
        _verify(
            run,
            _registry(keys, revoked_statement_sha256=(statement_digest,)),
            chain,
        )


def test_broken_prior_hash_chain_and_changed_setup_fail_closed() -> None:
    run, keys, registry, chain = _valid_fixture()
    broken_payload = json.loads(chain[1])
    broken_payload["statement"]["previous_attestation_sha256"] = "9" * 64
    statement_bytes = canonical_json_bytes(broken_payload["statement"])
    broken_payload["maker_signature_base64"] = base64.b64encode(
        keys["maker-1"].sign(statement_bytes)
    ).decode()
    broken_payload["checker_signature_base64"] = base64.b64encode(
        keys["checker-1"].sign(statement_bytes)
    ).decode()
    with pytest.raises(WorkshopTrustError, match="hash chain is broken"):
        _verify(run, registry, (chain[0], canonical_json_bytes(broken_payload), chain[2]))

    changed_setup = _statements(run)
    changed_setup[1]["setup"]["machine"]["serial_number"] = "SN-OTHER"
    changed_chain = _signed_chain(changed_setup, keys)
    with pytest.raises(WorkshopTrustError, match="setup changed"):
        _verify(run, registry, changed_chain)


@pytest.mark.parametrize("status", ("MISSING", "NOT_APPLICABLE"))
def test_missing_and_not_applicable_required_evidence_is_safe_but_never_eligible(
    status: str,
) -> None:
    run = _run()
    keys = _keys()
    statements = _statements(run)
    statements[0]["pre_cut"]["coupons"] = {
        "evidence": _reference("coupon-report", status=status),
        "coupons": [],
    }
    chain = _signed_chain(statements, keys)

    validate_signed_workshop_attestation_structure(chain[0])
    with pytest.raises(
        WorkshopTrustError,
        match=f"coupon qualification evidence is {status.lower()}",
    ):
        _verify(run, _registry(keys), chain)


def test_failed_coupon_reference_part_and_load_test_never_derive_eligibility() -> None:
    run = _run()
    keys = _keys()
    registry = _registry(keys)

    coupon_failure = _statements(run)
    coupon_failure[0]["pre_cut"]["coupons"]["coupons"][0]["outcome"] = "FAIL"
    _bind_evidence_claims(coupon_failure)
    with pytest.raises(WorkshopTrustError, match="every required coupon must pass"):
        _verify(run, registry, _signed_chain(coupon_failure, keys))

    reference_failure = _statements(run)
    reference_failure[1]["reference_part"]["outcome"] = "FAIL"
    _bind_evidence_claims(reference_failure)
    with pytest.raises(WorkshopTrustError, match="metrology did not pass"):
        _verify(run, registry, _signed_chain(reference_failure, keys))

    load_failure = _statements(run)
    load_failure[2]["final_workshop"]["load_test"]["outcome"] = "FAIL"
    _bind_evidence_claims(load_failure)
    with pytest.raises(WorkshopTrustError, match="must both pass"):
        _verify(run, registry, _signed_chain(load_failure, keys))


def test_signers_cannot_choose_weaker_air_cut_or_load_test_thresholds() -> None:
    run = _run()
    keys = _keys()
    registry = _registry(keys)

    weak_air_cut = _statements(run)
    weak_air_cut[0]["pre_cut"]["supervised_air_cut"]["minimum_clearance_um"] = 1
    _bind_evidence_claims(weak_air_cut)
    with pytest.raises(WorkshopTrustError, match="air cut clearance violates server policy"):
        _verify(run, registry, _signed_chain(weak_air_cut, keys))

    weak_load = _statements(run)
    load = weak_load[2]["final_workshop"]["load_test"]
    load["applied_load_n"] = 1
    load["duration_seconds"] = 1
    _bind_evidence_claims(weak_load)
    with pytest.raises(WorkshopTrustError, match="load test does not match server policy"):
        _verify(run, registry, _signed_chain(weak_load, keys))


def test_load_test_must_follow_prototype_and_support_its_claimed_duration() -> None:
    run = _run()
    keys = _keys()
    registry = _registry(keys)

    impossible_duration = _statements(run)
    load = impossible_duration[2]["final_workshop"]["load_test"]
    load["completed_at"] = _utc(NOW - timedelta(days=3, hours=22))
    _bind_evidence_claims(impossible_duration)
    with pytest.raises(WorkshopTrustError, match="invalid schema"):
        _verify(run, registry, _signed_chain(impossible_duration, keys))

    before_prototype = _statements(run)
    prototype_evidence = before_prototype[2]["final_workshop"]["prototype_build"]["evidence"]
    prototype_evidence["observed_at"] = _utc(NOW - timedelta(days=3, hours=22))
    with pytest.raises(WorkshopTrustError, match="load test does not match server policy"):
        _verify(run, registry, _signed_chain(before_prototype, keys))


@pytest.mark.parametrize(
    ("field", "value"),
    (("supplier_lot_id", "OTHER-LOT"), ("grain_orientation", "Y")),
)
def test_stock_lot_and_grain_are_exact_policy_bindings(field: str, value: str) -> None:
    run = _run()
    keys = _keys()
    statements = _statements(run)
    for statement in statements:
        statement["setup"]["stock"][0][field] = value
    _bind_evidence_claims(statements)

    with pytest.raises(WorkshopTrustError, match="stock identity does not match server policy"):
        _verify(run, _registry(keys), _signed_chain(statements, keys))


def test_policy_rejects_coupon_batches_not_in_production_stock() -> None:
    policy = _policy()
    policy["coupon_material_batch_ids"] = ["OTHER-BATCH"]

    with pytest.raises(ValueError, match="coupons must exactly cover production stock batches"):
        WorkshopVerificationPolicy.model_validate(policy)


def test_structured_claim_cannot_change_without_new_typed_evidence_record() -> None:
    run = _run()
    keys = _keys()
    statements = _statements(run)
    statements[1]["reference_part"]["metrology"][0]["measured_um"] = 10_020

    with pytest.raises(WorkshopTrustError, match="not bound to its structured claim"):
        _verify(run, _registry(keys), _signed_chain(statements, keys))


def test_one_evidence_blob_cannot_satisfy_two_workshop_roles() -> None:
    run = _run()
    keys = _keys()
    statements = _statements(run)
    coupon_reference = statements[0]["pre_cut"]["coupons"]["evidence"]
    statements[0]["pre_cut"]["independent_removal_comparison"]["evidence"] = deepcopy(
        coupon_reference
    )

    with pytest.raises(WorkshopTrustError, match="not bound to its structured claim"):
        _verify(run, _registry(keys), _signed_chain(statements, keys))


@pytest.mark.parametrize(
    ("target", "error"),
    (
        ("coupon_batch", "coupon material batches"),
        ("reference_program", "reference part does not match"),
        ("prototype_manifest", "prototype was built from another manifest"),
    ),
)
def test_physical_results_are_cross_bound_to_authoritative_run_and_policy(
    target: str,
    error: str,
) -> None:
    run = _run()
    keys = _keys()
    statements = _statements(run)
    if target == "coupon_batch":
        statements[0]["pre_cut"]["coupons"]["coupons"][0]["material_batch_id"] = (
            "OTHER-BATCH"
        )
    elif target == "reference_program":
        statements[1]["reference_part"]["machine_program_sha256"] = _sha("other-program")
    else:
        statements[2]["final_workshop"]["prototype_build"]["build_manifest_sha256"] = (
            _sha("other-manifest")
        )
    _bind_evidence_claims(statements)

    with pytest.raises(WorkshopTrustError, match=error):
        _verify(run, _registry(keys), _signed_chain(statements, keys))


def test_removal_comparison_must_use_policy_pinned_independent_engine() -> None:
    policy = _policy()
    policy["independent_engine"] = {
        "engine_id": "renamed-independent-engine",
        "engine_version": "9.0.0",
        "binary_sha256": _sha("postprocessor-binary"),
        "config_sha256": _sha("postprocessor-config"),
    }
    run = _run()
    run["workshop_policy_sha256"] = workshop_policy_sha256(
        WorkshopVerificationPolicy.model_validate(policy)
    )
    keys = _keys()
    statements = _statements(run)
    comparison = statements[0]["pre_cut"]["independent_removal_comparison"]
    comparison["comparison_engine_id"] = "renamed-independent-engine"
    comparison["comparison_engine_version"] = "9.0.0"
    comparison["comparison_engine_binary_sha256"] = _sha("postprocessor-binary")
    comparison["comparison_engine_config_sha256"] = _sha("postprocessor-config")
    _bind_evidence_claims(statements)

    with pytest.raises(WorkshopTrustError, match="not the independent server-policy check"):
        _verify(run, _registry(keys), _signed_chain(statements, keys), policy=policy)


def test_validation_only_or_missing_program_identity_never_derives_eligibility() -> None:
    validation_run = _run(kind=VALIDATION_ONLY_MACHINE_PROGRAM_KIND)
    keys = _keys()
    chain = _signed_chain(_statements(validation_run), keys)
    with pytest.raises(WorkshopTrustError, match="executable machine program identity"):
        _verify(validation_run, _registry(keys), chain)

    missing_identity = _run()
    del missing_identity["machine_program_set_sha256"]
    with pytest.raises(WorkshopTrustError, match="expected workshop run is invalid"):
        verify_workshop_attestation_chain(
            trust_registry=_registry(keys),
            attestation_bytes=chain,
            expected_run=missing_identity,
            expected_policy=_policy(),
            expected_server_nonces=NONCES,
            evidence_objects=_evidence_objects(chain),
            now=NOW,
        )


def test_attestation_is_strict_sidecar_and_cannot_claim_bundle_membership() -> None:
    run = _run()
    keys = _keys()
    statements = _statements(run)
    statements[0]["evidence_placement"] = "EMBEDDED_IN_BASE_BUNDLE"
    chain = _signed_chain(statements, keys)

    with pytest.raises(WorkshopTrustError, match="invalid schema"):
        _verify(run, _registry(keys), chain)


@pytest.mark.parametrize(
    ("path", "unsafe_value"),
    (
        (("setup", "tools", 0, "pocket_number"), True),
        (("setup", "stock", 0, "moisture_content_ppm"), 82_000.5),
    ),
)
def test_booleans_and_floats_are_rejected_before_schema_coercion(
    path: tuple[str | int, ...],
    unsafe_value: object,
) -> None:
    run, _keys_by_principal, registry, chain = _valid_fixture()
    payload = json.loads(chain[0])
    cursor: Any = payload["statement"]
    for segment in path[:-1]:
        cursor = cursor[segment]
    cursor[path[-1]] = unsafe_value
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()

    with pytest.raises(WorkshopTrustError, match="unsafe boolean or floating-point"):
        _verify(run, registry, (raw, chain[1], chain[2]))


def test_expired_attestation_and_expired_calibration_fail_closed() -> None:
    run = _run()
    keys = _keys()
    statements = _statements(run)
    statements[2]["expires_at"] = _utc(NOW - timedelta(seconds=1))
    with pytest.raises(WorkshopTrustError, match="attestation is expired"):
        _verify(run, _registry(keys), _signed_chain(statements, keys))

    expired_calibration = _statements(run)
    for statement in expired_calibration:
        statement["setup"]["machine"]["calibration_expires_at"] = _utc(
            NOW - timedelta(minutes=1)
        )
    _bind_evidence_claims(expired_calibration)
    with pytest.raises(WorkshopTrustError, match="calibration is not valid"):
        _verify(run, _registry(keys), _signed_chain(expired_calibration, keys))


def test_later_stage_evidence_cannot_predate_the_prior_attestation() -> None:
    run = _run()
    keys = _keys()
    statements = _statements(run)
    statements[1]["reference_part"]["evidence"]["observed_at"] = _utc(
        NOW - timedelta(days=6, minutes=1)
    )

    with pytest.raises(WorkshopTrustError, match="predates its prior workshop stage"):
        _verify(run, _registry(keys), _signed_chain(statements, keys))


def test_chain_length_stage_order_and_future_issue_time_fail_closed() -> None:
    run, keys, registry, chain = _valid_fixture()
    with pytest.raises(WorkshopTrustError, match="exactly three stages"):
        verify_workshop_attestation_chain(
            trust_registry=registry,
            attestation_bytes=chain[:2],
            expected_run=run,
            expected_policy=_policy(),
            expected_server_nonces=NONCES,
            evidence_objects=_evidence_objects(chain),
            now=NOW,
        )

    statements = _statements(run)
    statements[1]["stage"] = "FINAL_WORKSHOP"
    statements[1]["reference_part"] = None
    statements[1]["final_workshop"] = _final_workshop(run)
    _bind_evidence_claims(statements)
    with pytest.raises(WorkshopTrustError, match="stages are out of order"):
        _verify(run, registry, _signed_chain(statements, keys))

    future = _statements(run)
    future[2]["issued_at"] = _utc(NOW + timedelta(minutes=1))
    with pytest.raises(WorkshopTrustError, match="issued in the future"):
        _verify(run, registry, _signed_chain(future, keys))
