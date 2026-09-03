from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app.joint_retention import (
    RetentionMaterial,
    SignedRetentionEvidence,
    TrustRegistry,
    resolve_joint_retention_contract,
)
from app.joint_retention import (
    _canonical_json_bytes as server_canonical_json_bytes,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from custombuild_domain.models import (
    JointRetentionApplicationClass,
    JointRetentionLoadMode,
)
from pydantic import ValidationError

from scripts import joint_retention_certifier as toolkit

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "packages" / "contracts"


def _schema_definition_accepts_string(
    schema: dict[str, Any], definition_name: str, value: str
) -> bool:
    definition = schema["$defs"][definition_name]
    maximum = definition.get("maxLength")
    return (
        definition["type"] == "string"
        and (maximum is None or len(value) <= maximum)
        and re.fullmatch(definition["pattern"], value) is not None
    )


def request_payload() -> dict[str, Any]:
    return {
        "schema_version": "custombuild.joint-retention-certification-request.v2",
        "signed_evidence_schema_version": "custombuild.joint-retention-signed-evidence.v2",
        "application_class": "load_bearing_carcass_dado",
        "joint_geometry_fingerprint_schema": (
            "custombuild.joint-retention-application-geometry.v1"
        ),
        "source_design_hash": "1" * 64,
        "joint_geometry_sha256": "2" * 64,
        "engine_version": "bookcase-engine-6.0.0",
        "template_version": "bookcase-template-5.0.0",
        "eligible_for_current_binding": True,
        "blocking_issue": None,
        "excluded_applications": [
            {
                "application_class": "captive_inset_back_groove",
                "joint_count": 4,
                "retention_basis": "canonical_four_boundary_geometric_capture",
                "capture_proven": True,
            }
        ],
        "required_materials": [
            {
                "material_id": "birch-plywood",
                "material_version": "screening-2026.1",
                "actual_thickness_um": 18_000,
            }
        ],
        "required_load_cases": [
            {"mode": "shear", "rated_design_load_n": 600},
            {"mode": "withdrawal", "rated_design_load_n": 250},
        ],
        "minimum_safety_factor_permille": 2_000,
    }


def catalogue_entry() -> dict[str, Any]:
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
            {
                "material_id": "birch-plywood",
                "material_version": "screening-2026.1",
            }
        ],
        "minimum_applicable_thickness_um": 17_500,
        "maximum_applicable_thickness_um": 18_500,
        "load_cases": [
            {
                "mode": "shear",
                "rated_design_load_n": 600,
                "verified_capacity_n": 1_500,
            },
            {
                "mode": "withdrawal",
                "rated_design_load_n": 250,
                "verified_capacity_n": 600,
            },
        ],
        "safety_factor_permille": 2_000,
    }


def registry_payload(private_key: Ed25519PrivateKey) -> dict[str, Any]:
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "schema_version": "custombuild.joint-retention-trust-registry.v1",
        "issuers": [
            {
                "issuer_id": "independent-lab",
                "key_id": "retention-2026-01",
                "role": "joint_retention_certifier",
                "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                "not_before": (NOW - timedelta(days=30)).isoformat(),
                "not_after": (NOW + timedelta(days=365)).isoformat(),
                "revoked_at": None,
            }
        ],
        "revoked_statement_sha256": [],
        "revoked_system_versions": [],
    }


def prepare(
    private_key: Ed25519PrivateKey,
) -> tuple[toolkit.CertificationRequest, TrustRegistry, bytes]:
    request = toolkit.CertificationRequest.model_validate(request_payload())
    registry = TrustRegistry.model_validate(registry_payload(private_key))
    payload = toolkit.prepare_payload(
        request=request,
        registry=registry,
        catalogue_entry=catalogue_entry(),
        evidence_id="certification-2026-001",
        issuer_id="independent-lab",
        key_id="retention-2026-01",
        issued_at=(NOW - timedelta(minutes=1)).isoformat(),
        expires_at=(NOW + timedelta(days=90)).isoformat(),
        test_report=b"independent exact-geometry test report bytes",
        test_report_id="report-2026-001",
        test_report_version="1.0.0",
        installation_instruction=b"exact mechanical installation instruction bytes",
        installation_instruction_id="instruction-2026-001",
        installation_instruction_version="1.0.0",
        now=NOW,
    )
    return request, registry, payload


