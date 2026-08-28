from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from app import db as db_module
from app import readiness
from app.config import Settings
from sqlalchemy import Engine, create_engine


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "readiness_timeout_seconds": 3,
        "redis_url": "redis://:unit-test-secret@redis:6379/0",
        "s3_endpoint": "http://object-storage:8333",
        "s3_access_key": "unit-test-access",
        "s3_secret_key": "unit-test-object-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


class FakeConnection:
    def __init__(self, dialect: str) -> None:
        self.dialect = SimpleNamespace(name=dialect)
        self.executions: list[tuple[str, object | None]] = []

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self, statement: object, parameters: object | None = None
    ) -> SimpleNamespace | None:
        self.executions.append((str(statement), parameters))
        if "FROM alembic_version" in str(statement):
            return SimpleNamespace(
                scalars=lambda: (readiness.REQUIRED_DATABASE_REVISION,)
            )
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> FakeConnection:
        return self.connection


def test_postgres_readiness_engine_has_bounded_pool_and_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings(
        database_url=(
            "postgresql+psycopg://custombuild_api:unit-test-password@postgres/custombuild"
        )
    )
    created: list[tuple[str, dict[str, object]]] = []
    fake_engine = create_engine("sqlite+pysqlite:///:memory:")

    def create(url: str, **kwargs: object) -> Engine:
        created.append((url, kwargs))
        return fake_engine

    db_module.get_readiness_engine.cache_clear()
    monkeypatch.setattr(db_module, "get_settings", lambda: configured)
    monkeypatch.setattr(db_module, "create_engine", create)
    try:
        assert db_module.get_readiness_engine() is fake_engine
    finally:
        db_module.get_readiness_engine.cache_clear()
        fake_engine.dispose()

    assert created == [
        (
            configured.database_url,
            {
                "pool_pre_ping": True,
                "pool_size": 1,
                "max_overflow": 0,
                "pool_timeout": 3.0,
                "connect_args": {
                    "connect_timeout": 3,
                    "options": "-c statement_timeout=3000",
                },
            },
        )
    ]


def test_database_probe_sets_postgres_statement_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection("postgresql")
    monkeypatch.setattr(readiness, "get_readiness_engine", lambda: FakeEngine(connection))

    readiness.check_database(settings())

    assert connection.executions == [
        (
            "SELECT set_config('statement_timeout', :timeout, true)",
            {"timeout": "3s"},
        ),
        ("SELECT version_num FROM alembic_version ORDER BY version_num", None),
        ("SELECT 1", None),
    ]


def test_database_probe_rejects_an_older_schema_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection("postgresql")

    def execute(statement: object, parameters: object | None = None) -> SimpleNamespace | None:
        connection.executions.append((str(statement), parameters))
        if "FROM alembic_version" in str(statement):
            return SimpleNamespace(scalars=lambda: ("0013_storage_quota_security_functions",))
        return None

    connection.execute = execute  # type: ignore[method-assign]
    monkeypatch.setattr(readiness, "get_readiness_engine", lambda: FakeEngine(connection))

    with pytest.raises(RuntimeError, match="schema revision"):
        readiness.check_database(settings())


class CapacityResult:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def mappings(self) -> CapacityResult:
        return self

    def one_or_none(self) -> dict[str, object]:
        return self.row


class CapacityConnection(FakeConnection):
    def __init__(self, row: dict[str, object]) -> None:
        super().__init__("postgresql")
        self.row = row

    def execute(
        self,
        statement: object,
        parameters: object | None = None,
    ) -> CapacityResult | None:
        self.executions.append((str(statement), parameters))
        if "FROM storage_global_quotas" in str(statement):
            return CapacityResult(self.row)
        return None


def _capacity_settings() -> SimpleNamespace:
    return SimpleNamespace(
        app_env="production",
        readiness_timeout_seconds=3,
        storage_capacity_provisioned_bytes=1_000,
        storage_capacity_metadata_overhead_bytes=100,
        storage_capacity_emergency_reserve_bytes=100,
        storage_capacity_headroom_bytes=200,
        storage_capacity_byte_limit=800,
        storage_capacity_object_limit=100,
        storage_capacity_volume_identity="volume-0001",
        storage_capacity_operator_config_sha256="a" * 64,
        storage_capacity_deploy_descriptor_sha256="b" * 64,
        storage_capacity_max_age_seconds=600,
        s3_bucket="production-bucket",
    )


def _capacity_row(now: datetime) -> dict[str, object]:
    return {
        "capacity_verified": True,
        "provisioned_bytes": 1_000,
        "metadata_overhead_bytes": 100,
        "emergency_reserve_bytes": 100,
        "capacity_headroom_bytes": 200,
        "byte_limit": 800,
        "object_limit": 100,
        "volume_identity": "volume-0001",
        "capacity_bucket": "production-bucket",
        "capacity_operator_config_sha256": "a" * 64,
        "deploy_descriptor_sha256": "b" * 64,
        "inventory_sha256": "c" * 64,
        "capacity_evidence_sha256": "d" * 64,
        "reserved_bytes": 20,
        "committed_bytes": 100,
        "reserved_count": 1,
        "committed_count": 2,
        "inventory_object_count": 2,
        "inventory_bytes": 100,
        "ledger_object_count": 2,
        "ledger_bytes": 100,
        "database_now": now,
        "database_started_at": now - timedelta(hours=1),
        "maintenance_token": None,
        "maintenance_started_at": None,
        "maintenance_owner_expires_at": None,
        "recovery_database_started_at": now - timedelta(hours=1),
        "recovery_completed_at": now - timedelta(minutes=30),
        "capacity_verified_at": now - timedelta(seconds=5),
        "capacity_attested_at": now - timedelta(seconds=6),
    }


