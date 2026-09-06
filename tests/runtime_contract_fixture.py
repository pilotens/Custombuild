"""Synthetic runtime evidence for compiler tests, never a workshop measurement."""

from custombuild_postprocessors.runtime_contract import (
    LINUXCNC_ORACLE_BUILD_SHA256,
    LINUXCNC_ORACLE_PACKAGE_VERSION,
    REQUIRED_RUNTIME_SETTINGS,
    RUNTIME_COMPONENTS,
    RUNTIME_CONTRACT_SCHEMA_VERSION,
    RUNTIME_INPUTS,
    LinuxCNCRuntimeContract,
    RuntimeInventoryEntry,
    RuntimeSetting,
)


def runtime_contract_fixture() -> LinuxCNCRuntimeContract:
    return LinuxCNCRuntimeContract(
        schema_version=RUNTIME_CONTRACT_SCHEMA_VERSION,
        oracle_build_sha256=LINUXCNC_ORACLE_BUILD_SHA256,
        installed_package_version=LINUXCNC_ORACLE_PACKAGE_VERSION,
        effective_settings=tuple(
            RuntimeSetting(name, value) for name, value in sorted(REQUIRED_RUNTIME_SETTINGS.items())
        ),
        components=tuple(
            RuntimeInventoryEntry(name, "a" * 64) for name in sorted(RUNTIME_COMPONENTS)
        ),
        configuration_inputs=tuple(
            RuntimeInventoryEntry(name, "b" * 64) for name in sorted(RUNTIME_INPUTS)
        ),
        min_forward_velocity_rpm=6000,
        max_forward_velocity_rpm=24000,
        evidence_id="runtime-acceptance-2026-09",
        evidence_version="1.0.0",
        evidence_sha256="c" * 64,
        workshop_runtime_verified=True,
    )
