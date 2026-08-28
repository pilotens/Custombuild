"""Put storage-ledger mutations behind privileged database functions.

Revision ID: 0013_storage_quota_security_functions
Revises: 0012_storage_quota_ledger
"""

from __future__ import annotations

from alembic import op

revision = "0013_storage_quota_security_functions"
down_revision = "0012_storage_quota_ledger"
branch_labels = None
depends_on = None

_RUNTIME_ROLES = "custombuild_api, custombuild_worker"
_PUBLIC_FUNCTIONS = (
    "public.custombuild_storage_reserve_batch(text, jsonb, text, integer)",
    "public.custombuild_storage_renew_batch(text, jsonb, text, integer)",
    "public.custombuild_storage_commit_batch(text, jsonb, text)",
    "public.custombuild_storage_assert_reap_bucket(text)",
    "public.custombuild_storage_claim_expired_reservations(text, text, integer, integer)",
    "public.custombuild_storage_claim_delete_pending(text, text, integer, integer)",
    "public.custombuild_storage_finalize_reap(text, text, text, bigint, text, text)",
)
_API_FUNCTIONS = ("public.custombuild_storage_prepare_generation_retry(text, text)",)
_HELPER_FUNCTIONS = (
    "public._custombuild_storage_assert_uuid(text, text)",
    "public._custombuild_storage_assert_text(text, text, integer)",
    "public._custombuild_storage_require_tenant(text)",
    "public._custombuild_storage_assert_claims(jsonb)",
    "public._custombuild_storage_identity_matches(public.stored_objects, jsonb)",
    "public._custombuild_storage_claim_reap(text, text, integer, integer, text)",
    "public._custombuild_storage_enforce_domain_reference()",
    "public._custombuild_storage_enforce_generation_liveness()",
    "public._custombuild_storage_reject_tombstone_mutation()",
)
_DOMAIN_REFERENCE_TRIGGERS = (
    ("imported_assets", "custombuild_imported_asset_storage_identity"),
    ("external_evidence", "custombuild_external_evidence_storage_identity"),
    ("artifacts", "custombuild_artifact_storage_identity"),
)
_GENERATION_LIVENESS_TRIGGER = "custombuild_generation_storage_liveness"
_TOMBSTONE_APPEND_ONLY_TRIGGER = "custombuild_storage_tombstones_append_only"
_ATTESTOR_FUNCTIONS = (
    "public.custombuild_storage_lock_capacity()",
    (
        "public.custombuild_storage_attest_capacity(bigint, bigint, bigint, bigint, "
        "bigint, text, text, text, text, text, bigint, bigint, bigint, bigint, "
        "timestamptz, text)"
    ),
    "public.custombuild_storage_invalidate_capacity(text)",
)

_CREATE_ASSERT_UUID = r"""
CREATE OR REPLACE FUNCTION public._custombuild_storage_assert_uuid(
    p_name text,
    p_value text
) RETURNS void
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
BEGIN
    IF p_value IS NULL
       OR p_value !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR (p_value::uuid)::text IS DISTINCT FROM p_value THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'STORAGE_CLAIM_INVALID: ' || COALESCE(p_name, 'uuid')
                || ' must be a canonical lowercase UUID';
    END IF;
END;
$function$
"""

_CREATE_ASSERT_TEXT = r"""
CREATE OR REPLACE FUNCTION public._custombuild_storage_assert_text(
    p_name text,
    p_value text,
    p_maximum integer
) RETURNS void
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
BEGIN
    IF p_maximum IS NULL OR p_maximum < 1
       OR p_value IS NULL OR pg_catalog.length(p_value) < 1
       OR pg_catalog.length(p_value) > p_maximum
       OR p_value ~ '[[:cntrl:][:space:]]'
       OR pg_catalog.strpos(p_value, pg_catalog.chr(92)) > 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'STORAGE_CLAIM_INVALID: ' || COALESCE(p_name, 'text')
                || ' is not canonical';
    END IF;
END;
$function$
"""

_CREATE_REQUIRE_TENANT = r"""
CREATE OR REPLACE FUNCTION public._custombuild_storage_require_tenant(
    p_organization_id text
) RETURNS void
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
BEGIN
    PERFORM public._custombuild_storage_assert_uuid(
        'organization_id', p_organization_id
    );
    IF pg_catalog.current_setting('app.current_organization_id', true)
       IS DISTINCT FROM p_organization_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'STORAGE_TENANT_CONTEXT_MISMATCH';
    END IF;
END;
$function$
"""

_CREATE_ASSERT_CLAIMS = r"""
CREATE OR REPLACE FUNCTION public._custombuild_storage_assert_claims(
    p_claims jsonb
) RETURNS void
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_claim jsonb;
    v_key_count integer;
    v_keys_allowed boolean;
    v_object_key text;
    v_idempotency_key text;
    v_object_keys text[] := ARRAY[]::text[];
    v_idempotency_keys text[] := ARRAY[]::text[];
    v_size numeric;
BEGIN
    IF pg_catalog.jsonb_typeof(p_claims) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_claims) < 1
       OR pg_catalog.jsonb_array_length(p_claims) > 256 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'STORAGE_CLAIM_INVALID: claims must be a non-empty array '
                || 'of at most 256 objects';
    END IF;

    FOR v_claim IN
        SELECT item.value
        FROM pg_catalog.jsonb_array_elements(p_claims) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_claim) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'STORAGE_CLAIM_INVALID: every claim must be an object';
        END IF;
        SELECT pg_catalog.count(*),
               pg_catalog.bool_and(
                   key = ANY (ARRAY[
                       'project_id', 'object_key', 'sha256', 'size_bytes',
                       'media_type', 'owner_type', 'owner_id', 'idempotency_key'
                   ]::text[])
               )
        INTO v_key_count, v_keys_allowed
        FROM pg_catalog.jsonb_object_keys(v_claim) AS claim_key(key);
        IF v_key_count <> 8 OR NOT COALESCE(v_keys_allowed, false) THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'STORAGE_CLAIM_INVALID: claim keys do not match the canonical schema';
        END IF;
        IF pg_catalog.jsonb_typeof(v_claim -> 'project_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_claim -> 'object_key') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_claim -> 'sha256') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_claim -> 'size_bytes') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(v_claim -> 'media_type') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_claim -> 'owner_type') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_claim -> 'owner_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_claim -> 'idempotency_key') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'STORAGE_CLAIM_INVALID: claim value types do not match '
                    || 'the canonical schema';
        END IF;

        PERFORM public._custombuild_storage_assert_uuid(
            'project_id', v_claim ->> 'project_id'
        );
        PERFORM public._custombuild_storage_assert_uuid(
            'owner_id', v_claim ->> 'owner_id'
        );
        PERFORM public._custombuild_storage_assert_text(
            'object_key', v_claim ->> 'object_key', 512
        );
        PERFORM public._custombuild_storage_assert_text(
            'media_type', v_claim ->> 'media_type', 160
        );
        PERFORM public._custombuild_storage_assert_text(
            'owner_type', v_claim ->> 'owner_type', 40
        );
        PERFORM public._custombuild_storage_assert_text(
            'idempotency_key', v_claim ->> 'idempotency_key', 512
        );
        IF (v_claim ->> 'sha256') !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'STORAGE_CLAIM_INVALID: sha256 must be canonical lowercase hexadecimal';
        END IF;
        IF (v_claim ->> 'size_bytes') !~ '^[1-9][0-9]{0,18}$' THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'STORAGE_CLAIM_INVALID: size_bytes must be a positive canonical integer';
        END IF;
        v_size := (v_claim ->> 'size_bytes')::numeric;
        IF v_size > 10737418240 THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'STORAGE_CLAIM_INVALID: size_bytes exceeds the canonical '
                    || 'tenant byte limit';
        END IF;

        v_object_key := v_claim ->> 'object_key';
        v_idempotency_key := v_claim ->> 'idempotency_key';
        IF v_object_key = ANY (v_object_keys)
           OR v_idempotency_key = ANY (v_idempotency_keys) THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'STORAGE_CLAIM_INVALID: duplicate batch key';
        END IF;
        v_object_keys := pg_catalog.array_append(v_object_keys, v_object_key);
        v_idempotency_keys := pg_catalog.array_append(
            v_idempotency_keys, v_idempotency_key
        );
    END LOOP;
END;
$function$
"""

