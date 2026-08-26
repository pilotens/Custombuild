"""Create and verify a coordinated local Compose backup.

The command pauses application writers, inventories every S3 object, records a
PostgreSQL recovery point, stops SeaweedFS cleanly and archives its quiescent
volume. It always attempts to restart storage and application writers. Existing
backups and source volumes are never overwritten or deleted.
"""

from __future__ import annotations

import argparse
import hashlib
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
MANIFEST_SCHEMA = "custombuild.compose-backup.v4"
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
WAL_LSN_PATTERN = re.compile(r"^[0-9A-F]+/[0-9A-F]+$")

# Every external process has an explicit budget.  The short/configuration paths
# should fail quickly, while payload creation is allowed to handle large local
# volumes.  Recovery uses its own budget so a failed backup cannot wait forever
# while the application remains paused.
CONFIG_COMMAND_TIMEOUT_SECONDS = 30
SHORT_COMMAND_TIMEOUT_SECONDS = 120
LONG_BACKUP_COMMAND_TIMEOUT_SECONDS = 2 * 60 * 60
RECOVERY_TIMEOUT_SECONDS = 120
S3_READINESS_IO_TIMEOUT_SECONDS = 5.0
S3_READINESS_RETRY_INTERVAL_SECONDS = 1.0


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
        "SELECT json_build_object("
        "'captured_at', clock_timestamp(), "
        "'wal_lsn', pg_current_wal_lsn()::text, "
        "'alembic_heads', COALESCE((SELECT json_agg(version_num ORDER BY version_num) "
        "FROM alembic_version), '[]'::json), "
        "'row_counts', COALESCE((SELECT json_object_agg(tablename, row_count ORDER BY tablename) "
        "FROM (SELECT tablename, (((xpath('/row/count/text()', query_to_xml("
        "format('SELECT count(*) AS count FROM %I.%I', schemaname, tablename), "
        "false, true, ''))))[1]::text)::bigint AS row_count "
        "FROM pg_tables WHERE schemaname = 'public') AS counts), '{}'::json))::text;"
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


def _resolved_storage(config: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    try:
        api_environment = config["services"]["api"]["environment"]
        object_image = str(config["services"]["object-storage"]["image"])
        object_volume = str(config["volumes"]["object-storage-data"]["name"])
        endpoint = str(api_environment["S3_PUBLIC_ENDPOINT"])
        access_key = str(api_environment["S3_ACCESS_KEY"])
        secret_key = str(api_environment["S3_SECRET_KEY"])
        bucket = str(api_environment["S3_BUCKET"])
    except (KeyError, TypeError) as exc:
        raise BackupError("Compose configuration is missing object-storage settings") from exc
    if not SEAWEEDFS_IMAGE_PATTERN.fullmatch(object_image):
        raise BackupError("Compose must use the repository-owned SeaweedFS build")
    return object_volume, object_image, endpoint, access_key, secret_key, bucket


def create_backup(repo: Path, compose: Path, output: Path) -> dict[str, Any]:
    repo = repo.resolve()
    compose = compose.resolve()
    output = output.resolve()
    _prepare_private_output_directory(output)

    config = compose_config(repo, compose)
    project = str(config.get("name", "unknown"))
    postgres_environment = config["services"]["postgres"].get("environment", {})
    database = str(postgres_environment.get("POSTGRES_DB", "custombuild"))
    user = str(postgres_environment.get("POSTGRES_USER", "custombuild_bootstrap"))
    storage = _resolved_storage(config)
    object_volume, object_image, endpoint, access_key, secret_key, bucket = storage
    docker = executable("docker")
    compose_prefix = [docker, "compose", "--file", str(compose)]

    # Record the service before invoking Docker.  A client-side timeout does
    # not prove the daemon failed to apply the pause, so every attempted pause
    # receives one bounded unpause attempt in ``finally``.
    pause_attempted_services: list[str] = []
    object_stopped = False
    inventory: list[dict[str, Any]] | None = None
    snapshot: dict[str, Any] | None = None
    operation_error: BaseException | None = None
    try:
        for service in ("api", "worker", "scheduler"):
            if service not in config.get("services", {}):
                continue
            pause_attempted_services.append(service)
            run(
                [*compose_prefix, "pause", service],
                cwd=repo,
                timeout_seconds=SHORT_COMMAND_TIMEOUT_SECONDS,
                operation=f"Pause {service}",
            )
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
        if storage_available:
            for service in reversed(pause_attempted_services):
                try:
                    run(
                        [*compose_prefix, "unpause", service],
                        cwd=repo,
                        timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
                        operation=f"Unpause {service}",
                    )
                except BackupError as exc:
                    recovery_errors.append(f"{service} unpause failed: {exc}")
        elif pause_attempted_services:
            recovery_errors.append(
                "application writers remain paused because object storage is unavailable"
            )
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
