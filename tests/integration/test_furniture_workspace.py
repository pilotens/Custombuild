from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import app.furniture_api as furniture_api
import app.storage as storage_module
import pytest
from app.db import get_session_factory
from app.main import app
from app.models import (
    Artifact,
    DesignVersion,
    GenerationJob,
    GenerationJobType,
    JobStatus,
    OutboxEvent,
    StorageTenantQuota,
    StoredObject,
    StoredObjectState,
)
from botocore.exceptions import ClientError
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
TENANT = "11111111-1111-4111-8111-111111111111"
OTHER_TENANT = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def review_storage(monkeypatch):
    """Exercise real job, quota and verification code with only S3/CAD replaced."""

    class ReviewStorage:
        def __init__(self):
            self.objects = {}
            self.put_calls = []

        def head_bucket(self, **kwargs):
            pass

        def put_object(self, **kwargs):
            self.put_calls.append(kwargs)
            assert kwargs["IfNoneMatch"] == "*"
            key = kwargs["Key"]
            if key in self.objects:
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            self.objects[key] = {
                "ContentLength": len(kwargs["Body"]),
                "ContentType": kwargs["ContentType"],
                "Metadata": kwargs["Metadata"],
                "payload": bytes(kwargs["Body"]),
            }
            return {"ETag": '"created"'}

        def head_object(self, **kwargs):
            return {
                key: value for key, value in self.objects[kwargs["Key"]].items() if key != "payload"
            }

        def get_object(self, **kwargs):
            return {
                **self.head_object(**kwargs),
                "Body": io.BytesIO(self.objects[kwargs["Key"]]["payload"]),
            }

    import custombuild_worker.furniture_review as review

    storage = ReviewStorage()
    monkeypatch.setattr(tasks, "SessionFactory", get_session_factory())
    monkeypatch.setattr(tasks, "_s3_client", lambda: storage)
    monkeypatch.setattr(storage_module, "internal_s3_client", lambda: storage)
    monkeypatch.setattr(review, "build_furniture_review", lambda document: b"review-fixture")
    return storage


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


def test_export_uses_transactional_outbox_and_checks_snapshot_and_tenant(
    client,
    monkeypatch,
    review_storage,
):
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
        job = session.get(GenerationJob, job_id)
        assert job is not None
        assert job.job_type == GenerationJobType.furniture_review
        assert job.project_id == project["id"]
        assert job.design_version_id is None
        assert job.status == JobStatus.queued
        assert job.production_context_hash == content_hash(job.request_json)
        assert job.request_json["workspace"] == draft["workspace"]
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
    assert client.get(f"{path}/{job_id}", headers=HEADERS).json()["state"] == "queued"
    result = tasks.generate_furniture_review.run(job_id, TENANT)
    assert result["state"] == "succeeded"
    status = client.get(f"{path}/{job_id}", headers=HEADERS)
    assert status.status_code == 200, status.text
    exported = status.json()
    assert exported["state"] == "succeeded"
    assert exported["sha256"] == hashlib.sha256(b"review-fixture").hexdigest()
    assert exported["size_bytes"] == len(b"review-fixture")
    assert exported["revision"] == draft["revision"]
    assert not exported["physical_cutting_authorized"]
    assert "content_base64" not in exported
    downloaded = client.get(exported["download_path"], headers=HEADERS)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == b"review-fixture"
    assert downloaded.headers["content-type"] == "application/zip"
    assert client.get(exported["download_path"], headers=OTHER).status_code == 404
    assert client.get(f"/v1/jobs/{job_id}", headers=HEADERS).status_code == 404
    assert client.get(f"/v1/jobs/{job_id}/artifacts", headers=HEADERS).status_code == 404
    assert client.post(path, headers=HEADERS, json=request).json()["job_id"] == job_id
    assert len(review_storage.put_calls) == 1
    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None
        assert job.result_json is not None
        job.result_json = {**job.result_json, "workspace_sha256": "0" * 64}
    assert client.get(f"{path}/{job_id}", headers=HEADERS).status_code == 409
    assert client.get(exported["download_path"], headers=HEADERS).status_code == 409


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


