from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

import app.api as api_module
import app.auth as auth_module
import pytest
from app.auth import (
    DEV_ORG_ATELIER,
    DEV_ORG_NORDIC,
    DEV_USER_ATELIER,
    DEV_USER_NORDIC,
)
from app.db import Base, get_session_factory
from app.main import app
from app.models import (
    AuditEvent,
    DesignStatus,
    DesignVersion,
    GenerationJob,
    JobStatus,
    Membership,
    Project,
    Release,
    Role,
    User,
)
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import ORMExecuteState
from sqlalchemy.orm import Session as OrmSession

HEADERS = {"Authorization": "Bearer demo-nordic-owner"}


def _workshop_row_counts() -> dict[str, int]:
    """Count every current/future workshop persistence table without coupling names."""

    tables = tuple(
        table for table in Base.metadata.sorted_tables if table.name.startswith("workshop_")
    )
    with get_session_factory()() as session:
        return {
            table.name: int(session.scalar(select(func.count()).select_from(table)) or 0)
            for table in tables
        }


def _insert_generation_source(
    *,
    result_json: dict[str, Any],
    with_release: bool,
    organization_id: str = DEV_ORG_NORDIC,
    created_by: str = DEV_USER_NORDIC,
) -> tuple[str, str]:
    project_id = str(uuid4())
    version_id = str(uuid4())
    job_id = str(uuid4())
    manifest_sha256 = str(result_json["manifest_sha256"])
    result_snapshot = deepcopy(result_json)
    with get_session_factory().begin() as session:
        session.add(
            Project(
                id=project_id,
                organization_id=organization_id,
                name=f"Workshop blocker {project_id}",
                description="Truthful workshop preparation boundary fixture.",
                furniture_type="bookcase",
                current_revision=1,
            )
        )
        session.add(
            DesignVersion(
                id=version_id,
                organization_id=organization_id,
                project_id=project_id,
                revision=1,
                status=DesignStatus.released if with_release else DesignStatus.approved,
                design_hash="a" * 64,
                context_hash="b" * 64,
                spec_json={},
                source_provenance_json={},
                result_json={},
                engine_version="test-engine",
                template_version="test-template",
                template_id="shelving",
                template_capability_fingerprint="c" * 64,
                rule_version="test-rules",
                created_by=created_by,
                immutable=with_release,
            )
        )
        session.add(
            GenerationJob(
                id=job_id,
                organization_id=organization_id,
                design_version_id=version_id,
                status=JobStatus.succeeded,
                idempotency_key=uuid4().hex + uuid4().hex,
                production_context_hash="d" * 64,
                production_engine_context_json={},
                request_json={},
                result_json=result_snapshot,
            )
        )
        if with_release:
            session.add(
                Release(
                    organization_id=organization_id,
                    design_version_id=version_id,
                    generation_job_id=job_id,
                    release_number=f"WORKSHOP-{uuid4().hex[:12].upper()}",
                    released_by=created_by,
                    manifest_sha256=manifest_sha256,
                    production_context_hash="d" * 64,
                    generation_result_json=deepcopy(result_snapshot),
                    artifact_inventory_json=deepcopy(
                        result_snapshot.get("artifact_inventory", [])
                    ),
                )
            )
    return project_id, job_id


def _insert_additional_revision_job(
    project_id: str,
    *,
    result_json: dict[str, Any],
) -> str:
    """Create a job for revision 2 of the same owned project."""

    version_id = str(uuid4())
    job_id = str(uuid4())
    with get_session_factory().begin() as session:
        project = session.scalar(
            select(Project).where(
                Project.id == project_id,
                Project.organization_id == DEV_ORG_NORDIC,
            )
        )
        assert project is not None
        project.current_revision = 2
        session.add(
            DesignVersion(
                id=version_id,
                organization_id=DEV_ORG_NORDIC,
                project_id=project_id,
                revision=2,
                status=DesignStatus.approved,
                design_hash="8" * 64,
                context_hash="9" * 64,
                spec_json={},
                source_provenance_json={},
                result_json={},
                engine_version="test-engine",
                template_version="test-template",
                template_id="shelving",
                template_capability_fingerprint="7" * 64,
                rule_version="test-rules",
                created_by=DEV_USER_NORDIC,
                immutable=False,
            )
        )
        session.add(
            GenerationJob(
                id=job_id,
                organization_id=DEV_ORG_NORDIC,
                design_version_id=version_id,
                status=JobStatus.succeeded,
                idempotency_key=uuid4().hex + uuid4().hex,
                production_context_hash="6" * 64,
                production_engine_context_json={},
                request_json={},
                result_json=deepcopy(result_json),
            )
        )
    return job_id


