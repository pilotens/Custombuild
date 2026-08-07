from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import app.api as api_module
import pytest
from app.auth import DEV_ORG_NORDIC
from app.db import get_session_factory
from app.main import app
from app.models import Artifact, DesignVersion, GenerationJob, JobStatus
from fastapi.testclient import TestClient

HEADERS = {"Authorization": "Bearer demo-nordic-owner"}


def valid_spec() -> dict[str, object]:
    return {
        "width_mm": 700,
        "height_mm": 1000,
        "depth_mm": 320,
        "nominal_thickness_mm": 18,
        "measured_thickness_mm": 18,
        "shelf_count": 2,
        "load_per_shelf_kg": 10,
        "back_panel": True,
        "plinth": True,
        "divider_count": 0,
        "joint_system": "dado",
        "reinforcement_mode": "manual",
        "wall_anchor_required": False,
    }


def test_preview_is_reproducible() -> None:
    with TestClient(app) as client:
        first = client.post("/v1/designs/preview", headers=HEADERS, json=valid_spec())
        second = client.post("/v1/designs/preview", headers=HEADERS, json=valid_spec())
        assert first.status_code == second.status_code == 200
        assert first.json()["design_hash"] == second.json()["design_hash"]
        assert first.json()["parts"] == second.json()["parts"]
        assert first.json()["status"] == "PASS"


def test_joint_capability_matrix_and_preview_reject_unverified_systems() -> None:
    with TestClient(app) as client:
        capabilities = client.get("/v1/capabilities/joints", headers=HEADERS)
        assert capabilities.status_code == 200
        payload = capabilities.json()
        assert payload["version"] == "bookcase-joints-1.0.0"
        assert payload["joints"]["dado"]["status"] == "supported"
        assert payload["joints"]["shelf_pin"]["status"] == "conditional"
        assert {
            joint for joint, claim in payload["joints"].items() if claim["status"] == "blocked"
        } == {"dowel", "confirmat", "cam_dowel", "rabbet", "tenon"}

        for unsupported in (
            "dowel",
            "confirmat",
            "cam_dowel",
            "shelf_pin",
            "rabbet",
            "tenon",
        ):
            response = client.post(
                "/v1/designs/preview",
                headers=HEADERS,
                json=valid_spec() | {"joint_system": unsupported},
            )
            assert response.status_code == 422


def test_persist_validate_and_idempotently_queue_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Vertical flow fixture"}
        ).json()
        version_response = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={"spec": valid_spec()},
        )
        assert version_response.status_code == 201
        version = version_response.json()
        validated = client.post(
            f"/v1/projects/{project['id']}/versions/{version['revision']}/validate",
            headers=HEADERS,
        )
        assert validated.status_code == 200
        assert validated.json()["status"] == "design_validated"

        path = f"/v1/projects/{project['id']}/versions/{version['revision']}/generate"
        generation = {
            "stock_width_mm": 2440,
            "stock_height_mm": 1220,
            "stock_count": 4,
        }
        first = client.post(path, headers=HEADERS, json=generation)
        second = client.post(path, headers=HEADERS, json=generation)
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["production_context_hash"] == second.json()["production_context_hash"]
        frozen_context = first.json()["production_engine_context_json"]
        assert frozen_context["schema_version"] == "custombuild.production-engine-context.v1"
        assert len(frozen_context["machine_profile_fingerprint"]) == 64
        assert len(frozen_context["tool_library_fingerprint"]) == 64

        original_resolver = api_module.resolve_production_components

        def drifted_resolver(
            *,
            machine_profile_id: str,
            postprocessor_id: str,
            app_version: str,
            require_cad_runtime: bool = False,
        ):
            resolved = original_resolver(
                machine_profile_id=machine_profile_id,
                postprocessor_id=postprocessor_id,
                app_version=app_version,
                require_cad_runtime=require_cad_runtime,
            )
            return replace(
                resolved,
                context=replace(
                    resolved.context,
                    operations_engine_version="semantic-operations-library-drift",
                ),
            )

        monkeypatch.setattr(api_module, "resolve_production_components", drifted_resolver)
        after_library_drift = client.post(path, headers=HEADERS, json=generation)
        assert after_library_drift.status_code == 202
        assert after_library_drift.json()["id"] != first.json()["id"]
        assert (
            after_library_drift.json()["production_context_hash"]
            != first.json()["production_context_hash"]
        )


@pytest.mark.parametrize(
    "invalid_selection",
    (
        {"machine_profile_id": "unknown-machine"},
        {"postprocessor_id": "unknown-postprocessor"},
    ),
)
def test_generation_rejects_unversioned_catalog_selection(
    invalid_selection: dict[str, str],
) -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": f"Invalid catalog {invalid_selection}"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={"spec": valid_spec()},
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200

        response = client.post(f"{base}/generate", headers=HEADERS, json=invalid_selection)

        assert response.status_code == 422
        assert "unknown or unverified" in str(response.json()["detail"])


