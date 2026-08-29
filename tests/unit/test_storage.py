from __future__ import annotations

import hashlib
import socket
from dataclasses import replace
from io import BytesIO
from threading import Event
from time import monotonic
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from app import storage
from botocore.exceptions import ClientError, ReadTimeoutError
from fastapi import HTTPException


class RecordingBody(BytesIO):
    def __init__(self, payload: bytes, *, close_error: OSError | None = None) -> None:
        super().__init__(payload)
        self.close_calls = 0
        self.close_error = close_error
        self.read_sizes: list[int] = []

    def read(self, size: int | None = -1) -> bytes:
        self.read_sizes.append(-1 if size is None else size)
        return super().read(size)

    def close(self) -> None:
        self.close_calls += 1
        super().close()
        if self.close_error is not None:
            raise self.close_error


class RecordingSpool(BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class FailingReadSpool(RecordingSpool):
    def __init__(self) -> None:
        super().__init__()
        self.fail_reads = False

    def read(self, size: int | None = -1) -> bytes:
        if self.fail_reads:
            raise OSError("private temporary-file details")
        return super().read(size)


class VerifyingS3Client:
    def __init__(
        self,
        payload: bytes,
        *,
        content_type: str = "application/json",
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.content_type = content_type
        self.metadata = metadata or {}
        self.head_calls = 0
        self.get_calls = 0
        self.last_body: RecordingBody | None = None
        self.body_close_error: OSError | None = None
        self.head_error: Exception | None = None
        self.get_error: Exception | None = None
        self.put_calls: list[dict[str, Any]] = []
        self.put_error: Exception | None = None

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append(kwargs)
        if self.put_error is not None:
            raise self.put_error
        return {"ETag": '"immutable"'}

    def head_object(self, **_kwargs: Any) -> dict[str, Any]:
        self.head_calls += 1
        if self.head_error is not None:
            raise self.head_error
        return {
            "ContentLength": len(self.payload),
            "ContentType": self.content_type,
            "Metadata": self.metadata,
        }

    def get_object(self, **_kwargs: Any) -> dict[str, Any]:
        self.get_calls += 1
        if self.get_error is not None:
            raise self.get_error
        self.last_body = RecordingBody(
            self.payload,
            close_error=self.body_close_error,
        )
        return {
            "ContentLength": len(self.payload),
            "ContentType": self.content_type,
            "Metadata": self.metadata,
            "Body": self.last_body,
        }


def _expectation(payload: bytes) -> storage.StoredObjectExpectation:
    return storage.StoredObjectExpectation(
        object_key="private/org/evidence.json",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        content_type="application/json",
    )


def _install_verification_client(
    monkeypatch: pytest.MonkeyPatch, client: VerifyingS3Client
) -> None:
    monkeypatch.setattr(storage, "internal_s3_client", lambda: client)
    monkeypatch.setattr(
        storage,
        "get_settings",
        lambda: SimpleNamespace(s3_bucket="private-artifacts"),
    )


def _install_spool_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[RecordingSpool], list[tuple[int, str]]]:
    spools: list[RecordingSpool] = []
    calls: list[tuple[int, str]] = []

    def create_spool(*, max_size: int, mode: str) -> RecordingSpool:
        calls.append((max_size, mode))
        spool = RecordingSpool()
        spools.append(spool)
        return spool

    monkeypatch.setattr(storage, "SpooledTemporaryFile", create_spool)
    return spools, calls


def _install_artifact_signing_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ttl_seconds: int = 300,
) -> None:
    monkeypatch.setattr(
        storage,
        "get_settings",
        lambda: SimpleNamespace(
            artifact_signing_secret="unit-test-artifact-signing-secret",  # noqa: S106
            artifact_url_ttl_seconds=ttl_seconds,
        ),
    )


def test_artifact_access_accepts_the_exact_configured_ttl_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 2_000_000_000
    ttl_seconds = 300
    _install_artifact_signing_settings(monkeypatch, ttl_seconds=ttl_seconds)
    monkeypatch.setattr(storage.time, "time", lambda: float(now))
    expires_at = now + ttl_seconds
    signature = storage.sign_artifact_access("artifact", "organization", expires_at)

    storage.verify_artifact_access("artifact", "organization", expires_at, signature)


@pytest.mark.parametrize("expires_at", (2_000_000_000, 1_999_999_999))
def test_artifact_access_rejects_expired_or_current_second_links(
    monkeypatch: pytest.MonkeyPatch,
    expires_at: int,
) -> None:
    now = 2_000_000_000
    _install_artifact_signing_settings(monkeypatch)
    monkeypatch.setattr(storage.time, "time", lambda: float(now))
    signature = storage.sign_artifact_access("artifact", "organization", expires_at)

    with pytest.raises(HTTPException) as exc_info:
        storage.verify_artifact_access("artifact", "organization", expires_at, signature)

    assert exc_info.value.status_code == 410
    assert exc_info.value.detail == "Artifact link expired"


