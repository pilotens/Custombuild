from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Role(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    designer = "designer"
    reviewer = "reviewer"
    production = "production"
    operator = "operator"
    viewer = "viewer"


class DesignStatus(str, enum.Enum):
    concept = "concept"
    draft = "draft"
    design_validated = "design_validated"
    cam_validated = "cam_validated"
    approved = "approved"
    released = "released"
    superseded = "superseded"
    archived = "archived"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class StoredObjectState(str, enum.Enum):
    reserved = "reserved"
    committed = "committed"
    delete_pending = "delete_pending"
    reaping = "reaping"


class IdMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TenantMixin:
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )


class Organization(IdMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    oidc_sub: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(160))


class Membership(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("organization_id", "user_id"),)

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False))


class Project(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("organization_id", "name"),
        UniqueConstraint("organization_id", "id", name="uq_projects_org_id"),
    )

    name: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text, default="")
    furniture_type: Mapped[str] = mapped_column(String(80), default="bookcase")
    current_revision: Mapped[int] = mapped_column(Integer, default=0)
    draft_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    draft_template_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    draft_design_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    draft_spec_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    draft_workspace_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    draft_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    draft_updated_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )

    versions: Mapped[list[DesignVersion]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="DesignVersion.revision"
    )


class DesignVersion(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "design_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "revision"),
        UniqueConstraint("organization_id", "id", name="uq_design_versions_org_id"),
        UniqueConstraint(
            "organization_id",
            "project_id",
            "id",
            name="uq_design_versions_org_project_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_design_versions_org_project",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id", "source_import_id"],
            [
                "imported_assets.organization_id",
                "imported_assets.project_id",
                "imported_assets.id",
            ],
            ondelete="RESTRICT",
        ),
    )

    project_id: Mapped[str] = mapped_column(String(36), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[DesignStatus] = mapped_column(
        Enum(DesignStatus, native_enum=False), default=DesignStatus.draft
    )
    design_hash: Mapped[str] = mapped_column(String(64), index=True)
    context_hash: Mapped[str] = mapped_column(String(64), index=True)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_import_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    engine_version: Mapped[str] = mapped_column(String(40))
    template_version: Mapped[str] = mapped_column(String(40))
    template_id: Mapped[str] = mapped_column(String(80), default="shelving")
    template_capability_fingerprint: Mapped[str] = mapped_column(String(64), default="0" * 64)
    rule_version: Mapped[str] = mapped_column(String(40), default="unversioned")
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    immutable: Mapped[bool] = mapped_column(Boolean, default=False)

    project: Mapped[Project] = relationship(back_populates="versions")


class ImportedAsset(IdMixin, TimestampMixin, TenantMixin, Base):
    """Immutable reference input owned by one exact tenant project."""

    __tablename__ = "imported_assets"
    __table_args__ = (
        UniqueConstraint("organization_id", "project_id", "sha256"),
        UniqueConstraint("organization_id", "project_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_imported_assets_org_project",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "object_key"],
            ["stored_objects.organization_id", "stored_objects.object_key"],
            name="fk_imported_assets_stored_object",
            ondelete="RESTRICT",
        ),
        Index("ix_imported_assets_project_created", "project_id", "created_at"),
    )

    project_id: Mapped[str] = mapped_column(String(36), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    object_key: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String(160))
    original_filename: Mapped[str] = mapped_column(String(255))
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))


class GenerationJob(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key"),
        UniqueConstraint("organization_id", "id", name="uq_generation_jobs_org_id"),
        UniqueConstraint(
            "organization_id",
            "design_version_id",
            "id",
            name="uq_generation_jobs_org_version_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_generation_jobs_org_design_version",
            ondelete="CASCADE",
        ),
        Index("ix_generation_jobs_status_lease_expires_at", "status", "lease_expires_at"),
        Index("ix_generation_jobs_status_next_attempt_at", "status", "next_attempt_at"),
    )

    design_version_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False), default=JobStatus.queued, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(64))
    production_context_hash: Mapped[str] = mapped_column(String(64))
    production_engine_context_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        server_default=func.current_timestamp(),
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboxEvent(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("event_key"),
        Index(
            "ix_outbox_events_pending_available",
            "organization_id",
            "available_at",
            "created_at",
            "id",
            postgresql_where=text("dispatched_at IS NULL AND dead_lettered_at IS NULL"),
        ),
    )

    event_key: Mapped[str] = mapped_column(String(100))
    topic: Mapped[str] = mapped_column(String(100))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class Artifact(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("generation_job_id", "kind"),
        ForeignKeyConstraint(
            ["organization_id", "generation_job_id"],
            ["generation_jobs.organization_id", "generation_jobs.id"],
            name="fk_artifacts_org_generation_job",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "object_key"],
            ["stored_objects.organization_id", "stored_objects.object_key"],
            name="fk_artifacts_stored_object",
            ondelete="RESTRICT",
        ),
    )

    generation_job_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(80))
    object_key: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(160))


