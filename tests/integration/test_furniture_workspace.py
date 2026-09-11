from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import app.furniture_api as furniture_api
import pytest
from app.db import get_session_factory
from app.main import app
from app.models import DesignVersion, GenerationJob, JobStatus, OutboxEvent
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


def test_customer_measurements_survive_save_reload_history_and_cannot_skip_dimension_resolution(
    client,
):
    from pathlib import Path

    project, previous = save(client, "shelving")
    selected = json.loads(Path("examples/furniture/bookcase-4340x2540x280.json").read_text())
    path = f"/v1/furniture/projects/{project['id']}"
    saved = client.put(
        f"{path}/draft",
        headers=HEADERS,
        json={"expected_revision": previous["revision"], "workspace": selected},
    )
    assert saved.status_code == 200, saved.text
    draft = saved.json()
    assert draft["workspace"]["design"]["installation"] == selected["design"]["installation"]
    assert client.get(f"{path}/draft", headers=HEADERS).json() == draft
    history = client.get(f"{path}/history", headers=HEADERS).json()
    assert history["items"][0]["workspace"] == draft["workspace"]
    blocked = client.post(
        f"{path}/production-preview",
        headers=HEADERS,
        json={
            "expected_revision": draft["revision"],
            "expected_design_hash": draft["preview"]["design"]["design_hash"],
        },
    )
    assert blocked.status_code == 422, blocked.text
    assert "list" in blocked.text


