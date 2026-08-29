"""Add durable, tenant-bound object storage quota accounting.

Revision ID: 0012_storage_quota_ledger
Revises: 0011_runtime_role_privileges
"""

from __future__ import annotations

import re
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import RowMapping

revision = "0012_storage_quota_ledger"
down_revision = "0011_runtime_role_privileges"
branch_labels = None
depends_on = None

TENANT_STORAGE_BYTE_LIMIT = 10 * 1024**3
TENANT_STORAGE_OBJECT_LIMIT = 100_000
MAX_DATABASE_INTEGER = 2**63 - 1

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_LEGACY_OBJECTS = sa.text(
    """
    SELECT
        imported.organization_id,
        imported.project_id,
        imported.object_key,
        imported.sha256,
        imported.size_bytes,
        imported.media_type,
        'imported_asset' AS owner_type,
        imported.id AS owner_id,
        'imported:' || imported.id AS idempotency_key
    FROM imported_assets AS imported
    WHERE imported.organization_id = :organization_id
    UNION ALL
    SELECT
        evidence.organization_id,
        evidence.project_id,
        evidence.object_key,
        evidence.sha256,
        evidence.size_bytes,
        evidence.content_type AS media_type,
        'external_evidence' AS owner_type,
        evidence.id AS owner_id,
        'external-evidence:' || evidence.id AS idempotency_key
    FROM external_evidence AS evidence
    WHERE evidence.organization_id = :organization_id
    UNION ALL
    SELECT
        artifact.organization_id,
        version.project_id,
        artifact.object_key,
        artifact.sha256,
        artifact.size_bytes,
        artifact.content_type AS media_type,
        'generation_job' AS owner_type,
        job.id AS owner_id,
        'generation:' || job.id || ':' || artifact.kind || ':' || artifact.id
            AS idempotency_key
    FROM artifacts AS artifact
    LEFT JOIN generation_jobs AS job
      ON job.organization_id = artifact.organization_id
     AND job.id = artifact.generation_job_id
    LEFT JOIN design_versions AS version
      ON version.organization_id = job.organization_id
     AND version.id = job.design_version_id
    WHERE artifact.organization_id = :organization_id
    ORDER BY object_key, owner_type, owner_id
    """
)


