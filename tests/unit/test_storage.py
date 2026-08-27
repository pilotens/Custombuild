from __future__ import annotations

import hashlib
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from app import storage
from botocore.exceptions import ClientError, ReadTimeoutError


class RecordingS3Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str:
        self.calls.append({"operation": operation, **kwargs})
        return "https://artifacts.example.test/signed"


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
        self.last_body: BytesIO | None = None
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
        self.last_body = BytesIO(self.payload)
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


def test_s3_client_uses_v4_path_style_signatures(
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
            s3_public_endpoint="http://localhost:9000",
            s3_access_key="test-access",
            s3_secret_key="test-secret",  # noqa: S106 - inert unit-test credential
        ),
    )
    storage.s3_client.cache_clear()
    try:
        assert storage.s3_client() is sentinel
    finally:
        storage.s3_client.cache_clear()

    assert calls[0]["service"] == "s3"
    assert calls[0]["config"].signature_version == "s3v4"
    assert calls[0]["config"].s3 == {"addressing_style": "path"}


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


def test_presigned_download_sets_a_safe_content_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RecordingS3Client()
    monkeypatch.setattr(storage, "s3_client", lambda: client)

    result = storage.presigned_get(
        "org/job/bundle.zip",
        filename="custombuild-rev-7.zip",
    )

    assert result == "https://artifacts.example.test/signed"
    assert client.calls[0]["Params"]["ResponseContentDisposition"] == (
        'attachment; filename="custombuild-rev-7.zip"'
    )


@pytest.mark.parametrize(
    "filename",
    ("../bundle.zip", "bundle name.zip", "bundle\r\nX-Test: injected", "åäö.zip"),
)
def test_presigned_download_rejects_unsafe_filenames(
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    client = RecordingS3Client()
    monkeypatch.setattr(storage, "s3_client", lambda: client)

    with pytest.raises(ValueError, match="unsafe"):
        storage.presigned_get("org/job/bundle.zip", filename=filename)

    assert client.calls == []
