from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from math import isfinite
from tempfile import SpooledTemporaryFile
from threading import Event, Lock, Timer
from typing import IO, Any, Never

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from custombuild_manufacturing import MAX_API_TRANSIENT_BYTES
from fastapi import HTTPException

from .config import get_settings

_OBJECT_STREAM_CHUNK_BYTES = 1024 * 1024
_OBJECT_STORAGE_TOTAL_READ_SECONDS = 30.0
_VERIFIED_OBJECT_SPOOL_BYTES = 8 * 1024 * 1024
_VERIFIED_STORED_OBJECT_SEAL = object()
_VERIFIED_STORED_OBJECT_ITERATOR_SEAL = object()
_ARTIFACT_SIGNATURE_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SHARED_STORAGE_READ_DEADLINE: ContextVar[float | None] = ContextVar(
    "shared_storage_read_deadline",
    default=None,
)
_STORAGE_RUNTIME_OVERRIDE: ContextVar[tuple[Any, str] | None] = ContextVar(
    "storage_runtime_override",
    default=None,
)


class ArtifactIntegrityError(Exception):
    """The persisted artifact no longer matches its checksum-bound record."""


class ArtifactStorageUnavailableError(Exception):
    """Artifact storage could not be verified without risking destructive repair."""


def _storage_read_deadline() -> float:
    local_deadline = time.monotonic() + _OBJECT_STORAGE_TOTAL_READ_SECONDS
    shared_deadline = _SHARED_STORAGE_READ_DEADLINE.get()
    return local_deadline if shared_deadline is None else min(local_deadline, shared_deadline)


@contextmanager
def storage_read_deadline(total_seconds: float) -> Iterator[None]:
    """Apply one absolute deadline across a multi-object review operation."""

    if isinstance(total_seconds, bool) or not isinstance(total_seconds, int | float):
        raise ValueError("storage read deadline must be a positive finite number")
    seconds = float(total_seconds)
    if not isfinite(seconds) or seconds <= 0:
        raise ValueError("storage read deadline must be a positive finite number")
    candidate = time.monotonic() + seconds
    current = _SHARED_STORAGE_READ_DEADLINE.get()
    _SHARED_STORAGE_READ_DEADLINE.set(
        candidate if current is None else min(candidate, current)
    )
    try:
        yield
    finally:
        # Request-scoped ASGI generators can be finalized in a sibling copied
        # context after disconnect.  Restoring by value is safe there, whereas
        # ContextVar.reset(token) raises and can strand caller-owned cleanup.
        _SHARED_STORAGE_READ_DEADLINE.set(current)


@contextmanager
def storage_runtime(*, client: Any, bucket: str) -> Iterator[None]:
    """Inject one service-owned S3 runtime into the canonical verification gates."""

    if not isinstance(bucket, str) or not bucket:
        raise ValueError("storage runtime bucket must be a non-empty string")
    current = _STORAGE_RUNTIME_OVERRIDE.get()
    if current is not None:
        if current[0] is not client or current[1] != bucket:
            raise RuntimeError("storage runtime cannot change during verification")
        yield
        return
    token = _STORAGE_RUNTIME_OVERRIDE.set((client, bucket))
    try:
        yield
    finally:
        _STORAGE_RUNTIME_OVERRIDE.reset(token)


def _verification_storage_runtime() -> tuple[Any, str]:
    override = _STORAGE_RUNTIME_OVERRIDE.get()
    if override is not None:
        return override
    settings = get_settings()
    try:
        client = internal_s3_client()
    except (BotoCoreError, ClientError, OSError) as exc:
        _raise_storage_error(exc)
    return client, settings.s3_bucket


def _require_storage_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise ArtifactStorageUnavailableError(
            "artifact storage body exceeded its total read deadline"
        )


