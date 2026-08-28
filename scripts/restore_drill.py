"""Restore a coordinated backup into disposable, isolated Docker resources."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

try:
    from scripts.compose_backup import (
        POSTGRES_IMAGE,
        TOMBSTONE_HISTORY_SQL,
        VOLUME_INIT_IMAGE,
        BackupError,
        inventory_s3,
        verify_manifest,
    )
    from scripts.postgres_runtime_privileges import runtime_privileges_sql
except ModuleNotFoundError:
    from compose_backup import (  # type: ignore[import-not-found,no-redef]  # noqa: I001
        POSTGRES_IMAGE,
        TOMBSTONE_HISTORY_SQL,
        VOLUME_INIT_IMAGE,
        BackupError,
        inventory_s3,
        verify_manifest,
    )
    from postgres_runtime_privileges import runtime_privileges_sql  # type: ignore[import-not-found,no-redef]


SAFE_NAME = re.compile(r"^custombuild-restore-[a-f0-9]{8}(?:-(?:postgres|seaweed|extract))?$")
RESTORE_ACCESS_KEY = "custombuild-restore"
RESTORE_SECRET_KEY = "restore-drill-only-object-secret"  # noqa: S105 - disposable local drill
RESTORE_MIGRATOR_PASSWORD = "restore-drill-only-migrator-secret"  # noqa: S105
RESTORE_API_PASSWORD = "restore-drill-only-api-secret"  # noqa: S105
RESTORE_WORKER_PASSWORD = "restore-drill-only-worker-secret"  # noqa: S105
RESTORE_ATTESTOR_PASSWORD = "restore-drill-only-capacity-attestor-secret"  # noqa: S105
RESTORE_SCHEMA = "custombuild.restore-drill.v4"
POSTGRES_INIT_COMPLETE_MARKER = "PostgreSQL init process complete"
DOCKER_COMMAND_TIMEOUT_SECONDS = 120
DOCKER_PAYLOAD_TIMEOUT_SECONDS = 2 * 60 * 60
DOCKER_INSPECT_TIMEOUT_SECONDS = 30
DOCKER_PROBE_TIMEOUT_SECONDS = 5.0
DOCKER_CLEANUP_TIMEOUT_SECONDS = 30


def docker_executable() -> str:
    executable = shutil.which("docker")
    if not executable:
        raise BackupError("Docker CLI is not available")
    return executable


def docker(
    *arguments: str,
    capture: bool = False,
    timeout_seconds: int = DOCKER_COMMAND_TIMEOUT_SECONDS,
) -> str:
    if timeout_seconds <= 0:
        raise BackupError("Docker commands require a positive timeout")
    try:
        process = subprocess.run(  # noqa: S603 - explicit Docker argv, without a shell.
            [docker_executable(), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackupError(
            f"Docker operation timed out after {timeout_seconds} seconds; inspect the "
            "container runtime, remove only the named restore-drill resources and retry"
        ) from exc
    except OSError as exc:
        raise BackupError(
            "Docker operation could not start; verify the container runtime and retry"
        ) from exc
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip() or "Docker command failed"
        raise BackupError(detail)
    return process.stdout.strip() if capture else ""


def docker_resource_exists(
    *arguments: str,
    timeout_seconds: int = DOCKER_INSPECT_TIMEOUT_SECONDS,
) -> bool:
    if timeout_seconds <= 0:
        raise BackupError("Docker resource inspection requires a positive timeout")
    try:
        process = subprocess.run(  # noqa: S603 - explicit Docker argv, without a shell.
            [docker_executable(), *arguments],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackupError(
            f"Docker resource inspection timed out after {timeout_seconds} seconds; "
            "inspect the container runtime and retry"
        ) from exc
    except OSError as exc:
        raise BackupError(
            "Docker resource inspection could not start; verify the container runtime"
        ) from exc
    return process.returncode == 0


def validate_temporary_name(name: str) -> None:
    if not SAFE_NAME.fullmatch(name):
        raise BackupError(f"Unsafe restore-drill resource name: {name}")


def ensure_targets_absent(container_names: tuple[str, ...], volume_name: str) -> None:
    for name in container_names:
        validate_temporary_name(name)
        if docker_resource_exists("container", "inspect", name):
            raise BackupError(f"Refusing to reuse existing restore-drill container: {name}")
    validate_temporary_name(volume_name)
    if docker_resource_exists("volume", "inspect", volume_name):
        raise BackupError(f"Refusing to reuse existing restore-drill volume: {volume_name}")


def wait_for_postgres(container: str, timeout_seconds: int = 60) -> None:
    if timeout_seconds <= 0:
        raise BackupError("Disposable PostgreSQL readiness requires a positive timeout")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        probe_timeout = max(0.05, min(DOCKER_PROBE_TIMEOUT_SECONDS, remaining))
        try:
            logs = subprocess.run(  # noqa: S603 - validated container and fixed Docker argv.
                [docker_executable(), "logs", container],
                check=False,
                capture_output=True,
                text=True,
                timeout=probe_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackupError(
                "Disposable PostgreSQL log probe timed out; inspect and remove the exact "
                "restore-drill container, then retry"
            ) from exc
        except OSError as exc:
            raise BackupError(
                "Disposable PostgreSQL log probe could not start; verify Docker and retry"
            ) from exc
        combined_logs = f"{logs.stdout}\n{logs.stderr}"
        if logs.returncode != 0 or POSTGRES_INIT_COMPLETE_MARKER not in combined_logs:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            continue
        remaining = deadline - time.monotonic()
        probe_timeout = max(0.05, min(DOCKER_PROBE_TIMEOUT_SECONDS, remaining))
        try:
            process = subprocess.run(  # noqa: S603 - validated container, fixed Docker argv.
                [
                    docker_executable(),
                    "exec",
                    container,
                    "pg_isready",
                    "-U",
                    "postgres",
                    "-d",
                    "custombuild",
                ],
                check=False,
                capture_output=True,
                timeout=probe_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackupError(
                "Disposable PostgreSQL readiness probe timed out; inspect and remove the "
                "exact restore-drill container, then retry"
            ) from exc
        except OSError as exc:
            raise BackupError(
                "Disposable PostgreSQL readiness probe could not start; verify Docker and retry"
            ) from exc
        if process.returncode == 0:
            return
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    raise BackupError(
        "Disposable PostgreSQL did not become ready; inspect its logs, remove the exact "
        "restore-drill resources and retry"
    )


def wait_for_seaweed_inventory(
    endpoint: str,
    bucket: str,
    *,
    timeout_seconds: int = 90,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    last_error: BackupError | None = None
    while time.monotonic() < deadline:
        try:
            return inventory_s3(
                endpoint,
                RESTORE_ACCESS_KEY,
                RESTORE_SECRET_KEY,
                bucket,
            )
        except BackupError as exc:
            last_error = exc
            time.sleep(1)
    raise BackupError(f"Restored SeaweedFS did not become ready: {last_error}")


def current_alembic_heads(repo: Path) -> list[str]:
    ini_path = (repo / "services" / "api" / "alembic.ini").resolve()
    versions_path = (repo / "services" / "api" / "alembic").resolve()
    if not ini_path.is_file() or not versions_path.is_dir():
        raise BackupError("Could not locate the repository Alembic configuration")
    config = AlembicConfig(str(ini_path))
    config.set_main_option("script_location", str(versions_path))
    config.set_main_option("path_separator", "os")
    heads = sorted(ScriptDirectory.from_config(config).get_heads())
    if not heads:
        raise BackupError("Repository Alembic history has no head revision")
    return heads


def _published_s3_endpoint(container: str) -> str:
    binding = docker("port", container, "8333/tcp", capture=True)
    matches = re.findall(r"(?:127\.0\.0\.1|0\.0\.0\.0|\[::\]):([0-9]+)", binding)
    if not matches:
        raise BackupError(f"Could not resolve restored SeaweedFS S3 port: {binding}")
    return f"http://127.0.0.1:{matches[-1]}"


def _restored_database_probe(container: str) -> dict[str, Any]:
    query = (
        "SELECT json_build_object("  # noqa: S608 - fixed shared SQL constant only.
        "'alembic_heads', COALESCE((SELECT json_agg(version_num ORDER BY version_num) "
        "FROM alembic_version), '[]'::json), "
        "'row_counts', COALESCE((SELECT json_object_agg(tablename, row_count ORDER BY tablename) "
        "FROM (SELECT tablename, (((xpath('/row/count/text()', query_to_xml("
        "format('SELECT count(*) AS count FROM %I.%I', schemaname, tablename), "
        "false, true, ''))))[1]::text)::bigint AS row_count "
        "FROM pg_tables WHERE schemaname = 'public') AS counts), '{}'::json), "
        f"'tombstone_history', {TOMBSTONE_HISTORY_SQL})::text;"
    )
    raw = docker(
        "exec",
        container,
        "psql",
        "-U",
        "postgres",
        "-d",
        "custombuild",
        "--tuples-only",
        "--no-align",
        "--command",
        query,
        capture=True,
    )
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupError("Restored database probe returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise BackupError("Restored database probe did not return an object")
    return {str(key): value for key, value in result.items()}


def _verified_restored_tombstone_history(
    expected_snapshot: dict[str, Any],
    restored_probe: dict[str, Any],
) -> dict[str, Any]:
    expected = expected_snapshot.get("tombstone_history")
    restored = restored_probe.get("tombstone_history")
    if not isinstance(expected, dict) or not isinstance(restored, dict) or restored != expected:
        raise BackupError("Restored tombstone history does not match the backup")
    return {str(key): value for key, value in restored.items()}


def _restore_runtime_privileges(container: str) -> None:
    role_hardening_sql = """
