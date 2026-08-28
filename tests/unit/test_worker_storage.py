from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast

import custombuild_worker.tasks as worker_tasks
import pytest
from app.storage import ArtifactStorageUnavailableError
from botocore.exceptions import ClientError, ReadTimeoutError
from custombuild_manufacturing import ProductionBlockedError


class ConditionalObjectStorage:
    def __init__(self, *, conflict_status: int = 412) -> None:
        self.conflict_status = conflict_status
        self.payload: bytes | None = None
        self.content_type = ""
        self.metadata: dict[str, str] = {}
        self.put_failure: ClientError | None = None
        self.put_calls: list[dict[str, Any]] = []

    def head_bucket(self, **_kwargs: Any) -> None:
        return None

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        self.put_calls.append(kwargs)
        if self.put_failure is not None:
            raise self.put_failure
        if self.payload is not None:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed", "Message": "already exists"},
                    "ResponseMetadata": {"HTTPStatusCode": self.conflict_status},
                },
                "PutObject",
            )
        assert kwargs["IfNoneMatch"] == "*"
        self.payload = bytes(kwargs["Body"])
        self.content_type = str(kwargs["ContentType"])
        self.metadata = dict(kwargs["Metadata"])
        return {"ETag": '"created"'}

    def head_object(self, **_kwargs: Any) -> dict[str, Any]:
        assert self.payload is not None
        return {
            "ContentLength": len(self.payload),
            "ContentType": self.content_type,
            "Metadata": self.metadata,
        }

    def get_object(self, **_kwargs: Any) -> dict[str, Any]:
        assert self.payload is not None
        return {
            "ContentLength": len(self.payload),
            "ContentType": self.content_type,
            "Metadata": self.metadata,
            "Body": BytesIO(self.payload),
        }


def _install_storage(
    monkeypatch: pytest.MonkeyPatch,
    client: ConditionalObjectStorage,
) -> None:
    monkeypatch.setattr(worker_tasks, "_s3_client", lambda: client)
    monkeypatch.setattr(
        worker_tasks,
        "WORKER_SETTINGS",
        SimpleNamespace(s3_bucket="private-artifacts"),
    )


@pytest.mark.parametrize("conflict_status", (409, 412))
def test_worker_create_once_retry_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    conflict_status: int,
) -> None:
    client = ConditionalObjectStorage(conflict_status=conflict_status)
    _install_storage(monkeypatch, client)
    payload = b"deterministic production bundle"

    worker_tasks._put_object("org/hash/production.zip", payload, "application/zip")
    worker_tasks._put_object("org/hash/production.zip", payload, "application/zip")

    assert client.payload == payload
    assert len(client.put_calls) == 2
    assert all(call["IfNoneMatch"] == "*" for call in client.put_calls)


def test_worker_binds_bundle_object_to_exact_manifest_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ConditionalObjectStorage()
    _install_storage(monkeypatch, client)
    payload = b"deterministic production bundle"
    bundle_sha = worker_tasks.sha256_hex(payload)
    manifest_sha = "a" * 64
    bundle_key = (
        f"org/design/context/linked-v1/{manifest_sha}/{bundle_sha}/production.zip"
    )

    worker_tasks._put_object(
        bundle_key,
        payload,
        "application/zip",
        metadata={"manifest-sha256": manifest_sha},
    )

    assert client.metadata == {
        "sha256": worker_tasks.sha256_hex(payload),
        "immutable": "true",
        "manifest-sha256": manifest_sha,
    }

    with pytest.raises(ProductionBlockedError, match="non-deterministic"):
        worker_tasks._put_object(
            bundle_key,
            payload,
            "application/zip",
            metadata={"manifest-sha256": "b" * 64},
        )


def test_worker_rejects_different_bytes_at_the_same_content_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ConditionalObjectStorage()
    _install_storage(monkeypatch, client)
    original = b"production bytes 01"
    different = b"production bytes 02"
    key = "org/design/context/hash/production.zip"
    worker_tasks._put_object(key, original, "application/zip")

    with pytest.raises(ProductionBlockedError, match="non-deterministic"):
        worker_tasks._put_object(key, different, "application/zip")

    assert client.payload == original


@pytest.mark.parametrize("status_code", (403, 500, 503))
def test_worker_does_not_treat_auth_or_transient_failure_as_existing_object(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    client = ConditionalObjectStorage()
    client.put_failure = ClientError(
        {
            "Error": {"Code": "ServiceFailure", "Message": "provider details"},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        "PutObject",
    )
    _install_storage(monkeypatch, client)

    with pytest.raises(ArtifactStorageUnavailableError):
        worker_tasks._put_object(
            "org/design/context/hash/manifest.json",
            b'{"manifest":true}',
            "application/json",
        )

    assert client.payload is None


def test_worker_s3_client_has_a_bounded_stall_budget_before_the_task_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def client(service: str, **kwargs: object) -> object:
        captured.update(service=service, **kwargs)
        return sentinel

    monkeypatch.setattr(worker_tasks.boto3, "client", client)

    assert worker_tasks._s3_client() is sentinel
    config = cast(Any, captured["config"])
    assert captured["service"] == "s3"
    assert config.connect_timeout == worker_tasks.S3_CONNECT_TIMEOUT_SECONDS
    assert config.read_timeout == worker_tasks.S3_READ_TIMEOUT_SECONDS
    assert config.retries == {
        "mode": "standard",
        "total_max_attempts": worker_tasks.S3_TOTAL_MAX_ATTEMPTS,
    }
    assert worker_tasks.S3_STALLED_REQUEST_BUDGET_SECONDS < (
        worker_tasks.GENERATION_TASK_SOFT_TIME_LIMIT_SECONDS
    )


def test_worker_turns_an_s3_read_stall_into_an_actionable_job_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StalledObjectStorage:
        def head_bucket(self, **_kwargs: Any) -> None:
            raise ReadTimeoutError(endpoint_url="http://private-object-store:8333")

    monkeypatch.setattr(worker_tasks, "_s3_client", StalledObjectStorage)
    monkeypatch.setattr(
        worker_tasks,
        "WORKER_SETTINGS",
        SimpleNamespace(s3_bucket="private-artifacts"),
    )

    with pytest.raises(ArtifactStorageUnavailableError) as caught:
        worker_tasks._put_object(
            "org/design/context/hash/manifest.json",
            b'{"manifest":true}',
            "application/json",
        )

    assert str(caught.value) == (
        "artifact storage is temporarily unavailable; verify the object store and retry"
    )
    assert "private-object-store" not in str(caught.value)


def test_worker_never_bootstraps_a_missing_production_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingBucketStorage:
        create_calls = 0

        def head_bucket(self, **_kwargs: Any) -> None:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchBucket", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadBucket",
            )

        def create_bucket(self, **_kwargs: Any) -> None:
            self.create_calls += 1

    storage_client = MissingBucketStorage()
    monkeypatch.setattr(worker_tasks, "_s3_client", lambda: storage_client)
    monkeypatch.setattr(
        worker_tasks,
        "WORKER_SETTINGS",
        SimpleNamespace(s3_bucket="private-artifacts"),
    )

    with pytest.raises(ArtifactStorageUnavailableError):
        worker_tasks._put_object("org/job/evidence.json", b"evidence", "application/json")

    assert storage_client.create_calls == 0
