"""Create and verify a coordinated local Compose backup.

The command quiesces application writers, proves the storage recovery gate,
inventories every S3 object, records a PostgreSQL recovery point, stops
SeaweedFS cleanly and archives its quiescent volume. It only restarts writers
after storage and capacity are proven safe. Existing backups and source volumes
are never overwritten or deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import boto3
from botocore.config import Config

try:
    from scripts.source_manifest import build_source_manifest
except ModuleNotFoundError:  # Direct script execution.
    from source_manifest import build_source_manifest  # type: ignore[import-not-found,no-redef]

VOLUME_INIT_IMAGE = (
    "cgr.dev/chainguard/busybox:latest@sha256:"
    "928939fc7f20750dea03366627d83bfa497df565fcf6b55fdddb004ecd8426d6"
)
POSTGRES_IMAGE = (
    "cgr.dev/chainguard/postgres:latest@sha256:"
    "3af67abef0353ec61f054acf649abb5eaaae9742a9c1c9125e073c7833736060"
)
SEAWEEDFS_IMAGE = "custombuild-seaweedfs:uncommitted"
SEAWEEDFS_IMAGE_PATTERN = re.compile(
    r"^custombuild-seaweedfs:(?:uncommitted|[a-f0-9]{40}|[a-f0-9]{64})$"
)
MANIFEST_SCHEMA = "custombuild.compose-backup.v5"
TOMBSTONE_HISTORY_SCHEMA = "custombuild.storage-tombstone-history.v1"
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
WAL_LSN_PATTERN = re.compile(r"^[0-9A-F]+/[0-9A-F]+$")
S3_BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")

# PostgreSQL 18 provides the core sha256(bytea) function.  Hash a fixed-order
# JSON array rather than a JSON object so column names, locale and object-key
# ordering cannot silently change the anti-ABA history identity.  The timestamp
# is rendered in UTC with six fractional digits and every retired identity
# column is included.  Both backup and restore import this exact SQL fragment.
TOMBSTONE_HISTORY_SQL = """(
SELECT json_build_object(
  'schema_version', 'custombuild.storage-tombstone-history.v1',
  'count', count(*),
  'sha256', encode(
    sha256(
      convert_to(
        COALESCE(
          jsonb_agg(
            jsonb_build_array(
              tombstone.capacity_bucket,
              tombstone.object_key,
              tombstone.organization_id,
              tombstone.project_id,
              tombstone.sha256,
              tombstone.size_bytes,
              tombstone.media_type,
              tombstone.owner_type,
              tombstone.owner_id,
              tombstone.idempotency_key,
              tombstone.accounting_state,
              tombstone.claim_token,
              to_char(
                tombstone.retired_at AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
              )
            )
            ORDER BY tombstone.capacity_bucket COLLATE "C",
                     tombstone.object_key COLLATE "C"
          )::text,
          '[]'
        ),
        'UTF8'
      )
    ),
    'hex'
  )
)
FROM public.storage_object_tombstones AS tombstone
)"""

# Every external process has an explicit budget.  The short/configuration paths
# should fail quickly, while payload creation is allowed to handle large local
# volumes.  Recovery uses its own budget so a failed backup cannot wait forever
# while the application remains paused.
CONFIG_COMMAND_TIMEOUT_SECONDS = 30
SHORT_COMMAND_TIMEOUT_SECONDS = 120
LONG_BACKUP_COMMAND_TIMEOUT_SECONDS = 2 * 60 * 60
RECOVERY_TIMEOUT_SECONDS = 120
STORAGE_RECOVERY_TIMEOUT_SECONDS = 1200
STORAGE_RECOVERY_COMMAND_TIMEOUT_SECONDS = STORAGE_RECOVERY_TIMEOUT_SECONDS + 60
# Generation tasks have a two-hour hard limit. Give Celery a small additional
# Docker shutdown window so stop remains a warm, draining shutdown rather than
# killing an active reservation halfway through its commit protocol.
WORKER_DRAIN_GRACE_SECONDS = 2 * 60 * 60 + 120
WORKER_DRAIN_COMMAND_TIMEOUT_SECONDS = WORKER_DRAIN_GRACE_SECONDS + 60
FAIL_CLOSED_STOP_GRACE_SECONDS = 30
S3_READINESS_IO_TIMEOUT_SECONDS = 5.0
S3_READINESS_RETRY_INTERVAL_SECONDS = 1.0
CAPACITY_HEARTBEAT_PATH = "/run/custombuild-state/capacity-heartbeat.json"
CAPACITY_REFRESH_REQUEST_PATH = "/run/custombuild-state/capacity-refresh-request.json"


class BackupError(RuntimeError):
    """Raised when a coordinated backup cannot be created or verified."""


def executable(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise BackupError(f"Required executable is not available: {name}")
    return resolved


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_manifest_digest(repo: Path) -> str:
    return build_source_manifest(repo)[2]


def _prepare_private_output_directory(path: Path) -> None:
    """Create one backup directory that is owner-only on POSIX hosts."""

    try:
        if path.exists():
            if not path.is_dir() or any(path.iterdir()):
                raise BackupError(f"Refusing to overwrite non-empty backup directory: {path}")
        else:
            path.mkdir(parents=True, mode=0o700)
        if os.name == "posix":
            path.chmod(0o700)
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(
            "Could not create a private backup directory; verify ownership and permissions, "
            "then retry with a new empty directory"
        ) from exc


def _open_private_binary(path: Path) -> BinaryIO:
    """Create a new owner-only payload without a world-readable umask window."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "wb")
    except OSError as exc:
        with suppress(NameError, OSError):
            os.close(descriptor)
        raise BackupError(
            "Could not create a private backup payload; verify ownership and free space, "
            "then retry with a new empty directory"
        ) from exc