ALTER ROLE custombuild_migrator WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE custombuild_api WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE custombuild_worker WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE custombuild_storage_attestor WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOINHERIT NOREPLICATION NOBYPASSRLS;
REVOKE custombuild_api, custombuild_worker, custombuild_storage_attestor
  FROM custombuild_migrator;
REVOKE custombuild_migrator, custombuild_worker, custombuild_storage_attestor
  FROM custombuild_api;
REVOKE custombuild_migrator, custombuild_api, custombuild_storage_attestor
  FROM custombuild_worker;
REVOKE custombuild_migrator, custombuild_api, custombuild_worker
  FROM custombuild_storage_attestor;
GRANT CONNECT ON DATABASE custombuild
  TO custombuild_migrator, custombuild_api, custombuild_worker,
     custombuild_storage_attestor;
GRANT CREATE ON DATABASE custombuild TO custombuild_migrator;
GRANT USAGE, CREATE ON SCHEMA public TO custombuild_migrator;
"""
    sql = role_hardening_sql + runtime_privileges_sql()
    docker(
        "exec",
        container,
        "psql",
        "-U",
        "postgres",
        "-d",
        "custombuild",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    )


def _runtime_role_probe(container: str) -> dict[str, Any]:
    role_query = """
SELECT json_build_object(
  'migrator_safe', (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
    AND NOT rolcreaterole AND NOT rolinherit AND NOT rolreplication AND NOT rolbypassrls
    FROM pg_roles WHERE rolname = 'custombuild_migrator'),
  'api_safe', (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
    AND NOT rolcreaterole AND NOT rolinherit AND NOT rolreplication AND NOT rolbypassrls
    FROM pg_roles WHERE rolname = 'custombuild_api'),
  'worker_safe', (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
    AND NOT rolcreaterole AND NOT rolinherit AND NOT rolreplication AND NOT rolbypassrls
    FROM pg_roles WHERE rolname = 'custombuild_worker'),
  'attestor_safe', (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
    AND NOT rolcreaterole AND NOT rolinherit AND NOT rolreplication AND NOT rolbypassrls
    FROM pg_roles WHERE rolname = 'custombuild_storage_attestor'),
  'api_tombstone_privileges_absent', NOT (
    has_table_privilege('custombuild_api', 'public.storage_object_tombstones', 'SELECT')
    OR has_table_privilege('custombuild_api', 'public.storage_object_tombstones', 'INSERT')
    OR has_table_privilege('custombuild_api', 'public.storage_object_tombstones', 'UPDATE')
    OR has_table_privilege('custombuild_api', 'public.storage_object_tombstones', 'DELETE')
    OR has_table_privilege('custombuild_api', 'public.storage_object_tombstones', 'TRUNCATE')
    OR has_table_privilege('custombuild_api', 'public.storage_object_tombstones', 'REFERENCES')
    OR has_table_privilege('custombuild_api', 'public.storage_object_tombstones', 'TRIGGER')
    OR has_table_privilege('custombuild_api', 'public.storage_object_tombstones', 'MAINTAIN')),
  'worker_tombstone_privileges_absent', NOT (
    has_table_privilege('custombuild_worker', 'public.storage_object_tombstones', 'SELECT')
    OR has_table_privilege('custombuild_worker', 'public.storage_object_tombstones', 'INSERT')
    OR has_table_privilege('custombuild_worker', 'public.storage_object_tombstones', 'UPDATE')
    OR has_table_privilege('custombuild_worker', 'public.storage_object_tombstones', 'DELETE')
    OR has_table_privilege('custombuild_worker', 'public.storage_object_tombstones', 'TRUNCATE')
    OR has_table_privilege('custombuild_worker', 'public.storage_object_tombstones', 'REFERENCES')
    OR has_table_privilege('custombuild_worker', 'public.storage_object_tombstones', 'TRIGGER')
    OR has_table_privilege('custombuild_worker', 'public.storage_object_tombstones', 'MAINTAIN')),
  'attestor_tombstone_select_only',
    has_table_privilege(
      'custombuild_storage_attestor', 'public.storage_object_tombstones', 'SELECT'
    ) AND NOT (
      has_table_privilege(
        'custombuild_storage_attestor', 'public.storage_object_tombstones', 'INSERT'
      )
      OR has_table_privilege(
        'custombuild_storage_attestor', 'public.storage_object_tombstones', 'UPDATE'
      )
      OR has_table_privilege(
        'custombuild_storage_attestor', 'public.storage_object_tombstones', 'DELETE'
      )
      OR has_table_privilege(
        'custombuild_storage_attestor', 'public.storage_object_tombstones', 'TRUNCATE'
      )
      OR has_table_privilege(
        'custombuild_storage_attestor', 'public.storage_object_tombstones', 'REFERENCES'
      )
      OR has_table_privilege(
        'custombuild_storage_attestor', 'public.storage_object_tombstones', 'TRIGGER'
      )
      OR has_table_privilege(
        'custombuild_storage_attestor', 'public.storage_object_tombstones', 'MAINTAIN'
      )),
  'tombstone_column_grants_absent', NOT EXISTS (
    SELECT 1
    FROM pg_attribute attribute
    JOIN pg_class object ON object.oid = attribute.attrelid
    JOIN pg_namespace namespace ON namespace.oid = object.relnamespace
    CROSS JOIN LATERAL aclexplode(attribute.attacl) privilege
    LEFT JOIN pg_roles grantee ON grantee.oid = privilege.grantee
    WHERE namespace.nspname = 'public'
      AND object.relname = 'storage_object_tombstones'
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
      AND (
        privilege.grantee = 0
        OR grantee.rolname IN (
          'custombuild_api', 'custombuild_worker', 'custombuild_storage_attestor'
        ))),
  'memberships_absent', NOT EXISTS (
    SELECT 1 FROM pg_auth_members membership
    JOIN pg_roles member_role ON member_role.oid = membership.member
    JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
    WHERE member_role.rolname IN (
      'custombuild_api', 'custombuild_worker', 'custombuild_storage_attestor'
    ) OR granted_role.rolname = 'custombuild_storage_attestor'),
  'public_object_grants_absent', NOT EXISTS (
    SELECT 1 FROM pg_class object
    JOIN pg_namespace namespace ON namespace.oid = object.relnamespace
    CROSS JOIN LATERAL aclexplode(object.relacl) privilege
    WHERE namespace.nspname = 'public'
      AND object.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
      AND privilege.grantee = 0),
  'all_public_objects_owned_by_migrator', NOT EXISTS (
    SELECT 1 FROM pg_class object
    JOIN pg_namespace namespace ON namespace.oid = object.relnamespace
    WHERE namespace.nspname = 'public'
      AND object.relkind IN ('r', 'p', 'S', 'v', 'm')
      AND pg_get_userbyid(object.relowner) <> 'custombuild_migrator'))::text;
