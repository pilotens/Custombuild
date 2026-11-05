import hashlib
import io
import json
import zipfile
from copy import deepcopy
from uuid import uuid4

import pytest
from app.db import get_session_factory
from app.main import app
from app.models import GenerationJob, OutboxEvent, Project
from custombuild_domain.identity import content_hash
from custombuild_domain.measured_assembly import MeasuredAssembly, measured_assembly_hash
from custombuild_worker import tasks
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.furniture_review_storage import install_review_storage
from tests.integration.test_api_design_flow import valid_spec, valid_workspace_intent

HEADERS = {"Authorization": "Bearer demo-nordic-owner"}
OTHER = {"Authorization": "Bearer demo-atelier-owner"}
TENANT = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def request_body(client):
    response = client.post("/v1/constructions/trestle-table", headers=HEADERS, json={})
    assert response.status_code == 200, response.text
    construction = response.json()
    return dict(
        request=construction["request"],
        expected_construction_sha256=construction["construction_sha256"],
        idempotency_key=str(uuid4()),
    )


def test_construction_requires_authentication_and_rejects_unsupported_inputs(client):
    path = "/v1/constructions/trestle-table"
    assert client.post(path, json={}).status_code == 401
    assert client.post(path, headers=HEADERS, json={"length_mm": 3000}).status_code == 422
    response = client.post(path, headers=HEADERS, json={})
    assert response.status_code == 200
    data = response.json()
    assert len(data["assembly"]["joints"]) == 29
    assert data["assembly_sha256"] == measured_assembly_hash(
        MeasuredAssembly.model_validate(data["assembly"])
    )


def test_export_is_hash_bound_idempotent_tenant_scoped_and_uses_outbox(client, monkeypatch):
    storage = install_review_storage(monkeypatch)
    import custombuild_worker.trestle_review as exporter

    monkeypatch.setattr(exporter, "build_trestle_review", lambda source: b"trestle-fixture")
    body = request_body(client)
    path = "/v1/constructions/trestle-table/exports"
    assert (
        client.post(
            path, headers=HEADERS, json={**body, "expected_construction_sha256": "0" * 64}
        ).status_code
        == 409
    )
    created = client.post(path, headers=HEADERS, json=body)
    assert created.status_code == 202, created.text
    job = created.json()
    assert client.post(path, headers=HEADERS, json=body).json() == job
    changed = request_body(client)
    changed["idempotency_key"] = body["idempotency_key"]
    changed["request"]["length_mm"] = 2400
    preview = client.post(
        "/v1/constructions/trestle-table", headers=HEADERS, json=changed["request"]
    ).json()
    changed["expected_construction_sha256"] = preview["construction_sha256"]
    assert client.post(path, headers=HEADERS, json=changed).status_code == 409
    status = f"/v1/furniture/projects/{job['project_id']}/exports/{job['job_id']}"
    assert client.get(status, headers=OTHER).status_code == 404
    with get_session_factory()() as session:
        saved = session.get(GenerationJob, job["job_id"])
        assert saved.production_context_hash == content_hash(saved.request_json)
        assert (
            saved.request_json["workspace"]["schema_version"] == "custombuild.trestle-workspace.v1"
        )
        assert (
            session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.event_key == f"furniture-review:{job['job_id']}"
                )
            )
            is not None
        )
    assert tasks.generate_furniture_review.run(job["job_id"], TENANT)["state"] == "succeeded"
    ready = client.get(status, headers=HEADERS).json()
    assert ready["sha256"] == hashlib.sha256(b"trestle-fixture").hexdigest()
    assert ready["physical_cutting_authorized"] is False
    download = client.get(ready["download_path"], headers=HEADERS)
    assert download.content == b"trestle-fixture"
    assert len(storage.put_calls) == 1
    assert client.get(ready["download_path"], headers=OTHER).status_code == 404
    inspection = (
        f"/v1/constructions/trestle-table/projects/{job['project_id']}"
        f"/exports/{job['job_id']}/inspection"
    )
    assert client.get(inspection, headers=HEADERS).status_code == 409
    assert client.get(inspection, headers=OTHER).status_code == 404


