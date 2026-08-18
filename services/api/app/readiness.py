from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal

import boto3
from botocore.config import Config
from redis import Redis
from sqlalchemy import text

from .config import Settings
from .db import get_readiness_engine

DependencyName = Literal["database", "redis", "object_storage", "rule_engine"]


@dataclass(frozen=True)
class DependencyFailure:
    name: DependencyName
    error_type: str


def check_database(settings: Settings) -> None:
    with get_readiness_engine().connect() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": f"{settings.readiness_timeout_seconds}s"},
            )
        connection.execute(text("SELECT 1"))


def check_redis(settings: Settings) -> None:
    timeout = float(settings.readiness_timeout_seconds)
    client: Redis = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )
    try:
        if client.ping() is not True:
            raise RuntimeError("Redis ping did not return success")
    finally:
        client.close()


def check_object_storage(settings: Settings) -> None:
    timeout = float(settings.readiness_timeout_seconds)
    client: Any = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=timeout,
            read_timeout=timeout,
            retries={"total_max_attempts": 1, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )
    try:
        # Probe only the configured application bucket. Requiring
        # ListAllMyBuckets would defeat least-privilege S3 policies.
        client.head_bucket(Bucket=settings.s3_bucket)
    finally:
        client.close()


def check_rule_engine(_settings: Settings) -> None:
    from .design_service import assert_rule_engine_available

    assert_rule_engine_available()


def probe_dependencies(settings: Settings) -> tuple[dict[str, str], list[DependencyFailure]]:
    checks: tuple[tuple[DependencyName, Callable[[Settings], None]], ...] = (
        ("database", check_database),
        ("redis", check_redis),
        ("object_storage", check_object_storage),
        ("rule_engine", check_rule_engine),
    )
    statuses: dict[str, str] = {}
    failures: list[DependencyFailure] = []
    with ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix="readiness") as executor:
        futures = [executor.submit(check, settings) for _, check in checks]
        for (name, _), future in zip(checks, futures, strict=True):
            try:
                future.result()
            except Exception as exc:
                statuses[name] = "unavailable"
                failures.append(DependencyFailure(name=name, error_type=type(exc).__name__))
            else:
                statuses[name] = "ok"
    return statuses, failures
