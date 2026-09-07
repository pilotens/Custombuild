from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from contextlib import contextmanager
from uuid import uuid4

import app.furniture_api as furniture_api
import pytest
from app.db import get_session_factory
from app.main import app
from app.models import OutboxEvent
from custombuild_domain.furniture import FurnitureWorkspace
from custombuild_domain.identity import content_hash
from custombuild_worker import tasks
from custombuild_worker.furniture_review import build_furniture_review
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.integration.test_api_design_flow import valid_spec, valid_workspace_intent
from tests.unit.test_furniture_families import workspace

HEADERS = {"Authorization": "Bearer demo-nordic-owner"}
OTHER = {"Authorization": "Bearer demo-atelier-owner"}


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def save(client, family="table"):
    project = client.post(
        "/v1/projects", headers=HEADERS, json={"name": f"Furniture-{uuid4()}"}
    ).json()
    response = client.put(
        f"/v1/furniture/projects/{project['id']}/draft",
        headers=HEADERS,
        json={"expected_revision": 0, "workspace": workspace(family).model_dump(mode="json")},
    )
    assert response.status_code == 200, response.text
    return project, response.json()


def test_families_are_available_and_preview_requires_a_valid_session(client):
    assert client.get("/v1/furniture/catalog").status_code == 401
    catalog = client.get("/v1/furniture/catalog", headers=HEADERS).json()
    assert {f["id"] for f in catalog["families"]} == {"shelving", "table", "chest_of_drawers"}
    for family in ("shelving", "table", "chest_of_drawers"):
        response = client.post(
            "/v1/furniture/preview", headers=HEADERS, json=workspace(family).model_dump(mode="json")
        )
        assert response.status_code == 200, response.text
        assert not response.json()["physical_cutting_authorized"]


def test_profile_change_save_reload_and_history_preserve_old_revision(client):
    project, first = save(client, "chest_of_drawers")
    path = f"/v1/furniture/projects/{project['id']}"
    changed = json.loads(json.dumps(first["workspace"]))
    changed["design"]["hardware"]["catalog_id"] = "drawer-side-mount-400-layout"
    compare = client.post(
        "/v1/furniture/profile-change",
        headers=HEADERS,
        json={"current": first["workspace"], "proposed": changed},
    )
    assert compare.status_code == 200, compare.text
    assert compare.json()["intent_preserved"]
    assert "cam" in compare.json()["invalidated_reviews"]
    second = client.put(
        f"{path}/draft", headers=HEADERS, json={"expected_revision": 1, "workspace": changed}
    )
    assert second.status_code == 200, second.text
    assert second.json()["revision"] == 2
    assert second.json()["workspace"]["design"]["revision"] == 2
    assert client.get(f"{path}/draft", headers=HEADERS).json() == second.json()
    assert (
        client.put(
            f"{path}/draft", headers=HEADERS, json={"expected_revision": 1, "workspace": changed}
        ).status_code
        == 409
    )
    history = client.get(f"{path}/history", headers=HEADERS).json()["items"]
    assert [h["revision"] for h in history] == [2, 1]
    assert history[1]["workspace"] == first["workspace"]
    assert client.get(f"{path}/history", headers=OTHER).status_code == 404
    assert client.get(f"{path}/draft", headers=OTHER).status_code == 404


def test_furniture_drafts_do_not_unlock_the_existing_production_revision_gate(client):
    project, first = save(client)
    response = client.post(
        f"/v1/projects/{project['id']}/versions",
        headers=HEADERS,
        json={
            "template_id": "table",
            "spec": first["workspace"]["design"],
        },
    )
    assert response.status_code in {400, 409, 422}
    replacement = client.put(
        f"/v1/projects/{project['id']}/draft",
        headers=HEADERS,
        json={
            "expected_draft_revision": 1,
            "template_id": "shelving",
            "spec": valid_spec(),
            "workspace_spec": valid_workspace_intent(),
        },
    )
    assert replacement.status_code == 409, replacement.text
    current = client.get(f"/v1/furniture/projects/{project['id']}/draft", headers=HEADERS)
    assert current.json()["workspace"] == first["workspace"]


