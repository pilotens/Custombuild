from __future__ import annotations

import importlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

MIGRATION_PATH = Path("services/api/alembic/versions/0014_release_generation_binding.py")
ORG_ID = "11111111-1111-4111-8111-111111111111"
VERSION_ID = "22222222-2222-4222-8222-222222222222"
JOB_ID = "33333333-3333-4333-8333-333333333333"
RELEASE_ID = "44444444-4444-4444-8444-444444444444"
REVIEWER_ID = "88888888-8888-4888-8888-888888888888"
APPROVAL_ID = "99999999-9999-4999-8999-999999999999"
REPLACEMENT_JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RELEASE_AUDIT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CAM_AUDIT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
CAM_APPROVED_AT = datetime(2026, 8, 1, 10, tzinfo=UTC)
RELEASED_AT = CAM_APPROVED_AT + timedelta(hours=1)


def _migration() -> Any:
    return importlib.import_module("services.api.alembic.versions.0014_release_generation_binding")


def _fixture_rows() -> tuple[dict[str, Any], dict[str, Any], tuple[dict[str, Any], ...]]:
    result = {
        "bundle_object_key": f"evidence/{JOB_ID}/bundle",
        "bundle_sha256": "b" * 64,
        "bundle_size_bytes": 128,
        "manifest_object_key": f"evidence/{JOB_ID}/manifest",
        "manifest_sha256": "a" * 64,
        "manifest_size_bytes": 256,
        "evidence_artifacts": [
            {
                "kind": "dfm_report",
                "object_key": f"evidence/{JOB_ID}/dfm",
                "sha256": "c" * 64,
                "size_bytes": 64,
                "content_type": "application/json",
            }
        ],
    }
    release = {
        "id": RELEASE_ID,
        "organization_id": ORG_ID,
        "design_version_id": VERSION_ID,
        "release_number": "R1",
        "released_by": REVIEWER_ID,
        "manifest_sha256": "a" * 64,
        "created_at": RELEASED_AT,
    }
    job = {
        "id": JOB_ID,
        "organization_id": ORG_ID,
        "design_version_id": VERSION_ID,
        "status": "succeeded",
        "production_context_hash": "d" * 64,
        "result_json": result,
    }
    artifacts = (
        {
            "id": "55555555-5555-4555-8555-555555555555",
            "organization_id": ORG_ID,
            "generation_job_id": JOB_ID,
            "kind": "dfm_report",
            "object_key": f"evidence/{JOB_ID}/dfm",
            "sha256": "c" * 64,
            "size_bytes": 64,
            "content_type": "application/json",
        },
        {
            "id": "66666666-6666-4666-8666-666666666666",
            "organization_id": ORG_ID,
            "generation_job_id": JOB_ID,
            "kind": "manifest",
            "object_key": f"evidence/{JOB_ID}/manifest",
            "sha256": "a" * 64,
            "size_bytes": 256,
            "content_type": "application/json",
        },
        {
            "id": "77777777-7777-4777-8777-777777777777",
            "organization_id": ORG_ID,
            "generation_job_id": JOB_ID,
            "kind": "production_bundle",
            "object_key": f"evidence/{JOB_ID}/bundle",
            "sha256": "b" * 64,
            "size_bytes": 128,
            "content_type": "application/zip",
        },
    )
    return release, job, artifacts


def test_release_binding_revision_is_fail_closed_and_follows_quota_security() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "0014_release_generation_binding"' in source
    assert 'down_revision = "0013_storage_quota_security_functions"' in source
    assert "RELEASE_BINDING_BACKFILL_FAILED" in source
    assert "design_version.released" in source
    assert "design_version.approved" in source
    assert "payload_json ->> 'approval_type' = 'cam'" in source
    assert "immutable CAM provenance is no longer a successful exact job" in source
    assert "result_json ->> 'manifest_sha256' = :manifest_sha256" not in source
    assert "AND id = :generation_job_id" in source
    assert "generation_job_id IS NULL" in source
    assert source.index("_backfill_release_bindings()") < source.index(
        'op.alter_column("releases", column_name, nullable=False)'
    )
    assert "fk_releases_org_generation_job" in source
    assert 'ondelete="RESTRICT"' in source
    assert "uq_releases_org_generation_job" in source
    assert "json_typeof(generation_result_json) = 'object'" in source
    assert "generation_result_json ->> 'manifest_sha256' = manifest_sha256" in source
    assert "json_array_length(artifact_inventory_json) > 0" in source
    assert "set_config('app.current_organization_id'" in source


