"""Add a monotonic production high-water mark for retention trust.

Revision ID: 0018_joint_retention_registry_state
Revises: 0017_oidc_issuer_binding

The portable v1 registry document remains unchanged.  This revision stores its
exact canonical representation and exposes only two reviewed PostgreSQL
functions: a migrator-only monotonic activation and an API/worker exact-match
assertion.  Runtime processes can never auto-adopt a registry.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_joint_retention_registry_state"
down_revision = "0017_oidc_issuer_binding"
branch_labels = None
depends_on = None

REGISTRY_ACTIVATION_LOCK_ID = 4_340_449_326_452_121_818
TABLE_NAME = "joint_retention_registry_state"
INSTALL_FUNCTION = "public.custombuild_joint_retention_install_registry(jsonb,text,text,text)"
ASSERT_FUNCTION = "public.custombuild_joint_retention_assert_registry(text,text)"


def _configure_postgresql_security() -> None:
    op.execute(sa.text("SELECT pg_catalog.set_config('search_path', 'pg_catalog,public', true)"))
    op.execute(
        """
        CREATE FUNCTION public.custombuild_joint_retention_install_registry(
          p_registry pg_catalog.jsonb,
          p_registry_canonical_json pg_catalog.text,
          p_registry_sha256 pg_catalog.text,
          p_operator_reference_sha256 pg_catalog.text
        )
        RETURNS TABLE (activated_epoch pg_catalog.bigint, changed pg_catalog.boolean)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO pg_catalog, public
        AS $function$
        DECLARE
          v_state public.joint_retention_registry_state%ROWTYPE;
          v_old_issuer pg_catalog.jsonb;
          v_new_issuer pg_catalog.jsonb;
          v_next_epoch pg_catalog.bigint;
        BEGIN
          PERFORM pg_catalog.pg_advisory_xact_lock(4340449326452121818);
          IF p_registry IS NULL
             OR pg_catalog.jsonb_typeof(p_registry) <> 'object'
             OR p_registry ->> 'schema_version'
                <> 'custombuild.joint-retention-trust-registry.v1'
             OR pg_catalog.jsonb_typeof(p_registry -> 'issuers') <> 'array'
             OR pg_catalog.jsonb_typeof(
                  p_registry -> 'revoked_statement_sha256'
                ) <> 'array'
             OR pg_catalog.jsonb_typeof(
                  p_registry -> 'revoked_system_versions'
                ) <> 'array'
             OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_registry)) <> 4
          THEN
            RAISE EXCEPTION USING
              ERRCODE = '22023',
              MESSAGE = 'JOINT_RETENTION_REGISTRY_INVALID: registry shape is invalid';
          END IF;
          IF p_registry_canonical_json IS NULL
             OR pg_catalog.octet_length(p_registry_canonical_json) < 1
             OR pg_catalog.octet_length(p_registry_canonical_json) > 262144
             OR p_registry <> p_registry_canonical_json::pg_catalog.jsonb
          THEN
            RAISE EXCEPTION USING
              ERRCODE = '22023',
              MESSAGE = 'JOINT_RETENTION_REGISTRY_INVALID: canonical registry mismatch';
          END IF;
          IF p_registry_sha256 IS NULL
             OR p_registry_sha256 !~ '^[0-9a-f]{64}$'
             OR p_operator_reference_sha256 IS NULL
             OR p_operator_reference_sha256 !~ '^[0-9a-f]{64}$'
          THEN
            RAISE EXCEPTION USING
              ERRCODE = '22023',
              MESSAGE = 'JOINT_RETENTION_REGISTRY_INVALID: digest format is invalid';
          END IF;
          FOR v_new_issuer IN
            SELECT candidate.value
            FROM pg_catalog.jsonb_array_elements(p_registry -> 'issuers') candidate(value)
          LOOP
            IF pg_catalog.jsonb_typeof(v_new_issuer) <> 'object'
               OR (SELECT pg_catalog.count(*)
                   FROM pg_catalog.jsonb_object_keys(v_new_issuer)) <> 7
               OR NOT (v_new_issuer ? 'issuer_id')
               OR NOT (v_new_issuer ? 'key_id')
               OR NOT (v_new_issuer ? 'role')
               OR NOT (v_new_issuer ? 'public_key_base64')
               OR NOT (v_new_issuer ? 'not_before')
               OR NOT (v_new_issuer ? 'not_after')
               OR NOT (v_new_issuer ? 'revoked_at')
               OR pg_catalog.jsonb_typeof(v_new_issuer -> 'issuer_id') <> 'string'
               OR pg_catalog.jsonb_typeof(v_new_issuer -> 'key_id') <> 'string'
               OR pg_catalog.jsonb_typeof(v_new_issuer -> 'role') <> 'string'
               OR v_new_issuer ->> 'role' <> 'joint_retention_certifier'
               OR pg_catalog.jsonb_typeof(v_new_issuer -> 'public_key_base64') <> 'string'
               OR pg_catalog.octet_length(
                    v_new_issuer ->> 'public_key_base64'
                  ) <> 44
               OR v_new_issuer ->> 'public_key_base64'
                    !~ '^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$'
               OR pg_catalog.jsonb_typeof(v_new_issuer -> 'not_before') <> 'string'
               OR pg_catalog.jsonb_typeof(v_new_issuer -> 'not_after') <> 'string'
               OR pg_catalog.jsonb_typeof(v_new_issuer -> 'revoked_at')
                    NOT IN ('null', 'string')
            THEN
              RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'JOINT_RETENTION_REGISTRY_INVALID: issuer shape is invalid';
            END IF;
          END LOOP;
          IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(p_registry -> 'issuers') candidate(value)
            GROUP BY candidate.value ->> 'issuer_id', candidate.value ->> 'key_id'
            HAVING pg_catalog.count(*) <> 1
          ) THEN
            RAISE EXCEPTION USING
              ERRCODE = '22023',
              MESSAGE = 'JOINT_RETENTION_REGISTRY_INVALID: issuer identity is duplicated';
          END IF;

          SELECT * INTO STRICT v_state
          FROM public.joint_retention_registry_state
          WHERE id = 1
          FOR UPDATE;
          IF v_state.transition_epoch = 0 THEN
            IF v_state.registry_sha256 IS NOT NULL
               OR v_state.registry_canonical_json IS NOT NULL
               OR v_state.normalized_registry_json IS NOT NULL
               OR v_state.operator_reference_sha256 IS NOT NULL
            THEN
              RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'JOINT_RETENTION_REGISTRY_STATE_INVALID: partial initial state';
            END IF;
          ELSE
            IF v_state.registry_sha256 IS NULL
               OR v_state.registry_canonical_json IS NULL
               OR v_state.normalized_registry_json IS NULL
               OR v_state.operator_reference_sha256 IS NULL
            THEN
              RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'JOINT_RETENTION_REGISTRY_STATE_INVALID: partial activated state';
            END IF;
            IF v_state.registry_sha256 = p_registry_sha256 THEN
              IF v_state.registry_canonical_json <> p_registry_canonical_json
                 OR v_state.normalized_registry_json <> p_registry
              THEN
                RAISE EXCEPTION USING
                  ERRCODE = '55000',
                  MESSAGE = 'JOINT_RETENTION_REGISTRY_DIGEST_COLLISION: bytes differ';
              END IF;
              RETURN QUERY SELECT v_state.transition_epoch, false;
              RETURN;
            END IF;
            IF v_state.registry_canonical_json = p_registry_canonical_json
               OR v_state.normalized_registry_json = p_registry
            THEN
              RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'JOINT_RETENTION_REGISTRY_DIGEST_MISMATCH: unchanged bytes';
            END IF;
            IF NOT (
              (p_registry -> 'revoked_statement_sha256')
              @> (v_state.normalized_registry_json -> 'revoked_statement_sha256')
            ) OR NOT (
              (p_registry -> 'revoked_system_versions')
              @> (v_state.normalized_registry_json -> 'revoked_system_versions')
            ) THEN
              RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'JOINT_RETENTION_REGISTRY_ROLLBACK: revocations cannot be removed';
            END IF;
            IF EXISTS (
              SELECT 1
              FROM pg_catalog.jsonb_array_elements(p_registry -> 'issuers') candidate(value)
              JOIN pg_catalog.jsonb_array_elements(
                v_state.normalized_registry_json -> 'issuers'
              ) activated(value)
                ON candidate.value ->> 'public_key_base64'
                   = activated.value ->> 'public_key_base64'
              WHERE candidate.value ->> 'issuer_id'
                       IS DISTINCT FROM activated.value ->> 'issuer_id'
                 OR candidate.value ->> 'key_id'
                       IS DISTINCT FROM activated.value ->> 'key_id'
            ) THEN
              RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'JOINT_RETENTION_REGISTRY_ROLLBACK: issuer key material rebound';
            END IF;
            FOR v_old_issuer IN
              SELECT activated.value
              FROM pg_catalog.jsonb_array_elements(
                v_state.normalized_registry_json -> 'issuers'
              ) activated(value)
            LOOP
              SELECT candidate.value INTO v_new_issuer
              FROM pg_catalog.jsonb_array_elements(p_registry -> 'issuers') candidate(value)
              WHERE candidate.value ->> 'issuer_id' = v_old_issuer ->> 'issuer_id'
                AND candidate.value ->> 'key_id' = v_old_issuer ->> 'key_id';
              IF NOT FOUND THEN
                RAISE EXCEPTION USING
                  ERRCODE = '55000',
                  MESSAGE = 'JOINT_RETENTION_REGISTRY_ROLLBACK: issuer key cannot be removed';
              END IF;
              IF (v_new_issuer - 'revoked_at')
                   IS DISTINCT FROM (v_old_issuer - 'revoked_at')
              THEN
                RAISE EXCEPTION USING
                  ERRCODE = '55000',
                  MESSAGE = 'JOINT_RETENTION_REGISTRY_ROLLBACK: issuer key is immutable';
              END IF;
              IF (v_old_issuer -> 'revoked_at') IS DISTINCT FROM 'null'::pg_catalog.jsonb
                 AND (
                   (v_new_issuer -> 'revoked_at')
                     IS NOT DISTINCT FROM 'null'::pg_catalog.jsonb
                   OR (v_new_issuer ->> 'revoked_at')::pg_catalog.timestamptz
                        > (v_old_issuer ->> 'revoked_at')::pg_catalog.timestamptz
                 )
              THEN
                RAISE EXCEPTION USING
                  ERRCODE = '55000',
                  MESSAGE = 'JOINT_RETENTION_REGISTRY_ROLLBACK: revocation delayed or cleared';
              END IF;
            END LOOP;
          END IF;

          IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(p_registry -> 'issuers') candidate(value)
            GROUP BY candidate.value ->> 'public_key_base64'
            HAVING pg_catalog.count(*) <> 1
          ) THEN
            RAISE EXCEPTION USING
              ERRCODE = '22023',
              MESSAGE = 'JOINT_RETENTION_REGISTRY_INVALID: issuer key material is duplicated';
          END IF;

          v_next_epoch := v_state.transition_epoch + 1;
          UPDATE public.joint_retention_registry_state
          SET transition_epoch = v_next_epoch,
              registry_sha256 = p_registry_sha256,
              registry_canonical_json = p_registry_canonical_json,
              normalized_registry_json = p_registry,
              operator_reference_sha256 = p_operator_reference_sha256,
              updated_at = pg_catalog.clock_timestamp()
          WHERE id = 1;
          IF NOT FOUND THEN
            RAISE EXCEPTION USING
              ERRCODE = '55000',
              MESSAGE = 'JOINT_RETENTION_REGISTRY_STATE_INVALID: singleton missing';
          END IF;
          RETURN QUERY SELECT v_next_epoch, true;
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.custombuild_joint_retention_assert_registry(
          p_registry_canonical_json pg_catalog.text,
          p_registry_sha256 pg_catalog.text
        )
        RETURNS pg_catalog.bigint
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO pg_catalog, public
        AS $function$
        DECLARE
          v_epoch pg_catalog.bigint;
          v_registry_sha256 pg_catalog.text;
          v_registry_canonical_json pg_catalog.text;
        BEGIN
          PERFORM pg_catalog.pg_advisory_xact_lock_shared(4340449326452121818);
          SELECT transition_epoch, registry_sha256, registry_canonical_json
          INTO v_epoch, v_registry_sha256, v_registry_canonical_json
          FROM public.joint_retention_registry_state
          WHERE id = 1;
          IF NOT FOUND OR v_epoch < 1 OR v_registry_sha256 IS NULL
             OR v_registry_canonical_json IS NULL
          THEN
            RAISE EXCEPTION USING
              ERRCODE = '55000',
              MESSAGE = 'JOINT_RETENTION_REGISTRY_NOT_ACTIVATED';
          END IF;
          IF p_registry_sha256 IS NULL
             OR p_registry_sha256 !~ '^[0-9a-f]{64}$'
             OR p_registry_canonical_json IS NULL
             OR v_registry_sha256 <> p_registry_sha256
             OR v_registry_canonical_json <> p_registry_canonical_json
          THEN
            RAISE EXCEPTION USING
              ERRCODE = '55000',
              MESSAGE = 'JOINT_RETENTION_REGISTRY_MISMATCH';
          END IF;
          RETURN v_epoch;
        END;
        $function$
        """
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{TABLE_NAME} FROM PUBLIC")
    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE public.{TABLE_NAME} "
        "FROM custombuild_api, custombuild_worker, custombuild_storage_attestor"
    )
    for function_name in (INSTALL_FUNCTION, ASSERT_FUNCTION):
        op.execute(f"REVOKE ALL ON FUNCTION {function_name} FROM PUBLIC")
        op.execute(
            f"REVOKE ALL ON FUNCTION {function_name} "
            "FROM custombuild_api, custombuild_worker, custombuild_storage_attestor"
        )
    op.execute(
        """
        DO $acl$
        DECLARE
          v_column pg_catalog.name;
          v_function pg_catalog.regprocedure;
          v_grantee pg_catalog.name;
        BEGIN
          FOR v_grantee IN
            SELECT grantee.rolname
            FROM pg_catalog.pg_class relation
            JOIN pg_catalog.pg_namespace namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
              relation.relacl,
              pg_catalog.acldefault('r', relation.relowner)
            )) acl
            JOIN pg_catalog.pg_roles grantee ON grantee.oid = acl.grantee
            WHERE namespace.nspname = 'public'
              AND relation.relname = 'joint_retention_registry_state'
              AND acl.grantee <> relation.relowner
          LOOP
            EXECUTE pg_catalog.format(
              'REVOKE ALL PRIVILEGES ON TABLE public.joint_retention_registry_state FROM %I',
              v_grantee
            );
          END LOOP;

          FOR v_column, v_grantee IN
            SELECT attribute.attname,
                   CASE WHEN acl.grantee = 0 THEN NULL ELSE grantee.rolname END
            FROM pg_catalog.pg_attribute attribute
            JOIN pg_catalog.pg_class relation ON relation.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) acl
            LEFT JOIN pg_catalog.pg_roles grantee ON grantee.oid = acl.grantee
            WHERE namespace.nspname = 'public'
              AND relation.relname = 'joint_retention_registry_state'
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
              AND attribute.attacl IS NOT NULL
              AND acl.grantee <> relation.relowner
          LOOP
            IF v_grantee IS NULL THEN
              EXECUTE pg_catalog.format(
                'REVOKE ALL PRIVILEGES (%I) ON ' ||
                'public.joint_retention_registry_state FROM PUBLIC',
                v_column
              );
            ELSE
              EXECUTE pg_catalog.format(
                'REVOKE ALL PRIVILEGES (%I) ON ' ||
                'public.joint_retention_registry_state FROM %I',
                v_column,
                v_grantee
              );
            END IF;
          END LOOP;

          FOR v_function, v_grantee IN
            SELECT procedure.oid::pg_catalog.regprocedure, grantee.rolname
            FROM pg_catalog.pg_proc procedure
            CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
              procedure.proacl,
              pg_catalog.acldefault('f', procedure.proowner)
            )) acl
            JOIN pg_catalog.pg_roles grantee ON grantee.oid = acl.grantee
            WHERE procedure.oid = ANY(CAST(ARRAY[
              'public.custombuild_joint_retention_install_registry(jsonb,text,text,text)',
              'public.custombuild_joint_retention_assert_registry(text,text)'
            ] AS pg_catalog.regprocedure[]))
              AND acl.grantee <> procedure.proowner
          LOOP
            EXECUTE pg_catalog.format(
              'REVOKE ALL ON FUNCTION %s FROM %I',
              v_function,
              v_grantee
            );
          END LOOP;
        END;
        $acl$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {INSTALL_FUNCTION} TO custombuild_migrator")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {ASSERT_FUNCTION} "
        "TO custombuild_migrator, custombuild_api, custombuild_worker"
    )