def test_generation_requires_a_new_revision_after_design_library_drift() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Stale domain library fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={"spec": valid_spec()},
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        with get_session_factory().begin() as session:
            stored = session.get(DesignVersion, version["id"])
            assert stored is not None
            stored.engine_version = f"{stored.engine_version}-stale"

        response = client.post(f"{base}/generate", headers=HEADERS, json={})

        assert response.status_code == 409
        assert "frozen design libraries are stale" in str(response.json()["detail"])


def test_client_cannot_self_assert_wall_anchor_evidence() -> None:
    with TestClient(app) as client:
        payload = valid_spec() | {"wall_anchor_verified": True}
        response = client.post("/v1/designs/preview", headers=HEADERS, json=payload)
        assert response.status_code == 422
        assert "not accepted" in str(response.json()["detail"])


def test_measured_thickness_cannot_expand_the_versioned_material_range() -> None:
    with TestClient(app) as client:
        for unsupported_thickness in (16.9, 19.1):
            response = client.post(
                "/v1/designs/preview",
                headers=HEADERS,
                json=valid_spec() | {"measured_thickness_mm": unsupported_thickness},
            )
            assert response.status_code == 422


def _complete_generation(job_id: str, manifest_sha: str) -> None:
    with get_session_factory().begin() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None
        job.status = JobStatus.succeeded
        job.finished_at = datetime.now(UTC)
        job.result_json = {
            "authoritative_geometry": True,
            "dfm_status": "PASS",
            "manifest_sha256": manifest_sha,
        }


def test_cam_approval_is_bound_to_exact_job_context_and_manifest() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Approval binding fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={"spec": valid_spec()},
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200

        first = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(first["id"], "a" * 64)
        assert (
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={"approval_type": "design", "reason": "Design review completed"},
            ).status_code
            == 200
        )
        missing_job = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={"approval_type": "cam", "reason": "CAM review completed"},
        )
        assert missing_job.status_code == 422
        approved = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "CAM review completed",
                "generation_job_id": first["id"],
            },
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        second = client.post(f"{base}/generate", headers=HEADERS, json={"stock_width_mm": 2500})
        assert second.status_code == 202
        stale_release = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "R1", "confirmation": "RELEASE"},
        )
        assert stale_release.status_code == 409

        second_job = second.json()
        _complete_generation(second_job["id"], "b" * 64)
        rebound = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Rechecked changed stock context",
                "generation_job_id": second_job["id"],
            },
        )
        assert rebound.status_code == 200
        released = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "R1", "confirmation": "RELEASE"},
        )
        assert released.status_code == 200
        assert released.json()["manifest_sha256"] == "b" * 64
        repeated = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "IGNORED-R2", "confirmation": "RELEASE"},
        )
        assert repeated.status_code == 200
        assert repeated.json() == released.json()


def test_warning_requires_attributed_override_and_is_frozen_into_job_context() -> None:
    warning_spec = valid_spec() | {
        "width_mm": 500,
        "height_mm": 700,
        "depth_mm": 400,
        "shelf_count": 2,
        "load_per_shelf_kg": 39,
    }
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Warning override fixture"}
        ).json()
        version = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={"spec": warning_spec},
        ).json()
        assert version["result_json"]["status"] == "WARNING"
        base = f"/v1/projects/{project['id']}/versions/{version['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        blocked = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert blocked.status_code == 409

        missing_override = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={"approval_type": "design", "reason": "Reviewed warning"},
        )
        assert missing_override.status_code == 422
        approved = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "design",
                "reason": "Reviewed construction warning",
                "warning_overrides": [
                    {
                        "rule_id": "CB-DEFLECTION-001",
                        "reason": "Accepted for this screening fixture after documented review",
                    }
                ],
            },
        )
        assert approved.status_code == 200
        job_response = client.post(f"{base}/generate", headers=HEADERS, json={})
        assert job_response.status_code == 202

        with get_session_factory()() as session:
            job = session.get(GenerationJob, job_response.json()["id"])
            assert job is not None
            overrides = job.request_json["approved_warning_overrides"]
            assert len(overrides) == 1
            assert overrides[0]["rule_id"] == "CB-DEFLECTION-001"
            assert overrides[0]["approved_by"]
            assert overrides[0]["approved_at"]
            assert "documented review" in overrides[0]["reason"]

        _complete_generation(job_response.json()["id"], "c" * 64)
        assert (
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={
                    "approval_type": "cam",
                    "reason": "Reviewed generated CAM context",
                    "generation_job_id": job_response.json()["id"],
                },
            ).status_code
            == 200
        )
        changed_override = client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "design",
                "reason": "Construction warning reviewed again",
                "warning_overrides": [
                    {
                        "rule_id": "CB-DEFLECTION-001",
                        "reason": "Changed justification requires a new production generation",
                    }
                ],
            },
        )
        assert changed_override.status_code == 200
        assert changed_override.json()["status"] == "design_validated"
        release = client.post(
            f"{base}/release",
            headers=HEADERS,
            json={"release_number": "WARN-R1", "confirmation": "RELEASE"},
        )
        assert release.status_code == 409