def test_export_uses_transactional_outbox_and_checks_snapshot_and_tenant(client, monkeypatch):
    project, draft = save(client)
    path = f"/v1/furniture/projects/{project['id']}/exports"
    request = {
        "expected_revision": draft["revision"],
        "expected_design_hash": draft["preview"]["design"]["design_hash"],
    }
    assert (
        client.post(path, headers=HEADERS, json={**request, "expected_revision": 99}).status_code
        == 409
    )
    response = client.post(path, headers=HEADERS, json=request)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert client.post(path, headers=HEADERS, json=request).json()["job_id"] == job_id
    with get_session_factory()() as session:
        event = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_key == f"furniture-review:{job_id}")
        )
        assert event is not None
        published = []
        monkeypatch.setattr(tasks.celery_app, "send_task", lambda *a, **k: published.append((a, k)))
        assert tasks._dispatch_tenant_outbox_events([event], event.organization_id) == 1
        assert published[0][0] == ("custombuild.generate_furniture_review",)
        assert published[0][1]["task_id"] == job_id
    assert client.get(f"{path}/{job_id}", headers=OTHER).status_code == 404
    # Only bound bytes can cross the API result boundary, regardless of Redis state.
    content = b"review-result-fixture"
    result = {
        "design_hash": request["expected_design_hash"],
        "revision": draft["revision"],
        "workspace_sha256": content_hash(draft["workspace"]),
        "content_base64": base64.b64encode(content).decode(),
        "sha256": hashlib.sha256(content).hexdigest(),
    }

    class ResultClient:
        def AsyncResult(self, task_id):
            assert task_id == job_id
            return type("Result", (), {"state": "SUCCESS", "result": result})()

    monkeypatch.setattr(furniture_api, "review_result_client", ResultClient)
    downloaded = client.get(f"{path}/{job_id}", headers=HEADERS)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.json()["state"] == "succeeded"
    result["workspace_sha256"] = "0" * 64
    assert client.get(f"{path}/{job_id}", headers=HEADERS).status_code == 409


@pytest.mark.cad
@pytest.mark.parametrize("family", ["table", "chest_of_drawers", "shelving"])
def test_real_family_review_exports_match_parts_and_contain_no_machine_programs(family):
    pytest.importorskip("cadquery")
    draft = workspace(family)
    raw = build_furniture_review(draft)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        resolved = json.loads(archive.read("design/resolved.json"))
        assert not manifest["physical_cutting_authorized"]
        assert not any(n.endswith((".ngc", ".nc", ".gcode")) for n in names)
        assert archive.read("design/model.step").startswith(b"ISO-10303-21;")
        assert archive.read("design/model.glb")[:4] == b"glTF"
        assert archive.read("documents/part-drawings.pdf").startswith(b"%PDF-")
        for part in resolved["parts"]:
            for side in ("A", "B"):
                path = f"parts/{part['part_id']}/{side}.dxf"
                assert path in names
                assert b"$INSUNITS" in archive.read(path)
        for file in manifest["files"]:
            assert hashlib.sha256(archive.read(file["path"])).hexdigest() == file["sha256"]


def test_worker_review_loads_only_the_committed_tenant_snapshot(client, monkeypatch):
    project, draft = save(client)
    response = client.post(
        f"/v1/furniture/projects/{project['id']}/exports",
        headers=HEADERS,
        json={
            "expected_revision": 1,
            "expected_design_hash": draft["preview"]["design"]["design_hash"],
        },
    )
    job_id = response.json()["job_id"]

    @contextmanager
    def transaction(_tenant):
        with get_session_factory()() as session:
            yield session

    monkeypatch.setattr(tasks, "_tenant_transaction", transaction)
    import custombuild_worker.furniture_review as review

    monkeypatch.setattr(review, "build_furniture_review", lambda document: b"review-fixture")
    result = tasks.generate_furniture_review.run(job_id, "11111111-1111-4111-8111-111111111111")
    assert result["workspace_sha256"] == content_hash(
        FurnitureWorkspace.model_validate(draft["workspace"])
    )
    with pytest.raises(ValueError, match="not found"):
        tasks.generate_furniture_review.run(job_id, "22222222-2222-4222-8222-222222222222")