def test_artifact_access_rejects_a_valid_signature_beyond_the_configured_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 2_000_000_000
    ttl_seconds = 300
    _install_artifact_signing_settings(monkeypatch, ttl_seconds=ttl_seconds)
    monkeypatch.setattr(storage.time, "time", lambda: float(now))
    expires_at = now + ttl_seconds + 1
    signature = storage.sign_artifact_access("artifact", "organization", expires_at)

    with pytest.raises(HTTPException) as exc_info:
        storage.verify_artifact_access("artifact", "organization", expires_at, signature)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Invalid artifact expiry"


@pytest.mark.parametrize(
    "signature",
    ("é" * 64, "A" * 64, "a" * 63, "a" * 65, "a" * 63 + "g", None, True),
)
def test_artifact_access_rejects_noncanonical_signature_without_type_error(
    monkeypatch: pytest.MonkeyPatch,
    signature: object,
) -> None:
    now = 2_000_000_000
    _install_artifact_signing_settings(monkeypatch)
    monkeypatch.setattr(storage.time, "time", lambda: float(now))

    with pytest.raises(HTTPException) as exc_info:
        storage.verify_artifact_access(  # type: ignore[arg-type]
            "artifact",
            "organization",
            now + 1,
            signature,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Invalid artifact signature"


@pytest.mark.parametrize("expires_at", (True, False, 2_000_000_001.0, "2000000001", None))
def test_artifact_access_rejects_non_integer_expiry_before_signing(
    monkeypatch: pytest.MonkeyPatch,
    expires_at: object,
) -> None:
    _install_artifact_signing_settings(monkeypatch)
    monkeypatch.setattr(storage.time, "time", lambda: 2_000_000_000.0)

    def must_not_sign(*_args: object) -> str:
        raise AssertionError("invalid expiry reached artifact signing")

    monkeypatch.setattr(storage, "sign_artifact_access", must_not_sign)
    with pytest.raises(HTTPException) as exc_info:
        storage.verify_artifact_access(  # type: ignore[arg-type]
            "artifact",
            "organization",
            expires_at,
            "a" * 64,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Invalid artifact expiry"


def test_internal_s3_client_uses_private_endpoint_and_bounded_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    sentinel = object()

    def record_client(service: str, **kwargs: Any) -> object:
        calls.append({"service": service, **kwargs})
        return sentinel

    monkeypatch.setattr(boto3, "client", record_client)
    monkeypatch.setattr(
        storage,
        "get_settings",
        lambda: SimpleNamespace(
            s3_endpoint="http://minio:9000",
            s3_access_key="test-access",
            s3_secret_key="test-secret",  # noqa: S106 - inert unit-test credential
        ),
    )
    storage.internal_s3_client.cache_clear()
    try:
        assert storage.internal_s3_client() is sentinel
    finally:
        storage.internal_s3_client.cache_clear()

    assert calls[0]["service"] == "s3"
    assert calls[0]["endpoint_url"] == "http://minio:9000"
    assert calls[0]["config"].signature_version == "s3v4"
    assert calls[0]["config"].connect_timeout == 2
    assert calls[0]["config"].read_timeout == 5
    assert calls[0]["config"].s3 == {"addressing_style": "path"}


def test_lightweight_verification_trusts_matching_worker_checksum_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"valid":true}'
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    _install_verification_client(monkeypatch, client)

    storage.verify_stored_object(expectation, stream_hash=False)

    assert client.head_calls == 1
    assert client.get_calls == 0


def test_service_owned_storage_runtime_bypasses_unrelated_api_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"worker":"verified"}'
    expectation = _expectation(payload)
    client = VerifyingS3Client(
        payload,
        metadata={"sha256": expectation.sha256},
    )

    def must_not_load_api_settings() -> None:
        raise AssertionError("worker verification loaded API-only settings")

    monkeypatch.setattr(storage, "get_settings", must_not_load_api_settings)
    with storage.storage_runtime(client=client, bucket="production-artifacts"):
        storage.verify_stored_object(expectation, stream_hash=False)

    assert client.head_calls == 1
    assert client.get_calls == 0


@pytest.mark.parametrize(
    "metadata",
    (
        {"sha256": "placeholder"},
        {"sha256": "placeholder", "manifest-sha256": "b" * 64},
    ),
)
def test_bundle_verification_rejects_missing_or_mismatched_manifest_binding(
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, str],
) -> None:
    payload = b"checksum-bound production bundle"
    digest = hashlib.sha256(payload).hexdigest()
    expectation = storage.StoredObjectExpectation(
        object_key="private/org/production.zip",
        sha256=digest,
        size_bytes=len(payload),
        content_type="application/zip",
        required_metadata=(("manifest-sha256", "a" * 64),),
    )
    client = VerifyingS3Client(
        payload,
        content_type="application/zip",
        metadata={**metadata, "sha256": digest},
    )
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactIntegrityError, match="binding metadata"):
        storage.verify_stored_object(expectation, stream_hash=False)

    assert client.head_calls == 1
    assert client.get_calls == 0


