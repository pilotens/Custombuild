from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from custombuild_postprocessors import GCodeSafetyError, LinuxCNCProductionPostprocessor
from custombuild_postprocessors.runtime_contract import (
    LINUXCNC_ORACLE_BUILD_SHA256,
    LINUXCNC_ORACLE_CONTAINER_IMAGE,
    LINUXCNC_ORACLE_DEBIAN_SNAPSHOT,
    LINUXCNC_ORACLE_PACKAGE_VERSION,
    REQUIRED_RUNTIME_SETTINGS,
    LinuxCNCRuntimeContract,
)

from scripts.production_machine_profile import _postprocessor_draft, production_profile_json_schema
from tests.runtime_contract_fixture import runtime_contract_fixture
from tests.unit.test_linuxcnc_production_postprocessor import (
    production_document,
    production_machine_profile,
)
from tests.unit.test_production_machine_profile import (
    _document,
    _payload,
    _resign_postprocessor,
)


@pytest.mark.parametrize("name", sorted(REQUIRED_RUNTIME_SETTINGS))
def test_every_effective_runtime_deviation_is_rejected(name: str) -> None:
    payload = runtime_contract_fixture().as_dict()
    setting = next(item for item in payload["effective_settings"] if item["name"] == name)
    original = setting["value"]
    setting["value"] = (
        not original
        if type(original) is bool
        else (original + 1 if type(original) is int else "another-runtime")
    )
    with pytest.raises(ValueError, match="unsupported effective LinuxCNC setting"):
        LinuxCNCRuntimeContract.from_mapping(payload)


@pytest.mark.parametrize("field", sorted(runtime_contract_fixture().as_dict()))
def test_runtime_contract_requires_every_field(field: str) -> None:
    payload = runtime_contract_fixture().as_dict()
    del payload[field]
    with pytest.raises(ValueError, match="missing or unknown"):
        LinuxCNCRuntimeContract.from_mapping(payload)


@pytest.mark.parametrize("key", ["effective_settings", "components", "configuration_inputs"])
def test_inventory_cannot_be_partial_duplicate_or_reordered(key: str) -> None:
    original = runtime_contract_fixture().as_dict()
    for changed in (original[key][:-1], original[key] + original[key][:1], original[key][::-1]):
        payload = {**original, key: changed}
        with pytest.raises(ValueError, match="exactly cover|incomplete or non-canonical"):
            LinuxCNCRuntimeContract.from_mapping(payload)


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema_version", "another-schema"),
        ("oracle_build_sha256", "0" * 64),
        ("installed_package_version", "1:2.9.5"),
        ("evidence_id", ""),
        ("evidence_version", "bad version"),
        ("evidence_sha256", "broken-digest"),
        ("workshop_runtime_verified", False),
        ("workshop_runtime_verified", 1),
        ("min_forward_velocity_rpm", 0),
        ("max_forward_velocity_rpm", 6000),
        ("min_forward_velocity_rpm", True),
    ],
)
def test_runtime_build_evidence_and_limits_fail_closed(key: str, value: object) -> None:
    payload = runtime_contract_fixture().as_dict()
    payload[key] = value
    with pytest.raises(ValueError):
        LinuxCNCRuntimeContract.from_mapping(payload)


@pytest.mark.parametrize("key", ["components", "configuration_inputs"])
def test_inventory_hash_is_validated_and_bound(key: str) -> None:
    original = runtime_contract_fixture()
    payload = original.as_dict()
    payload[key][0]["sha256"] = "bad"
    with pytest.raises(ValueError, match="SHA-256"):
        LinuxCNCRuntimeContract.from_mapping(payload)
    payload[key][0]["sha256"] = "d" * 64
    changed = LinuxCNCRuntimeContract.from_mapping(payload)
    assert changed.sha256 != original.sha256
    profile = production_machine_profile()
    assert replace(profile, runtime_contract=changed).config_sha256 != profile.config_sha256


def test_spindle_clamp_drift_is_rejected_even_with_recomputed_profile_hash() -> None:
    from custombuild_manufacturing.production_machine_profile import (
        ProductionMachineProfileError,
        load_production_machine_profile,
    )

    payload = _payload()
    payload["postprocessor_profile"]["runtime_contract"]["max_forward_velocity_rpm"] = 23000
    _resign_postprocessor(payload)
    with pytest.raises(ProductionMachineProfileError, match="spindle clamps"):
        load_production_machine_profile(_document(payload), allow_test_only=True)
    profile = production_machine_profile()
    drifted = replace(
        profile,
        runtime_contract=replace(
            profile.runtime_contract,
            max_forward_velocity_rpm=23000,
        ),
    )
    with pytest.raises(GCodeSafetyError, match="does not exactly match"):
        LinuxCNCProductionPostprocessor(drifted).generate(production_document())


def test_authoring_never_claims_workshop_runtime_observations() -> None:
    assert _postprocessor_draft()["runtime_contract"] == {
        "$unresolved": "WORKSHOP_INPUT_REQUIRED",
    }
    schema = production_profile_json_schema()
    profile = schema["properties"]["payload"]["properties"]["postprocessor_profile"]
    runtime = profile["properties"]["runtime_contract"]
    assert runtime["additionalProperties"] is False
    assert runtime["properties"]["workshop_runtime_verified"] == {"const": True}
    assert runtime["properties"]["oracle_build_sha256"] == {"const": LINUXCNC_ORACLE_BUILD_SHA256}


def test_runtime_oracle_build_is_bound_to_all_three_ci_jobs() -> None:
    assert LINUXCNC_ORACLE_BUILD_SHA256 == (
        "687554236c4e62e98a98d6d430e9f97e7ff93c62f800d3ff15f329e19642b8ab"
    )
    for name in ("ci.yml", "prod-ci.yml", "cd.yml"):
        workflow = yaml.safe_load((Path(".github/workflows") / name).read_text())
        job = workflow["jobs"]["linuxcnc-interpreter-oracle"]
        assert job["container"]["image"] == LINUXCNC_ORACLE_CONTAINER_IMAGE
        install = job["steps"][0]["run"]
        assert install.count(LINUXCNC_ORACLE_DEBIAN_SNAPSHOT) == 2
        assert f"linuxcnc-uspace={LINUXCNC_ORACLE_PACKAGE_VERSION}" in install


@pytest.mark.parametrize("version", ["2.9.5", "2.9", "2.8.4"])
def test_unqualified_controller_version_is_rejected(version: str) -> None:
    with pytest.raises(ValueError, match="oracle-qualified"):
        replace(production_machine_profile(), controller_version=version)
