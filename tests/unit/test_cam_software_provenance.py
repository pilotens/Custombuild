from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from custombuild_manufacturing import cam_software_provenance as provenance_module
from custombuild_manufacturing.cam_software_provenance import (
    CAM_CANDIDATE_MANIFEST_SCHEMA_VERSION,
    CAM_CANDIDATE_PACKAGE_BUILDER_VERSION,
    CAM_CANDIDATE_VERIFICATION_DISPATCH_V1,
    CAM_SOFTWARE_PROVENANCE_SCHEMA_VERSION,
    CURRENT_CAM_IMPLEMENTATION_SUPPORT_ID,
    PRODUCER_BUILD_IDENTITY_SCHEMA_VERSION,
    SOURCE_MANIFEST_CODE_ROOT_KIND,
    CAMSoftwareProvenanceError,
    ProducerBuildIdentity,
    build_cam_software_provenance,
    cam_software_provenance_sha256,
    current_cam_implementation_identity,
    current_cam_implementation_versions,
    parse_producer_build_identity,
    parse_supported_cam_implementation_identity,
    supported_cam_implementation_identities,
    validate_cam_software_provenance,
)
from custombuild_manufacturing.cam_software_provenance import (
    test_only_producer_build_identity as _test_only_producer_build_identity,
)
from custombuild_manufacturing.model import canonical_json_bytes, sha256_hex


def _production_identity() -> ProducerBuildIdentity:
    return ProducerBuildIdentity(
        schema_version=PRODUCER_BUILD_IDENTITY_SCHEMA_VERSION,
        app_version="1.7.0",
        vcs_ref="1" * 40,
        source_manifest_sha256="2" * 64,
        dependency_lock_sha256="3" * 64,
    )


def test_production_provenance_is_closed_current_and_canonically_fingerprinted() -> None:
    identity = _production_identity()

    provenance = build_cam_software_provenance(identity)

    assert provenance == {
        "schema_version": CAM_SOFTWARE_PROVENANCE_SCHEMA_VERSION,
        "code_root": {
            "kind": SOURCE_MANIFEST_CODE_ROOT_KIND,
            "sha256": identity.source_manifest_sha256,
        },
        "producer_build": identity.as_dict(),
        "implementations": current_cam_implementation_versions(),
    }
    assert provenance["implementations"]["candidate_manifest_schema_version"] == (
        CAM_CANDIDATE_MANIFEST_SCHEMA_VERSION
    )
    assert provenance["implementations"]["candidate_package_builder_version"] == (
        CAM_CANDIDATE_PACKAGE_BUILDER_VERSION
    )
    assert validate_cam_software_provenance(provenance) == identity
    assert cam_software_provenance_sha256(provenance) == sha256_hex(
        canonical_json_bytes(provenance)
    )

    reordered = {
        "implementations": dict(reversed(tuple(provenance["implementations"].items()))),
        "producer_build": dict(reversed(tuple(provenance["producer_build"].items()))),
        "code_root": dict(reversed(tuple(provenance["code_root"].items()))),
        "schema_version": provenance["schema_version"],
    }
    assert cam_software_provenance_sha256(reordered) == cam_software_provenance_sha256(provenance)


def test_supported_implementation_identity_has_an_explicit_verification_dispatch() -> None:
    versions = current_cam_implementation_versions()

    identity = parse_supported_cam_implementation_identity(versions)

    assert identity == current_cam_implementation_identity()
    assert identity.support_id == CURRENT_CAM_IMPLEMENTATION_SUPPORT_ID
    assert identity.verification_dispatch == CAM_CANDIDATE_VERIFICATION_DISPATCH_V1
    assert identity.as_dict() == versions
    assert supported_cam_implementation_identities() == (identity,)
    support_id, verification_dispatch, implementation_digest = identity.dispatch_key
    assert support_id == CURRENT_CAM_IMPLEMENTATION_SUPPORT_ID
    assert verification_dispatch == CAM_CANDIDATE_VERIFICATION_DISPATCH_V1
    assert implementation_digest == sha256_hex(
        canonical_json_bytes(
            {
                "support_id": support_id,
                "verification_dispatch": verification_dispatch,
                "implementations": versions,
            }
        )
    )