def test_bundle_verification_accepts_exact_manifest_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"checksum-bound production bundle"
    digest = hashlib.sha256(payload).hexdigest()
    manifest_sha = "a" * 64
    expectation = storage.StoredObjectExpectation(
        object_key="private/org/production.zip",
        sha256=digest,
        size_bytes=len(payload),
        content_type="application/zip",
        required_metadata=(("manifest-sha256", manifest_sha),),
    )
    client = VerifyingS3Client(
        payload,
        content_type="application/zip",
        metadata={"sha256": digest, "manifest-sha256": manifest_sha},
    )
    _install_verification_client(monkeypatch, client)

    storage.verify_stored_object(expectation, stream_hash=False)

    assert client.head_calls == 1
    assert client.get_calls == 0


def test_legacy_object_without_checksum_metadata_is_streamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"legacy artifact"
    client = VerifyingS3Client(payload)
    _install_verification_client(monkeypatch, client)

    storage.verify_stored_object(_expectation(payload), stream_hash=False)

    assert client.head_calls == 1
    assert client.get_calls == 1


def test_strict_verification_streams_and_rejects_corrupt_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_payload = b"reviewed bytes"
    corrupt_payload = b"corrupted byte"
    expectation = _expectation(expected_payload)
    client = VerifyingS3Client(
        corrupt_payload,
        metadata={"sha256": expectation.sha256},
    )
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactIntegrityError, match="checksum"):
        storage.verify_stored_object(expectation, stream_hash=True)

    assert client.get_calls == 1


def test_stream_verification_stops_after_one_byte_beyond_the_recorded_size() -> None:
    body = RecordingBody(b"oversize-provider-body")

    with pytest.raises(storage.ArtifactIntegrityError, match="recorded size"):
        storage._stream_sha256(
            body,
            expected_size=3,
            deadline=float("inf"),
        )

    assert body.read_sizes == [4]
    assert body.closed
    assert body.close_calls == 1


def test_bounded_verified_read_uses_one_get_and_closes_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"canonical":true}'
    client = VerifyingS3Client(payload)
    _install_verification_client(monkeypatch, client)

    result = storage.read_verified_stored_object(
        _expectation(payload),
        max_bytes=64 * 1024,
    )

    assert result == payload
    assert client.head_calls == 0
    assert client.get_calls == 1
    assert client.last_body is not None and client.last_body.closed


def test_bounded_verified_read_rejects_oversize_before_storage_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"bounded payload"
    client = VerifyingS3Client(payload)
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactIntegrityError, match="size limit"):
        storage.read_verified_stored_object(
            _expectation(payload),
            max_bytes=len(payload) - 1,
        )

    assert client.head_calls == client.get_calls == 0


def test_bounded_verified_read_rejects_corrupt_bytes_and_closes_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = b"expected bytes"
    client = VerifyingS3Client(b"tampered bytes")
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactIntegrityError, match="checksum"):
        storage.read_verified_stored_object(
            _expectation(expected),
            max_bytes=64 * 1024,
        )

    assert client.head_calls == 0
    assert client.get_calls == 1
    assert client.last_body is not None and client.last_body.closed


def test_bounded_verified_read_closes_body_when_get_headers_are_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"header-bound bytes"
    client = VerifyingS3Client(payload, content_type="text/plain")
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactIntegrityError, match="content type"):
        storage.read_verified_stored_object(
            _expectation(payload),
            max_bytes=64 * 1024,
        )

    assert client.last_body is not None and client.last_body.closed
    assert client.last_body.close_calls == 1


def test_stream_verification_closes_body_when_get_headers_are_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"legacy header-bound bytes"

    class InvalidGetHeadersClient(VerifyingS3Client):
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            response = super().get_object(**kwargs)
            response["ContentType"] = "text/plain"
            return response

    client = InvalidGetHeadersClient(payload)
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactIntegrityError, match="content type"):
        storage.verify_stored_object(_expectation(payload), stream_hash=True)

    assert client.last_body is not None and client.last_body.closed
    assert client.last_body.close_calls == 1


