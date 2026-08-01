from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    name: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text, default="")
    furniture_type: Mapped[str] = mapped_column(String(80), default="bookcase")
    current_revision: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    versions: Mapped[list[DesignVersion]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="DesignVersion.revision"
    )


class DesignVersion(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "design_versions"
    __table_args__ = (UniqueConstraint("project_id", "revision"),)

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[DesignStatus] = mapped_column(
        Enum(DesignStatus, native_enum=False), default=DesignStatus.draft
    )
    design_hash: Mapped[str] = mapped_column(String(64), index=True)
    context_hash: Mapped[str] = mapped_column(String(64), index=True)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    engine_version: Mapped[str] = mapped_column(String(40), default="0.1.0")
    template_version: Mapped[str] = mapped_column(String(40), default="bookcase@1.0.0")
    rule_version: Mapped[str] = mapped_column(String(40), default="unversioned")
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    immutable: Mapped[bool] = mapped_column(Boolean, default=False)

    project: Mapped[Project] = relationship(back_populates="versions")


class GenerationJob(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (UniqueConstraint("organization_id", "idempotency_key"),)

    design_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("design_versions.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False), default=JobStatus.queued, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(64))
    production_context_hash: Mapped[str] = mapped_column(String(64))
    production_engine_context_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboxEvent(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (UniqueConstraint("event_key"),)

    event_key: Mapped[str] = mapped_column(String(100))
    topic: Mapped[str] = mapped_column(String(100))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class Artifact(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("generation_job_id", "kind"),)

    generation_job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(80))
    object_key: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(160))


class Approval(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("design_version_id", "approval_type"),)

    design_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("design_versions.id", ondelete="CASCADE")
    )
    approval_type: Mapped[str] = mapped_column(String(80))
    approved_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text)
    generation_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=True
    )
    production_context_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    overrides_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class Release(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "releases"
    __table_args__ = (UniqueConstraint("design_version_id"),)

    design_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("design_versions.id", ondelete="CASCADE")
    )
    release_number: Mapped[str] = mapped_column(String(80))
    released_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    manifest_sha256: Mapped[str] = mapped_column(String(64))


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
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(180))


class ProductionRecord(IdMixin, TimestampMixin, TenantMixin):
    design_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("design_versions.id", ondelete="CASCADE"), index=True
    )
    stable_key: Mapped[str] = mapped_column(String(160))
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ParameterDefinition(ProductionRecord, Base):
    __tablename__ = "parameter_definitions"


class ParameterValue(ProductionRecord, Base):
    __tablename__ = "parameter_values"


class Part(ProductionRecord, Base):
    __tablename__ = "parts"


class PartFace(ProductionRecord, Base):
    __tablename__ = "part_faces"


class ManufacturingFeature(ProductionRecord, Base):
    __tablename__ = "manufacturing_features"


class Constraint(ProductionRecord, Base):
    __tablename__ = "constraints"


class LoadCase(ProductionRecord, Base):
    __tablename__ = "load_cases"


class RuleEvaluationRecord(ProductionRecord, Base):
    __tablename__ = "rule_evaluations"


class AssemblyGraphRecord(ProductionRecord, Base):
    __tablename__ = "assembly_graphs"


class AssemblyStepRecord(ProductionRecord, Base):
    __tablename__ = "assembly_steps"


class BOMLine(ProductionRecord, Base):
    __tablename__ = "bom_lines"


class CutListLine(ProductionRecord, Base):
    __tablename__ = "cut_list_lines"


class NestingLayout(ProductionRecord, Base):
    __tablename__ = "nesting_layouts"


class SetupRecord(ProductionRecord, Base):
    __tablename__ = "setups"


class CAMOperationRecord(ProductionRecord, Base):
    __tablename__ = "cam_operations"


class Toolpath(ProductionRecord, Base):
    __tablename__ = "toolpaths"
