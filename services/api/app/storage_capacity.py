"""Exact runtime binding for privileged physical-storage attestations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy.engine import RowMapping

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class StorageCapacitySettings(Protocol):
    @property
    def app_env(self) -> str: ...

    @property
    def s3_bucket(self) -> str: ...

    @property
    def storage_capacity_operator_config_sha256(self) -> str: ...

    @property
    def storage_capacity_volume_identity(self) -> str: ...

    @property
    def storage_capacity_provisioned_bytes(self) -> int: ...

    @property
    def storage_capacity_metadata_overhead_bytes(self) -> int: ...

    @property
    def storage_capacity_emergency_reserve_bytes(self) -> int: ...

    @property
    def storage_capacity_headroom_bytes(self) -> int: ...

    @property
    def storage_capacity_byte_limit(self) -> int: ...

    @property
    def storage_capacity_object_limit(self) -> int: ...

    @property
    def storage_capacity_deploy_descriptor_sha256(self) -> str: ...

    @property
    def storage_capacity_max_age_seconds(self) -> int: ...


def _utc_timestamp(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise RuntimeError(f"storage capacity {name} is missing")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def validate_storage_capacity_evidence(
    settings: StorageCapacitySettings,
    row: Mapping[str, Any] | RowMapping | None,
) -> None:
    """Validate one database row against the exact running configuration."""

    if row is None or row["capacity_verified"] is not True:
        raise RuntimeError("storage capacity is not verified")
    if any(
        row[name] is not None
        for name in (
            "maintenance_token",
            "maintenance_started_at",
            "maintenance_owner_expires_at",
        )
    ):
        raise RuntimeError("storage maintenance is active")
    exact_values: tuple[tuple[str, object], ...] = (
        ("provisioned_bytes", settings.storage_capacity_provisioned_bytes),
        (
            "metadata_overhead_bytes",
            settings.storage_capacity_metadata_overhead_bytes,
        ),
        (
            "emergency_reserve_bytes",
            settings.storage_capacity_emergency_reserve_bytes,
        ),
        ("capacity_headroom_bytes", settings.storage_capacity_headroom_bytes),
        ("byte_limit", settings.storage_capacity_byte_limit),
        ("object_limit", settings.storage_capacity_object_limit),
        ("volume_identity", settings.storage_capacity_volume_identity),
        ("capacity_bucket", settings.s3_bucket),
        (
            "capacity_operator_config_sha256",
            settings.storage_capacity_operator_config_sha256,
        ),
        (
            "deploy_descriptor_sha256",
            settings.storage_capacity_deploy_descriptor_sha256,
        ),
    )
    if any(row[name] != expected for name, expected in exact_values):
        raise RuntimeError("storage capacity evidence does not match runtime settings")
    integer_fields = (
        "reserved_bytes",
        "committed_bytes",
        "reserved_count",
        "committed_count",
        "inventory_object_count",
        "inventory_bytes",
        "ledger_object_count",
        "ledger_bytes",
    )
    counters: dict[str, int] = {}
    for name in integer_fields:
        value = row[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("storage capacity counters are not canonical")
        counters[name] = value
    if (
        counters["inventory_object_count"] != counters["ledger_object_count"]
        or counters["inventory_bytes"] != counters["ledger_bytes"]
        or counters["reserved_bytes"] + counters["committed_bytes"]
        > settings.storage_capacity_byte_limit
        or counters["reserved_count"] + counters["committed_count"]
        > settings.storage_capacity_object_limit
    ):
        raise RuntimeError("storage capacity inventory and counters differ")
    for name in (
        "capacity_operator_config_sha256",
        "deploy_descriptor_sha256",
        "inventory_sha256",
        "capacity_evidence_sha256",
    ):
        value = row[name]
        if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
            raise RuntimeError("storage capacity evidence hash is not canonical")
    database_now = _utc_timestamp(row["database_now"], name="database clock")
    database_started_at = _utc_timestamp(row["database_started_at"], name="database start time")
    recovery_database_started_at = _utc_timestamp(
        row["recovery_database_started_at"], name="recovery database start time"
    )
    recovery_completed_at = _utc_timestamp(
        row["recovery_completed_at"], name="recovery completion time"
    )
    verified_at = _utc_timestamp(row["capacity_verified_at"], name="verification time")
    attested_at = _utc_timestamp(row["capacity_attested_at"], name="attestation time")
    maximum_age = timedelta(seconds=settings.storage_capacity_max_age_seconds)
    maximum_future_skew = timedelta(minutes=1)
    if (
        recovery_database_started_at != database_started_at
        or recovery_completed_at < recovery_database_started_at
        or recovery_completed_at > database_now + maximum_future_skew
        or verified_at < database_now - maximum_age
        or attested_at < database_now - maximum_age
        or verified_at > database_now + maximum_future_skew
        or attested_at > verified_at
    ):
        raise RuntimeError("storage capacity evidence is stale or future-dated")