def test_new_design_revision_supersedes_release_and_blocks_stale_artifacts() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Supersession fixture"}
        ).json()
        first = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={"spec": valid_spec()},
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{first['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        job = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        _complete_generation(job["id"], "d" * 64)
        with get_session_factory().begin() as session:
            session.add(
                Artifact(
                    organization_id=DEV_ORG_NORDIC,
                    generation_job_id=job["id"],
                    kind="production_bundle",
                    object_key="stale-fixture/production.zip",
                    sha256="e" * 64,
                    size_bytes=123,
                    content_type="application/zip",
                )
            )
        assert (
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={"approval_type": "design", "reason": "Design review completed"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/approve",
                headers=HEADERS,
                json={
                    "approval_type": "cam",
                    "reason": "CAM review completed",
                    "generation_job_id": job["id"],
                },
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/release",
                headers=HEADERS,
                json={"release_number": "STALE-R1", "confirmation": "RELEASE"},
            ).status_code
            == 200
        )

        artifact_listing = client.get(f"/v1/jobs/{job['id']}/artifacts", headers=HEADERS)
        assert artifact_listing.status_code == 200
        assert artifact_listing.json()[0]["download_url"].startswith("http://localhost:9000/")
        stale_download_path = artifact_listing.json()[0]["download_path"]

        changed_spec = valid_spec() | {"width_mm": 710}
        second = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={"spec": changed_spec},
        )
        assert second.status_code == 201
        assert second.json()["revision"] == 2

        versions = client.get(f"/v1/projects/{project['id']}/versions", headers=HEADERS).json()
        assert [(item["revision"], item["status"]) for item in versions] == [
            (2, "draft"),
            (1, "superseded"),
        ]
        assert versions[1]["immutable"] is True
        assert client.get(f"/v1/jobs/{job['id']}/artifacts", headers=HEADERS).status_code == 409
        assert client.get(stale_download_path, headers=HEADERS).status_code == 409
        assert (
            client.post(
                f"{base}/release",
                headers=HEADERS,
                json={"release_number": "STALE-R1", "confirmation": "RELEASE"},
            ).status_code
            == 409
        )


def test_new_design_revision_cancels_unfinished_generation() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Cancellation fixture"}
        ).json()
        first = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={"spec": valid_spec()},
        ).json()
        base = f"/v1/projects/{project['id']}/versions/{first['revision']}"
        assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
        queued = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
        assert queued["status"] == "queued"

        changed = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={"spec": valid_spec() | {"depth_mm": 330}},
        )
        assert changed.status_code == 201
        cancelled = client.get(f"/v1/jobs/{queued['id']}", headers=HEADERS)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert "superseded" in cancelled.json()["error"]


def test_reverting_to_an_older_design_creates_a_new_revision() -> None:
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects", headers=HEADERS, json={"name": "Revert fixture"}
        ).json()
        first = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={"spec": valid_spec()},
        ).json()
        first_base = f"/v1/projects/{project['id']}/versions/{first['revision']}"
        assert client.post(f"{first_base}/validate", headers=HEADERS).status_code == 200
        first_job = client.post(f"{first_base}/generate", headers=HEADERS, json={}).json()
        second = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={"spec": valid_spec() | {"width_mm": 720}},
        ).json()
        reverted = client.post(
            f"/v1/projects/{project['id']}/versions",
            headers=HEADERS,
            json={"spec": valid_spec()},
        )

        assert reverted.status_code == 201
        assert (first["revision"], second["revision"], reverted.json()["revision"]) == (
            1,
            2,
            3,
        )
        assert reverted.json()["design_hash"] == first["design_hash"]
        reverted_base = f"/v1/projects/{project['id']}/versions/{reverted.json()['revision']}"
        assert client.post(f"{reverted_base}/validate", headers=HEADERS).status_code == 200
        reverted_job = client.post(f"{reverted_base}/generate", headers=HEADERS, json={}).json()
        assert reverted_job["production_context_hash"] != first_job["production_context_hash"]
        versions = client.get(f"/v1/projects/{project['id']}/versions", headers=HEADERS).json()
        assert [item["status"] for item in versions] == [
            "design_validated",
            "superseded",
            "superseded",
        ]
