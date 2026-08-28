from __future__ import annotations

import hashlib
import io
import json
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import scripts.storage_capacity_preflight as capacity
from scripts.storage_capacity_preflight import (
    ATTESTATION_SCHEMA_VERSION,
    OPERATOR_CONFIG_SCHEMA_VERSION,
    CapacityPreflightError,
    LedgerObject,
    OperatorConfig,
    S3Object,
    VolumeObservation,
    build_attestation,
    canonical_json_bytes,
    inventory_s3,
    load_operator_config,
    validate_expected_environment,
    verify_deploy_descriptor,
    verify_inventory_matches_ledger,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def operator_value(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": OPERATOR_CONFIG_SCHEMA_VERSION,
        "volume_identity": "provider-volume-0001",
        "provisioned_bytes": 1_000,
        "metadata_overhead_bytes": 100,
        "emergency_reserve_bytes": 100,
        "headroom_bytes": 200,
        "byte_limit": 800,
        "object_limit": 100,
        "bucket": "production-artifacts",
        "deploy_descriptor_sha256": "d" * 64,
        "requested_at": "2026-08-28T12:00:00Z",
    }
    value.update(overrides)
    return value


def write_operator_config(
    tmp_path: Path,
    value: dict[str, object] | None = None,
) -> tuple[Path, str]:
    canonical = canonical_json_bytes(value or operator_value())
    path = tmp_path / "storage-capacity-operator.json"
    path.write_bytes(canonical + b"\n")
    return path, hashlib.sha256(canonical).hexdigest()


def parsed_config(tmp_path: Path) -> OperatorConfig:
    path, digest = write_operator_config(tmp_path)
    return load_operator_config(path, expected_sha256=digest, now=NOW)


def expected_environment(config: OperatorConfig) -> dict[str, str]:
    return {
        "STORAGE_CAPACITY_OPERATOR_CONFIG_SHA256": config.sha256,
        "STORAGE_CAPACITY_VOLUME_IDENTITY": config.volume_identity,
        "OBJECT_STORAGE_VOLUME_NAME": config.volume_identity,
        "STORAGE_CAPACITY_PROVISIONED_BYTES": str(config.provisioned_bytes),
        "STORAGE_CAPACITY_METADATA_OVERHEAD_BYTES": str(config.metadata_overhead_bytes),
        "STORAGE_CAPACITY_EMERGENCY_RESERVE_BYTES": str(config.emergency_reserve_bytes),
        "STORAGE_CAPACITY_HEADROOM_BYTES": str(config.headroom_bytes),
        "STORAGE_CAPACITY_BYTE_LIMIT": str(config.byte_limit),
        "STORAGE_CAPACITY_OBJECT_LIMIT": str(config.object_limit),
        "STORAGE_CAPACITY_DEPLOY_DESCRIPTOR_SHA256": (config.deploy_descriptor_sha256),
        "STORAGE_CAPACITY_MAX_AGE_SECONDS": "600",
        "S3_BUCKET": config.bucket,
    }


def ledger_object(
    *,
    key: str = "objects/a.bin",
    payload: bytes = b"payload",
    media_type: str = "application/octet-stream",
) -> LedgerObject:
    return LedgerObject(
        organization_id="00000000-0000-0000-0000-000000000001",
        key=key,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type=media_type,
    )


def s3_object(
    *,
    key: str = "objects/a.bin",
    payload: bytes = b"payload",
    media_type: str = "application/octet-stream",
) -> S3Object:
    digest = hashlib.sha256(payload).hexdigest()
    return S3Object(
        key=key,
        sha256=digest,
        size_bytes=len(payload),
        media_type=media_type,
        metadata=(("immutable", "true"), ("sha256", digest)),
    )


class FakeS3Client:
    def __init__(self, objects: dict[str, tuple[bytes, str, dict[str, str]]]) -> None:
        self.objects = objects

    def head_bucket(self, **_kwargs: str) -> dict[str, object]:
        return {}

    def list_objects_v2(self, **_kwargs: str) -> dict[str, object]:
        return {
            "Contents": [
                {"Key": key, "Size": len(value[0])}
                for key, value in reversed(tuple(self.objects.items()))
            ],
            "IsTruncated": False,
        }

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
        assert Bucket
        payload, media_type, metadata = self.objects[Key]
        return {
            "Body": io.BytesIO(payload),
            "ContentLength": len(payload),
            "ContentType": media_type,
            "Metadata": metadata,
        }


def test_operator_config_is_canonical_hash_bound_and_fresh(tmp_path: Path) -> None:
    config = parsed_config(tmp_path)

    assert config.volume_identity == "provider-volume-0001"
    assert config.headroom_bytes == 200
    assert config.byte_limit == 800
    assert config.requested_at == NOW
    assert len(config.sha256) == 64


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"unexpected": True}, "unknown fields"),
        ({"schema_version": "custombuild.storage-capacity-operator.v0"}, "unsupported"),
        ({"volume_identity": "../volume"}, "volume_identity"),
        ({"metadata_overhead_bytes": -1}, "metadata_overhead_bytes"),
        ({"headroom_bytes": 201}, "headroom_bytes must equal"),
        ({"provisioned_bytes": 200}, "must exceed headroom"),
        ({"byte_limit": 801}, "exceeds physically usable"),
        ({"bucket": "Production-Artifacts"}, "canonical S3"),
        ({"bucket": "192.0.2.1"}, "canonical S3"),
        ({"deploy_descriptor_sha256": "D" * 64}, "deploy_descriptor_sha256"),
        ({"requested_at": "2026-08-28T11:49:59Z"}, "stale"),
        ({"requested_at": "2026-08-28T12:00:31Z"}, "future"),
    ),
)
def test_operator_config_rejects_invalid_or_stale_values(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    path, digest = write_operator_config(tmp_path, operator_value(**overrides))

    with pytest.raises(CapacityPreflightError, match=message):
        load_operator_config(path, expected_sha256=digest, now=NOW)


def test_operator_config_rejects_hash_mismatch_noncanonical_json_and_duplicates(
    tmp_path: Path,
) -> None:
    path, digest = write_operator_config(tmp_path)
    with pytest.raises(CapacityPreflightError, match="does not match"):
        load_operator_config(path, expected_sha256="f" * 64, now=NOW)

    path.write_text(json.dumps(operator_value(), indent=2), encoding="utf-8")
    with pytest.raises(CapacityPreflightError, match="not canonical"):
        load_operator_config(path, expected_sha256=digest, now=NOW)

    path.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
    with pytest.raises(CapacityPreflightError, match="repeats field"):
        load_operator_config(path, expected_sha256=digest, now=NOW)


def test_expected_environment_must_match_every_operator_field(tmp_path: Path) -> None:
    config = parsed_config(tmp_path)
    environment = expected_environment(config)
    validate_expected_environment(config, environment)

    for name in tuple(environment):
        drifted = {**environment, name: f"{environment[name]}-drift"}
        with pytest.raises(CapacityPreflightError, match=name):
            validate_expected_environment(config, drifted)


def test_deploy_descriptor_is_bound_by_exact_raw_bytes(tmp_path: Path) -> None:
    descriptor = tmp_path / "deploy-descriptor.json"
    descriptor.write_bytes(b'{"schema_version":"custombuild.deploy-descriptor.v2"}\n')
    digest = hashlib.sha256(descriptor.read_bytes()).hexdigest()

    verify_deploy_descriptor(descriptor, digest)
    with pytest.raises(CapacityPreflightError, match="does not match"):
        verify_deploy_descriptor(descriptor, "f" * 64)


def test_s3_inventory_hashes_bodies_and_requires_immutable_metadata() -> None:
    payload_a = b"alpha"
    payload_b = b"beta"
    client = FakeS3Client(
        {
            "objects/b.bin": (
                payload_b,
                "application/octet-stream",
                {
                    "sha256": hashlib.sha256(payload_b).hexdigest(),
                    "immutable": "true",
                    "manifest-sha256": "a" * 64,
                },
            ),
            "objects/a.bin": (
                payload_a,
                "application/octet-stream",
                {
                    "sha256": hashlib.sha256(payload_a).hexdigest(),
                    "immutable": "true",
                },
            ),
        }
    )

    inventory = inventory_s3(client, "production-artifacts")

    assert [item.key for item in inventory] == ["objects/a.bin", "objects/b.bin"]
    assert inventory[0].sha256 == hashlib.sha256(payload_a).hexdigest()
    assert dict(inventory[1].metadata)["manifest-sha256"] == "a" * 64

    client.objects["objects/a.bin"][2]["sha256"] = "f" * 64
    with pytest.raises(CapacityPreflightError, match="immutable metadata"):
        inventory_s3(client, "production-artifacts")


def test_inventory_and_ledger_must_match_keys_identity_count_and_bytes() -> None:
    ledger = (ledger_object(),)
    inventory = (s3_object(),)
    verify_inventory_matches_ledger(ledger, inventory)

    with pytest.raises(CapacityPreflightError, match="unknown keys"):
        verify_inventory_matches_ledger((), inventory)
    with pytest.raises(CapacityPreflightError, match="missing from S3"):
        verify_inventory_matches_ledger(ledger, ())
    with pytest.raises(CapacityPreflightError, match="immutable ledger identity"):
        verify_inventory_matches_ledger(
            ledger,
            (s3_object(media_type="application/pdf"),),
        )


def test_attestation_binds_physical_limits_and_exact_inventory_digests(
    tmp_path: Path,
) -> None:
    config = parsed_config(tmp_path)
    ledger = (ledger_object(),)
    inventory = (s3_object(),)
    volume = VolumeObservation(device=41, total_bytes=1_000, available_bytes=900)

    attestation = build_attestation(
        config=config,
        volume=volume,
        inventory=inventory,
        ledger=ledger,
        attested_at=NOW,
    )

    assert attestation["schema_version"] == ATTESTATION_SCHEMA_VERSION
    assert attestation["volume"] == {
        "identity": "provider-volume-0001",
        "device": 41,
        "provisioned_bytes": 1_000,
        "observed_total_bytes": 1_000,
        "observed_available_bytes": 900,
        "metadata_overhead_bytes": 100,
        "emergency_reserve_bytes": 100,
        "headroom_bytes": 200,
    }
    assert attestation["s3_inventory"] == {
        "sha256": hashlib.sha256(canonical_json_bytes([inventory[0].evidence()])).hexdigest(),
        "object_count": 1,
        "bytes": 7,
    }
    unsigned = dict(attestation)
    digest = unsigned.pop("evidence_sha256")
    assert digest == hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()


class _Begin(AbstractContextManager[Any]):
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def __enter__(self) -> Any:
        return self.connection

    def __exit__(self, *_args: object) -> None:
        return None


class FakeEngine:
    def __init__(self) -> None:
        self.connection = SimpleNamespace(execute=self.execute)
        self.executions: list[tuple[str, dict[str, object] | None]] = []

    def begin(self) -> _Begin:
        return _Begin(self.connection)

    def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> SimpleNamespace:
        self.executions.append((str(statement), parameters))
        return SimpleNamespace(rowcount=1)


class FakeRows:
    def __init__(
        self,
        *,
        rows: tuple[dict[str, object], ...] = (),
        scalars: tuple[object, ...] = (),
    ) -> None:
        self.rows = rows
        self.scalar_values = scalars

    def mappings(self) -> FakeRows:
        return self

    def scalars(self) -> tuple[object, ...]:
        return self.scalar_values

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def __iter__(self) -> Any:
        return iter(self.rows)


class FakeRlsConnection:
    def __init__(self, *, tombstone_overlap: bool = False) -> None:
        self.organizations = ("tenant-a", "tenant-b")
        self.contexts: list[str] = []
        self.statements: list[str] = []
        self.tombstone_overlap = tombstone_overlap

    def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> FakeRows:
        sql = str(statement)
        self.statements.append(sql)
        if "custombuild_storage_lock_capacity" in sql:
            return FakeRows()
        if "FROM storage_global_quotas" in sql:
            return FakeRows(
                rows=(
                    {
                        "database_now": NOW,
                        "reserved_bytes": 0,
                        "reserved_count": 0,
                        "committed_bytes": 7,
                        "committed_count": 1,
                    },
                )
            )
        if "SELECT id FROM organizations" in sql:
            return FakeRows(scalars=self.organizations)
        if "set_config('app.current_organization_id'" in sql:
            context = str(parameters["organization_id"]) if parameters else ""
            self.contexts.append(context)
            return FakeRows()
        assert parameters is not None
        organization_id = str(parameters["organization_id"])
        assert self.contexts[-1] == organization_id
        if "JOIN storage_object_tombstones" in sql:
            if self.tombstone_overlap and organization_id == "tenant-a":
                return FakeRows(rows=({"overlap_key": "objects/a.bin"},))
            return FakeRows()
        if "FROM stored_objects" in sql:
            if organization_id == "tenant-a":
                return FakeRows(
                    rows=(
                        {
                            "organization_id": organization_id,
                            "object_key": "objects/a.bin",
                            "sha256": hashlib.sha256(b"payload").hexdigest(),
                            "size_bytes": 7,
                            "media_type": "application/octet-stream",
                            "state": "committed",
                        },
                    )
                )
            return FakeRows()
        if "FROM storage_tenant_quotas" in sql:
            committed_bytes = 7 if organization_id == "tenant-a" else 0
            committed_count = 1 if organization_id == "tenant-a" else 0
            return FakeRows(
                rows=(
                    {
                        "organization_id": organization_id,
                        "reserved_bytes": 0,
                        "reserved_count": 0,
                        "committed_bytes": committed_bytes,
                        "committed_count": committed_count,
                    },
                )
            )
        raise AssertionError(sql)


def test_locked_ledger_iterates_every_organization_under_force_rls() -> None:
    connection = FakeRlsConnection()

    _global, ledger, database_now = capacity.load_locked_ledger(
        connection,  # type: ignore[arg-type]
        capacity_bucket="production-artifacts",
    )

    assert database_now == NOW
    assert [item.organization_id for item in ledger] == ["tenant-a"]
    assert connection.contexts == ["tenant-a", "tenant-b", ""]
    assert "custombuild_storage_lock_capacity" in connection.statements[0]
    assert all("FOR UPDATE" not in statement for statement in connection.statements)
    assert all("FOR SHARE" not in statement for statement in connection.statements)


def test_locked_ledger_rejects_count_preserving_tombstone_identity_overlap() -> None:
    connection = FakeRlsConnection(tombstone_overlap=True)

    with pytest.raises(CapacityPreflightError, match="permanently retired"):
        capacity.load_locked_ledger(
            connection,  # type: ignore[arg-type]
            capacity_bucket="production-artifacts",
        )

    overlap_sql = next(
        statement
        for statement in connection.statements
        if "JOIN storage_object_tombstones" in statement
    )
    assert "tombstone.object_key = stored.object_key" in overlap_sql
    assert "OR tombstone.idempotency_key = stored.idempotency_key" in overlap_sql
    assert connection.contexts == ["tenant-a", ""]


def test_activation_persists_evidence_before_one_guarded_attestor_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = parsed_config(tmp_path)
    engine = FakeEngine()
    evidence_directory = tmp_path / "evidence"
    evidence_directory.mkdir()
    ledger = (ledger_object(),)
    inventory = (s3_object(),)
    monkeypatch.setattr(
        capacity,
        "load_locked_ledger",
        lambda _connection, *, capacity_bucket: ({}, ledger, NOW),
    )
    monkeypatch.setattr(capacity, "inventory_s3", lambda _client, _bucket: inventory)
    monkeypatch.setattr(capacity, "_database_clock", lambda _connection: NOW)
    monkeypatch.setattr(
        capacity,
        "observe_volume",
        lambda _path, *, provisioned_bytes: VolumeObservation(
            device=41,
            total_bytes=provisioned_bytes,
            available_bytes=900,
        ),
    )

    attestation, evidence_path = capacity.activate_capacity(
        engine,  # type: ignore[arg-type]
        config=config,
        volume_path=tmp_path,
        evidence_directory=evidence_directory,
        s3_client=object(),
    )

    assert evidence_path.read_bytes() == canonical_json_bytes(attestation) + b"\n"
    assert len(engine.executions) == 1
    sql, parameters = engine.executions[0]
    assert "SELECT public.custombuild_storage_attest_capacity(" in sql
    assert "UPDATE storage_global_quotas" not in sql
    assert parameters is not None
    assert parameters["evidence_sha256"] == attestation["evidence_sha256"]


def test_initial_busy_retry_still_requires_a_fresh_operator_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = parsed_config(tmp_path)
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        dispose=lambda: None,
    )
    requirements: list[bool] = []
    invalidations: list[object] = []

    class Parser:
        @staticmethod
        def parse_args(_argv: object) -> SimpleNamespace:
            return SimpleNamespace(
                operator_config=tmp_path / "operator.json",
                deploy_descriptor=tmp_path / "descriptor.json",
                volume_path=tmp_path,
                evidence_directory=tmp_path,
                watch_interval_seconds=60,
                heartbeat_file=tmp_path / "heartbeat.json",
            )

    def activate(_engine: object, **kwargs: object) -> object:
        requirements.append(bool(kwargs["require_fresh_operator_request"]))
        if len(requirements) == 1:
            raise capacity.CapacityAttestationBusy("writer is active")
        raise CapacityPreflightError("operator request became stale")

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://custombuild_storage_attestor:"
        "strong-capacity-attestor-test-password@postgres/custombuild",
    )
    monkeypatch.setattr(capacity, "_parser", lambda: Parser())
    monkeypatch.setattr(capacity, "load_operator_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(capacity, "validate_expected_environment", lambda *_args: None)
    monkeypatch.setattr(capacity, "verify_deploy_descriptor", lambda *_args: None)
    monkeypatch.setattr(capacity, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(capacity, "_s3_client", lambda _environment: object())
    monkeypatch.setattr(capacity, "install_capacity_refresh_signal", lambda: None)
    monkeypatch.setattr(capacity, "wait_for_capacity_refresh", lambda _seconds: None)
    monkeypatch.setattr(capacity, "activate_capacity", activate)
    monkeypatch.setattr(
        capacity,
        "invalidate_capacity",
        lambda actual_engine: invalidations.append(actual_engine),
    )

    assert capacity.main([]) == 1
    assert requirements == [True, True]
    assert invalidations == [engine]
    captured = capsys.readouterr()
    assert "refresh deferred" in captured.err
    assert "operator request became stale" in captured.err


def test_production_preflight_rejects_the_migrator_login_before_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = parsed_config(tmp_path)

    class Parser:
        @staticmethod
        def parse_args(_argv: object) -> SimpleNamespace:
            return SimpleNamespace(
                operator_config=tmp_path / "operator.json",
                deploy_descriptor=tmp_path / "descriptor.json",
                volume_path=tmp_path,
                evidence_directory=tmp_path,
                watch_interval_seconds=0,
                heartbeat_file=None,
            )

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://custombuild_migrator:"
        "strong-migrator-database-password@postgres/custombuild",
    )
    monkeypatch.setattr(capacity, "_parser", lambda: Parser())
    monkeypatch.setattr(capacity, "load_operator_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(capacity, "validate_expected_environment", lambda *_args: None)
    monkeypatch.setattr(capacity, "verify_deploy_descriptor", lambda *_args: None)
    monkeypatch.setattr(
        capacity,
        "create_engine",
        lambda *_args, **_kwargs: pytest.fail("migrator login reached create_engine"),
    )

    assert capacity.main([]) == 1

    assert "fixed storage-attestor role" in capsys.readouterr().err


def test_external_production_uses_one_preprovisioned_volume_and_live_attestor() -> None:
    external = yaml.safe_load(Path("compose.external-production.yml").read_text(encoding="utf-8"))
    attestor = external["services"]["storage-capacity-attestor"]

    assert external["volumes"]["object-storage-data"] == {
        "external": True,
        "name": "${OBJECT_STORAGE_VOLUME_NAME:?Set the immutable preprovisioned volume name}",
    }
    assert attestor["image"].startswith("$" + "{CUSTOMBUILD_DEPLOY_API_IMAGE:?")
    assert attestor["environment"]["OBJECT_STORAGE_VOLUME_NAME"] == (
        "${OBJECT_STORAGE_VOLUME_NAME:?Set the immutable preprovisioned volume name}"
    )
    assert "object-storage-data:/storage-volume:ro" in attestor["volumes"]
    assert attestor["read_only"] is True
    assert attestor["cap_drop"] == ["ALL"]
    assert "--watch-interval-seconds" in attestor["command"]
    assert "300" in attestor["command"]
    assert "capacity-heartbeat.json" in " ".join(attestor["healthcheck"]["test"])
    assert attestor["depends_on"]["storage-recovery"]["condition"] == (
        "service_completed_successfully"
    )
    for service in ("api", "worker", "scheduler"):
        dependency = external["services"][service]["depends_on"]
        assert dependency["storage-capacity-attestor"]["condition"] == "service_healthy"


def test_migration_never_fabricates_verified_physical_capacity() -> None:
    source = Path("services/api/alembic/versions/0012_storage_quota_ledger.py").read_text(
        encoding="utf-8"
    )

    assert "GLOBAL_STORAGE_BYTE_LIMIT" not in source
    assert "256 * 1024**3" not in source
    assert "0, :committed_count, false" in source
    assert '"capacity_verified"' in source
    assert "max(global_bytes, 1)" in source