class ExternalEvidence(IdMixin, TimestampMixin, TenantMixin, Base):
    """Immutable, checksum-bound evidence for one exact project design hash."""

    __tablename__ = "external_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_external_evidence_org_project",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "object_key"],
            ["stored_objects.organization_id", "stored_objects.object_key"],
            name="fk_external_evidence_stored_object",
            ondelete="RESTRICT",
        ),
        Index("ix_external_evidence_project_type", "project_id", "evidence_type"),
    )

    project_id: Mapped[str] = mapped_column(String(36), index=True)
    evidence_type: Mapped[str] = mapped_column(String(40))
    rule_id: Mapped[str] = mapped_column(String(40))
    catalog_id: Mapped[str] = mapped_column(String(160))
    catalog_version: Mapped[str] = mapped_column(String(80))
    design_hash: Mapped[str] = mapped_column(String(64), index=True)
    object_key: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(160))
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StorageGlobalQuota(TimestampMixin, Base):
    """One durable process-wide storage counter row."""

    __tablename__ = "storage_global_quotas"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_storage_global_quota_singleton"),
        CheckConstraint(
            "byte_limit > 0 AND object_limit > 0",
            name="ck_storage_global_quota_positive_limits",
        ),
        CheckConstraint(
            "reserved_bytes >= 0 AND committed_bytes >= 0 "
            "AND reserved_count >= 0 AND committed_count >= 0",
            name="ck_storage_global_quota_nonnegative_counters",
        ),
        CheckConstraint(
            "reserved_bytes <= byte_limit - committed_bytes "
            "AND reserved_count <= object_limit - committed_count",
            name="ck_storage_global_quota_within_limits",
        ),
        CheckConstraint(
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
            "AND capacity_operator_config_sha256 IS NOT NULL "
            "AND length(capacity_operator_config_sha256) = 64 "
            "AND deploy_descriptor_sha256 IS NOT NULL "
            "AND length(deploy_descriptor_sha256) = 64 "
            "AND inventory_sha256 IS NOT NULL AND length(inventory_sha256) = 64 "
            "AND inventory_object_count IS NOT NULL AND inventory_object_count >= 0 "
            "AND inventory_bytes IS NOT NULL AND inventory_bytes >= 0 "
            "AND ledger_object_count IS NOT NULL AND ledger_object_count >= 0 "
            "AND ledger_bytes IS NOT NULL AND ledger_bytes >= 0 "
            "AND capacity_attested_at IS NOT NULL "
            "AND capacity_verified_at IS NOT NULL "
            "AND capacity_evidence_sha256 IS NOT NULL "
            "AND length(capacity_evidence_sha256) = 64)",
            name="ck_storage_global_quota_verified_capacity",
        ),
        CheckConstraint(
            "maintenance_epoch >= 0",
            name="ck_storage_global_quota_maintenance_epoch",
        ),
        CheckConstraint(
            "(maintenance_token IS NULL AND maintenance_started_at IS NULL "
            "AND maintenance_owner_expires_at IS NULL "
            "AND maintenance_database_started_at IS NULL) OR "
            "(maintenance_token IS NOT NULL AND length(maintenance_token) = 36 "
            "AND maintenance_started_at IS NOT NULL "
            "AND maintenance_database_started_at IS NOT NULL "
            "AND maintenance_started_at >= maintenance_database_started_at "
            "AND maintenance_owner_expires_at > maintenance_started_at)",
            name="ck_storage_global_quota_maintenance_gate",
        ),
        CheckConstraint(
            "(recovery_database_started_at IS NULL AND recovery_completed_at IS NULL) "
            "OR (recovery_database_started_at IS NOT NULL "
            "AND recovery_completed_at >= recovery_database_started_at)",
            name="ck_storage_global_quota_recovery_proof",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    byte_limit: Mapped[int] = mapped_column(BigInteger)
    object_limit: Mapped[int] = mapped_column(BigInteger)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    committed_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_count: Mapped[int] = mapped_column(BigInteger, default=0)
    committed_count: Mapped[int] = mapped_column(BigInteger, default=0)
    capacity_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    provisioned_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_overhead_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    emergency_reserve_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    capacity_headroom_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    volume_identity: Mapped[str | None] = mapped_column(String(255), nullable=True)
    capacity_bucket: Mapped[str | None] = mapped_column(String(63), nullable=True)
    capacity_operator_config_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deploy_descriptor_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inventory_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inventory_object_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    inventory_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ledger_object_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ledger_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    capacity_attested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    capacity_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    capacity_evidence_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    maintenance_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    maintenance_epoch: Mapped[int] = mapped_column(BigInteger, default=0)
    maintenance_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    maintenance_owner_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    maintenance_database_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recovery_database_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recovery_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class StorageTenantQuota(TimestampMixin, Base):
    """One durable counter row for each organization."""

    __tablename__ = "storage_tenant_quotas"
    __table_args__ = (
        CheckConstraint(
            "byte_limit > 0 AND object_limit > 0",
            name="ck_storage_tenant_quota_positive_limits",
        ),
        CheckConstraint(
            "reserved_bytes >= 0 AND committed_bytes >= 0 "
            "AND reserved_count >= 0 AND committed_count >= 0",
            name="ck_storage_tenant_quota_nonnegative_counters",
        ),
        CheckConstraint(
            "reserved_bytes <= byte_limit - committed_bytes "
            "AND reserved_count <= object_limit - committed_count",
            name="ck_storage_tenant_quota_within_limits",
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    byte_limit: Mapped[int] = mapped_column(BigInteger)
    object_limit: Mapped[int] = mapped_column(BigInteger)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    committed_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_count: Mapped[int] = mapped_column(BigInteger, default=0)
    committed_count: Mapped[int] = mapped_column(BigInteger, default=0)


class StoredObject(TimestampMixin, Base):
    """Exact, tenant-bound identity and lifecycle for every persisted object."""

    __tablename__ = "stored_objects"
    __table_args__ = (
        UniqueConstraint(
            "object_key",
            name="uq_stored_objects_global_object_key",
        ),
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_stored_objects_org_idempotency_key",
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_stored_objects_org_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(object_key) > 0 AND length(idempotency_key) > 0",
            name="ck_stored_objects_nonempty_keys",
        ),
        CheckConstraint(
            "length(sha256) = 64 AND sha256 = lower(sha256)",
            name="ck_stored_objects_sha256_canonical",
        ),
        CheckConstraint("size_bytes > 0", name="ck_stored_objects_positive_size"),
        CheckConstraint(
            "length(media_type) > 0 AND length(owner_type) > 0 AND length(owner_id) > 0",
            name="ck_stored_objects_nonempty_identity",
        ),
        CheckConstraint(
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
        Index(
            "ix_stored_objects_org_state_lease",
            "organization_id",
            "state",
            "lease_expires_at",
            "claim_expires_at",
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    object_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str] = mapped_column(String(160))
    owner_type: Mapped[str] = mapped_column(String(40))
    owner_id: Mapped[str] = mapped_column(String(36))
    idempotency_key: Mapped[str] = mapped_column(String(512))
    state: Mapped[StoredObjectState] = mapped_column(
        Enum(StoredObjectState, native_enum=False, length=16)
    )
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class StorageObjectTombstone(Base):
    """Append-only proof that one physical bucket key can never be reused."""

    __tablename__ = "storage_object_tombstones"
    __table_args__ = (
        UniqueConstraint(
            "capacity_bucket",
            "idempotency_key",
            name="uq_storage_tombstones_bucket_idempotency_key",
        ),
        CheckConstraint(
            "length(capacity_bucket) > 0 AND length(object_key) > 0 "
            "AND length(idempotency_key) > 0",
            name="ck_storage_tombstones_nonempty_keys",
        ),
        CheckConstraint(
            "length(sha256) = 64 AND sha256 = lower(sha256)",
            name="ck_storage_tombstones_sha256_canonical",
        ),
        CheckConstraint("size_bytes > 0", name="ck_storage_tombstones_positive_size"),
        CheckConstraint(
            "length(organization_id) > 0 AND length(project_id) > 0 "
            "AND length(media_type) > 0 AND length(owner_type) > 0 "
            "AND length(owner_id) > 0",
            name="ck_storage_tombstones_nonempty_identity",
        ),
        CheckConstraint(
            "accounting_state IN ('reserved', 'committed')",
            name="ck_storage_tombstones_accounting_state",
        ),
    )

    # These historical identity fields intentionally have no foreign keys.
    # Tenant or project deletion must never erase a burned physical key.
    capacity_bucket: Mapped[str] = mapped_column(String(63), primary_key=True)
    object_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36))
    project_id: Mapped[str] = mapped_column(String(36))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str] = mapped_column(String(160))
    owner_type: Mapped[str] = mapped_column(String(40))
    owner_id: Mapped[str] = mapped_column(String(36))
    idempotency_key: Mapped[str] = mapped_column(String(512))
    accounting_state: Mapped[str] = mapped_column(String(16))
    claim_token: Mapped[str] = mapped_column(String(36))
    retired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Approval(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("design_version_id", "approval_type"),
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_approvals_org_design_version",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "generation_job_id"],
            ["generation_jobs.organization_id", "generation_jobs.id"],
            name="fk_approvals_org_generation_job",
            ondelete="CASCADE",
        ),
    )

    design_version_id: Mapped[str] = mapped_column(String(36))
    approval_type: Mapped[str] = mapped_column(String(80))
    approved_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text)
    generation_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    production_context_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    overrides_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class Release(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "releases"
    __table_args__ = (
        UniqueConstraint("design_version_id"),
        UniqueConstraint("organization_id", "id", name="uq_releases_org_id"),
        UniqueConstraint(
            "organization_id",
            "generation_job_id",
            name="uq_releases_org_generation_job",
        ),
        UniqueConstraint(
            "organization_id",
            "design_version_id",
            "generation_job_id",
            "id",
            name="uq_releases_org_version_job_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_releases_org_design_version",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "generation_job_id"],
            ["generation_jobs.organization_id", "generation_jobs.id"],
            name="fk_releases_org_generation_job",
            ondelete="RESTRICT",
        ),
    )

    design_version_id: Mapped[str] = mapped_column(String(36))
    generation_job_id: Mapped[str] = mapped_column(String(36))
    release_number: Mapped[str] = mapped_column(String(80))
    released_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    production_context_hash: Mapped[str] = mapped_column(String(64))
    generation_result_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    artifact_inventory_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)