def test_other_editor_cannot_overwrite_saved_construction(client):
    result = client.post(
        "/v1/constructions/trestle-table/exports", headers=HEADERS, json=request_body(client)
    ).json()
    response = client.put(
        f"/v1/projects/{result['project_id']}/draft",
        headers=HEADERS,
        json={
            "expected_draft_revision": 1,
            "template_id": "shelving",
            "spec": valid_spec(),
            "workspace_spec": valid_workspace_intent(),
        },
    )
    assert response.status_code == 409
    with get_session_factory()() as session:
        project = session.get(Project, result["project_id"])
        assert project.draft_spec_json["schema_version"] == "custombuild.trestle-workspace.v1"


@pytest.mark.parametrize("fault", ["source", "revision", "hash", "project"])
def test_worker_rejects_tampered_construction_snapshot(client, fault):
    result = client.post(
        "/v1/constructions/trestle-table/exports", headers=HEADERS, json=request_body(client)
    ).json()
    with get_session_factory()() as session:
        job = session.get(GenerationJob, result["job_id"])
        document = deepcopy(job.request_json)
        if fault == "source":
            document["workspace"]["request"]["width_mm"] += 10
        elif fault == "revision":
            document["workspace"]["revision"] = 2
        elif fault == "hash":
            document["design_hash"] = "0" * 64
        else:
            document["workspace"]["project_id"] = str(uuid4())
        job.request_json = document
        job.production_context_hash = content_hash(document)
        with pytest.raises(tasks.ProductionBlockedError):
            tasks._validated_furniture_review_snapshot(job)


def test_real_export_includes_machined_step_face_drawings_and_every_operation(client, monkeypatch):
    install_review_storage(monkeypatch)
    result = client.post(
        "/v1/constructions/trestle-table/exports", headers=HEADERS, json=request_body(client)
    ).json()
    assert tasks.generate_furniture_review.run(result["job_id"], TENANT)["state"] == "succeeded"
    path = f"/v1/furniture/projects/{result['project_id']}/exports/{result['job_id']}"
    ready = client.get(path, headers=HEADERS).json()
    blob = client.get(ready["download_path"], headers=HEADERS).content
    inspection = client.get(
        f"/v1/constructions/trestle-table/projects/{result['project_id']}"
        f"/exports/{result['job_id']}/inspection",
        headers=HEADERS,
    )
    assert inspection.status_code == 200, inspection.text
    checked = inspection.json()
    assert len(checked["joints"]) == 29
    assert all(j["state"] == "aligned" for j in checked["joints"])
    assert len(checked["bearings"]) == 3
    assert all(b["state"] == "aligned" for b in checked["bearings"])
    assert not checked["unreviewed_profile_parts"] and not any(checked["issues"].values())
    assert checked["physical_cutting_authorized"] is False
    assert checked["geometry_method"] == "exact_cad"
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = archive.namelist()
        assert sum(n.endswith(".step") for n in names) == 14
        assert sum(n.endswith(".dxf") for n in names) == 78
        assert sum(n.endswith(".svg") for n in names) == 78
        audit = json.loads(archive.read("validation/cad.json"))
        assert audit["geometric_checks_passed"]
        package = json.loads(archive.read("construction.json"))
        assert audit["assembly_sha256"] == package["assembly_sha256"]
        assert checked["assembly_sha256"] == package["assembly_sha256"]
        assert package["construction_sha256"] == ready["design_hash"]
        assert len(json.loads(archive.read("validation/step-roundtrips.json"))) == 13
        assert archive.read("documents/ritningar.pdf").startswith(b"%PDF")
        manifest = json.loads(archive.read("manifest.json"))
        for entry in manifest["files"]:
            content = archive.read(entry["path"])
            assert len(content) == entry["size_bytes"]
            assert hashlib.sha256(content).hexdigest() == entry["sha256"]
