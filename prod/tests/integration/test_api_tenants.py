from __future__ import annotations

from app.main import app
from app.seed import NORDIC_DEMO_PROJECT
from fastapi.testclient import TestClient

NORDIC = {"Authorization": "Bearer demo-nordic-owner"}
ATELIER = {"Authorization": "Bearer demo-atelier-owner"}


def test_projects_and_versions_are_tenant_isolated() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/v1/projects",
            headers=NORDIC,
            json={"name": "Isolation fixture"},
        )
        assert created.status_code == 201
        project_id = created.json()["id"]

        assert client.get(f"/v1/projects/{project_id}", headers=ATELIER).status_code == 404
        assert all(
            project["id"] != project_id
            for project in client.get("/v1/projects", headers=ATELIER).json()
        )


def test_same_project_name_is_allowed_in_separate_tenants() -> None:
    with TestClient(app) as client:
        for headers in (NORDIC, ATELIER):
            response = client.post(
                "/v1/projects",
                headers=headers,
                json={"name": "Tenant-local name"},
            )
            assert response.status_code == 201


def test_development_token_is_required() -> None:
    with TestClient(app) as client:
        assert client.get("/v1/projects").status_code == 401
        assert (
            client.get("/v1/projects", headers={"Authorization": "Bearer unknown"}).status_code
            == 401
        )


def test_seeded_bookcase_project_is_real_and_tenant_isolated() -> None:
    with TestClient(app) as client:
        project = client.get(f"/v1/projects/{NORDIC_DEMO_PROJECT}", headers=NORDIC)
        assert project.status_code == 200
        assert project.json()["current_revision"] == 1
        versions = client.get(
            f"/v1/projects/{NORDIC_DEMO_PROJECT}/versions", headers=NORDIC
        )
        assert versions.status_code == 200
        assert versions.json()[0]["result_json"]["parts"]
        assert (
            client.get(f"/v1/projects/{NORDIC_DEMO_PROJECT}", headers=ATELIER).status_code
            == 404
        )