class AuditEvent(IdMixin, TenantMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_org_time", "organization_id", "occurred_at"),)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class VersionedCatalog(IdMixin, TimestampMixin, TenantMixin):
    name: Mapped[str] = mapped_column(String(180))
    version: Mapped[str] = mapped_column(String(40))
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class FurnitureTemplate(VersionedCatalog, Base):
    __tablename__ = "furniture_templates"


class TemplateVersion(VersionedCatalog, Base):
    __tablename__ = "template_versions"


class Material(VersionedCatalog, Base):
    __tablename__ = "materials"


class MaterialPropertyVersion(VersionedCatalog, Base):
    __tablename__ = "material_property_versions"


class StockItem(VersionedCatalog, Base):
    __tablename__ = "stock_items"


class HardwareItem(VersionedCatalog, Base):
    __tablename__ = "hardware_items"


class JointDefinition(VersionedCatalog, Base):
    __tablename__ = "joint_definitions"


class MachineProfile(VersionedCatalog, Base):
    __tablename__ = "machine_profiles"


class ToolDefinition(VersionedCatalog, Base):
    __tablename__ = "tool_definitions"


class PostprocessorVersion(VersionedCatalog, Base):
    __tablename__ = "postprocessor_versions"


class DesignRecord(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "designs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_designs_org_project",
            ondelete="CASCADE",
        ),
    )
    project_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(180))


class ProductionRecord(IdMixin, TimestampMixin, TenantMixin):
    design_version_id: Mapped[str] = mapped_column(String(36), index=True)
    stable_key: Mapped[str] = mapped_column(String(160))
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ParameterDefinition(ProductionRecord, Base):
    __tablename__ = "parameter_definitions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_parameter_definitions_org_design_version",
            ondelete="CASCADE",
        ),
    )


class ParameterValue(ProductionRecord, Base):
    __tablename__ = "parameter_values"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_parameter_values_org_design_version",
            ondelete="CASCADE",
        ),
    )


class Part(ProductionRecord, Base):
    __tablename__ = "parts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_parts_org_design_version",
            ondelete="CASCADE",
        ),
    )


class PartFace(ProductionRecord, Base):
    __tablename__ = "part_faces"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_part_faces_org_design_version",
            ondelete="CASCADE",
        ),
    )


class ManufacturingFeature(ProductionRecord, Base):
    __tablename__ = "manufacturing_features"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_manufacturing_features_org_design_version",
            ondelete="CASCADE",
        ),
    )


