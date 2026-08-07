"""Canonical identity for every implementation used by production generation.

The API freezes this context when it enqueues a job.  A worker independently
resolves the same versioned components and refuses to emit artifacts if any
field has drifted.  Version constants intentionally live beside their owning
implementations; this module only composes and fingerprints them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from typing import Any

from custombuild_cad import (
    CAD_KERNEL_CONTRACT_VERSION,
    CADQUERY_ADAPTER_VERSION,
    CADQUERY_DISTRIBUTION_VERSION,
    OPENCASCADE_DISTRIBUTION,
    OPENCASCADE_DISTRIBUTION_VERSION,
)
from custombuild_cam import CAM_BACKPLOT_VERSION, CAM_VALIDATION_VERSION
from custombuild_domain import (
    BOOKCASE_ENGINE_VERSION,
    BOOKCASE_JOINT_SUPPORT_VERSION,
    BOOKCASE_TEMPLATE_VERSION,
    joint_support_payload,
)
from custombuild_postprocessors import (
    GCODE_PARSER_VERSION,
    GCODE_SAFETY_VALIDATOR_VERSION,
    LinuxCNCValidationPostprocessor,
)
from custombuild_rules import RULES_VERSION

from .adapters import MANUFACTURING_ADAPTER_VERSION
from .dfm import DFM_ENGINE_VERSION
from .exporters import ARTIFACT_EXPORTERS_VERSION
from .model import MachineProfile, canonical_data, canonical_json_bytes, sha256_hex
from .nesting import NESTING_ALGORITHM_VERSION
from .operations import OPERATIONS_ENGINE_VERSION, OPERATIONS_SCHEMA_VERSION
from .package import (
    ARTIFACT_SCHEMA_VERSION,
    PACKAGE_BUILDER_VERSION,
    PRODUCTION_MANIFEST_SCHEMA_VERSION,
)
from .pipeline import PRODUCTION_PIPELINE_VERSION
from .profiles import (
    REFERENCE_MACHINE_PROFILE_ID,
    linuxcnc_reference_router_1325,
    tool_catalog_fingerprint,
)

PRODUCTION_ENGINE_CONTEXT_SCHEMA_VERSION = "custombuild.production-engine-context.v1"
GENERATION_CONTEXT_SCHEMA_VERSION = "custombuild.generation-context.v1"
APPLICATION_ARTIFACT_SCHEMA_VERSION = "custombuild.application-artifacts.v1"
DOCUMENT_RENDERER_VERSION = "reportlab-production-documents-1.0.0"
REPORTLAB_DISTRIBUTION_VERSION = "4.4.9"


class ProductionContextError(ValueError):
    """A requested or persisted production component cannot be reproduced."""


@dataclass(frozen=True, slots=True)
class ProductionEngineContext:
    schema_version: str
    app_version: str
    domain_engine_version: str
    template_version: str
    rules_version: str
    joint_support_version: str
    joint_support_fingerprint: str
    manufacturing_adapter_version: str
    dfm_engine_version: str
    nesting_algorithm_version: str
    operations_engine_version: str
    operations_schema_version: str
    artifact_exporters_version: str
    production_pipeline_version: str
    package_builder_version: str
    manifest_schema_version: str
    artifact_schema_version: str
    application_artifact_schema_version: str
    document_renderer_version: str
    reportlab_version: str
    cad_adapter_version: str
    cad_kernel_contract_version: str
    cadquery_version: str
    opencascade_distribution: str
    opencascade_version: str
    cam_validation_version: str
    cam_backplot_version: str
    machine_profile_id: str
    machine_profile_version: str
    machine_profile_fingerprint: str
    tool_library_version: str
    tool_library_fingerprint: str
    postprocessor_id: str
    postprocessor_version: str
    gcode_parser_version: str
    gcode_safety_validator_version: str

    def as_dict(self) -> dict[str, Any]:
        value = canonical_data(self)
        if not isinstance(value, dict):  # pragma: no cover - dataclass invariant
            raise TypeError("production engine context did not serialize to an object")
        return {str(key): item for key, item in value.items()}

    @property
    def fingerprint(self) -> str:
        return sha256_hex(canonical_json_bytes(self))


@dataclass(frozen=True, slots=True)
class ResolvedProductionComponents:
    context: ProductionEngineContext
    machine: MachineProfile
    postprocessor: LinuxCNCValidationPostprocessor


def resolve_production_components(
    *,
    machine_profile_id: str,
    postprocessor_id: str,
    app_version: str,
    require_cad_runtime: bool = False,
) -> ResolvedProductionComponents:
    """Resolve the named catalog entries and freeze their complete identity."""

    if machine_profile_id != REFERENCE_MACHINE_PROFILE_ID:
        raise ProductionContextError(f"unknown or unverified machine profile: {machine_profile_id}")
    machine = linuxcnc_reference_router_1325()

    postprocessor = LinuxCNCValidationPostprocessor()
    if postprocessor_id != postprocessor.version:
        raise ProductionContextError(f"unknown or unverified postprocessor: {postprocessor_id}")

    _require_distribution("reportlab", REPORTLAB_DISTRIBUTION_VERSION)
    if require_cad_runtime:
        _require_distribution("cadquery", CADQUERY_DISTRIBUTION_VERSION)
        _require_distribution(OPENCASCADE_DISTRIBUTION, OPENCASCADE_DISTRIBUTION_VERSION)

    machine_snapshot = canonical_data(machine)
    joint_snapshot = joint_support_payload()
    context = ProductionEngineContext(
        schema_version=PRODUCTION_ENGINE_CONTEXT_SCHEMA_VERSION,
        app_version=app_version,
        domain_engine_version=BOOKCASE_ENGINE_VERSION,
        template_version=BOOKCASE_TEMPLATE_VERSION,
        rules_version=RULES_VERSION,
        joint_support_version=BOOKCASE_JOINT_SUPPORT_VERSION,
        joint_support_fingerprint=sha256_hex(canonical_json_bytes(joint_snapshot)),
        manufacturing_adapter_version=MANUFACTURING_ADAPTER_VERSION,
        dfm_engine_version=DFM_ENGINE_VERSION,
        nesting_algorithm_version=NESTING_ALGORITHM_VERSION,
        operations_engine_version=OPERATIONS_ENGINE_VERSION,
        operations_schema_version=OPERATIONS_SCHEMA_VERSION,
        artifact_exporters_version=ARTIFACT_EXPORTERS_VERSION,
        production_pipeline_version=PRODUCTION_PIPELINE_VERSION,
        package_builder_version=PACKAGE_BUILDER_VERSION,
        manifest_schema_version=PRODUCTION_MANIFEST_SCHEMA_VERSION,
        artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
        application_artifact_schema_version=APPLICATION_ARTIFACT_SCHEMA_VERSION,
        document_renderer_version=DOCUMENT_RENDERER_VERSION,
        reportlab_version=REPORTLAB_DISTRIBUTION_VERSION,
        cad_adapter_version=CADQUERY_ADAPTER_VERSION,
        cad_kernel_contract_version=CAD_KERNEL_CONTRACT_VERSION,
        cadquery_version=CADQUERY_DISTRIBUTION_VERSION,
        opencascade_distribution=OPENCASCADE_DISTRIBUTION,
        opencascade_version=OPENCASCADE_DISTRIBUTION_VERSION,
        cam_validation_version=CAM_VALIDATION_VERSION,
        cam_backplot_version=CAM_BACKPLOT_VERSION,
        machine_profile_id=machine.profile_id,
        machine_profile_version=machine.version,
        machine_profile_fingerprint=sha256_hex(canonical_json_bytes(machine_snapshot)),
        tool_library_version=machine.tool_library_version,
        tool_library_fingerprint=tool_catalog_fingerprint(machine.tools),
        postprocessor_id=postprocessor_id,
        postprocessor_version=postprocessor.version,
        gcode_parser_version=GCODE_PARSER_VERSION,
        gcode_safety_validator_version=GCODE_SAFETY_VALIDATOR_VERSION,
    )
    return ResolvedProductionComponents(context, machine, postprocessor)


def assert_frozen_design_versions(
    *,
    engine_version: str,
    template_version: str,
    rule_version: str,
) -> None:
    expected = {
        "engine_version": BOOKCASE_ENGINE_VERSION,
        "template_version": f"bookcase@{BOOKCASE_TEMPLATE_VERSION}",
        "rule_version": f"bookcase-rules@{RULES_VERSION}",
    }
    actual = {
        "engine_version": engine_version,
        "template_version": template_version,
        "rule_version": rule_version,
    }
    if actual != expected:
        raise ProductionContextError(
            "frozen design libraries are stale; create and validate a new design revision"
        )


def generation_context_payload(
    *,
    design_context_hash: str,
    design_version_id: str,
    revision: int,
    request: Mapping[str, Any],
    production_engine_context: ProductionEngineContext | Mapping[str, Any],
) -> dict[str, Any]:
    engine_context = (
        production_engine_context.as_dict()
        if isinstance(production_engine_context, ProductionEngineContext)
        else canonical_data(production_engine_context)
    )
    if not isinstance(engine_context, dict):
        raise ProductionContextError("production engine context must be an object")
    return {
        "schema_version": GENERATION_CONTEXT_SCHEMA_VERSION,
        "design_context_hash": design_context_hash,
        "design_version_id": design_version_id,
        "revision": revision,
        "request": canonical_data(request),
        "production_engine_context": engine_context,
    }


def generation_context_hash(
    *,
    design_context_hash: str,
    design_version_id: str,
    revision: int,
    request: Mapping[str, Any],
    production_engine_context: ProductionEngineContext | Mapping[str, Any],
) -> str:
    return sha256_hex(
        canonical_json_bytes(
            generation_context_payload(
                design_context_hash=design_context_hash,
                design_version_id=design_version_id,
                revision=revision,
                request=request,
                production_engine_context=production_engine_context,
            )
        )
    )


def contexts_equal(
    stored: Mapping[str, Any],
    current: ProductionEngineContext,
) -> bool:
    return canonical_json_bytes(stored) == canonical_json_bytes(current.as_dict())


def _require_distribution(distribution: str, expected_version: str) -> None:
    try:
        actual_version = distribution_version(distribution)
    except PackageNotFoundError as exc:
        raise ProductionContextError(
            f"required production dependency is missing: {distribution}@{expected_version}"
        ) from exc
    if actual_version != expected_version:
        raise ProductionContextError(
            f"production dependency drift: {distribution}@{actual_version}; "
            f"expected {expected_version}"
        )
