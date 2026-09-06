from __future__ import annotations

import hashlib
import os
import re
import stat
from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from typing import TypedDict
from urllib.parse import unquote, urlparse

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

INSECURE_SECRET_MARKERS = ("change-me", "development")
INSECURE_SECRET_VALUES = frozenset({"custombuild", "minioadmin", "password", "postgres"})
INSECURE_S3_ACCESS_KEYS = frozenset({"custombuild", "minioadmin"})
INSECURE_BUILD_IDENTITY_MARKERS = ("dirty", "local", "unknown", "uncommitted")
MIN_PRODUCTION_SECRET_LENGTH = 24
MAX_PRODUCTION_CAM_PROFILE_BYTES = 1024 * 1024
RAW_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
S3_ACCESS_KEY_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
S3_BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class BuildIdentityValues(TypedDict):
    app_version: str
    vcs_ref: str
    build_date: str
    source_url: str
    source_manifest_sha256: str
    dependency_lock_sha256: str


def read_production_cam_profile_source(
    *,
    profile_path: str,
    profile_json: str,
    profile_sha256: str,
    production: bool,
) -> bytes | str:
    """Read one bounded server-owned CAM profile without following a leaf symlink.

    Inline JSON remains useful for isolated development tests, but production
    must use an immutable read-only file mount.  API and worker call this same
    helper so their input and failure semantics cannot silently diverge.
    """

    if profile_path and profile_json:
        raise ValueError(
            "PRODUCTION_CAM_PROFILE_PATH and PRODUCTION_CAM_PROFILE_JSON are mutually exclusive"
        )
    if production and profile_json:
        raise ValueError("production CAM profile must use PRODUCTION_CAM_PROFILE_PATH")
    if profile_sha256 and SHA256_PATTERN.fullmatch(profile_sha256) is None:
        raise ValueError("PRODUCTION_CAM_PROFILE_SHA256 must be lowercase 64-character hex")
    if production and profile_path and not profile_sha256:
        raise ValueError(
            "production CAM profile path requires PRODUCTION_CAM_PROFILE_SHA256"
        )
    if not profile_path:
        if profile_sha256:
            if not profile_json:
                raise ValueError(
                    "PRODUCTION_CAM_PROFILE_SHA256 requires a configured CAM profile"
                )
            if hashlib.sha256(profile_json.encode("utf-8")).hexdigest() != profile_sha256:
                raise ValueError("production CAM profile SHA-256 does not match configured bytes")
        return profile_json
    if (
        profile_path != profile_path.strip()
        or RAW_CONTROL_PATTERN.search(profile_path) is not None
        or not Path(profile_path).is_absolute()
        or str(Path(profile_path)) != profile_path
    ):
        raise ValueError("PRODUCTION_CAM_PROFILE_PATH must be a canonical absolute path")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(profile_path, flags)
    except OSError as exc:
        raise ValueError("production CAM profile file is unavailable") from exc
    try:
        status_before = os.fstat(descriptor)
        if not stat.S_ISREG(status_before.st_mode):
            raise ValueError("production CAM profile path must identify a regular file")
        if production and status_before.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError(
                "production CAM profile file must not be group- or world-writable"
            )
        if not 0 < status_before.st_size <= MAX_PRODUCTION_CAM_PROFILE_BYTES:
            raise ValueError("production CAM profile file size is invalid")
        chunks: list[bytes] = []
        remaining = MAX_PRODUCTION_CAM_PROFILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        status_after = os.fstat(descriptor)
        identity_before = (
            status_before.st_dev,
            status_before.st_ino,
            status_before.st_mode,
            status_before.st_size,
            status_before.st_mtime_ns,
            status_before.st_ctime_ns,
        )
        identity_after = (
            status_after.st_dev,
            status_after.st_ino,
            status_after.st_mode,
            status_after.st_size,
            status_after.st_mtime_ns,
            status_after.st_ctime_ns,
        )
        if (
            identity_after != identity_before
            or len(payload) != status_before.st_size
            or len(payload) > MAX_PRODUCTION_CAM_PROFILE_BYTES
        ):
            raise ValueError("production CAM profile file changed while it was read")
        if profile_sha256 and hashlib.sha256(payload).hexdigest() != profile_sha256:
            raise ValueError("production CAM profile SHA-256 does not match configured bytes")
        return payload
    finally:
        os.close(descriptor)


def _normalise_secret(value: str) -> str:
    return value.strip().lower()


def is_insecure_secret(value: str) -> bool:
    normalised = _normalise_secret(value)
    return (
        not normalised
        or normalised in INSECURE_SECRET_VALUES
        or any(marker in normalised for marker in INSECURE_SECRET_MARKERS)
    )


