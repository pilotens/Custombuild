from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from scripts.storage_capacity_development import (
    DEVELOPMENT_OBJECT_LIMIT,
    _retain_only_current_evidence,
    development_operator_config,
    main,
)
from scripts.storage_capacity_preflight import CapacityPreflightError, canonical_json_bytes


def test_development_capacity_is_derived_from_the_mounted_filesystem(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 28, 12, 34, 56, 789, tzinfo=UTC)
    config = development_operator_config(
        {
            "OBJECT_STORAGE_VOLUME_NAME": "compose-volume-001",
            "S3_BUCKET": "custombuild-artifacts",
        },
        now=now,
        committed_bytes=12_345,
        volume_path=tmp_path,
    )

    filesystem = os.statvfs(tmp_path)
    expected_total = filesystem.f_frsize * filesystem.f_blocks
    expected_available = filesystem.f_frsize * filesystem.f_bavail
    expected_emergency = max(expected_total // 100, 1)
    expected_safety = max(expected_total // 100, 1)
    assert config.provisioned_bytes == expected_total
    assert config.headroom_bytes == (
        config.metadata_overhead_bytes + config.emergency_reserve_bytes
    )
    assert config.byte_limit == (12_345 + expected_available - expected_emergency - expected_safety)
    assert config.byte_limit - 12_345 + config.emergency_reserve_bytes == (
        expected_available - expected_safety
    )
    assert config.object_limit == DEVELOPMENT_OBJECT_LIMIT
    assert config.volume_identity == "compose-volume-001"
    assert config.bucket == "custombuild-artifacts"
    assert config.requested_at == now.replace(microsecond=0)
    unsigned = {
        "bucket": config.bucket,
        "byte_limit": config.byte_limit,
        "deploy_descriptor_sha256": config.deploy_descriptor_sha256,
        "emergency_reserve_bytes": config.emergency_reserve_bytes,
        "headroom_bytes": config.headroom_bytes,
        "metadata_overhead_bytes": config.metadata_overhead_bytes,
        "object_limit": config.object_limit,
        "provisioned_bytes": config.provisioned_bytes,
        "requested_at": "2026-08-28T12:34:56Z",
        "schema_version": "custombuild.storage-capacity-operator.v1",
        "volume_identity": config.volume_identity,
    }
    assert config.sha256 == hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()


@pytest.mark.parametrize("app_env", (None, "", "production"))
def test_development_attestor_requires_explicit_nonproduction_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    app_env: str | None,
) -> None:
    if app_env is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", app_env)

    assert main() == 1

    captured = capsys.readouterr()
    assert "requires explicit development or test mode" in captured.err


def test_development_attestor_rejects_the_migrator_login(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("S3_ENDPOINT", "http://object-storage:8333")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://custombuild_migrator:strong-migrator-password@postgres/db",
    )

    assert main() == 1

    assert "fixed storage-attestor PostgreSQL role" in capsys.readouterr().err


def test_development_evidence_retention_is_bounded(tmp_path: Path) -> None:
    evidence_names = [f"storage-capacity-attestation-{index:064x}.json" for index in range(32)]
    for name in evidence_names:
        (tmp_path / name).write_text("{}\n", encoding="utf-8")
    unrelated = tmp_path / "operator-note.txt"
    unrelated.write_text("keep\n", encoding="utf-8")
    current = tmp_path / evidence_names[-1]

    _retain_only_current_evidence(tmp_path, current)

    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [current.name, unrelated.name]
    )


def test_development_evidence_retention_rejects_an_unsafe_entry(
    tmp_path: Path,
) -> None:
    current = tmp_path / f"storage-capacity-attestation-{'a' * 64}.json"
    current.write_text("{}\n", encoding="utf-8")
    unsafe = tmp_path / f"storage-capacity-attestation-{'b' * 64}.json"
    unsafe.mkdir()

    with pytest.raises(CapacityPreflightError, match="unsafe entry"):
        _retain_only_current_evidence(tmp_path, current)


def test_compose_uses_separate_development_and_production_attestors() -> None:
    base = yaml.safe_load(Path("compose.yml").read_text(encoding="utf-8"))
    external = yaml.safe_load(Path("compose.external-production.yml").read_text(encoding="utf-8"))
    development = base["services"]["storage-capacity-attestor"]
    production = external["services"]["storage-capacity-attestor"]
    development_recovery = base["services"]["storage-recovery"]
    production_recovery = external["services"]["storage-recovery"]

    assert development["command"] == [
        "python",
        "-m",
        "scripts.storage_capacity_development",
    ]
    assert development["environment"]["APP_ENV"] == "${APP_ENV:-development}"
    assert base["services"]["migrate"]["environment"]["DATABASE_URL"].startswith(
        "${MIGRATION_DATABASE_URL:-postgresql+psycopg://custombuild_migrator:"
    )
    assert development["environment"]["DATABASE_URL"].startswith(
        "${CAPACITY_ATTESTOR_DATABASE_URL:-postgresql+psycopg://custombuild_storage_attestor:"
    )
    assert development["user"] == "65532:65532"
    assert production["command"][2] == "scripts.storage_capacity_preflight"
    assert production["environment"]["APP_ENV"] == "production"
    assert production["user"] == "65532:65532"
    assert production["volumes"][0] == "object-storage-data:/storage-volume:ro"
    assert development_recovery["command"] == [
        "python",
        "-m",
        "scripts.storage_recovery",
    ]
    assert development_recovery["restart"] == "no"
    assert (
        development_recovery["environment"]["DATABASE_URL"]
        == (base["services"]["migrate"]["environment"]["DATABASE_URL"])
    )
    assert (
        production_recovery["environment"]["DATABASE_URL"]
        == (external["services"]["migrate"]["environment"]["DATABASE_URL"])
    )
    assert development["depends_on"]["storage-recovery"]["condition"] == (
        "service_completed_successfully"
    )
    assert production["depends_on"]["storage-recovery"]["condition"] == (
        "service_completed_successfully"
    )
    for service_name in ("api", "worker", "scheduler"):
        dependency = base["services"][service_name]["depends_on"]
        assert dependency["storage-capacity-attestor"]["condition"] == "service_healthy"
