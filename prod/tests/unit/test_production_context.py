from __future__ import annotations

from dataclasses import fields, replace

import custombuild_manufacturing.production_context as production_context
import pytest
from custombuild_manufacturing.production_context import (
    CADQUERY_DISTRIBUTION_VERSION,
    REPORTLAB_DISTRIBUTION_VERSION,
    ProductionContextError,
    ProductionEngineContext,
    generation_context_hash,
    resolve_production_components,
)

APP_VERSION = "test-app-1.0.0"
REQUEST = {
    "machine_profile_id": "custombuild-router-1325-linuxcnc",
    "postprocessor_id": "linuxcnc-validation-1.0.0",
    "include_step": False,
}


def _hash(context: ProductionEngineContext) -> str:
    return generation_context_hash(
        design_context_hash="d" * 64,
        design_version_id="11111111-1111-1111-1111-111111111111",
        revision=4,
        request=REQUEST,
        production_engine_context=context,
    )


def test_every_frozen_engine_context_field_changes_identity() -> None:
    first = resolve_production_components(
        machine_profile_id=REQUEST["machine_profile_id"],
        postprocessor_id=REQUEST["postprocessor_id"],
        app_version=APP_VERSION,
    ).context
    second = resolve_production_components(
        machine_profile_id=REQUEST["machine_profile_id"],
        postprocessor_id=REQUEST["postprocessor_id"],
        app_version=APP_VERSION,
    ).context

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert _hash(first) == _hash(second)

    for field in fields(first):
        current = getattr(first, field.name)
        mutated = replace(first, **{field.name: f"{current}-changed"})
        assert mutated.fingerprint != first.fingerprint, field.name
        assert _hash(mutated) != _hash(first), field.name


def test_profile_and_tool_data_drift_change_generation_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_machine = production_context.linuxcnc_reference_router_1325()
    original = resolve_production_components(
        machine_profile_id=REQUEST["machine_profile_id"],
        postprocessor_id=REQUEST["postprocessor_id"],
        app_version=APP_VERSION,
    ).context
    changed_tool = replace(
        original_machine.tools[0],
        version=f"{original_machine.tools[0].version}-changed",
        measured_diameter_um=4_997,
        runout_um=17,
    )
    mutations = (
        replace(original_machine, version=f"{original_machine.version}-changed"),
        replace(original_machine, work_width_um=original_machine.work_width_um + 1),
        replace(
            original_machine,
            tool_library_version=f"{original_machine.tool_library_version}-changed",
        ),
        replace(original_machine, tools=(changed_tool, *original_machine.tools[1:])),
    )

    for machine in mutations:
        monkeypatch.setattr(
            production_context,
            "linuxcnc_reference_router_1325",
            lambda machine=machine: machine,
        )
        changed = resolve_production_components(
            machine_profile_id=REQUEST["machine_profile_id"],
            postprocessor_id=REQUEST["postprocessor_id"],
            app_version=APP_VERSION,
        ).context

        assert changed.machine_profile_fingerprint != original.machine_profile_fingerprint
        assert changed.fingerprint != original.fingerprint
        assert _hash(changed) != _hash(original)


def test_unknown_machine_and_postprocessor_are_rejected_at_resolution() -> None:
    with pytest.raises(ProductionContextError, match="unknown or unverified machine profile"):
        resolve_production_components(
            machine_profile_id="untrusted-machine",
            postprocessor_id=REQUEST["postprocessor_id"],
            app_version=APP_VERSION,
        )
    with pytest.raises(ProductionContextError, match="unknown or unverified postprocessor"):
        resolve_production_components(
            machine_profile_id=REQUEST["machine_profile_id"],
            postprocessor_id="untrusted-postprocessor",
            app_version=APP_VERSION,
        )


def test_worker_cad_dependency_version_is_part_of_runtime_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def installed_version(distribution: str) -> str:
        if distribution == "reportlab":
            return REPORTLAB_DISTRIBUTION_VERSION
        if distribution == "cadquery":
            return f"{CADQUERY_DISTRIBUTION_VERSION}-drifted"
        return production_context.OPENCASCADE_DISTRIBUTION_VERSION

    monkeypatch.setattr(production_context, "distribution_version", installed_version)

    with pytest.raises(ProductionContextError, match="production dependency drift: cadquery"):
        resolve_production_components(
            machine_profile_id=REQUEST["machine_profile_id"],
            postprocessor_id=REQUEST["postprocessor_id"],
            app_version=APP_VERSION,
            require_cad_runtime=True,
        )