def test_worker_review_loads_only_the_committed_tenant_snapshot(client, review_storage):
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

    assert tasks.generate_furniture_review.run(job_id, OTHER_TENANT)["state"] == (
        "already_running_or_complete"
    )
    assert review_storage.put_calls == []
    result = tasks.generate_furniture_review.run(job_id, TENANT)
    assert result["state"] == "succeeded"
    with get_session_factory()() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None
        assert job.result_json["workspace_sha256"] == content_hash(
            FurnitureWorkspace.model_validate(draft["workspace"])
        )
        assert job.status == JobStatus.succeeded
        assert job.lease_token is None
        assert job.attempts == 1
        stored = session.scalar(select(StoredObject).where(StoredObject.owner_id == job_id))
        assert stored is not None
        assert stored.state == StoredObjectState.committed
        assert stored.project_id == project["id"]
        assert stored.idempotency_key == (
            f"generation:{job_id}:furniture_review_bundle:{result['artifact_id']}"
        )
    assert tasks.generate_furniture_review.run(job_id, TENANT)["state"] == (
        "already_running_or_complete"
    )
    assert len(review_storage.put_calls) == 1


def request_review(client):
    project, draft = save(client)
    path = f"/v1/furniture/projects/{project['id']}/exports"
    body = {
        "expected_revision": draft["revision"],
        "expected_design_hash": draft["preview"]["design"]["design_hash"],
    }
    response = client.post(path, headers=HEADERS, json=body)
    assert response.status_code == 202, response.text
    return project, draft, path, body, response.json()["job_id"]


@pytest.mark.parametrize("state", (JobStatus.queued, JobStatus.running, JobStatus.succeeded))
def test_duplicate_review_reuses_exact_revision_after_five_minutes(client, review_storage, state):
    _project, _draft, path, body, job_id = request_review(client)
    if state == JobStatus.succeeded:
        assert tasks.generate_furniture_review.run(job_id, TENANT)["state"] == "succeeded"
    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        job.created_at = datetime.now(UTC) - timedelta(days=1)
        job.status = state
    response = client.post(path, headers=HEADERS, json=body)
    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == job_id
    with get_session_factory()() as session:
        events = list(
            session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_key == f"furniture-review:{job_id}")
            )
        )
        assert len(events) == 1


@pytest.mark.parametrize("changed", ("deadline", "engine", "engine_context"))
def test_review_reuse_rejects_expired_or_different_engine_context(client, changed):
    _project, _draft, path, body, job_id = request_review(client)
    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        if changed == "deadline":
            job.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        elif changed == "engine":
            job.request_json = {**job.request_json, "engine_version": "old-engine"}
            job.production_context_hash = content_hash(job.request_json)
        else:
            job.production_engine_context_json = {"furniture_engine_version": "old-engine"}
    if changed == "deadline":
        assert client.get(f"{path}/{job_id}", headers=HEADERS).json()["state"] == "failed"
    response = client.post(path, headers=HEADERS, json=body)
    assert response.status_code == 202, response.text
    assert response.json()["job_id"] != job_id


def test_changed_furniture_cancels_pending_review_and_old_worker_cannot_finish(
    client,
    review_storage,
):
    project, draft, path, body, job_id = request_review(client)
    claim = tasks._claim_job(job_id, TENANT, expected_job_type=GenerationJobType.furniture_review)
    assert claim is not None
    claimed, _ = claim
    assert (
        tasks._claim_job(job_id, TENANT, expected_job_type=GenerationJobType.furniture_review)
        is None
    )
    changed = draft["workspace"]
    changed["design"]["intent"]["width_um"] += 100_000
    saved = client.put(
        f"/v1/furniture/projects/{project['id']}/draft",
        headers=HEADERS,
        json={"expected_revision": draft["revision"], "workspace": changed},
    )
    assert saved.status_code == 200, saved.text
    assert client.get(f"{path}/{job_id}", headers=HEADERS).json()["state"] == "cancelled"
    assert not tasks._complete_furniture_review(job_id, TENANT, claimed.lease_token, {})
    assert tasks.generate_furniture_review.run(job_id, TENANT)["state"] == (
        "already_running_or_complete"
    )
    assert client.post(path, headers=HEADERS, json=body).status_code == 409
    assert review_storage.put_calls == []