_CREATE_IDENTITY_MATCH = r"""
CREATE OR REPLACE FUNCTION public._custombuild_storage_identity_matches(
    p_row public.stored_objects,
    p_claim jsonb
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
    SELECT (p_row).project_id = p_claim ->> 'project_id'
       AND (p_row).object_key = p_claim ->> 'object_key'
       AND (p_row).sha256 = p_claim ->> 'sha256'
       AND (p_row).size_bytes = (p_claim ->> 'size_bytes')::bigint
       AND (p_row).media_type = p_claim ->> 'media_type'
       AND (p_row).owner_type = p_claim ->> 'owner_type'
       AND (p_row).owner_id = p_claim ->> 'owner_id'
       AND (p_row).idempotency_key = p_claim ->> 'idempotency_key'
$function$
"""

_CREATE_RESERVE = r"""
CREATE OR REPLACE FUNCTION public.custombuild_storage_reserve_batch(
    p_organization_id text,
    p_claims jsonb,
    p_lease_token text,
    p_lease_duration_seconds integer
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_global public.storage_global_quotas%ROWTYPE;
    v_tenant public.storage_tenant_quotas%ROWTYPE;
    v_row public.stored_objects%ROWTYPE;
    v_claim jsonb;
    v_now timestamptz;
    v_expiry timestamptz;
    v_match_count integer;
    v_candidate integer;
    v_byte_delta bigint := 0;
    v_count_delta bigint := 0;
    v_objects jsonb := '[]'::jsonb;
BEGIN
    PERFORM public._custombuild_storage_require_tenant(p_organization_id);
    PERFORM public._custombuild_storage_assert_uuid('lease_token', p_lease_token);
    PERFORM public._custombuild_storage_assert_claims(p_claims);
    IF p_lease_duration_seconds IS NULL
       OR p_lease_duration_seconds < 1
       OR p_lease_duration_seconds > 10800 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'STORAGE_CLAIM_INVALID: lease duration is outside 1..10800 seconds';
    END IF;

    SELECT * INTO v_global
    FROM public.storage_global_quotas
    WHERE id = 1
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0002', MESSAGE = 'STORAGE_QUOTA_INVARIANT: global quota is missing';
    END IF;
    v_now := pg_catalog.clock_timestamp();
    IF v_global.maintenance_token IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'STORAGE_MAINTENANCE_ACTIVE: new reservations are fenced';
    END IF;
    IF v_global.recovery_database_started_at IS NULL
       OR v_global.recovery_completed_at IS NULL
       OR v_global.recovery_database_started_at
          IS DISTINCT FROM pg_catalog.pg_postmaster_start_time() THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'STORAGE_RECOVERY_REQUIRED: this database boot is not recovered';
    END IF;
    IF v_global.capacity_verified IS DISTINCT FROM true
       OR v_global.provisioned_bytes IS NULL
       OR v_global.metadata_overhead_bytes IS NULL
       OR v_global.emergency_reserve_bytes IS NULL
       OR v_global.capacity_headroom_bytes IS NULL
       OR v_global.provisioned_bytes <= v_global.capacity_headroom_bytes
       OR v_global.capacity_headroom_bytes
          <> v_global.metadata_overhead_bytes + v_global.emergency_reserve_bytes
       OR v_global.byte_limit
          > v_global.provisioned_bytes - v_global.capacity_headroom_bytes
       OR v_global.capacity_verified_at IS NULL
       OR v_global.capacity_attested_at IS NULL
       OR v_global.capacity_verified_at < v_now - INTERVAL '10 minutes'
       OR v_global.capacity_verified_at > v_now + INTERVAL '1 minute'
       OR v_global.capacity_attested_at < v_now - INTERVAL '10 minutes'
       OR v_global.capacity_attested_at > v_global.capacity_verified_at
       OR v_global.volume_identity IS NULL OR v_global.volume_identity = ''
       OR v_global.capacity_bucket IS NULL OR v_global.capacity_bucket = ''
       OR v_global.capacity_operator_config_sha256 IS NULL
       OR v_global.capacity_operator_config_sha256 !~ '^[0-9a-f]{64}$'
       OR v_global.deploy_descriptor_sha256 IS NULL
       OR v_global.deploy_descriptor_sha256 !~ '^[0-9a-f]{64}$'
       OR v_global.inventory_sha256 IS NULL
       OR v_global.inventory_sha256 !~ '^[0-9a-f]{64}$'
       OR v_global.capacity_evidence_sha256 IS NULL
       OR v_global.capacity_evidence_sha256 !~ '^[0-9a-f]{64}$'
       OR v_global.inventory_object_count IS NULL
       OR v_global.inventory_bytes IS NULL
       OR v_global.ledger_object_count IS NULL
       OR v_global.ledger_bytes IS NULL
       OR v_global.inventory_object_count <> v_global.ledger_object_count
       OR v_global.inventory_bytes <> v_global.ledger_bytes THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'STORAGE_CAPACITY_UNVERIFIED: fresh exact capacity evidence is required';
    END IF;

    INSERT INTO public.storage_tenant_quotas (
        organization_id, byte_limit, object_limit,
        reserved_bytes, committed_bytes, reserved_count, committed_count,
        created_at, updated_at
    ) VALUES (
        p_organization_id, 10737418240, 100000,
        0, 0, 0, 0, v_now, v_now
    ) ON CONFLICT (organization_id) DO NOTHING;
    SELECT * INTO v_tenant
    FROM public.storage_tenant_quotas
    WHERE organization_id = p_organization_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0002', MESSAGE = 'STORAGE_QUOTA_INVARIANT: tenant quota is missing';
    END IF;
    v_now := pg_catalog.clock_timestamp();
    v_expiry := v_now + pg_catalog.make_interval(secs => p_lease_duration_seconds);

    FOR v_claim IN
        SELECT item.value
        FROM pg_catalog.jsonb_array_elements(p_claims) AS item(value)
        ORDER BY item.value ->> 'object_key'
    LOOP
        IF EXISTS (
            SELECT 1
            FROM public.storage_object_tombstones AS tombstone
            WHERE tombstone.capacity_bucket = v_global.capacity_bucket
              AND (
                  tombstone.object_key = v_claim ->> 'object_key'
                  OR tombstone.idempotency_key = v_claim ->> 'idempotency_key'
              )
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23505',
                MESSAGE = 'STORAGE_CLAIM_CONFLICT: physical storage key or '
                    || 'idempotency identity is permanently retired';
        END IF;
        SELECT pg_catalog.count(*) INTO v_match_count
        FROM public.stored_objects
        WHERE organization_id = p_organization_id
          AND (object_key = v_claim ->> 'object_key'
               OR idempotency_key = v_claim ->> 'idempotency_key');
        IF v_match_count > 1 THEN
            RAISE EXCEPTION USING
                ERRCODE = '23505',
                MESSAGE = 'STORAGE_CLAIM_CONFLICT: object and idempotency keys '
                    || 'resolve to different identities';
        ELSIF v_match_count = 0 THEN
            INSERT INTO public.stored_objects (
                organization_id, object_key, project_id, sha256, size_bytes,
                media_type, owner_type, owner_id, idempotency_key, state,
                lease_token, lease_expires_at, claim_token, claim_expires_at,
                created_at, updated_at
            ) VALUES (
                p_organization_id, v_claim ->> 'object_key',
                v_claim ->> 'project_id', v_claim ->> 'sha256',
                (v_claim ->> 'size_bytes')::bigint, v_claim ->> 'media_type',
                v_claim ->> 'owner_type', v_claim ->> 'owner_id',
                v_claim ->> 'idempotency_key', 'reserved', p_lease_token,
                v_expiry, NULL, NULL, v_now, v_now
            );
            v_byte_delta := v_byte_delta + (v_claim ->> 'size_bytes')::bigint;
            v_count_delta := v_count_delta + 1;
            v_objects := v_objects || pg_catalog.jsonb_build_array(
                pg_catalog.jsonb_build_object(
                    'object_key', v_claim ->> 'object_key',
                    'state', 'reserved', 'lease_token', p_lease_token,
                    'newly_reserved', true
                )
            );
        ELSE
            SELECT * INTO STRICT v_row
            FROM public.stored_objects
            WHERE organization_id = p_organization_id
              AND (object_key = v_claim ->> 'object_key'
                   OR idempotency_key = v_claim ->> 'idempotency_key')
            FOR UPDATE;
            IF NOT public._custombuild_storage_identity_matches(v_row, v_claim) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE = 'STORAGE_CLAIM_CONFLICT: immutable storage identity differs';
            END IF;
            IF v_row.state = 'committed' THEN
                v_objects := v_objects || pg_catalog.jsonb_build_array(
                    pg_catalog.jsonb_build_object(
                        'object_key', v_row.object_key, 'state', 'committed',
                        'lease_token', NULL, 'newly_reserved', false
                    )
                );
            ELSIF v_row.state = 'reaping' THEN
                IF v_row.claim_token IS NULL
                   OR v_row.claim_token
                      !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   OR (v_row.claim_token::uuid)::text IS DISTINCT FROM v_row.claim_token
                   OR pg_catalog.substr(v_row.claim_token, 15, 1) NOT IN ('4', '5')
                   OR v_row.claim_expires_at IS NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'STORAGE_CLAIM_CONFLICT: reaping object has an '
                            || 'invalid claim';
                ELSIF v_row.claim_expires_at > v_now THEN
                    v_candidate := pg_catalog.ceil(
                        EXTRACT(EPOCH FROM (v_row.claim_expires_at - v_now))
                    )::integer + 5;
                ELSE
                    v_candidate := 5;
                END IF;
                IF v_candidate < 1 OR v_candidate > 3605 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'STORAGE_CLAIM_CONFLICT: reaper retry delay is '
                            || 'outside the canonical window';
                END IF;
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'STORAGE_GENERATION_RETRY_BUSY:' || v_candidate::text;
            ELSIF v_row.state <> 'reserved' OR v_row.lease_expires_at IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE = 'STORAGE_CLAIM_CONFLICT: object is being deleted or reaped';
            ELSIF v_row.lease_expires_at <= v_now THEN
                UPDATE public.stored_objects
                SET lease_token = p_lease_token,
                    lease_expires_at = v_expiry,
                    updated_at = v_now
                WHERE organization_id = p_organization_id
                  AND object_key = v_row.object_key;
                v_objects := v_objects || pg_catalog.jsonb_build_array(
                    pg_catalog.jsonb_build_object(
                        'object_key', v_row.object_key, 'state', 'reserved',
                        'lease_token', p_lease_token, 'newly_reserved', false
                    )
                );
            ELSIF v_row.lease_token IS DISTINCT FROM p_lease_token THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE = 'STORAGE_RESERVATION_BUSY: object has a different active lease';
            ELSE
                UPDATE public.stored_objects
                SET lease_expires_at = GREATEST(lease_expires_at, v_expiry),
                    updated_at = v_now
                WHERE organization_id = p_organization_id
                  AND object_key = v_row.object_key;
                v_objects := v_objects || pg_catalog.jsonb_build_array(
                    pg_catalog.jsonb_build_object(
                        'object_key', v_row.object_key, 'state', 'reserved',
                        'lease_token', p_lease_token, 'newly_reserved', false
                    )
                );
            END IF;
        END IF;
    END LOOP;

    IF (v_global.reserved_bytes::numeric + v_global.committed_bytes::numeric
        + v_byte_delta::numeric) > v_global.byte_limit::numeric
       OR (v_global.reserved_count::numeric + v_global.committed_count::numeric
           + v_count_delta::numeric) > v_global.object_limit::numeric
       OR (v_tenant.reserved_bytes::numeric + v_tenant.committed_bytes::numeric
           + v_byte_delta::numeric) > v_tenant.byte_limit::numeric
       OR (v_tenant.reserved_count::numeric + v_tenant.committed_count::numeric
           + v_count_delta::numeric) > v_tenant.object_limit::numeric THEN
        RAISE EXCEPTION USING
            ERRCODE = '53100', MESSAGE = 'STORAGE_QUOTA_EXCEEDED: whole batch does not fit';
    END IF;

    IF v_count_delta > 0 THEN
        UPDATE public.storage_global_quotas
        SET reserved_bytes = reserved_bytes + v_byte_delta,
            reserved_count = reserved_count + v_count_delta,
            updated_at = v_now
        WHERE id = 1;
        UPDATE public.storage_tenant_quotas
        SET reserved_bytes = reserved_bytes + v_byte_delta,
            reserved_count = reserved_count + v_count_delta,
            updated_at = v_now
        WHERE organization_id = p_organization_id;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'objects', v_objects,
        'newly_reserved_bytes', v_byte_delta,
        'newly_reserved_count', v_count_delta
    );
END;
$function$
"""

