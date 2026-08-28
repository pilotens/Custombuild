"""Bind every release to one immutable generation result and artifact inventory.

Revision ID: 0014_release_generation_binding
Revises: 0013_storage_quota_security_functions
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import RowMapping

revision = "0014_release_generation_binding"
down_revision = "0013_storage_quota_security_functions"
branch_labels = None
depends_on = None

_UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ARTIFACTS = 256
_MAX_ARTIFACT_BYTES = 10 * 1024**3

_RELEASE_ROWS = sa.text(
    """
    SELECT id, organization_id, design_version_id, release_number, released_by,
           manifest_sha256, created_at
    FROM releases
    WHERE organization_id = :organization_id
    ORDER BY id
    """
)
_RELEASE_AUDIT_ROWS = sa.text(
    """
    SELECT id, organization_id, actor_id, entity_id, occurred_at, payload_json
    FROM audit_events
    WHERE organization_id = :organization_id
      AND action = 'design_version.released'
      AND entity_type = 'release'
      AND entity_id = :release_id
    ORDER BY occurred_at, id
    """
)
_CAM_AUDIT_ROWS = sa.text(
    """
    SELECT id, organization_id, actor_id, entity_id, occurred_at, payload_json
    FROM audit_events
    WHERE organization_id = :organization_id
      AND action = 'design_version.approved'
      AND entity_type = 'design_version'
      AND entity_id = :design_version_id
      AND payload_json ->> 'approval_type' = 'cam'
      AND occurred_at <= :released_at
    ORDER BY occurred_at DESC, id DESC
    """
)
_CAM_APPROVAL_ROWS = sa.text(
    """
    SELECT id, organization_id, design_version_id, approved_by,
           generation_job_id, production_context_hash, manifest_sha256
    FROM approvals
    WHERE organization_id = :organization_id
      AND design_version_id = :design_version_id
      AND approval_type = 'cam'
    ORDER BY id
    """
)
# Deliberately address one audit-bound job by identity.  Searching the current
# succeeded set by manifest would let a later replacement masquerade as the job
# that was actually CAM-approved and released.
_JOB_ROWS = sa.text(
    """
    SELECT id, organization_id, design_version_id, status,
           production_context_hash, result_json
    FROM generation_jobs
    WHERE organization_id = :organization_id
      AND design_version_id = :design_version_id
      AND id = :generation_job_id
    ORDER BY id
    """
)
_ARTIFACT_ROWS = sa.text(
    """
    SELECT id, organization_id, generation_job_id, kind, object_key,
           sha256, size_bytes, content_type
    FROM artifacts
    WHERE organization_id = :organization_id
      AND generation_job_id = :generation_job_id
    ORDER BY kind, id
    """
)


def _canonical_text(name: str, value: object, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or "\\" in value
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise RuntimeError(f"RELEASE_BINDING_BACKFILL_FAILED: {name} is not canonical")
    return value


def _canonical_uuid(name: str, value: object) -> str:
    text_value = _canonical_text(name, value, maximum=36)
    if _UUID_PATTERN.fullmatch(text_value) is None:
        raise RuntimeError(f"RELEASE_BINDING_BACKFILL_FAILED: {name} is not a canonical UUID")
    return text_value


def _canonical_sha256(name: str, value: object) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise RuntimeError(f"RELEASE_BINDING_BACKFILL_FAILED: {name} is not canonical SHA-256")
    return value


def _validated_release_audit(
    release: RowMapping,
    release_audits: tuple[RowMapping, ...],
) -> RowMapping:
    release_id = _canonical_uuid("release id", release["id"])
    organization_id = _canonical_uuid("organization_id", release["organization_id"])
    released_by = _canonical_uuid("released_by", release["released_by"])
    release_number = _canonical_text("release_number", release["release_number"], maximum=80)
    manifest_sha256 = _canonical_sha256("release manifest_sha256", release["manifest_sha256"])
    if len(release_audits) != 1:
        raise RuntimeError(
            "RELEASE_BINDING_BACKFILL_FAILED: release "
            f"{release_id} has {len(release_audits)} immutable release audit records"
        )
    audit = release_audits[0]
    payload = audit["payload_json"]
    if (
        audit["organization_id"] != organization_id
        or audit["entity_id"] != release_id
        or audit["actor_id"] != released_by
        or audit["occurred_at"] is None
        or not isinstance(payload, Mapping)
        or payload.get("release_number") != release_number
        or payload.get("manifest_sha256") != manifest_sha256
    ):
        raise RuntimeError(
            "RELEASE_BINDING_BACKFILL_FAILED: release "
            f"{release_id} does not match its immutable release audit"
        )
    return audit


def _validated_provenance_job(
    release: RowMapping,
    cam_audits: tuple[RowMapping, ...],
    approvals: tuple[RowMapping, ...],
    jobs: tuple[RowMapping, ...],
) -> RowMapping:
    """Resolve only the job recorded by pre-release, append-only CAM provenance."""

    release_id = _canonical_uuid("release id", release["id"])
    organization_id = _canonical_uuid("organization_id", release["organization_id"])
    design_version_id = _canonical_uuid("design_version_id", release["design_version_id"])
    manifest_sha256 = _canonical_sha256("release manifest_sha256", release["manifest_sha256"])
    if not cam_audits:
        raise RuntimeError(
            "RELEASE_BINDING_BACKFILL_FAILED: release "
            f"{release_id} has no immutable CAM approval provenance"
        )
    latest_occurred_at = cam_audits[0]["occurred_at"]
    if latest_occurred_at is None:
        raise RuntimeError(
            "RELEASE_BINDING_BACKFILL_FAILED: release "
            f"{release_id} has malformed CAM approval provenance"
        )
    latest_audits = tuple(
        audit for audit in cam_audits if audit["occurred_at"] == latest_occurred_at
    )
    audit_bindings: set[tuple[str, str, str]] = set()
    for audit in latest_audits:
        payload = audit["payload_json"]
        if (
            audit["organization_id"] != organization_id
            or audit["entity_id"] != design_version_id
            or not isinstance(payload, Mapping)
            or payload.get("approval_type") != "cam"
        ):
            raise RuntimeError(
                "RELEASE_BINDING_BACKFILL_FAILED: release "
                f"{release_id} has malformed CAM approval provenance"
            )
        audit_bindings.add(
            (
                _canonical_uuid("CAM audit actor_id", audit["actor_id"]),
                _canonical_uuid("CAM audit generation_job_id", payload.get("generation_job_id")),
                _canonical_sha256("CAM audit manifest_sha256", payload.get("manifest_sha256")),
            )
        )
    if len(audit_bindings) != 1:
        raise RuntimeError(
            "RELEASE_BINDING_BACKFILL_FAILED: release "
            f"{release_id} has ambiguous CAM approval provenance"
        )
    audit_actor_id, generation_job_id, audit_manifest_sha256 = next(iter(audit_bindings))
    if audit_manifest_sha256 != manifest_sha256 or len(approvals) != 1:
        raise RuntimeError(
            "RELEASE_BINDING_BACKFILL_FAILED: release "
            f"{release_id} is not bound to one CAM approval"
        )

    approval = approvals[0]
    approval_id = _canonical_uuid("CAM approval id", approval["id"])
    approval_context_hash = _canonical_sha256(
        "CAM approval production_context_hash", approval["production_context_hash"]
    )
    if (
        approval["organization_id"] != organization_id
        or approval["design_version_id"] != design_version_id
        or approval["id"] != approval_id
        or approval["approved_by"] != audit_actor_id
        or approval["generation_job_id"] != generation_job_id
        or approval["manifest_sha256"] != manifest_sha256
    ):
        raise RuntimeError(
            "RELEASE_BINDING_BACKFILL_FAILED: release "
            f"{release_id} CAM approval drifted from immutable provenance"
        )

    matching_jobs = tuple(job for job in jobs if job["id"] == generation_job_id)
    if len(matching_jobs) != 1:
        raise RuntimeError(
            "RELEASE_BINDING_BACKFILL_FAILED: release "
            f"{release_id} provenance resolves to {len(matching_jobs)} jobs"
        )
    job = matching_jobs[0]
    result = job["result_json"]
    if (
        job["organization_id"] != organization_id
        or job["design_version_id"] != design_version_id
        or job["status"] != "succeeded"
        or job["production_context_hash"] != approval_context_hash
        or not isinstance(result, Mapping)
        or result.get("manifest_sha256") != manifest_sha256
    ):
        raise RuntimeError(
            "RELEASE_BINDING_BACKFILL_FAILED: release "
            f"{release_id} immutable CAM provenance is no longer a successful exact job"
        )
    return job


def _expected_inventory(result: object) -> dict[str, dict[str, Any]]:
    if not isinstance(result, Mapping):
        raise RuntimeError("RELEASE_BINDING_BACKFILL_FAILED: generation result is not an object")
    expectations: dict[str, dict[str, Any]] = {}
    object_keys: set[str] = set()
    normalized_kinds: set[str] = set()

    def add(
        kind: object,
        object_key: object,
        sha256: object,
        size_bytes: object,
        content_type: object,
    ) -> None:
        canonical_kind = _canonical_text("artifact kind", kind, maximum=80)
        normalized_kind = canonical_kind.casefold()
        canonical_key = _canonical_text("artifact object_key", object_key, maximum=512)
        canonical_sha = _canonical_sha256("artifact sha256", sha256)
        canonical_type = _canonical_text("artifact content_type", content_type, maximum=160)
        if (
            normalized_kind in normalized_kinds
            or canonical_key in object_keys
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes <= 0
            or size_bytes > _MAX_ARTIFACT_BYTES
        ):
            raise RuntimeError("RELEASE_BINDING_BACKFILL_FAILED: artifact inventory is ambiguous")
        normalized_kinds.add(normalized_kind)
        object_keys.add(canonical_key)
        expectations[canonical_kind] = {
            "kind": canonical_kind,
            "object_key": canonical_key,
            "sha256": canonical_sha,
            "size_bytes": size_bytes,
            "content_type": canonical_type,
        }

    add(
        "production_bundle",
        result.get("bundle_object_key"),
        result.get("bundle_sha256"),
        result.get("bundle_size_bytes"),
        "application/zip",
    )
    add(
        "manifest",
        result.get("manifest_object_key"),
        result.get("manifest_sha256"),
        result.get("manifest_size_bytes"),
        "application/json",
    )
    evidence = result.get("evidence_artifacts")
    if (
        not isinstance(evidence, list)
        or len(evidence) > _MAX_ARTIFACTS - 2
        or any(not isinstance(item, Mapping) for item in evidence)
    ):
        raise RuntimeError("RELEASE_BINDING_BACKFILL_FAILED: evidence inventory is malformed")
    for item in evidence:
        add(
            item.get("kind"),
            item.get("object_key"),
            item.get("sha256"),
            item.get("size_bytes"),
            item.get("content_type"),
        )
    return expectations


def _validated_inventory(
    release: RowMapping,
    job: RowMapping,
    artifacts: tuple[RowMapping, ...],
) -> list[dict[str, Any]]:
    organization_id = _canonical_uuid("organization_id", release["organization_id"])
    release_id = _canonical_uuid("release id", release["id"])
    design_version_id = _canonical_uuid("design_version_id", release["design_version_id"])
    manifest_sha256 = _canonical_sha256("release manifest_sha256", release["manifest_sha256"])
    job_id = _canonical_uuid("generation_job_id", job["id"])
    if job["organization_id"] != organization_id or job["design_version_id"] != design_version_id:
        raise RuntimeError(
            f"RELEASE_BINDING_BACKFILL_FAILED: release {release_id} crossed its tenant graph"
        )
    result = job["result_json"]
    expected = _expected_inventory(result)
    if not isinstance(result, Mapping) or result.get("manifest_sha256") != manifest_sha256:
        raise RuntimeError(
            f"RELEASE_BINDING_BACKFILL_FAILED: release {release_id} manifest is unbound"
        )
    if len(artifacts) != len(expected):
        raise RuntimeError(
            f"RELEASE_BINDING_BACKFILL_FAILED: release {release_id} inventory is incomplete"
        )

    inventory: list[dict[str, Any]] = []
    seen_kinds: set[str] = set()
    for artifact in artifacts:
        artifact_id = _canonical_uuid("artifact id", artifact["id"])
        kind = _canonical_text("artifact kind", artifact["kind"], maximum=80)
        expectation = expected.get(kind)
        if (
            expectation is None
            or kind in seen_kinds
            or artifact["organization_id"] != organization_id
            or artifact["generation_job_id"] != job_id
        ):
            raise RuntimeError(
                f"RELEASE_BINDING_BACKFILL_FAILED: release {release_id} inventory is ambiguous"
            )
        actual = {
            "artifact_id": artifact_id,
            "kind": kind,
            "object_key": _canonical_text(
                "artifact object_key", artifact["object_key"], maximum=512
            ),
            "sha256": _canonical_sha256("artifact sha256", artifact["sha256"]),
            "size_bytes": artifact["size_bytes"],
            "content_type": _canonical_text(
                "artifact content_type", artifact["content_type"], maximum=160
            ),
        }
        if {key: actual[key] for key in expectation} != expectation:
            raise RuntimeError(
                f"RELEASE_BINDING_BACKFILL_FAILED: release {release_id} artifact row drifted"
            )
        seen_kinds.add(kind)
        inventory.append(actual)
    if seen_kinds != set(expected):
        raise RuntimeError(
            f"RELEASE_BINDING_BACKFILL_FAILED: release {release_id} inventory is incomplete"
        )
    return inventory


def _backfill_release_bindings() -> None:
    bind = op.get_bind()
    organization_ids = tuple(
        str(value)
        for value in bind.execute(sa.text("SELECT id FROM organizations ORDER BY id")).scalars()
    )
    try:
        for organization_id in organization_ids:
            bind.execute(
                sa.text("SELECT set_config('app.current_organization_id', :organization_id, true)"),
                {"organization_id": organization_id},
            )
            releases = tuple(
                bind.execute(
                    _RELEASE_ROWS,
                    {"organization_id": organization_id},
                ).mappings()
            )
            for release in releases:
                release_audits = tuple(
                    bind.execute(
                        _RELEASE_AUDIT_ROWS,
                        {
                            "organization_id": organization_id,
                            "release_id": release["id"],
                        },
                    ).mappings()
                )
                release_audit = _validated_release_audit(release, release_audits)
                provenance_parameters = {
                    "organization_id": organization_id,
                    "design_version_id": release["design_version_id"],
                }
                cam_audits = tuple(
                    bind.execute(
                        _CAM_AUDIT_ROWS,
                        {
                            **provenance_parameters,
                            "released_at": release_audit["occurred_at"],
                        },
                    ).mappings()
                )
                approvals = tuple(
                    bind.execute(
                        _CAM_APPROVAL_ROWS,
                        provenance_parameters,
                    ).mappings()
                )
                latest_cam_payload = cam_audits[0]["payload_json"] if cam_audits else None
                provenance_job_id = (
                    latest_cam_payload.get("generation_job_id")
                    if isinstance(latest_cam_payload, Mapping)
                    else None
                )
                jobs = tuple(
                    bind.execute(
                        _JOB_ROWS,
                        {
                            **provenance_parameters,
                            "generation_job_id": provenance_job_id,
                        },
                    ).mappings()
                )
                job = _validated_provenance_job(
                    release,
                    cam_audits,
                    approvals,
                    jobs,
                )
                artifacts = tuple(
                    bind.execute(
                        _ARTIFACT_ROWS,
                        {
                            "organization_id": organization_id,
                            "generation_job_id": job["id"],
                        },
                    ).mappings()
                )
                inventory = _validated_inventory(release, job, artifacts)
                production_context_hash = _canonical_sha256(
                    "production_context_hash", job["production_context_hash"]
                )
                result = job["result_json"]
                updated = bind.execute(
                    sa.text(
                        """
                        UPDATE releases
                        SET generation_job_id = :generation_job_id,
                            production_context_hash = :production_context_hash,
                            generation_result_json = CAST(:generation_result_json AS json),
                            artifact_inventory_json = CAST(:artifact_inventory_json AS json)
                        WHERE id = :release_id
                          AND organization_id = :organization_id
                          AND design_version_id = :design_version_id
                          AND manifest_sha256 = :manifest_sha256
                          AND generation_job_id IS NULL
                        """
                    ),
                    {
                        "generation_job_id": job["id"],
                        "production_context_hash": production_context_hash,
                        "generation_result_json": json.dumps(
                            result,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        "artifact_inventory_json": json.dumps(
                            inventory,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        "release_id": release["id"],
                        "organization_id": organization_id,
                        "design_version_id": release["design_version_id"],
                        "manifest_sha256": release["manifest_sha256"],
                    },
                )
                if updated.rowcount != 1:
                    raise RuntimeError(
                        "RELEASE_BINDING_BACKFILL_FAILED: release changed during backfill"
                    )
    finally:
        bind.execute(sa.text("SELECT set_config('app.current_organization_id', '', true)"))


def upgrade() -> None:
    op.add_column(
        "releases",
        sa.Column("generation_job_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "releases",
        sa.Column("production_context_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "releases",
        sa.Column("generation_result_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "releases",
        sa.Column("artifact_inventory_json", sa.JSON(), nullable=True),
    )

    _backfill_release_bindings()

    for column_name in (
        "generation_job_id",
        "production_context_hash",
        "generation_result_json",
        "artifact_inventory_json",
    ):
        op.alter_column("releases", column_name, nullable=False)
    op.create_unique_constraint(
        "uq_releases_org_generation_job",
        "releases",
        ["organization_id", "generation_job_id"],
    )
    op.create_foreign_key(
        "fk_releases_org_generation_job",
        "releases",
        "generation_jobs",
        ["organization_id", "generation_job_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_releases_generation_job_uuid",
        "releases",
        "generation_job_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'",
    )
    op.create_check_constraint(
        "ck_releases_production_context_hash",
        "releases",
        "production_context_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_releases_generation_result_object",
        "releases",
        "json_typeof(generation_result_json) = 'object' "
        "AND generation_result_json ->> 'manifest_sha256' = manifest_sha256",
    )
    op.create_check_constraint(
        "ck_releases_artifact_inventory_array",
        "releases",
        "json_typeof(artifact_inventory_json) = 'array' "
        "AND json_array_length(artifact_inventory_json) > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_releases_artifact_inventory_array",
        "releases",
        type_="check",
    )
    op.drop_constraint(
        "ck_releases_generation_result_object",
        "releases",
        type_="check",
    )
    op.drop_constraint(
        "ck_releases_production_context_hash",
        "releases",
        type_="check",
    )
    op.drop_constraint(
        "ck_releases_generation_job_uuid",
        "releases",
        type_="check",
    )
    op.drop_constraint(
        "fk_releases_org_generation_job",
        "releases",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_releases_org_generation_job",
        "releases",
        type_="unique",
    )
    op.drop_column("releases", "artifact_inventory_json")
    op.drop_column("releases", "generation_result_json")
    op.drop_column("releases", "production_context_hash")
    op.drop_column("releases", "generation_job_id")