def test_storage_capacity_probe_requires_exact_fresh_runtime_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    row = _capacity_row(now)
    connection = CapacityConnection(row)
    monkeypatch.setattr(readiness, "get_readiness_engine", lambda: FakeEngine(connection))

    readiness.check_storage_capacity(_capacity_settings())  # type: ignore[arg-type]

    row["capacity_operator_config_sha256"] = "e" * 64
    with pytest.raises(RuntimeError, match="runtime settings"):
        readiness.check_storage_capacity(_capacity_settings())  # type: ignore[arg-type]
    row.update(_capacity_row(now))
    row["capacity_verified_at"] = now - timedelta(seconds=601)
    with pytest.raises(RuntimeError, match="stale"):
        readiness.check_storage_capacity(_capacity_settings())  # type: ignore[arg-type]


def test_storage_capacity_accepts_trusted_counter_changes_after_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    row = _capacity_row(now)
    row["committed_count"] = 3
    row["committed_bytes"] = 140
    connection = CapacityConnection(row)
    monkeypatch.setattr(readiness, "get_readiness_engine", lambda: FakeEngine(connection))

    readiness.check_storage_capacity(_capacity_settings())  # type: ignore[arg-type]


def test_storage_capacity_rejects_active_gate_and_stale_boot_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    row = _capacity_row(now)
    connection = CapacityConnection(row)
    monkeypatch.setattr(readiness, "get_readiness_engine", lambda: FakeEngine(connection))

    row["maintenance_token"] = "00000000-0000-0000-0000-000000000099"  # noqa: S105
    with pytest.raises(RuntimeError, match="maintenance"):
        readiness.check_storage_capacity(_capacity_settings())  # type: ignore[arg-type]
    row.update(_capacity_row(now))
    row["recovery_database_started_at"] = now - timedelta(hours=2)
    with pytest.raises(RuntimeError, match="stale"):
        readiness.check_storage_capacity(_capacity_settings())  # type: ignore[arg-type]


class FakeRedisClient:
    def __init__(self, ping_result: bool = True) -> None:
        self.ping_result = ping_result
        self.closed = False

    def ping(self) -> bool:
        return self.ping_result

    def close(self) -> None:
        self.closed = True


def test_redis_probe_applies_connect_and_command_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeRedisClient()
    calls: list[tuple[str, dict[str, object]]] = []

    def from_url(url: str, **kwargs: object) -> FakeRedisClient:
        calls.append((url, kwargs))
        return client

    monkeypatch.setattr(readiness.Redis, "from_url", from_url)

    readiness.check_redis(settings())

    assert calls == [
        (
            "redis://:unit-test-secret@redis:6379/0",
            {"socket_connect_timeout": 3.0, "socket_timeout": 3.0},
        )
    ]
    assert client.closed is True


def test_object_storage_probe_uses_internal_endpoint_and_bounded_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    bucket_calls: list[str] = []
    close_calls: list[bool] = []

    class FakeS3Client:
        def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803 - boto3 API spelling
            bucket_calls.append(Bucket)

        def close(self) -> None:
            close_calls.append(True)

    def client(*args: object, **kwargs: object) -> FakeS3Client:
        calls.append((args, kwargs))
        return FakeS3Client()

    monkeypatch.setattr(readiness.boto3, "client", client)

    readiness.check_object_storage(settings())

    assert calls[0][0] == ("s3",)
    options = calls[0][1]
    assert options["endpoint_url"] == "http://object-storage:8333"
    config: Any = options["config"]
    assert config.connect_timeout == 3.0
    assert config.read_timeout == 3.0
    assert config.retries["total_max_attempts"] == 1
    assert bucket_calls == ["custombuild-artifacts"]
    assert close_calls == [True]


def test_dependency_probe_reports_all_failures_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def database(_settings: Settings) -> None:
        calls.append("database")

    def redis(_settings: Settings) -> None:
        calls.append("redis")
        raise TimeoutError("credential-like-detail-must-not-be-returned")

    def object_storage(_settings: Settings) -> None:
        calls.append("object_storage")

    def storage_capacity(_settings: Settings) -> None:
        calls.append("storage_capacity")

    def rule_engine(_settings: Settings) -> None:
        calls.append("rule_engine")

    monkeypatch.setattr(readiness, "check_database", database)
    monkeypatch.setattr(readiness, "check_redis", redis)
    monkeypatch.setattr(readiness, "check_storage_capacity", storage_capacity)
    monkeypatch.setattr(readiness, "check_object_storage", object_storage)
    monkeypatch.setattr(readiness, "check_rule_engine", rule_engine)

    statuses, failures = readiness.probe_dependencies(settings())

    assert sorted(calls) == [
        "database",
        "object_storage",
        "redis",
        "rule_engine",
        "storage_capacity",
    ]
    assert statuses == {
        "database": "ok",
        "storage_capacity": "ok",
        "redis": "unavailable",
        "object_storage": "ok",
        "rule_engine": "ok",
    }
    assert failures == [readiness.DependencyFailure(name="redis", error_type="TimeoutError")]


def test_rule_engine_probe_fails_closed_when_package_cannot_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import design_service

    def missing(_name: str) -> object:
        raise ImportError("simulated missing rule package")

    monkeypatch.setattr(design_service, "import_module", missing)

    with pytest.raises(design_service.RuleEngineUnavailable, match="RULE_ENGINE_UNAVAILABLE"):
        readiness.check_rule_engine(settings())