class Constraint(ProductionRecord, Base):
    __tablename__ = "constraints"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_constraints_org_design_version",
            ondelete="CASCADE",
        ),
    )


class LoadCase(ProductionRecord, Base):
    __tablename__ = "load_cases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_load_cases_org_design_version",
            ondelete="CASCADE",
        ),
    )


class RuleEvaluationRecord(ProductionRecord, Base):
    __tablename__ = "rule_evaluations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_rule_evaluations_org_design_version",
            ondelete="CASCADE",
        ),
    )


class AssemblyGraphRecord(ProductionRecord, Base):
    __tablename__ = "assembly_graphs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_assembly_graphs_org_design_version",
            ondelete="CASCADE",
        ),
    )


class AssemblyStepRecord(ProductionRecord, Base):
    __tablename__ = "assembly_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_assembly_steps_org_design_version",
            ondelete="CASCADE",
        ),
    )


class BOMLine(ProductionRecord, Base):
    __tablename__ = "bom_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_bom_lines_org_design_version",
            ondelete="CASCADE",
        ),
    )


class CutListLine(ProductionRecord, Base):
    __tablename__ = "cut_list_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_cut_list_lines_org_design_version",
            ondelete="CASCADE",
        ),
    )


class NestingLayout(ProductionRecord, Base):
    __tablename__ = "nesting_layouts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_nesting_layouts_org_design_version",
            ondelete="CASCADE",
        ),
    )


class SetupRecord(ProductionRecord, Base):
    __tablename__ = "setups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_setups_org_design_version",
            ondelete="CASCADE",
        ),
    )


class CAMOperationRecord(ProductionRecord, Base):
    __tablename__ = "cam_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_cam_operations_org_design_version",
            ondelete="CASCADE",
        ),
    )


class Toolpath(ProductionRecord, Base):
    __tablename__ = "toolpaths"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "design_version_id"],
            ["design_versions.organization_id", "design_versions.id"],
            name="fk_toolpaths_org_design_version",
            ondelete="CASCADE",
        ),
    )


