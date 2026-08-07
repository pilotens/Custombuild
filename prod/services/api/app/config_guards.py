from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

INSECURE_SECRET_MARKERS = ("change-me", "development")
INSECURE_SECRET_VALUES = frozenset({"custombuild", "minioadmin", "password", "postgres"})
INSECURE_S3_ACCESS_KEYS = frozenset({"custombuild", "minioadmin"})


def _normalise_secret(value: str) -> str:
    return value.strip().lower()


def is_insecure_secret(value: str) -> bool:
    normalised = _normalise_secret(value)
    return (
        not normalised
        or normalised in INSECURE_SECRET_VALUES
        or any(marker in normalised for marker in INSECURE_SECRET_MARKERS)
    )


def validate_production_database_url(
    database_url: str,
    *,
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
    if is_insecure_secret(database.password):
        raise ValueError(f"production database password in {setting_name} must be replaced")


def validate_production_s3_credentials(access_key: str, secret_key: str) -> None:
    if _normalise_secret(access_key) in INSECURE_S3_ACCESS_KEYS or not access_key.strip():
        raise ValueError("production object-storage access key must be replaced")
    if is_insecure_secret(secret_key):
        raise ValueError("production object-storage secret must be replaced")
