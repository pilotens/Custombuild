from __future__ import annotations

import hashlib
import hmac
import re
import time
from functools import lru_cache
from typing import Any

import boto3
from fastapi import HTTPException

from .config import get_settings


def sign_artifact_access(artifact_id: str, organization_id: str, expires_at: int) -> str:
    message = f"{artifact_id}:{organization_id}:{expires_at}".encode()
    return hmac.new(
        get_settings().artifact_signing_secret.encode(), message, hashlib.sha256
    ).hexdigest()


def verify_artifact_access(
    artifact_id: str, organization_id: str, expires_at: int, signature: str
) -> None:
    if expires_at < int(time.time()):
        raise HTTPException(status_code=410, detail="Artifact link expired")
    expected = sign_artifact_access(artifact_id, organization_id, expires_at)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="Invalid artifact signature")


@lru_cache(maxsize=1)
def s3_client() -> Any:
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_public_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name="us-east-1",
    )


def presigned_get(object_key: str, *, filename: str | None = None) -> str:
    settings = get_settings()
    parameters = {"Bucket": settings.s3_bucket, "Key": object_key}
    if filename is not None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename) is None:
            raise ValueError("artifact download filename contains unsafe characters")
        parameters["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    return str(
        s3_client().generate_presigned_url(
            "get_object",
            Params=parameters,
            ExpiresIn=settings.artifact_url_ttl_seconds,
        )
    )