class WorkshopTrustState(Base):
    """Per-tenant serialization boundary for workshop trust mutations."""

    __tablename__ = "workshop_trust_states"
    __table_args__ = (
        CheckConstraint("trust_epoch >= 0", name="ck_workshop_trust_states_epoch"),
    )

    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    trust_epoch: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class WorkshopActor(IdMixin, TenantMixin, Base):
    """Canonical person identity used for workshop separation of duties."""

    __tablename__ = "workshop_actors"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_workshop_actors_org_id"),
        UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_workshop_actors_org_user",
        ),
        UniqueConstraint(
            "organization_id",
            "external_authority",
            "external_subject_sha256",
            name="uq_workshop_actors_org_external_subject",
        ),
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_workshop_actors_org_membership",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(actor_type = 'WORKFORCE_USER' AND user_id IS NOT NULL "
            "AND external_authority IS NULL AND external_subject_sha256 IS NULL) OR "
            "(actor_type = 'EXTERNAL_CERTIFIED_PERSON' AND user_id IS NULL "
            "AND external_authority IS NOT NULL AND length(external_authority) > 0 "
            "AND external_subject_sha256 IS NOT NULL "
            "AND length(external_subject_sha256) = 64 "
            "AND external_subject_sha256 = lower(external_subject_sha256))",
            name="ck_workshop_actors_identity_shape",
        ),
    )

    actor_type: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    external_authority: Mapped[str | None] = mapped_column(String(160), nullable=True)
    external_subject_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkshopSignerPrincipal(IdMixin, TenantMixin, Base):
    """Immutable signer alias bound one-to-one to a canonical person."""

    __tablename__ = "workshop_signer_principals"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_workshop_signer_principals_org_id",
        ),
        UniqueConstraint(
            "organization_id",
            "principal_id",
            name="uq_workshop_signer_principals_org_principal",
        ),
        UniqueConstraint(
            "organization_id",
            "actor_id",
            name="uq_workshop_signer_principals_org_actor",
        ),
        UniqueConstraint(
            "organization_id",
            "id",
            "actor_id",
            name="uq_workshop_signer_principals_org_id_actor",
        ),
        ForeignKeyConstraint(
            ["organization_id", "actor_id"],
            ["workshop_actors.organization_id", "workshop_actors.id"],
            name="fk_workshop_signer_principals_org_actor",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "signer_role IN "
            "('workshop_maker', 'workshop_checker', 'workshop_supervisor')",
            name="ck_workshop_signer_principals_role",
        ),
        CheckConstraint(
            "length(principal_id) > 0",
            name="ck_workshop_signer_principals_identity",
        ),
    )

    actor_id: Mapped[str] = mapped_column(String(36))
    principal_id: Mapped[str] = mapped_column(String(160))
    signer_role: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkshopIssuerKey(IdMixin, TenantMixin, Base):
    """Immutable Ed25519 trust anchor; revocation is append-only elsewhere."""

    __tablename__ = "workshop_issuer_keys"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_workshop_issuer_keys_org_id"),
        UniqueConstraint(
            "organization_id",
            "id",
            "actor_id",
            name="uq_workshop_issuer_keys_org_id_actor",
        ),
        UniqueConstraint(
            "organization_id",
            "signer_principal_id",
            "key_id",
            name="uq_workshop_issuer_keys_org_principal_key",
        ),
        UniqueConstraint(
            "organization_id",
            "public_key_sha256",
            name="uq_workshop_issuer_keys_org_public_key",
        ),
        ForeignKeyConstraint(
            ["organization_id", "signer_principal_id", "actor_id"],
            [
                "workshop_signer_principals.organization_id",
                "workshop_signer_principals.id",
                "workshop_signer_principals.actor_id",
            ],
            name="fk_workshop_issuer_keys_org_principal_actor",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(key_id) > 0 AND length(public_key_base64) = 44",
            name="ck_workshop_issuer_keys_identity",
        ),
        CheckConstraint(
            "length(public_key_sha256) = 64 "
            "AND public_key_sha256 = lower(public_key_sha256)",
            name="ck_workshop_issuer_keys_sha256",
        ),
        CheckConstraint(
            "not_before < not_after",
            name="ck_workshop_issuer_keys_validity",
        ),
        CheckConstraint(
            "qualified_pre_cut OR qualified_reference_part "
            "OR qualified_final_workshop OR qualified_air_cut_supervisor",
            name="ck_workshop_issuer_keys_qualification",
        ),
    )

    signer_principal_id: Mapped[str] = mapped_column(String(36))
    actor_id: Mapped[str] = mapped_column(String(36))
    key_id: Mapped[str] = mapped_column(String(160))
    public_key_base64: Mapped[str] = mapped_column(String(44))
    public_key_sha256: Mapped[str] = mapped_column(String(64))
    qualified_pre_cut: Mapped[bool] = mapped_column(Boolean, default=False)
    qualified_reference_part: Mapped[bool] = mapped_column(Boolean, default=False)
    qualified_final_workshop: Mapped[bool] = mapped_column(Boolean, default=False)
    qualified_air_cut_supervisor: Mapped[bool] = mapped_column(Boolean, default=False)
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    not_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkshopPolicyRecord(IdMixin, TenantMixin, Base):
    """Immutable canonical server-owned workshop verification policy."""

    __tablename__ = "workshop_policies"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_workshop_policies_org_id"),
        UniqueConstraint(
            "organization_id",
            "id",
            "policy_sha256",
            name="uq_workshop_policies_org_id_sha",
        ),
        UniqueConstraint(
            "organization_id",
            "policy_id",
            "policy_version",
            name="uq_workshop_policies_org_identity",
        ),
        UniqueConstraint(
            "organization_id",
            "policy_sha256",
            name="uq_workshop_policies_org_sha",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_actor_id"],
            ["workshop_actors.organization_id", "workshop_actors.id"],
            name="fk_workshop_policies_org_creator",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(policy_id) > 0 AND length(policy_version) > 0 "
            "AND length(schema_version) > 0",
            name="ck_workshop_policies_identity",
        ),
        CheckConstraint(
            "length(policy_sha256) = 64 AND policy_sha256 = lower(policy_sha256)",
            name="ck_workshop_policies_sha256",
        ),
        CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 4194304 "
            "AND length(canonical_json_bytes) = size_bytes",
            name="ck_workshop_policies_bytes",
        ),
    )

    policy_id: Mapped[str] = mapped_column(String(160))
    policy_version: Mapped[str] = mapped_column(String(160))
    schema_version: Mapped[str] = mapped_column(String(160))
    policy_sha256: Mapped[str] = mapped_column(String(64))
    canonical_json_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_by_actor_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkshopRunRecord(IdMixin, TenantMixin, Base):
    """Exact server-derived identity for one executable workshop run."""

    __tablename__ = "workshop_runs"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_workshop_runs_org_id"),
        UniqueConstraint(
            "organization_id",
            "id",
            "run_sha256",
            "workshop_policy_sha256",
            name="uq_workshop_runs_org_id_run_policy",
        ),
        UniqueConstraint(
            "organization_id",
            "run_sha256",
            name="uq_workshop_runs_org_sha",
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id", "design_version_id"],
            [
                "design_versions.organization_id",
                "design_versions.project_id",
                "design_versions.id",
            ],
            name="fk_workshop_runs_org_project_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "design_version_id", "generation_job_id"],
            [
                "generation_jobs.organization_id",
                "generation_jobs.design_version_id",
                "generation_jobs.id",
            ],
            name="fk_workshop_runs_org_version_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "organization_id",
                "design_version_id",
                "generation_job_id",
                "design_review_release_id",
            ],
            [
                "releases.organization_id",
                "releases.design_version_id",
                "releases.generation_job_id",
                "releases.id",
            ],
            name="fk_workshop_runs_org_release_graph",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "policy_record_id", "workshop_policy_sha256"],
            [
                "workshop_policies.organization_id",
                "workshop_policies.id",
                "workshop_policies.policy_sha256",
            ],
            name="fk_workshop_runs_org_policy",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_actor_id"],
            ["workshop_actors.organization_id", "workshop_actors.id"],
            name="fk_workshop_runs_org_creator",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "machine_program_kind = 'EXECUTABLE'",
            name="ck_workshop_runs_executable_only",
        ),
        CheckConstraint(
            "length(schema_version) > 0 AND length(postprocessor_id) > 0 "
            "AND length(postprocessor_version) > 0",
            name="ck_workshop_runs_identity",
        ),
        CheckConstraint(
            "length(run_sha256) = 64 AND run_sha256 = lower(run_sha256) "
            "AND length(design_hash) = 64 AND design_hash = lower(design_hash) "
            "AND length(production_context_hash) = 64 "
            "AND production_context_hash = lower(production_context_hash) "
            "AND length(manifest_sha256) = 64 AND manifest_sha256 = lower(manifest_sha256) "
            "AND length(bundle_sha256) = 64 AND bundle_sha256 = lower(bundle_sha256) "
            "AND length(operations_sha256) = 64 AND operations_sha256 = lower(operations_sha256) "
            "AND length(generation_plan_sha256) = 64 "
            "AND generation_plan_sha256 = lower(generation_plan_sha256) "
            "AND length(workshop_policy_sha256) = 64 "
            "AND workshop_policy_sha256 = lower(workshop_policy_sha256) "
            "AND length(machine_program_set_sha256) = 64 "
            "AND machine_program_set_sha256 = lower(machine_program_set_sha256) "
            "AND length(postprocessor_binary_sha256) = 64 "
            "AND postprocessor_binary_sha256 = lower(postprocessor_binary_sha256) "
            "AND length(postprocessor_config_sha256) = 64 "
            "AND postprocessor_config_sha256 = lower(postprocessor_config_sha256)",
            name="ck_workshop_runs_hashes",
        ),
    )

    schema_version: Mapped[str] = mapped_column(String(160))
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    design_version_id: Mapped[str] = mapped_column(String(36), index=True)
    design_review_release_id: Mapped[str] = mapped_column(String(36), index=True)
    generation_job_id: Mapped[str] = mapped_column(String(36), index=True)
    generation_finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    design_hash: Mapped[str] = mapped_column(String(64))
    production_context_hash: Mapped[str] = mapped_column(String(64))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    bundle_sha256: Mapped[str] = mapped_column(String(64))
    operations_sha256: Mapped[str] = mapped_column(String(64))
    generation_plan_sha256: Mapped[str] = mapped_column(String(64))
    policy_record_id: Mapped[str] = mapped_column(String(36))
    workshop_policy_sha256: Mapped[str] = mapped_column(String(64))
    machine_program_kind: Mapped[str] = mapped_column(String(16))
    machine_program_set_sha256: Mapped[str] = mapped_column(String(64))
    postprocessor_id: Mapped[str] = mapped_column(String(160))
    postprocessor_version: Mapped[str] = mapped_column(String(160))
    postprocessor_binary_sha256: Mapped[str] = mapped_column(String(64))
    postprocessor_config_sha256: Mapped[str] = mapped_column(String(64))
    run_sha256: Mapped[str] = mapped_column(String(64))
    created_by_actor_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkshopRunProgram(IdMixin, TenantMixin, Base):
    __tablename__ = "workshop_run_programs"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "id", name="uq_workshop_run_programs_org_id"
        ),
        UniqueConstraint(
            "organization_id",
            "workshop_run_id",
            "ordinal",
            name="uq_workshop_run_programs_org_run_ordinal",
        ),
        UniqueConstraint(
            "organization_id",
            "workshop_run_id",
            "program_id",
            name="uq_workshop_run_programs_org_run_program_id",
        ),
        UniqueConstraint(
            "organization_id",
            "workshop_run_id",
            "relative_path",
            name="uq_workshop_run_programs_org_run_path",
        ),
        UniqueConstraint(
            "organization_id",
            "workshop_run_id",
            "identity_sha256",
            name="uq_workshop_run_programs_org_run_identity",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workshop_run_id"],
            ["workshop_runs.organization_id", "workshop_runs.id"],
            name="fk_workshop_run_programs_org_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="ck_workshop_run_programs_ordinal"),
        CheckConstraint(
            "length(program_id) > 0 AND length(purpose) > 0 "
            "AND length(relative_path) > 0 AND length(setup_id) > 0 "
            "AND length(wcs_id) > 0 AND length(stock_id) > 0 "
            "AND length(media_type) > 0",
            name="ck_workshop_run_programs_identity",
        ),
        CheckConstraint(
            "length(operation_set_sha256) = 64 "
            "AND operation_set_sha256 = lower(operation_set_sha256) "
            "AND length(program_sha256) = 64 "
            "AND program_sha256 = lower(program_sha256) "
            "AND length(identity_sha256) = 64 "
            "AND identity_sha256 = lower(identity_sha256)",
            name="ck_workshop_run_programs_hashes",
        ),
        CheckConstraint(
            "program_size_bytes > 0 AND identity_size_bytes > 0 "
            "AND identity_size_bytes <= 1048576 "
            "AND length(canonical_identity_json_bytes) = identity_size_bytes",
            name="ck_workshop_run_programs_bytes",
        ),
        Index(
            "uq_workshop_run_programs_reference_part",
            "organization_id",
            "workshop_run_id",
            unique=True,
            postgresql_where=text("purpose = 'REFERENCE_PART'"),
            sqlite_where=text("purpose = 'REFERENCE_PART'"),
        ),
    )

    workshop_run_id: Mapped[str] = mapped_column(String(36))
    ordinal: Mapped[int] = mapped_column(Integer)
    program_id: Mapped[str] = mapped_column(String(160))
    purpose: Mapped[str] = mapped_column(String(24))
    relative_path: Mapped[str] = mapped_column(String(512))
    setup_id: Mapped[str] = mapped_column(String(160))
    wcs_id: Mapped[str] = mapped_column(String(160))
    stock_id: Mapped[str] = mapped_column(String(160))
    operation_set_sha256: Mapped[str] = mapped_column(String(64))
    program_sha256: Mapped[str] = mapped_column(String(64))
    program_size_bytes: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str] = mapped_column(String(160))
    identity_sha256: Mapped[str] = mapped_column(String(64))
    canonical_identity_json_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    identity_size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkshopNonceSet(IdMixin, TenantMixin, Base):
    __tablename__ = "workshop_nonce_sets"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_workshop_nonce_sets_org_id"),
        UniqueConstraint(
            "organization_id",
            "id",
            "workshop_run_id",
            "run_sha256",
            "workshop_policy_sha256",
            "generation",
            name="uq_workshop_nonce_sets_org_binding",
        ),
        UniqueConstraint(
            "organization_id",
            "workshop_run_id",
            "generation",
            name="uq_workshop_nonce_sets_org_run_generation",
        ),
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_workshop_nonce_sets_org_idempotency",
        ),
        ForeignKeyConstraint(
            [
                "organization_id",
                "workshop_run_id",
                "run_sha256",
                "workshop_policy_sha256",
            ],
            [
                "workshop_runs.organization_id",
                "workshop_runs.id",
                "workshop_runs.run_sha256",
                "workshop_runs.workshop_policy_sha256",
            ],
            name="fk_workshop_nonce_sets_org_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "issued_by_actor_id"],
            ["workshop_actors.organization_id", "workshop_actors.id"],
            name="fk_workshop_nonce_sets_org_issuer",
            ondelete="RESTRICT",
        ),
        CheckConstraint("generation > 0", name="ck_workshop_nonce_sets_generation"),
        CheckConstraint(
            "issued_at < expires_at",
            name="ck_workshop_nonce_sets_validity",
        ),
        CheckConstraint(
            "length(idempotency_key) > 0 AND length(nonce_key_version) > 0 "
            "AND length(nonce_derivation_context) = 32 "
            "AND nonce_derivation_scheme = 'CUSTOMBUILD-HMAC-SHA256-V1'",
            name="ck_workshop_nonce_sets_identity",
        ),
        CheckConstraint(
            "length(run_sha256) = 64 AND run_sha256 = lower(run_sha256) "
            "AND length(workshop_policy_sha256) = 64 "
            "AND workshop_policy_sha256 = lower(workshop_policy_sha256)",
            name="ck_workshop_nonce_sets_hashes",
        ),
        CheckConstraint(
            "(consumed_at IS NULL AND consumed_chain_sha256 IS NULL) OR "
            "(consumed_at IS NOT NULL AND consumed_at >= issued_at "
            "AND consumed_at <= expires_at "
            "AND consumed_chain_sha256 IS NOT NULL "
            "AND length(consumed_chain_sha256) = 64 "
            "AND consumed_chain_sha256 = lower(consumed_chain_sha256))",
            name="ck_workshop_nonce_sets_consumption",
        ),
        CheckConstraint(
            "invalidated_at IS NULL OR "
            "(invalidated_at >= issued_at AND consumed_at IS NULL)",
            name="ck_workshop_nonce_sets_invalidation",
        ),
        Index(
            "uq_workshop_nonce_sets_active_run",
            "organization_id",
            "workshop_run_id",
            unique=True,
            postgresql_where=text("invalidated_at IS NULL AND consumed_at IS NULL"),
            sqlite_where=text("invalidated_at IS NULL AND consumed_at IS NULL"),
        ),
    )

    workshop_run_id: Mapped[str] = mapped_column(String(36))
    run_sha256: Mapped[str] = mapped_column(String(64))
    workshop_policy_sha256: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    nonce_derivation_scheme: Mapped[str] = mapped_column(String(40))
    nonce_key_version: Mapped[str] = mapped_column(String(160))
    nonce_derivation_context: Mapped[bytes] = mapped_column(LargeBinary(32))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_chain_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issued_by_actor_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkshopNonce(Base):
    """One burned, digest-only server challenge for an exact run stage."""

    __tablename__ = "workshop_nonces"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "nonce_digest_sha256",
            name="uq_workshop_nonces_org_digest",
        ),
        ForeignKeyConstraint(
            [
                "organization_id",
                "nonce_set_id",
                "workshop_run_id",
                "run_sha256",
                "workshop_policy_sha256",
                "set_generation",
            ],
            [
                "workshop_nonce_sets.organization_id",
                "workshop_nonce_sets.id",
                "workshop_nonce_sets.workshop_run_id",
                "workshop_nonce_sets.run_sha256",
                "workshop_nonce_sets.workshop_policy_sha256",
                "workshop_nonce_sets.generation",
            ],
            name="fk_workshop_nonces_org_set_binding",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "stage IN ('PRE_CUT', 'REFERENCE_PART', 'FINAL_WORKSHOP')",
            name="ck_workshop_nonces_stage",
        ),
        CheckConstraint(
            "length(nonce_digest_sha256) = 64 "
            "AND nonce_digest_sha256 = lower(nonce_digest_sha256)",
            name="ck_workshop_nonces_digest",
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    nonce_set_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    stage: Mapped[str] = mapped_column(String(24), primary_key=True)
    workshop_run_id: Mapped[str] = mapped_column(String(36))
    run_sha256: Mapped[str] = mapped_column(String(64))
    workshop_policy_sha256: Mapped[str] = mapped_column(String(64))
    set_generation: Mapped[int] = mapped_column(Integer)
    nonce_digest_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkshopChainAcceptance(IdMixin, TenantMixin, Base):
    """Immutable initial acceptance; current validity is always re-derived."""

    __tablename__ = "workshop_chain_acceptances"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "id", name="uq_workshop_chain_acceptances_org_id"
        ),
        UniqueConstraint(
            "organization_id",
            "nonce_set_id",
            name="uq_workshop_chain_acceptances_org_nonce_set",
        ),
        UniqueConstraint(
            "organization_id",
            "workshop_run_id",
            "chain_sha256",
            name="uq_workshop_chain_acceptances_org_run_chain",
        ),
        ForeignKeyConstraint(
            [
                "organization_id",
                "workshop_run_id",
                "run_sha256",
                "workshop_policy_sha256",
            ],
            [
                "workshop_runs.organization_id",
                "workshop_runs.id",
                "workshop_runs.run_sha256",
                "workshop_runs.workshop_policy_sha256",
            ],
            name="fk_workshop_chain_acceptances_org_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "organization_id",
                "nonce_set_id",
                "workshop_run_id",
                "run_sha256",
                "workshop_policy_sha256",
                "nonce_set_generation",
            ],
            [
                "workshop_nonce_sets.organization_id",
                "workshop_nonce_sets.id",
                "workshop_nonce_sets.workshop_run_id",
                "workshop_nonce_sets.run_sha256",
                "workshop_nonce_sets.workshop_policy_sha256",
                "workshop_nonce_sets.generation",
            ],
            name="fk_workshop_chain_acceptances_org_nonce_set",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "verified_by_actor_id"],
            ["workshop_actors.organization_id", "workshop_actors.id"],
            name="fk_workshop_chain_acceptances_org_verifier",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "eligibility = 'VERIFIED_FOR_RELEASE_REVIEW'",
            name="ck_workshop_chain_acceptances_eligibility",
        ),
        CheckConstraint(
            "trust_epoch >= 0 AND nonce_set_generation > 0 "
            "AND verified_at < valid_until",
            name="ck_workshop_chain_acceptances_validity",
        ),
        CheckConstraint(
            "length(final_attestation_id) > 0 AND length(verifier_version) > 0",
            name="ck_workshop_chain_acceptances_identity",
        ),
        CheckConstraint(
            "length(chain_sha256) = 64 AND chain_sha256 = lower(chain_sha256) "
            "AND length(run_sha256) = 64 AND run_sha256 = lower(run_sha256) "
            "AND length(workshop_policy_sha256) = 64 "
            "AND workshop_policy_sha256 = lower(workshop_policy_sha256) "
            "AND length(registry_snapshot_sha256) = 64 "
            "AND registry_snapshot_sha256 = lower(registry_snapshot_sha256) "
            "AND length(verifier_source_sha256) = 64 "
            "AND verifier_source_sha256 = lower(verifier_source_sha256) "
            "AND length(pre_cut_attestation_sha256) = 64 "
            "AND pre_cut_attestation_sha256 = lower(pre_cut_attestation_sha256) "
            "AND length(reference_part_attestation_sha256) = 64 "
            "AND reference_part_attestation_sha256 = lower(reference_part_attestation_sha256) "
            "AND length(final_workshop_attestation_sha256) = 64 "
            "AND final_workshop_attestation_sha256 = lower(final_workshop_attestation_sha256) "
            "AND length(pre_cut_statement_sha256) = 64 "
            "AND pre_cut_statement_sha256 = lower(pre_cut_statement_sha256) "
            "AND length(reference_part_statement_sha256) = 64 "
            "AND reference_part_statement_sha256 = lower(reference_part_statement_sha256) "
            "AND length(final_workshop_statement_sha256) = 64 "
            "AND final_workshop_statement_sha256 = lower(final_workshop_statement_sha256)",
            name="ck_workshop_chain_acceptances_hashes",
        ),
        Index(
            "ix_workshop_chain_acceptances_current",
            "organization_id",
            "workshop_run_id",
            "trust_epoch",
            "valid_until",
        ),
    )

    workshop_run_id: Mapped[str] = mapped_column(String(36))
    run_sha256: Mapped[str] = mapped_column(String(64))
    workshop_policy_sha256: Mapped[str] = mapped_column(String(64))
    nonce_set_id: Mapped[str] = mapped_column(String(36))
    nonce_set_generation: Mapped[int] = mapped_column(Integer)
    chain_sha256: Mapped[str] = mapped_column(String(64))
    pre_cut_attestation_sha256: Mapped[str] = mapped_column(String(64))
    reference_part_attestation_sha256: Mapped[str] = mapped_column(String(64))
    final_workshop_attestation_sha256: Mapped[str] = mapped_column(String(64))
    pre_cut_statement_sha256: Mapped[str] = mapped_column(String(64))
    reference_part_statement_sha256: Mapped[str] = mapped_column(String(64))
    final_workshop_statement_sha256: Mapped[str] = mapped_column(String(64))
    final_attestation_id: Mapped[str] = mapped_column(String(160))
    trust_epoch: Mapped[int] = mapped_column(BigInteger)
    registry_snapshot_sha256: Mapped[str] = mapped_column(String(64))
    eligibility: Mapped[str] = mapped_column(String(40))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verifier_version: Mapped[str] = mapped_column(String(160))
    verifier_source_sha256: Mapped[str] = mapped_column(String(64))
    verified_by_actor_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkshopAcceptanceSigner(Base):
    """Exact canonical people and keys behind every accepted stage."""

    __tablename__ = "workshop_acceptance_signers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "acceptance_id"],
            [
                "workshop_chain_acceptances.organization_id",
                "workshop_chain_acceptances.id",
            ],
            name="fk_workshop_acceptance_signers_org_acceptance",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "issuer_key_id", "actor_id"],
            [
                "workshop_issuer_keys.organization_id",
                "workshop_issuer_keys.id",
                "workshop_issuer_keys.actor_id",
            ],
            name="fk_workshop_acceptance_signers_org_key_actor",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "stage IN ('PRE_CUT', 'REFERENCE_PART', 'FINAL_WORKSHOP')",
            name="ck_workshop_acceptance_signers_stage",
        ),
        CheckConstraint(
            "signer_role IN "
            "('workshop_maker', 'workshop_checker', 'workshop_supervisor') "
            "AND (signer_role <> 'workshop_supervisor' OR stage = 'PRE_CUT')",
            name="ck_workshop_acceptance_signers_role",
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    acceptance_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    stage: Mapped[str] = mapped_column(String(24), primary_key=True)
    signer_role: Mapped[str] = mapped_column(String(24), primary_key=True)
    issuer_key_id: Mapped[str] = mapped_column(String(36))
    actor_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkshopRevocation(IdMixin, TenantMixin, Base):
    """Append-only trust revocation; aliases are blocked by digest identity."""

    __tablename__ = "workshop_revocations"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "id", name="uq_workshop_revocations_org_id"
        ),
        UniqueConstraint(
            "organization_id",
            "target_kind",
            "target_sha256",
            name="uq_workshop_revocations_org_target",
        ),
        UniqueConstraint(
            "organization_id",
            "revocation_epoch",
            name="uq_workshop_revocations_org_epoch",
        ),
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_workshop_revocations_org_idempotency",
        ),
        ForeignKeyConstraint(
            ["organization_id", "revoked_by_actor_id"],
            ["workshop_actors.organization_id", "workshop_actors.id"],
            name="fk_workshop_revocations_org_actor",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "target_kind IN ('ISSUER_KEY', 'RUN', 'STATEMENT', "
            "'EVIDENCE_OBJECT', 'EVIDENCE_ATTACHMENT', 'EVIDENCE_CLAIM')",
            name="ck_workshop_revocations_target_kind",
        ),
        CheckConstraint(
            "length(target_sha256) = 64 AND target_sha256 = lower(target_sha256)",
            name="ck_workshop_revocations_target_sha256",
        ),
        CheckConstraint(
            "revocation_epoch > 0 AND length(reason) > 0 "
            "AND length(idempotency_key) > 0",
            name="ck_workshop_revocations_identity",
        ),
    )

    target_kind: Mapped[str] = mapped_column(String(24))
    target_sha256: Mapped[str] = mapped_column(String(64))
    revocation_epoch: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(500))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    revoked_by_actor_id: Mapped[str] = mapped_column(String(36))
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