def _validate_production_secret(value: str, *, label: str) -> None:
    if value != value.strip():
        raise ValueError(f"production {label} must not have surrounding whitespace")
    if RAW_CONTROL_PATTERN.search(value) is not None:
        raise ValueError(f"production {label} must not contain control characters")
    if len(value) < MIN_PRODUCTION_SECRET_LENGTH:
        raise ValueError(
            f"production {label} must be at least {MIN_PRODUCTION_SECRET_LENGTH} characters"
        )
    if is_insecure_secret(value):
        raise ValueError(f"production {label} must be replaced")


def validate_production_database_url(
    database_url: str,
    *,
    expected_username: str,
    setting_name: str = "DATABASE_URL",
) -> None:
    try:
        database = make_url(database_url)
    except ArgumentError as exc:
        raise ValueError(f"production {setting_name} is invalid") from exc

    if (
        database.get_backend_name() != "postgresql"
        or not database.username
        or not database.password
    ):
        raise ValueError(f"production requires password-authenticated PostgreSQL in {setting_name}")
    if database.username != expected_username:
        raise ValueError(
            f"production {setting_name} must use the exact database role {expected_username}"
        )
    _validate_production_secret(
        database.password,
        label=f"database password in {setting_name}",
    )


def validate_production_s3_credentials(access_key: str, secret_key: str) -> None:
    if access_key != access_key.strip():
        raise ValueError(
            "production object-storage access key must not have surrounding whitespace"
        )
    if RAW_CONTROL_PATTERN.search(access_key) is not None:
        raise ValueError("production object-storage access key must not contain control characters")
    if (
        S3_ACCESS_KEY_PATTERN.fullmatch(access_key) is None
        or _normalise_secret(access_key) in INSECURE_S3_ACCESS_KEYS
    ):
        raise ValueError("production object-storage access key must be replaced")
    _validate_production_secret(secret_key, label="object-storage secret")


def validate_production_s3_bucket(bucket: str) -> None:
    if bucket != bucket.strip():
        raise ValueError("production object-storage bucket must not have surrounding whitespace")
    if RAW_CONTROL_PATTERN.search(bucket) is not None:
        raise ValueError("production object-storage bucket must not contain control characters")
    try:
        ipv4_literal = ip_address(bucket).version == 4
    except ValueError:
        ipv4_literal = False
    if S3_BUCKET_PATTERN.fullmatch(bucket) is None or ".." in bucket or ipv4_literal:
        raise ValueError("production object-storage bucket must be a canonical S3 DNS name")


def validate_production_redis_url(redis_url: str) -> None:
    try:
        parsed = urlparse(redis_url)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("production REDIS_URL is invalid") from exc

    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
        raise ValueError("production REDIS_URL must identify a Redis service")
    password = unquote(parsed.password or "")
    _validate_production_secret(password, label="Redis password in REDIS_URL")


def validate_production_build_identity(
    *,
    app_version: str,
    vcs_ref: str,
    build_date: str,
    source_url: str,
    source_manifest_sha256: str,
    dependency_lock_sha256: str,
    dependency_lock_path: Path = Path("uv.lock"),
) -> None:
    """Reject mutable or ambiguous application builds in production."""

    normalised_version = app_version.strip().lower()
    if (
        app_version != app_version.strip()
        or not normalised_version
        or any(marker in normalised_version for marker in INSECURE_BUILD_IDENTITY_MARKERS)
    ):
        raise ValueError("production APP_VERSION must identify an immutable release")
    if re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", vcs_ref) is None:
        raise ValueError("production VCS_REF must be an exact 40- or 64-character hex revision")
    try:
        parsed_build_date = datetime.fromisoformat(build_date.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("production BUILD_DATE must be a timezone-aware timestamp") from exc
    if parsed_build_date.tzinfo is None or parsed_build_date.utcoffset() is None:
        raise ValueError("production BUILD_DATE must be a timezone-aware timestamp")
    parsed_source = urlparse(source_url)
    if (
        source_url != source_url.strip()
        or parsed_source.scheme != "https"
        or not parsed_source.hostname
        or parsed_source.username is not None
        or parsed_source.password is not None
        or parsed_source.params
        or parsed_source.query
        or parsed_source.fragment
    ):
        raise ValueError("production SOURCE_URL must be a canonical HTTPS URL")
    if re.fullmatch(r"[a-f0-9]{64}", source_manifest_sha256) is None:
        raise ValueError(
            "production SOURCE_MANIFEST_SHA256 must identify the exact Docker build context"
        )
    if re.fullmatch(r"[a-f0-9]{64}", dependency_lock_sha256) is None:
        raise ValueError("production DEPENDENCY_LOCK_SHA256 must be the uv.lock SHA-256")
    try:
        actual_lock_sha256 = hashlib.sha256(dependency_lock_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("production uv.lock must be available for identity verification") from exc
    if dependency_lock_sha256 != actual_lock_sha256:
        raise ValueError("production DEPENDENCY_LOCK_SHA256 does not match uv.lock")
