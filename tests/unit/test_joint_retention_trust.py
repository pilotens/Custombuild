from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.joint_retention import (
    JOINT_GEOMETRY_FINGERPRINT_SCHEMA,
    JOINT_RETENTION_CERTIFIER_ROLE,
    SIGNED_EVIDENCE_SCHEMA_VERSION,
    TRUST_REGISTRY_SCHEMA_VERSION,
    JointRetentionTrustError,
    resolve_joint_retention_contract,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from custombuild_domain.models import JointRetentionApplicationClass

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
GEOMETRY_SHA256 = "a" * 64


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _document(document_id: str, version: str, content: bytes) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "document_version": version,
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def _entry() -> dict[str, Any]:
    return {
        "system_id": "mechanical-dado-lock",
        "system_version": "1.0.0",
        "joint_type": "dado",
        "application_class": "load_bearing_carcass_dado",
        "method": "mechanical",
        "machining_scope": "no_additional_cnc",
        "hardware_sku": "SCREW-4X40-001",
        "hardware_count_per_joint": 2,
        "applicable_materials": [
            {"material_id": "mdf", "material_version": "screening-2026.1"},
            {"material_id": "mdf-6", "material_version": "screening-2026.1"},
        ],
        "minimum_applicable_thickness_um": 6_000,
        "maximum_applicable_thickness_um": 18_500,
        "load_cases": [
            {
                "mode": "shear",
                "rated_design_load_n": 300,
                "verified_capacity_n": 900,
            },
            {
                "mode": "withdrawal",
                "rated_design_load_n": 200,
                "verified_capacity_n": 600,
            },
        ],
        "safety_factor_permille": 2_000,
    }


def _signed_evidence(
    private_key: Ed25519PrivateKey,
    *,
    entry: dict[str, Any] | None = None,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> bytes:
    payload = {
        "schema_version": SIGNED_EVIDENCE_SCHEMA_VERSION,
        "evidence_id": "retention-test-2026-001",
        "issuer_id": "test-lab",
        "key_id": "ed25519-2026-01",
        "issued_at": (issued_at or NOW - timedelta(days=1)).isoformat(),
        "expires_at": (expires_at or NOW + timedelta(days=365)).isoformat(),
        "application_class": "load_bearing_carcass_dado",
        "joint_geometry_fingerprint_schema": JOINT_GEOMETRY_FINGERPRINT_SCHEMA,
        "engine_version": "bookcase-engine-6.0.0",
        "template_version": "bookcase-template-5.0.0",
        "joint_geometry_sha256": GEOMETRY_SHA256,
        "catalogue_entry": entry or _entry(),
        "test_report": _document(
            "retention-report-2026-001",
            "1.0.0",
            b"independent retention test report bytes",
        ),
        "installation_instruction": _document(
            "install-mechanical-dado-lock",
            "1.0.0",
            b"exact installation instruction bytes",
        ),
    }
    payload["signature_base64"] = base64.b64encode(
        private_key.sign(_canonical_json_bytes(payload))
    ).decode("ascii")
    return _canonical_json_bytes(payload)


def _registry(
    private_key: Ed25519PrivateKey,
    *,
    revoked_statement_sha256: tuple[str, ...] = (),
    revoked_system_versions: tuple[str, ...] = (),
    revoked_at: datetime | None = None,
) -> dict[str, Any]:
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "schema_version": TRUST_REGISTRY_SCHEMA_VERSION,
        "issuers": [
            {
                "issuer_id": "test-lab",
                "key_id": "ed25519-2026-01",
                "role": JOINT_RETENTION_CERTIFIER_ROLE,
                "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                "not_before": (NOW - timedelta(days=30)).isoformat(),
                "not_after": (NOW + timedelta(days=730)).isoformat(),
                "revoked_at": revoked_at.isoformat() if revoked_at else None,
            }
        ],
        "revoked_statement_sha256": list(revoked_statement_sha256),
        "revoked_system_versions": list(revoked_system_versions),
    }


def _resolve(
    evidence_bytes: bytes,
    registry: dict[str, Any],
):
    return resolve_joint_retention_contract(
        trust_registry=registry,
        evidence_bytes=evidence_bytes,
        expected_application_class=(
            JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
        ),
        expected_joint_geometry_sha256=GEOMETRY_SHA256,
        expected_engine_version="bookcase-engine-6.0.0",
        expected_template_version="bookcase-template-5.0.0",
        required_materials=(("mdf", "screening-2026.1"),),
        required_thicknesses_um=(18_000,),
        now=NOW,
    )


def test_trusted_signed_evidence_resolves_exact_contract() -> None:
    private_key = Ed25519PrivateKey.generate()
    evidence_bytes = _signed_evidence(private_key)

    contract = _resolve(evidence_bytes, _registry(private_key))

    assert contract.system_id == "mechanical-dado-lock"
    assert contract.evidence_id == "retention-test-2026-001"
    assert contract.evidence_sha256 == hashlib.sha256(evidence_bytes).hexdigest()
    assert contract.joint_geometry_sha256 == GEOMETRY_SHA256
    assert (
        contract.installation_instruction_sha256
        == hashlib.sha256(b"exact installation instruction bytes").hexdigest()
    )
    assert tuple(item.mode.value for item in contract.load_cases) == (
        "shear",
        "withdrawal",
    )
    assert tuple(
        (item.material_id, item.material_version) for item in contract.applicable_materials
    ) == (
        ("mdf", "screening-2026.1"),
        ("mdf-6", "screening-2026.1"),
    )


def test_signature_tampering_and_wrong_key_fail_closed() -> None:
    private_key = Ed25519PrivateKey.generate()
    evidence_bytes = _signed_evidence(private_key)
    tampered = json.loads(evidence_bytes)
    tampered["catalogue_entry"]["hardware_count_per_joint"] = 3

    with pytest.raises(JointRetentionTrustError, match="signature is invalid"):
        _resolve(_canonical_json_bytes(tampered), _registry(private_key))

    with pytest.raises(JointRetentionTrustError, match="signature is invalid"):
        _resolve(evidence_bytes, _registry(Ed25519PrivateKey.generate()))


def test_geometry_compiler_binding_and_canonical_bytes_fail_closed() -> None:
    private_key = Ed25519PrivateKey.generate()
    payload = json.loads(_signed_evidence(private_key))
    payload["joint_geometry_sha256"] = "b" * 64
    payload.pop("signature_base64")
    payload["signature_base64"] = base64.b64encode(
        private_key.sign(_canonical_json_bytes(payload))
    ).decode("ascii")
    with pytest.raises(
        JointRetentionTrustError,
        match="another application, geometry or compiler",
    ):
        _resolve(_canonical_json_bytes(payload), _registry(private_key))

    canonical = _signed_evidence(private_key)
    reserialized = json.dumps(json.loads(canonical), indent=2).encode("utf-8")
    with pytest.raises(JointRetentionTrustError, match="canonical JSON bytes"):
        _resolve(reserialized, _registry(private_key))


def test_revoked_statement_system_and_key_fail_closed() -> None:
    private_key = Ed25519PrivateKey.generate()
    evidence_bytes = _signed_evidence(private_key)
    digest = hashlib.sha256(evidence_bytes).hexdigest()

    with pytest.raises(JointRetentionTrustError, match="statement is revoked"):
        _resolve(
            evidence_bytes,
            _registry(private_key, revoked_statement_sha256=(digest,)),
        )
    with pytest.raises(JointRetentionTrustError, match="system version is revoked"):
        _resolve(
            evidence_bytes,
            _registry(
                private_key,
                revoked_system_versions=("mechanical-dado-lock@1.0.0",),
            ),
        )
    with pytest.raises(JointRetentionTrustError, match="issuer key is revoked"):
        _resolve(evidence_bytes, _registry(private_key, revoked_at=NOW - timedelta(seconds=1)))


def test_expired_future_and_out_of_window_evidence_fail_closed() -> None:
    private_key = Ed25519PrivateKey.generate()
    with pytest.raises(JointRetentionTrustError, match="expired"):
        _resolve(
            _signed_evidence(private_key, expires_at=NOW - timedelta(seconds=1)),
            _registry(private_key),
        )
    with pytest.raises(JointRetentionTrustError, match="issued in the future"):
        _resolve(
            _signed_evidence(private_key, issued_at=NOW + timedelta(seconds=1)),
            _registry(private_key),
        )


def test_embedded_document_checksum_is_verified_after_signature() -> None:
    private_key = Ed25519PrivateKey.generate()
    payload = json.loads(_signed_evidence(private_key))
    payload["test_report"]["content_base64"] = base64.b64encode(b"different").decode()
    payload.pop("signature_base64")
    payload["signature_base64"] = base64.b64encode(
        private_key.sign(_canonical_json_bytes(payload))
    ).decode("ascii")

    with pytest.raises(JointRetentionTrustError, match="test report checksum"):
        _resolve(_canonical_json_bytes(payload), _registry(private_key))


def test_material_and_thickness_applicability_fail_closed() -> None:
    private_key = Ed25519PrivateKey.generate()
    entry = _entry()
    entry["applicable_materials"] = entry["applicable_materials"][1:]
    with pytest.raises(JointRetentionTrustError, match="every material version"):
        _resolve(_signed_evidence(private_key, entry=entry), _registry(private_key))

    entry = _entry()
    entry["minimum_applicable_thickness_um"] = 19_000
    with pytest.raises(JointRetentionTrustError, match="every member thickness"):
        _resolve(_signed_evidence(private_key, entry=entry), _registry(private_key))


def test_unknown_fields_and_non_certifier_registry_fail_closed() -> None:
    private_key = Ed25519PrivateKey.generate()
    evidence = json.loads(_signed_evidence(private_key))
    evidence["browser_verified"] = True
    with pytest.raises(JointRetentionTrustError, match="invalid schema"):
        _resolve(_canonical_json_bytes(evidence), _registry(private_key))

    registry = _registry(private_key)
    registry["issuers"][0]["role"] = "designer"
    with pytest.raises(JointRetentionTrustError, match="trust registry is invalid"):
        _resolve(_signed_evidence(private_key), registry)