def _provenance_rows() -> tuple[
    dict[str, Any],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
]:
    release, job, _ = _fixture_rows()
    release_audits = (
        {
            "id": RELEASE_AUDIT_ID,
            "organization_id": ORG_ID,
            "actor_id": REVIEWER_ID,
            "entity_id": RELEASE_ID,
            "occurred_at": RELEASED_AT,
            "payload_json": {
                "release_number": "R1",
                "manifest_sha256": "a" * 64,
            },
        },
    )
    cam_audits = (
        {
            "id": CAM_AUDIT_ID,
            "organization_id": ORG_ID,
            "actor_id": REVIEWER_ID,
            "entity_id": VERSION_ID,
            "occurred_at": CAM_APPROVED_AT,
            "payload_json": {
                "approval_type": "cam",
                "generation_job_id": JOB_ID,
                "manifest_sha256": "a" * 64,
            },
        },
    )
    approvals = (
        {
            "id": APPROVAL_ID,
            "organization_id": ORG_ID,
            "design_version_id": VERSION_ID,
            "approved_by": REVIEWER_ID,
            "generation_job_id": JOB_ID,
            "production_context_hash": "d" * 64,
            "manifest_sha256": "a" * 64,
        },
    )
    return release, release_audits, cam_audits, approvals, (job,)


def test_release_backfill_resolves_exact_immutable_cam_provenance() -> None:
    migration = _migration()
    release, release_audits, cam_audits, approvals, jobs = _provenance_rows()

    release_audit = migration._validated_release_audit(release, release_audits)
    resolved = migration._validated_provenance_job(
        release,
        cam_audits,
        approvals,
        jobs,
    )

    assert release_audit["id"] == RELEASE_AUDIT_ID
    assert resolved["id"] == JOB_ID


def test_release_backfill_rejects_replacement_when_original_provenance_job_failed() -> None:
    migration = _migration()
    release, _, cam_audits, approvals, jobs = _provenance_rows()
    original = deepcopy(jobs[0])
    original["status"] = "failed"
    replacement = deepcopy(original)
    replacement.update(
        {
            "id": REPLACEMENT_JOB_ID,
            "status": "succeeded",
            "result_json": {
                **deepcopy(original["result_json"]),
                "bundle_object_key": f"evidence/{REPLACEMENT_JOB_ID}/bundle",
                "bundle_sha256": "e" * 64,
            },
        }
    )

    with pytest.raises(
        RuntimeError,
        match="immutable CAM provenance is no longer a successful exact job",
    ):
        migration._validated_provenance_job(
            release,
            cam_audits,
            approvals,
            (original, replacement),
        )


def test_release_backfill_rejects_missing_or_ambiguous_audit_provenance() -> None:
    migration = _migration()
    release, release_audits, cam_audits, approvals, jobs = _provenance_rows()

    with pytest.raises(RuntimeError, match="immutable release audit records"):
        migration._validated_release_audit(release, ())
    with pytest.raises(RuntimeError, match="no immutable CAM approval provenance"):
        migration._validated_provenance_job(release, (), approvals, jobs)
    with pytest.raises(RuntimeError, match="not bound to one CAM approval"):
        migration._validated_provenance_job(release, cam_audits, (), jobs)

    conflicting_audit = deepcopy(cam_audits[0])
    conflicting_audit["id"] = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    conflicting_audit["payload_json"]["generation_job_id"] = REPLACEMENT_JOB_ID
    with pytest.raises(RuntimeError, match="ambiguous CAM approval provenance"):
        migration._validated_provenance_job(
            release,
            (cam_audits[0], conflicting_audit),
            approvals,
            jobs,
        )