_CREATE_RENEW = r"""
CREATE OR REPLACE FUNCTION public.custombuild_storage_renew_batch(
    p_organization_id text,
    p_claims jsonb,
    p_lease_token text,
    p_lease_duration_seconds integer
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_claim jsonb;
    v_row public.stored_objects%ROWTYPE;
    v_match_count integer;
    v_now timestamptz;
    v_expiry timestamptz;
BEGIN
    PERFORM public._custombuild_storage_require_tenant(p_organization_id);
    PERFORM public._custombuild_storage_assert_uuid('lease_token', p_lease_token);
    PERFORM public._custombuild_storage_assert_claims(p_claims);
    IF p_lease_duration_seconds IS NULL
       OR p_lease_duration_seconds < 1
       OR p_lease_duration_seconds > 10800 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'STORAGE_CLAIM_INVALID: lease duration is outside 1..10800 seconds';
    END IF;

    PERFORM 1
    FROM public.stored_objects
    WHERE organization_id = p_organization_id
      AND (object_key IN (
               SELECT item.value ->> 'object_key'
               FROM pg_catalog.jsonb_array_elements(p_claims) AS item(value)
           )
           OR idempotency_key IN (
               SELECT item.value ->> 'idempotency_key'
               FROM pg_catalog.jsonb_array_elements(p_claims) AS item(value)
           ))
    ORDER BY object_key
    FOR UPDATE;
    v_now := pg_catalog.clock_timestamp();
    v_expiry := v_now + pg_catalog.make_interval(secs => p_lease_duration_seconds);

    FOR v_claim IN
        SELECT item.value
        FROM pg_catalog.jsonb_array_elements(p_claims) AS item(value)
        ORDER BY item.value ->> 'object_key'
    LOOP
        SELECT pg_catalog.count(*) INTO v_match_count
        FROM public.stored_objects
        WHERE organization_id = p_organization_id
          AND (object_key = v_claim ->> 'object_key'
               OR idempotency_key = v_claim ->> 'idempotency_key');
        IF v_match_count <> 1 THEN
            RAISE EXCEPTION USING
                ERRCODE = '23505',
                MESSAGE = 'STORAGE_CLAIM_CONFLICT: lease identity is missing '
                    || 'or ambiguous';
        END IF;
        SELECT * INTO STRICT v_row
        FROM public.stored_objects
        WHERE organization_id = p_organization_id
          AND (object_key = v_claim ->> 'object_key'
               OR idempotency_key = v_claim ->> 'idempotency_key');
        IF NOT public._custombuild_storage_identity_matches(v_row, v_claim)
           OR v_row.state <> 'reserved'
           OR v_row.lease_token IS DISTINCT FROM p_lease_token
           OR v_row.lease_expires_at IS NULL
           OR v_row.lease_expires_at <= v_now THEN
            RAISE EXCEPTION USING
                ERRCODE = '23505',
                MESSAGE = 'STORAGE_CLAIM_CONFLICT: reservation lease ownership '
                    || 'was lost';
        END IF;
        UPDATE public.stored_objects
        SET lease_expires_at = GREATEST(lease_expires_at, v_expiry),
            updated_at = v_now
        WHERE organization_id = p_organization_id
          AND object_key = v_row.object_key;
    END LOOP;
END;
$function$
"""