def _write_private_text(path: Path, payload: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            target.write(payload)
    except OSError as exc:
        with suppress(NameError, OSError):
            os.close(descriptor)
        raise BackupError(
            "Could not write the private backup manifest; verify ownership and free space, "
            "then retry with a new empty directory"
        ) from exc


def run(
    command: list[str],
    *,
    cwd: Path,
    stdout: BinaryIO | None = None,
    timeout_seconds: int = SHORT_COMMAND_TIMEOUT_SECONDS,
    operation: str = "Command",
) -> None:
    try:
        process = subprocess.run(  # noqa: S603 - argv-only and assembled internally.
            command,
            cwd=cwd,
            check=False,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        # Never include argv or TimeoutExpired output: Compose argv and provider
        # diagnostics may contain credentials resolved from the environment.
        raise BackupError(
            f"{operation} timed out after {timeout_seconds} seconds; "
            "inspect the service and retry in a new empty backup directory"
        ) from exc
    except OSError as exc:
        raise BackupError(
            f"{operation} could not be started; verify the local container runtime "
            "and retry in a new empty backup directory"
        ) from exc
    if process.returncode:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        raise BackupError(detail or f"Command failed: {' '.join(command)}")


def run_capture(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int = SHORT_COMMAND_TIMEOUT_SECONDS,
    operation: str = "Command",
) -> str:
    try:
        process = subprocess.run(  # noqa: S603 - argv-only and assembled internally.
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackupError(
            f"{operation} timed out after {timeout_seconds} seconds; "
            "inspect the service and retry in a new empty backup directory"
        ) from exc
    except OSError as exc:
        raise BackupError(
            f"{operation} could not be started; verify the local container runtime "
            "and retry in a new empty backup directory"
        ) from exc
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip()
        raise BackupError(detail or f"Command failed: {' '.join(command)}")
    return process.stdout.strip()


def compose_config(repo: Path, compose: Path) -> dict[str, Any]:
    raw = run_capture(
        [executable("docker"), "compose", "--file", str(compose), "config", "--format", "json"],
        cwd=repo,
        timeout_seconds=CONFIG_COMMAND_TIMEOUT_SECONDS,
        operation="Compose configuration",
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupError("Compose configuration is not valid JSON") from exc
    if not isinstance(value, dict):
        raise BackupError("Compose configuration is not a JSON object")
    return {str(key): item for key, item in value.items()}


def _s3_client(
    endpoint: str,
    access_key: str,
    secret_key: str,
    *,
    connect_timeout_seconds: float = 5.0,
    read_timeout_seconds: float = 60.0,
    total_max_attempts: int = 3,
) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=connect_timeout_seconds,
            read_timeout=read_timeout_seconds,
            retries={"total_max_attempts": total_max_attempts, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def inventory_s3(
    endpoint: str, access_key: str, secret_key: str, bucket: str
) -> list[dict[str, Any]]:
    """List and hash every current object in an S3 bucket."""
    if not all((endpoint, access_key, secret_key, bucket)):
        raise BackupError("Object-store endpoint, credentials and bucket must be configured")
    client = _s3_client(endpoint, access_key, secret_key)
    inventory: list[dict[str, Any]] = []
    continuation: str | None = None
    try:
        while True:
            parameters: dict[str, Any] = {"Bucket": bucket}
            if continuation:
                parameters["ContinuationToken"] = continuation
            response = client.list_objects_v2(**parameters)
            for listed in response.get("Contents", []):
                key = str(listed["Key"])
                expected_size = int(listed["Size"])
                digest = hashlib.sha256()
                actual_size = 0
                downloaded = client.get_object(Bucket=bucket, Key=key)
                with closing(downloaded["Body"]) as body:
                    for chunk in iter(lambda: body.read(1024 * 1024), b""):
                        digest.update(chunk)
                        actual_size += len(chunk)
                if actual_size != expected_size:
                    raise BackupError(
                        f"Object changed while inventorying {key!r}: "
                        f"listed {expected_size} bytes, read {actual_size}"
                    )
                content_type = downloaded.get("ContentType")
                metadata = downloaded.get("Metadata", {})
                if not isinstance(content_type, str) or not content_type:
                    raise BackupError(f"Object {key!r} has no immutable media type")
                if not isinstance(metadata, dict) or not all(
                    isinstance(name, str) and isinstance(value, str)
                    for name, value in metadata.items()
                ):
                    raise BackupError(f"Object {key!r} has invalid immutable metadata")
                inventory.append(
                    {
                        "key": key,
                        "size_bytes": actual_size,
                        "sha256": digest.hexdigest(),
                        "content_type": content_type,
                        "metadata": dict(sorted(metadata.items())),
                    }
                )
            if not response.get("IsTruncated"):
                break
            continuation = str(response.get("NextContinuationToken", ""))
            if not continuation:
                raise BackupError("S3 inventory was truncated without a continuation token")
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f"Could not inventory S3 bucket {bucket!r}: {exc}") from exc
    inventory.sort(key=lambda item: str(item["key"]))
    return inventory


def probe_s3_readiness(
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    *,
    io_timeout_seconds: float,
) -> None:
    """Confirm bucket access without downloading or hashing any object body."""

    if not all((endpoint, access_key, secret_key, bucket)):
        raise BackupError("Object-store endpoint, credentials and bucket must be configured")
    if io_timeout_seconds <= 0:
        raise BackupError("Object-storage readiness probe requires a positive timeout")
    client = _s3_client(
        endpoint,
        access_key,
        secret_key,
        connect_timeout_seconds=io_timeout_seconds,
        read_timeout_seconds=io_timeout_seconds,
        total_max_attempts=1,
    )
    try:
        client.list_objects_v2(Bucket=bucket, MaxKeys=1)
    except Exception as exc:
        raise BackupError("Object-storage readiness probe failed") from exc


def wait_for_s3_readiness(
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    *,
    timeout_seconds: int = 90,
) -> None:
    """Wait within one wall-clock budget, using one-attempt bounded S3 probes."""

    if timeout_seconds <= 0:
        raise BackupError("Object-storage readiness requires a positive timeout")
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # A request can spend time connecting and then reading. Giving each
        # phase at most half of the remaining budget prevents one probe from
        # consuming an unbounded recovery window; retries are disabled above.
        io_timeout = max(
            0.05,
            min(S3_READINESS_IO_TIMEOUT_SECONDS, remaining / 2),
        )
        try:
            probe_s3_readiness(
                endpoint,
                access_key,
                secret_key,
                bucket,
                io_timeout_seconds=io_timeout,
            )
            return
        except BackupError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(S3_READINESS_RETRY_INTERVAL_SECONDS, remaining))
    raise BackupError("Object storage did not become ready before the recovery deadline")


def database_snapshot(
    compose_prefix: list[str], repo: Path, database: str, user: str
) -> dict[str, Any]:
    query = (
        "SELECT json_build_object("  # noqa: S608 - fixed SQL constants only.
        "'captured_at', clock_timestamp(), "
        "'wal_lsn', pg_current_wal_lsn()::text, "
        "'alembic_heads', COALESCE((SELECT json_agg(version_num ORDER BY version_num) "
        "FROM alembic_version), '[]'::json), "
        "'row_counts', COALESCE((SELECT json_object_agg(tablename, row_count ORDER BY tablename) "
        "FROM (SELECT tablename, (((xpath('/row/count/text()', query_to_xml("
        "format('SELECT count(*) AS count FROM %I.%I', schemaname, tablename), "
        "false, true, ''))))[1]::text)::bigint AS row_count "
        "FROM pg_tables WHERE schemaname = 'public') AS counts), '{}'::json), "
        f"'tombstone_history', {TOMBSTONE_HISTORY_SQL})::text;"
    )
    raw = run_capture(
        [
            *compose_prefix,
            "exec",
            "-T",
            "postgres",
            "psql",
            "--username",
            user,
            "--dbname",
            database,
            "--tuples-only",
            "--no-align",
            "--command",
            query,
        ],
        cwd=repo,
        timeout_seconds=SHORT_COMMAND_TIMEOUT_SECONDS,
        operation="PostgreSQL recovery-point query",
    )
    try:
        snapshot = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupError("PostgreSQL recovery-point query returned invalid JSON") from exc
    if not isinstance(snapshot, dict):
        raise BackupError("PostgreSQL recovery-point query did not return an object")
    return {str(key): value for key, value in snapshot.items()}


def build_manifest(directory: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    files = []
    pairs = (
        ("database.dump", "POSTGRES_CUSTOM_DUMP"),
        ("artifacts.tar", "OBJECT_STORE_VOLUME"),
    )
    for name, role in pairs:
        path = directory / name
        if not path.is_file() or path.stat().st_size <= 0:
            raise BackupError(f"Backup payload is missing or empty: {name}")
        files.append(
            {
                "path": name,
                "role": role,
                "size_bytes": path.stat().st_size,
                "sha256": digest_file(path),
            }
        )
    return {
        "schema_version": MANIFEST_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        **metadata,
        "order": ["database.dump", "artifacts.tar"],
        "files": files,
    }


def _verify_database_snapshot(value: Any) -> None:
    if not isinstance(value, dict):
        raise BackupError("Backup manifest has no PostgreSQL recovery point")
    if not isinstance(value.get("captured_at"), str) or not value["captured_at"]:
        raise BackupError("Backup manifest has no PostgreSQL snapshot timestamp")
    try:
        captured_at = datetime.fromisoformat(value["captured_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError("Backup manifest has an invalid PostgreSQL snapshot timestamp") from exc
    if captured_at.tzinfo is None:
        raise BackupError("Backup manifest PostgreSQL snapshot timestamp has no timezone")
    if not isinstance(value.get("wal_lsn"), str) or not WAL_LSN_PATTERN.fullmatch(value["wal_lsn"]):
        raise BackupError("Backup manifest has an invalid PostgreSQL WAL LSN")
    heads = value.get("alembic_heads")
    if (
        not isinstance(heads, list)
        or not heads
        or not all(isinstance(head, str) and head for head in heads)
    ):
        raise BackupError("Backup manifest has invalid Alembic heads")
    if heads != sorted(set(heads)):
        raise BackupError("Backup manifest Alembic heads are not unique and sorted")
    row_counts = value.get("row_counts")
    if (
        not isinstance(row_counts, dict)
        or not row_counts
        or not all(
            isinstance(table, str)
            and table
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
            for table, count in row_counts.items()
        )
    ):
        raise BackupError("Backup manifest has invalid exact PostgreSQL row counts")
    tombstone_count = row_counts.get("storage_object_tombstones")
    if isinstance(tombstone_count, bool) or not isinstance(tombstone_count, int):
        raise BackupError("Backup manifest has no exact tombstone table row count")
    validate_tombstone_history(
        value.get("tombstone_history"),
        expected_count=tombstone_count,
    )


def validate_tombstone_history(value: Any, *, expected_count: int) -> None:
    """Validate the exact, non-secret proof of the append-only retired-key set."""

    if not isinstance(value, dict) or set(value) != {"schema_version", "count", "sha256"}:
        raise BackupError("Backup manifest has invalid tombstone history evidence")
    count = value.get("count")
    digest = value.get("sha256")
    if (
        value.get("schema_version") != TOMBSTONE_HISTORY_SCHEMA
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or count != expected_count
        or not isinstance(digest, str)
        or SHA256_PATTERN.fullmatch(digest) is None
    ):
        raise BackupError("Backup manifest has invalid tombstone history evidence")


def _verify_object_store(value: Any) -> None:
    if not isinstance(value, dict):
        raise BackupError("Backup manifest has no object-store evidence")
    image = value.get("image")
    if not isinstance(image, str) or not SEAWEEDFS_IMAGE_PATTERN.fullmatch(image):
        raise BackupError("Backup manifest does not bind an approved SeaweedFS build")
    image_id = value.get("image_id")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise BackupError("Backup manifest does not bind the exact SeaweedFS image ID")
    if not isinstance(value.get("bucket"), str) or not value["bucket"]:
        raise BackupError("Backup manifest has no object-store bucket")
    objects = value.get("objects")
    if not isinstance(objects, list):
        raise BackupError("Backup manifest has no S3 object inventory")
    seen: set[str] = set()
    total_size = 0
    for item in objects:
        if not isinstance(item, dict):
            raise BackupError("Backup manifest contains an invalid S3 object entry")
        key = item.get("key")
        size = item.get("size_bytes")
        digest = item.get("sha256")
        content_type = item.get("content_type")
        metadata = item.get("metadata")
        if not isinstance(key, str) or not key or key in seen:
            raise BackupError("Backup manifest contains an invalid or duplicate S3 key")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BackupError(f"Backup manifest contains an invalid size for {key!r}")
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise BackupError(f"Backup manifest contains an invalid SHA-256 for {key!r}")
        if not isinstance(content_type, str) or not content_type:
            raise BackupError(f"Backup manifest contains an invalid media type for {key!r}")
        if not isinstance(metadata, dict) or not all(
            isinstance(name, str) and name and isinstance(metadata_value, str)
            for name, metadata_value in metadata.items()
        ):
            raise BackupError(f"Backup manifest contains invalid metadata for {key!r}")
        if list(metadata) != sorted(metadata):
            raise BackupError(f"Backup manifest metadata is not canonical for {key!r}")
        seen.add(key)
        total_size += size
    if value.get("object_count") != len(objects) or value.get("total_size_bytes") != total_size:
        raise BackupError("Backup manifest object-store totals do not match its inventory")
    if objects != sorted(objects, key=lambda item: str(item["key"])):
        raise BackupError("Backup manifest S3 inventory is not sorted by key")


def verify_manifest(directory: Path) -> dict[str, Any]:
    directory = directory.resolve()
    manifest_path = directory / "manifest.json"
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError("Backup manifest is missing or invalid") from exc
    if not isinstance(raw_manifest, dict):
        raise BackupError("Backup manifest must be a JSON object")
    manifest = {str(key): item for key, item in raw_manifest.items()}
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise BackupError("Unsupported backup manifest schema")
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str):
        raise BackupError("Backup manifest has no creation timestamp")
    try:
        parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError("Backup manifest has an invalid creation timestamp") from exc
    if parsed_created_at.tzinfo is None:
        raise BackupError("Backup manifest creation timestamp has no timezone")
    if manifest.get("order") != ["database.dump", "artifacts.tar"]:
        raise BackupError("Backup capture order is invalid")
    entries = manifest.get("files")
    expected_roles = {
        "database.dump": "POSTGRES_CUSTOM_DUMP",
        "artifacts.tar": "OBJECT_STORE_VOLUME",
    }
    if not isinstance(entries, list) or len(entries) != len(expected_roles):
        raise BackupError("Backup manifest must contain exactly two payload files")
    seen_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupError("Backup manifest contains an invalid payload entry")
        name = entry.get("path")
        if not isinstance(name, str) or name not in expected_roles or name in seen_paths:
            raise BackupError("Backup manifest contains an unexpected payload path")
        if entry.get("role") != expected_roles[name]:
            raise BackupError(f"Backup payload role is invalid: {name}")
        seen_paths.add(name)
        path = directory / name
        if not path.is_file():
            raise BackupError(f"Backup payload is missing: {name}")
        valid_size = path.stat().st_size == entry.get("size_bytes")
        valid_digest = digest_file(path) == entry.get("sha256")
        if not valid_size or not valid_digest:
            raise BackupError(f"Backup checksum mismatch: {name}")
    if seen_paths != set(expected_roles):
        raise BackupError("Backup manifest is missing a required payload")
    _verify_database_snapshot(manifest.get("database_snapshot"))
    _verify_object_store(manifest.get("object_store"))
    revision = manifest.get("git_revision")
    source_manifest_sha256 = manifest.get("source_manifest_sha256")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise BackupError("Backup manifest has no exact Git revision")
    if not isinstance(source_manifest_sha256, str) or not SHA256_PATTERN.fullmatch(
        source_manifest_sha256
    ):
        raise BackupError("Backup manifest has no exact source manifest SHA-256")
    return manifest


def _compose_tcp_port(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 65_535 else None
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,4}", value) is None:
        return None
    port = int(value)
    return port if port <= 65_535 else None


def _is_canonical_s3_bucket(bucket: str) -> bool:
    """Mirror the production config guard without importing the API package."""

    try:
        ipv4_literal = ipaddress.ip_address(bucket).version == 4
    except ValueError:
        ipv4_literal = False
    return (
        S3_BUCKET_PATTERN.fullmatch(bucket) is not None and ".." not in bucket and not ipv4_literal
    )


def _resolved_storage(config: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    try:
        storage_service = config["services"]["object-storage"]
        storage_environment = storage_service["environment"]
        object_image = str(storage_service["image"])
        object_volume = str(config["volumes"]["object-storage-data"]["name"])
        endpoint = str(storage_environment["S3_BACKUP_ENDPOINT"])
        access_key = str(storage_environment["AWS_ACCESS_KEY_ID"])
        secret_key = str(storage_environment["AWS_SECRET_ACCESS_KEY"])
        bucket = str(storage_environment["S3_BUCKET"])
    except (KeyError, TypeError) as exc:
        raise BackupError("Compose configuration is missing object-storage settings") from exc
    if not _is_canonical_s3_bucket(bucket):
        raise BackupError("Compose object-storage bucket must be a canonical S3 DNS name")
    if not SEAWEEDFS_IMAGE_PATTERN.fullmatch(object_image):
        raise BackupError("Compose must use the repository-owned SeaweedFS build")
    try:
        parsed_endpoint = urlsplit(endpoint)
        endpoint_host = ipaddress.ip_address(parsed_endpoint.hostname or "")
        endpoint_port = parsed_endpoint.port
        endpoint_valid = (
            endpoint == endpoint.strip()
            and re.search(r"[\\\x00-\x20\x7f]", endpoint) is None
            and parsed_endpoint.scheme == "http"
            and endpoint_host.is_loopback
            and endpoint_port is not None
            and 1 <= endpoint_port <= 65_535
            and parsed_endpoint.username is None
            and parsed_endpoint.password is None
            and parsed_endpoint.path in {"", "/"}
            and not parsed_endpoint.query
            and not parsed_endpoint.fragment
        )
    except ValueError:
        endpoint_valid = False
        endpoint_host = ipaddress.ip_address("127.0.0.1")
        endpoint_port = None
    matching_port = False
    if endpoint_valid:
        for published in storage_service.get("ports", []) or []:
            if not isinstance(published, dict):
                continue
            raw_published = published.get("published")
            raw_target = published.get("target")
            published_port = _compose_tcp_port(raw_published)
            target_port = _compose_tcp_port(raw_target)
            if published_port is None or target_port is None:
                continue
            try:
                published_host = ipaddress.ip_address(str(published.get("host_ip", "")))
            except ValueError:
                continue
            if (
                published_host == endpoint_host
                and published_port == endpoint_port
                and target_port == 8333
                and str(published.get("protocol", "tcp")).lower() == "tcp"
            ):
                matching_port = True
                break
    if not matching_port:
        raise BackupError("Compose object-storage backup endpoint must match its loopback S3 port")
    return object_volume, object_image, endpoint, access_key, secret_key, bucket


def refresh_capacity_attestor(
    compose_prefix: list[str],
    repo: Path,
    *,
    resume: bool,
) -> None:
    """Require a heartbeat bound to new exact PostgreSQL evidence."""

    service = "storage-capacity-attestor"
    if resume:
        run(
            [*compose_prefix, "unpause", service],
            cwd=repo,
            timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
            operation="Unpause storage capacity attestor",
        )
    run(
        [
            *compose_prefix,
            "exec",
            "-T",
            service,
            "python",
            "-m",
            "scripts.storage_capacity_refresh",
            "prepare",
            "--heartbeat",
            CAPACITY_HEARTBEAT_PATH,
            "--request",
            CAPACITY_REFRESH_REQUEST_PATH,
        ],
        cwd=repo,
        timeout_seconds=SHORT_COMMAND_TIMEOUT_SECONDS,
        operation="Record storage capacity refresh baseline",
    )
    run(
        [*compose_prefix, "kill", "--signal", "SIGUSR1", service],
        cwd=repo,
        timeout_seconds=SHORT_COMMAND_TIMEOUT_SECONDS,
        operation="Request fresh storage capacity evidence",
    )
    run(
        [
            *compose_prefix,
            "exec",
            "-T",
            service,
            "python",
            "-m",
            "scripts.storage_capacity_refresh",
            "wait",
            "--heartbeat",
            CAPACITY_HEARTBEAT_PATH,
            "--request",
            CAPACITY_REFRESH_REQUEST_PATH,
            "--timeout-seconds",
            "110",
        ],
        cwd=repo,
        timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
        operation="Wait for fresh storage capacity evidence",
    )


def run_storage_recovery(compose_prefix: list[str], repo: Path) -> None:
    """Run the one-shot maintenance gate and require an unambiguous success."""

    run(
        [
            *compose_prefix,
            "run",
            "--rm",
            "--no-deps",
            "-e",
            "STORAGE_RECOVERY_TIMEOUT_SECONDS=" + str(STORAGE_RECOVERY_TIMEOUT_SECONDS),
            "storage-recovery",
        ],
        cwd=repo,
        timeout_seconds=STORAGE_RECOVERY_COMMAND_TIMEOUT_SECONDS,
        operation="Storage recovery gate",
    )


def _stop_worker(compose_prefix: list[str], repo: Path, *, operation: str) -> None:
    """Warm-stop Celery with enough time for its longest active task to drain."""

    run(
        [
            *compose_prefix,
            "stop",
            "--timeout",
            str(WORKER_DRAIN_GRACE_SECONDS),
            "worker",
        ],
        cwd=repo,
        timeout_seconds=WORKER_DRAIN_COMMAND_TIMEOUT_SECONDS,
        operation=operation,
    )


def _fail_closed_service(
    compose_prefix: list[str],
    repo: Path,
    service: str,
) -> None:
    """Best-effort bounded stop used only after a quiescence command failed."""

    run(
        [
            *compose_prefix,
            "stop",
            "--timeout",
            str(FAIL_CLOSED_STOP_GRACE_SECONDS),
            service,
        ],
        cwd=repo,
        timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
        operation=f"Fail-closed stop {service}",
    )


def create_backup(repo: Path, compose: Path, output: Path) -> dict[str, Any]:
    repo = repo.resolve()
    compose = compose.resolve()
    output = output.resolve()
    _prepare_private_output_directory(output)

    config = compose_config(repo, compose)
    services = config.get("services", {})
    required_services = {
        "api",
        "worker",
        "scheduler",
        "storage-recovery",
        "storage-capacity-attestor",
    }
    if not isinstance(services, dict) or not required_services.issubset(services):
        configured_services = set(services) if isinstance(services, dict) else set()
        missing = sorted(required_services - configured_services)
        raise BackupError(
            "Compose configuration is missing required backup services: " + ", ".join(missing)
        )
    project = str(config.get("name", "unknown"))
    postgres_environment = config["services"]["postgres"].get("environment", {})
    database = str(postgres_environment.get("POSTGRES_DB", "custombuild"))
    user = str(postgres_environment.get("POSTGRES_USER", "custombuild_bootstrap"))
    storage = _resolved_storage(config)
    object_volume, object_image, endpoint, access_key, secret_key, bucket = storage
    docker = executable("docker")
    compose_prefix = [docker, "compose", "--file", str(compose)]

    # Record each transition before invoking Docker. A client-side timeout does
    # not prove what the daemon applied, so a failed gate never permits writers
    # to resume and an attempted resume is rolled back on any later failure.
    pause_attempted_services: list[str] = []
    worker_stop_attempted = False
    attestor_pause_attempted = False
    storage_recovery_completed = False
    pre_capture_capacity_verified = False
    capture_started = False
    gate_failed = False
    object_stopped = False
    inventory: list[dict[str, Any]] | None = None
    snapshot: dict[str, Any] | None = None
    operation_error: BaseException | None = None
    try:
        quiescence_errors: list[str] = []
        for service in ("api", "scheduler"):
            pause_attempted_services.append(service)
            try:
                run(
                    [*compose_prefix, "pause", service],
                    cwd=repo,
                    timeout_seconds=SHORT_COMMAND_TIMEOUT_SECONDS,
                    operation=f"Pause {service}",
                )
            except BackupError as exc:
                quiescence_errors.append(f"{service} pause failed: {exc}")
                try:
                    _fail_closed_service(compose_prefix, repo, service)
                except BackupError as stop_exc:
                    quiescence_errors.append(f"{service} fail-closed stop failed: {stop_exc}")
        worker_stop_attempted = True
        try:
            _stop_worker(compose_prefix, repo, operation="Drain and stop worker")
        except BackupError as exc:
            quiescence_errors.append(f"worker drain failed: {exc}")
            try:
                run(
                    [*compose_prefix, "kill", "--signal", "SIGKILL", "worker"],
                    cwd=repo,
                    timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
                    operation="Fail-closed kill worker",
                )
            except BackupError as kill_exc:
                quiescence_errors.append(f"worker fail-closed kill failed: {kill_exc}")
        if quiescence_errors:
            raise BackupError("Writer quiescence failed: " + "; ".join(quiescence_errors))

        run_storage_recovery(compose_prefix, repo)
        storage_recovery_completed = True
        refresh_capacity_attestor(compose_prefix, repo, resume=False)
        pre_capture_capacity_verified = True
        attestor_pause_attempted = True
        run(
            [*compose_prefix, "pause", "storage-capacity-attestor"],
            cwd=repo,
            timeout_seconds=SHORT_COMMAND_TIMEOUT_SECONDS,
            operation="Pause storage capacity attestor",
        )
        capture_started = True
        inventory = inventory_s3(endpoint, access_key, secret_key, bucket)
        snapshot = database_snapshot(compose_prefix, repo, database, user)
        with _open_private_binary(output / "database.dump.partial") as target:
            run(
                [
                    *compose_prefix,
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "--username",
                    user,
                    "--dbname",
                    database,
                    "--format=custom",
                    "--no-owner",
                ],
                cwd=repo,
                stdout=target,
                timeout_seconds=LONG_BACKUP_COMMAND_TIMEOUT_SECONDS,
                operation="PostgreSQL dump",
            )
        (output / "database.dump.partial").replace(output / "database.dump")

        object_stopped = True
        run(
            [*compose_prefix, "stop", "object-storage"],
            cwd=repo,
            timeout_seconds=SHORT_COMMAND_TIMEOUT_SECONDS,
            operation="Stop object storage",
        )
        with _open_private_binary(output / "artifacts.tar.partial") as target:
            run(
                [
                    docker,
                    "run",
                    "--rm",
                    "--user",
                    "0:0",
                    "--mount",
                    f"type=volume,source={object_volume},target=/source,readonly",
                    VOLUME_INIT_IMAGE,
                    "tar",
                    "-C",
                    "/source",
                    "-cf",
                    "-",
                    ".",
                ],
                cwd=repo,
                stdout=target,
                timeout_seconds=LONG_BACKUP_COMMAND_TIMEOUT_SECONDS,
                operation="Object-storage archive",
            )
        (output / "artifacts.tar.partial").replace(output / "artifacts.tar")

        run(
            [*compose_prefix, "start", "object-storage"],
            cwd=repo,
            timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
            operation="Start object storage",
        )
        wait_for_s3_readiness(
            endpoint,
            access_key,
            secret_key,
            bucket,
            timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
        )
        restarted_inventory = inventory_s3(endpoint, access_key, secret_key, bucket)
        object_stopped = False
        if restarted_inventory != inventory:
            raise BackupError("S3 inventory changed while the backup was quiesced")
    except BaseException as exc:
        operation_error = exc
        if not capture_started:
            gate_failed = True
    finally:
        recovery_errors: list[str] = []
        storage_available = not object_stopped
        if object_stopped:
            try:
                run(
                    [*compose_prefix, "start", "object-storage"],
                    cwd=repo,
                    timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
                    operation="Recover object storage",
                )
            except BackupError as exc:
                recovery_errors.append(f"object-storage restart failed: {exc}")
            # A timed-out Docker client does not prove that the daemon failed to
            # start the container.  Always perform the bounded readiness check;
            # writers may resume only when the bucket answers a fresh request.
            try:
                wait_for_s3_readiness(
                    endpoint,
                    access_key,
                    secret_key,
                    bucket,
                    timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
                )
                storage_available = True
            except BackupError as exc:
                recovery_errors.append(f"object-storage readiness failed: {exc}")
        capacity_available = False
        if storage_available and storage_recovery_completed:
            try:
                refresh_capacity_attestor(
                    compose_prefix,
                    repo,
                    resume=attestor_pause_attempted,
                )
                capacity_available = True
            except BackupError as exc:
                recovery_errors.append(f"storage capacity refresh failed: {exc}")
        can_resume_writers = (
            storage_available
            and storage_recovery_completed
            and pre_capture_capacity_verified
            and capacity_available
            and not gate_failed
            and not recovery_errors
        )
        if can_resume_writers:
            resumed_services: list[str] = []
            worker_start_attempted = False
            try:
                worker_start_attempted = worker_stop_attempted
                if worker_stop_attempted:
                    run(
                        [*compose_prefix, "start", "worker"],
                        cwd=repo,
                        timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
                        operation="Start worker",
                    )
                for service in ("scheduler", "api"):
                    if service not in pause_attempted_services:
                        continue
                    resumed_services.append(service)
                    run(
                        [*compose_prefix, "unpause", service],
                        cwd=repo,
                        timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
                        operation=f"Unpause {service}",
                    )
            except BackupError as exc:
                recovery_errors.append(f"writer restart failed: {exc}")
                for service in reversed(resumed_services):
                    try:
                        run(
                            [*compose_prefix, "pause", service],
                            cwd=repo,
                            timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
                            operation=f"Re-pause {service} after recovery failure",
                        )
                    except BackupError as pause_exc:
                        recovery_errors.append(
                            f"{service} fail-closed re-pause failed: {pause_exc}"
                        )
                if worker_start_attempted:
                    try:
                        _stop_worker(
                            compose_prefix,
                            repo,
                            operation="Fail-closed stop worker after recovery failure",
                        )
                    except BackupError as stop_exc:
                        recovery_errors.append(f"worker fail-closed stop failed: {stop_exc}")
        else:
            if not storage_recovery_completed:
                recovery_errors.append(
                    "application writers remain stopped or paused because storage "
                    "recovery did not complete"
                )
            elif gate_failed or not pre_capture_capacity_verified:
                recovery_errors.append(
                    "application writers remain stopped or paused because the "
                    "pre-capture storage gate failed"
                )
            elif not storage_available:
                recovery_errors.append(
                    "application writers remain stopped or paused because object "
                    "storage is unavailable"
                )
            elif not capacity_available:
                recovery_errors.append(
                    "application writers remain stopped or paused because storage "
                    "capacity was not freshly attested"
                )
            else:
                recovery_errors.append(
                    "application writers remain stopped or paused because recovery "
                    "reported an error"
                )
        if recovery_errors and can_resume_writers:
            recovery_errors.append("application writers were returned to a stopped or paused state")
        if recovery_errors:
            detail = "; ".join(recovery_errors)
            if operation_error is not None:
                raise BackupError(
                    f"Backup failed ({operation_error}); recovery also failed: {detail}"
                ) from operation_error
            raise BackupError(f"Backup recovery failed: {detail}")
    if operation_error is not None:
        raise operation_error
    if inventory is None or snapshot is None:
        raise BackupError("Backup did not capture required recovery evidence")

    revision = run_capture(
        [executable("git"), "rev-parse", "HEAD"],
        cwd=repo,
        timeout_seconds=SHORT_COMMAND_TIMEOUT_SECONDS,
        operation="Git revision lookup",
    )
    object_image_id = run_capture(
        [docker, "image", "inspect", object_image, "--format", "{{.Id}}"],
        cwd=repo,
        timeout_seconds=SHORT_COMMAND_TIMEOUT_SECONDS,
        operation="SeaweedFS image identity lookup",
    )
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", object_image_id):
        raise BackupError("SeaweedFS image identity lookup returned an invalid image ID")
    source_manifest_sha256 = source_manifest_digest(repo)
    manifest = build_manifest(
        output,
        {
            "compose_project": project,
            "git_revision": revision,
            "source_manifest_sha256": source_manifest_sha256,
            "database": database,
            "database_snapshot": snapshot,
            "object_volume": object_volume,
            "object_store": {
                "image": object_image,
                "image_id": object_image_id,
                "bucket": bucket,
                "object_count": len(inventory),
                "total_size_bytes": sum(int(item["size_bytes"]) for item in inventory),
                "objects": inventory,
            },
        },
    )
    _write_private_text(
        output / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return verify_manifest(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, default=Path("compose.yml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    if args.verify_only:
        manifest = verify_manifest(args.output)
    else:
        manifest = create_backup(Path.cwd(), args.compose, args.output)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("Compose backup verification: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BackupError as exc:
        print(f"Compose backup verification: FAIL - {exc}")
        raise SystemExit(1) from exc