def write_private_key(path: Path, private_key: Ed25519PrivateKey) -> None:
    path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)


def test_prepared_payload_signs_and_cross_checks_with_exact_server_verifier(
    tmp_path: Path,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    request, registry, payload = prepare(private_key)
    raw_payload = json.loads(payload)
    key_file = tmp_path / "certifier-key.pem"
    write_private_key(key_file, private_key)

    assert payload == toolkit.canonical_json_bytes(raw_payload)
    assert payload == server_canonical_json_bytes(raw_payload)
    signed = toolkit.sign_payload(
        payload_bytes=payload,
        request=request,
        registry=registry,
        private_key_path=key_file,
        now=NOW,
    )
    summary = toolkit.verify_evidence(
        evidence_bytes=signed,
        request=request,
        registry=registry,
        now=NOW,
    )

    assert signed == server_canonical_json_bytes(json.loads(signed))
    assert summary == {
        "status": "evidence_verified_for_request",
        "schema_version": "custombuild.joint-retention-signed-evidence.v2",
        "request_source_design_hash": "1" * 64,
        "source_design_hash_authenticated_by_signature": False,
        "joint_geometry_sha256": "2" * 64,
        "evidence_id": "certification-2026-001",
        "evidence_sha256": hashlib.sha256(signed).hexdigest(),
        "issuer_id": "independent-lab",
        "key_id": "retention-2026-01",
        "system_id": "mechanical-dado-lock",
        "system_version": "1.0.0",
        "expires_at": "2026-12-02T12:00:00+00:00",
        "physical_cutting_authorized": False,
    }
    contract = resolve_joint_retention_contract(
        trust_registry=registry.model_dump(mode="json"),
        evidence_bytes=signed,
        expected_application_class=(JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO),
        expected_joint_geometry_sha256=request.joint_geometry_sha256,
        expected_engine_version=request.engine_version,
        expected_template_version=request.template_version,
        required_materials=(("birch-plywood", "screening-2026.1"),),
        required_thicknesses_um=(18_000,),
        required_loads_n=(
            (JointRetentionLoadMode.SHEAR, 600),
            (JointRetentionLoadMode.WITHDRAWAL, 250),
        ),
        minimum_safety_factor_permille=2_000,
        now=NOW,
    )
    assert contract.evidence_sha256 == summary["evidence_sha256"]
    assert contract.hardware_sku == "SCREW-4X40-001"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda payload: payload.__setitem__("joint_geometry_sha256", "3" * 64),
            "geometry/compiler",
        ),
        (
            lambda payload: payload.__setitem__("engine_version", "other-engine-1.0.0"),
            "geometry/compiler",
        ),
        (
            lambda payload: payload["catalogue_entry"].__setitem__(
                "applicable_materials",
                [{"material_id": "mdf", "material_version": "screening-2026.1"}],
            ),
            "request materials",
        ),
        (
            lambda payload: payload["catalogue_entry"]["load_cases"][0].__setitem__(
                "rated_design_load_n", 599
            ),
            "request load binding",
        ),
        (
            lambda payload: payload["catalogue_entry"].__setitem__("safety_factor_permille", 1_999),
            "request safety factor",
        ),
        (
            lambda payload: payload["catalogue_entry"]["load_cases"][0].__setitem__(
                "verified_capacity_n", 1_199
            ),
            "capacity is below",
        ),
    ],
)
def test_payload_rejects_mismatched_request_bindings(
    mutation: Any,
    message: str,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    request, registry, payload_bytes = prepare(private_key)
    payload = json.loads(payload_bytes)
    mutation(payload)

    with pytest.raises(toolkit.CertifierToolkitError, match=message):
        toolkit.validate_payload_binding(payload, request, registry, now=NOW)


def test_stronger_load_and_safety_claims_preserve_the_server_boundary() -> None:
    private_key = Ed25519PrivateKey.generate()
    request, registry, payload_bytes = prepare(private_key)
    payload = json.loads(payload_bytes)
    entry = payload["catalogue_entry"]
    entry["safety_factor_permille"] = 2_500
    entry["load_cases"] = [
        {
            "mode": "shear",
            "rated_design_load_n": 700,
            "verified_capacity_n": 2_000,
        },
        {
            "mode": "withdrawal",
            "rated_design_load_n": 300,
            "verified_capacity_n": 800,
        },
    ]

    validated = toolkit.validate_payload_binding(payload, request, registry, now=NOW)

    assert validated.catalogue_entry.safety_factor_permille == 2_500
    assert tuple(item.rated_design_load_n for item in validated.catalogue_entry.load_cases) == (
        700,
        300,
    )


def test_payload_rejects_unknown_fields_floats_and_noncanonical_bytes() -> None:
    private_key = Ed25519PrivateKey.generate()
    request, registry, payload_bytes = prepare(private_key)
    extra = json.loads(payload_bytes)
    extra["operator_approved"] = True
    with pytest.raises(toolkit.CertifierToolkitError, match="missing, unknown"):
        toolkit.validate_payload_binding(extra, request, registry, now=NOW)

    floating = payload_bytes.replace(
        b'"hardware_count_per_joint":2',
        b'"hardware_count_per_joint":2.0',
    )
    with pytest.raises(toolkit.CertifierToolkitError, match="floating-point"):
        toolkit._json_mapping(floating, label="unsigned payload")

    noncanonical = json.dumps(json.loads(payload_bytes), indent=2).encode("utf-8")
    with pytest.raises(toolkit.CertifierToolkitError, match="canonical JSON"):
        toolkit.sign_payload(
            payload_bytes=noncanonical,
            request=request,
            registry=registry,
            private_key_path=Path("never-read.pem"),
            now=NOW,
        )


def test_signing_requires_owner_only_matching_ed25519_key(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    request, registry, payload = prepare(private_key)
    key_file = tmp_path / "certifier.pem"
    write_private_key(key_file, private_key)
    key_file.chmod(0o644)
    with pytest.raises(toolkit.CertifierToolkitError, match="mode 0600"):
        toolkit.sign_payload(
            payload_bytes=payload,
            request=request,
            registry=registry,
            private_key_path=key_file,
            now=NOW,
        )

    key_file.chmod(0o600)
    wrong_key = Ed25519PrivateKey.generate()
    write_private_key(key_file, wrong_key)
    with pytest.raises(toolkit.CertifierToolkitError, match="does not match"):
        toolkit.sign_payload(
            payload_bytes=payload,
            request=request,
            registry=registry,
            private_key_path=key_file,
            now=NOW,
        )


def test_registry_validation_rejects_unknown_and_ambiguous_metadata(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    raw = registry_payload(private_key)
    raw["trusted_by_browser"] = True
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(toolkit.canonical_json_bytes(raw))
    with pytest.raises(toolkit.CertifierToolkitError, match="invalid or unknown"):
        toolkit.load_registry(registry_path)

    ambiguous = (
        b'{"schema_version":"custombuild.joint-retention-trust-registry.v1",'
        b'"schema_version":"custombuild.joint-retention-trust-registry.v1",'
        b'"issuers":[],"revoked_statement_sha256":[],"revoked_system_versions":[]}'
    )
    registry_path.write_bytes(ambiguous)
    with pytest.raises(toolkit.CertifierToolkitError, match="duplicate"):
        toolkit.load_registry(registry_path)


def test_nonsecret_ceremony_inputs_reject_symlinks(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(toolkit.canonical_json_bytes(registry_payload(private_key)))
    alias = tmp_path / "registry-alias.json"
    alias.symlink_to(registry_path)

    with pytest.raises(toolkit.CertifierToolkitError, match="non-symlink"):
        toolkit.load_registry(alias)


def test_published_schemas_are_strict_and_match_authoritative_model_fields() -> None:
    request_schema = json.loads(
        (CONTRACTS / "joint-retention-certification-request.v2.schema.json").read_bytes()
    )
    evidence_schema = json.loads(
        (CONTRACTS / "joint-retention-signed-evidence.v2.schema.json").read_bytes()
    )
    payload_schema = json.loads(
        (CONTRACTS / "joint-retention-signing-payload.v2.schema.json").read_bytes()
    )
    registry_schema = json.loads(
        (CONTRACTS / "joint-retention-trust-registry.v1.schema.json").read_bytes()
    )

    for schema in (request_schema, payload_schema, evidence_schema, registry_schema):
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
    assert set(request_schema["required"]) == set(toolkit.CertificationRequest.model_fields)
    assert set(payload_schema["required"]) == toolkit._UNSIGNED_FIELDS
    assert set(evidence_schema["required"]) == set(SignedRetentionEvidence.model_fields)
    assert set(registry_schema["required"]) == set(TrustRegistry.model_fields)
    assert evidence_schema["properties"]["schema_version"]["const"] == (
        "custombuild.joint-retention-signed-evidence.v2"
    )
    assert evidence_schema["properties"]["signature_base64"]["pattern"].endswith("[AQgw]==$")
    assert registry_schema["$defs"]["issuer"]["properties"]["public_key_base64"][
        "pattern"
    ].endswith("[AEIMQUYcgkosw048]=$")
    assert evidence_schema["$defs"]["embeddedDocument"]["properties"]["content_base64"][
        "pattern"
    ].endswith("[AEIMQUYcgkosw048]=)?$")
    assert registry_schema["properties"]["issuers"]["items"]["$ref"] == ("#/$defs/issuer")


@pytest.mark.parametrize(
    "schema_filename",
    (
        "joint-retention-certification-request.v2.schema.json",
        "joint-retention-signed-evidence.v2.schema.json",
    ),
)
def test_public_material_keys_match_authoritative_runtime_bounds(
    schema_filename: str,
) -> None:
    schema = json.loads((CONTRACTS / schema_filename).read_bytes())
    material = schema["$defs"]["material"]["properties"]
    version_at_limit = "v" * 80
    version_over_limit = "v" * 81
    material_id_at_limit = "m" * 128
    material_id_over_limit = "m" * 129

    assert material["material_version"]["$ref"] == "#/$defs/versionKey"
    assert _schema_definition_accepts_string(schema, "versionKey", version_at_limit)
    assert not _schema_definition_accepts_string(schema, "versionKey", version_over_limit)

    accepted = RetentionMaterial(
        material_id=material_id_at_limit,
        material_version=version_at_limit,
    )
    assert accepted.material_version == version_at_limit
    with pytest.raises(ValidationError, match="at most 80 characters"):
        RetentionMaterial(
            material_id=material_id_at_limit,
            material_version=version_over_limit,
        )

    assert material["material_id"]["$ref"] == "#/$defs/stableKey"
    assert _schema_definition_accepts_string(schema, "stableKey", material_id_at_limit)
    assert not _schema_definition_accepts_string(schema, "stableKey", material_id_over_limit)
    assert accepted.material_id == material_id_at_limit
    with pytest.raises(ValidationError, match="String should match pattern"):
        RetentionMaterial(
            material_id=material_id_over_limit,
            material_version=version_at_limit,
        )


def test_cli_never_overwrites_outputs_and_reports_only_nonsecret_digests(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_key = Ed25519PrivateKey.generate()
    registry = registry_payload(private_key)
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(toolkit.canonical_json_bytes(registry))

    assert toolkit.main(["validate-registry", "--registry", str(registry_path)]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    summary = json.loads(output.out)
    assert summary["status"] == "valid"
    assert summary["issuer_count"] == 1
    assert "public_key" not in output.out

    occupied = tmp_path / "occupied.json"
    occupied.write_text("keep", encoding="utf-8")
    with pytest.raises(toolkit.CertifierToolkitError, match="new file"):
        toolkit._write_new_file(occupied, b"replacement")
    assert occupied.read_text(encoding="utf-8") == "keep"
