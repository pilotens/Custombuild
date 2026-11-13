"""Run furniture review exports through durable generation jobs and artifacts.

Revision ID: 0022_durable_furniture_review_exports
Revises: 0021_furniture_review_reads

Furniture review jobs deliberately share the established generation lease,
outbox, storage-ledger and artifact tables.  They own a project snapshot
directly rather than inventing a production DesignVersion.
"""

from __future__ import annotations

# ruff: noqa: S608 -- SQL fragments are fixed migration text, never runtime input.
import sqlalchemy as sa
from alembic import op

revision = "0022_durable_furniture_review_exports"
down_revision = "0021_furniture_review_reads"
branch_labels = None
depends_on = None

OWNER_CHECK = "ck_generation_jobs_type_owner"
PROJECT_FK = "fk_generation_jobs_org_project"
PROJECT_ID_INDEX = "ix_generation_jobs_project_id"
PROJECT_INDEX = "ix_generation_jobs_project_type_created"


def _domain_reference_function(*, supports_furniture_review: bool) -> str:
    if supports_furniture_review:
        project_lookup = """
        SELECT COALESCE(generation_job.project_id, design_version.project_id)
        INTO v_project_id
        FROM public.generation_jobs AS generation_job
        LEFT JOIN public.design_versions AS design_version
          ON design_version.organization_id = generation_job.organization_id
         AND design_version.id = generation_job.design_version_id
        WHERE generation_job.organization_id = NEW.organization_id
          AND generation_job.id = NEW.generation_job_id
          AND (
              (generation_job.job_type = 'production'
               AND generation_job.project_id IS NULL
               AND design_version.id IS NOT NULL)
              OR
              (generation_job.job_type = 'furniture_review'
               AND generation_job.design_version_id IS NULL
               AND generation_job.project_id IS NOT NULL)
          );
        """
    else:
        project_lookup = """
        SELECT design_version.project_id INTO v_project_id
        FROM public.generation_jobs AS generation_job
        JOIN public.design_versions AS design_version
          ON design_version.organization_id = generation_job.organization_id
         AND design_version.id = generation_job.design_version_id
        WHERE generation_job.organization_id = NEW.organization_id
          AND generation_job.id = NEW.generation_job_id;
        """
    return f"""
CREATE OR REPLACE FUNCTION public._custombuild_storage_enforce_domain_reference(
) RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_media_type text;
    v_project_id text;
    v_owner_type text;
    v_owner_id text;
    v_idempotency_key text;
BEGIN
    IF TG_TABLE_SCHEMA IS DISTINCT FROM 'public'
       OR TG_TABLE_NAME NOT IN ('imported_assets', 'external_evidence', 'artifacts') THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'STORAGE_DOMAIN_REFERENCE_INVALID: unexpected trigger target';
    END IF;
    IF TG_TABLE_NAME = 'imported_assets' THEN
        v_media_type := NEW.media_type;
        v_project_id := NEW.project_id;
        v_owner_type := 'imported_asset';
        v_owner_id := NEW.id;
        v_idempotency_key := 'imported:' || NEW.id;
    ELSIF TG_TABLE_NAME = 'external_evidence' THEN
        v_media_type := NEW.content_type;
        v_project_id := NEW.project_id;
        v_owner_type := 'external_evidence';
        v_owner_id := NEW.id;
        v_idempotency_key := 'external-evidence:' || NEW.id;
    ELSE
        v_media_type := NEW.content_type;
        v_owner_type := 'generation_job';
        v_owner_id := NEW.generation_job_id;
        v_idempotency_key := 'generation:' || NEW.generation_job_id || ':'
            || NEW.kind || ':' || NEW.id;
        {project_lookup}
        IF NOT FOUND OR v_project_id IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23503',
                MESSAGE = 'STORAGE_DOMAIN_REFERENCE_INVALID: artifact owner graph is missing';
        END IF;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM public.stored_objects AS stored
        WHERE stored.organization_id = NEW.organization_id
          AND stored.object_key = NEW.object_key
          AND stored.state = 'committed'
          AND stored.project_id IS NOT DISTINCT FROM v_project_id
          AND stored.sha256 IS NOT DISTINCT FROM NEW.sha256
          AND stored.size_bytes IS NOT DISTINCT FROM NEW.size_bytes
          AND stored.media_type IS NOT DISTINCT FROM v_media_type
          AND stored.owner_type IS NOT DISTINCT FROM v_owner_type
          AND stored.owner_id IS NOT DISTINCT FROM v_owner_id
          AND stored.idempotency_key IS NOT DISTINCT FROM v_idempotency_key
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23503',
            MESSAGE = 'STORAGE_DOMAIN_REFERENCE_INVALID: reference must match an '
                || 'exact committed storage ledger identity';
    END IF;
    RETURN NEW;
END;
$function$
"""