@pytest.mark.parametrize(
    ("error_code", "status_code", "expected_error"),
    (
        ("NoSuchKey", 404, storage.ArtifactIntegrityError),
        ("NoSuchBucket", 404, storage.ArtifactStorageUnavailableError),
        ("AccessDenied", 403, storage.ArtifactStorageUnavailableError),
    ),
)
def test_bounded_verified_read_classifies_provider_failures_without_details(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    status_code: int,
    expected_error: type[Exception],
) -> None:
    payload = b"provider failure"
    client = VerifyingS3Client(payload)
    client.get_error = ClientError(
        {
            "Error": {"Code": error_code, "Message": "private/key leaked"},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        "GetObject",
    )
    _install_verification_client(monkeypatch, client)

    with pytest.raises(expected_error) as error:
        storage.read_verified_stored_object(
            _expectation(payload),
            max_bytes=64 * 1024,
        )

    assert "private/key" not in str(error.value)
    assert client.head_calls == 0
    assert client.get_calls == 1


def test_verified_stored_object_cannot_be_forged_or_subclassed() -> None:
    payload = b"sealed verified bytes"

    with pytest.raises(TypeError, match="storage-owned"):
        storage.VerifiedStoredObject(
            BytesIO(payload),
            _expectation(payload),
            _seal=object(),
        )

    with pytest.raises(TypeError, match="cannot be subclassed"):

        class ForgedVerifiedObject(storage.VerifiedStoredObject):
            pass


def test_open_verified_object_uses_one_get_and_returns_a_sealed_bounded_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"x" * (2 * 1024 * 1024 + 19)
    digest = hashlib.sha256(payload).hexdigest()
    expectation = storage.StoredObjectExpectation(
        object_key="private/org/production.zip",
        sha256=digest,
        size_bytes=len(payload),
        content_type="application/zip",
        required_metadata=(("manifest-sha256", "a" * 64),),
    )
    client = VerifyingS3Client(
        payload,
        content_type="application/zip",
        metadata={"sha256": digest, "manifest-sha256": "a" * 64},
    )
    _install_verification_client(monkeypatch, client)
    spools, spool_calls = _install_spool_recorder(monkeypatch)

    verified = storage.open_verified_stored_object(
        expectation,
        max_bytes=4 * 1024 * 1024,
    )

    assert client.head_calls == 0
    assert client.get_calls == 1
    assert client.last_body is not None
    assert client.last_body.close_calls == 1
    assert client.last_body.read_sizes == [
        1024 * 1024,
        1024 * 1024,
        20,
        1,
    ]
    assert all(size <= 1024 * 1024 for size in client.last_body.read_sizes)
    assert spool_calls == [(8 * 1024 * 1024, "w+b")]
    assert verified.sha256 == digest
    assert verified.size_bytes == len(payload)
    assert verified.content_type == "application/zip"
    assert not verified.closed

    assert b"".join(verified) == payload
    assert verified.closed
    assert spools[0].close_calls == 1
    verified.close()
    assert spools[0].close_calls == 1


def test_verified_spool_budget_rejects_overcommit_before_provider_access_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"123456"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    _install_verification_client(monkeypatch, client)
    budget = storage._TransientByteBudget(10)
    monkeypatch.setattr(storage, "_TRANSIENT_BYTE_BUDGET", budget)

    first = storage.open_verified_stored_object(expectation, max_bytes=len(payload))
    assert budget.reserved_bytes == len(payload)
    assert client.get_calls == 1

    with pytest.raises(
        storage.ArtifactStorageUnavailableError,
        match="capacity is temporarily exhausted",
    ):
        storage.open_verified_stored_object(expectation, max_bytes=len(payload))
    assert client.get_calls == 1
    assert budget.reserved_bytes == len(payload)

    first.close()
    assert budget.reserved_bytes == 0
    second = storage.open_verified_stored_object(expectation, max_bytes=len(payload))
    assert client.get_calls == 2
    assert budget.reserved_bytes == len(payload)
    second.close()
    second.close()
    assert budget.reserved_bytes == 0


def test_verified_spool_budget_is_released_when_integrity_verification_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = b"expected"
    expectation = _expectation(expected)
    client = VerifyingS3Client(
        b"tamperED",
        metadata={"sha256": expectation.sha256},
    )
    _install_verification_client(monkeypatch, client)
    budget = storage._TransientByteBudget(len(expected))
    monkeypatch.setattr(storage, "_TRANSIENT_BYTE_BUDGET", budget)

    with pytest.raises(storage.ArtifactIntegrityError, match="checksum"):
        storage.open_verified_stored_object(expectation, max_bytes=len(expected))

    assert budget.reserved_bytes == 0
    assert client.get_calls == 1


@pytest.mark.parametrize("invalid_max_bytes", (False, 0, -1))
def test_open_verified_object_strictly_rejects_invalid_limits_before_storage_access(
    monkeypatch: pytest.MonkeyPatch,
    invalid_max_bytes: int,
) -> None:
    payload = b"bounded artifact"
    client = VerifyingS3Client(payload)
    _install_verification_client(monkeypatch, client)

    with pytest.raises(ValueError, match="positive integer"):
        storage.open_verified_stored_object(
            _expectation(payload),
            max_bytes=invalid_max_bytes,
        )

    assert client.head_calls == client.get_calls == 0


@pytest.mark.parametrize("invalid_size", (False, 0, -1, 17))
def test_open_verified_object_strictly_rejects_invalid_or_oversize_records(
    monkeypatch: pytest.MonkeyPatch,
    invalid_size: int,
) -> None:
    payload = b"sixteen-byte-art"
    client = VerifyingS3Client(payload)
    _install_verification_client(monkeypatch, client)
    expectation = replace(_expectation(payload), size_bytes=invalid_size)

    with pytest.raises(storage.ArtifactIntegrityError, match="download limit"):
        storage.open_verified_stored_object(expectation, max_bytes=16)

    assert client.head_calls == client.get_calls == 0


@pytest.mark.parametrize(
    ("content_type", "metadata", "message"),
    (
        ("text/plain", {"sha256": "placeholder"}, "content type"),
        ("application/json", {}, "checksum metadata"),
        ("application/json", {"sha256": "b" * 64}, "checksum metadata"),
    ),
)
def test_open_verified_object_rejects_unbound_headers_and_closes_the_get_body(
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
    metadata: dict[str, str],
    message: str,
) -> None:
    payload = b"bound artifact"
    expectation = _expectation(payload)
    client = VerifyingS3Client(
        payload,
        content_type=content_type,
        metadata=metadata,
    )
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactIntegrityError, match=message):
        storage.open_verified_stored_object(expectation, max_bytes=64 * 1024)

    assert client.get_calls == 1
    assert client.last_body is not None
    assert client.last_body.close_calls == 1