"""
    raw_roles = docker(
        "exec",
        container,
        "psql",
        "-U",
        "postgres",
        "-d",
        "custombuild",
        "--tuples-only",
        "--no-align",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        role_query,
        capture=True,
    )
    try:
        roles = json.loads(raw_roles)
    except json.JSONDecodeError as exc:
        raise BackupError("Restored database role probe returned invalid JSON") from exc
    if roles != {
        "migrator_safe": True,
        "api_safe": True,
        "worker_safe": True,
        "attestor_safe": True,
        "api_tombstone_privileges_absent": True,
        "worker_tombstone_privileges_absent": True,
        "attestor_tombstone_select_only": True,
        "tombstone_column_grants_absent": True,
        "memberships_absent": True,
        "public_object_grants_absent": True,
        "all_public_objects_owned_by_migrator": True,
    }:
        raise BackupError("Restored database runtime-role attributes are unsafe")

    setup_query = """
DO $$
DECLARE source_organization text;
BEGIN
  SELECT organization_id INTO source_organization FROM projects ORDER BY id LIMIT 1;
  IF source_organization IS NULL THEN
    RAISE EXCEPTION 'restore drill requires at least one project';
  END IF;
  INSERT INTO organizations (id, name, slug, created_at, updated_at)
  VALUES ('ffffffff-ffff-ffff-ffff-ffffffffffff', 'Restore isolation probe',
          'restore-isolation-probe', now(), now());
  INSERT INTO projects (id, name, description, furniture_type, current_revision,
                        archived, created_at, updated_at, organization_id)
  VALUES ('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', 'Restore isolation probe', '',
          'shelving', 0, false, now(), now(),
          'ffffffff-ffff-ffff-ffff-ffffffffffff');
  INSERT INTO outbox_events
    (id, event_key, topic, payload_json, dispatched_at, attempts, created_at,
     updated_at, organization_id, dead_lettered_at, last_error)
  VALUES
    ('dddddddd-dddd-4ddd-8ddd-dddddddddddd',
     'restore-worker-original-tenant', 'restore.probe', '{}'::json, NULL, 0,
     now(), now(), source_organization, NULL, NULL),
    ('cccccccc-cccc-4ccc-8ccc-cccccccccccc',
     'restore-worker-foreign-tenant', 'restore.probe', '{}'::json, NULL, 0,
     now(), now(), 'ffffffff-ffff-ffff-ffff-ffffffffffff', NULL, NULL);