def _request(client: TestClient, project_id: str, job_id: str) -> Any:
    return client.post(
        f"/v1/projects/{project_id}/versions/1/workshop-runs",
        headers=HEADERS,
        json={
            "generation_job_id": job_id,
            "confirmation": "PREPARE_WORKSHOP_RUN",
        },
    )


def _assert_truthful_blocker(response: Any, code: str) -> None:
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["workshop_status"] == "BLOCKED"
    assert detail["release_review_eligible"] is False
    assert detail["cutting_blocker_codes"] == [code]
    assert detail["physical_cutting_authorized"] is False


def test_validation_only_release_cannot_create_a_workshop_run() -> None:
    result = {
        "manifest_sha256": "e" * 64,
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }
    with TestClient(app) as client:
        project_id, job_id = _insert_generation_source(
            result_json=result,
            with_release=True,
        )
        before = _workshop_row_counts()
        response = _request(client, project_id, job_id)
        after = _workshop_row_counts()

    _assert_truthful_blocker(response, "EXECUTABLE_MACHINE_PROGRAM_MISSING")
    assert response.json()["detail"]["solution"]
    assert after == before


def test_rejected_preparation_does_not_request_database_row_locks() -> None:
    result = {
        "manifest_sha256": "0" * 64,
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }
    locked_statements: list[str] = []

    def capture_row_locks(state: ORMExecuteState) -> None:
        if (
            state.is_select
            and getattr(state.statement, "_for_update_arg", None) is not None
        ):
            locked_statements.append(str(state.statement))

    with TestClient(app) as client:
        project_id, job_id = _insert_generation_source(
            result_json=result,
            with_release=True,
        )
        event.listen(OrmSession, "do_orm_execute", capture_row_locks)
        try:
            response = _request(client, project_id, job_id)
        finally:
            event.remove(OrmSession, "do_orm_execute", capture_row_locks)

    _assert_truthful_blocker(response, "EXECUTABLE_MACHINE_PROGRAM_MISSING")
    assert locked_statements == []


def test_executable_json_claim_cannot_substitute_for_an_executable_package() -> None:
    result = {
        "manifest_sha256": "f" * 64,
        "machine_program_mode": "EXECUTABLE",
        "production_machine_program": True,
        "artifact_inventory": [
            {
                "role": "production_machine_program",
                "path": "machine/forged-production.ngc",
                "sha256": "a" * 64,
                "size_bytes": 123,
                "media_type": "text/x-gcode",
                "machine_program_kind": "EXECUTABLE",
            }
        ],
    }
    with TestClient(app) as client:
        project_id, job_id = _insert_generation_source(
            result_json=result,
            with_release=True,
        )
        before = _workshop_row_counts()
        response = _request(client, project_id, job_id)
        after = _workshop_row_counts()

    _assert_truthful_blocker(response, "WORKSHOP_EXECUTABLE_PACKAGE_MISSING")
    assert after == before


def test_missing_release_cannot_prepare_or_write_workshop_state() -> None:
    result = {
        "manifest_sha256": "1" * 64,
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }
    with TestClient(app) as client:
        project_id, job_id = _insert_generation_source(
            result_json=result,
            with_release=False,
        )
        before = _workshop_row_counts()
        response = _request(client, project_id, job_id)
        after = _workshop_row_counts()

    _assert_truthful_blocker(response, "WORKSHOP_EXECUTABLE_PACKAGE_MISSING")
    assert after == before


