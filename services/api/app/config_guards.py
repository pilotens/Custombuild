from __future__ import annotations

import hashlib
import re
from datetime import datetime
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


class BuildIdentityValues(TypedDict):
    app_version: str
    vcs_ref: str
    build_date: str
    source_url: str
    source_manifest_sha256: str
    dependency_lock_sha256: str


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
    if (
        access_key != access_key.strip()
        or _normalise_secret(access_key) in INSECURE_S3_ACCESS_KEYS
        or not access_key
    ):
        raise ValueError("production object-storage access key must be replaced")
    _validate_production_secret(secret_key, label="object-storage secret")


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
    if app_version != app_version.strip() or not normalised_version or any(
        marker in normalised_version for marker in INSECURE_BUILD_IDENTITY_MARKERS
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