def test_open_verified_object_rejects_tampered_bytes_and_closes_every_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = b"expected"
    expectation = _expectation(expected)
    client = VerifyingS3Client(
        b"tamperED",
        metadata={"sha256": expectation.sha256},
    )
    _install_verification_client(monkeypatch, client)
    spools, _calls = _install_spool_recorder(monkeypatch)

    with pytest.raises(storage.ArtifactIntegrityError, match="checksum"):
        storage.open_verified_stored_object(expectation, max_bytes=64 * 1024)

    assert client.get_calls == 1
    assert client.last_body is not None
    assert client.last_body.close_calls == 1
    assert spools[0].close_calls == 1


def test_open_verified_object_closes_the_spool_when_provider_body_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"verified before close"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    client.body_close_error = OSError("private provider close details")
    _install_verification_client(monkeypatch, client)
    spools, _calls = _install_spool_recorder(monkeypatch)

    with pytest.raises(storage.ArtifactStorageUnavailableError) as error:
        storage.open_verified_stored_object(expectation, max_bytes=64 * 1024)

    assert str(error.value) == "artifact storage is temporarily unavailable"
    assert client.last_body is not None
    assert client.last_body.close_calls == 1
    assert spools[0].close_calls == 1


def test_verified_stream_closes_exactly_once_on_disconnect_and_background_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"streamed response bytes"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    _install_verification_client(monkeypatch, client)
    spools, _calls = _install_spool_recorder(monkeypatch)
    verified = storage.open_verified_stored_object(expectation, max_bytes=64 * 1024)

    iterator = verified.iter_bytes()
    assert next(iterator) == payload
    iterator.close()
    assert verified.closed
    assert spools[0].close_calls == 1

    verified.close()
    verified.close()
    assert spools[0].close_calls == 1
    with pytest.raises(RuntimeError, match="closed"):
        next(iter(verified))


def test_verified_iterator_close_before_first_next_closes_the_token_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"never yielded"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    _install_verification_client(monkeypatch, client)
    spools, _calls = _install_spool_recorder(monkeypatch)
    verified = storage.open_verified_stored_object(expectation, max_bytes=64 * 1024)

    iterator = verified.iter_bytes()
    iterator.close()

    assert verified.closed
    assert spools[0].close_calls == 1
    iterator.close()
    verified.close()
    assert spools[0].close_calls == 1
    with pytest.raises(StopIteration):
        next(iterator)
    with pytest.raises(RuntimeError, match="closed"):
        verified.iter_bytes()


def test_verified_iterator_io_failure_closes_the_token_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"stream read failure"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    _install_verification_client(monkeypatch, client)
    spool = FailingReadSpool()
    monkeypatch.setattr(storage, "SpooledTemporaryFile", lambda **_kwargs: spool)
    verified = storage.open_verified_stored_object(expectation, max_bytes=64 * 1024)
    spool.fail_reads = True

    iterator = verified.iter_bytes()
    with pytest.raises(storage.ArtifactStorageUnavailableError, match="could not be read"):
        next(iterator)

    assert verified.closed
    assert spool.close_calls == 1
    iterator.close()
    assert spool.close_calls == 1


def test_validation_bytes_rewinds_the_private_spool_for_normal_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"validate once, stream without another provider request"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    _install_verification_client(monkeypatch, client)
    spools, _calls = _install_spool_recorder(monkeypatch)
    verified = storage.open_verified_stored_object(expectation, max_bytes=64 * 1024)

    assert verified.validation_bytes(max_bytes=len(payload)) == payload
    assert not verified.closed
    assert spools[0].tell() == 0
    assert client.get_calls == 1

    assert b"".join(verified) == payload
    assert client.get_calls == 1
    assert verified.closed
    assert spools[0].close_calls == 1


@pytest.mark.parametrize("invalid_max_bytes", (False, 0, -1))
def test_validation_bytes_strictly_rejects_invalid_limits_without_consuming_token(
    monkeypatch: pytest.MonkeyPatch,
    invalid_max_bytes: int,
) -> None:
    payload = b"still streamable"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    _install_verification_client(monkeypatch, client)
    verified = storage.open_verified_stored_object(expectation, max_bytes=64 * 1024)

    with pytest.raises(ValueError, match="positive integer"):
        verified.validation_bytes(max_bytes=invalid_max_bytes)

    assert b"".join(verified) == payload


