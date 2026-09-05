"""Worker-owned production readiness for joint-retention trust."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from app.joint_retention_registry import (
    assert_joint_retention_registry_activated,
    parse_joint_retention_registry_json,
)
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from .config import WorkerSettings, get_worker_settings

GENERATION_QUEUE = "generation"


@contextmanager
def _registry_connection(settings: WorkerSettings) -> Iterator[Connection]:
    timeout_seconds = min(settings.database_statement_timeout_seconds, 5)
    lock_timeout_milliseconds = min(
        settings.database_lock_timeout_seconds,
        max(timeout_seconds - 1, 1),
    ) * 1000
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=float(timeout_seconds),
        connect_args={
            "connect_timeout": timeout_seconds,
            "options": (
                f"-c statement_timeout={timeout_seconds * 1000} "
                f"-c lock_timeout={lock_timeout_milliseconds}"
            ),
        },
    )
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def require_generation_registry_activation(
    *,
    expected_queue: str,
    settings: WorkerSettings | None = None,
) -> None:
    """Fail unless a production generation worker matches the DB high-water state."""

    if expected_queue != GENERATION_QUEUE:
        return
    runtime = get_worker_settings() if settings is None else settings
    if runtime.app_env != "production":
        return
    registry = parse_joint_retention_registry_json(
        runtime.joint_retention_trust_registry_json
    )
    with _registry_connection(runtime) as connection:
        assert_joint_retention_registry_activated(
            connection,
            registry,
            production=True,
        )
