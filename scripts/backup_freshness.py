"""Fail-closed freshness monitor for coordinated Custombuild backups."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.compose_backup import (
    MANIFEST_SCHEMA,
    BackupError,
    validate_tombstone_history,
    verify_manifest,
)


@dataclass(frozen=True)
class BackupCandidate:
    directory: Path
    created_at: datetime
    database_captured_at: datetime


def _timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"manifest has no {label} timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"manifest {label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"manifest {label} timestamp has no timezone")
    return parsed.astimezone(UTC)


def _read_candidate(path: Path) -> BackupCandidate:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("manifest is not readable JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != MANIFEST_SCHEMA:
        raise ValueError("manifest schema is not an approved coordinated-backup schema")
    database_snapshot = value.get("database_snapshot")
    if not isinstance(database_snapshot, dict):
        raise ValueError("manifest has no PostgreSQL snapshot")
    row_counts = database_snapshot.get("row_counts")
    tombstone_count = (
        row_counts.get("storage_object_tombstones") if isinstance(row_counts, dict) else None
    )
    if isinstance(tombstone_count, bool) or not isinstance(tombstone_count, int):
        raise ValueError("manifest has no exact tombstone table row count")
    try:
        validate_tombstone_history(
            database_snapshot.get("tombstone_history"),
            expected_count=tombstone_count,
        )
    except BackupError as exc:
        raise ValueError("manifest has invalid tombstone history evidence") from exc
    return BackupCandidate(
        directory=path.parent,
        created_at=_timestamp(value.get("created_at"), label="creation"),
        database_captured_at=_timestamp(
            database_snapshot.get("captured_at"),
            label="PostgreSQL snapshot",
        ),
    )


def backup_freshness(
    root: Path,
    *,
    max_age: timedelta,
    now: datetime | None = None,
    verify_payloads: bool = False,
) -> dict[str, Any]:
    """Return an alert-friendly backup status without exposing credentials or object keys."""
    if max_age <= timedelta(0):
        raise ValueError("max_age must be positive")
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    valid: list[BackupCandidate] = []
    invalid: list[tuple[Path, datetime, str]] = []
    for manifest in sorted(root.glob("**/manifest.json")):
        try:
            valid.append(_read_candidate(manifest))
        except ValueError as exc:
            try:
                modified = datetime.fromtimestamp(manifest.stat().st_mtime, tz=UTC)
            except OSError:
                modified = checked_at
            invalid.append((manifest, modified, str(exc)))

    base: dict[str, Any] = {
        "schema_version": "custombuild.backup-freshness.v1",
        "checked_at": checked_at.isoformat(),
        "max_age_seconds": int(max_age.total_seconds()),
        "valid_manifest_count": len(valid),
        "invalid_manifest_count": len(invalid),
    }
    if not valid:
        if invalid:
            return {
                **base,
                "status": "INVALID",
                "solution": "Inspect the invalid manifest, then create a new verified backup.",
            }
        return {
            **base,
            "status": "MISSING",
            "solution": "Create and verify a coordinated backup now.",
        }

    latest = max(valid, key=lambda item: item.created_at)
    newer_invalid = [item for item in invalid if item[1] >= latest.created_at]
    if newer_invalid:
        return {
            **base,
            "status": "INVALID",
            "latest_backup": str(latest.directory),
            "solution": "Inspect the newest invalid manifest, then create a new verified backup.",
        }

    future_tolerance = timedelta(minutes=5)
    if latest.created_at > checked_at + future_tolerance:
        return {
            **base,
            "status": "INVALID",
            "latest_backup": str(latest.directory),
            "solution": "Correct clock synchronization and create a new verified backup.",
        }
    if latest.database_captured_at > checked_at + future_tolerance:
        return {
            **base,
            "status": "INVALID",
            "latest_backup": str(latest.directory),
            "solution": "Correct database clock synchronization and create a new verified backup.",
        }

    if verify_payloads:
        try:
            verify_manifest(latest.directory)
        except BackupError:
            return {
                **base,
                "status": "INVALID",
                "latest_backup": str(latest.directory),
                "solution": "Quarantine the damaged backup and create a new verified backup.",
            }

    # A newly completed manifest can describe an old database dump.  Recovery
    # point age therefore comes from the database snapshot, never filesystem or
    # manifest completion time.
    age = max(timedelta(0), checked_at - latest.database_captured_at)
    details = {
        **base,
        "latest_backup": str(latest.directory),
        "latest_created_at": latest.created_at.isoformat(),
        "latest_database_snapshot_captured_at": latest.database_captured_at.isoformat(),
        "age_seconds": int(age.total_seconds()),
        "payloads_verified": verify_payloads,
    }
    if age > max_age:
        return {
            **details,
            "status": "STALE",
            "solution": "Create and verify a coordinated backup now, then check the scheduler.",
        }
    return {**details, "status": "OK", "solution": "No action required."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Directory containing backups")
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--verify-payloads", action="store_true")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        result = backup_freshness(
            arguments.root.resolve(),
            max_age=timedelta(hours=arguments.max_age_hours),
            verify_payloads=arguments.verify_payloads,
        )
    except ValueError as exc:
        parser.error(str(exc))
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