@contextmanager
def _bounded_storage_body(body: Any, *, deadline: float) -> Iterator[None]:
    """Own and close one provider body, aborting a blocked read at its deadline."""

    close = getattr(body, "close", None)
    if not callable(close):
        raise ArtifactStorageUnavailableError("artifact storage returned an invalid body")

    state_lock = Lock()
    expired = Event()
    close_errors: list[Exception] = []
    abort_errors: list[Exception] = []
    closed = False

    def close_once() -> None:
        nonlocal closed
        with state_lock:
            if closed:
                return
            closed = True
        try:
            close()
        except Exception as exc:  # provider close failures are always private
            close_errors.append(exc)

    def abort() -> None:
        expired.set()
        # Botocore's StreamingBody.close() may block behind the same buffered
        # reader lock as a hung read.  urllib3 exposes shutdown() specifically
        # to tear down the raw socket first, which releases that reader before
        # the exactly-once close below.
        raw_stream = getattr(body, "_raw_stream", None)
        shutdown = getattr(raw_stream, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception as exc:  # provider details must never escape
                abort_errors.append(exc)
        close_once()

    remaining = deadline - time.monotonic()
    timer: Timer | None = None
    if remaining <= 0:
        expired.set()
    elif isfinite(remaining):
        timer = Timer(remaining, abort)
        timer.daemon = True
        timer.start()
    try:
        if expired.is_set():
            raise ArtifactStorageUnavailableError(
                "artifact storage body exceeded its total read deadline"
            )
        yield
    finally:
        if timer is not None:
            timer.cancel()
        close_once()
        if timer is not None:
            timer.join(timeout=1.0)
            if timer.is_alive():
                raise ArtifactStorageUnavailableError(
                    "artifact storage body could not be aborted at its deadline"
                )
        if abort_errors:
            _raise_storage_error(abort_errors[0])
        if close_errors:
            _raise_storage_error(close_errors[0])
        if expired.is_set():
            raise ArtifactStorageUnavailableError(
                "artifact storage body exceeded its total read deadline"
            )


def _read_storage_chunk(body: Any, size: int, *, deadline: float) -> bytes:
    """Read one bounded chunk while enforcing one total provider-body deadline."""

    _require_storage_deadline(deadline)
    try:
        chunk = body.read(size)
    except (BotoCoreError, ClientError, OSError) as exc:
        _raise_storage_error(exc)
    if time.monotonic() > deadline:
        raise ArtifactStorageUnavailableError(
            "artifact storage body exceeded its total read deadline"
        )
    if not isinstance(chunk, bytes):
        raise ArtifactStorageUnavailableError("artifact storage returned an invalid body")
    return chunk


class _TransientByteBudget:
    """Reserve a bounded share of the API container's finite transient space."""

    __slots__ = ("_capacity_bytes", "_lock", "_reserved_bytes")

    def __init__(self, capacity_bytes: int) -> None:
        if type(capacity_bytes) is not int or capacity_bytes <= 0:
            raise ValueError("verified spool capacity must be a positive integer")
        self._capacity_bytes = capacity_bytes
        self._reserved_bytes = 0
        self._lock = Lock()

    @property
    def capacity_bytes(self) -> int:
        return self._capacity_bytes

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return self._reserved_bytes

    def reserve(self, size_bytes: int) -> _TransientByteReservation:
        if type(size_bytes) is not int or size_bytes <= 0:
            raise ArtifactIntegrityError("artifact spool reservation size is invalid")
        with self._lock:
            if size_bytes > self._capacity_bytes - self._reserved_bytes:
                raise ArtifactStorageUnavailableError(
                    "verified artifact spool capacity is temporarily exhausted"
                )
            self._reserved_bytes += size_bytes
        return _TransientByteReservation(self, size_bytes)

    def _release(self, size_bytes: int) -> None:
        with self._lock:
            if size_bytes > self._reserved_bytes:
                raise RuntimeError("verified spool reservation accounting underflow")
            self._reserved_bytes -= size_bytes


class _TransientByteReservation:
    """Idempotent ownership token for one exact transient byte reservation."""

    __slots__ = ("_budget", "_lock", "_size_bytes")

    def __init__(self, budget: _TransientByteBudget, size_bytes: int) -> None:
        self._budget: _TransientByteBudget | None = budget
        self._size_bytes = size_bytes
        self._lock = Lock()

    def close(self) -> None:
        with self._lock:
            budget = self._budget
            self._budget = None
        if budget is not None:
            budget._release(self._size_bytes)


_TRANSIENT_BYTE_BUDGET = _TransientByteBudget(MAX_API_TRANSIENT_BYTES)


@contextmanager
def reserve_transient_bytes(size_bytes: int) -> Iterator[None]:
    """Hold capacity for multipart or retained verification bytes."""

    if type(size_bytes) is not int or size_bytes < 0:
        raise ValueError("transient byte reservation must be a non-negative integer")
    if size_bytes == 0:
        yield
        return
    reservation = _TRANSIENT_BYTE_BUDGET.reserve(size_bytes)
    try:
        yield
    finally:
        reservation.close()


@dataclass(frozen=True, slots=True)
class StoredObjectExpectation:
    object_key: str
    sha256: str
    size_bytes: int
    content_type: str
    required_metadata: tuple[tuple[str, str], ...] = ()


class _VerifiedStoredObjectIterator(Iterator[bytes]):
    """Close-aware iterator that owns its verified token from construction."""

    __slots__ = ("_owner",)

    def __init__(self, owner: VerifiedStoredObject, *, _seal: object) -> None:
        if _seal is not _VERIFIED_STORED_OBJECT_ITERATOR_SEAL:
            raise TypeError("verified stored object iterators are storage-owned")
        self._owner = owner

    def __iter__(self) -> _VerifiedStoredObjectIterator:
        return self

    def __next__(self) -> bytes:
        return self._owner._next_stream_chunk()

    def close(self) -> None:
        self._owner.close()


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
        "_reservation",
        "_sha256",
        "_size_bytes",
        "_stream",
    )

    def __init__(
        self,
        stream: IO[bytes],
        expectation: StoredObjectExpectation,
        *,
        reservation: _TransientByteReservation | None = None,
        _seal: object,
    ) -> None:
        if _seal is not _VERIFIED_STORED_OBJECT_SEAL:
            raise TypeError("VerifiedStoredObject instances are storage-owned")
        if reservation is None:
            raise TypeError("VerifiedStoredObject instances require storage-owned capacity")
        self._stream: IO[bytes] | None = stream
        self._reservation: _TransientByteReservation | None = reservation
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

    def iter_bytes(self) -> _VerifiedStoredObjectIterator:
        """Claim the one-shot stream and return its close-aware iterator."""

        with self._lock:
            if self._closed:
                raise RuntimeError("verified stored object is closed")
            if self._iteration_started:
                raise RuntimeError("verified stored object is one-shot")
            self._iteration_started = True
        return _VerifiedStoredObjectIterator(
            self,
            _seal=_VERIFIED_STORED_OBJECT_ITERATOR_SEAL,
        )

    def _next_stream_chunk(self) -> bytes:
        failure: ArtifactStorageUnavailableError | None = None
        chunk = b""
        try:
            with self._lock:
                if self._closed or self._stream is None:
                    raise StopIteration
                stream = self._stream
                chunk = stream.read(_OBJECT_STREAM_CHUNK_BYTES)
                if not isinstance(chunk, bytes):
                    failure = ArtifactStorageUnavailableError(
                        "verified artifact spool returned invalid bytes"
                    )
        except (OSError, ValueError) as exc:
            self.close()
            raise ArtifactStorageUnavailableError(
                "verified artifact spool could not be read"
            ) from exc
        except BaseException:
            self.close()
            raise

        if failure is not None:
            self.close()
            raise failure
        if not chunk:
            self.close()
            raise StopIteration
        return chunk

    def validation_bytes(self, *, max_bytes: int) -> bytes:
        """Read all verified bytes for validation, then rewind for streaming."""

        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        failure: ArtifactIntegrityError | ArtifactStorageUnavailableError | None = None
        payload: bytes | None = None
        with self._lock:
            if self._closed:
                raise RuntimeError("verified stored object is closed")
            if self._iteration_started:
                raise RuntimeError("verified stored object iteration has started")
            if self._size_bytes > max_bytes:
                raise ArtifactIntegrityError("verified artifact exceeds its validation size limit")
            stream = self._stream
            if stream is None:
                raise RuntimeError("verified stored object is closed")
            try:
                payload = _read_validation_payload(
                    stream,
                    size_bytes=self._size_bytes,
                    sha256=self._sha256,
                )
            except (ArtifactIntegrityError, ArtifactStorageUnavailableError) as exc:
                failure = exc
        if failure is not None:
            self.close()
            raise failure
        if payload is None:
            raise RuntimeError("verified artifact validation returned no bytes")
        return payload

    def close(self) -> None:
        """Close the private spool exactly once, including repeated callbacks."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            stream = self._stream
            self._stream = None
            reservation = self._reservation
            self._reservation = None
        failure: OSError | None = None
        try:
            if stream is not None:
                stream.close()
        except OSError as exc:
            failure = exc
        finally:
            if reservation is not None:
                reservation.close()
        if failure is not None:
            raise ArtifactStorageUnavailableError(
                "verified artifact spool could not be closed"
            ) from failure

    def __enter__(self) -> VerifiedStoredObject:
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> None:
        self.close()


def _read_validation_payload(
    stream: IO[bytes],
    *,
    size_bytes: int,
    sha256: str,
) -> bytes:
    try:
        stream.seek(0)
        payload = bytearray()
        digest = hashlib.sha256()
        while len(payload) < size_bytes:
            chunk = stream.read(min(_OBJECT_STREAM_CHUNK_BYTES, size_bytes - len(payload)))
            if not isinstance(chunk, bytes):
                raise ArtifactStorageUnavailableError(
                    "verified artifact spool returned invalid bytes"
                )
            if not chunk:
                raise ArtifactIntegrityError("verified artifact spool is incomplete")
            payload.extend(chunk)
            digest.update(chunk)
        trailing = stream.read(1)
        if not isinstance(trailing, bytes):
            raise ArtifactStorageUnavailableError("verified artifact spool returned invalid bytes")
        if trailing or not hmac.compare_digest(digest.hexdigest(), sha256):
            raise ArtifactIntegrityError(
                "verified artifact spool does not match its checksum-bound bytes"
            )
        stream.seek(0)
    except (OSError, ValueError) as exc:
        raise ArtifactStorageUnavailableError("verified artifact spool could not be read") from exc
    return bytes(payload)


def sign_artifact_access(artifact_id: str, organization_id: str, expires_at: int) -> str:
    message = f"{artifact_id}:{organization_id}:{expires_at}".encode()
    return hmac.new(
        get_settings().artifact_signing_secret.encode(), message, hashlib.sha256
    ).hexdigest()


def verify_artifact_access(
    artifact_id: str, organization_id: str, expires_at: int, signature: str
) -> None:
    if type(expires_at) is not int:
        raise HTTPException(status_code=403, detail="Invalid artifact expiry")
    now = int(time.time())
    if expires_at <= now:
        raise HTTPException(status_code=410, detail="Artifact link expired")
    if expires_at > now + get_settings().artifact_url_ttl_seconds:
        raise HTTPException(status_code=403, detail="Invalid artifact expiry")
    if type(signature) is not str or _ARTIFACT_SIGNATURE_PATTERN.fullmatch(signature) is None:
        raise HTTPException(status_code=403, detail="Invalid artifact signature")
    expected = sign_artifact_access(artifact_id, organization_id, expires_at)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="Invalid artifact signature")


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
            str(key).strip().lower(): str(value).strip() for key, value in raw_metadata.items()
        }
        if any(metadata.get(key) != value for key, value in expectation.required_metadata):
            raise ArtifactIntegrityError("artifact object binding metadata does not match")


def _stream_sha256(
    body: Any,
    *,
    expected_size: int,
    deadline: float,
) -> tuple[str, int]:
    with _bounded_storage_body(body, deadline=deadline):
        return _stream_sha256_open(
            body,
            expected_size=expected_size,
            deadline=deadline,
        )


def _stream_sha256_open(
    body: Any,
    *,
    expected_size: int,
    deadline: float,
) -> tuple[str, int]:
    """Hash an already-owned body; the caller must close it exactly once."""

    digest = hashlib.sha256()
    size_bytes = 0
    while True:
        remaining = expected_size + 1 - size_bytes
        if remaining <= 0:
            raise ArtifactIntegrityError("artifact object exceeds its recorded size")
        chunk = _read_storage_chunk(
            body,
            min(_OBJECT_STREAM_CHUNK_BYTES, remaining),
            deadline=deadline,
        )
        if not chunk:
            break
        size_bytes += len(chunk)
        if size_bytes > expected_size:
            raise ArtifactIntegrityError("artifact object exceeds its recorded size")
        digest.update(chunk)
    return digest.hexdigest(), size_bytes


def verify_stored_object(
    expectation: StoredObjectExpectation,
    *,
    stream_hash: bool,
) -> None:
    """Verify one object without exposing its key or provider error details."""

    client, bucket = _verification_storage_runtime()
    _verify_object_with_client(
        client,
        bucket=bucket,
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
    client, bucket = _verification_storage_runtime()
    return _read_verified_object_with_client(
        client,
        bucket=bucket,
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
    client, bucket = _verification_storage_runtime()
    if type(bucket) is not str or not bucket:
        raise ArtifactIntegrityError("artifact bucket is invalid")
    reservation = _TRANSIENT_BYTE_BUDGET.reserve(expectation.size_bytes)
    try:
        return _open_verified_object_with_client(
            client,
            bucket=bucket,
            expectation=expectation,
            max_bytes=max_bytes,
            reservation=reservation,
        )
    except BaseException:
        reservation.close()
        raise


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
        str(key).strip().lower(): str(value).strip().lower() for key, value in raw_metadata.items()
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
    reservation: _TransientByteReservation,
) -> VerifiedStoredObject:
    deadline = _storage_read_deadline()
    _require_storage_deadline(deadline)
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
        with _bounded_storage_body(body, deadline=deadline):
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
                remaining = expectation.size_bytes + 1 - size_bytes
                if remaining <= 0:
                    raise ArtifactIntegrityError("artifact object exceeds its download limit")
                chunk = _read_storage_chunk(
                    body,
                    min(_OBJECT_STREAM_CHUNK_BYTES, remaining),
                    deadline=deadline,
                )
                if not chunk:
                    break
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
                raise ArtifactIntegrityError("artifact object checksum does not match its record")
            try:
                candidate.seek(0)
            except OSError as exc:
                raise ArtifactStorageUnavailableError(
                    "verified artifact spool could not be rewound"
                ) from exc
            verified = VerifiedStoredObject(
                candidate,
                expectation,
                reservation=reservation,
                _seal=_VERIFIED_STORED_OBJECT_SEAL,
            )
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
    deadline = _storage_read_deadline()
    _require_storage_deadline(deadline)
    response = _call_s3(
        client.get_object,
        Bucket=bucket,
        Key=expectation.object_key,
    )
    body = response.get("Body")
    if body is None:
        raise ArtifactStorageUnavailableError("artifact storage returned no body")
    payload = bytearray()
    with _bounded_storage_body(body, deadline=deadline):
        _require_object_headers(response, expectation)
        while True:
            remaining = expectation.size_bytes + 1 - len(payload)
            if remaining <= 0:
                raise ArtifactIntegrityError("artifact object exceeds its review size limit")
            chunk = _read_storage_chunk(
                body,
                min(_OBJECT_STREAM_CHUNK_BYTES, remaining),
                deadline=deadline,
            )
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > expectation.size_bytes:
                raise ArtifactIntegrityError("artifact object exceeds its review size limit")
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
    deadline = _storage_read_deadline()
    parameters = {"Bucket": bucket, "Key": expectation.object_key}
    _require_storage_deadline(deadline)
    head = _call_s3(client.head_object, **parameters)
    _require_storage_deadline(deadline)
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

    _require_storage_deadline(deadline)
    response = _call_s3(client.get_object, **parameters)
    body = response.get("Body")
    if body is None:
        raise ArtifactStorageUnavailableError("artifact storage returned no body")
    with _bounded_storage_body(body, deadline=deadline):
        _require_object_headers(response, expectation)
        digest, size_bytes = _stream_sha256_open(
            body,
            expected_size=expectation.size_bytes,
            deadline=deadline,
        )
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
        str(key).strip().lower(): str(value).strip() for key, value in raw_metadata.items()
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
