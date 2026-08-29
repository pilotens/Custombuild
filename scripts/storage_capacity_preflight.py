"""Physically attest object-storage capacity before production writers start.

The migration deliberately creates an unverified, no-growth quota row.  This
operator-only command inventories the mounted volume, S3 bucket and PostgreSQL
ledger, then activates one exact quota
inside a locked PostgreSQL transaction.  API and worker database roles must not
be able to run this command.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import signal
import sys
from collections.abc import Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, BinaryIO, Never

import boto3
from app.config_guards import validate_production_database_url
from botocore.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

ATTESTATION_SCHEMA_VERSION = "custombuild.storage-capacity.v1"
OPERATOR_CONFIG_SCHEMA_VERSION = "custombuild.storage-capacity-operator.v1"
ATTESTATION_MAX_AGE = timedelta(minutes=10)
MAX_FUTURE_CLOCK_SKEW = timedelta(seconds=30)
MAX_OPERATOR_CONFIG_BYTES = 64 * 1024
MAX_DEPLOY_DESCRIPTOR_BYTES = 16 * 1024 * 1024
MAX_DATABASE_INTEGER = 2**63 - 1
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
VOLUME_IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,254}\Z")
CANONICAL_TEXT_PATTERN = re.compile(r"[^\x00-\x20\x7f\\]+\Z")

OPERATOR_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "volume_identity",
        "provisioned_bytes",
        "metadata_overhead_bytes",
        "emergency_reserve_bytes",
        "headroom_bytes",
        "byte_limit",
        "object_limit",
        "bucket",
        "deploy_descriptor_sha256",
        "requested_at",
    }
)
_REFRESH_REQUESTED = Event()


class CapacityPreflightError(RuntimeError):
    """The target cannot safely activate storage reservations."""


class CapacityAttestationBusy(CapacityPreflightError):
    """A valid in-flight reservation temporarily prevents an exact inventory."""


def install_capacity_refresh_signal() -> None:
    """Wake a running attestor without reloading its verified operator input."""

    def request_refresh(_signum: int, _frame: object) -> None:
        _REFRESH_REQUESTED.set()

    signal.signal(signal.SIGUSR1, request_refresh)


def wait_for_capacity_refresh(timeout_seconds: int) -> None:
    _REFRESH_REQUESTED.wait(timeout_seconds)
    _REFRESH_REQUESTED.clear()


@dataclass(frozen=True, slots=True)
class OperatorConfig:
    volume_identity: str
    provisioned_bytes: int
    metadata_overhead_bytes: int
    emergency_reserve_bytes: int
    headroom_bytes: int
    byte_limit: int
    object_limit: int
    bucket: str
    deploy_descriptor_sha256: str
    requested_at: datetime
    sha256: str


@dataclass(frozen=True, slots=True)
class VolumeObservation:
    device: int
    total_bytes: int
    available_bytes: int


@dataclass(frozen=True, slots=True)
class S3Object:
    key: str
    sha256: str
    size_bytes: int
    media_type: str
    metadata: tuple[tuple[str, str], ...]

    def evidence(self) -> dict[str, object]:
        return {
            "key": self.key,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class LedgerObject:
    organization_id: str
    key: str
    sha256: str
    size_bytes: int
    media_type: str

    def evidence(self) -> dict[str, object]:
        return {
            "organization_id": self.organization_id,
            "key": self.key,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise CapacityPreflightError("capacity evidence is not canonical JSON") from exc


def _reject_constant(value: str) -> Never:
    raise CapacityPreflightError(f"operator config contains forbidden number {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapacityPreflightError(f"operator config repeats field {key!r}")
        result[key] = value
    return result


def _canonical_string(name: str, value: object, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or CANONICAL_TEXT_PATTERN.fullmatch(value) is None
        or value != value.strip()
    ):
        raise CapacityPreflightError(f"{name} is not canonical")
    return value


def _positive_integer(name: str, value: object, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or not minimum <= value <= MAX_DATABASE_INTEGER:
        qualifier = "non-negative" if allow_zero else "positive"
        raise CapacityPreflightError(f"{name} must be a {qualifier} database integer")
    return value


def _canonical_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise CapacityPreflightError("requested_at must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapacityPreflightError("requested_at must be a canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CapacityPreflightError("requested_at must be a canonical UTC timestamp")
    normalized = parsed.astimezone(UTC).replace(microsecond=0)
    if value != normalized.isoformat(timespec="seconds").replace("+00:00", "Z"):
        raise CapacityPreflightError("requested_at must be a canonical UTC timestamp")
    return normalized


def _require_fresh_request(requested_at: datetime, *, now: datetime) -> None:
    canonical_now = now.astimezone(UTC)
    if requested_at > canonical_now + MAX_FUTURE_CLOCK_SKEW:
        raise CapacityPreflightError("operator config requested_at is in the future")
    if canonical_now - requested_at > ATTESTATION_MAX_AGE:
        raise CapacityPreflightError("operator config is stale")


def load_operator_config(
    path: Path,
    *,
    expected_sha256: str,
    now: datetime,
) -> OperatorConfig:
    if SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise CapacityPreflightError("expected operator config SHA-256 is invalid")
    try:
        if path.is_symlink() or not path.is_file():
            raise CapacityPreflightError("operator config must be a regular non-symlink file")
        raw = path.read_bytes()
    except CapacityPreflightError:
        raise
    except OSError as exc:
        raise CapacityPreflightError("operator config could not be read") from exc
    if not raw or len(raw) > MAX_OPERATOR_CONFIG_BYTES:
        raise CapacityPreflightError("operator config has an invalid size")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except CapacityPreflightError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapacityPreflightError("operator config is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CapacityPreflightError("operator config must be a JSON object")
    actual_keys = frozenset(value)
    if actual_keys != OPERATOR_CONFIG_KEYS:
        unknown = sorted(actual_keys - OPERATOR_CONFIG_KEYS)
        missing = sorted(OPERATOR_CONFIG_KEYS - actual_keys)
        detail = []
        if unknown:
            detail.append(f"unknown fields: {', '.join(unknown)}")
        if missing:
            detail.append(f"missing fields: {', '.join(missing)}")
        raise CapacityPreflightError("operator config schema mismatch (" + "; ".join(detail) + ")")
    canonical = canonical_json_bytes(value)
    if raw not in {canonical, canonical + b"\n"}:
        raise CapacityPreflightError("operator config is not canonical JSON")
    digest = hashlib.sha256(canonical).hexdigest()
    if not hmac.compare_digest(digest, expected_sha256):
        raise CapacityPreflightError("operator config SHA-256 does not match")
    if value["schema_version"] != OPERATOR_CONFIG_SCHEMA_VERSION:
        raise CapacityPreflightError("operator config schema_version is unsupported")

    volume_identity = _canonical_string(
        "volume_identity", value["volume_identity"], maximum=255
    )
    if VOLUME_IDENTITY_PATTERN.fullmatch(volume_identity) is None:
        raise CapacityPreflightError("volume_identity is not canonical")
    provisioned_bytes = _positive_integer(
        "provisioned_bytes", value["provisioned_bytes"]
    )
    metadata_overhead_bytes = _positive_integer(
        "metadata_overhead_bytes",
        value["metadata_overhead_bytes"],
    )
    emergency_reserve_bytes = _positive_integer(
        "emergency_reserve_bytes", value["emergency_reserve_bytes"]
    )
    headroom_bytes = _positive_integer("headroom_bytes", value["headroom_bytes"])
    byte_limit = _positive_integer("byte_limit", value["byte_limit"])
    object_limit = _positive_integer("object_limit", value["object_limit"])
    if headroom_bytes != metadata_overhead_bytes + emergency_reserve_bytes:
        raise CapacityPreflightError(
            "headroom_bytes must equal metadata overhead plus emergency reserve"
        )
    if headroom_bytes >= provisioned_bytes:
        raise CapacityPreflightError("provisioned_bytes must exceed headroom_bytes")
    if byte_limit > provisioned_bytes - headroom_bytes:
        raise CapacityPreflightError("byte_limit exceeds physically usable capacity")
    bucket = _canonical_string("bucket", value["bucket"], maximum=63)
    try:
        ipv4_literal = ipaddress.ip_address(bucket).version == 4
    except ValueError:
        ipv4_literal = False
    if (
        BUCKET_PATTERN.fullmatch(bucket) is None
        or ".." in bucket
        or ipv4_literal
    ):
        raise CapacityPreflightError("bucket is not a canonical S3 bucket name")
    deploy_digest = _canonical_string(
        "deploy_descriptor_sha256",
        value["deploy_descriptor_sha256"],
        maximum=64,
    )
    if SHA256_PATTERN.fullmatch(deploy_digest) is None:
        raise CapacityPreflightError("deploy_descriptor_sha256 is invalid")
    requested_at = _canonical_timestamp(value["requested_at"])
    _require_fresh_request(requested_at, now=now)
    return OperatorConfig(
        volume_identity=volume_identity,
        provisioned_bytes=provisioned_bytes,
        metadata_overhead_bytes=metadata_overhead_bytes,
        emergency_reserve_bytes=emergency_reserve_bytes,
        headroom_bytes=headroom_bytes,
        byte_limit=byte_limit,
        object_limit=object_limit,
        bucket=bucket,
        deploy_descriptor_sha256=deploy_digest,
        requested_at=requested_at,
        sha256=digest,
    )


def validate_expected_environment(
    config: OperatorConfig,
    environment: Mapping[str, str],
) -> None:
    expected = {
        "STORAGE_CAPACITY_OPERATOR_CONFIG_SHA256": config.sha256,
        "STORAGE_CAPACITY_VOLUME_IDENTITY": config.volume_identity,
        "OBJECT_STORAGE_VOLUME_NAME": config.volume_identity,
        "STORAGE_CAPACITY_PROVISIONED_BYTES": str(config.provisioned_bytes),
        "STORAGE_CAPACITY_METADATA_OVERHEAD_BYTES": str(
            config.metadata_overhead_bytes
        ),
        "STORAGE_CAPACITY_EMERGENCY_RESERVE_BYTES": str(
            config.emergency_reserve_bytes
        ),
        "STORAGE_CAPACITY_HEADROOM_BYTES": str(config.headroom_bytes),
        "STORAGE_CAPACITY_BYTE_LIMIT": str(config.byte_limit),
        "STORAGE_CAPACITY_OBJECT_LIMIT": str(config.object_limit),
        "STORAGE_CAPACITY_DEPLOY_DESCRIPTOR_SHA256": (
            config.deploy_descriptor_sha256
        ),
        "STORAGE_CAPACITY_MAX_AGE_SECONDS": "600",
        "S3_BUCKET": config.bucket,
    }
    for name, exact_value in expected.items():
        if environment.get(name) != exact_value:
            raise CapacityPreflightError(
                f"operator config does not match explicit environment field {name}"
            )


def verify_deploy_descriptor(path: Path, expected_sha256: str) -> None:
    try:
        if path.is_symlink() or not path.is_file():
            raise CapacityPreflightError(
                "deploy descriptor must be a regular non-symlink file"
            )
        size = path.stat().st_size
        if size <= 0 or size > MAX_DEPLOY_DESCRIPTOR_BYTES:
            raise CapacityPreflightError("deploy descriptor has an invalid size")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except CapacityPreflightError:
        raise
    except OSError as exc:
        raise CapacityPreflightError("deploy descriptor could not be read") from exc
    if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
        raise CapacityPreflightError("deploy descriptor SHA-256 does not match")


def observe_volume(path: Path, *, provisioned_bytes: int) -> VolumeObservation:
    try:
        if path.is_symlink() or not path.is_dir():
            raise CapacityPreflightError("storage volume path must be a mounted directory")
        stat = path.stat()
        filesystem = os.statvfs(path)
    except CapacityPreflightError:
        raise
    except OSError as exc:
        raise CapacityPreflightError("storage volume could not be inspected") from exc
    total_bytes = filesystem.f_frsize * filesystem.f_blocks
    available_bytes = filesystem.f_frsize * filesystem.f_bavail
    if total_bytes != provisioned_bytes:
        raise CapacityPreflightError(
            "mounted filesystem size does not equal operator-attested provisioned_bytes"
        )
    if not 0 <= available_bytes <= total_bytes:
        raise CapacityPreflightError("mounted filesystem returned invalid capacity")
    return VolumeObservation(
        device=stat.st_dev,
        total_bytes=total_bytes,
        available_bytes=available_bytes,
    )


def _key_label(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:16]


def _read_body(body: BinaryIO, *, expected_size: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    actual_size = 0
    while True:
        chunk = body.read(1024 * 1024)
        if not isinstance(chunk, bytes):
            raise CapacityPreflightError("S3 returned a non-byte object body")
        if not chunk:
            break
        actual_size += len(chunk)
        if actual_size > expected_size:
            raise CapacityPreflightError("S3 object grew while it was inventoried")
        digest.update(chunk)
    return digest.hexdigest(), actual_size


def inventory_s3(client: Any, bucket: str) -> tuple[S3Object, ...]:
    try:
        client.head_bucket(Bucket=bucket)
    except Exception as exc:
        raise CapacityPreflightError("S3 bucket could not be verified") from exc
    result: list[S3Object] = []
    keys: set[str] = set()
    continuation: str | None = None
    seen_tokens: set[str] = set()
    while True:
        parameters: dict[str, str] = {"Bucket": bucket}
        if continuation is not None:
            parameters["ContinuationToken"] = continuation
        try:
            page = client.list_objects_v2(**parameters)
        except Exception as exc:
            raise CapacityPreflightError("S3 inventory could not be listed") from exc
        if not isinstance(page, Mapping):
            raise CapacityPreflightError("S3 inventory returned an invalid page")
        contents = page.get("Contents", [])
        if not isinstance(contents, Sequence) or isinstance(contents, str | bytes):
            raise CapacityPreflightError("S3 inventory returned invalid contents")
        for listed in contents:
            if not isinstance(listed, Mapping):
                raise CapacityPreflightError("S3 inventory returned an invalid object")
            key = _canonical_string("S3 object key", listed.get("Key"), maximum=512)
            listed_size = _positive_integer("S3 object size", listed.get("Size"))
            if key in keys:
                raise CapacityPreflightError("S3 inventory contains a duplicate key")
            keys.add(key)
            try:
                response = client.get_object(Bucket=bucket, Key=key)
            except Exception as exc:
                raise CapacityPreflightError(
                    f"S3 object {_key_label(key)} could not be read"
                ) from exc
            if not isinstance(response, Mapping):
                raise CapacityPreflightError("S3 returned an invalid object response")
            body = response.get("Body")
            if body is None or not callable(getattr(body, "read", None)):
                raise CapacityPreflightError("S3 returned an invalid object body")
            with closing(body):
                digest, actual_size = _read_body(body, expected_size=listed_size)
            if actual_size != listed_size:
                raise CapacityPreflightError(
                    f"S3 object {_key_label(key)} changed during inventory"
                )
            content_length = response.get("ContentLength")
            if type(content_length) is not int or content_length != actual_size:
                raise CapacityPreflightError(
                    f"S3 object {_key_label(key)} has inconsistent size metadata"
                )
            media_type = _canonical_string(
                "S3 object media type", response.get("ContentType"), maximum=160
            )
            raw_metadata = response.get("Metadata")
            if not isinstance(raw_metadata, Mapping) or any(
                not isinstance(name, str) or not isinstance(value, str)
                for name, value in raw_metadata.items()
            ):
                raise CapacityPreflightError(
                    f"S3 object {_key_label(key)} has invalid immutable metadata"
                )
            metadata = {
                _canonical_string("S3 metadata name", name, maximum=64).lower(): (
                    _canonical_string("S3 metadata value", value, maximum=2048)
                )
                for name, value in raw_metadata.items()
            }
            if len(metadata) != len(raw_metadata):
                raise CapacityPreflightError(
                    f"S3 object {_key_label(key)} has duplicate normalized metadata"
                )
            metadata_digest = metadata.get("sha256", "")
            if metadata.get("immutable") != "true" or not hmac.compare_digest(
                metadata_digest, digest
            ):
                raise CapacityPreflightError(
                    f"S3 object {_key_label(key)} has mismatched immutable metadata"
                )
            result.append(
                S3Object(
                    key=key,
                    sha256=digest,
                    size_bytes=actual_size,
                    media_type=media_type,
                    metadata=tuple(sorted(metadata.items())),
                )
            )
        truncated = page.get("IsTruncated", False)
        if truncated is False:
            break
        if truncated is not True:
            raise CapacityPreflightError("S3 inventory returned invalid pagination state")
        raw_token = page.get("NextContinuationToken")
        continuation = _canonical_string(
            "S3 continuation token", raw_token, maximum=4096
        )
        if continuation in seen_tokens:
            raise CapacityPreflightError("S3 inventory repeated a continuation token")
        seen_tokens.add(continuation)
    return tuple(sorted(result, key=lambda item: item.key))


def _row_integer(name: str, value: object, *, allow_zero: bool = True) -> int:
    return _positive_integer(name, value, allow_zero=allow_zero)


def _database_clock(connection: Connection) -> datetime:
    value = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
    if not isinstance(value, datetime):
        raise CapacityPreflightError("PostgreSQL returned an invalid clock timestamp")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def load_locked_ledger(
    connection: Connection,
    *,
    capacity_bucket: str | None = None,
) -> tuple[Mapping[str, Any], tuple[LedgerObject, ...], datetime]:
    # The login role has no table mutation privileges. This SECURITY DEFINER
    # call takes the same global row fence used by every storage writer and
    # holds it for this transaction, after which plain SELECT is sufficient.
    connection.execute(text("SELECT public.custombuild_storage_lock_capacity()"))
    global_row = connection.execute(
        text(
            "SELECT *, clock_timestamp() AS database_now "
            "FROM storage_global_quotas WHERE id = 1"
        )
    ).mappings().one_or_none()
    if global_row is None:
        raise CapacityPreflightError("global storage quota singleton is missing")
    database_now = global_row["database_now"]
    if not isinstance(database_now, datetime):
        raise CapacityPreflightError("PostgreSQL returned an invalid clock timestamp")
    if database_now.tzinfo is None:
        database_now = database_now.replace(tzinfo=UTC)
    else:
        database_now = database_now.astimezone(UTC)
    if _row_integer("global reserved_bytes", global_row["reserved_bytes"]) != 0 or (
        _row_integer("global reserved_count", global_row["reserved_count"]) != 0
    ):
        raise CapacityAttestationBusy(
            "active storage reservations temporarily block capacity attestation"
        )

    ledger: list[LedgerObject] = []
    globally_unique_keys: set[str] = set()
    per_tenant: dict[str, tuple[int, int]] = {}
    organization_ids = tuple(
        _canonical_string("organization id", value, maximum=36)
        for value in connection.execute(
            text("SELECT id FROM organizations ORDER BY id")
        ).scalars()
    )
    if len(set(organization_ids)) != len(organization_ids):
        raise CapacityPreflightError("organizations contains a duplicate identity")
    try:
        for organization_id in organization_ids:
            connection.execute(
                text(
                    "SELECT set_config("
                    "'app.current_organization_id', :organization_id, true)"
                ),
                {"organization_id": organization_id},
            )
            if capacity_bucket is not None:
                overlap = (
                    connection.execute(
                        text(
                            "SELECT stored.object_key AS overlap_key"
                            " FROM stored_objects AS stored"
                            " JOIN storage_object_tombstones AS tombstone"
                            "   ON tombstone.capacity_bucket = :capacity_bucket"
                            "  AND (tombstone.object_key = stored.object_key"
                            "       OR tombstone.idempotency_key = stored.idempotency_key)"
                            " WHERE stored.organization_id = :organization_id LIMIT 1"
                        ),
                        {
                            "capacity_bucket": capacity_bucket,
                            "organization_id": organization_id,
                        },
                    )
                    .mappings()
                    .one_or_none()
                )
                if overlap is not None:
                    raise CapacityPreflightError(
                        "live storage ledger overlaps a permanently retired storage identity"
                    )
            rows = connection.execute(
                text(
                    "SELECT organization_id, object_key, sha256, size_bytes, "
                    "media_type, state FROM stored_objects "
                    "WHERE organization_id = :organization_id "
                    "ORDER BY object_key"
                ),
                {"organization_id": organization_id},
            ).mappings()
            for row in rows:
                row_organization_id = _canonical_string(
                    "ledger organization_id", row["organization_id"], maximum=36
                )
                if row_organization_id != organization_id:
                    raise CapacityPreflightError(
                        "tenant ledger query crossed its RLS organization context"
                    )
                key = _canonical_string(
                    "ledger object_key", row["object_key"], maximum=512
                )
                if key in globally_unique_keys:
                    raise CapacityPreflightError(
                        "ledger reuses one physical S3 key across multiple rows"
                    )
                globally_unique_keys.add(key)
                digest = _canonical_string(
                    "ledger sha256", row["sha256"], maximum=64
                )
                if SHA256_PATTERN.fullmatch(digest) is None:
                    raise CapacityPreflightError(
                        "ledger contains a non-canonical SHA-256"
                    )
                size_bytes = _row_integer(
                    "ledger size_bytes", row["size_bytes"], allow_zero=False
                )
                media_type = _canonical_string(
                    "ledger media_type", row["media_type"], maximum=160
                )
                if row["state"] != "committed":
                    raise CapacityAttestationBusy(
                        "non-committed ledger objects block capacity attestation"
                    )
                ledger.append(
                    LedgerObject(
                        organization_id=row_organization_id,
                        key=key,
                        sha256=digest,
                        size_bytes=size_bytes,
                        media_type=media_type,
                    )
                )
                tenant_bytes, tenant_count = per_tenant.get(
                    organization_id, (0, 0)
                )
                per_tenant[organization_id] = (
                    tenant_bytes + size_bytes,
                    tenant_count + 1,
                )

            tenant_row = connection.execute(
                text(
                    "SELECT organization_id, reserved_bytes, committed_bytes, "
                    "reserved_count, committed_count FROM storage_tenant_quotas "
                    "WHERE organization_id = :organization_id"
                ),
                {"organization_id": organization_id},
            ).mappings().one_or_none()
            expected_bytes, expected_count = per_tenant.get(
                organization_id, (0, 0)
            )
            if tenant_row is None:
                if expected_count:
                    raise CapacityPreflightError(
                        "ledger object has no tenant quota counter"
                    )
                continue
            if tenant_row["organization_id"] != organization_id:
                raise CapacityPreflightError(
                    "tenant quota query crossed its RLS organization context"
                )
            if (
                _row_integer(
                    "tenant reserved_bytes", tenant_row["reserved_bytes"]
                )
                != 0
                or _row_integer(
                    "tenant reserved_count", tenant_row["reserved_count"]
                )
                != 0
                or _row_integer(
                    "tenant committed_bytes", tenant_row["committed_bytes"]
                )
                != expected_bytes
                or _row_integer(
                    "tenant committed_count", tenant_row["committed_count"]
                )
                != expected_count
            ):
                raise CapacityPreflightError(
                    "tenant quota counters do not match the ledger"
                )
    finally:
        connection.execute(
            text("SELECT set_config('app.current_organization_id', '', true)")
        )

    ledger_bytes = sum(item.size_bytes for item in ledger)
    ledger_count = len(ledger)
    if (
        _row_integer("global committed_bytes", global_row["committed_bytes"])
        != ledger_bytes
        or _row_integer("global committed_count", global_row["committed_count"])
        != ledger_count
    ):
        raise CapacityPreflightError("global quota counters do not match the ledger")
    return dict(global_row), tuple(ledger), database_now


def verify_inventory_matches_ledger(
    ledger: Sequence[LedgerObject],
    inventory: Sequence[S3Object],
) -> None:
    by_key = {item.key: item for item in inventory}
    if len(by_key) != len(inventory):
        raise CapacityPreflightError("S3 inventory contains duplicate keys")
    ledger_by_key = {item.key: item for item in ledger}
    if len(ledger_by_key) != len(ledger):
        raise CapacityPreflightError("ledger contains duplicate physical keys")
    unknown = set(by_key) - set(ledger_by_key)
    missing = set(ledger_by_key) - set(by_key)
    if unknown:
        raise CapacityPreflightError("S3 bucket contains unknown keys")
    if missing:
        raise CapacityPreflightError("ledger contains objects missing from S3")
    for key, ledger_item in ledger_by_key.items():
        stored = by_key[key]
        if (
            stored.sha256 != ledger_item.sha256
            or stored.size_bytes != ledger_item.size_bytes
            or stored.media_type != ledger_item.media_type
        ):
            raise CapacityPreflightError(
                f"S3 object {_key_label(key)} does not match its immutable ledger identity"
            )
    if sum(item.size_bytes for item in inventory) != sum(
        item.size_bytes for item in ledger
    ) or len(inventory) != len(ledger):
        raise CapacityPreflightError("S3 and database inventory totals do not match")


def build_attestation(
    *,
    config: OperatorConfig,
    volume: VolumeObservation,
    inventory: Sequence[S3Object],
    ledger: Sequence[LedgerObject],
    attested_at: datetime,
) -> dict[str, object]:
    inventory_evidence = [item.evidence() for item in inventory]
    ledger_evidence = [item.evidence() for item in ledger]
    unsigned: dict[str, object] = {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "operator_config_sha256": config.sha256,
        "operator_requested_at": config.requested_at.isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "attested_at": attested_at.astimezone(UTC).replace(microsecond=0).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "volume": {
            "identity": config.volume_identity,
            "device": volume.device,
            "provisioned_bytes": config.provisioned_bytes,
            "observed_total_bytes": volume.total_bytes,
            "observed_available_bytes": volume.available_bytes,
            "metadata_overhead_bytes": config.metadata_overhead_bytes,
            "emergency_reserve_bytes": config.emergency_reserve_bytes,
            "headroom_bytes": config.headroom_bytes,
        },
        "limits": {
            "byte_limit": config.byte_limit,
            "object_limit": config.object_limit,
        },
        "bucket": config.bucket,
        "deploy_descriptor_sha256": config.deploy_descriptor_sha256,
        "s3_inventory": {
            "sha256": hashlib.sha256(
                canonical_json_bytes(inventory_evidence)
            ).hexdigest(),
            "object_count": len(inventory),
            "bytes": sum(item.size_bytes for item in inventory),
        },
        "db_ledger": {
            "sha256": hashlib.sha256(
                canonical_json_bytes(ledger_evidence)
            ).hexdigest(),
            "object_count": len(ledger),
            "bytes": sum(item.size_bytes for item in ledger),
        },
    }
    evidence_sha256 = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    return {**unsigned, "evidence_sha256": evidence_sha256}


def _write_evidence(directory: Path, attestation: Mapping[str, object]) -> Path:
    digest = attestation.get("evidence_sha256")
    if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
        raise CapacityPreflightError("capacity evidence SHA-256 is invalid")
    try:
        if directory.is_symlink() or not directory.is_dir():
            raise CapacityPreflightError(
                "capacity evidence directory must be a mounted directory"
            )
        target = directory / f"storage-capacity-attestation-{digest}.json"
        payload = canonical_json_bytes(attestation) + b"\n"
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        return target
    except FileExistsError:
        try:
            if target.read_bytes() == payload:
                return target
        except OSError as exc:
            raise CapacityPreflightError("existing capacity evidence could not be read") from exc
        raise CapacityPreflightError("capacity evidence filename collision") from None
    except CapacityPreflightError:
        raise
    except OSError as exc:
        raise CapacityPreflightError("capacity evidence could not be persisted") from exc


def invalidate_capacity(engine: Engine) -> None:
    failure_evidence_sha256 = hashlib.sha256(
        b"custombuild.storage-capacity.invalidated.v1"
    ).hexdigest()
    with engine.begin() as connection:
        connection.execute(
            text(
                "SELECT public.custombuild_storage_invalidate_capacity("
                ":failure_evidence_sha256)"
            ),
            {"failure_evidence_sha256": failure_evidence_sha256},
        )


def activate_capacity(
    engine: Engine,
    *,
    config: OperatorConfig,
    volume_path: Path,
    evidence_directory: Path,
    s3_client: Any,
    require_fresh_operator_request: bool = True,
) -> tuple[dict[str, object], Path]:
    # S3 bodies can be large and slow. Inventory them before taking the global
    # quota row lock; the short database transaction below re-reads the entire
    # ledger and rejects any object/counter drift before activating evidence.
    inventory = inventory_s3(s3_client, config.bucket)
    volume = observe_volume(
        volume_path,
        provisioned_bytes=config.provisioned_bytes,
    )
    with engine.begin() as connection:
        _global_row, ledger, database_now = load_locked_ledger(
            connection,
            capacity_bucket=config.bucket,
        )
        if require_fresh_operator_request:
            _require_fresh_request(config.requested_at, now=database_now)
        verify_inventory_matches_ledger(ledger, inventory)
        required_available = (
            config.byte_limit
            - sum(item.size_bytes for item in ledger)
            + config.emergency_reserve_bytes
        )
        if volume.available_bytes < required_available:
            raise CapacityPreflightError(
                "mounted filesystem lacks quota growth plus emergency reserve"
            )
        completed_at = _database_clock(connection)
        attestation = build_attestation(
            config=config,
            volume=volume,
            inventory=inventory,
            ledger=ledger,
            attested_at=completed_at,
        )
        evidence_path = _write_evidence(evidence_directory, attestation)
        inventory_evidence = attestation["s3_inventory"]
        ledger_evidence = attestation["db_ledger"]
        if not isinstance(inventory_evidence, Mapping) or not isinstance(
            ledger_evidence, Mapping
        ):
            raise CapacityPreflightError("capacity attestation totals are invalid")
        connection.execute(
            text(
                "SELECT public.custombuild_storage_attest_capacity("
                ":provisioned_bytes, :metadata_overhead_bytes, "
                ":emergency_reserve_bytes, :byte_limit, :object_limit, "
                ":volume_identity, :capacity_bucket, :operator_config_sha256, "
                ":deploy_descriptor_sha256, :inventory_sha256, "
                ":inventory_object_count, :inventory_bytes, "
                ":ledger_object_count, :ledger_bytes, :attested_at, "
                ":evidence_sha256)"
            ),
            {
                "byte_limit": config.byte_limit,
                "object_limit": config.object_limit,
                "provisioned_bytes": config.provisioned_bytes,
                "metadata_overhead_bytes": config.metadata_overhead_bytes,
                "emergency_reserve_bytes": config.emergency_reserve_bytes,
                "volume_identity": config.volume_identity,
                "capacity_bucket": config.bucket,
                "operator_config_sha256": config.sha256,
                "deploy_descriptor_sha256": config.deploy_descriptor_sha256,
                "inventory_sha256": inventory_evidence["sha256"],
                "inventory_object_count": inventory_evidence["object_count"],
                "inventory_bytes": inventory_evidence["bytes"],
                "ledger_object_count": ledger_evidence["object_count"],
                "ledger_bytes": ledger_evidence["bytes"],
                "attested_at": completed_at,
                "evidence_sha256": attestation["evidence_sha256"],
            },
        )
    return attestation, evidence_path


def _write_heartbeat(path: Path, attestation: Mapping[str, object]) -> None:
    digest = attestation.get("evidence_sha256")
    attested_at = attestation.get("attested_at")
    if (
        not isinstance(digest, str)
        or SHA256_PATTERN.fullmatch(digest) is None
        or not isinstance(attested_at, str)
    ):
        raise CapacityPreflightError("capacity heartbeat evidence is invalid")
    payload = canonical_json_bytes(
        {
            "attested_at": attested_at,
            "evidence_sha256": digest,
            "schema_version": ATTESTATION_SCHEMA_VERSION,
        }
    ) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise CapacityPreflightError(
                "capacity heartbeat parent must be a mounted directory"
            )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except CapacityPreflightError:
        raise
    except OSError as exc:
        raise CapacityPreflightError("capacity heartbeat could not be persisted") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _watch_interval(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("watch interval must be an integer") from exc
    if seconds != 0 and not 60 <= seconds <= 300:
        raise argparse.ArgumentTypeError(
            "watch interval must be zero or between 60 and 300 seconds"
        )
    return seconds


def _s3_client(environment: Mapping[str, str]) -> Any:
    required = ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY")
    if any(not environment.get(name) for name in required):
        raise CapacityPreflightError("S3 endpoint and credentials must be explicit")
    return boto3.client(
        "s3",
        endpoint_url=environment["S3_ENDPOINT"],
        aws_access_key_id=environment["S3_ACCESS_KEY"],
        aws_secret_access_key=environment["S3_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=60,
            retries={"total_max_attempts": 3, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-config", type=Path, required=True)
    parser.add_argument("--deploy-descriptor", type=Path, required=True)
    parser.add_argument("--volume-path", type=Path, required=True)
    parser.add_argument("--evidence-directory", type=Path, required=True)
    parser.add_argument(
        "--watch-interval-seconds",
        type=_watch_interval,
        default=0,
        help="Continuously re-attest; production uses 300 seconds",
    )
    parser.add_argument("--heartbeat-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    environment = os.environ
    try:
        if (args.watch_interval_seconds > 0) != (args.heartbeat_file is not None):
            raise CapacityPreflightError(
                "watch mode and --heartbeat-file must be configured together"
            )
        expected_config_sha256 = environment.get(
            "STORAGE_CAPACITY_OPERATOR_CONFIG_SHA256", ""
        )
        config = load_operator_config(
            args.operator_config,
            expected_sha256=expected_config_sha256,
            now=datetime.now(UTC),
        )
        validate_expected_environment(config, environment)
        verify_deploy_descriptor(
            args.deploy_descriptor,
            config.deploy_descriptor_sha256,
        )
        database_url = environment.get("DATABASE_URL", "")
        database_username = make_url(database_url).username
        if database_username != "custombuild_storage_attestor":
            raise CapacityPreflightError(
                "capacity database URL must use the fixed storage-attestor role"
            )
        validate_production_database_url(
            database_url,
            expected_username=database_username,
            setting_name="CAPACITY_ATTESTOR_DATABASE_URL",
        )
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            if engine.dialect.name != "postgresql":
                raise CapacityPreflightError(
                    "capacity activation requires PostgreSQL"
                )
            client = _s3_client(environment)
            first_activation = True
            if args.watch_interval_seconds > 0:
                install_capacity_refresh_signal()
            while True:
                try:
                    attestation, evidence_path = activate_capacity(
                        engine,
                        config=config,
                        volume_path=args.volume_path,
                        evidence_directory=args.evidence_directory,
                        s3_client=client,
                        require_fresh_operator_request=first_activation,
                    )
                    first_activation = False
                    if args.heartbeat_file is not None:
                        _write_heartbeat(args.heartbeat_file, attestation)
                    print(
                        json.dumps(
                            {
                                "capacity_verified": True,
                                "evidence_sha256": attestation["evidence_sha256"],
                                "evidence_path": str(evidence_path),
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                    if args.watch_interval_seconds == 0:
                        break
                    wait_for_capacity_refresh(args.watch_interval_seconds)
                except CapacityAttestationBusy as exc:
                    if args.watch_interval_seconds == 0:
                        raise
                    print(
                        f"storage capacity refresh deferred: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    wait_for_capacity_refresh(min(30, args.watch_interval_seconds))
                    continue
                except Exception:
                    # A failed refresh must not leave a previous attestation
                    # looking current until its freshness window expires.
                    invalidate_capacity(engine)
                    raise
        finally:
            engine.dispose()
    except (CapacityPreflightError, ValueError) as exc:
        print(f"storage capacity preflight failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