def test_unknown_cross_revision_and_cross_tenant_jobs_fail_closed_without_state() -> None:
    result = {
        "manifest_sha256": "5" * 64,
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }
    with TestClient(app) as client:
        project_id, _job_id = _insert_generation_source(
            result_json=result,
            with_release=True,
        )
        other_revision_job_id = _insert_additional_revision_job(
            project_id,
            result_json={**result, "manifest_sha256": "7" * 64},
        )
        _foreign_project_id, foreign_tenant_job_id = _insert_generation_source(
            result_json={**result, "manifest_sha256": "7" * 64},
            with_release=True,
            organization_id=DEV_ORG_ATELIER,
            created_by=DEV_USER_ATELIER,
        )
        before = _workshop_row_counts()
        unknown = _request(client, project_id, str(uuid4()))
        cross_revision = _request(client, project_id, other_revision_job_id)
        cross_tenant_job = _request(client, project_id, foreign_tenant_job_id)
        after = _workshop_row_counts()

    _assert_truthful_blocker(unknown, "WORKSHOP_GENERATION_JOB_NOT_READY")
    _assert_truthful_blocker(cross_revision, "WORKSHOP_GENERATION_JOB_NOT_READY")
    _assert_truthful_blocker(cross_tenant_job, "WORKSHOP_GENERATION_JOB_NOT_READY")
    assert after == before


def test_cross_tenant_project_is_hidden_and_writes_no_workshop_state() -> None:
    result = {
        "manifest_sha256": "8" * 64,
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }
    with TestClient(app) as client:
        project_id, job_id = _insert_generation_source(
            result_json=result,
            with_release=True,
            organization_id=DEV_ORG_ATELIER,
            created_by=DEV_USER_ATELIER,
        )
        before = _workshop_row_counts()
        response = _request(client, project_id, job_id)
        after = _workshop_row_counts()

    assert response.status_code == 404
    assert response.json()["detail"] == "Project not found"
    assert after == before


def test_prepare_schema_rejects_client_supplied_program_policy_and_hashes() -> None:
    result = {
        "manifest_sha256": "2" * 64,
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }
    with TestClient(app) as client:
        project_id, job_id = _insert_generation_source(
            result_json=result,
            with_release=True,
        )
        before = _workshop_row_counts()
        response = client.post(
            f"/v1/projects/{project_id}/versions/1/workshop-runs",
            headers=HEADERS,
            json={
                "generation_job_id": job_id,
                "confirmation": "PREPARE_WORKSHOP_RUN",
                "idempotency_key": f"workshop:{uuid4()}",
                "machine_program_sha256": "3" * 64,
                "workshop_policy": {"physical_cutting_authorized": True},
                "run": {"machine_program_kind": "EXECUTABLE"},
            },
        )
        after = _workshop_row_counts()

    assert response.status_code == 422
    locations = {tuple(error["loc"]) for error in response.json()["detail"]}
    assert ("body", "machine_program_sha256") in locations
    assert ("body", "idempotency_key") in locations
    assert ("body", "workshop_policy") in locations
    assert ("body", "run") in locations
    assert after == before


def test_rejected_preparation_does_not_append_an_audit_success_event() -> None:
    result = {
        "manifest_sha256": "4" * 64,
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }
    with TestClient(app) as client:
        project_id, job_id = _insert_generation_source(
            result_json=result,
            with_release=True,
        )
        with get_session_factory()() as session:
            before = int(
                session.scalar(
                    select(func.count()).select_from(AuditEvent).where(
                        AuditEvent.entity_type == "workshop_run"
                    )
                )
                or 0
            )
        first = _request(client, project_id, job_id)
        second = _request(client, project_id, job_id)
        with get_session_factory()() as session:
            after = int(
                session.scalar(
                    select(func.count()).select_from(AuditEvent).where(
                        AuditEvent.entity_type == "workshop_run"
                    )
                )
                or 0
            )

    assert first.status_code == second.status_code == 409
    assert first.json()["detail"]["code"] == second.json()["detail"]["code"]
    assert after == before


