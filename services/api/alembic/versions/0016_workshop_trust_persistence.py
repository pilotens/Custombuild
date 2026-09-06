"""Add fail-closed persistence for executable workshop trust evidence.

Revision ID: 0016_workshop_trust_persistence
Revises: 0015_outbox_retry_schedule

This revision only installs a read-only persistence foundation.  It does not
grant either untrusted runtime a way to create or finalize workshop records.
Trusted, transaction-scoped database functions are deliberately deferred until
an executable production package exists and the object-storage sidecar domain
has been extended for workshop evidence and attestations.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0016_workshop_trust_persistence"
down_revision = "0015_outbox_retry_schedule"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "workshop_trust_states",
    "workshop_actors",
    "workshop_signer_principals",
    "workshop_issuer_keys",
    "workshop_policies",
    "workshop_runs",
    "workshop_run_programs",
    "workshop_nonce_sets",
    "workshop_nonces",
    "workshop_chain_acceptances",
    "workshop_acceptance_signers",
    "workshop_revocations",
)

# Nonce sets and trust-state rows are the only rows that a future reviewed
# SECURITY DEFINER function may transition.  Every other row is append-only.
IMMUTABLE_TABLES = (
    "workshop_actors",
    "workshop_signer_principals",
    "workshop_issuer_keys",
    "workshop_policies",
    "workshop_runs",
    "workshop_run_programs",
    "workshop_nonces",
    "workshop_chain_acceptances",
    "workshop_acceptance_signers",
    "workshop_revocations",
)

_PARENT_CONSTRAINTS = (
    (
        "design_versions",
        "uq_design_versions_org_project_id",
        ("organization_id", "project_id", "id"),
    ),
    (
        "generation_jobs",
        "uq_generation_jobs_org_version_id",
        ("organization_id", "design_version_id", "id"),
    ),
    (
        "releases",
        "uq_releases_org_id",
        ("organization_id", "id"),
    ),
    (
        "releases",
        "uq_releases_org_version_job_id",
        ("organization_id", "design_version_id", "generation_job_id", "id"),
    ),
)


def _id_column() -> sa.Column[str]:
    return sa.Column("id", sa.String(length=36), nullable=False)


def _organization_column() -> sa.Column[str]:
    return sa.Column("organization_id", sa.String(length=36), nullable=False)


def _created_at_column() -> sa.Column[datetime]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def _organization_fk(*, ondelete: str = "CASCADE") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        ondelete=ondelete,
    )


def _create_parent_constraints() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    pre_existing: list[str] = []
    for table_name, constraint_name, _columns in _PARENT_CONSTRAINTS:
        existing = {
            constraint.get("name")
            for constraint in inspector.get_unique_constraints(table_name)
        }
        if constraint_name in existing:
            pre_existing.append(f"{table_name}.{constraint_name}")

    # Downgrade must only remove constraints owned by this revision.  Alembic
    # cannot reconstruct that provenance later, so accepting a pre-existing
    # constraint with one of our names would make rollback destructive.
    if pre_existing:
        raise RuntimeError(
            "WORKSHOP_PERSISTENCE_MIGRATION_CONFLICT: pre-existing parent "
            "constraints: " + ", ".join(pre_existing)
        )

    for table_name, constraint_name, columns in _PARENT_CONSTRAINTS:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.create_unique_constraint(constraint_name, list(columns))
        else:
            op.create_unique_constraint(constraint_name, table_name, list(columns))
        inspector = sa.inspect(bind)


def _create_identity_tables() -> None:
    op.create_table(
        "workshop_trust_states",
        _organization_column(),
        sa.Column(
            "trust_epoch",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        _organization_fk(),
        sa.CheckConstraint(
            "trust_epoch >= 0",
            name="ck_workshop_trust_states_epoch",
        ),
        sa.PrimaryKeyConstraint("organization_id"),
    )

    op.create_table(
        "workshop_actors",
        _id_column(),
        _organization_column(),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("external_authority", sa.String(length=160), nullable=True),
        sa.Column("external_subject_sha256", sa.String(length=64), nullable=True),
        _created_at_column(),
        _organization_fk(),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_workshop_actors_org_membership",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(actor_type = 'WORKFORCE_USER' AND user_id IS NOT NULL "
            "AND external_authority IS NULL AND external_subject_sha256 IS NULL) OR "
            "(actor_type = 'EXTERNAL_CERTIFIED_PERSON' AND user_id IS NULL "
            "AND external_authority IS NOT NULL AND length(external_authority) > 0 "
            "AND external_subject_sha256 IS NOT NULL "
            "AND length(external_subject_sha256) = 64 "
            "AND external_subject_sha256 = lower(external_subject_sha256))",
            name="ck_workshop_actors_identity_shape",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_workshop_actors_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_workshop_actors_org_user",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "external_authority",
            "external_subject_sha256",
            name="uq_workshop_actors_org_external_subject",
        ),
    )

    op.create_table(
        "workshop_signer_principals",
        _id_column(),
        _organization_column(),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=160), nullable=False),
        sa.Column("signer_role", sa.String(length=24), nullable=False),
        _created_at_column(),
        _organization_fk(),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_id"],
            ["workshop_actors.organization_id", "workshop_actors.id"],
            name="fk_workshop_signer_principals_org_actor",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "signer_role IN "
            "('workshop_maker', 'workshop_checker', 'workshop_supervisor')",
            name="ck_workshop_signer_principals_role",
        ),
        sa.CheckConstraint(
            "length(principal_id) > 0",
            name="ck_workshop_signer_principals_identity",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_workshop_signer_principals_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "principal_id",
            name="uq_workshop_signer_principals_org_principal",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "actor_id",
            name="uq_workshop_signer_principals_org_actor",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            "actor_id",
            name="uq_workshop_signer_principals_org_id_actor",
        ),
    )

    op.create_table(
        "workshop_issuer_keys",
        _id_column(),
        _organization_column(),
        sa.Column("signer_principal_id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column("key_id", sa.String(length=160), nullable=False),
        sa.Column("public_key_base64", sa.String(length=44), nullable=False),
        sa.Column("public_key_sha256", sa.String(length=64), nullable=False),
        sa.Column("qualified_pre_cut", sa.Boolean(), nullable=False),
        sa.Column("qualified_reference_part", sa.Boolean(), nullable=False),
        sa.Column("qualified_final_workshop", sa.Boolean(), nullable=False),
        sa.Column("qualified_air_cut_supervisor", sa.Boolean(), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        _created_at_column(),
        _organization_fk(),
        sa.ForeignKeyConstraint(
            ["organization_id", "signer_principal_id", "actor_id"],
            [
                "workshop_signer_principals.organization_id",
                "workshop_signer_principals.id",
                "workshop_signer_principals.actor_id",
            ],
            name="fk_workshop_issuer_keys_org_principal_actor",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(key_id) > 0 AND length(public_key_base64) = 44",
            name="ck_workshop_issuer_keys_identity",
        ),
        sa.CheckConstraint(
            "length(public_key_sha256) = 64 "
            "AND public_key_sha256 = lower(public_key_sha256)",
            name="ck_workshop_issuer_keys_sha256",
        ),
        sa.CheckConstraint(
            "not_before < not_after",
            name="ck_workshop_issuer_keys_validity",
        ),
        sa.CheckConstraint(
            "qualified_pre_cut OR qualified_reference_part "
            "OR qualified_final_workshop OR qualified_air_cut_supervisor",
            name="ck_workshop_issuer_keys_qualification",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_workshop_issuer_keys_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            "actor_id",
            name="uq_workshop_issuer_keys_org_id_actor",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "signer_principal_id",
            "key_id",
            name="uq_workshop_issuer_keys_org_principal_key",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "public_key_sha256",
            name="uq_workshop_issuer_keys_org_public_key",
        ),
    )


def _create_policy_and_run_tables() -> None:
    op.create_table(
        "workshop_policies",
        _id_column(),
        _organization_column(),
        sa.Column("policy_id", sa.String(length=160), nullable=False),
        sa.Column("policy_version", sa.String(length=160), nullable=False),
        sa.Column("schema_version", sa.String(length=160), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("canonical_json_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_by_actor_id", sa.String(length=36), nullable=False),
        _created_at_column(),
        _organization_fk(),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_actor_id"],
            ["workshop_actors.organization_id", "workshop_actors.id"],
            name="fk_workshop_policies_org_creator",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(policy_id) > 0 AND length(policy_version) > 0 "
            "AND length(schema_version) > 0",
            name="ck_workshop_policies_identity",
        ),
        sa.CheckConstraint(
            "length(policy_sha256) = 64 AND policy_sha256 = lower(policy_sha256)",
            name="ck_workshop_policies_sha256",
        ),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 4194304 "
            "AND length(canonical_json_bytes) = size_bytes",
            name="ck_workshop_policies_bytes",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_workshop_policies_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            "policy_sha256",
            name="uq_workshop_policies_org_id_sha",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "policy_id",
            "policy_version",
            name="uq_workshop_policies_org_identity",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "policy_sha256",
            name="uq_workshop_policies_org_sha",
        ),
    )

    op.create_table(
        "workshop_runs",
        _id_column(),
        _organization_column(),
        sa.Column("schema_version", sa.String(length=160), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("design_version_id", sa.String(length=36), nullable=False),
        sa.Column("design_review_release_id", sa.String(length=36), nullable=False),
        sa.Column("generation_job_id", sa.String(length=36), nullable=False),
        sa.Column("generation_finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("design_hash", sa.String(length=64), nullable=False),
        sa.Column("production_context_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("bundle_sha256", sa.String(length=64), nullable=False),
        sa.Column("operations_sha256", sa.String(length=64), nullable=False),
        sa.Column("generation_plan_sha256", sa.String(length=64), nullable=False),
        sa.Column("policy_record_id", sa.String(length=36), nullable=False),
        sa.Column("workshop_policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("machine_program_kind", sa.String(length=16), nullable=False),
        sa.Column("machine_program_set_sha256", sa.String(length=64), nullable=False),
        sa.Column("postprocessor_id", sa.String(length=160), nullable=False),
        sa.Column("postprocessor_version", sa.String(length=160), nullable=False),
        sa.Column("postprocessor_binary_sha256", sa.String(length=64), nullable=False),
        sa.Column("postprocessor_config_sha256", sa.String(length=64), nullable=False),
        sa.Column("run_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by_actor_id", sa.String(length=36), nullable=False),
        _created_at_column(),
        _organization_fk(),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id", "design_version_id"],
            [
                "design_versions.organization_id",
                "design_versions.project_id",
                "design_versions.id",
            ],
            name="fk_workshop_runs_org_project_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "design_version_id", "generation_job_id"],
            [
                "generation_jobs.organization_id",
                "generation_jobs.design_version_id",
                "generation_jobs.id",
            ],
            name="fk_workshop_runs_org_version_job",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["organization_id", "policy_record_id", "workshop_policy_sha256"],
            [
                "workshop_policies.organization_id",
                "workshop_policies.id",
                "workshop_policies.policy_sha256",
            ],
            name="fk_workshop_runs_org_policy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_actor_id"],
            ["workshop_actors.organization_id", "workshop_actors.id"],
            name="fk_workshop_runs_org_creator",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "machine_program_kind = 'EXECUTABLE'",
            name="ck_workshop_runs_executable_only",
        ),
        sa.CheckConstraint(
            "length(schema_version) > 0 AND length(postprocessor_id) > 0 "
            "AND length(postprocessor_version) > 0",
            name="ck_workshop_runs_identity",
        ),
        sa.CheckConstraint(
            "length(run_sha256) = 64 AND run_sha256 = lower(run_sha256) "
            "AND length(design_hash) = 64 AND design_hash = lower(design_hash) "
            "AND length(production_context_hash) = 64 "
            "AND production_context_hash = lower(production_context_hash) "
            "AND length(manifest_sha256) = 64 "
            "AND manifest_sha256 = lower(manifest_sha256) "
            "AND length(bundle_sha256) = 64 AND bundle_sha256 = lower(bundle_sha256) "
            "AND length(operations_sha256) = 64 "
            "AND operations_sha256 = lower(operations_sha256) "
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_workshop_runs_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            "run_sha256",
            "workshop_policy_sha256",
            name="uq_workshop_runs_org_id_run_policy",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "run_sha256",
            name="uq_workshop_runs_org_sha",
        ),
    )

    op.create_table(
        "workshop_run_programs",
        _id_column(),
        _organization_column(),
        sa.Column("workshop_run_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("program_id", sa.String(length=160), nullable=False),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("relative_path", sa.String(length=512), nullable=False),
        sa.Column("setup_id", sa.String(length=160), nullable=False),
        sa.Column("wcs_id", sa.String(length=160), nullable=False),
        sa.Column("stock_id", sa.String(length=160), nullable=False),
        sa.Column("operation_set_sha256", sa.String(length=64), nullable=False),
        sa.Column("program_sha256", sa.String(length=64), nullable=False),
        sa.Column("program_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=160), nullable=False),
        sa.Column("identity_sha256", sa.String(length=64), nullable=False),
        sa.Column("canonical_identity_json_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("identity_size_bytes", sa.Integer(), nullable=False),
        _created_at_column(),
        _organization_fk(),
        sa.ForeignKeyConstraint(
            ["organization_id", "workshop_run_id"],
            ["workshop_runs.organization_id", "workshop_runs.id"],
            name="fk_workshop_run_programs_org_run",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_workshop_run_programs_ordinal",
        ),
        sa.CheckConstraint(
            "length(program_id) > 0 AND length(purpose) > 0 "
            "AND length(relative_path) > 0 AND length(setup_id) > 0 "
            "AND length(wcs_id) > 0 AND length(stock_id) > 0 "
            "AND length(media_type) > 0",
            name="ck_workshop_run_programs_identity",
        ),
        sa.CheckConstraint(
            "length(operation_set_sha256) = 64 "
            "AND operation_set_sha256 = lower(operation_set_sha256) "
            "AND length(program_sha256) = 64 "
            "AND program_sha256 = lower(program_sha256) "
            "AND length(identity_sha256) = 64 "
            "AND identity_sha256 = lower(identity_sha256)",
            name="ck_workshop_run_programs_hashes",
        ),
        sa.CheckConstraint(
            "program_size_bytes > 0 AND identity_size_bytes > 0 "
            "AND identity_size_bytes <= 1048576 "
            "AND length(canonical_identity_json_bytes) = identity_size_bytes",
            name="ck_workshop_run_programs_bytes",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_workshop_run_programs_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workshop_run_id",
            "ordinal",
            name="uq_workshop_run_programs_org_run_ordinal",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workshop_run_id",
            "program_id",
            name="uq_workshop_run_programs_org_run_program_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workshop_run_id",
            "relative_path",
            name="uq_workshop_run_programs_org_run_path",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workshop_run_id",
            "identity_sha256",
            name="uq_workshop_run_programs_org_run_identity",
        ),
    )
    op.create_index(
        "uq_workshop_run_programs_reference_part",
        "workshop_run_programs",
        ["organization_id", "workshop_run_id"],
        unique=True,
        postgresql_where=sa.text("purpose = 'REFERENCE_PART'"),
        sqlite_where=sa.text("purpose = 'REFERENCE_PART'"),
    )


def _create_nonce_tables() -> None:
    op.create_table(
        "workshop_nonce_sets",
        _id_column(),
        _organization_column(),
        sa.Column("workshop_run_id", sa.String(length=36), nullable=False),
        sa.Column("run_sha256", sa.String(length=64), nullable=False),
        sa.Column("workshop_policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("nonce_derivation_scheme", sa.String(length=40), nullable=False),
        sa.Column("nonce_key_version", sa.String(length=160), nullable=False),
        sa.Column(
            "nonce_derivation_context",
            sa.LargeBinary(length=32),
            nullable=False,
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_chain_sha256", sa.String(length=64), nullable=True),
        sa.Column("issued_by_actor_id", sa.String(length=36), nullable=False),
        _created_at_column(),
        _organization_fk(),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["organization_id", "issued_by_actor_id"],
            ["workshop_actors.organization_id", "workshop_actors.id"],
            name="fk_workshop_nonce_sets_org_issuer",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_workshop_nonce_sets_generation",
        ),
        sa.CheckConstraint(
            "issued_at < expires_at",
            name="ck_workshop_nonce_sets_validity",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) > 0 AND length(nonce_key_version) > 0 "
            "AND length(nonce_derivation_context) = 32 "
            "AND nonce_derivation_scheme = 'CUSTOMBUILD-HMAC-SHA256-V1'",
            name="ck_workshop_nonce_sets_identity",
        ),
        sa.CheckConstraint(
            "length(run_sha256) = 64 AND run_sha256 = lower(run_sha256) "
            "AND length(workshop_policy_sha256) = 64 "
            "AND workshop_policy_sha256 = lower(workshop_policy_sha256)",
            name="ck_workshop_nonce_sets_hashes",
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL AND consumed_chain_sha256 IS NULL) OR "
            "(consumed_at IS NOT NULL AND consumed_at >= issued_at "
            "AND consumed_at <= expires_at "
            "AND consumed_chain_sha256 IS NOT NULL "
            "AND length(consumed_chain_sha256) = 64 "
            "AND consumed_chain_sha256 = lower(consumed_chain_sha256))",
            name="ck_workshop_nonce_sets_consumption",
        ),
        sa.CheckConstraint(
            "invalidated_at IS NULL OR "
            "(invalidated_at >= issued_at AND consumed_at IS NULL)",
            name="ck_workshop_nonce_sets_invalidation",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_workshop_nonce_sets_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            "workshop_run_id",
            "run_sha256",
            "workshop_policy_sha256",
            "generation",
            name="uq_workshop_nonce_sets_org_binding",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workshop_run_id",
            "generation",
            name="uq_workshop_nonce_sets_org_run_generation",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_workshop_nonce_sets_org_idempotency",
        ),
    )
    op.create_index(
        "uq_workshop_nonce_sets_active_run",
        "workshop_nonce_sets",
        ["organization_id", "workshop_run_id"],
        unique=True,
        postgresql_where=sa.text("invalidated_at IS NULL AND consumed_at IS NULL"),
        sqlite_where=sa.text("invalidated_at IS NULL AND consumed_at IS NULL"),
    )

    op.create_table(
        "workshop_nonces",
        _organization_column(),
        sa.Column("nonce_set_id", sa.String(length=36), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("workshop_run_id", sa.String(length=36), nullable=False),
        sa.Column("run_sha256", sa.String(length=64), nullable=False),
        sa.Column("workshop_policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("set_generation", sa.Integer(), nullable=False),
        sa.Column("nonce_digest_sha256", sa.String(length=64), nullable=False),
        _created_at_column(),
        _organization_fk(),
        sa.ForeignKeyConstraint(
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
        sa.CheckConstraint(
            "stage IN ('PRE_CUT', 'REFERENCE_PART', 'FINAL_WORKSHOP')",
            name="ck_workshop_nonces_stage",
        ),
        sa.CheckConstraint(
            "length(nonce_digest_sha256) = 64 "
            "AND nonce_digest_sha256 = lower(nonce_digest_sha256)",
            name="ck_workshop_nonces_digest",
        ),
        sa.PrimaryKeyConstraint("organization_id", "nonce_set_id", "stage"),
        sa.UniqueConstraint(
            "organization_id",
            "nonce_digest_sha256",
            name="uq_workshop_nonces_org_digest",
        ),
    )


def _create_acceptance_tables() -> None:
    op.create_table(
        "workshop_chain_acceptances",
        _id_column(),
        _organization_column(),
        sa.Column("workshop_run_id", sa.String(length=36), nullable=False),
        sa.Column("run_sha256", sa.String(length=64), nullable=False),
        sa.Column("workshop_policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("nonce_set_id", sa.String(length=36), nullable=False),
        sa.Column("nonce_set_generation", sa.Integer(), nullable=False),
        sa.Column("chain_sha256", sa.String(length=64), nullable=False),
        sa.Column("pre_cut_attestation_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "reference_part_attestation_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "final_workshop_attestation_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("pre_cut_statement_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "reference_part_statement_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "final_workshop_statement_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("final_attestation_id", sa.String(length=160), nullable=False),
        sa.Column("trust_epoch", sa.BigInteger(), nullable=False),
        sa.Column("registry_snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("eligibility", sa.String(length=40), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verifier_version", sa.String(length=160), nullable=False),
        sa.Column("verifier_source_sha256", sa.String(length=64), nullable=False),
        sa.Column("verified_by_actor_id", sa.String(length=36), nullable=False),
        _created_at_column(),
        _organization_fk(),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["organization_id", "verified_by_actor_id"],
            ["workshop_actors.organization_id", "workshop_actors.id"],
            name="fk_workshop_chain_acceptances_org_verifier",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "eligibility = 'VERIFIED_FOR_RELEASE_REVIEW'",
            name="ck_workshop_chain_acceptances_eligibility",
        ),
        sa.CheckConstraint(
            "trust_epoch >= 0 AND nonce_set_generation > 0 "
            "AND verified_at < valid_until",
            name="ck_workshop_chain_acceptances_validity",
        ),
        sa.CheckConstraint(
            "length(final_attestation_id) > 0 AND length(verifier_version) > 0",
            name="ck_workshop_chain_acceptances_identity",
        ),
        sa.CheckConstraint(
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
            "AND reference_part_attestation_sha256 = "
            "lower(reference_part_attestation_sha256) "
            "AND length(final_workshop_attestation_sha256) = 64 "
            "AND final_workshop_attestation_sha256 = "
            "lower(final_workshop_attestation_sha256) "
            "AND length(pre_cut_statement_sha256) = 64 "
            "AND pre_cut_statement_sha256 = lower(pre_cut_statement_sha256) "
            "AND length(reference_part_statement_sha256) = 64 "
            "AND reference_part_statement_sha256 = "
            "lower(reference_part_statement_sha256) "
            "AND length(final_workshop_statement_sha256) = 64 "
            "AND final_workshop_statement_sha256 = "
            "lower(final_workshop_statement_sha256)",
            name="ck_workshop_chain_acceptances_hashes",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_workshop_chain_acceptances_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "nonce_set_id",
            name="uq_workshop_chain_acceptances_org_nonce_set",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workshop_run_id",
            "chain_sha256",
            name="uq_workshop_chain_acceptances_org_run_chain",
        ),
    )
    op.create_index(
        "ix_workshop_chain_acceptances_current",
        "workshop_chain_acceptances",
        ["organization_id", "workshop_run_id", "trust_epoch", "valid_until"],
    )

    op.create_table(
        "workshop_acceptance_signers",
        _organization_column(),
        sa.Column("acceptance_id", sa.String(length=36), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("signer_role", sa.String(length=24), nullable=False),
        sa.Column("issuer_key_id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        _created_at_column(),
        _organization_fk(),
        sa.ForeignKeyConstraint(
            ["organization_id", "acceptance_id"],
            [
                "workshop_chain_acceptances.organization_id",
                "workshop_chain_acceptances.id",
            ],
            name="fk_workshop_acceptance_signers_org_acceptance",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "issuer_key_id", "actor_id"],
            [
                "workshop_issuer_keys.organization_id",
                "workshop_issuer_keys.id",
                "workshop_issuer_keys.actor_id",
            ],
            name="fk_workshop_acceptance_signers_org_key_actor",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "stage IN ('PRE_CUT', 'REFERENCE_PART', 'FINAL_WORKSHOP')",
            name="ck_workshop_acceptance_signers_stage",
        ),
        sa.CheckConstraint(
            "signer_role IN "
            "('workshop_maker', 'workshop_checker', 'workshop_supervisor') "
            "AND (signer_role <> 'workshop_supervisor' OR stage = 'PRE_CUT')",
            name="ck_workshop_acceptance_signers_role",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "acceptance_id",
            "stage",
            "signer_role",
        ),
    )

    op.create_table(
        "workshop_revocations",
        _id_column(),
        _organization_column(),
        sa.Column("target_kind", sa.String(length=24), nullable=False),
        sa.Column("target_sha256", sa.String(length=64), nullable=False),
        sa.Column("revocation_epoch", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("revoked_by_actor_id", sa.String(length=36), nullable=False),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        _organization_fk(),
        sa.ForeignKeyConstraint(
            ["organization_id", "revoked_by_actor_id"],
            ["workshop_actors.organization_id", "workshop_actors.id"],
            name="fk_workshop_revocations_org_actor",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "target_kind IN ('ISSUER_KEY', 'RUN', 'STATEMENT', "
            "'EVIDENCE_OBJECT', 'EVIDENCE_ATTACHMENT', 'EVIDENCE_CLAIM')",
            name="ck_workshop_revocations_target_kind",
        ),
        sa.CheckConstraint(
            "length(target_sha256) = 64 "
            "AND target_sha256 = lower(target_sha256)",
            name="ck_workshop_revocations_target_sha256",
        ),
        sa.CheckConstraint(
            "revocation_epoch > 0 AND length(reason) > 0 "
            "AND length(idempotency_key) > 0",
            name="ck_workshop_revocations_identity",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_workshop_revocations_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "target_kind",
            "target_sha256",
            name="uq_workshop_revocations_org_target",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "revocation_epoch",
            name="uq_workshop_revocations_org_epoch",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_workshop_revocations_org_idempotency",
        ),
    )


def _create_indexes() -> None:
    for table_name in (
        "workshop_actors",
        "workshop_signer_principals",
        "workshop_issuer_keys",
        "workshop_policies",
        "workshop_runs",
        "workshop_run_programs",
        "workshop_nonce_sets",
        "workshop_chain_acceptances",
        "workshop_revocations",
    ):
        op.create_index(
            f"ix_{table_name}_organization_id",
            table_name,
            ["organization_id"],
        )
    for column_name in (
        "project_id",
        "design_version_id",
        "design_review_release_id",
        "generation_job_id",
    ):
        op.create_index(
            f"ix_workshop_runs_{column_name}",
            "workshop_runs",
            [column_name],
        )


def _configure_postgresql_security() -> None:
    tables_sql = ", ".join(TENANT_TABLES)
    op.execute(
        """
        CREATE FUNCTION public.custombuild_workshop_initialize_trust_state()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO pg_catalog, public
        AS $function$
        DECLARE
          v_previous_organization_id text;
        BEGIN
          v_previous_organization_id :=
            current_setting('app.current_organization_id', true);
          PERFORM set_config('app.current_organization_id', NEW.id::text, true);
          INSERT INTO public.workshop_trust_states (
            organization_id,
            trust_epoch,
            updated_at
          )
          VALUES (NEW.id, 0, CURRENT_TIMESTAMP)
          ON CONFLICT (organization_id) DO NOTHING;
          PERFORM set_config(
            'app.current_organization_id',
            COALESCE(v_previous_organization_id, ''),
            true
          );
          RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER workshop_initialize_trust_state
        AFTER INSERT ON organizations
        FOR EACH ROW
        EXECUTE FUNCTION public.custombuild_workshop_initialize_trust_state()
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.custombuild_workshop_reject_immutable_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path TO pg_catalog, public
        AS $function$
        BEGIN
          RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'WORKSHOP_IMMUTABLE_ROW: workshop trust rows are append-only';
        END;
        $function$
        """
    )
    for table_name in IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_reject_mutation
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW
            EXECUTE FUNCTION public.custombuild_workshop_reject_immutable_mutation()
            """
        )
    for table_name in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY workshop_tenant_isolation ON {table_name}
            USING (
              organization_id::text =
                current_setting('app.current_organization_id', true)
            )
            WITH CHECK (
              organization_id::text =
                current_setting('app.current_organization_id', true)
            )
            """
        )
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {tables_sql} FROM PUBLIC")
    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE {tables_sql} "
        "FROM custombuild_api, custombuild_worker"
    )
    # This foundation is intentionally inaccessible to both untrusted
    # runtimes.  Future reads and finalization must be exposed only alongside
    # a reviewed tenant-binding and verifier boundary, never by broad table
    # privileges.
    for function_name in (
        "public.custombuild_workshop_initialize_trust_state()",
        "public.custombuild_workshop_reject_immutable_mutation()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {function_name} FROM PUBLIC")
        op.execute(
            f"REVOKE ALL ON FUNCTION {function_name} "
            "FROM custombuild_api, custombuild_worker"
        )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    conflicting_tables = sorted(set(inspector.get_table_names()) & set(TENANT_TABLES))
    if conflicting_tables:
        raise RuntimeError(
            "WORKSHOP_PERSISTENCE_MIGRATION_CONFLICT: pre-existing tables: "
            + ", ".join(conflicting_tables)
        )
    _create_parent_constraints()
    _create_identity_tables()
    _create_policy_and_run_tables()
    _create_nonce_tables()
    _create_acceptance_tables()
    _create_indexes()
    op.execute(
        sa.text(
            "INSERT INTO workshop_trust_states "
            "(organization_id, trust_epoch, updated_at) "
            "SELECT id, 0, CURRENT_TIMESTAMP FROM organizations"
        )
    )
    if op.get_bind().dialect.name == "postgresql":
        _configure_postgresql_security()


def _drop_parent_constraints() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table_name, constraint_name, _columns in reversed(_PARENT_CONSTRAINTS):
        existing = {
            constraint.get("name")
            for constraint in inspector.get_unique_constraints(table_name)
        }
        if constraint_name not in existing:
            continue
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.drop_constraint(constraint_name, type_="unique")
        else:
            op.drop_constraint(constraint_name, table_name, type_="unique")
        inspector = sa.inspect(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS workshop_initialize_trust_state ON organizations")
    for table_name in reversed(TENANT_TABLES):
        if table_name in existing_tables:
            op.drop_table(table_name)
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP FUNCTION IF EXISTS "
            "public.custombuild_workshop_reject_immutable_mutation()"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS "
            "public.custombuild_workshop_initialize_trust_state()"
        )
    _drop_parent_constraints()