def upgrade() -> None:
    bind = op.get_bind()
    registry_json_type: sa.types.TypeEngine[object]
    if bind.dialect.name == "postgresql":
        registry_json_type = postgresql.JSONB(astext_type=sa.Text())
    else:
        registry_json_type = sa.JSON()
    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.SmallInteger(), nullable=False, autoincrement=False),
        sa.Column(
            "transition_epoch",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("registry_sha256", sa.String(length=64), nullable=True),
        sa.Column("registry_canonical_json", sa.Text(), nullable=True),
        sa.Column("normalized_registry_json", registry_json_type, nullable=True),
        sa.Column("operator_reference_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "id = 1",
            name="ck_joint_retention_registry_state_singleton",
        ),
        sa.CheckConstraint(
            "transition_epoch >= 0",
            name="ck_joint_retention_registry_state_epoch",
        ),
        sa.CheckConstraint(
            "(transition_epoch = 0 AND registry_sha256 IS NULL "
            "AND registry_canonical_json IS NULL "
            "AND normalized_registry_json IS NULL "
            "AND operator_reference_sha256 IS NULL) OR "
            "(transition_epoch > 0 AND registry_sha256 IS NOT NULL "
            "AND registry_canonical_json IS NOT NULL "
            "AND normalized_registry_json IS NOT NULL "
            "AND operator_reference_sha256 IS NOT NULL)",
            name="ck_joint_retention_registry_state_activation",
        ),
        sa.CheckConstraint(
            "registry_sha256 IS NULL OR (length(registry_sha256) = 64 "
            "AND registry_sha256 = lower(registry_sha256))",
            name="ck_joint_retention_registry_state_registry_sha256",
        ),
        sa.CheckConstraint(
            "operator_reference_sha256 IS NULL OR "
            "(length(operator_reference_sha256) = 64 "
            "AND operator_reference_sha256 = lower(operator_reference_sha256))",
            name="ck_joint_retention_registry_state_operator_sha256",
        ),
        sa.PrimaryKeyConstraint("id"),
        # The preceding OIDC migration deliberately freezes the transaction-local
        # search_path to ``pg_catalog,public``.  An unqualified CREATE TABLE would
        # therefore target pg_catalog on PostgreSQL and fail for the least-
        # privilege migrator.  Keep the persistence boundary explicit instead of
        # depending on whichever migration happened to run immediately before us.
        schema="public" if bind.dialect.name == "postgresql" else None,
    )
    if bind.dialect.name == "postgresql":
        insert_statement = sa.text(
            "INSERT INTO public.joint_retention_registry_state "
            "(id, transition_epoch, updated_at) VALUES (1, 0, CURRENT_TIMESTAMP)"
        )
    else:
        insert_statement = sa.text(
            "INSERT INTO joint_retention_registry_state "
            "(id, transition_epoch, updated_at) VALUES (1, 0, CURRENT_TIMESTAMP)"
        )
    op.execute(insert_statement)
    if bind.dialect.name == "postgresql":
        _configure_postgresql_security()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text("SELECT pg_catalog.set_config('search_path', 'pg_catalog,public', true)")
        )
        bind.execute(
            sa.text("SELECT pg_catalog.pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": REGISTRY_ACTIVATION_LOCK_ID},
        )
    if bind.dialect.name == "postgresql":
        epoch_query = sa.text(
            "SELECT transition_epoch FROM public.joint_retention_registry_state WHERE id = 1"
        )
    else:
        epoch_query = sa.text(
            "SELECT transition_epoch FROM joint_retention_registry_state WHERE id = 1"
        )
    epoch = bind.scalar(epoch_query)
    if epoch != 0:
        raise RuntimeError(
            "JOINT_RETENTION_REGISTRY_DOWNGRADE_BLOCKED: activated trust high-water "
            "state cannot be discarded; restore an approved pre-activation backup"
        )
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP FUNCTION IF EXISTS public.custombuild_joint_retention_assert_registry(text,text)"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS "
            "public.custombuild_joint_retention_install_registry(jsonb,text,text,text)"
        )
    op.drop_table(
        TABLE_NAME,
        schema="public" if bind.dialect.name == "postgresql" else None,
    )