_CREATE_COMMIT = r"""
CREATE OR REPLACE FUNCTION public.custombuild_storage_commit_batch(
    p_organization_id text,
    p_claims jsonb,
    p_lease_token text
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_global public.storage_global_quotas%ROWTYPE;
    v_tenant public.storage_tenant_quotas%ROWTYPE;
    v_row public.stored_objects%ROWTYPE;
    v_claim jsonb;
    v_match_count integer;
    v_now timestamptz;
    v_byte_delta bigint := 0;
    v_count_delta bigint := 0;
BEGIN
    PERFORM public._custombuild_storage_require_tenant(p_organization_id);
    PERFORM public._custombuild_storage_assert_uuid('lease_token', p_lease_token);
    PERFORM public._custombuild_storage_assert_claims(p_claims);

    SELECT * INTO v_global FROM public.storage_global_quotas WHERE id = 1 FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0002', MESSAGE = 'STORAGE_QUOTA_INVARIANT: global quota is missing';
    END IF;
    SELECT * INTO v_tenant
    FROM public.storage_tenant_quotas
    WHERE organization_id = p_organization_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0002', MESSAGE = 'STORAGE_QUOTA_INVARIANT: tenant quota is missing';
    END IF;
    PERFORM 1
    FROM public.stored_objects
    WHERE organization_id = p_organization_id
      AND (object_key IN (
               SELECT item.value ->> 'object_key'
               FROM pg_catalog.jsonb_array_elements(p_claims) AS item(value)
           )
           OR idempotency_key IN (
               SELECT item.value ->> 'idempotency_key'
               FROM pg_catalog.jsonb_array_elements(p_claims) AS item(value)
           ))
    ORDER BY object_key
    FOR UPDATE;
    v_now := pg_catalog.clock_timestamp();

    FOR v_claim IN
        SELECT item.value
        FROM pg_catalog.jsonb_array_elements(p_claims) AS item(value)
        ORDER BY item.value ->> 'object_key'
    LOOP
        SELECT pg_catalog.count(*) INTO v_match_count
        FROM public.stored_objects
        WHERE organization_id = p_organization_id
          AND (object_key = v_claim ->> 'object_key'
               OR idempotency_key = v_claim ->> 'idempotency_key');
        IF v_match_count <> 1 THEN
            RAISE EXCEPTION USING
                ERRCODE = '23505',
                MESSAGE = 'STORAGE_CLAIM_CONFLICT: commit identity is missing '
                    || 'or ambiguous';
        END IF;
        SELECT * INTO STRICT v_row
        FROM public.stored_objects
        WHERE organization_id = p_organization_id
          AND (object_key = v_claim ->> 'object_key'
               OR idempotency_key = v_claim ->> 'idempotency_key');
        IF NOT public._custombuild_storage_identity_matches(v_row, v_claim) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23505',
                MESSAGE = 'STORAGE_CLAIM_CONFLICT: immutable storage identity differs';
        ELSIF v_row.state = 'committed' THEN
            CONTINUE;
        ELSIF v_row.state <> 'reserved'
              OR v_row.lease_token IS DISTINCT FROM p_lease_token
              OR v_row.lease_expires_at IS NULL
              OR v_row.lease_expires_at <= v_now THEN
            RAISE EXCEPTION USING
                ERRCODE = '23505',
                MESSAGE = 'STORAGE_CLAIM_CONFLICT: reserved object is owned by '
                    || 'another lease';
        END IF;
        v_byte_delta := v_byte_delta + v_row.size_bytes;
        v_count_delta := v_count_delta + 1;
    END LOOP;

    IF v_global.reserved_bytes < v_byte_delta
       OR v_global.reserved_count < v_count_delta
       OR v_tenant.reserved_bytes < v_byte_delta
       OR v_tenant.reserved_count < v_count_delta THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0002',
            MESSAGE = 'STORAGE_QUOTA_INVARIANT: reserved counters would underflow';
    END IF;
    IF v_count_delta > 0 THEN
        UPDATE public.storage_global_quotas
        SET reserved_bytes = reserved_bytes - v_byte_delta,
            committed_bytes = committed_bytes + v_byte_delta,
            reserved_count = reserved_count - v_count_delta,
            committed_count = committed_count + v_count_delta,
            updated_at = v_now
        WHERE id = 1;
        UPDATE public.storage_tenant_quotas
        SET reserved_bytes = reserved_bytes - v_byte_delta,
            committed_bytes = committed_bytes + v_byte_delta,
            reserved_count = reserved_count - v_count_delta,
            committed_count = committed_count + v_count_delta,
            updated_at = v_now
        WHERE organization_id = p_organization_id;
        FOR v_claim IN
            SELECT item.value
            FROM pg_catalog.jsonb_array_elements(p_claims) AS item(value)
            ORDER BY item.value ->> 'object_key'
        LOOP
            UPDATE public.stored_objects
            SET state = 'committed', lease_token = NULL,
                lease_expires_at = NULL, updated_at = v_now
            WHERE organization_id = p_organization_id
              AND object_key = v_claim ->> 'object_key'
              AND state = 'reserved' AND lease_token = p_lease_token;
        END LOOP;
    END IF;
END;
$function$
"""

_CREATE_CLAIM_REAP_HELPER = r"""
CREATE OR REPLACE FUNCTION public._custombuild_storage_claim_reap(
    p_organization_id text,
    p_claim_token text,
    p_claim_duration_seconds integer,
    p_limit integer,
    p_accounting_state text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_row public.stored_objects%ROWTYPE;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_expiry timestamptz;
    v_marker text;
    v_effective_token text;
    v_result jsonb := '[]'::jsonb;
BEGIN
    PERFORM public._custombuild_storage_require_tenant(p_organization_id);
    PERFORM public._custombuild_storage_assert_uuid('claim_token', p_claim_token);
    IF p_claim_duration_seconds IS NULL OR p_claim_duration_seconds < 1
       OR p_claim_duration_seconds > 3600
       OR p_limit IS NULL OR p_limit < 1 OR p_limit > 256 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023', MESSAGE = 'STORAGE_CLAIM_INVALID: reaper bounds are invalid';
    END IF;
    IF p_accounting_state = 'reserved' THEN
        v_marker := '4';
    ELSIF p_accounting_state = 'committed' THEN
        v_marker := '5';
    ELSE
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'STORAGE_CLAIM_INVALID: reaper accounting state is invalid';
    END IF;
    v_effective_token := pg_catalog.overlay(p_claim_token, v_marker, 15, 1);
    v_expiry := v_now + pg_catalog.make_interval(secs => p_claim_duration_seconds);

    FOR v_row IN
        SELECT candidate.*
        FROM public.stored_objects AS candidate
        WHERE candidate.organization_id = p_organization_id
          AND (
              (p_accounting_state = 'reserved'
               AND candidate.state = 'reserved'
               AND candidate.lease_expires_at <= v_now)
              OR
              (p_accounting_state = 'committed'
               AND candidate.state IN ('committed', 'delete_pending'))
              OR
              (candidate.state = 'reaping'
               AND candidate.claim_expires_at <= v_now
               AND pg_catalog.substr(candidate.claim_token, 15, 1) = v_marker)
          )
          AND NOT EXISTS (
              SELECT 1 FROM public.imported_assets AS imported
              WHERE imported.organization_id = candidate.organization_id
                AND imported.object_key = candidate.object_key
          )
          AND NOT EXISTS (
              SELECT 1 FROM public.external_evidence AS evidence
              WHERE evidence.organization_id = candidate.organization_id
                AND evidence.object_key = candidate.object_key
          )
          AND NOT EXISTS (
              SELECT 1 FROM public.artifacts AS artifact
              WHERE artifact.organization_id = candidate.organization_id
                AND artifact.object_key = candidate.object_key
          )
          AND NOT EXISTS (
              SELECT 1 FROM public.generation_jobs AS generation_job
              WHERE candidate.owner_type = 'generation_job'
                AND generation_job.organization_id = candidate.organization_id
                AND generation_job.id = candidate.owner_id
                AND (
                    generation_job.status IN ('queued', 'running')
                    OR generation_job.lease_expires_at > v_now
                )
          )
        ORDER BY COALESCE(
                     candidate.lease_expires_at, candidate.claim_expires_at,
                     candidate.updated_at
                 ), candidate.object_key
        LIMIT p_limit
        FOR UPDATE OF candidate SKIP LOCKED
    LOOP
        UPDATE public.stored_objects
        SET state = 'reaping', lease_token = NULL, lease_expires_at = NULL,
            claim_token = v_effective_token, claim_expires_at = v_expiry,
            updated_at = v_now
        WHERE organization_id = p_organization_id
          AND object_key = v_row.object_key;
        v_result := v_result || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'organization_id', p_organization_id,
                'project_id', v_row.project_id,
                'object_key', v_row.object_key,
                'sha256', v_row.sha256,
                'size_bytes', v_row.size_bytes,
                'media_type', v_row.media_type,
                'owner_type', v_row.owner_type,
                'owner_id', v_row.owner_id,
                'claim_token', v_effective_token,
                'claim_expires_at', v_expiry,
                'accounting_state', p_accounting_state
            )
        );
    END LOOP;
    RETURN v_result;
END;
$function$
"""

