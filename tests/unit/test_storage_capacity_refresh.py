from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.storage_capacity_preflight import ATTESTATION_SCHEMA_VERSION
from scripts.storage_capacity_refresh import (
    REQUEST_SCHEMA_VERSION,
    CapacityRefreshError,
    CapacityState,
    _heartbeat_matches,
    _read_canonical_json,
    _write_control_file,
)


def state(
    *,
    digest: str = "a" * 64,
    attested_at: datetime | None = None,
    verified_at: datetime | None = None,
    verified: bool = True,
) -> CapacityState:
    now = datetime(2026, 8, 28, 12, 0, 2, tzinfo=UTC)
    return CapacityState(
        verified=verified,
        evidence_sha256=digest,
        attested_at=attested_at or now,
        verified_at=verified_at or now,
        database_now=now,
    )


def request() -> dict[str, object]:
    return {
        "baseline_evidence_sha256": "9" * 64,
        "baseline_verified_at": "2026-08-28T12:00:00.000000Z",
        "requested_at": "2026-08-28T12:00:01.000000Z",
        "schema_version": REQUEST_SCHEMA_VERSION,
    }


def heartbeat() -> dict[str, object]:
    return {
        "attested_at": "2026-08-28T12:00:02Z",
        "evidence_sha256": "a" * 64,
        "schema_version": ATTESTATION_SCHEMA_VERSION,
    }


def test_exact_new_database_bound_heartbeat_matches() -> None:
    assert _heartbeat_matches(request(), heartbeat(), state())


@pytest.mark.parametrize(
    "current",
    (
        state(verified=False),
        state(digest="b" * 64),
        state(verified_at=datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)),
        state(attested_at=datetime(2026, 8, 28, 12, 0, 1, tzinfo=UTC)),
    ),
)
def test_stale_or_unbound_database_evidence_is_rejected(
    current: CapacityState,
) -> None:
    assert not _heartbeat_matches(request(), heartbeat(), current)


def test_in_flight_pre_request_heartbeat_is_rejected() -> None:
    current = state(
        verified_at=datetime(2026, 8, 28, 12, 0, 0, 500_000, tzinfo=UTC)
    )

    assert not _heartbeat_matches(request(), heartbeat(), current)


def test_control_files_must_be_canonical_and_regular(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    _write_control_file(path, request())

    assert _read_canonical_json(path, label="request") == request()

    path.write_text('{"schema_version": "non-canonical"}\n', encoding="utf-8")
    with pytest.raises(CapacityRefreshError, match="canonical JSON"):
        _read_canonical_json(path, label="request")


def test_heartbeat_requires_exact_schema() -> None:
    extra = {**heartbeat(), "unexpected": True}
    wrong_version = {**heartbeat(), "schema_version": "wrong"}

    assert not _heartbeat_matches(request(), extra, state())
    assert not _heartbeat_matches(request(), wrong_version, state())


def test_verified_timestamp_must_advance_beyond_baseline() -> None:
    baseline = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)

    assert not _heartbeat_matches(
        request(),
        heartbeat(),
        state(verified_at=baseline),
    )
    assert _heartbeat_matches(
        request(),
        heartbeat(),
        state(verified_at=baseline + timedelta(seconds=2)),
    )