def test_transient_review_failure_retries_through_durable_typed_outbox(
    client,
    monkeypatch,
    review_storage,
):
    import custombuild_worker.furniture_review as review

    _project, _draft, path, _body, job_id = request_review(client)

    def transient_failure(_document):
        raise OSError("temporary generation failure")

    monkeypatch.setattr(review, "build_furniture_review", transient_failure)
    result = tasks.generate_furniture_review.run(job_id, TENANT)
    assert result == {"job_id": job_id, "state": "retry_scheduled", "retry_after_seconds": 5}
    assert review_storage.put_calls == []
    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        assert job.status == JobStatus.queued
        assert job.attempts == 1
        assert job.lease_token is None
        event = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_key == f"furniture-review-retry:{job_id}:1")
        )
        assert event is not None
        assert event.topic == "furniture.review.requested"
        assert event.payload_json == {"job_id": job_id, "organization_id": TENANT}
        assert event.available_at == job.next_attempt_at
        assert tasks.generate_furniture_review.run(job_id, TENANT)["state"] == (
            "already_running_or_complete"
        )
        job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(review, "build_furniture_review", lambda document: b"review-fixture")
    assert tasks.generate_furniture_review.run(job_id, TENANT)["state"] == "succeeded"
    assert client.get(f"{path}/{job_id}", headers=HEADERS).json()["state"] == "succeeded"
    with get_session_factory()() as session:
        assert session.get(GenerationJob, job_id).attempts == 2


def test_review_stale_lease_recovers_to_review_task_without_production_claim(
    client,
    review_storage,
):
    _project, _draft, _path, _body, job_id = request_review(client)
    assert tasks._claim_job(job_id, TENANT) is None
    claimed, version = tasks._claim_job(
        job_id, TENANT, expected_job_type=GenerationJobType.furniture_review
    )
    assert version is None
    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        event = tasks._recover_stale_job(job, now=datetime.now(UTC))
        assert event is not None
        assert event.topic == "furniture.review.requested"
        assert event.event_key == f"furniture-review-recovery:{job_id}:1"
        session.add(event)
    assert not tasks._complete_furniture_review(job_id, TENANT, claimed.lease_token, {})
    assert tasks.generate_furniture_review.run(job_id, TENANT)["state"] == "succeeded"
    assert len(review_storage.put_calls) == 1


@pytest.mark.parametrize(
    "field",
    ("engine_version", "workspace_sha256", "design_hash", "revision", "project_id", "requested_by"),
)
def test_review_worker_rejects_detached_snapshot_before_upload(client, review_storage, field):
    from custombuild_manufacturing import ProductionBlockedError

    _project, _draft, path, _body, job_id = request_review(client)
    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        job.request_json = {
            **job.request_json,
            field: 99 if field == "revision" else "0" * 64,
        }
        job.production_context_hash = content_hash(job.request_json)
    with pytest.raises(ProductionBlockedError, match="furniture review"):
        tasks.generate_furniture_review.run(job_id, TENANT)
    assert review_storage.put_calls == []
    assert client.get(f"{path}/{job_id}", headers=HEADERS).json()["state"] == "failed"


@pytest.mark.parametrize(
    "field",
    (
        "object_key",
        "artifact_id",
        "sha256",
        "size_bytes",
        "physical_cutting_authorized",
        "content_type",
        "file_name",
    ),
)
def test_review_status_and_download_reject_detached_artifact_identity(
    client,
    review_storage,
    field,
):
    _project, _draft, path, _body, job_id = request_review(client)
    tasks.generate_furniture_review.run(job_id, TENANT)
    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        job.result_json = {**job.result_json, field: "invalid"}
    assert client.get(f"{path}/{job_id}", headers=HEADERS).status_code == 409
    assert client.get(f"{path}/{job_id}/download", headers=HEADERS).status_code == 409


def test_review_download_verifies_object_bytes_and_exact_ledger_binding(client, review_storage):
    _project, _draft, path, _body, job_id = request_review(client)
    tasks.generate_furniture_review.run(job_id, TENANT)
    with get_session_factory()() as session:
        artifact = session.scalar(select(Artifact).where(Artifact.generation_job_id == job_id))
        assert artifact is not None
        object_key = artifact.object_key
    original = review_storage.objects[object_key]["payload"]
    review_storage.objects[object_key]["payload"] = b"X" * len(original)
    assert client.get(f"{path}/{job_id}/download", headers=HEADERS).status_code == 409
    review_storage.objects[object_key]["payload"] = original
    with get_session_factory().begin() as session:
        stored = session.get(StoredObject, (TENANT, object_key))
        stored.idempotency_key = "different-owner"
    assert client.get(f"{path}/{job_id}", headers=HEADERS).status_code == 409
    assert client.get(f"{path}/{job_id}/download", headers=HEADERS).status_code == 409


