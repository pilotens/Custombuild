from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.backup_freshness import backup_freshness
from scripts.compose_backup import (
    MANIFEST_SCHEMA,
    SEAWEEDFS_IMAGE,
    TOMBSTONE_HISTORY_SCHEMA,
    build_manifest,
)

NOW = datetime(2026, 8, 11, 18, 0, tzinfo=UTC)


def create_backup(
    directory: Path,
    *,
    created_at: datetime,
    database_captured_at: datetime | None = None,
) -> None:
    directory.mkdir(parents=True)
    (directory / "database.dump").write_bytes(b"PGDMP-test")
    (directory / "artifacts.tar").write_bytes(b"ustar-test")
    manifest = build_manifest(
        directory,
        {
            "compose_project": "custombuild-test",
            "database_snapshot": {
                "captured_at": (database_captured_at or created_at).isoformat(),
                "wal_lsn": "0/16B6C50",
                "alembic_heads": ["0004_design_source_provenance"],
                "row_counts": {
                    "alembic_version": 1,
                    "projects": 1,
                    "storage_object_tombstones": 0,
                },
                "tombstone_history": {
                    "schema_version": TOMBSTONE_HISTORY_SCHEMA,
                    "count": 0,
                    "sha256": hashlib.sha256(b"[]").hexdigest(),
                },
            },
            "git_revision": "3" * 40,
            "source_manifest_sha256": "2" * 64,
            "object_store": {
                "image": SEAWEEDFS_IMAGE,
                "image_id": "sha256:" + "1" * 64,
                "bucket": "custombuild-artifacts",
                "object_count": 0,
                "total_size_bytes": 0,
                "objects": [],
            },
        },
    )
    manifest["schema_version"] = MANIFEST_SCHEMA
    manifest["created_at"] = created_at.isoformat()
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_reports_fresh_verified_backup(tmp_path: Path) -> None:
    create_backup(tmp_path / "latest", created_at=NOW - timedelta(hours=2))

    result = backup_freshness(
        tmp_path,
        max_age=timedelta(hours=24),
        now=NOW,
        verify_payloads=True,
    )

    assert result["status"] == "OK"
    assert result["payloads_verified"] is True
    assert result["age_seconds"] == 7200


def test_reports_stale_or_missing_backup_with_a_solution(tmp_path: Path) -> None:
    assert backup_freshness(tmp_path, max_age=timedelta(hours=24), now=NOW)["status"] == "MISSING"
    create_backup(tmp_path / "old", created_at=NOW - timedelta(hours=25))

    result = backup_freshness(tmp_path, max_age=timedelta(hours=24), now=NOW)

    assert result["status"] == "STALE"
    assert "Create and verify" in result["solution"]


def test_new_manifest_cannot_hide_an_old_database_recovery_point(tmp_path: Path) -> None:
    create_backup(
        tmp_path / "late-finish",
        created_at=NOW - timedelta(minutes=5),
        database_captured_at=NOW - timedelta(hours=30),
    )

    result = backup_freshness(tmp_path, max_age=timedelta(hours=24), now=NOW)

    assert result["status"] == "STALE"
    assert result["age_seconds"] == 30 * 60 * 60
    assert result["latest_created_at"] == (NOW - timedelta(minutes=5)).isoformat()
    assert result["latest_database_snapshot_captured_at"] == (NOW - timedelta(hours=30)).isoformat()


def test_newer_invalid_manifest_fails_closed(tmp_path: Path) -> None:
    create_backup(tmp_path / "valid", created_at=NOW - timedelta(hours=2))
    invalid = tmp_path / "newer" / "manifest.json"
    invalid.parent.mkdir()
    invalid.write_text("not-json", encoding="utf-8")
    future_mtime = (NOW + timedelta(minutes=1)).timestamp()
    invalid.touch()
    os.utime(invalid, (future_mtime, future_mtime))

    result = backup_freshness(tmp_path, max_age=timedelta(hours=24), now=NOW)

    assert result["status"] == "INVALID"
    assert "invalid manifest" in result["solution"]


def test_payload_verification_detects_tampering(tmp_path: Path) -> None:
    directory = tmp_path / "damaged"
    create_backup(directory, created_at=NOW - timedelta(hours=1))
    (directory / "database.dump").write_bytes(b"tampered")

    result = backup_freshness(
        tmp_path,
        max_age=timedelta(hours=24),
        now=NOW,
        verify_payloads=True,
    )

    assert result["status"] == "INVALID"
    assert "Quarantine" in result["solution"]


@pytest.mark.parametrize("mutation", ("missing", "bad_digest", "count_mismatch"))
def test_freshness_rejects_invalid_tombstone_proof_without_payload_verification(
    mutation: str,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "invalid-history"
    create_backup(directory, created_at=NOW - timedelta(hours=1))
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    snapshot = manifest["database_snapshot"]
    if mutation == "missing":
        del snapshot["tombstone_history"]
    elif mutation == "bad_digest":
        snapshot["tombstone_history"]["sha256"] = "not-a-digest"
    elif mutation == "count_mismatch":
        snapshot["tombstone_history"]["count"] = 1
    else:  # pragma: no cover - parameter list is exhaustive.
        raise AssertionError(mutation)
    path.write_text(json.dumps(manifest), encoding="utf-8")

    result = backup_freshness(
        tmp_path,
        max_age=timedelta(hours=24),
        now=NOW,
        verify_payloads=False,
    )

    assert result["status"] == "INVALID"
    assert result["invalid_manifest_count"] == 1