_CREATE_CLAIM_EXPIRED = r"""
CREATE OR REPLACE FUNCTION public.custombuild_storage_claim_expired_reservations(
    p_organization_id text,
    p_claim_token text,
    p_claim_duration_seconds integer,
    p_limit integer
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
BEGIN
    RETURN public._custombuild_storage_claim_reap(
        p_organization_id, p_claim_token, p_claim_duration_seconds, p_limit, 'reserved'
    );
END;
$function$
"""

_CREATE_ASSERT_REAP_BUCKET = r"""
CREATE OR REPLACE FUNCTION public.custombuild_storage_assert_reap_bucket(
    p_capacity_bucket text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_capacity_bucket text;
BEGIN
    PERFORM public._custombuild_storage_assert_text(
        'capacity_bucket', p_capacity_bucket, 63
    );
    SELECT capacity_bucket INTO v_capacity_bucket
    FROM public.storage_global_quotas
    WHERE id = 1
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0002', MESSAGE = 'STORAGE_QUOTA_INVARIANT: global quota is missing';
    END IF;
    IF v_capacity_bucket IS DISTINCT FROM p_capacity_bucket THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'STORAGE_BUCKET_MISMATCH: provider bucket differs from ledger capacity';
    END IF;
    RETURN true;
END;
$function$
"""

_CREATE_CLAIM_DELETE_PENDING = r"""
CREATE OR REPLACE FUNCTION public.custombuild_storage_claim_delete_pending(
    p_organization_id text,
    p_claim_token text,
    p_claim_duration_seconds integer,
    p_limit integer
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
BEGIN
    RETURN public._custombuild_storage_claim_reap(
        p_organization_id, p_claim_token, p_claim_duration_seconds, p_limit, 'committed'
    );
END;
$function$
"""

_CREATE_ENFORCE_DOMAIN_REFERENCE = r"""
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
        SELECT design_version.project_id INTO v_project_id
        FROM public.generation_jobs AS generation_job
        JOIN public.design_versions AS design_version
          ON design_version.organization_id = generation_job.organization_id
         AND design_version.id = generation_job.design_version_id
        WHERE generation_job.organization_id = NEW.organization_id
          AND generation_job.id = NEW.generation_job_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '23503',
                MESSAGE = 'STORAGE_DOMAIN_REFERENCE_INVALID: artifact owner graph is missing';
        END IF;
    END IF;
    -- This constraint trigger is deferred to transaction end so the normal
    -- reserve -> domain INSERT -> commit flow can be atomic. A reference
    -- created after a reaper claim can never commit because reaping is not a
    -- valid terminal identity.
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

_CREATE_PREPARE_GENERATION_RETRY = r"""
CREATE OR REPLACE FUNCTION public.custombuild_storage_prepare_generation_retry(
    p_organization_id text,
    p_generation_job_id text
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_job_status text;
    v_row record;
    v_now timestamptz;
    v_candidate integer;
    v_retry_after integer := 0;
BEGIN
    PERFORM public._custombuild_storage_require_tenant(p_organization_id);
    PERFORM public._custombuild_storage_assert_uuid(
        'generation_job_id', p_generation_job_id
    );

    -- Every caller, including a direct function invocation, takes the same
    -- job -> object lock order used by the retry route and its BEFORE trigger.
    SELECT generation_job.status INTO v_job_status
    FROM public.generation_jobs AS generation_job
    WHERE generation_job.organization_id = p_organization_id
      AND generation_job.id = p_generation_job_id
    FOR UPDATE;
    IF NOT FOUND OR v_job_status NOT IN ('failed', 'succeeded') THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'STORAGE_QUOTA_INVARIANT: generation retry requires the '
                || 'exact terminal tenant job';
    END IF;

    PERFORM stored.object_key
    FROM public.stored_objects AS stored
    WHERE stored.organization_id = p_organization_id
      AND stored.owner_type = 'generation_job'
      AND stored.owner_id = p_generation_job_id
    ORDER BY stored.object_key
    FOR UPDATE;
    -- Claim expiry is evaluated against one database timestamp captured only
    -- after every owned row is locked.
    v_now := pg_catalog.clock_timestamp();

    FOR v_row IN
        SELECT stored.state, stored.claim_token, stored.claim_expires_at
        FROM public.stored_objects AS stored
        WHERE stored.organization_id = p_organization_id
          AND stored.owner_type = 'generation_job'
          AND stored.owner_id = p_generation_job_id
        ORDER BY stored.object_key
    LOOP
        IF v_row.state = 'reaping' THEN
            IF v_row.claim_token IS NULL
               OR v_row.claim_token
                  !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_row.claim_token::uuid)::text IS DISTINCT FROM v_row.claim_token
               OR pg_catalog.substr(v_row.claim_token, 15, 1) NOT IN ('4', '5')
               OR v_row.claim_expires_at IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'STORAGE_QUOTA_INVARIANT: generation retry storage has '
                        || 'an invalid reaper claim';
            END IF;
            IF v_row.claim_expires_at > v_now THEN
                v_candidate := pg_catalog.ceil(
                    EXTRACT(EPOCH FROM (v_row.claim_expires_at - v_now))
                )::integer + 5;
            ELSE
                -- An expired claim can only be transferred to another reaper.
                -- A writer must never reactivate the same physical key while
                -- any earlier unversioned DELETE could still arrive.
                v_candidate := 5;
            END IF;
            IF v_candidate < 1 OR v_candidate > 3605 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'STORAGE_QUOTA_INVARIANT: generation retry storage '
                        || 'claim delay is outside the canonical window';
            END IF;
            v_retry_after := GREATEST(v_retry_after, v_candidate);
        ELSIF v_row.state NOT IN ('reserved', 'committed', 'delete_pending') THEN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'STORAGE_QUOTA_INVARIANT: generation retry storage has '
                    || 'an unknown lifecycle state';
        END IF;
    END LOOP;
    RETURN v_retry_after;
END;
$function$
"""

_CREATE_ENFORCE_GENERATION_LIVENESS = r"""
CREATE OR REPLACE FUNCTION public._custombuild_storage_enforce_generation_liveness(
) RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_row record;
    v_now timestamptz;
    v_candidate integer;
    v_retry_after integer := 0;