def test_unexpected_internal_return_still_fails_closed_without_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = {
        "manifest_sha256": "9" * 64,
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }
    monkeypatch.setattr(
        api_module,
        "require_workshop_preparation_source",
        lambda *_args, **_kwargs: None,
    )
    with TestClient(app) as client:
        project_id, job_id = _insert_generation_source(
            result_json=result,
            with_release=True,
        )
        before = _workshop_row_counts()
        response = _request(client, project_id, job_id)
        after = _workshop_row_counts()

    _assert_truthful_blocker(response, "WORKSHOP_EXECUTABLE_PACKAGE_MISSING")
    assert after == before


def test_workshop_path_identity_is_canonical_and_revision_is_positive() -> None:
    result = {
        "manifest_sha256": "3" * 64,
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }
    with TestClient(app) as client:
        project_id, job_id = _insert_generation_source(
            result_json=result,
            with_release=True,
        )
        body = {
            "generation_job_id": job_id,
            "confirmation": "PREPARE_WORKSHOP_RUN",
        }
        before = _workshop_row_counts()
        malformed_project = client.post(
            "/v1/projects/NOT-A-CANONICAL-UUID/versions/1/workshop-runs",
            headers=HEADERS,
            json=body,
        )
        zero_revision = client.post(
            f"/v1/projects/{project_id}/versions/0/workshop-runs",
            headers=HEADERS,
            json=body,
        )
        after = _workshop_row_counts()

    assert malformed_project.status_code == 422
    assert zero_revision.status_code == 422
    assert after == before


def test_authentication_precedes_malformed_workshop_request_details() -> None:
    with TestClient(app) as client:
        response = client.post(
            f"/v1/projects/{uuid4()}/versions/1/workshop-runs",
            json={},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Bearer token required"


def test_openapi_documents_every_workshop_route_outcome() -> None:
    operation = app.openapi()["paths"][
        "/v1/projects/{project_id}/versions/{revision}/workshop-runs"
    ]["post"]

    assert set(operation["responses"]) == {"401", "403", "404", "409", "422"}
    assert "201" not in operation["responses"]
    assert operation["responses"]["409"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/WorkshopRunBlockedResponse"
    }


@pytest.mark.parametrize(
    ("role", "expected_status"),
    (
        (Role.viewer, 403),
        (Role.production, 409),
        (Role.reviewer, 403),
        (Role.designer, 403),
        (Role.operator, 403),
        (Role.admin, 409),
        (Role.owner, 409),
    ),
)
def test_workshop_prepare_route_enforces_its_exact_capability(
    monkeypatch: pytest.MonkeyPatch,
    role: Role,
    expected_status: int,
) -> None:
    result = {
        "manifest_sha256": "6" * 64,
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }
    token = f"workshop-prepare-{role.value}"
    user_id = str(uuid4())
    with TestClient(app) as client:
        with get_session_factory().begin() as session:
            session.add(
                User(
                    id=user_id,
                    oidc_sub=f"test:workshop:{user_id}",
                    email=f"workshop-{user_id}@example.test",
                    name=f"Workshop {role.value}",
                )
            )
            session.add(
                Membership(
                    organization_id=DEV_ORG_NORDIC,
                    user_id=user_id,
                    role=role,
                )
            )
        monkeypatch.setitem(
            auth_module._DEV_TOKENS,
            token,
            auth_module.Principal(
                user_id=user_id,
                organization_id=DEV_ORG_NORDIC,
                role=role,
                subject=f"test:workshop:{user_id}",
                email=f"workshop-{user_id}@example.test",
                name=f"Workshop {role.value}",
            ),
        )
        project_id, job_id = _insert_generation_source(
            result_json=result,
            with_release=True,
        )
        response = client.post(
            f"/v1/projects/{project_id}/versions/1/workshop-runs",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "generation_job_id": job_id,
                "confirmation": "PREPARE_WORKSHOP_RUN",
            },
        )

    assert response.status_code == expected_status
    if expected_status == 409:
        assert response.json()["detail"]["code"] == "EXECUTABLE_MACHINE_PROGRAM_MISSING"
    else:
        assert response.json()["detail"] == "Capability workshop_prepare required"