def _furniture_job_count(bind: sa.Connection) -> int:
    if bind.dialect.name != "postgresql":
        return int(
            bind.scalar(
                sa.text(
                    "SELECT count(*) FROM generation_jobs "
                    "WHERE job_type = 'furniture_review' OR design_version_id IS NULL"
                )
            )
            or 0
        )
    organization_ids = tuple(
        str(value)
        for value in bind.execute(sa.text("SELECT id FROM organizations ORDER BY id")).scalars()
    )
    count = 0
    try:
        for organization_id in organization_ids:
            bind.execute(
                sa.text("SELECT set_config('app.current_organization_id', :value, true)"),
                {"value": organization_id},
            )
            count += int(
                bind.scalar(
                    sa.text(
                        "SELECT count(*) FROM generation_jobs "
                        "WHERE job_type = 'furniture_review' OR design_version_id IS NULL"
                    )
                )
                or 0
            )
    finally:
        bind.execute(sa.text("SELECT set_config('app.current_organization_id', '', true)"))
    return count


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text("SELECT pg_catalog.set_config('search_path', 'pg_catalog,public', true)")
        )
    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "job_type",
                sa.String(length=32),
                nullable=False,
                server_default="production",
            )
        )
        batch_op.add_column(sa.Column("project_id", sa.String(length=36), nullable=True))
        batch_op.alter_column(
            "design_version_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
        batch_op.create_foreign_key(
            PROJECT_FK,
            "projects",
            ["organization_id", "project_id"],
            ["organization_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            OWNER_CHECK,
            "(job_type = 'production' AND design_version_id IS NOT NULL "
            "AND project_id IS NULL) OR "
            "(job_type = 'furniture_review' AND design_version_id IS NULL "
            "AND project_id IS NOT NULL)",
        )
        batch_op.create_index(
            PROJECT_ID_INDEX,
            ["project_id"],
            unique=False,
        )
        batch_op.create_index(
            PROJECT_INDEX,
            ["project_id", "job_type", "created_at"],
            unique=False,
        )
    if bind.dialect.name == "postgresql":
        op.execute(_domain_reference_function(supports_furniture_review=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text("SELECT pg_catalog.set_config('search_path', 'pg_catalog,public', true)")
        )
    if _furniture_job_count(bind) != 0:
        raise RuntimeError(
            "FURNITURE_EXPORT_DOWNGRADE_BLOCKED: durable review jobs or artifacts would be lost"
        )
    if bind.dialect.name == "postgresql":
        op.execute(_domain_reference_function(supports_furniture_review=False))
    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.drop_index(PROJECT_INDEX)
        batch_op.drop_index(PROJECT_ID_INDEX)
        batch_op.drop_constraint(OWNER_CHECK, type_="check")
        batch_op.drop_constraint(PROJECT_FK, type_="foreignkey")
        batch_op.alter_column(
            "design_version_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.drop_column("project_id")
        batch_op.drop_column("job_type")
