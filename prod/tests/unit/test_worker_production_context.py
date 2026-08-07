from __future__ import annotations

from typing import Any

import custombuild_worker.tasks as worker_tasks
import pytest
from app import __version__ as APP_VERSION
from app.models import DesignStatus, DesignVersion, GenerationJob, JobStatus
from custombuild_domain import BOOKCASE_ENGINE_VERSION, BOOKCASE_TEMPLATE_VERSION
from custombuild_manufacturing import ProductionBlockedError
from custombuild_manufacturing.production_context import (
    generation_context_hash,
    resolve_production_components,
)
from custombuild_rules import RULES_VERSION


def _job_and_version() -> tuple[GenerationJob, DesignVersion]:
    version = DesignVersion(
        id="11111111-1111-1111-1111-111111111111",
        organization_id="22222222-2222-2222-2222-222222222222",
        project_id="33333333-3333-3333-3333-333333333333",
        revision=2,
        status=DesignStatus.design_validated,
        design_hash="d" * 64,
        context_hash="c" * 64,
        spec_json={},
        result_json={},
        engine_version=BOOKCASE_ENGINE_VERSION,
        template_version=f"bookcase@{BOOKCASE_TEMPLATE_VERSION}",
        rule_version=f"bookcase-rules@{RULES_VERSION}",
        created_by="44444444-4444-4444-4444-444444444444",
        immutable=False,
    )
    request: dict[str, Any] = {
        "machine_profile_id": "custombuild-router-1325-linuxcnc",
        "postprocessor_id": "linuxcnc-validation-1.0.0",
        "include_step": False,
    }
    resolved = resolve_production_components(
        machine_profile_id=request["machine_profile_id"],
        postprocessor_id=request["postprocessor_id"],
        app_version=APP_VERSION,
    )
    context_hash = generation_context_hash(
        design_context_hash=version.context_hash,
        design_version_id=version.id,
        revision=version.revision,
        request=request,
        production_engine_context=resolved.context,
    )
    job = GenerationJob(
        id="55555555-5555-5555-5555-555555555555",
        organization_id=version.organization_id,
        design_version_id=version.id,
        status=JobStatus.queued,
        idempotency_key="e" * 64,
        production_context_hash=context_hash,
        production_engine_context_json=resolved.context.as_dict(),
        request_json=request,
        attempts=0,
    )
    return job, version


def test_worker_accepts_only_the_exact_frozen_context() -> None:
    job, version = _job_and_version()

    resolved = worker_tasks._resolve_current_job_context(job, version)

    assert resolved.context.as_dict() == job.production_engine_context_json
    assert resolved.context.fingerprint


@pytest.mark.parametrize("drift", ("engine-context", "generation-hash"))
def test_worker_blocks_persisted_context_drift(drift: str) -> None:
    job, version = _job_and_version()
    if drift == "engine-context":
        job.production_engine_context_json = {
            **job.production_engine_context_json,
            "operations_engine_version": "drifted-operations-engine",
        }
    else:
        job.production_context_hash = "0" * 64

    with pytest.raises(ProductionBlockedError, match="production engine context drift"):
        worker_tasks._resolve_current_job_context(job, version)


@pytest.mark.parametrize(
    "field_name",
    ("engine_version", "template_version", "rule_version"),
)
def test_worker_blocks_stale_frozen_design_libraries(field_name: str) -> None:
    job, version = _job_and_version()
    setattr(version, field_name, f"{getattr(version, field_name)}-stale")

    with pytest.raises(ProductionBlockedError, match="production engine context drift"):
        worker_tasks._resolve_current_job_context(job, version)


def test_worker_blocks_library_drift_before_build_or_object_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, version = _job_and_version()
    job.production_engine_context_json = {
        **job.production_engine_context_json,
        "tool_library_fingerprint": "0" * 64,
    }
    writes: list[str] = []

    def record_write(key: str, data: bytes, content_type: str) -> None:
        writes.append(key)

    monkeypatch.setattr(worker_tasks, "_put_object", record_write)
    with pytest.raises(ProductionBlockedError, match="production engine context drift"):
        worker_tasks._generate(job, version)

    assert writes == []