def test_v1_golden_implementation_corpus_resolves_to_the_real_dispatch_key() -> None:
    corpus_path = Path(__file__).parents[1] / "fixtures/cam/cam-implementation-v1.json"
    corpus = json.loads(corpus_path.read_bytes())

    identity = parse_supported_cam_implementation_identity(corpus["implementations"])

    assert identity.dispatch_key == (
        corpus["support_id"],
        corpus["verification_dispatch"],
        corpus["implementation_digest"],
    )
    assert identity == current_cam_implementation_identity()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("support_id", "custombuild.cam-implementation-stack.tampered"),
        ("verification_dispatch", "custombuild.cam-candidate-verifier.tampered"),
        ("gcode_parser_version", "linuxcnc-production-parser-tampered"),
        ("candidate_package_builder_version", "tampered-builder"),
    ),
)
def test_dispatch_key_binds_routing_and_full_implementation_identity(
    field: str,
    value: str,
) -> None:
    identity = current_cam_implementation_identity()

    assert replace(identity, **{field: value}).dispatch_key != identity.dispatch_key


def test_frozen_support_lookup_is_distinct_from_current_runtime_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provenance = build_cam_software_provenance(_production_identity())
    frozen = parse_supported_cam_implementation_identity(provenance["implementations"])
    monkeypatch.setattr(
        provenance_module,
        "PRODUCTION_TOOLPATH_ENGINE_VERSION",
        "production-toolpaths-future",
    )

    assert parse_supported_cam_implementation_identity(provenance["implementations"]) == frozen
    assert (
        validate_cam_software_provenance(
            provenance,
            require_current_implementations=False,
        )
        == _production_identity()
    )
    assert cam_software_provenance_sha256(
        provenance,
        require_current_implementations=False,
    ) == sha256_hex(canonical_json_bytes(provenance))
    with pytest.raises(CAMSoftwareProvenanceError, match="verification dispatch"):
        validate_cam_software_provenance(provenance)


def test_test_only_identity_requires_explicit_test_only_mode() -> None:
    identity = _test_only_producer_build_identity()
    provenance = build_cam_software_provenance(identity, allow_test_only=True)

    assert validate_cam_software_provenance(provenance, allow_test_only=True) == identity
    with pytest.raises(CAMSoftwareProvenanceError, match="production producer vcs_ref"):
        validate_cam_software_provenance(provenance)


def test_provenance_public_guards_fail_closed_on_non_boolean_and_non_mapping_inputs() -> None:
    with pytest.raises(CAMSoftwareProvenanceError, match="allow_test_only"):
        parse_producer_build_identity(_production_identity().as_dict(), allow_test_only=1)  # type: ignore[arg-type]
    with pytest.raises(CAMSoftwareProvenanceError, match="engine context"):
        provenance_module.producer_build_identity_from_engine_context([])
    with pytest.raises(CAMSoftwareProvenanceError, match="explicit boolean"):
        validate_cam_software_provenance({}, require_current_implementations=1)  # type: ignore[arg-type]


def test_engine_context_projection_and_expected_mapping_are_exact() -> None:
    identity = _production_identity()
    context = {
        "app_version": identity.app_version,
        "vcs_ref": identity.vcs_ref,
        "source_manifest_sha256": identity.source_manifest_sha256,
        "dependency_lock_sha256": identity.dependency_lock_sha256,
        "ignored_build_fact": "not-projected",
    }
    provenance = build_cam_software_provenance(identity)

    assert provenance_module.producer_build_identity_from_engine_context(context) == identity
    assert (
        validate_cam_software_provenance(
            provenance,
            expected_producer_build=identity.as_dict(),
        )
        == identity
    )
    with pytest.raises(CAMSoftwareProvenanceError, match="independently bound build"):
        validate_cam_software_provenance(
            provenance,
            expected_producer_build={**identity.as_dict(), "app_version": "different"},
        )