def production_bridge(client, project, draft):
    response = client.post(
        f"/v1/furniture/projects/{project['id']}/production-preview",
        headers=HEADERS,
        json={
            "expected_revision": draft["revision"],
            "expected_design_hash": draft["preview"]["design"]["design_hash"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def production_request(bridge):
    from app.furniture_production import furniture_production_input

    from tests.integration.test_api_design_flow import valid_production_context

    source = bridge["source_furniture"]
    return {
        "template_id": "shelving",
        "spec": furniture_production_input(
            FurnitureWorkspace.model_validate(source["workspace"])
        ).model_dump(mode="json"),
        "source_furniture": source,
        "production_context": valid_production_context(),
        "expected_design_hash": bridge["preview"]["design_hash"],
        "expected_current_revision": 0,
    }


def test_shelving_enters_existing_production_chain_without_changing_saved_furniture(client):
    project, draft = save(client, "shelving")
    bridge = production_bridge(client, project, draft)
    assert bridge["source_furniture"]["workspace"] == draft["workspace"]
    assert "retention_certification_request" in bridge["preview"]
    assert not bridge["physical_cutting_authorized"]
    body = production_request(bridge)
    path = f"/v1/projects/{project['id']}/versions"
    response = client.post(path, headers=HEADERS, json=body)
    assert response.status_code == 201, response.text
    version = response.json()
    assert version["result_json"]["source_furniture"] == bridge["source_furniture"]
    assert version["spec_json"] == bridge["preview"]["spec"]
    assert client.post(path, headers=HEADERS, json=body).json()["id"] == version["id"]
    assert (
        client.get(f"/v1/furniture/projects/{project['id']}/draft", headers=HEADERS).json() == draft
    )
    validated = client.post(f"{path}/1/validate", headers=HEADERS)
    assert validated.status_code == 200
    assert validated.json()["status"] == "design_validated"
    # Design screening cannot qualify an unresolved joint for machine use.
    blocked = client.post(
        f"{path}/1/approve",
        headers=HEADERS,
        json={"approval_type": "cam", "reason": "Test verifies the real unresolved joint gate"},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "DADO_RETENTION_EVIDENCE_MISSING"
    assert not version["immutable"]


def test_unchanged_furniture_save_preserves_revision_production_and_active_jobs(client):
    project, draft = save(client, "shelving")
    bridge = production_bridge(client, project, draft)
    version_path = f"/v1/projects/{project['id']}/versions"
    created = client.post(version_path, headers=HEADERS, json=production_request(bridge))
    assert created.status_code == 201, created.text
    version = client.get(f"{version_path}/1", headers=HEADERS).json()
    path = f"/v1/furniture/projects/{project['id']}"
    history = client.get(f"{path}/history", headers=HEADERS).json()
    jobs = {}
    with get_session_factory()() as session:
        stored_version = session.get(DesignVersion, version["id"])
        assert stored_version is not None
        for status in (JobStatus.queued, JobStatus.running):
            job = GenerationJob(
                id=str(uuid4()),
                organization_id=stored_version.organization_id,
                design_version_id=version["id"],
                status=status,
                idempotency_key=uuid4().hex,
                production_context_hash=version["context_hash"],
                production_engine_context_json={},
                request_json={},
                lease_token=str(uuid4()) if status == JobStatus.running else None,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            session.add(job)
            jobs[status] = (job.id, job.lease_token)
        session.commit()

    # Local identity and default omission are not furniture changes. They must
    # not supersede a workshop's preparation or cancel its generation work.
    local_identity = json.loads(json.dumps(draft["workspace"]))
    local_identity["design"].update(design_id="furniture", revision=99)
    explicit_defaults = json.loads(json.dumps(draft["workspace"]))
    explicit_defaults["design"]["intent"].update(shelf_load_basis="per_row", plinth_height_um=0)
    for unchanged in (draft["workspace"], local_identity, explicit_defaults):
        response = client.put(
            f"{path}/draft",
            headers=HEADERS,
            json={"expected_revision": draft["revision"], "workspace": unchanged},
        )
        assert response.status_code == 200, response.text
        assert response.json() == draft
    assert client.get(f"{path}/history", headers=HEADERS).json() == history
    assert client.get(f"{version_path}/1", headers=HEADERS).json() == version
    reopened = production_bridge(client, project, draft)
    assert reopened["source_furniture"] == bridge["source_furniture"]
    assert reopened["preview"]["design_hash"] == bridge["preview"]["design_hash"]
    with get_session_factory()() as session:
        for status, (job_id, lease_token) in jobs.items():
            job = session.get(GenerationJob, job_id)
            assert job is not None
            assert job.status == status
            assert job.lease_token == lease_token
            assert job.lease_expires_at is not None
            assert job.finished_at is None
    stale = client.put(
        f"{path}/draft",
        headers=HEADERS,
        json={"expected_revision": 0, "workspace": draft["workspace"]},
    )
    assert stale.status_code == 409, stale.text


def test_unchanged_inputs_still_create_revision_when_the_assessment_changes(client, monkeypatch):
    project, draft = save(client, "shelving")
    bridge = production_bridge(client, project, draft)
    version_path = f"/v1/projects/{project['id']}/versions"
    created = client.post(version_path, headers=HEADERS, json=production_request(bridge))
    assert created.status_code == 201, created.text
    original_preview = furniture_api.preview_furniture

    def changed_assessment(workspace):
        result = original_preview(workspace)
        result["rules"]["rules_version"] = "furniture-rules-updated"
        return result

    monkeypatch.setattr(furniture_api, "preview_furniture", changed_assessment)
    response = client.put(
        f"/v1/furniture/projects/{project['id']}/draft",
        headers=HEADERS,
        json={"expected_revision": draft["revision"], "workspace": draft["workspace"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 2
    assert (
        response.json()["preview"]["design"]["design_hash"]
        == draft["preview"]["design"]["design_hash"]
    )
    assert response.json()["preview"]["rules"]["rules_version"] == "furniture-rules-updated"
    assert client.get(f"{version_path}/1", headers=HEADERS).json()["status"] == "superseded"


@pytest.mark.parametrize("change", ["omit", "thickness", "load", "template"])
def test_production_cannot_replace_the_saved_furniture_design(client, change):
    project, draft = save(client, "shelving")
    bridge = production_bridge(client, project, draft)
    body = production_request(bridge)
    if change == "omit":
        body.pop("source_furniture")
    elif change == "thickness":
        body["spec"]["measured_thickness_mm"] = 17.8
    elif change == "load":
        body["spec"]["load_per_shelf_kg"] = 1
    else:
        body["template_id"] = "table"
    response = client.post(f"/v1/projects/{project['id']}/versions", headers=HEADERS, json=body)
    assert response.status_code == 409, response.text


def test_stale_batch_or_workshop_snapshot_requires_a_new_production_revision(client):
    project, draft = save(client, "shelving")
    bridge = production_bridge(client, project, draft)
    body = production_request(bridge)
    path = f"/v1/projects/{project['id']}/versions"
    first = client.post(path, headers=HEADERS, json=body)
    assert first.status_code == 201, first.text
    jobs = {}
    with get_session_factory()() as session:
        version = session.get(DesignVersion, first.json()["id"])
        assert version is not None
        for status in (JobStatus.queued, JobStatus.running, JobStatus.succeeded):
            job = GenerationJob(
                id=str(uuid4()),
                organization_id=version.organization_id,
                design_version_id=version.id,
                status=status,
                idempotency_key=uuid4().hex,
                production_context_hash=version.context_hash,
                production_engine_context_json={},
                request_json={},
                lease_token=str(uuid4()) if status == JobStatus.running else None,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            session.add(job)
            jobs[status] = job.id
        session.commit()
    changed = json.loads(json.dumps(draft["workspace"]))
    changed["design"]["material"]["batch_id"] = "replacement-batch"
    saved = client.put(
        f"/v1/furniture/projects/{project['id']}/draft",
        headers=HEADERS,
        json={"expected_revision": 1, "workspace": changed},
    )
    assert saved.status_code == 200, saved.text
    old = client.get(f"{path}/1", headers=HEADERS).json()
    assert old["status"] == "superseded"
    assert old["immutable"]
    with get_session_factory()() as session:
        for status, job_id in jobs.items():
            job = session.get(GenerationJob, job_id)
            assert job is not None
            if status == JobStatus.succeeded:
                assert job.status == JobStatus.succeeded
            else:
                assert job.status == JobStatus.cancelled
                assert job.lease_token is None
                assert job.lease_expires_at is None
                assert job.next_attempt_at is None
                assert job.finished_at is not None
    assert client.post(path, headers=HEADERS, json=body).status_code == 409
    new_bridge = production_bridge(client, project, saved.json())
    new_body = production_request(new_bridge) | {"expected_current_revision": 1}
    second = client.post(path, headers=HEADERS, json=new_body)
    assert second.status_code == 201, second.text
    assert second.json()["revision"] == 2
    assert second.json()["context_hash"] != first.json()["context_hash"]
    assert second.json()["design_hash"] == first.json()["design_hash"]
    assert client.get(f"{path}/1", headers=HEADERS).json()["status"] == "superseded"


@pytest.mark.parametrize("family", ["table", "chest_of_drawers"])
def test_furniture_production_bridge_rejects_unsupported_families_and_other_tenants(client, family):
    project, draft = save(client, family)
    path = f"/v1/furniture/projects/{project['id']}/production-preview"
    body = {
        "expected_revision": draft["revision"],
        "expected_design_hash": draft["preview"]["design"]["design_hash"],
    }
    assert client.post(path, headers=OTHER, json=body).status_code == 404
    assert client.post(path, headers=HEADERS, json=body).status_code == 422


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
@pytest.mark.parametrize(
    "family",
    [
        "table",
        "chest_of_drawers",
        "shelving",
        "customer-shelving",
        "material-stock-shelving",
    ],
)
def test_real_family_review_exports_match_parts_and_contain_no_machine_programs(family):
    from pathlib import Path

    from tests.unit.test_furniture_stock_planning import back_stock, selected

    pytest.importorskip("cadquery")
    stock_document = selected()
    stock_document["manufacturing"]["material_stocks"] = [back_stock()]
    draft = (
        FurnitureWorkspace.model_validate_json(
            Path("examples/furniture/bookcase-4340x2540x280.json").read_text()
        )
        if family == "customer-shelving"
        else FurnitureWorkspace.model_validate(stock_document)
        if family == "material-stock-shelving"
        else workspace(family)
    )
    raw = build_furniture_review(draft)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        resolved = json.loads(archive.read("design/resolved.json"))
        handoff = json.loads(archive.read("manufacturing/workshop-handoff.json"))
        import csv

        measurements = list(
            csv.DictReader(
                io.StringIO(archive.read("inspection/first-article-checks.csv").decode("utf-8-sig"))
            )
        )
        assert {r["part_id"] for r in measurements} == {p["part_id"] for p in resolved["parts"]}
        assert all(r["design_hash"] == resolved["design_hash"] for r in measurements)
        assert all(not r["measured"] and not r["result"] for r in measurements)
        assert handoff["design_hash"] == resolved["design_hash"]
        assert sum(group["part_count"] for group in handoff["stock_requirements"]) == len(
            resolved["parts"]
        )
        if family == "material-stock-shelving":
            assert handoff["stock_plan"]["geometry_compatible"]
            assert handoff["stock_plan"]["stock_groups"][1]["selection_source"] == "material"
            assert (
                handoff["stock_plan"]
                == json.loads(archive.read("validation/review.json"))["manufacturing"]
            )
            assert (
                json.loads(archive.read("design/workspace.json"))["manufacturing"][
                    "material_stocks"
                ]
                == stock_document["manufacturing"]["material_stocks"]
            )
        if family == "customer-shelving":
            assert handoff["dimensions"]["installation"]["width_um"] == 4_340_000
            assert handoff["dimensions"]["installation"]["trim_profile"]["height_um"] == 90_000
            assert handoff["dimensions"]["installation"]["trim_profile"]["width_um"] == 20_000
            assert handoff["dimensions"]["state"] == "requires_resolution"
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


def test_material_stock_choices_survive_revisions_and_production_source_without_inventing_stock(
    client,
):
    from tests.unit.test_furniture_stock_planning import back_stock, selected

    project, previous = save(client, "shelving")
    document = selected()
    document["manufacturing"]["material_stocks"] = [back_stock()]
    path = f"/v1/furniture/projects/{project['id']}"
    response = client.put(
        f"{path}/draft",
        headers=HEADERS,
        json={
            "expected_revision": previous["revision"],
            "workspace": document,
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["workspace"]["manufacturing"]["material_stocks"] == [back_stock()]
    assert client.get(f"{path}/draft", headers=HEADERS).json() == saved
    assert (
        client.get(f"{path}/history", headers=HEADERS).json()["items"][0]["workspace"]
        == saved["workspace"]
    )
    assert client.get(f"{path}/draft", headers=OTHER).status_code == 404
    bridge = production_bridge(client, project, saved)
    assert bridge["source_furniture"]["workspace"]["manufacturing"]["material_stocks"] == [
        back_stock()
    ]
    # A planning format is not an inventory or an accepted production setup.
    assert "production_context" not in bridge["preview"]
    unchanged = client.put(
        f"{path}/draft",
        headers=HEADERS,
        json={
            "expected_revision": saved["revision"],
            "workspace": saved["workspace"],
        },
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["revision"] == saved["revision"]