def test_validation_bytes_rejects_oversize_without_consuming_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"bounded validation bytes"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    _install_verification_client(monkeypatch, client)
    verified = storage.open_verified_stored_object(expectation, max_bytes=64 * 1024)

    with pytest.raises(storage.ArtifactIntegrityError, match="validation size limit"):
        verified.validation_bytes(max_bytes=len(payload) - 1)

    assert not verified.closed
    assert b"".join(verified) == payload


def test_validation_bytes_rejects_closed_or_started_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"one-shot validation"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    _install_verification_client(monkeypatch, client)
    closed = storage.open_verified_stored_object(expectation, max_bytes=64 * 1024)
    started = storage.open_verified_stored_object(expectation, max_bytes=64 * 1024)

    closed.close()
    with pytest.raises(RuntimeError, match="closed"):
        closed.validation_bytes(max_bytes=len(payload))

    iterator = started.iter_bytes()
    assert next(iterator) == payload
    with pytest.raises(RuntimeError, match="iteration has started"):
        started.validation_bytes(max_bytes=len(payload))
    iterator.close()


def test_validation_io_failure_invalidates_and_closes_the_token_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"temporary spool failure"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    _install_verification_client(monkeypatch, client)
    spool = FailingReadSpool()
    monkeypatch.setattr(
        storage,
        "SpooledTemporaryFile",
        lambda **_kwargs: spool,
    )
    verified = storage.open_verified_stored_object(expectation, max_bytes=64 * 1024)
    spool.fail_reads = True

    with pytest.raises(storage.ArtifactStorageUnavailableError) as error:
        verified.validation_bytes(max_bytes=len(payload))

    assert str(error.value) == "verified artifact spool could not be read"
    assert verified.closed
    assert spool.close_calls == 1
    verified.close()
    assert spool.close_calls == 1
    with pytest.raises(RuntimeError, match="closed"):
        verified.validation_bytes(max_bytes=len(payload))


def test_immutable_store_uses_conditional_create_and_verifies_persisted_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"immutable image bytes"
    expectation = _expectation(payload)
    client = VerifyingS3Client(
        payload,
        content_type=expectation.content_type,
        metadata={"sha256": expectation.sha256},
    )
    _install_verification_client(monkeypatch, client)

    storage.store_immutable_object(
        expectation.object_key,
        payload,
        expectation.content_type,
        expectation.sha256,
    )

    assert len(client.put_calls) == 1
    assert client.put_calls[0]["IfNoneMatch"] == "*"
    assert client.put_calls[0]["Metadata"] == {
        "sha256": expectation.sha256,
        "immutable": "true",
    }
    assert client.head_calls == client.get_calls == 1