def test_registry_alias_invalid_fields_and_schema_version_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = current_cam_implementation_identity()
    alias = replace(identity, support_id="custombuild.cam-implementation-stack.alias")
    monkeypatch.setattr(
        provenance_module,
        "_SUPPORTED_CAM_IMPLEMENTATION_IDENTITIES",
        {identity.support_id: identity, alias.support_id: alias},
    )
    with pytest.raises(CAMSoftwareProvenanceError, match="aliases"):
        supported_cam_implementation_identities()

    monkeypatch.setattr(
        provenance_module,
        "_SUPPORTED_CAM_IMPLEMENTATION_IDENTITIES",
        {identity.support_id: identity},
    )
    for value in (None, "", " whitespace ", "x" * 201):
        versions = identity.as_dict()
        versions["gcode_parser_version"] = value  # type: ignore[assignment]
        with pytest.raises(CAMSoftwareProvenanceError, match="identity is invalid"):
            parse_supported_cam_implementation_identity(versions)

    provenance = build_cam_software_provenance(_production_identity())
    provenance["schema_version"] = "unsupported"
    with pytest.raises(CAMSoftwareProvenanceError, match="schema is unsupported"):
        validate_cam_software_provenance(provenance)


@pytest.mark.parametrize("field", ("app_version", "vcs_ref"))
@pytest.mark.parametrize("value", (None, "", " whitespace ", "x" * 201))
def test_producer_text_fields_are_canonical(field: str, value: object) -> None:
    raw = _production_identity().as_dict()
    raw[field] = value  # type: ignore[assignment]

    with pytest.raises(CAMSoftwareProvenanceError, match=f"producer build {field}"):
        parse_producer_build_identity(raw, allow_test_only=True)


@pytest.mark.parametrize("container", ("outer", "code_root", "producer_build", "implementations"))
@pytest.mark.parametrize("mutation", ("missing", "extra"))
def test_every_provenance_object_rejects_missing_or_extra_keys(
    container: str,
    mutation: str,
) -> None:
    provenance = build_cam_software_provenance(_production_identity())
    mutated = deepcopy(provenance)
    targets = {
        "outer": mutated,
        "code_root": mutated["code_root"],
        "producer_build": mutated["producer_build"],
        "implementations": mutated["implementations"],
    }
    target = targets[container]
    if mutation == "missing":
        target.pop(next(iter(target)))
    else:
        target["unexpected"] = "unsafe"

    with pytest.raises(CAMSoftwareProvenanceError):
        validate_cam_software_provenance(mutated)


@pytest.mark.parametrize("field", tuple(current_cam_implementation_versions()))
def test_every_stale_implementation_version_is_rejected(field: str) -> None:
    provenance = build_cam_software_provenance(_production_identity())
    provenance["implementations"][field] = "stale-or-forged"

    with pytest.raises(CAMSoftwareProvenanceError, match="unsupported or stale"):
        validate_cam_software_provenance(provenance)


def test_code_root_must_equal_the_producer_source_manifest() -> None:
    provenance = build_cam_software_provenance(_production_identity())
    provenance["code_root"]["sha256"] = "4" * 64

    with pytest.raises(CAMSoftwareProvenanceError, match="code root differs"):
        validate_cam_software_provenance(provenance)


@pytest.mark.parametrize("value", (None, False, 0, "A" * 64, "2" * 63))
def test_code_root_digest_is_strictly_typed_and_canonical(value: object) -> None:
    provenance = build_cam_software_provenance(_production_identity())
    provenance["code_root"]["sha256"] = value

    with pytest.raises(CAMSoftwareProvenanceError, match="code-root binding is invalid"):
        validate_cam_software_provenance(provenance)


def test_independently_expected_producer_identity_must_match() -> None:
    identity = _production_identity()
    provenance = build_cam_software_provenance(identity)
    different_expected = ProducerBuildIdentity(
        **{
            **identity.as_dict(),
            "source_manifest_sha256": "5" * 64,
        }
    )

    with pytest.raises(CAMSoftwareProvenanceError, match="independently bound build"):
        validate_cam_software_provenance(
            provenance,
            expected_producer_build=different_expected,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", "custombuild.producer-build-identity.v0"),
        ("vcs_ref", "not-a-production-revision"),
        ("source_manifest_sha256", "A" * 64),
        ("dependency_lock_sha256", "3" * 63),
    ),
)
def test_producer_build_identity_rejects_noncanonical_values(
    field: str,
    value: str,
) -> None:
    raw = {**_production_identity().as_dict(), field: value}

    with pytest.raises(CAMSoftwareProvenanceError):
        parse_producer_build_identity(raw)
