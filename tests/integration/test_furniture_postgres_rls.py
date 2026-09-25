from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from app.models import Artifact, GenerationJob, Organization, Project, StoredObject
from sqlalchemy import Connection, create_engine, insert, select, text, update
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres


@dataclass(frozen=True)
class FurnitureOwnerGraph:
    connection: Connection
    organization_id: str
    other_organization_id: str
    project_id: str
    sibling_project_id: str
    other_project_id: str
    job_id: str
    other_job_id: str


def _insert_job(
    connection: Connection,
    *,
    organization_id: str,
    project_id: str | None,
    job_id: str,
    job_type: str = "furniture_review",
    design_version_id: str | None = None,
) -> None:
    connection.execute(
        insert(GenerationJob).values(
            id=job_id,
            organization_id=organization_id,
            project_id=project_id,
            design_version_id=design_version_id,
            job_type=job_type,
            status="succeeded",
            idempotency_key=job_id,
            production_context_hash="c" * 64,
            production_engine_context_json={},
            request_json={},
        )
    )


@pytest.fixture
def furniture_owner_graph() -> Iterator[FurnitureOwnerGraph]:
    database_url = os.getenv("TENANT_GRAPH_DATABASE_URL")
    if not database_url:
        pytest.skip("Furniture owner-graph probe requires TENANT_GRAPH_DATABASE_URL")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection, connection.begin() as transaction:
            # Keep the complete fixture uncommitted: rollback also removes ledger
            # identities without mutating global counters or retained artifacts.
            assert connection.execute(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            ).scalar_one(), "TENANT_GRAPH_DATABASE_URL must use the bootstrap superuser"
            graph = FurnitureOwnerGraph(connection, *(str(uuid4()) for _ in range(7)))
            for organization_id in (graph.organization_id, graph.other_organization_id):
                connection.execute(
                    insert(Organization).values(
                        id=organization_id,
                        name="Furniture owner graph",
                        slug=f"furniture-owner-{organization_id}",
                    )
                )
            for project_id, organization_id in (
                (graph.project_id, graph.organization_id),
                (graph.sibling_project_id, graph.organization_id),
                (graph.other_project_id, graph.other_organization_id),
            ):
                connection.execute(
                    insert(Project).values(
                        id=project_id, organization_id=organization_id, name=project_id
                    )
                )
            for job_id, project_id, organization_id in (
                (graph.job_id, graph.project_id, graph.organization_id),
                (graph.other_job_id, graph.other_project_id, graph.other_organization_id),
            ):
                _insert_job(
                    connection,
                    organization_id=organization_id,
                    project_id=project_id,
                    job_id=job_id,
                )
            try:
                yield graph
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.parametrize("role", ("custombuild_api", "custombuild_worker"))
def test_furniture_jobs_remain_tenant_scoped(
    furniture_owner_graph: FurnitureOwnerGraph, role: str
) -> None:
    graph = furniture_owner_graph
    connection = graph.connection
    connection.execute(
        text("SET LOCAL ROLE custombuild_api")
        if role == "custombuild_api"
        else text("SET LOCAL ROLE custombuild_worker")
    )
    assert connection.execute(
        text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).one() == (False, False)
    connection.execute(
        text("SELECT set_config('app.current_organization_id', :tenant, true)"),
        {"tenant": graph.organization_id},
    )
    assert connection.execute(
        select(GenerationJob.id).where(GenerationJob.id.in_([graph.job_id, graph.other_job_id]))
    ).scalars().all() == [graph.job_id]
    with pytest.raises(IntegrityError) as caught, connection.begin_nested():
        connection.execute(
            update(GenerationJob)
            .where(GenerationJob.id == graph.job_id)
            .values(project_id=graph.other_project_id)
        )
    assert caught.value.orig.diag.constraint_name == "fk_generation_jobs_org_project"


@pytest.mark.parametrize(
    ("job_type", "with_project", "with_version"),
    (
        ("furniture_review", False, False),
        ("furniture_review", True, True),
        ("production", True, False),
        ("production", False, False),
    ),
)
def test_furniture_and_production_jobs_require_disjoint_owners(
    furniture_owner_graph: FurnitureOwnerGraph,
    job_type: str,
    with_project: bool,
    with_version: bool,
) -> None:
    graph = furniture_owner_graph
    with pytest.raises(IntegrityError) as caught, graph.connection.begin_nested():
        _insert_job(
            graph.connection,
            organization_id=graph.organization_id,
            project_id=graph.project_id if with_project else None,
            job_id=str(uuid4()),
            job_type=job_type,
            design_version_id=str(uuid4()) if with_version else None,
        )
    assert caught.value.orig.diag.constraint_name == "ck_generation_jobs_type_owner"


@pytest.mark.parametrize(
    "mismatch", (None, "project_id", "owner_type", "owner_id", "idempotency_key", "sha256")
)
def test_furniture_artifact_requires_exact_committed_owner_binding(
    furniture_owner_graph: FurnitureOwnerGraph, mismatch: str | None
) -> None:
    graph = furniture_owner_graph
    connection = graph.connection
    artifact_id = str(uuid4())
    object_key = f"{graph.organization_id}/furniture-review/{artifact_id}.zip"
    ledger_identity = {
        "project_id": graph.project_id,
        "owner_type": "generation_job",
        "owner_id": graph.job_id,
        "idempotency_key": f"generation:{graph.job_id}:furniture_review_bundle:{artifact_id}",
        "sha256": "a" * 64,
    }
    if mismatch:
        ledger_identity[mismatch] = {
            "project_id": graph.sibling_project_id,
            "owner_type": "imported_asset",
            "owner_id": str(uuid4()),
            "idempotency_key": f"wrong:{artifact_id}",
            "sha256": "b" * 64,
        }[mismatch]
    # Bootstrap creates only this test's ledger fixture; the actual artifact
    # insert and deferred domain-reference trigger run under the worker role.
    connection.execute(
        insert(StoredObject).values(
            organization_id=graph.organization_id,
            object_key=object_key,
            size_bytes=12,
            media_type="application/zip",
            state="committed",
            **ledger_identity,
        )
    )
    connection.execute(text("SET LOCAL ROLE custombuild_worker"))
    connection.execute(
        text("SELECT set_config('app.current_organization_id', :tenant, true)"),
        {"tenant": graph.organization_id},
    )

    def insert_artifact() -> None:
        connection.execute(
            insert(Artifact).values(
                id=artifact_id,
                organization_id=graph.organization_id,
                generation_job_id=graph.job_id,
                kind="furniture_review_bundle",
                object_key=object_key,
                sha256="a" * 64,
                size_bytes=12,
                content_type="application/zip",
            )
        )
        # Flush the production deferred constraint without committing fixtures.
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    if mismatch is None:
        insert_artifact()
        assert (
            connection.scalar(select(Artifact.id).where(Artifact.id == artifact_id)) == artifact_id
        )
    else:
        with pytest.raises(IntegrityError) as caught, connection.begin_nested():
            insert_artifact()
        assert "STORAGE_DOMAIN_REFERENCE_INVALID" in str(caught.value.orig)
        assert connection.scalar(select(Artifact.id).where(Artifact.id == artifact_id)) is None