BEGIN
    IF TG_TABLE_SCHEMA IS DISTINCT FROM 'public'
       OR TG_TABLE_NAME IS DISTINCT FROM 'generation_jobs' THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'STORAGE_GENERATION_LIVENESS_INVALID: unexpected trigger target';
    END IF;
    IF NEW.status IN ('queued', 'running')
       OR NEW.lease_expires_at IS NOT NULL THEN
        -- This function runs from an immediate BEFORE trigger. If the job
        -- transition wins, KEY SHARE prevents a reaper claim from taking the
        -- object FOR UPDATE; if the claim wins, this lock waits and then sees
        -- the committed reaping state and rejects the transition.
        PERFORM stored.object_key
        FROM public.stored_objects AS stored
        WHERE stored.organization_id = NEW.organization_id
          AND stored.owner_type = 'generation_job'
          AND stored.owner_id = NEW.id
        ORDER BY stored.object_key
        FOR KEY SHARE;
        v_now := pg_catalog.clock_timestamp();
        IF NEW.status NOT IN ('queued', 'running')
           AND NEW.lease_expires_at <= v_now THEN
            RETURN NEW;
        END IF;
        FOR v_row IN
            SELECT stored.state, stored.claim_token, stored.claim_expires_at
            FROM public.stored_objects AS stored
            WHERE stored.organization_id = NEW.organization_id
              AND stored.owner_type = 'generation_job'
              AND stored.owner_id = NEW.id
            ORDER BY stored.object_key
        LOOP
            IF v_row.state = 'reaping' THEN
                IF v_row.claim_token IS NULL
                   OR v_row.claim_token
                      !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   OR (v_row.claim_token::uuid)::text IS DISTINCT FROM v_row.claim_token
                   OR pg_catalog.substr(v_row.claim_token, 15, 1) NOT IN ('4', '5')
                   OR v_row.claim_expires_at IS NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23503',
                        MESSAGE = 'STORAGE_GENERATION_LIVENESS_INVALID: generation '
                            || 'storage has an invalid reaper claim';
                END IF;
                IF v_row.claim_expires_at > v_now THEN
                    v_candidate := pg_catalog.ceil(
                        EXTRACT(EPOCH FROM (v_row.claim_expires_at - v_now))
                    )::integer + 5;
                ELSE
                    v_candidate := 5;
                END IF;
                IF v_candidate < 1 OR v_candidate > 3605 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23503',
                        MESSAGE = 'STORAGE_GENERATION_LIVENESS_INVALID: generation '
                            || 'storage claim delay is outside the canonical window';
                END IF;
                v_retry_after := GREATEST(v_retry_after, v_candidate);
            ELSIF v_row.state NOT IN ('reserved', 'committed', 'delete_pending') THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23503',
                    MESSAGE = 'STORAGE_GENERATION_LIVENESS_INVALID: generation '
                        || 'storage has an unknown lifecycle state';
            END IF;
        END LOOP;
        IF v_retry_after > 0 THEN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'STORAGE_GENERATION_RETRY_BUSY:' || v_retry_after::text;
        END IF;
    END IF;
    RETURN NEW;
