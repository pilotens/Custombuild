"""Continuously attest the local Compose volume for non-production writers.

Production must use ``storage_capacity_preflight`` with an operator-supplied,
digest-bound configuration and deploy descriptor. This helper exists only so
the self-contained development/CI Compose stack can derive the capacity of its
ephemeral named volume without treating that value as production evidence.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from scripts.storage_capacity_preflight import (
    OPERATOR_CONFIG_SCHEMA_VERSION,
    CapacityAttestationBusy,
    CapacityPreflightError,
    OperatorConfig,
    _s3_client,
    _write_heartbeat,
    activate_capacity,
    canonical_json_bytes,
    install_capacity_refresh_signal,
    invalidate_capacity,
    wait_for_capacity_refresh,
)

VOLUME_PATH = Path("/storage-volume")
EVIDENCE_DIRECTORY = Path("/run/custombuild-state/evidence")
HEARTBEAT_FILE = Path("/run/custombuild-state/capacity-heartbeat.json")
WATCH_INTERVAL_SECONDS = 300
BUSY_RETRY_SECONDS = 30
DEVELOPMENT_OBJECT_LIMIT = 1_000_000
_EVIDENCE_FILE_PATTERN = re.compile(
    r"storage-capacity-attestation-[0-9a-f]{64}\.json\Z"
)


def _filesystem_capacity(path: Path) -> tuple[int, int]:
    if path.is_symlink() or not path.is_dir():
        raise CapacityPreflightError(
            "development storage volume must be a mounted directory"
        )
    filesystem = os.statvfs(path)
    total_bytes = filesystem.f_frsize * filesystem.f_blocks
    available_bytes = filesystem.f_frsize * filesystem.f_bavail
    if total_bytes <= 2 or not 0 <= available_bytes <= total_bytes:
        raise CapacityPreflightError("development storage volume has no usable capacity")
    return total_bytes, available_bytes


def development_operator_config(
    environment: Mapping[str, str],
    *,
    now: datetime,
    committed_bytes: int,
    volume_path: Path = VOLUME_PATH,
) -> OperatorConfig:
    """Derive a conservative local-only contract from the mounted volume."""

    if isinstance(committed_bytes, bool) or committed_bytes < 0:
        raise CapacityPreflightError("development committed bytes are invalid")
    total_bytes, available_bytes = _filesystem_capacity(volume_path)
    safety_bytes = max(total_bytes // 100, 1)
    emergency_reserve_bytes = max(total_bytes // 100, 1)
    available_growth = available_bytes - emergency_reserve_bytes - safety_bytes
    if available_growth <= 0:
        raise CapacityPreflightError("development storage volume lacks safe free capacity")
    byte_limit = committed_bytes + available_growth
    metadata_overhead_bytes = total_bytes - byte_limit - emergency_reserve_bytes
    if metadata_overhead_bytes <= 0:
        raise CapacityPreflightError(
            "development filesystem usage cannot be reconciled with the ledger"
        )
    headroom_bytes = metadata_overhead_bytes + emergency_reserve_bytes
    if headroom_bytes >= total_bytes:
        raise CapacityPreflightError("development storage headroom consumes the volume")
    volume_identity = environment.get(
        "OBJECT_STORAGE_VOLUME_NAME", "custombuild-development-object-storage"
    )
    bucket = environment.get("S3_BUCKET", "")
    deploy_descriptor_sha256 = hashlib.sha256(
        b"custombuild.development-compose.capacity.v1"
    ).hexdigest()
    requested_at = now.astimezone(UTC).replace(microsecond=0)
    unsigned = {
        "bucket": bucket,
        "byte_limit": byte_limit,
        "deploy_descriptor_sha256": deploy_descriptor_sha256,
        "emergency_reserve_bytes": emergency_reserve_bytes,
        "headroom_bytes": headroom_bytes,
        "metadata_overhead_bytes": metadata_overhead_bytes,
        "object_limit": DEVELOPMENT_OBJECT_LIMIT,
        "provisioned_bytes": total_bytes,
        "requested_at": requested_at.isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "schema_version": OPERATOR_CONFIG_SCHEMA_VERSION,
        "volume_identity": volume_identity,
    }
    digest = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    return OperatorConfig(
        volume_identity=volume_identity,
        provisioned_bytes=total_bytes,
        metadata_overhead_bytes=metadata_overhead_bytes,
        emergency_reserve_bytes=emergency_reserve_bytes,
        headroom_bytes=headroom_bytes,
        byte_limit=byte_limit,
        object_limit=DEVELOPMENT_OBJECT_LIMIT,
        bucket=bucket,
        deploy_descriptor_sha256=deploy_descriptor_sha256,
        requested_at=requested_at,
        sha256=digest,
    )


def _retain_only_current_evidence(directory: Path, current: Path) -> None:
    """Keep the bounded development tmpfs from accumulating refresh records."""

    if (
        current.parent != directory
        or _EVIDENCE_FILE_PATTERN.fullmatch(current.name) is None
        or current.is_symlink()
        or not current.is_file()
    ):
        raise CapacityPreflightError("development capacity evidence path is invalid")
    try:
        for candidate in directory.iterdir():
            if candidate == current or _EVIDENCE_FILE_PATTERN.fullmatch(candidate.name) is None:
                continue
            if candidate.is_symlink() or not candidate.is_file():
                raise CapacityPreflightError(
                    "development capacity evidence directory contains an unsafe entry"
                )
            candidate.unlink()
    except CapacityPreflightError:
        raise
    except OSError as exc:
        raise CapacityPreflightError(
            "development capacity evidence retention failed"
        ) from exc


def main() -> int:
    environment = os.environ
    if environment.get("APP_ENV") not in {"development", "test"}:
        print(
            "development capacity attestor requires explicit development or test mode",
            file=sys.stderr,
        )
        return 1
    if environment.get("S3_ENDPOINT") != "http://object-storage:8333":
        print(
            "development capacity attestor requires the internal Compose S3 endpoint",
            file=sys.stderr,
        )
        return 1
    database_url = environment.get("DATABASE_URL", "")
    try:
        database_identity = make_url(database_url)
    except Exception as exc:
        print(f"development capacity attestor failed: {exc}", file=sys.stderr)
        return 1
    if (
        database_identity.drivername != "postgresql+psycopg"
        or database_identity.username != "custombuild_storage_attestor"
    ):
        print(
            "development capacity attestor requires the fixed storage-attestor "
            "PostgreSQL role",
            file=sys.stderr,
        )
        return 1

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        if engine.dialect.name != "postgresql":
            raise CapacityPreflightError(
                "development capacity activation requires PostgreSQL"
            )
        client = _s3_client(environment)
        EVIDENCE_DIRECTORY.mkdir(mode=0o700, parents=False, exist_ok=True)
        install_capacity_refresh_signal()
        while True:
            try:
                with engine.connect() as connection:
                    committed_bytes = connection.scalar(
                        text(
                            "SELECT committed_bytes FROM storage_global_quotas "
                            "WHERE id = 1"
                        )
                    )
                if isinstance(committed_bytes, bool) or not isinstance(
                    committed_bytes, int
                ):
                    raise CapacityPreflightError(
                        "development committed-byte counter is invalid"
                    )
                config = development_operator_config(
                    environment,
                    now=datetime.now(UTC),
                    committed_bytes=committed_bytes,
                )
                attestation, evidence_path = activate_capacity(
                    engine,
                    config=config,
                    volume_path=VOLUME_PATH,
                    evidence_directory=EVIDENCE_DIRECTORY,
                    s3_client=client,
                    require_fresh_operator_request=False,
                )
                _retain_only_current_evidence(EVIDENCE_DIRECTORY, evidence_path)
                _write_heartbeat(HEARTBEAT_FILE, attestation)
                print(
                    "development storage capacity verified: "
                    f"{attestation['evidence_sha256']} ({evidence_path})",
                    flush=True,
                )
                wait_for_capacity_refresh(WATCH_INTERVAL_SECONDS)
            except CapacityAttestationBusy as exc:
                print(
                    f"development storage capacity refresh deferred: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                wait_for_capacity_refresh(BUSY_RETRY_SECONDS)
            except Exception:
                invalidate_capacity(engine)
                raise
    except (CapacityPreflightError, ValueError) as exc:
        print(f"development capacity attestor failed: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