@pytest.mark.parametrize(
    ("target", "field", "replacement"),
    (
        ("release_audit", "actor_id", APPROVAL_ID),
        ("cam_audit", "generation_job_id", REPLACEMENT_JOB_ID),
        ("approval", "generation_job_id", REPLACEMENT_JOB_ID),
        ("approval", "production_context_hash", "e" * 64),
    ),
)
def test_release_backfill_rejects_missing_or_drifted_provenance(
    target: str,
    field: str,
    replacement: str,
) -> None:
    migration = _migration()
    release, release_audits, cam_audits, approvals, jobs = _provenance_rows()
    changed_release_audits = deepcopy(release_audits)
    changed_cam_audits = deepcopy(cam_audits)
    changed_approvals = deepcopy(approvals)
    if target == "release_audit":
        changed_release_audits[0][field] = replacement
        with pytest.raises(RuntimeError, match="RELEASE_BINDING_BACKFILL_FAILED"):
            migration._validated_release_audit(release, changed_release_audits)
        return
    if target == "cam_audit":
        changed_cam_audits[0]["payload_json"][field] = replacement
    else:
        changed_approvals[0][field] = replacement

    with pytest.raises(RuntimeError, match="RELEASE_BINDING_BACKFILL_FAILED"):
        migration._validated_provenance_job(
            release,
            changed_cam_audits,
            changed_approvals,
            jobs,
        )


def test_release_backfill_freezes_exact_job_and_artifact_row_identity() -> None:
    migration = _migration()
    release, job, artifacts = _fixture_rows()

    inventory = migration._validated_inventory(release, job, artifacts)

    assert [entry["kind"] for entry in inventory] == [
        "dfm_report",
        "manifest",
        "production_bundle",
    ]
    assert inventory[1] == {
        "artifact_id": "66666666-6666-4666-8666-666666666666",
        "kind": "manifest",
        "object_key": f"evidence/{JOB_ID}/manifest",
        "sha256": "a" * 64,
        "size_bytes": 256,
        "content_type": "application/json",
    }


@pytest.mark.parametrize(
    ("target", "field", "replacement"),
    (
        ("release", "manifest_sha256", "f" * 64),
        ("job", "organization_id", "99999999-9999-4999-8999-999999999999"),
        ("artifact", "sha256", "e" * 64),
        ("artifact", "generation_job_id", "99999999-9999-4999-8999-999999999999"),
    ),
)
def test_release_backfill_rejects_unbound_or_drifted_legacy_rows(
    target: str,
    field: str,
    replacement: str,
) -> None:
    migration = _migration()
    release, job, artifacts = _fixture_rows()
    changed_release = deepcopy(release)
    changed_job = deepcopy(job)
    changed_artifacts = deepcopy(artifacts)
    if target == "release":
        changed_release[field] = replacement
    elif target == "job":
        changed_job[field] = replacement
    else:
        changed_artifacts[0][field] = replacement

    with pytest.raises(RuntimeError, match="RELEASE_BINDING_BACKFILL_FAILED"):
        migration._validated_inventory(
            changed_release,
            changed_job,
            tuple(changed_artifacts),
        )


def test_release_backfill_rejects_duplicate_or_incomplete_result_inventory() -> None:
    migration = _migration()
    release, job, artifacts = _fixture_rows()
    duplicated_job = deepcopy(job)
    duplicated_job["result_json"]["evidence_artifacts"].append(
        deepcopy(duplicated_job["result_json"]["evidence_artifacts"][0])
    )

    with pytest.raises(RuntimeError, match="RELEASE_BINDING_BACKFILL_FAILED"):
        migration._validated_inventory(release, duplicated_job, artifacts)

    with pytest.raises(RuntimeError, match="RELEASE_BINDING_BACKFILL_FAILED"):
        migration._validated_inventory(release, job, artifacts[:-1])