END $$;
SELECT organization_id FROM projects
WHERE organization_id <> 'ffffffff-ffff-ffff-ffff-ffffffffffff'
ORDER BY id LIMIT 1;
"""
    original_tenant = (
        docker(
            "exec",
            container,
            "psql",
            "-U",
            "postgres",
            "-d",
            "custombuild",
            "--tuples-only",
            "--no-align",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            setup_query,
            capture=True,
        )
        .splitlines()[-1]
        .strip()
    )
    if not original_tenant:
        raise BackupError("Restore drill could not select a tenant for the RLS probe")

    api_query = (
        "SELECT set_config('app.current_organization_id', '"
        + original_tenant.replace("'", "''")
        + "', false); "
        "SELECT json_build_object('visible', count(*), 'foreign', count(*) FILTER "
        "(WHERE organization_id = 'ffffffff-ffff-ffff-ffff-ffffffffffff'))::text "
        "FROM projects;"
    )
    api_raw = docker(
        "exec",
        "--env",
        f"PGPASSWORD={RESTORE_API_PASSWORD}",
        container,
        "psql",
        "-U",
        "custombuild_api",
        "-d",
        "custombuild",
        "--tuples-only",
        "--no-align",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        api_query,
        capture=True,
    ).splitlines()[-1]
    try:
        api_result = json.loads(api_raw)
    except json.JSONDecodeError as exc:
        raise BackupError("Restored API-role RLS probe returned invalid JSON") from exc
    if int(api_result.get("visible", 0)) < 1 or api_result.get("foreign") != 0:
        raise BackupError("Restored API role did not enforce tenant RLS")

    worker_query = (
        "SELECT set_config('app.current_organization_id', '"
        + original_tenant.replace("'", "''")
        + "', false); "
        "SELECT json_build_object('visible', count(*), 'foreign', count(*) FILTER "
        "(WHERE organization_id = 'ffffffff-ffff-ffff-ffff-ffffffffffff'))::text "
        "FROM outbox_events WHERE id IN "
        "('dddddddd-dddd-4ddd-8ddd-dddddddddddd', "
        "'cccccccc-cccc-4ccc-8ccc-cccccccccccc');"
    )
    worker_raw = docker(
        "exec",
        "--env",
        f"PGPASSWORD={RESTORE_WORKER_PASSWORD}",
        container,
        "psql",
        "-U",
        "custombuild_worker",
        "-d",
        "custombuild",
        "--tuples-only",
        "--no-align",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        worker_query,
        capture=True,
    ).splitlines()[-1]
    try:
        worker_result = json.loads(worker_raw)
    except json.JSONDecodeError as exc:
        raise BackupError("Restored worker-role RLS probe returned invalid JSON") from exc
    if worker_result != {"visible": 1, "foreign": 0}:
        raise BackupError("Restored worker role did not enforce its outbox tenant boundary")
    docker(
        "exec",
        "--env",
        f"PGPASSWORD={RESTORE_MIGRATOR_PASSWORD}",
        container,
        "psql",
        "-U",
        "custombuild_migrator",
        "-d",
        "custombuild",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        (
            "CREATE TABLE public.restore_migration_probe (id integer PRIMARY KEY); "
            "ALTER TABLE public.restore_migration_probe ADD COLUMN checked boolean NOT NULL "
            "DEFAULT true; DROP TABLE public.restore_migration_probe;"
        ),
    )
    return {
        "roles": roles,
        "api_rls": api_result,
        "worker_rls": worker_result,
        "migrator_schema_mutation_verified": True,
    }


def _cleanup_resource(kind: str, name: str, errors: list[str]) -> None:
    validate_temporary_name(name)
    exists: bool | None = None
    try:
        exists = docker_resource_exists(
            kind,
            "inspect",
            name,
            timeout_seconds=DOCKER_INSPECT_TIMEOUT_SECONDS,
        )
    except BackupError as exc:
        errors.append(f"{kind} {name} inspection: {exc}")
    if exists is False:
        return
    # If inspection itself timed out, the exact narrowly validated resource may
    # still exist.  Make one independent bounded delete attempt before moving on
    # to the remaining cleanup targets.
    try:
        if kind == "container":
            docker(
                "rm",
                "--force",
                name,
                timeout_seconds=DOCKER_CLEANUP_TIMEOUT_SECONDS,
            )
        elif kind == "volume":
            docker(
                "volume",
                "rm",
                name,
                timeout_seconds=DOCKER_CLEANUP_TIMEOUT_SECONDS,
            )
        else:
            raise BackupError(f"Unsupported restore-drill resource kind: {kind}")
    except BackupError as exc:
        errors.append(f"{kind} {name} removal: {exc}")


def run_restore_drill(backup: Path, *, repo: Path | None = None) -> dict[str, object]:
    backup = backup.resolve()
    repo = (repo or Path.cwd()).resolve()
    manifest = verify_manifest(backup)
    suffix = uuid4().hex[:8]
    base = f"custombuild-restore-{suffix}"
    postgres_container = f"{base}-postgres"
    seaweed_container = f"{base}-seaweed"
    extract_container = f"{base}-extract"
    object_volume = base
    container_names = (postgres_container, seaweed_container, extract_container)
    ensure_targets_absent(container_names, object_volume)

    object_store = manifest["object_store"]
    if not isinstance(object_store, dict):
        raise BackupError("Backup manifest has no object-store evidence")
    bucket = str(object_store["bucket"])
    expected_inventory = object_store["objects"]
    if not isinstance(expected_inventory, list):
        raise BackupError("Backup manifest has no S3 object inventory")
    database_snapshot = manifest["database_snapshot"]
    if not isinstance(database_snapshot, dict):
        raise BackupError("Backup manifest has no PostgreSQL recovery point")
    expected_heads = sorted(str(value) for value in database_snapshot["alembic_heads"])
    expected_row_counts = database_snapshot.get("row_counts")
    if not isinstance(expected_row_counts, dict):
        raise BackupError("Backup manifest has no exact PostgreSQL row counts")
    if not isinstance(database_snapshot.get("tombstone_history"), dict):
        raise BackupError("Backup manifest has no exact tombstone history")
    repository_heads = current_alembic_heads(repo)

    cleanup_errors: list[str] = []
    operation_error: BaseException | None = None
    result: dict[str, object] | None = None
    try:
        docker(
            "run",
            "--detach",
            "--name",
            postgres_container,
            "--env",
            "POSTGRES_PASSWORD=restore-drill-only",
            "--env",
            "POSTGRES_DB=custombuild",
            POSTGRES_IMAGE,
        )
        wait_for_postgres(postgres_container)
        docker(
            "exec",
            postgres_container,
            "psql",
            "-U",
            "postgres",
            "-d",
            "custombuild",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            (
                "CREATE ROLE custombuild_migrator LOGIN PASSWORD '"
                + RESTORE_MIGRATOR_PASSWORD
                + "' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS; "
                "CREATE ROLE custombuild_api LOGIN PASSWORD '"
                + RESTORE_API_PASSWORD
                + "' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS; "
                "CREATE ROLE custombuild_worker LOGIN PASSWORD '"
                + RESTORE_WORKER_PASSWORD
                + "' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS; "
                "CREATE ROLE custombuild_storage_attestor LOGIN PASSWORD '"
                + RESTORE_ATTESTOR_PASSWORD
                + "' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS; "
                "GRANT CONNECT ON DATABASE custombuild TO custombuild_migrator, "
                "custombuild_storage_attestor; "
                "ALTER SCHEMA public OWNER TO custombuild_migrator; "
                "GRANT USAGE, CREATE ON SCHEMA public TO custombuild_migrator;"
            ),
        )
        restore_path = "/database.dump"
        docker(
            "cp",
            str(backup / "database.dump"),
            f"{postgres_container}:{restore_path}",
            timeout_seconds=DOCKER_PAYLOAD_TIMEOUT_SECONDS,
        )
        docker(
            "exec",
            "--env",
            f"PGPASSWORD={RESTORE_MIGRATOR_PASSWORD}",
            postgres_container,
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--username",
            "custombuild_migrator",
            "--dbname",
            "custombuild",
            restore_path,
            timeout_seconds=DOCKER_PAYLOAD_TIMEOUT_SECONDS,
        )
        database_probe = _restored_database_probe(postgres_container)
        restored_heads = sorted(str(value) for value in database_probe.get("alembic_heads", []))
        restored_row_counts = database_probe.get("row_counts")
        restored_tombstone_history = _verified_restored_tombstone_history(
            database_snapshot,
            database_probe,
        )
        if restored_heads != expected_heads:
            raise BackupError(
                f"Restored Alembic heads {restored_heads} do not match backup {expected_heads}"
            )
        if restored_heads != repository_heads:
            raise BackupError(
                "Restored Alembic heads "
                f"{restored_heads} do not match repository {repository_heads}"
            )
        if restored_row_counts != expected_row_counts:
            raise BackupError("Restored exact table row counts do not match the backup")
        project_rows = int(expected_row_counts.get("projects", 0))
        if project_rows < 1:
            raise BackupError(f"Restored database contains no projects: {project_rows}")
        _restore_runtime_privileges(postgres_container)
        runtime_role_evidence = _runtime_role_probe(postgres_container)

        docker("volume", "create", "--label", "custombuild.restore-drill=true", object_volume)
        object_image = str(object_store["image"])
        expected_object_image_id = str(object_store["image_id"])
        actual_object_image_id = docker(
            "image", "inspect", object_image, "--format", "{{.Id}}", capture=True
        )
        if actual_object_image_id != expected_object_image_id:
            raise BackupError("Restore SeaweedFS image ID does not match the backup manifest")
        docker(
            "run",
            "--name",
            extract_container,
            "--rm",
            "--user",
            "0:0",
            "--mount",
            f"type=bind,source={backup},target=/backup,readonly",
            "--mount",
            f"type=volume,source={object_volume},target=/restore",
            VOLUME_INIT_IMAGE,
            "tar",
            "-C",
            "/restore",
            "-xf",
            "/backup/artifacts.tar",
            timeout_seconds=DOCKER_PAYLOAD_TIMEOUT_SECONDS,
        )
        docker(
            "run",
            "--detach",
            "--name",
            seaweed_container,
            "--env",
            f"AWS_ACCESS_KEY_ID={RESTORE_ACCESS_KEY}",
            "--env",
            f"AWS_SECRET_ACCESS_KEY={RESTORE_SECRET_KEY}",
            "--env",
            f"S3_BUCKET={bucket}",
            "--mount",
            f"type=volume,source={object_volume},target=/data",
            "--publish",
            "127.0.0.1::8333",
            expected_object_image_id,
            "mini",
            "-dir=/data",
            "-master.telemetry=false",
        )
        endpoint = _published_s3_endpoint(seaweed_container)
        restored_inventory = wait_for_seaweed_inventory(endpoint, bucket)
        if restored_inventory != expected_inventory:
            raise BackupError("Restored S3 inventory does not match the backup manifest")

        result = {
            "schema_version": RESTORE_SCHEMA,
            "backup_created_at": manifest["created_at"],
            "git_revision": manifest["git_revision"],
            "source_manifest_sha256": manifest["source_manifest_sha256"],
            "database_snapshot": database_snapshot,
            "database_alembic_heads": restored_heads,
            "database_project_rows": project_rows,
            "database_exact_row_counts_verified": True,
            "database_tombstone_history": restored_tombstone_history,
            "database_tombstone_history_verified": True,
            "database_runtime_roles": runtime_role_evidence,
            "object_store_image": object_image,
            "object_store_image_id": expected_object_image_id,
            "object_store_bucket": bucket,
            "object_store_object_count": len(restored_inventory),
            "object_store_total_size_bytes": sum(
                int(item["size_bytes"]) for item in restored_inventory
            ),
            "object_store_hashes_verified": True,
            "object_store_metadata_verified": True,
            "tenant_rls_verified": True,
            "tenant_acceptance_required_before_traffic": True,
            "status": "PASS",
        }
    except BaseException as exc:
        operation_error = exc
    finally:
        for name in (seaweed_container, postgres_container, extract_container):
            _cleanup_resource("container", name, cleanup_errors)
        _cleanup_resource("volume", object_volume, cleanup_errors)

    if cleanup_errors:
        detail = "; ".join(cleanup_errors)
        if operation_error is not None:
            raise BackupError(
                f"Restore drill failed ({operation_error}); cleanup also failed: {detail}"
            ) from operation_error
        raise BackupError(f"Restore drill cleanup failed: {detail}")
    if operation_error is not None:
        raise operation_error
    if result is None:
        raise BackupError("Restore drill produced no result")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_restore_drill(args.backup)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BackupError as exc:
        print(f"Restore drill: FAIL - {exc}")
        raise SystemExit(1) from exc
