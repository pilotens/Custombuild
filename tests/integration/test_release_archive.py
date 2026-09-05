from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import app.api as api_module
import app.storage as storage_module
import pytest
from app.db import get_session_factory
from app.main import app
from app.models import (
    Approval,
    Artifact,
    GenerationJob,
    JobStatus,
    Release,
    StoredObject,
)
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm.attributes import flag_modified

from tests.integration import test_api_design_flow as flow

HEADERS = flow.HEADERS
OTHER_TENANT_HEADERS = {"Authorization": "Bearer demo-atelier-owner"}


@pytest.fixture(autouse=True)
def _use_verified_in_process_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this API integration suite independent of an external S3 service."""

    def open_verified(
        expectation: storage_module.StoredObjectExpectation,
        *,
        max_bytes: int,
    ) -> flow._SimulatedVerifiedDownload:
        if expectation.size_bytes > max_bytes:
            raise storage_module.ArtifactIntegrityError("size limit")
        return flow._SimulatedVerifiedDownload(expectation)

    monkeypatch.setattr(
        api_module,
        "verify_stored_object",
        lambda _expectation, *, stream_hash: None,
    )
    monkeypatch.setattr(
        api_module,
        "read_verified_stored_object",
        flow._simulated_verified_review_document,
    )
    monkeypatch.setattr(api_module, "open_verified_stored_object", open_verified)
    monkeypatch.setattr(api_module, "store_immutable_object", lambda *_args: None)
    monkeypatch.setattr(api_module, "_require_resolved_dado_retention", lambda _version: None)
    monkeypatch.setattr(
        api_module,
        "_frozen_dado_retention_is_unresolved",
        lambda _version: False,
    )

    def is_canonical_legacy_validation_result(result: dict[str, object]) -> bool:
        readiness = result.get("workshop_readiness")
        explicit_validation_claim = (
            result.get("machine_program_mode") == "VALIDATION_DRY_RUN"
            and result.get("production_machine_program") is False
        )
        historical_v1_claim = (
            "machine_program_mode" not in result
            and "production_machine_program" not in result
            and isinstance(readiness, dict)
            and readiness.get("schema_version") == flow.LEGACY_WORKSHOP_READINESS_SCHEMA_VERSION
        )
        return result.get("cam_candidate") is None and (
            explicit_validation_claim or historical_v1_claim
        )

    def allow_only_canonical_legacy_validation_fixture(job: GenerationJob) -> None:
        request = job.request_json if isinstance(job.request_json, dict) else {}
        result = job.result_json if isinstance(job.result_json, dict) else {}
        if request.get("include_cutting_candidate", False) is False and (
            is_canonical_legacy_validation_result(result)
        ):
            return
        flow._REAL_CAM_PROMOTION_GATE(job)

    def legacy_validation_candidate_digest(result: dict[str, object]) -> str:
        if is_canonical_legacy_validation_result(result):
            return flow._LEGACY_VALIDATION_ONLY_CAM_DIGEST
        return flow._REAL_CAM_CANDIDATE_DIGEST(result)

    # These archive regressions intentionally exercise pre-executable-CAM
    # validation releases. Keep only that exact legacy fixture isolated while
    # every real or malformed candidate still goes through the production gate.
    monkeypatch.setattr(
        api_module,
        "_require_promotable_cam_candidate_profile",
        allow_only_canonical_legacy_validation_fixture,
    )
    monkeypatch.setattr(
        api_module,
        "_cam_candidate_bundle_sha256",
        legacy_validation_candidate_digest,
    )


def _release_first_revision_and_create_second(
    client: TestClient,
    *,
    name: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, object], str]:
    project = client.post("/v1/projects", headers=HEADERS, json={"name": name}).json()
    project_id = str(project["id"])
    first = client.post(
        f"/v1/projects/{project_id}/versions",
        headers=HEADERS,
        json=flow.version_payload(project_id),
    ).json()
    base = f"/v1/projects/{project_id}/versions/{first['revision']}"
    assert client.post(f"{base}/validate", headers=HEADERS).status_code == 200
    flow.approve_design(client, base)
    job = client.post(f"{base}/generate", headers=HEADERS, json={}).json()
    flow._complete_generation(str(job["id"]), "8" * 64)
    assert (
        client.post(
            f"{base}/approve",
            headers=HEADERS,
            json=flow.design_approval_payload(client, base),
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"{base}/approve",
            headers=HEADERS,
            json={
                "approval_type": "cam",
                "reason": "Archived release CAM review completed",
                "generation_job_id": job["id"],
            },
        ).status_code
        == 200
    )
    release_response = client.post(
        f"{base}/release",
        headers=HEADERS,
        json={"release_number": "ARCHIVE-R1", "confirmation": "RELEASE"},
    )
    assert release_response.status_code == 200
    release = release_response.json()

    current_listing = client.get(f"/v1/jobs/{job['id']}/artifacts", headers=HEADERS)
    assert current_listing.status_code == 200
    current_bundle_path = next(
        item["download_path"]
        for item in current_listing.json()
        if item["kind"] == "production_bundle"
    )

    changed_spec = flow.valid_spec() | {"width_mm": 710}
    second = client.post(
        f"/v1/projects/{project_id}/versions",
        headers=HEADERS,
        json=flow.version_payload(
            project_id,
            changed_spec,
            expected_current_revision=int(first["revision"]),
        ),
    )
    assert second.status_code == 201
    return release, job, first, current_bundle_path


def test_superseded_release_keeps_its_exact_immutable_package() -> None:
    with TestClient(app) as client:
        release, job, first, current_bundle_path = _release_first_revision_and_create_second(
            client,
            name=f"Historical release {uuid4()}",
        )

        assert client.get(current_bundle_path, headers=HEADERS).status_code == 409

        # Historical retrieval is authorized by the immutable release and its
        # package. Later deletion/replacement of mutable approval authorities
        # must not retroactively destroy the released archive.
        with get_session_factory().begin() as session:
            session.execute(delete(Approval).where(Approval.design_version_id == str(first["id"])))

        listing = client.get(
            f"/v1/releases/{release['release_id']}/artifacts",
            headers=HEADERS,
        )
        assert listing.status_code == 200
        assert listing.json()
        assert {item["release_id"] for item in listing.json()} == {release["release_id"]}
        assert {item["revision"] for item in listing.json()} == {first["revision"]}
        bundle = next(item for item in listing.json() if item["kind"] == "production_bundle")
        assert bundle["sha256"] == "b" * 64
        assert bundle["download_path"].startswith(
            f"/v1/releases/{release['release_id']}/artifacts/{bundle['id']}/download?"
        )

        downloaded = client.get(bundle["download_path"], headers=HEADERS)
        assert downloaded.status_code == 200
        assert downloaded.content == b"x" * 128
        assert downloaded.headers["content-disposition"] == (
            f'attachment; filename="custombuild-project-{first["project_id"]}-'
            'release-ARCHIVE-R1-design-review-rev-1.zip"'
        )
        assert downloaded.headers["content-length"] == "128"
        assert downloaded.headers["etag"] == f'"{bundle["sha256"]}"'

        assert (
            client.get(
                f"/v1/releases/{release['release_id']}/artifacts",
                headers=OTHER_TENANT_HEADERS,
            ).status_code
            == 404
        )
        assert client.get(bundle["download_path"], headers=OTHER_TENANT_HEADERS).status_code == 403

        with get_session_factory()() as session:
            stored_release = session.get(Release, str(release["release_id"]))
            stored_job = session.get(GenerationJob, str(job["id"]))
            assert stored_release is not None
            assert stored_job is not None and isinstance(stored_job.result_json, dict)
            assert stored_release.manifest_sha256 == stored_job.result_json["manifest_sha256"]


def test_release_archive_fails_closed_for_every_mutable_binding_and_storage_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        release_payload, job_payload, _first, _current_path = (
            _release_first_revision_and_create_second(
                client,
                name=f"Historical release integrity {uuid4()}",
            )
        )
        release_id = str(release_payload["release_id"])
        job_id = str(job_payload["id"])
        endpoint = f"/v1/releases/{release_id}/artifacts"
        baseline = client.get(endpoint, headers=HEADERS)
        assert baseline.status_code == 200
        bundle = next(item for item in baseline.json() if item["kind"] == "production_bundle")

        with get_session_factory().begin() as session:
            release = session.get(Release, release_id)
            assert release is not None
            original_manifest_sha = release.manifest_sha256
            release.manifest_sha256 = "0" * 64
        assert client.get(endpoint, headers=HEADERS).status_code == 409
        with get_session_factory().begin() as session:
            release = session.get(Release, release_id)
            assert release is not None
            release.manifest_sha256 = original_manifest_sha

        with get_session_factory().begin() as session:
            job = session.get(GenerationJob, job_id)
            assert job is not None and isinstance(job.result_json, dict)
            original_result = deepcopy(job.result_json)
            job.result_json = deepcopy(job.result_json) | {"manifest_sha256": "1" * 64}
            flag_modified(job, "result_json")
        assert client.get(endpoint, headers=HEADERS).status_code == 409
        with get_session_factory().begin() as session:
            job = session.get(GenerationJob, job_id)
            assert job is not None
            job.result_json = original_result
            flag_modified(job, "result_json")

        with get_session_factory().begin() as session:
            artifact = session.get(Artifact, str(bundle["id"]))
            assert artifact is not None
            original_artifact_sha = artifact.sha256
            artifact.sha256 = "2" * 64
        assert client.get(endpoint, headers=HEADERS).status_code == 409
        with get_session_factory().begin() as session:
            artifact = session.get(Artifact, str(bundle["id"]))
            assert artifact is not None
            artifact.sha256 = original_artifact_sha

        with get_session_factory().begin() as session:
            artifact = session.get(Artifact, str(bundle["id"]))
            assert artifact is not None
            stored = session.get(StoredObject, (artifact.organization_id, artifact.object_key))
            assert stored is not None
            original_owner_id = stored.owner_id
            stored.owner_id = str(uuid4())
        assert client.get(endpoint, headers=HEADERS).status_code == 409
        with get_session_factory().begin() as session:
            artifact = session.get(Artifact, str(bundle["id"]))
            assert artifact is not None
            stored = session.get(StoredObject, (artifact.organization_id, artifact.object_key))
            assert stored is not None
            stored.owner_id = original_owner_id

        with get_session_factory().begin() as session:
            job = session.get(GenerationJob, job_id)
            assert job is not None and isinstance(job.result_json, dict)
            job.status = JobStatus.failed
            replacement = GenerationJob(
                organization_id=job.organization_id,
                design_version_id=job.design_version_id,
                status=JobStatus.succeeded,
                idempotency_key=f"archive-sequential-replacement-{uuid4()}",
                production_context_hash=job.production_context_hash,
                production_engine_context_json=deepcopy(job.production_engine_context_json),
                request_json=deepcopy(job.request_json),
                result_json=None,
                attempts=1,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
            session.add(replacement)
            session.flush()
            replacement_result = deepcopy(job.result_json)
            replacement_result["bundle_object_key"] = (
                f"evidence/{replacement.id}/replacement-production-bundle"
            )
            replacement_result["bundle_sha256"] = "c" * 64
            replacement_result["manifest_object_key"] = (
                f"evidence/{replacement.id}/replacement-manifest"
            )
            replacement_evidence = deepcopy(replacement_result["evidence_artifacts"])
            assert isinstance(replacement_evidence, list)
            for item in replacement_evidence:
                assert isinstance(item, dict)
                item["object_key"] = f"evidence/{replacement.id}/{item['kind']}"
            replacement_result["evidence_artifacts"] = replacement_evidence
            assert replacement_result["manifest_sha256"] == job.result_json["manifest_sha256"]
            assert replacement_result["bundle_sha256"] != job.result_json["bundle_sha256"]
            replacement.result_json = replacement_result
            flag_modified(replacement, "result_json")
            replacement_records = [
                {
                    "kind": "production_bundle",
                    "object_key": replacement_result["bundle_object_key"],
                    "sha256": replacement_result["bundle_sha256"],
                    "size_bytes": replacement_result["bundle_size_bytes"],
                    "content_type": "application/zip",
                },
                {
                    "kind": "manifest",
                    "object_key": replacement_result["manifest_object_key"],
                    "sha256": replacement_result["manifest_sha256"],
                    "size_bytes": replacement_result["manifest_size_bytes"],
                    "content_type": "application/json",
                },
                *replacement_evidence,
            ]
            flow._persist_committed_artifact_records(
                session,
                replacement,
                replacement_records,
            )
            replacement_id = replacement.id

        # This is the original P0 reproducer: a sole succeeded replacement has
        # the released manifest but a different bundle. The release must remain
        # bound to the now-failed original job and fail closed, never rebind.
        assert client.get(endpoint, headers=HEADERS).status_code == 409
        with get_session_factory().begin() as session:
            original = session.get(GenerationJob, job_id)
            replacement = session.get(GenerationJob, replacement_id)
            assert original is not None
            assert replacement is not None
            original.status = JobStatus.succeeded

        # Multiple successful jobs with the release manifest remain an
        # integrity error even though one is durably named by the release.
        assert client.get(endpoint, headers=HEADERS).status_code == 409
        with get_session_factory().begin() as session:
            replacement = session.get(GenerationJob, replacement_id)
            assert replacement is not None
            replacement.status = JobStatus.failed

        def unavailable(
            _expectation: storage_module.StoredObjectExpectation,
            *,
            max_bytes: int,
        ) -> bytes:
            del max_bytes
            raise storage_module.ArtifactStorageUnavailableError("provider outage")

        monkeypatch.setattr(api_module, "read_verified_stored_object", unavailable)
        assert client.get(endpoint, headers=HEADERS).status_code == 503
        monkeypatch.setattr(
            api_module,
            "read_verified_stored_object",
            flow._simulated_verified_review_document,
        )

        def unavailable_open(
            _expectation: storage_module.StoredObjectExpectation,
            *,
            max_bytes: int,
        ) -> flow._SimulatedVerifiedDownload:
            del max_bytes
            raise storage_module.ArtifactStorageUnavailableError("provider outage")

        monkeypatch.setattr(api_module, "open_verified_stored_object", unavailable_open)
        assert client.get(bundle["download_path"], headers=HEADERS).status_code == 503


def test_release_archive_rejects_target_swaps_even_with_a_valid_sibling_signature() -> None:
    with TestClient(app) as client:
        release, _job, _first, _current_path = _release_first_revision_and_create_second(
            client,
            name=f"Historical signed URL {uuid4()}",
        )
        listing = client.get(
            f"/v1/releases/{release['release_id']}/artifacts",
            headers=HEADERS,
        )
        assert listing.status_code == 200
        artifacts = listing.json()
        bundle = next(item for item in artifacts if item["kind"] == "production_bundle")
        manifest = next(item for item in artifacts if item["kind"] == "manifest")
        swapped_path = bundle["download_path"].replace(bundle["id"], manifest["id"], 1)

        assert client.get(swapped_path, headers=HEADERS).status_code == 403
