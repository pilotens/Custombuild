"""Bind an operator-requested attestor refresh to exact PostgreSQL evidence.

The backup controller runs ``prepare`` immediately before SIGUSR1 and ``wait``
afterwards.  A heartbeat is accepted only when it is canonical, newer than the
database baseline, and names the exact evidence row committed by the attestor.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

try:
    from scripts.storage_capacity_preflight import (
        ATTESTATION_SCHEMA_VERSION,
        canonical_json_bytes,
    )
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from storage_capacity_preflight import (  # type: ignore[import-not-found,no-redef]
        ATTESTATION_SCHEMA_VERSION,
        canonical_json_bytes,
    )

REQUEST_SCHEMA_VERSION = "custombuild.storage-capacity-refresh.v1"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MAX_CONTROL_FILE_BYTES = 4096


class CapacityRefreshError(RuntimeError):
    """A refresh cannot be tied to fresh exact database evidence."""


@dataclass(frozen=True, slots=True)
class CapacityState:
    verified: bool
    evidence_sha256: str | None
    attested_at: datetime | None
    verified_at: datetime | None
    database_now: datetime


def _aware_utc(value: object, *, name: str, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, datetime):
        raise CapacityRefreshError(f"capacity {name} timestamp is invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: object, *, name: str) -> datetime:
    if not isinstance(value, str):
        raise CapacityRefreshError(f"capacity {name} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapacityRefreshError(f"capacity {name} timestamp is invalid") from exc
    if parsed.tzinfo is None or _timestamp(parsed) != value:
        raise CapacityRefreshError(f"capacity {name} timestamp is not canonical")
    return parsed.astimezone(UTC)


def _capacity_state(engine: Engine) -> CapacityState:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT capacity_verified, capacity_evidence_sha256, "
                "capacity_attested_at, capacity_verified_at, "
                "clock_timestamp() AS database_now "
                "FROM storage_global_quotas WHERE id = 1"
            )
        ).mappings().one_or_none()
    if row is None:
        raise CapacityRefreshError("global storage quota singleton is missing")
    digest = row["capacity_evidence_sha256"]
    if digest is not None and (
        not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None
    ):
        raise CapacityRefreshError("capacity evidence SHA-256 is invalid")
    return CapacityState(
        verified=row["capacity_verified"] is True,
        evidence_sha256=digest,
        attested_at=_aware_utc(
            row["capacity_attested_at"], name="attested", optional=True
        ),
        verified_at=_aware_utc(
            row["capacity_verified_at"], name="verified", optional=True
        ),
        database_now=_aware_utc(row["database_now"], name="database clock")
        or datetime.min.replace(tzinfo=UTC),
    )


def _write_control_file(path: Path, payload: Mapping[str, object]) -> None:
    encoded = canonical_json_bytes(payload) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise CapacityRefreshError("capacity refresh state directory is invalid")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except CapacityRefreshError:
        raise
    except OSError as exc:
        raise CapacityRefreshError("capacity refresh state could not be written") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def prepare_refresh(
    engine: Engine,
    *,
    heartbeat_path: Path,
    request_path: Path,
) -> dict[str, object]:
    baseline = _capacity_state(engine)
    if heartbeat_path.parent != request_path.parent:
        raise CapacityRefreshError("capacity control files must share one directory")
    try:
        if heartbeat_path.is_symlink():
            raise CapacityRefreshError("capacity heartbeat path is a symlink")
        heartbeat_path.unlink(missing_ok=True)
    except CapacityRefreshError:
        raise
    except OSError as exc:
        raise CapacityRefreshError("old capacity heartbeat could not be cleared") from exc
    payload: dict[str, object] = {
        "baseline_evidence_sha256": baseline.evidence_sha256,
        "baseline_verified_at": (
            _timestamp(baseline.verified_at) if baseline.verified_at is not None else None
        ),
        "requested_at": _timestamp(baseline.database_now),
        "schema_version": REQUEST_SCHEMA_VERSION,
    }
    _write_control_file(request_path, payload)
    return payload


def _read_canonical_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise CapacityRefreshError(f"{label} is not a regular file")
        raw = path.read_bytes()
    except OSError as exc:
        raise CapacityRefreshError(f"{label} is unreadable") from exc
    if not 1 <= len(raw) <= MAX_CONTROL_FILE_BYTES:
        raise CapacityRefreshError(f"{label} has an invalid size")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapacityRefreshError(f"{label} is not canonical JSON") from exc
    if not isinstance(value, dict) or raw != canonical_json_bytes(value) + b"\n":
        raise CapacityRefreshError(f"{label} is not canonical JSON")
    return {str(key): item for key, item in value.items()}


def _heartbeat_matches(
    request: Mapping[str, object],
    heartbeat: Mapping[str, object],
    current: CapacityState,
) -> bool:
    if set(request) != {
        "baseline_evidence_sha256",
        "baseline_verified_at",
        "requested_at",
        "schema_version",
    } or request.get("schema_version") != REQUEST_SCHEMA_VERSION:
        raise CapacityRefreshError("capacity refresh request has an invalid schema")
    if set(heartbeat) != {"attested_at", "evidence_sha256", "schema_version"}:
        return False
    if heartbeat.get("schema_version") != ATTESTATION_SCHEMA_VERSION:
        return False
    digest = heartbeat.get("evidence_sha256")
    if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
        return False
    requested_at = _parse_timestamp(request.get("requested_at"), name="request")
    baseline_raw = request.get("baseline_verified_at")
    baseline_at = (
        _parse_timestamp(baseline_raw, name="baseline")
        if baseline_raw is not None
        else None
    )
    try:
        heartbeat_at = datetime.strptime(
            str(heartbeat.get("attested_at")), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC)
    except ValueError:
        return False
    return not (
        not current.verified
        or current.evidence_sha256 != digest
        or current.attested_at is None
        or current.verified_at is None
        or current.verified_at < requested_at
        or (baseline_at is not None and current.verified_at <= baseline_at)
        or current.attested_at.replace(microsecond=0) != heartbeat_at
    )


def wait_for_refresh(
    engine: Engine,
    *,
    heartbeat_path: Path,
    request_path: Path,
    timeout_seconds: int,
) -> str:
    if timeout_seconds < 1 or timeout_seconds > 300:
        raise CapacityRefreshError("capacity refresh timeout is outside 1..300 seconds")
    request = _read_canonical_json(request_path, label="capacity refresh request")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            heartbeat = _read_canonical_json(
                heartbeat_path, label="capacity heartbeat"
            )
            current = _capacity_state(engine)
            if _heartbeat_matches(request, heartbeat, current):
                digest = heartbeat["evidence_sha256"]
                assert isinstance(digest, str)
                return digest
        except (CapacityRefreshError, SQLAlchemyError):
            pass
        time.sleep(0.25)
    raise CapacityRefreshError("fresh exact storage capacity evidence did not arrive")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "wait"))
    parser.add_argument("--heartbeat", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=110)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    database_url = os.environ.get("DATABASE_URL", "")
    try:
        identity = make_url(database_url)
        if (
            identity.drivername != "postgresql+psycopg"
            or identity.username != "custombuild_storage_attestor"
        ):
            raise CapacityRefreshError(
                "capacity refresh requires the fixed storage-attestor database role"
            )
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            if arguments.action == "prepare":
                prepare_refresh(
                    engine,
                    heartbeat_path=arguments.heartbeat,
                    request_path=arguments.request,
                )
            else:
                digest = wait_for_refresh(
                    engine,
                    heartbeat_path=arguments.heartbeat,
                    request_path=arguments.request,
                    timeout_seconds=arguments.timeout_seconds,
                )
                print(digest)
        finally:
            engine.dispose()
    except (CapacityRefreshError, SQLAlchemyError, ValueError) as exc:
        print(f"capacity refresh failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