def test_review_quota_exhaustion_fails_before_upload_and_does_not_create_artifact(
    client,
    review_storage,
):
    from custombuild_manufacturing import ProductionBlockedError

    _project, _draft, path, _body, job_id = request_review(client)
    with get_session_factory().begin() as session:
        quota = session.get(StorageTenantQuota, TENANT)
        original_limit = quota.byte_limit
        quota.byte_limit = quota.reserved_bytes + quota.committed_bytes + 1
    try:
        with pytest.raises(ProductionBlockedError, match="storage quota"):
            tasks.generate_furniture_review.run(job_id, TENANT)
        assert review_storage.put_calls == []
        assert client.get(f"{path}/{job_id}", headers=HEADERS).json()["state"] == "failed"
        with get_session_factory()() as session:
            assert (
                session.scalar(select(Artifact).where(Artifact.generation_job_id == job_id)) is None
            )
            assert (
                session.scalar(select(StoredObject).where(StoredObject.owner_id == job_id)) is None
            )
    finally:
        with get_session_factory().begin() as session:
            session.get(StorageTenantQuota, TENANT).byte_limit = original_limit


@pytest.mark.parametrize("boundary", ("deadline", "attempts"))
def test_review_claim_enforces_execution_budget(client, review_storage, boundary):
    _project, _draft, path, _body, job_id = request_review(client)
    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        if boundary == "deadline":
            job.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        else:
            job.attempts = tasks.MAX_GENERATION_ATTEMPTS
    assert tasks.generate_furniture_review.run(job_id, TENANT)["state"] == (
        "already_running_or_complete"
    )
    assert client.get(f"{path}/{job_id}", headers=HEADERS).json()["state"] == "failed"
    assert review_storage.put_calls == []


def test_review_status_and_download_do_not_cross_projects(client, review_storage):
    _project, _draft, _path, _body, job_id = request_review(client)
    other_project, _ = save(client)
    path = f"/v1/furniture/projects/{other_project['id']}/exports/{job_id}"
    assert client.get(path, headers=HEADERS).status_code == 404
    tasks.generate_furniture_review.run(job_id, TENANT)
    assert client.get(path, headers=HEADERS).status_code == 404
    assert client.get(f"{path}/download", headers=HEADERS).status_code == 404


def test_review_completion_integrity_failure_rolls_back_artifact_and_quota_commit(
    client,
    monkeypatch,
    review_storage,
):
    from custombuild_manufacturing import ProductionBlockedError

    _project, _draft, path, _body, job_id = request_review(client)
    complete = tasks._complete_furniture_review

    def corrupt_before_completion(job_id, organization_id, lease_token, result):
        stored = review_storage.objects[result["object_key"]]
        stored["payload"] = b"X" * len(stored["payload"])
        return complete(job_id, organization_id, lease_token, result)

    monkeypatch.setattr(tasks, "_complete_furniture_review", corrupt_before_completion)
    with pytest.raises(ProductionBlockedError, match="final integrity verification"):
        tasks.generate_furniture_review.run(job_id, TENANT)
    with get_session_factory()() as session:
        job = session.get(GenerationJob, job_id)
        assert job.status == JobStatus.failed
        assert job.result_json is None
        assert session.scalar(select(Artifact).where(Artifact.generation_job_id == job_id)) is None
        stored = session.scalar(select(StoredObject).where(StoredObject.owner_id == job_id))
        assert stored is not None
        # Uncommitted bytes remain charged until the established storage reaper
        # has safely verified and removed them; no orphan escapes the quota.
        assert stored.state == StoredObjectState.reserved
    assert client.get(f"{path}/{job_id}", headers=HEADERS).json()["state"] == "failed"
    assert client.get(f"{path}/{job_id}/download", headers=HEADERS).status_code == 404


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
