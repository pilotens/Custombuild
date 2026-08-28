from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Generator, Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from tempfile import SpooledTemporaryFile
from threading import Lock
from typing import IO, Any, Never

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from .config import get_settings

_OBJECT_STREAM_CHUNK_BYTES = 1024 * 1024
_VERIFIED_OBJECT_SPOOL_BYTES = 8 * 1024 * 1024
_VERIFIED_STORED_OBJECT_SEAL = object()


class ArtifactIntegrityError(Exception):
    """The persisted artifact no longer matches its checksum-bound record."""


class ArtifactStorageUnavailableError(Exception):
    """Artifact storage could not be verified without risking destructive repair."""


@dataclass(frozen=True, slots=True)
class StoredObjectExpectation:
    object_key: str
    sha256: str
    size_bytes: int
    content_type: str
    required_metadata: tuple[tuple[str, str], ...] = ()


class VerifiedStoredObject:
    """One-shot access to an object whose persisted bytes were fully verified.

    Instances are created only by :func:`open_verified_stored_object`.  The
    underlying spool is deliberately private so callers cannot replace it with
    bytes that did not pass the storage-owned verification path.
    """

    __slots__ = (
        "_closed",
        "_content_type",
        "_iteration_started",
        "_lock",
        "_sha256",
        "_size_bytes",
        "_stream",
    )

    def __init__(
        self,
        stream: IO[bytes],
        expectation: StoredObjectExpectation,
        *,
        _seal: object,
    ) -> None:
        if _seal is not _VERIFIED_STORED_OBJECT_SEAL:
            raise TypeError("VerifiedStoredObject instances are storage-owned")
        self._stream: IO[bytes] | None = stream
        self._sha256 = expectation.sha256
        self._size_bytes = expectation.size_bytes
        self._content_type = expectation.content_type
        self._lock = Lock()
        self._closed = False
        self._iteration_started = False

    def __init_subclass__(cls, **_kwargs: Any) -> Never:
        raise TypeError("VerifiedStoredObject cannot be subclassed")

    @property
    def sha256(self) -> str:
        return self._sha256

    @property
    def size_bytes(self) -> int:
        return self._size_bytes

    @property
    def content_type(self) -> str:
        return self._content_type

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def __iter__(self) -> Iterator[bytes]:
        return self.iter_bytes()

    def iter_bytes(self) -> Generator[bytes]:
        """Yield verified bytes once and close the spool on every exit path."""

        with self._lock:
            if self._closed:
                raise RuntimeError("verified stored object is closed")
            if self._iteration_started:
                raise RuntimeError("verified stored object is one-shot")
            self._iteration_started = True
        try:
            while True:
                with self._lock:
                    stream = self._stream
                if stream is None:
                    return
                chunk = stream.read(_OBJECT_STREAM_CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        """Close the private spool exactly once, including repeated callbacks."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            stream = self._stream
            self._stream = None
        if stream is not None:
            try:
                stream.close()
            except OSError as exc:
                raise ArtifactStorageUnavailableError(
                    "verified artifact spool could not be closed"
                ) from exc

    def __enter__(self) -> VerifiedStoredObject:
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> None:
        self.close()


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
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )


@lru_cache(maxsize=1)
def internal_s3_client() -> Any:
    """Return the private S3 client used only for server-side integrity checks."""

    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=2,
            read_timeout=5,
            retries={"mode": "standard", "max_attempts": 1},
            s3={"addressing_style": "path"},
        ),
    )


def _raise_storage_error(exc: Exception) -> Never:
    if isinstance(exc, ClientError):
        response = exc.response if isinstance(exc.response, dict) else {}
        metadata = response.get("ResponseMetadata", {})
        status_code = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        error = response.get("Error", {})
        error_code = str(error.get("Code", "")) if isinstance(error, dict) else ""
        missing_key = error_code in {"NoSuchKey", "NotFound"} or (
            status_code == 404 and error_code in {"", "404"}
        )
        if missing_key:
            raise ArtifactIntegrityError("artifact object is missing") from None
    raise ArtifactStorageUnavailableError("artifact storage is temporarily unavailable") from None


def _call_s3(operation: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        response = operation(**kwargs)
    except (BotoCoreError, ClientError, OSError) as exc:
        _raise_storage_error(exc)
    if not isinstance(response, dict):
        raise ArtifactStorageUnavailableError("artifact storage returned an invalid response")
    return response


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _require_object_headers(response: dict[str, Any], expectation: StoredObjectExpectation) -> None:
    try:
        content_length = int(response["ContentLength"])
        content_type = str(response["ContentType"]).strip().lower()
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactIntegrityError("artifact object metadata is incomplete") from exc
    if content_length != expectation.size_bytes:
        raise ArtifactIntegrityError("artifact object size does not match its record")
    if content_type != expectation.content_type.strip().lower():
        raise ArtifactIntegrityError("artifact object content type does not match its record")
    if expectation.required_metadata:
        raw_metadata = response.get("Metadata")
        if not isinstance(raw_metadata, Mapping):
            raise ArtifactIntegrityError("artifact object binding metadata is missing")
        metadata = {
            str(key).strip().lower(): str(value).strip()
            for key, value in raw_metadata.items()
        }
        if any(metadata.get(key) != value for key, value in expectation.required_metadata):
            raise ArtifactIntegrityError("artifact object binding metadata does not match")


def _stream_sha256(body: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        while True:
            chunk = body.read(_OBJECT_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise ArtifactStorageUnavailableError("artifact storage returned an invalid body")
            digest.update(chunk)
            size_bytes += len(chunk)
    except ArtifactStorageUnavailableError:
        raise
    except (BotoCoreError, ClientError, OSError) as exc:
        _raise_storage_error(exc)
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            try:
                close()
            except (BotoCoreError, ClientError, OSError) as exc:
                _raise_storage_error(exc)
    return digest.hexdigest(), size_bytes


def verify_stored_object(
    expectation: StoredObjectExpectation,
    *,
    stream_hash: bool,
) -> None:
    """Verify one object without exposing its key or provider error details."""

    settings = get_settings()
    try:
        client = internal_s3_client()
    except (BotoCoreError, ClientError, OSError) as exc:
        _raise_storage_error(exc)
    _verify_object_with_client(
        client,
        bucket=settings.s3_bucket,
        expectation=expectation,
        stream_hash=stream_hash,
    )


def read_verified_stored_object(
    expectation: StoredObjectExpectation,
    *,
    max_bytes: int,
) -> bytes:
    """Read one small artifact once while proving its bounded persisted bytes."""

    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if expectation.size_bytes > max_bytes:
        raise ArtifactIntegrityError("artifact object exceeds its review size limit")
    settings = get_settings()
    try:
        client = internal_s3_client()
    except (BotoCoreError, ClientError, OSError) as exc:
        _raise_storage_error(exc)
    return _read_verified_object_with_client(
        client,
        bucket=settings.s3_bucket,
        expectation=expectation,
        max_bytes=max_bytes,
    )


def open_verified_stored_object(
    expectation: StoredObjectExpectation,
    *,
    max_bytes: int,
) -> VerifiedStoredObject:
    """Fetch, bound and fully verify an object before making it streamable.

    Exactly one ``GetObject`` request is used.  Persisted response headers,
    binding metadata, byte count and SHA-256 are all checked before the sealed
    one-shot object is returned to a response layer.
    """

    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if (
        type(expectation.object_key) is not str
        or not expectation.object_key
        or type(expectation.sha256) is not str
        or not _valid_sha256(expectation.sha256)
        or type(expectation.size_bytes) is not int
        or expectation.size_bytes <= 0
        or expectation.size_bytes > max_bytes
        or type(expectation.content_type) is not str
        or not expectation.content_type.strip()
    ):
        raise ArtifactIntegrityError("artifact record is invalid or exceeds its download limit")
    settings = get_settings()
    if type(settings.s3_bucket) is not str or not settings.s3_bucket:
        raise ArtifactIntegrityError("artifact bucket is invalid")
    try:
        client = internal_s3_client()
    except (BotoCoreError, ClientError, OSError) as exc:
        _raise_storage_error(exc)
    return _open_verified_object_with_client(
        client,
        bucket=settings.s3_bucket,
        expectation=expectation,
        max_bytes=max_bytes,
    )


def _close_download_body(body: Any) -> None:
    close = getattr(body, "close", None)
    if not callable(close):
        raise ArtifactStorageUnavailableError("artifact storage returned an invalid body")
    try:
        close()
    except (BotoCoreError, ClientError, OSError) as exc:
        _raise_storage_error(exc)


def _close_failed_spool(stream: IO[bytes]) -> None:
    try:
        stream.close()
    except OSError:
        # Preserve the verification/provider failure that made this spool unusable.
        return


def _require_download_headers(
    response: dict[str, Any],
    expectation: StoredObjectExpectation,
) -> None:
    if type(response.get("ContentLength")) is not int:
        raise ArtifactIntegrityError("artifact object metadata is incomplete")
    _require_object_headers(response, expectation)
    raw_metadata = response.get("Metadata")
    if not isinstance(raw_metadata, Mapping):
        raise ArtifactIntegrityError("artifact object checksum metadata is missing")
    metadata = {
        str(key).strip().lower(): str(value).strip().lower()
        for key, value in raw_metadata.items()
    }
    stored_digest = metadata.get("sha256", "")
    if not _valid_sha256(stored_digest) or not hmac.compare_digest(
        stored_digest, expectation.sha256
    ):
        raise ArtifactIntegrityError("artifact object checksum metadata does not match its record")


def _open_verified_object_with_client(
    client: Any,
    *,
    bucket: str,
    expectation: StoredObjectExpectation,
    max_bytes: int,
) -> VerifiedStoredObject:
    response = _call_s3(
        client.get_object,
        Bucket=bucket,
        Key=expectation.object_key,
    )
    body = response.get("Body")
    if body is None:
        raise ArtifactStorageUnavailableError("artifact storage returned no body")

    spool: IO[bytes] | None = None
    verified: VerifiedStoredObject | None = None
    try:
        try:
            _require_download_headers(response, expectation)
            try:
                candidate = SpooledTemporaryFile(  # noqa: SIM115 - ownership is transferred
                    max_size=_VERIFIED_OBJECT_SPOOL_BYTES,
                    mode="w+b",
                )
            except OSError as exc:
                raise ArtifactStorageUnavailableError(
                    "verified artifact spool could not be created"
                ) from exc
            spool = candidate

            digest = hashlib.sha256()
            size_bytes = 0
            while True:
                try:
                    chunk = body.read(_OBJECT_STREAM_CHUNK_BYTES)
                except (BotoCoreError, ClientError, OSError) as exc:
                    _raise_storage_error(exc)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ArtifactStorageUnavailableError(
                        "artifact storage returned an invalid body"
                    )
                size_bytes += len(chunk)
                if size_bytes > expectation.size_bytes or size_bytes > max_bytes:
                    raise ArtifactIntegrityError("artifact object exceeds its download limit")
                try:
                    written = candidate.write(chunk)
                except OSError as exc:
                    raise ArtifactStorageUnavailableError(
                        "verified artifact spool could not be written"
                    ) from exc
                if written != len(chunk):
                    raise ArtifactStorageUnavailableError(
                        "verified artifact spool could not be written"
                    )
                digest.update(chunk)

            if size_bytes != expectation.size_bytes or not hmac.compare_digest(
                digest.hexdigest(), expectation.sha256
            ):
                raise ArtifactIntegrityError(
                    "artifact object checksum does not match its record"
                )
            try:
                candidate.seek(0)
            except OSError as exc:
                raise ArtifactStorageUnavailableError(
                    "verified artifact spool could not be rewound"
                ) from exc
            verified = VerifiedStoredObject(
                candidate,
                expectation,
                _seal=_VERIFIED_STORED_OBJECT_SEAL,
            )
        finally:
            _close_download_body(body)
    except BaseException:
        if spool is not None:
            _close_failed_spool(spool)
        raise

    if verified is None:
        raise ArtifactStorageUnavailableError("artifact verification did not complete")
    return verified


def _read_verified_object_with_client(
    client: Any,
    *,
    bucket: str,
    expectation: StoredObjectExpectation,
    max_bytes: int,
) -> bytes:
    if (
        not expectation.object_key
        or not _valid_sha256(expectation.sha256)
        or expectation.size_bytes <= 0
        or expectation.size_bytes > max_bytes
        or not expectation.content_type.strip()
    ):
        raise ArtifactIntegrityError("artifact record is invalid or exceeds its review limit")
    if not bucket:
        raise ArtifactIntegrityError("artifact bucket is invalid")
    response = _call_s3(
        client.get_object,
        Bucket=bucket,
        Key=expectation.object_key,
    )
    _require_object_headers(response, expectation)
    body = response.get("Body")
    if body is None:
        raise ArtifactStorageUnavailableError("artifact storage returned no body")
    payload = bytearray()
    try:
        while True:
            remaining = max_bytes + 1 - len(payload)
            if remaining <= 0:
                raise ArtifactIntegrityError("artifact object exceeds its review size limit")
            chunk = body.read(min(_OBJECT_STREAM_CHUNK_BYTES, remaining))
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise ArtifactStorageUnavailableError("artifact storage returned an invalid body")
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise ArtifactIntegrityError("artifact object exceeds its review size limit")
    except (ArtifactIntegrityError, ArtifactStorageUnavailableError):
        raise
    except (BotoCoreError, ClientError, OSError) as exc:
        _raise_storage_error(exc)
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            try:
                close()
            except (BotoCoreError, ClientError, OSError) as exc:
                _raise_storage_error(exc)
    content = bytes(payload)
    if len(content) != expectation.size_bytes or not hmac.compare_digest(
        hashlib.sha256(content).hexdigest(), expectation.sha256
    ):
        raise ArtifactIntegrityError("artifact object checksum does not match its record")
    return content


def _verify_object_with_client(
    client: Any,
    *,
    bucket: str,
    expectation: StoredObjectExpectation,
    stream_hash: bool,
) -> None:
    if (
        not expectation.object_key
        or not _valid_sha256(expectation.sha256)
        or expectation.size_bytes <= 0
        or not expectation.content_type.strip()
    ):
        raise ArtifactIntegrityError("artifact record is invalid")
    if not bucket:
        raise ArtifactIntegrityError("artifact bucket is invalid")
    parameters = {"Bucket": bucket, "Key": expectation.object_key}
    head = _call_s3(client.head_object, **parameters)
    _require_object_headers(head, expectation)
    metadata = head.get("Metadata")
    stored_digest = str(metadata.get("sha256", "")).lower() if isinstance(metadata, dict) else ""
    if stored_digest:
        if not _valid_sha256(stored_digest) or not hmac.compare_digest(
            stored_digest, expectation.sha256
        ):
            raise ArtifactIntegrityError("artifact object checksum does not match its record")
        if not stream_hash:
            return

    response = _call_s3(client.get_object, **parameters)
    _require_object_headers(response, expectation)
    body = response.get("Body")
    if body is None:
        raise ArtifactStorageUnavailableError("artifact storage returned no body")
    digest, size_bytes = _stream_sha256(body)
    if size_bytes != expectation.size_bytes or not hmac.compare_digest(digest, expectation.sha256):
        raise ArtifactIntegrityError("artifact object checksum does not match its record")


def _is_conditional_create_conflict(exc: ClientError) -> bool:
    response = exc.response if isinstance(exc.response, dict) else {}
    response_metadata = response.get("ResponseMetadata", {})
    status_code = (
        response_metadata.get("HTTPStatusCode") if isinstance(response_metadata, dict) else None
    )
    error = response.get("Error", {})
    error_code = str(error.get("Code", "")) if isinstance(error, dict) else ""
    return status_code in {409, 412} or error_code in {
        "PreconditionFailed",
        "ConditionalRequestConflict",
    }


def store_create_once_object(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    content: bytes,
    content_type: str,
    sha256: str,
    metadata: Mapping[str, str] | None = None,
) -> None:
    """Conditionally create an object and prove that its stored bytes are exact.

    A 409/412 race is idempotent only when a fresh HEAD plus streamed checksum
    proves the existing object has the requested digest, size and content type.
    Provider, authorization and transient failures remain availability errors.
    """

    raw_metadata = dict(metadata or {})
    normalized_metadata = {
        str(key).strip().lower(): str(value).strip()
        for key, value in raw_metadata.items()
    }
    if (
        not bucket
        or not object_key
        or not content
        or not content_type
        or not _valid_sha256(sha256)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_metadata.items()
        )
        or len(normalized_metadata) != len(raw_metadata)
        or any(
            not key
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", key) is None
            or not value
            or key in {"sha256", "immutable"}
            for key, value in normalized_metadata.items()
        )
    ):
        raise ArtifactIntegrityError("immutable object metadata is invalid")
    if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), sha256):
        raise ArtifactIntegrityError("immutable object checksum does not match its content")
    expectation = StoredObjectExpectation(
        object_key=object_key,
        sha256=sha256,
        size_bytes=len(content),
        content_type=content_type,
        required_metadata=tuple(sorted(normalized_metadata.items())),
    )
    try:
        client.put_object(
            Bucket=bucket,
            Key=object_key,
            Body=content,
            ContentLength=len(content),
            ContentType=content_type,
            Metadata={
                "sha256": sha256,
                "immutable": "true",
                **normalized_metadata,
            },
            IfNoneMatch="*",
        )
    except ClientError as exc:
        if not _is_conditional_create_conflict(exc):
            _raise_storage_error(exc)
    except (BotoCoreError, OSError) as exc:
        _raise_storage_error(exc)
    _verify_object_with_client(
        client,
        bucket=bucket,
        expectation=expectation,
        stream_hash=True,
    )


def store_evidence_object(
    object_key: str,
    content: bytes,
    content_type: str,
    sha256: str,
) -> None:
    """Persist external evidence with the same create-once proof as source assets."""

    store_immutable_object(object_key, content, content_type, sha256)


def store_immutable_object(
    object_key: str,
    content: bytes,
    content_type: str,
    sha256: str,
) -> None:
    """Create a checksum-addressed object once and verify the persisted bytes.

    A conditional write prevents a later request from replacing an existing
    source object.  A raced/idempotent upload is accepted only when the bytes
    already stored under the address still match the server-computed digest.
    """

    settings = get_settings()
    try:
        client = internal_s3_client()
    except (BotoCoreError, ClientError, OSError) as exc:
        _raise_storage_error(exc)
    store_create_once_object(
        client,
        bucket=settings.s3_bucket,
        object_key=object_key,
        content=content,
        content_type=content_type,
        sha256=sha256,
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