def test_immutable_store_accepts_preexisting_object_only_when_bytes_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"same clipboard bytes"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    client.put_error = ClientError(
        {
            "Error": {"Code": "PreconditionFailed", "Message": "private/key exists"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        },
        "PutObject",
    )
    _install_verification_client(monkeypatch, client)

    storage.store_immutable_object(
        expectation.object_key,
        payload,
        expectation.content_type,
        expectation.sha256,
    )

    assert len(client.put_calls) == 1
    assert client.get_calls == 1


def test_create_once_bundle_rejects_preexisting_object_with_wrong_manifest_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"same bundle bytes"
    expectation = storage.StoredObjectExpectation(
        object_key="private/org/production.zip",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        content_type="application/zip",
    )
    client = VerifyingS3Client(
        payload,
        content_type="application/zip",
        metadata={
            "sha256": expectation.sha256,
            "manifest-sha256": "b" * 64,
        },
    )
    client.put_error = ClientError(
        {
            "Error": {"Code": "PreconditionFailed", "Message": "private/key exists"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        },
        "PutObject",
    )
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactIntegrityError, match="binding metadata"):
        storage.store_create_once_object(
            client,
            bucket="private-artifacts",
            object_key=expectation.object_key,
            content=payload,
            content_type=expectation.content_type,
            sha256=expectation.sha256,
            metadata={"manifest-sha256": "a" * 64},
        )

    assert len(client.put_calls) == 1
    assert client.get_calls == 0


def test_open_verified_object_total_deadline_releases_body_spool_and_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"slow-drip"
    expectation = _expectation(payload)
    clock = [0.0]

    class SlowDripBody(RecordingBody):
        def read(self, size: int | None = -1) -> bytes:
            clock[0] += 11.0
            bounded_size = 1 if size is None or size < 0 else min(size, 1)
            return super().read(bounded_size)

    class SlowDripClient(VerifyingS3Client):
        def get_object(self, **_kwargs: Any) -> dict[str, Any]:
            self.get_calls += 1
            self.last_body = SlowDripBody(self.payload)
            return {
                "ContentLength": len(self.payload),
                "ContentType": self.content_type,
                "Metadata": self.metadata,
                "Body": self.last_body,
            }

    client = SlowDripClient(payload, metadata={"sha256": expectation.sha256})
    _install_verification_client(monkeypatch, client)
    spools, _calls = _install_spool_recorder(monkeypatch)
    budget = storage._TransientByteBudget(64)
    monkeypatch.setattr(storage, "_TRANSIENT_BYTE_BUDGET", budget)
    monkeypatch.setattr(storage.time, "monotonic", lambda: clock[0])

    with pytest.raises(storage.ArtifactStorageUnavailableError, match="deadline"):
        storage.open_verified_stored_object(expectation, max_bytes=64)

    assert client.last_body is not None
    assert client.last_body.closed
    assert client.last_body.close_calls == 1
    assert len(spools) == 1 and spools[0].close_calls == 1
    assert budget.reserved_bytes == 0


def test_provider_body_watchdog_aborts_one_blocked_read_and_releases_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"blocked-provider-read"
    expectation = _expectation(payload)
    released = Event()

    class BlockingBody(RecordingBody):
        def read(self, size: int | None = -1) -> bytes:
            self.read_sizes.append(-1 if size is None else size)
            released.wait(timeout=2.0)
            return b"" if self.closed else BytesIO.read(self, size)

        def close(self) -> None:
            super().close()
            released.set()

    class BlockingClient(VerifyingS3Client):
        def get_object(self, **_kwargs: Any) -> dict[str, Any]:
            self.get_calls += 1
            self.last_body = BlockingBody(self.payload)
            return {
                "ContentLength": len(self.payload),
                "ContentType": self.content_type,
                "Metadata": self.metadata,
                "Body": self.last_body,
            }

    client = BlockingClient(payload, metadata={"sha256": expectation.sha256})
    _install_verification_client(monkeypatch, client)
    spools, _calls = _install_spool_recorder(monkeypatch)
    budget = storage._TransientByteBudget(64)
    monkeypatch.setattr(storage, "_TRANSIENT_BYTE_BUDGET", budget)
    monkeypatch.setattr(storage, "_OBJECT_STORAGE_TOTAL_READ_SECONDS", 0.02)

    started = monotonic()
    with pytest.raises(storage.ArtifactStorageUnavailableError, match="deadline"):
        storage.open_verified_stored_object(expectation, max_bytes=64)

    assert monotonic() - started < 1.0
    assert client.last_body is not None
    assert client.last_body.close_calls == 1
    assert len(spools) == 1 and spools[0].close_calls == 1
    assert budget.reserved_bytes == 0


def test_provider_watchdog_shuts_down_raw_socket_before_buffered_body_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"socket-blocked-provider-read"
    expectation = _expectation(payload)

    class SocketRawStream:
        def __init__(self) -> None:
            self.reader_socket, self.writer_socket = socket.socketpair()
            self.file = self.reader_socket.makefile("rb")
            self.shutdown_calls = 0
            self.close_calls = 0

        def read(self, size: int) -> bytes:
            return self.file.read(size)

        def shutdown(self) -> None:
            self.shutdown_calls += 1
            self.reader_socket.shutdown(socket.SHUT_RD)

        def close(self) -> None:
            self.close_calls += 1
            self.file.close()
            self.reader_socket.close()
            self.writer_socket.close()

    class SocketBody:
        def __init__(self) -> None:
            self._raw_stream = SocketRawStream()
            self.close_calls = 0

        def read(self, size: int) -> bytes:
            return self._raw_stream.read(size)

        def close(self) -> None:
            self.close_calls += 1
            self._raw_stream.close()

    class SocketClient(VerifyingS3Client):
        def __init__(self) -> None:
            super().__init__(payload, metadata={"sha256": expectation.sha256})
            self.socket_body: SocketBody | None = None

        def get_object(self, **_kwargs: Any) -> dict[str, Any]:
            self.get_calls += 1
            self.socket_body = SocketBody()
            return {
                "ContentLength": len(self.payload),
                "ContentType": self.content_type,
                "Metadata": self.metadata,
                "Body": self.socket_body,
            }

    client = SocketClient()
    _install_verification_client(monkeypatch, client)
    spools, _calls = _install_spool_recorder(monkeypatch)
    budget = storage._TransientByteBudget(64)
    monkeypatch.setattr(storage, "_TRANSIENT_BYTE_BUDGET", budget)
    monkeypatch.setattr(storage, "_OBJECT_STORAGE_TOTAL_READ_SECONDS", 0.02)

    started = monotonic()
    with pytest.raises(storage.ArtifactStorageUnavailableError, match="deadline"):
        storage.open_verified_stored_object(expectation, max_bytes=64)

    assert monotonic() - started < 1.0
    assert client.socket_body is not None
    assert client.socket_body._raw_stream.shutdown_calls == 1
    assert client.socket_body.close_calls == 1
    assert client.socket_body._raw_stream.close_calls == 1
    assert len(spools) == 1 and spools[0].close_calls == 1
    assert budget.reserved_bytes == 0


def test_nested_storage_deadlines_keep_the_earliest_absolute_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(storage.time, "monotonic", lambda: clock[0])

    with storage.storage_read_deadline(5.0):
        assert storage._storage_read_deadline() == 105.0
        clock[0] = 101.0
        with storage.storage_read_deadline(10.0):
            assert storage._storage_read_deadline() == 105.0
        assert storage._storage_read_deadline() == 105.0

    assert storage._storage_read_deadline() == 131.0


@pytest.mark.parametrize("status_code", (409, 412))
def test_create_once_race_rechecks_existing_object_for_both_conflict_statuses(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    payload = b"same deterministic artifact"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    client.put_error = ClientError(
        {
            "Error": {"Code": "Conflict", "Message": "private provider details"},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        "PutObject",
    )
    _install_verification_client(monkeypatch, client)

    storage.store_immutable_object(
        expectation.object_key,
        payload,
        expectation.content_type,
        expectation.sha256,
    )

    assert client.head_calls == client.get_calls == 1


def test_external_evidence_uses_the_same_conditional_create_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"signed supplier evidence"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    _install_verification_client(monkeypatch, client)

    storage.store_evidence_object(
        expectation.object_key,
        payload,
        expectation.content_type,
        expectation.sha256,
    )

    assert client.put_calls[0]["IfNoneMatch"] == "*"
    assert client.head_calls == client.get_calls == 1


@pytest.mark.parametrize("status_code", (403, 500, 503))
def test_create_once_does_not_reclassify_auth_or_transient_put_failures_as_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    payload = b"must never be treated as stored"
    expectation = _expectation(payload)
    client = VerifyingS3Client(payload, metadata={"sha256": expectation.sha256})
    client.put_error = ClientError(
        {
            "Error": {"Code": "ServiceFailure", "Message": "private provider details"},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        "PutObject",
    )
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactStorageUnavailableError):
        storage.store_immutable_object(
            expectation.object_key,
            payload,
            expectation.content_type,
            expectation.sha256,
        )

    assert client.head_calls == client.get_calls == 0


def test_immutable_store_rejects_tampered_preexisting_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = b"expected reference"
    expectation = _expectation(expected)
    client = VerifyingS3Client(
        b"tampered reference",
        metadata={"sha256": expectation.sha256},
    )
    client.put_error = ClientError(
        {
            "Error": {"Code": "PreconditionFailed", "Message": "private/key exists"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        },
        "PutObject",
    )
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactIntegrityError):
        storage.store_immutable_object(
            expectation.object_key,
            expected,
            expectation.content_type,
            expectation.sha256,
        )


def test_verification_rejects_size_drift_before_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"size drift"
    expectation = storage.StoredObjectExpectation(
        object_key="private/org/evidence.json",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload) + 1,
        content_type="application/json",
    )
    client = VerifyingS3Client(payload)
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactIntegrityError, match="size"):
        storage.verify_stored_object(expectation, stream_hash=False)

    assert client.get_calls == 0


def test_missing_object_is_repairable_without_leaking_provider_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"missing"
    client = VerifyingS3Client(payload)
    client.head_error = ClientError(
        {
            "Error": {"Code": "NoSuchKey", "Message": "secret/key does not exist"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "HeadObject",
    )
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactIntegrityError) as error:
        storage.verify_stored_object(_expectation(payload), stream_hash=False)

    assert "secret/key" not in str(error.value)


@pytest.mark.parametrize(
    ("error_code", "status_code"),
    (("NoSuchBucket", 404), ("InvalidAccessKeyId", 403), ("InvalidAccessKeyId", 404)),
)
def test_bucket_or_authentication_failure_is_not_classified_as_repairable(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    status_code: int,
) -> None:
    payload = b"must remain untouched"
    client = VerifyingS3Client(payload)
    client.head_error = ClientError(
        {
            "Error": {"Code": error_code, "Message": "private provider details"},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        "HeadObject",
    )
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactStorageUnavailableError) as error:
        storage.verify_stored_object(_expectation(payload), stream_hash=False)

    assert str(error.value) == "artifact storage is temporarily unavailable"
    assert "private provider" not in str(error.value)


@pytest.mark.parametrize(
    "provider_error",
    (
        ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "credentials leaked here"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "HeadObject",
        ),
        ClientError(
            {
                "Error": {"Code": "InternalError", "Message": "minio.internal:9000"},
                "ResponseMetadata": {"HTTPStatusCode": 503},
            },
            "HeadObject",
        ),
        ReadTimeoutError(endpoint_url="http://private-minio:9000"),
    ),
)
def test_transient_or_authorization_failure_is_generic_and_not_repairable(
    monkeypatch: pytest.MonkeyPatch,
    provider_error: Exception,
) -> None:
    payload = b"temporarily unavailable"
    client = VerifyingS3Client(payload)
    client.head_error = provider_error
    _install_verification_client(monkeypatch, client)

    with pytest.raises(storage.ArtifactStorageUnavailableError) as error:
        storage.verify_stored_object(_expectation(payload), stream_hash=False)

    message = str(error.value)
    assert message == "artifact storage is temporarily unavailable"
    assert "minio" not in message
    assert "credentials" not in message
