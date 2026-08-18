from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import boto3
import pytest
from app.storage import (
    ArtifactIntegrityError,
    ArtifactStorageUnavailableError,
    store_create_once_object,
)
from botocore.config import Config

pytestmark = pytest.mark.integration


def _seaweed_client(*, secret_key: str | None = None) -> Any:
    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("SEAWEEDFS_S3_ENDPOINT is required for the SeaweedFS integration test")
    access_key = os.environ.get("SEAWEEDFS_S3_ACCESS_KEY", "custombuild")
    configured_secret = os.environ.get(
        "SEAWEEDFS_S3_SECRET_KEY",
        "change-me-object-storage",
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key or configured_secret,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=2,
            read_timeout=5,
            retries={"mode": "standard", "max_attempts": 1},
            s3={"addressing_style": "path"},
        ),
    )


@pytest.fixture
def seaweed_bucket() -> Iterator[tuple[Any, str]]:
    client = _seaweed_client()
    bucket = f"custombuild-immutability-{uuid4().hex}"
    client.create_bucket(Bucket=bucket)
    try:
        yield client, bucket
    finally:
        response = client.list_objects_v2(Bucket=bucket)
        for item in response.get("Contents", []):
            client.delete_object(Bucket=bucket, Key=item["Key"])
        client.delete_bucket(Bucket=bucket)


def test_seaweedfs_441_create_once_retry_and_collision_preserve_original(
    seaweed_bucket: tuple[Any, str],
) -> None:
    client, bucket = seaweed_bucket
    original = b"immutable-release-v1"
    different = b"immutable-release-v2"
    original_digest = hashlib.sha256(original).hexdigest()
    key = f"sha256/{original_digest}/production.zip"

    store_create_once_object(
        client,
        bucket=bucket,
        object_key=key,
        content=original,
        content_type="application/zip",
        sha256=original_digest,
    )
    store_create_once_object(
        client,
        bucket=bucket,
        object_key=key,
        content=original,
        content_type="application/zip",
        sha256=original_digest,
    )
    with pytest.raises(ArtifactIntegrityError):
        store_create_once_object(
            client,
            bucket=bucket,
            object_key=key,
            content=different,
            content_type="application/zip",
            sha256=hashlib.sha256(different).hexdigest(),
        )

    head = client.head_object(Bucket=bucket, Key=key)
    stored = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    assert stored == original
    assert int(head["ContentLength"]) == len(original)
    assert head["ContentType"] == "application/zip"
    assert head["Metadata"]["sha256"] == original_digest


def test_seaweedfs_441_authorization_failure_is_not_a_create_conflict(
    seaweed_bucket: tuple[Any, str],
) -> None:
    _client, bucket = seaweed_bucket
    unauthorized = _seaweed_client(
        secret_key="intentionally-wrong-integration-secret"  # noqa: S106 - inert test key
    )
    payload = b"must-not-be-created"

    with pytest.raises(ArtifactStorageUnavailableError):
        store_create_once_object(
            unauthorized,
            bucket=bucket,
            object_key=f"auth-failure/{uuid4().hex}",
            content=payload,
            content_type="application/octet-stream",
            sha256=hashlib.sha256(payload).hexdigest(),
        )