END;
$function$
"""

_CREATE_FINALIZE_REAP = r"""
CREATE OR REPLACE FUNCTION public.custombuild_storage_finalize_reap(
    p_organization_id text,
    p_object_key text,
    p_sha256 text,
    p_size_bytes bigint,
    p_claim_token text,
    p_capacity_bucket text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_global public.storage_global_quotas%ROWTYPE;
    v_tenant public.storage_tenant_quotas%ROWTYPE;
    v_row public.stored_objects%ROWTYPE;
    v_now timestamptz;
    v_marker text;
BEGIN
    PERFORM public._custombuild_storage_require_tenant(p_organization_id);
    PERFORM public._custombuild_storage_assert_text('object_key', p_object_key, 512);
    PERFORM public._custombuild_storage_assert_uuid('claim_token', p_claim_token);
    PERFORM public._custombuild_storage_assert_text(
        'capacity_bucket', p_capacity_bucket, 63
    );
    IF p_sha256 IS NULL OR p_sha256 !~ '^[0-9a-f]{64}$'
       OR p_size_bytes IS NULL OR p_size_bytes < 1 OR p_size_bytes > 10737418240 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023', MESSAGE = 'STORAGE_CLAIM_INVALID: reaper identity is invalid';
    END IF;
    v_marker := pg_catalog.substr(p_claim_token, 15, 1);
    IF v_marker NOT IN ('4', '5') THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'STORAGE_CLAIM_INVALID: claim token has no accounting marker';
    END IF;

    SELECT * INTO v_global FROM public.storage_global_quotas WHERE id = 1 FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0002', MESSAGE = 'STORAGE_QUOTA_INVARIANT: global quota is missing';
    END IF;
    IF v_global.capacity_bucket IS DISTINCT FROM p_capacity_bucket THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'STORAGE_BUCKET_MISMATCH: provider bucket differs from ledger capacity';
    END IF;
    SELECT * INTO v_tenant
    FROM public.storage_tenant_quotas
    WHERE organization_id = p_organization_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0002', MESSAGE = 'STORAGE_QUOTA_INVARIANT: tenant quota is missing';
    END IF;
    SELECT * INTO v_row
    FROM public.stored_objects
    WHERE organization_id = p_organization_id AND object_key = p_object_key
    FOR UPDATE;
    v_now := pg_catalog.clock_timestamp();
    IF NOT FOUND OR v_row.state <> 'reaping'
       OR v_row.sha256 IS DISTINCT FROM p_sha256
       OR v_row.size_bytes IS DISTINCT FROM p_size_bytes
       OR v_row.claim_token IS DISTINCT FROM p_claim_token
       OR v_row.claim_expires_at IS NULL OR v_row.claim_expires_at <= v_now THEN
        RAISE EXCEPTION USING
            ERRCODE = '23505',
            MESSAGE = 'STORAGE_CLAIM_CONFLICT: reaper ownership or identity was lost';
    END IF;
    IF EXISTS (
           SELECT 1 FROM public.imported_assets
           WHERE organization_id = p_organization_id AND object_key = p_object_key
       ) OR EXISTS (
           SELECT 1 FROM public.external_evidence
           WHERE organization_id = p_organization_id AND object_key = p_object_key
       ) OR EXISTS (
           SELECT 1 FROM public.artifacts
           WHERE organization_id = p_organization_id AND object_key = p_object_key
       ) OR EXISTS (
           SELECT 1
           FROM public.stored_objects AS protected_object
           JOIN public.generation_jobs AS generation_job
             ON generation_job.organization_id = protected_object.organization_id
            AND generation_job.id = protected_object.owner_id
           WHERE protected_object.organization_id = p_organization_id
             AND protected_object.object_key = p_object_key
             AND protected_object.owner_type = 'generation_job'
             AND (
                 generation_job.status IN ('queued', 'running')
                 OR generation_job.lease_expires_at > v_now
             )
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23503',
            MESSAGE = 'STORAGE_REAP_BLOCKED: object still has a domain reference';
    END IF;

    -- Burn the physical bucket/key and its logical idempotency identity in the
    -- same database transaction that releases quota and removes the live row.
    -- A delayed or SDK-retried unversioned DELETE can then only target this
    -- retired key; no later writer can ever bind new bytes to it.
    INSERT INTO public.storage_object_tombstones (
        capacity_bucket, object_key, organization_id, project_id, sha256,
        size_bytes, media_type, owner_type, owner_id, idempotency_key,
        accounting_state, claim_token, retired_at
    ) VALUES (
        p_capacity_bucket, v_row.object_key, v_row.organization_id,
        v_row.project_id, v_row.sha256, v_row.size_bytes, v_row.media_type,
        v_row.owner_type, v_row.owner_id, v_row.idempotency_key,
        CASE WHEN v_marker = '4' THEN 'reserved' ELSE 'committed' END,
        p_claim_token, v_now
    );

    IF v_marker = '4' THEN
        IF v_global.reserved_bytes < p_size_bytes OR v_global.reserved_count < 1
           OR v_tenant.reserved_bytes < p_size_bytes OR v_tenant.reserved_count < 1 THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0002',
                MESSAGE = 'STORAGE_QUOTA_INVARIANT: reserved counters would underflow';
        END IF;
        UPDATE public.storage_global_quotas
        SET reserved_bytes = reserved_bytes - p_size_bytes,
            reserved_count = reserved_count - 1, updated_at = v_now
        WHERE id = 1;
        UPDATE public.storage_tenant_quotas
        SET reserved_bytes = reserved_bytes - p_size_bytes,
            reserved_count = reserved_count - 1, updated_at = v_now
        WHERE organization_id = p_organization_id;
    ELSE
        IF v_global.committed_bytes < p_size_bytes OR v_global.committed_count < 1
           OR v_tenant.committed_bytes < p_size_bytes OR v_tenant.committed_count < 1 THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0002',
                MESSAGE = 'STORAGE_QUOTA_INVARIANT: committed counters would underflow';
        END IF;
        UPDATE public.storage_global_quotas
        SET committed_bytes = committed_bytes - p_size_bytes,
            committed_count = committed_count - 1, updated_at = v_now
        WHERE id = 1;
        UPDATE public.storage_tenant_quotas
        SET committed_bytes = committed_bytes - p_size_bytes,
            committed_count = committed_count - 1, updated_at = v_now
        WHERE organization_id = p_organization_id;
    END IF;
    DELETE FROM public.stored_objects
    WHERE organization_id = p_organization_id
      AND object_key = p_object_key AND claim_token = p_claim_token;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0002', MESSAGE = 'STORAGE_QUOTA_INVARIANT: token-bound delete failed';
    END IF;
    RETURN true;
END;
$function$
"""

_CREATE_REJECT_TOMBSTONE_MUTATION = r"""
CREATE OR REPLACE FUNCTION public._custombuild_storage_reject_tombstone_mutation(
) RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '55000',
        MESSAGE = 'STORAGE_TOMBSTONE_IMMUTABLE: retired storage identities are append-only';
END;
$function$
"""

_CREATE_LOCK_CAPACITY = r"""
CREATE OR REPLACE FUNCTION public.custombuild_storage_lock_capacity(
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
BEGIN
    -- The attestor deliberately has SELECT-only table privileges. Acquire the
    -- global writer fence through this narrow entry point before it reads a
    -- tenant-by-tenant ledger snapshot.
    PERFORM 1
    FROM public.storage_global_quotas
    WHERE id = 1
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0002', MESSAGE = 'STORAGE_QUOTA_INVARIANT: global quota is missing';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.storage_global_quotas
        WHERE id = 1 AND maintenance_token IS NOT NULL
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'STORAGE_MAINTENANCE_ACTIVE: capacity attestation is fenced';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.storage_global_quotas
        WHERE id = 1
          AND (
              recovery_database_started_at IS NULL
              OR recovery_completed_at IS NULL
              OR recovery_database_started_at
                 IS DISTINCT FROM pg_catalog.pg_postmaster_start_time()
          )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'STORAGE_RECOVERY_REQUIRED: this database boot is not recovered';
    END IF;
END;
$function$
"""

_CREATE_ATTEST_CAPACITY = r"""
CREATE OR REPLACE FUNCTION public.custombuild_storage_attest_capacity(
    p_provisioned_bytes bigint,
    p_metadata_overhead_bytes bigint,
    p_emergency_reserve_bytes bigint,
    p_byte_limit bigint,
    p_object_limit bigint,
    p_volume_identity text,
    p_capacity_bucket text,
    p_capacity_operator_config_sha256 text,
    p_deploy_descriptor_sha256 text,
    p_inventory_sha256 text,
    p_inventory_object_count bigint,
    p_inventory_bytes bigint,
    p_ledger_object_count bigint,
    p_ledger_bytes bigint,
    p_capacity_attested_at timestamptz,
    p_capacity_evidence_sha256 text
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_global public.storage_global_quotas%ROWTYPE;
    v_now timestamptz;
    v_headroom bigint;
BEGIN
    PERFORM public._custombuild_storage_assert_text(
        'volume_identity', p_volume_identity, 255
    );
    PERFORM public._custombuild_storage_assert_text(
        'capacity_bucket', p_capacity_bucket, 63
    );
    IF p_capacity_operator_config_sha256 IS NULL
       OR p_capacity_operator_config_sha256 !~ '^[0-9a-f]{64}$'
       OR p_deploy_descriptor_sha256 IS NULL
       OR p_deploy_descriptor_sha256 !~ '^[0-9a-f]{64}$'
       OR p_inventory_sha256 IS NULL
       OR p_inventory_sha256 !~ '^[0-9a-f]{64}$'
       OR p_capacity_evidence_sha256 IS NULL
       OR p_capacity_evidence_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'STORAGE_CAPACITY_INVALID: evidence hashes are not canonical';
    END IF;
    IF p_provisioned_bytes IS NULL OR p_provisioned_bytes < 1
       OR p_metadata_overhead_bytes IS NULL OR p_metadata_overhead_bytes < 1
       OR p_emergency_reserve_bytes IS NULL OR p_emergency_reserve_bytes < 1
       OR p_byte_limit IS NULL OR p_byte_limit < 1
       OR p_object_limit IS NULL OR p_object_limit < 1
       OR p_inventory_object_count IS NULL OR p_inventory_object_count < 0
       OR p_inventory_bytes IS NULL OR p_inventory_bytes < 0
       OR p_ledger_object_count IS NULL OR p_ledger_object_count < 0
       OR p_ledger_bytes IS NULL OR p_ledger_bytes < 0
       OR p_capacity_attested_at IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023', MESSAGE = 'STORAGE_CAPACITY_INVALID: numeric evidence is incomplete';
    END IF;
    IF p_metadata_overhead_bytes::numeric + p_emergency_reserve_bytes::numeric
       > 9223372036854775807::numeric THEN
        RAISE EXCEPTION USING
            ERRCODE = '22003', MESSAGE = 'STORAGE_CAPACITY_INVALID: headroom overflows bigint';
    END IF;
    v_headroom := p_metadata_overhead_bytes + p_emergency_reserve_bytes;
    IF p_provisioned_bytes <= v_headroom
       OR p_byte_limit > p_provisioned_bytes - v_headroom THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'STORAGE_CAPACITY_INVALID: usable capacity exceeds '
                || 'physical capacity';
    END IF;

    SELECT * INTO v_global
    FROM public.storage_global_quotas
    WHERE id = 1
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0002', MESSAGE = 'STORAGE_QUOTA_INVARIANT: global quota is missing';
    END IF;
    v_now := pg_catalog.clock_timestamp();
    IF v_global.maintenance_token IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'STORAGE_MAINTENANCE_ACTIVE: capacity attestation is fenced';
    END IF;
    IF v_global.recovery_database_started_at IS NULL
       OR v_global.recovery_completed_at IS NULL
       OR v_global.recovery_database_started_at
          IS DISTINCT FROM pg_catalog.pg_postmaster_start_time() THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'STORAGE_RECOVERY_REQUIRED: this database boot is not recovered';
    END IF;
    IF p_capacity_attested_at < v_now - INTERVAL '5 minutes'
       OR p_capacity_attested_at > v_now + INTERVAL '1 minute' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'STORAGE_CAPACITY_INVALID: attestation timestamp is stale '
                || 'or future-dated';
    END IF;
    IF p_inventory_object_count <> p_ledger_object_count
       OR p_inventory_bytes <> p_ledger_bytes
       OR p_ledger_object_count <> v_global.committed_count
       OR p_ledger_bytes <> v_global.committed_bytes
       OR p_byte_limit < v_global.committed_bytes + v_global.reserved_bytes
       OR p_object_limit < v_global.committed_count + v_global.reserved_count THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'STORAGE_CAPACITY_INVALID: inventory, ledger or active '
                || 'counters differ';
    END IF;

    UPDATE public.storage_global_quotas
    SET byte_limit = p_byte_limit,
        object_limit = p_object_limit,
        capacity_verified = true,
        provisioned_bytes = p_provisioned_bytes,
        metadata_overhead_bytes = p_metadata_overhead_bytes,
        emergency_reserve_bytes = p_emergency_reserve_bytes,
        capacity_headroom_bytes = v_headroom,
        volume_identity = p_volume_identity,
        capacity_bucket = p_capacity_bucket,
        capacity_operator_config_sha256 = p_capacity_operator_config_sha256,
        deploy_descriptor_sha256 = p_deploy_descriptor_sha256,
        inventory_sha256 = p_inventory_sha256,
        inventory_object_count = p_inventory_object_count,
        inventory_bytes = p_inventory_bytes,
        ledger_object_count = p_ledger_object_count,
        ledger_bytes = p_ledger_bytes,
        capacity_attested_at = p_capacity_attested_at,
        capacity_verified_at = v_now,
        capacity_evidence_sha256 = p_capacity_evidence_sha256,
        updated_at = v_now
    WHERE id = 1;
END;
$function$
"""

_CREATE_INVALIDATE_CAPACITY = r"""
CREATE OR REPLACE FUNCTION public.custombuild_storage_invalidate_capacity(
    p_failure_evidence_sha256 text
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
BEGIN
    IF p_failure_evidence_sha256 IS NULL
       OR p_failure_evidence_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'STORAGE_CAPACITY_INVALID: failure evidence hash is not '
                || 'canonical';
    END IF;
    UPDATE public.storage_global_quotas
    SET capacity_verified = false,
        capacity_verified_at = pg_catalog.clock_timestamp(),
        capacity_evidence_sha256 = p_failure_evidence_sha256,
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0002', MESSAGE = 'STORAGE_QUOTA_INVARIANT: global quota is missing';
    END IF;
END;
$function$
"""

_CREATE_FUNCTIONS = (
    _CREATE_ASSERT_UUID,
    _CREATE_ASSERT_TEXT,
    _CREATE_REQUIRE_TENANT,
    _CREATE_ASSERT_CLAIMS,
    _CREATE_IDENTITY_MATCH,
    _CREATE_RESERVE,
    _CREATE_RENEW,
    _CREATE_COMMIT,
    _CREATE_CLAIM_REAP_HELPER,
    _CREATE_ASSERT_REAP_BUCKET,
    _CREATE_CLAIM_EXPIRED,
    _CREATE_CLAIM_DELETE_PENDING,
    _CREATE_ENFORCE_DOMAIN_REFERENCE,
    _CREATE_PREPARE_GENERATION_RETRY,
    _CREATE_ENFORCE_GENERATION_LIVENESS,
    _CREATE_FINALIZE_REAP,
    _CREATE_REJECT_TOMBSTONE_MUTATION,
    _CREATE_LOCK_CAPACITY,
    _CREATE_ATTEST_CAPACITY,
    _CREATE_INVALIDATE_CAPACITY,
)


def upgrade() -> None:
    # Role creation/password rotation belongs to the bootstrap administrator,
    # never to the NOCREATEROLE migrator. Fail clearly on upgraded clusters
    # until that fixed role has been provisioned rather than silently running
    # the long-lived attestor with migration authority.
    op.execute(
        "DO $role$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles "
        "WHERE rolname = 'custombuild_storage_attestor') THEN "
        "RAISE EXCEPTION USING ERRCODE = '42704', "
        "MESSAGE = 'custombuild_storage_attestor must be provisioned before migration 0013'; "
        "END IF; END $role$"
    )
    # PostgreSQL grants EXECUTE on newly created functions to PUBLIC by
    # default. Freeze future routines before creating this revision's entry
    # points, then sweep all current routines and regrant only the allow-list.
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator "
        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator "
        "REVOKE EXECUTE ON FUNCTIONS FROM custombuild_api, custombuild_worker, "
        "custombuild_storage_attestor"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
        "REVOKE EXECUTE ON FUNCTIONS FROM custombuild_api, custombuild_worker, "
        "custombuild_storage_attestor"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
        "REVOKE ALL PRIVILEGES ON TABLES FROM custombuild_storage_attestor"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
        "REVOKE ALL PRIVILEGES ON SEQUENCES FROM custombuild_storage_attestor"
    )
    for statement in _CREATE_FUNCTIONS:
        op.execute(statement)
    for table_name, trigger_name in _DOMAIN_REFERENCE_TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON public.{table_name}")
        op.execute(
            f"CREATE CONSTRAINT TRIGGER {trigger_name} "
            f"AFTER INSERT OR UPDATE ON public.{table_name} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION public._custombuild_storage_enforce_domain_reference()"
        )
    op.execute(f"DROP TRIGGER IF EXISTS {_GENERATION_LIVENESS_TRIGGER} ON public.generation_jobs")
    op.execute(
        f"CREATE TRIGGER {_GENERATION_LIVENESS_TRIGGER} "
        "BEFORE INSERT OR UPDATE ON public.generation_jobs "
        "FOR EACH ROW "
        "EXECUTE FUNCTION public._custombuild_storage_enforce_generation_liveness()"
    )
    op.execute(
        f"DROP TRIGGER IF EXISTS {_TOMBSTONE_APPEND_ONLY_TRIGGER} "
        "ON public.storage_object_tombstones"
    )
    op.execute(
        f"CREATE TRIGGER {_TOMBSTONE_APPEND_ONLY_TRIGGER} "
        "BEFORE UPDATE OR DELETE ON public.storage_object_tombstones "
        "FOR EACH ROW "
        "EXECUTE FUNCTION public._custombuild_storage_reject_tombstone_mutation()"
    )

    # Rebuild the storage ACL after every routine exists.  Table reads remain
    # visible for invariant checks, while every mutation and helper invocation
    # stays behind the migrator-owned security boundary.
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE storage_global_quotas, "
        "storage_tenant_quotas, stored_objects, storage_object_tombstones "
        f"FROM {_RUNTIME_ROLES}"
    )
    op.execute(
        "GRANT SELECT ON TABLE storage_global_quotas, storage_tenant_quotas, "
        f"stored_objects TO {_RUNTIME_ROLES}"
    )
    op.execute("REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {_RUNTIME_ROLES}")
    op.execute(
        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM custombuild_storage_attestor"
    )
    op.execute(
        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM custombuild_storage_attestor"
    )
    op.execute(
        "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM custombuild_storage_attestor"
    )
    op.execute("REVOKE ALL PRIVILEGES ON SCHEMA public FROM custombuild_storage_attestor")
    op.execute("GRANT USAGE ON SCHEMA public TO custombuild_storage_attestor")
    for signature in (
        *_HELPER_FUNCTIONS,
        *_PUBLIC_FUNCTIONS,
        *_API_FUNCTIONS,
        *_ATTESTOR_FUNCTIONS,
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO custombuild_migrator")
        op.execute(f"REVOKE ALL PRIVILEGES ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"REVOKE ALL PRIVILEGES ON FUNCTION {signature} FROM {_RUNTIME_ROLES}")
    for signature in _PUBLIC_FUNCTIONS[:3]:
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_RUNTIME_ROLES}")
    for signature in _PUBLIC_FUNCTIONS[3:]:
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO custombuild_worker")
    for signature in _API_FUNCTIONS:
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO custombuild_api")
    op.execute(
        "GRANT SELECT ON TABLE organizations, storage_global_quotas, "
        "storage_tenant_quotas, stored_objects, storage_object_tombstones "
        "TO custombuild_storage_attestor"
    )
    for signature in _ATTESTOR_FUNCTIONS:
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO custombuild_storage_attestor")


def downgrade() -> None:
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE organizations, storage_global_quotas, "
        "storage_tenant_quotas, stored_objects, storage_object_tombstones "
        "FROM custombuild_storage_attestor"
    )
    for table_name, trigger_name in reversed(_DOMAIN_REFERENCE_TRIGGERS):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON public.{table_name}")
    op.execute(f"DROP TRIGGER IF EXISTS {_GENERATION_LIVENESS_TRIGGER} ON public.generation_jobs")
    op.execute(
        f"DROP TRIGGER IF EXISTS {_TOMBSTONE_APPEND_ONLY_TRIGGER} "
        "ON public.storage_object_tombstones"
    )
    for signature in reversed(
        (*_HELPER_FUNCTIONS, *_PUBLIC_FUNCTIONS, *_API_FUNCTIONS, *_ATTESTOR_FUNCTIONS)
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    # Revision 0012 used direct ORM mutations.  Restore precisely that old ACL
    # when explicitly downgrading across this security boundary.
    op.execute(f"GRANT SELECT, UPDATE ON TABLE storage_global_quotas TO {_RUNTIME_ROLES}")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE storage_tenant_quotas, stored_objects "
        f"TO {_RUNTIME_ROLES}"
    )