def _canonical_text(name: str, value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise RuntimeError(f"STORAGE_QUOTA_BACKFILL_FAILED: {name} is missing or exceeds {maximum}")
    if (
        value != value.strip()
        or "\\" in value
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise RuntimeError(f"STORAGE_QUOTA_BACKFILL_FAILED: {name} is not canonical")
    return value


def _validated_legacy_row(
    organization_id: str,
    row: RowMapping,
) -> dict[str, Any]:
    row_organization_id = _canonical_text("organization_id", row["organization_id"], maximum=36)
    if row_organization_id != organization_id:
        raise RuntimeError("STORAGE_QUOTA_BACKFILL_FAILED: legacy row crossed its tenant context")
    project_id = _canonical_text("project_id", row["project_id"], maximum=36)
    object_key = _canonical_text("object_key", row["object_key"], maximum=512)
    sha256 = row["sha256"]
    if not isinstance(sha256, str) or _SHA256_PATTERN.fullmatch(sha256) is None:
        raise RuntimeError("STORAGE_QUOTA_BACKFILL_FAILED: legacy sha256 is not canonical")
    size_bytes = row["size_bytes"]
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
        raise RuntimeError("STORAGE_QUOTA_BACKFILL_FAILED: legacy size_bytes must be positive")
    media_type = _canonical_text("media_type", row["media_type"], maximum=160)
    owner_type = _canonical_text("owner_type", row["owner_type"], maximum=40)
    owner_id = _canonical_text("owner_id", row["owner_id"], maximum=36)
    idempotency_key = _canonical_text("idempotency_key", row["idempotency_key"], maximum=512)
    return {
        "organization_id": row_organization_id,
        "project_id": project_id,
        "object_key": object_key,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "media_type": media_type,
        "owner_type": owner_type,
        "owner_id": owner_id,
        "idempotency_key": idempotency_key,
    }


def _backfill_existing_objects() -> None:
    bind = op.get_bind()
    organization_ids = tuple(
        str(value)
        for value in bind.execute(sa.text("SELECT id FROM organizations ORDER BY id")).scalars()
    )
    global_bytes = 0
    global_count = 0
    global_object_keys: set[str] = set()
    try:
        for organization_id in organization_ids:
            bind.execute(
                sa.text("SELECT set_config('app.current_organization_id', :organization_id, true)"),
                {"organization_id": organization_id},
            )
            rows = tuple(
                _validated_legacy_row(organization_id, row)
                for row in bind.execute(
                    _LEGACY_OBJECTS,
                    {"organization_id": organization_id},
                ).mappings()
            )
            object_keys: set[str] = set()
            idempotency_keys: set[str] = set()
            for row in rows:
                object_key = str(row["object_key"])
                idempotency_key = str(row["idempotency_key"])
                if object_key in object_keys:
                    raise RuntimeError(
                        "STORAGE_QUOTA_BACKFILL_FAILED: duplicate legacy object_key "
                        f"for organization {organization_id}"
                    )
                if object_key in global_object_keys:
                    raise RuntimeError(
                        "STORAGE_QUOTA_BACKFILL_FAILED: duplicate physical legacy "
                        f"object_key across organizations: {object_key}"
                    )
                if idempotency_key in idempotency_keys:
                    raise RuntimeError(
                        "STORAGE_QUOTA_BACKFILL_FAILED: duplicate legacy idempotency_key "
                        f"for organization {organization_id}"
                    )
                object_keys.add(object_key)
                global_object_keys.add(object_key)
                idempotency_keys.add(idempotency_key)

            tenant_bytes = sum(int(row["size_bytes"]) for row in rows)
            tenant_count = len(rows)
            if (
                tenant_bytes > TENANT_STORAGE_BYTE_LIMIT
                or tenant_count > TENANT_STORAGE_OBJECT_LIMIT
            ):
                raise RuntimeError(
                    "STORAGE_QUOTA_BACKFILL_FAILED: existing tenant objects exceed "
                    f"the canonical quota for organization {organization_id}"
                )
            if rows:
                bind.execute(
                    sa.text(
                        """
                        INSERT INTO stored_objects (
                            organization_id, object_key, project_id, sha256,
                            size_bytes, media_type, owner_type, owner_id,
                            idempotency_key, state, lease_token,
                            lease_expires_at, claim_token, claim_expires_at,
                            created_at, updated_at
                        ) VALUES (
                            :organization_id, :object_key, :project_id, :sha256,
                            :size_bytes, :media_type, :owner_type, :owner_id,
                            :idempotency_key, 'committed', NULL, NULL, NULL, NULL,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    rows,
                )
            bind.execute(
                sa.text(
                    """
                    INSERT INTO storage_tenant_quotas (
                        organization_id, byte_limit, object_limit,
                        reserved_bytes, committed_bytes,
                        reserved_count, committed_count,
                        created_at, updated_at
                    ) VALUES (
                        :organization_id, :byte_limit, :object_limit,
                        0, :committed_bytes, 0, :committed_count,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "organization_id": organization_id,
                    "byte_limit": TENANT_STORAGE_BYTE_LIMIT,
                    "object_limit": TENANT_STORAGE_OBJECT_LIMIT,
                    "committed_bytes": tenant_bytes,
                    "committed_count": tenant_count,
                },
            )
            global_bytes += tenant_bytes
            global_count += tenant_count
    finally:
        bind.execute(sa.text("SELECT set_config('app.current_organization_id', '', true)"))

    if global_bytes > MAX_DATABASE_INTEGER or global_count > MAX_DATABASE_INTEGER:
        raise RuntimeError(
            "STORAGE_QUOTA_BACKFILL_FAILED: existing objects exceed database counters"
        )
    bind.execute(
        sa.text(
            """
            INSERT INTO storage_global_quotas (
                id, byte_limit, object_limit, reserved_bytes, committed_bytes,
                reserved_count, committed_count, capacity_verified,
                maintenance_epoch, recovery_database_started_at,
                recovery_completed_at,
                created_at, updated_at
            ) VALUES (
                1, :byte_limit, :object_limit, 0, :committed_bytes,
                0, :committed_count, false, 0, pg_postmaster_start_time(),
                clock_timestamp(), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            # A migration cannot attest its target volume.  Bootstrap limits
            # preserve the existing ledger but permit no additional capacity;
            # the privileged physical-capacity preflight replaces them.
            "byte_limit": max(global_bytes, 1),
            "object_limit": max(global_count, 1),
            "committed_bytes": global_bytes,
            "committed_count": global_count,
        },
    )


def _create_rls_policy(table_name: str, policy_name: str) -> None:
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {policy_name} ON {table_name} "
        "USING (organization_id = "
        "current_setting('app.current_organization_id', true)) "
        "WITH CHECK (organization_id = "
        "current_setting('app.current_organization_id', true))"
    )


def upgrade() -> None:
    op.create_table(
        "storage_global_quotas",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("byte_limit", sa.BigInteger(), nullable=False),
        sa.Column("object_limit", sa.BigInteger(), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("committed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("reserved_count", sa.BigInteger(), nullable=False),
        sa.Column("committed_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "capacity_verified",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("provisioned_bytes", sa.BigInteger(), nullable=True),
        sa.Column("metadata_overhead_bytes", sa.BigInteger(), nullable=True),
        sa.Column("emergency_reserve_bytes", sa.BigInteger(), nullable=True),
        sa.Column("capacity_headroom_bytes", sa.BigInteger(), nullable=True),
        sa.Column("volume_identity", sa.String(length=255), nullable=True),
        sa.Column("capacity_bucket", sa.String(length=63), nullable=True),
        sa.Column(
            "capacity_operator_config_sha256",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("deploy_descriptor_sha256", sa.String(length=64), nullable=True),
        sa.Column("inventory_sha256", sa.String(length=64), nullable=True),
        sa.Column("inventory_object_count", sa.BigInteger(), nullable=True),
        sa.Column("inventory_bytes", sa.BigInteger(), nullable=True),
        sa.Column("ledger_object_count", sa.BigInteger(), nullable=True),
        sa.Column("ledger_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "capacity_attested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "capacity_verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("capacity_evidence_sha256", sa.String(length=64), nullable=True),
        sa.Column("maintenance_token", sa.String(length=36), nullable=True),
        sa.Column(
            "maintenance_epoch",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "maintenance_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "maintenance_owner_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "maintenance_database_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "recovery_database_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "recovery_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_storage_global_quota_singleton"),
        sa.CheckConstraint(
            "byte_limit > 0 AND object_limit > 0",
            name="ck_storage_global_quota_positive_limits",
        ),
        sa.CheckConstraint(
            "reserved_bytes >= 0 AND committed_bytes >= 0 "
            "AND reserved_count >= 0 AND committed_count >= 0",
            name="ck_storage_global_quota_nonnegative_counters",
        ),
        sa.CheckConstraint(
            "reserved_bytes <= byte_limit - committed_bytes "
            "AND reserved_count <= object_limit - committed_count",
            name="ck_storage_global_quota_within_limits",
        ),
        sa.CheckConstraint(
            "capacity_verified = false OR ("
            "provisioned_bytes IS NOT NULL AND provisioned_bytes > 0 "
            "AND metadata_overhead_bytes IS NOT NULL AND metadata_overhead_bytes > 0 "
            "AND emergency_reserve_bytes IS NOT NULL AND emergency_reserve_bytes > 0 "
            "AND capacity_headroom_bytes IS NOT NULL "
            "AND capacity_headroom_bytes = metadata_overhead_bytes + emergency_reserve_bytes "
            "AND capacity_headroom_bytes < provisioned_bytes "
            "AND byte_limit <= provisioned_bytes - capacity_headroom_bytes "
            "AND volume_identity IS NOT NULL AND length(volume_identity) > 0 "
            "AND capacity_bucket IS NOT NULL AND length(capacity_bucket) > 0 "
            "AND capacity_operator_config_sha256 ~ '^[0-9a-f]{64}$' "
            "AND deploy_descriptor_sha256 ~ '^[0-9a-f]{64}$' "
            "AND inventory_sha256 ~ '^[0-9a-f]{64}$' "
            "AND inventory_object_count IS NOT NULL AND inventory_object_count >= 0 "
            "AND inventory_bytes IS NOT NULL AND inventory_bytes >= 0 "
            "AND ledger_object_count IS NOT NULL AND ledger_object_count >= 0 "
            "AND ledger_bytes IS NOT NULL AND ledger_bytes >= 0 "
            "AND capacity_attested_at IS NOT NULL "
            "AND capacity_verified_at IS NOT NULL "
            "AND capacity_evidence_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_storage_global_quota_verified_capacity",
        ),
        sa.CheckConstraint(
            "maintenance_epoch >= 0",
            name="ck_storage_global_quota_maintenance_epoch",
        ),
        sa.CheckConstraint(
            "(maintenance_token IS NULL AND maintenance_started_at IS NULL "
            "AND maintenance_owner_expires_at IS NULL "
            "AND maintenance_database_started_at IS NULL) OR "
            "(maintenance_token ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            "[0-9a-f]{4}-[0-9a-f]{12}$' "
            "AND maintenance_started_at IS NOT NULL "
            "AND maintenance_database_started_at IS NOT NULL "
            "AND maintenance_started_at >= maintenance_database_started_at "
            "AND maintenance_owner_expires_at > maintenance_started_at)",
            name="ck_storage_global_quota_maintenance_gate",
        ),
        sa.CheckConstraint(
            "(recovery_database_started_at IS NULL AND recovery_completed_at IS NULL) "
            "OR (recovery_database_started_at IS NOT NULL "
            "AND recovery_completed_at >= recovery_database_started_at)",
            name="ck_storage_global_quota_recovery_proof",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "storage_tenant_quotas",
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("byte_limit", sa.BigInteger(), nullable=False),
        sa.Column("object_limit", sa.BigInteger(), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("committed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("reserved_count", sa.BigInteger(), nullable=False),
        sa.Column("committed_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "byte_limit > 0 AND object_limit > 0",
            name="ck_storage_tenant_quota_positive_limits",
        ),
        sa.CheckConstraint(
            "reserved_bytes >= 0 AND committed_bytes >= 0 "
            "AND reserved_count >= 0 AND committed_count >= 0",
            name="ck_storage_tenant_quota_nonnegative_counters",
        ),
        sa.CheckConstraint(
            "reserved_bytes <= byte_limit - committed_bytes "
            "AND reserved_count <= object_limit - committed_count",
            name="ck_storage_tenant_quota_within_limits",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("organization_id"),
    )
    op.create_table(
        "storage_object_tombstones",
        sa.Column("capacity_bucket", sa.String(length=63), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=160), nullable=False),
        sa.Column("owner_type", sa.String(length=40), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("accounting_state", sa.String(length=16), nullable=False),
        sa.Column("claim_token", sa.String(length=36), nullable=False),
        sa.Column(
            "retired_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(capacity_bucket) > 0 AND length(object_key) > 0 "
            "AND length(idempotency_key) > 0",
            name="ck_storage_tombstones_nonempty_keys",
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_storage_tombstones_sha256_canonical",
        ),
        sa.CheckConstraint(
            "size_bytes > 0",
            name="ck_storage_tombstones_positive_size",
        ),
        sa.CheckConstraint(
            "length(organization_id) > 0 AND length(project_id) > 0 "
            "AND length(media_type) > 0 AND length(owner_type) > 0 "
            "AND length(owner_id) > 0",
            name="ck_storage_tombstones_nonempty_identity",
        ),
        sa.CheckConstraint(
            "accounting_state IN ('reserved', 'committed')",
            name="ck_storage_tombstones_accounting_state",
        ),
        sa.PrimaryKeyConstraint("capacity_bucket", "object_key"),
        sa.UniqueConstraint(
            "capacity_bucket",
            "idempotency_key",
            name="uq_storage_tombstones_bucket_idempotency_key",
        ),
    )
    op.create_table(
        "stored_objects",
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=160), nullable=False),
        sa.Column("owner_type", sa.String(length=40), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(object_key) > 0 AND length(idempotency_key) > 0",
            name="ck_stored_objects_nonempty_keys",
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_stored_objects_sha256_canonical",
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_stored_objects_positive_size"),
        sa.CheckConstraint(
            "length(media_type) > 0 AND length(owner_type) > 0 AND length(owner_id) > 0",
            name="ck_stored_objects_nonempty_identity",
        ),
        sa.CheckConstraint(
            "(state = 'reserved' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND claim_token IS NULL "
            "AND claim_expires_at IS NULL) OR "
            "(state = 'committed' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND claim_token IS NULL "
            "AND claim_expires_at IS NULL) OR "
            "(state = 'delete_pending' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND claim_token IS NULL "
            "AND claim_expires_at IS NULL) OR "
            "(state = 'reaping' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND claim_token IS NOT NULL "
            "AND claim_expires_at IS NOT NULL)",
            name="ck_stored_objects_state_lease",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_stored_objects_org_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("organization_id", "object_key"),
        sa.UniqueConstraint(
            "object_key",
            name="uq_stored_objects_global_object_key",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_stored_objects_org_idempotency_key",
        ),
    )
    op.create_index(
        "ix_stored_objects_project_id",
        "stored_objects",
        ["project_id"],
    )
    op.create_index(
        "ix_stored_objects_org_state_lease",
        "stored_objects",
        ["organization_id", "state", "lease_expires_at", "claim_expires_at"],
    )

    _create_rls_policy(
        "storage_tenant_quotas",
        "storage_tenant_quotas_tenant_isolation",
    )
    _create_rls_policy("stored_objects", "stored_objects_tenant_isolation")
    _backfill_existing_objects()

    op.create_foreign_key(
        "fk_imported_assets_stored_object",
        "imported_assets",
        "stored_objects",
        ["organization_id", "object_key"],
        ["organization_id", "object_key"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_external_evidence_stored_object",
        "external_evidence",
        "stored_objects",
        ["organization_id", "object_key"],
        ["organization_id", "object_key"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_artifacts_stored_object",
        "artifacts",
        "stored_objects",
        ["organization_id", "object_key"],
        ["organization_id", "object_key"],
        ondelete="RESTRICT",
    )
    # Deliberately grant nothing here.  Revision 0013 creates the immutable
    # SECURITY DEFINER API and then rebuilds the exact runtime allow-list.
    # Keeping this migration independent of the evolving application ACL also
    # makes fresh installs safe when later revisions add new tables.


def downgrade() -> None:
    op.drop_constraint("fk_artifacts_stored_object", "artifacts", type_="foreignkey")
    op.drop_constraint(
        "fk_external_evidence_stored_object",
        "external_evidence",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_imported_assets_stored_object",
        "imported_assets",
        type_="foreignkey",
    )
    op.drop_index("ix_stored_objects_org_state_lease", table_name="stored_objects")
    op.drop_index("ix_stored_objects_project_id", table_name="stored_objects")
    op.drop_table("stored_objects")
    op.drop_table("storage_object_tombstones")
    op.drop_table("storage_tenant_quotas")
    op.drop_table("storage_global_quotas")
